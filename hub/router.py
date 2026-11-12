"""The "auto" router: picks a provider for one request with a cheap word check (no AI call).

Only used when the request (and `default_provider` in hub.json) say "auto". Explicit picks are never touched.
  trivial  (short chit-chat, no tool intent)                  -> local model, if Ollama is up
  heavy    (tools, code, multi-step, long)                    -> duo (Gemini works), if Gemini is usable
  normal   (everything else)                                  -> the usual default (providers.resolve_default)
Whenever the preferred provider is not ready the usual default is used, so the router can only help.
"""
from __future__ import annotations

import re
import time

from . import providers
from .assistant import TOOL_GROUPS, TOOLISH

TRIVIAL_WORDS = 12     # fewer words than this, with no tool intent, is chit-chat
LONG_WORDS = 60        # more words than this is a big job
CODE_RE = re.compile(r"```|\b(?:def|class|function|import|traceback|stack ?trace|exception|compile|debug|refactor|regex|sql)\b", re.I)
MULTI_RE = re.compile(r"\b(?:step by step|and then|then|after that|first .+ then|for each|every)\b", re.I)


def classify(text: str) -> tuple[str, str]:
    """("trivial" | "heavy" | "normal", why)."""
    text = (text or "").strip()
    words = len(text.split())
    if words > LONG_WORDS:
        return "heavy", "long message"
    if "```" in text:
        return "heavy", "contains code"
    if any(p.search(text) for _n, p in TOOL_GROUPS.values()):
        return "heavy", "tool request"
    if TOOLISH.search(text):
        return "heavy", "action verb"
    if CODE_RE.search(text):
        return "heavy", "code question"
    if MULTI_RE.search(text) and words >= 6:
        return "heavy", "multi-step"
    if words < TRIVIAL_WORDS:
        return "trivial", "short chit-chat"
    return "normal", "ordinary question"


class Router:
    """Holds the last decision (process memory) for /api/status."""

    def __init__(self, config):
        self.config = config
        self.last: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "router_enabled", True))

    def status(self) -> dict:
        return {"enabled": self.enabled, "last_decision": self.last}

    def route(self, choice: str | None, text: str, ollama_up: bool | None = None) -> tuple[str, dict | None]:
        """(provider, decision). `decision` is None when the router did not decide (explicit pick or off)."""
        config = self.config
        chosen = (choice or "").strip().lower()
        default = (getattr(config, "default_provider", "auto") or "auto").strip().lower()
        if chosen not in ("", "auto") or default != "auto" or not self.enabled:
            return providers.resolve(config, choice), None
        fallback = providers.resolve_default(config)
        kind, why = classify(text)
        avail = providers.availability(config, ollama_up)
        target = fallback
        if kind == "trivial" and avail["local"]["ready"]:
            target = "local"
        elif kind == "heavy" and avail["duo"]["ready"]:
            target = "duo"
        decision = {"routed_to": target, "kind": kind, "reason": why, "default": fallback, "at": time.strftime("%H:%M:%S")}
        self.last = decision
        return target, decision
