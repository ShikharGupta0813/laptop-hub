"""Freelance proposals (hub/proposals.py). Fakes only: a fake clock and an invented profile. Rules under test:
- a draft is built from confirmed facts only: proof cites fact ids, a skill the owner never confirmed is never claimed;
- three opening lines are offered; a rate or timeline the owner did not type is a [FILL] that blocks Copy;
- the owner records which opening they used and whether it won; the stats say so plainly and refuse to compare tiny samples;
- copy only: there is no send, submit or apply route and nothing leaves the laptop."""
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

from hub import proposals as P
from hub.common import Ctx
from tests.apiwalk import flat_routes
from tests.helpers import make_config
from tests.test_autopilot import need_auth
from tests.test_interviews import PROFILE

NOW = dt.datetime(2026, 11, 20, 9, 0)
TASK = {"title": "Build a small reporting API", "company": "Northwind", "url": "https://upwork.example/jobs/1",
        "description": "We need a Python and FastAPI developer to build a small reporting API on Postgres. Budget to be discussed. Rust knowledge is a plus."}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.profile = copy.deepcopy(PROFILE)
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)

    def client(self, authed=True):
        app = FastAPI()
        app.include_router(P.make_router(self.ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile))
        self.app = app
        c = TestClient(app, cookies={"hub_session": "yes"} if authed else {})
        self.addCleanup(c.close)
        return c

    def draft(self, c, **kw):
        r = c.post("/api/proposals/draft", json={**TASK, **kw})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class DraftTests(Base):
    def test_draft_has_the_parts_and_three_openings(self):
        d = self.draft(self.client(), rate=40, currency="usd", unit="per hour", timeline="two weeks")
        text = d["text"]
        self.assertTrue(text.startswith("Hi,"))
        for part in ("Here is how I would do it", "Step one", "Timeline: two weeks.", "Rate: USD 40 per hour.", "One question:", "Thank you for reading."):
            self.assertIn(part, text)
        self.assertEqual(len(d["openings"]), 3)
        self.assertEqual({o["key"] for o in d["openings"]} <= set(P.OPENINGS), True)
        self.assertEqual(d["opening"], d["openings"][0]["key"])
        self.assertEqual(len({o["line"] for o in d["openings"]}), 3)
        for o in d["openings"]:
            self.assertTrue(o["text"].startswith("Hi,") and o["line"] in o["text"])
        self.assertFalse(d["blocked"], d["blockers"])
        self.assertTrue(d["note"].startswith("Draft only"))

    def test_proof_comes_from_confirmed_facts_with_ids(self):
        d = self.draft(self.client(), rate=40, currency="USD", unit="per hour", timeline="two weeks")
        proof = [s for s in d["sentences"] if s["kind"] == "claim"]
        self.assertTrue(proof)
        for s in proof:
            self.assertTrue(s["fact_ids"], s["text"])
            self.assertEqual(s["status"], "ok")
        joined = " ".join(s["text"] for s in proof)
        self.assertTrue("FastAPI" in joined or "Postgres" in joined or "Python" in joined)
        self.assertTrue(d["fact_ids"])

    def test_a_skill_the_owner_never_confirmed_is_not_claimed(self):
        d = self.draft(self.client(), rate=40, currency="USD", unit="per hour", timeline="two weeks")
        for o in d["openings"]:
            if o["key"] == "proof":
                self.assertNotIn("Rust", o["line"])
        for s in d["sentences"]:
            if s["kind"] == "claim":
                self.assertNotIn("Rust", s["text"])

    def test_a_claim_the_facts_do_not_back_blocks_the_draft(self):
        from hub import tailor as tl
        real = tl.first_person
        with mock.patch.object(tl, "first_person", lambda fb, b: ("I am a Rust compiler expert with ten years of kernel work.", real(fb, b)[1])):
            d = self.draft(self.client(), rate=40, currency="USD", unit="hour", timeline="two weeks")
        self.assertTrue(d["blocked"])
        self.assertTrue(any(b.startswith("Not backed by your facts") for b in d["blockers"]))
        by_key = {o["key"]: o for o in d["openings"]}
        self.assertTrue(by_key["proof"]["blocked"])
        self.assertTrue(all(o["blocked"] for o in d["openings"]))   # the proof lines sit in every version, so every version is blocked

    def test_missing_rate_and_timeline_are_blanks_that_block_copy(self):
        d = self.draft(self.client())
        self.assertTrue(d["blocked"])
        self.assertIn("[FILL: your rate and how it is charged]", d["text"])
        self.assertIn("[FILL: how long it will take you]", d["text"])
        self.assertTrue(any(b.startswith("Fill in:") for b in d["blockers"]))

    def test_the_question_fills_a_gap_in_the_posting(self):
        qs = P.client_questions({"title": "x", "description": "Python work."})
        self.assertIn("What budget range do you have in mind?", qs)
        self.assertIn("When do you need this finished by?", qs)
        qs2 = P.client_questions({"title": "x", "description": "Budget: $500. Deadline: Friday."})
        self.assertEqual(qs2, ["What would a finished result look like for you?"])

    def test_no_facts_still_gives_a_draft_without_a_proof_opening(self):
        self.profile = {}
        d = self.draft(self.client(), rate=40, currency="USD", unit="per hour", timeline="two weeks")
        self.assertTrue(d["no_facts"])
        self.assertNotIn("proof", {o["key"] for o in d["openings"]})
        self.assertFalse([s for s in d["sentences"] if s["kind"] == "claim"])

    def test_same_posting_same_draft(self):
        c = self.client()
        a = self.draft(c, rate=40, currency="USD", unit="hour", timeline="two weeks")
        b = self.draft(c, rate=40, currency="USD", unit="hour", timeline="two weeks")
        self.assertEqual(a["text"], b["text"])
        self.assertNotEqual(a["id"], b["id"])

    def test_bad_input_is_plain(self):
        c = self.client()
        self.assertEqual(c.post("/api/proposals/draft", json={}).status_code, 400)
        self.assertEqual(c.post("/api/proposals/draft", json={**TASK, "rate": "lots"}).status_code, 400)
        self.assertEqual(c.post("/api/proposals/draft", json={**TASK, "rate": -3}).status_code, 400)
        self.assertEqual(c.post("/api/proposals/draft", json={"item_id": "0" * 16}).status_code, 404)

    def test_a_task_from_the_hub_by_item_id(self):
        base = Path(self.config.data_path())
        base.mkdir(parents=True, exist_ok=True)
        item = {"id": "a" * 16, "title": "Fix a Python script", "company": "Client A", "url": "https://upwork.example/x", "kind": "freelance", "status": "new",
                "description": "Fix a Python script that fails on large files. Python and Postgres.", "pay_label": "$200", "source": "Board"}
        (base / "oppsources_items.json").write_text(json.dumps({item["id"]: item, "b" * 16: {**item, "id": "b" * 16, "kind": "job", "employment": "full-time"}}), encoding="utf-8")
        c = self.client()
        tasks = c.get("/api/proposals").json()["tasks"]
        self.assertEqual([t["id"] for t in tasks], ["a" * 16])
        d = c.post("/api/proposals/draft", json={"item_id": "a" * 16, "rate": 30, "currency": "USD", "unit": "hour", "timeline": "three days"}).json()
        self.assertIn("Fix a Python script", d["text"])
        self.assertEqual(c.get("/api/proposals").json()["proposals"][0]["platform"], "upwork.example")


