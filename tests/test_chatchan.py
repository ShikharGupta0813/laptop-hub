"""Google Chat channels for a personal account (hub/chatchan.py): parsing of notifications and Chat emails, mute / mention-only,
the 6 hour de-dupe across both ways in, per-conversation drafts, the copy-and-open hand-over, the laptop clipboard path, the
experimental web draft (fake browser, never sends), routes, assistant tools. Fakes only: no network, no browser, no clipboard."""
import asyncio
import datetime as dt
import inspect
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import alerts, chatchan, safety
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import Clock, need_auth

NOON = dt.datetime(2026, 11, 9, 12, 0)
PKG = "com.google.android.apps.dynamite"


class FakeSystem:
    platform = "linux"

    def __init__(self, tools=("xclip",), code=0):
        self.tools, self.code, self.runs = set(tools), code, []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def run(self, argv, input_text=None, timeout=20, cwd=None):
        self.runs.append((argv, input_text))
        return self.code, "", ""


class FakeDriver:
    """Records every call: the web draft may only open, type_draft, ask is_open and close."""

    def __init__(self, result="typed"):
        self.calls, self.result, self.live = [], result, True

    def open(self, url, headed):
        self.calls.append(("open", url, headed))

    def type_draft(self, selector, text, timeout_ms=20000):
        self.calls.append(("type_draft", selector, text))
        return self.result

    def is_open(self):
        self.calls.append(("is_open",))
        return False

    def close(self):
        self.calls.append(("close",))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chatch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Shikhar")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock(NOON)
        self.audits = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)),
                       notify=lambda title, body="", level="info", link=None, source="": None)
        self.eng = alerts.Alerts(self.ctx, clock=self.clock, speaker=lambda t: None, spawn=lambda fn: fn())
        self.system, self.opened = FakeSystem(), []
        self.drivers = []

        def factory(profile):
            d = FakeDriver()
            self.drivers.append(d)
            return d
        self.chat = chatchan.Chatch(self.ctx, clock=self.clock, system=self.system, opener=lambda u: self.opened.append(u) or True,
                                    web_factory=factory, web_live=0.0, web_wait=5.0)
        self.app = FastAPI()
        self.app.include_router(alerts.make_router(self.ctx, engine=self.eng))
        self.ctx.chatch = self.chat
        self.app.include_router(chatchan.make_router(self.ctx, engine=self.chat))
        self.client = TestClient(self.app, cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)

    def chat_ev(self, text, title="Team space", sender="Alice", group=True, **kw):
        return {"source": "chat", "app": "Google Chat (" + PKG + ")", "title": title, "sender": sender, "text": text,
                "group": group, "conversation": "shortcut-1", **kw}

    def add_space(self, label="Team space", **settings):
        return self.chat.entries.add({"kind": "space", "label": label, "settings": settings})


