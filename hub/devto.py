"""The old /api/devto/* routes, kept as thin shims over hub/posts.py so nothing that still calls them breaks.

The real feature is Posts with many destinations (hub/posts.py, hub/destinations.py, docs/api/destinations.md). Every
route here forwards to the same engine and works on the dev.to destination only. New code should use /api/posts and
/api/destinations.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends

from . import posts
from .common import Ctx
from .destinations import WARNING, DestinationError

DevtoError = DestinationError
Devto = posts.Posts
ENGINES = posts.ENGINES
engine_for = posts.engine_for
build = posts.build                      # the assistant tools are the generic post_* tools now
KEY_RE = re.compile(r"^\S{8,200}$")


def make_router(ctx: Ctx, **kwargs) -> APIRouter:
    engine = posts.engine_for(ctx, **kwargs)
    guarded = posts.guard_routes(engine)
    dests = engine.dests
    router = APIRouter(prefix="/api/devto", dependencies=[Depends(ctx.need_auth)])

    def dest() -> dict:
        d = dests.first_of_kind("devto")
        if d is None:
            raise DestinationError("There is no dev.to destination. Add one in Posts > Destinations.", 404)
        return d

    def legacy(view: dict) -> dict:
        """The old draft shape: one status, devto_id, devto_url (from the dev.to destination only)."""
        d = dests.first_of_kind("devto")
        t = (view.get("targets") or {}).get(d["id"], {}) if d else {}
        out = dict(view)
        out["status"] = t.get("status") if t.get("status") in ("saved", "published") else "local"
        out["devto_id"], out["devto_url"] = t.get("remote_id"), t.get("url", "")
        return out

    @router.get("/status")
    @guarded
    async def status():
        d = engine.load()
        dv = dests.first_of_kind("devto")
        return {"enabled": True, "connected": bool(dv and dests.connected(dv)), "drafts": len(d["drafts"]), "ideas": len(d["ideas"]),
                "checked_at": d["stats"].get("at"), "days_since_post": engine.last_post_days(), "warning": WARNING,
                "new_comments": sum(1 for c in d["comments"]["items"] if c.get("new"))}

    @router.put("/key")
    @guarded
    async def set_key(body: dict = Body(...)):
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not KEY_RE.match(key.strip()):
            raise DestinationError("Paste the API key from dev.to/settings/extensions (no spaces).", 400)
        dv = dests.first_of_kind("devto") or dests.create({"kind": "devto", "label": "dev.to"})
        dests.set_secret(dv["id"], key.strip())
        ctx.audit("destination_secret_set", id=dv["id"])
        return {"connected": dests.connected(dests.get(dv["id"]))}

    @router.delete("/key")
    @guarded
    async def delete_key():
        dv = dests.first_of_kind("devto")
        if dv:
            dests.clear_secret(dv["id"])
            ctx.audit("destination_secret_removed", id=dv["id"])
        return {"connected": bool(dv and dests.connected(dv))}

    @router.post("/test")
    @guarded
    async def test():
        out = await dests.test(dest()["id"])
        return {"ok": True, "username": out.get("name", "")}

    @router.get("/settings")
    @guarded
    async def get_settings():
        return {**engine.settings_view(), "connected": bool(dests.first_of_kind("devto") and dests.connected(dests.first_of_kind("devto")))}

    @router.patch("/settings")
    @guarded
    async def patch_settings(body: Any = Body(...)):
        return engine.patch_settings(body)

    @router.get("/ideas")
    @guarded
    async def ideas():
        d = engine.load()
        return {"ideas": d["ideas"], "at": d.get("ideas_at"), "via": d.get("ideas_via"), "warning": WARNING}

    @router.post("/ideas/refresh")
    @guarded
    async def ideas_refresh():
        out = await engine.make_ideas()
        ctx.audit("posts_ideas", n=len(out["ideas"]), via=out["via"])
        return out

    @router.get("/drafts")
    @guarded
    async def drafts():
        rows = sorted(engine.load()["drafts"].values(), key=lambda x: x["updated"], reverse=True)
        return {"drafts": [legacy(engine.view(x)) for x in rows], "warning": WARNING}

    @router.post("/drafts")
    @guarded
    async def create_draft(body: dict = Body(...)):
        if not isinstance(body, dict):
            raise DestinationError("Send the topic or the text of the draft.", 400)
        out = await engine.new_draft(topic=str(body.get("topic") or ""), idea_id=str(body.get("idea_id") or ""),
                                     title=str(body.get("title") or ""), body=str(body.get("body") or ""))
        ctx.audit("posts_draft", id=out["id"], by=out["generated_by"])
        return legacy(out)

    @router.get("/drafts/{draft_id}")
    @guarded
    async def one(draft_id: str):
        return legacy(engine.view(engine.get(draft_id)))

    @router.patch("/drafts/{draft_id}")
    @guarded
    async def patch_draft(draft_id: str, body: dict = Body(...)):
        return legacy(engine.update(draft_id, body if isinstance(body, dict) else {}))

    @router.delete("/drafts/{draft_id}")
    @guarded
    async def delete_draft(draft_id: str):
        engine.delete(draft_id)
        return {"ok": True}

    @router.post("/drafts/{draft_id}/save")
    @guarded
    async def save_remote(draft_id: str):
        out = await engine.save_remote(draft_id, dest()["id"])
        ctx.audit("post_save_draft", id=draft_id, destination=dest()["id"])
        return legacy(out)

    @router.post("/drafts/{draft_id}/publish")
    @guarded
    async def publish(draft_id: str, body: dict = Body(default={})):
        out = await engine.publish(draft_id, dest()["id"], (body or {}).get("confirm") is True)
        ctx.audit("post_publish", id=draft_id, destination=dest()["id"])
        return legacy(out)

    @router.get("/drafts/{draft_id}/pack")
    @guarded
    async def pack(draft_id: str):
        return engine.legacy_pack(draft_id)

    @router.get("/polish")
    @guarded
    async def polish():
        return await engine.polish()

    @router.get("/published")
    @guarded
    async def published():
        d = engine.load()
        dv = dests.first_of_kind("devto")
        return {"articles": [a for a in d["stats"].get("articles", []) if dv and a.get("dest") == dv["id"]],
                "checked_at": d["stats"].get("at"), "comments": [c for c in d["comments"]["items"] if dv and c.get("dest") == dv["id"]],
                "connected": bool(dv and dests.connected(dv))}

    @router.post("/refresh")
    @guarded
    async def refresh():
        dv = dest()
        if not dests.connected(dv):
            raise DestinationError("Connect your dev.to API key first (Posts > Destinations).", 409)
        out = await engine.refresh(notify=False, dest_id=dv["id"])
        if out["errors"]:
            raise DestinationError(out["errors"][0]["error"], 502)
        d = engine.load()
        return {**out, "articles": [a for a in out["articles"] if a.get("dest") == dv["id"]],
                "comments": [c for c in d["comments"]["items"] if c.get("dest") == dv["id"]], "checked_at": d["stats"].get("at")}

    @router.post("/comments/ack")
    @guarded
    async def ack():
        with engine.lock:
            d = engine.load()
            for c in d["comments"]["items"]:
                c["new"] = False
            engine.save(d)
        return {"ok": True}

    return router
