"""Tests for the LAPTOP module: the action registry. The operating system is faked, so nothing here
locks, sleeps, restarts, presses a key or touches the clipboard for real."""
import base64
import io
import json
import os
import subprocess
import tempfile
import logging
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import laptop
from hub.common import Ctx
from tests.helpers import make_config

try:
    from PIL import Image
except ImportError:          # pragma: no cover
    Image = None

logging.getLogger("hub.laptop").addHandler(logging.NullHandler())   # expected failures stay quiet

SECRET = "hunter2 $(calc) \"quoted\" 'single'\nsecond line"


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class FakeSystem(laptop.System):
    """Records what the actions ask the operating system to do, and does none of it."""

    def __init__(self, platform="win32", tools=()):
        self.platform = platform
        self.tools = set(tools)
        self.calls: list[tuple] = []
        self.run_result = (0, "", "")
        self.run_error: Exception | None = None
        self.pillow = True
        self.display = True
        self.screen_size = (3200, 1800)
        self.grab_error: Exception | None = None
        self.url_opens = True

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def has_pillow(self):
        return self.pillow

    def has_display(self):
        return self.display

    def run(self, argv, input_text=None, timeout=20, cwd=None):
        self.calls.append(("run", list(argv), input_text, timeout, cwd))
        if self.run_error:
            raise self.run_error
        return self.run_result

    def launch(self, argv):
        self.calls.append(("launch", list(argv)))

    def open_default(self, path):
        self.calls.append(("open_default", path))

    def open_url(self, url):
        self.calls.append(("open_url", url))
        return self.url_opens

    def start_app(self, target, args):
        self.calls.append(("start_app", target, list(args)))

    def grab_screen(self):
        self.calls.append(("grab",))
        if self.grab_error:
            raise self.grab_error
        return Image.new("RGB", self.screen_size, (10, 120, 200))

    def press_media_key(self, name, times=1):
        self.calls.append(("key", name, times))

    def terminate_process(self, pid):
        self.calls.append(("terminate", pid))
        return "terminated"

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class LaptopCase(unittest.TestCase):
    def setUp(self):
        self.audits = []
        self.system = FakeSystem()
        self.tmp = Path(tempfile.mkdtemp(prefix="laptop-test-"))

    def build(self, system=None, authed=True, **config):
        config.setdefault("apps", {"Chrome": {"target": "chrome.exe", "args": ["--new-window"]},
                                   "Notepad": {"target": "notepad.exe", "args": []}})
        cfg, _ = make_config(**config)
        ctx = Ctx(config=cfg, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda event, **f: self.audits.append((event, f)))
        app = FastAPI()
        app.include_router(laptop.make_router(ctx, system=system or self.system))
        client = TestClient(app)
        if authed:
            client.cookies.set("hub_session", "yes")
        self.addCleanup(client.close)
        return client

    def run_action(self, client, action, params=None, confirm=None):
        body = {"action": action, "params": params or {}}
        if confirm is not None:
            body["confirm"] = confirm
        return client.post("/api/laptop/run", json=body)

    def listing(self, client):
        return {a["id"]: a for a in client.get("/api/laptop/actions").json()["actions"]}


