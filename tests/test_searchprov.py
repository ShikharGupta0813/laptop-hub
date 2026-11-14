"""Search providers (hub/searchprov.py): fallback order and notes, every adapter's parsing, the JSON-API template, the
guards, the routes. Fake transports and fake keyring only: no network."""
import asyncio
import json
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import research, searchprov
from hub.common import Ctx
from hub.provlist import MemorySecrets
from tests.helpers import make_config


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


def row(n):
    return {"title": f"T{n}", "url": f"https://site{n}.example.com/", "snippet": f"s{n}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="sp-test-"))
        self.secrets = MemorySecrets()
        searchprov._BACKOFFS.clear()
        patcher = mock.patch("hub.research.default_resolver", lambda host: ["93.184.216.34"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = searchprov.list_for(self.config, self.secrets)

    def run_search(self, query="q", transport=None, limit=8):
        async def go():                       # one task, like the research job: the outcome travels in a ContextVar
            rows = await searchprov.search(self.config, query, limit, secrets=self.secrets, transport=transport)
            return rows, searchprov.take_outcome()
        return asyncio.run(go())

    def add(self, kind, settings=None, secret=None, label=None):
        body = {"kind": kind, "settings": settings or {}, "label": label or kind}
        if secret:
            body["secret"] = secret
        return self.store.add(body)


class Fallback(Base):
    def test_seeded_with_the_two_builtin_choices(self):
        self.assertEqual([e["kind"] for e in self.store.load()], ["ddg-html", "wikipedia"])

    def test_legacy_backend_setting_seeds_the_list(self):
        config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="sp-test-"), research_search_backend="wikipedia")
        entries = searchprov.list_for(config, self.secrets).load()
        self.assertEqual([(e["kind"], e["enabled"]) for e in entries], [("ddg-html", False), ("wikipedia", True)])

    def test_first_enabled_with_results_wins_and_says_so(self):
        async def ddg(q, n):
            return [row(1)]
        with mock.patch.object(research, "ddg_search", ddg):
            rows, out = self.run_search()
        self.assertEqual(rows[0]["title"], "T1")
        self.assertEqual(out.via, "DuckDuckGo")
        self.assertIn("Searched with DuckDuckGo", out.notes)

    def test_failure_falls_through_with_note_and_then_backs_off(self):
        calls = []

        async def ddg(q, n):
            calls.append("ddg")
            raise httpx.ConnectError("down")

        async def wiki(q, n):
            calls.append("wiki")
            return [row(2)]
        with mock.patch.object(research, "ddg_search", ddg), mock.patch.object(research, "wikipedia_search", wiki):
            rows, out = self.run_search()
            self.assertEqual(out.via, "Wikipedia")
            self.assertTrue(any(n.startswith("DuckDuckGo failed") for n in out.notes), out.notes)
            self.assertIn("Searched with Wikipedia", out.notes)
            rows, out = self.run_search()                      # DuckDuckGo is now skipped with a visible note
        self.assertEqual(calls, ["ddg", "wiki", "wiki"])
        self.assertTrue(any(n.startswith("Skipped DuckDuckGo") for n in out.notes), out.notes)

    def test_empty_result_falls_through_without_penalty(self):
        async def ddg(q, n):
            return []

        async def wiki(q, n):
            return [row(3)]
        with mock.patch.object(research, "ddg_search", ddg), mock.patch.object(research, "wikipedia_search", wiki):
            _rows, out = self.run_search()
            self.assertIn("DuckDuckGo found nothing; trying the next one", out.notes)
            self.assertEqual(searchprov.backoff_for(self.config).blocked_for("duckduckgo"), 0)

    def test_disabled_and_reordered_entries(self):
        self.store.update("duckduckgo", {"enabled": False})
        calls = []

        async def wiki(q, n):
            calls.append("wiki")
            return [row(1)]
        with mock.patch.object(research, "wikipedia_search", wiki):
            _rows, out = self.run_search()
        self.assertEqual((calls, out.via), (["wiki"], "Wikipedia"))
        self.store.update("duckduckgo", {"enabled": True})
        self.store.reorder(["wikipedia", "duckduckgo"])
        self.assertEqual([e["id"] for e in self.store.load()], ["wikipedia", "duckduckgo"])

    def test_everything_skipped_is_still_tried_once(self):
        async def ddg(q, n):
            raise httpx.ConnectError("down")

        async def wiki(q, n):
            raise httpx.ConnectError("down")
        with mock.patch.object(research, "ddg_search", ddg), mock.patch.object(research, "wikipedia_search", wiki):
            self.assertEqual(self.run_search()[0], [])
        worked = []

        async def wiki2(q, n):
            worked.append(1)
            return [row(4)]
        with mock.patch.object(research, "ddg_search", ddg), mock.patch.object(research, "wikipedia_search", wiki2):
            rows, _out = self.run_search()                      # both were in backoff: tried anyway rather than giving up
        self.assertEqual((len(rows), worked), (1, [1]))

    def test_no_provider_on_says_so(self):
        for e in self.store.load():
            self.store.update(e["id"], {"enabled": False})
        rows, out = self.run_search()
        self.assertEqual(rows, [])
        self.assertIn("No search provider is switched on", out.notes[0])


class Adapters(Base):
    def test_searxng_parses_json_and_allows_the_owners_private_instance(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"results": [{"title": "Hit <b>1</b>", "url": "https://a.example.org/x", "content": "about &amp; more"},
                                                         {"title": "no link", "content": "x"}]})
        self.store.reorder([self.add("searxng", {"base_url": "http://127.0.0.1:8888/"})["id"]])
        rows, out = self.run_search("hello world", httpx.MockTransport(handler))
        self.assertEqual(rows, [{"title": "Hit 1", "url": "https://a.example.org/x", "snippet": "about & more"}])
        self.assertEqual(dict(seen[0].url.params), {"q": "hello world", "format": "json"})
        self.assertEqual(seen[0].url.path, "/search")

    def test_brave_uses_the_keyring_token_header(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"web": {"results": [{"title": "B", "url": "https://b.example.org/", "description": "<strong>d</strong>"}]}})
        entry = self.add("brave", secret="BRAVE-KEY")
        self.store.reorder([entry["id"]])
        rows, out = self.run_search("x", httpx.MockTransport(handler))
        self.assertEqual(rows[0]["snippet"], "d")
        self.assertEqual(seen[0].headers["x-subscription-token"], "BRAVE-KEY")
        self.assertEqual(out.via, "brave")

    def test_brave_without_a_key_fails_politely_and_falls_through(self):
        entry = self.add("brave")
        self.store.reorder([entry["id"]])

        async def ddg(q, n):
            return [row(1)]
        with mock.patch.object(research, "ddg_search", ddg):
            _rows, out = self.run_search()
        self.assertIn("brave failed: no API key saved; trying the next one", out.notes)
        self.assertEqual(out.via, "DuckDuckGo")

    def test_json_api_bing_style_header_key_and_field_mapping(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"webPages": {"value": [{"name": "N", "url": "https://n.example.com/", "snippet": "S"}]}})
        preset = next(p for p in searchprov.PRESETS if p["label"].startswith("Bing"))
        entry = self.add("json-api", preset["settings"], secret="BING")
        self.store.reorder([entry["id"]])
        rows, _out = self.run_search("a b&c", httpx.MockTransport(handler), limit=3)
        self.assertEqual(rows, [{"title": "N", "url": "https://n.example.com/", "snippet": "S"}])
        self.assertEqual(seen[0].headers["ocp-apim-subscription-key"], "BING")
        self.assertEqual(seen[0].url.params["q"], "a b&c")             # encoded once, not corrupted
        self.assertEqual(seen[0].url.params["count"], "3")

    def test_json_api_key_in_query_string_and_google_cse_mapping(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"items": [{"title": "G", "link": "https://g.example.com/", "snippet": "gs"}]})
        preset = next(p for p in searchprov.PRESETS if p["label"].startswith("Google"))
        entry = self.add("json-api", preset["settings"], secret="GKEY")
        self.store.reorder([entry["id"]])
        rows, _out = self.run_search("q", httpx.MockTransport(handler))
        self.assertEqual(rows[0]["url"], "https://g.example.com/")
        self.assertEqual(seen[0].url.params["key"], "GKEY")

    def test_json_api_post_body_template_bearer_and_body_key(self):
        seen = []

        def handler(request):
            seen.append((request, json.loads(request.content)))
            return httpx.Response(200, json=[{"headline": "H", "href": "https://h.example.com/", "text": "t"}])
        settings = {"url_template": "https://api.example.com/search", "method": "POST",
                    "body_template": json.dumps({"q": "{query}", "max": "{limit}", "opts": {"lang": "en"}}),
                    "key_in": "body", "key_name": "api_key", "results_path": "", "title_field": "headline",
                    "url_field": "href", "snippet_field": "text"}
        entry = self.add("json-api", settings, secret="K1")
        self.store.reorder([entry["id"]])
        rows, _out = self.run_search("say \"hi\"", httpx.MockTransport(handler), limit=4)
        request, body = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(body, {"q": 'say "hi"', "max": 4, "opts": {"lang": "en"}, "api_key": "K1"})
        self.assertEqual(rows[0]["title"], "H")
        entry2 = self.store.update(entry["id"], {"settings": {"key_in": "bearer"}})
        self.assertEqual(entry2["settings"]["key_in"], "bearer")
        self.run_search("x", httpx.MockTransport(handler))
        self.assertEqual(seen[1][0].headers["authorization"], "Bearer K1")

    def test_get_path_and_map_rows_are_defensive(self):
        data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
        self.assertEqual(searchprov.get_path(data, "a.b.1.c"), 2)
        self.assertIsNone(searchprov.get_path(data, "a.x.y"))
        self.assertIsNone(searchprov.get_path(data, "a.b.9"))
        settings = {"results_path": "r", "title_field": "t", "url_field": "u", "snippet_field": "s"}
        rows = searchprov.map_rows({"r": [{"u": "javascript:alert(1)", "t": "x"}, {"u": "https://user:pw@x.example.com/", "t": "y"},
                                          "junk", {"u": "https://ok.example.com/", "t": 5, "s": None}]}, settings, 10)
        self.assertEqual(rows, [{"title": "5", "url": "https://ok.example.com/", "snippet": ""}])
        self.assertEqual(searchprov.map_rows({"r": "nope"}, settings, 5), [])

    def test_stack_exchange_site(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"items": [{"title": "How to &quot;x&quot;?", "link": "https://stackoverflow.com/q/1",
                                                        "tags": ["python", "asyncio"], "score": 12, "is_answered": True}]})
        entry = self.add("site", {"mode": "stackexchange", "site": "stackoverflow"})
        self.store.reorder([entry["id"]])
        rows, _out = self.run_search("asyncio", httpx.MockTransport(handler))
        self.assertEqual(rows[0]["title"], 'How to "x"?')
        self.assertIn("score 12", rows[0]["snippet"])
        self.assertEqual(seen[0].url.host, "api.stackexchange.com")
        self.assertEqual(seen[0].url.params["site"], "stackoverflow")

    def test_site_domains_restricts_results(self):
        queries = []

        async def ddg(q, n, **kw):
            queries.append(q)
            return [{"title": "d", "url": "https://docs.python.org/3/x", "snippet": ""},
                    {"title": "other", "url": "https://evil.example.com/docs.python.org", "snippet": ""}]
        entry = self.add("site", {"mode": "domains", "domains": "docs.python.org, https://www.mozilla.org/"})
        self.assertEqual(entry["settings"]["domains"], ["docs.python.org", "mozilla.org"])
        self.store.reorder([entry["id"]])
        with mock.patch.object(research, "ddg_search", ddg):
            rows, _out = self.run_search("asyncio")
        self.assertEqual([r["url"] for r in rows], ["https://docs.python.org/3/x"])
        self.assertIn("site:docs.python.org OR site:mozilla.org", queries[0])

    def test_wikipedia_language_setting(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"query": {"search": [{"title": "Haus", "snippet": "x"}]}})
        entry = self.add("wikipedia", {"lang": "de"})
        self.store.reorder([entry["id"]])
        rows, _out = self.run_search("haus", httpx.MockTransport(handler))
        self.assertEqual(rows[0]["url"], "https://de.wikipedia.org/wiki/Haus")
        self.assertEqual(seen[0].url.host, "de.wikipedia.org")


