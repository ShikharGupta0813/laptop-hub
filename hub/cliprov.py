"""CLI LOGIN PROVIDERS: talk to an AI through a program already logged in on this laptop (no API key, no per-token bill).
See docs/api/cliprov.md.

An owner-editable list (Settings > Providers > "CLI login"). Each entry has one kind, `cli-login`:
    argv       the command as a LIST of words, e.g. ["gemini", "-p", "Reply to the message on stdin."]. The user's prompt is
               NEVER placed here: it is written to the program's stdin (which also keeps long or private text off the
               command line, and lets Windows .cmd shims work, because cmd.exe re-parses arguments but not stdin).
    env_strip  names of environment variables removed for the child, so the program falls back to its own login
               (GEMINI_API_KEY, GOOGLE_API_KEY, ...) and can never bill an API key.
    timeout    seconds; on expiry the whole process tree is killed.
    output     "text" (stdout is the answer) or "json" (stdout is JSON; `json_path` like "response" or "result.text" finds it).
    cwd        working folder ("" = a fresh empty temporary folder).
    login_hint the sentence shown when the program says it is not logged in.
Ready-made entries: `gemini-cli` (Google's Gemini CLI, on) and `agy` (Antigravity placeholder, off: its command line is
unknown, run `agy --help` and edit the argv here). The existing claude-login provider is a separate, older thing and is not touched.

It is a normal provider named `cli:<id>`: hub/server.py `model_for("cli:<id>")` returns the client wrapped in redact.Guarded
(private details hidden on the way out and restored on the way back) and counted as `cli` in hub/usage.py.
Safety: argv list with shell=False always (a shell program or a prompt placeholder in argv is refused), output capped at 256 KB,
stderr shown only as a short secret-masked summary, the process tree killed on timeout or cancel.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import provlist
from .common import Ctx
from .llm import LLMError
from .provlist import ListError, clean

KINDS = ("cli-login",)
MAX_OUTPUT = 256 * 1024
MAX_STDERR = 16 * 1024
MAX_ARGS = 40
MAX_ANSWER = 100_000
DEFAULT_TIMEOUT = 120
TINY = "Reply with the single word: ok"
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "csh", "tcsh", "ksh", "cmd", "cmd.exe", "powershell", "powershell.exe",
          "pwsh", "pwsh.exe", "wsl", "wsl.exe"}
PLACEHOLDER = re.compile(r"\{\{?\s*(?:prompt|user|message|input|text|system|question)\s*\}?\}", re.I)
CMD_META = re.compile(r'[&|<>^%"`\x00-\x1f\x7f]')       # Windows cmd.exe re-parses these in .cmd shims
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+|\[\d+\]|\.\d+)*$")
SECRETISH = re.compile(r"(?i)(?:bearer\s+\S+|[A-Za-z0-9_\-]{28,}|(?:key|token|secret|password)\s*[=:]\s*\S+)")
NOT_FOUND = "CLI not found on PATH"
NOT_LOGGED_IN_DEFAULT = "not logged in: log in once in a terminal, then try again"
LOGIN_WORDS = ("not logged in", "login", "log in", "sign in", "signin", "authenticate", "authentication", "oauth",
               "credential", "unauthorized", "401", "403", "auth method", "gemini_api_key", "please set an auth")
LIMIT_WORDS = ("quota", "rate limit", "limit reached", "429", "too many requests", "resource_exhausted", "usage limit")

GEMINI_HINT = "not logged in: run `gemini` once in a terminal and choose Login with Google"
GEMINI_ARGV = ["gemini", "-p", "Reply to the message supplied on standard input. Reply with plain text only."]
GEMINI_NOTE = ("Uses your Google login (Google AI Pro limits apply), never an API key. Log in once: run `gemini` in a "
               "terminal and choose Login with Google. The prompt is sent on stdin; `-p` carries only a fixed instruction. "
               "Unverified default: if your version differs, run `gemini --help`, edit the argv here and press Test. "
               "For JSON add `--output-format json` to argv and set output to json with path `response`.")
AGY_ARGV = ["agy", "-p", "Reply to the message supplied on standard input. Reply with plain text only."]
AGY_NOTE = ("Antigravity CLI (Google AI Pro login, no API key). Log in once: run `agy` in a terminal and sign in. "
            "Print mode (`agy -p`) answers one prompt non-interactively; the prompt is sent on stdin and `-p` carries only a "
            "fixed instruction. Unverified default: press Test; if your version wants the prompt as an argument instead, edit "
            "the command here. Gemini CLI personal sign-in was ended by Google, so this replaces it.")


def seed() -> list[dict]:
    return [
        {"id": "gemini-cli", "kind": "cli-login", "label": "Gemini CLI (Google login)", "enabled": False,
         "settings": {"argv": list(GEMINI_ARGV), "env_strip": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"],
                      "timeout": DEFAULT_TIMEOUT, "output": "text", "json_path": "", "cwd": "",
                      "login_hint": GEMINI_HINT, "note": GEMINI_NOTE}},
        {"id": "agy", "kind": "cli-login", "label": "Antigravity (agy)", "enabled": True,
         "settings": {"argv": list(AGY_ARGV), "env_strip": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "timeout": DEFAULT_TIMEOUT,
                      "output": "text", "json_path": "", "cwd": "",
                      "login_hint": "not logged in: open `agy` once in a terminal and sign in", "note": AGY_NOTE}},
    ]


# ---- settings ----------------------------------------------------------------------------------------------------

def _words(value: Any, what: str) -> list[str]:
    if isinstance(value, str):
        value = [ln for ln in value.splitlines()]
    if not isinstance(value, list):
        raise ListError(f"The {what} must be a list.")
    return [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


def check_settings(kind: str, raw: dict) -> dict:
    argv = _words(raw.get("argv"), "command")
    if not argv:
        raise ListError("Write the command (first the program, then its words, one per line).")
    if len(argv) > MAX_ARGS or any(len(a) > 500 for a in argv):
        raise ListError("The command is too long.")
    if Path(argv[0].replace("\\", "/")).name.lower() in SHELLS:
        raise ListError("A shell can not be used here. Name the AI program itself.")
    for word in argv:
        if PLACEHOLDER.search(word):
            raise ListError("The prompt is sent on stdin and never inside the command: remove the placeholder from the command.")
        if CMD_META.search(word):
            raise ListError("A command word may not contain & | < > ^ % quotes or control characters.")
        if word.lower().startswith(("--api-key", "--apikey", "--key=")):
            raise ListError("Keys do not belong here. This kind uses the program's own login.")
    env_strip = []
    for name in _words(raw.get("env_strip", []), "list of variables"):
        if not ENV_NAME.match(name):
            raise ListError(f"'{clean(name, 40)}' is not a valid environment variable name.")
        env_strip.append(name)
    try:
        timeout = int(raw.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        raise ListError("The time limit must be a number of seconds.") from None
    if not 5 <= timeout <= 900:
        raise ListError("The time limit must be between 5 and 900 seconds.")
    output = raw.get("output", "text") or "text"
    if output not in ("text", "json"):
        raise ListError("Output must be text or json.")
    json_path = clean(raw.get("json_path"), 80).replace(" ", "")
    if output == "json" and not PATH_RE.match(json_path):
        raise ListError("For json output write the path to the answer, like response or result.text.")
    if output == "text":
        json_path = ""
    cwd = clean(raw.get("cwd"), 300)
    return {"argv": argv, "env_strip": env_strip[:30], "timeout": timeout, "output": output, "json_path": json_path,
            "cwd": cwd, "login_hint": clean(raw.get("login_hint"), 200), "note": clean(raw.get("note"), 600)}


# ---- running the program -----------------------------------------------------------------------------------------

def find_program(word: str) -> str | None:
    """The full path of argv[0] (so Windows .cmd shims resolve), or None."""
    if os.sep in word or "/" in word or (os.altsep and os.altsep in word):
        return word if os.path.isfile(word) else None
    return shutil.which(word)


def mask(text: str, limit: int = 200) -> str:
    """One short line with anything that looks like a key or token hidden."""
    line = " ".join(SECRETISH.sub("[hidden]", str(text or "")).split())
    return line[:limit]


def kill_tree(proc) -> None:
    """Stop the program and everything it started."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def dig(data: Any, path: str) -> Any:
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


