"""Updater, install-updater, /api/status version and /api/update-log. No real git, schtasks or network."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hub import config as cfg
from hub import setup_wizard as sw
from hub import updater
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config
from tests.test_setup_wizard import Base, Script

OLD, NEW = "a" * 40, "b" * 40


class FakeRunner:
    def __init__(self, dirty="", pull_rc=0, fetch_rc=0, new=NEW, changed="", pip_rc=0, run_rc=0):
        self.calls, self.dirty, self.pull_rc, self.fetch_rc = [], dirty, pull_rc, fetch_rc
        self.new, self.changed, self.pip_rc, self.run_rc = new, changed, pip_rc, run_rc
        self.pulled = False

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        a = argv[1:] if argv[0] == "git" else argv
        if argv[0] == "git":
            if a[0] == "status": return 0, self.dirty
            if a[0] == "rev-parse": return 0, (self.new if self.pulled else OLD)
            if a[0] == "fetch": return self.fetch_rc, "fetch out"
            if a[0] == "pull":
                self.pulled = self.pull_rc == 0
                return self.pull_rc, "pull out"
            if a[0] == "diff": return 0, self.changed
        if "pip" in argv: return self.pip_rc, "pip out"
        if argv[0] == "schtasks" and argv[1] == "/Run": return self.run_rc, ""
        return 0, ""

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        (self.root / ".git").mkdir(parents=True)
        self.log = Path(tmp.name) / "cfg" / "update.log"
        self.sleeps = []

    def run_update(self, runner, restart=True, root=None):
        u = updater.Updater(root or self.root, self.log, runner=runner, sleep=self.sleeps.append, python="PY",
                            now=lambda: dt.datetime(2026, 11, 12, 9, 0, 0))
        return u.run(restart=restart)

    def test_dirty_tree_refuses_and_never_pulls_or_stops(self):
        r = FakeRunner(dirty=" M hub/server.py\n")
        self.assertEqual(self.run_update(r), updater.EXIT_DIRTY)
        self.assertEqual(r.ran("git", "pull"), [])
        self.assertEqual(r.ran("schtasks"), [])
        self.assertIn("REFUSED", self.log.read_text())

    def test_not_a_git_checkout(self):
        r = FakeRunner()
        self.assertEqual(self.run_update(r, root=self.root.parent), updater.EXIT_NOT_GIT)
        self.assertEqual(r.calls, [])

    def test_pull_is_ff_only_then_restart_after_wait(self):
        r = FakeRunner()
        self.assertEqual(self.run_update(r), 0)
        self.assertEqual(r.ran("git", "pull"), [["git", "pull", "--ff-only"]])
        end, run = r.ran("schtasks", "/End"), r.ran("schtasks", "/Run")
        self.assertEqual(end, [["schtasks", "/End", "/TN", "LaptopHub"]])
        self.assertEqual(run, [["schtasks", "/Run", "/TN", "LaptopHub"]])
        self.assertLess(r.calls.index(end[0]), r.calls.index(run[0]))
        self.assertEqual(self.sleeps, [3])
        self.assertEqual(r.ran("PY"), [])   # requirements unchanged: no pip
        text = self.log.read_text()
        self.assertIn("aaaaaaa -> bbbbbbb", text)
        self.assertIn("2026-11-12 09:00:00", text)

    def test_requirements_change_runs_pip_with_same_interpreter(self):
        r = FakeRunner(changed="requirements.txt\n")
        self.assertEqual(self.run_update(r), 0)
        self.assertEqual(r.ran("PY"), [["PY", "-m", "pip", "install", "-r", "requirements.txt"]])

    def test_pip_failure_does_not_stop_the_hub(self):
        r = FakeRunner(changed="requirements.txt\n", pip_rc=1)
        self.assertEqual(self.run_update(r), updater.EXIT_FAILED)
        self.assertEqual(r.ran("schtasks"), [])

    def test_fetch_and_pull_failures_do_not_stop_the_hub(self):
        for kw in ({"fetch_rc": 1}, {"pull_rc": 1}):
            with self.subTest(kw=kw):
                r = FakeRunner(**kw)
                self.assertEqual(self.run_update(r), updater.EXIT_FAILED)
                self.assertEqual(r.ran("schtasks"), [])
        self.assertIn("FAILED", self.log.read_text())

    def test_already_up_to_date_skips_restart(self):
        r = FakeRunner(new=OLD)
        self.assertEqual(self.run_update(r), 0)
        self.assertEqual(r.ran("schtasks"), [])
        self.assertIn("already up to date", self.log.read_text())

    def test_without_restart_flag_task_is_untouched(self):
        r = FakeRunner()
        self.assertEqual(self.run_update(r, restart=False), 0)
        self.assertEqual(r.ran("schtasks"), [])

    def test_run_failure_after_stop_is_reported(self):
        r = FakeRunner(run_rc=1)
        self.assertEqual(self.run_update(r), updater.EXIT_FAILED)
        self.assertIn("could not start", self.log.read_text())


class VersionTest(unittest.TestCase):
    def test_read_version_from_ref_and_packed_and_detached(self):
        with tempfile.TemporaryDirectory() as t:
            git = Path(t) / ".git"
            (git / "refs" / "heads").mkdir(parents=True)
            (git / "HEAD").write_text("ref: refs/heads/main\n")
            self.assertEqual(updater.read_version(Path(t)), "")
            (git / "packed-refs").write_text("# pack\n" + "c" * 40 + " refs/heads/main\n")
            self.assertEqual(updater.read_version(Path(t)), "ccccccc")
            (git / "refs" / "heads" / "main").write_text("d" * 40 + "\n")
            self.assertEqual(updater.read_version(Path(t)), "ddddddd")
            (git / "HEAD").write_text("e" * 40 + "\n")
            self.assertEqual(updater.read_version(Path(t)), "eeeeeee")
        self.assertEqual(updater.read_version(Path("/nonexistent-xyz")), "")


class InstallUpdaterTest(Base):
    def test_registers_on_demand_task_and_saves_button(self):
        result = sw.step_updater(self.wizard(Script()))
        self.assertEqual(result.status, "ok")
        ps = self.sys.ran[0]
        self.assertEqual(ps[0], "powershell.exe")
        self.assertIn("Register-ScheduledTask", ps[-1])
        self.assertIn("LaptopHubUpdate", ps[-1])
        self.assertIn("update --restart", ps[-1])
        self.assertNotIn("-Trigger", ps[-1])
        button = cfg.load(self.path).scripts["update"]
        self.assertEqual(button, {"label": "Update REDLINE", "argv": ["schtasks", "/Run", "/TN", "LaptopHubUpdate"]})
        self.assertIn("Restart the hub", result.message)

    def test_non_default_config_is_passed_through(self):
        sw.step_updater(self.wizard(Script()))
        self.assertIn(f'--config "{self.path}" update --restart', self.sys.ran[0][-1])

    def test_failure_saves_no_button(self):
        self.sys.run_rc = 1
        self.assertEqual(sw.step_updater(self.wizard(Script())).status, "fail")
        self.assertNotIn("update", cfg.load(self.path).scripts)

    def test_non_windows_skips(self):
        self.sys.platform = "linux"
        self.assertEqual(sw.step_updater(self.wizard(Script())).status, "skip")
        self.assertEqual(self.sys.ran, [])

    def test_check_and_autostart_selection(self):
        self.assertEqual(sw.check_updater(self.wizard(Script())).status, "warn")
        sw.step_updater(self.wizard(Script()))
        self.assertEqual(sw.check_updater(self.wizard(Script())).status, "ok")
        self.assertEqual(sw.parse_only("autostart"), ["autostart", "updater"])
        self.assertIn("updater", sw.STEP_IDS)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.log = self.root / "update.log"
        deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        self.client = TestClient(create_app(self.config, deps, audit_path=self.root / "audit.log", update_log=self.log),
                                 base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(self.client.close)

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)

    def test_update_log_needs_login(self):
        self.assertEqual(self.client.get("/api/update-log").status_code, 401)

    def test_update_log_tail_is_limited(self):
        self.login()
        self.assertEqual(self.client.get("/api/update-log").json()["lines"], [])
        self.log.write_text("\n".join(f"line {i} " + "x" * 300 for i in range(50)) + "\n")
        data = self.client.get("/api/update-log").json()
        self.assertEqual(len(data["lines"]), 20)
        self.assertTrue(data["lines"][-1].startswith("line 49"))
        self.assertTrue(all(len(ln) <= 200 for ln in data["lines"]))
        self.assertLessEqual(len(data["last_update"]), 200)

    def test_status_has_version(self):
        self.login()
        status = self.client.get("/api/status").json()
        self.assertIn("version", status)
        self.assertIsInstance(status["version"], str)


if __name__ == "__main__":
    unittest.main()
