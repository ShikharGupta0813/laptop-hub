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
from .redact import KINDS as REDACT_KINDS

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
    router_local_chat: bool = False        # "auto": send short chit-chat to the local model (only worth it on a fast GPU; off = default provider)
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
    chat_history_enabled: bool = True       # keep conversations in a SQLite file so a restart does not lose them
    chat_history_days: int = 30             # days an idle conversation is kept
    today_cache_min: int = 15              # minutes Today reuses calendar, Asana and mail answers (0 = always live)
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
    # Screen vision (hub/vision.py): 'what's this error on my screen?' sent to Gemini (never the paid Claude API).
    vision_enabled: bool = True
    vision_daily_calls: int = 30
    research_search_backend: str = "auto"      # auto (DuckDuckGo, then Wikipedia) | ddg | wikipedia; hand-edit

    # Skills: community skills (markdown instructions) installed from GitHub. See docs/api/skills.md.
    skills_enabled: bool = True
    skills_sources: list[str] = field(default_factory=lambda: ["NousResearch/hermes-agent/skills/*", "openclaw/openclaw/skills"])
    # Brain: long-term memory and task desk (RCAs, decisions, follow-ups). See docs/api/brain.md.
    brain_enabled: bool = True
    brain_context: bool = True              # add the best matching memories to question-like assistant turns

    # Application queue (hub/applyq.py): drafts wait here; nothing is sent without the owner's tap. See docs/api/applyq.md.
    applyq_enabled: bool = True
    applyq_daily_cap: int = 10              # sends per day before an extra confirm is needed (0 = no cap)

    # Form pre-fill (hub/formfill.py): a browser on the laptop fills an application form and stops for the owner's tap. docs/api/formfill.md.
    formfill_enabled: bool = False
    formfill_headed: bool = True            # show the browser window on the laptop (true), or run it hidden (false)
    formfill_never: list[str] = field(default_factory=lambda: ["linkedin.com"])   # sites this never opens; the owner can edit it

    # Claude Code sessions (hub/ccsessions.py): watch the owner's Claude Code transcripts, nudge one from the phone.
    ccsessions_enabled: bool = True
    ccsessions_dir: str = ""                # "" = ~/.claude/projects
    # Build flow: spec-first delegation (brainstorm, spec, plan, Council builds step by step). See docs/api/buildflow.md.
    buildflow_enabled: bool = True
    # Laptop-side wake word (hub/wakeword.py): listen on the laptop microphone for "Hey Jarvis". Off by default.
    wake_laptop_enabled: bool = False
    wake_laptop_device: str = ""            # sounddevice name or index; "" = the default microphone
    # Opportunities finder (hub/opps.py): public job/freelance sources, off until the owner turns it on.
    opps_enabled: bool = False
    opps_interval_min: int = 120
    opps_min_score: int = 40
    # Source connectors (hub/oppsources.py): ATS boards, remote boards, company watch list. Off until the owner turns it on.
    oppsources_enabled: bool = False
    oppsources_interval_min: int = 60          # normal sources
    oppsources_watch_interval_min: int = 10    # company watch list (early-bird)
    oppsources_stale_days: int = 45
    oppsources_ghost_days: int = 60
    triage_ai: bool = False                 # True: the 'Ask AI' button on a find gives a second opinion (text leaves the laptop; plain verdict always shown) (hub/triage.py)

    # Privacy redaction (hub/redact.py): hide emails, phones, cards, keys... before text goes to a CLOUD model.
    redact_enabled: bool = True
    redact_kinds: list[str] = field(default_factory=lambda: list(REDACT_KINDS))
    redact_names: list[str] = field(default_factory=list)   # extra words to mask (your employer, a project name...)
    # Usage and budget (hub/usage.py): 0 / missing = no cap. At a cap the turn goes to the local model, with a note.
    cloud_daily_cap: int = 0                                 # requests per day across Gemini, Claude and duo
    provider_daily_caps: dict[str, int] = field(default_factory=dict)   # e.g. {"gemini": 200}
    claude_login_5h_soft_cap: int = 0                        # 0 = unknown; else the estimate shows calls in the last 5 h

    # Posts (hub/devto.py): dev.to drafts and post packs. The API key is NOT here (OS keyring). Reminders/checks are off.
    devto_enabled: bool = True
    devto_cadence_enabled: bool = False     # weekly "no post for N days, ideas ready" through Friday's batch
    devto_cadence_days: int = 7
    devto_stats_enabled: bool = False       # scheduled read of views/reactions/comments from dev.to
    devto_stats_hours: int = 24
    devto_blocklist_words: list[str] = field(default_factory=list)   # words that must never appear in a post

    backup_dir: str = ""                    # where `python -m hub backup` writes; empty = Documents/REDLINE-backups

    # Gmailx (hub/gmailx.py): company-account mail BODIES may reach a cloud AI only when this is True. Check your company policy first.
    gmail_company_ai_allowed: bool = False
    # Urgent-alert triage (hub/alerts.py, hub/asana.py). See docs/api/alerts.md. The Asana token is NOT here (keyring).
    alerts_enabled: bool = True
    alerts_vips: list[str] = field(default_factory=list)    # senders (name, number or address fragment) who always matter
    alerts_sources: list[str] = field(default_factory=lambda: ["whatsapp", "slack", "teams", "gmail", "gchat", "asana", "jira", "email", "sms"])
    alerts_rules: dict = field(default_factory=dict)        # editable word lists (urgent_words, time_words, ask_words, ignore_words, mention_words); missing = defaults
    alerts_speak_on_laptop: bool = False                    # say urgent alerts out loud on the laptop speakers
    alerts_ai: bool = False                                 # let the free AI write the plan (text redacted first)
    alerts_email_poll: bool = True                          # look at Gmail through hub/gmailx.py when that module exists
    asana_enabled: bool = False
    asana_poll_minutes: int = 5
    asana_workspace: str = ""                               # workspace gid; empty = the first one Asana lists

    # Deep thinking (hub/models.py): an optional local REASONING model (e.g. deepseek-r1:8b) for slow background jobs only.
    local_reasoning_model: str = ""         # empty = off. Never used for chat, greetings, fast commands or voice replies.
    deep_think_timeout_min: float = 20.0    # a deep-thinking job is stopped after this many minutes
    deep_think_max_chars: int = 30000       # cap on thinking + answer characters per job

    # Dictation hotkey (hub/dictation.py): hold a key on the LAPTOP, speak, release; the text is typed into the focused window.
    # Off by default. None for a list/dict means "the built-in default"; an empty list really is empty.
    dictation_enabled: bool = False
    dictation_paused: bool = False
    dictation_hotkey: str = "ctrl+alt+space"
    dictation_mode: str = "hold"            # hold (push to talk) | toggle
    dictation_language: str = ""            # Whisper language code; "" = detect
    dictation_punctuate: bool = True
    dictation_add_space: bool = True
    dictation_max_seconds: int = 120
    dictation_max_chars: int = 2000
    dictation_use_clipboard: bool = False   # paste instead of typing keys (restores the old clipboard)
    dictation_newline: bool = False         # may "new line" press Enter? Off: it becomes a space
    dictation_assistant_commands: bool = False
    dictation_blocklist: list | None = None         # window title / program fragments where nothing is ever typed
    dictation_spoken: dict | None = None            # spoken punctuation: phrase -> symbol
    dictation_scratch_phrases: list | None = None   # phrases that undo the last dictated chunk
    dictation_assistant_prefixes: list | None = None   # "send to jarvis" style prefixes
    # MCP client (hub/mcpclient.py): the server list is in Settings > MCP servers (mcp_servers.json), nothing is enabled by default.
    mcp_enabled: bool = True                # master switch: False hides every MCP tool and refuses to start any server
    mcp_idle_seconds: int = 300             # a server with no call for this long is stopped (started again on the next call)
    mcp_max_concurrent: int = 4             # tool calls running at once across all servers
    mcp_call_timeout: int = 60              # seconds before one tool call is given up
    # Presence-aware delivery (hub/presence.py): where the owner is decides how each notification reaches them.
    presence_enabled: bool = True           # False = every notification goes out the old way; 'unknown' presence does too
    presence_laptop_toasts: bool = False    # also show a Windows toast (title only) when a rule says toast-on-laptop
    # Agent controls (hub/panic.py, hub/trust.py): the panic button and the trust ladder.
    panic_active: bool = False              # persistent: set by the Panic button, cleared only by Resume (never by Settings)
    panic_confirm: bool = False             # True = Panic asks "are you sure" first; False = one tap works in an emergency
    panic_phrases: list | None = None       # spoken/typed panic phrases; None = the built-in list
    trust_enabled: bool = True              # the trust ladder (suggest "do this automatically" after repeated approvals)
    trust_threshold: int = 10               # approvals of one shape, with no denial in 30 days, before it is suggested
    # Voice output (hub/tts.py, docs/api/tts.md): 'browser' = the phone's own speechSynthesis, i.e. behaviour unchanged until the owner picks a voice.
    tts_enabled: bool = True                # False: the hub never synthesises (the phone and laptop fall back to their old speech)
    tts_engine: str = "browser"             # default engine: browser | sapi | piper | command (per-assistant choices live in assistants.json)
    # Meeting assistant (hub/meetings.py): records ONLY after an explicit start with a consent tick; transcript text only.
    meetings_enabled: bool = False          # off until the owner turns it on (POST /api/meetings/enable)
    meetings_keep_audio: bool = False       # True also writes WAV files beside the transcript (a warning is shown)
    meetings_max_minutes: int = 120         # a recording stops by itself after this long (1 to 480)
    meetings_model: str = "base"            # local Whisper model for meetings: tiny | base | small | medium
    # Computer control (hub/computeruse.py): Jarvis looks at the screen and clicks/types inside an approved scope. OFF by default.
    computeruse_enabled: bool = False
    computeruse_allow_unattended: bool = False      # False: refuses to start or continue while presence says you are away
    computeruse_keep_shots: bool = False            # False: the rolling folder of the last 20 pictures is deleted when the job ends
    computeruse_max_steps: int = 25
    computeruse_blocklist: list | None = None       # window title / program fragments never controlled; None = the built-in list
    computeruse_allow_chords: list | None = None    # key chords the owner allow-listed (for example alt+f4); None = none
    # Playbooks (hub/playbooks.py): "when X happens, do Y" rules. The feature is on but there are no playbooks until the owner writes one.
    playbooks_enabled: bool = True          # False: no playbook runs (the list stays); Panic pauses them too
    playbooks_max_concurrent: int = 3       # playbook runs at the same time, across all playbooks (1 to 20)
    # Scheduled job batches (hub/batches.py): slots build N ready applications/messages and send ONE notification. Sending is always a tap.
    batches_enabled: bool = True            # False (or Panic): no slot fires; the slot list stays
    batches_catchup_min: int = 360          # a slot missed while the laptop slept still fires once if within this many minutes (0 = never catch up)
    batches_outreach_cap: int = 10          # outreach messages sent per day through batches (0 = no cap)
    # Tailoring + answer bank (hub/tailor.py, hub/answerbank.py, hub/fitcheck.py, hub/factbase.py): per-job resume and cover note from the owner's own facts.
    tailor_enabled: bool = True
    tailor_ai: bool = False                 # True: cover notes are rewritten by the owner's chosen AI (still checked against the facts)
    answerbank_enabled: bool = True
    answerbank_min_confidence: int = 75     # an approved answer at or above this (0-100) may be pre-filled; below it is only suggested
    answerbank_embeddings: bool = False     # True: blend local-model embeddings into question matching (words only otherwise)
    # Google Chat channels for a PERSONAL Google account (hub/chatchan.py): read through notifications and Chat emails, reply = clipboard + open Chat.
    chatch_enabled: bool = True             # False: Chat messages are not parsed into spaces/threads and the pending list stays empty
    chatch_email_parse: bool = True         # read Google Chat notification emails (through the mail modules) as Chat messages
    # Networking drafts and interview coach (hub/networking.py, hub/interviews.py): drafts and practice only, nothing is ever sent.
    networking_enabled: bool = True
    networking_nudge_days: int = 14         # a contact not written to for this many days (1 to 365) shows as a "nudge due" suggestion
    networking_ai: bool = False             # True: drafts are reworded by the owner's chosen AI (still checked against the facts; falls back to the plain draft)
    interviews_enabled: bool = True
    interviews_ai: bool = False             # True: extra questions and a short critique from the owner's chosen AI (text only; falls back to rules)
    # Job tracker (hub/jobtrack.py): applications, follow-up reminders, the learning loop. Nothing is ever sent or applied by it.
    jobtrack_enabled: bool = True
    jobtrack_followup_days: list | None = None   # days after applying to remind you; None = [5, 10]; [] = no follow-up reminders
    jobtrack_ghost_days: int = 14                # quiet this long: the screen SUGGESTS marking it ghosted (never automatic)

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
        "router_enabled": bool(config.router_enabled), "router_local_chat": bool(config.router_local_chat), "tool_failover": bool(config.tool_failover),
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
        "today_cache_min": int(config.today_cache_min),
        "chat_history_enabled": bool(config.chat_history_enabled), "chat_history_days": int(config.chat_history_days),
        "today_morning_time": config.today_morning_time, "proactive_enabled": bool(config.proactive_enabled),
        "proactive_quiet_start": config.proactive_quiet_start, "proactive_quiet_end": config.proactive_quiet_end,
        "proactive_batch_minutes": int(config.proactive_batch_minutes),
        "skills_enabled": bool(config.skills_enabled), "skills_sources": list(config.skills_sources),
        "research_enabled": bool(config.research_enabled), "research_max_hops": int(config.research_max_hops),
        "research_max_sources": int(config.research_max_sources),
        "research_daily_ai_calls": int(config.research_daily_ai_calls),
        "vision_enabled": bool(config.vision_enabled), "vision_daily_calls": int(config.vision_daily_calls),
        "research_search_backend": str(config.research_search_backend),
        "brain_enabled": bool(config.brain_enabled), "brain_context": bool(config.brain_context),
        "ccsessions_enabled": bool(config.ccsessions_enabled), "ccsessions_dir": str(config.ccsessions_dir or ""),
        "buildflow_enabled": bool(config.buildflow_enabled),
        "wake_laptop_enabled": config.wake_laptop_enabled is True, "wake_laptop_device": str(config.wake_laptop_device or ""),
        "devto_enabled": config.devto_enabled is not False, "devto_cadence_enabled": config.devto_cadence_enabled is True,
        "devto_cadence_days": int(config.devto_cadence_days), "devto_stats_enabled": config.devto_stats_enabled is True,
        "devto_stats_hours": int(config.devto_stats_hours), "devto_blocklist_words": list(config.devto_blocklist_words),
        "backup_dir": config.backup_dir,
        "opps_enabled": config.opps_enabled is True, "opps_interval_min": int(config.opps_interval_min),
        "opps_min_score": int(config.opps_min_score),
        "oppsources_enabled": config.oppsources_enabled is True, "oppsources_interval_min": int(config.oppsources_interval_min),
        "oppsources_watch_interval_min": int(config.oppsources_watch_interval_min),
        "oppsources_stale_days": int(config.oppsources_stale_days), "oppsources_ghost_days": int(config.oppsources_ghost_days), "triage_ai": config.triage_ai is True,
        "alerts_enabled": config.alerts_enabled is not False, "alerts_vips": list(config.alerts_vips or []),
        "alerts_sources": list(config.alerts_sources or []), "alerts_rules": dict(config.alerts_rules or {}),
        "alerts_speak_on_laptop": config.alerts_speak_on_laptop is True, "alerts_ai": config.alerts_ai is True,
        "alerts_email_poll": config.alerts_email_poll is not False,
        "asana_enabled": config.asana_enabled is True, "asana_poll_minutes": int(config.asana_poll_minutes or 5),
        "asana_workspace": str(config.asana_workspace or ""),
        "redact_enabled": config.redact_enabled is not False, "redact_kinds": list(config.redact_kinds),
        "redact_names": list(config.redact_names), "cloud_daily_cap": int(config.cloud_daily_cap or 0),
        "provider_daily_caps": dict(config.provider_daily_caps or {}),
        "claude_login_5h_soft_cap": int(config.claude_login_5h_soft_cap or 0),
        "gmail_company_ai_allowed": config.gmail_company_ai_allowed is True,
        "local_reasoning_model": str(config.local_reasoning_model or ""),
        "deep_think_timeout_min": float(config.deep_think_timeout_min or 20.0),
        "deep_think_max_chars": int(config.deep_think_max_chars or 30000),
        "dictation_enabled": config.dictation_enabled is True, "dictation_paused": config.dictation_paused is True,
        "dictation_hotkey": str(config.dictation_hotkey or ""), "dictation_mode": str(config.dictation_mode or "hold"),
        "dictation_language": str(config.dictation_language or ""), "dictation_punctuate": config.dictation_punctuate is not False,
        "dictation_add_space": config.dictation_add_space is not False,
        "dictation_max_seconds": int(config.dictation_max_seconds or 120), "dictation_max_chars": int(config.dictation_max_chars or 2000),
        "dictation_use_clipboard": config.dictation_use_clipboard is True, "dictation_newline": config.dictation_newline is True,
        "dictation_assistant_commands": config.dictation_assistant_commands is True,
        "dictation_blocklist": config.dictation_blocklist, "dictation_spoken": config.dictation_spoken,
        "dictation_scratch_phrases": config.dictation_scratch_phrases,
        "dictation_assistant_prefixes": config.dictation_assistant_prefixes,
        "formfill_enabled": config.formfill_enabled is True, "formfill_headed": config.formfill_headed is not False,
        "formfill_never": list(config.formfill_never or []),
        "presence_enabled": config.presence_enabled is not False, "presence_laptop_toasts": config.presence_laptop_toasts is True,
        "panic_active": config.panic_active is True, "panic_confirm": config.panic_confirm is True,
        "panic_phrases": config.panic_phrases, "trust_enabled": config.trust_enabled is not False,
        "trust_threshold": int(config.trust_threshold or 10),
        "applyq_enabled": config.applyq_enabled is not False, "applyq_daily_cap": int(config.applyq_daily_cap or 0),
        "mcp_enabled": config.mcp_enabled is not False, "mcp_idle_seconds": int(config.mcp_idle_seconds),
        "mcp_max_concurrent": int(config.mcp_max_concurrent), "mcp_call_timeout": int(config.mcp_call_timeout),
        "tts_enabled": config.tts_enabled is not False, "tts_engine": str(config.tts_engine or "browser"),
        "meetings_enabled": config.meetings_enabled is True, "meetings_keep_audio": config.meetings_keep_audio is True,
        "meetings_max_minutes": int(config.meetings_max_minutes or 120), "meetings_model": str(config.meetings_model or "base"),
        "computeruse_enabled": config.computeruse_enabled is True, "computeruse_allow_unattended": config.computeruse_allow_unattended is True,
        "computeruse_keep_shots": config.computeruse_keep_shots is True, "computeruse_max_steps": int(config.computeruse_max_steps or 25),
        "computeruse_blocklist": config.computeruse_blocklist, "computeruse_allow_chords": config.computeruse_allow_chords,
        "playbooks_enabled": config.playbooks_enabled is not False, "playbooks_max_concurrent": int(config.playbooks_max_concurrent or 3),
        "batches_enabled": config.batches_enabled is not False, "batches_catchup_min": int(config.batches_catchup_min),
        "batches_outreach_cap": int(config.batches_outreach_cap),
        "tailor_enabled": config.tailor_enabled is not False, "tailor_ai": config.tailor_ai is True,
        "answerbank_enabled": config.answerbank_enabled is not False, "answerbank_min_confidence": int(config.answerbank_min_confidence or 75),
        "answerbank_embeddings": config.answerbank_embeddings is True,
        "chatch_enabled": config.chatch_enabled is not False, "chatch_email_parse": config.chatch_email_parse is not False,
        "networking_enabled": config.networking_enabled is not False, "networking_nudge_days": int(config.networking_nudge_days or 14),
        "networking_ai": config.networking_ai is True, "interviews_enabled": config.interviews_enabled is not False,
        "interviews_ai": config.interviews_ai is True,
        "jobtrack_enabled": config.jobtrack_enabled is not False, "jobtrack_followup_days": config.jobtrack_followup_days,
        "jobtrack_ghost_days": int(config.jobtrack_ghost_days or 14),
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
