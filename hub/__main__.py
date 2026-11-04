"""Command line: python -m hub init | serve | token | doctor"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import config as cfg
from .auth import hash_token


def cmd_init(args) -> int:
    path = Path(args.config) if args.config else cfg.default_path()
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to replace it (this changes your token).")
        return 1
    roots = {}
    for item in args.root or []:
        name, sep, folder = item.partition("=")
        if not sep or not folder:
            print(f"--root needs name=folder, got '{item}'.")
            return 1
        if not Path(folder).expanduser().is_dir():
            print(f"Folder not found: {folder}")
            return 1
        roots[name] = str(Path(folder).expanduser())
    config, token = cfg.make(roots or None)
    if args.model:
        config.chat_model = args.model
    for folder in (config.notes_dir, config.inbox_dir):
        Path(folder).mkdir(parents=True, exist_ok=True)
    cfg.save(config, path)
    print(f"Wrote {path}\n")
    print("Your login token (shown once, copy it now):\n")
    print(f"    {token}\n")
    print("Shared folders:")
    for name, folder in config.roots.items():
        print(f"    {name}: {folder}")
    print("\nNext: python -m hub doctor      then      python -m hub serve")
    return 0


def cmd_token(args) -> int:
    path = Path(args.config) if args.config else cfg.default_path()
    config = cfg.load(path)
    token = secrets.token_urlsafe(24)
    config.token_hash = hash_token(token)
    config.session_secret = secrets.token_bytes(32)  # logs every phone out
    cfg.save(config, path)
    print(f"New token (shown once):\n\n    {token}\n\nEvery phone is logged out. Restart the hub.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app
    path = Path(args.config) if args.config else cfg.default_path()
    config = cfg.load(path)
    print(f"Laptop Hub on http://{config.host}:{config.port}  (config {path})")
    print("Put HTTPS in front with:  tailscale serve --bg " + str(config.port))
    uvicorn.run(create_app(config, config_path=path), host=config.host, port=config.port, log_level="info")
    return 0


def cmd_doctor(args) -> int:
    path = Path(args.config) if args.config else cfg.default_path()
    ok = True

    def line(good: bool | None, text: str):
        print(f"[{'ok' if good else ('--' if good is None else 'FIX')}] {text}")

    try:
        config = cfg.load(path)
        line(True, f"config {path}")
    except cfg.ConfigError as exc:
        line(False, str(exc))
        return 1
    for name, folder in config.roots.items():
        good = Path(folder).is_dir()
        ok &= good
        line(good, f"folder {name}: {folder}")
    try:
        with urllib.request.urlopen(config.ollama_host + "/api/tags", timeout=3) as resp:
            models = [m["model"] for m in json.load(resp)["models"]]
        line(True, f"Ollama running, models: {', '.join(models) or 'none'}")
        if config.chat_model not in models:
            ok = False
            line(False, f"chat model '{config.chat_model}' missing: ollama pull {config.chat_model}")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        ok = False
        line(False, "Ollama not reachable (start the Ollama app). Needed for Explain/Ask and tags.")
    try:
        import faster_whisper  # noqa: F401
        line(True, "faster-whisper installed (voice notes)")
    except ImportError:
        ok = False
        line(False, "faster-whisper missing: pip install -r requirements-voice.txt (voice)")
    import os
    line(bool(os.environ.get("ANTHROPIC_API_KEY")) or None,
         "ANTHROPIC_API_KEY set (assistant and 'Ask Claude')" if os.environ.get("ANTHROPIC_API_KEY")
         else "ANTHROPIC_API_KEY not set: the assistant needs it, or choose the local model")
    for name, argv in config.shell_table().items():
        found = shutil.which(argv[0]) is not None
        line(found or None, f"shell {name}: {argv[0]}" + ("" if found else " (not found)"))
    if sys.platform == "win32":
        try:
            import winpty  # noqa: F401
            line(True, "pywinpty installed (Windows terminal)")
        except ImportError:
            ok = False
            line(False, "pywinpty missing: pip install pywinpty")
    line(shutil.which("tailscale") is not None or None,
         "tailscale found" if shutil.which("tailscale")
         else "tailscale not found: needed for HTTPS and remote access (tailscale.com/download)")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub", description=__doc__)
    parser.add_argument("--config", help="path to hub.json (default ~/.laptop-hub/hub.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create the config and your login token")
    init.add_argument("--root", action="append", metavar="NAME=FOLDER",
                      help="a folder to share, e.g. --root study=C:\\Users\\me\\notes (repeatable)")
    init.add_argument("--model", help="local chat model for Explain/Ask (default qwen3:4b)")
    init.add_argument("--force", action="store_true")
    sub.add_parser("serve", help="start the hub")
    sub.add_parser("token", help="make a new login token and log all phones out")
    sub.add_parser("doctor", help="check that everything the hub needs is in place")
    args = parser.parse_args(argv)
    return {"init": cmd_init, "serve": cmd_serve, "token": cmd_token, "doctor": cmd_doctor}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
