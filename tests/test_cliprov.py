"""CLI login providers (hub/cliprov.py): stand-in programs run for real (a tiny Python script), no network, no real AI CLI."""
import asyncio
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import cliprov, providers, usage
from hub.common import Ctx
from hub.llm import LLMError
from hub.provlist import MemorySecrets
from tests.helpers import make_config

SCRIPT = textwrap.dedent("""
    import json, os, subprocess, sys, time
    mode = sys.argv[1]
    data = sys.stdin.read()
    if mode == "echo":
        open(sys.argv[2], "w").write(data + "\\n--ARGV--" + json.dumps(sys.argv))
        print("ANSWER:" + data[-12:])
    elif mode == "env":
        print(json.dumps({k: os.environ.get(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "KEEP_ME")}))
    elif mode == "json":
        print("banner line"); print(json.dumps({"response": "hello json", "stats": {}}))
    elif mode == "jsonerr":
        print(json.dumps({"error": {"message": "Please set an Auth method"}}))
    elif mode == "sleep":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open(sys.argv[2], "w").write(str(child.pid))
        time.sleep(60)
    elif mode == "big":
        sys.stdout.write("x" * 2_000_000); sys.stdout.flush()
    elif mode == "login":
        sys.stderr.write("Error: not authenticated, please login. key=AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\\n"); sys.exit(1)
    elif mode == "limit":
        sys.stderr.write("429 quota exceeded\\n"); sys.exit(1)
    elif mode == "ok":
        print("ok")
""")


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                   # a killed child may linger as a zombie until reaped
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.config, _ = make_config(data_dir=tempfile.mkdtemp(prefix="cli-test-"))
        self.dir = Path(tempfile.mkdtemp(prefix="cli-script-"))
        self.script = self.dir / "fake_cli.py"
        self.script.write_text(SCRIPT, encoding="utf-8")
        cliprov.LAST.clear()
        self.audits = []

    def settings(self, mode, *extra, **kw):
        base = {"argv": [sys.executable, str(self.script), mode, *extra], "env_strip": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
                "timeout": 20, "output": "text", "json_path": ""}
        base.update(kw)
        return cliprov.check_settings("cli-login", base)

    def client(self, mode, *extra, **kw):
        return cliprov.CliLoginClient(self.settings(mode, *extra, **kw), "t")

    def run_(self, coro):
        return asyncio.run(coro)


