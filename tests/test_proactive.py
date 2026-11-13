"""Tests for Proactive (hub/proactive.py), the front page (hub/frontpage.py) and Friday's time-aware greeting.
Fakes only: fake clock, fake notifier, fake route caller. Nothing touches the network."""
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import persona, proactive
from hub.common import Ctx
from tests import test_today
from tests.helpers import make_config
from tests.test_autopilot import Clock, need_auth

NOON = dt.datetime(2026, 11, 9, 12, 0)
NIGHT = dt.datetime(2026, 11, 9, 23, 30)


class Classify(unittest.TestCase):
    def test_table(self):
        rows = [
            ({"type": "meeting", "minutes": 10}, "now"),
            ({"type": "meeting", "minutes": 0}, "now"),
            ({"type": "meeting", "minutes": 11}, "quiet"),
            ({"type": "meeting"}, "quiet"),
            ({"type": "reminder"}, "now"),
            ({"type": "mail", "urgent": True}, "now"),
            ({"type": "mail"}, "quiet"),
            ({"type": "asana", "urgent": True, "overdue_today": True}, "now"),
            ({"type": "asana", "urgent": True}, "quiet"),
            ({"type": "autopilot_ready"}, "batch"),
            ({"type": "research_done"}, "batch"),
            ({"type": "backup_done"}, "batch"),
            ({"type": "morning"}, "now"),
            ({"type": "weather"}, "quiet"),
            ({}, "quiet"),
        ]
        for event, want in rows:
            self.assertEqual(proactive.classify(event), want, event)

    def test_quiet_hours(self):
        self.assertEqual(proactive.classify({"type": "autopilot_ready"}, now=NIGHT), "quiet")
        self.assertEqual(proactive.classify({"type": "morning"}, now=NIGHT), "quiet")
        self.assertEqual(proactive.classify({"type": "mail", "urgent": True}, now=NIGHT), "quiet")
        self.assertEqual(proactive.classify({"type": "reminder"}, now=NIGHT), "now")
        self.assertEqual(proactive.classify({"type": "meeting", "minutes": 5}, now=NIGHT), "now")
        early = dt.datetime(2026, 11, 9, 6, 59)
        self.assertEqual(proactive.classify({"type": "autopilot_ready"}, now=early), "quiet")
        self.assertEqual(proactive.classify({"type": "autopilot_ready"}, now=dt.datetime(2026, 11, 9, 7, 0)), "batch")
        self.assertEqual(proactive.classify({"type": "autopilot_ready"}, now=NIGHT, quiet_start="23:45"), "batch")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proactive-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock(NOON)
        self.pushed = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None,
                       need_auth=need_auth, audit=lambda *a, **k: None,
                       notify=lambda title, body="", level="info", link=None, source="": self.pushed.append(
                           {"title": title, "body": body, "level": level, "link": link, "source": source}))
        self.app = FastAPI()
        self.app.include_router(proactive.make_router(ctx=self.ctx, clock=self.clock))
        self.engine = self.ctx.proactive
        self.client = TestClient(self.app, cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)


