"""FREELANCE PROPOSALS: a proposal draft for a freelance or task posting, built only from facts the owner confirmed, with three
opening lines to choose from, and a small A/B record of which opening won. See docs/api/proposals.md.

COPY ONLY. There is no send, submit or apply route and no platform is ever automated: the owner copies the text and submits it on the
platform by hand (the platforms' own rules forbid bots). The pieces are: a hook (the opening line), relevant proof (the owner's own bullets
that match the posting, each backed by a fact id and checked with `fitcheck.check_claims`), an approach (fixed steps that name the posting's own
words), a timeline and a rate (the owner's own numbers, or a [FILL: ...] blank that blocks Copy), and one question for the client.
The owner then records which opening they used and whether the bid won, lost or got no reply; `stats()` shows which openings win more,
and says plainly when there are too few bids to tell. Data: `proposals.json` in the data folder.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import factbase as fbmod
from . import fitcheck
from . import interviews as iv
from . import tailor as tl
from .assistant import ExtraTool, mark_tools
from .common import Ctx, InProcessClient, JsonFile
from .factbase import FactBase, clean, job_terms

log = logging.getLogger("hub.proposals")

from . import assistant as _assistant  # noqa: E402
_assistant.TOOL_GROUPS["proposals"] = (
    {"proposal_stats"},
    re.compile(r"\bproposal (?:stats|win rate|openings?)\b|\bwhich (?:proposal )?opening\b|\bfreelance (?:bids?|proposals?) (?:win|won|stats)\b", re.I))

MAX_PROPOSALS = 400
MIN_BIDS = 5                      # fewer used bids than this per opening: "too few to tell"
OPENINGS = ("proof", "problem", "question", "direct")
OPENING_LABEL = {"proof": "Proof first", "problem": "The job first", "question": "A question first", "direct": "Short and direct"}
OUTCOMES = ("won", "lost", "no_reply")
_LOCK = threading.RLock()
STATIC = ["Hi", "Thank you for reading", "Here is how I would do it", "Step one", "Step two", "Step three", "Before I quote",
          "confirm what done looks like", "and hand it over", "Could you tell me"]


class ProposalError(ValueError):
    """A plain sentence for the owner."""


def enabled(config) -> bool:
    return getattr(config, "proposals_enabled", True) is not False


def _short_title(title: str) -> str:
    t = re.sub(r"\s*[\[(].*?[\])]\s*", " ", title or "").strip(" -:")
    return " ".join(t.split()[:9]) or "this job"


def client_questions(job: dict) -> list[str]:
    """Questions that fill a gap in the posting: no budget, no deadline, no definition of done."""
    text = f"{job.get('title', '')}\n{job.get('description', '')}".lower()
    qs = []
    if not re.search(r"\b(?:budget|fixed price|\$\s?\d|per hour|/hr|hourly rate|rate)\b", text) and not job.get("pay"):
        qs.append("What budget range do you have in mind?")
    if not re.search(r"\b(?:deadline|by (?:the )?end of|within \d+|asap|urgent|due)\b", text):
        qs.append("When do you need this finished by?")
    qs.append("What would a finished result look like for you?")
    return qs[:3]


def build_proposal(fb: FactBase, job: dict, own: dict) -> dict:
    """`{text, blocked, blockers, openings[{key,label,text}], used_opening, sentences, fact_ids, words}`. `own` holds the owner's typed `rate`,
    `currency`, `unit`, `timeline`, `name`. Plain code only: the same posting and facts give the same draft."""
    title, desc = job.get("title", ""), job.get("description", "")
    short = _short_title(title)
    terms = job_terms(job, fb)
    skills = iv._skill_hits(desc, title)[:3]
    qs = client_questions(job)
    seq = [0]

    def sent(text, ids=(), kind="claim", optional=False, key=""):
        seq[0] += 1
        return {"id": f"s{seq[0]}", "text": text, "fact_ids": list(ids), "kind": kind, "src": "template", "status": "ok", "optional": optional, "key": key}

    bullets = tl._rank_bullets(fb, terms)
    proof = []
    for b in bullets[:2]:
        text, ids = tl.first_person(fb, b)
        proof.append(sent(text, ids, "claim"))
    # three alternative opening lines, each one sentence
    openings: dict[str, list[dict]] = {}
    if proof:
        openings["proof"] = [sent(proof[0]["text"], proof[0]["fact_ids"], "claim"), sent(f"That is close to what you describe in \"{short}\".", [], "glue")]
    openings["problem"] = [sent(f"Your post asks for {short}" + (f", and I would start by confirming the {skills[0]} side of it." if skills else ", and I would start by confirming exactly what done looks like."),
                                [], "glue")]
    openings["question"] = [sent(f"Before I quote: {qs[0]}", [], "glue")]
    openings["direct"] = [sent(f"Hi, I read your post for {short} and I would like to help with it.", [], "glue")]
    order = [k for k in ("proof", "problem", "question", "direct") if k in openings]
    chosen = order[:3]                                   # the first three are offered; the first is the default
    steps = [f"Step one: confirm the scope and what done looks like" + (f", starting with {skills[0]}" if skills else "") + ".",
             "Step two: build it in small pieces and show you each one." if not skills[1:] else f"Step two: build it in small pieces ({', '.join(skills[:3])}) and show you each one.",
             "Step three: test it, fix what is found and hand it over."]
    approach = [sent(s, [], "plan") for s in steps]
    timeline = sent(f"Timeline: {own['timeline']}." if own.get("timeline") else "Timeline: [FILL: how long it will take you].", [], "glue")
    rate_txt = ""
    if own.get("rate"):
        rate_txt = f"{own.get('currency') or ''} {float(own['rate']):,.0f}".strip() + (f" {own['unit']}" if own.get("unit") else "")
    rate = sent(f"Rate: {rate_txt}." if rate_txt else "Rate: [FILL: your rate and how it is charged].", [], "glue")
    def ask_for(opening_key: str) -> dict | None:
        pick = qs[1:] if opening_key == "question" else qs
        return sent(f"One question: {pick[0]}", [], "glue") if pick else None

    greeting = "Hi,"
    closing = "Thank you for reading."
    sign = own.get("name") or fb.name() or ""

    def assemble(opening_key: str) -> tuple[list[dict], str]:
        op = openings[opening_key]
        body = [*op] + [p for p in proof if p["fact_ids"] != op[0]["fact_ids"]]
        ask = ask_for(opening_key)
        sents = [*body, *approach, timeline, rate, *([ask] if ask else [])]
        parts = [greeting, " ".join(s["text"] for s in body), "Here is how I would do it:\n" + "\n".join("- " + s["text"] for s in approach),
                 timeline["text"] + " " + rate["text"]]
        if ask:
            parts.append(ask["text"])
        parts.append(closing + (f"\n{sign}" if sign else ""))
        return sents, "\n\n".join(parts)

    # Fixed wording says nothing about the owner, and the rate and timeline are the owner's own typed values: all of it is "free" for the honesty check.
    free = [fb.name(), *STATIC, short, *skills, greeting, closing, *steps, *qs, *(x["text"] for lst in openings.values() for x in lst if x["kind"] == "glue"),
            own.get("timeline", ""), rate_txt, f"Rate: {rate_txt}"]
    alts, first_sents = [], []
    for k in chosen:
        sents, t = assemble(k)
        res = fitcheck.check_claims(fb, sents, job, free)
        bad = {u["index"]: u["reason"] for u in res["unbacked"]}
        for i, s in enumerate(sents):
            s["status"], s["reason"] = ("unbacked", bad[i]) if i in bad else ("ok", "")
        fills = sorted(set(re.findall(r"\[FILL: [^\]]+\]", t)))
        blockers = [f"Not backed by your facts: {sents[i]['text'][:80]}" for i in sorted(bad)] + [f"Fill in: {f}" for f in fills]
        alts.append({"key": k, "label": OPENING_LABEL[k], "line": " ".join(x["text"] for x in openings[k]), "text": t, "fact_ids": sorted({i for x in openings[k] for i in x["fact_ids"]}),
                     "blocked": bool(blockers), "blockers": blockers})
        if not first_sents:
            first_sents = sents
    top = alts[0]
    return {"text": top["text"], "blocked": top["blocked"], "blockers": top["blockers"], "openings": alts, "opening": chosen[0], "sentences": first_sents,
            "fact_ids": sorted({i for s in first_sents for i in s["fact_ids"]}), "words": len(top["text"].split()), "title": title, "question": qs[0],
            "no_facts": not fb.facts,
            "note": "Draft only. Read it, change it, then paste it on the platform yourself. The hub never submits anything."}


def win_stats(rows: list[dict]) -> dict:
    """Per opening: how many bids used it, how many won, lost or got no reply, and the win rate (only where there are enough bids to mean something)."""
    stats = {}
    for k in OPENINGS:
        used = [r for r in rows if r.get("used") and r.get("opening") == k]
        decided = [r for r in used if r.get("outcome") in OUTCOMES]
        won = sum(1 for r in decided if r["outcome"] == "won")
        stats[k] = {"key": k, "label": OPENING_LABEL[k], "used": len(used), "decided": len(decided), "won": won,
                    "lost": sum(1 for r in decided if r["outcome"] == "lost"), "no_reply": sum(1 for r in decided if r["outcome"] == "no_reply"),
                    "win_rate": round(100 * won / len(decided)) if decided else None, "enough": len(decided) >= MIN_BIDS}
    ranked = [s for s in stats.values() if s["enough"]]
    best = max(ranked, key=lambda s: (s["win_rate"], s["decided"]), default=None) if len(ranked) >= 2 else None
    if best and sum(1 for s in ranked if s["win_rate"] == best["win_rate"]) > 1:
        best = None
    total = sum(s["decided"] for s in stats.values())
    if best:
        verdict = f"\"{best['label']}\" wins most so far ({best['win_rate']} percent of {best['decided']} bids). Keep testing: small numbers can mislead."
    elif total < MIN_BIDS * 2:
        verdict = f"Too few bids to tell. Record the opening you use and the result; each opening needs {MIN_BIDS} decided bids before a rate is compared."
    else:
        verdict = "No opening is clearly ahead yet."
    return {"openings": list(stats.values()), "decided_total": total, "verdict": verdict, "min_bids": MIN_BIDS}


class Proposals:
    def __init__(self, ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None, profile_fn=None, model_fn=None, applyq_fn=None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self.tailor = tl.Tailor(ctx, clock=self.clock, profile_fn=profile_fn, model_fn=model_fn, applyq_fn=applyq_fn)
        base = Path(ctx.config.data_path())
        self.file = JsonFile(base / "proposals.json")
        self.items_file = JsonFile(base / "oppsources_items.json")

    def _rows(self) -> list[dict]:
        d = self.file.load({})
        rows = d.get("proposals", []) if isinstance(d, dict) else []
        return [r for r in rows if isinstance(r, dict) and r.get("id")]

    def _save(self, rows: list[dict]) -> None:
        self.file.save({"proposals": rows[-MAX_PROPOSALS:]})

    def now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def tasks(self) -> list[dict]:
        """Freelance and task items already in the hub (a read of the sources file; the sources screen owns them)."""
        d = self.items_file.load({})
        out = []
        for i in (d.values() if isinstance(d, dict) else []):
            if isinstance(i, dict) and i.get("status") in ("new", "blocked", None) and (i.get("kind") in ("freelance", "llm-task") or i.get("employment") in ("freelance", "task-based")):
                out.append({"id": i.get("id", ""), "title": i.get("title", ""), "company": i.get("company", ""), "source": i.get("source", ""), "pay": i.get("pay_label", "")})
        return out[:40]

    def job(self, body: dict) -> dict:
        if body.get("item_id"):
            it = next((i for i in (self.items_file.load({}) or {}).values() if isinstance(i, dict) and i.get("id") == body["item_id"]), None) \
                if isinstance(self.items_file.load({}), dict) else None
            if not it:
                raise LookupError("No such item in the hub.")
            return {**tl.clean_job({"title": it.get("title"), "company": it.get("company"), "url": it.get("url"), "description": it.get("description")}),
                    "pay": it.get("pay"), "item_id": it["id"], "platform": oppsources_host(it.get("url", ""))}
        raw = {k: body.get(k) for k in ("title", "company", "description", "url") if isinstance(body.get(k), str)}
        if not (raw.get("title") or raw.get("description")):
            raise ProposalError("Pick a task from the list, or type the title and paste the description.")
        return {**tl.clean_job(raw), "platform": oppsources_host(raw.get("url", ""))}

    def draft(self, body: dict) -> dict:
        job = self.job(body)
        fb = self.tailor.facts()
        own = {}
        if body.get("rate") not in (None, ""):
            try:
                own["rate"] = float(str(body["rate"]).replace(",", ""))
            except ValueError:
                raise ProposalError("The rate must be a number.") from None
            if not 0 < own["rate"] <= 1e9:
                raise ProposalError("The rate must be above 0.")
            own["currency"] = clean(str(body.get("currency") or ""), 3).upper()
            own["unit"] = clean(str(body.get("unit") or ""), 20)
        own["timeline"] = clean(str(body.get("timeline") or ""), 80)
        own["name"] = clean(str(body.get("name") or ""), 60)
        res = build_proposal(fb, job, own)
        rec = {"id": "pr-" + secrets.token_hex(4), "item_id": job.get("item_id", ""), "title": job.get("title", ""), "company": job.get("company", ""),
               "platform": job.get("platform", ""), "opening": res["opening"], "openings": [{k: o[k] for k in ("key", "label", "line")} for o in res["openings"]],
               "text": res["text"], "created": self.now(), "used": False, "used_at": "", "outcome": "", "outcome_at": "", "note": ""}
        with _LOCK:
            rows = self._rows()
            rows.append(rec)
            self._save(rows)
        return {**res, "id": rec["id"]}

    def list(self) -> list[dict]:
        return sorted(self._rows(), key=lambda r: r["created"], reverse=True)

    def _mutate(self, pid: str, fn) -> dict:
        with _LOCK:
            rows = self._rows()
            for r in rows:
                if r["id"] == pid:
                    fn(r)
                    self._save(rows)
                    return r
        raise LookupError("No such proposal.")

    def mark_used(self, pid: str, body: dict) -> dict:
        """The owner says: I pasted this on the platform, with this opening. Recording it sends nothing."""
        def apply(r):
            key = body.get("opening") or r["opening"]
            if key not in OPENINGS or key not in [o["key"] for o in r["openings"]]:
                raise ProposalError("Pick one of the openings from this draft.")
            r.update(used=True, opening=key, used_at=self.now()[:10], note=clean(str(body.get("note") or r.get("note") or ""), 200))
        return self._mutate(pid, apply)

    def outcome(self, pid: str, body: dict) -> dict:
        if body.get("outcome") not in OUTCOMES:
            raise ProposalError("Outcome must be won, lost or no_reply.")

        def apply(r):
            if not r.get("used"):
                raise ProposalError("Mark the proposal as used first: say which opening you pasted.")
            r.update(outcome=body["outcome"], outcome_at=self.now()[:10])
        return self._mutate(pid, apply)

    def delete(self, pid: str) -> None:
        with _LOCK:
            rows = self._rows()
            if not any(r["id"] == pid for r in rows):
                raise LookupError("No such proposal.")
            self._save([r for r in rows if r["id"] != pid])

    def stats(self) -> dict:
        return win_stats(self._rows())

    def overview(self) -> dict:
        return {"tasks": self.tasks(), "proposals": self.list()[:40], "stats": self.stats(), "openings": [{"key": k, "label": OPENING_LABEL[k]} for k in OPENINGS],
                "settings": {"enabled": enabled(self.ctx.config)}, "sends": False}


def oppsources_host(url: str) -> str:
    from . import oppsources
    return oppsources._host(url or "")


def make_router(ctx: Ctx, *, clock=None, profile_fn=None, model_fn=None, applyq_fn=None, **_: Any) -> APIRouter:
    router = APIRouter(prefix="/api/proposals", dependencies=[Depends(ctx.need_auth)])
    svc = Proposals(ctx, clock=clock, profile_fn=profile_fn, model_fn=model_fn, applyq_fn=applyq_fn)
    router.service = svc                                      # type: ignore[attr-defined]

    def on() -> None:
        if not enabled(ctx.config):
            raise HTTPException(404, "Proposals are switched off (proposals_enabled is false).")

    def guard(fn):
        try:
            return fn()
        except (ProposalError, tl.TailorError) as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("")
    async def overview():
        on()
        return await asyncio.to_thread(svc.overview)

    @router.get("/stats")
    async def stats():
        on()
        return svc.stats()

    @router.post("/draft")
    async def draft(body: dict = Body(default_factory=dict)):
        on()
        return await asyncio.to_thread(lambda: guard(lambda: svc.draft(body)))

    @router.post("/{pid}/used")
    async def used(pid: str, body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.mark_used(pid, body))

    @router.post("/{pid}/outcome")
    async def outcome(pid: str, body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.outcome(pid, body))

    @router.delete("/{pid}")
    async def drop(pid: str):
        on()
        guard(lambda: svc.delete(pid))
        return {"ok": True}

    return router


def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    async def proposal_stats(a):
        ok, d = await hub.call("GET", "/api/proposals/stats")
        if not ok:
            return False, d
        lines = [d["verdict"]] + [f"- {o['label']}: used {o['used']}, decided {o['decided']}, won {o['won']}" + (f" ({o['win_rate']} percent)" if o["win_rate"] is not None else "") for o in d["openings"]]
        lines.append("Read-only. Proposals are copied and submitted by the owner by hand.")
        return True, "\n".join(lines)

    spec = {"name": "proposal_stats", "description": "Which freelance proposal opening lines won more bids, from the owner's own records. Read-only; never submits anything.",
            "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}
    return mark_tools([ExtraTool(spec, proposal_stats)])
