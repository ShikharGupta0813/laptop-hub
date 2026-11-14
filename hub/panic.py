"""Panic: one tap stops the agent everywhere, and keeps it stopped until the owner taps Resume.

What it does (docs/api/panic.md): stops every running assistant turn, cancels pending approvals and N3 countdowns, revokes
remembered approvals and trust rules, turns Full Control off, pauses every listener and background engine, and sets the
persistent flag `config.panic_active`. While it is set, the assistant refuses every tool that changes something or sends
something out, and every background engine reads itself as switched off (their `enabled` checks include `on(config)`),
so NOTHING the owner switched on in Settings is touched: Resume only clears the flag and restarts what was running.

Subsystems register a hook: `panic.register(name, stop_fn, resume_fn=None, ctx=ctx)`. `stop_fn()` returns a short
description of what it stopped (or None / False when nothing was running; that is not listed and not resumed). It may be
`async`. `resume_fn(state)` gets that return value back. One failing hook never stops the others: its error is collected.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import re
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from .common import Ctx, JsonFile

log = logging.getLogger("hub.panic")

HOOK_TIMEOUT = 10.0
TOKEN_TTL = 300                       # seconds a panic token works
SOURCE_NAMES = ("button", "voice", "token", "api")
DEFAULT_PHRASES = ("stop everything", "panic", "emergency stop", "pause everything", "kill everything")
PAUSED_REPLY = "Agent paused. Everything is stopped. Tap Resume in the app when you want me back."
BLOCKED_REPLY = "The agent is paused (Panic). Nothing that changes things or sends things runs until you tap Resume."
_GLOBAL: dict[str, tuple[Callable, Callable | None]] = {}     # hooks with no ctx (tests, scripts)


def on(config: Any) -> bool:
    """True while Panic is active. Cheap: every gate in the hub calls this."""
    return getattr(config, "panic_active", False) is True


def register(name: str, stop_fn: Callable, resume_fn: Callable | None = None, *, ctx: Any = None) -> None:
    """Add (or replace, by name) a subsystem's panic hook."""
    if ctx is None:
        _GLOBAL[name] = (stop_fn, resume_fn)
        return
    hooks = getattr(ctx, "panic_hooks", None)
    if hooks is None:
        hooks = {}
        ctx.panic_hooks = hooks
    hooks[name] = (stop_fn, resume_fn)


def register_flag(ctx: Any, name: str, key: str, default: bool = False) -> None:
    """For a background engine whose `enabled` already reads `on(config)`: Panic only has to report it. Nothing is
    changed in Settings, so Resume needs no code: the engine simply reads itself as on again."""
    def stop():
        return f"{name} paused" if getattr(ctx.config, key, default) else None
    register(name, stop, ctx=ctx)


def guard(config: Any) -> None:
    """For routes that start work or send things: refuse with 423 while Panic is active."""
    if on(config):
        raise HTTPException(423, BLOCKED_REPLY)


# ---- phrases ------------------------------------------------------------------------------------------------

