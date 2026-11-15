"""Tests for the job tracker (hub/jobtrack.py). Fakes only: a fake clock, a fake reminders service, fake job preferences and a fake
application queue. Invented data. No network, no AI, no mail."""
import datetime as dt
import re
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import jobtrack, today
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

PROFILE = {"name": "Sam Example", "headline": "Backend developer", "summary": "Backend developer who builds reliable APIs.",
           "skills": ["Python"], "experience": ["Built a payments API in Python that served two million requests a day"],
           "links": [], "standard_answers": {}, "resume_text": "..."}
START = dt.datetime(2026, 11, 2, 10, 0)


class FakeReminders:
    def __init__(self):
        self.items, self.deleted, self.updated, self.n = {}, [], [], 0

    def create(self, text, due, repeat="none", n=None, source="app", call=False):
        self.n += 1
        rid = f"r{self.n}"
        self.items[rid] = {"id": rid, "text": text, "due": due, "source": source, "call": call}
        return self.items[rid]

    def delete(self, rid):
        self.deleted.append(rid)
        self.items.pop(rid, None)

    def update(self, rid, body):
        self.updated.append((rid, body))
        if rid in self.items and "call" in body:
            self.items[rid]["call"] = body["call"]


class FakeJobprefs:
    def __init__(self, profiles):
        self.profiles, self.calls = profiles, []

    def enabled_profiles(self):
        return [dict(p) for p in self.profiles]

    def update(self, pid, raw):
        self.calls.append((pid, raw))
        for p in self.profiles:
            if p["id"] == pid:
                p.update(raw)


class FakeStore:
    def __init__(self, items):
        self.items = items

    def list(self, states=None, limit=200):
        return [i for i in self.items if not states or i["state"] in states]

    def get(self, iid):
        return next((i for i in self.items if i["id"] == iid), None)


class FakeApplyq:
    def __init__(self, items):
        self.store = FakeStore(items)
        self.sent = []

    def send(self, *a, **k):          # must never be reached
        self.sent.append((a, k))
        raise AssertionError("the tracker must never send")


def qitem(n, state="sent", day="2026-11-01", source_id="greenhouse", title="Python Developer", company="Acme"):
    return {"id": f"{n:012x}", "title": title, "company": company, "url": f"https://jobs.example.test/{n}", "source": "finder", "state": state,
            "sent_at": f"{day}T09:00:00", "applied_at": None, "created": f"{day}T08:00:00", "meta": {"source_id": source_id}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobtrack-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)))
        self.now = START
        self.rem = FakeReminders()
        self.jp = FakeJobprefs([])
        self.aq = FakeApplyq([])
        self.make()

    def make(self):
        self.t = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=self.rem, jobprefs=self.jp, applyq=self.aq,
                                   profile_fn=lambda c: PROFILE)

    def add(self, company="Acme", role="Python Developer", **kw):
        rec, warns = self.t.add({"company": company, "role": role, **kw})
        return rec, warns

    def advance(self, **kw):
        self.now = self.now + dt.timedelta(**kw)


