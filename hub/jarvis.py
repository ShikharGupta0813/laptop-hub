"""JARVIS: start and stop the owner's Jarvis voice assistant, and queue text commands for it.

Routes are under /api/jarvis (see docs/API.md). Jarvis is a separate project (config.jarvis_dir).
The hub starts it the way its own jarvis.cmd does (the folder's .venv python running jarvis.py),
detached, and remembers the process id in <data>/jarvis.json so a hub restart still knows.

"Running" means: a process with that id exists, is not a zombie, and its command line names
jarvis.py. If the remembered id is gone, the hub also looks for a Jarvis the owner started by hand
from the same folder, so it never starts a second one.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool

from .common import Ctx, JsonFile

SCRIPT = "jarvis.py"
LOG_NAME = "jarvis.log"
INBOX_SCRIPT = "remote_inbox.py"
INBOX_DIR = "remote_inbox"
STATUS_LINES = 30
MAX_LOG_LINES = 500
SAY_MAX = 2000
OUTPUT_FILE = "jarvis-output.log"
OUTPUT_MAX_BYTES = 2_000_000
STOP_GRACE_SECONDS = 5.0

Command = Callable[[Path], "list[str] | None"]
Launcher = Callable[[list, Path, Path], int]


def default_command(jarvis_dir: Path) -> list[str] | None:
    """What jarvis.cmd runs: the folder's own .venv python on jarvis.py. None if it isn't there."""
    for rel in (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python"):
        python = jarvis_dir / rel
        if python.is_file():
            return [str(python), SCRIPT]
    return None


_spawned: list[subprocess.Popen] = []     # kept so a finished Jarvis is reaped, not left a zombie


def default_launcher(argv: list[str], cwd: Path, output: Path) -> int:
    """Starts Jarvis detached, with stdout and stderr in `output`. Returns its process id."""
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        if output.stat().st_size > OUTPUT_MAX_BYTES:
            output.write_bytes(b"")
    except OSError:
        pass
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    with open(output, "ab") as out:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, env=env, **kwargs)
    _spawned[:] = [p for p in _spawned if p.poll() is None] + [proc]
    return proc.pid


def tail_lines(path: Path, count: int) -> list[str]:
    """The last `count` lines of a text file, reading only the end of it."""
    if count <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            data = b""
            while pos > 0 and data.count(b"\n") <= count:
                step = min(8192, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]           # the first line may have been cut in half
    return [line.rstrip("\r") for line in lines[-count:]]


def _is_jarvis(proc: psutil.Process) -> bool:
    try:
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return any(os.path.basename(arg).lower() == SCRIPT for arg in proc.cmdline())
    except (psutil.Error, OSError):
        return False


def _same_dir(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(str(b)))
    except OSError:
        return False


def _alive(proc: psutil.Process) -> bool:
    """Still running. A zombie (ended, waiting to be reaped) does not count."""
    try:
        if proc.status() != psutil.STATUS_ZOMBIE:
            return True
        try:
            proc.wait(0)                # reaps it if it is our own child
        except psutil.Error:
            pass
        return False
    except psutil.Error:
        return False


def wait_gone(procs: list[psutil.Process], timeout: float) -> list[psutil.Process]:
    """Waits until the processes have ended; returns the ones still running after `timeout`."""
    deadline = time.monotonic() + timeout
    alive = [p for p in procs if _alive(p)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = [p for p in alive if _alive(p)]
    return alive


class StopBody(BaseModel):
    confirm: StrictBool = False


class SayBody(BaseModel):
    text: str = ""


def make_router(ctx: Ctx, *, command: Command | None = None, launcher: Launcher | None = None) -> APIRouter:
    """`command` (jarvis_dir -> argv) and `launcher` (argv, cwd, output file -> pid) exist for tests.
    Passing `command` also lifts the "Windows only" rule, so a test can start a harmless stand-in."""
    router = APIRouter(prefix="/api/jarvis", dependencies=[Depends(ctx.need_auth)])
    lock = threading.Lock()
    config = ctx.config
    launch = launcher or default_launcher
    make_command = command or default_command
    state_file = JsonFile(config.data_path() / "jarvis.json")

    # ---- configuration and process state --------------------------------------------------------

    def setup() -> tuple[Path | None, list[str] | None, str]:
        """(jarvis folder, launch command, "") or (None, None, the one-sentence reason)."""
        raw = (config.jarvis_dir or "").strip()
        if not raw:
            return None, None, "Jarvis is not set up: set jarvis_dir in hub.json to your jarvis-voice-assistant folder."
        folder = Path(raw)
        if not folder.is_dir():
            return None, None, f"The Jarvis folder {raw} does not exist. Fix jarvis_dir in hub.json."
        if not (folder / SCRIPT).is_file():
            return None, None, f"There is no {SCRIPT} in {raw}. Fix jarvis_dir in hub.json."
        if command is None and sys.platform != "win32":
            return None, None, "Jarvis can only be started from a Windows laptop."
        argv = make_command(folder)
        if not argv:
            return None, None, (f"Jarvis's own Python (.venv) was not found in {raw}. "
                                "Set it up as its README says, then try again.")
        return folder, argv, ""

    def find_running(folder: Path) -> tuple[int, str | None] | None:
        """(pid, started_at) of the live Jarvis, from the remembered pid or else a scan, or None."""
        saved = state_file.load({})
        pid = saved.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                proc = psutil.Process(pid)
                created = float(saved.get("create_time") or 0)
                if _is_jarvis(proc) and (not created or abs(proc.create_time() - created) < 2):
                    return pid, saved.get("started_at")
            except psutil.Error:
                pass
        # Not the one we remember: maybe the owner started Jarvis by hand from this folder.
        found: dict[int, psutil.Process] = {}
        for proc in psutil.process_iter(["pid", "ppid"]):
            if _is_jarvis(proc):
                try:
                    if _same_dir(proc.cwd(), folder):
                        found[proc.pid] = proc
                except (psutil.Error, OSError):
                    continue
        tops = [p for p in found.values() if p.info.get("ppid") not in found]
        if tops:
            proc = tops[0]
            try:
                started = dt.datetime.fromtimestamp(proc.create_time()).astimezone().isoformat(timespec="seconds")
                state_file.save({"pid": proc.pid, "create_time": proc.create_time(), "started_at": started})
                return proc.pid, started
            except psutil.Error:
                return None
        return None

    def status() -> dict:
        folder, _argv, reason = setup()
        if folder is None:
            return {"configured": False, "reason": reason, "running": False, "pid": None,
                    "started_at": None, "log_tail": [], "remote_ready": False}
        live = find_running(folder)
        out = {"configured": True, "running": live is not None, "pid": live[0] if live else None,
               "started_at": live[1] if live else None,
               "log_tail": tail_lines(folder / LOG_NAME, STATUS_LINES),
               "remote_ready": (folder / INBOX_SCRIPT).is_file()}
        if live is None:
            # Jarvis may have crashed before it could write its own log: show what it printed.
            out["output_tail"] = tail_lines(config.data_path() / OUTPUT_FILE, 15)
        return out

    def need_setup() -> tuple[Path, list[str]]:
        folder, argv, reason = setup()
        if folder is None or argv is None:
            raise HTTPException(503, reason)
        return folder, argv

    def end_tree(pid: int) -> None:
        """Ends Jarvis and everything it started: politely first, then by force."""
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        procs = [root]
        try:
            procs += root.children(recursive=True)
        except psutil.Error:
            pass
        for proc in procs:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        alive = wait_gone(procs, STOP_GRACE_SECONDS)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
        alive = wait_gone(alive, STOP_GRACE_SECONDS)
        if alive:
            raise HTTPException(500, "Jarvis did not stop. Close it on the laptop (Task Manager, python.exe).")

    # ---- routes -----------------------------------------------------------------------------

    @router.get("/status")
    def get_status():
        return status()

    @router.post("/start")
    def start():
        folder, argv = need_setup()
        with lock:                       # two taps at once must not start two Jarvises
            if find_running(folder) is None:
                try:
                    pid = launch(argv, folder, config.data_path() / OUTPUT_FILE)
                except OSError as exc:
                    raise HTTPException(500, f"Jarvis could not be started: {' '.join(str(exc).split())[:150]}")
                created = None
                try:
                    created = psutil.Process(pid).create_time()
                except psutil.Error:
                    pass
                state_file.save({"pid": pid, "create_time": created,
                                 "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
                ctx.audit("jarvis_start", pid=pid)
        return status()

    @router.post("/stop")
    def stop(body: StopBody | None = None):
        folder, _argv = need_setup()
        if body is None or body.confirm is not True:
            raise HTTPException(409, "Stopping Jarvis needs your confirmation. Confirm it and send again.")
        with lock:
            live = find_running(folder)
            if live is not None:
                end_tree(live[0])
                ctx.audit("jarvis_stop", pid=live[0])
            state_file.save({})
        return status()

    @router.get("/log")
    def get_log(lines: int = Query(100, ge=1)):
        folder, _argv = need_setup()
        return {"lines": tail_lines(folder / LOG_NAME, min(lines, MAX_LOG_LINES))}

    @router.post("/say")
    def say(body: SayBody):
        folder, _argv = need_setup()
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Type something for Jarvis to do.")
        if len(text) > SAY_MAX:
            raise HTTPException(400, f"That is too long for Jarvis (the limit is {SAY_MAX:,} characters).")
        if find_running(folder) is None:
            raise HTTPException(409, "Jarvis is not running. Start it first.")
        if not (folder / INBOX_SCRIPT).is_file():
            raise HTTPException(409, "Jarvis cannot take typed commands yet: the small remote-inbox patch "
                                     "is not installed in the Jarvis folder (see docs/jarvis-remote-inbox.md).")
        inbox = folder / INBOX_DIR
        inbox.mkdir(exist_ok=True)
        item = f"{time.time_ns():020d}-{secrets.token_hex(3)}"
        tmp = inbox / f".{item}.tmp"           # Jarvis only reads *.txt, so it never sees half a file
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, inbox / f"{item}.txt")
        ctx.audit("jarvis_say", id=item, chars=len(text))
        return {"queued": True, "id": item}

    return router
