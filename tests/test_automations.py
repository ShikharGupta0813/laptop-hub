"""Tests for the AUTOMATIONS module. The hub's other routes are replaced by an injectable caller that records
every call, the clock is fake, and `wait` steps use a fake sleep: nothing here touches the laptop or waits."""
import asyncio
import datetime as dt
import json
import logging
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import automations as au
from hub.common import Ctx
from tests.helpers import TOKEN, make_config

D = dt.datetime
logging.getLogger("hub.automations").addHandler(logging.NullHandler())
logging.getLogger("hub.automations").propagate = False      # expected failures stay quiet
NOT_AVAILABLE = "That feature is not available on this hub."

REGISTRY = {"actions": [
    {"id": "system_info", "risk": "safe"}, {"id": "open_url", "risk": "safe"}, {"id": "mute", "risk": "safe"},
    {"id": "lock_screen", "risk": "safe"}, {"id": "sleep", "risk": "confirm"}, {"id": "shutdown", "risk": "confirm"},
    {"id": "restart", "risk": "confirm"}, {"id": "kill_process", "risk": "confirm"},
    {"id": "open_path", "risk": "confirm"}, {"id": "future_danger", "risk": "confirm"},
    {"id": "script:hello", "risk": "safe"}]}


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeHub:
    """Stands in for InProcessClient.call: records calls, answers from `routes` or a default."""

    def __init__(self):
        self.calls = []
        self.routes = {}          # (method, path) -> (ok, data) or a callable(body) -> (ok, data)

    async def __call__(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/laptop/actions":
            default = (True, REGISTRY)
        elif method == "GET" and path == "/api/scripts":
            default = (True, [{"name": "hello", "label": "Say hello"}])
        elif path == "/api/laptop/run":
            default = (True, {"ok": True, "kind": "none", "message": f"Did {body['action']}."})
        elif path.startswith("/api/run/"):
            default = (True, {"exit": 0, "output": "hello from a button"})
        elif path == "/api/notes":
            default = (True, {"id": "n1"})
        elif path == "/api/jarvis/say":
            default = (True, {"queued": True, "id": "x"})
        elif path == "/api/jarvis/start":
            default = (True, {"running": True})
        else:
            default = (False, NOT_AVAILABLE)
        answer = self.routes.get((method, path), default)
        return answer(body) if callable(answer) else answer

    def paths(self, path):
        return [c for c in self.calls if c[1] == path]


def notify_step(title="Hi", **extra):
    return {"type": "notify", "title": title, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="auto-test-"))
        self.audits = []
        self.notes = []
        self.hub = FakeHub()
        self.clock = Clock(D(2026, 11, 4, 9, 0, 0))   # a Wednesday
        self.slept = []

    def make_ctx(self, data_dir=None):
        config, _ = make_config(data_dir=str(data_dir or self.tmp / "data"))
        return Ctx(config=config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                   audit=lambda event, **f: self.audits.append((event, f)),
                   notify=lambda title, body="", level="info", link=None, source="": self.notes.append(
                       (title, body, level, source)))

    async def fake_sleep(self, seconds):
        self.slept.append(seconds)

    def engine(self, ctx=None):
        return au.Engine(ctx or self.make_ctx(), caller=self.hub, clock=self.clock, sleep=self.fake_sleep)

    def build(self, ctx=None, sleep=None):
        ctx = ctx or self.make_ctx()
        app = FastAPI()
        app.include_router(au.make_router(ctx, caller=self.hub, clock=self.clock, sleep=sleep or self.fake_sleep))
        client = TestClient(app)
        client.__enter__()                    # one event loop for the whole test, so background runs survive
        self.addCleanup(client.__exit__, None, None, None)
        client.cookies.set("hub_session", "yes")
        self.ctx = ctx
        return client

    def create(self, client, name="Routine", steps=None, **extra):
        body = {"name": name, "steps": steps or [notify_step()], **extra}
        r = client.post("/api/automations", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


# ---- step validation --------------------------------------------------------------------------------------------

class StepValidation(unittest.TestCase):
    GOOD = [
        {"type": "laptop_action", "action_id": "system_info"},
        {"type": "laptop_action", "action_id": "open_url", "params": {"url": "https://example.com/a"}},
        {"type": "laptop_action", "action_id": "script:Say hello", "params": {}},
        {"type": "script", "name": "hello"},
        {"type": "jarvis_say", "text": "good morning"},
        {"type": "jarvis_start"},
        {"type": "open_url", "url": "http://example.com"},
        {"type": "notify", "title": "T"},
        {"type": "notify", "title": "T", "body": "B"},
        {"type": "note", "title": "T", "body": "multi\nline"},
        {"type": "wait", "seconds": 300},
        {"type": "wait", "seconds": "5"},
        {"type": "briefing"},
        {"type": "reminder", "text": "x", "in_minutes": 10},
        {"type": "notify", "title": "T", "on_error": "continue"},
    ]
    BAD = [
        ("not an object", "just text", "must be an object"),
        ("unknown type", {"type": "format_disk"}, "not a step type"),
        ("missing type", {"title": "x"}, "not a step type"),
        ("type not text", {"type": ["notify"]}, "not a step type"),
        ("extra field", {"type": "notify", "title": "T", "argv": ["rm"]}, "not an option"),
        ("on_error bad", {"type": "notify", "title": "T", "on_error": "ignore"}, "on_error"),
        ("missing title", {"type": "notify"}, "Title is required"),
        ("blank title", {"type": "notify", "title": "   "}, "Title is required"),
        ("title not text", {"type": "notify", "title": 5}, "must be text"),
        ("title too long", {"type": "notify", "title": "x" * 121}, "too long"),
        ("control char", {"type": "notify", "title": "a\x00b"}, "not allowed"),
        ("note body too long", {"type": "note", "title": "t", "body": "x" * 5001}, "too long"),
        ("say too long", {"type": "jarvis_say", "text": "x" * 2001}, "too long"),
        ("wait too long", {"type": "wait", "seconds": 301}, "between 1 and 300"),
        ("wait zero", {"type": "wait", "seconds": 0}, "between 1 and 300"),
        ("wait negative", {"type": "wait", "seconds": -5}, "between 1 and 300"),
        ("wait text", {"type": "wait", "seconds": "soon"}, "whole number"),
        ("wait bool", {"type": "wait", "seconds": True}, "number"),
        ("wait fraction", {"type": "wait", "seconds": 2.5}, "whole number"),
        ("reminder minutes", {"type": "reminder", "text": "x", "in_minutes": 0}, "between 1"),
        ("reminder no text", {"type": "reminder", "in_minutes": 5}, "required"),
        ("url ftp", {"type": "open_url", "url": "ftp://example.com"}, "http"),
        ("url javascript", {"type": "open_url", "url": "javascript:alert(1)"}, "http"),
        ("url file", {"type": "open_url", "url": "file:///etc/passwd"}, "http"),
        ("url no host", {"type": "open_url", "url": "https://"}, "http"),
        ("url whitespace", {"type": "open_url", "url": "https://a.com/ x"}, "not a valid"),
        ("action open_url bad url", {"type": "laptop_action", "action_id": "open_url",
                                     "params": {"url": "file:///c:/x"}}, "http"),
        ("action open_url no url", {"type": "laptop_action", "action_id": "open_url"}, "web address"),
        ("action id weird", {"type": "laptop_action", "action_id": "a;rm -rf /"}, "not a valid laptop action"),
        ("action id missing", {"type": "laptop_action"}, "required"),
        ("params list", {"type": "laptop_action", "action_id": "x", "params": [1]}, "name and value"),
        ("params nested", {"type": "laptop_action", "action_id": "x", "params": {"a": {"b": 1}}}, "text, a number"),
        ("script name path", {"type": "script", "name": "../etc"}, "not a valid button"),
        ("script name slash", {"type": "script", "name": "a/b"}, "not a valid button"),
    ]

    def test_good_steps_are_cleaned_and_get_a_default_policy(self):
        for raw in self.GOOD:
            with self.subTest(raw=raw):
                step = au.validate_step(raw, 0)
                self.assertEqual(step["type"], raw["type"])
                self.assertIn(step["on_error"], ("stop", "continue"))
        self.assertEqual(au.validate_step({"type": "notify", "title": " T "}, 0)["on_error"], "stop")
        self.assertEqual(au.validate_step({"type": "wait", "seconds": "5"}, 0)["seconds"], 5)

    def test_bad_steps_are_refused_with_a_sentence(self):
        for label, raw, fragment in self.BAD:
            with self.subTest(label):
                with self.assertRaises(au.Bad) as caught:
                    au.validate_step(raw, 1)
                self.assertIn(fragment, str(caught.exception))
                self.assertTrue(str(caught.exception).startswith("Step 2"))

    def test_every_step_type_is_in_the_spec(self):
        self.assertEqual(set(au.STEP_TYPES), {"laptop_action", "script", "jarvis_say", "jarvis_start", "open_url",
                                              "notify", "note", "wait", "briefing", "reminder"})


# ---- schedules ----------------------------------------------------------------------------------------------------

class ScheduleMath(unittest.TestCase):
    def test_schedule_validation(self):
        good = [{"kind": "daily", "time": "08:30"}, {"kind": "weekdays", "time": "23:59"},
                {"kind": "weekly", "time": "07:00", "days": ["Mon", "wednesday", 6]},
                {"kind": "interval", "every_minutes": 5}, {"kind": "interval", "every_minutes": 1440},
                {"kind": "once", "at": "2030-01-02T03:04"}]
        for raw in good:
            with self.subTest(raw=raw):
                au.validate_schedule(raw)
        self.assertEqual(au.validate_schedule(good[2])["days"], [0, 2, 6])
        bad = [None, "daily", {"kind": "hourly"}, {"kind": "daily"}, {"kind": "daily", "time": "8:30"},
               {"kind": "daily", "time": "24:00"}, {"kind": "daily", "time": "08:30", "days": [1]},
               {"kind": "weekly", "time": "08:30"}, {"kind": "weekly", "time": "08:30", "days": []},
               {"kind": "weekly", "time": "08:30", "days": ["funday"]}, {"kind": "weekly", "time": "08:30", "days": [7]},
               {"kind": "interval", "every_minutes": 4}, {"kind": "interval", "every_minutes": 1441},
               {"kind": "interval", "every_minutes": "10"}, {"kind": "interval", "every_minutes": True},
               {"kind": "once", "at": "tomorrow"}, {"kind": "once"}]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(au.Bad):
                    au.validate_schedule(raw)

    def test_daily_next_and_previous(self):
        s = {"kind": "daily", "time": "08:30"}
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 8, 0)), D(2026, 11, 4, 8, 30))
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 8, 30)), D(2026, 11, 5, 8, 30))   # strictly after
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 9, 0)), D(2026, 11, 5, 8, 30))
        self.assertEqual(au.prev_slot(s, D(2026, 11, 4, 8, 29)), D(2026, 11, 3, 8, 30))
        self.assertEqual(au.prev_slot(s, D(2026, 11, 4, 8, 30)), D(2026, 11, 4, 8, 30))   # at or before

    def test_weekdays_skip_the_weekend(self):
        s = {"kind": "weekdays", "time": "09:00"}
        friday_evening = D(2026, 11, 6, 18, 0)
        self.assertEqual(au.next_slot(s, friday_evening), D(2026, 11, 9, 9, 0))           # Monday
        self.assertEqual(au.prev_slot(s, D(2026, 11, 8, 12, 0)), D(2026, 11, 6, 9, 0))    # Sunday looks back to Friday

    def test_weekly_uses_the_chosen_days_only(self):
        s = au.validate_schedule({"kind": "weekly", "time": "07:15", "days": ["tue", "sat"]})
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 12, 0)), D(2026, 11, 7, 7, 15))   # Wed -> Sat
        self.assertEqual(au.next_slot(s, D(2026, 11, 7, 8, 0)), D(2026, 11, 10, 7, 15))   # Sat -> Tue
        self.assertEqual(au.prev_slot(s, D(2026, 11, 4, 12, 0)), D(2026, 11, 3, 7, 15))

    def test_week_and_month_boundaries(self):
        s = {"kind": "daily", "time": "00:05"}
        self.assertEqual(au.next_slot(s, D(2026, 12, 31, 23, 59)), D(2027, 1, 1, 0, 5))

    def test_interval_runs_from_its_anchor(self):
        anchor = D(2026, 11, 4, 9, 0)
        s = {"kind": "interval", "every_minutes": 30}
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 9, 0), anchor), D(2026, 11, 4, 9, 30))
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 9, 29), anchor), D(2026, 11, 4, 9, 30))
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 9, 30), anchor), D(2026, 11, 4, 10, 0))
        self.assertEqual(au.prev_slot(s, D(2026, 11, 4, 10, 29), anchor), D(2026, 11, 4, 10, 0))
        self.assertIsNone(au.prev_slot(s, D(2026, 11, 4, 8, 0), anchor))

    def test_once(self):
        s = {"kind": "once", "at": "2026-11-05T08:00"}
        self.assertEqual(au.next_slot(s, D(2026, 11, 4, 9, 0)), D(2026, 11, 5, 8, 0))
        self.assertIsNone(au.next_slot(s, D(2026, 11, 5, 8, 0)))
        self.assertIsNone(au.prev_slot(s, D(2026, 11, 4, 9, 0)))
        self.assertEqual(au.prev_slot(s, D(2026, 11, 6, 9, 0)), D(2026, 11, 5, 8, 0))


