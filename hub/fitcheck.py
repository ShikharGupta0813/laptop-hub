"""Fit and honesty check. See docs/api/tailor.md.

Two jobs, both pure functions over a `FactBase` (hub/factbase.py) and a posting:

1. `analyse(fb, job)`: the posting's requirements against the owner's facts (matched / partial / missing, each with the
   fact ids that back it), a 0-100 score, and flags: over- or under-qualified, location / timezone / pay mismatch,
   visa and work-authorization needs, and scam signals (reuses `hub/opps_vet.py`). A check that cannot run because the
   owner has not set the matching preference is listed under `unchecked`, never guessed.
2. `check_claims(fb, sentences, job)`: every drafted sentence must cite facts, and the words that matter (numbers,
   names, tool names, anything that sounds like an experience claim) must appear in the facts it cites. A sentence that
   fails is `unbacked`; the draft is BLOCKED (`blocked: true`) until the owner removes it, rewrites it, or confirms it
   as a fact. This is what stops an invented claim from reaching an application.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .factbase import FILLER, STOP, FactBase, canon, in_pool, signal_tokens, token_pairs, tokens, _TOKEN

# ---- requirements -------------------------------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*•▪●◦‣·]|\d+[.)])\s+")
_SECTION = [
    ("must", re.compile(r"^(?:requirements?|qualifications?|what you(?:'ll| will)? (?:need|bring)|who you are|you have|you are|must[- ]haves?|"
                        r"minimum qualifications?|required (?:skills|qualifications|experience)|about you|skills? (?:and|&) experience|what we(?:'re| are) looking for)\b", re.I)),
    ("nice", re.compile(r"^(?:nice[- ]to[- ]haves?|bonus|preferred|preferred qualifications?|a plus|pluses|extra points|good to have)\b", re.I)),
    ("duty", re.compile(r"^(?:responsibilit\w+|what you(?:'ll| will)? do|the role|your (?:role|day|impact)|day[- ]to[- ]day|duties|in this role|about the (?:role|job|project)|scope)\b", re.I)),
    ("other", re.compile(r"^(?:about (?:us|the company|the team)|who we are|benefits|perks|we offer|what we offer|compensation|how to apply|our (?:mission|culture|values)|why join)\b", re.I)),
]
_REQ_HINT = re.compile(r"\b(?:years?|yrs?|experience (?:with|in|of)|proficien\w+|knowledge of|familiar\w*|degree|bachelor|master|phd|must|required|"
                       r"fluent|expertise|skilled|hands[- ]on|comfortable|background in|ability to|able to|certified|certification)\b", re.I)
_NICE_HINT = re.compile(r"\b(?:nice to have|preferred|a plus|bonus|ideally|desirable|advantage|would be great|is a plus)\b", re.I)
_MUST_HINT = re.compile(r"\b(?:must|required|essential|mandatory|need to|minimum)\b", re.I)
_META_LINE = re.compile(r"^\s*(?:location|salary|pay|compensation|benefits?|perks|contract|employment type|schedule|hours|rate|budget|duration|deadline|how to apply|apply)\s*[:\-–]", re.I)
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
_DEGREE = re.compile(r"\b(?:degree|bachelor'?s?|bsc|b\.?s\.?|b\.?a\.?|master'?s?|msc|m\.?s\.?|mba|phd|doctorate|diploma|graduate)\b", re.I)
_EQUIV = re.compile(r"\bor (?:equivalent|related|relevant|comparable)\b|\bequivalent (?:practical )?experience\b", re.I)
_LEVELS = [(4, re.compile(r"\b(?:phd|doctorate|doctoral)\b", re.I)), (3, re.compile(r"\b(?:master'?s?|msc|m\.?s\.?c?|mba)\b", re.I)),
           (2, re.compile(r"\b(?:bachelor'?s?|bsc|b\.?s\.?c?|b\.?a\.?|b\.?eng|b\.?tech|undergraduate)\b", re.I)),
           (1, re.compile(r"\b(?:diploma|associate'?s?|certificate)\b", re.I))]


def _level(text: str) -> int:
    return max((lvl for lvl, rx in _LEVELS if rx.search(text or "")), default=0)


def extract_requirements(job: dict) -> list[dict]:
    """[{id, text, must, kind: skill|years|education|other, terms: [canonical words], years: int|None}] from the posting text."""
    text = str(job.get("description") or "")
    lines: list[tuple[str, str]] = []          # (section, text)
    section = "other"
    for raw in re.split(r"\r?\n", text):
        line = raw.strip()
        if not line:
            continue
        head = re.sub(r"[:\-–—*#_]+\s*$", "", _BULLET.sub("", line)).strip()
        hit = next((name for name, rx in _SECTION if rx.match(head) and len(head) < 70), None)
        if hit and (len(head) < 60 and (raw.rstrip().endswith(":") or not _BULLET.match(raw) or len(head.split()) <= 6)) and len(line) < 80:
            section = hit
            continue
        if _BULLET.match(raw) or len(line) < 400:
            lines.append((section, _BULLET.sub("", line).strip()))
        else:
            for sent in re.split(r"(?<=[.!?])\s+", line):
                lines.append((section, sent.strip()))
    # Posting written as one paragraph: split into sentences.
    if "\n" not in text.strip():
        lines = [("other", s.strip()) for s in re.split(r"(?<=[.!?])\s+|;\s+", text.replace("\n", " ")) if s.strip()]
    reqs: list[dict] = []
    for sec, line in lines:
        if not 6 <= len(line) <= 400 or sec == "other" and not _REQ_HINT.search(line) or _META_LINE.match(line):
            continue
        if sec == "duty" and not _REQ_HINT.search(line):
            continue
        nice = sec == "nice" or bool(_NICE_HINT.search(line))
        must = (sec == "must" or bool(_MUST_HINT.search(line))) and not nice
        if sec == "duty":
            must = False
        ym = _YEARS.search(line)
        years = int(ym.group(1)) if ym else None
        kind = "years" if years is not None else "education" if _DEGREE.search(line) else "skill"
        pairs = [(c, raw) for c, raw in token_pairs(_YEARS.sub(" ", line)) if c not in FILLER and not c.isdigit() and len(c) > 1]
        terms = sorted({c for c, _ in pairs})
        reqs.append({"id": f"r{len(reqs) + 1}", "text": line[:300], "must": must, "nice": nice, "kind": kind, "terms": terms, "years": years,
                     "section": sec, "words": {c: raw for c, raw in pairs}})
        if len(reqs) >= 40:
            break
    if not reqs:       # no requirement-shaped lines at all: use the first sentences so the score still says something
        for s in [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if len(s.strip()) > 12][:8]:
            pairs = [(c, raw) for c, raw in token_pairs(s) if c not in FILLER and not c.isdigit() and len(c) > 1]
            reqs.append({"id": f"r{len(reqs) + 1}", "text": s[:300], "must": False, "nice": False, "kind": "skill",
                         "terms": sorted({c for c, _ in pairs}), "years": None, "section": "other", "words": {c: raw for c, raw in pairs}})
    return reqs


def _skill_years(fb: FactBase, term: str) -> tuple[int, str | None]:
    """(years, skill fact id) the facts support for one word; (0, id) when listed but without dated evidence; (-1, None) when absent."""
    best = (-1, None)
    for f in fb.of_type("skill"):
        if term in fb.tokens_of(f):
            if f.get("years", 0) > best[0] or best[0] < 0:
                best = (f.get("years", 0), f["id"])
    if best[0] >= 0:
        return best
    roles = []
    for b in fb.of_type("bullet"):
        if term in fb.tokens_of(b) and b.get("role_id") in fb.by_id and fb.by_id[b["role_id"]] not in roles:
            roles.append(fb.by_id[b["role_id"]])
    if roles:
        return fb._years_of(roles), roles[0]["id"]
    return -1, None


def match_requirement(fb: FactBase, req: dict) -> dict:
    out = dict(req)
    ev: list[str] = []
    terms = [fb.resolve(t) for t in req["terms"]]
    shown = {fb.resolve(c): raw for c, raw in (req.get("words") or {}).items()}
    owner_years = fb.years_experience()
    if req["kind"] == "years":
        need = req["years"] or 0
        found = []
        for t in terms:
            y, sid = _skill_years(fb, t)
            if y >= 0:
                found.append((y, sid, t))
        if terms and not found:
            out.update(status="missing", note="No fact mentions " + ", ".join(shown.get(t, t) for t in terms[:4]) + ".")
        elif not terms:
            ok = owner_years >= need
            out.update(status="matched" if ok else "partial" if owner_years >= 0.7 * need and owner_years else "missing",
                       note=f"You have {owner_years} years in total; they ask {need}.", have_years=owner_years)
            ev = [r["id"] for r in fb.roles()][:3]
        else:
            y, sid, term = max(found, key=lambda x: x[0])
            ev = [sid] if sid else []
            if y >= need:
                out.update(status="matched", note=f"{y} years with {term} in your history; they ask {need}.", have_years=y)
            elif y == 0 or y >= 0.7 * need:
                out.update(status="partial", note=f"{term} is in your facts, with {y} years you can show; they ask {need}.", have_years=y)
            else:
                out.update(status="missing", note=f"Only {y} years with {term} show in your history; they ask {need}.", have_years=y)
    elif req["kind"] == "education":
        need = _level(req["text"]) or 2
        have = max((_level(f["text"]) for f in fb.of_type("education")), default=0)
        ev = [f["id"] for f in fb.of_type("education") if _level(f["text"])][:2]
        if have >= need:
            out.update(status="matched", note="Education level meets it.")
        elif _EQUIV.search(req["text"]):
            out.update(status="partial", note="They accept equivalent experience.")
            ev = ev or [r["id"] for r in fb.roles()][:2]
        else:
            out.update(status="partial" if have else "missing", note="Your education facts do not show this level." if have else "No education fact on file.")
    else:
        if not terms:
            out.update(status="info", note="")
        else:
            index = fb.index_tokens()
            covered = [t for t in terms if t in index]
            ratio = len(covered) / len(terms)
            facts = sorted(((len(set(covered) & fb.tokens_of(f)), f["id"]) for f in fb.facts if f["type"] != "pref"), reverse=True)
            ev = [i for n, i in facts if n][:3]
            distinct = [t for t in covered if re.search(r"[0-9+#]", t) or len(t) >= 7]
            if ratio >= 0.6:
                out["status"] = "matched"
            elif ratio >= 0.25 or distinct:
                out["status"] = "partial"
            else:
                out["status"] = "missing"
            miss = [shown.get(t, t) for t in terms if t not in index]
            out["note"] = ("Not in your facts: " + ", ".join(miss[:6])) if miss and out["status"] != "matched" else ""
    out["evidence"] = ev
    return out


# ---- flags ---------------------------------------------------------------------------------------------------------

SENIORITY = [(re.compile(r"\b(?:intern|internship|trainee|apprentice)\b", re.I), (0, 1)),
             (re.compile(r"\b(?:junior|jr|entry[- ]level|graduate)\b", re.I), (0, 2)),
             (re.compile(r"\b(?:mid[- ]level|intermediate)\b", re.I), (2, 5)),
             (re.compile(r"\b(?:senior|sr)\b", re.I), (5, None)),
             (re.compile(r"\b(?:lead|staff)\b", re.I), (7, None)),
             (re.compile(r"\b(?:principal|director|head of|vp|vice president)\b", re.I), (10, None))]
TZ_ABBR = {"utc": 0, "gmt": 0, "wet": 0, "bst": 1, "cet": 1, "cest": 2, "eet": 2, "eest": 3, "msk": 3, "ist": 5.5, "pkt": 5, "sgt": 8, "hkt": 8, "jst": 9, "kst": 9,
           "aest": 10, "aedt": 11, "nzst": 12, "nzdt": 13, "est": -5, "edt": -4, "cst": -6, "cdt": -5, "mst": -7, "mdt": -6, "pst": -8, "pdt": -7,
           "akst": -9, "hst": -10, "brt": -3, "art": -3}
_TZ = re.compile(r"\b(?:(?:UTC|GMT)\s?([+-]\d{1,2})(?::?(\d\d))?|(" + "|".join(a.upper() for a in TZ_ABBR) + r"))\b")
_REMOTE = re.compile(r"\b(?:remote|work from home|wfh|fully distributed|anywhere)\b", re.I)
_ONSITE = re.compile(r"\b(?:on[- ]?site|in[- ]office|hybrid|in[- ]person|office[- ]based|relocation)\b", re.I)
_REGION = re.compile(r"(?:must (?:be )?(?:located|based|reside|resident|live)s? in|(?:located|based|resident|residing) in|residents? of|only (?:open )?to (?:candidates )?(?:in|from))\s+(?:the\s+)?"
                     r"([A-Z][A-Za-z.]+(?:[ ,/]+(?:and |or )?[A-Z][A-Za-z.]+){0,3})")
_REGION_ONLY = re.compile(r"\b([A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+)?)[- ](?:only|based candidates|residents only|applicants only)\b")
_LOCATION_FIELD = re.compile(r"(?:^|\n)\s*(?:location|where|office|city)\s*[:\-]\s*([^\n]{2,80})", re.I)
_AUTH = re.compile(r"authori[sz]ed to work|work authori[sz]ation|right to work|eligib\w+ to work|legally (?:able|entitled|eligible|permitted) to work|"
                   r"(?:must|need to) be an? (?:\w+\.?\s?){0,2}citizen|citizens? only|security clearance|work permit|visa", re.I)
_NO_SPONSOR = re.compile(r"(?:no|not|cannot|can't|unable to|do not|does not|don't|doesn't|will not|won't|without)\s+(?:\w+\s+){0,3}?(?:visa\s+)?sponsor\w*|"
                         r"sponsorship (?:is )?(?:not|unavailable)|must not require (?:visa )?sponsorship|(?:will|do|can) not (?:provide )?sponsor", re.I)
_SPONSOR_OK = re.compile(r"(?:visa )?sponsorship (?:is )?(?:available|provided|offered)|we (?:can |will )?sponsor|relocation (?:assistance|package|support)", re.I)
PERIOD_TO_YEAR = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1, "yr": 1, "annum": 1, "annual": 1}


_PAY = re.compile(r"(?P<cur>[$€£₹]|usd|eur|gbp|inr)\s?(?P<a>\d[\d,]*(?:\.\d+)?)\s?(?P<ak>k)?\b(?:\s?(?:-|–|to)\s?(?:[$€£₹]|usd|eur|gbp|inr)?\s?(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?P<bk>k)?\b)?"
                  r"(?:\s?(?:usd|eur|gbp|inr))?(?:\s?(?:/|per|an?|each|every|p\.?)\s?(?P<per>hour|hr|day|week|wk|month|mo|year|yr|annum|annually)\b)?", re.I)
_PER = {"hr": "hour", "wk": "week", "mo": "month", "yr": "year", "annum": "year", "annually": "year"}


def pay_amounts(text: str) -> list[tuple[float, str]]:
    """[(top amount as written, period)] for each pay figure; a figure without a period counts as yearly when it is 1,000 or more.
    Currencies are NOT converted: the owner's minimum is compared as written."""
    out = []
    for m in _PAY.finditer(text or ""):
        try:
            a = float(m.group("a").replace(",", ""))
            b = float(m.group("b").replace(",", "")) if m.group("b") else None
        except ValueError:
            continue
        k_a, k_b = bool(m.group("ak")), bool(m.group("bk"))
        if b is not None and k_b and not k_a and a < 1000:
            k_a = True                       # "90-110k": both are thousands
        if b is not None and k_a and not k_b and b < 1000:
            k_b = True
        hi = max(a * (1000 if k_a else 1), (b or 0) * (1000 if k_b else 1))
        per = (m.group("per") or "").lower()
        per = _PER.get(per, per)
        if not per:
            if hi < 1000:
                continue
            per = "year"
        out.append((hi, per))
    return out


