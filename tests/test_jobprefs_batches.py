"""Tests for job preferences (hub/jobprefs.py), the finder hook in hub/opps.py and scheduled batches (hub/batches.py).
Fakes only: a fake Gmail module, a fake form-fill service, a scripted clock. No network, no AI, no real browser."""
import asyncio
import copy
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import applyq, batches, jobprefs, opps, safety
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from hub.formfill import FormfillError
from tests.helpers import make_config
from tests.test_autopilot import need_auth

PROFILE = {"name": "Test Person", "headline": "Backend developer", "summary": "Backend developer who builds reliable APIs.",
           "skills": ["Python"], "experience": [], "links": [], "standard_answers": {}, "resume_text": "..."}


def F(title="Dev", description="", company="Acme", **kw):
    return {"title": title, "company": company, "description": description, **kw}


class DetectionTest(unittest.TestCase):
    """Remote detection edge cases."""

    def mode(self, title, desc="", **kw):
        return jobprefs.analyze(F(title, desc, **kw))

    def test_plain_remote_and_negations(self):
        self.assertEqual(self.mode("Dev (Remote)")["mode"], "remote")
        self.assertEqual(self.mode("Dev", "Work from home, anywhere you like")["mode"], "remote")
        self.assertEqual(self.mode("Dev", "This role is not remote. Must work from the office.")["mode"], "onsite")
        self.assertEqual(self.mode("Dev", "Remote work is not available for this role")["mode"], "onsite")
        self.assertEqual(self.mode("Dev", "No remote. Salary $120k - $150k")["mode"], "onsite")
        self.assertEqual(self.mode("Dev", "On-site only in Austin.")["mode"], "onsite")

    def test_hybrid_and_remote_friendly_are_not_remote(self):
        self.assertEqual(self.mode("Dev", "Hybrid, 3 days per week in office, London")["mode"], "hybrid")
        self.assertEqual(self.mode("Dev", "Remote friendly. Berlin.")["mode"], "hybrid")

    def test_remote_words_that_mean_something_else(self):
        self.assertEqual(self.mode("Engineer", "Remote sensing engineer for satellite imagery")["mode"], "unknown")
        self.assertEqual(self.mode("Engineer", "Build remote control software for robots")["mode"], "unknown")

    def test_location_line_and_source_hint(self):
        a = self.mode("Dev", "Location: USA Only\nBuild things")
        self.assertEqual(a["mode"], "unknown")
        self.assertEqual(a["geo"]["regions"], ["US"])
        self.assertEqual(self.mode("Dev", "Build things", source_remote=True)["mode"], "remote")
        self.assertEqual(self.mode("Dev", "Location: Remote\nBuild things")["mode"], "remote")

    def test_geo_restrictions(self):
        cases = [("Remote (US). Apply.", ["US"]), ("Must be located in the United States. Fully remote.", ["US"]),
                 ("Fully remote, open to candidates in the EU.", ["EU"]), ("Remote - Europe", ["EUROPE"]),
                 ("Remote role, authorized to work in the UK", ["UK"]), ("Remote, US-only", ["US"]),
                 ("Remote. Canada residents only", ["CA"]), ("Remote, anywhere in the US", ["US"])]
        for text, regions in cases:
            with self.subTest(text=text):
                g = jobprefs.analyze(F("Dev", text))["geo"]
                self.assertTrue(g["restricted"], text)
                self.assertEqual(g["regions"], regions)

    def test_geo_not_restricted(self):
        for text in ("Remote. Not limited to the US. Work from anywhere", "Remote worldwide", "Remote. We are a US-based company hiring globally.",
                     "Remote. We are based in Germany and hire anywhere", "Join us. Remote."):
            with self.subTest(text=text):
                self.assertFalse(jobprefs.analyze(F("Dev", text))["geo"]["restricted"], text)
        self.assertTrue(jobprefs.analyze(F("Dev", "Remote, worldwide"))["geo"]["worldwide"])

    def test_pronoun_us_is_not_a_region(self):
        self.assertEqual(jobprefs.analyze(F("Dev", "Remote. Come work with us only if you like cats"))["geo"]["regions"], [])

    def test_owner_alias(self):
        g = jobprefs.detect_geo(F("Dev", "Remote, Kerala residents only"), "remote", {"Kerala": "IN"})
        self.assertEqual(g["regions"], ["IN"])

    def test_timezone_warning(self):
        self.assertTrue(jobprefs.analyze(F("Dev", "Remote. PST time zone overlap needed"))["geo"]["timezones"])

    def test_kinds_level_pay_languages(self):
        a = jobprefs.analyze(F("Senior Annotator", "Task-based AI training, fluent German required. $20 per hour", kind="llm-task"))
        self.assertIn("task", a["kinds"])
        self.assertEqual(a["level"], "senior")
        self.assertEqual(a["languages"], ["German"])
        self.assertEqual(a["pay"][0]["unit"], "hour")
        self.assertIn("contract", jobprefs.analyze(F("Dev", "6 month contract"))["kinds"])
        self.assertIn("fulltime", jobprefs.analyze(F("Dev", "Full-time permanent"))["kinds"])
        self.assertEqual(jobprefs.parse_pay("raised $5M in funding"), [])
        self.assertEqual(jobprefs.parse_pay("EUR 4,000 per month")[0]["unit"], "month")
        self.assertEqual(jobprefs.parse_pay("$120k - $150k")[0]["hi"], 150000)