class Guards(Base):
    def test_public_json_api_cannot_reach_a_private_address(self):
        called = []
        entry = self.add("json-api", {"url_template": "http://10.0.0.5/search?q={query}"})
        self.store.reorder([entry["id"]])
        self.store.update("wikipedia", {"enabled": False})

        def handler(request):
            called.append(1)
            return httpx.Response(200, json={})
        async def ddg(q, n):
            return [row(1)]
        with mock.patch.object(research, "ddg_search", ddg):
            _rows, out = self.run_search("x", httpx.MockTransport(handler))
        self.assertEqual(called, [])      # never contacted: DuckDuckGo (patched) is second in the list and answers
        self.assertTrue(any("address not allowed" in n for n in out.notes), out.notes)

    def test_redirects_are_not_followed(self):
        def handler(request):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        entry = self.add("json-api", {"url_template": "https://api.example.com/s?q={query}"})
        self.store.reorder([entry["id"]])
        async def none(q, n):
            return []
        with mock.patch.object(research, "ddg_search", none), mock.patch.object(research, "wikipedia_search", none):
            _rows, out = self.run_search("x", httpx.MockTransport(handler))
        self.assertTrue(any("redirected (not followed)" in n for n in out.notes), out.notes)

    def test_results_are_cleaned_untrusted_text(self):
        def handler(request):
            return httpx.Response(200, json={"results": [{"title": "a\x00b​<script>x</script>", "url": "ftp://x.example.com/", "content": "c"},
                                                         {"title": "ok", "url": "https://ok.example.com/", "content": "z" * 1000}]})
        entry = self.add("searxng", {"base_url": "http://127.0.0.1:8888"})
        self.store.reorder([entry["id"]])
        rows, _out = self.run_search("x", httpx.MockTransport(handler))
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(rows[0]["snippet"]), 300)

    def test_validation(self):
        for kind, settings in [("json-api", {"url_template": "https://x.example.com/s"}),            # no {query}
                               ("json-api", {"url_template": "ftp://x.example.com/?q={query}"}),
                               ("json-api", {"url_template": "https://u:p@x.example.com/?q={query}"}),
                               ("json-api", {"url_template": "https://x.example.com/?q={query}", "key_in": "header", "key_name": "bad header!"}),
                               ("searxng", {"base_url": "not a url"}), ("site", {"mode": "domains", "domains": ""}),
                               ("site", {"mode": "domains", "domains": "not a domain"}), ("wikipedia", {"lang": "../x"})]:
            with self.assertRaises(searchprov.ListError, msg=f"{kind} {settings}"):
                self.store.add({"kind": kind, "settings": settings})
        with self.assertRaises(searchprov.ListError):
            self.store.add({"kind": "nope"})

    def test_secret_is_in_the_keyring_only(self):
        entry = self.add("brave", secret="TOPSECRET")
        self.assertTrue(entry["has_secret"])
        self.assertNotIn("TOPSECRET", json.dumps(self.store.public()))
        self.assertNotIn("TOPSECRET", self.store.path.read_text())
        self.assertEqual(self.secrets.data, {f"search:{entry['id']}": "TOPSECRET"})
        self.store.delete(entry["id"])
        self.assertEqual(self.secrets.data, {})


