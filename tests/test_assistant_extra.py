"""Tools added by other features plug into the assistant under the same approval rule."""
import unittest

from hub import assistant as A
from tests.fakes_assistant import reply, tool_block
from tests.test_assistant import Base, collect, of


def schema(**props):
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()},
            "required": list(props), "additionalProperties": False}


class ExtraToolsTest(Base):
    def setUp(self):
        super().setUp()
        self.ran = []

        async def read_tasks(args):
            self.ran.append(("tasks", args))
            return True, "2 tasks due today"

        async def lock(args):
            self.ran.append(("lock", args))
            return True, "locked"

        async def broken(args):
            raise RuntimeError("boom")

        self.tools = [
            A.ExtraTool({"name": "my_tasks", "description": "Read the owner's tasks.",
                         "input_schema": schema(filter="string")}, read_tasks),
            A.ExtraTool({"name": "laptop_lock", "description": "Lock the laptop.",
                         "input_schema": schema(reason="string")}, lock, needs_approval=True),
            A.ExtraTool({"name": "risky_only_when_asked", "description": "x",
                         "input_schema": schema(danger="string")}, lock,
                        needs_approval=lambda args: args["danger"] == "yes"),
            A.ExtraTool({"name": "flaky", "description": "x", "input_schema": schema(a="string")}, broken),
        ]

    def svc(self, script):
        return self.service(script, extra_tools=self.tools)

    def call(self, name, **args):
        return reply(tool_block("c1", name, args))

    async def test_the_model_is_offered_the_extra_tools(self):
        svc = self.svc([reply("hi")])
        await collect(svc.run("s", "hello", "claude", False))
        names = [t["name"] for t in self.claude.calls[0]["tools"]]
        self.assertIn("my_tasks", names)
        self.assertIn("laptop_lock", names)
        self.assertIn("run_command", names)

    async def test_a_read_only_extra_tool_runs_without_asking(self):
        svc = self.svc([self.call("my_tasks", filter="today"), reply("You have two.")])
        events = await collect(svc.run("s", "my tasks?", "claude", False))
        self.assertFalse(of(events, "tool_start")[0]["needs_approval"])
        self.assertEqual(of(events, "approval_needed"), [])
        self.assertEqual(of(events, "tool_result")[0]["output"], "2 tasks due today")
        self.assertEqual(self.ran, [("tasks", {"filter": "today"})])

    async def test_a_changing_extra_tool_waits_for_the_owner(self):
        svc = self.svc([self.call("laptop_lock", reason="leaving"), reply("Locked.")])
        events = await collect(svc.run("s", "lock it", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], True))
        self.assertTrue(of(events, "tool_start")[0]["needs_approval"])
        self.assertEqual(len(of(events, "approval_needed")), 1)
        self.assertEqual(self.ran, [("lock", {"reason": "leaving"})])

    async def test_declining_means_the_tool_never_runs(self):
        svc = self.svc([self.call("laptop_lock", reason="x"), reply("OK.")])
        events = await collect(svc.run("s", "lock it", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], False))
        self.assertTrue(of(events, "tool_result")[0]["denied"])
        self.assertEqual(self.ran, [])

    async def test_auto_approve_skips_the_pause_but_is_still_audited(self):
        audit = []
        svc = self.service([self.call("laptop_lock", reason="x"), reply("Done.")],
                           extra_tools=self.tools, audit=lambda *a, **k: audit.append((a, k)))
        events = await collect(svc.run("s", "lock it", "claude", True))
        self.assertEqual(of(events, "approval_needed"), [])
        self.assertEqual(self.ran, [("lock", {"reason": "x"})])
        self.assertEqual(audit[0][0][0], "assistant_tool")
        self.assertEqual(audit[0][1]["tool"], "laptop_lock")

    async def test_approval_can_depend_on_the_arguments(self):
        svc = self.svc([self.call("risky_only_when_asked", danger="no"), reply("ok")])
        events = await collect(svc.run("s", "x", "claude", False))
        self.assertEqual(of(events, "approval_needed"), [])
        svc = self.svc([self.call("risky_only_when_asked", danger="yes"), reply("ok")])
        events = await collect(svc.run("s", "x", "claude", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], False))
        self.assertEqual(len(of(events, "approval_needed")), 1)

    async def test_a_crashing_extra_tool_is_reported_not_fatal(self):
        svc = self.svc([self.call("flaky", a="1"), reply("Sorry, that failed.")])
        events = await collect(svc.run("s", "x", "claude", False))
        result = of(events, "tool_result")[0]
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["output"])
        self.assertEqual(of(events, "text")[-1]["t"], "Sorry, that failed.")

    async def test_bad_arguments_never_reach_an_extra_tool(self):
        svc = self.svc([self.call("my_tasks", wrong=1), reply("ok")])
        events = await collect(svc.run("s", "x", "claude", False))
        self.assertFalse(of(events, "tool_result")[0]["ok"])
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
