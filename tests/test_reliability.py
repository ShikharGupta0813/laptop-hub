"""Backup + watchdog: fake clock, runner, urlopen and prompt. Nothing real (schtasks, tailscale, network) is touched."""
import argparse
import datetime as dt
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from hub import backup, watchdog
from hub import config as cfg
from hub import setup_wizard as sw
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import make_config
from tests.test_setup_wizard import Base, Script

NOW = dt.datetime(2026, 11, 15, 11, 0, 0)
TOPIC = "redline-secret-topic-1234"


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cdir = self.root / "cfg"
        self.cdir.mkdir()
        self.path = self.cdir / "hub.json"
        self.config, _ = cfg.make()
        self.config.data_dir = str(self.root / "data")
        self.config.notes_dir = str(self.root / "HubNotes")
        self.config.inbox_dir = str(self.root / "HubInbox")
        self.config.ntfy_topic = TOPIC
        for d in (self.config.data_dir, self.config.notes_dir, self.config.inbox_dir):
            Path(d).mkdir(parents=True)
        cfg.save(self.config, self.path)
        (self.cdir / "update.log").write_text("pulled\n")
        con = sqlite3.connect(Path(self.config.data_dir) / "notes.db")
        con.execute("create table n(t text)")
        con.execute("insert into n values ('hello')")
        con.commit()
        con.close()
        (Path(self.config.data_dir) / "watch.json").write_text("{}")
        (Path(self.config.notes_dir) / "a.md").write_text("# a")
        (Path(self.config.inbox_dir) / "b.txt").write_text("b")
        # junk that must stay out
        (Path(self.config.data_dir) / "trash").mkdir()
        (Path(self.config.data_dir) / "trash" / "old.txt").write_text("x")
        (Path(self.config.data_dir) / "model.gguf").write_text("x")
        (Path(self.config.notes_dir) / ".git").mkdir()
        (Path(self.config.notes_dir) / ".git" / "HEAD").write_text("x")
        (Path(self.config.notes_dir) / ".venv").mkdir()
        (Path(self.config.notes_dir) / ".venv" / "py").write_text("x")
        self.dest = self.root / "out"


