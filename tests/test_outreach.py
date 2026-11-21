"""Tests for batched personal outreach (hub/outreach.py and its parts of hub/batches.py). A fake Gmail (records what would be sent),
a fake clock, a fake sleep. Invented people at example.test addresses. Nothing real is ever sent."""
import asyncio
import datetime as dt
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import applyq, batches, outreach, replies
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

START = dt.datetime(2026, 11, 16, 10, 0)


class FakeGmail:
    def __init__(self):
        self.sent = []
        self.fail_for = set()

    def list_accounts(self, ctx):
        return [{"id": "g1"}]

    def send_mail(self, ctx, account, to, subject, body, attachments=None):
        if to in self.fail_for:
            return {"ok": False, "error": "boom"}
        self.sent.append({"to": to, "subject": subject, "body": body, "attachments": attachments})
        return {"ok": True, "error": ""}


def person(n, **kw):
    base = {"name": f"Pat Person{n}", "email": f"pat{n}@person{n}.example.test", "company": f"Company{n}", "role": "Backend Developer",
            "origin": "manual", "source_url": f"https://company{n}.example.test/team", "address_confirmed": True}
    return base | kw


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="outreach-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)))
        self.now = START
        self.gmail = FakeGmail()
        self.sleeps = []

        async def fake_sleep(s):
            self.sleeps.append(s)
        self.aq = applyq.Applyq(self.ctx, clock=lambda: self.now, gmailx=self.gmail, profile_fn=lambda c: {})
        self.bt = batches.Batches(self.ctx, clock=lambda: self.now, applyq=self.aq, sleep=fake_sleep)
        self.o = outreach.Outreach(self.ctx, clock=lambda: self.now, applyq=self.aq, batches=self.bt,
                                   facts_fn=lambda p: ("Sam Example", "Built a payments API in Python"),
                                   networking_fn=lambda: [{"id": "nc-1", "name": "Ria Contact", "company": "Acme", "role": "EM", "how_known": "a meetup"}],
                                   watch_fn=lambda: [{"label": "Globex", "url": "https://globex.example.test/careers"}],
                                   replies_fn=lambda: type("R", (), {"pending": staticmethod(lambda: [])})())
        self.ctx.outreach = self.o                       # so the batch sender finds the same opt-out list

    def people(self, n, **kw):
        return [self.o.add_person(person(i, **kw))["id"] for i in range(1, n + 1)]

    def run_send(self, bid):
        self.bt.prepare_send(bid, None)
        asyncio.run(self.bt.run_send(bid))


class AddressTest(Base):
    def test_no_guessing_and_source_needed(self):
        with self.assertRaises(outreach.OutreachError):
            self.o.add_person({"name": "Pat Guess", "company": "Acme", "origin": "manual", "source_url": "https://acme.example.test"})   # no address typed
        with self.assertRaises(outreach.OutreachError):
            self.o.add_person(person(1, address_confirmed=False))                                                                           # not confirmed
        with self.assertRaises(outreach.OutreachError):
            self.o.add_person(person(1, source_url=""))                                                                                    # manual needs a public link
        with self.assertRaises(outreach.OutreachError):
            self.o.add_person(person(1, source_url="http://insecure.example.test"))
        for bad in ("not an address", "noreply@acme.example.test", "a..b@acme.example.test"):
            with self.assertRaises(outreach.OutreachError):
                self.o.add_person(person(1, email=bad))
        self.assertEqual(self.o.overview()["people"], [])

    def test_network_contact_needs_no_link_but_still_a_typed_address(self):
        p = self.o.add_person({"name": "Ria Contact", "email": "ria@acme.example.test", "company": "Acme", "origin": "network", "address_confirmed": True,
                               "known_from": "a meetup", "contact_id": "nc-1"})
        self.assertEqual(p["origin"], "network")
        c = self.o.candidates()
        self.assertEqual(c["contacts"][0]["name"], "Ria Contact")
        self.assertEqual(c["companies"][0]["label"], "Globex")
        self.assertNotIn("email", c["contacts"][0])                                    # the picker never offers an address

    def test_duplicate_person_rejected(self):
        self.people(1)
        with self.assertRaises(outreach.OutreachError):
            self.o.add_person(person(1))


