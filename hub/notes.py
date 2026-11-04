"""Notes as plain Markdown files, newest first, each with a title and tags.

Each note is `notes_dir/YYYY-MM-DD_HHMMSS_slug.md` with a small header:

    ---
    title: "Buy milk"
    created: "2026-11-04T10:12:03"
    source: "voice"
    tags: ["shopping"]
    tagged: false
    ---

    the words you said

The raw words are saved first. A title and tags are added a moment later by the local
model, and only if you haven't set them yourself. Because the filename starts with the
time, plain `ls` order is also note order.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_ID = re.compile(r"\A[0-9A-Za-z_\-]+\Z")
_LEADING = re.compile(r"\A\s*(?:hey,?\s+)?(?:note (?:it|this)|take (?:a )?note)\b[\s,:.\-]*", re.I)
_TRAILING = re.compile(r"[\s,.\-]*\b(?:note (?:it|this))[\s.!]*\Z", re.I)
MAX_TAGS = 5


class NoteError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Note:
    id: str
    title: str
    created: str
    source: str
    tags: list[str] = field(default_factory=list)
    tagged: bool = False
    body: str = ""

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "created": self.created,
                "source": self.source, "tags": self.tags, "tagged": self.tagged,
                "preview": self.body[:140]}

    def full(self) -> dict:
        return {**self.summary(), "body": self.body}


def clean_spoken(text: str) -> str:
    """Drop a spoken 'note it' command from either end of what was said."""
    text = _LEADING.sub("", text.strip(), count=1)
    text = _TRAILING.sub("", text, count=1)
    return text.strip()


def normalise_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[,\n]", tags.strip("[]"))
    seen, out = set(), []
    for tag in tags or []:
        tag = re.sub(r"[^a-z0-9-]", "", re.sub(r"[\s_]+", "-", str(tag).strip().lower())).strip("-")
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out[:MAX_TAGS]


def fallback_title(text: str) -> str:
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    title = " ".join(words[:8]).strip(" ,.;:-")
    if not title:
        return "Untitled note"
    return title[0].upper() + title[1:]


def slugify(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())[:6]
    return "-".join(words)[:40] or "note"


def _dump(note: Note) -> str:
    header = (f"title: {json.dumps(note.title)}\ncreated: {json.dumps(note.created)}\n"
              f"source: {json.dumps(note.source)}\ntags: {json.dumps(note.tags)}\n"
              f"tagged: {json.dumps(note.tagged)}")
    return f"---\n{header}\n---\n\n{note.body.strip()}\n"


def _value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip().strip("\"'")


def _parse(note_id: str, text: str) -> Note:
    match = _FRONT.match(text)
    meta, body = {}, text
    if match:
        for line in match.group(1).splitlines():
            key, sep, raw = line.partition(": ")
            if sep:
                meta[key.strip()] = _value(raw)
        body = match.group(2)
    return Note(id=note_id, title=str(meta.get("title") or note_id),
                created=str(meta.get("created") or ""), source=str(meta.get("source") or "text"),
                tags=normalise_tags(meta.get("tags")), tagged=bool(meta.get("tagged", False)),
                body=body.strip())


class NoteStore:
    def __init__(self, directory: str | Path, now=datetime.now):
        self.dir = Path(directory)
        self._now = now

    def _path(self, note_id: str) -> Path:
        if not _ID.match(note_id):
            raise NoteError(400, "Bad note id.")
        return self.dir / f"{note_id}.md"

    def _write(self, note: Note) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(note.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_dump(note), encoding="utf-8")
        os.replace(tmp, path)

    def add(self, text: str, source: str = "text") -> Note:
        text = text.strip()
        if not text:
            raise NoteError(400, "The note is empty.")
        moment = self._now()
        title = fallback_title(text)
        base = f"{moment:%Y-%m-%d_%H%M%S}_{slugify(title)}"
        note_id, n = base, 1
        while self._path(note_id).exists():
            n += 1
            note_id = f"{base}-{n}"
        note = Note(note_id, title, moment.strftime("%Y-%m-%dT%H:%M:%S"), source, [], False, text)
        self._write(note)
        return note

    def get(self, note_id: str) -> Note:
        path = self._path(note_id)
        if not path.exists():
            raise NoteError(404, "No such note.")
        return _parse(note_id, path.read_text(encoding="utf-8"))

    def all(self, tag: str | None = None, query: str | None = None) -> list[Note]:
        if not self.dir.exists():
            return []
        notes = []
        for path in sorted(self.dir.glob("*.md"), reverse=True):
            if not _ID.match(path.stem):
                continue
            note = _parse(path.stem, path.read_text(encoding="utf-8"))
            if tag and tag not in note.tags:
                continue
            if query and query.lower() not in f"{note.title}\n{note.body}\n{' '.join(note.tags)}".lower():
                continue
            notes.append(note)
        return notes

    def all_tags(self) -> list[dict]:
        counts: dict[str, int] = {}
        for note in self.all():
            for tag in note.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def edit(self, note_id: str, title: str | None = None, tags=None) -> Note:
        """A change made by the user. It also stops the model from overwriting it later."""
        note = self.get(note_id)
        if title is not None and title.strip():
            note.title = title.strip()
        if tags is not None:
            note.tags = normalise_tags(tags)
        note.tagged = True
        self._write(note)
        return note

    def apply_auto(self, note_id: str, title: str, tags) -> Note | None:
        """The model's title and tags, applied only if the user hasn't touched the note."""
        note = self.get(note_id)
        if note.tagged:
            return None
        if title.strip():
            note.title = title.strip()
        note.tags = normalise_tags(tags)
        note.tagged = True
        self._write(note)
        return note
