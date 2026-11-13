"""`python -m hub setup`: one interactive command that sets everything up on THIS laptop.

Secrets (Gmail app password, Asana token, calendar link, Google Chat webhooks, Gemini key) are read with
getpass and saved only where the existing code already looks for them: the OS keyring (services
jarvis-gmail / jarvis-calendar / jarvis-gchat / jarvis-gemini) and ~/.claude/.asana_token. No Claude API key is
asked for: the Claude login of the `claude` program is used instead. They are never printed, never written to hub.json, and never sent anywhere except to the
service they belong to when testing the login. Every question goes through ask()/ask_secret()/confirm() and every
effect on the machine through a `System` object, so tests never touch the real laptop.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config as cfg

GMAIL_SERVICE = "jarvis-gmail"        # mailer.py and hub/inbox.py
CALENDAR_SERVICE = "jarvis-calendar"  # agenda.py (key "ics_url")
GEMINI_SERVICE = "jarvis-gemini"      # hub/llm.py gemini_key() (key "api_key")
GEMINI_CHECK_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
CHAT_SERVICE = "jarvis-gchat"         # gchat.py (key "_spaces" plus one entry per space)
ASANA_TOKEN_FILE = ".claude/.asana_token"   # first file hub/work/asana.py reads
CLONE_URL = "https://github.com/ShikharGupta0813/jarvis-voice-assistant"
COUNCIL_CLONE_URL = "https://github.com/ShikharGupta0813/multi-llm-lab"
WEBHOOK_PREFIX = "https://chat.googleapis.com/v1/spaces/"
WARP_PATH = r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"
TASK_NAME = "LaptopHub"
UPDATE_TASK_NAME = "LaptopHubUpdate"
BACKUP_TASK_NAME = "LaptopHubBackup"
WATCHDOG_TASK_NAME = "LaptopHubWatchdog"
EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)
STEP_IDS = ("jarvis", "gmail", "asana", "calendar", "chat", "ai", "council", "autopilot", "phone", "name", "warp", "autostart", "updater", "backup", "watchdog")
# `gemini` is part of `ai`; it can also be run alone: python -m hub setup --only gemini
ONLY_ORDER = ("jarvis", "gmail", "asana", "calendar", "chat", "ai", "gemini", "council", "autopilot", "phone", "name", "warp", "autostart", "updater", "backup", "watchdog")
ALIASES = {"ntfy": "phone", "push": "phone", "email": "gmail", "mail": "gmail", "cal": "calendar",
           "claude": "ai", "claude-key": "ai", "key": "ai", "claude-login": "ai", "startup": "autostart",
           "auto-start": "autostart", "approvals": "autopilot", "drafts": "autopilot", "lab": "council", "multi-llm-lab": "council", "update": "updater", "update-button": "updater", "backups": "backup", "guard": "watchdog"}
TITLES = {"jarvis": "Jarvis folder", "gmail": "Gmail", "asana": "Asana", "calendar": "Calendar",
          "chat": "Google Chat", "ai": "AI", "gemini": "Gemini key", "council": "Council (multi-llm-lab)", "autopilot": "Autopilot", "phone": "Phone push", "name": "Your name",
          "warp": "Cloudflare WARP", "autostart": "Auto-start", "updater": "Update button", "backup": "Weekly backup", "watchdog": "Watchdog"}

TOKENISH = re.compile(r"https?://\S+|sk-ant-\S+|[A-Za-z0-9_\-]{24,}")


def scrub(text: Any, secrets_: tuple[str, ...] | list[str] = ()) -> str:
    """Text safe to print: known secrets and anything token-like or URL-like is replaced."""
    out = str(text)
    for secret in secrets_:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    return TOKENISH.sub("[hidden]", out)


class CheckFailed(Exception):
    """Raised by System tests. `kind` is auth, network or other; the message is never shown raw."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass
class Result:
    status: str   # ok | warn | fail | skip
    message: str


# ---------------------------------------------------------------------------------------------------------
# The real machine
# ---------------------------------------------------------------------------------------------------------

class System:
    """Everything that touches the real laptop. Tests replace this whole object."""

    platform = sys.platform

    def isatty(self) -> bool:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout.isatty())

    def home(self) -> Path:
        return Path.home()

    def env(self, name: str) -> str:
        return os.environ.get(name, "")

    def username(self) -> str:
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""

    def python_dir(self) -> Path:
        return Path(sys.executable).parent

    def exists(self, path: str | Path) -> bool:
        return Path(path).exists()

    def isdir(self, path: str | Path) -> bool:
        return Path(path).is_dir()

    def isfile(self, path: str | Path) -> bool:
        return Path(path).is_file()

    def listdir(self, path: str | Path) -> list[str]:
        return os.listdir(path)

    def makedirs(self, path: str | Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def read_text(self, path: str | Path) -> str:
        return Path(path).read_text(encoding="utf-8")

    def run(self, argv: list[str], cwd: str | None = None, timeout: float = 900,
            on_line: Callable[[str], None] | None = None) -> tuple[int, str]:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace")
        except OSError:
            return 127, "program not found"
        lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                lines.append(line)
                if on_line and line:
                    on_line(line)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return 124, "timed out"
        return proc.returncode, "\n".join(lines[-20:])

    # -- keyring -------------------------------------------------------------------------------------------
    def keyring_get(self, service: str, key: str) -> str | None:
        import keyring
        return keyring.get_password(service, key)

    def keyring_set(self, service: str, key: str, value: str) -> None:
        import keyring
        keyring.set_password(service, key, value)

    # -- network tests (nothing is sent but the login itself) -----------------------------------------------
    def imap_check(self, address: str, password: str) -> None:
        import imaplib
        try:
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=10)
        except (OSError, socket.timeout):
            raise CheckFailed("network") from None
        try:
            conn.login(address, password)
            conn.select("INBOX", readonly=True)
        except imaplib.IMAP4.error:
            raise CheckFailed("auth") from None
        except (OSError, socket.timeout):
            raise CheckFailed("network") from None
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def smtp_check(self, address: str, password: str) -> None:
        import smtplib
        import ssl
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=10) as smtp:
                smtp.login(address, password)
        except smtplib.SMTPAuthenticationError:
            raise CheckFailed("auth") from None
        except (OSError, smtplib.SMTPException):
            raise CheckFailed("network") from None

    def http_get(self, url: str, headers: dict | None = None, follow_redirects: bool = True,
                 timeout: float = 10) -> tuple[int, bytes]:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        handlers = [] if follow_redirects else [NoRedirect]
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with opener.open(request, timeout=timeout) as resp:
                return resp.status, resp.read(5_000_000)
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except (urllib.error.URLError, OSError, ValueError):
            raise CheckFailed("network") from None

    def http_post_json(self, url: str, body: dict, headers: dict | None = None, timeout: float = 20) -> tuple[int, bytes]:
        """POST JSON; unlike http_get this keeps the body of an error answer (Google explains quota errors there)."""
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"content-type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, resp.read(1_000_000)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(100_000)
            except OSError:
                return exc.code, b""
        except (urllib.error.URLError, OSError, ValueError):
            raise CheckFailed("network") from None

    def count_events_today(self, ics: bytes) -> int:
        try:
            import icalendar
            import recurring_ical_events
            cal = icalendar.Calendar.from_ical(ics)
            today = dt.date.today()
            return len(recurring_ical_events.of(cal).between(today, today + dt.timedelta(days=1)))
        except ImportError:
            return len(re.findall(rb"^BEGIN:VEVENT", ics, re.M))   # coarse: events in the feed
        except Exception:  # noqa: BLE001
            raise CheckFailed("other") from None

    # -- files and settings that hold secrets ----------------------------------------------------------------
    def write_private_file(self, path: str | Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if sys.platform == "win32":
            user = self.username()
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                               capture_output=True, timeout=30, check=False)
        else:
            os.chmod(path, 0o600)

    def set_user_env(self, name: str, value: str) -> bool:
        """Persist for new shells. Uses the registry directly so the value is never on a command line."""
        if sys.platform != "win32":
            return False
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        return True


