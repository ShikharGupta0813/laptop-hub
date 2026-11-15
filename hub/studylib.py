"""Study library: the owner's own material (PDFs, Word, slides, text, web pages, screenshots) kept for the Learn tab.

Material gets in three ways: uploaded from the phone or the laptop's browser, picked from the hub's shared folders
(individual files only; the Files tab's roots and permission rules apply, and the hub reads them in place, nothing is
uploaded anywhere), or pasted as text or a web address. Each document is read by a worker thread (never on the event
loop), cut into pages and search chunks, and, when the embedding model is running, embedded; without it the chunks
are searched by keywords (BM25). Progress is visible and a running job can be cancelled.

Every Learn mode takes a *library* source: `{"type": "library", "ids": [...], "page_from", "page_to"}`. The text it
yields is study material, fenced and treated as data by study.py like any other source.

Routes: /api/learn/library/*  (docs/api/studylib.md).  Tools: study_library_list, study_import_path.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import files, kb, study
from .common import Ctx, JsonFile, InProcessClient

MAX_UPLOAD_BYTES = 100 * 1024 * 1024      # one file
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PAGES = 3000
MAX_DOC_CHARS = 12_000_000
MAX_DOCS = 1000
MAX_IMPORT_FILES = 200
MAX_XML = 20 * 1024 * 1024
VIRTUAL_PAGE = 3000                        # characters per "page" of a document that has no real pages
CHUNK = 1000
MAX_CHUNKS = 20_000
MAX_EMBED_CHUNKS = 3000
EMBED_BATCH = 32
PAGE_EXTS = {"pdf", "pptx", "epub"}
TEXT_EXTS = {"txt", "text", "md", "markdown", "rst", "org", "csv", "log", "html", "htm", "xhtml"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
EXTS = sorted({"pdf", "docx", "pptx", "epub", *TEXT_EXTS, *IMAGE_EXTS})
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
              "gif": "image/gif", "bmp": "image/bmp"}
OCR_PROMPT = ("Transcribe all the text in this image exactly as written, in reading order. Reply with the text "
              "only. If there is no readable text, reply with NO TEXT.")

# Tests set these (and nothing else does). EMBEDDER: (texts, config) -> vectors, like kb.EMBEDDER.
EMBEDDER: Optional[Callable] = None
OCR: Optional[Callable[[bytes, str], str]] = None


class Cancelled(Exception):
    pass


def ext_of(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def _iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def human_bytes(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def check_file(name: str, size: int) -> str:
    """The extension of an acceptable file; raises SourceError with a plain sentence otherwise."""
    ext = ext_of(name)
    if ext not in EXTS:
        raise study.SourceError(415, f"'.{ext or '?'}' files can't be added. Use: " + ", ".join(
            "." + e for e in ("pdf", "docx", "pptx", "epub", "txt", "md", "html", "png", "jpg")) + " and similar.")
    limit = MAX_IMAGE_BYTES if ext in IMAGE_EXTS else MAX_UPLOAD_BYTES
    if size > limit:
        raise study.SourceError(413, f"That file is {human_bytes(size)}; the limit is {human_bytes(limit)}.")
    if size <= 0:
        raise study.SourceError(422, "That file is empty.")
    return ext


# ---- reading files (worker thread) ---------------------------------------------------------------

def virtual_pages(text: str, size: int = VIRTUAL_PAGE) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    pages, pos = [], 0
    while pos < len(text):
        end = kb._cut(text, pos, size)
        piece = text[pos:end].strip()
        if piece:
            pages.append(piece)
        pos = end if end > pos else pos + size
    return pages


def _safe_xml(data: bytes):
    if b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data.upper():
        raise ValueError("unsafe xml")
    return ET.fromstring(data)


def _zip_member(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_XML:
        raise ValueError("part too large")
    return z.read(info)


def read_pdf(path: Path, progress, cancel) -> list[str]:
    try:
        import pypdf
    except ImportError:
        raise study.SourceError(415, "Reading PDFs needs the pypdf package. Run `pip install pypdf` on the laptop, "
                                     "restart the hub, and try again.") from None
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise study.SourceError(422, "That PDF is password protected.")
        total = min(len(reader.pages), MAX_PAGES)
        pages, chars = [], 0
        for i in range(total):
            if cancel.is_set():
                raise Cancelled
            try:
                piece = (reader.pages[i].extract_text() or "").strip()
            except Exception:       # one bad page must not lose the other 400
                piece = ""
            pages.append(piece)
            chars += len(piece)
            progress((i + 1) / total * 0.8)
            if chars > MAX_DOC_CHARS:
                break
    except (study.SourceError, Cancelled):
        raise
    except Exception:
        raise study.SourceError(422, "Couldn't read that PDF. It may be damaged.") from None
    if not any(pages):
        raise study.SourceError(422, "That PDF has no text in it. A scanned PDF needs OCR first (add its pages as "
                                     "images instead).")
    return pages


def read_docx(path: Path) -> list[str]:
    try:
        text = kb.docx_to_text(path)
    except Exception:
        raise study.SourceError(422, "Couldn't read that Word file. It may be damaged or an old .doc.") from None
    return virtual_pages(text)


def read_pptx(path: Path, cancel) -> list[str]:
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.findall(r"\d+", n)[-1]))
            slides = []
            for name in names[:MAX_PAGES]:
                if cancel.is_set():
                    raise Cancelled
                root = _safe_xml(_zip_member(z, name))
                paras = []
                for para in root.iter(f"{ns}p"):
                    line = "".join(t.text or "" for t in para.iter(f"{ns}t")).strip()
                    if line:
                        paras.append(line)
                slides.append("\n".join(paras))
    except Cancelled:
        raise
    except Exception:
        raise study.SourceError(422, "Couldn't read that PowerPoint file. It may be damaged or an old .ppt.") from None
    return slides


def read_epub(path: Path, cancel) -> list[str]:
    try:
        with zipfile.ZipFile(path) as z:
            order: list[str] = []
            try:
                container = _safe_xml(_zip_member(z, "META-INF/container.xml"))
                opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
                opf = _safe_xml(_zip_member(z, opf_path))
                base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
                manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("}item")}
                for e in opf.iter():
                    if e.tag.endswith("}itemref") and manifest.get(e.get("idref")):
                        order.append(base + manifest[e.get("idref")].split("#")[0])
            except Exception:
                order = []
            if not order:
                order = sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
            chapters = []
            for name in order[:MAX_PAGES]:
                if cancel.is_set():
                    raise Cancelled
                try:
                    raw = _zip_member(z, name).decode("utf-8", errors="replace")
                except (KeyError, ValueError):
                    continue
                text = study.html_to_text(raw)[1]
                if text:
                    chapters.append(text)
    except Cancelled:
        raise
    except Exception:
        raise study.SourceError(422, "Couldn't read that EPUB. It may be damaged or DRM protected.") from None
    return chapters


def read_text_file(path: Path, ext: str) -> list[str]:
    with open(path, "rb") as f:
        raw = f.read(MAX_DOC_CHARS)
    if b"\x00" in raw[:4096]:
        raise study.SourceError(415, "That isn't a text file.")
    text = raw.decode("utf-8", errors="replace")
    if text.count("�") > max(8, len(text) // 20):
        raise study.SourceError(415, "That file isn't UTF-8 text.")
    if ext in ("html", "htm", "xhtml"):
        text = study.html_to_text(text)[1]
    return virtual_pages(text)


def default_ocr(ctx: Ctx) -> Callable[[bytes, str], str]:
    """OCR for screenshots: Tesseract if installed, else the first usable vision provider (Settings > Vision)."""
    def run(data: bytes, mime: str) -> str:
        try:
            import pytesseract
            from PIL import Image
            text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
            if text.strip():
                return text
        except Exception:
            pass
        try:
            from . import vision
            model = vision.engine_for(ctx)._model()
        except Exception:
            model = None
        if model is None:
            raise study.SourceError(415, "Reading images needs an OCR tool or a vision provider. Install Tesseract "
                                         "(pip install pytesseract) or add a vision provider in Settings.")
        try:
            reply = asyncio.run(_await(model.ask_image(OCR_PROMPT, "Transcribe this image.", data, mime)))
        except Exception:
            raise study.SourceError(502, "The vision provider couldn't read that image. Try again.") from None
        text = str(reply or "").strip()
        return "" if text.upper().startswith("NO TEXT") else text
    return run


async def _await(value):
    import inspect
    return await value if inspect.isawaitable(value) else value


def extract(path: Path, ext: str, progress, cancel, ocr) -> tuple[list[str], str]:
    """(pages, unit). `unit` is what a page number means: page, slide, chapter or section."""
    if ext == "pdf":
        return read_pdf(path, progress, cancel), "page"
    if ext == "pptx":
        return read_pptx(path, cancel), "slide"
    if ext == "epub":
        return read_epub(path, cancel), "chapter"
    if ext == "docx":
        return read_docx(path), "section"
    if ext in IMAGE_EXTS:
        if OCR is None and ocr is None:
            raise study.SourceError(415, "Reading images needs an OCR tool or a vision provider.")
        data = path.read_bytes()
        text = (OCR or ocr)(data, IMAGE_MIME.get(ext, "image/png"))
        return virtual_pages(text), "section"
    return read_text_file(path, ext), "section"


def chunk_pages(pages: list[str]) -> list[dict]:
    out: list[dict] = []
    for n, page in enumerate(pages, 1):
        for piece in kb.chunk_text(page, size=CHUNK, overlap=100):
            out.append({"p": n, "t": piece["text"]})
            if len(out) >= MAX_CHUNKS:
                return out
    return out


# ---- the library -----------------------------------------------------------------------------------

class Library:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.dir = Path(ctx.config.data_path()) / "studylib"
        self.index = JsonFile(self.dir / "index.json")
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studylib")
        self.cancels: dict[str, threading.Event] = {}
        self.progress: dict[str, float] = {}
        self.futures: dict[str, Any] = {}
        self._ocr = None
        with self.lock:
            data = self._load()
            changed = False
            for d in data["docs"]:       # a job a restart cut off cannot be resumed from nothing
                if d.get("status") in ("queued", "indexing"):
                    d["status"], d["error"] = "error", "The hub restarted while this was being read. Add it again."
                    changed = True
            if changed:
                self._save(data)

    # -- storage
    def _load(self) -> dict:
        data = self.index.load({"docs": []})
        if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
            data = {"docs": []}
        return data

    def _save(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.save(data)

    def _doc_file(self, doc_id: str, suffix: str = "json") -> JsonFile:
        return JsonFile(self.dir / "docs" / f"{doc_id}.{suffix}")

    def _update(self, doc_id: str, **fields) -> Optional[dict]:
        with self.lock:
            data = self._load()
            for d in data["docs"]:
                if d["id"] == doc_id:
                    d.update(fields)
                    self._save(data)
                    return dict(d)
        return None

    def docs(self) -> list[dict]:
        with self.lock:
            rows = [dict(d) for d in self._load()["docs"]]
        for d in rows:
            if d["id"] in self.progress and d.get("status") in ("queued", "indexing"):
                d["progress"] = round(self.progress[d["id"]], 2)
        rows.sort(key=lambda d: d.get("added", ""), reverse=True)
        return rows

    def get(self, doc_id: str) -> dict:
        for d in self.docs():
            if d["id"] == doc_id:
                return d
        raise study.SourceError(404, "That document isn't in your study library.")

    def ocr(self):
        if self._ocr is None:
            self._ocr = default_ocr(self.ctx)
        return self._ocr

    # -- adding
    def _fingerprint(self, *parts) -> str:
        return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:20]

    def _new(self, title: str, filename: str, ext: str, source: str, size: int, tags: list[str],
             fingerprint: str = "", origin: str = "") -> dict:
        with self.lock:
            data = self._load()
            if len(data["docs"]) >= MAX_DOCS:
                raise study.SourceError(409, f"The library is full ({MAX_DOCS} documents). Delete some first.")
            if fingerprint:
                for d in data["docs"]:
                    if d.get("fingerprint") == fingerprint and d.get("status") != "error":
                        dup = dict(d)
                        dup["duplicate"] = True
                        return dup
            doc = {"id": secrets.token_hex(6), "title": title[:200] or filename or "Untitled", "filename": filename,
                   "ext": ext, "source": source, "size": size, "pages": 0, "unit": "page", "chars": 0,
                   "chunks": 0, "search": "", "tags": tags, "subject": "", "added": _iso(), "status": "queued",
                   "progress": 0.0, "error": "", "fingerprint": fingerprint, "origin": origin}
            data["docs"].append(doc)
            self._save(data)
            return dict(doc)

    def add_file(self, path: Path, *, filename: str, source: str, title: str = "", tags: list[str] | None = None,
                 delete_after: bool = False, origin: str = "") -> dict:
        """Queue a file for reading. Raises SourceError for an unsupported or oversize file."""
        try:
            size = path.stat().st_size
        except OSError:
            raise study.SourceError(404, "Couldn't read that file.") from None
        try:
            ext = check_file(filename, size)
        except study.SourceError:
            if delete_after:
                path.unlink(missing_ok=True)
            raise
        fp = self._fingerprint(source, origin or filename, size, int(path.stat().st_mtime) if not delete_after else 0)
        try:
            doc = self._new(title or Path(filename).stem, filename, ext, source, size, tags or [], fp, origin)
        except study.SourceError:
            if delete_after:
                path.unlink(missing_ok=True)
            raise
        if doc.get("duplicate"):
            if delete_after:
                path.unlink(missing_ok=True)
            return doc
        self._start(doc["id"], lambda cancel: extract(path, ext, lambda v: self._set_progress(doc["id"], v),
                                                      cancel, self.ocr()),
                    cleanup=(lambda: path.unlink(missing_ok=True)) if delete_after else None)
        return doc

    def add_text(self, title: str, text: str, *, source: str = "paste", tags: list[str] | None = None,
                 origin: str = "") -> dict:
        text = (text or "").strip()
        if not text:
            raise study.SourceError(400, "There is no text to add.")
        text = text[:MAX_DOC_CHARS]
        doc = self._new(title or study._first_line(text, 50) or "Pasted text", "", "txt", source, len(text.encode()),
                        tags or [], self._fingerprint(source, origin, hashlib.sha1(text.encode()).hexdigest())
                        if source == "paste" or origin else "", origin)
        if doc.get("duplicate"):
            return doc
        self._start(doc["id"], lambda cancel: (virtual_pages(text), "section"))
        return doc

    def _set_progress(self, doc_id: str, value: float) -> None:
        self.progress[doc_id] = max(0.0, min(0.99, value))

    def _start(self, doc_id: str, reader, cleanup=None) -> None:
        cancel = threading.Event()
        self.cancels[doc_id] = cancel
        self.progress[doc_id] = 0.0
        self.futures[doc_id] = self.pool.submit(self._job, doc_id, reader, cancel, cleanup)

    def _alive(self, doc_id: str) -> bool:
        with self.lock:
            return any(d["id"] == doc_id for d in self._load()["docs"])

    def _job(self, doc_id: str, reader, cancel, cleanup) -> None:
        try:
            self._update(doc_id, status="indexing", error="")
            pages, unit = reader(cancel)
            pages = [p for p in pages]
            if not any(p.strip() for p in pages):
                raise study.SourceError(422, "No readable text was found in that file.")
            if not self._alive(doc_id):
                return
            self._doc_file(doc_id).save({"pages": pages})
            self._update(doc_id, pages=len(pages), unit=unit, chars=sum(len(p) for p in pages))
            self._index(doc_id, pages, cancel)
        except Cancelled:
            self._drop(doc_id, keep_row=True, status="cancelled", error="Cancelled.")
        except study.SourceError as exc:
            self._update(doc_id, status="error", error=exc.message)
        except Exception as exc:                       # never let a bad file kill the worker
            self._update(doc_id, status="error", error=f"Couldn't read that file ({type(exc).__name__}).")
        finally:
            self.progress.pop(doc_id, None)
            self.cancels.pop(doc_id, None)
            if cleanup:
                try:
                    cleanup()
                except OSError:
                    pass

    def _index(self, doc_id: str, pages: list[str], cancel) -> None:
        chunks = chunk_pages(pages)
        self._doc_file(doc_id).save({"pages": pages, "chunks": chunks})
        mode, vectors = "keywords", []
        if chunks and len(chunks) <= MAX_EMBED_CHUNKS:
            try:
                for i in range(0, len(chunks), EMBED_BATCH):
                    if cancel.is_set():
                        raise Cancelled
                    batch = [c["t"] for c in chunks[i:i + EMBED_BATCH]]
                    vectors.extend(self._embed(batch))
                    self._set_progress(doc_id, 0.8 + 0.19 * min(1.0, (i + EMBED_BATCH) / len(chunks)))
                mode = "embeddings"
            except kb.EmbedError:
                vectors = []
        vec = self._doc_file(doc_id, "vec.json")
        if vectors:
            vec.save({"v": [[round(x, 5) for x in v] for v in vectors]})
        else:
            try:
                vec.path.unlink()
            except OSError:
                pass
        if self._alive(doc_id):
            self._update(doc_id, status="ready", chunks=len(chunks), search=mode, progress=1.0, error="")

    def _embed(self, texts: list[str]):
        if EMBEDDER is None:
            return kb._embed(texts, self.ctx.config)
        try:
            vectors = EMBEDDER(texts, self.ctx.config)
        except kb.EmbedError:
            raise
        except Exception:
            raise kb.EmbedError("embedding failed") from None
        return [kb._unit(v) for v in vectors]

    def _drop(self, doc_id: str, keep_row: bool = False, **fields) -> None:
        for suffix in ("json", "vec.json"):
            try:
                self._doc_file(doc_id, suffix).path.unlink()
            except OSError:
                pass
        if keep_row:
            self._update(doc_id, pages=0, chunks=0, chars=0, **fields)
        else:
            with self.lock:
                data = self._load()
                data["docs"] = [d for d in data["docs"] if d["id"] != doc_id]
                self._save(data)

    # -- managing
    def delete(self, doc_id: str) -> None:
        self.get(doc_id)
        cancel = self.cancels.get(doc_id)
        if cancel:
            cancel.set()
        self._drop(doc_id)

    def cancel(self, doc_id: str) -> dict:
        doc = self.get(doc_id)
        cancel = self.cancels.get(doc_id)
        if cancel is None:
            raise study.SourceError(409, "That document isn't being read right now.")
        cancel.set()
        return doc

    def edit(self, doc_id: str, title: Optional[str], tags: Optional[list[str]], subject: Optional[str]) -> dict:
        self.get(doc_id)
        fields: dict[str, Any] = {}
        if title is not None:
            if not title.strip():
                raise study.SourceError(400, "A document needs a name.")
            fields["title"] = " ".join(title.split())[:200]
        if tags is not None:
            fields["tags"] = clean_tags(tags)
        if subject is not None:
            fields["subject"] = " ".join(subject.split())[:60]
        return self._update(doc_id, **fields) or self.get(doc_id)

    def reindex(self, doc_id: str) -> dict:
        doc = self.get(doc_id)
        if doc_id in self.cancels:
            raise study.SourceError(409, "That document is already being read.")
        stored = self._doc_file(doc_id).load(None)
        if not isinstance(stored, dict) or not stored.get("pages"):
            raise study.SourceError(409, "Nothing was kept for that document. Add the file again.")
        pages = stored["pages"]
        self._update(doc_id, status="queued", error="", search="")
        self._start(doc_id, lambda cancel: (pages, doc.get("unit", "page")))
        return self.get(doc_id)

    def wait(self, doc_id: str, timeout: float = 30.0) -> None:
        fut = self.futures.get(doc_id)
        if fut:
            fut.result(timeout=timeout)

    # -- reading material back
    def pages_of(self, doc_id: str) -> list[str]:
        data = self._doc_file(doc_id).load(None)
        return list(data.get("pages") or []) if isinstance(data, dict) else []

    def chunks_of(self, doc_id: str) -> list[dict]:
        data = self._doc_file(doc_id).load(None)
        return list(data.get("chunks") or []) if isinstance(data, dict) else []

    def source(self, ids: list[str], page_from: Optional[int] = None, page_to: Optional[int] = None) -> study.Loaded:
        """The selected documents as one study source. Too long for the model: pages are sampled evenly."""
        ids = list(dict.fromkeys(i for i in ids if i))
        if not ids:
            raise study.SourceError(400, "Choose at least one document from your library.")
        picked = [self.get(i) for i in ids]
        for d in picked:
            if d.get("status") != "ready":
                raise study.SourceError(409, f"'{d['title']}' isn't ready yet ({d.get('status')}). "
                                             + (d.get("error") or "Wait for it to finish reading."))
        if (page_from or page_to) and len(picked) > 1:
            raise study.SourceError(400, "A page range works with one document at a time.")
        budget = study.MAX_TEXT_CHARS // len(picked)
        parts = []
        for d in picked:
            pages = self.pages_of(d["id"])
            lo = max(1, page_from or 1)
            hi = min(len(pages), page_to or len(pages))
            if lo > hi:
                raise study.SourceError(400, f"'{d['title']}' has {len(pages)} {d.get('unit', 'page')}s; "
                                             f"{lo} to {hi} is empty.")
            window = pages[lo - 1:hi]
            if sum(len(p) for p in window) > budget:
                avg = max(1, sum(len(p) for p in window) // len(window))
                keep = max(1, budget // avg)
                window = [window[i] for i in study.spread_indexes(len(window), keep)]
            body = "\n\n".join(p for p in window if p)
            parts.append((f"## {d['title']}\n\n" if len(picked) > 1 else "") + body[:budget])
        text = "\n\n".join(parts).strip()
        if not text:
            raise study.SourceError(422, "Those pages have no text.")
        title = picked[0]["title"] if len(picked) == 1 else f"{len(picked)} library documents"
        rng = f", pages {page_from or 1}-{page_to or 'end'}" if (page_from or page_to) else ""
        loaded = study.Loaded("library", title, text, f"Library: {title}{rng}")
        loaded.ids, loaded.page_from, loaded.page_to = tuple(ids), page_from, page_to
        return loaded

    def search(self, ids: list[str], query: str, k: int = 6) -> list[dict]:
        """Best chunks across documents (keywords, fused with meaning when the documents were embedded)."""
        allc: list[dict] = []
        vecs: list[Optional[list]] = []
        for i in ids:
            doc = next((d for d in self.docs() if d["id"] == i), None)
            if not doc or doc.get("status") != "ready":
                continue
            chunks = self.chunks_of(i)
            vdata = self._doc_file(i, "vec.json").load(None) if doc.get("search") == "embeddings" else None
            v = vdata.get("v") if isinstance(vdata, dict) else None
            for n, c in enumerate(chunks):
                allc.append({"doc": i, "title": doc["title"], "p": c["p"], "t": c["t"]})
                vecs.append(v[n] if v and n < len(v) else None)
        if not allc:
            return []
        scores = study.bm25_scores([c["t"] for c in allc], query)
        order = sorted(range(len(allc)), key=lambda n: scores[n], reverse=True)
        ranks = [[n for n in order if scores[n] > 0][:50]]
        if all(v is not None for v in vecs):
            try:
                q = self._embed([query])[0]
                sims = [sum(a * b for a, b in zip(q, v)) for v in vecs]
                ranks.append(sorted(range(len(allc)), key=lambda n: sims[n], reverse=True)[:50])
            except kb.EmbedError:
                pass
        fused = kb._fuse(ranks)[:k] if any(ranks) else []
        return [allc[n] for n, _ in fused]


def clean_tags(tags) -> list[str]:
    out = []
    for t in tags or []:
        t = " ".join(str(t).split())[:30]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:10]


def public(doc: dict) -> dict:
    keep = ("id", "title", "filename", "ext", "source", "size", "pages", "unit", "chars", "chunks", "search", "tags",
            "subject", "added", "status", "progress", "error", "duplicate")
    return {k: doc[k] for k in keep if k in doc}


# ---- registry ---------------------------------------------------------------------------------------

_registry: dict[int, tuple[object, Library]] = {}
_registry_lock = threading.Lock()


def get_library(ctx: Ctx) -> Library:
    with _registry_lock:
        entry = _registry.get(id(ctx.config))
        if entry is None or entry[0] is not ctx.config:
            entry = (ctx.config, Library(ctx))
            _registry[id(ctx.config)] = entry
        return entry[1]


def chat_material(lib: Library, loaded: study.Loaded, query: str) -> str:
    """Chat over a library selection looks up the best chunks in the whole documents, not the clipped text."""
    hits = lib.search(list(loaded.ids), query, study.CHAT_CHUNKS + 2) if loaded.ids else []
    if not hits:
        return study.chat_material(loaded.text, query)
    return "\n\n...\n\n".join(f"[{h['title']}, p. {h['p']}]\n{h['t']}" for h in hits)


# ---- bodies and routes ------------------------------------------------------------------------------

class ImportBody(BaseModel):
    root: str = Field(max_length=100)
    paths: list[str] = Field(min_length=1, max_length=MAX_IMPORT_FILES)
    tags: list[str] = Field(default_factory=list, max_length=10)


class AbsImportBody(BaseModel):
    """Used by the assistant tool after the owner approved a file outside the shared folders."""
    absolute_path: str = Field(max_length=1000)
    confirmed_outside: bool = False
    tags: list[str] = Field(default_factory=list, max_length=10)


class PasteBody(BaseModel):
    title: str = Field(default="", max_length=200)
    text: Optional[str] = Field(default=None, max_length=2_000_000)
    url: Optional[str] = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=10)


class EditBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    tags: Optional[list[str]] = Field(default=None, max_length=10)
    subject: Optional[str] = Field(default=None, max_length=60)


def _inside_roots(roots: dict[str, str], target: Path) -> Optional[tuple[str, str]]:
    best = None
    for name, base in roots.items():
        try:
            b = Path(base).resolve()
            rel = target.relative_to(b)
        except (ValueError, OSError):
            continue
        if best is None or len(str(b)) > len(best[2]):
            best = (name, rel.as_posix(), str(b))
    return (best[0], best[1]) if best else None


def make_router(ctx: Ctx, *, fetcher=None) -> APIRouter:
    router = APIRouter(prefix="/api/learn/library", dependencies=[Depends(ctx.need_auth)])
    fetch = fetcher or study.safe_fetch
    lib = get_library(ctx)

    def fail(exc: study.SourceError):
        return HTTPException(exc.status, exc.message)

    @router.get("")
    async def listing(q: str = ""):
        rows = await asyncio.to_thread(lib.docs)
        needle = q.strip().lower()
        if needle:
            rows = [d for d in rows if needle in d["title"].lower() or needle in d.get("subject", "").lower()
                    or any(needle in t.lower() for t in d.get("tags", []))]
        subjects = sorted({d["subject"] for d in rows if d.get("subject")})
        return {"docs": [public(d) for d in rows], "subjects": subjects,
                "busy": any(d.get("status") in ("queued", "indexing") for d in rows),
                "limits": {"max_bytes": MAX_UPLOAD_BYTES, "max_image_bytes": MAX_IMAGE_BYTES,
                           "extensions": EXTS, "max_import_files": MAX_IMPORT_FILES}}

    @router.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...), title: str = Form(""), tags: str = Form("")):
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        name = (file.filename or "").replace("\\", "/").split("/")[-1].strip() or "upload"
        if declared > MAX_UPLOAD_BYTES + 1_000_000:
            raise HTTPException(413, f"That file is {human_bytes(declared)}; the limit is "
                                     f"{human_bytes(MAX_UPLOAD_BYTES)}.")
        ext = ext_of(name)
        if ext not in EXTS:
            try:
                check_file(name, 1)
            except study.SourceError as exc:
                raise fail(exc) from None
        staging = Path(ctx.config.data_path()) / "uploads-tmp"
        staging.mkdir(parents=True, exist_ok=True)
        limit = MAX_IMAGE_BYTES if ext in IMAGE_EXTS else MAX_UPLOAD_BYTES

        def save() -> tuple[Path, int]:
            fd, tmp = tempfile.mkstemp(dir=staging, prefix="study-")
            size = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    block = file.file.read(256 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        break
                    out.write(block)
            return Path(tmp), size

        path, size = await asyncio.to_thread(save)
        try:
            check_file(name, size)
            doc = await asyncio.to_thread(
                lib.add_file, path, filename=name, source="upload", title=title.strip(),
                tags=clean_tags(tags.split(",")), delete_after=True, origin=name)
        except study.SourceError as exc:
            path.unlink(missing_ok=True)
            raise fail(exc) from None
        ctx.audit("study_add", source="upload", ext=ext, size=size)
        return {"doc": public(doc)}

    @router.post("/import")
    async def import_files(body: ImportBody):
        results = []
        for rel in body.paths:
            try:
                target = files.resolve(ctx.config.roots, body.root, rel)
            except files.FileError as exc:
                if exc.status == 404 and body.root not in ctx.config.roots:
                    raise HTTPException(404, exc.message) from None
                results.append({"path": rel, "ok": False, "status": exc.status, "error": exc.message})
                continue
            try:
                if not target.is_file():
                    raise study.SourceError(404, "No such file.")
                doc = await asyncio.to_thread(lib.add_file, target, filename=target.name, source="laptop",
                                              tags=clean_tags(body.tags), origin=str(target))
                results.append({"path": rel, "ok": True, "doc": public(doc)})
            except study.SourceError as exc:
                results.append({"path": rel, "ok": False, "status": exc.status, "error": exc.message})
            except PermissionError:
                results.append({"path": rel, "ok": False, "status": 403, "error": "No permission to read that file."})
        ctx.audit("study_import", root=body.root, files=len(body.paths), added=sum(1 for r in results if r["ok"]))
        return {"results": results, "added": sum(1 for r in results if r["ok"])}

    @router.post("/import-path")
    async def import_path(body: AbsImportBody):
        """One file by its laptop path. Inside a shared folder: like /import. Elsewhere: only with the owner's approval,
        which the assistant tool states with confirmed_outside after its approval card was accepted."""
        raw = body.absolute_path.strip()
        if not raw:
            raise HTTPException(400, "Give the path of a file.")
        try:
            target = Path(os.path.expanduser(raw)).resolve()
        except (OSError, RuntimeError):
            raise HTTPException(400, "That path isn't valid.") from None
        found = _inside_roots(ctx.config.roots, target)
        if found is None:
            if not body.confirmed_outside:
                raise HTTPException(403, "That file is outside the shared folders. The owner has to approve it.")
            if _sensitive(target):
                raise HTTPException(403, "That file is in a folder that holds credentials, so it can't be added.")
        try:
            if not target.is_file():
                raise HTTPException(404, "No such file.")
            doc = await asyncio.to_thread(lib.add_file, target, filename=target.name, source="laptop",
                                          tags=clean_tags(body.tags), origin=str(target))
        except study.SourceError as exc:
            raise fail(exc) from None
        except PermissionError:
            raise HTTPException(403, "No permission to read that file.") from None
        ctx.audit("study_import", root=found[0] if found else "outside", files=1, added=1)
        return {"doc": public(doc), "inside_shared_folder": found is not None}

    @router.post("/paste")
    async def paste(body: PasteBody):
        if body.url and body.url.strip():
            url = body.url.strip()
            try:
                loaded = study.page_from_fetch(await fetch(url))
            except study.SourceError as exc:
                raise fail(exc) from None
            ctx.audit("learn_url", host=study.urlsplit(url).hostname or "")
            doc = lib.add_text(body.title.strip() or loaded.title, loaded.text, source="url",
                               tags=clean_tags(body.tags), origin=url)
        elif body.text and body.text.strip():
            try:
                doc = lib.add_text(body.title.strip(), body.text, source="paste", tags=clean_tags(body.tags))
            except study.SourceError as exc:
                raise fail(exc) from None
        else:
            raise HTTPException(400, "Paste some text or a web address first.")
        return {"doc": public(doc)}

    def one(doc_id: str) -> dict:
        try:
            return lib.get(doc_id)
        except study.SourceError as exc:
            raise fail(exc) from None

    @router.get("/{doc_id}")
    async def detail(doc_id: str):
        doc = await asyncio.to_thread(one, doc_id)
        pages = await asyncio.to_thread(lib.pages_of, doc_id)
        first = next((p for p in pages if p), "")
        return {"doc": public(doc), "excerpt": first[:400]}

    @router.get("/{doc_id}/text")
    async def text(doc_id: str, page_from: int = 1, page_to: int = 0, max_chars: int = 4000):
        doc = await asyncio.to_thread(one, doc_id)
        if doc.get("status") != "ready":
            raise HTTPException(409, "That document isn't ready yet.")
        pages = await asyncio.to_thread(lib.pages_of, doc_id)
        hi = page_to if page_to > 0 else len(pages)
        body = "\n\n".join(p for p in pages[max(1, page_from) - 1:hi] if p)
        return {"title": doc["title"], "text": body[:max(100, min(max_chars, 20_000))], "chars": len(body)}

    @router.patch("/{doc_id}")
    async def edit(doc_id: str, body: EditBody):
        try:
            return {"doc": public(await asyncio.to_thread(lib.edit, doc_id, body.title, body.tags, body.subject))}
        except study.SourceError as exc:
            raise fail(exc) from None

    @router.delete("/{doc_id}")
    async def delete(doc_id: str):
        try:
            await asyncio.to_thread(lib.delete, doc_id)
        except study.SourceError as exc:
            raise fail(exc) from None
        ctx.audit("study_delete")
        return {"ok": True}

    @router.post("/{doc_id}/reindex")
    async def reindex(doc_id: str):
        try:
            return {"doc": public(await asyncio.to_thread(lib.reindex, doc_id))}
        except study.SourceError as exc:
            raise fail(exc) from None

    @router.post("/{doc_id}/cancel")
    async def cancel(doc_id: str):
        try:
            return {"doc": public(await asyncio.to_thread(lib.cancel, doc_id))}
        except study.SourceError as exc:
            raise fail(exc) from None

    return router


def _sensitive(target: Path) -> bool:
    home = Path.home()
    for name in (".ssh", ".aws", ".claude", ".gnupg", ".kube", ".azure", ".config/gcloud"):
        try:
            target.relative_to((home / name).resolve())
            return True
        except (ValueError, OSError):
            continue
    return kb.blocked(target.name)


# ---- assistant tools (voice and text) ------------------------------------------------------------------

def build(get_app, config):
    from .assistant import ExtraTool, mark_tools
    hub = InProcessClient(get_app, config)

    def schema(props, required):
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    async def study_library_list(a):
        ok, d = await hub.call("GET", "/api/learn/library", params={"q": str(a.get("query") or "")[:100]})
        if not ok:
            return False, d
        docs = d.get("docs", [])
        if not docs:
            return True, "The study library is empty. Add material in Learn, or with study_import_path."
        lines = [f"- id {x['id']} | {x['title']} | {x['pages']} {x['unit']}s | {x['status']}"
                 + (f" | {', '.join(x['tags'])}" if x.get("tags") else "") for x in docs[:40]]
        more = f"\n... and {len(docs) - 40} more." if len(docs) > 40 else ""
        return True, "\n".join(lines) + more

    def outside(a) -> bool:
        raw = str(a.get("path") or "").strip()
        if not raw:
            return False
        try:
            return _inside_roots(config.roots, Path(os.path.expanduser(raw)).resolve()) is None
        except (OSError, RuntimeError):
            return True

    async def study_import_path(a):
        raw = str(a.get("path") or "").strip()
        if not raw:
            return False, "Give the path of a file on the laptop."
        body = {"absolute_path": raw[:1000], "confirmed_outside": outside(a)}
        if a.get("tags"):
            body["tags"] = [str(t) for t in a["tags"]][:10]
        ok, d = await hub.call("POST", "/api/learn/library/import-path", body=body)
        if not ok:
            return False, d
        doc = d["doc"]
        note = " (already in the library)" if doc.get("duplicate") else " It is being read now."
        return True, f"Added '{doc['title']}' to the study library (id {doc['id']}).{note}"

    return mark_tools([
        ExtraTool({"name": "study_library_list",
                   "description": "List documents in the owner's study library (title, pages, id). Optional query "
                                  "filters by title or tag. Read-only.",
                   "input_schema": schema({"query": {"type": "string"}}, [])}, study_library_list),
        ExtraTool({"name": "study_import_path",
                   "description": "Add one file from the laptop (pdf, docx, pptx, epub, txt, md, html, image) to the "
                                  "study library. Files inside the shared folders are read directly; any other "
                                  "path asks the owner first.",
                   "input_schema": schema({"path": {"type": "string"},
                                           "tags": {"type": "array", "items": {"type": "string"}}}, ["path"])},
                  study_import_path, needs_approval=outside),
    ], untrusted=["study_library_list"], mutating=[])