class BackupTest(Fixture):
    def make(self, when=NOW, **kw):
        return backup.make_backup(self.path, self.dest, now=lambda: when, say=lambda _m: None, **kw)

    def test_zip_contents_and_exclusions(self):
        code, z, info = self.make()
        self.assertEqual(code, 0)
        self.assertEqual(z.name, "redline-backup-2026-11-15.zip")
        names = set(zipfile.ZipFile(z).namelist())
        self.assertTrue({"config/hub.json", "config/update.log", "data/notes.db", "data/watch.json",
                         "notes/a.md", "inbox/b.txt", "manifest.json"} <= names, names)
        for bad in ("data/trash/old.txt", "data/model.gguf", "notes/.git/HEAD", "notes/.venv/py"):
            self.assertNotIn(bad, names)
        self.assertGreater(info["bytes"], 0)

    def test_sqlite_snapshot_is_readable(self):
        _, z, _ = self.make()
        out = self.root / "x.db"
        out.write_bytes(zipfile.ZipFile(z).read("data/notes.db"))
        self.assertEqual(sqlite3.connect(out).execute("select t from n").fetchone(), ("hello",))

    def test_no_secrets_in_zip(self):
        _, z, _ = self.make()
        blob = zipfile.ZipFile(z).read("config/hub.json").decode()
        self.assertNotIn(self.config.token_hash, blob)
        self.assertNotIn(self.config.session_secret.hex(), blob)
        data = json.loads(blob)
        self.assertEqual((data["token_hash"], data["session_secret"]), ("", ""))
        self.assertEqual(data["notes_dir"], self.config.notes_dir)

    def test_rotation_keeps_newest_8_and_other_files(self):
        self.dest.mkdir()
        (self.dest / "keep-me.txt").write_text("x")
        for day in range(1, 11):
            (self.dest / f"redline-backup-2026-11-{day:02d}.zip").write_bytes(b"x")
        _, _, info = self.make()
        left = sorted(p.name for p in self.dest.glob("redline-backup-*.zip"))
        self.assertEqual(len(left), 8)
        self.assertIn("redline-backup-2026-11-15.zip", left)
        self.assertNotIn("redline-backup-2026-11-01.zip", left)
        self.assertTrue((self.dest / "keep-me.txt").exists())
        self.assertEqual(len(info["removed"]), 3)

    def test_unreadable_file_is_noted_not_fatal(self):
        bad = Path(self.config.notes_dir) / "locked.txt"
        bad.write_text("x")
        real = zipfile.ZipFile.write

        def flaky(self_, filename, arcname=None, *a, **k):
            if str(arcname) == "notes/locked.txt":
                raise PermissionError("locked")
            return real(self_, filename, arcname, *a, **k)
        zipfile.ZipFile.write = flaky
        try:
            code, z, info = self.make()
        finally:
            zipfile.ZipFile.write = real
        self.assertEqual(code, 0)
        self.assertTrue(any("locked.txt" in s for s in info["skipped"]))
        self.assertIn("notes/a.md", zipfile.ZipFile(z).namelist())

    def test_backup_dir_in_config_and_missing_config(self):
        self.config.backup_dir = str(self.root / "custom")
        cfg.save(self.config, self.path)
        code, z, _ = backup.make_backup(self.path, None, now=lambda: NOW, say=lambda _m: None)
        self.assertEqual(code, 0)
        self.assertEqual(z.parent, self.root / "custom")
        self.assertEqual(backup.make_backup(self.root / "nope.json", self.dest, say=lambda _m: None)[0], 1)

    def test_run_pushes_size_and_survives_push_failure(self):
        pushes = []
        args = argparse.Namespace(config=str(self.path), to=str(self.dest), restore=None)
        self.assertEqual(backup.run(args, now=lambda: NOW, push=lambda *a: pushes.append(a)), 0)
        self.assertTrue(pushes[0][2].startswith("Backup done: "))

        def boom(*a):
            raise RuntimeError("net")
        self.assertEqual(backup.run(args, now=lambda: NOW, push=boom), 0)


class RestoreTest(Fixture):
    def setUp(self):
        super().setUp()
        self.zip = backup.make_backup(self.path, self.dest, now=lambda: NOW, say=lambda _m: None)[1]
        self.out = []
        # change the live state after the backup
        (Path(self.config.notes_dir) / "a.md").write_text("# CHANGED")
        (Path(self.config.notes_dir) / "new.md").write_text("new")
        self.config.display_name = "Changed"
        self.config.token_hash = "newhash"
        cfg.save(self.config, self.path)

    def restore(self, answer, zip_path=None):
        return backup.restore(zip_path or self.zip, self.path, confirm=lambda _p: answer,
                              now=lambda: dt.datetime(2026, 11, 16, 9, 0, 0), say=self.out.append, safety_dir=self.dest)

    def test_wrong_confirmation_changes_nothing(self):
        self.assertEqual(self.restore("yes"), 1)
        self.assertEqual((Path(self.config.notes_dir) / "a.md").read_text(), "# CHANGED")
        self.assertFalse(list(self.dest.glob("before-restore-*")))

    def test_restore_with_safety_copy_and_kept_login(self):
        self.assertEqual(self.restore("RESTORE"), 0)
        self.assertEqual((Path(self.config.notes_dir) / "a.md").read_text(), "# a")
        safety = list(self.dest.glob("before-restore-*.zip"))
        self.assertEqual(len(safety), 1)
        self.assertIn("notes/new.md", zipfile.ZipFile(safety[0]).namelist())
        loaded = cfg.load(self.path)
        self.assertEqual(loaded.token_hash, "newhash")          # this laptop's login survives
        self.assertEqual(loaded.display_name, "Chief")          # settings come from the backup
        self.assertIn("restart the hub", "\n".join(self.out))

    def test_not_a_backup_and_unsafe_paths(self):
        junk = self.root / "junk.zip"
        junk.write_bytes(b"not a zip")
        self.assertEqual(self.restore("RESTORE", junk), 2)
        evil = self.root / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("config/hub.json", "{}")
            zf.writestr("data/../../escape.txt", "x")
        self.assertEqual(self.restore("RESTORE", evil), 1)
        self.assertFalse((self.root.parent / "escape.txt").exists())