class DraftTest(Base):
    def test_draft_uses_confirmed_fact_footer_and_name(self):
        (pid,) = self.people(1)
        v = self.o.prepare([pid])
        card = v["items"][0]
        body = card["draft"]["body"]
        self.assertIn("Hello Pat,", body)
        self.assertIn("Company1", body)
        self.assertIn("Built a payments API in Python", body)
        self.assertIn("https://company1.example.test/team", body)
        self.assertIn('reply "no thanks"', body)
        self.assertTrue(body.rstrip().endswith("Sam Example"))
        self.assertLessEqual(len(body.split()), outreach.MAX_WORDS)
        self.assertEqual(card["contact_email"], "pat1@person1.example.test")
        self.assertEqual(card["route"], "email")
        self.assertIn("No file attached", card["resume"])

    def test_no_fact_no_claim(self):
        self.o.facts_fn = lambda p: ("Sam Example", "")
        (pid,) = self.people(1)
        body = self.o.prepare([pid])["items"][0]["draft"]["body"]
        self.assertNotIn("background", body)

    def test_missing_name_blocks_send(self):
        self.o.facts_fn = lambda p: ("", "")
        (pid,) = self.people(1)
        v = self.o.prepare([pid])
        self.assertIn("missing info", " ".join(v["items"][0]["warnings"]))
        self.run_send_expect_nothing(v["id"])

    def run_send_expect_nothing(self, bid):
        try:
            self.bt.prepare_send(bid, None)
            asyncio.run(self.bt.run_send(bid))
        except batches.BatchError:
            pass
        self.assertEqual(self.gmail.sent, [])

    def test_followup_draft_is_short_and_has_footer(self):
        d = outreach.build_draft({"name": "Pat Person", "company": "Acme"}, my_name="Sam", followup=True, first_sent="16 November")
        self.assertIn("16 November", d["body"])
        self.assertIn("no thanks", d["body"])
        self.assertLess(d["words"], 80)


class CapTest(Base):
    def test_batch_of_more_than_20_refused(self):
        ids = self.people(21)
        with self.assertRaises(outreach.OutreachError) as cm:
            self.o.prepare(ids)
        self.assertIn("at most 20", str(cm.exception))
        self.assertEqual(self.aq.store.list(None, 100), [])                             # nothing half-made

    def test_day_limit_follows_the_owner_cap_but_never_above_20(self):
        self.config.batches_outreach_cap = 3
        ids = self.people(5)
        with self.assertRaises(outreach.OutreachError) as cm:
            self.o.prepare(ids[:4])
        self.assertIn("Only 3", str(cm.exception))
        self.config.batches_outreach_cap = 0                                            # 0 means no owner cap, not no limit
        self.assertEqual(self.o.room()["limit"], 20)
        self.config.batches_outreach_cap = 200
        self.assertEqual(self.o.room()["limit"], 20)

    def test_cap_counts_what_was_sent_today_and_what_is_queued(self):
        self.config.batches_outreach_cap = 4
        ids = self.people(6)
        v = self.o.prepare(ids[:3])
        self.assertEqual(self.o.room()["left"], 1)
        with self.assertRaises(outreach.OutreachError):
            self.o.prepare(ids[3:5])
        self.run_send(v["id"])
        self.assertEqual(len(self.gmail.sent), 3)
        self.assertEqual(self.o.room()["left"], 1)
        with self.assertRaises(outreach.OutreachError):
            self.o.prepare(ids[3:5])
        self.now += dt.timedelta(days=1)                                                # a new day, a fresh allowance
        self.assertEqual(self.o.room()["left"], 4)

    def test_sender_stops_at_the_cap_even_if_the_batch_is_bigger(self):
        self.config.batches_outreach_cap = 2
        ids = self.people(3)
        self.config.batches_outreach_cap = 5
        v = self.o.prepare(ids)                                                         # built while the cap was 5
        self.config.batches_outreach_cap = 2                                            # the owner lowers it before sending
        self.run_send(v["id"])
        self.assertEqual(len(self.gmail.sent), 2)
        statuses = [e["result"]["status"] for e in self.bt.get(v["id"])["items"]]
        self.assertEqual(statuses.count("sent"), 2)
        self.assertEqual(statuses.count("stopped") + statuses.count("not_attempted"), 1)


