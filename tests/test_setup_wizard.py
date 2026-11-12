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
GEMINI = "AIzaSECRETGEMINIKEY1234567890"
SECRETS = (PASSWORD, ASANA, ICS, HOOK, CLAUDE, GEMINI)


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
        self.posts: list[tuple[str, dict]] = []
        self.post_answers: dict[str, tuple[int, bytes]] = {}
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

    def http_post_json(self, url, body, headers=None, timeout=20):
        if self.http_error: raise sw.CheckFailed(self.http_error)
        self.posts.append((url, body))
        for name, answer in self.post_answers.items():
            if f"/{name}:" in url:
                return answer
        return 200, b"{}"

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

    def test_ai_step_detects_cli_saves_gemini_key_and_default(self):
        self.sys.which = lambda n: "C:/bin/claude.cmd" if n == "claude" else None
        self.sys.http[sw.GEMINI_CHECK_URL] = (200, b"{}")
        result = sw.step_ai(self.wizard(Script(asks=["gemini"], secrets_=[GEMINI], confirms=[True])))
        self.assertEqual(result.status, "ok")
        self.assertIn(["C:/bin/claude.cmd", "--version"], self.sys.ran)
        self.assertEqual(self.sys.keyring[("jarvis-gemini", "api_key")], GEMINI)
        self.assertEqual(self.sys.user_env, {})   # no Anthropic key is asked for or stored
        self.assertEqual(cfg.load(self.path).default_provider, "gemini")
        self.assertIn("claude", self.output())
        self.assertNoSecrets()

    def test_ai_step_without_cli_tells_how_to_log_in_and_skips_gemini(self):
        result = sw.step_ai(self.wizard(Script(confirms=[False])))
        self.assertEqual(result.status, "ok")
        self.assertIn("run `claude` once", self.output())
        self.assertEqual(self.sys.keyring, {})
        self.assertEqual(cfg.load(self.path).default_provider, "auto")

    def test_gemini_bad_key_is_a_warning_and_saves_nothing(self):
        self.sys.http[sw.GEMINI_CHECK_URL] = (400, b"")
        result = sw.step_gemini(self.wizard(Script(secrets_=[GEMINI])))
        self.assertEqual(result.status, "warn")
        self.assertEqual(self.sys.keyring, {})
        self.assertNoSecrets()

    def test_gemini_network_down_can_still_save(self):
        self.sys.http_error = "network"
        result = sw.step_gemini(self.wizard(Script(secrets_=[GEMINI], confirms=[True])))
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.keyring[("jarvis-gemini", "api_key")], GEMINI)

    def test_gemini_probe_is_opt_in(self):
        self.sys.http[sw.GEMINI_CHECK_URL] = (200, b"{}")
        sw.step_gemini(self.wizard(Script(secrets_=[GEMINI])))
        self.assertEqual(self.sys.posts, [])

    def test_gemini_probe_ok_sends_one_token(self):
        self.sys.http[sw.GEMINI_CHECK_URL] = (200, b"{}")
        result = sw.step_gemini(self.wizard(Script(secrets_=[GEMINI], confirms=[True])))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.sys.posts), 1)
        self.assertEqual(self.sys.posts[0][1]["generationConfig"]["maxOutputTokens"], 1)
        self.assertIn("has quota", self.output())
        self.assertNoSecrets()

    def test_gemini_probe_no_quota_prints_the_options(self):
        from hub import llm
        llm.reset_gemini_state()
        self.addCleanup(llm.reset_gemini_state)
        self.sys.http[sw.GEMINI_CHECK_URL] = (200, b"{}")
        self.sys.post_answers["gemini-2.5-flash"] = (429, b"quota exceeded ... limit: 0, model: gemini-2.5-flash")
        result = sw.step_gemini(self.wizard(Script(secrets_=[GEMINI], confirms=[True])))
        self.assertEqual(result.status, "warn")
        self.assertIn("does not include API quota", self.output())
        self.assertIn("limit 0", llm.gemini_quota_problem(GEMINI))
        self.assertEqual(self.sys.keyring[("jarvis-gemini", "api_key")], GEMINI)   # still saved

    def test_gemini_probe_not_found(self):
        self.sys.http[sw.GEMINI_CHECK_URL] = (200, b"{}")
        self.sys.post_answers["gemini-2.5-flash"] = (404, b"not found")
        result = sw.step_gemini(self.wizard(Script(secrets_=[GEMINI], confirms=[True])))
        self.assertEqual(result.status, "ok")
        self.assertIn("doesn't know the model", self.output())

    def test_claude_is_an_alias_of_ai_and_gemini_runs_alone(self):
        self.assertEqual(sw.parse_only("claude"), ["ai"])
        self.assertEqual(sw.parse_only("gemini"), ["gemini"])
        self.assertNotIn("gemini", sw.parse_only(None))

    def test_ai_check_reports_ready_providers(self):
        self.sys.keyring[("jarvis-gemini", "api_key")] = GEMINI
        result = sw.check_ai(self.wizard(Script()))
        self.assertEqual(result.status, "ok")
        self.assertIn("Gemini key", result.message)

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


