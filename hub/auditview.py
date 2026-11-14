"""The audit screen's backend: a readable, filterable view over the existing audit log (docs/api/audit.md).

`GET /api/audit/search?since=&until=&kind=&level=&outcome=&q=&limit=&before=&format=` reads the log from the end (the file
is JSON lines, written by server.audit), skips damaged lines, turns each event into {time, event, kind, level, outcome, by,
summary, detail} and pages with a byte-offset cursor. Secrets never leave: sensitive keys are hidden, secret-shaped text is
scrubbed, and a `redaction` event shows its counts only. `format=csv` returns the same rows as a CSV file.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from .common import Ctx
from .settings import AUDIT_CHUNK, MAX_AUDIT_SCAN_BYTES, tail_lines

MAX_ROWS = 500
MAX_CSV_ROWS = 5000
FIELD_CHARS = 400
KINDS = ("tool", "approval", "denial", "n3", "outbound", "mcp", "panic", "trust", "setting", "login", "redaction", "other")
OUTCOMES = ("ran", "approved", "denied", "blocked", "asked", "cancelled", "failed", "info")
SENSITIVE_KEY = re.compile(r"token|secret|passw|passcode|api[_-]?key|apikey|cookie|authorization|bearer|credential|topic|private|session_secret|hash",
                           re.I)
SECRET_TEXT = re.compile(
    r"(?i)\b(?:authorization|bearer|token|password|passwd|secret|api[_-]?key|apikey|cookie)\b\s*[=:]?\s*(?:(?:bearer|basic|token)\s+)?[\"']?[^\s\"',;]{4,}"
    r"|\bsk-[A-Za-z0-9_\-]{12,}|\bAIza[0-9A-Za-z_\-]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|\b[A-Fa-f0-9]{40,}\b|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*")
OUTBOUND = {"work_email_send", "gmail_send", "applyq_send", "post_publish", "post_marked_posted", "work_chat_send", "formfill_submit",
            "ccsessions_send", "ntfy_push", "autopilot_execute", "jarvis_say"}
OUTBOUND_TOOLS = {"send_email", "send_application", "post_publish", "submit_form"}
SETTING_EVENTS = {"settings_change", "safety_rules_update", "redact_rules_update", "dictation_settings", "alerts_settings",
                  "friday_settings", "posts_settings", "watch_settings", "life_capture_settings", "notify_settings_changed",
                  "notify_prefs_update", "notify_prefs_reset", "wakeword_toggle", "dictation_toggle"}


def kind_of(event: str, row: dict) -> str:
    if event in ("assistant_approval",):
        return "approval" if row.get("approve") is True else "denial"
    if event.startswith("assistant_n3"):
        return "n3"
    if event in ("assistant_tool", "assistant_fast_command"):
        return "outbound" if row.get("tool") in OUTBOUND_TOOLS else "tool"
    if event.startswith("panic_") or event in ("assistant_panic_block", "assistant_stop", "assistant_stop_request"):
        return "panic"
    if event.startswith("trust_") or event.startswith("assistant_grant"):
        return "trust"
    if event.startswith("mcp_"):
        return "mcp"
    if event in OUTBOUND:
        return "outbound"
    if event in SETTING_EVENTS or event.endswith("_settings"):
        return "setting"
    if event in ("login", "login_failed", "login_blocked", "logout_all"):
        return "login"
    if event in ("redaction",):
        return "redaction"
    if event in ("assistant_taint_block", "assistant_custom_rule"):
        return "denial" if event == "assistant_taint_block" else "tool"
    return "other"


def outcome_of(event: str, row: dict) -> str:
    if event == "assistant_approval":
        return "approved" if row.get("approve") is True else "denied"
    return {"assistant_tool": "ran", "assistant_fast_command": "ran", "assistant_taint_block": "blocked",
            "assistant_panic_block": "blocked", "assistant_n3_attempt": "asked", "assistant_n3_confirmed": "approved",
            "assistant_n3_cancelled": "cancelled", "assistant_n3_refused": "blocked", "login_failed": "failed",
            "login_blocked": "blocked", "mcp_start_failed": "failed", "assistant_cancel": "cancelled",
            "assistant_grant_revoked": "info", "panic_activated": "ran", "panic_resumed": "ran"}.get(event, "info")


def approver_of(event: str, row: dict) -> str:
    if event == "assistant_approval":
        return "voice" if row.get("voice") else "tap"
    if event in ("assistant_tool", "assistant_fast_command"):
        if row.get("trust_rule"):
            return "trust rule"
        if row.get("full_control"):
            return "full control"
        if row.get("remembered"):
            return "remembered approval"
        by = row.get("by")
        if by in ("tap", "voice", "auto_approve"):
            return {"auto_approve": "auto-approve"}.get(by, by)
        if row.get("auto"):
            return "auto-approve"
        return "" if row.get("level") in (None, "N1") else "tap"
    if event == "assistant_n3_confirmed":
        return "typed CONFIRM"
    return ""


def scrub(value: Any, key: str = "", depth: int = 0) -> Any:
    """Hide sensitive keys, scrub secret-shaped text, shorten long strings. Numbers and booleans pass."""
    if key and SENSITIVE_KEY.search(key) and value not in (None, True, False, "", 0):
        return "[hidden]"
    if depth > 4:
        return "..."
    if isinstance(value, str):
        text = SECRET_TEXT.sub("[hidden]", value)
        return text if len(text) <= FIELD_CHARS else text[:FIELD_CHARS] + "..."
    if isinstance(value, dict):
        return {str(k)[:60]: scrub(v, str(k), depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [scrub(v, key, depth + 1) for v in list(value)[:40]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:FIELD_CHARS]


def summary_of(event: str, row: dict) -> str:
    tool = row.get("tool")
    if event == "assistant_tool":
        what = row.get("input")
        extra = ""
        if isinstance(what, dict):
            extra = SECRET_TEXT.sub("[hidden]", str(what.get("command") or what.get("path") or what.get("name") or ""))[:90]
        return f"{tool}{(': ' + extra) if extra else ''}"
    if event == "assistant_approval":
        return f"{'Approved' if row.get('approve') else 'Denied'} a request" + (" (typed CONFIRM)" if row.get("confirm") else "")
    if event.startswith("assistant_n3"):
        return f"Critical action {event.rsplit('_', 1)[-1]}: {tool or ''} {str(row.get('reason') or '')[:80]}".strip()
    if event == "redaction":
        counts = row.get("counts")
        total = sum(v for v in counts.values() if isinstance(v, int)) if isinstance(counts, dict) else 0
        return f"{total} private detail(s) hidden before the cloud"
    if event == "settings_change":
        changes = row.get("changes")
        return "Changed " + ", ".join(sorted(changes)[:6]) if isinstance(changes, dict) and changes else "Settings changed"
    if event == "panic_activated":
        return "PANIC: stopped " + ", ".join(row.get("stopped") or []) if row.get("stopped") else "PANIC pressed"
    if event == "login":
        return "Logged in"
    if tool:
        return f"{event.replace('_', ' ')}: {tool}"
    return event.replace("_", " ")


def normalise(row: dict, pos: int) -> dict:
    event = str(row["event"])[:80]
    detail = {k: v for k, v in row.items() if k not in ("time", "event")}
    if event == "redaction":                        # counts only, whatever else the line holds
        detail = {"counts": detail.get("counts") if isinstance(detail.get("counts"), dict) else {}}
    detail = scrub(detail)
    return {"pos": pos, "time": str(row.get("time", ""))[:30], "event": event, "kind": kind_of(event, row),
            "level": row.get("level") if row.get("level") in ("N1", "N2", "N3") else "",
            "outcome": outcome_of(event, row), "by": approver_of(event, row),
            "summary": scrub(summary_of(event, row)), "detail": detail}


def parse_when(value: str, end: bool = False) -> str:
    """'2026-11-14' or '2026-11-14T10:00' (or 'today', '7d') as a string comparable with the log's time stamps."""
    value = (value or "").strip()
    if not value:
        return ""
    if value == "today":
        value = time.strftime("%Y-%m-%d")
    m = re.fullmatch(r"(\d{1,3})d", value)
    if m:
        value = time.strftime("%Y-%m-%d", time.localtime(time.time() - int(m.group(1)) * 86400))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}(?::\d{2}(?::\d{2})?)?)?", value):
        raise ValueError("since and until must look like 2026-11-14, 2026-11-14T10:30, today or 7d.")
    value = value.replace(" ", "T")
    return value + ("￿" if end and len(value) == 10 else "")