class Running(Base):
    def test_prompt_goes_on_stdin_never_argv(self):
        out_file = self.dir / "got.txt"
        client = self.client("echo", str(out_file))
        answer = self.run_(client.complete("SYSTEM TEXT", "my secret question 12345"))
        got = out_file.read_text()
        stdin, argv = got.split("\n--ARGV--")
        self.assertIn("SYSTEM TEXT", stdin)
        self.assertIn("my secret question 12345", stdin)
        self.assertNotIn("secret question", argv)
        self.assertEqual(answer, "ANSWER:estion 12345")

    def test_env_vars_are_stripped_and_others_kept(self):
        env = {**os.environ, "GEMINI_API_KEY": "k1", "GOOGLE_API_KEY": "k2", "KEEP_ME": "yes"}
        client = cliprov.CliLoginClient(self.settings("env"), "t", base_env=env)
        seen = json.loads(self.run_(client.complete("", "x")))
        self.assertEqual(seen, {"GEMINI_API_KEY": None, "GOOGLE_API_KEY": None, "KEEP_ME": "yes"})

    def test_json_path_extraction(self):
        client = self.client("json", output="json", json_path="response")
        self.assertEqual(self.run_(client.complete("", "x")), "hello json")
        bad = self.client("json", output="json", json_path="result.text")
        with self.assertRaises(LLMError) as cm:
            self.run_(bad.complete("", "x"))
        self.assertIn("result.text", str(cm.exception))
        self.assertEqual(cliprov.dig({"a": [{"b": "deep"}]}, "a.0.b"), "deep")

    def test_json_error_object_reads_as_not_logged_in(self):
        client = self.client("jsonerr", output="json", json_path="response", login_hint="log in first")
        with self.assertRaises(LLMError) as cm:
            self.run_(client.complete("", "x"))
        self.assertEqual(str(cm.exception), "log in first")

    @unittest.skipIf(sys.platform == "win32", "process groups are POSIX here")
    def test_timeout_kills_the_whole_tree(self):
        pid_file = self.dir / "child.pid"
        client = self.client("sleep", str(pid_file), timeout=5)
        client.s["timeout"] = 1                                     # below the validated minimum, for the test only
        started = time.monotonic()
        with self.assertRaises(LLMError) as cm:
            self.run_(client.complete("", "x"))
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn("stopped", str(cm.exception))
        child = int(pid_file.read_text())
        time.sleep(0.3)
        self.assertFalse(alive(child))

    def test_output_is_capped(self):
        client = self.client("big")
        with self.assertRaises(LLMError) as cm:
            self.run_(client.complete("", "x"))
        self.assertIn("too much", str(cm.exception))

    def test_login_failure_is_friendly_and_secret_free(self):
        client = self.client("login", login_hint="not logged in: run `gemini` once")
        with self.assertRaises(LLMError) as cm:
            self.run_(client.complete("", "x"))
        self.assertEqual(str(cm.exception), "not logged in: run `gemini` once")
        self.assertNotIn("AIzaSy", client.last["stderr"])
        self.assertIn("[hidden]", client.last["stderr"])

    def test_limit_failure(self):
        with self.assertRaises(LLMError) as cm:
            self.run_(self.client("limit").complete("", "x"))
        self.assertIn("limit", str(cm.exception))

    def test_not_found(self):
        s = cliprov.check_settings("cli-login", {"argv": ["no-such-ai-program-xyz"], "env_strip": []})
        with self.assertRaises(LLMError) as cm:
            self.run_(cliprov.CliLoginClient(s, "t").complete("", "x"))
        self.assertIn("CLI not found on PATH", str(cm.exception))
        entry = {"id": "x", "settings": s}
        self.assertEqual(cliprov.status_of(entry)["state"], "not-found")
        self.assertEqual(cliprov.status_of(entry)["message"], "CLI not found on PATH")

    def test_fake_spawn_receives_list_argv_no_shell(self):
        calls = {}

        class Proc:
            returncode = 0
            pid = 1

            class _W:
                def __init__(self):
                    self.data = b""

                def write(self, d):
                    self.data += d

                async def drain(self):
                    pass

                def close(self):
                    pass

            class _R:
                def __init__(self, data):
                    self.data = data

                async def read(self, n):
                    d, self.data = self.data, b""
                    return d

            def __init__(self):
                self.stdin, self.stdout, self.stderr = self._W(), self._R(b"fine"), self._R(b"")

            async def wait(self):
                return 0

        async def spawn(*argv, **kw):
            calls["argv"], calls["kw"] = argv, kw
            calls["proc"] = Proc()
            return calls["proc"]

        s = cliprov.check_settings("cli-login", {"argv": [sys.executable, "-q"], "env_strip": []})
        client = cliprov.CliLoginClient(s, "t", spawn=spawn)
        self.assertEqual(self.run_(client.complete("sys", "hello")), "fine")
        self.assertEqual(calls["argv"][1:], ("-q",))
        self.assertNotIn("shell", calls["kw"])
        self.assertIn(b"hello", calls["proc"].stdin.data)
        self.assertNotIn("hello", " ".join(calls["argv"]))


class Settings(Base):
    def bad(self, **kw):
        with self.assertRaises(cliprov.ListError):
            cliprov.check_settings("cli-login", {"argv": ["gemini", "-p"], **kw})

    def test_refusals(self):
        self.bad(argv=["gemini", "-p", "{prompt}"])
        self.bad(argv=["bash", "-c", "gemini"])
        self.bad(argv=["gemini", "a & b"])
        self.bad(argv=["gemini", "--api-key=abc"])
        self.bad(argv=[])
        self.bad(env_strip=["bad name"])
        self.bad(timeout=1)
        self.bad(output="json", json_path="")
        self.bad(output="xml")

    def test_seed_entries(self):
        rows = {e["id"]: e for e in cliprov.seed()}
        self.assertTrue(rows["gemini-cli"]["enabled"])
        self.assertFalse(rows["agy"]["enabled"])
        self.assertIn("agy --help", rows["agy"]["settings"]["note"])
        self.assertIn("GEMINI_API_KEY", rows["gemini-cli"]["settings"]["env_strip"])
        self.assertIn("Login with Google", rows["gemini-cli"]["settings"]["login_hint"])
        self.assertEqual(len(cliprov.list_for(self.config).load()), 2)


