"""Workspaces: one-tap launchers for "get my laptop ready, then show it on the phone".

A workspace is a name and an ordered list of typed steps (max 12): run one of the owner's quick buttons
(`scripts` in hub.json), open a web address, start an installed program, open a folder or file inside the shared
roots, or wait. Launching runs the steps in order through the hub's existing routes (`/api/run/{name}` and
`/api/laptop/run`), always with `confirm: true` after the phone's confirm tap. Nothing here builds a shell string.
Programs can only be started from a Start Menu discovery (Windows) or the owner's `apps` in hub.json: a step holds
an id that is looked up in that list at launch, never a path. It never turns remote control on: the phone asks the
owner for that through `/api/screen/arm`.

The built-in "Whole laptop" tile has no steps: it only means "show me the laptop and let me control it".

Stored in `<data dir>/workspaces.json` as {"workspaces": [...], "recents": {...}} (an older plain list is read too).
Routes: see docs/api/workspaces.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import files as files_mod
from .common import Ctx, InProcessClient, JsonFile

Caller = Callable[..., Awaitable[tuple[bool, Any]]]

MAX_WORKSPACES = 30
MAX_STEPS = 12
MAX_WAIT = 120
MAX_TOTAL_WAIT = 300
MAX_APPS = 300
MAX_RECENTS = 100
WHOLE_LAPTOP_ID = "whole-laptop"
NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
ICON_RE = re.compile(r"^[a-z0-9-]{1,30}$")
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
APP_ID_RE = re.compile(r"^(?:[0-9a-f]{12}|cfg:[^\x00-\x1f]{1,60})$")
STEP_TYPES = ("script", "url", "app", "folder", "file", "wait")
# What each step type may carry. Anything else is refused, so a step can never smuggle in a path or command.
STEP_FIELDS = {"script": {"name"}, "url": {"url"}, "app": {"id", "name"}, "folder": {"root", "path"},
               "file": {"root", "path"}, "wait": {"seconds"}}
# Same idea as laptop.py: documents and folders only. Programs start from the app list, never from a file step.
PROGRAM_EXTENSIONS = {".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
                      ".hta", ".lnk", ".msi", ".msc", ".reg", ".cpl", ".scr", ".pif", ".jar", ".dll", ".sh", ".appx"}
SHORTCUT_EXTENSIONS = {".lnk", ".appref-ms"}
# Start Menu entries that are not programs people want to launch.
JUNK_RE = re.compile(r"uninstall|\bunins\d*\b|^remove |readme|read me|release notes|licen[sc]e|web ?site|documentation|"
                     r"user (guide|manual)|changelog|what'?s new|^help$|\bhelp (file|center)|report a (bug|problem)", re.I)
SCAN_FILE_CAP = 4000
SCAN_DEPTH = 5

TEMPLATES = [{
    "key": "nifi", "name": "NiFi (company VPN)", "icon": "globe",
    "description": "Connects the Cloudflare VPN on the laptop, then opens NiFi in its browser.",
    "url": "https://nifi.onestack.in", "pre_steps": [{"type": "script", "name": "warp_connect"}],
    "wait_seconds": 8, "open_in": "default_browser", "arm_minutes": 5,
    "steps": [{"type": "script", "name": "warp_connect"}, {"type": "wait", "seconds": 8},
              {"type": "url", "url": "https://nifi.onestack.in"}],
}]


class AppSystem:
    """The operating-system part of app launching. Tests replace this whole object."""

    platform = sys.platform

    def start_menu_dirs(self) -> list[Path]:
        """The two Start Menu Programs folders (all users first), or [] away from Windows."""
        if self.platform != "win32":
            return []
        out = []
        for var, rest in (("ProgramData", "Microsoft/Windows/Start Menu/Programs"),
                          ("APPDATA", "Microsoft/Windows/Start Menu/Programs")):
            base = os.environ.get(var)
            if base:
                out.append(Path(base) / rest)
        return out

    def start(self, path: str) -> None:
        """Opens a Start Menu shortcut the way double-clicking it would (never through a shell)."""
        os.startfile(path)  # type: ignore[attr-defined]


def clean_url(raw: Any) -> str:
    """The address if it is a plain http(s) one with a host; otherwise a 400 with a plain sentence."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(400, "That is not a web address.")
    url = raw.strip()
    if not url:
        return ""
    if len(url) > 2000 or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise HTTPException(400, "That is not a web address (it is too long, or has spaces or hidden characters).")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise HTTPException(400, "That is not a valid web address.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise HTTPException(400, "Only web addresses starting with http:// or https:// can be opened.")
    if not host:
        raise HTTPException(400, "That web address has no site name in it.")
    return url


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = "script"
    name: str | None = Field(default=None, max_length=60)
    id: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=2000)
    root: str | None = Field(default=None, max_length=60)
    path: str | None = Field(default=None, max_length=1000)
    seconds: int | None = Field(default=None, ge=0, le=MAX_WAIT)


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=60)
    icon: str | None = Field(default=None, max_length=30)
    steps: list[Step] | None = Field(default=None, max_length=MAX_STEPS)
    # The older shape, still accepted: buttons + a wait + one address.
    url: str | None = Field(default=None, max_length=2000)
    pre_steps: list[Step] | None = Field(default=None, max_length=MAX_STEPS)
    wait_seconds: int | None = Field(default=None, ge=0, le=MAX_WAIT)
    open_in: str | None = None
    arm_minutes: int | None = Field(default=None, ge=1, le=15)


