"""Laptop-side wake word: say "Hey Jarvis, ..." at the desk and the hub answers out loud, phone untouched.

OFF by default (`wake_laptop_enabled`). The hub reads the laptop microphone (optional `sounddevice`, imported lazily),
cuts speech out with a cheap energy gate, transcribes it with the local Whisper the hub already uses, and checks
the text against the wake phrase rules (a Python port of js/wakephrase.js: a greeting word plus the name, so "Jarvis is
a nice name" never wakes it). Only a match goes further: a chime, then the rest of the sentence (or the next thing you
say) goes to the assistant through the same route the phone uses (`/api/assistant/message`, voice on, auto-approve OFF,
so every change still waits for a tap on the phone, and a push tells you). The reply is spoken on the laptop speakers.

Privacy: audio never leaves the laptop (Whisper is local); only the wake sentence's text goes to the AI you chose.
Audio and the transcripts of everything that is not a wake phrase are discarded at once: never stored, never logged.

Everything the loop touches is injectable (audio source, transcriber, assistant, speaker, chime, clock) for tests.
Routes (login needed): GET /api/wakeword/status, POST /api/wakeword/toggle {"enabled": bool} (flips when omitted).
"""
from __future__ import annotations

import array
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import config as cfg
from .common import Ctx
from . import panic as _panic
from .assistants import load as load_assistants, wake_table
from .persona import split_persona

log = logging.getLogger("hub.wakeword")

RATE = 16000
FRAME = 512                      # samples per read (32 ms)
SESSION_ID = "laptop-wake"
COMMAND_WAIT_S = 8.0             # after a bare "Hey Jarvis", how long to wait for the command
ECHO_TAIL_S = 0.5                # keep ignoring the mic this long after speaking
SPEAK_MAX = 1200

# ---- wake phrase (port of js/wakephrase.js) ------------------------------------------------------------
_PREFIX = re.compile(r"^\s*(?:hey|hi|ok|okay|yo)[,.!\s]+(?=\S)", re.I)
_HEARD = {"jarvas": "jarvis", "jarves": "jarvis"}      # common mis-hearings; "service" is NOT one of them


def match_wake(transcript: str, jarvis_name: str = "Jarvis", assistants=None) -> tuple[str, str] | None:
    """(persona, rest of the sentence) when the text starts with a greeting and a name, else None.

    `assistants` is the owner's list (hub/assistants.py): its names and mis-hearings wake it. Without it the two
    built-in names (plus the custom Jarvis name) apply, as before."""
    raw = str(transcript or "")
    pre = _PREFIX.match(raw)
    if not pre:
        return None
    rest = raw[pre.end():]
    first = re.match(r"[\w'’]+", rest)
    if not first:
        return None
    word = first.group(0).lower()
    if assistants is not None:
        table = wake_table(assistants)
        if word not in table:
            return None                                          # the name must come first, never later in the sentence
        entry = next(a for a in assistants if a["id"] == table[word])
        rest = entry["wake_names"][0] + rest[first.end():]       # a mis-hearing becomes the real name
        persona, tail = split_persona(rest, jarvis_name, assistants)
        return (persona, tail) if persona else None
    custom = (jarvis_name or "Jarvis").strip().lower()
    if word not in ("jarvis", "friday", custom, *_HEARD):      # the name must come first, never later in the sentence
        return None
    if word in _HEARD:
        rest = "jarvis" + rest[first.end():]
    persona, tail = split_persona(rest, jarvis_name)
    return (persona, tail) if persona else None