class CliLoginClient:
    """A logged-in command line program as an AI client: async complete() and stream(). `name` is "cli"."""

    name = "cli"

    def __init__(self, settings: dict, ident: str = "", *, which: Callable[[str], str | None] | None = None,
                 spawn=None, base_env: dict | None = None):
        self.s = settings
        self.ident = ident
        self._which = which or find_program
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._env = base_env
        self.last: dict = {}          # what the last call saw: {"stdout": "", "stderr": "", "code": int}

    def child_env(self) -> dict:
        env = dict(self._env if self._env is not None else os.environ)
        for name in self.s.get("env_strip", []):
            env.pop(name, None)
            for key in [k for k in env if k.upper() == name.upper()]:     # Windows names are case-insensitive
                env.pop(key, None)
        return env

    def problem(self, stderr: str, code: int | None) -> LLMError:
        low = stderr.lower()
        if any(w in low for w in LIMIT_WORDS):
            return LLMError("The AI program says its usage limit is reached for now. Wait for it to reset or pick another AI.")
        if any(w in low for w in LOGIN_WORDS):
            return LLMError(self.s.get("login_hint") or NOT_LOGGED_IN_DEFAULT)
        first = mask(next((ln for ln in stderr.splitlines() if ln.strip()), ""))
        return LLMError(f"{self.s['argv'][0]} failed" + (f": {first}" if first else f" (exit code {code})") + ".")

    async def _run(self, stdin_text: str) -> str:
        argv = list(self.s["argv"])
        exe = self._which(argv[0])
        if not exe:
            raise LLMError(NOT_FOUND + f": {argv[0]}. Install it and log in once in a terminal.")
        argv[0] = exe
        timeout = float(self.s.get("timeout") or DEFAULT_TIMEOUT)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True            # its own process group so the whole tree can be killed
        own_dir = None
        cwd = self.s.get("cwd") or ""
        if not (cwd and os.path.isdir(cwd)):
            own_dir = tempfile.TemporaryDirectory(prefix="hub-cli-")
            cwd = own_dir.name
        proc = None
        try:
            try:
                proc = await self._spawn(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                         stderr=asyncio.subprocess.PIPE, cwd=cwd, env=self.child_env(), **kwargs)
            except FileNotFoundError:
                raise LLMError(NOT_FOUND + f": {argv[0]}.") from None
            except OSError as exc:
                raise LLMError(f"Couldn't start {self.s['argv'][0]} ({exc.__class__.__name__}).") from None
            out, err = bytearray(), bytearray()
            over = {"hit": False}

            async def feed():
                try:
                    proc.stdin.write(stdin_text.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            async def pull(stream, buf, cap, flag):
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        return
                    if len(buf) < cap:
                        buf += chunk[: cap - len(buf)]
                    if len(buf) >= cap and flag:
                        over["hit"] = True
                        kill_tree(proc)
                        return

            async def all_done():
                await asyncio.gather(feed(), pull(proc.stdout, out, MAX_OUTPUT, True), pull(proc.stderr, err, MAX_STDERR, False))
                await proc.wait()

            try:
                await asyncio.wait_for(all_done(), timeout)
            except asyncio.TimeoutError:
                kill_tree(proc)
                self.last = {"stdout": mask(out.decode("utf-8", "replace")), "stderr": mask(err.decode("utf-8", "replace")), "code": None}
                raise LLMError(f"{self.s['argv'][0]} took longer than {int(timeout)} seconds and was stopped.") from None
            text_out, text_err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
            self.last = {"stdout": mask(text_out), "stderr": mask(text_err), "code": proc.returncode}
            if over["hit"]:
                raise LLMError("The AI program printed too much (over 256 KB) and was stopped.")
            if proc.returncode != 0:
                raise self.problem(text_err or text_out, proc.returncode)
            return text_out
        finally:
            if proc is not None and proc.returncode is None:
                kill_tree(proc)                            # also on cancel
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:  # noqa: BLE001
                    pass
            if own_dir is not None:
                own_dir.cleanup()

    def extract(self, raw: str) -> str:
        if self.s.get("output") != "json":
            return raw.strip()[:MAX_ANSWER]
        text = raw.strip()
        start = text.find("{")
        if start < 0:
            start = text.find("[")
        try:
            data, _ = json.JSONDecoder().raw_decode(text[max(start, 0):])
        except ValueError:
            raise LLMError("The AI program did not print JSON. Check the output setting.") from None
        if isinstance(data, dict) and data.get("error") and not dig(data, self.s["json_path"]):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise self.problem(str(msg), None)
        found = dig(data, self.s["json_path"])
        if not isinstance(found, str):
            raise LLMError(f"No answer found at '{self.s['json_path']}' in the program's JSON. Check the path.")
        return found.strip()[:MAX_ANSWER]

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        message = (f"{system.strip()}\n\n" if system and system.strip() else "") + user
        if json_mode:
            message += "\n\nReply with JSON only: no code fences and no other text."
        answer = self.extract(await self._run(message))
        if not answer:
            raise LLMError("The AI program answered with nothing.")
        return answer

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        yield await self.complete(system, user)


# ---- the list, status, wiring ------------------------------------------------------------------------------------

LAST: dict[str, dict] = {}     # id -> {"ok": bool, "problem": str, "at": float}; in memory only


def migrate_agy(path: Path) -> bool:
    """Older hubs saved a placeholder `agy` entry (argv just ["agy"], switched off). Swap it for the real default
    (print mode, on) and put it first. An entry the owner edited or enabled is left alone."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        rows = data["entries"]
        idx = next(i for i, r in enumerate(rows) if r.get("id") == "agy")
        old = rows[idx]
        if old.get("enabled") is not False or old.get("settings", {}).get("argv") != ["agy"]:
            return False
    except (OSError, ValueError, KeyError, StopIteration, AttributeError, TypeError):
        return False
    new = next(e for e in seed() if e["id"] == "agy")
    rows[idx:idx + 1] = []
    rows.insert(0, new)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps({"entries": rows}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return True


def list_for(config, secrets: Any = None) -> provlist.ProviderList:
    migrate_agy(config.data_path() / "cli_providers.json")
    return provlist.ProviderList("cli", config.data_path() / "cli_providers.json", KINDS, seed, check_settings, secrets)


def status_of(entry: dict, which: Callable[[str], str | None] | None = None) -> dict:
    """{state, message, installed, program, last_ok, ready}: detection plus what the last call said."""
    found = (which or find_program)(entry["settings"]["argv"][0])
    last = LAST.get(entry["id"]) or {}
    if not found:
        state, msg = "not-found", NOT_FOUND
    elif last and not last.get("ok") and last.get("login"):
        state, msg = "not-logged-in", entry["settings"].get("login_hint") or NOT_LOGGED_IN_DEFAULT
    elif last and not last.get("ok"):
        state, msg = "failing", "Last try failed: " + last.get("problem", "")
    elif last.get("ok"):
        state, msg = "ok", "Worked lately."
    else:
        state, msg = "installed", "Installed. Press Test to check that you are logged in."
    return {"state": state, "message": msg, "installed": bool(found), "program": Path(found).name if found else "",
            "ready": bool(found) and state != "not-logged-in"}


def note_result(ident: str, ok: bool, problem: str = "", settings: dict | None = None) -> None:
    hint = (settings or {}).get("login_hint") or NOT_LOGGED_IN_DEFAULT
    LAST[ident] = {"ok": ok, "problem": mask(problem, 160), "at": time.time(),
                   "login": (not ok) and (problem == hint or problem.startswith("not logged in"))}


def client_for(config, ident: str, secrets: Any = None, **kwargs) -> CliLoginClient | None:
    entry = list_for(config, secrets).get(ident)
    if entry is None or not entry["enabled"]:
        return None
    return TrackedClient(entry, **kwargs)


class TrackedClient(CliLoginClient):
    """Same client; remembers whether the program worked so the list can show 'not logged in'."""

    def __init__(self, entry: dict, **kwargs):
        super().__init__(entry["settings"], entry["id"], **kwargs)

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        try:
            out = await super().complete(system, user, json_mode)
        except LLMError as exc:
            note_result(self.ident, False, str(exc), self.s)
            raise
        note_result(self.ident, True)
        return out


def build_guarded(config, audit: Callable[..., None], ident: str, secrets: Any = None):
    """For hub/server.py `model_for("cli:<id>")`: the client behind redaction and usage counting, or None."""
    from . import redact
    client = client_for(config, ident, secrets)
    return redact.Guarded(client, config, audit, count_as="cli") if client else None


def ready_entries(config) -> list[dict]:
    """[{id, label, ready, reason}] of the switched-on entries, for the model pickers (/api/status)."""
    out = []
    try:
        for e in list_for(config).enabled():
            st = status_of(e)
            out.append({"id": e["id"], "label": e["label"], "ready": st["ready"], "reason": st["message"]})
    except Exception:  # noqa: BLE001 - a broken file must never break /api/status
        return []
    return out


def is_enabled(config, ident: str) -> bool:
    try:
        entry = list_for(config).get(ident)
    except Exception:  # noqa: BLE001
        return False
    return bool(entry and entry["enabled"])


# ---- routes ------------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, secrets: Any = None, which=None, spawn=None) -> APIRouter:
    router = APIRouter(prefix="/api/cli-providers", dependencies=[Depends(ctx.need_auth)])
    store = list_for(ctx.config, secrets)
    fields = [
        {"key": "argv", "label": "Command (one word per line; the prompt goes on stdin, never here)", "type": "lines",
         "placeholder": "gemini\n-p\nReply to the message on stdin.", "required": True},
        {"key": "env_strip", "label": "Environment variables to remove (so the login is used, not a key)", "type": "list",
         "placeholder": "GEMINI_API_KEY, GOOGLE_API_KEY"},
        {"key": "timeout", "label": "Time limit (seconds)", "type": "number", "placeholder": "120"},
        {"key": "output", "label": "Output", "type": "select", "options": ["text", "json"]},
        {"key": "json_path", "label": "JSON path to the answer (json output only)", "type": "text", "placeholder": "response"},
        {"key": "cwd", "label": "Working folder (blank = an empty temporary folder)", "type": "text"},
        {"key": "login_hint", "label": "What to show when it is not logged in", "type": "text"}]

    def guarded(fn, *args):
        try:
            return fn(*args)
        except ListError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    def with_status(entry: dict) -> dict:
        st = status_of(entry, which)
        return {**entry, "status": {"failing": st["state"] in ("not-found", "not-logged-in", "failing"),
                                    "last_error": st["message"] if st["state"] != "ok" else "", "skip_seconds": 0,
                                    "last_ok": (LAST.get(entry["id"]) or {}).get("at") if st["state"] == "ok" else None,
                                    "message": st["message"], "state": st["state"], "installed": st["installed"]}}

    def snapshot():
        return {"entries": [with_status(e) for e in store.public()],
                "kinds": {"cli-login": {"label": "CLI login", "fields": fields, "note":
                                        "Uses a program that is already logged in on this laptop (no API key). Log in once in a terminal first."}},
                "presets": [{"label": p["label"], "kind": "cli-login", "settings": p["settings"]} for p in seed()],
                "note": "Pick it in the model picker as 'cli:<id>'. Text is redacted like other cloud calls and counted in Usage."}

    @router.get("")
    def listing():
        return snapshot()

    @router.post("")
    def add(body: dict = Body(...)):
        entry = guarded(store.add, body)
        ctx.audit("cli_provider_add", id=entry["id"])
        return {"ok": True, "entry": entry}

    @router.put("/order")
    def order(body: dict = Body(...)):
        return {"entries": guarded(store.reorder, body.get("ids"))}

    @router.post("/reset")
    def reset():
        LAST.clear()
        return {"entries": store.reset()}

    @router.put("/{ident}")
    def edit(ident: str, body: dict = Body(...)):
        LAST.pop(ident, None)
        return {"ok": True, "entry": guarded(store.update, ident, body)}

    @router.delete("/{ident}")
    def remove(ident: str):
        guarded(store.delete, ident)
        LAST.pop(ident, None)
        ctx.audit("cli_provider_delete", id=ident)
        return {"ok": True}

    @router.post("/{ident}/test")
    async def test(ident: str):
        entry = store.get(ident)                      # a switched-off entry can be tested before it is switched on
        if entry is None:
            raise HTTPException(404, "No such entry.")
        client = TrackedClient(entry, which=which, spawn=spawn)
        try:
            reply = await client.complete("You are a connection test.", TINY)
        except LLMError as exc:
            st = status_of(entry, which)
            return {"ok": False, "error": str(exc), "state": st["state"], "stdout": client.last.get("stdout", ""),
                    "stderr": client.last.get("stderr", "")}
        ctx.audit("cli_provider_test", id=ident)
        return {"ok": True, "reply": clean(reply, 80), "state": "ok", "stdout": client.last.get("stdout", "")[:200],
                "stderr": client.last.get("stderr", "")[:200]}

    return router
