"""Tests for the study library (hub/studylib.py): upload, picking laptop files, limits, permissions, and a library
selection driving flashcard / quiz / ask generation. No network, no real model, no Ollama, no OCR."""
import asyncio
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hub import cards, kb, study, studylib
from hub.common import Ctx
from hub.notes import NoteStore
from tests.helpers import make_config

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False


def multipage_pdf(pages):
    n = len(pages)
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()]
    font = 3 + 2 * n
    for i in range(n):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + n + i} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode())
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def tiny_pptx(slides):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, text in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml",
                       '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                       'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                       f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>")
    return buf.getvalue()


class FakeModel:
    def __init__(self):
        self.replies, self.calls = [], []

    async def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)

    async def stream(self, system, user):
        self.calls.append((system, user))
        yield "ok"


def no_embed(texts, config):
    raise kb.EmbedError("no embedding model in tests")


NET = ("Packets travel across networks. A router forwards packets between networks using addresses. "
       "DNS turns names into addresses so people do not remember numbers. ") * 6


class Base(unittest.TestCase):
    def setUp(self):
        self.model = FakeModel()
        self.audit_log = []
        self.config, self.root = make_config(data_dir=tempfile.mkdtemp(prefix="hub-studylib-"))
        self.outside = Path(tempfile.mkdtemp(prefix="hub-outside-"))
        studylib.EMBEDDER = no_embed
        self.addCleanup(setattr, studylib, "EMBEDDER", None)
        self.addCleanup(setattr, studylib, "OCR", None)

        def need_auth(request: Request):
            return None
        self.ctx = Ctx(config=self.config, model_for=lambda c: self.model, transcriber=None,
                       notes=NoteStore(self.config.notes_dir), need_auth=need_auth,
                       audit=lambda e, **f: self.audit_log.append((e, f)))
        app = FastAPI()
        app.include_router(studylib.make_router(self.ctx))
        app.include_router(study.make_router(self.ctx))
        app.include_router(cards.make_router(self.ctx))
        app.include_router(kb.make_router(self.ctx))
        self.app = app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.lib = studylib.get_library(self.ctx)

    def upload(self, name, data, **form):
        return self.client.post("/api/learn/library/upload", files={"file": (name, data)}, data=form)

    def ready(self, doc):
        self.lib.wait(doc["id"])
        return self.client.get(f"/api/learn/library/{doc['id']}").json()["doc"]

    def add_text_doc(self, title="Networking notes", text=NET):
        r = self.client.post("/api/learn/library/paste", json={"title": title, "text": text})
        self.assertEqual(r.status_code, 200, r.text)
        return self.ready(r.json()["doc"])

    def staging_leftovers(self):
        return list((Path(self.config.data_path()) / "uploads-tmp").glob("study-*"))


