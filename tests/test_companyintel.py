"""Company research brief (hub/companyintel.py). Fakes only: a scripted web (httpx.MockTransport), a fake resolver, a fake clock, a fake model.
Rules under test:
- only public pages are read, politely (a cap on requests), through the guarded fetcher; forbidden hosts are refused by name and never requested;
- the summary, hints and red flags come from plain code; the AI step is off by default and falls back;
- a brief is cached for 7 days and `refresh` reads again; nothing is sent."""
import asyncio
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import companyintel as CI, oppsources as S
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 20, 9, 0)
PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731
SITE = "https://acme.example/"
HOME = """<html><head><title>Acme Corp</title><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>
<nav><a href="/about">About us</a><a href="/careers">Careers</a><a href="/blog/">Engineering blog</a>
<a href="https://www.glassdoor.com/Overview/acme">Reviews</a><a href="https://other.example/about">Partner</a></nav>
<p>Acme Corp makes inventory tools for small shops and ships a new release every week.</p></body></html>"""
ABOUT = """<html><body><h1>About Acme</h1><p>Acme Corp was founded in 2012 and is headquartered in Pune, India. We are a team of 150 people building inventory
software for small shops across the country, and we are backed by Northwind Capital.</p>
<p>In 2024 we raised $20 million in a Series B round to grow the product team.</p></body></html>"""
CAREERS = "<html><body><h1>Careers</h1><p>We are hiring backend engineers and designers across two offices today.</p></body></html>"
BLOG = "<html><body><h1>Blog</h1><p>How we moved our reports to a nightly job and cut the database load by half this year.</p></body></html>"


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>https://news.example/{i}</link><pubDate>{d}</pubDate><source>Daily News</source></item>" for i, (t, d) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class Net:
    def __init__(self):
        self.routes, self.requests = {}, []

    def handler(self, request: httpx.Request):
        self.requests.append(str(request.url))
        item = self.routes.get(str(request.url))
        if item is None:
            return httpx.Response(404)
        status, ctype, body = item
        return httpx.Response(status, headers={"content-type": ctype}, content=body.encode())

    def hosts(self):
        return {u.split("/")[2] for u in self.requests}


class FakeModel:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, system, user, json_mode=False):
        self.calls.append(user)
        if not self.replies:
            raise RuntimeError("model down")
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)


async def nosleep(_s):
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ci-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.now = NOW.timestamp()
        self.model = None
        self.net = Net()
        self.net.routes.update({SITE: (200, "text/html", HOME), SITE + "about": (200, "text/html", ABOUT), SITE + "careers": (200, "text/html", CAREERS),
                                SITE + "blog/": (200, "text/html", BLOG),
                                SITE + "feed.xml": (200, "application/rss+xml", rss(("Acme Corp opens a new office in Pune", "Mon, 09 Nov 2026 08:00:00 GMT"),
                                                                        ("Acme Corp announces layoffs in its sales team", "Wed, 04 Nov 2026 08:00:00 GMT"), ("Release notes for October", "Mon, 02 Nov 2026 08:00:00 GMT")))})
        self.ctx = Ctx(config=self.config, model_for=lambda p: self.model, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)

    def svc(self):
        fetcher = S.Fetch(resolver=PUBLIC, transport=httpx.MockTransport(self.net.handler), clock=lambda: self.now, sleep=nosleep, gap=0)
        return CI.CompanyIntel(self.ctx, fetcher=fetcher, clock=lambda: self.now, model_fn=lambda p: self.model)

    def put_items(self, items):
        (Path(self.config.data_path())).mkdir(parents=True, exist_ok=True)
        (Path(self.config.data_path()) / "oppsources_items.json").write_text(json.dumps({i["id"]: i for i in items}), encoding="utf-8")

    def brief(self, svc=None, **body):
        return asyncio.run((svc or self.svc()).build({"company": "Acme Corp", "site": SITE, **body}))