class Parsing(unittest.TestCase):
    NAMES = ["Shikhar"]

    def test_notification_table(self):
        rows = [   # title, text, sender, group, expected (space, sender, dm, text, mention)
            ("Team space", "Alice: standup moved to 11", "", None, ("Team space", "Alice", False, "standup moved to 11", False)),
            ("Team space", "standup moved to 11", "Alice", True, ("Team space", "Alice", False, "standup moved to 11", False)),
            ("Team space", "Alice: @Shikhar can you review?", "", True, ("Team space", "Alice", False, "@Shikhar can you review?", True)),
            ("Alice", "are you free at 4?", "Alice", False, ("Alice", "Alice", True, "are you free at 4?", False)),
            ("Alice", "are you free at 4?", "", None, ("Alice", "Alice", True, "are you free at 4?", False)),
            ("Team space", "Bob mentioned you in a message", "Bob", True, ("Team space", "Bob", False, "Bob mentioned you in a message", True)),
            ("Ops space", "URGENT: prod is down", "Sam", None, ("Ops space", "Sam", False, "URGENT: prod is down", False)),
            ("Sam", "URGENT: prod is down", "", None, ("Sam", "Sam", True, "URGENT: prod is down", False)),
            ("Alice", "meet at 10:30 sharp", "", None, ("Alice", "Alice", True, "meet at 10:30 sharp", False)),   # no 'Name: ' prefix: a DM
        ]
        for title, text, sender, group, (space, who, dm, body, mention) in rows:
            with self.subTest(text=text):
                p = chatchan.parse_notification({"title": title, "text": text, "sender": sender, "group": group}, self.NAMES)
                self.assertEqual((p["space"], p["sender"], p["dm"], p["text"], p["mention"]), (space, who, dm, body, mention))

    def test_broadcast_is_not_a_personal_mention(self):
        p = chatchan.parse_notification({"title": "Team", "text": "Alice: @all lunch is here", "group": True}, self.NAMES)
        self.assertTrue(p["broadcast"])
        self.assertFalse(p["mention"])

    def test_topic_split_and_per_notification_conversation_id_is_not_a_thread(self):
        p = chatchan.parse_notification({"title": "Team space › Release plan", "text": "Alice: ok", "group": True,
                                         "conversation": PKG + ":tag:7"}, self.NAMES)
        self.assertEqual((p["space"], p["topic"], p["conversation"]), ("Team space", "Release plan", ""))
        q = chatchan.parse_notification({"title": "Team", "text": "ok", "sender": "Al", "group": True, "conversation": "shortcut-9"}, self.NAMES)
        self.assertEqual(q["conversation"], "shortcut-9")

    def test_space_keys_differ_for_dm_and_space(self):
        a = chatchan.parse_notification({"title": "Alice", "text": "hi", "group": False}, [])
        b = chatchan.parse_notification({"title": "Alice", "text": "Bob: hi", "group": True}, [])
        self.assertTrue(a["space_key"].startswith("dm:"))
        self.assertTrue(b["space_key"].startswith("space:"))
        self.assertNotEqual(a["space_key"], b["space_key"])

    def test_email_shapes(self):
        rows = [   # subject, body, expected (space, sender, dm, mention)
            ("Alice mentioned you in Team space", "Alice: @Shikhar please look\nReply in Google Chat", ("Team space", "Alice", False, True)),
            ("Bob sent a message in Design", "Bob: new mock is up", ("Design", "Bob", False, False)),
            ("Carol sent you a message", "Carol: lunch?", ("Carol", "Carol", True, False)),
            ("New messages in Ops", "Dan: deploy done\nOpen Chat", ("Ops", "Dan", False, False)),
            ("[Google Chat] Erin replied in Planning - Google Chat", "Erin: agreed", ("Planning", "Erin", False, False)),
        ]
        for subject, body, (space, who, dm, mention) in rows:
            with self.subTest(subject=subject):
                p = chatchan.parse_email("Google Chat <chat-noreply@google.com>", subject, body, self.NAMES)
                self.assertIsNotNone(p)
                self.assertEqual((p["space"], p["sender"], p["dm"], p["mention"], p["via"]), (space, who, dm, mention, "email"))

    def test_gmail_notification_preview_joins_subject_and_snippet(self):
        p = chatchan.parse_email("Google Chat", "", "Bob sent a message in Design - Bob: new mock is up", [])
        self.assertEqual((p["space"], p["sender"], p["text"]), ("Design", "Bob", "new mock is up"))

    def test_not_a_chat_email(self):
        self.assertIsNone(chatchan.parse_email("Shop <a@b.com>", "Your order has shipped", "Track it here", []))

    def test_chat_url_allow_list(self):
        for url, ok in (("https://chat.google.com/room/AAA", True), ("https://chat.google.com/", True),
                        ("https://mail.google.com/chat/u/0/#chat/space/A", True), ("https://mail.google.com/mail/u/0/", False),
                        ("https://mail.google.com/chatter", False), ("http://chat.google.com/room/A", False),
                        ("https://chat.google.com.evil.example/room", False), ("https://u:p@chat.google.com/room", False),
                        ("javascript:alert(1)", False), ("", False), (None, False)):
            with self.subTest(url=url):
                self.assertEqual(chatchan.chat_url_ok(url), ok)

    def test_android_intent_falls_back_to_the_web_page(self):
        link = chatchan.android_intent("https://chat.google.com/room/AAA?x=1")
        self.assertTrue(link.startswith("intent://chat.google.com/room/AAA?x=1#Intent;scheme=https;package=" + PKG))
        self.assertIn("S.browser_fallback_url=https%3A%2F%2Fchat.google.com%2Froom%2FAAA%3Fx%3D1", link)


