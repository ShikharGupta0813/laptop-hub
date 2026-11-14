"""TTS: natural-sounding LOCAL text to speech for Jarvis, Friday and the rest. See docs/api/tts.md.

Engines (the owner picks one per assistant in Settings > Voices; nothing changes until they do):
  browser  the phone's own speechSynthesis (default and fallback; the hub makes no audio)
  sapi     Windows System.Speech voices through a fixed PowerShell argv; the text goes in on stdin, never into the argv
  piper    rhasspy/piper local neural voices (the `piper` executable or `python -m piper`, a .onnx model, WAV out)
  command  the owner's own program: argv list that reads text on stdin and writes a WAV to stdout or to a temp path
Kokoro and other ONNX engines are NOT built in: the `command` engine runs any of them without this module trusting a
model loader. Everything is local; no text leaves the laptop and no text is ever logged or audited.

Hub side: `POST /api/tts` -> audio/wav (204 for the browser engine), `POST /api/tts/stream` (sentences in, framed WAVs out),
a per-sentence disk cache of the last N phrases, Piper model downloads that only the owner starts. Laptop side: the wake
word and alerts speak through `laptop_speaker()` (engine voice, first sentence early, stoppable), falling back to the old
one-liner (`wakeword.default_speaker`). A panic hook silences everything; presence (in a meeting, focusing) keeps the
laptop quiet. Everything the code touches (subprocess runner, player, clock, downloader) is injectable for tests.

Routes (login needed): GET /api/tts/status, GET /api/tts/voices, PUT /api/tts/settings, GET|PUT /api/tts/commands,
POST /api/tts, POST /api/tts/stream, POST /api/tts/stop, GET /api/tts/piper, POST /api/tts/piper/download,
DELETE /api/tts/piper/{voice}.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from .common import Ctx, JsonFile

log = logging.getLogger("hub.tts")

ENGINES = ("browser", "sapi", "piper", "command")       # keep in step with assistants.TTS_ENGINES
ENGINE_NAMES = {"browser": "This phone's voice", "sapi": "Windows voices (SAPI)", "piper": "Piper (local neural)",
                "command": "My own command"}
MAX_TEXT = 1500                  # characters per request
MAX_SENTENCE = 400               # longer pieces are split before synthesis
CACHE_MAX_DEFAULT = 200          # phrases kept on disk
CACHE_MAX_AGE_S = 30 * 86400
SYNTH_TIMEOUT_S = 60
MAX_COMMANDS = 8
DETAILS_LINE = "I put the details on screen."
PRESENCE_QUIET = {"in_meeting", "meeting", "focus", "focusing", "dnd"}
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "wsl"}
EVAL_FLAGS = {"-c", "/c", "/k", "-command", "-encodedcommand", "-e", "-enc", "--eval", "-exec", "-ec"}
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
VOICE_RE = re.compile(r"^[\w .()'\-]{1,80}$")
PIPER_VOICE_RE = re.compile(r"^([a-z]{2,3}_[A-Z]{2})-([a-z0-9_]+)-(x_low|low|medium|high)$")
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


class TTSError(Exception):
    """Synthesis or setup problem; the message is one plain sentence for the owner (never contains the spoken text)."""


# ---- pure text helpers ---------------------------------------------------------------------------------------
def speakable_text(text: str, cap: int = MAX_TEXT) -> str:
    """Markdown to something to say: code and long tables become one 'details on screen' line, links become 'a link'."""
    t = str(text or "")
    noted = [False]

    def details(_m=None) -> str:
        if noted[0]:
            return " "
        noted[0] = True
        return f" {DETAILS_LINE} "
    t = re.sub(r"```.*?(```|$)", details, t, flags=re.S)
    lines, run = [], 0
    for line in t.split("\n"):
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            run += 1
            if run > 3:                                         # the first three items are read, the rest is on screen
                lines.append(details())
                continue
        else:
            run = 0
        if re.match(r"^\s*\|.*\|\s*$", line):
            lines.append(details())
            continue
        lines.append(line)
    t = "\n".join(lines)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*_~>|]+", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t)
    return re.sub(r"\s+", " ", t).strip()[:cap]


def split_sentences(text: str, min_chars: int = 24, max_chars: int = MAX_SENTENCE) -> list[str]:
    """Sentences in order. Fragments shorter than min_chars join the next one so the voice does not chop 'Okay.'"""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", str(text or "").strip()) if p.strip()]
    out: list[str] = []
    carry = ""
    for p in parts:
        cur = f"{carry} {p}".strip() if carry else p
        if len(cur) < min_chars:
            carry = cur
            continue
        carry = ""
        while len(cur) > max_chars:
            cut = cur.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(cur[:cut].strip())
            cur = cur[cut:].strip()
        if cur:
            out.append(cur)
    if carry:
        if out and len(carry) < min_chars:
            out[-1] = f"{out[-1]} {carry}"
        else:
            out.append(carry)
    return out


def is_wav(data: bytes | None) -> bool:
    return bool(data) and len(data) > 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


# ---- the owner's command engine: validation ---------------------------------------------------------------------
def clean_command(raw: Any) -> dict:
    """One owner-supplied command, validated. No shell, no text placeholder, at most {out} for a file output."""
    if not isinstance(raw, dict):
        raise TTSError("A command must be an object.")
    cid = str(raw.get("id") or "").strip().lower()
    if not ID_RE.match(cid):
        raise TTSError("The id must be 2 to 24 characters: letters, digits, - or _, starting with a letter.")
    name = " ".join(str(raw.get("name") or cid).split())[:40] or cid
    argv = raw.get("argv")
    if isinstance(argv, str) or not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise TTSError("argv must be a list of strings: the program first, then its arguments.")
    if len(argv) > 24 or any(not a or len(a) > 300 or "\x00" in a or "\n" in a or "\r" in a for a in argv):
        raise TTSError("argv has an empty, too long or multi-line item (at most 24 items of 300 characters).")
    prog = re.sub(r"\.(exe|bat|cmd|com)$", "", re.split(r"[\\/]", argv[0])[-1].lower())
    if prog in SHELLS:
        raise TTSError("A shell cannot be the program. Name the voice program itself; the text arrives on its stdin.")
    if any(a.lower() in EVAL_FLAGS for a in argv[1:]):
        raise TTSError("Flags that run a command string (-c, /c, -Command) are not allowed.")
    output = str(raw.get("output") or "stdout").strip().lower()
    if output not in ("stdout", "file"):
        raise TTSError("output must be 'stdout' or 'file'.")
    for a in argv:
        for ph in PLACEHOLDER_RE.findall(a):
            if ph != "{out}":
                raise TTSError("The only placeholder allowed is {out} (the WAV path). The text always comes on stdin.")
    uses_out = any("{out}" in a for a in argv[1:])
    if output == "file" and not uses_out:
        raise TTSError("With output 'file' put {out} in argv where the program takes the WAV path.")
    if output == "stdout" and uses_out:
        raise TTSError("{out} is only for output 'file'.")
    if "{out}" in argv[0]:
        raise TTSError("The program itself cannot be a placeholder.")
    lang = re.sub(r"[^A-Za-z_-]", "", str(raw.get("lang") or ""))[:10]
    return {"id": cid, "name": name, "argv": list(argv), "output": output, "lang": lang}


def clean_commands(items: Any) -> list[dict]:
    if not isinstance(items, list):
        raise TTSError("commands must be a list.")
    if len(items) > MAX_COMMANDS:
        raise TTSError(f"At most {MAX_COMMANDS} commands.")
    out = [clean_command(i) for i in items]
    if len({c["id"] for c in out}) != len(out):
        raise TTSError("Two commands have the same id.")
    return out


# ---- default process runner and player -----------------------------------------------------------------------
def default_runner(argv: list[str], *, stdin_text: str, env: dict | None = None, timeout: float = SYNTH_TIMEOUT_S) -> tuple[int, bytes]:
    """Runs argv with NO shell; the text only ever goes on stdin. Returns (returncode, stdout bytes)."""
    try:
        done = subprocess.run(argv, input=stdin_text.encode("utf-8"), capture_output=True, timeout=timeout, env=env, shell=False)
    except subprocess.TimeoutExpired:
        raise TTSError("The voice program took too long and was stopped.") from None
    except OSError as exc:
        raise TTSError(f"The voice program could not start ({type(exc).__name__}).") from None
    return done.returncode, done.stdout


class SystemPlayer:
    """Plays a WAV file on the laptop speakers and blocks until it ends. stop() cuts it off at once."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @staticmethod
    def player_argv(path: str) -> list[str] | None:
        if sys.platform == "darwin" and shutil.which("afplay"):
            return ["afplay", path]
        for exe, extra in (("paplay", []), ("aplay", ["-q"]), ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"])):
            found = shutil.which(exe)
            if found:
                return [found, *extra, path]
        return None

    def play(self, path: str) -> None:
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME)
            except Exception as exc:  # noqa: BLE001
                raise TTSError(f"The laptop could not play the sound ({type(exc).__name__}).") from None
            return
        argv = self.player_argv(path)
        if argv is None:
            raise TTSError("No audio player found on this laptop (afplay, paplay, aplay or ffplay).")
        try:
            with self._lock:
                self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._proc.wait(timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TTSError(f"The laptop could not play the sound ({type(exc).__name__}).") from None
        finally:
            self._proc = None

    def stop(self) -> None:
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:  # noqa: BLE001
                pass
            return
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass


# ---- the phrase cache ----------------------------------------------------------------------------------------
class PhraseCache:
    """WAVs of recent sentences, named by a hash (so the folder reveals no text). Oldest go first."""

    def __init__(self, folder: Path, max_files: int = CACHE_MAX_DEFAULT, max_age_s: float = CACHE_MAX_AGE_S,
                 clock: Callable[[], float] = time.time):
        self.dir, self.max_files, self.max_age_s, self.clock = Path(folder), max(1, int(max_files)), max_age_s, clock

    @staticmethod
    def key(engine: str, voice: str, rate: float, pitch: float, text: str) -> str:
        raw = "\x1f".join([engine, voice, f"{rate:.2f}", f"{pitch:.2f}", text])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.wav"

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            data = p.read_bytes()
            now = self.clock()
            os.utime(p, (now, now))                       # a hit counts as use: frequent phrases stay
        except OSError:
            return None
        return data if is_wav(data) else None

    def put(self, key: str, data: bytes) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f"{key}.part"
            tmp.write_bytes(data)
            os.replace(tmp, self._path(key))
            now = self.clock()
            os.utime(self._path(key), (now, now))
            self.evict()
        except OSError:
            pass                                          # no cache is fine

    def _files(self) -> list[tuple[float, Path]]:
        out = []
        try:
            for p in self.dir.glob("*.wav"):
                try:
                    out.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        except OSError:
            pass
        return sorted(out, key=lambda t: t[0])

    def evict(self) -> int:
        files, removed, now = self._files(), 0, self.clock()
        keep = []
        for mtime, p in files:
            if now - mtime > self.max_age_s:
                removed += _rm(p)
            else:
                keep.append(p)
        for p in keep[:max(0, len(keep) - self.max_files)]:
            removed += _rm(p)
        return removed

    def clear(self) -> int:
        return sum(_rm(p) for _m, p in self._files())

    def stats(self) -> dict:
        files = self._files()
        return {"phrases": len(files), "bytes": sum(p.stat().st_size for _m, p in files if p.exists()), "max": self.max_files}


def _rm(p: Path) -> int:
    try:
        p.unlink()
        return 1
    except OSError:
        return 0


# ---- piper voices ----------------------------------------------------------------------------------------------
PIPER_CATALOG = [   # suggestions only (UNVERIFIED against the live Hugging Face listing); any id of the same shape can be typed
    ("en_US-lessac-medium", "English (US), Lessac, medium"), ("en_US-lessac-low", "English (US), Lessac, low (small and quick)"),
    ("en_US-amy-medium", "English (US), Amy, medium"), ("en_US-amy-low", "English (US), Amy, low (small and quick)"),
    ("en_US-ryan-medium", "English (US), Ryan, medium"), ("en_GB-alan-medium", "English (UK), Alan, medium"),
    ("en_GB-jenny_dioco-medium", "English (UK), Jenny, medium"),
]
SIZE_HINT_MB = {"x_low": "about 20", "low": "about 60", "medium": "about 60", "high": "about 110"}      # unverified
MAX_MODEL_BYTES = 250 * 1024 * 1024
MIN_MODEL_BYTES = 1024 * 1024


def piper_urls(voice: str) -> tuple[str, str]:
    """(onnx url, json url) on Hugging Face (rhasspy/piper-voices): <family>/<lang_REGION>/<name>/<quality>/<id>.onnx[.json]."""
    m = PIPER_VOICE_RE.match(voice or "")
    if not m:
        raise TTSError("A Piper voice id looks like en_US-lessac-medium.")
    lang, name, quality = m.groups()
    base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang.split('_')[0]}/{lang}/{name}/{quality}/{voice}"
    return base + ".onnx", base + ".onnx.json"


