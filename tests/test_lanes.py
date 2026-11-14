"""Work lanes (hub/lanes.py, hub/lanes_tools.py). Fakes only: a fake Council (no lab, no process), fake model clients, a
fake deep-thinking port, a temp data folder. No network, no real AI."""
import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request

from hub import brain, lanes, lanes_tools, persona, usage
from hub.assistant import TOOL_GROUPS, route_local_tools
from hub.common import Ctx
from hub.council import CouncilError
from hub.llm import LLMError
from tests.helpers import make_config


def need_auth(request: Request):
    if not request.cookies.get("hub_session"):
        raise HTTPException(401, "Log in first.")


class FakeCouncil:
    def __init__(self):
        self.ok, self.why = True, ""
        self.busy = False
        self.bodies, self.jobs, self.cancelled = [], {}, []
        self.local = ["qwen"]
        self.n = 0

    def ready(self):
        return self.ok, self.why

    def allow_claude(self):
        return False

    async def local_models(self):
        return list(self.local)

    def start(self, body):
        if self.busy:
            raise CouncilError("A council run is already going.", 409)
        self.n += 1
        job_id = f"{self.n:012x}"
        self.bodies.append(body)
        self.jobs[job_id] = {"id": job_id, "status": "running", "elapsed_s": 1, "log_tail": "round 1", "result": None, "error": ""}
        return dict(self.jobs[job_id])

    def get(self, job_id):
        return dict(self.jobs[job_id])

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.jobs[job_id]["status"] = "cancelled"

    def finish(self, job_id, status="accepted", answer="Use a heap.", stopped=None, error=""):
        self.jobs[job_id].update(status=status, error=error,
                                 result={"status": status if status in ("accepted", "failed", "stopped") else "failed",
                                         "answer": answer, "files": [], "calls": {"qwen": 3}, "stopped": stopped})


class FakeClient:
    def __init__(self, text="quick answer", error=None, hang=False):
        self.text, self.error, self.hang, self.calls = text, error, hang, []

    async def complete(self, system, user, json_mode=False):
        self.calls.append(user)
        if self.hang:
            await asyncio.sleep(30)
        if self.error:
            raise self.error
        return self.text


class FakeThink:
    def __init__(self, answer="deep answer", ok=True):
        self.answer, self.ok, self.tasks = answer, ok, []

    def ready(self):
        return True, ""

    def start(self, task):
        self.tasks.append(task)
        return "think1"

    def get(self, job_id):
        return {"status": "done" if self.ok else "failed", "answer": self.answer, "error": "no model"}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lanes-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits, self.pushed = [], []
        self.council = FakeCouncil()
        self.clients = {"local": FakeClient("local answer"), "gemini": FakeClient("gemini answer"),
                        "claude-login": FakeClient("claude answer")}
        self.think = FakeThink()
        self.app = FastAPI()
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda e, **k: self.audits.append((e, k)), get_app=lambda: self.app,
                       notify=lambda title, body="", level="info", link=None, source="": self.pushed.append((title, body)))
        self.build()

    def build(self, **kw):
        opts = dict(council=self.council, think=self.think, model_for=lambda kind: self.clients[kind],
                    ready_check=lambda lane: "", poll=0.01)
        opts.update(kw)
        self.app = FastAPI()
        self.router = lanes.make_router(self.ctx, **opts)
        self.svc = self.router.service
        self.app.include_router(self.router)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t",
                                      cookies={"hub_session": "yes"})
        self.addAsyncCleanup(self.http.aclose)
        self.addAsyncCleanup(self.svc.shutdown)

    async def until(self, run_id, status=None, tries=300):
        for _ in range(tries):
            run = self.svc.get(run_id)
            if (run["status"] != "running") if status is None else run["status"] == status:
                return self.svc.view(run)
            await asyncio.sleep(0.01)
        self.fail(f"run stuck: {self.svc.view(self.svc.get(run_id))}")

    async def go(self, **body):
        r = await self.http.post("/api/lanes/run", json={"task": "summarise this", **body})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def set_class(self, ident, lane_ids, **kw):
        self.svc.store.put_class(ident, {"lanes": lane_ids, **kw})


