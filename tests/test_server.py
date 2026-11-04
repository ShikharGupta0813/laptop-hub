import json
import signal
import sys
import time
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub import auth
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from hub.terminal import open_terminal
from tests.helpers import TOKEN, make_config


class Base(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.audit = self.root / "audit.log"
        self.terminals = []

        def terminal(argv, rows, cols):
            term = open_terminal(argv, rows, cols)
            self.terminals.append(term)
            return term

        deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(),
                    assistant=FakeAssistant(), terminal=terminal)
        self.client = TestClient(create_app(self.config, deps, audit_path=self.audit),
                                 base_url="http://testserver")
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post("/api/login", json={"token": TOKEN})
        self.assertEqual(response.status_code, 200)
        return response


class LoginTest(Base):
    def test_everything_needs_a_login(self):
        for path in ["/api/status", "/api/roots", "/api/files?root=docs", "/api/notes", "/api/scripts"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.assertFalse(self.client.get("/api/session").json()["authenticated"])

    def test_the_right_token_gives_a_locked_down_cookie(self):
        response = self.login()
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        self.assertEqual(self.client.get("/api/roots").status_code, 200)

    def test_a_wrong_token_is_refused_and_five_misses_lock_the_door(self):
        for _ in range(auth.MAX_FAILS):
            self.assertEqual(self.client.post("/api/login", json={"token": "nope"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 429)

    def test_a_forged_cookie_does_not_work(self):
        self.client.cookies.set(auth.COOKIE, "9999999999.deadbeef")
        self.assertEqual(self.client.get("/api/roots").status_code, 401)

    def test_logout_ends_the_session(self):
        self.login()
        self.client.post("/api/logout")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/roots").status_code, 401)

    def test_cross_site_login_is_refused(self):
        response = self.client.post("/api/login", json={"token": TOKEN}, headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_security_headers_and_no_caching_of_api(self):
        response = self.client.get("/api/session")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_logins_are_written_to_the_audit_log(self):
        self.client.post("/api/login", json={"token": "bad"})
        self.login()
        events = [json.loads(l)["event"] for l in self.audit.read_text().splitlines()]
        self.assertEqual(events, ["login_failed", "login"])
        self.assertNotIn(TOKEN, self.audit.read_text())

    def test_the_app_shell_is_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Laptop Hub", page.text)
        self.assertEqual(self.client.get("/sw.js").headers["cache-control"], "no-cache")
        self.assertEqual(self.client.get("/manifest.webmanifest").status_code, 200)


class FilesTest(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_browse_and_read(self):
        listing = self.client.get("/api/files", params={"root": "docs"}).json()
        self.assertEqual([e["name"] for e in listing["entries"]], ["a.md"])
        response = self.client.get("/api/file", params={"root": "docs", "path": "a.md"})
        self.assertEqual(response.text, "# Hello\n")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_escapes_are_forbidden(self):
        (self.root / "secret.txt").write_text("secret")
        for path in ["../secret.txt", "../../etc/passwd"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get("/api/file", params={"root": "docs", "path": path}).status_code, 403)
                self.assertEqual(self.client.get("/api/files", params={"root": "docs", "path": path}).status_code, 403)
        self.assertEqual(self.client.get("/api/files", params={"root": "ghost"}).status_code, 404)

    def test_a_html_file_cannot_run_script_in_the_app(self):
        (self.root / "docs" / "x.html").write_text("<script>alert(1)</script>")
        response = self.client.get("/api/file", params={"root": "docs", "path": "x.html"})
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.headers["content-security-policy"], "sandbox")

    def test_upload_lands_in_the_inbox_and_never_overwrites(self):
        first = self.client.post("/api/upload", files={"file": ("../../evil.txt", b"one")}).json()
        second = self.client.post("/api/upload", files={"file": ("../../evil.txt", b"two")}).json()
        self.assertEqual(first["saved_as"], "evil.txt")
        self.assertEqual(second["saved_as"], "evil-2.txt")
        inbox = self.root / "inbox"
        self.assertEqual((inbox / "evil.txt").read_bytes(), b"one")
        self.assertFalse((self.root.parent / "evil.txt").exists())

    def test_search(self):
        results = self.client.get("/api/search", params={"root": "docs", "q": "a.m"}).json()["results"]
        self.assertEqual([r["path"] for r in results], ["a.md"])


class NotesTest(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_a_typed_note_gets_a_title_and_tags_after_saving(self):
        note = self.client.post("/api/notes", json={"text": "remember the sharding quiz"}).json()
        self.assertEqual(note["title"], "Remember the sharding quiz")  # instant fallback title
        saved = self.client.get(f"/api/notes/{note['id']}").json()
        self.assertEqual((saved["title"], saved["tags"], saved["tagged"]), ("Pretend title", ["demo", "sample"], True))

    def test_a_voice_note_drops_the_spoken_command(self):
        result = self.client.post("/api/notes/voice", files={"file": ("rec.webm", b"audio")}).json()
        self.assertTrue(result["transcript"].startswith("Note it."))
        self.assertEqual(result["note"]["body"], "Remember to review the sharding chapter tomorrow morning.")
        self.assertEqual(result["note"]["source"], "voice")

    def test_a_silent_recording_is_a_clear_error(self):
        self.client.app.state  # noqa: B018 (the app is built; swap the transcriber below)
        class Silent:
            def transcribe(self, path):
                return "  "
        deps = Deps(llm=FakeLLM(), transcriber=Silent(), assistant=FakeAssistant())
        client = TestClient(create_app(self.config, deps, audit_path=self.audit))
        client.post("/api/login", json={"token": TOKEN})
        response = client.post("/api/notes/voice", files={"file": ("rec.webm", b"audio")})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(client.get("/api/notes").json()["notes"], [])

    def test_edit_filter_and_search(self):
        note = self.client.post("/api/notes", json={"text": "buy oat milk"}).json()
        self.client.patch(f"/api/notes/{note['id']}", json={"title": "Groceries", "tags": "home, shopping"})
        listing = self.client.get("/api/notes", params={"tag": "shopping"}).json()
        self.assertEqual([n["title"] for n in listing["notes"]], ["Groceries"])
        self.assertEqual(self.client.get("/api/notes", params={"q": "oat"}).json()["notes"][0]["id"], note["id"])
        self.assertEqual(self.client.get("/api/notes", params={"q": "zzz"}).json()["notes"], [])

    def test_bad_note_ids(self):
        self.assertEqual(self.client.get("/api/notes/does-not-exist").status_code, 404)
        self.assertEqual(self.client.get("/api/notes/..%2Fhub").status_code, 404)
        self.assertEqual(self.client.post("/api/notes", json={"text": "  "}).status_code, 400)


class AskAndAssistantTest(Base):
    def setUp(self):
        super().setUp()
        self.login()

    @staticmethod
    def events(response):
        return [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"]

    def test_ask_streams_an_answer_then_done(self):
        response = self.client.post("/api/ask", json={"mode": "explain", "text": "a hash map", "provider": "local"})
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))
        self.assertIn("pretend answer", "".join(e["t"] for e in self.events(response)))
        self.assertEqual(self.client.post("/api/ask", json={"mode": "nope"}).status_code, 400)

    def test_the_assistant_stream_always_ends_with_done(self):
        response = self.client.post("/api/assistant/message",
                                    json={"session_id": "s1", "text": "hello", "provider": "claude"})
        kinds = [e["type"] for e in self.events(response)]
        self.assertEqual(kinds[0], "text")
        self.assertIn("tool_result", kinds)
        self.assertEqual(kinds[-1], "done")

    def test_approving_nothing_is_a_404(self):
        response = self.client.post("/api/assistant/approve", json={"session_id": "s1", "call_id": "c9", "approve": True})
        self.assertEqual(response.status_code, 404)

    def test_a_crashing_assistant_still_ends_the_stream_cleanly(self):
        class Broken:
            async def run(self, *args):
                raise RuntimeError("boom")
                yield  # pragma: no cover

            def resolve(self, *args):
                return False

            def reset(self, *args):
                pass
        client = TestClient(create_app(self.config, Deps(assistant=Broken()), audit_path=self.audit))
        client.post("/api/login", json={"token": TOKEN})
        response = client.post("/api/assistant/message", json={"session_id": "s", "text": "x"})
        kinds = [e["type"] for e in self.events(response)]
        self.assertEqual(kinds, ["error", "done"])


class ButtonsAndStatusTest(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_buttons_list_and_run(self):
        self.assertEqual(self.client.get("/api/scripts").json(), [{"name": "hello", "label": "Say hello"}])
        result = self.client.post("/api/run/hello").json()
        self.assertEqual((result["exit"], result["output"].strip()), (0, "hello from a button"))
        self.assertEqual(self.client.post("/api/run/nope").status_code, 404)
        self.assertIn('"script"', self.audit.read_text())

    def test_status_has_the_basics(self):
        info = self.client.get("/api/status").json()
        for key in ["hostname", "disk", "ollama", "shells", "voice_ready"]:
            self.assertIn(key, info)
        self.assertTrue(info["ollama"]["up"])


@unittest.skipIf(sys.platform == "win32", "uses a POSIX pseudo-terminal; the Windows path needs a Windows test")
class TerminalTest(Base):
    def setUp(self):
        super().setUp()
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("terminal test hung")))
        signal.alarm(30)
        self.addCleanup(signal.alarm, 0)

    def output_of(self, ws, *messages):
        for message in messages:
            ws.send_text(json.dumps(message))
        out = b""
        try:
            while True:
                out += ws.receive_bytes()
        except WebSocketDisconnect:
            return out.decode("utf-8", "replace")

    def test_a_shell_runs_commands_and_follows_resizes(self):
        self.login()
        with self.client.websocket_connect("/ws/term?shell=sh&rows=24&cols=80") as ws:
            text = self.output_of(
                ws, {"type": "resize", "rows": 30, "cols": 100},
                {"type": "input", "data": "echo hub-$((20+22)); stty size; exit\n"})
        self.assertIn("hub-42", text)
        self.assertIn("30 100", text)
        self.assertIn('"terminal_open"', self.audit.read_text())

    def test_closing_the_socket_kills_the_shell(self):
        self.login()
        with self.client.websocket_connect("/ws/term?shell=sh") as ws:
            ws.send_text(json.dumps({"type": "input", "data": "sleep 300\n"}))
            time.sleep(0.3)
        deadline = time.time() + 5
        while self.terminals[-1].proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        self.assertIsNotNone(self.terminals[-1].proc.poll())

    def test_not_logged_in_wrong_shell_and_wrong_origin_are_refused(self):
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/ws/term?shell=sh"):
                pass
        self.assertEqual(caught.exception.code, 4401)
        self.login()
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/ws/term?shell=nope"):
                pass
        self.assertEqual(caught.exception.code, 4400)
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/ws/term?shell=sh", headers={"origin": "https://evil.example"}):
                pass
        self.assertEqual(caught.exception.code, 4403)

    def test_a_missing_shell_program_is_reported(self):
        self.config.shells = {"ghost": ["/no/such/shell"]}
        self.login()
        with self.client.websocket_connect("/ws/term?shell=ghost") as ws:
            self.assertIn("Can't start", json.loads(ws.receive_text())["error"])


if __name__ == "__main__":
    unittest.main()
