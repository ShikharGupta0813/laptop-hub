"""The combined shell poll (hub/ticker.py): one answer for panic, unread, alerts, spoken items and meetings, and a 304
while nothing changed. Plus static guards that no screen starts a timer without a visibility guard."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import TOKEN
from tests.test_bootcheck import make_app

JS = Path(__file__).resolve().parent.parent / "hub" / "static" / "js"


class TickerTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp(prefix="hub-home-")
        patcher = mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, self.config, self.root = make_app(real_assistant=True)
        self.client = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"}, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_needs_login(self):
        self.assertEqual(self.client.get("/api/ticker").status_code, 401)

    def test_combined_and_etag(self):
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)
        r = self.client.get("/api/ticker")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("unread", body)
        self.assertIsInstance(body["unread"], int)
        self.assertEqual(body["unread"], self.client.get("/api/notifications/unread-count").json()["count"])
        etag = r.headers["etag"]
        again = self.client.get("/api/ticker", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")


class TimerGuards(unittest.TestCase):
    def test_no_bare_background_intervals(self):
        """A setInterval in a screen/shell module must be visibility-guarded or cleared by its owner; the shared
        poller (poller.js) owns the always-on ones. Modules listed here are checked for a hidden guard nearby."""
        bad = []
        for name in ("main.js", "presence.js", "panic.js"):
            text = (JS / name).read_text(encoding="utf-8")
            for m in re.finditer(r"setInterval\(", text):
                window = text[max(0, m.start() - 160): m.end() + 260]
                if not re.search(r"document\.hidden|visibilityState|poller|onTick|subscribe", window):
                    bad.append(f"{name}: {text[m.start():m.start() + 70]!r}")
        self.assertEqual(bad, [], "background timers without a visibility guard")

    def test_poller_pauses_when_hidden(self):
        text = (JS / "poller.js").read_text(encoding="utf-8")
        self.assertIn("document.hidden", text)
        self.assertIn("visibilitychange", text)
        self.assertIn("/api/ticker", text)


if __name__ == "__main__":
    unittest.main()
