"""Tests for form pre-fill (hub/formfill.py). The mapper is tested as pure functions on synthetic HTML. The flow runs against a
FAKE browser driver (and a fake Playwright object model for the real driver's glue). One integration test fills a real
page served from 127.0.0.1 when Playwright and a Chromium/Chrome are installed; it skips otherwise."""
import asyncio
import json
import re
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import applyq, applyq_tools, formfill as ff, safety
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_applyq import JOB, PROFILE
from tests.test_autopilot import need_auth
from tests.test_safety import Base as SafetyBase
from tests.fakes_assistant import reply, tool_block
from hub import assistant as A

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"
PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a public address for any name

FORM = """
<html><body><form action="/apply">
<h2>Apply for Python API developer</h2>
<label for="n">Full name *</label><input id="n" name="name" required>
<label for="e">Email</label><input id="e" type="email" name="email" required>
<label>Phone <input name="phone"></label>
<input type="text" name="li" placeholder="LinkedIn profile">
<label for="gh">GitHub</label><input id="gh" name="gh">
<label for="notice">What is your notice period?</label><input id="notice" name="q1">
<label for="rate">Expected rate</label><input id="rate" name="q2">
<label for="cv">Resume/CV</label><input id="cv" type="file" name="cv">
<label for="why">Why do you want to work here?</label><textarea id="why" name="why"></textarea>
<label for="dob">Date of birth</label><input id="dob" type="date" name="dob">
<label for="g">Gender</label><select id="g" name="g"><option value="">Choose</option><option>Male</option><option>Female</option></select>
<label for="sal">Current salary</label><input id="sal" name="sal">
<label for="ssn">SSN</label><input id="ssn" name="ssn">
<input type="checkbox" id="terms" required><label for="terms">I agree to the terms and certify this is true</label>
<fieldset><legend>Are you willing to relocate?</legend>
<label><input type="radio" name="reloc" value="y"> Yes</label><label><input type="radio" name="reloc" value="n"> No</label></fieldset>
<input type="text" name="trap" style="display:none">
<label for="fc">Favourite colour</label><input id="fc" name="fc">
<label for="pet">Pet name *</label><input id="pet" name="pet" required>
<button type="submit">Submit application</button>
</form></body></html>
"""
THANKS = "<html><body><h1>Thank you for applying!</h1><p>Your application has been received.</p></body></html>"
SAME_FORM_ERROR = FORM.replace("<h2>", "<p>Please fix the errors below</p><h2>")

STD = {"Notice period": "2 weeks", "Expected rate": "$50/h", "Email": "me@example.test", "Phone": "+1 555 0100",
       "Willing to relocate": "No"}
LINKS = {"LinkedIn": "https://www.linkedin.com/in/test", "GitHub": "https://github.com/test", "Portfolio": "https://example.test/me"}


def facts_for(profile=None, draft=None, resume=None):
    p = dict(PROFILE)
    p.update(standard_answers=dict(STD), links=dict(LINKS))
    p.update(profile or {})
    d = draft if draft is not None else {"body": "Hello Acme, I build APIs.", "cover_note": "I like Acme because of its APIs.", "answers": {}}
    return ff.build_facts(p, d, this_year=2026, resume=resume)


