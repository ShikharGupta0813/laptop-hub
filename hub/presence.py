"""PRESENCE: where the owner is, and so how each notification should reach them. See docs/api/presence.md.

A presence STATE is computed from optional signals (each injectable, each may be missing):
  laptop idle time (Windows GetLastInputInfo), lock state (Windows OpenInputDesktop), the phone app heartbeat
  (`POST /api/presence/phone`), the calendar (a timed event happening now), a manual focus flag and manual
  "I am going out" / "I am back" overrides, and quiet hours (hub/proactive.py's own window).
States: at_laptop | near | away | phone_active | in_meeting | focus | quiet_hours | unknown.

DELIVERY RULES (an editable list, `presence_rules.json`) map (urgency class, state) -> actions:
  silent-list | toast-on-laptop | push-silent | push-phone | speak-phone | speak-laptop | hold-until-free | batch
Urgency class: critical (alert priority 3 / VIP), urgent (anything proactive decides is 'now'), normal (batched kinds).
Held items are released as ONE digest when the state that held them ends.

Hook: hub/proactive.py calls `route()` for 'now' and 'batch' decisions. It returns None (= the old behaviour, untouched)
when the feature is off, the state is unknown / quiet_hours, a panic is active or no rule matches. Nothing here ever
raises into the caller.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from .common import Ctx, JsonFile

log = logging.getLogger("hub.presence")

STATES = ("at_laptop", "near", "away", "phone_active", "in_meeting", "focus", "quiet_hours", "unknown")
IMMEDIATE = ("at_laptop", "focus", "in_meeting", "quiet_hours")     # entered without waiting for the dwell time
HOLD_STATES = ("in_meeting", "focus")
URGENCIES = ("critical", "urgent", "normal")
ACTIONS = ("silent-list", "toast-on-laptop", "push-silent", "push-phone", "speak-phone", "speak-laptop", "hold-until-free", "batch")
AT_LAPTOP_S = 180            # input within 3 minutes
AWAY_S = 900                 # idle over 15 minutes
PHONE_TTL_S = 75             # a heartbeat every 30 s; this much silence and the phone app is not open
SAMPLE_S = 15
DWELL_S = 60                 # between two "where am I" states a change must hold this long (no flapping)
CALENDAR_CACHE_S = 60
MAX_HELD = 100
MAX_RULES = 60
MAX_SPEECH = 20
SPEECH_TTL_S = 600
FOCUS_MAX_MIN = 720
AWAY_DEFAULT_MIN = 480
BACK_DEFAULT_MIN = 30
Clock = Callable[[], float]

ENGINES: dict[int, "Presence"] = {}


def _rule(u: str, s: str, *actions: str) -> dict:
    return {"id": f"{u}-{s}", "urgency": u, "state": s, "actions": list(actions), "enabled": True}


DEFAULT_RULES: list[dict] = [
    _rule("critical", "at_laptop", "toast-on-laptop", "push-silent", "speak-laptop"),
    _rule("critical", "near", "push-phone", "speak-phone"),
    _rule("critical", "away", "push-phone", "speak-phone"),
    _rule("critical", "phone_active", "push-phone", "speak-phone"),
    _rule("critical", "in_meeting", "push-silent"),
    _rule("critical", "focus", "push-phone"),
    _rule("urgent", "at_laptop", "toast-on-laptop", "push-silent"),
    _rule("urgent", "near", "push-phone"),
    _rule("urgent", "away", "push-phone", "speak-phone"),
    _rule("urgent", "phone_active", "push-phone", "speak-phone"),
    _rule("urgent", "in_meeting", "hold-until-free"),
    _rule("urgent", "focus", "hold-until-free"),
    _rule("normal", "at_laptop", "toast-on-laptop"),
    _rule("normal", "near", "batch"),
    _rule("normal", "away", "batch"),
    _rule("normal", "phone_active", "batch"),
    _rule("normal", "in_meeting", "hold-until-free"),
    _rule("normal", "focus", "hold-until-free"),
]

DEFAULT_PHRASES = {
    "away": ["i am going out", "i'm going out", "i am heading out", "i'm heading out", "i am leaving", "i'm leaving", "i am away", "i'm away"],
    "back": ["i am back", "i'm back", "im back", "i am home", "i'm home"],
    "focus": ["focus for", "focus mode", "do not disturb", "don't disturb me", "focus"],
    "focus_off": ["stop focus", "focus off", "end focus", "end focus mode", "stop focus mode", "focus mode off"],
}
PHRASE_KEYS = tuple(DEFAULT_PHRASES)


# ---- pure helpers ------------------------------------------------------------------------------------------
def event_class(event: dict, decision: str) -> str:
    def num(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else 0
    if num(event.get("urgency")) >= 3 or num(event.get("priority")) >= 3 or event.get("vip") is True:
        return "critical"
    return "urgent" if decision == "now" else "normal"


def clean_rules(raw: Any) -> list[dict]:
    """Only well-formed rules; anything else is dropped. Ids are made unique."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in (raw if isinstance(raw, list) else [])[:MAX_RULES]:
        if not isinstance(r, dict) or r.get("urgency") not in URGENCIES or r.get("state") not in (*STATES, "*"):
            continue
        acts = [a for a in (r.get("actions") if isinstance(r.get("actions"), list) else []) if a in ACTIONS]
        acts = list(dict.fromkeys(acts))[:5]
        if not acts:
            continue
        rid = re.sub(r"[^a-z0-9_-]", "", str(r.get("id") or "").lower())[:40] or f"{r['urgency']}-{r['state']}"
        while rid in seen:
            rid = f"{rid}-{secrets.token_hex(2)}"
        seen.add(rid)
        out.append({"id": rid, "urgency": r["urgency"], "state": r["state"], "actions": acts, "enabled": r.get("enabled") is not False})
    return out


