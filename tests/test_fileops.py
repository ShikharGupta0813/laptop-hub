"""Tests for hub.fileops: the write side of the Files tab."""
import hashlib
import io
import json
import os
import threading
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from hub import fileops
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config

READ_ONLY = "That folder is read-only. Add it to writable_roots in hub.json to change files there."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Base(unittest.TestCase):
    config_overrides: dict = {}

    def setUp(self):
        probe_cfg, probe_root = make_config()
        self.tmp = probe_root
        data = self.tmp / "hubdata"
        self.config, self.tmp = make_config(data_dir=str(data), **self.config_overrides)
        self.audit = self.tmp / "audit.log"
        self.notes, self.inbox, self.docs = self.tmp / "notes", self.tmp / "inbox", self.tmp / "docs"
        for folder in (self.notes, self.inbox):
            folder.mkdir(exist_ok=True)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("top secret")
        self.client = self.new_client()

    def new_client(self):
        deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        client = TestClient(create_app(self.config, deps, audit_path=self.audit),
                            base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(client.close)
        self.assertEqual(client.post("/api/login", json={"token": TOKEN}).status_code, 200)
        return client

    def post(self, path, **body):
        return self.client.post(f"/api/fileops{path}", json=body)

    def get(self, path, **params):
        return self.client.get(f"/api/fileops{path}", params=params)

    def audit_events(self):
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def symlink(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")


class RootsAndLoginTest(Base):
    def test_everything_needs_a_login(self):
        fresh = TestClient(create_app(self.config, Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(),
                                                        assistant=FakeAssistant()), audit_path=self.audit),
                           base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(fresh.close)
        for path in ["/roots", "/read?root=notes&path=a", "/trash", "/recent", "/search?q=a", "/stats",
                     "/zip?root=notes"]:
            with self.subTest(path=path):
                self.assertEqual(fresh.get(f"/api/fileops{path}").status_code, 401)
        self.assertEqual(fresh.post("/api/fileops/mkdir", json={"root": "notes", "path": "x"}).status_code, 401)

    def test_roots_say_which_are_writable(self):
        data = self.get("/roots").json()
        self.assertEqual({r["name"]: r["writable"] for r in data},
                         {"docs": False, "notes": True, "inbox": True})

    def test_a_post_without_the_app_header_is_refused(self):
        raw = TestClient(self.client.app, base_url="http://testserver")
        raw.cookies.update(self.client.cookies)
        self.assertEqual(raw.post("/api/fileops/mkdir", json={"root": "notes", "path": "x"}).status_code, 403)


class ReadOnlyTest(Base):
    def test_every_change_in_a_read_only_root_is_refused_with_403(self):
        (self.docs / "keep.txt").write_text("keep")
        (self.docs / "folder").mkdir()
        calls = [
            ("/mkdir", {"root": "docs", "path": "new"}),
            ("/write", {"root": "docs", "path": "new.txt", "content": "x"}),
            ("/rename", {"root": "docs", "path": "keep.txt", "new_name": "other.txt"}),
            ("/move", {"root": "docs", "path": "keep.txt", "dest_root": "notes", "dest_dir": ""}),
            ("/move", {"root": "notes", "path": "x", "dest_root": "docs", "dest_dir": ""}),
            ("/copy", {"root": "notes", "path": "x", "dest_root": "docs", "dest_dir": ""}),
            ("/delete", {"root": "docs", "path": "keep.txt", "confirm": True}),
        ]
        for path, body in calls:
            with self.subTest(path=path, body=body):
                response = self.post(path, **body)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["detail"], READ_ONLY)
        upload = self.client.post("/api/fileops/upload", data={"root": "docs", "dir": ""},
                                  files={"file": ("a.txt", b"hi")})
        self.assertEqual(upload.status_code, 403)
        self.assertEqual((self.docs / "keep.txt").read_text(), "keep")
        self.assertEqual(sorted(p.name for p in self.docs.iterdir()), ["a.md", "folder", "keep.txt"])

    def test_a_read_only_root_can_still_be_read_and_copied_from(self):
        (self.docs / "keep.txt").write_text("keep")
        read = self.get("/read", root="docs", path="keep.txt")
        self.assertEqual(read.status_code, 200)
        self.assertFalse(read.json()["writable"])
        copied = self.post("/copy", root="docs", path="keep.txt", dest_root="inbox", dest_dir="")
        self.assertEqual(copied.status_code, 200, copied.text)
        self.assertEqual((self.inbox / "keep.txt").read_text(), "keep")

    def test_writable_roots_in_the_config_is_honoured(self):
        self.config.writable_roots = ["docs"]
        self.assertEqual(self.post("/mkdir", root="docs", path="made").status_code, 200)
        self.assertEqual(self.post("/mkdir", root="notes", path="made").status_code, 403)

    def test_unknown_root_is_404(self):
        self.assertEqual(self.post("/mkdir", root="nope", path="x").status_code, 404)


class TraversalTest(Base):
    def test_paths_that_escape_are_refused_everywhere(self):
        (self.notes / "n.txt").write_text("n")
        for rel in ["../outside/secret.txt", "sub/../../outside/secret.txt", "../../../../etc/passwd"]:
            with self.subTest(rel=rel):
                for path, body in [
                    ("/write", {"root": "notes", "path": rel, "content": "pwned"}),
                    ("/mkdir", {"root": "notes", "path": rel + "dir"}),
                    ("/rename", {"root": "notes", "path": rel, "new_name": "x"}),
                    ("/delete", {"root": "notes", "path": rel, "confirm": True}),
                    ("/move", {"root": "notes", "path": rel, "dest_root": "inbox", "dest_dir": ""}),
                    ("/move", {"root": "notes", "path": "n.txt", "dest_root": "inbox", "dest_dir": "../outside"}),
                    ("/copy", {"root": "notes", "path": rel, "dest_root": "inbox", "dest_dir": ""}),
                    ("/copy", {"root": "notes", "path": "n.txt", "dest_root": "inbox", "dest_dir": "../outside"}),
                ]:
                    response = self.post(path, **body)
                    self.assertEqual(response.status_code, 403, f"{path}: {response.text}")
                self.assertEqual(self.get("/read", root="notes", path=rel).status_code, 403)
                self.assertEqual(self.get("/zip", root="notes", path=rel).status_code, 403)
        self.assertEqual((self.outside / "secret.txt").read_text(), "top secret")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])

    def test_a_nul_in_a_path_is_a_plain_400(self):
        self.assertEqual(self.post("/write", root="notes", path="a\x00b.txt", content="x").status_code, 400)
        self.assertEqual(self.get("/read", root="notes", path="a\x00b").status_code, 400)

    def test_the_folder_itself_cannot_be_deleted_renamed_or_moved(self):
        for path, body in [("/delete", {"root": "notes", "path": "", "confirm": True}),
                           ("/rename", {"root": "notes", "path": "", "new_name": "x"}),
                           ("/move", {"root": "notes", "path": "", "dest_root": "inbox", "dest_dir": ""})]:
            self.assertEqual(self.post(path, **body).status_code, 400, path)
        self.assertTrue(self.notes.is_dir())