def plan(html, **kw):
    page = ff.parse_page(html)
    fields = [f for f in page["fields"] if f["visible"] or f["type"] == "radio"]
    return {p["label"]: p for p in ff.plan_form(fields, facts_for(**kw))}


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.p = plan(FORM, resume={"value": "/x/resume.pdf", "name": "resume.pdf", "source": "file"})

    def status(self, label):
        return self.p[label]["status"]

    def test_mapping_table(self):
        expect = {
            "Full name *": ("fill", "full_name", "Test Person"), "Email": ("fill", "email", "me@example.test"),
            "Phone": ("fill", "phone", "+1 555 0100"), "LinkedIn profile": ("fill", "linkedin", "https://www.linkedin.com/in/test"),
            "GitHub": ("fill", "github", "https://github.com/test"), "What is your notice period?": ("fill", "standard_answer", "2 weeks"),
            "Expected rate": ("fill", "standard_answer", "$50/h"), "Resume/CV": ("fill", "resume", "/x/resume.pdf"),
            "Why do you want to work here?": ("fill", "why_us", "I like Acme because of its APIs."),
        }
        for label, (status, key, value) in expect.items():
            got = self.p[label]
            self.assertEqual((got["status"], got["key"], got["value"]), (status, key, value), label)

    def test_sensitive_and_unknown_are_never_filled(self):
        for label in ("Date of birth", "Gender", "Current salary", "SSN", "I agree to the terms and certify this is true"):
            self.assertEqual(self.status(label), "needs_you", label)
            self.assertEqual(self.p[label]["value"], "", label)
        self.assertEqual(self.status("Pet name *"), "needs_you")               # unknown AND required
        self.assertEqual(self.status("Favourite colour"), "skipped")           # unknown, optional
        self.assertNotIn("(unlabelled field)", self.p)
        self.assertEqual(len(self.p), 17)
        self.assertNotIn("trap", " ".join(self.p))                              # the hidden honeypot is not even listed

    def test_radio_group_uses_the_legend_and_picks_the_option(self):
        r = self.p["Are you willing to relocate?"]
        self.assertEqual((r["status"], r["action"], r["display"]), ("fill", "choose", "No"))
        page = ff.parse_page(FORM)
        radio = next(f for f in page["fields"] if f["type"] == "radio")
        self.assertEqual([o["label"] for o in radio["options"]], ["Yes", "No"])
        self.assertEqual(r["target"], radio["options"][1]["idx"])

    def test_missing_profile_facts_are_needs_you_not_invented(self):
        page = ff.parse_page("<label>Location <input name=l></label><label>Current company <input name=c></label>")
        got = {p["label"]: p for p in ff.plan_form(page["fields"], facts_for())}
        self.assertEqual(got["Location"]["status"], "needs_you")
        self.assertEqual(got["Current company"]["status"], "needs_you")
        self.assertIn("Add it to your profile", got["Location"]["reason"])

    def test_fill_markers_block_draft_text(self):
        got = plan(FORM, draft={"body": "Rate: [FILL: expected rate]", "cover_note": "Hi", "answers": {}})
        w = got["Why do you want to work here?"]
        self.assertEqual(w["status"], "needs_you")
        self.assertIn("[FILL: expected rate]", w["reason"])
        self.assertEqual(w["value"], "")

    def test_ambiguous_label_is_not_guessed(self):
        page = ff.parse_page("<label>Phone or email <input name=x></label>")
        p = ff.plan_form(page["fields"], facts_for())[0]
        self.assertEqual(p["status"], "needs_you")
        self.assertIn("more than one", p["reason"])

    def test_standard_answers_fuzzy_ambiguous_and_scale_guard(self):
        page = ff.parse_page("<label>Rate your Python skills (1-5) <input name=a></label>")
        self.assertEqual(ff.plan_form(page["fields"], facts_for())[0]["status"], "skipped")      # not the rate answer
        page = ff.parse_page("<label>Salary expectation <input name=a></label>")
        self.assertEqual(ff.plan_form(page["fields"], facts_for())[0]["value"], "$50/h")
        two = {"standard_answers": {"Notice period": "2 weeks", "Availability to start": "1 month"}}
        page = ff.parse_page("<label>When are you available to start? <input name=a></label>")
        self.assertEqual(ff.plan_form(page["fields"], facts_for(profile=two))[0]["status"], "needs_you")

    def test_select_needs_a_matching_option_and_value_checks(self):
        html = ("<label>Are you willing to relocate? <select name=a><option value=''>-</option><option>Yes</option><option>No</option></select></label>"
                "<label>Notice period <select name=b><option>Immediately</option><option>1 month</option></select></label>"
                "<label>Email <input type=email name=e></label><label>Years of experience <input type=number name=y></label>"
                "<label>Phone <input name=p maxlength=5></label>")
        page = ff.parse_page(html)
        facts = facts_for(profile={"standard_answers": {**STD, "Email": "not-an-email"}})
        got = {p["label"]: p for p in ff.plan_form(page["fields"], facts)}
        self.assertEqual((got["Are you willing to relocate?"]["status"], got["Are you willing to relocate?"]["value"]), ("fill", "No"))
        self.assertEqual(got["Notice period"]["status"], "needs_you")           # "2 weeks" is not an option
        self.assertEqual(got["Email"]["status"], "needs_you")
        self.assertEqual(got["Phone"]["status"], "needs_you")                   # longer than maxlength: never truncated
        self.assertEqual(got["Years of experience"]["status"], "needs_you")     # no dates, so nothing derived

    def test_label_sources(self):
        html = ('<label for=a>For label</label><input id=a name=x1>'
                '<label>Wrapped label <input name=x2></label>'
                '<input aria-label="Aria label" name=x3>'
                '<span id=lb>Labelled by</span><input aria-labelledby=lb name=x4>'
                '<div>Nearby text</div><div><input name=x5></div>'
                '<input placeholder="Placeholder only" name=x6>'
                '<input name=firstName>')
        labels = [f["label"] for f in ff.parse_page(html)["fields"]]
        self.assertEqual(labels, ["For label", "Wrapped label", "Aria label", "Labelled by", "Nearby text", "Placeholder only", "first Name"])

    def test_required_detection_and_locked_fields(self):
        page = ff.parse_page('<label>Name* <input name=n></label><input aria-required=true name=m aria-label="Phone"><input name=l aria-label="Email" disabled>')
        f = page["fields"]
        self.assertTrue(f[0]["required"] and f[1]["required"])
        self.assertEqual(ff.plan_form(f, facts_for())[2]["status"], "skipped")

    def test_derived_years_and_current_job_only_when_stated(self):
        exp = [{"title": "Dev", "company": "Old", "period": "2018 - 2020"}, {"title": "Lead", "company": "New", "period": "2021 - Present"}]
        facts = facts_for(profile={"experience": exp})
        self.assertEqual(facts["years_experience"]["value"], "8")
        self.assertEqual((facts["current_title"]["value"], facts["current_company"]["value"]), ("Lead", "New"))
        facts = facts_for(profile={"experience": [{"title": "Dev", "company": "Old", "period": "2018 - 2020"}]})
        self.assertNotIn("current_company", facts)                               # a past job is not "current"

    def test_first_last_name_only_when_unambiguous(self):
        self.assertEqual(facts_for()["first_name"]["value"], "Test")
        self.assertNotIn("first_name", facts_for(profile={"name": "Mary Jane Smith"}))

    def test_password_field_and_no_page_are_never_filled(self):
        page = ff.parse_page('<label>Password <input type=password name=p></label>')
        self.assertEqual(ff.plan_form(page["fields"], facts_for())[0]["status"], "needs_you")

    def test_garbage_html_does_not_raise(self):
        for html in ("", "<<<>>", "<input" * 50, "<div>" * 500 + "<input name=a>", "<label for=>x</label><input id=>"):
            self.assertIsInstance(ff.parse_page(html)["fields"], list)


class ControlsAndBlockersTest(unittest.TestCase):
    def controls(self, html):
        return ff.find_controls(ff.parse_page(html)["buttons"])

    def test_submit_detection(self):
        c = self.controls("<form><button type=button>Back</button><button type=submit>Submit application</button><button>Save draft</button></form>")
        self.assertEqual(c["submit"]["text"], "Submit application")
        self.assertIsNone(c["next"])
        self.assertEqual(self.controls('<form><input type=submit value="Send application"></form>')["submit"]["text"], "Send application")

    def test_wizard_next_and_ambiguous_submit(self):
        c = self.controls("<form><button>Next</button></form>")
        self.assertEqual((c["next"]["text"], c["submit"]), ("Next", None))
        c = self.controls("<form><button>Submit</button><button>Submit</button></form>")
        self.assertTrue(c["ambiguous"] and c["submit"] is None)
        self.assertIsNone(self.controls("<form><button>Sign in</button><button>Cancel</button></form>")["submit"])

    def test_hidden_or_disabled_controls_are_ignored(self):
        self.assertIsNone(self.controls('<form><button disabled>Submit</button><button style="display:none">Submit</button></form>')["submit"])

    def blockers(self, html, url="https://x.example.test/apply", status=None):
        return [b["kind"] for b in ff.detect_blockers(ff.parse_page(html), url, status)]

    def test_blockers(self):
        self.assertEqual(self.blockers(FORM), [])
        self.assertIn("login_required", self.blockers("<input type=email aria-label=Email><input type=password aria-label=Password>"))
        self.assertIn("captcha", self.blockers('<input name=a aria-label=Name><div class="g-recaptcha"></div>'))
        self.assertIn("captcha", self.blockers('<input name=a aria-label=Name><iframe src="https://challenges.cloudflare.com/x"></iframe>'))
        self.assertIn("captcha", self.blockers("<input name=a aria-label=Name><p>Please verify you are human</p>"))
        self.assertIn("blocked", self.blockers("<p>Access denied</p>", status=403))
        self.assertIn("no_form", self.blockers("<p>Hello</p>"))
        self.assertIn("login_required", self.blockers("<input name=a aria-label=Name>", url="https://x.example.test/login"))

    def test_step_info(self):
        page = ff.parse_page("<p>Step 2 of 4</p><button>Next</button><input aria-label=Name>")
        info = ff.wizard_info(page, ff.find_controls(page["buttons"]))
        self.assertEqual((info["current"], info["total"], info["has_next"]), (2, 4, True))

    def test_result_judgement(self):
        self.assertEqual(ff.judge_result("https://a.test/apply", "https://a.test/apply", "Thank you for applying!", 0), "applied")
        self.assertEqual(ff.judge_result("https://a.test/apply", "https://a.test/thank-you", "Done.", 0), "applied")
        self.assertEqual(ff.judge_result("https://a.test/apply", "https://a.test/apply", "Please fix the errors", 5), "needs_check")
        self.assertEqual(ff.judge_result("https://a.test/apply", "https://a.test/apply", "", 0), "needs_check")