class RegistryTests(LaptopCase):
    def test_login_is_required(self):
        client = self.build(authed=False)
        self.assertEqual(client.get("/api/laptop/actions").status_code, 401)
        self.assertEqual(self.run_action(client, "uptime").status_code, 401)
        self.assertEqual(self.audits, [])

    def test_listing_has_every_builtin_with_the_documented_shape(self):
        data = self.build().get("/api/laptop/actions").json()
        actions = {a["id"]: a for a in data["actions"]}
        expected = ["system_info", "battery", "disk_usage", "network_info", "uptime", "list_processes",
                    "kill_process", "open_url", "open_path", "screenshot", "lock_screen", "sleep", "restart",
                    "shutdown", "cancel_shutdown", "get_clipboard", "set_clipboard", "volume_up",
                    "volume_down", "mute", "play_pause", "next_track", "previous_track"]
        for name in expected:
            self.assertIn(name, actions)
        for a in actions.values():
            self.assertEqual(set(a) - {"unavailable_reason"},
                             {"id", "title", "category", "description", "risk", "params", "available"})
            self.assertIn(a["risk"], ("safe", "confirm"))
            for p in a["params"]:
                self.assertIn(p["type"], ("text", "number", "choice", "bool"))
                self.assertIn("required", p)
        category_ids = {c["id"] for c in data["categories"]}
        self.assertTrue({a["category"] for a in actions.values()} <= category_ids)
        for name in ("kill_process", "sleep", "restart", "shutdown"):
            self.assertEqual(actions[name]["risk"], "confirm", name)
        for name in ("cancel_shutdown", "lock_screen", "set_clipboard", "screenshot"):
            self.assertEqual(actions[name]["risk"], "safe", name)

    def test_apps_and_scripts_from_the_config_are_listed(self):
        client = self.build(scripts={"build": {"label": "Build it", "argv": ["make"], "cwd": "/tmp"},
                                     "broken": {"label": "No argv"}})
        actions = self.listing(client)
        chrome = actions["open_app:Chrome"]
        self.assertEqual((chrome["category"], chrome["params"], chrome["risk"]), ("apps", [], "safe"))
        self.assertIn("open_app:Notepad", actions)
        build = actions["script:build"]
        self.assertEqual((build["category"], build["title"], build["params"]), ("buttons", "Build it", []))
        self.assertNotIn("script:broken", actions)         # no argv list, so skipped, nothing crashes
        cats = [c["id"] for c in client.get("/api/laptop/actions").json()["categories"]]
        self.assertIn("apps", cats)
        self.assertIn("buttons", cats)
        self.assertNotIn("open_app", actions)              # the by-name alias is not listed

    def test_no_apps_or_scripts_means_no_such_categories(self):
        client = self.build(apps={}, scripts={})
        cats = [c["id"] for c in client.get("/api/laptop/actions").json()["categories"]]
        self.assertNotIn("apps", cats)
        self.assertNotIn("buttons", cats)

    def test_unknown_action_is_404(self):
        self.assertEqual(self.run_action(self.build(), "format_c").status_code, 404)
        self.assertEqual(self.run_action(self.build(), "open_app:Nope").status_code, 404)
        self.assertEqual(self.audits, [])

    def test_adding_an_action_is_one_registry_entry(self):
        async def hello(params):
            return laptop.text("hi " + params["who"])
        original = laptop.builtin_actions
        laptop.builtin_actions = lambda system: original(system) + [
            laptop.Action("hello", "Hello", "system", "Says hi.", hello,
                          params=[laptop.Param("who", "Who")])]
        self.addCleanup(setattr, laptop, "builtin_actions", original)
        client = self.build()
        self.assertIn("hello", self.listing(client))
        self.assertEqual(self.run_action(client, "hello", {"who": "Sam"}).json()["text"], "hi Sam")


