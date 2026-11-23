"""Tests for the job digest (hub/jobdigest.py): daily top 10, weekly review, follow-up nudges. Invented data, fake clock, fake finds,
fake reminders. No model, no mail, no network."""
import datetime as dt
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import jobdigest, jobtrack
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth
from tests.test_jobtrack import FakeApplyq, FakeJobprefs, FakeReminders, PROFILE

MON = dt.datetime(2026, 11, 16, 7, 0)        # a Monday, before the digest hour
SUN = dt.datetime(2026, 11, 22, 9, 30)       # a Sunday


def find(n, title="Python Developer", company="Acme", score=60, hours_ago=3, verdict="apply", status="new", now=MON, **kw):
    return {"id": f"{n:016x}", "title": title, "company": company, "url": f"https://jobs.example.test/{n}", "source": "Example board",
            "score": score, "found_ts": (now - dt.timedelta(hours=hours_ago)).timestamp(), "status": status,
            "triage": {"verdict": verdict, "reason": "5 of 6 must-have skills"} if verdict else None, "severity": "low", **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobdigest-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.notes, self.audits = [], []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)),
                       notify=lambda *a, **k: self.notes.append(k or a))
        self.now = MON.replace(hour=9)
        self.finds, self.pushed, self.ats, self.warm = [], [], {}, {}
        self.rem = FakeReminders()
        self.jt_rem = FakeReminders()
        self.jt = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=self.jt_rem, jobprefs=FakeJobprefs([]), applyq=FakeApplyq([]),
                                    profile_fn=lambda c: PROFILE)
        self.dg = jobdigest.Digest(self.ctx, clock=lambda: self.now, finds_fn=lambda: [dict(f) for f in self.finds],
                                   score_fn=lambda f: self.ats.get(f["id"]), warm_fn=lambda c: self.warm.get(c, []),
                                   push_fn=lambda f: self.pushed.append(f["id"]) or True, jobtrack=self.jt, reminders=self.rem,
                                   name_fn=lambda: "Sam Example")

    def advance(self, **kw):
        self.now = self.now + dt.timedelta(**kw)

    def track(self, company="Acme", role="Python Developer", day="2026-11-09", **kw):
        rec, _ = self.jt.add({"company": company, "role": role, "applied_at": day, **kw})
        return rec


class DailyTest(Base):
    def test_ranks_by_ats_then_finder_score_and_drops_skips(self):
        self.finds = [find(1, "Backend Engineer", "Beta", score=50), find(2, "API Developer", "Cora", score=90), find(3, "Data Role", "Dune", score=95, verdict="skip"),
                      find(4, "Platform Engineer", "Eden", score=40), find(5, "Old Find", "Fox", hours_ago=60)]
        self.ats = {f"{1:016x}": 88, f"{2:016x}": 70}              # find 4 has no ATS score: it falls back to the finder score 40
        d = self.dg.build()
        self.assertEqual([i["title"] for i in d["items"]], ["Backend Engineer", "API Developer", "Platform Engineer"])
        self.assertEqual(d["items"][0]["match"], {"score": 88, "kind": "ats"})
        self.assertEqual(d["items"][2]["match"], {"score": 40, "kind": "finder"})
        self.assertEqual((d["considered"], d["passed"]), (4, 3))     # the old find is never considered; the skip is considered but does not pass

    def test_at_most_ten_and_only_new_since_last_digest(self):
        self.finds = [find(i, f"Role {i}", f"Co{i}", score=50 + i) for i in range(1, 15)]
        d = self.dg.build()
        self.assertEqual(len(d["items"]), 10)
        self.assertEqual(d["items"][0]["title"], "Role 14")
        self.advance(days=1)
        self.finds += [find(20, "Fresh Role", "Newco", hours_ago=2, now=self.now)]
        d2 = self.dg.ensure_today()
        self.assertEqual([i["title"] for i in d2["items"]][:1], ["Fresh Role"])
        shown = {i["id"] for i in d["items"]}
        self.assertFalse(shown & {i["id"] for i in d2["items"]})      # nothing is shown twice

    def test_not_built_before_the_hour_and_built_once_after(self):
        self.finds = [find(1)]
        self.now = MON.replace(hour=7)
        self.assertEqual(self.dg.ensure_today(), {})
        self.now = MON.replace(hour=8, minute=5)
        first = self.dg.ensure_today()
        self.assertEqual(len(first["items"]), 1)
        self.finds.append(find(2, "Another", "Zed", hours_ago=0))
        self.advance(hours=3)
        self.assertEqual(self.dg.ensure_today()["generated_at"], first["generated_at"])     # not rebuilt the same day

    def test_hour_setting_is_respected(self):
        self.config.jobdigest_hour = 11
        self.finds = [find(1)]
        self.assertEqual(self.dg.ensure_today(), {})
        self.now = MON.replace(hour=11, minute=1)
        self.assertEqual(len(self.dg.ensure_today()["items"]), 1)

    def test_already_applied_and_blocked_are_left_out(self):
        self.track("Acme", "Python Developer")
        self.finds = [find(1, "Python Developer", "Acme"), find(2, "Other", "Beta", status="blocked"), find(3, "Gone", "Cora", status="dismissed"),
                      find(4, "Fine", "Dune")]
        self.assertEqual([i["title"] for i in self.dg.build()["items"]], ["Fine"])

    def test_warm_path_marks_and_today_line_counts_them(self):
        self.finds = [find(1, company="Beta"), find(2, "Other", "Cora", score=40)]
        self.warm = {"Beta": [{"name": "Riya Sample", "why": "works there now (Beta)"}]}
        d = self.dg.build()
        self.assertEqual(d["items"][0]["warm"][0]["name"], "Riya Sample")
        self.assertEqual(d["items"][1]["warm"], [])
        self.assertEqual(jobdigest.today_line(self.config, clock=lambda: self.now), "2 new jobs in your digest, 1 with someone you know")

    def test_empty_digest_is_stored_and_says_so(self):
        d = self.dg.ensure_today()
        self.assertEqual(d["items"], [])
        self.assertIn("Nothing new", jobdigest.spoken_digest(d))
        self.assertEqual(jobdigest.today_line(self.config, clock=lambda: self.now), "")

    def test_refresh_keeps_the_same_picks_and_adds_new_finds(self):
        self.finds = [find(1, score=70)]
        first = self.dg.ensure_today()
        self.finds.append(find(2, "Late Find", "Zed", score=80, hours_ago=0))
        self.dg._last_refresh = 0
        again = self.dg.refresh()
        self.assertEqual([i["title"] for i in again["items"]], ["Late Find", "Python Developer"])
        self.assertEqual(first["since_ts"], again["since_ts"])
        with self.assertRaises(jobdigest.DigestError):
            self.dg.refresh()                                            # a second one straight away is refused


