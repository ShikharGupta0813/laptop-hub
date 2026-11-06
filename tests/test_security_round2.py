"""Security regressions for the round-2 modules (found in the round-2 security review).

Each test names the hole it closes. No network, no real model.
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpcore
from fastapi.testclient import TestClient

from hub import auth, inbox, kb, notify, study, watch
from hub import config as cfg
from hub.common import InProcessClient
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config


def app_client(config, *, config_path=None, audit=None, login=True):
    deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
    app = create_app(config, deps, audit_path=audit, config_path=config_path)
    client = TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"})
    if login:
        assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
    return app, client


# ---- regex / parsing denial of service ---------------------------------------------------------------

class QuadraticParsing(unittest.TestCase):
    def test_a_huge_crafted_from_header_does_not_stall_the_inbox(self):
        # The address fallback regex was quadratic: 30 KB took ~1.5 s, 100 KB over 15 s, per mail.
        started = time.perf_counter()
        name, addr = inbox.parse_address("9" * 30_000)
        self.assertLess(time.perf_counter() - started, 0.3)
        self.assertEqual(addr, "")
        self.assertEqual(inbox.parse_address("Ann <ann@example.com>"), ("Ann", "ann@example.com"))

    def test_a_very_long_model_reason_is_cleaned_quickly(self):
        # watch.plain() runs a link regex that is quadratic on "a.a.a."; the model's reason is steerable by mail.
        started = time.perf_counter()
        out = watch.plain("mr." * 20_000, watch.REASON_CHARS)
        self.assertLess(time.perf_counter() - started, 0.3)
        self.assertLessEqual(len(out), watch.REASON_CHARS)
        self.assertEqual(watch.plain("Pay now https://evil.example/x <b>today</b>", 120), "Pay now today")


# ---- watch ingest --------------------------------------------------------------------------------

class WatchIngestBody(unittest.TestCase):
    def test_a_chunked_body_over_the_cap_is_refused(self):
        config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="hub-data-"))
        _, client = app_client(config)

        def chunks():
            for _ in range(10):
                yield b"x" * 10_000          # no Content-Length: the up-front check cannot see it
        r = client.post("/api/watch/ingest", content=chunks(), headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_ingest_needs_login_and_the_app_header(self):
        config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="hub-data-"))
        _, client = app_client(config, login=False)
        self.assertEqual(client.post("/api/watch/ingest", json={"source": "chat", "text": "hi"}).status_code, 401)
        _, client = app_client(config)
        r = client.post("/api/watch/ingest", json={"source": "chat", "text": "hi"}, headers={"X-Hub": ""})
        self.assertEqual(r.status_code, 403)


# ---- the hub's own files -------------------------------------------------------------------------

class FileopsProtectsTheRealConfig(unittest.TestCase):
    def setUp(self):
        data = Path(tempfile.mkdtemp(prefix="hub-data-"))
        self.config, self.tmp = make_config(data_dir=str(data), writable_roots=["docs", "notes", "inbox"])
        # The server was started with --config pointing inside a writable shared folder.
        self.config_path = self.tmp / "docs" / "myhub.json"
        cfg.save(self.config, self.config_path)
        _, self.client = app_client(self.config, config_path=self.config_path, audit=self.tmp / "audit.log")

    def test_the_loaded_hub_json_cannot_be_read_or_overwritten(self):
        r = self.client.get("/api/fileops/read", params={"root": "docs", "path": "myhub.json"})
        self.assertEqual(r.status_code, 403)
        import hashlib
        etag = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        r = self.client.post("/api/fileops/write", json={"root": "docs", "path": "myhub.json",
                                                         "content": '{"scripts": {"x": {"argv": ["calc"]}}}',
                                                         "etag": etag})
        self.assertEqual(r.status_code, 403)
        self.assertIn("token_hash", self.config_path.read_text())

    def test_another_file_cannot_be_renamed_onto_it(self):
        self.config_path.unlink()
        (self.tmp / "docs" / "evil.json").write_text("{}")
        r = self.client.post("/api/fileops/rename", json={"root": "docs", "path": "evil.json",
                                                          "new_name": "myhub.json"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.config_path.exists())

    def test_ordinary_files_next_to_it_still_work(self):
        r = self.client.post("/api/fileops/write", json={"root": "docs", "path": "plain.txt", "content": "ok"})
        self.assertEqual(r.status_code, 200, r.text)


class KbNeverIndexesHubSecrets(unittest.TestCase):
    def test_a_renamed_copy_of_hub_json_is_skipped(self):
        data = Path(tempfile.mkdtemp(prefix="hub-data-"))
        config, tmp = make_config(data_dir=str(data))
        copy = tmp / "docs" / "settings-copy.json"
        cfg.save(config, copy)
        (tmp / "docs" / "notes.md").write_text("# Plain\nNothing secret here at all.\n")
        kb.EMBEDDER = lambda texts, c: [[1.0, 0.0] for _ in texts]
        self.addCleanup(setattr, kb, "EMBEDDER", None)
        store = kb.KB(config)
        store.reindex(embed=False)
        hits = store.search(config.session_secret.hex(), semantic=False) + store.search("token_hash", semantic=False)
        self.assertEqual([h for h in hits if "settings-copy" in h["rel"]], [])
        self.assertEqual(store.file_chunks("docs", "settings-copy.json"), [])
        self.assertTrue(store.file_chunks("docs", "notes.md"))


# ---- notification links --------------------------------------------------------------------------

class NotificationLinks(unittest.TestCase):
    def test_protocol_relative_and_script_links_are_dropped(self):
        for bad in ("//evil.example/x", "/\\evil.example", "\\\\evil", "javascript:alert(1)", "data:text/html,x",
                    "JaVaScRiPt:alert(1)", " javascript:alert(1)"):
            self.assertIsNone(notify._clean_link(bad), bad)
        self.assertEqual(notify._clean_link("reminders"), "reminders")
        self.assertEqual(notify._clean_link("https://example.com/a"), "https://example.com/a")


# ---- SSRF: DNS rebinding -------------------------------------------------------------------------

class FakeInner:
    def __init__(self):
        self.hosts = []

    async def connect_tcp(self, host, port, **kw):
        self.hosts.append(host)
        raise httpcore.ConnectError("fake: nothing listens")

    async def sleep(self, seconds):
        pass


class Rebinder:
    """Public on the first lookup (the check), loopback on the next (the connection)."""

    def __init__(self):
        self.n = 0

    async def __call__(self, host, port):
        self.n += 1
        return ["93.184.216.34"] if self.n == 1 else ["127.0.0.1"]


class PinnedConnections(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_the_socket_goes_to_the_checked_address(self):
        inner = FakeInner()

        async def resolver(host, port):
            return ["93.184.216.34"]
        backend = study.PinnedBackend(resolver, inner)
        with self.assertRaises(httpcore.ConnectError):
            self.run_async(backend.connect_tcp("example.com", 443))
        self.assertEqual(inner.hosts, ["93.184.216.34"])

    def test_a_rebinding_name_is_refused_at_connect_time(self):
        inner = FakeInner()
        resolver = Rebinder()
        transport = study.pinned_transport(resolver, inner)
        with self.assertRaises(study.SourceError) as caught:
            self.run_async(study.safe_fetch("http://rebind.example/", transport=transport, resolver=resolver))
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(inner.hosts, [])          # never dialled 127.0.0.1

    def test_literal_private_addresses_are_refused(self):
        inner = FakeInner()
        backend = study.PinnedBackend(None, inner)
        for host in ("127.0.0.1", "10.0.0.5", "::1", "[::ffff:127.0.0.1]", "169.254.169.254"):
            with self.assertRaises(study.SourceError):
                self.run_async(backend.connect_tcp(host, 80))
        self.assertEqual(inner.hosts, [])

    def test_bookmarks_and_cards_use_the_pinned_fetch(self):
        seen = []

        async def spy(url, **kw):
            seen.append(kw.get("transport"))
            raise study.SourceError(400, "refused by the spy")
        config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="hub-data-"))
        with mock.patch.object(study, "safe_fetch", spy):
            _, client = app_client(config)
            r = client.post("/api/bookmarks", json={"url": "https://example.com/page"})
            self.assertEqual(r.status_code, 400)
            deck = client.post("/api/cards/decks", json={"name": "D"}).json()
            r = client.post(f"/api/cards/decks/{deck['id']}/generate",
                            json={"source": {"type": "url", "url": "https://example.com/x"}})
            self.assertEqual(r.status_code, 400)
        self.assertEqual(len(seen), 2)
        for transport in seen:
            self.assertIsNotNone(transport)
            self.assertIsInstance(transport._pool._network_backend, study.PinnedBackend)


# ---- sessions: log out everywhere ----------------------------------------------------------------

class LogoutAll(unittest.TestCase):
    def setUp(self):
        self.config, self.tmp = make_config(data_dir=tempfile.mkdtemp(prefix="hub-data-"))
        self.path = self.tmp / "hub.json"
        cfg.save(self.config, self.path)
        self.app, self.client = app_client(self.config, config_path=self.path, audit=self.tmp / "audit.log")

    def test_every_session_stops_working_and_the_change_is_saved(self):
        stolen = self.client.cookies.get(auth.COOKIE)
        self.assertEqual(self.client.get("/api/notes").status_code, 200)
        r = self.client.post("/api/logout-all")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["persisted"])
        fresh = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"})
        fresh.cookies.set(auth.COOKIE, stolen)
        self.assertEqual(fresh.get("/api/notes").status_code, 401)
        self.assertFalse(fresh.get("/api/session").json()["authenticated"])
        self.assertEqual(cfg.load(self.path).session_generation, 1)
        self.assertEqual(self.config.session_generation, 1)
        # Logging in again works, and the new cookie is good.
        self.assertEqual(fresh.post("/api/login", json={"token": TOKEN}).status_code, 200)
        self.assertEqual(fresh.get("/api/notes").status_code, 200)
        self.assertIn('"logout_all"', (self.tmp / "audit.log").read_text())

    def test_it_needs_a_login_and_the_app_header(self):
        anon = TestClient(self.app, base_url="http://testserver", headers={"X-Hub": "1"})
        self.assertEqual(anon.post("/api/logout-all").status_code, 401)
        self.assertEqual(self.client.post("/api/logout-all", headers={"X-Hub": ""}).status_code, 403)
        self.assertEqual(cfg.load(self.path).session_generation, 0)

    def test_settings_cannot_change_the_generation(self):
        r = self.client.patch("/api/settings", json={"session_generation": 0})
        self.assertEqual(r.status_code, 400)

    def test_the_hubs_own_inside_calls_keep_working(self):
        self.client.post("/api/logout-all")
        ok, data = asyncio.run(InProcessClient(lambda: self.app, self.config).call("GET", "/api/notes"))
        self.assertTrue(ok, data)

    def test_without_a_config_file_it_still_logs_everyone_out(self):
        config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="hub-data-"))
        app, client = app_client(config)
        cookie = client.cookies.get(auth.COOKIE)
        r = client.post("/api/logout-all")
        self.assertFalse(r.json()["persisted"])
        fresh = TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"})
        fresh.cookies.set(auth.COOKIE, cookie)
        self.assertEqual(fresh.get("/api/notes").status_code, 401)

    def test_a_cookie_from_another_generation_is_refused(self):
        secret = b"s" * 32
        old = auth.make_session(secret, now=1000)
        self.assertTrue(auth.session_valid(secret, old, now=1001))            # generation 0: old format still works
        self.assertFalse(auth.session_valid(secret, old, now=1001, generation=1))
        new = auth.make_session(secret, now=1000, generation=3)
        self.assertTrue(auth.session_valid(secret, new, now=1001, generation=3))
        self.assertFalse(auth.session_valid(secret, new, now=1001, generation=2))
        self.assertFalse(auth.session_valid(secret, new, now=1001))

    def test_the_token_command_still_rotates_and_keeps_the_generation(self):
        self.client.post("/api/logout-all")
        from hub.__main__ import main
        with mock.patch("builtins.print"):
            self.assertEqual(main(["--config", str(self.path), "token"]), 0)
        saved = cfg.load(self.path)
        self.assertNotEqual(saved.session_secret, self.config.session_secret)
        self.assertNotEqual(saved.token_hash, self.config.token_hash)
        self.assertEqual(saved.session_generation, 1)


if __name__ == "__main__":
    unittest.main()
