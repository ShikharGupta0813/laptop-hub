"""Tests for Workday form filling (hub/workday.py inside hub/formfill.py). Invented data only; the pages are static fixtures in
tests/fixtures/workday built from Workday's known markup (data-automation-id), never fetched from a real tenant. The mapper is tested as
pure functions, the flow with a FAKE browser that models the widgets, and one run in a real headless browser (skipped when Playwright
or a Chromium is missing) that serves the fixtures from 127.0.0.1 with a tiny script imitating the dropdown and picker widgets."""
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from hub import formfill as ff, workday as wd
from tests.test_formfill import PNG, PUBLIC, Base, browser_options

FIX = Path(__file__).parent / "fixtures" / "workday"
HOST = "https://acme.wd5.myworkdayjobs.com/Careers/job/Pune/Python-Developer_R100/apply"

PAGES = {p.name: p.read_text(encoding="utf-8") for p in FIX.glob("*.html")}
STD = {"Notice period": "2 weeks", "Country": "India", "State": "Maharashtra", "Postal Code": "411001", "Address Line 1": "12 Test Lane",
       "Location": "Pune, India", "Phone Device Type": "Mobile", "Country Phone Code": "India (+91)", "Phone": "+91 5550 0100",
       "Email": "me@example.test"}
EXPERIENCE = [
    {"title": "Developer", "company": "ExampleCo", "period": "Mar 2021 - Present",
     "bullets": ["Built a Python API used by 3 teams", "Wrote the release checklist"]},
    {"title": "Intern", "company": "OldCo", "period": "Jun 2019 - Dec 2020", "bullets": ["Fixed small bugs"]},
]
EDUCATION = "BSc in Computer Science, Example University, 2016-2020"


def parsed(name, html=None):
    return wd.parse(ff.parse_page(html if html is not None else PAGES[name]), HOST)


# ---- the pure reading --------------------------------------------------------------------------------------------

class DetectTest(unittest.TestCase):
    def test_host_or_markers_turn_workday_on(self):
        for url in ("https://acme.wd5.myworkdayjobs.com/x", "https://acme.myworkdaysite.com/recruiting/acme/x", "https://wd3.myworkday.com/acme/d/x"):
            self.assertTrue(wd.applies(url, ""), url)
        self.assertTrue(wd.applies("https://careers.example.test/apply", PAGES["my-information.html"]))     # a custom domain, Workday markup
        self.assertFalse(wd.applies("https://jobs.example.test/apply", "<form><input name=a></form>"))
        self.assertFalse(wd.applies("https://notmyworkdayjobs.com.evil.test/x", ""))

    def test_generic_digest_is_unchanged_without_extra(self):
        fields = [{"idx": 0, "type": "text", "name": "a", "label": "A", "visible": True}]
        a = ff.digest_of("https://x.test/p", fields, {0: "v"}, None)
        self.assertEqual(a, ff.digest_of("https://x.test/p", fields, {0: "v"}, None, None))
        self.assertEqual(a, ff.digest_of("https://x.test/p", fields, {0: "v"}, None, {}))
        self.assertNotEqual(a, ff.digest_of("https://x.test/p", fields, {0: "v"}, None, {"b1": "India"}))


