"""A throwaway hub with fake models, for developing and testing the phone UI.

    python -m hub.devserver --port 8799        # login token: devtoken

Uses temporary folders with sample notes, a fake model that streams canned text, a fake
speech-to-text, and a fake assistant that shows every kind of event (including an approval).
The terminal is a real shell. Nothing here touches your real files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import uvicorn

from . import config as cfg
from .auth import hash_token
from .server import Deps, create_app

DEV_TOKEN = "devtoken"


def _collect_fakes() -> dict[str, str]:
    """Canned model replies from each feature module, keyed by a piece of its system prompt."""
    import importlib
    fakes: dict[str, str] = {}
    from .server import FEATURE_MODULES
    for name in FEATURE_MODULES:
        try:
            fakes.update(getattr(importlib.import_module(f".{name}", __package__), "DEV_FAKES", {}))
        except (ModuleNotFoundError, AttributeError) as exc:
            if isinstance(exc, ModuleNotFoundError) and exc.name != f"{__package__}.{name}":
                raise
    return fakes


class FakeLLM:
    name = "local"

    def _canned(self, system):
        for marker, reply in _collect_fakes().items():
            if marker in system:
                return reply
        return None

    async def stream(self, system, user):
        canned = self._canned(system)
        if canned:
            for word in canned.split(" "):
                await asyncio.sleep(0.02)
                yield word + " "
            return
        for word in f"This is a pretend answer about: {user[-60:]!r}. ".split(" "):
            await asyncio.sleep(0.02)
            yield word + " "

    async def complete(self, system, user, json_mode=False):
        canned = self._canned(system)
        if canned:
            return canned
        return json.dumps({"title": "Pretend title", "tags": ["demo", "sample"]})

    async def models(self):
        return ["fake-model:1b"]


class FakeTranscriber:
    def transcribe(self, path):
        return "Note it. Remember to review the sharding chapter tomorrow morning."

    def transcribe_detailed(self, path):
        words = "Um so yesterday I I go to the office and uh we discuss the new plan".split()
        return {"text": " ".join(words), "duration": 6.0,
                "words": [{"word": w, "start": round(i * 0.4, 2), "end": round(i * 0.4 + 0.3, 2),
                           "prob": 0.5 if w in ("discuss", "plan") else 0.95}
                          for i, w in enumerate(words)]}


class FakeAssistant:
    """Enough behaviour to exercise the UI: text, a read-only tool, and an approval."""

    def __init__(self):
        self.waiting: dict[tuple[str, str], asyncio.Future] = {}

    async def run(self, session_id, text, provider, auto_approve, *, persona="", voice=False, context="", **_ignored):
        # persona / voice / context are accepted (the real assistant takes them) so Talk and voice mode work here.
        if voice:
            yield {"type": "text", "t": "Pretend voice reply. " + (f"(persona {persona}) " if persona else "")}
            yield {"type": "text", "t": "Done."}
            return
        yield {"type": "text", "t": "Let me look. "}
        await asyncio.sleep(0.1)
        yield {"type": "tool_start", "id": "c1", "name": "list_dir",
               "input": {"path": "~"}, "needs_approval": False}
        yield {"type": "tool_result", "id": "c1", "ok": True, "output": "notes/\nHubInbox/\nreadme.md"}
        yield {"type": "file_ref", "root": "notes", "path": "week1.md", "name": "week1.md"}
        if "run" in text.lower() or "delete" in text.lower():
            call = {"id": "c2", "name": "run_command", "input": {"command": "echo hello", "shell": "bash"}}
            yield {"type": "tool_start", **call, "needs_approval": not auto_approve}
            approved = auto_approve
            if not auto_approve:
                yield {"type": "approval_needed", **call}
                future = asyncio.get_running_loop().create_future()
                self.waiting[(session_id, "c2")] = future
                approved = await future
            if approved:
                yield {"type": "tool_result", "id": "c2", "ok": True, "output": "hello"}
            else:
                yield {"type": "tool_result", "id": "c2", "ok": False, "denied": True,
                       "output": "The user declined."}
        yield {"type": "text", "t": "Done. This was a pretend answer."}

    def resolve(self, session_id, call_id, approve):
        future = self.waiting.pop((session_id, call_id), None)
        if future and not future.done():
            future.set_result(approve)
            return True
        return False

    def stop(self, session_id):
        return False

    def reset(self, session_id):
        pass

    def grants(self):
        return []

    def revoke(self, grant_id):
        return False

    def revoke_all(self):
        return 0


class DemoSystem:
    """A stand-in for the operating system, so the phone UI can be tried without locking, killing,
    shutting down or opening anything on the machine running the dev server."""

    platform = "win32"  # so every action shows as available

    def __init__(self):
        from .laptop import System
        self._real = System()
        self.log: list[tuple] = []

    def which(self, name):
        return f"C:\\demo\\{name}.exe"

    def has_pillow(self):
        return True

    def has_display(self):
        return True

    def run(self, argv, input_text=None, timeout=20, cwd=None):
        self.log.append(("run", argv))
        if any("Get-Clipboard" in a for a in argv):
            return 0, "demo clipboard text\n", ""
        return 0, "", ""

    def launch(self, argv):
        self.log.append(("launch", argv))

    def open_default(self, path):
        self.log.append(("open", path))

    def open_url(self, url):
        self.log.append(("url", url))
        return True

    def start_app(self, target, args):
        self.log.append(("app", target, args))

    def grab_screen(self):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (1280, 720), (24, 32, 48))
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, 1240, 80), fill=(60, 80, 120))
        draw.text((60, 52), "Demo screenshot (nothing real is captured)", fill=(255, 255, 255))
        return image

    def press_media_key(self, name, times=1):
        self.log.append(("media", name, times))

    def protected_pids(self):
        return self._real.protected_pids()

    def terminate_process(self, pid):
        self.log.append(("terminate", pid))
        return "terminated"


def _patch_for_demo(root: Path) -> Path:
    """Use the demo operating system, and a fake Jarvis that only sleeps."""
    from . import jarvis, laptop

    original_laptop = laptop.make_router
    laptop.make_router = lambda ctx, system=None: original_laptop(ctx, system=DemoSystem())
    folder = root / "jarvis"
    folder.mkdir()
    (folder / "jarvis.py").write_text(
        "import time\nprint('demo jarvis running', flush=True)\n"
        "with open('jarvis.log', 'a') as f:\n    f.write('Jarvis started (demo)\\n')\n"
        "time.sleep(3600)\n", encoding="utf-8")
    (folder / "remote_inbox.py").write_text("# demo: marks Jarvis as able to take remote commands\n", encoding="utf-8")
    original_jarvis = jarvis.make_router
    jarvis.make_router = lambda ctx, command=None, launcher=None: original_jarvis(
        ctx, command=lambda d: [sys.executable, "jarvis.py"], launcher=launcher)
    return folder


def build(port: int):
    os.environ.setdefault("HUB_DEMO", "1")  # feature modules fill themselves with sample data
    os.environ.setdefault("HUB_WORK_DEMO", "1")  # Work tab shows sample tasks, meetings and contacts
    root = Path(tempfile.mkdtemp(prefix="hub-dev-"))
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "docs" / "week1.md").write_text(
        "# Week 1\n\nAn **LLM** predicts the next token.\n\n- tokens\n- embeddings\n\n"
        "```python\nprint('hi')\n```\n\n![pic](pic.png)\n\n[next](sub/week2.md)\n", encoding="utf-8")
    (root / "docs" / "sub" / "week2.md").write_text("# Week 2\n\nHallucinations and sampling.\n", encoding="utf-8")
    (root / "docs" / "evil.md").write_text("# Evil\n\n<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n", encoding="utf-8")
    (root / "docs" / "data.txt").write_text("plain text file\n", encoding="utf-8")
    (root / "docs" / "pic.png").write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"))
    jarvis_folder = _patch_for_demo(root)
    config = cfg.Config(
        token_hash=hash_token(DEV_TOKEN), session_secret=secrets.token_bytes(32),
        roots={"docs": str(root / "docs"), "notes": str(root / "notes"), "inbox": str(root / "inbox")},
        notes_dir=str(root / "notes"), inbox_dir=str(root / "inbox"),
        data_dir=str(root / "data"),  # never the real ~/.laptop-hub/data
        ccsessions_dir=str(root / "ccsessions"),  # never the real ~/.claude/projects (it holds real session names)
        port=port, jarvis_dir=str(jarvis_folder),
        apps={"Notepad": {"target": "notepad.exe", "args": []}, "Calculator": {"target": "calc.exe", "args": []}},
        scripts={"hello": {"label": "Say hello", "argv": ["echo", "hello from a button"]}})
    deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(),
                assistant=FakeAssistant())
    return create_app(config, deps, audit_path=root / "audit.log"), root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    app, root = build(args.port)
    print(f"Dev hub on http://127.0.0.1:{args.port}   token: {DEV_TOKEN}   data: {root}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
