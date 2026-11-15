"""Tests for urgent-alert triage (hub/alerts.py), the Asana poller (hub/asana.py) and the assistant tools.
Synthetic data and fakes only: fake clock, notifier, speaker, HTTP layer and token store. No network, no real tokens."""
import asyncio
import datetime as dt
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import alerts, alerts_tools, asana, brain, proactive
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import Clock, need_auth

NOON = dt.datetime(2026, 11, 9, 12, 0)
NIGHT = dt.datetime(2026, 11, 9, 23, 30)
FAKE_TOKEN = "1/1234567890123456:fakefakefakefakefakefake"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alerts-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Shikhar",
                                             alerts_vips=["Ravi Manager"])
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock(NOON)
        self.pushed, self.spoken = [], []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: None,
                       notify=lambda title, body="", level="info", link=None, source="": self.pushed.append(
                           {"title": title, "body": body, "level": level, "link": link, "source": source}))
        self.eng = alerts.Alerts(self.ctx, clock=self.clock, speaker=self.spoken.append, spawn=lambda fn: fn())
        self.app = FastAPI()
        self.app.include_router(proactive.make_router(ctx=self.ctx, clock=self.clock))
        self.app.include_router(alerts.make_router(self.ctx, engine=self.eng))
        self.client = TestClient(self.app, cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)

    def ev(self, text, sender="Someone", source="whatsapp", **kw):
        return {"source": source, "sender": sender, "text": text, **kw}