def plan(rules: list[dict], urgency: str, state: str) -> list[str] | None:
    """The actions for (urgency, state), or None when no enabled rule matches (= old behaviour). Exact state beats '*';
    a critical item with no critical rule of its own follows the urgent rules."""
    order = [urgency] + (["urgent"] if urgency == "critical" else [])
    for u in order:
        for want in (state, "*"):
            for r in rules:
                if r.get("enabled") is not False and r["urgency"] == u and r["state"] == want:
                    return list(r["actions"])
    return None


def compute_state(*, idle: float | None, locked: bool | None, phone_active: bool, meeting: bool, focus: bool,
                  override: str | None, quiet: bool) -> str:
    """The instant (un-debounced) state. Pure."""
    if focus:
        return "focus"
    if override in ("away", "at_laptop"):
        return override
    if meeting:
        return "in_meeting"
    if quiet:
        return "quiet_hours"
    if locked is not True and idle is not None and idle < AT_LAPTOP_S:
        return "at_laptop"
    if phone_active:
        return "phone_active"
    if locked is True or (idle is not None and idle >= AWAY_S):
        return "away"
    if idle is not None:
        return "near"
    return "unknown"


_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
              "forty": 40, "forty-five": 45, "sixty": 60, "ninety": 90}


def parse_duration(text: str) -> int | None:
    """'for an hour' / '30 minutes' / 'half an hour' / '2 hours' -> minutes, or None."""
    t = re.sub(r"[^a-z0-9\s-]", " ", str(text or "").lower())
    t = re.sub(r"^\s*for\s+", "", " ".join(t.split()))
    if re.fullmatch(r"(?:half an hour|half hour|30 min(?:ute)?s?)", t):
        return 30
    m = re.fullmatch(r"(\d{1,3}|[a-z-]+)\s*(hours?|hrs?|h|minutes?|mins?|m)", t)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS.get(m.group(1))
    if not n:
        return None
    return n * 60 if m.group(2).startswith("h") else n


