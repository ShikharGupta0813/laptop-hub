"""The owner's own rules: extra safety rules and extra redaction patterns. See docs/api/assistant-safety.md and docs/api/privacy-usage.md.

Both are short lists the owner edits in Settings. The ready-made behaviour (hub/safety.py classify, hub/redact.py kinds)
stays exactly as it is; these lists only add to it:

- safety rules  {pattern, regex, applies_to: command|tool|path, level: N2|N3, note, enabled}. A rule can only RAISE a
  level. "Treat anything touching D:\\payroll as critical" is one rule. Firing is written to the audit log (rule ids
  only, never the command).
- redaction rules  {label, pattern, regex, kind, enabled}. Words or a pattern that cloud AI must never see; they are
  swapped for [KIND_1] like the built-in kinds and put back in the reply. Audit rows hold counts only.

Patterns are checked by hub/safepattern.py (length limit, no runaway shapes, bounded matching).

Routes (login needed):
  GET/PUT  /api/safety/rules              {rules: [...]}  (PUT replaces the whole list)
  POST     /api/safety/rules/test         {kind: command|tool|path, text, rules?}  -> {level, built_in, rules, reason}
  GET/PUT  /api/redact/rules              {rules: [...]}
  POST     /api/redact/rules/test         {text, rules?}  -> {redacted, counts, total}
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from . import redact, safety
from .common import Ctx

MAX_TEST_CHARS = 20_000


def make_router(ctx: Ctx) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(ctx.need_auth)])
    config = ctx.config

    def rules_body(body: dict) -> list:
        rules = body.get("rules") if isinstance(body, dict) else None
        if not isinstance(rules, list):
            raise HTTPException(400, 'Send {"rules": [...]}.')
        return rules

    # ---- safety ----------------------------------------------------------------------------------------------
    def safety_view() -> dict:
        return {"rules": safety.load_custom_rules(config), "applies_to": list(safety.CUSTOM_APPLIES),
                "levels": list(safety.CUSTOM_LEVELS), "max": safety.CUSTOM_MAX_RULES}

    @router.get("/safety/rules")
    def get_safety_rules():
        return safety_view()

    @router.put("/safety/rules")
    def put_safety_rules(body: dict = Body(default_factory=dict)):
        try:
            saved = safety.save_custom_rules(config, rules_body(body))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(500, "The rules could not be saved.") from None
        ctx.audit("safety_rules_update", count=len(saved), critical=sum(1 for r in saved if r["level"] == "N3"))
        return safety_view()

    @router.post("/safety/rules/test")
    def test_safety(body: dict = Body(default_factory=dict)):
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, 'Send {"kind": "command", "text": "..."}.')
        draft = body.get("rules")
        if draft is not None and not isinstance(draft, list):
            raise HTTPException(400, "rules must be a list.")
        rules = None
        if draft is not None:
            try:
                rules = safety.clean_custom_rules(draft)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        else:
            rules = safety.load_custom_rules(config)
        return safety.try_custom_input(str(body.get("kind") or "command"), text, rules)

    # ---- redaction -------------------------------------------------------------------------------------------
    def redact_view() -> dict:
        return {"rules": redact.load_rules(config), "max": redact.MAX_CUSTOM_RULES,
                "builtin_kinds": list(redact.KINDS)}

    @router.get("/redact/rules")
    def get_redact_rules():
        return redact_view()

    @router.put("/redact/rules")
    def put_redact_rules(body: dict = Body(default_factory=dict)):
        try:
            saved = redact.save_rules(config, rules_body(body))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(500, "The rules could not be saved.") from None
        ctx.audit("redact_rules_update", count=len(saved))      # how many, never the words
        return redact_view()

    @router.post("/redact/rules/test")
    def test_redact(body: dict = Body(default_factory=dict)):
        """Preview with the saved rules, or with an unsaved draft list in `rules`."""
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, 'Send {"text": "..."}.')
        if len(text) > MAX_TEST_CHARS:
            raise HTTPException(413, f"Keep the preview text under {MAX_TEST_CHARS} characters.")
        draft = body.get("rules")
        if draft is None:
            return redact.preview(config, text)
        try:
            custom = redact.compile_rules(redact.clean_rules(draft))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        rd = redact.Redactor(redact.KINDS, getattr(config, "redact_names", None) or (), custom)
        out = rd.redact(text)
        counts = rd.summary()
        return {"enabled": True, "redacted": out, "counts": counts, "total": sum(counts.values())}

    return router

