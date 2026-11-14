"""Usage counters and the daily budget: how many AI requests went to which provider today.

Counts requests per provider per local day (gemini, claude-login, claude, duo, local, council, research, vision,
autopilot) in `usage.json` in the hub data folder (14 days kept, a damaged file starts over). Optional caps:
`cloud_daily_cap` (all cloud providers together) and `provider_daily_caps` {provider: n}; 0 / missing = no cap.
The router uses `cap_reached` to send a turn to the local model instead of a paid one (see hub/router.py).
Claude-login also keeps timestamps of its calls for a 5-hour window estimate (`claude_login_5h_soft_cap`).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

PROVIDERS = ("gemini", "claude-login", "claude", "duo", "local", "council", "research", "vision", "autopilot", "endpoint")
CLOUD = ("gemini", "claude-login", "claude", "duo")
LABELS = {"gemini": "Gemini", "claude-login": "Claude (my login)", "claude": "Claude (API key)", "duo": "Claude + Gemini",
          "local": "the local model", "council": "Council", "research": "Research", "vision": "Vision", "autopilot": "Autopilot",
          "endpoint": "My AI endpoint"}
KEEP_DAYS = 14
WINDOW_SECONDS = 5 * 3600


class Tracker:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def _load(self) -> dict:
        if self._data is None:
            data: Any = {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            days = data.get("days") if isinstance(data.get("days"), dict) else {}
            clean: dict[str, dict[str, int]] = {}
            for day, row in days.items():
                if isinstance(row, dict) and isinstance(day, str) and len(day) == 10:
                    clean[day] = {k: v for k, v in row.items()
                                  if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            calls = data.get("login_calls") if isinstance(data.get("login_calls"), list) else []
            self._data = {"days": clean, "login_calls": [t for t in calls if isinstance(t, (int, float)) and not isinstance(t, bool)]}
        return self._data

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass    # counting must never break a request

    def record(self, provider: str, n: int = 1) -> None:
        if not provider:
            return
        with self._lock:
            data = self._load()
            now = self._clock()
            row = data["days"].setdefault(self._today(), {})
            row[provider] = row.get(provider, 0) + n
            cutoff = (date.fromtimestamp(now) - timedelta(days=KEEP_DAYS - 1)).isoformat()
            for day in [d for d in data["days"] if d < cutoff]:
                del data["days"][day]
            if provider == "claude-login":
                data["login_calls"] = [t for t in data["login_calls"] if now - t < WINDOW_SECONDS] + [now] * n
            self._save(data)

    def today(self) -> dict[str, int]:
        with self._lock:
            return dict(self._load()["days"].get(self._today(), {}))

    def week(self) -> dict[str, int]:
        with self._lock:
            days = self._load()["days"]
            now = self._clock()
            out: dict[str, int] = {}
            for i in range(7):
                day = (date.fromtimestamp(now) - timedelta(days=i)).isoformat()
                for k, v in days.get(day, {}).items():
                    out[k] = out.get(k, 0) + v
            return out

    def login_window(self) -> int:
        with self._lock:
            now = self._clock()
            return sum(1 for t in self._load()["login_calls"] if now - t < WINDOW_SECONDS)


_TRACKERS: dict[str, Tracker] = {}
_TLOCK = threading.Lock()


def get(config) -> Tracker:
    """The tracker for this hub's data folder (one per folder, shared by every hook)."""
    path = str(Path(config.data_path()) / "usage.json")
    with _TLOCK:
        tr = _TRACKERS.get(path)
        if tr is None:
            tr = _TRACKERS[path] = Tracker(Path(path))
        return tr


def record(config, provider: str, n: int = 1) -> None:
    try:
        get(config).record(provider, n)
    except Exception:  # noqa: BLE001  counting must never break a request
        pass


# ---- caps ------------------------------------------------------------------------------------------------------

def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def caps(config) -> dict:
    raw = getattr(config, "provider_daily_caps", None)
    per = {k: _int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    return {"cloud_daily_cap": _int(getattr(config, "cloud_daily_cap", 0)),
            "provider_daily_caps": {k: v for k, v in per.items() if v and k in PROVIDERS},
            "claude_login_5h_soft_cap": _int(getattr(config, "claude_login_5h_soft_cap", 0))}


def cap_reached(config, provider: str) -> str:
    """"" when `provider` may be used now, else the sentence to show ("Daily Gemini cap reached; ...")."""
    if provider not in CLOUD:
        return ""
    c = caps(config)
    today = get(config).today()
    own = c["provider_daily_caps"].get(provider, 0)
    if own and today.get(provider, 0) >= own:
        return f"Daily {LABELS[provider]} cap reached; using the local model"
    total = c["cloud_daily_cap"]
    if total and sum(today.get(p, 0) for p in CLOUD) >= total:
        return "Daily cloud cap reached; using the local model"
    return ""


def report(config) -> dict:
    tr = get(config)
    c = caps(config)
    today, week = tr.today(), tr.week()
    rows = []
    for p in PROVIDERS:
        cap = c["provider_daily_caps"].get(p, 0)
        used = today.get(p, 0)
        rows.append({"provider": p, "label": LABELS[p], "today": used, "week": week.get(p, 0), "cap": cap,
                     "remaining": max(0, cap - used) if cap else None})
    cloud_used = sum(today.get(p, 0) for p in CLOUD)
    total = c["cloud_daily_cap"]
    out = {"day": tr._today(), "today": {p: today.get(p, 0) for p in PROVIDERS}, "week": {p: week.get(p, 0) for p in PROVIDERS},
           "providers": rows, "caps": c,
           "cloud": {"used": cloud_used, "cap": total, "remaining": max(0, total - cloud_used) if total else None}}
    soft = c["claude_login_5h_soft_cap"]
    if soft:
        n = tr.login_window()
        out["claude_login_5h"] = {"calls": n, "soft_cap": soft, "remaining": max(0, soft - n), "estimate": True}
    return out
