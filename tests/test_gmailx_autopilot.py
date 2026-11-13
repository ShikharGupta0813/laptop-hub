"""Autopilot and Today with Gmailx: account chips on cards, extra accounts as cards, company text kept off cloud AI."""
import json

from hub import gmailx
from hub.gmailx import SCOPE_READ, MemoryStore
from tests.test_autopilot import Base as APBase
from tests.test_gmailx import FakeGoogle, FakeLegacy


class GmailxAutopilot(APBase):
    def setUp(self):
        super().setUp()
        self.google, self.store = FakeGoogle(), MemoryStore()
        self.store.set(gmailx.CLIENT_KEY, json.dumps({"client_id": "client-id-123456", "client_secret": "secret-xyz"}))
        self.svc = gmailx.get_service(self.ctx, secrets_store=self.store, http=self.google, legacy=FakeLegacy(configured=False),
                                      now=lambda: self.clock.now.timestamp(), listen=False)

    def connect_company(self):
        self.google.grant("c", "me@corp.com", [SCOPE_READ])
        flow = self.svc.begin_connect("company")
        return self.svc.complete_connect(flow["state"], code="c")

    def test_one_account_keeps_working_and_cards_carry_the_personal_tag(self):
        self.hub.add_mail(7, "Priya <priya@example.com>", "Invoice", "Can you send me the invoice by Friday?")
        out = self.sweep()
        card = next(c for c in out["cards"] if c["source"] == "inbox")
        self.assertEqual(card["account"], "personal")
        self.assertEqual(card["action_kind"], "send_email")
        self.assertEqual(out["failed"], [])

    def test_company_account_mail_becomes_tagged_cards_without_any_ai_call(self):
        acct = self.connect_company()
        self.google.add_mail("me@corp.com", "m1", "Ann <ann@corp.com>", "Budget question",
                             "Can you review the budget?", "SECRET BODY", ts=int(self.clock.now.timestamp() * 1000))
        out = self.sweep()
        cards = [c for c in out["cards"] if c["account"] == "company"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["action_kind"], "none")
        self.assertIn("Budget question", cards[0]["title"])
        self.assertEqual(self.model.calls, [])                      # nothing went to any model
        self.assertNotIn("SECRET BODY", json.dumps(out))
        self.assertEqual(self.sweep()["new"], 0)                    # not duplicated on the next sweep
        self.assertTrue(acct["id"])

    def test_unreadable_company_account_is_a_note_not_a_failure(self):
        acct = self.connect_company()
        self.google.invalid_grant.add("rt-me@corp.com")
        self.clock.advance(hours=3)
        out = self.sweep()
        self.assertEqual(out["failed"], [])
        self.assertIn("company", " ".join(out["notes"].values()))
        self.assertTrue(acct)

    def test_relabelled_default_keeps_mail_text_off_a_cloud_drafter(self):
        self.svc.update("default", label="company")
        self.config.default_provider = "gemini"
        self.hub.add_mail(7, "Priya <priya@example.com>", "Invoice", "Can you send me the invoice by Friday?")
        out = self.sweep()
        card = next(c for c in out["cards"] if c["source"] == "inbox")
        self.assertEqual(card["account"], "company")
        self.assertNotIn("GET /api/inbox/messages/7", [f"{m} {p}" for m, p, _ in self.hub.calls])   # body not fetched
        self.assertNotIn("gemini", self.providers)
