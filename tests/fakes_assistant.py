"""Fakes for the assistant tests: a scripted Claude client and a scripted Ollama transport."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(call_id, name, args):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def reply(*blocks, stop=None, deltas=None, details=None):
    """One scripted model response. Text blocks are also streamed as text events."""
    blocks = [text_block(b) if isinstance(b, str) else b for b in blocks]
    if stop is None:
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    if deltas is None:
        deltas = [b.text for b in blocks if b.type == "text"]
    message = SimpleNamespace(content=blocks, stop_reason=stop, stop_details=details)
    return {"message": message, "deltas": deltas}


class _Stream:
    def __init__(self, item):
        self.item = item

    async def __aenter__(self):
        if isinstance(self.item, BaseException):
            raise self.item
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for piece in self.item["deltas"]:
            yield SimpleNamespace(type="text", text=piece)
        if self.item.get("raise_after"):
            raise self.item["raise_after"]

    async def get_final_message(self):
        return self.item["message"]


class FakeClaude:
    """Looks like AsyncAnthropic for `client.beta.messages.stream(**params)`.

    `script` items are reply() dicts, exceptions, or callables(params) -> one of those.
    When the script runs out, `default` (if set) is used for every further call.
    """

    def __init__(self, script=(), default=None):
        self.script = list(script)
        self.default = default
        self.calls = []
        self.beta = SimpleNamespace(messages=self)

    def stream(self, **params):
        self.calls.append({**params, "messages": list(params["messages"])})
        if self.script:
            item = self.script.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("FakeClaude ran out of scripted replies")
        if callable(item):
            item = item(params)
        return _Stream(item)


class FakeOllama:
    """An httpx MockTransport answering /api/chat with scripted NDJSON streams.

    `script` items are lists of chunk dicts (one NDJSON line each), or (status, text) tuples.
    """

    def __init__(self, script):
        self.script = list(script)
        self.bodies = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat", request.url.path
        self.bodies.append(json.loads(request.content))
        item = self.script.pop(0)
        if isinstance(item, tuple):
            return httpx.Response(item[0], text=item[1])
        if isinstance(item, Exception):
            raise item
        lines = "".join(json.dumps(c) + "\n" for c in item)
        return httpx.Response(200, text=lines)


def ollama_text(*pieces):
    chunks = [{"message": {"role": "assistant", "content": p}, "done": False} for p in pieces]
    return chunks + [{"message": {"role": "assistant", "content": ""}, "done": True}]


def ollama_calls(*calls, text=""):
    return [{"message": {"role": "assistant", "content": text,
                         "tool_calls": [{"function": c} for c in calls]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True}]
