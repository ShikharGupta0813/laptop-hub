"""Tests for job insights (hub/jobinsights.py): funnel, rejection patterns, score check, skill gaps, weekly experiment. Invented
records, fake clock. Plain code only, no model, no mail, no network."""
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import jobinsights, jobtrack
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth
from tests.test_jobtrack import FakeApplyq, FakeJobprefs, FakeReminders, PROFILE

NOW = dt.datetime(2026, 11, 16, 10, 0)           # a Monday


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="insights-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        self.now = NOW
        self.jt = jobtrack.Jobtrack(self.ctx, clock=lambda: self.now, reminders=FakeReminders(), jobprefs=FakeJobprefs([]), applyq=FakeApplyq([]),
                                    profile_fn=lambda c: PROFILE)
        self.ins = jobinsights.Insights(self.ctx, clock=lambda: self.now, jobtrack=self.jt, found_fn=lambda: [], postings_fn=lambda: [],
                                        facts_fn=lambda: ["Built a payments API in Python", "Skills: Python, SQL"])

    def app(self, n=1, day="2026-11-02", end=None, **kw):
        """n applications; `end` is the final status (applied, replied, interview, offer, rejected)."""
        out = []
        for i in range(n):
            rec, _ = self.jt.add({"company": f"Co{len(self.jt._data()['records'])}", "role": kw.pop("role", "Python Developer") if i == 0 else kw.get("role", "Python Developer"),
                                  "applied_at": day, **{k: v for k, v in kw.items() if k != "role"}})
            if end and end != "applied":
                if end == "interview":
                    self.jt.set_status(rec["id"], "replied")
                self.jt.set_status(rec["id"], end)
            out.append(rec)
        return out


class FunnelTest(Base):
    def test_funnel_per_source_family_resume_hour_day(self):
        self.app(4, source="greenhouse", resume_version="Data", role="Data Analyst", applied_hour=9)
        self.app(2, source="greenhouse", resume_version="Data", role="Data Analyst", applied_hour=9, end="replied")
        self.app(1, source="lever", resume_version="Backend", role="Backend Developer", applied_hour=15, end="interview")
        o = self.ins.overview()
        f = o["funnel"]
        src = {r["key"]: r for r in f["by_source"]}
        self.assertEqual((src["greenhouse"]["applied"], src["greenhouse"]["replies"]), (6, 2))
        self.assertEqual(src["lever"]["interviews"], 1)
        self.assertEqual({r["key"] for r in f["by_role_family"]}, {"data", "backend"})
        self.assertEqual({r["key"]: r["applied"] for r in f["by_resume"]}, {"Data": 6, "Backend": 1})
        self.assertEqual({r["key"] for r in f["by_hour"]}, {"before 11:00", "14:00 to 18:00"})
        self.assertEqual(f["by_weekday"][0]["key"], "Monday")           # 2026-11-02 is a Monday
        self.assertEqual(f["total"]["applied"], 7)

    def test_found_and_triaged_columns_are_optional(self):
        self.app(2, source="greenhouse")
        self.assertIsNone(self.ins.overview()["funnel"]["total"]["found"])           # no finder: columns empty, nothing breaks
        self.ins.found_fn = lambda: [{"source_id": "greenhouse", "triage": {"verdict": "apply"}}, {"source_id": "greenhouse", "triage": "skip"},
                                     {"source_id": "lever"}]
        f = self.ins.overview()["funnel"]
        g = next(r for r in f["by_source"] if r["key"] == "greenhouse")
        self.assertEqual((g["found"], g["triaged_apply"]), (2, 1))
        self.assertEqual((f["total"]["found"], f["total"]["triaged_apply"]), (3, 1))

    def test_finder_failure_is_survivable(self):
        def boom():
            raise RuntimeError("finder down")
        self.ins.found_fn = boom
        self.app(1)
        self.assertEqual(self.ins.overview()["funnel"]["total"]["applied"], 1)

    def test_withdrawn_is_left_out(self):
        r = self.app(2)
        self.jt.set_status(r[0]["id"], "withdrawn")
        self.assertEqual(self.ins.overview()["funnel"]["total"]["applied"], 1)