class LaunchBody(BaseModel):
    confirm: StrictBool = False
    skip_steps: StrictBool = False   # "Open anyway": do not run buttons or waits, only open things


class QuickBody(Step):
    confirm: StrictBool = False


def migrate_steps(record: dict) -> list[dict]:
    """The typed step list for a stored record, building it from the old pre_steps/url/wait_seconds if needed."""
    if isinstance(record.get("steps"), list):
        return [s for s in record["steps"] if isinstance(s, dict)]
    steps = [{"type": "script", "name": str(s.get("name", ""))} for s in record.get("pre_steps") or [] if isinstance(s, dict)]
    url = record.get("url") or ""
    wait = record.get("wait_seconds") or 0
    if steps and url and isinstance(wait, int) and wait > 0:
        steps.append({"type": "wait", "seconds": min(wait, MAX_WAIT)})
    if url:
        steps.append({"type": "url", "url": url})
    return steps


def legacy_view(steps: list[dict]) -> dict:
    """pre_steps / url / wait_seconds derived from the typed steps, for older phones and the search box."""
    wait = next((s.get("seconds", 0) for s in steps if s.get("type") == "wait"), 0)
    return {"pre_steps": [dict(s) for s in steps if s.get("type") == "script"],
            "url": next((s.get("url", "") for s in steps if s.get("type") == "url"), ""), "wait_seconds": wait}


def app_id_for(path: str) -> str:
    """A stable id for a Start Menu shortcut: a hash of its normalised path."""
    return hashlib.sha1(os.path.normcase(path).encode("utf-8", "replace")).hexdigest()[:12]


