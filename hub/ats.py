"""ATS match score and "how an ATS reads your resume". Plain code, no AI, nothing leaves the laptop. See docs/api/ats.md.

1. `score(fb, job, resume_text, ...)`: a 0-100 match between a posting and ONE resume version, with a breakdown like the paid
   tools (hard skills, soft skills, job title, years of experience, education and certificates, location), the matched
   keywords, and the MISSING keywords split in two:
     - `listed_in_facts`: "you have this": it is in your confirmed facts but not in this resume. Shown as a suggestion with the
       facts behind it; you decide whether to add it.
     - `gap`: "you do not have this": shown only as a gap. It is never added to anything.
   Keywords come from a curated dictionary (hub/skillwords.py: tech, data, product, ops, Indian-market terms, soft skills,
   certificates, with synonyms such as JS / JavaScript) plus phrase rules ("experience with X, Y and Z"). A keyword that is in the
   requirements counts more than one in a nice-to-have list; one in the job title counts most.
2. `read_check(filename, data)`: what an ATS would extract from a resume FILE (name, contact, titles, dates, skills, education)
   and the format traps that make it fail (tables and columns, text in images, contact details in a header or footer, missing dates,
   odd section names, special characters, a big file, unusual fonts). Reads PDF (pypdf, already in requirements), DOCX (standard
   library) and text. The file is read in memory and never stored or sent anywhere.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import io
import logging
import re
import zipfile
from typing import Any, Callable
from xml.etree import ElementTree

from fastapi import APIRouter, Body, Depends, HTTPException

from . import factbase as fbmod
from . import fitcheck, skillwords
from .assistant import ExtraTool, mark_tools
from .common import Ctx, InProcessClient
from .factbase import FactBase

log = logging.getLogger("hub.ats")

MAX_FILE = 6 * 1024 * 1024
BIG_FILE = 2 * 1024 * 1024
WEIGHTS = {"hard": 45, "soft": 10, "title": 15, "years": 15, "education": 10, "location": 5}
LABELS = {"hard": "Hard skills", "soft": "Soft skills", "title": "Job title", "years": "Years of experience",
          "education": "Education and certificates", "location": "Location"}
EXTRA_FACTOR = 0.6                         # a phrase the dictionary does not know counts less than a dictionary skill
MAX_KEYWORDS = 60


def enabled(config) -> bool:
    return getattr(config, "tailor_enabled", True) is not False


# ---- keywords in a posting ---------------------------------------------------------------------------------------------

_TRIGGER = re.compile(r"(?:experience (?:with|in|of|using)|proficien\w+ (?:in|with)|knowledge of|familiar\w* with|expertise in|skilled in|"
                      r"hands[- ]on (?:experience )?(?:with|in)|background in|understanding of|working knowledge of|such as|including|tools? like)\s+([^.;\n]{3,160})", re.I)
_LEAD = re.compile(r"^(?:strong|solid|good|deep|proven|working|excellent|extensive|basic|advanced|practical|demonstrated|a|an|the|our|any|of|in|with|using|and|or|e\.g\.|eg|i\.e\.)\s+", re.I)
_TRAIL = re.compile(r"\s+(?:is|are) (?:a )?(?:plus|must|required|preferred|desirable|an advantage).*$|\s+(?:is|are)\b.*$|\s+(?:experience|skills?|knowledge|background|expertise|a plus|preferred)$", re.I)
_WORD_STOP = frozenset("""etc other related relevant similar various several multiple both either more many some all any good great strong ability able years year
team teams work working environment company business role roles responsibilities requirements requirement you your we our they their us it
this that these those are is be as to for on at by from into over under within across through new large small high low end full time part""".split())
_TECHY = re.compile(r"\b(?:[A-Za-z]+[+#]{1,2}|[A-Za-z]*[a-z][A-Z][A-Za-z]*|[A-Za-z]+\.(?:js|io|net|py)|[A-Z]{2,6}[0-9]?)\b")
_ACRONYM_STOP = frozenset("""USA UK US INR USD EUR GBP CTC LPA WFH ASAP CEO CTO CFO COO HR PF ESI EPF FTE PTO EEO EOE LLC LLP INC LTD PVT NDA OK TBD ETC FAQ PDF IT AM PM IST UTC EST PST
GMT MBA BBA BCA MCA BSC MSC BTECH MTECH BE ME BA MA PHD NA TV AI SAAS API APIS SLA SDK B2B B2C CRUD""".split())
_YEARS_RX = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b", re.I)


def _line_sections(text: str) -> list[tuple[str, str]]:
    """[(section, line)] using the same section headings as the fit check: must / nice / duty / other."""
    out: list[tuple[str, str]] = []
    section = "other"
    for raw in re.split(r"\r?\n", text or ""):
        line = raw.strip()
        if not line:
            continue
        head = re.sub(r"[:\-–—*#_]+\s*$", "", fitcheck._BULLET.sub("", line)).strip()
        hit = next((name for name, rx in fitcheck._SECTION if rx.match(head) and len(head) < 70), None)
        if hit and len(line) < 80 and (raw.rstrip().endswith(":") or not fitcheck._BULLET.match(raw) or len(head.split()) <= 6):
            section = hit
            continue
        out.append((section, fitcheck._BULLET.sub("", line).strip()))
    if "\n" not in (text or "").strip():             # one paragraph: sentences
        out = [("other", s.strip()) for s in re.split(r"(?<=[.!?])\s+", (text or "").replace("\n", " ")) if s.strip()]
    return out


def _line_weight(section: str, line: str) -> tuple[float, str]:
    """(weight, bucket) for one line of the posting: required 3, general 2, nice-to-have 1."""
    if section == "nice" or fitcheck._NICE_HINT.search(line):
        return 1.0, "nice"
    if section == "must" or fitcheck._MUST_HINT.search(line):
        return 3.0, "must"
    if section == "other" and re.search(r"\b(?:about us|benefits|perks|we offer)\b", line, re.I):
        return 0.0, "skip"
    return 2.0, "general"


def _extras_in(line: str) -> list[str]:
    """Phrases the dictionary does not know: lists after 'experience with ...', and tech-looking words (Kafka-style names, C++, .NET...)."""
    found: list[str] = []
    for m in _TRIGGER.finditer(line):
        for part in re.split(r",|/|;|&|\band\b|\bor\b|\(|\)", m.group(1)):
            p = _TRAIL.sub("", _LEAD.sub("", _LEAD.sub("", part.strip(" .:-–"))))
            words = re.findall(r"[A-Za-z0-9+#.\-]+", p)
            if not 1 <= len(words) <= 3 or all(w.lower() in _WORD_STOP for w in words):
                continue
            if any(w.lower() in _WORD_STOP for w in words[:1] + words[-1:]):
                continue
            found.append(" ".join(words).lower())
    for tok in _TECHY.findall(line):
        t = tok.strip(".")
        if t.upper() in _ACRONYM_STOP or len(t) < 2 or t.lower() in _WORD_STOP:
            continue
        if t.isupper() and len(t) <= 2:
            continue
        found.append(t.lower())
    return found


def extract_keywords(job: dict) -> list[dict]:
    """[{term, kind: hard|soft|cert, weight, bucket: must|general|nice, in_title, count, known}] for the posting, most important first.
    `term` is the canonical skill ('kubernetes' for 'k8s'). Never raises on odd input."""
    job = job if isinstance(job, dict) else {}
    title = str(job.get("title") or "")
    desc = str(job.get("description") or "")
    acc: dict[str, dict] = {}

    def add(term: str, kind: str, weight: float, bucket: str, known: bool, title_hit=False):
        a = acc.setdefault(term, {"term": term, "kind": kind, "weight": 0.0, "bucket": bucket, "in_title": False, "count": 0, "known": known, "_buckets": set()})
        a["count"] += 1
        a["_buckets"].add(bucket)
        if weight > a["weight"]:
            a["weight"], a["bucket"] = weight, bucket
        a["in_title"] = a["in_title"] or title_hit

    for canon in skillwords.find(title):
        add(canon, skillwords.kind_of(canon), 3.0, "must", True, True)
    for section, line in _line_sections(desc):
        weight, bucket = _line_weight(section, line)
        if weight == 0.0:
            continue
        known = skillwords.find(line)
        for canon in known:
            add(canon, skillwords.kind_of(canon), weight, bucket, True)
        for extra in _extras_in(line):
            canon = skillwords.canonical(extra)
            if canon in known or canon in acc or extra in known:
                continue
            if canon in skillwords.SKILLS:
                add(canon, skillwords.kind_of(canon), weight, bucket, True)
            else:
                add(canon, "hard", weight * EXTRA_FACTOR, bucket, False)
    rows = []
    for a in acc.values():
        w = a["weight"] + min(a["count"] - 1, 3) * 0.5 + (2.0 if a["in_title"] else 0.0)
        nice_only = a["_buckets"] == {"nice"}
        rows.append({"term": a["term"], "kind": a["kind"], "weight": round(w, 2), "bucket": "nice" if nice_only else a["bucket"],
                     "in_title": a["in_title"], "count": a["count"], "known": a["known"]})
    rows.sort(key=lambda r: (-r["weight"], r["term"]))
    return rows[:MAX_KEYWORDS]


# ---- what the resume (and the facts) contain ---------------------------------------------------------------------------

def terms_in(text: str, wanted: list[dict] | None = None) -> set[str]:
    """Canonical skills found in `text`; plus, for the posting's phrases the dictionary does not know, an exact word match."""
    out = set(skillwords.find(text))
    low = (text or "").lower()
    for row in wanted or []:
        t = row["term"]
        if t not in out and not row.get("known") and re.search(r"(?<![a-z0-9+#])" + re.escape(t) + r"(?![a-z0-9+#])", low):
            out.add(t)
    return out