class UrlGuardTest(unittest.TestCase):
    def test_public_https_only(self):
        ok = lambda u, **k: ff.url_problem(u, resolver=PUBLIC, **k)  # noqa: E731
        self.assertEqual(ok("https://jobs.example.test/apply"), "")
        for bad in ("http://jobs.example.test/x", "ftp://a.example.test/x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.5/x",
                    "https://192.168.1.2/x", "https://169.254.169.254/x", "https://[::1]/x", "https://100.64.0.1/x", "https://intranet/x",
                    "https://a.local/x", "https://user:pw@a.example.test/x", "https:///x", "javascript:alert(1)", "https://a.example.test:badport/x"):
            self.assertNotEqual(ok(bad), "", bad)
        self.assertNotEqual(ff.url_problem("https://sneaky.example.test/", resolver=lambda h: ["10.1.2.3"]), "")   # DNS to a private address
        self.assertNotEqual(ff.url_problem("https://sneaky.example.test/", resolver=lambda h: ["93.184.216.34", "127.0.0.1"]), "")

    def test_localhost_switch_is_off_by_default_and_narrow(self):
        self.assertNotEqual(ff.url_problem("http://127.0.0.1:8000/x"), "")
        self.assertEqual(ff.url_problem("http://127.0.0.1:8000/x", allow_local=True), "")
        self.assertNotEqual(ff.url_problem("http://10.0.0.1/x", allow_local=True), "")
        self.assertNotEqual(ff.url_problem("http://example.test/x", resolver=PUBLIC), "")

    def test_never_list_matches_subdomains(self):
        self.assertEqual(ff.never_match("www.linkedin.com", ["linkedin.com"]), "linkedin.com")
        self.assertEqual(ff.never_match("notlinkedin.com", ["linkedin.com"]), "")


class FakeDriver(ff.Driver):
    """A browser that is a dict. `web` maps URL -> html; clicking Submit moves to web['__after__']."""
    instances: list = []

    def __init__(self, web, profile_dir=None, **kw):
        self.web, self.vals, self.calls, self.closed = web, {}, [], False
        self.cur = None
        self.status = kw.get("status")
        self.fail_open = kw.get("fail_open")
        self.shots = 0
        FakeDriver.instances.append(self)

    def open(self, url, headed):
        if self.fail_open:
            raise self.fail_open
        self.calls.append(("open", url, headed))
        self.cur = url
        return self.status

    def url(self): return self.cur
    def html(self): return self.web[self.cur]
    def values(self): return dict(self.vals)
    def fill_text(self, idx, value): self.calls.append(("text", idx)); self.vals[idx] = value
    def select(self, idx, value): self.calls.append(("select", idx)); self.vals[idx] = value
    def choose(self, idx): self.calls.append(("choose", idx)); self.vals[idx] = "1"
    def upload(self, idx, path): self.calls.append(("upload", idx)); self.vals[idx] = Path(path).name
    def settle(self): pass
    def blocked_navigation(self): return ""
    # custom-widget methods (Workday; see tests/test_workday.py for the fake that models them). Plain pages never use them.
    def type_text(self, idx, value): self.calls.append(("type", idx)); self.vals[idx] = value
    def open_list(self, btn_idx): self.calls.append(("open_list", btn_idx)); return []
    def pick_listed(self, btn_idx, text): self.calls.append(("pick", btn_idx)); return text
    def close_list(self): self.calls.append(("close_list",))
    def search_options(self, idx, query): self.calls.append(("search", idx)); return []
    def pick_searched(self, idx, text): self.calls.append(("pick_search", idx)); return text

    def click(self, idx):
        self.calls.append(("click", idx))
        label = next((b["text"] for b in ff.parse_page(self.html())["buttons"] if b["idx"] == idx), "")
        nxt = self.web.get(("click", self.cur, norm_label(label)))
        if nxt:
            self.cur = nxt

    def screenshot(self):
        self.shots += 1
        return PNG + str(self.shots).encode()

    def close(self): self.closed = True

    def edit(self, idx, value): self.vals[idx] = value        # the owner types in the visible window


def norm_label(t):
    return ff.norm(t)


class Web(dict):
    """Any other page of the fake web is the application form."""
    def __missing__(self, key):
        if isinstance(key, str):
            return FORM
        raise KeyError(key)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class Base(unittest.TestCase):
    URL = "https://jobs.example.test/apply"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="formfill-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), formfill_enabled=True, applyq_daily_cap=3)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: self.audits.append((a, k)), notify=lambda *a, **k: None)
        self.profile = {**PROFILE, "standard_answers": dict(STD), "links": dict(LINKS), "resume_text": "My resume text, long enough."}
        self.clock = Clock()
        self.web = Web({self.URL: FORM, ("click", self.URL, "submit application"): "https://jobs.example.test/thanks",
                    "https://jobs.example.test/thanks": THANKS})
        FakeDriver.instances = []
        self.opts = {}
        self.svc = None

    def build(self, **kw):
        kw.setdefault("driver_factory", lambda profile_dir: FakeDriver(self.web, profile_dir, **self.opts))
        kw.setdefault("clock", self.clock)
        kw.setdefault("resolver", PUBLIC)
        kw.setdefault("reaper", False)
        kw.setdefault("profile_fn", lambda c: self.profile)
        self.aq = applyq.Applyq(self.ctx, profile_fn=lambda c: self.profile)
        self.svc = ff.Formfill(self.ctx, svc=self.aq, **kw)
        self.addCleanup(lambda: self.svc.pool.shutdown(wait=False))
        return self.svc

    def item(self, n=1, url=None, **over):
        item, _ = self.aq.add({**JOB, "url": url or f"https://jobs.example.test/apply{'' if n == 1 else n}", "title": f"Job {n}", "channel": "web",
                               "description": "Python role.", **over})
        return item

    def client(self, **kw):
        svc = self.svc or self.build(**kw)
        app = FastAPI()
        app.include_router(self._router(svc))
        c = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(c.close)
        return c

    def _router(self, svc):
        with mock.patch.object(ff, "Formfill", lambda ctx, **k: svc):
            return ff.make_router(self.ctx)