def default_fetch(url: str, dest: Path, max_bytes: int, progress: Callable[[int, int], None]) -> None:
    """Streams an https URL to dest (a .part file). Raises TTSError on anything odd."""
    if not url.startswith("https://huggingface.co/"):
        raise TTSError("Only huggingface.co voices can be downloaded.")
    try:
        import httpx
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
            if r.status_code != 200:
                raise TTSError(f"The download was refused (status {r.status_code}).")
            if not str(r.url).startswith("https://"):
                raise TTSError("The download was redirected to a plain http address and was stopped.")
            total = int(r.headers.get("content-length") or 0)
            if total > max_bytes:
                raise TTSError("The file is bigger than the safety limit and was not downloaded.")
            got = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(65536):
                    got += len(chunk)
                    if got > max_bytes:
                        raise TTSError("The file is bigger than the safety limit and was stopped.")
                    fh.write(chunk)
                    progress(got, total)
    except TTSError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TTSError(f"The download failed ({type(exc).__name__}). Check the internet connection.") from None


# ---- the service -----------------------------------------------------------------------------------------------
@dataclass
class Synth:
    engine: str
    wav: bytes | None = None
    cached: bool = False
    reason: str = ""              # why there is no audio ("browser", "disabled", "silenced", or an error sentence)


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


