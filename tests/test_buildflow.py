"""Tests for the Build flow (hub/buildflow.py, hub/buildflow_tools.py): brainstorm questions, workspace safety, spec and
plan files, plan parsing from hand-edited markdown, delegation to the Council, checkbox ticking, approvals, the Brain
save, the proactive event and the routes. Everything is a fake: the AI is a scripted function, the Council is a fake
caller, the workspace is a temp folder. No network, no real AI, no real Council."""
import asyncio
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import brain, buildflow, buildflow_tools, settings
from hub import config as cfg
from hub.assistant import TOOL_GROUPS, route_local_tools
from hub.common import Ctx
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.llm import LLMError
from hub.server import FEATURE_MODULES, Deps, create_app
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 13, 10, 0)
JOB = "abcdef012345"


def q(text, *options):
    return json.dumps({"question": text, "options": list(options) or ["Yes", "No"]})


READY = json.dumps({"ready": True})
SPEC = json.dumps({"goal": "A tiny habit tracker.", "scope": ["Add habits", "Tick them"], "non_goals": ["Accounts"],
                   "approach": ["One HTML page"], "acceptance": ["Ticking a habit sticks after reload"], "risks": ["None big"]})
PLAN = json.dumps({"steps": [{"text": "Create index.html", "check": "The page opens"},
                             {"text": "Add the tick logic", "check": "A tick sticks"},
                             {"text": "Final check against the spec", "check": "All acceptance checks pass"}]})


class FakeAI:
    """Scripted replies, the last one repeats. Records every prompt."""

    def __init__(self, *replies):
        self.replies, self.calls, self.error = list(replies), [], None

    async def __call__(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeCouncil:
    """Stands in for the in-process calls to /api/council/*."""

    def __init__(self):
        self.runs, self.job, self.refuse = [], {"status": "running"}, None

    async def __call__(self, method, path, **kw):
        if method == "POST" and path == "/api/council/run":
            if self.refuse:
                return False, self.refuse
            self.runs.append(kw["body"])
            return True, {"id": JOB, "status": "running"}
        if method == "GET" and path == f"/api/council/jobs/{JOB}":
            return True, dict(self.job, id=JOB)
        return False, "not found"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="build-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Chief")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.work = self.root / "docs"                      # the "docs" shared root is the workspace
        self.events, self.audits = [], []
        self.ai, self.council = FakeAI(q("Who uses it?", "Me", "My family")), FakeCouncil()
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None,
                       need_auth=need_auth, audit=lambda *a, **k: self.audits.append((a, k)))
        self.ctx.proactive = SimpleNamespace(enabled=True, submit=self.events.append)
        self.engine = buildflow.BuildFlow(self.ctx, ask=self.ai, caller=self.council, clock=lambda: NOW)

    def run_async(self, coro):
        return asyncio.run(coro)

    def started(self, title="Habit tracker"):
        return self.run_async(self.engine.start("build me a habit tracker", "docs", title))

    def ready_flow(self):
        v = self.started()
        self.ai.replies = [READY]
        return self.run_async(self.engine.answer(v["id"], "Me"))

    def to_plan(self):
        v = self.ready_flow()
        self.ai.replies = [SPEC]
        self.run_async(self.engine.spec(v["id"]))
        self.ai.replies = [PLAN]
        return self.run_async(self.engine.plan(v["id"]))


