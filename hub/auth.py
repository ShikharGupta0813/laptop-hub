"""Login: one long random token, exchanged for a signed session cookie.

A wrong token is slowed down: five failures in a minute lock that client out for the rest
of the minute, so guessing is hopeless even before the token's length is considered.
"""
from __future__ import annotations

import hashlib
import hmac
import time

SESSION_SECONDS = 14 * 24 * 3600
COOKIE = "hub_session"
MAX_FAILS = 5
WINDOW_SECONDS = 60


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(secret: bytes, now: float | None = None) -> str:
    expires = str(int((now if now is not None else time.time()) + SESSION_SECONDS))
    return f"{expires}.{_sign(secret, expires)}"


def session_valid(secret: bytes, value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    expires, _, signature = value.partition(".")
    if not hmac.compare_digest(_sign(secret, expires), signature):
        return False
    try:
        return int(expires) > (now if now is not None else time.time())
    except ValueError:
        return False


class LoginLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._fails: dict[str, list[float]] = {}

    def _recent(self, client: str) -> list[float]:
        cutoff = self._clock() - WINDOW_SECONDS
        recent = [t for t in self._fails.get(client, []) if t > cutoff]
        self._fails[client] = recent
        return recent

    def blocked(self, client: str) -> bool:
        return len(self._recent(client)) >= MAX_FAILS

    def failed(self, client: str) -> None:
        self._recent(client).append(self._clock())

    def succeeded(self, client: str) -> None:
        self._fails.pop(client, None)
