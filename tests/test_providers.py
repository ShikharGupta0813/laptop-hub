"""Claude through the `claude` login, Gemini, and which provider is the default. No real CLI or network."""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from hub import llm, providers
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config
from tests.test_assistant import Base as AssistantBase, collect, of

FAKE_CLI = textwrap.dedent('''
    import json, os, sys, time
    mode = os.environ.get("FAKE_MODE", "echo")
    data = sys.stdin.read()
    args = sys.argv[1:]
    if mode == "unknown" and "--tools" in args:
        sys.stderr.write("error: unknown option '--tools'\\n"); sys.exit(1)
    if mode == "notlogged":
        print("Invalid API key \\u00b7 Please run /login"); sys.exit(1)
    if mode == "limit":
        sys.stderr.write("Claude AI usage limit reached|1700000000\\n"); sys.exit(1)
    if mode == "boom":
        sys.stderr.write("something odd happened\\n"); sys.exit(2)
    if mode == "hang":
        time.sleep(30)
    if mode == "chunks":
        for part in ["Hello there, friend. "] + ["word " * 20] * 6:
            sys.stdout.write(part); sys.stdout.flush(); time.sleep(0.05)
        sys.exit(0)
    print(json.dumps({"args": args, "stdin": data, "cwd": os.getcwd(),
                      "has_key": "ANTHROPIC_API_KEY" in os.environ,
                      "has_token": "ANTHROPIC_AUTH_TOKEN" in os.environ}) + " " + "x" * 200)
''')


class CliTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.script = Path(tmp.name) / "fake_claude.py"
        self.script.write_text(FAKE_CLI, encoding="utf-8")

    def client(self, mode="echo", **kw):
        env = dict(os.environ, FAKE_MODE=mode, ANTHROPIC_API_KEY="sk-ant-should-not-reach-child",
                   ANTHROPIC_AUTH_TOKEN="tok")
        kw.setdefault("timeout", 20)
        return llm.ClaudeCliClient("sonnet", command=[sys.executable, str(self.script)], env=env, **kw)

    async def echo(self, system="Be brief.", user="hi", **kw):
        text = await self.client(**kw).complete(system, user)
        return json.loads(text[:text.rindex("}") + 1])

    async def test_asks_for_text_only_with_tools_off_and_user_text_on_stdin(self):
        got = await self.echo("Be brief.", "what is 2+2?")
        args = got["args"]
        self.assertEqual(args[:3], ["-p", "--output-format", "text"])
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[args.index("--system-prompt") + 1], "Be brief.")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(got["stdin"], "what is 2+2?")

    async def test_runs_in_a_temp_dir_and_strips_the_api_key(self):
        got = await self.echo()
        self.assertFalse(got["has_key"])
        self.assertFalse(got["has_token"])
        self.assertIn("hub-claude-", got["cwd"])
        self.assertFalse(Path(got["cwd"]).exists())   # cleaned up afterwards

    async def test_a_long_system_prompt_goes_on_stdin(self):
        got = await self.echo("S" * 5000, "question")
        args = got["args"]
        self.assertEqual(args[args.index("--system-prompt") + 1], llm.CLI_BASE_SYSTEM)
        self.assertIn("S" * 5000, got["stdin"])
        self.assertTrue(got["stdin"].endswith("question"))

    async def test_json_mode_asks_for_json(self):
        text = await self.client().complete("s", "u", json_mode=True)
        self.assertIn("JSON only", json.loads(text[:text.rindex("}") + 1])["stdin"])

    async def test_stream_yields_pieces(self):
        pieces = [p async for p in self.client("chunks").stream("s", "u")]
        self.assertGreater(len(pieces), 1)
        self.assertTrue("".join(pieces).startswith("Hello there, friend."))

    async def test_not_logged_in_is_a_friendly_error(self):
        with self.assertRaises(llm.LLMError) as cm:
            await self.client("notlogged").complete("s", "u")
        self.assertIn("run `claude`", str(cm.exception))
        self.assertNotIn("Invalid API key", str(cm.exception))

    async def test_usage_limit_is_a_friendly_error(self):
        with self.assertRaises(llm.LLMError) as cm:
            await self.client("limit").complete("s", "u")
        self.assertIn("usage limit", str(cm.exception))

    async def test_other_failures_show_the_first_line(self):
        with self.assertRaises(llm.LLMError) as cm:
            await self.client("boom").complete("s", "u")
        self.assertIn("something odd happened", str(cm.exception))

    async def test_an_older_cli_without_a_flag_is_retried_with_fewer_flags(self):
        text = await self.client("unknown").complete("s", "u")
        self.assertNotIn("--tools", json.loads(text[:text.rindex("}") + 1])["args"])

    async def test_timeout_kills_the_program(self):
        with self.assertRaises(llm.LLMError) as cm:
            await self.client("hang", timeout=0.7).complete("s", "u")
        self.assertIn("too long", str(cm.exception))

    async def test_missing_program_says_how_to_install(self):
        client = llm.ClaudeCliClient("sonnet", command=["/no/such/claude-program"])
        with self.assertRaises(llm.LLMError) as cm:
            await client.complete("s", "u")
        self.assertIn("Install Claude Code", str(cm.exception))
        with mock.patch.object(llm, "find_claude_cli", return_value=None):
            with self.assertRaises(llm.LLMError) as cm:
                await llm.ClaudeCliClient("sonnet").complete("s", "u")
        self.assertIn("run `claude` once", str(cm.exception))

    def test_name(self):
        self.assertEqual(llm.ClaudeCliClient().name, "claude-login")


