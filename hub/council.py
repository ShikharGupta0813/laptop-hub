"""Council: side jobs that run the `multi-llm-lab` CLI (several models working on one task). See docs/api/council.md.

The lab is a separate project (config `council_dir`). The hub only starts it as a child process:

    <python> -m lab council --json [--verify=CMD] [--workers N] [--rounds N] [--max-calls N]
             [--escalate=claude] [--resume=RUN_DIR --notes -] -- TASK

with an argument list (never a shell). The task and the verify command are single argv values; the owner's notes
travel on STDIN. stdout is ONE JSON object `{status, answer, files, calls, stopped, run_dir}`; stderr is progress.

Rules: one run at a time (the laptop is CPU only), a hard wall-clock timeout, job state kept on disk so a hub
restart does not lose finished results, Claude escalation only when the owner set `council_allow_claude`, and
everything the lab prints is data for the owner to read, never something the hub acts on.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import usage
from .common import Ctx, JsonFile
from . import panic as _panic

log = logging.getLogger("hub.council")

MAX_TASK_CHARS = 4000
MAX_VERIFY_CHARS = 500
MAX_NOTES_CHARS = 8000
MAX_PACKET_CHARS = 60_000
MAX_ANSWER_CHARS = 20_000
MAX_STDOUT_BYTES = 1_000_000
LOG_KEEP_BYTES = 6000
LOG_VIEW_CHARS = 4000
KEEP_JOBS = 50
PACKET_TIMEOUT = 60.0
SAVE_EVERY = 2.0                   # seconds between log snapshots written to disk
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_MODEL_NAMES = 20
MODELS_TIMEOUT = 30.0
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LAB_STATUSES = ("accepted", "failed", "stopped")
FINISHED = ("accepted", "failed", "stopped", "cancelled", "timeout", "error", "interrupted")
SETUP_COMMAND = "python -m hub setup --only council"
NOT_SET_UP = f"Council is not set up on this laptop. Run: {SETUP_COMMAND}"


class CouncilError(Exception):
    """A refusal with a plain sentence for the phone."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _clean_log(text: str) -> str:
    return CONTROL_RE.sub("", ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")))


def _one_line(text: Any, n: int) -> str:
    return " ".join(str(text if text is not None else "").split())[:n]


def venv_python(council_dir: str) -> Path:
    base = Path(council_dir) / ".venv"
    return base / "Scripts" / "python.exe" if sys.platform == "win32" else base / "bin" / "python"


def resolve_python(config) -> str:
    """council_python if set, else the lab's own .venv if it has one, else the interpreter running the hub."""
    chosen = (getattr(config, "council_python", "") or "").strip()
    if chosen:
        return chosen
    if (config.council_dir or "").strip():
        candidate = venv_python(config.council_dir)
        if candidate.is_file():
            return str(candidate)
    return sys.executable


# ----------------------------------------------------------------------------------------------- processes

class Runner:
    """Every process the council starts. Tests replace this with a fake that needs no real program."""

    async def spawn(self, argv: list[str], cwd: str, env: dict[str, str], stdin_text: str | None):
        """Start a child (own process group, no shell). Returns an object with pid, stdout, stderr (stream
        readers), returncode and `await wait()`. Raises OSError if the program is missing."""
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass          # the child exited before reading; its exit code tells the story
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        return proc

    async def kill(self, proc) -> None:
        from .assistant import _kill_tree
        await _kill_tree(proc)

    async def run_once(self, argv: list[str], cwd: str, env: dict[str, str], timeout: float,
                       max_bytes: int) -> tuple[int | None, str, str]:
        """A short helper command. (exit code or None on timeout, stdout capped, stderr tail)."""
        proc = await self.spawn(argv, cwd, env, None)
        out = bytearray()

        async def drain(stream, sink, cap):
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                if len(sink) < cap:
                    sink.extend(chunk[:cap - len(sink)])
        err = bytearray()
        readers = [asyncio.ensure_future(drain(proc.stdout, out, max_bytes)),
                   asyncio.ensure_future(drain(proc.stderr, err, 8000))]
        try:
            await asyncio.wait_for(proc.wait(), timeout)
            await asyncio.wait_for(asyncio.gather(*readers), 5)
            code: int | None = proc.returncode
        except (asyncio.TimeoutError, TimeoutError):
            await self.kill(proc)
            code = None
        finally:
            for r in readers:
                r.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


# ------------------------------------------------------------------------------------------------- the jobs

def _read_clamped(value: Any, name: str, lo: int, hi: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise CouncilError(f"{name} must be a whole number from {lo} to {hi}.")
    if not lo <= int(value) <= hi:
        raise CouncilError(f"{name} must be a whole number from {lo} to {hi}.")
    return int(value)


def _read_text(value: Any, name: str, limit: int, *, multiline: bool, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise CouncilError(f"Say what the council should work on ({name}).")
        return ""
    if not isinstance(value, str):
        raise CouncilError(f"{name} must be text.")
    text = value.replace("\x00", "").strip()
    if not multiline and ("\n" in text or "\r" in text):
        raise CouncilError(f"{name} must be a single line.")
    if len(text) > limit:
        raise CouncilError(f"{name} is too long ({len(text)} characters; the limit is {limit}).")
    if required and not text:
        raise CouncilError(f"Say what the council should work on ({name}).")
    return text


class CouncilManager:
    def __init__(self, ctx: Ctx, runner: Runner | None = None, clock: Callable[[], float] | None = None):
        self.ctx = ctx
        self.config = ctx.config
        self.runner = runner or Runner()
        self.clock = clock or time.time
        self.dir = ctx.config.data_path() / "council" / "jobs"
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.procs: dict[str, Any] = {}
        self.logs: dict[str, bytearray] = {}
        self.cancelled: set[str] = set()
        self._load()

    # -- config ----------------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool((self.config.council_dir or "").strip())

    def lab_ready(self) -> bool:
        return self.enabled() and (Path(self.config.council_dir) / "lab").is_dir()

    def timeout_seconds(self) -> float:
        try:
            minutes = float(self.config.council_timeout_min)
        except (TypeError, ValueError):
            minutes = 90.0
        return max(1.0, min(minutes * 60.0, 24 * 3600.0))

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", NO_COLOR="1")
        if not self.config.council_allow_claude:
            # The lab must not be able to reach Claude through the hub's key unless the owner allowed it.
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def require_ready(self) -> None:
        if not self.enabled():
            raise CouncilError(NOT_SET_UP, 409)
        if not self.lab_ready():
            raise CouncilError("council_dir does not look like the multi-llm-lab folder (no 'lab' folder in it). "
                               f"Run: {SETUP_COMMAND}", 409)

    # -- persistence -----------------------------------------------------------------------------------
    def _path(self, job_id: str) -> JsonFile:
        return JsonFile(self.dir / f"{job_id}.json")

    def _save(self, job: dict) -> None:
        try:
            self._path(job["id"]).save(job)
        except OSError as exc:
            log.warning("council: could not save job %s (%s)", job["id"], exc)

    def _load(self) -> None:
        try:
            files = sorted(self.dir.glob("*.json"))
        except OSError:
            return
        for f in files:
            job = JsonFile(f).load(None)
            if not isinstance(job, dict) or not JOB_ID_RE.match(str(job.get("id", ""))):
                continue
            if job.get("status") == "running":
                # The hub went away while it ran (shutdown kills the child; a crash may not).
                job.update(status="interrupted", finished=self.clock(),
                           error="The hub restarted while this run was going, so it was stopped.")
                job.pop("pid", None)
                self._save(job)
            self.jobs[job["id"]] = job

    def _prune(self) -> None:
        done = sorted((j for j in self.jobs.values() if j["status"] != "running"), key=lambda j: j["created"])
        for job in done[:max(0, len(done) - KEEP_JOBS)]:
            self.jobs.pop(job["id"], None)
            try:
                (self.dir / f"{job['id']}.json").unlink()
            except OSError:
                pass

    # -- views -----------------------------------------------------------------------------------------
    def view(self, job: dict, full: bool = True) -> dict:
        now = self.clock()
        end = job.get("finished") or now
        out = {
            "id": job["id"], "status": job["status"], "running": job["status"] == "running",
            "task": job["task"] if full else _one_line(job["task"], 160),
            "verify_command": job.get("verify_command") or "",
            "options": job.get("options") or {}, "created": job["created"], "finished": job.get("finished"),
            "elapsed_s": max(0, int(end - job["created"])), "resume_of": job.get("resume_of"),
            "notes_chars": job.get("notes_chars", 0), "error": job.get("error") or "",
            "run_dir": (job.get("result") or {}).get("run_dir") or "",
            "has_result": bool(job.get("result")),
        }
        result = job.get("result")
        if full:
            out["log_tail"] = job.get("log_tail", "")
            out["result"] = result
        else:
            out["answer_preview"] = _one_line((result or {}).get("answer"), 200)
        return out

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id) if isinstance(job_id, str) and JOB_ID_RE.match(job_id) else None
        if job is None:
            raise CouncilError("There is no council run with that id.", 404)
        return job

    def running(self) -> dict | None:
        return next((j for j in self.jobs.values() if j["status"] == "running"), None)

    def status(self) -> dict:
        running = self.running()
        return {
            "enabled": self.enabled(), "ready": self.lab_ready(),
            "folder": Path(self.config.council_dir).name if self.enabled() else "",
            "python_found": Path(resolve_python(self.config)).is_file() if self.enabled() else False,
            "allow_claude": bool(self.config.council_allow_claude),
            "timeout_min": self.config.council_timeout_min, "running_job": running["id"] if running else None,
            "setup_command": SETUP_COMMAND,
            "limits": {"task": MAX_TASK_CHARS, "verify": MAX_VERIFY_CHARS, "notes": MAX_NOTES_CHARS},
        }

    def listing(self) -> list[dict]:
        rows = sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)
        return [self.view(j, full=False) for j in rows]

    # -- starting --------------------------------------------------------------------------------------
    def build_spec(self, body: Any) -> dict:
        """Check the request and return what to run. Nothing is started here."""
        if not isinstance(body, dict):
            raise CouncilError("Send a JSON object with a task.")
        spec: dict[str, Any] = {}
        resume_id = body.get("resume_job_id")
        base = None
        if resume_id not in (None, ""):
            base = self.get(resume_id if isinstance(resume_id, str) else "")
            if base["status"] == "running":
                raise CouncilError("That run is still going. Wait for it to finish before rerunning it.", 409)
            run_dir = (base.get("result") or {}).get("run_dir")
            if not isinstance(run_dir, str) or not self._safe_run_dir(run_dir):
                raise CouncilError("That run left no saved run folder to continue from.", 409)
            spec["resume_dir"] = run_dir
            spec["resume_of"] = base["id"]
        task = _read_text(body.get("task"), "task", MAX_TASK_CHARS, multiline=True, required=base is None)
        spec["task"] = task or (base["task"] if base else "")
        verify = _read_text(body.get("verify_command"), "verify_command", MAX_VERIFY_CHARS, multiline=False)
        spec["verify"] = verify or ((base.get("verify_command") or "") if base and body.get("verify_command") is None else "")
        notes = _read_text(body.get("notes"), "notes", MAX_NOTES_CHARS, multiline=True)
        if notes and base is None:
            raise CouncilError("Notes only make sense when rerunning a finished run (resume_job_id).")
        spec["notes"] = notes
        for key, name, lo, hi in (("workers", "workers", 1, 8), ("rounds", "rounds", 1, 10),
                                  ("max_calls", "max_calls", 1, 500)):
            spec[key] = _read_clamped(body.get(key), name, lo, hi)
        for key in ("only", "skip"):
            spec[key] = self._model_names(body.get(key), key)
        esc = body.get("escalate_claude")
        if esc not in (None, False, True):
            raise CouncilError("escalate_claude must be true or false.")
        if esc and not self.config.council_allow_claude:
            raise CouncilError("Claude escalation is switched off. The owner can allow it with "
                               "council_allow_claude in hub.json.")
        spec["escalate"] = bool(esc)
        return spec

    def _model_names(self, value: Any, key: str) -> list[str]:
        if value in (None, ""):
            return []
        if (not isinstance(value, list) or len(value) > MAX_MODEL_NAMES
                or not all(isinstance(n, str) and MODEL_NAME_RE.match(n) for n in value)):
            raise CouncilError(f"{key} must be a list of model names from the Models list "
                               f"(at most {MAX_MODEL_NAMES}).")
        if not self.config.council_allow_claude and any(self.is_claude(n) for n in value) and key == "only":
            raise CouncilError("Claude models are switched off. The owner can allow them with "
                               "council_allow_claude in hub.json.")
        return list(dict.fromkeys(value))

    @staticmethod
    def is_claude(*texts: Any) -> bool:
        return any("claude" in str(t).lower() for t in texts)

    @staticmethod
    def _safe_run_dir(path: str) -> bool:
        return (isinstance(path, str) and bool(path) and "\x00" not in path and os.path.isabs(path)
                and not path.startswith("-") and os.path.isdir(path))

    def build_argv(self, spec: dict) -> list[str]:
        argv = [resolve_python(self.config), "-m", "lab", "council", "--json"]
        if spec.get("verify"):
            argv.append(f"--verify={spec['verify']}")          # one argv value, so a leading '-' is harmless
        for key, flag in (("workers", "--workers"), ("rounds", "--rounds"), ("max_calls", "--max-calls")):
            if spec.get(key) is not None:
                argv += [flag, str(spec[key])]
        for key in ("only", "skip"):
            if spec.get(key):
                argv.append(f"--{key}={','.join(spec[key])}")    # names are checked; one argv value, no commas inside
        if spec.get("escalate"):
            argv.append("--escalate=claude")
        if spec.get("resume_dir"):
            argv.append(f"--resume={spec['resume_dir']}")
            if spec.get("notes"):
                argv += ["--notes", "-"]                       # the notes themselves go on stdin
        argv += ["--", spec["task"]]
        return argv

    def start(self, body: Any) -> dict:
        """Validate, then launch in the background. Returns the new job at once."""
        self.require_ready()
        spec = self.build_spec(body)
        running = self.running()
        if running:
            raise CouncilError("A council run is already going (it uses the whole laptop). Wait for it to finish "
                               f"or cancel it first (run {running['id']}).", 409)
        job = {
            "id": secrets.token_hex(6), "status": "running", "task": spec["task"], "verify_command": spec["verify"],
            "options": {k: spec[k] for k in ("workers", "rounds", "max_calls", "only", "skip") if spec.get(k) not in (None, [])}
                       | ({"escalate_claude": True} if spec["escalate"] else {}),
            "created": self.clock(), "finished": None, "resume_of": spec.get("resume_of"),
            "notes_chars": len(spec["notes"]), "log_tail": "", "result": None, "error": "",
        }
        self.jobs[job["id"]] = job
        self._save(job)
        self.logs[job["id"]] = bytearray()
        argv = self.build_argv(spec)
        self.tasks[job["id"]] = asyncio.get_running_loop().create_task(
            self._run(job, argv, spec["notes"] or None))
        usage.record(self.config, "council")
        self.ctx.audit("council_run", job=job["id"], task_chars=len(spec["task"]), verify=bool(spec["verify"]),
                       resume=bool(spec.get("resume_of")), notes_chars=len(spec["notes"]),
                       escalate=bool(spec["escalate"]))
        return self.view(job)

    # -- running ---------------------------------------------------------------------------------------
    async def _pump_stderr(self, job: dict, stream) -> None:
        buf = self.logs[job["id"]]
        last = 0.0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > LOG_KEEP_BYTES:
                del buf[:len(buf) - LOG_KEEP_BYTES]
            job["log_tail"] = _clean_log(buf.decode("utf-8", "replace"))[-LOG_VIEW_CHARS:]
            now = time.monotonic()
            if now - last >= SAVE_EVERY:
                last = now
                self._save(job)

    @staticmethod
    async def _read_stdout(stream) -> bytes:
        out = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(out)
            if len(out) < MAX_STDOUT_BYTES:
                out.extend(chunk[:MAX_STDOUT_BYTES - len(out)])

    async def _run(self, job: dict, argv: list[str], stdin_text: str | None) -> None:
        job_id = job["id"]
        timed_out = interrupted = False
        stdout = b""
        code: int | None = None
        try:
            try:
                proc = await self.runner.spawn(argv, self.config.council_dir, self.child_env(), stdin_text)
            except OSError as exc:
                job["error"] = f"Could not start the council ({_one_line(exc, 150)}). Check council_python."
                return self._finish(job, "error")
            self.procs[job_id] = proc
            job["pid"] = getattr(proc, "pid", None)
            self._save(job)
            out_task = asyncio.ensure_future(self._read_stdout(proc.stdout))
            err_task = asyncio.ensure_future(self._pump_stderr(job, proc.stderr))
            try:
                await asyncio.wait_for(proc.wait(), self.timeout_seconds())
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                await self.runner.kill(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
            except asyncio.CancelledError:
                interrupted = True
                await self.runner.kill(proc)
                raise
            finally:
                # Once the child is gone its pipes close; a grandchild holding one open must not hang us.
                done, pending = await asyncio.wait([out_task, err_task], timeout=5)
                for t in pending:
                    t.cancel()
                await asyncio.gather(out_task, err_task, return_exceptions=True)
                if out_task in done and not out_task.cancelled() and out_task.exception() is None:
                    stdout = out_task.result()
                code = getattr(proc, "returncode", None)
        except asyncio.CancelledError:
            job["error"] = "The hub shut down while this run was going, so it was stopped."
            self._finish(job, "interrupted", notify=False)
            raise
        except Exception as exc:  # noqa: BLE001 - a job must always end in a recorded state
            log.exception("council job %s failed", job_id)
            job["error"] = f"The council run broke ({type(exc).__name__})."
            return self._finish(job, "error")
        finally:
            self.procs.pop(job_id, None)
        if interrupted:
            return
        if timed_out:
            job["error"] = f"Stopped after {self.config.council_timeout_min:g} minutes (council_timeout_min)."
            self._attach_result(job, stdout)
            return self._finish(job, "timeout")
        if job_id in self.cancelled:
            self.cancelled.discard(job_id)
            job["error"] = "Cancelled."
            return self._finish(job, "cancelled", notify=False)
        result = self._attach_result(job, stdout)
        if result is not None:
            return self._finish(job, result["status"])
        last = next((ln for ln in reversed((job.get("log_tail") or "").splitlines()) if ln.strip()), "")
        job["error"] = (f"The council exited with code {code} and printed no result. " if code
                        else "The council finished but printed no result. ") + _one_line(last, 200)
        self._finish(job, "error")

    @staticmethod
    def parse_result(stdout: bytes) -> dict | None:
        """The lab's one JSON object, trimmed to safe sizes. None if there isn't a usable one."""
        text = stdout.decode("utf-8", "replace").strip()
        data = None
        for candidate in [text] + [ln for ln in reversed(text.splitlines()) if ln.strip().startswith("{")][:3]:
            try:
                data = json.loads(candidate)
                break
            except ValueError:
                continue
        if not isinstance(data, dict) or data.get("status") not in LAB_STATUSES:
            return None
        calls = data.get("calls") if isinstance(data.get("calls"), dict) else {}
        files = data.get("files") if isinstance(data.get("files"), list) else []
        stopped = data.get("stopped")
        return {
            "status": data["status"], "answer": str(data.get("answer") or "")[:MAX_ANSWER_CHARS],
            "files": [str(f)[:300] for f in files[:50]],
            "calls": {str(k)[:60]: int(v) for k, v in list(calls.items())[:30]
                      if isinstance(v, (int, float)) and not isinstance(v, bool)},
            "stopped": str(stopped)[:300] if stopped else "",
            "run_dir": str(data.get("run_dir") or "")[:500],
        }

    def _attach_result(self, job: dict, stdout: bytes) -> dict | None:
        result = self.parse_result(stdout)
        if result is not None:
            job["result"] = result
        return result

    def _finish(self, job: dict, status: str, notify: bool = True) -> None:
        job["status"] = status
        job["finished"] = self.clock()
        job.pop("pid", None)
        self._save(job)
        self._prune()
        if notify:
            result = job.get("result") or {}
            body = _one_line(result.get("answer") or job.get("error") or "", 200)
            try:
                self.ctx.notify(f"Council {status}", body, "info" if status == "accepted" else "warn",
                                "council", "council")
            except Exception:  # noqa: BLE001 - a notification problem must not lose the result
                log.exception("council: notify failed")

    # -- cancel / shutdown -----------------------------------------------------------------------------
    async def cancel(self, job_id: str) -> dict:
        self.require_ready_or_view()
        job = self.get(job_id)
        if job["status"] != "running":
            raise CouncilError("That run is not running any more.", 409)
        self.cancelled.add(job_id)
        proc = self.procs.get(job_id)
        if proc is not None:
            await self.runner.kill(proc)
        task = self.tasks.get(job_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), 20)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        self.ctx.audit("council_cancel", job=job_id)
        return self.view(job)

    def require_ready_or_view(self) -> None:
        if not self.enabled():
            raise CouncilError(NOT_SET_UP, 409)

    async def shutdown(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    # -- models ----------------------------------------------------------------------------------------
    @staticmethod
    def _clean_models(text: str) -> list[dict]:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("models")
        if not isinstance(data, list):
            raise CouncilError("The council tool did not return a usable list of models.", 502)
        out = []
        for item in data[:50]:
            if not isinstance(item, dict) or not MODEL_NAME_RE.match(str(item.get("name", ""))):
                continue
            row = {"name": item["name"], "provider": _one_line(item.get("provider"), 40),
                   "model": _one_line(item.get("model"), 80), "kind": _one_line(item.get("kind"), 20),
                   "cost": _one_line(item.get("cost"), 20), "reason": _one_line(item.get("reason"), 200),
                   "enabled": item.get("enabled") is True, "ready": item.get("ready") is True}
            row["paid"] = row["cost"].lower() not in ("", "free", "0", "local")
            row["claude"] = CouncilManager.is_claude(row["name"], row["provider"], row["model"])
            out.append(row)
        return out

    async def _lab(self, *args: str, timeout: float = MODELS_TIMEOUT) -> tuple[int | None, str, str]:
        argv = [resolve_python(self.config), "-m", "lab", *args]
        try:
            return await self.runner.run_once(argv, self.config.council_dir, self.child_env(), timeout, 400_000)
        except OSError as exc:
            raise CouncilError(f"Could not start the council tool ({_one_line(exc, 150)}). Check council_python.",
                               502) from None

    async def models(self) -> list[dict]:
        self.require_ready()
        code, out, err = await self._lab("models", "--json")
        if code is None:
            raise CouncilError("Listing the models took too long and was stopped.", 504)
        if code != 0:
            last = next((ln for ln in reversed(_clean_log(err).splitlines()) if ln.strip()), "")
            raise CouncilError("The council tool could not list its models. "
                               "(Does your multi-llm-lab have `models --json` yet?) " + _one_line(last, 200), 502)
        rows = self._clean_models(out)
        for row in rows:
            if row["claude"]:
                row["note"] = ("Off by default. Claude is only used when you allow it "
                               "(council_allow_claude in hub.json) and a run asks for escalation.")
                if not self.config.council_allow_claude:
                    row["locked"] = True
        return rows

    async def set_model(self, name: str, enabled: Any) -> list[dict]:
        self.require_ready()
        if not isinstance(name, str) or not MODEL_NAME_RE.match(name):
            raise CouncilError("That is not a valid model name.", 400)
        if not isinstance(enabled, bool):
            raise CouncilError("Say enabled: true or false.", 400)
        rows = await self.models()
        row = next((r for r in rows if r["name"] == name), None)
        if row is None:
            raise CouncilError("The council does not have a model with that name.", 404)
        if enabled and row.get("locked"):
            raise CouncilError("Claude is switched off for the council. The owner can allow it with "
                               "council_allow_claude in hub.json.", 409)
        code, _, err = await self._lab("models", f"--{'enable' if enabled else 'disable'}={name}")
        if code != 0:
            last = next((ln for ln in reversed(_clean_log(err or "").splitlines()) if ln.strip()), "")
            raise CouncilError("The council tool could not change that model. " + _one_line(last, 200), 502)
        self.ctx.audit("council_model", model=name, enabled=enabled)
        return await self.models()

    # -- review packet ---------------------------------------------------------------------------------
    async def review_packet(self, job_id: str) -> dict:
        self.require_ready()
        job = self.get(job_id)
        run_dir = (job.get("result") or {}).get("run_dir")
        if job["status"] == "running" or not isinstance(run_dir, str) or not self._safe_run_dir(run_dir):
            raise CouncilError("This run has no saved run folder to make a review packet from.", 409)
        argv = [resolve_python(self.config), "-m", "lab", "review-packet", "--", run_dir]
        try:
            code, out, err = await self.runner.run_once(argv, self.config.council_dir, self.child_env(),
                                                        PACKET_TIMEOUT, MAX_PACKET_CHARS * 4)
        except OSError as exc:
            raise CouncilError(f"Could not start the council tool ({_one_line(exc, 150)}).", 502) from None
        if code is None:
            raise CouncilError("Making the review packet took too long and was stopped.", 504)
        if code != 0:
            last = next((ln for ln in reversed(_clean_log(err).splitlines()) if ln.strip()), "")
            raise CouncilError("The council tool could not make a review packet. "
                               "(Does your multi-llm-lab have `review-packet` yet?) " + _one_line(last, 200), 502)
        text = CONTROL_RE.sub("", out.replace("\r\n", "\n"))
        truncated = len(text) > MAX_PACKET_CHARS
        return {"packet": text[:MAX_PACKET_CHARS], "truncated": truncated, "chars": min(len(text), MAX_PACKET_CHARS)}


# --------------------------------------------------------------------------------------------------- routes

MANAGERS: dict[int, CouncilManager] = {}


def manager_for(ctx: Ctx) -> CouncilManager:
    return MANAGERS[id(ctx)]


def make_router(ctx: Ctx, *, runner: Runner | None = None, clock: Callable[[], float] | None = None) -> APIRouter:
    """`runner` and `clock` exist so tests need no real lab and no real time."""
    mgr = MANAGERS[id(ctx)] = CouncilManager(ctx, runner=runner, clock=clock)
    router = APIRouter(prefix="/api/council", dependencies=[Depends(ctx.need_auth)])
    router.manager = mgr  # type: ignore[attr-defined]

    async def _panic_stop():
        running = [j for j, t in mgr.tasks.items() if not t.done()]
        if running:
            await mgr.shutdown()
        return f"{len(running)} council run(s) cancelled" if running else None
    _panic.register("Council runs", _panic_stop, ctx=ctx)   # hub/panic.py

    def refuse(exc: CouncilError) -> HTTPException:
        return HTTPException(exc.status, str(exc))

    @router.get("/status")
    async def status():
        return mgr.status()

    @router.post("/run")
    async def run(body: Any = Body(default=None)):
        _panic.guard(ctx.config)
        try:
            return mgr.start(body)
        except CouncilError as exc:
            raise refuse(exc) from None

    @router.get("/models")
    async def models():
        try:
            return {"models": await mgr.models()}
        except CouncilError as exc:
            raise refuse(exc) from None

    @router.post("/models/{name}")
    async def set_model(name: str, body: Any = Body(default=None)):
        try:
            enabled = body.get("enabled") if isinstance(body, dict) else None
            return {"models": await mgr.set_model(name, enabled)}
        except CouncilError as exc:
            raise refuse(exc) from None

    @router.get("/jobs")
    async def jobs():
        try:
            mgr.require_ready_or_view()
        except CouncilError as exc:
            raise refuse(exc) from None
        return {"jobs": mgr.listing()}

    @router.get("/jobs/{job_id}")
    async def job(job_id: str):
        try:
            mgr.require_ready_or_view()
            return mgr.view(mgr.get(job_id))
        except CouncilError as exc:
            raise refuse(exc) from None

    @router.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        try:
            return await mgr.cancel(job_id)
        except CouncilError as exc:
            raise refuse(exc) from None

    @router.get("/jobs/{job_id}/review-packet")
    async def packet(job_id: str):
        try:
            return await mgr.review_packet(job_id)
        except CouncilError as exc:
            raise refuse(exc) from None

    return router


async def start(ctx: Ctx):
    mgr = manager_for(ctx)

    async def stop():
        await mgr.shutdown()
        MANAGERS.pop(id(ctx), None)
    return stop