# ---- the HTTP surface ---------------------------------------------------------------------------------------------

class Routes(Base):
    def test_everything_needs_a_login(self):
        client = self.build()
        client.cookies.clear()
        for method, path in [("get", "/api/automations"), ("post", "/api/automations"),
                             ("get", "/api/automations/templates"), ("get", "/api/automations/step-types"),
                             ("get", "/api/automations/search?q=a"), ("get", "/api/automations/x"),
                             ("post", "/api/automations/x/run"), ("delete", "/api/automations/x")]:
            with self.subTest(path=path):
                self.assertEqual(getattr(client, method)(path).status_code, 401)

    def test_the_http_route_answers_400_for_an_unknown_type(self):
        client = self.build()
        r = client.post("/api/automations", json={"name": "x", "steps": [{"type": "format_disk"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a step type", r.json()["detail"])

    def test_create_read_update_delete(self):
        client = self.build()
        made = self.create(client, "Good morning", [notify_step("Up!"), {"type": "wait", "seconds": 2}],
                           icon="sun", description="Start the day")
        self.assertEqual(made["id"], "good-morning")
        self.assertEqual((made["enabled"], made["last_run"], made["last_status"], made["schedule"]),
                         (True, None, None, None))
        self.assertNotIn("last_slot", made)
        self.assertEqual(client.get("/api/automations").json()["routines"][0]["name"], "Good morning")
        r = client.put("/api/automations/good-morning", json={"name": "Morning", "schedule":
                                                              {"kind": "daily", "time": "08:00"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Morning")
        self.assertEqual(r.json()["steps"][0]["title"], "Up!")          # untouched fields stay
        self.assertEqual(r.json()["next_run"], "2026-11-05T08:00")
        r = client.put("/api/automations/good-morning", json={"schedule": None})
        self.assertIsNone(r.json()["schedule"])
        self.assertEqual(client.delete("/api/automations/good-morning").json(), {"ok": True})
        self.assertEqual(client.get("/api/automations/good-morning").status_code, 404)
        self.assertEqual(client.delete("/api/automations/good-morning").status_code, 404)

    def test_bad_input_is_a_400_with_a_sentence(self):
        client = self.build()
        for body, fragment in [({"steps": [notify_step()]}, "name"), ({"name": "x"}, "at least one step"),
                               ({"name": "x", "steps": []}, "at least one step"),
                               ({"name": "x" * 61, "steps": [notify_step()]}, "too long"),
                               ({"name": "x", "steps": [notify_step()] * 31}, "at most 30"),
                               ({"name": "x", "steps": [notify_step()], "enabled": "yes"}, "true or false"),
                               ({"name": "x", "steps": [notify_step()], "schedule": {"kind": "daily"}}, "time"),
                               ({"name": "x", "steps": [notify_step()],
                                 "schedule": {"kind": "once", "at": "2020-01-01T00:00"}}, "already passed")]:
            with self.subTest(body=body):
                r = client.post("/api/automations", json=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(fragment, r.json()["detail"])

    def test_ids_in_the_path_are_validated(self):
        client = self.build()
        for bad in ["..", "A B", "x" * 60, "a%2Fb"]:
            with self.subTest(bad=bad):
                self.assertEqual(client.get(f"/api/automations/{bad}").status_code, 404)

    def test_templates_and_step_types(self):
        client = self.build()
        templates = client.get("/api/automations/templates").json()["templates"]
        self.assertEqual([t["name"] for t in templates], ["Good morning", "Start work", "Study mode", "Leaving desk",
                                                          "Bedtime", "Focus 25", "Meeting prep", "Wrap up day"])
        for t in templates:                        # each one saves as is (with confirm where it says so)
            with self.subTest(t=t["name"]):
                r = client.post("/api/automations", json={**t, "confirm": t["dangerous"]})
                self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual({t["name"] for t in templates if t["dangerous"]}, {"Leaving desk", "Bedtime"})
        types = client.get("/api/automations/step-types").json()
        self.assertEqual({t["type"] for t in types["types"]}, set(au.STEP_TYPES))
        notify = next(t for t in types["types"] if t["type"] == "notify")
        self.assertEqual([f["name"] for f in notify["fields"]], ["title", "body"])
        wait = next(t for t in types["types"] if t["type"] == "wait")
        self.assertEqual(wait["fields"][0]["maximum"], 300)
        self.assertIn("shutdown", types["never_scheduled"])

    def test_search_finds_routines(self):
        client = self.build()
        self.create(client, "Study mode", [notify_step("Books")], description="Focus on reading")
        self.create(client, "Other")
        hits = client.get("/api/automations/search", params={"q": "reading"}).json()["results"]
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["kind"], hits[0]["title"]), ("routine", "Study mode"))
        self.assertEqual(hits[0]["open"], {"view": "automations", "section": "routines", "id": "study-mode"})
        self.assertEqual(client.get("/api/automations/search", params={"q": "books"}).json()["results"][0]["title"],
                         "Study mode")                       # step text counts
        self.assertEqual(client.get("/api/automations/search", params={"q": ""}).json()["results"], [])

    def test_routines_survive_a_restart_and_running_runs_become_interrupted(self):
        ctx = self.make_ctx()
        client = self.build(ctx)
        made = self.create(client, "Keep", [notify_step()])
        client.post(f"/api/automations/{made['id']}/run", json={})
        history = json.loads((Path(ctx.config.data_path()) / "automations_runs.json").read_text())
        history[made["id"]].append({"run_id": "abc123", "status": "running", "results": []})
        (Path(ctx.config.data_path()) / "automations_runs.json").write_text(json.dumps(history))
        again = self.engine(ctx)
        self.assertEqual(again.routines[0]["name"], "Keep")
        self.assertEqual(again.history(made["id"])[0]["status"], "interrupted")


# ---- running ----------------------------------------------------------------------------------------------------------

class Running(Base):
    def run_routine(self, steps, name="R", **extra):
        client = self.build()
        made = self.create(client, name, steps, **extra)
        r = client.post(f"/api/automations/{made['id']}/run", json={})
        self.assertEqual(r.status_code, 200, r.text)
        return client, made, r.json()

    def test_a_manual_run_returns_one_result_per_step(self):
        client, made, out = self.run_routine([
            notify_step("Hello", body="World"), {"type": "wait", "seconds": 7},
            {"type": "laptop_action", "action_id": "system_info"},
            {"type": "script", "name": "hello"}, {"type": "jarvis_say", "text": "hi"},
            {"type": "jarvis_start"}, {"type": "open_url", "url": "https://example.com"},
            {"type": "note", "title": "Title", "body": "Body"}])
        self.assertEqual(out["status"], "ok")
        self.assertEqual([r["ok"] for r in out["results"]], [True] * 8)
        self.assertEqual([r["type"] for r in out["results"]],
                         ["notify", "wait", "laptop_action", "script", "jarvis_say", "jarvis_start", "open_url", "note"])
        self.assertEqual(self.notes, [("Hello", "World", "info", "automations")])
        self.assertEqual(self.slept, [7])
        self.assertIn(("POST", "/api/laptop/run", {"action": "system_info", "params": {}, "confirm": False}),
                      self.hub.calls)
        self.assertIn(("POST", "/api/laptop/run",
                       {"action": "open_url", "params": {"url": "https://example.com"}, "confirm": False}),
                      self.hub.calls)
        self.assertEqual(self.hub.paths("/api/run/hello")[0][0], "POST")
        self.assertEqual(self.hub.paths("/api/jarvis/say")[0][2], {"text": "hi"})
        self.assertEqual(self.hub.paths("/api/notes")[0][2], {"text": "Title\n\nBody", "source": "text"})
        routine = client.get(f"/api/automations/{made['id']}").json()
        self.assertEqual((routine["last_status"], routine["last_run"]), ("ok", "2026-11-04T09:00:00"))
        runs = client.get(f"/api/automations/{made['id']}/runs").json()["runs"]
        self.assertEqual(runs[0]["run_id"], out["run_id"])
        self.assertEqual(runs[0]["trigger"], "manual")

    def test_script_path_is_quoted_when_run(self):
        engine = self.engine()
        routine = {"id": "r", "name": "R", "enabled": True, "schedule": None, "confirmed_dangerous": False,
                   "steps": [au.validate_step({"type": "script", "name": "my button"}, 0)]}
        engine.routines.append(routine)

        async def go():
            _, task = engine.start_run(routine, "manual")
            await task
        asyncio.run(go())
        self.assertEqual(self.hub.paths("/api/run/my%20button")[0][0], "POST")

    def test_stop_policy_skips_the_rest(self):
        self.hub.routes[("POST", "/api/jarvis/say")] = (False, "Jarvis is not running. Start it first.")
        client, made, out = self.run_routine([notify_step("a"), {"type": "jarvis_say", "text": "x"},
                                              notify_step("never shown")])
        self.assertEqual(out["status"], "failed")
        self.assertEqual([(r["ok"], r.get("skipped", False)) for r in out["results"]],
                         [(True, False), (False, False), (False, True)])
        self.assertEqual(out["results"][1]["message"], "Jarvis is not running. Start it first.")
        self.assertEqual([n[0] for n in self.notes], ["a"])
        self.assertEqual(client.get(f"/api/automations/{made['id']}").json()["last_status"], "failed")
        self.assertEqual(self.notes[-1][0], "a")                       # a manual failure does not notify

    def test_continue_policy_keeps_going(self):
        self.hub.routes[("POST", "/api/jarvis/say")] = (False, "Jarvis is not running. Start it first.")
        _, _, out = self.run_routine([{**{"type": "jarvis_say", "text": "x"}, "on_error": "continue"},
                                      notify_step("after")])
        self.assertEqual(out["status"], "partial")
        self.assertEqual([r["ok"] for r in out["results"]], [False, True])
        self.assertEqual([n[0] for n in self.notes], ["after"])

    def test_script_with_a_failing_exit_code_fails_the_step(self):
        self.hub.routes[("POST", "/api/run/hello")] = (True, {"exit": 3, "output": "boom"})
        _, _, out = self.run_routine([{"type": "script", "name": "hello"}])
        self.assertFalse(out["results"][0]["ok"])
        self.assertIn("exit code 3", out["results"][0]["message"])

    def test_a_crashing_step_is_a_failed_step_not_a_crashed_run(self):
        def explode(body):
            raise RuntimeError("secret internals")
        self.hub.routes[("POST", "/api/notes")] = explode
        _, _, out = self.run_routine([{"type": "note", "title": "x"}])
        self.assertEqual(out["status"], "failed")
        self.assertNotIn("secret", out["results"][0]["message"])

    def test_briefing_goes_into_a_notification_and_tolerates_a_missing_feature(self):
        self.hub.routes[("GET", "/api/inbox/briefing")] = (True, {"text": "3 emails, 2 meetings"})
        _, _, out = self.run_routine([{"type": "briefing"}], name="B1")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.notes, [("Your briefing", "3 emails, 2 meetings", "info", "automations")])
        self.notes.clear()
        del self.hub.routes[("GET", "/api/inbox/briefing")]
        _, _, out = self.run_routine([{"type": "briefing"}], name="B2")
        self.assertEqual((out["status"], out["results"][0]["ok"], out["results"][0].get("skipped")), ("ok", True, True))
        self.assertEqual(self.notes, [])

    def test_reminder_tolerates_a_missing_feature_and_uses_the_quick_route(self):
        _, _, out = self.run_routine([{"type": "reminder", "text": "Stretch", "in_minutes": 10}], name="R1")
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["results"][0]["skipped"])
        self.hub.routes[("POST", "/api/reminders/quick")] = (True, {"id": "1"})
        _, _, out = self.run_routine([{"type": "reminder", "text": "Stretch", "in_minutes": 10}], name="R2")
        self.assertNotIn("skipped", out["results"][0])
        self.assertEqual(self.hub.paths("/api/reminders/quick")[-1][2], {"text": "Stretch", "in_minutes": 10})

    def test_double_run_is_a_409(self):
        async def slow(_seconds):
            await asyncio.sleep(0.4)
        client = self.build(sleep=slow)
        made = self.create(client, "Slow", [{"type": "wait", "seconds": 1}])
        first = client.post(f"/api/automations/{made['id']}/run", json={"wait": False})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "running")
        second = client.post(f"/api/automations/{made['id']}/run", json={})
        self.assertEqual(second.status_code, 409)
        self.assertIn("already running", second.json()["detail"])
        self.assertTrue(client.get(f"/api/automations/{made['id']}").json()["running"])
        self.assertEqual(client.delete(f"/api/automations/{made['id']}").status_code, 409)
        self.assertEqual(client.put(f"/api/automations/{made['id']}", json={"name": "x"}).status_code, 409)
        import time
        for _ in range(50):
            runs = client.get(f"/api/automations/{made['id']}/runs").json()["runs"]
            if runs[0]["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(runs[0]["status"], "ok")
        again = client.post(f"/api/automations/{made['id']}/run", json={})        # the lock is released
        self.assertEqual(again.status_code, 200)

    def test_engine_lock_directly(self):
        engine = self.engine()
        routine = {"id": "r", "name": "R", "enabled": True, "schedule": None, "confirmed_dangerous": False,
                   "steps": [au.validate_step(notify_step(), 0)]}
        engine.routines.append(routine)

        async def go():
            _, task = engine.start_run(routine, "manual")
            with self.assertRaises(HTTPException) as caught:
                engine.start_run(routine, "manual")
            self.assertEqual(caught.exception.status_code, 409)
            await task
            _, task = engine.start_run(routine, "manual")          # free again
            await task
        asyncio.run(go())

    def test_only_the_last_20_runs_are_kept(self):
        client = self.build()
        made = self.create(client, "Many")
        for _ in range(23):
            client.post(f"/api/automations/{made['id']}/run", json={})
        runs = client.get(f"/api/automations/{made['id']}/runs").json()["runs"]
        self.assertEqual(len(runs), 20)
        self.assertEqual(len({r["run_id"] for r in runs}), 20)

    def test_disabled_routines_can_still_be_run_by_hand(self):
        client = self.build()
        made = self.create(client, "Off", enabled=False)
        self.assertEqual(client.post(f"/api/automations/{made['id']}/run", json={}).status_code, 200)

    def test_enable_route(self):
        client = self.build()
        made = self.create(client, "Toggle", schedule={"kind": "daily", "time": "10:00"})
        off = client.post(f"/api/automations/{made['id']}/enable", json={"enabled": False}).json()
        self.assertEqual((off["enabled"], off["next_run"]), (False, None))
        on = client.post(f"/api/automations/{made['id']}/enable").json()
        self.assertEqual((on["enabled"], on["next_run"]), (True, "2026-11-04T10:00"))
        self.assertEqual(client.post("/api/automations/nope/enable").status_code, 404)

    def test_wait_is_capped_even_if_the_file_is_edited(self):
        with self.assertRaises(au.Bad):
            au.validate_step({"type": "wait", "seconds": 3600}, 0)


# ---- dangerous steps ------------------------------------------------------------------------------------------------

class Dangerous(Base):
    def test_a_dangerous_step_needs_confirm_to_save(self):
        client = self.build()
        for action in ["lock_screen", "sleep", "open_path", "future_danger"]:     # the last: only the registry knows
            with self.subTest(action=action):
                body = {"name": action, "steps": [{"type": "laptop_action", "action_id": action,
                                                    "params": {"path": "C:\\x"} if action == "open_path" else {}}]}
                r = client.post("/api/automations", json=body)
                self.assertEqual(r.status_code, 409, r.text)
                self.assertIn("Confirm it and save again", r.json()["detail"])
                r = client.post("/api/automations", json={**body, "confirm": True})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertTrue(r.json()["confirmed_dangerous"])

    def test_confirmed_dangerous_cannot_be_claimed_by_the_request(self):
        client = self.build()
        r = client.post("/api/automations", json={"name": "x", "confirmed_dangerous": True,
                                                  "steps": [{"type": "laptop_action", "action_id": "lock_screen"}]})
        self.assertEqual(r.status_code, 409)
        r = client.post("/api/automations", json={"name": "safe", "steps": [notify_step()], "confirm": True})
        self.assertFalse(r.json()["confirmed_dangerous"])

    def test_editing_a_safe_routine_to_add_a_dangerous_step_asks_again(self):
        client = self.build()
        made = self.create(client, "Grow")
        steps = [notify_step(), {"type": "laptop_action", "action_id": "sleep"}]
        self.assertEqual(client.put(f"/api/automations/{made['id']}", json={"steps": steps}).status_code, 409)
        ok = client.put(f"/api/automations/{made['id']}", json={"steps": steps, "confirm": True})
        self.assertTrue(ok.json()["confirmed_dangerous"])
        back = client.put(f"/api/automations/{made['id']}", json={"steps": [notify_step()]})
        self.assertFalse(back.json()["confirmed_dangerous"])

    def test_a_dangerous_step_is_sent_to_the_laptop_with_confirm_only_when_the_routine_was_confirmed(self):
        client = self.build()
        made = self.create(client, "Away", [{"type": "laptop_action", "action_id": "lock_screen"}], confirm=True)
        out = client.post(f"/api/automations/{made['id']}/run", json={}).json()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.hub.paths("/api/laptop/run")[-1][2],
                         {"action": "lock_screen", "params": {}, "confirm": True})

    def test_a_dangerous_step_in_an_unconfirmed_routine_is_refused_at_run_time(self):
        client = self.build()
        made = self.create(client, "Tampered")
        engine = au.ENGINES[id(self.ctx)]
        routine = engine.find(made["id"])
        routine["steps"].append(au.validate_step({"type": "laptop_action", "action_id": "sleep"}, 1))
        out = client.post(f"/api/automations/{made['id']}/run", json={}).json()
        self.assertEqual(out["status"], "failed")
        self.assertIn("saved with your confirmation", out["results"][1]["message"])
        self.assertEqual(self.hub.paths("/api/laptop/run"), [])

    def test_shutdown_restart_and_kill_are_never_scheduled(self):
        client = self.build()
        for action, params in [("shutdown", {}), ("restart", {}), ("kill_process", {"pid": 1234})]:
            with self.subTest(action=action):
                body = {"name": action, "confirm": True,
                        "steps": [{"type": "laptop_action", "action_id": action, "params": params}]}
                r = client.post("/api/automations", json={**body, "schedule": {"kind": "daily", "time": "23:00"}})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("can't run on a schedule", r.json()["detail"])
                r = client.post("/api/automations", json=body)           # by hand is allowed
                self.assertEqual(r.status_code, 200, r.text)
                routine_id = r.json()["id"]
                r = client.put(f"/api/automations/{routine_id}", json={"schedule": {"kind": "weekdays", "time": "23:00"}})
                self.assertEqual(r.status_code, 400)
                self.assertIsNone(client.get(f"/api/automations/{routine_id}").json()["schedule"])

    def test_a_never_scheduled_step_needs_confirm_at_run_time(self):
        client = self.build()
        made = self.create(client, "Off", [{"type": "laptop_action", "action_id": "shutdown"}], confirm=True)
        r = client.post(f"/api/automations/{made['id']}/run", json={})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.hub.paths("/api/laptop/run"), [])
        self.assertEqual(client.get(f"/api/automations/{made['id']}/runs").json()["runs"], [])
        r = client.post(f"/api/automations/{made['id']}/run", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ok")
        self.assertEqual(self.hub.paths("/api/laptop/run")[-1][2], {"action": "shutdown", "params": {}, "confirm": True})

    def test_the_scheduler_itself_refuses_a_never_scheduled_step(self):
        engine = self.engine()
        engine.routines.append({
            "id": "bad", "name": "Bad", "enabled": True, "confirmed_dangerous": True, "last_slot": None,
            "schedule": {"kind": "daily", "time": "09:00"}, "schedule_anchor": None,
            "steps": [au.validate_step({"type": "laptop_action", "action_id": "shutdown"}, 0)]})
        self.clock.now = D(2026, 11, 4, 9, 1)
        asyncio.run(engine.tick())
        self.assertEqual(self.hub.paths("/api/laptop/run"), [])
        self.assertEqual(self.notes[0][2], "warn")
        self.assertIn(("automation_skipped", {"name": "Bad", "reason": "not_schedulable"}), self.audits)

    def test_unknown_laptop_actions_are_refused_at_save_when_the_registry_is_known(self):
        client = self.build()
        r = client.post("/api/automations", json={"name": "x", "steps": [{"type": "laptop_action",
                                                                         "action_id": "format_disk"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no action called", r.json()["detail"])
        r = client.post("/api/automations", json={"name": "y", "steps": [{"type": "script", "name": "nope"}]})
        self.assertEqual(r.status_code, 400)

    def test_when_the_registry_is_unreachable_the_built_in_rules_still_apply(self):
        self.hub.routes[("GET", "/api/laptop/actions")] = (False, "down")
        client = self.build()
        r = client.post("/api/automations", json={"name": "x", "steps": [{"type": "laptop_action", "action_id": "sleep"}]})
        self.assertEqual(r.status_code, 409)
        r = client.post("/api/automations", json={"name": "y", "confirm": True,
                                                  "schedule": {"kind": "daily", "time": "09:00"},
                                                  "steps": [{"type": "laptop_action", "action_id": "restart"}]})
        self.assertEqual(r.status_code, 400)


# ---- the scheduler -----------------------------------------------------------------------------------------------------

class Scheduler(Base):
    def setup_engine(self, schedule, name="Sched", steps=None, enabled=True):
        engine = self.engine()
        ctx_client = None  # routines are created through the engine so the slot state is real

        async def make():
            return await engine.write(None, {"name": name, "steps": steps or [notify_step("tick")],
                                            "schedule": schedule, "enabled": enabled},
                                      {"name", "steps", "schedule", "enabled"}, False)
        routine = asyncio.run(make())
        return engine, routine

    async def tick_at(self, engine, when):
        self.clock.now = when
        await engine.tick()
        await asyncio.gather(*engine.tasks)

    def test_fires_once_per_slot(self):
        engine, routine = self.setup_engine({"kind": "daily", "time": "09:30"})

        async def go():
            await self.tick_at(engine, D(2026, 11, 4, 9, 29, 40))
            self.assertEqual(self.notes, [])
            for second in (0, 20, 40):                                   # many ticks inside and after the slot
                await self.tick_at(engine, D(2026, 11, 4, 9, 30, second))
            await self.tick_at(engine, D(2026, 11, 4, 9, 35))
            self.assertEqual([n[0] for n in self.notes], ["tick"])
            await self.tick_at(engine, D(2026, 11, 5, 9, 30))             # next day: once more
            self.assertEqual(len(self.notes), 2)
        asyncio.run(go())
        self.assertEqual(len(engine.history(routine["id"])), 2)
        self.assertEqual({r["trigger"] for r in engine.history(routine["id"])}, {"schedule"})

    def test_two_ticks_at_the_same_moment_run_once(self):
        engine, _ = self.setup_engine({"kind": "daily", "time": "09:30"})

        async def go():
            self.clock.now = D(2026, 11, 4, 9, 30)
            await asyncio.gather(engine.tick(), engine.tick())
            await asyncio.gather(*engine.tasks)
        asyncio.run(go())
        self.assertEqual(len(self.notes), 1)

    def test_a_restart_inside_the_slot_does_not_fire_again(self):
        engine, routine = self.setup_engine({"kind": "daily", "time": "09:30"})
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 31)))
        reborn = self.engine(engine.ctx)
        asyncio.run(self.tick_at(reborn, D(2026, 11, 4, 9, 32)))
        self.assertEqual(len(self.notes), 1)

    def test_missed_by_more_than_10_minutes_is_skipped(self):
        engine, routine = self.setup_engine({"kind": "daily", "time": "09:30"})
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 41)))          # hub was down until 11 minutes late
        self.assertEqual(self.notes, [])
        self.assertEqual(engine.history(routine["id"]), [])
        self.assertTrue(any(e == "automation_skipped" and f["reason"] == "missed" for e, f in self.audits))
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 42)))          # and it is not retried
        self.assertEqual(self.notes, [])

    def test_missed_by_less_than_10_minutes_still_runs(self):
        engine, _ = self.setup_engine({"kind": "daily", "time": "09:30"})
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 39, 59)))
        self.assertEqual(len(self.notes), 1)

    def test_a_new_schedule_does_not_fire_for_an_earlier_slot(self):
        self.clock.now = D(2026, 11, 4, 9, 5)
        engine, _ = self.setup_engine({"kind": "daily", "time": "09:00"})   # created 5 minutes after 09:00
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 6)))
        self.assertEqual(self.notes, [])

    def test_weekdays_do_not_fire_on_the_weekend(self):
        self.clock.now = D(2026, 11, 6, 8, 0)                               # Friday
        engine, _ = self.setup_engine({"kind": "weekdays", "time": "09:00"})

        async def go():
            await self.tick_at(engine, D(2026, 11, 6, 9, 0))
            await self.tick_at(engine, D(2026, 11, 7, 9, 0))
            await self.tick_at(engine, D(2026, 11, 8, 9, 0))
            self.assertEqual(len(self.notes), 1)
            await self.tick_at(engine, D(2026, 11, 9, 9, 0))
            self.assertEqual(len(self.notes), 2)
        asyncio.run(go())

    def test_interval_fires_every_n_minutes_once_each(self):
        engine, _ = self.setup_engine({"kind": "interval", "every_minutes": 15})

        async def go():
            await self.tick_at(engine, D(2026, 11, 4, 9, 14, 40))
            self.assertEqual(len(self.notes), 0)
            await self.tick_at(engine, D(2026, 11, 4, 9, 15))
            await self.tick_at(engine, D(2026, 11, 4, 9, 15, 40))
            self.assertEqual(len(self.notes), 1)
            await self.tick_at(engine, D(2026, 11, 4, 9, 30))
            self.assertEqual(len(self.notes), 2)
            await self.tick_at(engine, D(2026, 11, 4, 12, 12))              # a long gap: the latest slot was 12 minutes ago, skipped
            self.assertEqual(len(self.notes), 2)
        asyncio.run(go())

    def test_once_fires_once_and_switches_itself_off(self):
        engine, routine = self.setup_engine({"kind": "once", "at": "2026-11-04T10:00"})

        async def go():
            await self.tick_at(engine, D(2026, 11, 4, 10, 0))
            await self.tick_at(engine, D(2026, 11, 4, 10, 1))
            await self.tick_at(engine, D(2026, 11, 5, 10, 0))
        asyncio.run(go())
        self.assertEqual(len(self.notes), 1)
        self.assertFalse(routine["enabled"])

    def test_disabled_routines_never_fire(self):
        engine, _ = self.setup_engine({"kind": "daily", "time": "09:30"}, enabled=False)
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 30)))
        self.assertEqual(self.notes, [])

    def test_re_enabling_does_not_replay_a_slot_that_passed_while_off(self):
        engine, routine = self.setup_engine({"kind": "daily", "time": "09:30"}, enabled=False)
        self.clock.now = D(2026, 11, 4, 9, 35)
        engine.set_enabled(routine, True)
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 36)))
        self.assertEqual(self.notes, [])
        asyncio.run(self.tick_at(engine, D(2026, 11, 5, 9, 30)))
        self.assertEqual(len(self.notes), 1)

    def test_a_scheduled_run_that_fails_notifies_with_a_warning(self):
        self.hub.routes[("POST", "/api/jarvis/say")] = (False, "Jarvis is not running. Start it first.")
        engine, _ = self.setup_engine({"kind": "daily", "time": "09:30"}, name="Morning",
                                      steps=[{"type": "jarvis_say", "text": "hi"}])
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 30)))
        self.assertEqual(self.notes, [("Routine 'Morning' had a problem", "Jarvis is not running. Start it first.",
                                       "warn", "automations")])

    def test_a_scheduled_run_while_the_routine_is_running_is_skipped_not_doubled(self):
        engine, routine = self.setup_engine({"kind": "daily", "time": "09:30"})
        engine.running[routine["id"]] = "other"
        asyncio.run(self.tick_at(engine, D(2026, 11, 4, 9, 30)))
        self.assertEqual(self.notes, [])
        self.assertTrue(any(f.get("reason") == "already_running" for _, f in self.audits))


