"""Assistant tools for Friday (the spoken day brief, day plan and laptop prep). See docs/api/friday.md.

Same rules as hub/assistant_tools_more.py: every tool calls the hub's own /api/friday route in-process, so the
route's checks and audit log apply. Reading the brief, proposing a plan and proposing laptop prep are free (a
proposal changes nothing). Adding the plan to the to-dos and opening things on the laptop ask the owner first,
and the approval card shows the exact blocks / addresses / files / apps. The model can only pass ids that the
hub itself stored as pending; it can never name a URL or path to open.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .assistant import ExtraTool
from .common import InProcessClient

ID_RE = re.compile(r"^[0-9a-f]{12}$")
BLOCK_RE = re.compile(r"^[0-9a-f]{10}$")


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _short(value: Any, n: int = 160) -> str:
    return " ".join(str(value if value is not None else "").split())[:n]


def _id_list(raw: Any, pattern: re.Pattern, required: bool) -> tuple[list[str] | None, str | None]:
    if raw is None and not required:
        return None, None
    if not isinstance(raw, list) or not raw or len(raw) > 50 or not all(isinstance(i, str) and pattern.match(i)
                                                                       for i in raw):
        return None, "Those ids are not valid. Take them from the proposal tool's answer."
    return raw, None


def fmt_plan(data: Any) -> str:
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not blocks:
        return _short(data.get("spoken") if isinstance(data, dict) else "", 600) or "Nothing to plan."
    lines = [f"- {b.get('start', '')[11:16]}-{b.get('end', '')[11:16]} {b.get('kind')}: {_short(b.get('title'), 100)} "
             f"(block id {b.get('id')})" for b in blocks[:30]]
    return "Proposed plan (nothing saved yet):\n" + "\n".join(lines) + "\nSpoken: " + _short(data.get("spoken"), 600)


def fmt_prep(items: list) -> str:
    if not items:
        return "Nothing to propose for the laptop."
    return "Proposals waiting for the owner's approval (nothing opened):\n" + "\n".join(
        f"- [{i.get('kind')}] {_short(i.get('target'), 300)} because {_short(i.get('why'), 120)} (id {i.get('id')})"
        for i in items[:30])


def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    def tool(name, description, props, required, run, needs_approval=False, preview=None):
        return ExtraTool({"name": name, "description": description, "input_schema": _schema(props, required)},
                         run, needs_approval, preview)

    model_prop = {"model": {"type": "string", "enum": list(PROVIDERS)}}

    async def friday_brief(a):
        params = {"model": a["model"]} if a.get("model") in PROVIDERS else None
        ok, d = await hub.call("GET", "/api/friday/brief", params=params)
        if not ok:
            return False, d
        failed = d.get("sources_failed") or []
        return True, _short(d.get("spoken"), 1500) + (f"\n(Could not read: {', '.join(map(str, failed))}.)"
                                                        if failed else "")

    async def friday_plan(a):
        body: dict = {}
        if a.get("model") in PROVIDERS:
            body["model"] = a["model"]
        if isinstance(a.get("focus_hours"), (int, float)) and not isinstance(a.get("focus_hours"), bool):
            body["focus_hours"] = max(0, min(12, a["focus_hours"]))
        ok, d = await hub.call("POST", "/api/friday/plan", body=body)
        return ok, fmt_plan(d) if ok else d

    async def apply_preview(a):
        ids, problem = _id_list(a.get("block_ids"), BLOCK_RE, required=False)
        if problem:
            return False, problem
        return True, {"adds": "local to-dos and reminders only (never Asana or the calendar)",
                      "blocks": ids or "every block of today's plan except breaks"}

    async def friday_apply_plan(a):
        ids, problem = _id_list(a.get("block_ids"), BLOCK_RE, required=False)
        if problem:
            return False, problem
        body: dict = {"confirm": True}
        if ids:
            body["block_ids"] = ids
        ok, d = await hub.call("POST", "/api/friday/plan/apply", body=body)
        if not ok:
            return False, d
        failed = [r for r in d.get("results") or [] if not r.get("ok")]
        return True, f"Added {d.get('applied', 0)} blocks to the to-dos and reminders." + (
            f" {len(failed)} failed: {_short(failed[0].get('error'), 160)}" if failed else "")

    async def friday_laptop_prep(a):
        ok, d = await hub.call("POST", "/api/friday/laptop-prep", body={})
        return ok, fmt_prep(d.get("items") or []) if ok else d

    async def run_preview(a):
        ids, problem = _id_list(a.get("ids"), ID_RE, required=True)
        if problem:
            return False, problem
        ok, d = await hub.call("GET", "/api/friday/pending")
        if not ok:
            return False, d
        table = {i.get("id"): i for i in d.get("items") or []}
        missing = [i for i in ids if i not in table]
        if missing:
            return False, "Some of those ids are not pending (expired, done or never proposed). Propose again."
        return True, {"will_open": [f"{table[i].get('kind')}: {table[i].get('target')}" for i in ids]}

    async def friday_run_laptop_prep(a):
        ids, problem = _id_list(a.get("ids"), ID_RE, required=True)
        if problem:
            return False, problem
        ok, d = await hub.call("POST", "/api/friday/laptop-prep/run", body={"confirm": True, "ids": ids})
        if not ok:
            return False, d
        rows = d.get("results") or []
        return True, "\n".join(f"- {_short(r.get('target'), 200) or r.get('id')}: "
                               f"{'opened' if r.get('ok') else 'not opened: ' + _short(r.get('error'), 160)}"
                               for r in rows) or "Nothing was run."

    return [
        tool("friday_brief", "Friday's spoken update for today: urgent items, Asana tags, tasks, meetings, mail "
                             "headers, chat alerts and reminders, as a short script to read aloud. Read-only.",
             model_prop, [], friday_brief),
        tool("friday_plan", "Propose a time-blocked plan for the rest of today around real meetings (nothing is "
                            "saved). focus_hours: total hours of work wanted. Read-only.",
             {**model_prop, "focus_hours": {"type": "number"}}, [], friday_plan),
        tool("friday_apply_plan", "Add today's proposed plan blocks (ids from friday_plan, or all) to the local "
                                  "to-dos and reminders. Never touches Asana or the calendar. The owner is asked first.",
             {"block_ids": {"type": "array", "items": {"type": "string"}}}, [], friday_apply_plan,
             needs_approval=True, preview=apply_preview),
        tool("friday_laptop_prep", "Propose what to open on the laptop for today (meeting join links, links in "
                                   "today's Asana task notes, your apps, recently changed files). Stores the "
                                   "proposals as pending; nothing is opened. Read-only.",
             {}, [], friday_laptop_prep),
        tool("friday_run_laptop_prep", "Open pending laptop-prep proposals by id (from friday_laptop_prep). Only "
                                       "stored pending ids work. The owner is asked first and sees each exact target.",
             {"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"], friday_run_laptop_prep,
             needs_approval=True, preview=run_preview),
    ]
