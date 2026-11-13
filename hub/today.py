"""TODAY: one read-only timeline of the current local day, plus a short spoken-style summary. See docs/api/today.md.

The owner's wish: "when my day starts, when my day ends, or when I say a summary word, show me EVERYTHING and
never make me search". So this module only READS what the hub already knows, through the hub's own routes
(calendar, Asana, expenses, inbox, Autopilot, Council) and the owner's git folders (reusing Autopilot's git helper),
and puts it on one page:

- GET /api/today       -> {meetings, tasks, expenses, mail, done, pending, timeline, numbers, sources_failed, ...}
- GET /api/today/text  -> {text, words, style}: <= 180 words of plain sentences, built from a template with NO AI call
  (deterministic and instant). `?style=evening` gives the "what happened today" review.

One source failing never hides the others: its name goes to `sources_failed` and its section is empty.
Everything read from mail, Asana, git or notes is shown as text only; nothing here writes, sends or runs anything.
Switched off with `today_enabled: false` (both routes then answer 404, so the phone shows "not on this hub").
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from . import autopilot as autopilot_mod
from .common import Ctx, InProcessClient

log = logging.getLogger("hub.today")

SOURCES = ("calendar", "asana", "expenses", "mail", "autopilot", "git", "council", "brain")
MAX_WORDS = 180
AUDIT_TAIL_BYTES = 600_000
ASANA_DETAIL_LIMIT = 8       # open tasks whose comments are read to see what was written today
CURRENCY_WORDS = {"INR": "rupees", "USD": "dollars", "EUR": "euros", "GBP": "pounds"}
STYLES = ("morning", "evening")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

Caller = Callable[..., Awaitable[tuple[bool, Any]]]


# ---- small helpers --------------------------------------------------------------------------------------------

def clean(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub("", value).split())[:limit]


def local(value: Any) -> dt.datetime | None:
    """An ISO string or epoch number as a naive LOCAL datetime; None when it is neither."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return dt.datetime.fromtimestamp(float(value))
        if isinstance(value, str) and value.strip():
            when = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return when.astimezone().replace(tzinfo=None) if when.tzinfo else when
    except (ValueError, OverflowError, OSError):
        return None
    return None


def hhmm(when: dt.datetime | None) -> str:
    return when.strftime("%H:%M") if when else ""


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100:,}.{cents % 100:02d}"