class BrainstormTest(Base):
    def test_start_returns_the_first_question_with_suggestions(self):
        v = self.started()
        self.assertEqual(v["stage"], "brainstorm")
        self.assertEqual(v["question"], {"question": "Who uses it?", "options": ["Me", "My family"]})
        self.assertEqual(v["workspace"], str(self.work.resolve()))
        self.assertIn("build me a habit tracker", self.ai.calls[0][1])

    def test_options_are_capped_at_three_and_padded_to_two(self):
        self.ai.replies = [json.dumps({"question": "Where?", "options": ["a", "b", "c", "d", "e"]})]
        self.assertEqual(self.started()["question"]["options"], ["a", "b", "c"])
        self.ai.replies = [json.dumps({"question": "Where?", "options": ["only"]})]
        self.assertEqual(self.started()["question"]["options"], ["only", "Not sure yet"])

    def test_answer_gives_the_next_question_and_stores_the_answer(self):
        v = self.started()
        self.ai.replies = [q("How big?", "Small", "Big")]
        v = self.run_async(self.engine.answer(v["id"], "Me"))
        self.assertEqual(v["question"]["question"], "How big?")
        self.assertEqual(v["answers"], [{"question": "Who uses it?", "answer": "Me"}])
        self.assertIn("Q1: Who uses it?", self.ai.calls[-1][1])

    def test_ai_can_say_ready_after_an_answer_but_not_before(self):
        self.ai.replies = [READY, q("A real question")]
        v = self.started()
        self.assertEqual(v["question"]["question"], "A real question")      # 'ready' with no answers is ignored
        self.ai.replies = [READY]
        v = self.run_async(self.engine.answer(v["id"], "Me"))
        self.assertTrue(v["ready"])
        self.assertIsNone(v["question"])
        self.assertIn("ready to write the spec", v["summary"].lower())

    def test_question_cap_is_six(self):
        self.ai.replies = [q("Another question?")]
        v = self.started()
        asked = 1
        while v["question"]:
            v = self.run_async(self.engine.answer(v["id"], f"answer {asked}"))
            if v["question"]:
                asked += 1
        self.assertEqual(asked, 6)
        self.assertEqual(len(v["answers"]), 6)
        self.assertTrue(v["ready"])
        self.assertEqual(len(self.ai.calls), 6)             # no seventh model call
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.answer(v["id"], "one more"))

    def test_empty_goal_empty_answer_and_unknown_id(self):
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.start("  ", "docs"))
        v = self.started()
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.answer(v["id"], "   "))
        with self.assertRaises(buildflow.BuildError) as c:
            self.engine.get("nope")
        self.assertEqual(c.exception.status, 404)

    def test_a_failing_ai_leaves_no_half_made_flow(self):
        self.ai.error = LLMError("Ollama is not running.")
        with self.assertRaises(buildflow.BuildError) as c:
            self.started()
        self.assertEqual(c.exception.status, 503)
        self.assertEqual(self.engine.store.count(), 0)

    def test_unreadable_ai_text_is_retried_once_then_refused(self):
        self.ai.replies = ["not json at all"]
        with self.assertRaises(buildflow.BuildError):
            self.started()
        self.assertEqual(len(self.ai.calls), 2)

    def test_free_provider_is_used_never_claude(self):
        picked = []

        async def complete(system, user, json_mode=False):
            return q("Why?")

        def model_for(provider):
            picked.append(provider)
            return SimpleNamespace(complete=complete)
        ctx = Ctx(config=self.config, model_for=model_for, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: None)
        self.config.default_provider = "claude"
        asyncio.run(buildflow.BuildFlow(ctx, clock=lambda: NOW).start("build me a thing", "docs"))
        self.assertTrue(picked)
        self.assertTrue(set(picked) <= {"local", "gemini"})


