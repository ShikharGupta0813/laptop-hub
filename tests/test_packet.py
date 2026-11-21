"""Master resumes, the batch tailoring queue and the application packet (hub/resumes.py, hub/packet.py).

Invented owner and postings only; the application queue is the real one on a temp folder with a mailer that fails the test if it is touched.
The rules under test:
- a master resume is built only from the facts; a family's focus words never add a skill the owner has not confirmed;
- every tailored resume records which master and version it came from, in the text the job tracker groups its reply stats by;
- the queue is bounded, works one item at a time, never blocks the event loop and sends nothing;
- a packet is a draft: putting it in the queue writes a draft, and nothing is ever sent."""
import asyncio
import copy
import datetime as dt
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import answerbank, applyq, ats, jobtrack, packet, resumes, tailor as T
from hub import factbase as F
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 9, 15, 0)

PROFILE = {
    "name": "Asha Verma", "headline": "Backend engineer",
    "summary": "Backend engineer who builds reliable payment APIs.",
    "skills": ["Python", "PostgreSQL", "Docker", "Terraform", "SQL", "Tableau"],
    "experience": [
        {"title": "Senior Backend Engineer", "company": "Payco", "period": "2021 - Present",
         "bullets": ["Built UPI payment APIs in Python handling 1M requests per day", "Tuned PostgreSQL queries and cut latency by 35%", "Mentored 3 junior engineers"]},
        {"title": "Data Analyst", "company": "Shopmate", "period": "2017 - 2021",
         "bullets": ["Built Tableau dashboards and SQL reports for 40 stores", "Wrote Docker based deployment scripts"]}],
    "education": "B.Tech Computer Science, Example Institute",
    "links": {"github": "https://github.com/asha-verma-example", "email": "asha@example.test"},
}

JOB = {"title": "Senior Python Backend Engineer", "company": "Acme Pay", "url": "https://jobs.example.test/acme/1", "contact_email": "jobs@acme.test",
       "description": """About us
Acme Pay builds payment tools for small shops across India.

Requirements
- 5+ years of Python
- Experience with Kubernetes and PostgreSQL
- Strong communication skills

Nice to have
- Experience with GraphQL

Application questions
Why are you interested in this role?
Describe a system you scaled.

Compensation: 25-35 LPA. Remote friendly.
"""}

JOB2 = {"title": "Data Analyst", "company": "Beta Retail", "url": "https://jobs.example.test/beta/2",
        "description": "Requirements\n- Experience with SQL and Tableau\n- Excel reporting\nSalary $60,000 - $70,000 per year."}


class Mail:
    def __getattr__(self, name):
        raise AssertionError(f"nothing may be sent: {name}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="packet-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits = []
        self.profile = copy.deepcopy(PROFILE)
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)))
        self.clock = lambda: NOW
        self._aq = applyq.Applyq(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, gmailx=Mail())
        resumes._QUEUES.clear()

    def aq(self):
        return self._aq

    def kw(self):
        return {"clock": self.clock, "profile_fn": lambda c: self.profile}

    def masters(self):
        return resumes.Masters(self.ctx, **self.kw())

    def queue(self):
        return resumes.TailorQueue(self.ctx, **self.kw(), applyq_fn=self.aq)

    def packets(self):
        return packet.Packets(self.ctx, **self.kw(), applyq_fn=self.aq)

    def item(self, job=JOB, **over):
        item, _ = self.aq().add({**job, **over})
        return item

    def client(self):
        app = FastAPI()
        app.include_router(resumes.make_router(self.ctx, **self.kw(), applyq_fn=self.aq))
        app.include_router(packet.make_router(self.ctx, **self.kw(), applyq_fn=self.aq))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(c.close)
        return c


