"""Provider fallback chain and plain-language "what is missing" messages (Friday, Jarvis, auto, duo).

Why: `auto` used to insist on the Gemini API key. An owner who is logged into the Gemini CLI (or only runs Ollama) got
a bare "Gemini needs a key" every time. Now every provider that can work is tried, in this order:

    the one asked for (or the default) -> Claude API key -> Gemini API key -> your CLI logins (Gemini CLI, in the order
    of Settings > Providers > CLI login) -> Claude login -> your own endpoints -> the local model (Ollama)

A provider that is not ready is skipped (with the reason kept). A provider that fails while answering is skipped too, and
the next one answers, but only when it failed before the first word (a half-written answer is never replaced). The local
model is the last stop and is never skipped for being "unknown". Nothing falls FROM the local model TO a cloud one: a
private "local" turn stays local. When nothing works the error names each provider, what is missing and the exact
command or setting that fixes it. See docs/api/fixit.md.
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Callable

from . import cliprov, providers
from .llm import LLMError

log = logging.getLogger("hub.provchain")

BASE_ORDER = ("claude", "gemini", "cli", "claude-login", "endpoint", "local")
LAST: dict[str, Any] = {"used": "", "skipped": [], "at": 0.0}     # for /api/fixit/providers (process memory only)


def _label(config, ident: str) -> str:
    if ident.startswith("cli:"):
        try:
            entry = cliprov.list_for(config).get(ident[4:])
            if entry:
                return entry["label"]
        except Exception:  # noqa: BLE001
            pass
        return "CLI login " + ident[4:]
    if ident.startswith("endpoint:"):
        try:
            from . import endpoints
            entry = endpoints.list_for(config).get(ident[9:])
            if entry:
                return entry["label"]
        except Exception:  # noqa: BLE001
            pass
        return "Endpoint " + ident[9:]
    return providers.LABELS.get(ident, ident)


# What fixes each one: the exact words, an optional command, and the button the app shows.
FIXES = {
    "claude": ("Set the ANTHROPIC_API_KEY environment variable on the laptop (or skip it: use Claude (my login)).",
               "", {"kind": "settings", "label": "Open provider settings", "target": "settings"}),
    "claude-login": ("Install Claude Code, then run `claude` once in a terminal and sign in.", "claude",
                     {"kind": "test", "label": "Test Claude login"}),
    "gemini": ("Save a free Gemini key from aistudio.google.com/apikey, or use the Antigravity login below instead.",
               "python -m hub setup --only gemini", {"kind": "test", "label": "Test Gemini key"}),
    "duo": ("Needs a Gemini key (or the Gemini CLI login) and optionally Claude Code.",
            "python -m hub setup --only ai", {"kind": "test", "label": "Test Claude + Gemini"}),
    "local": ("Start the Ollama app on the laptop (and run `ollama pull qwen3:4b` once).", "ollama serve",
              {"kind": "test", "label": "Test local model"}),
}


def _cli_fix(config, ident: str, state: str) -> tuple[str, str, dict]:
    program = "gemini"
    try:
        entry = cliprov.list_for(config).get(ident[4:])
        if entry:
            program = entry["settings"]["argv"][0]
    except Exception:  # noqa: BLE001
        pass
    if state == "not-found":
        text = (f"`{program}` is not installed or not on the PATH. Install it (for Antigravity: "
                "see the Antigravity install page), then press Test.")
    elif state == "not-logged-in":
        text = f"Run `{program}` once in a terminal on the laptop and sign in when it asks, then press Test."
    else:
        text = "Press Test. If it fails, open Settings > Providers > CLI login and check the command."
    return text, program, {"kind": "test", "label": "Test " + _label(config, ident)}


def ordered_ids(config, choice: str | None = None) -> list[str]:
    """Every provider id worth trying, best first, ready or not (the caller checks readiness)."""
    first = providers.resolve(config, choice)
    out: list[str] = []

    def add(ident: str) -> None:
        if ident and ident not in out:
            out.append(ident)

    add(first)
    for slot in BASE_ORDER:
        if slot == "cli":
            try:
                for e in cliprov.list_for(config).enabled():
                    add("cli:" + e["id"])
            except Exception:  # noqa: BLE001
                pass
        elif slot == "endpoint":
            try:
                from . import endpoints
                for e in endpoints.list_for(config).enabled():
                    add("endpoint:" + e["id"])
            except Exception:  # noqa: BLE001
                pass
        else:
            add(slot)
    if (choice or "").strip().lower() == "local":
        return ["local"]                   # an explicit local pick stays private: never leaves for the cloud
    return out


def readiness(config, ident: str, ollama_up: bool | None = None, avail: dict | None = None) -> tuple[bool, str]:
    """(ready, reason) for any provider id, from facts only (no network)."""
    avail = avail or providers.availability(config, ollama_up)
    if ident.startswith("cli:"):
        try:
            entry = cliprov.list_for(config).get(ident[4:])
        except Exception:  # noqa: BLE001
            entry = None
        if not entry or not entry["enabled"]:
            return False, "switched off in Settings > Providers > CLI login."
        st = cliprov.status_of(entry)
        return bool(st["ready"]), st["message"]
    if ident.startswith("endpoint:"):
        return True, "Your own endpoint is switched on."
    row = avail.get(providers.key_of(ident))
    if not row:
        return False, "Unknown provider."
    return bool(row["ready"]), row["reason"]


def fix_text(config, ident: str) -> tuple[str, str]:
    """(what to do, command) for a provider, whether or not it looks ready; used when a real try failed."""
    if ident.startswith("cli:"):
        st = cliprov.status_of(cliprov.list_for(config).get(ident[4:]) or {"settings": {"argv": ["gemini"]}, "id": ident[4:]})
        text, cmd, _ = _cli_fix(config, ident, st["state"])
        return text, cmd
    text, cmd, _ = FIXES.get(ident, ("Press Test again, or pick another AI.", "", None))
    return text, cmd


def diagnose(config, ollama_up: bool | None = None) -> list[dict]:
    """One row per provider: {id, label, ready, reason, fix, command, action}. Powers the Provider check panel."""
    avail = providers.availability(config, ollama_up)
    rows = []
    for ident in ordered_ids(config, "auto"):
        ok, reason = readiness(config, ident, ollama_up, avail)
        row = {"id": ident, "label": _label(config, ident), "ready": ok, "reason": reason, "fix": "", "command": "",
               "action": None}
        if ident.startswith("cli:"):
            st = cliprov.status_of(cliprov.list_for(config).get(ident[4:]) or {"settings": {"argv": ["gemini"]}, "id": ident[4:]})
            text, cmd, act = _cli_fix(config, ident, st["state"])
            test_path = f"/api/cli-providers/{ident[4:]}/test"
        elif ident.startswith("endpoint:"):
            text, cmd, act = "Press Test; check the address and key in Settings > Providers.", "", {"kind": "test", "label": "Test endpoint"}
            test_path = f"/api/ai-endpoints/{ident[9:]}/test"
        else:
            text, cmd, act = FIXES.get(ident, ("", "", None))
            test_path = f"/api/fixit/providers/{ident}/test"
        if not ok:
            row["fix"], row["command"] = text, cmd
        if act:
            row["action"] = {**act, "test_path": test_path}
        rows.append(row)
    for row in rows:                          # which one is answering now
        row["chosen"] = False
    for row in rows:
        if row["ready"]:
            row["chosen"] = True
            break
    return rows


def failure_message(config, tried: list[tuple[str, str]], ollama_up: bool | None = None) -> str:
    """The sentence shown when nothing answered. `tried` is [(provider id, what went wrong)]; the rest is added from facts."""
    said = dict(tried)
    lines = []
    for row in diagnose(config, ollama_up):
        why = said.pop(row["id"], "") or row["reason"]
        fix, command = (row["fix"], row["command"]) if row["fix"] else fix_text(config, row["id"])
        lines.append(f"- {row['label']}: {why.rstrip('.')}. Fix: {fix}" + (f" (command: {command})" if command else ""))
    lines += [f"- {_label(config, i)}: {why}" for i, why in said.items()]
    head = "No AI could answer right now. Here is what each one said:"
    tail = ("Quickest fix: start the Ollama app (local, free, no login), or log in to Antigravity (agy). "
            "Then open Friday or Jarvis > Provider check and press Test.")
    return "\n".join([head, *lines, tail])


class ChainModel:
    """complete()/stream() that walk the chain. `build(id)` returns a client or None; the first that answers wins."""

    name = "chain"

    def __init__(self, config, choice: str | None, build: Callable[[str], Any], say: Callable[[str], None] | None = None,
                 ollama_up: Callable[[], bool | None] | None = None):
        self.config = config
        self.choice = choice
        self.build = build
        self.say = say or (lambda text: log.info("%s", text))
        self.ollama_up = ollama_up or (lambda: None)
        self.used = ""          # the provider that really answered the last call (for "answered by ...")

    def _plan(self) -> tuple[list[str], list[tuple[str, str]]]:
        up = self.ollama_up()
        avail = providers.availability(self.config, up)
        ids, skipped = [], []
        for ident in ordered_ids(self.config, self.choice):
            ok, why = readiness(self.config, ident, up, avail)
            if ok:
                ids.append(ident)
            else:
                skipped.append((ident, why))
        return ids, skipped

    def _done(self, used: str, skipped: list[tuple[str, str]], failed: list[tuple[str, str]]) -> None:
        LAST.update(used=used, skipped=[{"id": i, "why": w} for i, w in skipped + failed], at=time.time())
        self.used = used
        first = ordered_ids(self.config, self.choice)[0]
        if used != first or failed:
            why = (dict(skipped + failed).get(first) or "not ready")
            self.say(f"AI: {_label(self.config, first)} not used ({why[:120]}); answered with {_label(self.config, used)}.")
        else:
            self.say(f"AI: answered with {_label(self.config, used)}.")

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        ids, skipped = self._plan()
        failed: list[tuple[str, str]] = []
        for ident in ids:
            client = self.build(ident)
            if client is None:
                failed.append((ident, "not available in this hub."))
                continue
            try:
                text = await client.complete(system, user, json_mode)
            except LLMError as exc:
                failed.append((ident, str(exc)))
                continue
            self._done(ident, skipped, failed)
            return text
        self._fail(skipped, failed)
        raise AssertionError  # pragma: no cover

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        ids, skipped = self._plan()
        failed: list[tuple[str, str]] = []
        for ident in ids:
            client = self.build(ident)
            if client is None:
                failed.append((ident, "not available in this hub."))
                continue
            started = False
            try:
                async for piece in client.stream(system, user):
                    if not started:
                        started = True
                        self._done(ident, skipped, failed)
                    yield piece
            except LLMError as exc:
                if started:
                    raise                          # a half answer is never swapped for another AI's
                failed.append((ident, str(exc)))
                continue
            if not started:                        # an empty answer counts as a failure of that provider
                failed.append((ident, "sent an empty answer."))
                continue
            return
        self._fail(skipped, failed)

    def _fail(self, skipped, failed):
        LAST.update(used="", skipped=[{"id": i, "why": w} for i, w in skipped + failed], at=time.time())
        message = failure_message(self.config, skipped + failed, self.ollama_up())
        lines = message.splitlines()
        self.say("AI: nothing could answer. " + (lines[1][:200] if len(lines) > 1 else ""))
        raise LLMError(message)