def facts_text(fb: FactBase) -> str:
    return "\n".join(f["text"] for f in fb.facts if f["type"] not in ("pref", "name", "link", "answer"))


def fact_ids_for(fb: FactBase, term: str, limit: int = 3) -> list[str]:
    out = []
    for f in fb.facts:
        if f["type"] in ("pref", "name", "link", "answer"):
            continue
        if term in terms_in(f["text"], [{"term": term, "known": term in skillwords.SKILLS}]):
            out.append(f["id"])
            if len(out) >= limit:
                break
    return out


# ---- the other categories ----------------------------------------------------------------------------------------------

_SENIOR = frozenset("senior sr junior jr lead staff principal associate intern trainee head chief i ii iii iv".split())
_TITLE_SYN = {"developer": "engineer", "programmer": "engineer", "swe": "engineer", "sde": "engineer", "dev": "engineer", "analyst": "analyst",
              "backend": "backend", "back-end": "backend", "frontend": "frontend", "front-end": "frontend", "fullstack": "fullstack", "full-stack": "fullstack",
              "mgr": "manager", "pm": "product manager"}


def _title_tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9+#]+(?:-[a-z0-9]+)?", (text or "").lower()):
        w = _TITLE_SYN.get(w, w)
        out += w.split()
    return out


def score_title(job_title: str, titles: list[str], resume_text: str) -> dict:
    core = [w for w in _title_tokens(job_title) if w not in _SENIOR and w not in ("and", "or", "of", "the", "a", "for", "at", "in", "remote", "hybrid")]
    if not core:
        return {"score": None, "note": "The posting has no job title to compare."}
    phrase = " ".join(core)
    pool: set[str] = set()
    best = 0.0
    for t in titles:
        toks = [w for w in _title_tokens(t) if w not in _SENIOR]
        pool |= set(toks)
        if " ".join(toks) == phrase:
            best = 1.0
        elif toks:
            best = max(best, len(set(core) & set(toks)) / len(set(core)))
    low = " ".join(_title_tokens(resume_text))
    if phrase and phrase in low:
        best = max(best, 0.9)
    best = max(best, 0.8 * len(set(core) & (pool | set(low.split()))) / len(set(core)) if not titles else best)
    seniority = [w for w in _title_tokens(job_title) if w in _SENIOR - {"i", "ii", "iii", "iv"}]
    note = f"Your titles: {', '.join(t for t in titles[:3]) or 'none found'}."
    score = round(100 * min(best, 1.0))
    if seniority and not any(s in _title_tokens(" ".join(titles) + " " + resume_text[:600]) for s in seniority):
        score = max(0, score - 10)
        note += f" The posting says {seniority[0]}; your resume does not show that level."
    return {"score": score, "note": note}


