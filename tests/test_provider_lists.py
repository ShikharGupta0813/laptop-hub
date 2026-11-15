"""Vision providers (hub/vision.py list), AI endpoints (hub/endpoints.py) and the shared list plumbing (hub/provlist.py).
Fake Ollama / OpenAI-compatible servers through httpx.MockTransport, a fake keyring, a fake screen: no network."""
import asyncio
import base64
import io
import json
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import autopilot, endpoints, provlist, usage, vision
from hub.common import Ctx
from hub.llm import LLMError
from hub.provlist import MemorySecrets
from tests.helpers import make_config
from tests.test_vision import FakeGemini, FakeSystem, need_auth


def jpeg_of(body: dict) -> bytes:
    """The picture bytes inside an Ollama or OpenAI request body."""
    msg = body["messages"][-1]
    if "images" in msg:
        return base64.b64decode(msg["images"][0])
    url = next(p["image_url"]["url"] for p in msg["content"] if p["type"] == "image_url")
    return base64.b64decode(url.split(",", 1)[1])


class VBase(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="vprov-test-"))
        vision._BACKOFFS.clear()
        self.audits, self.gemini, self.system = [], FakeGemini(), FakeSystem()
        self.models = {"gemini": self.gemini}
        self.secrets = MemorySecrets()
        self.requests = []
        self.reply = {"message": {"content": "A red square."}}
        self.status = 200
        self.ctx = Ctx(config=self.config, model_for=lambda p: self.models.get(p), transcriber=None, notes=None,
                       need_auth=need_auth, audit=lambda e, **f: self.audits.append((e, f)))
        app = FastAPI()
        app.include_router(vision.make_router(self.ctx, system=self.system, today=lambda: "2026-11-13",
                                              secrets=self.secrets, transport=httpx.MockTransport(self.handler)))
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.reply)

    def add(self, kind, settings=None, **extra):
        r = self.client.post("/api/vision/providers", json={"kind": kind, "settings": settings or {}, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["entry"]["id"]

    def explain(self):
        return self.client.post("/api/vision/explain", json={})


class VisionList(VBase):
    def test_seed_is_gemini_then_claude_login_which_is_honest_about_images(self):
        entries = self.client.get("/api/vision/providers").json()["entries"]
        self.assertEqual([e["kind"] for e in entries], ["gemini", "claude-login"])
        self.assertTrue(entries[0]["usable"])
        self.assertFalse(entries[1]["usable"])
        self.assertIn("cannot take pictures", entries[1]["unsupported"])

    def test_default_flow_is_unchanged_gemini_answers(self):
        r = self.explain()
        self.assertEqual((r.status_code, r.json()["provider"]), (200, "Gemini"))
        self.assertEqual(len(self.gemini.calls), 1)

    def test_ollama_image_call_shape(self):
        self.gemini.key = False
        ident = self.add("ollama", {"model": "llava", "host": "http://127.0.0.1:11434"})
        r = self.explain()
        self.assertEqual((r.status_code, r.json()["answer"], r.json()["provider"]), (200, "A red square.", "ollama"))
        request = self.requests[0]
        body = json.loads(request.content)
        self.assertEqual((request.method, request.url.path, request.url.host), ("POST", "/api/chat", "127.0.0.1"))
        self.assertEqual((body["model"], body["stream"]), ("llava", False))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(len(body["messages"][1]["images"]), 1)
        self.assertEqual(jpeg_of(body)[:2], b"\xff\xd8")                    # a real JPEG travelled as base64
        self.assertTrue(body["messages"][1]["content"])
        # the picture is never logged or audited
        self.assertNotIn(body["messages"][1]["images"][0], json.dumps(self.audits))
        self.assertTrue(ident)

    def test_ollama_uses_the_laptops_host_by_default(self):
        self.gemini.key = False
        self.add("ollama", {"model": "qwen2.5vl"})
        self.explain()
        self.assertEqual(self.requests[0].url.port, 11434)

    def test_failure_falls_through_to_the_next_provider_and_is_skipped_afterwards(self):
        self.gemini.reply = LLMError("Gemini is over its limit.")
        self.add("ollama", {"model": "llava"})
        first = self.explain().json()
        self.assertEqual((first["provider"], first["skipped"]), ("ollama", ["Gemini: Gemini is over its limit."]))
        self.gemini.calls.clear()
        second = self.explain().json()
        self.assertEqual(self.gemini.calls, [])                              # backed off: not even asked
        self.assertEqual(second["provider"], "ollama")
        status = {e["id"]: e["status"] for e in self.client.get("/api/vision/providers").json()["entries"]}
        self.assertTrue(status["gemini"]["failing"])
        self.assertIn("over its limit", status["gemini"]["last_error"])

    def test_all_fail_gives_one_503_with_every_reason(self):
        self.gemini.reply = LLMError("no quota")
        self.add("ollama", {"model": "llava"})
        self.status = 404
        r = self.explain()
        self.assertEqual(r.status_code, 503)
        self.assertIn("Gemini: no quota", r.json()["detail"])
        self.assertIn("ollama pull llava", r.json()["detail"])
        self.assertEqual(self.system.captures, 1)                            # one capture, tried in turn

    def test_order_and_switch_off_decide_who_is_asked(self):
        ident = self.add("ollama", {"model": "llava"})
        self.client.put("/api/vision/providers/order", json={"ids": [ident]})
        self.assertEqual(self.explain().json()["provider"], "ollama")
        self.assertEqual(self.gemini.calls, [])
        self.client.put(f"/api/vision/providers/{ident}", json={"enabled": False})
        self.assertEqual(self.explain().json()["provider"], "Gemini")
        self.client.put("/api/vision/providers/gemini", json={"enabled": False})
        r = self.explain()                                                   # nothing usable left
        self.assertEqual(r.status_code, 503)
        self.assertIn("No vision AI is set up", r.json()["detail"])
        self.assertEqual(self.system.captures, 2)

    def test_claude_login_with_images_still_works_when_it_can(self):
        self.gemini.key = False
        self.models["claude-login"] = FakeGemini(reply="from login")
        r = self.explain()
        self.assertEqual((r.json()["answer"], r.json()["provider"]), ("from login", "Claude (my login)"))

    def test_openai_compatible_request_shape_key_and_usage_count(self):
        self.gemini.key = False
        self.reply = {"choices": [{"message": {"content": "It is a dialog box."}}]}
        ident = self.add("openai-compatible", {"base_url": "http://127.0.0.1:1234/v1", "model": "vl-model", "allow_private": True},
                         secret="SK-VISION")
        r = self.explain()
        self.assertEqual((r.status_code, r.json()["answer"]), (200, "It is a dialog box."))
        request = self.requests[0]
        body = json.loads(request.content)
        self.assertEqual((request.url.path, request.headers["authorization"]), ("/v1/chat/completions", "Bearer SK-VISION"))
        self.assertEqual(body["model"], "vl-model")
        parts = body["messages"][1]["content"]
        self.assertEqual([p["type"] for p in parts], ["text", "image_url"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        # the key is in the fake keyring only: never in the list, the file, the audit log or the response
        self.assertEqual(self.secrets.data, {f"vision:{ident}": "SK-VISION"})
        for blob in (json.dumps(self.client.get("/api/vision/providers").json()), r.text, json.dumps(self.audits),
                     (self.config.data_path() / "vision_providers.json").read_text()):
            self.assertNotIn("SK-VISION", blob)
        self.assertGreaterEqual(usage.get(self.config).today().get("endpoint", 0), 1)    # counted like any cloud call

    def test_public_endpoint_cannot_be_a_private_address(self):
        self.gemini.key = False
        self.add("openai-compatible", {"base_url": "http://10.1.2.3/v1", "model": "m"})
        r = self.explain()
        self.assertEqual(r.status_code, 503)
        self.assertIn("not allowed", r.json()["detail"])
        self.assertEqual([r for r in self.requests if r.url.path != "/api/tags"], [])    # only the local Ollama probe may go out

    def test_test_button_sends_a_tiny_generated_picture(self):
        ident = self.add("ollama", {"model": "llava"})
        r = self.client.post(f"/api/vision/providers/{ident}/test")
        self.assertEqual((r.json()["ok"], r.json()["answer"]), (True, "A red square."))
        body = json.loads(self.requests[0].content)
        self.assertLess(len(jpeg_of(body)), 20_000)
        self.assertEqual(self.system.captures, 0)                            # no real screen was captured
        self.assertEqual(self.client.post("/api/vision/providers/claude-login/test").json()["ok"], False)
        self.assertEqual(self.client.post("/api/vision/providers/nope/test").status_code, 404)
        self.status = 500
        bad = self.client.post(f"/api/vision/providers/{ident}/test").json()
        self.assertFalse(bad["ok"])
        self.assertIn("vision model", bad["error"])

    def test_validation_and_login(self):
        for body in ({"kind": "ollama", "settings": {}}, {"kind": "openai-compatible", "settings": {"base_url": "x", "model": "m"}},
                     {"kind": "openai-compatible", "settings": {"base_url": "https://u:p@x.example.com/v1", "model": "m"}},
                     {"kind": "nope"}):
            self.assertEqual(self.client.post("/api/vision/providers", json=body).status_code, 400, body)
        for method, path in [("get", "/api/vision/providers"), ("post", "/api/vision/providers"),
                             ("put", "/api/vision/providers/order"), ("put", "/api/vision/providers/x"),
                             ("delete", "/api/vision/providers/x"), ("post", "/api/vision/providers/x/test")]:
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)

    def test_daily_cap_still_applies_across_the_chain(self):
        self.config.vision_daily_calls = 1
        self.assertEqual(self.explain().status_code, 200)
        self.assertEqual(self.explain().status_code, 429)


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="ep-test-"))
        self.secrets = MemorySecrets()
        self.requests = []
        self.status = 200
        self.reply = {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(endpoints.make_router(ctx, secrets=self.secrets, transport=httpx.MockTransport(self.handler)))
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)
        patcher = mock.patch("hub.research.default_resolver", lambda host: ["93.184.216.34"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.reply)

    def add(self, **settings):
        body = {"kind": "openai-compatible", "label": "Mine", "secret": "SK-1",
                "settings": {"base_url": "https://api.example.com/v1/", "model": "small", **settings}}
        r = self.client.post("/api/ai-endpoints", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["entry"]["id"]

    def test_routes_require_login(self):
        for method, path in [("get", "/api/ai-endpoints"), ("post", "/api/ai-endpoints"), ("put", "/api/ai-endpoints/choice"),
                             ("put", "/api/ai-endpoints/x"), ("delete", "/api/ai-endpoints/x"), ("post", "/api/ai-endpoints/x/test")]:
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)

    def test_add_test_and_no_secret_leak(self):
        ident = self.add()
        listing = self.client.get("/api/ai-endpoints")
        self.assertNotIn("SK-1", listing.text)
        self.assertTrue(listing.json()["entries"][0]["has_secret"])
        self.assertEqual(listing.json()["entries"][0]["settings"]["base_url"], "https://api.example.com/v1")
        r = self.client.post(f"/api/ai-endpoints/{ident}/test").json()
        self.assertTrue(r["ok"])
        request = self.requests[0]
        self.assertEqual((request.url.path, request.headers["authorization"]), ("/v1/chat/completions", "Bearer SK-1"))
        self.status = 401
        bad = self.client.post(f"/api/ai-endpoints/{ident}/test").json()
        self.assertFalse(bad["ok"])
        self.assertNotIn("SK-1", bad["error"])

    def test_cheap_choice_feeds_the_routine_ai_path(self):
        self.assertEqual(endpoints.cheap_choice(self.config, self.secrets), "")
        ident = self.add()
        self.assertEqual(autopilot.pick_provider(self.config) in ("local", "gemini"), True)      # nothing chosen: as before
        self.assertEqual(self.client.put("/api/ai-endpoints/choice", json={"id": ident}).status_code, 200)
        self.assertEqual(endpoints.cheap_choice(self.config, self.secrets), f"endpoint:{ident}")
        self.assertEqual(autopilot.pick_provider(self.config), f"endpoint:{ident}")
        self.client.put(f"/api/ai-endpoints/{ident}", json={"enabled": False})
        self.assertIn(autopilot.pick_provider(self.config), ("local", "gemini"))
        self.client.put(f"/api/ai-endpoints/{ident}", json={"enabled": True})
        self.client.delete(f"/api/ai-endpoints/{ident}")
        self.assertEqual(endpoints.chosen_id(self.config), "")                                  # deleting clears the choice
        self.assertEqual(self.client.put("/api/ai-endpoints/choice", json={"id": "nope"}).status_code, 404)

    def test_guarded_client_counts_usage_and_redacts(self):
        ident = self.add()
        client = endpoints.build_guarded(self.config, lambda *a, **k: None, ident, self.secrets)
        self.assertIsNotNone(client)
        client._inner._transport = httpx.MockTransport(self.handler)
        before = usage.get(self.config).today().get("endpoint", 0)
        out = asyncio.run(client.complete("sys", "mail me at jane.doe@example.com", json_mode=True))
        self.assertEqual(out, '{"ok": true}')
        self.assertEqual(usage.get(self.config).today().get("endpoint", 0), before + 1)
        sent = json.loads(self.requests[0].content)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertNotIn("jane.doe@example.com", json.dumps(sent["messages"]))

    def test_private_address_only_when_the_owner_says_so(self):
        ident = self.add(base_url="http://10.0.0.9:8000/v1")
        self.assertFalse(self.client.post(f"/api/ai-endpoints/{ident}/test").json()["ok"])
        self.assertEqual(self.requests, [])
        self.client.put(f"/api/ai-endpoints/{ident}", json={"settings": {"allow_private": True}})
        self.assertTrue(self.client.post(f"/api/ai-endpoints/{ident}/test").json()["ok"])

    def test_bad_settings(self):
        for settings in ({"base_url": "ftp://x"}, {"model": ""}, {"base_url": "https://u:p@x.example.com/v1"}):
            body = {"kind": "openai-compatible", "settings": {"base_url": "https://x.example.com/v1", "model": "m", **settings}}
            self.assertEqual(self.client.post("/api/ai-endpoints", json=body).status_code, 400, settings)


class ListPlumbing(unittest.TestCase):
    def test_backoff_doubles_and_resets(self):
        now = [1000.0]
        b = provlist.Backoff(lambda: now[0])
        b.fail("a", "boom")
        self.assertEqual(round(b.blocked_for("a")), 60)
        now[0] += 61
        self.assertEqual(b.blocked_for("a"), 0)
        b.fail("a", "boom")
        self.assertEqual(round(b.blocked_for("a")), 120)
        b.ok("a")
        self.assertEqual(b.blocked_for("a"), 0)
        self.assertFalse(b.view("a")["failing"])

    def test_damaged_file_starts_from_the_seed(self):
        config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="pl-test-"))
        path = config.data_path() / "vision_providers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual([e["kind"] for e in vision.list_for(config, MemorySecrets()).load()], ["gemini", "claude-login"])


if __name__ == "__main__":
    unittest.main()
