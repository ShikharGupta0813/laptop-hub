"""Shared plumbing for the owner's editable PROVIDER LISTS (search providers, vision providers, AI endpoints).

Design rule: things are lists the owner can add to, edit, switch off, reorder or delete in Settings, and today's
built-in choice is just one ready-made entry. An entry is a plain dict:
    {id, kind, label, enabled, settings: {...}, has_secret: bool}
The list lives in one JSON file in the hub data folder. The SECRET (API key) of an entry is never in that file, in a
response or in a log: it is kept in the OS keyring (service `redline-providers`, name `<list>:<id>`). A response only
says `has_secret`. Order in the list is the priority: the first enabled entry that works wins.

`Backoff` remembers (in memory only) which entries failed lately so the next call skips them for a while: 1 min after
the first failure, doubling up to 1 hour, cleared by a success.
"""
from __future__ import annotations

import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .common import JsonFile

SERVICE = "redline-providers"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_ENTRIES = 30
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class ListError(ValueError):
    """A plain sentence for the owner; `status` is the HTTP status the route uses."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub("", value).split())[:limit]


# ---- secrets ------------------------------------------------------------------------------------------------

class KeyringSecrets:
    """API keys in the OS keyring. Tests pass MemorySecrets (same three methods)."""

    def get(self, name: str) -> str:
        try:
            import keyring
            return (keyring.get_password(SERVICE, name) or "").strip()
        except Exception:  # noqa: BLE001 - no keyring installed or no backend
            return ""

    def set(self, name: str, value: str) -> None:
        try:
            import keyring
            keyring.set_password(SERVICE, name, value)
        except Exception:  # noqa: BLE001
            raise ListError("The key could not be saved: the OS keyring is not available "
                            "(pip install -r requirements-work.txt).", 503) from None

    def delete(self, name: str) -> None:
        try:
            import keyring
            keyring.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 - nothing stored is fine
            pass


class MemorySecrets:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, name: str) -> str:
        return self.data.get(name, "")

    def set(self, name: str, value: str) -> None:
        self.data[name] = value

    def delete(self, name: str) -> None:
        self.data.pop(name, None)


# ---- backoff ------------------------------------------------------------------------------------------------

class Backoff:
    FIRST, CAP = 60.0, 3600.0

    def __init__(self, clock: Callable[[], float] | None = None):
        self.clock = clock or time.time
        self.state: dict[str, dict] = {}
        self.lock = threading.Lock()

    def blocked_for(self, key: str) -> float:
        with self.lock:
            until = (self.state.get(key) or {}).get("until", 0.0)
        return max(0.0, until - self.clock())

    def fail(self, key: str, reason: str) -> None:
        with self.lock:
            st = self.state.setdefault(key, {"fails": 0})
            st["fails"] += 1
            st["until"] = self.clock() + min(self.CAP, self.FIRST * 2 ** (st["fails"] - 1))
            st["error"] = clean(reason, 160)
            st["at"] = self.clock()

    def ok(self, key: str) -> None:
        with self.lock:
            self.state[key] = {"fails": 0, "ok_at": self.clock()}

    def view(self, key: str) -> dict:
        with self.lock:
            st = dict(self.state.get(key) or {})
        wait = max(0.0, st.get("until", 0.0) - self.clock())
        return {"failing": wait > 0, "last_error": st.get("error", "") if st.get("fails") else "",
                "skip_seconds": int(wait), "fails": st.get("fails", 0), "last_ok": st.get("ok_at")}

    def forget(self, key: str) -> None:
        with self.lock:
            self.state.pop(key, None)


# ---- the list -----------------------------------------------------------------------------------------------

class ProviderList:
    """One list in one JSON file.

    `name` namespaces the keyring entries. `seed()` returns the ready-made entries written the first time the file is
    read (or whenever it is missing/damaged). `check(kind, settings)` returns the cleaned settings dict or raises
    ListError; `kinds` is the set of allowed kinds."""

    def __init__(self, name: str, path: Path, kinds: tuple[str, ...], seed: Callable[[], list[dict]],
                 check: Callable[[str, dict], dict], secrets: Any = None):
        self.name, self.path, self.kinds, self._seed, self._check = name, Path(path), kinds, seed, check
        self.secrets = secrets or KeyringSecrets()
        self.file = JsonFile(self.path)
        self.lock = threading.RLock()

    # storage
    def _clean_entry(self, raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        kind, ident = raw.get("kind"), raw.get("id")
        if kind not in self.kinds or not isinstance(ident, str) or not ID_RE.match(ident):
            return None
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        try:
            settings = self._check(kind, settings)
        except ListError:
            settings = {}
        return {"id": ident, "kind": kind, "label": clean(raw.get("label"), 60) or kind,
                "enabled": raw.get("enabled", True) is not False, "settings": settings}

    def load(self) -> list[dict]:
        with self.lock:
            data = self.file.load(None)
            rows = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = self._seed()
                self.file.save({"entries": rows})
            out, seen = [], set()
            for raw in rows:
                entry = self._clean_entry(raw)
                if entry and entry["id"] not in seen:
                    seen.add(entry["id"])
                    out.append(entry)
            return out[:MAX_ENTRIES]

    def _save(self, entries: list[dict]) -> None:
        self.file.save({"entries": entries})

    def secret_name(self, ident: str) -> str:
        return f"{self.name}:{ident}"

    def secret(self, ident: str) -> str:
        return self.secrets.get(self.secret_name(ident))

    def view(self, entry: dict) -> dict:
        """What a response may show: never the secret, only whether one is saved."""
        return {**entry, "settings": dict(entry["settings"]), "has_secret": bool(self.secret(entry["id"]))}

    def public(self) -> list[dict]:
        return [self.view(e) for e in self.load()]

    def get(self, ident: str) -> dict | None:
        return next((e for e in self.load() if e["id"] == ident), None)

    def enabled(self) -> list[dict]:
        return [e for e in self.load() if e["enabled"]]

    # editing
    def add(self, body: dict) -> dict:
        with self.lock:
            entries = self.load()
            if len(entries) >= MAX_ENTRIES:
                raise ListError(f"The list is full ({MAX_ENTRIES} entries). Delete one first.")
            kind = body.get("kind")
            if kind not in self.kinds:
                raise ListError("Pick one of: " + ", ".join(self.kinds) + ".")
            settings = self._check(kind, body.get("settings") if isinstance(body.get("settings"), dict) else {})
            ident = self._new_id(kind, {e["id"] for e in entries})
            entry = {"id": ident, "kind": kind, "label": clean(body.get("label"), 60) or kind,
                     "enabled": body.get("enabled", True) is not False, "settings": settings}
            self._apply_secret(ident, body)
            entries.append(entry)
            self._save(entries)
            return self.view(entry)

    def update(self, ident: str, body: dict) -> dict:
        with self.lock:
            entries = self.load()
            entry = next((e for e in entries if e["id"] == ident), None)
            if entry is None:
                raise ListError("No such entry.", 404)
            if "label" in body:
                entry["label"] = clean(body.get("label"), 60) or entry["label"]
            if "enabled" in body:
                entry["enabled"] = body.get("enabled") is not False
            if isinstance(body.get("settings"), dict):
                entry["settings"] = self._check(entry["kind"], {**entry["settings"], **body["settings"]})
            self._apply_secret(ident, body)
            self._save(entries)
            return self.view(entry)

    def delete(self, ident: str) -> None:
        with self.lock:
            entries = self.load()
            kept = [e for e in entries if e["id"] != ident]
            if len(kept) == len(entries):
                raise ListError("No such entry.", 404)
            self.secrets.delete(self.secret_name(ident))
            self._save(kept)

    def reorder(self, ids: Any) -> list[dict]:
        with self.lock:
            entries = self.load()
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise ListError("Send ids as a list of entry ids.")
            by_id = {e["id"]: e for e in entries}
            first = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
            rest = [e for e in entries if e["id"] not in {x["id"] for x in first}]
            self._save(first + rest)
            return self.public()

    def reset(self) -> list[dict]:
        with self.lock:
            for e in self.load():
                self.secrets.delete(self.secret_name(e["id"]))
            self._save(self._seed())
            return self.public()

    def _apply_secret(self, ident: str, body: dict) -> None:
        if body.get("clear_secret") is True:
            self.secrets.delete(self.secret_name(ident))
        secret = body.get("secret")
        if isinstance(secret, str) and secret.strip():
            self.secrets.set(self.secret_name(ident), secret.strip()[:500])

    @staticmethod
    def _new_id(kind: str, taken: set[str]) -> str:
        while True:
            ident = f"{kind}-{_secrets.token_hex(2)}"[:40]
            if ident not in taken:
                return ident


# ---- small validators the modules share -----------------------------------------------------------------------

def check_url(value: Any, what: str = "address", *, https_only: bool = False) -> str:
    """http(s) address without a login inside it, no query or fragment secrets expected. ListError when unusable."""
    from urllib.parse import urlsplit
    text = clean(value, 400)
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        raise ListError(f"The {what} is not a valid web address.") from None
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme not in allowed or not parts.hostname:
        raise ListError(f"The {what} must start with {' or '.join(s + '://' for s in allowed)}.")
    if parts.username or parts.password:
        raise ListError(f"The {what} may not contain a login (put the key in the key field).")
    return text