class WorkspaceTest(Base):
    def test_only_shared_folders_are_allowed(self):
        for bad in ("/etc", "/", str(self.root), "nonsense", "", "docs/../notes", "../docs", "docs/..", "C:\\Windows"):
            with self.assertRaises(buildflow.BuildError, msg=bad):
                self.run_async(self.engine.start("build me a thing", bad))
        self.assertEqual(self.engine.store.count(), 0)

    def test_root_name_subfolder_and_absolute_path_inside_a_root_work(self):
        (self.work / "app").mkdir()
        for ok in ("docs", "docs/app", str(self.work / "app"), str(self.work)):
            self.ai.replies = [q("Q?")]
            v = self.run_async(self.engine.start("build me a thing", ok))
            self.assertTrue(Path(v["workspace"]).is_dir())

    def test_a_missing_folder_or_a_file_is_refused(self):
        (self.work / "a.txt").write_text("x")
        for bad in ("docs/missing", "docs/a.txt"):
            with self.assertRaises(buildflow.BuildError):
                self.run_async(self.engine.start("build me a thing", bad))

    def test_a_symlink_out_of_the_root_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        try:
            (self.work / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.start("build me a thing", "docs/link"))

    def test_inside_refuses_an_escape(self):
        with self.assertRaises(buildflow.BuildError):
            buildflow.inside(self.work.resolve(), "../notes/x.md")


class SpecTest(Base):
    def test_spec_needs_an_answer(self):
        v = self.started()
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.spec(v["id"]))

    def test_preview_shows_path_and_first_20_lines_and_writes_nothing(self):
        v = self.ready_flow()
        self.ai.replies = [SPEC]
        p = self.run_async(self.engine.spec(v["id"], preview=True))
        self.assertEqual(p["path"], "docs/specs/2026-11-13-habit-tracker.md")
        self.assertFalse(p["written"])
        self.assertLessEqual(len(p["first_lines"].split("\n")), 20)
        self.assertIn("# Habit tracker", p["first_lines"])
        self.assertFalse((self.work / "docs").exists())

    def test_spec_is_written_inside_the_workspace_with_every_section(self):
        v = self.ready_flow()
        self.ai.replies = [SPEC]
        out = self.run_async(self.engine.spec(v["id"]))
        path = self.work / "docs" / "specs" / "2026-11-13-habit-tracker.md"
        self.assertEqual(out["spec_path"], "docs/specs/2026-11-13-habit-tracker.md")
        self.assertEqual(out["stage"], "spec")
        text = path.read_text(encoding="utf-8")
        for heading in ("## Goal", "## Scope", "## Non-goals", "## Approach", "## Acceptance checks", "## Risks"):
            self.assertIn(heading, text)
        self.assertIn("- Add habits", text)
        self.assertIn("buildflow_spec", [a[0][0] for a in self.audits])

    def test_preview_then_write_uses_the_same_draft_with_one_ai_call(self):
        v = self.ready_flow()
        self.ai.replies = [SPEC]
        before = len(self.ai.calls)
        self.run_async(self.engine.spec(v["id"], preview=True))
        self.run_async(self.engine.spec(v["id"]))
        self.assertEqual(len(self.ai.calls) - before, 1)

    def test_never_overwrites_an_existing_spec(self):
        folder = self.work / "docs" / "specs"
        folder.mkdir(parents=True)
        mine = folder / "2026-11-13-habit-tracker.md"
        mine.write_text("MY OWN SPEC", encoding="utf-8")
        v = self.ready_flow()
        self.ai.replies = [SPEC]
        out = self.run_async(self.engine.spec(v["id"]))
        self.assertEqual(mine.read_text(encoding="utf-8"), "MY OWN SPEC")
        self.assertEqual(out["spec_path"], "docs/specs/2026-11-13-habit-tracker-2.md")
        self.assertTrue((folder / "2026-11-13-habit-tracker-2.md").exists())

    def test_slug_is_safe(self):
        self.assertEqual(buildflow.slugify("../../Evil Name!!"), "evil-name")
        self.assertEqual(buildflow.slugify("???"), "build")
        self.assertLessEqual(len(buildflow.slugify("x" * 200)), 40)
        self.ai.replies = [q("Q?")]
        v = self.run_async(self.engine.start("build me a thing", "docs", "../../etc/passwd"))
        self.ai.replies = [READY]
        self.run_async(self.engine.answer(v["id"], "Me"))
        self.ai.replies = [SPEC]
        out = self.run_async(self.engine.spec(v["id"]))
        self.assertTrue(out["spec_path"].startswith("docs/specs/2026-11-13-etc-passwd"))
        self.assertNotIn("..", out["spec_path"])

    def test_a_second_spec_is_refused(self):
        v = self.ready_flow()
        self.ai.replies = [SPEC]
        self.run_async(self.engine.spec(v["id"]))
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.spec(v["id"]))