class RecordTest(Base):
    def test_add_has_history_and_defaults(self):
        rec, warns = self.add(source="Greenhouse", url="https://jobs.example.test/1", resume_version="backend-v2", salary_note="20-25 LPA")
        self.assertEqual(rec["status"], "applied")
        self.assertEqual(rec["applied_at"], "2026-11-02")
        self.assertEqual([h["status"] for h in rec["history"]], ["applied"])
        self.assertEqual(warns, [])
        self.assertEqual(rec["resume_version"], "backend-v2")

    def test_needs_company_and_role_and_valid_date(self):
        with self.assertRaises(jobtrack.TrackError):
            self.t.add({"company": "", "role": "x"})
        with self.assertRaises(jobtrack.TrackError):
            self.t.add({"company": "A", "role": "B", "applied_at": "not a date"})
        with self.assertRaises(jobtrack.TrackError):
            self.t.add({"company": "A", "role": "B", "applied_at": "2027-01-01"})   # in the future

    def test_status_history_is_kept(self):
        rec, _ = self.add()
        self.t.set_status(rec["id"], "viewed")
        self.advance(days=3)
        out = self.t.set_status(rec["id"], "interview", "phone screen booked")
        self.assertEqual([h["status"] for h in out["history"]], ["applied", "viewed", "interview"])
        self.assertEqual(out["history"][-1]["note"], "phone screen booked")
        with self.assertRaises(jobtrack.TrackError):
            self.t.set_status(rec["id"], "interview")        # same status twice
        with self.assertRaises(jobtrack.TrackError):
            self.t.set_status(rec["id"], "hired")            # unknown status

    def test_stale_posting_flag(self):
        rec, warns = self.add(posted_at="2026-09-20")
        self.assertTrue(rec["stale_posting"])
        self.assertTrue(any("old" in w for w in warns))
        fresh, warns2 = self.add(company="Beta", posted_at="2026-10-20")
        self.assertFalse(fresh["stale_posting"])
        self.assertEqual(warns2, [])

    def test_update_and_delete_clear_reminders(self):
        rec, _ = self.add()
        self.assertEqual(len(self.rem.items), 2)
        self.t.update(rec["id"], {"notes": "spoke to Dana", "salary_note": "ask 24"})
        self.assertEqual(len(self.rem.items), 2)
        self.t.update(rec["id"], {"applied_at": "2026-11-01"})        # moved a day earlier: reminders move too
        self.assertEqual(sorted(i["due"].day for i in self.rem.items.values()), [6, 11])
        self.t.delete(rec["id"])
        self.assertEqual(self.rem.items, {})
        with self.assertRaises(jobtrack.NotFound):
            self.t.update(rec["id"], {"notes": "x"})


class DuplicateTest(Base):
    def test_same_company_and_role_within_90_days_warns(self):
        self.add(company="Acme Inc.", role="Python Developer")
        self.advance(days=40)
        rec, warns = self.add(company="ACME", role="python developer")
        self.assertEqual(len(warns), 1)
        self.assertIn("40 days ago", warns[0])
        self.assertTrue(self.t.view(rec)["duplicates"])

    def test_after_90_days_or_other_role_no_warning(self):
        self.add()
        self.advance(days=91)
        _, warns = self.add()
        self.assertEqual(warns, [])
        _, warns2 = self.add(role="Data Engineer")
        self.assertEqual(warns2, [])

    def test_withdrawn_does_not_count(self):
        rec, _ = self.add()
        self.t.set_status(rec["id"], "withdrawn")
        self.advance(days=2)
        _, warns = self.add()
        self.assertEqual(warns, [])


