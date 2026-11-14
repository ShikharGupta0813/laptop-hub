"""CALENDAR FEEDS: a LIST of calendars the owner can add to, colour, switch off or delete (Settings > Alerts, "Calendars").

Items live in `calendars.json` (data folder): {id, kind, label, color, enabled}. Kinds:
- "google": the ready-made entry for the Google calendar the Jarvis setup already reads (hub/work). It can be switched off.
- "ics": any iCal feed address (Google "secret address in iCal format", Outlook "published calendar", Apple, a company
  calendar server...). The address can contain a secret, so it is kept ONLY in the OS keyring (service `redline-calendars`,
  key `cal:<id>`) and a view shows just the host. `webcal://` is read as https://.

Reading is a small stdlib parser (no dependency): VEVENT with DTSTART / DTEND / DURATION / SUMMARY / LOCATION / URL /
DESCRIPTION, line folding, UTC (`Z`), floating and `TZID=` times (IANA names through zoneinfo; a few common Windows names are
mapped; anything unknown is read as the laptop's local time), all-day dates, EXDATE, RECURRENCE-ID overrides, STATUS:CANCELLED
and RRULE with FREQ DAILY / WEEKLY / MONTHLY / YEARLY, INTERVAL, COUNT, UNTIL, BYDAY (weekly; monthly with 1MO or -1FR),
BYMONTHDAY. Not handled (ignored, never an error): BYSETPOS, BYWEEKNO, BYYEARDAY, RDATE, VTIMEZONE rules, alarms, attendees.

`hub/work/__init__.py` calls `meetings_between` so `/api/work/calendar` (which Today, Friday and the 10-minute meeting
reminder all read) returns Google and every enabled feed together; each meeting carries `feed` (label) and `color`.
This module also serves `GET /api/calendars/agenda` (feeds only) for the Settings screen.

Fetching is cached for 5 minutes, capped at 5 MB, never follows a redirect that would drop to http, and fails per feed (a
broken feed is a `last_error` on that feed, never an error for the others).
"""
from __future__ import annotations

import calendar as pycal
import datetime as dt
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .common import Ctx, JsonFile

log = logging.getLogger("hub.calendars")

SECRET_SERVICE = "redline-calendars"
MAX_FEEDS = 20
MAX_BYTES = 5_000_000
CACHE_SECONDS = 300
MAX_ITER = 4000
LABEL_MAX = 40
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
URL_RE = re.compile(r"^(?:https?|webcal)://[^\s<>\"']{3,1500}$")
DEFAULT_COLORS = ["#4285f4", "#0b8043", "#e67c73", "#f6bf26", "#8e24aa", "#039be5", "#616161", "#d50000"]
MEET_RE = re.compile(r"https://[^\s<>\"')]*(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|teams\.live\.com|webex\.com)[^\s<>\"')]*", re.I)
HTTPS_RE = re.compile(r"https://[^\s<>\"')]+", re.I)
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WINDOWS_TZ = {
    "UTC": "UTC", "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest", "Romance Standard Time": "Europe/Paris",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney", "Arabian Standard Time": "Asia/Dubai", "Singapore Standard Time": "Asia/Singapore",
}

Fetch = Callable[[str], "tuple[int, bytes]"]


