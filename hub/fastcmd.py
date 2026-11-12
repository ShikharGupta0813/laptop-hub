"""Direct commands for Full Control mode: "run git status", "open chrome", "type hello".

Only a few explicit verbs are recognised, and only when the rest of the sentence clearly is a command, a
configured app or a link. Anything else returns None and goes through the normal AI loop. No AI is involved
here: the match is plain pattern code, and the tool it names still goes through the assistant's own checks.
"""
from __future__ import annotations

import re
from typing import Any

SHELL_WORDS = r"(?:the\s+|a\s+|my\s+)?(?:terminal|shell|powershell|command\s+prompt|command\s+line|cmd|console)"
# First words that make a bare "run <x>" a real command (without them, "run through my schedule" is just English).
KNOWN_PROGRAMS = {
    "git", "ls", "dir", "pwd", "cd", "cat", "type", "echo", "whoami", "hostname", "ipconfig", "ifconfig", "ping",
    "tracert", "traceroute", "nslookup", "netstat", "tasklist", "taskkill", "systeminfo", "ver", "date", "time", "uptime",
    "df", "du", "ps", "top", "free", "uname", "which", "where", "tree", "mkdir", "rmdir", "rm", "mv", "cp", "copy", "move",
    "del", "touch", "head", "tail", "grep", "find", "findstr", "curl", "wget", "ssh", "scp", "python", "python3", "pip",
    "pip3", "py", "pytest", "node", "npm", "npx", "yarn", "pnpm", "deno", "bun", "cargo", "rustc", "go", "dotnet", "java",
    "javac", "mvn", "gradle", "make", "cmake", "docker", "docker-compose", "kubectl", "ollama", "winget", "choco",
    "scoop", "apt", "apt-get", "brew", "sudo", "powershell", "pwsh", "cmd", "wsl", "bash", "sh", "get-process",
    "get-childitem", "get-service", "get-content", "tailscale", "nvidia-smi", "sfc", "chkdsk", "shutdown", "code",
}
NOT_TYPED = {"of", "a", "an", "the", "in", "is", "are", "was", "were", "your", "my", "this", "that", "it", "these",
             "those", "what", "which", "one", "two", "about", "can", "could", "do", "does", "did", "how", "why", "when",
             "and", "or", "to", "for", "on", "with", "you", "i", "we", "they", "be", "should", "would", "will"}
_END = re.compile(r"(?<=[A-Za-z0-9])[.!?]+$")


def _trim(text: str) -> str:
    text = text.strip()
    return _END.sub("", text).strip()


def _unwrap(cmd: str) -> str:
    cmd = cmd.strip()
    if len(cmd) >= 2 and cmd[0] == cmd[-1] and cmd[0] in "`\"'":
        return cmd[1:-1].strip()
    return cmd


def _looks_like_command(cmd: str) -> bool:
    first = cmd.split(None, 1)[0].lower() if cmd.split() else ""
    if not first:
        return False
    if first in KNOWN_PROGRAMS:
        return True
    return first.startswith(("./", ".\\", "/", "~/", "c:\\", "c:/")) or bool(re.match(r"^[A-Za-z]:[\\/]", first))


_RUN = re.compile(
    rf"^(?:please\s+)?(?:run|execute|exec)\s+(?:this\s+|the\s+|a\s+)?(?P<kw>command|cmd)?\s*"
    rf"(?:(?:in|on|at|from|using|with|into)\s+{SHELL_WORDS})?\s*[:,]?\s*(?P<rest>.+)$", re.I | re.S)
_IN_TERMINAL = re.compile(
    rf"^(?:please\s+)?(?:in|on|at)\s+{SHELL_WORDS}\s*[:,]?\s*(?:run|execute|type)\s+(?P<rest>.+)$", re.I | re.S)
_TRAIL_SHELL = re.compile(rf"\s+(?:in|on|at|from|using)\s+{SHELL_WORDS}\s*$", re.I)
_OPEN = re.compile(r"^(?:please\s+)?(?:open|launch|start)\s+(?:up\s+)?(?:the\s+)?(?P<what>.+?)(?:\s+(?:app|application|please))*$", re.I)
_TYPE = re.compile(r"^(?:please\s+)?type\s+(?:out\s+)?(?P<text>.+)$", re.I | re.S)
_URL = re.compile(r"^(https?://\S+)$", re.I)
MAX_FAST_CHARS = 600


def match_fast_command(text: str, apps: dict[str, Any] | None = None) -> dict | None:
    """{"tool", "args", "say", "label"} or None. `apps` is config.apps (only those names can be opened)."""
    raw = str(text or "").strip()
    if not raw or len(raw) > MAX_FAST_CHARS:
        return None
    cleaned = _trim(raw)

    m = _IN_TERMINAL.match(cleaned)
    if m:
        cmd = _unwrap(m.group("rest"))
        return _run(cmd) if cmd else None

    m = _RUN.match(cleaned)
    if m:
        rest = m.group("rest").strip()
        explicit = bool(m.group("kw")) or bool(re.search(SHELL_WORDS, cleaned.split(rest[:20])[0] if rest else "", re.I))
        trail = _TRAIL_SHELL.search(rest)
        if trail:
            rest, explicit = rest[:trail.start()].strip(), True
        cmd = _unwrap(rest)
        if cmd and (explicit or _looks_like_command(cmd)):
            return _run(cmd)
        return None

    m = _OPEN.match(cleaned)
    if m:
        what = m.group("what").strip().strip("\"'`")
        if _URL.match(what):
            return {"tool": "laptop_action", "args": {"action": "open_url", "params": {"url": what}},
                    "say": "Opening the link.", "label": f"open {what}"}
        for name in (apps or {}):
            if str(name).strip().lower() == what.lower():
                return {"tool": "laptop_action", "args": {"action": "open_app", "params": {"name": str(name)}},
                        "say": f"Opening {name}.", "label": f"open {name}"}
        return None

    m = _TYPE.match(raw)
    if m:
        body = m.group("text").strip()
        colon = re.match(r"^(?:the following|this|that|out)?\s*:\s*(?P<t>.+)$", body, re.I | re.S)
        if colon:
            body = colon.group("t").strip()
        elif body.split(None, 1)[0].lower().strip(",") in NOT_TYPED:
            return None     # "type of weather is it" is a question, not dictation
        body = _unwrap(_trim(body)) if "\n" not in body else _unwrap(body)
        if body and len(body) <= 500:
            return {"tool": "type_text", "args": {"text": body}, "say": "Typed.", "label": "type text"}
    return None


def _run(cmd: str) -> dict:
    return {"tool": "run_command", "args": {"command": cmd}, "say": "Done.", "label": f"run {cmd[:60]}"}