class FollowupTest(Base):
    def test_reminders_at_day_5_and_10_at_nine(self):
        rec, _ = self.add()
        due = sorted(i["due"] for i in self.rem.items.values())
        self.assertEqual([(d.month, d.day, d.hour) for d in due], [(11, 7, 9), (11, 12, 9)])
        self.assertTrue(all(i["source"] == "jobtrack" and i["call"] is False for i in self.rem.items.values()))
        self.assertEqual([f["day"] for f in rec["followups"]], [5, 10])

    def test_configurable_days_and_none(self):
        self.config.jobtrack_followup_days = [3]
        rec, _ = self.add()
        self.assertEqual([f["day"] for f in rec["followups"]], [3])
        self.config.jobtrack_followup_days = []
        rec2, _ = self.add(company="Beta")
        self.assertEqual(rec2["followups"], [])
        self.assertEqual(jobtrack.followup_days(self.config), [])
        self.config.jobtrack_followup_days = None
        self.assertEqual(jobtrack.followup_days(self.config), [5, 10])

    def test_due_after_day_5_and_today_line(self):
        self.add()
        self.assertEqual(self.t.due_followups(), [])
        self.assertEqual(jobtrack.today_line(self.config, self.t.clock), "")
        self.advance(days=5)
        due = self.t.due_followups()
        self.assertEqual([(d["company"], d["day"]) for d in due], [("Acme", 5)])
        self.assertEqual(jobtrack.today_line(self.config, self.t.clock), "1 follow-up due")
        with mock.patch.object(jobtrack, "today_line", return_value="1 follow-up due") as m:     # Today uses the real clock
            self.assertEqual(today._jobtrack_line(self.ctx), "1 follow-up due")
        m.assert_called_once()
        self.t.followup_done(due[0]["id"], 5)
        self.assertEqual(self.t.due_followups(), [])
        self.advance(days=5)
        self.assertEqual(self.t.due_followups()[0]["day"], 10)

    def test_line_counts_several(self):
        self.add()
        self.add(company="Beta")
        self.advance(days=6)
        self.assertEqual(jobtrack.today_line(self.config, self.t.clock), "2 follow-ups due")

    def test_reply_cancels_and_back_to_waiting_reschedules(self):
        rec, _ = self.add()
        ids = list(self.rem.items)
        self.t.set_status(rec["id"], "replied")
        self.assertEqual(self.rem.items, {})
        self.assertEqual(sorted(self.rem.deleted), sorted(ids))
        self.advance(days=6)
        self.assertEqual(self.t.due_followups(), [])           # answered: nothing to chase
        self.t.set_status(rec["id"], "viewed")
        self.assertTrue(self.rem.items)                          # waiting again: the day 10 reminder is back

    def test_call_flag_goes_to_the_reminders(self):
        rec, _ = self.add()
        self.t.set_call(rec["id"], True)
        self.assertTrue(all(i["call"] for i in self.rem.items.values()))
        self.assertEqual(len(self.rem.updated), 2)
        later, _ = self.add(company="Beta", call=True)
        self.assertTrue(any(i["call"] for i in self.rem.items.values() if "Beta" in i["text"]))

    def test_old_import_is_not_chased(self):
        self.aq.store.items.append(qitem(1, day="2026-09-01"))
        self.assertEqual(self.t.sync_applyq(), 1)
        self.assertEqual(self.rem.items, {})
        self.assertEqual(self.t.due_followups(), [])

    def test_no_reminders_service_still_works(self):
        self._reminders_none = True
        t = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=None, jobprefs=self.jp, applyq=self.aq, profile_fn=lambda c: PROFILE)
        t.__class__ = type("NoRem", (jobtrack.Jobtrack,), {"reminders": property(lambda self: None)})
        rec, _ = t.add({"company": "Acme", "role": "Dev"})
        self.assertEqual([f["reminder_id"] for f in rec["followups"]], ["", ""])


class NoAutoChangeTest(Base):
    def test_time_passing_never_changes_a_status(self):
        rec, _ = self.add()
        self.advance(days=60)
        for _ in range(3):
            ov = self.t.overview()
        self.assertEqual(ov["records"][0]["status"], "applied")
        self.assertEqual(ov["records"][0]["suggest"]["status"], "ghosted")      # only a suggestion
        self.assertIn("Mark it ghosted?", ov["records"][0]["suggest"]["text"])
        self.assertEqual(self.t._data()["records"][0]["history"][-1]["status"], "applied")

    def test_queue_reply_is_only_a_suggestion(self):
        self.aq.store.items.append(qitem(1, state="sent"))
        self.t.sync_applyq()
        self.aq.store.items[0]["state"] = "replied"
        self.t.sync_applyq()
        ov = self.t.overview()
        self.assertEqual(ov["records"][0]["status"], "applied")
        self.assertEqual(ov["records"][0]["suggest"]["status"], "replied")

    def test_no_suggestion_when_recent(self):
        self.add()
        self.advance(days=3)
        self.assertIsNone(self.t.overview()["records"][0]["suggest"])


class ImportTest(Base):
    def test_sent_items_become_records_once(self):
        self.aq.store.items += [qitem(1, state="sent"), qitem(2, state="applied", day="2026-10-30", company="Beta"), qitem(3, state="ready"),
                                qitem(4, state="replied", company="Gamma")]
        self.assertEqual(self.t.sync_applyq(), 3)
        self.assertEqual(self.t.sync_applyq(), 0)
        recs = {r["company"]: r for r in self.t._data()["records"]}
        self.assertEqual(recs["Acme"]["source"], "greenhouse")
        self.assertEqual(recs["Gamma"]["status"], "replied")
        self.assertEqual(recs["Beta"]["applied_at"], "2026-10-30")
        self.assertEqual(recs["Acme"]["origin"], "applyq")

    def test_deleted_import_stays_deleted(self):
        self.aq.store.items.append(qitem(1))
        self.t.sync_applyq()
        self.t.delete(self.t._data()["records"][0]["id"])
        self.assertEqual(self.t.sync_applyq(), 0)

    def test_unreadable_queue_is_harmless(self):
        class Broken:
            @property
            def store(self):
                raise RuntimeError("boom")
        t = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=self.rem, applyq=Broken())
        self.assertEqual(t.sync_applyq(), 0)