def match_phrase(text: str, phrases: dict | None = None) -> dict | None:
    """{"kind": away|back|focus|focus_off, "minutes": int|None} for a short, clear "I am going out" style sentence."""
    t = str(text or "").lower().replace("’", "'")
    t = re.sub(r"^(?:hey |ok |okay )?(?:jarvis|friday)\s*[,.:]?\s*", "", t.strip())
    t = " ".join(re.sub(r"[^a-z0-9'\s-]", " ", t).split())
    if not t or len(t.split()) > 10:
        return None
    table = phrases or DEFAULT_PHRASES
    for kind in ("focus_off", "back", "away", "focus"):
        for ph in sorted((p for p in table.get(kind, []) if isinstance(p, str) and p.strip()), key=len, reverse=True):
            ph = " ".join(ph.lower().split())
            if t == ph or t.startswith(ph + " "):
                rest = t[len(ph):].strip()
                if kind in ("back", "focus_off"):
                    if rest and rest not in ("now", "please"):
                        continue
                    return {"kind": kind, "minutes": None}
                rest = re.sub(r"\s*\bplease$", "", rest)
                if not rest:
                    return {"kind": kind, "minutes": None}
                mins = parse_duration(rest)
                if mins:
                    return {"kind": kind, "minutes": mins}
    return None


# ---- the real sensors (Windows only; everything else reports None = unknown) ---------------------------------
def windows_idle_seconds() -> float | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return max(0.0, ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0)
    except Exception:  # noqa: BLE001
        return None


def windows_locked() -> bool | None:
    """The input desktop cannot be opened while the workstation is locked (or a secure prompt is up)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        handle = user32.OpenInputDesktop(0, False, 0x0100)      # DESKTOP_SWITCHDESKTOP
        if not handle:
            return True
        user32.CloseDesktop(ctypes.c_void_p(handle))
        return False
    except Exception:  # noqa: BLE001
        return None


def toast_argv() -> list[str] | None:
    """The command that shows a Windows toast. The TITLE is read from STDIN and is never part of the argv."""
    if sys.platform != "win32":
        return None
    import shutil
    ps = shutil.which("powershell") or "powershell"
    script = ("$t = [Console]::In.ReadToEnd().Trim(); "
              "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
              "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText01); "
              "$x.GetElementsByTagName('text').Item(0).AppendChild($x.CreateTextNode($t)) | Out-Null; "
              "$n = [Windows.UI.Notifications.ToastNotification]::new($x); "
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('REDLINE').Show($n)")
    return [ps, "-NoProfile", "-NonInteractive", "-Command", script]


def default_toaster(title: str) -> None:
    argv = toast_argv()
    title = " ".join(str(title or "").split())[:100]
    if argv is None or not title:
        return                                   # not Windows: the hub page's own toast (notification stream) is the fallback
    try:
        subprocess.run(argv, input=title, text=True, encoding="utf-8", capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def default_speaker(text: str) -> None:
    try:
        from . import wakeword
        wakeword.default_speaker(text)
    except Exception:  # noqa: BLE001
        pass


def calendar_meeting(ctx: Ctx, now: dt.datetime) -> dict | None:
    """The timed (not all-day) event happening now from the hub's calendar feeds, or None. Guarded."""
    try:
        from . import calendars
        rows = calendars.meetings_between(ctx, now.date(), 1)
    except Exception:  # noqa: BLE001
        return None
    return meeting_in(rows, now)


def meeting_in(rows: list[dict], now: dt.datetime) -> dict | None:
    aware = now if now.tzinfo else now.astimezone()
    for m in rows or []:
        try:
            if m.get("all_day"):
                continue
            s, e = dt.datetime.fromisoformat(str(m["start"])), dt.datetime.fromisoformat(str(m["end"]))
            s, e = (s if s.tzinfo else s.astimezone()), (e if e.tzinfo else e.astimezone())
            if s <= aware < e:
                return {"title": str(m.get("title") or "")[:80], "end": e.isoformat(timespec="seconds")}
        except (KeyError, ValueError, TypeError):
            continue
    return None