def search(path: Path | None, *, since: str = "", until: str = "", kind: str = "", level: str = "", outcome: str = "",
           q: str = "", limit: int = 100, before: str = "") -> dict:
    limit = max(1, min(int(limit), MAX_CSV_ROWS))
    out: dict[str, Any] = {"rows": [], "count": 0, "next_before": None, "truncated": False, "skipped": 0, "kinds": list(KINDS)}
    if path is None:
        return out
    kinds = {k for k in kind.split(",") if k}
    levels = {v.upper() for v in level.split(",") if v}
    outcomes = {o for o in outcome.split(",") if o}
    needle = q.strip().lower()
    cursor = int(before) if before.isdigit() else None
    more = False
    for offset, raw in tail_lines(path, cursor, MAX_AUDIT_SCAN_BYTES, AUDIT_CHUNK):
        if offset == -1:
            out["truncated"] = True
            break
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            out["skipped"] += 1                     # a damaged line is skipped, never an error
            continue
        if not isinstance(row, dict) or not isinstance(row.get("event"), str):
            out["skipped"] += 1
            continue
        when = str(row.get("time", ""))
        if since and when < since:
            break                                   # the log is in time order: everything further back is older
        if until and when > until:
            continue
        item = normalise(row, offset)
        if kinds and item["kind"] not in kinds:
            continue
        if levels and item["level"] not in levels:
            continue
        if outcomes and item["outcome"] not in outcomes:
            continue
        if needle and needle not in (item["summary"] + " " + item["event"] + " " + json.dumps(item["detail"], default=str)).lower():
            continue
        if len(out["rows"]) >= limit:
            more = True
            break
        out["rows"].append(item)
    out["count"] = len(out["rows"])
    if more and out["rows"]:
        out["next_before"] = out["rows"][-1]["pos"]
    return out


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value if value is not None else "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text      # a spreadsheet must not run a cell


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["time", "kind", "event", "level", "outcome", "approved_by", "summary", "detail"])
    for r in rows:
        writer.writerow([_cell(r["time"]), r["kind"], _cell(r["event"]), r["level"], r["outcome"], r["by"], _cell(r["summary"]),
                         _cell(r["detail"])])
    return buf.getvalue()


