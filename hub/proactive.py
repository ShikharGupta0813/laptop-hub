"""PROACTIVE: the interrupt-vs-batch rule for Friday (idea: ethanplusai/jarvis).

Every "thing the hub wants you to know" is an event. `classify(event)` is a pure function that says what to do:

- 'now'   urgent: a meeting in 10 minutes or less, a flagged-urgent mail / Asana task overdue today, a Brain reminder
          due, the morning "your day is ready" push. Sent at once through `ctx.notify` (which also feeds the ntfy
          phone push and the live notification stream the app shows as a toast).
- 'batch' things that finished (Autopilot drafts ready, research done, backup done). Collected and delivered as ONE
          sentence in Friday's voice, at most every `proactive_batch_minutes` (a fixed template: no AI call).
- 'quiet' everything else, and during quiet hours everything except 'now' events of type meeting/reminder. Stored
          and shown in Today, never pushed.

Event types hooked up: 'meeting' (Friday's calendar check, once per meeting through the `dedupe` key, so only this
path pushes it), 'backup_done' (batch) and 'backup_failed' (now) from the in-hub backup (Settings > Backup). The
`python -m hub backup` command and the watchdog run as separate processes without the dispatcher and keep their own
direct ntfy push.

Routes (login needed): GET /api/proactive/state -> {enabled, pending, events (last 20), last_flush}.
State lives in `proactive.json` under the data folder. Other modules call `submit_or_notify(ctx, event)`, which uses
the dispatcher when it is running and falls back to a plain `ctx.notify` when it is not.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any, Callable

from fastapi import APIRouter, Depends

from .common import Ctx, JsonFile

log = logging.getLogger("hub.proactive")

NOW_TYPES = ("reminder", "morning", "evening_summary", "backup_failed", "applyq_daily")
BATCH_TYPES = ("autopilot_ready", "research_done", "backup_done", "cc_waiting", "build_done", "applyq_ready", "devto_comment", "devto_cadence", "alert")
QUIET_PASS = ("meeting", "reminder")       # the only 'now' types that may interrupt quiet hours
MEETING_MINUTES = 10
MAX_EVENTS = 20
MAX_PENDING = 100
MAX_SEEN = 200
LOOP_TICK = 60.0
CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
Clock = Callable[[], dt.datetime]


def _minutes(value: Any, default: int) -> int:
    m = CLOCK_RE.match(str(value or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else default


def in_quiet(now: dt.datetime, start: str = "22:00", end: str = "07:00") -> bool:
    a, b, cur = _minutes(start, 1320), _minutes(end, 420), now.hour * 60 + now.minute
    if a == b:
        return False
    return a <= cur < b if a < b else (cur >= a or cur < b)


def classify(event: dict, *, now: dt.datetime | None = None, quiet_start: str = "22:00",
             quiet_end: str = "07:00") -> str:
    """'now' | 'batch' | 'quiet'. Pure. Quiet hours apply only when `now` is given."""
    event = event if isinstance(event, dict) else {}
    kind = str(event.get("type") or "")
    if kind == "meeting":
        mins = event.get("minutes")
        ok = isinstance(mins, (int, float)) and not isinstance(mins, bool) and 0 <= mins <= MEETING_MINUTES
        base = "now" if ok else "quiet"
    elif kind in ("mail", "asana"):
        flagged = event.get("urgent") is True and (kind == "mail" or event.get("overdue_today") is True)
        base = "now" if flagged else "quiet"
    elif kind == "alert":      # hub/alerts.py: urgency 2-3 interrupts, 1 is batched, 0 is only listed
        u = event.get("urgency")
        u = u if isinstance(u, int) and not isinstance(u, bool) else 0
        base = "now" if u >= 2 else "batch" if u == 1 else "quiet"
    elif kind in NOW_TYPES:
        base = "now"
    elif kind in BATCH_TYPES:
        base = "batch"
    else:
        base = "quiet"
    if now is not None and base != "quiet" and in_quiet(now, quiet_start, quiet_end):
        passes = kind in QUIET_PASS or (kind == "alert" and event.get("urgency") == 3)   # only a P0-grade alert wakes you
        return "now" if base == "now" and passes else "quiet"
    return base


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _lower_first(text: str) -> str:
    text = text.rstrip(".")
    return text[:1].lower() + text[1:]


def batch_sentence(events: list[dict], call: str = "Chief") -> str:
    """One sentence in Friday's voice. Deterministic template, no AI."""
    ap = sum(int(e.get("count") or 1) for e in events if e.get("type") == "autopilot_ready")
    res = sum(1 for e in events if e.get("type") == "research_done")
    bak = sum(1 for e in events if e.get("type") == "backup_done")
    cc = [e for e in events if e.get("type") == "cc_waiting"]
    bld = sum(1 for e in events if e.get("type") == "build_done")
    apq = sum(int(e.get("count") or 1) for e in events if e.get("type") == "applyq_ready")
    dc = [e for e in events if e.get("type") == "devto_comment"]
    cad = [e for e in events if e.get("type") == "devto_cadence"]
    alr = sum(1 for e in events if e.get("type") == "alert")
    parts = []
    if alr:
        parts.append("1 alert is waiting in Alerts" if alr == 1 else f"{alr} alerts are waiting in Alerts")
    if ap:
        parts.append("1 Autopilot draft is waiting for you" if ap == 1 else f"{ap} Autopilot drafts are waiting for you")
    if res:
        parts.append("your research is ready" if res == 1 else f"{res} research reports are ready")
    if bak:
        parts.append("the backup is done")
    if len(cc) == 1:
        parts.append(str(cc[0].get("title") or "Claude is waiting on you").rstrip("."))
    elif cc:
        parts.append(f"Claude is waiting on you in {len(cc)} sessions")
    if bld:
        parts.append("your build is finished" if bld == 1 else f"{bld} builds are finished")
    if apq:
        parts.append("1 application is ready to review" if apq == 1 else f"{apq} applications are ready to review")
    n_dc = sum(int(e.get("count") or 1) for e in dc)
    if n_dc == 1:
        parts.append(_lower_first(str(dc[0].get("title") or "you have a new comment on your post")))
    elif n_dc:
        parts.append(f"you have {n_dc} new comments on your posts")
    if cad:
        parts.append(_lower_first(str(cad[-1].get("title") or "no post lately, ideas ready")))
    return f"{call}, {_join(parts)}." if parts else ""


