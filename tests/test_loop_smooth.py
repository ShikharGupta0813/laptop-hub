"""The app stays smooth: auto-tagging is one local-only job at a time with a bounded queue, and Friday's default brief
gives up on a slow model after a few seconds. Only fakes here: no real AI, no real program."""
import asyncio
import json
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from hub import friday, server
from hub.devserver import FakeAssistant, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config


class SlowLocal:
    """A fake local model that records how many calls run at the same moment."""
    name = "local"

    def __init__(self, delay=0.1):
        self.delay = delay
        self.now = 0
        self.peak = 0
        self.calls = 0

    async def complete(self, system, user, json_mode=False):
        self.calls += 1
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.now -= 1
        return json.dumps({"title": "Tagged", "tags": ["auto"]})

    async def stream(self, system, user):
        yield "x"


class CloudSpy:
    """A cloud or login model that must never be called by auto-tagging."""
    name = "claude"

    def __init__(self):
        self.calls = 0

    async def complete(self, system, user, json_mode=False):
        self.calls += 1
        return json.dumps({"title": "Cloud", "tags": ["cloud"]})

    async def stream(self, system, user):
        self.calls += 1
        yield "x"


class AutoTagging(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.local = SlowLocal()
        self.cloud = [CloudSpy() for _ in range(4)]
        deps = Deps(llm=self.local, claude=self.cloud[0], claude_login=self.cloud[1], gemini=self.cloud[2],
                    duo=self.cloud[3], transcriber=FakeTranscriber(), assistant=FakeAssistant())
        app = create_app(self.config, deps, audit_path=self.root / "audit.log")
        self.client = TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"})
        self.client.__enter__()                # one shared event loop for every request
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/login", json={"token": TOKEN})

    def burst(self, count):
        ids = []

        def post(i):
            ids.append(self.client.post("/api/notes", json={"text": f"burst note number {i}"}).json()["id"])

        threads = [threading.Thread(target=post, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return ids

    def tagged(self, ids):
        return [i for i in ids if self.client.get(f"/api/notes/{i}").json()["tagged"]]

    def test_never_more_than_one_tag_job_and_only_the_local_model(self):
        ids = self.burst(6)
        time.sleep(1.5)
        self.assertEqual(len(ids), 6)
        self.assertEqual(self.local.peak, 1)
        self.assertEqual(len(self.tagged(ids)), 6)           # all fit in the queue
        self.assertEqual([c.calls for c in self.cloud], [0, 0, 0, 0])

    def test_jobs_beyond_the_queue_are_dropped_and_the_note_stays_untagged(self):
        self.local.delay = 0.3
        with mock.patch.object(server, "MAX_TAG_QUEUE", 3):
            ids = self.burst(10)
            time.sleep(2.0)
        done = self.tagged(ids)
        self.assertEqual(self.local.peak, 1)
        self.assertTrue(1 <= len(done) <= 4, len(done))      # 3 queued plus at most one that began early
        self.assertLess(len(done), 10)
        self.assertEqual(len(ids), 10)                       # every note was still saved
        self.assertEqual([c.calls for c in self.cloud], [0, 0, 0, 0])


class SlowModel:
    name = "slow"

    async def complete(self, system, user, json_mode=False):
        await asyncio.sleep(30)
        return "never"


class BriefBound(unittest.TestCase):
    def test_the_default_brief_gives_up_on_a_slow_model_after_the_bound(self):
        asked = []
        ctx = mock.Mock()
        ctx.model_for = lambda choice: asked.append(choice) or SlowModel()
        engine = friday.Engine.__new__(friday.Engine)
        engine.ctx = ctx
        started = time.monotonic()
        with mock.patch.object(friday, "AUTO_MODEL_TIMEOUT", 0.3):
            out = asyncio.run(engine.ask_model("auto", "sys", "data", None))
        self.assertIsNone(out)                                # None means: use the template text
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(asked, ["auto-quick"])               # the quick chain, not the plain "auto" one

    def test_an_explicit_choice_keeps_its_own_model_and_long_wait(self):
        asked = []
        ctx = mock.Mock()
        ctx.model_for = lambda choice: asked.append(choice) or SlowModel()
        engine = friday.Engine.__new__(friday.Engine)
        engine.ctx = ctx
        waits = []
        real_wait_for = asyncio.wait_for

        async def spy(coro, timeout):
            waits.append(timeout)
            coro.close()
            raise asyncio.TimeoutError

        with mock.patch.object(friday.asyncio, "wait_for", spy):
            out = asyncio.run(engine.ask_model("claude", "sys", "data", None))
        self.assertIsNone(out)
        self.assertEqual(asked, ["claude"])
        self.assertEqual(waits, [90])
        self.assertIs(real_wait_for, asyncio.wait_for)


if __name__ == "__main__":
    unittest.main()
