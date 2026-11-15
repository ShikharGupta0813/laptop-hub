"""Screen vision (hub/vision.py): fake capture, fake Gemini. No screen, no network."""
import asyncio
import base64
import io
import json
import logging
import os
import tempfile
import unittest

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from hub import vision
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from hub.llm import GeminiClient, LLMError
from tests.helpers import make_config


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


def noisy_jpeg(w, h, quality=95):
    image = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class FakeSystem:
    def __init__(self, jpeg=None, ok=True):
        self.jpeg, self.ok, self.captures = jpeg or noisy_jpeg(64, 48), ok, 0

    def availability(self):
        return self.ok, "" if self.ok else "Screen view is not set up."

    def capture_jpeg(self, monitor, max_side, quality):
        self.captures += 1
        return self.jpeg, (64, 48)


class FakeGemini:
    name = "gemini"

    def __init__(self, key=True, reply="It is a missing module. Run pip install x."):
        self.key, self.reply, self.calls = key, reply, []

    def has_key(self):
        return self.key

    async def ask_image(self, system, question, image, mime="image/jpeg"):
        self.calls.append((system, question, image, mime))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Base(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="vision-test-"))
        self.audits, self.gemini, self.system = [], FakeGemini(), FakeSystem()
        self.models = {"gemini": self.gemini}
        self.day = "2026-11-13"
        self.ctx = Ctx(config=self.config, model_for=lambda p: self.models.get(p), transcriber=None, notes=None,
                       need_auth=need_auth, audit=lambda e, **f: self.audits.append((e, f)))
        app = FastAPI()
        app.include_router(vision.make_router(self.ctx, system=self.system, today=lambda: self.day))
        self.client = TestClient(app, cookies={"hub_session": "yes"})

    def explain(self, **body):
        return self.client.post("/api/vision/explain", json=body)


class Resize(unittest.TestCase):
    def test_long_edge_and_size_caps(self):
        out = vision.shrink(noisy_jpeg(3200, 1800))
        self.assertLessEqual(max(Image.open(io.BytesIO(out)).size), 1600)
        self.assertLessEqual(len(out), 1_200_000)

    def test_huge_noise_still_under_byte_cap(self):
        out = vision.shrink(noisy_jpeg(2400, 1600, 100))
        self.assertLessEqual(len(out), 1_200_000)
        self.assertLessEqual(max(Image.open(io.BytesIO(out)).size), 1600)

    def test_region_crops(self):
        out = vision.shrink(noisy_jpeg(400, 200), (0.5, 0.0, 0.5, 1.0))
        self.assertEqual(Image.open(io.BytesIO(out)).size, (200, 200))

    def test_bad_region_and_bad_image(self):
        with self.assertRaises(vision.VisionError):
            vision.clean_region({"x": 0.8, "y": 0, "w": 0.5, "h": 1})
        with self.assertRaises(vision.VisionError):
            vision.shrink(b"not a jpeg")


