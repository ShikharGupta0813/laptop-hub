"""Tests for the interview scheduling helper, recruiter call card and referral drafts (hub/jobassist.py).
Fake calendar, fake clock, fake mail. Invented companies and people. No network, no AI, no real mail or calendar."""
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import jobassist, jobtrack, replies
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth
from tests.test_jobtrack import FakeApplyq, FakeJobprefs, FakeReminders, PROFILE, START
from tests.test_replies import FakeMail, mail

IST = jobassist.IST
MON = dt.datetime(2026, 11, 23, 9, 0, tzinfo=IST)       # a Monday
FRI = dt.datetime(2026, 11, 27, 14, 0, tzinfo=IST)


def ev(day, start, end, title="Meeting", all_day=False):
    d = dt.date.fromisoformat(day)
    s = dt.datetime.combine(d, dt.time.fromisoformat(start), IST)
    e = dt.datetime.combine(d, dt.time.fromisoformat(end), IST)
    return {"title": title, "start": s.isoformat(), "end": e.isoformat(), "all_day": all_day}


def busy(*events):
    return jobassist.busy_intervals(list(events))


class FakeCalendars:
    def __init__(self, meetings=None, feeds=1, fail=False, google=False):
        self.meetings, self.feeds, self.fail, self.google = meetings or [], feeds, fail, google
        self.errors, self.calls = {}, []

    def list_items(self):
        return [{"id": f"ics-{i}", "kind": "ics", "enabled": True} for i in range(self.feeds)]

    def meetings_between(self, start, days):
        self.calls.append((start, days))
        if self.fail:
            raise RuntimeError("boom")
        return self.meetings

    def google_item(self):
        return {"id": "google", "enabled": self.google}