def _flag(code: str, label: str, severity: str, detail: str = "", source: str = "fit") -> dict:
    return {"code": code, "label": label, "severity": severity, "detail": detail, "source": source}


def _geo_overlap(text: str, places: list[str]) -> bool:
    want = set(tokens(text))
    for p in places:
        if want & set(tokens(p)):
            return True
    return False


def _flags(fb: FactBase, job: dict, reqs: list[dict]) -> tuple[list[dict], list[str]]:
    prefs = fb.prefs
    flags: list[dict] = []
    unchecked: list[str] = []
    title = str(job.get("title") or "")
    text = " ".join(str(job.get(k) or "") for k in ("title", "description", "location", "company"))
    years = fb.years_experience()
    # seniority
    for rx, (low, high) in SENIORITY:
        hit = rx.search(title)
        if hit:
            if high is not None and years >= max(6, 3 * high):
                flags.append(_flag("over_qualified", "You may be over-qualified", "low", f"The title says '{hit.group(0)}'; you have {years} years of dated experience."))
            if low >= 5 and years and years < low * 0.6:
                flags.append(_flag("under_qualified", "The level may be above yours", "medium", f"Roles with this title usually want {low}+ years; your dated history covers {years}."))
            break
    stated = [r["years"] for r in reqs if r["kind"] == "years" and r["years"] and not r["terms"]]
    if stated and years and max(stated) > years * 1.3 + 1:
        flags.append(_flag("under_qualified", "Asks for more years than you can show", "medium", f"They ask {max(stated)}+ years; your dated history covers {years}."))
    if stated and years and max(stated) <= 3 and years >= 3 * max(stated) + 3:
        flags.append(_flag("over_qualified", "You have far more experience than they ask", "low", f"They ask {max(stated)} years; you have {years}."))
    # location
    remote, onsite = bool(_REMOTE.search(text)), bool(_ONSITE.search(text))
    where = (str(job.get("location") or "").strip() or ((_LOCATION_FIELD.search(str(job.get("description") or "")) or [None, ""])[1]).strip())
    owner_places = [p for p in [prefs.get("location", "")] + list(prefs.get("work_auth") or []) if p]
    if not prefs.get("location"):
        unchecked.append("location (set your location in Preferences)")
    elif where and not _geo_overlap(where, owner_places) and not (remote and prefs.get("remote_ok", True) and not _REGION.search(where)):
        if not prefs.get("relocate"):
            flags.append(_flag("location_mismatch", "Posting is for a different place", "medium" if (onsite or not remote) else "low", f"{where} vs your {prefs['location']}"))
    elif onsite and not remote and not where and not prefs.get("relocate"):
        flags.append(_flag("onsite_unknown_place", "On-site or hybrid; the place is not stated", "low", "Check the location before applying."))
    if remote and not prefs.get("remote_ok", True):
        flags.append(_flag("remote_not_wanted", "Remote role, and you asked for non-remote", "low", ""))
    restr = [m.group(1).strip(" ,") for m in _REGION.finditer(str(job.get("description") or ""))] + [m.group(1) for m in _REGION_ONLY.finditer(str(job.get("description") or ""))]
    restr = [r for r in restr if r.lower() not in ("this", "the", "our", "we", "you", "i")]
    if restr and owner_places and not any(_geo_overlap(r, owner_places) for r in restr):
        flags.append(_flag("region_restriction", "Restricted to a region you did not list", "medium", f"Posting says: {restr[0]}. You listed: {', '.join(owner_places[:3])}."))
    # time zone
    offsets = []
    for m in _TZ.finditer(str(job.get("description") or "") + " " + title):
        if m.group(3):
            offsets.append(TZ_ABBR[m.group(3).lower()])
        else:
            sign = -1 if m.group(1).startswith("-") else 1
            offsets.append(sign * (abs(int(m.group(1))) + (int(m.group(2) or 0) / 60)))
    if offsets:
        mine = prefs.get("timezone_offset")
        if mine is None:
            unchecked.append("time zone (set your UTC offset in Preferences)")
        else:
            worst = max(abs(o - mine) for o in offsets)
            if worst > float(prefs.get("tz_tolerance_hours") or 4):
                flags.append(_flag("timezone_mismatch", "Working hours are far from your time zone", "medium" if worst > 8 else "low", f"{worst:g} hours apart; you allow {prefs.get('tz_tolerance_hours')}."))
    # pay
    amounts = pay_amounts(text)
    min_y, min_h = prefs.get("min_pay_yearly"), prefs.get("min_pay_hourly")
    if amounts:
        if min_y is None and min_h is None:
            unchecked.append("pay (set your minimum in Preferences)")
        else:
            yearly = [v * PERIOD_TO_YEAR.get(p, 1) for v, p in amounts]
            hourly = [v for v, p in amounts if p == "hour"]
            top_y, top_h = max(yearly), max(hourly) if hourly else None
            below = (min_h is not None and top_h is not None and top_h < min_h) or (min_y is not None and top_h is None and top_y < min_y)
            if below:
                flags.append(_flag("pay_below_minimum", "Pay is below your minimum", "medium", f"Top of the posted pay is {top_h:g}/hour as written (currencies are not converted); your minimum is {min_h:g}." if top_h is not None else f"Top of the posted pay is about {top_y:,.0f}/year as written (currencies are not converted); your minimum is {min_y:,.0f}."))
    elif min_y is not None or min_h is not None:
        flags.append(_flag("pay_unstated", "Pay is not stated", "low", "Ask before spending time."))
    # work authorization
    desc = str(job.get("description") or "")
    needs = _AUTH.search(desc)
    nospon = _NO_SPONSOR.search(desc)
    if needs or nospon:
        mine = list(prefs.get("work_auth") or [])
        if nospon and prefs.get("needs_sponsorship"):
            flags.append(_flag("work_authorization", "No sponsorship, and you need it", "high", (nospon.group(0) or "")[:80]))
        elif not mine:
            flags.append(_flag("work_authorization", "Asks about work authorization; I have no answer from you", "medium", (needs or nospon).group(0)[:80]))
            unchecked.append("work authorization (set where you may work in Preferences)")
        else:
            flags.append(_flag("work_authorization", "Asks about work authorization", "low", f"You listed: {', '.join(mine[:4])}. Check it fits ({(needs or nospon).group(0)[:60]})."))
    elif prefs.get("needs_sponsorship") and not _SPONSOR_OK.search(desc):
        flags.append(_flag("sponsorship_unknown", "You need sponsorship; the posting does not say", "low", "Ask early."))
    return flags, unchecked


