"""Master resumes per role family, and the batch tailoring queue. See docs/api/resumes.md.

MASTER RESUMES. A master is one resume for a role family ("Backend", "Data", "Product"...), built ONLY from the owner's facts
(hub/factbase.py) with that family's skills and bullets first. Each time it is rebuilt it becomes a new numbered VERSION
("Backend v2"). A tailored resume (hub/tailor.py) records which master and version it came from in `variant.master` and
`variant.resume_version`; that label is exactly the text the job tracker groups its per-resume reply stats by
(`resume_version` in hub/jobtrack.py, up to 80 characters), and it is copied onto the application queue item when the
owner applies the tailored version, so the tracker's "what is working" table lists "Backend v2" against its replies.

BATCH TAILORING QUEUE. The owner picks several postings; the hub prepares a tailored resume, a cover note and an ATS score for
each, ONE AT A TIME in the background (a single worker, a small bounded queue, a pause between items), and the owner reviews
them one by one: accept, edit, or discard. Nothing is sent, applied or posted; accepting only writes the cover note into the
application's draft, as Tailor's own "use it" button does.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import ats, tailor
from . import factbase as fbmod
from .common import Ctx, JsonFile
from .factbase import FactBase

log = logging.getLogger("hub.resumes")

MAX_MASTERS = 12
MAX_VERSIONS = 20
MAX_PENDING = 12              # queued + being prepared, at any moment
MAX_PER_CALL = 8
MAX_STORED = 100
PAUSE_SECONDS = 0.3           # between two items: the worker never hogs the machine
TICK_SECONDS = 180            # the background loop only looks for left-over work (for example after a restart)
ITEM_TIMEOUT = 120
_LOCK = threading.RLock()

# Role families. `focus` words steer the ORDER and selection of the owner's own facts; they are never added to the resume.
FAMILIES = {
    "backend": ("Backend", "backend api apis python java node php services microservices database sql postgresql mysql redis queue scalability"),
    "frontend": ("Frontend", "frontend javascript typescript react vue angular html css responsive accessibility ui components"),
    "fullstack": ("Full stack", "fullstack frontend backend api javascript typescript react node python database sql deployment"),
    "data": ("Data", "data sql python pandas analytics etl pipeline dashboard tableau power bi reporting statistics warehouse"),
    "ml": ("Machine learning", "machine learning model models python pytorch tensorflow nlp training evaluation data features"),
    "devops": ("DevOps", "devops aws docker kubernetes terraform ci cd pipeline monitoring linux automation infrastructure"),
    "product": ("Product", "product roadmap users stakeholders requirements analytics metrics agile launch strategy research prioritisation"),
    "qa": ("QA and testing", "testing qa automation selenium pytest regression test cases quality bugs"),
    "mobile": ("Mobile", "mobile android ios kotlin swift flutter react native app store"),
    "ops": ("Operations", "operations process vendor supply logistics reporting excel compliance budget sop"),
}


class ResumeError(ValueError):
    pass


def enabled(config) -> bool:
    return getattr(config, "tailor_enabled", True) is not False


def label_of(master: dict, n: int) -> str:
    return f"{master['name']} v{n}"[:80]


def version_of(master: dict, n: Any = None) -> dict:
    """One version of a master (the current one when `n` is empty). Raises LookupError."""
    want = int(n) if str(n or "").isdigit() else master.get("current")
    for v in master.get("versions", []):
        if v["n"] == want:
            return v
    raise LookupError("No such version.")


def _focus_job(master: dict) -> dict:
    words = " ".join(master.get("focus") or [])
    return {"title": master["name"], "description": "Requirements:\n- experience with " + ", ".join(master.get("focus") or [master["name"]]) + "\n" + words}


def _fact_ids(resume: dict) -> list[str]:
    ids: list[str] = []
    for sec in resume.get("sections", []):
        for ln in sec["lines"]:
            ids += ln.get("fact_ids", [])
    ids += resume.get("header", {}).get("fact_ids", [])
    return list(dict.fromkeys(ids))


def facts_stamp(fb: FactBase) -> str:
    """A short fingerprint of the facts a resume can draw on, so 'the facts changed since this version' can be shown."""
    blob = json.dumps(sorted((f["id"], f["text"]) for f in fb.facts if f["type"] not in ("pref", "derived")))
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _core(resume: dict) -> str:
    return json.dumps([(s["name"], [(ln["text"], ln["style"]) for ln in s["lines"]]) for s in resume.get("sections", [])], sort_keys=True)


class Masters:
    def __init__(self, ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None, profile_fn=None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self.profile_fn = profile_fn
        self.file = JsonFile(Path(ctx.config.data_path()) / "resume_masters.json")

    def facts(self) -> FactBase:
        return tailor.Tailor(self.ctx, clock=self.clock, profile_fn=self.profile_fn).facts()

    def _all(self) -> list[dict]:
        with _LOCK:                       # every write holds this lock too, so a read never lands in the middle of a file swap
            data = self.file.load([])
        return data if isinstance(data, list) else []

    def list(self) -> list[dict]:
        return [self.public(m) for m in self._all()]

    def get(self, mid: str) -> dict:
        for m in self._all():
            if m["id"] == mid:
                return m
        raise LookupError("No such master resume.")

    def _put(self, master: dict) -> None:
        with _LOCK:
            rows = [m for m in self._all() if m["id"] != master["id"]] + [master]
            rows.sort(key=lambda m: m["created"])
            self.file.save(rows[:MAX_MASTERS])

    def public(self, m: dict, fb: FactBase | None = None) -> dict:
        cur = version_of(m)
        fb = fb or self.facts()
        return {"id": m["id"], "name": m["name"], "family": m["family"], "focus": m["focus"], "current": m["current"], "label": cur["label"],
                "created": m["created"], "updated": m.get("updated", m["created"]),
                "stale": cur.get("facts_stamp") != facts_stamp(fb),
                "versions": [{"n": v["n"], "label": v["label"], "created": v["created"], "note": v.get("note", ""), "lines": sum(len(s["lines"]) for s in v["resume"]["sections"])}
                             for v in reversed(m["versions"])]}

    def _version(self, m: dict, fb: FactBase, note: str) -> dict | None:
        resume = tailor.build_resume(fb, _focus_job(m))
        resume["target"] = ""
        resume["matched"] = []
        for sec in resume["sections"]:
            sec["lines"] = [ln for ln in sec["lines"] if ln.get("kind") != "glue"]      # "Seeking: <family>" is for a real posting, not a master
            for ln in sec["lines"]:
                skill_ids = [i for i in ln["fact_ids"] if fb.by_id.get(i, {}).get("type") == "skill"]
                if not skill_ids:
                    continue
                names = [fb.by_id[i]["text"] for i in skill_ids]                         # the owner's own spelling, not the focus words'
                if sec["name"] == "Skills":
                    ln["text"] = ", ".join(names)
                elif "skills include " in ln["text"]:
                    ln["text"] = ln["text"].split("skills include ")[0] + "skills include " + ", ".join(names) + "."
        resume["sections"] = [s for s in resume["sections"] if s["lines"]]
        last = m["versions"][-1] if m["versions"] else None
        if last and _core(last["resume"]) == _core(resume):
            return None
        n = (last["n"] if last else 0) + 1
        return {"n": n, "label": label_of(m, n), "created": self.clock().isoformat(timespec="seconds"), "note": fbmod.clean(note, 200),
                "fact_ids": _fact_ids(resume), "facts_stamp": facts_stamp(fb), "resume": resume}

    def create(self, name: str, family: str = "", focus: Any = None) -> dict:
        fam = FAMILIES.get(str(family or "").lower())
        name = fbmod.clean(name or (fam[0] if fam else ""), 40)
        if not name:
            raise ResumeError("Give the master resume a name, for example Backend.")
        words = [fbmod.clean(str(w), 30) for w in (focus if isinstance(focus, list) else re.split(r"[,\n;]", str(focus or ""))) if fbmod.clean(str(w), 30)]
        if not words and fam:
            words = fam[1].split()
        if not words:
            words = [w for w in re.findall(r"[A-Za-z0-9+#.]+", name)]
        if any(m["name"].lower() == name.lower() for m in self._all()):
            raise ResumeError("You already have a master resume with that name.")
        if len(self._all()) >= MAX_MASTERS:
            raise ResumeError(f"You can keep {MAX_MASTERS} master resumes. Delete one first.")
        fb = self.facts()
        if not fb.facts:
            raise ResumeError("There are no facts yet. Add your resume in Apps > Opportunities > Profile first.")
        now = self.clock().isoformat(timespec="seconds")
        m = {"id": "ms-" + secrets.token_hex(4), "name": name, "family": str(family or "").lower() if fam else "custom", "focus": words[:30],
             "created": now, "updated": now, "versions": [], "current": 1}
        v = self._version(m, fb, "first version")
        m["versions"].append(v)
        self._put(m)
        return self.public(m, fb)

    def update(self, mid: str, body: dict) -> dict:
        m = self.get(mid)
        if isinstance(body.get("name"), str) and body["name"].strip():
            name = fbmod.clean(body["name"], 40)
            if any(o["id"] != mid and o["name"].lower() == name.lower() for o in self._all()):
                raise ResumeError("You already have a master resume with that name.")
            m["name"] = name
        if isinstance(body.get("focus"), (list, str)):
            words = [fbmod.clean(str(w), 30) for w in (body["focus"] if isinstance(body["focus"], list) else re.split(r"[,\n;]", body["focus"])) if fbmod.clean(str(w), 30)]
            if words:
                m["focus"] = words[:30]
        m["updated"] = self.clock().isoformat(timespec="seconds")
        self._put(m)
        return self.public(m)

    def rebuild(self, mid: str, note: str = "") -> dict:
        """A new version from the facts as they are now. Unchanged content makes no new version."""
        m = self.get(mid)
        fb = self.facts()
        v = self._version(m, fb, note or "rebuilt from your facts")
        changed = v is not None
        if v is None:
            cur = version_of(m)
            cur["facts_stamp"] = facts_stamp(fb)         # the facts changed in ways that do not touch this resume: it is up to date
        else:
            m["versions"] = [*m["versions"], v][-MAX_VERSIONS:]
            m["current"] = v["n"]
        m["updated"] = self.clock().isoformat(timespec="seconds")
        self._put(m)
        return {**self.public(m, fb), "changed": changed}

    def use_version(self, mid: str, n: int) -> dict:
        m = self.get(mid)
        version_of(m, n)
        m["current"] = int(n)
        self._put(m)
        return self.public(m)

    def delete(self, mid: str) -> None:
        with _LOCK:
            rows = self._all()
            if not any(m["id"] == mid for m in rows):
                raise LookupError("No such master resume.")
            self.file.save([m for m in rows if m["id"] != mid])

    # -- choosing one for a posting ---------------------------------------------------------------------------
    def choose(self, wanted: Any, job: dict, fb: FactBase | None = None) -> dict | None:
        """`{id, name, version, label, stale, auto}` for a tailored resume to record, or None. `wanted`: a master id, 'none' (skip), or empty (pick the
        family whose focus words match the posting best; no match means none)."""
        rows = self._all()
        if not rows or str(wanted or "").lower() == "none":
            return None
        fb = fb or self.facts()
        if wanted:
            m = next((r for r in rows if r["id"] == str(wanted)), None)
            if m is None:
                raise LookupError("No such master resume.")
            auto = False
        else:
            text = f"{job.get('title', '')} {job.get('title', '')} {job.get('description', '')}".lower()
            toks = set(re.findall(r"[a-z0-9+#.]+", text))
            best, m = 0.0, None
            for r in rows:
                focus = {w.lower() for w in r["focus"]}
                hit = len(focus & toks) / max(len(focus), 1)
                title_hit = 0.5 if any(w in (job.get("title") or "").lower() for w in re.findall(r"[a-z]+", r["name"].lower()) if len(w) > 3) else 0.0
                if hit + title_hit > best:
                    best, m = hit + title_hit, r
            if m is None or best <= 0:
                return None
            auto = True
        cur = version_of(m)
        return {"id": m["id"], "name": m["name"], "version": cur["n"], "label": cur["label"], "stale": cur.get("facts_stamp") != facts_stamp(fb), "auto": auto}


# ---- the batch queue -------------------------------------------------------------------------------------------------------

class TailorQueue:
    def __init__(self, ctx: Ctx, *, clock=None, profile_fn=None, model_fn=None, applyq_fn=None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self.profile_fn, self.model_fn, self.applyq_fn = profile_fn, model_fn, applyq_fn
        self.file = JsonFile(Path(ctx.config.data_path()) / "resume_queue.json")
        self.tailor = tailor.Tailor(ctx, clock=self.clock, profile_fn=profile_fn, model_fn=model_fn, applyq_fn=applyq_fn)
        self.worker = threading.Lock()                  # held while ONE item is being prepared: never two at once
        self._task: asyncio.Task | None = None
        self.pause = PAUSE_SECONDS

    def _now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def _all(self) -> list[dict]:
        with _LOCK:                       # every write holds this lock too, so a read never lands in the middle of a file swap
            data = self.file.load([])
        return data if isinstance(data, list) else []

    def _save(self, rows: list[dict]) -> None:
        done = [r for r in rows if r["state"] in ("accepted", "discarded", "failed")]
        keep = [r for r in rows if r["state"] not in ("accepted", "discarded", "failed")] + done[-(MAX_STORED // 2):]
        keep.sort(key=lambda r: r["created"])
        self.file.save(keep[-MAX_STORED:])

    def list(self) -> list[dict]:
        return [self.light(r) for r in sorted(self._all(), key=lambda r: r["created"])]

    @staticmethod
    def light(r: dict) -> dict:
        return {k: r.get(k) for k in ("id", "application_id", "title", "company", "state", "created", "finished", "variant_id", "ats", "master", "error", "edited", "blocked")}

    def get(self, qid: str) -> dict:
        for r in self._all():
            if r["id"] == qid:
                return r
        raise LookupError("No such queue entry.")

    def _update(self, qid: str, **changes) -> dict:
        with _LOCK:
            rows = self._all()
            for r in rows:
                if r["id"] == qid:
                    r.update(changes)
                    self._save(rows)
                    return r
        raise LookupError("No such queue entry.")

    def requeue_stale(self) -> int:
        """After a restart an item left 'working' goes back to 'queued' (nothing else is lost)."""
        with _LOCK:
            rows = self._all()
            n = 0
            for r in rows:
                if r["state"] == "working" and not self.worker.locked():
                    r["state"] = "queued"
                    n += 1
            if n:
                self._save(rows)
            return n

    def pending(self) -> int:
        return sum(1 for r in self._all() if r["state"] in ("queued", "working"))

    # -- adding ----------------------------------------------------------------------------------------------
    def add(self, body: dict) -> dict:
        refs: list[dict] = []
        for aid in (body.get("application_ids") or [])[:MAX_PER_CALL] if isinstance(body.get("application_ids"), list) else []:
            if isinstance(aid, str) and aid.strip():
                refs.append({"application_id": aid.strip()})
        for j in (body.get("jobs") or [])[:MAX_PER_CALL] if isinstance(body.get("jobs"), list) else []:
            if isinstance(j, dict):
                refs.append({"job": j})
        if not refs:
            raise ResumeError("Pick one or more postings from your application queue, or paste a posting.")
        if len(refs) > MAX_PER_CALL:
            raise ResumeError(f"Add at most {MAX_PER_CALL} postings at a time.")
        added, skipped = [], []
        with _LOCK:
            rows = self._all()
            open_ids = {r.get("application_id") for r in rows if r["state"] in ("queued", "working", "ready") and r.get("application_id")}
            room = MAX_PENDING - sum(1 for r in rows if r["state"] in ("queued", "working"))
            for ref in refs:
                try:
                    if "application_id" in ref:
                        if ref["application_id"] in open_ids:
                            skipped.append({"ref": ref["application_id"], "reason": "already in the queue"})
                            continue
                        job, item = self.tailor.job_from_item(ref["application_id"])
                    else:
                        job, item = tailor.clean_job(ref["job"]), {}
                except (tailor.TailorError, LookupError) as exc:
                    skipped.append({"ref": str(ref.get("application_id") or "pasted posting")[:40], "reason": str(exc)})
                    continue
                if room <= 0:
                    skipped.append({"ref": job.get("title") or "posting", "reason": f"the queue holds {MAX_PENDING} waiting postings; try again when some are done"})
                    continue
                room -= 1
                entry = {"id": "rq-" + secrets.token_hex(4), "application_id": ref.get("application_id", ""), "job": job, "title": job.get("title") or "Posting",
                         "company": job.get("company", ""), "state": "queued", "created": self._now(), "finished": "", "variant_id": "", "ats": None,
                         "master": None, "error": "", "edited": False, "blocked": False,
                         "master_id": str(body.get("master_id") or "")[:40]}
                rows.append(entry)
                added.append(entry["id"])
            self._save(rows)
        return {"added": added, "skipped": skipped, "queue": self.list()}

    # -- working: exactly one at a time ------------------------------------------------------------------------
    def process_next(self) -> str | None:
        """Prepare the oldest queued posting. Returns its id, or None when nothing is queued or another item is being prepared."""
        if not self.worker.acquire(blocking=False):
            return None
        try:
            rows = [r for r in self._all() if r["state"] == "queued"]
            if not rows:
                return None
            entry = min(rows, key=lambda r: r["created"])
            self._update(entry["id"], state="working")
            try:
                body: dict = {"job": entry["job"], "ai": False}
                if entry.get("application_id"):
                    body["application_id"] = entry["application_id"]
                if entry.get("master_id"):
                    body["master_id"] = entry["master_id"]
                v = asyncio.run(asyncio.wait_for(self.tailor.preview(body), ITEM_TIMEOUT))
                fb = self.tailor.facts()
                s = ats.score(fb, entry["job"], ats.resume_text_of(v["resume"]), titles=ats.titles_of(fb, v["resume"]))
                self._update(entry["id"], state="ready", variant_id=v["id"], finished=self._now(), blocked=bool(v.get("blocked")),
                             master=v.get("master"), ats={"score": s["score"], "label": s["label"]}, error="")
            except Exception as exc:  # noqa: BLE001 - one bad posting must not stop the others
                log.warning("resume queue: %s failed (%s)", entry["id"], type(exc).__name__)
                self._update(entry["id"], state="failed", finished=self._now(), error=fbmod.clean(str(exc) or type(exc).__name__, 200))
            return entry["id"]
        finally:
            self.worker.release()

    async def drain(self) -> None:
        """Work through the queue, one item at a time, with a short pause between items. Runs in a thread per item, never on the event loop."""
        while True:
            done = await asyncio.to_thread(self.process_next)
            if done is None:
                return
            await asyncio.sleep(self.pause)

    def kick(self) -> bool:
        """Start the single background worker if it is not running. Returns whether it started now."""
        if self._task is not None and not self._task.done():
            return False
        try:
            self._task = asyncio.get_running_loop().create_task(self.drain())
        except RuntimeError:
            return False
        return True

    # -- the owner's decisions ---------------------------------------------------------------------------------
    def review(self, qid: str) -> dict:
        """The queue entry with its tailored resume, cover note and ATS score, for the screen. One call."""
        r = self.get(qid)
        out = self.light(r)
        out["job"] = {k: r["job"].get(k, "") for k in ("title", "company", "url")}
        if r.get("variant_id") and r["state"] in ("ready", "accepted"):
            try:
                v = self.tailor.get(r["variant_id"])
            except LookupError:
                return out
            fb = self.tailor.facts()
            out["variant"] = tailor.public_variant(v)
            out["resume_text"] = tailor.to_text(v["resume"])
            out["cover_text"] = tailor.cover_text(v["cover"], with_subject=True)
            out["ats_detail"] = ats.score(fb, r["job"], ats.resume_text_of(v["resume"]), titles=ats.titles_of(fb, v["resume"]))
        return out

    def accept(self, qid: str) -> dict:
        r = self.get(qid)
        if r["state"] != "ready":
            raise ResumeError("Only a prepared posting can be accepted.")
        v = self.tailor.get(r["variant_id"])
        b = tailor.blockers(v)
        if b:
            raise ResumeError("Not yet: " + " ".join(b) + " Open it in Tailor to fix it, or tap Edit here.")
        applied = None
        if r.get("application_id"):
            applied = self.tailor.apply_to_application(r["variant_id"], r["application_id"])
        self._update(qid, state="accepted", finished=self._now())
        return {"state": "accepted", "applied_to_queue": bool(applied), "resume_version": v.get("resume_version", ""),
                "message": "Saved. The cover note is in the application's draft." if applied else "Saved. It is not tied to an application yet; use the application packet to queue it.",
                "nothing_sent": True}

    def discard(self, qid: str) -> dict:
        r = self.get(qid)
        if r.get("variant_id"):
            try:
                self.tailor.delete(r["variant_id"])
            except LookupError:
                pass
        self._update(qid, state="discarded", finished=self._now(), variant_id="")
        return {"state": "discarded"}

    def edit(self, qid: str, body: dict) -> dict:
        """Change one cover-note sentence (or fill a blank) through Tailor's own resolver; the honesty check runs again."""
        r = self.get(qid)
        if r["state"] != "ready":
            raise ResumeError("Only a prepared posting can be edited.")
        res = {k: body[k] for k in ("action", "sentence", "text", "label", "value", "fact_text") if k in body}
        v = self.tailor.resolve(r["variant_id"], res)
        fb = self.tailor.facts()
        s = ats.score(fb, r["job"], ats.resume_text_of(v["resume"]), titles=ats.titles_of(fb, v["resume"]))
        self._update(qid, edited=True, blocked=bool(v.get("blocked")), ats={"score": s["score"], "label": s["label"]})
        return self.review(qid)

    def clear_done(self) -> int:
        with _LOCK:
            rows = self._all()
            keep = [r for r in rows if r["state"] in ("queued", "working", "ready")]
            self.file.save(keep)
            return len(rows) - len(keep)