def required_years(desc: str) -> int | None:
    best = None
    for m in _YEARS_RX.finditer(desc or ""):
        n = int(m.group(1))
        if not 1 <= n <= 25:
            continue
        around = (desc[max(0, m.start() - 25):m.end() + 14]).lower()
        if re.search(r"\b(?:years? old|years? of age|founded|since|in business|established)\b|\bago\b", around):
            continue
        if best is None or n > best:
            best = n
    return best


def score_years(fb: FactBase, desc: str) -> dict:
    need = required_years(desc)
    have = fb.years_experience()
    if need is None:
        return {"score": None, "note": "The posting names no years of experience.", "required": None, "have": have}
    if not have:
        return {"score": 0, "required": need, "have": 0, "note": f"The posting asks for {need}+ years. No dated roles were found in your facts, so years cannot be counted."}
    return {"score": round(100 * min(1.0, have / need)), "required": need, "have": have,
            "note": f"The posting asks for {need}+ years; your dated roles cover {have}."}


def score_education(fb: FactBase, desc: str, resume_text: str, found: set[str]) -> dict:
    req_level = 0
    for m in fitcheck._DEGREE.finditer(desc or ""):
        req_level = max(req_level, fitcheck._level((desc or "")[max(0, m.start() - 40):m.end() + 40]))
    equiv = bool(fitcheck._EQUIV.search(desc or ""))
    edu_text = " ".join(f["text"] for f in fb.of_type("education")) + " " + resume_text
    have_level = fitcheck._level(edu_text)
    certs = [t for t in skillwords.find(desc or "") if skillwords.kind_of(t) == "cert"]
    parts: list[float] = []
    notes: list[str] = []
    if req_level:
        if have_level >= req_level:
            parts.append(100)
            notes.append("Your degree meets the posting's.")
        elif have_level:
            parts.append(85 if equiv else 50)
            notes.append("Your degree is below the level the posting names." + (" It accepts equivalent experience." if equiv else ""))
        else:
            parts.append(80 if equiv else 0)
            notes.append("No degree found in your facts or resume." + (" The posting accepts equivalent experience." if equiv else ""))
    if certs:
        mine = terms_in(facts_text(fb) + "\n" + resume_text) & set(certs)
        parts.append(round(100 * len(mine) / len(certs)))
        notes.append("Certificates asked: " + ", ".join(certs) + (f"; you list {', '.join(sorted(mine))}." if mine else "; none are in your facts."))
    if not parts:
        return {"score": None, "note": "The posting names no degree or certificate.", "required_certs": []}
    return {"score": round(sum(parts) / len(parts)), "note": " ".join(notes), "required_certs": certs}


_CITIES = ("bengaluru", "bangalore", "mumbai", "delhi", "new delhi", "gurgaon", "gurugram", "noida", "hyderabad", "chennai", "pune", "kolkata", "ahmedabad", "jaipur",
           "chandigarh", "indore", "kochi", "coimbatore", "london", "new york", "san francisco", "singapore", "dubai", "berlin", "toronto", "sydney", "austin", "seattle")


def score_location(fb: FactBase, job: dict, resume_text: str) -> dict:
    blob = f"{job.get('location', '')} {job.get('title', '')} {str(job.get('description', ''))[:1500]}".lower()
    if re.search(r"\b(?:remote|work from home|wfh|anywhere)\b", blob) and not re.search(r"\bnot remote|no remote|onsite only|on-site only\b", blob):
        return {"score": 100, "note": "The posting is remote."}
    places = [c for c in _CITIES if re.search(r"\b" + re.escape(c) + r"\b", blob)]
    if not places:
        return {"score": None, "note": "No location in the posting."}
    mine = f"{fb.prefs.get('location', '')} {fb.prefs.get('availability', '')} {resume_text[:600]}".lower()
    if any(re.search(r"\b" + re.escape(c) + r"\b", mine) for c in places):
        return {"score": 100, "note": f"You are in or near {places[0].title()}."}
    if re.search(r"relocat|open to move|willing to move", mine):
        return {"score": 70, "note": f"The posting is in {places[0].title()}; you said you can relocate."}
    return {"score": 30, "note": f"The posting is in {places[0].title()}; your facts and resume do not show that city."}


# ---- the score ---------------------------------------------------------------------------------------------------------

def _label(n: int) -> str:
    return "strong" if n >= 75 else "good" if n >= 60 else "fair" if n >= 40 else "low"