class SymlinkTest(Base):
    def test_writing_through_a_link_that_escapes_is_refused(self):
        self.symlink(self.outside / "secret.txt", self.notes / "escape.txt")
        self.symlink(self.outside, self.notes / "outdir")
        for path, body in [
            ("/write", {"root": "notes", "path": "escape.txt", "content": "pwned", "etag": sha("top secret")}),
            ("/write", {"root": "notes", "path": "outdir/new.txt", "content": "pwned"}),
            ("/write", {"root": "notes", "path": "outdir/secret.txt", "content": "pwned", "etag": sha("top secret")}),
            ("/mkdir", {"root": "notes", "path": "outdir/sub"}),
            ("/delete", {"root": "notes", "path": "escape.txt", "confirm": True}),
            ("/rename", {"root": "notes", "path": "escape.txt", "new_name": "x"}),
            ("/move", {"root": "notes", "path": "escape.txt", "dest_root": "inbox", "dest_dir": ""}),
            ("/copy", {"root": "notes", "path": "escape.txt", "dest_root": "inbox", "dest_dir": ""}),
            ("/move", {"root": "inbox", "path": "nothing", "dest_root": "notes", "dest_dir": "outdir"}),
        ]:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.post(path, **body).status_code, 403)
        self.assertEqual((self.outside / "secret.txt").read_text(), "top secret")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])
        self.assertEqual(self.get("/read", root="notes", path="escape.txt").status_code, 403)

    def test_upload_into_a_folder_link_that_escapes_is_refused(self):
        self.symlink(self.outside, self.notes / "outdir")
        response = self.client.post("/api/fileops/upload", data={"root": "notes", "dir": "outdir"},
                                    files={"file": ("a.txt", b"hi")})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["secret.txt"])

    def test_a_link_that_stays_inside_the_folder_can_be_renamed_and_the_target_stays(self):
        (self.notes / "real.txt").write_text("real")
        self.symlink(self.notes / "real.txt", self.notes / "link.txt")
        response = self.post("/rename", root="notes", path="link.txt", new_name="renamed.txt")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.notes / "real.txt").is_file())
        self.assertTrue((self.notes / "renamed.txt").is_symlink())

    def test_zip_of_a_folder_with_an_escaping_link_is_refused(self):
        (self.notes / "dir").mkdir()
        (self.notes / "dir" / "ok.txt").write_text("ok")
        self.symlink(self.outside / "secret.txt", self.notes / "dir" / "leak.txt")
        self.assertEqual(self.get("/zip", root="notes", path="dir").status_code, 403)
        self.assertEqual(self.post("/copy", root="notes", path="dir", dest_root="inbox", dest_dir="").status_code, 403)
        self.assertEqual(list(self.inbox.iterdir()), [])