class FlowTest(Base):
    def go(self, **kw):
        c = self.client(**kw)
        item = self.item()
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 200, r.text)
        return c, item, r.json()

    def path(self, item, run, tail=""):
        return f"/api/applyq/{item['id']}/formfill/{run['run']}{tail}"

    def test_start_fills_lists_and_waits(self):
        c, item, run = self.go()
        self.assertEqual(run["state"], "ready")
        by = {f["label"]: f for f in run["fields"]}
        self.assertEqual(by["Email"]["value"], "me@example.test")
        self.assertEqual(by["Email"]["status"], "filled")
        self.assertEqual(by["Gender"]["status"], "needs_you")
        self.assertEqual([f["n"] for f in run["fields"]], list(range(1, len(run["fields"]) + 1)))
        self.assertEqual(run["counts"], {"filled": 10, "needs_you": 6, "skipped": 1})
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(run["submit_label"], "Submit application")
        drv = FakeDriver.instances[0]
        self.assertFalse([c for c in drv.calls if c[0] == "click"])             # filling never clicks
        self.assertTrue(drv.calls[0][2])                                         # headed by default
        self.assertEqual(self.aq.get(item["id"])["state"], "ready")
        shot = c.get(run["screenshot_url"])
        self.assertEqual((shot.status_code, shot.headers["content-type"], shot.headers["cache-control"]), (200, "image/png", "no-store"))
        self.assertTrue(shot.content.startswith(b"\x89PNG"))

    def test_uploads_resume_text_and_cleans_it_up(self):
        c, item, run = self.go()
        up = [x for x in FakeDriver.instances[0].calls if x[0] == "upload"]
        self.assertEqual(len(up), 1)
        self.assertTrue(any(f["label"] == "Resume/CV" and "resume.txt" in f["value"] for f in run["fields"]))
        c.post(self.path(item, run, "/cancel"))
        self.assertEqual(list((self.tmp / "data" / "formfill_tmp").glob("*")), [])

    def test_submit_happy_path_marks_applied_only_after_the_tap(self):
        c, item, run = self.go()
        body = {"confirm": True, "field_digest": run["digest"]}
        r = c.post(self.path(item, run, "/submit"), json=body)
        self.assertEqual(r.status_code, 409)                                      # the screenshot was never served
        self.assertTrue(r.json()["needs_look"])
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))
        c.get(run["screenshot_url"])
        r = c.post(self.path(item, run, "/submit"), json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["result"], "applied")
        self.assertEqual(self.aq.get(item["id"])["state"], "applied")
        self.assertEqual([x for x in FakeDriver.instances[0].calls if x[0] == "click"].__len__(), 1)
        self.assertTrue(FakeDriver.instances[0].closed)
        self.assertEqual(c.get(r.json()["screenshot_url"]).status_code, 200)       # the result screenshot is still there
        again = c.post(self.path(item, run, "/submit"), json=body)
        self.assertEqual(again.status_code, 409)                                   # no second click
        self.assertEqual(len([x for x in FakeDriver.instances[0].calls if x[0] == "click"]), 1)

    def test_submit_without_confirm_or_digest_does_nothing(self):
        c, item, run = self.go()
        c.get(run["screenshot_url"])
        for body in ({}, {"field_digest": run["digest"]}, {"confirm": "yes", "field_digest": run["digest"]}, {"confirm": False, "field_digest": run["digest"]},
                     {"confirm": True}, {"confirm": True, "field_digest": ""}):
            r = c.post(self.path(item, run, "/submit"), json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))

    def test_digest_mismatch_is_409_with_a_fresh_screenshot(self):
        c, item, run = self.go()
        c.get(run["screenshot_url"])
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": "0" * 32})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["changed"])
        fresh = c.get(self.path(item, run)).json()
        self.assertFalse(fresh["seen"])                                              # the new screenshot has not been looked at
        self.assertEqual(r.json()["digest"], fresh["digest"])
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))

    def test_owner_edit_after_the_screenshot_blocks_the_click(self):
        c, item, run = self.go()
        c.get(run["screenshot_url"])
        FakeDriver.instances[0].edit(0, "Somebody Else")                             # typed in the visible window
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["changed"])
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))
        new = r.json()["digest"]
        self.assertNotEqual(new, run["digest"])
        c.get(run["screenshot_url"])                                                 # looks at the new one
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": new})
        self.assertEqual(r.status_code, 200)

    def test_page_navigation_after_the_screenshot_blocks_the_click(self):
        c, item, run = self.go()
        c.get(run["screenshot_url"])
        FakeDriver.instances[0].cur = "https://jobs.example.test/thanks"
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 409)

    def test_stale_run_cannot_be_submitted(self):
        c, item, run = self.go()
        c.get(run["screenshot_url"])
        for _ in range(7):
            self.clock.advance(280)
            c.post(self.path(item, run, "/refresh"))                                 # keeps it from going idle
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": run["digest"]})
        self.assertIn(r.status_code, (409, 410))
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))
        self.assertTrue(FakeDriver.instances[0].closed)
        self.assertEqual(list((self.tmp / "data" / "formfill_shots").glob("*.png")), [])

    def test_idle_timeout_closes_the_browser_and_deletes_the_screenshot(self):
        c, item, run = self.go()
        self.assertEqual(len(list((self.tmp / "data" / "formfill_shots").glob("*.png"))), 1)
        self.clock.advance(299)
        self.assertEqual(c.get(self.path(item, run)).status_code, 200)
        self.clock.advance(301)
        r = c.get(self.path(item, run))
        self.assertEqual(r.status_code, 410)
        self.assertIn("idle", r.json()["detail"])
        self.assertTrue(FakeDriver.instances[0].closed)
        self.assertEqual(list((self.tmp / "data" / "formfill_shots").glob("*.png")), [])
        self.assertEqual(c.get(run["screenshot_url"]).status_code, 410)

    def test_idle_closing_works_without_any_request(self):
        c, item, run = self.go()
        self.clock.advance(400)
        asyncio.run(self.svc.call(self.svc.reap))
        self.assertTrue(FakeDriver.instances[0].closed)

    def test_screenshot_view_counts_as_activity(self):
        c, item, run = self.go()
        for _ in range(3):
            self.clock.advance(200)
            self.assertEqual(c.get(run["screenshot_url"]).status_code, 200)

    def test_one_form_at_a_time_and_rate_limit(self):
        c, item, run = self.go()
        other = self.item(2)
        r = c.post(f"/api/applyq/{other['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 409)
        c.post(self.path(item, run, "/cancel"))
        r = c.post(f"/api/applyq/{other['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 429)                                         # under 20 s after the last start
        self.assertIn("Wait", r.json()["detail"])
        self.clock.advance(21)
        self.assertEqual(c.post(f"/api/applyq/{other['id']}/formfill/start", json={}).status_code, 200)
        self.assertEqual(len(FakeDriver.instances), 2)

    def test_daily_cap_is_shared_with_applyq(self):
        c = self.client()
        for n in range(3):
            self.aq.mark(self.item(10 + n)["id"], "applied")
        item = self.item(2)
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirm"])
        self.assertEqual(FakeDriver.instances, [])
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={"confirm_over_cap": True})
        self.assertEqual(r.status_code, 200)

    def test_cancel_closes_and_forgets(self):
        c, item, run = self.go()
        self.assertEqual(c.post(self.path(item, run, "/cancel")).status_code, 200)
        self.assertTrue(FakeDriver.instances[0].closed)
        self.assertEqual(c.get(self.path(item, run)).status_code, 410)
        self.assertEqual(c.get(f"/api/applyq/{item['id']}/formfill").json(), {"run": None})

    def test_unknown_run_ids_are_404(self):
        c, item, run = self.go()
        for bad in ("nope", "0" * 16):
            self.assertEqual(c.get(f"/api/applyq/{item['id']}/formfill/{bad}").status_code, 404)

    def test_needs_check_when_the_page_does_not_say_it_worked(self):
        self.web[("click", self.URL, "submit application")] = "https://jobs.example.test/apply?error=1"
        self.web["https://jobs.example.test/apply?error=1"] = SAME_FORM_ERROR
        c, item, run = self.go()
        c.get(run["screenshot_url"])
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], "needs_check")
        saved = self.aq.get(item["id"])
        self.assertEqual(saved["state"], "ready")                                     # NOT applied
        self.assertIn("needs check", saved["flags"])
        self.assertTrue(any("did not clearly say" in n["text"] for n in saved["notes"]))

    def test_multi_step_form_reports_the_step_and_goes_next_on_confirm(self):
        step1 = '<p>Step 1 of 3</p><label>Full name <input name=n></label><label>Email <input type=email name=e></label><button type=button>Next</button>'
        step2 = '<p>Step 2 of 3</p><label>Phone <input name=p></label><button>Submit application</button>'
        self.web[self.URL] = step1
        self.web[("click", self.URL, "next")] = "https://jobs.example.test/apply/2"
        self.web["https://jobs.example.test/apply/2"] = step2
        c, item, run = self.go()
        self.assertEqual(run["step"]["label"], "step 1 of 3 filled")
        self.assertTrue(run["can_next"] and not run["can_submit"])
        self.assertEqual(c.post(self.path(item, run, "/next"), json={}).status_code, 400)       # needs confirm
        self.assertEqual(FakeDriver.instances[0].url(), self.URL)
        r = c.post(self.path(item, run, "/next"), json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        v = r.json()
        self.assertEqual((v["step"]["current"], v["step"]["total"]), (2, 3))
        self.assertEqual([f["label"] for f in v["fields"]], ["Phone"])
        self.assertTrue(v["can_submit"])

    def test_login_wall_is_reported_and_nothing_is_filled(self):
        self.web[self.URL] = '<label>Email <input type=email name=e></label><label>Password <input type=password name=p></label><button>Sign in</button>'
        c, item, run = self.go()
        self.assertIn("login_required", [b["kind"] for b in run["blockers"]])
        self.assertEqual(run["fields"], [])
        self.assertFalse(any(x[0] in ("text", "select", "choose", "upload") for x in FakeDriver.instances[0].calls))
        self.assertFalse(run["can_submit"])

    def test_captcha_blocks_submit_when_the_window_is_hidden(self):
        self.web[self.URL] = FORM.replace("<button", '<div class="g-recaptcha"></div><button')
        self.config.formfill_headed = False
        c, item, run = self.go()
        self.assertIn("captcha", [b["kind"] for b in run["blockers"]])
        self.assertFalse(run["can_submit"])
        c.get(run["screenshot_url"])
        r = c.post(self.path(item, run, "/submit"), json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 422)
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))

    def test_site_that_blocks_automation_falls_back(self):
        self.opts["status"] = 403
        self.web[self.URL] = "<h1>Access denied</h1>"
        c, item, run = self.go()
        self.assertIn("blocked", [b["kind"] for b in run["blockers"]])
        self.assertIn("apply yourself", run["blockers"][0]["message"])

    def test_browser_failure_is_a_clean_error_and_frees_the_slot(self):
        self.opts["fail_open"] = RuntimeError("boom with secret value")
        c = self.client()
        item = self.item()
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("secret", r.text)
        self.assertEqual(c.get(f"/api/applyq/{item['id']}/formfill").json(), {"run": None})

    def test_login_window_is_headed_and_exclusive(self):
        c = self.client()
        self.config.formfill_headed = False
        item = self.item()
        r = c.post(f"/api/applyq/{item['id']}/formfill/login")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["kind"], FakeDriver.instances[0].calls[0][2]), ("login", True))
        self.assertEqual(r.json()["fields"], [])
        self.assertEqual(c.post(f"/api/applyq/{item['id']}/formfill/start", json={}).status_code, 409)
        self.assertTrue(any(a[0][0] == "formfill_login" for a in self.audits))
        self.clock.advance(601)
        self.assertEqual(c.get(f"/api/applyq/{item['id']}/formfill").json(), {"run": None})
        self.assertTrue(FakeDriver.instances[0].closed)