class MasterTests(Base):
    def test_create_from_facts_with_version_label(self):
        m = self.masters().create("Backend", "backend")
        self.assertEqual((m["name"], m["family"], m["current"], m["label"]), ("Backend", "backend", 1, "Backend v1"))
        self.assertFalse(m["stale"])
        ms = self.masters()
        text = T.to_text(resumes.version_of(ms.get(m["id"]))["resume"])
        self.assertIn("Built UPI payment APIs", text)
        fb = ms.facts()
        ids = set(resumes.version_of(ms.get(m["id"]))["fact_ids"])
        self.assertTrue(ids and ids <= set(fb.by_id))

    def test_the_family_focus_never_adds_an_unconfirmed_skill(self):
        # devops focus names kubernetes, aws, ansible...; the owner has confirmed only Docker and Terraform.
        m = self.masters().create("DevOps", "devops")
        text = T.to_text(resumes.version_of(self.masters().get(m["id"]))["resume"]).lower()
        for never in ("kubernetes", "aws", "ansible", "jenkins", "monitoring"):
            self.assertNotIn(never, text)
        self.assertIn("terraform", text)
        # a hand-written focus list is steering only as well
        m2 = self.masters().create("Platform", "", ["kubernetes", "helm", "istio"])
        text2 = T.to_text(resumes.version_of(self.masters().get(m2["id"]))["resume"]).lower()
        self.assertNotIn("helm", text2)
        self.assertNotIn("istio", text2)

    def test_focus_orders_the_owners_own_facts(self):
        data = self.masters().create("Data", "data")
        back = self.masters().create("Backend", "backend")
        ms = self.masters()

        def skills(mid):
            lines = T.to_text(resumes.version_of(ms.get(mid))["resume"]).splitlines()
            return lines[lines.index("SKILLS") + 1]
        self.assertLess(skills(data["id"]).index("Tableau"), skills(back["id"]).index("Tableau"))
        self.assertEqual(sorted(skills(data["id"]).split(", ")), sorted(skills(back["id"]).split(", ")))     # same facts, different order

    def test_rebuild_makes_a_new_version_only_when_something_changed(self):
        ms = self.masters()
        m = ms.create("Backend", "backend")
        again = ms.rebuild(m["id"])
        self.assertFalse(again["changed"])
        self.assertEqual(again["current"], 1)
        self.profile["skills"].append("Redis")
        self.assertTrue(ms.public(ms.get(m["id"]))["stale"])           # the facts moved on
        v2 = ms.rebuild(m["id"], "added Redis")
        self.assertTrue(v2["changed"])
        self.assertEqual((v2["current"], v2["label"]), (2, "Backend v2"))
        self.assertFalse(v2["stale"])
        self.assertEqual([v["n"] for v in v2["versions"]], [2, 1])
        self.assertEqual(ms.use_version(m["id"], 1)["label"], "Backend v1")
        with self.assertRaises(LookupError):
            ms.use_version(m["id"], 9)

    def test_rules(self):
        ms = self.masters()
        ms.create("Backend", "backend")
        with self.assertRaises(resumes.ResumeError):
            ms.create("backend", "backend")                                # same name
        with self.assertRaises(resumes.ResumeError):
            ms.create("", "")
        for n in range(resumes.MAX_MASTERS - 1):
            ms.create(f"Role {n}", "", ["python"])
        with self.assertRaises(resumes.ResumeError):
            ms.create("One too many", "", ["python"])
        mid = ms.list()[0]["id"]
        ms.delete(mid)
        with self.assertRaises(LookupError):
            ms.get(mid)
        self.profile = {}
        with self.assertRaises(resumes.ResumeError):
            self.masters().create("Empty", "backend")

    def test_label_fits_what_the_tracker_stores(self):
        m = self.masters().create("N" * 70, "", ["python"])
        self.assertLessEqual(len(m["label"]), 80)

    def test_choose_picks_the_family_that_matches_the_posting(self):
        ms = self.masters()
        back = ms.create("Backend", "backend")
        data = ms.create("Data", "data")
        fb = ms.facts()
        self.assertEqual(ms.choose("", {"title": "Senior Python Backend Engineer", "description": "APIs, microservices and PostgreSQL"}, fb)["id"], back["id"])
        picked = ms.choose("", {"title": "Data Analyst", "description": "SQL, Tableau dashboards, reporting"}, fb)
        self.assertEqual((picked["id"], picked["auto"]), (data["id"], True))
        self.assertIsNone(ms.choose("", {"title": "Chef", "description": "kitchen knives"}, fb))
        self.assertIsNone(ms.choose("none", JOB, fb))
        self.assertEqual(ms.choose(data["id"], JOB, fb)["auto"], False)
        with self.assertRaises(LookupError):
            ms.choose("ms-nope", JOB, fb)

    def test_a_tailored_resume_records_its_master_and_version(self):
        ms = self.masters()
        m = ms.create("Backend", "backend")
        v = asyncio.run(T.Tailor(self.ctx, **self.kw(), applyq_fn=self.aq).preview({"job": JOB}))
        self.assertEqual(v["master"]["label"], "Backend v1")
        self.assertEqual((v["master"]["id"], v["master"]["version"], v["master"]["auto"]), (m["id"], 1, True))
        self.assertEqual(v["resume_version"], "Backend v1")
        self.profile["skills"].append("Redis")
        v2 = asyncio.run(T.Tailor(self.ctx, **self.kw(), applyq_fn=self.aq).preview({"job": JOB}))
        self.assertTrue(v2["master"]["stale"])                              # tells the owner to rebuild
        plain = asyncio.run(T.Tailor(self.ctx, **self.kw(), applyq_fn=self.aq).preview({"job": JOB, "master_id": "none"}))
        self.assertEqual((plain["master"], plain["resume_version"]), (None, ""))

    def test_no_masters_means_no_record_and_tailoring_still_works(self):
        v = asyncio.run(T.Tailor(self.ctx, **self.kw(), applyq_fn=self.aq).preview({"job": JOB}))
        self.assertEqual((v["master"], v["resume_version"]), (None, ""))

    def test_the_tracker_reply_stats_group_by_the_master_version(self):
        self.masters().create("Backend", "backend")
        item = self.item()
        t = T.Tailor(self.ctx, **self.kw(), applyq_fn=self.aq)
        v = asyncio.run(t.preview({"application_id": item["id"]}))
        t.apply_to_application(v["id"], item["id"])
        stored = self.aq().get(item["id"])
        self.assertEqual(stored["resume_version"], "Backend v1")
        self.aq().mark(item["id"], "applied")

        class Rem:
            def create(self, *a, **k):
                return {"id": "r1"}

            def delete(self, *a, **k):
                return True

            def update(self, *a, **k):
                return {}

        tracker = jobtrack.Jobtrack(self.ctx, clock=self.clock, reminders=Rem(), jobprefs=None, applyq=self.aq(), profile_fn=lambda c: self.profile)
        self.assertEqual(tracker.sync_applyq(), 1)
        rows = {r["key"]: r for r in tracker.stats()["by_resume"]}
        self.assertIn("Backend v1", rows)
        self.assertEqual(rows["Backend v1"]["applied"], 1)