class ParseTest(unittest.TestCase):
    def test_step_titles_and_numbers_come_from_the_progress_bar(self):
        got = [(parsed(n)["step"]["title"], parsed(n)["step"]["current"], parsed(n)["step"]["total"]) for n in
               ("my-information.html", "my-experience.html", "application-questions.html", "voluntary-disclosures.html", "review.html")]
        self.assertEqual(got, [("My Information", 1, 5), ("My Experience", 2, 5), ("Application Questions", 3, 5), ("Voluntary Disclosures", 4, 5), ("Review", 5, 5)])

    def test_widgets_labels_and_required_stars(self):
        by = {f["label"]: f for f in parsed("my-information.html")["fields"]}
        kinds = {k: v["wd"]["widget"] for k, v in by.items()}
        self.assertEqual(kinds["How Did You Hear About Us?"], "prompt")
        self.assertEqual(kinds["Country"], "listbox")
        self.assertEqual(kinds["State"], "listbox")
        self.assertEqual(kinds["Country Phone Code"], "prompt")
        self.assertEqual(kinds["First Name"], "text")
        self.assertTrue(by["First Name"]["required"])
        self.assertFalse(by["Phone Extension"]["required"])
        self.assertNotIn("*", " ".join(by))
        self.assertIsNotNone(by["Country"]["wd"]["btn"])

    def test_sections_entries_dates_and_the_resume_box(self):
        w = parsed("my-experience-prefilled.html")
        self.assertEqual(sorted(w["sections"]), ["education", "work"])
        work, edu = w["sections"]["work"], w["sections"]["education"]
        self.assertEqual([e["key"] for e in work["entries"]], ["workExperience-1"])
        self.assertEqual(sorted(work["entries"][0]["fields"]), ["company", "current", "desc", "location", "start", "title"])
        self.assertEqual(sorted(work["entries"][0]["fields"]["start"]["wd"]["parts"]), ["month", "year"])
        self.assertEqual(sorted(edu["entries"][0]["fields"]["end"]["wd"]["parts"]), ["year"])
        self.assertIsNotNone(work["add"])
        self.assertIsNotNone(edu["add"])
        by = {f["label"]: f for f in w["fields"]}
        self.assertEqual(by["Resume/CV"]["type"], "file")
        self.assertEqual(by["Type to Add Skills"]["wd"]["section"], "other")
        self.assertEqual(len(parsed("my-experience.html")["sections"]["work"]["entries"]), 0)

    def test_templates_are_not_part_of_the_page(self):
        self.assertEqual([f for f in parsed("my-experience.html")["fields"] if f["wd"]["section"] == "work"], [])

    def test_review_submit_is_submit_not_next(self):
        c = parsed("review.html")["controls"]
        self.assertEqual((c["submit"]["text"], c["next"]), ("Submit", None))
        c = parsed("my-information.html")["controls"]
        self.assertEqual((c["next"]["text"], c["submit"]), ("Save and Continue", None))

    def test_widget_buttons_are_never_controls(self):
        for name in PAGES:
            c = parsed(name)["controls"]
            for b in (c["submit"], c["next"]):
                self.assertTrue(b is None or b["text"] in ("Save and Continue", "Submit"), (name, b))

    def test_never_step_and_sign_in_wall(self):
        self.assertTrue(parsed("voluntary-disclosures.html")["never"])
        self.assertFalse(parsed("my-information.html")["never"])
        w = parsed("sign-in.html")
        self.assertTrue(w["login"])
        kinds = [b["kind"] for b in wd.blockers(w, [])]
        self.assertEqual(kinds, ["login_required"])

    def test_stray_inputs_keep_the_generic_mapping(self):
        w = parsed("x.html", "<div data-automation-id='applyFlowPage'><label>Notice period <input name=n></label></div>")
        self.assertEqual([(f["label"], f["wd"]["widget"]) for f in w["fields"]], [("Notice period", "stray")])


class FactsTest(unittest.TestCase):
    def test_periods(self):
        self.assertEqual(wd.parse_period("Jan 2020 - Present"), {"start": (1, 2020), "end": "present"})
        self.assertEqual(wd.parse_period("March 2019 to Dec 2022"), {"start": (3, 2019), "end": (12, 2022)})
        self.assertEqual(wd.parse_period("2018 - 2021"), {"start": (None, 2018), "end": (None, 2021)})
        self.assertEqual(wd.parse_period("06/2019 – 12/2020"), {"start": (6, 2019), "end": (12, 2020)})
        self.assertEqual(wd.parse_period("2020"), {"start": (None, 2020), "end": None})
        self.assertEqual(wd.parse_period("")["start"], None)

    def test_education_lines(self):
        e = wd.parse_education(EDUCATION)
        self.assertEqual((e["school"], e["degree"], e["study"], e["start"], e["end"]), ("Example University", "BSc", "Computer Science", 2016, 2020))
        e = wd.parse_education("Example Institute of Technology, 2019")
        self.assertEqual((e["school"], e["degree"], e["start"], e["end"]), ("Example Institute of Technology", "", None, 2019))
        self.assertEqual(wd.parse_education("Self taught")["school"], "")

    def test_source_choice_prefers_job_board_then_company_website(self):
        prefs = wd.source_prefs({"source": "adzuna"})
        self.assertEqual(wd._pick(prefs, ["Social Media", "Company Website", "Job Board"]), "Job Board")
        self.assertEqual(wd._pick(prefs, ["Social Media", "Company Website", "Employee Referral"]), "Company Website")
        self.assertEqual(wd._pick(prefs, ["Social Media", "Careers Page"]), "Careers Page")
        self.assertIsNone(wd._pick(prefs, ["Social Media", "Employee Referral", "Other"]))
        self.assertEqual(wd._pick(prefs, ["Job Boards", "Adzuna"]), "Adzuna")             # the posting's own source, when listed exactly
        self.assertEqual(wd._pick(wd.source_prefs({"source": "fake"}), ["Job Boards", "Other"]), "Job Boards")


