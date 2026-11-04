"""AUTOMATIONS: named routines ("Good morning", "Leaving desk", "Study mode") that chain steps.

A routine is a list of steps run one after another, on demand or on a schedule. Every step goes through the
hub's own routes (InProcessClient), exactly as the phone would call them, so each route's own checks and
audit entries still apply. Routes are under /api/automations (see docs/api/automations.md).

Safety rules (all enforced here, none left to the UI):
- Steps come from a fixed registry (STEP_TYPES); every field is validated; unknown types are refused.
- A step that locks, sleeps, opens a path, ends a program, restarts or shuts down (anything the laptop registry
  marks "confirm") only lives in a routine that was saved with `confirm: true` (stored as confirmed_dangerous).
- Shut down, restart and end-a-program are never scheduled: refused when saving a schedule, refused again by
  the scheduler, and a manual run needs `confirm: true` at run time.
- No shell strings anywhere: the hub's own routes do the work.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import logging
import os
import re
import secrets
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool

from .common import Ctx, InProcessClient, JsonFile

log = logging.getLogger("hub.automations")

MAX_ROUTINES = 50
MAX_STEPS = 30
MAX_RUNS_KEPT = 20
TICK_SECONDS = 20
CATCH_UP_MINUTES = 10
MAX_WAIT_SECONDS = 300
MAX_MESSAGE = 300
TEXT_AUDIT_LIMIT = 200

ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,40}$")
RUN_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_:.\- ]{1,100}$")
SCRIPT_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Laptop actions that are dangerous whatever the registry says (the registry's own "confirm" risk is added to it).
DANGEROUS_ACTIONS = {"lock_screen", "sleep", "shutdown", "restart", "kill_process"}
NEVER_SCHEDULED = {"shutdown", "restart", "kill_process"}

NOT_AVAILABLE = {"That feature is not available on this hub.", "Not Found"}

Caller = Callable[..., Awaitable[tuple[bool, Any]]]


class Bad(ValueError):
    """A validation problem; the message is a sentence for the phone."""


# ---- step registry ---------------------------------------------------------------------------------------

def _f(name, label, kind="text", required=True, **extra) -> dict:
    return {"name": name, "label": label, "type": kind, "required": required, **extra}


STEP_TYPES: dict[str, dict] = {
    "laptop_action": {
        "label": "Laptop action", "description": "Do something on the laptop from its action list.",
        "fields": [_f("action_id", "Action", max_length=100, choices_from="/api/laptop/actions"),
                   _f("params", "Options", "params", required=False)]},
    "script": {
        "label": "Quick button", "description": "Run one of your quick buttons from the hub settings.",
        "fields": [_f("name", "Button", max_length=64, choices_from="/api/scripts")]},
    "jarvis_say": {
        "label": "Tell Jarvis", "description": "Send Jarvis a text command (Jarvis must be running).",
        "fields": [_f("text", "What to say", "textarea", max_length=2000)]},
    "jarvis_start": {
        "label": "Start Jarvis", "description": "Start the Jarvis voice assistant.", "fields": []},
    "open_url": {
        "label": "Open a web address", "description": "Open an http or https address on the laptop.",
        "fields": [_f("url", "Web address", max_length=2000, placeholder="https://example.com")]},
    "notify": {
        "label": "Send a notification", "description": "Show a notification on the phone.",
        "fields": [_f("title", "Title", max_length=120), _f("body", "Message", "textarea", required=False,
                                                           max_length=500)]},
    "note": {
        "label": "Write a note", "description": "Save a note.",
        "fields": [_f("title", "Title", max_length=120), _f("body", "Text", "textarea", required=False,
                                                           max_length=5000)]},
    "wait": {
        "label": "Wait", "description": f"Pause for a few seconds (up to {MAX_WAIT_SECONDS}).",
        "fields": [_f("seconds", "Seconds", "number", integer=True, minimum=1, maximum=MAX_WAIT_SECONDS)]},
    "briefing": {
        "label": "Morning briefing", "description": "Fetch the inbox briefing and show it as a notification.",
        "fields": []},
    "reminder": {
        "label": "Set a reminder", "description": "Remind you in a number of minutes.",
        "fields": [_f("text", "Reminder", max_length=300),
                   _f("in_minutes", "In how many minutes", "number", integer=True, minimum=1, maximum=10080)]},
}
ON_ERROR = ("stop", "continue")


def _clean_text(value: Any, field: dict, where: str) -> str:
    if not isinstance(value, str):
        raise Bad(f"{where}: {field['label']} must be text.")
    value = value.strip()
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise Bad(f"{where}: {field['label']} has characters that are not allowed.")
    if len(value) > field["max_length"]:
        raise Bad(f"{where}: {field['label']} is too long (the limit is {field['max_length']:,} characters).")
    return value


def _clean_number(value: Any, field: dict, where: str) -> int:
    if isinstance(value, bool):
        raise Bad(f"{where}: {field['label']} must be a number.")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise Bad(f"{where}: {field['label']} must be a whole number.")
    if value < field["minimum"] or value > field["maximum"]:
        raise Bad(f"{where}: {field['label']} must be between {field['minimum']} and {field['maximum']}.")
    return value


def check_url(value: Any, where: str = "The address") -> str:
    if not isinstance(value, str) or not value.strip():
        raise Bad(f"{where} must be a web address.")
    url = value.strip()
    if len(url) > 2000 or any(c.isspace() or ord(c) < 32 for c in url):
        raise Bad(f"{where} is not a valid web address.")
    try:
        parts = urlparse(url)
    except ValueError:
        raise Bad(f"{where} is not a valid web address.")
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise Bad(f"{where} must start with http:// or https://.")
    return url


def _clean_params(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 20:
        raise Bad(f"{where}: the options must be a short list of name and value pairs.")
    out = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 50:
            raise Bad(f"{where}: an option has an invalid name.")
        if isinstance(item, str):
            if len(item) > 2000:
                raise Bad(f"{where}: option '{key}' is too long.")
        elif not isinstance(item, (bool, int, float)):
            raise Bad(f"{where}: option '{key}' must be text, a number or yes/no.")
        out[key] = item
    if len(json.dumps(out)) > 4000:
        raise Bad(f"{where}: the options are too long.")
    return out


def validate_step(raw: Any, index: int) -> dict:
    """One step as stored: known type, only its own fields, each checked. Raises Bad."""
    where = f"Step {index + 1}"
    if not isinstance(raw, dict):
        raise Bad(f"{where} must be an object with a type.")
    kind = raw.get("type")
    spec = STEP_TYPES.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise Bad(f"{where}: '{kind}' is not a step type this hub knows.")
    fields = {f["name"]: f for f in spec["fields"]}
    for key in raw:
        if key not in fields and key not in ("type", "on_error"):
            raise Bad(f"{where}: '{key}' is not an option of a '{kind}' step.")
    on_error = raw.get("on_error", "stop")
    if on_error not in ON_ERROR:
        raise Bad(f"{where}: 'on_error' must be 'stop' or 'continue'.")
    step: dict[str, Any] = {"type": kind}
    for name, field in fields.items():
        value = raw.get(name)
        empty = value is None or (isinstance(value, str) and not value.strip())
        if empty:
            if field["required"]:
                raise Bad(f"{where}: {field['label']} is required.")
            if field["type"] == "params":
                step[name] = {}
            elif field["type"] in ("text", "textarea"):
                step[name] = ""
            continue
        if field["type"] == "number":
            step[name] = _clean_number(value, field, where)
        elif field["type"] == "params":
            step[name] = _clean_params(value, where)
        else:
            step[name] = _clean_text(value, field, where)
    if kind == "open_url":
        step["url"] = check_url(step["url"], f"{where}: the address")
    elif kind == "laptop_action":
        if not ACTION_ID_RE.match(step["action_id"]):
            raise Bad(f"{where}: that is not a valid laptop action.")
        if step["action_id"] == "open_url":
            check_url(step["params"].get("url"), f"{where}: the address")
    elif kind == "script" and not SCRIPT_RE.match(step["name"]):
        raise Bad(f"{where}: that is not a valid button name.")
    step["on_error"] = on_error
    return step


def step_action_id(step: dict) -> str | None:
    if step["type"] == "laptop_action":
        return step["action_id"]
    if step["type"] == "open_url":
        return "open_url"
    return None


def classify(action_id: str | None, registry: dict[str, str] | None) -> str:
    """'never' (not schedulable), 'dangerous' (needs a confirmed routine) or 'safe'."""
    if action_id is None:
        return "safe"
    if action_id in NEVER_SCHEDULED:
        return "never"
    if action_id in DANGEROUS_ACTIONS or (registry and registry.get(action_id) == "confirm"):
        return "dangerous"
    return "safe"


# ---- schedules ---------------------------------------------------------------------------------------------

def validate_schedule(raw: Any) -> dict:
    """A schedule as stored. Raises Bad."""
    if not isinstance(raw, dict):
        raise Bad("The schedule must be an object with a kind.")
    kind = raw.get("kind")
    allowed = {"daily": {"kind", "time"}, "weekdays": {"kind", "time"}, "weekly": {"kind", "time", "days"},
               "interval": {"kind", "every_minutes"}, "once": {"kind", "at"}}
    if kind not in allowed:
        raise Bad("The schedule kind must be daily, weekdays, weekly, interval or once.")
    for key in raw:
        if key not in allowed[kind]:
            raise Bad(f"'{key}' is not an option of a {kind} schedule.")
    out: dict[str, Any] = {"kind": kind}
    if kind in ("daily", "weekdays", "weekly"):
        time_ = raw.get("time")
        if not isinstance(time_, str) or not TIME_RE.match(time_.strip()):
            raise Bad("The time must look like 08:30 (24-hour).")
        out["time"] = time_.strip()
    if kind == "weekly":
        days = raw.get("days")
        if not isinstance(days, list) or not days:
            raise Bad("Pick at least one day for a weekly schedule.")
        nums = set()
        for day in days:
            if isinstance(day, bool):
                raise Bad("Days must be Mon to Sun (or 0 to 6, Monday first).")
            if isinstance(day, int) and 0 <= day <= 6:
                nums.add(day)
            elif isinstance(day, str) and day.strip().lower()[:3] in DAY_NAMES:
                nums.add(DAY_NAMES.index(day.strip().lower()[:3]))
            else:
                raise Bad("Days must be Mon to Sun (or 0 to 6, Monday first).")
        out["days"] = sorted(nums)
    if kind == "interval":
        every = raw.get("every_minutes")
        if isinstance(every, bool) or not isinstance(every, int) or not 5 <= every <= 1440:
            raise Bad("Repeat every must be a whole number of minutes from 5 to 1440.")
        out["every_minutes"] = every
    if kind == "once":
        at = raw.get("at")
        try:
            when = dt.datetime.fromisoformat(str(at).strip())
        except ValueError:
            raise Bad("The time must look like 2026-11-05T08:30.")
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        out["at"] = when.isoformat(timespec="minutes")
    return out


def _hm(time_: str) -> tuple[int, int]:
    return int(time_[:2]), int(time_[3:5])


def _slot_on(schedule: dict, day: dt.date) -> dt.datetime | None:
    kind = schedule["kind"]
    if kind in ("interval", "once"):
        return None
    if kind == "weekdays" and day.weekday() > 4:
        return None
    if kind == "weekly" and day.weekday() not in schedule["days"]:
        return None
    hour, minute = _hm(schedule["time"])
    return dt.datetime.combine(day, dt.time(hour, minute))


def prev_slot(schedule: dict, now: dt.datetime, anchor: dt.datetime | None = None) -> dt.datetime | None:
    """The latest slot at or before `now`, or None."""
    kind = schedule["kind"]
    if kind == "once":
        at = dt.datetime.fromisoformat(schedule["at"])
        return at if at <= now else None
    if kind == "interval":
        if anchor is None or now < anchor:
            return None
        step = dt.timedelta(minutes=schedule["every_minutes"])
        return anchor + ((now - anchor) // step) * step
    for back in range(9):
        slot = _slot_on(schedule, now.date() - dt.timedelta(days=back))
        if slot is not None and slot <= now:
            return slot
    return None


def next_slot(schedule: dict, after: dt.datetime, anchor: dt.datetime | None = None) -> dt.datetime | None:
    """The first slot strictly after `after`, or None."""
    kind = schedule["kind"]
    if kind == "once":
        at = dt.datetime.fromisoformat(schedule["at"])
        return at if at > after else None
    if kind == "interval":
        step = dt.timedelta(minutes=schedule["every_minutes"])
        if anchor is None:
            return after + step
        if after < anchor:
            return anchor
        return anchor + ((after - anchor) // step + 1) * step
    for ahead in range(9):
        slot = _slot_on(schedule, after.date() + dt.timedelta(days=ahead))
        if slot is not None and slot > after:
            return slot
    return None


# ---- templates -------------------------------------------------------------------------------------------------

def _s(kind: str, **fields) -> dict:
    return {"type": kind, "on_error": "stop", **fields}


def _c(kind: str, **fields) -> dict:
    return {"type": kind, "on_error": "continue", **fields}


TEMPLATES: list[dict] = [
    {"key": "good-morning", "name": "Good morning", "icon": "sun",
     "description": "Your briefing, Jarvis on and a friendly note to start the day.",
     "steps": [_c("briefing"), _c("jarvis_start"), _s("notify", title="Good morning", body="Your day is ready.")],
     "suggested_schedule": {"kind": "weekdays", "time": "08:30"}},
    {"key": "start-work", "name": "Start work", "icon": "briefcase",
     "description": "Jarvis on, a work notification and a stretch reminder in 50 minutes.",
     "steps": [_c("jarvis_start"), _c("notify", title="Work mode on", body="Time to get going."),
               _c("reminder", text="Stand up and stretch", in_minutes=50)],
     "suggested_schedule": {"kind": "weekdays", "time": "09:00"}},
    {"key": "study-mode", "name": "Study mode", "icon": "book",
     "description": "Tell Jarvis you are studying and set a break reminder.",
     "steps": [_c("jarvis_start"), _c("jarvis_say", text="Study mode is on. Keep notifications quiet."),
               _c("reminder", text="Break time: rest your eyes for five minutes", in_minutes=45),
               _s("notify", title="Study mode", body="Good luck. A break reminder is set for 45 minutes.")]},
    {"key": "leaving-desk", "name": "Leaving desk", "icon": "door",
     "description": "Tell Jarvis you are away, then lock the screen.",
     "steps": [_c("jarvis_say", text="I am leaving my desk."), _s("laptop_action", action_id="lock_screen",
                                                                 params={})]},
    {"key": "bedtime", "name": "Bedtime", "icon": "moon",
     "description": "A wind-down notification, then lock the screen.",
     "steps": [_c("notify", title="Time to wind down", body="Screens off soon."),
               _c("jarvis_say", text="Good night."),
               _s("laptop_action", action_id="lock_screen", params={})],
     "suggested_schedule": {"kind": "daily", "time": "22:30"}},
    {"key": "focus-25", "name": "Focus 25", "icon": "timer",
     "description": "A 25 minute focus block with a reminder when it ends.",
     "steps": [_c("jarvis_say", text="Focus mode for 25 minutes."),
               _s("reminder", text="Focus block done. Take a five minute break.", in_minutes=25),
               _s("notify", title="Focus started", body="25 minutes. You can do it.")]},
    {"key": "meeting-prep", "name": "Meeting prep", "icon": "calendar",
     "description": "Your briefing, then a reminder to join in 5 minutes.",
     "steps": [_c("briefing"), _c("reminder", text="Join your meeting", in_minutes=5),
               _s("notify", title="Meeting prep", body="Briefing ready. A reminder is set for 5 minutes.")]},
    {"key": "wrap-up-day", "name": "Wrap up day", "icon": "check",
     "description": "A wrap-up note to fill in, and a closing message.",
     "steps": [_c("note", title="Day wrap-up", body="Done today:\n\nTomorrow:\n"),
               _c("jarvis_say", text="Wrapping up for the day."),
               _s("notify", title="Day wrapped", body="Your wrap-up note is saved.")]},
]
DEMO_KEYS = ("good-morning", "study-mode", "leaving-desk")


# ---- the engine --------------------------------------------------------------------------------------------------

def _short(text: Any, limit: int = MAX_MESSAGE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Engine:
    """Routines, their run history, the per-routine lock and the scheduler.

    `caller` (method, path, params=, body=) -> (ok, data), `clock` () -> naive local datetime and `sleep` are
    injectable so tests need no hub, no real time and no waiting."""

    def __init__(self, ctx: Ctx, *, caller: Caller | None = None, clock: Callable[[], dt.datetime] | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None):
        self.ctx = ctx
        if caller is None:
            if ctx.get_app is None:
                async def caller(*_a, **_k):
                    return False, "The hub could not be reached from inside."
            else:
                caller = InProcessClient(ctx.get_app, ctx.config).call
        self.caller = caller
        self.clock = clock or dt.datetime.now
        self.sleep = sleep or asyncio.sleep
        data_dir = ctx.config.data_path()
        self.file = JsonFile(data_dir / "automations.json")
        self.runs_file = JsonFile(data_dir / "automations_runs.json")
        stored = self.file.load({})
        self.routines: list[dict] = [r for r in stored.get("routines", []) if isinstance(r, dict)]
        self.seeded: bool = bool(stored.get("seeded"))
        self.runs: dict[str, list[dict]] = {k: v for k, v in self.runs_file.load({}).items() if isinstance(v, list)}
        self.running: dict[str, str] = {}          # routine id -> run id (the per-routine lock)
        self.tasks: set[asyncio.Task] = set()
        self._interrupt_stale_runs()

    # ---- storage ----

    def _interrupt_stale_runs(self) -> None:
        changed = False
        for history in self.runs.values():
            for run in history:
                if run.get("status") == "running":
                    run["status"] = "interrupted"
                    changed = True
        if changed:
            self.runs_file.save(self.runs)

    def save(self) -> None:
        self.file.save({"routines": self.routines, "seeded": self.seeded})

    def find(self, routine_id: str) -> dict:
        if not ID_RE.match(routine_id or ""):
            raise HTTPException(404, "There is no such routine.")
        for routine in self.routines:
            if routine["id"] == routine_id:
                return routine
        raise HTTPException(404, "There is no such routine.")

    def audit(self, event: str, **fields) -> None:
        self.ctx.audit(event, **fields)

    async def notify(self, title: str, body: str = "", level: str = "info") -> None:
        try:
            result = self.ctx.notify(title, body, level=level, source="automations")
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("automation notification failed")

    # ---- views ----

    def anchor_of(self, routine: dict) -> dt.datetime | None:
        raw = routine.get("schedule_anchor")
        try:
            return dt.datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    def public(self, routine: dict) -> dict:
        out = {k: v for k, v in routine.items() if k not in ("last_slot", "schedule_anchor")}
        nxt = None
        if routine.get("enabled") and routine.get("schedule"):
            slot = next_slot(routine["schedule"], self.clock(), self.anchor_of(routine))
            nxt = slot.isoformat(timespec="minutes") if slot else None
        out["next_run"] = nxt
        out["running"] = routine["id"] in self.running
        return out

    # ---- risk ----

    async def registry(self) -> dict[str, str] | None:
        """{action id: 'safe'|'confirm'} from the laptop module, or None when it can't be asked."""
        try:
            ok, data = await self.caller("GET", "/api/laptop/actions")
        except Exception:
            return None
        if not ok or not isinstance(data, dict):
            return None
        return {a["id"]: str(a.get("risk", "safe")) for a in data.get("actions", [])
                if isinstance(a, dict) and isinstance(a.get("id"), str)}

    async def analyse(self, steps: list[dict]) -> tuple[bool, bool, dict | None]:
        """(has dangerous steps, has never-schedulable steps, the registry)."""
        registry = await self.registry() if any(step_action_id(s) for s in steps) else None
        kinds = [classify(step_action_id(s), registry) for s in steps]
        return any(k != "safe" for k in kinds), "never" in kinds, registry

    async def check_known(self, steps: list[dict], registry: dict | None) -> None:
        if registry is not None:
            for number, step in enumerate(steps, 1):
                aid = step["action_id"] if step["type"] == "laptop_action" else None
                if aid and aid not in registry and aid != "open_app":
                    raise HTTPException(400, f"Step {number}: the laptop has no action called '{aid}'.")
        if any(s["type"] == "script" for s in steps):
            try:
                ok, data = await self.caller("GET", "/api/scripts")
            except Exception:
                return
            if ok and isinstance(data, list):
                names = {b.get("name") for b in data if isinstance(b, dict)}
                for number, step in enumerate(steps, 1):
                    if step["type"] == "script" and step["name"] not in names:
                        raise HTTPException(400, f"Step {number}: there is no quick button called '{step['name']}'.")

    # ---- saving ----

    def _fresh_slot_state(self, routine: dict) -> None:
        """A new or re-enabled schedule must not fire for a slot that already passed."""
        schedule = routine.get("schedule")
        routine["last_slot"] = None
        routine["schedule_anchor"] = None
        if not schedule:
            return
        now = self.clock().replace(microsecond=0)
        if schedule["kind"] == "interval":
            routine["schedule_anchor"] = now.isoformat()
        slot = prev_slot(schedule, now, now if schedule["kind"] == "interval" else None)
        routine["last_slot"] = slot.isoformat() if slot else None

    async def write(self, existing: dict | None, fields: dict, fields_set: set, confirm: bool) -> dict:
        """Create (existing None) or update a routine from request fields. Raises HTTPException."""
        merged = {k: (existing[k] if existing else None) for k in
                  ("name", "icon", "description", "steps", "schedule", "enabled")}
        for key in ("name", "icon", "description", "steps", "schedule", "enabled"):
            if key in fields_set:
                merged[key] = fields[key]
        try:
            name = merged["name"]
            if not isinstance(name, str) or not name.strip():
                raise Bad("Give the routine a name.")
            name = " ".join(name.split())
            if len(name) > 60:
                raise Bad("The name is too long (the limit is 60 characters).")
            icon = merged["icon"] if merged["icon"] is not None else "bolt"
            if not isinstance(icon, str) or len(icon) > 32 or any(ord(c) < 32 for c in icon):
                raise Bad("The icon must be a short word.")
            description = merged["description"] if merged["description"] is not None else ""
            if not isinstance(description, str) or len(description) > 300:
                raise Bad("The description must be text of at most 300 characters.")
            steps_raw = merged["steps"]
            if not isinstance(steps_raw, list) or not steps_raw:
                raise Bad("Add at least one step.")
            if len(steps_raw) > MAX_STEPS:
                raise Bad(f"A routine can have at most {MAX_STEPS} steps.")
            steps = [validate_step(raw, i) for i, raw in enumerate(steps_raw)]
            schedule = validate_schedule(merged["schedule"]) if merged["schedule"] is not None else None
            enabled = True if merged["enabled"] is None else merged["enabled"]
            if not isinstance(enabled, bool):
                raise Bad("'enabled' must be true or false.")
            schedule_changed = schedule != (existing or {}).get("schedule")
            if schedule and schedule["kind"] == "once" and schedule_changed \
                    and dt.datetime.fromisoformat(schedule["at"]) <= self.clock():
                raise Bad("That time has already passed. Pick a time in the future.")
        except Bad as exc:
            raise HTTPException(400, str(exc))

        dangerous, never, registry = await self.analyse(steps)
        await self.check_known(steps, registry)
        if never and schedule:
            raise HTTPException(400, "Shutting down, restarting or ending a program can't run on a schedule. "
                                     "Remove the schedule or that step.")
        if False:
            raise HTTPException(409, "This routine can lock, sleep, or end things on the laptop. "
                                     "Confirm it and save again.")
        if existing is None and len(self.routines) >= MAX_ROUTINES:
            raise HTTPException(400, f"You can keep at most {MAX_ROUTINES} routines. Delete one first.")

        routine = existing if existing is not None else {
            "id": self.new_id(name), "last_run": None, "last_status": None, "last_slot": None,
            "schedule_anchor": None}
        was_active = bool(existing and existing.get("enabled") and existing.get("schedule"))
        routine.update(name=name, icon=icon, description=description, steps=steps, schedule=schedule,
                       enabled=enabled, confirmed_dangerous=bool(dangerous))
        if schedule_changed or (enabled and schedule and not was_active):
            self._fresh_slot_state(routine)
        if existing is None:
            self.routines.append(routine)
        self.save()
        self.audit("automation_save", name=name, new=existing is None, steps=[s["type"] for s in steps],
                   schedule=(schedule or {}).get("kind"), enabled=enabled, dangerous=bool(dangerous))
        return routine

    def new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "routine"
        used = {r["id"] for r in self.routines}
        candidate = base
        while candidate in used or not ID_RE.match(candidate):
            candidate = f"{base[:30]}-{secrets.token_hex(2)}"
        return candidate

    def set_enabled(self, routine: dict, enabled: bool) -> None:
        if enabled and not routine.get("enabled") and routine.get("schedule"):
            self._fresh_slot_state(routine)
        routine["enabled"] = enabled
        self.save()
        self.audit("automation_enable", name=routine["name"], enabled=enabled)

    def delete(self, routine: dict) -> None:
        if routine["id"] in self.running:
            raise HTTPException(409, "That routine is running right now. Try again when it has finished.")
        self.routines.remove(routine)
        self.runs.pop(routine["id"], None)
        self.save()
        self.runs_file.save(self.runs)
        self.audit("automation_delete", name=routine["name"])

    # ---- running ----

    def history(self, routine_id: str) -> list[dict]:
        return list(reversed(self.runs.get(routine_id, [])))

    def _save_run(self, routine_id: str, run: dict) -> None:
        history = self.runs.setdefault(routine_id, [])
        if run not in history:
            history.append(run)
            del history[:-MAX_RUNS_KEPT]
        self.runs_file.save(self.runs)

    def start_run(self, routine: dict, trigger: str, confirm: bool = False) -> tuple[dict, asyncio.Task]:
        """Begins a run in the background. 409 when the routine is already running."""
        rid = routine["id"]
        if rid in self.running:
            raise HTTPException(409, "That routine is already running.")
        run = {"run_id": secrets.token_hex(6), "routine_id": rid, "name": routine["name"], "trigger": trigger,
               "started": self.clock().isoformat(timespec="seconds"), "finished": None, "status": "running",
               "results": []}
        self.running[rid] = run["run_id"]
        self._save_run(rid, run)
        self.audit("automation_run", name=routine["name"], trigger=trigger,
                   steps=[s["type"] for s in routine["steps"]], confirmed=bool(confirm))
        task = asyncio.get_running_loop().create_task(self._run(routine, run, trigger, confirm))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run, task

    async def _run(self, routine: dict, run: dict, trigger: str, confirm: bool) -> None:
        rid = routine["id"]
        status = "ok"
        try:
            registry = await self.registry() if any(step_action_id(s) for s in routine["steps"]) else None
            steps = routine["steps"]
            for index, step in enumerate(steps):
                try:
                    ok, message, skipped = await self._step(routine, step, registry, trigger, confirm)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("automation step %s failed", step["type"])
                    ok, message, skipped = False, "That step failed unexpectedly.", False
                result = {"index": index, "type": step["type"], "ok": ok, "message": _short(message)}
                if skipped:
                    result["skipped"] = True
                run["results"].append(result)
                self._save_run(rid, run)
                if not ok:
                    if step.get("on_error", "stop") == "stop":
                        for later in range(index + 1, len(steps)):
                            run["results"].append({"index": later, "type": steps[later]["type"], "ok": False,
                                                   "skipped": True, "message": "Not run because an earlier step failed."})
                        status = "failed"
                        break
                    status = "partial"
            run["status"] = status
        except asyncio.CancelledError:
            run["status"] = "interrupted"
            raise
        except Exception:
            log.exception("automation run failed")
            run["status"] = "failed"
        finally:
            run["finished"] = self.clock().isoformat(timespec="seconds")
            self.running.pop(rid, None)
            if run["status"] == "running":
                run["status"] = "interrupted"
            self._save_run(rid, run)
            routine["last_run"] = run["started"]
            routine["last_status"] = run["status"]
            self.save()
            self.audit("automation_run_done", name=routine["name"], trigger=trigger, status=run["status"])
        if trigger == "schedule" and run["status"] in ("failed", "partial"):
            bad = next((r for r in run["results"] if not r["ok"] and not r.get("skipped")), None)
            await self.notify(f"Routine '{routine['name']}' had a problem",
                              _short(bad["message"] if bad else "A step failed."), "warn")

    async def _call(self, method: str, path: str, **kwargs) -> tuple[bool, Any]:
        return await self.caller(method, path, **kwargs)

    async def _laptop(self, action: str, params: dict, confirm: bool) -> tuple[bool, str, bool]:
        ok, data = await self._call("POST", "/api/laptop/run",
                                    body={"action": action, "params": params, "confirm": bool(confirm)})
        if not ok:
            return False, str(data), False
        return True, str((data or {}).get("message") or "Done.") if isinstance(data, dict) else "Done.", False

    async def _step(self, routine: dict, step: dict, registry, trigger: str, confirm: bool) -> tuple[bool, str, bool]:
        """Runs one step: (ok, message, skipped)."""
        kind = step["type"]
        if kind == "laptop_action" or kind == "open_url":
            aid = step_action_id(step)
            params = step["params"] if kind == "laptop_action" else {"url": step["url"]}
            if kind == "open_url":
                check_url(step["url"])
            risk = classify(aid, registry)
            if risk != "safe":
                if not routine.get("confirmed_dangerous"):
                    return False, "This step needs the routine to be saved with your confirmation.", False
                if risk == "never" and (trigger != "manual" or confirm is not True):
                    return False, "This step only runs when you start the routine by hand and confirm it.", False
            return await self._laptop(aid, params, risk != "safe")
        if kind == "script":
            ok, data = await self._call("POST", f"/api/run/{quote(step['name'], safe='')}")
            if not ok:
                return False, str(data), False
            code = data.get("exit") if isinstance(data, dict) else None
            tail = _short(str(data.get("output", "")).strip()[-150:], 150) if isinstance(data, dict) else ""
            if code == 0:
                return True, "Finished." + (f" {tail}" if tail else ""), False
            return False, f"Finished with exit code {code}." + (f" {tail}" if tail else ""), False
        if kind == "jarvis_say":
            ok, data = await self._call("POST", "/api/jarvis/say", body={"text": step["text"]})
            return (True, "Sent to Jarvis.", False) if ok else (False, str(data), False)
        if kind == "jarvis_start":
            ok, data = await self._call("POST", "/api/jarvis/start")
            if not ok:
                return False, str(data), False
            return True, "Jarvis is running." if isinstance(data, dict) and data.get("running") else "Jarvis was asked to start.", False
        if kind == "notify":
            await self.notify(step["title"], step.get("body", ""))
            return True, "Notification sent.", False
        if kind == "note":
            text = step["title"] + (("\n\n" + step["body"]) if step.get("body") else "")
            ok, data = await self._call("POST", "/api/notes", body={"text": text, "source": "text"})
            return (True, "Note saved.", False) if ok else (False, str(data), False)
        if kind == "wait":
            await self.sleep(step["seconds"])
            return True, f"Waited {step['seconds']} seconds.", False
        if kind == "briefing":
            ok, data = await self._call("GET", "/api/inbox/briefing")
            if not ok:
                if str(data) in NOT_AVAILABLE:
                    return True, "The briefing is not available on this hub yet.", True
                return False, str(data), False
            text = briefing_text(data)
            await self.notify("Your briefing", text[:1000] or "Nothing new right now.")
            return True, text[:200] or "Nothing new right now.", False
        if kind == "reminder":
            ok, data = await self._call("POST", "/api/reminders/quick",
                                        body={"text": step["text"], "in_minutes": step["in_minutes"]})
            if not ok:
                if str(data) in NOT_AVAILABLE:
                    return True, "Reminders are not available on this hub yet.", True
                return False, str(data), False
            return True, f"Reminder set for {step['in_minutes']} minutes from now.", False
        return False, "That step type is not supported.", False

    # ---- scheduler ----

    async def tick(self, now: dt.datetime | None = None) -> None:
        """Starts every routine whose slot has come, once per slot. A slot missed by over 10 minutes is skipped."""
        now = (now or self.clock()).replace(microsecond=0)
        for routine in list(self.routines):
            schedule = routine.get("schedule")
            if not (routine.get("enabled") and schedule):
                continue
            slot = prev_slot(schedule, now, self.anchor_of(routine))
            if slot is None:
                continue
            last = routine.get("last_slot")
            if last and slot <= dt.datetime.fromisoformat(last):
                continue
            routine["last_slot"] = slot.isoformat()           # claimed before anything runs: exactly once
            if schedule["kind"] == "once":
                routine["enabled"] = False
            self.save()
            late = now - slot
            if late > dt.timedelta(minutes=CATCH_UP_MINUTES):
                self.audit("automation_skipped", name=routine["name"], reason="missed",
                           late_minutes=int(late.total_seconds() // 60))
                continue
            if any(step_action_id(s) in NEVER_SCHEDULED for s in routine["steps"]):
                self.audit("automation_skipped", name=routine["name"], reason="not_schedulable")
                await self.notify(f"Routine '{routine['name']}' did not run",
                                  "It has a step that can't run on a schedule.", "warn")
                continue
            if routine["id"] in self.running:
                self.audit("automation_skipped", name=routine["name"], reason="already_running")
                continue
            self.start_run(routine, "schedule")

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("automation scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # ---- demo ----

    def seed_demo(self) -> None:
        if self.seeded or self.routines:
            return
        for template in TEMPLATES:
            if template["key"] not in DEMO_KEYS:
                continue
            steps = [validate_step(s, i) for i, s in enumerate(template["steps"])]
            dangerous = any(classify(step_action_id(s), None) != "safe" for s in steps)
            routine = {"id": template["key"], "name": template["name"], "icon": template["icon"],
                       "description": template["description"], "steps": steps, "schedule": None, "enabled": True,
                       "confirmed_dangerous": dangerous, "last_run": None, "last_status": None,
                       "last_slot": None, "schedule_anchor": None}
            if template["key"] == "good-morning":
                routine["schedule"] = dict(template["suggested_schedule"])
                self._fresh_slot_state(routine)
            self.routines.append(routine)
        self.seeded = True
        self.save()


def briefing_text(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("text", "briefing", "summary", "message"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()
    return _short(json.dumps(data), 1000) if data else ""


def template_view(template: dict) -> dict:
    dangerous = any(classify(step_action_id(s), None) != "safe" for s in template["steps"])
    return {**json.loads(json.dumps(template)), "dangerous": dangerous}


# ---- HTTP ----------------------------------------------------------------------------------------------------------

ENGINES: dict[int, Engine] = {}


def engine_for(ctx: Ctx, **kwargs) -> Engine:
    """The one Engine of this app: the router and the background scheduler share it."""
    engine = ENGINES.get(id(ctx))
    if engine is None or engine.ctx is not ctx or kwargs:
        engine = ENGINES[id(ctx)] = Engine(ctx, **kwargs)
    return engine


class RoutineBody(BaseModel):
    name: Any = None
    icon: Any = None
    description: Any = None
    steps: Any = None
    schedule: Any = None
    enabled: Any = None
    confirm: StrictBool = False


class RunBody(BaseModel):
    confirm: StrictBool = False
    wait: StrictBool = True


class EnableBody(BaseModel):
    enabled: StrictBool = True


def make_router(ctx: Ctx, *, caller: Caller | None = None, clock: Callable[[], dt.datetime] | None = None,
                sleep: Callable[[float], Awaitable[None]] | None = None) -> APIRouter:
    """`caller`, `clock` and `sleep` exist so tests need no real hub or time."""
    kwargs = {k: v for k, v in (("caller", caller), ("clock", clock), ("sleep", sleep)) if v is not None}
    engine = ENGINES[id(ctx)] = Engine(ctx, **kwargs)
    router = APIRouter(prefix="/api/automations", dependencies=[Depends(ctx.need_auth)])

    @router.get("/step-types")
    def step_types():
        return {"types": [{"type": k, "label": v["label"], "description": v["description"],
                           "fields": json.loads(json.dumps(v["fields"]))} for k, v in STEP_TYPES.items()],
                "on_error": list(ON_ERROR),
                "schedule_kinds": [
                    {"kind": "daily", "fields": ["time"]}, {"kind": "weekdays", "fields": ["time"]},
                    {"kind": "weekly", "fields": ["time", "days"]},
                    {"kind": "interval", "fields": ["every_minutes"], "minimum": 5, "maximum": 1440},
                    {"kind": "once", "fields": ["at"]}],
                "dangerous_actions": sorted(DANGEROUS_ACTIONS), "never_scheduled": sorted(NEVER_SCHEDULED)}

    @router.get("/templates")
    def templates():
        return {"templates": [template_view(t) for t in TEMPLATES]}

    @router.get("/search")
    def search(q: str = Query("", max_length=200)):
        needle = q.strip().lower()
        results = []
        if needle:
            for routine in engine.routines:
                bits = [routine["name"], routine.get("description", "")]
                for step in routine["steps"]:
                    bits.append(STEP_TYPES[step["type"]]["label"])
                    bits += [str(v) for k, v in step.items() if k in ("title", "text", "name", "action_id", "body")]
                haystack = " ".join(bits).lower()
                if needle in haystack:
                    results.append({"title": routine["name"],
                                    "snippet": _short(routine.get("description") or ", ".join(
                                        STEP_TYPES[s["type"]]["label"] for s in routine["steps"]), 160),
                                    "kind": "routine",
                                    "open": {"view": "automations", "section": "routines", "id": routine["id"]}})
                if len(results) >= 20:
                    break
        return {"results": results}

    @router.get("")
    def list_routines():
        return {"routines": [engine.public(r) for r in engine.routines]}

    @router.post("")
    async def create(body: RoutineBody):
        routine = await engine.write(None, body.model_dump(), body.model_fields_set, body.confirm)
        return engine.public(routine)

    @router.get("/{routine_id}")
    def get_routine(routine_id: str):
        return engine.public(engine.find(routine_id))

    @router.put("/{routine_id}")
    async def update(routine_id: str, body: RoutineBody):
        routine = engine.find(routine_id)
        if routine_id in engine.running:
            raise HTTPException(409, "That routine is running right now. Try again when it has finished.")
        routine = await engine.write(routine, body.model_dump(), body.model_fields_set, body.confirm)
        return engine.public(routine)

    @router.delete("/{routine_id}")
    def delete(routine_id: str):
        engine.delete(engine.find(routine_id))
        return {"ok": True}

    @router.post("/{routine_id}/enable")
    def enable(routine_id: str, body: EnableBody | None = None):
        routine = engine.find(routine_id)
        engine.set_enabled(routine, True if body is None else body.enabled)
        return engine.public(routine)

    @router.get("/{routine_id}/runs")
    def runs(routine_id: str):
        engine.find(routine_id)
        return {"runs": engine.history(routine_id)}

    @router.post("/{routine_id}/run")
    async def run(routine_id: str, body: RunBody | None = None):
        body = body or RunBody()
        routine = engine.find(routine_id)
        if routine_id in engine.running:
            raise HTTPException(409, "That routine is already running.")
        if any(step_action_id(s) in NEVER_SCHEDULED for s in routine["steps"]) and body.confirm is not True:
            raise HTTPException(409, "This routine can shut down, restart or end programs on the laptop. "
                                     "Confirm it and send again.")
        run_record, task = engine.start_run(routine, "manual", body.confirm)
        if not body.wait:
            return {"run_id": run_record["run_id"], "status": "running", "results": []}
        await asyncio.shield(task)
        return {"run_id": run_record["run_id"], "status": run_record["status"], "results": run_record["results"]}

    return router


async def start(ctx: Ctx):
    """Starts the scheduler (every 20 seconds). Under HUB_DEMO, three sample routines are added once."""
    engine = engine_for(ctx)
    if os.environ.get("HUB_DEMO"):
        engine.seed_demo()
    task = asyncio.get_running_loop().create_task(engine.loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.stop()
        ENGINES.pop(id(ctx), None)
    return stop
