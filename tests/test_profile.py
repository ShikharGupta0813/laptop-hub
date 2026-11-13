"""Profile store (hub/profile.py). Synthetic resume data only: nothing real belongs in this repository."""
import base64
import io
import os
import stat
import tempfile
import unittest
import zipfile
import zlib
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import profile
from hub.common import Ctx
from tests.helpers import make_config

RESUME = """Alex Sample
Backend Engineer
alex@example.invalid | https://github.com/alex-sample | https://www.linkedin.com/in/alex-sample

Summary
Engineer who builds APIs and data pipelines.

Skills
Languages: Python, SQL, Go
Tools: Docker, FastAPI, PostgreSQL

Experience
Senior Engineer at Acme Labs  Jan 2019 - Present
- Built a billing API in Python
- Cut query time by 60%
Developer, Widget Co  2015 - 2018
- Wrote Django apps

Education
BSc Computer Science, Example University, 2014
"""


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


def make_ctx():
    config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="profile-test-"))
    return Ctx(config=config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
               audit=lambda *a, **k: None)


def tiny_pdf(lines, compress=False):
    body = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({ln}) Tj T* " for ln in lines) + "ET"
    data = body.encode()
    flt = ""
    if compress:
        data, flt = zlib.compress(data), "/Filter /FlateDecode "
    return (b"%PDF-1.4\n1 0 obj << /Length " + str(len(data)).encode() + b" " + flt.encode() + b">>\nstream\n" + data
            + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF")


def tiny_docx(paragraphs):
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs) + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class Parse(unittest.TestCase):
    def test_parse_resume_fields(self):
        p = profile.parse_resume(RESUME)
        self.assertEqual(p["name"], "Alex Sample")
        self.assertEqual(p["headline"], "Backend Engineer")
        self.assertIn("Python", p["skills"])
        self.assertIn("FastAPI", p["skills"])
        self.assertEqual(len(p["experience"]), 2)
        first = p["experience"][0]
        self.assertEqual((first["title"], first["company"]), ("Senior Engineer", "Acme Labs"))
        self.assertIn("Present", first["period"])
        self.assertEqual(first["bullets"], ["Built a billing API in Python", "Cut query time by 60%"])
        self.assertEqual(p["experience"][1]["company"], "Widget Co")
        self.assertIn("Computer Science", p["education"])
        self.assertEqual(p["links"]["github"], "https://github.com/alex-sample")
        self.assertEqual(p["links"]["email"], "alex@example.invalid")

    def test_skills_fall_back_to_vocabulary(self):
        p = profile.parse_resume("Pat Doe\nI write Python and SQL and deploy with Docker on AWS every day for years.")
        self.assertTrue({"python", "sql", "docker", "aws"} <= set(p["skills"]))

    def test_odd_text_does_not_crash(self):
        for text in ("", "\n\n", "x", "Skills\n", "Experience\n2019 - 2020\n"):
            profile.parse_resume(text)


class Files(unittest.TestCase):
    def test_text_and_markdown(self):
        self.assertIn("Alex Sample", profile.extract_resume_text("cv.md", RESUME.encode()))

    def test_docx(self):
        text = profile.extract_resume_text("cv.docx", tiny_docx(["Alex Sample", "Backend Engineer and more words here to pass"]))
        self.assertIn("Backend Engineer", text)

    def test_pdf_fallback_plain_and_compressed(self):
        lines = ["Alex Sample", "Backend Engineer with many years of experience"]
        for compress in (False, True):
            text = profile.pdf_text_fallback(tiny_pdf(lines, compress))
            self.assertIn("Alex Sample", text)
            self.assertIn("Backend Engineer", text)

    def test_pdf_without_pypdf_uses_fallback(self):
        with mock.patch.dict("sys.modules", {"pypdf": None}):
            text = profile.extract_resume_text("cv.pdf", tiny_pdf(["Alex Sample here is a long enough line of text"]))
        self.assertIn("Alex Sample", text)

    def test_unreadable_pdf_says_paste_text(self):
        with mock.patch.dict("sys.modules", {"pypdf": None}):
            with self.assertRaises(profile.ProfileError) as cm:
                profile.extract_resume_text("scan.pdf", b"%PDF-1.4 binary-only scanned image data" * 3)
        self.assertIn("Paste the text", str(cm.exception))

    def test_limits_and_bad_types(self):
        with self.assertRaises(profile.ProfileError):
            profile.extract_resume_text("a.txt", b"")
        with self.assertRaises(profile.ProfileError):
            profile.extract_resume_text("a.txt", b"x" * (profile.MAX_RESUME_BYTES + 1))
        with self.assertRaises(profile.ProfileError):
            profile.extract_resume_text("a.doc", b"old binary format 0123456789 0123456789 0123456789")
        with self.assertRaises(profile.ProfileError):
            profile.extract_resume_text("a.docx", b"PK not really a zip file at all, just text")
        with self.assertRaises(profile.ProfileError):
            profile.extract_resume_text("a.txt", b"too short")

    def test_xml_entities_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><x>&b;</x>'
        with self.assertRaises(ValueError):
            profile.safe_xml_root(bomb)
        xxe = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
        with self.assertRaises(ValueError):
            profile.safe_xml_root(xxe)
        self.assertEqual(profile.safe_xml_root(b"<x>ok</x>").text, "ok")


