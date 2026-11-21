"""ATS match score and the resume read check (hub/ats.py, hub/skillwords.py).

Everything is invented: a made-up owner, made-up postings, and resume FILES built here (a PDF writer, a DOCX writer) so each format trap
can be switched on one at a time. The rules under test:
- synonyms count as one skill (JS / JavaScript, k8s / Kubernetes, Postgres / PostgreSQL);
- missing keywords split into "in your facts but not on this resume" and "a real gap", and a gap is never added to anything;
- the score is plain code: nothing is sent and the facts are not changed;
- format traps are found on files with tables, columns, text boxes, pictures only, contact details in a header, and so on."""
import base64
import copy
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import ats, skillwords, tailor as T
from hub.common import Ctx
from hub.factbase import FactBase
from tests.helpers import make_config
from tests.test_autopilot import need_auth

PROFILE = {
    "name": "Asha Verma", "headline": "Backend engineer",
    "summary": "Backend engineer who builds reliable payment APIs.",
    "skills": ["Python", "PostgreSQL", "Docker", "Terraform", "Technical writing"],
    "experience": [
        {"title": "Senior Backend Engineer", "company": "Payco", "period": "2021 - Present",
         "bullets": ["Built UPI payment APIs in Python handling 1M requests per day", "Tuned PostgreSQL queries and cut latency by 35%", "Mentored 3 junior engineers"]},
        {"title": "Software Developer", "company": "Shopmate", "period": "2017 - 2021",
         "bullets": ["Wrote Docker based deployment scripts", "Built reporting dashboards"]}],
    "education": "B.Tech Computer Science, Example Institute",
    "links": {"github": "https://github.com/asha-verma-example", "email": "asha@example.test"},
}

JOB = {"title": "Senior Python Backend Engineer", "company": "Acme Pay", "url": "https://jobs.example.test/acme/1",
       "description": """About us
We build payment tools for small shops.

Requirements
- 5+ years of Python
- Experience with k8s and Postgres
- Strong communication skills
- Bachelor's degree in computer science or equivalent

Nice to have
- Experience with GraphQL

Location: Remote. Salary 25-35 LPA.
"""}


def pdf(items, *, images=0, size=(595, 842), font="Helvetica"):
    """A tiny valid PDF with text at exact positions: items are (x, y, text)."""
    stream = "".join(f"BT /F1 11 Tf {x} {y} Td ({t.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj ET\n" for x, y, t in items)
    if images:
        stream += "q 100 0 0 100 50 500 cm /Im1 Do Q\n"
    stream_b = stream.encode("latin-1", "replace")
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"}
    res = "/Font << /F1 4 0 R >>"
    if images:
        res += " /XObject << /Im1 5 0 R >>"
    objs[3] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] /Contents 7 0 R /Resources << {res} >> >>".encode()
    objs[4] = f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>".encode()
    objs[5] = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 3 >>\nstream\n\xff\x00\x00\nendstream"
    objs[6] = b"<< >>"
    objs[7] = b"<< /Length %d >>\nstream\n" % len(stream_b) + stream_b + b"\nendstream"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = out.tell()
        out.write(f"{n} 0 obj\n".encode() + objs[n] + b"\nendobj\n")
    start = out.tell()
    top = max(objs) + 1
    out.write(f"xref\n0 {top}\n".encode() + b"0000000000 65535 f \n")
    for n in range(1, top):
        out.write(f"{offsets.get(n, 0):010d} 00000 n \n".encode() if n in offsets else b"0000000000 65535 f \n")
    out.write(f"trailer\n<< /Size {top} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def para(text, font=""):
    rpr = f'<w:rPr><w:rFonts w:ascii="{font}"/></w:rPr>' if font else ""
    return f"<w:p><w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>"


def docx(paras, *, table=False, columns=1, textbox="", drawing=False, header="", footer="", font=""):
    body = "".join(para(p, font) for p in paras)
    if table:
        body += "<w:tbl><w:tr><w:tc>" + para("Skills") + "</w:tc><w:tc>" + para("Python") + "</w:tc></w:tr></w:tbl>"
    if textbox:
        body += f"<w:p><w:r><w:pict><w:txbxContent>{para(textbox)}</w:txbxContent></w:pict></w:r></w:p>"
    if drawing:
        body += "<w:p><w:r><w:drawing/></w:r></w:p>"
    body += f'<w:sectPr><w:cols w:num="{columns}"/></w:sectPr>'
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        if header:
            z.writestr("word/header1.xml", f'<w:hdr {W}>{para(header)}</w:hdr>')
        if footer:
            z.writestr("word/footer1.xml", f'<w:ftr {W}>{para(footer)}</w:ftr>')
    return buf.getvalue()


GOOD_TEXT = """Asha Verma
Backend engineer
asha@example.test | +91 98765 43210 | https://github.com/asha-verma-example

Summary
Backend engineer who builds reliable payment APIs.

Experience
Senior Backend Engineer at Payco, Jan 2021 - Present
- Built UPI payment APIs in Python handling 1M requests per day
- Tuned PostgreSQL queries and cut latency by 35%
Software Developer at Shopmate, Jun 2017 - Dec 2020
- Wrote Docker based deployment scripts

Skills
Python, PostgreSQL, Docker, Terraform

Education
B.Tech Computer Science, Example Institute, 2013 - 2017
"""


def codes(result):
    return {t["code"] for t in result["traps"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ats-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.profile = copy.deepcopy(PROFILE)
        self.audits = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)))

    def fb(self):
        return FactBase.build(self.profile, None, this_year=2026)

    def client(self):
        app = FastAPI()
        app.include_router(ats.make_router(self.ctx, profile_fn=lambda c: self.profile))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(c.close)
        return c


