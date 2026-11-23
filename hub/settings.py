"""Settings: the few options the phone may change, the audit log viewer, backups and diagnostics.

Only an allow-list of non-secret options can be changed from the phone (`ALLOWED_KEYS`). Everything that
decides who may log in or what may run on the laptop (token, session secret, allowed hosts and origins,
scripts, apps, shells, the Jarvis folder) stays hand-edited in hub.json and is refused with a 400.

The hub's own config file is found through `ctx.config_path` when the server provides it; otherwise
through `config.default_path()` (HUB_CONFIG or ~/.laptop-hub/hub.json), and only if that file is the
same hub's (same login token hash). When no file can be found the change still applies until restart and
the response says `persisted: false`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, StrictBool

from . import config as cfg
from . import providers
from . import redact as redact_mod
from . import usage as usage_mod
from .common import Ctx

# ---- what may be changed -------------------------------------------------------------------------------

MODEL_KEYS = ("chat_model", "assistant_model", "claude_model", "embed_model", "whisper_model",
              "gemini_model", "claude_cli_model")
ALLOWED_KEYS = (*MODEL_KEYS, "default_provider", "duo_verify", "duo_max_claude_per_hour", "kb_roots", "writable_roots", "ntfy_server", "ntfy_topic", "display_name",
               "full_control", "fast_commands", "jarvis_name", "autopilot_enabled", "autopilot_sweep_min",
               "autopilot_daily_ai_calls", "autopilot_research", "today_enabled", "today_evening_time", "today_cache_min", "chat_history_enabled", "chat_history_days", "skills_enabled",
               "research_enabled", "research_max_hops", "research_max_sources", "research_daily_ai_calls",
               "brain_enabled", "brain_context", "today_morning_time", "proactive_enabled", "proactive_quiet_start",
               "proactive_quiet_end", "proactive_batch_minutes", "vision_enabled", "vision_daily_calls", "buildflow_enabled",
               "wake_laptop_enabled", "wake_laptop_device", "formfill_enabled", "formfill_headed", "opps_enabled", "opps_interval_min", "opps_min_score",
               "redact_enabled", "redact_kinds", "redact_names", "cloud_daily_cap", "provider_daily_caps",
               "claude_login_5h_soft_cap", "applyq_enabled", "applyq_daily_cap", "gmail_company_ai_allowed")
ALLOWED_KEYS = (*ALLOWED_KEYS, "panic_confirm", "panic_phrases", "trust_enabled", "trust_threshold")   # hub/panic.py, hub/trust.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "oppsources_enabled", "oppsources_interval_min", "oppsources_watch_interval_min", "oppsources_stale_days", "oppsources_ghost_days", "triage_ai")   # hub/oppsources.py, hub/triage.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "tailor_enabled", "tailor_ai", "answerbank_enabled", "answerbank_min_confidence", "answerbank_embeddings")   # hub/tailor.py, hub/answerbank.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "local_reasoning_model", "deep_think_timeout_min", "deep_think_max_chars")   # hub/models.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "batches_enabled", "batches_catchup_min", "batches_outreach_cap")   # hub/batches.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "chatch_enabled", "chatch_email_parse")   # hub/chatchan.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "networking_enabled", "networking_nudge_days", "networking_ai", "interviews_enabled", "interviews_ai")   # hub/networking.py, hub/interviews.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "jobtrack_enabled", "jobtrack_followup_days", "jobtrack_ghost_days")   # hub/jobtrack.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "companyintel_enabled", "companyintel_ai", "offers_enabled", "proposals_enabled")   # hub/companyintel.py, offers.py, proposals.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "jobdigest_enabled", "jobdigest_hour", "jobdigest_notify", "jobdigest_weekly_day", "jobdigest_nudge_days", "jobdigest_nudges")   # hub/jobdigest.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "replies_enabled", "replies_ai", "jobinsights_enabled", "outreach_enabled", "outreach_followup_days", "outreach_gap_seconds")   # hub/replies.py, jobinsights.py, outreach.py
ALLOWED_KEYS = (*ALLOWED_KEYS, "sharein_enabled")   # hub/sharein.py
# Never shown and never accepted here (named so the UI can say "edit hub.json by hand").
LOCKED_KEYS = ("token_hash", "session_secret", "allowed_hosts", "allowed_origins", "scripts", "apps",
               "shells", "jarvis_dir", "session_generation", "council_dir", "council_python", "council_timeout_min",
               "council_allow_claude")
SECRET_KEYS = ("ntfy_topic", "redact_names")   # changes are audited without the value (the topic works like a password)

FULL_CONTROL_PHRASE = "I UNDERSTAND"
MODEL_NAME = re.compile(r"\A[A-Za-z0-9._:/-]{1,80}\Z")
TOPIC = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")
BACKUP_NAME = re.compile(r"\Ahub-backup-\d{8}-\d{6}(?:-\d{1,3})?\.zip\Z")
KEEP_BACKUPS = 5
MAX_BACKUP_BYTES = 2 * 1024 ** 3          # refuse to zip more than 2 GiB
SECRET_SCAN_BYTES = 2 * 1024 * 1024       # files up to this size are checked for the token hash / secret
MAX_AUDIT_ROWS = 500
MAX_AUDIT_SCAN_BYTES = 16 * 1024 * 1024   # the viewer never reads further back than this
AUDIT_CHUNK = 64 * 1024
MAX_FIELD_CHARS = 500
OLLAMA_TIMEOUT = 3.0

STARTED = time.time()


class PullBody(BaseModel):
    name: str
    confirm: StrictBool = False


class ModelTestBody(BaseModel):
    model: str


class BackupBody(BaseModel):
    confirm: StrictBool = False


# ---- config helpers ---------------------------------------------------------------------------------------

def _version() -> str:
    try:
        from .server import VERSION
        return str(VERSION)
    except Exception:  # noqa: BLE001 - the About page must never fail
        return "unknown"


def valid_model_name(value: Any) -> bool:
    """Letters, digits and . _ : / - only (as Ollama names are), no '..', not starting with '/' or '-'."""
    return (isinstance(value, str) and bool(MODEL_NAME.match(value.strip())) and ".." not in value
            and not value.strip().startswith(("/", "-")))


def _clean_name(value: Any, key: str) -> str:
    if not valid_model_name(value):
        raise HTTPException(400, f"{key} must be a model name like qwen3:4b (letters, digits and . _ : / - only).")
    return value.strip()


def _clean_roots(value: Any, key: str, roots: dict[str, str]) -> list[str]:
    if not isinstance(value, list) or len(value) > 50 or not all(isinstance(v, str) for v in value):
        raise HTTPException(400, f"{key} must be a list of shared folder names.")
    out: list[str] = []
    for name in value:
        if name not in roots:
            raise HTTPException(400, f"There is no shared folder called '{name}'.")
        if name not in out:
            out.append(name)
    return out


def _clean_ntfy_server(value: Any) -> str:
    bad = HTTPException(400, "ntfy_server must be an https address like https://ntfy.sh.")
    if not isinstance(value, str) or len(value) > 200:
        raise bad
    url = value.strip()
    try:
        parsed = urlparse(url)
        parsed.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise bad from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.params or any(c.isspace() for c in url)):
        raise bad
    return url.rstrip("/")


def validate_patch(config, body: Any) -> tuple[dict[str, Any], bool]:
    """(new values, confirmed). Raises HTTPException 400 for anything outside the allow-list or invalid."""
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, "Send the settings you want to change.")
    confirm = body.get("confirm") is True
    unknown = sorted(k for k in body if k not in ALLOWED_KEYS and k not in ("confirm", "full_control_confirm"))
    if unknown:
        locked = [k for k in unknown if k in LOCKED_KEYS]
        if locked:
            raise HTTPException(400, f"{', '.join(locked)} can only be changed by editing hub.json on the laptop.")
        raise HTTPException(400, f"Unknown setting: {', '.join(unknown)[:80]}.")
    changes: dict[str, Any] = {}
    for key in ALLOWED_KEYS:
        if key not in body:
            continue
        value = body[key]
        if key in MODEL_KEYS:
            changes[key] = _clean_name(value, key)
        elif key == "local_reasoning_model":
            if value != "" and not valid_model_name(value):
                raise HTTPException(400, "local_reasoning_model must be empty (off) or a model name like deepseek-r1:8b.")
            changes[key] = value.strip()
        elif key == "deep_think_timeout_min":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 240:
                raise HTTPException(400, "deep_think_timeout_min must be a number from 1 to 240.")
            changes[key] = float(value)
        elif key == "deep_think_max_chars":
            if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 200000:
                raise HTTPException(400, "deep_think_max_chars must be a whole number from 1000 to 200000.")
            changes[key] = value
        elif key == "default_provider":
            if value not in ("auto", "local", "claude", "claude-login", "gemini", "duo") and not (
                    isinstance(value, str) and re.fullmatch(r"cli:[a-z0-9][a-z0-9-]{0,39}", value)):
                raise HTTPException(400, "default_provider must be auto, local, claude, claude-login, gemini, duo or cli:<id>.")
            changes[key] = value
        elif key == "duo_verify":
            if not isinstance(value, bool):
                raise HTTPException(400, "duo_verify must be true or false.")
            changes[key] = value
        elif key == "duo_max_claude_per_hour":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 200:
                raise HTTPException(400, "duo_max_claude_per_hour must be a whole number from 0 to 200.")
            changes[key] = value
        elif key == "full_control":
            if not isinstance(value, bool):
                raise HTTPException(400, "full_control must be true or false.")
            if value and body.get("full_control_confirm") != FULL_CONTROL_PHRASE:
                raise HTTPException(400, f'Turning Full Control on needs the confirmation text "{FULL_CONTROL_PHRASE}" '
                                         "(send it as full_control_confirm). Turning it off needs nothing.")
            changes[key] = value
        elif key == "fast_commands":
            if not isinstance(value, bool):
                raise HTTPException(400, "fast_commands must be true or false.")
            changes[key] = value
        elif key == "jarvis_name":
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40 or not value.isprintable():
                raise HTTPException(400, "jarvis_name must be 1 to 40 printable characters.")
            changes[key] = value.strip()
        elif key == "wake_laptop_device":
            if not isinstance(value, str) or len(value) > 200 or not value.isprintable():
                raise HTTPException(400, "wake_laptop_device must be a short device name or number (or empty).")
            changes[key] = value.strip()
        elif key == "autopilot_enabled":
            if not isinstance(value, bool):
                raise HTTPException(400, "autopilot_enabled must be true or false.")
            changes[key] = value
        elif key in ("autopilot_research", "today_enabled", "chat_history_enabled", "skills_enabled", "research_enabled", "brain_enabled", "brain_context", "proactive_enabled", "vision_enabled", "buildflow_enabled", "wake_laptop_enabled", "opps_enabled", "oppsources_enabled", "applyq_enabled", "gmail_company_ai_allowed", "formfill_enabled", "formfill_headed", "batches_enabled", "chatch_enabled", "chatch_email_parse", "jobtrack_enabled", "replies_enabled", "replies_ai", "jobinsights_enabled", "outreach_enabled", "triage_ai"):
            if not isinstance(value, bool):
                raise HTTPException(400, f"{key} must be true or false.")
            changes[key] = value
        elif key == "sharein_enabled":
            if not isinstance(value, bool):
                raise HTTPException(400, "sharein_enabled must be true or false.")
            changes[key] = value
        elif key in ("tailor_enabled", "tailor_ai", "answerbank_enabled", "answerbank_embeddings"):
            if not isinstance(value, bool):
                raise HTTPException(400, f"{key} must be true or false.")
            changes[key] = value
        elif key == "answerbank_min_confidence":
            if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 100:
                raise HTTPException(400, "answerbank_min_confidence must be a whole number from 30 to 100.")
            changes[key] = value
        elif key in ("panic_confirm", "trust_enabled"):
            if not isinstance(value, bool):
                raise HTTPException(400, f"{key} must be true or false.")
            changes[key] = value
        elif key == "trust_threshold":
            if isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 100:
                raise HTTPException(400, "trust_threshold must be a whole number from 3 to 100.")
            changes[key] = value
        elif key == "panic_phrases":
            if not isinstance(value, list) or len(value) > 20 or not all(isinstance(v, str) and 2 <= len(v.strip()) <= 60 for v in value):
                raise HTTPException(400, "panic_phrases must be a list of up to 20 short phrases.")
            changes[key] = [v.strip() for v in value]
        elif key in ("opps_interval_min", "opps_min_score"):
            low, high = (15, 1440) if key == "opps_interval_min" else (0, 100)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise HTTPException(400, f"{key} must be a whole number from {low} to {high}.")
            changes[key] = value
        elif key in ("oppsources_interval_min", "oppsources_watch_interval_min", "oppsources_stale_days", "oppsources_ghost_days"):
            low, high = {"oppsources_interval_min": (5, 1440), "oppsources_watch_interval_min": (2, 120),
                         "oppsources_stale_days": (7, 365), "oppsources_ghost_days": (14, 730)}[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise HTTPException(400, f"{key} must be a whole number from {low} to {high}.")
            changes[key] = value
        elif key == "today_evening_time":
            if not isinstance(value, str) or not (value == "" or re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value)):
                raise HTTPException(400, "today_evening_time must look like 19:30 (or be empty to turn it off).")
            changes[key] = value
        elif key == "today_morning_time":
            if not isinstance(value, str) or not (value == "" or re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value)):
                raise HTTPException(400, "today_morning_time must look like 08:30 (or be empty to turn it off).")
            changes[key] = value
        elif key in ("proactive_quiet_start", "proactive_quiet_end"):
            if not isinstance(value, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
                raise HTTPException(400, f"{key} must look like 22:00.")
            changes[key] = value
        elif key == "proactive_batch_minutes":
            if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 720:
                raise HTTPException(400, "proactive_batch_minutes must be a whole number of minutes from 5 to 720.")
            changes[key] = value
        elif key == "today_cache_min":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 120:
                raise HTTPException(400, "today_cache_min must be a whole number of minutes from 0 (always live) to 120.")
            changes[key] = value
        elif key == "chat_history_days":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 365:
                raise HTTPException(400, "chat_history_days must be a whole number of days from 1 to 365.")
            changes[key] = value
        elif key == "autopilot_sweep_min":
            if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 240:
                raise HTTPException(400, "autopilot_sweep_min must be a whole number of minutes from 5 to 240.")
            changes[key] = value
        elif key == "autopilot_daily_ai_calls":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 500:
                raise HTTPException(400, "autopilot_daily_ai_calls must be a whole number from 0 to 500.")
            changes[key] = value
        elif key == "redact_enabled":
            if not isinstance(value, bool):
                raise HTTPException(400, "redact_enabled must be true or false.")
            changes[key] = value
        elif key == "redact_kinds":
            if not isinstance(value, list) or not all(isinstance(v, str) and v in redact_mod.KINDS for v in value):
                raise HTTPException(400, f"redact_kinds must be a list of: {', '.join(redact_mod.KINDS)}.")
            changes[key] = [k for k in redact_mod.KINDS if k in value]
        elif key == "redact_names":
            if (not isinstance(value, list) or len(value) > redact_mod.MAX_NAMES
                    or not all(isinstance(v, str) and 2 <= len(v.strip()) <= 60 and v.isprintable() for v in value)):
                raise HTTPException(400, "redact_names must be a list of up to 50 words, each 2 to 60 printable characters.")
            changes[key] = list(dict.fromkeys(v.strip() for v in value))
        elif key in ("cloud_daily_cap", "claude_login_5h_soft_cap"):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000:
                raise HTTPException(400, f"{key} must be a whole number from 0 (no cap) to 100000.")
            changes[key] = value
        elif key == "provider_daily_caps":
            if (not isinstance(value, dict) or not all(k in usage_mod.PROVIDERS for k in value)
                    or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000 for v in value.values())):
                raise HTTPException(400, f"provider_daily_caps must map providers ({', '.join(usage_mod.PROVIDERS)}) to whole numbers (0 = no cap).")
            changes[key] = {k: v for k, v in value.items() if v}
        elif key in ("networking_enabled", "networking_ai", "interviews_enabled", "interviews_ai", "companyintel_enabled", "companyintel_ai", "offers_enabled", "proposals_enabled"):
            if not isinstance(value, bool):
                raise HTTPException(400, f"{key} must be true or false.")
            changes[key] = value
        elif key == "networking_nudge_days":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 365:
                raise HTTPException(400, "networking_nudge_days must be a whole number from 1 to 365.")
        elif key == "jobtrack_followup_days":
            if (not isinstance(value, list) or len(value) > 4
                    or not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 120 for v in value)):
                raise HTTPException(400, "jobtrack_followup_days must be a list of up to 4 whole numbers from 1 to 120 (days after applying), or [] for none.")
            changes[key] = sorted(set(value))
        elif key in ("outreach_followup_days", "outreach_gap_seconds"):
            low, high = (1, 60) if key == "outreach_followup_days" else (0, 1800)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise HTTPException(400, f"{key} must be a whole number from {low} to {high}.")
            changes[key] = value
        elif key in ("jobdigest_enabled", "jobdigest_notify", "jobdigest_nudges"):
            if not isinstance(value, bool):
                raise HTTPException(400, f"{key} must be true or false.")
            changes[key] = value
        elif key in ("jobdigest_hour", "jobdigest_weekly_day", "jobdigest_nudge_days"):
            low, high = {"jobdigest_hour": (0, 23), "jobdigest_weekly_day": (0, 6), "jobdigest_nudge_days": (2, 60)}[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise HTTPException(400, f"{key} must be a whole number from {low} to {high}.")
            changes[key] = value
        elif key == "jobtrack_ghost_days":
            if isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 120:
                raise HTTPException(400, "jobtrack_ghost_days must be a whole number from 3 to 120.")
            changes[key] = value
        elif key in ("batches_catchup_min", "batches_outreach_cap"):
            high = 1440 if key == "batches_catchup_min" else 200
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
                raise HTTPException(400, f"{key} must be a whole number from 0 to {high}.")
            changes[key] = value
        elif key == "applyq_daily_cap":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 200:
                raise HTTPException(400, "applyq_daily_cap must be a whole number from 0 to 200.")
            changes[key] = value
        elif key == "vision_daily_calls":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 500:
                raise HTTPException(400, "vision_daily_calls must be a whole number from 0 to 500.")
            changes[key] = value
        elif key in ("research_max_hops", "research_max_sources", "research_daily_ai_calls"):
            low, high = {"research_max_hops": (1, 5), "research_max_sources": (1, 20), "research_daily_ai_calls": (0, 500)}[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise HTTPException(400, f"{key} must be a whole number from {low} to {high}.")
            changes[key] = value
        elif key in ("kb_roots", "writable_roots"):
            changes[key] = _clean_roots(value, key, config.roots)
        elif key == "display_name":
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40 or not value.isprintable():
                raise HTTPException(400, "display_name must be 1 to 40 printable characters.")
            changes[key] = value.strip()
        elif key == "ntfy_server":
            changes[key] = _clean_ntfy_server(value)
        elif key == "ntfy_topic":
            if not isinstance(value, str) or (value and not TOPIC.match(value)):
                raise HTTPException(400, "ntfy_topic must be 8 to 64 letters, digits, - or _ (or empty to switch push off).")
            changes[key] = value
    if not changes:
        raise HTTPException(400, "Send the settings you want to change.")
    added = [r for r in changes.get("writable_roots", []) if r not in config.writable_roots]
    for name in added:
        if not Path(config.roots[name]).is_dir():
            raise HTTPException(400, f"The folder for '{name}' does not exist on the laptop.")
    return changes, confirm


# ---- the audit log tail -------------------------------------------------------------------------------------

def tail_lines(path: Path, end: int | None = None, max_bytes: int = MAX_AUDIT_SCAN_BYTES, chunk: int = AUDIT_CHUNK):
    """Lines of a file from the end backwards as (offset, bytes), reading chunks, never the whole file.

    `end` starts the scan before that byte offset (a cursor). Stops after `max_bytes` have been read; a line cut
    by that limit is dropped. Sets `.truncated` on the returned generator-owner via the final item (see caller)."""
    try:
        handle = open(path, "rb")
    except OSError:
        return
    with handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        pos = size if end is None else max(0, min(end, size))
        scanned, carry = 0, b""
        while pos > 0 and scanned < max_bytes:
            step = min(chunk, pos, max_bytes - scanned)
            pos -= step
            scanned += step
            handle.seek(pos)
            data = handle.read(step) + carry
            parts = data.split(b"\n")
            carry = parts[0]
            offset = pos + len(carry) + 1
            starts = []
            for part in parts[1:]:
                starts.append((offset, part))
                offset += len(part) + 1
            for item in reversed(starts):
                yield item
        if pos == 0 and carry:
            yield (0, carry)
        elif pos > 0:
            yield (-1, b"")   # marker: stopped because of the read limit


def read_audit(path: Path | None, limit: int, q: str = "", event: str = "", before: str = "") -> dict:
    limit = max(1, min(int(limit), MAX_AUDIT_ROWS))
    if path is None:
        return {"rows": [], "count": 0, "next_before": None, "truncated": False}
    needle = q.strip().lower()
    cursor: int | None = None
    before_time = ""
    if before:
        if before.isdigit():
            cursor = int(before)
        else:
            before_time = before[:40]
    rows: list[dict] = []
    truncated = False
    more = False
    for offset, raw in tail_lines(path, cursor):
        if offset == -1:
            truncated = True
            break
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("event"), str):
            continue
        if event and row["event"] != event:
            continue
        if before_time and not str(row.get("time", "")) < before_time:
            continue
        if needle and needle not in text.lower():
            continue
        if len(rows) >= limit:
            more = True
            break
        row = {k: (v[:MAX_FIELD_CHARS] + "..." if isinstance(v, str) and len(v) > MAX_FIELD_CHARS else v)
               for k, v in row.items()}
        row["pos"] = offset
        rows.append(row)
    return {"rows": rows, "count": len(rows), "next_before": rows[-1]["pos"] if (rows and more) else None,
            "truncated": truncated}


def audit_events(path: Path | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if path is None:
        return counts
    for offset, raw in tail_lines(path, max_bytes=2 * 1024 * 1024):
        if offset == -1:
            break
        try:
            row = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("event"), str) and len(row["event"]) <= 80:
            counts[row["event"]] = counts.get(row["event"], 0) + 1
    return counts


# ---- backups ------------------------------------------------------------------------------------------------

def make_backup(config, audit_path: Path | None, config_path: Path | None, now=time.time) -> dict:
    """Zip the data folder, the notes folder and the audit log. hub.json and anything holding the token hash or
    session secret is left out. Keeps the newest KEEP_BACKUPS archives."""
    data = config.data_path()
    out_dir = data / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_resolved = out_dir.resolve()
    markers = [m.encode() for m in (config.token_hash, config.session_secret.hex()) if m]
    skip_paths = set()
    for p in (config_path, cfg.default_path()):
        try:
            if p:
                skip_paths.add(Path(p).resolve())
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    name, n = f"hub-backup-{stamp}.zip", 1
    while (out_dir / name).exists():
        n += 1
        name = f"hub-backup-{stamp}-{n}.zip"
    part = out_dir / (name + ".part")
    seen: set[Path] = set()
    stats = {"files": 0, "excluded": 0, "bytes": 0}

    def add(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                return
            resolved = path.resolve()
            if resolved in seen:
                return
            seen.add(resolved)
            if (path.name == "hub.json" or resolved in skip_paths or path.suffix in (".tmp", ".part")):
                stats["excluded"] += 1
                return
            size = path.stat().st_size
            if size <= SECRET_SCAN_BYTES:
                blob = path.read_bytes()
                if any(m in blob for m in markers):
                    stats["excluded"] += 1
                    return
            stats["bytes"] += size
            if stats["bytes"] > MAX_BACKUP_BYTES:
                raise HTTPException(413, "Your data is over 2 GB, which is too big for a one-tap backup.")
            archive.write(path, arcname)
            stats["files"] += 1
        except OSError:
            return   # a file that vanished or is locked is skipped

    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as archive:
            for label, base in (("data", data), ("notes", Path(config.notes_dir))):
                if not base.is_dir():
                    continue
                for folder, dirs, names in os.walk(base, followlinks=False):
                    dirs[:] = [d for d in dirs
                               if not Path(folder, d).is_symlink() and Path(folder, d).resolve() != out_resolved]
                    for fname in names:
                        full = Path(folder, fname)
                        add(archive, full, f"{label}/{full.relative_to(base).as_posix()}")
            if audit_path:
                add(archive, Path(audit_path), "audit/audit.log")
            archive.writestr("MANIFEST.json", json.dumps(
                {"created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now())), "files": stats["files"],
                 "version": _version()}))
        os.replace(part, out_dir / name)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    for old in list_backups(out_dir)[KEEP_BACKUPS:]:
        try:
            (out_dir / old["name"]).unlink()
        except OSError:
            pass
    return {"name": name, "size": (out_dir / name).stat().st_size, "files": stats["files"],
            "excluded": stats["excluded"]}


def list_backups(out_dir: Path) -> list[dict]:
    """Newest first."""
    found = []
    if out_dir.is_dir():
        for path in out_dir.iterdir():
            if BACKUP_NAME.match(path.name) and path.is_file() and not path.is_symlink():
                st = path.stat()
                found.append((st.st_mtime_ns, path.name, st))
    found.sort(reverse=True)
    return [{"name": name, "size": st.st_size,
             "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))} for _, name, st in found]


# ---- the router -------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, transport: httpx.AsyncBaseTransport | None = None) -> APIRouter:
    """`transport` lets tests answer Ollama's requests without a network."""
    router = APIRouter(prefix="/api/settings", dependencies=[Depends(ctx.need_auth)])
    config = ctx.config
    lock = threading.Lock()
    backup_lock = threading.Lock()

    def audit_path() -> Path | None:
        return Path(ctx.audit_path) if getattr(ctx, "audit_path", None) else None

    def config_file() -> Path | None:
        """Where hub.json is, or None when we cannot be sure it is this hub's file."""
        given = getattr(ctx, "config_path", None)
        if given:
            return Path(given)
        guess = cfg.default_path()
        try:
            if guess.is_file() and cfg.load(guess).token_hash == config.token_hash:
                return guess
        except cfg.ConfigError:
            pass
        return None

    def ollama(timeout: Any = OLLAMA_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=config.ollama_host, timeout=timeout, transport=transport)

    async def installed_models() -> tuple[bool, list[dict], str]:
        """(reachable, models, a friendly problem sentence)."""
        try:
            async with ollama() as client:
                response = await client.get("/api/tags")
            response.raise_for_status()
            items = response.json().get("models") or []
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, AttributeError):
            return False, [], "Ollama is not running. Start the Ollama app on the laptop."
        models = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("model") or item.get("name") or "")
            if not name:
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            models.append({"name": name, "size_mb": round(int(item.get("size") or 0) / 1e6),
                           "modified": str(item.get("modified_at") or "")[:19],
                           "params": str(details.get("parameter_size") or "")})
        return True, models, ""

    def snapshot() -> dict:
        return {
            "chat_model": config.chat_model, "assistant_model": config.assistant_model,
            "claude_model": config.claude_model, "whisper_model": config.whisper_model,
            "embed_model": config.embed_model,
            "kb_roots": list(config.kb_roots), "writable_roots": list(config.writable_roots),
            "roots": [{"name": n, "exists": Path(p).is_dir()} for n, p in config.roots.items()],
            "display_name": config.display_name, "jarvis_name": config.jarvis_name,
            "full_control": config.full_control is True, "fast_commands": config.fast_commands is True,
            "panic_active": config.panic_active is True, "panic_confirm": config.panic_confirm is True,
            "trust_enabled": config.trust_enabled is not False, "trust_threshold": int(config.trust_threshold or 10),
            "ntfy_server": config.ntfy_server, "ntfy_topic_set": bool(config.ntfy_topic),
            "jarvis_dir_set": bool((config.jarvis_dir or "").strip()),
            "council_dir_set": bool((config.council_dir or "").strip()),
            "shells": list(config.shell_table()), "apps": list(config.apps), "scripts": list(config.scripts),
            "has_claude_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "default_provider": config.default_provider, "gemini_model": config.gemini_model,
            "duo_verify": config.duo_verify, "duo_max_claude_per_hour": config.duo_max_claude_per_hour,
            "claude_cli_model": config.claude_cli_model,
            "autopilot_enabled": config.autopilot_enabled, "autopilot_sweep_min": config.autopilot_sweep_min,
            "autopilot_daily_ai_calls": config.autopilot_daily_ai_calls,
            "autopilot_research": config.autopilot_research, "today_enabled": config.today_enabled,
            "today_evening_time": config.today_evening_time, "today_cache_min": config.today_cache_min, "chat_history_enabled": config.chat_history_enabled,
            "chat_history_days": config.chat_history_days, "skills_enabled": config.skills_enabled,
            "research_enabled": config.research_enabled, "research_max_hops": config.research_max_hops,
            "research_max_sources": config.research_max_sources,
            "research_daily_ai_calls": config.research_daily_ai_calls,
            "vision_enabled": config.vision_enabled, "vision_daily_calls": config.vision_daily_calls,
            "brain_enabled": config.brain_enabled, "brain_context": config.brain_context,
            "today_morning_time": config.today_morning_time, "proactive_enabled": config.proactive_enabled,
            "proactive_quiet_start": config.proactive_quiet_start, "proactive_quiet_end": config.proactive_quiet_end,
            "proactive_batch_minutes": config.proactive_batch_minutes,
            "buildflow_enabled": config.buildflow_enabled,
            "opps_enabled": config.opps_enabled is True, "opps_interval_min": config.opps_interval_min,
            "opps_min_score": config.opps_min_score,
            "oppsources_enabled": config.oppsources_enabled is True, "oppsources_interval_min": config.oppsources_interval_min,
            "oppsources_watch_interval_min": config.oppsources_watch_interval_min,
            "oppsources_stale_days": config.oppsources_stale_days, "oppsources_ghost_days": config.oppsources_ghost_days, "triage_ai": config.triage_ai is True,
            "applyq_enabled": config.applyq_enabled, "applyq_daily_cap": config.applyq_daily_cap,
            "batches_enabled": config.batches_enabled is not False, "batches_catchup_min": config.batches_catchup_min,
            "batches_outreach_cap": config.batches_outreach_cap,
            "chatch_enabled": config.chatch_enabled is not False, "chatch_email_parse": config.chatch_email_parse is not False,
            "networking_enabled": config.networking_enabled is not False, "networking_nudge_days": config.networking_nudge_days,
            "networking_ai": config.networking_ai is True, "interviews_enabled": config.interviews_enabled is not False,
            "interviews_ai": config.interviews_ai is True,
            "companyintel_enabled": config.companyintel_enabled is not False, "companyintel_ai": config.companyintel_ai is True,
            "offers_enabled": config.offers_enabled is not False, "proposals_enabled": config.proposals_enabled is not False,
            "jobtrack_enabled": config.jobtrack_enabled is not False, "jobtrack_followup_days": config.jobtrack_followup_days,
            "jobtrack_ghost_days": config.jobtrack_ghost_days,
            "sharein_enabled": config.sharein_enabled is not False,
            "replies_enabled": config.replies_enabled is not False, "replies_ai": config.replies_ai is True,
            "jobinsights_enabled": config.jobinsights_enabled is not False, "outreach_enabled": config.outreach_enabled is not False,
            "outreach_followup_days": config.outreach_followup_days, "outreach_gap_seconds": config.outreach_gap_seconds,
            "jobdigest_enabled": config.jobdigest_enabled is not False, "jobdigest_hour": config.jobdigest_hour, "jobdigest_notify": config.jobdigest_notify is not False,
            "jobdigest_weekly_day": config.jobdigest_weekly_day, "jobdigest_nudge_days": config.jobdigest_nudge_days, "jobdigest_nudges": config.jobdigest_nudges is not False,
            "tailor_enabled": config.tailor_enabled is not False, "tailor_ai": config.tailor_ai is True,
            "answerbank_enabled": config.answerbank_enabled is not False, "answerbank_min_confidence": config.answerbank_min_confidence,
            "answerbank_embeddings": config.answerbank_embeddings is True,
            "wake_laptop_enabled": config.wake_laptop_enabled, "wake_laptop_device": config.wake_laptop_device,
            "redact_enabled": config.redact_enabled is not False, "redact_kinds": list(config.redact_kinds),
            "redact_kinds_all": list(redact_mod.KINDS), "redact_names": list(config.redact_names),
            "cloud_daily_cap": config.cloud_daily_cap, "provider_daily_caps": dict(config.provider_daily_caps),
            "claude_login_5h_soft_cap": config.claude_login_5h_soft_cap,
            "gmail_company_ai_allowed": config.gmail_company_ai_allowed is True,
            "local_reasoning_model": config.local_reasoning_model, "deep_think_timeout_min": config.deep_think_timeout_min,
            "deep_think_max_chars": config.deep_think_max_chars,
            "providers": providers.availability(config),
            "resolved_provider": providers.resolve_default(config),
            "version": _version(), "data_dir_exists": config.data_path().is_dir(),
            "platform": sys.platform, "locked": list(LOCKED_KEYS), "editable": list(ALLOWED_KEYS),
        }

    async def full_snapshot() -> dict:
        info = snapshot()
        reachable, models, _ = await installed_models()
        info["ollama"] = {"reachable": reachable, "models": [m["name"] for m in models]}
        info["config_persistable"] = config_file() is not None
        return info

    # ---- settings ----------------------------------------------------------------------------------------------

    @router.get("")
    @router.get("/")
    async def get_settings():
        return await full_snapshot()

    @router.patch("")
    @router.patch("/")
    async def patch_settings(body: Any = Body(...)):
        changes, confirmed = validate_patch(config, body)
        added = [r for r in changes.get("writable_roots", []) if r not in config.writable_roots]
        if added and not confirmed:
            raise HTTPException(409, f"Letting the phone change files in {', '.join(added)} needs your OK. "
                                     "Confirm to continue.")
        persisted, note = False, ""
        with lock:
            old = {k: getattr(config, k) for k in changes}
            path = config_file()
            if path is not None:
                try:
                    disk = cfg.load(path)
                    for key, value in changes.items():
                        setattr(disk, key, value)
                    cfg.save(disk, path)
                    persisted = True
                except cfg.ConfigError:
                    note = "The settings file on the laptop could not be read, so this change lasts until the hub restarts."
                except OSError:
                    note = "The settings file could not be written, so this change lasts until the hub restarts."
            else:
                note = "The settings file could not be found, so this change lasts until the hub restarts."
            for key, value in changes.items():
                setattr(config, key, value)
        shown = {k: ({"changed": True} if k in SECRET_KEYS else {"old": old[k], "new": changes[k]})
                 for k in changes if old[k] != changes[k]}
        ctx.audit("settings_change", changes=shown, persisted=persisted)
        if "full_control" in changes and old.get("full_control") != changes["full_control"]:
            try:        # remembered approvals never outlive a change of Full Control (docs/api/assistant-safety.md)
                ctx.get_app().state.assistant.revoke_all()
            except Exception:  # noqa: BLE001  tests and doubles may have no assistant
                pass
            try:        # a Full Control change also revokes every trust rule (hub/trust.py)
                ctx.trust.revoke_all("full_control_change")
            except Exception:  # noqa: BLE001
                pass
        info = await full_snapshot()
        info["persisted"] = persisted
        if note:
            info["note"] = note
        return info

    # ---- models --------------------------------------------------------------------------------------------------

    @router.get("/models")
    async def list_models():
        reachable, models, problem = await installed_models()
        names = {m["name"] for m in models}

        def has(wanted: str) -> bool:
            return wanted in names or f"{wanted}:latest" in names

        configured = {key: {"name": getattr(config, key), "installed": has(getattr(config, key)) if reachable else None}
                      for key in ("chat_model", "embed_model")}
        out = {"reachable": reachable, "models": models, "configured": configured}
        if problem:
            out["error"] = problem
        return out

    @router.post("/models/pull")
    async def pull_model(body: PullBody):
        name = body.name.strip()
        if not valid_model_name(name):
            raise HTTPException(400, "That is not a model name. Use something like qwen3:4b.")
        if not body.confirm:
            raise HTTPException(409, f"Downloading {name} can take several GB and a while. Confirm to start.")
        ctx.audit("model_pull", name=name)

        async def events():
            ok = False
            try:
                async with ollama(httpx.Timeout(None, connect=OLLAMA_TIMEOUT)) as client:
                    async with client.stream("POST", "/api/pull",
                                             json={"model": name, "name": name, "stream": True}) as response:
                        if response.status_code >= 400:
                            yield {"error": f"Ollama could not download {name} (it answered {response.status_code})."}
                        else:
                            async for line in response.aiter_lines():
                                if not line.strip():
                                    continue
                                try:
                                    item = json.loads(line)
                                except ValueError:
                                    continue
                                if not isinstance(item, dict):
                                    continue
                                if item.get("error"):
                                    yield {"error": str(item["error"])[:300]}
                                    break
                                total, done = item.get("total"), item.get("completed")
                                event = {"status": str(item.get("status", ""))[:80]}
                                if isinstance(total, int) and isinstance(done, int) and total > 0:
                                    event["percent"] = round(min(done / total, 1.0) * 100, 1)
                                yield event
                                if event["status"] == "success":
                                    ok = True
            except (httpx.HTTPError, httpx.InvalidURL):
                yield {"error": "Ollama is not running. Start the Ollama app on the laptop."}
            ctx.audit("model_pull_done", name=name, ok=ok)

        async def stream():
            async for event in events():
                yield f"data: {json.dumps(event)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.post("/models/test")
    async def test_model(body: ModelTestBody):
        name = body.model.strip()
        if not valid_model_name(name):
            raise HTTPException(400, "That is not a model name. Use something like qwen3:4b.")
        started = time.perf_counter()
        try:
            async with ollama(60.0) as client:
                response = await client.post("/api/generate", json={
                    "model": name, "prompt": "Reply with the single word: ready", "stream": False,
                    "options": {"num_predict": 8}})
            if response.status_code == 404:
                return {"ok": False, "model": name, "error": f"{name} is not installed. Download it first."}
            response.raise_for_status()
            reply = str(response.json().get("response", "")).strip()
        except httpx.TimeoutException:
            return {"ok": False, "model": name, "error": "The model took over a minute to answer."}
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            return {"ok": False, "model": name, "error": "Ollama is not running or could not answer."}
        return {"ok": True, "model": name, "latency_ms": round((time.perf_counter() - started) * 1000),
                "reply": reply[:80]}

    # ---- diagnostics ------------------------------------------------------------------------------------------------

    def writable_dir(path: Path) -> bool:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return probe.is_dir() and os.access(probe, os.W_OK)

    def local_checks() -> list[dict]:
        out: list[dict] = []

        def check(name: str, good: bool, detail: str, fix: str = "", required: bool = True) -> None:
            out.append({"name": name, "ok": bool(good), "detail": detail, "fix": "" if good else fix,
                        "level": "ok" if good else ("fail" if required else "warn")})

        path = config_file()
        check("Settings file", path is not None, "hub.json found." if path else "hub.json was not found by the settings page.",
              "Start the hub with `python -m hub serve` so it knows its settings file.", required=False)
        for name, folder in config.roots.items():
            check(f"Folder {name}", Path(folder).is_dir(), "Folder found." if Path(folder).is_dir() else "Folder is missing.",
                  f"Create the folder or fix roots.{name} in hub.json.")
        check("Notes folder", writable_dir(Path(config.notes_dir)), "Notes can be saved.", "Make sure notes_dir exists and is writable.")
        data = config.data_path()
        check("Data folder", writable_dir(data), "History and settings can be saved.",
              "Make sure data_dir exists and is writable.")
        ap = audit_path()
        if ap is not None:
            check("Audit log", writable_dir(ap.parent), "The audit log can be written.", "Make sure the audit log folder is writable.")
        try:
            free = shutil.disk_usage(data if data.exists() else Path.home()).free / 1e9
            check("Disk space", free >= 1, f"{free:.1f} GB free.", "Free up some space on the laptop disk.", required=False)
        except OSError:
            pass
        check("Voice notes (faster-whisper)", importlib.util.find_spec("faster_whisper") is not None,
              "faster-whisper installed.", "Run scripts\\setup-all.ps1 (or: pip install -r requirements-voice.txt)", required=False)
        avail = providers.availability(config)
        cloud = [avail[k]["label"] for k in ("claude", "claude_login", "gemini") if avail[k]["ready"]]
        check("AI providers", bool(cloud),
              "Ready: " + ", ".join(["Local model", *cloud]) + "." if cloud else "Only the local model is set up.",
              "Optional: run `python -m hub setup --only ai` to add Gemini or your Claude login.", required=False)
        for name, argv in config.shell_table().items():
            found = shutil.which(argv[0]) is not None
            check(f"Shell {name}", found, f"{argv[0]} found." if found else f"{argv[0]} not found.",
                  "Install it or fix shells in hub.json.", required=False)
        if sys.platform == "win32":
            has_pty = importlib.util.find_spec("winpty") is not None
            check("Windows terminal (pywinpty)", has_pty, "pywinpty installed.", "Run scripts\\setup-all.ps1 (or: pip install pywinpty)")
        has_ts = shutil.which("tailscale") is not None
        check("Tailscale", has_ts, "tailscale found." if has_ts else "tailscale not found.",
              "Install Tailscale (tailscale.com/download) for HTTPS and remote access.", required=False)
        if (config.jarvis_dir or "").strip():
            ok = (Path(config.jarvis_dir) / "jarvis.py").is_file()
            check("Jarvis folder", ok, "jarvis.py found." if ok else "jarvis.py not found in the Jarvis folder.",
                  "Point jarvis_dir in hub.json at your jarvis-voice-assistant folder.", required=False)
        if (config.council_dir or "").strip():
            ok = (Path(config.council_dir) / "lab").is_dir()
            check("Council (multi-llm-lab)", ok, "multi-llm-lab found." if ok else "No 'lab' folder in the council folder.",
                  "Run `python -m hub setup --only council`, or point council_dir in hub.json at the multi-llm-lab folder.",
                  required=False)
        return out

    @router.get("/diagnostics")
    async def diagnostics():
        checks = await asyncio.to_thread(local_checks)
        reachable, models, problem = await installed_models()
        names = {m["name"] for m in models}
        extra = [{"name": "Ollama", "ok": reachable,
                  "detail": f"Running, {len(models)} model{'s' if len(models) != 1 else ''}." if reachable else problem,
                  "fix": "" if reachable else "Start the Ollama app (needed for Explain, Ask and tags).",
                  "level": "ok" if reachable else "fail"}]
        if reachable:
            wanted_models = [("Chat model", config.chat_model, True), ("Search model", config.embed_model, False)]
            if (config.local_tool_model or "").strip():
                wanted_models.append(("Tool model", config.local_tool_model.strip(), False))
            for label, wanted, required in wanted_models:
                have = wanted in names or f"{wanted}:latest" in names
                extra.append({"name": label, "ok": have, "detail": f"{wanted} is installed." if have else f"{wanted} is missing.",
                              "fix": "" if have else f"Download it in Settings, or run: ollama pull {wanted}",
                              "level": "ok" if have else ("fail" if required else "warn")})
        return [checks[0], *extra, *checks[1:]] if checks else extra

    # ---- audit viewer --------------------------------------------------------------------------------------------------

    @router.get("/audit")
    async def audit_view(limit: int = Query(100, ge=1, le=10000), q: str = Query("", max_length=200),
                         event: str = Query("", max_length=80), before: str = Query("", max_length=40)):
        return await asyncio.to_thread(read_audit, audit_path(), min(limit, MAX_AUDIT_ROWS), q, event, before)

    @router.get("/audit/events")
    async def audit_event_names():
        counts = await asyncio.to_thread(audit_events, audit_path())
        return {"events": sorted(counts), "counts": counts}

    # ---- backups ------------------------------------------------------------------------------------------------------------

    def _backup_event(ok: bool, size: int) -> None:
        try:
            from . import backup as backup_mod
            from .proactive import submit_or_notify
            submit_or_notify(ctx, backup_mod.proactive_event(ok, size))
        except Exception:  # noqa: BLE001 - telling the owner must never change the backup's result
            pass

    @router.post("/backup")
    async def create_backup(body: BackupBody):
        if not body.confirm:
            raise HTTPException(409, "A backup copies your notes and history into one file on the laptop. Confirm to continue.")
        if not backup_lock.acquire(blocking=False):
            raise HTTPException(409, "A backup is already running.")
        try:
            result = await asyncio.to_thread(make_backup, config, audit_path(), config_file())
        except OSError:
            _backup_event(False, 0)
            raise HTTPException(500, "The backup could not be written. Check there is free space on the laptop.") from None
        finally:
            backup_lock.release()
        _backup_event(True, result.get("size") or 0)
        ctx.audit("backup_create", name=result["name"], size=result["size"], files=result["files"])
        return result

    @router.get("/backups")
    def backups():
        return {"backups": list_backups(config.data_path() / "backups")}

    @router.get("/backups/{name}")
    def download_backup(name: str):
        if not BACKUP_NAME.match(name):
            raise HTTPException(400, "That is not a backup name.")
        path = config.data_path() / "backups" / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "No such backup.")
        ctx.audit("backup_download", name=name)
        return FileResponse(path, media_type="application/zip", filename=name, content_disposition_type="attachment")

    # ---- about ---------------------------------------------------------------------------------------------------------------

    @router.get("/about")
    def about():
        return {"version": _version(), "uptime_s": int(time.time() - STARTED),
                "python": platform.python_version(), "platform": platform.platform()}

    return router