def score(fb: FactBase, job: dict, resume_text: str, *, titles: list[str] | None = None) -> dict:
    """The ATS match for one posting and one resume text. `titles` are the job titles the resume shows (default: from the facts).
    Pure: reads the facts, changes nothing, adds nothing to the resume."""
    job = job if isinstance(job, dict) else {}
    desc = str(job.get("description") or "")
    resume_text = str(resume_text or "")
    titles = list(titles) if titles is not None else [r.get("title") or r["text"] for r in fb.roles()] + [f["text"] for f in fb.of_type("headline")]
    keywords = extract_keywords(job)
    on_resume = terms_in(resume_text, keywords)
    in_facts = terms_in(facts_text(fb), keywords)
    cats: dict[str, dict] = {}
    for kind in ("hard", "soft"):
        rows = [k for k in keywords if k["kind"] == kind]
        total = sum(k["weight"] for k in rows)
        got = sum(k["weight"] for k in rows if k["term"] in on_resume)
        cats[kind] = {"score": round(100 * got / total) if total else None, "matched": [k["term"] for k in rows if k["term"] in on_resume],
                      "missing": [k["term"] for k in rows if k["term"] not in on_resume],
                      "note": "" if total else f"No {'soft' if kind == 'soft' else 'hard'} skills found in the posting."}
    cats["title"] = score_title(job.get("title", ""), titles, resume_text)
    cats["years"] = score_years(fb, desc)
    cats["education"] = score_education(fb, desc, resume_text, on_resume)
    cats["location"] = score_location(fb, job, resume_text)
    used = {k: v for k, v in cats.items() if v.get("score") is not None}
    weight_sum = sum(WEIGHTS[k] for k in used)
    total_score = round(sum(WEIGHTS[k] * v["score"] for k, v in used.items()) / weight_sum) if weight_sum else None
    breakdown = [{"key": k, "label": LABELS[k], "score": cats[k].get("score"), "weight": WEIGHTS[k] if k in used else 0,
                  **{x: cats[k][x] for x in ("note", "matched", "missing", "required", "have", "required_certs") if x in cats[k]}} for k in WEIGHTS]
    matched = [k for k in keywords if k["term"] in on_resume]
    missing_rows = [k for k in keywords if k["term"] not in on_resume and k["kind"] != "cert"]
    listed = [{**k, "fact_ids": fact_ids_for(fb, k["term"])} for k in missing_rows if k["term"] in in_facts]
    gap = [k for k in missing_rows if k["term"] not in in_facts]
    tips: list[str] = []
    for k in listed[:3]:
        tips.append(f"'{k['term']}' is in your confirmed facts but not on this resume. If you want, add it yourself (it is true).")
    for k in gap[:3]:
        if k["bucket"] == "must":
            tips.append(f"'{k['term']}' is asked for and is not in your facts. It is a gap; it is not added to anything.")
    return {"score": total_score, "label": _label(total_score) if total_score is not None else "unknown", "breakdown": breakdown,
            "matched": matched, "missing": {"listed_in_facts": listed, "gap": gap},
            "keywords_total": len(keywords), "tips": tips,
            "note": "A match score from keywords, like an ATS filter does. It is not a hiring decision.",
            "unknown_phrases": [k["term"] for k in keywords if not k["known"]][:12]}


def resume_text_of(resume: dict) -> str:
    from . import tailor
    return tailor.to_text(resume)


def titles_of(fb: FactBase, resume: dict | None) -> list[str]:
    """The owner's own titles (roles and headline). The tailored resume's 'Seeking: <posting title>' line is the posting's words, so it is not used."""
    out = [r.get("title") or r["text"] for r in fb.roles()]
    out += [f["text"] for f in fb.of_type("headline")]
    return [t for t in out if t]


# ---- how an ATS reads a resume file ------------------------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?)\d{3,5}[\s.-]?\d{3,5}(?!\d)")
_SAFE_FONT = re.compile(r"arial|helvetica|times|calibri|cambria|georgia|verdana|garamond|tahoma|courier|liberation|carlito|caladea|arimo|tinos|cousine|roboto|open ?sans|lato|"
                        r"source ?sans|noto|dejavu|segoe|trebuchet|palatino|book ?antiqua|century|symbol|zapf|inter|cmr|cmss|cmbx|latin ?modern|aptos|ubuntu|montserrat|"
                        r"raleway|merriweather|poppins|nunito|pt ?(?:sans|serif)|fira|libre|lora|mulish|work ?sans|dm ?sans", re.I)
_FANCY = re.compile("[☀-➿\U0001f300-\U0001faff⭐■-◿←-⇿-​‌‍⁠﻿ﬀ-ﬆ�• ]")
_PLAIN_BULLET = frozenset("• ")
_ODD_SECTIONS = {"my journey": "Work Experience", "where i've worked": "Work Experience", "where i have worked": "Work Experience", "career highlights": "Work Experience",
                 "career story": "Work Experience", "my story": "Summary", "what i do": "Summary", "who am i": "Summary", "toolbox": "Skills", "my toolbox": "Skills",
                 "superpowers": "Skills", "tech arsenal": "Skills", "what i know": "Skills", "learning": "Education", "where i studied": "Education", "my education": "Education",
                 "what i've built": "Projects", "things i built": "Projects", "life so far": "Work Experience", "playground": "Projects"}
_DATE_RANGE = re.compile(r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?[\s,]*)?(19|20)\d\d\s*(?:-|–|—|to)\s*"
                         r"(?:(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?[\s,]*)?(?:19|20)\d\d|present|current|now|ongoing)", re.I)
_MONTH = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d\d|\b(?:0?[1-9]|1[0-2])[/.-](?:19|20)\d\d", re.I)


class ReadError(ValueError):
    """The file could not be read; the message says what to do."""


def _trap(code: str, severity: str, title: str, detail: str, fix: str) -> dict:
    return {"code": code, "severity": severity, "title": title, "detail": detail, "fix": fix}


def _xml_root(data: bytes) -> ElementTree.Element:
    from . import profile
    return profile.safe_xml_root(data)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_paras(root) -> list[str]:
    out = []
    for p in root.iter(_W + "p"):
        out.append("".join((t.text or "") for t in p.iter(_W + "t")))
    return out