class DictionaryTests(unittest.TestCase):
    def test_synonyms_are_one_skill(self):
        for a, b in (("js", "javascript"), ("k8s", "kubernetes"), ("postgres", "postgresql"), ("golang", "go lang"), ("ml", "machine learning"),
                     ("amazon web services", "aws"), ("cicd", "ci/cd"), ("nodejs", "node.js"), ("ms excel", "excel")):
            self.assertEqual(skillwords.canonical(a), skillwords.canonical(b), (a, b))
        self.assertEqual(set(skillwords.find("Built with K8s, JS and Postgres")), {"kubernetes", "javascript", "postgresql"})

    def test_indian_market_and_soft_terms(self):
        found = set(skillwords.find("Experience with UPI, NPCI rules, GST filing and Razorpay. Strong communication skills."))
        self.assertTrue({"upi", "npci", "gst filing", "payment gateway", "communication"} <= found)
        self.assertEqual(skillwords.kind_of("communication"), "soft")
        self.assertEqual(skillwords.kind_of("pmp"), "cert")

    def test_ordinary_words_are_not_skills(self):
        self.assertNotIn("golang", skillwords.find("We go to market with our product next quarter"))
        self.assertNotIn("shell", skillwords.find("She sells sea shells and the hard shell is thick"))
        self.assertIn("golang", skillwords.find("Languages: Python, Go, SQL"))

    def test_longest_spelling_wins(self):
        found = skillwords.find("react native and react")
        self.assertIn("react native", found)
        self.assertEqual(len(found["react"]), 1)


class KeywordTests(Base):
    def test_weights_by_where_the_word_appears(self):
        rows = {r["term"]: r for r in ats.extract_keywords(JOB)}
        self.assertEqual(rows["python"]["bucket"], "must")
        self.assertTrue(rows["python"]["in_title"])
        self.assertEqual(rows["graphql"]["bucket"], "nice")
        self.assertGreater(rows["kubernetes"]["weight"], rows["graphql"]["weight"])
        self.assertGreater(rows["python"]["weight"], rows["kubernetes"]["weight"])
        self.assertEqual(rows["communication"]["kind"], "soft")
        self.assertIn("postgresql", rows)

    def test_phrases_the_dictionary_does_not_know(self):
        job = {"title": "Engineer", "description": "Requirements\n- Experience with Snowpipe, Temporal and Python\n- Must know PCI DSS"}
        rows = {r["term"]: r for r in ats.extract_keywords(job)}
        self.assertFalse(rows["snowpipe"]["known"])
        self.assertTrue(rows["python"]["known"])
        self.assertLess(rows["snowpipe"]["weight"], rows["python"]["weight"])
        self.assertIn("temporal", rows)

    def test_odd_input_never_raises(self):
        for job in ({}, {"title": ""}, {"description": None}, None, {"description": "x" * 20000}, {"title": "\u0000​"}):
            self.assertIsInstance(ats.extract_keywords(job), list)
        self.assertIsNone(ats.score(self.fb(), {}, "")["breakdown"][0]["score"])


