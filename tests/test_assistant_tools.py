"""The assistant's Work/laptop/Jarvis tools, run against the real app (Work in demo mode)."""
import os
import unittest

from hub import assistant as A
from hub import assistant_tools
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.fakes_assistant import reply, tool_block
from tests.test_assistant import Base, collect, of


class ToolsTest(Base):
    def setUp(self):
        super().setUp()
        os.environ["HUB_WORK_DEMO"] = "1"  # restored when Base's environment patch is undone
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(),
                                                assistant=FakeAssistant()),
                              audit_path=self.root / "audit.log")
        self.tools = assistant_tools.build(lambda: self.app, self.config)

    def svc(self, script, **kw):
        return self.service(script, extra_tools=self.tools, **kw)

    def call(self, name, **args):
        return reply(tool_block("c1", name, args))

    def by_name(self, name):
        return next(t for t in self.tools if t.spec["name"] == name)

    async def test_reading_tasks_and_meetings_needs_no_approval(self):
        svc = self.svc([self.call("asana_tasks", filter="all"), self.call("calendar"), reply("Done.")])
        events = await collect(svc.run("s", "what's on?", "claude", False))
        self.assertEqual(of(events, "approval_needed"), [])
        first, second = [e["output"] for e in of(events, "tool_result")]
        self.assertIn("overdue", first)
        self.assertRegex(first, r"id \d+")
        self.assertIn("Team standup", second)

    async def test_task_details_include_comments_and_subtasks(self):
        ok, listing = await self.tools[0].run({"filter": "all"})
        gid = listing.split("id ")[1].split()[0]
        ok, text = await self.by_name("asana_task").run({"gid": gid})
        self.assertTrue(ok)
        self.assertIn("project", text)

    async def test_sending_email_asks_first_and_shows_everything(self):
        svc = self.svc([self.call("send_email", to="me", subject="Hi", body="Hello there"), reply("Sent.")])
        shown = []
        events = await collect(svc.run("s", "mail me", "claude", False),
                               on_approval=lambda e: (shown.append(e["input"]), svc.resolve("s", e["id"], True)))
        self.assertEqual(shown[0]["subject"], "Hi")
        self.assertEqual(shown[0]["body"], "Hello there")
        self.assertIn("Email sent", of(events, "tool_result")[0]["output"])

    async def test_declined_email_is_never_drafted_or_sent(self):
        svc = self.svc([self.call("send_email", to="me", subject="Hi", body="x"), reply("OK.")])
        events = await collect(svc.run("s", "mail me", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], False))
        self.assertTrue(of(events, "tool_result")[0]["denied"])
        ok, text = await self.by_name("chat_send").run({"space": "nope", "text": "x"})
        self.assertFalse(ok)  # a bad space is a plain sentence, not a crash
        self.assertIsInstance(text, str)

    async def test_completing_a_task_asks_and_then_removes_it(self):
        ok, listing = await self.tools[0].run({"filter": "overdue"})
        gid = listing.split("id ")[1].split()[0]
        svc = self.svc([self.call("asana_complete", gid=gid), reply("Done.")])
        events = await collect(svc.run("s", "finish it", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], True))
        self.assertEqual(len(of(events, "approval_needed")), 1)
        ok, after = await self.tools[0].run({"filter": "all"})
        self.assertNotIn(f"id {gid}", after)

    async def test_a_task_id_that_is_not_a_plain_number_never_becomes_a_request(self):
        calls = []
        real = assistant_tools.Hub.call

        async def spy(hub, method, path, **kw):
            calls.append(path)
            return await real(hub, method, path, **kw)

        assistant_tools.Hub.call = spy
        self.addCleanup(setattr, assistant_tools.Hub, "call", real)
        for bad in ["../../api/status", "12/../../x", "1 2", "12?x=1", "١٢٣", ""]:
            for name in ("asana_task", "asana_comment", "asana_complete"):
                with self.subTest(gid=bad, tool=name):
                    ok, text = await self.by_name(name).run({"gid": bad, "text": "x"})
                    self.assertFalse(ok)
                    self.assertIn("number", text)
        self.assertEqual(calls, [])

    async def test_a_feature_that_is_not_set_up_says_so(self):
        os.environ.pop("HUB_WORK_DEMO")
        app = create_app(self.config, Deps(llm=FakeLLM(), assistant=FakeAssistant()),
                         audit_path=self.root / "a2.log")
        tools = assistant_tools.build(lambda: app, self.config)
        ok, text = await next(t for t in tools if t.spec["name"] == "asana_tasks").run({"filter": "today"})
        self.assertFalse(ok)
        self.assertTrue(text)

    def test_which_tools_ask_first(self):
        asks = lambda name, **a: self.by_name(name).needs_approval(a)
        for name, args in [("asana_comment", {}), ("asana_complete", {}), ("send_email", {}), ("chat_send", {}),
                           ("laptop_action", {"action": "lock_screen"}), ("laptop_action", {"action": "kill_process"}),
                           ("laptop_action", {"action": "open_url"}), ("laptop_action", {"action": "get_clipboard"}),
                           ("jarvis", {"action": "start"}), ("jarvis", {"action": "stop"}), ("jarvis", {"action": "say"})]:
            with self.subTest(name=name, args=args):
                self.assertTrue(asks(name, **args))
        for name, args in [("asana_tasks", {}), ("asana_task", {}), ("calendar", {}),
                           ("laptop_action", {"action": "battery"}), ("laptop_action", {"action": "volume_up"}),
                           ("laptop_action", {"action": "list"}), ("jarvis", {"action": "status"}),
                           ("english_progress", {}), ("quiz_history", {})]:
            with self.subTest(name=name, args=args):
                self.assertFalse(asks(name, **args))

    async def test_the_assistant_cannot_look_at_screenshots(self):
        ok, text = await self.by_name("laptop_action").run({"action": "screenshot"})
        self.assertFalse(ok)
        self.assertIn("Apps", text)


if __name__ == "__main__":
    unittest.main()