class ReadWriteTest(Base):
    def test_read_returns_content_etag_mtime_and_size(self):
        (self.notes / "a.txt").write_text("héllo\n", encoding="utf-8")
        data = self.get("/read", root="notes", path="a.txt").json()
        raw = "héllo\n".encode()
        self.assertEqual(data["content"], "héllo\n")
        self.assertEqual(data["etag"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(data["size"], len(raw))
        self.assertAlmostEqual(data["mtime"], (self.notes / "a.txt").stat().st_mtime, places=3)

    def test_binary_and_huge_files_are_not_opened(self):
        (self.notes / "b.bin").write_bytes(b"\xff\xfe\x00\x01")
        (self.notes / "big.txt").write_bytes(b"a" * (fileops.MAX_TEXT + 1))
        self.assertEqual(self.get("/read", root="notes", path="b.bin").status_code, 415)
        self.assertEqual(self.get("/read", root="notes", path="big.txt").status_code, 413)
        self.assertEqual(self.get("/read", root="notes", path="nothing.txt").status_code, 404)

    def test_create_then_edit_with_the_etag(self):
        created = self.post("/write", root="notes", path="n.txt", content="one")
        self.assertEqual(created.status_code, 200, created.text)
        etag = created.json()["etag"]
        self.assertEqual(etag, sha("one"))
        edited = self.post("/write", root="notes", path="n.txt", content="two", etag=etag)
        self.assertEqual(edited.status_code, 200)
        self.assertEqual((self.notes / "n.txt").read_text(), "two")
        self.assertEqual(edited.json()["etag"], sha("two"))

    def test_a_stale_etag_is_a_409_and_nothing_is_overwritten(self):
        (self.notes / "n.txt").write_text("one")
        etag = self.get("/read", root="notes", path="n.txt").json()["etag"]
        (self.notes / "n.txt").write_text("changed on the laptop")
        response = self.post("/write", root="notes", path="n.txt", content="mine", etag=etag)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "This file changed on the laptop since you opened it")
        self.assertEqual((self.notes / "n.txt").read_text(), "changed on the laptop")

    def test_writing_over_an_existing_file_without_an_etag_is_refused(self):
        (self.notes / "n.txt").write_text("one")
        self.assertEqual(self.post("/write", root="notes", path="n.txt", content="two").status_code, 409)
        self.assertEqual((self.notes / "n.txt").read_text(), "one")

    def test_an_etag_for_a_file_that_was_deleted_is_a_409(self):
        response = self.post("/write", root="notes", path="gone.txt", content="x", etag=sha("old"))
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.notes / "gone.txt").exists())

    def test_create_only_never_overwrites(self):
        self.assertEqual(self.post("/write", root="notes", path="n.txt", content="one", create_only=True).status_code, 200)
        again = self.post("/write", root="notes", path="n.txt", content="two", create_only=True)
        self.assertEqual(again.status_code, 409)
        self.assertEqual((self.notes / "n.txt").read_text(), "one")

    def test_size_limit_and_bad_targets(self):
        too_big = "a" * (fileops.MAX_TEXT + 1)
        self.assertEqual(self.post("/write", root="notes", path="big.txt", content=too_big).status_code, 413)
        exactly = "a" * fileops.MAX_TEXT
        self.assertEqual(self.post("/write", root="notes", path="ok.txt", content=exactly).status_code, 200)
        (self.notes / "dir").mkdir()
        self.assertEqual(self.post("/write", root="notes", path="dir", content="x", etag="e").status_code, 400)
        self.assertEqual(self.post("/write", root="notes", path="missing/x.txt", content="x").status_code, 404)
        self.assertEqual(self.post("/write", root="notes", path="", content="x").status_code, 400)

    def test_new_file_names_are_checked(self):
        for name in ["CON", "nul.txt", "bad.", "bad ", "a:b.txt", "a\x00b.txt"]:
            with self.subTest(name=name):
                self.assertEqual(self.post("/write", root="notes", path=name, content="x").status_code, 400)
        self.assertEqual(sorted(p.name for p in self.notes.iterdir()), [])

    def test_no_temp_files_are_left_behind(self):
        self.post("/write", root="notes", path="n.txt", content="one")
        self.assertEqual([p.name for p in self.notes.iterdir()], ["n.txt"])

    def test_text_is_saved_exactly_with_no_newline_translation(self):
        self.post("/write", root="notes", path="n.txt", content="a\r\nb\n")
        self.assertEqual((self.notes / "n.txt").read_bytes(), b"a\r\nb\n")

    def test_two_writes_with_the_same_etag_only_one_wins(self):
        (self.notes / "race.txt").write_text("start")
        clients = [self.new_client(), self.new_client()]
        for round_ in range(15):
            etag = clients[0].get("/api/fileops/read", params={"root": "notes", "path": "race.txt"}).json()["etag"]
            barrier = threading.Barrier(2)
            results = {}

            def go(i):
                barrier.wait()
                results[i] = clients[i].post("/api/fileops/write", json={
                    "root": "notes", "path": "race.txt", "content": f"writer {i} round {round_}", "etag": etag})

            threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            codes = sorted(r.status_code for r in results.values())
            self.assertEqual(codes, [200, 409], f"round {round_}")
            winner = next(i for i, r in results.items() if r.status_code == 200)
            self.assertEqual((self.notes / "race.txt").read_text(), f"writer {winner} round {round_}")
        self.assertEqual([p.name for p in self.notes.iterdir()], ["race.txt"])

    def test_two_create_only_writes_only_one_creates(self):
        clients = [self.new_client(), self.new_client()]
        barrier = threading.Barrier(2)
        results = {}

        def go(i):
            barrier.wait()
            results[i] = clients[i].post("/api/fileops/write", json={
                "root": "notes", "path": "new.txt", "content": f"writer {i}", "create_only": True})

        threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r.status_code for r in results.values()), [200, 409])