def sse(*texts):
    return "".join("data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": t}]}}]}) + "\r\n\r\n"
                   for t in texts)


class GeminiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        llm.reset_gemini_state()

    def client(self, handler, key="KEY123", **kw):
        return llm.GeminiClient("gemini-2.5-flash", api_key=key, transport=httpx.MockTransport(handler), **kw)

    async def test_stream_uses_sse_and_sends_the_key_in_a_header(self):
        seen = {}

        def handler(request):
            seen["url"], seen["key"] = str(request.url), request.headers.get("x-goog-api-key")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, text=sse("Hel", "lo"), headers={"content-type": "text/event-stream"})

        pieces = [p async for p in self.client(handler).stream("be brief", "hi")]
        self.assertEqual(pieces, ["Hel", "lo"])
        self.assertIn("gemini-2.5-flash:streamGenerateContent", seen["url"])
        self.assertIn("alt=sse", seen["url"])
        self.assertNotIn("KEY123", seen["url"])
        self.assertEqual(seen["key"], "KEY123")
        self.assertEqual(seen["body"]["systemInstruction"]["parts"][0]["text"], "be brief")
        self.assertEqual(seen["body"]["contents"][0]["parts"][0]["text"], "hi")

    async def test_complete_and_json_mode(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"text": "thinking", "thought": True}, {"text": ' {"a": 1} '}]}}]})

        out = await self.client(handler).complete("s", "u", json_mode=True)
        self.assertEqual(out, '{"a": 1}')
        self.assertEqual(bodies[0]["generationConfig"]["responseMimeType"], "application/json")

    async def test_bad_key(self):
        def handler(request):
            return httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}})
        for call in (lambda c: c.complete("s", "u"), lambda c: _drain(c.stream("s", "u"))):
            with self.assertRaises(llm.LLMError) as cm:
                await call(self.client(handler))
            self.assertIn("key was rejected", str(cm.exception))

    async def test_quota(self):
        def handler(request):
            return httpx.Response(429, json={"error": {"message": "quota"}})
        with self.assertRaises(llm.LLMError) as cm:
            await self.client(handler).complete("s", "u")
        self.assertIn("quota", str(cm.exception))
        with self.assertRaises(llm.LLMError) as cm:
            await _drain(self.client(handler).stream("s", "u"))
        self.assertIn("quota", str(cm.exception))

    async def test_network_down(self):
        def handler(request):
            raise httpx.ConnectError("down")
        with self.assertRaises(llm.LLMError) as cm:
            await self.client(handler).complete("s", "u")
        self.assertIn("internet", str(cm.exception))

    async def test_blocked_prompt(self):
        def handler(request):
            return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        with self.assertRaises(llm.LLMError):
            await self.client(handler).complete("s", "u")

    async def test_no_key_says_where_to_get_one(self):
        client = self.client(lambda r: httpx.Response(200, json={}), key=None, key_loader=lambda: "")
        with self.assertRaises(llm.LLMError) as cm:
            await client.complete("s", "u")
        self.assertIn("aistudio.google.com/apikey", str(cm.exception))

    def test_key_lookup_prefers_the_environment_then_the_keyring(self):
        llm.forget_gemini_key_cache()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}):
            self.assertEqual(llm.gemini_key(), "from-env")
        fake = mock.Mock()
        fake.get_password.return_value = "from-keyring"
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(sys.modules, {"keyring": fake}):
            llm.forget_gemini_key_cache()
            self.assertEqual(llm.gemini_key(), "from-keyring")
            fake.get_password.assert_called_with("jarvis-gemini", "api_key")
        llm.forget_gemini_key_cache()


    async def test_a_retired_model_falls_back_to_a_current_flash_model(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path.endswith("/models") and request.method == "GET":
                return httpx.Response(200, json={"models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3-pro", "supportedGenerationMethods": ["generateContent"]}]})
            if "gemini-3-flash:" in request.url.path:
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
            return httpx.Response(404, json={"error": {"message": "not found"}})

        client = self.client(handler, "KEY")
        self.assertEqual(await client.complete("s", "u"), "hi")
        self.assertEqual(client.model, "gemini-3-flash")


