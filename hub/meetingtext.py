"""Pure text helpers for the meeting assistant (hub/meetings.py): stitching overlapping chunks, due dates,
the no-AI extractor and the clean-up of model output. No I/O, no clock of its own: easy to test with tables.

Everything here treats the transcript as untrusted DATA: it only searches it, it never executes or follows it.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

OWNERS = ("me", "others", "unknown")
MAX_BULLETS = {"summary": 10, "decisions": 20, "risks": 15, "questions": 15, "actions": 40}
MAX_LINE = 300

# ---- stitching ---------------------------------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9']+")


def _norm_words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower())


def stitch(previous: str, new: str, max_words: int = 24, min_match: int = 2) -> str:
    """`new` without its beginning when that repeats the end of `previous` (the overlap between two chunks).

    Matching ignores case and punctuation; at least `min_match` words must repeat, so one repeated "no" is kept."""
    def tokens(text: str) -> list[str]:
        return [" ".join(_norm_words(w)) for w in str(text or "").split()]
    prev_words = [w for w in tokens(previous) if w][-max_words:]
    raw = str(new or "").split()
    if not raw or not prev_words:
        return " ".join(raw)
    new_words = tokens(new)
    best = 0
    for n in range(min(len(prev_words), len(new_words), max_words), min_match - 1, -1):
        if n >= min_match and prev_words[-n:] == new_words[:n]:
            best = n
            break
    return " ".join(raw[best:])


def stamp(seconds: float) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def transcript_lines(segments: list[dict]) -> list[str]:
    return [f"[{stamp(s.get('t', 0))}] {s.get('track', 'me')}: {s.get('text', '')}" for s in segments if s.get("text")]


def split_parts(lines: list[str], limit: int = 3000) -> list[str]:
    """Group transcript lines into parts of at most `limit` characters (a long line is cut)."""
    parts, cur, size = [], [], 0
    for line in lines:
        while len(line) > limit:
            if cur:
                parts.append("\n".join(cur))
                cur, size = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and cur:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


# ---- due dates ---------------------------------------------------------------------------------------------------
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_DAY_RE = "|".join(DAYS + tuple(d[:3] for d in DAYS if d[:3] != "sun") + ("sun",))
_MON_RE = "|".join(MONTHS) + "|" + "|".join(m[:3] for m in MONTHS if m != "may")


def _next_weekday(today: dt.date, idx: int) -> dt.date:
    delta = (idx - today.weekday()) % 7
    return today + dt.timedelta(days=delta or 7)


def _weekday_index(word: str) -> int:
    word = word.lower()
    for i, d in enumerate(DAYS):
        if d.startswith(word[:3]):
            return i
    return 0


def parse_due(text: Any, today: dt.date) -> str:
    """An ISO date for a due phrase in `text` ("by Friday", "tomorrow", "March 5th", "2026-12-01"), or ''."""
    t = " " + " ".join(str(text or "").lower().split()) + " "
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass
    if m := re.search(rf"\b({_MON_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t) or \
            re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON_RE})\b", t):
        a, b = m[1], m[2]
        mon, day = (a, b) if a[0].isalpha() else (b, a)
        idx = next((i for i, name in enumerate(MONTHS) if name.startswith(mon[:3])), None)
        if idx is not None:
            try:
                d = dt.date(today.year, idx + 1, int(day))
                if d < today:
                    d = dt.date(today.year + 1, idx + 1, int(day))
                return d.isoformat()
            except ValueError:
                pass
    if re.search(r"\b(?:today|tonight|eod|end of (?:the )?day|by close of business|cob)\b", t):
        return today.isoformat()
    if re.search(r"\btomorrow\b", t):
        return (today + dt.timedelta(days=1)).isoformat()
    if m := re.search(r"\bin (\d{1,2}) (day|days|week|weeks)\b", t):
        return (today + dt.timedelta(days=int(m[1]) * (7 if m[2].startswith("week") else 1))).isoformat()
    if re.search(r"\b(?:end of (?:the |this )?week|eow)\b", t):
        delta = (4 - today.weekday()) % 7
        return (today + dt.timedelta(days=delta)).isoformat()
    if re.search(r"\bnext week\b", t):
        return (today + dt.timedelta(days=7 - today.weekday())).isoformat()    # next Monday
    if m := re.search(rf"\b(?:by|on|this|next|before|until|till|for)?\s*\b({_DAY_RE})\b", t):
        return _next_weekday(today, _weekday_index(m[1])).isoformat()
    return ""


# ---- the no-AI extractor -----------------------------------------------------------------------------------------
_SENT = re.compile(r"(?<=[.!?])\s+")
_ACTION = re.compile(
    r"\b(?:will|todo|to-do|to do|action(?: item| point)?|need(?:s)? to|has to|have to|must|deadline|due|follow(?:ing)? up|"
    r"let me|can you|could you|please|i'll|we'll|they'll|he'll|she'll)\b|'ll\b|"
    rf"\bby (?:(?:next |this )?(?:{_DAY_RE})|tomorrow|eod|end of|\d)", re.I)
_DECISION = re.compile(r"\b(?:decided|decision|agreed|we(?:'ll| will) go with|let's go with|going with|approved|settled on|final call|signed off)\b", re.I)
_RISK = re.compile(r"\b(?:risk|blocker|blocked|blocking|concern|concerned|worried|worry|might (?:fail|slip|break)|problem|delay|delayed|dependency|issue)\b", re.I)
_QUESTION = re.compile(r"\b(?:open question|not sure|don't know|do not know|tbd|to be decided|need to find out)\b", re.I)
_FIRST = re.compile(r"\b(?:i|i'll|i will|i'm going to|i am going to|let me|my action|i can take|i'd)\b", re.I)
_YOU = re.compile(r"\b(?:can you|could you|would you|will you|you'll|you will|you need to|you should|please)\b", re.I)
_NAME = re.compile(r"\b([A-Z][a-z]{1,19})\s+(?:will|'ll|needs to|has to|is going to|can|should|to)\b")
_NOT_NAMES = {"This", "That", "It", "There", "We", "They", "He", "She", "You", "I", "What", "Who", "When", "Then", "So", "And",
              "But", "If", "The", "That's", "Which", "Okay", "Ok", "Yes", "No", "Well", "Also", "Let", "Please", "Maybe"}


def sentences(segments: list[dict]) -> list[dict]:
    out = []
    for seg in segments:
        for s in _SENT.split(str(seg.get("text") or "").strip()):
            s = s.strip()
            if s:
                out.append({"text": s, "track": seg.get("track", "me"), "t": seg.get("t", 0)})
    return out


def guess_owner(sentence: str, track: str, me_names: tuple[str, ...] = ()) -> tuple[str, str]:
    """(owner, who) for one sentence said on `track` ('me' = the laptop owner)."""
    if m := _NAME.search(sentence):
        name = m[1]
        if name not in _NOT_NAMES:
            if name.lower() in {n.lower() for n in me_names}:
                return "me", ""
            return "others", name
    if _YOU.search(sentence) and not _FIRST.search(sentence):
        return ("me", "") if track == "others" else ("others", "")
    if _FIRST.search(sentence):
        return ("me", "") if track == "me" else ("others", "")
    return "unknown", ""


def _clip(text: str, n: int = MAX_LINE) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def extract_fallback(segments: list[dict], today: dt.date, me_names: tuple[str, ...] = (), duration_s: float = 0.0) -> dict:
    """Notes without any AI: lines that look like actions, decisions, risks and questions, found by plain patterns."""
    sents = sentences(segments)
    actions, decisions, risks, questions, seen = [], [], [], [], set()

    def fresh(text: str) -> bool:
        key = " ".join(_norm_words(text))
        if key in seen:
            return False
        seen.add(key)
        return True

    for s in sents:
        text, words = s["text"], len(_norm_words(s["text"]))
        if words < 3:
            continue
        is_q = text.rstrip().endswith("?")
        if _DECISION.search(text) and fresh("d" + text):
            decisions.append(_clip(text))
        if _RISK.search(text) and not is_q and fresh("r" + text):
            risks.append(_clip(text))
        if (is_q and words >= 4) or _QUESTION.search(text):
            if fresh("q" + text):
                questions.append(_clip(text))
        if _ACTION.search(text) and words >= 4 and (not is_q or _YOU.search(text)) and fresh("a" + text):
            owner, who = guess_owner(text, s["track"], me_names)
            due = parse_due(text, today)
            actions.append({"text": _clip(text, 240), "owner": owner, "who": who, "due": due, "due_text": "", "t": s["t"]})
    key_sents = sorted((s for s in sents if len(_norm_words(s["text"])) >= 10), key=lambda s: -len(s["text"]))[:4]
    key_sents.sort(key=lambda s: s["t"])
    mins = max(1, round((duration_s or (segments[-1].get("t", 0) if segments else 0)) / 60))
    mine = sum(1 for s in segments if s.get("track") == "me")
    summary = [f"About {mins} min recorded; {mine} stretches from you and {len(segments) - mine} from others. "
               "No AI summary was made: these are plain pattern matches, please edit."]
    summary += [_clip(s["text"], 200) for s in key_sents]
    return {"summary": summary[: MAX_BULLETS["summary"]], "decisions": decisions[:MAX_BULLETS["decisions"]],
            "risks": risks[:MAX_BULLETS["risks"]], "questions": questions[:MAX_BULLETS["questions"]],
            "actions": actions[:MAX_BULLETS["actions"]]}


# ---- cleaning notes (from a model or from the owner's edits) --------------------------------------------------
def _num(value: Any) -> float:
    try:
        v = float(value)
        return v if v == v and 0 <= v < 10 ** 6 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _strings(value: Any, key: str) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, dict):
            v = v.get("text") or v.get("title") or ""
        s = _clip(v)
        if s and s not in out:
            out.append(s)
    return out[: MAX_BULLETS[key]]


def clean_notes(raw: Any, today: dt.date, keep: dict | None = None) -> dict:
    """The notes object with every field forced to a safe shape. `keep` maps an action id to what must survive an edit."""
    raw = raw if isinstance(raw, dict) else {}
    actions = []
    for i, a in enumerate((raw.get("actions") if isinstance(raw.get("actions"), list) else [])[: MAX_BULLETS["actions"]], 1):
        if isinstance(a, str):
            a = {"text": a}
        if not isinstance(a, dict):
            continue
        text = _clip(a.get("text") or a.get("title") or "", 240)
        if not text:
            continue
        owner = str(a.get("owner") or "unknown").strip().lower()
        owner = owner if owner in OWNERS else ("me" if owner in ("i", "owner", "self") else "unknown")
        due_text = _clip(a.get("due_text") or (a.get("due") if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a.get("due") or "")) else ""), 60)
        due = str(a.get("due") or "")
        due = due if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else parse_due(a.get("due") or "", today)
        aid = str(a.get("id") or f"a{i}")
        row = {"id": re.sub(r"[^a-z0-9_-]", "", aid.lower())[:12] or f"a{i}", "text": text, "owner": owner,
               "who": _clip(a.get("who"), 40), "due": due, "due_text": due_text if not due else "", "t": round(_num(a.get("t")), 1),
               "done": a.get("done") is True, "added": a.get("added") is True, "brain_id": _clip(a.get("brain_id"), 40)}
        actions.append(row)
    ids, fixed = set(), []
    for n, row in enumerate(actions, 1):
        while row["id"] in ids:
            row["id"] = f"{row['id']}x"[:12]
        ids.add(row["id"])
        fixed.append(row)
    return {"summary": _strings(raw.get("summary"), "summary"), "decisions": _strings(raw.get("decisions"), "decisions"),
            "risks": _strings(raw.get("risks"), "risks"), "questions": _strings(raw.get("questions"), "questions"),
            "actions": fixed, "method": raw.get("method") if raw.get("method") in ("ai", "fallback", "manual") else "manual",
            "note": _clip(raw.get("note"), 300), "generated": _clip(raw.get("generated"), 30)}


def merge_notes(parts: list[dict]) -> dict:
    """Put the notes of several transcript parts together, without repeating a line."""
    out: dict[str, list] = {k: [] for k in MAX_BULLETS}
    seen: dict[str, set] = {k: set() for k in MAX_BULLETS}
    for p in parts:
        for key in MAX_BULLETS:
            for item in p.get(key) or []:
                ident = " ".join(_norm_words(item["text"] if isinstance(item, dict) else item))
                if ident and ident not in seen[key]:
                    seen[key].add(ident)
                    out[key].append(item)
    for key, cap in MAX_BULLETS.items():
        rows = out[key]
        if len(rows) > cap:                      # keep an even spread rather than only the start
            step = len(rows) / cap
            out[key] = [rows[int(i * step)] for i in range(cap)]
    return out


# ---- search over meetings ----------------------------------------------------------------------------------------
def date_range(query: str, today: dt.date) -> tuple[tuple[dt.date, dt.date] | None, str]:
    """(from, to) for a phrase like 'last week' in the query, and the query without that phrase."""
    q = " " + str(query or "").lower() + " "
    monday = today - dt.timedelta(days=today.weekday())
    rules = (
        (r"\blast week\b", (monday - dt.timedelta(days=7), monday - dt.timedelta(days=1))),
        (r"\bthis week\b", (monday, today)),
        (r"\byesterday\b", (today - dt.timedelta(days=1),) * 2),
        (r"\btoday\b", (today, today)),
        (r"\blast month\b", ((today.replace(day=1) - dt.timedelta(days=1)).replace(day=1), today.replace(day=1) - dt.timedelta(days=1))),
        (r"\bthis month\b", (today.replace(day=1), today)),
    )
    for pattern, span in rules:
        if re.search(pattern, q):
            return span, " ".join(re.sub(pattern, " ", q).split())
    return None, " ".join(q.split())