class Intake(Base):
    def test_chat_notification_becomes_alert_with_space_sender_and_pending_draft(self):
        a = self.eng.ingest(self.chat_ev("@Shikhar can you review the plan today?"))
        self.assertEqual(a["source"], "gchat")
        self.assertEqual(a["sender"], "Alice")
        self.assertTrue(a["mention"])
        self.assertEqual(a["chat"]["space"], "Team space")
        self.assertFalse(a["chat"]["dm"])
        item = self.chat.get(a["chat_pending"])
        self.assertEqual((item["space"], item["sender"], item["status"]), ("Team space", "Alice", "new"))
        self.assertTrue(item["draft"].strip())               # an auto draft sits in its own slot
        self.assertEqual(item["draft_by"], "auto")

    def test_dm_is_a_dm(self):
        a = self.eng.ingest(self.chat_ev("can you send me the report?", title="Ravi", sender="Ravi", group=False))
        self.assertTrue(a["chat"]["dm"])
        self.assertEqual(self.chat.get(a["chat_pending"])["sender"], "Ravi")

    def test_same_message_by_notification_and_email_is_one_alert_even_after_an_hour(self):
        first = self.eng.ingest(self.chat_ev("Can you share the notes on the outage?"))
        self.assertIsNotNone(first)
        self.clock.advance(hours=1)                                  # past the alerts engine's own 10 minute window
        again = self.eng.ingest({"source": "email", "app": "email-poll", "sender": "Google Chat <chat-noreply@google.com>",
                                 "title": "Alice sent a message in Team space",
                                 "text": "Alice: Can you share the notes on the outage?", "mail_id": "m1"})
        self.assertIsNone(again)
        self.assertEqual(len(self.chat.listing()["items"]), 1)

    def test_a_repeat_the_same_way_is_left_to_the_alerts_engine(self):
        self.assertIsNotNone(self.eng.ingest(self.chat_ev("Can you share the notes on the outage?")))
        self.assertIsNone(self.eng.ingest(self.chat_ev("Can you share the notes on the outage?")))      # within 10 minutes
        self.clock.advance(minutes=11)
        self.assertIsNotNone(self.eng.ingest(self.chat_ev("Can you share the notes on the outage?")))

    def test_chat_email_is_first_class(self):
        a = self.eng.ingest({"source": "email", "app": "email-poll", "sender": "Google Chat <chat-noreply@google.com>",
                             "title": "Bob mentioned you in Design", "text": "Bob: @Shikhar need your sign-off by EOD?", "mail_id": "m2"})
        self.assertEqual((a["source"], a["sender"], a["chat"]["space"], a["chat"]["via"]), ("gchat", "Bob", "Design", "email"))
        self.assertTrue(a["mention"])

    def test_email_parse_switch_and_other_mail_untouched(self):
        self.config.chatch_email_parse = False
        a = self.eng.ingest({"source": "email", "app": "email-poll", "sender": "Google Chat <chat-noreply@google.com>",
                             "title": "Bob mentioned you in Design", "text": "Bob: @Shikhar please look?", "mail_id": "m3"})
        self.assertEqual(a["source"], "email")
        self.assertNotIn("chat", a)
        self.config.chatch_email_parse = True
        b = self.eng.ingest({"source": "email", "app": "email-poll", "sender": "Boss <boss@example.com>",
                             "title": "Can you send the report?", "text": "by EOD please", "mail_id": "m4"})
        self.assertEqual(b["source"], "email")

    def test_muted_space_and_mention_only(self):
        e = self.add_space("Random", match=["random"])
        self.chat.entries.update(e["id"], {"enabled": False})
        self.assertIsNone(self.eng.ingest(self.chat_ev("Can you look at this today?", title="Random")))
        self.chat.entries.update(e["id"], {"enabled": True, "settings": {"mention_only": True}})
        self.assertIsNone(self.eng.ingest(self.chat_ev("Can you look at this today please?", title="Random")))
        a = self.eng.ingest(self.chat_ev("@Shikhar can you look at this today?", title="Random"))
        self.assertIsNotNone(a)
        self.assertEqual(self.chat.get(a["chat_pending"])["entry_id"], e["id"])

    def test_switched_off_leaves_alerts_exactly_as_before(self):
        self.config.chatch_enabled = False
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?"))
        self.assertEqual(a["source"], "gchat")
        self.assertNotIn("chat_pending", a)
        self.assertEqual(self.chat.listing()["items"], [])

    def test_legacy_watch_hook_still_works(self):
        alerts.on_watch_ingest(self.ctx, {"source": "chat", "app": PKG, "title": "Team space", "text": "Alice: can you review this today?"})
        self.assertEqual(len(self.chat.listing()["items"]), 1)


