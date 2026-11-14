"""BUILD FLOW: spec-first delegation. "Jarvis, build me X" -> one question at a time -> a spec file -> a checkbox plan
-> the plan is worked through step by step by the Council while the owner only approves. See docs/api/buildflow.md.

A flow = {id, title, workspace, stage: brainstorm|spec|plan|build|done, answers[], spec_path, plan_path, steps[]}
kept in <data dir>/buildflow.sqlite. The spec and the plan are plain markdown files INSIDE the workspace
(docs/specs/YYYY-MM-DD-<slug>.md, docs/plans/<same-slug>.md). The plan file is the truth: steps are parsed back from it
on every look, so the owner can hand-edit it, and a ticked checkbox is progress.

Safety: the workspace must be one of the owner's shared roots (or a folder inside one); no traversal, no symlink
escape. Files are created with exclusive-create, never overwritten (a -2 suffix is used). Writing a file and
delegating a step are mutating: the assistant tools ask the owner first (preview = path + first 20 lines). The AI is
the configured free one (Gemini or local, never the paid Claude API). Steps run through the existing Council routes.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import brain as brain_mod
from . import proactive
from .autopilot import pick_provider
from .common import Ctx, InProcessClient, parse_json
from . import panic as _panic
from .llm import LLMError

log = logging.getLogger("hub.buildflow")

MAX_QUESTIONS = 6
MAX_FLOWS = 50
MAX_STEPS = 30
MAX_GOAL = 1500
MAX_ANSWER = 800
MAX_TITLE = 80
PREVIEW_LINES = 20
MODEL_TIMEOUT = 120.0
POLL_SECONDS = 20
ID_RE = re.compile(r"^[0-9a-f]{12}$")
STAGES = ("brainstorm", "spec", "plan", "build", "done")
STEP_RE = re.compile(r"^(\s*)[-*]\s*\[( |x|X)\]\s*(?:(\d+)[.)]\s*)?(.+?)\s*$")
CHECK_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:acceptance check|acceptance|check)\s*[:\-]\s*(.+?)\s*$", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]")
Caller = Callable[..., Awaitable[tuple[bool, Any]]]
Ask = Callable[[str, str], Awaitable[str]]


class BuildError(ValueError):
    """A problem with a build flow; the message is a plain sentence for the owner."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean(value: Any, limit: int, multiline: bool = False) -> str:
    text = _CONTROL.sub("", str(value if value is not None else ""))
    if multiline:
        text = "\n".join(" ".join(line.split()) for line in text.replace("\r", "").split("\n")).strip()
    else:
        text = " ".join(text.split())
    return text[:limit]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40].strip("-")
    return slug or "build"


# ---- workspace safety --------------------------------------------------------------------------------------------

def roots_of(config) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, raw in (getattr(config, "roots", None) or {}).items():
        try:
            out[str(name)] = Path(str(raw)).resolve()
        except (OSError, ValueError):
            continue
    return out