class Proactive:
    def __init__(self, ctx: Ctx, clock: Clock | None = None):
        self.ctx = ctx
        self.clock: Clock = clock or dt.datetime.now
        self.file = JsonFile(ctx.config.data_path() / "proactive.json")

    # ---- config (read live) ---------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return getattr(self.ctx.config, "proactive_enabled", True) is not False

    def _quiet(self) -> tuple[str, str]:
        c = self.ctx.config
        return (str(getattr(c, "proactive_quiet_start", "22:00")), str(getattr(c, "proactive_quiet_end", "07:00")))

    def _window(self) -> int:
        v = getattr(self.ctx.config, "proactive_batch_minutes", 30)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 30

    def _call(self) -> str:
        return str(getattr(self.ctx.config, "display_name", "") or "Chief").strip() or "Chief"

    # ---- state -----------------------------------------------------------------------------------------
    def load(self) -> dict:
        d = self.file.load({})
        d = d if isinstance(d, dict) else {}
        return {"pending": [e for e in d.get("pending", []) if isinstance(e, dict)][-MAX_PENDING:],
                "events": [e for e in d.get("events", []) if isinstance(e, dict)][-MAX_EVENTS:],
                "last_flush": d.get("last_flush") if isinstance(d.get("last_flush"), str) else "",
                "seen": [str(x) for x in d.get("seen", [])][-MAX_SEEN:],
                "morning": str(d.get("morning") or ""), "applyq_daily": str(d.get("applyq_daily") or "")}

    def save(self, d: dict) -> None:
        try:
            self.file.save(d)
        except OSError:
            log.warning("proactive state could not be saved")

    def view(self) -> dict:
        d = self.load()
        return {"enabled": self.enabled, "pending": d["pending"], "events": d["events"][::-1],
                "last_flush": d["last_flush"] or None}

    # ---- dispatch ---------------------------------------------------------------------------------------
    def _push(self, title: str, body: str, level: str, link: str | None) -> None:
        try:
            self.ctx.notify(title, body=body, level=level, link=link, source="proactive")
        except Exception:  # noqa: BLE001 - a broken notifier must not break the caller
            log.warning("proactive push failed")

    def submit(self, event: dict) -> str:
        """Decide and act. Returns the decision. Never raises."""
        try:
            event = dict(event)
            now = self.clock()
            qs, qe = self._quiet()
            decision = classify(event, now=now, quiet_start=qs, quiet_end=qe)
            d = self.load()
            dedupe = str(event.get("dedupe") or "")[:80]
            if dedupe:                               # e.g. one push per meeting: the same key is never handled twice
                if f"e:{dedupe}" in d["seen"]:
                    return "duplicate"
                d["seen"].append(f"e:{dedupe}")
            title = " ".join(str(event.get("title") or "").split())[:120]
            body = str(event.get("body") or "")[:400]
            link = event.get("link") if isinstance(event.get("link"), str) else None
            at = now.isoformat(timespec="seconds")
            if decision == "now":
                level = event.get("level") if event.get("level") in ("info", "warn", "error") else ("warn" if event.get("urgent") else "info")
                self._push(title or "Friday", body, level, link)
            elif decision == "batch":
                d["pending"].append({"type": str(event.get("type")), "title": title,
                                     "count": event.get("count") or 1, "at": at})
            d["events"].append({"type": str(event.get("type") or ""), "title": title, "body": body[:200],
                                "decision": decision, "at": at})
            d["pending"], d["events"] = d["pending"][-MAX_PENDING:], d["events"][-MAX_EVENTS:]
            self.save(d)
            if decision == "batch":
                self.flush()
            return decision
        except Exception:  # noqa: BLE001
            log.exception("proactive submit failed")
            return "quiet"

    def flush(self, force: bool = False) -> bool:
        """Deliver the batch as one sentence when the window has passed. True when something was pushed."""
        try:
            d = self.load()
            now = self.clock()
            if not d["pending"] or not self.enabled:
                return False
            qs, qe = self._quiet()
            if not force and in_quiet(now, qs, qe):
                return False
            if d["last_flush"] and not force:
                try:
                    last = dt.datetime.fromisoformat(d["last_flush"])
                except ValueError:
                    last = None
                if last and (now - last).total_seconds() < self._window() * 60:
                    return False
            text = batch_sentence(d["pending"], self._call())
            d["pending"] = []
            d["last_flush"] = now.isoformat(timespec="seconds")
            self.save(d)
            if text:
                self._push("Friday", text, "info", "autopilot")
            return bool(text)
        except Exception:  # noqa: BLE001
            log.exception("proactive flush failed")
            return False

    # ---- scheduled bits (called by the loop) -----------------------------------------------------------
    def morning(self) -> bool:
        """Once a day at/after today_morning_time: 'Good morning, Chief. Your day is ready' with the page link."""
        t = str(getattr(self.ctx.config, "today_morning_time", "08:30") or "")
        m = CLOCK_RE.match(t)
        now = self.clock()
        if not self.enabled or not m or not getattr(self.ctx.config, "today_enabled", True):
            return False
        if now.hour * 60 + now.minute < int(m.group(1)) * 60 + int(m.group(2)):
            return False
        d = self.load()
        key = now.date().isoformat()
        if d["morning"] == key:
            return False
        d["morning"] = key
        self.save(d)                                  # first: a failure below never repeats the push
        call = self._call()
        return self.submit({"type": "morning", "title": f"Good morning, {call}",
                            "body": f"Good morning, {call}. Your day is ready.",
                            "link": "/api/today/front-page"}) == "now"

    def reminders(self) -> int:
        """Brain follow-ups that have come due and were not announced yet -> 'now' events."""
        from . import brain
        if not self.enabled:
            return 0
        now = self.clock()
        due = [f for f in brain.today_view(self.ctx.config, now)["due"] if f.get("overdue")]
        d = self.load()
        fresh = [f for f in due if f"b:{f['id']}" not in d["seen"]]
        for f in fresh:
            d["seen"].append(f"b:{f['id']}")
        if fresh:
            self.save(d)
        for f in fresh:
            self.submit({"type": "reminder", "title": f"Reminder: {f['title']}"[:120],
                         "body": (f"From {f['who']}" if f.get("who") else "Follow-up due"), "link": "brain"})
        return len(fresh)

    def applyq_daily(self) -> bool:
        """Once a day at/after today_evening_time: Friday's one-line application summary (hub/applyq.py)."""
        t = str(getattr(self.ctx.config, "today_evening_time", "18:00") or "")
        m = CLOCK_RE.match(t)
        now = self.clock()
        if not self.enabled or not m or now.hour * 60 + now.minute < int(m.group(1)) * 60 + int(m.group(2)):
            return False
        try:
            from . import applyq
            if not applyq.enabled(self.ctx.config):
                return False
            d = self.load()
            key = now.date().isoformat()
            if d.get("applyq_daily") == key:
                return False
            d["applyq_daily"] = key
            self.save(d)
            line = applyq.Applyq(self.ctx, clock=self.clock).daily_line()
        except Exception:  # noqa: BLE001 - optional feature
            return False
        if line.startswith("Today: 0 applied, 0 ready, 0 replies"):
            return False
        return self.submit({"type": "applyq_daily", "title": "Applications", "body": line, "link": "applyq"}) == "now"

    def tick(self) -> None:
        for step in (self.flush, self.morning, self.reminders, self.applyq_daily):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("proactive step failed")

    async def loop(self) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(LOOP_TICK)


def submit_or_notify(ctx: Ctx, event: dict) -> None:
    """For other modules: route through the interrupt-vs-batch rule when it is running, else a plain notification."""
    engine = getattr(ctx, "proactive", None)
    if engine is not None and engine.enabled:
        engine.submit(event)
        return
    ctx.notify(title=event.get("title") or "", body=event.get("body") or "", level="info",
               link=event.get("link"), source=event.get("source") or "proactive")


def make_router(ctx: Ctx, *, clock: Clock | None = None) -> APIRouter:
    engine = Proactive(ctx, clock)
    ctx.proactive = engine        # type: ignore[attr-defined]
    router = APIRouter(prefix="/api/proactive", dependencies=[Depends(ctx.need_auth)])
    router.engine = engine        # type: ignore[attr-defined]

    @router.get("/state")
    def state():
        return engine.view()

    return router


async def start(ctx: Ctx):
    engine = getattr(ctx, "proactive", None)
    if engine is None:
        return None
    task = asyncio.get_running_loop().create_task(engine.loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return stop