class MkdirRenameMoveCopyTest(Base):
    def test_mkdir_makes_nested_folders_and_is_repeatable(self):
        first = self.post("/mkdir", root="notes", path="a/b/c")
        self.assertEqual((first.status_code, first.json()["created"]), (200, True))
        self.assertTrue((self.notes / "a" / "b" / "c").is_dir())
        self.assertFalse(self.post("/mkdir", root="notes", path="a/b/c").json()["created"])
        (self.notes / "file").write_text("x")
        self.assertEqual(self.post("/mkdir", root="notes", path="file").status_code, 409)
        self.assertEqual(self.post("/mkdir", root="notes", path="a/NUL").status_code, 400)
        self.assertEqual(self.post("/mkdir", root="notes", path="x/bad./y").status_code, 400)
        self.assertFalse((self.notes / "x").exists())

    def test_rename_rules(self):
        (self.notes / "a.txt").write_text("a")
        (self.notes / "b.txt").write_text("b")
        for bad in ["x/y", "..", ".", "", "a\\b", "CON", "x.", "x\x00y", "../b.txt"]:
            with self.subTest(new_name=bad):
                self.assertEqual(self.post("/rename", root="notes", path="a.txt", new_name=bad).status_code, 400)
        self.assertEqual(self.post("/rename", root="notes", path="a.txt", new_name="b.txt").status_code, 409)
        self.assertEqual(self.post("/rename", root="notes", path="zzz", new_name="q").status_code, 404)
        done = self.post("/rename", root="notes", path="a.txt", new_name="c.txt")
        self.assertEqual((done.status_code, done.json()["path"]), (200, "c.txt"))
        self.assertEqual((self.notes / "c.txt").read_text(), "a")
        self.assertEqual((self.notes / "b.txt").read_text(), "b")

    def test_move_between_writable_roots(self):
        (self.notes / "m.txt").write_text("m")
        (self.inbox / "dest").mkdir()
        done = self.post("/move", root="notes", path="m.txt", dest_root="inbox", dest_dir="dest")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json(), {"ok": True, "root": "inbox", "path": "dest/m.txt"})
        self.assertFalse((self.notes / "m.txt").exists())
        self.assertEqual((self.inbox / "dest" / "m.txt").read_text(), "m")

    def test_move_conflicts_and_loops(self):
        (self.notes / "m.txt").write_text("m")
        (self.inbox / "m.txt").write_text("other")
        self.assertEqual(self.post("/move", root="notes", path="m.txt", dest_root="inbox", dest_dir="").status_code, 409)
        self.assertEqual((self.inbox / "m.txt").read_text(), "other")
        self.assertEqual(self.post("/move", root="notes", path="m.txt", dest_root="notes", dest_dir="").status_code, 400)
        self.assertEqual(self.post("/move", root="notes", path="m.txt", dest_root="inbox", dest_dir="nope").status_code, 404)
        (self.notes / "d" / "sub").mkdir(parents=True)
        self.assertEqual(self.post("/move", root="notes", path="d", dest_root="notes", dest_dir="d/sub").status_code, 400)
        self.assertTrue((self.notes / "d" / "sub").is_dir())

    def test_copy_file_and_folder_and_collisions(self):
        (self.notes / "d" / "inner").mkdir(parents=True)
        (self.notes / "d" / "inner" / "f.txt").write_text("f")
        (self.notes / "n.txt").write_text("n")
        self.assertEqual(self.post("/copy", root="notes", path="d", dest_root="inbox", dest_dir="").status_code, 200)
        self.assertEqual((self.inbox / "d" / "inner" / "f.txt").read_text(), "f")
        second = self.post("/copy", root="notes", path="d", dest_root="inbox", dest_dir="")
        self.assertEqual(second.json()["path"], "d (2)")
        dup = self.post("/copy", root="notes", path="n.txt", dest_root="notes", dest_dir="")
        self.assertEqual(dup.json()["path"], "n (2).txt")
        self.assertEqual((self.notes / "n.txt").read_text(), "n")
        self.assertEqual(self.post("/copy", root="notes", path="d", dest_root="notes", dest_dir="d/inner").status_code, 400)

    def test_every_change_is_audited_with_root_and_path_but_no_content(self):
        self.post("/write", root="notes", path="secret-note.txt", content="TOP-SECRET-CONTENT")
        self.post("/mkdir", root="notes", path="d")
        self.post("/rename", root="notes", path="secret-note.txt", new_name="r.txt")
        self.post("/copy", root="notes", path="r.txt", dest_root="inbox", dest_dir="")
        self.post("/move", root="notes", path="r.txt", dest_root="inbox", dest_dir="d" if False else "")
        self.post("/delete", root="notes", path="d", confirm=True)
        events = {e["event"]: e for e in self.audit_events() if e["event"].startswith("fileops_")}
        self.assertEqual(set(events), {"fileops_write", "fileops_mkdir", "fileops_rename", "fileops_copy",
                                       "fileops_delete"} | ({"fileops_move"} if "fileops_move" in events else set()))
        self.assertEqual(events["fileops_write"]["root"], "notes")
        self.assertEqual(events["fileops_write"]["path"], "secret-note.txt")
        self.assertNotIn("TOP-SECRET-CONTENT", self.audit.read_text())

    def test_refused_changes_are_not_audited(self):
        self.post("/write", root="docs", path="x.txt", content="x")
        self.post("/mkdir", root="notes", path="../outside/x")
        self.assertEqual([e for e in self.audit_events() if e["event"].startswith("fileops_")], [])