class Replies(Base):
    def two(self):
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?", title="Plan space"))
        b = self.eng.ingest(self.chat_ev("Can you send the numbers by EOD?", title="Money space", sender="Bob"))
        return self.chat.get(a["chat_pending"]), self.chat.get(b["chat_pending"])

    def test_each_destination_has_its_own_text(self):
        one, two = self.two()
        self.chat.set_draft(one["id"], "Yes, reviewing now.")
        self.chat.set_draft(two["id"], "Numbers by 5pm.")
        self.assertEqual(self.chat.prepare(one["id"])["text"], "Yes, reviewing now.")
        self.assertEqual(self.chat.prepare(two["id"])["text"], "Numbers by 5pm.")
        self.chat.set_draft(one["id"], "Changed.")
        self.assertEqual(self.chat.get(two["id"])["draft"], "Numbers by 5pm.")        # no copy-to-all

    def test_prepare_needs_a_draft_and_marks_opened_and_audits_no_text(self):
        one, _ = self.two()
        self.chat.set_draft(one["id"], "   ")
        with self.assertRaises(ValueError):
            self.chat.prepare(one["id"])
        self.chat.set_draft(one["id"], "On it.")
        prep = self.chat.prepare(one["id"])
        self.assertEqual(self.chat.get(one["id"])["status"], "opened")
        self.assertIn("press Send yourself", prep["instructions"])
        logged = str(self.audits)
        self.assertIn("chat_reply_prepared", logged)
        self.assertNotIn("On it.", logged)

    def test_open_plan_exact_link_or_home(self):
        one, two = self.two()
        self.assertFalse(self.chat.open_plan(one)["exact"])
        self.assertEqual(self.chat.open_plan(one)["url"], "https://chat.google.com/")
        e = self.add_space("Plan space", match=["plan"], link="https://chat.google.com/room/PLAN123")
        item = self.chat.add_channel_from(one["id"])             # already matches the entry just added
        self.assertEqual(item["id"], e["id"])
        fresh = self.eng.ingest(self.chat_ev("Can you check the plan again today?", title="Plan space"))
        plan = self.chat.open_plan(self.chat.get(fresh["chat_pending"]))
        self.assertTrue(plan["exact"])
        self.assertEqual(plan["url"], "https://chat.google.com/room/PLAN123")
        self.assertTrue(plan["android_intent"].startswith("intent://chat.google.com/room/PLAN123"))
        self.assertIn("android.intent.action.SEND", plan["share_intent"])

    def test_auto_draft_replaces_only_this_item(self):
        one, two = self.two()
        self.chat.set_draft(one["id"], "mine")
        self.chat.set_draft(two["id"], "theirs")
        self.chat.auto_draft(one["id"])
        self.assertNotEqual(self.chat.get(one["id"])["draft"], "mine")
        self.assertEqual(self.chat.get(two["id"])["draft"], "theirs")

    def test_laptop_copies_via_stdin_and_opens_the_owners_browser(self):
        one, _ = self.two()
        self.chat.set_draft(one["id"], "Reply text")
        out = self.chat.open_on_laptop(one["id"])
        self.assertTrue(out["copied"] and out["opened"])
        self.assertEqual(self.system.runs[0], (["xclip", "-selection", "clipboard", "-i"], "Reply text"))   # on stdin, not in the command
        self.assertEqual(self.opened, ["https://chat.google.com/"])
        self.assertEqual(self.chat.get(one["id"])["status"], "opened")

    def test_laptop_without_a_clipboard_tool_says_so(self):
        self.chat.system = FakeSystem(tools=())
        one, _ = self.two()
        self.chat.set_draft(one["id"], "Reply text")
        out = self.chat.open_on_laptop(one["id"])
        self.assertFalse(out["copied"])
        self.assertIn("clipboard", out["note"])

    def test_laptop_never_opens_a_non_chat_address(self):
        one, _ = self.two()
        self.chat.set_draft(one["id"], "x")
        self.chat._update(one["id"], link="https://evil.example.com/room")      # even a tampered file cannot send the browser elsewhere
        out = self.chat.open_on_laptop(one["id"])
        self.assertEqual(self.opened, ["https://chat.google.com/"])
        self.assertTrue(out["opened"])


