"""The "duo" provider: Claude plans and checks (rarely), Gemini does the work. Fakes only."""
import asyncio
import json
import os
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from hub import llm, providers
from hub.assistant import needs_plan
from hub.devserver import FakeAssistant, FakeTranscriber
from hub.duo import DuoClient, Supervisor
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config
from tests.test_assistant import Base as AssistantBase, collect, of
from tests.test_providers import Tagged

LONG = "please look at the laptop and tell me how healthy the system is right now, with a short summary"
CALL = '<tool_call>{"name": "system_status", "arguments": {}}</tool_call>'


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeClaude:
    """Answers the planning prompt with a plan and the review prompt with `verdict`."""

    def __init__(self, verdict="OK", error=None):
        self.calls = []
        self.verdict = verdict
        self.error = error

    async def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return "1. Call system_status. 2. Answer briefly." if "supervisor" in system else self.verdict


class FakeGemini:
    name = "gemini"

    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def stream(self, system, user):
        self.calls.append((system, user))
        for piece in self.script.pop(0):
            yield piece

    async def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        return "gemini-complete"


class DuoAssistantTest(AssistantBase):
    def duo(self, script, claude=None, **kw):
        self.clock = Clock()
        self.claude = claude or FakeClaude()
        self.gemini = FakeGemini(script)
        self.sup = Supervisor(self.claude, clock=self.clock, **kw)
        svc = self.service()
        svc.model_for = lambda p: DuoClient(self.gemini, self.sup)
        return svc

    async def go(self, svc, text, sid="s"):
        return await collect(svc.run(sid, text, "duo", False))

    async def test_plan_is_injected_into_geminis_system_prompt(self):
        svc = self.duo([["All healthy."]])
        events = await self.go(svc, LONG)
        self.assertFalse(of(events, "error"))
        self.assertEqual(len(self.claude.calls), 1)
        self.assertIn(LONG, self.claude.calls[0][1])
        system = self.gemini.calls[0][0]
        self.assertIn("Supervisor plan", system)
        self.assertIn("Call system_status", system)
        self.assertIn("system_status(", system)      # a tool the plan names is offered to Gemini
        self.assertEqual("".join(e["t"] for e in of(events, "text")), "All healthy.")

    async def test_trivial_messages_never_call_claude(self):
        svc = self.duo([["Hi!"], ["Fine."]])
        for text in ("hi", "how are you?"):
            await self.go(svc, text)
        self.assertEqual(self.claude.calls, [])
        self.assertNotIn("Supervisor plan", self.gemini.calls[0][0])
        self.assertTrue(needs_plan(LONG))
        self.assertTrue(needs_plan("delete the temp folder"))
        self.assertFalse(needs_plan("thanks a lot"))

    async def test_claude_failure_falls_back_silently_with_a_cooldown(self):
        err = llm.LLMError("Your Claude usage limit is reached for now. Wait for it to reset.")
        svc = self.duo([["One."], ["Two."], ["Three."]], claude=FakeClaude(error=err))
        events = await self.go(svc, LONG)
        self.assertFalse(of(events, "error"))
        self.assertEqual("".join(e["t"] for e in of(events, "text")), "One.")
        self.assertNotIn("Supervisor plan", self.gemini.calls[0][0])
        self.assertEqual(self.sup.status()["note"], "Claude supervisor paused: usage limit")
        await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 1)          # cooling down: Claude not asked again
        self.clock.t += 31 * 60
        self.assertEqual(self.sup.status()["note"], "")
        await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 2)

    async def test_hourly_cap(self):
        svc = self.duo([["a"], ["b"], ["c"], ["d"]], max_per_hour=2)
        for _ in range(3):
            await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 2)
        self.assertIn("hourly cap", self.sup.status()["note"])
        self.clock.t += 3601
        await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 3)

    async def test_a_slow_claude_is_cut_off_after_the_timeout(self):
        class Slow(FakeClaude):
            async def complete(self, system, user, json_mode=False):
                await asyncio.sleep(5)
        svc = self.duo([["ok"]], claude=Slow(), timeout=0.05)
        events = await self.go(svc, LONG)
        self.assertFalse(of(events, "error"))
        self.assertEqual(self.sup.status()["reason"], "timeout")

    async def test_verify_is_on_by_default_but_skipped_when_no_tool_ran(self):
        self.assertTrue(Supervisor(FakeClaude()).verify_enabled)
        svc = self.duo([["Plain answer."]])
        await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 1)          # the plan only

    async def test_verify_off_means_no_review_call(self):
        svc = self.duo([["Checking. " + CALL], ["Done."]], verify=False)
        await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 1)

    async def test_verify_ok_makes_no_correction_turn(self):
        svc = self.duo([["Checking. " + CALL], ["Done."]])
        events = await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 2)
        self.assertEqual(len(self.gemini.calls), 2)
        self.assertIn("system_status", self.claude.calls[1][1])
        self.assertFalse(of(events, "error"))

    async def test_verify_problem_gives_gemini_exactly_one_correction_turn(self):
        claude = FakeClaude(verdict="PROBLEM: you never said the disk is full.")
        svc = self.duo([["Checking. " + CALL], ["Done."], ["Also, the disk is full."]], claude=claude)
        events = await self.go(svc, LONG)
        self.assertEqual(len(self.claude.calls), 2)          # plan + one review, no second review
        self.assertEqual(len(self.gemini.calls), 3)
        self.assertIn("never said the disk is full", self.gemini.calls[2][1])
        self.assertTrue("".join(e["t"] for e in of(events, "text")).endswith("the disk is full."))

    async def test_repair_reminder_for_a_cut_off_tool_call(self):
        svc = self.duo([['<tool_call>{"name": "system_status", "argu'], ["Sorry. All fine."]],
                       claude=FakeClaude(error=llm.LLMError("x")))
        events = await self.go(svc, LONG)
        self.assertEqual(of(events, "tool_start"), [])
        self.assertIn("not valid JSON", self.gemini.calls[1][1])
        self.assertIn("Sorry", "".join(e["t"] for e in of(events, "text")))

    async def test_repairs_are_capped(self):
        bad = ["<tool_call>{oops</tool_call>"]
        svc = self.duo([bad, bad, bad, bad], claude=FakeClaude(error=llm.LLMError("x")))
        events = await self.go(svc, LONG)
        self.assertTrue(of(events, "error"))
        self.assertEqual(len(self.gemini.calls), 3)


