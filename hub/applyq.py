"""APPLYQ: the ready-to-send application queue. See docs/api/applyq.md.

The job/gig finder (hub/opps.py) calls `add_item(ctx, item)`. Each item is drafted (from the owner's profile only,
never invented) and waits as "ready". NOTHING is ever sent automatically: the only code that sends is
`Applyq.send`, reached from `POST /api/applyq/{id}/send` (a tap in the app) or the assistant tool `send_application`
(which needs an approval, level N2). Adding, drafting, summaries and the proactive loop never send.

States: found -> drafting -> ready -> sent | applied | skipped | failed, plus replied (an answer came back).
Optional neighbours are imported defensively: `profile` (get_profile), `gmailx` (send_mail, list_accounts, search).
When they are missing the queue still works (drafts say what is missing, email sending offers "copy" instead).
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib
import json
import logging
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .common import Ctx
from . import panic as _panic

log = logging.getLogger("hub.applyq")

STATES = ("found", "drafting", "ready", "sent", "applied", "skipped", "failed", "replied")
CHANNELS = ("email", "web", "message")
MAX_TEXT = 8000
MAX_ITEMS = 2000
DEFAULT_CAP = 10
FILL = "[FILL: {}]"
_FILL_RE = re.compile(r"\[FILL: ([^\]]{1,60})\]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}")
_WORD = re.compile(r"[a-z0-9+#.]{2,}")
GENERIC_DOMAINS = frozenset("gmail.com googlemail.com outlook.com hotmail.com yahoo.com icloud.com proton.me protonmail.com "
                            "live.com aol.com".split())
STOP = frozenset("the and for with you your our are this that from will have has not but all can any who what job role "
                 "work team we to of in on at as an or be is it by us".split())
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class ApplyqError(ValueError):
    """A problem the owner can fix; the message is one plain sentence."""


def enabled(config) -> bool:
    return getattr(config, "applyq_enabled", True) is not False


def daily_cap(config) -> int:
    value = getattr(config, "applyq_daily_cap", DEFAULT_CAP)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else DEFAULT_CAP


# ---- helpers ---------------------------------------------------------------------------------------------------

def clean(value: Any, limit: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value).replace("\r", "")
    if multiline:
        return "\n".join(line.rstrip() for line in text.split("\n")).strip()[:limit]
    return " ".join(text.split())[:limit]


def norm_url(url: str) -> str:
    """One key per posting: lower-case host, no fragment, no tracking parameters, no trailing slash."""
    url = (url or "").strip()
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if not p.scheme or not p.netloc:
        return url
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in ("ref", "fbclid", "gclid", "source")])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "", query, ""))


def words(text: str) -> set[str]:
    return {w.strip(".") for w in _WORD.findall(str(text or "").lower()) if w.strip(".") and w not in STOP}


def find_email(text: str) -> str:
    for m in _EMAIL.findall(text or ""):
        if not re.match(r"(?:no-?reply|donotreply)@", m, re.I):
            return m.lower()
    return ""


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def now_iso(clock: Callable[[], dt.datetime]) -> str:
    return clock().isoformat(timespec="seconds")


# ---- the store -------------------------------------------------------------------------------------------------

class Store:
    """items(id, url, state, created, data JSON) in SQLite. One short connection per call (thread safe)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, "
                       "state TEXT NOT NULL, created TEXT NOT NULL, data TEXT NOT NULL)")

    @contextlib.contextmanager
    def _db(self):
        con = sqlite3.connect(str(self.path), timeout=10)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def count(self) -> int:
        with self.lock, self._db() as db:
            return db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def by_url(self, url: str) -> dict | None:
        with self.lock, self._db() as db:
            row = db.execute("SELECT data FROM items WHERE url=?", (norm_url(url),)).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, item_id: str) -> dict | None:
        with self.lock, self._db() as db:
            row = db.execute("SELECT data FROM items WHERE id=?", (item_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def insert(self, item: dict) -> None:
        with self.lock, self._db() as db:
            db.execute("INSERT INTO items(id,url,state,created,data) VALUES(?,?,?,?,?)",
                       (item["id"], norm_url(item["url"]), item["state"], item["created"], json.dumps(item)))

    def save(self, item: dict) -> None:
        with self.lock, self._db() as db:
            db.execute("UPDATE items SET state=?, data=? WHERE id=?", (item["state"], json.dumps(item), item["id"]))

    def list(self, states: tuple[str, ...] | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT data FROM items", []
        if states:
            sql += " WHERE state IN (%s)" % ",".join("?" * len(states))
            args += list(states)
        with self.lock, self._db() as db:
            rows = db.execute(sql + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete(self, item_id: str) -> bool:
        with self.lock, self._db() as db:
            return db.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount > 0


_STORES: dict[str, Store] = {}
_STORES_LOCK = threading.Lock()


def store_for(config) -> Store:
    path = Path(config.data_path()) / "applyq.sqlite"
    with _STORES_LOCK:
        store = _STORES.get(str(path))
        if store is None:
            store = _STORES[str(path)] = Store(path)
        return store


# ---- optional neighbours ---------------------------------------------------------------------------------------

def _optional(name: str):
    try:
        return importlib.import_module(f".{name}", __package__)
    except ImportError:
        return None


def load_profile(ctx) -> dict:
    """The owner's profile as a dict, {} when hub/profile.py is absent or fails."""
    mod = _optional("profile")
    fn = getattr(mod, "get_profile", None)
    if fn is None:
        return {}
    try:
        data = fn(ctx)
    except Exception:  # noqa: BLE001 - a broken profile must not break the queue
        log.warning("applyq: profile could not be read")
        return {}
    return data if isinstance(data, dict) else {}


def load_gmailx():
    return _optional("gmailx")


# ---- drafting --------------------------------------------------------------------------------------------------

def _as_list(value: Any) -> list:
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"[,\n;]", value) if s.strip()]
    return list(value) if isinstance(value, (list, tuple)) else []


def _experience_lines(profile: dict) -> list[tuple[str, str]]:
    """(role heading, statement) pairs from the profile's experience (strings, or dicts with bullets/summary)."""
    out: list[tuple[str, str]] = []
    for entry in _as_list(profile.get("experience")):
        if isinstance(entry, str):
            out.append(("", entry.strip()))
        elif isinstance(entry, dict):
            head = " at ".join(p for p in (str(entry.get("title") or entry.get("role") or "").strip(),
                                           str(entry.get("company") or "").strip()) if p)
            bullets = _as_list(entry.get("bullets") or entry.get("highlights") or entry.get("summary"))
            if bullets:
                out.extend((head, str(b)) for b in bullets)
            elif head:
                out.append(("", head))
    return [(clean(h, 120), clean(t, 300)) for h, t in out if clean(t, 300)]


def _answer_for(profile: dict, *needles: str) -> str:
    answers = profile.get("standard_answers")
    if isinstance(answers, dict):
        for key, value in answers.items():
            if any(n in str(key).lower() for n in needles) and str(value or "").strip():
                return clean(str(value), 500)
    return ""


def build_draft(item: dict, profile: dict) -> dict:
    """The template draft. Pure, no AI. Uses ONLY profile facts; anything unknown becomes a [FILL: ...] marker that
    is also listed in `missing`, so the owner fills it before sending."""
    profile = profile if isinstance(profile, dict) else {}
    job_words = words(" ".join(str(item.get(k) or "") for k in ("title", "company", "description")))
    skills = [str(s).strip() for s in _as_list(profile.get("skills")) if str(s).strip()]
    job_text = " ".join(str(item.get(k) or "") for k in ("title", "description")).lower()
    matched = [s for s in skills if (words(s) and words(s) <= job_words) or re.search(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])", job_text)]
    matched = list(dict.fromkeys(matched))[:8]
    scored = sorted(((len(words(text) & job_words), n, f"{head}: {text}" if head else text)
                     for n, (head, text) in enumerate(_experience_lines(profile))), key=lambda t: (-t[0], t[1]))
    bullets = [line for hits, _n, line in scored if hits > 0][:3]
    name = clean(str(profile.get("name") or ""), 80)
    summary = clean(str(profile.get("summary") or profile.get("headline") or ""), 600)
    links = [clean(str(x), 200) for x in _as_list(profile.get("links")) if str(x).strip()][:3]
    title, company = clean(item.get("title"), 160), clean(item.get("company"), 120)
    desc = str(item.get("description") or "").lower()
    missing: list[str] = []

    def need(label: str) -> str:
        if label not in missing:
            missing.append(label)
        return FILL.format(label)

    lines = [f"Hello{' ' + company + ' team' if company else ''},", ""]
    lines.append(f"I am writing about the {title or 'role'}" + (f" at {company}." if company else "."))
    lines.append(summary if summary else need("short introduction about you"))
    if matched:
        lines += ["", "Where my skills match this role: " + ", ".join(matched) + "."]
    if bullets:
        lines += ["", "Relevant experience:"] + [f"- {b}" for b in bullets]
    answers: dict[str, str] = {}
    standard = profile.get("standard_answers")
    if isinstance(standard, dict):
        answers = {clean(str(k), 120): clean(str(v), 800) for k, v in list(standard.items())[:30] if str(v or "").strip()}
    extras = []
    if re.search(r"\b(?:rate|salary|compensation|ctc|pay|budget)\b", desc):
        extras.append(("Expected rate", _answer_for(profile, "rate", "salary", "compensation", "ctc") or need("expected rate")))
    if re.search(r"\b(?:notice period|start date|availability|available|start)\b", desc):
        extras.append(("Availability", _answer_for(profile, "availab", "notice", "start") or need("availability")))
    if re.search(r"\b(?:portfolio|work samples?|github)\b", desc):
        extras.append(("Portfolio", links[0] if links else need("portfolio link")))
    if extras:
        lines.append("")
        lines += [f"{label}: {value}" for label, value in extras]
    if links and not any(l == "Portfolio" for l, _ in extras):
        lines += ["", "Links: " + ", ".join(links)]
    lines += ["", "Thank you for your time. I would be glad to talk.", "", name or need("your name")]
    body = "\n".join(lines)
    cover = clean(" ".join(p for p in (
        f"Applying for {title}" + (f" at {company}." if company else "."), summary or "",
        ("Relevant: " + ", ".join(matched) + ".") if matched else "") if p), 1000) or FILL.format("cover note")
    if cover == FILL.format("cover note"):
        missing.append("cover note")
    if not profile:
        missing.append("profile (not set up: nothing here comes from your profile)")
    return {"subject": clean(f"Application: {title}" + (f" - {name}" if name else ""), 200) or "Application",
            "body": body, "cover_note": cover, "answers": answers, "missing": list(dict.fromkeys(missing)),
            "facts_from": ["profile"] if profile else [], "generator": "template"}


def unresolved(draft: dict) -> list[str]:
    """[FILL: x] markers still present anywhere in the draft."""
    blob = " ".join([str(draft.get("subject") or ""), str(draft.get("body") or ""), str(draft.get("cover_note") or "")]
                    + [str(v) for v in (draft.get("answers") or {}).values()])
    return list(dict.fromkeys(_FILL_RE.findall(blob)))


def make_ai_draft_fn(ctx) -> Callable[[dict, dict, dict], dict | None]:
    """An optional AI rewrite of the template draft's wording. The model comes from `ctx.model_for("auto")`, which the
    server already wraps with the redaction hook (hub/redact.py) and never points at the paid Claude API for routine
    work. The prompt contains only the profile facts and the posting; the model is told not to add anything."""
    def run(item: dict, profile: dict, base: dict) -> dict | None:
        from .common import ask_json
        system = ("Rewrite an application message. Use ONLY the facts in PROFILE. Never invent experience, employers, "
                  "numbers or skills. Keep every [FILL: ...] marker exactly. Reply as JSON: {\"body\": str, \"cover_note\": str}.")
        user = json.dumps({"posting": {k: item.get(k) for k in ("title", "company", "description")},
                           "profile": {k: profile.get(k) for k in ("name", "summary", "skills", "experience")},
                           "template": {"body": base["body"], "cover_note": base["cover_note"]}})[:12000]
        data = asyncio.run(ask_json(ctx.model_for("auto"), system, user))
        if isinstance(data, dict) and isinstance(data.get("body"), str) and data["body"].strip():
            return {"body": clean(data["body"], MAX_TEXT, True), "cover_note": clean(data.get("cover_note"), 1500) or base["cover_note"]}
        return None
    return run


# ---- the service -----------------------------------------------------------------------------------------------

def infer_channel(item: dict) -> str:
    ch = str(item.get("channel") or "").lower()
    if ch in CHANNELS:
        return ch
    if item.get("contact_email"):
        return "email"
    if str(item.get("kind") or "").lower() in ("message", "dm", "gig_message"):
        return "message"
    return "web"


def clean_meta(raw: Any) -> dict:
    """Small JSON-safe extras from the finder (source id, job-preference match). Anything else is dropped."""
    if not isinstance(raw, dict):
        return {}
    try:
        blob = json.dumps(raw, default=str)
        return json.loads(blob) if len(blob) <= 4000 else {}
    except (TypeError, ValueError):
        return {}


class Applyq:
    def __init__(self, ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None,
                 profile_fn: Callable[[Any], dict] | None = None, gmailx: Any = None,
                 draft_fn: Callable[[dict, dict, dict], dict | None] | None = None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self.profile_fn = profile_fn or (lambda c: load_profile(c))
        self._gmailx = gmailx
        self.draft_fn = draft_fn
        self.store = store_for(ctx.config)

    @property
    def gmailx(self):
        return self._gmailx if self._gmailx is not None else load_gmailx()

    # -- adding (never sends) --------------------------------------------------------------------------------
    def add(self, raw: dict, *, draft: bool = True) -> tuple[dict, bool]:
        """(item, created). A URL that is already queued returns the existing item untouched."""
        if not isinstance(raw, dict):
            raise ApplyqError("An item is a dictionary.")
        url = clean(raw.get("url"), 1000)
        title = clean(raw.get("title"), 200)
        description = clean(raw.get("description"), MAX_TEXT, True)
        if not url:
            if not (title or description):
                raise ApplyqError("Give a link or some text.")
            url = "manual:" + hashlib.sha1((title + description).encode()).hexdigest()[:16]
        existing = self.store.by_url(url)
        if existing:
            return existing, False
        if self.store.count() >= MAX_ITEMS:
            raise ApplyqError("The queue is full; skip or delete old items first.")
        contact = clean(raw.get("contact_email"), 200).lower() or find_email(description)
        flags = [clean(str(f), 40) for f in _as_list(raw.get("flags"))][:10]
        score = raw.get("score")
        item = {"id": secrets.token_hex(6), "title": title or url, "company": clean(raw.get("company"), 120),
                "url": url, "source": clean(raw.get("source"), 60) or "finder", "kind": clean(raw.get("kind"), 30) or "job",
                "description": description, "score": float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0,
                "reason": clean(raw.get("reason"), 400), "flags": flags, "contact_email": contact if _EMAIL.fullmatch(contact or "") else "",
                "draft": None, "channel": "", "account": "", "state": "found", "created": now_iso(self.clock),
                "updated": now_iso(self.clock), "sent_at": None, "applied_at": None, "skipped_at": None,
                "replied_at": None, "notes": [], "error": ""}
        item["meta"] = clean_meta(raw.get("meta"))
        item["channel"] = infer_channel({**item, "channel": raw.get("channel")})
        item["account"] = clean(raw.get("account"), 80)
        self.store.insert(item)
        if draft:
            item = self.draft(item["id"])
            self._announce_ready(item)
        return item, True

    def _announce_ready(self, item: dict) -> None:
        if item.get("state") != "ready":
            return
        try:
            from .proactive import submit_or_notify
            submit_or_notify(self.ctx, {"type": "applyq_ready", "title": "Application ready: " + item["title"][:80],
                                        "body": "An application is ready for your review.", "link": "applyq", "source": "applyq"})
            from . import playbooks                      # playbooks: "when an application is ready, ..."
            playbooks.emit(self.ctx, "opportunity-ready", {"kind": "application", "item_id": str(item.get("id") or ""),
                                                           "title": str(item.get("title") or "")[:200], "text": "An application is ready for your review."})
        except Exception:  # noqa: BLE001
            log.warning("applyq: notification failed")

    # -- drafting (never sends) -------------------------------------------------------------------------------
    def draft(self, item_id: str, *, ai: bool = False) -> dict:
        item = self.get(item_id)
        if item["state"] in ("sent", "applied", "replied"):
            raise ApplyqError("This one is already sent or applied; its draft is kept as it was.")
        item["state"] = "drafting"
        self.store.save(item)
        profile = self.profile_fn(self.ctx) or {}
        try:
            base = build_draft(item, profile)
            fn = self.draft_fn or (make_ai_draft_fn(self.ctx) if ai else None)
            if fn is not None:
                try:
                    extra = fn(item, profile, base)
                except Exception:  # noqa: BLE001 - the template is always the fallback
                    log.warning("applyq: AI draft failed; using the template")
                    extra = None
                if isinstance(extra, dict):
                    for key, limit in (("subject", 200), ("body", MAX_TEXT), ("cover_note", 1500)):
                        if isinstance(extra.get(key), str) and extra[key].strip():
                            base[key] = clean(extra[key], limit, key != "subject")
                    base["generator"] = "ai"
                    base["missing"] = list(dict.fromkeys([*base["missing"], *unresolved(base)]))
            try:                                         # answer bank (hub/answerbank.py): extra answers for topics the posting mentions
                from . import answerbank
                for label, text in answerbank.draft_answers(self.ctx, item, profile).items():
                    base.setdefault("answers", {}).setdefault(label, text)
            except Exception:  # noqa: BLE001 - the bank is an extra
                pass
            item["draft"] = base
            item["state"] = "ready"
            item["error"] = ""
        except Exception as exc:  # noqa: BLE001
            log.exception("applyq: drafting failed")
            item["state"], item["error"] = "found", clean(str(exc), 200)
        item["updated"] = now_iso(self.clock)
        self.store.save(item)
        return item

    # -- reading ---------------------------------------------------------------------------------------------
    def get(self, item_id: str) -> dict:
        if not _ID_RE.match(item_id or ""):
            raise LookupError("No such application.")
        item = self.store.get(item_id)
        if not item:
            raise LookupError("No such application.")
        return item

    def public(self, item: dict) -> dict:
        out = dict(item)
        out["missing"] = unresolved(item["draft"]) if item.get("draft") else []
        return out

    def list(self, tab: str = "ready") -> list[dict]:
        groups = {"ready": ("found", "drafting", "ready", "failed"), "sent": ("sent", "applied", "replied"),
                  "skipped": ("skipped",), "all": None}
        if tab not in groups:
            raise ApplyqError("tab must be ready, sent, skipped or all.")
        items = self.store.list(groups[tab])
        if tab == "ready":
            items.sort(key=lambda i: -float(i.get("score") or 0))
        return [self.public(i) for i in items]

    # -- editing and marking (never sends) ---------------------------------------------------------------------
    def update(self, item_id: str, body: dict) -> dict:
        item = self.get(item_id)
        for key, limit in (("title", 200), ("company", 120)):
            if isinstance(body.get(key), str):
                item[key] = clean(body[key], limit) or item[key]
        if "contact_email" in body:
            value = clean(body.get("contact_email"), 200).lower()
            if value and not _EMAIL.fullmatch(value):
                raise ApplyqError("That is not an email address.")
            item["contact_email"] = value
        if "channel" in body:
            if body["channel"] not in CHANNELS:
                raise ApplyqError("channel must be email, web or message.")
            item["channel"] = body["channel"]
        if "account" in body:
            item["account"] = clean(body.get("account"), 80)
        if isinstance(body.get("draft"), dict):
            d = dict(item.get("draft") or {"subject": "", "body": "", "cover_note": "", "answers": {}})
            for key, limit in (("subject", 200), ("body", MAX_TEXT), ("cover_note", 1500)):
                if isinstance(body["draft"].get(key), str):
                    d[key] = clean(body["draft"][key], limit, key != "subject")
            if isinstance(body["draft"].get("answers"), dict):
                d["answers"] = {clean(str(k), 120): clean(str(v), 800) for k, v in list(body["draft"]["answers"].items())[:30]}
            d["missing"] = unresolved(d)
            d["generator"] = d.get("generator", "template")
            item["draft"] = d
        if isinstance(body.get("note"), str) and body["note"].strip():
            item["notes"] = [*item.get("notes", []), {"at": now_iso(self.clock), "text": clean(body["note"], 400)}][-20:]
        item["updated"] = now_iso(self.clock)
        self.store.save(item)
        return self.public(item)

    def mark(self, item_id: str, state: str) -> dict:
        item = self.get(item_id)
        stamp = now_iso(self.clock)
        if state == "applied":
            item["applied_at"] = stamp
        elif state == "skipped":
            item["skipped_at"] = stamp
        elif state == "ready":
            item["state"] = "ready" if item.get("draft") else "found"
        else:
            raise ApplyqError("Unsupported state.")
        if state != "ready":
            item["state"] = state
        item["updated"] = stamp
        self.store.save(item)
        return self.public(item)

    def formfill_result(self, item_id: str, applied: bool, note: str) -> dict:
        """Record the outcome of a form the owner submitted from the laptop (hub/formfill.py). `applied` only when the page
        said so; otherwise the item stays ready and carries a "needs check" flag. Never sends anything."""
        item = self.get(item_id)
        stamp = now_iso(self.clock)
        item["notes"] = [*item.get("notes", []), {"at": stamp, "text": clean(note, 400)}][-20:]
        flags = [f for f in item.get("flags", []) if f != "needs check"]
        if applied:
            item.update(state="applied", applied_at=stamp, error="")
        else:
            flags.append("needs check")
        item["flags"], item["updated"] = flags, stamp
        self.store.save(item)
        return self.public(item)

    # -- the ONE place that sends ---------------------------------------------------------------------------------
    def sent_today(self) -> int:
        today = self.clock().date().isoformat()
        return sum(1 for i in self.store.list(("sent", "replied"), 2000) if str(i.get("sent_at") or "")[:10] == today)

    def copy_pack(self, item: dict) -> dict:
        d = item.get("draft") or {}
        return {"to": item.get("contact_email") or "", "subject": d.get("subject", ""), "body": d.get("body", ""),
                "cover_note": d.get("cover_note", ""), "answers": d.get("answers", {}), "url": item["url"]}

    def send(self, item_id: str, *, confirm_over_cap: bool = False, account: str = "") -> dict:
        """Only called from POST /send and the approved assistant tool. Returns a result dict; never raises for
        'could not send' (the owner gets a sentence and the item stays ready or becomes failed)."""
        item = self.get(item_id)
        if item["state"] not in ("ready", "failed"):
            return {"ok": False, "error": f"This one is {item['state']}, so it cannot be sent."}
        draft = item.get("draft")
        if not draft:
            return {"ok": False, "error": "Draft it first."}
        channel = infer_channel(item)
        if channel != "email":
            return {"ok": True, "action": "copy" if channel == "message" else "open", "channel": channel,
                    "sent": False, "copy": self.copy_pack(item),
                    "message": "Nothing was sent. Copy the text, apply yourself, then tap I applied."}
        left = unresolved(draft)
        if left:
            return {"ok": False, "error": "Fill in the missing info first: " + ", ".join(left) + "."}
        if not item.get("contact_email"):
            return {"ok": False, "error": "This posting has no apply-by-email address. Add one, or use the web channel."}
        cap = daily_cap(self.ctx.config)
        if cap and self.sent_today() >= cap and not confirm_over_cap:
            return {"ok": False, "needs_confirm": True,
                    "error": f"You already sent {self.sent_today()} today (cap {cap}). Confirm to send anyway."}
        mail = self.gmailx
        send_mail = getattr(mail, "send_mail", None)
        if send_mail is None:
            return {"ok": False, "reason": "no_gmail", "copy": self.copy_pack(item),
                    "error": "Gmail sending is not set up on this hub. Copy the message and send it yourself."}
        acct = clean(account, 80) or item.get("account") or ""
        if not acct:
            accounts = getattr(mail, "list_accounts", None)
            try:
                listed = accounts(self.ctx) if accounts else []
            except Exception:  # noqa: BLE001
                listed = []
            acct = str(listed[0].get("id")) if listed and isinstance(listed[0], dict) else ""
        try:
            result = send_mail(self.ctx, acct, item["contact_email"], draft["subject"], draft["body"], None)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": clean(str(exc), 200) or "Gmail failed."}
        if not isinstance(result, dict) or not result.get("ok"):
            err = clean(str((result or {}).get("error") if isinstance(result, dict) else "") or "Gmail did not send it.", 300)
            item["state"], item["error"], item["updated"] = "failed", err, now_iso(self.clock)
            self.store.save(item)
            return {"ok": False, "error": err, "copy": self.copy_pack(item)}
        item.update(state="sent", account=acct, sent_at=now_iso(self.clock), updated=now_iso(self.clock), error="")
        self.store.save(item)
        with contextlib.suppress(Exception):
            self.ctx.audit("applyq_send", id=item["id"], channel="email", to_domain=domain_of(item["contact_email"]))
        return {"ok": True, "sent": True, "channel": "email", "item": self.public(item),
                "message": "Sent. Email cannot be undone."}

    # -- replies ---------------------------------------------------------------------------------------------
    def check_replies(self) -> dict:
        mail = self.gmailx
        search = getattr(mail, "search", None)
        if search is None:
            return {"checked": 0, "replied": [], "available": False}
        hits, checked = [], 0
        for item in self.store.list(("sent",), 500):
            to = item.get("contact_email") or ""
            if not to:
                continue
            checked += 1
            domain = domain_of(to)
            query = f"from:{to}" if domain in GENERIC_DOMAINS else f"from:{domain}"
            try:
                messages = search(self.ctx, item.get("account") or "", query) or []
            except Exception:  # noqa: BLE001
                continue
            if self._matches(item, messages):
                item["state"], item["replied_at"] = "replied", now_iso(self.clock)
                item["updated"] = item["replied_at"]
                self.store.save(item)
                hits.append(item["id"])
        return {"checked": checked, "replied": hits, "available": True}

    @staticmethod
    def _matches(item: dict, messages: list) -> bool:
        to = item.get("contact_email") or ""
        domain = domain_of(to)
        subject = str((item.get("draft") or {}).get("subject") or "").lower()
        for m in messages:
            if not isinstance(m, dict):
                continue
            sender = str(m.get("from") or m.get("sender") or "").lower()
            sender_addr = find_email(sender) or sender
            subj = str(m.get("subject") or "").lower()
            if domain in GENERIC_DOMAINS:
                if sender_addr == to:
                    return True
            elif domain_of(sender_addr) == domain or sender_addr.endswith("." + domain):
                return True
            if subject and subject in subj:
                return True
        return False

    # -- counters ----------------------------------------------------------------------------------------------
    def summary(self) -> dict:
        today = self.clock().date()
        items = self.store.list(None, 5000)

        def on(field: str, day: dt.date) -> int:
            return sum(1 for i in items if str(i.get(field) or "")[:10] == day.isoformat())

        def within(field: str, days: int) -> int:
            keys = {(today - dt.timedelta(days=n)).isoformat() for n in range(days)}
            return sum(1 for i in items if str(i.get(field) or "")[:10] in keys)
        ready = sum(1 for i in items if i["state"] == "ready")
        streak, day = 0, today
        active = lambda d: on("sent_at", d) + on("applied_at", d) > 0  # noqa: E731
        if not active(day):
            day -= dt.timedelta(days=1)
        while active(day) and streak < 3650:
            streak += 1
            day -= dt.timedelta(days=1)
        return {"today": {"applied": on("applied_at", today), "sent": on("sent_at", today), "ready": ready,
                          "skipped": on("skipped_at", today), "replied": on("replied_at", today)},
                "week": {"applied": within("applied_at", 7), "sent": within("sent_at", 7), "skipped": within("skipped_at", 7),
                         "replied": within("replied_at", 7)},
                "streak": streak, "cap": daily_cap(self.ctx.config), "sent_today": self.sent_today()}

    def daily_line(self) -> str:
        s = self.summary()["today"]
        return f"Today: {s['applied'] + s['sent']} applied, {s['ready']} ready, {s['replied']} replies"


# ---- hooks for the finder, Today and Friday --------------------------------------------------------------------

def add_item(ctx, item: dict) -> dict | None:
    """For hub/opps.py. Queues one finding (deduped by URL), drafts it, returns the stored item or None when the
    queue is off or the item is unusable. Never sends and never raises."""
    try:
        if not enabled(ctx.config):
            return None
        stored, _created = Applyq(ctx).add(item)
        return stored
    except ApplyqError:
        return None
    except Exception:  # noqa: BLE001
        log.exception("applyq: add_item failed")
        return None


def today_line(config) -> str:
    """One short line for Today / the front page, "" when the queue is off or empty. Never raises."""
    try:
        if not enabled(config):
            return ""
        ctx = Ctx(config=config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None,
                  audit=lambda *a, **k: None)
        s = Applyq(ctx).summary()
        t = s["today"]
        if not (t["ready"] or t["sent"] or t["applied"] or t["replied"]):
            return ""
        return f"Applications today: {t['applied'] + t['sent']} sent, {t['ready']} ready, {t['replied']} replies"
    except Exception:  # noqa: BLE001
        return ""


# ---- the router ------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None, profile_fn=None, gmailx=None,
                draft_fn=None) -> APIRouter:
    router = APIRouter(prefix="/api/applyq", dependencies=[Depends(ctx.need_auth)])
    svc = Applyq(ctx, clock=clock, profile_fn=profile_fn, gmailx=gmailx, draft_fn=draft_fn)
    router.service = svc                          # type: ignore[attr-defined]

    def need_on() -> None:
        if not enabled(ctx.config):
            raise HTTPException(404, "The application queue is switched off (applyq_enabled is false).")

    def guard(fn):
        try:
            return fn()
        except ApplyqError as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("")
    async def listing(tab: str = Query("ready", max_length=10)):
        need_on()
        items = await asyncio.to_thread(lambda: guard(lambda: svc.list(tab)))
        return {"items": items, "count": len(items), "summary": await asyncio.to_thread(svc.summary)}

    @router.get("/summary")
    async def summary():
        need_on()
        return await asyncio.to_thread(svc.summary)

    @router.get("/accounts")
    async def accounts():
        need_on()
        fn = getattr(svc.gmailx, "list_accounts", None)
        if fn is None:
            return {"accounts": [], "gmail": False}
        try:
            return {"accounts": list(await asyncio.to_thread(fn, ctx)), "gmail": True}
        except Exception:  # noqa: BLE001
            return {"accounts": [], "gmail": False}

    @router.post("/manual")
    async def manual(body: dict = Body(default_factory=dict)):
        need_on()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "Paste a link or some text.")
        text = text.strip()[: MAX_TEXT + 1000]
        m = re.match(r"^(https?://\S+)\s*([\s\S]*)$", text)
        raw = {"url": m.group(1), "description": m.group(2), "title": (m.group(2).split("\n", 1)[0] if m.group(2) else "")[:120],
               "source": "manual", "kind": "manual", "score": 0} if m else \
            {"title": text.split("\n", 1)[0][:120], "description": text, "source": "manual", "kind": "manual", "score": 0}
        for key in ("company", "contact_email", "channel"):
            if isinstance(body.get(key), str):
                raw[key] = body[key]
        item, created = await asyncio.to_thread(lambda: guard(lambda: svc.add(raw)))
        return {"item": svc.public(item), "created": created}

    @router.post("/check-replies")
    async def check_replies():
        need_on()
        return await asyncio.to_thread(svc.check_replies)

    @router.get("/{item_id}")
    async def one(item_id: str):
        need_on()
        return svc.public(guard(lambda: svc.get(item_id)))

    @router.patch("/{item_id}")
    async def patch(item_id: str, body: dict = Body(default_factory=dict)):
        need_on()
        return await asyncio.to_thread(lambda: guard(lambda: svc.update(item_id, body)))

    @router.post("/{item_id}/draft")
    async def draft(item_id: str, body: dict = Body(default_factory=dict)):
        need_on()
        item = await asyncio.to_thread(lambda: guard(lambda: svc.draft(item_id, ai=body.get("ai") is True)))
        return svc.public(item)

    @router.post("/{item_id}/send")
    async def send(item_id: str, body: dict = Body(default_factory=dict)):
        need_on()
        _panic.guard(ctx.config)
        result = await asyncio.to_thread(lambda: guard(lambda: svc.send(
            item_id, confirm_over_cap=body.get("confirm_over_cap") is True,
            account=body.get("account") if isinstance(body.get("account"), str) else "")))
        if result.get("needs_confirm"):
            return JSONResponse({**result, "detail": result["error"]}, status_code=409)
        return result

    @router.post("/{item_id}/applied")
    async def applied(item_id: str):
        need_on()
        return await asyncio.to_thread(lambda: guard(lambda: svc.mark(item_id, "applied")))

    @router.post("/{item_id}/skip")
    async def skip(item_id: str):
        need_on()
        return await asyncio.to_thread(lambda: guard(lambda: svc.mark(item_id, "skipped")))

    @router.post("/{item_id}/restore")
    async def restore(item_id: str):
        need_on()
        return await asyncio.to_thread(lambda: guard(lambda: svc.mark(item_id, "ready")))

    @router.delete("/{item_id}")
    async def delete(item_id: str):
        need_on()
        guard(lambda: svc.get(item_id))
        svc.store.delete(item_id)
        return {"ok": True}

    return router