class AbTests(Base):
    def make(self, c, n, key, outcomes):
        """n proposals that used opening `key`, with the given outcomes (None = still waiting)."""
        for i in range(n):
            d = self.draft(c, rate=40, currency="USD", unit="hour", timeline="two weeks")
            keys = [o["key"] for o in d["openings"]]
            use = key if key in keys else keys[0]
            self.assertEqual(c.post(f"/api/proposals/{d['id']}/used", json={"opening": use}).status_code, 200)
            if outcomes[i] is not None:
                self.assertEqual(c.post(f"/api/proposals/{d['id']}/outcome", json={"outcome": outcomes[i]}).status_code, 200)

    def test_used_and_outcome_are_recorded_by_the_owner(self):
        c = self.client()
        d = self.draft(c, rate=40, currency="USD", unit="hour", timeline="two weeks")
        self.assertEqual(c.post(f"/api/proposals/{d['id']}/outcome", json={"outcome": "won"}).status_code, 400)       # not marked used yet
        self.assertEqual(c.post(f"/api/proposals/{d['id']}/used", json={"opening": "nonsense"}).status_code, 400)
        other = next(o["key"] for o in d["openings"] if o["key"] != d["opening"])
        r = c.post(f"/api/proposals/{d['id']}/used", json={"opening": other, "note": "pasted on the platform"})
        self.assertEqual((r.json()["used"], r.json()["opening"], r.json()["used_at"]), (True, other, "2026-11-20"))
        self.assertEqual(c.post(f"/api/proposals/{d['id']}/outcome", json={"outcome": "maybe"}).status_code, 400)
        r = c.post(f"/api/proposals/{d['id']}/outcome", json={"outcome": "won"})
        self.assertEqual(r.json()["outcome"], "won")
        self.assertEqual(c.post("/api/proposals/pr-nope/used", json={}).status_code, 404)

    def test_too_few_bids_to_tell(self):
        c = self.client()
        self.make(c, 3, "problem", ["won", "lost", "lost"])
        s = c.get("/api/proposals/stats").json()
        self.assertIn("Too few", s["verdict"])
        row = next(o for o in s["openings"] if o["key"] == "problem")
        self.assertEqual((row["used"], row["decided"], row["won"], row["lost"]), (3, 3, 1, 2))
        self.assertFalse(row["enough"])

    def test_which_opening_wins_more_with_enough_bids(self):
        rows = []
        for key, outcomes in (("proof", ["won"] * 4 + ["lost"]), ("problem", ["won"] + ["lost"] * 4), ("question", ["no_reply"] * 2)):
            for i, o in enumerate(outcomes):
                rows.append({"opening": key, "used": True, "outcome": o})
        s = P.win_stats(rows)
        by = {o["key"]: o for o in s["openings"]}
        self.assertEqual((by["proof"]["win_rate"], by["problem"]["win_rate"]), (80, 20))
        self.assertTrue(by["proof"]["enough"] and by["problem"]["enough"] and not by["question"]["enough"])
        self.assertIn("Proof first", s["verdict"])
        self.assertIn("80 percent of 5", s["verdict"])
        self.assertEqual(by["question"]["no_reply"], 2)
        self.assertIsNone(by["direct"]["win_rate"])

    def test_a_tie_is_not_called_a_winner(self):
        rows = [{"opening": k, "used": True, "outcome": o} for k in ("proof", "problem") for o in ["won"] * 3 + ["lost"] * 2]
        self.assertIn("No opening is clearly ahead", P.win_stats(rows)["verdict"])

    def test_unused_drafts_do_not_count(self):
        c = self.client()
        for _ in range(3):
            self.draft(c, rate=40, currency="USD", unit="hour", timeline="two weeks")
        s = c.get("/api/proposals/stats").json()
        self.assertTrue(all(o["used"] == 0 for o in s["openings"]))

    def test_delete_removes_it_from_the_stats(self):
        c = self.client()
        self.make(c, 1, "proof", ["won"])
        pid = c.get("/api/proposals").json()["proposals"][0]["id"]
        self.assertEqual(c.delete(f"/api/proposals/{pid}").status_code, 200)
        self.assertEqual(c.get("/api/proposals/stats").json()["decided_total"], 0)
        self.assertEqual(c.delete(f"/api/proposals/{pid}").status_code, 404)