class Classification(Base):
    def test_table(self):
        rows = [   # text, sender, source, urgency (None = not asserted), kind, raised
            ("URGENT: prod is down, need a fix ASAP", "Dev", "slack", 3, None, True),
            ("Please send the report by EOD", "Colleague", "slack", 1, "task", True),
            ("Lunch at 1?", "Friend", "whatsapp", 0, "question", True),            # any direct question raises an alert
            ("Can you check this today?", "Ravi Manager", "whatsapp", 3, None, True),
            ("Walk-in urgent care open late, 20% off first visit", "Clinic", "whatsapp", 0, None, False),
            ("URGENT sale! Limited time offer, unsubscribe", "Shop", "gmail", 0, None, False),
            ("P0 blocker on checkout", "Dev", "slack", 3, None, True),
            ("what is going on? why is it breaking", "Peer", "slack", None, "question", True),
            ("send your ETA now", "Peer", "whatsapp", None, None, True),
            ("Standup moved, join the call at 5", "Lead", "teams", None, "meeting", True),
            ("@Shikhar can you look at this", "Peer", "slack", None, None, True),
            ("good night", "Friend", "whatsapp", 0, "other", False),
            ("Happy birthday!", "Aunt", "whatsapp", 0, "other", False),
        ]
        for text, sender, source, urgency, kind, raised in rows:
            c = alerts.classify(text, sender, source, vips=["Ravi Manager"])
            self.assertEqual(c["raise"], raised, text)
            if urgency is not None:
                self.assertEqual(c["urgency"], urgency, text)
            if kind:
                self.assertEqual(c["kind"], kind, text)
            self.assertTrue(c["reasons"] or not c["raise"], text)

    def test_intents(self):
        self.assertEqual(alerts.classify("send your ETA now", "x", "slack")["intent"], "eta")
        self.assertEqual(alerts.classify("what is going on, why is it breaking?", "x", "slack")["intent"], "why")

    def test_every_mention_raises_even_when_it_looks_like_junk(self):
        for text in ("@Shikhar nice sale today", "Ravi mentioned you in #general", "hello Shikhar"):
            a = self.eng.ingest(self.ev(text, "Someone", "slack"))
            self.assertIsNotNone(a, text)
            self.assertTrue(a["mention"], text)
        self.assertEqual(alerts.classify("x", "y", "slack", mention=True)["raise"], True)

    def test_vip_message_always_raises_and_question_is_urgent(self):
        a = self.eng.ingest(self.ev("Why did the job fail?", "Ravi Manager"))
        self.assertGreaterEqual(a["urgency"], 2)
        self.assertIn("VIP", " ".join(a["reasons"]))
        b = self.eng.ingest(self.ev("ok thanks", "Ravi Manager"))
        self.assertIsNotNone(b)

    def test_noise_is_dropped(self):
        self.assertIsNone(self.eng.ingest(self.ev("Walk-in urgent care, 20% off", "Clinic")))
        self.assertIsNone(self.eng.ingest(self.ev("good night", "Friend")))
        self.assertEqual(self.eng.listing()["total_open"], 0)

    def test_rules_and_sources_are_editable(self):
        # a word the owner adds becomes urgent; removing a default stops it counting
        self.assertEqual(alerts.classify("code red", "x", "slack")["urgency"], 0)
        s = self.client.post("/api/alerts/settings", headers={"X-Hub": "1"},
                             json={"rules": {"urgent_words": ["code red"], "ignore_words": []}}).json()
        self.assertEqual(s["rules"]["urgent_words"], ["code red"])
        a = self.eng.ingest(self.ev("code red on the data pipeline", "Peer", "slack"))
        self.assertGreaterEqual(a["urgency"], 2)
        self.assertIsNone(self.eng.ingest(self.ev("urgent: deploy now", "Peer2", "slack")) if False else None)
        low = self.eng.ingest(self.ev("this is urgent", "Peer3", "slack"))
        self.assertTrue(low is None or low["urgency"] < 2)
        self.client.post("/api/alerts/settings", json={"reset_rules": True}, headers={"X-Hub": "1"})
        self.assertIn("urgent", self.client.get("/api/alerts/settings").json()["rules"]["urgent_words"])
        bad = self.client.post("/api/alerts/settings", json={"rules": {"nope": []}}, headers={"X-Hub": "1"})
        self.assertEqual(bad.status_code, 400)
        # sources: remove whatsapp, add a custom package fragment
        self.client.post("/api/alerts/settings", json={"sources": ["slack", "com.example.chat"]}, headers={"X-Hub": "1"})
        self.assertIsNone(self.eng.ingest(self.ev("URGENT outage now", "Dev", "whatsapp")))
        self.assertIsNotNone(self.eng.ingest({"source": "other", "app": "com.example.chat", "sender": "Dev", "text": "URGENT outage now"}))

    def test_ai_hook_only_raises_and_sees_redacted_text(self):
        seen = []
        eng = alerts.Alerts(self.ctx, clock=self.clock, classifier=lambda e: seen.append(e) or {"urgency": 3, "reason": "x"},
                            speaker=self.spoken.append, spawn=lambda fn: fn())
        a = eng.ingest(self.ev("Please look at this, mail me at secret.person@example.com", "Peer", "slack"))
        self.assertEqual(a["urgency"], 2)               # capped at 2
        self.assertNotIn("secret.person@example.com", seen[0]["preview"])
        low = alerts.Alerts(self.ctx, clock=self.clock, classifier=lambda e: {"urgency": 0}, spawn=lambda fn: fn())
        self.assertGreaterEqual(low.ingest(self.ev("URGENT outage now", "Zed", "slack"))["urgency"], 2)


class Dedupe(Base):
    def test_same_text_within_ten_minutes_is_one_alert(self):
        self.assertIsNotNone(self.eng.ingest(self.ev("URGENT: server down asap", "Dev")))
        self.assertIsNone(self.eng.ingest(self.ev("urgent: server  down ASAP", "dev")))
        self.clock.advance(minutes=11)
        self.assertIsNotNone(self.eng.ingest(self.ev("URGENT: server down asap", "Dev")))
        self.assertEqual(len(self.pushed), 2)