class ScoreTests(Base):
    def resume_text(self, extra=""):
        return T.to_text(T.build_resume(self.fb(), None)) + extra

    def test_score_and_breakdown(self):
        r = ats.score(self.fb(), JOB, self.resume_text())
        self.assertTrue(0 <= r["score"] <= 100)
        keys = [b["key"] for b in r["breakdown"]]
        self.assertEqual(keys, ["hard", "soft", "title", "years", "education", "location"])
        by = {b["key"]: b for b in r["breakdown"]}
        self.assertEqual(by["years"]["required"], 5)
        self.assertEqual(by["years"]["score"], 100)          # 2017 - 2026
        self.assertEqual(by["education"]["score"], 100)      # B.Tech meets "Bachelor's"
        self.assertEqual(by["location"]["score"], 100)       # remote
        self.assertIn("python", by["hard"]["matched"])
        self.assertIn("postgresql", [m["term"] for m in r["matched"]])
        self.assertEqual(r["label"], ats._label(r["score"]))

    def test_synonym_match_k8s_and_postgres(self):
        job = {"title": "Platform Engineer", "description": "Requirements\n- Experience with k8s and Postgres"}
        r = ats.score(self.fb(), job, "Worked with Kubernetes and PostgreSQL daily")
        self.assertEqual({m["term"] for m in r["matched"]}, {"kubernetes", "postgresql"})
        self.assertEqual(r["missing"]["gap"], [])

    def test_missing_keywords_split_facts_vs_gap(self):
        # Terraform is a confirmed skill of the owner but missing from this resume text; Kubernetes is not in the facts at all.
        job = {"title": "Platform Engineer", "description": "Requirements\n- Experience with Terraform, Kubernetes and Python"}
        r = ats.score(self.fb(), job, "Python developer. Docker.")
        listed = {k["term"]: k for k in r["missing"]["listed_in_facts"]}
        gap = {k["term"] for k in r["missing"]["gap"]}
        self.assertIn("terraform", listed)
        self.assertTrue(listed["terraform"]["fact_ids"])
        self.assertEqual(gap, {"kubernetes"})
        self.assertNotIn("terraform", gap)
        self.assertTrue(any("kubernetes" in t and "gap" in t for t in r["tips"]))

    def test_a_gap_is_never_inserted_anywhere(self):
        fb = self.fb()
        before = [dict(f) for f in fb.facts]
        r = ats.score(fb, JOB, self.resume_text())
        self.assertIn("kubernetes", {k["term"] for k in r["missing"]["gap"]})
        self.assertEqual(before, [dict(f) for f in fb.facts])
        for job in (JOB, {"title": "Kubernetes Platform Engineer", "description": "Requirements\n- Kubernetes, Helm, Istio"}):
            text = "\n".join(ln for ln in T.to_text(T.build_resume(fb, job)).lower().splitlines() if not ln.startswith("seeking:"))   # 'Seeking: <posting title>' is the posting's own words
            self.assertNotIn("kubernetes", text)
            self.assertNotIn("helm", text)
            self.assertNotIn("istio", text)

    def test_must_have_missing_costs_more_than_nice_to_have(self):
        base = "Requirements\n- Experience with Python\n"
        miss_must = ats.score(self.fb(), {"title": "Engineer", "description": base + "- Experience with Kafka"}, "Python")
        miss_nice = ats.score(self.fb(), {"title": "Engineer", "description": base + "Nice to have\n- Experience with Kafka"}, "Python")
        self.assertLess(miss_must["breakdown"][0]["score"], miss_nice["breakdown"][0]["score"])

    def test_empty_categories_do_not_drag_the_score(self):
        job = {"title": "Python Developer", "description": "Requirements\n- Python"}
        r = ats.score(self.fb(), job, "Python")
        by = {b["key"]: b for b in r["breakdown"]}
        self.assertIsNone(by["soft"]["score"])
        self.assertEqual(by["soft"]["weight"], 0)
        self.assertGreaterEqual(r["score"], 80)

    def test_years_education_certs_location(self):
        fb = self.fb()
        self.assertEqual(ats.required_years("We need 8+ years of experience in sales. Founded 12 years ago."), 8)
        y = ats.score_years(fb, "10+ years of experience")
        self.assertEqual((y["required"], y["score"]), (10, 90))
        e = ats.score_education(fb, "PhD required. AWS certified solutions architect preferred.", "", set())
        self.assertLess(e["score"], 50)
        self.assertEqual(e["required_certs"], ["aws certified"])
        loc = ats.score_location(fb, {"location": "Pune", "description": "On site in Pune"}, "Asha, Mumbai")
        self.assertEqual(loc["score"], 30)
        self.assertEqual(ats.score_location(fb, {"description": "Work from home"}, "")["score"], 100)
        self.profile["links"]["email"] = "asha@example.test"
        fb2 = FactBase.build(self.profile, {"confirmed": [], "hidden": [], "aliases": {}, "prefs": {"location": "Pune"}}, this_year=2026)
        self.assertEqual(ats.score_location(fb2, {"location": "Pune"}, "")["score"], 100)

    def test_title_match(self):
        self.assertEqual(ats.score_title("Backend Engineer", ["Backend Developer"], "")["score"], 100)
        self.assertLess(ats.score_title("Data Scientist", ["Backend Developer"], "")["score"], 40)
        senior = ats.score_title("Senior Backend Engineer", ["Backend Engineer"], "Backend engineer")
        self.assertLess(senior["score"], 100)
        self.assertIn("level", senior["note"])

    def test_the_score_does_not_send_or_write(self):
        before = sorted(p.name for p in Path(self.config.data_path()).glob("*"))
        ats.score(self.fb(), JOB, self.resume_text())
        self.assertEqual(before, sorted(p.name for p in Path(self.config.data_path()).glob("*")))
        src = Path(ats.__file__).read_text(encoding="utf-8")
        for bad in ("send_mail", "gmailx", "smtplib", "requests.", "httpx"):
            self.assertNotIn(bad, src)


