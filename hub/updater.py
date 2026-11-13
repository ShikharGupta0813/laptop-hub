"""`python -m hub update [--restart]`: pull the latest code from GitHub and (optionally) restart the hub.

It runs as its own scheduled task (LaptopHubUpdate), never as a child of the hub: stopping the LaptopHub task
would otherwise kill the updater half way. Nothing is stopped unless the pull (and pip, if needed) succeeded.
Every effect goes through a `runner(argv, cwd, timeout) -> (rc, output)` so tests never touch git or schtasks.
No secrets are handled here; git's own output is truncated before it is logged.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

TASK_NAME = "LaptopHub"
UPDATE_TASK_NAME = "LaptopHubUpdate"
LOG_NAME = "update.log"
EXIT_OK, EXIT_FAILED, EXIT_DIRTY, EXIT_NOT_GIT = 0, 1, 3, 4
Runner = Callable[[list, str, float], "tuple[int, str]"]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def log_path_for(config_path: Path | None) -> Path:
    """update.log sits next to hub.json (default ~/.laptop-hub)."""
    base = Path(config_path).parent if config_path else Path.home() / ".laptop-hub"
    return base / LOG_NAME


def real_runner(argv: list, cwd: str, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return 127, "program not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 126, type(exc).__name__
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def read_version(root: Path | None = None) -> str:
    """Short commit hash of the checkout, read from .git without running anything. '' if unknown."""
    try:
        git = (root or repo_root()) / ".git"
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            ref_file = git / ref
            if ref_file.is_file():
                head = ref_file.read_text(encoding="utf-8").strip()
            else:
                head = ""
                for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref and not line.startswith(("#", "^")):
                        head = parts[0]
                        break
        return head[:7] if len(head) >= 7 and all(c in "0123456789abcdef" for c in head.lower()) else ""
    except (OSError, UnicodeError):
        return ""


def tail_log(path: Path, lines: int = 20, width: int = 200) -> list[str]:
    """The last few log lines, each truncated. Nothing else of the file is ever returned."""
    lines = max(0, min(lines, 20))
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = [ln.strip()[:width] for ln in data.splitlines() if ln.strip()]
    return out[-lines:] if lines else []


def spawn_detached(argv: list, cwd: str) -> None:
    """Start argv in its own session/process group so it outlives the hub (which the update restarts). No shell."""
    kw: dict = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)  # noqa: S603 - argv list, never a shell


def start_update(config_path: Path | None = None, *, root: Path | None = None, runner: Runner | None = None,
                 spawner: Callable[[list, str], None] | None = None, python: str | None = None) -> dict:
    """The single way the app starts an update (the Settings button). Returns {'method': 'task'|'process'}.
    If the Windows task LaptopHubUpdate exists it is run; otherwise `python -m hub update --restart` is spawned
    detached. Both end in Updater.run(restart=True), the only implementation. Raises OSError when nothing could start."""
    root_dir = str(root or repo_root())
    runner = runner or real_runner
    spawner = spawner or spawn_detached
    rc, _ = runner(["schtasks", "/Query", "/TN", UPDATE_TASK_NAME], root_dir, 15)
    if rc == 0:
        rc, out = runner(["schtasks", "/Run", "/TN", UPDATE_TASK_NAME], root_dir, 30)
        if rc == 0:
            return {"method": "task"}
    argv = [python or sys.executable, "-m", "hub"]
    if config_path:
        argv += ["--config", str(config_path)]
    argv += ["update", "--restart"]
    spawner(argv, root_dir)
    return {"method": "process"}


class Updater:
    def __init__(self, root: Path, log_path: Path, *, runner: Runner = real_runner,
                 sleep: Callable[[float], None] = time.sleep, python: str | None = None,
                 now: Callable[[], dt.datetime] = dt.datetime.now):
        self.root, self.log_path, self.runner, self.sleep = Path(root), Path(log_path), runner, sleep
        self.python = python or sys.executable
        self.now = now

    def log(self, text: str) -> None:
        line = f"{self.now().strftime('%Y-%m-%d %H:%M:%S')} {text}"
        print(line)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def git(self, *args: str, timeout: float = 120) -> tuple[int, str]:
        return self.runner(["git", *args], str(self.root), timeout)

    def run(self, restart: bool = False) -> int:
        try:
            return self._run(restart)
        except Exception as exc:  # noqa: BLE001 - the running hub must survive any updater bug
            self.log(f"FAILED: unexpected {type(exc).__name__}; the running hub was not touched.")
            return EXIT_FAILED

    def _run(self, restart: bool) -> int:
        self.log(f"update started{' (with restart)' if restart else ''} in {self.root}")
        if not (self.root / ".git").exists():
            self.log("FAILED: this folder is not a git checkout, so there is nothing to pull. Hub not touched.")
            return EXIT_NOT_GIT
        rc, out = self.git("status", "--porcelain", "--untracked-files=no", timeout=60)
        if rc != 0:
            self.log(f"FAILED: git status failed ({out[:150]}). Hub not touched.")
            return EXIT_FAILED
        if out.strip():
            count = len([ln for ln in out.splitlines() if ln.strip()])
            self.log(f"REFUSED: {count} tracked file(s) have local changes, so nothing was pulled. "
                     "Commit or discard them (git status), then try again. Hub not touched.")
            return EXIT_DIRTY
        rc, old = self.git("rev-parse", "HEAD", timeout=30)
        if rc != 0:
            self.log("FAILED: could not read the current commit. Hub not touched.")
            return EXIT_FAILED
        old = old.strip()
        rc, out = self.git("fetch", timeout=180)
        if rc != 0:
            self.log(f"FAILED: git fetch failed (no network, or not signed in to GitHub): {out[-150:]}. Hub not touched.")
            return EXIT_FAILED
        rc, out = self.git("pull", "--ff-only", timeout=180)
        if rc != 0:
            self.log(f"FAILED: git pull --ff-only failed (local commits that GitHub does not have?): {out[-150:]}. Hub not touched.")
            return EXIT_FAILED
        rc, new = self.git("rev-parse", "HEAD", timeout=30)
        new = new.strip() if rc == 0 else ""
        if not new:
            self.log("FAILED: could not read the new commit. Hub not touched.")
            return EXIT_FAILED
        if new == old:
            self.log(f"already up to date at {old[:7]}")
            return EXIT_OK
        self.log(f"pulled {old[:7]} -> {new[:7]}")
        rc, changed = self.git("diff", "--name-only", old, new, "--", "requirements.txt", timeout=60)
        if rc == 0 and changed.strip():
            self.log("requirements.txt changed; installing dependencies")
            rc, out = self.runner([self.python, "-m", "pip", "install", "-r", "requirements.txt"], str(self.root), 900)
            if rc != 0:
                self.log(f"FAILED: pip install failed ({out[-150:]}). Code is updated but the hub was not restarted.")
                return EXIT_FAILED
            self.log("dependencies installed")
        if not restart:
            self.log(f"updated to {new[:7]}; restart the hub to use it")
            return EXIT_OK
        return self.restart_hub(new)

    def restart_hub(self, new: str) -> int:
        rc, _ = self.runner(["schtasks", "/End", "/TN", TASK_NAME], str(self.root), 60)
        if rc != 0:
            self.log(f"note: schtasks /End {TASK_NAME} returned {rc} (it may not have been running)")
        self.sleep(3)
        rc, out = self.runner(["schtasks", "/Run", "/TN", TASK_NAME], str(self.root), 60)
        if rc != 0:
            self.log(f"FAILED: could not start {TASK_NAME} again ({out[-150:]}). Start the hub by hand: python -m hub serve")
            return EXIT_FAILED
        self.log(f"hub restarted on {new[:7]}")
        return EXIT_OK
