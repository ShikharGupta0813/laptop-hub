"""Tests for the Brain (hub/brain.py, hub/brain_tools.py): store, search, reminder link, task intake, the fast
"remember" path, context injection, routes, tools, Today and the persona line. Everything is a fake: the model is a
scripted object, time is a fake clock, reminders use a fake clock. No network, no real AI."""
import asyncio
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import assistant as A
from hub import brain, brain_tools, persona, reminders, today
from hub import config as cfg
from hub.assistant import TOOL_GROUPS, ExtraTool, route_local_tools
from hub.common import Ctx
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.llm import LLMError
from hub.server import Deps, create_app
from tests.fakes_assistant import FakeClaude, reply
from tests.helpers import make_config
from tests.test_autopilot import need_auth

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 9, 15, 0)          # naive local, a Monday afternoon


def good_verdict(**over):
    data = {"doable": "no", "blockers": ["The vendor API has no bulk endpoint"], "risks": ["rate limits"],
            "questions": ["Can we get a vendor key?"], "plan": ["Ask the vendor"], "estimate": "2 days"}
    data.update(over)
    return json.dumps(data)


class FakeModel:
    def __init__(self, text=None):
        self.text = text if text is not None else good_verdict()
        self.calls = []
        self.error = None

    async def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.text


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Chief")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.model, self.providers, self.audits = FakeModel(), [], []
        self.ctx = Ctx(config=self.config, model_for=self.model_for, transcriber=None, notes=None,
                       need_auth=need_auth, audit=lambda *a, **k: self.audits.append((a, k)))
        self.rclock = lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
        self.reminders = reminders._service(self.ctx, clock=self.rclock, tz=UTC)   # the Brain finds this one
        self.brain = brain.Brain(self.ctx, clock=lambda: NOW)
        self.store = self.brain.store

    def model_for(self, provider):
        self.providers.append(provider)
        return self.model

    def client(self):
        app = FastAPI()
        app.include_router(brain.make_router(self.ctx, clock=lambda: NOW))
        client = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(client.close)
        return client

    def seed(self):
        a = self.brain.add(text="Login timeouts came from the proxy idle limit. Fix: raise it to 120 seconds.",
                           title="RCA login timeout", kind="rca", tags="login,proxy")
        b = self.brain.add(text="We decided to keep SQLite for the hub data.", kind="decision")
        c = self.brain.add(text="Call the vendor about the invoice.", who="Ravi", kind="task")
        return a, b, c


class StoreTest(Base):
    def test_add_get_and_public_shape(self):
        item = self.brain.add(text="Remember the staging password rotates monthly", kind="fact", tags=["Ops", "ops"])
        self.assertEqual(self.store.get(item["id"])["title"], item["title"])
        self.assertEqual(item["tags"], ["ops"])
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["created"], "2026-11-09T15:00:00")

    def test_sizes_are_capped_and_bad_input_refused(self):
        big = self.brain.add(text="x" * 20000)
        self.assertEqual(len(big["body"]), brain.MAX_BODY)
        self.assertLessEqual(len(big["title"]), brain.MAX_TITLE)
        with self.assertRaises(brain.BrainError):
            self.brain.add(text="   ")
        with self.assertRaises(brain.BrainError):
            self.brain.add(text="a", kind="gossip")
        with mock.patch.object(brain, "MAX_ITEMS", 2):
            self.brain.add(text="one")
            with self.assertRaises(brain.BrainError):
                self.brain.add(text="two and three")

    def test_search_ranks_titles_above_bodies_and_returns_dates(self):
        self.seed()
        self.brain.add(text="Lunch notes: the login page looked nice", kind="note")
        hits = self.store.search("what was the RCA for login timeout", status="all")
        self.assertEqual(hits[0]["title"], "RCA login timeout")
        self.assertGreater(hits[0]["score"], hits[-1]["score"] if len(hits) > 1 else 0)
        self.assertTrue(hits[0]["created"].startswith("2026-11-09"))

    def test_like_fallback_gives_the_same_best_answer(self):
        self.seed()
        fts = self.store.search("login timeout rca", status="all")[0]["id"]
        self.store.fts = False
        self.assertEqual(self.store.search("login timeout rca", status="all")[0]["id"], fts)

    def test_filters_by_kind_persona_and_status(self):
        a, b, c = self.seed()
        self.assertEqual([i["id"] for i in self.store.search("", kind="rca")], [a["id"]])
        self.assertEqual(a["persona"], "jarvis")
        self.assertEqual(c["persona"], "friday")           # a task from a person
        friday = [i["id"] for i in self.store.search("", persona="friday")]
        self.assertIn(c["id"], friday)
        self.assertNotIn(a["id"], friday)
        self.brain.update(c["id"], {"status": "done"})
        self.assertNotIn(c["id"], [i["id"] for i in self.store.search("")])
        self.assertIn(c["id"], [i["id"] for i in self.store.search("", status="all")])

    def test_persona_is_chosen_from_the_kind(self):
        self.assertEqual(brain.persona_for("rca"), "jarvis")
        self.assertEqual(brain.persona_for("fact"), "jarvis")
        self.assertEqual(brain.persona_for("research"), "jarvis")
        self.assertEqual(brain.persona_for("task", "Ravi"), "friday")
        self.assertEqual(brain.persona_for("task", ""), "jarvis")
        self.assertEqual(brain.persona_for("note"), "both")
        self.assertEqual(brain.persona_for("note", has_reminder=True), "friday")

    def test_notes_are_appended_and_quotes_in_queries_are_harmless(self):
        item = self.brain.add(text="Deploy plan", kind="task")
        out = self.brain.update(item["id"], {"status": "blocked", "note": "waiting for keys"})
        self.assertIn("[2026-11-09] waiting for keys", out["body"])
        self.assertEqual(self.store.search('"; DROP TABLE items; -- *', status="all"), [])
        self.assertEqual(self.store.count(), 1)