# ---- a fake browser that models the widgets -------------------------------------------------------------------------------

def _walk(n):
    for c in n.children:
        if isinstance(c, str) or c.tag in ff.SKIP:
            continue
        yield c
        yield from _walk(c)


class WDFake(ff.Driver):
    """Pages are the fixture strings. It keeps what was typed/picked by element id, adds entries when Add is clicked, and moves to the
    next page when the footer button is clicked. It records every call so tests can assert what was and was not touched."""
    instances: list = []

    def __init__(self, start="my-information.html", **kw):
        self.name, self.start = start, start
        self.vals, self.lists, self.chips, self.calls = {}, {}, {}, []
        self.added = {"work": 0, "education": 0}
        self.submitted = 0
        self.shots = 0
        self.closed = False
        self.last_options: list[str] = []
        WDFake.instances.append(self)

    # -- the page -------------------------------------------------------------------------------------------------------
    def base(self):
        return PAGES[self.name] if self.name in PAGES else "<html><body><h1>Thank you for applying!</h1><p>Your application has been received.</p></body></html>"

    def html(self):
        h = self.base()
        for kind, label in (("work", "Add Work Experience"), ("education", "Add Education")):
            tpl = re.search(rf'<template id="{kind}-entry">(.*?)</template>', h, re.S)
            if not tpl or not self.added[kind]:
                continue
            existing = len(re.findall(r'data-automation-id="%s-\d+"' % ("workExperience" if kind == "work" else "education"), h))
            new = "".join(tpl.group(1).replace("__N__", str(existing + i + 1)) for i in range(self.added[kind]))
            marker = f'</div><button type="button" data-automation-id="Add" aria-label="{label}"'
            h = h.replace(marker, new + marker, 1)
        return h

    def page(self):
        return ff.parse_page(self.html())

    def initial(self):
        out = {}
        for n in _walk(ff.build_tree(self.html())):
            if n.tag == "input" and n.get("id"):
                out[n.get("id")] = "1" if n.attrs.get("checked") is not None and n.get("type") == "checkbox" else ("0" if n.get("type") == "checkbox" else n.get("value"))
            elif n.tag == "textarea" and n.get("id"):
                out[n.get("id")] = "".join(c for c in n.children if isinstance(c, str))
        return out

    def node_with_id(self, id_):
        return next((n for n in _walk(ff.build_tree(self.html())) if n.get("id") == id_), None)

    def field(self, idx):
        return next(f for f in self.page()["fields"] if f["idx"] == idx)

    def button_node(self, idx):
        page = self.page()
        rev = {v: k for k, v in page["index"]["b"].items()}
        return next(n for n in _walk(page["root"]) if id(n) in page["index"]["b"] and page["index"]["b"][id(n)] == idx)

    # -- the Driver ------------------------------------------------------------------------------------------------------
    def open(self, url, headed):
        self.calls.append(("open", url, headed))
        return 200

    def url(self): return f"{HOST}/{self.name.replace('.html', '')}"

    def values(self):
        base = self.initial()
        return {f["idx"]: self.vals.get(f["id"], base.get(f["id"], "")) for f in self.page()["fields"] if f["id"]}

    def fill_text(self, idx, value): self.calls.append(("text", idx)); self.vals[self.field(idx)["id"]] = value
    def type_text(self, idx, value): self.calls.append(("type", idx, value)); self.vals[self.field(idx)["id"]] = value
    def select(self, idx, value): self.calls.append(("select", idx))
    def choose(self, idx): self.calls.append(("choose", idx)); self.vals[self.field(idx)["id"]] = "1"
    def upload(self, idx, path): self.calls.append(("upload", idx)); self.vals[self.field(idx)["id"]] = Path(path).name
    def settle(self): pass
    def blocked_navigation(self): return ""
    def screenshot(self): self.shots += 1; return PNG + str(self.shots).encode()
    def close(self): self.closed = True

    def click(self, idx):
        self.calls.append(("click", idx))
        node = self.button_node(idx)
        if node.get("data-submit"):
            self.submitted += 1
            self.name = "thanks"
        elif node.get("data-next"):
            self.name = node.get("data-next")
        elif node.get("data-automation-id") == "Add":
            self.added["work" if "Work" in node.get("aria-label") else "education"] += 1

    def open_list(self, btn_idx):
        self.calls.append(("open_list", btn_idx))
        return json.loads(self.button_node(btn_idx).get("data-options") or "[]")

    def pick_listed(self, btn_idx, text):
        self.calls.append(("pick", btn_idx, text))
        self.lists[self.button_node(btn_idx).get("id")] = text
        return text

    def close_list(self): self.calls.append(("close_list",))

    def search_options(self, idx, query):
        self.calls.append(("search", idx, query))
        node = self.node_with_id(self.field(idx)["id"])
        return [o for o in json.loads(node.get("data-options") or "[]") if query.lower() in o.lower()]

    def pick_searched(self, idx, text):
        self.calls.append(("pick_search", idx, text))
        self.chips.setdefault(self.field(idx)["id"], []).append(text)
        return ", ".join(self.chips[self.field(idx)["id"]])

    def wd_values(self):
        out = {}
        for f in wd.parse(self.page(), "")["fields"]:
            if f["wd"]["widget"] == "listbox":
                out[f"b{f['wd']['btn']}"] = self.lists.get(f["id"]) or ff._own_text(self.button_node(f["wd"]["btn"]))
            elif f["wd"]["widget"] == "prompt":
                out[f"p{f['idx']}"] = "|".join(self.chips.get(f["id"], []))
        return out

    def plain(self, kind):
        return [c for c in self.calls if c[0] == kind]


