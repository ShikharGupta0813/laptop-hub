"""LAPTOP: a registry of actions the phone can run on the laptop (see docs/API.md).

Adding an action is one entry: build an `Action(...)` in `builtin_actions` below. The phone lists
the actions and draws a generic form from each one's `params`, so no UI work is needed.

Every call that touches the operating system goes through one small `System` object, so tests
swap it for a fake and never lock, sleep or press a key for real. Nothing here ever builds a
shell string: programs run from argument lists, and text from the phone (the clipboard) travels
on stdin, never inside a command line.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import logging
import math
import os
import platform as platform_mod
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool

from .common import Ctx

log = logging.getLogger("hub.laptop")

SCRIPT_TIMEOUT = 120            # seconds, the same as the existing /api/run
CLIPBOARD_MAX = 100_000         # characters, both directions
SCREENSHOT_MAX_SIDE = 1600      # pixels
KILL_GRACE_SECONDS = 3.0        # terminate, wait this long, then kill
TOP_PROCESSES = 30
IS_WINDOWS = sys.platform == "win32"

CATEGORIES = [
    ("system", "System"), ("processes", "Processes"), ("launch", "Open"), ("screen", "Screen and power"),
    ("clipboard", "Clipboard"), ("media", "Media"), ("apps", "Apps"), ("buttons", "Buttons"),
]


class ActionError(Exception):
    """A failure with a plain sentence for the phone. `status` is the HTTP code (default 500)."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


class BadParams(ActionError):
    def __init__(self, message: str):
        super().__init__(message, 400)


# ---------------------------------------------------------------------------------- the OS layer

class System:
    """Every operating-system call the actions make. Tests replace this whole object."""

    platform = sys.platform          # "win32", "darwin" or "linux"

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def has_pillow(self) -> bool:
        try:
            import PIL.ImageGrab  # noqa: F401
            return True
        except Exception:
            return False

    def has_display(self) -> bool:
        if self.platform in ("win32", "darwin"):
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def run(self, argv: list[str], input_text: str | None = None, timeout: float = 20,
            cwd: str | None = None) -> tuple[int, str, str]:
        """Runs a program (no shell). Returns (exit code, stdout, stderr).
        Raises FileNotFoundError or subprocess.TimeoutExpired."""
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        result = subprocess.run(
            argv, input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True, timeout=timeout, cwd=cwd or None, creationflags=flags)
        return (result.returncode, result.stdout.decode("utf-8", "replace"),
                result.stderr.decode("utf-8", "replace"))

    def launch(self, argv: list[str]) -> None:
        """Starts a program and does not wait for it."""
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)

    def open_default(self, path: str) -> None:
        """Opens a file or folder with the default app."""
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        elif self.platform == "darwin":
            self.launch(["open", path])
        else:
            self.launch(["xdg-open", path])

    def open_url(self, url: str) -> bool:
        return bool(webbrowser.open(url))

    def start_app(self, target: str, args: list[str]) -> None:
        """Launches an owner-configured app. An argument list or os.startfile, never a shell string."""
        if IS_WINDOWS:
            if args:
                self.launch([target, *args])
            else:
                os.startfile(target)  # type: ignore[attr-defined]
        elif self.platform == "darwin":
            if os.sep in target or self.which(target):
                self.launch([target, *args])
            else:
                self.launch(["open", "-a", target, *(["--args", *args] if args else [])])
        else:
            if self.which(target) or os.sep in target:
                self.launch([target, *args])
            else:
                self.launch(["xdg-open", target])

    def grab_screen(self):
        from PIL import ImageGrab
        if IS_WINDOWS:
            return ImageGrab.grab(all_screens=True)
        return ImageGrab.grab()

    def press_media_key(self, name: str, times: int = 1) -> None:
        import ctypes
        vk = MEDIA_KEYS[name]
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        for _ in range(times):
            user32.keybd_event(vk, 0, 1, 0)       # 1 = KEYEVENTF_EXTENDEDKEY
            user32.keybd_event(vk, 0, 1 | 2, 0)   # 2 = KEYEVENTF_KEYUP
            time.sleep(0.02)

    def protected_pids(self) -> set[int]:
        """The hub itself and every process above it (the shell or service that started it)."""
        import psutil
        pids = {os.getpid()}
        try:
            pids.update(p.pid for p in psutil.Process().parents())
        except psutil.Error:
            pass
        return pids

    def terminate_process(self, pid: int) -> str:
        """Ends one process politely, then forcefully. Returns 'terminated' or 'killed'."""
        import psutil
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(KILL_GRACE_SECONDS)
                return "terminated"
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(KILL_GRACE_SECONDS)
                return "killed"
        except psutil.NoSuchProcess:
            raise ActionError(f"There is no process with id {pid} (it may have just ended).", 400)
        except psutil.AccessDenied:
            raise ActionError(f"Windows will not let the hub end process {pid} (it needs administrator rights).")