# ---- audit, demo, source hygiene ----------------------------------------------------------------------------------------

class Hygiene(Base):
    def test_audit_has_names_and_step_types_but_not_step_text(self):
        client = self.build()
        long_text = "TOPSECRET " * 40
        made = self.create(client, "Audited", [{"type": "jarvis_say", "text": long_text},
                                              {"type": "note", "title": "Private title", "body": long_text}])
        client.post(f"/api/automations/{made['id']}/run", json={})
        client.delete(f"/api/automations/{made['id']}")
        events = [e for e, _ in self.audits]
        for needed in ("automation_save", "automation_run", "automation_run_done", "automation_delete"):
            self.assertIn(needed, events)
        dumped = json.dumps(self.audits)
        self.assertNotIn("TOPSECRET", dumped)
        self.assertNotIn("Private title", dumped)
        save = next(f for e, f in self.audits if e == "automation_save")
        self.assertEqual((save["name"], save["steps"]), ("Audited", ["jarvis_say", "note"]))

    def test_the_module_never_builds_shell_commands(self):
        source = (Path(au.__file__)).read_text(encoding="utf-8")
        for word in ("subprocess", "os.system", "shell=True", "Popen", "create_subprocess"):
            self.assertNotIn(word, source)

    def test_demo_seeds_three_templates_once(self):
        import os
        os.environ["HUB_DEMO"] = "1"
        self.addCleanup(os.environ.pop, "HUB_DEMO", None)
        ctx = self.make_ctx()

        async def go():
            stop = await au.start(ctx)
            await stop()
            stop = await au.start(ctx)
            await stop()
        asyncio.run(go())
        engine = self.engine(ctx)
        self.assertEqual([r["name"] for r in engine.routines], ["Good morning", "Study mode", "Leaving desk"])
        self.assertTrue(engine.routines[2]["confirmed_dangerous"])
        self.assertEqual(engine.routines[0]["schedule"], {"kind": "weekdays", "time": "08:30"})

    def test_start_returns_a_stop_that_ends_the_scheduler(self):
        ctx = self.make_ctx()

        async def go():
            stop = await au.start(ctx)
            await asyncio.sleep(0)
            await stop()
        asyncio.run(go())

    def test_a_run_that_is_cancelled_is_marked_interrupted(self):
        async def forever(_s):
            await asyncio.sleep(60)
        engine = au.Engine(self.make_ctx(), caller=self.hub, clock=self.clock, sleep=forever)
        routine = {"id": "r", "name": "R", "enabled": True, "schedule": None, "confirmed_dangerous": False,
                   "steps": [au.validate_step({"type": "wait", "seconds": 5}, 0)]}
        engine.routines.append(routine)

        async def go():
            engine.start_run(routine, "manual")
            await asyncio.sleep(0.05)
            await engine.stop()
        asyncio.run(go())
        self.assertEqual(engine.history("r")[0]["status"], "interrupted")
        self.assertEqual(engine.running, {})


