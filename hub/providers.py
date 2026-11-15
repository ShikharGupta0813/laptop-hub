"""Which AI providers this laptop can use right now, and which one is the default.

Providers: "local" (Ollama), "claude" (Anthropic API key), "claude-login" (the `claude` program logged in
with a Claude subscription), "gemini" (Google API key) and "duo" (Claude login plans, Gemini does the work). `default_provider` in hub.json is "auto" or one
of those names; "auto" picks claude if ANTHROPIC_API_KEY is set, else gemini if a key is saved, else claude-login if the
`claude` program is installed, else local. A logged-in CLI provider (Gemini CLI, `cli:<id>`) counts before the Claude login. Heavy jobs can pick claude-login explicitly.
"""
from __future__ import annotations

import os

from .llm import PROVIDERS, find_claude_cli, gemini_key, gemini_quota_problem

LABELS = {"local": "Local model", "claude": "Claude (API key)", "claude-login": "Claude (my login)",
          "gemini": "Gemini", "duo": "Claude + Gemini"}


def _claude_login_cli(config) -> str | None:
    return find_claude_cli(getattr(config, "claude_cli_path", "") or "")


def availability(config, ollama_up: bool | None = None) -> dict[str, dict]:
    """{provider: {"ready": bool, "reason": str, "label": str}}. `ollama_up` None means not checked."""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    cli = _claude_login_cli(config)
    key = gemini_key()
    gem_saved = bool(key)
    no_quota = gemini_quota_problem(key) if gem_saved else ""   # the last call with this key found limit 0
    gem = gem_saved and not no_quota
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
                   "reason": no_quota or ("A Gemini key is saved." if gem
                                         else "No Gemini key. Run `python -m hub setup --only gemini`.")},
        "duo": {"ready": gem, "label": LABELS["duo"],
                "reason": (f"Gemini isn't usable: {no_quota}. Use Claude (my login) or the local model, or fix the key."
                           if no_quota else
                           "Gemini does the work; Claude (your login) plans and checks, sparingly."
                           if gem and cli else
                           "Gemini does the work. Claude supervisor not available (install Claude Code and run "
                           "`claude` once), so Gemini works alone." if gem else
                           "Needs a Gemini key (and optionally Claude Code for the supervisor). "
                           "Run `python -m hub setup --only ai`.")},
    }


def key_of(provider: str) -> str:
    return provider.replace("-", "_")


def _ready_cli_entries(config) -> list[str]:
    try:
        from . import cliprov
        return [e["id"] for e in cliprov.ready_entries(config) if e["ready"]
                and cliprov.status_of(cliprov.list_for(config).get(e["id"]))["installed"]]
    except Exception:  # noqa: BLE001 - a broken list never breaks provider choice
        return []


def resolve_default(config) -> str:
    choice = (getattr(config, "default_provider", "auto") or "auto").strip().lower()
    if choice in PROVIDERS:
        return choice
    if choice.startswith("cli:"):       # the owner pointed the default at one of their CLI login entries
        from . import cliprov
        if cliprov.is_enabled(config, choice[4:]):
            return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    key = gemini_key()
    if key and not gemini_quota_problem(key):
        return "gemini"       # free key: routine jobs go here so the Claude subscription quota is kept
    for entry in _ready_cli_entries(config):
        return "cli:" + entry      # a logged-in command line AI (the Gemini CLI) counts: no API key needed
    if _claude_login_cli(config):
        return "claude-login"
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