class ListsCrud(Base):
    async def test_ready_made_lanes_and_classes(self):
        d = (await self.http.get("/api/lanes")).json()
        by = {x["id"]: x for x in d["lanes"]}
        self.assertEqual((by["council"]["cost"], by["local"]["cost"]), ("free", "free"))
        self.assertIn(by["gemini"]["cost"], ("limited", "paid"))
        self.assertIn("claude-login", by)
        classes = {c["id"]: c for c in d["classes"]}
        self.assertEqual(classes["routine"]["lanes"], ["council", "local", "gemini"])
        self.assertEqual(classes["private"]["lanes"], ["council", "local"])
        self.assertTrue(classes["private"]["private"])
        self.assertEqual(classes["hard-reasoning"]["lanes"][0], "local-reasoning")
        self.assertEqual(classes["coding"]["lanes"], ["council", "claude-login"])
        self.assertEqual(d["saved"]["week"], 0)

    async def test_lane_crud_and_validation(self):
        r = await self.http.post("/api/lanes", json={"id": "mine", "label": "My endpoint", "kind": "endpoint:home", "cost": "free",
                                                    "daily_cap": 5, "notes": "box under the desk"})
        self.assertEqual(r.status_code, 200)
        mine = next(x for x in r.json()["lanes"] if x["id"] == "mine")
        self.assertEqual((mine["kind"], mine["daily_cap"]), ("endpoint:home", 5))
        for bad in ({"id": "x", "label": "X", "kind": "nope"}, {"id": "x", "label": "X", "kind": "local", "cost": "gold"},
                    {"id": "x", "label": "X", "kind": "local", "daily_cap": -1}, {"id": "Bad Id", "label": "X", "kind": "local"},
                    {"id": "x", "label": "", "kind": "local"}, {"id": "x", "label": "X", "kind": "cli:bad id"}):
            self.assertEqual((await self.http.post("/api/lanes", json=bad)).status_code, 400, bad)
        self.assertEqual((await self.http.post("/api/lanes", json={"id": "mine", "label": "Again", "kind": "local"})).status_code, 409)
        r = await self.http.put("/api/lanes/mine", json={"enabled": False, "daily_cap": 9})
        mine = next(x for x in r.json()["lanes"] if x["id"] == "mine")
        self.assertEqual((mine["enabled"], mine["daily_cap"], mine["label"]), (False, 9, "My endpoint"))
        self.assertEqual((await self.http.put("/api/lanes/ghost", json={})).status_code, 404)
        r = await self.http.delete("/api/lanes/mine")
        self.assertNotIn("mine", [x["id"] for x in r.json()["lanes"]])
        self.assertEqual((await self.http.delete("/api/lanes/mine")).status_code, 404)

    async def test_deleting_a_lane_drops_it_from_every_class(self):
        r = await self.http.delete("/api/lanes/gemini")
        routine = next(c for c in r.json()["classes"] if c["id"] == "routine")
        self.assertEqual(routine["lanes"], ["council", "local"])

    async def test_class_crud_order_and_validation(self):
        r = await self.http.post("/api/lanes/classes", json={"id": "mail", "label": "Mail drafts", "lanes": ["local", "council"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(next(c for c in r.json()["classes"] if c["id"] == "mail")["lanes"], ["local", "council"])
        self.assertEqual((await self.http.post("/api/lanes/classes", json={"id": "bad", "label": "B", "lanes": ["ghost"]})).status_code, 400)
        self.assertEqual((await self.http.post("/api/lanes/classes", json={"id": "mail", "label": "B", "lanes": []})).status_code, 409)
        r = await self.http.put("/api/lanes/classes/mail", json={"lanes": ["council", "gemini", "local"], "private": True})
        mail = next(c for c in r.json()["classes"] if c["id"] == "mail")
        self.assertEqual((mail["lanes"], mail["private"]), (["council", "gemini", "local"], True))
        self.assertEqual((await self.http.put("/api/lanes/classes/ghost", json={})).status_code, 404)
        self.assertEqual((await self.http.delete("/api/lanes/classes/mail")).status_code, 200)
        for c in [c["id"] for c in self.svc.store.classes()][:-1]:
            await self.http.delete(f"/api/lanes/classes/{c}")
        last = self.svc.store.classes()[0]["id"]
        self.assertEqual((await self.http.delete(f"/api/lanes/classes/{last}")).status_code, 409)

    async def test_reset_restores_the_ready_made_lists(self):
        await self.http.delete("/api/lanes/gemini")
        await self.http.delete("/api/lanes/classes/coding")
        d = (await self.http.post("/api/lanes/reset")).json()
        self.assertIn("gemini", [x["id"] for x in d["lanes"]])
        self.assertIn("coding", [c["id"] for c in d["classes"]])

    async def test_switched_on_cli_providers_appear_as_lanes(self):
        d = (await self.http.get("/api/lanes")).json()
        cli = [x for x in d["lanes"] if x["kind"].startswith("cli:")]
        self.assertTrue(cli, "the ready-made gemini-cli entry is on")
        self.assertTrue(all(x["auto"] and x["enabled"] for x in cli))
        self.assertEqual((await self.http.delete(f"/api/lanes/{cli[0]['id']}")).status_code, 409)
        await self.http.put(f"/api/lanes/{cli[0]['id']}", json={"cost": "free"})       # an edit pins it as the owner's own
        pinned = next(x for x in (await self.http.get("/api/lanes")).json()["lanes"] if x["id"] == cli[0]["id"])
        self.assertEqual(pinned["cost"], "free")
        self.assertFalse(pinned.get("auto"))

    async def test_lists_survive_a_new_service(self):
        await self.http.post("/api/lanes", json={"id": "mine", "label": "Mine", "kind": "local", "cost": "free"})
        self.build()
        self.assertIn("mine", [x["id"] for x in self.svc.store.lanes()])


class Escalation(Base):
    async def test_council_first_returns_a_job_id_then_accepted_is_saved(self):
        run = await self.go()
        self.assertTrue(run["running"])
        self.assertEqual(run["class"], "routine")
        job = run["council_job_id"]
        self.assertRegex(job, r"^[0-9a-f]{12}$")
        self.assertEqual(self.council.bodies[0]["task"], "summarise this")
        self.assertNotIn("only", self.council.bodies[0])
        self.council.finish(job)
        done = await self.until(run["id"])
        self.assertEqual((done["status"], done["lane"], done["saved"], done["answer"]), ("done", "council", True, "Use a heap."))
        self.assertEqual(self.clients["gemini"].calls, [])
        self.assertEqual(self.pushed, [])               # the council notifies for its own run

    async def test_council_not_set_up_falls_to_local_and_notes_why(self):
        self.council.ok, self.council.why = False, "Council is not set up on this laptop."
        run = await self.go()
        self.assertEqual((run["status"], run["lane"], run["saved"]), ("done", "local", True))
        skipped = run["attempts"][0]
        self.assertEqual((skipped["lane"], skipped["status"]), ("council", "skipped"))
        self.assertIn("not set up", skipped["why"])
        self.assertEqual(self.council.bodies, [])

    async def test_busy_council_counts_as_unavailable(self):
        self.council.busy = True
        run = await self.go()
        self.assertEqual(run["lane"], "local")
        self.assertIn("already going", run["attempts"][0]["why"])

    async def test_verify_failure_escalates_with_the_reason(self):
        run = await self.go(verify="pytest -q")
        self.assertEqual(self.council.bodies[0]["verify_command"], "pytest -q")
        self.council.finish(run["council_job_id"], status="failed", answer="half done")
        done = await self.until(run["id"])
        self.assertEqual((done["status"], done["lane"]), ("done", "local"))
        council_attempt, local_attempt = done["attempts"][0], done["attempts"][1]
        self.assertEqual(council_attempt["status"], "failed")
        self.assertIn("verify", council_attempt["why"])
        self.assertIn("verify", local_attempt["escalated_because"])
        self.assertTrue(done["saved"])                  # local is free

    async def test_stopped_and_no_answer_escalate(self):
        for status, answer, stopped, word in (("stopped", "", "ran out of budget", "stopped"), ("accepted", "  ", None, "no answer")):
            run = await self.go()
            self.council.finish(run["council_job_id"], status=status, answer=answer, stopped=stopped)
            done = await self.until(run["id"])
            self.assertEqual(done["lane"], "local", status)
            self.assertIn(word, done["attempts"][0]["why"], status)

    async def test_timeout_and_interrupted_council_escalate(self):
        run = await self.go()
        self.council.finish(run["council_job_id"], status="timeout")
        done = await self.until(run["id"])
        self.assertEqual(done["lane"], "local")
        self.assertIn("timed out", done["attempts"][0]["why"])

    async def test_quick_lane_failure_walks_the_chain_and_a_limited_answer_is_not_saved(self):
        self.set_class("routine", ["local", "gemini", "claude-login"])
        self.clients["local"].error = LLMError("Ollama is not running.")
        self.clients["gemini"].error = RuntimeError("quota")
        run = await self.go()
        self.assertEqual((run["status"], run["lane"], run["saved"]), ("done", "claude-login", False))
        self.assertEqual([a["status"] for a in run["attempts"]], ["failed", "failed", "accepted"])
        self.assertIn("Ollama", run["attempts"][0]["why"])
        self.assertIn("quota", run["attempts"][1]["why"])
        self.assertIn("quota", run["attempts"][2]["escalated_because"])
        self.assertTrue(any("finished" in t for t, _ in self.pushed) or self.pushed == [])

    async def test_quick_lane_timeout_escalates(self):
        self.build(quick_timeout=0.05)
        self.set_class("routine", ["local", "gemini"])
        self.clients["local"].hang = True
        run = await self.go()
        self.assertEqual(run["lane"], "gemini")
        self.assertIn("longer than", run["attempts"][0]["why"])

    async def test_empty_answer_counts_as_failure(self):
        self.set_class("routine", ["local", "gemini"])
        self.clients["local"].text = "   "
        run = await self.go()
        self.assertEqual(run["lane"], "gemini")
        self.assertIn("no answer", run["attempts"][0]["why"])

    async def test_all_lanes_failing_ends_failed_with_every_reason(self):
        self.set_class("routine", ["local", "gemini"])
        self.clients["local"].error = LLMError("local down")
        self.clients["gemini"].error = LLMError("gemini down")
        run = await self.go()
        self.assertEqual(run["status"], "failed")
        self.assertIn("local down", run["note"])
        self.assertIn("gemini down", run["note"])
        self.assertFalse(run["saved"])

    async def test_explicit_lane_never_escalates(self):
        self.clients["local"].error = LLMError("local down")
        run = await self.go(lane="local")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.clients["gemini"].calls, [])
        self.assertEqual(len(run["attempts"]), 1)

    async def test_try_a_stronger_model_goes_to_the_next_lane_and_records_it(self):
        self.set_class("routine", ["local", "gemini", "claude-login"])
        run = await self.go()
        self.assertEqual(run["lane"], "local")
        stronger = (await self.http.post(f"/api/lanes/runs/{run['id']}/stronger")).json()
        self.assertEqual((stronger["status"], stronger["lane"], stronger["saved"]), ("done", "gemini", False))
        self.assertIn("stronger", " ".join(stronger["notes"]))
        self.assertEqual(self.clients["claude-login"].calls, [])
        again = (await self.http.post(f"/api/lanes/runs/{run['id']}/stronger")).json()
        self.assertEqual(again["lane"], "claude-login")
        r = await self.http.post(f"/api/lanes/runs/{run['id']}/stronger")
        self.assertEqual(r.status_code, 409)
        self.assertIn("no stronger lane", r.json()["detail"])

    async def test_stronger_refused_while_running(self):
        run = await self.go()
        self.assertEqual((await self.http.post(f"/api/lanes/runs/{run['id']}/stronger")).status_code, 409)

    async def test_council_lane_can_ask_for_claude_only_through_the_lane_flag(self):
        await self.http.post("/api/lanes", json={"id": "council-claude", "label": "Council + Claude", "kind": "council",
                                                 "escalate_claude": True})
        run = await self.go(lane="council-claude")
        self.assertTrue(self.council.bodies[0]["escalate_claude"])
        self.council.finish(run["council_job_id"])
        await self.until(run["id"])

    async def test_council_refusing_claude_is_just_an_unavailable_lane(self):
        class Refusing(FakeCouncil):
            def start(self, body):
                if body.get("escalate_claude"):
                    raise CouncilError("Claude escalation is switched off.")
                return super().start(body)
        self.council = Refusing()
        self.build(council=self.council)
        self.set_class("routine", ["council", "local"])
        await self.http.post("/api/lanes", json={"id": "council-claude", "label": "Council + Claude", "kind": "council",
                                                 "escalate_claude": True})
        self.set_class("routine", ["council-claude", "local"])
        run = await self.go()
        self.assertEqual(run["lane"], "local")
        self.assertIn("switched off", run["attempts"][0]["why"])

    async def test_hard_reasoning_starts_with_local_deep_thinking(self):
        run = await self.go(**{"class": "hard-reasoning"})
        self.assertEqual((run["status"], run["lane"], run["saved"], run["answer"]), ("done", "local-reasoning", True, "deep answer"))
        self.assertEqual(self.think.tasks, ["summarise this"])
        self.think.ok = False
        run = await self.go(**{"class": "hard-reasoning"})
        self.assertEqual(run["lane"], "gemini")
        self.assertIn("no model", run["attempts"][0]["why"])


class Private(Base):
    async def test_private_council_is_limited_to_local_models(self):
        run = await self.go(**{"class": "private"})
        self.assertEqual(self.council.bodies[0]["only"], ["qwen"])
        self.council.finish(run["council_job_id"])
        self.assertEqual((await self.until(run["id"]))["lane"], "council")

    async def test_private_with_no_local_council_model_does_not_start_the_council(self):
        self.council.local = []
        run = await self.go(**{"class": "private"})
        self.assertEqual(self.council.bodies, [])
        self.assertEqual(run["lane"], "local")

    async def test_private_never_reaches_a_cloud_lane_even_if_the_list_says_so(self):
        self.set_class("private", ["council", "local", "gemini", "claude-login"])
        self.council.ok = False
        self.clients["local"].error = LLMError("local down")
        run = await self.go(**{"class": "private"})
        self.assertEqual(run["status"], "needs_owner")
        self.assertEqual(self.clients["gemini"].calls, [])
        self.assertEqual(self.clients["claude-login"].calls, [])
        self.assertIn("never leaves", run["note"])
        skipped = [a for a in run["attempts"] if a["lane"] in ("gemini", "claude-login")]
        self.assertTrue(skipped and all("laptop" in a["why"] for a in skipped))

    async def test_private_council_failure_goes_only_to_local_never_cloud(self):
        run = await self.go(**{"class": "private"})
        self.council.finish(run["council_job_id"], status="stopped", answer="", stopped="gave up")
        done = await self.until(run["id"])
        self.assertEqual(done["lane"], "local")
        self.assertEqual(self.clients["gemini"].calls, [])

    async def test_explicit_cloud_lane_for_private_class_is_refused(self):
        r = await self.http.post("/api/lanes/run", json={"task": "secret", "class": "private", "lane": "gemini"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.clients["gemini"].calls, [])

    async def test_stronger_on_private_has_no_cloud_lane_to_go_to(self):
        self.set_class("private", ["local", "gemini"])
        run = await self.go(**{"class": "private"})
        self.assertEqual(run["lane"], "local")
        r = await self.http.post(f"/api/lanes/runs/{run['id']}/stronger")
        self.assertEqual(r.status_code, 409)
        self.assertIn("stays on the laptop", r.json()["detail"])
        self.assertEqual(self.clients["gemini"].calls, [])

    async def test_plan_for_private_marks_it(self):
        d = (await self.http.post("/api/lanes/plan", json={"class": "private"})).json()
        self.assertTrue(d["private"])
        self.assertEqual(d["first"]["lane"], "council")


class Caps(Base):
    async def test_exhausted_lane_is_skipped_with_a_note(self):
        self.set_class("routine", ["gemini", "local"])
        await self.http.put("/api/lanes/gemini", json={"daily_cap": 2})
        usage.record(self.config, "gemini", 2)
        run = await self.go()
        self.assertEqual(run["lane"], "local")
        self.assertEqual(run["attempts"][0]["status"], "skipped")
        self.assertIn("daily cap", run["attempts"][0]["why"])
        self.assertEqual(self.clients["gemini"].calls, [])

    async def test_usage_py_caps_apply_too(self):
        self.config.provider_daily_caps = {"gemini": 1}
        usage.record(self.config, "gemini", 1)
        self.set_class("routine", ["gemini", "local"])
        run = await self.go()
        self.assertEqual(run["lane"], "local")
        self.assertIn("cap", run["attempts"][0]["why"])

    async def test_below_the_cap_the_lane_runs(self):
        await self.http.put("/api/lanes/gemini", json={"daily_cap": 3})
        usage.record(self.config, "gemini", 1)
        self.set_class("routine", ["gemini", "local"])
        self.assertEqual((await self.go())["lane"], "gemini")

    async def test_overview_shows_usage_and_why_not(self):
        await self.http.put("/api/lanes/gemini", json={"daily_cap": 1})
        usage.record(self.config, "gemini", 1)
        gem = next(x for x in (await self.http.get("/api/lanes")).json()["lanes"] if x["id"] == "gemini")
        self.assertEqual(gem["used_today"], 1)
        self.assertIn("cap", gem["why_not"])


class Saved(Base):
    async def test_free_lane_answers_are_counted_and_limited_ones_are_not(self):
        await self.go(lane="local")
        await self.go(lane="local")
        await self.go(lane="gemini")
        d = (await self.http.get("/api/lanes/saved")).json()
        self.assertEqual((d["week"], d["today"], d["runs_week"], d["paid_or_limited_week"]), (2, 2, 3, 1))
        self.assertEqual(d["by_lane"], {"local": 2})

    async def test_escalated_answer_is_not_saved_and_old_runs_leave_the_week(self):
        self.set_class("routine", ["local", "gemini"])
        self.clients["local"].error = LLMError("down")
        run = await self.go()
        self.assertFalse(run["saved"])
        self.assertEqual((await self.http.get("/api/lanes/saved")).json()["week"], 0)
        self.clients["local"].error = None
        old = await self.go(lane="local")
        self.svc.runs[old["id"]]["finished"] = time.time() - 8 * 86400
        self.assertEqual((await self.http.get("/api/lanes/saved")).json()["week"], 0)

    async def test_failed_run_saves_nothing(self):
        self.clients["local"].error = LLMError("down")
        await self.go(lane="local")
        self.assertEqual((await self.http.get("/api/lanes/saved")).json()["week"], 0)


class Routes(Base):
    async def test_run_validation(self):
        for body in ({}, {"task": ""}, {"task": "x" * 4001}, {"task": "x", "class": "ghost"}, {"task": "x", "lane": "ghost"},
                     {"task": "x", "verify": "a\nb"}, {"task": "x", "link": {"kind": "other", "id": "1"}}):
            self.assertEqual((await self.http.post("/api/lanes/run", json=body)).status_code, 404 if "ghost" in str(body) else 400, body)

    async def test_run_list_and_get(self):
        run = await self.go()
        d = (await self.http.get(f"/api/lanes/runs/{run['id']}")).json()
        self.assertEqual(d["council"]["status"], "running")
        self.assertIn("round 1", d["council"]["log_tail"])
        rows = (await self.http.get("/api/lanes/runs")).json()["runs"]
        self.assertEqual(rows[0]["id"], run["id"])
        self.assertNotIn("answer", rows[0])
        self.assertEqual((await self.http.get("/api/lanes/runs/nothex")).status_code, 404)
        self.assertEqual((await self.http.get("/api/lanes/runs/" + "0" * 12)).status_code, 404)

    async def test_cancel_stops_the_council_run(self):
        run = await self.go()
        d = (await self.http.post(f"/api/lanes/runs/{run['id']}/cancel")).json()
        self.assertEqual(d["status"], "cancelled")
        self.assertEqual(self.council.cancelled, [run["council_job_id"]])
        self.assertEqual((await self.http.post(f"/api/lanes/runs/{run['id']}/cancel")).status_code, 409)
        await asyncio.sleep(0.05)
        self.assertEqual(self.svc.get(run["id"])["status"], "cancelled")

    async def test_owner_cancelling_in_the_council_screen_ends_the_run_without_escalating(self):
        run = await self.go()
        self.council.jobs[run["council_job_id"]]["status"] = "cancelled"
        done = await self.until(run["id"])
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(self.clients["local"].calls, [])

    async def test_runs_survive_and_running_ones_become_interrupted(self):
        run = await self.go()
        done = await self.go(lane="local")
        self.build()
        self.assertEqual(self.svc.get(run["id"])["status"], "interrupted")
        self.assertEqual(self.svc.get(done["id"])["status"], "done")
        self.assertEqual(self.svc.get(done["id"])["answer"], "local answer")

    async def test_audit_never_holds_the_task_text(self):
        await self.go(task="very secret text", lane="local")
        flat = repr(self.audits)
        self.assertNotIn("very secret", flat)
        self.assertIn("lanes_run", flat)

    async def test_every_route_needs_login(self):
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t")
        self.addAsyncCleanup(anon.aclose)
        rid = "0" * 12
        calls = [("GET", "/api/lanes"), ("GET", "/api/lanes/saved"), ("GET", "/api/lanes/runs"), ("GET", f"/api/lanes/runs/{rid}"),
                 ("POST", "/api/lanes/run"), ("POST", "/api/lanes/plan"), ("POST", "/api/lanes/reset"), ("POST", "/api/lanes"),
                 ("PUT", "/api/lanes/local"), ("DELETE", "/api/lanes/local"), ("POST", "/api/lanes/classes"),
                 ("PUT", "/api/lanes/classes/routine"), ("DELETE", "/api/lanes/classes/routine"),
                 ("POST", f"/api/lanes/runs/{rid}/stronger"), ("POST", f"/api/lanes/runs/{rid}/cancel")]
        for method, path in calls:
            r = await anon.request(method, path, json={})
            self.assertEqual(r.status_code, 401, (method, path))


class BrainLink(Base):
    async def test_send_to_council_links_the_result_back_to_the_task_as_a_note(self):
        import datetime as dt
        self.app = FastAPI()
        self.build()
        self.app.include_router(brain.make_router(self.ctx, clock=lambda: dt.datetime(2026, 11, 9, 15, 0)))
        self.ctx.need_auth = need_auth
        item = (await self.http.post("/api/brain/items", json={"text": "Tidy the release notes", "kind": "task"})).json()
        run = await self.go(task="Tidy the release notes", lane="council", link={"kind": "brain", "id": item["id"]})
        self.council.finish(run["council_job_id"], answer="Notes tidied: three bullets.")
        done = await self.until(run["id"])
        for _ in range(100):
            if self.svc.get(run["id"]).get("linked") not in ("pending", None, ""):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.svc.get(run["id"])["linked"], "yes")
        body = (await self.http.get("/api/brain/items", params={"q": "Tidy", "status": "all"})).json()["items"][0]["body"]
        self.assertIn("Council result: Notes tidied: three bullets.", body)
        self.assertEqual(done["status"], "done")

    async def test_a_failed_run_leaves_a_note_saying_so(self):
        import datetime as dt
        self.build()
        self.app.include_router(brain.make_router(self.ctx, clock=lambda: dt.datetime(2026, 11, 9, 15, 0)))
        item = (await self.http.post("/api/brain/items", json={"text": "Do the thing", "kind": "task"})).json()
        run = await self.go(task="Do the thing", lane="council", link={"kind": "brain", "id": item["id"]})
        self.council.finish(run["council_job_id"], status="stopped", answer="", stopped="gave up")
        await self.until(run["id"])
        for _ in range(100):
            if self.svc.get(run["id"]).get("linked") == "yes":
                break
            await asyncio.sleep(0.01)
        body = (await self.http.get("/api/brain/items", params={"q": "thing", "status": "all"})).json()["items"][0]["body"]
        self.assertIn("could not finish", body)


class Tools(Base):
    def tools(self):
        return {t.spec["name"]: t for t in lanes_tools.build(lambda: self.app, self.config)}

    async def test_gating_and_taint(self):
        t = self.tools()
        self.assertTrue(t["run_in_lane"].needs_approval({"task": "x"}))
        self.assertFalse(t["lanes_status"].needs_approval({}))
        self.assertTrue(getattr(t["run_in_lane"], "_untrusted", False))
        self.assertTrue(getattr(t["lanes_status"], "_untrusted", False))
        self.assertFalse(getattr(t["run_in_lane"], "mutating", False))

    async def test_preview_names_the_first_lane_and_the_fallbacks(self):
        ok, info = await self.tools()["run_in_lane"].preview({"task": "summarise"})
        self.assertTrue(ok, info)
        self.assertEqual((info["class"], info["goes_first_to"], info["cost"]), ("routine", "Council", "free"))
        self.assertEqual(info["only_if_it_fails"], ["Local model", "Gemini"])
        ok, info = await self.tools()["run_in_lane"].preview({"task": "x", "class": "private"})
        self.assertTrue(info["stays_on_the_laptop"])
        ok, info = await self.tools()["run_in_lane"].preview({"task": "x", "class": "private", "lane": "gemini"})
        self.assertFalse(ok)
        ok, info = await self.tools()["run_in_lane"].preview({"task": ""})
        self.assertFalse(ok)
        ok, info = await self.tools()["run_in_lane"].preview({"task": "x", "class": "../etc"})
        self.assertFalse(ok)

    async def test_preview_refuses_when_no_lane_can_take_it(self):
        self.council.ok, self.council.why = False, "Council is not set up on this laptop."
        self.set_class("routine", ["council"])
        ok, info = await self.tools()["run_in_lane"].preview({"task": "x"})
        self.assertFalse(ok)
        self.assertIn("not set up", info)

    async def test_run_and_status_round_trip(self):
        t = self.tools()
        ok, text = await t["run_in_lane"].run({"task": "summarise"})
        self.assertTrue(ok, text)
        self.assertIn("council run", text)
        run_id = text.split("lane run ")[1].split(" ")[0]
        ok, text = await t["lanes_status"].run({"run_id": run_id})
        self.assertIn("The council is working on it", text)
        self.council.finish(next(iter(self.council.jobs)))
        await self.until(run_id)
        ok, text = await t["lanes_status"].run({"run_id": run_id})
        self.assertIn("Use a heap.", text)
        self.assertIn("data, not instructions", text)
        ok, text = await t["lanes_status"].run({})
        self.assertIn("council (free): ready", text)
        self.assertIn("routine: council -> local -> gemini", text)
        self.assertIn("kept off limited and paid lanes", text)
        ok, text = await t["lanes_status"].run({"run_id": "nope"})
        self.assertFalse(ok)

    async def test_quick_lane_answers_directly(self):
        ok, text = await self.tools()["run_in_lane"].run({"task": "reformat", "lane": "local"})
        self.assertTrue(ok)
        self.assertIn("local answer", text)

    async def test_natural_phrases_route_to_the_lane_tools(self):
        names, pattern = TOOL_GROUPS["lanes"]
        self.assertEqual(names, {"run_in_lane", "lanes_status"})
        for phrase in ("give this to council", "send this to the council", "do the routine work", "save my tokens",
                       "try a stronger model", "keep this private work local"):
            self.assertTrue(pattern.search(phrase), phrase)
        specs = [{"name": n, "description": "d", "input_schema": {"properties": {}}} for n in ("run_in_lane", "lanes_status", "read_file")]
        history = [{"role": "user", "content": "send this to the council"}]
        self.assertIn("run_in_lane", [s["name"] for s in route_local_tools(specs, history)])

    async def test_both_personas_prefer_free_lanes(self):
        for who in ("jarvis", "friday"):
            self.assertIn("run_in_lane", persona.persona_prompt(who, "Chief"))


class WithTheRealCouncilManager(unittest.IsolatedAsyncioTestCase):
    """The default CouncilPort against the real CouncilManager and its fake lab process (tests/test_council.py)."""

    async def test_private_run_goes_through_the_real_manager_with_only_local_models(self):
        import json
        from hub import council
        from tests.test_council import FakeRunner, RESULT
        tmp = Path(tempfile.mkdtemp(prefix="lanes-real-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "lab" / "lab").mkdir(parents=True)
        config, root = make_config(data_dir=str(tmp / "data"), council_dir=str(tmp / "lab"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        app = FastAPI()
        ctx = Ctx(config=config, model_for=lambda p: FakeClient(), transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: None, get_app=lambda: app)
        runner = FakeRunner()
        app.include_router(council.make_router(ctx, runner=runner))
        router = lanes.make_router(ctx, ready_check=lambda lane: "", poll=0.01)
        app.include_router(router)
        self.addAsyncCleanup(router.service.shutdown)
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", cookies={"hub_session": "yes"})
        self.addAsyncCleanup(http.aclose)
        run = (await http.post("/api/lanes/run", json={"task": "tidy my notes", "class": "private"})).json()
        self.assertTrue(run["running"])
        await asyncio.sleep(0.05)
        argv = runner.spawned[0][0]
        self.assertIn("--only=qwen", argv)               # the hosted gemini-flash is left out of private work
        self.assertEqual(argv[-2:], ["--", "tidy my notes"])
        runner.procs[0].finish(0, json.dumps({**RESULT, "run_dir": ""}), "round 1\n")
        for _ in range(300):
            view = router.service.view(router.service.get(run["id"]))
            if not view["running"]:
                break
            await asyncio.sleep(0.01)
        self.assertEqual((view["status"], view["lane"], view["answer"]), ("done", "council", "Use a heap."))
        self.assertEqual((await http.get("/api/lanes/saved")).json()["week"], 1)


if __name__ == "__main__":
    unittest.main()