class WebDraft(Base):
    def setUp(self):
        super().setUp()
        import hub.research as research
        self._orig = research.default_resolver
        research.default_resolver = lambda host: ["93.184.216.34"]
        self.addCleanup(setattr, research, "default_resolver", self._orig)
        self.add_space("Plan space", match=["plan"], link="https://chat.google.com/room/PLAN123")
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?", title="Plan space"))
        self.pid = a["chat_pending"]
        self.chat.set_draft(self.pid, "Reviewing now.")

    def turn_on(self):
        self.chat.entries.update("web-draft", {"enabled": True})

    def test_off_by_default(self):
        self.assertFalse(next(e for e in self.chat.entries.load() if e["kind"] == "web-draft")["enabled"])
        with self.assertRaises(chatchan.ChatError) as cm:
            self.chat.web_draft(self.pid, True)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.drivers, [])

    def test_needs_confirm_a_link_and_a_draft(self):
        self.turn_on()
        with self.assertRaises(chatchan.ChatError):
            self.chat.web_draft(self.pid, None)
        self.chat.set_draft(self.pid, "")
        with self.assertRaises(chatchan.ChatError):
            self.chat.web_draft(self.pid, True)
        self.chat.set_draft(self.pid, "x")
        self.chat._update(self.pid, link="")
        with self.assertRaises(chatchan.ChatError) as cm:
            self.chat.web_draft(self.pid, True)
        self.assertIn("wrong conversation", str(cm.exception))
        self.assertEqual(self.drivers, [])

    def test_types_the_draft_and_only_that(self):
        self.turn_on()
        out = self.chat.web_draft(self.pid, True)
        self.assertEqual(out["state"], "typed")
        self.chat.web.thread.join(5)
        d = self.drivers[0]
        self.assertEqual(d.calls[0], ("open", "https://chat.google.com/room/PLAN123", True))        # always a visible window
        self.assertEqual(d.calls[1], ("type_draft", chatchan.DEFAULT_SELECTOR, "Reviewing now."))
        self.assertLessEqual({c[0] for c in d.calls}, {"open", "type_draft", "is_open", "close"})
        self.assertIn(("close",), d.calls)
        self.assertEqual(self.chat.get(self.pid)["status"], "opened")

    def test_login_wall_and_missing_box_are_reported_not_forced(self):
        self.turn_on()
        self.chat.web_factory = lambda profile: FakeDriver("needs_login")
        self.assertIn("sign in", self.chat.web_draft(self.pid, True)["note"])
        self.chat.web.thread.join(5)
        self.chat.web_factory = lambda profile: FakeDriver("not_found")
        self.assertIn("compose box", self.chat.web_draft(self.pid, True)["note"])
        self.chat.web.thread.join(5)

    def test_no_playwright_says_how_to_install(self):
        self.turn_on()
        self.chat.web_factory = None
        self.chat.web_available = lambda: False
        with self.assertRaises(chatchan.ChatError) as cm:
            self.chat.web_draft(self.pid, True)
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("playwright", str(cm.exception).lower())

    def test_real_driver_only_fills_never_presses_or_clicks(self):
        driver_src = inspect.getsource(chatchan.make_web_driver)
        for banned in (".press(", ".click(", ".keyboard", "Enter", ".dblclick(", "dispatch_event", ".submit"):
            self.assertNotIn(banned, driver_src)

        class Box:
            def __init__(s, page): s.page = page
            @property
            def last(s): return s
            def wait_for(s, state, timeout): s.page.calls.append(("wait", state))
            def fill(s, text, timeout): s.page.calls.append(("fill", text))

        class Page:
            url = "https://chat.google.com/room/A"
            def __init__(s): s.calls = []
            def locator(s, sel): s.calls.append(("locator", sel)); return Box(s)
            def is_closed(s): return False

        d = chatchan.make_web_driver(self.tmp / "profile")
        d._page = Page()
        self.assertEqual(d.type_draft('[role="textbox"]', "hello"), "typed")
        self.assertEqual([c[0] for c in d._page.calls], ["locator", "wait", "fill"])
        d._page.url = "https://accounts.google.com/signin"
        self.assertEqual(d.type_draft("x", "hello"), "needs_login")
        self.assertEqual(len(d._page.calls), 3)                     # nothing typed on the login page


