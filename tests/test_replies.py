"""Tests for reply sorting (hub/replies.py). Fake mail, fake clock, fake reminders. Invented companies and people. No network, no AI,
no real mail."""
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import jobtrack, replies
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth
from tests.test_jobtrack import FakeApplyq, FakeJobprefs, FakeReminders, PROFILE, START


class FakeMail:
    def __init__(self, mails=None, connected=True):
        self.mails = mails or []          # [{id, sender, subject, snippet, date, thread, body}]
        self.connected = connected
        self.recent_calls = 0
        self.body_calls = []

    def list_accounts(self, *a):
        return [{"id": "g1", "connected": self.connected}]

    def recent(self, account, hours=24, max_results=30):
        self.recent_calls += 1
        return [{k: v for k, v in m.items() if k != "body"} | {"account": "g1"} for m in self.mails][:max_results]

    def get_message(self, account, mid, destination="local"):
        assert destination == "local", "mail bodies must be read with destination local"
        self.body_calls.append(mid)
        m = next(m for m in self.mails if m["id"] == mid)
        return {**m, "account": account}


def mail(n, sender, subject, body, thread="", date="2026-11-04"):
    return {"id": f"m{n:03d}", "sender": sender, "subject": subject, "snippet": body[:100], "body": body, "thread": thread, "date": date}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="replies-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        self.now = START + dt.timedelta(days=3)
        self.rem = FakeReminders()
        self.jt = jobtrack.Jobtrack(self.ctx, clock=lambda: START, reminders=self.rem, jobprefs=FakeJobprefs([]), applyq=FakeApplyq([]),
                                    profile_fn=lambda c: PROFILE)
        self.jt.clock = lambda: self.now
        self.mail = FakeMail()
        self.r = replies.Replies(self.ctx, clock=lambda: self.now, mail=self.mail, jobtrack=self.jt, outreach=None)

    def rec(self, company="Acme", role="Python Developer", **kw):
        return self.jt.add({"company": company, "role": role, "applied_at": "2026-11-02", **kw})[0]


class ClassifyTest(unittest.TestCase):
    def kind(self, subject, body="", **kw):
        return replies.classify(subject, body, **kw)["kind"]

    def test_rejection_phrasings(self):
        for body in ["Unfortunately we will not be moving forward with your application.",
                     "We regret to inform you that your profile has not been shortlisted.",
                     "Your profile does not match our current requirement. We will keep your profile in our database.",
                     "After careful thought we decided to proceed with other candidates.",
                     "The position has been filled.",
                     "We are unable to offer you the role at this time."]:
            self.assertEqual(self.kind("Update on your application", body), "rejection", body)

    def test_negative_beats_positive_words(self):
        self.assertEqual(self.kind("Interview feedback", "Unfortunately you were not selected after the interview."), "rejection")
        self.assertEqual(self.kind("Offer", "We are unable to offer you a position."), "rejection")

    def test_interview_invites_including_indian_phrasings(self):
        for body in ["Your profile is shortlisted. Please share your availability for a technical round.",
                     "We would like to schedule a call to discuss your profile.",
                     "HR round on Monday, kindly confirm a convenient slot.",
                     "Please book a time on my Calendly link.",
                     "Telephonic discussion with our hiring manager tomorrow."]:
            self.assertEqual(self.kind("Acme: next steps", body), "interview", body)

    def test_assessment_offer_recruiter_ack(self):
        self.assertEqual(self.kind("Test", "Please complete the HackerRank test link within 3 days."), "assessment")
        self.assertEqual(self.kind("Take-home", "Here is your take-home assignment."), "assessment")
        self.assertEqual(self.kind("Congratulations", "We are pleased to offer you the role. Offer letter attached."), "offer")
        self.assertEqual(self.kind("Python opening", "I came across your profile. We are hiring for a backend role. Please share your updated resume, current CTC and notice period."), "recruiter")
        self.assertEqual(self.kind("Application received", "Thank you for applying. We have received your application. This is an automated message."), "auto_ack")

    def test_auto_ack_that_mentions_interview_stays_an_ack(self):
        self.assertEqual(self.kind("Thanks", "Thank you for applying. If you are selected for an interview we will contact you. Do not reply to this email."), "auto_ack")

    def test_opt_out_only_for_outreach(self):
        self.assertEqual(self.kind("Re: hello", "No thanks, not interested. Please remove me.", outreach=True), "opt_out")
        self.assertNotEqual(self.kind("Re: hello", "Not interested in other roles, thanks.", outreach=False), "opt_out")

    def test_unknown_is_other(self):
        self.assertEqual(self.kind("Lunch?", "Are we still on for lunch?"), "other")


