"""The owner's FACT BASE: every statement a resume, cover note or form answer may make about the owner, with an id.

Facts are extracted from the private profile (`hub/profile.py`: name, headline, summary, skills, experience with
bullets and dates, education, links, standard answers) plus facts the owner confirmed by hand (`confirmed`) and the
owner's own preferences (rate, availability). Everything generated later cites fact ids; `hub/fitcheck.py` refuses
any sentence whose words are not backed by the facts it cites. Nothing here is invented: a fact is a copy of what the
owner wrote, or something derived by counting (years from dates, the roles whose bullets mention a skill).

Stored in `<data>/factbase.json` (mode 0600): confirmed facts, hidden fact ids, aliases and preferences. The profile
itself stays in `profile.json`; this file only holds what the owner added here. See docs/api/tailor.md.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .common import Ctx

MAX_CONFIRMED = 300
MAX_TEXT = 600
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_LOCK = threading.RLock()

# ---- words ----------------------------------------------------------------------------------------------------

STOP = frozenset("""a an the and or of to in on at for with by from as is are was were be been being this that these those it its
we you your our their they he she i me my us them his her not no but if so than then there here also can will would should could may
might must do does did done have has had having about into over under per via within without across between such other any all each
both more most very much many some few own same too just only own what which who whom whose when where why how up out off again
further once etc eg ie""".split())
# Words that appear in nearly every posting or resume and carry no skill signal.
FILLER = frozenset("""experience experienced strong ability able skills skill knowledge working work worked works team teams good excellent
years year yrs proven track record role roles position job candidate candidates looking join company business environment
responsible responsibilities requirements requirement required preferred plus bonus nice have must need needs needed
including include includes etc relevant related using use used based high level highly new well success successful
opportunity opportunities help helping ensure ensuring day days time full part make making demonstrated understanding
familiarity familiar proficiency proficient hands-on handson solid deep great""".split())

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.\-/]*[A-Za-z0-9+#]|[A-Za-z0-9]")


def canon(token: str) -> str:
    """One comparable form of a word: lowercase, no punctuation (keeps + and #), a trailing plural s removed."""
    t = re.sub(r"[^a-z0-9+#]", "", token.lower())
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss") and not t.endswith("us"):
        t = t[:-1]
    return t


def token_pairs(text: str, *, drop_stop: bool = True) -> list[tuple[str, str]]:
    """[(canonical word, the word as written)] in order."""
    out = []
    for raw in _TOKEN.findall(text or ""):
        for part in ([raw] if re.search(r"[+#]", raw) or re.fullmatch(r"[A-Za-z0-9]+\.[A-Za-z]{1,3}", raw) else re.split(r"[/\-]", raw)):
            c = canon(part)
            if c and not (drop_stop and (c in STOP or canon(part.lower()) in STOP)):
                out.append((c, part))
    return out


def tokens(text: str, *, drop_stop: bool = True) -> list[str]:
    """Canonical words of a text, in order. `node.js` stays one word ('nodejs'), `c++` stays 'c++'."""
    return [c for c, _raw in token_pairs(text, drop_stop=drop_stop)]


def same_word(a: str, b: str) -> bool:
    """Same word, or one is the other plus a short ending ('postgres' / 'postgresql'); the shorter must be at least 7 letters."""
    if a == b:
        return True
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    return len(s) >= 7 and l.startswith(s) and len(l) - len(s) <= 4


def in_pool(word: str, pool: set[str]) -> bool:
    return word in pool or (len(word) >= 7 and any(same_word(word, p) for p in pool if len(p) >= 7))


# Words that name a level or a kind of job, not a skill: they appear in every posting and every resume.
ROLE_WORDS = frozenset("engineer developer senior junior lead staff principal manager analyst specialist associate consultant officer".split())


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def signal_tokens(text: str, aliases: dict | None = None) -> set[str]:
    """Tokens that say something (no stop words, no filler), with the owner's aliases applied."""
    out = {t for t in tokens(text) if t not in FILLER and not t.isdigit()}
    if aliases:
        out = {aliases.get(t, t) for t in out}
    return out


def phrase_key(text: str) -> str:
    """A phrase as one comparable string: 'Node.js', 'NodeJS' and 'node js' share it."""
    return "".join(tokens(text, drop_stop=False))


def norm_text(text: str) -> str:
    return " ".join(_CONTROL.sub("", str(text or "")).split())


def clean(value: Any, limit: int = MAX_TEXT) -> str:
    return norm_text(value)[:limit] if isinstance(value, str) else ""


def fid(kind: str, text: str) -> str:
    return f"{kind[:4]}-{hashlib.sha1((kind + '|' + ' '.join(str(text).lower().split())).encode()).hexdigest()[:8]}"


# ---- preferences ------------------------------------------------------------------------------------------------

TONES = {
    "direct": {"greeting": "Hi {to},", "closing": "Thanks,"},
    "warm": {"greeting": "Hello {to},", "closing": "Thank you, and best wishes,"},
    "formal": {"greeting": "Dear {to},", "closing": "Kind regards,"},
}
DEFAULT_PREFS = {
    "tone": "direct", "tone_notes": "", "sign_off": "", "tones": {},
    "rate": "", "availability": "", "location": "", "remote_ok": True, "relocate": False,
    "timezone_offset": None, "tz_tolerance_hours": 4, "min_pay_yearly": None, "min_pay_hourly": None,
    "work_auth": [], "needs_sponsorship": False, "target_titles": [], "max_bullets": 4,
}


def clean_prefs(raw: Any, base: dict | None = None) -> dict:
    """Validate an edit of the preferences; unknown keys are dropped, bad values raise ValueError (a plain sentence)."""
    out = dict(DEFAULT_PREFS)
    out.update(base or {})
    if not isinstance(raw, dict):
        raise ValueError("preferences must be an object.")
    for key, value in raw.items():
        if key not in DEFAULT_PREFS:
            continue
        if key in ("tone",):
            v = clean(value, 40).lower()
            if not v:
                raise ValueError("tone cannot be empty.")
            out[key] = v
        elif key in ("tone_notes",):
            out[key] = clean(value, 400)
        elif key in ("sign_off", "rate", "availability", "location"):
            out[key] = clean(value, 200)
        elif key in ("remote_ok", "relocate", "needs_sponsorship"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false.")
            out[key] = value
        elif key == "timezone_offset":
            if value in (None, ""):
                out[key] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not -12 <= value <= 14:
                raise ValueError("timezone_offset must be hours from UTC, from -12 to 14 (or empty).")
            else:
                out[key] = float(value)
        elif key == "tz_tolerance_hours":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 24:
                raise ValueError("tz_tolerance_hours must be from 0 to 24.")
            out[key] = float(value)
        elif key in ("min_pay_yearly", "min_pay_hourly"):
            if value in (None, ""):
                out[key] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{key} must be a positive number (or empty).")
            else:
                out[key] = float(value)
        elif key == "max_bullets":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
                raise ValueError("max_bullets must be a whole number from 1 to 8.")
            out[key] = value
        elif key in ("work_auth", "target_titles"):
            if isinstance(value, str):
                value = re.split(r"[,;\n]", value)
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a list of words.")
            out[key] = [v for v in (clean(x, 60) for x in value[:20]) if v]
        elif key == "tones":
            if not isinstance(value, dict):
                raise ValueError("tones must map a tone name to its greeting and closing.")
            tones = {}
            for name, spec in list(value.items())[:10]:
                if not isinstance(spec, dict) or not clean(name, 40):
                    continue
                tones[clean(name, 40).lower()] = {"greeting": clean(spec.get("greeting"), 80), "closing": clean(spec.get("closing"), 80)}
            out[key] = tones
    return out


# ---- the store ----------------------------------------------------------------------------------------------------

def path_for(ctx: Ctx) -> Path:
    return Path(ctx.config.data_path()) / "factbase.json"


def load(ctx: Ctx) -> dict:
    with _LOCK:
        try:
            data = json.loads(path_for(ctx).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    prefs = dict(DEFAULT_PREFS)
    try:
        prefs = clean_prefs(data.get("prefs") or {}, DEFAULT_PREFS)
    except ValueError:
        pass
    return {"confirmed": [c for c in data.get("confirmed", []) if isinstance(c, dict) and c.get("id") and c.get("text")][:MAX_CONFIRMED],
            "hidden": [h for h in data.get("hidden", []) if isinstance(h, str)][:2000],
            "aliases": {str(k): str(v) for k, v in (data.get("aliases") or {}).items()} if isinstance(data.get("aliases"), dict) else {},
            "prefs": prefs}


def save(ctx: Ctx, data: dict) -> None:
    path = path_for(ctx)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)


def _tag_list(tags) -> list[str]:
    """Up to 8 short tags. Anything that is not a list of text (a number, an object) means no tags, not a crash."""
    if not isinstance(tags, (list, tuple)):
        return []
    return [t for t in (clean(x, 30) for x in tags[:8] if isinstance(x, str)) if t]


def add_confirmed(ctx: Ctx, text: str, tags: Iterable[str] = (), *, clock=None) -> dict:
    """Owner-confirmed extra fact. The text is exactly what the owner typed or approved."""
    text = clean(text, MAX_TEXT)
    if len(text) < 3:
        raise ValueError("A fact needs a sentence of at least a few words.")
    now = (clock() if clock else dt.datetime.now()).isoformat(timespec="seconds")
    with _LOCK:
        data = load(ctx)
        fact_id = fid("conf", text)
        if any(c["id"] == fact_id for c in data["confirmed"]):
            return next(c for c in data["confirmed"] if c["id"] == fact_id)
        if len(data["confirmed"]) >= MAX_CONFIRMED:
            raise ValueError(f"You can keep up to {MAX_CONFIRMED} confirmed facts. Delete some first.")
        entry = {"id": fact_id, "text": text, "tags": _tag_list(tags), "created": now}
        data["confirmed"].append(entry)
        if fact_id in data["hidden"]:
            data["hidden"].remove(fact_id)
        save(ctx, data)
    return entry


def update_confirmed(ctx: Ctx, fact_id: str, text: str | None = None, tags: Iterable[str] | None = None) -> dict:
    with _LOCK:
        data = load(ctx)
        entry = next((c for c in data["confirmed"] if c["id"] == fact_id), None)
        if entry is None:
            raise LookupError("No such confirmed fact.")
        if text is not None:
            text = clean(text, MAX_TEXT)
            if len(text) < 3:
                raise ValueError("A fact needs a sentence of at least a few words.")
            entry["text"] = text      # the id stays, so drafts that cite it keep pointing at the owner's current wording
        if tags is not None:
            entry["tags"] = _tag_list(tags)
        save(ctx, data)
    return entry


def delete_fact(ctx: Ctx, fact_id: str) -> str:
    """Confirmed fact: removed. Profile-derived fact: hidden (so it is never used), reversible with `unhide`."""
    with _LOCK:
        data = load(ctx)
        before = len(data["confirmed"])
        data["confirmed"] = [c for c in data["confirmed"] if c["id"] != fact_id]
        if len(data["confirmed"]) < before:
            save(ctx, data)
            return "deleted"
        if fact_id not in data["hidden"]:
            data["hidden"].append(fact_id)
            save(ctx, data)
        return "hidden"


def unhide(ctx: Ctx, fact_id: str) -> None:
    with _LOCK:
        data = load(ctx)
        if fact_id in data["hidden"]:
            data["hidden"].remove(fact_id)
            save(ctx, data)


def set_prefs(ctx: Ctx, raw: dict) -> dict:
    with _LOCK:
        data = load(ctx)
        data["prefs"] = clean_prefs(raw, data["prefs"])
        save(ctx, data)
        return data["prefs"]


def set_aliases(ctx: Ctx, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("aliases must map a word to the word it means (for example postgres to postgresql).")
    out = {}
    for k, v in list(raw.items())[:200]:
        a, b = clean(str(k), 60).lower(), clean(str(v), 60).lower()
        if phrase_key(a) and phrase_key(b) and phrase_key(a) != phrase_key(b):
            out[a] = b
    with _LOCK:
        data = load(ctx)
        data["aliases"] = out
        save(ctx, data)
    return out


# ---- extraction from the profile -----------------------------------------------------------------------------------

_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_NOW_WORDS = re.compile(r"present|current|now|ongoing|today", re.I)
_NUMBER = re.compile(r"(?:[$€£₹]\s?)?\d[\d,]*(?:\.\d+)?\s?(?:%|[kKmMbB]\b|x\b|\+)?")


def span_of(period: str, this_year: int) -> tuple[int, int] | None:
    years = [int(y) for y in _YEAR.findall(period or "")]
    if not years:
        return None
    start = min(years)
    end = this_year if _NOW_WORDS.search(period or "") else max(years)
    return (start, max(start, end)) if start <= this_year + 1 else None


def metrics_in(text: str) -> list[str]:
    out = []
    for m in _NUMBER.finditer(text or ""):
        raw = m.group(0).strip()
        digits = re.sub(r"[^\d.]", "", raw)
        if not digits or re.fullmatch(r"(?:19|20)\d{2}", digits):
            continue
        has_unit = bool(re.search(r"[%$€£₹kKmMbBx+]", raw))
        try:
            big = float(digits) >= 10
        except ValueError:
            continue
        if has_unit or big:
            out.append(raw)
    return out


def _as_list(value: Any) -> list:
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"[\n;]", value) if s.strip()]
    return list(value) if isinstance(value, (list, tuple)) else []


class FactBase:
    """The facts as a list of dicts `{id, type, text, source, ...}` with lookups. Build with `FactBase.build(...)`."""

    def __init__(self, facts: list[dict], aliases: dict, prefs: dict, this_year: int):
        self.facts = facts
        self.by_id = {f["id"]: f for f in facts}
        self.aliases = aliases
        self.prefs = prefs
        self.this_year = this_year
        self._tok: dict[str, set[str]] = {}

    # -- building ------------------------------------------------------------------------------------------
    @classmethod
    def build(cls, profile: dict, store: dict | None = None, *, this_year: int | None = None) -> "FactBase":
        store = store or {"confirmed": [], "hidden": [], "aliases": {}, "prefs": dict(DEFAULT_PREFS)}
        this_year = this_year or dt.date.today().year
        profile = profile if isinstance(profile, dict) else {}
        facts: list[dict] = []
        seen: set[str] = set()

        def add(kind: str, text: str, source: str, **extra) -> dict | None:
            text = clean(text, MAX_TEXT)
            if not text:
                return None
            f = {"id": extra.pop("id", None) or fid(kind, text), "type": kind, "text": text, "source": source, **extra}
            if f["id"] in seen:
                return next((x for x in facts if x["id"] == f["id"]), None)
            seen.add(f["id"])
            facts.append(f)
            return f

        add("name", str(profile.get("name") or ""), "profile name")
        add("headline", str(profile.get("headline") or ""), "profile headline")
        for n, sentence in enumerate(re.split(r"(?<=[.!?])\s+", str(profile.get("summary") or "").strip())):
            if len(sentence) > 8:
                add("summary", sentence, f"profile summary sentence {n + 1}")
        roles: list[dict] = []
        for idx, entry in enumerate(_as_list(profile.get("experience"))):
            if isinstance(entry, str):
                add("bullet", entry, f"profile experience {idx + 1}")
                continue
            if not isinstance(entry, dict):
                continue
            title = clean(str(entry.get("title") or entry.get("role") or ""), 120)
            company = clean(str(entry.get("company") or ""), 120)
            period = clean(str(entry.get("period") or entry.get("dates") or ""), 60)
            label = " at ".join(p for p in (title, company) if p)
            if not label:
                continue
            span = span_of(period, this_year)
            role = add("role", label + (f" ({period})" if period else ""), f"profile experience {idx + 1}", title=title, company=company,
                       period=period, start=span[0] if span else None, end=span[1] if span else None, bullets=[])
            if role is None:
                continue
            roles.append(role)
            for b_i, b in enumerate(_as_list(entry.get("bullets") or entry.get("highlights") or entry.get("summary"))):
                bullet = add("bullet", str(b), f"profile experience {idx + 1}, bullet {b_i + 1}", role_id=role["id"], order=b_i)
                if bullet is None:
                    continue
                role["bullets"].append(bullet["id"])
                for m in metrics_in(bullet["text"]):
                    add("metric", m, f"number in bullet {b_i + 1} of experience {idx + 1}", parent=bullet["id"], role_id=role["id"])
        for line in _as_list(profile.get("education")):
            add("education", str(line), "profile education")
        links = profile.get("links")
        pairs = list(links.items()) if isinstance(links, dict) else [("", x) for x in _as_list(links)]
        for label, url in pairs:
            url = clean(str(url or ""), 300)
            if url:
                add("link", (f"{clean(str(label), 40)}: " if label else "") + url, "profile link", label=clean(str(label), 40).lower(), url=url)
        for q, a in list((profile.get("standard_answers") or {}).items())[:60] if isinstance(profile.get("standard_answers"), dict) else []:
            if str(a or "").strip():
                add("answer", f"{clean(str(q), 120)}: {clean(str(a), 400)}", "your standard answers", question=clean(str(q), 120), value=clean(str(a), 400))
        raw_skills = profile.get("skills")
        if isinstance(raw_skills, str):
            raw_skills = re.split(r"[,;\n]", raw_skills)
        skills = [clean(str(s), 60) for s in _as_list(raw_skills) if clean(str(s), 60)]
        for s in dict.fromkeys(skills):
            add("skill", s, "profile skills")
        for c in store.get("confirmed", []):
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            facts.append({"id": c["id"], "type": "confirmed", "text": c["text"], "source": "confirmed by you", "tags": c.get("tags", []),
                          "created": c.get("created", "")})
        covered: set[int] = set()
        for r in roles:
            if r.get("start") is not None and r.get("end") is not None:
                covered.update(range(r["start"], r["end"]))
        if covered:
            facts.append({"id": "calc-years", "type": "derived", "text": f"{len(covered)} years of experience (counted from your role dates)",
                          "source": "derived from your resume dates", "years": len(covered)})
        prefs = store.get("prefs") or DEFAULT_PREFS
        for key, label in (("rate", "Rate"), ("availability", "Availability"), ("location", "Location")):
            if prefs.get(key):
                facts.append({"id": f"pref-{key}", "type": "pref", "text": f"{label}: {prefs[key]}", "source": f"your preference '{key}'", "value": prefs[key]})
        hidden = set(store.get("hidden") or [])
        facts = [f for f in facts if f["id"] not in hidden]
        aliases = {phrase_key(k): phrase_key(v) for k, v in (store.get("aliases") or {}).items() if phrase_key(k) and phrase_key(v)}
        fb = cls(facts, aliases, prefs, this_year)
        fb._link_skills()
        return fb

    def _link_skills(self) -> None:
        """Evidence for each skill: the bullets and roles that mention it. Years: the union of those roles' date spans."""
        bullets = [f for f in self.facts if f["type"] == "bullet"]
        for f in self.facts:
            if f["type"] != "skill":
                continue
            key, want = phrase_key(f["text"]), set(tokens(f["text"]))
            ev, role_ids = [], []
            for b in bullets:
                words = self.tokens_of(b)
                if (want and want <= words) or (key and key in phrase_key(b["text"]) and len(key) >= 3):
                    ev.append(b["id"])
                    if b.get("role_id") and b["role_id"] not in role_ids:
                        role_ids.append(b["role_id"])
            f["evidence"] = ev
            f["role_ids"] = role_ids
            f["years"] = self._years_of([self.by_id[r] for r in role_ids if r in self.by_id])

    # -- reading ------------------------------------------------------------------------------------------------
    def tokens_of(self, fact: dict) -> set[str]:
        got = self._tok.get(fact["id"])
        if got is None:
            got = signal_tokens(fact["text"], self.aliases) | {self.aliases.get(t, t) for t in tokens(fact["text"])}
            self._tok[fact["id"]] = got
        return got

    def of_type(self, *types: str) -> list[dict]:
        return [f for f in self.facts if f["type"] in types]

    def roles(self) -> list[dict]:
        return self.of_type("role")

    def get(self, fact_id: str) -> dict | None:
        return self.by_id.get(fact_id)

    def name(self) -> str:
        n = self.of_type("name")
        return n[0]["text"] if n else ""

    def _years_of(self, roles: list[dict]) -> int:
        covered: set[int] = set()
        for r in roles:
            if r.get("start") is not None and r.get("end") is not None:
                covered.update(range(r["start"], r["end"]))
        return len(covered)

    def years_experience(self) -> int:
        """Years covered by the dated roles (overlaps counted once)."""
        return self._years_of(self.roles())

    def index_tokens(self) -> set[str]:
        if getattr(self, "_index", None) is None:
            out: set[str] = set()
            for f in self.facts:
                if f["type"] not in ("pref",):
                    out |= self.tokens_of(f)
            self._index = out
            self._resolved: dict[str, str] = {}
        return self._index

    def resolve(self, word: str) -> str:
        """The owner's spelling of a posting word when it is a near spelling of one of theirs, else the word (aliases applied first)."""
        word = self.aliases.get(word, word)
        index = self.index_tokens()
        if word in index or len(word) < 7:
            return word
        got = self._resolved.get(word)
        if got is None:
            got = next((p for p in index if len(p) >= 7 and same_word(word, p)), word)
            self._resolved[word] = got
        return got

    def all_text_tokens(self, ids: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for i in ids:
            f = self.by_id.get(i)
            if f:
                out |= {canon(t) for t in tokens(f["text"], drop_stop=False)}
                out |= self.tokens_of(f)
        return out

    def link(self, kind: str) -> dict | None:
        """First link fact whose label or address names `kind` ('github', 'linkedin', 'portfolio', 'website', 'email')."""
        links = self.of_type("link")
        for f in links:
            blob = (f.get("label", "") + " " + f.get("url", "")).lower()
            if kind in blob or (kind == "email" and "@" in f.get("url", "")):
                return f
        if kind in ("portfolio", "website"):
            for f in links:
                blob = f.get("url", "").lower()
                if "@" not in blob and not any(x in blob for x in ("linkedin", "github")):
                    return f
        return None

    def email(self) -> str:
        f = self.link("email")
        return (f or {}).get("url", "")

    def public(self) -> list[dict]:
        out = []
        for f in self.facts:
            row = {k: v for k, v in f.items() if k not in ("bullets",)}
            row["editable"] = f["type"] == "confirmed"
            out.append(row)
        return out


def job_terms(job: dict, fb: "FactBase | dict | None" = None) -> Counter:
    """Weighted words of a posting: title words count extra, repeats count up to three times. With a fact base, a posting word that is
    a near spelling of one of the owner's words ('postgresql' / 'postgres') is mapped to the owner's word."""
    resolve = fb.resolve if isinstance(fb, FactBase) else (lambda t: (fb or {}).get(t, t))
    c: Counter = Counter()
    for t in tokens(str(job.get("description") or "")):
        t = resolve(t)
        if t in FILLER or t.isdigit() or len(t) < 2:
            continue
        c[t] = min(3, c[t] + 1)
    for t in tokens(str(job.get("title") or "")):
        t = resolve(t)
        if t not in FILLER and not t.isdigit():
            c[t] += 2
    return c


def relevance(fb: FactBase, fact: dict, terms: Counter) -> float:
    """How much a fact speaks to a posting: sum of the weights of the posting words it contains. Tech-looking words count double,
    job-level words ('engineer', 'senior') count a quarter: they are in every resume."""
    score = 0.0
    for t in fb.tokens_of(fact):
        w = terms.get(t)
        if w:
            score += w * (2.0 if re.search(r"[0-9+#]", t) else 0.25 if t in ROLE_WORDS else 1.0)
    return score