class RefusalTest(Base):
    def start(self, item, c=None):
        c = c or self.client()
        return c.post(f"/api/applyq/{item['id']}/formfill/start", json={})

    def test_never_list_default_and_edit(self):
        c = self.client()
        li = self.item(5, url="https://www.linkedin.com/jobs/view/123")
        r = self.start(li, c)
        self.assertEqual(r.status_code, 400)
        self.assertIn("never-fill list", r.json()["detail"])
        self.assertEqual(FakeDriver.instances, [])
        self.assertEqual(c.get("/api/formfill/never").json(), {"never": ["linkedin.com"]})
        ok = self.item(6, url="https://boards.example.test/jobs/1")
        self.assertEqual(c.put("/api/formfill/never", json={"never": ["https://www.Boards.example.test/x", "linkedin.com"]}).json(),
                         {"never": ["boards.example.test", "linkedin.com"]})
        self.assertEqual(self.start(ok, c).status_code, 400)
        self.assertEqual(c.put("/api/formfill/never", json={"never": ["not a domain"]}).status_code, 400)
        self.assertEqual(c.put("/api/formfill/never", json={"never": []}).json(), {"never": []})          # the owner may empty it
        self.assertEqual(self.start(li, c).status_code, 200)

    def test_private_and_plain_http_pages_are_refused(self):
        c = self.client()
        for n, url in enumerate(("http://jobs.example.test/x", "https://10.0.0.5/form", "https://localhost/form", "https://jobs.example.test@evil.test/")):
            r = self.start(self.item(20 + n, url=url), c)
            self.assertEqual(r.status_code, 400, url)
        self.assertEqual(FakeDriver.instances, [])
        self.svc.resolver = lambda h: ["192.168.0.9"]
        self.assertEqual(self.start(self.item(40, url="https://rebind.example.test/x"), c).status_code, 400)

    def test_only_ready_web_items_with_a_draft(self):
        c = self.client()
        email = self.item(7, contact_email="hr@acme.test", channel="email")
        self.assertEqual(self.start(email, c).status_code, 400)
        done = self.item(8)
        self.aq.mark(done["id"], "skipped")
        self.assertEqual(self.start(done, c).status_code, 409)
        self.assertEqual(c.post("/api/applyq/aaaaaaaaaaaa/formfill/start", json={}).status_code, 404)

    def test_switched_off_and_missing_playwright(self):
        self.config.formfill_enabled = False
        c = self.client()
        item = self.item()
        self.assertEqual(self.start(item, c).status_code, 404)
        st = c.get("/api/formfill/status").json()
        self.assertFalse(st["enabled"])
        self.assertIn("formfill_enabled", st["reason"])

    def test_missing_playwright_status_and_start(self):
        self.build(driver_factory=None, available=lambda: False)
        c = self.client()
        st = c.get("/api/formfill/status").json()
        self.assertEqual((st["enabled"], st["playwright"]), (True, False))
        self.assertIn("python -m playwright install chromium", st["install"])
        r = self.start(self.item(), c)
        self.assertEqual(r.status_code, 503)
        self.assertIn("pip install playwright", r.json()["detail"])

    def test_driver_without_the_package_raises_a_clean_error(self):
        d = ff.PlaywrightDriver(self.tmp / "p")
        with mock.patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(ff.FormfillError) as cm:
                d.open("https://x.example.test/", True)
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("python -m playwright install chromium", cm.exception.message)

    def test_routes_need_login(self):
        self.build()
        app = FastAPI()
        app.include_router(self._router(self.svc))
        anon = TestClient(app)
        item = self.item()
        for method, path, kw in (("get", "/api/formfill/status", {}), ("get", "/api/formfill/never", {}), ("put", "/api/formfill/never", {"json": {"never": []}}),
                                 ("post", f"/api/applyq/{item['id']}/formfill/start", {"json": {}}), ("post", f"/api/applyq/{item['id']}/formfill/login", {}),
                                 ("get", f"/api/applyq/{item['id']}/formfill", {}), ("get", f"/api/applyq/{item['id']}/formfill/{'0' * 16}/screenshot", {}),
                                 ("post", f"/api/applyq/{item['id']}/formfill/{'0' * 16}/submit", {"json": {"confirm": True, "field_digest": "x"}}),
                                 ("post", f"/api/applyq/{item['id']}/formfill/{'0' * 16}/next", {"json": {"confirm": True}}),
                                 ("post", f"/api/applyq/{item['id']}/formfill/{'0' * 16}/cancel", {})):
            self.assertEqual(getattr(anon, method)(path, **kw).status_code, 401, path)
        self.assertEqual(FakeDriver.instances, [])