class ReminderLinkTest(Base):
    def test_a_phrase_makes_a_real_reminder_and_the_item_keeps_the_link(self):
        item = self.brain.add(text="Send Ravi the report", remind_at="tomorrow 9am")
        self.assertEqual(item["remind_at"], "2026-11-10T09:00:00")
        self.assertEqual(item["persona"], "friday")
        r = self.reminders._find(item["reminder_id"])
        self.assertIn("Send Ravi the report", r["text"])
        self.assertEqual(r["source"], "brain")

    def test_an_iso_time_works_and_nonsense_is_refused(self):
        item = self.brain.add(text="Check build", remind_at="2026-11-12T08:30:00")
        self.assertEqual(item["remind_at"], "2026-11-12T08:30:00")
        with self.assertRaises(brain.BrainError):
            self.brain.add(text="Check", remind_at="whenever the moon is blue")
        self.assertEqual(self.store.count(), 1)

    def test_done_finishes_and_delete_removes_the_reminder(self):
        a = self.brain.add(text="First", remind_at="tomorrow 9am")
        b = self.brain.add(text="Second", remind_at="tomorrow 10am")
        self.brain.update(a["id"], {"status": "done"})
        self.assertTrue(self.reminders._find(a["reminder_id"])["done"])
        self.brain.delete(b["id"])
        with self.assertRaises(Exception):
            self.reminders._find(b["reminder_id"])
        self.assertIsNone(self.store.get(b["id"]))

    def test_due_lists_todays_followups_and_flags_late_ones(self):
        self.brain.add(text="Late one", remind_at="2026-11-09T14:59:30")
        self.brain.add(text="Tonight", remind_at="2026-11-09T18:00:00")
        self.brain.add(text="Tomorrow", remind_at="2026-11-10T09:00:00")
        d = self.brain.due()
        self.assertEqual([i["title"] for i in d["due"]], ["Late one", "Tonight"])
        self.assertTrue(d["due"][0]["overdue"])
        self.assertFalse(d["due"][1]["overdue"])