class EvaluateTest(unittest.TestCase):
    def ev(self, find, prof, owner=None):
        return jobprefs.evaluate(find, jobprefs.clean_profile({"name": "P", **prof}), owner or {})

    def test_remote_only(self):
        self.assertTrue(self.ev(F("Dev (Remote)", "worldwide"), {"remote_only": True})["ok"])
        self.assertFalse(self.ev(F("Dev", "Hybrid in London"), {"remote_only": True})["ok"])
        r = self.ev(F("Dev", "Build things"), {"remote_only": True})
        self.assertFalse(r["ok"])
        self.assertIn("remote is not stated", r["fails"][0])

    def test_remote_but_us_only_honours_owner_region(self):
        find = F("Dev", "Fully remote, US only.")
        india = self.ev(find, {"remote_only": True}, {"regions": ["India"]})
        self.assertFalse(india["ok"])
        self.assertIn("outside your region", india["fails"][0])
        self.assertTrue(self.ev(find, {"remote_only": True}, {"regions": ["United States"]})["ok"])
        self.assertTrue(self.ev(find, {"remote_only": True}, {"regions": ["Canada", "US"]})["ok"])
        unknown = self.ev(find, {"remote_only": True}, {})                   # no region set: a warning, not a block
        self.assertTrue(unknown["ok"])
        self.assertTrue(any("limited to" in w for w in unknown["warns"]))
        off = self.ev(find, {"remote_only": True}, {"regions": ["India"], "honour_region": False})
        self.assertTrue(off["ok"])

    def test_region_groups(self):
        eu = F("Dev", "Remote, EU only")
        self.assertTrue(self.ev(eu, {"remote_only": True}, {"regions": ["Germany"]})["ok"])
        self.assertFalse(self.ev(eu, {"remote_only": True}, {"regions": ["UK"]})["ok"])         # UK is Europe, not the EU
        self.assertTrue(self.ev(F("Dev", "Remote, Europe"), {"remote_only": True}, {"regions": ["UK"]})["ok"])
        self.assertTrue(self.ev(F("Dev", "Remote, EMEA"), {"remote_only": True}, {"regions": ["Kenya"]})["ok"])

    def test_modes_and_locations(self):
        self.assertFalse(self.ev(F("Dev", "On-site in Berlin"), {"work_modes": ["remote", "hybrid"]})["ok"])
        self.assertTrue(self.ev(F("Dev", "Hybrid. Location: Berlin"), {"work_modes": ["hybrid"], "locations": ["Berlin"]})["ok"])
        self.assertFalse(self.ev(F("Dev", "Hybrid. Location: Madrid"), {"work_modes": ["hybrid"], "locations": ["Berlin"]})["ok"])

    def test_kind_pay_keywords_companies_level_language_source(self):
        self.assertFalse(self.ev(F("Dev", "Full-time permanent"), {"kinds": ["contract", "freelance"]})["ok"])
        self.assertTrue(self.ev(F("Dev", "Contract role"), {"kinds": ["contract"]})["ok"])
        self.assertTrue(self.ev(F("Dev", "Remote"), {"kinds": ["contract"]})["ok"])                 # type not stated: allowed, warned
        low = self.ev(F("Dev", "$20 per hour"), {"min_pay": {"amount": 50, "currency": "USD", "unit": "hour"}})
        self.assertFalse(low["ok"])
        self.assertTrue(self.ev(F("Dev", "$60 per hour"), {"min_pay": {"amount": 50, "currency": "USD", "unit": "hour"}})["ok"])
        self.assertTrue(self.ev(F("Dev", "$120k-$150k a year"), {"min_pay": {"amount": 50, "currency": "USD", "unit": "hour"}})["ok"])  # 150k/2080 = 72/h
        self.assertFalse(self.ev(F("Dev", "$60k a year"), {"min_pay": {"amount": 50, "currency": "USD", "unit": "hour"}})["ok"])
        self.assertTrue(self.ev(F("Dev", "pay not stated"), {"min_pay": {"amount": 50, "currency": "USD", "unit": "hour"}})["ok"])
        self.assertTrue(self.ev(F("Dev", "EUR 10 per hour"), {"min_pay": {"amount": 50, "currency": "USD", "unit": "hour"}})["ok"])    # other currency: warn only
        self.assertFalse(self.ev(F("Dev", "Needs Kubernetes"), {"exclude": ["kubernetes"]})["ok"])
        self.assertTrue(self.ev(F("Dev", "Needs Kubernetesque thing"), {"exclude": ["kubernetes"]})["ok"])    # whole words only
        self.assertFalse(self.ev(F("Dev", "x", company="Evil Corp Inc."), {"avoid_companies": ["evil corp"]})["ok"])
        self.assertFalse(self.ev(F("Senior Dev", "x"), {"seniority": ["junior", "mid"]})["ok"])
        self.assertTrue(self.ev(F("Developer", "x"), {"seniority": ["junior"]})["ok"])
        self.assertFalse(self.ev(F("Dev", "Fluent German required"), {"languages": ["English"]})["ok"])
        self.assertTrue(self.ev(F("Dev", "Fluent German required"), {"languages": ["English", "german"]})["ok"])
        required = self.ev(F("Dev", "Python"), {"include": ["rust"], "include_required": True})
        self.assertFalse(required["ok"])
        soft = self.ev(F("Dev", "Python and Rust"), {"include": ["rust", "python"]})
        self.assertTrue(soft["ok"])
        self.assertGreater(soft["delta"], 0)

    def test_profile_validation(self):
        for bad in ({"name": ""}, {"name": "x", "work_modes": ["moon"]}, {"name": "x", "min_pay": {"amount": "abc"}},
                    {"name": "x", "min_pay": {"amount": 5, "unit": "fortnight"}}, {"name": "x", "min_pay": {"amount": 5, "currency": "dollars"}},
                    {"name": "x", "daily_quota": -1}, {"name": "x", "remote_only": "yes"}, {"name": "x", "schedule": {"kind": "interval", "every_minutes": 30}}):
            with self.subTest(bad=bad), self.assertRaises(jobprefs.PrefError):
                jobprefs.clean_profile(bad)
        ok = jobprefs.clean_profile({"name": " Remote  AI ", "include": "A, b ,a", "languages": ["english"], "min_pay": {"amount": "50", "currency": "usd", "unit": "hour"}})
        self.assertEqual(ok["include"], ["a", "b"])
        self.assertEqual(ok["languages"], ["English"])
        self.assertEqual(ok["min_pay"], {"amount": 50.0, "currency": "USD", "unit": "hour"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="batches-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Chief")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits, self.notes = [], []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)), notify=lambda *a, **k: self.notes.append((a, k)))
        self.now = dt.datetime(2026, 11, 9, 8, 0)
        self.clock = lambda: self.now
        self.mail = FakeMail()
        self.profile = copy.deepcopy(PROFILE)
        self.svc = applyq.Applyq(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, gmailx=self.mail)
        self.prefs = jobprefs.engine_for(self.ctx)
        self.sent_notes = []
        self.ff = FakeFormfill(self.svc)
        self.b = batches.Batches(self.ctx, clock=self.clock, applyq=self.svc, formfill=self.ff, sleep=self.fake_sleep,
                                 notify=lambda t, b: self.sent_notes.append((t, b)), top_up=lambda n: 0)
        self.slept = []

    async def fake_sleep(self, s):
        self.slept.append(s)

    def add(self, n, desc="Remote python role.", email=True, **over):
        raw = {"title": f"Python dev {n}", "company": f"Co{n}", "url": f"https://jobs.example.test/{n}", "source": "fake", "kind": "job",
               "description": desc + (f" Apply by email to hr{n}@co{n}.test." if email else ""), "score": 80 - n, "reason": "skills match", **over}
        item, _ = self.svc.add(raw)
        self.assertEqual(item["state"], "ready", item.get("error"))
        return item