def read_docx(data: bytes) -> tuple[str, list[dict], dict]:
    traps: list[dict] = []
    meta: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > 40 * 1024 * 1024:
                raise ReadError("That document is too large to read.")
            names = z.namelist()
            body = _xml_root(z.read("word/document.xml"))
            heads = [_xml_root(z.read(n)) for n in names if re.match(r"word/(?:header|footer)\d*\.xml$", n)]
            styles = _xml_root(z.read("word/styles.xml")) if "word/styles.xml" in names else None
    except ReadError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError):
        raise ReadError("I could not open that Word file. Save it again as .docx, or check the text version.") from None
    paras = _docx_paras(body)
    text = "\n".join(paras)
    tables = len(list(body.iter(_W + "tbl")))
    if tables:
        traps.append(_trap("table", "high", "Tables", f"{tables} table(s). An ATS reads a table cell by cell, so rows, dates and skills can come out mixed up.",
                           "Use plain paragraphs and tabs or a simple line instead of a table or a grid."))
    cols = [int(c.get(_W + "num", "1") or 1) for c in body.iter(_W + "cols") if str(c.get(_W + "num", "1")).isdigit()]
    if any(c > 1 for c in cols):
        traps.append(_trap("columns", "high", "Two columns", "The page has more than one text column. Many systems read straight across, mixing the columns.",
                           "Use one column. Put the skills list in its own section under the summary."))
    boxes = len(list(body.iter(_W + "txbxContent")))
    if boxes:
        traps.append(_trap("textbox", "high", "Text boxes", f"{boxes} text box(es). Text inside a box is often skipped by an ATS.", "Move that text into the main page as normal lines."))
    pics = len(list(body.iter(_W + "drawing"))) + len(list(body.iter(_W + "pict")))
    if pics and not boxes:
        traps.append(_trap("images", "medium", "Pictures or shapes", f"{pics} picture(s) or shape(s). An ATS cannot read words inside a picture.",
                           "Do not put any words, skill bars or contact details inside pictures. A photo is not needed."))
    hf_text = "\n".join("\n".join(_docx_paras(h)) for h in heads)
    meta["header_footer_chars"] = len(hf_text)
    if hf_text and (EMAIL.search(hf_text) or PHONE.search(hf_text)):
        in_body = bool(EMAIL.search(text))
        traps.append(_trap("header_contact", "medium" if in_body else "high", "Contact details in the header or footer",
                           "Your email or phone is in the page header or footer. Many ATS ignore those areas" + ("; it is also in the body, which is safe." if in_body else ", and it is not in the body."),
                           "Repeat your name, email and phone at the top of the main page."))
    fonts = {e.get(_W + "ascii") for root in (body, styles) if root is not None for e in root.iter(_W + "rFonts") if e.get(_W + "ascii")}
    meta["fonts"] = sorted(fonts)[:12]
    odd = sorted(f for f in fonts if not _SAFE_FONT.search(f))
    if odd:
        traps.append(_trap("fonts", "low", "Unusual fonts", "Fonts that may not map to plain text: " + ", ".join(odd[:4]) + ".", "Use a common font such as Calibri, Arial or Times New Roman."))
    meta.update(tables=tables, columns=max(cols, default=1), pictures=pics, text_boxes=boxes)
    return text, traps, meta


def _pdf_fragments(page, collect: list) -> str:
    def visit(text, cm, tm, font_dict, font_size):
        t = (text or "").strip()
        if not t:
            return
        try:
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        except Exception:  # noqa: BLE001
            return
        collect.append((x, y, t, float(font_size or 10)))
    return page.extract_text(visitor_text=visit) or ""


def read_pdf(data: bytes) -> tuple[str, list[dict], dict]:
    try:
        import pypdf
    except ImportError:
        raise ReadError("Reading PDF files needs the pypdf package (it is in requirements.txt). Check the text of your resume instead, or run scripts/setup-all.ps1.") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            raise ReadError("That PDF is password protected. An ATS cannot open it either; save a copy without the password.")
        pages = list(reader.pages)
    except ReadError:
        raise
    except Exception:  # noqa: BLE001
        raise ReadError("I could not open that PDF. It may be damaged. Export it again from your editor.") from None
    traps: list[dict] = []
    texts: list[str] = []
    rows_multi = rows_total = 0
    hf_hits = 0
    fonts: dict[str, bool] = {}
    image_pages = no_text_pages = images = 0
    for page in pages[:6]:
        frags: list = []
        try:
            texts.append(_pdf_fragments(page, frags))
        except Exception:  # noqa: BLE001
            texts.append("")
        try:
            w = float(page.mediabox.width) or 595.0
            h = float(page.mediabox.height) or 842.0
        except Exception:  # noqa: BLE001
            w, h = 595.0, 842.0
        rows: dict[int, list] = {}
        for x, y, t, size in frags:
            rows.setdefault(round(y / 3), []).append((x, t, size))
            if (y > 0.93 * h or y < 0.06 * h) and (EMAIL.search(t) or PHONE.search(t)):
                hf_hits += 1
        for items in rows.values():
            items.sort()
            rows_total += 1
            for (x1, t1, s1), (x2, t2, _s2) in zip(items, items[1:]):
                end1 = x1 + len(t1) * s1 * 0.5
                if x2 - end1 > 0.14 * w and len(t1) >= 6 and len(t2) >= 6:
                    rows_multi += 1
                    break
        try:
            n_img = len(page.images)
        except Exception:  # noqa: BLE001
            n_img = 0
        images += n_img
        if n_img:
            image_pages += 1
        if len((texts[-1] or "").strip()) < 40:
            no_text_pages += 1
        try:
            res = page.get("/Resources") or {}
            fd = (res.get_object() if hasattr(res, "get_object") else res).get("/Font") or {}
            for _k, ref in (fd.get_object() if hasattr(fd, "get_object") else fd).items():
                f = ref.get_object()
                name = str(f.get("/BaseFont") or "").lstrip("/")
                name = re.sub(r"^[A-Z]{6}\+", "", name)
                desc = f.get("/FontDescriptor")
                emb = bool(desc and any(k in desc.get_object() for k in ("/FontFile", "/FontFile2", "/FontFile3")))
                if name:
                    fonts[name] = emb
        except Exception:  # noqa: BLE001
            pass
    text = "\n".join(texts)
    meta = {"pages": len(pages), "images": images, "fonts": sorted(fonts)[:12]}
    if len(pages) > 2:
        traps.append(_trap("long", "low", "More than two pages", f"{len(pages)} pages. Recruiters and some systems favour one or two.", "Trim older roles to a line each."))
    if no_text_pages:
        traps.append(_trap("scanned", "high", "Text that is a picture", f"{no_text_pages} page(s) have no readable text; the words are probably an image or a scan. An ATS sees a blank page.",
                           "Export the resume as a PDF from the editor (File, Save as PDF), not a scan or a screenshot."))
    elif images:
        traps.append(_trap("images", "medium", "Pictures", f"{images} picture(s). An ATS cannot read words inside a picture (logos and photos are ignored).",
                           "Keep skill bars, charts and any contact details out of pictures."))
    if rows_total and rows_multi >= 4 and rows_multi / rows_total >= 0.2:
        traps.append(_trap("columns", "high", "Columns or a table", f"{rows_multi} lines have text far apart on the same row (a second column or a table). An ATS may read straight across and mix the two.",
                           "Use one column. Move a side column (skills, contact) into normal sections."))
    if hf_hits:
        traps.append(_trap("header_contact", "medium", "Contact details at the very top or bottom edge",
                           "Your email or phone sits in the page margin area (header or footer). Many ATS ignore it.", "Put your name, email and phone in the main body at the top."))
    garbled = text.count("�") + len(re.findall(r"\(cid:\d+\)", text))
    if garbled >= 3:
        traps.append(_trap("garbled", "high", "Letters do not copy out correctly", "Some characters come out as boxes or codes, so an ATS reads garbage. This is usually a font problem.",
                           "Export again with standard fonts embedded, or use Save as PDF from Word or Google Docs."))
    odd = sorted(f for f in fonts if not _SAFE_FONT.search(f))
    if odd:
        traps.append(_trap("fonts", "low", "Unusual fonts", "Fonts that may not map to plain text: " + ", ".join(odd[:4]) + ".", "Use a common font such as Calibri, Arial or Times New Roman."))
    unemb = sorted(f for f, e in fonts.items() if not e and not re.match(r"(?:Helvetica|Times|Courier|Symbol|ZapfDingbats)", f))
    if unemb:
        traps.append(_trap("fonts_not_embedded", "low", "Fonts not stored in the file", "Not embedded: " + ", ".join(unemb[:4]) + ". The reader may swap them.", "Export with 'embed fonts' on."))
    return text, traps, meta