MEDIA_KEYS = {"volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
              "next_track": 0xB0, "previous_track": 0xB1, "play_pause": 0xB3}


# ------------------------------------------------------------------------------ registry building

@dataclass
class Param:
    name: str
    label: str
    type: str = "text"              # text | number | choice | bool
    required: bool = True
    choices: list[str] | None = None
    placeholder: str | None = None
    default: Any = None
    integer: bool = False
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 2000

    def public(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "label": self.label, "type": self.type,
                               "required": self.required}
        if self.choices is not None:
            out["choices"] = list(self.choices)
        if self.placeholder:
            out["placeholder"] = self.placeholder
        return out


Availability = Callable[[], tuple[bool, str]]
Runner = Callable[[dict], Awaitable[dict]]


def always() -> tuple[bool, str]:
    return True, ""


@dataclass
class Action:
    id: str
    title: str
    category: str
    description: str
    run: Runner
    risk: str = "safe"                         # "safe" or "confirm"
    params: list[Param] = field(default_factory=list)
    available: Availability = always
    sensitive: tuple[str, ...] = ()            # params that are never written to the audit log
    hidden: bool = False                       # runnable by id but not listed

    def public(self) -> dict:
        ok, reason = self.available()
        out = {"id": self.id, "title": self.title, "category": self.category,
               "description": self.description, "risk": self.risk,
               "params": [p.public() for p in self.params], "available": bool(ok)}
        if not ok:
            out["unavailable_reason"] = reason or "Not available on this laptop."
        return out


def validate(params: list[Param], raw: dict) -> dict:
    """Checks `raw` against the schema. Returns the clean values; raises BadParams (a sentence)."""
    known = {p.name for p in params}
    for key in raw:
        if key not in known:
            raise BadParams(f"'{key}' is not an option of this action.")
    clean: dict[str, Any] = {}
    for p in params:
        value = raw.get(p.name)
        if value is None or (isinstance(value, str) and value == "" and p.type != "text"):
            if p.required:
                raise BadParams(f"{p.label} is required.")
            if p.default is not None:
                clean[p.name] = p.default
            continue
        if p.type == "text":
            if not isinstance(value, str):
                raise BadParams(f"{p.label} must be text.")
            if value == "":
                if p.required:
                    raise BadParams(f"{p.label} is required.")
                continue
            if len(value) > p.max_length:
                raise BadParams(f"{p.label} is too long (the limit is {p.max_length:,} characters).")
            if "\x00" in value:
                raise BadParams(f"{p.label} contains a character that is not allowed.")
            clean[p.name] = value
        elif p.type == "number":
            if isinstance(value, bool):
                raise BadParams(f"{p.label} must be a number.")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise BadParams(f"{p.label} must be a number.") from None
            if not math.isfinite(number):
                raise BadParams(f"{p.label} must be a number.")
            if p.integer:
                if number != int(number):
                    raise BadParams(f"{p.label} must be a whole number.")
                number = int(number)
            if p.minimum is not None and number < p.minimum:
                raise BadParams(f"{p.label} must be at least {p.minimum:g}.")
            if p.maximum is not None and number > p.maximum:
                raise BadParams(f"{p.label} must be at most {p.maximum:g}.")
            clean[p.name] = number
        elif p.type == "choice":
            if str(value) not in (p.choices or []):
                raise BadParams(f"{p.label} must be one of: {', '.join(p.choices)}." if p.choices
                                else f"There is nothing to choose for {p.label} yet.")
            clean[p.name] = str(value)
        elif p.type == "bool":
            if isinstance(value, bool):
                clean[p.name] = value
            elif str(value).lower() in ("true", "1", "yes", "on"):
                clean[p.name] = True
            elif str(value).lower() in ("false", "0", "no", "off"):
                clean[p.name] = False
            else:
                raise BadParams(f"{p.label} must be yes or no.")
        else:  # a registry bug, not the phone's fault
            raise ActionError(f"The action has an unknown option type '{p.type}'.")
    return clean


