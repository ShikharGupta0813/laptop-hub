"""Share to REDLINE: a job the owner shares from another app on the phone (Share > REDLINE) becomes an opportunity.

The shared TEXT (and link) is turned into a find through the same path as every other find (hub/opps.py: vetting, job
preferences, triage go / no-go), then scored the way an applicant tracking system would (hub/ats.py). The owner decides
what happens next with one tap: "Tailor + packet" (hub/packet.py) or "Add to apply queue" (hub/opps.py). This module
never queues, sends or applies anything itself.

Hard rules (docs/api/sharein.md):
- Nothing is fetched from LinkedIn, Naukri or any other host in `oppsources.NEVER_AUTOMATE`, nor from a link shortener
  (it may point at one of them). For those only the shared text is used. A bare link to such a host is kept as a note and the
  owner is asked to paste the job text too.
- Any other bare link goes through the existing pasted-link path (opps.add_link), which reads the public page as it does today.
- Plain code only: no model sees the shared text. Nothing leaves the laptop.
- The text is data. It is cut to a safe size, never run, and the audit line holds the host and sizes only.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException

from . import ats, opps, opps_vet, oppsources
from .common import Ctx

log = logging.getLogger("hub.sharein")

MAX_TEXT = 20000           # characters read from one share
KEEP_TEXT = 6000           # characters stored with the find (opps keeps 1500 for feeds; a shared posting is read in full once)
RATE_MAX = 30              # shares per RATE_WINDOW seconds
RATE_WINDOW = 3600
SOURCE_NAME = "Shared from phone"
BARE_WORDS = 12            # a share with fewer words than this (after the links are removed) is "just a link"
SHORTENERS = {"lnkd.in", "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "rb.gy", "cutt.ly", "is.gd", "shorturl.at",
              "t.ly", "tiny.cc", "s.id", "rebrand.ly", "amzn.to", "fb.me", "youtu.be", "wa.me"}
SITE_NAMES = {"linkedin.com": "LinkedIn", "lnkd.in": "LinkedIn", "naukri.com": "Naukri", "indeed.com": "Indeed",
              "glassdoor.com": "Glassdoor", "instahyre.com": "Instahyre", "wellfound.com": "Wellfound"}


def enabled(config) -> bool:
    return getattr(config, "sharein_enabled", True) is not False


class ShareError(ValueError):
    """A plain sentence for the owner. `status` is the HTTP status the route uses."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- reading what was shared ---------------------------------------------------------------------------------------

_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRAIL = ".,;:!?)]}>'\""
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def urls_in(text: str) -> list[str]:
    out: list[str] = []
    for m in _URL.findall(text or ""):
        u = m.rstrip(_TRAIL)
        if u and u not in out:
            out.append(u)
    return out


def strip_urls(text: str) -> str:
    return _URL.sub(" ", text or "")


def host_of(url: str) -> str:
    return opps_vet.host_of(url)


def blocked_host(host: str) -> str:
    """The never-automate site (or link shortener) a host belongs to, else ''. A link here is never opened or read by the hub."""
    return oppsources._under(host, oppsources.NEVER_AUTOMATE) or oppsources._under(host, SHORTENERS)


def site_name(host: str) -> str:
    d = oppsources._under(host, SITE_NAMES) or blocked_host(host)
    return SITE_NAMES.get(d) or (d or host)


_LI_JOB = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})", re.I)
_LI_ID = re.compile(r"(?:^|[?&])(?:currentJobId|jobId)=(\d{6,})", re.I)


def canonical_url(url: str) -> str:
    """A stable form of the link so the same job shared twice is found again. Keeps the link only (nothing is opened by the hub)."""
    url = (url or "").strip()
    if not opps.norm_url(url):
        return ""
    host = host_of(url)
    parts = urlsplit(url)
    if oppsources._under(host, {"linkedin.com"}):
        m = _LI_JOB.search(parts.path) or _LI_ID.search(parts.query)
        if m:
            return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
        return f"https://www.linkedin.com{parts.path.rstrip('/') or '/'}"
    if oppsources._under(host, {"naukri.com", "instahyre.com", "wellfound.com", "glassdoor.com"}):
        return f"https://{host}{parts.path.rstrip('/') or '/'}"             # the page is named by its path; the query is only tracking
    return opps.norm_url(url)