class IntakeTest(Base):
    def run_intake(self, **kw):
        return asyncio.run(self.brain.intake(kw.pop("who", "Ravi"), kw.pop("request", "Export all invoices in one call"),
                                             **kw))

    def test_verdict_is_stored_spoken_and_uses_memory(self):
        self.seed()
        out = self.run_intake(request="Fix the login timeout for the vendor portal")
        self.assertEqual(out["assessment"]["doable"], "no")
        self.assertIn("Chief, from Ravi", out["spoken"])
        self.assertIn("can't be done yet because The vendor API has no bulk endpoint", out["spoken"])
        item = out["item"]
        self.assertEqual((item["kind"], item["who"], item["persona"], item["source"]), ("task", "Ravi", "friday", "intake"))
        self.assertEqual(item["assessment"]["estimate"], "2 days")
        self.assertIn("Verdict: no", item["body"])
        system, user = self.model.calls[0]
        self.assertIn("RCA login timeout", user)       # the relevant memory reached the model
        self.assertIn("BEGIN DATA", user)

    def test_yes_and_partly_sound_different(self):
        self.model.text = good_verdict(doable="yes", blockers=[], plan=["Write the script"])
        self.assertIn("can be done. Start with: Write the script", self.run_intake()["spoken"])
        self.model.text = good_verdict(doable="partly")
        self.assertIn("in part", self.run_intake()["spoken"])

    def test_a_failing_or_garbled_model_still_saves_the_task(self):
        self.model.error = LLMError("model is down")
        out = self.run_intake()
        self.assertIsNone(out["assessment"])
        self.assertIn("could not assess", out["spoken"])
        self.assertEqual(out["item"]["kind"], "task")
        self.model.error = None
        self.model.text = json.dumps({"doable": "maybe"})
        self.assertIsNone(self.run_intake()["assessment"])
        self.assertEqual(self.store.count(), 2)

    def test_the_paid_claude_provider_is_never_used(self):
        self.config.default_provider = "claude"
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            self.run_intake()
        self.assertTrue(self.providers)
        self.assertNotIn("claude", self.providers)
        self.assertTrue(set(self.providers) <= {"local", "gemini"})

    def test_a_follow_up_time_is_linked(self):
        out = self.run_intake(remind_at="tomorrow 9am")
        self.assertTrue(out["item"]["reminder_id"])


class FastPathTest(Base):
    def test_plain_remember_and_note_are_saved_without_a_model(self):
        self.assertEqual(brain.fast_save(self.config, "remember the wifi password is on the router"), "Saved.")
        self.assertEqual(brain.fast_save(self.config, "Note: the RCA for the outage was a full disk"), "Saved.")
        items = self.store.search("", status="all")
        self.assertEqual({i["kind"] for i in items}, {"note", "rca"})
        self.assertEqual([i for i in items if i["kind"] == "rca"][0]["persona"], "jarvis")

    def test_questions_times_and_other_text_are_left_to_the_assistant(self):
        for text in ("remember when we fixed the proxy?", "remember what Ravi said", "remember me",
                     "remember to call Ravi tomorrow", "remember to ping Anu at 5pm", "note", "open notes",
                     "do you remember the RCA", "what should I note", "hello there"):
            self.assertEqual(brain.fast_save(self.config, text), "", text)
        self.assertEqual(self.store.count(), 0)

    def test_switched_off(self):
        self.config.brain_enabled = False
        self.assertEqual(brain.fast_save(self.config, "remember the milk is out"), "")


class ContextTest(Base):
    def test_a_question_with_a_good_match_gets_dated_memories_within_600_chars(self):
        self.seed()
        self.brain.add(text="y" * 3000, title="RCA login timeout second", kind="rca")
        out = brain.context_for("What was the RCA we did for the login timeout?", self.config)
        self.assertIn("2026-11-09", out)
        self.assertIn("RCA login timeout", out)
        self.assertLessEqual(len(out), 600)
        self.assertLessEqual(out.count("\n") + 1, 3)

    def test_nothing_for_greetings_commands_statements_or_weak_matches(self):
        self.seed()
        for text in ("hey jarvis", "open the notes folder", "run git status", "the login timeout is bad today",
                     "what is the weather in paris?", "what was the quarterly budget for marketing?", ""):
            self.assertEqual(brain.context_for(text, self.config), "", text)

    def test_can_be_switched_off(self):
        self.seed()
        self.config.brain_context = False
        self.assertEqual(brain.context_for("what was the RCA for the login timeout?", self.config), "")
        self.config.brain_context, self.config.brain_enabled = True, False
        self.assertEqual(brain.context_for("what was the RCA for the login timeout?", self.config), "")


