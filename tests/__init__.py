"""Test package setup: keep the test run off the real network.

`create_app` starts a background warm-up that opens a real connection to Google when a Gemini key is saved on the
machine (DNS + TLS on every app start, and the exit waits for it). Tests that really want the warm-up give the client a
fake transport; every other client answers "not warmed" at once.
"""
from hub import llm as _llm

_real_warm = _llm.GeminiClient.warm


async def _warm_offline(self) -> bool:
    if self._transport is None:
        return False
    return await _real_warm(self)


_llm.GeminiClient.warm = _warm_offline