LIMIT0 = {"error": {"code": 429, "message": "You exceeded your current quota. * Quota exceeded for metric: "
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, model: x"}}


def _model(name, methods=("generateContent",)):
    return {"name": f"models/{name}", "supportedGenerationMethods": list(methods)}


class GeminiPickerTest(unittest.TestCase):
    def test_ranking_drops_codenames_previews_and_non_text_models(self):
        models = [_model(n) for n in (
            "gemini-4-argon", "gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.0-flash-exp", "gemini-3-flash",
            "gemini-2.5-flash-image", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3-pro", "gemini-2.5-flash-tts",
            "gemini-2.5-flash-thinking", "gemini-live-2.5-flash", "gemini-robotics-er-1.5-flash",
            "gemini-embedding-001", "gemini-2.5-flash-latest")]
        models.append(_model("gemini-9-flash", ["embedContent"]))
        order = llm.rank_gemini_models(models, "gemini-2.5-flash")
        self.assertEqual(order, ["gemini-3-flash", "gemini-2.5-flash", "gemini-2.5-flash-latest",
                                 "gemini-2.5-flash-lite", "gemini-3-pro", "gemini-2.5-pro"])
        self.assertNotIn("gemini-4-argon", order)

    def test_a_pro_configured_model_ranks_pro_first(self):
        order = llm.rank_gemini_models([_model(n) for n in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro")],
                                       "gemini-2.5-pro")
        self.assertEqual(order, ["gemini-3-pro", "gemini-2.5-pro", "gemini-2.5-flash"])

    def test_which_429s_mean_no_quota(self):
        self.assertTrue(llm.gemini_no_quota(429, json.dumps(LIMIT0)))
        self.assertTrue(llm.gemini_no_quota(429, "GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
        self.assertFalse(llm.gemini_no_quota(429, "slow down, per minute"))
        self.assertFalse(llm.gemini_no_quota(500, "limit: 0"))


class GeminiFailoverTest(unittest.IsolatedAsyncioTestCase):
    NAMES = ["gemini-4-argon", "gemini-2.5-flash", "gemini-3-flash", "gemini-2.5-flash-lite"]

    def setUp(self):
        llm.reset_gemini_state()
        self.addCleanup(llm.reset_gemini_state)
        self.asked = []

    def client(self, behaviour, model="gemini-2.5-flash"):
        def handler(request):
            if request.url.path.endswith("/models") and request.method == "GET":
                return httpx.Response(200, json={"models": [_model(n) for n in self.NAMES]})
            name = request.url.path.split("/")[-1].split(":")[0]
            self.asked.append(name)
            result = behaviour(name)
            if result == "ok":
                if "stream" in request.url.path:
                    return httpx.Response(200, text=sse("fine"), headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "fine"}]}}]})
            return httpx.Response(result[0], **result[1])
        return llm.GeminiClient(model, api_key="K1", transport=httpx.MockTransport(handler))

    @staticmethod
    def a_dead_b_good(name):
        return (429, {"json": LIMIT0}) if name == "gemini-2.5-flash" else (
            "ok" if name == "gemini-3-flash" else (404, {"json": {}}))

    async def test_limit_zero_on_one_model_moves_to_the_next_complete(self):
        c = self.client(self.a_dead_b_good)
        self.assertEqual(await c.complete("s", "u"), "fine")
        self.assertEqual(c.model, "gemini-3-flash")
        self.assertNotIn("gemini-4-argon", self.asked)
        self.asked.clear()
        self.assertEqual(await c.complete("s", "u"), "fine")
        self.assertEqual(self.asked, ["gemini-3-flash"])        # the good one is kept

    async def test_limit_zero_on_one_model_moves_to_the_next_stream(self):
        c = self.client(self.a_dead_b_good)
        self.assertEqual(await _drain(c.stream("s", "u")), ["fine"])
        self.assertEqual(c.model, "gemini-3-flash")

    async def test_a_failed_model_is_remembered_by_a_new_client_today(self):
        await self.client(self.a_dead_b_good).complete("s", "u")
        self.asked.clear()
        c = self.client(self.a_dead_b_good)
        self.assertEqual(await c.complete("s", "u"), "fine")
        self.assertEqual(self.asked, ["gemini-3-flash"])        # never asked the dead one again

    async def test_all_exhausted_gives_the_options_and_sets_the_reason(self):
        c = self.client(lambda name: (429, {"json": LIMIT0}))
        for call in (lambda: c.complete("s", "u"), lambda: _drain(c.stream("s", "u"))):
            llm.reset_gemini_state()
            with self.assertRaises(llm.LLMError) as cm:
                await call()
            msg = str(cm.exception)
            self.assertIn("free tier has no quota for the available models", msg)
            self.assertIn("aistudio.google.com/apikey", msg)
            self.assertIn("does not include API quota", msg)
            self.assertIn("Claude login / a local model", msg)
        self.assertEqual(llm.gemini_quota_problem("K1"), llm.GEMINI_NO_QUOTA_REASON)
        self.assertEqual(llm.gemini_quota_problem("another key"), "")

    async def test_a_plain_per_minute_429_does_not_switch_models(self):
        c = self.client(lambda name: (429, {"json": {"error": {"message": "try later"}}}))
        with self.assertRaises(llm.LLMError) as cm:
            await c.complete("s", "u")
        self.assertIn("free quota is used up", str(cm.exception))
        self.assertEqual(self.asked, ["gemini-2.5-flash"])

    async def test_reason_expires_after_an_hour(self):
        llm.note_gemini_quota_failure("K1")
        with mock.patch.object(llm.time, "time", return_value=llm.time.time() + llm.GEMINI_REASON_TTL + 5):
            self.assertEqual(llm.gemini_quota_problem("K1"), "")

    async def test_stream_walks_past_a_missing_model_and_a_dead_one(self):
        def behaviour(name):
            return {"gemini-2.5-flash": (404, {"json": {}}), "gemini-3-flash": (429, {"json": LIMIT0})}.get(name, "ok")
        c = self.client(behaviour)
        self.assertEqual(await _drain(c.stream("s", "u")), ["fine"])
        self.assertEqual(self.asked[:3], ["gemini-2.5-flash", "gemini-3-flash", "gemini-2.5-flash-lite"])
        self.assertNotIn("gemini-4-argon", self.asked)

    async def test_success_clears_the_reason(self):
        llm.note_gemini_quota_failure("K1")
        await self.client(lambda name: "ok").complete("s", "u")
        self.assertEqual(llm.gemini_quota_problem("K1"), "")


