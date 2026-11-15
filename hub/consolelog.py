"""The hub's own console: clear status lines printed to the terminal AND kept in <data>/hub-console.log.

Why: the owner started the hub, looked at the laptop terminal and saw nothing, so there was nothing to debug with. Now
`python -m hub serve` prints a startup banner (URL, login, voice engine, AI providers found, features on), and the AI
provider choice and its failures (with causes) are printed as they happen. The same lines are stored in a small
rotating file so the app can show them (Friday/Jarvis > Live log) when the terminal is hidden.

Nothing secret is ever written: tokens, keys and message text never come through here, only provider names, reasons and
settings names. `say()` does nothing until `enable()` is called (the serve command does that), so tests stay quiet.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

FILE_NAME = "hub-console.log"
MAX_BYTES = 400_000
_state: dict = {"path": None, "echo": True}
_lock = threading.Lock()


def enable(path: Path | None, echo: bool = True) -> None:
    _state["path"] = Path(path) if path else None
    _state["echo"] = echo


def disable() -> None:
    _state["path"] = None


def enabled() -> bool:
    return _state["path"] is not None


def say(text: str) -> None:
    """One timestamped line to the terminal and the file. Never raises."""
    if not enabled():
        return
    line = f"{time.strftime('%H:%M:%S')}  {' '.join(str(text).split())[:500]}"
    with _lock:
        if _state["echo"]:
            try:
                print(line, flush=True)
            except Exception:  # noqa: BLE001 - no console (pythonw): the file still gets it
                pass
        try:
            path: Path = _state["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size > MAX_BYTES:
                    path.write_text("", encoding="utf-8")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def banner_lines(config, config_path=None, *, tts_engine: str = "browser", provider_rows: list[dict] | None = None,
                 default_provider: str = "local") -> list[str]:
    """The startup banner as plain lines (pure, so it is testable)."""
    url = f"http://{config.host}:{config.port}"
    rows = provider_rows or []
    ready = [r["label"] for r in rows if r.get("ready")]
    missing = [f"{r['label']} ({r['reason'].rstrip('.')})" for r in rows if not r.get("ready")]
    feats = []
    for name, on in (("router", getattr(config, "router_enabled", False)),
                     ("tool failover", getattr(config, "tool_failover", False)),
                     ("fast commands", getattr(config, "fast_commands", False)),
                     ("brain", getattr(config, "brain_enabled", False)),
                     ("autopilot", getattr(config, "autopilot_enabled", False)),
                     ("proactive", getattr(config, "proactive_enabled", False)),
                     ("alerts/Friday", True), ("Jarvis start/stop", bool(getattr(config, "jarvis_dir", "")))):
        if on:
            feats.append(name)
    lines = [
        "=" * 64,
        f"Laptop Hub is starting  ({time.strftime('%Y-%m-%d %H:%M')})",
        f"URL:        {url}   (HTTPS for the phone: tailscale serve --bg {config.port})",
        f"Config:     {config_path or 'default'}",
        ("Login:      token required (hub.json keeps only its hash; `python -m hub token` makes a new one)"
         if getattr(config, "token_hash", "") else "Login:      NO TOKEN SET. Run: python -m hub init"),
        f"Voice:      {tts_engine} ({'the phone speaks, using its own voice' if tts_engine == 'browser' else 'the laptop makes the audio, the phone plays it'})",
        f"AI ready:   {', '.join(ready) if ready else 'NONE'}   (default: {default_provider})",
    ]
    if missing:
        lines.append("AI missing: " + "; ".join(missing))
    if not ready:
        lines.append("            -> fix: start the Ollama app, or run `gemini` once and log in, or python -m hub setup --only ai")
    lines.append("Features:   " + (", ".join(feats) or "basic"))
    lines.append("Live log:   in the app: Friday or Jarvis > Live log  (also: python -m hub doctor)")
    lines.append("=" * 64)
    return lines