class PlanTest(Base):
    def test_plan_shares_the_spec_slug_and_has_checkbox_steps(self):
        out = self.to_plan()
        self.assertEqual(out["plan_path"], "docs/plans/2026-11-13-habit-tracker.md")
        self.assertEqual(out["stage"], "plan")
        text = (self.work / out["plan_path"]).read_text(encoding="utf-8")
        self.assertIn("- [ ] 1. Create index.html", text)
        self.assertIn("  - Check: The page opens", text)
        self.assertEqual((out["done"], out["total"], out["next"]), (0, 3, "Create index.html"))
        self.assertEqual(out["summary"], "0 of 3 done, next: Create index.html")

    def test_plan_never_overwrites_an_existing_file(self):
        plans = self.work / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "2026-11-13-habit-tracker.md").write_text("OLD", encoding="utf-8")
        out = self.to_plan()
        self.assertEqual((plans / "2026-11-13-habit-tracker.md").read_text(encoding="utf-8"), "OLD")
        self.assertEqual(out["plan_path"], "docs/plans/2026-11-13-habit-tracker-2.md")

    def test_plan_needs_the_spec_and_a_usable_ai_answer(self):
        v = self.ready_flow()
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.plan(v["id"]))
        self.ai.replies = [SPEC]
        self.run_async(self.engine.spec(v["id"]))
        self.ai.replies = [json.dumps({"steps": []})]
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.plan(v["id"]))

    def test_parse_hand_edited_markdown(self):
        text = "\n".join([
            "# My plan", "Some intro", "- [x] 1. Already done", "  - Check: it exists",
            "* [ ] 2) Star bullet and paren", "- [X] Third, no number", "  - Acceptance: shows up",
            "   - [ ] 9. Nested and renumbered", "- not a step", "- [ ]", "Check: stray line"])
        steps = buildflow.parse_plan(text)
        self.assertEqual([(s["n"], s["text"], s["done"]) for s in steps],
                         [(1, "Already done", True), (2, "Star bullet and paren", False),
                          (3, "Third, no number", True), (9, "Nested and renumbered", False)])
        self.assertEqual(steps[0]["check"], "it exists")
        self.assertEqual(steps[2]["check"], "shows up")
        self.assertEqual(steps[1]["check"], "")

    def test_the_flow_follows_the_edited_file(self):
        out = self.to_plan()
        (self.work / out["plan_path"]).write_text(
            "# Mine\n- [x] 1. Create index.html\n- [ ] 2. A step I wrote myself\n- [ ] 3. Another\n", encoding="utf-8")
        v = self.run_async(self.engine.status(out["id"]))
        self.assertEqual((v["done"], v["total"], v["next"]), (1, 3, "A step I wrote myself"))
        self.assertEqual(v["summary"], "1 of 3 done, next: A step I wrote myself")
        self.run_async(self.engine.next_step(out["id"]))
        self.assertIn("A step I wrote myself", self.council.runs[0]["task"])

    def test_tick_line_only_touches_that_line(self):
        text = "- [ ] 1. a\n- [ ] 2. b [ ] keep\n"
        self.assertEqual(buildflow.tick_line(text, 1), "- [ ] 1. a\n- [x] 2. b [ ] keep\n")