class GeminiReasonTest(unittest.TestCase):
    def test_availability_explains_limit_zero_and_hides_gemini_and_duo(self):
        llm.reset_gemini_state()
        self.addCleanup(llm.reset_gemini_state)
        config, _ = make_config(default_provider="auto")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(providers, "gemini_key", return_value="K9"), \
                mock.patch.object(providers, "find_claude_cli", return_value="/bin/claude"):
            self.assertTrue(providers.availability(config)["gemini"]["ready"])
            llm.note_gemini_quota_failure("K9")
            a = providers.availability(config)
            self.assertFalse(a["gemini"]["ready"])
            self.assertEqual(a["gemini"]["reason"], "key found \u2014 no free quota on this key (limit 0)")
            self.assertFalse(a["duo"]["ready"])
            self.assertIn("no free quota", a["duo"]["reason"])
            self.assertEqual(providers.resolve_default(config), "claude-login")   # falls back


async def _drain(gen):
    return [p async for p in gen]


class DefaultProviderTest(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(default_provider="auto")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.cli = mock.patch.object(providers, "find_claude_cli", return_value=None)
        self.gem = mock.patch.object(providers, "gemini_key", return_value="")
        self.cli_mock, self.gem_mock = self.cli.start(), self.gem.start()
        self.addCleanup(self.cli.stop)
        self.addCleanup(self.gem.stop)

    def test_nothing_set_up_means_local(self):
        self.assertEqual(providers.resolve_default(self.config), "local")

    def test_order_is_api_key_then_gemini_then_login_then_local(self):
        self.cli_mock.return_value = "/usr/bin/claude"
        self.assertEqual(providers.resolve_default(self.config), "claude-login")
        self.gem_mock.return_value = "key"
        self.assertEqual(providers.resolve_default(self.config), "gemini")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk"}):
            self.assertEqual(providers.resolve_default(self.config), "claude")

    def test_an_explicit_default_wins(self):
        self.config.default_provider = "claude-login"
        self.assertEqual(providers.resolve_default(self.config), "claude-login")
        self.config.default_provider = "nonsense"
        self.assertEqual(providers.resolve_default(self.config), "local")

    def test_resolve_treats_blank_and_auto_as_default(self):
        self.gem_mock.return_value = "key"
        for choice in ("", None, "auto", "AUTO"):
            self.assertEqual(providers.resolve(self.config, choice), "gemini")
        self.assertEqual(providers.resolve(self.config, "local"), "local")

    def test_availability_has_reasons(self):
        self.cli_mock.return_value = "/usr/bin/claude"
        a = providers.availability(self.config)
        self.assertTrue(a["claude_login"]["ready"])
        self.assertFalse(a["claude"]["ready"])
        self.assertFalse(a["gemini"]["ready"])
        self.assertIn("setup --only gemini", a["gemini"]["reason"])
        self.assertFalse(providers.availability(self.config, ollama_up=False)["local"]["ready"])


class Tagged:
    def __init__(self, name):
        self.name = name

    async def models(self):
        return []

    async def stream(self, system, user):
        yield self.name

    async def complete(self, system, user, json_mode=False):
        return self.name


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config(default_provider="auto")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        for target, value in (("find_claude_cli", None), ("gemini_key", "")):
            m = mock.patch.object(providers, target, return_value=value)
            m.start()
            self.addCleanup(m.stop)
        self.providers_patch = providers
        deps = Deps(llm=Tagged("local"), claude=Tagged("claude"), claude_login=Tagged("claude-login"),
                    gemini=Tagged("gemini"), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        self.client = TestClient(create_app(self.config, deps, audit_path=self.root / "audit.log"),
                                 base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)

    def ask(self, provider):
        r = self.client.post("/api/ask", json={"mode": "custom", "question": "hi", "provider": provider})
        return "".join(json.loads(line[6:]).get("t", "") for line in r.text.splitlines()
                       if line.startswith("data: {"))

    def test_each_provider_name_reaches_its_client(self):
        for name in ("local", "claude", "claude-login", "gemini"):
            self.assertEqual(self.ask(name), name)

    def test_auto_and_blank_use_the_default(self):
        self.config.router_enabled = False     # the router (tests/test_router.py) would send "hi" to the local model
        self.assertEqual(self.ask("auto"), "local")
        with mock.patch.object(providers, "gemini_key", return_value="k"):
            self.assertEqual(self.ask("auto"), "gemini")
            self.assertEqual(self.ask(""), "gemini")

    def test_status_lists_providers_and_default(self):
        s = self.client.get("/api/status").json()
        self.assertEqual(set(s["providers"]), {"local", "claude", "claude_login", "gemini", "duo", "router"})
        self.assertEqual(s["default_provider"], "local")
        self.assertFalse(s["claude_ready"])
        with mock.patch.object(providers, "find_claude_cli", return_value="/bin/claude"):
            s = self.client.get("/api/status").json()
        self.assertTrue(s["providers"]["claude_login"]["ready"])
        self.assertTrue(s["claude_ready"])
        self.assertEqual(s["default_provider"], "claude-login")

    def test_status_shows_no_free_quota_reason_for_the_phone(self):
        llm.reset_gemini_state()
        self.addCleanup(llm.reset_gemini_state)
        with mock.patch.object(providers, "gemini_key", return_value="KQ"), \
                mock.patch.object(providers, "find_claude_cli", return_value="/bin/claude"):
            s = self.client.get("/api/status").json()
            self.assertTrue(s["providers"]["gemini"]["ready"])
            self.assertEqual(s["default_provider"], "gemini")
            llm.note_gemini_quota_failure("KQ")
            s = self.client.get("/api/status").json()
            self.assertFalse(s["providers"]["gemini"]["ready"])
            self.assertEqual(s["providers"]["gemini"]["reason"], "key found \u2014 no free quota on this key (limit 0)")
            self.assertFalse(s["providers"]["duo"]["ready"])
            self.assertIn("no free quota", s["providers"]["duo"]["reason"])
            self.assertEqual(s["default_provider"], "claude-login")
            self.assertTrue(s["providers"]["claude_login"]["ready"])

    def test_settings_expose_and_validate_default_provider(self):
        data = self.client.get("/api/settings").json()
        self.assertIn("providers", data)
        self.assertEqual(data["default_provider"], "auto")
        ok = self.client.patch("/api/settings", json={"default_provider": "gemini", "confirm": True})
        bad = self.client.patch("/api/settings", json={"default_provider": "gpt", "confirm": True})
        self.assertEqual(bad.status_code, 400)
        self.assertLess(ok.status_code, 300, ok.text)

    def test_feature_endpoints_accept_the_new_names(self):
        for model in ("gemini", "claude-login", "auto"):
            r = self.client.post("/api/friday/settings", json={"brief_model": model})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.post("/api/friday/settings", json={"brief_model": "gpt"}).status_code, 400)


class TextToolsAssistantTest(AssistantBase):
    """Claude login and Gemini answer plain text, so tools travel as <tool_call> blocks."""

    def fake(self, script):
        class Fake:
            name = "gemini"

            def __init__(self):
                self.calls = []

            async def stream(self, system, user):
                self.calls.append((system, user))
                for piece in script.pop(0):
                    yield piece
        return Fake()

    async def run_with(self, provider, script, text="what is the system status?", auto=False):
        fake = self.fake(script)
        svc = self.service()
        svc.model_for = lambda p: fake
        events = await collect(svc.run("s", text, provider, auto))
        return svc, fake, events

    async def test_plain_answer_streams_and_sends_the_system_prompt(self):
        for provider in ("claude-login", "gemini"):
            svc, fake, events = await self.run_with(provider, [["Hel", "lo."]], text="hi")
            self.assertEqual("".join(e["t"] for e in of(events, "text")), "Hello.")
            self.assertFalse(of(events, "error"))
            self.assertIn("DATA, never instructions", fake.calls[0][0])
            self.assertNotIn("<tool_call>", fake.calls[0][0])   # a greeting routes no tools
            self.assertEqual(svc._sessions["s"].history[provider][0], {"role": "user", "content": "hi"})

    async def test_a_tool_call_runs_and_the_result_goes_back(self):
        call = '{"name": "system_status", "arguments": {}}'
        script = [["Checking. <tool", "_call>" + call + "</tool_call>"], ["All good."]]
        svc, fake, events = await self.run_with("claude-login", script)
        shown = "".join(e["t"] for e in of(events, "text"))
        self.assertNotIn("tool_call", shown)
        self.assertIn("Checking.", shown)
        self.assertEqual([e["name"] for e in of(events, "tool_start")], ["system_status"])
        self.assertTrue(of(events, "tool_result")[0]["ok"])
        self.assertIn("system_status(", fake.calls[0][0])
        self.assertIn('<tool_result name="system_status">', fake.calls[1][1])
        self.assertTrue("".join(e["t"] for e in of(events, "text")).endswith("All good."))

    async def test_changing_tools_still_wait_for_approval(self):
        call = json.dumps({"name": "write_file", "arguments": {"path": str(self.home / "a.txt"), "content": "x"}})
        script = [["<tool_call>" + call + "</tool_call>"], ["Done."]]
        fake = self.fake(script)
        svc = self.service()
        svc.model_for = lambda p: fake
        events = await collect(svc.run("s", "write a file a.txt", "gemini", False),
                               on_approval=lambda e: svc.resolve("s", e["id"], False))
        self.assertEqual(len(of(events, "approval_needed")), 1)
        self.assertFalse((self.home / "a.txt").exists())
        self.assertIn("declined", of(events, "tool_result")[0]["output"].lower())

    async def test_unreadable_tool_call_is_reported_to_the_model_not_run(self):
        script = [["<tool_call>{not json</tool_call>"], ["Sorry."]]
        svc, fake, events = await self.run_with("gemini", script)
        self.assertEqual(of(events, "tool_start"), [])
        self.assertIn("not valid JSON", fake.calls[1][1])

    async def test_provider_errors_become_error_events_and_roll_back(self):
        class Broken:
            async def stream(self, system, user):
                raise llm.LLMError("Gemini's free quota is used up for now.")
                yield ""
        svc = self.service()
        svc.model_for = lambda p: Broken()
        events = await collect(svc.run("s", "hi", "gemini", False))
        self.assertIn("quota", of(events, "error")[0]["message"])
        self.assertEqual(svc._sessions["s"].history["gemini"], [])

    async def test_without_a_client_factory_it_says_so(self):
        svc = self.service()
        events = await collect(svc.run("s", "hi", "gemini", False))
        self.assertTrue(of(events, "error"))

    async def test_claude_api_path_is_unchanged(self):
        from tests.fakes_assistant import reply
        svc = self.service([reply("Hi from the API.")])
        events = await collect(svc.run("s", "hi", "claude", False))
        self.assertEqual("".join(e["t"] for e in of(events, "text")), "Hi from the API.")


if __name__ == "__main__":
    unittest.main()
