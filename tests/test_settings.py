"""Tests for the SETTINGS module: the allow-listed config editor, audit viewer, backups, diagnostics.
Ollama is answered by a fake transport; nothing here touches the network or a real model."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import auth, settings
from hub import config as cfg
from hub.common import Ctx
from tests.helpers import make_config

FAKE_KEY = "sk-ant-THIS-MUST-NEVER-LEAK-123"
TOPIC = "abcdefgh12345678"


def ollama_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/tags":
        return httpx.Response(200, json={"models": [
            {"name": "qwen3:4b", "model": "qwen3:4b", "size": 2_500_000_000, "modified_at": "2026-10-01T10:00:00Z",
             "details": {"parameter_size": "4B"}}]})
    if path == "/api/pull":
        lines = [{"status": "pulling manifest"}, {"status": "pulling abc", "total": 200, "completed": 50},
                 {"status": "pulling abc", "total": 200, "completed": 200}, {"status": "success"}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
    if path == "/api/generate":
        body = json.loads(request.content)
        if body["model"] == "missing:1b":
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json={"response": "ready"})
    return httpx.Response(404)


def ollama_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused")


class Base(unittest.TestCase):
    handler = staticmethod(ollama_handler)
    with_config_file = True

    def setUp(self):
        self.config, self.root = make_config(data_dir="")
        self.config.data_dir = str(self.root / "data")
        self.config.ntfy_topic = TOPIC
        self.config.jarvis_dir = str(self.root / "jarvis-secret-folder")
        self.config_path = self.root / "hub.json"
        if self.with_config_file:
            cfg.save(self.config, self.config_path)
        self.audit_path = self.root / "audit.log"
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY, "HUB_CONFIG": str(self.root / "no-such.json")})
        env.start()
        self.addCleanup(env.stop)
        self.build()

    def build(self):
        app = FastAPI()

        def need_auth(request: Request):
            if not auth.session_valid(self.config.session_secret, request.cookies.get(auth.COOKIE)):
                raise HTTPException(401, "Log in first.")

        def audit(event, **fields):
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"time": "2026-11-04T10:00:00", "event": event, **fields}) + "\n")

        self.ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=audit, get_app=lambda: app, audit_path=self.audit_path)
        if self.with_config_file:
            self.ctx.config_path = self.config_path
        app.include_router(settings.make_router(self.ctx, transport=httpx.MockTransport(self.handler)))
        self.client = TestClient(app, base_url="http://testserver")
        self.addCleanup(self.client.close)
        self.client.cookies.set(auth.COOKIE, auth.make_session(self.config.session_secret))

    def audit_rows(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines() if line.strip()]

    def disk(self):
        return json.loads(self.config_path.read_text())


class LoginTest(Base):
    def test_everything_needs_a_login(self):
        self.client.cookies.clear()
        for method, path in [("get", ""), ("patch", ""), ("get", "/models"), ("post", "/models/pull"), ("post", "/models/test"),
                             ("get", "/diagnostics"), ("get", "/audit"), ("get", "/audit/events"), ("post", "/backup"),
                             ("get", "/backups"), ("get", "/backups/hub-backup-20260101-000000.zip"), ("get", "/about")]:
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)("/api/settings" + path).status_code, 401)


class ReadTest(Base):
    def test_shape(self):
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["chat_model"], "qwen3:4b")
        self.assertEqual(data["writable_roots"], ["notes", "inbox"])
        self.assertIn({"name": "docs", "exists": True}, data["roots"])
        self.assertTrue(data["ntfy_topic_set"])
        self.assertTrue(data["jarvis_dir_set"])
        self.assertTrue(data["has_claude_key"])
        self.assertEqual(data["scripts"], ["hello"])
        self.assertEqual(data["shells"], ["sh"])
        self.assertEqual(data["ollama"], {"reachable": True, "models": ["qwen3:4b"]})
        self.assertFalse(data["data_dir_exists"])
        self.assertTrue(data["config_persistable"])
        for key in ("version", "platform", "kb_roots", "ntfy_server", "apps", "embed_model", "whisper_model"):
            self.assertIn(key, data)

    def test_no_secret_ever_appears(self):
        self.config.allowed_origins = ["https://origin-secret.example"]
        text = self.client.get("/api/settings").text + self.client.get("/api/settings/about").text \
            + self.client.get("/api/settings/diagnostics").text + self.client.get("/api/settings/models").text
        for secret in (self.config.token_hash, self.config.session_secret.hex(), FAKE_KEY, TOPIC,
                       "jarvis-secret-folder", "origin-secret.example", str(self.root)):
            self.assertNotIn(secret, text)

    def test_ollama_down_is_friendly(self):
        self.handler = ollama_down
        self.build()
        data = self.client.get("/api/settings").json()
        self.assertEqual(data["ollama"], {"reachable": False, "models": []})
        models = self.client.get("/api/settings/models").json()
        self.assertFalse(models["reachable"])
        self.assertIn("Ollama", models["error"])

    def test_models_lists_installed(self):
        data = self.client.get("/api/settings/models").json()
        self.assertEqual(data["models"][0]["name"], "qwen3:4b")
        self.assertTrue(data["configured"]["chat_model"]["installed"])
        self.assertFalse(data["configured"]["embed_model"]["installed"])

    def test_about(self):
        data = self.client.get("/api/settings/about").json()
        self.assertTrue({"version", "uptime_s", "python", "platform"} <= set(data))


class PatchTest(Base):
    def patch(self, body):
        return self.client.patch("/api/settings", json=body)

    def test_locked_keys_are_rejected_with_400(self):
        before = self.disk()
        locked = {"token_hash": "x" * 64, "session_secret": "00" * 32, "allowed_hosts": ["evil.example"],
                  "allowed_origins": ["https://evil.example"], "scripts": {"x": {"argv": ["rm", "-rf", "/"]}},
                  "apps": {"x": {"target": "calc.exe"}}, "shells": {"x": ["bash"]}, "jarvis_dir": "/tmp"}
        for key, value in locked.items():
            with self.subTest(key=key):
                response = self.patch({key: value})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("hub.json", response.json()["detail"])
        # mixed with a valid key: nothing at all is applied
        response = self.patch({"chat_model": "llama3:8b", "token_hash": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.config.chat_model, "qwen3:4b")
        self.assertEqual(self.disk(), before)
        self.assertEqual(self.config.allowed_hosts, ["testserver"])

    def test_unknown_and_empty_bodies(self):
        self.assertEqual(self.patch({"port": 1}).status_code, 400)
        self.assertEqual(self.patch({"host": "0.0.0.0"}).status_code, 400)
        self.assertEqual(self.patch({}).status_code, 400)
        self.assertEqual(self.patch({"confirm": True}).status_code, 400)
        self.assertEqual(self.patch([1]).status_code, 400)

    def test_models_are_validated(self):
        for bad in ["", "a b", "../x", "x;rm", "x" * 81, 5, None, "a\nb"]:
            with self.subTest(bad=bad):
                self.assertEqual(self.patch({"chat_model": bad}).status_code, 400)
        response = self.patch({"chat_model": "llama3:8b", "embed_model": "mxbai-embed-large", "whisper_model": "small",
                               "claude_model": "claude-sonnet-5-5", "assistant_model": "claude-sonnet-5-5"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.config.chat_model, "llama3:8b")
        self.assertEqual(response.json()["chat_model"], "llama3:8b")

    def test_persisted_to_the_file_and_login_survives(self):
        old_hash, old_secret = self.config.token_hash, self.config.session_secret
        response = self.patch({"chat_model": "llama3:8b", "kb_roots": ["docs"]})
        self.assertTrue(response.json()["persisted"])
        saved = self.disk()
        self.assertEqual(saved["chat_model"], "llama3:8b")
        self.assertEqual(saved["kb_roots"], ["docs"])
        self.assertEqual(saved["token_hash"], old_hash)
        self.assertEqual(bytes.fromhex(saved["session_secret"]), old_secret)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_file_edits_by_hand_are_not_overwritten(self):
        data = self.disk()
        data["allowed_hosts"] = ["hand-edited.example"]
        self.config_path.write_text(json.dumps(data))
        self.patch({"chat_model": "llama3:8b"})
        self.assertEqual(self.disk()["allowed_hosts"], ["hand-edited.example"])

    def test_roots_must_exist(self):
        self.assertEqual(self.patch({"kb_roots": ["nope"]}).status_code, 400)
        self.assertEqual(self.patch({"kb_roots": "docs"}).status_code, 400)
        self.assertEqual(self.patch({"kb_roots": [1]}).status_code, 400)
        self.assertEqual(self.patch({"writable_roots": ["../etc"], "confirm": True}).status_code, 400)
        self.assertEqual(self.patch({"kb_roots": ["docs", "docs", "notes"]}).json()["kb_roots"], ["docs", "notes"])

    def test_adding_a_writable_root_needs_confirm(self):
        response = self.patch({"writable_roots": ["notes", "inbox", "docs"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.config.writable_roots, ["notes", "inbox"])
        self.assertEqual(self.disk()["writable_roots"], ["notes", "inbox"])
        for not_true in ("yes", 1, "true", None):
            self.assertEqual(self.patch({"writable_roots": ["notes", "inbox", "docs"], "confirm": not_true}).status_code, 409)
        ok = self.patch({"writable_roots": ["notes", "inbox", "docs"], "confirm": True})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.config.writable_roots, ["notes", "inbox", "docs"])
        self.assertEqual(self.disk()["writable_roots"], ["notes", "inbox", "docs"])

    def test_removing_a_writable_root_needs_no_confirm(self):
        self.assertEqual(self.patch({"writable_roots": ["notes"]}).status_code, 200)
        self.assertEqual(self.config.writable_roots, ["notes"])

    def test_cannot_make_a_missing_folder_writable(self):
        self.config.roots["ghost"] = str(self.root / "does-not-exist")
        self.assertEqual(self.patch({"writable_roots": ["notes", "ghost"], "confirm": True}).status_code, 400)

    def test_ntfy_server_and_topic(self):
        for bad in ["http://ntfy.sh", "ftp://x.example", "https://", "https://u:p@ntfy.sh", "https://ntfy.sh/?a=1",
                    "https://ntfy .sh", "javascript:alert(1)", "https://ntfy.sh:notaport", 5, "https://" + "a" * 300]:
            with self.subTest(bad=bad):
                self.assertEqual(self.patch({"ntfy_server": bad}).status_code, 400)
        self.assertEqual(self.patch({"ntfy_server": "https://ntfy.example.org/"}).status_code, 200)
        self.assertEqual(self.config.ntfy_server, "https://ntfy.example.org")
        for bad in ["short", "has space 12345", "x" * 65, "semi;colon12345", 12345678]:
            with self.subTest(bad=bad):
                self.assertEqual(self.patch({"ntfy_topic": bad}).status_code, 400)
        self.assertEqual(self.patch({"ntfy_topic": "A_new-topic_9876"}).status_code, 200)
        self.assertEqual(self.config.ntfy_topic, "A_new-topic_9876")
        self.assertEqual(self.patch({"ntfy_topic": ""}).json()["ntfy_topic_set"], False)

    def test_changes_are_audited_old_to_new_without_the_topic(self):
        self.patch({"chat_model": "llama3:8b", "ntfy_topic": "A_new-topic_9876"})
        row = [r for r in self.audit_rows() if r["event"] == "settings_change"][-1]
        self.assertEqual(row["changes"]["chat_model"], {"old": "qwen3:4b", "new": "llama3:8b"})
        self.assertEqual(row["changes"]["ntfy_topic"], {"changed": True})
        self.assertNotIn("A_new-topic_9876", json.dumps(row))
        self.assertNotIn(TOPIC, json.dumps(row))

    def test_rejected_changes_are_not_audited_as_changes(self):
        self.patch({"token_hash": "x"})
        self.assertEqual([r for r in self.audit_rows() if r["event"] == "settings_change"], [])


class NoConfigFileTest(Base):
    with_config_file = False

    def test_change_applies_but_says_it_was_not_saved(self):
        response = self.client.patch("/api/settings", json={"chat_model": "llama3:8b"}).json()
        self.assertFalse(response["persisted"])
        self.assertIn("restart", response["note"])
        self.assertEqual(self.config.chat_model, "llama3:8b")

    def test_a_default_file_of_another_hub_is_never_overwritten(self):
        other, _ = make_config()
        other.token_hash = "f" * 64
        stranger = self.root / "other-hub.json"
        cfg.save(other, stranger)
        before = stranger.read_text()
        with mock.patch.dict(os.environ, {"HUB_CONFIG": str(stranger)}):
            response = self.client.patch("/api/settings", json={"chat_model": "llama3:8b"}).json()
        self.assertFalse(response["persisted"])
        self.assertEqual(stranger.read_text(), before)

    def test_the_default_file_of_this_hub_is_used(self):
        cfg.save(self.config, self.root / "mine.json")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": str(self.root / "mine.json")}):
            response = self.client.patch("/api/settings", json={"chat_model": "llama3:8b"}).json()
        self.assertTrue(response["persisted"])
        self.assertEqual(json.loads((self.root / "mine.json").read_text())["chat_model"], "llama3:8b")


class ModelsTest(Base):
    def events(self, response):
        out = []
        for line in response.text.splitlines():
            if line.startswith("data: "):
                out.append(line[6:])
        return out

    def test_pull_needs_confirm_and_a_valid_name(self):
        self.assertEqual(self.client.post("/api/settings/models/pull", json={"name": "qwen3:4b"}).status_code, 409)
        self.assertEqual(self.client.post("/api/settings/models/pull", json={"name": "qwen3:4b", "confirm": "yes"}).status_code, 422)
        for bad in ["", "a b", "x;y", "../../etc", "x" * 81, "a\nb"]:
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post("/api/settings/models/pull", json={"name": bad, "confirm": True}).status_code, 400)
        self.assertEqual([r for r in self.audit_rows() if r["event"] == "model_pull"], [])

    def test_pull_streams_progress(self):
        response = self.client.post("/api/settings/models/pull", json={"name": "llama3.2:3b", "confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = self.events(response)
        self.assertEqual(events[-1], "[DONE]")
        parsed = [json.loads(e) for e in events[:-1]]
        self.assertIn({"status": "pulling abc", "percent": 25.0}, parsed)
        self.assertEqual(parsed[-1]["status"], "success")
        rows = self.audit_rows()
        self.assertEqual([r["event"] for r in rows if r["event"].startswith("model_pull")], ["model_pull", "model_pull_done"])
        self.assertTrue([r for r in rows if r["event"] == "model_pull_done"][0]["ok"])

    def test_pull_when_ollama_is_down(self):
        self.handler = ollama_down
        self.build()
        response = self.client.post("/api/settings/models/pull", json={"name": "llama3.2:3b", "confirm": True})
        events = self.events(response)
        self.assertIn("Ollama is not running", json.loads(events[0])["error"])
        self.assertEqual(events[-1], "[DONE]")

    def test_model_test(self):
        ok = self.client.post("/api/settings/models/test", json={"model": "qwen3:4b"}).json()
        self.assertTrue(ok["ok"])
        self.assertIsInstance(ok["latency_ms"], int)
        self.assertEqual(ok["reply"], "ready")
        missing = self.client.post("/api/settings/models/test", json={"model": "missing:1b"}).json()
        self.assertFalse(missing["ok"])
        self.assertIn("not installed", missing["error"])
        self.assertEqual(self.client.post("/api/settings/models/test", json={"model": "bad name"}).status_code, 400)

    def test_model_test_when_ollama_is_down(self):
        self.handler = ollama_down
        self.build()
        data = self.client.post("/api/settings/models/test", json={"model": "qwen3:4b"}).json()
        self.assertFalse(data["ok"])


class DiagnosticsTest(Base):
    def test_a_list_of_checks(self):
        checks = self.client.get("/api/settings/diagnostics").json()
        self.assertIsInstance(checks, list)
        for check in checks:
            self.assertTrue({"name", "ok", "detail", "fix"} <= set(check), check)
            self.assertIsInstance(check["ok"], bool)
        names = {c["name"]: c for c in checks}
        self.assertTrue(names["Ollama"]["ok"])
        self.assertTrue(names["Chat model"]["ok"])
        self.assertFalse(names["Search model"]["ok"])          # nomic-embed-text is not installed
        self.assertIn("ollama pull nomic-embed-text", names["Search model"]["fix"])
        self.assertTrue(names["AI providers"]["ok"])
        self.assertTrue(names["Folder docs"]["ok"])

    def test_a_missing_folder_and_ollama_down_are_reported(self):
        self.handler = ollama_down
        self.build()
        self.config.roots["gone"] = str(self.root / "gone")
        names = {c["name"]: c for c in self.client.get("/api/settings/diagnostics").json()}
        self.assertFalse(names["Folder gone"]["ok"])
        self.assertIn("hub.json", names["Folder gone"]["fix"])
        self.assertFalse(names["Ollama"]["ok"])
        self.assertEqual(names["Ollama"]["level"], "fail")


class AuditViewerTest(Base):
    def write(self, lines):
        with open(self.audit_path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line if isinstance(line, str) else json.dumps(line))
                fh.write("\n")

    def rows(self, **params):
        response = self.client.get("/api/settings/audit", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_newest_first_and_garbage_is_skipped(self):
        self.write([{"time": "2026-11-04T09:00:00", "event": "login"}, "this is not json {{{", "",
                    "[1, 2, 3]", '"just a string"', '{"no_event": true}', '{"event": 5}',
                    {"time": "2026-11-04T09:30:00", "event": "upload", "name": "a.txt"}, '{"event": "cut off', "\x00\x00\x00"])
        data = self.rows()
        self.assertEqual([r["event"] for r in data["rows"]], ["upload", "login"])
        self.assertEqual(data["count"], 2)

    def test_missing_file_is_empty(self):
        self.assertEqual(self.rows()["rows"], [])
        self.assertEqual(self.client.get("/api/settings/audit/events").json()["events"], [])

    def test_never_more_than_500_rows(self):
        self.write([{"time": f"2026-11-04T10:{i // 60:02d}:{i % 60:02d}", "event": "tick", "n": i} for i in range(700)])
        data = self.rows(limit=10000)
        self.assertEqual(len(data["rows"]), 500)
        self.assertEqual(data["rows"][0]["n"], 699)
        self.assertEqual(self.client.get("/api/settings/audit?limit=0").status_code, 422)
        self.assertEqual(len(self.rows(limit=7)["rows"]), 7)

    def test_filters(self):
        self.write([{"time": "2026-11-04T09:00:00", "event": "login", "client": "phone"},
                    {"time": "2026-11-04T09:10:00", "event": "upload", "name": "Report.PDF"},
                    {"time": "2026-11-04T09:20:00", "event": "login", "client": "tablet"}])
        self.assertEqual([r["client"] for r in self.rows(event="login")["rows"]], ["tablet", "phone"])
        self.assertEqual([r["event"] for r in self.rows(q="report")["rows"]], ["upload"])
        self.assertEqual([r["client"] for r in self.rows(before="2026-11-04T09:20:00")["rows"] if "client" in r], ["phone"])
        self.assertEqual(self.rows(event="nope")["rows"], [])

    def test_paging_with_the_cursor(self):
        self.write([{"time": "2026-11-04T10:00:00", "event": "tick", "n": i} for i in range(25)])
        seen, before = [], ""
        for _ in range(10):
            data = self.rows(limit=10, before=before)
            seen += [r["n"] for r in data["rows"]]
            if data["next_before"] is None:
                break
            before = str(data["next_before"])
        self.assertEqual(seen, list(range(24, -1, -1)))

    def test_event_names(self):
        self.write([{"event": "login"}, {"event": "upload"}, {"event": "login"}, "junk"])
        data = self.client.get("/api/settings/audit/events").json()
        self.assertEqual(data["events"], ["login", "upload"])
        self.assertEqual(data["counts"]["login"], 2)

    def test_long_values_are_clipped(self):
        self.write([{"event": "x", "blob": "y" * 5000}])
        self.assertLess(len(self.rows()["rows"][0]["blob"]), 600)

    def test_a_huge_file_is_read_from_the_end_only(self):
        with open(self.audit_path, "wb") as fh:
            fh.truncate(60 * 1024 * 1024)        # 60 MB of nothing in front
            fh.seek(0, os.SEEK_END)
            for i in range(5):
                fh.write(json.dumps({"time": "2026-11-04T10:00:00", "event": "tail", "n": i}).encode() + b"\n")
        read = {"bytes": 0}
        real_open = open

        class Counting:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.handle.close()

            def read(self, n=-1):
                data = self.handle.read(n)
                read["bytes"] += len(data)
                return data

            def __getattr__(self, name):
                return getattr(self.handle, name)

        with mock.patch("hub.settings.open", lambda *a, **k: Counting(real_open(*a, **k)), create=True):
            data = self.rows(limit=3)
        self.assertEqual([r["n"] for r in data["rows"]], [4, 3, 2])
        self.assertLess(read["bytes"], 256 * 1024)

    def test_a_scan_that_finds_nothing_stops_at_the_read_limit(self):
        with open(self.audit_path, "wb") as fh:
            fh.write(b"x" * (settings.MAX_AUDIT_SCAN_BYTES + 5 * 1024 * 1024))
        data = self.rows(event="login")
        self.assertEqual(data["rows"], [])
        self.assertTrue(data["truncated"])


class BackupTest(Base):
    def setUp(self):
        super().setUp()
        data = self.config.data_path()
        data.mkdir(parents=True)
        (data / "todos.json").write_text('{"items": ["buy milk"]}')
        (data / "sub").mkdir()
        (data / "sub" / "deep.txt").write_text("deep")
        Path(self.config.notes_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.config.notes_dir) / "2026-11-04_100000_hello.md").write_text("---\ntitle: \"Hello\"\n---\n\nhello note\n")
        self.audit_path.write_text('{"time": "t", "event": "login"}\n')

    def make(self):
        return self.client.post("/api/settings/backup", json={"confirm": True})

    def names(self, name):
        with zipfile.ZipFile(self.config.data_path() / "backups" / name) as z:
            return z.namelist()

    def test_needs_confirm(self):
        self.assertEqual(self.client.post("/api/settings/backup", json={}).status_code, 409)
        self.assertEqual(self.client.post("/api/settings/backup", json={"confirm": "yes"}).status_code, 422)
        self.assertFalse((self.config.data_path() / "backups").exists())

    def test_contents(self):
        response = self.make()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        names = self.names(body["name"])
        self.assertIn("data/todos.json", names)
        self.assertIn("data/sub/deep.txt", names)
        self.assertIn("notes/2026-11-04_100000_hello.md", names)
        self.assertIn("audit/audit.log", names)
        self.assertFalse(any(n.startswith("data/backups") for n in names))
        self.assertEqual([r["name"] for r in self.audit_rows() if r["event"] == "backup_create"], [body["name"]])

    def test_secrets_are_excluded(self):
        data = self.config.data_path()
        (data / "hub.json").write_text(self.config_path.read_text())
        (data / "sneaky-copy.txt").write_text("hash is " + self.config.token_hash)
        (data / "sneaky-secret.bin").write_bytes(self.config.session_secret.hex().encode())
        shutil_target = self.root / "outside.txt"
        shutil_target.write_text("outside the data folder " + self.config.token_hash)
        os.symlink(shutil_target, data / "link.txt")
        body = self.make().json()
        names = self.names(body["name"])
        for bad in ("data/hub.json", "data/sneaky-copy.txt", "data/sneaky-secret.bin", "data/link.txt"):
            self.assertNotIn(bad, names)
        self.assertGreaterEqual(body["excluded"], 3)
        raw = io.BytesIO()
        with zipfile.ZipFile(self.config.data_path() / "backups" / body["name"]) as z:
            for name in z.namelist():
                raw.write(z.read(name))
        blob = raw.getvalue()
        self.assertNotIn(self.config.token_hash.encode(), blob)
        self.assertNotIn(self.config.session_secret.hex().encode(), blob)
        self.assertNotIn(b"token_hash", blob)
        self.assertNotIn(b"session_secret", blob)

    def test_the_real_config_file_is_never_included(self):
        # even when the config file sits inside the data folder
        data = self.config.data_path()
        inside = data / "conf" / "settings-copy.json"
        inside.parent.mkdir()
        cfg.save(self.config, inside)
        self.ctx.config_path = inside
        names = self.names(self.make().json()["name"])
        self.assertNotIn("data/conf/settings-copy.json", names)

    def test_keeps_the_last_five(self):
        made = [self.make().json()["name"] for _ in range(7)]
        listed = self.client.get("/api/settings/backups").json()["backups"]
        self.assertEqual(len(listed), 5)
        self.assertEqual(listed[0]["name"], made[-1])        # newest first
        self.assertEqual(len(list((self.config.data_path() / "backups").iterdir())), 5)

    def test_a_backup_does_not_contain_older_backups(self):
        self.make()
        names = self.names(self.make().json()["name"])
        self.assertFalse(any("hub-backup" in n for n in names))

    def test_download(self):
        name = self.make().json()["name"]
        response = self.client.get(f"/api/settings/backups/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("attachment", response.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            self.assertIn("data/todos.json", z.namelist())

    def test_backup_names_are_validated(self):
        self.make()
        for bad in ["hub.json", "hub-backup-1.zip", "hub-backup-20260101-000000.zip.txt", "x\\y.zip",
                    "hub-backup-20260101-000000.zip%00", "%2e%2e%2fhub.json", "..%2f..%2fetc%2fpasswd", "hub-backup-%2e%2e.zip"]:
            with self.subTest(bad=bad):
                response = self.client.get(f"/api/settings/backups/{bad}")
                self.assertIn(response.status_code, (400, 404))
                self.assertNotIn("token_hash", response.text)
        self.assertEqual(self.client.get("/api/settings/backups/hub-backup-20200101-000000.zip").status_code, 404)

    def test_a_symlink_named_like_a_backup_is_not_served(self):
        out = self.config.data_path() / "backups"
        out.mkdir(exist_ok=True)
        secret = self.root / "secret.txt"
        secret.write_text("TOP SECRET")
        os.symlink(secret, out / "hub-backup-20200101-000000.zip")
        self.assertEqual(self.client.get("/api/settings/backups/hub-backup-20200101-000000.zip").status_code, 404)
        self.assertEqual(self.client.get("/api/settings/backups").json()["backups"], [])

    def test_no_half_file_is_left_behind_on_failure(self):
        with mock.patch("hub.settings.zipfile.ZipFile.write", side_effect=PermissionError("boom")):
            self.make()   # unreadable files are skipped, the archive still completes
        leftovers = [p for p in (self.config.data_path() / "backups").iterdir() if p.suffix == ".part"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