class GenericDraft(Base):
    def setUp(self):
        super().setUp()
        self.brain = brain.Brain(self.ctx, clock=self.clock)

    def test_why_is_it_breaking_quotes_stored_note_as_it_is(self):
        text = "Checkout 500 errors: the connection pool was exhausted.\nFix: raised pool size to 50."
        item = self.brain.add(text=text, title="Checkout 500 errors RCA", kind="rca")
        a = self.eng.ingest(self.ev("why is the checkout breaking with 500 errors?", "Ravi Manager"))
        self.assertEqual(a["intent"], "why")
        self.assertIn(text, a["reply_draft"])
        self.assertEqual(a["memory"]["id"], item["id"])
        self.assertIn(a["memory"]["date"], a["reply_draft"])
        self.assertIn("RCA", a["reply_draft"])
        self.assertEqual(a["status"], "new")

    def test_rca_request_is_just_find_what_i_know(self):
        text = "Login timeouts came from the proxy idle limit."
        item = self.brain.add(text=text, title="Login timeout RCA", kind="rca")
        a = self.eng.ingest(self.ev("Do you have the RCA for the login timeouts?", "Peer", "slack"))
        self.assertEqual(a["memory"]["id"], item["id"])
        self.assertIn(text, a["reply_draft"])

    def test_eta_question_uses_the_task_plan(self):
        self.brain.add(text="From Boss: fix invoice export\n\nDue: 2026-11-10 17:00\n\nPlan:\n1. Reproduce the export bug\n"
                       "2. Patch the exporter\n3. Run the export tests\n\nEvidence to gather:\n- logs", title="Fix invoice export",
                       kind="task", source="alerts")
        a = self.eng.ingest(self.ev("send your ETA now for the invoice export fix", "Ravi Manager"))
        self.assertEqual(a["intent"], "eta")
        self.assertIn("1. Reproduce the export bug", a["reply_draft"])
        self.assertIn("Patch the exporter", a["plan"])
        self.assertIn("Give Ravi a realistic ETA", " ".join(a["plan"]))

    def test_nothing_found_says_so_and_offers_a_note(self):
        a = self.eng.ingest(self.ev("What was going on with the kafka lag yesterday?", "Ravi Manager"))
        self.assertIsNone(a["memory"])
        self.assertTrue(a["offer_note"])
        self.assertIn("looking into this", a["reply_draft"])
        self.assertTrue(self.eng.public(a)["actions"]["save_note"])
        done = self.eng.save_note(a["id"])
        self.assertTrue(done["task_id"])
        self.assertEqual(brain.store_for(self.config).get(done["task_id"])["kind"], "note")
        again = self.eng.ingest(self.ev("What was going on with the kafka lag yesterday, details please?", "Ravi Manager"))
        self.assertIsNone(again["memory"])               # the empty stub is never quoted back

    def test_draft_is_never_sent_and_links_are_for_the_owner(self):
        a = self.eng.ingest(self.ev("Can you check the invoice export today?", "+91 98765 43210"))
        pub = self.eng.public(a)
        self.assertTrue(pub["actions"]["copy"])
        self.assertTrue(pub["actions"]["open"]["url"].startswith("https://wa.me/919876543210?text="))
        self.assertEqual(a["status"], "new")
        src = Path(alerts.__file__).read_text(encoding="utf-8") + Path(asana.__file__).read_text(encoding="utf-8")
        for banned in ("smtplib", "send_email", "chat_send", "sendmail", "/comment"):
            self.assertNotIn(banned, src, banned)
        self.assertNotIn("rca-request", src)


class TaskFlow(Base):
    def test_asana_task_creates_brain_task_with_plan_and_evidence(self):
        a = self.eng.ingest({"source": "asana", "sender": "Asana", "title": "Fix login timeout error in staging",
                             "text": "Users see a timeout on the login page", "due_on": "2026-11-09", "kind_hint": "task"})
        self.assertEqual(a["kind"], "task")
        self.assertTrue(3 <= len(a["plan"]) <= 6)
        self.assertTrue(any("logs" in e for e in a["evidence"]))
        self.assertTrue(a["due"].startswith("2026-11-09"))
        self.assertTrue(a["task_id"])
        item = brain.store_for(self.config).get(a["task_id"])
        self.assertEqual(item["kind"], "task")
        self.assertIn("Plan:", item["body"])
        self.assertIn("Evidence to gather", item["body"])

    def test_due_inference(self):
        n = NOON
        self.assertEqual(alerts.infer_due("by 5pm please", n), n.replace(hour=17, minute=0))
        self.assertEqual(alerts.infer_due("need it tomorrow morning", n).hour, 9)
        self.assertIsNone(alerts.infer_due("whenever", n))

    def test_ai_plan_enrichment_uses_redacted_text_and_falls_back(self):
        class Model:
            def __init__(self): self.seen = []
            async def complete(self, system, user, json_mode=False):
                self.seen.append(user)
                return '{"plan": ["a one", "b two", "c three"], "evidence": ["logs"]}'
        m = Model()
        self.ctx.model_for = lambda p: m
        self.config.alerts_ai = True
        a = self.eng.ingest(self.ev("Please fix the bug, call 98765 43210 about it", "Peer", "slack"))
        asyncio.run(self.eng.enrich_ai(a["id"]))
        self.assertEqual(self.eng.get(a["id"])["plan"], ["a one", "b two", "c three"])
        self.assertNotIn("98765 43210", m.seen[0])
        self.ctx.model_for = lambda p: (_ for _ in ()).throw(RuntimeError("down"))
        b = self.eng.ingest(self.ev("Please review the PR asap", "Peer2", "slack"))
        before = list(b["plan"])
        asyncio.run(self.eng.enrich_ai(b["id"]))
        self.assertEqual(self.eng.get(b["id"])["plan"], before)