class AssistantHookTest(Base, unittest.IsolatedAsyncioTestCase):
    def service(self, script=()):
        self.claude = FakeClaude(script)
        dummy = [ExtraTool({"name": n, "description": "x", "input_schema": {"type": "object", "properties": {}}},
                           None) for n in ("remember", "recall")]
        return A.AssistantService(self.config, claude=self.claude, extra_tools=dummy)

    async def collect(self, svc, text, persona_name=""):
        return [e async for e in svc.run("s", text, "claude", False, persona=persona_name)]

    async def test_remember_is_instant_and_never_calls_the_model(self):
        svc = self.service([])
        events = await self.collect(svc, "remember the build server is on port 8081")
        self.assertEqual("".join(e.get("t", "") for e in events if e["type"] == "text"), "Saved.")
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.store.search("build server")[0]["kind"], "note")

    async def test_a_recall_question_gets_the_memory_in_the_prompt_and_a_greeting_does_not(self):
        self.seed()
        svc = self.service([reply("It was the proxy idle limit."), reply("ok")])
        await self.collect(svc, "What was the RCA we did for the login timeout?", "jarvis")
        system = self.claude.calls[0]["system"]
        self.assertIn("From the owner's memory", str(system))
        self.assertIn("proxy idle limit", str(system))
        await self.collect(svc, "please summarise the log file for me", "jarvis")
        self.assertNotIn("From the owner's memory", str(self.claude.calls[1]["system"]))
        events = await self.collect(svc, "hey jarvis")
        self.assertEqual(len(self.claude.calls), 2)       # a greeting never reaches the model

    async def test_without_the_brain_tools_nothing_changes(self):
        svc = A.AssistantService(self.config, claude=FakeClaude([reply("hi there")]))
        events = await self.collect(svc, "remember the milk")
        self.assertIn("hi there", "".join(e.get("t", "") for e in events if e["type"] == "text"))


class RouteTest(Base):
    def test_create_list_search_patch_delete(self):
        c = self.client()
        made = c.post("/api/brain/items", json={"text": "RCA: the cache key missed the tenant id", "kind": "rca",
                                                 "tags": ["cache"]}).json()
        self.assertEqual(made["persona"], "jarvis")
        c.post("/api/brain/items", json={"text": "Ravi wants the report", "kind": "task", "who": "Ravi"})
        r = c.get("/api/brain/items", params={"q": "cache tenant"}).json()
        self.assertEqual(r["items"][0]["id"], made["id"])
        self.assertEqual(r["total"], 2)
        self.assertEqual(len(c.get("/api/brain/items", params={"kind": "task"}).json()["items"]), 1)
        self.assertEqual(len(c.get("/api/brain/items", params={"persona": "friday"}).json()["items"]), 1)
        p = c.patch(f"/api/brain/items/{made['id']}", json={"status": "done", "title": "Cache RCA"}).json()
        self.assertEqual((p["status"], p["title"]), ("done", "Cache RCA"))
        self.assertEqual(c.get("/api/brain/items", params={"kind": "rca"}).json()["items"], [])
        self.assertTrue(c.delete(f"/api/brain/items/{made['id']}").json()["ok"])
        self.assertEqual(c.delete(f"/api/brain/items/{made['id']}").status_code, 404)

    def test_inputs_are_validated(self):
        c = self.client()
        for body in ({}, {"text": ""}, {"text": 5}, {"text": "x", "kind": "gossip"}, {"text": "x", "persona": "bob"},
                     {"text": "x" * 20000}, {"text": "x", "remind_at": "never ever"}, {"text": "x", "who": "w" * 500}):
            self.assertEqual(c.post("/api/brain/items", json=body).status_code, 400, body)
        for params in ({"kind": "bad"}, {"status": "bad"}, {"persona": "bad"}, {"limit": 0}):
            self.assertIn(c.get("/api/brain/items", params=params).status_code, (400, 422), params)
        self.assertEqual(c.patch("/api/brain/items/nothex", json={}).status_code, 400)
        self.assertEqual(c.patch("/api/brain/items/" + "a" * 12, json={}).status_code, 404)
        self.assertEqual(c.patch("/api/brain/items/" + "a" * 12, json={"status": "weird"}).status_code, 404)
        self.assertEqual(c.post("/api/brain/intake", json={"from": "x"}).status_code, 400)
        self.assertEqual(c.post("/api/brain/intake", json={"request": 4}).status_code, 400)

    def test_markup_is_stored_as_text(self):
        c = self.client()
        made = c.post("/api/brain/items", json={"text": "<img src=x onerror=alert(1)>"}).json()
        self.assertEqual(made["body"], "<img src=x onerror=alert(1)>")      # kept as text; the UI uses textContent

    def test_intake_and_due(self):
        c = self.client()
        out = c.post("/api/brain/intake", json={"from": "Ravi", "request": "Migrate the DB tonight",
                                                "remind_at": "2026-11-09T20:00:00"}).json()
        self.assertEqual(out["assessment"]["doable"], "no")
        due = c.get("/api/brain/due").json()
        self.assertEqual([i["id"] for i in due["due"]], [out["item"]["id"]])
        self.assertEqual(len(due["open_tasks"]), 1)

    def test_login_is_needed_and_the_switch_works(self):
        app = FastAPI()
        app.include_router(brain.make_router(self.ctx, clock=lambda: NOW))
        with TestClient(app) as anon:
            self.assertEqual(anon.get("/api/brain/items").status_code, 401)
        self.config.brain_enabled = False
        self.assertEqual(self.client().get("/api/brain/items").status_code, 404)


