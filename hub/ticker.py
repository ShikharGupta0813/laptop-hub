"""One lightweight poll for everything the app shell used to ask for separately.

GET /api/ticker (login needed) answers {panic, unread, alerts, speech, meeting} by calling the same read-only handlers
the individual routes use (/api/panic, /api/notifications/unread-count, /api/alerts/pending, /api/presence/pending,
/api/meetings/status), so the answers can never drift. A module that is not installed leaves its key out. The reply
carries an ETag: a client that sends it back in If-None-Match gets an empty 304 while nothing changed.
Nothing here sends or applies anything; acknowledging spoken items stays on the original routes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

# key -> (path, how to shape the handler's answer)
SOURCES = {
    "panic": ("/api/panic", lambda r: r),
    "unread": ("/api/notifications/unread-count", lambda r: int((r or {}).get("count") or 0)),
    "alerts": ("/api/alerts/pending", lambda r: (r or {}).get("alerts") or []),
    "speech": ("/api/presence/pending", lambda r: (r or {}).get("speech") or []),
    "meeting": ("/api/meetings/status", lambda r: r),
}


def make_router(ctx):
    router = APIRouter(prefix="/api/ticker", dependencies=[Depends(ctx.need_auth)])

    def walk(node, prefix=""):
        # FastAPI keeps included routers nested; flatten them to (full path, methods, endpoint).
        for route in getattr(node, "routes", []):
            inner = getattr(route, "original_router", None)
            if inner is not None:
                extra = getattr(getattr(route, "include_context", None), "prefix", "") or ""
                yield from walk(inner, prefix + extra)
                continue
            path = getattr(route, "path", None)
            if path is not None:
                yield prefix + path, set(getattr(route, "methods", None) or ()), getattr(route, "endpoint", None)

    table: dict[str, Any] = {}

    def handler_for(path: str):
        if not table:
            for full, methods, endpoint in walk(ctx.get_app()):
                if "GET" in methods and endpoint is not None:
                    table.setdefault(full, endpoint)
        return table.get(path)

    @router.get("")
    def ticker(request: Request, response: Response) -> Any:
        out: dict[str, Any] = {}
        for key, (path, shape) in SOURCES.items():
            fn = handler_for(path)
            if fn is None:
                continue
            try:
                out[key] = shape(fn())
            except Exception:  # noqa: BLE001  one module being unwell must not blank the rest
                continue
        etag = '"' + hashlib.sha1(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()[:16] + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-store"
        return out

    return router
