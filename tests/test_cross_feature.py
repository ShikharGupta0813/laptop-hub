"""Cross-feature checks over the REAL tool registry and app: taint gate for every changing tool after every untrusted
tool, outbound tools asking in Full Control, redaction of results from new tools, secrets never in responses or the
audit log, and the alert -> proactive -> notification/spoken pipeline. Fakes only."""
import asyncio
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from hub import assistant as A
from hub import chatstore, safety
from hub.devserver import FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.apiwalk import flat_routes
from tests.fakes_assistant import FakeClaude, reply, tool_block
from tests.helpers import TOKEN, make_config
from tests.test_bootcheck import make_app


async def collect(gen):
    return [e async for e in gen]


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def sample_args(spec):
    """Smallest arguments that pass the tool's schema, so the call really reaches the gate."""
    schema = spec["input_schema"]
    out = {}
    for key in schema.get("required", []):
        p = schema["properties"][key]
        kind = p.get("type")
        if "enum" in p:
            out[key] = p["enum"][0]
        elif kind == "integer" or kind == "number":
            out[key] = max(1, int(p.get("minimum", 1)))
        elif kind == "boolean":
            out[key] = True
        elif kind == "array":
            out[key] = []
        elif kind == "object":
            out[key] = {}
        else:
            out[key] = "x"
    return out