_ROLE_WORD = re.compile(r"\b(?:engineer|developer|manager|analyst|designer|consultant|lead|intern|officer|director|specialist|architect|scientist|administrator|executive|associate|"
                        r"coordinator|assistant|programmer|tester|head|founder|trainee)\b", re.I)
_BULLET_START = re.compile(r"^\s*(?:[-*\u2022\u25aa\u25cf\u25e6\u2023\u00b7]|\d+[.)])\s+")


def _undated_roles(lines: list[str]) -> list[str]:
    """Role-looking lines (they start with a capital and name a job title early, short, not a bullet) in the Experience section with no date range."""
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or _BULLET_START.match(raw):
            continue
        head = " ".join(line.split()[:6])
        if len(line) < 90 and line[0].isupper() and _ROLE_WORD.search(head) and not _DATE_RANGE.search(line):
            out.append(line[:60])
    return out


def analyse_text(text: str) -> tuple[dict, list[dict]]:
    """The fields an ATS would pull out of the text, and the traps visible in the text itself."""
    from . import profile
    text = str(text or "")
    parsed = profile.parse_resume(text)
    head, sections = profile.split_sections(text)
    head_text = "\n".join(head[:12])
    email_m = EMAIL.search(text)
    email = email_m.group(0) if email_m else ""
    phone_m = PHONE.search(re.sub(r"https?://\S+", "", text))
    phone = phone_m.group(0).strip() if phone_m and len(re.sub(r"\D", "", phone_m.group(0))) >= 8 else ""
    exp = parsed.get("experience") or []
    ranges = _DATE_RANGE.findall(text)
    n_ranges = len([m for m in _DATE_RANGE.finditer(text)])
    edu_lines = [ln.strip() for ln in (parsed.get("education") or "").splitlines() if ln.strip()]
    fields = {"name": parsed.get("name") or "", "email": email, "phone": phone, "links": parsed.get("links") or {},
              "titles": [e["title"] for e in exp if e.get("title")][:12], "companies": [e["company"] for e in exp if e.get("company")][:12],
              "dates": [e["period"] for e in exp if e.get("period")][:12], "skills": (parsed.get("skills") or [])[:40], "education": edu_lines[:6],
              "sections": [k for k in sections if k][:8], "headline": parsed.get("headline") or ""}
    traps: list[dict] = []
    if len(text.strip()) < 40:
        traps.append(_trap("empty", "high", "No text found", "An ATS would see an empty resume.", "Export the resume as a text-based PDF or a DOCX."))
        return fields, traps
    if not email:
        traps.append(_trap("no_email", "high", "No email found", "An ATS looks for an email address to create your profile.", "Put your email in the main text at the top."))
    if not phone:
        traps.append(_trap("no_phone", "medium", "No phone number found", "Most ATS profiles ask for a phone number.", "Add a phone number with the country code."))
    if not fields["name"]:
        traps.append(_trap("no_name", "medium", "Name not clear", "The first line should be only your name so the ATS can fill the Name field.", "Put just your name on the first line."))
    if "experience" not in sections and "skills" not in sections and "education" not in sections:
        traps.append(_trap("sections", "medium", "No standard section names", "No 'Experience', 'Skills' or 'Education' heading was found. An ATS sorts text by those names.",
                           "Use plain headings: Summary, Experience, Skills, Education."))
    else:
        for want in ("experience", "skills", "education"):
            if want not in sections:
                traps.append(_trap("missing_" + want, "low", f"No '{want.title()}' heading", f"An ATS sorts your resume by standard headings and did not find '{want.title()}'.", f"Add a plain '{want.title()}' heading."))
    odd = []
    for ln in text.splitlines():
        key = re.sub(r"[:\-_=*#|]+", " ", ln).strip().lower()
        key = re.sub(r"\s+", " ", key)
        if key in _ODD_SECTIONS:
            odd.append((ln.strip(), _ODD_SECTIONS[key]))
    if odd:
        traps.append(_trap("odd_sections", "medium", "Creative section names", "; ".join(f"'{a}' (use '{b}')" for a, b in odd[:4]) + ". An ATS only knows the common names.", "Rename them to plain headings."))
    if sections.get("experience") or exp:
        undated = _undated_roles(sections.get("experience", []))
        if not n_ranges:
            traps.append(_trap("no_dates", "high", "No dates on your roles", "No date range such as 'Jun 2021 - Present' was found. An ATS cannot count your experience.", "Put a start and end date next to every role."))
        else:
            if undated:
                traps.append(_trap("some_dates", "medium", "Some roles have no dates", f"{len(undated)} role(s) have no date range: " + "; ".join(undated[:3]) + ".", "Add dates to each role."))
            if not undated and not _MONTH.search(text):
                traps.append(_trap("year_only", "low", "Dates have years only", "Only years were found (no months). Some systems round that badly.", "Use 'Jun 2021 - Mar 2024'."))
    elif sections:
        traps.append(_trap("no_experience_body", "medium", "No roles were read", "The Experience section did not produce any role with a title and company.", "Put each role on one line: Title, Company, dates."))
    if not fields["skills"] and "skills" in sections:
        traps.append(_trap("skills_empty", "low", "Skills not read", "The Skills section is there but no skill names came out.", "Write skills as a comma-separated line."))
    fancy = [c for c in _FANCY.findall(text) if c not in _PLAIN_BULLET]
    if fancy:
        sample = "".join(dict.fromkeys(fancy))[:8]
        traps.append(_trap("special_chars", "low" if len(fancy) < 8 else "medium", "Special characters",
                           f"{len(fancy)} unusual symbol(s) such as {sample!r} (icons, arrows, ligatures, hidden spaces). An ATS may drop them or break the word next to them.",
                           "Use a plain round bullet or a hyphen. Write 'Phone' and 'Email' as words instead of icons."))
    if "\t" in text and text.count("\t") > 10:
        traps.append(_trap("tabs", "low", "Many tab gaps", "Lots of tab spacing often means a table or a side-by-side layout.", "Use single lines."))
    return fields, traps