class FakeMail:
    def __init__(self):
        self.sent, self.fail = [], False

    def send_mail(self, ctx, account, to, subject, body, attachments=None):
        self.sent.append((account, to, subject))
        return {"ok": False, "error": "smtp down"} if self.fail else {"ok": True}

    def list_accounts(self, ctx):
        return [{"id": "main"}]

    def search(self, ctx, account, query):
        return []


class FakeFormfill:
    """Same surface as hub.formfill.Formfill for what batches uses; one scripted behaviour per item id."""
    gap, last_start = 0, -1e9

    def __init__(self, svc):
        self.svc, self.script, self.calls = svc, {}, []
        self.enabled_ = True

    def clock(self):
        return 0.0

    def status(self):
        return {"enabled": self.enabled_, "playwright": True, "never": ["blocked-site.test"]}

    async def call(self, fn, *a):
        return fn(*a)

    def start(self, item_id, confirm_over_cap=False):
        self.calls.append(("start", item_id))
        what = self.script.get(item_id, "ok")
        if what == "error":
            raise FormfillError("The page could not be opened or read.", 502)
        view = {"run": "r-" + item_id, "fields": [], "blockers": [], "can_submit": True, "can_next": False, "digest": "d1"}
        if what == "captcha":
            view["blockers"] = [{"kind": "captcha", "message": "A captcha is waiting."}]
        elif what == "login":
            view["blockers"] = [{"kind": "login_required", "message": "The site wants a login."}]
        elif what == "needs":
            view["fields"] = [{"status": "needs_you", "required": True, "label": "Visa status"}]
        elif what == "multistep":
            view["can_submit"], view["can_next"] = False, True
        return view

    def screenshot(self, run):
        self.calls.append(("shot", run))
        return b"png"

    def cancel(self, run):
        self.calls.append(("cancel", run))
        return {"ok": True}

    def submit(self, run, digest, confirm):
        item_id = run[2:]
        self.calls.append(("submit", item_id))
        assert confirm is True and digest == "d1"
        what = self.script.get(item_id, "ok")
        if what == "unclear":
            self.svc.formfill_result(item_id, False, "unclear")
            return {"result": "needs_check"}
        self.svc.formfill_result(item_id, True, "ok")
        return {"result": "applied"}