_PREFIX = re.compile(r"^(?:hi[,!]?\s+|hey[,!]?\s+)?(?:i found|found|saw|check out|have a look at|look at|take a look at|sharing)\s+(?:this|a|an|the)\s+"
                     r"(?:new\s+)?(?:job|jobs|job opening|opening|role|position|vacancy|opportunity|post)\b(?P<rest>.*)$", re.I)
_AT_CO = re.compile(r"^\s*(?:at|from|with)\s+(?P<co>[^:\n]+?)\s*[:\-–—|]\s*(?P<t>.+)$", re.I)
_AT_ONLY = re.compile(r"^\s*(?:at|from)\s+(?P<co>[^:\n]+?)\s*[:\-–—|]?\s*$", re.I)
_HIRING = re.compile(r"^(?P<co>.+?)\s+is\s+(?:now\s+)?hiring\s*(?:for\s+)?(?:an?\s+|the\s+)?(?P<t>.+?)(?:\s+(?:in|at|for|-|–)\s+.+|\s*\(.*|[.!]*)$", re.I)
_TITLE_AT = re.compile(r"^(?P<t>.+?)\s+at\s+(?P<co>.+?)(?:\s*[-–—|·/,(]\s*.+)?$", re.I)
_SPLIT = re.compile(r"^(?P<t>.+?)\s*[-–—|·]\s*(?P<co>.+?)(?:\s*[-–—|·]\s*.+)?$")
_LABEL_T = re.compile(r"^\s*(?:job\s*title|title|role|position|designation|opening)\s*[:\-]\s*(?P<v>.+)$", re.I | re.M)
_LABEL_C = re.compile(r"^\s*(?:company(?:\s*name)?|organi[sz]ation|employer|hiring\s*company)\s*[:\-]\s*(?P<v>.+)$", re.I | re.M)
_SITE_TAIL = re.compile(r"\s*[|\-–—·]\s*(?:naukri(?:\.com)?|linkedin|indeed|instahyre|wellfound|glassdoor)\b.*$", re.I)
_BAD_CO = re.compile(r"^\d|\b(?:yrs?|years?|lpa|lakhs?|remote|hybrid|onsite|on-site|full[- ]time|part[- ]time|experience|apply|easy apply|"
                     r"permanent|contract|walk[- ]?in|salary)\b", re.I)
_COMPANY_SUFFIX = re.compile(r"\b(?:pvt|private|ltd|limited|inc|llc|llp|corp|corporation|technologies|labs|solutions|systems|software|"
                             r"services|group|bank|finance|capital|payments|networks)\b", re.I)


def _tidy(value: str, limit: int) -> str:
    value = _SITE_TAIL.sub("", " ".join(_CONTROL.sub("", value or "").split()))
    return value.strip(" -–—|·:,.")[:limit]


def _good_company(co: str) -> bool:
    return bool(co) and len(co) <= 80 and len(co.split()) <= 7 and not _BAD_CO.search(co)


def guess_title_company(text: str, subject: str = "") -> tuple[str, str]:
    """Best guess of (title, company) from LinkedIn-, Naukri- and plain-style share text. Either may be ''. The owner can fix both."""
    body = strip_urls(text)
    title = company = ""
    m = _LABEL_T.search(body)
    if m:
        title = _tidy(m.group("v"), 120)
    m = _LABEL_C.search(body)
    if m:
        company = _tidy(m.group("v"), 80)
    lines = [ln.strip() for ln in body.replace("\r", "").split("\n") if ln.strip()]
    first = _tidy(subject, 200) if len(subject or "") > 6 else ""
    for line in ([lines[0]] if lines else []) + ([first] if first else []):
        if title and company:
            break
        line = " ".join(line.split())[:240]
        t = co = ""
        pm = _PREFIX.match(line)
        if pm:
            rest = pm.group("rest").strip()
            am = _AT_CO.match(rest)
            ao = _AT_ONLY.match(rest)
            if am:
                co, t = am.group("co"), am.group("t")
            elif ao:
                co = ao.group("co")
            else:
                rest = rest.lstrip(" :-–—|")
                for rx in (_TITLE_AT, _SPLIT):
                    mm = rx.match(rest)
                    if mm and _good_company(_tidy(mm.group("co"), 80)):
                        t, co = mm.group("t"), mm.group("co")
                        break
                else:
                    t = rest
        else:
            mm = _HIRING.match(line)
            if mm:
                co, t = mm.group("co"), mm.group("t")
            else:
                for rx in (_TITLE_AT, _SPLIT):
                    mm = rx.match(line)
                    if mm and len(mm.group("t").split()) <= 9 and _good_company(_tidy(mm.group("co"), 80)):
                        t, co = mm.group("t"), mm.group("co")
                        break
                else:
                    t = line if len(line.split()) <= 14 else ""
        title = title or _tidy(t, 120)
        company = company or (_tidy(co, 80) if _good_company(_tidy(co, 80)) else "")
    if not title and lines and not _PREFIX.match(lines[0]):
        title = _tidy(lines[0], 120)
    return title, company