_FILLER = {"hey", "ok", "okay", "hi", "yo", "please", "jarvis", "friday", "computer", "assistant"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", str(text).lower())).strip()


def phrases(config: Any) -> list[str]:
    mine = getattr(config, "panic_phrases", None)
    if isinstance(mine, list) and mine:
        out = [_norm(p) for p in mine if str(p).strip()]
        return [p for p in out if p] or list(DEFAULT_PHRASES)
    return list(DEFAULT_PHRASES)


def matches_phrase(text: str, config: Any) -> bool:
    """True when the WHOLE message is a panic phrase (optionally addressed: 'Jarvis stop everything').
    A sentence that merely contains the word ('don't panic about the bug') is not a panic."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 80:
        return False
    words = _norm(raw).split()
    while words and words[0] in _FILLER:
        words.pop(0)
    while words and words[-1] in ("please", "now"):
        words.pop()
    return " ".join(words) in {_norm(p) for p in phrases(config)}


# ---- tokens: a short-lived way to hit Panic without a cookie ----------------------------------------------------

def make_token(config: Any, now: float | None = None) -> str:
    stamp = str(int(now if now is not None else time.time()))
    mac = hmac.new(bytes(config.session_secret) + b"|panic", stamp.encode(), sha256).hexdigest()[:32]
    return f"{stamp}.{mac}"


def token_ok(config: Any, token: str | None, now: float | None = None) -> bool:
    try:
        stamp, mac = str(token or "").split(".", 1)
        age = (now if now is not None else time.time()) - int(stamp)
        good = hmac.new(bytes(config.session_secret) + b"|panic", stamp.encode(), sha256).hexdigest()[:32]
        return 0 <= age <= TOKEN_TTL and hmac.compare_digest(good, mac)
    except Exception:  # noqa: BLE001
        return False


# ---- the engine -----------------------------------------------------------------------------------------------

class Panic:
    def __init__(self, ctx: Ctx, clock: Callable[[], float] = time.time):
        self.ctx = ctx
        self.clock = clock
        self.lock = threading.Lock()
        self.file = JsonFile(Path(ctx.config.data_path()) / "panic.json")

    # -- state --
    def state(self) -> dict:
        data = self.file.load({})
        data = data if isinstance(data, dict) else {}
        return {"active": on(self.ctx.config), "at": data.get("at"), "source": data.get("source"),
                "paused": data.get("paused") if isinstance(data.get("paused"), list) else [],
                "errors": data.get("errors") if isinstance(data.get("errors"), list) else [],
                "full_control_was_on": bool(data.get("full_control_was_on")),
                "confirm": getattr(self.ctx.config, "panic_confirm", False) is True,
                "phrases": phrases(self.ctx.config)}

    def _persist_flag(self, active: bool) -> None:
        self.ctx.config.panic_active = active
        self._edit_config({"panic_active": active})

    def _edit_config(self, changes: dict) -> None:
        for key, value in changes.items():
            setattr(self.ctx.config, key, value)
        path = getattr(self.ctx, "config_path", None)
        if not path:
            return
        try:
            from . import config as cfg
            disk = cfg.load(Path(path))
            for key, value in changes.items():
                setattr(disk, key, value)
            cfg.save(disk, Path(path))
        except Exception:  # noqa: BLE001  still set in memory until the hub restarts
            log.warning("Panic could not write the settings file.")

    def _audit(self, event: str, **fields) -> None:
        try:
            self.ctx.audit(event, **fields)
        except Exception:  # noqa: BLE001
            pass

    def _hooks(self) -> dict[str, tuple[Callable, Callable | None]]:
        hooks = dict(_GLOBAL)
        hooks.update(getattr(self.ctx, "panic_hooks", None) or {})
        return hooks

    @staticmethod
    async def _call(fn: Callable, *args) -> Any:
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, HOOK_TIMEOUT)
        return result

    # -- activate --
    async def activate(self, source: str = "button") -> dict:
        """Stop everything. Idempotent: a second call while active changes nothing and returns the same report."""
        with self.lock:
            if on(self.ctx.config):
                view = self.state()
                view["already"] = True
                return view
            self._persist_flag(True)            # first, so every gate is shut before anything else runs
        stopped: list[dict] = []
        errors: list[dict] = []
        full = getattr(self.ctx.config, "full_control", False) is True
        if full:
            self._edit_config({"full_control": False})
            stopped.append({"name": "Full Control", "detail": "switched off (stays off after Resume)", "state": None})
        assistant = getattr(self.ctx, "assistant", None)
        if assistant is not None and hasattr(assistant, "panic_stop_all"):
            try:
                info = await self._call(assistant.panic_stop_all)
                if info:
                    stopped.append({"name": "Assistant", "detail": info.get("detail", ""), "state": None})
            except Exception as exc:  # noqa: BLE001
                errors.append({"name": "Assistant", "error": type(exc).__name__ + ": " + str(exc)[:160]})
        for name, (stop_fn, _resume) in self._hooks().items():
            try:
                state = await self._call(stop_fn)
            except Exception as exc:  # noqa: BLE001  one failing hook must not stop the others
                errors.append({"name": name, "error": type(exc).__name__ + ": " + str(exc)[:160]})
                continue
            if state:
                detail = state if isinstance(state, str) else (state.get("detail", "") if isinstance(state, dict) else "")
                stopped.append({"name": name, "detail": str(detail)[:200], "state": state})
        record = {"active": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.clock())),
                  "source": source if source in SOURCE_NAMES else "api", "paused": stopped, "errors": errors,
                  "full_control_was_on": full}
        try:
            self.file.save(record)
        except OSError:
            pass
        self._audit("panic_activated", source=record["source"], stopped=[s["name"] for s in stopped],
                    errors=[e["name"] for e in errors], full_control_was_on=full)
        try:
            self.ctx.notify(title="Agent paused", body="Panic stopped everything. Open the app and tap Resume when ready.",
                            level="warn", source="panic")
        except Exception:  # noqa: BLE001
            pass
        return self.state()

    # -- resume --
    async def resume(self) -> dict:
        """Clear the flag and restart only what Panic actually stopped. Full Control, remembered approvals and trust
        rules stay off: the owner turns them on again on purpose."""
        if not on(self.ctx.config):
            view = self.state()
            view["already"] = True
            return view
        before = self.state()
        self._persist_flag(False)
        hooks = self._hooks()
        resumed, errors = [], []
        for item in before["paused"]:
            entry = hooks.get(item.get("name"))
            if not entry or entry[1] is None:
                continue
            try:
                await self._call(entry[1], item.get("state"))
                resumed.append(item["name"])
            except Exception as exc:  # noqa: BLE001
                errors.append({"name": item.get("name"), "error": type(exc).__name__ + ": " + str(exc)[:160]})
        try:
            self.file.save({"active": False, "at": before.get("at"), "source": before.get("source"),
                            "paused": [], "errors": errors, "resumed_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                                                         time.localtime(self.clock()))})
        except OSError:
            pass
        self._audit("panic_resumed", resumed=resumed, errors=[e["name"] for e in errors])
        view = self.state()
        view["resumed"] = resumed
        return view


# ---- routes ---------------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, clock: Callable[[], float] | None = None) -> APIRouter:
    engine = Panic(ctx, clock or time.time)
    ctx.panic = engine                       # type: ignore[attr-defined]
    assistant = getattr(ctx, "assistant", None)
    if assistant is not None:                # the voice phrase calls this before any model runs
        try:
            assistant.panic_cb = lambda source="voice": engine.activate(source)
        except Exception:  # noqa: BLE001
            pass
    router = APIRouter(prefix="/api/panic")
    router.engine = engine                   # type: ignore[attr-defined]

    def need_login(request: Request) -> None:
        ctx.need_auth(request)

    def panic_gate(request: Request, x_panic_token: str | None = Header(None)) -> None:
        """Login, or a short-lived panic token (it works even when the session has just expired)."""
        if x_panic_token and token_ok(ctx.config, x_panic_token):
            request.state.panic_via = "token"
            return
        ctx.need_auth(request)

    @router.get("", dependencies=[Depends(need_login)])
    @router.get("/", dependencies=[Depends(need_login)])
    def status():
        return engine.state()

    @router.get("/arm", dependencies=[Depends(need_login)])
    def arm():
        """A token good for five minutes that can trigger Panic (and only Panic) without the login cookie."""
        return {"token": make_token(ctx.config, engine.clock()), "expires_in": TOKEN_TTL}

    @router.post("", dependencies=[Depends(panic_gate)])
    @router.post("/", dependencies=[Depends(panic_gate)])
    async def trigger(request: Request, body: dict = Body(default_factory=dict)):
        source = getattr(request.state, "panic_via", None) or str(body.get("source") or "button")
        return await engine.activate(source)

    @router.post("/resume", dependencies=[Depends(need_login)])
    async def resume():
        return await engine.resume()

    return router
