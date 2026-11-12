"""hub.json: where the settings live, and how a first config is made.

The login token is never stored. Only its SHA-256 hash is, which is enough because the
token is long and random. `python -m hub init` prints the token once.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .auth import hash_token

DEFAULT_PORT = 8765


class ConfigError(ValueError):
    """The config is missing or wrong. The message says what to fix."""


@dataclass
class Config:
    token_hash: str
    session_secret: bytes
    roots: dict[str, str]
    notes_dir: str
    inbox_dir: str
    data_dir: str = ""
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    ollama_host: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:4b"
    whisper_model: str = "base"
    claude_model: str = "claude-opus-5-5"
    # The assistant does many small tool calls, so it uses the faster, cheaper Sonnet.
    assistant_model: str = "claude-sonnet-5-5"
    # "auto" picks claude (API key) > claude-login (the `claude` program) > gemini (key) > local.
    default_provider: str = "auto"
    claude_cli_model: str = "sonnet"       # model alias for "Claude (my login)"
    claude_cli_path: str = ""              # only if `claude` is not on PATH
    gemini_model: str = "gemini-2.5-flash"   # point at your Pro model (e.g. gemini-2.5-pro) if you have one
    duo_verify: bool = True                # "Claude + Gemini": after tools ran, Claude checks the work was really done (one more Claude call)
    duo_max_claude_per_hour: int = 20      # hard cap on Claude supervisor calls per hour (0 = never ask Claude)
    allowed_origins: list[str] = field(default_factory=list)
    # Host names the hub answers to besides localhost and *.ts.net (blocks DNS-rebinding pages)
    allowed_hosts: list[str] = field(default_factory=list)
    jarvis_dir: str = ""  # folder of the jarvis-voice-assistant repo (email, calendar, chat helpers)
    shells: dict[str, list[str]] = field(default_factory=dict)
    scripts: dict[str, dict] = field(default_factory=dict)
    embed_model: str = "nomic-embed-text"   # Ollama model that turns text into vectors ("Ask my files")
    kb_roots: list[str] = field(default_factory=list)   # which shared folders "Ask my files" reads (empty = all)
    writable_roots: list[str] = field(default_factory=lambda: ["notes", "inbox"])  # roots the file manager may change
    ntfy_server: str = "https://ntfy.sh"    # optional phone push notifications through ntfy
    ntfy_topic: str = ""                    # a long random topic name you subscribe to in the ntfy app
    # Apps the phone may launch on the laptop: {"Chrome": {"target": "chrome.exe", "args": []}}
    apps: dict[str, dict] = field(default_factory=dict)
    # Mixed into every session cookie's signature; POST /api/logout-all raises it to log every phone out.
    session_generation: int = 0
    display_name: str = "Chief"             # what the hub and the assistant call you (set by `python -m hub setup`)

    def data_path(self) -> Path:
        """Where Learn and English keep their history (quiz results, vocabulary...)."""
        return Path(self.data_dir) if self.data_dir else Path.home() / ".laptop-hub" / "data"

    def shell_table(self) -> dict[str, list[str]]:
        return self.shells or default_shells()


def default_shells() -> dict[str, list[str]]:
    if sys.platform == "win32":
        return {"powershell": ["powershell.exe", "-NoLogo"], "wsl": ["wsl.exe"],
                "cmd": ["cmd.exe"]}
    return {"bash": [os.environ.get("SHELL") or "/bin/bash"]}


def default_path() -> Path:
    return Path(os.environ.get("HUB_CONFIG") or Path.home() / ".laptop-hub" / "hub.json")


def make(roots: dict[str, str] | None = None) -> tuple[Config, str]:
    """A fresh config, and the one-time login token to show the user."""
    token = secrets.token_urlsafe(24)
    home = Path.home()
    notes_dir = str(home / "HubNotes")
    inbox_dir = str(home / "HubInbox")
    roots = dict(roots or {"home": str(home)})
    roots.setdefault("notes", notes_dir)
    roots.setdefault("inbox", inbox_dir)
    config = Config(token_hash=hash_token(token), session_secret=secrets.token_bytes(32),
                    roots=roots, notes_dir=notes_dir, inbox_dir=inbox_dir)
    return config, token


def save(config: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "token_hash": config.token_hash,
        "session_secret": config.session_secret.hex(),
        "roots": config.roots, "notes_dir": config.notes_dir, "inbox_dir": config.inbox_dir,
        "data_dir": config.data_dir, "jarvis_dir": config.jarvis_dir,
        "host": config.host, "port": config.port, "ollama_host": config.ollama_host,
        "chat_model": config.chat_model, "whisper_model": config.whisper_model,
        "claude_model": config.claude_model, "assistant_model": config.assistant_model,
        "default_provider": config.default_provider, "claude_cli_model": config.claude_cli_model,
        "claude_cli_path": config.claude_cli_path, "gemini_model": config.gemini_model,
        "duo_verify": bool(config.duo_verify), "duo_max_claude_per_hour": int(config.duo_max_claude_per_hour),
        "allowed_origins": config.allowed_origins, "allowed_hosts": config.allowed_hosts, "shells": config.shells,
        "scripts": config.scripts, "apps": config.apps, "embed_model": config.embed_model,
        "kb_roots": config.kb_roots, "writable_roots": config.writable_roots,
        "ntfy_server": config.ntfy_server, "ntfy_topic": config.ntfy_topic,
        "session_generation": int(config.session_generation or 0),
        "display_name": config.display_name,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # no effect on Windows; the folder is already per-user
    except OSError:
        pass
    os.replace(tmp, path)


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"{path} not found. Run `python -m hub init` first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["session_secret"] = bytes.fromhex(data["session_secret"])
        known = Config.__dataclass_fields__
        config = Config(**{k: v for k, v in data.items() if k in known})
        config.session_generation = int(config.session_generation or 0)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path} is damaged ({exc}). Run `python -m hub init --force`.") from None
    if config.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: host is {config.host}. The hub is reachable by other devices on your "
              "network without HTTPS. Use Tailscale and keep host 127.0.0.1.", file=sys.stderr)
    return config