# ---- the energy gate / utterance cutter ----------------------------------------------------------------
def rms(frame: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    step = samples[::2] or samples                     # every other sample is plenty for a loudness estimate
    return (sum(s * s for s in step) / len(step)) ** 0.5 if step else 0.0


class Segmenter:
    """Feed 16 kHz mono int16 frames. Returns the PCM of one utterance once it ends (0.8 s of quiet, or the 12 s cap)."""

    def __init__(self, rate: int = RATE, frame: int = FRAME, min_rms: float = 350.0, factor: float = 2.5,
                 silence_s: float = 0.8, max_s: float = 12.0, min_voiced_s: float = 0.25, preroll_s: float = 0.3):
        self.min_rms, self.factor = min_rms, factor
        fs = frame / rate
        self.silence_n, self.max_n = max(1, round(silence_s / fs)), max(1, round(max_s / fs))
        self.min_voiced_n, self.preroll_n = max(1, round(min_voiced_s / fs)), max(0, round(preroll_s / fs))
        self.reset()

    def reset(self) -> None:
        self.floor, self.active, self.quiet, self.voiced = 0.0, False, 0, 0
        self.frames: list[bytes] = []
        self.preroll: list[bytes] = []

    @property
    def threshold(self) -> float:
        return max(self.min_rms, self.floor * self.factor)

    def feed(self, frame: bytes) -> bytes | None:
        level = rms(frame)
        loud = level >= self.threshold
        if not self.active:
            if not loud:
                self.floor = self.floor * 0.95 + level * 0.05    # learn the room's background level
                self.preroll = (self.preroll + [frame])[-self.preroll_n:] if self.preroll_n else []
                return None
            self.active, self.quiet, self.voiced = True, 0, 0
            self.frames, self.preroll = list(self.preroll), []
        self.frames.append(frame)
        if loud:
            self.voiced += 1
            self.quiet = 0
        else:
            self.quiet += 1
        if self.quiet >= self.silence_n or len(self.frames) >= self.max_n:
            pcm, voiced = b"".join(self.frames), self.voiced
            self.frames, self.active, self.quiet, self.voiced = [], False, 0, 0
            return pcm if voiced >= self.min_voiced_n else None      # a click or a cough is not speech
        return None


# ---- the real audio, chime and speaker (all lazy, all optional) -----------------------------------------
class NoMicrophone(Exception):
    """Raised with a plain sentence when the microphone cannot be used."""


class SoundDeviceSource:
    def __init__(self, device: str = ""):
        try:
            import sounddevice as sd
        except Exception:
            raise NoMicrophone("Needs the optional sounddevice package. Run: pip install sounddevice") from None
        try:
            name = str(device or "").strip()
            dev: Any = (int(name) if name.isdigit() else name) or None
            self._stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, device=dev)
            self._stream.start()
        except Exception as exc:
            raise NoMicrophone(f"No usable microphone on this laptop ({str(exc)[:120]}).") from None

    def read(self, n: int = FRAME) -> bytes:
        data, _overflow = self._stream.read(n)
        return bytes(data)

    def flush(self) -> None:
        try:
            avail = self._stream.read_available
            if avail:
                self._stream.read(avail)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


