"""Dictation hotkey on the LAPTOP: hold a key, speak, let go, and the words are typed into whatever window has focus.

OFF by default (`dictation_enabled`). Everything optional is imported lazily: `pynput` (or the `keyboard` package) for the
global hotkey and the key presses, `sounddevice` for the microphone (the same path as hub/wakeword.py). When a package is
missing the status says so in one plain sentence; nothing crashes.

Pipeline: hotkey down -> short beep, record -> hotkey up (or press again in toggle mode) -> LOCAL Whisper (the hub's own
transcriber, nothing goes to the cloud, ever) -> post-process (spoken punctuation, capital letter, trailing space) -> type
the text with real key events in small chunks (pynput uses SendInput on Windows) -> done chime.

Privacy: the audio lives in memory only, is handed to Whisper as an in-memory WAV and is dropped straight after: never
written to disk, never logged. The dictated TEXT is never logged or audited either (the audit line holds a character count).

Safety:
* The text goes to WHATEVER HAS FOCUS. REDLINE cannot see inside a window, so it cannot know a field is a password box.
  `dictation_blocklist` (window title / program fragments such as "password", "keepass", "bitwarden") stops it: nothing
  is recorded or typed while a blocklisted window is in front. The check uses the foreground window title on Windows and
  is skipped on other systems. If the window changes while typing, typing stops.
* Enter is never pressed: "new line" is typed as a space unless the owner enables `dictation_newline`.
* Length is capped (`dictation_max_seconds`, `dictation_max_chars`). A pause switch (`dictation_paused`) ignores the hotkey.

Voice commands (editable lists): "scratch that" backspaces the last dictated chunk when the focus did not change; with
`dictation_assistant_commands` on, "send to Jarvis ..." / "ask Jarvis ..." sends the rest to the assistant (the same
in-process route as the laptop wake listener, approvals still on the phone) and speaks the reply instead of typing.

Everything the pipeline touches is injectable (hotkey source, microphone, transcriber, keyboard, window-title provider,
clipboard, chime, clock) for tests. Routes (login needed): GET /api/dictation/status, POST /api/dictation/toggle,
PUT /api/dictation/settings.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import re
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import config as cfg
from .common import Ctx

log = logging.getLogger("hub.dictation")

RATE = 16000
FRAME = 512
CHUNK = 24                      # characters typed per burst
MIN_S = 0.25                    # a shorter hold is an accidental tap
INSTALL_HINT = "Dictation needs optional packages. Run: python -m pip install pynput sounddevice"

DEFAULT_BLOCKLIST = ["password", "keepass", "bitwarden", "1password", "lastpass"]
DEFAULT_SPOKEN = {"comma": ",", "full stop": ".", "question mark": "?", "exclamation mark": "!", "colon": ":",
                  "semicolon": ";", "new line": "\n"}
DEFAULT_SCRATCH = ["scratch that"]
DEFAULT_PREFIXES = ["send to jarvis", "ask jarvis"]
MODES = ("hold", "toggle")

_MODS = {"control": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl", "left ctrl": "ctrl", "right ctrl": "ctrl",
         "option": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "left alt": "alt", "right alt": "alt", "alt gr": "alt",
         "shift_l": "shift", "shift_r": "shift", "left shift": "shift", "right shift": "shift",
         "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd", "win": "cmd", "windows": "cmd", "super": "cmd", "meta": "cmd",
         "left windows": "cmd", "right windows": "cmd", "escape": "esc", " ": "space", "spacebar": "space"}
MODIFIERS = ("ctrl", "alt", "shift", "cmd")


class DepMissing(Exception):
    """Plain sentence: which optional package is missing."""


# ---- hotkey string and tracker ---------------------------------------------------------------------------
def norm_key(name: str) -> str:
    n = str(name or "").strip().lower()
    return _MODS.get(n, n)


def parse_hotkey(text: str) -> list[str]:
    """'Ctrl+Alt+Space' -> ['ctrl', 'alt', 'space']. Raises ValueError with a plain sentence."""
    parts = [norm_key(p) for p in str(text or "").split("+")]
    if not all(parts) or not 1 <= len(parts) <= 4 or len(set(parts)) != len(parts):
        raise ValueError("The hotkey must be one to four different keys joined by +, like ctrl+alt+space.")
    for p in parts:
        if not re.fullmatch(r"[a-z0-9]|f(?:[1-9]|1\d|2[0-4])|[a-z_]{2,12}", p):
            raise ValueError(f"'{p}' is not a key name I understand.")
    if len(parts) == 1 and not (re.fullmatch(r"f(?:[1-9]|1\d|2[0-4])", parts[0]) or parts[0] in ("pause", "scroll_lock", "insert", "caps_lock")):
        raise ValueError("A single key would also type itself. Use a function key (f9) or add ctrl or alt.")
    if all(p in MODIFIERS for p in parts) and len(parts) < 2:
        raise ValueError("Add a normal key to the modifier.")
    return parts


class HotkeyTracker:
    """Turns raw key down/up events into 'press' and 'release' of the combination (auto-repeat is ignored)."""

    def __init__(self, combo: list[str], emit: Callable[[str], None]):
        self.combo, self.emit, self.down, self.active = set(combo), emit, set(), False

    def event(self, kind: str, name: str) -> None:
        key = norm_key(name)
        if kind == "down":
            self.down.add(key)
            if not self.active and self.combo <= self.down:
                self.active = True
                self.emit("press")
        elif kind == "up":
            self.down.discard(key)
            if self.active and key in self.combo:
                self.active = False
                self.emit("release")


# ---- spoken punctuation and clean-up ---------------------------------------------------------------------
def clean_spoken(raw: Any) -> dict[str, str]:
    """Validated {phrase: symbol}. A symbol of '\\n' (typed two characters in the editor) means a line break."""
    if not isinstance(raw, dict) or len(raw) > 60:
        raise ValueError("The spoken punctuation list must be phrase = symbol pairs (60 at most).")
    out: dict[str, str] = {}
    for phrase, sym in raw.items():
        p = re.sub(r"\s+", " ", str(phrase or "").strip().lower())
        if not re.fullmatch(r"[a-z' ]{1,30}", p):
            raise ValueError(f"'{str(phrase)[:30]}' is not a speakable phrase (letters and spaces only).")
        s = "\n" if str(sym) in ("\n", "\\n") else str(sym)
        if s != "\n" and not (1 <= len(s) <= 8 and s.isprintable()):
            raise ValueError(f"The symbol for '{p}' must be 1 to 8 printable characters.")
        out[p] = s
    return out


def spoken_to_text(spoken: dict[str, str]) -> dict[str, str]:       # for the browser: a line break shows as \n
    return {k: ("\\n" if v == "\n" else v) for k, v in spoken.items()}


def apply_spoken(text: str, spoken: dict[str, str], newline_ok: bool) -> str:
    for phrase in sorted(spoken, key=len, reverse=True):
        sym = spoken[phrase]
        pat = re.compile(r"\s*(?:[,.;:!?]\s*)?\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b[,.;:!?]?", re.I)
        if sym == "\n":
            text = pat.sub(lambda _m: "\n" if newline_ok else " ", text)
        else:
            lead = "" if all(ch in ",.;:!?" for ch in sym) else " "      # punctuation hugs the word before it
            text = pat.sub(lambda _m, s=sym, l=lead: l + s + " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def postprocess(text: str, *, spoken: dict[str, str] | None = None, punctuate: bool = True, newline_ok: bool = False,
                add_space: bool = True, max_chars: int = 2000) -> str:
    t = str(text or "").strip()
    if not t:
        return ""
    t = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", t)                      # no control characters, no tab, no carriage return
    if punctuate and spoken:
        t = apply_spoken(t, spoken, newline_ok)
    if not newline_ok:
        t = t.replace("\n", " ")
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    t = re.sub(r"(^|[.?!]\s+|\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    t = t[:max(1, int(max_chars))].rstrip()
    if t and add_space and not t.endswith("\n"):
        t += " "
    return t


def _norm_words(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", str(text or "").lower())).strip()


def match_prefix(text: str, prefixes: list[str]) -> str | None:
    """The rest of the sentence after a command prefix ('send to jarvis ...'), or None. A bare prefix gives ''."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        words = [re.escape(w) for w in _norm_words(prefix).split()]
        if not words:
            continue
        m = re.match(r"^\W*" + r"\W+".join(words) + r"\b\W*(.*)$", str(text or ""), re.I | re.S)
        if m:
            return m.group(1).strip()
    return None


