"""Local-model speed: tool routing, small context, keep_alive, warm-up, status events."""
import unittest

import httpx

from hub import assistant as A
from hub import assistant_tools_more, llm
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.fakes_assistant import FakeOllama, ollama_calls, ollama_text, reply
from tests.test_assistant import Base, collect, of


class SpeedTest(Base):
    def setUp(self):
        super().setUp()
        app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(),
                                           assistant=FakeAssistant()),
                         audit_path=self.root / "audit.log")
        self.extra = assistant_tools_more.build(lambda: app, self.config)

    def local(self, script):
        self.ollama = FakeOllama(script)
        return A.LocalChat("http://ollama.test", "qwen3:4b", transport=self.ollama.transport)

    def names(self, i):
        return {t["function"]["name"] for t in self.ollama.bodies[i].get("tools", [])}

    async def test_greeting_sends_no_tools_and_small_ctx(self):
        svc = self.service(local=self.local([ollama_text("Hi!")]), extra_tools=self.extra)
        await collect(svc.run("s", "Hello there my friend, how are you today", "local", False))
        body = self.ollama.bodies[0]
        self.assertNotIn("tools", body)
        self.assertEqual(body["options"]["num_ctx"], 4096)
        self.assertIn("DATA", body["messages"][0]["content"])
        # the prompt names the temp home and shared folders; their length depends on the OS, not on the prompt
        paths = len(str(self.home)) + sum(len(v) for v in self.config.roots.values())
        self.assertLess(len(body["messages"][0]["content"]) - paths, 900)

    async def test_reminder_request_includes_reminder_tools(self):
        svc = self.service(local=self.local([ollama_text("ok")]), extra_tools=self.extra)
        await collect(svc.run("s", "remind me tomorrow to call mum", "local", False))
        names = self.names(0)
        self.assertLessEqual({"reminder_add", "reminders_list"}, names)
        self.assertNotIn("spending_summary", names)
        self.assertEqual(self.ollama.bodies[0]["options"]["num_ctx"], 8192)
        for t in self.ollama.bodies[0]["tools"]:
            self.assertLessEqual(len(t["function"]["description"]), 90)

    async def test_used_tool_stays_included(self):
        svc = self.service(local=self.local([
            ollama_calls({"name": "reminders_list", "arguments": {}}), ollama_text("none"),
            ollama_text("Fine.")]), extra_tools=self.extra)
        await collect(svc.run("s", "what are my reminders", "local", False))
        await collect(svc.run("s", "thanks, how are you", "local", False))
        self.assertIn("reminders_list", self.names(2))
        self.assertIn("reminders_list", self.names(1))

    async def test_keep_alive_on_every_request(self):
        svc = self.service(local=self.local([ollama_text("a"), ollama_text("b")]))
        await collect(svc.run("s", "what is the weather", "local", False))
        await collect(svc.run("s", "list the files in ~", "local", False))
        self.assertTrue(all(b["keep_alive"] == "30m" for b in self.ollama.bodies))
        self.assertIn("list_dir", self.names(1))

    async def test_claude_still_gets_all_tools(self):
        svc = self.service([reply("hi")], extra_tools=self.extra)
        await collect(svc.run("s", "what is the weather", "claude", False))
        sent = {t["name"] for t in self.claude.calls[0]["tools"]}
        self.assertEqual(sent, {s["name"] for s in svc.specs})

    async def test_status_events_are_opt_in(self):
        svc = self.service(local=self.local([ollama_text("a")]))
        self.assertFalse(of(await collect(svc.run("s", "what is the weather", "local", False)), "status"))
        svc.emit_status = True
        svc._local = self.local([ollama_text("b"), ollama_text("c")])
        svc._local_last_used = float("-inf")  # cold
        events = await collect(svc.run("t", "what is the weather", "local", False))
        self.assertEqual(of(events, "status")[0]["text"], "Loading the local model…")
        events = await collect(svc.run("t", "again", "local", False))
        self.assertEqual(of(events, "status")[0]["text"], "Thinking…")

    def test_history_is_trimmed_to_budget(self):
        hist = []
        for i in range(10):
            hist += [{"role": "user", "content": f"q{i} " + "x" * 3000},
                     {"role": "assistant", "content": "a" * 3000}]
        view = A.fit_history(hist, 2000)
        self.assertEqual(view[0]["role"], "user")
        self.assertLess(len(view), len(hist))
        self.assertTrue(view[-1] is hist[-1])


class WarmUp(unittest.IsolatedAsyncioTestCase):
    async def test_warm_up_posts_empty_prompt_with_keep_alive(self):
        seen = []

        def handler(request):
            import json
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"done": True})
        ok = await llm.warm_up("http://o.test", "m", transport=httpx.MockTransport(handler))
        self.assertTrue(ok)
        path, body = seen[0]
        self.assertEqual((path, body["prompt"], body["keep_alive"]), ("/api/generate", "", "30m"))

    async def test_warm_up_never_raises(self):
        def boom(request):
            raise httpx.ConnectError("down")
        self.assertFalse(await llm.warm_up("http://o.test", "m", transport=httpx.MockTransport(boom)))


if __name__ == "__main__":
    unittest.main()
