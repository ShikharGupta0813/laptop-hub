"""Command line: python -m hub setup | init | serve | token | doctor | add-script | update | install-updater | backup | watchdog | fix-deps"""
from __future__ import annotations

import argparse
import json
import re
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
    if args.jarvis_dir:
        jarvis = Path(args.jarvis_dir).expanduser()
        if not (jarvis / "jarvis.py").is_file():
            print(f"No jarvis.py in {jarvis}. Point --jarvis-dir at your jarvis-voice-assistant folder.")
            return 1
        config.jarvis_dir = str(jarvis)
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


SCRIPT_NAME_RE = cfg.SCRIPT_NAME_RE


def cmd_add_script(args) -> int:
    """Save a quick button into hub.json. Buttons are hand-edited on purpose (never through the API): this is
    the owner-run command on the laptop."""
    if not SCRIPT_NAME_RE.match(args.name or ""):
        print("The name must be 1-40 characters: lowercase letters, digits, - or _ (for example warp_connect).")
        return 1
    argv = list(args.argv or [])
    if not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
        print("Give the program after --, for example:  python -m hub add-script NAME --label \"Label\" -- program.exe arg")
        return 1
    path = Path(args.config) if args.config else cfg.default_path()
    try:
        config = cfg.load(path)
    except cfg.ConfigError as exc:
        print(exc)
        return 1
    label = (args.label or args.name).strip() or args.name
    replaced = cfg.set_script(config, args.name, label, argv)
    cfg.save(config, path)
    print(f"{'Replaced' if replaced else 'Saved'} quick button '{args.name}' in {path}:")
    print(f"    label: {label}")
    print(f"    runs:  {json.dumps(argv)}")
    print("Restart the hub (or wait for the next start) for it to appear.")
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
        with urllib.request.urlopen(config.ollama_host + "/api/tags", timeout=2) as resp:
            models = [m["model"] for m in json.load(resp)["models"]]
        line(True, f"Ollama running, models: {', '.join(models) or 'none'}")
        if config.chat_model not in models:
            ok = False
            line(False, f"chat model '{config.chat_model}' missing: ollama pull {config.chat_model}")
        tool_model = (config.local_tool_model or "").strip()
        if tool_model:
            have = tool_model in models or f"{tool_model}:latest" in models
            line(have, f"tool model '{tool_model}' " + ("installed (used for tool turns)" if have
                 else f"missing: ollama pull {tool_model}"))
    except Exception:  # noqa: BLE001 - slow, down, or garbage answer: never crash the doctor
        line(None, "Ollama not reachable (start the Ollama app). Needed for Explain/Ask and tags.")
    try:
        import faster_whisper  # noqa: F401
        line(True, "faster-whisper installed (voice notes)")
    except ImportError:
        ok = False
        line(False, "faster-whisper missing: python -m hub fix-deps (voice)")
    from . import providers as providers_mod
    avail = providers_mod.availability(config)
    ready = [avail[k]["label"] for k in ("claude", "claude_login", "gemini") if avail[k]["ready"]]
    if ready:
        line(True, "AI ready: Local model, " + ", ".join(ready)
             + f" (default: {providers_mod.LABELS.get(providers_mod.resolve_default(config), 'local')})")
    else:
        line(None, "AI: only the local model. Optional: python -m hub setup --only ai (Gemini key or Claude login)")
    line(None, f"router: {'on' if config.router_enabled else 'off'} (auto: chit-chat local, tools via Gemini); "
               f"tool failover to Gemini: {'on' if config.tool_failover else 'off'}")
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
    from . import doctor_checks as dc
    probe = dc.Probe()
    extra: list[tuple] = []
    for fn in (lambda: dc.check_git(probe), lambda: dc.check_hub_running(probe, config.port),
               lambda: dc.check_tailscale_serve(probe), lambda: dc.check_pip(probe)):
        try:
            extra.append(fn())
        except Exception:  # noqa: BLE001 - never crash the doctor
            extra.append((None, "a check could not run"))
    try:
        extra += dc.check_requirements(probe)
    except Exception:  # noqa: BLE001
        extra.append((None, "requirements: could not check"))
    extra += dc.wizard_lines(path)
    extra += dc.check_wake(config)
    for good, text in extra:
        if good is False:
            ok = False
        line(good, text)
    return 0 if ok else 1