# ---------------------------------------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------------------------------------

class Wizard:
    def __init__(self, config_path: Path, system: System | None = None, *,
                 ask: Callable[[str, str], str] | None = None,
                 ask_secret: Callable[[str], str] | None = None,
                 confirm: Callable[[str, bool], bool] | None = None,
                 out: Callable[[str], None] | None = None, color: bool | None = None):
        self.system = system or System()
        self.config_path = Path(config_path)
        self._ask = ask or _real_ask
        self._ask_secret = ask_secret or _real_ask_secret
        self._confirm = confirm or _real_confirm
        self._out = out or print
        self.color = (self.system.isatty() and not os.environ.get("NO_COLOR")) if color is None else color
        self.config: cfg.Config | None = None
        self.known_secrets: list[str] = []
        self.results: list[tuple[str, Result]] = []

    # -- io (every printed line is scrubbed of anything entered as a secret) -------------------------------------
    def say(self, text: str = "") -> None:
        self._out(scrub_known(str(text), self.known_secrets))

    def ask(self, prompt: str, default: str = "") -> str:
        return (self._ask(prompt, default) or "").strip() or default

    def secret(self, prompt: str) -> str:
        value = (self._ask_secret(prompt) or "").strip()
        if value:
            self.known_secrets.append(value)
        return value

    def confirm(self, prompt: str, default: bool = True) -> bool:
        return bool(self._confirm(prompt, default))

    def ask_path(self, prompt: str, default: str) -> str | None:
        """Ask for a folder. Rejects a lone y/n (an answer typed at the wrong question); None after 3 bad tries."""
        for _ in range(3):
            raw = self.ask(prompt, default)
            cleaned = clean_path(raw)
            if cleaned.lower() in YES_NO_WORDS:
                self.say(f"That looks like an answer, not a folder. Type a folder path or press Enter for {default}")
                continue
            return cleaned or default
        self.say("No usable folder after 3 tries; skipping this step.")
        return None

    def tone(self, status: str, text: str) -> str:
        label = {"ok": "OK ", "warn": "!! ", "fail": "XX ", "skip": "-- "}[status]
        if not self.color:
            return f"[{label.strip():<4}] {text}"
        code = {"ok": "32", "warn": "33", "fail": "31", "skip": "90"}[status]
        return f"\033[{code}m[{label.strip():<4}]\033[0m {text}"

    def report(self, step: str, result: Result) -> Result:
        self.results.append((step, result))
        self.say(self.tone(result.status, f"{TITLES[step]}: {result.message}"))
        return result

    # -- config ----------------------------------------------------------------------------------------------
    def load_config(self, create: bool) -> cfg.Config | None:
        if self.config is not None:
            return self.config
        try:
            self.config = cfg.load(self.config_path)
        except cfg.ConfigError:
            if not create:
                return None
            self.config, token = cfg.make()
            for folder in (self.config.notes_dir, self.config.inbox_dir):
                self.system.makedirs(folder)
            cfg.save(self.config, self.config_path)
            self.say(f"Created {self.config_path}. Your hub login token (shown once, copy it now):")
            self.say(f"    {token}")
        return self.config

    def save_config(self) -> None:
        if self.config is not None:
            cfg.save(self.config, self.config_path)

    def keyring_ok(self) -> bool:
        try:
            self.system.keyring_get(GMAIL_SERVICE, "address")
            return True
        except Exception:  # noqa: BLE001
            return False


def scrub_known(text: str, known: list[str]) -> str:
    for secret in known:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


YES_NO_WORDS = {"y", "yes", "n", "no"}


