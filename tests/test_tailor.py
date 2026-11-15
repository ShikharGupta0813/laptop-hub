"""Fact base, fit and honesty check, tailoring, answer bank (hub/factbase.py, fitcheck.py, tailor.py, answerbank.py).

Fakes only: a fake clock, fake models that return canned JSON (so the prompt and the grounding rules are tested without any AI),
a fake embedder, the real application queue on a temp folder with a fake mailer. The rules under test:
- every sentence is traceable to fact ids; a sentence the facts do not back is flagged and BLOCKS the variant;
- nothing is sent; low-confidence and owner-only answers are never auto-filled; AI answers wait for the owner's OK."""
import asyncio
import copy
import datetime as dt
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import answerbank, applyq, factbase as F, fitcheck as FC, formfill, tailor as T
from hub.assistant import TOOL_GROUPS, route_local_tools
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 9, 15, 0)

PROFILE = {
    "name": "Jo Rivera", "headline": "Backend engineer",
    "summary": "Backend engineer who builds reliable data APIs. Enjoys mentoring.",
    "skills": ["Python", "FastAPI", "Postgres", "Docker", "Technical writing"],
    "experience": [
        {"title": "Senior Developer", "company": "Initech", "period": "2021 - Present",
         "bullets": ["Built a Python and FastAPI service handling 2M requests per day", "Cut Postgres query time by 40% with indexing",
                     "Mentored 3 junior engineers"]},
        {"title": "Developer", "company": "Hooli", "period": "2016 - 2021",
         "bullets": ["Wrote Docker based deployment scripts", "Organised the office party"]}],
    "education": "BSc Computer Science, State University",
    "links": {"github": "https://github.com/jo-rivera", "email": "jo@example.test"},
    "standard_answers": {"Notice period": "2 weeks"},
    "resume_text": "PRIVATE RESUME TEXT jo@example.test +1 555 0100",
}

JOB = {"title": "Senior Python Backend Engineer", "company": "Acme", "url": "https://jobs.example.test/acme/1",
       "contact_email": "jobs@acme.test",
       "description": """About us
We build things.

Requirements
- 5+ years of Python
- Experience with PostgreSQL and Docker
- Kubernetes experience
- Bachelor's degree in computer science or equivalent

Nice to have
- Experience with GraphQL

Location: Berlin, Germany. Salary $90,000 - $110,000 per year. Must be authorized to work in the EU.
"""}