def audit_fields(action: Action, params: dict) -> dict:
    """The params that may go in the audit log: sensitive ones are replaced by their length."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in action.sensitive:
            out[f"{key}_chars"] = len(value) if isinstance(value, str) else 0
        elif isinstance(value, str):
            out[key] = value[:200]
        else:
            out[key] = value
    return out


def text(message: str, body: str | None = None) -> dict:
    return {"kind": "text", "message": message, "text": body if body is not None else message}


def done(message: str) -> dict:
    return {"kind": "none", "message": message}


def table(message: str, columns: list[str], rows: list[list]) -> dict:
    return {"kind": "table", "message": message, "columns": columns, "rows": rows}


def _gb(n: float) -> float:
    return round(n / 1024 ** 3, 1)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts)


def safe_text(exc: BaseException) -> str:
    """An exception as one short sentence."""
    return " ".join((str(exc) or type(exc).__name__).split())[:200]


def scale_png(image, max_side: int = SCREENSHOT_MAX_SIDE) -> tuple[bytes, tuple[int, int]]:
    """A PIL image as PNG bytes whose longest side is at most `max_side` (never enlarged)."""
    image = image.convert("RGB")
    longest = max(image.size)
    if longest > max_side:
        ratio = max_side / longest
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), image.size


def valid_url(raw: str) -> str:
    """The URL if it is plain http(s) with a host; otherwise BadParams."""
    candidate = raw.strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        raise BadParams("That is not a web address (it has spaces or hidden characters).")
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError:
        raise BadParams("That is not a valid web address.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise BadParams("Only web addresses starting with http:// or https:// can be opened.")
    if not host:
        raise BadParams("That web address has no site name in it.")
    return candidate


def builtin_actions(system: System) -> list[Action]:
    """Every built-in action. To add one, add an `Action(...)` to this list."""
    win = lambda: system.platform == "win32"
    only_windows = lambda what: (lambda: (win(), "" if win() else f"{what} only works on a Windows laptop."))

    async def blocking(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    # ---- system -------------------------------------------------------------------------------
    def need_psutil():
        import psutil
        return psutil

    async def system_info(_):
        def work():
            psutil = need_psutil()
            mem = psutil.virtual_memory()
            lines = [f"Computer: {socket.gethostname()}",
                     f"System: {platform_mod.platform()}",
                     f"Processor: {platform_mod.processor() or platform_mod.machine()}",
                     f"Cores: {psutil.cpu_count(logical=False) or '?'} physical, {psutil.cpu_count()} logical",
                     f"CPU in use: {psutil.cpu_percent(interval=0.3):.0f}%",
                     f"Memory: {_gb(mem.used)} of {_gb(mem.total)} GB in use ({mem.percent:.0f}%)"]
            return "\n".join(lines)
        body = await blocking(work)
        return text("System information", body)

    def battery_available():
        try:
            import psutil
            if psutil.sensors_battery() is None:
                return False, "This laptop reports no battery."
        except Exception:
            return False, "Battery information is not available on this system."
        return True, ""

    async def battery(_):
        psutil = need_psutil()
        info = psutil.sensors_battery()
        if info is None:
            raise ActionError("This laptop reports no battery.", 501)
        state = "plugged in" if info.power_plugged else "on battery"
        line = f"{info.percent:.0f}%, {state}"
        if not info.power_plugged and info.secsleft and info.secsleft > 0:
            line += f", about {_duration(info.secsleft)} left"
        return text(f"Battery: {line}", f"Battery: {line}")

    async def disk_usage(_):
        def work():
            psutil = need_psutil()
            rows, seen = [], set()
            for part in psutil.disk_partitions(all=False):
                if "cdrom" in part.opts or not part.fstype or part.mountpoint in seen:
                    continue
                seen.add(part.mountpoint)
                try:
                    use = psutil.disk_usage(part.mountpoint)
                except OSError:
                    continue   # an empty card reader, a disconnected network drive
                rows.append([part.mountpoint, _gb(use.total), _gb(use.used), _gb(use.free), round(use.percent)])
            return rows
        rows = await blocking(work)
        return table(f"{len(rows)} drive{'s' if len(rows) != 1 else ''}",
                     ["Drive", "Total GB", "Used GB", "Free GB", "Used %"], rows)

    async def network_info(_):
        def work():
            psutil = need_psutil()
            lines = [f"Computer name: {socket.gethostname()}"]
            for name, addrs in psutil.net_if_addrs().items():
                ips = [a.address.split("%")[0] for a in addrs
                       if a.family in (socket.AF_INET, socket.AF_INET6)
                       and not a.address.startswith(("127.", "::1", "fe80"))]
                if ips:
                    lines.append(f"{name}: {', '.join(ips)}")
            return "\n".join(lines)
        return text("Network information", await blocking(work))

    async def uptime(_):
        psutil = need_psutil()
        seconds = time.time() - psutil.boot_time()
        since = dt.datetime.fromtimestamp(psutil.boot_time()).strftime("%a %d %b, %H:%M")
        return text(f"Up for {_duration(seconds)}", f"Up for {_duration(seconds)} (since {since})")

    # ---- processes ----------------------------------------------------------------------------
    async def list_processes(_):
        def work():
            psutil = need_psutil()
            procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
            for p in procs:
                try:
                    p.cpu_percent(None)       # the first reading is always 0; prime it
                except psutil.Error:
                    pass
            time.sleep(0.25)
            rows = []
            for p in procs:
                try:
                    mem = p.info["memory_info"]
                    if mem is None:
                        continue
                    rows.append([p.info["pid"], p.info["name"] or "?", round(mem.rss / 1024 ** 2, 1),
                                 round(p.cpu_percent(None), 1)])
                except psutil.Error:
                    continue
            rows.sort(key=lambda r: r[2], reverse=True)
            return rows[:TOP_PROCESSES]
        rows = await blocking(work)
        return table(f"Top {len(rows)} programs by memory", ["PID", "Name", "Memory (MB)", "CPU %"], rows)

    async def kill_process(params):
        pid = params["pid"]
        if pid <= 4 or pid in system.protected_pids():
            raise BadParams("That process is the hub itself (or the program that started it), "
                            "or a core system process. It will not be ended from here.")
        how = await blocking(system.terminate_process, pid)
        return done(f"Process {pid} was {how}.")

    # ---- open things --------------------------------------------------------------------------
    async def open_url(params):
        url = valid_url(params["url"])
        if not await blocking(system.open_url, url):
            raise ActionError("The laptop could not open a browser for that address.")
        return done("Opened the address on the laptop.")

    async def open_path(params):
        raw = params["path"].strip().strip('"')
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            raise BadParams("Give the full path, for example C:\\Users\\you\\Documents\\notes.txt.")
        if not path.exists():
            raise BadParams("That file or folder does not exist on the laptop.")
        await blocking(system.open_default, str(path))
        return done(f"Opened {path.name or str(path)} on the laptop.")

    # ---- screen and power ---------------------------------------------------------------------
    def screenshot_available():
        if not system.has_pillow():
            return False, "Screenshots need Pillow. Run: pip install -r requirements-laptop.txt"
        if not system.has_display():
            return False, "This computer has no screen the hub can capture."
        return True, ""

    async def screenshot(_):
        def work():
            return scale_png(system.grab_screen())
        try:
            png, (w, h) = await blocking(work)
        except ActionError:
            raise
        except Exception as exc:
            raise ActionError(f"Could not capture the screen: {safe_text(exc)}")
        return {"kind": "image", "message": f"Screenshot of the laptop screen ({w}x{h})",
                "image_b64": base64.b64encode(png).decode("ascii"), "mime": "image/png"}

    def command_action(action_id: str, title: str, description: str, table_: dict[str, list[str]],
                       message: str, risk: str = "safe", reason: str = "") -> Action:
        """An action that runs one fixed program per platform."""
        def argv() -> list[str] | None:
            return table_.get(system.platform)

        def available():
            cmd = argv()
            if cmd is None:
                return False, reason or f"{title} is not supported on this operating system."
            if system.platform != "win32" and not system.which(cmd[0]):
                return False, f"{cmd[0]} is not installed on this computer."
            return True, ""

        async def run(_):
            cmd = argv()
            if cmd is None:
                raise ActionError(f"{title} is not supported on this operating system.", 501)
            try:
                code, out, err = await blocking(system.run, cmd, None, 20)
            except FileNotFoundError:
                raise ActionError(f"{cmd[0]} was not found on the laptop.")
            except subprocess.TimeoutExpired:
                raise ActionError(f"{title} did not finish in time.")
            if code != 0:
                raise ActionError(f"{title} failed: {safe_text(Exception(err.strip() or out.strip() or f'exit code {code}'))}")
            return done(message)

        return Action(action_id, title, "screen", description, run, risk=risk, available=available)

    # ---- clipboard ----------------------------------------------------------------------------
    PS = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    CLIP_GET = {"win32": PS + ["[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                "darwin": ["pbpaste"]}
    CLIP_SET = {"win32": PS + ["[Console]::InputEncoding=[Text.Encoding]::UTF8; "
                                "$t=[Console]::In.ReadToEnd(); Set-Clipboard -Value $t"],
                "darwin": ["pbcopy"]}

    def linux_clip(setting: bool) -> list[str] | None:
        for exe, get, put in (("wl-paste", ["wl-paste", "--no-newline"], ["wl-copy"]),
                              ("xclip", ["xclip", "-selection", "clipboard", "-o"],
                               ["xclip", "-selection", "clipboard", "-i"]),
                              ("xsel", ["xsel", "-ob"], ["xsel", "-ib"])):
            if system.which(exe):
                return put if setting else get
        return None

    def clip_argv(setting: bool) -> list[str] | None:
        if system.platform == "linux":
            return linux_clip(setting)
        return (CLIP_SET if setting else CLIP_GET).get(system.platform)

    def clip_available(setting: bool):
        def check():
            if clip_argv(setting) is None:
                return False, "No clipboard tool was found (on Linux install wl-clipboard, xclip or xsel)."
            return True, ""
        return check

    async def clipboard_run(argv, input_text=None):
        try:
            code, out, err = await blocking(system.run, argv, input_text, 20)
        except FileNotFoundError:
            raise ActionError("The clipboard tool was not found on the laptop.")
        except subprocess.TimeoutExpired:
            raise ActionError("The clipboard did not answer in time.")
        if code != 0:
            raise ActionError("The laptop's clipboard could not be used right now.")
        return out

    async def get_clipboard(_):
        out = await clipboard_run(clip_argv(False))
        if len(out) > CLIPBOARD_MAX:
            out = out[:CLIPBOARD_MAX]
            note = f" (shortened to the first {CLIPBOARD_MAX:,})"
        else:
            note = ""
        if not out:
            return text("The clipboard is empty (or holds something that is not text).", "")
        return text(f"Clipboard text, {len(out):,} characters{note}", out)

    async def set_clipboard(params):
        value = params["text"]
        await clipboard_run(clip_argv(True), value)      # on stdin, never in the command line
        return done(f"Copied {len(value):,} characters to the laptop's clipboard.")

    # ---- media keys ---------------------------------------------------------------------------
    def media_action(action_id: str, title: str, key: str, message: str, steps: bool = False,
                     playerctl: str | None = None) -> Action:
        def available():
            if win():
                return True, ""
            if playerctl and system.which("playerctl"):
                return True, ""
            return False, "Media keys only work on a Windows laptop."

        async def run(params):
            times = int(params.get("steps", 5)) if steps else 1
            if win():
                await blocking(system.press_media_key, key, times)
            else:
                code, _, _ = await blocking(system.run, ["playerctl", playerctl], None, 10)
                if code != 0:
                    raise ActionError("No media player answered.")
            return done(message)

        params = [Param("steps", "How many steps (each is about 2%)", "number", required=False,
                        default=5, integer=True, minimum=1, maximum=25, placeholder="5")] if steps else []
        return Action(action_id, title, "media", f"{title} on the laptop.", run, params=params,
                      available=available)

    return [
        Action("system_info", "System information", "system", "Computer name, system, processor and memory use.",
               system_info),
        Action("battery", "Battery", "system", "Charge level and whether it is plugged in.", battery,
               available=battery_available),
        Action("disk_usage", "Disk space", "system", "Size and free space of each drive.", disk_usage),
        Action("network_info", "Network", "system", "The computer name and its IP addresses.", network_info),
        Action("uptime", "Uptime", "system", "How long the laptop has been on.", uptime),

        Action("list_processes", "Running programs", "processes",
               f"The {TOP_PROCESSES} programs using the most memory.", list_processes),
        Action("kill_process", "End a program", "processes",
               "Stops one running program by its process id (see Running programs).", kill_process,
               risk="confirm", params=[Param("pid", "Process id", "number", integer=True, minimum=1,
                                             placeholder="1234")]),

        Action("open_url", "Open a web address", "launch", "Opens an http or https address in the laptop's browser.",
               open_url, params=[Param("url", "Web address", placeholder="https://example.com", max_length=2000)]),
        Action("open_path", "Open a file or folder", "launch",
               "Opens an existing file or folder on the laptop with its default app.", open_path,
               params=[Param("path", "Full path", placeholder="C:\\Users\\you\\Documents", max_length=1000)]),

        Action("screenshot", "Screenshot", "screen", "A picture of what is on the laptop screen now.", screenshot,
               available=screenshot_available),
        command_action("lock_screen", "Lock the screen", "Locks the laptop (like Win+L).",
                       {"win32": ["rundll32.exe", "user32.dll,LockWorkStation"],
                        "darwin": ["pmset", "displaysleepnow"], "linux": ["loginctl", "lock-session"]},
                       "The laptop is locked."),
        command_action("sleep", "Sleep", "Puts the laptop to sleep. The hub is unreachable until it wakes.",
                       {"win32": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                        "darwin": ["pmset", "sleepnow"], "linux": ["systemctl", "suspend"]},
                       "The laptop is going to sleep.", risk="confirm"),
        command_action("restart", "Restart", "Restarts the laptop after 30 seconds.",
                       {"win32": ["shutdown", "/r", "/t", "30"]}, "The laptop restarts in 30 seconds. "
                       "Use Cancel shutdown to stop it.", risk="confirm",
                       reason="Restart from the phone only works on a Windows laptop."),
        command_action("shutdown", "Shut down", "Shuts the laptop down after 30 seconds.",
                       {"win32": ["shutdown", "/s", "/t", "30"]}, "The laptop shuts down in 30 seconds. "
                       "Use Cancel shutdown to stop it.", risk="confirm",
                       reason="Shut down from the phone only works on a Windows laptop."),
        command_action("cancel_shutdown", "Cancel shutdown", "Cancels a pending restart or shutdown.",
                       {"win32": ["shutdown", "/a"]}, "The pending shutdown or restart was cancelled.",
                       reason="Only works on a Windows laptop."),

        Action("get_clipboard", "Read the clipboard", "clipboard", "Shows the text on the laptop's clipboard.",
               get_clipboard, available=clip_available(False)),
        Action("set_clipboard", "Set the clipboard", "clipboard", "Puts text on the laptop's clipboard.",
               set_clipboard, params=[Param("text", "Text", placeholder="Text to copy", max_length=CLIPBOARD_MAX)],
               available=clip_available(True), sensitive=("text",)),

        media_action("volume_up", "Volume up", "volume_up", "Volume raised.", steps=True),
        media_action("volume_down", "Volume down", "volume_down", "Volume lowered.", steps=True),
        media_action("mute", "Mute", "volume_mute", "Mute toggled."),
        media_action("play_pause", "Play or pause", "play_pause", "Play/pause pressed.", playerctl="play-pause"),
        media_action("next_track", "Next track", "next_track", "Skipped to the next track.", playerctl="next"),
        media_action("previous_track", "Previous track", "previous_track", "Went back a track.",
                     playerctl="previous"),
    ]


def config_actions(config, system: System) -> list[Action]:
    """The owner's `apps` and `scripts` from hub.json, as registry entries."""
    actions: list[Action] = []

    def app_action(name: str, spec: dict) -> Action | None:
        target = spec.get("target") if isinstance(spec, dict) else None
        if not isinstance(target, str) or not target.strip():
            return None
        raw_args = spec.get("args")
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []

        async def run(_):
            try:
                await asyncio.to_thread(system.start_app, target, args)
            except FileNotFoundError:
                raise ActionError(f"{name} was not found on the laptop (looked for {target}).")
            except OSError as exc:
                raise ActionError(f"{name} could not be started: {safe_text(exc)}")
            return done(f"Opened {name} on the laptop.")

        return Action(f"open_app:{name}", f"Open {name}", "apps", f"Starts {name} on the laptop.", run)

    for name, spec in (config.apps or {}).items():
        action = app_action(str(name), spec)
        if action:
            actions.append(action)

    def script_action(name: str, spec: dict) -> Action | None:
        argv = spec.get("argv") if isinstance(spec, dict) else None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return None

        async def run(_):
            try:
                code, out, err = await asyncio.to_thread(system.run, argv, None, SCRIPT_TIMEOUT, spec.get("cwd"))
            except FileNotFoundError:
                return {"kind": "text", "message": f"Program not found: {argv[0]}",
                        "text": f"Program not found: {argv[0]}", "exit": 127}
            except subprocess.TimeoutExpired:
                return {"kind": "text", "message": f"Stopped after {SCRIPT_TIMEOUT} seconds.",
                        "text": f"Stopped after {SCRIPT_TIMEOUT} seconds.", "exit": 124}
            output = (out + err)[-4000:]
            message = "Finished." if code == 0 else f"Finished with exit code {code}."
            return {"kind": "text", "message": message, "text": output or message, "exit": code}

        return Action(f"script:{name}", str(spec.get("label") or name), "buttons",
                      f"Runs the button '{name}' from the hub settings.", run)

    for name, spec in (config.scripts or {}).items():
        action = script_action(str(name), spec)
        if action:
            actions.append(action)
    return actions


