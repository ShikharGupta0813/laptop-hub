"""hub/oppsources.py. Fakes only: a scripted web (httpx.MockTransport), a fake resolver, a fake finder and a fake mailbox."""
import asyncio
import json
import tempfile
import unittest

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import oppsources as S
from hub.assistant import TOOL_GROUPS
from hub.common import Ctx
from hub.provlist import MemorySecrets
from tests.helpers import make_config

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731
NOW = 1_794_657_600.0          # 2026-11-14 12:00 UTC
ORIG_ANNOUNCE = S.OppSources._announce_early


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class Net:
    """url -> (status, content_type, body[, headers]) or an exception. Records every request (url, headers)."""

    def __init__(self):
        self.routes, self.requests, self.calls = {}, [], []

    def handler(self, request: httpx.Request):
        self.requests.append((str(request.url), dict(request.headers)))
        self.calls.append((request.method, str(request.url), request.content))
        item = self.routes.get(str(request.url))
        if item is None:
            return httpx.Response(404)
        if isinstance(item, Exception):
            raise item
        status, ctype, body, *rest = item
        headers = {"content-type": ctype, **(rest[0] if rest else {})}
        if status in (301, 302):
            headers["location"], body = body, ""
        return httpx.Response(status, headers=headers, content=body if isinstance(body, bytes) else body.encode())

    def urls(self):
        return [u for u, _ in self.requests]


class FakeFinder:
    def __init__(self):
        self.calls = []

    def ingest(self, rows, *, source_name, source_id, kind, sender="", contact=""):
        self.calls.append({"rows": rows, "source": source_name, "source_id": source_id, "kind": kind})
        return {"new": len(rows), "queued": 0, "blocked": 0, "seen": 0, "low": 0}

    def find(self, fid):
        return None


class FakeMail:
    def __init__(self, items):
        self.items, self.destinations = items, []

    def recent(self, account, hours=24, max_results=30):
        return self.items

    def get_message(self, account, mid, destination="local"):
        self.destinations.append(destination)
        return next(i for i in self.items if i["id"] == mid)


