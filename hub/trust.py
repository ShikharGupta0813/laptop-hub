"""Trust ladder: after the owner approves the same kind of N2 action many times with no denial, offer to do it automatically.

Shapes come from `safety.arg_shape` (tool + program(s) + folder, or the action id, ...). Only the shape is stored, never
the arguments. A TRUST RULE is a shape with an expiry (default 30 days, renewable) and a daily use cap. Rules apply only to
N2: never to N3, never to the tools that always ask (`safety.ALWAYS_ASK`, anything that sends, publishes, submits or
applies), never to MCP tools, never in a tainted turn. A denial of the shape, a Panic, or any Full Control change revokes
at once. Every automatic run is audited with the rule id. See docs/api/trust.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import safety
from .common import Ctx, JsonFile

DAY = 86400
WINDOW_DAYS = 30
DEFAULT_RULE_DAYS = 30
MAX_RULE_DAYS = 365
DEFAULT_DAILY_CAP = 20
MAX_DAILY_CAP = 500
MAX_STAMPS = 100
MAX_SHAPES = 500
OUTBOUND_WORD = re.compile(r"(?:^|[_\-])(?:send|publish|submit|apply|post|email|tweet|upload)(?:$|[_\-])", re.I)
SCOPE_TEXT = {"run_command": "the same program(s) in the same folder", "write_file": "the same folder",
              "laptop_action": "the same laptop action", "jarvis": "the same Jarvis action",
              "run_script": "the same script"}


def shape_key(name: str, args: dict) -> str:
    return json.dumps(safety.arg_shape(name, args), separators=(",", ":"), default=list)


def shape_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def tool_eligible(name: str, *, always_ask: bool = False, dynamic: bool = False) -> bool:
    """Whether a tool can ever be on the trust ladder, whatever the arguments."""
    return not (always_ask or dynamic or name in safety.ALWAYS_ASK or OUTBOUND_WORD.search(name or ""))


class Trust:
    def __init__(self, ctx: Ctx, clock: Callable[[], float] = time.time):
        self.ctx = ctx
        self.clock = clock
        self.lock = threading.RLock()
        self.file = JsonFile(Path(ctx.config.data_path()) / "trust.json")

    # -- config --
    @property
    def enabled(self) -> bool:
        return getattr(self.ctx.config, "trust_enabled", True) is not False

    @property
    def threshold(self) -> int:
        try:
            return max(3, min(int(getattr(self.ctx.config, "trust_threshold", 10) or 10), 100))
        except (TypeError, ValueError):
            return 10

    # -- storage --
    def _load(self) -> dict:
        data = self.file.load({})
        data = data if isinstance(data, dict) else {}
        data.setdefault("shapes", {})
        data.setdefault("rules", [])
        data.setdefault("last_push_day", "")
        return data

    def _save(self, data: dict) -> None:
        shapes = data["shapes"]
        if len(shapes) > MAX_SHAPES:                 # keep the most recently seen
            keep = sorted(shapes.items(), key=lambda kv: kv[1].get("last", 0), reverse=True)[:MAX_SHAPES]
            data["shapes"] = dict(keep)
        try:
            self.file.save(data)
        except OSError:
            pass

    def _audit(self, event: str, **fields) -> None:
        try:
            self.ctx.audit(event, **fields)
        except Exception:  # noqa: BLE001
            pass

    def _day(self, now: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else self.clock()))

    # -- the ladder --
    def eligible(self, name: str, level: str, *, always_ask=False, dynamic=False, tainted=False) -> bool:
        return (self.enabled and level == safety.N2 and not tainted and tool_eligible(name, always_ask=always_ask, dynamic=dynamic)
                and getattr(self.ctx.config, "full_control", False) is not True
                and getattr(self.ctx.config, "panic_active", False) is not True)

    def record(self, name: str, args: dict, approved: bool, *, by: str = "tap") -> None:
        """One approved or denied N2 call (the caller checked `eligible`). A denial revokes the matching rule."""
        key, now = shape_key(name, args), self.clock()
        with self.lock:
            data = self._load()
            row = data["shapes"].setdefault(key, {"tool": name, "label": safety.describe_shape(name, args), "id": shape_id(key),
                                                  "approvals": [], "denials": [], "streak": 0, "first": now})
            row["last"] = now
            if approved:
                row["approvals"] = (row["approvals"] + [now])[-MAX_STAMPS:]
                row["streak"] = int(row.get("streak", 0)) + 1
            else:
                row["denials"] = (row["denials"] + [now])[-MAX_STAMPS:]
                row["streak"] = 0
                row.pop("dismissed", None)
                for rule in data["rules"]:
                    if rule["key"] == key and not rule.get("revoked"):
                        self._revoke_rule(rule, "denied")
            self._save(data)
        if approved:
            self.maybe_push()

    def _suggestion_rows(self, data: dict, now: float) -> list[dict]:
        out = []
        active = {r["key"] for r in data["rules"] if self._active(r, now)}
        for key, row in data["shapes"].items():
            if key in active or int(row.get("streak", 0)) < self.threshold:
                continue
            if any(t > now - WINDOW_DAYS * DAY for t in row.get("denials", [])):
                continue
            if row.get("dismissed", 0) > now - WINDOW_DAYS * DAY:
                continue
            if not tool_eligible(row.get("tool", "")):
                continue
            out.append({"id": row["id"], "tool": row["tool"], "label": row["label"], "approvals": int(row["streak"]),
                        "last_approved": (row.get("approvals") or [0])[-1], "scope": SCOPE_TEXT.get(row["tool"], "the same kind of call")})
        return sorted(out, key=lambda r: -r["approvals"])

    def suggestions(self) -> list[dict]:
        if not self.enabled:
            return []
        with self.lock:
            return self._suggestion_rows(self._load(), self.clock())

    def dismiss(self, sid: str) -> bool:
        with self.lock:
            data = self._load()
            for row in data["shapes"].values():
                if row.get("id") == sid:
                    row["dismissed"] = self.clock()
                    self._save(data)
                    return True
        return False

    def maybe_push(self) -> bool:
        """At most one phone/app push per day announcing that suggestions are waiting."""
        if getattr(self.ctx.config, "panic_active", False) is True:
            return False
        with self.lock:
            data = self._load()
            now = self.clock()
            rows = self._suggestion_rows(data, now) if self.enabled else []
            if not rows or data.get("last_push_day") == self._day(now):
                return False
            data["last_push_day"] = self._day(now)
            self._save(data)
        try:
            self.ctx.notify(title="Do some approvals automatically?",
                            body=f"{len(rows)} kind(s) of action you always approve can run on their own. Review in Settings > Trust rules.",
                            level="info", link="#settings", source="trust")
        except Exception:  # noqa: BLE001
            pass
        return True

    # -- rules --
    def _active(self, rule: dict, now: float) -> bool:
        return not rule.get("revoked") and rule.get("expires", 0) > now

    def accept(self, sid: str, days: int | None = None, daily_cap: int | None = None) -> dict:
        days = DEFAULT_RULE_DAYS if days is None else max(1, min(int(days), MAX_RULE_DAYS))
        cap = DEFAULT_DAILY_CAP if daily_cap is None else max(1, min(int(daily_cap), MAX_DAILY_CAP))
        now = self.clock()
        with self.lock:
            data = self._load()
            for key, row in data["shapes"].items():
                if row.get("id") != sid:
                    continue
                if not any(r["id"] == sid for r in self._suggestion_rows(data, now)):
                    raise ValueError("That action is not on the suggestion list (any denial, a Panic or a change of Full Control resets it).")
                rule = {"id": uuid.uuid4().hex[:10], "key": key, "tool": row["tool"], "label": row["label"],
                        "scope": SCOPE_TEXT.get(row["tool"], "the same kind of call"), "created": now,
                        "expires": now + days * DAY, "daily_cap": cap, "uses": 0, "day": "", "day_uses": 0,
                        "last_used": None, "extended": 0, "revoked": None}
                data["rules"].append(rule)
                self._save(data)
                break
            else:
                raise LookupError("No such suggestion.")
        self._audit("trust_rule_created", rule=rule["id"], tool=rule["tool"], label=rule["label"], days=days, daily_cap=cap)
        return self._view(rule, now)

    def claim(self, name: str, args: dict, level: str, **kw) -> dict | None:
        """The rule that lets this call run without a tap, with today's use counted; None when none applies."""
        if not self.eligible(name, level, **kw):
            return None
        key, now = shape_key(name, args), self.clock()
        today = self._day(now)
        with self.lock:
            data = self._load()
            for rule in data["rules"]:
                if rule["key"] != key or not self._active(rule, now):
                    continue
                if rule.get("day") != today:
                    rule["day"], rule["day_uses"] = today, 0
                if rule["day_uses"] >= rule["daily_cap"]:
                    continue                                    # over today's cap: the owner is asked again
                rule["day_uses"] += 1
                rule["uses"] = int(rule.get("uses", 0)) + 1
                rule["last_used"] = now
                self._save(data)
                return dict(rule)
        return None

    def _revoke_rule(self, rule: dict, reason: str) -> None:
        rule["revoked"] = reason
        rule["revoked_at"] = self.clock()
        self._audit("trust_rule_revoked", rule=rule["id"], tool=rule["tool"], label=rule["label"], reason=reason)

    def revoke(self, rule_id: str, reason: str = "owner") -> bool:
        with self.lock:
            data = self._load()
            for rule in data["rules"]:
                if rule["id"] == rule_id and not rule.get("revoked"):
                    self._revoke_rule(rule, reason)
                    for row in data["shapes"].values():       # starts the ladder again from zero
                        if row.get("id") == shape_id(rule["key"]):
                            row["streak"] = 0
                            if reason == "owner":                  # the owner took it back: do not nag for 30 days
                                row["dismissed"] = self.clock()
                    self._save(data)
                    return True
        return False

    def revoke_all(self, reason: str = "owner") -> int:
        with self.lock:
            data = self._load()
            n = 0
            for rule in data["rules"]:
                if not rule.get("revoked") and rule.get("expires", 0) > self.clock():
                    self._revoke_rule(rule, reason)
                    n += 1
            if n:
                self._save(data)
        return n

    def extend(self, rule_id: str, days: int | None = None) -> dict | None:
        days = DEFAULT_RULE_DAYS if days is None else max(1, min(int(days), MAX_RULE_DAYS))
        now = self.clock()
        with self.lock:
            data = self._load()
            for rule in data["rules"]:
                if rule["id"] == rule_id and not rule.get("revoked"):
                    rule["expires"] = max(rule["expires"], now) + days * DAY
                    rule["expires"] = min(rule["expires"], now + MAX_RULE_DAYS * DAY)
                    rule["extended"] = int(rule.get("extended", 0)) + 1
                    self._save(data)
                    self._audit("trust_rule_extended", rule=rule_id, days=days)
                    return self._view(rule, now)
        return None

    def _view(self, rule: dict, now: float) -> dict:
        state = "revoked" if rule.get("revoked") else ("expired" if rule.get("expires", 0) <= now else "active")
        return {"id": rule["id"], "tool": rule["tool"], "label": rule["label"], "scope": rule.get("scope", ""),
                "created": rule["created"], "expires": rule["expires"], "expires_in_days": max(0, int((rule["expires"] - now) // DAY)),
                "daily_cap": rule["daily_cap"], "uses": rule.get("uses", 0),
                "uses_today": rule.get("day_uses", 0) if rule.get("day") == self._day(now) else 0,
                "last_used": rule.get("last_used"), "extended": rule.get("extended", 0), "state": state,
                "revoked": rule.get("revoked")}

    def rules(self, include_old: bool = False) -> list[dict]:
        now = self.clock()
        with self.lock:
            rows = [self._view(r, now) for r in self._load()["rules"]]
        if not include_old:
            rows = [r for r in rows if r["state"] == "active" or r.get("last_used") and r["last_used"] > now - 7 * DAY]
        return sorted(rows, key=lambda r: -r["created"])[:100]

    def view(self) -> dict:
        return {"enabled": self.enabled, "threshold": self.threshold, "suggestions": self.suggestions(), "rules": self.rules()}


# ---- routes ---------------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, clock: Callable[[], float] | None = None) -> APIRouter:
    store = Trust(ctx, clock or time.time)
    ctx.trust = store                         # type: ignore[attr-defined]
    assistant = getattr(ctx, "assistant", None)
    if assistant is not None:
        try:
            assistant.trust = store
        except Exception:  # noqa: BLE001
            pass
    try:
        from . import panic
        panic.register("Trust rules", lambda: (lambda n: f"{n} automatic rule(s) revoked" if n else None)(store.revoke_all("panic")),
                       ctx=ctx)
    except Exception:  # noqa: BLE001
        pass
    router = APIRouter(prefix="/api/trust", dependencies=[Depends(ctx.need_auth)])
    router.store = store                      # type: ignore[attr-defined]

    @router.get("")
    @router.get("/")
    def overview():
        return store.view()

    @router.post("/suggestions/{sid}/accept")
    def accept(sid: str, body: dict = Body(default_factory=dict)):
        try:
            return store.accept(sid, body.get("days"), body.get("daily_cap"))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/suggestions/{sid}/dismiss")
    def dismiss(sid: str):
        if not store.dismiss(sid):
            raise HTTPException(404, "No such suggestion.")
        return {"ok": True}

    @router.post("/rules/{rule_id}/revoke")
    def revoke(rule_id: str):
        if not store.revoke(rule_id):
            raise HTTPException(404, "No such active rule.")
        return {"ok": True}

    @router.post("/rules/{rule_id}/extend")
    def extend(rule_id: str, body: dict = Body(default_factory=dict)):
        try:
            row = store.extend(rule_id, body.get("days"))
        except (TypeError, ValueError):
            raise HTTPException(400, "days must be a whole number.") from None
        if row is None:
            raise HTTPException(404, "No such active rule.")
        return row

    @router.post("/revoke-all")
    def revoke_all():
        return {"ok": True, "revoked": store.revoke_all("owner")}

    return router