class Routes(Base):
    def test_routes_require_login(self):
        anon = TestClient(self.app)
        for method, path in (("get", "/api/chatch"), ("get", "/api/chatch/pending"), ("post", "/api/chatch/parse-test"),
                             ("post", "/api/chatch/pending/abc/prepare")):
            self.assertEqual(getattr(anon, method)(path).status_code, 401, path)

    def test_snapshot_is_honest_about_every_path(self):
        d = self.client.get("/api/chatch").json()
        caps = {c["id"]: c for c in d["capabilities"]}
        self.assertFalse(caps["rest-api"]["works"])
        self.assertIn("UNVERIFIED", caps["rest-api"]["limits"])
        self.assertIn("NOT verified", caps["read-email"]["verified"])
        self.assertIn("terms", caps["web-draft"]["limits"])
        self.assertEqual([e["kind"] for e in d["entries"]], ["email-source", "web-draft"])
        self.assertFalse(d["entries"][1]["enabled"])

    def test_channel_crud_validates_links_and_words(self):
        r = self.client.post("/api/chatch", json={"kind": "space", "label": "Plan", "settings": {"match": ["plan"], "link": "https://evil.example.com/x"}})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/chatch", json={"kind": "space", "label": "Plan", "settings": {"match": ["plan"], "link": "https://chat.google.com/room/A"}})
        self.assertEqual(r.status_code, 200)
        ident = r.json()["entry"]["id"]
        self.assertEqual(self.client.put(f"/api/chatch/{ident}", json={"enabled": False}).json()["entry"]["enabled"], False)
        self.assertEqual(self.client.put("/api/chatch/order", json={"ids": [ident]}).status_code, 200)
        self.assertEqual(self.client.post("/api/chatch/reset", json={}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/chatch/{ident}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/chatch/{ident}").status_code, 404)
        bad = self.client.post("/api/chatch", json={"kind": "web-draft", "settings": {"selector": "a; drop"}})
        self.assertEqual(bad.status_code, 400)

    def test_settings_and_parse_test(self):
        self.assertEqual(self.client.post("/api/chatch/settings", json={"email_parse": "yes"}).status_code, 400)
        out = self.client.post("/api/chatch/settings", json={"email_parse": False, "enabled": True}).json()
        self.assertEqual(out, {"enabled": True, "email_parse": False})
        r = self.client.post("/api/chatch/parse-test", json={"kind": "email", "sender": "x", "subject": "Alice mentioned you in Ops", "body": "Alice: hi"})
        self.assertEqual(r.json()["parsed"]["space"], "Ops")
        r = self.client.post("/api/chatch/parse-test", json={"kind": "email", "subject": "Your invoice", "body": "pay"})
        self.assertIsNone(r.json()["parsed"])
        r = self.client.post("/api/chatch/parse-test", json={"kind": "notification", "title": "Team", "text": "Al: hi", "group": True})
        self.assertEqual(r.json()["parsed"]["sender"], "Al")

    def test_pending_flow_over_http(self):
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?"))
        pid = a["chat_pending"]
        listing = self.client.get("/api/chatch/pending").json()
        self.assertEqual(listing["open"], 1)
        self.assertEqual(listing["items"][0]["label"], "Team space")
        self.assertEqual(self.client.put(f"/api/chatch/pending/{pid}/draft", json={"text": "Looking now."}).json()["draft"], "Looking now.")
        self.assertEqual(self.client.put(f"/api/chatch/pending/{pid}/draft", json={"text": 5}).status_code, 400)
        prep = self.client.post(f"/api/chatch/pending/{pid}/prepare").json()
        self.assertEqual(prep["text"], "Looking now.")
        self.assertIn("url", prep["open"])
        self.assertEqual(self.client.post(f"/api/chatch/pending/{pid}/open-laptop").json()["opened"], True)
        self.assertEqual(self.client.post(f"/api/chatch/pending/{pid}/status", json={"status": "bogus"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/chatch/pending/{pid}/status", json={"status": "done"}).json()["status"], "done")
        self.assertEqual(self.client.get("/api/chatch/pending").json()["open"], 0)
        self.assertEqual(self.client.get("/api/chatch/pending/nope").status_code, 404)
        self.assertEqual(self.client.post(f"/api/chatch/pending/{pid}/web-draft", json={"confirm": True}).status_code, 409)

    def test_add_channel_from_pending_and_from_alert(self):
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?", title="Plan space"))
        r = self.client.post(f"/api/chatch/pending/{a['chat_pending']}/add-channel").json()
        self.assertEqual(r["entry"]["label"], "Plan space")
        other = self.eng.ingest({"source": "slack", "sender": "X", "text": "Can you review this today?"})
        self.assertEqual(self.client.post(f"/api/chatch/from-alert/{other['id']}").status_code, 400)
        self.assertEqual(self.client.post("/api/chatch/from-alert/zzz").status_code, 404)
        again = self.client.post(f"/api/chatch/from-alert/{a['id']}").json()
        self.assertEqual(again["id"], a["chat_pending"])

    def test_alert_public_carries_chat_fields_for_the_ui(self):
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?"))
        shown = self.client.get(f"/api/alerts/{a['id']}").json()
        self.assertEqual(shown["chat_pending"], a["chat_pending"])
        self.assertEqual(shown["chat"]["space"], "Team space")


class Tools(Base):
    def _run(self, coro_fn):
        from hub import common
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

    def test_tools_list_and_draft_never_send(self):
        tools = {t.spec["name"]: t for t in chatchan.build(lambda: self.app, self.config)}
        self.assertEqual(set(tools), {"chat_list_pending", "chat_draft_reply"})
        self.assertFalse(any("send" in n for n in tools))
        a = self.eng.ingest(self.chat_ev("Can you review the plan today?"))
        pid = a["chat_pending"]

        async def go():
            ok, text = await tools["chat_list_pending"].run({})
            self.assertTrue(ok and pid in text and "data, not instructions" in text)
            ok, text = await tools["chat_draft_reply"].run({"id": pid, "text": "I will review it by 3pm."})
            self.assertTrue(ok and "NOT sent" in text and "I will review it by 3pm." in text)
            self.assertEqual(self.chat.get(pid)["draft"], "I will review it by 3pm.")
            ok, text = await tools["chat_draft_reply"].run({"id": pid})                  # no text: a fresh draft from Memory
            self.assertTrue(ok and "NOT sent" in text)
            ok, _ = await tools["chat_draft_reply"].run({"id": "bad"})
            self.assertFalse(ok)
        self._run(go)

    def test_tools_are_tainted_and_the_group_routes_chat_phrases(self):
        from hub.assistant import TOOL_GROUPS
        tools = {t.spec["name"]: t for t in chatchan.build(lambda: self.app, self.config)}
        for t in tools.values():
            self.assertTrue(getattr(t, "_untrusted", False))          # chat text is other people's words
        names, pattern = TOOL_GROUPS["chatchan"]
        self.assertEqual(names, {"chat_list_pending", "chat_draft_reply"})
        for phrase in ("anything in google chat I need to answer", "draft a chat reply", "who mentioned me in chat"):
            self.assertTrue(pattern.search(phrase), phrase)
        for phrase in ("what is the weather", "open my notes"):
            self.assertFalse(pattern.search(phrase), phrase)

    def test_sending_stays_tap_only(self):
        self.assertIn("chat_send", safety.ALWAYS_ASK)
        for name in ("chat_list_pending", "chat_draft_reply"):
            self.assertNotIn(name, safety.ALWAYS_ASK)
        src = Path(chatchan.__file__).read_text(encoding="utf-8")
        for banned in ("import httpx", "import requests", "import smtplib", "chat.googleapis.com", "urlopen("):
            self.assertNotIn(banned, src, banned)


class SettingsKeys(Base):
    def test_settings_allow_list_and_view(self):
        from hub import settings
        self.assertIn("chatch_enabled", settings.ALLOWED_KEYS)
        self.assertIn("chatch_email_parse", settings.ALLOWED_KEYS)


if __name__ == "__main__":
    unittest.main()
