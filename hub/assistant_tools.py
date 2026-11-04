"""Assistant tools for the other features: Asana, calendar, email, chat, laptop actions, Jarvis,
English progress. Each tool calls the hub's own API inside the process, as the signed-in owner, so the
route's own checks, drafts and audit log all apply. Only the tools listed here can be called: the model
cannot reach any other route.

Tools that read are free to run. Tools that change something (send, post, comment, complete, lock, kill,
start/stop Jarvis...) go through the assistant's approval step like run_command does.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from . import auth
from .assistant import ExtraTool

MAX_LINES = 40

# Laptop actions that only read status or press a media key: no approval needed. Everything else asks.
FREE_LAPTOP_ACTIONS = {"list", "system_info", "battery", "disk_usage", "network_info", "uptime",
                       "list_processes", "volume_up", "volume_down", "mute", "play_pause",
                       "next_track", "previous_track"}
NO_IMAGES = {"screenshot"}  # the assistant can't look at pictures; the Apps screen shows them


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _bad_gid(args: dict) -> str | None:
    """Task ids go into a URL path, so only plain numbers are allowed (no '../' tricks)."""
    gid = str(args.get("gid", ""))
    return None if gid.isascii() and gid.isdigit() else "A task id is a number, taken from asana_tasks."


def _s(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {"type": "string"}


class Hub:
    """Calls the hub's own routes in-process with an owner session cookie."""

    def __init__(self, get_app: Callable[[], Any], config, base_url: str = "http://hub.internal"):
        self._get_app, self._config, self._base = get_app, config, base_url
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._get_app()), base_url=self._base,
                headers={"X-Hub": "1"}, timeout=120.0)
        return self._client

    async def call(self, method: str, path: str, *, params: dict | None = None,
                   body: dict | None = None) -> tuple[bool, Any]:
        """(ok, json) on success; (False, a plain sentence) when the route said no."""
        try:
            # A fresh session cookie per call, so it never expires while the hub keeps running.
            cookie = f"{auth.COOKIE}={auth.make_session(self._config.session_secret)}"
            response = await self._http().request(method, path, params=params, json=body,
                                                  headers={"Cookie": cookie})
        except httpx.HTTPError as exc:
            return False, f"The hub could not be reached from inside ({exc})."
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            if response.status_code == 404 and not detail:
                return False, "That feature is not available on this hub."
            return False, str(detail or f"The hub answered {response.status_code}.")
        return True, data


# ---- formatting: short plain text, because the answer is read on a phone -----------------------------

def _due(task: dict) -> str:
    return task.get("due_on") or "no date"


def fmt_tasks(data: dict) -> str:
    counts = data.get("counts", {})
    head = (f"{counts.get('overdue', 0)} overdue, {counts.get('today', 0)} due today, "
            f"{counts.get('upcoming', 0)} upcoming.")
    tasks = data.get("tasks", [])
    if not tasks:
        return head + " No tasks in this list."
    lines = [f"- {t['name']} (due {_due(t)}{', ' + t['project'] if t.get('project') else ''}) id {t['gid']}"
             for t in tasks[:MAX_LINES]]
    more = f"\n... and {len(tasks) - MAX_LINES} more." if len(tasks) > MAX_LINES else ""
    return head + "\n" + "\n".join(lines) + more


def fmt_task(data: dict) -> str:
    t = data["task"]
    out = [f"{t['name']} (due {_due(t)}), project {t.get('project') or 'none'}, "
           f"{'completed' if t.get('completed') else 'open'}."]
    if t.get("notes"):
        out.append("Notes: " + t["notes"][:1500])
    for s in t.get("subtasks", [])[:15]:
        out.append(f"- subtask [{'x' if s.get('completed') else ' '}] {s['name']}")
    for c in t.get("comments", [])[-5:]:
        out.append(f"- comment by {c.get('author')}: {str(c.get('text'))[:300]}")
    return "\n".join(out)


