"""FIX-IT: what is not set up, which AI answers, and live logs, for the Friday and Jarvis screens. See docs/api/fixit.md.

Routes (login needed), all read-only except the provider test (one tiny question to ONE provider, nothing is sent
anywhere else, nothing is saved):
    GET  /api/fixit/providers              every provider: ready?, why, the exact fix, the Test button's endpoint
    POST /api/fixit/providers/{id}/test    ask that provider (not the chain) one tiny question
    GET  /api/fixit/checklist              things the laptop side is missing (Jarvis folder, venv, patch, AI, voice engine)
    GET  /api/fixit/log?source=&lines=     jarvis (jarvis.log), output (jarvis-output.log) or hub (the hub console)
    GET  /api/fixit/doctor                 the `python -m hub doctor` essentials as lines

No secrets: only provider names, reasons, file names and the owner's own log lines (keys are masked).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from . import cliprov, consolelog, providers, provchain
from .common import Ctx
from .jarvis import INBOX_SCRIPT, LOG_NAME, OUTPUT_FILE, SCRIPT, default_command, tail_lines
from .llm import LLMError

MAX_LINES = 500
SOURCES = ("jarvis", "output", "hub")
TINY = "Reply with the single word: ok"


async def ollama_models(config) -> list[str] | None:
    """Model names when Ollama answers within 2 seconds, else None."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(config.ollama_host.rstrip("/") + "/api/tags")
            resp.raise_for_status()
            return [m.get("model", "") for m in resp.json().get("models", [])]
    except Exception:  # noqa: BLE001 - down, slow or garbage: "not running"
        return None


def jarvis_items(config) -> list[dict]:
    raw = (config.jarvis_dir or "").strip()
    items = []

    def add(ident, label, ok, detail, step="", action=None):
        items.append({"id": ident, "label": label, "ok": ok, "detail": detail, "step": "" if ok else step,
                      "action": None if ok else action})

    folder = Path(raw) if raw else None
    add("jarvis-dir", "Jarvis folder is set", bool(raw), raw or "jarvis_dir is empty.",
        "Open hub.json on the laptop and set \"jarvis_dir\" to your jarvis-voice-assistant folder, then restart the hub.",
        {"kind": "settings", "label": "Open settings", "target": "settings"})
    if not raw:
        return items
    exists = bool(folder and folder.is_dir())
    add("jarvis-exists", "Jarvis folder exists", exists, raw if exists else f"{raw} was not found.",
        f"Fix \"jarvis_dir\" in hub.json: {raw} does not exist.")
    if not exists:
        return items
    has_script = (folder / SCRIPT).is_file()
    add("jarvis-script", f"{SCRIPT} is in the folder", has_script, SCRIPT if has_script else f"No {SCRIPT} in {raw}.",
        f"Point jarvis_dir at the folder that contains {SCRIPT} (the jarvis-voice-assistant repo).")
    has_venv = default_command(folder) is not None
    add("jarvis-venv", "Jarvis's own Python (.venv) exists", has_venv, ".venv found." if has_venv else "No .venv in the folder.",
        f"In {raw}: python -m venv .venv  then  .venv\\Scripts\\pip install -r requirements.txt  (as Jarvis's README says).")
    has_inbox = (folder / INBOX_SCRIPT).is_file()
    add("jarvis-inbox", "Typing to Jarvis from the phone is installed", has_inbox,
        "remote_inbox.py found." if has_inbox else f"No {INBOX_SCRIPT} next to {SCRIPT}.",
        "Follow docs/jarvis-remote-inbox.md in the laptop-hub folder (adds remote_inbox.py and 4 lines to jarvis.py). "
        "Until then use Talk > Jarvis in the app: it answers without the separate Jarvis program.",
        {"kind": "open", "label": "Talk to Jarvis here", "target": "assistant"})
    if sys.platform != "win32":
        add("jarvis-os", "Laptop can start Jarvis", False, "Jarvis can only be started from a Windows laptop.",
            "Run the hub on the Windows laptop that has Jarvis.")
    return items


async def checklist(config, tts_engine: str, tts_enabled: bool, probe=None) -> list[dict]:
    up = await (probe or ollama_models)(config)
    rows = provchain.diagnose(config, up is not None)
    ready = [r for r in rows if r["ready"] and (r["id"] != "local" or up is not None)]
    items = [{"id": "provider", "label": "At least one AI can answer", "ok": bool(ready),
              "detail": ("Ready: " + ", ".join(r["label"] for r in ready)) if ready else "No AI is ready.",
              "step": "" if ready else "Start the Ollama app (free, local), or run `gemini` once in a terminal and choose Login with Google, "
                                       "or run `python -m hub setup --only ai`. Then press Test in Provider check.",
              "action": None if ready else {"kind": "scroll", "label": "Open Provider check", "target": "provider-check"}}]
    items += jarvis_items(config)
    engine_ok = tts_enabled
    items.append({"id": "voice-engine", "label": "Voice engine", "ok": True,
                  "detail": f"{tts_engine}: " + ("the laptop makes the audio and the phone plays it." if tts_engine != "browser"
                                                else "the phone speaks with its own voice (needs a voice in the phone's settings)."),
                  "step": "", "action": None})
    if not engine_ok:
        items[-1].update(ok=False, detail="Spoken replies are switched off in Settings > Voices.",
                         step="Settings > Voices: switch spoken replies on.",
                         action={"kind": "settings", "label": "Open settings", "target": "settings"})
    return items


