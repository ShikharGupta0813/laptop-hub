"""The web server: login, files, notes, ask, assistant, quick buttons and the terminal.

API details are in docs/API.md. Every route except login and the static app needs the
session cookie. The server listens on 127.0.0.1 only; Tailscale puts HTTPS in front of it.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import (APIRouter, BackgroundTasks, Body, Depends, FastAPI, File, HTTPException, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assistant_tools, assistant_tools_more, auth, brain_tools, ccsessions, council_tools, files, friday_tools, research, skills_tools, vision
from . import llm as llm_mod
from . import updater as updater_mod
from .assistant import LOCAL_CTX_TOOLS, AssistantService
from .latency import Latency
from .persona import clean_persona, split_persona
from .common import Ctx
from .config import Config
from . import providers as providers_mod
from .router import Router
from .duo import DuoClient, Supervisor
from .llm import (ClaudeClient, ClaudeCliClient, GeminiClient, LLMError, OllamaClient, build_prompt,
                  label_note)
from .notes import NoteError, NoteStore, clean_spoken
from .speech import SpeechError, Transcriber
from .terminal import TerminalError, open_terminal

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
VERSION = "1.0"
# Feature modules, each `hub/<name>.py` (or a package) with make_router(ctx) and optionally start(ctx).
FEATURE_MODULES = ("work", "study", "english", "laptop", "jarvis", "kb", "fileops", "notify", "reminders", "inbox",
                   "screen", "productivity", "life", "automations", "watch", "friday", "workspaces", "cards", "settings", "search", "council", "autopilot", "today", "skills", "research", "brain", "proactive", "vision", "ccsessions")
INTERNAL_HOST = "hub.internal"  # the assistant's in-process calls


@dataclass
class Deps:
    """Everything that talks to the outside world, so tests can swap it."""
    llm: Any = None
    claude: Any = None
    claude_login: Any = None
    gemini: Any = None
    duo: Any = None
    transcriber: Any = None
    label: Callable | None = None
    terminal: Callable = open_terminal
    assistant: Any = None
    clock: Callable[[], float] = time.time
    extra: dict = field(default_factory=dict)


class LoginBody(BaseModel):
    token: str


class AskBody(BaseModel):
    mode: str = "explain"
    text: str = ""
    question: str = ""
    provider: str = "auto"


class NoteBody(BaseModel):
    text: str
    source: str = "text"


class NotePatch(BaseModel):
    title: str | None = None
    tags: list[str] | str | None = None


class AssistantBody(BaseModel):
    session_id: str
    text: str
    provider: str = "auto"
    auto_approve: bool = False
    persona: str = "auto"      # "jarvis" | "friday" | "auto" (a leading name in the text decides)
    voice: bool = False        # the text came from the microphone
    context: str = ""          # what the phone is showing right now; the assistant treats it as live context


class ApproveBody(BaseModel):
    session_id: str
    call_id: str
    approve: bool


class ResetBody(BaseModel):
    session_id: str


class StopBody(BaseModel):
    session_id: str


def create_app(config: Config, deps: Deps | None = None, audit_path: Path | None = None,
               config_path: Path | None = None, update_log: Path | None = None) -> FastAPI:
    deps = deps or Deps()
    llm = deps.llm or OllamaClient(config.ollama_host, config.chat_model)
    claude = deps.claude or ClaudeClient(config.claude_model)
    claude_login = deps.claude_login or ClaudeCliClient(config.claude_cli_model or "sonnet",
                                                        path=config.claude_cli_path)
    gemini = deps.gemini or GeminiClient(config.gemini_model)
    # Claude only supervises (plans, optionally reviews) and is rationed; Gemini does the work.
    duo = deps.duo or DuoClient(gemini, Supervisor(claude_login, lambda: config.duo_max_claude_per_hour,
                                                    lambda: config.duo_verify))
    transcriber = deps.transcriber or Transcriber(config.whisper_model)
    router = Router(config)
    ollama_seen: dict[str, bool | None] = {"up": None}   # last Ollama answer from /api/status (None = not checked)
    store = NoteStore(config.notes_dir)
    limiter = auth.LoginLimiter()
    config_lock = threading.Lock()   # logout-all's read-change-write of hub.json
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hub-term")
    audit_file = audit_path or Path.home() / ".laptop-hub" / "audit.log"
    update_log_file = update_log or updater_mod.log_path_for(config_path)

    async def label(text: str):
        if deps.label:
            return await deps.label(text)
        # routine job: the default provider (Gemini key, else local...) so Claude quota is kept
        try:
            return await label_note(model_for("auto"), text)
        except LLMError:
            return await label_note(llm, text)

    def audit(event: str, **fields) -> None:
        try:
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass  # a full disk must not take the hub down

    # The assistant may never write the hub's own config or audit log, wherever they are.
    # Work, laptop and Jarvis tools call this app's own routes (see assistant_tools.py); `app` is
    # created just below, so it is looked up on first use.
    assistant = deps.assistant or AssistantService(
        config, audit=audit, protected_paths=[p for p in (audit_file, config_path) if p],
        extra_tools=assistant_tools.build(lambda: app, config) + assistant_tools_more.build(lambda: app, config)
        + friday_tools.build(lambda: app, config) + council_tools.build(lambda: app, config)
        + skills_tools.build(lambda: app, config)
        + research.build(lambda: app, config)
        + vision.build(lambda: app, config)
        + brain_tools.build(lambda: app, config)
        + ccsessions.build(lambda: app, config))

    latency = getattr(assistant, "latency", None) or Latency()   # per-turn timings for GET /api/latency
    warming = {"busy": False}

    async def warm_all():
        """Load everything a first voice turn needs: chat + tool model, Whisper, the Gemini connection. Never raises."""
        if warming["busy"]:
            return
        warming["busy"] = True
        try:
            jobs = []
            if deps.llm is None and not os.environ.get("HUB_DEMO"):
                jobs.append(llm_mod.warm_up(config.ollama_host, config.chat_model))
                tool_model = (config.local_tool_model or "").strip()
                if tool_model and tool_model != config.chat_model:
                    # tool turns send tools, so they use the larger context; same num_ctx avoids a reload
                    jobs.append(llm_mod.warm_up(config.ollama_host, tool_model, num_ctx=LOCAL_CTX_TOOLS))
            if deps.transcriber is None and hasattr(transcriber, "preload"):
                jobs.append(asyncio.to_thread(transcriber.preload))
            if deps.gemini is None and gemini.has_key():
                jobs.append(gemini.warm())
            await asyncio.gather(*jobs, return_exceptions=True)
        finally:
            warming["busy"] = False

    modules: list = []   # feature modules that were found; filled in below

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        """A module may export `async def start(ctx)` (background work, e.g. the reminder clock) that
        returns an optional `async def stop()`."""
        stops = []
        # Load the local model into memory now, so the first question is not a cold start (never raises).
        warm = None
        if not os.environ.get("HUB_DEMO"):
            warm = asyncio.create_task(warm_all())
        for module in modules:
            start = getattr(module, "start", None)
            if start is not None:
                stops.append(await start(ctx))
        yield
        for stop in reversed(stops):
            if stop is not None:
                await stop()

    app = FastAPI(title="Laptop Hub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def hostname_of(value: str | None) -> str:
        value = (value or "").strip().lower()
        if value.startswith("["):
            return value[1:].split("]")[0]
        return value.rsplit(":", 1)[0] if value.count(":") == 1 else value

    allowed_hosts = {"127.0.0.1", "localhost", "::1", INTERNAL_HOST, *(h.lower() for h in config.allowed_hosts)}

    def host_ok(headers) -> bool:
        """Only our own names: a page on another name pointed at this laptop (DNS rebinding) is refused."""
        host = hostname_of(headers.get("host"))
        return host in allowed_hosts or host.endswith(".ts.net")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if not host_ok(request.headers):
            return JSONResponse({"detail": "This address is not allowed. Open the hub by its Tailscale address."},
                                status_code=421)
        # A plain HTML form (for example one injected into rendered text) cannot add a custom header.
        if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
                and request.headers.get("x-hub") != "1"):
            return JSONResponse({"detail": "Requests must come from the app (missing X-Hub header)."},
                                status_code=403)
        response = await call_next(request)
        for key, value in {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; connect-src 'self'; media-src 'self' blob:; "
                "worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
        }.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def origin_ok(headers) -> bool:
        origin = headers.get("origin")
        if not origin:
            return True
        netloc = urlparse(origin).netloc
        allowed = {headers.get("host"), headers.get("x-forwarded-host"), *config.allowed_origins,
                   *(urlparse(o).netloc for o in config.allowed_origins)}
        return netloc in allowed

    def session_ok(value: str | None) -> bool:
        return auth.session_valid(config.session_secret, value, deps.clock(), config.session_generation)

    def need_auth(request: Request) -> None:
        if not session_ok(request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "Log in first.")

    def client_id(request: Request) -> str:
        """Who a failed login counts against. Behind `tailscale serve` every request comes from
        127.0.0.1, so use the tailnet identity it forwards, or the forwarded address."""
        return (request.headers.get("tailscale-user-login")
                or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown"))

    # ---- login -------------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        """Unauthenticated liveness probe for `python -m hub watchdog`. Reveals nothing."""
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request):
        ok = session_ok(request.cookies.get(auth.COOKIE))
        return {"authenticated": ok, "version": VERSION}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        if not origin_ok(request.headers):
            raise HTTPException(403, "Cross-site request refused.")
        who = client_id(request)
        if limiter.blocked(who):
            audit("login_blocked", client=who)
            raise HTTPException(429, "Too many wrong tries. Wait a minute.")
        if not auth.token_matches(body.token.strip(), config.token_hash):
            limiter.failed(who)
            audit("login_failed", client=who)
            raise HTTPException(401, "That token is wrong.")
        limiter.succeeded(who)
        audit("login", client=who)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response = JSONResponse({"ok": True})
        response.set_cookie(auth.COOKIE, auth.make_session(config.session_secret, deps.clock(), config.session_generation),
                            max_age=auth.SESSION_SECONDS, httponly=True, samesite="strict",
                            secure=secure, path="/")
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(need_auth)])

    @api.post("/logout-all")
    def logout_all():
        """Log out every phone and browser: the session generation in hub.json goes up by one, so every cookie
        signed before stops working (this one too). Saved to hub.json when the server knows where it is."""
        persisted = False
        with config_lock:
            new_generation = int(config.session_generation or 0) + 1
            if config_path is not None:
                from . import config as cfg
                try:
                    disk = cfg.load(Path(config_path))
                    disk.session_generation = max(new_generation, int(disk.session_generation or 0) + 1)
                    cfg.save(disk, Path(config_path))
                    new_generation = disk.session_generation
                    persisted = True
                except (cfg.ConfigError, OSError):
                    persisted = False
            config.session_generation = new_generation
            if not persisted:
                # Not saved: also change the secret in memory, so old cookies stay dead until a restart
                # (after a restart the saved secret is back; `python -m hub token` rotates it for good).
                import secrets as _secrets
                config.session_secret = _secrets.token_bytes(32)
        audit("logout_all", generation=new_generation, persisted=persisted)
        response = JSONResponse({"ok": True, "persisted": persisted,
                                 "message": "Every phone and browser is logged out. Log in again with your token."})
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    def fail(exc: Exception):
        status = getattr(exc, "status", 500)
        return HTTPException(status, getattr(exc, "message", str(exc)))

    # ---- status ------------------------------------------------------------------------

    def machine_numbers() -> dict[str, Any]:
        """Disk, CPU, memory, battery. Blocks briefly, so it runs off the event loop."""
        usage = shutil.disk_usage(Path.home())
        out: dict[str, Any] = {"disk": {"free_gb": round(usage.free / 1e9, 1),
                                        "total_gb": round(usage.total / 1e9, 1)}}
        try:
            import psutil
            mem = psutil.virtual_memory()
            out["cpu_percent"] = psutil.cpu_percent(interval=0.2)
            out["memory"] = {"percent": mem.percent, "total_gb": round(mem.total / 1e9, 1)}
            battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
            out["battery"] = ({"percent": round(battery.percent), "plugged": battery.power_plugged}
                              if battery else None)
        except ImportError:
            out.update(cpu_percent=None, memory=None, battery=None)
        return out

    @api.get("/status")
    async def status():
        info: dict[str, Any] = {
            "hostname": socket.gethostname(), "platform": platform.platform(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "shells": list(config.shell_table()), "scripts": len(config.scripts),
            "chat_model": config.chat_model, "version": updater_mod.read_version(),
            "full_control": config.full_control is True, "jarvis_name": config.jarvis_name,
        }
        info.update(await asyncio.to_thread(machine_numbers))
        try:
            info["ollama"] = {"up": True, "models": await llm.models()}
        except LLMError:
            info["ollama"] = {"up": False, "models": []}
        ollama_seen["up"] = info["ollama"]["up"]
        avail = await asyncio.to_thread(providers_mod.availability, config, info["ollama"]["up"])
        info["providers"] = avail
        avail["router"] = router.status()
        info["failovers"] = int(getattr(assistant, "failovers", 0) or 0)
        sup = getattr(duo, "supervisor", None)
        info["duo"] = sup.status() if sup else {"paused": False, "reason": "", "note": ""}
        if info["duo"]["note"] and "duo" in avail:
            avail["duo"]["note"] = info["duo"]["note"]
        info["default_provider"] = await asyncio.to_thread(providers_mod.resolve_default, config)
        # "ready" for the assistant: any cloud AI (API key, Claude login or Gemini) can answer
        try:   # Autopilot's small status block (enabled, cards waiting, today's AI calls)
            from . import autopilot as autopilot_mod
            info["autopilot"] = autopilot_mod.status_block(ctx)
        except Exception:  # noqa: BLE001 - the status page must never fail because of a feature
            info["autopilot"] = {"enabled": False, "pending": 0, "ai_calls_today": 0}
        info["claude_ready"] = any(avail[k]["ready"] for k in ("claude", "claude_login", "gemini"))
        try:
            import faster_whisper  # noqa: F401
            info["voice_ready"] = True
        except ImportError:
            info["voice_ready"] = deps.transcriber is not None
        return info

    @api.get("/update-log")
    def update_log_tail():
        """Read-only: the last lines of update.log (20 at most, 200 characters each) and nothing else."""
        lines = updater_mod.tail_log(update_log_file, 20, 200)
        return {"version": updater_mod.read_version(), "lines": lines, "last_update": lines[-1] if lines else ""}

    update_lock = threading.Lock()
    update_started = {"at": 0.0}

    @api.post("/update")
    async def update_start(body: dict = Body(default={})):
        """Starts an update (pull + restart) without needing the scheduled task. The owner confirms in the UI."""
        if body.get("confirm") is not True:
            raise HTTPException(409, "An update restarts the hub. Confirm to continue.")
        with update_lock:
            if time.monotonic() - update_started["at"] < 60 and update_started["at"]:
                raise HTTPException(409, "An update was just started. Wait a minute.")
            update_started["at"] = time.monotonic()
        try:
            result = await asyncio.to_thread(updater_mod.start_update, config_path)
        except OSError:
            update_started["at"] = 0.0
            audit("update_start", ok=False)
            raise HTTPException(500, "The update could not be started. Run: python -m hub update --restart") from None
        audit("update_start", ok=True, method=result["method"])
        return {"ok": True, "method": result["method"], "version": updater_mod.read_version(),
                "message": "Updating now. The app will reconnect in about 20 seconds."}

    # ---- files -------------------------------------------------------------------------

    @api.get("/roots")
    def roots():
        return [{"name": name} for name in config.roots]

    @api.get("/files")
    def list_files(root: str, path: str = ""):
        try:
            entries = files.list_dir(config.roots, root, path)
        except files.FileError as exc:
            raise fail(exc)
        return {"root": root, "path": path, "entries": [e.__dict__ for e in entries]}

    @api.get("/file")
    def read_file(root: str, path: str, download: bool = False):
        try:
            target = files.resolve(config.roots, root, path)
        except files.FileError as exc:
            raise fail(exc)
        if not target.is_file():
            raise HTTPException(404, "Not a file.")
        media_type, attach = files.serving_type(target)
        headers = {}
        if media_type != "application/pdf":
            headers["Content-Security-Policy"] = "sandbox"
        return FileResponse(target, media_type=media_type, headers=headers,
                            filename=target.name if (attach or download) else None,
                            content_disposition_type="attachment" if (attach or download) else "inline")

    @api.post("/upload")
    async def upload(file: UploadFile = File(...)):
        inbox = Path(config.inbox_dir)
        inbox.mkdir(parents=True, exist_ok=True)
        name = "".join(c if c.isalnum() or c in "._- " else "_" for c in os.path.basename(file.filename or "upload")).strip(". ") or "upload"
        stem, suffix = os.path.splitext(name)
        target, n = inbox / name, 1
        while target.exists():
            n += 1
            target = inbox / f"{stem}-{n}{suffix}"
        size = 0
        try:
            with open(target, "wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "File is over 200 MB.")
                    out.write(chunk)
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
        audit("upload", name=target.name, size=size)
        return {"saved_as": target.name, "size": size}

    # ---- ask the model about a passage ---------------------------------------------------

    def sse(generator):
        async def stream():
            try:
                async for chunk in generator:
                    yield f"data: {json.dumps(chunk)}\n\n"
            except LLMError as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api.post("/ask")
    async def ask(body: AskBody):
        try:
            system, user = build_prompt(body.mode, body.text, body.question)
        except LLMError as exc:
            raise HTTPException(400, str(exc))
        picked, decision = router.route(body.provider, body.question or body.text, ollama_seen["up"])
        model = model_for(picked)

        async def pieces():
            if decision:
                yield {"meta": {"routed_to": decision["routed_to"], "reason": decision["reason"]}}
            async for text in model.stream(system, user):
                yield {"t": text}
        return sse(pieces())

    # ---- voice -----------------------------------------------------------------------------

    async def transcribe_upload(file: UploadFile) -> str:
        suffix = os.path.splitext(file.filename or "")[1] or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            size = 0
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    os.unlink(tmp.name)
                    raise HTTPException(413, "Recording is over 25 MB.")
                tmp.write(chunk)
        began = time.monotonic()
        try:
            text = await asyncio.to_thread(transcriber.transcribe, tmp.name)
            latency.note_stt((time.monotonic() - began) * 1000)
            return text
        except SpeechError as exc:
            raise HTTPException(503, str(exc))
        finally:
            os.unlink(tmp.name)

    @api.post("/transcribe")
    async def transcribe(file: UploadFile = File(...)):
        return {"text": await transcribe_upload(file)}

    # ---- notes -----------------------------------------------------------------------------

    async def tag_in_background(note_id: str, text: str) -> None:
        try:
            title, tags = await label(text)
            await asyncio.to_thread(store.apply_auto, note_id, title, tags)
        except (LLMError, NoteError, OSError):
            pass  # the note keeps its plain title and no tags; the user can set them

    @api.get("/notes")
    def list_notes(tag: str | None = None, q: str | None = None):
        return {"notes": [n.summary() for n in store.all(tag, q)], "tags": store.all_tags()}

    @api.post("/notes")
    def add_note(body: NoteBody, background: BackgroundTasks):
        try:
            note = store.add(body.text, "text" if body.source not in ("voice", "ai") else body.source)
        except NoteError as exc:
            raise fail(exc)
        background.add_task(tag_in_background, note.id, note.body)
        return note.full()

    @api.post("/notes/voice")
    async def add_voice_note(background: BackgroundTasks, file: UploadFile = File(...)):
        transcript = await transcribe_upload(file)
        text = clean_spoken(transcript)
        if not text:
            raise HTTPException(422, "I couldn't hear any words in that recording.")
        note = store.add(text, "voice")
        background.add_task(tag_in_background, note.id, note.body)
        return {"note": note.full(), "transcript": transcript}

    @api.get("/notes/{note_id}")
    def get_note(note_id: str):
        try:
            return store.get(note_id).full()
        except NoteError as exc:
            raise fail(exc)

    @api.patch("/notes/{note_id}")
    def patch_note(note_id: str, body: NotePatch):
        try:
            return store.edit(note_id, body.title, body.tags).full()
        except NoteError as exc:
            raise fail(exc)

    # ---- quick buttons -----------------------------------------------------------------------

    @api.get("/scripts")
    def list_scripts():
        return [{"name": name, "label": spec.get("label", name)} for name, spec in config.scripts.items()]

    @api.post("/run/{name}")
    async def run_script(name: str):
        spec = config.scripts.get(name)
        if not spec:
            raise HTTPException(404, "No such button.")
        audit("script", name=name, argv=spec["argv"])
        try:
            result = await asyncio.to_thread(
                subprocess.run, spec["argv"], capture_output=True, text=True, timeout=120,
                cwd=spec.get("cwd"))
        except FileNotFoundError:
            return {"exit": 127, "output": f"Program not found: {spec['argv'][0]}"}
        except subprocess.TimeoutExpired:
            return {"exit": 124, "output": "Stopped after 120 seconds."}
        return {"exit": result.returncode, "output": (result.stdout + result.stderr)[-4000:]}

    # ---- assistant ------------------------------------------------------------------------------

    @api.post("/assistant/message")
    async def assistant_message(body: AssistantBody):
        # A leading name routes the message (typed or spoken alike): "Jarvis, run the tests" / "Friday, my day?"
        persona, text = clean_persona(body.persona), body.text
        if not persona:
            named, stripped = split_persona(body.text, config.jarvis_name)
            if named and stripped:
                persona, text = named, stripped
        provider, decision = router.route(body.provider, text, ollama_seen["up"])
        audit("assistant_message", session=body.session_id, provider=provider,
              auto_approve=body.auto_approve, chars=len(text), persona=persona or "jarvis",
              **({"full_control": True} if config.full_control is True else {}))
        extra_args = {k: v for k, v in (("persona", persona), ("voice", body.voice)) if v}
        screen = (body.context or "").strip()[:6000]
        run_text = (f"[The user is looking at this screen right now. Use it as live context, do not repeat it back.]\n"
                    f"{screen}\n[End of screen]\n\n{text}") if screen else text

        async def events():
            if decision:
                yield f"data: {json.dumps({'type': 'meta', 'routed_to': decision['routed_to'], 'reason': decision['reason']})}\n\n"
            try:
                async for event in assistant.run(body.session_id, run_text, provider,
                                                 body.auto_approve, **extra_args):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:  # the stream must always end cleanly for the phone
                yield f"data: {json.dumps({'type': 'error', 'message': f'Assistant failed: {exc}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api.get("/latency")
    def get_latency(n: int = 20):
        """The last turns' timings (memory only): stt_ms, first_token_ms, total_ms."""
        return {"spans": latency.recent(min(max(n, 1), 50)), "summary": latency.summary()}

    @api.get("/assistant/info")
    def assistant_info():
        return {"full_control": config.full_control is True, "fast_commands": config.fast_commands is True,
                "jarvis_name": config.jarvis_name, "display_name": config.display_name}

    @api.post("/assistant/warm")
    async def assistant_warm():
        """Voice mode opened (or is still open): load the models and keep them loaded. Returns at once."""
        if not warming["busy"]:
            asyncio.create_task(warm_all())
        return {"ok": True}

    @api.post("/assistant/approve")
    def assistant_approve(body: ApproveBody):
        audit("assistant_approval", session=body.session_id, call=body.call_id, approve=body.approve)
        if not assistant.resolve(body.session_id, body.call_id, body.approve):
            raise HTTPException(404, "Nothing is waiting for that approval.")
        return {"ok": True}

    @api.post("/assistant/stop")
    async def assistant_stop(body: StopBody):
        """Stop: cancel the running turn, deny what waits for approval, end the stream with a 'stopped' note."""
        stop = getattr(assistant, "stop", None)
        stopped = bool(stop(body.session_id)) if stop else False
        audit("assistant_stop_request", session=body.session_id, stopped=stopped)
        return {"ok": True, "stopped": stopped}

    @api.post("/assistant/reset")
    def assistant_reset(body: ResetBody):
        assistant.reset(body.session_id)
        return {"ok": True}

    app.include_router(api)

    # Feature modules (work, study, english). Each exposes make_router(ctx); one that isn't
    # written yet is skipped, but a real error inside a module is never hidden.
    def model_for(provider: str):
        choice = providers_mod.resolve(config, provider)
        if choice == "claude":
            return claude
        if choice == "claude-login":
            return claude_login
        if choice == "gemini":
            return gemini
        if choice == "duo":
            return duo
        return llm

    if hasattr(assistant, "model_for"):
        assistant.model_for = model_for

    ctx = Ctx(config=config, model_for=model_for, transcriber=transcriber, notes=store,
              need_auth=need_auth, audit=audit, get_app=lambda: app, audit_path=audit_file,
              config_path=config_path)
    for name in FEATURE_MODULES:
        try:
            module = importlib.import_module(f".{name}", __package__)
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.{name}":
                raise
            continue
        modules.append(module)
        if hasattr(module, "make_router"):
            app.include_router(module.make_router(ctx))

    # ---- terminal ---------------------------------------------------------------------------------

    async def refuse(ws: WebSocket, code: int) -> None:
        # Accept, then close with a code: closing before accept shows browsers a bare 1006.
        await ws.accept()
        await ws.close(code=code)

    @app.websocket("/ws/term")
    async def terminal_socket(ws: WebSocket):
        if not session_ok(ws.cookies.get(auth.COOKIE)):
            return await refuse(ws, 4401)
        if not origin_ok(ws.headers) or not host_ok(ws.headers):
            return await refuse(ws, 4403)
        table = config.shell_table()
        shell = ws.query_params.get("shell") or next(iter(table))
        if shell not in table:
            return await refuse(ws, 4400)
        try:
            rows = min(max(int(ws.query_params.get("rows", 24)), 2), 200)
            cols = min(max(int(ws.query_params.get("cols", 80)), 10), 400)
        except ValueError:
            return await refuse(ws, 4400)
        await ws.accept()
        try:
            term = deps.terminal(table[shell], rows, cols)
        except TerminalError as exc:
            await ws.send_text(json.dumps({"error": str(exc)}))
            await ws.close()
            return
        audit("terminal_open", shell=shell)
        loop = asyncio.get_running_loop()

        async def shell_to_phone():
            """Shell output -> phone. Ends when the shell exits or the phone is gone."""
            try:
                while True:
                    data = await loop.run_in_executor(pool, term.read)
                    if not data:
                        break
                    await ws.send_bytes(data)
                await ws.close()
            except Exception:  # the connection is already gone; cleanup below handles the shell
                pass

        async def phone_to_shell():
            """Keystrokes and resizes -> shell. Ends when the phone disconnects."""
            try:
                while True:
                    message = json.loads(await ws.receive_text())
                    if message.get("type") == "input":
                        term.write(str(message.get("data", "")).encode("utf-8"))
                    elif message.get("type") == "resize":
                        term.resize(min(max(int(message["rows"]), 2), 200),
                                    min(max(int(message["cols"]), 10), 400))
            except (WebSocketDisconnect, RuntimeError, ValueError, KeyError, OSError):
                pass

        tasks = [asyncio.create_task(shell_to_phone()), asyncio.create_task(phone_to_shell())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await loop.run_in_executor(None, term.close)  # hang up the shell and its children
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            term.release()
            audit("terminal_close", shell=shell)

    # ---- the app itself ------------------------------------------------------------------------------

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