class Base(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="oppsrc-test-"))
        self.config.oppsources_enabled = True
        self.notes = []
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: None, notify=lambda *a, **k: self.notes.append((a, k)))
        self.net, self.finder, self.sleeps = Net(), FakeFinder(), []
        self.now = NOW

        async def nosleep(sec):
            self.sleeps.append(sec)
        fetcher = S.Fetch(resolver=PUBLIC, transport=httpx.MockTransport(self.net.handler), clock=lambda: self.now, sleep=nosleep)
        self.mail = FakeMail([])
        self.secrets = MemorySecrets()        # never the real OS keyring in tests
        self.eng = S.engine_for(self.ctx, fetcher=fetcher, clock=lambda: self.now, finder=self.finder, mail=self.mail, secrets=self.secrets)
        app = FastAPI()
        app.include_router(S.make_router(self.ctx))
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)
        self.early = []
        S.OppSources._announce_early = lambda eng, it: self.early.append(it["title"])

    def tearDown(self):
        S.OppSources._announce_early = ORIG_ANNOUNCE

    def run_async(self, coro):
        return asyncio.run(coro)

    def add(self, **kw):
        body = {"label": "Src", "adapter": "rss", "kind": "job", "settings": {"url": "https://example.com/feed.xml"}}
        body.update(kw)
        return self.eng.add_source(body)

    def run_src(self, src, **kw):
        return self.run_async(self.eng.run_source(src, **kw))

    def all_items(self):
        return list(self.eng._items().values())


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Acme: Senior Python Engineer</title><link>https://example.com/jobs/1?utm_source=x</link>
<description>&lt;p&gt;Fully remote, US only. $90-120k per year. Contract.&lt;/p&gt;</description><pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Bobs: Data entry</title><link>https://example.com/jobs/2</link><description>Pay a registration fee to start. Earn money.</description></item>
</channel></rss>"""


class Sources(Base):
    def test_list_starts_empty_and_nothing_is_enabled_by_default(self):
        self.assertEqual(self.client.get("/api/oppsources/sources").json()["sources"], [])
        self.assertGreater(len(self.client.get("/api/oppsources/presets").json()["presets"]), 20)
        fresh, _ = make_config(data_dir=tempfile.mkdtemp())
        self.assertFalse(fresh.oppsources_enabled)

    def test_crud_and_validation(self):
        r = self.client.post("/api/oppsources/sources", json={"label": "My feed", "adapter": "rss", "kind": "job", "settings": {"url": "https://example.com/f.xml"}})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["id"]
        r = self.client.put(f"/api/oppsources/sources/{sid}", json={"enabled": False, "label": "Renamed", "interval_min": 30})
        self.assertEqual((r.json()["label"], r.json()["enabled"], r.json()["interval_min"]), ("Renamed", False, 30))
        bad = [{"label": "", "adapter": "rss", "settings": {"url": "https://a.example.com"}},
               {"label": "x", "adapter": "nope", "settings": {}},
               {"label": "x", "adapter": "rss", "settings": {"url": "ftp://a.example.com"}},
               {"label": "x", "adapter": "rss", "settings": {"url": "https://u:p@a.example.com"}},
               {"label": "x", "adapter": "rss", "settings": {"url": "http://127.0.0.1/feed"}},
               {"label": "x", "adapter": "rss", "settings": {"url": "http://localhost/feed"}},
               {"label": "x", "adapter": "json", "settings": {"url": "https://a.example.com", "fields": {}}},
               {"label": "x", "adapter": "html", "settings": {"url": "https://a.example.com", "item_selector": "li:nth-child(2)"}},
               {"label": "x", "adapter": "greenhouse", "settings": {"board": "bad board!"}},
               {"label": "x", "adapter": "email", "settings": {}},
               {"label": "x", "adapter": "rss", "kind": "weird", "settings": {"url": "https://a.example.com"}},
               {"label": "x", "adapter": "rss", "interval_min": 1, "settings": {"url": "https://a.example.com"}},
               {"label": "x", "adapter": "email", "watch": True, "settings": {"senders": ["a@b.c"]}}]
        for body in bad:
            self.assertEqual(self.client.post("/api/oppsources/sources", json=body).status_code, 400, body)
        self.assertEqual(self.client.delete(f"/api/oppsources/sources/{sid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/oppsources/sources/{sid}").status_code, 404)
        self.assertEqual(self.client.put("/api/oppsources/sources/..%2Fx", json={}).status_code, 404)

    def test_routes_need_login(self):
        for method, path in (("get", "/api/oppsources"), ("get", "/api/oppsources/presets"), ("post", "/api/oppsources/run"),
                             ("get", "/api/oppsources/items"), ("post", "/api/oppsources/watch")):
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)


class Presets(Base):
    def test_verification_is_honest(self):
        by = {p["id"]: p for p in self.client.get("/api/oppsources/presets").json()["presets"]}
        # checked live on 2026-11-15 (see docs/api/oppsources.md); workable stayed unverified (empty job lists) and platforms are never fetched
        # 2026-11-21: Personio and Breezy answered live; Workday redirected to its maintenance page; Adzuna, Jooble and Findwork need a key (not called)
        for pid, p in by.items():
            if pid in ("workable", "workday", "adzuna", "jooble", "findwork") or p["adapter"] == "email":
                self.assertEqual(p["verification"], "unverified", pid)
                self.assertIn("UNVERIFIED", p["note"]) if pid != "workable" and p["adapter"] != "email" else None
            elif pid in ("personio", "breezy"):
                self.assertEqual(p["verification"], "verified 2026-11-21", pid)
            else:
                self.assertEqual(p["verification"], "verified 2026-11-15", pid)
            self.assertTrue(p["note"], pid)
        for want in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "teamtailor", "remoteok", "wwr", "remotive",
                     "himalayas", "jobicy", "arbeitnow", "workingnomads", "wwr-programming", "jobspresso", "nodesk", "python-jobs",
                     "hn-hiring", "upwork", "fiverr", "freelancer", "toptal", "contra", "data-annotation"):
            self.assertIn(want, by)

    def test_add_a_whole_group_in_one_tap_and_only_once(self):
        r = self.client.post("/api/oppsources/presets/add-group", json={"group": "freelance"})
        self.assertEqual(r.status_code, 200, r.text)
        added = r.json()["added"]
        self.assertGreaterEqual(len(added), 10)
        for want in ("upwork", "guru", "braintrust", "truelancer"):
            self.assertIn(want, added)
        rows = self.client.get("/api/oppsources/sources").json()["sources"]
        self.assertEqual(len(rows), len(added))
        for row in rows:
            self.assertEqual(row["adapter"], "email")
        again = self.client.post("/api/oppsources/presets/add-group", json={"group": "freelance"}).json()
        self.assertEqual(again["added"], [])
        self.assertEqual(len(again["already_there"]), len(added))
        task = self.client.post("/api/oppsources/presets/add-group", json={"group": "task"}).json()
        self.assertIn("prolific", task["added"])
        self.assertEqual(self.client.post("/api/oppsources/presets/add-group", json={"group": "ats"}).status_code, 400)

    def test_platforms_are_email_or_link_only_and_never_auto_submit(self):
        for pid in ("upwork", "fiverr", "freelancer", "toptal", "contra", "data-annotation", "outlier", "alignerr", "mercor"):
            r = self.client.post(f"/api/oppsources/presets/{pid}/add", json={})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["adapter"], "email", pid)
        r = self.client.post("/api/oppsources/presets/upwork/add", json={"mode": "open-link", "label": "Upwork links"})
        self.assertEqual(r.json()["adapter"], "link")
        self.assertEqual(self.client.post("/api/oppsources/presets/upwork/add", json={"mode": "scrape"}).status_code, 400)
        for p in self.client.get("/api/oppsources/presets").json()["presets"]:
            if p["group"] in ("freelance", "task"):
                self.assertFalse(p["auto_submit"])
                self.assertTrue(p["never_automated"])
                self.assertIn("Auto-submit is OFF", p["note"])
        self.run_async(self.eng.run_now())                       # none of them is ever fetched
        self.assertEqual(self.net.requests, [])

    def test_ats_preset_needs_a_board(self):
        self.assertEqual(self.client.post("/api/oppsources/presets/greenhouse/add", json={}).status_code, 400)
        r = self.client.post("/api/oppsources/presets/lever/add", json={"board": "acme", "region": "eu"})
        self.assertEqual(r.json()["settings"]["region"], "eu")
        self.assertEqual(S.ats_url("lever", r.json()["settings"]), "https://api.eu.lever.co/v0/postings/acme?mode=json")
        self.assertEqual(self.client.post("/api/oppsources/presets/nope/add", json={}).status_code, 404)


class Adapters(Base):
    def test_rss_normalises_everything(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS)
        res = self.run_src(self.add())
        self.assertEqual(res["state"], "ok", res)
        items = {i["title"]: i for i in self.all_items()}
        ok = items["Acme: Senior Python Engineer"]
        self.assertEqual(ok["work_mode"], "remote")
        self.assertEqual(ok["geo"]["regions"], ["US"])
        self.assertEqual(ok["employment"], "contract")
        self.assertEqual((ok["pay"]["min"], ok["pay"]["max"], ok["pay"]["unit"]), (90000, 120000, "year"))
        self.assertEqual(ok["apply_method"], "external_manual")
        self.assertGreater(ok["posted_ts"], 0)
        self.assertFalse(ok["auto_submit"])
        bad = items["Bobs: Data entry"]
        self.assertEqual(bad["status"], "blocked")
        handed = [r["title"] for c in self.finder.calls for r in c["rows"]]
        self.assertIn("Acme: Senior Python Engineer", handed)
        self.assertNotIn("Bobs: Data entry", handed)
        self.assertIn("Work mode: remote (US)", self.finder.calls[0]["rows"][0]["description"])

    def test_rss_split_company(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS)
        self.run_src(self.add(settings={"url": "https://example.com/feed.xml", "split_company": True}))
        t = {i["title"]: i for i in self.all_items()}
        self.assertEqual(t["Senior Python Engineer"]["company"], "Acme")

    def test_json_mapping_remoteok_style(self):
        data = [{"legal": "terms"}, {"position": "Go Engineer", "company": "Gopher", "url": "https://remoteok.com/r/9", "tags": ["go", "backend"],
                                     "description": "<p>Build things</p>", "location": "Worldwide", "epoch": 1_794_600_000, "salary_min": 90000, "salary_max": 120000}]
        self.net.routes["https://remoteok.com/api"] = (200, "application/json", json.dumps(data))
        r = self.client.post("/api/oppsources/presets/remoteok/add", json={})
        self.assertEqual(r.status_code, 200, r.text)
        res = self.run_src(r.json())
        self.assertEqual(res["found"], 1, res)
        it = self.all_items()[0]
        self.assertEqual((it["title"], it["company"], it["work_mode"], it["geo"]["worldwide"]), ("Go Engineer", "Gopher", "remote", True))
        self.assertEqual((it["pay"]["min"], it["pay"]["unit"], it["pay"]["currency"]), (90000, "year", "USD"))
        self.assertEqual(it["posted_ts"], 1_794_600_000)
        self.assertIn("Tags: go, backend", it["description"])

    def test_json_custom_mapping_nested_paths_and_template(self):
        data = {"data": {"rows": [{"id": 7, "info": {"name": "Rust Dev", "org": "Orc"}, "where": {"city": "Oslo"}, "remote": False, "pay": "NOK 600 per hour"}]}}
        self.net.routes["https://api.example.org/jobs"] = (200, "application/json", json.dumps(data))
        src = self.add(adapter="json", settings={"url": "https://api.example.org/jobs", "list_path": "data.rows", "url_template": "https://example.org/job/{id}",
                                                 "fields": {"title": "info.name", "company": "info.org", "id": "id", "location": "where.city", "remote": "remote", "pay_text": "pay"}})
        self.run_src(src)
        it = self.all_items()[0]
        self.assertEqual((it["title"], it["company"], it["url"], it["work_mode"]), ("Rust Dev", "Orc", "https://example.org/job/7", "onsite"))
        self.assertTrue(self.run_async(self.eng.test_source(src))["ok"])

    def test_json_wrong_path_is_a_clear_error(self):
        self.net.routes["https://api.example.org/jobs"] = (200, "application/json", "{}")
        res = self.run_src(self.add(adapter="json", settings={"url": "https://api.example.org/jobs", "list_path": "nope", "fields": {"title": "t"}}))
        self.assertEqual(res["state"], "error")
        self.assertIn("list", res["error"])

    def test_html_selectors_respect_robots(self):
        page = ("<ul><li class='j'><a href='/job/1'>Backend Engineer</a><span class='c'>Zed</span><span class='l'>Remote - EU</span></li>"
                "<li class='j'><a href='/job/2'>Designer</a></li></ul>")
        self.net.routes["https://careers.example.com/open"] = (200, "text/html", page)
        self.net.routes["https://careers.example.com/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
        src = self.add(adapter="html", settings={"url": "https://careers.example.com/open", "item_selector": "li.j",
                                                 "fields": {"title": "a", "url": "a@href", "company": ".c", "location": ".l"}})
        res = self.run_src(src)
        self.assertEqual(res["found"], 2, res)
        it = next(i for i in self.all_items() if i["title"] == "Backend Engineer")
        self.assertEqual((it["company"], it["url"], it["work_mode"], it["geo"]["regions"]), ("Zed", "https://careers.example.com/job/1", "remote", ["EU"]))
        self.net.requests.clear()
        self.net.routes["https://careers.example.com/robots.txt"] = (200, "text/plain", "User-agent: *\nDisallow: /\n")
        self.eng.fetcher._robots = {}
        res = self.run_src(src, force=True)
        self.assertEqual(res["state"], "manual_check")
        self.assertNotIn("https://careers.example.com/open", self.net.urls())

    def test_login_wall_and_block_are_manual_check(self):
        self.net.routes["https://example.com/feed.xml"] = (302, "text/html", "https://example.com/login")
        self.assertEqual(self.run_src(self.add())["state"], "manual_check")
        self.net.routes["https://example.com/feed.xml"] = (403, "text/html", "no")
        self.assertEqual(self.run_src(self.add(label="b"))["state"], "manual_check")

    def test_greenhouse(self):
        body = {"jobs": [{"id": 11, "title": "Platform Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/11", "location": {"name": "Remote - US"},
                          "updated_at": "2026-11-12T10:00:00Z", "content": "&lt;p&gt;Great job. $150,000 - $180,000 a year&lt;/p&gt;"}]}
        self.net.routes["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"] = (200, "application/json", json.dumps(body))
        self.run_src(self.eng.add_preset("greenhouse", {"board": "acme", "label": "Acme"}))
        it = self.all_items()[0]
        self.assertEqual((it["company"], it["work_mode"], it["apply_method"], it["geo"]["regions"]), ("acme", "remote", "ats_form", ["US"]))
        self.assertEqual(it["pay"]["max"], 180000)
        self.assertNotIn("<p>", it["description"])

    def test_lever(self):
        body = [{"id": "abc", "text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc", "applyUrl": "https://jobs.lever.co/acme/abc/apply",
                 "categories": {"location": "Berlin", "commitment": "Full-time", "team": "Data"}, "workplaceType": "hybrid",
                 "descriptionPlain": "Work with data", "salaryRange": {"currency": "EUR", "interval": "per-year-salary", "min": 70000, "max": 90000}}]
        self.net.routes["https://api.lever.co/v0/postings/acme?mode=json"] = (200, "application/json", json.dumps(body))
        self.run_src(self.eng.add_preset("lever", {"board": "acme"}))
        it = self.all_items()[0]
        self.assertEqual((it["work_mode"], it["employment"], it["apply_method"], it["pay"]["currency"]), ("hybrid", "full-time", "ats_form", "EUR"))

    def test_other_ats_shapes(self):
        cases = {
            "ashby": ("https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true",
                      {"jobs": [{"id": "1", "title": "SRE", "location": "NYC", "isRemote": True, "employmentType": "FullTime", "jobUrl": "https://jobs.ashbyhq.com/acme/1",
                                 "publishedAt": "2026-11-10T00:00:00Z", "descriptionPlain": "Keep it up"}]}),
            "workable": ("https://apply.workable.com/api/v1/widget/accounts/acme?details=true",
                         {"name": "Acme Ltd", "jobs": [{"title": "QA", "shortcode": "AB12", "url": "https://apply.workable.com/acme/j/AB12", "city": "Paris", "country": "France",
                                                          "telecommuting": True, "employment_type": "Contract", "published_on": "2026-11-11", "description": "<p>Test</p>"}]}),
            "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100",
                                {"content": [{"id": "77", "name": "PM", "company": {"name": "Acme"}, "location": {"city": "Rome", "country": "it", "remote": True},
                                              "typeOfEmployment": {"label": "Part-time"}, "releasedDate": "2026-11-09T00:00:00Z"}]}),
            "recruitee": ("https://acme.recruitee.com/api/offers/",
                          {"offers": [{"id": 5, "title": "Support", "careers_url": "https://acme.recruitee.com/o/support", "location": "Remote", "remote": True,
                                       "employment_type_code": "fulltime", "created_at": "2026-11-08T00:00:00Z", "description": "<p>Help</p>", "min_salary": 40000, "max_salary": 50000, "salary_currency": "EUR"}]}),
        }
        for adapter, (url, body) in cases.items():
            self.net.routes[url] = (200, "application/json", json.dumps(body))
            res = self.run_src(self.eng.add_preset(adapter, {"board": "acme"}))
            self.assertEqual(res["state"], "ok", (adapter, res))
            self.assertEqual(res["found"], 1, adapter)
        by = {i["title"]: i for i in self.all_items()}
        self.assertEqual(by["SRE"]["work_mode"], "remote")
        self.assertEqual(by["QA"]["company"], "Acme Ltd")
        self.assertEqual(by["QA"]["employment"], "contract")
        self.assertEqual(by["PM"]["employment"], "part-time")
        self.assertEqual(by["PM"]["url"], "https://jobs.smartrecruiters.com/acme/77")
        self.assertEqual(by["Support"]["pay"]["currency"], "EUR")
        self.assertTrue(all(i["apply_method"] == "ats_form" for i in by.values()))

    def test_teamtailor_rss(self):
        feed = ("<?xml version='1.0'?><rss version='2.0' xmlns:tt='https://teamtailor.com/locations'><channel><item><title>Barista Lead</title>"
                "<link>https://acme.teamtailor.com/jobs/1-barista</link><description>Coffee</description><tt:remoteStatus>hybrid</tt:remoteStatus>"
                "<pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>")
        self.net.routes["https://acme.teamtailor.com/jobs.rss"] = (200, "application/rss+xml", feed)
        self.run_src(self.eng.add_preset("teamtailor", {"board": "acme"}))
        it = self.all_items()[0]
        self.assertEqual((it["work_mode"], it["apply_method"]), ("hybrid", "ats_form"))

    def test_hn_who_is_hiring(self):
        first = {"hits": [{"objectID": "4242"}]}
        comments = {"hits": [{"objectID": "9", "created_at": "2026-11-02T10:00:00Z",
                              "comment_text": "Zed | Backend Engineer | Berlin | REMOTE (EU) | $120k<p>We build things</p>"}]}
        self.net.routes["https://hn.algolia.com/api/v1/search_by_date?query=%22Ask%20HN%3A%20Who%20is%20hiring%3F%22&tags=story,author_whoishiring&hitsPerPage=1"] = (200, "application/json", json.dumps(first))
        self.net.routes["https://hn.algolia.com/api/v1/search?tags=comment,story_4242&hitsPerPage=100"] = (200, "application/json", json.dumps(comments))
        self.run_src(self.eng.add_preset("hn-hiring", {}))
        it = self.all_items()[0]
        self.assertEqual((it["company"], it["title"], it["work_mode"]), ("Zed", "Backend Engineer", "remote"))

    def test_email_alert_is_read_locally_and_respects_senders(self):
        self.mail.items = [
            {"account": "work", "id": "m1", "sender": "Upwork <noreply@upwork.com>", "subject": "New jobs for you", "date": "2026-11-13T10:00:00",
             "body": "New job matches\nPython scraper needed ($300 budget)\nhttps://www.upwork.com/jobs/~01abc\nUnsubscribe https://www.upwork.com/unsubscribe"},
            {"account": "work", "id": "m2", "sender": "friend@example.com", "subject": "Lunch?", "date": "2026-11-13T10:00:00", "body": "https://example.com/x"}]
        src = self.eng.add_preset("upwork", {})
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        self.assertEqual(self.mail.destinations, ["local"])          # bodies never leave the laptop: no cloud destination
        it = self.all_items()[0]
        self.assertEqual((it["apply_method"], it["auto_submit"], it["kind"]), ("platform_manual", False, "freelance"))
        self.assertIn("upwork.com/jobs", it["url"])
        self.assertEqual(len(self.all_items()), 1)
        self.assertEqual(self.finder.calls[0]["kind"], "freelance")
        self.run_src(src)                                             # the same mail is not read twice
        self.assertEqual(self.mail.destinations, ["local"])
        self.mail.items.append({"account": "work", "id": "m3", "sender": "x@upwork.com", "subject": "More", "date": "2026-11-13T11:00:00",
                                "body": "Another gig here\nhttps://www.upwork.com/jobs/~02"})
        self.assertEqual(self.run_async(self.eng.test_source(src))["found"], 1)    # the test button must not mark mail as seen
        self.assertEqual(self.run_async(self.eng.test_source(src))["found"], 1)

    def test_webhook(self):
        src = self.add(label="Hook", adapter="webhook", settings={})
        r = self.client.post(f"/api/oppsources/sources/{src['id']}/webhook")
        secret, path = r.json()["secret"], r.json()["path"]
        body = {"items": [{"title": "Remote Go dev", "company": "Hooky", "url": "https://hooky.example/j/1", "description": "Fully remote", "pay": "$70/hour"}]}
        self.assertEqual(self.anon.post(path, json=body).status_code, 401)
        self.assertEqual(self.anon.post(path, json=body, headers={"X-Webhook-Secret": "wrong"}).status_code, 401)
        ok = self.anon.post(path, json=body, headers={"X-Webhook-Secret": secret})
        self.assertEqual(ok.status_code, 200, ok.text)
        it = self.all_items()[0]
        self.assertEqual((it["company"], it["pay"]["unit"], it["work_mode"]), ("Hooky", "hour", "remote"))
        self.assertEqual(self.anon.post(path, content="not json", headers={"X-Webhook-Secret": secret}).status_code, 400)
        for _ in range(12):
            self.anon.post(path, json=body, headers={"X-Webhook-Secret": "bad"})
        self.assertEqual(self.anon.post(path, json=body, headers={"X-Webhook-Secret": secret}).status_code, 429)
        self.assertEqual(self.anon.post("/hooks/source/missing", json=body, headers={"X-Webhook-Secret": secret}).status_code, 401)

    def test_unreachable_source_backs_off(self):
        self.net.routes["https://example.com/feed.xml"] = httpx.ConnectError("down")
        src = self.add()
        self.assertEqual(self.run_src(src)["state"], "error")
        first = self.eng._state()["runs"][src["id"]]["next_at"]
        self.run_src(src)
        second = self.eng._state()["runs"][src["id"]]["next_at"]
        self.assertGreater(second - self.now, first - self.now)
        self.assertFalse(self.eng.due(src, self.now))
        self.assertTrue(self.eng.due(src, second + 1))
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS)
        self.assertEqual(self.run_src(src)["state"], "ok")
        self.assertEqual(self.eng._state()["runs"][src["id"]]["fails"], 0)


class Fetching(Base):
    def test_etag_and_if_modified_since(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS, {"ETag": '"v1"', "Last-Modified": "Fri, 13 Nov 2026 10:00:00 GMT"})
        src = self.add()
        self.run_src(src)
        self.assertNotIn("if-none-match", self.net.requests[-1][1])
        self.net.routes["https://example.com/feed.xml"] = (304, "application/rss+xml", "")
        res = self.run_src(src)
        self.assertEqual(res["state"], "unchanged")
        h = self.net.requests[-1][1]
        self.assertEqual((h.get("if-none-match"), h.get("if-modified-since")), ('"v1"', "Fri, 13 Nov 2026 10:00:00 GMT"))
        self.assertEqual(len(self.all_items()), 2)
        self.eng.update_source(src["id"], {"settings": {"url": "https://example.com/other.xml"}})      # an edited address starts clean
        self.assertNotIn("etag", self.eng._state()["runs"][src["id"]])

    def test_unchanged_body_is_not_reprocessed(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS)
        src = self.add()
        self.run_src(src)
        calls = len(self.finder.calls)
        self.assertEqual(self.run_src(src)["state"], "unchanged")
        self.assertEqual(len(self.finder.calls), calls)

    def test_per_host_rate_limit(self):
        for n in (1, 2, 3):
            self.net.routes[f"https://example.com/f{n}.xml"] = (200, "application/rss+xml", RSS)
            self.add(label=f"s{n}", settings={"url": f"https://example.com/f{n}.xml"})
        self.run_async(self.eng.run_now())
        self.assertGreaterEqual(len([s for s in self.sleeps if s > 0]), 2)
        self.assertTrue(all(0 <= s <= 3 * S.HOST_GAP for s in self.sleeps))

    def test_private_hosts_never_fetched_even_if_stored(self):
        src = self.add()
        self.eng.sources_file.save([{**src, "settings": {"url": "http://169.254.169.254/latest"}}])
        res = self.run_src(self.eng.sources()[0])
        self.assertEqual(res["state"], "manual_check")
        self.assertEqual(self.net.requests, [])

    def test_test_button_stores_nothing(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS, {"ETag": '"v1"'})
        src = self.add()
        r = self.client.post(f"/api/oppsources/sources/{src['id']}/test")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["found"], 2)
        self.assertEqual(r.json()["sample"][0]["work_mode"], "remote")
        self.assertEqual(self.all_items(), [])
        self.assertEqual(self.eng._state()["runs"], {})
        self.net.routes["https://example.com/feed.xml"] = (200, "text/html", "nope")
        self.assertIn("not a readable feed", self.client.post(f"/api/oppsources/sources/{src['id']}/test").json()["error"])


class Pipeline(Base):
    def posting(self, title, company, url, desc="Remote python role", published=None):
        return {"title": title, "company": company, "url": url, "description": desc, "published": published or "2026-11-12T10:00:00Z"}

    def test_same_job_on_three_boards_is_one_item(self):
        a, b, c = self.add(label="Board A"), self.add(label="Board B"), self.add(label="Board C")
        self.eng.process(a, [self.posting("Senior Python Engineer", "Acme Inc.", "https://a.example/1")])
        self.eng.process(b, [self.posting("Senior Python Engineer (Remote)", "ACME", "https://b.example/9")])
        res = self.eng.process(c, [self.posting("Senior Python Engineer - Remote", "Acme, LLC", "https://c.example/5")])
        self.assertEqual(len(self.all_items()), 1)
        it = self.all_items()[0]
        self.assertEqual(sorted(it["also_on"]), ["Board B", "Board C"])
        self.assertEqual(res["dup"], 1)
        self.assertIn("multi", [s["code"] for s in it["signals"]])
        self.assertEqual(sum(len(c["rows"]) for c in self.finder.calls), 1)       # the finder saw it once

    def test_same_url_without_company_dedupes(self):
        a, b = self.add(label="A"), self.add(label="B")
        self.eng.process(a, [self.posting("Engineer", "", "https://x.example/j?utm_source=a")])
        self.eng.process(b, [self.posting("Engineer role", "", "https://www.x.example/j/")])
        self.assertEqual(len(self.all_items()), 1)

    def test_repost_and_ghost_detection(self):
        s = self.add()
        self.eng.process(s, [self.posting("Staff Engineer", "Acme", "https://a.example/1")])
        for n in (2, 3):
            self.now += 86400
            self.eng.process(s, [self.posting("Staff Engineer", "Acme", f"https://a.example/{n}")])
        it = self.all_items()[0]
        self.assertEqual(it["reposts"], 2)
        codes = [x["code"] for x in it["signals"]]
        self.assertIn("repost", codes)
        self.assertIn("ghost", codes)
        s2 = self.add(label="Old")                           # an old posting that is still listed is a possible ghost
        self.config.oppsources_stale_days = 365
        self.eng.process(s2, [self.posting("Old Role", "Zed", "https://z.example/1", published="2026-07-01T00:00:00Z")])
        old = next(i for i in self.all_items() if i["title"] == "Old Role")
        self.assertIn("ghost", [x["code"] for x in old["signals"]])

    def test_stale_items_expire_and_are_not_handed_on(self):
        s = self.add()
        res = self.eng.process(s, [self.posting("Ancient", "Acme", "https://a.example/old", published="2026-01-01T00:00:00Z"),
                                   self.posting("Fresh", "Acme", "https://a.example/new")])
        self.assertEqual(res["expired"], 1)
        self.assertEqual({i["title"]: i["status"] for i in self.all_items()}, {"Ancient": "expired", "Fresh": "new"})
        self.assertEqual([r["title"] for c in self.finder.calls for r in c["rows"]], ["Fresh"])
        self.assertEqual([i["title"] for i in self.eng.items()], ["Fresh"])
        self.now += 60 * 86400                               # time passes: the fresh one goes stale too
        self.eng.process(s, [])
        self.assertEqual(next(i["status"] for i in self.all_items() if i["title"] == "Fresh"), "expired")

    def test_complete_lists_close_removed_roles(self):
        s = self.add()
        self.eng.process(s, [self.posting("A role", "Acme", "https://a.example/1"), self.posting("B role", "Acme", "https://a.example/2")], complete=True)
        res = self.eng.process(s, [self.posting("A role", "Acme", "https://a.example/1")], complete=True)
        self.assertEqual(res["closed"], 1)
        self.assertEqual({i["title"]: i["status"] for i in self.all_items()}, {"A role": "new", "B role": "closed"})

    def test_scam_goes_to_blocked_and_not_the_finder(self):
        s = self.add()
        self.eng.process(s, [self.posting("Packer", "Shady", "https://s.example/1", desc="Send us money for the starter kit. Pay a registration fee. Contact us on WhatsApp only.")])
        self.assertEqual(self.all_items()[0]["status"], "blocked")
        self.assertEqual(self.finder.calls, [])
        self.assertEqual([i["title"] for i in self.eng.items(status="blocked")], ["Packer"])

    def test_platform_items_are_never_auto_submit(self):
        s = self.add(label="Hook", adapter="webhook", settings={})
        self.eng.process(s, [self.posting("Fix my site", "Client", "https://www.upwork.com/jobs/~01", desc="Fixed price $200. Freelance.")])
        it = self.all_items()[0]
        self.assertEqual((it["apply_method"], it["auto_submit"], it["kind"], it["employment"]), ("platform_manual", False, "freelance", "freelance"))
        self.assertIn("by hand", it["apply_note"])
        self.eng.process(s, [self.posting("Profile", "Co", "https://www.linkedin.com/jobs/view/1")])
        self.assertEqual(next(i for i in self.all_items() if i["title"] == "Profile")["apply_method"], "platform_manual")

    def test_apply_by_email(self):
        s = self.add()
        self.eng.process(s, [self.posting("Mail me", "Co", "https://co.example/1", desc="Please send your CV to jobs@co.example to apply.")])
        it = self.all_items()[0]
        self.assertEqual((it["apply_method"], it["apply_email"]), ("email", "jobs@co.example"))


class EarlyBird(Base):
    def test_detect_ats_from_urls(self):
        cases = {"https://boards.greenhouse.io/acme": ("greenhouse", "acme"), "https://job-boards.greenhouse.io/acme/jobs/12": ("greenhouse", "acme"),
                 "https://jobs.lever.co/acme/abc": ("lever", "acme"), "https://jobs.eu.lever.co/acme": ("lever", "acme"),
                 "https://jobs.ashbyhq.com/acme": ("ashby", "acme"), "https://apply.workable.com/acme/": ("workable", "acme"),
                 "https://careers.smartrecruiters.com/Acme": ("smartrecruiters", "Acme"), "https://acme.recruitee.com/": ("recruitee", "acme"),
                 "https://acme.teamtailor.com/jobs": ("teamtailor", "acme")}
        for url, (adapter, board) in cases.items():
            got = S.detect_ats(url)
            self.assertEqual((got["adapter"], got["board"]), (adapter, board), url)
        self.assertEqual(S.detect_ats("https://jobs.eu.lever.co/acme")["region"], "eu")
        self.assertIsNone(S.detect_ats("https://example.com/careers"))
        self.assertEqual(S.detect_ats("https://example.com/careers", '<iframe src="https://boards.greenhouse.io/embed/job_board?for=zed"></iframe>')["board"], "zed")

    def test_watch_from_page_markup_respects_robots(self):
        self.net.routes["https://zed.example/careers"] = (200, "text/html", '<a href="https://jobs.lever.co/zedco">Open roles</a>')
        self.net.routes["https://zed.example/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
        r = self.client.post("/api/oppsources/watch", json={"url": "https://zed.example/careers", "label": "Zed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["adapter"], r.json()["settings"]["board"], r.json()["watch"]), ("lever", "zedco", True))
        self.net.routes["https://zed.example/robots.txt"] = (200, "text/plain", "User-agent: *\nDisallow: /\n")
        self.eng.fetcher._robots = {}
        r = self.client.post("/api/oppsources/watch", json={"url": "https://zed.example/careers"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("robots", r.json()["detail"])
        self.assertEqual(self.client.post("/api/oppsources/watch", json={"url": "http://127.0.0.1/careers"}).status_code, 400)

    def test_new_roles_are_early_but_the_first_run_is_a_baseline(self):
        r = self.run_async(self.eng.add_watch("https://boards.greenhouse.io/acme", "Acme"))
        url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"

        def board(*roles):
            return (200, "application/json", json.dumps({"jobs": [{"id": i, "title": t, "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
                                                                   "location": {"name": "Remote"}, "updated_at": "2026-11-13T10:00:00Z", "content": "Remote role"} for i, t in roles]}))
        self.net.routes[url] = board((1, "Existing Engineer"))
        self.run_src(r)
        self.assertEqual(self.early, [])                                       # baseline: what was already open is not 'early'
        self.assertFalse(any(i.get("early") for i in self.all_items()))
        self.now += 11 * 60
        self.net.routes[url] = board((1, "Existing Engineer"), (2, "Brand New Engineer"))
        res = self.run_src(r)
        self.assertEqual(res["counts"]["early"], 1)
        self.assertEqual(self.early, ["Brand New Engineer"])
        it = next(i for i in self.all_items() if i["title"] == "Brand New Engineer")
        self.assertTrue(it["early"])
        self.assertIn("early", [s["code"] for s in it["signals"]])
        self.assertTrue(self.finder.calls[-1]["rows"][0]["meta"]["early"])
        self.assertEqual([i["title"] for i in self.eng.items(early=True)], ["Brand New Engineer"])

    def test_poll_interval_is_owner_set_and_watch_is_faster(self):
        w = self.eng.add_source({"label": "W", "adapter": "greenhouse", "watch": True, "settings": {"board": "w"}})
        n = self.add(label="N")
        self.assertEqual((self.eng._interval(w), self.eng._interval(n)), (10, 60))
        self.config.oppsources_watch_interval_min = 3
        self.config.oppsources_interval_min = 30
        self.assertEqual((self.eng._interval(w), self.eng._interval(n)), (3, 30))
        self.eng.update_source(w["id"], {"interval_min": 5})
        self.assertEqual(self.eng._interval(self.eng.get_source(w["id"])), 5)
        self.net.routes["https://boards-api.greenhouse.io/v1/boards/w/jobs?content=true"] = (200, "application/json", '{"jobs": []}')
        self.run_src(self.eng.get_source(w["id"]))
        self.assertEqual(self.eng._state()["runs"][w["id"]]["next_at"], self.now + 5 * 60)
        self.assertFalse(self.eng.due(self.eng.get_source(w["id"]), self.now + 60))
        self.assertTrue(self.eng.due(self.eng.get_source(w["id"]), self.now + 5 * 60))

    def test_early_roles_reach_the_real_queue_even_with_a_low_score(self):
        from hub import opps
        pushed = []
        real = opps.Opps(self.ctx, push=lambda ctx, item: pushed.append(item) or True, clock=lambda: self.now)
        self.eng._finder = real
        self.config.opps_min_score = 100                      # nothing could reach this score by fit alone
        w = self.eng.add_source({"label": "W", "adapter": "greenhouse", "watch": True, "settings": {"board": "w", "company": "Wco"}})
        url = "https://boards-api.greenhouse.io/v1/boards/w/jobs?content=true"
        self.net.routes[url] = (200, "application/json", '{"jobs": []}')
        self.run_src(w)
        self.net.routes[url] = (200, "application/json", json.dumps({"jobs": [{"id": 3, "title": "Welder", "absolute_url": "https://boards.greenhouse.io/w/jobs/3",
                                                                               "content": "x", "updated_at": "2026-11-13T10:00:00Z"}]}))
        self.run_src(w)
        self.assertEqual([p["title"] for p in pushed], ["Welder"])


class Freelance(Base):
    def make_item(self):
        s = self.add(label="Hook", adapter="webhook", settings={})
        self.eng.process(s, [{"title": "Build a scraper", "company": "Client Co", "url": "https://www.upwork.com/jobs/~01",
                              "description": "Freelance gig: need a Python scraper for 3 sites. Fixed price $400.", "published": "2026-11-13T00:00:00Z"}])
        return self.all_items()[0]

    def test_proposal_context(self):
        it = self.make_item()
        ctx = S.proposal_context(it)
        self.assertTrue(ctx["ok"])
        self.assertIn("scraper", ctx["task_summary"])
        self.assertEqual((ctx["budget"]["min"], ctx["budget"]["unit"], ctx["budget"]["type"], ctx["budget"]["currency"]), (400, "project", "fixed", "USD"))
        self.assertEqual((ctx["platform"], ctx["auto_submit"], ctx["client"]), ("upwork.com", False, "Client Co"))
        self.assertIn("private profile", ctx["rules"])
        self.assertTrue(S.proposal_context(it["id"], self.ctx)["ok"])
        self.assertFalse(S.proposal_context("0" * 16, self.ctx)["ok"])
        self.assertEqual(self.client.get(f"/api/oppsources/items/{it['id']}/proposal-context").json()["task_summary"], ctx["task_summary"])

    def test_slot_and_bid_tracking(self):
        it = self.make_item()
        self.assertEqual(it["proposal"]["state"], "none")
        r = self.client.put(f"/api/oppsources/items/{it['id']}/proposal", json={"state": "draft", "text": "Hello, here is my plan."})
        self.assertEqual(r.json()["proposal"]["text"], "Hello, here is my plan.")
        self.assertEqual(self.client.put(f"/api/oppsources/items/{it['id']}/proposal", json={"state": "sent_by_bot"}).status_code, 400)
        r = self.client.put(f"/api/oppsources/items/{it['id']}/bid", json={"status": "submitted", "amount": 350, "currency": "usd", "note": "sent by hand"})
        self.assertEqual((r.json()["bid"]["status"], r.json()["bid"]["currency"]), ("submitted", "USD"))
        self.assertEqual(self.client.put(f"/api/oppsources/items/{it['id']}/bid", json={"status": "auto-sent"}).status_code, 400)
        self.assertEqual(self.client.put(f"/api/oppsources/items/{it['id']}/bid", json={"status": "won", "amount": -5}).status_code, 400)
        self.assertTrue(self.client.get("/api/oppsources/items").json()["items"][0]["needs_proposal"])
        self.assertEqual(self.client.put("/api/oppsources/items/zzzz/bid", json={"status": "won"}).status_code, 404)

    def test_regular_jobs_have_no_proposal_slot(self):
        s = self.add()
        self.eng.process(s, [{"title": "Engineer", "company": "Acme", "url": "https://a.example/1", "description": "Full-time role", "published": "2026-11-13T00:00:00Z"}])
        it = self.all_items()[0]
        self.assertNotIn("proposal", it)
        self.assertEqual(self.client.put(f"/api/oppsources/items/{it['id']}/proposal", json={"state": "draft"}).status_code, 409)

    def test_open_link_mode(self):
        r = self.client.post("/api/oppsources/links", json={"url": "https://www.fiverr.com/requests/1", "title": "Logo for my cafe", "budget": "$150",
                                                            "note": "Need a logo. Freelance."})
        self.assertEqual(r.status_code, 200, r.text)
        it = r.json()["item"]
        self.assertEqual((it["apply_method"], it["auto_submit"], it["kind"]), ("platform_manual", False, "freelance"))
        self.assertEqual(it["pay"]["unit"], "project")
        self.assertEqual(it["proposal"]["state"], "none")
        self.assertEqual(self.net.requests, [])                                # nothing was fetched from the platform
        self.assertEqual(self.client.post("/api/oppsources/links", json={"url": "javascript:alert(1)"}).status_code, 400)


class StatusAndTools(Base):
    def test_status_shows_last_fetch_and_item_counts(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS)
        self.run_src(self.add())
        d = self.client.get("/api/oppsources").json()
        row = d["sources"][0]
        self.assertEqual((row["status"]["state"], row["status"]["found"], row["status"]["items"]), ("ok", 2, 2))
        self.assertNotIn("etag", row["status"])
        self.assertEqual(d["counts"]["blocked"], 1)
        self.assertEqual(d["watch_interval_min"], 10)

    def test_dismiss_and_filters(self):
        self.net.routes["https://example.com/feed.xml"] = (200, "application/rss+xml", RSS)
        self.run_src(self.add())
        it = self.client.get("/api/oppsources/items", params={"mode": "remote"}).json()["items"][0]
        self.assertEqual(self.client.get("/api/oppsources/items", params={"q": "python"}).json()["items"][0]["id"], it["id"])
        self.assertEqual(self.client.get("/api/oppsources/items", params={"mode": "onsite"}).json()["items"], [])
        self.client.post(f"/api/oppsources/items/{it['id']}/dismiss")
        self.assertEqual(self.client.get("/api/oppsources/items", params={"mode": "remote"}).json()["items"], [])

    def test_assistant_tools_registered(self):
        tools = {t.spec["name"]: t for t in S.build(lambda: None, self.config)}
        self.assertEqual(set(tools), {"sources_list", "sources_add", "sources_test"})
        self.assertTrue(tools["sources_add"].needs_approval({}))
        self.assertFalse(tools["sources_list"].needs_approval({}))
        names, rx = TOOL_GROUPS["oppsources"]
        self.assertEqual(names, {"sources_list", "sources_add", "sources_test"})
        self.assertTrue(rx.search("watch the careers page of Acme"))

    def test_tools_through_the_app(self):
        app = FastAPI()
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None,
                       audit=lambda *a, **k: None)
        open_ctx.oppsources = self.eng
        app.include_router(S.make_router(open_ctx))
        tools = {t.spec["name"]: t for t in S.build(lambda: app, self.config)}
        ok, out = self.run_async(tools["sources_list"].run({}))
        self.assertTrue(ok, out)
        self.assertIn("No job sources", out)
        ok, out = self.run_async(tools["sources_add"].run({"preset": "greenhouse", "board": "acme"}))
        self.assertTrue(ok, out)
        sid = self.eng.sources()[0]["id"]
        self.net.routes["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"] = (200, "application/json", json.dumps(
            {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}]}))
        ok, out = self.run_async(tools["sources_test"].run({"id": sid}))
        self.assertTrue(ok, out)
        self.assertIn("1 item", out)
        ok, out = self.run_async(tools["sources_list"].run({}))
        self.assertIn(sid, out)


ADZUNA = {"count": 2, "mean": 900000, "results": [
    {"id": "4001", "title": "Backend Developer", "description": "Python and SQL. Hybrid in Pune.", "created": "2026-11-12T09:00:00Z",
     "redirect_url": "https://www.adzuna.in/land/ad/4001?se=abc", "salary_min": 900000, "salary_max": 1200000, "salary_is_predicted": "0",
     "contract_type": "permanent", "contract_time": "full_time", "location": {"display_name": "Pune, Maharashtra", "area": ["India", "Maharashtra", "Pune"]},
     "company": {"display_name": "Acme Labs"}, "category": {"label": "IT Jobs"}},
    {"id": "4002", "title": "Data Engineer", "description": "SQL, Airflow.", "created": "2026-11-13T09:00:00Z",
     "redirect_url": "https://www.adzuna.in/land/ad/4002", "salary_min": 500000, "salary_max": 500000, "salary_is_predicted": "1",
     "location": {"display_name": "Remote"}, "company": {"display_name": "Zed"}}]}
JOOBLE = {"totalCount": 2, "jobs": [
    {"title": "Python Developer", "location": "Berlin", "snippet": "Python, Django. Remote friendly.", "salary": "EUR 60k per year", "source": "example.org",
     "type": "Full-time", "link": "https://jooble.org/desc/111", "company": "Acme GmbH", "updated": "2026-11-10T10:00:00.0000000", "id": 111},
    {"title": "", "link": "https://jooble.org/desc/112"}]}
FINDWORK = {"count": 1, "next": None, "previous": None, "results": [
    {"id": "fw1", "role": "Remote Python Engineer", "company_name": "Acme", "company_num_employees": "11-50", "employment_type": "full time",
     "location": "Remote", "remote": True, "logo": None, "url": "https://findwork.dev/fw1/remote-python-engineer",
     "text": "<p>Python and Postgres.</p>", "date_posted": "2026-11-12T10:00:00Z", "keywords": ["python", "postgres"], "source": "x"}]}
PERSONIO = """<?xml version="1.0" encoding="UTF-8"?><workzag-jobs>
<position><id>7001</id><office>Berlin</office><department>Tech</department><name>Backend Engineer (m/f/d)</name>
<jobDescriptions><jobDescription><name>Your tasks</name><value>&lt;p&gt;Build APIs in Python.&lt;/p&gt;</value></jobDescription></jobDescriptions>
<employmentType>permanent</employmentType><seniority>experienced</seniority><schedule>full-time</schedule><createdAt>2026-11-11T17:43:42+00:00</createdAt></position>
<position><id>7002</id><office>Munich</office><name>Intern</name><jobDescriptions></jobDescriptions><schedule>part-time</schedule><createdAt>2026-11-10T10:00:00+00:00</createdAt></position>
</workzag-jobs>"""
BREEZY = [{"id": "b1", "friendly_id": "b1-data-analyst", "name": "Data Analyst", "url": "https://acme.breezy.hr/p/b1-data-analyst", "published_date": "2026-11-06T20:57:57.244Z",
           "type": {"id": "full-time", "name": "Full-Time"}, "location": {"name": "Remote", "is_remote": True, "remote_details": {"value": "remote"}},
           "department": "", "salary": "$21 / hour", "company": {"name": "Acme", "friendly_id": "acme"}}]
WORKDAY = {"total": 3, "jobPostings": [
    {"title": "Senior Engineer", "externalPath": "/job/Pune/Senior-Engineer_JR100", "locationsText": "Pune", "postedOn": "Posted 3 Days Ago", "bulletFields": ["JR100"]},
    {"title": "Analyst", "externalPath": "/job/Remote/Analyst_JR101", "locationsText": "Remote", "postedOn": "Posted Today", "bulletFields": ["JR101"]}]}


class NewSources(Base):
    """Adzuna, Jooble, Findwork (keyed), Workday, Personio, Breezy. Every fixture is invented."""

    def add_keyed(self, provider, **settings):
        ok = self.client.post(f"/api/oppsources/presets/{provider}/add", json=settings)
        self.assertEqual(ok.status_code, 200, ok.text)
        return ok.json()

    def test_keyed_presets_start_disabled_and_say_why(self):
        by = {p["id"]: p for p in self.client.get("/api/oppsources/presets").json()["presets"]}
        for pid in ("adzuna", "jooble", "findwork"):
            self.assertEqual(by[pid]["key_set"], False)
            self.assertIn("needs your free", by[pid]["disabled_reason"])
            self.assertEqual(by[pid]["verification"], "unverified")
        src = self.add_keyed("adzuna", country="in", what="python developer")
        self.assertFalse(src["enabled"])
        self.assertIn("Needs your free Adzuna key", src["note"])
        r = self.client.put(f"/api/oppsources/sources/{src['id']}", json={"enabled": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("free Adzuna key", r.text)
        self.run_async(self.eng.run_now())
        self.assertEqual(self.net.requests, [])                       # nothing is fetched without a key
        r = self.client.post(f"/api/oppsources/sources/{src['id']}/test").json()
        self.assertIn("free Adzuna key", r["error"])
        self.assertEqual(self.net.requests, [])

    def test_keys_are_saved_in_the_keyring_and_never_echoed(self):
        secret_id, secret_key = "abcd1234efgh", "zzzz9999yyyy8888"
        r = self.client.put("/api/oppsources/keys/adzuna", json={"values": {"app_id": secret_id, "app_key": secret_key}})
        self.assertEqual(r.status_code, 200, r.text)
        for text in (r.text, self.client.get("/api/oppsources/keys").text, self.client.get("/api/oppsources/presets").text, self.client.get("/api/oppsources").text):
            self.assertNotIn(secret_id, text)
            self.assertNotIn(secret_key, text)
        self.assertTrue(self.client.get("/api/oppsources/keys").json()["keys"]["adzuna"]["set"])
        self.assertEqual(self.secrets.data["oppsources:adzuna:app_id"], secret_id)         # in the keyring store only
        import pathlib
        for f in pathlib.Path(self.config.data_path()).rglob("*"):
            if f.is_file():
                self.assertNotIn(secret_key, f.read_text(encoding="utf-8", errors="ignore"), f.name)
        src = self.add_keyed("adzuna", country="in", what="python")
        self.assertTrue(src["enabled"])                                 # a key exists now, so the preset starts on
        self.assertEqual(self.client.put("/api/oppsources/keys/adzuna", json={"values": {"app_id": "short", "app_key": secret_key}}).status_code, 400)
        self.assertEqual(self.client.put("/api/oppsources/keys/adzuna", json={"values": {"app_id": "has space 1234", "app_key": secret_key}}).status_code, 400)
        self.assertEqual(self.client.put("/api/oppsources/keys/nope", json={"values": {}}).status_code, 404)
        self.assertEqual(self.client.put("/api/oppsources/keys/adzuna", json={"values": {"app_id": secret_id}}).status_code, 400)   # both parts needed
        d = self.client.delete("/api/oppsources/keys/adzuna").json()
        self.assertFalse(d["keys"]["adzuna"]["set"])
        self.assertNotIn("oppsources:adzuna:app_id", self.secrets.data)
        self.assertFalse(self.eng.get_source(src["id"])["enabled"])          # its source switched itself off
        self.assertEqual(self.anon.get("/api/oppsources/keys").status_code, 401)
        self.assertEqual(self.anon.put("/api/oppsources/keys/adzuna", json={"values": {}}).status_code, 401)

    def test_scrub_removes_a_key_from_any_text(self):
        self.client.put("/api/oppsources/keys/jooble", json={"values": {"key": "joobleKEY-12345678"}})
        out = self.eng.keys.scrub("could not reach https://jooble.org/api/joobleKEY-12345678 now")
        self.assertNotIn("joobleKEY", out)
        self.assertIn("***", out)

    def test_adzuna_url_fields_and_estimated_salary(self):
        self.client.put("/api/oppsources/keys/adzuna", json={"values": {"app_id": "id-12345678", "app_key": "key-87654321"}})
        src = self.add_keyed("adzuna", country="in", what="python developer", where="Pune")
        url_seen = []

        def handler(request):
            url_seen.append(str(request.url))
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(ADZUNA).encode())
        self.eng.fetcher.transport = httpx.MockTransport(handler)
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        self.assertEqual(res["found"], 2)
        u = url_seen[0]
        self.assertTrue(u.startswith("https://api.adzuna.com/v1/api/jobs/in/search/1?"))
        for part in ("app_id=id-12345678", "app_key=key-87654321", "what=python+developer", "where=Pune"):
            self.assertIn(part, u)
        items = {i["title"]: i for i in self.all_items()}
        a = items["Backend Developer"]
        self.assertEqual((a["pay"]["min"], a["pay"]["max"], a["pay"]["currency"], a["pay"]["unit"]), (900000, 1200000, "INR", "year"))
        self.assertEqual(a["company"], "Acme Labs")
        self.assertEqual(a["location"], "Pune, Maharashtra")
        self.assertIsNone(items["Data Engineer"]["pay"])                   # Adzuna's own estimate is not shown as pay
        self.assertNotIn("id-12345678", json.dumps(self.eng.status()))
        self.assertNotIn("key-87654321", json.dumps(self.all_items()))

    def test_jooble_is_a_post_with_the_key_in_the_address_only(self):
        self.client.put("/api/oppsources/keys/jooble", json={"values": {"key": "jooble-secret-key-1"}})
        src = self.add_keyed("jooble", keywords="python", location="Berlin")
        calls = []

        def handler(request):
            calls.append((request.method, str(request.url), request.content))
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(JOOBLE).encode())
        self.eng.fetcher.transport = httpx.MockTransport(handler)
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        method, url, body = calls[0]
        self.assertEqual((method, url), ("POST", "https://jooble.org/api/jooble-secret-key-1"))
        self.assertEqual(json.loads(body)["keywords"], "python")
        self.assertEqual(json.loads(body)["location"], "Berlin")
        it = self.all_items()[0]
        self.assertEqual((it["title"], it["company"], it["location"]), ("Python Developer", "Acme GmbH", "Berlin"))
        self.assertNotIn("jooble-secret-key-1", json.dumps(self.all_items()) + json.dumps(self.eng.status()) + json.dumps(self.eng._state()))
        # a failure never repeats the address
        self.eng.fetcher.transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom " + str(r.url))))
        bad = self.run_src(src, force=True)
        self.assertNotIn("jooble-secret-key-1", json.dumps(bad))
        self.assertNotIn("jooble-secret-key-1", json.dumps(self.eng._state()))

    def test_findwork_sends_the_token_header_to_its_own_host_only(self):
        self.client.put("/api/oppsources/keys/findwork", json={"values": {"token": "findwork-token-12"}})
        src = self.add_keyed("findwork", search="python", location="remote")
        seen = []

        def handler(request):
            seen.append((str(request.url), request.headers.get("authorization")))
            if request.url.host == "findwork.dev" and request.url.path == "/api/jobs/":
                return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(FINDWORK).encode())
            return httpx.Response(404)
        self.eng.fetcher.transport = httpx.MockTransport(handler)
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        self.assertEqual(seen[0][0], "https://findwork.dev/api/jobs/?search=python&location=remote")
        self.assertEqual(seen[0][1], "Token findwork-token-12")
        it = self.all_items()[0]
        self.assertEqual((it["title"], it["company"], it["work_mode"]), ("Remote Python Engineer", "Acme", "remote"))
        self.assertIn("Tags: python, postgres", it["description"])
        # a redirect to another host never carries the token
        seen.clear()

        def redirecting(request):
            seen.append((str(request.url), request.headers.get("authorization")))
            if request.url.host == "findwork.dev":
                return httpx.Response(302, headers={"location": "https://elsewhere.example/api"})
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")
        self.eng.fetcher.transport = httpx.MockTransport(redirecting)
        self.run_src(src, force=True)
        self.assertEqual(seen[-1][1], None)

    def test_adzuna_jooble_findwork_rows_parse_directly(self):
        rows = S.parse_adzuna(ADZUNA, {"country": "in"})
        self.assertEqual([r["title"] for r in rows], ["Backend Developer", "Data Engineer"])
        self.assertEqual(rows[0]["employment"], "full-time permanent")
        self.assertEqual(S.parse_adzuna(ADZUNA, {"country": "gb"})[0]["pay"]["currency"], "GBP")
        self.assertEqual([r["title"] for r in S.parse_jooble(JOOBLE, {})], ["Python Developer"])
        self.assertEqual(S.parse_jooble(JOOBLE, {})[0]["pay_text"], "EUR 60k per year")
        self.assertEqual(S.parse_findwork(FINDWORK, {})[0]["remote"], "remote")
        for bad in ({}, [], {"results": "x"}):
            with self.assertRaises(ValueError):
                S.parse_adzuna(bad, {})
        with self.assertRaises(ValueError):
            S.parse_jooble({"nope": 1}, {})

    def test_workday_posts_pages_and_respects_robots(self):
        self.assertEqual(self.client.post("/api/oppsources/presets/workday/add", json={"tenant": "acme", "site": "Careers"}).status_code, 400)
        bad = self.client.post("/api/oppsources/presets/workday/add", json={"tenant": "acme", "site": "Careers", "host": "evil.com/x"})
        self.assertEqual(bad.status_code, 400)
        src = self.add_keyed("workday", tenant="acme", site="Careers", host="wd5", search="engineer")
        self.assertEqual(src["adapter"], "workday")
        self.assertTrue(src["enabled"])
        calls = []
        base = "https://acme.wd5.myworkdayjobs.com"

        def handler(request):
            calls.append((request.method, str(request.url), request.content))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nDisallow: /private/\n")
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(WORKDAY).encode())
        self.eng.fetcher.transport = httpx.MockTransport(handler)
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual(posts[0][1], base + "/wday/cxs/acme/Careers/jobs")
        body = json.loads(posts[0][2])
        self.assertEqual((body["limit"], body["offset"], body["searchText"]), (20, 0, "engineer"))
        self.assertEqual(len(posts), 1)                            # 2 results < a full page of 20: one request is enough
        self.assertIn(("GET", base + "/robots.txt", b""), calls)
        items = {i["title"]: i for i in self.all_items()}
        self.assertEqual(items["Senior Engineer"]["url"], base + "/Careers/job/Pune/Senior-Engineer_JR100")
        self.assertEqual(items["Senior Engineer"]["apply_method"], "ats_form")
        self.assertEqual(items["Senior Engineer"]["company"], "acme")
        self.assertEqual(items["Analyst"]["posted_at"][:10], "2026-11-14")
        self.assertEqual(items["Senior Engineer"]["posted_at"][:10], "2026-11-11")
        self.assertTrue(all(s <= 3 * S.HOST_GAP for s in self.sleeps))

    def test_workday_robots_refusal_and_pagination(self):
        src = self.add_keyed("workday", tenant="acme", site="Careers", host="wd1")
        self.eng.fetcher.transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nDisallow: /\n")
                                                         if r.url.path == "/robots.txt" else httpx.Response(500))
        res = self.run_src(src)
        self.assertEqual(res["state"], "manual_check", res)
        full = {"total": 45, "jobPostings": [{"title": f"Role {n}", "externalPath": f"/job/X/R_{n}", "locationsText": "X", "postedOn": "Posted 30+ Days Ago", "bulletFields": [f"R{n}"]} for n in range(20)]}
        offsets = []

        def handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            offsets.append(json.loads(request.content)["offset"])
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(full).encode())
        self.eng.fetcher.transport = httpx.MockTransport(handler)
        self.eng.fetcher._robots = {}
        self.eng.update_source(src["id"], {"settings": {"tenant": "acme", "site": "Careers", "host": "wd1", "search": ""}})
        self.run_src(src, force=True)
        self.assertEqual(offsets, [0, 20, 40])                     # at most three pages

    def test_workday_redirect_to_a_maintenance_page_is_an_error_not_a_crash(self):
        src = self.add_keyed("workday", tenant="acme", site="Careers", host="wd5")
        self.eng.fetcher.transport = httpx.MockTransport(lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else
                                                         httpx.Response(303, headers={"location": "/maintenance"}) if r.url.path != "/maintenance" else
                                                         httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>Workday is currently unavailable.</html>"))
        res = self.run_src(src)
        self.assertEqual(res["state"], "error")
        self.assertIn("not JSON", res["error"])

    def test_workday_settings_are_validated(self):
        for bad in ({"tenant": "a b", "site": "S", "host": "wd5"}, {"tenant": "acme", "site": "S/../x", "host": "wd5"}, {"tenant": "acme", "site": "S", "host": "wd5.evil.com"}):
            with self.assertRaises(S.SourceError):
                S.check_settings("workday", bad)
        self.assertEqual(S.check_settings("workday", {"tenant": "acme", "site": "Careers", "host": "WD3"})["host"], "wd3")
        self.assertEqual(S.workday_posted("Posted Yesterday", NOW), "2026-11-13")
        self.assertEqual(S.workday_posted("who knows", NOW), "")

    def test_personio_parses_the_xml_feed(self):
        src = self.add_keyed("personio", board="acme-gmbh")
        self.assertEqual(S.ats_url("personio", src["settings"]), "https://acme-gmbh.jobs.personio.de/xml")
        self.net.routes["https://acme-gmbh.jobs.personio.de/xml"] = (200, "text/xml", PERSONIO)
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        items = {i["title"]: i for i in self.all_items()}
        a = items["Backend Engineer (m/f/d)"]
        self.assertEqual(a["url"], "https://acme-gmbh.jobs.personio.de/job/7001")
        self.assertEqual(a["location"], "Berlin")
        self.assertIn("Build APIs in Python.", a["description"])
        self.assertEqual(a["apply_method"], "ats_form")
        self.assertEqual(a["employment"], "full-time")
        self.assertEqual(items["Intern"]["employment"], "part-time")
        self.net.routes["https://acme-gmbh.jobs.personio.de/xml"] = (200, "text/xml", "<html>nope")
        self.assertIn("not a readable Personio feed", self.client.post(f"/api/oppsources/sources/{src['id']}/test").json()["error"])
        with self.assertRaises(ValueError):
            S.parse_personio("<!DOCTYPE x [<!ENTITY a 'b'>]><workzag-jobs/>", {"board": "x"})

    def test_breezy_parses_the_json_list(self):
        src = self.add_keyed("breezy", board="acme")
        self.assertEqual(S.ats_url("breezy", src["settings"]), "https://acme.breezy.hr/json")
        self.net.routes["https://acme.breezy.hr/json"] = (200, "application/json; charset=utf-8", json.dumps(BREEZY))
        res = self.run_src(src)
        self.assertEqual(res["state"], "ok", res)
        it = self.all_items()[0]
        self.assertEqual((it["title"], it["company"], it["work_mode"], it["url"]), ("Data Analyst", "Acme", "remote", "https://acme.breezy.hr/p/b1-data-analyst"))
        self.assertEqual((it["pay"]["min"], it["pay"]["unit"]), (21, "hour"))
        self.assertEqual(it["apply_method"], "ats_form")
        self.net.routes["https://acme.breezy.hr/json"] = (200, "application/json", '{"not": "a list"}')
        self.assertIn("not a Breezy", self.client.post(f"/api/oppsources/sources/{src['id']}/test").json()["error"])

    def test_detect_ats_knows_the_new_systems(self):
        self.assertEqual(S.detect_ats("https://acme.jobs.personio.de/"), {"adapter": "personio", "board": "acme"})
        self.assertEqual(S.detect_ats("https://acme.breezy.hr/"), {"adapter": "breezy", "board": "acme"})
        self.assertEqual(S.apply_method("https://acme.wd5.myworkdayjobs.com/x/job/1", "workday", "job", "", ""), "ats_form")

    def test_new_sources_never_touch_the_never_automate_sites(self):
        for host in ("linkedin.com", "indeed.com", "naukri.com", "wellfound.com"):
            self.assertIn(host, S.NEVER_AUTOMATE)
        for pid in ("adzuna", "jooble", "findwork", "workday", "personio", "breezy"):
            self.assertFalse(S.preset_view(S.PRESET_BY_ID[pid])["auto_submit"])


class GhostSignals(Base):
    def test_reopened_long_open_vague_and_no_reply(self):
        s = self.add()
        row = {"title": "Platform Engineer", "company": "Zed", "url": "https://z.example/1", "description": "Some role.", "published": "2026-09-20T00:00:00Z"}
        self.config.oppsources_stale_days = 365
        self.eng.process(s, [row], complete=True)
        it = self.all_items()[0]
        codes = [x["code"] for x in it["signals"]]
        self.assertIn("long_open", codes)                      # posted 55 days before NOW
        self.assertIn("vague", codes)                          # no salary, team or location
        self.assertIn("ghost", codes)
        self.assertIn("Likely ghost", next(x["label"] for x in it["signals"] if x["code"] == "ghost"))
        self.eng.process(s, [], complete=True)                 # it closes
        self.assertEqual(self.all_items()[0]["status"], "closed")
        self.now += 86400
        self.eng.process(s, [row])                             # the same company and title is listed again
        it = self.all_items()[0]
        self.assertEqual(it["reopened"], 1)
        self.assertIn("reopened", [x["code"] for x in it["signals"]])

    def test_a_fresh_specific_posting_has_no_ghost_label(self):
        s = self.add()
        row = {"title": "Platform Engineer", "company": "Zed", "url": "https://z.example/2", "published": "2026-11-13T00:00:00Z",
               "description": "Join our payments team in Pune. $120k per year. You will report to the engineering manager.", "location": "Pune"}
        self.eng.process(s, [row])
        codes = [x["code"] for x in self.all_items()[0]["signals"]]
        self.assertNotIn("ghost", codes)
        self.assertNotIn("vague", codes)

    def test_company_that_never_replies_comes_from_the_job_tracker(self):
        from hub import jobtrack
        tracker = jobtrack.engine_for(self.ctx)
        tracker.add({"company": "Zed Inc", "role": "Engineer", "applied_at": "2026-09-01"})
        tracker.add({"company": "Zed", "role": "Developer", "applied_at": "2026-09-10"})
        s = self.add()
        row = {"title": "Staff Engineer", "company": "Zed", "url": "https://z.example/3", "published": "2026-11-13T00:00:00Z",
               "description": "Join our platform team in Pune. $150k per year."}
        self.eng.process(s, [row])
        sig = self.all_items()[0]["signals"]
        self.assertIn("noreply", [x["code"] for x in sig])
        self.assertIn("ghost", [x["code"] for x in sig])

    def test_a_reply_clears_the_no_reply_flag(self):
        from hub import jobtrack
        tracker = jobtrack.engine_for(self.ctx)
        a, _ = tracker.add({"company": "Zed", "role": "Engineer", "applied_at": "2026-09-01"})
        tracker.add({"company": "Zed", "role": "Developer", "applied_at": "2026-09-10"})
        tracker.set_status(a["id"], "replied")
        s = self.add()
        self.eng.process(s, [{"title": "Staff Engineer", "company": "Zed", "url": "https://z.example/4", "published": "2026-11-13T00:00:00Z",
                              "description": "Join our platform team in Pune. $150k per year."}])
        self.assertNotIn("noreply", [x["code"] for x in self.all_items()[0]["signals"]])


class SettingsKeys(unittest.TestCase):
    def test_settings_keys_registered(self):
        from hub import settings
        for key in ("oppsources_enabled", "oppsources_interval_min", "oppsources_watch_interval_min", "oppsources_stale_days", "oppsources_ghost_days"):
            self.assertIn(key, settings.ALLOWED_KEYS)
        c, _ = make_config(data_dir=tempfile.mkdtemp())
        self.assertEqual((c.oppsources_enabled, c.oppsources_watch_interval_min, c.oppsources_stale_days), (False, 10, 45))


if __name__ == "__main__":
    unittest.main()