def read_check(filename: str, data: bytes | None = None, *, text: str | None = None) -> dict:
    """What an ATS extracts from this resume file, and the traps. Local only; the bytes are never stored. Raises ReadError."""
    traps: list[dict] = []
    meta: dict[str, Any] = {}
    fmt = "text"
    size = len(data) if data is not None else len((text or "").encode("utf-8"))
    if data is not None:
        if not data:
            raise ReadError("The file is empty.")
        if len(data) > MAX_FILE:
            raise ReadError("That file is over 6 MB; an ATS will not take it either. Export a smaller one.")
        name = (filename or "").lower()
        if data[:5] == b"%PDF-" or name.endswith(".pdf"):
            fmt = "pdf"
            body, traps, meta = read_pdf(data)
        elif data[:2] == b"PK" or name.endswith(".docx"):
            fmt = "docx"
            body, traps, meta = read_docx(data)
        elif name.endswith((".doc", ".rtf", ".odt", ".pages")):
            raise ReadError("That file type is often unreadable by an ATS. Save it as PDF or DOCX and check that.")
        else:
            body = data.decode("utf-8", errors="replace")
    else:
        body = str(text or "")
    if size > BIG_FILE:
        traps.append(_trap("big_file", "medium", "File over 2 MB", f"{round(size / 1048576, 1)} MB. Many application forms reject files over 2 MB.", "Export again without big pictures or embedded fonts."))
    fields, text_traps = analyse_text(body)
    have = {t["code"] for t in traps}
    traps += [t for t in text_traps if t["code"] not in have]
    order = {"high": 0, "medium": 1, "low": 2}
    traps.sort(key=lambda t: order[t["severity"]])
    penalty = sum({"high": 20, "medium": 10, "low": 4}[t["severity"]] for t in traps)
    readable = max(0, 100 - penalty)
    return {"format": fmt, "size": size, "readable": readable, "label": _label(readable), "fields": fields, "traps": traps,
            "ats_view": re.sub(r"\n{3,}", "\n\n", body).strip()[:4000], "meta": meta,
            "pdf_supported": _has_pypdf(), "note": "Read on your laptop. The file was not stored or sent anywhere. This is a guide to common ATS behaviour, not a test of one specific system."}


def _has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


# ---- routes ------------------------------------------------------------------------------------------------------------

class AtsService:
    def __init__(self, ctx: Ctx, *, profile_fn=None, clock=None):
        self.ctx = ctx
        self.profile_fn = profile_fn
        self.clock = clock or dt.datetime.now

    def profile(self) -> dict:
        if self.profile_fn:
            return self.profile_fn(self.ctx) or {}
        try:
            from . import profile
            return profile.get_profile(self.ctx)
        except Exception:  # noqa: BLE001
            return {}

    def facts(self) -> FactBase:
        return FactBase.build(self.profile(), fbmod.load(self.ctx), this_year=self.clock().year)

    def job_for(self, body: dict) -> dict:
        from . import tailor
        if body.get("application_id"):
            from . import applyq
            item = applyq.Applyq(self.ctx).get(str(body["application_id"]))
            return tailor.clean_job({"title": item.get("title"), "company": item.get("company"), "url": item.get("url"),
                                     "description": item.get("description"), "kind": item.get("kind")})
        return tailor.clean_job(body.get("job") if isinstance(body.get("job"), dict) else body)

    def resume_for(self, fb: FactBase, body: dict) -> tuple[str, dict, str]:
        """(resume text, resume dict, what it is) for a variant id, a master id, pasted text, or the base resume from the facts."""
        from . import tailor
        if isinstance(body.get("resume_text"), str) and body["resume_text"].strip():
            return body["resume_text"][:60000], {}, "the text you pasted"
        if body.get("variant_id"):
            v = tailor.Tailor(self.ctx, profile_fn=self.profile_fn, clock=self.clock).get(str(body["variant_id"]))
            return resume_text_of(v["resume"]), v["resume"], f"tailored resume {v['id']}"
        if body.get("master_id"):
            from . import resumes
            m = resumes.Masters(self.ctx, profile_fn=self.profile_fn, clock=self.clock).get(str(body["master_id"]))
            ver = resumes.version_of(m, body.get("version"))
            return resume_text_of(ver["resume"]), ver["resume"], ver["label"]
        base = tailor.build_resume(fb, None)
        return resume_text_of(base), base, "your base resume (every fact)"

    def score(self, body: dict) -> dict:
        job = self.job_for(body)
        fb = self.facts()
        if not fb.facts:
            raise ReadError("There are no facts yet. Add your resume in Apps > Opportunities > Profile first.")
        text, resume, what = self.resume_for(fb, body)
        out = score(fb, job, text, titles=titles_of(fb, resume))
        out["resume"] = what
        out["job"] = {k: job.get(k, "") for k in ("title", "company")}
        return out

    def readcheck(self, body: dict) -> dict:
        from . import tailor
        if body.get("variant_id"):
            fmt = body.get("format") if body.get("format") in ("pdf", "docx", "txt") else "pdf"
            v = tailor.Tailor(self.ctx, profile_fn=self.profile_fn, clock=self.clock).get(str(body["variant_id"]))
            lines = tailor.render_lines(v["resume"])
            if fmt == "txt":
                return read_check("resume.txt", text=tailor.to_text(v["resume"]))
            return read_check("resume." + fmt, tailor.pdf_bytes(lines) if fmt == "pdf" else tailor.docx_bytes(lines))
        if isinstance(body.get("data_b64"), str) and body["data_b64"]:
            if len(body["data_b64"]) > MAX_FILE * 4 // 3 + 16:
                raise ReadError("That file is over 6 MB.")
            try:
                raw = base64.b64decode(body["data_b64"], validate=True)
            except (binascii.Error, ValueError):
                raise ReadError("The file did not arrive intact. Try again.") from None
            return read_check(str(body.get("filename") or "resume")[:120], raw)
        if isinstance(body.get("text"), str) and body["text"].strip():
            return read_check("resume.txt", text=body["text"][:60000])
        text = str(self.profile().get("resume_text") or "")
        if not text.strip():
            raise ReadError("Choose a resume file, paste its text, or add your resume in Apps > Opportunities > Profile first.")
        out = read_check("resume.txt", text=text)
        out["note"] = ("Only the text of your saved resume is kept, so layout traps (tables, columns, header contact, fonts) cannot be checked for it. "
                       "Choose the actual file to check those.") + " " + out["note"]
        out["from_profile"] = True
        return out