class BuildTest(Base):
    def plan_text(self, out):
        return (self.work / out["plan_path"]).read_text(encoding="utf-8")

    def test_next_delegates_the_first_unchecked_step_to_the_council(self):
        out = self.to_plan()
        r = self.run_async(self.engine.next_step(out["id"]))
        self.assertEqual(r["job_id"], JOB)
        self.assertEqual(r["stage"], "build")
        task = self.council.runs[0]["task"]
        self.assertIn("Create index.html", task)
        self.assertIn("The page opens", task)
        self.assertIn(str((self.work / "docs" / "specs" / "2026-11-13-habit-tracker.md").resolve()), task)
        self.assertIn(str(self.work.resolve()), task)
        self.assertLessEqual(len(task), 4000)
        self.assertTrue(r["steps"][0]["running"])

    def test_preview_starts_nothing(self):
        out = self.to_plan()
        p = self.run_async(self.engine.next_step(out["id"], preview=True))
        self.assertEqual((p["number"], p["of"], p["step"]), (1, 3, "Create index.html"))
        self.assertEqual(self.council.runs, [])

    def test_success_ticks_the_checkbox(self):
        out = self.to_plan()
        self.run_async(self.engine.next_step(out["id"]))
        self.council.job = {"status": "accepted", "result": {"status": "accepted", "answer": "done"}}
        v = self.run_async(self.engine.status(out["id"]))
        text = self.plan_text(out)
        self.assertIn("- [x] 1. Create index.html", text)
        self.assertIn("- [ ] 2. Add the tick logic", text)
        self.assertEqual((v["done"], v["total"], v["job_id"]), (1, 3, ""))
        self.assertEqual(v["next"], "Add the tick logic")

    def test_failure_leaves_the_box_and_records_the_reason(self):
        out = self.to_plan()
        self.run_async(self.engine.next_step(out["id"]))
        self.council.job = {"status": "failed", "result": {"status": "failed", "stopped": "verify command failed twice"}}
        v = self.run_async(self.engine.status(out["id"]))
        self.assertIn("- [ ] 1. Create index.html", self.plan_text(out))
        self.assertEqual(v["done"], 0)
        self.assertEqual(v["job_id"], "")
        self.assertIn("verify command failed twice", v["steps"][0]["failure"])
        self.council.job = {"status": "running"}
        r = self.run_async(self.engine.next_step(out["id"]))       # the same step is retried
        self.assertIn("Create index.html", self.council.runs[1]["task"])
        self.assertEqual(r["steps"][0]["failure"], "")

    def test_a_running_step_blocks_a_second_one(self):
        out = self.to_plan()
        self.run_async(self.engine.next_step(out["id"]))
        with self.assertRaises(buildflow.BuildError) as c:
            self.run_async(self.engine.next_step(out["id"]))
        self.assertEqual(c.exception.status, 409)
        self.assertEqual(len(self.council.runs), 1)

    def test_the_council_refusing_is_reported_and_nothing_is_recorded(self):
        out = self.to_plan()
        self.council.refuse = "A council run is already going."
        with self.assertRaises(buildflow.BuildError) as c:
            self.run_async(self.engine.next_step(out["id"]))
        self.assertIn("already going", str(c.exception))
        self.assertEqual(self.engine.get(out["id"])["job_id"], "")

    def test_next_needs_a_plan(self):
        v = self.started()
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.next_step(v["id"]))

    def test_finishing_saves_a_decision_and_emits_one_batch_event(self):
        out = self.to_plan()
        for _ in range(3):
            self.council.job = {"status": "running"}
            self.run_async(self.engine.next_step(out["id"]))
            self.council.job = {"status": "accepted", "result": {"status": "accepted"}}
            v = self.run_async(self.engine.status(out["id"]))
        self.assertEqual((v["done"], v["total"], v["stage"], v["finished"]), (3, 3, "done", True))
        self.assertEqual(v["summary"], "3 of 3 done. Finished.")
        items = brain.store_for(self.config).search("", kind="decision", status="all")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Built: Habit tracker")
        self.assertIn("docs/plans/2026-11-13-habit-tracker.md", items[0]["body"])
        self.assertEqual(self.events, [{"type": "build_done", "title": "Build finished: Habit tracker (3 steps)", "count": 1,
                                        "body": "docs/specs/2026-11-13-habit-tracker.md", "source": "buildflow"}])
        self.run_async(self.engine.status(out["id"]))               # looking again does not repeat it
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(brain.store_for(self.config).search("", kind="decision", status="all")), 1)
        with self.assertRaises(buildflow.BuildError):
            self.run_async(self.engine.next_step(out["id"]))

    def test_ticking_the_last_box_by_hand_also_finishes(self):
        out = self.to_plan()
        path = self.work / out["plan_path"]
        path.write_text(path.read_text(encoding="utf-8").replace("[ ]", "[x]"), encoding="utf-8")
        v = self.run_async(self.engine.status(out["id"]))
        self.assertEqual(v["stage"], "done")
        self.assertEqual(len(self.events), 1)

    def test_brain_off_means_no_brain_item_but_still_the_event(self):
        self.config.brain_enabled = False
        out = self.to_plan()
        path = self.work / out["plan_path"]
        path.write_text(path.read_text(encoding="utf-8").replace("[ ]", "[x]"), encoding="utf-8")
        self.run_async(self.engine.status(out["id"]))
        self.assertEqual(brain.store_for(self.config).search("", kind="decision", status="all"), [])
        self.assertEqual(len(self.events), 1)

    def test_switched_off_refuses_everything(self):
        self.config.buildflow_enabled = False
        with self.assertRaises(buildflow.BuildError) as c:
            self.started()
        self.assertEqual(c.exception.status, 409)

    def test_delete_removes_the_flow_but_not_the_files(self):
        out = self.to_plan()
        self.engine.delete(out["id"])
        self.assertEqual(self.engine.store.count(), 0)
        self.assertTrue((self.work / out["plan_path"]).exists())

    def test_proactive_batch_type_and_sentence(self):
        from hub import proactive
        self.assertIn("build_done", proactive.BATCH_TYPES)
        self.assertEqual(proactive.classify({"type": "build_done"}), "batch")
        self.assertIn("build is finished", proactive.batch_sentence([{"type": "build_done"}]))