class SlotsAndBuildTest(Base):
    def test_slot_validation_and_crud(self):
        for bad in ({"name": ""}, {"name": "x", "time": "9am"}, {"name": "x", "time": "09:00", "count": 0}, {"name": "x", "time": "09:00", "kind": "spam"},
                    {"name": "x", "time": "09:00", "profile": "nope"}, {"name": "x"}, {"name": "x", "schedule": {"kind": "interval", "every_minutes": 30}}):
            with self.subTest(bad=bad), self.assertRaises(batches.BatchError):
                self.b.add_slot(bad)
        s = self.b.add_slot({"name": "Morning", "time": "09:00", "count": 3})
        self.assertEqual(self.b.slots()[0]["schedule"], {"kind": "daily", "time": "09:00"})
        w = self.b.update_slot(s["id"], {"time": "10:15", "days": [0, 2], "count": 4})
        self.assertEqual(w["schedule"]["kind"], "weekly")
        self.assertTrue(self.b.delete_slot(s["id"]))
        self.assertFalse(self.b.delete_slot(s["id"]))

    def test_slot_uses_profile_schedule(self):
        p = self.prefs.add({"name": "A", "schedule": {"kind": "daily", "time": "07:30"}})
        s = self.b.add_slot({"name": "From profile", "profile": p["id"]})
        self.assertIsNone(s["schedule"])
        self.assertEqual(self.b.schedule_of(s)["time"], "07:30")

    def test_build_ranks_dedupes_and_never_sends(self):
        for n in range(1, 6):
            self.add(n)
        res = self.b.build(kind="applications", count=3)
        batch = res["batch"]
        self.assertEqual([c["title"] for c in batch["items"]], ["Python dev 1", "Python dev 2", "Python dev 3"])   # best score first
        self.assertEqual(self.mail.sent, [])
        self.assertTrue(all(c["ticked"] and c["route"] == "email" for c in batch["items"]))
        again = self.b.build(kind="applications", count=5)                                   # items 1-3 are claimed by the open batch
        self.assertEqual([c["title"] for c in again["batch"]["items"]], ["Python dev 4", "Python dev 5"])
        self.assertEqual(self.b.build(kind="applications", count=5)["batch"], None)
        self.assertEqual(self.mail.sent, [])

    def test_applied_skipped_and_unfilled_are_left_out(self):
        a, b_, c = self.add(1), self.add(2), self.add(3)
        self.svc.mark(a["id"], "applied")
        self.svc.mark(b_["id"], "skipped")
        d = self.add(4, desc="Remote role, please state your expected rate.")                  # leaves a [FILL: expected rate] marker
        self.assertTrue(applyq.unresolved(d["draft"]))
        res = self.b.build(count=10)
        self.assertEqual([x["item_id"] for x in res["batch"]["items"]], [c["id"]])
        self.assertIn("need your info first", " ".join(res["batch"]["notes"]))

    def test_job_profile_is_a_hard_filter_in_the_batch(self):
        self.add(1, desc="Remote, US only.")
        self.add(2, desc="Remote worldwide.")
        self.add(3, desc="On-site in Austin.")
        self.prefs.set_owner({"regions": ["India"]})
        self.prefs.add({"name": "Remote", "remote_only": True})
        res = self.b.build(count=10)
        self.assertEqual([c["title"] for c in res["batch"]["items"]], ["Python dev 2"])
        self.assertIn("did not match your profile", " ".join(res["batch"]["notes"]))

    def test_no_profile_means_no_filter(self):
        self.add(1, desc="On-site in Austin.")
        self.assertEqual(len(self.b.build(count=5)["batch"]["items"]), 1)

    def test_profile_quota_and_daily_cap(self):
        p = self.prefs.add({"name": "Q", "daily_quota": 2})
        for n in range(1, 6):
            self.add(n, meta={"match": {"profile": p["id"]}})
        self.assertEqual(len(self.b.build(count=5, profile=p["id"])["batch"]["items"]), 2)
        self.assertIsNone(self.b.build(count=5, profile=p["id"])["batch"])                  # the quota is used up for today
        self.config.applyq_daily_cap = 1
        other = batches.Batches(self.ctx, clock=self.clock, applyq=self.svc, formfill=self.ff, top_up=lambda n: 0)
        x = self.add(9)
        self.svc.mark(x["id"], "applied")
        res = other.build(count=5)
        self.assertIsNone(res["batch"])
        self.assertIn("daily limit", res["message"])

    def test_outreach_kind_is_separate(self):
        self.add(1)
        m = self.add(2, kind="outreach", title="Message to a hiring manager")
        res = self.b.build(kind="outreach", count=5)
        self.assertEqual([c["item_id"] for c in res["batch"]["items"]], [m["id"]])
        self.assertEqual([c["title"] for c in self.b.build(kind="applications", count=5)["batch"]["items"]], ["Python dev 1"])

    def test_linkedin_and_indeed_are_manual_and_unticked(self):
        li = self.add(1, url="https://www.linkedin.com/jobs/view/123")
        ind = self.add(2, url="https://de.indeed.com/viewjob?jk=1")
        ok = self.add(3)
        self.b.set_manual_hosts(["example-boards.test", "linkedin.com"])
        self.assertEqual(self.b.manual_hosts(), ["example-boards.test", "indeed.com", "linkedin.com"])   # the fixed two stay
        res = self.b.build(count=5)
        cards = {c["item_id"]: c for c in res["batch"]["items"]}
        for i in (li, ind):
            self.assertEqual(cards[i["id"]]["route"], "manual")
            self.assertFalse(cards[i["id"]]["ticked"])
            self.assertFalse(cards[i["id"]]["sendable"])
            self.assertIn("copy", cards[i["id"]])
        self.assertEqual(cards[ok["id"]]["route"], "email")

    def test_card_fields(self):
        self.add(1, desc="Remote, worldwide. $60 per hour contract.", meta={"match": {"pay": "$60/hour", "why": ["remote"], "warns": ["x"]}})
        card = self.b.build(count=1)["batch"]["items"][0]
        for key in ("company", "title", "mode", "pay", "why", "warnings", "draft", "resume", "route", "result"):
            self.assertIn(key, card)
        self.assertEqual(card["mode"], "remote")
        self.assertEqual(card["pay"], "$60/hour")
        self.assertIn("preview", card["draft"])
        self.assertTrue(card["resume"])

    def test_edit_and_tick(self):
        self.add(1)
        self.add(2)
        batch = self.b.build(count=5)["batch"]
        first = batch["items"][0]["item_id"]
        view = self.b.edit_item(batch["id"], first, {"ticked": False})
        self.assertFalse(view["items"][0]["ticked"])
        view = self.b.edit_item(batch["id"], first, {"draft": {"subject": "New subject", "body": "Edited body"}})
        self.assertEqual(view["items"][0]["draft"]["subject"], "New subject")
        with self.assertRaises(batches.BatchError):
            self.b.edit_item(batch["id"], first, {"ticked": "yes"})