# ---------------------------------------------------------------------------------------------------------------

class FakeTools:
    """Scripted `schtasks` and `tailscale`. Each key is a command prefix; the value is a list of (rc, out), the last one repeats."""
    def __init__(self, **answers):
        self.answers, self.calls = {}, []
        for key, val in answers.items():
            self.answers[key] = list(val)

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        key = " ".join(argv)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if key.startswith(prefix.replace("_", " ")):
                seq = self.answers[prefix]
                return seq.pop(0) if len(seq) > 1 else seq[0]
        return 0, ""

    def ran(self, prefix):
        return [c for c, _ in self.calls if " ".join(c).startswith(prefix)]


HEALTHY = dict(**{
    "schtasks /Query": [(0, "")], "tailscale status": [(0, "100.1.1.1 laptop me@ windows -")],
    "tailscale serve status": [(0, "https://x.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765")]})


class WatchdogTest(Fixture):
    def dog(self, tools, health, **kw):
        self.pushes, self.sleeps, self.lines = [], [], []
        seq = list(health)
        return watchdog.Watchdog(self.config, self.path, runner=tools, sleep=self.sleeps.append,
                                 health=lambda url, t: (seq.pop(0) if len(seq) > 1 else seq[0]),
                                 now=kw.get("now", lambda: NOW),
                                 push=lambda title, body, prio="default": self.pushes.append((title, body, prio)) or True,
                                 say=self.lines.append)

    def log_text(self):
        return (self.cdir / "watchdog.log").read_text()

    def test_healthy_is_quiet(self):
        tools = FakeTools(**HEALTHY)
        self.assertEqual(self.dog(tools, [True]).run_once(), 0)
        self.assertEqual(self.pushes, [])
        self.assertEqual(tools.ran("schtasks /Run"), [])
        self.assertIn("ok hub=ok tailscale=ok serve=ok", self.log_text())
        self.assertEqual(len(self.log_text().splitlines()), 1)

    def test_health_url_and_timeout(self):
        urls = []
        d = watchdog.Watchdog(self.config, self.path, runner=FakeTools(**HEALTHY), say=lambda _m: None,
                              health=lambda u, t: urls.append((u, t)) or True, push=lambda *a, **k: True)
        d.run_once()
        self.assertEqual(urls[0], ("http://127.0.0.1:8765/api/health", 5))

    def test_hub_down_runs_task_waits_rechecks_and_pushes(self):
        tools = FakeTools(**HEALTHY)
        d = self.dog(tools, [False, True])
        self.assertEqual(d.run_once(), 0)
        self.assertEqual(tools.ran("schtasks /Run /TN LaptopHub"), [["schtasks", "/Run", "/TN", "LaptopHub"]])
        self.assertEqual(self.sleeps, [10])
        self.assertEqual([p[1] for p in self.pushes], ["Watchdog: restarted the hub"])
        self.assertIn("repaired", self.log_text())

    def test_hub_does_not_come_back_alerts_and_never_kills(self):
        tools = FakeTools(**HEALTHY)
        self.assertEqual(self.dog(tools, [False]).run_once(), 1)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("did not restart", self.pushes[0][1])
        self.assertEqual(tools.ran("schtasks /End"), [])
        self.assertEqual(tools.ran("taskkill"), [])

    def test_task_missing_does_not_run(self):
        tools = FakeTools(**{**HEALTHY, "schtasks /Query": [(1, "ERROR")]})
        self.assertEqual(self.dog(tools, [False]).run_once(), 1)
        self.assertEqual(tools.ran("schtasks /Run"), [])
        self.assertIn("task is missing", self.pushes[0][1])

    def test_tailscale_stopped_runs_up_with_30s_timeout(self):
        tools = FakeTools(**{**HEALTHY, "tailscale status": [(0, "Tailscale is stopped."), (0, "100.1.1.1 laptop")]})
        self.assertEqual(self.dog(tools, [True]).run_once(), 0)
        up = [(c, t) for c, t in tools.calls if c == ["tailscale", "up"]]
        self.assertEqual(up, [(["tailscale", "up"], 30)])
        self.assertEqual([p[1] for p in self.pushes], ["Watchdog: started Tailscale again"])

    def test_tailscale_logged_out_only_pushes(self):
        tools = FakeTools(**{**HEALTHY, "tailscale status": [(1, "Logged out.\nLog in at: https://login.tailscale.com/a/x")]})
        self.assertEqual(self.dog(tools, [True]).run_once(), 1)
        self.assertEqual(tools.ran("tailscale up"), [])
        self.assertEqual(tools.ran("tailscale serve"), [])
        self.assertEqual([p[1] for p in self.pushes], ["Watchdog: Tailscale is logged out - open the laptop"])

    def test_logged_out_alert_is_not_repeated_every_pass(self):
        tools = FakeTools(**{**HEALTHY, "tailscale status": [(1, "Logged out.")]})
        t = [NOW]
        d = self.dog(tools, [True], now=lambda: t[0])
        d.run_once()
        t[0] = NOW + dt.timedelta(minutes=10)
        d.run_once()
        self.assertEqual(len(self.pushes), 1)
        t[0] = NOW + dt.timedelta(hours=7)
        d.run_once()
        self.assertEqual(len(self.pushes), 2)

    def test_serve_missing_is_restored(self):
        tools = FakeTools(**{**HEALTHY, "tailscale serve status": [(0, "No serve config")]})
        self.assertEqual(self.dog(tools, [True]).run_once(), 0)
        self.assertEqual(tools.ran("tailscale serve --bg"), [["tailscale", "serve", "--bg", "8765"]])
        self.assertEqual(len(self.pushes), 1)

    def test_tailscale_not_installed_is_quiet(self):
        tools = FakeTools(**{**HEALTHY, "tailscale status": [(127, "program not found")],
                             "tailscale serve status": [(127, "program not found")]})
        self.assertEqual(self.dog(tools, [True]).run_once(), 0)
        self.assertEqual(self.pushes, [])

    def test_log_rotates_at_1mb(self):
        log = self.cdir / "watchdog.log"
        log.write_text("x" * (watchdog.LOG_MAX_BYTES + 10))
        self.dog(FakeTools(**HEALTHY), [True]).run_once()
        self.assertTrue((self.cdir / "watchdog.log.1").exists())
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_unexpected_error_does_not_raise(self):
        def boom(argv, timeout):
            raise RuntimeError("x")
        self.assertEqual(self.dog(boom, [True]).run_once(), 1)
        self.assertIn("ERROR", self.log_text())

    def test_run_once_flag_and_missing_config(self):
        args = argparse.Namespace(config=str(self.path), once=True)
        self.assertEqual(watchdog.run(args, runner=FakeTools(**HEALTHY), health=lambda u, t: True,
                                      push=lambda *a, **k: True, say=lambda _m: None), 0)
        self.assertEqual(watchdog.run(argparse.Namespace(config=str(self.root / "no.json"), once=True)), 1)