class RejectionTest(Base):
    def test_none_yet(self):
        self.app(2)
        self.assertEqual(self.ins.overview()["rejections"]["n"], 0)

    def test_stage_days_and_traits(self):
        self.now = dt.datetime(2026, 11, 9, 10, 0)
        self.app(3, source="board-x", end="rejected")                                  # straight rejections, 7 days after 2 Nov
        self.app(1, source="board-x", day="2026-11-01", end="interview")
        self.app(6, source="greenhouse")
        self.now = NOW
        r = self.ins.overview()["rejections"]
        self.assertEqual(r["n"], 3)
        self.assertEqual(r["stages"][0]["stage"], "straight after applying (screening)")
        self.assertEqual(r["days"]["median"], 7)
        self.assertTrue(any("board-x" in t["trait"] for t in r["traits"]))

    def test_after_interview_stage(self):
        self.app(1, end="interview")
        rec = self.jt._data()["records"][0]
        self.jt.set_status(rec["id"], "rejected")
        self.assertEqual(self.ins.overview()["rejections"]["stages"][0]["stage"], "after an interview")


class ScoreTest(Base):
    def test_not_enough_data_says_so(self):
        self.app(3, score=70)
        s = self.ins.overview()["score_check"]
        self.assertEqual(s["verdict"], "not_enough")
        self.assertEqual(s["bands"], [])

    def test_high_scores_predict_replies(self):
        for score in (30, 35, 40):
            self.app(1, score=score)
        for score in (50, 55, 60):
            self.app(1, score=score, end="replied") if score == 50 else self.app(1, score=score)
        for score in (80, 85, 90):
            self.app(1, score=score, end="replied")
        s = self.ins.overview()["score_check"]
        self.assertEqual([b["key"] for b in s["bands"]], ["low", "middle", "high"])
        self.assertEqual(s["verdict"], "yes")
        self.assertEqual(s["bands"][2]["reply_rate"], 100)
        self.assertEqual(s["bands"][0]["reply_rate"], 0)

    def test_inverse_and_unscored_are_ignored(self):
        for score in (30, 35, 40):
            self.app(1, score=score, end="replied")
        for score in (50, 55, 60, 80, 85, 90):
            self.app(1, score=score)
        self.app(5)                                                                    # unscored: not counted
        s = self.ins.overview()["score_check"]
        self.assertEqual((s["n"], s["verdict"]), (9, "no"))


class SkillGapTest(Base):
    def test_counts_unconfirmed_skills_only_and_never_adds_facts(self):
        self.ins.postings_fn = lambda: [{"url": "https://x.example/1", "title": "Backend", "description": "Python, SQL, Kubernetes and Terraform required"},
                                        {"url": "https://x.example/2", "title": "Platform", "description": "Kubernetes, AWS, Python"},
                                        {"url": "https://x.example/3", "title": "SRE", "description": "Kubernetes and Docker"}]
        g = self.ins.overview()["skill_gaps"]
        gaps = {x["skill"]: x["count"] for x in g["gaps"]}
        self.assertEqual(gaps["Kubernetes"], 3)
        self.assertEqual(gaps["Terraform"], 1)
        self.assertNotIn("Python", gaps)                                               # confirmed
        self.assertNotIn("SQL", gaps)
        self.assertEqual(g["gaps"][0]["skill"], "Kubernetes")
        self.assertIn("Python", {x["skill"] for x in g["have"]})
        self.assertIn("Nothing here is added", g["note"])

    def test_uses_tracker_posting_text_and_dedupes(self):
        self.app(1, url="https://x.example/1", posting="We use Docker and Kafka")
        self.ins.postings_fn = lambda: [{"url": "https://x.example/1", "title": "Same", "description": "Docker Kafka"}]
        g = self.ins.overview()["skill_gaps"]
        self.assertEqual(g["postings"], 1)
        self.assertEqual({x["skill"]: x["count"] for x in g["gaps"]}["Docker"], 1)

    def test_skill_matching_is_word_bounded(self):
        self.assertEqual(jobinsights.skills_in("JavaScript developer"), {"JavaScript"})
        self.assertNotIn("Java", jobinsights.skills_in("JavaScript developer"))
        self.assertNotIn("Go", jobinsights.skills_in("good to go on a category"))


