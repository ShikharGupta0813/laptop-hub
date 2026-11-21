"""Interview coach (hub/interviews.py). Fakes only: a fake clock and a fake model that returns canned JSON. Rules under test:
- questions come from the posting by plain code (same posting, same questions); an AI step only adds to them;
- feedback is from rules (length, STAR parts, a number, filler words), and a critique from an AI is optional;
- stories and "tell me about yourself" use only confirmed facts; the negotiation sheet has only the owner's numbers;
- no AI means everything still works; practice answers can be deleted; nothing is sent."""
import asyncio
import copy
import datetime as dt
import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import applyq, factbase as F, interviews as I
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from tests.apiwalk import flat_routes
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 20, 9, 0)
PROFILE = {
    "name": "Sam Rivera", "headline": "Backend engineer", "summary": "Backend engineer who builds reliable data APIs.",
    "skills": ["Python", "FastAPI", "Postgres", "Docker", "Mentoring"],
    "experience": [
        {"title": "Senior Developer", "company": "Initech", "period": "2021 - Present",
         "bullets": ["Built a Python and FastAPI service handling 2M requests per day", "Cut Postgres query time by 40% with indexing",
                     "Mentored 3 junior engineers"]},
        {"title": "Developer", "company": "Hooli", "period": "2016 - 2021", "bullets": ["Wrote Docker based deployment scripts"]}],
    "education": "BSc Computer Science, State University",
}
DESC = """About us
We build data tools for small shops and we ship every week with a small, friendly team.

Responsibilities
- Build and run Python and FastAPI services
- Mentor junior engineers
- Handle production incidents and be on-call

Requirements
- 5+ years of Python
- Experience with PostgreSQL and Docker
- Kubernetes experience

Location: Berlin, Germany. Fast-paced team, tight deadlines, remote friendly."""
JOB = {"title": "Senior Python Backend Engineer", "company": "Acme", "description": DESC}
GOOD = ("At Initech our API was slow when traffic grew. My job was to cut the response time before a big launch. I profiled the queries, "
        "added two indexes and moved a report to a nightly job. As a result the slowest call went from 4 seconds to 1 second and the launch went well, "
        "so that the support team stopped getting complaints about timeouts. I also wrote a short note so the next person knew why we chose those indexes, "
        "and I checked the numbers again the week after the launch to be sure the change had held under real traffic.")