class PushTest(Fixture):
    def test_push_goes_straight_to_ntfy_with_config_topic(self):
        seen = []

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def opener(req, timeout):
            seen.append((req.full_url, req.data, req.get_header("Title"), timeout))
            return Resp()
        self.assertTrue(watchdog.push_ntfy(self.config, "REDLINE backup", "Backup done: 14 MB", opener=opener))
        self.assertEqual(seen[0], (f"https://ntfy.sh/{TOPIC}", b"Backup done: 14 MB", "REDLINE backup", 8))

    def test_app_chosen_topic_wins_and_no_topic_means_no_push(self):
        (Path(self.config.data_dir) / "notify_settings.json").write_text(json.dumps({"ntfy_topic": "app-topic-12345"}))
        self.assertEqual(watchdog.active_topic(self.config), "app-topic-12345")
        self.config.ntfy_topic = ""
        (Path(self.config.data_dir) / "notify_settings.json").unlink()
        self.assertFalse(watchdog.push_ntfy(self.config, "t", "b", opener=lambda *a, **k: self.fail("sent")))

    def test_failed_send_returns_false(self):
        def opener(req, timeout):
            raise OSError("down")
        self.assertFalse(watchdog.push_ntfy(self.config, "t", "b", opener=opener))


class HealthRouteTest(unittest.TestCase):
    def test_health_is_open_and_tiny(self):
        config, root = make_config()
        client = TestClient(create_app(config, deps=Deps(llm=FakeLLM(), transcriber=FakeTranscriber(),
                                                         assistant=FakeAssistant()), config_path=root / "hub.json"))
        r = client.get("/api/health")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))