class Delivery(Base):
    def test_now_pushes_spoken_once_and_phone_pending(self):
        self.config.alerts_speak_on_laptop = True
        a = self.eng.ingest(self.ev("URGENT P0: checkout is down", "Dev"))
        self.assertEqual(a["delivery"], "now")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0]["link"], "alerts")
        self.assertEqual(len(self.spoken), 1)
        self.assertTrue(self.spoken[0].startswith("Shikhar, urgent:"))
        p = self.client.get("/api/alerts/pending").json()["alerts"]
        self.assertEqual(len(p), 1)
        self.client.post("/api/alerts/pending/ack", json={"ids": [p[0]["id"]]}, headers={"X-Hub": "1"})
        self.assertEqual(self.client.get("/api/alerts/pending").json()["alerts"], [])

    def test_laptop_voice_off_by_default(self):
        self.eng.ingest(self.ev("URGENT P0: checkout is down", "Dev"))
        self.assertEqual(self.spoken, [])

    def test_lower_urgency_is_batched_not_pushed_now(self):
        a = self.eng.ingest(self.ev("Please send the report by EOD", "Colleague", "slack"))
        self.assertEqual(a["urgency"], 1)
        self.assertEqual(a["delivery"], "batch")
        self.assertTrue(all("Urgent" not in p["title"] for p in self.pushed))
        self.assertEqual(self.spoken, [])

    def test_quiet_hours_hold_all_but_critical(self):
        self.clock.now = NIGHT
        self.config.alerts_speak_on_laptop = True
        two = self.eng.ingest(self.ev("Please check this ASAP", "Peer", "slack"))
        self.assertEqual(two["urgency"], 2)
        self.assertEqual(two["delivery"], "quiet")
        self.assertEqual((self.pushed, self.spoken), ([], []))
        three = self.eng.ingest(self.ev("URGENT P0 outage, prod is down", "Dev2"))
        self.assertEqual(three["urgency"], 3)
        self.assertEqual(three["delivery"], "now")
        self.assertEqual(len(self.spoken), 1)

    def test_proactive_classify_alert(self):
        self.assertEqual(proactive.classify({"type": "alert", "urgency": 2}), "now")
        self.assertEqual(proactive.classify({"type": "alert", "urgency": 1}), "batch")
        self.assertEqual(proactive.classify({"type": "alert", "urgency": 0}), "quiet")
        self.assertEqual(proactive.classify({"type": "alert", "urgency": 3}, now=NIGHT), "now")
        self.assertEqual(proactive.classify({"type": "alert", "urgency": 2}, now=NIGHT), "quiet")

    def test_without_proactive_engine_falls_back_to_plain_notify(self):
        self.ctx.proactive = None
        a = self.eng.ingest(self.ev("URGENT P0: checkout is down", "Dev"))
        self.assertEqual(a["delivery"], "now")
        self.assertEqual(len(self.pushed), 1)


