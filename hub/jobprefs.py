"""JOB PREFERENCES: the owner's search profiles and the filter that applies them. See docs/api/jobprefs.md.

A PROFILE is one named search ("Remote AI training gigs", "Full-time backend"): work modes (remote / hybrid / onsite, plus a
"remote only" hard switch), employment kinds, locations, a minimum pay (amount + currency + unit), keywords to include and
exclude, companies to avoid, seniority, languages, the sources it reads, a daily quota and a schedule. Profiles live in
`jobprefs.json` in the data folder and are edited in Apps > Job preferences. Nothing is hard-coded to a person or a site:
the only built-in data are generic word lists (region names, languages, pay symbols) and two starter templates the owner
may use or ignore. With NO enabled profile nothing is filtered (the finder behaves as before).

Two kinds of effect on a find, both pure code (no model reads the posting):
- HARD filters. Any one failing means the find is never surfaced (the finder marks it "filtered" with the reason; the
  batch builder skips it). Hard: work mode (and remote-only), remote-but-restricted-to-another-region (when the owner set
  a region), employment kind, minimum pay when it is stated in a comparable unit, excluded keywords, avoided companies,
  seniority, required languages the owner does not speak, on-site/hybrid location, and the profile's source list.
- SOFT scoring. Included keywords, a preferred mode, stated pay above the minimum, a matching seniority add points;
  unknowns (pay not stated, region not stated) take a little off and become warnings the owner sees on the card.
When a posting does not say something (mode, kind, pay) it is NOT treated as a mismatch, except that "remote only"
requires remote to be stated: an unclear posting is never shown to someone who asked for remote only.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .common import Ctx, JsonFile

log = logging.getLogger("hub.jobprefs")

MODES = ("remote", "hybrid", "onsite")
KINDS = ("fulltime", "parttime", "contract", "freelance", "task", "internship")
KIND_LABELS = {"fulltime": "Full-time", "parttime": "Part-time", "contract": "Contract", "freelance": "Freelance / project",
               "task": "Task-based / gig", "internship": "Internship"}
UNITS = ("hour", "day", "week", "month", "year", "project", "task")
LEVELS = ("intern", "junior", "mid", "senior", "lead", "principal")
MAX_PROFILES = 12
MAX_LIST = 50
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PER_YEAR = {"hour": 2080.0, "day": 260.0, "week": 52.0, "month": 12.0, "year": 1.0}
MAX_TEXT = 6000


def clean(value: Any, limit: int = 120) -> str:
    return " ".join(_CONTROL.sub("", value).split())[:limit] if isinstance(value, str) else ""


# ---- regions ---------------------------------------------------------------------------------------------------
# code -> (aliases, direct parents). A group (EU, EUROPE, NA ...) is a code too. A posting restricted to P is open to an
# owner in O when P is O or one of O's ancestors. Owners can add aliases ("Kerala": "IN") in the screen.
_R = {
    "US": (["US", "USA", "U.S.", "U.S.A.", "United States", "United States of America"], ["NA"]),
    "CA": (["Canada", "Canadian"], ["NA"]), "MX": (["Mexico"], ["NA", "LATAM"]), "BR": (["Brazil"], ["LATAM"]),
    "AR": (["Argentina"], ["LATAM"]), "CO": (["Colombia"], ["LATAM"]),
    "UK": (["UK", "U.K.", "United Kingdom", "Great Britain", "England"], ["EUROPE"]), "IE": (["Ireland"], ["EU"]),
    "DE": (["Germany"], ["EU"]), "FR": (["France"], ["EU"]), "ES": (["Spain"], ["EU"]), "IT": (["Italy"], ["EU"]),
    "NL": (["Netherlands", "Holland"], ["EU"]), "PL": (["Poland"], ["EU"]), "PT": (["Portugal"], ["EU"]),
    "SE": (["Sweden"], ["EU"]), "CH": (["Switzerland"], ["EUROPE"]), "TR": (["Turkey", "Turkiye"], ["EUROPE", "MENA"]),
    "IN": (["India"], ["ASIA", "APAC"]), "PK": (["Pakistan"], ["ASIA", "APAC"]), "BD": (["Bangladesh"], ["ASIA", "APAC"]),
    "PH": (["Philippines"], ["ASIA", "APAC"]), "ID": (["Indonesia"], ["ASIA", "APAC"]), "SG": (["Singapore"], ["ASIA", "APAC"]),
    "JP": (["Japan"], ["ASIA", "APAC"]), "AU": (["Australia"], ["APAC"]), "NZ": (["New Zealand"], ["APAC"]),
    "ZA": (["South Africa"], ["AFRICA"]), "NG": (["Nigeria"], ["AFRICA"]), "KE": (["Kenya"], ["AFRICA"]),
    "EG": (["Egypt"], ["AFRICA", "MENA"]), "AE": (["UAE", "United Arab Emirates"], ["MENA", "ASIA"]), "IL": (["Israel"], ["MENA"]),
    "NA": (["North America"], ["AMERICAS"]), "LATAM": (["LATAM", "Latin America", "South America"], ["AMERICAS"]),
    "AMERICAS": (["Americas", "The Americas"], []), "EU": (["EU", "European Union"], ["EUROPE"]),
    "EUROPE": (["Europe", "European"], ["EMEA"]), "EMEA": (["EMEA"], []), "AFRICA": (["Africa"], ["EMEA"]),
    "MENA": (["MENA", "Middle East"], ["EMEA"]), "ASIA": (["Asia"], []), "APAC": (["APAC", "Asia Pacific", "Asia-Pacific"], []),
}
REGION_NAMES = {"US": "United States", "UK": "United Kingdom", "EU": "European Union", "NA": "North America",
                "LATAM": "Latin America", "AMERICAS": "the Americas", "EUROPE": "Europe", "EMEA": "EMEA", "APAC": "Asia-Pacific",
                "MENA": "Middle East & North Africa", "ASIA": "Asia", "AFRICA": "Africa"}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


_ALIAS = {_key(a): code for code, (aliases, _p) in _R.items() for a in aliases}
_ALIAS.update({_key(c): c for c in _R})


def region_name(code: str) -> str:
    return REGION_NAMES.get(code) or (_R[code][0][0] if code in _R else code)


def resolve_region(text: str, extra: dict | None = None) -> str | None:
    k = _key(text)
    if not k:
        return None
    for name, code in (extra or {}).items():
        if _key(name) == k:
            return code if code in _R or re.match(r"^[A-Z0-9]{2,12}$", str(code)) else None
    return _ALIAS.get(k)


def coverage(code: str) -> set[str]:
    """The code and every group above it."""
    out, todo = set(), [code]
    while todo:
        c = todo.pop()
        if c in out:
            continue
        out.add(c)
        todo.extend(_R.get(c, ([], []))[1])
    return out


def _region_rx(extra: dict | None = None) -> re.Pattern:
    names = {a for aliases, _p in _R.values() for a in aliases} | {n for n in (extra or {}) if clean(n, 40)}
    names = sorted(names, key=len, reverse=True)
    short = [re.escape(n) for n in names if len(_key(n)) <= 3 and n.upper() == n.replace(".", "").upper() and not n.islower()]
    long_ = [re.escape(n) for n in names if re.escape(n) not in short]
    parts = []
    if long_:
        parts.append("(?i:" + "|".join(long_) + ")")
    if short:
        parts.append("(?-i:" + "|".join(short) + ")")
    return re.compile(r"(?<![\w.])(?:" + "|".join(parts) + r")(?![\w])")


# ---- reading a posting -----------------------------------------------------------------------------------------

_NEG = re.compile(r"(?:\bnot|\bno|n't|\bnon|\bnever|\bwithout)[\s\-]+(?:\w+[\s\-]+){0,2}$", re.I)
_NOT_REMOTE_CONTEXT = re.compile(r"\bremote(?:ly)?\s+(?:sens\w+|control\w*|access\w*|desktop|monitor\w*|sensor\w*|device\w*|"
                                 r"procedure\w*|server\w*|call\w*|work(?:er)?\s+permission)\b", re.I)
_REMOTE_NEG = re.compile(
    r"(?:\b(?:not|non|no|isn'?t|never|without)[\s\-]+(?:a\s+|fully\s+|100%\s+|entirely\s+)?remote\b)"
    r"|(?:\bremote(?:\s+work(?:ing)?)?\s+(?:is\s+|are\s+)?(?:not|n't)\s+(?:available|possible|offered|allowed|an\s+option|supported)\b)"
    r"|(?:\bunable\s+to\s+(?:offer|support)\s+remote\b)|(?:\b(?:on-?site|in[- ]office|in[- ]person)\s+only\b)"
    r"|(?:\bmust\s+(?:work|be)\s+(?:from|in|at|on)\s+(?:the\s+|our\s+)?(?:office|site|on-?site)\b)", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\b\d\s*(?:-\s*\d\s*)?days?\s+(?:a|per|each|in\s+the)\s+(?:week\s+)?(?:in[- ]?office|on-?site|in\s+the\s+office)\b"
                     r"|\bremote[- ](?:friendly|optional|possible)\b|\boffice\s+and\s+remote\b", re.I)
_REMOTE = re.compile(r"\b(?:fully[- ]|100%[ -]|completely[ -]|globally[ -])?remote(?:ly)?(?:[- ](?:first|only|work|role|position|job|team))?\b"
                     r"|\bwork(?:ing)?\s+from\s+(?:home|anywhere)\b|\bwfh\b|\btelecommut\w+\b|\bhome[- ]based\b|\bdistributed\s+team\b"
                     r"|\bvirtual\s+(?:position|role|job)\b|\bwork\s+from\s+anywhere\b", re.I)
_ONSITE = re.compile(r"\bon-?site\b|\bin[- ]?office\b|\bin[- ]person\b|\boffice[- ]based\b|\brelocat(?:e|ion|ing)\b"
                     r"|\bmust\s+(?:be\s+)?(?:located|based|live)\s+in\b", re.I)
_WORLDWIDE = re.compile(r"\bworld[- ]?wide\b|\banywhere\s+in\s+the\s+world\b|\bwork\s+from\s+anywhere\b|\bglobal(?:ly)?\b|"
                        r"\bany\s+(?:country|location|time\s?zone)\b|\ball\s+countries\b|\binternational\s+candidates\b|\banywhere\b", re.I)
_TZ = re.compile(r"\b(?:UTC|GMT)\s?[+\-−]\s?\d{1,2}(?::\d\d)?\b|\b(?:PST|PDT|EST|EDT|CST|CDT|MST|CET|CEST|IST|AEST|BST|EET)\b"
                 r"(?:\s*(?:[±+\-]\s?\d+\s*(?:h|hrs|hours)?\s*)?(?:time\s?zone|hours|overlap|working\s+hours))?", re.I)
_LOC_LINE = re.compile(r"(?:^|[.;|]\s+)\s*(?:job\s+)?location\s*[:\-]\s*([^\n]+)", re.I)


def _positive_spans(rx: re.Pattern, text: str) -> list[re.Match]:
    return [m for m in rx.finditer(text) if not _NEG.search(text[max(0, m.start() - 24):m.start()])]


def location_line(find: dict) -> str:
    m = _LOC_LINE.search(str(find.get("description") or ""))
    if m:
        return clean(re.split(r"\.\s|;|\|", m.group(1))[0], 160)
    return clean(str(find.get("location") or ""), 160)


def detect_mode(find: dict) -> dict:
    """{mode: remote|hybrid|onsite|unknown, signals: [...]}. Reads the location line, title and description; negations
    ("not remote", "no remote work", "on-site only") win over a stray "remote"; "remote sensing/control/desktop" is ignored."""
    loc = location_line(find)
    title = clean(str(find.get("title") or ""), 200)
    desc = str(find.get("description") or "")[:MAX_TEXT]
    cleaned = _NOT_REMOTE_CONTEXT.sub(" ", f"{loc}\n{title}\n{desc}")
    signals: list[str] = []
    neg = _REMOTE_NEG.search(cleaned)
    positive = None if neg else next(iter(_positive_spans(_REMOTE, cleaned)), None)
    if neg:
        signals.append("says remote is not available: " + clean(neg.group(0), 40))
    hybrid = _HYBRID.search(cleaned)
    onsite = _ONSITE.search(cleaned)
    loc_remote = bool(loc and not _REMOTE_NEG.search(loc) and _REMOTE.search(_NOT_REMOTE_CONTEXT.sub(" ", loc)))
    if hybrid and not (loc_remote and not _HYBRID.search(f"{loc}\n{title}")):
        signals.append("hybrid: " + clean(hybrid.group(0), 40))
        return {"mode": "hybrid", "signals": signals}
    if positive is not None:
        signals.append("remote: " + clean(positive.group(0), 40))
        return {"mode": "remote", "signals": signals}
    if neg or onsite:
        if onsite:
            signals.append("on-site: " + clean(onsite.group(0), 40))
        return {"mode": "onsite", "signals": signals}
    return {"mode": "unknown", "signals": signals}


_COMPANY_CTX = re.compile(r"(?:\bwe(?:'re|\s+are)\b|\bour\s+(?:\w+\s+){0,2}(?:is|are)\b|headquarter\w*|\bhq\b|\bfounded\b)[^.\n]{0,40}$", re.I)
_COMPANY_AFTER = re.compile(r"^[\s\-]*(?:company|companies|startup|firm|business|team|organi[sz]ation|headquarter\w*|hq|office)\b", re.I)


def detect_geo(find: dict, mode: str, extra: dict | None = None) -> dict:
    """Where a REMOTE posting accepts people. {restricted, regions: [codes], worldwide, timezones, text}.
    "Remote (US)", "US only", "must be located in the UK", "authorized to work in the EU", "Location: USA Only" restrict;
    "worldwide", "work from anywhere" lift it. Ignored: a negation ("not limited to the US") and the company's own
    address ("we are based in Germany", "a US-based company")."""
    loc = location_line(find)
    desc = str(find.get("description") or "")[:MAX_TEXT]
    text = f"{loc}\n{clean(str(find.get('title') or ''), 200)}\n{desc}"
    reg = _region_rx(extra)
    R = r"(?P<r>" + reg.pattern + r")"
    patterns = [
        rf"{R}[\s\-]*(?:\w+[\s\-]+)?(?:only|based|residents?|citizens?|nationals?)\b",
        rf"(?:located|based|reside|resident|residing|live|living)\s+(?:\w+\s+){{0,2}}?(?:in|within)\s+(?:the\s+)?{R}",
        r"(?:eligible\s+to|(?:legally\s+)?authori[sz]ed\s+to|authori[sz]ation\s+to|right\s+to|legally\s+allowed\s+to)\s+work\s+(?:\w+\s+){0,2}?"
        rf"(?:in|within|from)\s+(?:the\s+)?{R}",
        rf"work(?:ing)?\s+(?:remotely\s+)?(?:from\s+home\s+)?(?:from|in|within)\s+(?:the\s+)?{R}",
        rf"(?:open|available|restricted|limited)\s+to\s+(?:[a-z]+\s+){{0,3}}?(?:in\s+|from\s+|within\s+|based\s+in\s+)?(?:the\s+)?{R}",
        rf"\bremote\b\s*[\(\[\-\u2013\u2014:,/]\s*(?:anywhere\s+in\s+)?(?:the\s+)?{R}",
        rf"\banywhere\s+in\s+(?:the\s+)?{R}",
    ]
    found: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            before = text[max(0, m.start() - 30):m.start()]
            if _NEG.search(before[-24:]) or _COMPANY_CTX.search(before) or _COMPANY_AFTER.match(text[m.end():m.end() + 24]):
                continue
            code = resolve_region(m.group("r"), extra)
            if code and code not in found:
                found.append(code)
    if loc:
        for m in reg.finditer(loc):
            code = resolve_region(m.group(0), extra)
            if code and code not in found and not _NEG.search(loc[max(0, m.start() - 24):m.start()]):
                found.append(code)
    worldwide = bool(_WORLDWIDE.search(text)) and not found
    tz = []
    for m in _TZ.finditer(text):
        t = clean(m.group(0), 40)
        if t and t not in tz:
            tz.append(t)
    return {"restricted": bool(found) and mode in ("remote", "unknown"), "regions": found, "worldwide": worldwide, "timezones": tz[:4],
            "text": ", ".join(region_name(c) for c in found)}


_KIND_RX = {
    "internship": re.compile(r"\binternships?\b|\bintern\b|\binterns\b", re.I),
    "task": re.compile(r"\btask[- ]based\b|\bper[- ]task\b|\bmicro-?tasks?\b|\bgig\b|\bgigs\b|\bannotat\w+|\bdata\s+label+ing\b|\bratings?\s+tasks?\b|"
                       r"\bai\s+train(?:er|ing)\b|\bllm\s+(?:train|eval)\w*|\bhuman\s+data\b|\brlhf\b", re.I),
    "freelance": re.compile(r"\bfreelanc\w+|\bproject[- ]based\b|\bper[- ]project\b|\bone[- ]off\b|\bfixed[- ]price\b", re.I),
    "contract": re.compile(r"\bcontract(?:or|ors)?\b|\bc2c\b|\bcorp[- ]to[- ]corp\b|\bfixed[- ]term\b|\b1099\b|\btemp(?:orary)?\s+(?:role|position|work)\b", re.I),
    "parttime": re.compile(r"\bpart[- ]?time\b", re.I),
    "fulltime": re.compile(r"\bfull[- ]?time\b|\bpermanent\b", re.I),
}
_FIND_KIND = {"llm-task": "task", "freelance": "freelance"}


def detect_kinds(find: dict) -> list[str]:
    text = f"{find.get('title') or ''}\n{str(find.get('description') or '')[:MAX_TEXT]}"
    out = [k for k, rx in _KIND_RX.items() if _positive_spans(rx, text)]
    hint = _FIND_KIND.get(str(find.get("kind") or ""))
    if hint and hint not in out:
        out.append(hint)
    return out


_SENIORITY = [("principal", re.compile(r"\b(?:principal|director|vp|vice\s+president|distinguished|head\s+of)\b", re.I)),
              ("lead", re.compile(r"\b(?:lead|staff|manager|team\s+lead)\b", re.I)),
              ("senior", re.compile(r"\b(?:senior|sr)\b\.?", re.I)),
              ("intern", re.compile(r"\bintern(?:ship)?\b", re.I)),
              ("junior", re.compile(r"\b(?:junior|jr|entry[- ]level|graduate|trainee|associate)\b\.?", re.I)),
              ("mid", re.compile(r"\b(?:mid[- ]?level|intermediate|mid)\b", re.I))]


def detect_level(title: str) -> str | None:
    for level, rx in _SENIORITY:
        if rx.search(title or ""):
            return level
    return None


# ---- pay -------------------------------------------------------------------------------------------------------
_CUR_MAP = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR",
            "rs": "INR", "rs.": "INR", "inr": "INR", "cad": "CAD", "aud": "AUD", "sgd": "SGD"}
_UNIT_MAP = {"hour": "hour", "hr": "hour", "h": "hour", "hourly": "hour", "day": "day", "week": "week", "wk": "week", "month": "month",
             "mo": "month", "monthly": "month", "year": "year", "yr": "year", "annum": "year", "annually": "year", "yearly": "year",
             "project": "project", "task": "task"}
_CUR = r"(?P<cur>US\$|USD|EUR|GBP|INR|CAD|AUD|SGD|Rs\.?|\$|€|£|₹)"
_NUM = r"(?P<a>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<af>\d{1,2}))?\s?(?P<ak>[kK])?(?![\d.]*\s?(?:[mMbB]\b|mm\b|million|billion|bn\b))"
_UNIT = r"\s*(?:/|per\s+|an?\s+|each\s+|\s)\s*(?P<unit>hour|hr|h|day|week|wk|month|mo|year|yr|annum|project|task)\b|\s*(?P<unit2>hourly|monthly|yearly|annually)\b"
_PAY = re.compile(_CUR + r"\s?" + _NUM + r"(?:\s?(?:-|–|—|to)\s?" + r"(?:" + _CUR.replace("(?P<cur>", "(?P<cur2>") + r"\s?)?"
                  + _NUM.replace("(?P<a>", "(?P<b>").replace("(?P<af>", "(?P<bf>").replace("(?P<ak>", "(?P<bk>") + r")?(?:" + _UNIT + r")?", re.I)


def _amount(whole: str, frac: str | None, k: str | None) -> float:
    v = float(whole.replace(",", "") + ("." + frac if frac else ""))
    return v * 1000 if k else v


def parse_pay(text: str) -> list[dict]:
    """[{currency, lo, hi, unit|None}] for each amount with a currency symbol or code."""
    out = []
    for m in _PAY.finditer(text or ""):
        cur = _CUR_MAP.get(m.group("cur").lower().rstrip("."), _CUR_MAP.get(m.group("cur").lower()))
        if not cur:
            continue
        lo = _amount(m.group("a"), m.group("af"), m.group("ak"))
        hi = _amount(m.group("b"), m.group("bf"), m.group("bk")) if m.group("b") else lo
        if m.group("b") and not m.group("bk") and m.group("ak") and hi < lo:       # "$50k-60"
            hi *= 1000
        lo, hi = min(lo, hi), max(lo, hi)
        unit = _UNIT_MAP.get((m.group("unit") or m.group("unit2") or "").lower())
        if unit is None and hi >= 10000:
            unit = "year"
        out.append({"currency": cur, "lo": lo, "hi": hi, "unit": unit})
    return out[:6]


def pay_label(pay: list[dict]) -> str:
    if not pay:
        return ""
    p = pay[0]
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(p["currency"], p["currency"] + " ")
    fmt = lambda v: f"{v:,.0f}" if v >= 100 or float(v).is_integer() else f"{v:,.2f}"  # noqa: E731
    span = fmt(p["lo"]) if p["lo"] == p["hi"] else f"{fmt(p['lo'])}-{fmt(p['hi'])}"
    return f"{sym}{span}" + (f"/{p['unit']}" if p["unit"] else "")


def pay_verdict(pay: list[dict], min_pay: dict | None) -> tuple[str, str]:
    """('ok'|'below'|'unknown'|'none', sentence). Only a stated amount in the same currency and a convertible unit can be 'below'."""
    if not min_pay or not min_pay.get("amount"):
        return "none", ""
    if not pay:
        return "unknown", "pay not stated"
    want, cur, unit = float(min_pay["amount"]), min_pay.get("currency") or "", min_pay.get("unit") or "year"
    comparable = [p for p in pay if (not cur or p["currency"] == cur) and p["unit"] and (p["unit"] == unit or (p["unit"] in PER_YEAR and unit in PER_YEAR))]
    if not comparable:
        return "unknown", "pay is in another currency or unit, check it yourself"
    best = 0.0
    for p in comparable:
        top = p["hi"] if p["unit"] == unit else p["hi"] * PER_YEAR[p["unit"]] / PER_YEAR[unit]
        best = max(best, top)
    if best + 1e-9 >= want:
        return "ok", "pay meets your minimum"
    return "below", f"pay tops out below your minimum ({pay_label(pay)} vs {want:,.0f} {cur}/{unit})".replace("  ", " ")


# ---- languages -------------------------------------------------------------------------------------------------
LANGS = ["English", "Spanish", "French", "German", "Portuguese", "Italian", "Dutch", "Polish", "Russian", "Ukrainian", "Turkish", "Arabic",
         "Hebrew", "Hindi", "Bengali", "Urdu", "Tamil", "Telugu", "Marathi", "Mandarin", "Chinese", "Cantonese", "Japanese", "Korean",
         "Vietnamese", "Thai", "Indonesian", "Malay", "Swedish", "Norwegian", "Danish", "Finnish", "Czech", "Greek", "Romanian", "Hungarian"]
_LANG = "(?:" + "|".join(LANGS) + ")"
_LANG_REQ = [re.compile(r"\b(?:fluen\w+|proficien\w+|native|business[- ]level|advanced|bilingual)\s+(?:in\s+|level\s+|-\s*)?(" + _LANG + r"(?:\s*(?:,|and|&)\s*" + _LANG + r")*)\b", re.I),
             re.compile(r"\b(" + _LANG + r")[- ](?:speaking|speaker|speakers)\b", re.I),
             re.compile(r"\b(" + _LANG + r")\s+(?:is\s+)?(?:required|mandatory|a\s+must|essential)\b", re.I),
             re.compile(r"\bmust\s+(?:be\s+able\s+to\s+)?speak\s+(" + _LANG + r"(?:\s*(?:,|and|&)\s*" + _LANG + r")*)\b", re.I)]


def required_languages(text: str) -> list[str]:
    out: list[str] = []
    for rx in _LANG_REQ:
        for m in rx.finditer(text or ""):
            for lang in re.findall(_LANG, m.group(1), re.I):
                lang = lang.title()
                if lang not in out:
                    out.append(lang)
    return out


# ---- the verdict -----------------------------------------------------------------------------------------------

def _words_hit(word: str, text: str) -> bool:
    return bool(word) and re.search(r"(?<![\w+#])" + re.escape(word.lower()) + r"(?![\w+#])", text) is not None


_SUFFIX = re.compile(r"\b(?:inc|llc|ltd|limited|gmbh|corp|corporation|co|company|plc|ag|sa|bv)\b\.?")


def norm_company(name: str) -> str:
    return " ".join(_SUFFIX.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", str(name).lower())).split())


def analyze(find: dict, owner: dict | None = None, extra_aliases: dict | None = None) -> dict:
    """Everything the filters read from one posting: mode, geo, kinds, pay, level, languages. Pure."""
    owner = owner or {}
    mode = detect_mode(find)
    if mode["mode"] == "unknown" and find.get("source_remote") is True:   # a board that lists only remote work
        mode = {"mode": "remote", "signals": ["the source lists only remote work"]}
    geo = detect_geo(find, mode["mode"], extra_aliases)
    text = f"{find.get('title') or ''}\n{str(find.get('description') or '')[:MAX_TEXT]}"
    pay = parse_pay(f"{find.get('description') or ''}")
    return {"mode": mode["mode"], "mode_signals": mode["signals"], "geo": geo, "kinds": detect_kinds(find), "pay": pay,
            "pay_text": pay_label(pay), "level": detect_level(str(find.get("title") or "")), "languages": required_languages(text),
            "location": location_line(find)}


def owner_codes(owner: dict, extra: dict | None) -> set[str]:
    out: set[str] = set()
    for name in owner.get("regions") or []:
        code = resolve_region(str(name), extra)
        if code:
            out |= coverage(code)
    return out


def evaluate(find: dict, prof: dict, owner: dict | None = None, extra_aliases: dict | None = None, analysis: dict | None = None) -> dict:
    """One profile against one posting: {ok, fails[], warns[], why[], delta, badges}. Pure."""
    owner = owner or {}
    a = analysis or analyze(find, owner, extra_aliases)
    fails: list[str] = []
    warns: list[str] = []
    why: list[str] = []
    delta = 0
    text = f"{find.get('title') or ''}\n{find.get('company') or ''}\n{str(find.get('description') or '')[:MAX_TEXT]}".lower()
    modes = list(prof.get("work_modes") or [])
    remote_only = prof.get("remote_only") is True
    if remote_only:
        modes = ["remote"]
    mode = a["mode"]
    if modes:
        if mode == "unknown":
            if remote_only:
                fails.append("remote is not stated (you asked for remote only)")
            else:
                warns.append("work mode not stated")
        elif mode not in modes:
            fails.append(f"{mode} work, you want {'/'.join(modes)}")
        else:
            why.append(mode)
            if mode == modes[0]:
                delta += 4
    geo = a["geo"]
    owner_set = owner_codes(owner, extra_aliases)
    if mode == "remote" or (mode == "unknown" and geo["restricted"]):
        if geo["restricted"]:
            where = geo["text"]
            if owner_set and owner.get("honour_region", True) is not False:
                if not any(c in owner_set for c in geo["regions"]):
                    fails.append(f"remote but only for {where}, outside your region")
                else:
                    why.append(f"remote, open to {where} (you qualify)")
            else:
                warns.append(f"remote but limited to {where}; set your region to filter these")
                delta -= 6
        elif geo["worldwide"]:
            why.append("remote, worldwide")
            delta += 2
        elif mode == "remote":
            warns.append("remote, but where you may live is not stated")
            delta -= 2
    if geo["timezones"] and mode == "remote":
        tz = geo["timezones"][0]
        mine = [str(t).lower() for t in (prof.get("timezones") or owner.get("timezones") or [])]
        if mine and not any(m in tz.lower() or tz.lower() in m for m in mine):
            warns.append(f"asks for hours around {tz}")
        elif not mine:
            warns.append(f"mentions {tz}")
    locs = [str(x) for x in prof.get("locations") or []]
    if mode in ("onsite", "hybrid") and locs:
        where = (a["location"] or "").lower()
        hay = where or text
        if any(_words_hit(loc.lower(), hay) for loc in locs):
            why.append("location fits")
        elif where:
            fails.append(f"{mode} in {a['location']}, not in your places")
        else:
            warns.append("office location not stated")
    kinds = list(prof.get("kinds") or [])
    if kinds:
        if not a["kinds"]:
            warns.append("employment type not stated")
            delta -= 2
        elif not set(a["kinds"]) & set(kinds):
            fails.append("type is " + "/".join(KIND_LABELS[k] for k in a["kinds"]).lower())
        else:
            why.append("/".join(KIND_LABELS[k] for k in a["kinds"] if k in kinds).lower())
            delta += 3
    state, sentence = pay_verdict(a["pay"], prof.get("min_pay"))
    if state == "below":
        fails.append(sentence)
    elif state == "unknown":
        warns.append(sentence)
        delta -= 2
    elif state == "ok":
        why.append(sentence)
        delta += 5
    for word in prof.get("exclude") or []:
        if _words_hit(str(word), text):
            fails.append(f"mentions '{word}', which you excluded")
            break
    hits = [w for w in (prof.get("include") or []) if _words_hit(str(w), text)]
    if prof.get("include"):
        if hits:
            why.append("mentions " + ", ".join(hits[:4]))
            delta += min(10, 4 * len(hits))
        elif prof.get("include_required") is True:
            fails.append("none of your required keywords appear")
        else:
            delta -= 3
    company = norm_company(find.get("company") or "")
    for avoid in prof.get("avoid_companies") or []:
        n = norm_company(avoid)
        if n and company and (company == n or _words_hit(n, company)):
            fails.append(f"{find.get('company')} is on your avoid list")
            break
    levels = list(prof.get("seniority") or [])
    if levels and a["level"]:
        if a["level"] in levels:
            why.append(a["level"] + " level")
            delta += 3
        else:
            fails.append(f"level is {a['level']}")
    speaks = {str(x).strip().title() for x in prof.get("languages") or []}
    if speaks and a["languages"]:
        missing = [x for x in a["languages"] if x not in speaks]
        if missing:
            fails.append("requires " + ", ".join(missing))
    return {"ok": not fails, "fails": fails, "warns": warns, "why": why, "delta": max(-25, min(25, delta)),
            "profile": prof.get("id", ""), "profile_name": prof.get("name", "")}


# ---- profile validation ----------------------------------------------------------------------------------------

class PrefError(ValueError):
    pass


def _list(raw: Any, limit: int = 80, lower: bool = False) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for x in raw:
        v = clean(str(x), limit)
        v = v.lower() if lower else v
        if v and v not in out:
            out.append(v)
    return out[:MAX_LIST]


def _pick(raw: Any, allowed: tuple, what: str) -> list[str]:
    items = _list(raw, 30, lower=True)
    bad = [x for x in items if x not in allowed]
    if bad:
        raise PrefError(f"{what}: '{bad[0]}' is not one of {', '.join(allowed)}.")
    return items


def clean_profile(raw: Any, current: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise PrefError("A profile is an object.")
    cur = dict(current or {})
    out = dict(cur)
    if "name" in raw or not current:
        name = clean(raw.get("name"), 60)
        if not name:
            raise PrefError("Give the profile a name.")
        out["name"] = name
    for key in ("enabled", "remote_only", "include_required"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise PrefError(f"{key} must be true or false.")
            out[key] = raw[key]
    if "work_modes" in raw:
        out["work_modes"] = _pick(raw["work_modes"], MODES, "work modes")
    if "kinds" in raw:
        out["kinds"] = _pick(raw["kinds"], KINDS, "employment kinds")
    if "seniority" in raw:
        out["seniority"] = _pick(raw["seniority"], LEVELS, "seniority")
    for key, lower in (("locations", False), ("timezones", False), ("include", True), ("exclude", True), ("avoid_companies", False),
                       ("languages", False), ("sources", False)):
        if key in raw:
            out[key] = _list(raw[key], 80, lower)
    if "sources" in out:
        out["sources"] = [s for s in out["sources"] if ID_RE.match(s.lower())]
    if "languages" in out:
        out["languages"] = [x.title() for x in out["languages"]]
    if "min_pay" in raw:
        mp = raw["min_pay"]
        if mp in (None, "", {}) or (isinstance(mp, dict) and mp.get("amount") in (None, "", 0)):
            out["min_pay"] = None
        elif isinstance(mp, dict):
            amount, cur_, unit = mp.get("amount"), str(mp.get("currency") or "").strip().upper(), str(mp.get("unit") or "year").strip().lower()
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                raise PrefError("The minimum pay must be a number.") from None
            if not 0 < amount < 1e9:
                raise PrefError("The minimum pay must be a positive number.")
            if cur_ and not CURRENCY_RE.match(cur_):
                raise PrefError("The currency is a three-letter code like USD or EUR.")
            if unit not in UNITS:
                raise PrefError(f"The pay unit must be one of {', '.join(UNITS)}.")
            out["min_pay"] = {"amount": amount, "currency": cur_, "unit": unit}
        else:
            raise PrefError("min_pay is {amount, currency, unit}.")
    if "daily_quota" in raw:
        q = raw["daily_quota"]
        if isinstance(q, bool) or not isinstance(q, int) or not 0 <= q <= 200:
            raise PrefError("The daily quota is a whole number from 0 (no quota) to 200.")
        out["daily_quota"] = q
    if "schedule" in raw:
        sched = raw["schedule"]
        if sched in (None, "", {}):
            out["schedule"] = None
        else:
            try:
                from .automations import Bad, validate_schedule
                out["schedule"] = validate_schedule(sched)
                if out["schedule"]["kind"] not in ("daily", "weekdays", "weekly"):
                    raise PrefError("A profile schedule is daily, weekdays or weekly.")
            except Bad as exc:
                raise PrefError(str(exc)) from None
    out.setdefault("enabled", True)
    out.setdefault("remote_only", False)
    out.setdefault("include_required", False)
    for key in ("work_modes", "kinds", "seniority", "locations", "timezones", "include", "exclude", "avoid_companies", "languages", "sources"):
        out.setdefault(key, [])
    out.setdefault("min_pay", None)
    out.setdefault("daily_quota", 0)
    out.setdefault("schedule", None)
    return out


TEMPLATES = [
    {"key": "remote-only", "about": "Remote work only; skips postings that say remote but limit it to another region.",
     "profile": {"name": "Remote only", "remote_only": True, "work_modes": ["remote"]}},
    {"key": "gigs", "about": "Short, task-based or freelance work, remote.",
     "profile": {"name": "Gigs and tasks", "remote_only": True, "kinds": ["freelance", "task", "contract"], "daily_quota": 10}},
]


# ---- the store and engine --------------------------------------------------------------------------------------

# work_auth: countries or regions the owner may work in without sponsorship; notice_days: days before the owner can start (None = not set);
# relocate: willing to move for on-site work; languages: spoken languages (English is always assumed). Read by hub/triage.py.
DEFAULT_OWNER = {"regions": [], "timezones": [], "honour_region": True, "work_auth": [], "notice_days": None, "relocate": False, "languages": []}


class Jobprefs:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.file = JsonFile(ctx.config.data_path() / "jobprefs.json")
        self.lock = threading.RLock()

    def _data(self) -> dict:
        d = self.file.load({})
        d = d if isinstance(d, dict) else {}
        d.setdefault("profiles", [])
        d.setdefault("owner", dict(DEFAULT_OWNER))
        d.setdefault("aliases", {})
        return d

    def profiles(self) -> list[dict]:
        return [p for p in self._data()["profiles"] if isinstance(p, dict)]

    def profile(self, pid: str) -> dict | None:
        return next((p for p in self.profiles() if p.get("id") == pid), None)

    def owner(self) -> dict:
        return {**DEFAULT_OWNER, **(self._data().get("owner") or {})}

    def aliases(self) -> dict:
        a = self._data().get("aliases")
        return a if isinstance(a, dict) else {}

    def add(self, raw: dict) -> dict:
        with self.lock:
            d = self._data()
            if len(d["profiles"]) >= MAX_PROFILES:
                raise PrefError(f"You can keep {MAX_PROFILES} profiles. Delete one first.")
            p = clean_profile(raw)
            p["id"] = "p" + uuid.uuid4().hex[:8]
            d["profiles"].append(p)
            self.file.save(d)
            return p

    def update(self, pid: str, raw: dict) -> dict | None:
        with self.lock:
            d = self._data()
            for i, p in enumerate(d["profiles"]):
                if p.get("id") == pid:
                    new = clean_profile(raw, p)
                    new["id"] = pid
                    d["profiles"][i] = new
                    self.file.save(d)
                    return new
            return None

    def delete(self, pid: str) -> bool:
        with self.lock:
            d = self._data()
            kept = [p for p in d["profiles"] if p.get("id") != pid]
            if len(kept) == len(d["profiles"]):
                return False
            d["profiles"] = kept
            self.file.save(d)
            return True

    def set_owner(self, raw: dict) -> dict:
        with self.lock:
            d = self._data()
            cur = {**DEFAULT_OWNER, **(d.get("owner") or {})}
            if "regions" in raw:
                cur["regions"] = _list(raw["regions"], 40)
            if "timezones" in raw:
                cur["timezones"] = _list(raw["timezones"], 40)
            if "honour_region" in raw:
                if not isinstance(raw["honour_region"], bool):
                    raise PrefError("honour_region must be true or false.")
                cur["honour_region"] = raw["honour_region"]
            if "work_auth" in raw:
                cur["work_auth"] = _list(raw["work_auth"], 40)
            if "languages" in raw:
                cur["languages"] = [x.title() for x in _list(raw["languages"], 30)]
            if "relocate" in raw:
                if not isinstance(raw["relocate"], bool):
                    raise PrefError("relocate must be true or false.")
                cur["relocate"] = raw["relocate"]
            if "notice_days" in raw:
                nd = raw["notice_days"]
                if nd in (None, ""):
                    cur["notice_days"] = None
                elif isinstance(nd, bool) or not isinstance(nd, int) or not 0 <= nd <= 365:
                    raise PrefError("notice_days is a whole number of days from 0 to 365 (or empty).")
                else:
                    cur["notice_days"] = nd
            d["owner"] = cur
            self.file.save(d)
            return cur

    def set_aliases(self, raw: Any) -> dict:
        if not isinstance(raw, dict) or len(raw) > 100:
            raise PrefError("Aliases are {place name: region code}.")
        out = {}
        for name, code in raw.items():
            n, c = clean(str(name), 40), clean(str(code), 12).upper()
            if n and re.match(r"^[A-Z0-9]{2,12}$", c):
                out[n] = c
        with self.lock:
            d = self._data()
            d["aliases"] = out
            self.file.save(d)
        return out

    # ---- applying ----
    def enabled_profiles(self) -> list[dict]:
        return [p for p in self.profiles() if p.get("enabled") is not False]

    def assess(self, find: dict, *, manual: bool = False, only: str = "") -> dict | None:
        """The verdict used by the finder and the batch builder. None when no profile is enabled (nothing is filtered).
        {ok, delta, match{profiles, mode, geo, pay, kinds, why, warns, fails}, reason}."""
        profs = self.enabled_profiles()
        if only:
            profs = [p for p in profs if p.get("id") == only]
        if not profs:
            return None
        owner, aliases = self.owner(), self.aliases()
        a = analyze(find, owner, aliases)
        source = str(find.get("source_id") or "")
        results = []
        for p in profs:
            if p.get("sources") and source and source not in p["sources"] and not manual:
                results.append({"ok": False, "fails": ["this source is not in the profile's sources"], "warns": [], "why": [], "delta": 0,
                                "profile": p["id"], "profile_name": p["name"]})
                continue
            results.append(evaluate(find, p, owner, aliases, a))
        passing = sorted((r for r in results if r["ok"]), key=lambda r: -r["delta"])
        best = passing[0] if passing else min(results, key=lambda r: len(r["fails"]))
        ok = bool(passing) or manual
        match = {"profiles": [r["profile"] for r in passing], "profile": best["profile"], "mode": a["mode"], "geo": a["geo"],
                 "pay": a["pay_text"], "kinds": a["kinds"], "level": a["level"], "why": best["why"], "warns": best["warns"],
                 "fails": [] if passing else best["fails"]}
        reason = ""
        if not passing:
            reason = "; ".join(f"{r['profile_name']}: {r['fails'][0]}" for r in results[:3] if r["fails"])
        return {"ok": ok, "delta": best["delta"] if passing else 0, "match": match, "reason": reason[:300],
                "manual_exempt": manual and not passing}

    def view(self) -> dict:
        regions = sorted({code for code in _R}, key=lambda c: region_name(c))
        return {"profiles": self.profiles(), "owner": self.owner(), "aliases": self.aliases(),
                "options": {"modes": list(MODES), "kinds": [[k, KIND_LABELS[k]] for k in KINDS], "units": list(UNITS), "levels": list(LEVELS),
                            "regions": [[c, region_name(c)] for c in regions]},
                "templates": [{"key": t["key"], "about": t["about"], "name": t["profile"]["name"]} for t in TEMPLATES],
                "has_profiles": bool(self.profiles())}


def engine_for(ctx: Ctx) -> Jobprefs:
    eng = getattr(ctx, "jobprefs", None)
    if eng is None:
        eng = Jobprefs(ctx)
        try:
            ctx.jobprefs = eng
        except AttributeError:  # pragma: no cover
            pass
    return eng


def assess(ctx: Ctx, find: dict, *, manual: bool = False, only: str = "") -> dict | None:
    """For hub/opps.py and hub/batches.py. Never raises; None means 'no profile, do not filter'."""
    try:
        return engine_for(ctx).assess(find, manual=manual, only=only)
    except Exception:  # noqa: BLE001 - a broken preferences file must never stop the finder
        log.warning("jobprefs: assess failed", exc_info=True)
        return None


# ---- routes ----------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx) -> APIRouter:
    eng = engine_for(ctx)
    router = APIRouter(prefix="/api/jobprefs", dependencies=[Depends(ctx.need_auth)])

    def guard(fn):
        try:
            return fn()
        except PrefError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("")
    async def overview():
        return eng.view()

    @router.put("/owner")
    async def owner(body: dict = Body(default_factory=dict)):
        guard(lambda: eng.set_owner(body))
        ctx.audit("jobprefs_owner")
        return eng.view()

    @router.put("/aliases")
    async def aliases(body: dict = Body(default_factory=dict)):
        guard(lambda: eng.set_aliases(body.get("aliases")))
        return eng.view()

    @router.post("/profiles")
    async def add(body: dict = Body(default_factory=dict)):
        p = guard(lambda: eng.add(body))
        ctx.audit("jobprefs_profile_add", id=p["id"])
        return p

    @router.post("/profiles/from-template/{key}")
    async def from_template(key: str):
        t = next((t for t in TEMPLATES if t["key"] == key), None)
        if t is None:
            raise HTTPException(404, "No such template.")
        return guard(lambda: eng.add(dict(t["profile"])))

    @router.put("/profiles/{pid}")
    async def edit(pid: str, body: dict = Body(default_factory=dict)):
        if not ID_RE.match(pid):
            raise HTTPException(404, "No such profile.")
        p = guard(lambda: eng.update(pid, body))
        if p is None:
            raise HTTPException(404, "No such profile.")
        ctx.audit("jobprefs_profile_edit", id=pid)
        return p

    @router.delete("/profiles/{pid}")
    async def remove(pid: str):
        if not ID_RE.match(pid) or not eng.delete(pid):
            raise HTTPException(404, "No such profile.")
        ctx.audit("jobprefs_profile_delete", id=pid)
        return {"ok": True}

    @router.post("/check")
    async def check(body: dict = Body(default_factory=dict)):
        """Test a posting against every profile (nothing is stored)."""
        find = {k: str(body.get(k) or "")[:MAX_TEXT] for k in ("title", "company", "description", "location", "kind", "source_id")}
        owner_, aliases_ = eng.owner(), eng.aliases()
        a = analyze(find, owner_, aliases_)
        return {"analysis": a, "profiles": [evaluate(find, p, owner_, aliases_, a) for p in eng.profiles()]}

    return router