class RunBody(BaseModel):
    action: str = Field(min_length=1, max_length=300)
    params: dict[str, Any] = Field(default_factory=dict)
    confirm: StrictBool = False


def make_router(ctx: Ctx, *, system: System | None = None) -> APIRouter:
    """`system` exists so tests can fake the operating system."""
    system = system or System()
    router = APIRouter(prefix="/api/laptop", dependencies=[Depends(ctx.need_auth)])

    def registry() -> dict[str, Action]:
        """Built fresh each call so edits to apps/scripts in the config show up."""
        entries = builtin_actions(system) + config_actions(ctx.config, system)
        table_: dict[str, Action] = {}
        for action in entries:
            table_.setdefault(action.id, action)
        # `open_app` with a name, for callers that do not list first. Only configured names work.
        apps = {a.id.split(":", 1)[1]: a for a in entries if a.id.startswith("open_app:")}

        async def open_named(params):
            target = apps.get(params["name"])
            if target is None:
                raise BadParams("That app is not on your list. Add it under 'apps' in hub.json.")
            return await target.run({})

        table_["open_app"] = Action(
            "open_app", "Open an app", "apps", "Opens an app from your list.", open_named,
            params=[Param("name", "App", "choice", choices=list(apps))], hidden=True)
        return table_

    @router.get("/actions")
    def list_actions():
        entries = [a for a in registry().values() if not a.hidden]
        used = {a.category for a in entries}
        return {"categories": [{"id": i, "title": t} for i, t in CATEGORIES if i in used],
                "actions": [a.public() for a in entries]}

    @router.post("/run")
    async def run(body: RunBody):
        action = registry().get(body.action)
        if action is None:
            raise HTTPException(404, "There is no such action.")
        ok, reason = action.available()
        if not ok:
            raise HTTPException(501, reason or "This action is not available on this laptop.")
        try:
            params = validate(action.params, body.params)
        except ActionError as exc:
            raise HTTPException(exc.status, str(exc))
        if action.risk == "confirm" and body.confirm is not True:
            raise HTTPException(409, f"'{action.title}' needs your confirmation. Confirm it and send again.")
        ctx.audit("laptop_run", action=action.id, **audit_fields(action, params))
        try:
            result = await action.run(params)
        except ActionError as exc:
            raise HTTPException(exc.status, str(exc))
        except Exception as exc:
            log.exception("laptop action %s failed", action.id)
            raise HTTPException(500, f"'{action.title}' failed on the laptop: {safe_text(exc)}")
        return {"ok": True, **result}

    return router