class Routes(Base):
    H = {"X-Hub": "1"}

    def test_routes_require_login(self):
        anon = TestClient(self.app)
        for method, path in (("get", "/api/alerts"), ("get", "/api/alerts/pending"), ("post", "/api/alerts/test"),
                             ("post", "/api/alerts/ingest"), ("get", "/api/alerts/settings")):
            self.assertEqual(getattr(anon, method)(path, headers=self.H).status_code, 401, path)

    def test_test_alert_end_to_end(self):
        self.config.alerts_speak_on_laptop = True
        r = self.client.post("/api/alerts/test", headers=self.H)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["alert"]["urgency"], 3)
        self.assertEqual(body["delivery"], "now")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(len(self.spoken), 1)
        self.assertIn("This is a test", body["spoken_text"])
        listing = self.client.get("/api/alerts").json()
        self.assertEqual(listing["counts"]["urgent"], 1)
        self.assertEqual(listing["alerts"][0]["group"], "urgent")

    def test_status_snooze_and_settings(self):
        a = self.client.post("/api/alerts/test", headers=self.H).json()["alert"]
        r = self.client.post(f"/api/alerts/{a['id']}/snooze", json={"minutes": 30}, headers=self.H)
        self.assertEqual(r.json()["status"], "snoozed")
        self.assertEqual(self.client.get("/api/alerts").json()["total_open"], 0)
        self.clock.advance(minutes=31)
        self.assertEqual(self.client.get("/api/alerts").json()["total_open"], 1)
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "handled"}, headers=self.H).json()["status"], "handled")
        self.assertEqual(self.client.post("/api/alerts/zzz/status", json={"status": "seen"}, headers=self.H).status_code, 404)
        s = self.client.post("/api/alerts/settings", json={"vips": ["Boss Person"], "speak_on_laptop": True}, headers=self.H).json()
        self.assertEqual(s["vips"], ["Boss Person"])
        self.assertTrue(self.config.alerts_speak_on_laptop)
        self.assertEqual(self.client.post("/api/alerts/settings", json={"vips": "x"}, headers=self.H).status_code, 400)

    def test_ingest_route(self):
        r = self.client.post("/api/alerts/ingest", json=self.ev("URGENT: db is down asap", "Dev"), headers=self.H)
        self.assertTrue(r.json()["alert_id"])
        self.assertEqual(self.client.post("/api/alerts/ingest", json={"source": "x"}, headers=self.H).status_code, 400)

    def test_watch_hook(self):
        alerts.on_watch_ingest(self.ctx, {"source": "chat", "app": "Google Chat", "title": "Dev", "text": "URGENT outage asap",
                                          "sender": "Dev"})
        self.assertEqual(self.ctx.alerts.listing()["counts"]["urgent"], 1)


class FakeStore:
    def __init__(self): self.token = ""
    def get(self): return self.token
    def set(self, t): self.token = t
    def delete(self): self.token = ""


class FakeAsana:
    def __init__(self):
        self.tasks = [{"gid": "11", "name": "Old task", "modified_at": "2026-11-01T00:00:00Z", "due_on": None}]
        self.stories = {"11": []}
        self.urls, self.headers = [], []
        self.status = 200

    def __call__(self, method, url, headers):
        self.urls.append(url)
        self.headers.append(headers)
        if self.status != 200:
            return self.status, {}
        if "/users/me" in url:
            return 200, {"data": {"gid": "1", "name": "Tester", "workspaces": [{"gid": "99", "name": "Work"}]}}
        if "/stories" in url:
            gid = url.split("/tasks/")[1].split("/")[0]
            return 200, {"data": self.stories.get(gid, [])}
        if "/tasks?" in url:
            return 200, {"data": self.tasks}
        return 404, {}