class SlotTest(unittest.TestCase):
    def test_three_slots_on_three_days_inside_working_hours(self):
        slots = jobassist.find_slots([], MON)
        self.assertEqual(len(slots), 3)
        days = [dt.datetime.fromisoformat(s["start"]).date() for s in slots]
        self.assertEqual(days, [dt.date(2026, 11, 23), dt.date(2026, 11, 24), dt.date(2026, 11, 25)])
        for s in slots:
            a, b = dt.datetime.fromisoformat(s["start"]), dt.datetime.fromisoformat(s["end"])
            self.assertGreaterEqual(a.time(), dt.time(10, 0))
            self.assertLessEqual(b.time(), dt.time(19, 0))
            self.assertIn("IST", s["label"])

    def test_event_and_30_minute_buffer_are_avoided(self):
        slots = jobassist.find_slots(busy(ev("2026-11-23", "11:00", "12:30")), MON)
        first = dt.datetime.fromisoformat(slots[0]["start"])
        self.assertEqual(first.date(), dt.date(2026, 11, 23))
        self.assertGreaterEqual(first.time(), dt.time(13, 0))          # 12:30 + 30 minutes
        for s in slots:
            a, b = dt.datetime.fromisoformat(s["start"]), dt.datetime.fromisoformat(s["end"])
            for b0, b1 in busy(ev("2026-11-23", "11:00", "12:30")):
                self.assertFalse(b0 - dt.timedelta(minutes=30) < b and b1 + dt.timedelta(minutes=30) > a)

    def test_a_slot_one_minute_inside_the_buffer_is_refused(self):
        # a meeting 14:00 to 15:00 makes 15:00 and 15:30 unusable; 16:00 is the first free start after it
        slots = jobassist.find_slots(busy(ev("2026-11-24", "14:00", "15:00")), MON, want=5)
        tue = [dt.datetime.fromisoformat(s["start"]) for s in slots if dt.datetime.fromisoformat(s["start"]).date() == dt.date(2026, 11, 24)]
        for t in tue:
            self.assertFalse(dt.time(12, 30) < t.time() < dt.time(15, 30), t)

    def test_weekends_are_skipped(self):
        slots = jobassist.find_slots([], FRI)
        days = {dt.datetime.fromisoformat(s["start"]).date() for s in slots}
        self.assertTrue(all(d.weekday() < 5 for d in days))
        self.assertIn(dt.date(2026, 11, 30), days)

    def test_nothing_sooner_than_two_hours_and_late_today_is_dropped(self):
        soon = dt.datetime(2026, 11, 23, 10, 10, tzinfo=IST)
        first = dt.datetime.fromisoformat(jobassist.find_slots([], soon)[0]["start"])
        self.assertGreaterEqual(first, dt.datetime(2026, 11, 23, 12, 30, tzinfo=IST))
        late = dt.datetime(2026, 11, 23, 17, 30, tzinfo=IST)
        self.assertEqual(dt.datetime.fromisoformat(jobassist.find_slots([], late)[0]["start"]).date(), dt.date(2026, 11, 24))

    def test_only_the_next_five_working_days_are_searched(self):
        week = [ev(d, "10:00", "19:00") for d in ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27")]
        self.assertEqual(jobassist.find_slots(busy(*week), MON), [])
        week.pop()
        self.assertEqual(len(jobassist.find_slots(busy(*week), MON, want=3)), 3)       # Friday still free

    def test_all_day_out_of_office_blocks_the_day_but_a_birthday_does_not(self):
        ooo = ev("2026-11-23", "00:00", "00:00", "Out of office", all_day=True)
        bday = ev("2026-11-24", "00:00", "00:00", "Sam's birthday", all_day=True)
        slots = jobassist.find_slots(busy(ooo, bday), MON)
        days = [dt.datetime.fromisoformat(s["start"]).date() for s in slots]
        self.assertNotIn(dt.date(2026, 11, 23), days)
        self.assertIn(dt.date(2026, 11, 24), days)

    def test_other_time_zones_are_converted_to_ist(self):
        utc = dt.timezone.utc
        e = {"title": "Call", "start": dt.datetime(2026, 11, 24, 6, 0, tzinfo=utc).isoformat(), "end": dt.datetime(2026, 11, 24, 7, 0, tzinfo=utc).isoformat(), "all_day": False}
        b = jobassist.busy_intervals([e])
        self.assertEqual(b[0][0].hour, 11)                       # 06:00 UTC is 11:30 IST
        self.assertEqual(b[0][0].minute, 30)

    def test_working_hours_config(self):
        slots = jobassist.find_slots([], MON, start=dt.time(14, 0), end=dt.time(16, 0), slot_min=60)
        for s in slots:
            self.assertGreaterEqual(dt.datetime.fromisoformat(s["start"]).time(), dt.time(14, 0))
            self.assertLessEqual(dt.datetime.fromisoformat(s["end"]).time(), dt.time(16, 0))

    def test_schedule_host_finds_booking_links_only(self):
        self.assertEqual(jobassist.schedule_host("Pick a time: https://calendly.com/priya-nair/30min thanks"), "calendly.com")
        self.assertEqual(jobassist.schedule_host("see https://acme.cal.com/x/15min"), "cal.com")
        self.assertEqual(jobassist.schedule_host("Our site https://acme.example.com/careers"), "")
        self.assertEqual(jobassist.schedule_host("https://evil-calendly.com/x"), "")
        self.assertEqual(jobassist.schedule_host(""), "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobassist-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        self.rem = FakeReminders()
        self.jt = jobtrack.Jobtrack(self.ctx, clock=lambda: START + dt.timedelta(days=3), reminders=self.rem, jobprefs=FakeJobprefs([]),
                                    applyq=FakeApplyq([]), profile_fn=lambda c: PROFILE)
        self.mail = FakeMail()
        self.rp = replies.Replies(self.ctx, clock=lambda: START + dt.timedelta(days=3), mail=self.mail, jobtrack=self.jt, outreach=None)
        self.cal = FakeCalendars()
        self.owner = {"skills": ["Python"], "evidence": {"python"}, "years": 4}
        self.ja = jobassist.JobAssist(self.ctx, clock=lambda: MON, calendars_fn=lambda: self.cal, replies_fn=lambda: self.rp,
                                      jobtrack_fn=lambda: self.jt, profile_fn=lambda c: PROFILE, owner_fn=lambda: self.owner)

    def rec(self, company="Acme", role="Python Developer", **kw):
        return self.jt.add({"company": company, "role": role, "applied_at": "2026-11-02", **kw})[0]

    def invite(self, body, n=1, subject="Interview for Python Developer", sender="Priya Nair <jobs@acme.example.com>"):
        self.mail.mails.append(mail(n, sender, subject, body))
        self.rp.poll(force=True)
        return next(s for s in self.rp._data()["suggestions"] if s["mail_key"].endswith(f"m{n:03d}"))

    def contacts(self, *people):
        (self.tmp / "data").mkdir(parents=True, exist_ok=True)
        (self.tmp / "data" / "networking.json").write_text(json.dumps({"contacts": list(people), "drafts": []}), encoding="utf-8")


class ScheduleTest(Base):
    def test_interview_invite_gets_a_draft_with_three_free_times(self):
        self.rec()
        s = self.invite("We would like to schedule a call with you about the Python Developer role. Please share your availability.")
        self.assertEqual(s["kind"], "interview")
        out = self.ja.schedule(s["id"])
        self.assertEqual(len(out["slots"]), 3)
        self.assertFalse(out["sends"])
        self.assertEqual(out["link_host"], "")
        body = out["draft"]["body"]
        for slot in out["slots"]:
            self.assertIn(slot["label"], body)
        self.assertTrue(body.startswith("Hello Priya,"))
        self.assertIn("Sam Example", body)                                 # the confirmed name
        self.assertEqual(out["draft"]["subject"], "Re: Interview for Python Developer")
        self.assertEqual(self.cal.calls[0][0], MON.date())

    def test_slots_avoid_the_fake_calendars_events(self):
        self.rec()
        self.cal.meetings = [ev("2026-11-23", "10:00", "19:00", "Workshop"), ev("2026-11-24", "10:00", "13:00", "Planning")]
        s = self.invite("Please share your availability for a call.")
        out = self.ja.schedule(s["id"])
        for slot in out["slots"]:
            a = dt.datetime.fromisoformat(slot["start"])
            self.assertNotEqual(a.date(), dt.date(2026, 11, 23))
            if a.date() == dt.date(2026, 11, 24):
                self.assertGreaterEqual(a.time(), dt.time(13, 30))

    def test_booking_link_is_reported_and_never_opened(self):
        self.rec()
        s = self.invite("Hi, please pick a slot that suits you: https://calendly.com/priya-nair/30min")
        self.assertEqual(s["schedule_host"], "calendly.com")
        out = self.ja.schedule(s["id"])
        self.assertEqual(out["link_host"], "calendly.com")
        self.assertIsNone(out["draft"])
        self.assertIn("never opens it", out["message"])
        self.assertNotIn("https://", json.dumps(out))                     # the link itself is not even kept

    def test_unreadable_or_missing_calendars_are_said_plainly(self):
        self.rec()
        s = self.invite("Please share your availability for a call.")
        self.cal.fail = True
        out = self.ja.schedule(s["id"])
        self.assertEqual(len(out["slots"]), 3)
        self.assertTrue(any("could not be read" in n for n in out["notes"]))
        self.cal.fail, self.cal.feeds, self.cal.google = False, 0, True
        out = self.ja.schedule(s["id"])
        self.assertTrue(any("No calendar feed" in n for n in out["notes"]))
        self.assertTrue(any("Google Calendar events are not read" in n for n in out["notes"]))

    def test_a_full_week_asks_for_their_times_instead(self):
        self.rec()
        s = self.invite("Please share your availability for a call.")
        self.cal.meetings = [ev(d, "10:00", "19:00") for d in ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27")]
        out = self.ja.schedule(s["id"])
        self.assertEqual(out["slots"], [])
        self.assertIn("Could you share a few times", out["draft"]["body"])

    def test_only_interview_replies_and_known_ids(self):
        self.rec()
        s = self.invite("Unfortunately we will not be moving forward with your application.", subject="Your application")
        self.assertEqual(s["kind"], "rejection")
        with self.assertRaises(jobassist.AssistError):
            self.ja.schedule(s["id"])
        with self.assertRaises(LookupError):
            self.ja.schedule("0" * 12)
        with self.assertRaises(LookupError):
            self.ja.schedule("not-an-id")

    def test_team_sender_gets_a_plain_hello(self):
        self.assertEqual(jobassist.first_name("Acme Careers Team"), "")
        self.assertEqual(jobassist.first_name("noreply@acme.example.com"), "")
        self.assertEqual(jobassist.first_name("Priya Nair"), "Priya")

    def test_overview_lists_invites_and_active_records(self):
        r = self.rec()
        self.invite("Please share your availability for a call.")
        self.rec("Globex", role="Data Analyst", status="rejected")
        o = self.ja.overview()
        self.assertEqual(len(o["invites"]), 1)
        self.assertEqual([x["id"] for x in o["records"]], [r["id"]])
        self.assertEqual(o["settings"]["work_start"], "10:00")


class CallCardTest(Base):
    POSTING = ("Python Developer. We need strong Python, REST API design and PostgreSQL. You will build payment APIs for our team. "
               "Remote within India. 2+ years of experience.")

    def test_card_has_every_section_and_never_invents_a_salary(self):
        r = self.rec(posting=self.POSTING, source="company site", salary_note="")
        c = self.ja.call_card(r["id"])
        self.assertEqual((c["company"], c["role"], c["status"]), ("Acme", "Python Developer", "applied"))
        self.assertIn("company site", c["came_from"])
        self.assertFalse(c["salary_ask_set"])
        self.assertEqual(c["salary_ask"], "")
        self.assertIn("never guessed", c["salary_ask_note"])
        self.assertTrue(1 <= len(c["points"]) <= 3)
        confirmed = " ".join(PROFILE["experience"])
        for p in c["points"]:
            self.assertIn(p["text"], confirmed)
        self.assertIn("Is there a pay range for this role?", c["questions"])
        self.assertFalse(c["sends"])

    def test_notice_period_comes_from_job_preferences_and_unset_says_so(self):
        r = self.rec(posting=self.POSTING)
        self.assertIsNone(self.ja.call_card(r["id"])["notice_days"])
        self.assertIn("Not set", self.ja.call_card(r["id"])["notice_text"])
        from hub import jobprefs
        jobprefs.engine_for(self.ctx).set_owner({"notice_days": 60})
        c = self.ja.call_card(r["id"])
        self.assertEqual(c["notice_days"], 60)
        self.assertTrue(any("60 days" in q for q in c["questions"]))

    def test_salary_ask_is_the_owners_global_or_per_application_value(self):
        a, b = self.rec(posting=self.POSTING), self.rec("Globex", role="Python Developer", posting=self.POSTING)
        self.ja.set_salary_ask("18 LPA fixed")
        self.assertEqual(self.ja.call_card(a["id"])["salary_ask"], "18 LPA fixed")
        self.ja.set_salary_ask("22 LPA fixed", b["id"])
        self.assertEqual(self.ja.call_card(b["id"])["salary_ask"], "22 LPA fixed")
        self.assertEqual(self.ja.call_card(a["id"])["salary_ask"], "18 LPA fixed")
        self.ja.set_salary_ask("", b["id"])
        self.assertEqual(self.ja.call_card(b["id"])["salary_ask"], "18 LPA fixed")
        with self.assertRaises(LookupError):
            self.ja.set_salary_ask("1", "0" * 12)

    def test_no_posting_text_says_checks_did_not_run(self):
        r = self.rec()
        c = self.ja.call_card(r["id"])
        self.assertEqual(c["red_flags"], [])
        self.assertTrue(any("No posting text" in n for n in c["notes"]))

    OWNER = {"skills": ["Python"], "evidence": set(), "years": 4, "regions": ["India"], "owner_codes": {"IN"}, "auth_codes": {"IN"},
             "auth_names": ["India"], "notice_days": None, "relocate": False, "needs_sponsorship": False, "languages": set(), "aliases": {}, "no_reply": {}}

    def test_red_flags_come_from_triage(self):
        self.owner = self.OWNER
        r = self.rec(posting="Python developer. Open only to candidates in the United States. Must be authorized to work in the US. No visa sponsorship.")
        c = self.ja.call_card(r["id"])
        self.assertEqual(c["triage_verdict"], "skip")
        self.assertTrue(any("authorised" in f or "United States" in f for f in c["red_flags"]), c["red_flags"])
        r2 = self.rec("Globex", posting="Python Developer. Requires 10+ years of experience. Python, Django, AWS, Docker, Kubernetes.")
        self.assertTrue(any("10+ years" in f for f in self.ja.call_card(r2["id"])["red_flags"]))

    def test_card_for_an_apply_queue_item(self):
        item = {"id": "a" * 12, "title": "Backend Engineer", "company": "Globex", "url": "https://jobs.example.test/1", "source": "finder", "state": "ready",
                "description": "", "created": "2026-11-01T08:00:00"}
        self.jt._applyq = FakeApplyq([item])
        c = self.ja.call_card("", item["id"])
        self.assertEqual((c["company"], c["role"], c["came_from"]), ("Globex", "Backend Engineer", "finder"))
        with self.assertRaises(LookupError):
            self.ja.call_card("", "b" * 12)

    def test_find_record_by_company_words(self):
        a = self.rec("Acme Payments")
        self.rec("Globex", role="Data Analyst")
        self.assertEqual(self.ja.find_record("acme")[0]["id"], a["id"])
        self.assertEqual(self.ja.find_record("zzz"), [])


class ReferralTest(Base):
    PERSON = {"id": "nc-aaaa1111", "name": "Rohan Mehta", "company": "Acme", "past_companies": "", "role": "Senior Engineer", "how_known": "college", "note": "", "last_contact": ""}

    def test_draft_for_a_known_person_has_the_link_a_fact_and_three_lines(self):
        r = self.rec(url="https://jobs.example.test/acme/1", posting="Python developer building payments APIs.")
        self.contacts(self.PERSON)
        out = self.ja.referral(r["id"])
        d = out["draft"]
        self.assertFalse(out["sends"])
        self.assertEqual(out["contact_id"], "nc-aaaa1111")
        self.assertIn("https://jobs.example.test/acme/1", d["body"])
        self.assertTrue(d["body"].startswith("Hi Rohan,"))
        self.assertEqual(d["blockers"], [])
        self.assertEqual(len(d["facts_used"]), 1)
        self.assertIn("payments API in Python", d["body"])
        content = [ln for ln in d["body"].split("\n") if ln.strip()][1:-2]
        self.assertEqual(len(content), 3)
        self.assertLessEqual(d["words"], 110)
        self.assertIn("works there now", d["why"][0])

    def test_missing_link_blocks_copying(self):
        r = self.rec(url="")
        self.contacts(self.PERSON)
        d = self.ja.referral(r["id"])["draft"]
        self.assertIn("Add the job link.", d["blockers"])
        self.assertIn("[FILL: job link]", d["body"])

    def test_no_contact_at_the_company_gives_no_draft(self):
        r = self.rec("Globex")
        self.contacts(self.PERSON)
        out = self.ja.referral(r["id"])
        self.assertIsNone(out["draft"])
        self.assertEqual(out["candidates"], [])
        self.assertIn("Nobody in your contacts", out["message"])

    def test_former_colleague_wording_and_choice_of_contact(self):
        r = self.rec(url="https://jobs.example.test/acme/1")
        other = {**self.PERSON, "id": "nc-bbbb2222", "name": "Isha Rao", "company": "Initech", "past_companies": "Acme"}
        self.contacts(self.PERSON, other)
        out = self.ja.referral(r["id"], "nc-bbbb2222")
        self.assertEqual(out["contact_id"], "nc-bbbb2222")
        self.assertIn("used to work there", out["draft"]["body"])
        self.assertEqual(len(out["candidates"]), 2)

    def test_asked_is_marked_only_on_the_owners_tap_and_can_be_undone(self):
        r = self.rec(url="https://jobs.example.test/acme/1")
        self.contacts(self.PERSON)
        self.ja.referral(r["id"])
        self.assertEqual(self.ja.referral(r["id"])["asked"], {})
        self.assertFalse(self.ja.overview()["records"][0]["referral_asked"])
        res = self.ja.mark_asked(r["id"], "nc-aaaa1111")
        self.assertEqual(res["asked"], MON.date().isoformat())
        self.assertEqual(self.ja.referral(r["id"])["asked"], {"nc-aaaa1111": MON.date().isoformat()})
        self.assertTrue(self.ja.overview()["records"][0]["referral_asked"])
        self.assertEqual(self.ja.mark_asked(r["id"], "nc-aaaa1111", False)["asked"], "")
        with self.assertRaises(LookupError):
            self.ja.mark_asked(r["id"], "nc-nobody")

    def test_interview_stage_tip(self):
        r = self.rec(url="https://jobs.example.test/acme/1", status="interview")
        self.contacts(self.PERSON)
        self.assertTrue(any("hiring manager" in t for t in self.ja.referral(r["id"])["tips"]))


class RouteTest(Base):
    def client(self):
        app = FastAPI()
        app.include_router(jobassist.make_router(self.ctx, clock=lambda: MON, calendars_fn=lambda: self.cal, replies_fn=lambda: self.rp,
                                                 jobtrack_fn=lambda: self.jt, profile_fn=lambda c: PROFILE, owner_fn=lambda: self.owner))
        return TestClient(app, cookies={"hub_session": "yes"}), TestClient(app)

    def test_needs_login_and_works_end_to_end(self):
        c, anon = self.client()
        self.assertEqual(anon.get("/api/jobassist").status_code, 401)
        r = self.rec(url="https://jobs.example.test/acme/1", posting=CallCardTest.POSTING)
        s = self.invite("Please share your availability for a call.")
        self.assertEqual(len(c.get("/api/jobassist").json()["invites"]), 1)
        sched = c.post("/api/jobassist/schedule", json={"suggestion_id": s["id"]})
        self.assertEqual(sched.status_code, 200)
        self.assertEqual(len(sched.json()["slots"]), 3)
        self.assertEqual(c.post("/api/jobassist/schedule", json={"suggestion_id": "0" * 12}).status_code, 404)
        self.assertEqual(c.post("/api/jobassist/schedule", json={}).status_code, 404)
        card = c.get("/api/jobassist/call-card", params={"record_id": r["id"]})
        self.assertEqual(card.status_code, 200)
        self.assertEqual(c.get("/api/jobassist/call-card", params={"query": "acme"}).json()["company"], "Acme")
        self.assertEqual(c.get("/api/jobassist/call-card", params={"query": "zzz"}).status_code, 404)
        self.assertEqual(c.get("/api/jobassist/call-card").status_code, 400)
        put = c.put("/api/jobassist/salary-ask", json={"salary_ask": "20 LPA"})
        self.assertEqual(put.json()["salary_ask"], "20 LPA")
        self.assertEqual(c.get("/api/jobassist").json()["salary_ask"], "20 LPA")
        self.assertEqual(c.post("/api/jobassist/referral", json={"record_id": r["id"]}).status_code, 200)

    def test_switched_off_answers_404(self):
        self.config.jobassist_enabled = False
        c, _ = self.client()
        self.assertEqual(c.get("/api/jobassist").status_code, 404)


class SafetyTest(unittest.TestCase):
    def test_module_has_no_network_process_or_send_code(self):
        text = Path(jobassist.__file__).read_text(encoding="utf-8")
        for banned in ("import httpx", "import requests", "urlopen", "socket", "subprocess", "playwright", "webbrowser", "send_message", ".send("):
            self.assertNotIn(banned, text)

    def test_friday_tool_is_read_only_and_untrusted(self):
        tools = jobassist.build(lambda: None, make_config()[0])
        self.assertEqual([t.spec["name"] for t in tools], ["call_card"])
        self.assertFalse(getattr(tools[0], "mutating", False))
        self.assertTrue(getattr(tools[0], "_untrusted", False))
        from hub.assistant import TOOL_GROUPS
        self.assertIn("call_card", TOOL_GROUPS["jobassist"][0])

    def test_replies_keeps_only_the_host_of_a_booking_link(self):
        self.assertEqual(replies._schedule_host("Book https://calendly.com/x/y?name=Sam"), "calendly.com")


if __name__ == "__main__":
    unittest.main()