class Upload(Base):
    def test_multipart_text_upload_is_indexed_and_listed(self):
        r = self.upload("dns.md", b"# DNS\n" + NET.encode(), tags="net, dns")
        self.assertEqual(r.status_code, 200, r.text)
        doc = self.ready(r.json()["doc"])
        self.assertEqual(doc["status"], "ready")
        self.assertEqual(doc["title"], "dns")
        self.assertEqual(doc["tags"], ["net", "dns"])
        self.assertEqual(doc["search"], "keywords")          # embedding model absent: keyword fallback
        listing = self.client.get("/api/learn/library").json()
        self.assertEqual([d["id"] for d in listing["docs"]], [doc["id"]])
        self.assertIn("pdf", listing["limits"]["extensions"])
        self.assertEqual(self.staging_leftovers(), [])

    @unittest.skipUnless(HAVE_PYPDF, "pypdf is not installed")
    def test_pdf_pages_counted(self):
        r = self.upload("book.pdf", multipage_pdf(["Alpha chapter one", "Beta chapter two", "Gamma chapter three"]))
        doc = self.ready(r.json()["doc"])
        self.assertEqual((doc["status"], doc["pages"], doc["unit"]), ("ready", 3, "page"))

    def test_pptx_slides_are_pages(self):
        doc = self.ready(self.upload("deck.pptx", tiny_pptx(["First slide text", "Second slide text"])).json()["doc"])
        self.assertEqual((doc["status"], doc["pages"], doc["unit"]), ("ready", 2, "slide"))

    def test_unsupported_type_is_a_clear_error(self):
        r = self.upload("movie.mp4", b"\x00\x00\x00 ftyp")
        self.assertEqual(r.status_code, 415)
        self.assertIn(".mp4", r.json()["detail"])
        self.assertEqual(self.client.get("/api/learn/library").json()["docs"], [])
        self.assertEqual(self.staging_leftovers(), [])

    def test_size_limit(self):
        orig = studylib.MAX_UPLOAD_BYTES
        studylib.MAX_UPLOAD_BYTES = 2000
        self.addCleanup(setattr, studylib, "MAX_UPLOAD_BYTES", orig)
        r = self.upload("big.txt", b"x" * 5000)
        self.assertEqual(r.status_code, 413)
        self.assertIn("limit", r.json()["detail"])
        self.assertEqual(self.staging_leftovers(), [])

    def test_empty_and_garbage_files_fail_in_the_library_not_the_hub(self):
        self.assertEqual(self.upload("empty.txt", b"").status_code, 422)
        doc = self.ready(self.upload("bad.pdf", b"not a pdf at all").json()["doc"])
        self.assertEqual(doc["status"], "error")
        self.assertTrue(doc["error"])

    def test_same_file_twice_is_not_added_twice(self):
        a = self.upload("a.txt", NET.encode()).json()["doc"]
        self.ready(a)
        b = self.upload("a.txt", NET.encode()).json()["doc"]
        self.assertTrue(b.get("duplicate"))
        self.assertEqual(len(self.client.get("/api/learn/library").json()["docs"]), 1)

    def test_image_uses_ocr(self):
        studylib.OCR = lambda data, mime: "Slide text about routers and packets in a screenshot."
        doc = self.ready(self.upload("shot2.png", b"\x89PNG fake2").json()["doc"])
        self.assertEqual(doc["status"], "ready")
        self.assertIn("routers", self.client.get(f"/api/learn/library/{doc['id']}/text").json()["text"])

    def test_paste_and_url(self):
        async def fetcher(url):
            return study.Fetched(url, "text/html", b"<html><title>Web page</title><body><p>" + NET.encode() + b"</p></body>")
        app = FastAPI()
        app.include_router(studylib.make_router(self.ctx, fetcher=fetcher))
        c = TestClient(app)
        r = c.post("/api/learn/library/paste", json={"url": "https://example.com/x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.ready(r.json()["doc"])["title"], "Web page")
        self.assertEqual(c.post("/api/learn/library/paste", json={}).status_code, 400)


class LaptopFiles(Base):
    def put(self, name, data, root="docs"):
        p = Path(self.config.roots[root]) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_import_selected_files_from_a_shared_folder(self):
        self.put("sub/a.txt", NET.encode())
        self.put("sub/b.md", b"# B\n" + NET.encode() + b" extra words here")
        r = self.client.post("/api/learn/library/import", json={"root": "docs", "paths": ["sub/a.txt", "sub/b.md"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["added"], 2)
        for item in r.json()["results"]:
            self.assertEqual(self.ready(item["doc"])["status"], "ready")
        self.assertTrue((Path(self.config.roots["docs"]) / "sub/a.txt").exists())   # read in place, untouched

    def test_a_folder_is_not_importable(self):
        self.put("sub/a.txt", NET.encode())
        r = self.client.post("/api/learn/library/import", json={"root": "docs", "paths": ["sub"]})
        self.assertEqual(r.json()["added"], 0)
        self.assertEqual(r.json()["results"][0]["status"], 404)

    def test_one_bad_file_does_not_stop_the_others(self):
        self.put("ok.txt", NET.encode())
        self.put("clip.mkv", b"video")
        r = self.client.post("/api/learn/library/import", json={"root": "docs", "paths": ["clip.mkv", "ok.txt", "gone.txt"]})
        res = {x["path"]: x for x in r.json()["results"]}
        self.assertEqual(res["clip.mkv"]["status"], 415)
        self.assertTrue(res["ok.txt"]["ok"])
        self.assertEqual(res["gone.txt"]["status"], 404)

    def test_denied_outside_the_shared_folders(self):
        secret = self.outside / "private.txt"
        secret.write_text(NET)
        r = self.client.post("/api/learn/library/import", json={"root": "docs", "paths": ["../../" + str(secret).lstrip("/"),
                                                                                       "../" + secret.name]})
        self.assertTrue(all(x["status"] == 403 for x in r.json()["results"]), r.text)
        self.assertEqual(r.json()["added"], 0)
        self.assertEqual(self.client.post("/api/learn/library/import", json={"root": "nope", "paths": ["a"]}).status_code, 404)
        self.assertEqual(self.client.get("/api/learn/library").json()["docs"], [])

    def test_symlink_out_of_a_root_is_denied(self):
        target = self.outside / "t.txt"
        target.write_text(NET)
        link = Path(self.config.roots["docs"]) / "link.txt"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("no symlinks")
        r = self.client.post("/api/learn/library/import", json={"root": "docs", "paths": ["link.txt"]})
        self.assertEqual(r.json()["results"][0]["status"], 403)

    def test_import_path_needs_approval_outside_roots(self):
        f = self.outside / "x.txt"
        f.write_text(NET)
        self.assertEqual(self.client.post("/api/learn/library/import-path", json={"absolute_path": str(f)}).status_code, 403)
        ok = self.client.post("/api/learn/library/import-path", json={"absolute_path": str(f), "confirmed_outside": True})
        self.assertEqual(ok.status_code, 200, ok.text)
        inside = self.put("in.txt", NET.encode())
        r2 = self.client.post("/api/learn/library/import-path", json={"absolute_path": str(inside)})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["inside_shared_folder"])

    def test_credential_folders_never_import(self):
        ssh = Path.home() / ".ssh" / "id_x.txt"
        r = self.client.post("/api/learn/library/import-path", json={"absolute_path": str(ssh), "confirmed_outside": True})
        self.assertEqual(r.status_code, 403)


class Tools(Base):
    def tools(self):
        return {t.spec["name"]: t for t in studylib.build(lambda: self.app, self.config)}

    def test_tool_approval_rule_follows_the_roots(self):
        tools = self.tools()
        t = tools["study_import_path"]
        self.assertFalse(t.needs_approval({"path": str(Path(self.config.roots["docs"]) / "a.md")}))
        self.assertTrue(t.needs_approval({"path": str(self.outside / "z.txt")}))
        self.assertFalse(tools["study_library_list"].needs_approval({}))
        self.assertTrue(tools["study_library_list"].untrusted({}))


class Manage(Base):
    def test_rename_tags_subject_reindex_delete(self):
        doc = self.add_text_doc()
        r = self.client.patch(f"/api/learn/library/{doc['id']}", json={"title": "  Net  notes ", "subject": "Networking", "tags": ["a", "A", "b"]})
        self.assertEqual(r.json()["doc"]["title"], "Net notes")
        self.assertEqual(r.json()["doc"]["tags"], ["a", "b"])
        self.assertEqual(self.client.get("/api/learn/library?q=networking").json()["docs"][0]["id"], doc["id"])
        self.assertEqual(self.client.patch(f"/api/learn/library/{doc['id']}", json={"title": " "}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/learn/library/{doc['id']}/reindex").status_code, 200)
        self.assertEqual(self.ready(doc)["status"], "ready")
        self.assertEqual(self.client.delete(f"/api/learn/library/{doc['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/learn/library/{doc['id']}").status_code, 404)
        self.assertEqual(list((Path(self.config.data_path()) / "studylib" / "docs").glob("*")), [])

    def test_cancel_a_running_job(self):
        started = threading.Event()

        def slow(cancel):
            started.set()
            while not cancel.wait(0.01):
                pass
            raise studylib.Cancelled
        doc = self.lib._new("Slow", "slow.pdf", "pdf", "upload", 10, [])
        self.lib._start(doc["id"], slow)
        self.assertTrue(started.wait(5))
        self.assertEqual(self.client.post(f"/api/learn/library/{doc['id']}/cancel").status_code, 200)
        self.lib.wait(doc["id"])
        self.assertEqual(self.client.get(f"/api/learn/library/{doc['id']}").json()["doc"]["status"], "cancelled")
        self.assertEqual(self.client.post(f"/api/learn/library/{doc['id']}/cancel").status_code, 409)

    def test_embeddings_used_when_available(self):
        studylib.EMBEDDER = lambda texts, config: [[1.0, float(len(t) % 7)] for t in texts]
        doc = self.add_text_doc()
        self.assertEqual(doc["search"], "embeddings")
        self.assertTrue(self.lib.search([doc["id"]], "router packets", 3))


class Selection(Base):
    def test_library_selection_drives_flashcards_with_difficulty(self):
        doc = self.add_text_doc("Big text", "\n\n".join("Chapter %d. " % n + ("Topic%d words about subject. " % n) * 40 for n in range(1, 6)))
        deck = self.client.post("/api/cards/decks", json={"name": "Net"}).json()
        self.model.replies.append({"cards": [{"front": "What is a router?", "back": "It forwards packets"},
                                             {"front": "What is DNS?", "back": "Names to addresses"}]})
        r = self.client.post(f"/api/cards/decks/{deck['id']}/generate", json={
            "source": {"type": "library", "ids": [doc["id"]]}, "count": 2, "difficulty": "hard"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["cards"]), 2)
        user = self.model.calls[-1][1]
        self.assertIn("Difficulty: hard", user)
        self.assertIn("Topic1", user)               # the library text reached the model, fenced as a source
        self.assertIn("<source", user)

    def test_flashcard_duplicates_are_skipped_with_library_source(self):
        doc = self.add_text_doc()
        deck = self.client.post("/api/cards/decks", json={"name": "Net"}).json()
        self.client.post("/api/cards/cards", json={"deck_id": deck["id"], "front": "What is DNS?", "back": "x"})
        self.model.replies.append({"cards": [{"front": "What is DNS?", "back": "Names"}, {"front": "What is a router?", "back": "Forwards"}]})
        r = self.client.post(f"/api/cards/decks/{deck['id']}/generate", json={"source": {"type": "library", "ids": [doc["id"]]}, "count": 5})
        self.assertEqual([c["front"] for c in r.json()["cards"]], ["What is a router?"])
        self.assertEqual(r.json()["skipped_duplicates"], 1)

    def test_page_range_limits_the_text(self):
        doc = self.add_text_doc("Ranged", "\n\n".join(("Pageword%d unique " % n) * 150 for n in range(1, 6)))
        self.assertGreater(doc["pages"], 2)
        last = self.lib.source([doc["id"]], doc["pages"], doc["pages"])
        self.assertIn("Pageword5", last.text)
        self.assertNotIn("Pageword1 ", last.text)
        with self.assertRaises(study.SourceError):
            self.lib.source([doc["id"]], 999, 1000)
        r = self.client.post("/api/learn/source/preview", json={"source": {"type": "library", "ids": [doc["id"]], "page_from": 1, "page_to": 1}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("Pageword5", r.json()["excerpt"])

    def test_quiz_from_two_documents(self):
        a = self.add_text_doc("Routers", "Routers forward packets between networks. " * 20)
        b = self.add_text_doc("DNS", "DNS maps names to addresses for clients. " * 20)
        self.model.replies.append({"title": "T", "questions": [
            {"kind": "mcq", "question": "What forwards packets?", "choices": ["Router", "Fan", "Cable"], "answer": 0,
             "explanation": "Routers forward.", "topic": "Routing"},
            {"kind": "short", "question": "What does DNS map?", "answer": "names", "accept": [], "explanation": "Names.", "topic": "DNS"},
            {"kind": "short", "question": "Who forwards?", "answer": "router", "accept": [], "explanation": "x", "topic": "R"}]})
        r = self.client.post("/api/learn/quiz", json={"source": {"type": "library", "ids": [a["id"], b["id"]]}, "count": 3})
        self.assertEqual(r.status_code, 200, r.text)
        user = self.model.calls[-1][1]
        self.assertIn("Routers forward", user)
        self.assertIn("DNS maps", user)

    def test_missing_or_empty_selection_is_refused(self):
        r = self.client.post("/api/learn/source/preview", json={"source": {"type": "library", "ids": ["nope"]}})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/learn/source/preview", json={"source": {"type": "library", "ids": []}})
        self.assertEqual(r.status_code, 400)

    def test_ask_my_files_answers_from_the_selected_library_documents(self):
        doc = self.add_text_doc("Net", NET)
        r = self.client.post("/api/kb/ask", json={"question": "What does a router do?", "library": [doc["id"]]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("library:" + doc["id"], r.text)
        self.assertIn("<sources>", self.model.calls[-1][1])

    def test_chat_over_library_uses_chunk_search(self):
        doc = self.add_text_doc("Net", NET)
        r = self.client.post("/api/learn/chat", json={"session_id": "s1", "message": "How does DNS work?",
                                                      "source": {"type": "library", "ids": [doc["id"]]}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("DNS turns names", self.model.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