class SendTest(Base):
    def test_nothing_is_sent_by_preparing(self):
        self.o.prepare(self.people(3))
        self.assertEqual(self.gmail.sent, [])

    def test_send_needs_confirm(self):
        v = self.o.prepare(self.people(2))
        for bad in (None, False, "yes", 1):
            with self.assertRaises(outreach.OutreachError):
                self.o.send(v["id"], bad)
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(self.bt.get(v["id"])["state"], "ready")

    def test_one_tap_sends_all_spaced_apart(self):
        v = self.o.prepare(self.people(3))
        out = self.o.send(v["id"], True)
        self.assertTrue(out["started"])
        for _ in range(100):
            if self.bt.get(v["id"])["state"] != "sending":
                break
            time.sleep(0.05)
        self.assertEqual([m["to"] for m in self.gmail.sent], [f"pat{i}@person{i}.example.test" for i in (1, 2, 3)])
        self.assertEqual(self.sleeps, [batches.DEFAULT_OUTREACH_GAP] * 2)               # 3 emails, 2 pauses of a few minutes
        self.assertGreaterEqual(batches.DEFAULT_OUTREACH_GAP, 120)
        self.assertEqual(self.bt.get(v["id"])["state"], "done")

    def test_no_attachment_unless_chosen(self):
        (a, b) = self.people(2)
        v1 = self.o.prepare([a])
        self.run_send(v1["id"])
        self.assertIn(self.gmail.sent[0]["attachments"], (None, []))
        (resume := self.bt.ctx.config.data_path() / "formfill_resume.pdf").write_bytes(b"%PDF-1.4 fake")
        v2 = self.o.prepare([b], attach_resume=True)
        self.assertIn("resume", v2["items"][0]["resume"].lower())
        self.run_send(v2["id"])
        self.assertEqual(self.gmail.sent[1]["attachments"], [str(resume)])

    def test_panic_stops_the_batch(self):
        v = self.o.prepare(self.people(2))
        self.bt.prepare_send(v["id"], None)
        self.bt.abort = True
        asyncio.run(self.bt.run_send(v["id"]))
        self.assertEqual(self.gmail.sent, [])

    def test_discard_frees_the_people_and_the_queue(self):
        ids = self.people(2)
        v = self.o.prepare(ids)
        self.o.discard_batch(v["id"])
        self.assertEqual(self.o.room()["queued"], 0)
        self.assertEqual(self.gmail.sent, [])
        v2 = self.o.prepare(ids)                                                        # first email still allowed
        self.assertEqual(len(v2["items"]), 2)

    def test_first_email_only_once(self):
        ids = self.people(1)
        v = self.o.prepare(ids)
        self.run_send(v["id"])
        self.o.sync()
        with self.assertRaises(outreach.OutreachError):
            self.o.prepare(ids)