class WDBase(Base):
    def setUp(self):
        super().setUp()
        WDFake.instances = []
        self.profile = {**self.profile, "experience": EXPERIENCE, "education": EDUCATION, "standard_answers": dict(STD)}
        self.start_page = "my-information.html"
        self.item_url = HOST

    def drv(self):
        return WDFake.instances[0]

    def go(self, start=None):
        page = start or self.start_page
        self.build(driver_factory=lambda pd: WDFake(page))
        c = self.client()
        item = self.item(url=self.item_url)
        r = c.post(f"/api/applyq/{item['id']}/formfill/start", json={})
        self.assertEqual(r.status_code, 200, r.text)
        return c, item, r.json()

    def next(self, c, item, run):
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/next", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def by(run):
        return {f["label"]: f for f in run["fields"]}


class MyInformationTest(WDBase):
    def test_fills_dropdowns_pickers_and_text_and_reports_the_step(self):
        c, item, run = self.go()
        by = self.by(run)
        self.assertEqual(run["step"]["label"], "step 1 of 5 filled (My Information)")
        self.assertEqual(by["First Name"]["value"], "Test")
        self.assertEqual(by["Last Name"]["value"], "Person")
        self.assertEqual(by["Country"]["value"], "India")
        self.assertEqual(by["State"]["value"], "Maharashtra")
        self.assertEqual(by["City"]["value"], "Pune")
        self.assertEqual(by["Postal Code"]["value"], "411001")
        self.assertEqual(by["Address Line 1"]["value"], "12 Test Lane")
        self.assertEqual(by["Phone Device Type"]["value"], "Mobile")
        self.assertEqual(by["Country Phone Code"]["value"], "India (+91)")
        self.assertEqual(by["Phone Number"]["value"], "+91 5550 0100")
        self.assertEqual(by["Phone Extension"]["status"], "skipped")
        self.assertEqual(by["How Did You Hear About Us?"]["status"], "filled")
        d = self.drv()
        self.assertEqual(d.chips["source"], ["Job Board"])
        self.assertEqual(d.chips["pcode"], ["India (+91)"])
        self.assertEqual(d.lists["country"], "India")
        self.assertEqual(run["counts"]["needs_you"], 0)

    def test_nothing_is_clicked_while_filling_and_next_is_offered_not_taken(self):
        c, item, run = self.go()
        self.assertEqual(self.drv().plain("click"), [])                 # open_list clicks inside the driver; no button click from the flow
        self.assertTrue(run["can_next"] and not run["can_submit"])
        self.assertEqual(self.drv().name, "my-information.html")

    def test_a_missing_standard_answer_is_needs_you_not_a_guess(self):
        self.profile["standard_answers"].pop("State")
        self.profile["standard_answers"].pop("Country Phone Code")
        c, item, run = self.go()
        by = self.by(run)
        self.assertEqual(by["State"]["status"], "needs_you")
        self.assertIn("I do not have your state", by["State"]["reason"])
        self.assertEqual(by["Country Phone Code"]["status"], "needs_you")           # "Country" must not be reused for the phone code
        self.assertEqual(self.drv().chips.get("pcode"), None)

    def test_a_choice_that_is_not_listed_is_not_forced(self):
        self.profile["standard_answers"]["State"] = "Atlantis"
        c, item, run = self.go()
        by = self.by(run)
        self.assertEqual(by["State"]["status"], "needs_you")
        self.assertIn("clearly fits", by["State"]["reason"])
        self.assertIn(("close_list",), self.drv().calls)