def make_router(ctx: Ctx, *, caller: Caller | None = None,
                sleep: Callable[[float], Awaitable[None]] | None = None,
                system: AppSystem | None = None) -> APIRouter:
    """`caller`, `sleep` and `system` exist so tests need no real hub, no real waiting and no real Start Menu."""
    store = JsonFile(ctx.config.data_path() / "workspaces.json")
    system = system or AppSystem()
    state: dict[str, Any] = {"client": None}
    launching: set[str] = set()
    router = APIRouter(prefix="/api/workspaces", dependencies=[Depends(ctx.need_auth)])

    async def call(method: str, path: str, body: dict | None = None):
        if caller is not None:
            return await caller(method, path, body=body)
        if state["client"] is None:
            state["client"] = InProcessClient(ctx.get_app, ctx.config)
        return await state["client"].call(method, path, body=body)

    async def nap(seconds: float) -> None:
        await (sleep or asyncio.sleep)(seconds)

    # ---- storage ----------------------------------------------------------------------------
    def read() -> tuple[list[dict], dict]:
        data = store.load({})
        if isinstance(data, list):
            data = {"workspaces": data}
        if not isinstance(data, dict):
            data = {}
        items = data.get("workspaces")
        recents = data.get("recents")
        items = [w for w in items if isinstance(w, dict) and ID_RE.match(str(w.get("id", "")))] if isinstance(items, list) else []
        return items, recents if isinstance(recents, dict) else {}

    def write(items: list[dict] | None = None, recents: dict | None = None) -> None:
        old_items, old_recents = read()
        store.save({"workspaces": old_items if items is None else items, "recents": old_recents if recents is None else recents})

    def load() -> list[dict]:
        return read()[0]

    def whole_laptop() -> dict:
        return {"id": WHOLE_LAPTOP_ID, "name": "Whole laptop", "icon": "laptop", "builtin": True, "steps": [], "pre_steps": [],
                "url": "", "wait_seconds": 0, "open_in": "default_browser", "arm_minutes": 5, "created": 0,
                "description": "Just show the laptop screen and let me use it. Nothing is opened."}

    # ---- apps: Start Menu discovery plus the owner's `apps` ---------------------------------------
    def discover() -> dict[str, dict]:
        """id -> {id, name, source, path?}. Owner apps first, then Start Menu; one entry per name (case-insensitive)."""
        found: dict[str, dict] = {}
        seen: set[str] = set()
        for name, spec in (ctx.config.apps or {}).items():
            target = spec.get("target") if isinstance(spec, dict) else None
            if not isinstance(target, str) or not target.strip() or not str(name).strip() or len(str(name)) > 60:
                continue
            seen.add(str(name).casefold())
            found[f"cfg:{name}"] = {"id": f"cfg:{name}", "name": str(name), "source": "hub.json"}
        count = 0
        for base in system.start_menu_dirs():
            try:
                base = Path(base)
                if not base.is_dir():
                    continue
                walk = os.walk(base, followlinks=False)
                for folder, dirs, names in walk:
                    depth = len(Path(folder).relative_to(base).parts)
                    dirs[:] = sorted(d for d in dirs if not d.startswith(".") and depth < SCAN_DEPTH and not hidden(Path(folder) / d))
                    for filename in sorted(names):
                        count += 1
                        if count > SCAN_FILE_CAP:
                            break
                        path = Path(folder) / filename
                        stem, ext = os.path.splitext(filename)
                        if ext.lower() not in SHORTCUT_EXTENSIONS or filename.startswith(".") or not stem.strip():
                            continue
                        label = stem.strip()
                        if len(label) > 60 or JUNK_RE.search(label) or hidden(path) or path.is_symlink():
                            continue
                        if label.casefold() in seen:
                            continue
                        seen.add(label.casefold())
                        app_id = app_id_for(str(path))
                        found[app_id] = {"id": app_id, "name": label, "source": "start_menu", "path": str(path)}
                    if count > SCAN_FILE_CAP:
                        break
            except OSError:
                continue
        return found

    def hidden(path: Path) -> bool:
        try:
            attrs = getattr(path.stat(), "st_file_attributes", 0)
        except OSError:
            return True
        return bool(attrs & 2) or path.name.lower() == "desktop.ini"

    def listed_apps(q: str = "") -> list[dict]:
        needle = q.strip().casefold()
        rows = [a for a in discover().values() if not needle or needle in a["name"].casefold()]
        rows.sort(key=lambda a: (a["name"].casefold(), a["id"]))
        return rows[:MAX_APPS]

    def app_by_id(app_id: str) -> dict:
        """Only an id present in the current discovery resolves. Anything else is refused."""
        if not isinstance(app_id, str) or not APP_ID_RE.match(app_id):
            raise HTTPException(400, "That is not a known app id.")
        entry = discover().get(app_id)
        if entry is None:
            raise HTTPException(400, "That app was not found in the laptop's Start Menu or your app list. Refresh the app list and pick it again.")
        return entry

    # ---- steps ------------------------------------------------------------------------------
    def script_names() -> set[str]:
        return set((ctx.config.scripts or {}).keys())

    def script_label(name: str) -> str:
        spec = (ctx.config.scripts or {}).get(name)
        return str(spec.get("label") or name) if isinstance(spec, dict) else name

    def need_script(name: str) -> str:
        if not isinstance(name, str) or not NAME_RE.match(name) or name not in script_names():
            raise HTTPException(400, f"There is no quick button called '{str(name)[:40]}'. Add it on the laptop with "
                                     "`python -m hub add-script NAME --label \"Label\" -- PROGRAM ARGS`.")
        return name

    def resolve_in_root(kind: str, root: Any, rel: Any) -> tuple[Path, str]:
        if not isinstance(root, str) or not root:
            raise HTTPException(400, f"A {kind} step needs a shared folder to look in.")
        rel = rel if isinstance(rel, str) else ""
        if "\x00" in rel or len(rel) > 1000:
            raise HTTPException(400, "That path is not valid.")
        try:
            target = files_mod.resolve(ctx.config.roots, root, rel)
        except files_mod.FileError as exc:
            raise HTTPException(400 if exc.status == 404 else exc.status, exc.message) from None
        if not target.exists():
            raise HTTPException(400, f"That {kind} does not exist inside '{root}'.")
        if kind == "folder" and not target.is_dir():
            raise HTTPException(400, "That is a file, not a folder.")
        if kind == "file":
            if not target.is_file():
                raise HTTPException(400, "That is a folder, not a file.")
            if target.suffix.lower() in PROGRAM_EXTENSIONS:
                raise HTTPException(400, "A file step opens documents, not programs. Pick the program from the app list instead.")
        return target, rel.strip()

    def check_steps(steps: list[dict]) -> list[dict]:
        """Validate and normalise. Nothing from here is trusted again blindly: launch calls it again."""
        if len(steps) > MAX_STEPS:
            raise HTTPException(400, f"A workspace can have up to {MAX_STEPS} steps.")
        out: list[dict] = []
        total_wait = 0
        for raw in steps:
            kind = raw.get("type")
            if kind not in STEP_TYPES:
                raise HTTPException(400, "A step must be one of: quick button, web address, app, folder, file or wait.")
            extra = {k for k, v in raw.items() if v is not None and k != "type"} - STEP_FIELDS[kind]
            if extra:
                raise HTTPException(400, f"A {kind} step cannot have '{sorted(extra)[0]}'.")
            if kind == "script":
                out.append({"type": "script", "name": need_script(raw.get("name"))})
            elif kind == "url":
                url = clean_url(raw.get("url"))
                if not url:
                    raise HTTPException(400, "A web address step needs an address.")
                out.append({"type": "url", "url": url})
            elif kind == "app":
                entry = app_by_id(raw.get("id"))
                out.append({"type": "app", "id": entry["id"], "name": entry["name"]})
            elif kind in ("folder", "file"):
                _, rel = resolve_in_root(kind, raw.get("root"), raw.get("path"))
                out.append({"type": kind, "root": raw["root"], "path": rel})
            else:
                seconds = raw.get("seconds")
                if not isinstance(seconds, int) or isinstance(seconds, bool) or not 0 <= seconds <= MAX_WAIT:
                    raise HTTPException(400, f"A wait step needs 0 to {MAX_WAIT} seconds.")
                total_wait += seconds
                if total_wait > MAX_TOTAL_WAIT:
                    raise HTTPException(400, f"Waits can add up to at most {MAX_TOTAL_WAIT} seconds in one workspace.")
                out.append({"type": "wait", "seconds": seconds})
        return out

    def step_label(step: dict) -> str:
        kind = step["type"]
        if kind == "script":
            return script_label(step["name"])
        if kind == "url":
            return host_of(step["url"]) or "web address"
        if kind == "app":
            return str(step.get("name") or "app")
        if kind in ("folder", "file"):
            return Path(step.get("path") or "").name or step["root"]
        return f"wait {step['seconds']} s"

    def from_legacy(body: WorkspaceBody, current: dict | None) -> list[dict]:
        base = migrate_steps(current) if current else []
        if current and any(s.get("type") not in ("script", "url", "wait") for s in base):
            raise HTTPException(400, "This workspace uses newer steps. Edit its 'steps' instead of the older fields.")
        old = legacy_view(base)
        given = body.model_fields_set
        scripts = [s.model_dump(exclude_none=True) for s in body.pre_steps] if "pre_steps" in given and body.pre_steps is not None else old["pre_steps"]
        if any(x.get("type") != "script" or set(x) - {"type", "name"} for x in scripts):
            raise HTTPException(400, "Only quick buttons ('script' with a name) can go in pre_steps. Use 'steps' for other kinds.")
        url = (body.url or "") if "url" in given else old["url"]
        wait = body.wait_seconds if "wait_seconds" in given and body.wait_seconds is not None else old["wait_seconds"]
        return migrate_steps({"pre_steps": scripts, "url": url.strip() if isinstance(url, str) else url, "wait_seconds": wait})

    def build(existing: dict | None, body: WorkspaceBody) -> dict:
        given = body.model_fields_set
        record = dict(existing) if existing else {
            "id": uuid.uuid4().hex[:12], "name": "", "icon": "globe", "steps": [], "open_in": "default_browser",
            "arm_minutes": 5, "created": time.time()}
        if existing:
            record["steps"] = migrate_steps(existing)
            for legacy in ("url", "pre_steps", "wait_seconds"):
                record.pop(legacy, None)
        if "name" in given or not existing:
            name = (body.name or "").strip()
            if not name:
                raise HTTPException(400, "Give the workspace a name.")
            record["name"] = name
        if "icon" in given and body.icon is not None:
            if not ICON_RE.match(body.icon):
                raise HTTPException(400, "That icon name is not valid.")
            record["icon"] = body.icon
        if "steps" in given and body.steps is not None:
            if given & {"url", "pre_steps", "wait_seconds"}:
                raise HTTPException(400, "Send either 'steps' or the older url/pre_steps/wait_seconds, not both.")
            record["steps"] = check_steps([s.model_dump(exclude_none=True) for s in body.steps])
        elif given & {"url", "pre_steps", "wait_seconds"}:
            record["steps"] = check_steps(from_legacy(body, existing))
        if "open_in" in given and body.open_in is not None:
            if body.open_in != "default_browser":
                raise HTTPException(400, "Workspaces can only open in the laptop's default browser.")
            record["open_in"] = body.open_in
        if "arm_minutes" in given and body.arm_minutes is not None:
            record["arm_minutes"] = body.arm_minutes
        if not record["steps"]:
            raise HTTPException(400, "A workspace needs at least one step (a web address, an app, a folder or a quick button).")
        return record

    def public(record: dict) -> dict:
        steps = migrate_steps(record)
        out = {k: v for k, v in record.items() if k not in ("pre_steps", "url", "wait_seconds")}
        decorated = []
        for step in steps:
            item = dict(step)
            if item.get("type") == "app" and isinstance(item.get("id"), str):
                item["name"] = item.get("name") or "app"
            decorated.append(item)
        return {**out, "steps": decorated, **legacy_view(steps), "launching": record["id"] in launching}

    def find(items: list[dict], workspace_id: str) -> dict:
        if workspace_id == WHOLE_LAPTOP_ID:
            return whole_laptop()
        for item in items:
            if item["id"] == workspace_id:
                return item
        raise HTTPException(404, "That workspace was not found.")

    # ---- recents ----------------------------------------------------------------------------
    def bump_recent(entry: dict) -> None:
        _, recents = read()
        row = recents.get(entry["id"]) if isinstance(recents.get(entry["id"]), dict) else {}
        recents[entry["id"]] = {"name": entry["name"], "count": int(row.get("count", 0)) + 1, "last": time.time()}
        if len(recents) > MAX_RECENTS:
            keep = sorted(recents.items(), key=lambda kv: (kv[1].get("count", 0), kv[1].get("last", 0)), reverse=True)[:MAX_RECENTS]
            recents = dict(keep)
        write(recents=recents)

    # ---- running one step -------------------------------------------------------------------
    async def run_step(step: dict) -> tuple[bool, str]:
        kind = step["type"]
        if kind == "script":
            ok, data = await call("POST", f"/api/run/{step['name']}")
            if not ok:
                return False, str(data)
            exit_code = data.get("exit") if isinstance(data, dict) else None
            if exit_code == 0:
                return True, "Done."
            tail = str((data or {}).get("output", "")).strip().splitlines()[-1:] if isinstance(data, dict) else []
            return False, f"It finished with exit code {exit_code}." + (f" {tail[0][:200]}" if tail else "")
        if kind == "wait":
            await nap(float(step["seconds"]))
            return True, f"Waited {step['seconds']} seconds."
        if kind == "url":
            ok, data = await call("POST", "/api/laptop/run", {"action": "open_url", "params": {"url": clean_url(step["url"])}, "confirm": True})
            return bool(ok), "Opened on the laptop." if ok else str(data)
        if kind in ("folder", "file"):
            target, _ = resolve_in_root(kind, step.get("root"), step.get("path"))
            ok, data = await call("POST", "/api/laptop/run", {"action": "open_path", "params": {"path": str(target)}, "confirm": True})
            return bool(ok), "Opened on the laptop." if ok else str(data)
        entry = app_by_id(step.get("id"))
        if entry["source"] == "hub.json":
            ok, data = await call("POST", "/api/laptop/run", {"action": "open_app", "params": {"name": entry["name"]}, "confirm": True})
            if ok:
                bump_recent(entry)
            return bool(ok), f"Opened {entry['name']}." if ok else str(data)
        try:
            await asyncio.to_thread(system.start, entry["path"])
        except FileNotFoundError:
            return False, f"{entry['name']} was not found on the laptop any more."
        except OSError:
            return False, f"{entry['name']} could not be started."
        bump_recent(entry)
        return True, f"Opened {entry['name']}."

    def audit_step(step: dict) -> dict:
        """Names and a URL host only. Never a URL path, a file path or a shortcut path."""
        kind = step["type"]
        if kind == "url":
            return {"kind": kind, "host": host_of(step["url"])}
        if kind == "app":
            return {"kind": kind, "name": str(step.get("name", ""))[:60]}
        if kind in ("folder", "file"):
            return {"kind": kind, "root": step["root"]}
        if kind == "script":
            return {"kind": kind, "name": step["name"]}
        return {"kind": kind}

    async def execute(label: str, raw_steps: list[dict], skip: bool) -> dict:
        steps = check_steps(raw_steps)             # re-check everything now: hub.json or the Start Menu may have changed
        todo = [s for s in steps if not (skip and s["type"] in ("script", "wait"))]
        hosts = [host_of(s["url"]) for s in steps if s["type"] == "url"]
        ctx.audit("workspace_launch", name=label, host=hosts[0] if hosts else "", steps=len(todo))
        results: list[dict] = []
        stopped = False
        opened = False
        open_message = ""
        for step in todo:
            ctx.audit("workspace_step", workspace=label, **audit_step(step))
            try:
                ok, message = await run_step(step)
            except HTTPException as exc:
                ok, message = False, str(exc.detail)
            results.append({"type": step["type"], "name": step.get("name") or step_label(step), "label": step_label(step),
                            "ok": ok, "message": message})
            if step["type"] in ("url", "app", "folder", "file"):
                opened = opened or ok
                open_message = message
            if not ok:
                stopped = True
                break
        ok, status = await call("GET", "/api/screen/status")
        screen_ready = bool(ok and isinstance(status, dict) and status.get("available"))
        return {"ok": not stopped, "steps": results, "opened": opened, "open_message": open_message,
                "stopped": stopped, "screen_ready": screen_ready}

    # ---- routes (fixed paths first, then /{id}) ---------------------------------------------------
    @router.get("/templates")
    def templates():
        known = script_names()
        out = []
        for t in TEMPLATES:
            item = {**t, "pre_steps": [dict(s) for s in t["pre_steps"]], "steps": [dict(s) for s in t["steps"]]}
            item["missing_scripts"] = [s["name"] for s in t["pre_steps"] if s["name"] not in known]
            out.append(item)
        return {"templates": out}

    @router.get("/search")
    def search(q: str = Query("", max_length=200)):
        needle = q.strip().lower()
        results = []
        if needle:
            for item in [whole_laptop(), *load()]:
                host = legacy_view(migrate_steps(item))["url"]
                if needle in f"{item['name']} {host_of(host)}".lower():
                    results.append({"title": item["name"], "snippet": host_of(host) or "Workspace",
                                    "kind": "workspace", "open": {"view": "screen", "section": "workspaces", "id": item["id"]}})
                if len(results) >= 20:
                    break
        return {"results": results}

    @router.get("/apps")
    def apps(q: str = Query("", max_length=100)):
        """Programs the launcher may start: Start Menu shortcuts (Windows) plus `apps` in hub.json."""
        _, recents = read()
        rows = listed_apps(q)
        return {"apps": [{"id": a["id"], "name": a["name"], "source": a["source"],
                          "count": int((recents.get(a["id"]) or {}).get("count", 0))} for a in rows],
                "supported": bool(system.start_menu_dirs()) or bool(ctx.config.apps)}

    @router.get("/recents")
    def recents_list(limit: int = Query(12, ge=1, le=50)):
        """The most used apps first (only ones that still exist)."""
        _, recents = read()
        known = discover()
        rows = [{"id": k, "name": known[k]["name"], "source": known[k]["source"], "count": int(v.get("count", 0)), "last": v.get("last", 0)}
                for k, v in recents.items() if k in known and isinstance(v, dict)]
        rows.sort(key=lambda r: (-r["count"], -float(r["last"] or 0), r["name"].casefold()))
        return {"recents": rows[:limit]}

    @router.post("/quick")
    async def quick(body: QuickBody):
        """Launch one thing right now (an app, a link, a folder or a file) without saving a workspace."""
        if body.confirm is not True:
            raise HTTPException(409, "This opens something on your laptop. Confirm it and send again.")
        if body.type not in ("app", "url", "folder", "file"):
            raise HTTPException(400, "Quick launch can open an app, a web address, a folder or a file.")
        raw = body.model_dump(exclude_none=True, exclude={"confirm"})
        step = check_steps([raw])[0]
        key = "quick:" + step["type"]
        if key in launching:
            raise HTTPException(409, "Something is already opening. Wait for it to finish.")
        launching.add(key)
        try:
            result = await execute(step_label(step), [step], False)
        finally:
            launching.discard(key)
        return {**result, "arm_minutes": 5}

    @router.get("")
    def list_workspaces():
        return {"workspaces": [public(whole_laptop()), *[public(w) for w in load()]]}

    @router.post("")
    def create(body: WorkspaceBody):
        items = load()
        if len(items) >= MAX_WORKSPACES:
            raise HTTPException(400, f"You can keep up to {MAX_WORKSPACES} workspaces. Delete one first.")
        record = build(None, body)
        items.append(record)
        write(items)
        view = legacy_view(record["steps"])
        ctx.audit("workspace_create", name=record["name"], host=host_of(view["url"]), steps=len(record["steps"]))
        return public(record)

    @router.get("/{workspace_id}")
    def get_one(workspace_id: str):
        return public(find(load(), workspace_id))

    @router.put("/{workspace_id}")
    def update(workspace_id: str, body: WorkspaceBody):
        if workspace_id == WHOLE_LAPTOP_ID:
            raise HTTPException(400, "The Whole laptop tile is built in and cannot be changed.")
        items = load()
        current = find(items, workspace_id)
        if workspace_id in launching:
            raise HTTPException(409, "This workspace is opening right now. Try again in a moment.")
        new = build(current, body)
        items[items.index(current)] = new
        write(items)
        ctx.audit("workspace_update", name=new["name"], host=host_of(legacy_view(new["steps"])["url"]), steps=len(new["steps"]))
        return public(new)

    @router.delete("/{workspace_id}")
    def delete(workspace_id: str):
        if workspace_id == WHOLE_LAPTOP_ID:
            raise HTTPException(400, "The Whole laptop tile is built in and cannot be deleted.")
        items = load()
        current = find(items, workspace_id)
        if workspace_id in launching:
            raise HTTPException(409, "This workspace is opening right now. Try again in a moment.")
        write([w for w in items if w is not current])
        ctx.audit("workspace_delete", name=current["name"])
        return {"ok": True}

    @router.post("/{workspace_id}/launch")
    async def launch(workspace_id: str, body: LaunchBody):
        if body.confirm is not True:
            raise HTTPException(409, "Opening a workspace runs things on your laptop. Confirm it and send again.")
        record = find(load(), workspace_id)
        if workspace_id in launching:
            raise HTTPException(409, "This workspace is already opening. Wait for it to finish.")
        check_steps(migrate_steps(record))          # refuse bad data before anything runs or the lock is taken
        launching.add(workspace_id)
        try:
            result = await execute(record["name"], migrate_steps(record), body.skip_steps)
            return {**result, "arm_minutes": record.get("arm_minutes", 5), "workspace": public(record)}
        finally:
            launching.discard(workspace_id)

    return router
