"""Small things the feature modules (work, study, english) share."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from .llm import LLMError


@dataclass
class Ctx:
    """What a feature module gets from the server. Build routers with `make_router(ctx)`."""
    config: Any
    model_for: Callable[[str], Any]   # "local" | "claude" -> client with async stream() and complete()
    transcriber: Any                  # .transcribe(path) -> str, .transcribe_detailed(path) -> dict
    notes: Any                        # hub.notes.NoteStore
    need_auth: Callable               # FastAPI dependency; pass as dependencies=[Depends(ctx.need_auth)]
    audit: Callable[..., None]        # audit("event", key=value...)


def http_from(exc: Exception) -> HTTPException:
    """A model problem as a 503 whose message tells the user what to do."""
    return HTTPException(503, str(exc))


def parse_json(text: str) -> Any:
    """The first JSON object or array in a model reply (models often wrap it in prose or fences)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("The model did not return usable JSON.")


async def ask_json(model, system: str, user: str, retries: int = 1) -> Any:
    """Ask for JSON, and try once more if the reply can't be parsed. Raises LLMError."""
    last = None
    for _ in range(retries + 1):
        reply = await model.complete(system, user, json_mode=True)
        try:
            return parse_json(reply)
        except ValueError as exc:
            last = exc
            user += "\n\nYour last reply was not valid JSON. Reply with the JSON only."
    raise LLMError(f"The model's answer could not be read ({last}). Try again or use Claude.")


class JsonFile:
    """A small JSON document on disk, written atomically so a crash can't leave half a file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, default: Any) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