class RouteTest(Base):
    def client(self):
        app = FastAPI()
        app.include_router(buildflow.make_router(self.ctx, ask=self.ai, caller=self.council, clock=lambda: NOW))
        client = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(client.close)
        return app, client

    def test_everything_needs_a_login(self):
        app, _ = self.client()
        anon = TestClient(app)
        for method, path in (("GET", "/api/buildflow"), ("POST", "/api/buildflow"), ("GET", "/api/buildflow/workspaces"),
                             ("GET", "/api/buildflow/abcdef012345"), ("POST", "/api/buildflow/abcdef012345/answer"),
                             ("POST", "/api/buildflow/abcdef012345/spec"), ("POST", "/api/buildflow/abcdef012345/plan"),
                             ("POST", "/api/buildflow/abcdef012345/next"), ("GET", "/api/buildflow/abcdef012345/status"),
                             ("DELETE", "/api/buildflow/abcdef012345")):
            self.assertEqual(anon.request(method, path, json={}).status_code, 401, (method, path))

    def test_full_walk_through_the_routes(self):
        _, c = self.client()
        self.assertEqual(c.get("/api/buildflow").json()["flows"], [])
        self.assertIn("docs", [w["name"] for w in c.get("/api/buildflow/workspaces").json()["workspaces"]])
        r = c.post("/api/buildflow", json={"goal": "build me a habit tracker", "workspace": "docs", "title": "Habits"})
        self.assertEqual(r.status_code, 200, r.text)
        fid = r.json()["id"]
        self.ai.replies = [READY]
        self.assertTrue(c.post(f"/api/buildflow/{fid}/answer", json={"answer": "Me"}).json()["ready"])
        self.ai.replies = [SPEC]
        p = c.post(f"/api/buildflow/{fid}/spec", json={"preview": True}).json()
        self.assertEqual(p["path"], "docs/specs/2026-11-13-habits.md")
        self.assertEqual(c.post(f"/api/buildflow/{fid}/spec").json()["stage"], "spec")
        self.ai.replies = [PLAN]
        self.assertEqual(c.post(f"/api/buildflow/{fid}/plan", json={}).json()["total"], 3)
        self.assertEqual(c.post(f"/api/buildflow/{fid}/next").json()["job_id"], JOB)
        self.assertEqual(c.get(f"/api/buildflow/{fid}/status").json()["stage"], "build")
        self.assertEqual(len(c.get("/api/buildflow").json()["flows"]), 1)
        self.assertEqual(c.delete(f"/api/buildflow/{fid}").json(), {"deleted": fid})
        self.assertEqual(c.get(f"/api/buildflow/{fid}").status_code, 404)

    def test_validation(self):
        _, c = self.client()
        self.assertEqual(c.post("/api/buildflow", json={"goal": "x" * 5000, "workspace": "docs"}).status_code, 422)
        self.assertEqual(c.post("/api/buildflow", json={"goal": "hi there", "workspace": "docs", "extra": 1}).status_code, 422)
        self.assertEqual(c.post("/api/buildflow", json={"goal": "hi there"}).status_code, 422)
        self.assertEqual(c.post("/api/buildflow", json={"goal": "hi there", "workspace": "/etc"}).status_code, 400)
        self.assertEqual(c.post("/api/buildflow", json={"goal": "hi there", "workspace": "docs/../notes"}).status_code, 400)
        self.assertEqual(c.get("/api/buildflow/not-an-id").status_code, 404)
        self.assertEqual(c.post("/api/buildflow/abcdef012345/answer", json={"answer": "x"}).status_code, 404)
        self.assertEqual(c.post("/api/buildflow/abcdef012345/next", json={"bogus": 1}).status_code, 422)
        self.assertEqual(c.delete("/api/buildflow/abcdef012345").status_code, 404)
        self.ai.error = LLMError("AI is down")
        self.assertEqual(c.post("/api/buildflow", json={"goal": "hi there", "workspace": "docs"}).status_code, 503)

    def test_switched_off_lists_nothing_and_refuses_changes(self):
        _, c = self.client()
        self.config.buildflow_enabled = False
        self.assertFalse(c.get("/api/buildflow").json()["enabled"])
        self.assertEqual(c.post("/api/buildflow", json={"goal": "hi there", "workspace": "docs"}).status_code, 409)