class FakeModel:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, system, user, json_mode=False):
        self.calls.append({"system": system, "user": user})
        if not self.replies:
            raise RuntimeError("model down")
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="int-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.profile = copy.deepcopy(PROFILE)
        self.model = None
        self.ctx = Ctx(config=self.config, model_for=lambda p: self.model, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        self.q = None

    def queue(self):
        if self.q is None:
            class Mail:
                def send_mail(*a, **k):
                    raise AssertionError("the interview coach must never send mail")
            self.q = applyq.Applyq(self.ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile, gmailx=Mail())
        return self.q

    def client(self, authed=True):
        app = FastAPI()
        app.include_router(I.make_router(self.ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile, model_fn=lambda p: self.model,
                                         applyq_fn=lambda: self.queue()))
        self.app = app
        c = TestClient(app, cookies={"hub_session": "yes"} if authed else {})
        self.addCleanup(c.close)
        return c

    def fb(self):
        return F.FactBase.build(self.profile, F.load(self.ctx), this_year=2026)


class QuestionTests(Base):
    def test_skills_and_signals_come_from_the_posting(self):
        res = I.make_questions(JOB, self.fb())
        self.assertEqual(res["generator"], "rules")
        low = [s.lower() for s in res["skills"]]
        for s in ("python", "fastapi", "docker", "kubernetes"):
            self.assertIn(s, low)
        tech = [q for q in res["questions"] if q["kind"] == "technical"]
        beh = [q for q in res["questions"] if q["kind"] == "behavioural"]
        self.assertTrue(any("Python" in q["text"] for q in tech))
        self.assertTrue(any("Kubernetes" in q["why"] for q in tech))             # a skill they ask for even when the owner has no fact
        self.assertTrue(any("mentored" in q["text"] for q in beh))               # "Mentor junior engineers"
        self.assertTrue(any("deadline" in q["text"] for q in beh))
        self.assertTrue(any("production" in q["text"] for q in beh))
        self.assertTrue(any(q["kind"] == "motivation" and "Acme" in q["text"] for q in res["questions"]))
        self.assertLessEqual(len(tech), 8)
        for q in res["questions"]:
            self.assertTrue(q["id"].startswith("q-") and q["why"] and q["text"].endswith(("?", ".")))
        self.assertEqual(len({q["id"] for q in res["questions"]}), len(res["questions"]))

    def test_same_posting_same_questions(self):
        a, b = I.make_questions(JOB, self.fb()), I.make_questions(dict(JOB), self.fb())
        self.assertEqual([q["id"] for q in a["questions"]], [q["id"] for q in b["questions"]])

    def test_a_fact_hint_only_when_the_owner_has_one(self):
        res = I.make_questions(JOB, self.fb())
        py = next(q for q in res["questions"] if q["kind"] == "technical" and "Python" in q["text"])
        self.assertIsNotNone(py["fact"])
        self.assertIn(py["fact"]["id"], self.fb().by_id)
        kube = next(q for q in res["questions"] if "Kubernetes" in q["why"])
        self.assertIsNone(kube["fact"])

    def test_title_only_and_unknown_skills_still_give_questions(self):
        res = I.make_questions({"title": "Pastry Chef", "company": "Cafe"}, None)
        self.assertTrue(res["questions"])
        self.assertTrue(any(q["kind"] == "technical" for q in res["questions"]))
        self.assertEqual(res["skills"], [])

    def test_words_like_rest_and_go_are_not_skills(self):
        res = I.make_questions({"title": "Dev", "description": "Go to the market and rest of the team. We go far."}, None)
        self.assertEqual(res["skills"], [])

    def test_route_with_application_and_pasted_text(self):
        c = self.client()
        item, _ = self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/q", "company": "Globex", "description": "Python, Docker and deadlines."})
        r = c.post("/api/interviews/questions", json={"application_id": item["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["job"]["company"], "Globex")
        r = c.post("/api/interviews/questions", json={"title": "Dev", "description": "Docker and Linux"})
        self.assertTrue(any("Docker" in q["why"] for q in r.json()["questions"]))
        self.assertEqual(c.post("/api/interviews/questions", json={}).status_code, 400)
        self.assertEqual(c.post("/api/interviews/questions", json={"application_id": "nope"}).status_code, 404)

    def test_ai_adds_questions_and_never_replaces(self):
        self.model = FakeModel({"questions": [{"text": "How would you shard a busy Postgres table?", "kind": "technical"},
                                              {"text": "short", "kind": "technical"},
                                              {"text": "Tell me about a time you changed your mind.", "kind": "behavioural"}]})
        r = self.client().post("/api/interviews/questions", json={**JOB, "ai": True}).json()
        self.assertEqual(r["generator"], "rules+ai")
        texts = [q["text"] for q in r["questions"]]
        self.assertIn("How would you shard a busy Postgres table?", texts)
        self.assertNotIn("short", texts)
        self.assertTrue(any(q["source"] == "rules" for q in r["questions"]))
        self.assertEqual([q["source"] for q in r["questions"] if q["source"] == "ai"], ["ai", "ai"])
        sent = self.model.calls[0]["user"]
        self.assertIn("Senior Python Backend Engineer", sent)
        self.assertNotIn("Initech", sent)                                          # no facts about the owner go to the model

    def test_no_ai_or_broken_ai_still_gives_the_rule_questions(self):
        c = self.client()
        r = c.post("/api/interviews/questions", json={**JOB, "ai": True}).json()   # model is None
        self.assertEqual(r["generator"], "rules")
        self.assertIn("No AI model", r["ai_note"])
        base = [q["id"] for q in r["questions"]]
        self.model = FakeModel()
        r = c.post("/api/interviews/questions", json={**JOB, "ai": True}).json()
        self.assertIn("failed", r["ai_note"])
        self.assertEqual([q["id"] for q in r["questions"]], base)
        self.model = FakeModel("not json", "not json either")
        self.assertEqual([q["id"] for q in c.post("/api/interviews/questions", json={**JOB, "ai": True}).json()["questions"]], base)

    def test_ai_off_by_default_makes_no_call(self):
        self.model = FakeModel()
        self.client().post("/api/interviews/questions", json=JOB)
        self.assertEqual(self.model.calls, [])


class FeedbackTests(Base):
    def checks(self, fb):
        return {c["id"]: c["ok"] for c in fb["checks"]}

    def test_a_good_star_answer_with_a_number_passes(self):
        fb = I.rule_feedback("behavioural", GOOD, self.fb())
        ok = self.checks(fb)
        for k in ("length", "situation", "task", "action", "result", "metric", "filler", "ownership"):
            self.assertTrue(ok[k], (k, fb))
        self.assertEqual(fb["passed"], fb["total"])
        self.assertIn("Good", fb["summary"])
        self.assertEqual(fb["seconds"], round(fb["words"] / I.WORDS_PER_SECOND))

    def test_short_vague_answer_fails_with_tips(self):
        fb = I.rule_feedback("behavioural", "I think I would just work hard and talk to the team.", self.fb())
        ok = self.checks(fb)
        self.assertFalse(ok["length"])
        self.assertFalse(ok["metric"])
        self.assertFalse(ok["result"])
        self.assertTrue(all(c["tip"] for c in fb["checks"] if not c["ok"]))
        self.assertIn("Next:", fb["summary"])

    def test_too_long_answer(self):
        fb = I.rule_feedback("behavioural", "I did a thing at work and it went fine. " * 40)
        self.assertFalse(self.checks(fb)["length"])
        self.assertIn("Cut", next(c["tip"] for c in fb["checks"] if c["id"] == "length"))

    def test_filler_words_are_counted(self):
        text = GOOD + " Um, basically, you know, it was kind of like a lot of work, actually, literally."
        fb = I.rule_feedback("behavioural", text)
        self.assertGreaterEqual(len(fb["fillers"]), 5)
        self.assertFalse(self.checks(fb)["filler"])
        self.assertIn("um", fb["fillers"])
        clean = I.rule_feedback("behavioural", GOOD)
        self.assertEqual(clean["fillers"], [])

    def test_all_we_and_no_i_is_flagged(self):
        text = "We had a slow service. Our goal was to fix it. We tried indexes and we added a cache and our team shipped it. We saw 30% faster pages as a result. " * 2
        fb = I.rule_feedback("behavioural", text)
        self.assertFalse(self.checks(fb)["ownership"])

    def test_technical_answers_check_for_a_reason_not_star(self):
        fb = I.rule_feedback("technical", "An index speeds up reads because the database can jump to rows instead of scanning the whole table, "
                                          "but it slows writes. For example, on a 2M row table we indexed the user id and the query took 20 ms.")
        ids = [c["id"] for c in fb["checks"]]
        self.assertIn("reason", ids)
        self.assertNotIn("situation", ids)
        self.assertTrue(self.checks(fb)["reason"])
        self.assertTrue(self.checks(fb)["metric"])

    def test_numbers_that_no_fact_mentions_are_only_a_reminder(self):
        fb = I.rule_feedback("behavioural", GOOD + " We handled 9000 requests per second.", self.fb())
        self.assertIn("9000", " ".join(fb["unverified_numbers"]))
        fb2 = I.rule_feedback("behavioural", "I cut Postgres query time by 40% at Initech.", self.fb())
        self.assertEqual(fb2["unverified_numbers"], [])

    def test_empty_answer_does_not_crash(self):
        fb = I.rule_feedback("behavioural", "")
        self.assertEqual(fb["words"], 0)
        self.assertFalse(self.checks(fb)["length"])


class PracticeTests(Base):
    def post(self, c, **over):
        body = {"question": "Tell me about a time something you built did not work.", "kind": "behavioural", "answer": GOOD, "role": "Engineer", "company": "Acme"}
        return c.post("/api/interviews/practice", json={**body, **over})

    def test_store_list_and_delete_one_and_all(self):
        c = self.client()
        a = self.post(c).json()
        b = self.post(c, answer="I fixed it in 2 days because the logs said so, for example.").json()
        self.assertEqual(a["feedback"]["total"], len(a["feedback"]["checks"]))
        self.assertEqual({p["id"] for p in c.get("/api/interviews/practice").json()["practice"]}, {a["id"], b["id"]})
        self.assertTrue((Path(self.config.data_path()) / "interviews.json").exists())
        self.assertEqual(c.delete(f"/api/interviews/practice/{a['id']}").status_code, 200)
        self.assertEqual(c.delete(f"/api/interviews/practice/{a['id']}").status_code, 404)
        self.assertEqual([p["id"] for p in c.get("/api/interviews/practice").json()["practice"]], [b["id"]])
        r = c.delete("/api/interviews/practice").json()
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(c.get("/api/interviews/practice").json()["practice"], [])
        self.assertNotIn("Initech", (Path(self.config.data_path()) / "interviews.json").read_text(encoding="utf-8"))

    def test_feedback_route_does_not_store(self):
        c = self.client()
        r = c.post("/api/interviews/feedback", json={"question": "Why us?", "kind": "motivation", "answer": "Because I like your product and your team."})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/interviews/practice").json()["practice"], [])

    def test_bad_input(self):
        c = self.client()
        self.assertEqual(self.post(c, question="").status_code, 400)
        self.assertEqual(self.post(c, answer="hi").status_code, 400)

    def test_ai_critique_is_added_when_asked(self):
        self.model = FakeModel({"critique": "Strong result. Add who benefited. Open with the problem."})
        r = self.post(self.client(), ai=True).json()
        self.assertIn("Strong result", r["critique"])
        self.assertTrue(r["feedback"]["checks"])                                 # rules are still there
        user = self.model.calls[0]["user"]
        self.assertIn("Acme", user)
        self.assertNotIn("Sam Rivera", user)

    def test_no_ai_or_broken_ai_gives_rule_feedback_only(self):
        c = self.client()
        r = self.post(c, ai=True).json()                                          # model None
        self.assertEqual(r["critique"], "")
        self.assertIn("No AI model", r["ai_note"])
        self.assertTrue(r["feedback"]["checks"])
        self.model = FakeModel()
        r = self.post(c, ai=True).json()
        self.assertEqual(r["critique"], "")
        self.assertIn("failed", r["ai_note"])
        self.model = FakeModel({"critique": ""})
        self.assertEqual(self.post(c, ai=True).json()["critique"], "")

    def test_ai_is_off_by_default(self):
        self.model = FakeModel({"critique": "x"})
        self.post(self.client())
        self.assertEqual(self.model.calls, [])

    def test_the_store_is_capped(self):
        c = self.client()
        for _ in range(3):
            self.post(c)
        self.assertEqual(len(c.get("/api/interviews/practice").json()["practice"]), 3)


class StoryTests(Base):
    def test_suggestions_come_from_the_owners_bullets(self):
        sug = I.story_suggestions(self.fb())
        self.assertTrue(sug)
        ids = set(self.fb().by_id)
        for s in sug:
            self.assertTrue(set(s["fact_ids"]) <= ids)
            self.assertTrue(s["action"])
            self.assertEqual((s["task"], s["result"]), ("", ""))
        self.assertTrue(sug[0]["metrics"])                                          # bullets with a number first
        self.assertIn("Initech", " ".join(s["situation"] for s in sug))

    def test_crud_and_fact_ids_are_checked(self):
        c = self.client()
        sug = c.get("/api/interviews/stories").json()["suggestions"][0]
        r = c.post("/api/interviews/stories", json={**sug, "task": "Make the API faster", "result": "40% faster", "fact_ids": [*sug["fact_ids"], "fake-id"]})
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertNotIn("fake-id", s["fact_ids"])
        self.assertEqual(s["unverified_numbers"], [])
        s2 = c.put(f"/api/interviews/stories/{s['id']}", json={"result": "Handled 777 customers a day"}).json()
        self.assertIn("777", " ".join(s2["unverified_numbers"]))
        self.assertEqual(s2["action"], s["action"])
        self.assertEqual(len(c.get("/api/interviews/stories").json()["stories"]), 1)
        self.assertEqual(c.delete(f"/api/interviews/stories/{s['id']}").status_code, 200)
        self.assertEqual(c.delete(f"/api/interviews/stories/{s['id']}").status_code, 404)
        self.assertEqual(c.put("/api/interviews/stories/st-none", json={"title": "x"}).status_code, 404)
        self.assertEqual(c.post("/api/interviews/stories", json={}).status_code, 400)

    def test_no_facts_no_suggestions(self):
        self.profile = {}
        self.assertEqual(self.client().get("/api/interviews/stories").json()["suggestions"], [])


class AboutMeTests(Base):
    def test_60_and_90_second_versions_fit_and_stay_in_the_facts(self):
        fb = self.fb()
        a60, a90 = I.about_me(fb, 60), I.about_me(fb, 90)
        self.assertLessEqual(a60["words"], 150)
        self.assertLessEqual(a90["words"], 225)
        self.assertLessEqual(a60["est_seconds"], 63)
        self.assertLessEqual(a90["est_seconds"], 94)
        self.assertGreaterEqual(a90["words"], a60["words"])
        for a in (a60, a90):
            self.assertFalse(a["blocked"], a["blockers"])
            self.assertIn("Senior Developer at Initech", a["text"])
            self.assertIn("10 years", a["text"])
            self.assertNotIn("Kubernetes", a["text"])
            for s in a["sentences"]:
                if s["kind"] == "claim":
                    self.assertTrue(s["fact_ids"] and set(s["fact_ids"]) <= set(fb.by_id))
        self.assertIn("BSc", a90["text"])
        self.assertNotIn("BSc", a60["text"])

    def test_with_a_posting_it_leads_with_matching_skills(self):
        a = I.about_me(self.fb(), 60, JOB)
        self.assertIn("Python", a["text"])
        self.assertIn("interests me", a["text"])
        self.assertFalse(a["blocked"], a["blockers"])

    def test_no_facts_says_so(self):
        a = I.about_me(F.FactBase.build({}, None, this_year=2026), 60)
        self.assertTrue(a["empty"])
        self.assertEqual(a["text"], "")
        self.assertIn("no facts", a["note"].lower())

    def test_route_and_bad_seconds(self):
        c = self.client()
        self.assertEqual(c.post("/api/interviews/about-me", json={"seconds": 90}).status_code, 200)
        self.assertEqual(c.post("/api/interviews/about-me", json={"seconds": 45}).status_code, 400)
        self.assertEqual(c.post("/api/interviews/about-me", json={"seconds": "abc"}).status_code, 400)
        r = c.post("/api/interviews/about-me", json={"seconds": 60, "title": "Dev", "company": "Acme", "description": "Python"})
        self.assertIn("Acme", r.json()["text"])


class BriefTests(Base):
    def test_brief_from_the_posting_text(self):
        b = I.make_brief(JOB, self.fb(), story_count=2)
        self.assertEqual((b["title"], b["company"]), ("Senior Python Backend Engineer", "Acme"))
        self.assertIn("data tools", b["summary"])
        self.assertIn("Mentor junior engineers", b["duties"])
        self.assertTrue(any("Python" in m for m in b["must_haves"]))
        self.assertTrue(any("Kubernetes" in g for g in b["gaps"]))                   # no confirmed fact about it
        self.assertEqual(b["location"], "Berlin, Germany. Fast-paced team, tight deadlines, remote friendly.")
        self.assertTrue(any("Python" in q for q in b["ask_them"]))
        self.assertTrue(any("time zones" in q for q in b["ask_them"]))
        self.assertTrue(any("on-call" in q.lower() for q in b["ask_them"]))
        self.assertLessEqual(len(b["ask_them"]), 10)
        self.assertTrue(any("Kubernetes" in c for c in b["checklist"]))
        self.assertTrue(any("2 saved" in c for c in b["checklist"]))
        self.assertIn("Nothing about the company was looked up", b["note"])

    def test_brief_without_facts_has_no_gaps_but_still_works(self):
        b = I.make_brief(JOB, None)
        self.assertEqual(b["gaps"], [])
        self.assertTrue(b["checklist"] and b["ask_them"])

    def test_route(self):
        c = self.client()
        r = c.post("/api/interviews/brief", json=JOB)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post("/api/interviews/brief", json={}).status_code, 400)


class NegotiationTests(Base):
    def test_sheet_has_only_the_owners_numbers(self):
        c = self.client()
        empty = c.get("/api/interviews/negotiation").json()
        self.assertFalse(empty["set"])
        text = " ".join(empty["scripts"])
        self.assertIn("[FILL: your target]", text)
        self.assertTrue(empty["tips"])
        r = c.put("/api/interviews/negotiation", json={"currency": "EUR", "low": 60000, "target": "70000", "high": 80000, "notice": "one month"}).json()
        self.assertTrue(r["set"])
        text = " ".join(r["scripts"])
        for n in ("60,000", "70,000", "80,000"):
            self.assertIn(n, text)
        self.assertIn("one month", text)
        self.assertNotIn("FILL", text)
        import re
        allowed = {"60,000", "70,000", "80,000"}
        self.assertEqual(set(re.findall(r"\d{1,3}(?:,\d{3})+", text + " ".join(r["tips"]))), allowed)    # no invented market numbers
        self.assertEqual(c.get("/api/interviews/negotiation").json()["target"], 70000)

    def test_validation(self):
        c = self.client()
        for body in ({"low": -1}, {"low": "abc"}, {"high": 1e12}, {"low": 90, "high": 50}, {"target": 10, "low": 20}, {"low": True}):
            self.assertEqual(c.put("/api/interviews/negotiation", json=body).status_code, 400, body)
        r = c.put("/api/interviews/negotiation", json={"low": "", "target": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["low"])


class RouteTests(Base):
    def test_every_route_needs_login(self):
        c = self.client(authed=False)
        for method, path in (("get", "/api/interviews"), ("post", "/api/interviews/questions"), ("post", "/api/interviews/feedback"),
                             ("get", "/api/interviews/practice"), ("post", "/api/interviews/practice"), ("delete", "/api/interviews/practice/x"),
                             ("delete", "/api/interviews/practice"), ("get", "/api/interviews/stories"), ("post", "/api/interviews/stories"),
                             ("put", "/api/interviews/stories/x"), ("delete", "/api/interviews/stories/x"), ("post", "/api/interviews/about-me"),
                             ("post", "/api/interviews/brief"), ("get", "/api/interviews/negotiation"), ("put", "/api/interviews/negotiation")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, (method, path))

    def test_switched_off(self):
        self.config.interviews_enabled = False
        c = self.client()
        self.assertEqual(c.get("/api/interviews").status_code, 404)
        self.assertEqual(c.post("/api/interviews/questions", json=JOB).status_code, 404)

    def test_overview_is_one_call(self):
        c = self.client()
        self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/ov", "company": "Globex", "description": "Python."})
        o = c.get("/api/interviews").json()
        self.assertEqual(set(o), {"applications", "stories", "suggestions", "practice", "negotiation", "has_facts", "settings", "sends", "days", "debriefs", "speak_targets", "wpm_target", "filler_words"})
        self.assertIs(o["sends"], False)
        self.assertTrue(o["has_facts"])
        self.assertEqual(o["applications"][0]["company"], "Globex")

    def test_nothing_is_sent_and_there_is_no_send_route(self):
        c = self.client()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("nothing may open a connection")):
            c.post("/api/interviews/questions", json=JOB)
            c.post("/api/interviews/practice", json={"question": "Why us?", "kind": "motivation", "answer": GOOD})
            c.post("/api/interviews/about-me", json={"seconds": 60})
            c.post("/api/interviews/brief", json=JOB)
            c.get("/api/interviews")
        self.assertEqual(self.queue().list("sent"), [])
        paths = [p for p, _m, _e in flat_routes(self.app)]
        self.assertTrue(len(paths) > 10)
        for p in paths:
            self.assertNotRegex(p, r"/(send|post|publish|submit)\b")


class SpokenFeedbackTests(Base):
    """Spoken practice: the screen records, /api/transcribe makes the text, and these are the numbers (plain arithmetic and word patterns)."""

    def sp(self, text, seconds, **kw):
        return I.spoken_feedback(kw.pop("kind", "behavioural"), text, seconds, kw.pop("pauses", None), None, kw.pop("target", None))

    def test_words_a_minute_and_the_band(self):
        self.assertEqual(self.sp("word " * 120, 60)["wpm"], 120)
        self.assertEqual(self.sp("word " * 120, 60)["wpm_band"], "good")
        self.assertEqual(self.sp("word " * 200, 60)["wpm_band"], "fast")
        self.assertEqual(self.sp("word " * 60, 60)["wpm_band"], "slow")
        self.assertEqual(self.sp("word " * 160, 60)["wpm_band"], "good")          # the edges of the range are good
        self.assertEqual(self.sp("word " * 110, 60)["wpm_band"], "good")
        self.assertEqual(self.sp("word " * 15, 90)["wpm"], 10)
        self.assertIsNone(self.sp("one two three four", 3)["wpm"])                 # too short a recording to say
        self.assertEqual(I.WPM_GOOD, (110, 160))

    def test_speaking_time_against_the_target_for_the_kind(self):
        self.assertEqual(self.sp("word " * 40, 20)["time"]["label"], "short")
        self.assertEqual(self.sp("word " * 120, 90)["time"]["label"], "good")
        self.assertEqual(self.sp("word " * 300, 150)["time"]["label"], "long")
        self.assertEqual(self.sp("word " * 60, 40, kind="motivation")["time"], {"lo": 30, "hi": 90, "ok": True, "label": "good"})
        self.assertEqual(self.sp("word " * 60, 40, kind="behavioural")["time"]["label"], "short")      # the same 40 seconds is short for a story
        self.assertEqual(self.sp("word " * 60, 40, target=(30, 45))["time"]["ok"], True)
        tips = self.sp("word " * 40, 20)["tips"]
        self.assertTrue(any("aim for 60 to 120" in t for t in tips))

    def test_filler_words_are_counted_by_word(self):
        text = "Um, so basically I actually built it, you know, and like, uh it worked. So we shipped. It was like really good."
        f = self.sp(text, 30)["fillers"]
        self.assertEqual(f["by_word"], {"um": 1, "uh": 1, "like": 2, "basically": 1, "actually": 1, "you know": 1, "so": 1})
        self.assertEqual(f["total"], 8)
        self.assertEqual(f["per_100_words"], round(8 / len(text.split()) * 100, 1))
        self.assertFalse(f["ok"])

    def test_ordinary_words_are_not_fillers(self):
        text = "I like Python because it is simple. We chose it so that the team could learn fast. It looks like a clean design so I kept it. So, we moved on."
        f = self.sp(text, 30)["fillers"]
        self.assertEqual(f["by_word"].get("like", 0), 0)       # "like Python" and "looks like a" are not fillers
        self.assertEqual(f["by_word"].get("so", 0), 1)          # only a sentence that starts with "so" counts; "so that" and "so I" do not
        self.assertEqual(self.sp("Clean answer with no filler words at all.", 10)["fillers"]["total"], 0)

    def test_pauses_when_timing_data_exists(self):
        p = self.sp("word " * 100, 60, pauses=[0.5, 2.5, 3.1, 1.0])["pauses"]
        self.assertEqual((p["count"], p["long"], p["longest"], p["total"], p["ok"]), (4, 2, 3.1, 7.1, True))
        bad = self.sp("word " * 100, 60, pauses=[2.0, 2.5, 3.1])
        self.assertFalse(bad["pauses"]["ok"])
        self.assertTrue(any("pauses" in t for t in bad["tips"]))
        self.assertIsNone(self.sp("word " * 100, 60)["pauses"])                    # no timing data: nothing is said about pauses
        self.assertEqual(self.sp("word " * 100, 60, pauses=[])["pauses"]["longest"], 0.0)

    def test_pause_list_is_cleaned(self):
        self.assertEqual(I._pause_list([0.1, "1.5", 2, 9999]), [1.5, 2.0])
        self.assertIsNone(I._pause_list(None))
        with self.assertRaises(I.InterviewError):
            I._pause_list("long")
        with self.assertRaises(I.InterviewError):
            I._pause_list(["x"])

    def test_star_structure_is_checked_for_behavioural_answers_only(self):
        s = self.sp(GOOD, 70)
        ids = {c["id"] for c in s["checks"]}
        self.assertTrue({"situation", "task", "action", "result", "ownership", "metric"} <= ids)
        self.assertNotIn("length", ids)                                             # replaced by speaking time
        self.assertNotIn("filler", ids)                                             # replaced by the filler count
        self.assertTrue(all(c["ok"] for c in s["checks"]), s["checks"])
        t = self.sp("It is fine and works well in most cases for me.", 20, kind="technical")
        self.assertIn("reason", {c["id"] for c in t["checks"]})
        self.assertNotIn("situation", {c["id"] for c in t["checks"]})

    def test_summary_and_errors(self):
        s = self.sp(GOOD, 70)
        self.assertIn("words a minute", s["summary"])
        self.assertIn("structure checks", s["summary"])
        with self.assertRaises(I.InterviewError):
            self.sp("some words here", 0)

    def test_the_routes_keep_the_spoken_numbers_with_the_answer(self):
        c = self.client()
        body = {"question": "Tell me about a time something broke.", "kind": "behavioural", "answer": GOOD, "seconds": 70, "pauses": [0.6, 2.4]}
        r = c.post("/api/interviews/practice", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        sp = r.json()["spoken"]
        self.assertEqual(sp["seconds"], 70.0)
        self.assertEqual(sp["pauses"]["long"], 1)
        self.assertEqual(c.get("/api/interviews/practice").json()["practice"][0]["spoken"]["wpm"], sp["wpm"])
        self.assertNotIn("spoken", c.post("/api/interviews/feedback", json={**body, "seconds": None}).json())      # typed answers have none
        self.assertEqual(c.post("/api/interviews/feedback", json={**body, "target_seconds": [20, 40]}).json()["spoken"]["time"]["label"], "long")
        for bad in ({"seconds": 0}, {"seconds": 5000}, {"seconds": "soon"}, {"pauses": "many"}):
            self.assertEqual(c.post("/api/interviews/feedback", json={**body, **bad}).status_code, 400, bad)


class DebriefTests(Base):
    def seed_stories(self, c):
        a = c.post("/api/interviews/stories", json={"title": "Cut slow queries", "situation": "At Initech the API was slow", "task": "Cut response time",
                                                    "action": "I profiled Postgres queries and added indexes", "result": "Calls went from 4 seconds to 1 second"}).json()
        b = c.post("/api/interviews/stories", json={"title": "Team lunch", "action": "I booked a table"}).json()
        return a, b

    def test_questions_asked_join_the_list_for_that_company(self):
        c = self.client()
        r = c.post("/api/interviews/debriefs", json={"company": "Acme", "role": "Backend Engineer", "round": "Round 1", "interviewer": "Dana Lee", "rating": 4,
                                                     "asked": ["How would you design a rate limiter?", "Tell me about a time you disagreed with your manager.", "How would you design a rate limiter?"],
                                                     "went_well": "Good talk about Postgres.", "to_improve": "Slow down.", "follow_ups": ["Send the code sample"]})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["questions_saved"], d["stories_added"]), (2, 0))             # the repeated question counts once in one debrief
        q = c.post("/api/interviews/questions", json=JOB).json()
        first = q["questions"][:2]
        self.assertEqual({x["source"] for x in first}, {"debrief"})
        self.assertEqual({x["text"] for x in first}, {"How would you design a rate limiter?", "Tell me about a time you disagreed with your manager."})
        self.assertTrue(all("earlier interview" in x["why"] for x in first))
        self.assertEqual({x["kind"] for x in first}, {"technical", "behavioural"})
        self.assertEqual(q["from_debriefs"], 2)                                              # both are new: the rule list has other wording
        self.assertEqual(len({x["id"] for x in q["questions"]}), len(q["questions"]))
        other = c.post("/api/interviews/questions", json={**JOB, "company": "Globex"}).json()
        self.assertNotIn("debrief", {x["source"] for x in other["questions"]})

    def test_a_question_asked_twice_says_so(self):
        c = self.client()
        for _ in range(2):
            c.post("/api/interviews/debriefs", json={"company": "Acme", "asked": ["How do you test an API?"]})
        q = c.post("/api/interviews/questions", json=JOB).json()["questions"][0]
        self.assertEqual(q["text"], "How do you test an API?")
        self.assertIn("(2 times)", q["why"])

    def test_the_debrief_feeds_the_star_story_bank(self):
        c = self.client()
        a, b = self.seed_stories(c)
        r = c.post("/api/interviews/debriefs", json={"company": "Acme", "asked": ["Tell me about a slow system."], "stories_used": [a["id"], "st-nope"],
                                                     "new_stories": [{"title": "Fixed the nightly job", "situation": "Reports failed at Hooli", "task": "Make them reliable",
                                                                      "action": "I split the job and added retries", "result": "No failures for 3 months"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["stories_added"], 1)
        stories = c.get("/api/interviews/stories").json()["stories"]
        self.assertEqual(len(stories), 3)
        new = next(s for s in stories if s["title"] == "Fixed the nightly job")
        self.assertEqual(new["source"], "debrief")
        self.assertEqual(new["debrief_id"], r.json()["id"])
        self.assertEqual(new["result"], "No failures for 3 months")
        used = next(s for s in stories if s["id"] == a["id"])
        self.assertEqual((used["used_count"], used["last_used"]), (1, "2026-11-20"))
        self.assertNotIn("used_count", next(s for s in stories if s["id"] == b["id"]))
        self.assertTrue(new["unverified_numbers"] or new["unverified_numbers"] == [])         # a reminder list, never a block

    def test_a_bad_story_in_a_debrief_changes_nothing(self):
        c = self.client()
        r = c.post("/api/interviews/debriefs", json={"company": "Acme", "asked": ["Q one?"], "new_stories": [{"situation": "no title and no action"}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(c.get("/api/interviews/debriefs").json()["debriefs"], [])
        self.assertNotIn("debrief", {x["source"] for x in c.post("/api/interviews/questions", json=JOB).json()["questions"]})

    def test_validation_is_plain(self):
        c = self.client()
        for body in ({}, {"company": "Acme"}, {"asked": ["x?"]}, {"company": "Acme", "asked": ["x?"], "rating": 9}, {"company": "Acme", "asked": ["x?"], "rating": "good"},
                     {"company": "Acme", "asked": ["x?"], "date": "yesterday"}):
            r = c.post("/api/interviews/debriefs", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.json()["detail"])

    def test_deleting_a_debrief_takes_its_questions_back(self):
        c = self.client()
        d = c.post("/api/interviews/debriefs", json={"company": "Acme", "asked": ["How do you test an API?"]}).json()
        d2 = c.post("/api/interviews/debriefs", json={"company": "Acme", "asked": ["How do you test an API?"]}).json()
        self.assertEqual(c.delete(f"/api/interviews/debriefs/{d['id']}").status_code, 200)
        self.assertIn("earlier interview.", c.post("/api/interviews/questions", json=JOB).json()["questions"][0]["why"])        # asked once now
        self.assertEqual(c.delete(f"/api/interviews/debriefs/{d2['id']}").status_code, 200)
        self.assertNotIn("debrief", {x["source"] for x in c.post("/api/interviews/questions", json=JOB).json()["questions"]})
        self.assertEqual(c.delete(f"/api/interviews/debriefs/{d2['id']}").status_code, 404)

    def test_debrief_from_an_application(self):
        c = self.client()
        item, _ = self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/d", "company": "Globex", "description": "Python and Docker."})
        r = c.post("/api/interviews/debriefs", json={"application_id": item["id"], "asked": ["Why Python?"]}).json()
        self.assertEqual((r["company"], r["role"]), ("Globex", "Python dev"))
        self.assertEqual(c.post("/api/interviews/debriefs", json={"application_id": "nope", "asked": ["x?"]}).status_code, 404)

    def test_thank_you_note_uses_the_existing_draft_and_sends_nothing(self):
        c = self.client()
        d = c.post("/api/interviews/debriefs", json={"company": "Acme", "role": "Backend Engineer", "interviewer": "Dana Lee", "asked": ["Why Python?"]}).json()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("nothing may open a connection")):
            r = c.post(f"/api/interviews/debriefs/{d['id']}/thank-you", json={"topic": "the Postgres design question"})
        self.assertEqual(r.status_code, 200, r.text)
        note = r.json()
        self.assertEqual(note["kind"], "thank_you")
        self.assertIn("Dana", note["text"])
        self.assertEqual(self.queue().list("sent"), [])
        from hub import networking
        drafts = networking.Networking(self.ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile).drafts()
        self.assertEqual(drafts[0]["id"], note["id"])
        self.assertEqual(c.post("/api/interviews/debriefs/db-nope/thank-you", json={}).status_code, 404)
        self.profile.clear()
        self.assertEqual(c.post(f"/api/interviews/debriefs/{d['id']}/thank-you", json={}).status_code, 400)       # no facts: a plain message


class DayPackTests(Base):
    BODY = {"title": JOB["title"], "company": "Acme", "description": DESC, "when": "2026-11-24 15:00 IST", "place": "https://meet.example.test/abc",
            "who": ["Dana Lee (Engineering Manager)", "Ravi Shah"], "round": "Technical"}

    def test_the_pack_has_every_part(self):
        c = self.client()
        r = c.post("/api/interviews/day-pack", json=self.BODY)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["day"]["when"], "2026-11-24 15:00 IST")
        self.assertEqual(p["day"]["who"], ["Dana Lee (Engineering Manager)", "Ravi Shah"])
        self.assertEqual(p["role"]["company"], "Acme")
        self.assertLessEqual(len(p["questions"]), 5)
        self.assertEqual(len(p["questions"]), 5)
        self.assertEqual(len(p["stories"]), 3)
        self.assertTrue(all(s["starter"] for s in p["stories"]))                       # no saved stories yet: starters from the facts
        self.assertTrue(p["ask_them"] and len(p["ask_them"]) <= 5)
        self.assertTrue(p["checklist"][0].startswith("Open the interview link"))
        self.assertFalse(p["company_brief"]["available"])
        self.assertIn("Company brief", p["company_brief"]["hint"])
        self.assertFalse(p["sends"])
        kinds = [q["kind"] for q in p["questions"]]
        self.assertEqual((kinds.count("technical"), kinds.count("behavioural")), (2, 2))
        self.assertIn("Likely questions:", p["read_aloud"])
        self.assertIn("2026-11-24 15:00 IST", p["read_aloud"])
        self.assertIn("No company brief is saved yet.", p["read_aloud"])
        self.assertNotIn("\n\n\n", p["read_aloud"])

    def test_best_stories_are_the_ones_that_fit_the_posting_and_are_complete(self):
        c = self.client()
        DebriefTests.seed_stories(DebriefTests, c)
        for t in ("Python API tuning", "Docker rollout at Hooli"):
            c.post("/api/interviews/stories", json={"title": t, "situation": "x", "task": "y", "action": f"I did {t} with Python and Docker", "result": "it worked"})
        p = c.post("/api/interviews/day-pack", json=self.BODY).json()
        titles = [s["title"] for s in p["stories"]]
        self.assertEqual(len(titles), 3)
        self.assertNotIn("Team lunch", titles)                                            # nothing in common with the posting, no result
        self.assertTrue(all(not s["starter"] for s in p["stories"]))

    def test_a_used_story_is_ranked_a_little_lower(self):
        stories = [{"id": "a", "title": "Python tuning", "action": "Python", "result": "ok", "situation": "s", "task": "t", "used_count": 3},
                   {"id": "b", "title": "Python rollout", "action": "Python", "result": "ok", "situation": "s", "task": "t"}]
        best = I.best_stories(stories, {"title": "Python dev", "description": "Python"}, [], n=2)
        self.assertEqual([s["id"] for s in best], ["b", "a"])

    def test_earlier_questions_from_debriefs_come_first(self):
        c = self.client()
        c.post("/api/interviews/debriefs", json={"company": "Acme", "asked": ["How would you design a rate limiter?"], "to_improve": "Slow down.", "round": "Round 1"})
        p = c.post("/api/interviews/day-pack", json=self.BODY).json()
        self.assertEqual(p["questions"][0]["text"], "How would you design a rate limiter?")
        self.assertEqual(p["questions"][0]["source"], "debrief")
        self.assertEqual(p["earlier"][0]["to_improve"], "Slow down.")
        self.assertIn("rate limiter", p["read_aloud"])

    def test_a_saved_company_brief_is_included_without_any_new_request(self):
        from hub import companyintel
        base = Path(self.config.data_path())
        base.mkdir(parents=True, exist_ok=True)
        key = "acme|acme.example"
        brief = {"company": "Acme", "summary": "Acme makes inventory tools.", "generated_at": "2026-11-19T09:00:00", "cached": False,
                 "red_flags": [{"id": "news", "text": "News headline: layoffs."}, {"id": "scope", "text": "Flags come from public text only."}],
                 "news": [{"title": "Acme opens an office"}]}
        (base / "companyintel.json").write_text(json.dumps({key: {"brief": brief, "expires_ts": NOW.timestamp() + 86400, "saved_ts": NOW.timestamp()}}), encoding="utf-8")
        c = self.client()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("no network for a saved brief")):
            p = c.post("/api/interviews/day-pack", json=self.BODY).json()
        cb = p["company_brief"]
        self.assertTrue(cb["available"])
        self.assertEqual(cb["summary"], "Acme makes inventory tools.")
        self.assertEqual(cb["red_flags"], ["News headline: layoffs."])                     # the generic scope note is not a red flag
        self.assertIn("Acme makes inventory tools.", p["read_aloud"])
        self.assertTrue(companyintel.enabled(self.config))

    def test_saving_the_day_and_listing_it(self):
        c = self.client()
        p = c.post("/api/interviews/day-pack", json={**self.BODY, "save": True}).json()
        self.assertTrue(p["day"]["id"].startswith("dy-"))
        days = c.get("/api/interviews/days").json()["days"]
        self.assertEqual((days[0]["company"], days[0]["place"]), ("Acme", "https://meet.example.test/abc"))
        self.assertEqual(c.get("/api/interviews").json()["days"][0]["id"], p["day"]["id"])
        self.assertEqual(c.post("/api/interviews/day-pack", json=self.BODY).json()["day"].get("id"), None)        # not saved unless asked
        self.assertEqual(c.delete(f"/api/interviews/days/{p['day']['id']}").status_code, 200)
        self.assertEqual(c.delete(f"/api/interviews/days/{p['day']['id']}").status_code, 404)

    def test_from_an_application_and_nothing_is_sent(self):
        c = self.client()
        item, _ = self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/dp", "company": "Globex", "description": "Python, Docker and deadlines."})
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("nothing may open a connection")):
            p = c.post("/api/interviews/day-pack", json={"application_id": item["id"], "who": "Dana, Ravi"}).json()
        self.assertEqual(p["role"]["company"], "Globex")
        self.assertEqual(p["day"]["who"], ["Dana", "Ravi"])
        self.assertEqual(self.queue().list("sent"), [])
        self.assertEqual(c.post("/api/interviews/day-pack", json={"application_id": "nope"}).status_code, 404)

    def test_friday_tool_reads_the_pack_out(self):
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(I.make_router(open_ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile))
        self.config.session_generation = 0
        tools = {t.spec["name"]: t for t in I.build(lambda: app, self.config)}
        tool = tools["interview_day_pack"]
        self.assertFalse(tool.needs_approval({}))
        self.assertTrue(tool.untrusted({}))
        ok, text = asyncio.run(tool.run({"title": JOB["title"], "company": "Acme", "description": DESC, "when": "Tuesday 3 pm", "who": ["Dana"]}))
        self.assertTrue(ok, text)
        self.assertIn("Tuesday 3 pm", text)
        self.assertIn("Likely questions:", text)
        self.assertIn("Nothing was sent", text)
        ok, _ = asyncio.run(tool.run({}))
        self.assertFalse(ok)


class WiringTests(Base):
    def test_wiring_settings_and_tool_group(self):
        from hub import server, settings
        self.assertIn("interviews", server.FEATURE_MODULES)
        for key in ("interviews_enabled", "interviews_ai"):
            self.assertIn(key, settings.ALLOWED_KEYS)
        names, rx = TOOL_GROUPS["networking"]
        self.assertIn("interview_prep", names)
        self.assertTrue(rx.search("help me with interview questions for this job"))
        self.assertTrue(rx.search("how do I negotiate the offer"))

    def test_tool_is_read_only_and_untrusted(self):
        tools = {t.spec["name"]: t for t in I.build(lambda: self.app, self.config)}
        self.assertEqual(set(tools), {"interview_prep", "interview_day_pack"})
        self.assertFalse(tools["interview_prep"].needs_approval({}))
        self.assertTrue(tools["interview_prep"].untrusted({}))

    def test_tool_round_trips(self):
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(I.make_router(open_ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile))
        self.config.session_generation = 0
        tool = I.build(lambda: app, self.config)[0]
        ok, text = asyncio.run(tool.run({"part": "questions", "title": JOB["title"], "company": "Acme", "description": DESC}))
        self.assertTrue(ok, text)
        self.assertIn("Kubernetes", text)
        self.assertIn("Nothing was sent", text)
        ok, text = asyncio.run(tool.run({"part": "brief", "title": JOB["title"], "description": DESC}))
        self.assertTrue(ok, text)
        self.assertIn("Gaps", text)
        ok, text = asyncio.run(tool.run({"part": "about_me", "seconds": 90}))
        self.assertTrue(ok, text)
        self.assertIn("Initech", text)
        ok, text = asyncio.run(tool.run({"part": "questions"}))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