def clean_path(text: str) -> str:
    """Trim spaces and one pair of matching quotes (Windows 'Copy as path' adds them)."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def parse_yes_no(answer: str) -> bool | None:
    answer = (answer or "").strip().lower()
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    return None


def _real_ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ")


def _real_ask_secret(prompt: str) -> str:
    import getpass
    return getpass.getpass(f"{prompt} (hidden, Enter to skip): ")


def _real_confirm(prompt: str, default: bool = True, reader: Callable[[str], str] = input,
                  say: Callable[[str], None] = print) -> bool:
    """y/yes/n/no/Enter only. Anything else is re-asked; after 3 bad answers it counts as no."""
    for _ in range(3):
        answer = reader(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip()
        if not answer:
            return default
        value = parse_yes_no(answer)
        if value is not None:
            return value
        say("Please answer y or n (Enter = yes)" if default else "Please answer y or n (Enter = no)")
    say("No clear answer after 3 tries; treating that as no.")
    return False


def _fail_text(kind: str, what: str) -> str:
    return {"auth": f"{what} rejected the login.", "network": f"couldn't reach {what} (network or timeout).",
            }.get(kind, f"{what} test failed.")


# ---------------------------------------------------------------------------------------------------------
# Steps. Each returns a Result; check_* functions test without asking or changing anything.
# ---------------------------------------------------------------------------------------------------------

def default_jarvis_dir(w: Wizard) -> str:
    if w.system.platform == "win32" and w.system.isdir("D:\\"):
        return "D:\\jarvis-voice-assistant"
    return str(w.system.home() / "jarvis-voice-assistant")


def check_jarvis(w: Wizard) -> Result:
    config = w.load_config(False)
    if config is None or not config.jarvis_dir:
        return Result("warn", "no Jarvis folder set in hub.json")
    helpers = [h for h in ("agenda", "mailer", "gchat") if not w.system.isfile(Path(config.jarvis_dir) / f"{h}.py")]
    if helpers:
        return Result("fail", f"{config.jarvis_dir} is missing {', '.join(h + '.py' for h in helpers)}")
    return Result("ok", f"helpers found in {config.jarvis_dir}")


def step_jarvis(w: Wizard) -> Result:
    config = w.load_config(True)
    assert config is not None
    folder = w.ask_path("Where is (or should I put) your Jarvis folder?", config.jarvis_dir or default_jarvis_dir(w))
    if folder is None:
        return Result("skip", "no Jarvis folder chosen; email, calendar and chat stay off")
    path = Path(os.path.abspath(os.path.expanduser(folder)))
    w.say(f"Folder: {path}")
    s = w.system
    if s.isdir(path) and s.listdir(path):
        if not s.isfile(path / "jarvis.py"):
            return Result("fail", f"{path} exists but has no jarvis.py. I won't touch it; pick another folder.")
        w.say(f"Found {path}; leaving its files alone.")
    elif s.exists(path) and not s.isdir(path):
        return Result("fail", f"{path} is a file, not a folder.")
    else:
        if not w.confirm(f"{path} isn't there. Download Jarvis from GitHub into it?", True):
            return Result("skip", "no Jarvis folder; email, calendar and chat stay off")
        if not s.which("git"):
            return Result("fail", "git isn't installed (winget install --id Git.Git -e), then run setup again")
        w.say("If git asks you to sign in to GitHub, a browser window or a prompt will appear. Finish it there.")
        rc, _ = s.run(["git", "clone", CLONE_URL, str(path)], on_line=lambda l: w.say("   " + l[:100]))
        if rc != 0:
            return Result("fail", "git clone failed (not signed in, or no network). Run setup again after fixing")
    msg = f"hub will use {path}"
    if s.isfile(path / "requirements.txt") and not s.exists(path / ".venv"):
        w.say("Jarvis's own virtual environment (.venv) is only needed to run Jarvis the voice assistant itself.")
        w.say("The hub does NOT need it: it only loads agenda.py, mailer.py and gchat.py, using the hub's Python "
              "(pip install -r requirements-work.txt).")
        if w.confirm("Create Jarvis's .venv and install its requirements anyway? (takes a few minutes)", False):
            rc, _ = s.run([sys.executable, "-m", "venv", str(path / ".venv")], on_line=lambda l: w.say("   " + l[:100]))
            py = path / ".venv" / ("Scripts" if s.platform == "win32" else "bin") / ("python.exe" if s.platform == "win32" else "python")
            if rc == 0:
                rc, _ = s.run([str(py), "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(path),
                              on_line=lambda l: w.say("   " + l[:100]))
            msg += "; Jarvis venv ready" if rc == 0 else "; Jarvis venv install failed (the hub doesn't need it)"
    config.jarvis_dir = str(path)
    w.save_config()
    return Result("ok", msg)


def default_council_dir(w: Wizard) -> str:
    if w.system.platform == "win32" and w.system.isdir("D:\\"):
        return "D:\\multi-llm-lab"
    return str(w.system.home() / "multi-llm-lab")


def check_council(w: Wizard) -> Result:
    config = w.load_config(False)
    if config is None or not config.council_dir:
        return Result("warn", "no council folder set in hub.json (optional: side jobs that use several models)")
    if not w.system.isdir(Path(config.council_dir) / "lab"):
        return Result("fail", f"{config.council_dir} has no 'lab' folder (is it the multi-llm-lab checkout?)")
    venv = Path(config.council_dir) / ".venv"
    note = "" if (config.council_python or w.system.exists(venv)) else " (no .venv yet; the hub's Python is used)"
    return Result("ok", f"multi-llm-lab found in {config.council_dir}{note}")


def step_council(w: Wizard) -> Result:
    config = w.load_config(True)
    assert config is not None
    w.say("Council runs your multi-llm-lab project (local and free hosted models working together) as a "
          "background job from the phone. It never uses Claude unless you turn that on in hub.json.")
    folder = w.ask_path("Where is (or should I put) your multi-llm-lab folder?",
                        config.council_dir or default_council_dir(w))
    if folder is None:
        return Result("skip", "no council folder chosen; the Council stays off")
    path = Path(os.path.abspath(os.path.expanduser(folder)))
    w.say(f"Folder: {path}")
    s = w.system
    if s.isdir(path) and s.listdir(path):
        if not s.isdir(path / "lab"):
            return Result("fail", f"{path} exists but has no 'lab' folder. I won't touch it; pick another folder.")
        w.say(f"Found {path}; leaving its files alone.")
    elif s.exists(path) and not s.isdir(path):
        return Result("fail", f"{path} is a file, not a folder.")
    else:
        if not w.confirm(f"{path} isn't there. Download multi-llm-lab from GitHub into it?", True):
            return Result("skip", "no council folder; the Council stays off")
        if not s.which("git"):
            return Result("fail", "git isn't installed (winget install --id Git.Git -e), then run setup again")
        w.say("If git asks you to sign in to GitHub, a browser window or a prompt will appear. Finish it there.")
        rc, _ = s.run(["git", "clone", COUNCIL_CLONE_URL, str(path)], on_line=lambda l: w.say("   " + l[:100]))
        if rc != 0:
            return Result("fail", "git clone failed (not signed in, or no network). Run setup again after fixing")
    msg = f"hub will use {path}"
    windows = s.platform == "win32"
    py = path / ".venv" / ("Scripts" if windows else "bin") / ("python.exe" if windows else "python")
    if s.isfile(path / "requirements.txt") and not s.exists(path / ".venv"):
        w.say("The council needs its own Python packages. They go into a virtual environment inside the folder "
              "(.venv), so nothing else on your laptop changes.")
        if w.confirm("Create the .venv and install its requirements now? (takes a few minutes)", True):
            rc, _ = s.run([sys.executable, "-m", "venv", str(path / ".venv")], on_line=lambda l: w.say("   " + l[:100]))
            if rc == 0:
                rc, _ = s.run([str(py), "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(path),
                              on_line=lambda l: w.say("   " + l[:100]))
            msg += "; venv ready" if rc == 0 else "; venv install failed (run it again, or set council_python in hub.json)"
        else:
            msg += "; no venv (the hub's Python is used)"
    config.council_dir = str(path)
    w.save_config()
    return Result("ok", msg + ". Restart the hub; the Council appears under Apps")


def split_paths(text: str) -> list[str]:
    """Folder paths typed on one line, separated by ; or , (a Windows path has neither), quotes dropped."""
    out: list[str] = []
    for part in re.split(r"[;,\n]", text or ""):
        cleaned = clean_path(part)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def check_autopilot(w: Wizard) -> Result:
    config = w.load_config(False)
    if config is None:
        return Result("warn", "no hub.json yet")
    if not config.autopilot_enabled:
        return Result("warn", "off (optional: drafts your mail, chat and Asana replies for one-tap approval)")
    missing = [r for r in config.autopilot_repos if not w.system.isdir(r)]
    if missing:
        return Result("fail", f"on, but a standup folder is missing: {missing[0]}")
    return Result("ok", f"on, every {config.autopilot_sweep_min} min, {config.autopilot_daily_ai_calls} AI calls a day max, "
                        f"{len(config.autopilot_repos)} standup folder(s), evening summary {config.today_evening_time or 'off'}")


def step_autopilot(w: Wizard) -> Result:
    config = w.load_config(True)
    assert config is not None
    w.say("Autopilot watches your mail, chat alerts and Asana, writes draft replies with a free AI (never Claude), "
          "and lines them up as Approve / Edit / Skip cards on your phone. Nothing is sent until you tap.")
    if not w.confirm("Turn Autopilot on?", bool(config.autopilot_enabled)):
        config.autopilot_enabled = False
        w.save_config()
        return Result("skip", "Autopilot stays off (turn it on later in Settings or run this step again)")
    config.autopilot_enabled = True
    w.say("For the morning standup draft, give the folders of your git projects (full paths, separated by ;). "
          "Press Enter to skip.")
    current = "; ".join(config.autopilot_repos)
    typed = w.ask("Git folders for the standup (optional)", current)
    repos: list[str] = []
    problems: list[str] = []
    for path in split_paths(typed):
        (repos if w.system.isdir(path) else problems).append(path)
    for path in problems:
        w.say(f"   not a folder, left out: {path}")
    config.autopilot_repos = repos
    tone = clean_tone(w.ask("How should your drafts sound? One line (Enter keeps the current one)", config.autopilot_tone))
    config.autopilot_tone = tone or config.autopilot_tone
    w.save_config()
    note = f"{len(repos)} standup folder(s)" + (f", {len(problems)} not found" if problems else "")
    return Result("ok", f"Autopilot is on ({note}). Restart the hub; the cards appear under Apps > Autopilot")


def clean_tone(text: str) -> str:
    """One printable line of at most 200 characters; angle brackets and quotes dropped."""
    text = "".join(ch for ch in (text or "") if ch.isprintable() and ch not in "<>\"`")
    return " ".join(text.split())[:200]


def check_gmail(w: Wizard) -> Result:
    try:
        address = w.system.keyring_get(GMAIL_SERVICE, "address")
        password = w.system.keyring_get(GMAIL_SERVICE, address) if address else None
    except Exception:  # noqa: BLE001
        return Result("fail", "can't read the keyring (pip install keyring)")
    if not address or not password:
        return Result("warn", "not set up")
    w.known_secrets.append(password)
    return test_gmail(w, address, password, saved=True)


def test_gmail(w: Wizard, address: str, password: str, saved: bool = False) -> Result:
    problems = []
    for name, fn in (("IMAP (reading)", w.system.imap_check), ("SMTP (sending)", w.system.smtp_check)):
        try:
            fn(address, password)
        except CheckFailed as exc:
            problems.append(_fail_text(exc.kind, name))
        except Exception:  # noqa: BLE001
            problems.append(_fail_text("other", name))
    if problems:
        return Result("fail", f"{address}: " + " ".join(problems))
    return Result("ok", f"{address}: reading and sending logins work" + ("" if saved else " and the password is saved"))


WORKSPACE_NOTE = (
    "Company Google Workspace accounts (like @onestack.in) often have App passwords or IMAP turned off by the admin.",
    "If that is the case here, the login test below will be rejected and only your admin can change it.",
    "A personal Gmail account works without any admin help.",
)


def split_emails(text: str) -> list[str]:
    """Addresses separated by commas, semicolons or spaces; order kept, duplicates dropped."""
    found: list[str] = []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip().strip("<>\"'")
        if part and part.lower() not in [f.lower() for f in found]:
            found.append(part)
    return found


def step_gmail(w: Wizard) -> Result:
    s = w.system
    try:
        current = s.keyring_get(GMAIL_SERVICE, "address")
    except Exception:  # noqa: BLE001
        return Result("fail", "the OS keyring isn't available. Run: pip install -r requirements-work.txt")
    if current and not w.confirm(f"Gmail is already saved for {current}. Replace it?", False):
        return check_gmail(w)
    for line in WORKSPACE_NOTE:
        w.say(line)
    entered = w.ask("Your Gmail / Google Workspace address (Enter to skip)")
    if not entered:
        return Result("skip", "skipped")
    candidates = split_emails(entered)
    bad = [c for c in candidates if not EMAIL_RE.match(c)]
    if bad or not candidates:
        return Result("fail", "that doesn't look like an email address (one per entry, like name@gmail.com); nothing saved")
    address = candidates[0]
    if len(candidates) > 1:
        w.say("I found these addresses:")
        for i, c in enumerate(candidates, 1):
            w.say(f"  {i}. {c}")
        w.say("Only ONE mailbox can be connected at a time. To connect the other one later, run: "
              "python -m hub setup --only gmail")
        address = ""
        for _ in range(3):
            pick = w.ask("Which one should REDLINE read? (number)")
            if pick.isdigit() and 1 <= int(pick) <= len(candidates):
                address = candidates[int(pick) - 1]
                break
            if not pick:
                return Result("skip", "skipped")
            w.say(f"Type a number from 1 to {len(candidates)}, or press Enter to skip.")
        if not address:
            return Result("skip", "no mailbox chosen")
        w.say(f"Using {address}")
    w.say("Gmail needs an App password, not your normal password:")
    w.say("  1. Turn on 2-Step Verification for the Google account.")
    w.say("  2. Open https://myaccount.google.com/apppasswords and create one named Jarvis.")
    w.say("  3. Paste the 16 letters here (hidden; right-click pastes in this window).")
    password = w.secret("App password").replace(" ", "")
    if not password:
        return Result("skip", "skipped")
    if len(password) != 16 or not password.isalpha():
        return Result("fail", f"received {len(password)} characters; app passwords are exactly 16 letters. Nothing saved")
    result = test_gmail(w, address, password)
    if result.status == "fail" and "rejected" in result.message:
        w.say("Why this can happen:")
        for line in WORKSPACE_NOTE:
            w.say("  " + line)
        return result
    if result.status == "fail" and not w.confirm("The test failed (probably the network). Save it anyway?", False):
        return result
    try:
        s.keyring_set(GMAIL_SERVICE, "address", address)
        s.keyring_set(GMAIL_SERVICE, address, password)
        back = s.keyring_get(GMAIL_SERVICE, address) == password and s.keyring_get(GMAIL_SERVICE, "address") == address
    except Exception:  # noqa: BLE001
        return Result("fail", "couldn't save to the keyring")
    if not back:
        return Result("fail", "saved, but reading it back from the keyring did not match")
    return result if result.status == "ok" else Result("warn", f"saved for {address}, but {result.message}")


def asana_token_path(w: Wizard) -> Path:
    return w.system.home() / ASANA_TOKEN_FILE


def _asana_user(w: Wizard, token: str) -> Result:
    try:
        status, body = w.system.http_get("https://app.asana.com/api/1.0/users/me",
                                         {"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                         follow_redirects=False)
    except CheckFailed as exc:
        return Result("fail", _fail_text(exc.kind, "Asana"))
    if status in (401, 403):
        return Result("fail", "Asana rejected the token")
    if status != 200:
        return Result("fail", f"Asana answered with status {status}")
    try:
        name = json.loads(body)["data"]["name"]
    except (ValueError, KeyError, TypeError):
        return Result("fail", "Asana's answer wasn't understood")
    return Result("ok", f"connected as {scrub(name)[:60]}")


def _current_asana_token(w: Wizard) -> str:
    from .work.asana import find_token
    env = {"ASANA_TOKEN": w.system.env("ASANA_TOKEN")}
    return find_token(env, w.system.home()) or ""


def check_asana(w: Wizard) -> Result:
    token = _current_asana_token(w)
    if not token:
        return Result("warn", "not set up")
    w.known_secrets.append(token)
    return _asana_user(w, token)


def step_asana(w: Wizard) -> Result:
    if _current_asana_token(w) and not w.confirm("An Asana token is already saved. Replace it?", False):
        return check_asana(w)
    w.say("Create a Personal Access Token at https://app.asana.com/0/my-apps and paste it (hidden).")
    token = w.secret("Asana token")
    if not token:
        return Result("skip", "skipped")
    result = _asana_user(w, token)
    if result.status == "fail":
        return Result("fail", result.message + "; nothing saved")
    try:
        w.system.write_private_file(asana_token_path(w), token + "\n")
    except OSError:
        return Result("fail", "couldn't write the token file")
    return Result("ok", result.message + f" (token saved in {asana_token_path(w)}, readable only by you)")


def _calendar_check(w: Wizard, url: str) -> Result:
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if not url.lower().startswith("https://"):
        return Result("fail", "the calendar link must start with https:// (or webcal://)")
    try:
        status, body = w.system.http_get(url)
        if status in (403, 404):
            return Result("fail", f"Google answered {status}. The secret address may be mistyped (copy the whole link "
                          "ending in .ics), reset, or your company admin has disabled it for @onestack.in. A personal "
                          "calendar link works without admin help; or skip - Friday still works without a calendar.")
        if status != 200:
            return Result("fail", f"the calendar link answered with status {status}")
        count = w.system.count_events_today(body)
    except CheckFailed as exc:
        return Result("fail", _fail_text(exc.kind, "the calendar link"))
    return Result("ok", f"{count} event{'s' if count != 1 else ''} today")


def check_calendar(w: Wizard) -> Result:
    try:
        url = w.system.keyring_get(CALENDAR_SERVICE, "ics_url")
    except Exception:  # noqa: BLE001
        return Result("fail", "can't read the keyring (pip install keyring)")
    if not url:
        return Result("warn", "not set up")
    w.known_secrets.append(url)
    return _calendar_check(w, url)


def step_calendar(w: Wizard) -> Result:
    try:
        current = w.system.keyring_get(CALENDAR_SERVICE, "ics_url")
    except Exception:  # noqa: BLE001
        return Result("fail", "the OS keyring isn't available. Run: pip install -r requirements-work.txt")
    if current and not w.confirm("A calendar link is already saved. Replace it?", False):
        return check_calendar(w)
    w.say("Google Calendar: Settings, click your calendar, Integrate calendar, copy 'Secret address in iCal format'.")
    w.say("Anyone with that link can read your calendar; it stays on this laptop.")
    url = w.secret("Calendar link")
    if not url:
        return Result("skip", "skipped")
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    result = _calendar_check(w, url)
    if result.status == "fail":
        return Result("fail", result.message.rstrip(".") + ". Nothing saved.")
    try:
        w.system.keyring_set(CALENDAR_SERVICE, "ics_url", url)
        if w.system.keyring_get(CALENDAR_SERVICE, "ics_url") != url:
            return Result("fail", "saved, but reading it back did not match")
    except Exception:  # noqa: BLE001
        return Result("fail", "couldn't save to the keyring")
    return Result("ok", result.message + " (link saved)")


def _chat_spaces(w: Wizard) -> list[str]:
    return json.loads(w.system.keyring_get(CHAT_SERVICE, "_spaces") or "[]")


def check_chat(w: Wizard) -> Result:
    try:
        names = _chat_spaces(w)
    except Exception:  # noqa: BLE001
        return Result("fail", "can't read the keyring (pip install keyring)")
    return Result("ok", f"{len(names)} space{'s' if len(names) != 1 else ''}: {', '.join(names)}") if names else Result("warn", "no spaces added")


CHAT_NOTE = (
    "Google Chat webhooks can only POST messages into a Space, not into 1:1 chats.",
    "They must be allowed by your Workspace admin. If 'Apps & integrations' or 'Webhooks' is missing in the space "
    "menu, the admin has turned them off: ask them, or skip.",
    "REDLINE cannot read Chat through webhooks. Reading messages works through the Android app's notification "
    "capture instead (Settings > Phone capture > Watch chat).",
    "To make one: open the space, click its name, Apps & integrations, Webhooks, add one, copy the URL.",
)


def step_chat(w: Wizard) -> Result:
    try:
        names = _chat_spaces(w)
    except Exception:  # noqa: BLE001
        return Result("fail", "the OS keyring isn't available. Run: pip install -r requirements-work.txt")
    if names:
        w.say(f"Spaces already saved: {', '.join(names)}")
    for line in CHAT_NOTE:
        w.say(line)
    added = 0
    while True:
        name = w.ask("Space name to add (Enter to finish)").lower()
        if not name:
            break
        if not re.fullmatch(r"[a-z0-9 _-]{1,40}", name) or name == "_spaces":
            w.say(w.tone("fail", "use letters, digits, spaces, - or _ for the name"))
            continue
        url = w.secret(f"Webhook URL for '{name}'")
        if not url:
            continue
        if not url.startswith(WEBHOOK_PREFIX):
            w.say(w.tone("fail", f"that isn't a Google Chat webhook (it must start with {WEBHOOK_PREFIX}); not saved"))
            continue
        try:
            current = [n for n in _chat_spaces(w) if n != name] + [name]
            w.system.keyring_set(CHAT_SERVICE, name, url)
            w.system.keyring_set(CHAT_SERVICE, "_spaces", json.dumps(current))
            if w.system.keyring_get(CHAT_SERVICE, name) != url:
                raise RuntimeError
        except Exception:  # noqa: BLE001
            return Result("fail", "couldn't save to the keyring")
        added += 1
        w.say(w.tone("ok", f"saved space '{name}'"))
    if not added and not names:
        return Result("skip", "skipped")
    return Result("ok", f"{added} added; spaces: {', '.join(_chat_spaces(w))}")


def _claude_cli(w: Wizard) -> tuple[str | None, str]:
    """(path, version text) of the `claude` program, or (None, "")."""
    path = w.system.which("claude") or w.system.which("claude.cmd")
    if not path:
        return None, ""
    rc, out = w.system.run([path, "--version"], timeout=30)
    if rc != 0:
        return None, ""
    lines = (out or "").strip().splitlines()
    return path, (lines[0][:60] if lines else "")


def _gemini_saved(w: Wizard) -> bool:
    if w.system.env("GEMINI_API_KEY"):
        return True
    try:
        return bool(w.system.keyring_get(GEMINI_SERVICE, "api_key"))
    except Exception:  # noqa: BLE001
        return False


def check_ai(w: Wizard) -> Result:
    cli, _ = _claude_cli(w)
    ready = ["local model"]
    if w.system.env("ANTHROPIC_API_KEY"):
        ready.append("Claude API key")
    if cli:
        ready.append("Claude login (claude program found)")
    if _gemini_saved(w):
        from . import llm
        ready.append("Gemini key" + (" (no free quota on it, limit 0)" if llm.gemini_quota_problem() else ""))
    config = w.load_config(False)
    default = config.default_provider if config else "auto"
    status = "ok" if len(ready) > 1 else "warn"
    return Result(status, "ready: " + ", ".join(ready) + f"; default: {default}")


def check_gemini(w: Wizard) -> Result:
    from . import llm
    if not _gemini_saved(w):
        return Result("warn", "no Gemini key saved (free one: aistudio.google.com/apikey)")
    if llm.gemini_quota_problem():
        return Result("warn", "a Gemini key is saved, but " + llm.GEMINI_NO_QUOTA_REASON.split("\u2014", 1)[1].strip())
    return Result("ok", "a Gemini key is saved")


def gemini_probe(w: Wizard, key: str) -> tuple[str, str]:
    """One tiny live generate call (1 output token). ("ok" | "no_quota" | "not_found" | "error", model tried).
    A model with no quota or no such model moves on to the next ranked candidate, like the hub does."""
    from . import llm
    config = w.load_config(False)
    configured = (config.gemini_model if config else "") or "gemini-2.5-flash"
    names = [configured]
    try:
        code, raw = w.system.http_get(GEMINI_CHECK_URL.replace("pageSize=1", "pageSize=200"), headers={"x-goog-api-key": key})
        if code < 400:
            names += [n for n in llm.rank_gemini_models(json.loads(raw).get("models") or [], configured)
                      if n != configured][:3]
    except (CheckFailed, ValueError):
        pass
    saw = ""
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 1}}
    for name in names:
        try:
            code, raw = w.system.http_post_json(f"{llm.GEMINI_BASE}/models/{name}:generateContent", body,
                                                headers={"x-goog-api-key": key})
        except CheckFailed:
            return "error", name
        if code < 400:
            return "ok", name
        if code == 404:
            saw = saw or "not_found"
        elif llm.gemini_no_quota(code, raw.decode("utf-8", "replace")):
            saw = "no_quota"
        else:
            return "error", name
    if saw == "no_quota":
        llm.note_gemini_quota_failure(key)
    return saw or "error", names[0]


def step_gemini(w: Wizard) -> Result:
    if _gemini_saved(w) and not w.confirm("A Gemini key is already saved. Replace it?", False):
        return check_gemini(w)
    w.say("Gemini has a free tier, separate from your Claude limits. Get a key at https://aistudio.google.com/apikey "
          "(sign in with Google, press Create API key). It is stored in your Windows keyring, never in hub.json.")
    key = w.secret("Gemini API key")
    if not key:
        return Result("skip", "skipped")
    try:
        code, _ = w.system.http_get(GEMINI_CHECK_URL, headers={"x-goog-api-key": key})
    except CheckFailed:
        if not w.confirm("Couldn't reach Google to test the key. Save it anyway?", True):
            return Result("skip", "not saved")
        code = 200
    if code in (400, 401, 403):
        return Result("warn", "Google rejected that key, so it was not saved. Make a new one and run: python -m hub setup --only gemini")
    if code >= 400:
        w.say(w.tone("warn", f"Google answered {code} when testing the key; saving it anyway"))
    try:
        w.system.keyring_set(GEMINI_SERVICE, "api_key", key)
        if w.system.keyring_get(GEMINI_SERVICE, "api_key") != key:
            raise RuntimeError
    except Exception:  # noqa: BLE001
        return Result("warn", "couldn't save to the keyring (pip install keyring); set GEMINI_API_KEY instead")
    from . import llm
    llm.reset_gemini_state()
    llm.forget_gemini_key_cache()
    done = Result("ok", "key works and is saved in the keyring" if code < 400 else "key saved in the keyring")
    if w.confirm("Test the key with one tiny live request (uses about 1 token of quota)?", False):
        outcome, model = gemini_probe(w, key)
        if outcome == "ok":
            w.say(w.tone("ok", f"Gemini answered on {model}: the key has quota"))
        elif outcome == "no_quota":
            w.say(w.tone("warn", "Google says this key's free tier has no quota (limit 0). " + llm.GEMINI_EXHAUSTED))
            return Result("warn", "key saved, but it has no free quota; " + llm.GEMINI_EXHAUSTED)
        elif outcome == "not_found":
            w.say(w.tone("warn", f"Google doesn't know the model {model}. The hub will pick a current one on its own."))
        else:
            w.say(w.tone("warn", "The test request failed (network or a Google error); the key is saved anyway"))
    return done


def step_ai(w: Wizard) -> Result:
    w.say("The hub can use these AIs. None needs an Anthropic API key (that is billed per use):")
    w.say("  - Local model (Ollama): free, private, runs on this laptop.")
    w.say("  - Gemini: a free key from aistudio.google.com/apikey. Recommended for everyday jobs; its free tier is")
    w.say("    separate from your Claude limits.")
    w.say("  - Claude (my login): the `claude` program (Claude Code) logged in with your Claude subscription.")
    w.say("    It shares your normal Claude usage limit, so keep it for heavy tasks.")
    cli, version = _claude_cli(w)
    if cli:
        w.say(w.tone("ok", "Claude Code found" + (f" ({version})" if version else "")
                     + ". If you have never signed in, open a terminal, run `claude` once and log in."))
    else:
        w.say(w.tone("warn", "Claude Code not found. To use your Claude login: install it from claude.com/claude-code, "
                             "run `claude` once and sign in. Then re-run: python -m hub setup --only ai"))
    notes = []
    if _gemini_saved(w):
        notes.append("Gemini key present")
    elif w.confirm("Add a free Gemini key now?", True):
        r = step_gemini(w)
        w.say(w.tone(r.status, f"Gemini key: {r.message}"))
        if r.status == "ok":
            notes.append("Gemini key saved")
    ready = ["local"] + (["claude"] if w.system.env("ANTHROPIC_API_KEY") else []) \
        + (["claude-login"] if cli else []) + (["gemini"] if _gemini_saved(w) or "Gemini key saved" in notes else [])
    gem_ok = "gemini" in ready
    if gem_ok:
        ready.append("duo")
    config = w.load_config(True)
    options = ["auto", *ready]
    w.say("Default AI for everyday jobs: auto picks Claude API key if set, else Gemini, else your Claude login, else local.")
    suggest = config.default_provider if config.default_provider in options else "auto"
    if "duo" in ready and cli and config.default_provider in ("auto", ""):
        w.say("Recommended: duo. Claude (your login) plans and checks, sparingly; Gemini does the work, so your Claude quota lasts.")
        suggest = "duo"
    choice = w.ask(f"Default AI ({'/'.join(options)})", suggest).lower()
    if choice not in options:
        w.say(w.tone("warn", f"'{choice[:20]}' isn't one of {', '.join(options)}; keeping auto"))
        choice = "auto"
    config.default_provider = choice
    w.save_config()
    return Result("ok", f"default AI: {choice}; ready: {', '.join(ready)}" + (f" ({'; '.join(notes)})" if notes else ""))


def check_phone(w: Wizard) -> Result:
    config = w.load_config(False)
    if config is None or not config.ntfy_topic:
        return Result("warn", "no push topic yet")
    return Result("ok", f"topic {config.ntfy_topic} on {config.ntfy_server}")


def step_phone(w: Wizard) -> Result:
    config = w.load_config(True)
    assert config is not None
    if config.ntfy_topic and w.confirm("A push topic already exists. Make a new random topic? (the old one stops working)", False):
        config.ntfy_topic = ""
    if not config.ntfy_topic:
        config.ntfy_topic = secrets.token_urlsafe(18)   # 24 characters
    if not config.ntfy_server:
        config.ntfy_server = "https://ntfy.sh"
    w.save_config()
    w.say(f"Install the free ntfy app on your phone and subscribe to this topic: {config.ntfy_topic}")
    w.say(f"(server {config.ntfy_server}; the topic works like a password, so don't share it)")
    return Result("ok", "push topic ready")


def check_name(w: Wizard) -> Result:
    config = w.load_config(False)
    return Result("ok", f"I call you {config.display_name}") if config else Result("warn", "no hub.json yet")


def clean_name(text: str) -> str:
    """1-20 printable characters; control characters, angle brackets and quotes dropped, spaces collapsed."""
    text = "".join(ch for ch in (text or "") if ch.isprintable() and ch not in "<>\"'`")
    return " ".join(text.split())[:20]


def step_name(w: Wizard) -> Result:
    config = w.load_config(True)
    assert config is not None
    current = config.display_name or "Chief"
    name = clean_name(w.ask(f'What should I call you? (Enter keeps "{current}")', current)) or current
    config.display_name = name
    w.save_config()
    return Result("ok", f"I'll call you {name}")


def check_warp(w: Wizard) -> Result:
    config = w.load_config(False)
    if config and "warp_connect" in config.scripts:
        return Result("ok", "quick button 'warp_connect' exists")
    return Result("warn", "no warp_connect button")


def step_warp(w: Wizard) -> Result:
    config = w.load_config(True)
    assert config is not None
    path = WARP_PATH
    if not w.system.exists(path):
        return Result("skip", "Cloudflare WARP isn't at its usual place; add the button by hand with `python -m hub add-script`")
    if not w.confirm("Add a 'Connect Cloudflare' quick button that runs warp-cli connect?", False):
        return Result("skip", "skipped")
    config.scripts["warp_connect"] = {"label": "Connect Cloudflare", "argv": [path, "connect"]}
    w.save_config()
    return Result("ok", "quick button 'warp_connect' saved")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_autostart(w: Wizard) -> Result:
    if w.system.platform != "win32":
        return Result("skip", "Windows only")
    rc, _ = w.system.run(["schtasks", "/Query", "/TN", TASK_NAME])
    return Result("ok", f"scheduled task {TASK_NAME} exists") if rc == 0 else Result("warn", f"no scheduled task {TASK_NAME}")


def step_autostart(w: Wizard) -> Result:
    s = w.system
    if s.platform != "win32":
        return Result("skip", "scheduled tasks are Windows only")
    result = check_autostart(w)
    question = "Update the auto-start task (hub starts when you log in)?" if result.status == "ok" \
        else "Start the hub automatically when you log in?"
    if not w.confirm(question, True):
        return Result("skip", "skipped")
    pythonw = s.python_dir() / "pythonw.exe"
    exe = str(pythonw if s.exists(pythonw) else s.python_dir() / "python.exe")
    arguments = "-m hub serve"
    if w.config_path != cfg.default_path():
        arguments = f'-m hub --config "{w.config_path}" serve'
    script = (
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(arguments)} "
        f"-WorkingDirectory {_ps_quote(str(repo_root()))}; "
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
        "$p = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $a -Trigger $t -Settings $p -Force | Out-Null")
    rc, _ = s.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)
    if rc != 0:
        return Result("fail", "couldn't register the scheduled task (try an administrator PowerShell)")
    msg = f"task {TASK_NAME} runs the hub at logon"
    if w.confirm("Stop the laptop sleeping while plugged in (powercfg standby-timeout-ac 0)?", True):
        rc, _ = s.run(["powercfg", "/change", "standby-timeout-ac", "0"], timeout=30)
        msg += "; sleep on AC disabled" if rc == 0 else "; couldn't change the sleep setting"
        if rc != 0:
            return Result("warn", msg)
    return Result("ok", msg)


def check_updater(w: Wizard) -> Result:
    if w.system.platform != "win32":
        return Result("skip", "Windows only")
    rc, _ = w.system.run(["schtasks", "/Query", "/TN", UPDATE_TASK_NAME])
    config = w.load_config(False)
    has_button = bool(config and "update" in config.scripts)
    if rc == 0 and has_button:
        return Result("ok", f"task {UPDATE_TASK_NAME} and the 'Update REDLINE' button exist")
    missing = [n for n, ok in ((f"scheduled task {UPDATE_TASK_NAME}", rc == 0), ("'update' button", has_button)) if not ok]
    return Result("warn", "missing " + " and ".join(missing) + " (python -m hub install-updater)")


def step_updater(w: Wizard) -> Result:
    """Register the on-demand task that runs `hub update --restart`, then add the Launcher button for it.
    It is a task of its own so that stopping the hub's task cannot kill the updater."""
    s = w.system
    if s.platform != "win32":
        return Result("skip", "scheduled tasks are Windows only")
    config = w.load_config(True)
    assert config is not None
    pythonw = s.python_dir() / "pythonw.exe"
    exe = str(pythonw if s.exists(pythonw) else s.python_dir() / "python.exe")
    arguments = "-m hub update --restart"
    if w.config_path != cfg.default_path():
        arguments = f'-m hub --config "{w.config_path}" update --restart'
    script = (
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(arguments)} "
        f"-WorkingDirectory {_ps_quote(str(repo_root()))}; "
        "$p = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"Register-ScheduledTask -TaskName {_ps_quote(UPDATE_TASK_NAME)} -Action $a -Settings $p -Force | Out-Null")
    rc, _ = s.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)
    if rc != 0:
        return Result("fail", "couldn't register the update task (try an administrator PowerShell)")
    cfg.set_script(config, "update", "Update REDLINE", ["schtasks", "/Run", "/TN", UPDATE_TASK_NAME])
    w.save_config()
    return Result("ok", f"task {UPDATE_TASK_NAME} registered and 'Update REDLINE' button saved. "
                        "Restart the hub once so the button appears (log out and in, or: schtasks /End /TN LaptopHub, then /Run)")