def fmt_meetings(data: dict) -> str:
    meetings = data.get("meetings", [])
    if not meetings:
        return "No meetings."
    lines = []
    for m in meetings[:MAX_LINES]:
        when = "all day" if m.get("all_day") else f"{str(m['start'])[11:16]}-{str(m['end'])[11:16]}"
        lines.append(f"- {when} {m['title']}" + (f" [{m['platform']} link]" if m.get("platform") else ""))
    return "\n".join(lines)


def fmt_result(data: dict) -> str:
    """A laptop action result as text."""
    text = data.get("message") or ""
    if data.get("kind") == "table":
        cols = data.get("columns", [])
        rows = [" | ".join(str(c) for c in r) for r in data.get("rows", [])[:25]]
        return "\n".join([text, " | ".join(cols), *rows]).strip()
    if data.get("kind") == "text" and data.get("text"):
        return (text + "\n" + str(data["text"])).strip()
    return text or "Done."


def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = Hub(get_app, config)

    def tool(name, description, props, required, run, needs_approval=False, preview=None):
        return ExtraTool({"name": name, "description": description,
                          "input_schema": _schema(props, required)}, run, needs_approval, preview)

    async def asana_tasks(a):
        ok, d = await hub.call("GET", "/api/work/asana/tasks", params={"filter": a.get("filter") or "today"})
        return ok, fmt_tasks(d) if ok else d

    async def asana_task(a):
        if (problem := _bad_gid(a)):
            return False, problem
        ok, d = await hub.call("GET", f"/api/work/asana/tasks/{a['gid']}")
        return ok, fmt_task(d) if ok else d

    async def calendar(a):
        params = {"days": int(a.get("days") or 1)}
        if a.get("day"):
            params["day"] = a["day"]
        ok, d = await hub.call("GET", "/api/work/calendar", params=params)
        return ok, fmt_meetings(d) if ok else d

    async def asana_comment(a):
        if (problem := _bad_gid(a)):
            return False, problem
        ok, d = await hub.call("POST", f"/api/work/asana/tasks/{a['gid']}/comment", body={"text": a["text"]})
        return ok, "Comment added." if ok else d

    async def asana_complete(a):
        if (problem := _bad_gid(a)):
            return False, problem
        ok, d = await hub.call("POST", f"/api/work/asana/tasks/{a['gid']}/complete")
        return ok, "Task marked complete." if ok else d

    drafts: dict[tuple, str] = {}  # the draft shown to the owner is the one that gets sent

    async def email_preview(a):
        """Resolve the recipient and keep the draft, so the approval card shows the real address."""
        ok, d = await hub.call("POST", "/api/work/email/draft",
                               body={"to": a["to"], "subject": a["subject"], "body": a["body"]})
        if not ok:
            return False, d
        drafts[(a["to"], a["subject"], a["body"])] = d["draft_id"]
        return True, {"resolved_to": d["to"]}

    async def send_email(a):
        draft_id = drafts.pop((a["to"], a["subject"], a["body"]), None)
        if draft_id is None:  # approved without a preview (auto-approve): create the draft now
            ok, d = await hub.call("POST", "/api/work/email/draft",
                                   body={"to": a["to"], "subject": a["subject"], "body": a["body"]})
            if not ok:
                return False, d
            draft_id = d["draft_id"]
        ok, sent = await hub.call("POST", "/api/work/email/send", body={"draft_id": draft_id})
        return ok, f"Email sent to {sent['to']}." if ok else sent

    async def chat_send(a):
        ok, d = await hub.call("POST", "/api/work/chat/send", body={"space": a["space"], "text": a["text"]})
        return ok, f"Posted to {a['space']}." if ok else d

    async def laptop_action(a):
        action, params = a["action"], a.get("params") or {}
        if action == "list":
            ok, d = await hub.call("GET", "/api/laptop/actions")
            if not ok:
                return False, d
            return True, "\n".join(f"- {x['id']}: {x['description']}"
                                   + (" (asks first)" if x["risk"] == "confirm" else "")
                                   + ("" if x["available"] else f" [not available: {x.get('unavailable_reason')}]")
                                   for x in d["actions"] if x["id"] not in NO_IMAGES)
        if action in NO_IMAGES:
            return False, "I can't look at pictures. Ask the owner to open Apps > Screenshot on the phone."
        # The owner already approved this call in the assistant, so confirm for the route.
        ok, d = await hub.call("POST", "/api/laptop/run", body={"action": action, "params": params, "confirm": True})
        return ok, fmt_result(d) if ok else d

    async def jarvis(a):
        act = a["action"]
        route = {"status": ("GET", "/api/jarvis/status", None), "start": ("POST", "/api/jarvis/start", None),
                 "stop": ("POST", "/api/jarvis/stop", {"confirm": True}),
                 "say": ("POST", "/api/jarvis/say", {"text": a.get("text", "")})}[act]
        ok, d = await hub.call(route[0], route[1], body=route[2])
        if not ok:
            return False, d
        if act == "status":
            return True, (f"Jarvis is {'running' if d['running'] else 'not running'}"
                          f"{'' if d['configured'] else ' (not set up: ' + str(d.get('reason')) + ')'}.")
        return True, {"start": "Jarvis started.", "stop": "Jarvis stopped.", "say": "Sent to Jarvis."}[act]

    async def english_progress(a):
        ok, d = await hub.call("GET", "/api/english/overview")
        if not ok:
            return False, d
        return True, (f"{d['streak_days']} day streak, {d['sessions_total']} speaking sessions, "
                      f"{d['vocab_due']} words due for review.")

    async def quiz_history(a):
        ok, d = await hub.call("GET", "/api/learn/history")
        if not ok:
            return False, d
        recent = ", ".join(f"{q['title']} {round(q['percent'])}%" for q in d["quizzes"][:5]) or "none yet"
        return True, f"{d['streak_days']} day quiz streak. Recent: {recent}."

    return [
        tool("asana_tasks", "List the owner's open Asana tasks. filter: today (default), overdue, upcoming or all. Read-only.",
             {"filter": {"type": "string", "enum": ["today", "overdue", "upcoming", "all"]}}, [], asana_tasks),
        tool("asana_task", "Details of one Asana task (notes, subtasks, recent comments) by its id. Read-only.",
             {"gid": _s("The task id from asana_tasks.")}, ["gid"], asana_task),
        tool("calendar", "The owner's meetings. day is YYYY-MM-DD (default today), days how many days (default 1). Read-only.",
             {"day": _s(), "days": {"type": "integer"}}, [], calendar),
        tool("asana_comment", "Add a comment to an Asana task. The owner is asked first.",
             {"gid": _s(), "text": _s()}, ["gid", "text"], asana_comment, needs_approval=True),
        tool("asana_complete", "Mark an Asana task complete. The owner is asked first.",
             {"gid": _s()}, ["gid"], asana_complete, needs_approval=True),
        tool("send_email", "Send an email from the owner's account. 'to' is a contact name, 'me' or an address. The owner "
                           "sees the whole email and is asked first.",
             {"to": _s(), "subject": _s(), "body": _s()}, ["to", "subject", "body"], send_email, needs_approval=True,
             preview=email_preview),
        tool("chat_send", "Post a message to one of the owner's Google Chat spaces. The owner is asked first.",
             {"space": _s(), "text": _s()}, ["space", "text"], chat_send, needs_approval=True),
        tool("laptop_action", "Do something on the laptop through its action list (lock, open a link or app, volume, "
                              "processes, clipboard...). Use action 'list' first to see the actions and their params. "
                              "Reading status is free; anything that changes the laptop asks the owner first.",
             {"action": _s("An action id, or 'list'."), "params": {"type": "object"}}, ["action"], laptop_action,
             needs_approval=lambda a: a.get("action") not in FREE_LAPTOP_ACTIONS),
        tool("jarvis", "Control the owner's Jarvis voice assistant on the laptop: status (free), start, stop, or say "
                       "(send it a text command). Start, stop and say ask the owner first.",
             {"action": {"type": "string", "enum": ["status", "start", "stop", "say"]}, "text": _s()}, ["action"], jarvis,
             needs_approval=lambda a: a.get("action") != "status"),
        tool("english_progress", "The owner's English practice streak and progress. Read-only.", {}, [], english_progress),
        tool("quiz_history", "The owner's recent quiz results and streak. Read-only.", {}, [], quiz_history),
    ]
