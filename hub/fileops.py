"""The write side of the Files tab: edit, create, rename, move, copy, upload, trash, zip.

Changes are allowed only inside the roots named in `config.writable_roots`. Every path goes through the
same safe resolver as the read-only Files tab (`hub.files.resolve`), so `..` and links that point outside a
root are refused. Nothing is ever hard-deleted except by emptying the trash.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import heapq
import io
import itertools
import json
import os
import posixpath
import re
import secrets
import shutil
import stat as statmod
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StrictBool
from starlette.formparsers import FormParserError, multipart, parse_options_header

from . import config as hubconfig
from . import files
from .common import Ctx, JsonFile

MAX_TEXT = 2 * 1024 * 1024            # biggest file the editor opens or saves
MAX_UPLOAD = 100 * 1024 * 1024        # biggest upload
MAX_ZIP = 500 * 1024 * 1024           # biggest folder that is zipped (or copied) in one go
MAX_ZIP_ENTRIES = 100_000
MAX_WALK_VISITS = 50_000
MAX_NAME = 200
CHUNK = 64 * 1024
READ_ONLY = "That folder is read-only. Add it to writable_roots in hub.json to change files there."
CHANGED = "This file changed on the laptop since you opened it"
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
SKIP_DIRS = {"node_modules", "__pycache__", ".git"}


# ---- names ------------------------------------------------------------------------------------

def clean_name(raw: str) -> str:
    """A safe file name from whatever a browser or a person typed ('' if nothing usable is left).

    Keeps only the last path piece (so `../../x`, `/etc/passwd` and `C:\\a\\b.txt` become `x`, `passwd`,
    `b.txt`), drops NUL and control characters, replaces characters Windows forbids, removes trailing dots
    and spaces and prefixes Windows device names (CON, NUL, COM1...).
    """
    text = str(raw).replace("\x00", "")
    pieces = [p for p in re.split(r"[\\/]", text) if p]
    name = pieces[-1] if pieces else ""
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name)
    name = name.lstrip(" ").rstrip(". ")
    if not name:
        return ""
    if name.split(".")[0].rstrip().upper() in RESERVED:
        name = "_" + name
    if len(name) > MAX_NAME:
        stem, ext = os.path.splitext(name)
        name = stem[:MAX_NAME - len(ext)] + ext if len(ext) < 20 else name[:MAX_NAME]
    return name


def valid_name(name: str) -> bool:
    """True when `name` is a single, already-clean name (what rename, mkdir and write insist on)."""
    return bool(name) and name not in (".", "..") and clean_name(name) == name


BAD_NAME = ("That name can't be used. Use a plain name without slashes, and avoid names like CON or NUL "
            "and names that end in a dot or a space.")


def unique_name(folder: Path, name: str) -> str:
    """`name`, or `name (2).ext`, `name (3).ext`... when something of that name is already there."""
    if not os.path.lexists(folder / name):
        return name
    stem, ext = os.path.splitext(name)
    for n in itertools.count(2):
        candidate = f"{stem} ({n}){ext}"
        if not os.path.lexists(folder / candidate):
            return candidate
    raise AssertionError  # pragma: no cover


# ---- small helpers ----------------------------------------------------------------------------

def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(exc: files.FileError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@contextlib.contextmanager
def _guard_os():
    """Turn file system errors into plain sentences."""
    try:
        yield
    except files.FileError as exc:
        raise _fail(exc) from None
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(404, "That file or folder is no longer there.") from None
    except FileExistsError:
        raise HTTPException(409, "Something with that name is already there.") from None
    except PermissionError:
        raise HTTPException(403, "The laptop does not allow changes there.") from None
    except IsADirectoryError:
        raise HTTPException(400, "That is a folder, not a file.") from None
    except NotADirectoryError:
        raise HTTPException(400, "That is a file, not a folder.") from None
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(507, "The laptop's disk is full.") from None
        if exc.errno == errno.ENAMETOOLONG:
            raise HTTPException(400, "That name is too long.") from None
        raise HTTPException(500, "The laptop could not do that.") from None
    except ValueError:  # an embedded NUL in a path, for example
        raise HTTPException(400, "That path is not valid.") from None


def _plain_rel(rel: str) -> str:
    if not isinstance(rel, str) or "\x00" in rel:
        raise HTTPException(400, "That path is not valid.")
    return rel


def _sidecar_write(path: Path, data: dict) -> None:
    JsonFile(path).save(data)


# ---- bodies -----------------------------------------------------------------------------------

class PathBody(BaseModel):
    root: str
    path: str


class WriteBody(PathBody):
    content: str
    etag: str | None = None
    create_only: StrictBool = False


class RenameBody(PathBody):
    new_name: str


class MoveBody(PathBody):
    dest_root: str
    dest_dir: str = ""


class DeleteBody(PathBody):
    confirm: StrictBool = False


class RestoreBody(BaseModel):
    id: str


class EmptyBody(BaseModel):
    confirm: StrictBool = False
    root: str | None = None


# One change at a time, so an etag check and the replace after it can never interleave with another write.
_LOCK = threading.RLock()


# ---- the router -------------------------------------------------------------------------------

def make_router(ctx: Ctx) -> APIRouter:
    router = APIRouter(prefix="/api/fileops", dependencies=[Depends(ctx.need_auth)])
    lock = _LOCK

    # -- roots and permissions

    def roots() -> dict[str, str]:
        return ctx.config.roots

    def base_of(root: str) -> Path:
        return files.resolve(roots(), root, "")

    def writable(root: str) -> Path:
        base = base_of(root)            # 404 for an unknown folder, before the read-only answer
        if root not in ctx.config.writable_roots:
            raise HTTPException(403, READ_ONLY)
        return base

    def protected() -> list[Path]:
        """The hub's own data, audit log and config: never changed or copied out through this module."""
        found = []
        for p in (ctx.config.data_path(), ctx.audit_path, hubconfig.default_path()):
            if p:
                try:
                    found.append(Path(p).resolve())
                except (OSError, ValueError):
                    continue
        return found

    def deny_protected(path: Path) -> None:
        resolved = path.resolve()
        for prot in protected():
            if _within(resolved, prot) or _within(prot, resolved):
                raise HTTPException(403, "That belongs to the hub itself and can't be changed here.")

    def deny_inside_protected(path: Path) -> None:
        """For reading out (copy, zip): a folder that merely contains hub files is fine, they are skipped."""
        if is_protected_inside(path):
            raise HTTPException(403, "That belongs to the hub itself and can't be changed here.")

    def is_protected_inside(path: Path) -> bool:
        resolved = path.resolve()
        return any(_within(resolved, prot) for prot in protected())

    def locate(root: str, rel: str) -> Path:
        """The real path (links followed) of rel inside root; refused when it leaves the root."""
        return files.resolve(roots(), root, _plain_rel(rel))

    def locate_entry(root: str, rel: str) -> Path:
        """The path of the entry itself, so a link can be renamed or trashed without touching its target."""
        rel = _plain_rel(rel)
        files.resolve(roots(), root, rel)    # refuses anything that leaves the root
        lexical = posixpath.normpath("/" + rel.replace("\\", "/")).lstrip("/")
        base = base_of(root)
        if lexical in ("", "."):
            return base
        parent_rel, name = posixpath.split(lexical)
        return files.resolve(roots(), root, parent_rel) / name

    def rel_of(root: str, path: Path) -> str:
        rel = path.relative_to(base_of(root)).as_posix()
        return "" if rel == "." else rel

    def not_the_root(root: str, path: Path) -> None:
        if path == base_of(root):
            raise HTTPException(400, "Pick a file or folder inside the shared folder, not the folder itself.")

    # -- the trash

    def trash_base() -> Path:
        return ctx.config.data_path() / "trash"

    def trash_dir(root: str) -> Path:
        return trash_base() / quote(root, safe="")

    def trash_entry(item_id: str) -> tuple[str, Path, Path]:
        """(root, entry path, sidecar path) for a trash id, which is validated, never trusted."""
        head, sep, entry = item_id.partition("/")
        if not sep or not head or not entry or "/" in entry or "\\" in entry or "\x00" in item_id \
                or entry.startswith(".") or entry in (".", ".."):
            raise HTTPException(404, "That item is not in the trash.")
        root = unquote(head)
        folder = trash_base() / head
        return root, folder / entry, folder / ".meta" / f"{entry}.json"

    def tree_stats(path: Path, deadline_visits: int = 200_000) -> tuple[int, int, int, bool]:
        """(files, folders, bytes, truncated) under path, never following links."""
        n_files = n_dirs = total = visits = 0
        for folder, dirs, names in os.walk(path):
            n_dirs += len(dirs)
            for name in names:
                visits += 1
                try:
                    st = os.lstat(os.path.join(folder, name))
                except OSError:
                    continue
                if statmod.S_ISREG(st.st_mode):
                    n_files += 1
                    total += st.st_size
            if visits > deadline_visits:
                return n_files, n_dirs, total, True
        return n_files, n_dirs, total, False

    # -- reading

    @router.get("/roots")
    def list_roots():
        return [{"name": name, "writable": name in ctx.config.writable_roots} for name in roots()]

    @router.get("/read")
    def read(root: str, path: str):
        with _guard_os():
            target = locate(root, path)
            if not target.is_file():
                raise HTTPException(404, "Not a file.")
            deny_protected(target)
            st = target.stat()
            if st.st_size > MAX_TEXT:
                raise HTTPException(413, "That file is too big to open here (the limit is 2 MB).")
            data = target.read_bytes()
        try:
            if b"\x00" in data:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, "This is not a plain text file, so it can't be opened for editing.") from None
        return {"root": root, "path": rel_of(root, target), "content": content,
                "etag": hashlib.sha256(data).hexdigest(), "mtime": st.st_mtime, "size": len(data),
                "writable": root in ctx.config.writable_roots}

    # -- writing

    @router.post("/mkdir")
    def mkdir(body: PathBody):
        with _guard_os():
            base = writable(body.root)
            target = locate(body.root, body.path)
            not_the_root(body.root, target)
            deny_protected(target)
            with lock:
                if target.is_dir():
                    return {"ok": True, "path": rel_of(body.root, target), "created": False}
                if os.path.lexists(target):
                    raise HTTPException(409, "A file with that name is already there.")
                # every folder that will be created needs a clean name
                missing = []
                probe = target
                while not os.path.lexists(probe) and probe != base:
                    missing.append(probe.name)
                    probe = probe.parent
                if not all(valid_name(name) for name in missing):
                    raise HTTPException(400, BAD_NAME)
                target.mkdir(parents=True)
            rel = rel_of(body.root, target)
        ctx.audit("fileops_mkdir", root=body.root, path=rel)
        return {"ok": True, "path": rel, "created": True}

    @router.post("/write")
    def write(body: WriteBody):
        with _guard_os():
            writable(body.root)
            target = locate(body.root, body.path)
            not_the_root(body.root, target)
            deny_protected(target)
            try:
                data = body.content.encode("utf-8")
            except UnicodeEncodeError:
                raise HTTPException(400, "That text can't be saved as UTF-8.") from None
            if len(data) > MAX_TEXT:
                raise HTTPException(413, "That file is too big to save here (the limit is 2 MB).")
            if not target.parent.is_dir():
                raise HTTPException(404, "The folder for that file doesn't exist.")
            with lock:
                exists = os.path.lexists(target)
                if exists and target.is_dir():
                    raise HTTPException(400, "That is a folder, not a file.")
                if not exists and not valid_name(target.name):
                    raise HTTPException(400, BAD_NAME)
                if body.create_only:
                    if exists:
                        raise HTTPException(409, "A file with that name is already there.")
                elif exists:
                    if body.etag is None:
                        raise HTTPException(409, "That file already exists. Open it first and save from there, "
                                                 "so nothing is overwritten by accident.")
                    if _sha256(target) != body.etag:
                        raise HTTPException(409, CHANGED)
                elif body.etag is not None:
                    raise HTTPException(409, CHANGED)   # it was deleted since it was opened
                tmp = target.parent / f".hub-{secrets.token_hex(6)}.tmp"
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                    with os.fdopen(fd, "wb") as out:
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                    if exists:
                        with contextlib.suppress(OSError):
                            shutil.copymode(target, tmp)
                    if body.create_only:
                        try:
                            os.link(tmp, target)       # fails if something appeared a moment ago
                        except FileExistsError:
                            raise HTTPException(409, "A file with that name is already there.") from None
                        except OSError:
                            os.replace(tmp, target)
                    else:
                        os.replace(tmp, target)
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                st = target.stat()
            rel = rel_of(body.root, target)
        ctx.audit("fileops_write", root=body.root, path=rel, bytes=len(data), created=not exists)
        return {"ok": True, "path": rel, "etag": hashlib.sha256(data).hexdigest(), "mtime": st.st_mtime,
                "size": st.st_size}

    @router.post("/rename")
    def rename(body: RenameBody):
        with _guard_os():
            writable(body.root)
            if not valid_name(body.new_name):
                raise HTTPException(400, BAD_NAME)
            source = locate_entry(body.root, body.path)
            not_the_root(body.root, source)
            deny_protected(source)
            with lock:
                if not os.path.lexists(source):
                    raise HTTPException(404, "That file or folder is no longer there.")
                dest = source.parent / body.new_name
                if os.path.lexists(dest):
                    raise HTTPException(409, "Something with that name is already there.")
                os.rename(source, dest)
            old, new = rel_of(body.root, source), rel_of(body.root, dest)
        ctx.audit("fileops_rename", root=body.root, path=old, new_path=new)
        return {"ok": True, "path": new}

    @router.post("/move")
    def move(body: MoveBody):
        with _guard_os():
            writable(body.root)
            writable(body.dest_root)
            source = locate_entry(body.root, body.path)
            not_the_root(body.root, source)
            deny_protected(source)
            dest_dir = locate(body.dest_root, body.dest_dir)
            deny_protected(dest_dir)
            with lock:
                if not os.path.lexists(source):
                    raise HTTPException(404, "That file or folder is no longer there.")
                if not dest_dir.is_dir():
                    raise HTTPException(404, "The folder to move it into doesn't exist.")
                if source.is_dir() and not source.is_symlink() and _within(dest_dir.resolve(), source.resolve()):
                    raise HTTPException(400, "A folder can't be moved into itself.")
                dest = dest_dir / source.name
                if dest == source:
                    raise HTTPException(400, "It is already in that folder.")
                if os.path.lexists(dest):
                    raise HTTPException(409, "Something with that name is already there.")
                shutil.move(str(source), str(dest))
            old, new = rel_of(body.root, source), rel_of(body.dest_root, dest)
        ctx.audit("fileops_move", root=body.root, path=old, dest_root=body.dest_root, dest_path=new)
        return {"ok": True, "root": body.dest_root, "path": new}

    def copy_tree(source: Path, dest: Path, base: Path, budget: list[int]) -> None:
        """Copy a folder, skipping links and the hub's own files; refuse links that leave the root."""
        dest.mkdir()
        for entry in sorted(os.scandir(source), key=lambda e: e.name):
            child = Path(entry.path)
            if entry.is_symlink():
                try:
                    child.resolve().relative_to(base)
                except ValueError:
                    raise HTTPException(403, "That folder contains a shortcut that points outside the shared "
                                             "folder, so it can't be copied.") from None
                continue
            if is_protected_inside(child):
                continue
            if entry.is_dir(follow_symlinks=False):
                copy_tree(child, dest / entry.name, base, budget)
            elif entry.is_file(follow_symlinks=False):
                budget[0] += entry.stat(follow_symlinks=False).st_size
                if budget[0] > MAX_ZIP:
                    raise HTTPException(413, "That folder is too big to copy in one go (the limit is 500 MB).")
                shutil.copy2(child, dest / entry.name)

    @router.post("/copy")
    def copy(body: MoveBody):
        with _guard_os():
            writable(body.dest_root)
            source = locate(body.root, body.path)         # the source may be in a read-only folder
            not_the_root(body.root, source)
            deny_inside_protected(source)
            dest_dir = locate(body.dest_root, body.dest_dir)
            deny_protected(dest_dir)
            with lock:
                if not os.path.lexists(source):
                    raise HTTPException(404, "That file or folder is no longer there.")
                if not dest_dir.is_dir():
                    raise HTTPException(404, "The folder to copy it into doesn't exist.")
                if source.is_dir() and _within(dest_dir, source):
                    raise HTTPException(400, "A folder can't be copied into itself.")
                dest = dest_dir / unique_name(dest_dir, source.name)
                if source.is_dir():
                    try:
                        copy_tree(source, dest, base_of(body.root), [0])
                    except BaseException:
                        shutil.rmtree(dest, ignore_errors=True)
                        raise
                else:
                    shutil.copy2(source, dest)
            old, new = rel_of(body.root, source), rel_of(body.dest_root, dest)
        ctx.audit("fileops_copy", root=body.root, path=old, dest_root=body.dest_root, dest_path=new)
        return {"ok": True, "root": body.dest_root, "path": new}

    # -- delete, trash

    @router.post("/delete")
    def delete(body: DeleteBody):
        with _guard_os():
            writable(body.root)
            if body.confirm is not True:
                raise HTTPException(409, "Moving this to the trash needs your confirmation. Confirm it and send again.")
            source = locate_entry(body.root, body.path)
            not_the_root(body.root, source)
            deny_protected(source)
            with lock:
                if not os.path.lexists(source):
                    raise HTTPException(404, "That file or folder is no longer there.")
                folder = trash_dir(body.root)
                folder.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                label = source.name[:150]
                entry = f"{stamp}-{label}"
                for n in itertools.count(2):
                    if not os.path.lexists(folder / entry):
                        break
                    entry = f"{stamp}~{n}-{label}"
                is_dir = source.is_dir() and not source.is_symlink()
                rel = rel_of(body.root, source)
                item_id = f"{quote(body.root, safe='')}/{entry}"
                meta = {"id": item_id, "root": body.root, "path": rel, "name": source.name, "is_dir": is_dir,
                        "size": None if is_dir else os.lstat(source).st_size, "deleted_at": time.time()}
                sidecar = folder / ".meta" / f"{entry}.json"
                _sidecar_write(sidecar, meta)
                try:
                    shutil.move(str(source), str(folder / entry))
                except BaseException:
                    with contextlib.suppress(OSError):
                        sidecar.unlink()
                    raise
        ctx.audit("fileops_delete", root=body.root, path=rel, trash_id=item_id)
        return {"ok": True, "id": item_id}

    @router.get("/trash")
    def list_trash():
        items = []
        base = trash_base()
        with _guard_os():
            folders = [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
            for folder in folders:
                for entry in folder.iterdir():
                    if entry.name.startswith("."):
                        continue
                    item_id = f"{folder.name}/{entry.name}"
                    meta = JsonFile(folder / ".meta" / f"{entry.name}.json").load(None)
                    if not isinstance(meta, dict):      # the note about it is missing: show what we know
                        meta = {"root": unquote(folder.name), "path": entry.name, "name": entry.name,
                                "is_dir": entry.is_dir(), "size": None, "deleted_at": entry.lstat().st_mtime}
                    items.append({"id": item_id, "root": meta.get("root"), "path": meta.get("path"),
                                  "name": meta.get("name"), "is_dir": bool(meta.get("is_dir")),
                                  "size": meta.get("size"), "deleted_at": meta.get("deleted_at")})
        items.sort(key=lambda i: i["deleted_at"] or 0, reverse=True)
        return {"items": items}

    @router.post("/trash/restore")
    def restore(body: RestoreBody):
        with _guard_os():
            root, entry, sidecar = trash_entry(body.id)
            if not os.path.lexists(entry):
                raise HTTPException(404, "That item is not in the trash.")
            writable(root)
            meta = JsonFile(sidecar).load(None)
            rel = meta.get("path") if isinstance(meta, dict) else None
            if not isinstance(rel, str) or not rel:
                rel = entry.name.split("-", 2)[-1]
            with lock:
                # the original location, re-checked now: it must still be inside the folder
                original = locate_entry(root, rel)
                deny_protected(original.parent)
                parent = original.parent
                if not parent.is_dir():
                    parent.mkdir(parents=True)
                dest = parent / unique_name(parent, original.name)
                shutil.move(str(entry), str(dest))
                with contextlib.suppress(OSError):
                    sidecar.unlink()
            back = rel_of(root, dest)
        ctx.audit("fileops_restore", root=root, path=back, trash_id=body.id)
        return {"ok": True, "root": root, "path": back}

    @router.post("/trash/empty")
    def empty_trash(body: EmptyBody):
        if body.confirm is not True:
            raise HTTPException(409, "Emptying the trash deletes everything in it for good. Confirm it and send again.")
        removed = 0
        with _guard_os(), lock:
            base = trash_base()
            data_dir = ctx.config.data_path().resolve()
            if not base.is_dir():
                folders = []
            elif not _within(base.resolve(), data_dir):
                raise HTTPException(403, "The trash folder is not where it should be.")
            elif body.root is not None:
                folders = [trash_dir(body.root)] if trash_dir(body.root).is_dir() else []
            else:
                folders = [p for p in base.iterdir() if p.is_dir()]
            for folder in folders:
                if folder.is_symlink() or folder.parent != base:
                    continue
                for entry in list(folder.iterdir()):
                    if entry.name == ".meta":
                        continue
                    if entry.is_symlink() or not entry.is_dir():
                        entry.unlink()
                    else:
                        shutil.rmtree(entry)
                    removed += 1
                shutil.rmtree(folder / ".meta", ignore_errors=True)
        ctx.audit("fileops_trash_empty", count=removed, root=body.root or "")
        return {"ok": True, "removed": removed}

    # -- upload

    def inbox_root() -> str:
        if "inbox" in roots():
            return "inbox"
        inbox = Path(ctx.config.inbox_dir).resolve()
        for name, path in roots().items():
            if Path(path).resolve() == inbox:
                return name
        return "inbox"

    @router.post("/upload")
    async def upload(request: Request):
        limit = MAX_UPLOAD
        ctype = request.headers.get("content-type", "")
        if not ctype.lower().startswith("multipart/form-data") or multipart is None:
            raise HTTPException(400, "Send the file as a form upload.")
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > limit + CHUNK:
            raise HTTPException(413, f"That file is too big (the limit is {limit // (1024 * 1024)} MB).")
        _, params = parse_options_header(ctype)
        boundary = params.get(b"boundary")
        if not boundary:
            raise HTTPException(400, "The upload was not understood.")

        staging = ctx.config.data_path() / "uploads-tmp"
        staging.mkdir(parents=True, exist_ok=True)
        state = {"size": 0, "file": None, "path": None, "filename": None, "field": None,
                 "name": "", "hname": b"", "hvalue": b"", "disp": b"", "fields": {}, "parts": 0, "done_file": False}

        def on_part_begin():
            state.update(hname=b"", hvalue=b"", disp=b"", field=None)
            state["parts"] += 1
            if state["parts"] > 20:
                raise HTTPException(400, "The upload was not understood.")

        def on_header_field(data, start, end):
            state["hname"] += data[start:end]

        def on_header_value(data, start, end):
            state["hvalue"] += data[start:end]

        def on_header_end():
            if state["hname"].lower() == b"content-disposition":
                state["disp"] = state["hvalue"]
            state["hname"] = state["hvalue"] = b""

        def on_headers_finished():
            _, options = parse_options_header(state["disp"])
            name = options.get(b"name", b"").decode("utf-8", "replace")
            if b"filename" in options:
                if name != "file":
                    raise HTTPException(400, "Choose a file to upload.")
                if state["path"] is not None:
                    raise HTTPException(400, "Upload one file at a time.")
                state["filename"] = options[b"filename"].decode("utf-8", "replace")
                fd, tmp = tempfile.mkstemp(dir=staging, prefix="up-")
                state["file"], state["path"] = os.fdopen(fd, "wb"), Path(tmp)
                state["name"] = "\0file"
            else:
                state["name"], state["field"] = name, bytearray()

        def on_part_data(data, start, end):
            chunk = data[start:end]
            if state["name"] == "\0file" and state["field"] is None and state["file"] is not None:
                state["size"] += len(chunk)
                if state["size"] > limit:
                    raise HTTPException(413, f"That file is too big (the limit is {limit // (1024 * 1024)} MB).")
                state["file"].write(chunk)
            elif state["field"] is not None:
                if len(state["field"]) + len(chunk) > 4096:
                    raise HTTPException(400, "The upload was not understood.")
                state["field"] += chunk

        def on_part_end():
            if state["field"] is not None:
                state["fields"][state["name"]] = state["field"].decode("utf-8", "replace")
            elif state["file"] is not None:
                state["file"].close()
                state["file"] = None
                state["done_file"] = True
            state["field"] = None
            state["name"] = ""

        callbacks = {"on_part_begin": on_part_begin, "on_part_data": on_part_data, "on_part_end": on_part_end,
                     "on_header_field": on_header_field, "on_header_value": on_header_value,
                     "on_header_end": on_header_end, "on_headers_finished": on_headers_finished}

        def cleanup():
            if state["file"] is not None:
                with contextlib.suppress(OSError):
                    state["file"].close()
            if state["path"] is not None:
                with contextlib.suppress(OSError):
                    state["path"].unlink()

        try:
            parser = multipart.MultipartParser(boundary, callbacks)
            async for chunk in request.stream():
                parser.write(chunk)
            parser.finalize()
            if not state["done_file"]:
                raise HTTPException(400, "Choose a file to upload.")
            with _guard_os():
                root = state["fields"].get("root") or inbox_root()
                writable(root)
                dest_dir = locate(root, state["fields"].get("dir", ""))
                deny_protected(dest_dir)
                if not dest_dir.is_dir():
                    raise HTTPException(404, "The folder to upload into doesn't exist.")
                name = clean_name((state["filename"] or "").replace("%00", "")) or "upload"
                with lock:
                    final = _place_new(state["path"], dest_dir, name)
                rel = rel_of(root, final)
            size = state["size"]
        except FormParserError:
            cleanup()
            raise HTTPException(400, "The upload was not understood.") from None
        except BaseException:
            cleanup()
            raise
        with contextlib.suppress(OSError):
            state["path"].unlink()
        ctx.audit("fileops_upload", root=root, path=rel, bytes=size)
        return {"ok": True, "root": root, "path": rel, "name": final.name, "size": size}

    # -- zip

    def plan_zip(root: str, start: Path) -> list[tuple[Path, str, bool]]:
        """(path, name in the zip, is_folder) for everything under start. Refuses links that leave the root."""
        base = base_of(root)
        top = start.name or root
        plan: list[tuple[Path, str, bool]] = []
        total = 0
        if start.is_file():
            return [(start, top, False)]
        plan.append((start, top, True))
        for folder, dirs, names in os.walk(start):
            keep = []
            for name in sorted(dirs) + sorted(names):
                path = Path(folder, name)
                arc = f"{top}/{path.relative_to(start).as_posix()}"
                if os.path.islink(path):
                    try:
                        path.resolve().relative_to(base)
                    except (ValueError, OSError):
                        raise HTTPException(403, "That folder contains a shortcut that points outside the "
                                                 "shared folder, so it can't be zipped.") from None
                    continue     # a link that stays inside the folder is not followed or included
                if is_protected_inside(path):
                    continue
                if name in dirs:
                    keep.append(name)
                    plan.append((path, arc, True))
                else:
                    try:
                        st = os.lstat(path)
                    except OSError:
                        continue
                    if not statmod.S_ISREG(st.st_mode):
                        continue
                    total += st.st_size
                    plan.append((path, arc, False))
                if total > MAX_ZIP:
                    raise HTTPException(413, "That folder is too big to zip (the limit is 500 MB).")
                if len(plan) > MAX_ZIP_ENTRIES:
                    raise HTTPException(413, "That folder has too many files to zip in one go.")
            dirs[:] = keep
        return plan

    class Sink(io.RawIOBase):
        """Where the zip is written to: collects bytes so they can be sent as they are produced."""

        def __init__(self):
            self.buf = bytearray()
            self.pos = 0

        def writable(self):
            return True

        def write(self, b):
            self.buf += b
            self.pos += len(b)
            return len(b)

        def tell(self):
            return self.pos

        def take(self) -> bytes:
            data = bytes(self.buf)
            self.buf.clear()
            return data

    def zip_stream(plan):
        sink = Sink()
        sent = 0
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, arc, is_dir in plan:
                if is_dir:
                    archive.writestr(arc + "/", b"")
                    continue
                try:
                    source = open(path, "rb")
                except OSError:
                    continue     # it vanished or became unreadable after the plan was made
                with source:
                    info = zipfile.ZipInfo.from_file(path, arc, strict_timestamps=False)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(info, "w") as out:
                        while chunk := source.read(CHUNK):
                            sent += len(chunk)
                            if sent > MAX_ZIP:       # it grew while being zipped
                                return
                            out.write(chunk)
                            if sink.buf:
                                yield sink.take()
                if sink.buf:
                    yield sink.take()
        yield sink.take()

    @router.get("/zip")
    def zip_folder(root: str, path: str = ""):
        with _guard_os():
            start = locate(root, path)
            if not start.exists():
                raise HTTPException(404, "That file or folder is no longer there.")
            deny_inside_protected(start)
            plan = plan_zip(root, start)
        name = (start.name or root) + ".zip"
        ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", name) or "folder.zip"
        headers = {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"}
        return StreamingResponse(zip_stream(plan), media_type="application/zip", headers=headers)

    # -- looking around

    def walk_files(base: Path):
        """Regular, visible files under base (no hidden folders, node_modules, .git or links out)."""
        visits = 0
        for folder, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                       and not is_protected_inside(Path(folder, d))]
            for name in names:
                if name.startswith("."):
                    continue
                visits += 1
                path = Path(folder, name)
                try:
                    st = os.lstat(path)
                    if statmod.S_ISLNK(st.st_mode):
                        path.resolve().relative_to(base)
                        st = os.stat(path)
                except (OSError, ValueError):
                    continue
                if statmod.S_ISREG(st.st_mode):
                    yield path, st
            if visits > MAX_WALK_VISITS:
                return

    @router.get("/recent")
    def recent(limit: int = Query(20, ge=1, le=100)):
        heap: list = []
        counter = itertools.count()
        deadline = time.monotonic() + 5
        for name in list(roots()):
            base = base_of(name)
            for path, st in walk_files(base):
                item = (st.st_mtime, next(counter), name, path, st.st_size)
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item[0] > heap[0][0]:
                    heapq.heapreplace(heap, item)
                if time.monotonic() > deadline:
                    break
        writable_roots = set(ctx.config.writable_roots)
        out = []
        for mtime, _, name, path, size in sorted(heap, reverse=True):
            out.append({"root": name, "path": rel_of(name, path), "name": path.name, "size": size,
                        "modified": mtime, "writable": name in writable_roots})
        return {"files": out}

    @router.get("/search")
    def search(q: str = ""):
        results = []
        for name in list(roots()):
            if len(results) >= 20:
                break
            try:
                found = files.search(roots(), name, q)
            except files.FileError:
                continue
            for hit in found:
                results.append({"title": hit["name"], "snippet": f"{name}/{hit['path']}", "kind": "file",
                                "open": {"view": "files", "id": f"{name}:{hit['path']}"}})
                if len(results) >= 20:
                    break
        return {"results": results}

    @router.get("/stats")
    def stats(root: str | None = None):
        names = [root] if root else list(roots())
        out = []
        with _guard_os():
            for name in names:
                base = base_of(name)
                n_files, n_dirs, total, truncated = tree_stats(base) if base.is_dir() else (0, 0, 0, False)
                trash_bytes = 0
                if trash_dir(name).is_dir():
                    trash_bytes = tree_stats(trash_dir(name))[2]
                try:
                    disk = shutil.disk_usage(base if base.exists() else base.parent)
                    disk_info = {"total": disk.total, "used": disk.used, "free": disk.free}
                except OSError:
                    disk_info = None
                out.append({"name": name, "writable": name in ctx.config.writable_roots, "files": n_files,
                            "folders": n_dirs, "bytes": total, "truncated": truncated,
                            "trash_bytes": trash_bytes, "disk": disk_info})
        return {"roots": out}

    return router


def _place_new(source: Path, folder: Path, name: str) -> Path:
    """Put the finished upload `source` into `folder` under `name`, or `name (2)`... if taken. Never overwrites."""
    for n in itertools.count(1):
        stem, ext = os.path.splitext(name)
        candidate = folder / (name if n == 1 else f"{stem} ({n}){ext}")
        try:
            os.link(source, candidate)         # fails if the name is taken: nothing is ever overwritten
            return candidate
        except FileExistsError:
            continue
        except OSError:                        # other disk, or a file system without hard links: copy instead
            if os.path.lexists(candidate):
                continue
            part = folder / f".hub-{secrets.token_hex(6)}.tmp"
            try:
                shutil.copyfile(source, part)
                if os.path.lexists(candidate):
                    continue
                os.replace(part, candidate)
                return candidate
            finally:
                with contextlib.suppress(OSError):
                    part.unlink()
    raise AssertionError  # pragma: no cover

