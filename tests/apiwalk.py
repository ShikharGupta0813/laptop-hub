"""Helpers for tests that walk every route of the real app (FastAPI keeps included routers nested)."""
from __future__ import annotations


def flat_routes(app_or_router, prefix: str = ""):
    """Yield (path, methods, endpoint) for every route, through nested included routers."""
    for route in app_or_router.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            extra = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from flat_routes(inner, prefix + extra)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        yield prefix + path, set(getattr(route, "methods", None) or {"WS"}), getattr(route, "endpoint", None)