def make_router(ctx: Ctx, *, profile_fn=None, clock=None, **_: Any) -> APIRouter:
    router = APIRouter(prefix="/api/ats", dependencies=[Depends(ctx.need_auth)])
    svc = AtsService(ctx, profile_fn=profile_fn, clock=clock)
    router.service = svc                                      # type: ignore[attr-defined]

    def on() -> None:
        if not enabled(ctx.config):
            raise HTTPException(404, "ATS tools are switched off (tailor_enabled is false).")

    def guard(fn):
        try:
            return fn()
        except ReadError as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/info")
    async def info():
        return {"enabled": enabled(ctx.config), "pdf_supported": _has_pypdf(), "docx_supported": True, "max_file_mb": MAX_FILE // 1048576,
                "weights": WEIGHTS, "skills_known": len(skillwords.SKILLS)}

    @router.post("/score")
    async def api_score(body: dict = Body(default_factory=dict)):
        """{application_id | job:{title,description,...}, variant_id? | master_id? | resume_text?} -> score, breakdown, matched, missing."""
        on()
        import asyncio
        return await asyncio.to_thread(lambda: guard(lambda: svc.score(body)))

    @router.post("/readcheck")
    async def api_readcheck(body: dict = Body(default_factory=dict)):
        """{filename, data_b64} | {text} | {variant_id, format} | {} (your saved resume text) -> fields, traps, ats_view. Nothing is stored."""
        on()
        import asyncio
        out = await asyncio.to_thread(lambda: guard(lambda: svc.readcheck(body)))
        ctx.audit("ats_readcheck", format=out["format"], size=out["size"], traps=len(out["traps"]))        # sizes only, never content
        return out

    return router


# ---- assistant tool (read-only) ----------------------------------------------------------------------------------------

def _short(value: Any, n: int = 200) -> str:
    return " ".join(str(value if value is not None else "").split())[:n]


def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    async def ats_score(a):
        body: dict = {}
        if isinstance(a.get("application_id"), str) and a["application_id"].strip():
            body["application_id"] = a["application_id"].strip()
        elif isinstance(a.get("description"), str) and a["description"].strip():
            body["job"] = {k: a[k] for k in ("title", "company", "description") if isinstance(a.get(k), str)}
        else:
            return False, "Give an application id, or the job title and description."
        for k in ("variant_id", "master_id"):
            if isinstance(a.get(k), str) and a[k].strip():
                body[k] = a[k].strip()
        ok, d = await hub.call("POST", "/api/ats/score", body=body)
        if not ok:
            return False, d
        lines = [f"ATS match {d['score']}/100 ({d['label']}) for {_short(d['job'].get('title'), 80)}, resume: {d['resume']}."]
        lines.append("; ".join(f"{b['label']} {b['score']}" for b in d["breakdown"] if b["score"] is not None))
        listed = d["missing"]["listed_in_facts"][:5]
        gap = d["missing"]["gap"][:5]
        if listed:
            lines.append("In the owner's facts but not on this resume (owner may add): " + ", ".join(k["term"] for k in listed))
        if gap:
            lines.append("Not in the owner's facts (gaps, never to be added): " + ", ".join(k["term"] for k in gap))
        lines.append("Read-only: nothing was changed or sent.")
        return True, "\n".join(lines)

    schema = {"type": "object", "additionalProperties": False, "required": [],
              "properties": {"application_id": {"type": "string"}, "title": {"type": "string"}, "company": {"type": "string"}, "description": {"type": "string"},
                             "variant_id": {"type": "string"}, "master_id": {"type": "string"}}}
    return mark_tools([ExtraTool({"name": "ats_score", "description": "How well the owner's resume (a tailored version, a master resume, or the base resume) matches ONE job posting the way an "
                                  "applicant tracking system scores it: 0-100, a breakdown, matched keywords, and missing keywords split into 'in the owner's facts but not on this resume' "
                                  "and 'a real gap'. Read-only; never adds a skill to the resume.", "input_schema": schema}, ats_score, untrusted=True)])
