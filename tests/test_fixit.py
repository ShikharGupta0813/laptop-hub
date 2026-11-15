"""Provider fallback chain, actionable messages, Provider check / checklist / log routes, console banner."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import cliprov, consolelog, fixit, providers, provchain
from hub.common import Ctx
from hub.llm import LLMError
from tests.helpers import make_config


class Fake:
    def __init__(self, name, answer="hello there", error=None, chunks=None):
        self.name, self.answer, self.error, self.chunks, self.calls = name, answer, error, chunks, 0

    async def complete(self, system, user, json_mode=False):
        self.calls += 1
        if self.error:
            raise LLMError(self.error)
        return self.answer

    async def stream(self, system, user):
        self.calls += 1
        for piece in self.chunks if self.chunks is not None else [self.answer]:
            if piece is LLMError:
                raise LLMError(self.error or "broke")
            yield piece
        if self.error and self.chunks is None:
            raise LLMError(self.error)


def run(coro):
    return asyncio.run(coro)


async def collect(model, system="s", user="u"):
    return "".join([p async for p in model.stream(system, user)])


class Base(unittest.TestCase):
    """No Gemini key, no Anthropic key, no Claude login. A fake `gemini` program may or may not be installed."""

    def setUp(self):
        self.config, self.root = make_config(default_provider="auto")
        env = mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("hub.providers.gemini_key", ""), ("hub.providers.find_claude_cli", None),
                              ("hub.provchain.providers.gemini_key", "")):
            p = mock.patch(target, lambda *a, _v=value, **k: _v)
            p.start()
            self.addCleanup(p.stop)
        self.installed = {"gemini": True, "agy": True}
        p = mock.patch("hub.cliprov.find_program", lambda word: f"/bin/{word}" if self.installed.get(word) else None)
        p.start()
        self.addCleanup(p.stop)
        self.said = []

    def chain(self, clients, choice="auto", ollama_up=True):
        built = []

        def build(ident):
            built.append(ident)
            return clients.get(ident)
        model = provchain.ChainModel(self.config, choice, build, say=self.said.append, ollama_up=lambda: ollama_up)
        model.built = built
        return model


class DefaultResolution(Base):
    def test_gemini_cli_login_is_the_default_without_any_key(self):
        self.assertEqual(providers.resolve_default(self.config), "cli:agy")

    def test_no_cli_and_no_key_falls_to_local(self):
        self.installed["gemini"] = False; self.installed["agy"] = False
        self.assertEqual(providers.resolve_default(self.config), "local")

    def test_api_key_still_wins_over_cli(self):
        with mock.patch("hub.providers.gemini_key", lambda: "k"), mock.patch("hub.providers.gemini_quota_problem", lambda k=None: ""):
            self.assertEqual(providers.resolve_default(self.config), "gemini")


class Chain(Base):
    def test_uses_gemini_cli_when_gemini_key_missing(self):
        cli, local = Fake("cli"), Fake("local")
        model = self.chain({"cli:agy": cli, "local": local})
        self.assertEqual(run(model.complete("s", "u")), "hello there")
        self.assertEqual((cli.calls, local.calls), (1, 0))
        self.assertTrue(any("answered with Antigravity" in s for s in self.said), self.said)

    def test_stale_gemini_choice_falls_through_to_cli_not_unavailable(self):
        cli = Fake("cli")
        model = self.chain({"cli:agy": cli}, choice="gemini")
        self.assertEqual(run(model.complete("s", "u")), "hello there")
        self.assertTrue(any("Gemini not used" in s for s in self.said), self.said)

    def test_failing_provider_is_skipped_and_next_answers(self):
        cli, local = Fake("cli", error="not logged in: run gemini"), Fake("local", answer="from local")
        model = self.chain({"cli:agy": cli, "local": local})
        self.assertEqual(run(model.complete("s", "u")), "from local")
        self.assertEqual(provchain.LAST["used"], "local")
        self.assertIn("cli:agy", [s["id"] for s in provchain.LAST["skipped"]])

    def test_stream_falls_back_before_first_word(self):
        cli, local = Fake("cli", error="down", chunks=[LLMError]), Fake("local", chunks=["a", "b"])
        model = self.chain({"cli:agy": cli, "local": local})
        self.assertEqual(run(collect(model)), "ab")

    def test_stream_never_swaps_a_half_answer(self):
        cli, local = Fake("cli", error="cut", chunks=["half", LLMError]), Fake("local")
        model = self.chain({"cli:agy": cli, "local": local})
        with self.assertRaises(LLMError):
            run(collect(model))
        self.assertEqual(local.calls, 0)

    def test_local_choice_never_leaves_the_laptop(self):
        cli, local = Fake("cli"), Fake("local", error="Ollama isn't running.")
        model = self.chain({"cli:agy": cli, "local": local}, choice="local")
        with self.assertRaises(LLMError):
            run(model.complete("s", "u"))
        self.assertEqual(cli.calls, 0)

    def test_everything_down_gives_an_actionable_message(self):
        self.installed["gemini"] = False; self.installed["agy"] = False
        model = self.chain({"local": Fake("local", error="Ollama isn't running. Start the Ollama app.")}, ollama_up=False)
        with self.assertRaises(LLMError) as ctx:
            run(model.complete("s", "u"))
        text = str(ctx.exception)
        self.assertNotEqual(text.strip().lower(), "gemini unavailable")
        for needle in ("python -m hub setup --only gemini", "gemini", "Ollama", "Provider check", "Antigravity"):
            self.assertIn(needle, text)
        self.assertTrue(any(s.startswith("AI: nothing could answer") for s in self.said))

    def test_not_logged_in_cli_names_the_exact_command(self):
        cliprov.note_result("agy", False, cliprov.GEMINI_HINT, {"login_hint": cliprov.GEMINI_HINT})
        self.addCleanup(cliprov.LAST.clear)
        rows = {r["id"]: r for r in provchain.diagnose(self.config, True)}
        row = rows["cli:agy"]
        self.assertFalse(row["ready"])
        self.assertIn("sign in", row["fix"])
        self.assertEqual(row["command"], "agy")
        self.assertEqual(row["action"]["kind"], "test")
        self.assertEqual(row["action"]["test_path"], "/api/cli-providers/agy/test")


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class Routes(Base):
    def build(self, up=True, jarvis_dir=""):
        self.config.jarvis_dir = jarvis_dir
        ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: None)
        self.raw = {}
        ctx.raw_model_for = lambda ident: self.raw[ident]

        async def probe(config):
            return ["qwen3:4b"] if up else None
        app = FastAPI()
        app.include_router(fixit.make_router(ctx, probe=probe))
        client = TestClient(app)
        client.cookies.set("hub_session", "yes")
        self.addCleanup(client.close)
        return client

    def test_needs_login(self):
        client = self.build()
        client.cookies.clear()
        self.assertEqual(client.get("/api/fixit/providers").status_code, 401)

    def test_providers_panel_rows_and_summary(self):
        data = self.build().get("/api/fixit/providers").json()
        ids = [p["id"] for p in data["providers"]]
        self.assertEqual(ids[0], "cli:agy")
        self.assertIn("local", ids)
        self.assertTrue(data["providers"][0]["chosen"])
        self.assertIn("Antigravity", data["summary"])

    def test_ollama_down_and_nothing_else_is_reported_with_fix(self):
        self.installed["gemini"] = False; self.installed["agy"] = False
        data = self.build(up=False).get("/api/fixit/providers").json()
        self.assertFalse(any(p["chosen"] for p in data["providers"]))
        local = next(p for p in data["providers"] if p["id"] == "local")
        self.assertIn("Ollama", local["fix"])
        self.assertIn("python -m hub setup --only gemini", data["summary"])

    def test_provider_test_hits_only_that_provider(self):
        client = self.build()
        self.raw["cli:agy"] = Fake("cli", answer="ok")
        r = client.post("/api/fixit/providers/cli:agy/test").json()
        self.assertTrue(r["ok"], r)
        self.raw["local"] = Fake("local", error="Ollama isn't running.")
        r = client.post("/api/fixit/providers/local/test").json()
        self.assertFalse(r["ok"])
        self.assertIn("Ollama", r["error"])
        self.assertIn("ollama", r["fix"].lower())

    def test_provider_test_of_unready_provider_explains_without_calling(self):
        client = self.build()
        r = client.post("/api/fixit/providers/gemini/test").json()
        self.assertFalse(r["ok"])
        self.assertIn("setup --only gemini", r["command"])

    def test_checklist_names_each_missing_piece(self):
        client = self.build(jarvis_dir="")
        items = {i["id"]: i for i in client.get("/api/fixit/checklist").json()["items"]}
        self.assertFalse(items["jarvis-dir"]["ok"])
        self.assertIn("jarvis_dir", items["jarvis-dir"]["step"])
        folder = Path(tempfile.mkdtemp(prefix="jv-"))
        client = self.build(jarvis_dir=str(folder))
        items = {i["id"]: i for i in client.get("/api/fixit/checklist").json()["items"]}
        self.assertFalse(items["jarvis-script"]["ok"])
        (folder / "jarvis.py").write_text("print(1)")
        items = {i["id"]: i for i in client.get("/api/fixit/checklist").json()["items"]}
        self.assertTrue(items["jarvis-script"]["ok"])
        self.assertFalse(items["jarvis-venv"]["ok"])
        self.assertIn("venv", items["jarvis-venv"]["step"])
        self.assertFalse(items["jarvis-inbox"]["ok"])
        self.assertIn("jarvis-remote-inbox.md", items["jarvis-inbox"]["step"])
        (folder / "remote_inbox.py").write_text("")
        items = {i["id"]: i for i in client.get("/api/fixit/checklist").json()["items"]}
        self.assertTrue(items["jarvis-inbox"]["ok"])

    def test_no_provider_checklist_item(self):
        self.installed["gemini"] = False; self.installed["agy"] = False
        items = {i["id"]: i for i in self.build(up=False).get("/api/fixit/checklist").json()["items"]}
        self.assertFalse(items["provider"]["ok"])
        self.assertIn("Ollama", items["provider"]["step"])

    def test_log_sources_and_masking(self):
        folder = Path(tempfile.mkdtemp(prefix="jv-"))
        (folder / "jarvis.log").write_text("one\ntwo\napi_key=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n")
        client = self.build(jarvis_dir=str(folder))
        data = client.get("/api/fixit/log?source=jarvis&lines=2").json()
        self.assertEqual(len(data["lines"]), 2)
        self.assertNotIn("ABCDEFGHIJKLMNOP", " ".join(data["lines"]))
        self.assertFalse(client.get("/api/fixit/log?source=hub").json()["exists"])
        consolelog.enable(self.config.data_path() / consolelog.FILE_NAME, echo=False)
        self.addCleanup(consolelog.disable)
        consolelog.say("provider chosen: Local model")
        hub = client.get("/api/fixit/log?source=hub").json()
        self.assertTrue(hub["exists"])
        self.assertIn("provider chosen", hub["lines"][-1])
        self.assertEqual(client.get("/api/fixit/log?source=nope").status_code, 400)

    def test_doctor_lines(self):
        lines = self.build().get("/api/fixit/doctor").json()["lines"]
        text = "\n".join(l["text"] for l in lines)
        self.assertIn("Ollama running", text)
        self.assertIn("Antigravity", text)


class ServerFallback(Base):
    """End to end through hub.server.model_for: a stale 'gemini' pick (no key) and 'auto' still get an answer."""

    def ask(self, provider):
        from hub import auth  # noqa: F401
        from hub.devserver import FakeLLM
        from hub.server import Deps, create_app
        from tests.helpers import TOKEN
        deps = Deps(llm=FakeLLM(), transcriber=object())
        client = TestClient(create_app(self.config, deps, audit_path=self.root / "audit.log"),
                            base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(client.close)
        self.assertEqual(client.post("/api/login", json={"token": TOKEN}).status_code, 200)
        self.installed["gemini"] = False; self.installed["agy"] = False
        r = client.post("/api/ask", json={"mode": "explain", "text": "hello world", "provider": provider})
        import json as _json
        pieces = [_json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
        return "".join(p.get("t", "") + (" ERROR: " + p["error"] if "error" in p else "") for p in pieces)

    def test_stale_gemini_pick_answers_from_the_local_model(self):
        text = self.ask("gemini")
        self.assertIn("pretend answer", text)
        self.assertNotIn("ERROR", text)

    def test_auto_answers_from_the_local_model_when_nothing_else_is_ready(self):
        self.config.default_provider = "auto"
        self.assertIn("pretend answer", self.ask("auto"))


class Banner(unittest.TestCase):
    def test_banner_lists_url_login_voice_providers_features(self):
        config, _ = make_config()
        rows = [{"label": "Local model", "ready": True, "reason": ""},
                {"label": "Gemini", "ready": False, "reason": "No Gemini key."}]
        text = "\n".join(consolelog.banner_lines(config, "hub.json", tts_engine="piper", provider_rows=rows,
                                                  default_provider="Local model"))
        for needle in ("URL:", str(config.port), "Login:", "piper", "AI ready:   Local model", "AI missing: Gemini", "Features:"):
            self.assertIn(needle, text)

    def test_banner_says_none_ready_with_fix(self):
        config, _ = make_config()
        text = "\n".join(consolelog.banner_lines(config, None, provider_rows=[{"label": "X", "ready": False, "reason": "no"}]))
        self.assertIn("NONE", text)
        self.assertIn("-> fix", text)

    def test_say_is_silent_until_enabled_and_never_raises(self):
        consolelog.disable()
        consolelog.say("x")
        path = Path(tempfile.mkdtemp()) / "c.log"
        consolelog.enable(path, echo=False)
        self.addCleanup(consolelog.disable)
        consolelog.say("hello")
        self.assertIn("hello", path.read_text())


if __name__ == "__main__":
    unittest.main()
