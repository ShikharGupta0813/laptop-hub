"""Tests for the application queue (hub/applyq.py, hub/applyq_tools.py). All fakes and synthetic data: no network,
no AI, no real Gmail. The hard rule under test: nothing sends unless the explicit send call is made."""
import asyncio
import copy
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import applyq, applyq_tools, proactive, safety
from hub.assistant import TOOL_GROUPS, route_local_tools
from hub.common import Ctx
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 9, 15, 0)

PROFILE = {"name": "Test Person", "headline": "Backend developer", "summary": "Backend developer who builds reliable APIs.",
           "skills": ["Python", "FastAPI", "SQLite", "Kubernetes"],
           "experience": [{"title": "Developer", "company": "ExampleCo", "bullets": ["Built a Python API used by 3 teams", "Organised the office party"]}],
           "links": ["https://example.test/me"], "standard_answers": {"Notice period": "2 weeks"}, "resume_text": "..."}

JOB = {"title": "Python API developer", "company": "Acme", "url": "https://jobs.example.test/1?utm_source=x", "source": "fake",
       "description": "We need Python and FastAPI. Apply by email to hiring@acme.test. Please state your expected rate.",
       "score": 0.8, "reason": "Skills match", "flags": ["remote"], "kind": "job"}


class FakeMail:
    def __init__(self, ok=True, replies=None):
        self.sent, self.ok, self.replies = [], ok, replies or []

    def send_mail(self, ctx, account, to, subject, body, attachments=None):
        self.sent.append((account, to, subject, body))
        return {"ok": True} if self.ok else {"ok": False, "error": "smtp down"}

    def list_accounts(self, ctx):
        return [{"id": "main", "label": "Main", "address": "me@example.test"}]

    def search(self, ctx, account, query):
        return self.replies


class NoSearchMail(FakeMail):
    search = None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="applyq-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Chief")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits, self.notes = [], []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)),
                       notify=lambda *a, **k: self.notes.append((a, k)))
        self.mail = FakeMail()
        self.profile = copy.deepcopy(PROFILE)
        self.clock = lambda: NOW
        self.svc = applyq.Applyq(self.ctx, clock=lambda: self.clock(), profile_fn=lambda c: self.profile, gmailx=self.mail)

    def client(self, **kw):
        app = FastAPI()
        app.include_router(applyq.make_router(self.ctx, clock=lambda: self.clock(), profile_fn=lambda c: self.profile,
                                              gmailx=kw.get("gmailx", self.mail)))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(c.close)
        return c

    def job(self, n=1, **over):
        return {**JOB, "url": f"https://jobs.example.test/{n}", **over}


