"""Coding-agent session ADAPTERS (hub/ccsessions.py): the list, generic-log, experimental codex-cli, and the rule that only
a claude-code adapter with a safe resume template can be steered. Temp files only; nothing is run."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from hub import ccsessions as cc
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config
from tests.test_ccsessions import SID, asst, user


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccad-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.work = self.tmp / "proj"
        self.work.mkdir()
        self.config, self.root = make_config(ccsessions_dir=str(self.projects))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        cc.JOBS.clear()
        self.store = cc.adapter_list(self.config)

    def touch(self, path, text, age=0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def claude_session(self, sid=SID, age=120, folder=None):
        rows = [dict(r, cwd=str(self.work)) for r in (user("go"), asst("done"))]
        return self.touch((folder or self.projects) / "-tmp-proj" / f"{sid}.jsonl", "".join(json.dumps(r) + "\n" for r in rows), age)


class AdapterList(Base):
    def test_claude_code_is_the_ready_made_entry_and_everything_still_works(self):
        entries = self.store.load()
        self.assertEqual([(e["id"], e["kind"]) for e in entries], [("claude-code", "claude-code")])
        self.assertEqual(entries[0]["settings"]["resume_argv"], cc.DEFAULT_RESUME)
        self.claude_session()
        (row,) = cc.discover(self.config)
        self.assertEqual((row["id"], row["adapter"], row["steerable"], row["read_only"]), (SID, "claude-code", True, False))

    def test_adapter_can_be_switched_off_or_deleted(self):
        self.claude_session()
        self.store.update("claude-code", {"enabled": False})
        self.assertEqual(cc.discover(self.config), [])
        self.store.update("claude-code", {"enabled": True})
        self.store.delete("claude-code")
        self.assertEqual(cc.discover(self.config), [])
        self.assertIsNone(cc.detail(self.config, SID))

    def test_second_claude_code_folder_gets_prefixed_ids(self):
        other = self.tmp / "wsl-projects"
        self.claude_session(folder=other)
        entry = self.store.add({"kind": "claude-code", "label": "Claude in WSL", "settings": {"dir": str(other)}})
        (row,) = cc.discover(self.config)
        self.assertEqual((row["id"], row["native_id"], row["adapter_label"]), (f"{entry['id']}_{SID}", SID, "Claude in WSL"))
        self.assertEqual(cc.detail(self.config, row["id"])["turns"][-1]["text"], "done")

    def test_validation(self):
        bad = [{"kind": "generic-log", "settings": {}}, {"kind": "generic-log", "settings": {"path": "x", "patterns": ["a b"]}},
               {"kind": "generic-log", "settings": {"path": "x", "patterns": ["../*"]}}, {"kind": "nope"}]
        for body in bad:
            with self.assertRaises(Exception, msg=body):
                self.store.add(body)


class ResumeTemplate(Base):
    def test_unsafe_templates_are_refused(self):
        for bad in (["bash", "-c", "{session}"], ["cmd", "/c", "claude {session}"], ["C:\\Windows\\System32\\cmd.exe", "{session}"],
                    ["powershell.exe", "-Command", "{session}"], ["sudo", "claude", "{session}"], ["claude", "-p"],
                    ["claude", "--resume", "{session}", "{message}"], ["claude"], "claude --resume {session}",
                    ["{session}", "x"], ["claude", "--resume", "{session}", "a\x00b"], ["env", "X=1", "claude", "{session}"]):
            with self.assertRaises(Exception, msg=bad):
                cc.check_resume(bad)
        self.assertEqual(cc.check_resume(["mycode", "--continue", "{session}"]), ["mycode", "--continue", "{session}"])
        self.assertEqual(cc.check_resume(None), [])

    def test_only_a_claude_code_adapter_with_a_template_is_steerable(self):
        entry = self.store.add({"kind": "claude-code", "label": "No steering", "settings": {"dir": str(self.tmp / "none"), "resume_argv": []}})
        self.assertFalse(self.store.get(entry["id"])["settings"]["resume_argv"])
        log = self.touch(self.tmp / "agent.log", "line one\nline two\n", age=300)
        self.store.add({"kind": "generic-log", "label": "Agent", "settings": {"path": str(log)}})
        rows = {r["adapter_label"]: r for r in cc.discover(self.config)}
        self.assertTrue(rows["Agent"]["read_only"] and not rows["Agent"]["steerable"])
        with mock.patch("hub.ccsessions.shutil.which", return_value="/usr/bin/x"):
            with self.assertRaises(cc.SendError) as e:
                cc.check_send(self.config, rows["Agent"]["id"], "hello")
        self.assertIn("read-only", str(e.exception))

    def test_custom_template_runs_as_argv_with_message_on_stdin(self):
        other = self.tmp / "other"
        self.claude_session(folder=other)
        entry = self.store.add({"kind": "claude-code", "label": "Custom", "settings": {
            "dir": str(other), "resume_argv": ["mycode", "--resume", "{session}", "--print"]}})
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, **kw)
            return mock.Mock(returncode=0, stdout='{"result": "ok"}', stderr="")
        with mock.patch("hub.ccsessions.shutil.which", lambda name: f"/opt/{name}"), mock.patch("hub.ccsessions.subprocess.run", fake_run):
            job = cc.start_send(self.config, f"{entry['id']}_{SID}", "do it; rm -rf /")
            for _ in range(100):
                if job["status"] != "running":
                    break
                time.sleep(0.02)
        self.assertEqual(seen["argv"], ["/opt/mycode", "--resume", SID, "--print"])
        self.assertEqual((seen["input"], seen["shell"]), ("do it; rm -rf /", False))
        self.assertEqual(job["status"], "done")

    def test_missing_program_is_reported_by_name(self):
        other = self.tmp / "other"
        self.claude_session(folder=other)
        entry = self.store.add({"kind": "claude-code", "settings": {"dir": str(other), "resume_argv": ["mycode", "{session}"]}})
        with mock.patch("hub.ccsessions.shutil.which", return_value=None):
            with self.assertRaises(cc.SendError) as e:
                cc.check_send(self.config, f"{entry['id']}_{SID}", "hi")
        self.assertIn("`mycode`", str(e.exception))


class GenericLog(Base):
    def test_text_file_is_tailed_read_only(self):
        log = self.touch(self.tmp / "agent.log", "starting\n" + "x" * 50 + "\nfinished the build\n", age=300)
        self.store.add({"kind": "generic-log", "label": "My agent", "settings": {"path": str(log)}})
        rows = [r for r in cc.discover(self.config) if r["adapter_label"] == "My agent"]
        (row,) = rows
        self.assertEqual((row["status"], row["last_line"], row["project_name"], row["messages"]), ("idle", "finished the build", "agent.log", 3))
        self.assertTrue(row["id"].startswith(f"{row['adapter']}_"))
        d = cc.detail(self.config, row["id"])
        self.assertEqual([t["role"] for t in d["turns"]], ["log"] * 3)
        self.assertEqual(d["turns"][-1]["text"], "finished the build")

    def test_recent_write_shows_working(self):
        log = self.touch(self.tmp / "live.log", "tick\n", age=2)
        self.store.add({"kind": "generic-log", "settings": {"path": str(log)}})
        self.assertEqual([r["status"] for r in cc.discover(self.config) if r["adapter"] != "claude-code"], ["working"])

    def test_jsonl_lines_use_their_text_field_and_roles(self):
        lines = [{"role": "user", "content": "fix it", "timestamp": "2026-11-13T10:00:00Z"},
                 {"type": "assistant", "message": "I fixed it"}, {"event": "tick", "n": 1}, "not json {"]
        log = self.touch(self.tmp / "s.jsonl", "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines) + "\n", age=300)
        self.store.add({"kind": "generic-log", "settings": {"path": str(log)}})
        row = next(r for r in cc.discover(self.config) if r["adapter"] != "claude-code")
        self.assertEqual((row["last_user"], row["last_assistant"]), ("fix it", "I fixed it"))
        turns = cc.detail(self.config, row["id"])["turns"]
        self.assertEqual(turns[0]["at"], "2026-11-13T10:00:00Z")
        self.assertEqual(turns[-1]["text"], "not json {")

    def test_folder_with_patterns_skips_symlinks_and_other_files(self):
        folder = self.tmp / "logs"
        self.touch(folder / "a.log", "a\n", 100)
        self.touch(folder / "sub" / "b.log", "b\n", 50)
        self.touch(folder / "c.dat", "no\n", 10)
        try:
            os.symlink("/etc/hostname", folder / "link.log")
            os.symlink(self.tmp, folder / "dirlink")
        except OSError:
            self.skipTest("symlinks not available (no privilege on Windows)")
        self.store.add({"kind": "generic-log", "settings": {"path": str(folder), "patterns": ["*.log"]}})
        names = sorted(r["project_name"] for r in cc.discover(self.config) if r["adapter"] != "claude-code")
        self.assertEqual(names, ["a.log", "b.log"])

    def test_only_the_tail_is_read_and_a_missing_path_is_quiet(self):
        log = self.touch(self.tmp / "big.log", ("old line\n" * 100_000) + "last line\n", 300)
        self.store.add({"kind": "generic-log", "settings": {"path": str(log)}})
        self.store.add({"kind": "generic-log", "settings": {"path": str(self.tmp / "missing")}})
        rows = [r for r in cc.discover(self.config) if r["adapter"] != "claude-code"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["last_line"], rows[0]["messages_approximate"]), ("last line", True))

    def test_log_text_is_cleaned(self):
        log = self.touch(self.tmp / "x.log", "ok\x00\x1b[31m red\u0007 text\n", 300)
        self.store.add({"kind": "generic-log", "settings": {"path": str(log)}})
        row = next(r for r in cc.discover(self.config) if r["adapter"] != "claude-code")
        self.assertNotIn("\x00", row["last_line"])
        self.assertNotIn("\x07", row["last_line"])


class CodexCli(Base):
    def rollout(self, lines, age=300):
        return self.touch(self.tmp / "codex" / "2026" / "11" / "13" / "rollout-2026-11-13T10-00-00-abc.jsonl",
                          "".join(json.dumps(x) + "\n" for x in lines), age)

    def message(self, role, text, kind=None):
        return {"timestamp": "2026-11-13T10:00:01Z", "type": "response_item",
                "payload": {"type": "message", "role": role, "content": [{"type": kind or ("input_text" if role == "user" else "output_text"), "text": text}]}}

    def test_reads_messages_status_and_working_folder(self):
        self.rollout([{"type": "session_meta", "payload": {"id": "abc", "cwd": str(self.work)}},
                      self.message("user", "<environment_context>cwd...</environment_context>"),
                      self.message("user", "add a test"), {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
                      self.message("assistant", "Added the test.")])
        entry = self.store.add({"kind": "codex-cli", "label": "Codex", "settings": {"dir": str(self.tmp / "codex")}})
        (row,) = [r for r in cc.discover(self.config) if r["adapter"] == entry["id"]]
        self.assertEqual((row["last_user"], row["last_assistant"], row["status"]), ("add a test", "Added the test.", "waiting"))
        self.assertEqual((row["project"], row["project_name"], row["messages"]), (str(self.work), "proj", 2))
        self.assertEqual((row["experimental"], row["read_only"], row["kind"]), (True, True, "codex-cli"))
        self.assertEqual([t["role"] for t in cc.detail(self.config, row["id"])["turns"]], ["user", "assistant"])

    def test_unreadable_format_falls_back_to_raw_lines_and_is_never_steerable(self):
        self.rollout([{"weird": "shape", "text": "something happened"}])
        entry = self.store.add({"kind": "codex-cli", "settings": {"dir": str(self.tmp / "codex")}})
        (row,) = [r for r in cc.discover(self.config) if r["adapter"] == entry["id"]]
        self.assertEqual(row["last_line"], "something happened")
        with mock.patch("hub.ccsessions.shutil.which", return_value="/usr/bin/codex"):
            with self.assertRaises(cc.SendError) as e:
                cc.check_send(self.config, row["id"], "hi")
        self.assertIn("read-only", str(e.exception))

    def test_flat_older_shape(self):
        self.rollout([{"type": "message", "role": "user", "content": "hello"}, {"type": "message", "role": "assistant", "content": "hi"}])
        entry = self.store.add({"kind": "codex-cli", "settings": {"dir": str(self.tmp / "codex")}})
        (row,) = [r for r in cc.discover(self.config) if r["adapter"] == entry["id"]]
        self.assertEqual((row["last_user"], row["last_assistant"]), ("hello", "hi"))

    def test_read_only_sessions_never_trigger_the_waiting_notification(self):
        self.rollout([self.message("user", "x"), self.message("assistant", "done")], age=900)
        self.store.add({"kind": "codex-cli", "settings": {"dir": str(self.tmp / "codex")}})
        events = []
        ctx = mock.Mock(config=self.config)
        ctx.proactive = mock.Mock(enabled=True, submit=lambda e: events.append(e) or "batch")
        self.assertEqual(cc.Watcher(ctx, clock=time.time).tick(), 0)
        self.assertEqual(events, [])


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config, Deps(llm=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant()),
                              audit_path=self.root / "audit.log")

    def client(self, login=True):
        c = TestClient(self.app, base_url="http://localhost", headers={"X-Hub": "1"})
        if login:
            self.assertEqual(c.post("/api/login", json={"token": TOKEN}).status_code, 200)
        return c

    def test_routes_require_login(self):
        c = self.client(False)
        for method, path in [("get", "/api/ccsessions/adapters"), ("post", "/api/ccsessions/adapters"),
                             ("put", "/api/ccsessions/adapters/order"), ("put", "/api/ccsessions/adapters/x"),
                             ("delete", "/api/ccsessions/adapters/x")]:
            self.assertEqual(getattr(c, method)(path).status_code, 401, path)

    def test_list_add_edit_delete_and_validation(self):
        c = self.client()
        data = c.get("/api/ccsessions/adapters").json()
        self.assertEqual([e["id"] for e in data["entries"]], ["claude-code"])
        self.assertTrue(data["entries"][0]["steerable"])
        self.assertEqual({p["kind"] for p in data["presets"]}, {"codex-cli", "generic-log"})
        self.assertTrue(any("experimental" in p["label"].lower() for p in data["presets"]))
        log = self.touch(self.tmp / "a.log", "hello\n", 300)
        r = c.post("/api/ccsessions/adapters", json={"kind": "generic-log", "label": "A", "settings": {"path": str(log)}})
        self.assertEqual(r.status_code, 200, r.text)
        ident = r.json()["entry"]["id"]
        self.assertEqual([s["adapter_label"] for s in c.get("/api/ccsessions").json()["sessions"]], ["A"])
        sid = c.get("/api/ccsessions").json()["sessions"][0]["id"]
        self.assertEqual(c.get(f"/api/ccsessions/{sid}").json()["turns"][-1]["text"], "hello")
        send = c.post(f"/api/ccsessions/{sid}/send", json={"message": "hi", "confirm": True})
        self.assertEqual(send.status_code, 409)
        self.assertIn("read-only", send.json()["detail"])
        self.assertEqual(c.post("/api/ccsessions/adapters", json={"kind": "claude-code", "settings": {"resume_argv": ["bash", "-c", "{session}"]}}).status_code, 400)
        self.assertEqual(c.post("/api/ccsessions/adapters", json={"kind": "generic-log", "settings": {}}).status_code, 400)
        self.assertEqual(c.put("/api/ccsessions/adapters/order", json={"ids": [ident]}).json()["entries"][0]["id"], ident)
        self.assertEqual(c.put(f"/api/ccsessions/adapters/{ident}", json={"enabled": False}).json()["entry"]["enabled"], False)
        self.assertEqual(c.delete(f"/api/ccsessions/adapters/{ident}").status_code, 200)
        self.assertEqual(c.delete(f"/api/ccsessions/adapters/{ident}").status_code, 404)
        self.assertEqual(c.get("/api/ccsessions/adapters").status_code, 200)    # "adapters" is not taken as a session id

    def test_assistant_tools_mark_read_only_rows(self):
        log = self.touch(self.tmp / "a.log", "hello world\n", 300)
        self.store.add({"kind": "generic-log", "label": "A", "settings": {"path": str(log)}})
        tools = {t.spec["name"]: t for t in cc.build(lambda: self.app, self.config)}
        import asyncio
        ok, text = asyncio.run(tools["cc_sessions"].run({}))
        self.assertTrue(ok)
        self.assertIn("read-only", text)
        self.assertIn("hello world", text)


if __name__ == "__main__":
    unittest.main()