class CalendarProblem(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def default_fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "redline-hub", "Accept": "text/calendar, */*"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:        # noqa: S310 - http(s) only, checked by the caller
            return response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


# ---- the ICS parser ---------------------------------------------------------------------------------------

def unfold(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def split_prop(line: str) -> tuple[str, dict, str]:
    """`DTSTART;TZID=Europe/Berlin:20261110T090000` -> ("DTSTART", {"TZID": "Europe/Berlin"}, "20261110T090000")."""
    head, _, value = line.partition(":") if '"' not in line.split(":", 1)[0] else _quoted_split(line)
    name, *params = head.split(";")
    pm = {}
    for p in params:
        k, _, v = p.partition("=")
        pm[k.upper()] = v.strip('"')
    return name.upper(), pm, value


def _quoted_split(line: str) -> tuple[str, str, str]:
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            return line[:i], ":", line[i + 1:]
    return line, "", ""


def unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def get_tz(name: str, local: dt.tzinfo | None):
    name = (name or "").strip()
    if not name:
        return local
    name = WINDOWS_TZ.get(name, name)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name, or no tz database installed: read it as local time
        return dt.timezone.utc if name.upper() in ("UTC", "Z", "GMT") else local


def parse_when(value: str, params: dict, local: dt.tzinfo | None):
    """(value, is_all_day, tzinfo-or-None). Timed values are naive wall times with the tz kept aside, or aware UTC."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8])), True, None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?", value)
    if not m:
        raise ValueError("bad date")
    y, mo, d, h, mi, s, z = m.groups()
    naive = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    if z:
        return naive.replace(tzinfo=dt.timezone.utc), False, dt.timezone.utc
    tz = get_tz(params.get("TZID", ""), local)
    return naive, False, tz


def parse_duration(value: str) -> dt.timedelta | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return None
    sign, w, d, h, mi, s = m.groups()
    td = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def parse_ics(text: str, local: dt.tzinfo | None = None) -> list[dict]:
    """The VEVENTs of an iCalendar document as plain dicts. Never raises on broken input: bad events are skipped."""
    events: list[dict] = []
    cur: dict | None = None
    for line in unfold(text):
        up = line.upper()
        if up == "BEGIN:VEVENT":
            cur = {"exdates": []}
            continue
        if up == "END:VEVENT":
            if cur is not None and cur.get("start") is not None:
                try:
                    _finish(cur)
                    events.append(cur)
                except (ValueError, OverflowError):
                    pass
            cur = None
            continue
        if cur is None:
            continue
        try:
            name, params, value = split_prop(line)
            if name == "DTSTART":
                cur["start"], cur["all_day"], cur["tz"] = parse_when(value, params, local)
            elif name == "DTEND":
                cur["end"] = parse_when(value, params, local)[0]
            elif name == "DURATION":
                cur["duration"] = parse_duration(value)
            elif name == "SUMMARY":
                cur["summary"] = unescape(value)
            elif name == "LOCATION":
                cur["location"] = unescape(value)
            elif name == "DESCRIPTION":
                cur["description"] = unescape(value)
            elif name == "URL":
                cur["url"] = value.strip()
            elif name == "UID":
                cur["uid"] = value.strip()
            elif name == "STATUS":
                cur["status"] = value.strip().upper()
            elif name == "RRULE":
                cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].append(parse_when(v, params, local)[0])
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"] = parse_when(value, params, local)[0]
        except (ValueError, OverflowError):
            continue
    return events


def _finish(ev: dict) -> None:
    start = ev["start"]
    if ev.get("end") is None:
        if ev.get("duration") is not None:
            ev["end"] = start + ev["duration"]
        elif ev["all_day"]:
            ev["end"] = start + dt.timedelta(days=1)
        else:
            ev["end"] = start
    ev["length"] = ev["end"] - start if type(ev["end"]) is type(start) else dt.timedelta(0)


# ---- recurrence --------------------------------------------------------------------------------------------

def _ordinal_day(year: int, month: int, spec: str) -> dt.date | None:
    m = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", spec)
    if not m:
        return None
    n, wd = int(m.group(1)) if m.group(1) else 0, WEEKDAYS.index(m.group(2))
    days = [dt.date(year, month, d) for d in range(1, pycal.monthrange(year, month)[1] + 1)]
    cands = [d for d in days if d.weekday() == wd]
    if not n:
        return cands[0] if cands else None
    try:
        return cands[n - 1] if n > 0 else cands[n]
    except IndexError:
        return None


def _candidates(base: dt.datetime, rule: dict):
    """Naive wall-time starts of a basic RRULE, in order, forever (the caller stops it)."""
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL") or 1))
    byday = [x for x in (rule.get("BYDAY") or "").upper().split(",") if x]
    bymd = [int(x) for x in (rule.get("BYMONTHDAY") or "").split(",") if re.fullmatch(r"-?\d{1,2}", x)]
    t = base.time()
    if freq == "DAILY":
        d = base
        while True:
            yield d
            d += dt.timedelta(days=interval)
    elif freq == "WEEKLY":
        days = sorted({WEEKDAYS.index(x[-2:]) for x in byday if x[-2:] in WEEKDAYS}) or [base.weekday()]
        monday = base.date() - dt.timedelta(days=base.weekday())
        while True:
            for wd in days:
                cand = dt.datetime.combine(monday + dt.timedelta(days=wd), t)
                if cand >= base:
                    yield cand
            monday += dt.timedelta(weeks=interval)
    elif freq == "MONTHLY":
        y, mo = base.year, base.month
        while True:
            picks: list[dt.date] = []
            if byday:
                picks = [d for d in (_ordinal_day(y, mo, s) for s in byday) if d]
            else:
                last = pycal.monthrange(y, mo)[1]
                for n in bymd or [base.day]:
                    day = n if n > 0 else last + 1 + n
                    if 1 <= day <= last:
                        picks.append(dt.date(y, mo, day))
            for d in sorted(picks):
                cand = dt.datetime.combine(d, t)
                if cand >= base:
                    yield cand
            mo += interval
            y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
    elif freq == "YEARLY":
        y = base.year
        while True:
            try:
                cand = base.replace(year=y)
                if cand >= base:
                    yield cand
            except ValueError:
                pass
            y += interval
    else:
        yield base


def occurrences(ev: dict, win_start: dt.datetime, win_end: dt.datetime, local: dt.tzinfo) -> list[tuple[dt.datetime, dt.datetime]]:
    """Aware (start, end) pairs of one event that overlap [win_start, win_end). All-day events start at local midnight."""
    start, length, all_day, tz = ev["start"], ev["length"], ev["all_day"], ev.get("tz")

    def aware(wall: dt.datetime) -> dt.datetime:
        if all_day:
            return wall.replace(tzinfo=local)
        if wall.tzinfo is not None:
            return wall.astimezone(local)
        return wall.replace(tzinfo=tz or local).astimezone(local)

    base = dt.datetime.combine(start, dt.time()) if all_day else start
    rule = ev.get("rrule")
    out: list[tuple[dt.datetime, dt.datetime]] = []
    if not rule:
        s = aware(base)
        e = s + (length if length > dt.timedelta(0) else dt.timedelta(0))
        if e > win_start and s < win_end or (s == e and win_start <= s < win_end):
            out.append((s, e))
        return out
    until = None
    if rule.get("UNTIL"):
        try:
            u, u_all, _tz = parse_when(rule["UNTIL"], {}, local)
            until = dt.datetime.combine(u, dt.time(23, 59, 59)).replace(tzinfo=local) if u_all else aware(u)
        except ValueError:
            until = None
    count = int(rule["COUNT"]) if str(rule.get("COUNT") or "").isdigit() else None
    ex = set()
    for x in ev.get("exdates") or []:
        ex.add(aware(dt.datetime.combine(x, dt.time())) if type(x) is dt.date else aware(x))
    wall_base = base.replace(tzinfo=None)        # the wall clock is what repeats; the zone is applied to each instance
    n = 0
    for i, wall in enumerate(_candidates(wall_base, rule)):
        if i >= MAX_ITER:
            break
        s = aware(wall.replace(tzinfo=tz) if (tz is not None and not all_day) else wall)
        if until is not None and s > until:
            break
        n += 1
        if count is not None and n > count:
            break
        if s >= win_end:
            break
        e = s + length
        if s in ex:
            continue
        if e > win_start or (e == s and s >= win_start):
            out.append((s, e))
    return out


def expand(events: list[dict], win_start: dt.datetime, win_end: dt.datetime, local: dt.tzinfo) -> list[dict]:
    """Meeting dicts for all instances in the window (RECURRENCE-ID overrides replace the instance they point at)."""
    overridden: set[tuple[str, dt.datetime]] = set()
    for ev in events:
        rid = ev.get("recurrence_id")
        if rid is not None and ev.get("uid"):
            r = rid if isinstance(rid, dt.datetime) else dt.datetime.combine(rid, dt.time())
            overridden.add((ev["uid"], r.astimezone(local) if r.tzinfo else r.replace(tzinfo=ev.get("tz") or local).astimezone(local)))
    out: list[dict] = []
    for ev in events:
        if ev.get("status") == "CANCELLED":
            continue
        for s, e in occurrences(ev, win_start, win_end, local):
            if ev.get("recurrence_id") is None and (ev.get("uid"), s) in overridden:
                continue
            blob = " ".join([ev.get("url", ""), ev.get("location", ""), ev.get("description", "")])
            link = (MEET_RE.search(blob) or HTTPS_RE.search(ev.get("url", "") + " " + ev.get("location", "")) or [None])
            link = link.group(0) if hasattr(link, "group") else ""
            out.append({"title": (ev.get("summary") or "(no title)")[:200], "start": s.isoformat(), "end": e.isoformat(),
                        "all_day": bool(ev["all_day"]), "link": link, "platform": _platform(link),
                        "location": (ev.get("location") or "")[:200]})
    out.sort(key=lambda m: (not m["all_day"], m["start"]))
    return out


def _platform(link: str) -> str:
    low = link.lower()
    for key, name in (("zoom.us", "zoom"), ("meet.google", "meet"), ("teams.", "teams"), ("webex", "webex")):
        if key in low:
            return name
    return ""


# ---- the list ----------------------------------------------------------------------------------------------

def make_store():
    from .gmailx import KeyringStore
    return KeyringStore(SECRET_SERVICE)


def _local_tz() -> dt.tzinfo:
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


class Calendars:
    def __init__(self, ctx: Ctx, *, store: Any = None, fetch: Fetch | None = None, clock: Callable[[], float] = time.time,
                 tz: dt.tzinfo | None = None):
        self.ctx = ctx
        self.store = store or make_store()
        self.fetch = fetch or default_fetch
        self.clock = clock
        self.tz = tz
        self.file = JsonFile(ctx.config.data_path() / "calendars.json")
        self.cache: dict[str, tuple[float, str]] = {}
        self.errors: dict[str, str] = {}

    def _load(self) -> list[dict]:
        data = self.file.load({})
        items = data.get("items") if isinstance(data, dict) else None
        items = [i for i in items if isinstance(i, dict) and i.get("kind") in ("ics", "google") and i.get("id")] if isinstance(items, list) else []
        if not any(i["id"] == "google" for i in items):
            items.insert(0, {"id": "google", "kind": "google", "label": "Google Calendar", "color": DEFAULT_COLORS[0],
                             "enabled": True, "builtin": True})
        return items

    def _save(self, items: list[dict]) -> None:
        self.file.save({"items": items})

    def _url(self, item_id: str) -> str:
        try:
            return (self.store.get("cal:" + item_id) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def view(self, item: dict) -> dict:
        out = {"id": item["id"], "kind": item["kind"], "label": item.get("label") or item["kind"], "color": item.get("color") or DEFAULT_COLORS[0],
               "enabled": item.get("enabled") is not False, "builtin": item.get("builtin") is True,
               "last_error": self.errors.get(item["id"]) or None}
        if item["kind"] == "ics":
            url = self._url(item["id"])
            host = re.sub(r"^\w+://", "", url).split("/", 1)[0] if url else ""
            out.update({"has_secret": bool(url), "address_hint": (host + "/...") if host else "", "secret_ref": f"keyring:{SECRET_SERVICE}/cal:{item['id']}"})
        return out

    def list_items(self) -> list[dict]:
        return [self.view(i) for i in self._load()]

    def _find(self, items: list[dict], item_id: str) -> dict:
        for i in items:
            if i["id"] == item_id:
                return i
        raise CalendarProblem("There is no calendar with that id.", 404)

    @staticmethod
    def _color(value: Any, default: str) -> str:
        if value is None:
            return default
        if not isinstance(value, str) or not COLOR_RE.match(value):
            raise CalendarProblem("A colour looks like #4285f4.")
        return value.lower()

    @staticmethod
    def _address(value: Any) -> str:
        v = str(value or "").strip()
        if not URL_RE.match(v):
            raise CalendarProblem("The calendar address must start with https:// (or webcal://).")
        return "https://" + v[len("webcal://"):] if v.lower().startswith("webcal://") else v

    def add(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise CalendarProblem("Send the calendar as a JSON object.")
        label = " ".join(str(body.get("label") or "").split())[:LABEL_MAX]
        if not label:
            raise CalendarProblem("Give the calendar a name.")
        address = self._address(body.get("url"))
        items = self._load()
        if len(items) >= MAX_FEEDS:
            raise CalendarProblem(f"At most {MAX_FEEDS} calendars.")
        base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "cal"
        iid, n = "ics-" + base, 1
        while any(i["id"] == iid for i in items):
            n += 1
            iid = f"ics-{base}-{n}"
        item = {"id": iid, "kind": "ics", "label": label, "enabled": body.get("enabled") is not False,
                "color": self._color(body.get("color"), DEFAULT_COLORS[len(items) % len(DEFAULT_COLORS)])}
        try:
            self.store.set("cal:" + iid, address)
        except Exception:  # noqa: BLE001
            raise CalendarProblem("Secure storage (the OS keyring) is not available, so the address was not saved.", 503) from None
        items.append(item)
        self._save(items)
        self.ctx.audit("calendar_feed_add", id=iid)
        return self.view(item)

    def update(self, item_id: str, body: Any) -> dict:
        if not isinstance(body, dict):
            raise CalendarProblem("Send the changes as a JSON object.")
        items = self._load()
        item = self._find(items, item_id)
        if "label" in body:
            label = " ".join(str(body["label"] or "").split())[:LABEL_MAX]
            if not label:
                raise CalendarProblem("Give the calendar a name.")
            item["label"] = label
        if "color" in body:
            item["color"] = self._color(body["color"], item.get("color") or DEFAULT_COLORS[0])
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise CalendarProblem("enabled is true or false.")
            item["enabled"] = body["enabled"]
        if body.get("url"):
            if item["kind"] != "ics":
                raise CalendarProblem("Only an iCal feed has an address.")
            try:
                self.store.set("cal:" + item_id, self._address(body["url"]))
            except CalendarProblem:
                raise
            except Exception:  # noqa: BLE001
                raise CalendarProblem("Secure storage (the OS keyring) is not available.", 503) from None
            self.cache.pop(item_id, None)
        self._save(items)
        return self.view(item)

    def delete(self, item_id: str) -> None:
        items = self._load()
        item = self._find(items, item_id)
        if item.get("builtin") is True:
            item["enabled"] = False                        # the ready-made Google entry cannot vanish: it is only switched off
            self._save(items)
            return
        self.store.delete("cal:" + item_id)
        self._save([i for i in items if i["id"] != item_id])
        self.cache.pop(item_id, None)
        self.errors.pop(item_id, None)
        self.ctx.audit("calendar_feed_delete", id=item_id)

    # -- reading
    def _text(self, item: dict) -> str:
        iid = item["id"]
        hit = self.cache.get(iid)
        if hit and self.clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        url = self._url(iid)
        if not url:
            raise CalendarProblem("This calendar has no saved address. Edit it and paste the address again.", 409)
        try:
            status, raw = self.fetch(url)
        except (OSError, TimeoutError, urllib.error.URLError):
            raise CalendarProblem("Couldn't reach that calendar address.", 502) from None
        if status in (401, 403, 404, 410):
            raise CalendarProblem(f"The calendar server answered {status}. The address may have been reset.", 502)
        if not 200 <= status < 300:
            raise CalendarProblem(f"The calendar server answered {status}.", 502)
        if len(raw) > MAX_BYTES:
            raise CalendarProblem("That calendar file is larger than 5 MB.", 502)
        text = raw.decode("utf-8", "replace")
        if "BEGIN:VCALENDAR" not in text.upper():
            raise CalendarProblem("That address does not return an iCal (.ics) calendar.", 502)
        self.cache[iid] = (self.clock(), text)
        return text

    def feed_meetings(self, item: dict, win_start: dt.datetime, win_end: dt.datetime) -> list[dict]:
        local = self.tz or _local_tz()
        events = parse_ics(self._text(item), local)
        rows = expand(events, win_start, win_end, local)
        for r in rows:
            r["feed"], r["feed_id"], r["color"] = item.get("label") or "Calendar", item["id"], item.get("color") or DEFAULT_COLORS[0]
        return rows

    def meetings_between(self, start: dt.date, days: int = 1) -> list[dict]:
        """Meetings of every ENABLED ics feed in [start, start+days). A broken feed is skipped (and shown on its card)."""
        local = self.tz or _local_tz()
        ws = dt.datetime.combine(start, dt.time(), local)
        we = ws + dt.timedelta(days=max(1, min(int(days), 62)))
        out: list[dict] = []
        for item in self._load():
            if item["kind"] != "ics" or item.get("enabled") is False:
                continue
            try:
                out += self.feed_meetings(item, ws, we)
                self.errors.pop(item["id"], None)
            except CalendarProblem as exc:
                self.errors[item["id"]] = str(exc)
            except Exception:  # noqa: BLE001
                self.errors[item["id"]] = "That calendar could not be read."
        out.sort(key=lambda m: (not m["all_day"], m["start"]))
        return out

    def google_item(self) -> dict:
        return next(i for i in self._load() if i["id"] == "google")

    def test(self, item_id: str) -> dict:
        item = self._find(self._load(), item_id)
        if item["kind"] == "google":
            try:
                from .work.jarvis_bridge import Bridge
                ok = bool(Bridge(getattr(self.ctx.config, "jarvis_dir", "") or "").state("calendar").configured)
            except Exception:  # noqa: BLE001
                ok = False
            return {"ok": ok, "message": "Google Calendar is connected." if ok else "Run setup calendar in the Jarvis folder to connect Google Calendar."}
        self.cache.pop(item_id, None)
        local = self.tz or _local_tz()
        today = dt.datetime.combine(dt.datetime.now(local).date(), dt.time(), local)
        rows = self.feed_meetings(item, today, today + dt.timedelta(days=7))
        self.errors.pop(item_id, None)
        return {"ok": True, "found": len(rows), "message": f"Connected. {len(rows)} event(s) in the next 7 days."}


def get(ctx: Ctx, **overrides) -> Calendars:
    reg = getattr(ctx, "calendars", None)
    if reg is None or overrides:
        reg = Calendars(ctx, **overrides)
        ctx.calendars = reg                      # type: ignore[attr-defined]
    return reg


def meetings_between(ctx: Ctx, start: dt.date, days: int = 1) -> list[dict]:
    """Used by hub/work: the meetings of every enabled feed other than Google."""
    return get(ctx).meetings_between(start, days)


def google_enabled(ctx: Ctx) -> bool:
    try:
        return get(ctx).google_item().get("enabled") is not False
    except Exception:  # noqa: BLE001
        return True


def google_color(ctx: Ctx) -> str:
    try:
        return get(ctx).google_item().get("color") or DEFAULT_COLORS[0]
    except Exception:  # noqa: BLE001
        return DEFAULT_COLORS[0]


# ---- the kind interface used by hub/sources.py -------------------------------------------------------------

def kind_list(ctx: Ctx) -> dict:
    return {"items": get(ctx).list_items(), "colors": DEFAULT_COLORS}


def kind_add(ctx: Ctx, body: Any) -> dict:
    return get(ctx).add(body)


def kind_update(ctx: Ctx, item_id: str, body: Any) -> dict:
    return get(ctx).update(item_id, body)


def kind_delete(ctx: Ctx, item_id: str) -> None:
    get(ctx).delete(item_id)


def kind_test(ctx: Ctx, item_id: str) -> dict:
    return get(ctx).test(item_id)


def make_router(ctx: Ctx, **overrides) -> APIRouter:
    reg = get(ctx, **overrides) if overrides else get(ctx)
    router = APIRouter(prefix="/api/calendars", dependencies=[Depends(ctx.need_auth)])

    @router.get("/agenda")
    def agenda(day: str | None = Query(None, max_length=10), days: int = Query(1, ge=1, le=31)):
        try:
            start = dt.date.fromisoformat(day) if day else dt.date.today()
        except ValueError:
            raise HTTPException(400, "day must look like 2026-11-04.") from None
        return {"meetings": reg.meetings_between(start, days)}

    return router