def cmd_fix_deps(args) -> int:
    """Install the requirements through this interpreter (never pip.exe, which Windows Application Control may block)."""
    import subprocess
    from . import updater
    root = updater.repo_root()
    argv = [sys.executable, "-m", "pip", "install"]
    for name in ("requirements.txt", "requirements-voice.txt"):
        if (root / name).is_file():
            argv += ["-r", name]
    print("Running: " + " ".join(argv))
    try:
        rc = subprocess.run(argv, cwd=str(root)).returncode
    except OSError as exc:
        print(f"Could not start Python's pip ({type(exc).__name__}).")
        return 1
    print("Dependencies are installed." if rc == 0 else "pip failed. If Windows blocked it, ask for the Application Control rule, or try: python -m pip --version")
    return rc


def cmd_update(args) -> int:
    from . import updater
    path = Path(args.config) if args.config else cfg.default_path()
    return updater.Updater(updater.repo_root(), updater.log_path_for(path), config_path=path).run(restart=args.restart)


def cmd_install_updater(args) -> int:
    from .setup_wizard import Wizard, step_updater
    path = Path(args.config) if args.config else cfg.default_path()
    w = Wizard(path)
    result = w.report("updater", step_updater(w))
    return 1 if result.status == "fail" else 0


def cmd_backup(args) -> int:
    from . import backup
    return backup.run(args)


def cmd_watchdog(args) -> int:
    from . import watchdog
    return watchdog.run(args)


def cmd_setup(args) -> int:
    from .setup_wizard import run_setup
    return run_setup(args)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub", description=__doc__)
    parser.add_argument("--config", help="path to hub.json (default ~/.laptop-hub/hub.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create the config and your login token")
    init.add_argument("--root", action="append", metavar="NAME=FOLDER",
                      help="a folder to share, e.g. --root study=C:\\Users\\me\\notes (repeatable)")
    init.add_argument("--jarvis-dir", metavar="FOLDER", help="your jarvis-voice-assistant folder (optional)")
    init.add_argument("--model", help="local chat model for Explain/Ask (default qwen3:4b)")
    init.add_argument("--force", action="store_true")
    setup = sub.add_parser("setup", help="interactive one-command setup: Jarvis, Gmail, Asana, calendar, push, auto-start")
    setup.add_argument("--only", metavar="STEPS", help="comma list of steps: jarvis,gmail,asana,calendar,chat,ai,gemini,phone,name,warp,autostart,updater,backup,watchdog "
                       "(e.g. --only gmail,calendar)")
    setup.add_argument("--check", action="store_true", help="only test what is already configured; change nothing")
    setup.add_argument("--all", action="store_true", help="no questions: set up the updater, watchdog and backup tasks in one go")
    upd = sub.add_parser("update", help="pull the latest code from GitHub (and restart the hub with --restart)")
    upd.add_argument("--restart", action="store_true", help="after a successful pull, restart the LaptopHub scheduled task")
    sub.add_parser("install-updater", help="Windows: add the 'Update REDLINE' phone button (scheduled task LaptopHubUpdate)")
    bk = sub.add_parser("backup", help="zip settings, data, notes and inbox (keeps the last 8); --restore ZIP puts one back")
    bk.add_argument("--to", metavar="DIR", help="folder for the zip (default: backup_dir or Documents\\REDLINE-backups)")
    bk.add_argument("--restore", metavar="ZIP", help="restore a backup zip (asks you to type RESTORE first)")
    wd = sub.add_parser("watchdog", help="check the hub and Tailscale, repair them, push to the phone if needed")
    wd.add_argument("--once", action="store_true", help="one pass then exit (what the scheduled task uses)")
    sub.add_parser("fix-deps", help="install requirements.txt and requirements-voice.txt through python -m pip")
    sub.add_parser("serve", help="start the hub")
    sub.add_parser("token", help="make a new login token and log all phones out")
    sub.add_parser("doctor", help="check that everything the hub needs is in place")
    add = sub.add_parser("add-script", help="save a quick button: add-script NAME --label \"Label\" -- PROGRAM ARGS")
    add.add_argument("name", help="lowercase letters, digits, - and _ (for example warp_connect)")
    add.add_argument("--label", help="the text on the button")
    # Everything after a bare "--" is the program and its arguments, untouched by option parsing.
    argv = list(sys.argv[1:] if argv is None else argv)
    tail: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, tail = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if args.command == "add-script":
        args.argv = tail
    return {"init": cmd_init, "serve": cmd_serve, "token": cmd_token, "doctor": cmd_doctor,
            "add-script": cmd_add_script, "setup": cmd_setup, "update": cmd_update,
            "install-updater": cmd_install_updater, "backup": cmd_backup, "watchdog": cmd_watchdog, "fix-deps": cmd_fix_deps}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