def make_router(ctx: Ctx) -> APIRouter:
    router = APIRouter(prefix="/api/audit", dependencies=[Depends(ctx.need_auth)])

    @router.get("/search")
    async def audit_search(since: str = Query("", max_length=40), until: str = Query("", max_length=40),
                           kind: str = Query("", max_length=200), level: str = Query("", max_length=20),
                           outcome: str = Query("", max_length=100), q: str = Query("", max_length=200),
                           limit: int = Query(100, ge=1, le=MAX_CSV_ROWS), before: str = Query("", max_length=20),
                           format: str = Query("json", pattern="^(json|csv)$")):
        try:
            lo, hi = parse_when(since), parse_when(until, end=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        csv_out = format == "csv"
        path = Path(ctx.audit_path) if getattr(ctx, "audit_path", None) else None
        result = await asyncio.to_thread(lambda: search(path, since=lo, until=hi, kind=kind, level=level, outcome=outcome, q=q,
                                                       limit=min(limit, MAX_CSV_ROWS if csv_out else MAX_ROWS), before=before))
        if csv_out:
            ctx.audit("audit_export", rows=result["count"])
            return Response(to_csv(result["rows"]), media_type="text/csv",
                            headers={"Content-Disposition": 'attachment; filename="redline-audit.csv"'})
        return result

    return router