class Route(Base):
    def test_requires_login(self):
        anon = TestClient(self.client.app)
        self.assertEqual(anon.post("/api/vision/explain", json={}).status_code, 401)
        self.assertEqual(anon.get("/api/vision/status").status_code, 401)

    def test_explains_with_image_and_question(self):
        r = self.explain(question="what is this error?")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["untrusted"])
        _system, question, image, mime = self.gemini.calls[0]
        self.assertEqual((question, mime), ("what is this error?", "image/jpeg"))
        self.assertLessEqual(len(image), 1_200_000)
        self.assertNotIn("image", r.json())

    def test_answer_capped_at_120_words(self):
        self.gemini.reply = "word " * 300
        self.assertLessEqual(len(self.explain().json()["answer"].split()), 121)

    def test_no_gemini_key_is_a_clear_message(self):
        self.gemini.key = False
        r = self.explain()
        self.assertEqual(r.status_code, 503)
        self.assertIn("No vision AI is set up", r.json()["detail"])
        self.assertIn("moondream", r.json()["detail"])
        self.assertEqual(self.system.captures, 0)         # nothing was captured

    def test_setup_view_and_test_button_when_nothing_is_set_up(self):
        self.gemini.key = False
        info = self.client.get("/api/vision/setup").json()
        self.assertFalse(info["ready"])
        self.assertIn("moondream", [o["tag"] for o in info["pull_options"]])
        self.assertFalse(self.client.post("/api/vision/test").json()["ok"])

    def test_phone_screen_explained_from_its_text_without_a_vision_model(self):
        self.gemini.key = False

        class Text:
            async def complete(self, system, user, json_mode=False):
                return "This is the Notes list."
        self.models["auto"] = Text()
        r = self.explain(source="phone", text="Notes\nNew note\nGroceries")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["source"], "phone")
        self.assertEqual(self.system.captures, 0)
        self.assertEqual(self.explain(source="phone", text="  ").status_code, 400)

    def test_claude_without_images_is_never_used(self):
        class NoImages:
            async def complete(self, *a, **k):
                raise AssertionError("must not be called")
        self.gemini.key = False
        self.models.update({"claude-login": NoImages(), "claude": FakeGemini(reply="paid!")})
        self.assertEqual(self.explain().status_code, 503)      # even a "claude" with images is not consulted

    def test_claude_login_with_images_is_the_fallback(self):
        self.gemini.key = False
        self.models["claude-login"] = FakeGemini(reply="from login")
        self.assertEqual(self.explain().json()["answer"], "from login")

    def test_budget(self):
        self.config.vision_daily_calls = 2
        self.assertEqual(self.explain().status_code, 200)
        self.assertEqual(self.explain().json()["left_today"], 0)
        self.assertEqual(self.explain().status_code, 429)
        self.assertEqual(len(self.gemini.calls), 2)
        self.day = "2026-11-14"                           # a new day resets it
        self.assertEqual(self.explain().status_code, 200)

    def test_switched_off(self):
        self.config.vision_enabled = False
        self.assertEqual(self.explain().status_code, 404)
        self.assertEqual(self.system.captures, 0)

    def test_capture_unavailable_and_model_error(self):
        self.system.ok = False
        self.assertEqual(self.explain().status_code, 501)
        self.system.ok = True
        self.gemini.reply = LLMError("Gemini is having trouble. Try again shortly.")
        r = self.explain()
        self.assertEqual(r.status_code, 503)
        self.assertIn("Gemini", r.json()["detail"])

    def test_bad_region_is_400(self):
        self.assertEqual(self.explain(region={"x": 2}).status_code, 400)

    def test_no_bytes_in_audit_or_logs(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        root = logging.getLogger()
        handler, old = Grab(level=logging.DEBUG), root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old)))
        self.explain(question="q")
        image = self.gemini.calls[0][2]
        blob = json.dumps(self.audits) + " ".join(records)
        self.assertEqual(self.audits[0][0], "vision_explain")
        self.assertNotIn(base64.b64encode(image[:60]).decode(), blob)
        self.assertNotIn(repr(image[:30]), blob)
        self.assertLess(len(blob), 2000)


class Tool(Base):
    def setUp(self):
        super().setUp()
        self.tool = vision.build(lambda: None, self.config)[0]

    def test_needs_approval_preview_and_taint(self):
        self.assertEqual(self.tool.spec["name"], "screen_explain")
        self.assertTrue(self.tool.needs_approval({}))
        self.assertTrue(self.tool.untrusted({}))
        ok, info = asyncio.run(self.tool.preview({}))
        self.assertTrue(ok)
        self.assertEqual(info["what"], "Look at your screen")

    def test_tool_group_routes_screen_questions(self):
        names, pattern = TOOL_GROUPS["vision"]
        self.assertIn("screen_explain", names)
        for text in ("what's this error on my screen", "what am I looking at", "what's this"):
            self.assertTrue(pattern.search(text), text)

    def test_registered_module_and_defaults(self):
        from hub.server import FEATURE_MODULES
        self.assertIn("vision", FEATURE_MODULES)
        self.assertTrue(self.config.vision_enabled)
        self.assertEqual(self.config.vision_daily_calls, 30)


class GeminiImage(unittest.TestCase):
    def test_inline_image_part_is_sent(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok answer"}]}}]})
        client = GeminiClient(api_key="k", transport=httpx.MockTransport(handler))
        out = asyncio.run(client.ask_image("sys", "what?", b"\xff\xd8abc", "image/jpeg"))
        self.assertEqual(out, "ok answer")
        parts = seen["body"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], "what?")
        self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), b"\xff\xd8abc")


if __name__ == "__main__":
    unittest.main()


class AutoOllama(Base):
    """A vision model already pulled into Ollama works with no provider list entry."""

    def setUp(self):
        import httpx
        self.tags = {"models": [{"name": "llama3:8b", "size": 1}, {"name": "moondream:latest", "size": 1}]}

        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=self.tags)
            if request.url.path == "/api/chat":
                return httpx.Response(200, json={"message": {"content": "A red square."}})
            return httpx.Response(404)
        self.transport = httpx.MockTransport(handler)
        super().setUp()
        app = FastAPI()
        app.include_router(vision.make_router(self.ctx, system=self.system, today=lambda: self.day, transport=self.transport))
        vision.ENGINES[id(self.ctx)].transport = self.transport
        self.client = TestClient(app, cookies={"hub_session": "yes"})

    def test_pulled_vision_model_is_used_automatically(self):
        self.gemini.key = False
        r = self.explain()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("moondream", r.json()["provider"])
        self.assertTrue(self.client.get("/api/vision/status").json()["ready"])
        t = self.client.post("/api/vision/test").json()
        self.assertTrue(t["ok"], t)
        self.assertEqual(self.client.get("/api/vision/setup").json()["pull_options"][0]["tag"], "llava:7b")