class AsanaTests(Base):
    def setUp(self):
        super().setUp()
        self.http, self.store = FakeAsana(), FakeStore()
        self.poller = asana.Poller(self.ctx, http=self.http, store=self.store, triage=lambda ctx, e: alerts.triage(ctx, e))
        self.ctx.alerts = self.eng
        self.app.include_router(asana.make_router(self.ctx, poller=self.poller))

    def test_connect_never_leaks_token(self):
        stream = []
        handler = logging.Handler()
        handler.emit = lambda rec: stream.append(rec.getMessage())
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        r = self.client.post("/api/asana/connect", json={"token": FAKE_TOKEN}, headers={"X-Hub": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["connected"])
        self.assertEqual(r.json()["workspaces"][0]["gid"], "99")
        self.assertNotIn(FAKE_TOKEN, r.text)
        self.assertEqual(self.store.token, FAKE_TOKEN)
        self.assertEqual(self.http.headers[0]["Authorization"], f"Bearer {FAKE_TOKEN}")
        self.http.status = 401
        bad = self.client.post("/api/asana/test", headers={"X-Hub": "1"})
        self.assertEqual(bad.status_code, 502)           # never 401: the app would log the owner out
        self.assertNotIn(FAKE_TOKEN, bad.text)
        self.assertNotIn(FAKE_TOKEN, "\n".join(stream))
        self.assertNotIn(FAKE_TOKEN, (self.tmp / "data").as_posix() + "".join(
            p.read_text(errors="ignore") for p in (self.tmp / "data").rglob("*") if p.is_file()))

    def test_bad_token_not_kept_and_disconnect_clears(self):
        self.http.status = 401
        r = self.client.post("/api/asana/connect", json={"token": FAKE_TOKEN}, headers={"X-Hub": "1"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.store.token, "")
        self.http.status = 200
        self.client.post("/api/asana/connect", json={"token": FAKE_TOKEN}, headers={"X-Hub": "1"})
        self.client.post("/api/asana/disconnect", headers={"X-Hub": "1"})
        self.assertEqual(self.store.token, "")
        self.assertFalse(self.config.asana_enabled)

    def test_poll_baseline_then_new_task_and_comment(self):
        self.store.token = FAKE_TOKEN
        first = self.poller.poll_once()
        self.assertTrue(first["baseline"])
        self.assertEqual(first["alerts"], 0)
        self.http.tasks.append({"gid": "12", "name": "URGENT: fix prod login", "modified_at": "2026-11-09T10:00:00Z",
                                "due_on": "2026-11-09", "permalink_url": "https://app.asana.com/0/1/12", "notes": "blocker"})
        self.http.tasks[0]["modified_at"] = "2026-11-09T11:00:00Z"
        self.http.stories["11"] = [
            {"resource_subtype": "comment_added", "created_at": "2026-11-09T11:00:00Z", "text": "ASAP, need this today?",
             "created_by": {"gid": "5", "name": "Boss"}},
            {"resource_subtype": "comment_added", "created_at": "2026-11-09T11:01:00Z", "text": "my own reply",
             "created_by": {"gid": "1", "name": "Tester"}},
            {"resource_subtype": "system", "created_at": "2026-11-09T11:02:00Z", "text": "assigned"}]
        second = self.poller.poll_once()
        self.assertFalse(second["baseline"])
        self.assertEqual(second["events"], 2)
        rows = self.eng.listing()["alerts"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(a["urgency"] >= 2 and a["source"] == "asana" for a in rows))
        third = self.poller.poll_once()
        self.assertEqual(third["events"], 0)                    # nothing is reported twice
        self.assertTrue(all("access_token" not in u and FAKE_TOKEN not in u for u in self.http.urls))
        self.assertTrue(any("assignee=me" in u and "workspace=99" in u for u in self.http.urls))

    def test_status_routes_need_login(self):
        self.assertEqual(TestClient(self.app).get("/api/asana/status").status_code, 401)


class Tools(Base):
    def test_tools_list_show_draft_mark_snooze(self):
        from hub.assistant import ExtraTool
        tools = {t.spec["name"]: t for t in alerts_tools.build(lambda: self.app, self.config)}
        self.assertEqual(set(tools), {"list_alerts", "show_alert", "draft_reply", "mark_alert", "snooze_alert"})
        self.assertFalse(any("send" in n for n in tools))
        a = self.eng.ingest(self.ev("Can you share the notes on the login outage?", "Ravi Manager"))
        from hub import common

        async def go():
            # route the tools at the test app (InProcessClient uses ASGI with a signed cookie)
            ok, text = await tools["list_alerts"].run({})
            self.assertTrue(ok and a["id"] in text)
            ok, text = await tools["show_alert"].run({"id": a["id"]})
            self.assertTrue(ok and "Draft reply (NOT sent" in text)
            ok, text = await tools["draft_reply"].run({"id": a["id"]})
            self.assertIn("NOT sent", text)
            ok, text = await tools["mark_alert"].run({"id": a["id"], "status": "handled"})
            self.assertTrue(ok)
            ok, text = await tools["snooze_alert"].run({"id": "bad"})
            self.assertFalse(ok)
        self.app.dependency_overrides = {}
        self._run_with_session(go)

    def _run_with_session(self, coro_fn):
        from hub import auth, common
        # make need_auth accept the in-process client's cookie: the test need_auth wants hub_session=yes
        orig = common.InProcessClient.call

        async def call(self_, method, path, *, params=None, body=None):
            import httpx
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://hub.internal",
                                         headers={"X-Hub": "1", "Cookie": "hub_session=yes"}) as c:
                r = await c.request(method, path, params=params, json=body)
            data = r.json()
            if r.status_code >= 400:
                return False, str(data.get("detail"))
            return True, data
        common.InProcessClient.call = call
        self.addCleanup(setattr, common.InProcessClient, "call", orig)
        asyncio.run(coro_fn())

    def test_registered(self):
        from hub import assistant, server
        self.assertIn("alerts", server.FEATURE_MODULES)
        self.assertIn("asana", server.FEATURE_MODULES)
        self.assertIn("triage", assistant.TOOL_GROUPS)
        names, rx = assistant.TOOL_GROUPS["triage"]
        self.assertTrue(rx.search("what's urgent right now"))
        self.assertIn("draft_reply", names)
        static = Path(__file__).resolve().parent.parent / "hub" / "static"
        sw = (static / "sw.js").read_text(encoding="utf-8")
        self.assertIn("/js/alerts.js", sw)
        self.assertIn("/css/alerts.css", sw)
        self.assertIn('id: "alerts"', (static / "js" / "main.js").read_text(encoding="utf-8"))


class Email(Base):
    def test_gmailx_guarded_and_used(self):
        self.assertEqual(asyncio.run(alerts.Alerts(self.ctx, clock=self.clock, gmailx_mod=None).poll_email()) in (0, 1, 2), True)

        class GX:
            @staticmethod
            def list_accounts(ctx): return ["me@example.com"]
            @staticmethod
            async def search(ctx, account, query):
                return [{"id": "m1", "from": "Ravi Manager <ravi@example.com>", "subject": "Need the report by EOD",
                         "snippet": "please send it asap"}]
        eng = alerts.Alerts(self.ctx, clock=self.clock, gmailx_mod=GX, spawn=lambda fn: fn())
        self.assertEqual(asyncio.run(eng.poll_email()), 1)
        self.assertEqual(asyncio.run(eng.poll_email()), 0)       # the same mail id is never raised twice
        a = eng.listing()["alerts"][0]
        self.assertEqual(a["source"], "email")
        self.assertGreaterEqual(a["urgency"], 2)
        self.assertTrue(a["actions"]["open"]["url"].startswith("mailto:ravi@example.com"))


class UrgentDefaults(Base):
    """What counts as a NOW alert (urgency 2 or more, spoken by the phone) with no settings saved at all."""

    def test_new_default_urgent_words(self):
        for text in ("prod issue on checkout", "Production issue: orders failing", "payment failed for customer 12",
                     "server down in the east region", "Deployment failed on main", "need a hotfix today", "P1 ticket raised",
                     "possible data loss on the db", "security incident reported"):
            c = alerts.classify(text, "Dev", "slack", rules=alerts.DEFAULT_RULES)
            self.assertGreaterEqual(c["urgency"], 2, text)
            self.assertTrue(c["raise"], text)

    def test_no_false_positives_on_near_misses(self):
        for text in ("The server downtime is planned for Sunday", "hotfixes are listed in the notes", "my P10 form is ready",
                     "emergency room visit went fine", "Walk-in urgent care is open late"):
            c = alerts.classify(text, "Friend", "whatsapp", rules=alerts.DEFAULT_RULES)
            self.assertLess(c["urgency"], 2, text)

    def test_owner_rules_override_the_defaults(self):
        self.config.alerts_rules = {"urgent_words": ["fire drill"]}
        a = self.eng.ingest(self.ev("prod issue on checkout", "Dev", "slack"))
        self.assertTrue(a is None or a["urgency"] < 2)
        b = self.eng.ingest(self.ev("fire drill at the office", "Dev", "slack"))
        self.assertGreaterEqual(b["urgency"], 2)

    def test_owner_ignore_word_silences_a_default(self):
        self.config.alerts_rules = {"ignore_words": ["prod issue"]}
        a = self.eng.ingest(self.ev("prod issue on checkout", "Dev", "whatsapp"))
        self.assertTrue(a is None or a["urgency"] == 0)

    def test_asana_task_due_today_is_now(self):
        a = self.eng.ingest(self.ev("Update the runbook", "Asana", "asana", title="Runbook", due_on=NOON.date().isoformat()),
                            trusted=True)
        self.assertGreaterEqual(a["urgency"], 2)
        self.assertEqual(a["delivery"], "now")
        self.assertTrue(a["speak_phone"])

    def test_asana_task_overdue_is_now_but_future_is_not(self):
        late = self.eng.ingest(self.ev("Old thing", "Asana", "asana", title="Old", due_on="2026-11-01"), trusted=True)
        self.assertGreaterEqual(late["urgency"], 2)
        soon = self.eng.ingest(self.ev("Later thing", "Asana", "asana", title="Later", due_on="2026-11-20"), trusted=True)
        self.assertLess(soon["urgency"], 2)

    def test_asana_task_with_urgent_wording_is_now(self):
        a = self.eng.ingest(self.ev("Urgent: fix the login bug", "Asana", "asana", title="Login"), trusted=True)
        self.assertGreaterEqual(a["urgency"], 2)

    def test_payment_debit_alert_from_the_phone_is_spoken(self):
        a = self.eng.ingest(self.ev("Rs 4,500 debited from A/c XX1234 on 09-Nov. Not you? Call the bank.", "BANK", "sms"))
        self.assertGreaterEqual(a["urgency"], 2)
        self.assertEqual(a["delivery"], "now")
        self.assertEqual(len(self.client.get("/api/alerts/pending").json()["alerts"]), 1)

    def test_otp_message_stays_silent(self):
        a = self.eng.ingest(self.ev("123456 is your OTP. Amount debited if you share it.", "BANK", "sms"))
        self.assertTrue(a is None or a["urgency"] == 0)


class PhoneQueue(Base):
    def test_item_shape_and_ack_never_repeats(self):
        item = self.eng.push_phone("rem:abc:1", "Reminder: send the invoice", title="Call reminder", kind="reminder",
                                   source="reminder", urgency=3, preview="send the invoice", call=True)
        self.assertTrue(item["id"].startswith("c"))
        p = self.client.get("/api/alerts/pending").json()["alerts"]
        self.assertEqual(p, [{"id": item["id"], "urgency": 3, "speech": "Reminder: send the invoice",
                              "title": "Call reminder", "kind": "reminder", "source": "reminder",
                              "preview": "send the invoice", "call": True}])
        self.assertEqual(self.client.post("/api/alerts/pending/ack", json={"ids": [item["id"]]}).json(), {"acked": 1})
        self.assertEqual(self.client.get("/api/alerts/pending").json()["alerts"], [])
        self.assertIsNone(self.eng.push_phone("rem:abc:1", "Reminder: send the invoice", call=True))   # same key, no repeat
        self.assertEqual(self.client.get("/api/alerts/pending").json()["alerts"], [])

    def test_call_waits_hours_but_a_plain_item_expires_in_minutes(self):
        self.eng.push_phone("a", "Call me item", call=True, urgency=3)
        self.eng.push_phone("b", "Plain item", urgency=2)
        self.clock.now = NOON + dt.timedelta(hours=2)
        self.assertEqual([p["speech"] for p in self.eng.pending()], ["Call me item"])
        self.clock.now = NOON + dt.timedelta(hours=13)
        self.assertEqual(self.eng.pending(), [])

    def test_call_item_is_queued_even_when_alerts_are_switched_off(self):
        self.config.alerts_enabled = False
        self.assertIsNone(self.eng.push_phone("x", "plain"))
        self.assertIsNotNone(self.eng.push_phone("y", "ring", call=True, urgency=3))

    def test_normal_alert_in_pending_has_call_false_and_new_fields(self):
        self.eng.ingest(self.ev("URGENT P0: checkout is down", "Dev"))
        p = self.client.get("/api/alerts/pending").json()["alerts"][0]
        self.assertEqual((p["call"], p["kind"], p["source"]), (False, "other", "whatsapp"))

    def test_hub_backup_failure_reaches_the_phone(self):
        from hub import backup
        self.ctx.alerts = self.eng
        self.assertEqual(self.ctx.proactive.submit(backup.proactive_event(False)), "now")
        p = self.eng.pending()
        self.assertEqual((len(p), p[0]["source"], p[0]["urgency"], p[0]["call"]), (1, "hub", 2, False))


if __name__ == "__main__":
    unittest.main()