class QueueTests(Base):
    def test_add_is_bounded(self):
        q = self.queue()
        ids = [self.item({**JOB, "url": f"https://jobs.example.test/n/{n}"})["id"] for n in range(resumes.MAX_PER_CALL + 3)]
        with self.assertRaises(resumes.ResumeError):
            q.add({})
        out = q.add({"application_ids": ids})                                 # more than one call may take: the extras are ignored
        self.assertEqual(len(out["added"]), resumes.MAX_PER_CALL)
        self.assertEqual(q.add({"application_ids": ids[:2]})["skipped"][0]["reason"], "already in the queue")
        for batch in range(3):
            q.add({"jobs": [{"title": f"Job {batch}-{n}", "description": "Requirements\n- Python"} for n in range(resumes.MAX_PER_CALL)]})
        self.assertLessEqual(q.pending(), resumes.MAX_PENDING)
        full = q.add({"jobs": [{"title": "One more", "description": "Python"}]})
        self.assertEqual(full["added"], [])
        self.assertIn("waiting postings", full["skipped"][0]["reason"])

    def test_bad_refs_are_skipped_not_fatal(self):
        out = self.queue().add({"application_ids": ["abcdef123456", "nope"], "jobs": [{"title": ""}, {"title": "Real", "description": "Requirements\n- Python"}]})
        self.assertEqual(len(out["added"]), 1)
        self.assertEqual(len(out["skipped"]), 3)

    def test_one_at_a_time_in_order(self):
        q = self.queue()
        for n in range(3):
            q.add({"jobs": [{"title": f"Job {n}", "description": "Requirements\n- Python"}]})
            time.sleep(0.01)
        order, live, peak = [], [0], [0]
        real = q.tailor.preview

        async def slow(body):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            order.append(body["job"]["title"])
            await asyncio.sleep(0.02)
            out = await real(body)
            live[0] -= 1
            return out
        q.tailor.preview = slow
        q.pause = 0
        asyncio.run(q.drain())
        self.assertEqual(order, ["Job 0", "Job 1", "Job 2"])
        self.assertEqual(peak[0], 1)
        self.assertEqual([r["state"] for r in q.list()], ["ready"] * 3)
        self.assertIsNone(q.process_next())                                   # nothing left

    def test_a_second_worker_cannot_start_while_one_is_working(self):
        q = self.queue()
        q.add({"jobs": [{"title": "Job", "description": "Requirements\n- Python"}]})
        self.assertTrue(q.worker.acquire(blocking=False))
        try:
            self.assertIsNone(q.process_next())
            self.assertEqual(q.list()[0]["state"], "queued")
        finally:
            q.worker.release()

    def test_the_event_loop_keeps_running_while_an_item_is_prepared(self):
        q = self.queue()
        q.add({"jobs": [{"title": "Job", "description": "Requirements\n- Python"}]})
        real = q.tailor.preview

        async def slow(body):
            time.sleep(0.4)                                                    # a blocking pause inside the worker thread
            return await real(body)
        q.tailor.preview = slow
        ticks = []

        async def main():
            async def ticker():
                while True:
                    ticks.append(1)
                    await asyncio.sleep(0.02)
            t = asyncio.create_task(ticker())
            await q.drain()
            t.cancel()
        asyncio.run(main())
        self.assertGreater(len(ticks), 8)

    def test_kick_starts_one_worker(self):
        q = self.queue()
        q.add({"jobs": [{"title": "Job", "description": "Requirements\n- Python"}]})

        async def main():
            first, second = q.kick(), q.kick()
            await q._task
            return first, second
        self.assertEqual(asyncio.run(main()), (True, False))
        self.assertEqual(q.list()[0]["state"], "ready")

    def test_one_bad_posting_does_not_stop_the_rest(self):
        q = self.queue()
        q.add({"jobs": [{"title": "Boom", "description": "Requirements\n- Python"}, {"title": "Fine", "description": "Requirements\n- Python"}]})
        real = q.tailor.preview

        async def flaky(body):
            if body["job"]["title"] == "Boom":
                raise RuntimeError("model down")
            return await real(body)
        q.tailor.preview = flaky
        q.pause = 0
        asyncio.run(q.drain())
        states = {r["title"]: r["state"] for r in q.list()}
        self.assertEqual(states, {"Boom": "failed", "Fine": "ready"})
        self.assertIn("model down", [r for r in q.list() if r["title"] == "Boom"][0]["error"])

    def test_requeue_after_restart(self):
        q = self.queue()
        q.add({"jobs": [{"title": "Job", "description": "Requirements\n- Python"}]})
        q._update(q.list()[0]["id"], state="working")
        self.assertEqual(q.requeue_stale(), 1)
        self.assertEqual(q.list()[0]["state"], "queued")

    def test_ready_item_has_resume_cover_and_ats_score(self):
        self.masters().create("Backend", "backend")
        item = self.item()
        q = self.queue()
        q.add({"application_ids": [item["id"]]})
        q.pause = 0
        asyncio.run(q.drain())
        r = q.review(q.list()[0]["id"])
        self.assertEqual(r["state"], "ready")
        self.assertTrue(0 <= r["ats"]["score"] <= 100)
        self.assertEqual(r["master"]["label"], "Backend v1")
        self.assertIn("Acme Pay", r["cover_text"])
        self.assertIn("asha verma", r["resume_text"].lower())
        self.assertEqual(r["ats_detail"]["score"], r["ats"]["score"])
        self.assertNotIn("kubernetes", {k["term"] for k in r["ats_detail"]["matched"]})
        self.assertIn("kubernetes", {k["term"] for k in r["ats_detail"]["missing"]["gap"]})
        self.assertNotIn("kubernetes", r["resume_text"].lower().replace("seeking: senior python backend engineer", ""))
        self.assertEqual(self.aq().get(item["id"])["state"], "ready")          # still just a queued draft

    def test_accept_edit_discard(self):
        item = self.item()
        q = self.queue()
        q.add({"application_ids": [item["id"]], "jobs": [{"title": "Other", "description": "Requirements\n- Python"}]})
        q.pause = 0
        asyncio.run(q.drain())
        by = {r["title"]: r["id"] for r in q.list()}
        # accept writes the cover note into the application's draft and records the resume version; it sends nothing
        out = q.accept(by[item["title"]])
        self.assertTrue(out["nothing_sent"] and out["applied_to_queue"])
        self.assertEqual(self.aq().get(item["id"])["state"], "ready")
        self.assertIn("Acme Pay", self.aq().get(item["id"])["draft"]["body"])
        with self.assertRaises(resumes.ResumeError):
            q.accept(by[item["title"]])                                        # already accepted
        # a pasted posting has no application to write into
        other = q.get(by["Other"])
        self.assertFalse(q.accept(other["id"])["applied_to_queue"])
        # discard removes the tailored version
        item2 = self.item(JOB2)
        q.add({"application_ids": [item2["id"]]})
        asyncio.run(q.drain())
        rid = [r["id"] for r in q.list() if r["state"] == "ready"][0]
        vid = q.get(rid)["variant_id"]
        q.discard(rid)
        with self.assertRaises(LookupError):
            q.tailor.get(vid)
        self.assertEqual(q.get(rid)["state"], "discarded")

    def test_accept_is_refused_while_the_cover_note_has_a_blank_and_edit_fixes_it(self):
        item = self.item()
        q = self.queue()
        q.add({"application_ids": [item["id"]]})
        asyncio.run(q.drain())
        rid = q.list()[0]["id"]
        v = q.tailor.get(q.get(rid)["variant_id"])
        v["cover"]["subject"] = "Application for [FILL: team name]"
        q.tailor._put(v)
        with self.assertRaises(resumes.ResumeError) as cm:
            q.accept(rid)
        self.assertIn("Fill in", str(cm.exception))
        out = q.edit(rid, {"action": "fill", "label": "team name", "value": "the payments team"})
        self.assertTrue(out["edited"])
        self.assertFalse(out["blocked"])
        self.assertTrue(q.accept(rid)["nothing_sent"])
        with self.assertRaises(resumes.ResumeError):
            q.edit(rid, {"action": "fill"})                                    # no longer ready

    def test_nothing_is_ever_sent(self):
        item = self.item()
        q = self.queue()
        q.add({"application_ids": [item["id"]]})
        asyncio.run(q.drain())
        q.accept(q.list()[0]["id"])                                            # Mail() raises if anything touches it
        src = Path(resumes.__file__).read_text(encoding="utf-8")
        for bad in ("send_mail", "gmailx", "smtplib", ".send("):
            self.assertNotIn(bad, src)

    def test_the_queue_file_is_trimmed(self):
        q = self.queue()
        rows = [{"id": f"rq-{n}", "state": "accepted", "created": f"2026-01-{n % 28 + 1:02d}T00:00:{n % 60:02d}", "job": {}} for n in range(150)]
        q._save(rows)
        self.assertLessEqual(len(q._all()), resumes.MAX_STORED // 2)


class QueueRouteTests(Base):
    def test_routes_end_to_end(self):
        item = self.item()
        with TestClient(self.app(), cookies={"hub_session": "yes"}) as c:
            ov = c.get("/api/resumes/overview").json()
            self.assertEqual(ov["masters"], [])
            self.assertIn("backend", [f["key"] for f in ov["families"]])
            made = c.post("/api/resumes/masters", json={"family": "backend"})
            self.assertEqual(made.status_code, 200, made.text)
            self.assertEqual(made.json()["label"], "Backend v1")
            self.assertEqual(c.post("/api/resumes/masters", json={"family": "backend"}).status_code, 400)
            mid = made.json()["id"]
            self.assertIn("Built UPI", c.get(f"/api/resumes/masters/{mid}/versions/1").json()["text"])
            self.assertEqual(c.post(f"/api/resumes/masters/{mid}/rebuild", json={}).json()["changed"], False)
            self.assertEqual(c.put(f"/api/resumes/masters/{mid}", json={"name": "Backend roles"}).json()["name"], "Backend roles")
            r = c.post("/api/resumes/queue", json={"application_ids": [item["id"]]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["added"] and r.json()["queue"][0]["state"] in ("queued", "working", "ready"), True)
            for _ in range(100):
                q = c.get("/api/resumes/queue").json()
                if q["queue"][0]["state"] in ("ready", "failed"):
                    break
                time.sleep(0.1)
            self.assertEqual(q["queue"][0]["state"], "ready", q)
            qid = q["queue"][0]["id"]
            rev = c.get(f"/api/resumes/queue/{qid}").json()
            self.assertIn("resume_text", rev)
            acc = c.post(f"/api/resumes/queue/{qid}/accept")
            self.assertEqual(acc.status_code, 200, acc.text)
            self.assertEqual(acc.json()["resume_version"], "Backend v1")      # a rename does not rewrite the labels already recorded
            self.assertEqual(c.post(f"/api/resumes/queue/{qid}/accept").status_code, 400)
            self.assertEqual(c.get("/api/resumes/queue/rq-nope").status_code, 404)
            self.assertEqual(c.post("/api/resumes/queue", json={}).status_code, 400)
            self.assertEqual(c.delete(f"/api/resumes/masters/{mid}").json(), {"ok": True})
            self.assertEqual(c.delete(f"/api/resumes/masters/{mid}").status_code, 404)
            self.assertEqual(c.post("/api/resumes/queue/clear").json()["removed"], 1)

    def app(self):
        app = FastAPI()
        app.include_router(resumes.make_router(self.ctx, **self.kw(), applyq_fn=self.aq))
        return app

    def test_auth_and_switch(self):
        c = TestClient(self.app())
        for method, path in (("get", "/api/resumes/overview"), ("post", "/api/resumes/masters"), ("post", "/api/resumes/queue"), ("get", "/api/resumes/queue")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, path)
        self.config.tailor_enabled = False
        d = TestClient(self.app(), cookies={"hub_session": "yes"})
        self.assertEqual(d.get("/api/resumes/overview").status_code, 404)

    def test_start_returns_a_stop(self):
        async def main():
            stop = await resumes.start(self.ctx)
            await stop()
        asyncio.run(main())


class PacketPieceTests(unittest.TestCase):
    def test_company_summary_comes_from_the_posting(self):
        s = packet.company_summary(JOB)
        self.assertIn("payment tools for small shops", s["text"])
        self.assertEqual(s["source"], "the 'About' part of the posting")
        self.assertEqual(s["site"], "jobs.example.test")
        self.assertEqual(s["email_domain"], "acme.test")
        flat = packet.company_summary({"title": "Dev", "description": "We make boats. They float. They are blue."})
        self.assertEqual(flat["source"], "the opening of the posting")
        self.assertEqual(packet.company_summary({"title": "Dev", "description": ""})["text"], "")

    def test_pay_range(self):
        self.assertTrue(packet.pay_range(JOB)["found"])
        self.assertIn("25-35 LPA", packet.pay_range(JOB)["lines"][0])
        usd = packet.pay_range(JOB2)
        self.assertEqual(usd["amounts"][0], {"top": 70000.0, "per": "year"})
        none = packet.pay_range({"title": "Dev", "description": "Great team. No numbers here, 5+ years."})
        self.assertFalse(none["found"])
        self.assertEqual(none["lines"], [])

    def test_questions_are_found_and_deduplicated(self):
        qs = packet.detect_questions(JOB, ["What is your notice period?", "Do you like cats?"])
        self.assertIn("Do you like cats?", qs)
        self.assertIn("Why are you interested in this role?", qs)
        self.assertIn("Describe a system you scaled.", qs)
        self.assertEqual(sum(1 for q in qs if "notice" in q.lower()), 1)
        self.assertLessEqual(len(packet.detect_questions(JOB, [f"Question number {n} here?" for n in range(60)])), packet.MAX_QUESTIONS)

    def test_only_web_links_are_links(self):
        self.assertEqual(packet.safe_url("javascript:alert(1)"), "")
        self.assertEqual(packet.safe_url("https://x.example.test/a"), "https://x.example.test/a")


class PacketTests(Base):
    def setUp(self):
        super().setUp()
        bank = answerbank.Bank(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile)
        for e in bank.list():
            if e["label"] == "Notice period":
                bank.update(e["id"], {"answer": "30 days"})

    def test_one_page_has_everything(self):
        item = self.item()
        v = asyncio.run(self.packets().prepare({"application_id": item["id"], "questions": ["What is your notice period?", "What is your gender?"]}))
        self.assertTrue(v["nothing_sent"])
        self.assertEqual(v["job"]["company"], "Acme Pay")
        self.assertIn("asha verma", v["resume"]["text"].lower())
        self.assertIn("Acme Pay", v["cover"]["text"])
        self.assertTrue(0 <= v["ats"]["score"] <= 100)
        self.assertEqual([b["key"] for b in v["ats"]["breakdown"]][:2], ["hard", "soft"])
        self.assertIn("kubernetes", {k["term"] for k in v["ats"]["missing"]["gap"]})
        self.assertIn("payment tools", v["company"]["text"])
        self.assertTrue(v["pay"]["found"])
        self.assertEqual(v["links"]["posting"], "https://jobs.example.test/acme/1")
        self.assertIn("https://github.com/asha-verma-example", v["links"]["yours"])
        self.assertTrue(any(d["href"].startswith("/api/tailor/variants/tv-") for d in v["downloads"]))
        by = {a["question"]: a for a in v["answers"]}
        self.assertEqual((by["What is your notice period?"]["status"], by["What is your notice period?"]["answer"]), ("auto", "30 days"))
        self.assertTrue(by["What is your notice period?"]["usable"])
        self.assertTrue(by["What is your gender?"]["owner_only"])
        self.assertFalse(by["What is your gender?"]["usable"])
        self.assertIn("Why are you interested in this role?", by)
        self.assertEqual(self.aq().get(item["id"])["state"], "ready")          # and still only a draft

    def test_reviewing_does_not_touch_the_queue_or_send(self):
        item = self.item()
        before = self.aq().get(item["id"])
        pk = self.packets()
        v = asyncio.run(pk.prepare({"application_id": item["id"]}))
        pk.view(v["id"])
        pk.text_pack(v["id"])
        after = self.aq().get(item["id"])
        self.assertEqual(before["draft"], after["draft"])                      # nothing changed until the owner taps "put in the queue"
        self.assertEqual(after["state"], "ready")

    def test_the_variant_is_reused_unless_fresh(self):
        item = self.item()
        pk = self.packets()
        a = asyncio.run(pk.prepare({"application_id": item["id"]}))
        b = asyncio.run(pk.prepare({"application_id": item["id"]}))
        c = asyncio.run(pk.prepare({"application_id": item["id"], "fresh": True}))
        self.assertEqual(a["resume"]["variant_id"], b["resume"]["variant_id"])
        self.assertNotEqual(a["resume"]["variant_id"], c["resume"]["variant_id"])

    def test_one_tap_puts_a_draft_in_the_queue_and_sends_nothing(self):
        self.masters().create("Backend", "backend")
        item = self.item()
        pk = self.packets()
        v = asyncio.run(pk.prepare({"application_id": item["id"], "questions": ["What is your notice period?", "What is your gender?"]}))
        out = pk.to_queue(v["id"])
        self.assertTrue(out["nothing_sent"])
        self.assertEqual(out["application_id"], item["id"])
        self.assertFalse(out["created"])
        stored = self.aq().get(item["id"])
        self.assertEqual(stored["state"], "ready")
        self.assertIsNone(stored["sent_at"])
        self.assertEqual(stored["resume_version"], "Backend v1")
        self.assertEqual(stored["draft"]["answers"]["What is your notice period?"], "30 days")
        self.assertNotIn("What is your gender?", stored["draft"]["answers"])    # owner-only questions are never filled
        self.assertEqual(pk.view(v["id"])["state"], "queued")
        self.assertFalse(pk.view(v["id"])["can_queue"])
        again = pk.to_queue(v["id"])                                           # a second tap changes nothing
        self.assertEqual(again["application_id"], item["id"])

    def test_a_pasted_posting_becomes_a_queued_draft(self):
        pk = self.packets()
        v = asyncio.run(pk.prepare({"job": JOB2}))
        self.assertEqual(v["application_id"], "")
        out = pk.to_queue(v["id"])
        self.assertTrue(out["created"])
        stored = self.aq().get(out["application_id"])
        self.assertEqual((stored["state"], stored["source"]), ("ready", "packet"))
        self.assertIn("Tableau", stored["draft"]["body"])
        self.assertIsNone(stored["sent_at"])

    def test_a_blocked_cover_note_stays_visibly_blocked_in_the_queue(self):
        item = self.item()
        pk = self.packets()
        v = asyncio.run(pk.prepare({"application_id": item["id"]}))
        var = pk.tailor.get(v["resume"]["variant_id"])
        var["cover"]["subject"] = "Hello [FILL: name of the hiring manager]"
        pk.tailor._put(var)
        view = pk.view(v["id"])
        self.assertTrue(view["blocked"])
        self.assertFalse([d for d in view["downloads"] if "Cover" in d["label"]])   # no cover download while blocked
        out = pk.to_queue(v["id"])
        self.assertTrue(out["blocked"])
        self.assertIn("[FILL:", self.aq().get(item["id"])["draft"]["body"])         # the queue itself refuses to send a draft with a blank

    def test_text_pack(self):
        pk = self.packets()
        v = asyncio.run(pk.prepare({"job": JOB, "questions": ["What is your notice period?"]}))
        text = pk.text_pack(v["id"])
        for part in ("APPLICATION PACKET", "COVER NOTE", "RESUME", "ANSWERS", "30 days", "PAY IN THE POSTING"):
            self.assertIn(part, text)

    def test_packets_are_listed_and_deleted(self):
        pk = self.packets()
        v = asyncio.run(pk.prepare({"job": JOB2}))
        self.assertEqual(pk.list()[0]["id"], v["id"])
        pk.delete(v["id"])
        with self.assertRaises(LookupError):
            pk.view(v["id"])
        with self.assertRaises(LookupError):
            pk.delete(v["id"])

    def test_bad_input(self):
        pk = self.packets()
        with self.assertRaises(tailor_error()):
            asyncio.run(pk.prepare({"job": {}}))
        with self.assertRaises(LookupError):
            asyncio.run(pk.prepare({"application_id": "abcdef123456"}))
        self.profile = {}
        with self.assertRaises(tailor_error()):
            asyncio.run(pk.prepare({"job": JOB}))

    def test_routes(self):
        item = self.item()
        c = self.client()
        r = c.post("/api/packet/prepare", json={"application_id": item["id"]})
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["id"]
        self.assertEqual(c.get(f"/api/packet/{pid}").json()["id"], pid)
        self.assertEqual(c.get("/api/packet").json()["packets"][0]["id"], pid)
        t = c.get(f"/api/packet/{pid}/text")
        self.assertEqual(t.status_code, 200)
        self.assertIn("APPLICATION PACKET", t.text)
        q = c.post(f"/api/packet/{pid}/queue")
        self.assertEqual(q.status_code, 200, q.text)
        self.assertTrue(q.json()["nothing_sent"])
        self.assertEqual(c.get("/api/packet/pk-nope").status_code, 404)
        self.assertEqual(c.post("/api/packet/prepare", json={}).status_code, 400)
        self.assertEqual(c.post("/api/packet/prepare", json={"application_id": "abcdef123456"}).status_code, 404)
        self.assertEqual(c.delete(f"/api/packet/{pid}").json(), {"ok": True})
        self.assertTrue(any(a[0][0] == "packet_queued" for a in self.audits))

    def test_auth_and_switch(self):
        app = FastAPI()
        app.include_router(packet.make_router(self.ctx))
        c = TestClient(app)
        for method, path in (("get", "/api/packet"), ("post", "/api/packet/prepare"), ("get", "/api/packet/pk-1"), ("post", "/api/packet/pk-1/queue")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, path)
        self.config.tailor_enabled = False
        self.assertEqual(self.client().get("/api/packet").status_code, 404)

    def test_the_module_cannot_send(self):
        src = Path(packet.__file__).read_text(encoding="utf-8")
        for bad in ("send_mail", "gmailx", "smtplib", ".send(", "formfill", "submit("):
            self.assertNotIn(bad, src)


class ToolTests(unittest.TestCase):
    def test_packet_tool_is_a_draft_only_tool(self):
        from hub.assistant import TOOL_GROUPS
        tools = packet.build(lambda: None, None)
        self.assertEqual([t.spec["name"] for t in tools], ["packet_prepare"])
        self.assertFalse(tools[0].needs_approval({}))
        self.assertIn("does not send", tools[0].spec["description"])
        self.assertIn("packet_prepare", TOOL_GROUPS["ats"][0])
        self.assertTrue(TOOL_GROUPS["ats"][1].search("prepare a packet for this job"))


def tailor_error():
    return T.TailorError


if __name__ == "__main__":
    unittest.main()
