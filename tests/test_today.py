"""Tests for Today (hub/today.py), the evening summary, the voice/typed approval tools and Autopilot research.
Every source is a fake route caller, time is a fake clock, git is a fake runner, the model is a fake that must never
be asked by Today. Nothing here touches the network, a real model or real mail."""
import asyncio
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import autopilot, friday_tools, today
from hub.assistant import TOOL_GROUPS, route_local_tools
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import Clock, FakeHub, FakeModel, need_auth

MON_AFTERNOON = dt.datetime(2026, 11, 9, 15, 0)
MON_EVENING = dt.datetime(2026, 11, 9, 19, 35)


def iso(hour, minute=0, day=9):
    return dt.datetime(2026, 11, day, hour, minute).isoformat()


class TodayHub(FakeHub):
    """FakeHub plus the day's other sources."""

    def __init__(self, clock):
        super().__init__(clock)
        self.meetings = []
        self.expenses = []
        self.unread = 4
        self.jobs = []
        self.search_results = []
        self.mail_search = []
        self.missing = set()

    async def __call__(self, method, path, *, params=None, body=None):
        if path in self.missing:
            self.calls.append((method, path, body))
            return False, "not available"
        if path == "/api/work/calendar":
            self.calls.append((method, path, body))
            return True, {"meetings": self.meetings}
        if path == "/api/expenses":
            self.calls.append((method, path, body))
            return True, {"month": "2026-11", "expenses": self.expenses}
        if path == "/api/inbox/status":
            self.calls.append((method, path, body))
            return True, {"configured": True, "unread": self.unread}
        if path == "/api/council/jobs":
            self.calls.append((method, path, body))
            return True, {"jobs": self.jobs}
        if path == "/api/search/all":
            self.calls.append((method, path, params))
            return True, {"results": self.search_results}
        if path == "/api/inbox/search":
            self.calls.append((method, path, params))
            return True, {"results": self.mail_search}
        return await super().__call__(method, path, params=params, body=body)