def resolve_workspace(config, value: Any) -> Path:
    """The folder for `value`: a shared root's name ("docs"), "root/sub/folder", or an absolute path inside a root.
    Refuses anything else (a path outside every root, `..` anywhere, a file, a missing folder)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
        raise BuildError("Pick one of your shared folders as the workspace.")
    raw = value.strip()
    roots = roots_of(config)
    if not roots:
        raise BuildError("No shared folders are set up on this hub, so there is nowhere to build.", 409)
    parts = re.split(r"[\\/]+", raw)
    if ".." in parts:
        raise BuildError("That workspace path goes outside your shared folders (no '..' allowed).")
    if raw in roots:
        target = roots[raw]
    elif parts[0] in roots and not re.match(r"^(?:[A-Za-z]:|/|\\\\)", raw):
        target = roots[parts[0]].joinpath(*[p for p in parts[1:] if p])
    else:
        target = Path(raw)
        if not target.is_absolute():
            raise BuildError("The workspace must be one of your shared folders: " + ", ".join(sorted(roots)) + ".")
    try:
        target = target.resolve()
    except (OSError, ValueError):
        raise BuildError("That workspace path is not usable.") from None
    if not any(target == base or base in target.parents for base in roots.values()):
        raise BuildError("That folder is not inside one of your shared folders, so Jarvis will not build there.")
    if not target.is_dir():
        raise BuildError("That workspace folder does not exist.")
    return target


def inside(base: Path, rel: str) -> Path:
    """`rel` under `base`, resolved; refuses an escape."""
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise BuildError("That path is outside the workspace.")
    return target


# ---- plan file ---------------------------------------------------------------------------------------------------

def parse_plan(text: str) -> list[dict]:
    """Steps from markdown: '- [ ] 1. Do it' (+ an indented 'Check: ...' line). Hand edits are fine: any checkbox
    line is a step; the number is the file's own when present."""
    steps: list[dict] = []
    lines = text.replace("\r", "").split("\n")
    for index, line in enumerate(lines):
        m = STEP_RE.match(line)
        if not m or len(steps) >= MAX_STEPS:
            continue
        step = {"n": int(m.group(3)) if m.group(3) else len(steps) + 1, "text": clean(m.group(4), 400),
                "done": m.group(2) in "xX", "check": "", "line": index}
        for follow in lines[index + 1:index + 3]:
            c = CHECK_RE.match(follow)
            if c:
                step["check"] = clean(c.group(1), 300)
                break
            if STEP_RE.match(follow):
                break
        if step["text"]:
            steps.append(step)
    return steps


def tick_line(text: str, line_no: int) -> str:
    lines = text.split("\n")
    if 0 <= line_no < len(lines):
        lines[line_no] = re.sub(r"\[ \]", "[x]", lines[line_no], count=1)
    return "\n".join(lines)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def create_new(folder: Path, stem: str, text: str) -> Path:
    """Write folder/<stem>.md without ever overwriting: -2, -3 ... on a clash."""
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1, 200):
        path = folder / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(text)
            return path
        except FileExistsError:
            continue
    raise BuildError("Too many files with that name already exist.", 409)


# ---- the store ---------------------------------------------------------------------------------------------------

COLUMNS = ("id", "title", "goal", "workspace", "stage", "answers", "pending", "ready", "spec_path", "plan_path",
           "draft", "job_id", "job_step", "notes", "finished", "created", "updated")
JSON_COLUMNS = ("answers", "pending", "draft", "notes")


class FlowStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS flows(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, workspace TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'brainstorm', answers TEXT NOT NULL DEFAULT '[]', pending TEXT NOT NULL DEFAULT 'null',
                ready INTEGER NOT NULL DEFAULT 0, spec_path TEXT NOT NULL DEFAULT '', plan_path TEXT NOT NULL DEFAULT '',
                draft TEXT NOT NULL DEFAULT 'null', job_id TEXT NOT NULL DEFAULT '', job_step TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '{}', finished INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL, updated TEXT NOT NULL)""")

    @contextlib.contextmanager
    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _load(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        flow = dict(row)
        for key in JSON_COLUMNS:
            try:
                flow[key] = json.loads(flow[key])
            except (TypeError, ValueError):
                flow[key] = [] if key == "answers" else ({} if key == "notes" else None)
        flow["ready"], flow["finished"] = bool(flow["ready"]), bool(flow["finished"])
        return flow

    def count(self) -> int:
        with self.lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM flows").fetchone()[0]

    def add(self, flow: dict) -> dict:
        row = {k: flow.get(k) for k in COLUMNS}
        for key in JSON_COLUMNS:
            row[key] = json.dumps(row[key])
        with self.lock, self._conn() as c:
            c.execute(f"INSERT INTO flows({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                      [row[k] for k in COLUMNS])
        return self.get(flow["id"])  # type: ignore[return-value]

    def get(self, flow_id: str) -> dict | None:
        with self.lock, self._conn() as c:
            return self._load(c.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone())

    def update(self, flow_id: str, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in COLUMNS and k != "id"}
        for key in JSON_COLUMNS:
            if key in fields:
                fields[key] = json.dumps(fields[key])
        for key in ("ready", "finished"):
            if key in fields:
                fields[key] = 1 if fields[key] else 0
        if fields:
            with self.lock, self._conn() as c:
                c.execute(f"UPDATE flows SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), flow_id])
        return self.get(flow_id)

    def delete(self, flow_id: str) -> bool:
        with self.lock, self._conn() as c:
            return c.execute("DELETE FROM flows WHERE id=?", (flow_id,)).rowcount > 0

    def all(self) -> list[dict]:
        with self.lock, self._conn() as c:
            rows = c.execute("SELECT * FROM flows ORDER BY created DESC, rowid DESC").fetchall()
        return [self._load(r) for r in rows]  # type: ignore[misc]


# ---- AI prompts --------------------------------------------------------------------------------------------------

QUESTION_SYSTEM = (
    "You help a non-technical owner shape a small software project before anything is built. Ask ONE short, plain "
    "question at a time (no jargon) that most reduces what is unclear. Reply with JSON only: "
    '{"question": "...", "options": ["short answer", "short answer", "short answer"]} with 2 or 3 suggested answers. '
    'When you already know enough to write a spec, reply {"ready": true}. Never repeat a question already answered.')
SPEC_SYSTEM = (
    "You write a short project spec in plain language for a non-technical owner. Reply with JSON only: "
    '{"goal": "one or two sentences", "scope": ["..."], "non_goals": ["..."], "approach": ["..."], '
    '"acceptance": ["a check anyone can run or see"], "risks": ["..."]}. Each list has 2 to 6 short items.')
PLAN_SYSTEM = (
    "You turn a spec into a build plan of small steps that each fit one short coding job. Reply with JSON only: "
    '{"steps": [{"text": "what to do, one sentence", "check": "one line: how to tell it worked"}]} '
    "with 3 to 12 steps in order. The last step is the final check against the spec.")


def _items(value: Any, n: int = 6, width: int = 240) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for t in (clean(v, width) for v in value[:n]) if t]


def render_spec(flow: dict, data: dict) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- (none yet)"
    goal = clean(data.get("goal"), 600) or flow["goal"]
    return "\n".join([
        f"# {flow['title']}", "", f"_Spec drafted by Jarvis on {flow['created'][:10]}. Edit it freely._", "",
        "## Goal", goal, "", "## Scope", bullets(_items(data.get("scope"))), "",
        "## Non-goals", bullets(_items(data.get("non_goals"))), "",
        "## Approach", bullets(_items(data.get("approach"))), "",
        "## Acceptance checks", bullets(_items(data.get("acceptance"))), "",
        "## Risks", bullets(_items(data.get("risks"))), ""])


def render_plan(flow: dict, spec_rel: str, steps: list[dict]) -> str:
    out = [f"# Plan: {flow['title']}", "", f"Spec: `{spec_rel}`", "",
           "Tick a box (change `[ ]` to `[x]`) when a step is done. Jarvis follows this file.", ""]
    for n, step in enumerate(steps, 1):
        out += [f"- [ ] {n}. {step['text']}", f"  - Check: {step['check']}"]
    return "\n".join(out) + "\n"


# ---- the engine --------------------------------------------------------------------------------------------------

class BuildFlow:
    def __init__(self, ctx: Ctx, *, ask: Ask | None = None, caller: Caller | None = None,
                 clock: Callable[[], dt.datetime] | None = None):
        self.ctx, self.config = ctx, ctx.config
        self.store = FlowStore(Path(ctx.config.data_path()) / "buildflow.sqlite")
        self._ask, self._caller = ask, caller
        self.clock = clock or dt.datetime.now
        self.client: InProcessClient | None = None
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return getattr(self.config, "buildflow_enabled", True) is not False and not _panic.on(self.config)

    def require_on(self) -> None:
        if not self.enabled:
            raise BuildError("The build flow is switched off (buildflow_enabled in Settings).", 409)

    def now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    # -- helpers
    def get(self, flow_id: Any) -> dict:
        flow = self.store.get(flow_id) if isinstance(flow_id, str) and ID_RE.match(flow_id) else None
        if flow is None:
            raise BuildError("There is no build with that id.", 404)
        return flow

    def workspace_dir(self, flow: dict) -> Path:
        try:
            return resolve_workspace(self.config, flow["workspace"])
        except BuildError as exc:      # the folder or the root went away since
            raise BuildError(f"The workspace is not usable any more: {exc}", 409) from None

    async def complete(self, system: str, user: str) -> Any:
        """One AI call, JSON back. Free providers only. Raises BuildError (503) when the AI cannot answer."""
        last: Exception | None = None
        for _ in range(2):
            try:
                if self._ask is not None:
                    reply = await asyncio.wait_for(self._ask(system, user), MODEL_TIMEOUT)
                else:
                    model = self.ctx.model_for(pick_provider(self.config))
                    reply = await asyncio.wait_for(model.complete(system, user, json_mode=True), MODEL_TIMEOUT)
                return parse_json(reply)
            except asyncio.TimeoutError:
                raise BuildError("The AI took too long. Try again.", 503) from None
            except LLMError as exc:
                raise BuildError(str(exc), 503) from None
            except ValueError as exc:
                last = exc
                user += "\n\nYour last reply was not valid JSON. Reply with the JSON only."
        raise BuildError(f"The AI's answer could not be read ({last}). Try again.", 503)

    async def call(self, method: str, path: str, **kwargs):
        if self._caller is not None:
            return await self._caller(method, path, **kwargs)
        if self.client is None:
            self.client = InProcessClient(self.ctx.get_app, self.config)
        return await self.client.call(method, path, **kwargs)

    # -- views
    def steps_of(self, flow: dict) -> list[dict]:
        if not flow["plan_path"]:
            return []
        try:
            text = inside(self.workspace_dir(flow), flow["plan_path"]).read_text(encoding="utf-8")
        except (OSError, BuildError):
            return []
        return parse_plan(text)

    def view(self, flow: dict, steps: list[dict] | None = None) -> dict:
        steps = self.steps_of(flow) if steps is None else steps
        done = sum(1 for s in steps if s["done"])
        nxt = next((s for s in steps if not s["done"]), None)
        notes = flow.get("notes") or {}
        return {
            "id": flow["id"], "title": flow["title"], "goal": flow["goal"], "workspace": flow["workspace"],
            "workspace_name": Path(flow["workspace"]).name or flow["workspace"], "stage": flow["stage"],
            "answers": flow["answers"], "question": flow["pending"] if not flow["ready"] else None,
            "ready": flow["ready"], "max_questions": MAX_QUESTIONS, "spec_path": flow["spec_path"],
            "plan_path": flow["plan_path"], "job_id": flow["job_id"], "finished": flow["finished"],
            "steps": [{"n": s["n"], "text": s["text"], "check": s["check"], "done": s["done"],
                       "running": bool(flow["job_id"]) and not s["done"] and s["text"] == flow["job_step"],
                       "failure": notes.get(s["text"], "") if not s["done"] else ""} for s in steps],
            "done": done, "total": len(steps), "next": nxt["text"] if nxt else "", "created": flow["created"],
            "summary": self.summary(flow, done, len(steps), nxt),
        }

    @staticmethod
    def summary(flow: dict, done: int, total: int, nxt: dict | None) -> str:
        if flow["stage"] == "brainstorm":
            if flow["ready"]:
                return "All the questions are answered. Ready to write the spec."
            return f"{len(flow['answers'])} of up to {MAX_QUESTIONS} questions answered."
        if flow["stage"] == "spec":
            return "The spec is written. Next: write the plan."
        if not total:
            return "The plan file has no checkbox steps yet."
        if flow["stage"] == "done" or (done == total):
            return f"{done} of {total} done. Finished."
        return f"{done} of {total} done, next: {nxt['text'] if nxt else ''}"

    # -- brainstorm
    def _ask_prompt(self, flow: dict) -> str:
        qa = "\n".join(f"Q{i}: {a['question']}\nA{i}: {a['answer']}" for i, a in enumerate(flow["answers"], 1)) or "(none yet)"
        return (f"The owner wants: {flow['goal']}\nFolder: {Path(flow['workspace']).name}\n\nAnswered so far:\n{qa}\n\n"
                f"Question number {len(flow['answers']) + 1} of at most {MAX_QUESTIONS}.")

    @staticmethod
    def _question(data: Any) -> dict | None:
        if not isinstance(data, dict):
            return None
        q = clean(data.get("question"), 300)
        if not q:
            return None
        options = _items(data.get("options"), 3, 80)
        for filler in ("Not sure yet", "Something else"):
            if len(options) < 2 and filler not in options:
                options.append(filler)
        return {"question": q, "options": options[:3]}

    async def next_question(self, flow: dict) -> dict:
        """Ask the AI for the next question (or 'ready'); saves and returns the flow."""
        if len(flow["answers"]) >= MAX_QUESTIONS:
            return self.store.update(flow["id"], ready=True, pending=None, updated=self.now())  # type: ignore[return-value]
        data = await self.complete(QUESTION_SYSTEM, self._ask_prompt(flow))
        if isinstance(data, dict) and data.get("ready") is True and flow["answers"]:
            return self.store.update(flow["id"], ready=True, pending=None, updated=self.now())  # type: ignore[return-value]
        question = self._question(data)
        if question is None:        # e.g. 'ready' before anything was answered: ask once more, firmly
            data = await self.complete(QUESTION_SYSTEM, self._ask_prompt(flow) + "\nYou MUST ask a question now.")
            question = self._question(data)
        if question is None:
            raise BuildError("The AI did not give a usable question. Try again.", 503)
        return self.store.update(flow["id"], pending=question, ready=False, updated=self.now())  # type: ignore[return-value]

    async def start(self, goal: Any, workspace: Any, title: Any = None) -> dict:
        self.require_on()
        text = clean(goal, MAX_GOAL, multiline=True)
        if len(text) < 3:
            raise BuildError("Say what you want built.")
        folder = resolve_workspace(self.config, workspace)
        if self.store.count() >= MAX_FLOWS:
            raise BuildError(f"There are {MAX_FLOWS} builds already. Delete some old ones first.", 409)
        name = clean(title, MAX_TITLE) if title else ""
        if not name:
            name = clean(text.split("\n", 1)[0], MAX_TITLE).rstrip(".!?")
        flow = self.store.add({"id": secrets.token_hex(6), "title": name or "New build", "goal": text,
                               "workspace": str(folder), "stage": "brainstorm", "answers": [], "pending": None,
                               "ready": False, "spec_path": "", "plan_path": "", "draft": None, "job_id": "",
                               "job_step": "", "notes": {}, "finished": False, "created": self.now(),
                               "updated": self.now()})
        try:
            flow = await self.next_question(flow)
        except BuildError:
            self.store.delete(flow["id"])
            raise
        self.ctx.audit("buildflow_start", flow=flow["id"], goal_chars=len(text))
        return self.view(flow)

    async def answer(self, flow_id: Any, answer: Any) -> dict:
        self.require_on()
        flow = self.get(flow_id)
        if flow["stage"] != "brainstorm" or flow["ready"] or not flow["pending"]:
            raise BuildError("There is no question waiting on this build.", 409)
        text = clean(answer, MAX_ANSWER, multiline=True)
        if not text:
            raise BuildError("Write an answer (or tap one of the suggestions).")
        answers = [*flow["answers"], {"question": flow["pending"]["question"], "answer": text}]
        flow = self.store.update(flow["id"], answers=answers, pending=None, updated=self.now())  # type: ignore[assignment]
        flow = await self.next_question(flow)
        return self.view(flow)

    # -- spec and plan (drafted, then written on approval)
    def _paths(self, flow: dict, folder: Path, kind: str) -> tuple[Path, str]:
        """(folder to write into, file stem) for 'spec' or 'plan'."""
        if kind == "spec":
            stem = f"{self.clock().strftime('%Y-%m-%d')}-{slugify(flow['title'])}"
            return folder / "docs" / "specs", stem
        return folder / "docs" / "plans", Path(flow["spec_path"]).stem

    def _preview(self, path: Path, folder: Path, text: str) -> dict:
        lines = text.split("\n")
        return {"path": str(path.relative_to(folder)).replace("\\", "/"), "first_lines": "\n".join(lines[:PREVIEW_LINES]),
                "total_lines": len(lines), "written": False}

    async def spec(self, flow_id: Any, preview: bool = False) -> dict:
        self.require_on()
        flow = self.get(flow_id)
        if not flow["answers"]:
            raise BuildError("Answer at least one question first, so the spec has something to say.", 409)
        if flow["spec_path"]:
            raise BuildError("The spec is already written: " + flow["spec_path"], 409)
        folder = self.workspace_dir(flow)
        draft = flow["draft"] if isinstance(flow["draft"], dict) and flow["draft"].get("kind") == "spec" else None
        if draft is None:
            qa = "\n".join(f"- {a['question']} -> {a['answer']}" for a in flow["answers"])
            data = await self.complete(SPEC_SYSTEM, f"Project: {flow['title']}\nGoal: {flow['goal']}\nAnswers:\n{qa}")
            if not isinstance(data, dict):
                raise BuildError("The AI did not give a usable spec. Try again.", 503)
            draft = {"kind": "spec", "text": render_spec(flow, data)}
            flow = self.store.update(flow["id"], draft=draft)  # type: ignore[assignment]
        where, stem = self._paths(flow, folder, "spec")
        if preview:
            return self._preview(where / f"{stem}.md", folder, draft["text"])
        path = create_new(where, stem, draft["text"])
        rel = str(path.relative_to(folder)).replace("\\", "/")
        flow = self.store.update(flow["id"], spec_path=rel, stage="spec", draft=None, updated=self.now())  # type: ignore[assignment]
        self.ctx.audit("buildflow_spec", flow=flow["id"], path=rel)
        out = self.view(flow)
        out["written"] = True
        return out

    async def plan(self, flow_id: Any, preview: bool = False) -> dict:
        self.require_on()
        flow = self.get(flow_id)
        if not flow["spec_path"]:
            raise BuildError("Write the spec first.", 409)
        if flow["plan_path"]:
            raise BuildError("The plan is already written: " + flow["plan_path"], 409)
        folder = self.workspace_dir(flow)
        try:
            spec_text = inside(folder, flow["spec_path"]).read_text(encoding="utf-8")[:12000]
        except (OSError, BuildError):
            raise BuildError("The spec file cannot be read any more. Check " + flow["spec_path"], 409) from None
        draft = flow["draft"] if isinstance(flow["draft"], dict) and flow["draft"].get("kind") == "plan" else None
        if draft is None:
            data = await self.complete(PLAN_SYSTEM, f"Project: {flow['title']}\n\nSpec file (the owner may have edited it):\n{spec_text}")
            raw = data.get("steps") if isinstance(data, dict) else None
            steps = []
            for item in (raw if isinstance(raw, list) else [])[:12]:
                if isinstance(item, dict):
                    text, check = clean(item.get("text"), 300), clean(item.get("check"), 240)
                elif isinstance(item, str):
                    text, check = clean(item, 300), ""
                else:
                    continue
                if text:
                    steps.append({"text": text, "check": check or "It works as the spec says."})
            if not steps:
                raise BuildError("The AI did not give a usable plan. Try again.", 503)
            draft = {"kind": "plan", "text": render_plan(flow, flow["spec_path"], steps)}
            flow = self.store.update(flow["id"], draft=draft)  # type: ignore[assignment]
        where, stem = self._paths(flow, folder, "plan")
        if preview:
            return self._preview(where / f"{stem}.md", folder, draft["text"])
        path = create_new(where, stem, draft["text"])
        rel = str(path.relative_to(folder)).replace("\\", "/")
        flow = self.store.update(flow["id"], plan_path=rel, stage="plan", draft=None, updated=self.now())  # type: ignore[assignment]
        self.ctx.audit("buildflow_plan", flow=flow["id"], path=rel)
        out = self.view(flow)
        out["written"] = True
        return out

    # -- building (the Council does the work)
    def task_text(self, flow: dict, folder: Path, step: dict, total: int) -> str:
        spec = inside(folder, flow["spec_path"])
        plan = inside(folder, flow["plan_path"])
        text = (f"Build step {step['n']} of {total} for the project \"{flow['title']}\".\n"
                f"Work inside this folder only: {folder}\n"
                f"Read the spec first: {spec}\nThe plan (do not tick its boxes yourself): {plan}\n\n"
                f"DO ONLY THIS STEP: {step['text']}\n")
        if step["check"]:
            text += f"It is done when: {step['check']}\n"
        return text[:3900]

    async def next_step(self, flow_id: Any, preview: bool = False) -> dict:
        self.require_on()
        async with self.lock:
            flow = await self.refresh(self.get(flow_id))
            if not flow["plan_path"]:
                raise BuildError("Write the plan first.", 409)
            if flow["job_id"]:
                raise BuildError(f"A step is already running (council run {flow['job_id']}). Look at it with build_status.", 409)
            steps = self.steps_of(flow)
            step = next((s for s in steps if not s["done"]), None)
            if step is None:
                raise BuildError("Every step in the plan is already ticked." if steps else "The plan file has no checkbox steps.", 409)
            folder = self.workspace_dir(flow)
            task = self.task_text(flow, folder, step, len(steps))
            if preview:
                return {"step": step["text"], "number": step["n"], "of": len(steps), "check": step["check"],
                        "task_chars": len(task), "runs": "the Council (free models) in the background"}
            ok, data = await self.call("POST", "/api/council/run", body={"task": task})
            if not ok:
                raise BuildError(str(data), 409)
            job_id = str((data or {}).get("id") or "")
            if not job_id:
                raise BuildError("The Council did not start a run.", 502)
            notes = dict(flow["notes"] or {})
            notes.pop(step["text"], None)
            flow = self.store.update(flow["id"], job_id=job_id, job_step=step["text"], stage="build", notes=notes,
                                     updated=self.now())  # type: ignore[assignment]
            self.ctx.audit("buildflow_next", flow=flow["id"], step=step["n"], job=job_id)
            out = self.view(flow)
            out["job_id"] = job_id
            return out

    async def refresh(self, flow: dict) -> dict:
        """Settle a finished council run (tick or record the reason) and notice a finished plan. Never raises."""
        try:
            if flow["job_id"]:
                ok, job = await self.call("GET", f"/api/council/jobs/{flow['job_id']}")
                if ok and isinstance(job, dict) and job.get("status") and job.get("status") != "running":
                    flow = self._settle(flow, job)
            steps = self.steps_of(flow)
            if (steps and all(s["done"] for s in steps) and not flow["finished"] and not flow["job_id"]
                    and flow["stage"] in ("plan", "build")):
                flow = self._finish(flow, len(steps))
        except Exception:  # noqa: BLE001 - a look must never fail because of a side effect
            log.exception("buildflow refresh failed")
        return flow

    def _settle(self, flow: dict, job: dict) -> dict:
        step_text = flow["job_step"]
        notes = dict(flow["notes"] or {})
        if job.get("status") == "accepted":
            try:
                folder = self.workspace_dir(flow)
                path = inside(folder, flow["plan_path"])
                text = path.read_text(encoding="utf-8")
                match = next((s for s in parse_plan(text) if s["text"] == step_text and not s["done"]), None)
                if match is not None:
                    write_atomic(path, tick_line(text, match["line"]))
            except (OSError, BuildError) as exc:
                notes[step_text] = clean(f"The step passed but the plan file could not be ticked ({exc}).", 200)
            else:
                notes.pop(step_text, None)
        else:
            result = job.get("result") or {}
            reason = result.get("stopped") or job.get("error") or result.get("answer") or ""
            notes[step_text] = clean(f"Council run {job.get('status')}: {reason}", 200)
        return self.store.update(flow["id"], job_id="", job_step="", notes=notes, updated=self.now())  # type: ignore[return-value]

    def _finish(self, flow: dict, count: int) -> dict:
        flow = self.store.update(flow["id"], finished=True, stage="done", updated=self.now())  # type: ignore[assignment]
        title = f"Build finished: {flow['title']} ({count} steps)"
        if brain_mod.enabled(self.config):
            try:
                body = (f"Built \"{flow['title']}\" in {flow['workspace']} with {count} plan steps.\n"
                        f"Goal: {flow['goal']}\nSpec: {flow['spec_path']}\nPlan: {flow['plan_path']}")
                brain_mod.store_for(self.config).add({
                    "kind": "decision", "title": clean(f"Built: {flow['title']}", 80),
                    "body": clean(body, 4000, multiline=True), "tags": ["buildflow"], "who": "",
                    "project": Path(flow["workspace"]).name[:60], "persona": "jarvis",
                    "created": self.now(), "remind_at": None, "reminder_id": None, "status": "open",
                    "source": "buildflow", "meta": ""})
            except Exception:  # noqa: BLE001
                log.exception("buildflow: brain save failed")
        try:
            proactive.submit_or_notify(self.ctx, {"type": "build_done", "title": title, "count": 1,
                                                  "body": flow["spec_path"], "source": "buildflow"})
        except Exception:  # noqa: BLE001
            log.exception("buildflow: proactive event failed")
        self.ctx.audit("buildflow_done", flow=flow["id"], steps=count)
        return flow

    async def status(self, flow_id: Any) -> dict:
        self.require_on()
        return self.view(await self.refresh(self.get(flow_id)))

    async def listing(self) -> list[dict]:
        out = []
        for flow in self.store.all():
            out.append(self.view(await self.refresh(flow)))
        return out

    def delete(self, flow_id: Any) -> dict:
        flow = self.get(flow_id)
        self.store.delete(flow["id"])
        self.ctx.audit("buildflow_delete", flow=flow["id"])
        return {"deleted": flow["id"]}

    def workspaces(self) -> list[dict]:
        return [{"name": n, "path": str(p)} for n, p in sorted(roots_of(self.config).items()) if p.is_dir()]

    async def sweep(self) -> None:
        for flow in self.store.all():
            if flow["job_id"]:
                await self.refresh(flow)


# ---- routes ------------------------------------------------------------------------------------------------------

class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(max_length=MAX_GOAL * 2)
    workspace: str = Field(max_length=1000)
    title: str | None = Field(default=None, max_length=200)


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(max_length=MAX_ANSWER * 2)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview: bool = False


def make_router(ctx: Ctx, *, ask: Ask | None = None, caller: Caller | None = None,
                clock: Callable[[], dt.datetime] | None = None) -> APIRouter:
    engine = BuildFlow(ctx, ask=ask, caller=caller, clock=clock)
    ctx.buildflow = engine        # type: ignore[attr-defined]
    _panic.register_flag(ctx, "Build flow", "buildflow_enabled", True)   # Panic pauses it, Resume restarts it (hub/panic.py)
    router = APIRouter(prefix="/api/buildflow", dependencies=[Depends(ctx.need_auth)])
    router.engine = engine        # type: ignore[attr-defined]

    async def guarded(work):
        try:
            return await work
        except BuildError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.get("")
    async def listing():
        return {"enabled": engine.enabled, "max_questions": MAX_QUESTIONS,
                "flows": await engine.listing() if engine.enabled else []}

    @router.post("")
    async def create(body: StartBody):
        return await guarded(engine.start(body.goal, body.workspace, body.title))

    @router.get("/workspaces")
    def workspaces():
        return {"workspaces": engine.workspaces()}

    @router.get("/{flow_id}")
    async def one(flow_id: str):
        return await guarded(engine.status(flow_id))

    @router.get("/{flow_id}/status")
    async def status(flow_id: str):
        return await guarded(engine.status(flow_id))

    @router.post("/{flow_id}/answer")
    async def answer(flow_id: str, body: AnswerBody):
        return await guarded(engine.answer(flow_id, body.answer))

    @router.post("/{flow_id}/spec")
    async def spec(flow_id: str, body: PreviewBody | None = None):
        return await guarded(engine.spec(flow_id, bool(body and body.preview)))

    @router.post("/{flow_id}/plan")
    async def plan(flow_id: str, body: PreviewBody | None = None):
        return await guarded(engine.plan(flow_id, bool(body and body.preview)))

    @router.post("/{flow_id}/next")
    async def next_step(flow_id: str, body: PreviewBody | None = None):
        return await guarded(engine.next_step(flow_id, bool(body and body.preview)))

    @router.delete("/{flow_id}")
    def delete(flow_id: str):
        try:
            return engine.delete(flow_id)
        except BuildError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    return router


async def start(ctx: Ctx):
    engine = getattr(ctx, "buildflow", None)
    if engine is None:
        return None

    async def loop():
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                if engine.enabled:
                    await engine.sweep()
            except Exception:  # noqa: BLE001
                log.exception("buildflow sweep failed")

    task = asyncio.get_running_loop().create_task(loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return stop