class BriefTests(Base):
    def test_brief_is_built_from_the_public_pages_by_plain_code(self):
        b = self.brief()
        self.assertEqual(b["company"], "Acme Corp")
        self.assertIn("inventory", b["about"])
        kinds = {p["kind"] for p in b["pages"]}
        self.assertEqual(kinds, {"home", "about", "careers", "blog"})
        self.assertEqual(b["hints"]["founded"], "2012")
        self.assertIn("Pune", b["hints"]["place"])
        self.assertTrue(any("150" in s for s in b["hints"]["size"]))
        self.assertTrue(any("Series B" in s for s in b["hints"]["funding"]))
        self.assertEqual(b["summary_by"], "plain code")
        self.assertIn("founded 2012", b["summary"])
        self.assertFalse(b["cached"])
        self.assertFalse(b["sends"])

    def test_news_and_red_flags(self):
        b = self.brief()
        titles = [n["title"] for n in b["news"]]
        self.assertIn("Acme Corp opens a new office in Pune", titles)
        self.assertIn("Release notes for October", titles)             # the company's own feed, found on its home page
        self.assertEqual(b["news"][0]["date"], "2026-11-09")           # newest first
        flags = [f for f in b["red_flags"] if f["id"] == "news"]
        self.assertEqual(len(flags), 1)
        self.assertIn("layoffs", flags[0]["text"])

    def test_old_news_is_dropped(self):
        self.net.routes[SITE + "feed.xml"] = (200, "application/rss+xml", rss(("Acme Corp announces layoffs", "Mon, 01 Jan 2024 08:00:00 GMT")))
        b = self.brief()
        self.assertFalse([f for f in b["red_flags"] if f["id"] == "news"])

    def test_postings_already_in_the_hub_reposts_and_old_ones(self):
        old = self.now - 90 * 86400
        items = [{"id": f"{i:016x}", "title": "Backend Engineer", "company": "Acme Corp Pvt Ltd", "url": "https://x.example/%d" % i, "status": "new",
                  "posted_ts": old, "source": "Board", "pay_label": ""} for i in range(3)]
        items.append({"id": "f" * 16, "title": "Designer", "company": "Other Co", "status": "new", "posted_ts": old})
        self.put_items(items)
        b = self.brief()
        self.assertEqual(b["postings"]["count"], 3)
        self.assertEqual(b["postings"]["oldest_days"], 90)
        ids = {f["id"] for f in b["red_flags"]}
        self.assertIn("reposts", ids)
        self.assertIn("old", ids)

    def test_tracker_rows_for_the_company(self):
        Path(self.config.data_path()).mkdir(parents=True, exist_ok=True)
        (Path(self.config.data_path()) / "jobtrack.json").write_text(json.dumps({"records": [
            {"id": "a" * 12, "company": "Acme Corp", "role": "Backend Engineer", "status": "interview", "applied_at": "2026-11-01"},
            {"id": "b" * 12, "company": "Zed", "role": "x", "status": "applied", "applied_at": "2026-11-01"}]}), encoding="utf-8")
        b = self.brief()
        self.assertEqual([t["status"] for t in b["tracker"]], ["interview"])

    def test_without_a_site_only_hub_data_is_used_and_google_news_is_never_asked(self):
        b = asyncio.run(self.svc().build({"company": "Acme Corp"}))
        self.assertFalse(b["pages"])
        self.assertTrue(any("website" in n for n in b["notes"]))
        self.assertFalse(b["news"])
        self.assertNotIn("news.google.com", self.net.hosts())
        self.assertNotIn("acme.example", " ".join(self.net.requests))

    def test_a_page_that_cannot_be_read_is_noted_not_fatal(self):
        del self.net.routes[SITE]
        b = self.brief()
        self.assertFalse(b["pages"])
        self.assertTrue(any(not s["ok"] for s in b["sources"]))
        self.assertFalse(b["news"])   # no home page, so no announced feed