class AddAndDraft(Base):
    def test_dedupe_by_url_ignores_tracking_and_slash(self):
        a, created = self.svc.add(self.job(1))
        b, again = self.svc.add({**self.job(1), "url": "https://JOBS.example.test/1/?utm_source=z#top"})
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.svc.store.count(), 1)

    def test_add_item_is_the_finder_interface_and_never_raises(self):
        stored = applyq.add_item(self.ctx, self.job(2))
        self.assertEqual(stored["state"], "ready")
        self.assertEqual(stored["contact_email"], "hiring@acme.test")
        self.assertEqual(stored["channel"], "email")
        self.assertIsNone(applyq.add_item(self.ctx, {"foo": 1}))
        self.assertIsNone(applyq.add_item(self.ctx, "nonsense"))
        self.config.applyq_enabled = False
        self.assertIsNone(applyq.add_item(self.ctx, self.job(3)))

    def test_draft_uses_only_profile_facts(self):
        item, _ = self.svc.add(self.job())
        d = item["draft"]
        self.assertIn("Backend developer who builds reliable APIs.", d["body"])
        self.assertIn("Python", d["body"])
        self.assertIn("FastAPI", d["body"])
        self.assertNotIn("Kubernetes", d["body"])                      # not relevant to the posting
        self.assertIn("Built a Python API used by 3 teams", d["body"])
        self.assertNotIn("office party", d["body"])                    # not relevant, not used
        self.assertEqual(d["facts_from"], ["profile"])
        for invented in ("10 years", "led a team", "AWS"):
            self.assertNotIn(invented, d["body"])

    def test_missing_info_is_flagged_not_invented(self):
        item, _ = self.svc.add(self.job())
        self.assertIn("expected rate", item["draft"]["missing"])
        self.assertIn("[FILL: expected rate]", item["draft"]["body"])
        self.assertEqual(self.svc.public(item)["missing"], ["expected rate"])
        self.profile["standard_answers"]["Expected rate"] = "50 per hour"
        item = self.svc.draft(item["id"])
        self.assertNotIn("expected rate", item["draft"]["missing"])
        self.assertIn("50 per hour", item["draft"]["body"])

    def test_no_profile_degrades_gracefully(self):
        self.profile = {}
        item, _ = self.svc.add(self.job())
        self.assertEqual(item["state"], "ready")
        self.assertTrue(any("profile" in m for m in item["draft"]["missing"]))
        self.assertIn("[FILL: short introduction about you]", item["draft"]["body"])
        self.assertEqual(item["draft"]["facts_from"], [])

    def test_missing_profile_module_means_empty_profile(self):
        with mock.patch.object(applyq, "_optional", return_value=None):
            self.assertEqual(applyq.load_profile(self.ctx), {})

    def test_injected_draft_fn_is_used_and_failures_fall_back(self):
        svc = applyq.Applyq(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, gmailx=self.mail,
                            draft_fn=lambda item, prof, base: {"body": "AI body [FILL: rate]", "cover_note": "AI note"})
        item, _ = svc.add(self.job(5))
        self.assertEqual(item["draft"]["generator"], "ai")
        self.assertIn("rate", item["draft"]["missing"])
        boom = applyq.Applyq(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, gmailx=self.mail,
                             draft_fn=lambda *a: 1 / 0)
        item, _ = boom.add(self.job(6))
        self.assertEqual(item["draft"]["generator"], "template")

    def test_channel_inference(self):
        self.assertEqual(applyq.infer_channel({"contact_email": "a@b.test"}), "email")
        self.assertEqual(applyq.infer_channel({"kind": "message"}), "message")
        self.assertEqual(applyq.infer_channel({}), "web")
        self.assertEqual(applyq.infer_channel({"channel": "message", "contact_email": "a@b.test"}), "message")


class NeverAutomatic(Base):
    def test_no_code_path_sends_except_send(self):
        with mock.patch.object(applyq, "load_gmailx", return_value=self.mail):
            item = applyq.add_item(self.ctx, self.job(7))
            applyq.add_item(self.ctx, self.job(7))                                   # duplicate
            self.svc.draft(item["id"])
            self.svc.update(item["id"], {"note": "hi"})
            self.svc.mark(item["id"], "applied")
            self.svc.summary()
            self.svc.check_replies()
            applyq.today_line(self.config)
            engine = proactive.Proactive(self.ctx, clock=lambda: dt.datetime(2026, 11, 9, 23, 0))
            engine.tick()
            c = self.client()
            c.get("/api/applyq")
            c.post("/api/applyq/manual", json={"text": "https://x.example.test/job"})
            c.post(f"/api/applyq/{item['id']}/draft", json={})
        self.assertEqual(self.mail.sent, [])
        self.assertEqual(self.svc.store.get(item["id"])["sent_at"], None)