class RealRegistry(unittest.IsolatedAsyncioTestCase):
    """The tools the real app registers, with their `run` swapped for a recorder so nothing touches the world."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="hub-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_CONFIG", "ANTHROPIC_API_KEY")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        app, self.config, self.root = make_app(real_assistant=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.real = dict(app.state.assistant._extra)
        self.ran = []
        self._chat_store = None

    def tools(self):
        out = []
        for name, tool in self.real.items():
            def make(n):
                async def run(args):
                    self.ran.append(n)
                    return True, f"{n} ran"
                return run
            clone = A.ExtraTool(tool.spec, make(name), needs_approval=tool._needs, preview=None, untrusted=tool._untrusted,
                                mutating=tool.mutating, level=tool.level, always_ask=tool.always_ask, sensitive=tool.sensitive)
            out.append(clone)
        return out

    def spec_of(self, name):
        return self.real[name].spec

    def service(self, script, **config):
        for k, v in config.items():
            setattr(self.config, k, v)
        self.claude = FakeClaude(script)
        self.audit = []
        svc = A.AssistantService(self.config, claude=self.claude, extra_tools=self.tools(),
                                 audit=lambda event, **f: self.audit.append((event, f)))
        # One chat-history file for the whole test: opening and creating it again for each of the hundreds of services
        # in the taint test cost most of its time on Windows.
        if self._chat_store is None:
            self._chat_store = chatstore.ChatStore(Path(self.config.data_path()) / chatstore.FILE)
            self.addCleanup(self._chat_store.close)
        svc._chat_db = self._chat_store
        return svc


class EveryToolValidatesTest(RealRegistry):
    def test_the_assistants_validator_accepts_minimal_arguments_for_every_tool(self):
        app, _c, _r = make_app(real_assistant=True)
        for spec in app.state.assistant._static_specs:
            with self.subTest(tool=spec["name"]):
                args, problem = A.validate(spec, sample_args(spec))
                self.assertIsNone(problem)
                self.assertIsInstance(args, dict)
                _args, problem = A.validate(spec, {})
                self.assertTrue(problem is None or "required" in problem.lower() or "missing" in problem.lower())


class TaintAcrossModulesTest(RealRegistry):
    async def asyncSetUp(self):
        # IsolatedAsyncioTestCase runs the loop in debug mode, which records a stack for every scheduled callback; across
        # the several hundred (untrusted x changing) turns below that costs minutes on Windows (slow linecache stat calls).
        asyncio.get_running_loop().set_debug(False)

    async def test_every_untrusted_tool_blocks_every_changing_tool(self):
        self.maxDiff = None
        tools = self.tools()
        untrusted = [t.spec["name"] for t in tools if t.untrusted(sample_args(t.spec))]
        changing = [t.spec["name"] for t in tools if t.needs_approval(sample_args(t.spec)) or t.mutating]
        self.assertGreater(len(untrusted), 15)
        self.assertGreater(len(changing), 30)
        spec = {t.spec["name"]: t.spec for t in tools}
        bad = []
        for source in untrusted:
            for target in (n for n in changing if n != source):    # a blocked call ends the batch, so one pair per turn
                self.ran.clear()
                calls = [tool_block("u0", source, sample_args(spec[source])),
                         tool_block("c0", target, sample_args(spec[target]))]
                svc = self.service([reply(*calls, stop="tool_use"), reply("ok")], full_control=True)
                events = await collect(svc.run("s", "go", "claude", True))
                results = {e["id"]: e for e in of(events, "tool_result")}
                if self.ran != [source] or results.get("c0", {}).get("blocked") != "taint" or of(events, "approval_needed"):
                    bad.append((source, target, list(self.ran)))
        self.assertEqual(bad, [], "a changing tool ran (or asked) after untrusted content was read")


class OutboundAsksEveryTimeTest(RealRegistry):
    async def test_outbound_tools_ask_even_in_full_control_with_auto_approve(self):
        outbound = sorted(safety.ALWAYS_ASK)
        self.assertEqual(set(outbound), {"send_email", "send_application", "post_publish", "submit_form", "chat_send", "computer_run", "batch_send"})
        for name in outbound:
            with self.subTest(tool=name):
                self.assertIn(name, self.real, f"{name} is not registered")
                self.ran.clear()
                svc = self.service([reply(tool_block("t1", name, sample_args(self.spec_of(name))), stop="tool_use"), reply("done")],
                                   full_control=True)
                asked = []
                events = []
                async for e in svc.run("s", "send it", "claude", True):
                    events.append(e)
                    if e["type"] == "approval_needed":
                        asked.append(e["name"])
                        svc.resolve("s", e["id"], False)
                self.assertEqual(asked, [name])
                self.assertEqual(self.ran, [], "ran without the owner's tap")

    def test_mcp_tools_always_ask(self):
        src = (Path(__file__).resolve().parent.parent / "hub" / "mcpclient.py").read_text(encoding="utf-8")
        self.assertIn("always_ask=True", src)


class RedactionOnNewToolPaths(RealRegistry):
    async def test_results_of_new_tools_are_redacted_for_claude(self):
        async def mail(args):
            return True, "From bob@example.com: my card is 4111 1111 1111 1111, call back"
        tool = A.ExtraTool({"name": "gmail_peek", "description": "peek", "input_schema":
                            {"type": "object", "properties": {}, "required": []}}, mail, untrusted=True)
        self.claude = FakeClaude([reply(tool_block("t1", "gmail_peek", {}), stop="tool_use"), reply("ok")])
        svc = A.AssistantService(self.config, claude=self.claude, extra_tools=[tool], audit=lambda *a, **k: None)
        await collect(svc.run("s", "check mail", "claude", True))
        second = repr(self.claude.calls[1]["messages"])
        self.assertNotIn("bob@example.com", second)
        self.assertNotIn("4111", second)


class SecretsNeverLeakTest(unittest.TestCase):
    SECRET = "SECRETVALUE-9f3a7c1d2b"

    def setUp(self):
        home = tempfile.mkdtemp(prefix="hub-home-")
        env = mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home})
        env.start()
        self.addCleanup(env.stop)
        store = {}
        fake = types.ModuleType("keyring")
        fake.get_password = lambda s, n: store.get((s, n), self.SECRET)      # every secret slot "is filled"
        fake.set_password = lambda s, n, v: store.__setitem__((s, n), v)
        fake.delete_password = lambda s, n: store.pop((s, n), None)
        errors = types.ModuleType("keyring.errors")
        errors.PasswordDeleteError = errors.KeyringError = type("KeyringError", (Exception,), {})
        fake.errors = errors
        patch = mock.patch.dict(sys.modules, {"keyring": fake, "keyring.errors": errors})
        patch.start()
        self.addCleanup(patch.stop)
        self.app, self.config, self.root = make_app(real_assistant=True)
        self.client = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"},
                                 raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/login", json={"token": TOKEN})

    def test_no_route_response_audit_or_notification_holds_a_stored_secret(self):
        # write paths that take a secret, so it really is stored somewhere before everything is read back
        self.client.post("/api/gmail/accounts/imap", json={"label": "Work", "address": "me@corp.example",
                                                           "password": self.SECRET, "preset": "custom",
                                                           "imap_host": "imap.corp.example", "smtp_host": "smtp.corp.example"})
        leaks = []
        for path, methods, _ep in sorted(flat_routes(self.app)):
            if "GET" in methods and path.startswith("/api/") and "stream" not in path:
                r = self.client.get(re.sub(r"\{[^}]*\}", "x", path))
                if self.SECRET in r.text:
                    leaks.append(path)
        self.assertEqual(leaks, [])
        audit = self.root / "audit.log"
        self.assertNotIn(self.SECRET, audit.read_text() if audit.exists() else "")
        for f in Path(self.config.data_dir).rglob("*"):
            if f.is_file() and f.suffix in (".json", ".log", ".jsonl", ".txt"):
                self.assertNotIn(self.SECRET, f.read_text(errors="ignore"), str(f))


class AlertPipelineTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="hub-home-")
        env = mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home})
        env.start()
        self.addCleanup(env.stop)
        self.app, self.config, self.root = make_app(real_assistant=True)
        self.client = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/login", json={"token": TOKEN})
        # The real laptop idle time would make presence say "at_laptop" (no phone speech) whenever someone is using this
        # machine. This test is about the pipeline, so presence answers "speak as before"; presence has its own tests.
        pin = mock.patch("hub.presence.alert_actions", return_value=None)
        pin.start()
        self.addCleanup(pin.stop)

    def test_urgent_alert_reaches_notifications_the_phone_voice_queue_and_proactive_log(self):
        r = self.client.post("/api/alerts/ingest", json={"source": "whatsapp", "title": "Boss", "sender": "Boss",
                                                         "text": "URGENT: production is down, call me now"})
        body = r.json()
        self.assertTrue(body["accepted"])
        self.assertEqual(body["urgency"], 3)
        notes = self.client.get("/api/notifications").json()["notifications"]
        urgent = [n for n in notes if n["title"].startswith("Urgent")]
        self.assertTrue(urgent and urgent[0]["link"] == "alerts" and urgent[0]["level"] == "error")
        pending = self.client.get("/api/alerts/pending").json()["alerts"]
        self.assertEqual([p["id"] for p in pending], [body["alert_id"]])
        self.assertIn("urgent", pending[0]["speech"].lower())
        events = self.client.get("/api/proactive/state").json()["events"]
        self.assertTrue(any(e["type"] == "alert" and e["decision"] == "now" for e in events))
        self.client.post("/api/alerts/pending/ack", json={"ids": [body["alert_id"]]})
        self.assertEqual(self.client.get("/api/alerts/pending").json()["alerts"], [])

    def test_junk_does_not_notify(self):
        before = self.client.get("/api/notifications").json()["unread"]
        r = self.client.post("/api/alerts/ingest", json={"source": "sms", "title": "Shop", "sender": "Shop",
                                                         "text": "Flash deal! 50% off, unsubscribe here"})
        self.assertEqual(self.client.get("/api/notifications").json()["unread"], before)
        self.assertIn(r.status_code, (200, 202))


if __name__ == "__main__":
    unittest.main()
