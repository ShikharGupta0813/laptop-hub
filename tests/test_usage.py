"""Usage counters, caps, the budget fallback in the router, and the routes. Fakes only."""
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from hub import providers, usage
from hub.devserver import FakeAssistant, FakeTranscriber
from hub.redact import Guarded
from hub.router import Router
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config
from tests.test_providers import Tagged


class Clock:
    def __init__(self, t=None):
        self.t = t or time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))

    def __call__(self):
        return self.t


class Tracking(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hub-usage-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.clock = Clock()
        self.path = self.dir / "usage.json"

    def tracker(self):
        return usage.Tracker(self.path, self.clock)

    def test_counts_per_provider_and_persists(self):
        t = self.tracker()
        for p in ("gemini", "gemini", "claude-login", "local"):
            t.record(p)
        self.assertEqual(t.today(), {"gemini": 2, "claude-login": 1, "local": 1})
        again = self.tracker()                          # a fresh process reads the file
        self.assertEqual(again.today(), {"gemini": 2, "claude-login": 1, "local": 1})
        self.assertEqual(json.loads(self.path.read_text())["days"]["2026-11-13"]["gemini"], 2)

    def test_new_day_starts_at_zero_and_week_sums(self):
        t = self.tracker()
        t.record("gemini", 3)
        self.clock.t += 86400
        t.record("gemini")
        self.assertEqual(t.today(), {"gemini": 1})
        self.assertEqual(t.week(), {"gemini": 4})
        self.clock.t += 7 * 86400
        self.assertEqual(t.week(), {})

    def test_rotation_keeps_fourteen_days(self):
        t = self.tracker()
        for i in range(20):
            t.record("gemini")
            self.clock.t += 86400
        t.record("local")
        days = json.loads(self.path.read_text())["days"]
        self.assertEqual(len(days), usage.KEEP_DAYS)
        oldest = (date.fromtimestamp(self.clock.t) - timedelta(days=usage.KEEP_DAYS - 1)).isoformat()
        self.assertEqual(min(days), oldest)

    def test_corrupt_file_starts_over(self):
        for junk in ("{not json", "[]", '{"days": 5}', '{"days": {"2026-11-13": {"gemini": "x", "local": -3, "claude": 2}}}', ""):
            self.path.write_text(junk)
            t = self.tracker()
            t.record("gemini")
            fresh = self.tracker()
            self.assertEqual(fresh.today().get("gemini"), 1, junk)
        self.assertEqual(fresh.today().get("claude"), None)

    def test_unwritable_folder_never_raises(self):
        t = usage.Tracker(self.dir / "file" / "usage.json", self.clock)
        (self.dir / "file").write_text("i am a file, not a folder")
        t.record("gemini")                                  # the write fails quietly
        self.assertEqual(t.today(), {"gemini": 1})

    def test_login_five_hour_window(self):
        t = self.tracker()
        for _ in range(3):
            t.record("claude-login")
        self.clock.t += 4 * 3600
        t.record("claude-login")
        self.assertEqual(t.login_window(), 4)
        self.clock.t += 2 * 3600                            # the first three are now over 5 h old
        self.assertEqual(t.login_window(), 1)
        self.assertEqual(self.tracker().login_window(), 1)


class Caps(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_no_caps_means_never_reached(self):
        usage.record(self.config, "gemini", 1000)
        self.assertEqual(usage.cap_reached(self.config, "gemini"), "")

    def test_provider_cap_and_message(self):
        self.config.provider_daily_caps = {"gemini": 2}
        usage.record(self.config, "gemini")
        self.assertEqual(usage.cap_reached(self.config, "gemini"), "")
        usage.record(self.config, "gemini")
        self.assertEqual(usage.cap_reached(self.config, "gemini"), "Daily Gemini cap reached; using the local model")
        self.assertEqual(usage.cap_reached(self.config, "claude-login"), "")
        self.assertEqual(usage.cap_reached(self.config, "local"), "")

    def test_cloud_cap_counts_all_cloud_providers_not_local(self):
        self.config.cloud_daily_cap = 3
        usage.record(self.config, "gemini")
        usage.record(self.config, "claude-login")
        usage.record(self.config, "local", 50)
        usage.record(self.config, "research", 50)
        self.assertEqual(usage.cap_reached(self.config, "duo"), "")
        usage.record(self.config, "claude")
        self.assertIn("cloud cap", usage.cap_reached(self.config, "gemini"))
        self.assertEqual(usage.cap_reached(self.config, "local"), "")

    def test_report_headroom(self):
        self.config.provider_daily_caps = {"gemini": 10}
        self.config.cloud_daily_cap = 20
        self.config.claude_login_5h_soft_cap = 40
        usage.record(self.config, "gemini", 4)
        usage.record(self.config, "claude-login", 5)
        r = usage.report(self.config)
        row = next(x for x in r["providers"] if x["provider"] == "gemini")
        self.assertEqual((row["today"], row["cap"], row["remaining"]), (4, 10, 6))
        self.assertEqual(r["cloud"], {"used": 9, "cap": 20, "remaining": 11})
        self.assertEqual(r["claude_login_5h"]["calls"], 5)
        self.assertEqual(r["claude_login_5h"]["remaining"], 35)
        self.assertEqual(set(r["today"]), set(usage.PROVIDERS))

    def test_five_hour_block_only_when_set(self):
        self.assertNotIn("claude_login_5h", usage.report(self.config))


class FixedRouter(unittest.TestCase):
    """The budget fallback in the router: cap reached -> local with a visible note, never another paid provider."""

    def setUp(self):
        self.config, self.root = make_config(default_provider="auto")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        for name, val in (("gemini_key", "k"), ("find_claude_cli", "/usr/bin/claude")):
            m = mock.patch.object(providers, name, return_value=val)
            m.start()
            self.addCleanup(m.stop)
        self.router = Router(self.config)

    def test_under_cap_is_unchanged(self):
        self.config.provider_daily_caps = {"gemini": 5}
        usage.record(self.config, "gemini", 4)
        self.assertEqual(self.router.route("gemini", "explain recursion in depth please")[0], "gemini")

    def test_explicit_provider_at_cap_falls_back_to_local_with_note(self):
        self.config.provider_daily_caps = {"gemini": 5}
        usage.record(self.config, "gemini", 5)
        provider, decision = self.router.route("gemini", "explain recursion in depth please")
        self.assertEqual(provider, "local")
        self.assertEqual(decision["routed_to"], "local")
        self.assertTrue(decision["capped"])
        self.assertEqual(decision["note"], "Daily Gemini cap reached; using the local model.")
        self.assertIn("use gemini anyway", decision["reason"])

    def test_auto_route_at_cap_never_picks_a_different_paid_provider(self):
        self.config.provider_daily_caps = {"gemini": 1}
        usage.record(self.config, "gemini", 1)
        provider, decision = self.router.route("auto", "please run the tests and then fix every failing one")
        self.assertEqual(provider, "duo" if usage.cap_reached(self.config, "duo") == "" else "local")
        self.config.provider_daily_caps = {"duo": 1, "gemini": 1}
        usage.record(self.config, "duo")
        provider, decision = self.router.route("auto", "please run the tests and then fix every failing one")
        self.assertEqual(provider, "local")
        self.assertNotIn(provider, ("gemini", "claude-login", "claude"))
        self.assertTrue(decision["capped"])

    def test_cloud_cap_covers_the_default(self):
        self.config.default_provider = "gemini"
        self.config.cloud_daily_cap = 2
        usage.record(self.config, "claude-login", 2)
        provider, decision = self.router.route("", "tell me a long story about dragons and castles")
        self.assertEqual(provider, "local")
        self.assertIn("Daily cloud cap reached", decision["note"])

    def test_text_override_lifts_the_cap_for_one_turn_only(self):
        self.config.provider_daily_caps = {"gemini": 1}
        usage.record(self.config, "gemini", 1)
        provider, decision = self.router.route("auto", "use gemini anyway: explain how rainbows form")
        self.assertEqual(provider, "gemini")
        self.assertFalse(decision.get("capped"))
        self.assertEqual(self.router.route("gemini", "next question, no override")[0], "local")

    def test_override_flag(self):
        self.config.provider_daily_caps = {"gemini": 1}
        usage.record(self.config, "gemini", 1)
        self.assertEqual(self.router.route("gemini", "hello there", override_cap=True)[0], "gemini")
        self.assertEqual(self.router.route("gemini", "hello there")[0], "local")

    def test_local_is_never_capped(self):
        self.config.cloud_daily_cap = 1
        usage.record(self.config, "gemini", 9)
        provider, decision = self.router.route("local", "hi")
        self.assertEqual(provider, "local")
        self.assertIsNone(decision)


class Hooks(unittest.IsolatedAsyncioTestCase):
    async def test_guarded_client_counts_each_request(self):
        config, root = make_config()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        class Fake:
            name = "gemini"

            async def complete(self, system, user, json_mode=False):
                return "ok"

            async def stream(self, system, user):
                yield "ok"

        g = Guarded(Fake(), config, count_as="gemini")
        await g.complete("s", "u")
        [x async for x in g.stream("s", "u")]
        duo_leg = Guarded(Fake(), config, count_as="duo")
        await duo_leg.complete("s", "u")
        local = Guarded(Fake(), config, count_as="local", redact_text=False)
        await local.complete("s", "u")
        self.assertEqual(usage.get(config).today(), {"gemini": 2, "duo": 1, "local": 1})


class Routes(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config(default_provider="auto")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
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
        return c

    def test_usage_route_needs_login(self):
        c = self.client()
        self.assertEqual(c.get("/api/usage").status_code, 401)
        c.post("/api/login", json={"token": TOKEN})
        body = c.get("/api/usage").json()
        self.assertEqual(body["today"]["gemini"], 0)
        self.assertEqual(body["caps"]["cloud_daily_cap"], 0)

    def test_ask_counts_and_falls_back_at_cap(self):
        self.config.provider_daily_caps = {"gemini": 1}
        c = self.client()
        c.post("/api/login", json={"token": TOKEN})
        first = c.post("/api/ask", json={"mode": "custom", "text": "", "question": "what is two plus two exactly?", "provider": "gemini"})
        self.assertEqual(first.status_code, 200)
        self.assertIn("gemini", first.text)
        self.assertEqual(c.get("/api/usage").json()["today"]["gemini"], 1)
        second = c.post("/api/ask", json={"mode": "custom", "text": "", "question": "what is two plus three exactly?", "provider": "gemini"})
        self.assertIn("Daily Gemini cap reached; using the local model", second.text)
        self.assertIn("local", second.text)
        self.assertEqual(c.get("/api/usage").json()["today"]["gemini"], 1)      # not counted: it never went out
        third = c.post("/api/ask", json={"mode": "custom", "text": "", "question": "what is two plus four exactly?",
                                         "provider": "gemini", "override_cap": True})
        self.assertNotIn("cap reached", third.text)
        self.assertEqual(c.get("/api/usage").json()["today"]["gemini"], 2)

    def test_assistant_message_announces_the_fallback(self):
        self.config.provider_daily_caps = {"gemini": 1}
        usage.record(self.config, "gemini")
        c = self.client()
        c.post("/api/login", json={"token": TOKEN})
        r = c.post("/api/assistant/message", json={"session_id": "s", "text": "hi", "provider": "gemini"})
        self.assertIn('"routed_to": "local"', r.text)
        self.assertIn("Daily Gemini cap reached; using the local model", r.text)


if __name__ == "__main__":
    unittest.main()
