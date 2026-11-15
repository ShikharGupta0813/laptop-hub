"""Extra `python -m hub doctor` checks, each returning (status, text) where status is True (ok), None (warn) or False (FIX).

Every effect (git, pip, tailscale, the hub's /api/health) goes through an injectable `Probe`, so tests never touch
the real machine. Nothing here prints a secret: the Gemini key is only checked for presence by the wizard.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable

Status = "bool | None"
PIP_ADVICE = ("Windows Application Control may be blocking pip.exe / python -m pip. Always go through the interpreter: "
              "python -m pip install -r requirements.txt -r requirements-voice.txt  (or: python -m hub fix-deps, or run scripts\\setup-all.ps1)")
REQUIREMENT_FILES = ("requirements.txt", "requirements-voice.txt", "requirements-work.txt", "requirements-laptop.txt",
                     "requirements-extra.txt", "requirements-screen.txt")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class Probe:
    def run(self, argv: list, timeout: float = 10, cwd: str | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=cwd,
                               stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return 127, "program not found"
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 126, type(exc).__name__
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    def health(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def dist_version(self, name: str) -> str | None:
        from importlib import metadata
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return None


# ---- requirements ---------------------------------------------------------------------------------------------

_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*([<>=!~][^;#]*)?(?:;\s*([^#]*))?")


def parse_requirements(text: str) -> list[tuple[str, str, str]]:
    """[(name, specifier, marker)] from a requirements file's text. Comments, options and blank lines are skipped."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _REQ.match(line)
        if m:
            out.append((m.group(1), (m.group(3) or "").strip(), (m.group(4) or "").strip()))
    return out


def _marker_applies(marker: str, platform: str) -> bool:
    if not marker:
        return True
    m = re.search(r"sys_platform\s*(==|!=)\s*[\"']([^\"']+)[\"']", marker)
    if m:
        return (platform == m.group(2)) == (m.group(1) == "==")
    return True


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _meets(version: str, spec: str) -> bool:
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        m = re.match(r"(>=|<=|==|!=|>|<|~=)\s*(.+)", part)
        if not m:
            continue
        op, want = m.group(1), _vtuple(m.group(2))
        have = _vtuple(version)
        ok = {">=": have >= want, "<=": have <= want, "==": have == want, "!=": have != want,
              ">": have > want, "<": have < want, "~=": have >= want}[op]
        if not ok:
            return False
    return True


