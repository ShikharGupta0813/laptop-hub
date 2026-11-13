"""ASANA POLLER for urgent-alert triage (optional, off by default: config `asana_enabled`).

Every `asana_poll_minutes` it asks Asana's official REST API (https://app.asana.com/api/1.0) for the tasks assigned to the
owner and for new comments, and hands each NEW thing to `alerts.triage` as an event with source "asana". Rules in
hub/alerts.py decide whether the task name or the comment sounds urgent. Read-only: this module never writes to Asana.

The Personal Access Token is kept in the OS keyring (service `redline-asana`, like the Gemini key lives in the keyring),
NEVER in hub.json, never in a log line, never in a response. If no keyring token is saved, the token the Work tab already
uses (ASANA_TOKEN or ~/.claude/.asana_token, see hub/work/asana.py) is reused read-only.

Calls (simple on purpose). UNVERIFIED against a live Asana account: this was built against the public docs and a fake HTTP
layer in tests; the first real poll is the real test.
  GET /users/me                                  -> {"data": {"gid", "name", "workspaces": [{"gid","name"}]}}
  GET /tasks?assignee=me&workspace=<gid>&completed_since=now&opt_fields=...   (open tasks assigned to me; documented trick:
                                                  completed_since=now returns only incomplete tasks)
  GET /tasks/{gid}/stories?opt_fields=...        -> comments are the stories whose resource_subtype is "comment_added"

First poll only records a baseline (so a long backlog does not flood the phone). After that: a task not seen before, and a
comment newer than the last one seen (not written by the owner), become events.

Routes (login needed, prefix /api/asana): GET /status ; POST /connect {token, workspace?} ; POST /disconnect ;
POST /workspace {gid} ; POST /enable {enabled, poll_minutes?} ; POST /test ; POST /poll-now.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from .common import Ctx, JsonFile
from .work.asana import API_BASE, find_token, urllib_transport

log = logging.getLogger("hub.asana")

KEYRING_SERVICE = "redline-asana"
KEYRING_NAME = "pat"
TASK_FIELDS = "name,due_on,modified_at,created_at,permalink_url,notes,completed"
STORY_FIELDS = "created_at,created_by.name,created_by.gid,resource_subtype,text"
MAX_TASKS = 100
MAX_STORY_TASKS = 25
MIN_POLL, MAX_POLL = 1, 120
LOOP_TICK = 30.0
GID_RE = re.compile(r"^[0-9]{1,30}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9/:._-]{20,200}$")

# http(method, url, headers) -> (status, parsed json or {})
Http = Callable[[str, str, dict], "tuple[int, dict]"]


class AsanaProblem(Exception):
    """A sentence the UI can show. Never contains the token."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class KeyringStore:
    """The token store. Tests pass a dict-backed fake with the same three methods."""

    def get(self) -> str:
        try:
            import keyring
            return (keyring.get_password(KEYRING_SERVICE, KEYRING_NAME) or "").strip()
        except Exception:  # noqa: BLE001 - no keyring installed or no backend
            return ""

    def set(self, token: str) -> None:
        try:
            import keyring
            keyring.set_password(KEYRING_SERVICE, KEYRING_NAME, token)
        except Exception:  # noqa: BLE001
            raise AsanaProblem("Secure storage (the OS keyring) is not available, so the token was not saved. "
                               "Install the keyring package, or set ASANA_TOKEN on the laptop.") from None

    def delete(self) -> None:
        try:
            import keyring
            keyring.delete_password(KEYRING_SERVICE, KEYRING_NAME)
        except Exception:  # noqa: BLE001 - nothing saved is fine
            pass


def default_http(method: str, url: str, headers: dict) -> tuple[int, dict]:
    status, _h, raw = urllib_transport(method, url, headers, None)      # never follows a redirect with the token attached
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


