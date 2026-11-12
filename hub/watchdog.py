"""`python -m hub watchdog [--once]`: keep the hub and its Tailscale link alive while you are away.

Runs as the scheduled task LaptopHubWatchdog (every 10 minutes, `--once`). One pass checks:
  a) the hub answers GET /api/health on 127.0.0.1 (if not: start the LaptopHub task, wait, check again)
  b) Tailscale is up (Stopped / NoState: `tailscale up`; Logged out: only tell you, it needs a browser)
  c) `tailscale serve` still points at the hub (if not: `tailscale serve --bg PORT`)
Quiet when everything is healthy. After a repair, or a problem it cannot fix, ONE ntfy push goes to your phone
(sent straight to ntfy from the config topic: the hub may be the thing that is down). It never kills a process.
Every effect goes through an injectable function, so tests never touch schtasks, tailscale or the network.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import config as cfg

TASK_NAME = "LaptopHub"
WATCHDOG_TASK_NAME = "LaptopHubWatchdog"
LOG_NAME = "watchdog.log"
STATE_NAME = "watchdog.state.json"
LOG_MAX_BYTES = 1024 * 1024
REPEAT_ALERT_SECONDS = 6 * 3600      # a problem that stays unfixable is repeated at most this often
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
Runner = Callable[[list, float], "tuple[int, str]"]


def real_runner(argv: list, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return 127, "program not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 126, type(exc).__name__
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def real_health(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200 and bool(json.loads(resp.read(4096).decode("utf-8")).get("ok"))
    except Exception:  # noqa: BLE001 - refused, timed out, garbage: all mean "not answering"
        return False


def config_dir(config_path: Path | None) -> Path:
    return Path(config_path).parent if config_path else Path.home() / ".laptop-hub"


def active_topic(config) -> str:
    """Same rule as the hub: a topic chosen in the app (notify_settings.json) wins over hub.json."""
    try:
        data = json.loads((config.data_path() / "notify_settings.json").read_text(encoding="utf-8"))
        topic = str(data.get("ntfy_topic") or "")
        if TOPIC_RE.match(topic):
            return topic
    except (OSError, ValueError, AttributeError):
        pass
    topic = str(getattr(config, "ntfy_topic", "") or "")
    return topic if TOPIC_RE.match(topic) else ""


def push_ntfy(config, title: str, body: str, priority: str = "default", opener=urllib.request.urlopen) -> bool:
    """Send one push straight to ntfy (no hub needed). False if no topic is set or the send failed."""
    topic = active_topic(config)
    server = str(getattr(config, "ntfy_server", "") or "").rstrip("/")
    if not topic or not server.lower().startswith(("http://", "https://")):
        return False
    request = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8"), method="POST",
        headers={"Title": title.encode("ascii", "replace").decode("ascii")[:100] or "REDLINE",
                 "Priority": priority, "Tags": "warning"})
    try:
        with opener(request, timeout=8) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001 - the URL holds the topic: never log it
        return False


def tailscale_state(text: str) -> str:
    """'ok', 'stopped', 'logged_out' or 'unknown' from `tailscale status` output."""
    low = text.lower()
    if "logged out" in low or "needslogin" in low or "log in at" in low:
        return "logged_out"
    if "tailscale is stopped" in low or "nostate" in low or "stopped" in low:
        return "stopped"
    if not low.strip() or "unable to connect" in low or "failed to connect" in low:
        return "unknown"
    return "ok"


class Watchdog:
    def __init__(self, config, config_path: Path | None, *, runner: Runner = real_runner,
                 health: Callable[[str, float], bool] = real_health, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], dt.datetime] = dt.datetime.now,
                 push: Callable[..., bool] | None = None, say: Callable[[str], None] = print):
        self.config, self.runner, self.health, self.sleep, self.now = config, runner, health, sleep, now
        self.push = push or (lambda title, body, priority="default": push_ntfy(config, title, body, priority))
        self.say = say
        base = config_dir(config_path)
        self.log_path, self.state_path = base / LOG_NAME, base / STATE_NAME
        self.url = f"http://127.0.0.1:{int(config.port)}/api/health"

    # -- checks -------------------------------------------------------------------------------------------------
    def hub_up(self) -> bool:
        return bool(self.health(self.url, 5))

    def check_hub(self, repaired: list[str], problems: dict[str, str]) -> str:
        if self.hub_up():
            return "ok"
        rc, _ = self.runner(["schtasks", "/Query", "/TN", TASK_NAME], 30)
        if rc != 0:
            problems["hub_task"] = f"Watchdog: the hub is down and the {TASK_NAME} task is missing - open the laptop"
            return "down(no task)"
        self.runner(["schtasks", "/Run", "/TN", TASK_NAME], 30)
        self.sleep(10)
        if self.hub_up():
            repaired.append("Watchdog: restarted the hub")
            return "restarted"
        problems["hub_down"] = "Watchdog: the hub is down and did not restart - open the laptop"
        return "down"

    def check_tailscale(self, repaired: list[str], problems: dict[str, str]) -> str:
        rc, out = self.runner(["tailscale", "status"], 20)
        if rc == 127:
            return "not installed"
        state = tailscale_state(out)
        if state == "ok" and rc == 0:
            return "ok"
        if state == "logged_out":
            problems["ts_logged_out"] = "Watchdog: Tailscale is logged out - open the laptop"
            return "logged out"
        if state == "unknown" and rc != 0 and not out.strip():
            return "unknown"
        # Stopped / NoState / not answering: ask it to come up (needs no browser when it has logged in before).
        rc, out = self.runner(["tailscale", "up"], 30)
        rc2, out2 = self.runner(["tailscale", "status"], 20)
        after = tailscale_state(out + "\n" + out2)
        if after == "logged_out":
            problems["ts_logged_out"] = "Watchdog: Tailscale is logged out - open the laptop"
            return "logged out"
        if rc2 == 0 and after == "ok":
            repaired.append("Watchdog: started Tailscale again")
            return "restarted"
        problems["ts_down"] = "Watchdog: Tailscale is stopped and would not start - open the laptop"
        return "down"

    def check_serve(self, repaired: list[str], problems: dict[str, str]) -> str:
        port = int(self.config.port)
        rc, out = self.runner(["tailscale", "serve", "status"], 20)
        if rc == 127:
            return "not installed"
        if f"127.0.0.1:{port}" in out:
            return "ok"
        rc, _ = self.runner(["tailscale", "serve", "--bg", str(port)], 30)
        if rc == 0:
            repaired.append("Watchdog: restored the Tailscale link to the hub")
            return "restored"
        problems["serve"] = "Watchdog: could not restore the Tailscale link - open the laptop"
        return "broken"

    # -- one pass -----------------------------------------------------------------------------------------------
    def run_once(self) -> int:
        repaired: list[str] = []
        problems: dict[str, str] = {}
        try:
            hub = self.check_hub(repaired, problems)
            ts = self.check_tailscale(repaired, problems)
            serve = self.check_serve(repaired, problems) if ts in ("ok", "restarted") else "skipped"
        except Exception as exc:  # noqa: BLE001 - a watchdog bug must never become a crash loop
            self.log(f"ERROR unexpected {type(exc).__name__}")
            return 1
        messages = list(repaired) + self._new_problems(problems)
        status = "PROBLEM" if problems else ("repaired" if repaired else "ok")
        self.log(f"{status} hub={hub} tailscale={ts} serve={serve}" + (f" | {'; '.join(repaired + list(problems.values()))}"
                                                                      if repaired or problems else ""))
        if messages:
            try:
                self.push("REDLINE watchdog", "\n".join(messages), "high" if problems else "default")
            except Exception:  # noqa: BLE001
                pass
        return 1 if problems else 0

    def _new_problems(self, problems: dict[str, str]) -> list[str]:
        """Only problems not already announced in the last REPEAT_ALERT_SECONDS; healed ones are forgotten."""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            sent = state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            sent = {}
        now = self.now().timestamp()
        fresh = [m for k, m in problems.items() if now - float(sent.get(k, 0) or 0) >= REPEAT_ALERT_SECONDS]
        new_state = {k: (now if m in fresh else sent.get(k, now)) for k, m in problems.items()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(new_state), encoding="utf-8")
        except OSError:
            pass
        return fresh

    def log(self, text: str) -> None:
        line = f"{self.now().strftime('%Y-%m-%d %H:%M:%S')} {text}"
        self.say(line)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
                self.log_path.replace(self.log_path.with_name(LOG_NAME + ".1"))
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def run(args, **kw) -> int:
    path = Path(args.config) if getattr(args, "config", None) else cfg.default_path()
    try:
        config = cfg.load(path)
    except cfg.ConfigError as exc:
        print(exc)
        return 1
    dog = Watchdog(config, path, **kw)
    if getattr(args, "once", False):
        return dog.run_once()
    print("Watchdog running every 10 minutes. Ctrl+C to stop. (The scheduled task uses --once.)")
    try:
        while True:
            dog.run_once()
            time.sleep(600)
    except KeyboardInterrupt:
        return 0
