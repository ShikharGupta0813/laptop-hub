"""The auto router, the Gemini failover for fumbled local tool calls, and the local tool model. Fakes only."""
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from hub import providers
from hub.devserver import FakeAssistant, FakeTranscriber
from hub.router import Router, classify
from hub.server import Deps, create_app
from tests.fakes_assistant import ollama_calls, ollama_text
from tests.helpers import TOKEN, make_config
from tests.test_assistant import Base, collect, of
from tests.test_duo import FakeGemini, Tagged

BAD = {"name": "read_file", "arguments": {}}            # missing "path": a bad call
GOOD = '<tool_call>{"name": "system_status", "arguments": {}}</tool_call>'


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify("hi there")[0], "trivial")
        self.assertEqual(classify("thanks, that was great")[0], "trivial")
        self.assertEqual(classify("create a folder called taxes")[0], "heavy")
        self.assertEqual(classify("what is on my calendar")[0], "heavy")
        self.assertEqual(classify("here is code ```x = 1``` why")[0], "heavy")
        self.assertEqual(classify("word " * 61)[0], "heavy")
        self.assertEqual(classify("why is the sky blue during the day but red at sunset in the evening")[0], "normal")


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(default_provider="auto")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.gem = mock.patch.object(providers, "gemini_key", return_value="k")
        self.cli = mock.patch.object(providers, "find_claude_cli", return_value=None)
        self.gem_m, _ = self.gem.start(), self.cli.start()
        # a real logged-in CLI AI on this machine (agy, Gemini CLI) must not change the default
        self.ready_cli = mock.patch.object(providers, "_ready_cli_entries", return_value=[])
        self.ready_cli.start()
        self.addCleanup(self.ready_cli.stop)
        self.addCleanup(self.gem.stop)
        self.addCleanup(self.cli.stop)
        self.router = Router(self.config)

    def test_trivial_goes_local_and_tools_go_duo(self):
        self.assertEqual(self.router.route("auto", "hello!")[0], "local")
        self.assertEqual(self.router.last["kind"], "trivial")
        provider, decision = self.router.route("", "find my tax pdf and move it")
        self.assertEqual((provider, decision["routed_to"]), ("duo", "duo"))
        self.assertEqual(self.router.route("auto", "why is the sky blue during the day but red at sunset")[0], "gemini")

    def test_explicit_pick_is_untouched(self):
        self.assertEqual(self.router.route("claude-login", "hello!"), ("claude-login", None))
        self.assertEqual(self.router.route("local", "find my files")[1], None)
        self.config.default_provider = "gemini"       # an explicit default is an explicit pick too
        self.assertEqual(self.router.route("auto", "hello!"), ("gemini", None))

    def test_router_off_is_the_old_behaviour(self):
        self.config.router_enabled = False
        self.assertEqual(self.router.route("auto", "hello!"), ("gemini", None))
        self.assertEqual(self.router.route("auto", "find my files"), ("gemini", None))
        self.assertEqual(self.router.status()["enabled"], False)

    def test_falls_back_to_default_when_not_ready(self):
        self.assertEqual(self.router.route("auto", "hello", ollama_up=False)[0], "gemini")
        self.gem_m.return_value = ""
        self.assertEqual(self.router.route("auto", "find my files")[0], "local")    # default is local here


class RouterServerTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config(default_provider="auto")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        for name, val in (("gemini_key", "k"), ("find_claude_cli", None)):
            m = mock.patch.object(providers, name, return_value=val)
            m.start()
            self.addCleanup(m.stop)

    def client(self):
        deps = Deps(llm=Tagged("local"), claude=Tagged("claude"), claude_login=Tagged("cli"),
                    gemini=Tagged("gemini"), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        c = TestClient(create_app(self.config, deps, audit_path=self.root / "audit.log"),
                       base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(c.close)
        self.assertEqual(c.post("/api/login", json={"token": TOKEN}).status_code, 200)
        return c

    def events(self, c, text, provider="auto"):
        r = c.post("/api/assistant/message", json={"session_id": "s", "text": text, "provider": provider,
                                                    "auto_approve": False})
        return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]

    def test_meta_event_status_and_ask(self):
        c = self.client()
        self.assertEqual(c.get("/api/status").json()["providers"]["router"], {"enabled": True, "last_decision": None})
        meta = self.events(c, "hello")[0]
        self.assertEqual((meta["type"], meta["routed_to"]), ("meta", "local"))
        self.assertEqual(self.events(c, "hello", "gemini")[0]["type"], "text")      # explicit: no meta
        r = c.get("/api/status").json()
        self.assertEqual(r["providers"]["router"]["last_decision"]["routed_to"], "local")
        self.assertEqual(r["failovers"], 0)
        ask = c.post("/api/ask", json={"mode": "custom", "question": "find my files", "provider": "auto"})
        lines = [json.loads(x[6:]) for x in ask.text.splitlines() if x.startswith("data: {")]
        self.assertEqual(lines[0]["meta"]["routed_to"], "duo")

    def test_router_off_sends_no_meta(self):
        self.config.router_enabled = False
        c = self.client()
        self.assertEqual(self.events(c, "hello")[0]["type"], "text")


class FailoverTest(Base):
    def local(self, script):
        from tests.test_assistant import Local
        return Local.local(self, script)

    def setUp(self):
        super().setUp()
        for name, val in (("gemini_key", "k"), ("find_claude_cli", None)):
            m = mock.patch.object(providers, name, return_value=val)
            m.start()
            self.addCleanup(m.stop)

    def svc(self, local_script, gemini_script):
        svc = self.service(local=self.local(local_script))
        self.gemini = FakeGemini(gemini_script)
        svc.model_for = lambda p: self.gemini if p == "gemini" else None
        return svc

    async def test_two_bad_calls_then_gemini_does_that_turn(self):
        svc = self.svc([ollama_calls(BAD), ollama_calls(BAD), ollama_text("Local wraps up.")], [[GOOD]])
        events = await collect(svc.run("s", "read the file", "local", False))
        notes = of(events, "note")
        self.assertEqual(len(notes), 1)
        self.assertIn("asked Gemini", notes[0]["text"])
        names = [e["name"] for e in of(events, "tool_start")]
        self.assertEqual(names, ["read_file", "read_file", "system_status"])
        self.assertEqual(len(self.gemini.calls), 1)
        self.assertEqual(svc.failovers, 1)
        self.assertFalse(of(events, "error"))
        self.assertEqual("".join(e["t"] for e in of(events, "text")), "Local wraps up.")

    async def test_without_gemini_the_old_behaviour_stays(self):
        svc = self.svc([ollama_calls(BAD), ollama_calls(BAD), ollama_text("Giving up.")], [])
        svc.model_for = lambda p: None
        events = await collect(svc.run("s", "read the file", "local", False))
        self.assertFalse(of(events, "note"))
        self.assertEqual(svc.failovers, 0)
        self.assertEqual("".join(e["t"] for e in of(events, "text")), "Giving up.")

    async def test_failover_off_and_gemini_not_ready(self):
        svc = self.svc([ollama_calls(BAD), ollama_calls(BAD), ollama_text("Done.")], [["x"]])
        svc.config.tool_failover = False
        events = await collect(svc.run("s", "read the file", "local", False))
        self.assertFalse(of(events, "note"))
        self.assertEqual(self.gemini.calls, [])
        svc.config.tool_failover = True
        with mock.patch.object(providers, "gemini_key", return_value=""):
            self.assertFalse(svc._failover_ready())

    async def test_never_fails_over_to_claude(self):
        asked = []
        svc = self.svc([ollama_calls(BAD), ollama_calls(BAD), ollama_text("Done.")], [])
        svc.model_for = lambda p: asked.append(p) or None
        with mock.patch.object(providers, "find_claude_cli", return_value="/bin/claude"):
            await collect(svc.run("s", "read the file", "local", False))
        self.assertTrue(set(asked) <= {"gemini"})

    async def test_good_calls_never_fail_over(self):
        (self.home / "f.txt").write_text("hi")
        svc = self.svc([ollama_calls({"name": "read_file", "arguments": {"path": "f.txt"}}),
                        ollama_text("It says hi.")], [])
        events = await collect(svc.run("s", "read f.txt", "local", False))
        self.assertFalse(of(events, "note"))


class ToolModelTest(Base):
    def local(self, script):
        from tests.test_assistant import Local
        return Local.local(self, script)

    async def test_tool_turns_use_the_tool_model_and_chat_keeps_chat_model(self):
        self.config.local_tool_model = "qwen3:4b-instruct-q8_0"
        svc = self.service(local=self.local([ollama_text("Hello!"), ollama_calls({"name": "system_status", "arguments": {}}),
                                             ollama_text("All good.")]))
        await collect(svc.run("s", "hello there my friend, how are you", "local", False))   # no tools sent
        await collect(svc.run("s", "check the system status", "local", False))
        models = [b["model"] for b in self.ollama.bodies]
        self.assertEqual(models, ["qwen3:4b", "qwen3:4b-instruct-q8_0", "qwen3:4b-instruct-q8_0"])
        self.assertNotIn("tools", self.ollama.bodies[0])

    async def test_off_by_default(self):
        svc = self.service(local=self.local([ollama_text("ok")]))
        await collect(svc.run("s", "check the system status", "local", False))
        self.assertEqual(self.ollama.bodies[0]["model"], "qwen3:4b")
        self.assertEqual(self.config.local_tool_model, "")
