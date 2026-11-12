"""The "duo" provider: Claude (your `claude` login) supervises, Gemini does the work.

Claude is asked for a short plan once per non-trivial assistant request (and, if `duo_verify` is on, which is the default, for a short
check that the work was really done, only after tools ran). Gemini runs every tool call and writes every answer. Everything else (notes,
cards, briefs) goes straight to Gemini. The Claude subscription quota is small, so Claude calls are capped per
hour, and any Claude failure just pauses the supervisor for a while: the request carries on with Gemini alone.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Callable

from .llm import LLMError

CLAUDE_TIMEOUT = 25.0
COOLDOWNS = {"usage limit": 30 * 60, "not logged in": 10 * 60, "Claude not installed": 10 * 60,
             "timeout": 5 * 60, "error": 5 * 60}

PLAN_SYSTEM = (
    "You are the supervisor of a smaller assistant that runs tools on the owner's laptop. Write a compact plan "
    "for the assistant to follow. At most 120 words and at most 6 numbered steps. Say which of the listed tools "
    "to use, what to avoid, and flag anything that deserves the owner's approval or could destroy data. "
    "No preamble and no code fences. You cannot run anything yourself.")
VERIFY_SYSTEM = (
    "You check the work of a smaller assistant that ran tools. Was every part of the owner's request done, and "
    "do the tool results show it succeeded? Reply with exactly OK if so. Otherwise reply with PROBLEM: followed "
    "by at most 80 words saying what is missing or wrong and what to do. Only report real problems (a part of "
    "the request not done, a failed or missing tool result, an unsupported claim, or something unsafe); "
    "never style.")


def _kind(exc: Exception) -> str:
    low = str(exc).lower()
    if "usage limit" in low or "rate limit" in low or "quota" in low:
        return "usage limit"
    if "isn't logged in" in low or "not logged in" in low or "sign in" in low:
        return "not logged in"
    if "install claude" in low or "couldn't start claude" in low:
        return "Claude not installed"
    if "too long" in low or isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    return "error"


class Supervisor:
    """Rations and guards the Claude calls. `ask` returns text, or None when Claude is paused or failed."""

    def __init__(self, claude, max_per_hour: Callable[[], int] | int = 20, verify: Callable[[], bool] | bool = True,
                 clock: Callable[[], float] = time.monotonic, timeout: float = CLAUDE_TIMEOUT):
        self.claude = claude
        self._max = max_per_hour if callable(max_per_hour) else (lambda: max_per_hour)
        self._verify = verify if callable(verify) else (lambda: verify)
        self._clock = clock
        self.timeout = timeout
        self._calls: list[float] = []
        self._paused_until = 0.0
        self._reason = ""

    @property
    def verify_enabled(self) -> bool:
        return bool(self._verify())

    def _recent(self) -> list[float]:
        now = self._clock()
        self._calls = [t for t in self._calls if now - t < 3600]
        return self._calls

    def paused_reason(self) -> str:
        """Why Claude won't be asked right now, or "" when it will."""
        if self._clock() < self._paused_until:
            return self._reason
        cap = int(self._max() or 0)
        if cap <= 0:
            return "switched off (duo_max_claude_per_hour is 0)"
        if len(self._recent()) >= cap:
            return f"hourly cap reached ({cap} per hour)"
        return ""

    def status(self) -> dict:
        reason = self.paused_reason()
        return {"paused": bool(reason), "reason": reason,
                "note": f"Claude supervisor paused: {reason}" if reason else "",
                "calls_last_hour": len(self._recent()), "max_per_hour": int(self._max() or 0),
                "verify": self.verify_enabled}

    async def ask(self, system: str, user: str) -> str | None:
        if self.paused_reason():
            return None
        self._recent().append(self._clock())     # failed calls count too: they cost quota as well
        try:
            text = await asyncio.wait_for(self.claude.complete(system, user), self.timeout)
        except Exception as exc:  # noqa: BLE001  Claude must never break the owner's request
            if isinstance(exc, asyncio.CancelledError):
                raise
            kind = _kind(exc)
            self._paused_until = self._clock() + COOLDOWNS.get(kind, COOLDOWNS["error"])
            self._reason = kind
            return None
        return (text or "").strip() or None

    async def plan(self, request: str, tool_names: list[str], context: str = "") -> str | None:
        user = (f"Owner's request:\n{request[:1500]}\n\n"
                + (f"Earlier in this chat:\n{context[:600]}\n\n" if context else "")
                + "Tools the assistant has: " + ", ".join(tool_names) + "\n\nWrite the plan.")
        text = await self.ask(PLAN_SYSTEM, user)
        return text[:1200] if text else None

    async def review(self, request: str, steps: str, answer: str) -> str | None:
        """None when the answer is fine (or Claude is unavailable); else what to fix."""
        user = (f"Owner's request:\n{request[:1000]}\n\nTools the assistant ran and their results:\n{steps[:1500] or 'none'}"
                f"\n\nAssistant's final answer:\n{answer[:1500]}")
        text = await self.ask(VERIFY_SYSTEM, user)
        if not text or not text.upper().startswith("PROBLEM"):
            return None
        return text.split(":", 1)[1].strip()[:600] if ":" in text else text[:600]


class DuoClient:
    """complete()/stream() go straight to Gemini. The assistant reads `.gemini` and `.supervisor` for the rest."""

    name = "duo"

    def __init__(self, gemini, supervisor: Supervisor):
        self.gemini = gemini
        self.supervisor = supervisor

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        return await self.gemini.complete(system, user, json_mode)

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        async for piece in self.gemini.stream(system, user):
            yield piece