class DraftTest(Base):
    def test_draft_uses_only_your_facts_and_is_not_sent(self):
        rec, _ = self.add(role="Backend Python Developer")
        d = self.t.draft(rec["id"], 5)
        self.assertIn("Backend Python Developer", d["body"])
        self.assertIn("served two million requests a day", d["body"])
        self.assertIn("Sam Example", d["body"])
        self.assertTrue(d["facts_used"])
        self.assertFalse(d["sent"])
        self.assertNotRegex(d["body"], r"\d{2,}\s*(million|years)")      # no invented numbers
        second = self.t.draft(rec["id"], 10)
        self.assertIn("once more", second["body"])

    def test_draft_without_matching_fact_says_so(self):
        rec, _ = self.add(role="Pastry Chef")
        d = self.t.draft(rec["id"])
        self.assertEqual(d["facts_used"], [])
        self.assertIn("No confirmed fact", d["note"])
        self.assertNotIn("One thing from my background", d["body"])

    def test_draft_without_profile_leaves_a_blank_to_fill(self):
        t = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=self.rem, jobprefs=self.jp, applyq=self.aq, profile_fn=lambda c: {})
        rec, _ = t.add({"company": "Acme", "role": "Dev"})
        self.assertIn("[FILL: your name]", t.draft(rec["id"])["body"])


def many(t, source, n, role="Python Developer", company="Co"):
    out = []
    for i in range(n):
        rec, _ = t.add({"company": f"{company}{source}{i}", "role": role, "source": source})
        out.append(rec)
    return out