# ---- routes ------------------------------------------------------------------------------------------------------------------

_QUEUES: dict[str, TailorQueue] = {}


def queue_for(ctx: Ctx, **kw) -> TailorQueue:
    key = str(ctx.config.data_path())
    q = _QUEUES.get(key)
    if q is None or kw:
        q = _QUEUES[key] = TailorQueue(ctx, **kw)
    return q


def make_router(ctx: Ctx, *, clock=None, profile_fn=None, model_fn=None, applyq_fn=None, **_: Any) -> APIRouter:
    router = APIRouter(prefix="/api/resumes", dependencies=[Depends(ctx.need_auth)])
    masters = Masters(ctx, clock=clock, profile_fn=profile_fn)
    queue = queue_for(ctx, clock=clock, profile_fn=profile_fn, model_fn=model_fn, applyq_fn=applyq_fn)
    router.masters, router.queue = masters, queue          # type: ignore[attr-defined]

    def on() -> None:
        if not enabled(ctx.config):
            raise HTTPException(404, "Resume tools are switched off (tailor_enabled is false).")

    def guard(fn):
        try:
            return fn()
        except (ResumeError, tailor.TailorError) as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    async def run(fn):
        return await asyncio.to_thread(lambda: guard(fn))

    @router.get("/overview")
    async def overview():
        """One call for the screen: masters, the queue (light), the families on offer."""
        on()

        def go():
            fb = masters.facts()
            return {"masters": [masters.public(m, fb) for m in masters._all()], "queue": queue.list(), "pending": queue.pending(), "max_pending": MAX_PENDING,
                    "families": [{"key": k, "name": v[0]} for k, v in FAMILIES.items()], "facts": len(fb.facts)}
        return await run(go)

    @router.post("/masters")
    async def create(body: dict = Body(default_factory=dict)):
        on()
        out = await run(lambda: masters.create(str(body.get("name") or ""), str(body.get("family") or ""), body.get("focus")))
        ctx.audit("resume_master_created", id=out["id"])
        return out

    @router.put("/masters/{mid}")
    async def update(mid: str, body: dict = Body(default_factory=dict)):
        on()
        return await run(lambda: masters.update(mid, body))

    @router.post("/masters/{mid}/rebuild")
    async def rebuild(mid: str, body: dict = Body(default_factory=dict)):
        on()
        return await run(lambda: masters.rebuild(mid, str(body.get("note") or "")))

    @router.post("/masters/{mid}/use/{n}")
    async def use(mid: str, n: int):
        on()
        return await run(lambda: masters.use_version(mid, n))

    @router.get("/masters/{mid}/versions/{n}")
    async def version(mid: str, n: int):
        on()

        def go():
            m = masters.get(mid)
            v = version_of(m, n)
            return {"id": mid, "n": v["n"], "label": v["label"], "text": tailor.to_text(v["resume"]), "created": v["created"], "note": v.get("note", "")}
        return await run(go)

    @router.delete("/masters/{mid}")
    async def drop(mid: str):
        on()
        await run(lambda: masters.delete(mid))
        return {"ok": True}

    @router.post("/queue")
    async def enqueue(body: dict = Body(default_factory=dict)):
        """{application_ids?: [...], jobs?: [{title, description, ...}], master_id?}. Starts the one background worker; returns at once."""
        on()
        out = await run(lambda: queue.add(body))
        if out["added"]:
            queue.kick()
        ctx.audit("resume_queue_add", count=len(out["added"]))
        return out

    @router.get("/queue")
    async def queue_list():
        on()
        return {"queue": await run(queue.list), "pending": queue.pending(), "max_pending": MAX_PENDING}

    @router.get("/queue/{qid}")
    async def queue_review(qid: str):
        on()
        return await run(lambda: queue.review(qid))

    @router.post("/queue/{qid}/accept")
    async def queue_accept(qid: str):
        on()
        out = await run(lambda: queue.accept(qid))
        ctx.audit("resume_queue_accept", id=qid)
        return out

    @router.post("/queue/{qid}/discard")
    async def queue_discard(qid: str):
        on()
        return await run(lambda: queue.discard(qid))

    @router.post("/queue/{qid}/edit")
    async def queue_edit(qid: str, body: dict = Body(default_factory=dict)):
        on()
        return await run(lambda: queue.edit(qid, body))

    @router.post("/queue/clear")
    async def queue_clear():
        on()
        return {"removed": await run(queue.clear_done)}

    return router


async def start(ctx: Ctx):
    """A slow check (every few minutes) for queued work left over, for example after a restart. It only starts the single worker."""
    queue = queue_for(ctx)
    try:
        await asyncio.to_thread(queue.requeue_stale)
    except Exception:  # noqa: BLE001
        log.warning("resume queue: could not requeue", exc_info=True)

    async def loop():
        while True:
            try:
                if enabled(ctx.config) and await asyncio.to_thread(queue.pending):
                    queue.kick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("resume queue loop error", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)
    task = asyncio.get_running_loop().create_task(loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return stop