class MyExperienceTest(WDBase):
    def reach(self):
        c, item, run = self.go()
        return c, item, self.next(c, item, run)

    def test_adds_one_entry_per_profile_role_and_school_and_fills_them(self):
        c, item, run = self.reach()
        d, by = self.drv(), self.by(run)
        self.assertEqual(d.added, {"work": 2, "education": 1})
        self.assertEqual((d.vals["we1-title"], d.vals["we1-company"]), ("Developer", "ExampleCo"))
        self.assertEqual((d.vals["we1-start-month"], d.vals["we1-start-year"]), ("03", "2021"))
        self.assertEqual(d.vals["we1-cur"], "1")                                     # the current job ticks "I currently work here"
        self.assertNotIn("we1-end-month", d.vals)                                    # and has no end date
        self.assertEqual((d.vals["we2-title"], d.vals["we2-company"]), ("Intern", "OldCo"))
        self.assertEqual((d.vals["we2-end-month"], d.vals["we2-end-year"]), ("12", "2020"))
        self.assertNotIn("we2-cur", d.vals)
        self.assertEqual(d.vals["we1-desc"], "- Built a Python API used by 3 teams\n- Wrote the release checklist")
        self.assertEqual(d.vals["ed1-school"], "Example University")
        self.assertEqual((d.vals["ed1-from-year"], d.vals["ed1-to-year"]), ("2016", "2020"))
        self.assertEqual(d.chips["ed1-study"], ["Computer Science"])
        self.assertEqual(by["Education 1: Degree"]["status"], "needs_you")           # "BSc" is not clearly "Bachelor's Degree"
        self.assertEqual(by["Work Experience 1: Job Title"]["value"], "Developer")
        self.assertEqual(by["Resume/CV"]["status"], "filled")
        self.assertTrue(d.plain("upload"))
        self.assertEqual(by["Type to Add Skills"]["status"], "skipped")
        self.assertEqual(run["step"]["label"], "step 2 of 5 filled (My Experience)")
        self.assertFalse(d.submitted)

    def test_no_entry_is_added_that_the_profile_does_not_have(self):
        self.profile["experience"] = EXPERIENCE[:1]
        self.profile["education"] = ""
        c, item, run = self.reach()
        self.assertEqual(self.drv().added, {"work": 1, "education": 0})
        self.assertEqual(self.by(run)["Education"]["status"], "skipped")

    def test_hidden_facts_are_not_used(self):
        from hub import factbase
        roles = [f for f in factbase.FactBase.build(self.profile, None, this_year=2026).facts if f["type"] == "role"]
        factbase.save(self.ctx, {"confirmed": [], "hidden": [roles[1]["id"]], "aliases": {}, "prefs": dict(factbase.DEFAULT_PREFS)})
        c, item, run = self.reach()
        d = self.drv()
        self.assertEqual(d.added["work"], 1)
        self.assertNotIn("we2-title", d.vals)

    def test_entries_already_on_the_page_are_matched_not_duplicated(self):
        self.start_page = "my-experience-prefilled.html"
        c, item, run = self.go()
        d, by = self.drv(), self.by(run)
        self.assertEqual(d.added, {"work": 1, "education": 0})                       # ExampleCo is already there; only OldCo is new
        self.assertNotIn("we1-title", d.vals)                                        # nothing typed over the existing entry
        self.assertNotIn("we1-company", d.vals)
        self.assertEqual(d.vals["we2-company"], "OldCo")
        self.assertEqual(by["Work Experience 1: Job Title"]["status"], "skipped")
        self.assertIn("Already", by["Work Experience 1: Job Title"]["reason"])
        self.assertEqual(by["Education 1: School or University"]["status"], "skipped")
        self.assertEqual(len(re.findall(r'data-automation-id="workExperience-\d+"', d.html())), 2)
        self.assertEqual(len(re.findall(r'data-automation-id="education-\d+"', d.html())), 1)

    def test_a_year_only_work_date_is_not_made_up(self):
        self.profile["experience"] = [{"title": "Developer", "company": "ExampleCo", "period": "2018 - 2021", "bullets": []}]
        c, item, run = self.reach()
        by = self.by(run)
        self.assertEqual(by["Work Experience 1: From"]["status"], "needs_you")
        self.assertNotIn("we1-start-year", self.drv().vals)

    def test_a_page_that_loses_a_typed_date_is_reported(self):
        class Forgetful(WDFake):
            def type_text(self, idx, value):
                self.calls.append(("type", idx, value))
        self.build(driver_factory=lambda pd: Forgetful("my-experience.html"))
        c = self.client()
        item = self.item(url=self.item_url)
        run = c.post(f"/api/applyq/{item['id']}/formfill/start", json={}).json()
        self.assertEqual(self.by(run)["Work Experience 1: From"]["status"], "needs_you")


