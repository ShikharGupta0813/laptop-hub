"""Tests for reminders, timers and the quick parser. A fake clock drives everything; no sleeping, no network."""
import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import reminders
from hub.common import Ctx
from hub.reminders import ParseError, parse_quick
from hub.server import create_app
from tests.helpers import TOKEN, make_config

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=NY)      # a Wednesday


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class Clock:
    """Holds UTC so advancing is exact, even across a clock change."""

    def __init__(self, local: datetime = NOW):
        self.utc = local.astimezone(timezone.utc)

    def now(self):
        return self.utc

    def set(self, local_text: str):
        self.utc = datetime.fromisoformat(local_text).replace(tzinfo=NY).astimezone(timezone.utc)

    def advance(self, **kwargs):
        self.utc += timedelta(**kwargs)


def local(dt: datetime) -> str:
    return dt.astimezone(NY).strftime("%Y-%m-%d %H:%M")


class Base(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.config.data_dir = str(self.root / "data")
        self.build(Clock())

    def build(self, clock):
        """(Re)start the module the way the server does: new Ctx, same data folder."""
        self.clock = clock
        self.sent = []
        self.audits = []

        def notify(title, body="", level="info", link=None, source=""):
            self.sent.append({"title": title, "body": body, "level": level, "link": link, "source": source})

        self.ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda event, **fields: self.audits.append((event, fields)), notify=notify)
        self.router = reminders.make_router(self.ctx, clock=clock.now, tz=NY)
        self.service = self.router.service
        app = FastAPI()
        app.include_router(self.router)
        self.client = TestClient(app, base_url="http://testserver", cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)

    def add(self, text="Water plants", due="2026-11-04T10:05:00-05:00", **extra):
        response = self.client.post("/api/reminders", json={"text": text, "due": due, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def get(self, rid):
        return self.client.get(f"/api/reminders/{rid}").json()


# ---- the parser ---------------------------------------------------------------------------------------

PARSE_TABLE = [
    # phrase, due (local), repeat, n, text, echo
    ("remind me to call mom tomorrow at 9am", "2026-11-05 09:00", "none", None, "call mom", "Tomorrow 9:00 am — call mom"),
    ("in 10 minutes check oven", "2026-11-04 10:10", "none", None, "check oven", "Today 10:10 am — check oven"),
    ("every weekday 8:30 standup", "2026-11-05 08:30", "weekdays", None, "standup",
     "Every weekday 8:30 am (next: Tomorrow 8:30 am) — standup"),
    ("friday 5pm send report", "2026-11-06 17:00", "none", None, "send report", "Friday 5:00 pm — send report"),
    ("on 25 dec at noon buy gift", "2026-12-25 12:00", "none", None, "buy gift", "Fri 25 Dec 12:00 pm — buy gift"),
    ("Remind me at 5pm to leave work", "2026-11-04 17:00", "none", None, "leave work", "Today 5:00 pm — leave work"),
    ("call bob at 9", "2026-11-04 21:00", "none", None, "call bob", "Today 9:00 pm — call bob"),
    ("dentist tomorrow 2:30pm", "2026-11-05 14:30", "none", None, "dentist", "Tomorrow 2:30 pm — dentist"),
    ("tomorrow morning run", "2026-11-05 09:00", "none", None, "run", "Tomorrow 9:00 am — run"),
    ("tomorrow evening dinner with sam", "2026-11-05 18:00", "none", None, "dinner with sam", None),
    ("tonight call dad", "2026-11-04 20:00", "none", None, "call dad", None),
    ("tonight at 9 call dad", "2026-11-04 21:00", "none", None, "call dad", None),
    ("in an hour and a half make tea", "2026-11-04 11:30", "none", None, "make tea", None),
    ("in half an hour stretch", "2026-11-04 10:30", "none", None, "stretch", None),
    ("in two hours call the bank", "2026-11-04 12:00", "none", None, "call the bank", None),
    ("in 1 hour 15 minutes laundry", "2026-11-04 11:15", "none", None, "laundry", None),
    ("20 minutes from now check email", "2026-11-04 10:20", "none", None, "check email", None),
    ("in 2 days at 9am send card", "2026-11-06 09:00", "none", None, "send card", None),
    ("in 3 weeks renew passport", "2026-11-25 10:00", "none", None, "renew passport", None),
    ("in 2 months pay insurance", "2027-01-04 10:00", "none", None, "pay insurance", None),
    ("next friday lunch with jo", "2026-11-13 09:00", "none", None, "lunch with jo", "Fri 13 Nov 9:00 am — lunch with jo"),
    ("next monday plan the week", "2026-11-09 09:00", "none", None, "plan the week", None),
    ("on wednesday at 5pm gym", "2026-11-04 17:00", "none", None, "gym", None),
    ("on wednesday at 9am gym", "2026-11-11 09:00", "none", None, "gym", None),
    ("monday at 9 gym", "2026-11-09 09:00", "none", None, "gym", None),
    ("day after tomorrow 4pm parcel", "2026-11-06 16:00", "none", None, "parcel", None),
    ("tmrw 8am bins", "2026-11-05 08:00", "none", None, "bins", None),
    ("every monday 9am gym", "2026-11-09 09:00", "weekly", None, "gym", None),
    ("gym on mondays at 6pm", "2026-11-09 18:00", "weekly", None, "gym", None),
    ("daily 7:30 vitamins", "2026-11-05 07:30", "daily", None, "vitamins", None),
    ("every day at 9pm journal", "2026-11-04 21:00", "daily", None, "journal", None),
    ("remind me to take pills every morning", "2026-11-05 09:00", "daily", None, "take pills", None),
    ("every night at 10 lock up", "2026-11-04 22:00", "daily", None, "lock up", None),
    ("stretch every morning at 8", "2026-11-05 08:00", "daily", None, "stretch", None),
    ("monthly on the 31st pay rent", "2026-12-31 09:00", "monthly", None, "pay rent", None),
    ("pay rent on the 1st of every month", "2026-12-01 09:00", "monthly", None, "pay rent", None),
    ("every month on the 15th at 10am invoice", "2026-11-15 10:00", "monthly", None, "invoice", None),
    ("every year 3 march birthday", "2027-03-03 09:00", "yearly", None, "birthday", None),
    ("annually on 4 nov renew domain", "2027-11-04 09:00", "yearly", None, "renew domain", None),
    ("every 30 minutes stretch", "2026-11-04 10:30", "every_n_minutes", 30, "stretch", "Every 30 minutes — stretch"),
    ("every 2 hours drink water", "2026-11-04 12:00", "every_n_minutes", 120, "drink water", "Every 2 hours — drink water"),
    ("every hour stand up", "2026-11-04 11:00", "every_n_minutes", 60, "stand up", None),
    ("alarm 6:30 weekdays", "2026-11-05 06:30", "weekdays", None, "Alarm", None),
    ("wake me up at 7", "2026-11-05 07:00", "none", None, "Alarm", "Tomorrow 7:00 am — Alarm"),
    ("set an alarm for 6:45am", "2026-11-05 06:45", "none", None, "Alarm", None),
    ("dec 25 2027 9am xmas", "2027-12-25 09:00", "none", None, "xmas", None),
    ("2026-12-01 10:00 book flight", "2026-12-01 10:00", "none", None, "book flight", None),
    ("on the 15th at 2:30pm dentist", "2026-11-15 14:30", "none", None, "dentist", None),
    ("on the 3rd call landlord", "2026-12-03 09:00", "none", None, "call landlord", None),
    ("1 jan 8am party", "2027-01-01 08:00", "none", None, "party", None),
    ("25/12 buy gift", "2026-12-25 09:00", "none", None, "buy gift", None),
    ("12/25 buy gift", "2026-12-25 09:00", "none", None, "buy gift", None),
    ("29 feb 2028 leap day", "2028-02-29 09:00", "none", None, "leap day", None),
    ("at 7 take pills", "2026-11-04 19:00", "none", None, "take pills", None),
    ("half past 3 tea", "2026-11-04 15:30", "none", None, "tea", None),
    ("quarter to 4 pick up kids", "2026-11-04 15:45", "none", None, "pick up kids", None),
    ("at midnight happy new year", "2026-11-05 00:00", "none", None, "happy new year", None),
    ("tomorrow at noon call", "2026-11-05 12:00", "none", None, "call", None),
    ("by eod send report", "2026-11-04 17:00", "none", None, "send report", None),
    ("at 17:30 train home", "2026-11-04 17:30", "none", None, "train home", None),
    ("5 o'clock tea", "2026-11-04 17:00", "none", None, "tea", None),
    ("at 9.30pm film", "2026-11-04 21:30", "none", None, "film", None),
    ("Please remind me about the meeting friday at 10 a.m.", "2026-11-06 10:00", "none", None, "the meeting", None),
    ("don't forget to water the plants tomorrow 7am", "2026-11-05 07:00", "none", None, "water the plants", None),
    ("get 2 things at 5", "2026-11-04 17:00", "none", None, "get 2 things", None),
]

PARSE_ERRORS = [
    ("", "Type what you want"),
    ("remind me", "What should I remind you about"),
    ("buy milk", "could not tell when"),
    ("call mom later", "instead of 'later'"),
    ("remind me next week to call", "next week"),
    ("call mom this weekend", "Saturday or Sunday"),
    ("call mom today", "What time today"),
    ("call mom at 8am today", "already passed"),
    ("this morning at 8 call dad", "already passed"),
    ("call mom yesterday 5pm", "past"),
    ("3/4 pick up", "cannot tell if 3/4"),
    ("31 feb call", "does not have a day 31"),
    ("25 dec 2020 old", "already passed"),
    ("every other day water", "not supported"),
    ("every 3 days water", "not supported"),
    ("every fortnight x", "did not understand"),
    ("call mom friday and sunday at 5pm", "more than one date"),
    ("meet at 9 and also at 10", "more than one time"),
    ("every weekday in 2 hours x", "when it repeats"),
    ("check in 5 minutes at 9pm", "not both"),
    ("remind me friday in 2 hours x", "not both"),
    ("meet monthly at 9am", "Which day of the month"),
    ("standup weekly 9am", "Which day of the week"),
    ("party yearly 9am", "Which date each year"),
    ("x every 5 minutes at 5pm", "not both"),
    ("in 30 seconds call", "under a minute"),
    ("call at 13pm", "12-hour clock"),
    ("call at 25:00", "does not look right"),
    ("x" * 600, "too long"),
    ("timer", "How long"),
]


class ParserTest(unittest.TestCase):
    def test_table(self):
        for phrase, due, repeat, n, text, echo in PARSE_TABLE:
            with self.subTest(phrase=phrase):
                parsed = parse_quick(phrase, NOW, NY)
                self.assertEqual(local(parsed.due), due)
                self.assertEqual((parsed.repeat, parsed.n, parsed.text), (repeat, n, text))
                if echo:
                    self.assertEqual(parsed.echo, echo)
                self.assertIn(" — ", parsed.echo)

    def test_errors_are_plain_sentences(self):
        for phrase, fragment in PARSE_ERRORS:
            with self.subTest(phrase=phrase[:40]):
                with self.assertRaises(ParseError) as caught:
                    parse_quick(phrase, NOW, NY)
                self.assertIn(fragment, str(caught.exception))
                self.assertNotIn("Traceback", str(caught.exception))

    def test_the_time_is_not_taken_from_the_task_text(self):
        parsed = parse_quick("remind me to buy 5 apples tomorrow at 9am", NOW, NY)
        self.assertEqual((parsed.text, local(parsed.due)), ("buy 5 apples", "2026-11-05 09:00"))
        parsed = parse_quick("call 911 test in 5 minutes", NOW, NY)
        self.assertEqual(parsed.text, "call 911 test")

    def test_a_time_later_today_stays_today_and_an_earlier_one_means_tomorrow(self):
        self.assertEqual(local(parse_quick("call at 5pm", NOW, NY).due), "2026-11-04 17:00")
        self.assertEqual(local(parse_quick("call at 8am", NOW, NY).due), "2026-11-05 08:00")

    def test_a_day_name_on_that_day_rolls_to_next_week_when_the_time_has_passed(self):
        self.assertEqual(local(parse_quick("on wednesday at 9am x", NOW, NY).due), "2026-11-11 09:00")

    def test_timers(self):
        for phrase, seconds, label in (("timer 10 minutes pasta", 600, "pasta"), ("set a timer for 90 seconds", 90, "Timer"),
                                       ("start a 25-minute timer for focus", 1500, "focus"),
                                       ("set a timer for 1 hour 30 minutes", 5400, "Timer")):
            with self.subTest(phrase=phrase):
                parsed = parse_quick(phrase, NOW, NY)
                self.assertEqual((parsed.kind, parsed.seconds, parsed.text), ("timer", seconds, label))
        with self.assertRaises(ParseError):
            parse_quick("timer 3 months", NOW, NY)

    def test_dst_gap_moves_the_time_forward_instead_of_failing(self):
        # 2 Mar 2026 + 6 days is Sunday 8 March, when 02:30 does not exist in New York
        now = datetime(2026, 3, 8, 0, 30, tzinfo=NY)
        parsed = parse_quick("today at 2:30 am wake", now, NY)
        self.assertEqual(local(parsed.due), "2026-03-08 03:30")


class RecurrenceTest(unittest.TestCase):
    def nxt(self, anchor, repeat, after, n=None, due=None):
        anchor = datetime.fromisoformat(anchor)
        after = datetime.fromisoformat(after).replace(tzinfo=NY)
        return local(reminders.next_occurrence(anchor, repeat, n, after, NY, due=due))

    def test_monthly_on_the_31st_clamps_then_recovers(self):
        self.assertEqual(self.nxt("2026-01-31T09:00", "monthly", "2026-01-31T09:00"), "2026-02-28 09:00")
        self.assertEqual(self.nxt("2026-01-31T09:00", "monthly", "2026-02-28T09:00"), "2026-03-31 09:00")
        self.assertEqual(self.nxt("2026-01-31T09:00", "monthly", "2026-03-31T09:00"), "2026-04-30 09:00")
        self.assertEqual(self.nxt("2026-01-31T09:00", "monthly", "2027-01-15T00:00"), "2027-01-31 09:00")

    def test_yearly_on_29_feb(self):
        self.assertEqual(self.nxt("2028-02-29T08:00", "yearly", "2028-02-29T08:00"), "2029-02-28 08:00")
        self.assertEqual(self.nxt("2028-02-29T08:00", "yearly", "2031-03-01T00:00"), "2032-02-29 08:00")

    def test_weekdays_skip_the_weekend(self):
        self.assertEqual(self.nxt("2026-11-02T08:30", "weekdays", "2026-11-06T08:30"), "2026-11-09 08:30")
        self.assertEqual(self.nxt("2026-11-02T08:30", "weekdays", "2026-11-07T12:00"), "2026-11-09 08:30")

    def test_daily_keeps_the_wall_clock_across_the_spring_clock_change(self):
        due = reminders.next_occurrence(datetime(2026, 3, 7, 8, 30), "daily", None,
                                        datetime(2026, 3, 7, 8, 30, tzinfo=NY), NY)
        self.assertEqual(due.isoformat(), "2026-03-08T08:30:00-04:00")
        gap = reminders.next_occurrence(datetime(2026, 3, 7, 2, 30), "daily", None,
                                        datetime(2026, 3, 7, 2, 30, tzinfo=NY), NY)
        self.assertEqual(local(gap), "2026-03-08 03:30")      # 02:30 does not exist that day
        after_gap = reminders.next_occurrence(datetime(2026, 3, 7, 2, 30), "daily", None, gap, NY)
        self.assertEqual(local(after_gap), "2026-03-09 02:30")  # and the original time comes back

    def test_every_n_minutes_lands_on_the_grid(self):
        due = datetime(2026, 11, 4, 10, 0, tzinfo=NY)
        later = datetime(2026, 11, 4, 10, 47, tzinfo=NY)
        self.assertEqual(local(reminders.next_occurrence(due.replace(tzinfo=None), "every_n_minutes", 15, later, NY, due=due)),
                         "2026-11-04 11:00")

    def test_a_huge_gap_is_fast(self):
        anchor = datetime(2026, 1, 1, 9, 0)
        after = datetime(2099, 12, 1, tzinfo=NY)
        self.assertEqual(local(reminders.next_occurrence(anchor, "daily", None, after, NY)), "2099-12-01 09:00")


class CrudTest(Base):
    def test_create_list_update_delete(self):
        made = self.add("Call mom", "2026-11-05T09:00:00-05:00")
        self.assertEqual((made["repeat"], made["done"], made["status"], made["source"]), ("none", False, "upcoming", "app"))
        self.assertEqual(made["when"], "Tomorrow 9:00 am")
        self.assertEqual([r["id"] for r in self.client.get("/api/reminders").json()["reminders"]], [made["id"]])
        patched = self.client.patch(f"/api/reminders/{made['id']}", json={"text": "Call Mum", "due": "2026-11-05T10:00:00"}).json()
        self.assertEqual((patched["text"], patched["due"]), ("Call Mum", "2026-11-05T10:00:00-05:00"))
        self.assertEqual(self.client.delete(f"/api/reminders/{made['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/reminders/{made['id']}").status_code, 404)
        self.assertIn(("reminder_deleted", {"id": made["id"]}), self.audits)

    def test_a_time_with_no_offset_is_local_and_z_is_understood(self):
        self.assertEqual(self.add(due="2026-11-05T09:00")["due"], "2026-11-05T09:00:00-05:00")
        self.assertEqual(self.add(due="2026-11-05T14:00:00Z")["due"], "2026-11-05T09:00:00-05:00")

    def test_validation_gives_plain_sentences(self):
        bad = [({"text": "", "due": "2026-11-05T09:00"}, "what the reminder is about"),
               ({"text": "x" * 301, "due": "2026-11-05T09:00"}, "too long"),
               ({"text": "x", "due": "tomorrow"}, "date and time"),
               ({"text": "x", "due": "2026-11-04T09:00"}, "already passed"),
               ({"text": "x", "due": "1999-01-01T00:00"}, "date and time"),
               ({"text": "x", "due": "2026-11-05T09:00", "repeat": "hourly"}, "repeat must be"),
               ({"text": "x", "due": "2026-11-05T09:00", "repeat": "every_n_minutes"}, "how many minutes"),
               ({"text": "x", "due": "2026-11-05T09:00", "repeat": "every_n_minutes", "n": 0}, "how many minutes"),
               ({"text": ["x"], "due": "2026-11-05T09:00"}, "what the reminder is about")]
        for body, fragment in bad:
            with self.subTest(body=body):
                response = self.client.post("/api/reminders", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(fragment, response.json()["detail"])

    def test_control_characters_are_stripped_from_text(self):
        made = self.add("Line\none\x00\x07  two", "2026-11-05T09:00")
        self.assertEqual(made["text"], "Line one two")

    def test_ids_are_validated(self):
        for rid in ("zzz", "..%2f..%2fetc", "ABCDEF123456", "1234", "g" * 12):
            with self.subTest(rid=rid):
                self.assertEqual(self.client.get(f"/api/reminders/{rid}").status_code, 404)
                self.assertEqual(self.client.post(f"/api/reminders/{rid}/done").status_code, 404)
                self.assertEqual(self.client.delete(f"/api/reminders/{rid}").status_code, 404)

    def test_everything_needs_a_login(self):
        anonymous = TestClient(self.client.app, base_url="http://testserver")
        for method, path in (("get", "/api/reminders"), ("post", "/api/reminders"), ("post", "/api/reminders/quick"),
                             ("get", "/api/reminders/timers"), ("post", "/api/reminders/timers"),
                             ("get", "/api/reminders/search?q=a"), ("delete", "/api/reminders/abcdef123456")):
            with self.subTest(path=path):
                self.assertEqual(getattr(anonymous, method)(path).status_code, 401)

    def test_a_repeating_reminder_created_in_the_past_starts_at_its_next_time(self):
        made = self.add("Standup", "2026-11-02T08:30:00-05:00", repeat="weekdays")
        self.assertEqual(made["due"], "2026-11-05T08:30:00-05:00")

    def test_a_weekend_start_for_weekdays_moves_to_monday(self):
        made = self.add("Standup", "2026-11-07T08:30:00-05:00", repeat="weekdays")
        self.assertEqual(made["due"], "2026-11-09T08:30:00-05:00")

    def test_list_order_and_status_filter(self):
        later = self.add("later", "2026-11-09T09:00")
        soon = self.add("soon", "2026-11-05T09:00")
        done = self.add("finished", "2026-11-06T09:00")
        self.client.post(f"/api/reminders/{done['id']}/done")
        self.assertEqual([r["text"] for r in self.client.get("/api/reminders").json()["reminders"]], ["soon", "later"])
        self.assertEqual([r["text"] for r in self.client.get("/api/reminders?status=done").json()["reminders"]], ["finished"])
        self.assertEqual([r["text"] for r in self.client.get("/api/reminders?status=all").json()["reminders"]],
                         ["soon", "later", "finished"])
        self.assertEqual(self.client.get("/api/reminders?status=weird").status_code, 400)
        self.assertEqual(later["status"], "upcoming")
        self.assertEqual(soon["status"], "upcoming")

    def test_too_many_reminders_are_refused(self):
        with mock.patch.object(reminders, "MAX_REMINDERS", 2):
            self.add("a", "2026-11-05T09:00")
            self.add("b", "2026-11-05T09:00")
            self.assertEqual(self.client.post("/api/reminders", json={"text": "c", "due": "2026-11-05T09:00"}).status_code, 400)

    def test_search(self):
        self.add("Call the dentist", "2026-11-05T09:00")
        self.add("Buy milk", "2026-11-05T09:00")
        results = self.client.get("/api/reminders/search?q=DENT").json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["title"], results[0]["kind"]), ("Call the dentist", "reminder"))
        self.assertEqual(results[0]["open"]["view"], "reminders")
        self.assertEqual(self.client.get("/api/reminders/search?q=").json(), {"results": []})
        self.assertEqual(self.client.get("/api/reminders/search?q=zzz").json(), {"results": []})


class QuickRouteTest(Base):
    def test_quick_saves_and_echoes(self):
        data = self.client.post("/api/reminders/quick", json={"text": "remind me to call mom tomorrow at 9am"}).json()
        self.assertEqual(data["echo"], "Tomorrow 9:00 am — call mom")
        self.assertTrue(data["saved"])
        self.assertEqual((data["reminder"]["text"], data["reminder"]["source"]), ("call mom", "quick"))
        self.assertEqual(len(self.client.get("/api/reminders").json()["reminders"]), 1)

    def test_preview_saves_nothing(self):
        data = self.client.post("/api/reminders/quick", json={"text": "in 10 minutes check oven", "preview": True}).json()
        self.assertFalse(data["saved"])
        self.assertEqual(data["reminder"]["due"], "2026-11-04T10:10:00-05:00")
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"], [])

    def test_ambiguous_phrase_is_a_400_with_a_sentence(self):
        response = self.client.post("/api/reminders/quick", json={"text": "call mom"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("could not tell when", response.json()["detail"])
        self.assertEqual(self.client.post("/api/reminders/quick", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/reminders/quick", json={"text": 5}).status_code, 400)

    def test_a_timer_phrase_starts_a_timer(self):
        data = self.client.post("/api/reminders/quick", json={"text": "timer 10 minutes pasta"}).json()
        self.assertEqual((data["kind"], data["timer"]["seconds"], data["timer"]["label"]), ("timer", 600, "pasta"))
        self.assertEqual(len(self.client.get("/api/reminders/timers").json()["timers"]), 1)


class SchedulerTest(Base):
    def test_fires_exactly_once_with_the_right_notification(self):
        self.add("Water plants", "2026-11-04T10:05:00-05:00")
        self.clock.advance(minutes=4)
        self.assertEqual(self.service.tick(), [])
        self.clock.advance(minutes=1, seconds=5)
        self.assertEqual(len(self.service.tick()), 1)
        self.assertEqual(self.sent, [{"title": "Reminder: Water plants", "body": "Due Today 10:05 am.", "level": "info",
                                      "link": "reminders", "source": "reminders"}])
        for _ in range(5):
            self.clock.advance(seconds=15)
            self.assertEqual(self.service.tick(), [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"][0]["status"], "fired")

    def test_a_restart_does_not_fire_it_again_and_keeps_the_data(self):
        made = self.add("Water plants", "2026-11-04T10:05:00-05:00")
        self.clock.advance(minutes=5, seconds=5)
        self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.build(self.clock)                       # the hub restarts
        self.assertEqual(self.get(made["id"])["text"], "Water plants")
        self.clock.advance(minutes=10)
        self.assertEqual(self.service.tick(), [])
        self.assertEqual(self.sent, [])

    def test_an_unfired_reminder_survives_a_restart_and_then_fires_once(self):
        self.add("Later", "2026-11-04T11:00:00-05:00")
        self.build(self.clock)
        self.clock.advance(hours=1)
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(self.sent), 1)

    def test_a_notifier_that_breaks_does_not_stop_the_clock(self):
        self.add("a", "2026-11-04T10:05:00-05:00")
        self.ctx.notify = mock.Mock(side_effect=RuntimeError("boom"))
        self.clock.advance(minutes=6)
        with self.assertLogs("hub.reminders", level="WARNING"):
            self.assertEqual(len(self.service.tick()), 1)
        self.assertEqual(self.service.tick(), [])

    def test_late_means_missed_wording(self):
        self.add("Pay bill", "2026-11-04T10:05:00-05:00")
        self.clock.advance(minutes=5, seconds=10)           # an ordinary tick a few seconds late
        self.service.tick()
        self.assertTrue(self.sent[-1]["title"].startswith("Reminder:"))
        self.add("Call bank", "2026-11-04T10:30:00-05:00")
        self.clock.advance(minutes=60)                        # the laptop was asleep
        self.service.tick()
        self.assertEqual(self.sent[-1]["title"], "Missed reminder: Call bank")
        self.assertIn("was due Today 10:30 am", self.sent[-1]["body"])

    def test_catch_up_never_announces_more_than_ten(self):
        for i in range(15):
            self.add(f"item {i}", f"2026-11-04T{10 + i // 5}:{(i % 5) * 10 + 5:02d}:00-05:00")
        self.clock.advance(days=2)
        self.service.tick()
        titles = [n["title"] for n in self.sent]
        self.assertEqual(len(titles), 11)
        self.assertTrue(all(t.startswith("Missed reminder:") for t in titles[:10]))
        self.assertEqual(titles[10], "More missed reminders")
        self.assertIn("5 more", self.sent[10]["body"])
        self.assertEqual(titles[0], "Missed reminder: item 14")      # the most recent ones are named
        self.sent.clear()
        self.clock.advance(seconds=15)
        self.assertEqual(self.service.tick(), [])                     # all 15 are settled
        self.assertEqual(self.sent, [])

    def test_a_repeating_reminder_missed_several_times_fires_once_and_moves_on(self):
        self.add("Vitamins", "2026-11-05T07:30:00-05:00", repeat="daily")
        self.clock.advance(days=3, hours=2)
        self.service.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0]["title"].startswith("Missed reminder"))
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"][0]["due"], "2026-11-08T07:30:00-05:00")

    def test_daily_repeat_advances_and_fires_each_day(self):
        self.add("Vitamins", "2026-11-04T10:30:00-05:00", repeat="daily")
        for day in range(3):
            self.clock.set(f"2026-11-{4 + day:02d}T10:30:05")
            self.service.tick()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"][0]["due"], "2026-11-07T10:30:00-05:00")

    def test_weekdays_repeat_jumps_the_weekend(self):
        self.add("Standup", "2026-11-06T08:30:00-05:00", repeat="weekdays")
        self.clock.set("2026-11-06T08:30:10")
        self.service.tick()
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"][0]["due"], "2026-11-09T08:30:00-05:00")

    def test_monthly_on_the_31st_across_month_ends(self):
        self.clock.set("2026-01-01T09:00:00")
        self.build(self.clock)
        self.add("Rent", "2026-01-31T09:00:00-05:00", repeat="monthly")
        seen = []
        for stamp in ("2026-01-31T09:00:10", "2026-02-28T09:00:10", "2026-03-31T09:00:10"):
            self.clock.set(stamp)
            self.service.tick()
            seen.append(self.client.get("/api/reminders").json()["reminders"][0]["due"][:16])
        self.assertEqual(seen, ["2026-02-28T09:00", "2026-03-31T09:00", "2026-04-30T09:00"])
        self.assertEqual(len(self.sent), 3)

    def test_daily_stays_at_the_same_local_time_over_the_clock_change(self):
        self.clock.set("2026-03-07T07:00:00")
        self.build(self.clock)
        self.add("Run", "2026-03-07T08:30:00-05:00", repeat="daily")
        self.clock.set("2026-03-07T08:30:10")
        self.service.tick()
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"][0]["due"], "2026-03-08T08:30:00-04:00")

    def test_every_n_minutes(self):
        self.add("Stretch", "2026-11-04T10:15:00-05:00", repeat="every_n_minutes", n=15)
        for _ in range(4):
            self.clock.advance(minutes=15)
            self.service.tick()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"][0]["due"], "2026-11-04T11:15:00-05:00")

    def test_next_wake_is_never_longer_than_fifteen_seconds(self):
        self.assertEqual(self.service.seconds_until_next(), reminders.TICK_SECONDS)
        self.add("soon", "2026-11-04T10:00:40-05:00")
        self.assertAlmostEqual(self.service.seconds_until_next(), 15.0)
        self.clock.advance(seconds=30)
        self.assertAlmostEqual(self.service.seconds_until_next(), 10.0)


class SnoozeAndDoneTest(Base):
    def test_snooze_delays_and_fires_again_once(self):
        made = self.add("Call", "2026-11-04T10:05:00-05:00")
        self.clock.advance(minutes=5, seconds=1)
        self.service.tick()
        snoozed = self.client.post(f"/api/reminders/{made['id']}/snooze", json={"minutes": 10}).json()
        self.assertEqual((snoozed["status"], snoozed["snoozed_until"]), ("snoozed", "2026-11-04T10:15:01-05:00"))
        self.clock.advance(minutes=9)
        self.assertEqual(self.service.tick(), [])
        self.clock.advance(minutes=1, seconds=5)
        self.assertEqual(len(self.service.tick()), 1)
        self.assertEqual(self.service.tick(), [])
        self.assertEqual(len(self.sent), 2)

    def test_snooze_before_it_is_due_pushes_it_later(self):
        made = self.add("Call", "2026-11-04T10:05:00-05:00")
        self.client.post(f"/api/reminders/{made['id']}/snooze", json={"minutes": 30})
        self.clock.advance(minutes=20)
        self.assertEqual(self.service.tick(), [])
        self.clock.advance(minutes=11)
        self.assertEqual(len(self.service.tick()), 1)

    def test_snooze_defaults_to_ten_minutes_and_is_validated(self):
        made = self.add("Call", "2026-11-04T10:05:00-05:00")
        self.assertEqual(self.client.post(f"/api/reminders/{made['id']}/snooze", json={}).json()["snoozed_until"],
                         "2026-11-04T10:10:00-05:00")
        for minutes in (0, -5, 10081, "ten", 1.5, True):
            with self.subTest(minutes=minutes):
                self.assertEqual(self.client.post(f"/api/reminders/{made['id']}/snooze", json={"minutes": minutes}).status_code, 400)

    def test_done_stops_it_and_a_done_reminder_is_not_fired(self):
        made = self.add("Call", "2026-11-04T10:05:00-05:00")
        done = self.client.post(f"/api/reminders/{made['id']}/done").json()
        self.assertEqual((done["done"], done["status"]), (True, "done"))
        self.clock.advance(hours=1)
        self.assertEqual(self.service.tick(), [])

    def test_done_on_a_repeating_one_that_just_fired_only_acknowledges(self):
        made = self.add("Standup", "2026-11-04T10:30:00-05:00", repeat="daily")
        self.clock.advance(minutes=30, seconds=5)
        self.service.tick()
        after = self.client.post(f"/api/reminders/{made['id']}/done").json()
        self.assertEqual((after["done"], after["due"]), (False, "2026-11-05T10:30:00-05:00"))

    def test_done_on_a_repeating_one_before_it_fires_skips_that_occurrence(self):
        made = self.add("Standup", "2026-11-04T10:30:00-05:00", repeat="daily")
        after = self.client.post(f"/api/reminders/{made['id']}/done").json()
        self.assertEqual((after["done"], after["due"]), (False, "2026-11-05T10:30:00-05:00"))
        self.clock.advance(minutes=40)
        self.assertEqual(self.service.tick(), [])

    def test_done_can_be_undone_with_patch_and_old_done_items_are_pruned(self):
        made = self.add("Call", "2026-11-04T10:05:00-05:00")
        self.client.post(f"/api/reminders/{made['id']}/done")
        self.assertFalse(self.client.patch(f"/api/reminders/{made['id']}", json={"done": False}).json()["done"])
        self.client.post(f"/api/reminders/{made['id']}/done")
        self.clock.advance(days=31)
        self.service.tick()
        self.assertEqual(self.client.get("/api/reminders?status=all").json()["reminders"], [])

    def test_changing_the_time_of_a_fired_reminder_arms_it_again(self):
        made = self.add("Call", "2026-11-04T10:05:00-05:00")
        self.clock.advance(minutes=6)
        self.service.tick()
        self.client.patch(f"/api/reminders/{made['id']}", json={"due": "2026-11-04T11:00:00-05:00"})
        self.clock.advance(minutes=55)
        self.assertEqual(len(self.service.tick()), 1)


class TimerTest(Base):
    def test_countdown_fires_once_and_survives_restart(self):
        timer = self.client.post("/api/reminders/timers", json={"seconds": 300, "label": "Pasta"}).json()
        self.assertEqual((timer["state"], timer["remaining_seconds"], timer["label"]), ("running", 300, "Pasta"))
        self.clock.advance(seconds=100)
        self.assertEqual(self.client.get("/api/reminders/timers").json()["timers"][0]["remaining_seconds"], 200)
        self.build(self.clock)
        self.assertEqual(len(self.client.get("/api/reminders/timers").json()["timers"]), 1)
        self.clock.advance(seconds=201)
        self.assertEqual(len(self.service.tick()), 1)
        self.assertEqual(self.sent[0]["title"], "Timer done: Pasta")
        self.assertEqual((self.sent[0]["link"], self.sent[0]["source"], self.sent[0]["level"]), ("reminders", "reminders", "info"))
        self.assertEqual(self.service.tick(), [])
        self.assertEqual(self.client.get("/api/reminders/timers").json()["timers"][0]["state"], "finished")
        self.build(self.clock)
        self.clock.advance(minutes=1)
        self.assertEqual(self.service.tick(), [])

    def test_a_timer_that_ended_while_the_hub_was_off_says_missed(self):
        self.client.post("/api/reminders/timers", json={"seconds": 60})
        self.clock.advance(hours=1)
        self.service.tick()
        self.assertEqual(self.sent[0]["title"], "Missed timer: Timer")

    def test_default_label_validation_and_cancel(self):
        timer = self.client.post("/api/reminders/timers", json={"seconds": 30}).json()
        self.assertEqual(timer["label"], "Timer")
        for body in ({}, {"seconds": 0}, {"seconds": -1}, {"seconds": 8 * 86400}, {"seconds": "60"}, {"seconds": True},
                     {"seconds": 5, "label": "x" * 400}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/reminders/timers", json=body).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/reminders/timers/{timer['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/reminders/timers/{timer['id']}").status_code, 404)
        self.assertEqual(self.client.delete("/api/reminders/timers/not-an-id").status_code, 404)
        self.clock.advance(minutes=5)
        self.assertEqual(self.service.tick(), [])

    def test_the_timer_limit(self):
        with mock.patch.object(reminders, "MAX_TIMERS", 2):
            for _ in range(2):
                self.client.post("/api/reminders/timers", json={"seconds": 60})
            self.assertEqual(self.client.post("/api/reminders/timers", json={"seconds": 60}).status_code, 400)

    def test_timers_wake_the_loop_early(self):
        self.client.post("/api/reminders/timers", json={"seconds": 3})
        self.assertAlmostEqual(self.service.seconds_until_next(), 3.0)


class LifecycleTest(Base):
    def test_start_runs_the_clock_and_stop_ends_it(self):
        self.add("Overdue soon", "2026-11-04T10:00:30-05:00")
        self.clock.advance(minutes=5)

        async def scenario():
            stop = await reminders.start(self.ctx)
            await asyncio.sleep(0.1)
            await stop()

        asyncio.run(asyncio.wait_for(scenario(), 10))
        self.assertEqual(len(self.sent), 1)

    def test_demo_data_is_seeded_once(self):
        async def run_start():
            stop = await reminders.start(self.ctx)
            await stop()

        with mock.patch.dict(os.environ, {"HUB_DEMO": "1"}):
            asyncio.run(run_start())
            texts = [r["text"] for r in self.client.get("/api/reminders").json()["reminders"]]
            self.assertEqual(sorted(texts), ["Call mom", "Send report", "Standup"])
            for r in self.client.get("/api/reminders").json()["reminders"]:
                self.client.delete(f"/api/reminders/{r['id']}")
            self.build(self.clock)
            asyncio.run(run_start())
            self.assertEqual(self.client.get("/api/reminders").json()["reminders"], [])
        asyncio.run(run_start())
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"], [])

    def test_a_damaged_data_file_does_not_break_startup(self):
        Path(self.config.data_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.config.data_dir) / "reminders.json").write_text('{"reminders": [1, {"id": "x"}, null], "timers": "no"}')
        self.build(self.clock)
        self.assertEqual(self.client.get("/api/reminders").json()["reminders"], [])


class FullAppTest(unittest.TestCase):
    def test_the_real_server_serves_it_and_a_finished_timer_reaches_the_notification_center(self):
        config, root = make_config()
        config.data_dir = str(root / "data")
        app = create_app(config, audit_path=root / "audit.log")
        with TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"}) as client:   # runs start()
            self.assertEqual(client.get("/api/reminders").status_code, 401)
            client.post("/api/login", json={"token": TOKEN})
            quick = client.post("/api/reminders/quick", json={"text": "in 10 minutes check oven"})
            self.assertEqual(quick.status_code, 200, quick.text)
            self.assertEqual(client.get("/api/reminders/search?q=oven").json()["results"][0]["kind"], "reminder")
            self.assertEqual(client.post("/api/reminders/quick", json={"text": "call mom"}).status_code, 400)
            bare = TestClient(app, base_url="http://testserver", cookies=client.cookies)
            self.assertEqual(bare.post("/api/reminders/timers", json={"seconds": 5}).status_code, 403)
            self.assertEqual(client.post("/api/reminders/timers", json={"seconds": 1, "label": "Eggs"}).status_code, 200)
            found = []
            for _ in range(40):
                found = client.get("/api/notifications?source=reminders").json()["notifications"]
                if found:
                    break
                time.sleep(0.25)
        self.assertEqual([n["title"] for n in found], ["Timer done: Eggs"])
        self.assertEqual(found[0]["link"], "reminders")


if __name__ == "__main__":
    unittest.main()