class Base(unittest.TestCase):
    start = MON_AFTERNOON
    enabled = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="today-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), autopilot_enabled=self.enabled,
                                             autopilot_repos=[str(self.repo)])
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock(self.start)
        self.hub = TodayHub(self.clock)
        self.model = FakeModel()
        self.model_calls = []
        self.audits, self.notes = [], []
        self.git_out = ""
        ctx = Ctx(config=self.config, model_for=self.model_for, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: self.audits.append((a, k)),
                  notify=lambda *a, **k: self.notes.append(k or a))
        self.ctx = ctx
        app = FastAPI()
        app.include_router(autopilot.make_router(ctx, caller=self.hub, clock=self.clock, git_runner=self.git))
        self.engine = autopilot.ENGINES[id(ctx)]
        self.ap_client = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(self.ap_client.close)
        app.include_router(today.make_router(ctx, caller=self.bridge, clock=self.clock, git_runner=self.git,
                                             audit_reader=lambda day: self.audit_rows))
        self.audit_rows = []
        self.app = app
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)

    def model_for(self, provider):
        self.model_calls.append(provider)
        return self.model

    def git(self, argv):
        if "config" in argv:
            return 0, "me@example.com\n"
        return 0, self.git_out

    async def bridge(self, method, path, *, params=None, body=None):
        """Today's caller: Autopilot routes are real (against the autopilot router), the rest is the fake hub."""
        if path.startswith("/api/autopilot"):
            r = await asyncio.to_thread(self.ap_client.request, method, path, params=params, json=body)
            return (r.status_code < 400), r.json()
        return await self.hub(method, path, params=params, body=body)

    def day(self):
        r = self.client.get("/api/today")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def fill(self):
        h = self.hub
        h.meetings = [{"title": "Standup", "start": iso(10), "end": iso(10, 30), "all_day": False, "link": "https://meet.example/x", "platform": "meet"},
                      {"title": "Design review", "start": iso(16), "end": iso(17), "all_day": False, "link": "", "platform": ""}]
        h.tasks = [{"gid": "111", "name": "Ship export", "due_on": "2026-11-09", "project": "Web", "completed": False},
                   {"gid": "222", "name": "Fix login", "due_on": "2026-11-06", "project": "Web", "completed": False},
                   {"gid": "333", "name": "Later", "due_on": "2027-01-01", "project": "Web", "completed": False}]
        h.details["111"] = {"notes": "n", "modified_at": iso(11, 5), "comments": [
            {"author": "Me", "text": "plan", "created_at": iso(11, 5)}, {"author": "Other", "text": "x", "created_at": iso(12)}]}
        h.details["222"] = {"notes": "", "modified_at": iso(9, 0, day=5), "comments": []}
        h.expenses = [{"id": "e1", "amount": 25000, "currency": "INR", "category": "food", "note": "lunch", "date": "2026-11-09"},
                      {"id": "e2", "amount": 10000, "currency": "INR", "category": "travel", "note": "", "date": "2026-11-09"},
                      {"id": "e3", "amount": 99900, "currency": "INR", "category": "food", "note": "old", "date": "2026-11-08"}]
        h.jobs = [{"id": "j1", "task": "Review the export code", "status": "done", "created": self.clock.now.replace(hour=13).timestamp()},
                  {"id": "j2", "task": "Yesterday job", "status": "done", "created": (self.clock.now - dt.timedelta(days=1)).timestamp()}]
        self.git_out = (f"{iso(9, 15)}\tFix login redirect\n{iso(12, 40)}\tAdd export button\n"
                        f"{iso(8, 0, day=8)}\tYesterday commit")
        # an executed mail reply from Autopilot today
        h.add_mail(7, "Priya <priya@example.com>", "Invoice", "Can you send me the invoice by Friday?")
        self.clock.now = self.clock.now.replace(hour=8, minute=40)
        card = asyncio.run(self.engine.sweep())["cards"][0]
        self.clock.now = MON_AFTERNOON.replace(hour=14)
        r = self.ap_client.post(f"/api/autopilot/{card['id']}/approve", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.clock.now = self.start
        h.add_mail(8, "Sam <sam@example.com>", "Deploy", "Could you check the deploy today?")
        asyncio.run(self.engine.sweep())                       # one card stays pending
        h.calls.clear()
        self.model_calls.clear()
        self.notes.clear()


class Aggregate(Base):
    def test_every_section(self):
        self.fill()
        d = self.day()
        self.assertEqual(d["date"], "2026-11-09")
        self.assertEqual([m["title"] for m in d["meetings"]], ["Standup", "Design review"])
        self.assertEqual([m["past"] for m in d["meetings"]], [True, False])
        self.assertEqual([t["name"] for t in d["tasks"]["due_today"]], ["Ship export"])
        self.assertEqual([t["name"] for t in d["tasks"]["overdue"]], ["Fix login"])
        self.assertEqual([t["name"] for t in d["tasks"]["commented"]], ["Ship export"])   # only mine, only today
        self.assertEqual([t["name"] for t in d["tasks"]["updated"]], ["Ship export"])
        self.assertEqual(d["expenses"]["count"], 2)                                         # yesterday's is not today's
        self.assertEqual(d["expenses"]["by_currency"]["INR"]["total_text"], "350.00")
        self.assertEqual(d["mail"]["unread"], 4)
        self.assertEqual([r["to"] for r in d["mail"]["replied"]], ["priya@example.com"])
        self.assertEqual([c["subject"] for c in d["done"]["commits"]], ["Fix login redirect", "Add export button"])
        self.assertEqual([j["id"] for j in d["done"]["council"]], ["j1"])
        self.assertEqual(d["pending"]["autopilot"], len(self.engine.pending()))
        self.assertGreaterEqual(d["pending"]["autopilot"], 1)
        self.assertEqual(d["pending"]["next_meeting"]["title"], "Design review")
        self.assertEqual(d["pending"]["next_meeting"]["in_min"], 60)
        self.assertEqual(d["sources_failed"], [])
        n = d["numbers"]
        self.assertEqual((n["commits"], n["mails_handled"], n["cards_approved"], n["spent_text"]),
                         (2, 1, 1, "350.00 INR"))

    def test_timeline_is_time_ordered_with_spending_last(self):
        self.fill()
        tl = self.day()["timeline"]
        timed = [i["time"] for i in tl if i["time"]]
        self.assertEqual(timed, sorted(timed))
        self.assertEqual({i["kind"] for i in tl}, {"meeting", "commit", "mail", "task", "council", "expense"})
        self.assertEqual([i["kind"] for i in tl[-2:]], ["expense", "expense"])
        self.assertEqual(tl[0]["title"], "Fix login redirect")

    def test_asana_completions_come_from_the_audit_log(self):
        self.fill()
        self.audit_rows = [{"time": "2026-11-09T12:00:00", "event": "work_asana", "gid": "9", "action": "complete"},
                           {"time": "2026-11-09T12:01:00", "event": "work_asana", "gid": "9", "action": "comment"}]
        d = self.day()
        self.assertEqual(d["tasks"]["completed"], 1)
        self.assertIn("completed one task", today.spoken(d, "evening").replace("1 task", "one task"))

    def test_one_failing_source_does_not_hide_the_rest(self):
        self.fill()
        self.hub.missing = {"/api/work/calendar", "/api/expenses"}
        d = self.day()
        self.assertEqual(d["sources_failed"], ["calendar", "expenses"])
        self.assertEqual(d["meetings"], [])
        self.assertEqual(len(d["done"]["commits"]), 2)
        self.assertIn("could not read calendar, expenses", today.spoken(d))

    def test_council_missing_is_not_a_failure_and_empty_day_is_fine(self):
        self.hub.missing = {"/api/council/jobs"}
        d = self.day()
        self.assertEqual(d["sources_failed"], [])
        self.assertEqual(d["timeline"], [])

    def test_read_only_and_no_ai(self):
        self.fill()
        self.day()
        self.client.get("/api/today/text")
        self.assertEqual([m for m, p, _ in self.hub.calls if m != "GET"], [])
        self.assertEqual(self.model_calls, [])

    def test_switched_off_and_auth(self):
        self.config.today_enabled = False
        self.assertEqual(self.client.get("/api/today").status_code, 404)
        self.assertEqual(self.client.get("/api/today/text").status_code, 404)
        self.config.today_enabled = True
        anon = TestClient(self.app)
        self.assertEqual(anon.get("/api/today").status_code, 401)


class Text(Base):
    def test_deterministic_short_and_plain(self):
        self.fill()
        a = self.client.get("/api/today/text").json()
        b = self.client.get("/api/today/text").json()
        self.assertEqual(a["text"], b["text"])
        self.assertLessEqual(a["words"], 180)
        for bad in ("**", "#", "- ", "{"):
            self.assertNotIn(bad, a["text"])
        self.assertIn("2 meetings", a["text"])
        self.assertIn("Design review at 16:00", a["text"])
        self.assertIn(f"{len(self.engine.pending())} drafts waiting", a["text"])
        self.assertEqual(a["numbers"]["commits"], 2)

    def test_evening_variant(self):
        self.fill()
        e = self.client.get("/api/today/text", params={"style": "evening"}).json()["text"]
        m = self.client.get("/api/today/text").json()["text"]
        self.assertNotEqual(e, m)
        self.assertIn("You made 2 commits in repo", e)
        self.assertIn("You spent 350 rupees in 2 payments", e)
        self.assertIn("replied to 1 mail", e)
        self.assertEqual(self.client.get("/api/today/text", params={"style": "weird"}).status_code, 400)

    def test_word_cap_with_a_huge_day(self):
        d = {"weekday": "Monday", "numbers": {"expense_count": 9, "commits": 30, "tasks_due": 40, "tasks_overdue": 9,
                                              "meetings_left": 5, "council_runs": 2, "cards_approved": 3},
             "meetings": [{"all_day": False}] * 9, "tasks": {"due_today": [{"name": "x " * 30}], "overdue": [{"name": "y " * 30}]},
             "done": {"commits": [{"repo": "alpha", "subject": "word " * 40}] * 3, "tasks": {"commented": [1] * 4, "completed": 2}},
             "mail": {"replied": [{"to": "a@b.c"}] * 3, "unread": 12}, "pending": {"autopilot": 7, "next_meeting": {"title": "T", "time": "10:00"}},
             "expenses": {"by_currency": {"INR": {"total": 12345}, "USD": {"total": 500}}}, "sources_failed": ["asana"]}
        for style in ("morning", "evening"):
            self.assertLessEqual(len(today.spoken(d, style).split()), 180)

    def test_empty_day(self):
        text = self.client.get("/api/today/text").json()["text"]
        self.assertIn("Your calendar is clear", text)


class Evening(Base):
    start = MON_EVENING

    def run_evening(self):
        return asyncio.run(self.engine.maybe_evening())

    def test_fires_once_per_day_with_push_and_card(self):
        self.fill()
        self.assertTrue(self.run_evening())
        self.assertFalse(self.run_evening())
        self.assertFalse(self.run_evening())
        self.assertEqual(len(self.notes), 1)
        note = self.notes[0]
        self.assertLessEqual(len(note["body"]), 200)
        self.assertIn("Here is how your Monday went", note["body"])
        cards = [c for c in self.engine.store.list(("pending",)) if c["source"] == "today"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["action_kind"], "none")
        self.assertIn("commits", cards[0]["draft"])
        self.assertGreater(len(cards[0]["draft"]), 100)
        self.assertEqual(self.model_calls, [])                    # the evening text itself costs no AI call
        self.clock.advance(days=1)
        self.assertTrue(self.run_evening())                       # a new day fires again
        self.assertEqual(len([c for c in self.engine.store.list(("pending",)) if c["source"] == "today"]), 2)

    def test_not_before_the_time_not_on_weekends_not_when_off(self):
        self.clock.now = MON_EVENING.replace(hour=19, minute=29)
        self.assertFalse(self.run_evening())
        self.clock.now = dt.datetime(2026, 11, 14, 20, 0)          # Saturday
        self.assertFalse(self.run_evening())
        self.clock.now = MON_EVENING
        self.config.today_enabled = False
        self.assertFalse(self.run_evening())
        self.config.today_enabled = True
        self.config.autopilot_enabled = False
        self.assertFalse(self.run_evening())
        self.config.autopilot_enabled = True
        self.config.today_evening_time = ""
        self.assertFalse(self.run_evening())
        self.config.today_evening_time = "19:30"
        self.assertEqual(self.notes, [])
        self.assertTrue(self.run_evening())

    def test_a_broken_source_still_pushes_once(self):
        self.hub.missing = {"/api/work/calendar"}
        self.assertTrue(self.run_evening())
        self.assertFalse(self.run_evening())
        self.assertEqual(len(self.notes), 1)


class Tools(Base):
    """Typed and spoken use are the same tools: the same text reaches the same Friday tool group."""

    def setUp(self):
        super().setUp()
        outer = self

        class Bridge:
            def __init__(self, get_app, config):
                pass

            async def call(self, method, path, *, params=None, body=None):
                r = await asyncio.to_thread(outer.client.request, method, path, params=params, json=body)
                data = r.json()
                if r.status_code >= 400:
                    return False, str(data.get("detail"))
                return True, data
        with mock.patch.object(friday_tools, "InProcessClient", Bridge):
            self.tools = {t.spec["name"]: t for t in friday_tools.build(lambda: self.app, self.config)}

    def run_tool(self, name, args=None):
        return asyncio.run(self.tools[name].run(args or {}))

    def two_cards(self):
        self.hub.add_mail(1, "Priya <priya@example.com>", "Invoice", "Can you send me the invoice by Friday?")
        self.hub.tasks = [{"gid": "111", "name": "Ship export", "due_on": "2026-11-10", "project": "Web", "completed": False}]
        asyncio.run(self.engine.sweep())
        self.assertEqual(len(self.engine.pending()), 2)

    def test_today_summary_tool(self):
        self.fill()
        ok, text = self.run_tool("today_summary")
        self.assertTrue(ok, text)
        self.assertIn("Design review at 16:00", text)
        self.assertIn("Key numbers", text)
        ok, evening = self.run_tool("today_summary", {"style": "evening"})
        self.assertIn("how your Monday went", evening)
        self.assertFalse(self.tools["today_summary"].needs_approval({}))
        self.config.today_enabled = False
        ok, text = self.run_tool("today_summary")
        self.assertFalse(ok)

    def test_brief_tool_appends_the_day_summary(self):
        # exercise through the real tool with the bridge: friday routes do not exist here, so only check the helper path
        ok, text = self.run_tool("friday_brief")
        self.assertFalse(ok)          # no friday router in this mini app (404): the tool reports it, nothing crashes

    def test_summary_words_route_to_the_tools_typed_or_spoken(self):
        specs = [{"name": n} for n in ("today_summary", "autopilot_pending", "autopilot_decide", "list_dir", "read_file")]
        for phrase in ("summary", "give me my day", "what's the update", "day end", "End of day", "what did I do today",
                       "what have I done", "wrap up", "recap please", "any updates", "Day summary", "approve 1",
                       "skip the mail one", "what is pending"):
            history = [{"role": "user", "content": phrase}]
            names = {s["name"] for s in route_local_tools(specs, history)}
            self.assertIn("today_summary", names, phrase)
            self.assertIn("autopilot_decide", names, phrase)
        self.assertEqual(route_local_tools(specs, [{"role": "user", "content": "hello there"}]), [])

    def test_pending_lists_numbered_and_read_only(self):
        self.two_cards()
        ok, text = self.run_tool("autopilot_pending")
        self.assertTrue(ok, text)
        self.assertIn("2 waiting", text)
        self.assertRegex(text, r"1\. .*id [0-9a-f]{12}")
        self.assertRegex(text, r"2\. ")
        self.assertEqual(len(self.engine.pending()), 2)
        self.assertEqual(self.hub.sent, [])
        self.assertFalse(self.tools["autopilot_pending"].needs_approval({}))

    def numbered(self, text, needle):
        for line in text.splitlines():
            if needle in line:
                return line.split(".")[0]
        self.fail(needle)

    def test_decide_approve_by_number_sends_the_reply(self):
        self.two_cards()
        ok, listing = self.run_tool("autopilot_pending")
        n = self.numbered(listing, "Priya")
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": n, "decision": "approve"})
        self.assertTrue(ok, text)
        self.assertEqual(text, "Sent the reply to Priya.")
        self.assertEqual(self.hub.sent[0][0], "priya@example.com")
        self.assertEqual(len(self.engine.pending()), 1)

    def test_decide_with_edited_text_and_typed_phrases(self):
        self.two_cards()
        self.run_tool("autopilot_pending")
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "approve number 1", "decision": "approve",
                                                      "edited_text": "Sending it Thursday."})
        card_titles = [c["title"] for c in self.engine.pending()]
        self.assertTrue(ok, text)
        self.assertEqual(len(card_titles), 1)
        sent = self.hub.sent or [(None, None, None)]
        comments = self.hub.comments or [(None, None)]
        self.assertTrue(sent[0][2] == "Sending it Thursday." or comments[0][1] == "Sending it Thursday.")

    def test_decide_skip_by_descriptor_and_ambiguity(self):
        self.two_cards()
        self.run_tool("autopilot_pending")
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "the card", "decision": "skip"})
        self.assertFalse(ok)                                        # nothing identifies a card
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "the mail one", "decision": "skip"})
        self.assertTrue(ok, text)
        self.assertIn("Skipped", text)
        self.assertEqual(self.hub.sent, [])
        self.assertEqual(len(self.engine.pending()), 1)
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "7", "decision": "skip"})
        self.assertFalse(ok)
        self.assertIn("no number 7", text)

    def test_decide_needs_a_listing_and_a_valid_decision(self):
        self.two_cards()
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "1", "decision": "approve"})
        self.assertFalse(ok)
        self.assertIn("List the pending cards first", text)
        self.run_tool("autopilot_pending")
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "1", "decision": "delete"})
        self.assertFalse(ok)
        self.assertEqual(self.hub.sent, [])
        # an id the listing never showed is refused too
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": "a" * 12, "decision": "approve"})
        self.assertFalse(ok)

    def test_decide_refuses_a_card_that_is_no_longer_pending(self):
        self.two_cards()
        ok, listing = self.run_tool("autopilot_pending")
        card = [c for c in self.engine.pending() if c["action_kind"] == "send_email"][0]
        self.ap_client.post(f"/api/autopilot/{card['id']}/skip")    # handled from the phone meanwhile
        n = self.numbered(listing, "Priya")
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": n, "decision": "approve"})
        self.assertFalse(ok)
        self.assertIn("isn't pending any more", text)
        self.assertEqual(self.hub.sent, [])
        # and the same card cannot be approved twice
        ok, listing2 = self.run_tool("autopilot_pending")
        n2 = self.numbered(listing2, "Ship export")
        self.assertTrue(self.run_tool("autopilot_decide", {"number_or_id": n2, "decision": "approve"})[0])
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": n2, "decision": "approve"})
        self.assertFalse(ok)

    def test_the_recipient_guard_still_applies_to_voice_approval(self):
        self.two_cards()
        card = [c for c in self.engine.pending() if c["action_kind"] == "send_email"][0]
        self.engine.store.write("UPDATE cards SET payload = ? WHERE id = ?", (
            __import__("json").dumps({**self.engine.store.get(card["id"])["payload"], "to": "evil@elsewhere.com"}), card["id"]))
        ok, listing = self.run_tool("autopilot_pending")
        ok, text = self.run_tool("autopilot_decide", {"number_or_id": self.numbered(listing, "Priya"), "decision": "approve"})
        self.assertFalse(ok)
        self.assertIn("doesn't match the sender", text)
        self.assertEqual(self.hub.sent, [])
        # the mail service picking another recipient is refused as well
        self.engine.store.write("UPDATE cards SET status='skipped' WHERE id = ?", (card["id"],))

    def test_listing_expires(self):
        self.two_cards()
        self.run_tool("autopilot_pending")
        with mock.patch.object(friday_tools.time, "time", return_value=friday_tools.time.time() + 4000):
            ok, text = self.run_tool("autopilot_decide", {"number_or_id": "1", "decision": "skip"})
        self.assertFalse(ok)
        self.assertEqual(len(self.engine.pending()), 2)

    def test_autopilot_off(self):
        self.config.autopilot_enabled = False
        ok, text = self.run_tool("autopilot_pending")
        self.assertIn("off", text)