def make_router(ctx: Ctx, *, probe=None) -> APIRouter:
    router = APIRouter(prefix="/api/fixit", dependencies=[Depends(ctx.need_auth)])
    config = ctx.config
    find_ollama = probe or ollama_models

    def tts_state() -> tuple[str, bool]:
        try:
            from .tts import TTSService
            st = TTSService(config).settings()
            return st["engine"], bool(st["enabled"])
        except Exception:  # noqa: BLE001
            return getattr(config, "tts_engine", "browser") or "browser", True

    @router.get("/providers")
    async def providers_view():
        up = await find_ollama(config)
        rows = provchain.diagnose(config, up is not None)
        for row in rows:
            if row["id"] == "local" and up is None:
                row["ready"], row["reason"] = False, "Ollama isn't running."
                row["fix"], row["command"] = provchain.FIXES["local"][0], provchain.FIXES["local"][1]
        for row in rows:
            row["chosen"] = False
        for row in rows:
            if row["ready"]:
                row["chosen"] = True
                break
        return {"providers": rows, "default": providers.resolve_default(config), "last": provchain.LAST,
                "summary": ("Answering with " + next(r["label"] for r in rows if r["chosen"])) if any(r["chosen"] for r in rows)
                else provchain.failure_message(config, [], up is not None)}

    @router.post("/providers/{ident:path}/test")
    async def provider_test(ident: str):
        raw = getattr(ctx, "raw_model_for", None)
        if raw is None:
            raise HTTPException(501, "This hub cannot test providers one by one.")
        ok, why = provchain.readiness(config, ident)
        if not ok and ident != "local":
            fix, command = provchain.fix_text(config, ident)
            return {"ok": False, "error": why, "fix": fix, "command": command}
        started = time.perf_counter()
        try:
            client = raw(ident)
            reply = await asyncio.wait_for(client.complete("You are a connection test.", TINY), timeout=90)
        except LLMError as exc:
            fix, command = provchain.fix_text(config, ident)
            return {"ok": False, "error": str(exc), "fix": fix, "command": command}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "It took over 90 seconds to answer.", "fix": "Try again, or use another AI.", "command": ""}
        ctx.audit("fixit_provider_test", provider=ident)
        return {"ok": True, "reply": " ".join(str(reply).split())[:80], "latency_ms": round((time.perf_counter() - started) * 1000)}

    @router.get("/checklist")
    async def checklist_view():
        engine, enabled = tts_state()
        items = await checklist(config, engine, enabled, find_ollama)
        return {"items": items, "problems": sum(1 for i in items if i["ok"] is False)}

    def log_path(source: str) -> Path | None:
        if source == "jarvis":
            raw = (config.jarvis_dir or "").strip()
            return Path(raw) / LOG_NAME if raw else None
        if source == "output":
            return config.data_path() / OUTPUT_FILE
        return config.data_path() / consolelog.FILE_NAME

    @router.get("/log")
    def log(source: str = Query("jarvis"), lines: int = Query(120, ge=1)):
        if source not in SOURCES:
            raise HTTPException(400, "source must be jarvis, output or hub.")
        path = log_path(source)
        if path is None:
            return {"source": source, "lines": [], "exists": False,
                    "note": "jarvis_dir is not set in hub.json, so there is no Jarvis log."}
        out = [provchain_mask(line) for line in tail_lines(path, min(lines, MAX_LINES))]
        note = ""
        if not path.is_file():
            note = {"jarvis": "Jarvis has not written its own log yet (start Jarvis).",
                    "output": "Jarvis has not been started by the hub yet.",
                    "hub": "The hub writes this when started with `python -m hub serve`."}[source]
        return {"source": source, "lines": out, "exists": path.is_file(), "note": note, "file": path.name}

    @router.get("/doctor")
    async def doctor():
        up = await find_ollama(config)
        lines: list[dict] = []

        def add(good, text):
            lines.append({"ok": good, "text": text})

        add(True, f"hub running on {config.host}:{config.port}")
        add(up is not None, "Ollama running: " + (", ".join(up) or "no models") if up is not None
            else "Ollama not running. Start the Ollama app (needed for the local model).")
        for r in provchain.diagnose(config, up is not None):
            if r["id"] == "local":
                continue
            add(True if r["ready"] else None, f"{r['label']}: {r['reason']}" + ("" if r["ready"] else f" Fix: {r['fix']}"))
        engine, enabled = tts_state()
        add(enabled, f"voice engine: {engine}" + ("" if enabled else " (spoken replies are off in Settings > Voices)"))
        for i in jarvis_items(config):
            add(i["ok"], f"{i['label']}: {i['detail']}" + ("" if i["ok"] else f" Fix: {i['step']}"))
        add(consolelog.enabled() or None, "hub console log: " + ("on" if consolelog.enabled()
            else "off (start the hub with `python -m hub serve` to fill it)"))
        return {"lines": lines}

    return router


def provchain_mask(line: str) -> str:
    return cliprov.mask(line, 400)
