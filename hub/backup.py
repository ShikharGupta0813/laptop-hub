"""`python -m hub backup [--to DIR] [--restore ZIP]`: one zip of everything that cannot be re-downloaded.

Goes in: hub.json (WITHOUT the login token hash and session secret), update.log, the hub data folder (the
SQLite databases and JSON state), and your HubNotes / HubInbox folders. Never goes in: .venv, .git, caches,
trash, upload scratch space, model files (.gguf, .safetensors ...). API keys are not on disk at all (they live
in the Windows keyring), so they are not in the zip either.

Backups are named redline-backup-YYYY-MM-DD.zip (a second run on the same day replaces that day's file) and
only the newest 8 are kept. Everything that touches the clock, the network or the prompt is injectable for tests.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from . import config as cfg

KEEP = 8
NAME_RE = re.compile(r"^redline-backup-\d{4}-\d{2}-\d{2}\.zip$")
SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", "node_modules", "trash", "uploads-tmp", "backups"}
SKIP_SUFFIXES = {".gguf", ".bin", ".safetensors", ".pt", ".pth", ".onnx", ".ckpt", ".pyc", ".tmp", ".part"}
SKIP_NAMES = {"kb.sqlite"}          # the "Ask my files" index: rebuilt by the hub, can be huge
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
MAX_FILE_BYTES = 500 * 1024 * 1024
REDACT = ("token_hash", "session_secret")
CONFIRM_WORD = "RESTORE"
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def default_dest(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Documents" / "REDLINE-backups"


def _skip(path: Path) -> bool:
    return path.name.lower() in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES


def _walk(base: Path, prefix: str, exclude: Path | None, skipped: list[str]):
    """Yield (arcname, file) below `base`, pruning junk folders; unreadable things are noted, never raised."""
    if not base.is_dir():
        return
    errors: list[OSError] = []
    for root, dirs, files in os.walk(base, onerror=errors.append):
        here = Path(root)
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not (here / d).is_symlink()
                   and not (exclude and (here / d).resolve() == exclude)]
        for name in files:
            path = here / name
            if _skip(path) or path.is_symlink():
                continue
            yield f"{prefix}/{path.relative_to(base).as_posix()}", path
    for err in errors:
        skipped.append(f"{prefix}: {getattr(err, 'filename', '') or 'folder'} (unreadable)")


def redacted_config_text(config_path: Path) -> str:
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    for key in REDACT:
        if key in data:
            data[key] = ""
    return json.dumps(data, indent=2) + "\n"


def _snapshot_sqlite(path: Path, tmpdir: Path, number: int) -> Path | None:
    """A consistent copy of a live database (the hub may be writing). None if it is not a readable database."""
    target = tmpdir / f"{number}-{path.name}"
    try:
        src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return target
    except sqlite3.Error:
        return None


def make_backup(config_path: Path, dest_dir: Path | None = None, *, now: Callable[[], dt.datetime] = dt.datetime.now,
                keep: int = KEEP, name: str | None = None, rotate: bool = True,
                say: Callable[[str], None] = print) -> tuple[int, Path | None, dict]:
    """Returns (exit code, zip path, info). info has files, bytes (zip size), skipped, parts."""
    config_path = Path(config_path)
    info: dict = {"files": 0, "bytes": 0, "skipped": [], "parts": {}}
    try:
        config = cfg.load(config_path)
    except cfg.ConfigError as exc:
        say(str(exc))
        return EXIT_FAILED, None, info
    backup_dir = getattr(config, "backup_dir", "")
    dest = Path(dest_dir) if dest_dir else (Path(backup_dir).expanduser() if backup_dir else default_dest())
    stamp = now()
    final = dest / (name or f"redline-backup-{stamp:%Y-%m-%d}.zip")
    tmp_zip = final.with_name(final.name + ".part")
    skipped: list[str] = info["skipped"]
    try:
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()
        with tempfile.TemporaryDirectory(prefix="redline-bk-") as tmp:
            tmpdir = Path(tmp)
            counter = [0]
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                def add(arc: str, path: Path, part: str) -> None:
                    src = path
                    if path.suffix.lower() in SQLITE_SUFFIXES:
                        counter[0] += 1
                        snap = _snapshot_sqlite(path, tmpdir, counter[0])
                        if snap is not None:
                            src = snap
                    try:
                        if src.stat().st_size > MAX_FILE_BYTES:
                            skipped.append(f"{arc} (over 500 MB)")
                            return
                        zf.write(src, arc)
                    except OSError:
                        skipped.append(f"{arc} (unreadable)")
                        return
                    info["files"] += 1
                    info["parts"][part] = info["parts"].get(part, 0) + 1

                try:
                    zf.writestr("config/hub.json", redacted_config_text(config_path))
                    info["files"] += 1
                    info["parts"]["config"] = 1
                except (OSError, ValueError):
                    skipped.append("config/hub.json (unreadable)")
                log = config_path.parent / "update.log"
                if log.is_file():
                    add("config/update.log", log, "config")
                groups = [("data", config.data_path()), ("notes", Path(config.notes_dir).expanduser()),
                          ("inbox", Path(config.inbox_dir).expanduser())]
                for part, folder in groups:
                    for arc, path in _walk(folder, part, dest_resolved, skipped):
                        base = re.sub(r"-(wal|shm|journal)$", "", path.name)
                        if base != path.name and path.with_name(base).exists():
                            continue   # captured inside the database snapshot
                        add(arc, path, part)
                zf.writestr("manifest.json", json.dumps({
                    "made": stamp.isoformat(timespec="seconds"), "files": info["files"],
                    "note": "hub.json has no token hash or session secret; keys live in the Windows keyring"}, indent=2))
        os.replace(tmp_zip, final)
    except OSError as exc:
        say(f"Backup failed: {type(exc).__name__} writing to {dest}. Is the folder writable and the disk not full?")
        try:
            tmp_zip.unlink()
        except OSError:
            pass
        return EXIT_FAILED, None, info
    info["bytes"] = final.stat().st_size
    if rotate and not name:
        info["removed"] = rotate_backups(dest, keep)
    say(f"Backup written: {final}")
    say(f"  {info['files']} files, {human_size(info['bytes'])}: " +
        ", ".join(f"{k} {v}" for k, v in info["parts"].items()))
    for item in skipped[:10]:
        say(f"  skipped: {item}")
    if len(skipped) > 10:
        say(f"  ... and {len(skipped) - 10} more skipped")
    return EXIT_OK, final, info


def rotate_backups(dest: Path, keep: int = KEEP) -> list[str]:
    """Delete all but the newest `keep` redline-backup-DATE.zip files. Other files are never touched."""
    mine = sorted((p for p in Path(dest).glob("redline-backup-*.zip") if NAME_RE.match(p.name)), key=lambda p: p.name)
    removed = []
    for old in (mine[:-keep] if keep > 0 else mine):
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    return removed


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.0f} MB"
    return f"{max(1, n // 1024) if n else 0} KB"


# ---- restore ---------------------------------------------------------------------------------------------

def _safe_member(name: str) -> bool:
    p = Path(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and ":" not in name and "\\" not in name


def restore(zip_path: Path, config_path: Path, *, confirm: Callable[[str], str] = input,
            now: Callable[[], dt.datetime] = dt.datetime.now, say: Callable[[str], None] = print,
            safety_dir: Path | None = None) -> int:
    zip_path, config_path = Path(zip_path), Path(config_path)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile):
        say(f"{zip_path} is not a readable backup zip.")
        return EXIT_USAGE
    with zf:
        names = zf.namelist()
        if "manifest.json" not in names or not any(n.startswith(("config/", "data/")) for n in names):
            say(f"{zip_path.name} does not look like a REDLINE backup (no manifest). Nothing was changed.")
            return EXIT_USAGE
        if [n for n in names if not _safe_member(n)]:
            say("This zip contains unsafe paths; refusing to restore it. Nothing was changed.")
            return EXIT_FAILED
        files = [n for n in names if not n.endswith("/") and n != "manifest.json"]
        say(f"Restoring {zip_path.name} ({len(files)} files) over the current hub state:")
        say(f"  config: {config_path.parent}")
        say("This replaces files with the same name. A safety copy of the current state is made first.")
        answer = confirm(f"Type {CONFIRM_WORD} to continue, anything else cancels: ")
        if (answer or "").strip() != CONFIRM_WORD:
            say("Cancelled. Nothing was changed.")
            return EXIT_FAILED
        old_secrets: dict = {}
        if config_path.exists():
            try:
                cur = cfg.load(config_path)
            except cfg.ConfigError:
                cur = None
            backup_dir = getattr(cur, "backup_dir", "") if cur else ""
            dest = Path(safety_dir) if safety_dir else (Path(backup_dir).expanduser() if backup_dir else default_dest())
            code, safety, _ = make_backup(config_path, dest, now=now, rotate=False, say=lambda _m: None,
                                          name=f"before-restore-{now():%Y-%m-%d-%H%M%S}.zip")
            if code != EXIT_OK:
                say("Could not make the safety copy, so nothing was restored.")
                return EXIT_FAILED
            say(f"Safety copy of the current state: {safety}")
            try:
                old = json.loads(config_path.read_text(encoding="utf-8"))
                old_secrets = {k: old[k] for k in REDACT if old.get(k)}
            except (OSError, ValueError):
                pass
        # hub.json first, keeping THIS laptop's login secrets (the zip has none).
        try:
            data = json.loads(zf.read("config/hub.json").decode("utf-8"))
        except (KeyError, ValueError):
            data = None
        if data is not None:
            data.update(old_secrets)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            config = cfg.load(config_path)
        except cfg.ConfigError as exc:
            say(f"Restored config is not usable ({exc}). The safety copy is untouched.")
            return EXIT_FAILED
        targets = {"data": config.data_path(), "notes": Path(config.notes_dir).expanduser(),
                   "inbox": Path(config.inbox_dir).expanduser(), "config": config_path.parent}
        count, failed = 0, 0
        for member in files:
            part, _, rel = member.partition("/")
            if part not in targets or not rel or member == "config/hub.json":
                continue
            out = targets[part] / rel
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if out.suffix.lower() in SQLITE_SUFFIXES:
                    for side in SQLITE_SIDECARS:      # a stale WAL would fight the restored database
                        stale = out.with_name(out.name + side)
                        if stale.exists():
                            stale.unlink()
                count += 1
            except OSError:
                failed += 1
                say(f"  could not write {member}")
    say(f"Restored {count} files" + (f", {failed} failed" if failed else "") + ".")
    if not old_secrets:
        say("This laptop had no login token: run  python -m hub token  to make one.")
    say("Next: restart the hub (log out and in, or: schtasks /End /TN LaptopHub  then  schtasks /Run /TN LaptopHub).")
    return EXIT_FAILED if failed else EXIT_OK


def proactive_event(ok: bool, size: int = 0) -> dict:
    """The event the hub's dispatcher gets for a backup: done is 'batch', failed is 'now' (see hub/proactive.py)."""
    if ok:
        return {"type": "backup_done", "title": "Backup done", "body": f"Backup done: {human_size(size)}", "link": "settings"}
    return {"type": "backup_failed", "title": "Backup failed", "body": "The backup failed. Look at the laptop.",
            "link": "settings", "urgent": True}


def run(args, *, now: Callable[[], dt.datetime] = dt.datetime.now, confirm: Callable[[str], str] = input,
        push: Callable[..., bool] | None = None) -> int:
    """Entry point for `python -m hub backup`."""
    path = Path(args.config) if getattr(args, "config", None) else cfg.default_path()
    if getattr(args, "restore", None):
        return restore(Path(args.restore), path, confirm=confirm, now=now)
    code, _zpath, info = make_backup(path, Path(args.to).expanduser() if getattr(args, "to", None) else None, now=now)
    from . import watchdog
    sender = push or watchdog.push_ntfy
    try:
        config = cfg.load(path)
        if code == EXIT_OK:
            sender(config, "REDLINE backup", f"Backup done: {human_size(info['bytes'])}")
        else:
            sender(config, "REDLINE backup", "Backup FAILED: look at the laptop", "high")
    except Exception:  # noqa: BLE001 - a failed push must never change the backup's result
        pass
    return code