class NotifyTest(Base):
    def test_one_notification_a_day_and_switchable(self):
        self.finds = [find(1)]
        self.dg.ensure_today(notify=True)
        self.dg.ensure_today(notify=True)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0]["source"], "jobdigest")
        self.advance(days=1)
        self.finds = [find(2, "Next", "Zed", hours_ago=1, now=self.now)]
        self.dg.ensure_today(notify=True)
        self.assertEqual(len(self.notes), 2)

    def test_off_and_empty_send_nothing(self):
        self.config.jobdigest_notify = False
        self.finds = [find(1)]
        self.dg.ensure_today(notify=True)
        self.assertEqual(self.notes, [])
        self.config.jobdigest_notify = True
        self.advance(days=1)
        self.finds = []
        self.dg.ensure_today(notify=True)
        self.assertEqual(self.notes, [])

    def test_screen_open_never_notifies(self):
        self.finds = [find(1)]
        self.dg.overview()
        self.assertEqual(self.notes, [])


class QueueTest(Base):
    def test_queue_uses_the_push_and_marks_the_item(self):
        self.finds = [find(1)]
        self.dg.ensure_today()
        item = self.dg.queue(f"{1:016x}")
        self.assertTrue(item["queued"])
        self.assertEqual(self.pushed, [f"{1:016x}"])
        with self.assertRaises(jobdigest.NotFound):
            self.dg.queue(f"{9:016x}")

    def test_scam_cannot_be_queued(self):
        self.finds = [find(1)]
        self.dg.ensure_today()
        self.finds = [find(1, status="blocked")]
        with self.assertRaises(jobdigest.DigestError):
            self.dg.queue(f"{1:016x}")


class WeeklyTest(Base):
    def test_counts_best_resume_and_one_suggestion(self):
        self.now = SUN
        for i in range(4):
            r = self.track(f"A{i}", "Backend Developer", day="2026-11-17", resume_version="Backend", source="greenhouse")
            if i < 3:
                self.jt.set_status(r["id"], "replied")
        self.jt.set_status(self.jt._data()["records"][0]["id"], "interview")
        for i in range(4):
            self.track(f"B{i}", "Data Analyst", day="2026-11-18", resume_version="Data", source="lever")
        w = self.dg.weekly()
        self.assertEqual((w["applied"], w["replies"], w["interviews"]), (8, 3, 1))
        self.assertEqual(w["best_resume"]["key"], "Backend")
        self.assertEqual(w["best_opening"]["key"], "backend")
        self.assertTrue(w["is_review_day"])
        self.assertIn("Use the resume 'Backend' more", w["suggestion"])
        self.assertIn("sent 8 applications", w["text"])

    def test_old_applications_are_not_counted_and_empty_is_honest(self):
        self.now = SUN
        self.track("Old", "Backend Developer", day="2026-10-01")
        w = self.dg.weekly()
        self.assertEqual(w["applied"], 0)
        self.assertIn("No applications went out", w["suggestion"])
        self.assertIsNone(w["best_resume"])
        self.assertFalse(jobdigest.Digest(self.ctx, clock=lambda: MON, jobtrack=self.jt).weekly()["is_review_day"])

    def test_a_week_with_no_tracker_data_says_what_to_do(self):
        self.now = SUN
        self.assertIn("Nothing is tracked", self.dg.weekly()["suggestion"])