class ReadCheckTests(Base):
    def test_clean_text_is_read_fully(self):
        r = ats.read_check("resume.txt", text=GOOD_TEXT)
        f = r["fields"]
        self.assertEqual(f["name"], "Asha Verma")
        self.assertEqual(f["email"], "asha@example.test")
        self.assertIn("98765", f["phone"])
        self.assertIn("Senior Backend Engineer", f["titles"])
        self.assertIn("Payco", f["companies"])
        self.assertTrue(any("2021" in d for d in f["dates"]))
        self.assertIn("Python", f["skills"])
        self.assertTrue(f["education"])
        self.assertEqual(r["traps"], [])
        self.assertEqual(r["readable"], 100)
        self.assertIn("Payco", r["ats_view"])

    def test_text_traps(self):
        bad = """Asha Verma

My Journey
Experience
Senior Backend Engineer at Payco
➤ Built things ✅ and more • ﬁ ​
Toolbox
Python
"""
        r = ats.read_check("resume.txt", text=bad)
        c = codes(r)
        self.assertTrue({"no_email", "no_phone", "odd_sections", "no_dates", "special_chars", "sections"} & c)
        self.assertIn("no_email", c)
        self.assertIn("odd_sections", c)
        self.assertIn("special_chars", c)
        self.assertIn("no_dates", c)
        self.assertLess(r["readable"], 60)
        first = r["traps"][0]
        self.assertEqual(first["severity"], "high")

    def test_dates_with_years_only_and_partial(self):
        txt = GOOD_TEXT.replace("Jan 2021 - Present", "2021 - Present").replace("Jun 2017 - Dec 2020", "2017 - 2020")
        self.assertIn("year_only", codes(ats.read_check("r.txt", text=txt)))
        txt2 = GOOD_TEXT.replace("Software Developer at Shopmate, Jun 2017 - Dec 2020", "Software Developer at Shopmate")
        self.assertIn("some_dates", codes(ats.read_check("r.txt", text=txt2)))

    def test_docx_traps_one_by_one(self):
        lines = GOOD_TEXT.splitlines()
        clean = ats.read_check("r.docx", docx(lines))
        self.assertEqual(clean["format"], "docx")
        self.assertEqual(clean["traps"], [])
        self.assertEqual(clean["fields"]["email"], "asha@example.test")
        self.assertIn("table", codes(ats.read_check("r.docx", docx(lines, table=True))))
        self.assertIn("columns", codes(ats.read_check("r.docx", docx(lines, columns=2))))
        self.assertIn("textbox", codes(ats.read_check("r.docx", docx(lines, textbox="Python, Docker"))))
        self.assertIn("images", codes(ats.read_check("r.docx", docx(lines, drawing=True))))
        self.assertIn("fonts", codes(ats.read_check("r.docx", docx(lines, font="Comic Sans MS"))))
        self.assertNotIn("fonts", codes(ats.read_check("r.docx", docx(lines, font="Calibri"))))

    def test_docx_contact_in_header_is_flagged(self):
        no_contact = [ln for ln in GOOD_TEXT.splitlines() if "@" not in ln]
        r = ats.read_check("r.docx", docx(no_contact, header="Asha Verma | asha@example.test | +91 98765 43210"))
        t = next(x for x in r["traps"] if x["code"] == "header_contact")
        self.assertEqual(t["severity"], "high")                 # and not in the body either
        self.assertIn("no_email", codes(r))
        both = ats.read_check("r.docx", docx(GOOD_TEXT.splitlines(), footer="asha@example.test"))
        self.assertEqual(next(x for x in both["traps"] if x["code"] == "header_contact")["severity"], "medium")

    def test_pdf_clean_and_columns(self):
        rows = [(72, 780 - n * 16, ln) for n, ln in enumerate(GOOD_TEXT.splitlines()) if ln.strip()]
        clean = ats.read_check("r.pdf", pdf(rows))
        self.assertEqual(clean["format"], "pdf")
        self.assertEqual(clean["fields"]["email"], "asha@example.test")
        self.assertNotIn("columns", codes(clean))
        self.assertNotIn("scanned", codes(clean))
        two = []
        for n in range(12):
            two.append((60, 780 - n * 16, f"Skills line number {n} left"))
            two.append((330, 780 - n * 16, f"Experience line {n} right"))
        r = ats.read_check("r.pdf", pdf(two))
        self.assertIn("columns", codes(r))
        self.assertEqual(next(t for t in r["traps"] if t["code"] == "columns")["severity"], "high")

    def test_pdf_text_in_images_only(self):
        r = ats.read_check("scan.pdf", pdf([], images=1))
        self.assertIn("scanned", codes(r))
        self.assertEqual(r["traps"][0]["severity"], "high")
        some = ats.read_check("r.pdf", pdf([(72, 700 - n * 16, ln) for n, ln in enumerate(GOOD_TEXT.splitlines()) if ln.strip()], images=1))
        self.assertIn("images", codes(some))
        self.assertNotIn("scanned", codes(some))

    def test_pdf_contact_in_the_margin_and_odd_font(self):
        body = [(72, 700 - n * 16, ln) for n, ln in enumerate(GOOD_TEXT.splitlines()[3:]) if ln.strip()]
        r = ats.read_check("r.pdf", pdf(body + [(72, 30, "Asha Verma asha@example.test +91 98765 43210")], font="ComicSansMS"))
        self.assertIn("header_contact", codes(r))
        self.assertIn("fonts", codes(r))

    def test_our_own_exports_read_cleanly(self):
        v = T.build_resume(self.fb(), None)
        lines = T.render_lines(v)
        for name, data in (("r.pdf", T.pdf_bytes(lines)), ("r.docx", T.docx_bytes(lines))):
            r = ats.read_check(name, data)
            self.assertFalse([t for t in r["traps"] if t["severity"] == "high" and t["code"] not in ("no_email", "no_phone")], (name, r["traps"]))
            self.assertEqual(r["fields"]["name"], "Asha Verma", name)

    def test_big_and_unreadable_files(self):
        big = ("Asha Verma\nasha@example.test\n" + "Python " * 10).encode() + b"x" * (2 * 1024 * 1024 + 10)
        self.assertIn("big_file", codes(ats.read_check("r.txt", big)))
        with self.assertRaises(ats.ReadError):
            ats.read_check("r.txt", b"a" * (6 * 1024 * 1024 + 1))
        for name in ("r.doc", "r.rtf", "r.pages"):
            with self.assertRaises(ats.ReadError):
                ats.read_check(name, b"abc")
        with self.assertRaises(ats.ReadError):
            ats.read_check("r.txt", b"")
        with self.assertRaises(ats.ReadError):
            ats.read_check("r.docx", b"PK\x03\x04 not a real zip")
        with self.assertRaises(ats.ReadError):
            ats.read_check("r.pdf", b"%PDF-1.4 broken")

    def test_empty_text_is_a_high_trap(self):
        self.assertIn("empty", codes(ats.read_check("r.txt", text="  \n ")))


