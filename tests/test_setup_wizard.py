"""`python -m hub setup`: all prompts and all machine effects are fakes, so nothing real is touched."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from hub import config as cfg
from hub import setup_wizard as sw

PASSWORD = "abcdefghijklmnop"
ASANA = "1/1200000000000000:ASANASECRETVALUE0123456789abcdef"
ICS = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-SECRETSECRET/basic.ics"
HOOK = sw.WEBHOOK_PREFIX + "AAAA/messages?key=SECRETKEY&token=SECRETTOKEN"
CLAUDE = "sk-ant-api03-SECRETCLAUDEKEY"
SECRETS = (PASSWORD, ASANA, ICS, HOOK, CLAUDE)


class FakeSystem:
    platform = "win32"

    def __init__(self, home: Path):
        self.home_dir = home
        self.keyring: dict[tuple[str, str], str] = {}
        self.files: dict[str, str] = {}
        self.dirs: set[str] = set()
        self.existing: set[str] = set()
        self.ran: list[list[str]] = []
        self.run_rc = 0
        self.imap_error = None
        self.smtp_error = None
        self.http: dict[str, tuple[int, bytes]] = {}
        self.http_error = None
        self.env_vars: dict[str, str] = {}
        self.user_env: dict[str, str] = {}
        self.tty = True

    def isatty(self): return self.tty
    def home(self): return self.home_dir
    def env(self, n): return self.env_vars.get(n, "")
    def username(self): return "tester"
    def python_dir(self): return Path("C:/Py")
    def exists(self, p): return str(p) in self.existing or str(p) in self.dirs or str(p) in self.files
    def isdir(self, p): return str(p) in self.dirs
    def isfile(self, p): return str(p) in self.files
    def listdir(self, p): return [k for k in self.files if k.startswith(str(p))] or ["x"] if str(p) in self.dirs and any(k.startswith(str(p)) for k in self.files) else []
    def makedirs(self, p): self.dirs.add(str(p))
    def which(self, n): return "git" if n == "git" else None
    def read_text(self, p): return self.files[str(p)]

    def run(self, argv, cwd=None, timeout=900, on_line=None):
        self.ran.append(list(argv))
        if argv[:2] == ["git", "clone"] and self.run_rc == 0:
            self.dirs.add(argv[3])
            self.files[str(Path(argv[3]) / "jarvis.py")] = ""
        if on_line:
            on_line("progress")
        return self.run_rc, ""

    def keyring_get(self, s, k): return self.keyring.get((s, k))
    def keyring_set(self, s, k, v): self.keyring[(s, k)] = v

    def imap_check(self, a, p):
        if self.imap_error: raise sw.CheckFailed(self.imap_error)

    def smtp_check(self, a, p):
        if self.smtp_error: raise sw.CheckFailed(self.smtp_error)

    def http_get(self, url, headers=None, follow_redirects=True, timeout=10):
        if self.http_error: raise sw.CheckFailed(self.http_error)
        return self.http.get(url, (404, b""))

    def count_events_today(self, ics): return 3

    def write_private_file(self, path, text): self.files[str(path)] = text

    def set_user_env(self, n, v):
        self.user_env[n] = v
        return True


class Script:
    """Scripted answers: ask/secret/confirm pop from queues; a missing answer means Enter."""
    def __init__(self, asks=(), secrets_=(), confirms=()):
        self.asks, self.secs, self.confs = list(asks), list(secrets_), list(confirms)

    def ask(self, prompt, default=""): return self.asks.pop(0) if self.asks else ""
    def secret(self, prompt): return self.secs.pop(0) if self.secs else ""
    def confirm(self, prompt, default=True): return self.confs.pop(0) if self.confs else default


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / "hub.json"
        self.sys = FakeSystem(self.home)
        self.lines: list[str] = []

    def wizard(self, script: Script) -> sw.Wizard:
        return sw.Wizard(self.path, self.sys, ask=script.ask, ask_secret=script.secret, confirm=script.confirm,
                         out=self.lines.append, color=False)

    def output(self): return "\n".join(self.lines)

    def assertNoSecrets(self):
        blob = self.output() + (self.path.read_text() if self.path.exists() else "")
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
            self.assertNotIn(secret.split("?")[0] if "?" in secret else secret, blob)


class TestGmail(Base):
    def run_gmail(self, **kw):
        w = self.wizard(Script(asks=["me@gmail.com"], secrets_=[PASSWORD], confirms=kw.get("confirms", [])))
        return sw.step_gmail(w), w

    def test_saves_and_reads_back_without_printing(self):
        result, _ = self.run_gmail()
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.keyring[(sw.GMAIL_SERVICE, "address")], "me@gmail.com")
        self.assertEqual(self.sys.keyring[(sw.GMAIL_SERVICE, "me@gmail.com")], PASSWORD)
        self.assertNoSecrets()

    def test_idempotent_rerun_keeps_value(self):
        self.run_gmail()
        w = self.wizard(Script(confirms=[False]))
        self.assertEqual(sw.step_gmail(w).status, "ok")
        self.assertEqual(self.sys.keyring[(sw.GMAIL_SERVICE, "me@gmail.com")], PASSWORD)

    def test_auth_error_saves_nothing(self):
        self.sys.imap_error = "auth"
        result, _ = self.run_gmail()
        self.assertEqual(result.status, "fail")
        self.assertIn("rejected", result.message)
        self.assertEqual(self.sys.keyring, {})
        self.assertNotIn(PASSWORD, result.message)

    def test_network_down_can_decline_save(self):
        self.sys.imap_error = self.sys.smtp_error = "network"
        result, _ = self.run_gmail(confirms=[False])
        self.assertEqual(result.status, "fail")
        self.assertEqual(self.sys.keyring, {})

    def test_wrong_length_rejected(self):
        w = self.wizard(Script(asks=["me@gmail.com"], secrets_=["short"]))
        self.assertEqual(sw.step_gmail(w).status, "fail")
        self.assertEqual(self.sys.keyring, {})

    def test_enter_skips(self):
        self.assertEqual(sw.step_gmail(self.wizard(Script())).status, "skip")

    def test_check_mode_tests_saved_login(self):
        self.run_gmail()
        self.assertEqual(sw.check_gmail(self.wizard(Script())).status, "ok")
        self.sys.smtp_error = "auth"
        self.assertEqual(sw.check_gmail(self.wizard(Script())).status, "fail")


class TestOtherSteps(Base):
    def test_asana_writes_file_not_config_and_prints_only_name(self):
        self.sys.http["https://app.asana.com/api/1.0/users/me"] = (200, json.dumps({"data": {"name": "Shikhar"}}).encode())
        w = self.wizard(Script(secrets_=[ASANA]))
        result = sw.step_asana(w)
        self.assertEqual(result.status, "ok")
        self.assertIn("Shikhar", result.message)
        self.assertEqual(self.sys.files[str(self.home / ".claude/.asana_token")].strip(), ASANA)
        self.assertNoSecrets()

    def test_asana_rejected_writes_nothing(self):
        self.sys.http["https://app.asana.com/api/1.0/users/me"] = (401, b"")
        result = sw.step_asana(self.wizard(Script(secrets_=[ASANA])))
        self.assertEqual(result.status, "fail")
        self.assertEqual(self.sys.files, {})

    def test_asana_network_down(self):
        self.sys.http_error = "network"
        self.assertEqual(sw.step_asana(self.wizard(Script(secrets_=[ASANA]))).status, "fail")

    def test_calendar_counts_and_stores_in_keyring(self):
        self.sys.http[ICS] = (200, b"BEGIN:VCALENDAR")
        result = sw.step_calendar(self.wizard(Script(secrets_=[ICS])))
        self.assertEqual(result.status, "ok")
        self.assertIn("3 events today", result.message)
        self.assertEqual(self.sys.keyring[(sw.CALENDAR_SERVICE, "ics_url")], ICS)
        self.assertNoSecrets()

    def test_chat_uses_gchat_storage_format(self):
        w = self.wizard(Script(asks=["Team", ""], secrets_=[HOOK]))
        self.assertEqual(sw.step_chat(w).status, "ok")
        self.assertEqual(json.loads(self.sys.keyring[(sw.CHAT_SERVICE, "_spaces")]), ["team"])
        self.assertEqual(self.sys.keyring[(sw.CHAT_SERVICE, "team")], HOOK)
        self.assertNoSecrets()

    def test_chat_rejects_foreign_url(self):
        w = self.wizard(Script(asks=["team", ""], secrets_=["https://evil.example/x"]))
        sw.step_chat(w)
        self.assertEqual(self.sys.keyring, {})

    def test_claude_key_goes_to_user_env_only(self):
        result = sw.step_claude(self.wizard(Script(secrets_=[CLAUDE])))
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.user_env["ANTHROPIC_API_KEY"], CLAUDE)
        self.assertEqual(self.sys.ran, [])   # never on a command line
        self.assertNoSecrets()

    def test_phone_topic_stable_and_name(self):
        w = self.wizard(Script(asks=["Shikhar"]))
        sw.step_phone(w)
        topic = w.config.ntfy_topic
        self.assertEqual(len(topic), 24)
        sw.step_phone(self.wizard(Script()))          # re-run keeps it
        self.assertEqual(cfg.load(self.path).ntfy_topic, topic)
        self.assertEqual(cfg.load(self.path).ntfy_server, "https://ntfy.sh")
        w2 = self.wizard(Script(asks=["Shikhar"]))
        sw.step_name(w2)
        self.assertEqual(cfg.load(self.path).display_name, "Shikhar")

    def test_warp_asks_first(self):
        self.sys.existing.add(sw.WARP_PATH)
        w = self.wizard(Script(confirms=[False]))
        self.assertEqual(sw.step_warp(w).status, "skip")
        self.assertEqual(cfg.load(self.path).scripts, {})
        w = self.wizard(Script(confirms=[True]))
        sw.step_warp(w)
        self.assertEqual(cfg.load(self.path).scripts["warp_connect"]["argv"], [sw.WARP_PATH, "connect"])

    def test_autostart_uses_argv_list_and_asks_powercfg(self):
        result = sw.step_autostart(self.wizard(Script(confirms=[True, True])))
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.ran[1][0], "powershell.exe")
        self.assertIn("Register-ScheduledTask", self.sys.ran[1][-1])
        self.assertIn("-Force", self.sys.ran[1][-1])
        self.assertEqual(self.sys.ran[-1], ["powercfg", "/change", "standby-timeout-ac", "0"])


class TestJarvis(Base):
    def test_clone_when_missing(self):
        target = str(self.home / "jv")
        w = self.wizard(Script(asks=[target], confirms=[True]))
        result = sw.step_jarvis(w)
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.ran[0], ["git", "clone", sw.CLONE_URL, target])
        self.assertEqual(cfg.load(self.path).jarvis_dir, target)

    def test_existing_folder_is_never_cloned_over(self):
        target = str(self.home / "jv")
        self.sys.dirs.add(target)
        self.sys.files[str(Path(target) / "jarvis.py")] = ""
        result = sw.step_jarvis(self.wizard(Script(asks=[target])))
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.ran, [])

    def test_unrelated_nonempty_folder_refused(self):
        target = str(self.home / "other")
        self.sys.dirs.add(target)
        self.sys.files[str(Path(target) / "notes.txt")] = ""
        result = sw.step_jarvis(self.wizard(Script(asks=[target])))
        self.assertEqual(result.status, "fail")
        self.assertEqual(self.sys.ran, [])

    def test_clone_failure_is_reported(self):
        self.sys.run_rc = 1
        result = sw.step_jarvis(self.wizard(Script(asks=[str(self.home / "jv")], confirms=[True])))
        self.assertEqual(result.status, "fail")


class TestRun(Base):
    def run_all(self, only=None, check=False, script=None):
        w = self.wizard(script or Script())
        return sw.run_wizard(w, only, check), w

    def test_only_selection(self):
        code, w = self.run_all("gmail, asana", script=Script(confirms=[False, False]))
        self.assertEqual([s for s, _ in w.results], ["gmail", "asana"])
        self.assertEqual(code, 0)

    def test_unknown_step(self):
        code, _ = self.run_all("nope")
        self.assertEqual(code, 2)

    def test_check_changes_nothing_and_prompts_nothing(self):
        code, w = self.run_all(check=True, script=Script(confirms=[True] * 50))
        self.assertEqual(self.sys.keyring, {})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.sys.ran, [r for r in self.sys.ran if r[0] == "schtasks"])

    def test_refuses_without_tty(self):
        self.sys.tty = False
        w = self.wizard(Script())
        code = sw.run_setup(SimpleNamespace(config=str(self.path), only=None, check=False), w)
        self.assertEqual(code, 2)
        self.assertFalse(self.path.exists())

    def test_full_run_leaks_nothing_anywhere(self):
        self.sys.http["https://app.asana.com/api/1.0/users/me"] = (200, b'{"data":{"name":"Me"}}')
        self.sys.http[ICS] = (200, b"x")
        script = Script(asks=["jv_not_used"], secrets_=[PASSWORD, ASANA, ICS, CLAUDE, HOOK],
                        confirms=[False, True, True, True, False, False, True, True, True, False, False])
        script.asks = ["me@gmail.com", "", "Chief"]   # gmail address, chat names end, display name
        old_out, old_err = io.StringIO(), io.StringIO()
        with redirect_stdout(old_out), redirect_stderr(old_err):
            code, w = self.run_all("gmail,asana,calendar,claude,phone,name", script=script)
        captured = old_out.getvalue() + old_err.getvalue()
        for secret in SECRETS:
            self.assertNotIn(secret, captured)
        self.assertNoSecrets()
        for p in self.home.rglob("*"):
            if p.is_file():
                for secret in SECRETS:
                    self.assertNotIn(secret, p.read_text(errors="ignore"))
        self.assertEqual(sw.scrub("key sk-ant-abc and https://x/y?t=1 and " + "A" * 30), "key [hidden] and [hidden] and [hidden]")

    def test_echo_guard_masks_a_secret_that_slips_into_output(self):
        w = self.wizard(Script(secrets_=[PASSWORD]))
        w.secret("x")
        w.say("oops " + PASSWORD)
        self.assertNotIn(PASSWORD, self.output())


if __name__ == "__main__":
    unittest.main()