class ScheduleTest(Base):
    def setUp(self):
        super().setUp()
        for n in range(1, 8):
            self.add(n)
        self.slot = self.b.add_slot({"name": "Morning", "time": "09:00", "count": 3})

    def test_fires_once_with_one_notification(self):
        self.now = dt.datetime(2026, 11, 9, 8, 59)
        self.assertEqual(self.b.tick(), [])
        self.now = dt.datetime(2026, 11, 9, 9, 1)
        made = self.b.tick()
        self.assertEqual(len(made), 1)
        self.assertEqual(len(made[0]["items"]), 3)
        self.assertEqual(len(self.sent_notes), 1)
        self.assertEqual(self.b.tick(), [])                                     # same slot never fires twice
        self.assertEqual(len(self.sent_notes), 1)
        self.assertEqual(self.mail.sent, [])                                    # a notification, never a send

    def test_catches_up_after_sleep_once(self):
        self.now = dt.datetime(2026, 11, 11, 11, 30)                            # slept through two days of 09:00
        made = self.b.tick()
        self.assertEqual(len(made), 1)                                          # one batch, not three
        self.assertEqual(self.b.tick(), [])

    def test_too_late_is_skipped_and_recorded(self):
        self.now = dt.datetime(2026, 11, 9, 20, 0)                              # 11 hours late, window is 6
        self.assertEqual(self.b.tick(), [])
        self.assertEqual(self.sent_notes, [])
        self.assertTrue(any(a[0] and a[0][0] == "batch_slot_missed" for a in self.audits))
        self.assertEqual(self.b.tick(), [])
        self.now = dt.datetime(2026, 11, 10, 9, 5)                              # tomorrow works normally
        self.assertEqual(len(self.b.tick()), 1)

    def test_no_catchup_when_zero(self):
        self.config.batches_catchup_min = 0
        self.now = dt.datetime(2026, 11, 9, 9, 5)
        self.assertEqual(self.b.tick(), [])

    def test_panic_and_disabled_skip(self):
        self.now = dt.datetime(2026, 11, 9, 9, 1)
        self.config.panic_active = True
        self.assertEqual(self.b.tick(), [])
        self.config.panic_active = False
        self.config.batches_enabled = False
        self.assertEqual(self.b.tick(), [])
        self.assertEqual(self.sent_notes, [])
        self.config.batches_enabled = True
        self.assertEqual(len(self.b.tick()), 1)

    def test_disabled_slot_and_empty_pool_make_no_noise(self):
        self.b.update_slot(self.slot["id"], {"enabled": False})
        self.now = dt.datetime(2026, 11, 9, 9, 1)
        self.assertEqual(self.b.tick(), [])
        self.b.update_slot(self.slot["id"], {"enabled": True})
        for i in self.svc.store.list(("ready",)):
            self.svc.mark(i["id"], "skipped")
        self.now = dt.datetime(2026, 11, 10, 9, 1)
        self.assertEqual(self.b.tick(), [])
        self.assertEqual(self.sent_notes, [])

    def test_new_batch_replaces_the_unsent_old_one(self):
        self.now = dt.datetime(2026, 11, 9, 9, 1)
        first = self.b.tick()[0]
        self.now = dt.datetime(2026, 11, 10, 9, 1)
        second = self.b.tick()[0]
        self.assertEqual(self.b.get(first["id"])["state"], "expired")
        self.assertEqual(len(second["items"]), 3)
        self.assertEqual(self.b.get(second["id"])["state"], "ready")