class Store(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_empty_profile_has_all_keys(self):
        p = profile.get_profile(self.ctx)
        for key in ("name", "headline", "summary", "skills", "experience", "education", "links", "standard_answers",
                    "resume_text", "updated_at"):
            self.assertIn(key, p)

    def test_manual_edits_survive_a_new_resume_unless_overwrite(self):
        profile.apply_resume(self.ctx, RESUME)
        profile.update_fields(self.ctx, {"headline": "Platform Engineer", "skills": ["Rust"]})
        out = profile.apply_resume(self.ctx, RESUME)
        self.assertEqual(out["headline"], "Platform Engineer")
        self.assertEqual(out["skills"], ["Rust"])
        self.assertEqual(out["name"], "Alex Sample")
        self.assertIn("headline", out["_kept"])
        out = profile.apply_resume(self.ctx, RESUME, overwrite=True)
        self.assertEqual(out["headline"], "Backend Engineer")
        self.assertIn("Python", out["skills"])
        self.assertEqual(profile.manual_fields(self.ctx), [])

    def test_clean_field_validation(self):
        self.assertEqual(profile.clean_field("skills", "a, b;c\nb"), ["a", "b", "c"])
        with self.assertRaises(ValueError):
            profile.clean_field("links", ["x"])
        with self.assertRaises(ValueError):
            profile.clean_field("experience", "x")
        with self.assertRaises(ValueError):
            profile.update_fields(self.ctx, {"updated_at": "x"})

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_file_is_private(self):
        profile.update_fields(self.ctx, {"name": "Alex"})
        mode = stat.S_IMODE(os.stat(profile.path_for(self.ctx)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_model_extraction_is_optional_and_redacted(self):
        seen = []

        async def ask(system, user):
            seen.append(user)
            return '{"headline": "ML Engineer", "skills": ["PyTorch", "Python"], "bogus": 1}'

        import asyncio
        out = asyncio.run(profile.extract_with_model(self.ctx, "Reach me at alex@example.invalid. Python expert " * 3, ask))
        self.assertEqual(out["headline"], "ML Engineer")
        self.assertNotIn("bogus", out)
        self.assertNotIn("alex@example.invalid", seen[0])     # redacted before leaving

        async def broken(system, user):
            raise RuntimeError("down")
        self.assertEqual(asyncio.run(profile.extract_with_model(self.ctx, "text", broken)), {})


class Routes(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        app = FastAPI()
        app.include_router(profile.make_router(self.ctx))
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)

    def test_all_routes_require_login(self):
        for method, path in (("get", "/api/profile"), ("put", "/api/profile"), ("post", "/api/profile/resume"),
                             ("delete", "/api/profile/resume")):
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)

    def test_upload_text_then_edit_then_replace(self):
        r = self.client.post("/api/profile/resume", json={"text": RESUME})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Alex Sample")
        self.assertIn("name", r.json()["filled"])
        r = self.client.put("/api/profile", json={"standard_answers": {"notice period": "2 weeks", "expected rate": "$60/hour"},
                                                    "headline": "Platform Engineer"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["standard_answers"]["notice period"], "2 weeks")
        r = self.client.post("/api/profile/resume", json={"text": RESUME})
        self.assertEqual(r.json()["headline"], "Platform Engineer")
        self.assertIn("headline", r.json()["kept"])
        r = self.client.post("/api/profile/resume", json={"text": RESUME, "overwrite": True})
        self.assertEqual(r.json()["headline"], "Backend Engineer")
        self.assertEqual(self.client.get("/api/profile").json()["standard_answers"]["notice period"], "2 weeks")

    def test_upload_pdf_docx_base64(self):
        b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
        r = self.client.post("/api/profile/resume", json={"filename": "cv.pdf", "data_b64": b64(tiny_pdf(["Alex Sample", "Backend Engineer with long experience in many areas"]))})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Alex Sample")
        r = self.client.post("/api/profile/resume", json={"filename": "cv.docx", "overwrite": True,
                                                            "data_b64": b64(tiny_docx(["Jo Sample", "Data Analyst with many years of experience"]))})
        self.assertEqual(r.json()["name"], "Jo Sample")

    def test_bad_uploads_are_400_with_advice(self):
        for body in ({}, {"data_b64": "!!!notbase64"}, {"text": "short"}, {"filename": "a.docx", "data_b64": base64.b64encode(b"PK junk junk junk junk junk junk junk").decode()}):
            r = self.client.post("/api/profile/resume", json=body)
            self.assertEqual(r.status_code, 400, body)
        r = self.client.put("/api/profile", json={"links": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.put("/api/profile", json={}).status_code, 400)

    def test_resume_text_is_not_in_audit(self):
        events = []
        self.ctx.audit = lambda *a, **k: events.append((a, k))
        self.client.post("/api/profile/resume", json={"text": RESUME})
        self.assertNotIn("Alex", repr(events))


if __name__ == "__main__":
    unittest.main()
