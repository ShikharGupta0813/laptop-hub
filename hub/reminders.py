"""Reminders, timers and alarms, with a rule-based "just type it" parser (no model needed).

- Reminders: text + due time + optional repeat (daily, weekdays, weekly, monthly, yearly, every N minutes).
- Timers: countdowns that notify when they reach zero.
- Alarm: simply a reminder (usually repeating) whose text is "Alarm"; the quick parser understands
  "alarm 6:30 weekdays" and "wake me up at 7".
- Scheduler: `start(ctx)` runs a loop (wakes at least every 15 seconds) that fires what is due through
  `ctx.notify(...)` exactly once, persists that it did, catches up after downtime ("missed" wording,
  never more than 10 notifications in one go) and moves repeating reminders to their next time.

Times are kept as ISO text with the local UTC offset. Repeats are worked out on the wall clock ("daily 8:30"
stays 8:30 across a clock change) from an `anchor`, so "monthly on the 31st" goes Jan 31, Feb 28, Mar 31.
Everything that changes data runs on the event loop thread, so the stored lists need no lock.
"""
from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from .common import Ctx, JsonFile

log = logging.getLogger("hub.reminders")

TICK_SECONDS = 15.0
MISSED_AFTER = 120          # seconds late before a notification says "missed"
MISSED_CAP = 10             # most missed items announced one by one in a single pass
MAX_REMINDERS = 500
MAX_TIMERS = 50
MAX_TEXT = 300
MAX_CALL_TEXT = 500         # a "call" reminder is read out in full, so it may be longer
MAX_CALL_INPUT = 800        # the whole spoken phrase for a call reminder (words about the time included)
MAX_TIMER_SECONDS = 7 * 86400
KEEP_DONE_DAYS = 30
KEEP_TIMER_HOURS = 24
REPEATS = ("none", "daily", "weekdays", "weekly", "monthly", "yearly", "every_n_minutes")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY3 = [d[:3] for d in DAY_NAMES]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]
MONTH3 = [m[:3] for m in MONTH_NAMES]


# ---- time helpers ------------------------------------------------------------------------------------

class LocalTZ(tzinfo):
    """The computer's own time zone, daylight saving included, without needing a zone database."""

    def utcoffset(self, dt):
        return dt.replace(tzinfo=None).astimezone().utcoffset()

    def dst(self, dt):
        return dt.replace(tzinfo=None).astimezone().dst()

    def tzname(self, dt):
        return dt.replace(tzinfo=None).astimezone().tzname()

    def fromutc(self, dt):
        aware = dt.replace(tzinfo=timezone.utc).astimezone()
        out = aware.replace(tzinfo=None).replace(tzinfo=self)
        if out.utcoffset() != aware.utcoffset():
            out = out.replace(fold=1)
        return out

    def __repr__(self):
        return "LocalTZ()"


def localize(naive: datetime, tz: tzinfo) -> datetime:
    """A wall-clock time in `tz`. A time the clocks skip (spring forward) moves to the next valid minute."""
    return naive.replace(tzinfo=tz).astimezone(timezone.utc).astimezone(tz)


def iso(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).isoformat(timespec="seconds")