class ExperimentTest(Base):
    def test_one_per_week_and_stable(self):
        a = self.ins.overview()["experiment"]["current"]
        b = self.ins.overview()["experiment"]["current"]
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["state"], "suggested")
        self.assertEqual(a["week"], "2026-W47")
        self.assertEqual(len(self.ins._data()["experiments"]), 1)

    def test_resume_experiment_from_data(self):
        self.app(5, resume_version="Backend")
        self.app(5, resume_version="Data", end="replied")
        e = self.ins.overview()["experiment"]["current"]
        self.assertEqual((e["kind"], e["target"]), ("resume", "Data"))
        self.assertIn("Data", e["text"])

    def test_start_skip_and_result_check_next_week(self):
        self.app(5, resume_version="Backend")
        self.app(5, resume_version="Data", end="replied")
        e = self.ins.overview()["experiment"]["current"]
        self.ins.start_experiment(e["id"])
        with self.assertRaises(jobtrack.TrackError):
            self.ins.start_experiment(e["id"])
        self.assertEqual(self.ins.overview()["experiment"]["current"]["result"]["verdict"], "too_early")
        # a week later: 6 applications with the Data resume, 5 got replies, against the earlier ones
        self.now = NOW + dt.timedelta(days=7)
        for _ in range(6):
            r, _w = self.jt.add({"company": f"New{_}", "role": "Data Analyst", "applied_at": "2026-11-17", "resume_version": "Data"})
            self.jt.set_status(r["id"], "replied") if _ < 5 else None
        o = self.ins.overview()["experiment"]
        self.assertNotEqual(o["current"]["id"], e["id"])                                # a new week, a new suggestion
        self.assertEqual(o["last"]["id"], e["id"])
        self.assertEqual(o["last"]["result"]["verdict"], "better")
        self.assertEqual(o["last"]["result"]["during"]["applied"], 6)

    def test_skip(self):
        e = self.ins.overview()["experiment"]["current"]
        self.assertEqual(self.ins.skip_experiment(e["id"])["state"], "skipped")
        self.assertEqual(self.ins.overview()["experiment"]["current"]["state"], "skipped")


class RouteTest(Base):
    def test_routes(self):
        app = FastAPI()
        app.include_router(jobinsights.make_router(self.ctx, clock=lambda: self.now, jobtrack=self.jt, found_fn=lambda: [], postings_fn=lambda: [], facts_fn=lambda: []))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.assertEqual(TestClient(app).get("/api/jobinsights").status_code, 401)
        d = c.get("/api/jobinsights").json()
        for key in ("funnel", "rejections", "score_check", "skill_gaps", "experiment"):
            self.assertIn(key, d)
        eid = d["experiment"]["current"]["id"]
        self.assertEqual(c.post(f"/api/jobinsights/experiment/{eid}/start").json()["state"], "active")
        self.assertEqual(c.post(f"/api/jobinsights/experiment/{eid}/start").status_code, 400)
        self.assertEqual(c.post("/api/jobinsights/experiment/nope/skip").status_code, 404)
        self.config.jobinsights_enabled = False
        self.assertEqual(c.get("/api/jobinsights").status_code, 404)


if __name__ == "__main__":
    unittest.main()