def _vet(job: dict) -> dict:
    try:
        from . import opps_vet
        return opps_vet.vet(title=str(job.get("title") or ""), company=str(job.get("company") or ""), url=str(job.get("url") or ""),
                            description=str(job.get("description") or ""), contact=str(job.get("contact_email") or ""))
    except Exception:  # noqa: BLE001 - the check is a help, never a crash
        return {"flags": [], "severity": "none"}


def analyse(fb: FactBase, job: dict) -> dict:
    """The full fit result. Never raises on odd input."""
    job = job if isinstance(job, dict) else {}
    reqs = [match_requirement(fb, r) for r in extract_requirements(job)]
    scored = [r for r in reqs if r["status"] in ("matched", "partial", "missing") and r.get("section") != "duty"] or \
             [r for r in reqs if r["status"] in ("matched", "partial", "missing")]
    total = sum(2.0 if r["must"] else 1.0 for r in scored)
    got = sum((2.0 if r["must"] else 1.0) * (1.0 if r["status"] == "matched" else 0.5 if r["status"] == "partial" else 0.0) for r in scored)
    score = round(100 * got / total) if total else None
    flags, unchecked = _flags(fb, job, reqs)
    vet = _vet(job)
    for f in vet.get("flags", []):
        flags.append(_flag("scam_" + f["code"], f["label"], f["severity"], f.get("detail", ""), "scam"))
    label = ("unknown" if score is None else "strong" if score >= 75 else "decent" if score >= 50 else "stretch" if score >= 25 else "weak")
    counts = {s: sum(1 for r in reqs if r["status"] == s) for s in ("matched", "partial", "missing")}
    order = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: order.get(f["severity"], 3))
    return {"score": score, "label": label, "counts": counts, "requirements": reqs, "flags": flags, "unchecked": unchecked,
            "scam_severity": vet.get("severity", "none"), "risk_blocked": vet.get("severity") == "high",
            "years_experience": fb.years_experience(),
            "summary": _summary(score, label, counts, flags)}