class ForbiddenTests(Base):
    def test_forbidden_hosts_are_refused_by_name(self):
        for url in ("https://www.linkedin.com/company/acme", "https://glassdoor.com/Overview/acme", "https://www.levels.fyi/companies/acme",
                    "https://in.indeed.com/cmp/acme", "https://www.naukri.com/acme-jobs", "https://wellfound.com/company/acme", "https://x.com/acme"):
            self.assertTrue(CI.refused(url), url)
        self.assertEqual(CI.refused("https://acme.example/"), "")
        self.assertTrue(CI.refused("ftp://acme.example/"))
        self.assertTrue(CI.refused("acme"))

    def test_typing_a_forbidden_site_is_an_error_and_nothing_is_requested(self):
        with self.assertRaises(CI.IntelError):
            asyncio.run(self.svc().build({"company": "Acme Corp", "site": "https://www.linkedin.com/company/acme"}))
        self.assertEqual(self.net.requests, [])

    def test_forbidden_links_on_the_site_are_never_followed(self):
        b = self.brief()
        self.assertNotIn("www.glassdoor.com", self.net.hosts())
        self.assertNotIn("other.example", self.net.hosts())             # another site's page is not this company's about page
        self.assertTrue(all(CI.refused(u) == "" for u in self.net.requests))
        self.assertTrue(b["pages"])

    def test_a_forbidden_feed_is_refused_and_recorded(self):
        self.net.routes[SITE] = (200, "text/html", HOME.replace("/feed.xml", "https://www.linkedin.com/feed.xml"))
        b = self.brief()
        self.assertTrue(any("linkedin.com" in r["url"] for r in b["refused"]))
        self.assertNotIn("www.linkedin.com", self.net.hosts())

    def test_private_addresses_are_blocked_by_the_guard(self):
        b = asyncio.run(self.svc().build({"company": "Acme Corp", "site": "http://127.0.0.1/"}))
        self.assertFalse(b["pages"])
        self.assertTrue(any(not s["ok"] for s in b["sources"]))
        self.assertNotIn("127.0.0.1", self.net.hosts())

    def test_site_from_posting_address(self):
        self.assertEqual(CI.site_from("https://careers.acme.example/jobs/12"), "https://careers.acme.example/")
        self.assertEqual(CI.site_from("https://boards.greenhouse.io/acme/jobs/1"), "")
        self.assertEqual(CI.site_from("https://www.linkedin.com/jobs/view/1"), "")


class PoliteTests(Base):
    def test_robots_txt_is_honoured(self):
        self.net.routes[SITE + "robots.txt"] = (200, "text/plain", "User-agent: *\nDisallow: /\n")
        b = self.brief()
        self.assertFalse(b["pages"])
        self.assertTrue(any("robots" in s["note"] for s in b["sources"]))

    def test_a_brief_makes_a_small_number_of_requests(self):
        self.brief()
        real = [u for u in self.net.requests if not u.endswith("robots.txt")]
        self.assertLessEqual(len(real), CI.MAX_FETCHES)

    def test_the_cap_stops_early(self):
        old = CI.MAX_FETCHES
        CI.MAX_FETCHES = 2
        self.addCleanup(setattr, CI, "MAX_FETCHES", old)
        b = self.brief()
        self.assertTrue(any("polite" in n for n in b["notes"]))
        self.assertLessEqual(len([u for u in self.net.requests if not u.endswith("robots.txt")]), 2)


class CacheTests(Base):
    def test_cached_for_seven_days_then_read_again(self):
        svc = self.svc()
        first = self.brief(svc)
        n = len(self.net.requests)
        self.assertGreater(n, 0)
        again = self.brief(svc)
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.net.requests), n)                 # nothing was fetched
        self.assertEqual(again["summary"], first["summary"])
        self.now += 6 * 86400
        self.assertTrue(self.brief(svc)["cached"])
        self.now += 2 * 86400                                       # eight days after the first read
        fresh = self.brief(svc)
        self.assertFalse(fresh["cached"])
        self.assertGreater(len(self.net.requests), n)

    def test_refresh_reads_again(self):
        svc = self.svc()
        self.brief(svc)
        n = len(self.net.requests)
        self.assertFalse(self.brief(svc, refresh=True)["cached"])
        self.assertGreater(len(self.net.requests), n)

    def test_cache_is_per_company_and_can_be_forgotten(self):
        svc = self.svc()
        self.brief(svc)
        self.assertIsNotNone(svc.cached("Acme Corp"))
        self.assertIsNone(svc.cached("Other Co"))
        self.assertEqual(svc.forget("Acme Corp"), 1)
        self.assertIsNone(svc.cached("Acme Corp"))

    def test_the_cache_holds_only_public_text(self):
        self.brief()
        raw = (Path(self.config.data_path()) / "companyintel.json").read_text(encoding="utf-8")
        self.assertIn("Acme", raw)
        self.assertNotIn("token", raw.lower())