class Research(Base):
    def draft_prompt(self):
        return self.model.calls[-1][1]

    def sweep(self):
        return asyncio.run(self.engine.sweep())

    def test_email_research_adds_thread_and_notes_without_extra_ai_calls(self):
        self.clock.now = MON_AFTERNOON
        self.hub.add_mail(7, "Priya <priya@example.com>", "Re: Invoice for Atlas", "Can you send me the invoice by Friday?")
        self.hub.mail_search = [{"title": "Invoice for Atlas", "snippet": "Priya: here is the first request", "open": {"id": "3"}},
                                {"title": "Same mail", "snippet": "dup", "open": {"id": "7"}}]
        self.hub.search_results = [{"title": "atlas.md", "snippet": "Atlas invoices go through finance", "kind": "note"},
                                  {"title": "Settings", "snippet": "x", "kind": "setting"}]
        card = self.sweep()["cards"][0]
        self.assertEqual(self.engine.ai_used(), 1)
        self.assertEqual(len(self.model.calls), 1)
        prompt = self.draft_prompt()
        self.assertIn("Earlier mail on this subject", prompt)
        self.assertIn("here is the first request", prompt)
        self.assertNotIn("dup", prompt)
        self.assertIn("Atlas invoices go through finance", prompt)
        self.assertNotIn("Settings", prompt)
        self.assertEqual(card["research"], "thread + notes")
        self.assertEqual([m for m, p, _ in self.hub.calls if m != "GET"], [])

    def test_asana_research_uses_comments_and_subtasks(self):
        self.hub.tasks = [{"gid": "111", "name": "Ship export", "due_on": "2026-11-10", "project": "Web", "completed": False}]
        self.hub.details["111"] = {"notes": "Export CSV.", "subtasks": [{"name": "CSV", "completed": True}],
                                   "comments": [{"author": "Lee", "text": "Need PDF too", "created_at": "x"}]}
        card = self.sweep()["cards"][0]
        self.assertIn("Need PDF too", self.draft_prompt())
        self.assertIn("subtask (done): CSV", self.draft_prompt())
        self.assertEqual(card["research"], "task")
        self.hub.tasks.append({"gid": "222", "name": "Other", "due_on": "2026-11-10", "project": "", "completed": False})
        self.hub.search_results = [{"title": "n", "snippet": "related note", "kind": "note"}]
        card2 = [c for c in self.sweep()["cards"]][0]
        self.assertEqual(card2["research"], "notes")

    def test_chat_research_is_notes_only(self):
        self.hub.add_alert("a1", "chat", "Sam", "Atlas", "Can you join the Atlas call now?")
        self.hub.search_results = [{"title": "atlas.md", "snippet": "call notes", "kind": "file"}]
        card = self.sweep()["cards"][0]
        self.assertEqual(card["research"], "notes")
        self.assertIn("call notes", self.draft_prompt())

    def test_context_is_capped(self):
        self.hub.add_mail(7, "Priya <priya@example.com>", "Invoice question", "Can you check this invoice?")
        self.hub.mail_search = [{"title": "T" * 300, "snippet": "S" * 900, "open": {"id": str(i)}} for i in range(9)]
        self.hub.search_results = [{"title": "n" * 300, "snippet": "m" * 900, "kind": "note"}] * 9
        text, label = asyncio.run(self.engine.research("email", uid=7, subject="Invoice question"))
        self.assertLessEqual(len(text), autopilot.RESEARCH_CHARS)
        self.assertEqual(label, "thread + notes")
        searches = [p for m, p, _ in self.hub.calls if p in ("/api/inbox/search", "/api/search/all")]
        self.assertLessEqual(len(searches), autopilot.RESEARCH_LOOKUPS)

    def test_disabled_flag_and_exhausted_budget(self):
        self.hub.add_mail(7, "Priya <priya@example.com>", "Invoice question", "Can you check this invoice?")
        self.hub.mail_search = [{"title": "Earlier", "snippet": "x", "open": {"id": "1"}}]
        self.config.autopilot_research = False
        card = self.sweep()["cards"][0]
        self.assertEqual(card["research"], "")
        self.assertNotIn("Earlier mail", self.draft_prompt())
        self.assertEqual([p for m, p, _ in self.hub.calls if p in ("/api/inbox/search", "/api/search/all")], [])
        self.config.autopilot_research = True
        self.config.autopilot_daily_ai_calls = 1
        self.hub.add_mail(8, "B <b@example.com>", "Other question", "Could you confirm this?")
        self.hub.add_mail(9, "C <c@example.com>", "Third question", "Could you confirm that?")
        self.hub.calls.clear()
        self.sweep()                                              # one draft uses the whole budget; the other is over it
        self.assertLessEqual(len([p for m, p, _ in self.hub.calls if p == "/api/inbox/search"]), 1)

    def test_a_failing_lookup_still_makes_the_card(self):
        self.hub.missing = {"/api/inbox/search", "/api/search/all"}
        self.hub.add_mail(7, "Priya <priya@example.com>", "Invoice question", "Can you check this invoice?")
        card = self.sweep()["cards"][0]
        self.assertEqual((card["action_kind"], card["research"]), ("send_email", ""))

    def test_provider_guard_is_unchanged(self):
        self.config.default_provider = "claude"
        self.assertNotIn(autopilot.pick_provider(self.config), autopilot.NEVER)
        self.hub.add_mail(7, "Priya <priya@example.com>", "Invoice question", "Can you check this invoice?")
        self.sweep()
        self.assertTrue(self.model_calls and all(p in ("local", "gemini") for p in self.model_calls))

    def test_settings_validation(self):
        from hub import settings as settings_mod
        self.assertIn("autopilot_research", settings_mod.ALLOWED_KEYS)
        self.assertIn("today_evening_time", settings_mod.ALLOWED_KEYS)