class TTS:
    def __init__(self, ctx_or_config: Any, *, runner: Callable | None = None, player: Any = None, clock: Callable[[], float] | None = None,
                 fetch: Callable | None = None, platform: str | None = None, which: Callable[[str], str | None] | None = None,
                 presence: Callable[[], str] | None = None):
        self.ctx = ctx_or_config if hasattr(ctx_or_config, "config") else None
        self.config = ctx_or_config.config if self.ctx is not None else ctx_or_config
        self.runner = runner or default_runner
        self.player = player or SystemPlayer()
        self.clock = clock or time.time
        self.fetch = fetch or default_fetch
        self.platform = platform or sys.platform
        self.which = which or shutil.which
        self._presence = presence
        self.silenced = False
        self.last_error = ""
        self._synth_lock = threading.Lock()
        self._speak_lock = threading.Lock()
        self._stop = threading.Event()
        self.speaking = False
        self._voices_cache: dict[str, tuple[float, list[dict]]] = {}
        self.job: dict = {"state": "idle", "voice": "", "bytes": 0, "total": 0, "message": ""}
        self._job_thread: threading.Thread | None = None

    # ---- storage ----
    @property
    def data(self) -> Path:
        return Path(self.config.data_path()) / "tts"

    def _file(self) -> JsonFile:
        return JsonFile(self.data / "tts.json")

    def settings(self) -> dict:
        raw = self._file().load({})
        raw = raw if isinstance(raw, dict) else {}
        engine = str(raw.get("engine") or getattr(self.config, "tts_engine", "browser") or "browser")
        enabled = raw["enabled"] if isinstance(raw.get("enabled"), bool) else getattr(self.config, "tts_enabled", True) is not False
        return {"enabled": enabled, "engine": engine if engine in ENGINES else "browser", "headphones": raw.get("headphones") is True,
                "piper_exe": str(raw.get("piper_exe") or ""), "cache_phrases": int(_num(raw.get("cache_phrases"), CACHE_MAX_DEFAULT, 10, 2000)),
                "commands": self._clean_stored_commands(raw.get("commands"))}

    @staticmethod
    def _clean_stored_commands(items: Any) -> list[dict]:
        try:
            return clean_commands(items or [])
        except TTSError:
            return []

    def save_settings(self, **changes) -> dict:
        raw = self._file().load({})
        raw = dict(raw) if isinstance(raw, dict) else {}
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise TTSError("enabled must be true or false.")
            raw["enabled"] = changes["enabled"]
        if "engine" in changes:
            if changes["engine"] not in ENGINES:
                raise TTSError("The engine must be one of: " + ", ".join(ENGINES) + ".")
            raw["engine"] = changes["engine"]
        if "headphones" in changes:
            if not isinstance(changes["headphones"], bool):
                raise TTSError("headphones must be true or false.")
            raw["headphones"] = changes["headphones"]
        if "piper_exe" in changes:
            exe = str(changes["piper_exe"] or "").strip()
            if exe and (len(exe) > 300 or "\x00" in exe or "\n" in exe or not Path(exe).is_file()):
                raise TTSError("That Piper program path does not exist on this laptop.")
            raw["piper_exe"] = exe
        if "commands" in changes:
            raw["commands"] = clean_commands(changes["commands"])
        raw["version"] = 1
        self._file().save(raw)
        self._voices_cache.clear()
        return self.settings()

    @property
    def cache(self) -> PhraseCache:
        return PhraseCache(self.data / "cache", self.settings()["cache_phrases"], clock=self.clock)

    # ---- state the callers ask about ----
    @property
    def enabled(self) -> bool:
        return self.settings()["enabled"]

    def laptop_barge_enabled(self) -> bool:
        return self.settings()["headphones"]            # speakers would hear themselves, so only in headphones mode

    def silence(self) -> None:                         # panic hook
        self.silenced = True
        self.stop_speaking()

    def resume(self) -> None:
        self.silenced = False

    def stop_speaking(self) -> None:
        self._stop.set()
        try:
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass

    def quiet_by_presence(self) -> bool:
        """True when presence says in a meeting or focusing. Guarded: no hub/presence.py (or an odd one) means not quiet."""
        try:
            if self._presence is not None:
                value: Any = self._presence()
            else:
                value = None
                holder = getattr(self.ctx, "presence", None) if self.ctx is not None else None
                for obj in (holder, _presence_module()):
                    if obj is None:
                        continue
                    for name in ("current", "state", "status"):
                        fn = getattr(obj, name, None)
                        if callable(fn):
                            try:
                                value = fn(self.ctx) if obj is not holder else fn()
                            except TypeError:
                                value = fn()
                            break
                    if value is not None:
                        break
            if isinstance(value, dict):
                value = value.get("state") or value.get("mode") or value.get("status")
            return str(value or "").strip().lower() in PRESENCE_QUIET
        except Exception:  # noqa: BLE001
            return False

    # ---- engines ----
    def _piper_models(self) -> list[Path]:
        try:
            return sorted(p for p in (self.data / "piper").glob("*.onnx") if p.is_file())
        except OSError:
            return []

    def _piper_base(self) -> list[str] | None:
        exe = self.settings()["piper_exe"]
        if exe and Path(exe).is_file():
            return [exe]
        found = self.which("piper")
        if found:
            return [found]
        try:
            if importlib.util.find_spec("piper") is not None:
                return [sys.executable, "-m", "piper"]
        except (ImportError, ValueError):
            pass
        return None

    def _powershell(self) -> str | None:
        if self.platform != "win32":
            return None
        return self.which("powershell") or "powershell"

    def engine_status(self) -> list[dict]:
        out = []
        for eid in ENGINES:
            ok, note = True, ""
            if eid == "sapi":
                ok = self._powershell() is not None
                note = "" if ok else "Windows only."
            elif eid == "piper":
                have_exe, models = self._piper_base() is not None, len(self._piper_models())
                ok = have_exe and models > 0
                note = "Install piper (pip install piper-tts) and download a voice below." if not have_exe else ("Download a voice below." if not models else "")
            elif eid == "command":
                cmds = self.settings()["commands"]
                ok = bool(cmds)
                note = "" if ok else "Add a command in the list below."
            out.append({"id": eid, "name": ENGINE_NAMES[eid], "available": ok, "note": note})
        return out

    def voices(self, engine: str, fresh: bool = False) -> list[dict]:
        """[{id, name, lang}] for an engine. browser has none the hub can list (the phone knows its own)."""
        hit = self._voices_cache.get(engine)
        if hit and not fresh and self.clock() - hit[0] < 60:
            return hit[1]
        out: list[dict] = []
        if engine == "sapi" and self._powershell():
            script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name }")
            try:
                rc, stdout = self.runner([self._powershell(), "-NoProfile", "-NonInteractive", "-Command", script], stdin_text="", env=None, timeout=30)
            except TTSError:
                rc, stdout = 1, b""
            for line in stdout.decode("utf-8", "replace").splitlines() if rc == 0 else []:
                name, _, lang = line.strip().partition("|")
                if name and VOICE_RE.match(name):
                    out.append({"id": name, "name": name, "lang": lang})
        elif engine == "piper":
            for p in self._piper_models():
                m = PIPER_VOICE_RE.match(p.stem)
                out.append({"id": p.stem, "name": p.stem, "lang": m.group(1) if m else ""})
        elif engine == "command":
            out = [{"id": c["id"], "name": c["name"], "lang": c["lang"]} for c in self.settings()["commands"]]
        self._voices_cache[engine] = (self.clock(), out)
        return out

    # ---- who speaks how ----
    def profile_for(self, assistant: str | None = None, **override) -> dict:
        """{engine, voice, rate, pitch} for an assistant id, with optional per-call overrides (the Test voice button)."""
        st = self.settings()
        voice_in: dict = {}
        if assistant:
            try:
                from . import assistants as A
                entry = A.find(A.load(self.config), str(assistant))
                voice_in = dict(entry.get("voice") or {}) if entry else {}
            except Exception:  # noqa: BLE001
                voice_in = {}
        engine = str(override.get("engine") or voice_in.get("engine") or st["engine"] or "browser")
        if engine not in ENGINES:
            engine = "browser"
        voice = str(override["voice"]) if override.get("voice") is not None else str(voice_in.get("voice") or "")
        rate = _num(override.get("rate") if override.get("rate") is not None else voice_in.get("rate", 1.0), 1.0, 0.5, 2.0)
        pitch = _num(override.get("pitch") if override.get("pitch") is not None else voice_in.get("pitch", 1.0), 1.0, 0.0, 2.0)
        return {"engine": engine, "voice": voice, "rate": rate, "pitch": pitch}

    def profiles(self) -> dict:
        try:
            from . import assistants as A
            ids = [a["id"] for a in A.load(self.config)]
        except Exception:  # noqa: BLE001
            ids = ["jarvis", "friday"]
        st = self.settings()
        avail = {e["id"]: e["available"] for e in self.engine_status()}
        out = {}
        for aid in ids:
            p = self.profile_for(aid)
            p["hub"] = bool(st["enabled"] and p["engine"] != "browser" and avail.get(p["engine"]))
            out[aid] = p
        return out

    # ---- synthesis ----
    def _clean(self, text: Any) -> str:
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        if not t:
            raise TTSError("There is nothing to say.")
        if len(t) > MAX_TEXT:
            raise TTSError(f"That is too long to say in one go (at most {MAX_TEXT} characters).")
        return t

    def synthesize(self, text: str, assistant: str | None = None, **override) -> Synth:
        """One sentence (or short text) to WAV bytes through the assistant's engine, via the cache. Never raises for a
        missing engine: Synth.wav is None with a reason, and the caller falls back to the phone or the old speaker."""
        text = self._clean(text)
        prof = self.profile_for(assistant, **override)
        eng = prof["engine"]
        if self.silenced:
            return Synth(eng, reason="silenced")
        if not self.enabled:
            return Synth(eng, reason="disabled")
        if eng == "browser":
            return Synth(eng, reason="browser")
        voice = self._resolve_voice(eng, prof["voice"])
        cache = self.cache
        key = cache.key(eng, voice, prof["rate"], prof["pitch"], text)
        hit = cache.get(key)
        if hit is not None:
            return Synth(eng, hit, cached=True)
        with self._synth_lock:                         # one voice job at a time: a CPU cannot do two faster anyway
            if self.silenced:
                return Synth(eng, reason="silenced")
            try:
                wav = self._run_engine(eng, text, voice, prof["rate"], prof["pitch"])
            except TTSError as exc:
                self.last_error = str(exc)
                return Synth(eng, reason=str(exc))
        self.last_error = ""
        cache.put(key, wav)
        return Synth(eng, wav)

    def _resolve_voice(self, engine: str, voice: str) -> str:
        if voice:
            return voice
        listed = self.voices(engine)
        return listed[0]["id"] if listed else ""

    def _tmp(self, suffix: str = ".wav") -> Path:
        d = self.data / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{uuid.uuid4().hex}{suffix}"

    def _run_engine(self, engine: str, text: str, voice: str, rate: float, pitch: float) -> bytes:
        base_env = {**os.environ, "REDLINE_TTS_VOICE": voice, "REDLINE_TTS_RATE": f"{rate:.2f}", "REDLINE_TTS_PITCH": f"{pitch:.2f}"}
        out = self._tmp()
        try:
            if engine == "sapi":
                ps = self._powershell()
                if ps is None:
                    raise TTSError("Windows voices need a Windows laptop.")
                if voice and not VOICE_RE.match(voice):
                    raise TTSError("That voice name is not valid.")
                env = {**base_env, "REDLINE_TTS_OUT": str(out), "REDLINE_TTS_SAPI_RATE": str(max(-10, min(10, round((rate - 1.0) * 10))))}
                script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                          "if ($env:REDLINE_TTS_VOICE) { $s.SelectVoice($env:REDLINE_TTS_VOICE) }; "
                          "$s.Rate = [int]$env:REDLINE_TTS_SAPI_RATE; $s.SetOutputToWaveFile($env:REDLINE_TTS_OUT); "
                          "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
                rc, _o = self.runner([ps, "-NoProfile", "-NonInteractive", "-Command", script], stdin_text=text, env=env, timeout=SYNTH_TIMEOUT_S)
                return self._read_out(rc, out, None, "The Windows voice")
            if engine == "piper":
                base = self._piper_base()
                if base is None:
                    raise TTSError("Piper is not installed. Run: pip install piper-tts (or set the program in Settings > Voices).")
                models = {p.stem: p for p in self._piper_models()}
                model = models.get(voice) if voice else (next(iter(models.values())) if models else None)
                if model is None:
                    raise TTSError("That Piper voice is not downloaded. Download one in Settings > Voices.")
                argv = [*base, "--model", str(model), "--output_file", str(out), "--length_scale", f"{1.0 / rate:.2f}"]
                rc, _o = self.runner(argv, stdin_text=text, env=base_env, timeout=SYNTH_TIMEOUT_S)
                return self._read_out(rc, out, None, "Piper")
            if engine == "command":
                cmds = {c["id"]: c for c in self.settings()["commands"]}
                cmd = cmds.get(voice)
                if cmd is None:
                    raise TTSError("That command is not in your list.")
                if not (Path(cmd["argv"][0]).is_file() or self.which(cmd["argv"][0])):
                    raise TTSError("The command's program was not found on this laptop.")
                argv = [a.replace("{out}", str(out)) for a in cmd["argv"]]
                rc, stdout = self.runner(argv, stdin_text=text, env=base_env, timeout=SYNTH_TIMEOUT_S)
                return self._read_out(rc, out, stdout if cmd["output"] == "stdout" else None, "Your command")
            raise TTSError("That engine makes no audio on the laptop.")
        finally:
            _rm(out)

    @staticmethod
    def _read_out(rc: int, out: Path, stdout: bytes | None, who: str) -> bytes:
        if rc != 0:
            raise TTSError(f"{who} failed (exit code {rc}).")
        data = stdout if stdout is not None else (out.read_bytes() if out.exists() else b"")
        if not is_wav(data):
            raise TTSError(f"{who} did not produce a WAV file.")
        return data

    # ---- the laptop speaker ----
    def speak_aloud(self, text: str, persona: str | None = None, fallback: Callable[[str], None] | None = None) -> str:
        """Say text on the laptop speakers, sentence by sentence (the first starts while the rest is made).

        Returns "spoken", "suppressed" (silenced or presence says quiet), or "fallback" (the engine could not; `fallback`
        was given the text that was still unsaid, normally the old one-liner)."""
        if self.silenced or self.quiet_by_presence():
            return "suppressed"
        text = speakable_text(text)
        if not text:
            return "suppressed"
        sentences = split_sentences(text)
        prof = self.profile_for(persona)
        usable = self.enabled and prof["engine"] != "browser" and {e["id"]: e["available"] for e in self.engine_status()}.get(prof["engine"])
        if not usable:
            if fallback:
                fallback(text)
            return "fallback"
        with self._speak_lock:
            self._stop.clear()
            self.speaking = True
            try:
                return self._pipeline(sentences, persona, fallback)
            finally:
                self.speaking = False

    def _pipeline(self, sentences: list[str], persona: str | None, fallback: Callable[[str], None] | None) -> str:
        q: queue.Queue = queue.Queue(maxsize=2)
        halt = threading.Event()                       # the engine or the player broke (not the owner cutting us off)

        def produce() -> None:
            try:
                for s in sentences:
                    if self._stop.is_set() or halt.is_set():
                        break
                    r = self.synthesize(s, persona)
                    if r.wav is None:
                        q.put(("fail", None))
                        return
                    q.put(("wav", r.wav))
            finally:
                q.put(("end", None))
        t = threading.Thread(target=produce, name="hub-tts-synth", daemon=True)
        t.start()
        spoken, broken = 0, False
        while True:
            kind, payload = q.get()
            if kind == "end":
                break
            if kind == "fail":
                broken = True
                continue
            if self._stop.is_set() or halt.is_set():
                continue                               # drain: nothing more is played
            path = self._tmp()
            try:
                path.write_bytes(payload)
                self.player.play(str(path))
                spoken += 1
            except (TTSError, OSError) as exc:
                self.last_error = str(exc)
                broken = True
                halt.set()
            finally:
                _rm(path)
        t.join(timeout=5)
        if broken and not self._stop.is_set():
            rest = " ".join(sentences[spoken:])
            if fallback and rest:
                fallback(rest)
            return "fallback"
        return "spoken"

    # ---- Piper downloads (owner-initiated only) ----
    def piper_state(self) -> dict:
        models = []
        for p in self._piper_models():
            models.append({"id": p.stem, "bytes": p.stat().st_size})
        catalog = [{"id": vid, "label": label, "size_mb": SIZE_HINT_MB[PIPER_VOICE_RE.match(vid).group(3)],
                    "installed": any(m["id"] == vid for m in models)} for vid, label in PIPER_CATALOG]
        return {"models": models, "catalog": catalog, "job": dict(self.job), "installed_exe": self._piper_base() is not None,
                "note": "Voices come from huggingface.co (rhasspy/piper-voices). Sizes and the catalogue are UNVERIFIED; typically tens of megabytes each.",
                "warning": "Each voice is a download of tens of megabytes (some over 100). It only starts when you press Download."}

    def start_download(self, voice: str, expected_sha: str = "", wait: bool = False) -> dict:
        if not PIPER_VOICE_RE.match(voice or ""):
            raise TTSError("A Piper voice id looks like en_US-lessac-medium.")
        if self.job.get("state") == "running":
            raise TTSError("A voice is already downloading.")
        if expected_sha and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
            raise TTSError("sha256 must be 64 hex characters.")
        self.job = {"state": "running", "voice": voice, "bytes": 0, "total": 0, "message": "Starting…"}
        t = threading.Thread(target=self._download, args=(voice, expected_sha.lower()), name="hub-tts-download", daemon=True)
        self._job_thread = t
        t.start()
        if wait:
            t.join(timeout=120)
        return dict(self.job)

    def _download(self, voice: str, expected_sha: str) -> None:
        folder = self.data / "piper"
        onnx, meta = folder / f"{voice}.onnx", folder / f"{voice}.onnx.json"
        parts = [onnx.with_suffix(".onnx.part"), meta.with_suffix(".json.part")]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            need = MAX_MODEL_BYTES + (2 << 20)
            if shutil.disk_usage(folder).free < need:
                raise TTSError("Not enough free disk space for the voice.")
            u_onnx, u_json = piper_urls(voice)

            def prog(got: int, total: int) -> None:
                self.job.update(bytes=got, total=total)
            self.fetch(u_json, parts[1], 5 * 1024 * 1024, lambda *_a: None)
            try:
                info = json.loads(parts[1].read_text(encoding="utf-8"))
                if not isinstance(info, dict) or "audio" not in info:
                    raise ValueError
            except (OSError, ValueError):
                raise TTSError("The voice's settings file looked wrong, so nothing was kept.") from None
            self.fetch(u_onnx, parts[0], MAX_MODEL_BYTES, prog)
            size = parts[0].stat().st_size
            if size < MIN_MODEL_BYTES:
                raise TTSError("The voice file is too small to be a real model, so nothing was kept.")
            digest = hashlib.sha256()
            with open(parts[0], "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            if expected_sha and digest.hexdigest() != expected_sha:
                raise TTSError("The downloaded file does not match the checksum you gave, so nothing was kept.")
            os.replace(parts[1], meta)
            os.replace(parts[0], onnx)
            (folder / f"{voice}.onnx.sha256").write_text(digest.hexdigest() + "\n", encoding="utf-8")
            self._voices_cache.clear()
            self.job = {"state": "done", "voice": voice, "bytes": size, "total": size, "message": "Downloaded. Pick it in a voice below."}
        except TTSError as exc:
            self.job = {"state": "error", "voice": voice, "bytes": 0, "total": 0, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            self.job = {"state": "error", "voice": voice, "bytes": 0, "total": 0, "message": f"The download failed ({type(exc).__name__})."}
        finally:
            for p in parts:
                _rm(p)

    def delete_model(self, voice: str) -> bool:
        if not PIPER_VOICE_RE.match(voice or ""):
            raise TTSError("A Piper voice id looks like en_US-lessac-medium.")
        folder = self.data / "piper"
        gone = sum(_rm(folder / f"{voice}{ext}") for ext in (".onnx", ".onnx.json", ".onnx.sha256"))
        self._voices_cache.clear()
        return gone > 0

    def cleanup(self) -> None:
        self.cache.evict()
        try:
            for p in (self.data / "tmp").glob("*"):
                if self.clock() - p.stat().st_mtime > 3600:
                    _rm(p)
        except OSError:
            pass


def _presence_module():
    try:
        from . import presence                      # another worker's module; absent in many installs
        return presence
    except Exception:  # noqa: BLE001
        return None


# ---- the laptop speaker for wakeword and alerts -------------------------------------------------------------------
def laptop_speaker(ctx: Ctx, persona: Callable[[], str | None] | None = None) -> Callable[[str], None]:
    """speaker(text) for hub/wakeword.py and hub/alerts.py: the engine voice, else the old SAPI/say/espeak one-liner."""
    def legacy(text: str) -> None:
        from . import wakeword
        wakeword.default_speaker(text)

    def say(text: str) -> None:
        svc = getattr(ctx, "tts", None)
        if svc is None:
            legacy(text)
            return
        try:
            svc.speak_aloud(text, persona() if persona else None, fallback=legacy)
        except Exception:  # noqa: BLE001 - never lose the sentence because the new path broke
            log.warning("engine speech failed; using the old speaker")
            legacy(text)
    return say


def _register_panic(ctx: Ctx, svc: TTS) -> bool:
    try:
        from . import panic                         # hub/panic.py belongs to the panic-button work; absent = nothing to hook
        panic.register("voice", svc.silence, svc.resume, ctx=ctx)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- routes -----------------------------------------------------------------------------------------------------
def frame(kind: int, payload: bytes = b"") -> bytes:
    """Stream frame: 1 byte kind (0 audio/wav, 1 use the phone's voice, 2 silent, 3 error), 4 bytes big-endian length, payload."""
    return struct.pack(">BI", kind, len(payload)) + payload


def make_router(ctx: Ctx, *, service: TTS | None = None) -> APIRouter:
    svc = service or TTS(ctx)
    ctx.tts = svc                       # type: ignore[attr-defined]
    _register_panic(ctx, svc)
    router = APIRouter(prefix="/api/tts", dependencies=[Depends(ctx.need_auth)])

    def audit(event: str, **fields) -> None:
        try:
            ctx.audit(event, **fields)
        except Exception:  # noqa: BLE001
            pass

    def fail(exc: TTSError, code: int = 400):
        raise HTTPException(code, str(exc))

    def overrides(body: dict) -> dict:
        out: dict = {}
        if body.get("engine"):
            if body["engine"] not in ENGINES:
                raise HTTPException(400, "Unknown engine.")
            out["engine"] = body["engine"]
        for k in ("voice", "rate", "pitch"):
            if body.get(k) is not None:
                out[k] = body[k]
        if "voice" in out:
            out["voice"] = str(out["voice"])[:80]
        return out

    def one(body: dict) -> Response:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "Send the text to say.")
        if len(text) > MAX_TEXT:
            raise HTTPException(413, f"Too long: at most {MAX_TEXT} characters.")
        aid = body.get("assistant")
        r = svc.synthesize(text, str(aid) if aid else None, **overrides(body))
        if r.wav is not None:
            return Response(r.wav, media_type="audio/wav", headers={"X-TTS-Engine": r.engine, "X-TTS-Cached": "1" if r.cached else "0",
                                                                      "Cache-Control": "no-store"})
        if r.reason == "silenced":
            return Response(status_code=423, headers={"X-TTS-Engine": r.engine})      # panic: say nothing, do not fall back to the phone
        if r.reason in ("browser", "disabled"):
            return Response(status_code=204, headers={"X-TTS-Engine": "browser"})
        raise HTTPException(502, r.reason or "The voice engine failed.")

    @router.get("/status")
    def status():
        st = svc.settings()
        return {"enabled": st["enabled"], "engine": st["engine"], "headphones": st["headphones"], "silenced": svc.silenced,
                "engines": svc.engine_status(), "profiles": svc.profiles(), "commands": st["commands"], "piper_exe": st["piper_exe"],
                "cache": svc.cache.stats(), "speaking": svc.speaking, "last_error": svc.last_error,
                "max_text": MAX_TEXT, "quiet_now": svc.quiet_by_presence()}

    @router.get("/voices")
    def voices(engine: str = Query("sapi"), fresh: bool = Query(False)):
        if engine not in ENGINES:
            raise HTTPException(400, "Unknown engine.")
        return {"engine": engine, "voices": svc.voices(engine, fresh=fresh)}

    @router.put("/settings")
    def put_settings(body: dict = Body(default_factory=dict)):
        changes = {k: body[k] for k in ("enabled", "engine", "headphones", "piper_exe") if k in body}
        if not changes:
            raise HTTPException(400, "Nothing to change.")
        try:
            svc.save_settings(**changes)
        except TTSError as exc:
            fail(exc)
        audit("tts_settings", keys=sorted(changes))
        return status()

    @router.get("/commands")
    def get_commands():
        return {"commands": svc.settings()["commands"], "max": MAX_COMMANDS}

    @router.put("/commands")
    def put_commands(body: dict = Body(default_factory=dict)):
        if body.get("confirm") is not True:
            raise HTTPException(400, "Saving a command that runs a program needs confirm: true.")
        try:
            svc.save_settings(commands=body.get("commands"))
        except TTSError as exc:
            fail(exc)
        audit("tts_commands", ids=[c["id"] for c in svc.settings()["commands"]])
        return {"commands": svc.settings()["commands"], "max": MAX_COMMANDS}

    @router.post("")
    @router.post("/")
    def speak(body: dict = Body(default_factory=dict)):
        return one(body if isinstance(body, dict) else {})

    @router.post("/stream")
    async def stream(request: Request):
        """Lines of JSON {"text", "assistant"?} in, frames out (see frame()): the first sentence's audio leaves while the later
        ones are still being made. Browsers cannot send a request body and read the reply at once, so voice mode posts one
        sentence per POST /api/tts instead; this is for clients that already hold several sentences."""
        def handle(line: bytes) -> bytes:
            try:
                body = json.loads(line.decode("utf-8"))
                if not isinstance(body, dict) or not isinstance(body.get("text"), str) or not body["text"].strip():
                    return frame(3, b"Send lines like {\"text\": \"...\"}.")
                if len(body["text"]) > MAX_TEXT:
                    return frame(3, b"Too long.")
                r = svc.synthesize(body["text"], str(body["assistant"]) if body.get("assistant") else None, **overrides(body))
            except (ValueError, HTTPException, TTSError):
                return frame(3, b"Bad line.")
            if r.wav is not None:
                return frame(0, r.wav)
            if r.reason == "silenced":
                return frame(2)
            if r.reason in ("browser", "disabled"):
                return frame(1)
            return frame(3, r.reason.encode("utf-8")[:300])

        body = b""
        async for chunk in request.stream():           # read first: Starlette's disconnect listener competes for the body otherwise
            body += chunk
            if len(body) > 256 * 1024:
                raise HTTPException(413, "That is too much text for one stream.")
        lines = [ln for ln in body.split(b"\n") if ln.strip()][:60]

        async def gen():                                # each frame leaves as soon as its sentence is made
            for ln in lines:
                yield await run_in_threadpool(handle, ln)
        return StreamingResponse(gen(), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    @router.post("/stop")
    def stop():
        svc.stop_speaking()
        return {"ok": True}

    @router.get("/piper")
    def piper():
        return svc.piper_state()

    @router.post("/piper/download")
    def piper_download(body: dict = Body(default_factory=dict)):
        if body.get("confirm") is not True:
            raise HTTPException(400, "Downloading a voice needs confirm: true (it is tens of megabytes).")
        try:
            svc.start_download(str(body.get("voice") or ""), str(body.get("sha256") or ""))
        except TTSError as exc:
            fail(exc, 409 if "already" in str(exc) else 400)
        audit("tts_piper_download", voice=str(body.get("voice"))[:60])
        return svc.piper_state()

    @router.delete("/piper/{voice}")
    def piper_delete(voice: str):
        try:
            svc.delete_model(voice)
        except TTSError as exc:
            fail(exc)
        audit("tts_piper_delete", voice=voice[:60])
        return svc.piper_state()

    return router


async def start(ctx: Ctx):
    svc = getattr(ctx, "tts", None)
    if svc is None:
        return None
    try:
        await run_in_threadpool(svc.cleanup)
    except Exception:  # noqa: BLE001
        pass

    async def stop():
        svc.stop_speaking()
    return stop
