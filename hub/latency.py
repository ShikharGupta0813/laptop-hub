"""Tiny in-memory timing log: how long each voice/Talk turn took (nothing is written to disk).

Each turn is one span: {time, provider, voice, stt_ms, first_token_ms, total_ms, tools, fast, ok}. The newest
`MAX_SPANS` are kept. `GET /api/latency` returns them with a one-line summary for the diagnostics screen.
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque

MAX_SPANS = 50
STT_FRESH_SECONDS = 90.0     # a transcription this recent belongs to the turn that follows it


class Latency:
    def __init__(self, clock=time.time, size: int = MAX_SPANS):
        self._spans: deque[dict] = deque(maxlen=size)
        self._lock = threading.Lock()
        self._clock = clock
        self._stt: tuple[float, int] | None = None

    def note_stt(self, ms: float) -> None:
        with self._lock:
            self._stt = (self._clock(), int(round(ms)))

    def take_stt(self) -> int | None:
        with self._lock:
            if self._stt is None:
                return None
            at, ms = self._stt
            self._stt = None
            return ms if self._clock() - at <= STT_FRESH_SECONDS else None

    def add(self, **span) -> dict:
        row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._clock())), **span}
        with self._lock:
            self._spans.append(row)
        return row

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self._spans)[-max(1, int(n)):][::-1]

    def summary(self) -> str:
        rows = self.recent(MAX_SPANS)
        if not rows:
            return "No turns timed yet."
        last = rows[0]

        def part(label, key):
            v = last.get(key)
            return f"{label} {v / 1000:.1f}s" if isinstance(v, (int, float)) else None
        bits = [b for b in (part("stt", "stt_ms"), part("first word", "first_token_ms"), part("total", "total_ms")) if b]
        totals = [r["total_ms"] for r in rows if isinstance(r.get("total_ms"), (int, float))]
        tail = f" (median total {statistics.median(totals) / 1000:.1f}s over {len(totals)} turns)" if len(totals) > 2 else ""
        return "Last turn: " + ", ".join(bits) + tail