def default_chime(persona: str = "jarvis") -> None:
    if sys.platform != "win32":
        return                                            # no sound dependency elsewhere: the spoken reply is the cue
    try:
        import winsound
        for f, ms in ((784, 90), (1175, 120)) if persona == "friday" else ((523, 90), (784, 120)):
            winsound.Beep(f, ms)
    except Exception:
        pass


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " code omitted ", str(text or ""), flags=re.S)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[`*_#>|]+", "", t)
    return re.sub(r"\s+", " ", t).strip()[:SPEAK_MAX]


def speak_argv() -> list[str] | None:
    """The command that reads stdin aloud on this machine, or None. The text is NEVER part of the argv."""
    if sys.platform == "win32":
        ps = shutil.which("powershell") or "powershell"
        return [ps, "-NoProfile", "-NonInteractive", "-Command",
                "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Speak([Console]::In.ReadToEnd())"]
    if sys.platform == "darwin" and shutil.which("say"):
        return ["say"]                                    # reads stdin when given no text
    for exe in ("espeak-ng", "espeak"):
        found = shutil.which(exe)
        if found:
            return [found, "--stdin"]
    return None


def default_speaker(text: str) -> None:
    text = clean_for_speech(text)
    argv = speak_argv()
    if not text or argv is None:
        return
    try:
        subprocess.run(argv, input=text, text=True, encoding="utf-8", capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pass


# ---- talking to the assistant, exactly like the phone does -----------------------------------------------
def message_body(text: str, persona: str, session_id: str = SESSION_ID) -> dict:
    """The /api/assistant/message payload: voice on, auto-approve OFF (changes still wait for the phone)."""
    return {"session_id": session_id, "text": text, "context": "", "provider": "auto",
            "auto_approve": False, "persona": persona, "voice": True}


def consume(events, notify: Callable[..., Any], say: Callable[[str], None]) -> str:
    """Reply text from assistant events. An approval request becomes a push and one spoken line, never an action."""
    reply, told = [], False
    for ev in events:
        kind = ev.get("type")
        if kind == "text":
            reply.append(str(ev.get("t") or ""))
        elif kind == "approval_needed":
            try:
                notify("Approval needed",
                       body=f"The laptop wake word asked for: {ev.get('name', 'an action')}. Open the app to approve or deny.",
                       level="warn", link=None, source="wakeword")
            except Exception:
                pass
            if not told:
                told = True
                say("I need your approval on the phone for that.")
        elif kind == "error":
            return str(ev.get("message") or "Something went wrong.")
    return "".join(reply).strip()


def default_assistant(ctx: Ctx, loop_getter: Callable[[], Any]) -> Callable[..., str]:
    from . import auth
    from .common import InProcessClient
    client = InProcessClient(ctx.get_app, ctx.config)

    async def run(text: str, persona: str) -> list[dict]:
        gen = getattr(ctx.config, "session_generation", 0)
        cookie = f"{auth.COOKIE}={auth.make_session(ctx.config.session_secret, generation=gen)}"
        events: list[dict] = []
        async with client._http().stream("POST", "/api/assistant/message", json=message_body(text, persona),
                                         headers={"Cookie": cookie}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    try:
                        events.append(json.loads(line[5:].strip()))
                    except ValueError:
                        continue
        return events

    def ask(text: str, persona: str, say: Callable[[str], None] = lambda _t: None) -> str:
        loop = loop_getter()
        if loop is None:
            return ""
        events = asyncio.run_coroutine_threadsafe(run(text, persona), loop).result(timeout=1800)
        return consume(events, ctx.notify, say)
    return ask


# ---- the listener ---------------------------------------------------------------------------------------
class WakeListener:
    def __init__(self, ctx: Ctx, *, source_factory: Callable[[str], Any] | None = None, transcriber: Any = None,
                 assistant: Callable[..., str] | None = None, speaker: Callable[[str], None] | None = None,
                 chime: Callable[[str], None] | None = None, clock: Callable[[], float] | None = None,
                 segmenter: Segmenter | None = None):
        self.ctx = ctx
        self.source_factory = source_factory or SoundDeviceSource
        self.transcriber = transcriber
        self.assistant = assistant
        self.speaker = speaker or default_speaker
        self.chime = chime or default_chime
        self.clock = clock or time.monotonic
        self.seg = segmenter or Segmenter()
        self.state, self.message = "off", ""
        self.speaking = False
        self.ignore_until = 0.0
        self.awaiting: tuple[str, float] | None = None     # (persona, deadline) after a bare wake phrase
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:                              # config is read live
        return getattr(self.ctx.config, "wake_laptop_enabled", False) is True and not _panic.on(self.ctx.config)

    def status(self) -> dict:
        return {"enabled": self.enabled, "state": self.state, "message": self.message,
                "listening": self.state in ("listening", "thinking", "speaking"),
                "device": str(getattr(self.ctx.config, "wake_laptop_device", "") or "")}

    # ---- one frame in, maybe an utterance out (the echo guard lives here) ----
    def feed(self, frame: bytes) -> bytes | None:
        if self.speaking or self.clock() < self.ignore_until:
            self.seg.reset()
            return None
        return self.seg.feed(frame)

    def say_aloud(self, text: str) -> None:
        """Speak on the laptop speakers; the microphone is ignored meanwhile and for a short tail after."""
        self.speaking = True
        prev, self.state = self.state, "speaking"
        try:
            self.speaker(text)
        except Exception:
            log.warning("speaking failed")
        finally:
            self.speaking = False
            self.ignore_until = self.clock() + ECHO_TAIL_S
            self.seg.reset()
            if self.state == "speaking":
                self.state = prev if prev != "speaking" else "listening"

    def _tmpdir(self) -> Path:
        try:
            d = Path(self.ctx.config.data_path()) / "wake-tmp"
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            return Path(tempfile.gettempdir())

    def transcribe(self, pcm: bytes) -> str:
        tr = self.transcriber or self.ctx.transcriber
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="wake-", dir=str(self._tmpdir()))
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(pcm)
            return str(tr.transcribe(path) or "").strip()
        finally:
            try:
                os.remove(path)                              # the recording is gone as soon as it is read
            except OSError:
                pass

    def handle(self, pcm: bytes) -> None:
        """One utterance: transcribe, wake check, and (only on a wake) the assistant. Non-wake text is dropped here."""
        self.state = "thinking"
        try:
            text = self.transcribe(pcm)
            if self.awaiting and self.clock() <= self.awaiting[1]:
                persona, command = self.awaiting[0], text
                self.awaiting = None
            else:
                self.awaiting = None
                hit = match_wake(text, getattr(self.ctx.config, "jarvis_name", "Jarvis"), load_assistants(self.ctx.config)) if text else None
                if hit is None:
                    return                                   # not for us: nothing kept, nothing logged
                persona, command = hit
                self.ctx.audit("wakeword", persona=persona, chars=len(command))
                self.chime(persona)
                if not command:
                    self.awaiting = (persona, self.clock() + COMMAND_WAIT_S)
                    return
            if not command.strip() or self.assistant is None:
                return
            reply = self.assistant(command, persona, self.say_aloud)
            if reply:
                self.say_aloud(reply)
        except Exception as exc:
            log.warning("wake handling failed: %s", type(exc).__name__)
        finally:
            if self.state == "thinking":
                self.state = "listening"

    # ---- thread ----
    def _open(self):
        try:
            src = self.source_factory(str(getattr(self.ctx.config, "wake_laptop_device", "") or ""))
        except NoMicrophone as exc:
            self.state, self.message = ("missing_dependency" if "sounddevice" in str(exc) else "no_mic"), str(exc)
            return None
        except Exception as exc:
            self.state, self.message = "no_mic", f"The microphone could not be opened ({type(exc).__name__})."
            return None
        self.state, self.message = "listening", ""
        return src

    def _run(self) -> None:
        src = self._open()
        if src is None:
            return
        try:
            while not self._stop.is_set():
                frame = src.read(FRAME)
                if not frame:
                    continue
                pcm = self.feed(frame)
                if pcm:
                    self.handle(pcm)
                    flush = getattr(src, "flush", None)
                    if flush:
                        flush()
        except Exception as exc:
            self.state, self.message = "error", f"Listening stopped ({type(exc).__name__}). Switch it off and on again."
        finally:
            try:
                src.close()
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.state, self.message = "starting", ""
            self._thread = threading.Thread(target=self._run, name="wakeword", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t, self._thread = self._thread, None
        if t:
            t.join(timeout=3)
        self.state, self.message, self.awaiting = "off", "", None


# ---- module wiring -------------------------------------------------------------------------------------
def _persist(ctx: Ctx, enabled: bool) -> None:
    ctx.config.wake_laptop_enabled = enabled
    path = getattr(ctx, "config_path", None)
    if not path:
        return
    try:
        disk = cfg.load(Path(path))
        disk.wake_laptop_enabled = enabled
        cfg.save(disk, Path(path))
    except Exception:
        pass                                                  # still on or off until the hub restarts


def make_router(ctx: Ctx, *, listener: WakeListener | None = None) -> APIRouter:
    holder: dict[str, Any] = {"loop": None}
    lis = listener or WakeListener(ctx)
    if lis.assistant is None:
        lis.assistant = default_assistant(ctx, lambda: holder["loop"])
    ctx.wakeword = lis                  # type: ignore[attr-defined]
    ctx.wakeword_holder = holder        # type: ignore[attr-defined]

    async def _panic_stop():
        was = lis.state not in ("off", "error")
        await asyncio.to_thread(lis.stop)
        return "wake word listener stopped" if was else None

    async def _panic_resume(_state):
        if lis.enabled:
            await asyncio.to_thread(lis.start)
    _panic.register("Wake word (laptop)", _panic_stop, _panic_resume, ctx=ctx)   # hub/panic.py
    router = APIRouter(prefix="/api/wakeword", dependencies=[Depends(ctx.need_auth)])

    @router.get("/status")
    def status():
        return lis.status()

    @router.post("/toggle")
    def toggle(body: dict = Body(default={})):
        want = body.get("enabled") if isinstance(body, dict) else None
        if want is not None and not isinstance(want, bool):
            raise HTTPException(400, "enabled must be true or false.")
        want = (not lis.enabled) if want is None else want
        _persist(ctx, want)
        ctx.audit("wakeword_toggle", enabled=want)
        if want:
            lis.start()
        else:
            lis.stop()
        return lis.status()

    return router


async def start(ctx: Ctx):
    lis = getattr(ctx, "wakeword", None)
    if lis is None:
        return None
    ctx.wakeword_holder["loop"] = asyncio.get_running_loop()    # type: ignore[attr-defined]
    if lis.enabled:
        lis.start()

    async def stop():
        await asyncio.to_thread(lis.stop)
    return stop