class ValidationTests(LaptopCase):
    def test_params_are_checked_with_plain_sentences(self):
        client = self.build()
        cases = [
            ("kill_process", {}, "required"),
            ("kill_process", {"pid": "abc"}, "number"),
            ("kill_process", {"pid": 1.5}, "whole"),
            ("kill_process", {"pid": True}, "number"),
            ("kill_process", {"pid": 0}, "at least"),
            ("kill_process", {"pid": 99, "extra": 1}, "not an option"),
            ("open_url", {}, "required"),
            ("open_url", {"url": 5}, "text"),
            ("open_url", {"url": ""}, "required"),
            ("open_url", {"url": "https://a.example/" + "x" * 3000}, "too long"),
            ("volume_up", {"steps": 99}, "at most"),
            ("volume_up", {"steps": "lots"}, "number"),
            ("uptime", {"anything": 1}, "not an option"),
        ]
        for action, params, word in cases:
            r = self.run_action(client, action, params, confirm=True)
            self.assertEqual(r.status_code, 400, (action, params, r.text))
            self.assertIn(word, r.json()["detail"], (action, params))
            self.assertTrue(r.json()["detail"].endswith("."))
        self.assertEqual(self.system.of("terminate"), [])
        self.assertEqual(self.audits, [])                  # nothing ran, nothing audited

    def test_numbers_may_arrive_as_strings(self):
        r = self.run_action(self.build(), "kill_process", {"pid": "4242"}, confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.system.of("terminate"), [("terminate", 4242)])

    def test_choice_and_bool_params(self):
        params = [laptop.Param("mode", "Mode", "choice", choices=["a", "b"]),
                  laptop.Param("flag", "Flag", "bool", required=False)]
        self.assertEqual(laptop.validate(params, {"mode": "a", "flag": "yes"}), {"mode": "a", "flag": True})
        self.assertEqual(laptop.validate(params, {"mode": "b", "flag": False}), {"mode": "b", "flag": False})
        with self.assertRaises(laptop.BadParams):
            laptop.validate(params, {"mode": "c"})
        with self.assertRaises(laptop.BadParams):
            laptop.validate(params, {"mode": "a", "flag": "maybe"})

    def test_a_malformed_request_body_is_rejected(self):
        client = self.build()
        self.assertEqual(client.post("/api/laptop/run", json={"params": {}}).status_code, 422)
        self.assertEqual(client.post("/api/laptop/run", json={"action": "uptime", "params": []}).status_code, 422)


class ConfirmAndAvailabilityTests(LaptopCase):
    def test_confirm_rule_is_409_until_confirmed(self):
        client = self.build()
        for action, params in (("kill_process", {"pid": 4242}), ("sleep", {}), ("restart", {}), ("shutdown", {})):
            for confirm in (None, False, "true"):       # only a real JSON true counts
                r = self.run_action(client, action, params, confirm=confirm)
                self.assertIn(r.status_code, (409, 422), (action, confirm))
                if r.status_code == 409:
                    self.assertIn("confirm", r.json()["detail"].lower())
        self.assertEqual(self.system.calls, [])             # nothing was done
        self.assertEqual(self.audits, [])
        r = self.run_action(client, "kill_process", {"pid": 4242}, confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.system.of("terminate"), [("terminate", 4242)])
        self.assertEqual(self.run_action(client, "restart", confirm=True).status_code, 200)
        self.assertEqual(self.system.of("run")[-1][1], ["shutdown", "/r", "/t", "30"])

    def test_the_confirm_flag_on_a_safe_action_is_harmless(self):
        self.assertEqual(self.run_action(self.build(), "lock_screen", confirm=True).status_code, 200)

    def test_power_commands_are_exact_argument_lists(self):
        client = self.build()
        self.run_action(client, "shutdown", confirm=True)
        self.run_action(client, "restart", confirm=True)
        self.run_action(client, "cancel_shutdown")
        self.run_action(client, "lock_screen")
        self.run_action(client, "sleep", confirm=True)
        self.assertEqual([c[1] for c in self.system.of("run")], [
            ["shutdown", "/s", "/t", "30"], ["shutdown", "/r", "/t", "30"], ["shutdown", "/a"],
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]])

    def test_unavailable_actions_are_501_with_the_reason_and_run_nothing(self):
        system = FakeSystem(platform="linux")
        client = self.build(system)
        actions = self.listing(client)
        for name in ("restart", "shutdown", "cancel_shutdown", "volume_up", "get_clipboard", "set_clipboard"):
            self.assertFalse(actions[name]["available"], name)
            self.assertTrue(actions[name]["unavailable_reason"])
            r = self.run_action(client, name, {"text": "x"} if name == "set_clipboard" else {}, confirm=True)
            self.assertEqual(r.status_code, 501, name)
            self.assertEqual(r.json()["detail"], actions[name]["unavailable_reason"])
        self.assertEqual(system.calls, [])
        self.assertEqual(self.audits, [])

    def test_missing_pillow_or_display_makes_screenshot_unavailable(self):
        self.system.pillow = False
        a = self.listing(self.build())["screenshot"]
        self.assertFalse(a["available"])
        self.assertIn("Pillow", a["unavailable_reason"])
        self.system.pillow, self.system.display = True, False
        self.assertFalse(self.listing(self.build())["screenshot"]["available"])
        self.assertEqual(self.run_action(self.build(), "screenshot").status_code, 501)

    def test_linux_and_mac_fallbacks_when_the_tool_exists(self):
        system = FakeSystem(platform="linux", tools=("loginctl", "playerctl", "xclip"))
        client = self.build(system)
        actions = self.listing(client)
        self.assertTrue(actions["lock_screen"]["available"])
        self.assertTrue(actions["play_pause"]["available"])
        self.assertFalse(actions["volume_up"]["available"])
        self.assertTrue(actions["get_clipboard"]["available"])
        self.assertEqual(self.run_action(client, "play_pause").status_code, 200)
        self.assertEqual(system.of("run")[-1][1], ["playerctl", "play-pause"])
        mac = FakeSystem(platform="darwin", tools=("pmset", "pbcopy", "pbpaste"))
        self.assertTrue(self.listing(self.build(mac))["sleep"]["available"])
        self.run_action(self.build(mac), "set_clipboard", {"text": "hi"})
        self.assertEqual(mac.of("run")[-1][1:3], (["pbcopy"], "hi"))
        no_tool = FakeSystem(platform="linux")
        self.assertFalse(self.listing(self.build(no_tool))["lock_screen"]["available"])

    def test_a_failing_command_is_a_500_with_a_sentence(self):
        client = self.build()
        self.system.run_result = (1, "", "Access is denied.\r\n")
        r = self.run_action(client, "lock_screen")
        self.assertEqual(r.status_code, 500)
        self.assertIn("Access is denied", r.json()["detail"])
        self.system.run_error = RuntimeError("the roof fell in")
        r = self.run_action(client, "lock_screen")
        self.assertEqual(r.status_code, 500)
        self.assertIn("the roof fell in", r.json()["detail"])
        self.assertNotIn("Traceback", r.json()["detail"])
        self.system.run_error = FileNotFoundError("nope")
        self.assertEqual(self.run_action(client, "lock_screen").status_code, 500)
        self.system.run_error = subprocess.TimeoutExpired("x", 20)
        self.assertEqual(self.run_action(client, "lock_screen").status_code, 500)


