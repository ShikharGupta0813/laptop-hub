"""Notification center: one list of "things the hub wants you to know", fed by every other module.

`make_router(ctx)` builds a NotifyCenter and REPLACES `ctx.notify` with its push function, so any
module that calls `ctx.notify(title, body="", level="info", link=None, source="")` at runtime lands
here. The push is synchronous and thread-safe (a lock around a small sqlite file), so it can be
called from the event loop, a worker thread or a background task alike.

Stored under `config.data_path()`: `notifications.db` (latest 500, nothing older than 60 days) and
`notify_settings.json`. If an ntfy topic is configured a copy of each notification is also pushed to
the phone from a short-lived background thread (5 second timeout, failures are logged, never raised).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .common import Ctx, JsonFile

log = logging.getLogger("hub.notify")

MAX_KEPT = 500
MAX_AGE_DAYS = 60
LEVELS = ("info", "warn", "error")
LEVEL_RANK = {name: i for i, name in enumerate(LEVELS)}
NTFY_PRIORITY = {"info": "3", "warn": "4", "error": "5"}
NTFY_TAGS = {"info": "information_source", "warn": "warning", "error": "rotating_light"}
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
LINK_RE = re.compile(r"^[\w\-./#?=&%]{1,200}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_NTFY_THREADS = 8
KEEPALIVE_SECONDS = 15.0


def _clean(text: Any, limit: int, single_line: bool = False) -> str:
    text = CONTROL_RE.sub("", str(text if text is not None else ""))
    if single_line:
        text = " ".join(text.split())
    return text.strip()[:limit]


def _clean_link(link: Any) -> str | None:
    """A link is either a view name inside the app ("reminders") or a plain http(s) address.
    Anything else (javascript:, data:...) is dropped so it can never become a clickable script."""
    if not link:
        return None
    link = str(link).strip()
    if link.lower().startswith(("http://", "https://")):
        return link[:500] if " " not in link and not CONTROL_RE.search(link) else None
    if link.startswith(("//", "/\\", "\\")):   # protocol-relative: another site, not a view
        return None
    return link if ":" not in link and LINK_RE.match(link) else None


def _http_send(url: str, headers: dict, content: bytes) -> None:
    import httpx
    response = httpx.post(url, headers=headers, content=content, timeout=5.0, follow_redirects=False)
    response.raise_for_status()


class NotifyCenter:
    def __init__(self, db_path: Path, settings_path: Path, *, config=None, audit: Callable[..., None] | None = None,
                 clock: Callable[[], float] = time.time, sender: Callable[[str, dict, bytes], None] | None = None):
        self.db_path = Path(db_path)
        self.settings = JsonFile(settings_path)
        self.config = config
        self._audit = audit or (lambda *a, **k: None)
        self._clock = clock
        self._sender = sender or _http_send
        self._lock = threading.RLock()
        self._ready = False
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._subs_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ---- storage ---------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=5.0)
        db.row_factory = sqlite3.Row
        if not self._ready:
            db.execute("CREATE TABLE IF NOT EXISTS notifications ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, title TEXT NOT NULL, "
                       "body TEXT NOT NULL DEFAULT '', level TEXT NOT NULL, link TEXT, "
                       "source TEXT NOT NULL DEFAULT '', read INTEGER NOT NULL DEFAULT 0)")
            db.commit()
            self._ready = True
        return db

    def _view(self, row: sqlite3.Row) -> dict:
        created = datetime.fromtimestamp(row["ts"]).astimezone().isoformat(timespec="seconds")
        return {"id": row["id"], "title": row["title"], "body": row["body"], "level": row["level"],
                "link": row["link"], "source": row["source"], "read": bool(row["read"]), "created": created}

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock, closing(self._connect()) as db:
            return db.execute(sql, args).fetchall()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock, closing(self._connect()) as db:
            cursor = db.execute(sql, args)
            db.commit()
            return cursor.rowcount

    # ---- push ------------------------------------------------------------------------------------

    def push(self, title: str, body: str = "", level: str = "info", link: str | None = None,
             source: str = "", phone: Any = True) -> dict | None:
        """Add a notification. Never raises: a broken notification must not break the caller.
        `phone`: True = the usual ntfy push, "silent" = a low-priority push, False = in-app list only (hub/presence.py)."""
        try:
            level = level if level in LEVEL_RANK else "info"
            title = _clean(title, 200, single_line=True) or "Notification"
            body = _clean(body, 2000)
            link = _clean_link(link)
            source = _clean(source, 40, single_line=True)
            now = self._clock()
            with self._lock, closing(self._connect()) as db:
                cursor = db.execute("INSERT INTO notifications (ts, title, body, level, link, source) "
                                    "VALUES (?, ?, ?, ?, ?, ?)", (now, title, body, level, link, source))
                new_id = cursor.lastrowid
                db.execute("DELETE FROM notifications WHERE ts < ?", (now - MAX_AGE_DAYS * 86400,))
                db.execute("DELETE FROM notifications WHERE id NOT IN "
                           "(SELECT id FROM notifications ORDER BY id DESC LIMIT ?)", (MAX_KEPT,))
                db.commit()
                row = db.execute("SELECT * FROM notifications WHERE id = ?", (new_id,)).fetchone()
            item = self._view(row)
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("Could not store a notification (%s).", type(exc).__name__)
            return None
        self._announce(item)
        if phone is not False:
            self._maybe_push_phone(item, now, silent=phone == "silent")
        return item

    __call__ = push

    # ---- reading ---------------------------------------------------------------------------------

    def list(self, *, unread_only: bool = False, limit: int = 50, source: str | None = None,
             after_id: int | None = None) -> list[dict]:
        where, args = [], []
        if unread_only:
            where.append("read = 0")
        if source:
            where.append("source = ?")
            args.append(source)
        if after_id is not None:
            where.append("id > ?")
            args.append(after_id)
        sql = "SELECT * FROM notifications" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY id " + ("ASC" if after_id is not None else "DESC") + " LIMIT ?"
        return [self._view(r) for r in self._query(sql, (*args, max(1, min(int(limit), 200))))]

    def unread_count(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM notifications WHERE read = 0")[0]["n"]

    def get(self, notification_id: int) -> dict | None:
        rows = self._query("SELECT * FROM notifications WHERE id = ?", (notification_id,))
        return self._view(rows[0]) if rows else None

    def mark_read(self, ids: list[int] | None = None) -> int:
        """Mark some (or, with ids=None, all) notifications read. Returns how many changed."""
        if ids is None:
            return self._write("UPDATE notifications SET read = 1 WHERE read = 0")
        ids = list(ids)[:MAX_KEPT]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        return self._write(f"UPDATE notifications SET read = 1 WHERE read = 0 AND id IN ({marks})", tuple(ids))

    def delete(self, notification_id: int) -> bool:
        return self._write("DELETE FROM notifications WHERE id = ?", (notification_id,)) > 0

    def clear(self) -> int:
        return self._write("DELETE FROM notifications")

    # ---- live stream -------------------------------------------------------------------------------

    def _announce(self, item: dict) -> None:
        """Hand the new item to every open /stream, whichever thread we are on."""
        with self._subs_lock:
            subs = list(self._subs)
        for loop, queue in subs:
            def put(queue=queue):
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    pass   # a stuck reader misses items rather than blocking everyone
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:   # that loop is closed; its stream is gone
                with self._subs_lock:
                    self._subs[:] = [s for s in self._subs if s[1] is not queue]

    @staticmethod
    def _frame(item: dict) -> str:
        return f"id: {item['id']}\ndata: {json.dumps(item)}\n\n"

    async def stream(self, last_id: int | None = None, *, keepalive: float = KEEPALIVE_SECONDS,
                     poll: float = 1.0, disconnected: Callable[[], Any] | None = None):
        """Server-sent events: every new notification, a comment line every `keepalive` seconds of
        silence, and (with Last-Event-ID) whatever was missed while the phone was reconnecting.
        Ends when the client goes away (the generator is cancelled or `disconnected()` turns true)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        entry = (asyncio.get_running_loop(), queue)
        with self._subs_lock:
            self._subs.append(entry)
        try:
            sent = last_id
            yield "retry: 3000\n\n"
            if last_id is not None:
                for item in self.list(limit=50, after_id=last_id):
                    sent = item["id"]
                    yield self._frame(item)
            quiet_since = time.monotonic()
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=poll)
                except asyncio.TimeoutError:
                    if disconnected is not None and await disconnected():
                        return
                    if time.monotonic() - quiet_since >= keepalive:
                        quiet_since = time.monotonic()
                        yield ": keepalive\n\n"
                    continue
                if sent is not None and item["id"] <= sent:
                    continue
                sent = item["id"]
                quiet_since = time.monotonic()
                yield self._frame(item)
        finally:
            with self._subs_lock:
                self._subs[:] = [s for s in self._subs if s is not entry]

    def subscriber_count(self) -> int:
        with self._subs_lock:
            return len(self._subs)

    # ---- settings --------------------------------------------------------------------------------

    def load_settings(self) -> dict:
        data = self.settings.load({})
        data = data if isinstance(data, dict) else {}
        quiet = data.get("quiet_hours")
        if not (isinstance(quiet, dict) and CLOCK_RE.match(str(quiet.get("start"))) and CLOCK_RE.match(str(quiet.get("end")))):
            quiet = None
        topic = data.get("ntfy_topic") if TOPIC_RE.match(str(data.get("ntfy_topic") or "")) else ""
        return {"ntfy_topic": topic, "quiet_hours": quiet,
                "min_level": data.get("min_level") if data.get("min_level") in LEVEL_RANK else "info"}

    def save_settings(self, settings: dict) -> None:
        self.settings.save(settings)

    def active_topic(self) -> tuple[str, str]:
        """(topic, where it came from). The settings page wins over hub.json."""
        mine = self.load_settings()["ntfy_topic"]
        if mine:
            return mine, "settings"
        from_config = str(getattr(self.config, "ntfy_topic", "") or "")
        return (from_config, "config") if from_config else ("", "")

    def settings_view(self) -> dict:
        settings = self.load_settings()
        topic, origin = self.active_topic()
        server = str(getattr(self.config, "ntfy_server", "") or "")
        valid = bool(TOPIC_RE.match(topic)) and server.lower().startswith(("http://", "https://"))
        return {"ntfy_enabled": valid, "ntfy_topic_hint": f"{topic[:2]}… ({len(topic)} characters)" if topic else "",
                "ntfy_topic_from": origin or None, "ntfy_server": server,
                "quiet_hours": settings["quiet_hours"], "min_level": settings["min_level"]}

    # ---- phone push (ntfy) -----------------------------------------------------------------------

    def _in_quiet_hours(self, quiet: dict | None, now: float) -> bool:
        if not quiet:
            return False
        local = datetime.fromtimestamp(now).astimezone()
        minutes = local.hour * 60 + local.minute
        start = int(quiet["start"][:2]) * 60 + int(quiet["start"][3:])
        end = int(quiet["end"][:2]) * 60 + int(quiet["end"][3:])
        if start == end:
            return False
        return start <= minutes < end if start < end else (minutes >= start or minutes < end)

    def _maybe_push_phone(self, item: dict, now: float, silent: bool = False) -> None:
        try:
            topic, _ = self.active_topic()
            server = str(getattr(self.config, "ntfy_server", "") or "").rstrip("/")
            if not topic or not TOPIC_RE.match(topic) or not server.lower().startswith(("http://", "https://")):
                return
            settings = self.load_settings()
            if LEVEL_RANK[item["level"]] < LEVEL_RANK[settings["min_level"]]:
                return
            if self._in_quiet_hours(settings["quiet_hours"], now):
                return
            headers = {"Title": item["title"].encode("ascii", "replace").decode("ascii")[:100] or "Hub",
                       "Priority": NTFY_PRIORITY[item["level"]], "Tags": NTFY_TAGS[item["level"]]}
            if silent:
                headers["Priority"] = "2"
            link = item["link"]
            if link and link.lower().startswith(("http://", "https://")):
                headers["Click"] = link
            content = (item["body"][:500] or item["title"]).encode("utf-8")
            with self._subs_lock:
                self._threads[:] = [t for t in self._threads if t.is_alive()]
                if len(self._threads) >= MAX_NTFY_THREADS:
                    log.warning("Too many phone pushes in flight; one was skipped.")
                    return
                thread = threading.Thread(target=self._send_phone, daemon=True, name="hub-ntfy",
                                          args=(f"{server}/{topic}", headers, content, item["level"], item["source"]))
                self._threads.append(thread)
            thread.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Phone push was not started (%s).", type(exc).__name__)

    def _send_phone(self, url: str, headers: dict, content: bytes, level: str, source: str) -> None:
        ok = True
        try:
            self._sender(url, headers, content)
        except Exception as exc:  # noqa: BLE001 - the URL holds the topic, so log only the kind of failure
            ok = False
            log.warning("Phone push failed (%s).", type(exc).__name__)
        self._audit("ntfy_push", level=level, source=source, ok=ok)

    def wait_for_pushes(self, timeout: float = 5.0) -> None:
        """For tests: wait until the background phone pushes have finished."""
        with self._subs_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)