class TestCouncil(Base):
    def lab_system(self, target):
        """A fake machine where `git clone` of the lab puts a lab/ folder and requirements.txt there."""
        orig = self.sys.run

        def run(argv, cwd=None, timeout=900, on_line=None):
            rc, out = orig(argv, cwd, timeout, on_line)
            if argv[:2] == ["git", "clone"] and rc == 0:
                self.sys.dirs.add(str(Path(argv[3]) / "lab"))
                self.sys.files[str(Path(argv[3]) / "requirements.txt")] = ""
            return rc, out
        self.sys.run = run

    def test_clone_and_venv_only_after_confirm_and_dir_is_saved(self):
        target = str(self.home / "multi-llm-lab")
        self.lab_system(target)
        w = self.wizard(Script(asks=[target], confirms=[True, True]))
        result = sw.step_council(w)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(self.sys.ran[0], ["git", "clone", sw.COUNCIL_CLONE_URL, str(Path(target))])
        self.assertEqual(self.sys.ran[1][1:3], ["-m", "venv"])
        self.assertEqual(self.sys.ran[2][1:5], ["-m", "pip", "install", "-r"])
        self.assertEqual(cfg.load(self.path).council_dir, str(Path(target)))
        self.assertIn("venv ready", result.message)

    def test_declining_the_clone_changes_nothing(self):
        w = self.wizard(Script(asks=[str(self.home / "lab")], confirms=[False]))
        result = sw.step_council(w)
        self.assertEqual(result.status, "skip")
        self.assertEqual(self.sys.ran, [])
        self.assertEqual(cfg.load(self.path).council_dir, "")

    def test_declining_the_venv_still_saves_the_folder(self):
        target = str(self.home / "multi-llm-lab")
        self.lab_system(target)
        result = sw.step_council(self.wizard(Script(asks=[target], confirms=[True, False])))
        self.assertEqual(result.status, "ok")
        self.assertEqual([a[0] for a in self.sys.ran], ["git"])
        self.assertEqual(cfg.load(self.path).council_dir, str(Path(target)))

    def test_existing_lab_folder_is_never_touched_and_unrelated_folder_is_refused(self):
        target = str(self.home / "mine")
        self.sys.dirs.add(target)
        self.sys.dirs.add(str(Path(target) / "lab"))
        self.sys.files[str(Path(target) / "lab" / "x.py")] = ""
        self.sys.existing.add(str(Path(target) / ".venv"))
        result = sw.step_council(self.wizard(Script(asks=[target])))
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.sys.ran, [])
        other = str(self.home / "other")
        self.sys.dirs.add(other)
        self.sys.files[str(Path(other) / "readme.txt")] = ""
        result = sw.step_council(self.wizard(Script(asks=[other])))
        self.assertEqual(result.status, "fail")
        self.assertEqual(cfg.load(self.path).council_dir, str(Path(target)))

    def test_clone_failure_and_missing_git(self):
        self.sys.run_rc = 1
        result = sw.step_council(self.wizard(Script(asks=[str(self.home / "a")], confirms=[True])))
        self.assertEqual(result.status, "fail")
        self.sys.which = lambda n: None
        result = sw.step_council(self.wizard(Script(asks=[str(self.home / "b")], confirms=[True])))
        self.assertIn("git", result.message)

    def test_check_reports_each_state_and_changes_nothing(self):
        w = self.wizard(Script())
        w.load_config(True)
        self.assertEqual(sw.check_council(w).status, "warn")
        w.config.council_dir = str(self.home / "gone")
        self.assertEqual(sw.check_council(w).status, "fail")
        self.sys.dirs.add(str(self.home / "gone" / "lab"))
        self.assertEqual(sw.check_council(w).status, "ok")
        self.assertEqual(self.sys.ran, [])

    def test_step_is_in_the_run_order_and_has_aliases(self):
        self.assertIn("council", sw.STEP_IDS)
        self.assertEqual(sw.parse_only("lab,multi-llm-lab"), ["council"])
        self.assertLess(sw.STEP_IDS.index("ai"), sw.STEP_IDS.index("council"))
        w = self.wizard(Script())
        code = sw.run_wizard(w, "council", check=True)
        self.assertIn("Council (multi-llm-lab)", self.output())
        self.assertEqual(code, 0)


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
        script = Script(asks=["jv_not_used"], secrets_=[PASSWORD, ASANA, ICS, GEMINI, HOOK],
                        confirms=[True, True, True, True, True])
        script.asks = ["me@gmail.com", "auto", "Chief"]   # gmail address, display name
        old_out, old_err = io.StringIO(), io.StringIO()
        with redirect_stdout(old_out), redirect_stderr(old_err):
            code, w = self.run_all("gmail,asana,calendar,ai,phone,name", script=script)
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


