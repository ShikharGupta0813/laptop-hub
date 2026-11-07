"""Which AI providers this laptop can use right now, and which one is the default.

Providers: "local" (Ollama), "claude" (Anthropic API key), "claude-login" (the `claude` program logged in
with a Claude subscription) and "gemini" (Google API key). `default_provider` in hub.json is "auto" or one
of those names; "auto" picks claude if ANTHROPIC_API_KEY is set, else claude-login if the `claude` program
is installed, else gemini if a key is saved, else local.
"""
from __future__ import annotations

import os

from .llm import PROVIDERS, find_claude_cli, gemini_key

LABELS = {"local": "Local model", "claude": "Claude (API key)", "claude-login": "Claude (my login)",
          "gemini": "Gemini"}


def _claude_login_cli(config) -> str | None:
    return find_claude_cli(getattr(config, "claude_cli_path", "") or "")


def availability(config, ollama_up: bool | None = None) -> dict[str, dict]:
    """{provider: {"ready": bool, "reason": str, "label": str}}. `ollama_up` None means not checked."""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    cli = _claude_login_cli(config)
    gem = bool(gemini_key())
    local_ready = ollama_up is not False
    return {
        "local": {"ready": local_ready, "label": LABELS["local"],
                  "reason": "Runs on this laptop." if local_ready else "Ollama isn't running."},
        "claude": {"ready": has_key, "label": LABELS["claude"],
                   "reason": "ANTHROPIC_API_KEY is set." if has_key else "No ANTHROPIC_API_KEY."},
        "claude_login": {"ready": bool(cli), "label": LABELS["claude-login"],
                         "reason": "Claude Code is installed (uses your Claude login)." if cli
                         else "Claude Code isn't installed. Install it and run `claude` once to log in."},
        "gemini": {"ready": gem, "label": LABELS["gemini"],
                   "reason": "A Gemini key is saved." if gem
                   else "No Gemini key. Run `python -m hub setup --only gemini`."},
    }


def key_of(provider: str) -> str:
    return provider.replace("-", "_")


def resolve_default(config) -> str:
    choice = (getattr(config, "default_provider", "auto") or "auto").strip().lower()
    if choice in PROVIDERS:
        return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    if _claude_login_cli(config):
        return "claude-login"
    if gemini_key():
        return "gemini"
    return "local"


def resolve(config, choice: str | None) -> str:
    """A provider name from whatever a request said: "", "auto" or None mean the default."""
    choice = (choice or "").strip().lower()
    if choice in PROVIDERS:
        return choice
    if choice in ("", "auto"):
        return resolve_default(config)
    return choice   # unknown names fall through to the local model in the caller


def ready_names(config) -> list[str]:
    avail = availability(config)
    return [p for p in PROVIDERS if avail[key_of(p)]["ready"]]