class Routes(Base):
    def setUp(self):
        super().setUp()
        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(searchprov.make_router(ctx, secrets=self.secrets))
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)

    def test_routes_require_login(self):
        for method, path in [("get", "/api/search-providers"), ("post", "/api/search-providers"),
                             ("put", "/api/search-providers/order"), ("put", "/api/search-providers/x"),
                             ("delete", "/api/search-providers/x"), ("post", "/api/search-providers/x/test"),
                             ("post", "/api/search-providers/search"), ("post", "/api/search-providers/reset")]:
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)

    def test_crud_reorder_and_no_secret_in_responses(self):
        r = self.client.post("/api/search-providers", json={"kind": "brave", "label": "My Brave", "secret": "S3CRET"})
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["entry"]["id"]
        self.assertNotIn("S3CRET", r.text)
        listing = self.client.get("/api/search-providers").json()
        self.assertEqual([e["kind"] for e in listing["entries"]], ["ddg-html", "wikipedia", "brave"])
        self.assertTrue(listing["entries"][2]["has_secret"])
        self.assertNotIn("S3CRET", json.dumps(listing))
        self.assertIn("json-api", listing["kinds"])
        self.client.put("/api/search-providers/order", json={"ids": [ident]})
        self.assertEqual(self.client.get("/api/search-providers").json()["entries"][0]["id"], ident)
        self.assertEqual(self.client.put(f"/api/search-providers/{ident}", json={"enabled": False}).json()["entry"]["enabled"], False)
        self.assertEqual(self.client.put(f"/api/search-providers/{ident}", json={"clear_secret": True}).json()["entry"]["has_secret"], False)
        self.assertEqual(self.client.delete(f"/api/search-providers/{ident}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/search-providers/{ident}").status_code, 404)
        self.assertEqual(self.client.post("/api/search-providers", json={"kind": "json-api", "settings": {}}).status_code, 400)

    def test_test_button_and_try_search(self):
        async def ddg(q, n):
            return [row(1)]
        with mock.patch.object(research, "ddg_search", ddg):
            r = self.client.post("/api/search-providers/duckduckgo/test")
            self.assertEqual((r.json()["ok"], r.json()["count"]), (True, 1))
            r = self.client.post("/api/search-providers/search", json={"query": "hello"})
        self.assertEqual(r.json()["via"], "DuckDuckGo")
        self.assertTrue(r.json()["untrusted"])
        self.assertEqual(self.client.post("/api/search-providers/nope/test").status_code, 404)


class ResearchUsesTheList(Base):
    def test_researcher_search_goes_through_the_list_and_leaves_a_note(self):
        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda *a, **k: None)

        async def ddg(q, n):
            raise httpx.ConnectError("down")

        async def wiki(q, n):
            return [row(7)]
        engine = research.Researcher(ctx)
        async def go():
            return await engine._configured_search("q"), research._take_search_outcome()
        with mock.patch.object(research, "ddg_search", ddg), mock.patch.object(research, "wikipedia_search", wiki):
            rows, out = asyncio.run(go())
        self.assertEqual(rows[0]["title"], "T7")
        self.assertIn("Searched with Wikipedia", out.notes)


if __name__ == "__main__":
    unittest.main()