def check_requirements(probe: Probe, root: Path | None = None, platform: str | None = None) -> list[tuple]:
    root = root or repo_root()
    platform = platform or sys.platform
    results: list[tuple] = []
    for name in REQUIREMENT_FILES:
        path = root / name
        try:
            reqs = parse_requirements(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        problems = []
        for pkg, spec, marker in reqs:
            if not _marker_applies(marker, platform):
                continue
            have = probe.dist_version(pkg)
            if have is None:
                problems.append(f"{pkg} missing")
            elif spec and not _meets(have, spec):
                problems.append(f"{pkg} {have} (needs {spec})")
        if problems:
            hint = ""
            if any(p.startswith("av ") and "needs" in p for p in problems):
                hint = " (voice returns 500 with av 17+)"
            results.append((False, f"{name}: {', '.join(problems)}{hint}. Fix: run scripts\\setup-all.ps1 (or python -m hub fix-deps)"))
        else:
            results.append((True, f"{name}: all {len(reqs)} packages installed at the right versions"))
    return results


# ---- tools the script scripts\\setup-all.ps1 installs: the same list there and here ---------------------------------

SETUP_HINT = "run scripts\\setup-all.ps1"


def check_voice(platform: str | None = None, which: Callable[[str], str | None] | None = None) -> tuple:
    """The laptop voice: Piper (best), else Windows voices (nothing to install)."""
    import shutil
    platform, which = platform or sys.platform, which or shutil.which
    try:
        piper = bool(which("piper")) or importlib.util.find_spec("piper") is not None
    except (ImportError, ValueError):
        piper = False
    if piper:
        return True, "laptop voice: Piper installed (download a voice in Settings > Voices)"
    if platform == "win32":
        return True, "laptop voice: Windows voices (SAPI) work; for a more natural voice " + SETUP_HINT + " (installs Piper)"
    return None, "laptop voice: Piper missing and no Windows voices here: " + SETUP_HINT + " or pip install piper-tts"


def check_playwright(platform: str | None = None, env: dict | None = None, home: Path | None = None) -> tuple:
    """Playwright package AND its Chromium (form filling, chat channels)."""
    import os
    try:
        have = importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        have = False
    if not have:
        return None, "playwright not installed (form filling, chat): " + SETUP_HINT
    platform, env, home = platform or sys.platform, env if env is not None else os.environ, home or Path.home()
    custom = env.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        base = Path(custom)
    elif platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "ms-playwright"
    elif platform == "darwin":
        base = home / "Library" / "Caches" / "ms-playwright"
    else:
        base = home / ".cache" / "ms-playwright"
    try:
        found = any(base.glob("chromium*"))
    except OSError:
        found = False
    return (True, "playwright and its Chromium browser installed") if found else (None, "playwright is installed but not its browser: python -m playwright install chromium (or " + SETUP_HINT + ")")


def check_cli_tools(probe: Probe, which: Callable[[str], str | None] | None = None) -> list[tuple]:
    """Node (the phone app build and some tools), agy (Antigravity CLI) and Ollama's program."""
    import shutil
    which = which or shutil.which
    out: list[tuple] = []
    out.append((True, "node found") if which("node") else (None, "node not found (only needed to build the phone app): winget install OpenJS.NodeJS.LTS"))
    agy = which("agy")
    if not agy:
        out.append((None, "agy (Antigravity CLI) not found: optional AI provider. Install Antigravity, then run `agy` once to sign in"))
    else:
        rc, _o = probe.run([agy, "--help"], 15)
        out.append((True, "agy works. If it asks you to sign in, run `agy` once in a terminal") if rc == 0
                   else (None, "agy found but `agy --help` failed: reinstall Antigravity, then run `agy` once to sign in"))
    out.append((True, "ollama program found") if which("ollama") else (None, "ollama program not found: winget install Ollama.Ollama (" + SETUP_HINT + ")"))
    return out


# ---- the rest -------------------------------------------------------------------------------------------------

def check_git(probe: Probe, root: Path | None = None) -> tuple:
    root = str(root or repo_root())
    if not (Path(root) / ".git").exists():
        return None, "code is not a git checkout, so it cannot self-update (clone it with git)"
    rc, _ = probe.run(["git", "fetch", "--quiet"], 15, root)
    offline = rc != 0
    rc, out = probe.run(["git", "rev-list", "--count", "HEAD..@{u}"], 10, root)
    if rc != 0:
        return None, "git: could not compare with GitHub" + (" (offline?)" if offline else "") + ". Try: python -m hub update"
    try:
        behind = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, "git: could not compare with GitHub. Try: python -m hub update"
    if behind:
        return None, f"git: {behind} update(s) waiting on GitHub. Fix: python -m hub update --restart (or the Update button)"
    return True, "git: up to date with GitHub" + (" (as of the last fetch; offline now)" if offline else "")


def check_hub_running(probe: Probe, port: int) -> tuple:
    if probe.health(port):
        return True, f"hub running: /api/health answers on port {port}"
    return None, f"hub not answering on port {port}. Start it: python -m hub serve (or: schtasks /Run /TN LaptopHub)"


def check_tailscale_serve(probe: Probe) -> tuple:
    rc, out = probe.run(["tailscale", "serve", "status"], 10)
    if rc == 127:
        return None, "Tailscale serve: tailscale not installed (tailscale.com/download)"
    m = re.search(r"https://[^\s]+", out or "")
    if rc == 0 and m:
        return True, f"Tailscale serve address: {m.group(0)}"
    return None, "Tailscale serve is not pointing at the hub. Fix: tailscale serve --bg 8787 (use your port)"


def check_pip(probe: Probe) -> tuple:
    rc, out = probe.run([sys.executable, "-m", "pip", "--version"], 20)
    if rc == 0:
        return True, "python -m pip works"
    return False, "python -m pip --version fails. " + PIP_ADVICE


def check_wake(config=None) -> list[tuple]:
    """Wake word / safety / redaction modules, reported only when they exist (other workers add them)."""
    out = []
    for mod, label in (("hub.wakeword", "laptop wake listener"), ("hub.safety", "safety levels"),
                       ("hub.redact", "redaction"), ("hub.usage", "usage tracking"), ("hub.dictation", "dictation hotkey")):
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            continue
        text = f"{label}: module present"
        try:
            module = importlib.import_module(mod)
            fn = getattr(module, "doctor_line", None)
            if callable(fn):
                got = fn(config)
                if isinstance(got, tuple) and len(got) == 2:
                    out.append(got)
                    continue
        except Exception:  # noqa: BLE001 - never crash the doctor
            text = f"{label}: module present but could not be checked"
            out.append((None, text))
            continue
        out.append((True, text))
    return out


WIZARD_CHECKS: list[tuple[str, str, Callable | None]] = [
    ("watchdog", "check_watchdog", None),
    ("weekly backup", "check_backup", None),
    ("updater", "check_updater", None),
    ("Gemini key", "check_gemini", None),
    ("ntfy topic", "check_phone", None),
    ("Autopilot", "check_autopilot", None),
    ("Google calendar", "check_calendar", None),
]


def wizard_lines(path: Path) -> list[tuple]:
    """One line per wizard check; a crash in one check becomes a '--' line, never a crash."""
    out = []
    try:
        from . import setup_wizard
        wiz = setup_wizard.Wizard(path)
    except Exception:  # noqa: BLE001
        return [(None, "setup checks unavailable")]
    for label, fn_name, _ in WIZARD_CHECKS:
        try:
            result = getattr(setup_wizard, fn_name)(wiz)
            status = True if result.status == "ok" else (False if result.status == "fail" and label != "Google calendar" else None)
            out.append((status, f"{label}: {result.message}"))
        except Exception:  # noqa: BLE001
            out.append((None, f"{label}: could not check"))
    return out