class TrashTest(Base):
    def test_delete_needs_confirmation(self):
        (self.notes / "n.txt").write_text("n")
        for body in [{"root": "notes", "path": "n.txt"}, {"root": "notes", "path": "n.txt", "confirm": False}]:
            self.assertEqual(self.post("/delete", **body).status_code, 409)
        self.assertEqual(self.client.post("/api/fileops/delete", json={"root": "notes", "path": "n.txt",
                                                                       "confirm": "yes"}).status_code, 422)
        self.assertTrue((self.notes / "n.txt").exists())

    def test_trash_and_restore_round_trip(self):
        (self.notes / "sub").mkdir()
        (self.notes / "sub" / "n.txt").write_text("precious")
        deleted = self.post("/delete", root="notes", path="sub/n.txt", confirm=True)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse((self.notes / "sub" / "n.txt").exists())
        trashed = list((Path(self.config.data_dir) / "trash" / "notes").iterdir())
        names = [p.name for p in trashed if not p.name.startswith(".")]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("-n.txt"), names)
        sidecar = Path(self.config.data_dir) / "trash" / "notes" / ".meta" / f"{names[0]}.json"
        meta = json.loads(sidecar.read_text())
        self.assertEqual((meta["root"], meta["path"]), ("notes", "sub/n.txt"))
        listing = self.get("/trash").json()["items"]
        self.assertEqual(len(listing), 1)
        self.assertEqual((listing[0]["root"], listing[0]["path"], listing[0]["name"]), ("notes", "sub/n.txt", "n.txt"))
        self.assertEqual(listing[0]["id"], deleted.json()["id"])
        restored = self.post("/trash/restore", id=listing[0]["id"])
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["path"], "sub/n.txt")
        self.assertEqual((self.notes / "sub" / "n.txt").read_text(), "precious")
        self.assertEqual(self.get("/trash").json()["items"], [])

    def test_restore_recreates_the_folder_and_avoids_overwriting(self):
        (self.notes / "gone").mkdir()
        (self.notes / "gone" / "n.txt").write_text("old")
        self.post("/delete", root="notes", path="gone", confirm=True)
        item = self.get("/trash").json()["items"][0]
        self.assertTrue(item["is_dir"])
        (self.notes / "gone").mkdir()
        (self.notes / "gone" / "n.txt").write_text("new")
        restored = self.post("/trash/restore", id=item["id"])
        self.assertEqual(restored.json()["path"], "gone (2)")
        self.assertEqual((self.notes / "gone" / "n.txt").read_text(), "new")
        self.assertEqual((self.notes / "gone (2)" / "n.txt").read_text(), "old")

    def test_deleting_twice_the_same_name_keeps_both(self):
        for text in ("first", "second"):
            (self.notes / "n.txt").write_text(text)
            self.assertEqual(self.post("/delete", root="notes", path="n.txt", confirm=True).status_code, 200)
        items = self.get("/trash").json()["items"]
        self.assertEqual(len({i["id"] for i in items}), 2)

    def test_restore_needs_a_valid_id_and_a_writable_root(self):
        for bad in ["", "notes", "notes/", "notes/../x", "../x/y", "notes/.meta", "notes/a/b", "notes/missing",
                    "nope/20260101-000000-x", "notes\\x"]:
            with self.subTest(id=bad):
                self.assertEqual(self.post("/trash/restore", id=bad).status_code, 404)
        (self.notes / "n.txt").write_text("n")
        item_id = self.post("/delete", root="notes", path="n.txt", confirm=True).json()["id"]
        self.config.writable_roots = ["inbox"]
        response = self.post("/trash/restore", id=item_id)
        self.assertEqual((response.status_code, response.json()["detail"]), (403, READ_ONLY))
        self.assertEqual(len(self.get("/trash").json()["items"]), 1)

    def test_empty_trash_needs_confirmation_and_really_deletes_only_the_trash(self):
        (self.notes / "n.txt").write_text("n")
        (self.notes / "keep.txt").write_text("keep")
        (self.inbox / "i").mkdir()
        (self.inbox / "i" / "x.txt").write_text("x")
        self.post("/delete", root="notes", path="n.txt", confirm=True)
        self.post("/delete", root="inbox", path="i", confirm=True)
        self.assertEqual(self.post("/trash/empty").status_code, 409)
        self.assertEqual(len(self.get("/trash").json()["items"]), 2)
        done = self.post("/trash/empty", confirm=True)
        self.assertEqual((done.status_code, done.json()["removed"]), (200, 2))
        self.assertEqual(self.get("/trash").json()["items"], [])
        self.assertEqual((self.notes / "keep.txt").read_text(), "keep")
        self.assertEqual(self.post("/trash/empty", confirm=True).json()["removed"], 0)
        left = [p for p in (Path(self.config.data_dir) / "trash").rglob("*") if p.is_file()]
        self.assertEqual(left, [])

    def test_empty_trash_can_be_limited_to_one_root(self):
        (self.notes / "n.txt").write_text("n")
        (self.inbox / "i.txt").write_text("i")
        self.post("/delete", root="notes", path="n.txt", confirm=True)
        self.post("/delete", root="inbox", path="i.txt", confirm=True)
        self.assertEqual(self.post("/trash/empty", confirm=True, root="notes").json()["removed"], 1)
        self.assertEqual([i["root"] for i in self.get("/trash").json()["items"]], ["inbox"])

    def test_delete_of_a_link_trashes_the_link_not_its_target(self):
        (self.notes / "real.txt").write_text("real")
        self.symlink(self.notes / "real.txt", self.notes / "link.txt")
        self.assertEqual(self.post("/delete", root="notes", path="link.txt", confirm=True).status_code, 200)
        self.assertEqual((self.notes / "real.txt").read_text(), "real")
        self.assertFalse((self.notes / "link.txt").exists())

    def test_trash_events_are_audited(self):
        (self.notes / "n.txt").write_text("n")
        item_id = self.post("/delete", root="notes", path="n.txt", confirm=True).json()["id"]
        self.post("/trash/restore", id=item_id)
        self.post("/delete", root="notes", path="n.txt", confirm=True)
        self.post("/trash/empty", confirm=True)
        events = [e["event"] for e in self.audit_events() if e["event"].startswith("fileops_")]
        self.assertEqual(events, ["fileops_delete", "fileops_restore", "fileops_delete", "fileops_trash_empty"])