def _register_task(w: Wizard, task_name: str, module_args: str, trigger_ps: str, what: str) -> Result:
    """Register a hidden scheduled task running `python -m hub <module_args>` (same pattern as the updater)."""
    s = w.system
    pythonw = s.python_dir() / "pythonw.exe"
    exe = str(pythonw if s.exists(pythonw) else s.python_dir() / "python.exe")
    arguments = f"-m hub {module_args}"
    if w.config_path != cfg.default_path():
        arguments = f'-m hub --config "{w.config_path}" {module_args}'
    script = (
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(arguments)} "
        f"-WorkingDirectory {_ps_quote(str(repo_root()))}; {trigger_ps} "
        "$p = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 2); "
        f"Register-ScheduledTask -TaskName {_ps_quote(task_name)} -Action $a -Trigger $t -Settings $p -Force | Out-Null")
    rc, _ = s.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)
    if rc != 0:
        return Result("fail", f"couldn't register the {task_name} task (try an administrator PowerShell)")
    return Result("ok", f"task {task_name} registered: {what}")


def _check_task(w: Wizard, task_name: str, fix: str) -> Result:
    if w.system.platform != "win32":
        return Result("skip", "Windows only")
    rc, _ = w.system.run(["schtasks", "/Query", "/TN", task_name])
    return Result("ok", f"scheduled task {task_name} exists") if rc == 0 else \
        Result("warn", f"no scheduled task {task_name} ({fix})")