class ToolTest(Base):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant()),
                              audit_path=self.root / "audit.log")
        self.tools = brain_tools.build(lambda: self.app, self.config)

    def tool(self, name):
        return next(t for t in self.tools if t.spec["name"] == name)

    def run_tool(self, name, **args):
        return asyncio.run(self.tool(name).run(args))

    def test_specs_and_approval(self):
        self.assertEqual({t.spec["name"] for t in self.tools},
                         {"remember", "recall", "task_intake", "task_update", "tasks_list"})
        for t in self.tools:
            self.assertFalse(t.needs_approval({}), t.spec["name"])          # only local notes are written
            self.assertEqual(t.spec["input_schema"]["type"], "object")
        self.assertEqual(self.tool("remember").spec["input_schema"]["required"], ["text"])
        self.assertEqual(self.tool("task_intake").spec["input_schema"]["required"], ["request"])

    def test_the_brain_tool_group_is_routed(self):
        names, pattern = TOOL_GROUPS["brain"]
        self.assertEqual(names, {"remember", "recall", "task_intake", "task_update", "tasks_list"})
        for text in ("what was the RCA for the proxy", "someone gave me a task", "remember this for tomorrow",
                     "any follow ups today", "task from Ravi: fix login"):
            self.assertRegex(text, pattern)
        specs = [t.spec for t in self.tools] + [{"name": "list_dir", "description": "d", "input_schema": {}}]
        picked = {s["name"] for s in route_local_tools(specs, [{"role": "user", "content": "what was the RCA for login"}])}
        self.assertIn("recall", picked)

    def test_remember_recall_and_dates(self):
        ok, text = self.run_tool("remember", text="RCA: login timeouts were the proxy idle limit", kind="rca")
        self.assertTrue(ok, text)
        self.assertIn("Saved as rca", text)
        ok, text = self.run_tool("recall", query="what was the RCA for the login timeouts")
        self.assertTrue(ok)
        self.assertRegex(text, r"\[rca \| \d{4}-\d\d-\d\d \| jarvis\]")
        ok, text = self.run_tool("recall", query="zebra spaceship")
        self.assertIn("Nothing", text)
        self.assertFalse(self.run_tool("remember", text="  ")[0])

    def test_remember_with_a_time_links_a_reminder(self):
        ok, text = self.run_tool("remember", text="Send the report to Ravi", remind_at="tomorrow 9am")
        self.assertTrue(ok, text)
        self.assertIn("remind you at", text)
        ok, text = self.run_tool("remember", text="x", remind_at="no idea when")
        self.assertFalse(ok)

    def test_intake_update_and_list(self):
        async def fake_ask(model, system, user, retries=1):
            return json.loads(good_verdict(doable="partly"))
        with mock.patch.object(brain, "ask_json", fake_ask):
            ok, text = self.run_tool("task_intake", **{"from": "Anu", "request": "Add SSO to the portal"})
        self.assertTrue(ok, text)
        self.assertIn("from Anu", text)
        task_id = text.split("saved as task ")[1].rstrip(")")
        ok, text = self.run_tool("tasks_list")
        self.assertIn("verdict partly", text)
        ok, text = self.run_tool("task_update", id=task_id, status="done", note="shipped")
        self.assertTrue(ok, text)
        self.assertIn("No open tasks", self.run_tool("tasks_list")[1])
        self.assertIn(task_id, self.run_tool("tasks_list", status="done")[1])
        self.assertFalse(self.run_tool("task_update", id=task_id)[0])

    def test_intake_with_a_useless_model_answer_still_saves(self):
        ok, text = self.run_tool("task_intake", request="Rewrite the kernel")   # FakeLLM answers with other JSON
        self.assertTrue(ok, text)
        self.assertIn("could not assess", text)


