"""Chat history that survives a hub restart (GitHub issue #16).

One small SQLite file, `chat_history.sqlite`, in the hub data folder. Per conversation (session id) it keeps the owner's
words and the assistant's final answer, plus a short "used: tool names" note. Never raw tool output (it can hold mail
bodies or secrets) and never an answer that was written after reading untrusted content. History is context only: it is
loaded into the in-memory conversation, it never runs anything.

Thread-safe, never raises: a broken file means "no persistence" (logged once). Callers on the event loop use
asyncio.to_thread for save_turn()/load(); every call is one tiny statement.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

FILE = "chat_history.sqlite"
MAX_ROWS = 40             # last N messages kept per conversation (a turn is a user row plus an assistant row)
MAX_TEXT = 4000           # characters kept of any one message
DEFAULT_DAYS = 30


class ChatStore:
    def __init__(self, path: Path, *, days: float = DEFAULT_DAYS, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.days = days
        self._clock = clock
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._warned = False

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
            try:
                db.execute("CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, "
                           "provider TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '', "
                           "at REAL NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS chat_session ON chat (session, id)")
                db.commit()
            except Exception:
                db.close()
                raise
            self._db = db
        return self._db

    def _fail(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning("chat history file is unusable (%s); conversations will not be kept across restarts",
                        type(exc).__name__)
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = None

    def save_turn(self, session: str, provider: str, user_text: str, answer: str, tools: list[str] | None = None) -> None:
        """Keep one finished turn. `answer` "" means: keep only what the owner said."""
        now = self._clock()
        note = ("used: " + ", ".join(dict.fromkeys(str(t)[:60] for t in (tools or []) if t)))[:300] if tools else ""
        rows = [(session, provider, "user", str(user_text)[:MAX_TEXT], "", now)]
        if answer:
            rows.append((session, provider, "assistant", str(answer)[:MAX_TEXT], note, now))
        try:
            with self._lock:
                db = self._conn()
                db.executemany("INSERT INTO chat (session, provider, role, text, tools, at) VALUES (?,?,?,?,?,?)", rows)
                db.execute("DELETE FROM chat WHERE session = ? AND id NOT IN "
                           "(SELECT id FROM chat WHERE session = ? ORDER BY id DESC LIMIT ?)", (session, session, MAX_ROWS))
                db.execute("DELETE FROM chat WHERE session IN (SELECT session FROM chat GROUP BY session HAVING MAX(at) < ?)",
                           (now - self.days * 86400,))
                db.commit()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def load(self, session: str) -> list[tuple[str, str, str]]:
        """[(provider, role, text)] oldest first. [] when there is nothing, it is too old, or the file is broken."""
        try:
            with self._lock:
                rows = self._conn().execute("SELECT provider, role, text, at FROM chat WHERE session = ? ORDER BY id",
                                            (session,)).fetchall()
            if not rows or max(float(r[3]) for r in rows) < self._clock() - self.days * 86400:
                return []                                        # idle too long: same rule the prune uses
            return [(str(p), str(r), str(t)) for p, r, t, _ in rows]
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return []

    def clear(self, session: str) -> None:
        try:
            with self._lock:
                db = self._conn()
                db.execute("DELETE FROM chat WHERE session = ?", (session,))
                db.commit()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
