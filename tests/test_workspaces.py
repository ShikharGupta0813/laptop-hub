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
        self.assertEqual(len(self.client.get("/api/workspaces").json()["workspaces"]), 1)
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
        self.assertEqual([(s["name"], s["ok"]) for s in data["steps"]], [("warp_connect", True)])
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
        self.assertEqual(data["steps"], [])
        self.assertFalse(self.hub.paths("/api/run/"))

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
        data[0]["url"] = "file:///C:/Windows/System32/calc.exe"
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
