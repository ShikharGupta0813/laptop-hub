"""Privacy and usage routes: GET /api/usage and POST /api/redact/preview. See docs/api/privacy-usage.md."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from . import redact, usage
from .common import Ctx

MAX_PREVIEW_CHARS = 20_000


def make_router(ctx: Ctx) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(ctx.need_auth)])
    config = ctx.config

    @router.get("/usage")
    def get_usage():
        return usage.report(config)

    @router.post("/redact/preview")
    def redact_preview(body: dict = Body(...)):
        """What would be hidden in this text? Nothing is stored or audited with values."""
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise HTTPException(400, "Send {\"text\": \"...\"}.")
        if len(text) > MAX_PREVIEW_CHARS:
            raise HTTPException(413, f"Keep the preview text under {MAX_PREVIEW_CHARS} characters.")
        return redact.preview(config, text)

    return router