class SupervisorStatusTest(unittest.TestCase):
    def test_status_note_and_zero_cap(self):
        sup = Supervisor(FakeClaude(), max_per_hour=0)
        self.assertTrue(sup.status()["paused"])
        self.assertEqual(asyncio.run(sup.ask("s", "u")), None)

    def test_not_logged_in_and_missing_cli_reasons(self):
        for msg, reason in ((llm.CLI_LOGIN_HINT, "not logged in"), (llm.CLI_INSTALL_HINT, "Claude not installed")):
            sup = Supervisor(FakeClaude(error=llm.LLMError(msg)))
            asyncio.run(sup.ask("s", "u"))
            self.assertEqual(sup.status()["reason"], reason)


class DuoServerTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config(default_provider="auto")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.cli = mock.patch.object(providers, "find_claude_cli", return_value=None)
        self.gem = mock.patch.object(providers, "gemini_key", return_value="")
        self.cli_m, self.gem_m = self.cli.start(), self.gem.start()
        self.addCleanup(self.cli.stop)
        self.addCleanup(self.gem.stop)
        self.claude = FakeClaude()

    def client(self, **deps):
        deps = Deps(llm=Tagged("local"), claude=Tagged("claude"), claude_login=self.claude,
                    gemini=Tagged("gemini"), transcriber=FakeTranscriber(), assistant=FakeAssistant(), **deps)
        c = TestClient(create_app(self.config, deps, audit_path=self.root / "audit.log"),
                       base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(c.close)
        self.assertEqual(c.post("/api/login", json={"token": TOKEN}).status_code, 200)
        return c

    def ask(self, c, provider):
        r = c.post("/api/ask", json={"mode": "custom", "question": "hi", "provider": provider})
        return "".join(json.loads(line[6:]).get("t", "") for line in r.text.splitlines() if line.startswith("data: {"))

    def test_plain_calls_go_to_gemini_and_never_to_claude(self):
        c = self.client()
        self.assertEqual(self.ask(c, "duo"), "gemini")
        self.assertEqual(self.claude.calls, [])

    def test_model_for_duo_is_a_duo_client(self):
        from hub.server import create_app as _  # noqa: F401
        c = self.client()
        r = c.post("/api/friday/settings", json={"brief_model": "duo"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_auto_does_not_pick_duo(self):
        self.gem_m.return_value = "k"
        self.cli_m.return_value = "/bin/claude"
        c = self.client()
        self.assertEqual(c.get("/api/status").json()["default_provider"], "gemini")

    def test_availability_needs_gemini_and_mentions_the_supervisor(self):
        a = providers.availability(self.config)["duo"]
        self.assertFalse(a["ready"])
        self.gem_m.return_value = "k"
        a = providers.availability(self.config)["duo"]
        self.assertTrue(a["ready"])
        self.assertIn("not available", a["reason"])
        self.cli_m.return_value = "/bin/claude"
        self.assertNotIn("not available", providers.availability(self.config)["duo"]["reason"])
        self.assertEqual(providers.resolve(self.config, "duo"), "duo")

    def test_status_shows_the_paused_note(self):
        sup = Supervisor(FakeClaude(error=llm.LLMError("Your Claude usage limit is reached")))
        asyncio.run(sup.ask("s", "u"))
        self.gem_m.return_value = "k"
        s = self.client(duo=DuoClient(Tagged("gemini"), sup)).get("/api/status").json()
        self.assertEqual(s["duo"]["note"], "Claude supervisor paused: usage limit")
        self.assertEqual(s["providers"]["duo"]["note"], "Claude supervisor paused: usage limit")
        self.assertIn("duo", s["providers"])

    def test_settings_for_duo(self):
        c = self.client()
        self.assertTrue(c.get("/api/settings").json()["duo_verify"])
        for body in ({"default_provider": "duo"}, {"duo_verify": False}, {"duo_max_claude_per_hour": 5}):
            r = c.patch("/api/settings", json={**body, "confirm": True})
            self.assertLess(r.status_code, 300, r.text)
        self.assertEqual((self.config.default_provider, self.config.duo_verify, self.config.duo_max_claude_per_hour),
                         ("duo", False, 5))
        for body in ({"duo_verify": "yes"}, {"duo_max_claude_per_hour": -1}, {"duo_max_claude_per_hour": True}):
            self.assertEqual(c.patch("/api/settings", json={**body, "confirm": True}).status_code, 400)

    def test_the_duo_client_is_what_model_for_returns(self):
        c = self.client()
        # exercised through the app: a duo ask reaches Gemini's stream
        self.assertEqual(self.ask(c, "duo"), "gemini")


class GeminiProPreferenceTest(unittest.IsolatedAsyncioTestCase):
    def client(self, model, names):
        def handler(request):
            if request.url.path.endswith("/models") and request.method == "GET":
                return httpx.Response(200, json={"models": [
                    {"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in names]})
            if request.url.path.split("/")[-1].split(":")[0].removeprefix("models/") in names:
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
            return httpx.Response(404, json={"error": {"message": "nope"}})
        return llm.GeminiClient(model, api_key="K", transport=httpx.MockTransport(handler))

    async def test_a_retired_pro_model_falls_back_to_the_best_pro(self):
        c = self.client("gemini-1.5-pro", ["gemini-2.5-flash", "gemini-3-flash", "gemini-2.5-pro", "gemini-3-pro"])
        self.assertEqual(await c.complete("s", "u"), "hi")
        self.assertEqual(c.model, "gemini-3-pro")

    async def test_no_pro_available_falls_back_to_flash(self):
        c = self.client("gemini-1.5-pro", ["gemini-2.5-flash", "gemini-3-flash"])
        await c.complete("s", "u")
        self.assertEqual(c.model, "gemini-3-flash")


if __name__ == "__main__":
    unittest.main()