class SafetyTests(Base):
    def test_nothing_is_sent_and_there_is_no_send_route(self):
        c = self.client(False)
        self.assertEqual(c.get("/api/proposals").status_code, 401)
        c = self.client()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("nothing may open a connection")):
            d = self.draft(c, rate=40, currency="USD", unit="hour", timeline="two weeks")
            c.post(f"/api/proposals/{d['id']}/used", json={})
            c.get("/api/proposals/stats")
        for p, _m, _e in flat_routes(self.app):
            self.assertNotRegex(p, r"/(send|post|publish|submit|apply)\b")

    def test_switched_off(self):
        c = self.client()
        self.config.proposals_enabled = False
        self.assertEqual(c.get("/api/proposals").status_code, 404)

    def test_wiring_settings_and_tool(self):
        from hub import server, settings
        from hub.assistant import TOOL_GROUPS
        self.assertIn("proposals", server.FEATURE_MODULES)
        self.assertIn("proposals_enabled", settings.ALLOWED_KEYS)
        self.assertIn("proposal_stats", TOOL_GROUPS["proposals"][0])
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(P.make_router(open_ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile))
        self.config.session_generation = 0
        tool = P.build(lambda: app, self.config)[0]
        self.assertFalse(tool.needs_approval({}))
        ok, text = asyncio.run(tool.run({}))
        self.assertTrue(ok, text)
        self.assertIn("Too few bids", text)
        self.assertIn("by hand", text)


if __name__ == "__main__":
    unittest.main()
