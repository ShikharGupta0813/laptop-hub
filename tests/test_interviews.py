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
        self.assertEqual(set(o), {"applications", "stories", "suggestions", "practice", "negotiation", "has_facts", "settings", "sends"})
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
        self.assertEqual(set(tools), {"interview_prep"})
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
