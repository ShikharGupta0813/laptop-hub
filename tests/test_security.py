"""Regression tests for the issues found in the security review. Each one failed before its fix."""
import os
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub import assistant as A
from hub import assistant_tools, auth
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.fakes_assistant import reply, tool_block
from tests.helpers import TOKEN, make_config
from tests.test_assistant import Base as AssistantBase, collect, of


class RequestRulesTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config(allowed_hosts=["testserver", "hub.example.org"])
        deps = Deps(llm=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        self.app = create_app(self.config, deps, audit_path=self.root / "audit.log")
        self.client = TestClient(self.app, base_url="http://testserver")
        self.addCleanup(self.client.close)

    def login(self, **kw):
        return self.client.post("/api/login", json={"token": TOKEN}, headers={"X-Hub": "1", **kw})

    def test_a_plain_form_post_cannot_reach_any_changing_route(self):
        """An HTML form (even one injected into rendered text) cannot add a custom header."""
        self.login()
        for path in ["/api/run/hello", "/api/jarvis/start", "/api/work/asana/tasks/1/complete", "/api/logout"]:
            r = self.client.post(path, data={})  # what a browser form sends: no X-Hub
            self.assertEqual(r.status_code, 403, path)
        r = self.client.post("/api/upload", files={"file": ("a.txt", b"x")})  # multipart form
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/api/roots").status_code, 200)  # reading still works
        self.assertEqual(self.client.post("/api/run/hello", headers={"X-Hub": "1"}).status_code, 200)

    def test_the_login_itself_needs_the_header_too(self):
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 403)

    def test_only_our_own_host_names_are_answered(self):
        """A page on another name pointed at this laptop (DNS rebinding) is refused outright."""
        for host in ["evil.example", "rebind.evil.example:8765", "192.168.1.5"]:
            r = self.client.get("/api/session", headers={"Host": host})
            self.assertEqual(r.status_code, 421, host)
        for host in ["localhost:8765", "127.0.0.1", "my-laptop.tail1234.ts.net", "hub.example.org", "[::1]:8765"]:
            self.assertEqual(self.client.get("/api/session", headers={"Host": host}).status_code, 200, host)
        # the lockout cannot be driven from a rebound page either
        r = self.client.post("/api/login", json={"token": "x"}, headers={"X-Hub": "1", "Host": "evil.example"})
        self.assertEqual(r.status_code, 421)

    def test_the_terminal_socket_refuses_an_unknown_host(self):
        self.login()
        with self.client.websocket_connect("/ws/term?shell=sh", headers={"host": "evil.example"}) as ws:
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_text()
        self.assertEqual(caught.exception.code, 4403)

    def test_one_device_cannot_lock_everyone_out(self):
        attacker = {"X-Hub": "1", "X-Forwarded-For": "100.64.0.9"}
        for _ in range(auth.MAX_FAILS):
            self.client.post("/api/login", json={"token": "wrong"}, headers=attacker)
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}, headers=attacker).status_code, 429)
        owner = self.client.post("/api/login", json={"token": TOKEN},
                                 headers={"X-Hub": "1", "X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(owner.status_code, 200)

    def test_sessions_do_not_last_a_month(self):
        self.assertLessEqual(auth.SESSION_SECONDS, 14 * 24 * 3600)


class AssistantSafetyTest(AssistantBase):
    def svc(self, script, **kw):
        return self.service(script, **kw)

    def call(self, name, **args):
        return reply(tool_block("c1", name, args))

    async def refused(self, name, **args):
        svc = self.svc([self.call(name, **args), reply("ok")])
        events = await collect(svc.run("s", "go", "claude", False))
        result = of(events, "tool_result")[0]
        self.assertFalse(result["ok"], (name, args))
        self.assertEqual(of(events, "approval_needed"), [])
        return result["output"]

    async def test_network_paths_are_refused_by_every_path_tool(self):
        for unc in ("\\\\attacker.example\\share", "//attacker.example/share", "\\\\?\\C:\\x", "\\\\.\\pipe\\x"):
            for name, key in [("list_dir", "path"), ("read_file", "path"), ("file_info", "path"),
                              ("show_file", "path"), ("search_files", "folder"), ("write_file", "path")]:
                args = {key: unc, **({"query": "x"} if name == "search_files" else {}),
                        **({"content": "x"} if name == "write_file" else {})}
                with self.subTest(unc=unc, tool=name):
                    self.assertIn("network paths", await self.refused(name, **args))
        self.assertIn("network paths", await self.refused("run_command", command="dir", shell="sh",
                                                           cwd="\\\\attacker.example\\share"))

    async def test_credential_folders_cannot_be_read_without_asking(self):
        for sub in (".ssh", ".aws", ".claude", ".gnupg", ".laptop-hub"):
            folder = self.home / sub
            folder.mkdir(exist_ok=True)
            (folder / "secret.txt").write_text("top secret")
            for name, args in [("list_dir", {"path": str(folder)}),
                               ("read_file", {"path": str(folder / "secret.txt")}),
                               ("read_file", {"path": f"~/{sub}/secret.txt"}),
                               ("file_info", {"path": str(folder / "secret.txt")}),
                               ("search_files", {"query": "secret", "folder": str(folder)})]:
                with self.subTest(sub=sub, tool=name):
                    self.assertIn("credentials", await self.refused(name, **args))

    async def test_ordinary_folders_still_work(self):
        (self.home / "docs").mkdir()
        (self.home / "docs" / "a.txt").write_text("hello")
        svc = self.svc([self.call("read_file", path="~/docs/a.txt"), reply("ok")])
        events = await collect(svc.run("s", "read", "claude", False))
        self.assertIn("hello", of(events, "tool_result")[0]["output"])

    async def test_notes_the_assistant_writes_need_a_tap(self):
        svc = self.svc([self.call("add_note", text="<form action='/api/jarvis/start'>x</form>"), reply("ok")])
        events = await collect(svc.run("s", "note", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], False))
        self.assertEqual(len(of(events, "approval_needed")), 1)
        self.assertEqual(self.config.notes_dir and list(__import__("pathlib").Path(self.config.notes_dir).glob("*.md")), [])

    async def test_the_approval_card_gets_the_whole_text_not_a_shortened_copy(self):
        long_body = "harmless " * 400 + "SECRET-TAIL"
        self.assertGreater(len(long_body), A.EVENT_VALUE_CHARS)
        svc = self.svc([self.call("write_file", path=str(self.home / "x.txt"), content=long_body), reply("ok")])
        shown = []
        await collect(svc.run("s", "write", "claude", False),
                      on_approval=lambda e: (shown.append(e["input"]), svc.resolve("s", e["id"], False)))
        self.assertTrue(shown[0]["content"].endswith("SECRET-TAIL"))
        self.assertEqual(shown[0]["content"], long_body)

    async def test_a_call_too_long_to_review_is_refused_instead_of_half_shown(self):
        output = await self.refused("write_file", path=str(self.home / "x.txt"), content="x" * (A.MAX_REVIEW_CHARS + 1))
        self.assertIn("too long to show you", output)


class EmailApprovalTest(unittest.IsolatedAsyncioTestCase):
    """The card for an email shows the address it will really go to, and that exact draft is sent."""

    def setUp(self):
        os.environ["HUB_WORK_DEMO"] = "1"
        self.addCleanup(os.environ.pop, "HUB_WORK_DEMO", None)
        self.config, self.root = make_config()
        self.app = create_app(self.config, Deps(llm=FakeLLM(), assistant=FakeAssistant()),
                              audit_path=self.root / "audit.log")
        self.tools = {t.spec["name"]: t for t in assistant_tools.build(lambda: self.app, self.config)}

    async def test_preview_resolves_the_recipient_and_run_sends_that_draft(self):
        tool = self.tools["send_email"]
        args = {"to": "me", "subject": "Hi", "body": "Hello"}
        ok, info = await tool.preview(args)
        self.assertTrue(ok)
        self.assertRegex(info["resolved_to"], r"@")
        ok, text = await tool.run(args)
        self.assertTrue(ok, text)
        self.assertIn("sent", text.lower())

    async def test_an_unknown_recipient_fails_before_anyone_is_asked(self):
        ok, info = await self.tools["send_email"].preview({"to": "nobody-here", "subject": "s", "body": "b"})
        self.assertFalse(ok)
        self.assertIsInstance(info, str)

    async def test_the_assistants_own_session_never_expires_while_the_hub_runs(self):
        hub = assistant_tools.Hub(lambda: self.app, self.config)
        ok, _ = await hub.call("GET", "/api/roots")
        self.assertTrue(ok)
        # later calls mint a fresh cookie each time (no stored, expiring one)
        self.assertIsNone(hub._http().cookies.get(auth.COOKIE))


if __name__ == "__main__":
    unittest.main()