# ---- the engine -------------------------------------------------------------------------------------------
class Presence:
    def __init__(self, ctx: Ctx, *, clock: Clock | None = None, idle_source: Callable[[], float | None] | None = None,
                 lock_source: Callable[[], bool | None] | None = None,
                 calendar_source: Callable[[dt.datetime], dict | None] | None = None,
                 toaster: Callable[[str], None] | None = None, speaker: Callable[[str], None] | None = None,
                 dwell: float = DWELL_S, sync: bool = False):
        self.ctx = ctx
        self.clock: Clock = clock or time.time
        self.idle_source = idle_source or windows_idle_seconds
        self.lock_source = lock_source or windows_locked
        self.calendar_source = calendar_source or (lambda now: calendar_meeting(ctx, now))
        self.toaster = toaster or default_toaster
        self.speaker = speaker or default_speaker
        self.dwell = dwell
        self.sync = sync                       # tests: run toast / speech inline instead of on a thread
        self.lock = threading.RLock()
        self.file = JsonFile(ctx.config.data_path() / "presence.json")
        self.rules_file = JsonFile(ctx.config.data_path() / "presence_rules.json")
        self.phrases_file = JsonFile(ctx.config.data_path() / "presence_phrases.json")
        self.phone: dict = {}                  # {"visible", "screen_on", "at"}; memory only
        self.committed: str | None = None
        self.since: float = 0.0
        self.cand: str | None = None
        self.cand_since: float = 0.0
        self.last_sample: float = -1e9
        self.signals: dict = {}
        self.speech: list[dict] = []
        self.manual_pause = False
        self._cal: tuple[float, dict | None] = (-1e9, None)

    # ---- config --------------------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return getattr(self.ctx.config, "presence_enabled", True) is not False

    @property
    def toasts(self) -> bool:
        return getattr(self.ctx.config, "presence_laptop_toasts", False) is True

    def paused(self) -> bool:
        """True while a panic is active (hub/panic.py, when it exists) or the owner paused the rules: everything then
        goes out the old, direct way."""
        if self.manual_pause:
            return True
        try:
            panic = importlib.import_module("hub.panic")
            return bool(panic.on(self.ctx.config))
        except Exception:  # noqa: BLE001 - the module may not exist
            return False
        return False

    # ---- stored state -------------------------------------------------------------------------------------
    def _load(self) -> dict:
        d = self.file.load({})
        d = d if isinstance(d, dict) else {}
        ov = d.get("override") if isinstance(d.get("override"), dict) else {}
        return {"focus_until": float(d["focus_until"]) if isinstance(d.get("focus_until"), (int, float)) else 0.0,
                "override": {"state": ov.get("state") if ov.get("state") in ("away", "at_laptop") else "",
                             "until": float(ov["until"]) if isinstance(ov.get("until"), (int, float)) else 0.0},
                "held": [h for h in d.get("held", []) if isinstance(h, dict)][-MAX_HELD:]}

    def _save(self, d: dict) -> None:
        try:
            self.file.save(d)
        except OSError:
            log.warning("presence state could not be saved")

    def rules(self) -> list[dict]:
        d = self.rules_file.load(None)
        if isinstance(d, dict) and isinstance(d.get("rules"), list):
            return clean_rules(d["rules"])
        return [dict(r, actions=list(r["actions"])) for r in DEFAULT_RULES]

    def set_rules(self, raw: Any) -> list[dict]:
        rules = clean_rules(raw)
        self.rules_file.save({"rules": rules})
        return rules

    def reset_rules(self) -> None:
        try:
            self.rules_file.path.unlink()
        except OSError:
            pass

    def phrases(self) -> dict:
        d = self.phrases_file.load(None)
        out = {k: list(v) for k, v in DEFAULT_PHRASES.items()}
        if isinstance(d, dict):
            for k in PHRASE_KEYS:
                if isinstance(d.get(k), list):
                    out[k] = [" ".join(str(x).lower().split())[:60] for x in d[k] if isinstance(x, str) and x.strip()][:20]
        return out

    def set_phrases(self, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("phrases must be an object.")
        cur = self.phrases()
        for k in PHRASE_KEYS:
            if k in raw:
                if not isinstance(raw[k], list):
                    raise ValueError(f"{k} must be a list of phrases.")
                cur[k] = [" ".join(str(x).lower().split())[:60] for x in raw[k] if isinstance(x, str) and x.strip()][:20]
        self.phrases_file.save(cur)
        return cur

    # ---- manual flags --------------------------------------------------------------------------------------
    def set_focus(self, minutes: float | None) -> dict:
        """minutes None / 0 = off."""
        with self.lock:
            d = self._load()
            d["focus_until"] = self.clock() + float(minutes) * 60 if minutes else 0.0
            self._save(d)
        self.observe(force=True)
        return self.focus_view()

    def set_override(self, kind: str, minutes: float | None = None) -> None:
        """kind away | back | clear. 'back' pins at_laptop for a while (the owner said they are here)."""
        with self.lock:
            d = self._load()
            if kind == "clear":
                d["override"] = {"state": "", "until": 0.0}
            else:
                state = "away" if kind == "away" else "at_laptop"
                mins = minutes or (AWAY_DEFAULT_MIN if kind == "away" else BACK_DEFAULT_MIN)
                d["override"] = {"state": state, "until": self.clock() + float(mins) * 60}
            self._save(d)
        self.observe(force=True)

    def focus_view(self) -> dict:
        until = self._load()["focus_until"]
        left = until - self.clock()
        return {"on": left > 0, "until": until if left > 0 else None, "minutes_left": max(0, int(left // 60) + (1 if left % 60 else 0)) if left > 0 else 0}

    # ---- phone heartbeat ----------------------------------------------------------------------------------
    def phone_beat(self, visible: bool, screen_on: bool | None) -> None:
        self.phone = {"visible": bool(visible), "screen_on": screen_on, "at": self.clock()}

    def phone_active(self) -> bool:
        p = self.phone
        return bool(p) and p.get("visible") is True and p.get("screen_on") is not False and self.clock() - p["at"] <= PHONE_TTL_S

    def app_open(self) -> bool:
        p = self.phone
        return bool(p) and p.get("visible") is True and self.clock() - p["at"] <= PHONE_TTL_S

    # ---- sampling and the state machine ------------------------------------------------------------------
    def _guard(self, fn: Callable, *a):
        try:
            return fn(*a)
        except Exception:  # noqa: BLE001 - a broken sensor means unknown, never an error
            return None

    def _meeting(self, now_dt: dt.datetime, t: float) -> dict | None:
        if t - self._cal[0] >= CALENDAR_CACHE_S:
            self._cal = (t, self._guard(self.calendar_source, now_dt))
        return self._cal[1]

    def _quiet(self, now_dt: dt.datetime) -> bool:
        from . import proactive
        c = self.ctx.config
        return proactive.in_quiet(now_dt, str(getattr(c, "proactive_quiet_start", "22:00")), str(getattr(c, "proactive_quiet_end", "07:00")))

    def raw(self) -> tuple[str, dict]:
        t = self.clock()
        now_dt = dt.datetime.fromtimestamp(t)
        d = self._load()
        idle = self._guard(self.idle_source)
        idle = float(idle) if isinstance(idle, (int, float)) and not isinstance(idle, bool) else None
        locked = self._guard(self.lock_source)
        locked = locked if isinstance(locked, bool) else None
        focus = d["focus_until"] > t
        ov = d["override"]["state"] if d["override"]["until"] > t else ""
        meeting = self._meeting(now_dt, t)
        phone = self.phone_active()
        quiet = bool(self._guard(self._quiet, now_dt))
        sig = {"laptop_idle_s": None if idle is None else round(idle), "locked": locked,
               "phone": {"visible": self.phone.get("visible"), "screen_on": self.phone.get("screen_on"),
                         "age_s": round(t - self.phone["at"]) if self.phone else None, "active": phone, "app_open": self.app_open()},
               "meeting": meeting or None, "focus": bool(focus), "override": ov or None, "quiet_hours": quiet}
        return compute_state(idle=idle, locked=locked, phone_active=phone, meeting=bool(meeting), focus=focus,
                             override=ov or None, quiet=quiet), sig

    def observe(self, force: bool = False) -> str:
        """Take a sample and move the committed state (with hysteresis between the 'where am I' states)."""
        with self.lock:
            t = self.clock()
            if not force and self.committed is not None and t - self.last_sample < SAMPLE_S:
                return self.committed
            raw, self.signals = self.raw()
            self.last_sample = t
            if self.committed is None:
                self.committed, self.since, self.cand = raw, t, None
            elif raw == self.committed:
                self.cand = None
            elif raw in IMMEDIATE or self.committed in IMMEDIATE:
                self.committed, self.since, self.cand = raw, t, None
            else:
                if self.cand != raw:
                    self.cand, self.cand_since = raw, t
                if t - self.cand_since >= self.dwell:
                    self.committed, self.since, self.cand = raw, t, None
            return self.committed

    def state(self) -> str:
        return self.observe() if self.enabled else "unknown"

    def view(self) -> dict:
        st = self.state()
        d = self._load()
        return {"enabled": self.enabled, "paused": self.paused(), "state": st,
                "since": dt.datetime.fromtimestamp(self.since).isoformat(timespec="seconds") if self.since else None,
                "signals": self.signals, "focus": self.focus_view(), "held": len(d["held"]),
                "override": ({"state": d["override"]["state"], "until": d["override"]["until"]}
                             if d["override"]["until"] > self.clock() and d["override"]["state"] else None)}

    # ---- delivery -----------------------------------------------------------------------------------------
    def actions_for(self, event: dict, decision: str) -> list[str] | None:
        """The planned actions, or None for 'do what the hub always did'. Used by proactive and by alerts (speech)."""
        if not self.enabled or decision not in ("now", "batch") or self.paused():
            return None
        st = self.state()
        if st in ("unknown", "quiet_hours"):
            return None
        return plan(self.rules(), event_class(event, decision), st)

    def _notify(self, title: str, body: str, level: str, link: str | None, phone: Any) -> None:
        try:
            if phone is True:
                self.ctx.notify(title, body=body, level=level, link=link, source="proactive")
            else:
                try:
                    self.ctx.notify(title, body=body, level=level, link=link, source="proactive", phone=phone)
                except TypeError:
                    pass                    # a notifier without the phone switch: never push when we were told not to
        except Exception:  # noqa: BLE001
            log.warning("presence notify failed")

    def _bg(self, fn: Callable, *a) -> None:
        if self.sync:
            fn(*a)
        else:
            threading.Thread(target=fn, args=a, daemon=True, name="hub-presence").start()

    def route(self, event: dict, decision: str, title: str, body: str, link: str | None, level: str) -> str | None:
        """Act on one proactive decision. None = the caller does its normal thing."""
        acts = self.actions_for(event, decision)
        if not acts:
            return None
        if "hold-until-free" in acts:
            self.hold(event, title, body, link, level)
            return "held"
        if "batch" in acts:
            return "batch"                  # the caller's own batching (one sentence, 30 minute window)
        phone: Any = True if "push-phone" in acts else "silent" if "push-silent" in acts else False
        self._notify(title or "Friday", body, level, link, phone)
        if "toast-on-laptop" in acts and self.toasts:
            self._bg(self.toaster, title)   # the title only, never the body
        if event.get("type") != "alert":    # alerts speak through their own queue (alerts.py asks actions_for)
            if "speak-phone" in acts and self.app_open():
                self.enqueue_speech(body or title)
            if "speak-laptop" in acts:
                self._bg(self.speaker, title)
        return "now" if phone is not False or "speak-phone" in acts else "handled"

    def hold(self, event: dict, title: str, body: str, link: str | None, level: str) -> None:
        with self.lock:
            d = self._load()
            d["held"].append({"type": str(event.get("type") or ""), "title": title[:120], "body": body[:200], "link": link,
                              "level": level, "at": self.clock(), "reason": self.state()})
            d["held"] = d["held"][-MAX_HELD:]
            self._save(d)

    def release(self, force: bool = False) -> bool:
        """When the state that held items is over, deliver them as ONE digest. True when something was sent."""
        with self.lock:
            st = self.state()
            d = self._load()
            if not d["held"] or (st in HOLD_STATES and not force):
                return False
            held, d["held"] = d["held"], []
            self._save(d)
        n = len(held)
        reasons = {h.get("reason") for h in held}
        where = ("in the meeting" if reasons == {"in_meeting"} else "while you were in focus mode" if reasons == {"focus"}
                 else "while you were busy")
        title = f"You missed {n} thing{'s' if n != 1 else ''} {where}"
        lines = [h["title"] for h in held if h.get("title")][:8]
        body = "; ".join(lines) + (f"; and {n - len(lines)} more" if n > len(lines) else "")
        self._notify(title, body, "warn" if any(h.get("level") in ("warn", "error") for h in held) else "info", None,
                     False if st == "at_laptop" else True)
        if st == "at_laptop" and self.toasts:
            self._bg(self.toaster, title)
        return True

    def enqueue_speech(self, text: str) -> None:
        text = " ".join(str(text or "").split())[:200]
        if not text:
            return
        with self.lock:
            self.speech.append({"id": secrets.token_hex(4), "text": text, "at": self.clock()})
            self.speech = self.speech[-MAX_SPEECH:]

    def pending_speech(self) -> list[dict]:
        with self.lock:
            cut = self.clock() - SPEECH_TTL_S
            self.speech = [s for s in self.speech if s["at"] >= cut]
            return [{"id": s["id"], "text": s["text"]} for s in self.speech]

    def ack_speech(self, ids: list) -> int:
        with self.lock:
            before = len(self.speech)
            self.speech = [s for s in self.speech if s["id"] not in {str(i) for i in ids}]
            return before - len(self.speech)

    def tick(self) -> None:
        if not self.enabled:
            return
        try:
            self.observe(force=True)
            self.release()
        except Exception:  # noqa: BLE001
            log.exception("presence tick failed")

    async def loop(self) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(SAMPLE_S)

    # ---- voice ---------------------------------------------------------------------------------------------
    def say(self, text: str) -> str:
        """The fast-command layer: '' when `text` is not a presence phrase, else Friday's one-line answer."""
        if not self.enabled:
            return ""
        hit = match_phrase(text, self.phrases())
        if not hit:
            return ""
        kind, mins = hit["kind"], hit["minutes"]
        if kind == "focus":
            self.set_focus(mins or 60)
            n = mins or 60
            return f"Focus is on for {n} minutes. I will hold everything except emergencies."
        if kind == "focus_off":
            self.set_focus(None)
            self.release()
            return "Focus is off."
        if kind == "away":
            self.set_override("away", mins)
            return "Noted, you are out. I will send things to your phone."
        self.set_override("back")
        self.release()
        return "Welcome back."


# ---- module-level entry points ------------------------------------------------------------------------------
def engine_for(ctx: Ctx) -> "Presence | None":
    return ENGINES.get(id(ctx.config))


def route(ctx: Ctx, event: dict, decision: str, title: str, body: str, link: str | None, level: str) -> str | None:
    eng = getattr(ctx, "presence", None)
    if eng is None or decision not in ("now", "batch"):
        return None
    try:
        return eng.route(event, decision, title, body, link, level)
    except Exception:  # noqa: BLE001
        log.exception("presence route failed")
        return None


def alert_actions(ctx: Ctx, event: dict) -> list[str] | None:
    """For hub/alerts.py: the planned actions for a 'now' alert, or None (= speak as before)."""
    eng = getattr(ctx, "presence", None)
    if eng is None:
        return None
    try:
        return eng.actions_for(event, "now")
    except Exception:  # noqa: BLE001
        return None


def fast_reply(config: Any, text: str) -> str:
    """For hub/assistant.py's fast-command layer: '' unless `text` is a presence phrase."""
    eng = ENGINES.get(id(config))
    if eng is None:
        return ""
    try:
        return eng.say(text)
    except Exception:  # noqa: BLE001
        return ""


def make_router(ctx: Ctx, **kw) -> APIRouter:
    eng = Presence(ctx, **kw)
    ctx.presence = eng            # type: ignore[attr-defined]
    ENGINES[id(ctx.config)] = eng
    router = APIRouter(prefix="/api/presence", dependencies=[Depends(ctx.need_auth)])
    router.engine = eng           # type: ignore[attr-defined]

    @router.get("")
    def get_presence():
        return eng.view()

    @router.post("/phone")
    def phone(body: dict = Body(default_factory=dict)):
        visible, screen = body.get("visible"), body.get("screen_on")
        if not isinstance(visible, bool) or (screen is not None and not isinstance(screen, bool)):
            raise HTTPException(400, "Send {visible: true|false, screen_on?: true|false}.")
        eng.phone_beat(visible, screen)
        return {"ok": True, "state": eng.state()}

    @router.post("/focus")
    def focus(body: dict = Body(default_factory=dict)):
        mins = body.get("minutes")
        if body.get("off") is True or mins == "off" or mins == 0:
            out = eng.set_focus(None)
            eng.release()
        elif isinstance(mins, (int, float)) and not isinstance(mins, bool) and 1 <= mins <= FOCUS_MAX_MIN:
            out = eng.set_focus(mins)
        else:
            raise HTTPException(400, f"Send {{minutes: 1-{FOCUS_MAX_MIN}}} or {{off: true}}.")
        ctx.audit("presence_focus", on=out["on"])
        return {"focus": out, "state": eng.state()}

    @router.post("/override")
    def override(body: dict = Body(default_factory=dict)):
        kind, mins = body.get("state"), body.get("minutes")
        if kind not in ("away", "back", "clear") or (mins is not None and not (isinstance(mins, (int, float)) and not isinstance(mins, bool) and 1 <= mins <= 1440)):
            raise HTTPException(400, "Send {state: away|back|clear, minutes?: 1-1440}.")
        eng.set_override(kind, mins)
        if kind != "away":
            eng.release()
        ctx.audit("presence_override", state=kind)
        return eng.view()

    @router.get("/rules")
    def get_rules():
        return {"rules": eng.rules(), "actions": list(ACTIONS), "states": [*STATES, "*"], "urgencies": list(URGENCIES)}

    @router.put("/rules")
    def put_rules(body: dict = Body(default_factory=dict)):
        if not isinstance(body.get("rules"), list) or len(body["rules"]) > MAX_RULES:
            raise HTTPException(400, "rules must be a list.")
        rules = eng.set_rules(body["rules"])
        ctx.audit("presence_rules_update", count=len(rules))
        return {"rules": rules, "actions": list(ACTIONS), "states": [*STATES, "*"], "urgencies": list(URGENCIES)}

    @router.post("/rules/reset")
    def reset_rules():
        eng.reset_rules()
        ctx.audit("presence_rules_reset")
        return {"rules": eng.rules(), "actions": list(ACTIONS), "states": [*STATES, "*"], "urgencies": list(URGENCIES)}

    @router.get("/phrases")
    def get_phrases():
        return {"phrases": eng.phrases()}

    @router.put("/phrases")
    def put_phrases(body: dict = Body(default_factory=dict)):
        try:
            return {"phrases": eng.set_phrases(body.get("phrases"))}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/pending")
    def pending():
        return {"speech": eng.pending_speech()}

    @router.post("/pending/ack")
    def ack(body: dict = Body(default_factory=dict)):
        ids = body.get("ids")
        if not isinstance(ids, list) or len(ids) > 50:
            raise HTTPException(400, "ids must be a list.")
        return {"acked": eng.ack_speech(ids)}

    return router


async def start(ctx: Ctx):
    eng = getattr(ctx, "presence", None)
    if eng is None:
        return None
    task = asyncio.get_running_loop().create_task(eng.loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ENGINES.pop(id(ctx.config), None)
    return stop