class Sending(Base):
    def ready(self, n=1, fill=True, **over):
        item, _ = self.svc.add(self.job(n, **over))
        if fill:                                           # the owner filled in the rate
            d = item["draft"]
            self.svc.update(item["id"], {"draft": {"body": d["body"].replace("[FILL: expected rate]", "40/h"),
                                                   "subject": d["subject"]}})
        return item["id"]

    def test_email_send_uses_chosen_account_and_marks_sent(self):
        i = self.ready()
        r = self.client().post(f"/api/applyq/{i}/send", json={"account": "main"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["sent"])
        self.assertEqual(len(self.mail.sent), 1)
        account, to, subject, body = self.mail.sent[0]
        self.assertEqual((account, to), ("main", "hiring@acme.test"))
        self.assertIn("40/h", body)
        self.assertEqual(self.svc.get(i)["state"], "sent")
        again = self.svc.send(i)                                                     # cannot send twice
        self.assertFalse(again["ok"])
        self.assertEqual(len(self.mail.sent), 1)

    def test_unfilled_markers_block_sending(self):
        i = self.ready(fill=False)
        r = self.client().post(f"/api/applyq/{i}/send", json={}).json()
        self.assertFalse(r["ok"])
        self.assertIn("expected rate", r["error"])
        self.assertEqual(self.mail.sent, [])

    def test_gmail_absent_offers_copy(self):
        i = self.ready()
        svc = applyq.Applyq(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, gmailx=None)
        with mock.patch.object(applyq, "load_gmailx", return_value=None):
            r = svc.send(i)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_gmail")
        self.assertIn("hiring@acme.test", r["copy"]["to"])
        self.assertEqual(self.svc.get(i)["state"], "ready")

    def test_failed_send_is_recorded_and_can_be_retried(self):
        i = self.ready()
        self.mail.ok = False
        r = self.svc.send(i)
        self.assertFalse(r["ok"])
        self.assertEqual(self.svc.get(i)["state"], "failed")
        self.mail.ok = True
        self.assertTrue(self.svc.send(i)["sent"])

    def test_web_and_message_channels_never_send(self):
        w, _ = self.svc.add({**self.job(8), "description": "Apply on our site."})
        self.assertEqual(w["channel"], "web")
        r = self.client().post(f"/api/applyq/{w['id']}/send", json={}).json()
        self.assertTrue(r["ok"])
        self.assertFalse(r["sent"])
        self.assertEqual(r["action"], "open")
        self.assertEqual(self.mail.sent, [])
        m, _ = self.svc.add({**self.job(9), "kind": "message", "description": "DM me."})
        self.assertEqual(self.svc.send(m["id"])["action"], "copy")
        self.assertEqual(self.mail.sent, [])
        r = self.client().post(f"/api/applyq/{w['id']}/applied")
        self.assertEqual(r.json()["state"], "applied")

    def test_daily_cap_needs_extra_confirm(self):
        self.config.applyq_daily_cap = 2
        ids = [self.ready(n=20 + n) for n in range(3)]
        c = self.client()
        for i in ids[:2]:
            self.assertEqual(c.post(f"/api/applyq/{i}/send", json={}).status_code, 200)
        r = c.post(f"/api/applyq/{ids[2]}/send", json={})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirm"])
        self.assertEqual(len(self.mail.sent), 2)
        r = c.post(f"/api/applyq/{ids[2]}/send", json={"confirm_over_cap": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.mail.sent), 3)

    def test_cap_zero_means_no_cap_and_next_day_resets(self):
        self.config.applyq_daily_cap = 1
        a, b = self.ready(n=30), self.ready(n=31)
        self.assertTrue(self.svc.send(a)["sent"])
        self.assertTrue(self.svc.send(b).get("needs_confirm"))
        self.clock = lambda: NOW + dt.timedelta(days=1)
        self.assertTrue(self.svc.send(b)["sent"])


class SummaryAndReplies(Base):
    def test_summary_counts_and_streak(self):
        ids = []
        for n in range(4):
            item, _ = self.svc.add(self.job(40 + n, description="site only"))
            ids.append(item["id"])
        self.svc.mark(ids[0], "applied")
        self.svc.mark(ids[1], "skipped")
        s = self.svc.summary()
        self.assertEqual(s["today"], {"applied": 1, "sent": 0, "ready": 2, "skipped": 1, "replied": 0})
        self.assertEqual(s["streak"], 1)
        self.clock = lambda: NOW + dt.timedelta(days=1)
        s = self.svc.summary()
        self.assertEqual(s["today"]["applied"], 0)
        self.assertEqual(s["week"]["applied"], 1)
        self.assertEqual(s["streak"], 1)                                             # yesterday still counts
        self.clock = lambda: NOW + dt.timedelta(days=3)
        self.assertEqual(self.svc.summary()["streak"], 0)
        self.assertEqual(self.svc.daily_line(), "Today: 0 applied, 2 ready, 0 replies")

    def test_reply_detection_by_sender_domain(self):
        item, _ = self.svc.add(self.job(50))
        self.svc.update(item["id"], {"draft": {"body": item["draft"]["body"].replace("[FILL: expected rate]", "x")}})
        self.svc.send(item["id"])
        self.mail.replies = [{"from": "Someone Else <x@other.test>", "subject": "Hi"}]
        self.assertEqual(self.svc.check_replies()["replied"], [])
        self.mail.replies = [{"from": "Hiring <hiring@acme.test>", "subject": "Re: your application"}]
        out = self.svc.check_replies()
        self.assertEqual(out["replied"], [item["id"]])
        self.assertEqual(self.svc.get(item["id"])["state"], "replied")
        self.assertEqual(self.svc.summary()["today"]["replied"], 1)

    def test_generic_mail_domains_need_the_exact_address(self):
        item, _ = self.svc.add(self.job(51, description="mail boss@gmail.com"))
        self.svc.update(item["id"], {"draft": {"body": "ok"}})
        self.svc.send(item["id"])
        self.mail.replies = [{"from": "other@gmail.com", "subject": "x"}]
        self.assertEqual(self.svc.check_replies()["replied"], [])
        self.mail.replies = [{"from": "Boss <boss@gmail.com>", "subject": "x"}]
        self.assertEqual(len(self.svc.check_replies()["replied"]), 1)

    def test_search_absent_is_graceful(self):
        svc = applyq.Applyq(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, gmailx=NoSearchMail())
        self.assertEqual(svc.check_replies(), {"checked": 0, "replied": [], "available": False})

    def test_today_line(self):
        self.assertEqual(applyq.today_line(self.config), "")
        self.svc.add(self.job(60))
        self.assertIn("1 ready", applyq.today_line(self.config))
        self.config.applyq_enabled = False
        self.assertEqual(applyq.today_line(self.config), "")


class ProactiveTest(Base):
    def test_types_and_sentence(self):
        self.assertIn("applyq_ready", proactive.BATCH_TYPES)
        self.assertEqual(proactive.classify({"type": "applyq_ready"}), "batch")
        self.assertEqual(proactive.classify({"type": "applyq_daily"}), "now")
        self.assertIn("2 applications are ready", proactive.batch_sentence([{"type": "applyq_ready"}, {"type": "applyq_ready"}]))

    def test_daily_summary_once_after_evening_time(self):
        self.svc.add(self.job(70))
        late = dt.datetime(2026, 11, 9, 20, 0)
        engine = proactive.Proactive(self.ctx, clock=lambda: late)
        self.assertTrue(engine.applyq_daily())
        self.assertFalse(engine.applyq_daily())
        self.assertTrue(any("Today: 0 applied, 1 ready, 0 replies" in k.get("body", "") for _a, k in self.notes))
        early = proactive.Proactive(self.ctx, clock=lambda: dt.datetime(2026, 11, 10, 9, 0))
        self.assertFalse(early.applyq_daily())

    def test_adding_a_ready_item_announces_through_batch(self):
        engine = proactive.Proactive(self.ctx, clock=lambda: dt.datetime(2026, 11, 9, 12, 0))
        self.ctx.proactive = engine
        self.svc.add(self.job(71))
        self.assertTrue(any(e["type"] == "applyq_ready" for e in engine.view()["events"]))


class RoutesTest(Base):
    def test_routes_require_login(self):
        app = FastAPI()
        app.include_router(applyq.make_router(self.ctx))
        c = TestClient(app)
        for method, path in (("get", "/api/applyq"), ("get", "/api/applyq/summary"), ("post", "/api/applyq/manual"),
                             ("post", "/api/applyq/abc/send"), ("patch", "/api/applyq/abc"), ("post", "/api/applyq/abc/applied")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, path)

    def test_manual_edit_tabs_and_errors(self):
        c = self.client()
        self.assertEqual(c.post("/api/applyq/manual", json={"text": "  "}).status_code, 400)
        r = c.post("/api/applyq/manual", json={"text": "https://jobs.example.test/m1\nPython gig, mail jobs@m.test"}).json()
        self.assertTrue(r["created"])
        i = r["item"]["id"]
        self.assertEqual(r["item"]["source"], "manual")
        self.assertFalse(c.post("/api/applyq/manual", json={"text": "https://jobs.example.test/m1"}).json()["created"])
        t = c.post("/api/applyq/manual", json={"text": "Plain text posting for a Rust dev"}).json()
        self.assertTrue(t["item"]["url"].startswith("manual:"))
        self.assertEqual(c.patch(f"/api/applyq/{i}", json={"contact_email": "nope"}).status_code, 400)
        self.assertEqual(c.patch(f"/api/applyq/{i}", json={"channel": "fax"}).status_code, 400)
        r = c.patch(f"/api/applyq/{i}", json={"account": "main", "draft": {"subject": "S", "body": "B"}}).json()
        self.assertEqual((r["account"], r["draft"]["subject"]), ("main", "S"))
        self.assertEqual(c.post(f"/api/applyq/{i}/skip").json()["state"], "skipped")
        self.assertEqual([x["id"] for x in c.get("/api/applyq?tab=skipped").json()["items"]], [i])
        self.assertEqual(c.post(f"/api/applyq/{i}/restore").json()["state"], "ready")
        self.assertEqual(c.get("/api/applyq?tab=bogus").status_code, 400)
        self.assertEqual(c.get("/api/applyq/zzz").status_code, 404)
        self.assertEqual(c.get("/api/applyq/accounts").json()["accounts"][0]["id"], "main")
        self.assertEqual(c.delete(f"/api/applyq/{i}").status_code, 200)

    def test_switched_off_is_404(self):
        self.config.applyq_enabled = False
        self.assertEqual(self.client().get("/api/applyq/summary").status_code, 404)

    def test_accounts_without_gmail(self):
        app = FastAPI()
        with mock.patch.object(applyq, "load_gmailx", return_value=None):
            app.include_router(applyq.make_router(self.ctx))
            c = TestClient(app, cookies={"hub_session": "yes"})
            self.assertEqual(c.get("/api/applyq/accounts").json(), {"accounts": [], "gmail": False})


class ToolTest(Base):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant()),
                              audit_path=self.root / "audit.log")
        self.tools = applyq_tools.build(lambda: self.app, self.config)

    def tool(self, name):
        return next(t for t in self.tools if t.spec["name"] == name)

    def run_tool(self, name, **args):
        return asyncio.run(self.tool(name).run(args))

    def test_specs_levels_and_approval(self):
        names = {t.spec["name"] for t in self.tools}
        self.assertEqual(names, {"list_ready_applications", "show_application", "draft_application", "mark_applied",
                                 "skip_application", "send_application"})
        send = self.tool("send_application")
        self.assertTrue(send.needs_approval({"id": "x"}))
        self.assertEqual(send.declared_level({"id": "x"}), "N2")
        self.assertEqual(safety.classify("send_application", {"id": "x"}, base="N2"), "N2")      # voice may approve N2
        self.assertEqual(send.spec["input_schema"]["required"], ["id"])
        for n in names - {"send_application"}:
            self.assertFalse(self.tool(n).needs_approval({}), n)

    def test_routing_group(self):
        names, pattern = TOOL_GROUPS["applyq"]
        self.assertIn("send_application", names)
        for text in ("any applications ready", "send the application to Acme", "did I apply today"):
            self.assertRegex(text, pattern)
        specs = [t.spec for t in self.tools] + [{"name": "list_dir", "description": "d", "input_schema": {}}]
        picked = {s["name"] for s in route_local_tools(specs, [{"role": "user", "content": "what applications are ready"}])}
        self.assertIn("list_ready_applications", picked)

    def test_send_needs_a_specific_item(self):
        ok, text = self.run_tool("send_application")
        self.assertFalse(ok)
        self.assertIn("which application", text)
        ok, text = asyncio.run(self.tool("send_application").preview({}))
        self.assertFalse(ok)

    def test_preview_then_send_through_the_app(self):
        with mock.patch.object(applyq, "load_gmailx", return_value=self.mail), \
                mock.patch.object(applyq, "load_profile", return_value=self.profile):
            item = applyq.add_item(self.ctx, self.job(80, description="Mail hiring@acme.test."))
            ok, info = asyncio.run(self.tool("send_application").preview({"id": item["id"]}))
            self.assertTrue(ok, info)
            self.assertEqual(info["to"], "hiring@acme.test")
            self.assertEqual(self.mail.sent, [])                                 # preview never sends
            ok, text = self.run_tool("list_ready_applications")
            self.assertTrue(ok)
            self.assertIn("Python API developer", text)
            ok, text = self.run_tool("show_application", id=item["id"])
            self.assertIn("hiring@acme.test", text)
            self.assertEqual(self.mail.sent, [])
            ok, text = self.run_tool("send_application", id=item["id"])
            self.assertTrue(ok, text)
            self.assertEqual(len(self.mail.sent), 1)
            ok, text = self.run_tool("mark_applied", id=item["id"])
            self.assertTrue(ok)
            ok, text = self.run_tool("skip_application", id="nothere")
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