class AuditTests(LaptopCase):
    def test_every_run_is_audited_with_the_action_and_params(self):
        client = self.build()
        self.run_action(client, "open_url", {"url": "https://example.com/a"})
        self.run_action(client, "kill_process", {"pid": 777}, confirm=True)
        self.run_action(client, "uptime")
        self.assertEqual(self.audits[0], ("laptop_run", {"action": "open_url", "url": "https://example.com/a"}))
        self.assertEqual(self.audits[1], ("laptop_run", {"action": "kill_process", "pid": 777}))
        self.assertEqual(self.audits[2], ("laptop_run", {"action": "uptime"}))

    def test_audit_never_holds_clipboard_text_or_screenshots(self):
        client = self.build()
        self.system.run_result = (0, SECRET, "")
        self.assertEqual(self.run_action(client, "set_clipboard", {"text": SECRET}).status_code, 200)
        self.assertEqual(self.run_action(client, "get_clipboard").status_code, 200)
        shot = self.run_action(client, "screenshot")
        self.assertEqual(shot.status_code, 200)
        dump = json.dumps(self.audits)
        self.assertNotIn("hunter2", dump)
        self.assertNotIn("calc", dump)
        self.assertNotIn(shot.json()["image_b64"][:60], dump)
        self.assertNotIn("image_b64", dump)
        self.assertIn(("laptop_run", {"action": "set_clipboard", "text_chars": len(SECRET)}), self.audits)

    def test_refused_requests_are_not_audited_as_runs(self):
        client = self.build()
        self.run_action(client, "kill_process", {"pid": 5})               # no confirm
        self.run_action(client, "kill_process", {"pid": "abc"}, confirm=True)  # bad params
        self.run_action(client, "nothing_like_it")                            # unknown
        self.assertEqual(self.audits, [])

    def test_a_failure_after_the_audit_is_still_audited(self):
        self.system.run_error = RuntimeError("x")
        self.run_action(self.build(), "lock_screen")
        self.assertEqual(len(self.audits), 1)