class ToolTest(unittest.TestCase):
    """Through the real app, so the tools call the real routes in-process."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="build-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ai, self.council, self.engines = FakeAI(q("Who uses it?", "Me", "Family")), FakeCouncil(), []
        outer = self

        class Engine(buildflow.BuildFlow):
            def __init__(self, ctx, **kw):
                super().__init__(ctx, ask=outer.ai, caller=outer.council, clock=lambda: NOW)
                outer.engines.append(self)
        patcher = mock.patch.object(buildflow, "BuildFlow", Engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant()),
                              audit_path=self.root / "audit.log")
        self.tools = buildflow_tools.build(lambda: self.app, self.config)

    def tool(self, name):
        return next(t for t in self.tools if t.spec["name"] == name)

    def run_tool(self, name, **args):
        return asyncio.run(self.tool(name).run(args))

    def test_specs_and_approval_gating(self):
        names = {t.spec["name"]: t for t in self.tools}
        self.assertEqual(set(names), {"build_start", "build_answer", "build_spec", "build_plan", "build_next",
                                      "build_status", "build_list"})
        for gated in ("build_spec", "build_plan", "build_next"):
            self.assertTrue(names[gated].needs_approval({}), gated)
            self.assertTrue(names[gated].mutating, gated)
            self.assertIsNotNone(names[gated].preview)
        for free in ("build_start", "build_answer", "build_status", "build_list"):
            self.assertFalse(names[free].needs_approval({}), free)
            self.assertFalse(names[free].mutating, free)
        for t in self.tools:
            self.assertEqual(t.spec["input_schema"]["type"], "object")
            self.assertFalse(t.spec["input_schema"]["additionalProperties"])

    def test_tool_group_is_routed(self):
        names, pattern = TOOL_GROUPS["buildflow"]
        self.assertEqual(names, {t.spec["name"] for t in self.tools})
        for text in ("Jarvis, build me a habit tracker", "build a small site", "start a new project", "write the spec",
                     "plan the work", "do the next step"):
            self.assertRegex(text, pattern)
        specs = [t.spec for t in self.tools] + [{"name": "list_dir", "description": "d", "input_schema": {}}]
        picked = {s["name"] for s in route_local_tools(specs, [{"role": "user", "content": "build me a habit tracker"}])}
        self.assertIn("build_start", picked)
        self.assertIn("buildflow", FEATURE_MODULES)

    def test_a_whole_build_through_the_tools_with_previews(self):
        ok, text = self.run_tool("build_start", goal="build me a habit tracker", workspace="docs")
        self.assertTrue(ok, text)
        self.assertIn("Who uses it?", text)
        self.assertIn("Me; Family", text)
        fid = self.engines[-1].store.all()[0]["id"]
        self.ai.replies = [READY]
        ok, text = self.run_tool("build_answer", id=fid, answer="Me")
        self.assertIn("ready", text.lower())
        self.ai.replies = [SPEC]
        ok, info = asyncio.run(self.tool("build_spec").preview({"id": fid}))
        self.assertTrue(ok)
        self.assertEqual(info["writes_file"], "docs/specs/2026-11-13-build-me-a-habit-tracker.md")
        self.assertIn("## Goal", info["first_lines"])
        self.assertFalse((self.root / "docs" / "docs").exists())            # a preview writes nothing
        ok, text = self.run_tool("build_spec", id=fid)
        self.assertTrue(ok, text)
        self.ai.replies = [PLAN]
        ok, info = asyncio.run(self.tool("build_plan").preview({"id": fid}))
        self.assertEqual(info["writes_file"], "docs/plans/2026-11-13-build-me-a-habit-tracker.md")
        ok, text = self.run_tool("build_plan", id=fid)
        self.assertIn("3 steps", text)
        ok, info = asyncio.run(self.tool("build_next").preview({"id": fid}))
        self.assertEqual(info["hands_to_council"], "step 1 of 3: Create index.html")
        self.assertEqual(self.council.runs, [])
        ok, text = self.run_tool("build_next", id=fid)
        self.assertIn(f"council run {JOB}", text)
        self.council.job = {"status": "accepted", "result": {"status": "accepted"}}
        ok, text = self.run_tool("build_status", id=fid)
        self.assertIn("1 of 3 done, next: Add the tick logic", text)
        self.assertIn("[x] 1. Create index.html", text)
        ok, text = self.run_tool("build_list")
        self.assertIn(fid, text)

    def test_bad_input_is_refused_before_any_route(self):
        self.assertFalse(self.run_tool("build_start", goal="", workspace="docs")[0])
        self.assertFalse(self.run_tool("build_start", goal="x thing", workspace="/etc")[0])
        self.assertFalse(self.run_tool("build_answer", id="zzz", answer="x")[0])
        self.assertFalse(asyncio.run(self.tool("build_next").preview({"id": "../x"}))[0])
        self.assertFalse(self.run_tool("build_status", id="000000000000")[0])

    def test_setting_is_exposed(self):
        self.assertIn("buildflow_enabled", settings.ALLOWED_KEYS)
        self.assertIs(cfg.Config.__dataclass_fields__["buildflow_enabled"].default, True)


if __name__ == "__main__":
    unittest.main()