class OtherStepsTest(WDBase):
    def test_voluntary_step_is_left_untouched(self):
        c, item, run = self.go("voluntary-disclosures.html")
        d = self.drv()
        self.assertEqual({c[0] for c in d.calls}, {"open"})                          # no typing, no list, no search, no tick, no click
        self.assertEqual(run["counts"]["filled"], 0)
        self.assertTrue(all(f["status"] == "needs_you" for f in run["fields"]))
        self.assertIn("Voluntary Disclosures (whole step)", self.by(run))
        self.assertEqual(run["step"]["label"], "step 4 of 5 filled (Voluntary Disclosures)")
        self.assertTrue(run["can_next"])                                             # moving on is still the owner's tap

    def test_application_questions(self):
        c, item, run = self.go("application-questions.html")
        by, d = self.by(run), self.drv()
        self.assertEqual(by["How did you hear about this opportunity?"]["value"], "Job Board")
        self.assertEqual(by["What is your notice period?"]["value"], "2 weeks")
        for q in ("Are you legally authorized to work in the country in which this job is located?",
                  "Will you now or in the future require sponsorship for employment visa status?"):
            self.assertEqual(by[q]["status"], "needs_you")
            self.assertEqual(by[q]["value"], "")
        self.assertEqual(by["Do you have any relatives who work here?"]["status"], "needs_you")        # required and unknown
        self.assertEqual(by["Preferred Work Location"]["status"], "needs_you")
        self.assertEqual(d.plain("choose"), [])                                      # no radio was ticked

    def test_company_website_when_there_is_no_job_board_option(self):
        PAGES["aq2.html"] = PAGES["application-questions.html"].replace("Job Board", "Company Website")
        self.addCleanup(PAGES.pop, "aq2.html", None)
        c, item, run = self.go("aq2.html")
        self.assertEqual(self.drv().lists["howheard"], "Company Website")

    def test_neither_option_means_needs_you(self):
        PAGES["aq3.html"] = PAGES["application-questions.html"].replace("Job Board", "Newsletter").replace("Careers Page", "Blog")
        self.addCleanup(PAGES.pop, "aq3.html", None)
        c, item, run = self.go("aq3.html")
        self.assertEqual(self.by(run)["How did you hear about this opportunity?"]["status"], "needs_you")
        self.assertNotIn("howheard", self.drv().lists)

    def test_sign_in_wall_is_a_login_blocker_and_nothing_is_filled(self):
        c, item, run = self.go("sign-in.html")
        self.assertEqual([b["kind"] for b in run["blockers"]], ["login_required"])
        self.assertEqual(run["fields"], [])
        self.assertEqual({c[0] for c in self.drv().calls}, {"open"})
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post(f"/api/applyq/{item['id']}/formfill/login").status_code, 200)     # the existing login-window flow

    def test_review_step_offers_submit_but_never_clicks_it(self):
        c, item, run = self.go("review.html")
        self.assertEqual(run["submit_label"], "Submit")
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(run["step"]["label"], "step 5 of 5 (Review)")
        self.assertEqual(self.drv().submitted, 0)
        self.assertEqual(self.drv().plain("click"), [])

    def test_the_whole_walk_moves_only_on_the_owners_confirm_and_submit_needs_look_and_digest(self):
        c, item, run = self.go()
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/next", json={})
        self.assertEqual(r.status_code, 400)                                         # no confirm, no move
        self.assertEqual(self.drv().name, "my-information.html")
        for expected in ("my-experience.html", "application-questions.html", "voluntary-disclosures.html", "review.html"):
            run = self.next(c, item, run)
            self.assertEqual(self.drv().name, expected)
        self.assertEqual(self.drv().submitted, 0)
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 409)                                         # the screenshot was not looked at
        self.assertEqual(self.drv().submitted, 0)
        c.get(run["screenshot_url"])
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.drv().submitted, 1)
        self.assertEqual(r.json()["result"], "applied")


