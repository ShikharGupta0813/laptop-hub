"""The web server: login, files, notes, ask, assistant, quick buttons and the terminal.

API details are in docs/API.md. Every route except login and the static app needs the
session cookie. The server listens on 127.0.0.1 only; Tailscale puts HTTPS in front of it.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import (APIRouter, BackgroundTasks, Depends, FastAPI, File, HTTPException, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, files
from .assistant import AssistantService
from .config import Config
from .llm import ClaudeClient, LLMError, OllamaClient, build_prompt, label_note
from .notes import NoteError, NoteStore, clean_spoken
from .speech import SpeechError, Transcriber
from .terminal import TerminalError, open_terminal

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
VERSION = "1.0"


@dataclass
class Deps:
    """Everything that talks to the outside world, so tests can swap it."""
    llm: Any = None
    claude: Any = None
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
    provider: str = "local"


class NoteBody(BaseModel):
    text: str
    source: str = "text"


class NotePatch(BaseModel):
    title: str | None = None
    tags: list[str] | str | None = None


class AssistantBody(BaseModel):
    session_id: str
    text: str
    provider: str = "claude"
    auto_approve: bool = False


class ApproveBody(BaseModel):
    session_id: str
    call_id: str
    approve: bool


class ResetBody(BaseModel):
    session_id: str


def create_app(config: Config, deps: Deps | None = None, audit_path: Path | None = None) -> FastAPI:
    deps = deps or Deps()
    llm = deps.llm or OllamaClient(config.ollama_host, config.chat_model)
    claude = deps.claude or ClaudeClient(config.claude_model)
    transcriber = deps.transcriber or Transcriber(config.whisper_model)
    assistant = deps.assistant or AssistantService(config, claude=None, local=None)
    store = NoteStore(config.notes_dir)
    limiter = auth.LoginLimiter()
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hub-term")
    audit_file = audit_path or Path.home() / ".laptop-hub" / "audit.log"

    async def label(text: str):
        return await (deps.label or (lambda t: label_note(llm, t)))(text)

    def audit(event: str, **fields) -> None:
        try:
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass  # a full disk must not take the hub down

    app = FastAPI(title="Laptop Hub", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
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

    def need_auth(request: Request) -> None:
        if not auth.session_valid(config.session_secret, request.cookies.get(auth.COOKIE), deps.clock()):
            raise HTTPException(401, "Log in first.")

    def client_id(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # ---- login -------------------------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request):
        ok = auth.session_valid(config.session_secret, request.cookies.get(auth.COOKIE), deps.clock())
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
        response.set_cookie(auth.COOKIE, auth.make_session(config.session_secret, deps.clock()),
                            max_age=auth.SESSION_SECONDS, httponly=True, samesite="strict",
                            secure=secure, path="/")
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(need_auth)])

    def fail(exc: Exception):
        status = getattr(exc, "status", 500)
        return HTTPException(status, getattr(exc, "message", str(exc)))

    # ---- status ------------------------------------------------------------------------

    @api.get("/status")
    async def status():
        info: dict[str, Any] = {
            "hostname": socket.gethostname(), "platform": platform.platform(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "shells": list(config.shell_table()), "scripts": len(config.scripts),
            "claude_ready": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "chat_model": config.chat_model,
        }
        usage = shutil.disk_usage(Path.home())
        info["disk"] = {"free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}
        try:
            import psutil
            mem = psutil.virtual_memory()
            info["cpu_percent"] = psutil.cpu_percent(interval=0.2)
            info["memory"] = {"percent": mem.percent, "total_gb": round(mem.total / 1e9, 1)}
            battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
            info["battery"] = ({"percent": round(battery.percent), "plugged": battery.power_plugged}
                               if battery else None)
        except ImportError:
            info.update(cpu_percent=None, memory=None, battery=None)
        try:
            info["ollama"] = {"up": True, "models": await llm.models()}
        except LLMError:
            info["ollama"] = {"up": False, "models": []}
        try:
            import faster_whisper  # noqa: F401
            info["voice_ready"] = True
        except ImportError:
            info["voice_ready"] = deps.transcriber is not None
        return info

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

    @api.get("/search")
    def search(root: str, q: str, path: str = ""):
        try:
            return {"results": files.search(config.roots, root, q, path)}
        except files.FileError as exc:
            raise fail(exc)

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
        model = claude if body.provider == "claude" else llm

        async def pieces():
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
        try:
            return await asyncio.to_thread(transcriber.transcribe, tmp.name)
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
        audit("assistant_message", session=body.session_id, provider=body.provider,
              auto_approve=body.auto_approve, chars=len(body.text))

        async def events():
            try:
                async for event in assistant.run(body.session_id, body.text, body.provider,
                                                 body.auto_approve):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:  # the stream must always end cleanly for the phone
                yield f"data: {json.dumps({'type': 'error', 'message': f'Assistant failed: {exc}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api.post("/assistant/approve")
    def assistant_approve(body: ApproveBody):
        audit("assistant_approval", session=body.session_id, call=body.call_id, approve=body.approve)
        if not assistant.resolve(body.session_id, body.call_id, body.approve):
            raise HTTPException(404, "Nothing is waiting for that approval.")
        return {"ok": True}

    @api.post("/assistant/reset")
    def assistant_reset(body: ResetBody):
        assistant.reset(body.session_id)
        return {"ok": True}

    app.include_router(api)

    # ---- terminal ---------------------------------------------------------------------------------

    @app.websocket("/ws/term")
    async def terminal_socket(ws: WebSocket):
        if not auth.session_valid(config.session_secret, ws.cookies.get(auth.COOKIE), deps.clock()):
            await ws.close(code=4401)
            return
        if not origin_ok(ws.headers):
            await ws.close(code=4403)
            return
        table = config.shell_table()
        shell = ws.query_params.get("shell") or next(iter(table))
        if shell not in table:
            await ws.close(code=4400)
            return
        try:
            rows = min(max(int(ws.query_params.get("rows", 24)), 2), 200)
            cols = min(max(int(ws.query_params.get("cols", 80)), 10), 400)
        except ValueError:
            await ws.close(code=4400)
            return
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