class OptOutTest(Base):
    def test_opted_out_never_drafted_or_added(self):
        ids = self.people(2)
        self.o.add_optout(person_id=ids[0], why="marked by you")
        with self.assertRaises(outreach.OutreachError) as cm:
            self.o.prepare(ids)
        self.assertIn("opt-out", str(cm.exception))
        with self.assertRaises(outreach.OutreachError):
            self.o.add_person(person(1, email="pat1@person1.example.test", name="Again"))
        self.assertEqual(len(self.o.prepare([ids[1]])["items"]), 1)

    def test_opt_out_added_after_batch_is_built_still_blocks_the_send(self):
        ids = self.people(3)
        v = self.o.prepare(ids)
        self.o.add_optout(address="pat2@person2.example.test", name="Pat Person2")
        self.run_send(v["id"])
        self.assertEqual([m["to"] for m in self.gmail.sent], ["pat1@person1.example.test", "pat3@person3.example.test"])
        res = {e["item_id"]: e["result"] for e in self.bt.get(v["id"])["items"]}
        skipped = [r for r in res.values() if r["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("opt-out", skipped[0]["message"])

    def test_remove_from_list_is_the_owners_choice_and_listed(self):
        ids = self.people(1)
        row = self.o.add_optout(person_id=ids[0])
        self.assertTrue(self.o.is_opted_out("PAT1@person1.example.test"))                # case does not matter
        self.assertEqual(self.o.overview()["optout"][0]["address"], "pat1@person1.example.test")
        self.o.remove_optout(row["id"])
        self.assertFalse(self.o.is_opted_out("pat1@person1.example.test"))

    def test_send_block_covers_any_outreach_item(self):
        self.o.add_optout(address="x@other.example.test")
        self.assertIn("opt-out", outreach.send_block(self.ctx, {"contact_email": "x@other.example.test"}))
        self.assertEqual(outreach.send_block(self.ctx, {"contact_email": "y@other.example.test"}), "")

    def test_reply_sorting_suggests_and_owner_confirms_optout(self):
        ids = self.people(1)
        v = self.o.prepare(ids)
        self.run_send(v["id"])
        self.o.sync()
        self.assertEqual(self.o.sent_addresses(), {"pat1@person1.example.test": "Pat Person1"})

        class Mail:
            def list_accounts(self, *a):
                return [{"id": "g1", "connected": True}]

            def recent(self, account, hours=24, max_results=30):
                return [{"id": "m1", "account": "g1", "sender": "Pat Person1 <pat1@person1.example.test>", "subject": "Re: A question about Backend Developer at Company1",
                         "snippet": "No thanks", "date": "2026-11-17", "thread": ""}]

            def get_message(self, account, mid, destination="local"):
                return {"id": mid, "account": account, "body": "No thanks, not interested. Please remove me.", **self.recent(account)[0]}

        class JT:
            def _data(self):
                return {"records": []}
        self.now += dt.timedelta(days=1)
        r = replies.Replies(self.ctx, clock=lambda: self.now, mail=Mail(), jobtrack=JT(), outreach=self.o)
        r.poll()
        s = r.pending()[0]
        self.assertEqual((s["kind"], s["action"]), ("opt_out", "optout"))
        self.assertFalse(self.o.is_opted_out("pat1@person1.example.test"))              # a suggestion only
        r.confirm(s["id"])
        self.assertTrue(self.o.is_opted_out("pat1@person1.example.test"))


class FollowupTest(Base):
    def sent_batch(self, n=1):
        ids = self.people(n)
        v = self.o.prepare(ids)
        self.run_send(v["id"])
        return ids

    def test_due_after_five_days_not_before(self):
        self.sent_batch()
        self.assertEqual(self.o.followups_due(), [])
        self.now += dt.timedelta(days=4)
        self.assertEqual(self.o.followups_due(), [])
        self.now += dt.timedelta(days=1)
        self.assertEqual(len(self.o.followups_due()), 1)

    def test_configurable_days(self):
        self.config.outreach_followup_days = 2
        self.sent_batch()
        self.now += dt.timedelta(days=2)
        self.assertEqual(len(self.o.followups_due()), 1)

    def test_followup_needs_its_own_batch_and_its_own_tap(self):
        ids = self.sent_batch()
        self.now += dt.timedelta(days=5)
        before = len(self.gmail.sent)
        v = self.o.prepare_followups(ids)
        self.assertEqual(len(self.gmail.sent), before)                                  # drafting a follow-up sends nothing
        self.assertEqual(v["slot_name"], "Follow-ups")
        self.assertIn("one short follow-up", v["items"][0]["draft"]["body"])
        self.assertIn("no thanks", v["items"][0]["draft"]["body"])
        with self.assertRaises(outreach.OutreachError):
            self.o.send(v["id"], None)
        self.assertEqual(len(self.gmail.sent), before)
        self.run_send(v["id"])
        self.assertEqual(len(self.gmail.sent), before + 1)

    def test_only_one_followup_and_not_after_a_reply(self):
        ids = self.sent_batch(2)
        self.now += dt.timedelta(days=6)
        self.o.mark_replied("pat2@person2.example.test")
        due = [d["id"] for d in self.o.followups_due()]
        self.assertEqual(due, [ids[0]])
        v = self.o.prepare_followups([ids[0]])
        self.run_send(v["id"])
        self.o.sync()
        self.now += dt.timedelta(days=10)
        self.assertEqual(self.o.followups_due(), [])
        with self.assertRaises(outreach.OutreachError):
            self.o.prepare_followups([ids[0]])

    def test_not_due_person_cannot_be_followed_up_early(self):
        ids = self.sent_batch()
        with self.assertRaises(outreach.OutreachError):
            self.o.prepare_followups(ids)

    def test_opted_out_gets_no_followup(self):
        ids = self.sent_batch()
        self.now += dt.timedelta(days=6)
        self.o.add_optout(person_id=ids[0])
        self.assertEqual(self.o.followups_due(), [])

    def test_person_with_a_pending_reply_is_not_chased(self):
        self.sent_batch()
        self.now += dt.timedelta(days=6)
        self.o.replies_fn = lambda: type("R", (), {"pending": staticmethod(lambda: [{"address": "pat1@person1.example.test"}])})()
        self.assertEqual(self.o.followups_due(), [])


class RouteTest(Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(outreach.make_router(self.ctx, clock=lambda: self.now, applyq=self.aq, batches=self.bt, facts_fn=lambda p: ("Sam Example", ""),
                                                networking_fn=lambda: [], watch_fn=lambda: [], replies_fn=lambda: type("R", (), {"pending": staticmethod(lambda: [])})()))
        self.c = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)

    def test_auth_and_flow(self):
        self.assertEqual(self.anon.get("/api/outreach").status_code, 401)
        p = self.c.post("/api/outreach/people", json=person(1)).json()
        self.assertEqual(p["state"], "not written to yet")
        self.assertNotEqual(self.c.post("/api/outreach/people", json=person(2, address_confirmed=False)).status_code, 200)
        v = self.c.post("/api/outreach/prepare", json={"person_ids": [p["id"]]}).json()
        self.assertEqual(len(v["items"]), 1)
        self.assertEqual(self.c.post("/api/outreach/prepare", json={"person_ids": "x"}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/outreach/batches/{v['id']}/send", json={}).status_code, 400)
        self.assertEqual(self.gmail.sent, [])
        o = self.c.get("/api/outreach").json()
        self.assertEqual(o["room"]["left"], 9)                                       # default owner cap 10, one waiting
        self.assertEqual(o["settings"]["hard_batch"], 20)
        self.assertEqual(self.c.post(f"/api/outreach/batches/{v['id']}/discard").status_code, 200)
        self.config.outreach_enabled = False
        self.assertEqual(self.c.get("/api/outreach").status_code, 404)

    def test_optout_routes(self):
        p = self.c.post("/api/outreach/people", json=person(1)).json()
        row = self.c.post("/api/outreach/optout", json={"person_id": p["id"]}).json()
        self.assertEqual(self.c.get("/api/outreach/optout").json()["optout"][0]["id"], row["id"])
        self.assertEqual(self.c.post("/api/outreach/prepare", json={"person_ids": [p["id"]]}).status_code, 400)
        self.assertEqual(self.c.delete(f"/api/outreach/optout/{row['id']}").status_code, 200)


class SettingsTest(unittest.TestCase):
    """The new keys are accepted, validated and saved by the Settings route."""

    def setUp(self):
        import httpx
        from hub import auth, config as cfg, settings
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config_path = self.root / "hub.json"
        cfg.save(self.config, self.config_path)
        app = FastAPI()
        ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, audit=lambda *a, **k: None, get_app=lambda: app,
                  need_auth=lambda: None, config_path=self.config_path)
        app.include_router(settings.make_router(ctx, transport=httpx.MockTransport(lambda r: httpx.Response(404))))
        self.client = TestClient(app, base_url="http://testserver")
        self.client.cookies.set(auth.COOKIE, auth.make_session(self.config.session_secret))

    def test_valid_and_invalid_values(self):
        ok = self.client.patch("/api/settings", json={"outreach_followup_days": 7, "outreach_gap_seconds": 240, "outreach_enabled": False,
                                                      "replies_enabled": False, "replies_ai": True, "jobinsights_enabled": False})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((self.config.outreach_followup_days, self.config.outreach_gap_seconds), (7, 240))
        self.assertEqual((self.config.outreach_enabled, self.config.replies_enabled, self.config.replies_ai, self.config.jobinsights_enabled),
                         (False, False, True, False))
        for body in ({"outreach_followup_days": 0}, {"outreach_followup_days": 61}, {"outreach_gap_seconds": -1}, {"outreach_gap_seconds": 5000},
                     {"outreach_gap_seconds": True}, {"replies_ai": "yes"}, {"outreach_enabled": 1}):
            self.assertEqual(self.client.patch("/api/settings", json=body).status_code, 400, body)


class SourceHygieneTest(unittest.TestCase):
    def test_module_never_fetches_or_looks_up_addresses(self):
        text = Path(outreach.__file__).read_text(encoding="utf-8")
        for banned in ("import httpx", "import requests", "urlopen", "socket", "subprocess", "playwright"):
            self.assertNotIn(banned, text)

    def test_assistant_tools_are_draft_only_and_read_only(self):
        from hub.assistant import TOOL_GROUPS
        out = outreach.build(lambda: None, make_config()[0])
        self.assertEqual([t.spec["name"] for t in out], ["outreach_prepare"])
        self.assertFalse(any("send" in t.spec["name"] for t in out))
        rep = replies.build(lambda: None, make_config()[0])
        self.assertEqual([t.spec["name"] for t in rep], ["replies_review"])
        self.assertIn("outreach_prepare", TOOL_GROUPS["outreach"][0])
        self.assertIn("replies_review", TOOL_GROUPS["replies"][0])

    def test_hard_limits_are_constants_in_code(self):
        self.assertEqual((outreach.HARD_BATCH, outreach.HARD_DAY, batches.HARD_OUTREACH_BATCH, batches.HARD_OUTREACH_DAY), (20, 20, 20, 20))


if __name__ == "__main__":
    unittest.main()
