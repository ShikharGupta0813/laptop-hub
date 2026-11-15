"""Networking drafts (hub/networking.py). Fakes only: a fake clock, a fake model that returns canned JSON, the real application
queue on a temp folder with a mailer that fails the test if it is ever used. Rules under test:
- a draft is built only from the owner's facts; a claim they do not back is flagged and BLOCKS the draft;
- at most 120 words; "nudge due" is a suggestion after N days; contacts have CRUD;
- no AI (or a bad AI answer) falls back to the plain draft; nothing is ever sent (there is not even a send route)."""
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

from hub import applyq, factbase as F, networking as N
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from tests.apiwalk import flat_routes
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 20, 9, 0)
PROFILE = {
    "name": "Sam Rivera", "headline": "Backend engineer", "summary": "Backend engineer who builds reliable data APIs.",
    "skills": ["Python", "FastAPI", "Postgres", "Docker"],
    "experience": [
        {"title": "Senior Developer", "company": "Initech", "period": "2021 - Present",
         "bullets": ["Built a Python and FastAPI service handling 2M requests per day", "Cut Postgres query time by 40% with indexing"]},
        {"title": "Developer", "company": "Hooli", "period": "2016 - 2021", "bullets": ["Wrote Docker based deployment scripts"]}],
    "education": "BSc Computer Science, State University", "links": {"github": "https://github.com/sam-rivera"},
}
JOB = {"title": "Senior Python Backend Engineer", "company": "Acme",
       "description": "We need a Python and FastAPI engineer with Postgres and Docker.\n\nRequirements\n- 5+ years of Python\n- Kubernetes experience"}


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
        self.tmp = Path(tempfile.mkdtemp(prefix="net-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.profile = copy.deepcopy(PROFILE)
        self.model = None
        self.now = NOW
        self.ctx = Ctx(config=self.config, model_for=lambda p: self.model, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        self.q = None

    def queue(self):
        if self.q is None:
            class Mail:
                def send_mail(*a, **k):
                    raise AssertionError("networking must never send mail")
            self.q = applyq.Applyq(self.ctx, clock=lambda: self.now, profile_fn=lambda c: self.profile, gmailx=Mail())
        return self.q

    def client(self, authed=True):
        app = FastAPI()
        app.include_router(N.make_router(self.ctx, clock=lambda: self.now, profile_fn=lambda c: self.profile, model_fn=lambda p: self.model,
                                         applyq_fn=lambda: self.queue()))
        self.app = app
        c = TestClient(app, cookies={"hub_session": "yes"} if authed else {})
        self.addCleanup(c.close)
        return c

    def fb(self):
        return F.FactBase.build(self.profile, F.load(self.ctx), this_year=2026)

    def make(self, c, kind="referral", **extra):
        r = c.post("/api/networking/drafts", json={"kind": kind, "company": "Acme", "role": "Senior Python Backend Engineer",
                                                   "description": JOB["description"], **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class DraftTests(Base):
    def test_every_kind_is_short_backed_and_has_a_subject(self):
        c = self.client()
        for kind in N.KINDS:
            d = self.make(c, kind, contact_name="Alex Chen", how_known="the Python meetup", topic="the data platform", sent_on="2026-11-10")
            self.assertLessEqual(d["words"], N.MAX_WORDS, kind)
            self.assertTrue(d["subject"].strip(), kind)
            self.assertFalse(d["blocked"], (kind, d["blockers"]))
            self.assertIn("Acme", d["text"])
            self.assertTrue(d["text"].startswith("Hi Alex,"), kind)
            self.assertTrue(d["text"].rstrip().endswith("Sam Rivera"))
            self.assertNotIn("Kubernetes", d["text"])               # a skill that is in the posting but not in the facts
            self.assertEqual(d["sent_on"], "")
            self.assertIn("Nothing is sent", d["note"])

    def test_referral_uses_a_real_fact_and_the_relationship_the_owner_typed(self):
        d = self.make(self.client(), "referral", contact_name="Alex Chen", how_known="the Python meetup")
        self.assertIn("2M requests per day", d["text"])
        self.assertIn("We know each other through the Python meetup.", d["text"])

    def test_follow_up_names_the_date_in_plain_words(self):
        d = self.make(self.client(), "follow_up", sent_on="2026-11-10")
        self.assertIn("10 November 2026", d["text"])
        self.assertFalse(d["blocked"])

    def test_word_cap_drops_optional_sentences_not_facts(self):
        self.profile["skills"] = ["Python", "FastAPI", "Postgres", "Docker"]
        self.profile["experience"][0]["bullets"] = ["Built a Python and FastAPI service handling 2M requests per day " + "with many careful steps " * 12]
        c = self.client()
        for kind in N.KINDS:
            d = self.make(c, kind, interviewer="Alex")
            self.assertLessEqual(d["words"], N.MAX_WORDS, kind)
            self.assertFalse(d["blocked"], kind)

    def test_no_facts_is_refused(self):
        self.profile = {}
        r = self.client().post("/api/networking/drafts", json={"kind": "referral", "company": "Acme"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no facts", r.json()["detail"].lower())

    def test_bad_input(self):
        c = self.client()
        self.assertEqual(c.post("/api/networking/drafts", json={"kind": "spam", "company": "Acme"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/drafts", json={"kind": "referral"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/drafts", json={"kind": "follow_up", "company": "Acme", "sent_on": "tomorrow"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/drafts", json={"kind": "follow_up", "company": "Acme", "sent_on": "2027-01-01"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/drafts", json={"kind": "referral", "application_id": "nope"}).status_code, 404)

    def test_from_an_application(self):
        item, _ = self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/p", "company": "Globex", "description": "Python and Docker work."})
        d = self.make(self.client(), "recruiter", application_id=item["id"], company="", role="")
        self.assertIn("Globex", d["text"])
        self.assertEqual(d["application_id"], item["id"])

    def test_sentences_name_fact_ids_that_exist(self):
        c = self.client()
        d = self.make(c, "referral")
        ids = {f["id"] for f in self.fb().facts}
        data = json.loads((Path(self.config.data_path()) / "networking.json").read_text(encoding="utf-8"))
        sents = data["drafts"][0]["cover"]["blocks"][0]["sentences"]
        claims = [s for s in sents if s["kind"] == "claim"]
        self.assertTrue(claims)
        for s in claims:
            self.assertTrue(s["fact_ids"])
            self.assertTrue(set(s["fact_ids"]) <= ids)
        self.assertEqual(d["words"], len(d["text"].split()))


class HonestyTests(Base):
    def test_a_claim_the_facts_do_not_back_blocks_the_draft(self):
        c = self.client()
        d = self.make(c, "referral")
        sid = "s1"
        r = c.post(f"/api/networking/drafts/{d['id']}/resolve", json={"action": "edit", "sentence": sid, "text": "I led a team of 50 engineers at Globex."})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertTrue(out["blocked"])
        self.assertTrue(any("Globex" in b or "50" in b for b in out["blockers"]))

    def test_removing_the_claim_unblocks_and_confirming_it_backs_it(self):
        c = self.client()
        d = self.make(c, "referral")
        c.post(f"/api/networking/drafts/{d['id']}/resolve", json={"action": "edit", "sentence": "s1", "text": "I led a team of 50 engineers at Globex."})
        out = c.post(f"/api/networking/drafts/{d['id']}/resolve", json={"action": "remove", "sentence": "s1"}).json()
        self.assertFalse(out["blocked"])
        d2 = self.make(c, "referral")
        c.post(f"/api/networking/drafts/{d2['id']}/resolve", json={"action": "edit", "sentence": "s1", "text": "I led a team of 50 engineers at Globex."})
        out = c.post(f"/api/networking/drafts/{d2['id']}/resolve", json={"action": "confirm", "sentence": "s1", "fact_text": "I led a team of 50 engineers at Globex"}).json()
        self.assertFalse(out["blocked"])
        self.assertTrue(any("Globex" in f["text"] for f in self.fb().of_type("confirmed")))

    def test_missing_name_leaves_a_visible_blank_that_blocks_copy(self):
        self.profile["name"] = ""
        d = self.make(self.client(), "recruiter")
        self.assertTrue(d["blocked"])
        self.assertIn("[FILL: your name]", d["text"])
        c = self.client()
        out = c.post(f"/api/networking/drafts/{d['id']}/resolve", json={"action": "fill", "label": "your name", "value": "Sam"}).json()
        self.assertFalse(out["blocked"])
        self.assertTrue(out["text"].rstrip().endswith("Sam"))

    def test_resolve_errors(self):
        c = self.client()
        d = self.make(c, "referral")
        base = f"/api/networking/drafts/{d['id']}/resolve"
        self.assertEqual(c.post(base, json={"action": "edit", "sentence": "zz", "text": "x"}).status_code, 400)
        self.assertEqual(c.post(base, json={"action": "edit", "sentence": "s1", "text": ""}).status_code, 400)
        self.assertEqual(c.post(base, json={"action": "dance", "sentence": "s1"}).status_code, 400)
        self.assertEqual(c.post(base, json={"action": "fill", "label": "x", "value": "y"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/drafts/nd-none/resolve", json={"action": "remove", "sentence": "s1"}).status_code, 404)


class AiTests(Base):
    def facts_ids(self):
        fb = self.fb()
        bullet = next(f for f in fb.of_type("bullet") if "2M" in f["text"])
        return bullet, fb.of_type("role")[0]

    def test_valid_ai_wording_is_used(self):
        bullet, role = self.facts_ids()
        self.model = FakeModel({"sentences": [
            {"text": "At Initech, I built a Python and FastAPI service handling 2M requests per day.", "fact_ids": [bullet["id"], role["id"]], "kind": "claim"},
            {"text": "Could you point me to the right person?", "fact_ids": [], "kind": "glue"}]})
        d = self.make(self.client(), "referral", ai=True)
        self.assertEqual(d["cover"]["generator"], "ai")
        self.assertFalse(d["blocked"])
        self.assertIn("Could you point me", d["text"])
        sent = self.model.calls[0]["user"]
        self.assertIn("Senior Python Backend Engineer", sent)
        self.assertNotIn("Alex", sent)                               # no contact names go to the model
        self.assertNotIn("PRIVATE", sent)

    def test_invented_ai_claim_is_dropped_for_the_plain_draft(self):
        self.model = FakeModel({"sentences": [{"text": "I led a Kubernetes migration for 50 teams at Globex.", "fact_ids": [], "kind": "claim"}]})
        d = self.make(self.client(), "referral", ai=True)
        self.assertEqual(d["cover"]["generator"], "template")
        self.assertIn("do not back", d["cover"]["ai_note"])
        self.assertNotIn("Globex", d["text"])
        self.assertFalse(d["blocked"])

    def test_ai_over_the_word_cap_is_dropped(self):
        bullet, role = self.facts_ids()
        one = {"text": "At Initech, I built a Python and FastAPI service handling 2M requests per day and kept it running.", "fact_ids": [bullet["id"], role["id"]], "kind": "claim"}
        self.model = FakeModel({"sentences": [one] * 9})
        d = self.make(self.client(), "referral", ai=True)
        self.assertEqual(d["cover"]["generator"], "template")
        self.assertLessEqual(d["words"], N.MAX_WORDS)

    def test_no_ai_or_broken_ai_falls_back(self):
        c = self.client()
        d = self.make(c, "referral", ai=True)                         # model is None
        self.assertEqual(d["cover"]["generator"], "template")
        self.assertIn("No AI model", d["cover"]["ai_note"])
        self.model = FakeModel()                                      # raises "model down"
        d = self.make(c, "referral", ai=True)
        self.assertEqual(d["cover"]["generator"], "template")
        self.assertIn("failed", d["cover"]["ai_note"])
        self.model = FakeModel("not json at all", "still not json")
        d = self.make(c, "referral", ai=True)
        self.assertEqual(d["cover"]["generator"], "template")

    def test_ai_is_off_by_default(self):
        self.model = FakeModel()
        self.make(self.client(), "referral")
        self.assertEqual(self.model.calls, [])


class ContactTests(Base):
    def test_crud(self):
        c = self.client()
        r = c.post("/api/networking/contacts", json={"name": "Alex Chen", "company": "Acme", "how_known": "the Python meetup", "last_contact": "2026-11-01"})
        self.assertEqual(r.status_code, 200, r.text)
        cid = r.json()["id"]
        self.assertEqual(c.get("/api/networking").json()["contacts"][0]["name"], "Alex Chen")
        r = c.put(f"/api/networking/contacts/{cid}", json={"role": "Engineering manager", "note": "Met twice"})
        self.assertEqual((r.json()["role"], r.json()["name"], r.json()["last_contact"]), ("Engineering manager", "Alex Chen", "2026-11-01"))
        self.assertEqual(c.put("/api/networking/contacts/nc-none", json={"name": "x"}).status_code, 404)
        self.assertEqual(c.delete(f"/api/networking/contacts/{cid}").status_code, 200)
        self.assertEqual(c.delete(f"/api/networking/contacts/{cid}").status_code, 404)
        self.assertEqual(c.get("/api/networking").json()["contacts"], [])

    def test_validation(self):
        c = self.client()
        self.assertEqual(c.post("/api/networking/contacts", json={"company": "Acme"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/contacts", json={"name": "A", "last_contact": "11/01/2026"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/contacts", json={"name": "A", "last_contact": "2026-02-31"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/contacts", json={"name": "A", "last_contact": "2027-01-01"}).status_code, 400)
        r = c.post("/api/networking/contacts", json={"name": "  A\x00 B  ", "note": "x" * 900})
        self.assertEqual(r.json()["name"], "A B")
        self.assertLessEqual(len(r.json()["note"]), 300)

    def test_contact_fills_the_draft(self):
        c = self.client()
        cid = c.post("/api/networking/contacts", json={"name": "Alex Chen", "how_known": "the Python meetup", "last_contact": "2026-11-01"}).json()["id"]
        d = self.make(c, "follow_up", contact_id=cid)
        self.assertTrue(d["text"].startswith("Hi Alex,"))
        self.assertIn("1 November 2026", d["text"])
        self.assertEqual(d["contact_id"], cid)


class NudgeTests(Base):
    def contact(self, c, name, last):
        return c.post("/api/networking/contacts", json={"name": name, "last_contact": last}).json()["id"]

    def test_due_after_n_days_default_14(self):
        c = self.client()
        self.contact(c, "Fresh", "2026-11-07")       # 13 days
        self.contact(c, "Due", "2026-11-06")         # 14 days
        self.contact(c, "Old", "2026-10-01")
        self.contact(c, "Never", "")
        nudges = c.get("/api/networking/nudges").json()
        self.assertEqual(nudges["days"], 14)
        self.assertEqual([n["name"] for n in nudges["nudges"]], ["Old", "Due"])
        self.assertEqual(nudges["nudges"][1]["days"], 14)
        self.assertEqual(nudges["nudges"][0]["suggest"], "follow_up")
        self.assertEqual(c.get("/api/networking").json()["nudges"], nudges["nudges"])

    def test_n_comes_from_config_and_bad_values_fall_back(self):
        c = self.client()
        self.contact(c, "Fresh", "2026-11-07")
        self.config.networking_nudge_days = 7
        self.assertEqual([n["name"] for n in c.get("/api/networking/nudges").json()["nudges"]], ["Fresh"])
        for bad in (0, -3, 1000, True, "x"):
            self.config.networking_nudge_days = bad
            self.assertEqual(N.nudge_days(self.config), 14)

    def test_time_moves_with_the_clock(self):
        c = self.client()
        self.contact(c, "Soon", "2026-11-15")
        self.assertEqual(c.get("/api/networking/nudges").json()["nudges"], [])
        self.now = NOW + dt.timedelta(days=10)
        self.assertEqual(len(c.get("/api/networking/nudges").json()["nudges"]), 1)

    def test_marking_a_draft_sent_resets_the_nudge(self):
        c = self.client()
        cid = self.contact(c, "Due", "2026-10-01")
        self.assertEqual(len(c.get("/api/networking/nudges").json()["nudges"]), 1)
        d = self.make(c, "follow_up", contact_id=cid)
        r = c.post(f"/api/networking/drafts/{d['id']}/sent", json={})
        self.assertEqual(r.json()["sent_on"], "2026-11-20")
        self.assertEqual(c.get("/api/networking/nudges").json()["nudges"], [])
        self.assertEqual(c.get("/api/networking").json()["contacts"][0]["last_contact"], "2026-11-20")

    def test_application_with_no_reply_is_suggested_once(self):
        c = self.client()
        item, _ = self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/n", "company": "Globex", "description": "Python."})
        item.update(state="sent", sent_at="2026-11-01T10:00:00")
        self.queue().store.save(item)
        n = c.get("/api/networking/nudges").json()["nudges"]
        self.assertEqual([(x["kind"], x["company"], x["days"]) for x in n], [("application", "Globex", 19)])
        d = self.make(c, "follow_up", application_id=item["id"])
        c.post(f"/api/networking/drafts/{d['id']}/sent", json={"date": "2026-11-19"})
        self.assertEqual(c.get("/api/networking/nudges").json()["nudges"], [])
        item.update(state="replied")
        self.queue().store.save(item)
        self.assertEqual(c.get("/api/networking/nudges").json()["nudges"], [])


class SentTests(Base):
    def test_mark_sent_only_records_a_date(self):
        c = self.client()
        d = self.make(c, "referral")
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("nothing may open a connection")):
            r = c.post(f"/api/networking/drafts/{d['id']}/sent", json={"date": "2026-11-18"})
        self.assertEqual(r.json()["sent_on"], "2026-11-18")
        self.assertEqual(c.post(f"/api/networking/drafts/{d['id']}/sent", json={"undo": True}).json()["sent_on"], "")
        self.assertEqual(c.post(f"/api/networking/drafts/{d['id']}/sent", json={"date": "2027-05-05"}).status_code, 400)
        self.assertEqual(c.post("/api/networking/drafts/nd-none/sent", json={}).status_code, 404)

    def test_there_is_no_route_that_sends(self):
        self.client()
        paths = [p for p, _m, _e in flat_routes(self.app)]
        self.assertTrue(len(paths) > 8)
        for path in paths:
            self.assertNotRegex(path, r"/(send|post|publish|submit)\b")

    def test_nothing_is_sent_while_drafting_and_resolving(self):
        c = self.client()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("nothing may open a connection")):
            for kind in N.KINDS:
                d = self.make(c, kind)
                c.post(f"/api/networking/drafts/{d['id']}/resolve", json={"action": "remove", "sentence": "s1"})
            c.get("/api/networking")
        self.assertEqual(self.queue().list("sent"), [])               # the mailer in the queue raises if used

    def test_delete_and_list(self):
        c = self.client()
        d = self.make(c, "thank_you", interviewer="Alex")
        self.assertEqual(c.get("/api/networking/drafts").json()["drafts"][0]["id"], d["id"])
        self.assertEqual(c.get(f"/api/networking/drafts/{d['id']}").json()["kind"], "thank_you")
        self.assertEqual(c.delete(f"/api/networking/drafts/{d['id']}").status_code, 200)
        self.assertEqual(c.get(f"/api/networking/drafts/{d['id']}").status_code, 404)


class RouteTests(Base):
    def test_every_route_needs_login(self):
        c = self.client(authed=False)
        for method, path in (("get", "/api/networking"), ("get", "/api/networking/nudges"), ("get", "/api/networking/drafts"),
                             ("post", "/api/networking/drafts"), ("post", "/api/networking/contacts"), ("put", "/api/networking/contacts/x"),
                             ("delete", "/api/networking/contacts/x"), ("get", "/api/networking/drafts/x"), ("delete", "/api/networking/drafts/x"),
                             ("post", "/api/networking/drafts/x/resolve"), ("post", "/api/networking/drafts/x/sent")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, (method, path))

    def test_switched_off(self):
        self.config.networking_enabled = False
        c = self.client()
        self.assertEqual(c.get("/api/networking").status_code, 404)
        self.assertEqual(c.post("/api/networking/drafts", json={"kind": "referral", "company": "A"}).status_code, 404)

    def test_overview_is_one_call_with_everything_the_screen_needs(self):
        c = self.client()
        self.queue().add({"title": "Python dev", "url": "https://jobs.example.test/o", "company": "Globex", "description": "Python."})
        o = c.get("/api/networking").json()
        self.assertEqual(set(o), {"contacts", "nudges", "drafts", "applications", "kinds", "settings", "max_words", "sends"})
        self.assertEqual([k["id"] for k in o["kinds"]], list(N.KINDS))
        self.assertEqual(o["applications"][0]["company"], "Globex")
        self.assertIs(o["sends"], False)
        self.assertEqual(o["settings"]["nudge_days"], 14)

    def test_the_data_stays_in_the_data_folder(self):
        c = self.client()
        self.make(c, "referral")
        self.assertTrue((Path(self.config.data_path()) / "networking.json").exists())


class WiringTests(Base):
    def test_wiring_settings_and_tool_group(self):
        from hub import server, settings
        self.assertIn("networking", server.FEATURE_MODULES)
        for key in ("networking_enabled", "networking_nudge_days", "networking_ai"):
            self.assertIn(key, settings.ALLOWED_KEYS)
        names, rx = TOOL_GROUPS["networking"]
        self.assertEqual(names, {"network_draft", "interview_prep"})
        self.assertTrue(rx.search("write a referral request for Acme"))
        self.assertTrue(rx.search("a follow-up email to the recruiter"))
        self.assertEqual(self.config.networking_nudge_days, 14)

    def test_tool_is_draft_only_and_untrusted(self):
        tools = {t.spec["name"]: t for t in N.build(lambda: self.app, self.config)}
        self.assertEqual(set(tools), {"network_draft"})
        t = tools["network_draft"]
        self.assertFalse(t.needs_approval({}))
        self.assertTrue(t.untrusted({}))
        self.assertIn("kind", t.spec["input_schema"]["required"])

    def test_tool_round_trip(self):
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(N.make_router(open_ctx, clock=lambda: self.now, profile_fn=lambda c: self.profile))
        self.config.session_generation = 0
        tools = {t.spec["name"]: t for t in N.build(lambda: app, self.config)}
        ok, text = asyncio.run(tools["network_draft"].run({"kind": "referral", "company": "Acme", "role": "Python Engineer"}))
        self.assertTrue(ok, text)
        self.assertIn("Nothing was sent", text)
        self.assertIn("Subject:", text)
        ok, text = asyncio.run(tools["network_draft"].run({"kind": "bogus"}))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