class KillTests(LaptopCase):
    def test_refuses_the_hub_itself_and_its_parents(self):
        client = self.build(FakeSystem())
        real = laptop.System()
        self.system.protected_pids = real.protected_pids     # the real parent chain
        protected = real.protected_pids()
        self.assertIn(os.getpid(), protected)
        self.assertIn(os.getppid(), protected)
        for pid in (os.getpid(), os.getppid()):
            r = self.run_action(client, "kill_process", {"pid": pid}, confirm=True)
            self.assertEqual(r.status_code, 400, pid)
            self.assertIn("hub itself", r.json()["detail"])
        for pid in (1, 4):
            self.assertEqual(self.run_action(client, "kill_process", {"pid": pid}, confirm=True).status_code, 400)
        self.assertEqual(self.system.of("terminate"), [])

    def test_a_vanished_process_is_a_clear_400(self):
        class Gone(FakeSystem):
            def terminate_process(self, pid):
                raise laptop.ActionError(f"There is no process with id {pid} (it may have just ended).", 400)
        r = self.run_action(self.build(Gone()), "kill_process", {"pid": 31337}, confirm=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("31337", r.json()["detail"])

    def test_real_terminate_ends_a_real_child_and_reports_unknown_pids(self):
        import subprocess as sp
        import sys
        child = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(laptop.System().terminate_process(child.pid), "terminated")
        self.assertIsNotNone(child.poll())
        with self.assertRaises(laptop.ActionError) as ctx:
            laptop.System().terminate_process(child.pid)      # already gone and reaped
        self.assertEqual(ctx.exception.status, 400)


class LaunchTests(LaptopCase):
    def test_open_url_refuses_everything_but_http_and_https(self):
        client = self.build()
        bad = ["file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "JaVaScRiPt:alert(1)",
               "data:text/html,<script>1</script>", "ftp://example.com", "calc.exe", "//evil.example",
               "http://", "https://", "mailto:a@b.c", "vbscript:x", "ms-settings:", "http://a b.example",
               "https://ok.example/\nfile:///x", " file:///x", "\\\\server\\share", "C:\\Windows\\notepad.exe"]
        for url in bad:
            r = self.run_action(client, "open_url", {"url": url})
            self.assertEqual(r.status_code, 400, url)
        self.assertEqual(self.system.of("open_url"), [])
        self.assertEqual(len(self.audits), len(bad))     # attempts are on record even though refused

    def test_open_url_opens_plain_web_addresses(self):
        client = self.build()
        for url in ("https://example.com/path?q=1", "http://localhost:8765/", "HTTPS://Example.com"):
            self.assertEqual(self.run_action(client, "open_url", {"url": url}).status_code, 200, url)
        self.assertEqual([c[1] for c in self.system.of("open_url")],
                         ["https://example.com/path?q=1", "http://localhost:8765/", "HTTPS://Example.com"])

    def test_open_url_reports_a_browser_that_would_not_open(self):
        self.system.url_opens = False
        self.assertEqual(self.run_action(self.build(), "open_url", {"url": "https://a.example"}).status_code, 500)

    def test_open_path_refuses_a_missing_or_relative_path(self):
        client = self.build()
        missing = self.tmp / "nope.txt"
        r = self.run_action(client, "open_path", {"path": str(missing)}, confirm=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("does not exist", r.json()["detail"])
        self.assertEqual(self.run_action(client, "open_path", {"path": "relative/file.txt"}, confirm=True).status_code, 400)
        self.assertEqual(self.system.of("open_default"), [])

    def test_open_path_opens_an_existing_file_and_folder(self):
        client = self.build()
        file = self.tmp / "notes.txt"
        file.write_text("x")
        # Opening a file can start a program, so it asks first like the other risky actions.
        self.assertEqual(self.run_action(client, "open_path", {"path": str(file)}).status_code, 409)
        self.assertEqual(self.run_action(client, "open_path", {"path": str(file)}, confirm=True).status_code, 200)
        self.assertEqual(self.run_action(client, "open_path", {"path": f'"{self.tmp}"'}, confirm=True).status_code, 200)
        self.assertEqual([c[1] for c in self.system.of("open_default")], [str(file), str(self.tmp)])

    def test_open_path_refuses_programs_and_network_paths(self):
        client = self.build()
        for name in ("setup.exe", "run.bat", "x.ps1", "evil.lnk", "page.hta", "tool.JS", "app.msi"):
            program = self.tmp / name
            program.write_text("x")
            r = self.run_action(client, "open_path", {"path": str(program)}, confirm=True)
            self.assertEqual(r.status_code, 400, name)
            self.assertIn("not programs", r.json()["detail"])
        for unc in ("\\\\attacker.example\\share\\x.txt", "//attacker.example/share/x.txt"):
            r = self.run_action(client, "open_path", {"path": unc}, confirm=True)
            self.assertEqual(r.status_code, 400, unc)
            self.assertIn("Network paths", r.json()["detail"])
        self.assertEqual(self.system.of("open_default"), [])

    def test_open_app_uses_only_the_configured_target_and_args(self):
        client = self.build()
        r = self.run_action(client, "open_app:Chrome")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.system.of("start_app"), [("start_app", "chrome.exe", ["--new-window"])])
        # No way to add arguments from the phone.
        self.assertEqual(self.run_action(client, "open_app:Chrome", {"args": "--evil"}).status_code, 400)
        self.assertEqual(len(self.system.of("start_app")), 1)

    def test_open_app_by_name_refuses_names_not_in_the_config(self):
        client = self.build()
        for name in ("calc.exe", "Evil", "chrome", "../Chrome", "Chrome; calc", ""):
            r = self.run_action(client, "open_app", {"name": name})
            self.assertEqual(r.status_code, 400, name)
        self.assertEqual(self.system.of("start_app"), [])
        self.assertEqual(self.run_action(client, "open_app", {"name": "Notepad"}).status_code, 200)
        self.assertEqual(self.system.of("start_app"), [("start_app", "notepad.exe", [])])

    def test_open_app_with_no_apps_configured(self):
        client = self.build(apps={})
        r = self.run_action(client, "open_app", {"name": "anything"})
        self.assertEqual(r.status_code, 400)

    def test_an_app_that_will_not_start_is_a_500_sentence(self):
        class Broken(FakeSystem):
            def start_app(self, target, args):
                raise FileNotFoundError(target)
        r = self.run_action(self.build(Broken()), "open_app:Chrome")
        self.assertEqual(r.status_code, 500)
        self.assertIn("Chrome", r.json()["detail"])

    def test_real_start_app_builds_argument_lists_not_shell_strings(self):
        calls = []

        class Spy(laptop.System):
            platform = "linux"
            def which(self, name):
                return "/usr/bin/" + name
            def launch(self, argv):
                calls.append(argv)
        Spy().start_app("firefox", ["--private-window", "a b; calc"])
        self.assertEqual(calls, [["firefox", "--private-window", "a b; calc"]])


class ScriptTests(LaptopCase):
    SCRIPTS = {"hello": {"label": "Say hello", "argv": ["echo", "hello"], "cwd": "/work"}}

    def test_scripts_run_argv_only_with_120_seconds_and_the_cwd(self):
        self.system.run_result = (0, "hello\n", "")
        r = self.run_action(self.build(scripts=self.SCRIPTS), "script:hello")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["ok"], body["kind"], body["text"], body["exit"]), (True, "text", "hello\n", 0))
        self.assertEqual(self.system.of("run"), [("run", ["echo", "hello"], None, 120, "/work")])
        self.assertEqual(self.audits, [("laptop_run", {"action": "script:hello"})])

    def test_script_failures_report_instead_of_crashing(self):
        client = self.build(scripts=self.SCRIPTS)
        self.system.run_result = (3, "", "bad thing")
        body = self.run_action(client, "script:hello").json()
        self.assertEqual(body["exit"], 3)
        self.assertIn("bad thing", body["text"])
        self.system.run_error = FileNotFoundError()
        self.assertEqual(self.run_action(client, "script:hello").json()["exit"], 127)
        self.system.run_error = subprocess.TimeoutExpired("x", 120)
        timed_out = self.run_action(client, "script:hello").json()
        self.assertEqual(timed_out["exit"], 124)
        self.assertIn("120 seconds", timed_out["text"])