class HubOwnFilesTest(Base):
    def test_the_hubs_own_data_cannot_be_changed_through_a_writable_root(self):
        data = self.notes / "hubdata"
        self.config.data_dir = str(data)
        data.mkdir()
        (data / "reminders.json").write_text("[]")
        for path, body in [
            ("/write", {"root": "notes", "path": "hubdata/reminders.json", "content": "x", "etag": sha("[]")}),
            ("/write", {"root": "notes", "path": "hubdata/new.json", "content": "x"}),
            ("/delete", {"root": "notes", "path": "hubdata", "confirm": True}),
            ("/rename", {"root": "notes", "path": "hubdata", "new_name": "x"}),
            ("/move", {"root": "notes", "path": "hubdata/reminders.json", "dest_root": "inbox", "dest_dir": ""}),
            ("/copy", {"root": "notes", "path": "hubdata/reminders.json", "dest_root": "inbox", "dest_dir": ""}),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.post(path, **body).status_code, 403)
        self.assertEqual((data / "reminders.json").read_text(), "[]")

    def test_zip_of_a_folder_holding_the_hubs_data_leaves_it_out(self):
        data = self.notes / "hubdata"
        self.config.data_dir = str(data)
        data.mkdir()
        (data / "secret.json").write_text("{}")
        (self.notes / "ok.txt").write_text("ok")
        response = self.get("/zip", root="notes")
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        self.assertIn("notes/ok.txt", names)
        self.assertFalse([n for n in names if "secret.json" in n], names)