class FakeModel:
    """Returns queued replies (strings of JSON) and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, user, json_mode=False):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        if not self.replies:
            raise RuntimeError("model down")
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


def sentences(*items):
    return {"sentences": [{"text": t, "fact_ids": ids, "kind": kind} for t, ids, kind in items]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tailor-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits = []
        self.model = None
        self.profile = copy.deepcopy(PROFILE)
        self.ctx = Ctx(config=self.config, model_for=lambda p: self.model, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)))
        self.clock = lambda: NOW

    def fb(self, **prefs):
        store = F.load(self.ctx)
        store["prefs"] = F.clean_prefs(prefs, store["prefs"])
        return F.FactBase.build(self.profile, store, this_year=2026)

    def job(self, **over):
        return T.clean_job({**JOB, **over})

    def tailor(self):
        return T.Tailor(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, model_fn=lambda p: self.model)

    def bank(self, **kw):
        return answerbank.Bank(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, model_fn=lambda p: self.model, **kw)

    def client(self):
        app = FastAPI()
        t = T.make_router(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, model_fn=lambda p: self.model, applyq_fn=lambda: self.queue())
        b = answerbank.make_router(self.ctx, clock=self.clock, profile_fn=lambda c: self.profile, model_fn=lambda p: self.model)
        app.include_router(t)
        app.include_router(b)
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(c.close)
        return c

    def queue(self):
        class Mail:
            def send_mail(*a, **k):
                raise AssertionError("tailoring must never send")
        return applyq.Applyq(self.ctx, clock=lambda: NOW, profile_fn=lambda c: self.profile, gmailx=Mail())


class FactBaseTests(Base):
    def test_extracts_typed_facts_with_stable_ids(self):
        fb = self.fb()
        kinds = {f["type"] for f in fb.facts}
        self.assertTrue({"name", "headline", "summary", "skill", "role", "bullet", "metric", "education", "link", "answer", "derived"} <= kinds)
        again = F.FactBase.build(self.profile, None, this_year=2026)
        self.assertEqual([f["id"] for f in fb.facts if f["type"] == "bullet"], [f["id"] for f in again.facts if f["type"] == "bullet"])
        self.assertEqual(len({f["id"] for f in fb.facts}), len(fb.facts))
        for f in fb.facts:
            self.assertTrue(f["source"])

    def test_skill_evidence_dates_and_derived_years(self):
        fb = self.fb()
        py = next(f for f in fb.of_type("skill") if f["text"] == "Python")
        self.assertEqual(py["years"], 5)                       # 2021 .. 2026 from the bullet's role
        self.assertTrue(py["evidence"])
        tw = next(f for f in fb.of_type("skill") if f["text"] == "Technical writing")
        self.assertEqual((tw["years"], tw["evidence"]), (0, []))   # listed, with nothing to show for it
        self.assertEqual(fb.years_experience(), 10)
        self.assertEqual(fb.get("calc-years")["years"], 10)

    def test_metrics_extracted_from_bullets(self):
        fb = self.fb()
        texts = {m["text"] for m in fb.of_type("metric")}
        self.assertIn("40%", texts)
        self.assertTrue(any("2M" in t for t in texts))
        self.assertNotIn("2021", texts)

    def test_confirmed_facts_hidden_facts_and_prefs(self):
        e = F.add_confirmed(self.ctx, "Spoke at a regional Python meetup in 2023", ["talk"], clock=self.clock)
        self.assertEqual(F.add_confirmed(self.ctx, "Spoke at a regional Python meetup in 2023", clock=self.clock)["id"], e["id"])
        fb = self.fb()
        self.assertEqual(fb.get(e["id"])["type"], "confirmed")
        self.assertEqual(fb.get(e["id"])["source"], "confirmed by you")
        bullet = fb.of_type("bullet")[0]
        self.assertEqual(F.delete_fact(self.ctx, bullet["id"]), "hidden")
        self.assertIsNone(self.fb().get(bullet["id"]))
        F.unhide(self.ctx, bullet["id"])
        self.assertIsNotNone(self.fb().get(bullet["id"]))
        self.assertEqual(F.delete_fact(self.ctx, e["id"]), "deleted")
        with self.assertRaises(ValueError):
            F.add_confirmed(self.ctx, "ab")
        self.assertEqual(F.set_prefs(self.ctx, {"rate": "$60/hour"})["rate"], "$60/hour")
        self.assertEqual(self.fb().get("pref-rate")["value"], "$60/hour")
        with self.assertRaises(ValueError):
            F.set_prefs(self.ctx, {"timezone_offset": 99})
        mode = (Path(self.config.data_path()) / "factbase.json").stat().st_mode & 0o777
        if sys.platform != "win32":      # Windows has no POSIX permission bits
            self.assertEqual(mode & 0o077, 0)

    def test_string_experience_links_list_and_empty_profile(self):
        fb = F.FactBase.build({"experience": ["Ran a shop"], "links": ["https://example.test/me"], "skills": "Go, Rust"}, None, this_year=2026)
        self.assertEqual(len(fb.of_type("bullet")), 1)
        self.assertEqual(len(fb.of_type("link")), 1)
        self.assertEqual({f["text"] for f in fb.of_type("skill")}, {"Go", "Rust"})
        self.assertEqual(F.FactBase.build({}, None).facts, [])

    def test_aliases_map_words(self):
        F.set_aliases(self.ctx, {"K8s": "Kubernetes"})
        self.assertEqual(F.load(self.ctx)["aliases"], {"k8s": "kubernetes"})
        self.assertEqual(F.job_terms({"title": "k8s admin"}, self.fb()).get(F.phrase_key("kubernetes")), 2)


class FitTests(Base):
    def test_requirements_matched_partial_missing_and_score(self):
        fit = FC.analyse(self.fb(), self.job())
        by = {r["text"]: r for r in fit["requirements"]}
        self.assertEqual(by["5+ years of Python"]["status"], "matched")
        self.assertEqual(by["Experience with PostgreSQL and Docker"]["status"], "matched")   # postgres ~ postgresql
        self.assertEqual(by["Kubernetes experience"]["status"], "missing")
        self.assertEqual(by["Experience with GraphQL"]["status"], "missing")
        self.assertEqual(by["Bachelor's degree in computer science or equivalent"]["status"], "matched")
        for r in fit["requirements"]:
            if r["status"] == "matched":
                self.assertTrue(r["evidence"], r["text"])
        self.assertTrue(0 < fit["score"] < 100)
        self.assertEqual(fit["counts"]["missing"], 2)
        self.assertNotIn("Location: Berlin", " ".join(by))        # metadata lines are not requirements

    def test_partial_and_years_shortfall(self):
        job = self.job(description="Requirements\n- Experience with Docker and Terraform\n- 8+ years of Python\n")
        by = {r["text"]: r for r in FC.analyse(self.fb(), job)["requirements"]}
        self.assertEqual(by["Experience with Docker and Terraform"]["status"], "partial")
        self.assertEqual(by["8+ years of Python"]["status"], "missing")      # 5 of 8 years is 62%, under the 70% line

    def test_years_rule_is_seventy_percent(self):
        job = self.job(description="Requirements\n- 9+ years of Python\n")
        r = FC.analyse(self.fb(), job)["requirements"][0]
        self.assertEqual(r["status"], "missing")
        job = self.job(description="Requirements\n- 7+ years of Python\n")
        self.assertEqual(FC.analyse(self.fb(), job)["requirements"][0]["status"], "partial")

    def test_skill_listed_without_evidence_is_partial_for_years(self):
        job = self.job(description="Requirements\n- 3+ years of technical writing\n")
        r = FC.analyse(self.fb(), job)["requirements"][0]
        self.assertEqual(r["status"], "partial")

    def test_flags_need_preferences_and_say_what_is_unchecked(self):
        fit = FC.analyse(self.fb(), self.job())
        self.assertTrue(any("location" in u for u in fit["unchecked"]))
        self.assertTrue(any("work authorization" in u for u in fit["unchecked"]))
        codes = {f["code"] for f in fit["flags"]}
        self.assertNotIn("location_mismatch", codes)
        self.assertIn("work_authorization", codes)

    def test_location_pay_timezone_visa_flags(self):
        fb = self.fb(location="Lisbon, Portugal", min_pay_yearly=150000, timezone_offset=0, tz_tolerance_hours=3, work_auth=["Portugal"], needs_sponsorship=True)
        job = self.job(description=JOB["description"] + "\nOn-site in the office. Hours 9-5 EST. We do not provide visa sponsorship.")
        codes = {f["code"]: f for f in FC.analyse(fb, job)["flags"]}
        self.assertIn("location_mismatch", codes)
        self.assertIn("pay_below_minimum", codes)
        self.assertIn("timezone_mismatch", codes)
        self.assertEqual(codes["work_authorization"]["severity"], "high")

    def test_remote_with_matching_region_has_no_location_flag(self):
        fb = self.fb(location="Berlin, Germany", min_pay_yearly=50000)
        job = self.job(description="Remote role. Location: Berlin, Germany.\nRequirements\n- Python\nPay: $90,000 per year")
        codes = {f["code"] for f in FC.analyse(fb, job)["flags"]}
        self.assertNotIn("location_mismatch", codes)
        self.assertNotIn("pay_below_minimum", codes)

    def test_region_restriction(self):
        fb = self.fb(location="Lisbon, Portugal")
        job = self.job(description="Fully remote. Candidates must be located in the United States.\nRequirements\n- Python")
        self.assertIn("region_restriction", {f["code"] for f in FC.analyse(fb, job)["flags"]})

    def test_seniority_flags(self):
        under = FC.analyse(F.FactBase.build({"experience": [{"title": "Dev", "company": "X", "period": "2024 - Present", "bullets": ["Built a Go tool"]}]}, None, this_year=2026),
                           self.job(title="Principal Engineer", description="Requirements\n- Go"))
        self.assertIn("under_qualified", {f["code"] for f in under["flags"]})
        over = FC.analyse(self.fb(), self.job(title="Junior Python Developer", description="Requirements\n- Python"))
        self.assertIn("over_qualified", {f["code"] for f in over["flags"]})

    def test_scam_signals_reuse_opps_vet(self):
        job = self.job(description="Pay a registration fee first. Guaranteed income, message us on WhatsApp.\nRequirements\n- Python")
        fit = FC.analyse(self.fb(), job)
        self.assertTrue(fit["risk_blocked"])
        self.assertTrue(any(f["source"] == "scam" and f["severity"] == "high" for f in fit["flags"]))

    def test_odd_input_never_raises(self):
        for job in ({}, {"title": ""}, {"description": "x" * 5000}, {"title": "A", "description": "\n\n\n"}):
            FC.analyse(self.fb(), job)
        FC.analyse(F.FactBase.build({}, None), self.job())


class ClaimTests(Base):
    """Golden cases for the honesty check."""

    def setUp(self):
        super().setUp()
        self.fb_ = self.fb()
        self.bullet = next(f for f in self.fb_.of_type("bullet") if "FastAPI" in f["text"])
        self.postgres = next(f for f in self.fb_.of_type("bullet") if "Postgres" in f["text"])

    def check(self, text, ids, kind="claim", src=""):
        return FC.check_claims(self.fb_, [{"text": text, "fact_ids": ids, "kind": kind, "src": src}], self.job(), ["Jo Rivera"])

    def test_backed_claim_passes(self):
        self.assertTrue(self.check("I built a Python and FastAPI service handling 2M requests per day.", [self.bullet["id"]])["ok"])

    def test_claim_without_a_fact_is_unbacked(self):
        r = self.check("I led a team of 12 engineers.", [])
        self.assertFalse(r["ok"])
        self.assertIn("cites no fact", r["unbacked"][0]["reason"])

    def test_invented_number_is_unbacked(self):
        r = self.check("I built a Python service handling 50M requests per day.", [self.bullet["id"]])
        self.assertFalse(r["ok"])
        self.assertIn("50M", r["unbacked"][0]["reason"])

    def test_invented_tool_is_unbacked(self):
        r = self.check("I built a FastAPI service on Kubernetes.", [self.bullet["id"]])
        self.assertFalse(r["ok"])
        self.assertIn("Kubernetes", r["unbacked"][0]["reason"])

    def test_cites_real_fact_but_says_something_else(self):
        r = self.check("I managed a department and shipped products worldwide.", [self.bullet["id"]])
        self.assertFalse(r["ok"])

    def test_unknown_fact_id(self):
        r = self.check("I built a Python service.", ["bull-doesnotexist"])
        self.assertIn("does not exist", r["unbacked"][0]["reason"])

    def test_glue_may_not_smuggle_a_claim(self):
        self.assertFalse(self.check("I have ten years of leadership experience.", [], "glue")["ok"])
        self.assertTrue(self.check("Would a short conversation make sense?", [], "glue")["ok"])
        self.assertTrue(self.check("I am applying for the Senior Python Backend Engineer role at Acme.", [], "glue")["ok"])

    def test_glue_with_unknown_capitalised_name_is_unbacked(self):
        self.assertFalse(self.check("I also know Terraform.", [], "glue")["ok"])

    def test_plan_and_quote(self):
        self.assertTrue(self.check('Agree what done looks like for "Kubernetes experience", then build and test it.', [], "plan", "template")["ok"])
        self.assertTrue(self.check("5+ years of Python", [], "quote")["ok"])
        self.assertFalse(self.check("Must have ten years of Haskell", [], "quote")["ok"])

    def test_fill_marker_is_not_a_claim(self):
        self.assertTrue(self.check("Rate: [FILL: your rate]", [], "glue")["ok"])

    def test_near_spelling_of_a_skill_is_backed(self):
        self.assertTrue(self.check("I cut PostgreSQL query time by 40% with indexing.", [self.postgres["id"]])["ok"])

    def test_alias_backs_a_posting_spelling(self):
        F.set_aliases(self.ctx, {"k8s": "kubernetes"})
        self.profile["skills"].append("k8s")
        self.fb_ = self.fb()
        k = next(f for f in self.fb_.of_type("skill") if f["text"] == "k8s")
        self.assertTrue(self.check("Skills I bring to this role: Kubernetes.", [k["id"]])["ok"])


class TailorBuildTests(Base):
    def test_resume_reorders_and_keeps_only_backed_lines(self):
        fb = self.fb()
        job = self.job(title="Platform role", description="Requirements\n- Postgres performance tuning\n- Postgres indexing\n- Docker\n")
        resume = T.build_resume(fb, job)
        exp = next(s for s in resume["sections"] if s["name"] == "Experience")["lines"]
        bullets = [ln["text"] for ln in exp if ln["style"] == "bullet"]
        self.assertTrue(bullets[0].startswith("Cut Postgres"))             # the Postgres bullet moved to the top
        for sec in resume["sections"]:
            for ln in sec["lines"]:
                if ln["kind"] == "claim":
                    self.assertTrue(ln["fact_ids"], ln["text"])
                    for i in ln["fact_ids"]:
                        self.assertIn(i, fb.by_id)
        res = FC.check_claims(fb, T.resume_sentences(resume), job, ["Jo Rivera"])
        self.assertTrue(res["ok"], res["unbacked"])

    def test_keywords_mirrored_only_when_backed_by_a_fact(self):
        fb = self.fb()
        md = T.to_markdown(T.build_resume(fb, self.job()))
        self.assertIn("PostgreSQL", md)                 # the posting's spelling of the owner's Postgres skill
        self.assertNotIn("Kubernetes", md)              # not in the facts, so never added
        self.assertNotIn("GraphQL", md)
        self.assertNotIn("Seeking: Senior Python Backend Engineer Kubernetes", md)

    def test_base_resume_is_complete_and_in_order(self):
        md = T.to_markdown(T.build_resume(self.fb(), None))
        self.assertIn("Organised the office party", md)
        self.assertLess(md.index("Python"), md.index("Postgres"))

    def test_limit_bullets_for_irrelevant_older_roles(self):
        self.profile["experience"].append({"title": "Clerk", "company": "Old Co", "period": "2008 - 2012", "bullets": ["Filed paper", "Answered phones", "Made tea"]})
        resume = T.build_resume(self.fb(), self.job())
        exp = next(s for s in resume["sections"] if s["name"] == "Experience")["lines"]
        i = next(n for n, ln in enumerate(exp) if "Old Co" in ln["text"])
        self.assertEqual(sum(1 for ln in exp[i + 1:] if ln["style"] == "bullet"), 1)

    def test_diff_against_base(self):
        fb = self.fb()
        base, var = T.build_resume(fb, None), T.build_resume(fb, self.job(description="Requirements\n- Postgres\n"), max_bullets=1)
        d = T.diff_resume(base, var)
        self.assertIn("--- base resume", d["unified"])
        self.assertTrue(d["removed"])
        self.assertTrue(any("Postgres" in x for x in d["added"] + d["removed"]) or d["kept"])

    def test_cover_opens_with_a_concrete_proof_and_cites_facts(self):
        fb = self.fb()
        cover = T.build_cover(fb, self.job(), "email")
        first = T.all_sentences(cover)[0]
        self.assertTrue(first["text"].startswith("At Initech, I built a Python and FastAPI service"))
        for s in T.all_sentences(cover):
            if s["kind"] == "claim":
                self.assertTrue(s["fact_ids"])
        self.assertTrue(T.check_cover(fb, cover, self.job())["ok"])
        text = T.cover_text(cover)
        for cliche in ("passionate", "excited", "dynamic", "team player", "thrilled", "perfect fit"):
            self.assertNotIn(cliche, text.lower())
        self.assertLess(cover["words"], 140)

    def test_cover_never_mentions_a_missing_skill(self):
        text = T.cover_text(T.build_cover(self.fb(), self.job(), "email"))
        self.assertNotIn("Kubernetes", text)
        self.assertNotIn("GraphQL", text)

    def test_channels_shape_the_message(self):
        fb, job = self.fb(), self.job()
        email = T.build_cover(fb, job, "email")
        form = T.build_cover(fb, job, "form")
        msg = T.build_cover(fb, job, "message")
        out = T.build_cover(fb, job, "outreach")
        free = T.build_cover(fb, job, "freelance")
        self.assertTrue(email["greeting"].startswith("Hi") and email["subject"].startswith("Application:"))
        self.assertEqual((form["greeting"], form["signature"], form["subject"]), ("", "", ""))
        self.assertEqual(len(T.all_sentences(msg)), 2)
        self.assertIn("conversation", T.cover_text(out))
        headings = [b["heading"] for b in free["blocks"]]
        self.assertIn("How I would do it", headings)
        self.assertIn("Availability and rate", headings)
        self.assertTrue(T.check_cover(fb, free, job)["ok"])

    def test_freelance_rate_comes_from_owner_setting_or_stays_blank(self):
        job = self.job(kind="gig")
        self.assertEqual(T.infer_channel(job), "freelance")
        blank = T.build_cover(self.fb(), job)
        self.assertIn("[FILL: your rate]", T.cover_text(blank))
        self.assertIn("your rate", blank["missing"])
        full = T.build_cover(self.fb(rate="$60/hour", availability="from next Monday"), job)
        text = T.cover_text(full)
        self.assertIn("Rate: $60/hour", text)
        self.assertIn("Availability: from next Monday", text)
        self.assertNotIn("[FILL", text)

    def test_tone_is_owner_editable(self):
        formal = T.build_cover(self.fb(tone="formal"), self.job(), "email")
        self.assertTrue(formal["greeting"].startswith("Dear") and formal["closing"] == "Kind regards,")
        custom = T.build_cover(self.fb(tone="brisk", tones={"brisk": {"greeting": "Morning {to},", "closing": "Cheers,"}}, sign_off="Best,"), self.job(), "email")
        self.assertEqual((custom["greeting"], custom["closing"]), ("Morning Acme team,", "Best,"))

    def test_missing_name_becomes_a_blank_not_an_invention(self):
        self.profile["name"] = ""
        cover = T.build_cover(self.fb(), self.job(), "email")
        self.assertIn("[FILL: your name]", T.cover_text(cover))

    def test_no_relevant_fact_says_only_what_is_true(self):
        job = self.job(title="Pastry chef", description="Requirements\n- Sourdough baking\n- Cake decorating\n")
        cover = T.build_cover(self.fb(), job, "email")
        text = T.cover_text(cover)
        self.assertNotIn("sourdough", text.lower())
        self.assertTrue(T.check_cover(self.fb(), cover, job)["ok"])

    def test_exports(self):
        fb, job = self.fb(), self.job()
        resume = T.build_resume(fb, job)
        pdf = T.pdf_bytes(T.render_lines(resume))
        self.assertTrue(pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"Jo Rivera", pdf)
        docx = T.docx_bytes(T.render_lines(resume))
        with zipfile.ZipFile(io.BytesIO(docx)) as z:
            self.assertIn("word/document.xml", z.namelist())
        from hub import profile
        self.assertIn("Initech", profile.docx_text(docx))      # the hub's own reader opens it
        self.assertIn("# Jo Rivera", T.to_markdown(resume))
        long = [("para", "word " * 3000)]
        self.assertGreater(T.pdf_bytes(long).count(b"/Type /Page "), 1)
        self.assertTrue(T.pdf_bytes([("para", "Zoë café “quotes” (parens) back\\slash")]).startswith(b"%PDF"))


class AICoverTests(Base):
    def setUp(self):
        super().setUp()
        self.fb_ = self.fb()
        self.job_ = self.job()
        self.base = T.build_cover(self.fb_, self.job_, "email")
        self.bullet = next(f for f in self.fb_.of_type("bullet") if "FastAPI" in f["text"])

    def run_ai(self, model):
        return asyncio.run(T.ai_cover(model, self.fb_, self.job_, self.base))

    def test_prompt_rules_and_payload_contents(self):
        model = FakeModel(sentences(("I built a Python and FastAPI service handling 2M requests per day.", [self.bullet["id"]], "claim")))
        self.run_ai(model)
        call = model.calls[0]
        for rule in ("ONLY the facts", "fact_ids", "Never invent", "No flattery", "DATA", "JSON"):
            self.assertIn(rule, call["system"])
        self.assertTrue(call["json_mode"])
        payload = json.loads(call["user"])
        ids = {f["id"] for f in payload["FACTS"]}
        self.assertIn(self.bullet["id"], ids)
        blob = call["user"]
        self.assertNotIn("PRIVATE RESUME TEXT", blob)         # the raw resume never goes to the model
        self.assertNotIn("jo@example.test", blob)             # nor the contact address
        self.assertIn("Kubernetes", payload["posting"]["description"])
        self.assertEqual(payload["channel"], "email")

    def test_grounded_reply_is_accepted_and_checked(self):
        model = FakeModel(sentences(("I built a Python and FastAPI service handling 2M requests per day.", [self.bullet["id"]], "claim"),
                                    ("Would a short conversation make sense?", [], "glue")))
        cover = self.run_ai(model)
        self.assertEqual(cover["generator"], "ai")
        self.assertTrue(T.check_cover(self.fb_, cover, self.job_)["ok"])
        self.assertEqual(len(model.calls), 1)

    def test_invented_claim_is_flagged_and_blocks_after_one_repair_try(self):
        bad = sentences(("I built a Python and FastAPI service handling 2M requests per day.", [self.bullet["id"]], "claim"),
                        ("I led a team of 40 engineers at Google.", [self.bullet["id"]], "claim"))
        model = FakeModel(bad, bad)
        cover = self.run_ai(model)
        self.assertEqual(len(model.calls), 2)                     # one repair attempt, told what was rejected
        self.assertIn("rejected_sentences", model.calls[1]["user"])
        T.check_cover(self.fb_, cover, self.job_)
        statuses = [s["status"] for s in T.all_sentences(cover)]
        self.assertEqual(statuses, ["ok", "unbacked"])
        variant = {"cover": cover, "fit": {}, "risk_ack": False}
        self.assertTrue(T.blockers(variant))

    def test_repair_succeeds(self):
        bad = sentences(("I led a team of 40 engineers at Google.", [self.bullet["id"]], "claim"))
        good = sentences(("I built a Python and FastAPI service handling 2M requests per day.", [self.bullet["id"]], "claim"))
        cover = self.run_ai(FakeModel(bad, good))
        self.assertTrue(T.check_cover(self.fb_, cover, self.job_)["ok"])

    def test_sentence_without_ids_cannot_pass_as_glue_claim(self):
        model = FakeModel(sentences(("I have a decade of leadership experience.", [], "glue")))
        cover = self.run_ai(FakeModel(*(model.replies * 2)))
        self.assertFalse(T.check_cover(self.fb_, cover, self.job_)["ok"])

    def test_unreadable_reply_and_model_down_fall_back_to_the_template(self):
        out = self.run_ai(FakeModel("not json at all", "still not json"))
        self.assertEqual(out["generator"], "template")
        self.assertIn("template", out["ai_note"])
        out = self.run_ai(FakeModel())
        self.assertEqual(out["generator"], "template")

    def test_preview_ai_flag_and_missing_model(self):
        t = self.tailor()
        self.model = None
        v = asyncio.run(t.preview({"job": JOB, "ai": True}))
        self.assertIn("No AI model", v["cover"]["ai_note"])
        self.model = FakeModel(sentences(("I built a Python and FastAPI service handling 2M requests per day.", [self.bullet["id"]], "claim")))
        v = asyncio.run(t.preview({"job": JOB, "ai": True}))
        self.assertEqual(v["cover"]["generator"], "ai")
        self.assertFalse(v["blocked"] and "do not back" in " ".join(v["blockers"]))


class VariantTests(Base):
    def preview(self, **body):
        return asyncio.run(self.tailor().preview({"job": JOB, **body}))

    def test_preview_stores_a_variant_with_fit_and_diff(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": JOB}))
        self.assertTrue(v["id"].startswith("tv-"))
        self.assertIn("score", v["fit"])
        self.assertEqual(t.get(v["id"])["job"]["company"], "Acme")
        self.assertTrue(t.diff(v["id"])["unified"] is not None)
        self.assertEqual(len(t.list()), 1)

    def test_no_facts_is_a_plain_error(self):
        self.profile = {}
        with self.assertRaises(T.TailorError):
            self.preview()

    def test_blocked_until_the_owner_resolves_each_flag(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": JOB}))
        self.assertFalse(v["blocked"])
        raw = t.get(v["id"])
        raw["cover"]["blocks"][0]["sentences"].append({"id": "s99", "text": "I won an industry award for leadership.", "fact_ids": [], "kind": "claim", "src": "ai", "status": "ok"})
        t._put(raw)
        raw = t.recheck(t.get(v["id"]))
        t._put(raw)
        self.assertTrue(T.public_variant(t.get(v["id"]))["blocked"])
        with self.assertRaises(T.TailorError):
            t.export(v["id"], "md", "cover")
        out = t.resolve(v["id"], {"action": "remove", "sentence": "s99"})
        self.assertFalse(out["blocked"])
        t.export(v["id"], "md", "cover")

    def test_edit_is_rechecked_and_confirm_adds_an_owner_fact(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": JOB}))
        raw = t.get(v["id"])
        raw["cover"]["blocks"][0]["sentences"].append({"id": "s99", "text": "I won an industry award for leadership.", "fact_ids": [], "kind": "claim", "src": "ai", "status": "unbacked"})
        t._put(raw)
        out = t.resolve(v["id"], {"action": "edit", "sentence": "s99", "text": "I led a team of 40 engineers.", "fact_ids": []})
        self.assertTrue(out["blocked"])                            # an edit with no fact still does not pass
        out = t.resolve(v["id"], {"action": "confirm", "sentence": "s99", "fact_text": "Led a team of 40 engineers at Initech"})
        self.assertFalse(out["blocked"])
        facts = F.load(self.ctx)["confirmed"]
        self.assertEqual(facts[0]["text"], "Led a team of 40 engineers at Initech")
        self.assertIn(facts[0]["id"], t.get(v["id"])["cover"]["blocks"][0]["sentences"][-1]["fact_ids"])
        self.assertIn(facts[0]["id"], F.FactBase.build(self.profile, F.load(self.ctx)).by_id)

    def test_fill_blank_and_risk_ack(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": {**JOB, "kind": "gig"}}))
        self.assertTrue(v["blocked"])
        self.assertIn("your rate", " ".join(v["blockers"]))
        out = t.resolve(v["id"], {"action": "fill", "label": "your rate", "value": "$75/hour"})
        self.assertFalse(out["blocked"])
        self.assertIn("$75/hour", T.cover_text(out["cover"]))
        self.assertNotIn("[FILL", T.cover_text(out["cover"]))
        scam = asyncio.run(t.preview({"job": {**JOB, "description": "Pay a registration fee first. Guaranteed income, message us on WhatsApp.\nRequirements\n- Python"}}))
        self.assertTrue(scam["blocked"])
        self.assertFalse(t.resolve(scam["id"], {"action": "ack_risk"})["blocked"])
        with self.assertRaises(T.TailorError):
            t.resolve(v["id"], {"action": "fill", "label": "nothing", "value": "x"})
        with self.assertRaises(T.TailorError):
            t.resolve(v["id"], {"action": "bogus"})

    def test_exports_per_variant(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": JOB}))
        for fmt, mime in (("md", "text/markdown"), ("txt", "text/plain"), ("docx", "officedocument"), ("pdf", "application/pdf")):
            for part in ("resume", "cover"):
                data, ctype, name = t.export(v["id"], fmt, part)
                self.assertTrue(data)
                self.assertIn(mime, ctype)
                self.assertTrue(name.endswith(fmt))
        with self.assertRaises(T.TailorError):
            t.export(v["id"], "exe", "resume")

    def test_variants_kept_per_application_and_apply_never_sends(self):
        q = self.queue()
        item, _ = q.add({**JOB, "url": "https://jobs.example.test/acme/1"})
        t = self.tailor()
        v1 = asyncio.run(t.preview({"application_id": item["id"]}))
        asyncio.run(t.preview({"job": JOB}))
        self.assertEqual([v["id"] for v in t.list(item["id"])], [v1["id"]])
        self.assertEqual(v1["job"]["title"], JOB["title"])
        out = t.apply_to_application(v1["id"], item["id"])
        self.assertFalse(out["blocked"])
        fresh = q.get(item["id"])
        self.assertIn("At Initech, I built a Python and FastAPI service", fresh["draft"]["body"])
        self.assertEqual(fresh["state"], "ready")             # applied to the draft only; the queue still waits for the owner's Send
        self.assertTrue(any("Tailored variant" in n["text"] for n in fresh["notes"]))

    def test_blocked_variant_blocks_the_queue_send(self):
        q = self.queue()
        item, _ = q.add({**JOB, "url": "https://jobs.example.test/acme/2"})
        t = self.tailor()
        v = asyncio.run(t.preview({"application_id": item["id"], "channel": "freelance"}))
        self.assertTrue(v["blocked"])
        out = t.apply_to_application(v["id"], item["id"])
        self.assertTrue(out["blocked"])
        res = q.send(item["id"])
        self.assertFalse(res["ok"])
        self.assertIn("Fill in the missing info", res["error"])

    def test_variant_cap_and_delete(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": JOB}))
        t.delete(v["id"])
        with self.assertRaises(LookupError):
            t.get(v["id"])
        with self.assertRaises(LookupError):
            t.delete(v["id"])

    def test_clean_job_validation(self):
        with self.assertRaises(T.TailorError):
            T.clean_job({"company": "X"})
        with self.assertRaises(T.TailorError):
            T.clean_job("text")
        self.assertEqual(T.clean_job({"title": "T", "channel": "carrier-pigeon"})["channel"], "")


class AnswerBankTests(Base):
    def test_starters_are_seeded_once_and_empty_where_the_owner_decides(self):
        b = self.bank()
        rows = b.list()
        labels = {e["label"] for e in rows}
        for want in ("Salary expectation", "Notice period", "Work authorization", "Relocation", "GitHub", "Why this company", "Demographic questions"):
            self.assertIn(want, labels)
        self.assertEqual(len(self.bank().list()), len(rows))
        self.assertEqual(next(e for e in rows if e["label"] == "Salary expectation")["answer"], "")
        demo = next(e for e in rows if e["category"] == "demographic")
        self.assertEqual((demo["answer"], demo["owner_only"]), ("Prefer not to say", True))

    def test_match_confidence_auto_suggest_none(self):
        b = self.bank()
        sal = next(e for e in b.list() if e["label"] == "Salary expectation")
        b.update(sal["id"], {"answer": "Around $95k, open to discussion"})
        auto = b.match("What are your salary expectations?")
        self.assertEqual((auto["status"], auto["entry_id"]), ("auto", sal["id"]))
        self.assertGreaterEqual(auto["confidence"], 75)
        self.assertEqual(auto["answer"], "Around $95k, open to discussion")
        fuzzy = b.match("Desired annual compensation (USD)")
        self.assertIn(fuzzy["status"], ("auto", "suggest"))
        self.assertEqual(fuzzy["entry_id"], sal["id"])
        none = b.match("Which pizza topping do you prefer?")
        self.assertEqual((none["status"], none["answer"]), ("none", ""))
        self.assertEqual(b.match("")["status"], "none")

    def test_low_confidence_is_never_auto(self):
        b = self.bank()
        sal = next(e for e in b.list() if e["label"] == "Salary expectation")
        b.update(sal["id"], {"answer": "Around $95k"})
        r = b.match("Tell us about your salary and benefits wishes, bonus structure, equity and commute")
        self.assertNotEqual(r["status"], "auto")
        self.config.answerbank_min_confidence = 100
        r = b.match("Expected salary")
        self.assertEqual(r["status"], "auto" if r["confidence"] >= 100 else "suggest")
        self.config.answerbank_min_confidence = 75

    def test_threshold_setting_changes_the_outcome(self):
        b = self.bank()
        n = next(e for e in b.list() if e["label"] == "Notice period")
        b.update(n["id"], {"answer": "Two weeks"})
        q = "Please state your notice and any garden leave you would need to serve"
        r1 = b.match(q)
        self.config.answerbank_min_confidence = 30
        r2 = b.match(q)
        self.config.answerbank_min_confidence = 75
        self.assertEqual(r1["confidence"], r2["confidence"])
        if 30 <= r1["confidence"] < 75:
            self.assertEqual((r1["status"], r2["status"]), ("suggest", "auto"))

    def test_demographic_and_other_owner_only_questions_are_never_filled(self):
        b = self.bank()
        for q in ("What is your gender?", "Veteran status", "Do you have a disability?", "Please state your ethnicity", "Date of birth", "Passport number",
                  "Have you ever been convicted of a felony?", "Sexual orientation (optional)"):
            r = b.match(q)
            self.assertEqual(r["status"], "owner_only", q)
            self.assertEqual(r["confidence"], 0)
        self.assertEqual(b.match("Gender")["answer"], "Prefer not to say")     # a suggestion the owner sees, never auto
        # even if the owner approves and edits the demographic entry, it stays owner-only
        demo = next(e for e in b.list() if e["category"] == "demographic")
        out = b.update(demo["id"], {"owner_only": False, "answer": "Prefer not to answer"})
        self.assertTrue(out["owner_only"])
        self.assertEqual(b.match("Gender")["status"], "owner_only")
        self.assertNotIn("Gender", b.formfill_answers())
        self.assertNotIn("Demographic questions", b.formfill_answers())

    def test_rating_questions_are_not_answered(self):
        self.assertEqual(self.bank().match("On a scale of 1 to 5 rate your Python")["status"], "none")

    def test_slots_fill_from_facts_and_blanks_block_auto(self):
        b = self.bank()
        gh = b.match("GitHub profile URL")
        self.assertEqual((gh["status"], gh["answer"]), ("auto", "https://github.com/jo-rivera"))
        self.assertTrue(gh["fact_ids"])
        li = b.match("LinkedIn profile")                          # no LinkedIn link in the profile
        self.assertNotEqual(li["status"], "auto")
        self.assertIn("link_linkedin", li["fills"])
        self.assertIn("[FILL: link_linkedin]", li["answer"])
        why = b.match("Why do you want to work here?", {"title": "Senior Python Backend Engineer", "company": "Acme", "description": "Python and FastAPI APIs"})
        self.assertNotEqual(why["status"], "auto")                # {reason} is the owner's to write
        self.assertIn("reason", why["fills"])
        self.assertIn("Acme", why["answer"])
        self.assertIn("FastAPI", why["answer"])                   # proof slot: the owner's own bullet

    def test_ambiguous_questions_are_capped(self):
        b = self.bank()
        sal = next(e for e in b.list() if e["label"] == "Salary expectation")
        rate = next(e for e in b.list() if e["label"] == "Freelance rate")
        b.update(sal["id"], {"answer": "$95k a year"})
        b.update(rate["id"], {"answer": "$60/hour"})
        b.add({"label": "Pay wish", "questions": ["expected pay"], "answer": "$100k"})
        r = b.match("Expected rate")
        self.assertNotEqual(r["status"], "auto")

    def test_owner_added_entries_and_validation(self):
        b = self.bank()
        e = b.add({"label": "Driving licence", "questions": ["Do you hold a full driving licence?", "driving license"], "answer": "Yes, category B", "category": "custom"})
        self.assertEqual(b.match("Do you have a driving license?")["entry_id"], e["id"])
        with self.assertRaises(ValueError):
            b.add({"label": "", "questions": []})
        with self.assertRaises(LookupError):
            b.update("ab-nope", {})
        b.delete(e["id"])
        self.assertEqual(b.match("Do you have a driving license?")["status"], "none")
        n = b.reset_starters()
        self.assertEqual(n, 0)
        b.delete(next(x for x in b.list() if x["label"] == "Relocation")["id"])
        self.assertEqual(b.reset_starters(), 1)

    def test_embeddings_blend_when_available_and_fall_back_when_down(self):
        vecs = {}

        def embed(texts):
            return [vecs.get(t, [1.0, 0.0]) if "paycheck" not in t else [0.0, 1.0] for t in texts]

        def vec_for(t):
            return [0.0, 1.0] if t in ("Salary expectation", "Expected salary") or "paycheck" in t else [1.0, 0.0]
        b = self.bank(embed_fn=lambda texts: [vec_for(t) for t in texts])
        sal = next(e for e in b.list() if e["label"] == "Salary expectation")
        b.update(sal["id"], {"answer": "$95k"})
        words_only = self.bank().match("What would you like in your paycheck?")
        blended = b.match("What would you like in your paycheck?")
        self.assertGreater(blended["confidence"], words_only["confidence"])
        self.assertEqual(blended["entry_id"], sal["id"])

        def down(texts):
            raise RuntimeError("ollama is down")
        b2 = self.bank(embed_fn=down)
        self.assertEqual(b2.match("Expected salary")["status"], "auto")

    def test_pending_answers_are_never_used(self):
        b = self.bank()
        e = b.add({"label": "Visa", "questions": ["visa status"], "answer": "Citizen"}, approved=False, source="ai")
        r = b.match("What is your visa status?")
        self.assertEqual(r["entry_id"], e["id"])
        self.assertEqual(r["status"], "suggest")
        self.assertNotIn("visa status", b.formfill_answers())
        self.assertEqual(len(b.pending()), 1)


class AnswerDraftTests(Base):
    def setUp(self):
        super().setUp()
        self.fb_ = self.fb()
        self.bullet = next(f for f in self.fb_.of_type("bullet") if "FastAPI" in f["text"])
        self.years = self.fb_.get("calc-years")

    def draft(self, reply, q="How many years of backend experience do you have?"):
        self.model = FakeModel(reply)
        return asyncio.run(self.bank().draft_unknown(q, {"title": "Backend Engineer", "company": "Acme", "description": "APIs"}))

    def test_grounded_draft_is_pending_until_approved_then_saved_to_the_bank(self):
        b = self.bank()
        self.model = FakeModel({"can_answer": True, "answer": "I have 10 years of experience.", "fact_ids": [self.years["id"]]})
        out = asyncio.run(b.draft_unknown("How many years of backend experience do you have?"))
        self.assertEqual((out["status"], out["drafted"]), ("needs_ok", True))
        self.assertEqual(out["entry"]["state"], "pending")
        self.assertEqual(self.bank().match("How many years of backend experience do you have?")["status"], "suggest")
        approved = b.approve(out["entry"]["id"])
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["source"], "ai_approved")
        again = self.bank().match("How many years of backend experience do you have?")
        self.assertEqual((again["status"], again["answer"]), ("auto", "I have 10 years of experience."))

    def test_prompt_is_grounded_and_leaks_nothing_else(self):
        self.draft({"can_answer": False, "answer": "", "fact_ids": []})
        call = self.model.calls[0]
        self.assertIn("ONLY the facts", call["system"])
        self.assertIn("Never invent", call["system"])
        self.assertNotIn("PRIVATE RESUME TEXT", call["user"])
        self.assertNotIn("jo@example.test", call["user"])
        self.assertIn(self.years["id"], call["user"])

    def test_invented_answer_is_dropped_not_saved(self):
        out = self.draft({"can_answer": True, "answer": "I have 25 years of experience leading Kubernetes teams.", "fact_ids": [self.years["id"]]})
        self.assertEqual((out["status"], out["drafted"]), ("needs_you", False))
        self.assertTrue(out["unbacked"])
        self.assertEqual(self.bank().pending(), [])

    def test_cannot_answer_and_no_model_and_bad_json(self):
        self.assertEqual(self.draft({"can_answer": False, "answer": "", "fact_ids": []})["status"], "needs_you")
        self.model = None
        self.assertIn("No AI model", asyncio.run(self.bank().draft_unknown("Describe your favourite project?"))["reason"])
        self.model = FakeModel("nope", "nope")
        self.assertEqual(asyncio.run(self.bank().draft_unknown("Describe your favourite project?"))["status"], "needs_you")

    def test_owner_only_questions_never_reach_the_model(self):
        self.model = FakeModel({"can_answer": True, "answer": "Female", "fact_ids": []})
        out = asyncio.run(self.bank().draft_unknown("What is your gender?"))
        self.assertEqual(out["status"], "owner_only")
        self.assertEqual(self.model.calls, [])

    def test_known_answer_is_not_redrafted(self):
        b = self.bank()
        n = next(e for e in b.list() if e["label"] == "Notice period")
        b.update(n["id"], {"answer": "2 weeks"})
        self.model = FakeModel()
        out = asyncio.run(b.draft_unknown("What is your notice period?"))
        self.assertFalse(out["drafted"])
        self.assertEqual(self.model.calls, [])

    def test_editing_an_ai_draft_makes_it_the_owners_own(self):
        out = self.draft({"can_answer": True, "answer": "I have 10 years of experience.", "fact_ids": [self.years["id"]]})
        done = self.bank().approve(out["entry"]["id"], "More than ten years, mostly backend.")
        self.assertEqual((done["source"], done["answer"]), ("owner", "More than ten years, mostly backend."))
        with self.assertRaises(ValueError):
            self.bank().approve(self.bank().add({"label": "x", "questions": ["x"], "answer": ""}, approved=False)["id"])


class IntegrationTests(Base):
    def test_formfill_answers_exclude_owner_only_slots_and_pending(self):
        b = self.bank()
        n = next(e for e in b.list() if e["label"] == "Notice period")
        b.update(n["id"], {"answer": "Two weeks"})
        out = b.formfill_answers()
        self.assertEqual(out["Notice period"], "Two weeks")
        self.assertEqual(out["GitHub profile"], "https://github.com/jo-rivera")
        self.assertNotIn("LinkedIn profile", out)                  # unfilled slot
        self.assertFalse(any("company" in v for v in out.values()))
        self.assertFalse(any(k.lower().startswith("gender") for k in out))

    def test_formfill_plans_a_field_with_a_bank_answer(self):
        html = '<form><label for="n">What is your notice period?</label><input id="n" name="n" type="text"></form>'
        page = formfill.parse_page(html)
        facts = formfill.build_facts(self.profile, None, this_year=2026)
        facts["_answers"]["value"] = {**facts["_answers"]["value"], "Notice period": "Two weeks"}
        # the profile's standard answer already wins, so use a different question the profile does not hold
        b = self.bank()
        e = b.add({"label": "Driving licence", "questions": ["Do you hold a full driving licence"], "answer": "Yes"})
        html = '<form><label for="n">Do you hold a full driving licence</label><input id="n" name="n" type="text"></form>'
        page = formfill.parse_page(html)
        facts = formfill.build_facts(self.profile, None, this_year=2026)
        before = formfill.plan_form([f for f in page["fields"] if f["visible"]], facts)
        self.assertEqual(before[0]["status"], "skipped")
        facts["_answers"]["value"] = {**facts["_answers"]["value"], **b.formfill_answers()}
        after = formfill.plan_form([f for f in page["fields"] if f["visible"]], facts)
        self.assertEqual((after[0]["status"], after[0]["value"]), ("fill", "Yes"))

    def test_applyq_draft_gets_bank_answers_for_topics_the_posting_mentions(self):
        b = self.bank()
        n = next(e for e in b.list() if e["label"] == "Notice period")
        b.update(n["id"], {"answer": "Two weeks"})
        r = next(e for e in b.list() if e["label"] == "Relocation")
        b.update(r["id"], {"answer": "Open to relocating"})
        q = self.queue()
        item, _ = q.add({"title": "Python dev", "company": "Acme", "url": "https://jobs.example.test/x", "description": "Python. Please state your notice period, relocation and portfolio."})
        answers = item["draft"]["answers"]
        self.assertEqual(answers.get("Notice period"), "2 weeks")        # the profile's own standard answer is never overridden
        self.assertEqual(answers.get("GitHub"), "https://github.com/jo-rivera")
        self.assertEqual(answers.get("Relocation"), "Open to relocating")
        self.assertNotIn("Why this company", answers)
        self.assertEqual(item["state"], "ready")

    def test_applyq_without_the_bank_still_works(self):
        self.config.answerbank_enabled = False
        q = self.queue()
        item, _ = q.add({"title": "Python dev", "url": "https://jobs.example.test/y", "description": "notice period"})
        self.assertEqual(item["state"], "ready")
        self.config.answerbank_enabled = True

    def test_wiring(self):
        from hub import server, settings
        self.assertIn("tailor", server.FEATURE_MODULES)
        self.assertIn("answerbank", server.FEATURE_MODULES)
        for key in ("tailor_enabled", "tailor_ai", "answerbank_enabled", "answerbank_min_confidence", "answerbank_embeddings"):
            self.assertIn(key, settings.ALLOWED_KEYS)
        names, rx = TOOL_GROUPS["tailor"]
        self.assertEqual(names, {"tailor_preview", "answerbank_list", "answerbank_add"})
        self.assertTrue(rx.search("tailor my resume for this job"))
        self.assertTrue(rx.search("what's in my answer bank"))

    def test_tools_flags_and_calls(self):
        tools = {t.spec["name"]: t for t in T.build(lambda: self.app, self.config)}
        self.assertEqual(set(tools), {"tailor_preview", "answerbank_list", "answerbank_add"})
        self.assertTrue(tools["answerbank_add"].needs_approval({}))
        self.assertFalse(tools["tailor_preview"].needs_approval({}))
        self.assertTrue(tools["tailor_preview"].untrusted({}))
        self.assertFalse(tools["answerbank_list"].untrusted({}))

    def setUpApp(self):
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(T.make_router(open_ctx, clock=self.clock, profile_fn=lambda c: self.profile, applyq_fn=lambda: self.queue()))
        app.include_router(answerbank.make_router(open_ctx, clock=self.clock, profile_fn=lambda c: self.profile))
        self.app = app

    def run_tool(self, name, **args):
        tools = {t.spec["name"]: t for t in T.build(lambda: self.app, self.config)}
        return asyncio.run(tools[name].run(args))

    def test_tool_round_trips(self):
        self.setUpApp()
        self.config.session_generation = 0
        ok, text = self.run_tool("tailor_preview", title=JOB["title"], company="Acme", description=JOB["description"])
        self.assertTrue(ok, text)
        self.assertIn("variant tv-", text)
        self.assertIn("Nothing was sent", text)
        self.assertIn("Kubernetes", text)                           # listed as a missing must-have
        ok, text = self.run_tool("tailor_preview")
        self.assertFalse(ok)
        ok, text = self.run_tool("answerbank_add", question="Do you hold a clean driving licence?", answer="Yes, category B")
        self.assertTrue(ok, text)
        ok, text = self.run_tool("answerbank_list", query="driving")
        self.assertTrue(ok)
        self.assertIn("Yes, category B", text)
        ok, text = self.run_tool("answerbank_list")
        self.assertIn("always answered by the owner", text)
        ok, text = self.run_tool("answerbank_add", question="x")
        self.assertFalse(ok)


class RouteTests(Base):
    def test_auth_required(self):
        app = FastAPI()
        app.include_router(T.make_router(self.ctx))
        app.include_router(answerbank.make_router(self.ctx))
        c = TestClient(app)
        for path in ("/api/tailor/facts", "/api/tailor/prefs", "/api/tailor/variants", "/api/answerbank", "/api/answerbank/pending"):
            self.assertEqual(c.get(path).status_code, 401, path)

    def test_facts_and_prefs_routes(self):
        c = self.client()
        d = c.get("/api/tailor/facts").json()
        self.assertEqual(d["years_experience"], 10)
        e = c.post("/api/tailor/facts", json={"text": "Spoke at a Python meetup", "tags": ["talk"]}).json()
        self.assertEqual(c.put(f"/api/tailor/facts/{e['id']}", json={"text": "Spoke at a regional Python meetup"}).json()["text"], "Spoke at a regional Python meetup")
        self.assertEqual(c.post("/api/tailor/facts", json={"text": "x"}).status_code, 400)
        self.assertEqual(c.delete(f"/api/tailor/facts/{e['id']}").json()["result"], "deleted")
        self.assertEqual(c.put(f"/api/tailor/facts/{e['id']}", json={"text": "gone"}).status_code, 404)
        r = c.put("/api/tailor/prefs", json={"rate": "$55/hour", "tone": "warm", "aliases": {"k8s": "kubernetes"}}).json()
        self.assertEqual((r["prefs"]["rate"], r["aliases"]), ("$55/hour", {"k8s": "kubernetes"}))
        self.assertEqual(c.put("/api/tailor/prefs", json={"remote_ok": "yes"}).status_code, 400)
        self.assertIn("warm", c.get("/api/tailor/prefs").json()["tones"])

    def test_preview_resolve_export_apply_routes(self):
        c = self.client()
        q = self.queue()
        item, _ = q.add({**JOB, "url": "https://jobs.example.test/route"})
        v = c.post("/api/tailor/preview", json={"application_id": item["id"]}).json()
        self.assertFalse(v["blocked"])
        self.assertEqual(c.get(f"/api/tailor/variants?application_id={item['id']}").json()["variants"][0]["id"], v["id"])
        self.assertIn("unified", c.get(f"/api/tailor/variants/{v['id']}/diff").json())
        for fmt in ("md", "txt", "docx", "pdf"):
            r = c.get(f"/api/tailor/variants/{v['id']}/export", params={"format": fmt, "part": "resume"})
            self.assertEqual(r.status_code, 200, fmt)
            self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(c.get(f"/api/tailor/variants/{v['id']}/export", params={"format": "md", "part": "nope"}).status_code, 400)
        self.assertEqual(c.get(f"/api/tailor/variants/{v['id']}/export", params={"format": "exe"}).status_code, 400)
        a = c.post(f"/api/tailor/variants/{v['id']}/apply", json={"application_id": item["id"]})
        self.assertEqual(a.status_code, 200)
        self.assertEqual(c.post(f"/api/tailor/variants/{v['id']}/apply", json={}).status_code, 400)
        self.assertEqual(c.post(f"/api/tailor/variants/{v['id']}/resolve", json={"action": "bogus"}).status_code, 400)
        self.assertEqual(c.get("/api/tailor/variants/tv-nope").status_code, 404)
        self.assertEqual(c.delete(f"/api/tailor/variants/{v['id']}").status_code, 200)
        self.assertEqual(c.post("/api/tailor/preview", json={"application_id": "nope"}).status_code, 404)
        self.assertEqual(c.post("/api/tailor/preview", json={"job": {"company": "x"}}).status_code, 400)

    def test_fitcheck_and_check_routes(self):
        c = self.client()
        fit = c.post("/api/tailor/fitcheck", json={"job": JOB}).json()
        self.assertIn("requirements", fit)
        res = c.post("/api/tailor/check", json={"job": JOB, "sentences": [{"text": "I led a team of 12 engineers.", "fact_ids": [], "kind": "claim"}]}).json()
        self.assertFalse(res["ok"])

    def test_switched_off(self):
        self.config.tailor_enabled = False
        self.config.answerbank_enabled = False
        c = self.client()
        self.assertEqual(c.get("/api/tailor/facts").status_code, 404)
        self.assertEqual(c.get("/api/answerbank").status_code, 404)
        self.config.tailor_enabled = True
        self.config.answerbank_enabled = True

    def test_answerbank_routes(self):
        c = self.client()
        d = c.get("/api/answerbank").json()
        self.assertGreater(len(d["entries"]), 5)
        self.assertEqual(d["min_confidence"], 75)
        sal = next(e for e in d["entries"] if e["label"] == "Salary expectation")
        self.assertEqual(c.put(f"/api/answerbank/{sal['id']}", json={"answer": "$95k"}).status_code, 200)
        m = c.post("/api/answerbank/match", json={"question": "Expected salary?"}).json()
        self.assertEqual((m["status"], m["answer"]), ("auto", "$95k"))
        mf = c.post("/api/answerbank/match-form", json={"questions": ["Expected salary?", "Gender", "Favourite colour"]}).json()["results"]
        self.assertEqual([r["status"] for r in mf], ["auto", "owner_only", "none"])
        used = c.post("/api/answerbank/match", json={"question": "Expected salary?", "use": True}).json()
        self.assertEqual(used["status"], "auto")
        self.assertEqual(next(e for e in c.get("/api/answerbank").json()["entries"] if e["id"] == sal["id"])["uses"], 1)
        self.assertEqual(c.post("/api/answerbank", json={"label": ""}).status_code, 400)
        self.assertEqual(c.post("/api/answerbank/ab-none/approve", json={}).status_code, 404)
        self.assertEqual(c.delete("/api/answerbank/ab-none").status_code, 404)
        self.assertEqual(c.post("/api/answerbank/reset-starters").json()["added"], 0)
        self.assertEqual(c.get("/api/answerbank?q=notice").json()["entries"][0]["label"], "Notice period")

    def test_draft_and_approve_routes(self):
        c = self.client()
        years = self.fb().get("calc-years")
        self.model = FakeModel({"can_answer": True, "answer": "I have 10 years of experience.", "fact_ids": [years["id"]]})
        d = c.post("/api/answerbank/draft", json={"question": "How many years of backend experience do you have?"}).json()
        self.assertEqual(d["status"], "needs_ok")
        self.assertEqual(len(c.get("/api/answerbank/pending").json()["entries"]), 1)
        self.assertEqual(c.get("/api/answerbank").json()["pending"], 1)
        r = c.post(f"/api/answerbank/{d['entry']['id']}/approve", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/answerbank/pending").json()["entries"], [])


class NeverSendsTests(Base):
    def test_modules_never_import_the_mailer_or_send(self):
        for mod in ("tailor", "answerbank", "fitcheck", "factbase"):
            src = Path(__file__).resolve().parents[1].joinpath("hub", f"{mod}.py").read_text(encoding="utf-8")
            self.assertNotIn("send_mail", src, mod)
            self.assertNotIn("gmailx", src, mod)
            self.assertNotIn("smtplib", src, mod)

    def test_whole_flow_sends_nothing(self):
        t = self.tailor()
        v = asyncio.run(t.preview({"job": JOB}))
        t.export(v["id"], "pdf", "resume")
        q = self.queue()
        item, _ = q.add({**JOB, "url": "https://jobs.example.test/never"})
        t.apply_to_application(v["id"], item["id"])
        self.assertEqual(q.get(item["id"])["state"], "ready")
        self.assertIsNone(q.get(item["id"])["sent_at"])


class OddTagsTest(Base):
    def test_tags_that_are_not_a_list_of_text_are_ignored_not_a_500(self):
        c = self.client()
        for tags in (5, 1.5, True, {"a": 1}, "abc", [1, None, {"a": 1}]):
            with self.subTest(tags=tags):
                r = c.post("/api/tailor/facts", json={"text": f"Mentored two interns in {abs(hash(str(tags))) % 1000}", "tags": tags})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["tags"], [])
                r = c.put(f"/api/tailor/facts/{r.json()['id']}", json={"tags": tags})
                self.assertLess(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()