class MatchTest(Base):
    def test_company_domain_and_name_match(self):
        acme, other = self.rec("Acme Pvt Ltd"), self.rec("Globex", role="Data Analyst")
        m = {"sender": "Acme Careers <jobs@acme.example.com>", "subject": "Your application", "snippet": "", "date": "2026-11-04"}
        rec, _ = replies.best_match([acme, other], m, "")
        self.assertEqual(rec["id"], acme["id"])

    def test_ats_sender_matches_by_company_name_in_text(self):
        acme, other = self.rec("Acme"), self.rec("Globex", role="Data Analyst")
        m = {"sender": "no-reply@greenhouse.io", "subject": "Thank you for applying to Acme", "snippet": "", "date": "2026-11-04"}
        rec, _ = replies.best_match([acme, other], m, "")
        self.assertEqual(rec["id"], acme["id"])

    def test_two_roles_same_company_needs_the_role_or_asks(self):
        a, b = self.rec("Acme", role="Python Developer"), self.rec("Acme", role="Data Analyst")
        m = {"sender": "jobs@acme.example.com", "subject": "Interview for Data Analyst", "snippet": "", "date": "2026-11-04"}
        rec, _ = replies.best_match([a, b], m, "")
        self.assertEqual(rec["id"], b["id"])
        m2 = {"sender": "jobs@acme.example.com", "subject": "Hello", "snippet": "", "date": "2026-11-04"}
        rec2, cands = replies.best_match([a, b], m2, "")
        self.assertIsNone(rec2)
        self.assertEqual(len(cands), 2)

    def test_thread_helps_and_mail_before_applying_is_ignored(self):
        a = self.rec("Acme")
        m = {"sender": "x@gmail.com", "subject": "Re: hello", "snippet": "", "date": "2026-11-04", "thread": "t1"}
        self.assertIsNone(replies.best_match([a], m, "")[0])
        self.assertEqual(replies.best_match([a], m, "", {"t1": a["id"]})[0]["id"], a["id"])
        old = {"sender": "jobs@acme.example.com", "subject": "Acme", "snippet": "", "date": "2026-10-01"}
        self.assertIsNone(replies.best_match([a], old, "")[0])


