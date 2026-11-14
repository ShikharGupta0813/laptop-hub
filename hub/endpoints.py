"""AI ENDPOINTS: the owner's list of OpenAI-compatible APIs (OpenAI, OpenRouter, LM Studio, vLLM, Groq, ...).
See docs/api/endpoints.md.

Each entry: label, enabled, base URL (".../v1"), model, optional key (OS keyring only), `allow_private` (the owner's
own LM Studio / vLLM on this laptop or tailnet). One endpoint can be the "cheap draft" provider: the routine paths
(Autopilot, Posts, Research summaries, application drafts, alert drafts) pick it through `autopilot.pick_provider`
as the model `endpoint:<id>`. The main assistant routing is NOT changed: this is only one more option, set here.
Requests go through the same privacy wrapper as every cloud call (redaction + usage count, see hub/redact.py).

`OpenAICompatClient` is also what the vision list uses for its "openai-compatible" kind (images as data: URLs).
Safety: public endpoints go through the SSRF guard from hub/research.py; private addresses only when the entry says
`allow_private`; redirects are never followed; the key is only ever sent as `Authorization: Bearer`, never logged.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from . import provlist
from .common import Ctx
from .llm import LLMError
from .provlist import ListError, clean

KINDS = ("openai-compatible",)
TIMEOUT = httpx.Timeout(10.0, read=120.0)
MAX_TOKENS = 1500
CHOICE_KEY = "cheap"


def check_settings(kind: str, raw: dict) -> dict:
    base = provlist.check_url(raw.get("base_url"), "base URL").rstrip("/")
    model = clean(raw.get("model"), 120)
    if not model:
        raise ListError("Write the model name (for example gpt-4o-mini).")
    return {"base_url": base, "model": model, "allow_private": raw.get("allow_private") is True}


def seed() -> list[dict]:
    return []          # nothing is ready-made: an endpoint needs the owner's own URL


async def guard(url: str, allow_private: bool) -> None:
    """Public addresses only unless the owner allowed a private one. Raises LLMError."""
    from .research import BlockedURL, check_url
    if allow_private:
        return
    try:
        await check_url(url)
    except BlockedURL as exc:
        raise LLMError(f"That address is not allowed ({exc}). Tick 'allow a private address' for your own server.") from None


def _problem(status: int, text: str = "") -> LLMError:
    if status in (401, 403):
        return LLMError("The endpoint refused the key. Check it in Settings > Providers.")
    if status == 404:
        return LLMError("The endpoint did not know that URL or model. Check the base URL and model name.")
    if status == 429:
        return LLMError("The endpoint says the limit is reached. Try again later.")
    if status == 413 or status == 400:
        return LLMError("The endpoint did not accept that request (too big or the model has no image support).")
    return LLMError(f"The endpoint failed ({status}).")


class OpenAICompatClient:
    """chat/completions with an optional image. `key` is a string or a callable returning one."""

    name = "endpoint"

    def __init__(self, base_url: str, model: str, key: Any = "", *, allow_private: bool = False, transport=None):
        self.base_url, self.model, self._key = base_url.rstrip("/"), model, key
        self.allow_private, self._transport = allow_private, transport

    def key(self) -> str:
        return (self._key() if callable(self._key) else self._key) or ""

    def has_key(self) -> bool:
        return True            # a key is optional (local servers); a refused key is reported on the call

    async def _post(self, messages: list, json_mode: bool = False) -> str:
        url = f"{self.base_url}/chat/completions"
        await guard(url, self.allow_private)
        body: dict = {"model": self.model, "messages": messages, "stream": False, "max_tokens": MAX_TOKENS}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.key():
            headers["Authorization"] = f"Bearer {self.key()}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport, follow_redirects=False) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"The endpoint could not be reached ({type(exc).__name__}).") from None
        if resp.status_code >= 400:
            raise _problem(resp.status_code)
        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("The endpoint sent an answer that could not be read.") from None
        if isinstance(text, list):                       # some servers answer with content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return str(text or "").strip()

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        return await self._post([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode)

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        yield await self.complete(system, user)

    async def ask_image(self, system: str, question: str, image: bytes, mime: str = "image/jpeg") -> str:
        data = base64.b64encode(image).decode("ascii")
        return await self._post([
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": question},
                                         {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]}])


# ---- the list ---------------------------------------------------------------------------------------------------

def list_for(config, secrets: Any = None) -> provlist.ProviderList:
    return provlist.ProviderList("endpoint", config.data_path() / "ai_endpoints.json", KINDS, seed, check_settings, secrets)


def _choice_file(config):
    from .common import JsonFile
    return JsonFile(config.data_path() / "ai_endpoints_choice.json")


def chosen_id(config) -> str:
    data = _choice_file(config).load({})
    return data.get(CHOICE_KEY, "") if isinstance(data, dict) and isinstance(data.get(CHOICE_KEY), str) else ""


def cheap_choice(config, secrets: Any = None) -> str:
    """'endpoint:<id>' when the owner picked an enabled endpoint for routine drafts, else ''."""
    ident = chosen_id(config)
    if not ident:
        return ""
    try:
        entry = list_for(config, secrets).get(ident)
    except Exception:  # noqa: BLE001 - a broken file means "none chosen"
        return ""
    return f"endpoint:{ident}" if entry and entry["enabled"] else ""


def client_for(config, ident: str, secrets: Any = None, transport=None) -> OpenAICompatClient | None:
    store = list_for(config, secrets)
    entry = store.get(ident)
    if entry is None or not entry["enabled"]:
        return None
    s = entry["settings"]
    return OpenAICompatClient(s["base_url"], s["model"], lambda: store.secret(ident),
                              allow_private=s.get("allow_private", False), transport=transport)


# ---- routes -----------------------------------------------------------------------------------------------------

TINY = "Reply with the single word: ok"


def make_router(ctx: Ctx, *, secrets: Any = None, transport=None) -> APIRouter:
    router = APIRouter(prefix="/api/ai-endpoints", dependencies=[Depends(ctx.need_auth)])
    store = list_for(ctx.config, secrets)
    fields = [{"key": "base_url", "label": "Base URL", "type": "url", "placeholder": "https://api.openai.com/v1", "required": True},
              {"key": "model", "label": "Model", "type": "text", "placeholder": "gpt-4o-mini", "required": True},
              {"key": "allow_private", "label": "Allow a private address (my own server)", "type": "bool"}]

    def guarded(fn, *args):
        try:
            return fn(*args)
        except ListError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    def snapshot():
        return {"entries": store.public(), "chosen": chosen_id(ctx.config),
                "kinds": {"openai-compatible": {"label": "OpenAI-compatible API", "fields": fields, "secret": "API key (optional for local servers)"}},
                "presets": [{"label": "OpenAI", "kind": "openai-compatible", "settings": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"}},
                            {"label": "OpenRouter", "kind": "openai-compatible", "settings": {"base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini"}},
                            {"label": "LM Studio (this laptop)", "kind": "openai-compatible",
                             "settings": {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "allow_private": True}}],
                "note": "Used only for routine drafts when you choose it below. The main assistant is unchanged."}

    @router.get("")
    def listing():
        return snapshot()

    @router.post("")
    def add(body: dict = Body(...)):
        entry = guarded(store.add, body)
        ctx.audit("endpoint_add", id=entry["id"], kind=entry["kind"])
        return {"ok": True, "entry": entry}

    @router.put("/choice")
    def choose(body: dict = Body(...)):
        ident = body.get("id") or ""
        if ident and store.get(ident) is None:
            raise HTTPException(404, "No such endpoint.")
        _choice_file(ctx.config).save({CHOICE_KEY: ident})
        ctx.audit("endpoint_choice", id=ident)
        return {"ok": True, "chosen": ident}

    @router.put("/order")
    def order(body: dict = Body(...)):
        return {"entries": guarded(store.reorder, body.get("ids"))}

    @router.put("/{ident}")
    def edit(ident: str, body: dict = Body(...)):
        return {"ok": True, "entry": guarded(store.update, ident, body)}

    @router.delete("/{ident}")
    def remove(ident: str):
        guarded(store.delete, ident)
        if chosen_id(ctx.config) == ident:
            _choice_file(ctx.config).save({CHOICE_KEY: ""})
        ctx.audit("endpoint_delete", id=ident)
        return {"ok": True}

    @router.post("/{ident}/test")
    async def test(ident: str):
        client = client_for(ctx.config, ident, secrets, transport)
        if client is None:
            raise HTTPException(404, "No such endpoint, or it is switched off.")
        try:
            reply = await asyncio.wait_for(client.complete("You are a connection test.", TINY), 60)
        except (LLMError, asyncio.TimeoutError) as exc:
            return {"ok": False, "error": str(exc) or "The endpoint took too long."}
        return {"ok": True, "reply": clean(reply, 80)}

    return router


def build_guarded(config, audit: Callable[..., None], ident: str, secrets: Any = None):
    """For hub/server.py: the endpoint client wrapped with redaction and usage counting, or None."""
    from . import redact
    client = client_for(config, ident, secrets)
    return redact.Guarded(client, config, audit, count_as="endpoint") if client else None

