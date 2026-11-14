"""One push per message: Watch (hub/watch.py) and Alerts (hub/alerts.py) coordinate through hub/notifydedupe.py.
Fake clock, fake notifier, no network."""
import unittest

from hub import alerts, notifydedupe
from tests.test_watch import Base

APP = "com.google.android.apps.dynamite"


class RegistryTest(unittest.TestCase):
    def test_key_is_normalised_and_source_aliases_match(self):
        a = notifydedupe.key_of("chat", " Sam ", "Prod is DOWN!!")
        b = notifydedupe.key_of("gchat", "sam", "prod   is down")
        self.assertEqual(a, b)
        self.assertNotEqual(a, notifydedupe.key_of("chat", "sam", "prod is up"))
        self.assertEqual(notifydedupe.key_of("mail", "a", "x"), notifydedupe.key_of("gmail", "a", "x"))

    def test_first_claim_wins_and_window_expires(self):
        now = [1000.0]
        reg = notifydedupe.Registry(lambda: now[0])
        self.assertEqual(reg.claim("chat", "sam", "hi", "alerts"), "alerts")
        self.assertEqual(reg.claim("chat", "sam", "hi", "watch"), "alerts")
        self.assertEqual(reg.held_by("gchat", "Sam", "HI"), "alerts")
        now[0] += notifydedupe.WINDOW_SECONDS - 1
        self.assertEqual(reg.held_by("chat", "sam", "hi"), "alerts")
        now[0] += 2
        self.assertEqual(reg.held_by("chat", "sam", "hi"), "")
        self.assertEqual(reg.claim("chat", "sam", "hi", "watch"), "watch")


class BothPipelines(Base):
    def setUp(self):
        super().setUp()
        self.client = self.build(chat_monitor=True)
        self.engine_.create_rule({"name": "Prod", "source": "chat", "match": {"keywords": ["prod"]}, "level": "warn"})
        self.alerts = alerts.Alerts(self.ctx, clock=self.clock, speaker=lambda t: None, spawn=lambda fn: fn())
        self.ctx.alerts = self.alerts

    def post(self, text, sender="Sam"):
        r = self.client.post("/api/watch/ingest", json={"source": "chat", "app": APP, "title": "Ops space",
                                                        "sender": sender, "text": text})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def pushes(self, source):
        return [n for n in self.notes if n["source"] == source]

    def test_same_message_pushes_once_and_alert_is_still_listed(self):
        out = self.post("URGENT: prod is down, need a fix ASAP")
        self.assertTrue(out["alerted"])
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(len(self.pushes("alerts")), 1)
        self.assertEqual(self.pushes("watch"), [])
        self.assertEqual(len(self.engine_.alerts), 1)                    # Watch still records its alert
        self.assertFalse(self.engine_.alerts[0]["notified"])
        self.assertEqual(self.engine_.alerts[0]["deduped"], "alerts")

    def test_different_messages_push_twice(self):
        self.post("URGENT: prod is down, need a fix ASAP", sender="Sam")
        self.post("prod deploy finished", sender="Kim")                   # Watch rule only; Alerts raises nothing
        self.assertEqual(len(self.pushes("alerts")), 1)
        self.assertEqual(len(self.pushes("watch")), 1)

    def test_alerts_disabled_leaves_watch_unchanged(self):
        self.ctx.config.alerts_enabled = False
        self.post("URGENT: prod is down, need a fix ASAP")
        self.assertEqual(len(self.pushes("watch")), 1)
        self.assertEqual(self.pushes("alerts"), [])
        self.assertTrue(self.engine_.alerts[0]["notified"])
        self.assertNotIn("deduped", self.engine_.alerts[0])

    def test_watch_first_then_alerts_lists_but_does_not_push(self):
        reg = notifydedupe.registry_for(self.ctx)
        reg.claim("chat", "Sam", "URGENT: prod is down, need a fix ASAP", "watch")
        a = self.alerts.ingest({"source": "gchat", "app": APP, "sender": "Sam", "title": "Ops space",
                                "text": "URGENT: prod is down, need a fix ASAP"})
        self.assertIsNotNone(a)
        self.assertEqual(a["delivery"], "listed")
        self.assertEqual(a["deduped"], "watch")
        self.assertEqual(self.notes, [])
        self.assertEqual(self.alerts.get(a["id"])["deduped"], "watch")           # stored, so the Alerts screen lists it

    def test_window_expiry_lets_the_same_text_notify_again(self):
        now = [5000.0]
        self.ctx.notify_dedupe = notifydedupe.Registry(lambda: now[0])
        self.post("URGENT: prod is down, need a fix ASAP")
        self.assertEqual(len(self.pushes("alerts")), 1)
        now[0] += notifydedupe.WINDOW_SECONDS + 5
        self.clock.advance(minutes=11)                                    # both pipelines also forget it
        self.post("URGENT: prod is down, need a fix ASAP")
        self.assertEqual(len(self.pushes("alerts")), 2)
        self.assertEqual(self.pushes("watch"), [])


if __name__ == "__main__":
    unittest.main()
