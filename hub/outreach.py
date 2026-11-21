"""BATCHED PERSONAL OUTREACH: 5 to 20 short, personal emails a day to people YOU chose, reviewed as a list, sent with ONE tap.
See docs/api/outreach.md.

How it works: you pick people (from your networking contacts, from companies on your watch list, or by hand with a link to the
public page where you got their address). Each email is drafted for that person from your CONFIRMED facts (company, role, one
relevant fact) and put in an ordinary outreach batch (`hub/batches.py`). You read the list, edit what you like and tap Send once.
The batch machinery sends them one by one through your own Gmail (`Applyq.send`), a few minutes apart.

HARD LIMITS IN CODE (not settings you can raise):
- at most 20 people in a batch and 20 outreach emails a day (`batches_outreach_cap` can only LOWER the day limit),
- NO ADDRESS GUESSING: an address is only ever one you typed yourself and said you did not guess; nothing is looked up, derived
  from a name or a company, or scraped; no page is fetched,
- no attachment unless you tick "attach my resume" for that batch (then only your resume file, nothing else),
- anyone on the OPT-OUT list is never drafted and never sent to, even if they were already in a batch: the check runs again at the
  moment of sending. A reply of "no thanks" or "unsubscribe" is only SUGGESTED for the list (hub/replies.py); you confirm it,
- every email carries a line offering to stop,
- ONE follow-up per person after `outreach_followup_days` (default 5): it is drafted into its own batch and needs its own tap,
- nothing here sends: sending is `POST /api/outreach/batches/{id}/send {confirm: true}` (the same path as the approved
  `batch_send` tool, which is always asked every time). Panic stops it.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import threading
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException

from . import assistant as _assistant
from .assistant import ExtraTool, mark_tools
from .common import Ctx, InProcessClient, JsonFile

log = logging.getLogger("hub.outreach")

HARD_BATCH = 20
HARD_DAY = 20
MIN_BATCH = 1                    # the owner may pick any number from 1; 5 to 20 is the intended use
MAX_PEOPLE = 400
DEFAULT_FOLLOWUP_DAYS = 5
MAX_WORDS = 130
FOOTER = 'If you would rather not hear from me, just reply "no thanks" and I will not write again.'
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}$")
_ROLE_BOXES = re.compile(r"^(?:no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|abuse|bounce)", re.I)
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_FILL = "[FILL: {}]"

_assistant.TOOL_GROUPS["outreach"] = (
    {"outreach_prepare"},
    re.compile(r"\b(?:prepare|draft|build) (?:a |an |my )?(?:batch of )?(?:outreach|personal (?:emails?|messages?))\b|\boutreach (?:batch|emails?|today)\b|\bwrite to (?:the )?people i picked\b", re.I))


class OutreachError(ValueError):
    pass


class NotFound(LookupError):
    pass


def clean(value: Any, limit: int = 200, multiline: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value)
    return text.strip()[:limit] if multiline else " ".join(text.split())[:limit]


def enabled(config) -> bool:
    return getattr(config, "outreach_enabled", True) is not False


def followup_days(config) -> int:
    v = getattr(config, "outreach_followup_days", DEFAULT_FOLLOWUP_DAYS)
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 60 else DEFAULT_FOLLOWUP_DAYS


def valid_address(addr: str) -> bool:
    a = (addr or "").strip()
    return bool(_EMAIL.match(a)) and not _ROLE_BOXES.match(a) and ".." not in a


def public_link(url: str) -> str:
    u = clean(url, 500)
    try:
        p = urlsplit(u)
    except ValueError:
        return ""
    return u if p.scheme == "https" and p.hostname and "." in p.hostname else ""


def first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0][:40]


def build_draft(person: dict, *, my_name: str = "", fact: str = "", followup: bool = False, first_sent: str = "") -> dict:
    """The email for one person. Pure, no model. Only the owner's confirmed fact (if one fits) says anything about the owner."""
    company, role = person.get("company", ""), person.get("role", "")
    hello = f"Hello {first_name(person['name'])},"
    sign = ["Best regards,", my_name or _FILL.format("your name")]
    if followup:
        subject = clean(f"Following up on my note about {company}" if company else "Following up on my earlier note", 120)
        lines = [hello, "", f"I wrote to you on {first_sent or 'a few days ago'}" + (f" about {company}" if company else "") +
                 ". This is one short follow-up in case it got buried; there is no need to reply if it is not a fit.", "", FOOTER, "", *sign]
        return {"subject": subject, "body": "\n".join(lines), "facts_used": [], "words": len(" ".join(lines).split())}
    subject = clean(f"A question about {role} at {company}" if role and company else (f"A short note about {company}" if company else "A short personal note"), 120)
    lines = [hello, ""]
    reason = f"I am writing to you because of {company}" + (f" and its {role} work." if role else ".") if company else "I am writing to you personally."
    lines.append(reason)
    if person.get("known_from"):
        lines.append(f"We know each other through {person['known_from']}.")
    if person.get("source_url"):
        lines.append(f"I found your public details here: {person['source_url']}")
    proof = f"One thing from my background that may be relevant: {fact.strip().rstrip('.')}." if fact else ""
    ask = "If you are the right person, or can point me to someone who is, I would be grateful for a short reply."
    body = lines + ([proof] if proof else []) + [ask, "", FOOTER, "", *sign]
    if len(" ".join(body).split()) > MAX_WORDS and proof:
        body.remove(proof)                              # the optional sentence goes first
        proof = ""
    return {"subject": subject, "body": "\n".join(body), "facts_used": [fact] if proof else [], "words": len(" ".join(body).split())}


