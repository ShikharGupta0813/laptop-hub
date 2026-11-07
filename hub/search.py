"""Global search and the Home dashboard.

Search asks every feature's own `GET /api/<feature>/search?q=` at the same time (through the in-process
client, so each feature's checks still apply), adds notes, apps and settings, and merges the answers into one
ranked list. A feature that is missing, slow (2.5 s) or broken is simply left out.

Home builds the whole dashboard in one call. Every section is built on its own, cached for 20 seconds, and a
section that fails is replaced by an empty value and named in `sections_failed`, so Home is always a 200.

Routes live under /api (the router prefix is just /api) because Home is /api/home. The files module's own
`GET /api/search?root=&q=` is kept working: when `root` is given the request is a file-name search.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from . import files
from .common import InProcessClient, JsonFile, Ctx

SOURCES = (   # (name, routes tried in order until one exists, default kind)
    ("kb", ("/api/kb/search",), "kb"),
    ("fileops", ("/api/fileops/search",), "file"),
    ("reminders", ("/api/reminders/search",), "reminder"),
    ("todos", ("/api/todos/search", "/api/productivity/todos/search"), "todo"),
    ("journal", ("/api/journal/search", "/api/productivity/journal/search"), "journal"),
    ("bookmarks", ("/api/bookmarks/search", "/api/life/bookmarks/search"), "bookmark"),
    ("snippets", ("/api/snippets/search", "/api/life/snippets/search"), "snippet"),
    ("expenses", ("/api/expenses/search", "/api/life/expenses/search"), "expense"),
    ("cards", ("/api/cards/search",), "card"),
    ("automations", ("/api/automations/search",), "automation"),
    ("inbox", ("/api/inbox/search",), "inbox"),
    ("watch", ("/api/watch/search",), "alert"),
    ("workspaces", ("/api/workspaces/search",), "workspace"),
)
BUILTIN_SOURCES = ("notes", "apps", "settings")
MIN_Q, MAX_Q = 2, 200
DEFAULT_LIMIT, MAX_LIMIT = 30, 100
PER_SOURCE_MAX = 20
RECENT_MAX = 10
KIND_OK = re.compile(r"\A[a-z][a-z0-9_-]{0,29}\Z")

SETTINGS_PAGES = (   # (title, section, keywords)
    ("Models", "models", ("model", "ollama", "chat model", "qwen", "whisper", "embed", "claude", "download", "pull", "ai")),
    ("Shared folders", "folders", ("roots", "folders", "ask my files", "knowledge", "writable", "files", "kb")),
    ("Push notifications", "notifications", ("ntfy", "push", "topic", "alerts", "phone")),
    ("Backup", "backup", ("backup", "export", "zip", "save", "restore", "copy")),
    ("Audit log", "audit", ("audit", "log", "history", "activity", "security", "who did")),
    ("Diagnostics", "diagnostics", ("doctor", "check", "health", "fix", "problems", "troubleshoot")),
    ("About", "about", ("version", "about", "uptime", "python")),
)


# ---- ranking -------------------------------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def title_score(query: str, title: str) -> float:
    """exact 100 > prefix 80 > word prefix 65 > substring 50 > all words 40 > fuzzy 20..35 > 0."""
    q, t = query.lower().strip(), title.lower().strip()
    if not q or not t:
        return 0.0
    if t == q:
        return 100.0
    if t.startswith(q):
        return 80.0
    tw = _words(t)
    if any(w.startswith(q) for w in tw):
        return 65.0
    if q in t:
        return 50.0
    qw = _words(q)
    if len(qw) > 1 and all(any(w.startswith(x) for w in tw) for x in qw):
        return 40.0
    best = 0.0
    for w in [t[:60], *tw[:12]]:
        best = max(best, SequenceMatcher(None, q[:40], w).ratio())
    return 20.0 + (best - 0.72) * 50 if best >= 0.72 else 0.0


def _clip(value: Any, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def clean_result(item: Any, default_kind: str) -> dict | None:
    if not isinstance(item, dict) or not str(item.get("title") or "").strip():
        return None
    kind = item.get("kind")
    kind = kind if isinstance(kind, str) and KIND_OK.match(kind) else default_kind
    opened = item.get("open")
    opened = ({k: _clip(opened[k], 200) for k in ("view", "section", "id") if opened.get(k) not in (None, "")}
              if isinstance(opened, dict) else {})
    return {"title": _clip(item["title"], 200).strip(), "snippet": _clip(item.get("snippet"), 300),
            "kind": kind, "open": opened}


def _snippet_around(text: str, needle: str, width: int = 140) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    at = flat.lower().find(needle.lower())
    if at < 0:
        return flat[:width]
    start = max(0, at - width // 3)
    return ("..." if start else "") + flat[start:start + width]


def merge_results(query: str, groups: list[tuple[int, list[dict]]]) -> list[dict]:
    """groups: (source order, cleaned results in the source's own order). Ranked, deduped, stable."""
    ranked: dict[tuple, tuple] = {}
    for order, items in groups:
        for idx, item in enumerate(items):
            match = title_score(query, item["title"])
            if match == 0 and query.lower() in item.get("snippet", "").lower():
                match = 12.0
            score = max(match, 10.0) + max(0.0, 3.0 - idx * 0.15) + item.pop("_bonus", 0.0)
            key = (item["kind"], json.dumps(item["open"], sort_keys=True) if item["open"] else item["title"].lower())
            sortkey = (-score, order, idx)
            if key not in ranked or sortkey < ranked[key][0]:
                ranked[key] = (sortkey, {**item, "score": round(score, 1)})
    return [entry for _, entry in sorted(ranked.values(), key=lambda pair: pair[0])]


# ---- the router ------------------------------------------------------------------------------------------------

class SectionError(Exception):
    pass


def make_router(ctx: Ctx, *, client: Any = None, timeout: float = 2.5, section_timeout: float = 3.0,
                home_ttl: float = 20.0, clock=time.monotonic, now=datetime.now) -> APIRouter:
    """`client` (anything with `async call(method, path, params=, body=)`), the timeouts, the clocks are for tests."""
    router = APIRouter(prefix="/api", dependencies=[Depends(ctx.need_auth)])
    config = ctx.config
    if client is None and ctx.get_app is not None:
        client = InProcessClient(ctx.get_app, config)
    recent_file = JsonFile(config.data_path() / "search_recent.json")
    recent_lock = threading.Lock()

    # ---- search sources -------------------------------------------------------------------------------------------

    async def first_route(routes: tuple[str, ...], q: str):
        ok, data = False, None
        for route in routes:
            ok, data = await client.call("GET", route, params={"q": q})
            if ok:
                break
        return ok, data

    async def ask_source(name: str, routes: tuple[str, ...], kind: str, q: str):
        """(results, timed_out). A missing route, an error or a slow answer gives no results."""
        if client is None:
            return [], False
        try:
            ok, data = await asyncio.wait_for(first_route(routes, q), timeout)
        except asyncio.TimeoutError:
            return [], True
        except Exception:  # noqa: BLE001 - one broken feature must not break search
            return [], False
        raw = data.get("results") if ok and isinstance(data, dict) else None
        if not isinstance(raw, list):
            return [], False
        cleaned = [r for r in (clean_result(item, kind) for item in raw[:PER_SOURCE_MAX]) if r]
        return cleaned, False

    async def search_notes(q: str) -> list[dict]:
        if ctx.notes is None:
            return []
        try:
            notes = await asyncio.to_thread(ctx.notes.all)
        except Exception:  # noqa: BLE001
            return []
        words = _words(q)
        out = []
        for note in notes:   # newest first, which the merge keeps as the tie-break
            hay = f"{note.title}\n{' '.join(note.tags)}\n{note.body}".lower()
            if q.lower() not in hay and not (len(words) > 1 and all(w in hay for w in words)):
                continue
            tag_hit = any(q.lower() == t for t in note.tags)
            out.append({"title": note.title, "kind": "note", "open": {"view": "notes", "id": note.id},
                        "snippet": _snippet_around(note.body or note.title, q) if q.lower() in note.body.lower()
                        else (" ".join(f"#{t}" for t in note.tags) or note.body[:140]),
                        "_bonus": 25.0 if tag_hit else 0.0})
            if len(out) >= PER_SOURCE_MAX:
                break
        return out

    def search_apps(q: str) -> list[dict]:
        out = []
        for name in config.apps:
            if title_score(q, name):
                out.append({"title": name, "kind": "app", "snippet": "Open this app on the laptop",
                            "open": {"view": "apps", "section": "launch", "id": name}})
        for name, spec in config.scripts.items():
            label = str(spec.get("label", name)) if isinstance(spec, dict) else name
            if title_score(q, label) or title_score(q, name):
                out.append({"title": label, "kind": "button", "snippet": "Quick button",
                            "open": {"view": "apps", "section": "buttons", "id": name}})
        return out[:PER_SOURCE_MAX]

    def search_settings(q: str) -> list[dict]:
        out = []
        for title, section, keywords in SETTINGS_PAGES:
            hit = title_score(q, title)
            if not hit:
                ql = q.lower()
                if ql in keywords:
                    hit = 45.0
                elif any(k.startswith(ql) for k in keywords):
                    hit = 35.0
                elif any(ql in k for k in keywords):
                    hit = 25.0
            if hit:
                out.append({"title": title, "kind": "setting", "snippet": "Settings: " + ", ".join(keywords[:4]),
                            "open": {"view": "settings", "section": section},
                            "_bonus": 0.0 if title_score(q, title) else hit - 10})
        return out

    async def global_search(q: str, kinds: str, limit: int) -> dict:
        q = q.strip()
        if len(q) < MIN_Q:
            raise HTTPException(400, "Type at least 2 letters to search.")
        if len(q) > MAX_Q:
            raise HTTPException(400, "That search is too long. Use under 200 characters.")
        wanted = {k.strip().lower() for k in kinds.split(",") if k.strip()}

        def use(*names: str) -> bool:
            return not wanted or any(n in wanted for n in names)

        jobs, order = [], []
        for pos, (name, routes, kind) in enumerate(SOURCES):
            if use(name, kind):
                jobs.append(ask_source(name, routes, kind, q))
                order.append((pos, name))
        outcomes = await asyncio.gather(*jobs)
        groups: list[tuple[int, list[dict]]] = []
        timed_out = []
        for (pos, name), (results, slow) in zip(order, outcomes):
            groups.append((pos, results))
            if slow:
                timed_out.append(name)
        base = len(SOURCES)
        if use("notes", "note"):
            groups.append((base, await search_notes(q)))
        if use("apps", "app", "button"):
            groups.append((base + 1, search_apps(q)))
        if use("settings", "setting"):
            groups.append((base + 2, search_settings(q)))
        merged = merge_results(q, groups)
        if wanted:
            merged = [r for r in merged if r["kind"] in wanted or _source_of(r["kind"]) in wanted]
        counts: dict[str, int] = {}
        for r in merged:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        return {"query": q, "results": merged[:limit], "groups": counts, "total": len(merged), "timed_out": timed_out}

    def _source_of(kind: str) -> str:
        for name, _route, default in SOURCES:
            if default == kind:
                return name
        return {"note": "notes", "app": "apps", "button": "apps", "setting": "settings"}.get(kind, "")

    @router.get("/search")
    async def search(q: str = Query("", max_length=1000), kinds: str = Query("", max_length=300),
                     limit: int = Query(DEFAULT_LIMIT, ge=1, le=1000), root: str = Query("", max_length=100),
                     path: str = Query("", max_length=1000)):
        if root:   # the files module's original "find a file by name" call
            try:
                return {"results": await asyncio.to_thread(files.search, config.roots, root, q, path)}
            except files.FileError as exc:
                raise HTTPException(getattr(exc, "status", 500), getattr(exc, "message", str(exc))) from None
        return await global_search(q, kinds, min(limit, MAX_LIMIT))

    @router.get("/search/all")
    async def search_all(q: str = Query("", max_length=1000), kinds: str = Query("", max_length=300),
                         limit: int = Query(DEFAULT_LIMIT, ge=1, le=1000)):
        """Same as GET /api/search without `root`; always reachable even while the files route owns /api/search."""
        return await global_search(q, kinds, min(limit, MAX_LIMIT))

    # ---- recent queries ---------------------------------------------------------------------------------------------

    def recent_list() -> list[str]:
        data = recent_file.load([])
        return [s for s in data if isinstance(s, str)][:RECENT_MAX] if isinstance(data, list) else []

    @router.get("/search/suggest")
    def suggest(q: str = Query("", max_length=200)):
        needle = q.strip().lower()
        recent = [r for r in recent_list() if not needle or r.lower().startswith(needle)]
        names = [t for t, _s, _k in SETTINGS_PAGES] + list(config.apps) + [
            str(s.get("label", n)) if isinstance(s, dict) else n for n, s in config.scripts.items()]
        titles = [n for n in names if needle and n.lower().startswith(needle)][:6]
        return {"recent": recent, "suggestions": titles}

    @router.post("/search/recent")
    def record_recent(body: dict = Body(...)):
        q = str(body.get("q", "")).strip()
        if not MIN_Q <= len(q) <= MAX_Q:
            raise HTTPException(400, "Type at least 2 letters to search.")
        with recent_lock:
            items = [r for r in recent_list() if r.lower() != q.lower()]
            items.insert(0, q)
            recent_file.save(items[:RECENT_MAX])
            return {"recent": items[:RECENT_MAX]}

    @router.delete("/search/recent")
    def clear_recent():
        with recent_lock:
            recent_file.save([])
        return {"recent": []}

    # ---- home ---------------------------------------------------------------------------------------------------------

    cache: dict[str, tuple[float, Any]] = {}

    class Memo:
        """Calls made during one Home request, shared between sections (the productivity summary is used 3 times)."""

        def __init__(self):
            self.calls: dict[str, asyncio.Future] = {}

        async def get(self, path: str, params: dict | None = None) -> Any:
            key = path + "?" + json.dumps(params or {}, sort_keys=True)
            if key not in self.calls:
                future = asyncio.ensure_future(self._fetch(path, params))
                future.add_done_callback(lambda f: f.cancelled() or f.exception())
                self.calls[key] = future
            return await asyncio.shield(self.calls[key])

        async def _fetch(self, path: str, params: dict | None) -> Any:
            if client is None:
                raise SectionError("no client")
            try:
                ok, data = await asyncio.wait_for(client.call("GET", path, params=params), section_timeout)
            except asyncio.TimeoutError:
                raise SectionError("timed out") from None
            if not ok:
                raise SectionError(str(data))
            return data

        async def try_get(self, *paths: str, params: dict | None = None) -> Any:
            """The answer of the first of `paths` that works, or None when none does."""
            for path in paths:
                try:
                    return await self.get(path, params)
                except SectionError:
                    continue
            return None

    def items_of(data: Any, *keys: str) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in keys:
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def parse_when(value: Any) -> datetime | None:
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        return when

    def int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def scalars(data: Any) -> dict:
        """A feature summary reduced to plain values so Home stays small."""
        out: dict = {}
        if not isinstance(data, dict):
            return out
        for key, value in list(data.items())[:40]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[str(key)[:40]] = value[:200] if isinstance(value, str) else value
            elif isinstance(value, dict):
                inner = {str(k)[:40]: v for k, v in list(value.items())[:12]
                         if isinstance(v, (int, float, bool)) or v is None or (isinstance(v, str) and len(v) <= 200)}
                out[str(key)[:40]] = inner
            elif isinstance(value, list) and len(value) <= 10:
                kept = [v if not isinstance(v, dict) else {str(k)[:40]: x for k, x in list(v.items())[:8]
                                                            if isinstance(x, (int, float, bool, str)) or x is None}
                        for v in value if isinstance(v, (str, int, float, bool, dict))]
                if len(kept) == len(value):
                    out[str(key)[:40]] = kept
        return out

    async def s_greeting(memo: Memo):
        hour = now().hour
        period = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 17 else "evening" if 17 <= hour < 22 else "night"
        out: dict = {"period": period}
        status = await memo.try_get("/api/work/status")
        user = ((status or {}).get("asana") or {}).get("user") if isinstance(status, dict) else None
        if isinstance(user, str) and user.strip():
            out["name"] = user.strip().split()[0][:40]
        return out

    async def s_laptop(memo: Memo):
        data = await memo.get("/api/status")
        battery = data.get("battery") if isinstance(data, dict) else None
        memory = data.get("memory") if isinstance(data, dict) else None
        return {"online": True,
                "battery": ({"percent": battery.get("percent"), "plugged": battery.get("plugged")}
                            if isinstance(battery, dict) else None),
                "cpu": data.get("cpu_percent") if isinstance(data, dict) else None,
                "mem": memory.get("percent") if isinstance(memory, dict) else None}

    async def s_agenda(memo: Memo):
        data = await memo.get("/api/work/calendar", {"days": 1})
        out = []
        for m in items_of(data, "meetings", "events")[:8]:
            if isinstance(m, dict) and m.get("title"):
                out.append({"title": _clip(m["title"], 120), "start": _clip(m.get("start"), 40), "end": _clip(m.get("end"), 40),
                            "all_day": bool(m.get("all_day")), "link": _clip(m.get("link"), 300),
                            "platform": _clip(m.get("platform"), 20)})
        return out

    async def s_tasks(memo: Memo):
        asana, todos = await asyncio.gather(memo.try_get("/api/work/asana/tasks", params={"filter": "all"}),
                                            memo.try_get("/api/todos", "/api/productivity/todos", params={"done": "false"}))
        if asana is None and todos is None:
            raise SectionError("no task source")
        today = now().date().isoformat()
        rows = []
        for t in items_of(asana, "tasks"):
            if isinstance(t, dict) and t.get("name") and not t.get("completed"):
                due = _clip(t.get("due_on"), 10) or None
                rows.append({"title": _clip(t["name"], 140), "due": due, "source": "asana", "overdue": bool(due and due < today),
                             "open": {"view": "work", "section": "tasks", "id": _clip(t.get("gid"), 60)}})
        for t in items_of(todos, "todos", "items", "tasks"):
            if isinstance(t, dict) and (t.get("title") or t.get("text")) and not (t.get("done") or t.get("completed")):
                due = (_clip(t.get("due") or t.get("due_on") or t.get("due_date"), 10)) or None
                rows.append({"title": _clip(t.get("title") or t.get("text"), 140), "due": due, "source": "todo",
                             "overdue": bool(due and due < today),
                             "open": {"view": "notes", "section": "todos", "id": _clip(t.get("id"), 60)}})
        rows.sort(key=lambda r: (r["due"] is None, r["due"] or ""))
        return rows[:5]

    async def s_reminders(memo: Memo):
        data = await memo.get("/api/reminders")
        if isinstance(data, dict):   # {"reminders": [...]} or similar
            data = next((v for v in data.values() if isinstance(v, list)), [])
        rows = []
        for r in data if isinstance(data, list) else []:
            if not isinstance(r, dict) or any(r.get(k) is True for k in ("done", "completed", "fired")):
                continue
            text = next((r[k] for k in ("title", "text", "message", "name") if r.get(k)), "")
            when = next((str(r[k]) for k in ("when", "due", "due_at", "at", "fire_at", "remind_at", "next_run", "time") if r.get(k)), "")
            if text:
                rows.append({"id": _clip(r.get("id"), 60), "title": _clip(text, 140), "when": when[:40],
                             "open": {"view": "reminders", "id": _clip(r.get("id"), 60)}})
        rows.sort(key=lambda r: parse_when(r["when"]) or datetime.max)   # stable: unparsable times keep the feature's order
        return rows[:5]

    async def s_notifications(memo: Memo):
        data = await memo.try_get("/api/notifications", "/api/notify")
        if data is None:
            raise SectionError("no notifications")
        items = [n for n in items_of(data, "notifications", "items") if isinstance(n, dict)]
        unread = int_or_none(data.get("unread")) if isinstance(data, dict) else None
        if unread is None:
            unread = sum(1 for n in items if not n.get("read"))
        latest = [{"id": _clip(n.get("id"), 60), "title": _clip(n.get("title"), 120), "body": _clip(n.get("body"), 200),
                   "level": _clip(n.get("level") or "info", 10), "created": _clip(n.get("created") or n.get("time"), 40),
                   "read": bool(n.get("read"))} for n in items[:3]]
        return {"unread": unread, "latest": latest}

    def streak_in(summary: Any, name: str, *extra: str) -> int | None:
        """A streak from a feature summary: {"streaks": {name: n}}, "<name>_streak", or one of the `extra` keys."""
        if not isinstance(summary, dict):
            return None
        streaks = summary.get("streaks")
        for candidate in (streaks.get(name) if isinstance(streaks, dict) else None, summary.get(f"{name}_streak"),
                          *(summary.get(k) for k in extra)):
            if int_or_none(candidate) is not None:
                return candidate
        return None

    async def s_streaks(memo: Memo):
        learn, english, prod, focus = await asyncio.gather(
            memo.try_get("/api/learn/history"), memo.try_get("/api/english/overview"),
            memo.try_get("/api/productivity/summary"), memo.try_get("/api/focus/stats"))
        if learn is None and english is None and prod is None and focus is None:
            raise SectionError("no streak source")
        return {"learn": int_or_none(learn.get("streak_days")) if isinstance(learn, dict) else None,
                "english": int_or_none(english.get("streak_days")) if isinstance(english, dict) else None,
                "habits": streak_in(prod, "habits", "best_streak"), "focus": streak_in(focus, "focus", "streak_days"),
                "journal": streak_in(prod, "journal")}

    async def s_summaries(memo: Memo):
        prod, life, cards = await asyncio.gather(memo.try_get("/api/productivity/summary"), memo.try_get("/api/life/summary"),
                                                 memo.try_get("/api/cards/summary"))
        if prod is None and life is None and cards is None:
            raise SectionError("no summaries")
        return {"productivity": scalars(prod) if prod is not None else None, "life": scalars(life) if life is not None else None,
                "cards": scalars(cards) if cards is not None else None}

    async def s_journal_today(memo: Memo):
        """True when there is a journal entry today (None when that cannot be known). Not shown, only used for hints."""
        data = await memo.get("/api/journal/stats", {"days": 1})
        days = data.get("by_day") if isinstance(data, dict) else None
        if isinstance(days, list) and days and isinstance(days[-1], dict) and int_or_none(days[-1].get("entries")) is not None:
            return days[-1]["entries"] > 0
        raise SectionError("unknown")

    async def s_jarvis(memo: Memo):
        data = await memo.get("/api/jarvis/status")
        return {"running": bool(data.get("running")) if isinstance(data, dict) else False}

    async def s_recent_notes(memo: Memo):
        if ctx.notes is None:
            raise SectionError("no notes")
        notes = await asyncio.to_thread(ctx.notes.all)
        return [{"id": n.id, "title": n.title, "created": n.created, "tags": n.tags, "open": {"view": "notes", "id": n.id}}
                for n in notes[:3]]

    SECTIONS = (   # (name, builder, value to show when it cannot be built)
        ("greeting", s_greeting, lambda: {"period": "day"}),
        ("laptop", s_laptop, lambda: {"online": True, "battery": None, "cpu": None, "mem": None}),
        ("agenda", s_agenda, list),
        ("tasks", s_tasks, list),
        ("reminders", s_reminders, list),
        ("notifications", s_notifications, lambda: {"unread": 0, "latest": []}),
        ("streaks", s_streaks, lambda: {k: None for k in ("learn", "english", "habits", "focus", "journal")}),
        ("summaries", s_summaries, lambda: {"productivity": None, "life": None, "cards": None}),
        ("jarvis", s_jarvis, lambda: {"running": False}),
        ("recent_notes", s_recent_notes, list),
        ("_journal_today", s_journal_today, lambda: None),   # only feeds the hints; never reported
    )

    async def build_section(name: str, builder, default, memo: Memo):
        hit = cache.get(name)
        if hit and clock() - hit[0] < home_ttl:
            return name, hit[1], False
        try:
            value = await asyncio.wait_for(builder(memo), section_timeout + 1)
        except Exception:  # noqa: BLE001 - a broken section must not take Home down
            return name, default(), True
        cache[name] = (clock(), value)
        return name, value, False

    def first_positive(data: dict, *keys: str) -> int:
        return next((data[k] for k in keys if int_or_none(data.get(k)) and data[k] > 0), 0)

    def suggestions(sections: dict, journal_today: bool | None) -> list[str]:
        """Up to four rule-based hints (no model): next meeting, overdue work, due cards, habits, journal, unread."""
        out: list[str] = []
        moment = now()
        for m in sections["agenda"]:
            start = parse_when(m.get("start"))
            if start and not m["all_day"] and timedelta(0) <= start - moment <= timedelta(minutes=60):
                out.append(f"{m['title']} starts at {start:%H:%M}. Open the join link.")
                break
        prod = sections["summaries"].get("productivity") or {}
        overdue = [t for t in sections["tasks"] if t.get("overdue")]
        count = max(len(overdue), first_positive(prod, "overdue"))
        if overdue:
            more = f" and {count - 1} more" if count > 1 else ""
            out.append(f"Finish the overdue task \"{overdue[0]['title']}\"{more}.")
        elif count:
            out.append(f"You have {count} overdue task{'s' if count != 1 else ''}.")
        cards = sections["summaries"].get("cards") or {}
        due = first_positive(cards, "due_today", "due", "due_count", "cards_due")
        if due:
            out.append(f"{due} flashcard{'s are' if due != 1 else ' is'} due for review.")
        left = first_positive(prod, "habits_unchecked", "habits_remaining", "habits_pending", "habits_left")
        total, done = int_or_none(prod.get("habits_total")), int_or_none(prod.get("habits_done_today"))
        if not left and total is not None and done is not None:
            left = max(0, total - done)
        if left:
            out.append(f"{left} habit{'s are' if left != 1 else ' is'} not checked off yet today.")
        if journal_today is False or prod.get("journal_today") is False:
            out.append("Write today's journal entry.")
        if sections["notifications"]["unread"]:
            n = sections["notifications"]["unread"]
            out.append(f"You have {n} unread notification{'s' if n != 1 else ''}.")
        return out[:4]

    @router.get("/home")
    async def home(model: str = Query("local", max_length=20)):
        """`model` is accepted for the app's convention but Home never calls a model."""
        memo = Memo()
        built = await asyncio.gather(*(build_section(n, b, d, memo) for n, b, d in SECTIONS))
        sections = {name: value for name, value, _failed in built}
        failed = [name for name, _value, was_failed in built if was_failed and not name.startswith("_")]
        moment = now()
        return {"greeting": sections["greeting"], "now": moment.strftime("%Y-%m-%dT%H:%M:%S"),
                "laptop": sections["laptop"], "agenda": sections["agenda"], "tasks": sections["tasks"],
                "reminders": sections["reminders"], "notifications": sections["notifications"],
                "streaks": sections["streaks"], "summaries": sections["summaries"], "jarvis": sections["jarvis"],
                "recent_notes": sections["recent_notes"],
                "suggestions": suggestions(sections, sections["_journal_today"]),
                "sections_failed": failed}

    return router