def _summary(score, label, counts, flags) -> str:
    bits = [f"Fit {score}/100 ({label})" if score is not None else "Fit unknown (no requirements found in the posting)",
            f"{counts['matched']} matched, {counts['partial']} partial, {counts['missing']} missing"]
    high = [f["label"] for f in flags if f["severity"] == "high"]
    med = [f["label"] for f in flags if f["severity"] == "medium"]
    if high:
        bits.append("STOP: " + "; ".join(high[:3]))
    if med:
        bits.append("Check: " + "; ".join(med[:3]))
    return ". ".join(bits) + "."


# ---- honesty check ----------------------------------------------------------------------------------------------------

# Words a sentence may use freely: grammar, light business glue, and the verbs of an approach plan. None of them says
# anything about the owner's experience (that is what the pattern below and the coverage rule are for).
GLUE = frozenset("""applying apply application applied writing write wrote reach reaching reached contact hello thank thanks glad happy talk
speak chat call share send sent attach attached attaching link links below above see seen read review reviewed role roles position posting
post advert ad opening vacancy project projects task tasks job work works worked working need needs needed needing looking look asks asked
ask asking mentioned mention says said describe describes described covers cover covered background match matches matching fit fits relevant
related similar kind sort type work example examples proof sample samples start starting begin beginning availability available availabil
week weeks soon now today next first then plan approach steps step confirm confirming agree agreeing agreed scope scoping deliver delivering
delivered delivery test testing tested ship shipping milestone milestones check checking checks daily weekly update updates updating
question questions detail details note notes proposal bid quote price rate rates budget timeline time timeframe date dates deadline deadlines
can could would will should shall may might happy glad keen ready able interested interest regards best kind sincerely cheers hi dear hello
yes sure please also additionally however therefore one two three four five six seven eight nine ten like love enjoy liked good great
answer answers before after short conversation sense done looks build builds bring brings maps hand hands anything else further discuss discussion
meet meeting talk talks talked listen call calls introduce introduction myself writing wrote""".split())