class RouteTests(Base):
    def test_auth_required(self):
        app = FastAPI()
        app.include_router(ats.make_router(self.ctx))
        c = TestClient(app)
        for method, path in (("get", "/api/ats/info"), ("post", "/api/ats/score"), ("post", "/api/ats/readcheck")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, path)

    def test_score_route(self):
        c = self.client()
        r = c.post("/api/ats/score", json={"job": JOB})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIn("breakdown", d)
        self.assertEqual(d["resume"], "your base resume (every fact)")
        pasted = c.post("/api/ats/score", json={"job": JOB, "resume_text": "Python Docker"}).json()
        self.assertLess(pasted["score"], d["score"])
        self.assertEqual(c.post("/api/ats/score", json={"job": {}}).status_code, 400)
        self.assertEqual(c.post("/api/ats/score", json={"job": JOB, "variant_id": "tv-nope"}).status_code, 404)
        self.assertEqual(c.post("/api/ats/score", json={"application_id": "abcdef123456"}).status_code, 404)

    def test_score_without_facts(self):
        self.profile = {}
        self.assertEqual(self.client().post("/api/ats/score", json={"job": JOB}).status_code, 400)

    def test_readcheck_route_variants(self):
        c = self.client()
        raw = docx(GOOD_TEXT.splitlines(), table=True)
        r = c.post("/api/ats/readcheck", json={"filename": "cv.docx", "data_b64": base64.b64encode(raw).decode()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("table", {t["code"] for t in r.json()["traps"]})
        self.assertEqual(c.post("/api/ats/readcheck", json={"filename": "cv.docx", "data_b64": "!!!"}).status_code, 400)
        t = c.post("/api/ats/readcheck", json={"text": GOOD_TEXT}).json()
        self.assertEqual(t["traps"], [])
        self.assertEqual(c.post("/api/ats/readcheck", json={}).status_code, 400)       # no saved resume text
        self.assertTrue(any(a[0][0] == "ats_readcheck" for a in self.audits))
        for a in self.audits:
            self.assertNotIn("asha", str(a).lower())                                    # sizes only, never content

    def test_readcheck_of_the_saved_resume_says_layout_cannot_be_checked(self):
        self.profile["resume_text"] = GOOD_TEXT
        d = self.client().post("/api/ats/readcheck", json={}).json()
        self.assertTrue(d["from_profile"])
        self.assertIn("layout", d["note"])

    def test_disabled(self):
        self.config.tailor_enabled = False
        c = self.client()
        self.assertEqual(c.post("/api/ats/score", json={"job": JOB}).status_code, 404)
        self.assertEqual(c.post("/api/ats/readcheck", json={"text": GOOD_TEXT}).status_code, 404)


class ToolTests(unittest.TestCase):
    def test_tool_is_read_only_and_grouped(self):
        from hub.assistant import TOOL_GROUPS
        tools = ats.build(lambda: None, None)
        self.assertEqual([t.spec["name"] for t in tools], ["ats_score"])
        self.assertFalse(tools[0].needs_approval({}))
        self.assertIn("ats_score", TOOL_GROUPS["ats"][0])
        self.assertTrue(TOOL_GROUPS["ats"][1].search("how does my resume match this ATS"))


if __name__ == "__main__":
    unittest.main()
