"""One-page application packet for ONE posting. See docs/api/packet.md.

A packet puts everything the owner needs to decide on one screen: the tailored resume, the cover note, the ATS score, answers from
the answer bank for the form's questions, a short company summary taken from the posting text, the pay range if the posting shows
one, and links. The owner reviews it once, then either puts it into the application queue (hub/applyq.py) with one tap, or
copies / downloads it. A packet NEVER submits, sends or posts anything: "put in the queue" only writes a draft into the queue,
where sending still needs the owner's own tap. Everything is built from the owner's facts (hub/factbase.py) and the posting text;
no outside call is made, and an AI model is used only when the owner has switched tailoring AI on (same rule as Tailor).
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
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from . import answerbank, ats, fitcheck, tailor
from . import factbase as fbmod
from .assistant import ExtraTool, mark_tools
from .common import Ctx, InProcessClient, JsonFile

log = logging.getLogger("hub.packet")

MAX_PACKETS = 40
MAX_QUESTIONS = 20
_LOCK = threading.RLock()
_ABOUT = re.compile(r"^(?:about (?:us|the company|the team|[\w .&'-]{2,40})|who we are|our (?:mission|story)|company overview|overview)\s*:?\s*$", re.I)
_QUESTION = re.compile(r"(?:^|\s)(?:please (?:describe|explain|tell us|share|state|list|provide)|tell us (?:about|why|how)|describe (?:a|your|how)|why (?:do|are|would) you)\b", re.I)
_LPA = re.compile(r"(?:₹|inr|rs\.?)?\s?\d{1,3}(?:\.\d+)?\s?(?:-|–|to)\s?\d{1,3}(?:\.\d+)?\s?(?:lpa|lakhs?|lacs?|l\b|ctc)|\b\d{1,3}(?:\.\d+)?\s?(?:lpa|lakhs? per annum)\b", re.I)


class PacketError(ValueError):
    pass


def enabled(config) -> bool:
    return getattr(config, "tailor_enabled", True) is not False


# ---- pieces built from the posting text (plain code) -----------------------------------------------------------------------

def company_summary(job: dict) -> dict:
    """A short summary taken from the posting itself: the 'About us' part when there is one, else the opening sentences. Nothing is looked up."""
    desc = str(job.get("description") or "")
    lines = [ln.strip() for ln in desc.replace("\r", "").split("\n")]
    picked: list[str] = []
    grab = False
    for ln in lines:
        if not ln:
            if picked and grab:
                break
            continue
        if _ABOUT.match(re.sub(r"[:\-–—*#_]+\s*$", "", ln).strip()):
            grab = True
            continue
        if grab:
            picked.append(ln)
            if len(" ".join(picked)) > 380:
                break
    text = " ".join(picked)
    source = "the 'About' part of the posting"
    if not text:
        flat = " ".join(ln for ln in lines if ln and not fitcheck._BULLET.match(ln))
        text = " ".join(re.split(r"(?<=[.!?])\s+", flat)[:2])
        source = "the opening of the posting"
    text = fbmod.clean(text, 420)
    url = str(job.get("url") or "")
    host = urlsplit(url).hostname if re.match(r"https?://", url, re.I) else ""
    email = str(job.get("contact_email") or "")
    return {"company": job.get("company", ""), "text": text, "source": source if text else "", "site": host or "", "email_domain": email.split("@")[-1] if "@" in email else "",
            "note": "Taken from the posting text only. Nothing was looked up." if text else "The posting has no company description."}


def pay_range(job: dict) -> dict:
    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    lines, seen = [], set()
    for ln in re.split(r"\r?\n|(?<=[.!?])\s+", text):
        ln = ln.strip()
        if not ln or len(ln) > 400:
            continue
        if fitcheck.pay_amounts(ln) or _LPA.search(ln):
            snip = fbmod.clean(ln, 160)
            if snip.lower() not in seen:
                seen.add(snip.lower())
                lines.append(snip)
    amounts = [{"top": a, "per": per} for a, per in fitcheck.pay_amounts(text)][:4]
    return {"found": bool(lines), "lines": lines[:4], "amounts": amounts,
            "note": "As written in the posting. Currencies are not converted." if lines else "The posting shows no pay."}


def detect_questions(job: dict, extra: list[str] | None = None) -> list[str]:
    """The form's questions: the owner's list, questions written in the posting, and the standard ones the posting hints at."""
    out: list[str] = []
    for q in extra or []:
        q = fbmod.clean(str(q), 300)
        if q:
            out.append(q)
    desc = str(job.get("description") or "")
    for ln in re.split(r"\r?\n", desc):
        ln = fitcheck._BULLET.sub("", ln).strip()
        if 12 <= len(ln) <= 250 and (ln.endswith("?") or _QUESTION.search(ln)) and not fitcheck._META_LINE.match(ln):
            out.append(ln)
    blob = (str(job.get("title") or "") + " " + desc).lower()
    std = [("What is your notice period?", True), ("What are your salary expectations?", True),
           ("Are you authorized to work in this country?", bool(re.search(r"authori[sz]|visa|sponsor|right to work|eligible to work", blob))),
           ("Link to your portfolio or GitHub", bool(re.search(r"portfolio|github|work samples?", blob))),
           ("Are you willing to relocate?", bool(re.search(r"relocat", blob))),
           ("Why do you want to work at this company?", True)]
    out += [q for q, hint in std if hint]
    seen, uniq = set(), []
    for q in out:
        k = answerbank.qtokens(q)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(q)
    return uniq[:MAX_QUESTIONS]


def safe_url(url: str) -> str:
    return url if re.match(r"https?://[^\s]+$", url or "", re.I) else ""


# ---- the service -----------------------------------------------------------------------------------------------------------

class Packets:
    def __init__(self, ctx: Ctx, *, clock=None, profile_fn=None, model_fn=None, applyq_fn=None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self.profile_fn, self.model_fn, self.applyq_fn = profile_fn, model_fn, applyq_fn
        self.file = JsonFile(Path(ctx.config.data_path()) / "packets.json")
        self.tailor = tailor.Tailor(ctx, clock=self.clock, profile_fn=profile_fn, model_fn=model_fn, applyq_fn=applyq_fn)
        self.bank = answerbank.Bank(ctx, clock=self.clock, profile_fn=profile_fn, model_fn=model_fn)

    def _all(self) -> list[dict]:
        with _LOCK:                       # every write holds this lock too, so a read never lands in the middle of a file swap
            data = self.file.load([])
        return data if isinstance(data, list) else []

    def _put(self, rec: dict) -> None:
        with _LOCK:
            rows = [r for r in self._all() if r["id"] != rec["id"]] + [rec]
            rows.sort(key=lambda r: r["created"])
            self.file.save(rows[-MAX_PACKETS:])

    def get_record(self, pid: str) -> dict:
        for r in self._all():
            if r["id"] == pid:
                return r
        raise LookupError("No such packet.")

    def list(self) -> list[dict]:
        return [{k: r.get(k) for k in ("id", "title", "company", "state", "created", "application_id")} for r in sorted(self._all(), key=lambda r: r["created"], reverse=True)]

    def delete(self, pid: str) -> None:
        with _LOCK:
            rows = self._all()
            if not any(r["id"] == pid for r in rows):
                raise LookupError("No such packet.")
            self.file.save([r for r in rows if r["id"] != pid])

    # -- preparing (a draft only) ------------------------------------------------------------------------------
    async def prepare(self, body: dict) -> dict:
        app_id = str(body.get("application_id") or "").strip()
        if app_id:
            job, _item = self.tailor.job_from_item(app_id)
            if isinstance(body.get("job"), dict):
                job = tailor.clean_job({**job, **body["job"]})
        else:
            job = tailor.clean_job(body.get("job") if isinstance(body.get("job"), dict) else body)
        variant = None
        if app_id and body.get("fresh") is not True:
            mine = self.tailor.list(app_id)
            if mine:
                variant = mine[0]
        if variant is None:
            pre = {"job": job, "master_id": body.get("master_id") or "", "channel": body.get("channel") or ""}
            if app_id:
                pre["application_id"] = app_id
            if "ai" in body:
                pre["ai"] = body["ai"] is True
            variant = await self.tailor.preview(pre)
        questions = detect_questions(job, [q for q in (body.get("questions") or []) if isinstance(q, str)][:MAX_QUESTIONS] if isinstance(body.get("questions"), list) else [])
        rec = {"id": "pk-" + secrets.token_hex(4), "application_id": app_id, "variant_id": variant["id"], "job": job, "questions": questions,
               "title": job.get("title") or "Posting", "company": job.get("company", ""), "state": "draft",
               "created": self.clock().isoformat(timespec="seconds")}
        self._put(rec)
        return await asyncio.to_thread(self.view, rec["id"])

    # -- the one page ------------------------------------------------------------------------------------------
    def view(self, pid: str) -> dict:
        rec = self.get_record(pid)
        job = rec["job"]
        v = self.tailor.get(rec["variant_id"])
        fb = self.tailor.facts()
        s = ats.score(fb, job, ats.resume_text_of(v["resume"]), titles=ats.titles_of(fb, v["resume"]))
        answers = []
        for m in self.bank.match_many(rec["questions"], job):
            usable = m["status"] in ("auto", "suggest") and bool(m["answer"]) and not m["fills"]
            answers.append({"question": m["question"], "status": m["status"], "answer": m["answer"], "confidence": m["confidence"], "label": m["label"],
                            "reason": m["reason"], "fills": m["fills"], "owner_only": m["status"] == "owner_only", "usable": usable})
        pub = tailor.public_variant(v)
        mine = [f["url"] for f in fb.of_type("link") if safe_url(f.get("url", ""))][:6]
        links = {"posting": safe_url(job.get("url", "")), "apply_email": job.get("contact_email", ""), "yours": mine}
        dl = [{"label": f"{part.title()} ({fmt.upper()})", "href": f"/api/tailor/variants/{v['id']}/export?format={fmt}&part={part}"}
              for part in ("resume", "cover") for fmt in ("docx", "pdf", "txt") if not (part == "cover" and pub["blocked"])]
        return {"id": rec["id"], "state": rec["state"], "created": rec["created"], "application_id": rec["application_id"],
                "job": {k: job.get(k, "") for k in ("title", "company", "url", "location")}, "channel": v["channel"],
                "fit": {"score": v["fit"]["score"], "label": v["fit"]["label"], "summary": v["fit"]["summary"], "flags": v["fit"].get("flags", [])[:6],
                        "risk_blocked": v["fit"].get("risk_blocked", False)},
                "ats": {"score": s["score"], "label": s["label"], "breakdown": s["breakdown"], "matched": s["matched"], "missing": s["missing"], "tips": s["tips"]},
                "resume": {"variant_id": v["id"], "text": tailor.to_text(v["resume"]), "version": v.get("resume_version", ""), "master": v.get("master")},
                "cover": {"text": tailor.cover_text(v["cover"], with_subject=True), "words": v["cover"].get("words", 0)},
                "blocked": pub["blocked"], "blockers": pub["blockers"],
                "answers": answers, "company": company_summary(job), "pay": pay_range(job), "links": links, "downloads": dl,
                "can_queue": rec["state"] != "queued", "nothing_sent": True,
                "note": "This page is a draft. Nothing has been sent, submitted or posted."}

    # -- the one tap: into the application queue (still a draft there) --------------------------------------------
    def to_queue(self, pid: str) -> dict:
        rec = self.get_record(pid)
        svc = self.tailor._applyq()
        item_id = rec["application_id"]
        created = False
        job = rec["job"]
        if not item_id:
            raw = {"title": job.get("title"), "company": job.get("company"), "url": job.get("url") or "", "description": job.get("description"),
                   "contact_email": job.get("contact_email"), "kind": job.get("kind") or "job", "source": "packet", "channel": job.get("channel") or ""}
            item, created = svc.add(raw, draft=True)         # a draft in the queue; adding never sends
            item_id = item["id"]
        out = self.tailor.apply_to_application(rec["variant_id"], item_id)
        usable: dict[str, str] = {}
        for a in self.view(pid)["answers"]:
            if a["usable"]:
                usable[a["question"][:120]] = a["answer"][:800]
        if usable:
            current = (svc.get(item_id).get("draft") or {}).get("answers") or {}
            svc.update(item_id, {"draft": {"answers": {**current, **usable}}, "note": "Answers added from the application packet"})
        rec["application_id"], rec["state"] = item_id, "queued"
        self._put(rec)
        return {"ok": True, "application_id": item_id, "created": created, "blocked": out["blocked"], "blockers": out["blockers"], "answers_added": len(usable),
                "message": "It is in your application queue as a draft. Nothing was sent; you send it from there.", "nothing_sent": True}

    def text_pack(self, pid: str) -> str:
        v = self.view(pid)
        parts = [f"APPLICATION PACKET: {v['job']['title']}" + (f" at {v['job']['company']}" if v["job"]["company"] else ""), "",
                 f"ATS match: {v['ats']['score']}/100 ({v['ats']['label']}). Fit: {v['fit']['summary']}", "", "COVER NOTE", v["cover"]["text"], "", "RESUME", v["resume"]["text"]]
        ans = [a for a in v["answers"] if a["answer"] and not a["owner_only"]]
        if ans:
            parts += ["", "ANSWERS"] + [f"{a['question']}\n{a['answer']}" for a in ans]
        if v["pay"]["found"]:
            parts += ["", "PAY IN THE POSTING"] + v["pay"]["lines"]
        if v["links"]["posting"]:
            parts += ["", "POSTING: " + v["links"]["posting"]]
        return "\n".join(parts).strip() + "\n"


def make_router(ctx: Ctx, *, clock=None, profile_fn=None, model_fn=None, applyq_fn=None, **_: Any) -> APIRouter:
    router = APIRouter(prefix="/api/packet", dependencies=[Depends(ctx.need_auth)])
    svc = Packets(ctx, clock=clock, profile_fn=profile_fn, model_fn=model_fn, applyq_fn=applyq_fn)
    router.service = svc                                      # type: ignore[attr-defined]

    def on() -> None:
        if not enabled(ctx.config):
            raise HTTPException(404, "Packets are switched off (tailor_enabled is false).")

    def guard(fn):
        try:
            return fn()
        except (PacketError, tailor.TailorError) as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("")
    async def listing():
        on()
        return {"packets": await asyncio.to_thread(svc.list)}

    @router.post("/prepare")
    async def prepare(body: dict = Body(default_factory=dict)):
        """{application_id | job:{title,description,...}, questions?: [...], master_id?, fresh?, ai?}. Builds the packet page. Sends nothing."""
        on()
        try:
            out = await svc.prepare(body)
        except (PacketError, tailor.TailorError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        ctx.audit("packet_prepared", id=out["id"])
        return out

    @router.get("/{pid}")
    async def one(pid: str):
        on()
        return await asyncio.to_thread(lambda: guard(lambda: svc.view(pid)))

    @router.post("/{pid}/queue")
    async def queue(pid: str):
        """The one tap: put the packet's cover note and answers into the application queue as a draft. Never sends."""
        on()
        out = await asyncio.to_thread(lambda: guard(lambda: svc.to_queue(pid)))
        ctx.audit("packet_queued", id=pid, application=out["application_id"])
        return out

    @router.get("/{pid}/text")
    async def text(pid: str):
        on()
        body = await asyncio.to_thread(lambda: guard(lambda: svc.text_pack(pid)))
        return PlainTextResponse(body, headers={"Cache-Control": "no-store"})

    @router.delete("/{pid}")
    async def drop(pid: str):
        on()
        guard(lambda: svc.delete(pid))
        return {"ok": True}

    return router