def check_backup(w: Wizard) -> Result:
    return _check_task(w, BACKUP_TASK_NAME, "python -m hub setup --only backup")


def step_backup(w: Wizard) -> Result:
    """Optional weekly backup (Sunday 11:00) into Documents/REDLINE-backups. The wizard already asked "Set up Weekly backup?"."""
    if w.system.platform != "win32":
        return Result("skip", "scheduled tasks are Windows only")
    return _register_task(w, BACKUP_TASK_NAME, "backup", "$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 11:00;",
                          "every Sunday 11:00, zips go to Documents\\REDLINE-backups. Run now: python -m hub backup")


def check_watchdog(w: Wizard) -> Result:
    return _check_task(w, WATCHDOG_TASK_NAME, "python -m hub setup --only watchdog")


def step_watchdog(w: Wizard) -> Result:
    """Every 10 minutes: restart the hub / Tailscale if they died, and push to the phone when it did something."""
    if w.system.platform != "win32":
        return Result("skip", "scheduled tasks are Windows only")
    return _register_task(
        w, WATCHDOG_TASK_NAME, "watchdog --once",
        "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10);",
        "checks every 10 minutes and pushes to your phone only when something was fixed or needs you")


STEPS: dict[str, tuple[Callable[[Wizard], Result], Callable[[Wizard], Result]]] = {
    "jarvis": (step_jarvis, check_jarvis), "gmail": (step_gmail, check_gmail), "asana": (step_asana, check_asana),
    "calendar": (step_calendar, check_calendar), "chat": (step_chat, check_chat), "ai": (step_ai, check_ai),
    "gemini": (step_gemini, check_gemini), "council": (step_council, check_council),
    "autopilot": (step_autopilot, check_autopilot),
    "phone": (step_phone, check_phone), "name": (step_name, check_name), "warp": (step_warp, check_warp),
    "autostart": (step_autostart, check_autostart), "updater": (step_updater, check_updater),
    "backup": (step_backup, check_backup), "watchdog": (step_watchdog, check_watchdog),
}