def money_spoken(cents: int, currency: str) -> str:
    whole = abs(int(cents)) // 100
    word = CURRENCY_WORDS.get(currency)
    return f"{whole:,} {word}" if word else f"{whole:,} {currency}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def audit_events(path: Any, day: dt.date) -> list[dict]:
    """Audit lines of `day` (the tail of the file only). Used for Asana completions, which no route lists."""
    try:
        file = Path(path)
        size = file.stat().st_size
        with open(file, "rb") as f:
            f.seek(max(0, size - AUDIT_TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return []
    out = []
    for line in raw.splitlines():
        if day.isoformat() not in line[:40]:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("time") or "")[:10] == day.isoformat():
            out.append(row)
    return out


# ---- collecting -----------------------------------------------------------------------------------------------

async def collect(ctx: Ctx, call: Caller, clock: Callable[[], dt.datetime], *, git_runner=None,
                  audit_reader: Callable[[dt.date], list[dict]] | None = None) -> dict:
    """The whole day as JSON. `call(method, path, params=, body=)` returns (ok, data) like InProcessClient.call."""
    now = clock()
    day = now.date()
    iso_day = day.isoformat()
    failed: list[str] = []
    config = ctx.config

    async def guard(name: str, coro):
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one broken source must not hide the others
            log.exception("today source %s failed", name)
            failed.append(name)
            return None

    # -- calendar
    async def src_calendar() -> list[dict]:
        ok, data = await call("GET", "/api/work/calendar", params={"day": iso_day})
        if not ok or not isinstance(data, dict):
            failed.append("calendar")
            return []
        out = []
        for m in data.get("meetings") or []:
            if not isinstance(m, dict):
                continue
            start, end = local(m.get("start")), local(m.get("end"))
            link = m.get("link") if isinstance(m.get("link"), str) and m["link"].startswith(("https://", "http://")) else ""
            out.append({"title": clean(m.get("title"), 120) or "(no title)", "all_day": m.get("all_day") is True,
                        "start": start.isoformat(timespec="minutes") if start else "", "start_time": hhmm(start),
                        "end_time": hhmm(end), "link": link, "platform": clean(m.get("platform"), 12),
                        "past": bool(end and end <= now)})
        return sorted(out, key=lambda x: (not x["all_day"], x["start"]))[:30]

    # -- Asana
    async def src_asana() -> dict:
        ok, data = await call("GET", "/api/work/asana/tasks", params={"filter": "all"})
        if not ok or not isinstance(data, dict):
            failed.append("asana")
            return {"due_today": [], "overdue": [], "updated": [], "commented": [], "completed": 0}
        rows = [t for t in data.get("tasks") or [] if isinstance(t, dict) and not t.get("completed")]

        def pick(t: dict) -> dict:
            return {"gid": clean(str(t.get("gid") or ""), 30), "name": clean(t.get("name"), 120),
                    "project": clean(t.get("project"), 80), "due_on": str(t.get("due_on") or "")[:10]}
        due = [pick(t) for t in rows if isinstance(t.get("due_on"), str) and t["due_on"][:10] == iso_day]
        late = [pick(t) for t in rows if isinstance(t.get("due_on"), str) and t["due_on"][:10] < iso_day]
        ok, status = await call("GET", "/api/work/status")
        me = clean(((status.get("asana") or {}).get("user")) if ok and isinstance(status, dict) else "", 80).lower()
        updated, commented = [], []
        for t in (late + due)[:ASANA_DETAIL_LIMIT]:
            if not t["gid"].isdigit():
                continue
            ok, detail = await call("GET", f"/api/work/asana/tasks/{t['gid']}")
            task = detail.get("task") if ok and isinstance(detail, dict) else None
            if not isinstance(task, dict):
                continue
            changed = local(task.get("modified_at"))
            if changed and changed.date() == day:
                updated.append({**t, "at": hhmm(changed)})
            for c in task.get("comments") or []:
                when = local(c.get("created_at")) if isinstance(c, dict) else None
                if when and when.date() == day and (not me or clean(c.get("author"), 80).lower() == me):
                    commented.append({**t, "at": hhmm(when), "start": when.isoformat(timespec="minutes")})
                    break
        reader = audit_reader or (lambda d: audit_events(ctx.audit_path, d))
        completed = sum(1 for e in await asyncio.to_thread(reader, day)
                        if e.get("event") == "work_asana" and e.get("action") == "complete")
        return {"due_today": due[:15], "overdue": late[:15], "updated": updated, "commented": commented,
                "completed": completed}

    # -- expenses
    async def src_expenses() -> dict:
        ok, data = await call("GET", "/api/expenses", params={"month": iso_day[:7]})
        if not ok or not isinstance(data, dict):
            failed.append("expenses")
            return {"by_currency": {}, "items": [], "count": 0}
        items, totals = [], {}
        for e in data.get("expenses") or []:
            if not isinstance(e, dict) or str(e.get("date") or "")[:10] != iso_day:
                continue
            cents, cur = e.get("amount"), clean(e.get("currency"), 6) or "INR"
            if not isinstance(cents, int) or isinstance(cents, bool):
                continue
            bucket = totals.setdefault(cur, {"total": 0, "count": 0})
            bucket["total"] += cents
            bucket["count"] += 1
            items.append({"id": clean(str(e.get("id") or ""), 40), "amount": cents, "amount_text": money(cents),
                          "currency": cur, "category": clean(e.get("category"), 30), "note": clean(e.get("note"), 100)})
        for b in totals.values():
            b["total_text"] = money(b["total"])
        return {"by_currency": totals, "items": items[:50], "count": len(items)}

    # -- mail (unread count here; replies come from Autopilot's cards below)
    async def src_mail() -> dict:
        ok, data = await call("GET", "/api/inbox/status")
        if not ok or not isinstance(data, dict):
            return {"configured": False, "unread": None}
        unread = data.get("unread")
        return {"configured": bool(data.get("configured")),
                "unread": unread if isinstance(unread, int) and not isinstance(unread, bool) else None}

    # -- Autopilot
    async def src_autopilot() -> dict:
        ok, live = await call("GET", "/api/autopilot")
        if not ok or not isinstance(live, dict):
            return {"enabled": False, "pending": 0, "pending_cards": [], "today_cards": []}
        ok2, hist = await call("GET", "/api/autopilot/history", params={"limit": 200})
        cards = hist.get("cards") if ok2 and isinstance(hist, dict) else []
        today_cards = []
        for c in cards or []:
            if not isinstance(c, dict):
                continue
            when = local(c.get("executed_at")) or (local(c.get("created")) if c.get("status") in ("approved", "edited") else None)
            if not when or when.date() != day or c.get("status") not in ("executed", "approved", "edited"):
                continue
            today_cards.append({"title": clean(c.get("title"), 120), "kind": c.get("action_kind"), "status": c.get("status"),
                                "source": c.get("source"), "to": clean(c.get("recipient") or "", 120),
                                "sender": clean(c.get("sender"), 80), "subject": clean(c.get("subject") or "", 120),
                                "at": hhmm(when), "start": when.isoformat(timespec="minutes")})
        pend = [c for c in live.get("cards") or [] if isinstance(c, dict)]
        return {"enabled": bool(live.get("enabled")), "pending": len(pend),
                "pending_cards": [{"id": c.get("id"), "title": clean(c.get("title"), 100),
                                   "kind": c.get("action_kind")} for c in pend[:10]],
                "today_cards": today_cards}

    # -- git (Autopilot's helper, in a thread)
    async def src_git() -> list[dict]:
        repos = getattr(config, "autopilot_repos", None) or []
        found = await asyncio.to_thread(autopilot_mod.git_commits, repos, day, git_runner)
        out = []
        for c in found:
            when = local(c["at"])
            if when and when.date() == day:
                out.append({"repo": c["repo"], "subject": c["subject"], "at": hhmm(when),
                            "start": when.isoformat(timespec="minutes")})
        return sorted(out, key=lambda x: x["start"])

    # -- council
    async def src_council() -> list[dict]:
        ok, data = await call("GET", "/api/council/jobs")
        if not ok or not isinstance(data, dict):
            return []                                  # Council is optional: missing is not a failure
        out = []
        for j in data.get("jobs") or []:
            when = local(j.get("created")) if isinstance(j, dict) else None
            if when and when.date() == day:
                out.append({"id": clean(str(j.get("id") or ""), 40), "task": clean(j.get("task"), 100),
                            "status": clean(j.get("status"), 20), "at": hhmm(when),
                            "start": when.isoformat(timespec="minutes")})
        return sorted(out, key=lambda x: x["start"])

    # -- Brain: follow-ups due today (Friday's list) and open tasks, read from the memory file
    async def src_brain() -> dict:
        from . import brain
        return await asyncio.to_thread(brain.today_view, config, now)

    results = await asyncio.gather(*(guard(n, f()) for n, f in (
        ("calendar", src_calendar), ("asana", src_asana), ("expenses", src_expenses), ("mail", src_mail),
        ("autopilot", src_autopilot), ("git", src_git), ("council", src_council), ("brain", src_brain))))
    meetings, tasks, expenses, mail, ap, commits, council, memory = results
    memory = memory or {"due": [], "tasks": []}
    meetings = meetings or []
    tasks = tasks or {"due_today": [], "overdue": [], "updated": [], "commented": [], "completed": 0}
    expenses = expenses or {"by_currency": {}, "items": [], "count": 0}
    mail = mail or {"configured": False, "unread": None}
    ap = ap or {"enabled": False, "pending": 0, "pending_cards": [], "today_cards": []}
    commits, council = commits or [], council or []

    replies = [{"to": c["to"] or c["sender"], "subject": c["subject"], "at": c["at"], "start": c["start"],
                "by": "autopilot"} for c in ap["today_cards"] if c["kind"] == "send_email" and c["status"] == "executed"]
    mail = {**mail, "replied": replies}
    comments_sent = [c for c in ap["today_cards"] if c["kind"] == "asana_comment" and c["status"] == "executed"]
    done = {"commits": commits, "tasks": {"commented": tasks["commented"], "completed": tasks["completed"],
                                          "updated": tasks["updated"]},
            "cards": ap["today_cards"], "council": council}

    upcoming = [m for m in meetings if not m["all_day"] and not m["past"] and m["start_time"]]
    nxt = None
    if upcoming:
        first = upcoming[0]
        begins = local(first["start"])
        nxt = {"title": first["title"], "time": first["start_time"], "link": first["link"],
               "in_min": max(0, int((begins - now).total_seconds() // 60)) if begins else None}
    pending = {"autopilot": ap["pending"], "autopilot_enabled": ap["enabled"], "cards": ap["pending_cards"],
               "next_meeting": nxt}

    # -- the timeline: everything with a time, in order; spending has no clock time so it closes the day
    timeline: list[dict] = []

    def add(start: str, at: str, kind: str, title: str, sub: str = "") -> None:
        timeline.append({"start": start, "time": at, "kind": kind, "title": title, "sub": sub})
    for m in meetings:
        if not m["all_day"]:
            add(m["start"], m["start_time"], "meeting", m["title"],
                (m["start_time"] + "-" + m["end_time"]) if m["end_time"] else "")
    for c in commits:
        add(c["start"], c["at"], "commit", c["subject"], c["repo"])
    for r in replies:
        add(r["start"], r["at"], "mail", f"Replied to {r['to']}" if r["to"] else "Replied", r["subject"])
    for c in comments_sent:
        add(c["start"], c["at"], "task", "Commented on " + c["title"].split(": ", 1)[-1], "by Autopilot")
    for c in tasks["commented"]:
        add(c["start"], c["at"], "task", "Commented on " + c["name"], c["project"])
    for c in ap["today_cards"]:
        if c["kind"] in ("copy_text", "none"):
            add(c["start"], c["at"], "card", c["title"], c["status"])
    for j in council:
        add(j["start"], j["at"], "council", "Council: " + j["task"], j["status"])
    for f in memory["due"]:
        if f["date"] == iso_day:
            add(f["start"], f["time"], "followup", "Follow up: " + f["title"], f["who"] and "from " + f["who"])
    timeline.sort(key=lambda x: x["start"])
    for e in expenses["items"]:
        timeline.append({"start": "", "time": "", "kind": "expense",
                         "title": f"{e['amount_text']} {e['currency']} {e['note'] or e['category']}".strip(),
                         "sub": e["category"]})

    handled = len(replies) + len(comments_sent)
    approved = sum(1 for c in ap["today_cards"])
    numbers = {"spent": {cur: b["total"] for cur, b in expenses["by_currency"].items()},
               "spent_text": ", ".join(f"{b['total_text']} {cur}" for cur, b in expenses["by_currency"].items()) or "0",
               "expense_count": expenses["count"], "mails_handled": len(replies), "mail_unread": mail["unread"],
               "commits": len(commits), "cards_approved": approved, "cards_pending": ap["pending"],
               "meetings": len([m for m in meetings if not m["all_day"]]), "meetings_left": len(upcoming),
               "tasks_due": len(tasks["due_today"]), "tasks_overdue": len(tasks["overdue"]),
               "tasks_completed": tasks["completed"], "council_runs": len(council), "handled": handled,
               "followups_due": len(memory["due"]), "memory_tasks": len(memory["tasks"])}
    return {"date": iso_day, "weekday": now.strftime("%A"), "generated_at": now.isoformat(timespec="seconds"),
            "meetings": meetings, "tasks": {k: tasks[k] for k in ("due_today", "overdue", "updated", "commented")} |
            {"completed": tasks["completed"]},
            "expenses": expenses, "mail": mail, "done": done, "pending": pending, "timeline": timeline,
            "brain": memory, "proactive": _proactive_view(ctx), "applyq": _applyq_line(ctx),
            "numbers": numbers, "sources_failed": sorted(set(failed))}


def _applyq_line(ctx: Ctx) -> str:
    """One short line about the application queue (hub/applyq.py), "" when it is off, empty or missing."""
    try:
        from . import applyq
        return applyq.today_line(ctx.config)
    except Exception:  # noqa: BLE001 - optional
        return ""


def _proactive_view(ctx: Ctx) -> dict:
    """What Friday held back (quiet hours) or has queued, for the Today screen. Empty when proactive is not running."""
    engine = getattr(ctx, "proactive", None)
    try:
        view = engine.view() if engine is not None else None
    except Exception:  # noqa: BLE001
        view = None
    if not view:
        return {"held": [], "pending": 0}
    return {"held": [{"title": clean(x.get("title"), 120), "at": str(x.get("at") or "")[11:16]}
                     for x in view["events"] if x.get("decision") == "quiet" and x.get("title")][:5],
            "pending": len(view["pending"])}


# ---- the spoken summary (a template: no AI, deterministic) --------------------------------------------------------

def _names(rows: list[dict], key: str = "name", n: int = 2) -> str:
    names = [r[key] for r in rows[:n] if r.get(key)]
    return " and ".join(names)


def spoken(data: dict, style: str = "morning") -> str:
    """At most MAX_WORDS words of plain sentences. Less important sentences are dropped first."""
    style = style if style in STYLES else "morning"
    n = data.get("numbers") or {}
    meetings = [m for m in data.get("meetings") or [] if not m.get("all_day")]
    tasks = data.get("tasks") or {}
    done = data.get("done") or {}
    pending = data.get("pending") or {}
    # (priority 1 = keep longest, sentence)
    parts: list[tuple[int, str]] = []
    if style == "evening":
        parts.append((1, f"Here is how your {data.get('weekday', 'day')} went."))
        parts.append((1, f"You had {plural(len(meetings), 'meeting')}." if meetings else "You had no meetings."))
        commits = done.get("commits") or []
        if commits:
            repos = sorted({c["repo"] for c in commits})
            tail = f", including {commits[0]['subject']}" if commits[0].get("subject") else ""
            parts.append((1, f"You made {plural(len(commits), 'commit')} in {' and '.join(repos[:2])}{tail}."))
        else:
            parts.append((3, "No commits today."))
        replied = (data.get("mail") or {}).get("replied") or []
        if replied:
            parts.append((2, f"You replied to {plural(len(replied), 'mail')}, including {replied[0].get('to') or 'one person'}."))
        t = done.get("tasks") or {}
        did = len(t.get("commented") or []) + int(t.get("completed") or 0)
        if did:
            bits = []
            if t.get("completed"):
                bits.append(f"completed {plural(int(t['completed']), 'task')}")
            if t.get("commented"):
                bits.append(f"commented on {plural(len(t['commented']), 'task')}")
            parts.append((2, "In Asana you " + " and ".join(bits) + "."))
        if n.get("expense_count"):
            parts.append((1, f"You spent {_spent_words(data)} in {plural(n['expense_count'], 'payment')}."))
        else:
            parts.append((3, "You logged no spending."))
        if n.get("council_runs"):
            parts.append((3, f"You ran the council {plural(n['council_runs'], 'time')}."))
        if n.get("cards_approved"):
            parts.append((2, f"You approved {plural(n['cards_approved'], 'Autopilot card')}."))
        open_now = int(n.get("tasks_due") or 0) + int(n.get("tasks_overdue") or 0)
        if open_now:
            parts.append((1, f"Still open in Asana: {plural(open_now, 'task')} due today or overdue."))
        if pending.get("autopilot"):
            parts.append((1, f"{plural(pending['autopilot'], 'draft')} still waiting for your approval."))
        fu = (data.get("brain") or {}).get("due") or []
        if fu:
            parts.append((2, f"{plural(len(fu), 'follow-up')} still open, starting with {fu[0]['title']}."))
    else:
        parts.append((1, f"Here is your {data.get('weekday', 'day')}."))
        if meetings:
            nxt = pending.get("next_meeting")
            left = int(n.get("meetings_left") or 0)
            line = f"You have {plural(len(meetings), 'meeting')} today"
            if nxt:
                line += f", {plural(left, 'more')} to go, next is {nxt['title']} at {nxt['time']}"
            parts.append((1, line + "."))
        else:
            parts.append((2, "Your calendar is clear."))
        due, late = tasks.get("due_today") or [], tasks.get("overdue") or []
        if due or late:
            bits = []
            if due:
                bits.append(f"{len(due)} due today")
            if late:
                bits.append(f"{len(late)} overdue")
            line = "Tasks: " + " and ".join(bits)
            lead = _names(late or due, n=2)
            parts.append((1, line + (f", starting with {lead}." if lead else ".")))
        else:
            parts.append((3, "No tasks are due."))
        if n.get("expense_count"):
            parts.append((2, f"Spent so far: {_spent_words(data)}."))
        mail = data.get("mail") or {}
        if mail.get("unread"):
            parts.append((2, f"You have {plural(int(mail['unread']), 'unread mail')}."))
        if pending.get("autopilot"):
            parts.append((1, f"{plural(pending['autopilot'], 'draft')} waiting for your approval."))
        fu = (data.get("brain") or {}).get("due") or []
        if fu:
            parts.append((1, f"Follow-ups due: {len(fu)}, starting with {fu[0]['title']}" +
                          (f" for {fu[0]['who']}." if fu[0].get("who") else ".")))
        so_far = []
        if n.get("commits"):
            so_far.append(plural(n["commits"], "commit"))
        if n.get("mails_handled"):
            so_far.append(f"{plural(n['mails_handled'], 'mail')} answered")
        if so_far:
            parts.append((3, "So far today: " + " and ".join(so_far) + "."))
    if data.get("sources_failed"):
        parts.append((2, "I could not read " + ", ".join(data["sources_failed"]) + "."))
    # trim: drop the least important sentences (last first) until the text fits
    def words(items):
        return sum(len(s.split()) for _, s in items)
    for level in (3, 2):
        while words(parts) > MAX_WORDS and any(p == level for p, _ in parts):
            index = max(i for i, (p, _) in enumerate(parts) if p == level)
            parts.pop(index)
    text = " ".join(s for _, s in parts)
    out = text.split()
    return " ".join(out[:MAX_WORDS]) if len(out) > MAX_WORDS else text


def _spent_words(data: dict) -> str:
    by = (data.get("expenses") or {}).get("by_currency") or {}
    return " and ".join(money_spoken(b["total"], cur) for cur, b in list(by.items())[:2]) or "nothing"


def key_numbers(data: dict) -> dict:
    n = data.get("numbers") or {}
    return {k: n.get(k) for k in ("meetings", "tasks_due", "tasks_overdue", "spent_text", "mails_handled",
                                  "mail_unread", "commits", "cards_approved", "cards_pending")}


# ---- the router -----------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, caller: Caller | None = None, clock: Callable[[], dt.datetime] | None = None,
                git_runner=None, audit_reader=None) -> APIRouter:
    """`caller`, `clock`, `git_runner` and `audit_reader` exist so tests need no real hub, time or git."""
    router = APIRouter(prefix="/api/today", dependencies=[Depends(ctx.need_auth)])
    holder: dict[str, Any] = {}
    now_fn = clock or dt.datetime.now

    async def call(method: str, path: str, **kwargs):
        if caller is not None:
            return await caller(method, path, **kwargs)
        if "c" not in holder:
            holder["c"] = InProcessClient(ctx.get_app, ctx.config)
        return await holder["c"].call(method, path, **kwargs)

    def need_on() -> None:
        if not getattr(ctx.config, "today_enabled", True):
            raise HTTPException(404, "Today is switched off (today_enabled is false).")

    async def build() -> dict:
        return await collect(ctx, call, now_fn, git_runner=git_runner, audit_reader=audit_reader)

    @router.get("")
    @router.get("/")
    async def today():
        need_on()
        return await build()

    @router.get("/text")
    async def text(style: str = Query("morning", max_length=20)):
        need_on()
        if style not in STYLES:
            raise HTTPException(400, "style must be morning or evening.")
        data = await build()
        said = spoken(data, style)
        return {"text": said, "words": len(said.split()), "style": style, "numbers": key_numbers(data),
                "sources_failed": data["sources_failed"]}

    @router.get("/front-page", response_class=HTMLResponse)
    async def front_page():
        """Printable one-pager of the day (inline CSS, no external assets), from the same data as GET /api/today."""
        from . import frontpage
        need_on()
        data = await build()
        call = str(getattr(ctx.config, "display_name", "") or "Chief").strip() or "Chief"
        return HTMLResponse(frontpage.render(data, call), headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    return router