class ScreenshotTests(LaptopCase):
    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_screenshot_is_a_png_with_the_longest_side_at_most_1600(self):
        r = self.run_action(self.build(), "screenshot")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["ok"], body["kind"], body["mime"]), (True, "image", "image/png"))
        png = base64.b64decode(body["image_b64"])
        self.assertTrue(png.startswith(b"\x89PNG"))
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.size, (1600, 900))
        self.assertIn("1600x900", body["message"])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_tall_and_small_screens(self):
        self.system.screen_size = (900, 2400)
        image = Image.open(io.BytesIO(base64.b64decode(
            self.run_action(self.build(), "screenshot").json()["image_b64"])))
        self.assertEqual(image.size, (600, 1600))
        self.system.screen_size = (800, 600)            # never enlarged
        image = Image.open(io.BytesIO(base64.b64decode(
            self.run_action(self.build(), "screenshot").json()["image_b64"])))
        self.assertEqual(image.size, (800, 600))

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_a_failed_grab_is_a_500_sentence(self):
        self.system.grab_error = OSError("screen grab failed")
        r = self.run_action(self.build(), "screenshot")
        self.assertEqual(r.status_code, 500)
        self.assertIn("Could not capture the screen", r.json()["detail"])


class ClipboardTests(LaptopCase):
    def test_set_clipboard_passes_the_text_on_stdin_never_in_argv(self):
        client = self.build()
        r = self.run_action(client, "set_clipboard", {"text": SECRET})
        self.assertEqual(r.status_code, 200, r.text)
        _, argv, stdin, _, _ = self.system.of("run")[0]
        self.assertEqual(stdin, SECRET)
        self.assertEqual(argv[:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])
        self.assertNotIn("hunter2", " ".join(argv))
        self.assertNotIn("calc", " ".join(argv).replace("Set-Clipboard", ""))
        self.assertIn("Set-Clipboard", argv[-1])
        self.assertNotIn("hunter2", json.dumps(r.json()))     # not echoed back either

    def test_get_clipboard_returns_the_text(self):
        self.system.run_result = (0, "copied text", "")
        body = self.run_action(self.build(), "get_clipboard").json()
        self.assertEqual((body["kind"], body["text"]), ("text", "copied text"))
        self.assertIn("Get-Clipboard", self.system.of("run")[0][1][-1])
        self.assertIsNone(self.system.of("run")[0][2])

    def test_empty_clipboard_and_failures(self):
        client = self.build()
        self.system.run_result = (0, "", "")
        self.assertEqual(self.run_action(client, "get_clipboard").json()["text"], "")
        self.system.run_result = (1, "", "boom")
        self.assertEqual(self.run_action(client, "get_clipboard").status_code, 500)

    def test_the_size_cap_is_100000_characters(self):
        client = self.build()
        ok = self.run_action(client, "set_clipboard", {"text": "a" * 100_000})
        self.assertEqual(ok.status_code, 200)
        tooBig = self.run_action(client, "set_clipboard", {"text": "a" * 100_001})
        self.assertEqual(tooBig.status_code, 400)
        self.assertIn("too long", tooBig.json()["detail"])
        self.assertEqual(self.run_action(client, "set_clipboard", {"text": ""}).status_code, 400)
        self.system.run_result = (0, "b" * 150_000, "")
        self.assertEqual(len(self.run_action(client, "get_clipboard").json()["text"]), 100_000)


