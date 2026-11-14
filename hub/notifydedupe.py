"""One push per message: the small registry Watch (hub/watch.py) and Alerts (hub/alerts.py) share.

Both features can see the same Gmail / Google Chat message (the Android capture posts it to Watch, and Watch hands it to
Alerts). Whoever is about to PUSH a message first `claim`s it here; the other one finds the claim and stays quiet
for that message (it still records the alert so it is listed on its screen). The key is source + sender + a hash of the
normalised text, so the same message through either pipeline lands on the same key. A claim lasts WINDOW_SECONDS
(10 minutes); after that the same text may notify again.

Nothing here pushes or stores anything on disk; it is process memory only.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Any, Callable

WINDOW_SECONDS = 10 * 60
MAX_KEYS = 500

_SOURCES = {"chat": "chat", "gchat": "chat", "mail": "mail", "gmail": "mail", "email": "mail", "outlook": "mail"}


def _norm(value: Any) -> str:
    return " ".join(re.sub(r"[^\w\s@.:/-]", " ", str(value or "").lower()).split())


def key_of(source: str, sender: str, text: str) -> str:
    """Same key for Watch's "chat"/"mail" and Alerts' "gchat"/"gmail"; other sources keep their own name."""
    src = _SOURCES.get(_norm(source), _norm(source))
    digest = hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()[:16]
    return f"{src}\x1f{_norm(sender)}\x1f{digest}"


class Registry:
    def __init__(self, clock: Callable[[], float] | None = None):
        self.clock = clock or time.time
        self._claims: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for k in [k for k, (t, _o) in self._claims.items() if now - t >= WINDOW_SECONDS]:
            del self._claims[k]
        while len(self._claims) > MAX_KEYS:
            del self._claims[next(iter(self._claims))]

    def held_by(self, source: str, sender: str, text: str) -> str:
        """Who holds a fresh claim on this message ("watch", "alerts") or ""."""
        with self._lock:
            self._expire(self.clock())
            return (self._claims.get(key_of(source, sender, text)) or (0, ""))[1]

    def claim(self, source: str, sender: str, text: str, owner: str) -> str:
        """Take the message for `owner` unless somebody holds it. Returns the holder (== owner when it is ours)."""
        key = key_of(source, sender, text)
        with self._lock:
            now = self.clock()
            self._expire(now)
            held = self._claims.get(key)
            if held:
                return held[1]
            self._claims[key] = (now, owner)
            return owner


def registry_for(ctx) -> Registry:
    """The registry of this hub (kept on the Ctx so tests and instances do not share state)."""
    reg = getattr(ctx, "notify_dedupe", None)
    if reg is None:
        reg = Registry()
        try:
            ctx.notify_dedupe = reg
        except Exception:  # noqa: BLE001 - a frozen ctx just gets a throwaway registry
            pass
    return reg