class SendTest(Base):
    def send(self, batch, ids=None):
        self.b.prepare_send(batch["id"], ids)
        return asyncio.run(self.b.run_send(batch["id"]))

    def statuses(self, view):
        return [(c["title"], (c["result"] or {}).get("status")) for c in view["items"]]

    def test_email_batch_sends_ticked_only(self):
        for n in range(1, 4):
            self.add(n)
        batch = self.b.build(count=3)["batch"]
        off = batch["items"][1]["item_id"]
        self.b.edit_item(batch["id"], off, {"ticked": False})
        view = self.send(batch)
        self.assertEqual([m[1] for m in self.mail.sent], ["hr1@co1.test", "hr3@co3.test"])
        self.assertEqual(self.statuses(view), [("Python dev 1", "sent"), ("Python dev 2", None), ("Python dev 3", "sent")])
        self.assertIn("cannot be taken back", view["items"][0]["result"]["message"])
        self.assertEqual(self.svc.get(off)["state"], "ready")                    # un-ticked stays untouched
        self.assertEqual(view["state"], "done")

    def test_manual_items_are_never_sent(self):
        self.add(1, url="https://www.linkedin.com/jobs/view/1")
        self.add(2)
        batch = self.b.build(count=5)["batch"]
        view = self.send(batch, [c["item_id"] for c in batch["items"]])           # even if the client ticks the LinkedIn one
        self.assertEqual([m[1] for m in self.mail.sent], ["hr2@co2.test"])
        self.assertIsNone(view["items"][0]["result"])

    def test_nothing_sendable_is_refused(self):
        self.add(1, url="https://www.linkedin.com/jobs/view/1")
        batch = self.b.build(count=5)["batch"]
        with self.assertRaises(batches.BatchError):
            self.b.prepare_send(batch["id"], None)

    def test_email_error_stops_the_batch(self):
        for n in range(1, 4):
            self.add(n)
        batch = self.b.build(count=3)["batch"]
        self.mail.fail = True
        view = self.send(batch)
        self.assertEqual(len(self.mail.sent), 1)                                  # no blind retry, no second attempt
        self.assertEqual([s for _t, s in self.statuses(view)], ["failed", "not_attempted", "not_attempted"])
        self.assertEqual(view["state"], "ready")
        self.mail.fail = False
        view = self.send(batch)                                                   # a new tap retries what has no final result
        self.assertEqual([s for _t, s in self.statuses(view)], ["sent", "sent", "sent"])
        self.assertEqual(view["state"], "done")

    def test_final_results_are_not_resent(self):
        self.add(1)
        self.add(2)
        batch = self.b.build(count=2)["batch"]
        self.send(batch)
        self.assertEqual(len(self.mail.sent), 2)
        with self.assertRaises(batches.BatchError):
            self.b.prepare_send(batch["id"], None)                                # state is done
        self.assertEqual(len(self.mail.sent), 2)

    def test_forms_one_after_another_and_stop_on_captcha(self):
        items = [self.add(n, email=False) for n in range(1, 5)]
        batch = self.b.build(count=4)["batch"]
        self.assertEqual(batch["items"][0]["route"], "form")
        self.ff.script = {items[1]["id"]: "captcha"}
        view = self.send(batch)
        self.assertEqual([s for _t, s in self.statuses(view)], ["applied", "stopped", "not_attempted", "not_attempted"])
        self.assertIn("captcha", view["items"][1]["result"]["message"])
        self.assertIn(("shot", "r-" + items[0]["id"]), self.ff.calls)
        self.assertNotIn(("start", items[2]["id"]), self.ff.calls)                 # never went on to the third form
        self.assertNotIn(("submit", items[1]["id"]), self.ff.calls)               # nothing submitted behind a captcha
        self.assertEqual(self.svc.get(items[0]["id"])["state"], "applied")
        self.assertEqual(self.svc.get(items[1]["id"])["state"], "ready")

    def test_form_outcomes(self):
        items = {k: self.add(n, email=False) for n, k in enumerate(["login", "needs", "multi", "unclear", "after"], 1)}
        self.ff.script = {items["login"]["id"]: "login", items["needs"]["id"]: "needs", items["multi"]["id"]: "multistep",
                          items["unclear"]["id"]: "unclear"}
        batch = self.b.build(count=5)["batch"]
        # login blocks the first one: the whole batch stops
        view = self.send(batch)
        self.assertEqual([s for _t, s in self.statuses(view)][0], "stopped")
        self.assertIn("login", view["items"][0]["result"]["message"])
        # untick it; the rest: needs-you and multi-step are skipped (no submit), unclear stops, last is not attempted
        self.b.edit_item(batch["id"], items["login"]["id"], {"ticked": False})
        view = self.send(batch)
        got = dict(self.statuses(view))
        self.assertEqual(got["Python dev 2"], "needs_you")
        self.assertEqual(got["Python dev 3"], "skipped")
        self.assertEqual(got["Python dev 4"], "needs_check")
        self.assertEqual(got["Python dev 5"], "not_attempted")
        self.assertNotIn(("submit", items["needs"]["id"]), self.ff.calls)
        self.assertNotIn(("submit", items["multi"]["id"]), self.ff.calls)
        self.assertEqual(self.svc.get(items["unclear"]["id"])["state"], "ready")
        self.assertIn("needs check", self.svc.get(items["unclear"]["id"])["flags"])
        self.assertEqual(view["state"], "ready")                                  # not finished

    def test_form_error_stops(self):
        items = [self.add(n, email=False) for n in range(1, 4)]
        self.ff.script = {items[0]["id"]: "error"}
        view = self.send(self.b.build(count=3)["batch"])
        self.assertEqual([s for _t, s in self.statuses(view)], ["stopped", "not_attempted", "not_attempted"])
        self.assertEqual(len([c for c in self.ff.calls if c[0] == "start"]), 1)

    def test_form_gap_is_honoured(self):
        self.ff.gap, self.ff.last_start = 20, 0.0
        self.add(1, email=False)
        self.send(self.b.build(count=1)["batch"])
        self.assertTrue(self.slept and self.slept[0] > 19)

    def test_form_off_makes_items_manual(self):
        self.ff.enabled_ = False
        self.add(1, email=False)
        card = self.b.build(count=1)["batch"]["items"][0]
        self.assertEqual(card["route"], "manual")
        self.assertIn("not set up", card["route_note"])

    def test_formfill_never_list_is_respected(self):
        self.add(1, email=False, url="https://blocked-site.test/job/1")
        self.assertEqual(self.b.build(count=1)["batch"]["items"][0]["route"], "manual")

    def test_daily_limit_stops_the_send(self):
        for n in range(1, 4):
            self.add(n)
        batch = self.b.build(count=3)["batch"]
        self.config.applyq_daily_cap = 2                                          # the limit was lowered after the batch was built
        view = self.send(batch)
        self.assertEqual([s for _t, s in self.statuses(view)], ["sent", "sent", "stopped"])
        self.assertEqual(len(self.mail.sent), 2)

    def test_outreach_cap(self):
        self.config.batches_outreach_cap = 1
        for n in (1, 2):
            self.add(n, kind="outreach")
        batch = self.b.build(kind="outreach", count=5)["batch"]
        self.assertEqual(len(batch["items"]), 1)                                 # trimmed at build time
        self.send(batch)
        self.assertEqual(self.b.day_stats()["messages"], 1)

    def test_panic_mid_batch_stops(self):
        for n in range(1, 4):
            self.add(n)
        batch = self.b.build(count=3)["batch"]
        self.b.prepare_send(batch["id"], None)
        self.config.panic_active = True
        view = asyncio.run(self.b.run_send(batch["id"]))
        self.assertEqual(self.mail.sent, [])
        self.assertEqual([s for _t, s in self.statuses(view)], ["not_attempted"] * 3)
        self.config.panic_active = False

    def test_counters(self):
        for n in range(1, 3):
            self.add(n)
        self.b.add_slot({"name": "Morning", "time": "09:00", "count": 2})
        slot = self.b.slots()[0]
        batch = self.b.build(count=2, slot=slot)["batch"]
        self.send(batch)
        o = self.b.overview()
        self.assertEqual(o["counters"]["applications"], 2)
        self.assertEqual(o["slots"][0]["today"], {"built": 2, "sent": 2})