# ---- routes ----------------------------------------------------------------------------------------

def _int_id(value: str) -> int:
    if not re.fullmatch(r"\d{1,12}", value or ""):
        raise HTTPException(404, "That notification does not exist.")
    return int(value)


def _bool(value: Any) -> bool:
    return value is True


def make_router(ctx: Ctx, *, clock: Callable[[], float] | None = None,
                sender: Callable[[str, dict, bytes], None] | None = None) -> APIRouter:
    data = ctx.config.data_path()
    center = NotifyCenter(data / "notifications.db", data / "notify_settings.json", config=ctx.config,
                          audit=lambda *a, **k: ctx.audit(*a, **k), clock=clock or time.time, sender=sender)
    ctx.notify = center.push      # every other module reaches us through ctx.notify at runtime
    router = APIRouter(prefix="/api/notifications", dependencies=[Depends(ctx.need_auth)])
    router.center = center        # type: ignore[attr-defined]  # so tests and other code can reach it

    @router.get("")
    @router.get("/")
    def list_notifications(unread_only: bool = False, limit: int = 50, source: str = ""):
        items = center.list(unread_only=unread_only, limit=limit, source=_clean(source, 40) or None)
        return {"notifications": items, "unread": center.unread_count()}

    @router.get("/unread-count")
    def unread_count():
        return {"count": center.unread_count()}

    @router.post("/read")
    def mark_read(body: dict = Body(default_factory=dict)):
        if _bool(body.get("all")):
            changed = center.mark_read(None)
        else:
            ids = body.get("ids")
            if (not isinstance(ids, list) or not ids or len(ids) > MAX_KEPT
                    or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
                raise HTTPException(400, "Say which notifications to mark as read (a list of ids, or all: true).")
            changed = center.mark_read(ids)
        return {"updated": changed, "unread": center.unread_count()}

    @router.post("/clear")
    def clear(body: dict = Body(default_factory=dict)):
        if not _bool(body.get("confirm")):
            raise HTTPException(409, "This empties the whole notification list. Send confirm: true to go ahead.")
        removed = center.clear()
        ctx.audit("notifications_cleared", count=removed)
        return {"removed": removed}

    @router.get("/stream")
    async def stream(request: Request):
        last = request.headers.get("last-event-id", "")
        last_id = int(last) if re.fullmatch(r"\d{1,12}", last) else None
        return StreamingResponse(center.stream(last_id, disconnected=request.is_disconnected),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.post("/test")
    def send_test():
        item = center.push("Test notification", "If you can read this, notifications work.",
                           level="info", source="notify")
        if item is None:
            raise HTTPException(500, "The test notification could not be saved. Check the hub's data folder.")
        return item

    @router.get("/settings")
    def get_settings():
        return center.settings_view()

    @router.post("/settings")
    def set_settings(body: dict = Body(default_factory=dict)):
        settings = center.load_settings()
        changed = []
        if "ntfy_topic" in body:
            topic = body["ntfy_topic"]
            if topic in (None, ""):
                settings["ntfy_topic"] = ""
            elif isinstance(topic, str) and TOPIC_RE.match(topic):
                settings["ntfy_topic"] = topic
            else:
                raise HTTPException(400, "The topic must be 8 to 64 letters, numbers, dashes or underscores. "
                                         "Make it long and random: anyone who knows an ntfy topic can read it.")
            changed.append("ntfy_topic")
        if "quiet_hours" in body:
            quiet = body["quiet_hours"]
            if quiet is None:
                settings["quiet_hours"] = None
            elif (isinstance(quiet, dict) and isinstance(quiet.get("start"), str) and isinstance(quiet.get("end"), str)
                  and CLOCK_RE.match(quiet["start"]) and CLOCK_RE.match(quiet["end"])):
                if quiet["start"] == quiet["end"]:
                    raise HTTPException(400, "Quiet hours need different start and end times.")
                settings["quiet_hours"] = {"start": quiet["start"], "end": quiet["end"]}
            else:
                raise HTTPException(400, "Quiet hours need a start and an end like 22:00 and 07:00.")
            changed.append("quiet_hours")
        if "min_level" in body:
            if body["min_level"] not in LEVEL_RANK:
                raise HTTPException(400, "The lowest level to push must be info, warn or error.")
            settings["min_level"] = body["min_level"]
            changed.append("min_level")
        if changed:
            center.save_settings(settings)
            ctx.audit("notify_settings_changed", fields=changed)   # the topic itself is never logged
        return center.settings_view()

    @router.delete("/{notification_id}")
    def delete_one(notification_id: str):
        if not center.delete(_int_id(notification_id)):
            raise HTTPException(404, "That notification does not exist.")
        return {"ok": True}

    return router
