"""Tests for the SuccessFactors, Taleo, Oracle Recruiting, iCIMS and Ashby form adapters (hub/formadapters inside hub/formfill.py).
Invented data only; the pages are static fixtures in tests/fixtures/<system>/ built from each system's known markup, never fetched from
a real tenant. The reader is tested as pure functions, the flow with a FAKE browser that models the widgets, and one run per system in
a real headless browser (skipped when Playwright or a Chromium is missing) that serves the fixtures from 127.0.0.1 with a tiny script
imitating the pop-up lists and Yes/No buttons."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hub import formadapters as fa, formfill as ff
from tests.test_formfill import FORM, PNG, Base, browser_options

FIX = Path(__file__).parent / "fixtures"
URLS = {
    "ashby": "https://jobs.ashbyhq.com/acme-test/11111111-2222-3333-4444-555555555555/application",
    "taleo": "https://acme.taleo.net/careersection/ex/jobapply.ftl",
    "oraclerc": "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1234/apply",
    "successfactors": "https://career5.successfactors.com/career?company=acmetest&career_job_req_id=1234",
    "icims": "https://careers-acme.icims.com/jobs/1234/python-developer/job",
}
PAGES = {s: {p.name: p.read_text(encoding="utf-8") for p in (FIX / s).glob("*.html")} for s in URLS}
STD = {"Notice period": "2 weeks", "Country": "India", "Email": "me@example.test", "Phone": "+91 5550 0100", "Willing to relocate": "No",
       "Location": "Pune, India"}


def parsed(system, name, html=None):
    page = ff.parse_page(html if html is not None else PAGES[system][name])
    return {"ashby": fa.ashby.ASHBY, "taleo": fa.taleo.TALEO, "oraclerc": fa.oraclerc.ORACLE, "successfactors": fa.successfactors.SUCCESSFACTORS,
            "icims": fa.icims.ICIMS}[system].parse(page, URLS[system])


def widgets(w):
    return {f["label"]: f["wd"]["widget"] for f in w["fields"]}


# ---- which system is this page -----------------------------------------------------------------------------------

class DetectTest(unittest.TestCase):
    def test_hosts_turn_the_right_system_on(self):
        for key, url in URLS.items():
            self.assertEqual(fa.for_page(url, "").key, key, key)
        for url, key in (("https://acme.sapsf.com/career", "successfactors"), ("https://jobs.sap.com/job/x", "successfactors"),
                         ("https://acme.taleo.net/x", "taleo"), ("https://acme.icims.com/jobs/1/x/job", "icims"), ("https://jobs.ashbyhq.com/acme", "ashby")):
            self.assertEqual(fa.for_page(url, "").key, key, url)

    def test_look_alike_hosts_and_other_oracle_products_are_not_matched(self):
        for url in ("https://notataleo.net/x", "https://taleo.net.evil.test/x", "https://jobs.ashbyhq.com.evil.test/x", "https://icims.com.evil.test/x",
                    "https://acme.fa.us2.oraclecloud.com/fscmUI/faces/x", "https://acme.oraclecloud.com/"):
            self.assertIsNone(fa.for_page(url, ""), url)

    def test_markup_turns_a_custom_domain_on_but_a_plain_page_does_not(self):
        self.assertEqual(fa.for_page("https://careers.example.test/apply", PAGES["ashby"]["application.html"]).key, "ashby")
        self.assertEqual(fa.for_page("https://careers.example.test/apply", PAGES["taleo"]["personal.html"]).key, "taleo")
        self.assertEqual(fa.for_page("https://careers.example.test/apply", PAGES["oraclerc"]["apply.html"]).key, "oraclerc")
        self.assertEqual(fa.for_page("https://careers.example.test/apply", PAGES["successfactors"]["apply.html"]).key, "successfactors")
        self.assertEqual(fa.for_page("https://careers.example.test/apply", PAGES["icims"]["frame-apply.html"]).key, "icims")
        self.assertIsNone(fa.for_page("https://jobs.example.test/apply", FORM))

    def test_icims_frame_is_wanted_only_for_icims_pages(self):
        self.assertTrue(fa.frame_wanted("https://careers-acme.icims.com/jobs/1/x/job", ""))
        self.assertTrue(fa.frame_wanted("https://careers.example.test/job", PAGES["icims"]["job.html"]))
        self.assertFalse(fa.frame_wanted("https://jobs.example.test/apply", FORM))


class GenericPathUntouchedTest(Base):
    def test_other_sites_never_reach_an_adapter(self):
        self.build()
        page = ff.parse_page(FORM)
        self.assertIsNone(self.svc._wd_read("https://jobs.example.test/apply", FORM, page))

    def test_plain_driver_calls_are_unchanged(self):
        d = ff.PlaywrightDriver(Path("."))
        self.assertIsNone(d._sc)
        self.assertIs(d._s(), d._page)


# ---- the pure reading --------------------------------------------------------------------------------------------

class AshbyParseTest(unittest.TestCase):
    def test_widgets_and_labels(self):
        w = parsed("ashby", "application.html")
        k = widgets(w)
        self.assertEqual(k["Name"], "text")
        self.assertEqual(k["Resume"], "file")
        self.assertEqual(k["Current location"], "combo")
        self.assertEqual(k["How did you hear about us?"], "combo")
        self.assertEqual(k["Are you willing to relocate?"], "yesno")
        self.assertEqual(k["I agree to the privacy policy"], "checkbox")
        by = {f["label"]: f for f in w["fields"]}
        self.assertTrue(by["Name"]["required"] and by["Are you willing to relocate?"]["required"])
        self.assertFalse(by["Do you have a friend working here?"]["required"])
        self.assertEqual(sorted(by["Are you willing to relocate?"]["wd"]["btns"]), ["no", "yes"])

    def test_yes_no_buttons_and_autofill_are_never_page_controls(self):
        c = parsed("ashby", "application.html")["controls"]
        self.assertEqual((c["submit"]["text"], c["next"], c["ambiguous"]), ("Submit Application", None, False))

    def test_the_voluntary_section_is_marked_but_the_page_is_not_a_never_step(self):
        w = parsed("ashby", "application.html")
        self.assertFalse(w["never"])
        never = {f["label"] for f in w["fields"] if f["wd"]["never"]}
        self.assertEqual(never, {"Preferred pronouns", "Veteran status", "Anything else you want to share about yourself?"})


class TaleoParseTest(unittest.TestCase):
    def test_steps_controls_and_never_step(self):
        p, e, r = (parsed("taleo", n) for n in ("personal.html", "eeo.html", "review.html"))
        self.assertEqual((p["step"]["current"], p["step"]["total"], p["step"]["title"]), (2, 4, "Personal Information"))
        self.assertEqual((p["controls"]["next"]["text"], p["controls"]["submit"]), ("Next", None))
        self.assertTrue(e["never"])
        self.assertFalse(p["never"])
        self.assertEqual((r["controls"]["submit"]["text"], r["controls"]["next"]), ("Submit", None))

    def test_sign_in_page_is_a_login_wall(self):
        w = parsed("taleo", "login.html")
        self.assertTrue(w["login"])
        self.assertEqual([b["kind"] for b in fa.taleo.TALEO.blockers(w, [])], ["login_required"])
        self.assertFalse(parsed("taleo", "personal.html")["login"])


class OracleParseTest(unittest.TestCase):
    def test_jet_widgets_use_the_label_hint_and_class_hints(self):
        k = widgets(parsed("oraclerc", "apply.html"))
        self.assertEqual(k["First Name"], "text")                 # label only in the oj-input-text label-hint
        self.assertEqual(k["Country"], "combo")
        self.assertEqual(k["How did you hear about us?"], "combo")
        self.assertEqual(k["Available from"], "date")             # a date placeholder, not a combo
        self.assertEqual(k["Date of birth"], "date")
        self.assertEqual(k["Are you willing to relocate?"], "radio")
        self.assertEqual(k["I consent to the processing of my personal data"], "checkbox")


class SuccessFactorsParseTest(unittest.TestCase):
    def test_ui5_widgets(self):
        k = widgets(parsed("successfactors", "apply.html"))
        self.assertEqual(k["Country/Region"], "combo")
        self.assertEqual(k["Preferred Language"], "select")
        self.assertEqual(k["Available From"], "date")
        self.assertEqual(k["First Name"], "text")
        self.assertEqual(k["Resume"], "file")

    def test_login_and_controls(self):
        self.assertTrue(parsed("successfactors", "login.html")["login"])
        c = parsed("successfactors", "apply.html")["controls"]
        self.assertEqual((c["next"]["text"], c["submit"]), ("Next", None))
        self.assertEqual(parsed("successfactors", "review.html")["controls"]["submit"]["text"], "Submit")


class ICIMSParseTest(unittest.TestCase):
    def test_the_frame_content_reads_like_a_page(self):
        w = parsed("icims", "frame-apply.html")
        k = widgets(w)
        self.assertEqual((k["First Name"], k["Country"], k["Resume"]), ("text", "select", "file"))
        self.assertEqual(w["step"]["current"], 1)
        self.assertEqual(w["controls"]["next"]["text"], "Next")
        self.assertTrue(parsed("icims", "frame-login.html")["login"])
        self.assertEqual(parsed("icims", "frame-review.html")["controls"]["submit"]["text"], "Submit")


# ---- the flow with a fake browser --------------------------------------------------------------------------------------

def _walk(n):
    for c in n.children:
        if isinstance(c, str) or c.tag in ff.SKIP:
            continue
        yield c
        yield from _walk(c)


class AdFake(ff.Driver):
    """Pages are the fixture strings. It remembers what was typed, picked or pressed, moves to the next page when a data-next button is
    clicked and records every call so tests can assert what was and was not touched."""
    instances: list = []

    def __init__(self, system, start, **opts):
        self.system, self.name, self.opts = system, start, opts
        self.vals, self.shown, self.pressed, self.calls = {}, {}, set(), []
        self.submitted = 0
        self.shots = 0
        self.closed = False
        AdFake.instances.append(self)

    def html(self): return PAGES[self.system][self.name] if self.name in PAGES[self.system] else "<html><body><h1>Thank you for applying!</h1></body></html>"
    def page(self): return ff.parse_page(self.html())
    def url(self): return URLS[self.system]
    def open(self, url, headed): self.calls.append(("open", url)); return 200

    def node(self, kind, idx):
        page = self.page()
        table = page["index"][kind]
        return next(n for n in _walk(page["root"]) if id(n) in table and table[id(n)] == idx)

    def field(self, idx): return next(f for f in self.page()["fields"] if f["idx"] == idx)

    def values(self):
        out = {}
        for f in self.page()["fields"]:
            for o in (f["options"] if f["type"] in ("radio", "checkbox") else [f]):
                out[o["idx"]] = self.vals.get((self.name, o["idx"]), "0" if f["type"] in ("radio", "checkbox") else "")
        return out

    def _set(self, idx, v): self.vals[(self.name, idx)] = v
    def fill_text(self, idx, value): self.calls.append(("text", idx)); self._set(idx, value)
    def type_text(self, idx, value): self.calls.append(("type", idx)); self._set(idx, value)
    def select(self, idx, value): self.calls.append(("select", idx)); self._set(idx, value)
    def choose(self, idx): self.calls.append(("choose", idx)); self._set(idx, "1")
    def upload(self, idx, path): self.calls.append(("upload", idx)); self._set(idx, Path(path).name)
    def settle(self): pass
    def blocked_navigation(self): return ""
    def screenshot(self): self.shots += 1; return PNG + str(self.shots).encode()
    def close(self): self.closed = True
    def close_list(self): self.calls.append(("close_list",))

    def click(self, idx):
        self.calls.append(("click", idx))
        node = self.node("b", idx)
        if node.get("data-yesno"):
            if not self.opts.get("lose_yesno"):
                self.pressed = {i for i in self.pressed if self.node("b", i).parent is not node.parent} | {idx}
        elif node.get("data-submit"):
            self.submitted += 1
            self.name = "thanks"
        elif node.get("data-next"):
            self.name = node.get("data-next")

    def combo_options(self, idx, query=""):
        self.calls.append(("combo_options", idx, query))
        node = self.node("f", idx)
        opts = json.loads(node.get("data-options") or "[]")
        if query and node.attrs.get("data-search") is not None:
            opts = [o for o in opts if query.lower() in o.lower()]
        return opts

    def combo_pick(self, idx, text):
        self.calls.append(("combo_pick", idx, text))
        if self.opts.get("lose_combo"):
            return ""
        self.shown[(self.name, idx)] = text
        return text

    def ad_values(self):
        out = {}
        for f in self.page()["fields"]:
            node = self.node("f", f["idx"]) if f["tag"] == "input" and f["type"] not in ("radio", "checkbox") else None
            if node is not None and node.get("data-options"):
                out[f"c{f['idx']}"] = self.shown.get((self.name, f["idx"]), "")
        for b in self.page()["buttons"]:
            if b["text"].lower() in ("yes", "no"):
                out[f"t{b['idx']}"] = "1" if b["idx"] in self.pressed else "0"
        return out

    def plain(self, kind): return [c for c in self.calls if c[0] == kind]


class AdBase(Base):
    system = "ashby"
    start_page = "application.html"

    def setUp(self):
        super().setUp()
        AdFake.instances = []
        self.profile = {**self.profile, "standard_answers": dict(STD)}

    def drv(self): return AdFake.instances[0]

    def go(self, start=None, **opts):
        page = start or self.start_page
        self.build(driver_factory=lambda pd: AdFake(self.system, page, **opts))
        c = self.client()
        item = self.item(url=URLS[self.system])
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 200, r.text)
        return c, item, r.json()

    def next(self, c, item, run):
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/next", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def by(run): return {f["label"]: f for f in run["fields"]}

    def status(self, run, label): return (run["fields"] and self.by(run)[label]["status"])


class AshbyFlowTest(AdBase):
    def test_fills_text_combos_and_yes_no_and_leaves_the_rest(self):
        c, item, run = self.go()
        by = self.by(run)
        for label, want in (("Name", "Test Person"), ("Email", "me@example.test"), ("Phone", "+91 5550 0100"), ("Current location", "Pune, India"),
                            ("How did you hear about us?", "Job Board"), ("Are you willing to relocate?", "No"), ("What is your notice period?", "2 weeks")):
            self.assertEqual((label, by[label]["status"], by[label]["value"]), (label, "filled", want))
        self.assertEqual(by["Resume"]["status"], "filled")
        self.assertEqual(by["LinkedIn URL"]["value"], "https://www.linkedin.com/in/test")

    def test_legal_voluntary_and_consent_stay_with_the_owner(self):
        c, item, run = self.go()
        by = self.by(run)
        self.assertEqual(by["Are you legally authorized to work in India?"]["status"], "needs_you")
        for label in ("Preferred pronouns", "Veteran status", "Anything else you want to share about yourself?"):
            self.assertEqual(by[label]["status"], "needs_you", label)
            self.assertIn("voluntary", by[label]["reason"])
        self.assertNotEqual(by["I agree to the privacy policy"]["status"], "filled")
        self.assertNotEqual(by["Do you have a friend working here?"]["status"], "filled")
        d = self.drv()
        pressed = {d.node("b", i).parent.get("class") + str(i) for i in d.pressed}
        self.assertEqual(len(pressed), 1)                                           # only the relocate question was answered
        self.assertFalse([c for c in d.plain("select")])                            # the veteran select was never touched

    def test_nothing_is_submitted_and_autofill_is_not_pressed(self):
        c, item, run = self.go()
        d = self.drv()
        clicked = [d.node("b", c[1]).get("class") for c in d.plain("click")]
        self.assertEqual(len(clicked), 1)
        self.assertEqual(d.submitted, 0)
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(run["submit_label"], "Submit Application")
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 409)                                        # the owner has not looked at the screenshot yet
        self.assertEqual(d.submitted, 0)

    def test_a_choice_that_is_not_listed_is_not_forced(self):
        self.profile["standard_answers"]["Location"] = "Atlantis"
        self.profile["location"] = "Atlantis"
        c, item, run = self.go()
        self.assertEqual(self.by(run)["Current location"]["status"], "needs_you")
        self.assertIn(("close_list",), self.drv().calls)
        self.assertNotIn(4, [c[1] for c in self.drv().plain("combo_pick")])        # the location combo was never picked (the source combo was)

    def test_a_page_that_loses_a_pick_or_a_press_is_reported(self):
        c, item, run = self.go(lose_combo=True, lose_yesno=True)
        by = self.by(run)
        self.assertEqual(by["Current location"]["status"], "needs_you")
        self.assertEqual(by["Are you willing to relocate?"]["status"], "needs_you")
        self.assertIn("did not keep", by["Are you willing to relocate?"]["reason"])

    def test_digest_moves_when_a_combo_or_a_yes_no_changes(self):
        c, item, run = self.go()
        d = self.drv()
        before = run["digest"]
        d.shown[(d.name, next(i for i, v in [(k[1], v) for k, v in d.shown.items()] if v == "Pune, India"))] = "Mumbai, India"
        mid = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh").json()["digest"]
        self.assertNotEqual(before, mid)
        d.pressed = set()
        after = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh").json()["digest"]
        self.assertNotEqual(mid, after)


class TaleoFlowTest(AdBase):
    system, start_page = "taleo", "personal.html"

    def test_walks_the_wizard_only_on_the_owners_confirm(self):
        c, item, run = self.go()
        by = self.by(run)
        self.assertEqual(run["step"]["label"], "step 2 of 4 filled (Personal Information)")
        for label, want in (("First Name", "Test"), ("Last Name", "Person"), ("E-mail Address", "me@example.test"), ("Mobile Phone", "+91 5550 0100"),
                            ("Country/Territory", "India"), ("How did you hear about us?", "Job Board")):
            self.assertEqual((label, by[label]["status"], by[label]["value"]), (label, "filled", want))
        self.assertEqual(by["Date of Birth"]["status"], "needs_you")
        self.assertEqual(by["Current Salary"]["status"], "needs_you")
        self.assertNotEqual(by["I agree to the terms and the privacy statement"]["status"], "filled")
        self.assertFalse(self.drv().plain("choose"))
        d = self.drv()
        self.assertEqual(d.name, "personal.html")                                   # no Next was pressed while filling
        self.assertTrue(run["can_next"] and not run["can_submit"])

        run = self.next(c, item, run)                                               # voluntary step: untouched
        self.assertEqual(run["counts"]["filled"], 0)
        calls_before = len(d.calls)
        self.assertEqual(d.name, "eeo.html")
        self.assertTrue(all(f["status"] == "needs_you" for f in run["fields"]))
        self.assertEqual(len(d.calls), calls_before)

        run = self.next(c, item, run)                                               # review
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(d.submitted, 0)

    def test_sign_in_wall_is_a_login_blocker_and_nothing_is_filled(self):
        c, item, run = self.go("login.html")
        self.assertIn("login_required", [b["kind"] for b in run["blockers"]])
        self.assertEqual(run["counts"]["filled"], 0)
        self.assertFalse(self.drv().plain("text"))


class OracleFlowTest(AdBase):
    system, start_page = "oraclerc", "apply.html"

    def test_fills_jet_combos_text_and_radios_but_not_dates_or_consent(self):
        c, item, run = self.go()
        by = self.by(run)
        for label, want in (("First Name", "Test"), ("Last Name", "Person"), ("Email", "me@example.test"), ("Phone Number", "+91 5550 0100"),
                            ("Country", "India"), ("How did you hear about us?", "Job Board")):
            self.assertEqual((label, by[label]["status"], by[label]["value"]), (label, "filled", want))
        self.assertEqual(by["Are you willing to relocate?"]["status"], "filled")
        self.assertEqual(by["Available from"]["status"], "needs_you")
        self.assertEqual(by["Date of birth"]["status"], "needs_you")
        self.assertEqual(by["Do you require visa sponsorship?"]["status"], "needs_you")
        self.assertNotEqual(by["I consent to the processing of my personal data"]["status"], "filled")
        d = self.drv()
        self.assertEqual(d.name, "apply.html")
        self.assertEqual(len(d.plain("choose")), 1)
        self.assertEqual(run["step"]["label"], "step 1 of 3 filled (Contact Information)")

    def test_review_offers_submit_and_never_clicks_it(self):
        c, item, run = self.go()
        run = self.next(c, item, run)
        self.assertEqual(run["submit_label"], "Submit")
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(self.drv().submitted, 0)


class SuccessFactorsFlowTest(AdBase):
    system, start_page = "successfactors", "apply.html"

    def test_fills_ui5_combo_native_select_and_text(self):
        c, item, run = self.go()
        by = self.by(run)
        for label, want in (("First Name", "Test"), ("Email Address", "me@example.test"), ("Country/Region", "India"), ("How did you hear about us?", "Job Board")):
            self.assertEqual((label, by[label]["status"], by[label]["value"]), (label, "filled", want))
        self.assertEqual(by["Preferred Language"]["status"], "skipped")           # nothing in the profile about it
        self.assertEqual(by["Available From"]["status"], "needs_you")
        self.assertEqual(by["Nationality"]["status"], "needs_you")
        self.assertNotEqual(by["I accept the data privacy statement"]["status"], "filled")
        self.assertTrue(run["can_next"] and not run["can_submit"])
        self.assertEqual(self.drv().plain("combo_options")[0][2], "")             # the list is opened as it is first

    def test_search_as_you_type_combo_gets_the_words_typed_when_the_open_list_has_no_match(self):
        self.profile["standard_answers"]["Country"] = "India"
        c, item, run = self.go()
        d = self.drv()
        self.assertTrue(any(call[0] == "combo_options" for call in d.calls))

    def test_login_wall(self):
        c, item, run = self.go("login.html")
        self.assertIn("login_required", [b["kind"] for b in run["blockers"]])
        self.assertEqual(run["counts"]["filled"], 0)


class ICIMSFlowTest(AdBase):
    system, start_page = "icims", "frame-apply.html"

    def test_fills_the_framed_form(self):
        c, item, run = self.go()
        by = self.by(run)
        for label, want in (("First Name", "Test"), ("Last Name", "Person"), ("Email", "me@example.test"), ("Country", "India"), ("How did you hear about us?", "Job Board")):
            self.assertEqual((label, by[label]["status"], by[label]["value"]), (label, "filled", want))
        self.assertEqual(by["Previous Salary"]["status"], "needs_you")
        self.assertNotEqual(by["I agree to the terms of use"]["status"], "filled")
        self.assertEqual(by["Resume"]["status"], "filled")
        run = self.next(c, item, run)
        self.assertEqual(run["submit_label"], "Submit")
        self.assertEqual(self.drv().submitted, 0)

    def test_login_wall_in_the_frame(self):
        c, item, run = self.go("frame-login.html")
        self.assertIn("login_required", [b["kind"] for b in run["blockers"]])
        self.assertEqual(run["counts"]["filled"], 0)


# ---- a real browser on the fixtures ---------------------------------------------------------------------------------------

class FixtureHandler(BaseHTTPRequestHandler):
    root: Path = FIX
    posts: list = []

    def log_message(self, *a): pass

    def do_GET(self):
        name = self.path.split("?")[0].lstrip("/")
        path = (FIX / "formadapters" / "ad-mock.js") if name == "ad-mock.js" else self.root / name
        if not path.is_file() or path.parent not in (self.root, FIX / "formadapters"):
            self.send_response(404)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if name.endswith(".js") else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        FixtureHandler.posts.append(self.path)
        self.send_response(204)
        self.end_headers()


@unittest.skipUnless(browser_options(), "Playwright with a Chromium/Chrome is not installed")
class RealBrowserAdaptersTest(AdBase):
    def serve(self, system, page):
        FixtureHandler.posts = []
        handler = type("H", (FixtureHandler,), {"root": FIX / system})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config.formfill_headed = False
        opts = browser_options()
        self.build(driver_factory=lambda pd: ff.PlaywrightDriver(pd, allow_local=True, launch_options=opts), allow_local=True)
        self.addCleanup(lambda: self.svc.pool.submit(self.svc._end, "test").result(timeout=20))
        c = self.client()
        item = self.item(url=f"http://127.0.0.1:{server.server_port}/{page}")
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 200, r.text)
        return c, item, r.json()

    def on_page(self, fn):
        return self.svc.pool.submit(fn).result(timeout=30)

    def sc(self):
        drv = self.svc.run.driver
        return lambda: drv._s()

    def test_ashby_react_select_and_yes_no_buttons(self):
        c, item, run = self.serve("ashby", "application.html")
        by = self.by(run)
        self.assertEqual((by["Current location"]["status"], by["Current location"]["value"]), ("filled", "Pune, India"))
        self.assertEqual((by["How did you hear about us?"]["status"], by["How did you hear about us?"]["value"]), ("filled", "Job Board"))
        self.assertEqual((by["Are you willing to relocate?"]["status"], by["Are you willing to relocate?"]["value"]), ("filled", "No"))
        s = self.sc()
        self.assertEqual(self.on_page(lambda: s().locator("#loc").input_value()), "Pune, India")
        self.assertEqual(self.on_page(lambda: s().locator("#src-value").inner_text()), "Job Board")             # shown in the value box, not the input
        pressed = self.on_page(lambda: s().locator('button[aria-pressed="true"]').all_inner_texts())
        self.assertEqual(pressed, ["No"])                                                                          # only the relocate question
        self.assertEqual(self.on_page(lambda: s().locator("#vet").input_value()), "")
        self.assertFalse(self.on_page(lambda: s().locator("#priv").is_checked()))
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(FixtureHandler.posts, [])
        # the digest moves when the owner changes a choice in the window
        before = run["digest"]
        self.on_page(lambda: s().locator('button[data-yesno]').nth(0).click())
        after = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh").json()["digest"]
        self.assertNotEqual(before, after)

    def test_taleo_wizard(self):
        c, item, run = self.serve("taleo", "personal.html")
        by = self.by(run)
        self.assertEqual((by["Country/Territory"]["value"], by["How did you hear about us?"]["value"]), ("India", "Job Board"))
        s = self.sc()
        self.assertEqual(self.on_page(lambda: s().locator("#requisitionListInterface\\.ID6\\.row1").input_value()), "IN")
        self.assertEqual(self.on_page(lambda: s().locator("#requisitionListInterface\\.ID9\\.row1").input_value()), "")      # date of birth
        run = self.next(c, item, run)
        self.assertEqual(run["counts"]["filled"], 0)                                                               # voluntary page untouched
        self.assertEqual(self.on_page(lambda: s().locator("select").first.input_value()), "-1")
        run = self.next(c, item, run)
        self.assertEqual(run["submit_label"], "Submit")
        self.assertEqual(FixtureHandler.posts, [])

    def test_oracle_jet_combos_and_radios(self):
        c, item, run = self.serve("oraclerc", "apply.html")
        by = self.by(run)
        self.assertEqual((by["Country"]["status"], by["Country"]["value"]), ("filled", "India"))
        self.assertEqual(by["How did you hear about us?"]["value"], "Job Board")
        self.assertEqual(by["First Name"]["value"], "Test")
        s = self.sc()
        self.assertEqual(self.on_page(lambda: s().locator('[id="country|input"]').input_value()), "India")
        self.assertEqual(self.on_page(lambda: s().locator('input[name="reloc"]:checked').get_attribute("value")), "no")
        self.assertFalse(self.on_page(lambda: s().locator('input[name="visa"]:checked').count()))
        self.assertEqual(self.on_page(lambda: s().locator('[id="avail|input"]').input_value()), "")
        self.assertFalse(self.on_page(lambda: s().locator('input[name="consent"]').is_checked()))
        run = self.next(c, item, run)
        self.assertEqual(run["submit_label"], "Submit")
        self.assertEqual(FixtureHandler.posts, [])

    def test_successfactors_ui5_combo_and_select(self):
        c, item, run = self.serve("successfactors", "apply.html")
        by = self.by(run)
        self.assertEqual((by["Country/Region"]["status"], by["Country/Region"]["value"]), ("filled", "India"))
        self.assertEqual(by["How did you hear about us?"]["status"], "filled")
        s = self.sc()
        self.assertEqual(self.on_page(lambda: s().locator("#ctry").input_value()), "India")
        self.assertEqual(self.on_page(lambda: s().locator("#src").input_value()), "job")
        self.assertEqual(self.on_page(lambda: s().locator("#avail").input_value()), "")
        self.assertEqual(self.on_page(lambda: s().locator("#natl").input_value()), "")
        self.assertEqual(FixtureHandler.posts, [])

    def test_icims_form_inside_an_iframe(self):
        c, item, run = self.serve("icims", "job.html")
        by = self.by(run)
        self.assertEqual((by["First Name"]["status"], by["First Name"]["value"]), ("filled", "Test"))
        self.assertEqual(by["Country"]["value"], "India")
        drv = self.svc.run.driver
        self.assertIsNotNone(drv._sc)                                                                              # the driver worked inside the frame
        s = self.sc()
        self.assertEqual(self.on_page(lambda: s().locator("#PersonProfileFields\\.FirstName").input_value()), "Test")
        self.assertEqual(self.on_page(lambda: s().locator("#PersonProfileFields\\.Country").input_value()), "IN")
        self.assertEqual(self.on_page(lambda: s().locator("#PersonProfileFields\\.Sal").input_value()), "")
        run = self.next(c, item, run)                                                                              # Next is clicked inside the frame
        self.assertEqual(run["submit_label"], "Submit")
        self.assertTrue(run["can_submit"])
        self.assertEqual(FixtureHandler.posts, [])


if __name__ == "__main__":
    unittest.main()