class RoutesTest(Base):
    def client(self):
        app = FastAPI()
        app.include_router(batches.make_router(self.ctx, clock=self.clock, applyq=self.svc, formfill=self.ff, sleep=self.fake_sleep,
                                               notify=lambda t, b: self.sent_notes.append((t, b)), top_up=lambda n: 0))
        app.include_router(jobprefs.make_router(self.ctx))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(c.close)
        return c

    def test_jobprefs_routes(self):
        c = self.client()
        d = c.get("/api/jobprefs").json()
        self.assertEqual(d["profiles"], [])
        self.assertIn("remote", d["options"]["modes"])
        p = c.post("/api/jobprefs/profiles", json={"name": "Remote AI", "remote_only": True, "kinds": ["task"]}).json()
        self.assertTrue(p["id"])
        self.assertEqual(c.post("/api/jobprefs/profiles", json={"name": ""}).status_code, 400)
        self.assertEqual(c.put(f"/api/jobprefs/profiles/{p['id']}", json={"daily_quota": 5}).json()["daily_quota"], 5)
        self.assertEqual(c.put("/api/jobprefs/profiles/nope", json={}).status_code, 404)
        self.assertEqual(c.put("/api/jobprefs/owner", json={"regions": ["India"], "honour_region": True}).json()["owner"]["regions"], ["India"])
        chk = c.post("/api/jobprefs/check", json={"title": "Dev", "description": "Remote, US only"}).json()
        self.assertFalse(chk["profiles"][0]["ok"])
        self.assertEqual(c.post("/api/jobprefs/profiles/from-template/remote-only").status_code, 200)
        self.assertEqual(c.delete(f"/api/jobprefs/profiles/{p['id']}").status_code, 200)
        self.assertEqual(c.delete(f"/api/jobprefs/profiles/{p['id']}").status_code, 404)

    def test_batch_routes_and_send_needs_confirm(self):
        for n in (1, 2):
            self.add(n)
        c = self.client()
        s = c.post("/api/batches/slots", json={"name": "Morning", "time": "09:00", "count": 2}).json()
        self.assertEqual(c.post("/api/batches/slots", json={"name": "x"}).status_code, 400)
        built = c.post("/api/batches/build", json={"slot_id": s["id"]}).json()["batch"]
        self.assertEqual(len(built["items"]), 2)
        self.assertEqual(self.sent_notes, [])                                   # building by hand makes no notification
        bid = built["id"]
        self.assertEqual(c.post(f"/api/batches/{bid}/send", json={}).status_code, 400)
        self.assertEqual(self.mail.sent, [])
        self.assertEqual(c.patch(f"/api/batches/{bid}/items/{built['items'][0]['item_id']}", json={"ticked": False}).status_code, 200)
        self.assertEqual(c.post(f"/api/batches/{bid}/send", json={"confirm": True}).status_code, 200)
        import time
        deadline = time.monotonic() + 30        # the send runs in the app's own thread; a loaded machine can be slow
        while time.monotonic() < deadline:
            if c.get(f"/api/batches/{bid}").json()["state"] != "sending":
                break
            time.sleep(0.02)
        view = c.get(f"/api/batches/{bid}").json()
        self.assertEqual(view["state"], "done")
        self.assertEqual(len(self.mail.sent), 1)
        self.assertEqual(c.get("/api/batches").json()["counters"]["applications"], 1)
        self.assertEqual(c.post("/api/batches/xyz/send", json={"confirm": True}).status_code, 404)

    def test_panic_blocks_send(self):
        self.add(1)
        c = self.client()
        bid = c.post("/api/batches/build", json={"kind": "applications", "count": 1}).json()["batch"]["id"]
        self.config.panic_active = True
        self.assertEqual(c.post(f"/api/batches/{bid}/send", json={"confirm": True}).status_code, 423)
        self.assertEqual(c.post("/api/batches/build", json={}).status_code, 423)
        self.config.panic_active = False
        self.assertEqual(self.mail.sent, [])

    def test_settings_route(self):
        c = self.client()
        self.assertEqual(c.put("/api/batches/settings", json={"catchup_min": 99999}).status_code, 400)
        r = c.put("/api/batches/settings", json={"catchup_min": 30, "outreach_cap": 3, "manual_hosts": ["boards.test"]}).json()
        self.assertEqual(r["settings"]["catchup_min"], 30)
        self.assertEqual(self.config.batches_outreach_cap, 3)
        self.assertEqual(r["settings"]["manual_hosts"], ["boards.test"])
        self.assertEqual(c.put("/api/batches/settings", json={"manual_hosts": ["not a domain"]}).status_code, 400)


