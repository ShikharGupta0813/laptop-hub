"""The assistant's tools for reminders, to-dos, habits, spending, mail headers and search."""
import unittest

from hub import assistant_tools_more
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.fakes_assistant import reply, tool_block
from tests.test_assistant import Base, collect, of

READS = {"reminders_list", "todos_list", "habits_today", "focus_status", "spending_summary", "inbox_unread",
         "watch_alerts", "notifications_recent", "search_documents", "search_everything"}
WRITES = {"reminder_add", "todo_add", "todo_done", "habit_check", "focus_start", "journal_add", "expense_add",
          "bookmark_add"}


class MoreToolsTest(Base):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(),
                                                assistant=FakeAssistant()), audit_path=self.root / "audit.log")
        self.tools = assistant_tools_more.build(lambda: self.app, self.config)

    def by_name(self, name):
        return next(t for t in self.tools if t.spec["name"] == name)

    def call(self, name, **args):
        return reply(tool_block("c1", name, args))

    def test_every_tool_that_changes_something_asks_first_and_reads_do_not(self):
        names = {t.spec["name"] for t in self.tools}
        self.assertEqual(names, READS | WRITES)
        for t in self.tools:
            self.assertEqual(t.needs_approval({}), t.spec["name"] in WRITES, t.spec["name"])

    def test_no_tool_deletes_or_sends(self):
        for t in self.tools:
            self.assertNotRegex(t.spec["name"], r"delete|remove|send|clear|drop")

    async def test_ids_with_path_tricks_are_refused(self):
        for bad in ("../x", "a/b", "", "x" * 80, "a b"):
            for name in ("todo_done", "habit_check"):
                ok, text = await self.by_name(name).run({"id": bad})
                self.assertFalse(ok)
                self.assertIn("not valid", text)

    async def test_a_reminder_added_through_the_assistant_appears_in_the_list(self):
        svc = self.service([self.call("reminder_add", text="in 30 minutes check the oven"),
                            self.call("reminders_list"), reply("Done.")], extra_tools=self.tools)
        shown = []
        events = await collect(svc.run("s", "remind me", "claude", False),
                               on_approval=lambda e: (shown.append(e["name"]), svc.resolve("s", e["id"], True)))
        self.assertEqual(shown, ["reminder_add"])          # the read did not ask
        outputs = [e["output"] for e in of(events, "tool_result")]
        self.assertIn("Saved", outputs[0])
        self.assertIn("check the oven", outputs[1])

    async def test_declining_a_todo_adds_nothing(self):
        svc = self.service([self.call("todo_add", text="buy milk tomorrow"), self.call("todos_list"), reply("OK.")],
                           extra_tools=self.tools)
        events = await collect(svc.run("s", "add it", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], False))
        first, second = of(events, "tool_result")
        self.assertTrue(first["denied"])
        self.assertIn("No tasks", second["output"])

    async def test_a_todo_can_be_added_listed_and_finished(self):
        ok, text = await self.by_name("todo_add").run({"text": "buy milk tomorrow !2"})
        self.assertTrue(ok, text)
        ok, listing = await self.by_name("todos_list").run({})
        self.assertIn("buy milk", listing)
        tid = listing.split(" id ")[1].split()[0]
        ok, text = await self.by_name("todo_done").run({"id": tid})
        self.assertTrue(ok, text)
        ok, listing = await self.by_name("todos_list").run({})
        self.assertIn("No tasks", listing)

    async def test_missing_features_give_a_sentence_not_a_crash(self):
        for name, args in (("inbox_unread", {}), ("watch_alerts", {}), ("search_documents", {"query": "x"})):
            ok, text = await self.by_name(name).run(args)
            self.assertIsInstance(text, str)

    def test_formatters_survive_odd_data(self):
        for fmt in (assistant_tools_more.fmt_reminders, assistant_tools_more.fmt_todos, assistant_tools_more.fmt_mail,
                    assistant_tools_more.fmt_alerts, assistant_tools_more.fmt_hits, assistant_tools_more.fmt_habits):
            for odd in (None, {}, [], {"x": 1}, [{}], {"results": [{"title": None}]}):
                self.assertIsInstance(fmt(odd), str)


if __name__ == "__main__":
    unittest.main()