class StatsTest(Base):
    def test_rates_per_source_keyword_resume_and_days(self):
        a = many(self.t, "greenhouse", 4, role="Backend Engineer")
        b = many(self.t, "boards", 4, role="Frontend Engineer")
        self.t.update(a[0]["id"], {"resume_version": "v1"})
        self.t.update(a[1]["id"], {"resume_version": "v2"})
        self.advance(days=4)
        self.t.set_status(a[0]["id"], "interview")
        self.t.set_status(a[1]["id"], "rejected")
        s = self.t.stats()
        src = {r["key"]: r for r in s["by_source"]}
        self.assertEqual((src["greenhouse"]["applied"], src["greenhouse"]["replies"], src["greenhouse"]["interviews"]), (4, 2, 1))
        self.assertEqual(src["boards"]["replies"], 0)
        kw = {r["key"]: r for r in s["by_keyword"]}
        self.assertEqual(kw["backend"]["replies"], 2)
        self.assertEqual(kw["engineer"]["applied"], 8)
        res = {r["key"]: r for r in s["by_resume"]}
        self.assertEqual(res["v1"]["interviews"], 1)
        self.assertEqual(res["(not recorded)"]["applied"], 6)
        self.assertEqual(s["days_to_first_reply"], {"n": 2, "median": 4, "mean": 4.0})

    def test_withdrawn_is_left_out(self):
        recs = many(self.t, "greenhouse", 3)
        self.t.set_status(recs[0]["id"], "withdrawn")
        self.assertEqual(self.t.stats()["total"]["applied"], 2)

    def test_weak_source_suggestion_is_a_note_when_profiles_read_all_sources(self):
        many(self.t, "remoteboard", 12)
        s = {x["id"]: x for x in self.t.suggestions()}["weak_source:remoteboard"]
        self.assertIn("remoteboard gave 0 replies from 12 applications: lower its weight", s["text"])
        self.assertFalse(s["adjustable"])
        self.assertIn("no per-source weight", s["note"])
        with self.assertRaises(jobtrack.TrackError):
            self.t.accept("weak_source:remoteboard")
        self.assertEqual(self.jp.calls, [])

    def test_accept_removes_the_source_from_profiles_that_list_it(self):
        self.jp.profiles = [{"id": "p1", "name": "Remote jobs", "sources": ["remoteboard", "greenhouse"], "include": []},
                            {"id": "p2", "name": "Other", "sources": ["greenhouse"], "include": []}]
        many(self.t, "remoteboard", 12)
        s = {x["id"]: x for x in self.t.suggestions()}["weak_source:remoteboard"]
        self.assertTrue(s["adjustable"])
        self.assertEqual([p["id"] for p in s["applies_to"]], ["p1"])
        out = self.t.accept("weak_source:remoteboard")
        self.assertEqual(out["changed"], ["Remote jobs"])
        self.assertEqual(self.jp.calls, [("p1", {"sources": ["greenhouse"]})])
        self.assertNotIn("weak_source:remoteboard", [x["id"] for x in self.t.suggestions()])     # handled, not shown again
        self.assertTrue(any(a[0][0] == "jobtrack_suggestion_accepted" for a in self.audits))

    def test_accept_never_empties_a_source_list(self):
        self.jp.profiles = [{"id": "p1", "name": "Only", "sources": ["remoteboard"], "include": []}]
        many(self.t, "remoteboard", 12)
        with self.assertRaises(jobtrack.TrackError):
            self.t.accept("weak_source:remoteboard")
        self.assertEqual(self.jp.calls, [])

    def test_good_source_and_keyword_suggestions(self):
        self.jp.profiles = [{"id": "p1", "name": "Main", "sources": ["lever"], "include": ["python"]}]
        recs = many(self.t, "greenhouse", 6, role="Kafka Engineer")
        for r in recs[:3]:
            self.t.set_status(r["id"], "interview")
        sug = {x["id"]: x for x in self.t.suggestions()}
        self.assertIn("greenhouse gave 3 interviews from 6 applications", sug["good_source:greenhouse"]["text"])
        self.assertTrue(sug["good_source:greenhouse"]["adjustable"])
        self.assertTrue(sug["good_keyword:kafka"]["adjustable"])
        self.t.accept("good_keyword:kafka")
        self.assertEqual(self.jp.calls[-1], ("p1", {"include": ["python", "kafka"]}))
        self.t.accept("good_source:greenhouse")
        self.assertEqual(self.jp.calls[-1], ("p1", {"sources": ["lever", "greenhouse"]}))

    def test_dismiss_hides_a_suggestion(self):
        many(self.t, "remoteboard", 12)
        self.t.dismiss("weak_source:remoteboard")
        self.assertNotIn("weak_source:remoteboard", [x["id"] for x in self.t.suggestions()])
        with self.assertRaises(jobtrack.NotFound):
            self.t.dismiss("weak_source:remoteboard")
        with self.assertRaises(jobtrack.NotFound):
            self.t.accept("nonsense")

    def test_too_few_applications_gives_no_suggestion(self):
        many(self.t, "remoteboard", 5)
        self.assertEqual([x for x in self.t.suggestions() if x["kind"] == "weak_source"], [])

    def test_jobprefs_missing_is_fine(self):
        t = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=self.rem, jobprefs=None, applyq=self.aq)
        t.jp  # noqa: B018 - real engine is created on the temp data folder; no profiles
        many(t, "remoteboard", 12)
        s = {x["id"]: x for x in t.suggestions()}["weak_source:remoteboard"]
        self.assertFalse(s["adjustable"])


class SummaryTest(Base):
    def test_weekly_summary(self):
        recs = many(self.t, "greenhouse", 4)
        self.advance(days=2)
        self.t.set_status(recs[0]["id"], "interview")
        self.t.set_status(recs[1]["id"], "replied")
        self.advance(days=4)
        s = self.t.summary()
        self.assertEqual((s["applied"], s["replies"], s["interviews"]), (4, 2, 1))
        self.assertEqual(s["best_source"], "greenhouse")
        self.assertIn("applied to 4 jobs", s["text"])
        self.assertIn("follow-ups due", s["text"])
        self.assertIn("Best source so far: greenhouse (2 of 4 replied)", s["text"])

    def test_empty_summary(self):
        self.assertIn("applied to 0 jobs", self.t.summary()["text"])