class Boom(Script):
    """Raises KeyboardInterrupt (or EOFError) at the n-th ask."""
    def __init__(self, exc=KeyboardInterrupt, **kw):
        super().__init__(**kw)
        self.exc = exc

    def ask(self, prompt, default=""):
        if not self.asks:
            raise self.exc()
        return self.asks.pop(0)


class TestUx(Base):
    def test_confirm_reasks_then_accepts(self):
        said, answers = [], iter(["Shikhar", "maybe", "YES"])
        self.assertTrue(sw._real_confirm("Go?", True, reader=lambda p: next(answers), say=said.append))
        self.assertEqual(said, ["Please answer y or n (Enter = yes)"] * 2)

    def test_confirm_enter_uses_default_and_three_bad_means_no(self):
        self.assertFalse(sw._real_confirm("Go?", False, reader=lambda p: "", say=lambda t: None))
        said = []
        self.assertFalse(sw._real_confirm("Go?", True, reader=lambda p: "C:\\x", say=said.append))
        self.assertIn("treating that as no", said[-1])

    def test_path_rejects_lone_y_and_cleans_quotes(self):
        target = str(self.home / "jv")
        w = self.wizard(Script(asks=["y", f'  "{target}" '], confirms=[True]))
        self.assertEqual(sw.step_jarvis(w).status, "ok")
        self.assertIn("That looks like an answer, not a folder", self.output())
        self.assertIn(f"Folder: {target}", self.output())
        self.assertEqual(self.sys.ran[0][3], target)

    def test_path_three_bad_answers_skips(self):
        w = self.wizard(Script(asks=["y", "n", "yes"]))
        self.assertEqual(sw.step_jarvis(w).status, "skip")
        self.assertEqual(self.sys.ran, [])

    def test_name_is_asked_directly_and_sanitized(self):
        w = self.wizard(Script(asks=['  <b>Shikhar</b> "the great and mighty"  ']))
        sw.step_name(w)
        saved = cfg.load(self.path).display_name
        self.assertLessEqual(len(saved), 20)
        self.assertTrue(saved.startswith("bShikhar"))
        code, w2 = TestRun.run_all(self, "name", script=Script(asks=["Shikhar"], confirms=[False]))
        self.assertEqual(cfg.load(self.path).display_name, "Shikhar")
        self.assertNotIn("Set up Your name?", self.output())

    def test_email_list_numbered_and_picked(self):
        w = self.wizard(Script(asks=["a@onestack.in , b@gmail.com", "2"], secrets_=[PASSWORD]))
        self.assertEqual(sw.step_gmail(w).status, "ok")
        self.assertEqual(self.sys.keyring[(sw.GMAIL_SERVICE, "address")], "b@gmail.com")
        out = self.output()
        self.assertIn("1. a@onestack.in", out)
        self.assertIn("ONE mailbox", out)
        self.assertIn("--only gmail", out)
        self.assertIn("App passwords or IMAP turned off", out)

    def test_email_bad_pick_skips_on_enter(self):
        w = self.wizard(Script(asks=["a@x.com; b@y.com", "9", ""]))
        self.assertEqual(sw.step_gmail(w).status, "skip")
        self.assertEqual(self.sys.keyring, {})

    def test_auth_failure_prints_workspace_explanation(self):
        self.sys.imap_error = "auth"
        w = self.wizard(Script(asks=["me@onestack.in"], secrets_=[PASSWORD]))
        self.assertEqual(sw.step_gmail(w).status, "fail")
        self.assertIn("Why this can happen", self.output())

    def test_calendar_404_explained_without_url(self):
        w = self.wizard(Script(secrets_=[ICS]))
        result = sw.step_calendar(w)
        self.assertEqual(result.status, "fail")
        self.assertIn("Google answered 404", result.message)
        self.assertIn("Friday still works", result.message)
        self.assertNoSecrets()
        self.assertNotIn(ICS, result.message)

    def test_chat_explains_limits_and_names_app_capture(self):
        sw.step_chat(self.wizard(Script()))
        out = self.output()
        self.assertIn("not into 1:1 chats", out)
        self.assertIn("Workspace admin", out)
        self.assertIn("Watch chat", out)

    def test_new_topic_option(self):
        w = self.wizard(Script())
        sw.step_phone(w)
        old = w.config.ntfy_topic
        w = self.wizard(Script(confirms=[True]))
        sw.step_phone(w)
        self.assertNotEqual(cfg.load(self.path).ntfy_topic, old)

    def test_ctrl_c_stops_whole_wizard_cleanly(self):
        script = Boom(asks=[], confirms=[True, False, True])   # jarvis yes -> ask raises
        w = self.wizard(script)
        calls = []
        code = sw.run_wizard(w, "jarvis,gmail,asana", run_doctor=lambda: calls.append(1))
        out = self.output()
        self.assertEqual(code, 130)
        self.assertIn("Stopped. Nothing after this point was changed.", out)
        self.assertNotIn("interrupted", out)
        self.assertEqual(w.results, [])
        self.assertEqual(calls, [])
        self.assertIn("[1/3] Jarvis folder", out)
        self.assertNotIn("[2/3]", out)

    def test_ctrl_c_keeps_completed_steps_in_summary(self):
        script = Boom(asks=[], confirms=[False, True])   # jarvis skipped, gmail starts -> ask raises
        w = self.wizard(script)
        sw.run_wizard(w, "jarvis,gmail,asana")
        self.assertEqual([s for s, _ in w.results], ["jarvis"])
        self.assertIn("Jarvis folder", self.output().split("== Summary")[1])

    def test_eof_is_like_ctrl_c(self):
        w = self.wizard(Boom(exc=EOFError, confirms=[True]))
        self.assertEqual(sw.run_wizard(w, "jarvis,gmail"), 130)

    def test_doctor_crash_does_not_crash_wizard(self):
        def boom():
            raise KeyboardInterrupt
        w = self.wizard(Script(confirms=[False]))
        self.assertEqual(sw.run_wizard(w, "asana", run_doctor=boom), 0)
        self.assertIn("Doctor skipped", self.output())

    def test_header_and_help_line(self):
        w = self.wizard(Script(confirms=[False]))
        sw.run_wizard(w, "asana")
        out = self.output()
        self.assertIn("REDLINE setup", out)
        self.assertIn("Re-run any time: python -m hub setup --only gmail,calendar", out)
        self.assertIn("[1/1] Asana", out)
        self.assertNotIn("Laptop Hub", out)


class TestDoctor(unittest.TestCase):
    def test_ollama_exception_prints_not_reachable(self):
        from unittest import mock
        from hub import __main__ as m
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hub.json"
            c, _ = cfg.make()
            cfg.save(c, p)
            buf = io.StringIO()
            with mock.patch.object(m.urllib.request, "urlopen", side_effect=RuntimeError("slow")), redirect_stdout(buf):
                m.cmd_doctor(SimpleNamespace(config=str(p)))
        self.assertIn("[--] Ollama not reachable", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