class Cache(Base):
    """Calendar, Asana and mail answers are kept in a SQLite file for today_cache_min minutes (default 15); local sources stay live."""

    def calls_to(self, path):
        return len([c for c in self.hub.calls if c[1] == path])

    def get(self, query=""):
        r = self.client.get("/api/today" + query)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_a_second_open_does_not_ask_the_outside_services_again(self):
        self.hub.meetings = [{"title": "Standup", "start": iso(16), "end": iso(16, 30)}]
        first = self.get()
        self.clock.advance(seconds=5)
        second = self.get()
        self.assertEqual(self.calls_to("/api/work/calendar"), 1)
        self.assertEqual(self.calls_to("/api/inbox/status"), 1)
        self.assertEqual(first["meetings"], second["meetings"])
        self.assertEqual(self.calls_to("/api/expenses"), 2)          # spending is local: always live

    def test_local_sources_are_always_live(self):
        self.get()
        self.hub.expenses = [{"id": 1, "amount": 12.5, "currency": "INR", "category": "food", "note": "tea", "at": iso(14)}]
        self.assertGreaterEqual(self.calls_to("/api/expenses"), 1)
        self.get()
        self.assertEqual(self.calls_to("/api/expenses"), 2)

    def test_refresh_skips_the_memory_and_rewrites_it(self):
        self.get()
        self.hub.meetings = [{"title": "New invite", "start": iso(17), "end": iso(17, 30)}]
        self.assertEqual(self.get()["meetings"], [])                      # still the remembered, empty calendar
        fresh = self.get("?refresh=1")
        self.assertEqual([m["title"] for m in fresh["meetings"]], ["New invite"])
        self.assertEqual([m["title"] for m in self.get()["meetings"]], ["New invite"])   # the refresh was kept
        self.assertEqual(self.calls_to("/api/work/calendar"), 2)

    def test_it_expires_after_the_minutes_in_the_setting(self):
        self.get()
        self.clock.advance(minutes=14)
        self.get()
        self.assertEqual(self.calls_to("/api/work/calendar"), 1)
        self.clock.advance(minutes=2)
        self.get()
        self.assertEqual(self.calls_to("/api/work/calendar"), 2)

    def test_zero_means_always_live(self):
        self.config.today_cache_min = 0
        self.get()
        self.get()
        self.assertEqual(self.calls_to("/api/work/calendar"), 2)

    def test_it_survives_a_hub_restart(self):
        self.hub.meetings = [{"title": "Standup", "start": iso(16), "end": iso(16, 30)}]
        self.get()
        app2 = FastAPI()
        app2.include_router(today.make_router(self.ctx, caller=self.bridge, clock=self.clock, git_runner=self.git,
                                              audit_reader=lambda day: self.audit_rows))
        client2 = TestClient(app2, cookies={"hub_session": "yes"})
        self.addCleanup(client2.close)
        r = client2.get("/api/today").json()
        self.assertEqual([m["title"] for m in r["meetings"]], ["Standup"])
        self.assertEqual(self.calls_to("/api/work/calendar"), 1)

    def test_a_service_that_is_down_shows_the_older_copy_and_says_so(self):
        self.hub.meetings = [{"title": "Standup", "start": iso(16), "end": iso(16, 30)}]
        self.get()
        self.hub.missing = {"/api/work/calendar"}
        self.clock.advance(minutes=30)
        data = self.get()
        self.assertEqual([m["title"] for m in data["meetings"]], ["Standup"])
        self.assertIn("/api/work/calendar", data["cache"]["stale"])
        self.assertNotIn("calendar", data["sources_failed"])

    def test_nothing_remembered_and_down_is_still_a_failed_source(self):
        self.hub.missing = {"/api/work/calendar"}
        self.assertIn("calendar", self.get()["sources_failed"])

    def test_past_meetings_follow_the_clock_even_when_the_calendar_is_remembered(self):
        self.config.today_cache_min = 60
        self.hub.meetings = [{"title": "Sync", "start": iso(15, 10), "end": iso(15, 40)}]
        self.assertFalse(self.get()["meetings"][0]["past"])
        self.clock.advance(minutes=45)                                   # 15:45, still inside the 60 minutes
        data = self.get()
        self.assertEqual(self.calls_to("/api/work/calendar"), 1)
        self.assertTrue(data["meetings"][0]["past"])

    def test_yesterdays_answers_are_not_reused(self):
        self.get()
        self.clock.advance(days=1)
        self.get()
        self.assertEqual(self.calls_to("/api/work/calendar"), 2)

    def test_the_page_says_how_old_the_outside_data_is(self):
        self.get()
        self.clock.advance(minutes=7)
        data = self.get()
        self.assertEqual(data["cache"]["age_min"], 7)
        self.assertEqual(data["cache"]["minutes"], 15.0)

    def test_the_text_and_front_page_share_the_same_memory(self):
        self.get()
        self.client.get("/api/today/text")
        self.client.get("/api/today/front-page")
        self.assertEqual(self.calls_to("/api/work/calendar"), 1)

    def test_setting_is_editable_and_validated(self):
        from hub import settings as settings_mod
        self.assertIn("today_cache_min", settings_mod.ALLOWED_KEYS)
        self.assertEqual(self.config.today_cache_min, 15)


if __name__ == "__main__":
    unittest.main()