class RouteTest(Base):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(jobtrack.make_router(self.ctx, clock=lambda: self.now, reminders=self.rem, jobprefs=self.jp, applyq=self.aq,
                                                     profile_fn=lambda c: PROFILE))
        self.c = TestClient(self.app, cookies={"hub_session": "yes"})
        self.anon = TestClient(self.app)

    def test_auth_required_everywhere(self):
        for method, path in (("get", "/api/jobtrack"), ("get", "/api/jobtrack/summary"), ("get", "/api/jobtrack/stats"),
                             ("post", "/api/jobtrack/applications"), ("post", "/api/jobtrack/check"),
                             ("post", "/api/jobtrack/applications/aaaaaaaaaaaa/status"), ("get", "/api/jobtrack/applications/aaaaaaaaaaaa/draft"),
                             ("delete", "/api/jobtrack/applications/aaaaaaaaaaaa"), ("post", "/api/jobtrack/suggestions/x:y/accept")):
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)

    def test_full_flow(self):
        r = self.c.post("/api/jobtrack/applications", json={"company": "Acme", "role": "Python Developer", "source": "referral"})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["record"]["id"]
        chk = self.c.post("/api/jobtrack/check", json={"company": "acme", "role": "python developer", "posted_at": "2026-09-01"}).json()
        self.assertEqual(len(chk["duplicates"]), 1)
        self.assertTrue(chk["stale"])
        dup = self.c.post("/api/jobtrack/applications", json={"company": "Acme", "role": "Python Developer"}).json()
        self.assertEqual(len(dup["warnings"]), 1)
        ov = self.c.get("/api/jobtrack").json()
        self.assertEqual(len(ov["records"]), 2)
        self.assertEqual(ov["settings"]["followup_days"], [5, 10])
        self.assertIn("summary", ov)
        st = self.c.post(f"/api/jobtrack/applications/{rid}/status", json={"status": "replied", "note": "hello"})
        self.assertEqual(st.json()["status"], "replied")
        self.assertEqual(self.c.post(f"/api/jobtrack/applications/{rid}/status", json={"status": "bogus"}).status_code, 400)
        self.assertEqual(self.c.patch(f"/api/jobtrack/applications/{rid}", json={"notes": "n"}).json()["notes"], "n")
        d = self.c.get(f"/api/jobtrack/applications/{rid}/draft?day=5").json()
        self.assertFalse(d["sent"])
        self.assertEqual(self.c.post(f"/api/jobtrack/applications/{rid}/call", json={"call": "yes"}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/jobtrack/applications/{rid}/call", json={"call": True}).status_code, 200)
        self.assertEqual(self.c.post(f"/api/jobtrack/applications/{rid}/followups/5/done").status_code, 200)
        self.assertEqual(self.c.post(f"/api/jobtrack/applications/{rid}/followups/99/done").status_code, 404)
        self.assertEqual(self.c.get("/api/jobtrack/summary").json()["applied"], 2)
        self.assertEqual(self.c.delete(f"/api/jobtrack/applications/{rid}").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/jobtrack/applications/{rid}").status_code, 404)

    def test_bad_ids_and_validation(self):
        self.assertEqual(self.c.get("/api/jobtrack/applications/not-an-id/draft").status_code, 404)
        self.assertEqual(self.c.post("/api/jobtrack/applications", json={"company": "x"}).status_code, 400)
        self.assertEqual(self.c.post("/api/jobtrack/suggestions/nope/accept").status_code, 404)

    def test_overview_imports_from_the_queue(self):
        self.aq.store.items.append(qitem(1))
        self.assertEqual(len(self.c.get("/api/jobtrack").json()["records"]), 1)
        self.assertEqual(len(self.c.get("/api/jobtrack").json()["records"]), 1)

    def test_accept_route_changes_only_the_profile(self):
        self.jp.profiles = [{"id": "p1", "name": "Main", "sources": ["remoteboard", "x"], "include": []}]
        many(self.t, "remoteboard", 12)          # same data file as the router's engine
        r = self.c.post("/api/jobtrack/suggestions/weak_source:remoteboard/accept", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.jp.calls, [("p1", {"sources": ["x"]})])

    def test_switched_off(self):
        self.config.jobtrack_enabled = False
        self.assertEqual(self.c.get("/api/jobtrack").status_code, 404)
        self.assertEqual(jobtrack.today_line(self.config), "")


class NothingIsSentTest(Base):
    def test_whole_flow_never_touches_send(self):
        self.aq.store.items.append(qitem(1))
        rec, _ = self.add()
        self.t.overview()
        self.t.draft(rec["id"])
        self.advance(days=12)
        self.t.overview()
        self.t.set_status(rec["id"], "ghosted")
        self.assertEqual(self.aq.sent, [])
        self.assertEqual({i["source"] for i in self.rem.items.values()} - {"jobtrack"}, set())

    def test_module_has_no_send_post_or_network_code(self):
        src = (Path(jobtrack.__file__)).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        for banned in ("gmailx", "smtplib", "httpx", "requests", "urllib", "aiohttp", "socket", "subprocess", "webbrowser", "playwright", "formfill"):
            self.assertNotIn(banned, code.replace("InProcessClient", ""), banned)
        self.assertIsNone(re.search(r"\.send\(|\bsend_mail\b|\bpublish\(|call\(\"(POST|PUT|PATCH|DELETE)\"", code))
        # the only outbound hop is the assistant tool reading this module's own read-only summary route
        self.assertEqual(code.count("hub.call("), 1)
        self.assertIn('hub.call("GET", "/api/jobtrack/summary")', code)


class ToolAndSettingsTest(Base):
    def test_tool_is_read_only_and_marked_untrusted(self):
        tools = {t.spec["name"]: t for t in jobtrack.build(lambda: None, self.config)}
        self.assertEqual(set(tools), {"jobtrack_summary"})
        self.assertFalse(tools["jobtrack_summary"].needs_approval({}))
        self.assertFalse(tools["jobtrack_summary"].mutating)
        self.assertTrue(tools["jobtrack_summary"].untrusted({}))
        self.assertEqual(TOOL_GROUPS["jobtrack"][0], {"jobtrack_summary"})
        self.assertTrue(TOOL_GROUPS["jobtrack"][1].search("how is my job search going? any replies"))

    def test_config_helpers(self):
        self.assertEqual(jobtrack.ghost_days(self.config), 14)
        self.config.jobtrack_ghost_days = 2
        self.assertEqual(jobtrack.ghost_days(self.config), 14)
        self.config.jobtrack_followup_days = [10, 5, 5, True, 500]
        self.assertEqual(jobtrack.followup_days(self.config), [5, 10])


class SettingsKeysTest(unittest.TestCase):
    """The three config keys are accepted, validated and saved by the Settings route."""

    def setUp(self):
        import httpx
        from hub import auth, settings
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config_path = self.root / "hub.json"
        from hub import config as cfg
        cfg.save(self.config, self.config_path)
        app = FastAPI()
        ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, audit=lambda *a, **k: None, get_app=lambda: app,
                  need_auth=lambda: None, config_path=self.config_path)
        app.include_router(settings.make_router(ctx, transport=httpx.MockTransport(lambda r: httpx.Response(404))))
        self.client = TestClient(app, base_url="http://testserver")
        self.client.cookies.set(auth.COOKIE, auth.make_session(self.config.session_secret))

    def test_valid_and_invalid_values(self):
        ok = self.client.patch("/api/settings", json={"jobtrack_followup_days": [10, 3], "jobtrack_ghost_days": 20, "jobtrack_enabled": False})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self.config.jobtrack_followup_days, [3, 10])
        self.assertEqual(self.config.jobtrack_ghost_days, 20)
        self.assertFalse(self.config.jobtrack_enabled)
        for body in ({"jobtrack_followup_days": "5,10"}, {"jobtrack_followup_days": [0]}, {"jobtrack_followup_days": [1, 2, 3, 4, 5]},
                     {"jobtrack_followup_days": [True]}, {"jobtrack_ghost_days": 1}, {"jobtrack_ghost_days": True}, {"jobtrack_enabled": "yes"}):
            self.assertEqual(self.client.patch("/api/settings", json=body).status_code, 400, body)
        self.assertEqual(self.client.patch("/api/settings", json={"jobtrack_followup_days": []}).status_code, 200)
        self.assertEqual(self.config.jobtrack_followup_days, [])


if __name__ == "__main__":
    unittest.main()
