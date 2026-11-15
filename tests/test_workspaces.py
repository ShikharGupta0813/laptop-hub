"""Tests for WORKSPACES and the `add-script` command. The hub's other routes are replaced by a recording
caller and sleeping is faked: nothing touches the laptop or waits."""
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import __main__ as cli
from hub import config as cfg
from hub import workspaces as ws
from hub.common import Ctx
from tests.helpers import make_config

SCRIPTS = {"warp_connect": {"label": "Connect Cloudflare", "argv": ["warp-cli", "connect"]},
           "hello": {"label": "Say hello", "argv": ["echo", "hi"]}}


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class FakeHub:
    def __init__(self):
        self.calls = []
        self.routes = {}

    async def __call__(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, body))
        answer = self.routes.get((method, path))
        if answer is None:
            if path.startswith("/api/run/"):
                answer = (True, {"exit": 0, "output": "ok"})
            elif path == "/api/laptop/run":
                answer = (True, {"ok": True, "message": "Opened the address on the laptop."})
            elif path == "/api/screen/status":
                answer = (True, {"available": True})
            else:
                answer = (False, "That feature is not available on this hub.")
        return answer

    def paths(self, prefix):
        return [c for c in self.calls if c[1].startswith(prefix)]


class Base(unittest.TestCase):
    def setUp(self):
        self.audits, self.slept, self.hub = [], [], FakeHub()
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="ws-test-"), scripts=dict(SCRIPTS))
        self.ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda event, **f: self.audits.append((event, f)))
        self.app = FastAPI()

        async def fake_sleep(seconds):
            self.slept.append(seconds)
        self.app.include_router(ws.make_router(self.ctx, caller=self.hub, sleep=fake_sleep))
        self.client = TestClient(self.app)
        self.client.cookies.set("hub_session", "yes")

    def make(self, **extra):
        body = {"name": "NiFi", "url": "https://nifi.onestack.in", "pre_steps": [{"type": "script", "name": "warp_connect"}],
                "wait_seconds": 8, **extra}
        r = self.client.post("/api/workspaces", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestCrud(Base):
    def test_login_required(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/workspaces").status_code, 401)
        self.assertEqual(self.client.post("/api/workspaces/abc/launch", json={"confirm": True}).status_code, 401)

    def test_create_list_update_delete(self):
        w = self.make()
        self.assertEqual((w["open_in"], w["arm_minutes"], w["wait_seconds"]), ("default_browser", 5, 8))
        self.assertEqual([x["id"] for x in self.client.get("/api/workspaces").json()["workspaces"]], ["whole-laptop", w["id"]])
        r = self.client.put(f"/api/workspaces/{w['id']}", json={"name": "NiFi prod", "arm_minutes": 10})
        self.assertEqual((r.json()["name"], r.json()["arm_minutes"], r.json()["url"]), ("NiFi prod", 10, "https://nifi.onestack.in"))
        self.assertEqual(self.client.delete(f"/api/workspaces/{w['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/workspaces/{w['id']}").status_code, 404)
        self.assertTrue(Path(self.config.data_path() / "workspaces.json").exists())

    def test_bad_input_refused(self):
        post = lambda **b: self.client.post("/api/workspaces", json={"name": "X", "url": "https://a.example", **b})
        self.assertEqual(self.client.post("/api/workspaces", json={"name": "  ", "url": "https://a.example"}).status_code, 400)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.example", "https://", "https://a b.example", "nifi.onestack.in"):
            self.assertEqual(post(url=url).status_code, 400, url)
        self.assertEqual(post(pre_steps=[{"type": "script", "name": "nope"}]).status_code, 400)
        self.assertEqual(post(pre_steps=[{"type": "shell", "name": "hello"}]).status_code, 400)
        self.assertEqual(post(pre_steps=[{"type": "script", "name": "../x"}]).status_code, 400)
        self.assertEqual(post(wait_seconds=121).status_code, 422)
        self.assertEqual(post(arm_minutes=16).status_code, 422)
        self.assertEqual(post(arm_minutes=0).status_code, 422)
        self.assertEqual(post(open_in="chrome").status_code, 400)
        self.assertEqual(post(argv=["calc"]).status_code, 422)
        self.assertEqual(self.client.post("/api/workspaces", json={"name": "Empty"}).status_code, 400)

    def test_templates(self):
        t = self.client.get("/api/workspaces/templates").json()["templates"][0]
        self.assertEqual((t["name"], t["url"], t["wait_seconds"]), ("NiFi (company VPN)", "https://nifi.onestack.in", 8))
        self.assertEqual(t["pre_steps"], [{"type": "script", "name": "warp_connect"}])
        self.assertEqual(t["missing_scripts"], [])
        del self.config.scripts["warp_connect"]
        self.assertEqual(self.client.get("/api/workspaces/templates").json()["templates"][0]["missing_scripts"], ["warp_connect"])

    def test_search(self):
        self.make()
        r = self.client.get("/api/workspaces/search", params={"q": "nifi"}).json()["results"]
        self.assertEqual((r[0]["kind"], r[0]["open"]["view"]), ("workspace", "screen"))
        self.assertEqual(self.client.get("/api/workspaces/search", params={"q": "zzz"}).json()["results"], [])


class TestLaunch(Base):
    def launch(self, w, **body):
        return self.client.post(f"/api/workspaces/{w['id']}/launch", json=body)

    def test_needs_confirm(self):
        w = self.make()
        self.assertEqual(self.launch(w).status_code, 409)
        self.assertEqual(self.launch(w, confirm="yes").status_code, 422)
        self.assertEqual(self.hub.calls, [])

    def test_happy_path_order_and_no_arming(self):
        w = self.make()
        r = self.launch(w, confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual([(s["type"], s["ok"]) for s in data["steps"]], [("script", True), ("wait", True), ("url", True)])
        self.assertTrue(data["opened"] and data["screen_ready"])
        self.assertEqual([c[1] for c in self.hub.calls], ["/api/run/warp_connect", "/api/laptop/run", "/api/screen/status"])
        self.assertEqual(self.hub.calls[1][2], {"action": "open_url", "params": {"url": "https://nifi.onestack.in"}, "confirm": True})
        self.assertEqual(self.slept, [8.0])
        self.assertFalse([c for c in self.hub.calls if "/arm" in c[1] or "/input" in c[1]])

    def test_audit_has_name_and_host_only(self):
        w = self.make(url="https://nifi.onestack.in/secret/path?token=abc")
        self.launch(w, confirm=True)
        event, fields = [a for a in self.audits if a[0] == "workspace_launch"][0]
        self.assertEqual(fields["host"], "nifi.onestack.in")
        self.assertNotIn("secret", json.dumps(fields))
        self.assertNotIn("token", json.dumps(fields))

    def test_failed_step_stops_before_opening(self):
        w = self.make()
        self.hub.routes[("POST", "/api/run/warp_connect")] = (True, {"exit": 1, "output": "Error: policy blocks this\n"})
        data = self.launch(w, confirm=True).json()
        self.assertFalse(data["opened"])
        self.assertTrue(data["stopped"])
        self.assertFalse(data["steps"][0]["ok"])
        self.assertIn("policy blocks", data["steps"][0]["message"])
        self.assertFalse(self.hub.paths("/api/laptop/run"))
        self.assertEqual(self.slept, [])

    def test_skip_steps_only_opens(self):
        w = self.make()
        data = self.launch(w, confirm=True, skip_steps=True).json()
        self.assertTrue(data["opened"])
        self.assertEqual([s["type"] for s in data["steps"]], ["url"])
        self.assertFalse(self.hub.paths("/api/run/"))
        self.assertEqual(self.slept, [])

    def test_unknown_script_refused_before_anything_runs(self):
        w = self.make()
        del self.config.scripts["warp_connect"]            # hub.json edited after saving
        r = self.launch(w, confirm=True)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.hub.calls, [])

    def test_bad_stored_url_refused(self):
        w = self.make()
        path = self.config.data_path() / "workspaces.json"
        data = json.loads(path.read_text())
        data["workspaces"][0]["steps"][-1]["url"] = "file:///C:/Windows/System32/calc.exe"
        path.write_text(json.dumps(data))
        self.assertEqual(self.launch(w, confirm=True).status_code, 400)
        self.assertEqual(self.hub.calls, [])

    def test_open_failure_reported(self):
        w = self.make()
        self.hub.routes[("POST", "/api/laptop/run")] = (False, "The laptop could not open a browser for that address.")
        data = self.launch(w, confirm=True).json()
        self.assertFalse(data["opened"])
        self.assertIn("could not open", data["open_message"])

    def test_screen_not_installed(self):
        w = self.make()
        self.hub.routes[("GET", "/api/screen/status")] = (True, {"available": False})
        self.assertFalse(self.launch(w, confirm=True).json()["screen_ready"])

    def test_no_url_only_scripts(self):
        w = self.make(url="")
        data = self.launch(w, confirm=True).json()
        self.assertFalse(data["opened"])
        self.assertFalse(self.hub.paths("/api/laptop/run"))

    def test_unknown_workspace(self):
        self.assertEqual(self.client.post("/api/workspaces/abcdef123456/launch", json={"confirm": True}).status_code, 404)


class FakeSystem:
    """A Start Menu on disk (two folders, like ProgramData and AppData) and a recorder instead of os.startfile."""
    platform = "win32"

    def __init__(self, dirs):
        self.dirs, self.started, self.fail = dirs, [], None

    def start_menu_dirs(self):
        return self.dirs

    def start(self, path):
        if self.fail:
            raise self.fail
        self.started.append(path)


def touch(base: Path, rel: str):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    return p


class AppBase(Base):
    """Base with a fake Start Menu tree."""
    def setUp(self):
        super().setUp()
        tree = Path(tempfile.mkdtemp(prefix="startmenu-"))
        self.common, self.user = tree / "ProgramData", tree / "AppData"
        for rel in ("Google Chrome.lnk", "Visual Studio Code/Visual Studio Code.lnk", "Microsoft Edge.lnk",
                    "Uninstall Foo.lnk", "Foo/Uninstall Foo App.lnk", "Foo/Foo.lnk", "Foo/Readme.lnk", "Foo/Release Notes.lnk",
                    ".secret.lnk", "notes.txt", "desktop.ini", "Windows Terminal.lnk", "7-Zip/7-Zip File Manager.lnk"):
            touch(self.common, rel)
        for rel in ("Google Chrome.lnk", "Zed Editor.lnk", "File Explorer.lnk"):
            touch(self.user, rel)
        self.system = FakeSystem([self.common, self.user])
        self.app.router.routes.clear()
        self.app.include_router(ws.make_router(self.ctx, caller=self.hub, sleep=self._sleep, system=self.system))

    async def _sleep(self, seconds):
        self.slept.append(seconds)

    def apps(self, q=""):
        r = self.client.get("/api/workspaces/apps", params={"q": q})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["apps"]

    def app_id(self, name):
        return next(a["id"] for a in self.apps() if a["name"] == name)


class TestDiscovery(AppBase):
    def test_lists_dedupes_filters_and_sorts(self):
        names = [a["name"] for a in self.apps()]
        self.assertEqual(names, sorted(names, key=str.casefold))
        self.assertEqual(names.count("Google Chrome"), 1)                          # in both folders
        for good in ("Visual Studio Code", "Microsoft Edge", "Windows Terminal", "Zed Editor", "File Explorer", "Foo", "7-Zip File Manager"):
            self.assertIn(good, names)
        for bad in ("Uninstall Foo", "Uninstall Foo App", "Readme", "Release Notes", ".secret", "notes", "desktop"):
            self.assertNotIn(bad, names)
        self.assertTrue(all(set(a) >= {"id", "name", "source"} for a in self.apps()))

    def test_ids_are_stable_and_search_filters(self):
        self.assertEqual(self.app_id("Microsoft Edge"), self.app_id("Microsoft Edge"))
        self.assertEqual(self.app_id("Microsoft Edge"), ws.app_id_for(str(self.common / "Microsoft Edge.lnk")))
        self.assertEqual([a["name"] for a in self.apps("EDGE")], ["Microsoft Edge"])
        self.assertEqual(self.apps("zzzz"), [])

    def test_cap_and_empty_off_windows(self):
        for i in range(320):
            touch(self.user, f"Many/App {i:03d}.lnk")
        self.assertEqual(len(self.apps()), 300)
        self.system.dirs = []
        body = self.client.get("/api/workspaces/apps").json()
        self.assertEqual(body["apps"], [])
        self.assertFalse(body["supported"])

    def test_config_apps_included_and_win_name_clash(self):
        self.config.apps["Google Chrome"] = {"target": "chrome.exe"}
        self.config.apps["Notes tool"] = {"target": "notepad.exe"}
        rows = {a["name"]: a for a in self.apps()}
        self.assertEqual(rows["Notes tool"]["source"], "hub.json")
        self.assertEqual(rows["Google Chrome"]["source"], "hub.json")

    def test_login_required(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/workspaces/apps").status_code, 401)
        self.assertEqual(self.client.post("/api/workspaces/quick", json={"type": "url", "url": "https://a.example", "confirm": True}).status_code, 401)


class TestAppLaunch(AppBase):
    def quick(self, **body):
        return self.client.post("/api/workspaces/quick", json=body)

    def test_unknown_ids_and_paths_refused(self):
        real = str(self.common / "Microsoft Edge.lnk")
        for bad in ("deadbeefdead", "cfg:nope", real, "../../x", "C:\\Windows\\System32\\calc.exe", "", "x" * 90):
            r = self.quick(type="app", id=bad, confirm=True)
            self.assertIn(r.status_code, (400, 422), bad)
        self.assertEqual(self.system.started, [])
        self.assertEqual(self.hub.calls, [])
        # also as a saved workspace step
        r = self.client.post("/api/workspaces", json={"name": "X", "steps": [{"type": "app", "id": "deadbeefdead"}]})
        self.assertEqual(r.status_code, 400)
        # a long Windows temp path trips the 80-char id limit first (422); either way it is refused
        self.assertIn(self.client.post("/api/workspaces", json={"name": "X", "steps": [{"type": "app", "id": real}]}).status_code, (400, 422))

    def test_launch_needs_confirm(self):
        r = self.quick(type="app", id=self.app_id("Microsoft Edge"))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.system.started, [])

    def test_quick_launch_starts_only_the_discovered_path(self):
        r = self.quick(type="app", id=self.app_id("Microsoft Edge"), confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["opened"])
        self.assertEqual(self.system.started, [str(self.common / "Microsoft Edge.lnk")])
        audit = [a for a in self.audits if a[0] == "workspace_step"][0][1]
        self.assertEqual((audit["kind"], audit["name"]), ("app", "Microsoft Edge"))
        self.assertNotIn("ProgramData", json.dumps(self.audits))

    def test_app_removed_after_saving_is_refused_at_launch(self):
        w = self.client.post("/api/workspaces", json={"name": "Dev", "steps": [{"type": "app", "id": self.app_id("Zed Editor")}]}).json()
        (self.user / "Zed Editor.lnk").unlink()
        r = self.client.post(f"/api/workspaces/{w['id']}/launch", json={"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.system.started, [])

    def test_config_app_goes_through_laptop_route(self):
        self.config.apps["Notes tool"] = {"target": "notepad.exe"}
        r = self.quick(type="app", id="cfg:Notes tool", confirm=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.hub.calls[0][2], {"action": "open_app", "params": {"name": "Notes tool"}, "confirm": True})
        self.assertEqual(self.system.started, [])

    def test_start_failure_reported(self):
        self.system.fail = OSError("blocked")
        data = self.quick(type="app", id=self.app_id("Microsoft Edge"), confirm=True).json()
        self.assertFalse(data["ok"] or data["opened"])
        self.assertIn("could not be started", data["steps"][0]["message"])

    def test_recents_counting(self):
        edge, chrome = self.app_id("Microsoft Edge"), self.app_id("Google Chrome")
        for app_id in (edge, chrome, edge):
            self.quick(type="app", id=app_id, confirm=True)
        recents = self.client.get("/api/workspaces/recents").json()["recents"]
        self.assertEqual([(r["name"], r["count"]) for r in recents], [("Microsoft Edge", 2), ("Google Chrome", 1)])
        self.assertEqual({a["name"]: a["count"] for a in self.apps()}["Microsoft Edge"], 2)
        saved = json.loads((self.config.data_path() / "workspaces.json").read_text())
        self.assertEqual(saved["recents"][edge]["count"], 2)
        (self.common / "Microsoft Edge.lnk").unlink()                    # an app that is gone is not suggested
        self.assertEqual([r["name"] for r in self.client.get("/api/workspaces/recents").json()["recents"]], ["Google Chrome"])
        self.assertEqual(self.client.get("/api/workspaces/recents", params={"limit": 0}).status_code, 422)

    def test_workspace_with_app_step_counts_too(self):
        w = self.client.post("/api/workspaces", json={"name": "Dev", "steps": [
            {"type": "app", "id": self.app_id("Zed Editor")}, {"type": "wait", "seconds": 2}, {"type": "url", "url": "https://a.example/x"}]}).json()
        self.assertEqual(w["steps"][0]["name"], "Zed Editor")
        data = self.client.post(f"/api/workspaces/{w['id']}/launch", json={"confirm": True}).json()
        self.assertTrue(data["ok"] and data["opened"])
        self.assertEqual(self.slept, [2.0])
        self.assertEqual(self.client.get("/api/workspaces/recents").json()["recents"][0]["count"], 1)


class TestStepValidation(AppBase):
    def setUp(self):
        super().setUp()
        root = Path(self.config.roots["docs"])
        (root / "sub").mkdir(exist_ok=True)
        (root / "sub" / "plan.txt").write_text("hi")
        (root / "tool.exe").write_text("MZ")

    def post(self, steps):
        return self.client.post("/api/workspaces", json={"name": "T", "steps": steps}).status_code

    def test_table(self):
        edge = self.app_id("Microsoft Edge")
        table = [
            ([{"type": "script", "name": "hello"}], 200),
            ([{"type": "script", "name": "nope"}], 400),
            ([{"type": "script", "name": "../x"}], 400),
            ([{"type": "script"}], 400),
            ([{"type": "url", "url": "https://a.example/p?q=1"}], 200),
            ([{"type": "url", "url": "http://a.example"}], 200),
            ([{"type": "url", "url": "file:///etc/passwd"}], 400),
            ([{"type": "url", "url": "javascript:alert(1)"}], 400),
            ([{"type": "url", "url": "https://a b.example"}], 400),
            ([{"type": "url"}], 400),
            ([{"type": "app", "id": edge}], 200),
            ([{"type": "app", "id": "nothere12345"}], 400),
            ([{"type": "app", "id": edge, "path": "C:/x.exe"}], 400),
            ([{"type": "app", "path": "C:/Windows/notepad.exe"}], 400),
            ([{"type": "folder", "root": "docs", "path": "sub"}], 200),
            ([{"type": "folder", "root": "docs", "path": ""}], 200),
            ([{"type": "folder", "root": "docs", "path": "../.."}], 403),
            ([{"type": "folder", "root": "docs", "path": "/etc"}], 400),
            ([{"type": "folder", "root": "nope", "path": ""}], 400),
            ([{"type": "folder", "root": "docs", "path": "sub/plan.txt"}], 400),
            ([{"type": "folder", "root": "docs", "path": "missing"}], 400),
            ([{"type": "folder", "root": "docs", "path": "sub", "url": "https://a.example"}], 400),
            ([{"type": "file", "root": "docs", "path": "sub/plan.txt"}], 200),
            ([{"type": "file", "root": "docs", "path": "tool.exe"}], 400),
            ([{"type": "file", "root": "docs", "path": "sub"}], 400),
            ([{"type": "file", "root": "docs", "path": "../a.md"}], 403),
            ([{"type": "wait", "seconds": 0}], 200),
            ([{"type": "wait", "seconds": 120}], 200),
            ([{"type": "wait", "seconds": 121}], 422),
            ([{"type": "wait", "seconds": -1}], 422),
            ([{"type": "wait"}], 400),
            ([{"type": "wait", "seconds": 5, "name": "x"}], 400),
            ([{"type": "shell", "name": "hello"}], 400),
            ([{"type": "script", "name": "hello", "argv": ["calc"]}], 422),
            ([], 400),
            ([{"type": "wait", "seconds": 120}] * 3, 400),                 # waits add up to more than 300
            ([{"type": "wait", "seconds": 1}] * 12, 200),
            ([{"type": "wait", "seconds": 1}] * 13, 422),                  # more than 12 steps
        ]
        for steps, want in table:
            self.assertEqual(self.post(steps), want, steps)

    def test_steps_and_old_fields_not_mixed(self):
        r = self.client.post("/api/workspaces", json={"name": "T", "url": "https://a.example", "steps": [{"type": "wait", "seconds": 1}]})
        self.assertEqual(r.status_code, 400)

    def test_folder_and_file_open_through_laptop_route_with_resolved_path(self):
        w = self.client.post("/api/workspaces", json={"name": "T", "steps": [
            {"type": "folder", "root": "docs", "path": "sub"}, {"type": "file", "root": "docs", "path": "sub/plan.txt"}]}).json()
        data = self.client.post(f"/api/workspaces/{w['id']}/launch", json={"confirm": True}).json()
        self.assertTrue(data["ok"])
        calls = [c[2] for c in self.hub.paths("/api/laptop/run")]
        base = Path(self.config.roots["docs"]).resolve()
        self.assertEqual(calls, [{"action": "open_path", "params": {"path": str(base / "sub")}, "confirm": True},
                                 {"action": "open_path", "params": {"path": str(base / "sub" / "plan.txt")}, "confirm": True}])
        audit = [a[1] for a in self.audits if a[0] == "workspace_step"]
        self.assertEqual([a["root"] for a in audit], ["docs", "docs"])
        self.assertNotIn("plan.txt", json.dumps(self.audits))

    def test_path_escape_stored_by_hand_is_refused_at_launch(self):
        w = self.client.post("/api/workspaces", json={"name": "T", "steps": [{"type": "folder", "root": "docs", "path": "sub"}]}).json()
        path = self.config.data_path() / "workspaces.json"
        data = json.loads(path.read_text())
        data["workspaces"][0]["steps"][0]["path"] = "../../.."
        path.write_text(json.dumps(data))
        r = self.client.post(f"/api/workspaces/{w['id']}/launch", json={"confirm": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.hub.calls, [])

    def test_failure_stops_the_rest(self):
        w = self.client.post("/api/workspaces", json={"name": "T", "steps": [
            {"type": "script", "name": "hello"}, {"type": "app", "id": self.app_id("Zed Editor")}, {"type": "url", "url": "https://a.example"}]}).json()
        self.hub.routes[("POST", "/api/run/hello")] = (True, {"exit": 2, "output": "bad"})
        data = self.client.post(f"/api/workspaces/{w['id']}/launch", json={"confirm": True}).json()
        self.assertTrue(data["stopped"])
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(self.system.started, [])
        # "Open anyway": skips the button but still opens the rest
        data = self.client.post(f"/api/workspaces/{w['id']}/launch", json={"confirm": True, "skip_steps": True}).json()
        self.assertEqual([s["type"] for s in data["steps"]], ["app", "url"])
        self.assertEqual(len(self.system.started), 1)


class TestMigrationAndWholeLaptop(AppBase):
    def old_file(self, records):
        (self.config.data_path() / "workspaces.json").write_text(json.dumps(records))

    def test_old_plain_list_is_migrated(self):
        self.old_file([{"id": "abcdef123456", "name": "NiFi", "icon": "globe", "url": "https://nifi.onestack.in",
                        "pre_steps": [{"type": "script", "name": "warp_connect"}], "wait_seconds": 8, "open_in": "default_browser",
                        "arm_minutes": 7, "created": 1.0}])
        w = self.client.get("/api/workspaces/abcdef123456").json()
        self.assertEqual(w["steps"], [{"type": "script", "name": "warp_connect"}, {"type": "wait", "seconds": 8},
                                      {"type": "url", "url": "https://nifi.onestack.in"}])
        self.assertEqual((w["url"], w["wait_seconds"], w["pre_steps"], w["arm_minutes"]),
                         ("https://nifi.onestack.in", 8, [{"type": "script", "name": "warp_connect"}], 7))
        data = self.client.post("/api/workspaces/abcdef123456/launch", json={"confirm": True}).json()
        self.assertEqual([s["type"] for s in data["steps"]], ["script", "wait", "url"])
        self.assertEqual(self.slept, [8.0])

    def test_old_url_only_has_no_wait_and_old_put_still_works(self):
        self.old_file([{"id": "abcdef123456", "name": "Docs", "url": "https://a.example", "pre_steps": [], "wait_seconds": 8}])
        self.assertEqual(self.client.get("/api/workspaces/abcdef123456").json()["steps"], [{"type": "url", "url": "https://a.example"}])
        r = self.client.put("/api/workspaces/abcdef123456", json={"url": "https://b.example", "pre_steps": [{"type": "script", "name": "hello"}], "wait_seconds": 3})
        self.assertEqual([s["type"] for s in r.json()["steps"]], ["script", "wait", "url"])
        saved = json.loads((self.config.data_path() / "workspaces.json").read_text())
        self.assertNotIn("url", saved["workspaces"][0])
        self.assertIsInstance(saved["recents"], dict)

    def test_old_fields_refused_on_a_workspace_with_new_steps(self):
        w = self.client.post("/api/workspaces", json={"name": "T", "steps": [{"type": "app", "id": self.app_id("Zed Editor")}]}).json()
        self.assertEqual(self.client.put(f"/api/workspaces/{w['id']}", json={"url": "https://b.example"}).status_code, 400)

    def test_whole_laptop_tile_needs_no_steps(self):
        listed = self.client.get("/api/workspaces").json()["workspaces"]
        tile = listed[0]
        self.assertEqual((tile["id"], tile["builtin"], tile["steps"]), ("whole-laptop", True, []))
        self.assertEqual(self.client.post("/api/workspaces/whole-laptop/launch", json={}).status_code, 409)
        r = self.client.post("/api/workspaces/whole-laptop/launch", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["ok"] and data["screen_ready"])
        self.assertEqual((data["steps"], data["opened"]), ([], False))
        self.assertEqual([c[1] for c in self.hub.calls], ["/api/screen/status"])      # nothing launched, control not armed
        self.assertEqual(self.system.started, [])
        self.assertEqual(self.client.delete("/api/workspaces/whole-laptop").status_code, 400)
        self.assertEqual(self.client.put("/api/workspaces/whole-laptop", json={"name": "x"}).status_code, 400)
        self.assertEqual(len(self.client.get("/api/workspaces").json()["workspaces"]), 1)   # never stored

    def test_quick_url_and_folder(self):
        q = lambda **b: self.client.post("/api/workspaces/quick", json=b)
        self.assertEqual(q(type="url", url="https://a.example").status_code, 409)
        self.assertEqual(q(type="url", url="file:///x", confirm=True).status_code, 400)
        self.assertEqual(q(type="script", name="hello", confirm=True).status_code, 400)
        self.assertEqual(q(type="folder", root="docs", path="../..", confirm=True).status_code, 403)
        self.assertEqual(self.hub.calls, [])
        r = q(type="url", url="https://a.example/secret?t=1", confirm=True)
        self.assertTrue(r.json()["opened"])
        self.assertNotIn("secret", json.dumps(self.audits))


class TestLock(Base):
    def test_second_launch_while_running_is_409(self):
        gate = asyncio.Event()

        async def slow_sleep(_):
            await gate.wait()
        app = FastAPI()
        app.include_router(ws.make_router(self.ctx, caller=self.hub, sleep=slow_sleep))

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                         cookies={"hub_session": "yes"}) as c:
                made = (await c.post("/api/workspaces", json={"name": "N", "url": "https://a.example",
                                                              "pre_steps": [{"type": "script", "name": "hello"}], "wait_seconds": 5})).json()
                first = asyncio.create_task(c.post(f"/api/workspaces/{made['id']}/launch", json={"confirm": True}))
                await asyncio.sleep(0.05)
                second = await c.post(f"/api/workspaces/{made['id']}/launch", json={"confirm": True})
                delete = await c.delete(f"/api/workspaces/{made['id']}")
                gate.set()
                done = await first
                again = await c.post(f"/api/workspaces/{made['id']}/launch", json={"confirm": True})
                return second.status_code, delete.status_code, done.status_code, again.status_code
        self.assertEqual(asyncio.run(scenario()), (409, 409, 200, 200))


class TestAddScript(unittest.TestCase):
    def setUp(self):
        config, _ = cfg.make()
        self.path = Path(tempfile.mkdtemp(prefix="cli-test-")) / "hub.json"
        cfg.save(config, self.path)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--config", str(self.path), *args])
        return code, out.getvalue()

    def test_saves_and_prints(self):
        exe = "C:\\Program Files\\Cloudflare\\Cloudflare WARP\\warp-cli.exe"
        code, out = self.run_cli("add-script", "warp_connect", "--label", "Connect Cloudflare", "--", exe, "connect")
        self.assertEqual(code, 0)
        self.assertIn("warp_connect", out)
        self.assertIn("Connect Cloudflare", out)
        self.assertEqual(cfg.load(self.path).scripts["warp_connect"], {"label": "Connect Cloudflare", "argv": [exe, "connect"]})

    def test_keeps_dashes_and_options_in_argv(self):
        self.run_cli("add-script", "x", "--label", "L", "--", "prog", "--label", "-v")
        self.assertEqual(cfg.load(self.path).scripts["x"]["argv"], ["prog", "--label", "-v"])

    def test_default_label_and_replace(self):
        self.run_cli("add-script", "a-b_1", "--", "echo")
        self.assertEqual(cfg.load(self.path).scripts["a-b_1"], {"label": "a-b_1", "argv": ["echo"]})
        code, out = self.run_cli("add-script", "a-b_1", "--label", "New", "--", "echo", "2")
        self.assertIn("Replaced", out)
        self.assertEqual(cfg.load(self.path).scripts["a-b_1"]["label"], "New")

    def test_refuses_bad_name_and_empty_argv(self):
        before = self.path.read_text()
        for name in ("Bad Name", "UPPER", "a" * 41, "../x", "a;b"):
            self.assertEqual(self.run_cli("add-script", name, "--", "echo")[0], 1, name)
        self.assertEqual(self.run_cli("add-script", "ok")[0], 1)
        self.assertEqual(self.run_cli("add-script", "ok", "--")[0], 1)
        self.assertEqual(self.path.read_text(), before)

    def test_missing_config(self):
        self.path.unlink()
        self.assertEqual(self.run_cli("add-script", "ok", "--", "echo")[0], 1)


if __name__ == "__main__":
    unittest.main()