class NudgeTest(Base):
    def setUp(self):
        super().setUp()
        self.config.jobtrack_followup_days = []          # the tracker is quiet, so this module's own day is free

    def test_no_reply_nudge_has_a_draft_and_a_reminder_and_never_doubles(self):
        rec = self.track(day="2026-11-12")               # day 7 is 2026-11-19
        self.assertEqual(self.dg.nudge_pass(), 1)
        self.assertEqual(self.dg.nudge_pass(), 0)
        n = self.dg.nudges()
        self.assertEqual(len(n), 1)
        self.assertEqual((n[0]["kind"], n[0]["due"][:10]), ("noreply", "2026-11-19"))
        self.assertIn("Python Developer", n[0]["body"])
        self.assertTrue("Sam Example" in n[0]["body"] or "[FILL" in n[0]["body"])
        self.assertFalse(n[0]["sent"])
        self.assertEqual(len(self.rem.items), 1)
        self.assertEqual(list(self.rem.items.values())[0]["source"], "jobdigest")
        self.assertEqual(rec["status"], "applied")

    def test_the_trackers_own_followup_covers_it(self):
        self.config.jobtrack_followup_days = [5, 10]
        self.track(day="2026-11-12")                     # tracker reminds on 11-17 and 11-22; our 11-19 is within two days of 11-17
        self.assertEqual(self.dg.nudge_pass(), 0)
        self.assertEqual(self.rem.items, {})

    def test_changes_when_the_number_of_days_changes_and_far_apart_days_are_both_kept(self):
        self.config.jobtrack_followup_days = [14]
        self.track(day="2026-11-12")
        self.assertEqual(self.dg.nudge_pass(), 1)

    def test_status_change_cancels_the_open_nudge(self):
        rec = self.track(day="2026-11-12")
        self.dg.nudge_pass()
        self.jt.set_status(rec["id"], "replied")
        self.dg.nudge_pass()
        self.assertEqual(self.dg.nudges(), [])
        self.assertEqual(self.rem.items, {})

    def test_an_old_import_is_not_chased(self):
        self.track(day="2026-10-01")
        self.assertEqual(self.dg.nudge_pass(), 0)

    def test_interview_gets_thank_you_within_a_day_and_a_status_check(self):
        rec = self.track(day="2026-11-09")
        self.now = dt.datetime(2026, 11, 17, 14, 0)
        self.jt.set_status(rec["id"], "replied")
        self.jt.set_status(rec["id"], "interview")
        self.dg.nudge_pass()
        kinds = {n["kind"]: n for n in self.dg.nudges()}
        self.assertEqual(set(kinds), {"thanks", "status"})
        self.assertEqual(kinds["thanks"]["due"], "2026-11-17T16:00")
        self.assertEqual(kinds["status"]["due"][:10], "2026-11-24")
        self.assertIn("Thank you for taking the time", kinds["thanks"]["body"])
        self.assertNotIn("I have", kinds["thanks"]["body"])             # no claim about the owner is invented
        self.assertEqual(self.dg.nudge_pass(), 0)

    def test_late_evening_interview_moves_the_thank_you_to_next_morning(self):
        rec = self.track(day="2026-11-09")
        self.now = dt.datetime(2026, 11, 17, 21, 30)
        self.jt.set_status(rec["id"], "interview")
        self.dg.nudge_pass()
        self.assertEqual({n["kind"]: n["due"] for n in self.dg.nudges()}["thanks"], "2026-11-18T09:00")

    def test_setting_the_interview_time_replaces_the_nudges(self):
        rec = self.track(day="2026-11-09")
        self.now = dt.datetime(2026, 11, 17, 14, 0)
        self.jt.set_status(rec["id"], "interview")
        self.dg.nudge_pass()
        self.dg.set_interview(rec["id"], "2026-11-18T11:00")
        due = {n["kind"]: n["due"] for n in self.dg.nudges()}
        self.assertEqual(due["thanks"][:10], "2026-11-18")
        self.assertEqual(due["status"][:10], "2026-11-25")
        self.assertEqual(len(self.dg.nudges()), 2)
        with self.assertRaises(jobdigest.DigestError):
            self.dg.set_interview(rec["id"], "next week")

    def test_done_removes_the_reminder_and_off_switch_stops_everything(self):
        self.track(day="2026-11-12")
        self.dg.nudge_pass()
        nid = self.dg.nudges()[0]["id"]
        self.dg.nudge_done(nid)
        self.assertEqual((self.dg.nudges(), self.rem.items), ([], {}))
        self.assertEqual(self.dg.nudge_pass(), 0)                        # the done one is remembered, not made again
        self.config.jobdigest_nudges = False
        self.track("Other", "Go Developer", day="2026-11-13")
        self.assertEqual(self.dg.nudge_pass(), 0)

    def test_interview_nudge_is_not_made_for_a_withdrawn_or_rejected_one(self):
        rec = self.track(day="2026-11-09")
        self.jt.set_status(rec["id"], "rejected")
        self.assertEqual(self.dg.nudge_pass(), 0)


