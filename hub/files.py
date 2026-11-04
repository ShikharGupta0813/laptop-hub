"""Read-only file access inside folders the owner chose.

Every path is resolved (symlinks included) and must still sit inside its root. A path
that escapes, such as `../..` or a link pointing outside, is refused.
"""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

MAX_SEARCH_RESULTS = 100
MAX_SEARCH_VISITS = 50_000
# Shown inline. Everything else is served as plain text or as a download, never as a page,
# so a file on the laptop can never run script in the hub's own origin.
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


class FileError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    modified: float


def resolve(roots: dict[str, str], root: str, rel: str = "") -> Path:
    if root not in roots:
        raise FileError(404, f"Unknown folder '{root}'.")
    base = Path(roots[root]).resolve()
    target = (base / rel.lstrip("/\\")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise FileError(403, "That path is outside the shared folder.") from None
    return target


def list_dir(roots: dict[str, str], root: str, rel: str = "") -> list[Entry]:
    folder = resolve(roots, root, rel)
    if not folder.is_dir():
        raise FileError(404, "Not a folder.")
    base = Path(roots[root]).resolve()
    entries = []
    try:
        children = list(os.scandir(folder))
    except PermissionError:
        raise FileError(403, "No permission to open that folder.") from None
    for child in children:
        try:
            stat = child.stat()
            real = Path(child.path).resolve()
            real.relative_to(base)  # a link that leaves the root is not listed
        except (OSError, ValueError):
            continue
        entries.append(Entry(child.name, child.is_dir(), stat.st_size, stat.st_mtime))
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def search(roots: dict[str, str], root: str, query: str, rel: str = "") -> list[dict]:
    start = resolve(roots, root, rel)
    base = Path(roots[root]).resolve()
    needle = query.lower().strip()
    if not needle:
        return []
    found, visits = [], 0
    for folder, dirs, names in os.walk(start):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for name in [*dirs, *names]:
            visits += 1
            if needle in name.lower():
                path = Path(folder, name)
                try:
                    path.resolve().relative_to(base)
                except (OSError, ValueError):
                    continue
                found.append({"name": name, "path": path.relative_to(base).as_posix(),
                              "is_dir": path.is_dir()})
                if len(found) >= MAX_SEARCH_RESULTS:
                    return found
        if visits > MAX_SEARCH_VISITS:
            break
    return found


def serving_type(path: Path) -> tuple[str, bool]:
    """(content type, as_attachment) for a file, safe for the hub's own origin."""
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed in INLINE_TYPES:
        return guessed, False
    try:
        with open(path, "rb") as f:
            f.read(4096).decode("utf-8")
        return "text/plain; charset=utf-8", False
    except UnicodeDecodeError:
        return "application/octet-stream", True