_ASSERT = re.compile(r"\b(?:i|i've|i'm|my|we|we've|our)\b.*\b(?:led|lead|built|build|managed|manage|designed|design|shipped|ship|delivered|developed|develop|created|"
                     r"launched|launch|scaled|founded|owned|reduced|increased|improved|saved|grew|cut|won|taught|mentored|wrote|architected|implemented|"
                     r"years?|yrs?|experience[d]?|expert|expertise|certified|certification|degree|worked|specialis\w+|proficient|fluent|award\w*|published)\b", re.I)


def _risky(text: str) -> list[str]:
    """Words whose truth matters: numbers, tool/tech names (digits, + or #), ALL-CAPS, and capitalised words inside a sentence."""
    out = []
    raws = _TOKEN.findall(text or "")
    for i, raw in enumerate(raws):
        if len(raw) < 2 and not raw.isdigit():
            continue
        if re.search(r"\d", raw) or re.search(r"[+#]", raw) or (raw.isupper() and len(raw) >= 2 and raw not in ("OK",)) or (i > 0 and raw[:1].isupper() and not raw.isupper()):
            out.append(raw)
    return out


def _free_tokens(fb: FactBase, job: dict | None, extra: list[str]) -> set[str]:
    job = job or {}
    free: set[str] = set()
    for blob in (job.get("title"), job.get("company"), (job.get("url") or "").split("/")[2] if "://" in str(job.get("url") or "") else "", fb.name(), *extra):
        free |= {canon(t) for t in tokens(str(blob or ""), drop_stop=False)}
    return free