class DigestTest(WDBase):
    def test_digest_changes_when_a_dropdown_or_picker_changes(self):
        c, item, run = self.go()
        d = self.drv()
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh")
        self.assertEqual(r.json()["digest"], run["digest"])                           # nothing changed: same digest
        d.lists["state"] = "Karnataka"                                               # the owner changes State in the window
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh")
        self.assertNotEqual(r.json()["digest"], run["digest"])
        d.chips["source"] = ["Social Media"]
        again = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh").json()["digest"]
        self.assertNotEqual(again, r.json()["digest"])

    def test_a_widget_change_blocks_submit(self):
        # a review page that also carries a dropdown: changing it after the screenshot must stop the click
        PAGES["review-widget.html"] = PAGES["review.html"].replace('<div data-automation-id="reviewPage">', listbox_html() + '<div data-automation-id="reviewPage">')
        self.addCleanup(PAGES.pop, "review-widget.html", None)
        c, item, run = self.go("review-widget.html")
        c.get(run["screenshot_url"])
        self.drv().lists["rw"] = "Option B"
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": run["digest"]})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json().get("changed"))
        self.assertEqual(self.drv().submitted, 0)


def listbox_html():
    return ('<div data-automation-id="formField-rw"><label id="rw-label" for="rw">Pick one</label><button type="button" id="rw" aria-haspopup="listbox" '
            'data-automation-id="rwDropdown" data-options="[&quot;Option A&quot;, &quot;Option B&quot;]">Select One</button></div>')


class GenericPathUntouchedTest(unittest.TestCase):
    def test_other_sites_never_reach_the_workday_reader(self):
        from tests.test_formfill import FORM
        self.assertFalse(wd.applies("https://jobs.example.test/apply", FORM))


# ---- a real browser on the fixtures ---------------------------------------------------------------------------------------