def parse_iso(value: Any, tz: tzinfo) -> datetime:
    """ISO text -> aware datetime in `tz`. A time with no offset is taken as local time."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty")
    dt = datetime.fromisoformat(value.strip())
    if not 1999 <= dt.year <= 2101:      # checked before the zone maths: years like 0001 or 9999 crash it on Windows
        raise ValueError("range")
    try:
        dt = localize(dt, tz) if dt.tzinfo is None else dt.astimezone(tz)
    except (OSError, OverflowError) as exc:
        raise ValueError("range") from exc
    if not 2000 <= dt.year <= 2100:
        raise ValueError("range")
    return dt


def add_months(d: date, months: int) -> date:
    year, month0 = divmod(d.year * 12 + d.month - 1 + months, 12)
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def occurrence(anchor: datetime, repeat: str, k: int) -> datetime:
    """The k-th wall-clock time of a repeat that started at `anchor` (a naive local time)."""
    if repeat == "daily":
        return anchor + timedelta(days=k)
    if repeat == "weekly":
        return anchor + timedelta(days=7 * k)
    if repeat == "monthly":
        return datetime.combine(add_months(anchor.date(), k), anchor.time())
    if repeat == "yearly":
        return datetime.combine(add_months(anchor.date(), 12 * k), anchor.time())
    raise ValueError(repeat)


def next_occurrence(anchor: datetime, repeat: str, n: int | None, after: datetime, tz: tzinfo,
                    due: datetime | None = None) -> datetime:
    """The first time of the repeat that is strictly after `after`."""
    if repeat == "every_n_minutes":
        step = (n or 1) * 60
        base = due or after
        k = max(0, int((after - base).total_seconds() // step) + 1)
        return base + timedelta(seconds=step * k)
    wall = after.astimezone(tz).replace(tzinfo=None)
    if repeat == "weekdays":
        day = max(anchor.date(), wall.date() - timedelta(days=1))
        for _ in range(30):
            if day.weekday() < 5:
                candidate = localize(datetime.combine(day, anchor.time()), tz)
                if candidate > after:
                    return candidate
            day += timedelta(days=1)
        raise ValueError("no weekday found")
    gap_days = (wall.date() - anchor.date()).days
    start = {"daily": gap_days - 1, "weekly": gap_days // 7 - 1,
             "monthly": (wall.year - anchor.year) * 12 + wall.month - anchor.month - 1,
             "yearly": wall.year - anchor.year - 1}[repeat]
    for k in range(max(0, start), max(0, start) + 5000):
        candidate = localize(occurrence(anchor, repeat, k), tz)
        if candidate > after:
            return candidate
    raise ValueError("no next time found")


def fmt_time(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def fmt_day(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"
    if days == -1:
        return "Yesterday"
    if 2 <= days <= 6:
        return DAY_NAMES[dt.weekday()]
    year = f" {dt.year}" if dt.year != now.year else ""
    return f"{DAY3[dt.weekday()]} {dt.day} {MONTH3[dt.month - 1]}{year}"


def describe_when(dt: datetime, now: datetime, tz: tzinfo) -> str:
    dt, now = dt.astimezone(tz), now.astimezone(tz)
    return f"{fmt_day(dt, now)} {fmt_time(dt)}"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe_repeat(repeat: str, n: int | None, anchor: datetime) -> str:
    t = fmt_time(anchor)
    if repeat == "daily":
        return f"Every day {t}"
    if repeat == "weekdays":
        return f"Every weekday {t}"
    if repeat == "weekly":
        return f"Every {DAY_NAMES[anchor.weekday()]} {t}"
    if repeat == "monthly":
        return f"Every month on the {ordinal(anchor.day)}, {t}"
    if repeat == "yearly":
        return f"Every year on {anchor.day} {MONTH3[anchor.month - 1]}, {t}"
    n = n or 1
    if n == 1:
        return "Every minute"
    if n == 60:
        return "Every hour"
    return f"Every {n // 60} hours" if n % 60 == 0 else f"Every {n} minutes"


def describe_seconds(seconds: int) -> str:
    parts = []
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count} {name}{'' if count == 1 else 's'}")
    return " ".join(parts[:2]) or "0 seconds"


def clean_text(value: Any, what: str = "reminder", limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, f"Write what the {what} is about.")
    text = " ".join(CONTROL_RE.sub(" ", value).split())
    if not text:
        raise HTTPException(400, f"Write what the {what} is about.")
    if len(text) > limit:
        raise HTTPException(400, f"That is too long. Keep it under {limit} characters.")
    return text


def clean_call(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise HTTPException(400, "call must be true or false.")
    return value


# ---- the quick parser --------------------------------------------------------------------------------

class ParseError(ValueError):
    """The phrase could not be understood; the message is a plain sentence for the user."""


@dataclass
class Parsed:
    kind: str                      # "reminder" | "timer"
    text: str
    echo: str
    due: datetime | None = None
    repeat: str = "none"
    n: int | None = None
    seconds: int = 0               # timers
    call: bool = False             # ring the phone and read the full text out (see hub/alerts.py push_phone)


WHEN_HELP = ("I could not tell when. Try something like 'tomorrow at 9am', 'in 10 minutes', "
             "'friday 5pm' or 'every weekday 8:30'.")
NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
            "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty[ -]?five|forty|fifty|sixty|ninety"
NUM = rf"(?:\d+(?:\.\d+)?|a|an|{_WORDS})"
NUM2 = rf"(?:\d+|{_WORDS})"
HOURWORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|h|m)"
DUR1 = rf"(?:half an hour|an hour and a half|{NUM}\s*{UNIT}\b)"
DURLIST = rf"{DUR1}(?:\s*(?:,|and)?\s*{DUR1})*"
MON = "|".join(sorted([m.lower() for m in MONTH_NAMES] + ["jan", "feb", "mar", "apr", "jun", "jul", "aug",
                                                         "sept", "sep", "oct", "nov", "dec"], key=len, reverse=True))
WD_FULL = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
WD_ABBR = "tues|thurs|thur|mon|tue|wed|thu|fri|sat|sun"
MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(MONTH_NAMES)}
MONTH_INDEX.update({m.lower(): i + 1 for i, m in enumerate(MONTH3)})
MONTH_INDEX["sept"] = 9
WD_INDEX = {name: i for i, name in enumerate(WD_FULL.split("|"))}
WD_INDEX.update({"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6})
POD_HOURS = {"morning": 9, "afternoon": 14, "evening": 18, "night": 20}
NOUNS = r"things|items|people|times|minutes?|mins?|hours?|days?|weeks?|months?|years?|dollars?|bucks|pounds|euros|kg|km|miles"


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


FILLER_RX = _rx(
    r"^\s*(?:(?:hey|ok|okay|hi)[ ,]+)?(?:jarvis[ ,:]+)?(?:(?:please|pls)\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:remind\s+me|set\s+(?:me\s+)?(?:a\s+|an\s+)?(?:reminder|alarm)|add\s+(?:a\s+)?reminder|make\s+(?:a\s+)?reminder"
    r"|reminder|remember|don'?t\s+forget|dont\s+forget|wake\s+me\s+up|alarm)\b(?:\s+(?:to|that|about))?[ ,:]*")
REMIND_ANYWHERE_RX = _rx(r"\bremind\s+me\b|\bplease\b")
VAGUE = [
    (_rx(r"\bnext\s+(week|month|year)\b"), "I cannot tell which day you mean by 'next {0}'. Say a day, like 'next Monday 9am'."),
    (_rx(r"\b(?:this\s+|next\s+|every\s+|on\s+the\s+)?(weekends?)\b"), "Which day, Saturday or Sunday? Please say the day."),
    (_rx(r"\bend\s+of\s+(?:the\s+)?(week|month|year)\b"), "Please give a date for the end of the {0}, like '30 nov'."),
    (_rx(r"\b(yesterday|last\s+(?:night|week|month|year)|\d+\s*\w+\s+ago)\b"),
     "That sounds like the past. I can only set reminders for the future."),
    (_rx(r"\b(sometime|someday|eventually|at some point|in a while|a bit later|later on|whenever|asap)\b"),
     "I need an actual time, like 'in 2 hours' or 'tomorrow at 9am'."),
]

REPEAT_RULES: list[tuple[str, re.Pattern]] = [
    ("bad_n", _rx(rf"\bevery\s+(?:other|{NUM2})\s+(?:days?|weeks?|months?|years?)\b")),
    ("n_minutes", _rx(rf"\bevery\s+(?P<n>{NUM2})\s*(?P<u>minutes?|mins?|hours?|hrs?|h|m)\b")),
    ("half_hour", _rx(r"\bevery\s+half\s+(?:an\s+)?hour\b")),
    ("hour", _rx(r"\b(?:every\s+hour|hourly)\b")),
    ("minute", _rx(r"\bevery\s+minute\b")),
    ("weekdays", _rx(r"\b(?:(?:every|each)\s+(?:week\s*day|work\s*day)s?|(?:on\s+)?(?:week\s*days|work\s*days)"
                     r"|mon(?:day)?\s*(?:-|to|through|thru)\s*fri(?:day)?)\b")),
    ("daily_pod", _rx(r"\b(?:every|each)\s+(?P<pod>morning|afternoon|evening|night)\b")),
    ("daily", _rx(r"\b(?:every\s*day|each\s+day|daily)\b")),
    ("weekly_plural", _rx(rf"\b(?:(?:every|each|on)\s+)?(?P<wd>{WD_FULL})s\b")),
    ("weekly_day", _rx(rf"\b(?:every|each)\s+(?P<wd>{WD_FULL}|{WD_ABBR})\b")),
    ("weekly", _rx(r"\b(?:every\s+week|each\s+week|weekly)\b")),
    ("monthly_day", _rx(r"\b(?:on\s+)?(?:the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)\s+(?:of\s+)?(?:every|each)\s+month\b")),
    ("monthly", _rx(r"\b(?:every\s+month|each\s+month|monthly)\b")),
    ("yearly_date", _rx(rf"\bevery\s+(?=(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{MON})\b|(?:{MON})\.?\s+\d{{1,2}}\b))")),
    ("yearly", _rx(r"\b(?:every\s+year|each\s+year|yearly|annually)\b")),
]
# "call me at 5 ...", "ring me ...", "... by call": the reminder rings the phone. "remind me to call mom" is NOT one.
CALL_RX = _rx(r"\b(?:(?:by|via|through|with|using)\s+(?:a\s+|an\s+)?(?:phone\s+)?(?:call|ring)|give\s+me\s+a\s+(?:call|ring)"
              r"|(?:call|ring|phone)\s+me)\b[ ,.]*")
STRAY_EVERY_RX = _rx(r"\b(?:every|each)\b")

REL_RX = _rx(rf"(?:\b(?:in|after)\s+(?P<d1>{DURLIST})|(?P<d2>{DURLIST})\s+from\s+now)")
DUR_PIECE_RX = _rx(rf"half an hour|an hour and a half|(?P<num>{NUM})\s*(?P<unit>{UNIT})\b")

POD_RULES = [
    ("daypod", _rx(r"\b(?P<day>this|tomorrow|today)\s+(?P<pod>morning|afternoon|evening|night)\b")),
    ("tonight", _rx(r"\btonight\b")),
    ("inthe", _rx(r"\bin\s+the\s+(?P<pod>morning|afternoon|evening)\b")),
]
_PREFIX = r"(?:\b(?:on|by|for)\s+)?"
DATE_RULES = [
    ("iso", _rx(rf"{_PREFIX}\b(?P<y>\d{{4}})-(?P<m>\d{{2}})-(?P<d>\d{{2}})\b")),
    ("slash", _rx(rf"{_PREFIX}(?<![\d./:])\b(?P<a>\d{{1,2}})/(?P<b>\d{{1,2}})(?:/(?P<y>\d{{4}}|\d{{2}}))?\b")),
    ("dots", _rx(rf"{_PREFIX}\b(?P<a>\d{{1,2}})\.(?P<b>\d{{1,2}})\.(?P<y>\d{{4}})\b")),
    ("day_month", _rx(rf"{_PREFIX}(?:\bthe\s+)?\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{MON})\b\.?"
                      rf"(?:,?\s+(?P<year>20\d{{2}})\b)?")),
    ("month_day", _rx(rf"{_PREFIX}\b(?P<mon>{MON})\.?\s+(?:the\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?::\d|am\b|pm\b))"
                      rf"(?:,?\s+(?P<year>20\d{{2}})\b)?")),
    ("dom", _rx(rf"{_PREFIX}(?:\bthe\s+)?\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)\b")),
    ("dayafter", _rx(r"\bday\s+after\s+tomorrow\b")),
    ("tomorrow", _rx(r"(?:\bfor\s+)?\b(?:tomorrow|tmrw|tmr)\b")),
    ("today", _rx(r"(?:\bfor\s+)?\btoday\b")),
    ("weekday", _rx(rf"(?:\b(?P<pre>on|this|next|by|coming)\s+)?\b(?P<wd>{WD_FULL})\b")),
    ("weekday_abbr", _rx(rf"\b(?P<pre>on|this|next|by|coming)\s+(?P<wd>{WD_ABBR})\b\.?")),
]
_AT = r"(?:(?:\bat|\baround|\bby)\s*|@\s*)?"
MER = r"(?P<mer>[ap])\.?m\b\.?"
TIME_RULES = [
    ("colon", _rx(rf"{_AT}\b(?P<h>\d{{1,2}}):(?P<m>\d{{2}})(?::\d{{2}})?\s*(?:{MER})?")),
    ("dot", _rx(rf"{_AT}\b(?P<h>\d{{1,2}})\.(?P<m>\d{{2}})\s*{MER}")),
    ("hm", _rx(rf"{_AT}\b(?P<h>\d{{1,2}})\s*{MER}")),
    ("oclock", _rx(rf"{_AT}\b(?P<h>\d{{1,2}})\s*o'?clock\b")),
    ("fraction", _rx(rf"{_AT}\b(?P<q>half|quarter)\s+(?P<dir>past|to)\s+(?P<h>\d{{1,2}}|{HOURWORDS})\b")),
    ("noon", _rx(rf"{_AT}\b(?P<w>noon|midday|midnight)\b")),
    ("eod", _rx(r"\b(?:by\s+)?(?:eod|end\s+of\s+(?:the\s+)?day|close\s+of\s+business|cob)\b")),
    ("bare", _rx(rf"(?:\bat|@)\s*(?P<h>\d{{1,2}}|{HOURWORDS})\b(?![:.]\d)(?!\s*(?:st|nd|rd|th|%|{NOUNS})\b)")),
]
TIMER_WORD_RX = _rx(r"\btimers?\b")
EDGE_LEAD = {"to", "that", "about", "and", "then", "also", "me", "please", "for", "at", "on", "by", "in"}
EDGE_TRAIL = {"please", "at", "on", "by", "for", "in", "then", "and", "to", "from", "of"}


class _Work:
    """The phrase being taken apart: each matched piece is cut out and the rest is the task text."""

    def __init__(self, text: str):
        self.s = text

    def take(self, rx: re.Pattern) -> re.Match | None:
        m = rx.search(self.s)
        if m:
            self.s = self.s[:m.start()] + " " + self.s[m.end():]
        return m


def _number(token: str) -> float:
    token = token.lower().replace("-", " ").strip()
    return float(token) if re.fullmatch(r"\d+(?:\.\d+)?", token) else float(NUMWORDS[re.sub(r"\s+", " ", token)])


def _duration(text: str) -> tuple[int, int, int]:
    """(months, days, seconds) for a phrase like '1 hour 30 minutes'."""
    months = days = 0
    seconds = 0.0
    for m in DUR_PIECE_RX.finditer(text):
        word = m.group(0).lower()
        if word.startswith("half"):
            seconds += 1800
            continue
        if word.startswith("an hour and"):
            seconds += 5400
            continue
        value, unit = _number(m.group("num")), m.group("unit").lower()
        if unit.startswith("mon"):
            if value != int(value):
                raise ParseError("I can only handle whole months, like 'in 2 months'.")
            months += int(value)
        elif unit.startswith("y"):
            if value != int(value):
                raise ParseError("I can only handle whole years, like 'in 2 years'.")
            months += 12 * int(value)
        elif unit.startswith("w"):
            days += int(value * 7)
            seconds += (value * 7 - int(value * 7)) * 86400
        elif unit.startswith("d"):
            days += int(value)
            seconds += (value - int(value)) * 86400
        elif unit.startswith("h"):
            seconds += value * 3600
        elif unit.startswith("s"):
            seconds += value
        else:   # m, min, minutes
            seconds += value * 60
    return months, days, int(round(seconds))


def _normalize(text: Any) -> str:
    text = unicodedata.normalize("NFKC", text if isinstance(text, str) else "")
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"(\d)-(?=(?:second|minute|min|hour|hr|day|week|month|year)s?\b)", r"\1 ", text, flags=re.I)
    return " ".join(CONTROL_RE.sub(" ", text).split())


def _tidy(text: str) -> str:
    words = [w for w in re.sub(r"\s+", " ", text).strip(" ,;:.-–—").split(" ") if w]
    while words and words[0].lower().strip(",.;:") in EDGE_LEAD:
        words.pop(0)
    while words and words[-1].lower().strip(",.;:") in EDGE_TRAIL:
        words.pop()
    return " ".join(words).strip(" ,;:.-–—")


def _try_timer(s: str) -> Parsed | None:
    if not TIMER_WORD_RX.search(s) or re.search(r"\bremind", s, re.I):
        return None
    w = _Work(s)
    m = w.take(_rx(rf"(?:\bfor\s+)?(?P<d>{DURLIST})"))
    if not m:
        raise ParseError("How long should the timer run? Try 'timer 10 minutes'.")
    months, days, secs = _duration(m.group("d"))
    if months:
        raise ParseError("Timers are for up to a week. For longer, make a reminder instead.")
    total = days * 86400 + secs
    if total < 1 or total > MAX_TIMER_SECONDS:
        raise ParseError("A timer can run from 1 second up to 7 days.")
    w.s = re.sub(r"\b(?:set|start|make|a|an|me|the|countdown|timers?|called|named|please|label)\b", " ", w.s, flags=re.I)
    label = _tidy(w.s) or "Timer"
    return Parsed("timer", label, f"Timer for {describe_seconds(total)} — {label}" if label != "Timer"
                  else f"Timer for {describe_seconds(total)}", seconds=total)


def _hm_options(rec: dict, bias: str | None) -> tuple[list[tuple[int, int]], bool]:
    """(possible (hour, minute) choices, is_ambiguous) for a spoken time."""
    kind = rec["kind"]
    if kind == "eod":
        return [(17, 0)], False
    if kind == "noon":
        return [{"noon": (12, 0), "midday": (12, 0), "midnight": (0, 0)}[rec["w"].lower()]], False
    h, m = rec["h"], rec["m"]
    if rec.get("mer"):
        if not 1 <= h <= 12:
            raise ParseError(f"'{h} {rec['mer'].lower()}m' is not a time on a 12-hour clock.")
        return [(h % 12 + (12 if rec["mer"].lower() == "p" else 0), m)], False
    if h > 23 or m > 59:
        raise ParseError("That time does not look right.")
    if h == 0 or h >= 13:
        return [(h, m)], False
    if h == 12:
        return [(12, m)], False
    if bias == "am":
        return [(h, m)], False
    if bias == "pm":
        return [(h + 12, m)], False
    return [(h, m), (h + 12, m)], True


def _time_record(name: str, m: re.Match) -> dict:
    g = m.groupdict()
    if name in ("noon", "eod"):
        return {"kind": name, "w": g.get("w") or "eod"}
    if name == "fraction":
        h = int(g["h"]) if g["h"].isdigit() else int(NUMWORDS[g["h"].lower()])
        if not 1 <= h <= 12:
            raise ParseError("That time does not look right.")
        if g["q"].lower() == "half":
            return {"kind": "fraction", "h": h, "m": 30, "mer": None}
        hour = h if g["dir"].lower() == "past" else (h - 1 or 12)
        return {"kind": "fraction", "h": hour, "m": 15 if g["dir"].lower() == "past" else 45, "mer": None}
    h = int(g["h"]) if g["h"].isdigit() else int(NUMWORDS[g["h"].lower()])
    minute = int(g["m"]) if g.get("m") else 0
    if minute > 59:
        raise ParseError("That time does not look right.")
    return {"kind": name, "h": h, "m": minute, "mer": g.get("mer")}


def _day_in_month(year: int, month: int, day: int) -> date:
    if month < 1 or month > 12:
        raise ParseError("That is not a real date.")
    if day < 1 or day > calendar.monthrange(year, month)[1]:
        raise ParseError(f"{MONTH_NAMES[month - 1]} {year} does not have a day {day}.")
    return date(year, month, day)


def _date_record(name: str, m: re.Match, now: datetime) -> dict:
    g = m.groupdict()
    if name == "iso":
        return {"kind": "fixed", "date": _day_in_month(int(g["y"]), int(g["m"]), int(g["d"])), "year": True}
    if name in ("slash", "dots"):
        a, b = int(g["a"]), int(g["b"])
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            month, day = a, b
        elif a == b and a <= 12:
            day, month = a, b
        else:
            raise ParseError(f"I cannot tell if {a}/{b} is day/month or month/day. Write the month as a word, like '{a} dec'.")
        year = g.get("y")
        if year:
            year = int(year) + (2000 if len(year) == 2 else 0)
            return {"kind": "fixed", "date": _day_in_month(year, month, day), "year": True}
        return {"kind": "monthday", "month": month, "day": day}
    if name in ("day_month", "month_day"):
        month, day = MONTH_INDEX[g["mon"].lower().rstrip(".")], int(g["day"])
        if g.get("year"):
            return {"kind": "fixed", "date": _day_in_month(int(g["year"]), month, day), "year": True}
        if day > calendar.monthrange(2024, month)[1]:   # 2024 is a leap year: only 29 Feb is allowed to be unusual
            raise ParseError(f"{MONTH_NAMES[month - 1]} does not have a day {day}.")
        return {"kind": "monthday", "month": month, "day": day}
    if name == "dom":
        day = int(g["day"])
        if not 1 <= day <= 31:
            raise ParseError("That is not a day of the month.")
        return {"kind": "dom", "day": day}
    if name == "dayafter":
        return {"kind": "offset", "days": 2}
    if name == "tomorrow":
        return {"kind": "offset", "days": 1}
    if name == "today":
        return {"kind": "offset", "days": 0}
    pre = (g.get("pre") or "").lower()
    return {"kind": "weekday", "wd": WD_INDEX[g["wd"].lower()], "next": pre == "next"}


def _rule_hits(w: _Work, rules, builder, what: str) -> list[dict]:
    hits = []
    for name, rx in rules:
        while True:
            m = w.take(rx)
            if not m:
                break
            hits.append(builder(name, m))
    if len(hits) > 1:
        raise ParseError(f"I found more than one {what} in that. Please give just one.")
    return hits


def _resolve_date(rec: dict, hm: tuple[int, int], now: datetime) -> date:
    """The calendar day for a date phrase, such that day + hm is in the future (when the phrase allows)."""
    today = now.date()
    at = dtime(*hm)
    kind = rec["kind"]
    if kind == "offset":
        return today + timedelta(days=rec["days"])
    if kind == "fixed":
        return rec["date"]
    if kind == "weekday":
        delta = (rec["wd"] - today.weekday()) % 7
        if rec["next"]:
            delta = delta or 7
            if (today + timedelta(days=delta)).isocalendar()[:2] == today.isocalendar()[:2]:
                delta += 7
        elif delta == 0 and datetime.combine(today, at) <= now.replace(tzinfo=None):
            delta = 7
        return today + timedelta(days=delta)
    if kind == "dom":
        for months in range(0, 14):
            first = add_months(today.replace(day=1), months)
            if rec["day"] <= calendar.monthrange(first.year, first.month)[1]:
                candidate = first.replace(day=rec["day"])
                if datetime.combine(candidate, at) > now.replace(tzinfo=None):
                    return candidate
        raise ParseError("I could not find that day of the month.")
    if kind == "monthday":
        for year in range(today.year, today.year + 9):
            if rec["day"] <= calendar.monthrange(year, rec["month"])[1]:
                candidate = date(year, rec["month"], rec["day"])
                if datetime.combine(candidate, at) > now.replace(tzinfo=None):
                    return candidate
    raise ParseError("I could not work out that date.")


def parse_quick(text: str, now: datetime, tz: tzinfo, call: bool = False) -> Parsed:
    """Turn a phrase like 'remind me to call mom tomorrow at 9am' into a reminder (or a timer)."""
    now = now.astimezone(tz).replace(microsecond=0)
    s = _normalize(text)
    if not s:
        raise ParseError("Type what you want to be reminded of, for example: remind me to call mom tomorrow at 9am.")
    call = call is True or bool(CALL_RX.search(s))
    if call:
        s = _normalize(CALL_RX.sub(" ", s))
        if not s:
            raise ParseError("What should I call you about? Try 'call me at 5pm to join the release check'.")
    if len(s) > (MAX_CALL_INPUT if call else 500):
        raise ParseError("That is too long for a reminder. Keep it to a sentence or two.")
    timer = None if call else _try_timer(s)
    if timer:
        return timer
    is_alarm = bool(re.search(r"\b(?:alarm|wake\s+me\s+up)\b", s, re.I))
    s = re.sub(r"\b(alarm)\s+for\b", r"\1 at", s, flags=re.I)
    s = re.sub(r"\bwake\s+me\s+up\s+for\b", "wake me up at", s, flags=re.I)
    s = FILLER_RX.sub("", s, count=1)
    for rx, message in VAGUE:
        m = rx.search(s)
        if m:
            raise ParseError(message.format(*(g.lower() for g in m.groups() if g)))
    w = _Work(s)
    w.s = REMIND_ANYWHERE_RX.sub(" ", w.s)

    # --- repeat ---
    repeat, n, pod, date_recs = "none", None, None, []
    for name, rx in REPEAT_RULES:
        m = w.take(rx)
        if not m:
            continue
        g = m.groupdict()
        if name == "bad_n":
            raise ParseError("I can repeat daily, on weekdays, weekly, monthly, yearly or every N minutes. "
                             "'Every other' and 'every 3 days' are not supported yet.")
        if name == "n_minutes":
            value = int(_number(g["n"]))
            n = value * (60 if g["u"].lower().startswith("h") else 1)
            repeat = "every_n_minutes"
        elif name in ("half_hour", "hour", "minute"):
            repeat, n = "every_n_minutes", {"half_hour": 30, "hour": 60, "minute": 1}[name]
        elif name == "weekdays":
            repeat = "weekdays"
        elif name in ("daily", "daily_pod"):
            repeat = "daily"
            pod = (g.get("pod") or "").lower() or None
        elif name in ("weekly_plural", "weekly_day"):
            repeat = "weekly"
            date_recs.append({"kind": "weekday", "wd": WD_INDEX[g["wd"].lower()], "next": False})
        elif name == "weekly":
            repeat = "weekly"
        elif name == "monthly_day":
            repeat = "monthly"
            date_recs.append({"kind": "dom", "day": int(g["day"])})
        elif name == "monthly":
            repeat = "monthly"
        else:   # yearly, yearly_date (the date itself is read below)
            repeat = "yearly"
        break
    else:
        if STRAY_EVERY_RX.search(w.s):
            raise ParseError("I did not understand that 'every'. Try 'every weekday 8:30', "
                             "'every Monday 9am' or 'every 30 minutes'.")
    if repeat != "none" and STRAY_EVERY_RX.search(w.s):
        raise ParseError("Please give just one repeat, like 'every weekday' or 'every 30 minutes'.")

    # --- delay ("in 10 minutes") ---
    rel = None
    m = w.take(REL_RX)
    if m:
        rel = _duration(m.group("d1") or m.group("d2"))
        if repeat != "none":
            raise ParseError("Say when it starts as a day and time, not as 'in ...', when it repeats.")

    # --- part of day, then date ---
    for name, rx in POD_RULES:
        m = w.take(rx)
        if m:
            g = m.groupdict()
            if name == "daypod":
                pod = g["pod"].lower()
                day = g["day"].lower()
                date_recs.append({"kind": "offset", "days": 1 if day == "tomorrow" else 0})
            elif name == "tonight":
                pod = "night"
                date_recs.append({"kind": "offset", "days": 0})
            else:
                pod = g["pod"].lower()
            break
    date_recs += _rule_hits(w, DATE_RULES, lambda name, m: _date_record(name, m, now), "date")
    if len(date_recs) > 1:
        raise ParseError("I found more than one day in that. Please give just one.")
    date_rec = date_recs[0] if date_recs else None
    time_recs = _rule_hits(w, TIME_RULES, _time_record, "time")
    time_rec = time_recs[0] if time_recs else None

    task = _tidy(w.s)
    if not task:
        if is_alarm:
            task = "Alarm"
        else:
            raise ParseError("What should I remind you about? Try 'remind me to call mom tomorrow at 9am'.")
    limit = MAX_CALL_TEXT if call else MAX_TEXT
    if len(task) > limit:
        raise ParseError(f"That is too long for a reminder. Keep it under {limit} characters.")

    due = _resolve(repeat, n, rel, date_rec, time_rec, pod, now, tz, w.s, is_alarm)
    anchor = due.astimezone(tz).replace(tzinfo=None)
    where = _echo_when(due, repeat, n, anchor, now, tz)
    return Parsed("reminder", task, f"{where} — {task}" + (" (by call)" if call else ""), due=due, repeat=repeat, n=n,
                  call=call)


def _echo_when(due: datetime, repeat: str, n: int | None, anchor: datetime, now: datetime, tz: tzinfo) -> str:
    if repeat == "none":
        return describe_when(due, now, tz)
    if repeat == "every_n_minutes":
        return describe_repeat(repeat, n, anchor)
    return f"{describe_repeat(repeat, n, anchor)} (next: {describe_when(due, now, tz)})"


def _resolve(repeat, n, rel, date_rec, time_rec, pod, now, tz, leftover, alarm=False) -> datetime:
    if repeat == "every_n_minutes":
        if rel or date_rec or time_rec or pod:
            raise ParseError("Use either 'every 30 minutes' or a time of day, not both.")
        if not 1 <= n <= 10080:
            raise ParseError("Repeats can be every 1 minute up to every week.")
        return now + timedelta(minutes=n)
    bias = ("am" if alarm else None) if pod is None else ("am" if pod == "morning" else "pm")   # alarms wake you up
    if rel:
        months, days, secs = rel
        if date_rec:
            raise ParseError("Please give either a delay like 'in 2 hours' or a day and time, not both.")
        if time_rec:
            if secs:
                raise ParseError("Please give either a delay like 'in 2 hours' or a time of day, not both.")
            options, ambiguous = _hm_options(time_rec, bias)
            hm = options[0] if not ambiguous else (options[0] if 7 <= options[0][0] <= 11 else options[-1])
            day = add_months(now.date(), months) + timedelta(days=days)
            return localize(datetime.combine(day, dtime(*hm)), tz)
        if months or days:
            day = add_months(now.date(), months) + timedelta(days=days)
            return localize(datetime.combine(day, now.time().replace(microsecond=0)), tz) + timedelta(seconds=secs)
        if secs < 60:
            raise ParseError("That is under a minute. Use a timer for short countdowns, like 'timer 30 seconds'.")
        return now + timedelta(seconds=secs)
    if not (date_rec or time_rec or pod or repeat != "none"):
        if re.search(r"\blater\b", leftover, re.I):
            raise ParseError("I need an actual time instead of 'later', like 'in 2 hours' or 'tomorrow at 9am'.")
        raise ParseError(WHEN_HELP)
    kind = date_rec["kind"] if date_rec else None
    if kind == "offset" and date_rec["days"] == 0 and not time_rec and not pod:
        raise ParseError("What time today? Try 'today at 5pm' or 'in 2 hours'.")
    soonest = repeat == "none" and (date_rec is None or (kind == "offset" and date_rec["days"] == 0))
    if time_rec:
        options, ambiguous = _hm_options(time_rec, bias)
    elif pod:
        options, ambiguous = [(POD_HOURS[pod], 0)], False
    else:
        options, ambiguous = [(9, 0)], False
    if soonest:
        days = [0, 1] if date_rec is None else [0]
        candidates = sorted(localize(datetime.combine(now.date() + timedelta(days=d), dtime(*hm)), tz)
                            for d in days for hm in options)
        for candidate in candidates:
            if candidate > now:
                return candidate
        if date_rec is not None:
            raise ParseError("That time has already passed today. Try a later time, or say 'tomorrow'.")
        raise ParseError(WHEN_HELP)
    hm = options[0]
    if ambiguous:
        hm = options[0] if 7 <= options[0][0] <= 11 else options[1]    # 7-11 mean morning, 1-6 afternoon
    if repeat == "none" or date_rec is not None:
        if date_rec is None:
            day = now.date()
        else:
            if repeat == "monthly" and kind not in ("dom", "fixed", "monthday", "offset"):
                raise ParseError("Monthly reminders need a day of the month, like 'the 15th'.")
            day = _resolve_date(date_rec, hm, now)
        due = localize(datetime.combine(day, dtime(*hm)), tz)
        if date_rec is not None and due <= now:
            raise ParseError("That time has already passed. Try a later time or a later date.")
        if repeat == "weekdays" and due.weekday() >= 5:
            due = next_occurrence(due.replace(tzinfo=None), "weekdays", None, due, tz)
        return due
    # repeating, no date: the first time after now
    if repeat == "weekly":
        raise ParseError("Which day of the week? Try 'every Monday 9am'.")
    if repeat == "monthly":
        raise ParseError("Which day of the month? Try 'monthly on the 15th'.")
    if repeat == "yearly":
        raise ParseError("Which date each year? Try 'every 25 dec' or 'yearly on 3 march'.")
    anchor = datetime.combine(now.date(), dtime(*hm))
    return next_occurrence(anchor, repeat, None, now, tz)


# ---- the store and the scheduler -----------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _check_id(value: str) -> str:
    if not ID_RE.match(value or ""):
        raise HTTPException(404, "That item does not exist.")
    return value


class RemindersService:
    def __init__(self, ctx: Ctx, path: Path, *, clock: Callable[[], datetime] | None = None, tz: tzinfo | None = None):
        self.ctx = ctx
        self.file = JsonFile(path)
        self.tz = tz or LocalTZ()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.data = self._load()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poke: asyncio.Event | None = None

    # -- storage --
    def _load(self) -> dict:
        raw = self.file.load({})
        raw = raw if isinstance(raw, dict) else {}
        data = {"reminders": [], "timers": [], "seeded": bool(raw.get("seeded"))}
        for key, need in (("reminders", ("id", "text", "due", "repeat")), ("timers", ("id", "label", "ends_at", "seconds"))):
            for item in raw.get(key) or []:
                if isinstance(item, dict) and all(k in item for k in need) and ID_RE.match(str(item["id"])):
                    data[key].append(item)
        return data

    def save(self) -> None:
        try:
            self.file.save(self.data)
        except OSError as exc:
            log.warning("Could not save reminders (%s).", type(exc).__name__)

    def now(self) -> datetime:
        return self._clock().astimezone(self.tz)

    def wake(self) -> None:
        if self._loop is not None and self._poke is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._poke.set)

    # -- helpers --
    def _dt(self, value: Any) -> datetime | None:
        try:
            return parse_iso(value, self.tz)
        except (ValueError, TypeError):
            return None

    def trigger(self, r: dict) -> datetime | None:
        return self._dt(r.get("snoozed_until") or r.get("due"))

    def _find(self, rid: str) -> dict:
        _check_id(rid)
        for r in self.data["reminders"]:
            if r["id"] == rid:
                return r
        raise HTTPException(404, "That reminder does not exist.")

    def status(self, r: dict, now: datetime) -> str:
        if r.get("done"):
            return "done"
        trig = self.trigger(r)
        if r["repeat"] == "none" and trig is not None and r.get("notified") == iso(trig, self.tz):
            return "fired"
        if r.get("snoozed_until") and trig is not None and trig > now:
            return "snoozed"
        return "upcoming"

    def public(self, r: dict, now: datetime | None = None) -> dict:
        now = now or self.now()
        trig = self.trigger(r)
        due = self._dt(r.get("due"))
        out = {k: r.get(k) for k in ("id", "text", "due", "repeat", "n", "done", "snoozed_until", "created", "source")}
        out["call"] = r.get("call") is True
        out.update(done_at=r.get("done_at"), last_fired=r.get("last_fired"), status=self.status(r, now),
                   next_fire=iso(trig, self.tz) if trig else None,
                   when=describe_when(trig or due, now, self.tz) if (trig or due) else "")
        if r["repeat"] != "none" and due is not None:
            out["repeat_text"] = describe_repeat(r["repeat"], r.get("n"), self._anchor(r, due))
        return out

    def _anchor(self, r: dict, due: datetime) -> datetime:
        try:
            return datetime.fromisoformat(r["anchor"])
        except (KeyError, ValueError, TypeError):
            return due.astimezone(self.tz).replace(tzinfo=None)

    def public_timer(self, t: dict, now: datetime | None = None) -> dict:
        now = now or self.now()
        ends = self._dt(t["ends_at"]) or now
        remaining = max(0, int((ends - now).total_seconds() + 0.999))
        return {"id": t["id"], "label": t["label"], "seconds": t["seconds"], "created": t.get("created"),
                "ends_at": t["ends_at"], "state": "finished" if t.get("fired") else "running",
                "remaining_seconds": 0 if t.get("fired") else remaining}

    # -- reminders --
    def _schedule(self, r: dict, due: datetime, repeat: str, n: int | None, now: datetime, check_past: bool) -> None:
        if repeat not in REPEATS:
            raise HTTPException(400, "The repeat must be none, daily, weekdays, weekly, monthly, yearly or every_n_minutes.")
        if repeat == "every_n_minutes":
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 10080:
                raise HTTPException(400, "Say how many minutes between repeats (1 to 10080).")
        else:
            n = None
        anchor = due.astimezone(self.tz).replace(tzinfo=None)
        if repeat == "weekdays" and anchor.weekday() >= 5:
            due = next_occurrence(anchor, "weekdays", None, due, self.tz)
            anchor = due.replace(tzinfo=None)
        if due < now - timedelta(seconds=60):
            if repeat == "none":
                if check_past:
                    raise HTTPException(400, "That time has already passed. Pick a time in the future.")
            else:
                due = next_occurrence(anchor, repeat, n, now, self.tz, due=due)
        r.update(due=iso(due, self.tz), repeat=repeat, n=n, anchor=anchor.isoformat(timespec="minutes"),
                 snoozed_until=None, notified=None, fired_pending=False)

    def create(self, text: Any, due: Any, repeat: str = "none", n: Any = None, source: str = "app",
               call: Any = False) -> dict:
        call = clean_call(call)
        text = clean_text(text, limit=MAX_CALL_TEXT if call else MAX_TEXT)
        now = self.now()
        if isinstance(due, datetime):
            due_dt = due.astimezone(self.tz)
        else:
            try:
                due_dt = parse_iso(due, self.tz)
            except (ValueError, TypeError):
                raise HTTPException(400, "Give the time as a date and time, like 2026-12-25T09:00:00.") from None
        if len(self.data["reminders"]) >= MAX_REMINDERS:
            raise HTTPException(400, "You have too many reminders. Delete some old ones first.")
        r = {"id": _new_id(), "text": text, "done": False, "done_at": None, "created": iso(now, self.tz),
             "source": " ".join(str(source or "app").split())[:40] or "app", "call": call}
        self._schedule(r, due_dt, repeat, n, now, check_past=True)
        self.data["reminders"].append(r)
        self.save()
        self.wake()
        return r

    def update(self, rid: str, body: dict) -> dict:
        r = self._find(rid)
        now = self.now()
        if "call" in body:
            r["call"] = clean_call(body["call"])
        if "text" in body:
            r["text"] = clean_text(body["text"], limit=MAX_CALL_TEXT if r.get("call") else MAX_TEXT)
        if any(k in body for k in ("due", "repeat", "n")):
            if "due" in body:
                try:
                    due = parse_iso(body["due"], self.tz)
                except (ValueError, TypeError):
                    raise HTTPException(400, "Give the time as a date and time, like 2026-12-25T09:00:00.") from None
            else:
                due = self._dt(r["due"]) or now
            repeat = body.get("repeat", r["repeat"])
            n = body.get("n", r.get("n")) if repeat == "every_n_minutes" else None
            self._schedule(r, due, repeat, n, now, check_past="due" in body)
            r["done"], r["done_at"] = False, None
        if "done" in body:
            if body["done"] is True:
                self._mark_done(r, now)
            elif body["done"] is False:
                r["done"], r["done_at"] = False, None
            else:
                raise HTTPException(400, "done must be true or false.")
        self.save()
        self.wake()
        return r

    def delete(self, rid: str) -> None:
        r = self._find(rid)
        self.data["reminders"].remove(r)
        self.save()
        self.ctx.audit("reminder_deleted", id=rid)

    def snooze(self, rid: str, minutes: Any) -> dict:
        r = self._find(rid)
        if minutes is None:
            minutes = 10
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 10080:
            raise HTTPException(400, "Snooze for a whole number of minutes between 1 and 10080.")
        now = self.now()
        r["snoozed_until"] = iso(now + timedelta(minutes=minutes), self.tz)
        r["done"], r["done_at"] = False, None
        self.save()
        self.wake()
        return r

    def _mark_done(self, r: dict, now: datetime) -> None:
        if r["repeat"] == "none":
            r["done"], r["done_at"], r["snoozed_until"] = True, iso(now, self.tz), None
            return
        if r.get("fired_pending"):          # this occurrence already fired and moved on: just acknowledge it
            r["fired_pending"] = False
        else:                               # skip the coming occurrence
            due = self._dt(r["due"]) or now
            r["due"] = iso(next_occurrence(self._anchor(r, due), r["repeat"], r.get("n"), max(due, now), self.tz, due=due), self.tz)
        r["snoozed_until"] = None

    def done(self, rid: str) -> dict:
        r = self._find(rid)
        self._mark_done(r, self.now())
        self.save()
        self.wake()
        return r

    def list(self, status: str = "open") -> list[dict]:
        now = self.now()
        items = self.data["reminders"]
        if status == "open":
            items = [r for r in items if not r.get("done")]
        elif status == "done":
            items = [r for r in items if r.get("done")]
        far = datetime.max.replace(tzinfo=timezone.utc, year=2200)
        items = sorted(items, key=lambda r: (bool(r.get("done")), self.trigger(r) or far))
        return [self.public(r, now) for r in items]

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if not q:
            return []
        now = self.now()
        hits = [r for r in self.data["reminders"] if q in r["text"].lower()]
        hits.sort(key=lambda r: (bool(r.get("done")), r.get("due", "")))
        return [{"title": r["text"], "snippet": ("Done" if r.get("done") else self.public(r, now)["when"]),
                 "kind": "reminder", "open": {"view": "reminders", "section": "", "id": r["id"]}} for r in hits[:20]]

    # -- timers --
    def create_timer(self, seconds: Any, label: Any = "") -> dict:
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 1 <= seconds <= MAX_TIMER_SECONDS:
            raise HTTPException(400, "A timer can run from 1 second up to 7 days.")
        label = clean_text(label, "timer")[:80] if isinstance(label, str) and label.strip() else "Timer"
        running = [t for t in self.data["timers"] if not t.get("fired")]
        if len(running) >= MAX_TIMERS:
            raise HTTPException(400, "You have too many timers running. Cancel some first.")
        now = self.now()
        t = {"id": _new_id(), "label": label, "seconds": int(round(seconds)), "created": iso(now, self.tz),
             "ends_at": iso(now + timedelta(seconds=seconds), self.tz), "fired": False}
        self.data["timers"].append(t)
        self.save()
        self.wake()
        return t

    def delete_timer(self, tid: str) -> None:
        _check_id(tid)
        for t in self.data["timers"]:
            if t["id"] == tid:
                self.data["timers"].remove(t)
                self.save()
                self.wake()
                return
        raise HTTPException(404, "That timer does not exist.")

    def list_timers(self) -> list[dict]:
        now = self.now()
        timers = sorted(self.data["timers"], key=lambda t: (bool(t.get("fired")), t["ends_at"]))
        return [self.public_timer(t, now) for t in timers]

    # -- the clock tick --
    def tick(self, now: datetime | None = None) -> list[dict]:
        """Fire everything that is due. Returns what was announced (for tests and logs)."""
        now = (now or self.now()).astimezone(self.tz)
        due_items: list[tuple[str, dict, datetime, bool]] = []
        for r in self.data["reminders"]:
            if r.get("done"):
                continue
            trig = self.trigger(r)
            if trig is None or trig > now:
                continue
            if r["repeat"] == "none" and r.get("notified") == iso(trig, self.tz):
                continue
            due_items.append(("reminder", r, trig, (now - trig).total_seconds() > MISSED_AFTER))
        for t in self.data["timers"]:
            ends = self._dt(t["ends_at"])
            if not t.get("fired") and ends is not None and ends <= now:
                due_items.append(("timer", t, ends, (now - ends).total_seconds() > MISSED_AFTER))
        if not due_items:
            self._prune(now)
            return []
        # State first (so a crash can never make it fire twice), then the notifications.
        for kind, item, trig, _late in due_items:
            if kind == "timer":
                item["fired"], item["fired_at"] = True, iso(now, self.tz)
                continue
            item["last_fired"] = iso(now, self.tz)
            item["notified"] = iso(trig, self.tz)
            if item["repeat"] != "none":
                due = self._dt(item["due"]) or now
                if due <= now:
                    item["due"] = iso(next_occurrence(self._anchor(item, due), item["repeat"], item.get("n"),
                                                      now, self.tz, due=due), self.tz)
                item["snoozed_until"] = None
                item["fired_pending"] = True
        self._prune(now)
        self.save()
        on_time = [d for d in due_items if not d[3]]
        late = sorted((d for d in due_items if d[3]), key=lambda d: d[2], reverse=True)
        sent = []
        for kind, item, trig, is_late in on_time + late[:MISSED_CAP]:
            sent.append(self._announce(kind, item, trig, is_late, now))
        if len(late) > MISSED_CAP:
            extra = len(late) - MISSED_CAP
            self._notify("More missed reminders", f"{extra} more reminders or timers came due while the hub was "
                         "not running. Open Reminders to see them.")
            sent.append({"kind": "summary", "count": extra})
        return sent

    def _notify(self, title: str, body: str) -> None:
        try:
            self.ctx.notify(title, body, level="info", link="reminders", source="reminders")
        except Exception as exc:  # noqa: BLE001 - a broken notifier must not stop the clock
            log.warning("Could not send a reminder notification (%s).", type(exc).__name__)

    def _announce(self, kind: str, item: dict, trig: datetime, late: bool, now: datetime) -> dict:
        when = describe_when(trig, now, self.tz)
        if kind == "reminder":
            text = item["text"]
            title = f"{'Missed reminder' if late else 'Reminder'}: {text}"[:200]
            self._notify(title, f"It was due {when}." if late else f"Due {when}.")
            if item.get("call") is True:
                from . import alerts as alerts_mod      # rings the phone: urgency 3, full text, not held by quiet hours
                alerts_mod.push_phone(self.ctx, f"rem:{item['id']}:{item.get('notified') or trig.isoformat()}",
                                      "Reminder: " + text, title="Call reminder", kind="reminder", source="reminder",
                                      urgency=3, preview=text, call=True)
        else:
            label = item["label"]
            length = describe_seconds(item["seconds"])
            title = f"{'Missed timer' if late else 'Timer done'}: {label}"[:200]
            self._notify(title, f"Your {length} timer finished {when}." if late else f"Your {length} timer has finished.")
        return {"kind": kind, "id": item["id"], "missed": late}

    def _prune(self, now: datetime) -> None:
        changed = False
        keep_done = now - timedelta(days=KEEP_DONE_DAYS)
        before = len(self.data["reminders"])
        self.data["reminders"] = [r for r in self.data["reminders"]
                                  if not (r.get("done") and (self._dt(r.get("done_at")) or now) < keep_done)]
        changed |= len(self.data["reminders"]) != before
        keep_timer = now - timedelta(hours=KEEP_TIMER_HOURS)
        before = len(self.data["timers"])
        self.data["timers"] = [t for t in self.data["timers"]
                               if not (t.get("fired") and (self._dt(t.get("fired_at")) or now) < keep_timer)]
        changed |= len(self.data["timers"]) != before
        if changed:
            self.save()

    def seconds_until_next(self, cap: float = TICK_SECONDS) -> float:
        now = self.now()
        wait = cap
        for r in self.data["reminders"]:
            trig = None if r.get("done") else self.trigger(r)
            if trig is not None and not (r["repeat"] == "none" and r.get("notified") == iso(trig, self.tz)):
                wait = min(wait, (trig - now).total_seconds())
        for t in self.data["timers"]:
            ends = None if t.get("fired") else self._dt(t["ends_at"])
            if ends is not None:
                wait = min(wait, (ends - now).total_seconds())
        return max(0.25, wait)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._poke = asyncio.Event()
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("The reminder clock hit a problem; it will try again.")
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.seconds_until_next())
            except asyncio.TimeoutError:
                pass
            self._poke.clear()

    def seed_demo(self) -> None:
        if self.data["seeded"] or self.data["reminders"]:
            return
        now = self.now()
        tomorrow = now.date() + timedelta(days=1)
        self.create("Call mom", localize(datetime.combine(tomorrow, dtime(9, 0)), self.tz), source="demo")
        standup = next_occurrence(datetime.combine(now.date(), dtime(8, 30)), "weekdays", None, now, self.tz)
        self.create("Standup", standup, "weekdays", source="demo")
        friday = _resolve_date({"kind": "weekday", "wd": 4, "next": False}, (17, 0), now)
        self.create("Send report", localize(datetime.combine(friday, dtime(17, 0)), self.tz), source="demo")
        self.data["seeded"] = True
        self.save()


# ---- routes ------------------------------------------------------------------------------------------

def _service(ctx: Ctx, **kwargs) -> RemindersService:
    service = getattr(ctx, "_reminders_service", None)
    if service is None:
        service = RemindersService(ctx, ctx.config.data_path() / "reminders.json", **kwargs)
        ctx._reminders_service = service   # type: ignore[attr-defined]  # start() finds the same one
    return service


def make_router(ctx: Ctx, *, clock: Callable[[], datetime] | None = None, tz: tzinfo | None = None) -> APIRouter:
    """`clock` (returns an aware datetime) and `tz` exist so tests can use a fake clock."""
    service = _service(ctx, clock=clock, tz=tz)
    router = APIRouter(prefix="/api/reminders", dependencies=[Depends(ctx.need_auth)])
    router.service = service   # type: ignore[attr-defined]

    @router.get("")
    @router.get("/")
    async def list_reminders(status: str = "open"):
        if status not in ("open", "done", "all"):
            raise HTTPException(400, "status must be open, done or all.")
        return {"reminders": service.list(status)}

    @router.post("")
    @router.post("/")
    async def create(body: dict = Body(default_factory=dict)):
        r = service.create(body.get("text"), body.get("due"), body.get("repeat") or "none", body.get("n"),
                           body.get("source") or "app", body.get("call"))
        return service.public(r)

    @router.post("/quick")
    async def quick(body: dict = Body(default_factory=dict)):
        """Parse a phrase and (unless preview is true) save it. Returns the item plus a one-line echo."""
        preview = body.get("preview") is True
        try:
            parsed = parse_quick(body.get("text"), service.now(), service.tz, clean_call(body.get("call")))
        except ParseError as exc:
            raise HTTPException(400, str(exc)) from None
        if parsed.kind == "timer":
            if preview:
                return {"kind": "timer", "saved": False, "echo": parsed.echo, "timer": None,
                        "seconds": parsed.seconds, "label": parsed.text}
            timer = service.create_timer(parsed.seconds, parsed.text)
            return {"kind": "timer", "saved": True, "echo": parsed.echo, "timer": service.public_timer(timer)}
        if preview:
            return {"kind": "reminder", "saved": False, "echo": parsed.echo,
                    "reminder": {"text": parsed.text, "due": iso(parsed.due, service.tz), "repeat": parsed.repeat,
                                 "n": parsed.n, "call": parsed.call}}
        r = service.create(parsed.text, parsed.due, parsed.repeat, parsed.n, "quick", parsed.call)
        return {"kind": "reminder", "saved": True, "echo": parsed.echo, "reminder": service.public(r)}

    @router.get("/search")
    async def search(q: str = ""):
        return {"results": service.search(q[:100])}

    @router.get("/timers")
    async def timers():
        return {"timers": service.list_timers()}

    @router.post("/timers")
    async def add_timer(body: dict = Body(default_factory=dict)):
        return service.public_timer(service.create_timer(body.get("seconds"), body.get("label") or ""))

    @router.delete("/timers/{timer_id}")
    async def cancel_timer(timer_id: str):
        service.delete_timer(timer_id)
        return {"ok": True}

    @router.get("/{reminder_id}")
    async def get_one(reminder_id: str):
        return service.public(service._find(reminder_id))

    @router.patch("/{reminder_id}")
    async def patch(reminder_id: str, body: dict = Body(default_factory=dict)):
        return service.public(service.update(reminder_id, body))

    @router.delete("/{reminder_id}")
    async def delete(reminder_id: str):
        service.delete(reminder_id)
        return {"ok": True}

    @router.post("/{reminder_id}/snooze")
    async def snooze(reminder_id: str, body: dict = Body(default_factory=dict)):
        return service.public(service.snooze(reminder_id, body.get("minutes")))

    @router.post("/{reminder_id}/done")
    async def done(reminder_id: str):
        return service.public(service.done(reminder_id))

    return router


async def start(ctx: Ctx):
    """Runs the reminder clock. Returns the function that stops it."""
    service = _service(ctx)
    if os.environ.get("HUB_DEMO"):
        service.seed_demo()
    task = asyncio.create_task(service.run(), name="hub-reminders")

    async def stop():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return stop