class MediaTests(LaptopCase):
    def test_keys_are_the_right_virtual_keys(self):
        client = self.build()
        for name, key in (("mute", "volume_mute"), ("play_pause", "play_pause"), ("next_track", "next_track"),
                          ("previous_track", "previous_track")):
            self.assertEqual(self.run_action(client, name).status_code, 200, name)
            self.assertEqual(self.system.of("key")[-1], ("key", key, 1))
        self.run_action(client, "volume_up")
        self.run_action(client, "volume_down", {"steps": 3})
        self.assertEqual(self.system.of("key")[-2:], [("key", "volume_up", 5), ("key", "volume_down", 3)])
        self.assertEqual(laptop.MEDIA_KEYS["volume_up"], 0xAF)
        self.assertEqual(laptop.MEDIA_KEYS["volume_down"], 0xAE)
        self.assertEqual(laptop.MEDIA_KEYS["volume_mute"], 0xAD)
        self.assertEqual(laptop.MEDIA_KEYS["play_pause"], 0xB3)
        self.assertEqual(laptop.MEDIA_KEYS["next_track"], 0xB0)
        self.assertEqual(laptop.MEDIA_KEYS["previous_track"], 0xB1)


class RealSystemReadOnlyTests(LaptopCase):
    """The harmless built-ins against this machine's real psutil (Windows, Linux or macOS)."""

    def setUp(self):
        super().setUp()
        self.client = self.build(laptop.System())

    def test_the_info_actions_return_text(self):
        for name in ("system_info", "network_info", "uptime"):
            body = self.run_action(self.client, name).json()
            self.assertEqual(body["kind"], "text", name)
            self.assertTrue(body["text"], name)

    def test_disk_usage_is_a_table(self):
        body = self.run_action(self.client, "disk_usage").json()
        self.assertEqual(body["kind"], "table")
        self.assertEqual(body["columns"], ["Drive", "Total GB", "Used GB", "Free GB", "Used %"])
        for row in body["rows"]:
            self.assertEqual(len(row), 5)

    def test_process_table_shape(self):
        body = self.run_action(self.client, "list_processes").json()
        self.assertEqual((body["ok"], body["kind"]), (True, "table"))
        self.assertEqual(body["columns"], ["PID", "Name", "Memory (MB)", "CPU %"])
        rows = body["rows"]
        self.assertTrue(0 < len(rows) <= 30)
        for pid, name, mem, cpu in rows:
            self.assertIsInstance(pid, int)
            self.assertIsInstance(name, str)
            self.assertIsInstance(mem, float)
            self.assertIsInstance(cpu, float)
        memories = [r[2] for r in rows]
        self.assertEqual(memories, sorted(memories, reverse=True))

    def test_battery_is_available_or_says_why_not(self):
        a = self.listing(self.client)["battery"]
        if a["available"]:
            self.assertIn("%", self.run_action(self.client, "battery").json()["text"])
        else:
            self.assertTrue(a["unavailable_reason"])
            self.assertEqual(self.run_action(self.client, "battery").status_code, 501)


class HelperTests(unittest.TestCase):
    def test_valid_url(self):
        self.assertEqual(laptop.valid_url(" https://a.example/x "), "https://a.example/x")
        for bad in ("file:///x", "javascript:1", "http://", "", "a.example", "http://[::1"):
            with self.assertRaises(laptop.BadParams):
                laptop.valid_url(bad)

    def test_durations(self):
        self.assertEqual(laptop._duration(59), "0 minutes")
        self.assertEqual(laptop._duration(3600 + 120), "1 hour, 2 minutes")
        self.assertEqual(laptop._duration(2 * 86400 + 3 * 3600), "2 days, 3 hours, 0 minutes")


if __name__ == "__main__":
    unittest.main()