class FixtureHandler(BaseHTTPRequestHandler):
    posts: list = []

    def log_message(self, *a): pass

    def do_GET(self):
        name = self.path.split("?")[0].lstrip("/") or "my-information.html"
        path = FIX / name
        if not path.is_file() or path.parent != FIX:
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
class RealBrowserWorkdayTest(WDBase):
    def serve(self, page):
        FixtureHandler.posts = []
        server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
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

    def test_walks_the_fixture_steps_in_a_real_browser(self):
        c, item, run = self.serve("my-information.html")
        by = self.by(run)
        self.assertEqual(run["step"]["label"], "step 1 of 5 filled (My Information)")
        for label, want in (("Country", "India"), ("State", "Maharashtra"), ("Phone Device Type", "Mobile"), ("Country Phone Code", "India (+91)"),
                            ("First Name", "Test"), ("City", "Pune"), ("Phone Number", "+91 5550 0100")):
            self.assertEqual((label, by[label]["status"], by[label]["value"]), (label, "filled", want))
        self.assertEqual(by["How Did You Hear About Us?"]["status"], "filled")
        drv = self.svc.run.driver
        shown = self.on_page(drv.wd_values)
        self.assertIn("Maharashtra", shown.values())
        self.assertIn("Job Board", shown.values())
        # the digest moves when a dropdown is changed by hand
        before = run["digest"]
        state_btn = int(next(k for k, v in shown.items() if v == "Maharashtra")[1:])
        self.on_page(lambda: (drv.open_list(state_btn), drv.pick_listed(state_btn, "Karnataka")))
        after = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/refresh").json()["digest"]
        self.assertNotEqual(before, after)
        r = c.post(f"/api/applyq/{item['id']}/formfill/{run['run']}/submit", json={"confirm": True, "field_digest": before})
        self.assertEqual(r.status_code, 409)

        run = self.next(c, item, run)                                                # My Experience
        by = self.by(run)
        self.assertEqual(run["step"]["label"], "step 2 of 5 filled (My Experience)")
        self.assertEqual(by["Work Experience 1: Job Title"]["value"], "Developer")
        self.assertEqual(by["Work Experience 2: Company"]["value"], "OldCo")
        self.assertEqual(by["Education 1: School or University"]["value"], "Example University")
        self.assertEqual(by["Resume/CV"]["status"], "filled")
        counts = self.on_page(lambda: [drv._page.locator(f'[data-automation-id="{a}"]').count() for a in
                                       ("workExperience-1", "workExperience-2", "workExperience-3", "education-1", "education-2")])
        self.assertEqual(counts, [1, 1, 0, 1, 0])                                    # one entry per role and school, none extra
        self.assertEqual(self.on_page(lambda: drv._page.locator("#we1-title").input_value()), "Developer")
        self.assertEqual(self.on_page(lambda: drv._page.locator("#we1-cur").is_checked()), True)
        self.assertEqual(self.on_page(lambda: drv._page.locator("#we2-start-month").input_value()), "06")

        run = self.next(c, item, run)                                                # Application Questions
        by = self.by(run)
        self.assertEqual(by["How did you hear about this opportunity?"]["value"], "Job Board")
        self.assertEqual(by["Are you legally authorized to work in the country in which this job is located?"]["status"], "needs_you")
        self.assertFalse(self.on_page(lambda: drv._page.locator('input[type="radio"]:checked').count()))

        run = self.next(c, item, run)                                                # Voluntary Disclosures: untouched
        self.assertEqual(run["counts"]["filled"], 0)
        untouched = self.on_page(lambda: [drv._page.locator('button[aria-haspopup="listbox"]').first.inner_text(),
                                          drv._page.locator('[data-automation-id="selectedItem"]').count(),
                                          drv._page.locator("#terms").is_checked(), drv._page.locator("#sigName").input_value()])
        self.assertEqual(untouched, ["Select One", 0, False, ""])

        run = self.next(c, item, run)                                                # Review
        self.assertEqual(run["submit_label"], "Submit")
        self.assertTrue(run["can_submit"] and not run["can_next"])
        self.assertEqual(FixtureHandler.posts, [])                                   # Submit was never pressed

    def test_existing_entries_are_not_duplicated_in_a_real_browser(self):
        c, item, run = self.serve("my-experience-prefilled.html")
        drv = self.svc.run.driver
        counts = self.on_page(lambda: [drv._page.locator(f'[data-automation-id="{a}"]').count() for a in
                                       ("workExperience-1", "workExperience-2", "workExperience-3", "education-1", "education-2")])
        self.assertEqual(counts, [1, 1, 0, 1, 0])                                    # ExampleCo and the school were there; only OldCo was added
        self.assertEqual(self.on_page(lambda: drv._page.locator("#we1-title").input_value()), "Developer")
        self.assertEqual(self.on_page(lambda: drv._page.locator("#we1-company").input_value()), "ExampleCo")
        self.assertEqual(self.on_page(lambda: drv._page.locator("#we2-company").input_value()), "OldCo")
        self.assertEqual(FixtureHandler.posts, [])


if __name__ == "__main__":
    unittest.main()
