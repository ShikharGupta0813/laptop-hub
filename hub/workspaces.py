"""Workspaces: a one-tap launcher for "get my laptop ready, then show it on the phone".

A workspace is a name, an optional web address, and a short list of the owner's own quick buttons
(`scripts` in hub.json) to run first, for example one that connects the company VPN. Launching it runs those
buttons in order through the hub's existing `/api/run/{name}` route, waits, then opens the address through the
existing laptop action `open_url`. Nothing here builds a shell string or starts a program itself, and it never
turns remote control on: the phone asks the owner for that through `/api/screen/arm`.

Stored in `<data dir>/workspaces.json`. Routes: see docs/api/workspaces.md.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .common import Ctx, InProcessClient, JsonFile

Caller = Callable[..., Awaitable[tuple[bool, Any]]]

MAX_WORKSPACES = 30
MAX_STEPS = 5
MAX_WAIT = 120
NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
ICON_RE = re.compile(r"^[a-z0-9-]{1,30}$")
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")

TEMPLATES = [{
    "key": "nifi", "name": "NiFi (company VPN)", "icon": "globe",
    "description": "Connects the Cloudflare VPN on the laptop, then opens NiFi in its browser.",
    "url": "https://nifi.onestack.in", "pre_steps": [{"type": "script", "name": "warp_connect"}],
    "wait_seconds": 8, "open_in": "default_browser", "arm_minutes": 5,
}]


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
    name: str = Field(min_length=1, max_length=40)


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=60)
    icon: str | None = Field(default=None, max_length=30)
    url: str | None = Field(default=None, max_length=2000)
    pre_steps: list[Step] | None = Field(default=None, max_length=MAX_STEPS)
    wait_seconds: int | None = Field(default=None, ge=0, le=MAX_WAIT)
    open_in: str | None = None
    arm_minutes: int | None = Field(default=None, ge=1, le=15)


class LaunchBody(BaseModel):
    confirm: StrictBool = False
    skip_steps: StrictBool = False   # "Open anyway": do not run the buttons, only open the address


def make_router(ctx: Ctx, *, caller: Caller | None = None,
                sleep: Callable[[float], Awaitable[None]] | None = None) -> APIRouter:
    """`caller` and `sleep` exist so tests need no real hub and no real waiting."""
    store = JsonFile(ctx.config.data_path() / "workspaces.json")
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

    def load() -> list[dict]:
        data = store.load([])
        return [w for w in data if isinstance(w, dict) and ID_RE.match(str(w.get("id", "")))] if isinstance(data, list) else []

    def script_names() -> set[str]:
        return set((ctx.config.scripts or {}).keys())

    def script_label(name: str) -> str:
        spec = (ctx.config.scripts or {}).get(name)
        return str(spec.get("label") or name) if isinstance(spec, dict) else name

    def check_steps(steps: list[dict]) -> list[dict]:
        known = script_names()
        out = []
        for step in steps:
            if step["type"] != "script":
                raise HTTPException(400, "Only quick buttons ('script') can be run before opening a workspace.")
            if not NAME_RE.match(step["name"]) or step["name"] not in known:
                raise HTTPException(400, f"There is no quick button called '{step['name']}'. Add it on the laptop with "
                                         "`python -m hub add-script NAME --label \"Label\" -- PROGRAM ARGS`.")
            out.append({"type": "script", "name": step["name"]})
        return out

    def build(existing: dict | None, body: WorkspaceBody) -> dict:
        given = body.model_fields_set
        record = dict(existing) if existing else {
            "id": uuid.uuid4().hex[:12], "name": "", "icon": "globe", "url": "", "pre_steps": [], "wait_seconds": 0,
            "open_in": "default_browser", "arm_minutes": 5, "created": time.time()}
        if "name" in given or not existing:
            name = (body.name or "").strip()
            if not name:
                raise HTTPException(400, "Give the workspace a name.")
            record["name"] = name
        if "icon" in given and body.icon is not None:
            if not ICON_RE.match(body.icon):
                raise HTTPException(400, "That icon name is not valid.")
            record["icon"] = body.icon
        if "url" in given:
            record["url"] = clean_url(body.url)
        if "pre_steps" in given and body.pre_steps is not None:
            record["pre_steps"] = check_steps([s.model_dump() for s in body.pre_steps])
        if "wait_seconds" in given and body.wait_seconds is not None:
            record["wait_seconds"] = body.wait_seconds
        if "open_in" in given and body.open_in is not None:
            if body.open_in != "default_browser":
                raise HTTPException(400, "Workspaces can only open in the laptop's default browser.")
            record["open_in"] = body.open_in
        if "arm_minutes" in given and body.arm_minutes is not None:
            record["arm_minutes"] = body.arm_minutes
        if not record["url"] and not record["pre_steps"]:
            raise HTTPException(400, "A workspace needs a web address or at least one quick button.")
        return record

    def public(record: dict) -> dict:
        return {**record, "launching": record["id"] in launching}

    def find(items: list[dict], workspace_id: str) -> dict:
        for item in items:
            if item["id"] == workspace_id:
                return item
        raise HTTPException(404, "That workspace was not found.")

    @router.get("/templates")
    def templates():
        known = script_names()
        return {"templates": [{**t, "pre_steps": [dict(s) for s in t["pre_steps"]],
                               "missing_scripts": [s["name"] for s in t["pre_steps"] if s["name"] not in known]}
                              for t in TEMPLATES]}

    @router.get("/search")
    def search(q: str = Query("", max_length=200)):
        needle = q.strip().lower()
        results = []
        if needle:
            for item in load():
                if needle in f"{item['name']} {host_of(item.get('url', ''))}".lower():
                    results.append({"title": item["name"], "snippet": host_of(item.get("url", "")) or "Workspace",
                                    "kind": "workspace", "open": {"view": "screen", "section": "workspaces", "id": item["id"]}})
                if len(results) >= 20:
                    break
        return {"results": results}

    @router.get("")
    def list_workspaces():
        return {"workspaces": [public(w) for w in load()]}

    @router.post("")
    def create(body: WorkspaceBody):
        items = load()
        if len(items) >= MAX_WORKSPACES:
            raise HTTPException(400, f"You can keep up to {MAX_WORKSPACES} workspaces. Delete one first.")
        record = build(None, body)
        items.append(record)
        store.save(items)
        ctx.audit("workspace_create", name=record["name"], host=host_of(record["url"]))
        return public(record)

    @router.get("/{workspace_id}")
    def get_one(workspace_id: str):
        return public(find(load(), workspace_id))

    @router.put("/{workspace_id}")
    def update(workspace_id: str, body: WorkspaceBody):
        items = load()
        current = find(items, workspace_id)
        if workspace_id in launching:
            raise HTTPException(409, "This workspace is opening right now. Try again in a moment.")
        new = build(current, body)
        items[items.index(current)] = new
        store.save(items)
        ctx.audit("workspace_update", name=new["name"], host=host_of(new["url"]))
        return public(new)

    @router.delete("/{workspace_id}")
    def delete(workspace_id: str):
        items = load()
        current = find(items, workspace_id)
        if workspace_id in launching:
            raise HTTPException(409, "This workspace is opening right now. Try again in a moment.")
        store.save([w for w in items if w is not current])
        ctx.audit("workspace_delete", name=current["name"])
        return {"ok": True}

    @router.post("/{workspace_id}/launch")
    async def launch(workspace_id: str, body: LaunchBody):
        if body.confirm is not True:
            raise HTTPException(409, "Opening a workspace runs things on your laptop. Confirm it and send again.")
        record = find(load(), workspace_id)
        if workspace_id in launching:
            raise HTTPException(409, "This workspace is already opening. Wait for it to finish.")
        # Re-check everything now: hub.json may have been edited since the workspace was saved.
        steps = [] if body.skip_steps else check_steps(record["pre_steps"])
        url = clean_url(record.get("url"))
        launching.add(workspace_id)
        try:
            ctx.audit("workspace_launch", name=record["name"], host=host_of(url), steps=len(steps))
            results: list[dict] = []
            stopped = False
            for step in steps:
                ok, data = await call("POST", f"/api/run/{step['name']}")
                if not ok:
                    results.append({"name": step["name"], "label": script_label(step["name"]), "ok": False, "message": str(data)})
                    stopped = True
                    break
                exit_code = data.get("exit") if isinstance(data, dict) else None
                good = exit_code == 0
                tail = str((data or {}).get("output", "")).strip().splitlines()[-1:] if isinstance(data, dict) else []
                message = "Done." if good else f"It finished with exit code {exit_code}." + (f" {tail[0][:200]}" if tail else "")
                results.append({"name": step["name"], "label": script_label(step["name"]), "ok": good, "message": message})
                if not good:
                    stopped = True
                    break
            opened = False
            open_message = ""
            if not stopped and url:
                if steps and record.get("wait_seconds"):
                    await nap(float(record["wait_seconds"]))
                ok, data = await call("POST", "/api/laptop/run", {"action": "open_url", "params": {"url": url}, "confirm": True})
                opened = bool(ok)
                open_message = "Opened on the laptop." if ok else str(data)
            ok, status = await call("GET", "/api/screen/status")
            screen_ready = bool(ok and isinstance(status, dict) and status.get("available"))
            return {"ok": opened or (not url and not stopped), "steps": results, "opened": opened, "open_message": open_message,
                    "stopped": stopped, "screen_ready": screen_ready, "arm_minutes": record.get("arm_minutes", 5),
                    "workspace": public(record)}
        finally:
            launching.discard(workspace_id)

    return router