class AiTests(Base):
    def test_ai_is_off_by_default(self):
        self.model = FakeModel({"summary": "x"})
        self.brief()
        self.assertEqual(self.model.calls, [])

    def test_ai_summary_when_asked_and_only_public_text_is_sent(self):
        self.model = FakeModel({"summary": "Acme makes inventory tools and is growing."})
        b = self.brief(ai=True)
        self.assertEqual(b["summary"], "Acme makes inventory tools and is growing.")
        self.assertIn("AI", b["summary_by"])
        self.assertIn("Series B", self.model.calls[0])
        self.assertNotIn("Sam", self.model.calls[0])

    def test_ai_failure_falls_back_to_the_plain_summary(self):
        self.model = FakeModel()                                     # raises
        b = self.brief(ai=True)
        self.assertEqual(b["summary_by"], "plain code")
        self.assertIn("failed", b["ai_note"])
        self.assertIn("founded 2012", b["summary"])

    def test_no_model_is_a_note(self):
        self.model = None
        b = self.brief(ai=True)
        self.assertIn("No AI model", b["ai_note"])


class RouteTests(Base):
    def client(self, authed=True):
        app = FastAPI()
        fetcher = S.Fetch(resolver=PUBLIC, transport=httpx.MockTransport(self.net.handler), clock=lambda: self.now, sleep=nosleep, gap=0)
        app.include_router(CI.make_router(self.ctx, fetcher=fetcher, clock=lambda: self.now, model_fn=lambda p: self.model))
        self.app = app
        c = TestClient(app, cookies={"hub_session": "yes"} if authed else {})
        self.addCleanup(c.close)
        return c

    def test_auth_and_round_trip(self):
        self.assertEqual(self.client(False).post("/api/companyintel/brief", json={"company": "Acme Corp"}).status_code, 401)
        c = self.client()
        r = c.post("/api/companyintel/brief", json={"company": "Acme Corp", "site": SITE})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("summary", r.json())
        o = c.get("/api/companyintel").json()
        self.assertEqual(o["briefs"][0]["company"], "Acme Corp")
        self.assertEqual(o["cache_days"], 7)
        self.assertEqual(c.delete("/api/companyintel/cache", params={"company": "Acme Corp"}).json()["deleted"], 1)

    def test_errors_are_plain(self):
        c = self.client()
        self.assertEqual(c.post("/api/companyintel/brief", json={}).status_code, 400)
        self.assertEqual(c.post("/api/companyintel/brief", json={"company": "Acme", "site": "https://www.glassdoor.com/x"}).status_code, 400)
        self.assertEqual(c.post("/api/companyintel/brief", json={"application_id": "nope"}).status_code, 404)

    def test_switched_off(self):
        self.config.companyintel_enabled = False
        self.assertEqual(self.client().get("/api/companyintel").status_code, 404)

    def test_no_send_route(self):
        from tests.apiwalk import flat_routes
        c = self.client()
        for p, _m, _e in flat_routes(self.app):
            self.assertNotRegex(p, r"/(send|post|publish|submit)\b")


class WiringTests(Base):
    def test_wiring_settings_and_tool(self):
        from hub import server, settings
        from hub.assistant import TOOL_GROUPS
        self.assertIn("companyintel", server.FEATURE_MODULES)
        for key in ("companyintel_enabled", "companyintel_ai"):
            self.assertIn(key, settings.ALLOWED_KEYS)
        names, rx = TOOL_GROUPS["companyintel"]
        self.assertIn("company_brief", names)
        self.assertTrue(rx.search("give me a company brief for Acme"))
        self.assertFalse(self.config.companyintel_ai)

    def test_tool_round_trip_and_flags(self):
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        fetcher = S.Fetch(resolver=PUBLIC, transport=httpx.MockTransport(self.net.handler), clock=lambda: self.now, sleep=nosleep, gap=0)
        app = FastAPI()
        app.include_router(CI.make_router(open_ctx, fetcher=fetcher, clock=lambda: self.now))
        self.config.session_generation = 0
        tool = CI.build(lambda: app, self.config)[0]
        self.assertFalse(tool.needs_approval({}))
        self.assertTrue(tool.untrusted({}))
        ok, text = asyncio.run(tool.run({"company": "Acme Corp", "site": SITE}))
        self.assertTrue(ok, text)
        self.assertIn("layoffs", text)
        self.assertIn("Nothing was sent", text)
        ok, _ = asyncio.run(tool.run({}))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