class RouteTest(Base):
    def client(self):
        app = FastAPI()
        app.include_router(jobdigest.make_router(self.ctx, clock=lambda: self.now, finds_fn=lambda: [dict(f) for f in self.finds],
                                                 score_fn=lambda f: None, warm_fn=lambda c: [], push_fn=lambda f: True, jobtrack=self.jt,
                                                 reminders=self.rem))
        return app, TestClient(app, cookies={"hub_session": "yes"})

    def test_one_call_gives_everything_and_needs_login(self):
        self.finds = [find(1)]
        app, c = self.client()
        self.assertEqual(TestClient(app).get("/api/jobdigest").status_code, 401)
        d = c.get("/api/jobdigest").json()
        self.assertEqual(set(d), {"digest", "weekly", "nudges", "settings"})
        self.assertEqual(len(d["digest"]["items"]), 1)
        self.assertEqual(d["settings"]["hour"], 8)

    def test_queue_route_and_bad_ids(self):
        self.finds = [find(1)]
        _, c = self.client()
        c.get("/api/jobdigest")
        self.assertEqual(c.post(f"/api/jobdigest/items/{1:016x}/queue").json()["queued"], True)
        self.assertEqual(c.post("/api/jobdigest/items/zzz/queue").status_code, 404)
        self.assertEqual(c.post(f"/api/jobdigest/items/{7:016x}/queue").status_code, 404)
        self.assertEqual(c.post("/api/jobdigest/nudges/zzz/done").status_code, 404)
        self.assertEqual(c.post("/api/jobdigest/interviews/zzz", json={"when": "2026-11-18T10:00"}).status_code, 404)
        self.assertTrue(self.audits)

    def test_switched_off_answers_404(self):
        self.config.jobdigest_enabled = False
        _, c = self.client()
        self.assertEqual(c.get("/api/jobdigest").status_code, 404)
        self.assertEqual(c.get("/api/jobdigest/weekly").status_code, 404)
        self.assertEqual(jobdigest.today_line(self.config), "")


class ToolAndSourceTest(Base):
    def test_tools_are_read_only_and_marked_untrusted(self):
        tools = {t.spec["name"]: t for t in jobdigest.build(lambda: None, self.config)}
        self.assertEqual(set(tools), {"job_digest", "job_week_review"})
        for t in tools.values():
            self.assertFalse(t.needs_approval({}))
            self.assertFalse(t.mutating)
            self.assertTrue(t.untrusted({}))
        self.assertEqual(TOOL_GROUPS["jobdigest"][0], {"job_digest", "job_week_review"})
        self.assertTrue(TOOL_GROUPS["jobdigest"][1].search("what's new in jobs?"))
        self.assertTrue(TOOL_GROUPS["jobdigest"][1].search("how was my week"))

    def test_module_has_no_send_or_network_code(self):
        src = Path(jobdigest.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        for banned in ("gmailx", "smtplib", "httpx", "requests", "urllib", "aiohttp", "socket", "subprocess", "webbrowser", "playwright", "formfill"):
            self.assertNotIn(banned, code.replace("InProcessClient", ""), banned)
        self.assertIsNone(re.search(r"\.send\(|\bsend_mail\b|\bpublish\(", code))
        self.assertEqual(code.count("hub.call("), 2)
        self.assertNotIn("set_status(", code.replace("eng.set_status(find_id", ""))     # only the finder's "queued" mark, never an application status


class SettingsTest(Base):
    def test_defaults(self):
        c = self.config
        self.assertEqual((c.jobdigest_enabled, c.jobdigest_hour, c.jobdigest_notify, c.jobdigest_weekly_day, c.jobdigest_nudge_days),
                         (True, 8, True, 6, 7))


if __name__ == "__main__":
    unittest.main()