class FinderHookTest(Base):
    def setUp(self):
        super().setUp()
        self.config.opps_min_score = 0

    def engine(self):
        self.pushed = []
        return opps.Opps(self.ctx, push=lambda ctx, item: self.pushed.append(item) or True)

    def rows(self):
        return [{"title": "Python Dev", "url": "https://x.test/1", "company": "A", "description": "Remote, US only. Python."},
                {"title": "Python Dev 2", "url": "https://x.test/2", "company": "B", "description": "Remote worldwide. Python."},
                {"title": "Python Dev 3", "url": "https://x.test/3", "company": "C", "description": "On-site in Austin. Python."}]

    def test_without_profile_nothing_is_filtered(self):
        eng = self.engine()
        counts = eng.ingest(self.rows(), source_name="T", source_id="t", kind="job")
        self.assertEqual(counts["filtered"], 0)
        self.assertEqual(len(self.pushed), 3)

    def test_profile_filters_before_the_queue(self):
        self.prefs.set_owner({"regions": ["India"]})
        self.prefs.add({"name": "Remote", "remote_only": True})
        eng = self.engine()
        counts = eng.ingest(self.rows(), source_name="T", source_id="t", kind="job")
        self.assertEqual(counts["filtered"], 2)
        self.assertEqual([i["title"] for i in self.pushed], ["Python Dev 2"])
        self.assertEqual(self.pushed[0]["meta"]["match"]["mode"], "remote")
        shown = [f["title"] for f in eng.finds()]
        self.assertEqual(shown, ["Python Dev 2"])                                  # filtered finds are hidden by default
        filtered = eng.finds(status="filtered")
        self.assertEqual(len(filtered), 2)
        self.assertIn("outside your region", [f for f in filtered if f["title"] == "Python Dev"][0]["filtered_reason"])

    def test_pasted_links_are_exempt(self):
        self.prefs.add({"name": "Remote", "remote_only": True})
        eng = self.engine()
        counts = eng.ingest([self.rows()[2]], source_name="Pasted link", source_id="manual", kind="job")
        self.assertEqual(counts["filtered"], 0)
        self.assertEqual(len(self.pushed), 1)

    def test_profile_source_list_and_remote_boards(self):
        self.prefs.add({"name": "Only boards", "sources": ["wwr"], "remote_only": True})
        eng = self.engine()
        eng.ingest([{"title": "Dev", "url": "https://x.test/9", "company": "Z", "description": "Build things. Python"}], source_name="Other", source_id="other", kind="job")
        self.assertEqual(self.pushed, [])
        eng.ingest([{"title": "Dev", "url": "https://x.test/10", "company": "Z", "description": "Build things. Python"}], source_name="WWR", source_id="wwr", kind="job")
        self.assertEqual(len(self.pushed), 1)                                      # wwr is a remote-only board, so "remote" holds


class SafetyWiringTest(unittest.TestCase):
    def test_batch_send_always_asks(self):
        self.assertIn("batch_send", safety.ALWAYS_ASK)
        self.assertIn("batches", TOOL_GROUPS)
        self.assertEqual(TOOL_GROUPS["batches"][0], {"batch_list", "batch_prepare", "batch_send"})

    def test_tool_flags(self):
        tools = {t.spec["name"]: t for t in batches.build(lambda: None, make_config()[0])}
        self.assertTrue(tools["batch_send"].needs_approval({}))
        self.assertFalse(tools["batch_list"].needs_approval({}))
        self.assertTrue(tools["batch_prepare"].mutating)


if __name__ == "__main__":
    unittest.main()
