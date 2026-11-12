"""Full Control, persona routing, fast commands, latency and the voice text helpers."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from hub import assistant as A
from hub import config as cfg
from hub import fastcmd, persona
from hub.latency import Latency
from hub.server import Deps, create_app
from tests.fakes_assistant import FakeClaude, FakeOllama, ollama_calls, ollama_text, reply, tool_block
from tests.helpers import TOKEN, make_config
from tests.test_settings import Base as SettingsBase

POSIX = sys.platform != "win32"
JS = Path(__file__).resolve().parent.parent / "hub" / "static" / "js"


async def collect(gen):
    return [e async for e in gen]


def of(events, kind):
    return [e for e in events if e["type"] == kind]


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="hub-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_CONFIG", "ANTHROPIC_API_KEY")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config, self.root = make_config(shells={"sh": ["/bin/sh"]})
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audit = []

    def service(self, script=(), local=None, **kw):
        self.claude = FakeClaude(script)
        return A.AssistantService(self.config, claude=self.claude, local=local,
                                  audit=lambda event, **f: self.audit.append((event, f)), **kw)


@unittest.skipUnless(POSIX, "uses /bin/sh")
class FullControlTest(Base):
    def run_call(self, command="echo hi"):
        return [reply(tool_block("t1", "run_command", {"command": command}), stop="tool_use"), reply("done")]

    async def test_off_asks_first(self):
        svc = self.service(self.run_call())
        events = []
        async for e in svc.run("s", "go", "claude", False):
            events.append(e)
            if e["type"] == "approval_needed":
                svc.resolve("s", e["id"], True)
        self.assertEqual(len(of(events, "approval_needed")), 1)
        self.assertTrue(all("full_control" not in f for _e, f in self.audit))

    async def test_on_runs_straight_away_and_is_audited(self):
        self.config.full_control = True
        svc = self.service(self.run_call("echo hi"))
        events = await collect(svc.run("s", "go", "claude", False))
        self.assertFalse(of(events, "approval_needed"))
        self.assertFalse(of(events, "tool_start")[0]["needs_approval"])
        self.assertTrue(of(events, "tool_start")[0]["full_control"])
        self.assertIn("hi", of(events, "tool_result")[0]["output"])
        rows = [f for e, f in self.audit if e == "assistant_tool"]
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["full_control"], True)
        self.assertEqual(rows[0]["tool"], "run_command")

    async def test_on_covers_file_writes_and_scripts_too(self):
        self.config.full_control = True
        target = self.home / "new.txt"
        svc = self.service([reply(tool_block("t1", "write_file", {"path": str(target), "content": "x"}), stop="tool_use"),
                            reply(tool_block("t2", "run_script", {"name": "hello"}), stop="tool_use"), reply("ok")])
        events = await collect(svc.run("s", "go", "claude", False))
        self.assertFalse(of(events, "approval_needed"))
        self.assertEqual(target.read_text(), "x")
        self.assertEqual(len([1 for e, f in self.audit if e == "assistant_tool" and f.get("full_control")]), 2)

    async def test_the_hub_own_files_stay_protected(self):
        self.config.full_control = True
        protected = self.home / ".laptop-hub" / "hub.json"
        svc = self.service([reply(tool_block("t1", "write_file", {"path": str(protected), "content": "x"}), stop="tool_use"),
                            reply("ok")])
        events = await collect(svc.run("s", "go", "claude", False))
        self.assertFalse(protected.exists())
        self.assertFalse(of(events, "tool_result")[0]["ok"])

    async def test_a_string_is_not_enough(self):
        self.config.full_control = "false"      # only a real True counts
        svc = self.service(self.run_call())
        self.assertFalse(svc.full_control)


@unittest.skipUnless(POSIX, "uses /bin/sh")
class FastPathTest(Base):
    async def test_run_goes_straight_to_the_tool_with_no_model_call(self):
        self.config.full_control = True
        svc = self.service()            # FakeClaude has no script: any model call would raise
        events = await collect(svc.run("s", "run echo fastpath", "claude", False))
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(of(events, "tool_start")[0]["name"], "run_command")
        self.assertFalse(of(events, "approval_needed"))
        text = "".join(e["t"] for e in of(events, "text"))
        self.assertIn("Done.", text)
        self.assertIn("fastpath", text)
        self.assertTrue(any(e == "assistant_fast_command" for e, _f in self.audit))
        self.assertTrue(any(e == "assistant_tool" and f.get("full_control") for e, f in self.audit))
        self.assertTrue(svc.latency.recent(1)[0]["fast"])

    async def test_nothing_fast_without_full_control(self):
        svc = self.service([reply("hello")])
        await collect(svc.run("s", "run echo x", "claude", False))
        self.assertEqual(len(self.claude.calls), 1)

    async def test_fast_commands_off_goes_to_the_model(self):
        self.config.full_control = True
        self.config.fast_commands = False
        svc = self.service([reply("hello")])
        await collect(svc.run("s", "run echo x", "claude", False))
        self.assertEqual(len(self.claude.calls), 1)

    async def test_plain_english_still_goes_to_the_model(self):
        self.config.full_control = True
        svc = self.service([reply("Sure.")])
        events = await collect(svc.run("s", "run through my schedule", "claude", False))
        self.assertEqual(len(self.claude.calls), 1)
        self.assertFalse(of(events, "tool_start"))

    async def test_a_failing_command_is_reported_not_raised(self):
        self.config.full_control = True
        svc = self.service()
        events = await collect(svc.run("s", "run sh -c 'exit 3'", "claude", False))
        text = "".join(e["t"] for e in of(events, "text"))
        self.assertFalse(of(events, "error"))
        self.assertIn("exit code 3", text)

    async def test_works_on_the_local_provider_too(self):
        self.config.full_control = True
        ollama = FakeOllama([])         # no scripted reply: a model call would fail the handler
        svc = self.service(local=A.LocalChat("http://x", "m", transport=ollama.transport))
        events = await collect(svc.run("s", "Run echo local.", "local", False))
        self.assertEqual(ollama.bodies, [])
        self.assertIn("local", "".join(e["t"] for e in of(events, "text")))


class FastPatternTest(unittest.TestCase):
    apps = {"Chrome": {"target": "chrome.exe"}, "VS Code": {"target": "code"}}

    def hit(self, text):
        return fastcmd.match_fast_command(text, self.apps)

    def test_commands(self):
        cases = {"run git status": "git status", "Run git status.": "git status",
                 "run this command on terminal: git pull": "git pull", "run command ls -la": "ls -la",
                 "run pytest -q on the terminal": "pytest -q", "in the terminal run pip list": "pip list",
                 "execute python -V": "python -V", "run the command echo hi": "echo hi",
                 "Run npm test in the terminal": "npm test", "run `ls`": "ls"}
        for text, command in cases.items():
            with self.subTest(text=text):
                got = self.hit(text)
                self.assertEqual(got["tool"], "run_command")
                self.assertEqual(got["args"], {"command": command})

    def test_open_only_known_apps_and_links(self):
        self.assertEqual(self.hit("open chrome")["args"], {"action": "open_app", "params": {"name": "Chrome"}})
        self.assertEqual(self.hit("Open VS Code.")["args"]["params"]["name"], "VS Code")
        self.assertEqual(self.hit("open https://example.com")["args"]["action"], "open_url")
        self.assertIsNone(self.hit("open the report"))
        self.assertIsNone(self.hit("open my heart to you"))

    def test_type(self):
        self.assertEqual(self.hit("type hello world")["args"], {"text": "hello world"})
        self.assertEqual(self.hit('type "hi there"')["args"], {"text": "hi there"})
        self.assertEqual(self.hit("type the following: dear sir,")["args"], {"text": "dear sir,"})

    def test_things_that_must_not_fast_path(self):
        for text in ["run through my schedule", "run the numbers on this", "run a check on the build", "run my errands",
                     "run it", "run this command", "can you run git status", "what happens if I run ls",
                     "type of weather is it", "type the report", "open", "hello", "run", "I run every morning",
                     "start with the plan", "", "x" * 700]:
            with self.subTest(text=text[:40]):
                self.assertIsNone(self.hit(text))


class ToolErrorTest(Base):
    async def test_a_tool_exception_becomes_a_message_and_the_stream_goes_on(self):
        svc = self.service([reply(tool_block("t1", "system_status", {}), stop="tool_use"), reply("recovered")])
        with mock.patch.object(svc, "_system_status", side_effect=RuntimeError("disk exploded")):
            events = await collect(svc.run("s", "status", "claude", False))
        result = of(events, "tool_result")[0]
        self.assertFalse(result["ok"])
        self.assertIn("disk exploded", result["output"])
        self.assertEqual("".join(e["t"] for e in of(events, "text")), "recovered")

    async def test_a_crashing_model_ends_as_an_error_event(self):
        svc = self.service([RuntimeError("model blew up")])
        events = await collect(svc.run("s", "what is the weather", "claude", False))
        self.assertEqual(of(events, "error")[0]["message"], "Assistant failed: model blew up")
        self.assertFalse(svc._sessions["s"].running)


class PersonaTest(Base):
    def test_split_python(self):
        s = persona.split_persona
        self.assertEqual(s("Jarvis, run the tests"), ("jarvis", "run the tests"))
        self.assertEqual(s("hey Friday what's my day"), ("friday", "what's my day"))
        self.assertEqual(s("okay jarvis open chrome"), ("jarvis", "open chrome"))
        self.assertEqual(s("run the tests, Jarvis."), ("jarvis", "run the tests"))
        self.assertEqual(s("friday"), ("friday", ""))
        self.assertEqual(s("Friday at 5 I have gym"), ("", "Friday at 5 I have gym"))
        self.assertEqual(s("Friday's plan"), ("", "Friday's plan"))
        self.assertEqual(s("what about friday"), ("", "what about friday"))
        self.assertEqual(s("Computer, run it", "Computer"), ("jarvis", "run it"))

    async def test_prompts_carry_the_persona_and_call_sign(self):
        self.config.display_name = "Boss"
        svc = self.service([reply("a"), reply("b"), reply("c")])
        await collect(svc.run("s", "x", "claude", False, persona="jarvis"))
        await collect(svc.run("s", "y", "claude", False, persona="friday"))
        await collect(svc.run("s", "z", "claude", False, voice=True))
        j, f, v = (c["system"] for c in self.claude.calls)
        self.assertIn("laptop operator", j)
        self.assertIn('"Boss"', j)
        self.assertIn("day assistant", f)
        self.assertNotIn("laptop operator", f)
        self.assertIn("spoken", v)
        self.assertNotIn("spoken", j)
        self.assertLess(len(j) - len(svc.system), 400)     # the persona line stays short

    async def test_persona_reaches_the_local_prompt(self):
        ollama = FakeOllama([ollama_text("ok")])
        svc = self.service(local=A.LocalChat("http://x", "m", transport=ollama.transport))
        await collect(svc.run("s", "what is the weather", "local", False, persona="friday"))
        self.assertIn("day assistant", ollama.bodies[0]["messages"][0]["content"])


class Recorder:
    """A stand-in assistant that records how the server called it."""
    def __init__(self):
        self.calls = []
        self.latency = Latency()

    async def run(self, session_id, text, provider, auto_approve, **kw):
        self.calls.append((text, kw))
        yield {"type": "text", "t": "ok"}

    def resolve(self, *a):
        return False

    def reset(self, *a):
        pass


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.rec = Recorder()
        app = create_app(self.config, Deps(assistant=self.rec), audit_path=self.root / "audit.log")
        self.client = TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)

    def send(self, text, **extra):
        r = self.client.post("/api/assistant/message", json={"session_id": "s", "text": text, "provider": "local", **extra})
        self.assertEqual(r.status_code, 200)
        return r

    def audit_rows(self):
        return [json.loads(x) for x in (self.root / "audit.log").read_text().splitlines()]

    def test_typed_message_with_a_leading_name_is_routed_and_stripped(self):
        self.send("Jarvis, run the tests")
        self.send("hey friday, what is my day")
        self.send("plain message")
        self.assertEqual(self.rec.calls[0], ("run the tests", {"persona": "jarvis"}))
        self.assertEqual(self.rec.calls[1], ("what is my day", {"persona": "friday"}))
        self.assertEqual(self.rec.calls[2], ("plain message", {}))

    def test_explicit_persona_and_voice_flag_pass_through(self):
        self.send("hello there", persona="friday", voice=True)
        self.assertEqual(self.rec.calls[0], ("hello there", {"persona": "friday", "voice": True}))

    def test_custom_jarvis_name(self):
        self.config.jarvis_name = "Computer"
        self.send("Computer, lock it")
        self.assertEqual(self.rec.calls[0], ("lock it", {"persona": "jarvis"}))

    def test_status_and_info_show_full_control(self):
        self.assertIs(self.client.get("/api/status").json()["full_control"], False)
        self.config.full_control = True
        self.assertIs(self.client.get("/api/status").json()["full_control"], True)
        info = self.client.get("/api/assistant/info").json()
        self.assertTrue(info["full_control"])
        self.assertEqual(info["jarvis_name"], "Jarvis")
        self.send("hi")
        self.assertTrue(self.audit_rows()[-1]["full_control"])

    def test_latency_endpoint(self):
        self.assertEqual(self.client.get("/api/latency").json()["spans"], [])
        self.rec.latency.add(stt_ms=300, first_token_ms=800, total_ms=1500)
        data = self.client.get("/api/latency?n=5").json()
        self.assertEqual(data["spans"][0]["first_token_ms"], 800)
        self.assertIn("first word 0.8s", data["summary"])
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/latency").status_code, 401)

    def test_warm_endpoint_answers_at_once(self):
        self.assertEqual(self.client.post("/api/assistant/warm").json(), {"ok": True})


class RealLatencyTest(Base):
    async def test_a_turn_records_a_span_with_stt_and_first_token(self):
        svc = self.service([reply("Hello there friend.")])
        svc.latency.note_stt(420)
        await collect(svc.run("s", "what is the weather", "claude", False, voice=True))
        span = svc.latency.recent(1)[0]
        self.assertEqual(span["stt_ms"], 420)
        self.assertIsInstance(span["first_token_ms"], int)
        self.assertGreaterEqual(span["total_ms"], span["first_token_ms"])
        self.assertTrue(span["voice"] and span["ok"])

    def test_ring_buffer_is_bounded_and_stt_expires(self):
        clock = [1000.0]
        lat = Latency(clock=lambda: clock[0], size=3)
        for i in range(5):
            lat.add(total_ms=i)
        self.assertEqual([r["total_ms"] for r in lat.recent(10)], [4, 3, 2])
        lat.note_stt(100)
        clock[0] += 500
        self.assertIsNone(lat.take_stt())

    async def test_gemini_text_voice_turn_skips_the_claude_planning_call(self):
        class Gemini:
            async def stream(self, system, prompt):
                yield "fine"

        class Supervisor:
            verify_enabled = True
            planned = 0

            async def plan(self, *a):
                Supervisor.planned += 1
                return "plan"

        class Duo:
            gemini = Gemini()
            supervisor = Supervisor()
        svc = self.service()
        svc.model_for = lambda name: Duo()
        await collect(svc.run("s", "please find my files and list them all now", "duo", False, voice=True))
        self.assertEqual(Supervisor.planned, 0)
        await collect(svc.run("s2", "please find my files and list them all now", "duo", False))
        self.assertEqual(Supervisor.planned, 1)


class FullControlSettingsTest(SettingsBase):
    def patch(self, body):
        return self.client.patch("/api/settings", json=body)

    def test_turning_on_needs_the_exact_phrase(self):
        for body in ({"full_control": True}, {"full_control": True, "full_control_confirm": "i understand"},
                     {"full_control": True, "confirm": True}, {"full_control": True, "full_control_confirm": True}):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 400)
                self.assertFalse(self.config.full_control)
        self.assertFalse(self.disk().get("full_control"))

    def test_on_with_phrase_persists_and_is_audited_then_off_is_one_call(self):
        r = self.patch({"full_control": True, "full_control_confirm": "I UNDERSTAND"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["full_control"])
        self.assertIs(self.config.full_control, True)
        self.assertIs(self.disk()["full_control"], True)
        row = [x for x in self.audit_rows() if x["event"] == "settings_change"][-1]
        self.assertEqual(row["changes"]["full_control"], {"old": False, "new": True})
        r = self.patch({"full_control": False})
        self.assertEqual(r.status_code, 200)
        self.assertIs(self.config.full_control, False)
        self.assertIs(self.disk()["full_control"], False)

    def test_other_new_keys(self):
        self.assertEqual(self.patch({"fast_commands": False, "jarvis_name": " Edith "}).status_code, 200)
        self.assertFalse(self.config.fast_commands)
        self.assertEqual(self.config.jarvis_name, "Edith")
        self.assertEqual(self.patch({"fast_commands": "no"}).status_code, 400)
        self.assertEqual(self.patch({"jarvis_name": ""}).status_code, 400)

    def test_config_file_only_trusts_a_real_true(self):
        data = self.disk()
        data["full_control"] = "false"
        self.config_path.write_text(json.dumps(data))
        self.assertIs(cfg.load(self.config_path).full_control, False)
        data["full_control"] = True
        self.config_path.write_text(json.dumps(data))
        self.assertIs(cfg.load(self.config_path).full_control, True)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class VoiceTextHelpersTest(unittest.TestCase):
    def run_js(self, expr):
        code = f'import {{splitName, splitStream, speakable}} from "{JS / "speechchunk.js"}"; console.log(JSON.stringify({expr}));'
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_name_routing_matches_python(self):
        cases = ["Jarvis, run the tests", "hey Friday what's my day", "run the tests, Jarvis.", "friday",
                 "Friday at 5 I have gym", "Friday's plan", "what about friday", "okay jarvis open chrome"]
        for text in cases:
            with self.subTest(text=text):
                got = self.run_js(f"splitName({json.dumps(text)})")
                self.assertEqual((got["persona"], got["text"]), persona.split_persona(text))

    def test_sentence_chunking(self):
        got = self.run_js('splitStream("Sure thing. I will open it now. Then we go")')
        self.assertEqual(got["done"], ["Sure thing. I will open it now."])
        self.assertEqual(got["rest"], " Then we go")
        self.assertEqual(self.run_js('splitStream("Version 3.5 is out and fine. ok", {final: true})')["done"],
                         ["Version 3.5 is out and fine.", "ok"])
        self.assertEqual(self.run_js('splitStream("go ```code. here. more")')["done"], [])
        self.assertEqual(self.run_js('splitStream("Short. Tiny.")')["done"], [])      # held back until flushed
        self.assertEqual(self.run_js('splitStream("Short. Tiny.", {final: true})')["done"], ["Short. Tiny."])

    def test_modules_parse(self):
        for name in ("voicemode.js", "assistant.js", "fullcontrol.js", "settings.js", "speechchunk.js", "home.js"):
            with self.subTest(name=name):
                src = (JS / name).read_text(encoding="utf-8")
                out = subprocess.run(["node", "--input-type=module", "--check"], input=src, capture_output=True, text=True)
                self.assertEqual(out.returncode, 0, out.stderr)


class GeminiConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_kept_alive_client_serves_every_turn_and_warm_up_opens_it(self):
        import httpx
        from hub.llm import GeminiClient
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"models": []})
            body = 'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n'
            return httpx.Response(200, text=body)
        g = GeminiClient(api_key="k", transport=httpx.MockTransport(handler))
        self.assertTrue(await g.warm())
        first = g._shared
        for _ in range(2):
            self.assertEqual("".join([p async for p in g.stream("s", "u")]), "hi")
        self.assertIs(g._shared, first)
        self.assertEqual(len(calls), 3)
        await first.aclose()
        self.assertTrue(g.has_key())


class SpeechTest(unittest.TestCase):
    def test_voice_transcription_uses_a_fast_beam_and_can_preload(self):
        from hub.speech import Transcriber
        seen = {}

        class Model:
            def transcribe(self, path, **kw):
                seen.update(kw)
                return [], None
        t = Transcriber("base")
        t._model = Model()
        self.assertTrue(t.preload())
        t.transcribe("x.webm")
        self.assertEqual(seen["beam_size"], 1)


if __name__ == "__main__":
    unittest.main()
