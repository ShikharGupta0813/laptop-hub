"""API smoke over the real app (fake models, empty data dir, no keyring, no network): no route may answer 500,
every module starts and stops cleanly on a first run, and nothing is left running afterwards."""
import asyncio
import gc
import os
import re
import tempfile
import threading
import unittest
import warnings
from unittest import mock

from fastapi.testclient import TestClient

from tests.apiwalk import flat_routes
from tests.helpers import TOKEN
from tests.test_bootcheck import make_app

# Routes that would act on the real machine (lock, shut down, open apps, pull models...) are not hit with blank bodies.
SKIP = ("/api/login", "/api/logout", "/api/laptop", "/api/jarvis/", "/api/updater", "/api/update", "/api/terminal",
        "/api/wakeword", "/api/dictation", "/api/screen", "/api/models/pull", "/api/backup")
# "Not set up" answers are by design (the module degrades to a plain status); a crash is not.
NOT_CONFIGURED = (501, 503)


class Base(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="hub-home-")
        patcher = mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.config, self.root = make_app(real_assistant=True)
        self.client = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"},
                                 raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)

    def walk(self, verbs, payload=None):
        bad = []
        count = 0
        for path, methods, _endpoint in sorted(flat_routes(self.app)):
            if not path.startswith("/api/") or "stream" in path or any(path.startswith(s) for s in SKIP):
                continue
            url = re.sub(r"\{[^}]*\}", "x", path)
            for method in sorted(methods & verbs):
                count += 1
                r = self.client.request(method, url, **({"json": payload} if payload is not None else {}))
                if r.status_code >= 500 and r.status_code not in NOT_CONFIGURED:
                    bad.append((method, url, r.status_code, r.text[:150]))
        self.assertGreater(count, 50)
        return bad


class ApiSmokeTest(Base):
    def test_no_get_route_answers_500_on_a_first_run(self):
        self.assertEqual(self.walk({"GET"}), [])

    def test_no_write_route_answers_500_to_an_empty_or_junk_body(self):
        self.assertEqual(self.walk({"POST", "PUT", "PATCH", "DELETE"}, {}), [])
        self.assertEqual(self.walk({"POST", "PUT", "PATCH", "DELETE"}, {"id": 1, "text": None, "items": "x"}), [])


class LifecycleTest(unittest.TestCase):
    def test_start_and_stop_leave_nothing_running(self):
        home = tempfile.mkdtemp(prefix="hub-home-")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            app, _config, _root = make_app(real_assistant=True)
            before = {t.ident for t in threading.enumerate()}
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"}) as client:
                    client.post("/api/login", json={"token": TOKEN})
                    client.get("/api/status")
                gc.collect()
            problems = [str(w.message) for w in caught
                        if issubclass(w.category, (RuntimeWarning, ResourceWarning)) and
                        ("never awaited" in str(w.message) or "Task was destroyed" in str(w.message))]
            self.assertEqual(problems, [])
            import time
            for _ in range(30):                      # threads may need a moment to wind down
                left = [t for t in threading.enumerate() if t.ident not in before and t.is_alive() and not t.daemon]
                if not left:
                    break
                time.sleep(0.1)
            self.assertEqual([t.name for t in left], [], "non-daemon threads still running after shutdown")


if __name__ == "__main__":
    unittest.main()
