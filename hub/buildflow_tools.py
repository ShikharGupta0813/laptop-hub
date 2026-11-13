"""Assistant tools for the Build flow (spec-first delegation). See docs/api/buildflow.md.

Same rules as the other tool modules: each tool calls the hub's own /api/buildflow route in-process, so the route's
checks and audit log apply. Asking questions and reading status only touch the flow's own small database. Writing the
spec, writing the plan and handing a step to the Council are mutating: the owner is asked first (the card shows the
file path and its first 20 lines, or the exact step), and they are refused on a turn that read outside text.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .assistant import ExtraTool, mark_tools
from .common import InProcessClient

ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def fmt_question(d: dict) -> str:
    q = d.get("question")
    head = f"Build {d.get('id')} ({d.get('title')})."
    if q:
        n = len(d.get("answers") or []) + 1
        opts = "; ".join(q.get("options") or [])
        return (f"{head} Ask the owner this ONE question (number {n} of at most {d.get('max_questions')}), then wait for "
                f"the answer and call build_answer: {q.get('question')} Suggested answers: {opts}.")
    return f"{head} All the questions are answered. Say it is ready and call build_spec."


def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    def tool(name, description, props, required, run, needs_approval=False, preview=None):
        return ExtraTool({"name": name, "description": description, "input_schema": _schema(props, required)},
                         run, needs_approval, preview)

    def bad_id(a) -> str | None:
        fid = a.get("id")
        if not isinstance(fid, str) or not ID_RE.match(fid):
            return "That build id is not valid. Take it from build_list."
        return None

    async def build_start(a):
        goal, workspace = a.get("goal"), a.get("workspace")
        if not isinstance(goal, str) or not goal.strip():
            return False, "Say what the owner wants built."
        if not isinstance(workspace, str) or not workspace.strip():
            return False, "Say which shared folder to build in (the owner's folder names are in the workspace list)."
        body = {"goal": goal, "workspace": workspace}
        if isinstance(a.get("title"), str) and a["title"].strip():
            body["title"] = a["title"]
        ok, d = await hub.call("POST", "/api/buildflow", body=body)
        return (True, fmt_question(d)) if ok else (False, d)

    async def build_answer(a):
        if (p := bad_id(a)):
            return False, p
        if not isinstance(a.get("answer"), str) or not a["answer"].strip():
            return False, "Pass the owner's answer."
        ok, d = await hub.call("POST", f"/api/buildflow/{a['id']}/answer", body={"answer": a["answer"]})
        return (True, fmt_question(d)) if ok else (False, d)

    async def file_preview(a, what):
        if (p := bad_id(a)):
            return False, p
        ok, d = await hub.call("POST", f"/api/buildflow/{a['id']}/{what}", body={"preview": True})
        if not ok:
            return False, d
        return True, {"writes_file": d["path"], "first_lines": d["first_lines"], "total_lines": d["total_lines"]}

    async def spec_preview(a):
        return await file_preview(a, "spec")

    async def plan_preview(a):
        return await file_preview(a, "plan")

    async def build_spec(a):
        if (p := bad_id(a)):
            return False, p
        ok, d = await hub.call("POST", f"/api/buildflow/{a['id']}/spec", body={})
        if not ok:
            return False, d
        return True, f"Spec written to {d['spec_path']}. The owner can edit it. Next: build_plan."

    async def build_plan(a):
        if (p := bad_id(a)):
            return False, p
        ok, d = await hub.call("POST", f"/api/buildflow/{a['id']}/plan", body={})
        if not ok:
            return False, d
        return True, (f"Plan written to {d['plan_path']} with {d['total']} steps. The owner can edit it; its checkboxes "
                      "are the progress. Next: build_next.")

    async def next_preview(a):
        if (p := bad_id(a)):
            return False, p
        ok, d = await hub.call("POST", f"/api/buildflow/{a['id']}/next", body={"preview": True})
        if not ok:
            return False, d
        return True, {"hands_to_council": f"step {d['number']} of {d['of']}: {d['step']}", "done_when": d["check"],
                      "runs": d["runs"]}

    async def build_next(a):
        if (p := bad_id(a)):
            return False, p
        ok, d = await hub.call("POST", f"/api/buildflow/{a['id']}/next", body={})
        if not ok:
            return False, d
        return True, (f"Started council run {d['job_id']} for the next step in the background. When it passes the box is "
                      "ticked in the plan file; use build_status to look.")

    async def build_status(a):
        if (p := bad_id(a)):
            return False, p
        ok, d = await hub.call("GET", f"/api/buildflow/{a['id']}/status")
        if not ok:
            return False, d
        lines = [f"{d['title']} [{d['stage']}]: {d['summary']}"]
        for s in d["steps"][:30]:
            mark = "x" if s["done"] else ("running" if s["running"] else " ")
            extra = f" (last try failed: {s['failure']})" if s.get("failure") else ""
            lines.append(f"[{mark}] {s['n']}. {s['text']}{extra}")
        return True, "\n".join(lines)

    async def build_list(a):
        ok, d = await hub.call("GET", "/api/buildflow")
        if not ok:
            return False, d
        if not d.get("enabled"):
            return True, "The build flow is switched off."
        flows = d.get("flows") or []
        if not flows:
            return True, "No builds yet."
        return True, "\n".join(f"- {f['id']}: {f['title']} [{f['stage']}] {f['summary']}" for f in flows[:20])

    return mark_tools([
        tool("build_start",
             "Start a spec-first BUILD for the owner ('build me X', 'new project'). Asks the free AI for the FIRST "
             "clarifying question; ask the owner that one question, then call build_answer with their answer. One "
             "question at a time, at most 6. The workspace must be one of the owner's shared folder names.",
             {"goal": {"type": "string", "description": "What the owner wants built, in their words."},
              "workspace": {"type": "string", "description": "A shared folder name (or a folder inside one) to build in."},
              "title": {"type": "string", "description": "Optional short name."}},
             ["goal", "workspace"], build_start),
        tool("build_answer", "Give the owner's answer to the current build question; returns the next question or says the spec is ready.",
             {"id": {"type": "string"}, "answer": {"type": "string"}}, ["id", "answer"], build_answer),
        tool("build_spec",
             "Write the spec file (docs/specs/DATE-name.md) inside the workspace from the answers. Never overwrites. "
             "The owner is asked first and sees the path and the first lines.",
             {"id": {"type": "string"}}, ["id"], build_spec, needs_approval=True, preview=spec_preview),
        tool("build_plan",
             "Turn the spec into a checkbox plan file (docs/plans/DATE-name.md). The owner may edit it; checkboxes are "
             "the progress. The owner is asked first and sees the path and the first lines.",
             {"id": {"type": "string"}}, ["id"], build_plan, needs_approval=True, preview=plan_preview),
        tool("build_next",
             "Hand the NEXT unchecked plan step to the Council (free models) in the background. Returns a run id; the "
             "box is ticked when it passes. The owner is asked first (unless Full Control) and sees the step.",
             {"id": {"type": "string"}}, ["id"], build_next, needs_approval=True, preview=next_preview),
        tool("build_status", "'N of M done, next: ...' and the plan's steps for one build. Read-only.",
             {"id": {"type": "string"}}, ["id"], build_status),
        tool("build_list", "List the owner's builds with their stage and progress. Read-only.", {}, [], build_list),
    ], untrusted=(), mutating=("build_spec", "build_plan", "build_next"))