class TodayAndPersonaTest(Base):
    def setUp(self):
        super().setUp()
        from tests.test_today import TodayHub
        from tests.test_autopilot import Clock
        self.clock = Clock(dt.datetime(2026, 11, 9, 15, 0))
        self.hub = TodayHub(self.clock)

    async def bridge(self, method, path, *, params=None, body=None):
        return await self.hub(method, path, params=params, body=body)

    def today_client(self):
        app = FastAPI()
        app.include_router(today.make_router(self.ctx, caller=self.bridge, clock=self.clock,
                                             git_runner=lambda a: (0, ""), audit_reader=lambda d: []))
        client = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(client.close)
        return client

    def test_followups_and_open_tasks_show_up_in_today(self):
        self.brain.add(text="Send Ravi the report", who="Ravi", remind_at="2026-11-09T17:00:00")
        self.brain.add(text="Review the migration plan", kind="task", who="Anu")
        self.brain.add(text="Not today", remind_at="2026-11-12T09:00:00")
        data = self.today_client().get("/api/today").json()
        self.assertEqual([f["title"] for f in data["brain"]["due"]], ["Send Ravi the report"])
        self.assertEqual(len(data["brain"]["tasks"]), 1)
        follow = [t for t in data["timeline"] if t["kind"] == "followup"]
        self.assertEqual((follow[0]["time"], follow[0]["title"]), ("17:00", "Follow up: Send Ravi the report"))
        self.assertEqual(data["numbers"]["followups_due"], 1)
        self.assertNotIn("brain", data["sources_failed"])
        said = self.today_client().get("/api/today/text").json()["text"]
        self.assertIn("Follow-ups due: 1, starting with Send Ravi the report for Ravi.", said)

    def test_an_empty_or_switched_off_memory_changes_nothing(self):
        data = self.today_client().get("/api/today").json()
        self.assertEqual(data["brain"], {"due": [], "tasks": []})
        self.assertNotIn("Follow-up", self.today_client().get("/api/today/text").json()["text"])
        self.config.brain_enabled = False
        self.assertEqual(self.today_client().get("/api/today").json()["brain"], {"due": [], "tasks": []})

    def test_personas_each_get_their_memory_line(self):
        self.assertIn("recall first", persona.persona_prompt("jarvis", "Chief"))
        friday = persona.persona_prompt("friday", "Chief")
        self.assertIn("recall and the follow-ups", friday)
        self.assertLess(len(friday), 520)

    def test_settings_and_config_know_the_switches(self):
        from hub import settings
        self.assertIn("brain_enabled", settings.ALLOWED_KEYS)
        self.assertIn("brain_context", settings.ALLOWED_KEYS)
        path = self.tmp / "hub.json"
        self.config.brain_context = False
        cfg.save(self.config, path)
        self.assertIs(cfg.load(path).brain_context, False)
        self.assertIs(cfg.Config.__dataclass_fields__["brain_enabled"].default, True)

    def test_the_screen_is_registered(self):
        static = Path(__file__).resolve().parent.parent / "hub" / "static"
        self.assertIn('id: "brain"', (static / "js" / "main.js").read_text(encoding="utf-8"))
        sw = (static / "sw.js").read_text(encoding="utf-8")
        self.assertIn("/js/brain.js", sw)
        self.assertIn("/css/brain.css", sw)
        self.assertTrue((static / "js" / "brain.js").exists() and (static / "css" / "brain.css").exists())


if __name__ == "__main__":
    unittest.main()