class Dispatch(Base):
    def test_now_pushes_immediately(self):
        self.assertEqual(self.engine.submit({"type": "reminder", "title": "Call Asha", "body": "From Asha"}), "now")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0]["title"], "Call Asha")
        self.assertEqual(self.pushed[0]["source"], "proactive")

    def test_backup_events(self):
        from hub import backup
        self.assertEqual(proactive.classify(backup.proactive_event(True, 2048)), "batch")
        self.assertEqual(proactive.classify(backup.proactive_event(False)), "now")
        self.assertEqual(self.engine.submit(backup.proactive_event(False)), "now")
        self.assertEqual(self.pushed[0]["title"], "Backup failed")
        self.assertEqual(self.engine.submit(backup.proactive_event(True, 2048)), "batch")
        self.assertEqual(self.pushed[1]["body"], "Chief, the backup is done.")

    def test_dedupe_key_fires_once(self):
        ev = {"type": "meeting", "minutes": 8, "title": "Standup", "dedupe": "m:abc"}
        self.assertEqual(self.engine.submit(ev), "now")
        self.assertEqual(self.engine.submit(ev), "duplicate")
        self.assertEqual(self.engine.submit({**ev, "dedupe": "m:other"}), "now")
        self.assertEqual(len(self.pushed), 2)

    def test_batching_window(self):
        self.engine.submit({"type": "autopilot_ready", "count": 2})
        self.assertEqual(len(self.pushed), 1)                       # first batch goes out at once
        self.assertEqual(self.pushed[0]["body"], "Chief, 2 Autopilot drafts are waiting for you.")
        self.clock.advance(minutes=5)
        self.engine.submit({"type": "research_done"})
        self.clock.advance(minutes=5)
        self.engine.submit({"type": "backup_done"})
        self.assertEqual(len(self.pushed), 1)                       # still inside the 30 minute window
        self.assertEqual(len(self.client.get("/api/proactive/state").json()["pending"]), 2)
        self.clock.advance(minutes=19)
        self.assertFalse(self.engine.flush())
        self.clock.advance(minutes=1)
        self.assertTrue(self.engine.flush())
        self.assertEqual(len(self.pushed), 2)                       # ONE push for both
        self.assertEqual(self.pushed[1]["body"], "Chief, your research is ready and the backup is done.")
        self.assertEqual(self.client.get("/api/proactive/state").json()["pending"], [])
        self.assertFalse(self.engine.flush())

    def test_batch_minutes_config(self):
        self.config.proactive_batch_minutes = 10
        self.engine.submit({"type": "backup_done"})
        self.clock.advance(minutes=11)
        self.engine.submit({"type": "backup_done"})
        self.assertEqual(len(self.pushed), 2)

    def test_quiet_hours_hold_everything_but_reminders(self):
        self.clock.now = NIGHT
        self.assertEqual(self.engine.submit({"type": "autopilot_ready"}), "quiet")
        self.assertEqual(self.engine.submit({"type": "mail", "urgent": True, "title": "Boss"}), "quiet")
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.engine.submit({"type": "reminder", "title": "Pills"}), "now")
        self.assertEqual(len(self.pushed), 1)
        events = self.client.get("/api/proactive/state").json()["events"]
        self.assertEqual([e["decision"] for e in events], ["now", "quiet", "quiet"])   # newest first

    def test_batch_waits_for_morning(self):
        self.clock.now = dt.datetime(2026, 11, 9, 21, 50)
        self.engine.submit({"type": "backup_done"})                 # goes out (first batch)
        self.clock.advance(minutes=2)
        self.engine.submit({"type": "research_done"})               # inside the window: waits
        self.clock.now = NIGHT
        self.assertFalse(self.engine.flush())                       # window passed, but quiet hours
        self.assertEqual(len(self.pushed), 1)
        self.clock.now = dt.datetime(2026, 11, 10, 7, 5)
        self.assertTrue(self.engine.flush())
        self.assertEqual(len(self.pushed), 2)

    def test_disabled(self):
        self.config.proactive_enabled = False
        self.engine.submit({"type": "backup_done"})
        self.assertFalse(self.engine.flush())
        self.assertFalse(self.client.get("/api/proactive/state").json()["enabled"])
        proactive.submit_or_notify(self.ctx, {"type": "backup_done", "title": "Backup", "body": "done"})
        self.assertEqual(self.pushed[-1]["title"], "Backup")       # falls back to a plain notification

    def test_morning_push_once_a_day(self):
        self.clock.now = dt.datetime(2026, 11, 9, 8, 29)
        self.assertFalse(self.engine.morning())
        self.clock.now = dt.datetime(2026, 11, 9, 8, 31)
        self.assertTrue(self.engine.morning())
        self.assertFalse(self.engine.morning())
        self.assertEqual(self.pushed[0]["body"], "Good morning, Chief. Your day is ready.")
        self.assertEqual(self.pushed[0]["link"], "/api/today/front-page")
        self.clock.now = dt.datetime(2026, 11, 10, 8, 31)
        self.assertTrue(self.engine.morning())

    def test_morning_off_and_never_in_quiet_hours(self):
        self.config.today_morning_time = ""
        self.assertFalse(self.engine.morning())
        self.config.today_morning_time = "06:00"
        self.clock.now = dt.datetime(2026, 11, 9, 6, 30)
        self.assertFalse(self.engine.morning())                     # held by quiet hours (still before 07:00)
        self.assertEqual(self.pushed, [])

    def test_routes_need_login(self):
        self.assertEqual(TestClient(self.app).get("/api/proactive/state").status_code, 401)

    def test_state_keeps_last_20(self):
        for i in range(25):
            self.engine.submit({"type": "weather", "title": f"w{i}"})
        events = self.client.get("/api/proactive/state").json()["events"]
        self.assertEqual(len(events), 20)
        self.assertEqual(events[0]["title"], "w24")


class FrontPage(test_today.Base):
    def test_front_page_escapes_and_is_self_contained(self):
        self.hub.tasks = [{"gid": "1", "name": "<script>alert(1)</script>", "due_on": "2026-11-09",
                           "project": "<img src=x onerror=y>", "completed": False},
                          {"gid": "2", "name": "Late \"one\" & co", "due_on": "2026-11-01", "project": "P",
                           "completed": False}]
        self.hub.meetings = [{"title": "<b>Sync</b>", "start": test_today.iso(16), "end": test_today.iso(17),
                              "all_day": False, "link": "", "platform": ""}]
        r = self.client.get("/api/today/front-page")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/html", r.headers["content-type"])
        html = r.text
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<img", html.lower())
        self.assertNotIn("<b>Sync", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("Late &quot;one&quot; &amp; co", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertIn("clear the overdue task first", html)
        self.assertIn("Next meetings", html)
        self.assertIn("Spend so far", html)
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])

    def test_front_page_needs_login_and_respects_switch(self):
        self.assertEqual(TestClient(self.app).get("/api/today/front-page").status_code, 401)
        self.config.today_enabled = False
        self.assertEqual(self.client.get("/api/today/front-page").status_code, 404)

    def test_empty_day(self):
        r = self.client.get("/api/today/front-page")
        self.assertIn("No meetings left today.", r.text)
        self.assertIn("a clear day", r.text)


class Greeting(unittest.TestCase):
    def test_time_aware(self):
        def at(hour):
            return persona.greeting_reply("hey friday", clock=lambda: dt.datetime(2026, 11, 9, hour, 0))
        self.assertTrue(at(8).startswith("Good morning, Chief!"))
        self.assertTrue(at(13).startswith("Good afternoon, Chief!"))
        self.assertTrue(at(19).startswith("Good evening, Chief!"))
        self.assertTrue(at(2).startswith("Good evening, Chief!"))
        self.assertIn("day", at(8))
        self.assertIn("At your service", persona.greeting_reply("hey jarvis"))


if __name__ == "__main__":
    unittest.main()