class PollTest(Base):
    def setUp(self):
        super().setUp()
        self.acme = self.rec("Acme")
        self.mail.mails = [
            mail(1, "Acme Careers <jobs@acme.example.com>", "Interview invitation", "We would like to schedule a call to discuss your profile.", thread="t1"),
            mail(2, "Newsletter <news@shop.example.com>", "Big sale", "Everything is cheaper today."),
            mail(3, "no-reply@greenhouse.io", "Update from Globex", "Unfortunately we will not be moving forward."),
        ]

    def test_suggests_and_changes_nothing(self):
        res = self.r.poll()
        self.assertTrue(res["ran"])
        pend = self.r.pending()
        top = next(s for s in pend if s["record_id"] == self.acme["id"])
        self.assertEqual((top["kind"], top["status"], top["action"]), ("interview", "interview", "status"))
        self.assertIn("Acme replied: an interview invite. Mark as interview?", top["text"])
        self.assertEqual(self.jt._data()["records"][0]["status"], "applied")           # nothing changed by itself
        self.assertEqual(self.rem.deleted, [])                                         # follow-ups still there
        self.assertNotIn("m002", self.mail.body_calls)                                 # junk is never opened
        self.assertEqual(len(pend), 2)                                                 # Acme, plus the Globex rejection that matches nothing tracked (shown as "pick one")
        self.assertEqual(next(s for s in pend if s["record_id"] == "")["action"], "pick")

    def test_unmatched_rejection_is_shown_as_pick(self):
        self.mail.mails.append(mail(4, "hr@initech.example.com", "Application update", "Unfortunately we will not be moving forward."))
        self.r.poll()
        pick = [s for s in self.r.pending() if s["action"] == "pick"]
        self.assertTrue(pick)
        self.assertEqual(pick[0]["record_id"], "")

    def test_confirm_changes_status_and_cancels_followups_only_then(self):
        self.r.poll()
        s = next(s for s in self.r.pending() if s["record_id"] == self.acme["id"])
        pending_reminders = [f for f in self.jt._data()["records"][0]["followups"] if f["reminder_id"]]
        self.assertTrue(pending_reminders)
        out = self.r.confirm(s["id"])
        self.assertEqual(out["record"]["status"], "interview")
        rec = self.jt._data()["records"][0]
        self.assertTrue(all(f["cancelled"] or f["done"] for f in rec["followups"]))
        self.assertTrue(self.rem.deleted)
        self.assertFalse([x for x in self.r.pending() if x["id"] == s["id"]])
        with self.assertRaises(jobtrack.NotFound):
            self.r.confirm(s["id"])                                                    # one tap, once

    def test_confirmed_rejection_cancels_followups(self):
        self.mail.mails = [mail(5, "jobs@acme.example.com", "Your application", "We regret to inform you that we will not be moving forward.")]
        self.r.poll()
        s = self.r.pending()[0]
        self.assertEqual(s["status"], "rejected")
        self.r.confirm(s["id"])
        rec = self.jt._data()["records"][0]
        self.assertEqual(rec["status"], "rejected")
        self.assertTrue(all(f["cancelled"] or f["done"] for f in rec["followups"]))

    def test_dismiss_keeps_status(self):
        self.r.poll()
        for s in self.r.pending():
            self.r.dismiss(s["id"])
        self.assertEqual(self.jt._data()["records"][0]["status"], "applied")
        self.assertEqual(self.r.pending(), [])

    def test_pick_needs_a_choice(self):
        self.mail.mails = [mail(6, "hr@initech.example.com", "Update", "Unfortunately we will not be moving forward.")]
        self.r.poll()
        s = self.r.pending()[0]
        with self.assertRaises(jobtrack.TrackError):
            self.r.confirm(s["id"])
        self.assertEqual(self.r.confirm(s["id"], record_id=self.acme["id"])["record"]["status"], "rejected")

    def test_poll_is_cached_and_bounded(self):
        self.r.poll()
        calls = self.mail.recent_calls
        self.assertEqual(self.r.poll()["reason"], "cached")                           # inside 30 minutes: no mail call
        self.assertEqual(self.mail.recent_calls, calls)
        self.now += dt.timedelta(minutes=31)
        self.assertTrue(self.r.poll()["ran"])
        # many relevant mails: only MAX_BODIES bodies are read in one pass
        self.mail.mails = [mail(100 + i, f"jobs@acme.example.com", f"Interview round {i}", "Please share availability for a technical round.") for i in range(30)]
        self.mail.body_calls.clear()
        self.now += dt.timedelta(minutes=31)
        self.r.poll()
        self.assertLessEqual(len(self.mail.body_calls), replies.MAX_BODIES)
        self.now += dt.timedelta(minutes=31)
        self.mail.body_calls.clear()
        self.r.poll()
        self.assertTrue(self.mail.body_calls)                                          # the rest waits for the next pass

    def test_seen_mails_are_not_read_twice(self):
        self.r.poll()
        self.mail.body_calls.clear()
        self.now += dt.timedelta(minutes=31)
        self.r.poll()
        self.assertEqual(self.mail.body_calls, [])

    def test_off_when_mail_not_configured_or_disabled(self):
        self.mail.connected = False
        self.assertEqual(self.r.poll()["reason"], "mail not connected")
        self.assertEqual(self.mail.recent_calls, 0)
        self.config.replies_enabled = False
        self.assertEqual(self.r.poll()["reason"], "off")

    def test_no_model_unless_replies_ai(self):
        called = []
        self.r.model_fn = lambda: called.append(1)
        self.mail.mails = [mail(7, "jobs@acme.example.com", "Hello from Acme", "Just saying hi about your Python Developer application.")]
        self.r.poll()
        self.assertEqual(called, [])                                                   # replies_ai is off by default

    def test_today_line(self):
        self.r.poll()
        self.assertEqual(replies.today_line(self.config), "2 replies to review, 1 interview invite")
        self.assertIn("2 replies to review", jobtrack.today_line(self.config, clock=lambda: self.now))


class RouteTest(Base):
    def test_routes_confirm_and_dismiss(self):
        self.rec("Acme")
        self.mail.mails = [mail(1, "jobs@acme.example.com", "Interview", "Please share your availability for a technical round.")]
        app = FastAPI()
        app.include_router(replies.make_router(self.ctx, clock=lambda: self.now, mail=self.mail, jobtrack=self.jt, outreach=None))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.assertEqual(TestClient(app).get("/api/replies").status_code, 401)
        self.assertEqual(c.get("/api/replies").json()["pending"], [])                  # reading the screen never reads mail
        self.assertEqual(self.mail.recent_calls, 0)
        d = c.post("/api/replies/check").json()
        self.assertEqual(d["counts"]["interview"], 1)
        sid = d["pending"][0]["id"]
        self.assertEqual(c.post("/api/replies/zzzz/confirm", json={}).status_code, 404)
        self.assertEqual(c.post(f"/api/replies/{sid}/confirm", json={}).status_code, 200)
        self.assertEqual(self.jt._data()["records"][0]["status"], "interview")
        self.config.replies_enabled = False
        self.assertEqual(c.get("/api/replies").status_code, 404)


if __name__ == "__main__":
    unittest.main()