def parse_only(text: str | None) -> list[str]:
    if not text:
        return list(STEP_IDS)
    chosen: list[str] = []
    for raw in text.split(","):
        key = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if key not in STEPS:
            raise ValueError(f"Unknown step '{raw.strip()[:30]}'. Steps: {', '.join(ONLY_ORDER)}")
        if key not in chosen:
            chosen.append(key)
    if "autostart" in chosen and "updater" not in chosen:
        chosen.append("updater")   # `--only autostart` also offers the update button
    return [s for s in ONLY_ORDER if s in chosen]


def run_wizard(w: Wizard, only: str | None = None, check: bool = False, run_doctor: Callable[[], None] | None = None) -> int:
    try:
        chosen = parse_only(only)
    except ValueError as exc:
        w.say(str(exc))
        return 2
    w.say("REDLINE setup. " + ("Checking what is configured (nothing is changed)." if check else
          "Secrets are typed hidden and stay on this laptop."))
    w.say("Press Enter to skip any step, Ctrl+C to stop. Re-run any time: python -m hub setup --only gmail,calendar")
    w.say("Steps: " + ", ".join(STEP_IDS))
    stopped = False
    total = len(chosen)
    for number, step in enumerate(chosen, 1):
        w.say("")
        w.say(f"[{number}/{total}] {TITLES[step]}")
        act, test = STEPS[step]
        try:
            if check:
                result = test(w)
            elif step == "name" or w.confirm(f"Set up {TITLES[step]}?", True):
                result = act(w)
            else:
                result = Result("skip", "skipped")
        except (KeyboardInterrupt, EOFError):
            w.say("")
            w.say("Stopped. Nothing after this point was changed.")
            stopped = True
            break
        except Exception as exc:  # noqa: BLE001 - a step must never stop the others, and must not leak
            result = Result("fail", "unexpected problem: " + scrub(type(exc).__name__))
        w.report(step, result)
    w.say("")
    w.say("== Summary" + (" (steps completed before you stopped)" if stopped else ""))
    for step, result in w.results:
        w.say(w.tone(result.status, f"{TITLES[step]:<16} {result.message}"))
    if stopped:
        return 130
    if run_doctor and not check:
        w.say("")
        w.say("== Hub doctor")
        try:
            run_doctor()
        except KeyboardInterrupt:
            w.say("Doctor skipped. Run it later: python -m hub doctor")
        except Exception:  # noqa: BLE001
            w.say("Doctor couldn't finish. Run it later: python -m hub doctor")
    if not check:
        w.say("")
        w.say("Restart the hub so it picks everything up:  python -m hub serve  (or log out and in if auto-start is on).")
    return 1 if any(r.status == "fail" for _, r in w.results) else 0


def run_setup(args, wizard: Wizard | None = None) -> int:
    path = Path(args.config) if getattr(args, "config", None) else cfg.default_path()
    w = wizard or Wizard(path)
    check = bool(getattr(args, "check", False))
    if getattr(args, "all", False):
        # No questions: the three scheduled tasks (updater, watchdog, backup) in one go.
        w.confirm = lambda prompt, default=True: True   # type: ignore[method-assign]
        return run_wizard(w, "updater,watchdog,backup", check, None)
    if not check and not w.system.isatty():
        w.say("Setup is interactive and needs a real terminal (it asks for secrets with hidden input). "
              "Open PowerShell and run: python -m hub setup")
        return 2

    def doctor() -> None:
        from .__main__ import cmd_doctor
        import argparse
        cmd_doctor(argparse.Namespace(config=str(path)))

    return run_wizard(w, getattr(args, "only", None), check, doctor)