class UploadTest(Base):
    def upload(self, filename="a.txt", data=b"hello", **form):
        return self.client.post("/api/fileops/upload", data=form, files={"file": (filename, data)})

    def test_upload_goes_to_the_inbox_by_default(self):
        response = self.upload("hello.txt", b"hi there")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "root": "inbox", "path": "hello.txt", "name": "hello.txt",
                                           "size": 8})
        self.assertEqual((self.inbox / "hello.txt").read_bytes(), b"hi there")

    def test_upload_into_a_chosen_folder(self):
        (self.notes / "sub").mkdir()
        response = self.upload("n.txt", b"x", root="notes", dir="sub")
        self.assertEqual(response.json()["path"], "sub/n.txt")
        self.assertEqual(self.upload("n.txt", b"x", root="notes", dir="nope").status_code, 404)

    def test_collisions_get_a_numbered_suffix_and_nothing_is_overwritten(self):
        self.upload("a.txt", b"one")
        self.assertEqual(self.upload("a.txt", b"two").json()["name"], "a (2).txt")
        self.assertEqual(self.upload("a.txt", b"three").json()["name"], "a (3).txt")
        self.assertEqual((self.inbox / "a.txt").read_bytes(), b"one")
        self.assertEqual((self.inbox / "a (2).txt").read_bytes(), b"two")

    def test_file_names_are_sanitized(self):
        cases = {
            "../../x": "x", "../../../etc/passwd": "passwd", "/etc/passwd": "passwd", "C:\\Windows\\evil.txt": "evil.txt",
            "a\x00b.txt": "ab.txt", "CON": "_CON", "con.txt": "_con.txt", "NUL": "_NUL", "nul.tar.gz": "_nul.tar.gz",
            "COM1": "_COM1", "lpt9.txt": "_lpt9.txt", "report.txt.": "report.txt", "report.txt   ": "report.txt",
            "  lead.txt": "lead.txt", "..": "upload", ".": "upload", "": "upload", "a<b>:c|d?.txt": "a_b__c_d_.txt",
            "dir/": "dir", "tab\tname.txt": "tab_name.txt", "...": "upload",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(fileops.clean_name(raw) or "upload", expected)
        # and through the route: nothing may end up outside the inbox
        for raw in ["../../x", "/etc/passwd", "a\x00b.txt", "CON", "evil.txt."]:
            response = self.upload(raw, b"data")
            self.assertEqual(response.status_code, 200, raw)
            saved = (self.inbox / response.json()["name"]).resolve()
            self.assertEqual(saved.parent, self.inbox.resolve())
        self.assertFalse((self.tmp / "x").exists())
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()),
                         sorted(["x", "passwd", "ab.txt", "_CON", "evil.txt"]))

    def test_upload_limit_is_enforced_while_streaming_and_leaves_nothing_behind(self):
        original = fileops.MAX_UPLOAD
        fileops.MAX_UPLOAD = 1000
        self.addCleanup(setattr, fileops, "MAX_UPLOAD", original)
        self.assertEqual(self.upload("ok.bin", b"x" * 900).status_code, 200)
        big = self.upload("big.bin", b"x" * 5000)
        self.assertEqual(big.status_code, 413)
        self.assertIn("too big", big.json()["detail"])
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ["ok.bin"])
        staging = Path(self.config.data_dir) / "uploads-tmp"
        self.assertEqual(list(staging.iterdir()) if staging.exists() else [], [])

    def test_a_missing_file_field_and_a_non_multipart_body_are_400(self):
        self.assertEqual(self.client.post("/api/fileops/upload", data={"root": "inbox"},
                                          files={"other": ("a.txt", b"x")}).status_code, 400)
        self.assertEqual(self.client.post("/api/fileops/upload", json={"root": "inbox"}).status_code, 400)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_two_files_in_one_upload_are_refused(self):
        response = self.client.post("/api/fileops/upload",
                                    files=[("file", ("a.txt", b"1")), ("file", ("b.txt", b"2"))])
        self.assertEqual(response.status_code, 400)

    def test_upload_is_audited_without_content(self):
        self.upload("a.txt", b"SECRET-BYTES")
        event = [e for e in self.audit_events() if e["event"] == "fileops_upload"][0]
        self.assertEqual((event["root"], event["path"]), ("inbox", "a.txt"))
        self.assertNotIn("SECRET-BYTES", self.audit.read_text())

    def test_clean_name_and_valid_name(self):
        self.assertTrue(fileops.valid_name("notes (2).md"))
        self.assertTrue(fileops.valid_name(".hidden"))
        for bad in ["", ".", "..", "a/b", "a\\b", "CON", "Aux.txt", "x.", "x ", " x", "a\x00", "a:b"]:
            self.assertFalse(fileops.valid_name(bad), bad)
        self.assertLessEqual(len(fileops.clean_name("a" * 500 + ".txt")), fileops.MAX_NAME)
        self.assertTrue(fileops.clean_name("a" * 500 + ".txt").endswith(".txt"))