class PrivacyTest(Base):
    def test_audit_has_host_and_id_never_field_values_or_paths(self):
        c = self.client()
        item = self.item()
        run = c.post(f"/api/applyq/{item['id']}/formfill/start", json={}).json()
        c.get(run["screenshot_url"])
        c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": run["digest"]})
        events = [a[0][0] for a in self.audits]
        self.assertEqual(events, ["formfill_start", "formfill_submit", "formfill_result"])
        blob = json.dumps([[a[0], a[1]] for a in self.audits], default=str)
        self.assertIn("jobs.example.test", blob)
        self.assertIn(item["id"], blob)
        for secret in ("Test Person", "me@example.test", "+1 555 0100", "$50/h", "linkedin.com/in", "/apply", "resume", "Backend developer"):
            self.assertNotIn(secret, blob)

    def test_no_code_reads_cookies_or_storage(self):
        src = (Path(ff.__file__)).read_text(encoding="utf-8")
        for needle in (".cookies(", "storage_state", "add_cookies", "document.cookie", "localStorage", "sessionStorage", "get_cookies"):
            self.assertNotIn(needle, src)
        for needle in ("webdriver", "user_agent", "captcha_solver", "2captcha", "add_init_script"):
            self.assertNotIn(needle, src)

    def test_responses_never_carry_cookie_or_password_values(self):
        self.web[self.URL] = FORM + '<input type="password" name="pw" value="hunter2" style="display:none">'
        c = self.client()
        item = self.item()
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertNotIn("hunter2", r.text)
        self.assertNotIn("cookie", r.text.lower())
        self.assertNotIn("password", json.dumps(r.json()["fields"]).lower().replace("a password", ""))

    def test_screenshot_is_served_only_through_the_authenticated_route(self):
        c = self.client()
        item = self.item()
        run = c.post(f"/api/applyq/{item['id']}/formfill/start", json={}).json()
        files = list((self.tmp / "data" / "formfill_shots").glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(str(self.tmp), json.dumps(run))                          # no filesystem path in any response
        self.assertEqual(run["screenshot_url"], f"/api/applyq/{item['id']}/formfill/{run['run']}/screenshot")

    def test_profile_dir_lives_under_the_hub_data_dir(self):
        self.build()
        self.assertEqual(self.svc.profile_dir, Path(self.config.data_path()) / "formfill_profile")


class ToolsTest(SafetyBase):
    """Assistant tools. submit_form is in ALWAYS_ASK, so Full Control and auto-approve never skip the owner's tap."""

    def tools(self):
        real = {t.spec["name"]: t for t in applyq_tools.build(lambda: None, self.config)}
        self.assertIn("fill_application_form", real)
        spec = real["submit_form"]
        self.submit_spec = spec

        async def fake(a):
            self.ran.append(("submit_form", a))
            return True, "submitted (fake)"
        return [A.ExtraTool(spec.spec, fake, needs_approval=spec._needs, level="N2")]

    def test_registered_always_ask_and_levels(self):
        self.assertIn("submit_form", safety.ALWAYS_ASK)
        self.assertNotIn("fill_application_form", safety.ALWAYS_ASK)
        tools = {t.spec["name"]: t for t in applyq_tools.build(lambda: None, self.config)}
        self.assertTrue(tools["submit_form"].needs_approval({"id": "x"}))
        self.assertEqual(tools["submit_form"].declared_level({"id": "x"}), "N2")
        self.assertTrue(tools["fill_application_form"].needs_approval({"id": "x"}))
        self.assertEqual(tools["fill_application_form"].declared_level({"id": "x"}), "N2")
        self.assertEqual(tools["submit_form"].spec["input_schema"]["required"], ["id"])

    async def test_full_control_and_auto_approve_still_ask_for_submit_form(self):
        self.config.full_control = True
        svc = self.service([reply(tool_block("t1", "submit_form", {"id": "abc"}), stop="tool_use"), reply("done")])
        asked = []

        def answers(e):
            asked.append(e["name"])
            svc.resolve("s", e["id"], False)
        events = await self.drive(svc, answers, auto=True)
        self.assertEqual(asked, ["submit_form"])
        self.assertEqual(self.ran, [])
        self.assertTrue([e for e in events if e["type"] == "tool_start"][0]["needs_approval"])

    async def test_declined_or_approved(self):
        svc = self.service([reply(tool_block("t1", "submit_form", {"id": "abc"}), stop="tool_use"), reply("done")])
        await self.drive(svc, lambda e: svc.resolve("s", e["id"], True))
        self.assertEqual(len(self.ran), 1)


class ToolsThroughAppTest(Base):
    """The real tools against a real in-process hub route set."""

    def setUp(self):
        super().setUp()
        self.build()
        app = FastAPI()
        app.include_router(self._router(self.svc))
        app.include_router(applyq.make_router(self.ctx, profile_fn=lambda c: self.profile))
        self.app = app

        from hub import auth
        self.tools = {t.spec["name"]: t for t in applyq_tools.build(lambda: app, self.config)}
        # in-process calls carry a signed session cookie; the test auth only wants "yes"
        self.patch = mock.patch.object(auth, "make_session", lambda *a, **k: "yes")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def run_tool(self, name, **a):
        return asyncio.run(self.tools[name].run(a))

    def test_fill_then_submit_requires_a_look_first(self):
        item = self.item()
        ok, text = self.run_tool("fill_application_form", id=item["id"])
        self.assertTrue(ok, text)
        self.assertIn("filled", text)
        self.assertIn("Nothing is submitted", text)
        self.assertNotIn("me@example.test", text)                                  # field values never go to the model
        ok, info = asyncio.run(self.tools["submit_form"].preview({"id": item["id"]}))
        self.assertFalse(ok)
        self.assertIn("looked at the current screenshot", info)
        ok, text = self.run_tool("submit_form", id=item["id"])
        self.assertFalse(ok)
        self.assertFalse(any(x[0] == "click" for x in FakeDriver.instances[0].calls))
        run = self.svc.run
        run.seen = run.digest                                                       # the owner looked (screenshot route does this)
        ok, info = asyncio.run(self.tools["submit_form"].preview({"id": item["id"]}))
        self.assertTrue(ok, info)
        self.assertEqual(info["site"], "jobs.example.test")
        ok, text = self.run_tool("submit_form", id=item["id"])
        self.assertTrue(ok, text)
        self.assertIn("marked applied", text)
        self.assertEqual(self.aq.get(item["id"])["state"], "applied")

    def test_submit_needs_an_id_and_an_open_form(self):
        ok, _ = self.run_tool("submit_form")
        self.assertFalse(ok)
        ok, text = self.run_tool("submit_form", id=self.item()["id"])
        self.assertFalse(ok)
        self.assertIn("No form is open", text)


# ---- the real driver's glue, against a fake Playwright object model -----------------------------------------------

class FakeRequest:
    def __init__(self, url, nav=True): self.url, self._nav = url, nav
    def is_navigation_request(self): return self._nav


class FakeRoute:
    def __init__(self): self.result = None
    def abort(self, reason="failed"): self.result = ("abort", reason)
    def continue_(self): self.result = ("continue", None)


class FakeLocator:
    def __init__(self, page, sel): self.page, self.sel = page, sel
    @property
    def first(self): return self
    def fill(self, v, timeout=0): self.page.log.append(("fill", self.sel, v))
    def select_option(self, value=None, timeout=0): self.page.log.append(("select", self.sel, value))
    def check(self, force=False, timeout=0): self.page.log.append(("check", self.sel, force))
    def set_input_files(self, p, timeout=0): self.page.log.append(("upload", self.sel, p))
    def click(self, timeout=0): self.page.log.append(("click", self.sel))


class FakePage:
    url = "about:blank"
    def __init__(self, ctx=None): self.log, self.ctx = [], ctx
    def goto(self, url, wait_until=None, timeout=0):
        self.url = url
        if self.ctx is not None and self.ctx.redirect_to:       # a redirect to a private address goes through the route handler
            self.ctx.handler(FakeRoute(), FakeRequest(self.ctx.redirect_to))
            raise RuntimeError("net::ERR_BLOCKED_BY_CLIENT")
        return mock.Mock(status=200)
    def evaluate(self, js): self.log.append(("evaluate",)); return {}
    def content(self): return FORM
    def locator(self, sel): return FakeLocator(self, sel)
    def wait_for_load_state(self, *a, **k): pass
    def screenshot(self, **k): return PNG


class FakeContext:
    def __init__(self, opts, redirect_to=""):
        self.opts, self.handler, self.closed, self.redirect_to = opts, None, False, redirect_to
        self.page = FakePage(self)
        self.pages = [self.page]
    def route(self, pattern, handler): self.pattern, self.handler = pattern, handler
    def close(self): self.closed = True
    def cookies(self, *a): raise AssertionError("cookies must never be read")                  # noqa: ARG002
    def storage_state(self, *a, **k): raise AssertionError("storage state must never be read")


class FakeChromium:
    def __init__(self, fail_first=0, redirect_to=""): self.launches, self.fail_first, self.redirect_to = [], fail_first, redirect_to
    def launch_persistent_context(self, **opts):
        self.launches.append(opts)
        if len(self.launches) <= self.fail_first:
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at /x")
        self.ctx = FakeContext(opts, self.redirect_to)
        return self.ctx


class FakePW:
    def __init__(self, chromium): self.chromium, self.stopped = chromium, False
    def start(self): return self
    def stop(self): self.stopped = True


class PlaywrightGlueTest(unittest.TestCase):
    def driver(self, chromium, **kw):
        tmp = Path(tempfile.mkdtemp(prefix="pwglue-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return ff.PlaywrightDriver(tmp / "formfill_profile", resolver=PUBLIC, sync_factory=lambda: FakePW(chromium), **kw), tmp

    def test_persistent_profile_headed_and_navigation_guard(self):
        ch = FakeChromium()
        d, tmp = self.driver(ch)
        self.assertEqual(d.open("https://jobs.example.test/apply", True), 200)
        opts = ch.launches[0]
        self.assertEqual(opts["user_data_dir"], str(tmp / "formfill_profile"))
        self.assertFalse(opts["headless"])
        self.assertFalse(opts["accept_downloads"])
        route = FakeRoute()
        ch.ctx.handler(route, FakeRequest("http://169.254.169.254/latest"))
        self.assertEqual(route.result[0], "abort")
        self.assertNotEqual(d.blocked_navigation(), "")
        route = FakeRoute()
        ch.ctx.handler(route, FakeRequest("https://jobs.example.test/next"))
        self.assertEqual(route.result[0], "continue")
        route = FakeRoute()
        ch.ctx.handler(route, FakeRequest("https://cdn.example.test/logo.png", nav=False))     # subresources are not navigations
        self.assertEqual(route.result[0], "continue")
        d.fill_text(3, "hello")
        d.select(4, "v")
        d.choose(5)
        d.upload(6, "/tmp/x.pdf")
        d.click(2)
        log = ch.ctx.page.log
        self.assertIn(("fill", '[data-rl-idx="3"]', "hello"), log)
        self.assertIn(("click", '[data-rl-btn="2"]'), log)
        self.assertEqual(d.screenshot(), PNG)
        d.close()
        self.assertTrue(ch.ctx.closed)

    def test_headless_flag_follows_the_window_setting(self):
        ch = FakeChromium()
        d, _ = self.driver(ch)
        d.open("https://jobs.example.test/apply", False)
        self.assertTrue(ch.launches[0]["headless"])

    def test_falls_back_to_the_owners_chrome_then_edge(self):
        ch = FakeChromium(fail_first=1)
        d, _ = self.driver(ch)
        d.open("https://jobs.example.test/apply", True)
        self.assertEqual([l.get("channel") for l in ch.launches], [None, "chrome"])
        ch = FakeChromium(fail_first=3)
        d, _ = self.driver(ch)
        with self.assertRaises(ff.FormfillError) as cm:
            d.open("https://jobs.example.test/apply", True)
        self.assertIn("python -m playwright install chromium", cm.exception.message)

    def test_a_refused_first_navigation_is_a_clean_error(self):
        ch = FakeChromium(redirect_to="http://10.0.0.7/admin")
        d, _ = self.driver(ch)
        with self.assertRaises(ff.FormfillError) as cm:
            d.open("https://jobs.example.test/", True)
        self.assertIn("refused", cm.exception.message)
        self.assertEqual(cm.exception.status, 400)


# ---- one real run, when Playwright and a browser exist ---------------------------------------------------------------

REAL_FORM = """<html><body><form action="/thanks" method="get">
<label for="n">Full name *</label><input id="n" name="name" required>
<label for="e">Email</label><input id="e" type="email" name="email">
<label for="why">Why do you want to work here?</label><textarea id="why" name="why"></textarea>
<label for="notice">Notice period</label><select id="notice" name="notice"><option>Immediately</option><option>2 weeks</option></select>
<label for="dob">Date of birth</label><input id="dob" name="dob">
<fieldset><legend>Are you willing to relocate?</legend><label><input type="radio" name="r" value="y"> Yes</label><label><input type="radio" name="r" value="n"> No</label></fieldset>
<label for="cv">Resume</label><input id="cv" type="file" name="cv">
<input name="trap" style="position:absolute;left:-9999px">
<button type="submit">Submit application</button></form></body></html>"""


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        body = (REAL_FORM if self.path.startswith("/apply") else "<h1>Thank you for applying!</h1>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def browser_options():
    """Launch options for a real browser, or None. Prefers Playwright's Chromium, else a system Chrome/Chromium."""
    if not ff.playwright_available():
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        if exe and Path(exe).exists():
            return {"args": ["--no-sandbox"]}
    except Exception:  # noqa: BLE001
        pass
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return {"executable_path": path, "args": ["--no-sandbox"]}
    return None


@unittest.skipUnless(browser_options(), "Playwright with a Chromium/Chrome is not installed")
class RealBrowserTest(Base):
    def test_fills_a_local_form_and_submits_only_after_confirm(self):
        server = HTTPServer(("127.0.0.1", 0), QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.config.formfill_headed = False
        opts = browser_options()
        self.profile["standard_answers"].update({"Willing to relocate": "No", "Notice period": "2 weeks"})
        self.build(driver_factory=lambda pd: ff.PlaywrightDriver(pd, allow_local=True, launch_options=opts), allow_local=True)
        self.addCleanup(lambda: self.svc.pool.submit(self.svc._end, "test").result(timeout=20))
        item = self.item(url=base + "/apply")
        c = self.client()
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 200, r.text)
        run = r.json()
        by = {f["label"]: f for f in run["fields"]}
        self.assertEqual(by["Full name *"]["status"], "filled")
        self.assertEqual(by["Email"]["value"], "me@example.test")
        self.assertEqual(by["Notice period"]["value"], "2 weeks")
        self.assertEqual(by["Are you willing to relocate?"]["value"], "No")
        self.assertEqual(by["Date of birth"]["status"], "needs_you")
        self.assertNotIn("trap", json.dumps(run))
        self.assertTrue(run["can_submit"])
        shot = c.get(run["screenshot_url"])
        self.assertTrue(shot.content.startswith(b"\x89PNG"))
        # nothing has been submitted yet: the server saw only the GET of the form
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": "bad"})
        self.assertEqual(r.status_code, 409)
        run = c.get(f"/api/applyq/{item['id']}/formfill/{run['run']}").json()
        c.get(run["screenshot_url"])
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["result"], "applied")
        self.assertEqual(self.aq.get(item["id"])["state"], "applied")


if __name__ == "__main__":
    unittest.main()