class WizardTest(Base):
    def test_backup_step_registers_weekly_task(self):
        result = sw.step_backup(self.wizard(Script()))
        self.assertEqual(result.status, "ok")
        ps = self.sys.ran[0][-1]
        self.assertIn("LaptopHubBackup", ps)
        self.assertIn("-Weekly -DaysOfWeek Sunday -At 11:00", ps)
        self.assertIn("-m hub", ps)
        self.assertIn(" backup", ps)

    def test_watchdog_step_registers_10_minute_task(self):
        result = sw.step_watchdog(self.wizard(Script()))
        self.assertEqual(result.status, "ok")
        ps = self.sys.ran[0][-1]
        self.assertIn("LaptopHubWatchdog", ps)
        self.assertIn("watchdog --once", ps)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 10)", ps)

    def test_failure_and_non_windows(self):
        self.sys.run_rc = 1
        self.assertEqual(sw.step_watchdog(self.wizard(Script())).status, "fail")
        self.sys.platform = "linux"
        self.assertEqual(sw.step_backup(self.wizard(Script())).status, "skip")
        self.assertEqual(sw.check_watchdog(self.wizard(Script())).status, "skip")

    def test_checks_and_only(self):
        self.sys.run_rc = 1
        self.assertEqual(sw.check_backup(self.wizard(Script())).status, "warn")
        self.assertEqual(sw.check_watchdog(self.wizard(Script())).status, "warn")
        self.sys.run_rc = 0
        self.assertEqual(sw.check_backup(self.wizard(Script())).status, "ok")
        self.assertEqual(sw.check_watchdog(self.wizard(Script())).status, "ok")
        self.assertEqual(sw.parse_only("backup,watchdog"), ["backup", "watchdog"])
        self.assertEqual(sw.parse_only("guard"), ["watchdog"])
        self.assertIn("backup", sw.STEP_IDS)
        self.assertIn("watchdog", sw.STEP_IDS)

    def test_doctor_lines(self):
        from hub import __main__ as cli
        config, _ = cfg.make()
        cfg.save(config, self.path)
        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            cli.cmd_doctor(argparse.Namespace(config=str(self.path)))
        out = buf.getvalue()
        self.assertIn("watchdog:", out)
        self.assertIn("weekly backup:", out)

    def test_cli_parses_new_commands(self):
        from hub import __main__ as cli
        calls = []
        orig = (cli.cmd_backup, cli.cmd_watchdog)
        try:
            cli.cmd_backup = lambda a: calls.append(("b", a.to, a.restore)) or 0
            cli.cmd_watchdog = lambda a: calls.append(("w", a.once)) or 0
            self.assertEqual(cli.main(["backup", "--to", "D"]), 0)
            self.assertEqual(cli.main(["watchdog", "--once"]), 0)
        finally:
            cli.cmd_backup, cli.cmd_watchdog = orig
        self.assertEqual(calls, [("b", "D", None), ("w", True)])


if __name__ == "__main__":
    unittest.main()