def parse_share(text: str, url: str = "", subject: str = "") -> dict:
    """{text, url, host, blocked (site name or ''), bare, title, company}. `text` has the links removed."""
    raw = _CONTROL.sub("", str(text or ""))[:MAX_TEXT]
    links = urls_in(raw)
    link = ""
    for cand in ([str(url or "").strip()] if url else []) + links:
        if opps.norm_url(cand):
            link = cand.rstrip(_TRAIL)
            break
    body = "\n".join(ln.rstrip() for ln in strip_urls(raw).replace("\r", "").split("\n"))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    words = len(re.findall(r"[A-Za-z0-9]+", body))
    host = host_of(link) if link else ""
    hit = blocked_host(host) if host else ""
    # a link to a site that must not be read counts too when it is hidden among other links in the text
    if not hit:
        for u in links:
            if blocked_host(host_of(u)):
                hit, host, link = blocked_host(host_of(u)), host_of(u), u.rstrip(_TRAIL)
                break
    title, company = guess_title_company(body, subject)
    return {"text": body, "url": canonical_url(link) if link else "", "raw_url": link, "host": host, "blocked": hit,
            "site": site_name(host) if hit else "", "bare": words < BARE_WORDS, "words": words, "title": title, "company": company}


# ---- the service ---------------------------------------------------------------------------------------------------

def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def ats_summary(ctx: Ctx, job: dict, profile_fn=None, clock=None) -> dict | None:
    """A short ATS result for the sheet, or None (no facts yet, tool switched off). Plain code."""
    if not ats.enabled(ctx.config):
        return None
    try:
        out = ats.AtsService(ctx, profile_fn=profile_fn, clock=clock).score({"job": job})
    except ats.ReadError as exc:
        return {"score": None, "label": "", "note": str(exc)}
    except Exception:  # noqa: BLE001 - the score is an extra
        log.warning("sharein: ATS score failed", exc_info=True)
        return None
    return {"score": out["score"], "label": out["label"], "resume": out["resume"],
            "matched": [k["term"] for k in out["matched"]][:8], "matched_total": len(out["matched"]),
            "listed_in_facts": [k["term"] for k in out["missing"]["listed_in_facts"]][:6],
            "gap": [k["term"] for k in out["missing"]["gap"]][:6], "tips": out["tips"][:2],
            "breakdown": [{"label": b["label"], "score": b["score"]} for b in out["breakdown"] if b["score"] is not None]}


def find_view(f: dict | None) -> dict | None:
    if not f:
        return None
    from . import triage
    return {"id": f["id"], "title": f.get("title", ""), "company": f.get("company", ""), "url": f.get("url", ""), "status": f.get("status", ""),
            "score": f.get("score"), "reason": f.get("reason", ""), "source": f.get("source", ""),
            "flags": [{"label": x.get("label", ""), "severity": x.get("severity", "")} for x in (f.get("flags") or [])][:6],
            "blocked_reason": f.get("blocked_reason", ""), "filtered_reason": f.get("filtered_reason", ""),
            "note": bool((f.get("meta") or {}).get("placeholder")), "triage": triage.compact(f["triage"]) if f.get("triage") else None}