# ---- assistant tool (a draft only) -----------------------------------------------------------------------------------------

def _short(value: Any, n: int = 200) -> str:
    return " ".join(str(value if value is not None else "").split())[:n]


def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    async def packet_prepare(a):
        body: dict = {}
        if isinstance(a.get("application_id"), str) and a["application_id"].strip():
            body["application_id"] = a["application_id"].strip()
        elif isinstance(a.get("description"), str) and a["description"].strip():
            body["job"] = {k: a[k] for k in ("title", "company", "description", "url") if isinstance(a.get(k), str)}
        else:
            return False, "Give an application id, or the job title and description."
        if isinstance(a.get("questions"), list):
            body["questions"] = [q for q in a["questions"] if isinstance(q, str)][:MAX_QUESTIONS]
        ok, d = await hub.call("POST", "/api/packet/prepare", body=body)
        if not ok:
            return False, d
        lines = [f"Packet {d['id']} for {_short(d['job']['title'], 80)}: ATS match {d['ats']['score']}/100 ({d['ats']['label']}); fit {d['fit']['label']}.",
                 f"Resume version: {d['resume']['version'] or 'none recorded'}. {len([x for x in d['answers'] if x['usable']])} answer(s) ready from the answer bank.",
                 ("BLOCKED until the owner resolves: " + " ".join(d["blockers"])) if d["blocked"] else "Every cover-note sentence is backed by the owner's facts.",
                 "A draft only. Nothing was sent or queued. The owner reviews it in Apps > Application packet and decides."]
        return True, "\n".join(lines)

    schema = {"type": "object", "additionalProperties": False, "required": [],
              "properties": {"application_id": {"type": "string"}, "title": {"type": "string"}, "company": {"type": "string"}, "description": {"type": "string"},
                             "url": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}}}}
    return mark_tools([ExtraTool({"name": "packet_prepare", "description": "Prepare a one-page application packet (tailored resume, cover note, ATS match score, answers from the answer bank, "
                                  "company summary, pay, links) for ONE job posting. A draft only: it does not send, submit or queue anything; the owner reviews it and decides.",
                                  "input_schema": schema}, packet_prepare, untrusted=True)])