# ---- through the real app ---------------------------------------------------------------------------------------------------

class RealApp(unittest.TestCase):
    def test_a_routine_runs_through_the_real_routes(self):
        from hub.server import create_app
        config, root = make_config(data_dir=None)
        config.data_dir = str(root / "data")
        client = TestClient(create_app(config, audit_path=root / "audit.log"), base_url="http://testserver",
                            headers={"X-Hub": "1"})
        with client:
            self.assertEqual(client.get("/api/automations").status_code, 401)
            self.assertEqual(client.post("/api/login", json={"token": TOKEN}).status_code, 200)
            r = client.post("/api/automations", json={"name": "Real", "steps": [
                {"type": "script", "name": "hello"}, {"type": "note", "title": "From a routine", "body": "hi"},
                notify_step("Done")]})
            self.assertEqual(r.status_code, 200, r.text)
            out = client.post(f"/api/automations/{r.json()['id']}/run", json={}).json()
            self.assertEqual(out["status"], "ok", out)
            self.assertIn("hello from a button", out["results"][0]["message"])
            titles = [n["title"] for n in client.get("/api/notes").json()["notes"]]
            self.assertTrue(any("From a routine" in t for t in titles), titles)
            self.assertEqual(client.post("/api/automations", json={"name": "x", "steps": [
                {"type": "script", "name": "missing"}]}).status_code, 400)
            lines = (root / "audit.log").read_text().splitlines()
            self.assertTrue(any('"automation_run"' in line for line in lines))


if __name__ == "__main__":
    unittest.main()
