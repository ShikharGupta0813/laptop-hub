"""hub.json: where the settings live, and how a first config is made.

The login token is never stored. Only its SHA-256 hash is, which is enough because the
token is long and random. `python -m hub init` prints the token once.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .auth import hash_token

DEFAULT_PORT = 8765


class ConfigError(ValueError):
    """The config is missing or wrong. The message says what to fix."""


@dataclass
class Config:
    token_hash: str
    session_secret: bytes
    roots: dict[str, str]
    notes_dir: str
    inbox_dir: str
    data_dir: str = ""
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ollama_host: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:4b"
    whisper_model: str = "base"
    claude_model: str = "claude-opus-5-5"
    # The assistant does many small tool calls, so it uses the faster, cheaper Sonnet.
    assistant_model: str = "claude-sonnet-5-5"
    # "auto" picks claude (API key) > claude-login (the `claude` program) > gemini (key) > local.
    default_provider: str = "auto"
    claude_cli_model: str = "sonnet"       # model alias for "Claude (my login)"
    claude_cli_path: str = ""              # only if `claude` is not on PATH
    gemini_model: str = "gemini-2.5-flash"   # point at your Pro model (e.g. gemini-2.5-pro) if you have one
    duo_verify: bool = True                # "Claude + Gemini": after tools ran, Claude checks the work was really done (one more Claude call)
    duo_max_claude_per_hour: int = 20      # hard cap on Claude supervisor calls per hour (0 = never ask Claude)
    router_enabled: bool = True            # "auto" looks at each message: chit-chat -> local, tool/code/long -> duo (Gemini)
    tool_failover: bool = True             # the local model fumbled a tool call twice: Gemini does that one step
    local_tool_model: str = ""             # optional sharper Ollama model for tool turns only (empty = chat_model does both)
    allowed_origins: list[str] = field(default_factory=list)
    # Host names the hub answers to besides localhost and *.ts.net (blocks DNS-rebinding pages)
    allowed_hosts: list[str] = field(default_factory=list)
    jarvis_dir: str = ""  # folder of the jarvis-voice-assistant repo (email, calendar, chat helpers)
    shells: dict[str, list[str]] = field(default_factory=dict)
    scripts: dict[str, dict] = field(default_factory=dict)
    embed_model: str = "nomic-embed-text"   # Ollama model that turns text into vectors ("Ask my files")
    kb_roots: list[str] = field(default_factory=list)   # which shared folders "Ask my files" reads (empty = all)
    writable_roots: list[str] = field(default_factory=lambda: ["notes", "inbox"])  # roots the file manager may change
    ntfy_server: str = "https://ntfy.sh"    # optional phone push notifications through ntfy
    ntfy_topic: str = ""                    # a long random topic name you subscribe to in the ntfy app
    # Apps the phone may launch on the laptop: {"Chrome": {"target": "chrome.exe", "args": []}}
    apps: dict[str, dict] = field(default_factory=dict)
    # Mixed into every session cookie's signature; POST /api/logout-all raises it to log every phone out.
    session_generation: int = 0
    display_name: str = "Chief"             # what the hub and the assistant call you (set by `python -m hub setup`)
    jarvis_name: str = "Jarvis"             # the laptop-operator persona's name (voice: "Jarvis, ..." routes to it)
    # FULL CONTROL: every assistant/Friday tool approval is granted automatically (run_command and file writes
    # included). Off by default. The phone can only turn it ON with the typed confirmation "I UNDERSTAND".
    full_control: bool = False
    fast_commands: bool = True              # with full_control: "run <cmd>" / "open <app>" / "type <text>" skip the AI
    # Council: side jobs that run the multi-llm-lab CLI (several models working on one task). See docs/api/council.md.
    council_dir: str = ""                   # folder of the multi-llm-lab checkout; empty = the feature is off
    council_python: str = ""                # interpreter for it; empty = <council_dir>/.venv if present, else the hub's own
    council_timeout_min: float = 90         # hard wall-clock limit for one council run (minutes)
    council_allow_claude: bool = False      # only when True may a run be started with `--escalate claude`
    # Autopilot: prepares drafts with a free AI and queues them as approval cards. Off until you turn it on.
    autopilot_enabled: bool = False
    autopilot_sweep_min: int = 15           # minutes between looks at mail, chat alerts and Asana
    autopilot_daily_ai_calls: int = 30      # most AI calls Autopilot may make per day (then cards come without a draft)
    autopilot_expire_hours: int = 48        # an untouched card expires after this long
    autopilot_standup_time: str = "08:30"   # local time of the weekday standup draft
    autopilot_repos: list[str] = field(default_factory=list)   # local git folders read for the standup draft
    autopilot_tone: str = "Friendly but professional, short, plain text."   # the voice drafts are written in
    autopilot_research: bool = True         # read the mail thread / Asana task / notes before drafting (no extra AI calls)
    # Today: one read-only timeline of the day (meetings, tasks, spending, mail, what you did) plus a spoken summary.
    today_enabled: bool = True
    today_evening_time: str = "19:30"       # weekday time of the evening summary (needs Autopilot on); "" = off
    today_morning_time: str = "08:30"       # daily "your day is ready" push with the front-page link; "" = off
    # Proactive: interrupt-vs-batch rule (hub/proactive.py). Urgent things push now; finished things are batched.
    proactive_enabled: bool = True
    proactive_quiet_start: str = "22:00"
    proactive_quiet_end: str = "07:00"
    proactive_batch_minutes: int = 30
    # Deep research with citations (hub/research.py): multi-hop web investigation, free AI only, daily AI-call budget.
    research_enabled: bool = True
    research_max_hops: int = 3
    research_max_sources: int = 8
    research_daily_ai_calls: int = 20
    research_search_backend: str = "auto"      # auto (DuckDuckGo, then Wikipedia) | ddg | wikipedia; hand-edit

    # Skills: community skills (markdown instructions) installed from GitHub. See docs/api/skills.md.
    skills_enabled: bool = True
    skills_sources: list[str] = field(default_factory=lambda: ["NousResearch/hermes-agent/skills/*", "openclaw/openclaw/skills"])
    # Brain: long-term memory and task desk (RCAs, decisions, follow-ups). See docs/api/brain.md.
    brain_enabled: bool = True
    brain_context: bool = True              # add the best matching memories to question-like assistant turns

    backup_dir: str = ""                    # where `python -m hub backup` writes; empty = Documents/REDLINE-backups

    def data_path(self) -> Path:
        """Where Learn and English keep their history (quiz results, vocabulary...)."""
        return Path(self.data_dir) if self.data_dir else Path.home() / ".laptop-hub" / "data"

    def shell_table(self) -> dict[str, list[str]]:
        return self.shells or default_shells()


def default_shells() -> dict[str, list[str]]:
    if sys.platform == "win32":
        return {"powershell": ["powershell.exe", "-NoLogo"], "wsl": ["wsl.exe"],
                "cmd": ["cmd.exe"]}
    return {"bash": [os.environ.get("SHELL") or "/bin/bash"]}


SCRIPT_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def set_script(config: "Config", name: str, label: str, argv: list[str]) -> bool:
    """Put a quick button into `config.scripts` (caller saves). True if it replaced an existing one."""
    replaced = name in config.scripts
    config.scripts[name] = {"label": label, "argv": list(argv)}
    return replaced


def default_path() -> Path:
    return Path(os.environ.get("HUB_CONFIG") or Path.home() / ".laptop-hub" / "hub.json")


def make(roots: dict[str, str] | None = None) -> tuple[Config, str]:
    """A fresh config, and the one-time login token to show the user."""
    token = secrets.token_urlsafe(24)
    home = Path.home()
    notes_dir = str(home / "HubNotes")
    inbox_dir = str(home / "HubInbox")
    roots = dict(roots or {"home": str(home)})
    roots.setdefault("notes", notes_dir)
    roots.setdefault("inbox", inbox_dir)
    config = Config(token_hash=hash_token(token), session_secret=secrets.token_bytes(32),
                    roots=roots, notes_dir=notes_dir, inbox_dir=inbox_dir)
    return config, token


def save(config: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "token_hash": config.token_hash,
        "session_secret": config.session_secret.hex(),
        "roots": config.roots, "notes_dir": config.notes_dir, "inbox_dir": config.inbox_dir,
        "data_dir": config.data_dir, "jarvis_dir": config.jarvis_dir,
        "host": config.host, "port": config.port, "ollama_host": config.ollama_host,
        "chat_model": config.chat_model, "whisper_model": config.whisper_model,
        "claude_model": config.claude_model, "assistant_model": config.assistant_model,
        "default_provider": config.default_provider, "claude_cli_model": config.claude_cli_model,
        "claude_cli_path": config.claude_cli_path, "gemini_model": config.gemini_model,
        "duo_verify": bool(config.duo_verify), "duo_max_claude_per_hour": int(config.duo_max_claude_per_hour),
        "router_enabled": bool(config.router_enabled), "tool_failover": bool(config.tool_failover),
        "local_tool_model": config.local_tool_model,
        "allowed_origins": config.allowed_origins, "allowed_hosts": config.allowed_hosts, "shells": config.shells,
        "scripts": config.scripts, "apps": config.apps, "embed_model": config.embed_model,
        "kb_roots": config.kb_roots, "writable_roots": config.writable_roots,
        "ntfy_server": config.ntfy_server, "ntfy_topic": config.ntfy_topic,
        "session_generation": int(config.session_generation or 0),
        "display_name": config.display_name, "jarvis_name": config.jarvis_name,
        "full_control": bool(config.full_control), "fast_commands": bool(config.fast_commands),
        "council_dir": config.council_dir, "council_python": config.council_python,
        "council_timeout_min": config.council_timeout_min, "council_allow_claude": bool(config.council_allow_claude),
        "autopilot_enabled": bool(config.autopilot_enabled), "autopilot_sweep_min": int(config.autopilot_sweep_min),
        "autopilot_daily_ai_calls": int(config.autopilot_daily_ai_calls),
        "autopilot_expire_hours": int(config.autopilot_expire_hours),
        "autopilot_standup_time": config.autopilot_standup_time, "autopilot_repos": list(config.autopilot_repos),
        "autopilot_tone": config.autopilot_tone, "autopilot_research": bool(config.autopilot_research),
        "today_enabled": bool(config.today_enabled), "today_evening_time": config.today_evening_time,
        "today_morning_time": config.today_morning_time, "proactive_enabled": bool(config.proactive_enabled),
        "proactive_quiet_start": config.proactive_quiet_start, "proactive_quiet_end": config.proactive_quiet_end,
        "proactive_batch_minutes": int(config.proactive_batch_minutes),
        "skills_enabled": bool(config.skills_enabled), "skills_sources": list(config.skills_sources),
        "research_enabled": bool(config.research_enabled), "research_max_hops": int(config.research_max_hops),
        "research_max_sources": int(config.research_max_sources),
        "research_daily_ai_calls": int(config.research_daily_ai_calls),
        "research_search_backend": str(config.research_search_backend),
        "brain_enabled": bool(config.brain_enabled), "brain_context": bool(config.brain_context),
        "backup_dir": config.backup_dir,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # no effect on Windows; the folder is already per-user
    except OSError:
        pass
    os.replace(tmp, path)


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"{path} not found. Run `python -m hub init` first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["session_secret"] = bytes.fromhex(data["session_secret"])
        known = Config.__dataclass_fields__
        config = Config(**{k: v for k, v in data.items() if k in known})
        config.session_generation = int(config.session_generation or 0)
        config.full_control = config.full_control is True       # only a real JSON true counts, never "false" or 1
        config.fast_commands = config.fast_commands is not False
        config.jarvis_name = str(config.jarvis_name or "Jarvis").strip()[:40] or "Jarvis"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path} is damaged ({exc}). Run `python -m hub init --force`.") from None
    if config.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: host is {config.host}. The hub is reachable by other devices on your "
              "network without HTTPS. Use Tailscale and keep host 127.0.0.1.", file=sys.stderr)
    return config