class Outreach:
    def __init__(self, ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None, applyq: Any = None, batches: Any = None,
                 networking_fn: Callable[[], list[dict]] | None = None, watch_fn: Callable[[], list[dict]] | None = None,
                 facts_fn: Callable[[dict], tuple[str, str]] | None = None, replies_fn: Callable[[], Any] | None = None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self._aq, self._batches = applyq, batches
        self.networking_fn, self.watch_fn, self.facts_fn, self.replies_fn = networking_fn, watch_fn, facts_fn, replies_fn
        self.file = JsonFile(ctx.config.data_path() / "outreach.json")
        self.lock = threading.RLock()

    # neighbours -----------------------------------------------------------------------------------------------------
    @property
    def aq(self):
        if self._aq is None:
            from . import applyq
            self._aq = applyq.Applyq(self.ctx, clock=self.clock)
        return self._aq

    @property
    def batches(self):
        if self._batches is not None:
            return self._batches
        from . import batches
        return batches.engine_for(self.ctx)

    def today(self) -> dt.date:
        return self.clock().date()

    def stamp(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def _data(self) -> dict:
        d = self.file.load({})
        d = d if isinstance(d, dict) else {}
        d["people"] = [p for p in d.get("people", []) if isinstance(p, dict) and p.get("id")]
        d["optout"] = [o for o in d.get("optout", []) if isinstance(o, dict) and o.get("address")]
        return d

    def _save(self, d: dict) -> None:
        d["people"], d["optout"] = d["people"][-MAX_PEOPLE:], d["optout"][-2000:]
        self.file.save(d)

    # the opt-out list -----------------------------------------------------------------------------------------------
    def optout_list(self) -> list[dict]:
        return sorted(self._data()["optout"], key=lambda o: o.get("at", ""), reverse=True)

    def is_opted_out(self, address: str) -> bool:
        a = (address or "").strip().lower()
        return bool(a) and any(o["address"] == a for o in self._data()["optout"])

    def add_optout(self, *, address: str = "", person_id: str = "", name: str = "", why: str = "marked by you") -> dict:
        with self.lock:
            d = self._data()
            if person_id:
                p = next((p for p in d["people"] if p["id"] == person_id), None)
                if p is None:
                    raise NotFound("No such person.")
                address, name = p["email"], name or p["name"]
            address = (address or "").strip().lower()
            if not valid_address(address):
                raise OutreachError("Give a plain email address to put on the opt-out list.")
            row = next((o for o in d["optout"] if o["address"] == address), None)
            if row is None:
                row = {"id": secrets.token_hex(6), "address": address, "name": clean(name, 80), "why": clean(why, 160), "at": self.stamp()}
                d["optout"].append(row)
            for p in d["people"]:
                if p["email"].lower() == address:
                    p["opted_out"] = True
            self._save(d)
        try:
            self.ctx.audit("outreach_optout", id=row["id"])
        except Exception:  # noqa: BLE001
            pass
        return row

    def remove_optout(self, oid: str) -> None:
        with self.lock:
            d = self._data()
            row = next((o for o in d["optout"] if o["id"] == oid), None)
            if row is None:
                raise NotFound("No such entry.")
            d["optout"].remove(row)
            for p in d["people"]:
                if p["email"].lower() == row["address"]:
                    p["opted_out"] = False
            self._save(d)

    # people ---------------------------------------------------------------------------------------------------------
    def add_person(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise OutreachError("A person is an object.")
        name, company = clean(raw.get("name"), 80), clean(raw.get("company"), 120)
        if not name:
            raise OutreachError("Give the person's name.")
        email = clean(raw.get("email"), 200).lower()
        if not email:
            raise OutreachError("Type the person's email address yourself. Addresses are never guessed or looked up.")
        if raw.get("address_confirmed") is not True:
            raise OutreachError("Tick that you got this address from the person or from their own public page, and did not guess it.")
        if not valid_address(email):
            raise OutreachError("That does not look like a plain personal email address.")
        origin = str(raw.get("origin") or "manual")
        if origin not in ("network", "watch", "manual"):
            raise OutreachError("origin must be network, watch or manual.")
        link = public_link(str(raw.get("source_url") or ""))
        if origin != "network" and not link:
            raise OutreachError("Add the https link of the public page where you found this person or their address.")
        if raw.get("source_url") and not link:
            raise OutreachError("The source link must be an https page.")
        with self.lock:
            d = self._data()
            if any(p["email"].lower() == email for p in d["people"]):
                raise OutreachError("That person is already in your list.")
            if any(o["address"] == email for o in d["optout"]):
                raise OutreachError("That address is on your opt-out list, so it cannot be added.")
            if len(d["people"]) >= MAX_PEOPLE:
                raise OutreachError("The list is full; remove people you are done with.")
            p = {"id": secrets.token_hex(6), "name": name, "email": email, "company": company, "role": clean(raw.get("role"), 120),
                 "origin": origin, "source_url": link, "known_from": clean(raw.get("known_from"), 160), "contact_id": clean(raw.get("contact_id"), 40),
                 "added": self.today().isoformat(), "first": None, "followup": None, "replied": False, "opted_out": False}
            d["people"].append(p)
            self._save(d)
        return p

    def remove_person(self, pid: str) -> None:
        with self.lock:
            d = self._data()
            if not any(p["id"] == pid for p in d["people"]):
                raise NotFound("No such person.")
            d["people"] = [p for p in d["people"] if p["id"] != pid]
            self._save(d)

    def mark_replied(self, address: str) -> None:
        a = (address or "").strip().lower()
        with self.lock:
            d = self._data()
            hit = False
            for p in d["people"]:
                if p["email"].lower() == a:
                    p["replied"], hit = True, True
            if hit:
                self._save(d)

    def sent_addresses(self) -> dict[str, str]:
        """{address: name} for people already written to (so reply sorting can tell their answers apart). Local file only."""
        return {p["email"].lower(): p["name"] for p in self._data()["people"] if p.get("first") and p["first"].get("sent_at")}

    # picking candidates (guarded: all optional) -------------------------------------------------------------------------------
    def candidates(self) -> dict:
        contacts: list[dict] = []
        try:
            if self.networking_fn is not None:
                contacts = list(self.networking_fn())
            else:
                from . import networking
                contacts = networking.Networking(self.ctx).contacts()
        except Exception:  # noqa: BLE001
            contacts = []
        companies: list[dict] = []
        try:
            if self.watch_fn is not None:
                companies = list(self.watch_fn())
            else:
                from . import oppsources
                eng = oppsources.engine_for(self.ctx)
                companies = [{"label": s.get("label", ""), "url": (s.get("settings") or {}).get("url", "")} for s in eng.sources() if s.get("watch")]
        except Exception:  # noqa: BLE001
            companies = []
        return {"contacts": [{"id": clean(c.get("id"), 40), "name": clean(c.get("name"), 80), "company": clean(c.get("company"), 120),
                              "role": clean(c.get("role"), 120), "how_known": clean(c.get("how_known"), 160)} for c in contacts[:200] if isinstance(c, dict)],
                "companies": [{"label": clean(c.get("label"), 100), "url": public_link(str(c.get("url") or ""))} for c in companies[:100] if isinstance(c, dict)],
                "note": "Pick a contact or company to start, then type the address yourself. The hub never looks addresses up."}

    # drafting ---------------------------------------------------------------------------------------------------------------------
    def _facts(self, person: dict) -> tuple[str, str]:
        """(owner's name, ONE confirmed fact that fits the role, or ''). Never raises."""
        if self.facts_fn is not None:
            return self.facts_fn(person)
        try:
            from . import applyq, factbase
            from .factbase import job_terms, relevance
            fb = factbase.FactBase.build(applyq.load_profile(self.ctx) or {}, factbase.load(self.ctx), this_year=self.today().year)
            terms = job_terms({"title": person.get("role", ""), "description": person.get("company", "")}, fb)
            ranked = sorted(((relevance(fb, f, terms), f) for f in fb.of_type("bullet")), key=lambda x: -x[0])
            fact = ranked[0][1]["text"] if ranked and ranked[0][0] > 0 else ""
            return fb.name() or "", fact
        except Exception:  # noqa: BLE001
            return "", ""

    # day room ----------------------------------------------------------------------------------------------------------------------
    def room(self) -> dict:
        """How many more outreach emails may be queued today: the day limit minus what was sent and what sits in open batches."""
        from . import batches as bt
        stats = self.batches.day_stats()
        queued = sum(len(b["items"]) for b in self.batches.open_batches() if b.get("kind") == "outreach")
        limit = min(bt.outreach_day_limit(self.ctx.config), HARD_DAY)
        return {"limit": limit, "sent_today": stats["messages"], "queued": queued, "left": max(0, limit - stats["messages"] - queued)}

    def _item_for(self, person: dict, draft: dict, *, followup: bool, attach: bool) -> dict:
        raw = {"title": draft["subject"], "company": person["company"], "kind": "outreach", "channel": "email", "source": "outreach",
               "url": f"manual:outreach-{person['id']}-{secrets.token_hex(3)}", "contact_email": person["email"],
               "description": f"Outreach to {person['name']}", "meta": {"outreach_person": person["id"], "followup": followup, "attach_resume": attach}}
        item, _created = self.aq.add(raw, draft=False)
        item["draft"] = {"subject": draft["subject"], "body": draft["body"], "cover_note": "", "answers": {}, "missing": [], "facts_from": ["facts"] if draft["facts_used"] else [],
                         "generator": "outreach"}
        from . import applyq
        item["draft"]["missing"] = applyq.unresolved(item["draft"])
        item["state"], item["updated"] = "ready", self.stamp()
        item["contact_email"] = person["email"]
        self.aq.store.save(item)
        return item

    def _prepare(self, person_ids: list[str], *, followup: bool, attach: bool) -> dict:
        from .batches import BatchError
        ids = [i for i in dict.fromkeys(person_ids or []) if isinstance(i, str)]
        if not ids:
            raise OutreachError("Pick at least one person.")
        if len(ids) > HARD_BATCH:
            raise OutreachError(f"A batch holds at most {HARD_BATCH} people. Pick fewer.")
        with self.lock:
            d = self._data()
            by = {p["id"]: p for p in d["people"]}
            people = []
            for i in ids:
                p = by.get(i)
                if p is None:
                    raise NotFound("One of those people is no longer in your list.")
                if p.get("opted_out") or self.is_opted_out(p["email"]):
                    raise OutreachError(f"{p['name']} is on your opt-out list and is never written to.")
                if not valid_address(p["email"]):
                    raise OutreachError(f"{p['name']} has no valid address.")
                if followup:
                    if not p.get("first") or not p["first"].get("sent_at"):
                        raise OutreachError(f"You have not written to {p['name']} yet.")
                    if p.get("followup"):
                        raise OutreachError(f"{p['name']} already had their one follow-up.")
                    if p.get("replied"):
                        raise OutreachError(f"{p['name']} already replied.")
                elif p.get("first"):
                    raise OutreachError(f"{p['name']} already has a first email. Use a follow-up after {followup_days(self.ctx.config)} days.")
                people.append(p)
            room = self.room()
            if len(people) > room["left"]:
                raise OutreachError(f"Only {room['left']} more outreach emails fit today (limit {room['limit']}, {room['sent_today']} sent, {room['queued']} waiting in a batch).")
            items = []
            for p in people:
                my_name, fact = self._facts(p)
                draft = build_draft(p, my_name=my_name, fact="" if followup else fact, followup=followup,
                                    first_sent=human(p["first"]["sent_at"]) if followup else "")
                items.append(self._item_for(p, draft, followup=followup, attach=attach))
            try:
                view = self.batches.create_batch("outreach", [i["id"] for i in items], name="Follow-ups" if followup else "Personal outreach",
                                                 notes=["Each email ends with a line offering to stop.", f"Sent a few minutes apart, at most {HARD_DAY} a day."])
            except BatchError as exc:
                for i in items:                          # nothing is left half-made
                    self.aq.store.delete(i["id"])
                raise OutreachError(exc.message) from None
            for p, i in zip(people, items):
                p["queued"] = {"item_id": i["id"], "kind": "followup" if followup else "first", "batch_id": view["id"]}
            self._save(d)
        try:
            self.ctx.audit("outreach_prepared", batch=view["id"], people=len(people), followup=followup)
        except Exception:  # noqa: BLE001
            pass
        return view

    def prepare(self, person_ids: list[str], attach_resume: bool = False) -> dict:
        return self._prepare(person_ids, followup=False, attach=bool(attach_resume))

    def prepare_followups(self, person_ids: list[str], attach_resume: bool = False) -> dict:
        due = {f["id"] for f in self.followups_due()}
        bad = [i for i in person_ids if i not in due]
        if bad:
            raise OutreachError("One of those is not due for a follow-up yet.")
        return self._prepare(person_ids, followup=True, attach=bool(attach_resume))

    def discard_batch(self, bid: str) -> dict:
        view = self.batches.discard(bid)
        with self.lock:
            for card in view["items"]:
                it = self.aq.store.get(card["item_id"]) if card.get("item_id") else None
                if it is not None and it.get("state") in ("ready", "found"):
                    it["state"] = "skipped"
                    self.aq.store.save(it)
            d = self._data()
            for p in d["people"]:
                if (p.get("queued") or {}).get("batch_id") == bid:
                    p["queued"] = None
            self._save(d)
        return view

    def send(self, bid: str, confirm: Any) -> dict:
        """Starts the send of one reviewed outreach batch. Needs the owner's confirm; the batch engine does the rest."""
        if confirm is not True:
            raise OutreachError("Sending needs confirm: true (your tap on the reviewed list).")
        from .batches import BatchError
        try:
            b = self.batches.get(bid)
            if b.get("kind") != "outreach":
                raise OutreachError("That is not an outreach batch.")
            return self.batches.start_send(bid)
        except BatchError as exc:
            raise OutreachError(exc.message) from None

    # state sync (reads the queue's own file) ---------------------------------------------------------------------------------------
    def sync(self) -> None:
        with self.lock:
            d = self._data()
            changed = False
            for p in d["people"]:
                q = p.get("queued")
                if not q:
                    continue
                it = self.aq.store.get(q["item_id"])
                if it is None:
                    p["queued"], changed = None, True
                elif it.get("state") in ("sent", "replied"):
                    p["first" if q["kind"] == "first" else "followup"] = {"item_id": it["id"], "sent_at": it.get("sent_at") or self.stamp()}
                    p["queued"], changed = None, True
                elif it.get("state") == "skipped":
                    p["queued"], changed = None, True
            if changed:
                self._save(d)

    def _has_pending_reply(self, address: str) -> bool:
        try:
            r = self.replies_fn() if self.replies_fn else None
            if r is None:
                from . import replies
                r = replies.engine_for(self.ctx)
            return any(s.get("address") == address.lower() for s in r.pending())
        except Exception:  # noqa: BLE001
            return False

    def followups_due(self) -> list[dict]:
        self.sync()
        days, today, out = followup_days(self.ctx.config), self.today(), []
        for p in self._data()["people"]:
            f = p.get("first")
            if not f or p.get("followup") or p.get("replied") or p.get("opted_out") or p.get("queued") or self.is_opted_out(p["email"]):
                continue
            try:
                due = dt.date.fromisoformat(f["sent_at"][:10]) + dt.timedelta(days=days)
            except (ValueError, KeyError):
                continue
            if due <= today and not self._has_pending_reply(p["email"]):
                out.append({"id": p["id"], "name": p["name"], "company": p["company"], "first_sent": f["sent_at"][:10], "due": due.isoformat()})
        return sorted(out, key=lambda x: x["due"])

    # the screen ----------------------------------------------------------------------------------------------------------------------------
    def person_view(self, p: dict) -> dict:
        state = ("opted out" if p.get("opted_out") or self.is_opted_out(p["email"]) else "replied" if p.get("replied") else
                 "follow-up sent" if p.get("followup") else "waiting in a batch" if p.get("queued") else
                 "written to" if p.get("first") else "not written to yet")
        return {k: p.get(k) for k in ("id", "name", "email", "company", "role", "origin", "source_url", "known_from", "added")} | {"state": state}

    def overview(self) -> dict:
        from . import batches as bt
        self.sync()
        d = self._data()
        open_batches = []
        for b in self.batches.open_batches():
            if b.get("kind") == "outreach":
                open_batches.append(self.batches.view(b))
        return {"people": [self.person_view(p) for p in d["people"]], "optout": self.optout_list(), "followups_due": self.followups_due(),
                "batches": open_batches, "room": self.room(), "candidates": self.candidates(),
                "settings": {"hard_batch": HARD_BATCH, "hard_day": HARD_DAY, "day_limit": bt.outreach_day_limit(self.ctx.config),
                             "gap_seconds": bt.outreach_gap(self.ctx.config), "followup_days": followup_days(self.ctx.config), "enabled": enabled(self.ctx.config)},
                "rules": ["Nothing is sent until you tap Send on a reviewed batch.", "Addresses are only ever ones you typed. They are never guessed or looked up.",
                          f"At most {HARD_BATCH} people per batch and {HARD_DAY} emails per day, a few minutes apart.",
                          "Anyone on the opt-out list is never written to."]}


def human(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso[:10])
        return f"{d.day} {d.strftime('%B')}"
    except ValueError:
        return ""


def send_block(ctx: Ctx, item: dict) -> str:
    """Called by the batch sender before every outreach email. A sentence when this one must NOT go, "" when it may."""
    addr = str(item.get("contact_email") or "").strip().lower()
    if not valid_address(addr):
        return "No valid address on this one."
    if engine_for(ctx).is_opted_out(addr):
        return "This person is on your opt-out list, so nothing was sent."
    return ""


def engine_for(ctx: Ctx, **kw) -> Outreach:
    eng = getattr(ctx, "outreach", None)
    if eng is None or kw:
        eng = Outreach(ctx, **kw)
        try:
            ctx.outreach = eng
        except AttributeError:  # pragma: no cover
            pass
    return eng


# ---- routes ------------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, **kw) -> APIRouter:
    eng = engine_for(ctx, **kw)
    router = APIRouter(prefix="/api/outreach", dependencies=[Depends(ctx.need_auth)])
    router.service = eng      # type: ignore[attr-defined]

    def guard(fn):
        if not enabled(ctx.config):
            raise HTTPException(404, "Outreach is switched off (outreach_enabled is false).")
        try:
            return fn()
        except OutreachError as exc:
            raise HTTPException(400, str(exc)) from None
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None

    def pid(value: str) -> str:
        if not _ID_RE.match(value or ""):
            raise HTTPException(404, "No such person.")
        return value

    def ids(body: dict) -> list[str]:
        v = body.get("person_ids")
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise HTTPException(400, "person_ids is a list of person ids.")
        return v

    @router.get("")
    async def overview():
        return await asyncio.to_thread(lambda: guard(eng.overview))

    @router.post("/people")
    async def add(body: dict = Body(default_factory=dict)):
        return await asyncio.to_thread(lambda: guard(lambda: eng.person_view(eng.add_person(body))))

    @router.delete("/people/{person_id}")
    async def remove(person_id: str):
        guard(lambda: eng.remove_person(pid(person_id)))
        return {"ok": True}

    @router.post("/people/{person_id}/replied")
    async def replied(person_id: str):
        def run():
            p = next((p for p in eng._data()["people"] if p["id"] == pid(person_id)), None)
            if p is None:
                raise NotFound("No such person.")
            eng.mark_replied(p["email"])
            return {"ok": True}
        return guard(run)

    @router.post("/prepare")
    async def prepare(body: dict = Body(default_factory=dict)):
        """Drafts a batch for the people you picked. Sends nothing."""
        return await asyncio.to_thread(lambda: guard(lambda: eng.prepare(ids(body), body.get("attach_resume") is True)))

    @router.post("/followups/prepare")
    async def prepare_followups(body: dict = Body(default_factory=dict)):
        return await asyncio.to_thread(lambda: guard(lambda: eng.prepare_followups(ids(body), body.get("attach_resume") is True)))

    @router.post("/batches/{batch_id}/send")
    async def send(batch_id: str, body: dict = Body(default_factory=dict)):
        return await asyncio.to_thread(lambda: guard(lambda: eng.send(batch_id, body.get("confirm"))))

    @router.post("/batches/{batch_id}/discard")
    async def discard(batch_id: str):
        return await asyncio.to_thread(lambda: guard(lambda: eng.discard_batch(batch_id)))

    @router.get("/optout")
    async def optout():
        return {"optout": guard(eng.optout_list)}

    @router.post("/optout")
    async def add_optout(body: dict = Body(default_factory=dict)):
        return guard(lambda: eng.add_optout(address=str(body.get("address") or ""), person_id=str(body.get("person_id") or ""), name=str(body.get("name") or "")))

    @router.delete("/optout/{oid}")
    async def del_optout(oid: str):
        guard(lambda: eng.remove_optout(oid))
        return {"ok": True}

    return router


# ---- assistant tool (draft only) ---------------------------------------------------------------------------------------

def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    async def outreach_prepare(a):
        ids_ = a.get("person_ids")
        if not isinstance(ids_, list) or not ids_ or not all(isinstance(x, str) for x in ids_):
            ok, d = await hub.call("GET", "/api/outreach")
            if not ok:
                return False, d
            people = [p for p in d.get("people", []) if p["state"] == "not written to yet"][:20]
            if not people:
                return True, "Nobody is waiting. Add people on the Outreach screen first (you type each address yourself)."
            return True, "People you could write to (ids): " + "; ".join(f"{p['id']} {p['name']}" for p in people) + ". Call again with person_ids."
        ok, d = await hub.call("POST", "/api/outreach/prepare", body={"person_ids": ids_})
        if not ok:
            return False, d
        return True, (f"Drafted {len(d['items'])} emails in batch {d['id']}. NOTHING was sent. Open Apps > Outreach to read them and tap Send "
                      "(you stay in charge of the send).")

    tools = [ExtraTool({"name": "outreach_prepare",
                        "description": "Draft (never send) personal outreach emails for people the owner already added on the Outreach screen. Without person_ids it lists who can be drafted. "
                                       "At most 20 people. The owner reviews and sends from the screen.",
                        "input_schema": {"type": "object", "properties": {"person_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
                                         "required": [], "additionalProperties": False}},
                       outreach_prepare, needs_approval=False)]
    return mark_tools(tools, untrusted=("outreach_prepare",), mutating=("outreach_prepare",))