class Wiring(Base):
    def setUp(self):
        super().setUp()
        self.secrets = MemorySecrets()
        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda e, **k: self.audits.append(e))
        app = FastAPI()
        app.include_router(cliprov.make_router(ctx, secrets=self.secrets))
        self.client_ = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)

    def add(self, mode, *extra, **kw):
        body = {"kind": "cli-login", "label": "Fake", "settings": self.settings(mode, *extra, **kw)}
        r = self.client_.post("/api/cli-providers", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["entry"]["id"]

    def test_routes_need_login(self):
        for method, path in (("get", ""), ("post", ""), ("put", "/order"), ("post", "/gemini-cli/test"), ("delete", "/gemini-cli")):
            r = getattr(self.anon, method)("/api/cli-providers" + path)
            self.assertEqual(r.status_code, 401, path)

    def test_listing_shows_status_and_no_secrets(self):
        data = self.client_.get("/api/cli-providers").json()
        ids = [e["id"] for e in data["entries"]]
        self.assertEqual(ids[:2], ["gemini-cli", "agy"])
        for e in data["entries"]:
            self.assertIn("message", e["status"])
        if not cliprov.find_program("gemini"):
            self.assertEqual(data["entries"][0]["status"]["message"], "CLI not found on PATH")

    def test_test_route_ok_and_failures(self):
        ident = self.add("ok")
        r = self.client_.post(f"/api/cli-providers/{ident}/test").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["reply"], "ok")
        bad = self.add("login", login_hint="not logged in: run `gemini` once")
        r = self.client_.post(f"/api/cli-providers/{bad}/test").json()
        self.assertFalse(r["ok"])
        self.assertIn("not logged in", r["error"])
        self.assertEqual(r["state"], "not-logged-in")
        self.assertNotIn("AIzaSy", json.dumps(r))
        listed = {e["id"]: e for e in self.client_.get("/api/cli-providers").json()["entries"]}
        self.assertEqual(listed[bad]["status"]["state"], "not-logged-in")
        missing = self.client_.post("/api/cli-providers/gemini-cli/test").json()
        if not cliprov.find_program("gemini"):
            self.assertEqual(missing["error"].split(".")[0].split(":")[0], "CLI not found on PATH")
        self.assertEqual(self.client_.post("/api/cli-providers/nope/test").status_code, 404)

    def test_bad_body_is_a_400(self):
        r = self.client_.post("/api/cli-providers", json={"kind": "cli-login", "settings": {"argv": ["gemini", "{prompt}"]}})
        self.assertEqual(r.status_code, 400)

    def test_guarded_client_redacts_and_counts_usage(self):
        got = self.dir / "got.txt"
        ident = self.add("echo", str(got))
        client = cliprov.build_guarded(self.config, lambda *a, **k: None, ident, self.secrets)
        self.assertIsNotNone(client)
        before = usage.get(self.config).today().get("cli", 0)
        out = self.run_(client.complete("sys", "mail me at jane.doe@example.com please now"))
        self.assertNotIn("jane.doe@example.com", got.read_text())
        self.assertEqual(usage.get(self.config).today().get("cli", 0), before + 1)
        self.assertTrue(out.startswith("ANSWER:"))

    def test_switched_off_entry_has_no_client_and_default_provider_resolves(self):
        self.assertIsNone(cliprov.build_guarded(self.config, None, "agy"))          # agy is off by default
        self.assertIsNone(cliprov.client_for(self.config, "nope"))
        self.assertEqual(self.client_.put("/api/cli-providers/agy", json={"enabled": True}).status_code, 200)
        self.config.default_provider = "cli:agy"
        self.assertEqual(providers.resolve_default(self.config), "cli:agy")
        self.config.default_provider = "cli:missing"
        self.assertNotEqual(providers.resolve_default(self.config), "cli:missing")
        self.assertIn("cli", usage.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