class Sharein:
    def __init__(self, ctx: Ctx, *, engine: opps.Opps | None = None, clock: Callable[[], float] | None = None,
                 profile_fn=None):
        self.ctx = ctx
        self.engine = engine or opps.engine_for(ctx)
        self.clock = clock or time.time
        self.profile_fn = profile_fn
        self.hits: collections.deque = collections.deque()

    def limit(self) -> None:
        now = self.clock()
        while self.hits and self.hits[0] < now - RATE_WINDOW:
            self.hits.popleft()
        if len(self.hits) >= RATE_MAX:
            raise ShareError(f"That is a lot of shares in an hour ({RATE_MAX}). Try again in a little while.", 429)
        self.hits.append(now)

    # -- finding the same job again ----------------------------------------------------------------------------
    def existing(self, url: str, title: str, company: str) -> dict | None:
        finds = list(self.engine._state()["finds"].values())
        u = opps.norm_url(url) if url else ""
        if u:
            for f in finds:
                if f.get("url") and opps.norm_url(f["url"]) == u:
                    return f
        t, c = _norm(title), _norm(company)
        if t and c:
            for f in finds:
                if _norm(f.get("title", "")) == t and _norm(f.get("company", "")) == c:
                    return f
        return None

    def _revive(self, f: dict) -> dict:
        """The owner shared it on purpose, so a find they dismissed earlier comes back."""
        if f.get("status") == "dismissed":
            return self.engine.set_status(f["id"], "new") or f
        return f

    # -- one share -----------------------------------------------------------------------------------------------
    async def handle(self, body: dict) -> dict:
        p = parse_share(body.get("text") or "", body.get("url") or "", body.get("subject") or "")
        title = opps.clean(body.get("title"), 200) or p["title"]
        company = opps.clean(body.get("company"), 100) or p["company"]
        if not p["text"] and not p["url"]:
            raise ShareError("Nothing to read. Share the job text or paste it here.")
        self.limit()
        replace = str(body.get("replace_id") or "")
        old = self.engine.find(replace) if replace and opps.FIND_ID_RE.match(replace) else None
        if p["bare"] and not old:
            return await self._bare(p, title, company)
        return await self._text(p, title, company, old)

    def _response(self, p: dict, find: dict | None, *, existing: bool = False, needs_text: bool = False, message: str = "",
                  ats_out: dict | None = None, problem: str = "") -> dict:
        view = find_view(find)
        placeholder = bool(view and view["note"])
        can_act = bool(view) and not placeholder and view["status"] != "blocked"
        job = ({"title": find.get("title", ""), "company": find.get("company", ""), "url": find.get("url", ""),
                "description": find.get("description", "")} if find and not placeholder else None)
        return {"find": view, "job": job, "triage": (view or {}).get("triage") if not placeholder else None, "ats": ats_out, "existing": existing,
                "needs_text": needs_text, "message": message, "problem": problem,
                "host": p["host"], "site": p["site"], "never_automate": bool(p["blocked"]),
                "actions": {"packet": can_act, "queue": can_act and view["status"] != "queued", "queued": bool(view and view["status"] == "queued")},
                "nothing_sent": True}

    async def _bare(self, p: dict, title: str, company: str) -> dict:
        """Only a link was shared."""
        url = p["url"]
        if not url:
            raise ShareError("Nothing to read. Share the job text or paste it here.")
        found = await asyncio.to_thread(self.existing, url, "", "")
        if found:
            found = await asyncio.to_thread(self._revive, found)
            note = bool((found.get("meta") or {}).get("placeholder"))
            msg = (f"That {p['site'] or p['host']} link is already saved as a note. Paste the job text too." if note else "You already have this job.")
            return self._response(p, found, existing=True, needs_text=note, message=msg, ats_out=None if note else await self._ats(found))
        if p["blocked"]:
            row = {"title": title or f"Shared link ({p['site'] or p['host']})", "url": url, "company": company, "description": "Link kept as a note. The job text was not shared.",
                   "meta": {"shared": True, "placeholder": True}}
            await asyncio.to_thread(self._ingest, [row])
            find = self.engine.find(opps.find_key(url, opps.clean(row["title"], 200), opps.clean(company, 100), "manual"))
            msg = (f"That is a {p['site'] or p['host']} link. REDLINE never opens or reads {p['site'] or p['host']}, so there is nothing to score yet. "
                   "I kept the link as a note. Open the job there, copy its text, and paste it here too.")
            return self._response(p, find, needs_text=True, message=msg)
        # an ordinary public page: the same pasted-link path as Opportunities > Add a link (reads the public page)
        try:
            res = await self.engine.add_link(url, title, auto_queue=False, source_name=SOURCE_NAME)
        except ValueError as exc:
            raise ShareError(str(exc)) from None
        find = res.get("find")
        if find is not None:
            await asyncio.to_thread(self._mark_shared, find["id"])
            find = self.engine.find(find["id"])
        if not res.get("read"):
            msg = "The page could not be read" + (f" ({res['problem']})" if res.get("problem") else "") + ". Paste the job text here to score it."
            return self._response(p, find, needs_text=True, message=msg, problem=res.get("problem", ""))
        return self._response(p, find, message="Read the public page.", ats_out=await self._ats(find))

    def _mark_shared(self, find_id: str) -> None:
        with self.engine.lock:
            state = self.engine._state()
            f = state["finds"].get(find_id)
            if f is not None:
                f.setdefault("meta", {})["shared"] = True
                self.engine._save_state(state)

    def _ingest(self, rows: list[dict]) -> dict:
        return self.engine.ingest(rows, source_name=SOURCE_NAME, source_id="manual", kind="job", auto_queue=False)

    async def _text(self, p: dict, title: str, company: str, old: dict | None) -> dict:
        """Job text was shared (with or without a link). The text is used as it is; no page is fetched."""
        url = p["url"]
        if not title:
            title = f"Shared job ({p['site'] or p['host']})" if p["host"] else "Shared job"
        found = old or await asyncio.to_thread(self.existing, url, title, company)
        if found and not (found.get("meta") or {}).get("placeholder") and not old:
            found = await asyncio.to_thread(self._revive, found)
            return self._response(p, found, existing=True, message="You already have this job. Nothing was added twice.", ats_out=await self._ats(found))
        if found:                                    # a note kept earlier from a bare link, or a re-check: replaced by the full text
            await asyncio.to_thread(self.engine.delete_find, found["id"])
            url = url or found.get("url", "")
        full = p["text"][:KEEP_TEXT]
        row = {"title": title, "company": company, "url": url, "description": full, "meta": {"shared": True}}
        await asyncio.to_thread(self._ingest, [row])
        key = opps.find_key(url, opps.clean(title, 200), opps.clean(company, 100), "manual")
        find = await asyncio.to_thread(self._widen, key, full)
        if find is None:
            raise ShareError("That could not be turned into a job. Check the text and try again.")
        ats_out = await self._ats(find)
        msg = "Here is how it looks. Nothing has been sent or queued."
        if p["blocked"]:
            msg = f"Used only the text you shared. {p['site'] or p['host']} was not opened. " + msg
        return self._response(p, find, ats_out=ats_out, message=msg)

    def _widen(self, key: str, full: str) -> dict | None:
        """Feeds keep 1,500 characters. A job shared by hand keeps up to KEEP_TEXT and is triaged on all of it."""
        from . import triage
        with self.engine.lock:
            state = self.engine._state()
            f = state["finds"].get(key)
            if f is None:
                return None
            f["description"] = full
            if f.get("status") != "blocked":
                tri = triage.assess_find(self.ctx, f)
                if tri:
                    f["triage"] = tri
            self.engine._save_state(state)
            return dict(f)

    async def _ats(self, find: dict | None) -> dict | None:
        if not find:
            return None
        job = {"title": find.get("title", ""), "company": find.get("company", ""), "description": find.get("description", "")}
        return await asyncio.to_thread(ats_summary, self.ctx, job, self.profile_fn)


# ---- routes --------------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, clock=None, profile_fn=None, **_: Any) -> APIRouter:
    router = APIRouter(prefix="/api/sharein", dependencies=[Depends(ctx.need_auth)])
    svc = Sharein(ctx, clock=clock, profile_fn=profile_fn)
    router.service = svc                                      # type: ignore[attr-defined]

    @router.post("")
    async def share(body: dict = Body(default_factory=dict)):
        """{text, url?, subject?, title?, company?, replace_id?} -> {find, triage, ats, existing, needs_text, message, actions}. Sends and queues nothing."""
        if not enabled(ctx.config):
            raise HTTPException(404, "Share to REDLINE is switched off (sharein_enabled is false).")
        if not isinstance(body, dict):
            raise HTTPException(400, "Send a JSON object.")
        try:
            out = await svc.handle(body)
        except ShareError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        ctx.audit("sharein", host=out["host"], never=out["never_automate"], existing=out["existing"], chars=len(str(body.get("text") or "")))
        return out

    return router