class ZipTest(Base):
    def make_tree(self):
        (self.docs / "proj" / "sub").mkdir(parents=True)
        (self.docs / "proj" / "a.txt").write_text("alpha")
        (self.docs / "proj" / "sub" / "b.txt").write_text("beta" * 1000)
        (self.docs / "proj" / "empty").mkdir()

    def test_zip_of_a_folder_in_a_read_only_root(self):
        self.make_tree()
        response = self.get("/zip", root="docs", path="proj")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn('filename="proj.zip"', response.headers["content-disposition"])
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertIsNone(archive.testzip())
        self.assertEqual(sorted(archive.namelist()), ["proj/", "proj/a.txt", "proj/empty/", "proj/sub/", "proj/sub/b.txt"])
        self.assertEqual(archive.read("proj/a.txt"), b"alpha")
        self.assertEqual(archive.read("proj/sub/b.txt"), b"beta" * 1000)

    def test_zip_of_a_whole_root_and_of_a_single_file(self):
        self.make_tree()
        whole = zipfile.ZipFile(io.BytesIO(self.get("/zip", root="docs").content))
        self.assertIn("docs/proj/a.txt", whole.namelist())
        single = zipfile.ZipFile(io.BytesIO(self.get("/zip", root="docs", path="proj/a.txt").content))
        self.assertEqual(single.namelist(), ["a.txt"])

    def test_zip_over_the_cap_is_refused_before_streaming(self):
        self.make_tree()
        original = fileops.MAX_ZIP
        fileops.MAX_ZIP = 100
        self.addCleanup(setattr, fileops, "MAX_ZIP", original)
        response = self.get("/zip", root="docs", path="proj")
        self.assertEqual(response.status_code, 413)
        self.assertIn("too big", response.json()["detail"])

    def test_zip_of_something_missing_is_404(self):
        self.assertEqual(self.get("/zip", root="docs", path="nope").status_code, 404)

    def test_a_link_that_stays_inside_is_not_followed(self):
        self.make_tree()
        self.symlink(self.docs / "proj", self.docs / "proj" / "loop")
        names = zipfile.ZipFile(io.BytesIO(self.get("/zip", root="docs", path="proj").content)).namelist()
        self.assertFalse([n for n in names if "loop" in n])


class LookingAroundTest(Base):
    def test_recent_lists_newest_first_across_roots_and_skips_hidden_things(self):
        def make(path, age):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
            os.utime(path, (1_700_000_000 - age, 1_700_000_000 - age))

        make(self.notes / "new.txt", 0)
        make(self.inbox / "mid.txt", 100)
        make(self.docs / "old.md", 1000)
        make(self.notes / ".hidden.txt", -50)
        make(self.notes / ".git" / "config", -60)
        make(self.docs / "node_modules" / "pkg" / "index.js", -70)
        make(self.docs / ".secret" / "k.txt", -80)
        make(self.notes / "__pycache__" / "x.pyc", -90)
        files = self.get("/recent", limit=3).json()["files"]
        self.assertEqual([(f["root"], f["path"]) for f in files],
                         [("notes", "new.txt"), ("inbox", "mid.txt"), ("docs", "old.md")])
        self.assertEqual([f["writable"] for f in files], [True, True, False])
        self.assertEqual(len(self.get("/recent", limit=1).json()["files"]), 1)
        self.assertEqual(self.get("/recent", limit=0).status_code, 422)
        paths = [f["path"] for f in self.get("/recent", limit=100).json()["files"]]
        for hidden in [".hidden.txt", ".git/config", "node_modules/pkg/index.js", ".secret/k.txt", "x.pyc"]:
            self.assertFalse([p for p in paths if hidden in p], hidden)

    def test_recent_does_not_follow_a_link_out_of_a_root(self):
        self.symlink(self.outside / "secret.txt", self.notes / "leak.txt")
        paths = [f["path"] for f in self.get("/recent").json()["files"]]
        self.assertNotIn("leak.txt", paths)

    def test_search_finds_names_across_roots_in_the_module_search_shape(self):
        (self.notes / "plan-budget.md").write_text("x")
        (self.docs / "sub").mkdir()
        (self.docs / "sub" / "Budget 2026.txt").write_text("x")
        results = self.get("/search", q="budget").json()["results"]
        by_title = {r["title"]: r for r in results}
        self.assertEqual(set(by_title), {"plan-budget.md", "Budget 2026.txt"})
        hit = by_title["plan-budget.md"]
        self.assertEqual((hit["kind"], hit["open"]), ("file", {"view": "files", "id": "notes:plan-budget.md"}))
        self.assertEqual(by_title["Budget 2026.txt"]["open"]["id"], "docs:sub/Budget 2026.txt")
        self.assertEqual(self.get("/search", q="").json()["results"], [])

    def test_search_returns_at_most_twenty(self):
        for i in range(30):
            (self.notes / f"match-{i}.txt").write_text("x")
        self.assertEqual(len(self.get("/search", q="match").json()["results"]), 20)

    def test_stats_per_root(self):
        (self.notes / "a.txt").write_text("12345")
        (self.notes / "d").mkdir()
        (self.notes / "d" / "b.txt").write_text("123")
        data = self.get("/stats", root="notes").json()["roots"]
        self.assertEqual(len(data), 1)
        self.assertEqual((data[0]["name"], data[0]["files"], data[0]["folders"], data[0]["bytes"]), ("notes", 2, 1, 8))
        self.assertTrue(data[0]["writable"])
        self.assertGreater(data[0]["disk"]["total"], 0)
        self.assertNotIn(str(self.tmp), json.dumps(data))
        self.assertEqual({r["name"] for r in self.get("/stats").json()["roots"]}, {"docs", "notes", "inbox"})
        self.assertEqual(self.get("/stats", root="nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