# ---- foreground window title (Windows only; elsewhere unknown) --------------------------------------------
def foreground_title() -> str:
    """'<window title> | <program>' of the focused window on Windows, '' elsewhere or on any error."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        proc = ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                size = wintypes.DWORD(520)
                path = ctypes.create_unicode_buffer(520)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    proc = Path(path.value).name
            finally:
                kernel32.CloseHandle(handle)
        return f"{buf.value} | {proc}"
    except Exception:
        return ""


def is_blocked(title: str, blocklist: list[str]) -> bool:
    low = str(title or "").lower()
    return bool(low) and any(str(b).strip().lower() in low for b in blocklist if str(b).strip())


# ---- optional backends (lazy) ----------------------------------------------------------------------------
def _pynput_name(key: Any) -> str:
    name = getattr(key, "name", None)
    if name:
        return norm_key(name)
    ch = getattr(key, "char", None)
    if ch:
        return chr(ord(ch) + 96) if ord(ch) < 32 else ch.lower()          # ctrl+letter arrives as a control character
    return ""


class PynputHotkeys:
    def __init__(self):
        try:
            from pynput import keyboard as kb
        except Exception:
            raise DepMissing(INSTALL_HINT) from None
        self._kb, self._listener = kb, None

    def start(self, on_event: Callable[[str, str], None]) -> None:
        self._listener = self._kb.Listener(on_press=lambda k: on_event("down", _pynput_name(k)),
                                           on_release=lambda k: on_event("up", _pynput_name(k)))
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None


class KeyboardPkgHotkeys:
    def __init__(self):
        try:
            import keyboard
        except Exception:
            raise DepMissing(INSTALL_HINT) from None
        self._k, self._hook = keyboard, None

    def start(self, on_event: Callable[[str, str], None]) -> None:
        self._hook = self._k.hook(lambda e: on_event("down" if e.event_type == "down" else "up", norm_key(e.name or "")))

    def stop(self) -> None:
        if self._hook is not None:
            self._k.unhook(self._hook)
            self._hook = None


def default_hotkeys() -> Any:
    for cls in (PynputHotkeys, KeyboardPkgHotkeys):
        try:
            return cls()
        except DepMissing:
            continue
    raise DepMissing(INSTALL_HINT)


class PynputTyper:
    def __init__(self):
        try:
            from pynput import keyboard as kb
        except Exception:
            raise DepMissing(INSTALL_HINT) from None
        self._kb, self._c = kb, kb.Controller()

    def type_text(self, text: str) -> None:
        self._c.type(text)                                      # key events (SendInput on Windows), not a paste

    def backspace(self, n: int) -> None:
        for _ in range(n):
            self._c.press(self._kb.Key.backspace)
            self._c.release(self._kb.Key.backspace)

    def paste(self) -> None:
        with self._c.pressed(self._kb.Key.ctrl):
            self._c.press("v")
            self._c.release("v")


class KeyboardPkgTyper:
    def __init__(self):
        try:
            import keyboard
        except Exception:
            raise DepMissing(INSTALL_HINT) from None
        self._k = keyboard

    def type_text(self, text: str) -> None:
        self._k.write(text)

    def backspace(self, n: int) -> None:
        for _ in range(n):
            self._k.send("backspace")

    def paste(self) -> None:
        self._k.send("ctrl+v")


def default_typer() -> Any:
    for cls in (PynputTyper, KeyboardPkgTyper):
        try:
            return cls()
        except DepMissing:
            continue
    raise DepMissing(INSTALL_HINT)


class TkClipboard:
    """Clipboard through the standard library (only used when the owner enables dictation_use_clipboard)."""

    def _root(self):
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        return r

    def get(self) -> str | None:
        try:
            r = self._root()
            try:
                return r.clipboard_get()
            finally:
                r.destroy()
        except Exception:
            return None

    def set(self, text: str) -> None:
        r = self._root()
        try:
            r.clipboard_clear()
            r.clipboard_append(text)
            r.update()
        finally:
            r.destroy()


def default_chime(kind: str = "start") -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound
        for f, ms in {"start": ((880, 60),), "done": ((523, 70), (784, 90)), "error": ((330, 200),)}.get(kind, ()):
            winsound.Beep(f, ms)
    except Exception:
        pass


def _wav_bytes(pcm: bytes) -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    buf.seek(0)
    return buf


def transcribe_pcm(tr: Any, pcm: bytes, language: str = "") -> str:
    """Whisper on an in-memory WAV (faster-whisper reads file objects), so the recording never touches the disk."""
    src = _wav_bytes(pcm)
    if language and hasattr(tr, "_load") and hasattr(tr, "_lock"):
        with tr._lock:
            segments, _ = tr._load().transcribe(src, language=language, vad_filter=True, beam_size=1,
                                                condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip()
    return str(tr.transcribe(src) or "").strip()


# ---- the dictation engine ---------------------------------------------------------------------------------
class Dictation:
    def __init__(self, ctx: Ctx, *, hotkey_factory: Callable[[], Any] | None = None,
                 source_factory: Callable[[str], Any] | None = None, transcriber: Any = None,
                 typer_factory: Callable[[], Any] | None = None, title_provider: Callable[[], str] | None = None,
                 assistant: Callable[..., str] | None = None, speaker: Callable[[str], None] | None = None,
                 chime: Callable[[str], None] | None = None, clipboard: Any = None,
                 sleep: Callable[[float], None] | None = None, run_async: bool = True):
        self.ctx = ctx
        self.hotkey_factory = hotkey_factory or default_hotkeys
        self.source_factory = source_factory
        self.transcriber = transcriber
        self.typer_factory = typer_factory or default_typer
        self.title_provider = title_provider or foreground_title
        self.assistant = assistant
        self.speaker = speaker
        self.chime = chime or default_chime
        self.clipboard = clipboard
        self.sleep = sleep or time.sleep
        self.run_async = run_async
        self.min_s = MIN_S
        self.state, self.message = "off", ""
        self.hotkeys: Any = None
        self.typer: Any = None
        self.last: tuple[int, str] | None = None          # (characters typed, window) for "scratch that"
        self._tracker: HotkeyTracker | None = None
        self._tracker_key = ""
        self._src: Any = None
        self._frames: list[bytes] = []
        self._rec_thread: threading.Thread | None = None
        self._stop_rec = threading.Event()
        self._recording = False
        self._busy = False
        self._lock = threading.RLock()
        self._work: list[threading.Thread] = []

    # ---- config (read live) ----
    def _c(self, name: str, default: Any) -> Any:
        v = getattr(self.ctx.config, "dictation_" + name, None)
        return default if v is None else v

    @property
    def enabled(self) -> bool:
        return self._c("enabled", False) is True

    @property
    def paused(self) -> bool:
        return self._c("paused", False) is True

    def effective(self) -> dict:
        return {"enabled": self.enabled, "paused": self.paused, "hotkey": str(self._c("hotkey", "ctrl+alt+space")),
                "mode": self._c("mode", "hold"), "language": str(self._c("language", "")),
                "punctuate": self._c("punctuate", True) is not False, "add_space": self._c("add_space", True) is not False,
                "max_seconds": int(self._c("max_seconds", 120)), "max_chars": int(self._c("max_chars", 2000)),
                "use_clipboard": self._c("use_clipboard", False) is True, "newline": self._c("newline", False) is True,
                "assistant_commands": self._c("assistant_commands", False) is True,
                "blocklist": list(self._c("blocklist", DEFAULT_BLOCKLIST)),
                "spoken": spoken_to_text(dict(self._c("spoken", DEFAULT_SPOKEN))),
                "scratch_phrases": list(self._c("scratch_phrases", DEFAULT_SCRATCH)),
                "assistant_prefixes": list(self._c("assistant_prefixes", DEFAULT_PREFIXES))}

    def status(self) -> dict:
        e = self.effective()
        return {"enabled": self.enabled, "paused": self.paused, "state": self.state, "message": self.message,
                "active": self.enabled and not self.paused and self.state in ("ready", "recording", "transcribing", "typing"),
                "recording": self._recording, "hotkey": e["hotkey"], "mode": e["mode"],
                "settings": e, "defaults": {"hotkey": "ctrl+alt+space", "blocklist": DEFAULT_BLOCKLIST,
                                            "spoken": spoken_to_text(DEFAULT_SPOKEN), "scratch_phrases": DEFAULT_SCRATCH,
                                            "assistant_prefixes": DEFAULT_PREFIXES},
                "target_note": "Text is typed into whichever window has focus when you let go of the hotkey."}

    def join(self, timeout: float = 5.0) -> None:                     # tests
        for t in list(self._work):
            t.join(timeout)

    # ---- hotkey events ----
    def on_key(self, kind: str, name: str) -> None:
        key = str(self._c("hotkey", "ctrl+alt+space"))
        if self._tracker is None or key != self._tracker_key:
            try:
                combo = parse_hotkey(key)
            except ValueError:
                return
            self._tracker, self._tracker_key = HotkeyTracker(combo, self.on_combo), key
        self._tracker.event(kind, name)

    def on_combo(self, what: str) -> None:
        if not self.enabled or self.paused or self.state in ("off", "missing_dependency", "error"):
            return
        toggle = self._c("mode", "hold") == "toggle"
        if what == "press":
            if toggle and self._recording:
                self.finish()
            elif not self._recording:
                self.begin()
        elif what == "release" and not toggle and self._recording:
            self.finish()

    # ---- recording ----
    def _blocked_now(self) -> bool:
        return is_blocked(self.title_provider(), list(self._c("blocklist", DEFAULT_BLOCKLIST)))

    def begin(self) -> None:
        with self._lock:
            if self._recording or self._busy:
                return
            if self._blocked_now():
                self.message = "Not recording: the window in front is on the dictation blocklist."
                self.chime("error")
                return
            self.chime("start")
            try:
                if self.source_factory is None:
                    if importlib.util.find_spec("sounddevice") is None:
                        raise DepMissing(INSTALL_HINT)
                    from .wakeword import SoundDeviceSource
                    self._src = SoundDeviceSource("")
                else:
                    self._src = self.source_factory("")
            except DepMissing as exc:
                self.state, self.message = "missing_dependency", str(exc)
                return
            except Exception as exc:
                self.state, self.message = "no_mic", str(exc)[:160] or "The microphone could not be opened."
                return
            self._frames, self._recording = [], True
            self._stop_rec.clear()
            self.state, self.message = "recording", ""
            self._rec_thread = threading.Thread(target=self._capture, name="dictation-mic", daemon=True)
            self._rec_thread.start()

    def _capture(self) -> None:
        src, limit = self._src, max(1, int(float(self._c("max_seconds", 120)) * RATE / FRAME))
        try:
            while not self._stop_rec.is_set():
                frame = src.read(FRAME)
                if frame:
                    self._frames.append(frame)
                if len(self._frames) >= limit:
                    self.message = "Stopped at the time limit."
                    self.finish(from_capture=True)
                    return
        except Exception:
            self._stop_rec.set()

    def finish(self, from_capture: bool = False) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self._stop_rec.set()
            t, src = self._rec_thread, self._src
            self._rec_thread = self._src = None
        if t and not from_capture:
            t.join(timeout=3)
        try:
            if src:
                src.close()
        except Exception:
            pass
        pcm, self._frames = b"".join(self._frames), []
        if len(pcm) < self.min_s * RATE * 2:
            self.state = "ready"
            return
        self._busy, self.state = True, "transcribing"
        if self.run_async:
            w = threading.Thread(target=self._process_safe, args=(pcm,), name="dictation-work", daemon=True)
            self._work = [x for x in self._work if x.is_alive()] + [w]
            w.start()
        else:
            self._process_safe(pcm)

    def _process_safe(self, pcm: bytes) -> None:
        try:
            self.process(pcm)
        except Exception as exc:
            self.state, self.message = "ready", f"Dictation failed ({type(exc).__name__})."
            self.chime("error")
            log.warning("dictation failed: %s", type(exc).__name__)     # the type only, never any text
        finally:
            pcm = b""
            self._busy = False
            if self.state in ("transcribing", "typing"):
                self.state = "ready"

    # ---- one recording in, typed text out ----
    def process(self, pcm: bytes) -> str:
        tr = self.transcriber or self.ctx.transcriber
        text = transcribe_pcm(tr, pcm, str(self._c("language", "")))
        pcm = b""                                                        # the audio is gone
        if not text:
            self.message = "Heard nothing."
            return ""
        title = self.title_provider()
        if is_blocked(title, list(self._c("blocklist", DEFAULT_BLOCKLIST))):
            self.message = "Not typing: the window in front is on the dictation blocklist."
            self.chime("error")
            return ""
        scratch = [_norm_words(p) for p in self._c("scratch_phrases", DEFAULT_SCRATCH)]
        if _norm_words(text) in scratch:
            return self._scratch(title)
        if self._c("assistant_commands", False) is True:
            rest = match_prefix(text, list(self._c("assistant_prefixes", DEFAULT_PREFIXES)))
            if rest is not None:
                return self._to_assistant(rest)
        final = postprocess(text, spoken=dict(self._c("spoken", DEFAULT_SPOKEN)), punctuate=self._c("punctuate", True) is not False,
                            newline_ok=self._c("newline", False) is True, add_space=self._c("add_space", True) is not False,
                            max_chars=int(self._c("max_chars", 2000)))
        if not final:
            return ""
        self.state = "typing"
        typed = self._type(final, title)
        if typed:
            self.last = (len(typed), title)
            self.ctx.audit("dictation", chars=len(typed))                 # a count, never the words
            if len(typed) == len(final):
                self.message = ""
            self.chime("done")
        return typed

    def _type(self, text: str, title: str) -> str:
        typer = self.typer
        if typer is None:
            raise DepMissing(INSTALL_HINT)
        if self._c("use_clipboard", False) is True:
            clip = self.clipboard or TkClipboard()
            prev = clip.get()
            clip.set(text)
            typer.paste()
            self.sleep(0.15)
            if prev is not None:
                clip.set(prev)
            return text
        done = 0
        for i in range(0, len(text), CHUNK):
            if i and self.title_provider() != title:
                self.message = "The window changed, so typing stopped."
                break
            chunk = text[i:i + CHUNK]
            typer.type_text(chunk)
            done += len(chunk)
            self.sleep(0.004)
        return text[:done]

    def _scratch(self, title: str) -> str:
        if not self.last or self.last[1] != title:
            self.message = "Nothing to scratch here (the window changed or nothing was typed)."
            return ""
        n, _ = self.last
        self.last = None
        self.typer.backspace(n)
        self.message = ""
        self.chime("done")
        return ""

    def _to_assistant(self, rest: str) -> str:
        if not rest or self.assistant is None:
            self.message = "Say what to ask after 'send to Jarvis'." if not rest else "The assistant is not available."
            return ""
        self.ctx.audit("dictation_assistant", chars=len(rest))
        self.state = "thinking"
        speak = self.speaker
        if speak is None:
            from .wakeword import default_speaker
            speak = default_speaker
        reply = self.assistant(rest, "jarvis", speak)
        if reply:
            speak(reply)
            if self.speaker is None:
                try:
                    from .wakeword import speak_argv
                    if speak_argv() is None:                              # nothing can talk here: push it instead
                        self.ctx.notify("Jarvis", body=str(reply)[:300], level="info", link=None, source="dictation")
                except Exception:
                    pass
        self.chime("done")
        return ""

    # ---- lifecycle ----
    def start(self) -> None:
        with self._lock:
            if self.hotkeys is not None:
                return
            self.state, self.message = "starting", ""
            try:
                if self.source_factory is None and importlib.util.find_spec("sounddevice") is None:
                    raise DepMissing(INSTALL_HINT)
                self.typer = self.typer_factory()
                hk = self.hotkey_factory()
                hk.start(self.on_key)
            except DepMissing as exc:
                self.state, self.message = "missing_dependency", str(exc)
                return
            except Exception as exc:
                self.state, self.message = "error", f"The hotkey listener could not start ({type(exc).__name__})."
                return
            self.hotkeys = hk
            self.state = "paused" if self.paused else "ready"

    def stop(self) -> None:
        with self._lock:
            hk, self.hotkeys = self.hotkeys, None
        if self._recording:
            self.finish()
        if hk is not None:
            try:
                hk.stop()
            except Exception:
                pass
        self.state, self.message, self._tracker = "off", "", None

    def refresh(self) -> None:
        """After a settings change: start or stop to match, and show paused in the status."""
        if self.enabled and self.hotkeys is None:
            self.start()
        elif not self.enabled and self.hotkeys is not None:
            self.stop()
        elif self.hotkeys is not None:
            self.state = "paused" if self.paused else ("ready" if self.state == "paused" else self.state)


# ---- settings validation -----------------------------------------------------------------------------------
def _bool(v: Any, key: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{key} must be true or false.")
    return v


def _words(v: Any, key: str, limit: int = 40) -> list[str]:
    if not isinstance(v, list) or len(v) > limit:
        raise ValueError(f"{key} must be a list of at most {limit} short phrases.")
    out = []
    for x in v:
        s = re.sub(r"\s+", " ", str(x or "").strip().lower())
        if not s:
            continue
        if len(s) > 40 or not s.isprintable():
            raise ValueError(f"{key}: '{s[:20]}...' is too long.")
        if s not in out:
            out.append(s)
    return out


def clean_settings(body: Any) -> dict:
    if not isinstance(body, dict) or not body:
        raise ValueError("Send an object with the settings to change.")
    out: dict[str, Any] = {}
    for key, v in body.items():
        if key in ("enabled", "paused", "punctuate", "add_space", "use_clipboard", "newline", "assistant_commands"):
            out["dictation_" + key] = _bool(v, key)
        elif key == "hotkey":
            parse_hotkey(v)
            out["dictation_hotkey"] = "+".join(parse_hotkey(v))
        elif key == "mode":
            if v not in MODES:
                raise ValueError("mode must be hold or toggle.")
            out["dictation_mode"] = v
        elif key == "language":
            if not isinstance(v, str) or not re.fullmatch(r"([a-z]{2,3}(-[A-Za-z]{2,4})?)?", v.strip()):
                raise ValueError("language must be a short code like en, or empty to detect.")
            out["dictation_language"] = v.strip()
        elif key in ("max_seconds", "max_chars"):
            low, high = (5, 600) if key == "max_seconds" else (50, 10000)
            if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
                raise ValueError(f"{key} must be a whole number from {low} to {high}.")
            out["dictation_" + key] = v
        elif key == "blocklist":
            out["dictation_blocklist"] = _words(v, key, 60)
        elif key == "scratch_phrases":
            out["dictation_scratch_phrases"] = _words(v, key, 10)
        elif key == "assistant_prefixes":
            out["dictation_assistant_prefixes"] = _words(v, key, 10)
        elif key == "spoken":
            out["dictation_spoken"] = clean_spoken(v)
        else:
            raise ValueError(f"'{key}' is not a dictation setting.")
    return out


def _persist(ctx: Ctx, changes: dict) -> None:
    for k, v in changes.items():
        setattr(ctx.config, k, v)
    path = getattr(ctx, "config_path", None)
    if not path:
        return
    try:
        disk = cfg.load(Path(path))
        for k, v in changes.items():
            setattr(disk, k, v)
        cfg.save(disk, Path(path))
    except Exception:
        pass                                                  # still applies until the hub restarts


def doctor_line(config=None) -> tuple:
    if getattr(config, "dictation_enabled", False) is not True:
        return (True, "dictation hotkey: off (optional)")
    have = lambda m: importlib.util.find_spec(m) is not None     # noqa: E731
    missing = [n for n, ok in (("pynput or keyboard", have("pynput") or have("keyboard")), ("sounddevice", have("sounddevice"))) if not ok]
    return (False, f"dictation hotkey: missing {', '.join(missing)}. {INSTALL_HINT}") if missing else (True, "dictation hotkey: on")


# ---- module wiring -------------------------------------------------------------------------------------
def make_router(ctx: Ctx, *, engine: Dictation | None = None) -> APIRouter:
    holder: dict[str, Any] = {"loop": None}
    eng = engine or Dictation(ctx)
    if eng.assistant is None:
        try:
            from .wakeword import default_assistant
            eng.assistant = default_assistant(ctx, lambda: holder["loop"])
        except Exception:
            eng.assistant = None
    ctx.dictation = eng                 # type: ignore[attr-defined]
    ctx.dictation_holder = holder       # type: ignore[attr-defined]
    router = APIRouter(prefix="/api/dictation", dependencies=[Depends(ctx.need_auth)])

    @router.get("/status")
    def status():
        return eng.status()

    @router.post("/toggle")
    def toggle(body: dict = Body(default={})):
        body = body if isinstance(body, dict) else {}
        changes: dict[str, bool] = {}
        for key in ("enabled", "paused"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise HTTPException(400, f"{key} must be true or false.")
                changes["dictation_" + key] = body[key]
        if not changes:
            changes["dictation_enabled"] = not eng.enabled
        _persist(ctx, changes)
        ctx.audit("dictation_toggle", **{k.replace("dictation_", ""): v for k, v in changes.items()})
        eng.refresh()
        return eng.status()

    @router.put("/settings")
    def put_settings(body: Any = Body(default=None)):
        try:
            changes = clean_settings(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        _persist(ctx, changes)
        ctx.audit("dictation_settings", keys=sorted(k.replace("dictation_", "") for k in changes))
        eng.refresh()
        return eng.status()

    return router


async def start(ctx: Ctx):
    eng = getattr(ctx, "dictation", None)
    if eng is None:
        return None
    ctx.dictation_holder["loop"] = asyncio.get_running_loop()    # type: ignore[attr-defined]
    if eng.enabled:
        await asyncio.to_thread(eng.start)

    async def stop():
        await asyncio.to_thread(eng.stop)
    return stop