class Poller:
    def __init__(self, ctx: Ctx, *, http: Http | None = None, store: Any = None, triage: Callable | None = None):
        self.ctx = ctx
        self.http = http or default_http
        self.store = store or KeyringStore()
        self._triage = triage
        self.state_file = JsonFile(ctx.config.data_path() / "asana_state.json")
        self.last_error = ""
        self.last_poll = ""

    # ---- config ----
    @property
    def enabled(self) -> bool:
        return getattr(self.ctx.config, "asana_enabled", False) is True

    def minutes(self) -> int:
        v = getattr(self.ctx.config, "asana_poll_minutes", 5)
        return v if isinstance(v, int) and not isinstance(v, bool) and MIN_POLL <= v <= MAX_POLL else 5

    def token(self) -> str:
        return self.store.get() or (find_token() or "")

    # ---- the API ----
    def call(self, path: str, params: dict | None = None) -> dict:
        token = self.token()
        if not token:
            raise AsanaProblem("No Asana token is saved. Connect Asana first.")
        url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            status, data = self.http("GET", url, {"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except (OSError, TimeoutError) as exc:
            raise AsanaProblem(f"Can't reach Asana ({type(exc).__name__}).") from None
        if status == 401:
            raise AsanaProblem("Asana rejected the token. Connect again with a new personal access token.", 401)
        if status == 403:
            raise AsanaProblem("Asana says this token has no access to that.", 403)
        if status == 429:
            raise AsanaProblem("Asana is rate-limiting. The next poll will try again.", 429)
        if not 200 <= status < 300:
            raise AsanaProblem(f"Asana answered {status}.", status)
        return data

    def me(self) -> dict:
        d = self.call("/users/me", {"opt_fields": "name,workspaces.name"}).get("data")
        if not isinstance(d, dict) or not d.get("gid"):
            raise AsanaProblem("Asana's answer could not be read.")
        return d

    def workspace(self, me: dict | None = None) -> str:
        chosen = str(getattr(self.ctx.config, "asana_workspace", "") or "")
        if chosen:
            return chosen
        spaces = (me or self.me()).get("workspaces") or []
        return str(spaces[0].get("gid")) if spaces and isinstance(spaces[0], dict) else ""

    # ---- connect / disconnect / test ----
    def connect(self, token: str, workspace: str = "") -> dict:
        from .alerts import persist_config
        token = str(token or "").strip()
        if not TOKEN_RE.match(token):
            raise AsanaProblem("That does not look like an Asana personal access token.")
        if workspace and not GID_RE.match(workspace):
            raise AsanaProblem("The workspace id is a number.")
        self.store.set(token)
        try:
            me = self.me()
        except AsanaProblem:
            self.store.delete()                      # a token Asana refuses is not kept
            raise
        persist_config(self.ctx, asana_enabled=True, **({"asana_workspace": workspace} if workspace else {}))
        self.ctx.audit("asana_connect", ok=True)    # no token in the audit log
        return self.status(me)

    def disconnect(self) -> dict:
        from .alerts import persist_config
        self.store.delete()
        persist_config(self.ctx, asana_enabled=False, asana_workspace="")
        self.state_file.save({})
        self.ctx.audit("asana_disconnect")
        return self.status()

    def status(self, me: dict | None = None) -> dict:
        connected = bool(self.token())
        out = {"enabled": self.enabled, "connected": connected, "poll_minutes": self.minutes(),
               "workspace": str(getattr(self.ctx.config, "asana_workspace", "") or ""), "last_poll": self.last_poll or None,
               "last_error": self.last_error or None, "token_source": "keyring" if self.store.get() else ("work-tab file/env" if connected else "none")}
        if me:
            out["name"] = me.get("name")
            out["workspaces"] = [{"gid": str(w.get("gid")), "name": str(w.get("name") or "")} for w in me.get("workspaces") or []
                                 if isinstance(w, dict)]
        return out

    def test(self) -> dict:
        """One harmless call: who am I, and which workspaces. Used by the Test button."""
        try:
            me = self.me()
            self.last_error = ""
            return {"ok": True, **self.status(me)}
        except AsanaProblem as exc:
            self.last_error = str(exc)
            raise

    def set_workspace(self, gid: str) -> dict:
        from .alerts import persist_config
        if not GID_RE.match(str(gid or "")):
            raise AsanaProblem("The workspace id is a number.")
        persist_config(self.ctx, asana_workspace=str(gid))
        self.state_file.save({})                      # a different workspace starts with a fresh baseline
        return self.status()

    def set_enabled(self, enabled: bool, minutes: Any = None) -> dict:
        from .alerts import persist_config
        changes: dict[str, Any] = {"asana_enabled": bool(enabled)}
        if minutes is not None:
            if not isinstance(minutes, int) or isinstance(minutes, bool) or not MIN_POLL <= minutes <= MAX_POLL:
                raise AsanaProblem(f"Poll every {MIN_POLL} to {MAX_POLL} minutes.")
            changes["asana_poll_minutes"] = minutes
        persist_config(self.ctx, **changes)
        return self.status()

    # ---- polling ----
    def triage(self, event: dict):
        if self._triage is not None:
            return self._triage(self.ctx, event)
        from . import alerts
        return alerts.triage(self.ctx, event)

    def poll_once(self) -> dict:
        """One round. Returns {"baseline": bool, "events": n, "alerts": n}. Raises AsanaProblem (message is safe to show)."""
        me = self.me()
        my_gid = str(me.get("gid"))
        ws = self.workspace(me)
        if not ws:
            raise AsanaProblem("Asana lists no workspace for this token.")
        data = self.call("/tasks", {"assignee": "me", "workspace": ws, "completed_since": "now", "opt_fields": TASK_FIELDS,
                                    "limit": str(MAX_TASKS)})
        tasks = [t for t in data.get("data") or [] if isinstance(t, dict) and GID_RE.match(str(t.get("gid") or ""))
                 and not t.get("completed")]
        state = self.state_file.load({})
        state = state if isinstance(state, dict) else {}
        known: dict = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
        baseline = not state.get("baseline_done")
        events: list[dict] = []
        new_known: dict = {}
        story_checks = 0
        for t in tasks[:MAX_TASKS]:
            gid, mod = str(t["gid"]), str(t.get("modified_at") or "")
            entry = known.get(gid) if isinstance(known.get(gid), dict) else None
            new_known[gid] = {"mod": mod, "story": (entry or {}).get("story", "")}
            if entry is None and not baseline:
                events.append({"source": "asana", "app": "Asana", "sender": "Asana", "title": str(t.get("name") or "")[:200],
                               "text": str(t.get("notes") or "")[:300], "due_on": str(t.get("due_on") or ""),
                               "link": str(t.get("permalink_url") or ""), "gid": gid, "kind_hint": "task"})
            if (entry is None or entry.get("mod") != mod) and story_checks < MAX_STORY_TASKS:
                story_checks += 1
                last = (entry or {}).get("story", "")
                newest = last
                try:
                    stories = self.call(f"/tasks/{gid}/stories", {"opt_fields": STORY_FIELDS}).get("data") or []
                except AsanaProblem as exc:
                    if exc.status in (401, 429):
                        raise
                    stories = []
                for st in stories:
                    if not isinstance(st, dict) or st.get("resource_subtype") != "comment_added":
                        continue
                    at = str(st.get("created_at") or "")
                    newest = max(newest, at)
                    by = st.get("created_by") if isinstance(st.get("created_by"), dict) else {}
                    if at > last and not baseline and str(by.get("gid") or "") != my_gid and entry is not None:
                        events.append({"source": "asana", "app": "Asana", "sender": str(by.get("name") or "Asana")[:80],
                                       "title": str(t.get("name") or "")[:200], "text": str(st.get("text") or "")[:400],
                                       "due_on": str(t.get("due_on") or ""), "link": str(t.get("permalink_url") or ""), "gid": gid})
                new_known[gid]["story"] = newest
        self.state_file.save({"baseline_done": True, "tasks": new_known})
        raised = 0
        for ev in events:
            try:
                raised += 1 if self.triage(ev) else 0
            except Exception:  # noqa: BLE001 - one bad event must not lose the rest
                log.info("asana event could not be triaged")
        self.last_poll = self._now()
        self.last_error = ""
        return {"baseline": baseline, "events": len(events), "alerts": raised}

    def _now(self) -> str:
        import datetime as dt
        return dt.datetime.now().isoformat(timespec="seconds")

    def safe_poll(self) -> dict | None:
        try:
            return self.poll_once()
        except AsanaProblem as exc:
            self.last_error = str(exc)
            log.info("asana poll problem: %s", exc.status or "error")
        except Exception:  # noqa: BLE001
            self.last_error = "The Asana poll failed unexpectedly."
            log.info("asana poll failed")
        return None

    async def loop(self) -> None:
        waited = self.minutes() * 60.0            # first poll after one interval, not at boot
        while True:
            await asyncio.sleep(LOOP_TICK)
            waited -= LOOP_TICK
            if waited > 0:
                continue
            waited = self.minutes() * 60.0
            if self.enabled and self.token():
                await asyncio.to_thread(self.safe_poll)


def make_router(ctx: Ctx, *, poller: Poller | None = None) -> APIRouter:
    p = poller or Poller(ctx)
    ctx.asana = p                      # type: ignore[attr-defined]
    router = APIRouter(prefix="/api/asana", dependencies=[Depends(ctx.need_auth)])
    router.poller = p                  # type: ignore[attr-defined]

    def run(fn, *args):
        try:
            return fn(*args)
        except AsanaProblem as exc:
            # Never pass Asana's 401 on as 401: the app would think the hub login expired.
            raise HTTPException(502 if exc.status in (401, 403) else 400, str(exc)) from None

    @router.get("/status")
    def status():
        return p.status()

    @router.post("/connect")
    async def connect(body: Any = Body(default=None)):
        if not isinstance(body, dict):
            raise HTTPException(400, "Send {\"token\": \"...\"}.")
        return await asyncio.to_thread(run, p.connect, body.get("token"), str(body.get("workspace") or ""))

    @router.post("/disconnect")
    def disconnect():
        return run(p.disconnect)

    @router.post("/workspace")
    def workspace(body: Any = Body(default=None)):
        return run(p.set_workspace, body.get("gid") if isinstance(body, dict) else "")

    @router.post("/enable")
    def enable(body: Any = Body(default=None)):
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise HTTPException(400, "Send {\"enabled\": true or false}.")
        return run(p.set_enabled, body["enabled"], body.get("poll_minutes"))

    @router.post("/test")
    async def test():
        return await asyncio.to_thread(run, p.test)

    @router.post("/poll-now")
    async def poll_now():
        if not p.token():
            raise HTTPException(409, "Connect Asana first.")
        return await asyncio.to_thread(run, p.poll_once)

    return router


async def start(ctx: Ctx):
    p = getattr(ctx, "asana", None)
    if p is None:
        return None
    task = asyncio.get_running_loop().create_task(p.loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return stop