def check_claims(fb: FactBase, sentences: list[dict], job: dict | None = None, extra_allowed: list[str] | None = None) -> dict:
    """Each sentence is `{text, fact_ids, kind: claim|glue|plan}`. Returns
    `{ok, unbacked: [{index, text, reason}], checked, verdicts: [{index, ok, reason}]}`."""
    free = _free_tokens(fb, job, list(extra_allowed or []))
    job_tokens = {canon(t) for t in tokens(" ".join(str((job or {}).get(k) or "") for k in ("title", "description", "company")), drop_stop=False)}
    verdicts, unbacked = [], []
    for i, s in enumerate(sentences):
        text = str(s.get("text") or "").strip()
        ids = [x for x in (s.get("fact_ids") or []) if isinstance(x, str)]
        kind = s.get("kind") if s.get("kind") in ("claim", "glue", "plan", "quote") else "claim"
        reason = ""
        if not text:
            verdicts.append({"index": i, "ok": True, "reason": ""})
            continue
        if re.search(r"\[FILL:", text):
            reason = ""       # a visible placeholder is a to-do for the owner, not a claim; it blocks sending elsewhere
        else:
            missing_ids = [x for x in ids if x not in fb.by_id]
            cited = fb.all_text_tokens(ids) | {fb.aliases.get(t, t) for t in fb.all_text_tokens(ids)}
            allowed = cited | free
            if missing_ids:
                reason = "It cites a fact that does not exist."
            elif kind == "claim" and not ids:
                reason = "It says something about you but cites no fact."
            elif kind == "quote":
                blob = " ".join(str((job or {}).get(k) or "") for k in ("title", "description"))
                if " ".join(text.lower().split()).strip(" .") not in " ".join(blob.lower().split()):
                    reason = "It is presented as a quote from the posting but is not in the posting."
            elif kind != "claim" and not ids and s.get("src") != "template" and _ASSERT.search(text):
                reason = "It reads like a claim about your experience but cites no fact."
            else:
                bad = [r for r in _risky(text) if not in_pool(canon(r), allowed) and not in_pool(fb.aliases.get(canon(r), canon(r)), allowed)
                       and not (kind in ("glue", "plan") and canon(r) in job_tokens)]
                if bad:
                    reason = "These words are not in the facts it cites: " + ", ".join(dict.fromkeys(bad[:5])) + "."
                else:
                    content = [t for t in tokens(text) if t not in FILLER and t not in GLUE and not t.isdigit()]
                    pool = allowed | (job_tokens if kind in ("glue", "plan") else set())
                    if kind == "claim" or ids:
                        pool = allowed
                    loose = [t for t in content if not in_pool(t, pool) and not in_pool(fb.aliases.get(t, t), pool)]
                    if content and (kind == "claim" or ids) and (len(content) - len(loose)) / len(content) < 0.6:
                        reason = "Most of its words are not in the facts it cites (" + ", ".join(dict.fromkeys(loose[:5])) + ")."
                    elif content and kind in ("glue", "plan") and not ids and loose and len(loose) / len(content) > 0.5:
                        reason = "It uses words that come neither from the posting nor from your facts (" + ", ".join(dict.fromkeys(loose[:5])) + ")."
        verdicts.append({"index": i, "ok": not reason, "reason": reason})
        if reason:
            unbacked.append({"index": i, "text": text, "reason": reason})
    return {"ok": not unbacked, "unbacked": unbacked, "checked": len(sentences), "verdicts": verdicts}
