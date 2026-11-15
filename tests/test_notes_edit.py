"""Editing a note's words and deleting a note (to the trash, never for good)."""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hub import notes
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import TOKEN, make_config


class StoreTest(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.store = notes.NoteStore(base / "notes", trash_dir=base / "trash" / "notes")
        self.base = base

    def test_editing_the_words_keeps_title_tags_and_the_flag(self):
        note = self.store.add("first draft of the plan")
        self.store.apply_auto(note.id, "Plan", ["work"])
        changed = self.store.edit(note.id, body="second draft\r\nwith two lines")
        self.assertEqual(changed.body, "second draft\nwith two lines")
        again = self.store.get(note.id)
        self.assertEqual((again.title, again.tags, again.tagged), ("Plan", ["work"], True))
        self.assertEqual(again.body, "second draft\nwith two lines")

    def test_editing_the_words_does_not_stop_the_model_labelling_an_untouched_note(self):
        note = self.store.add("a thought")
        self.store.edit(note.id, body="a better thought")
        self.assertIsNotNone(self.store.apply_auto(note.id, "Thought", ["idea"]))

    def test_body_that_looks_like_a_header_stays_body(self):
        note = self.store.add("x")
        self.store.edit(note.id, body='---\ntitle: "evil"\n---\nstill the body')
        again = self.store.get(note.id)
        self.assertEqual(again.title, "X")
        self.assertIn("still the body", again.body)

    def test_empty_huge_and_nul_bodies_are_refused(self):
        note = self.store.add("keep me")
        for bad, status in (("   ", 400), ("x" * (notes.MAX_BODY + 1), 413), ("a\x00b", 400)):
            with self.assertRaises(notes.NoteError) as ctx:
                self.store.edit(note.id, body=bad)
            self.assertEqual(ctx.exception.status, status)
        self.assertEqual(self.store.get(note.id).body, "keep me")

    def test_delete_moves_the_file_to_the_trash_with_a_sidecar(self):
        note = self.store.add("bye")
        trash_id = self.store.delete(note.id)
        self.assertEqual(self.store.all(), [])
        entry = self.base / "trash" / "notes" / trash_id.split("/", 1)[1]
        self.assertTrue(entry.is_file())
        self.assertIn("bye", entry.read_text(encoding="utf-8"))
        meta = json.loads((self.base / "trash" / "notes" / ".meta" / f"{entry.name}.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["root"], meta["path"]), ("notes", f"{note.id}.md"))

    def test_delete_of_unknown_or_tricky_ids(self):
        for bad, status in (("nope", 404), ("../x", 400), ("..\\x", 400), ("a/b", 400), ("", 400)):
            with self.assertRaises(notes.NoteError) as ctx:
                self.store.delete(bad)
            self.assertEqual(ctx.exception.status, status, bad)

    def test_a_file_outside_the_notes_folder_is_never_touched(self):
        outside = self.base / "secret.md"
        outside.write_text("keep", encoding="utf-8")
        for bad in ("../secret", "..%2Fsecret"):
            with self.assertRaises(notes.NoteError):
                self.store.delete(bad)
            with self.assertRaises(notes.NoteError):
                self.store.edit(bad, body="overwritten")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_no_trash_folder_means_no_delete(self):
        bare = notes.NoteStore(self.base / "n2")
        note = bare.add("stay")
        with self.assertRaises(notes.NoteError):
            bare.delete(note.id)
        self.assertEqual(len(bare.all()), 1)

    def test_two_deletes_in_one_second_do_not_collide(self):
        a = self.store.add("same words")
        b = self.store.add("same words")
        self.assertNotEqual(self.store.delete(a.id), self.store.delete(b.id))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.config.writable_roots = ["notes"]
        self.audit = self.root / "audit.log"
        deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
        self.client = TestClient(create_app(self.config, deps, audit_path=self.audit),
                                 base_url="http://testserver", headers={"X-Hub": "1"})
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.post("/api/login", json={"token": TOKEN}).status_code, 200)
        self.note = self.client.post("/api/notes", json={"text": "write the report"}).json()
        self.url = f"/api/notes/{self.note['id']}"

    def events(self):
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_patch_body_saves_and_is_audited_without_the_text(self):
        reply = self.client.patch(self.url, json={"body": "write the report by Friday"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["body"], "write the report by Friday")
        self.assertEqual(self.client.get(self.url).json()["body"], "write the report by Friday")
        edits = [e for e in self.events() if e["event"] == "note_edit"]
        self.assertEqual(edits[-1]["body_changed"], True)
        self.assertNotIn("Friday", self.audit.read_text(encoding="utf-8"))

    def test_patch_title_and_tags_still_work(self):
        reply = self.client.patch(self.url, json={"title": "Report", "tags": "work"}).json()
        self.assertEqual((reply["title"], reply["tags"], reply["body"]), ("Report", ["work"], "write the report"))

    def test_patch_refusals(self):
        self.assertEqual(self.client.patch(self.url, json={"body": "  "}).status_code, 400)
        self.assertEqual(self.client.patch(self.url, json={"body": "x" * 20001}).status_code, 413)
        self.assertEqual(self.client.patch("/api/notes/nope", json={"body": "x"}).status_code, 404)
        self.assertIn(self.client.patch("/api/notes/..%2Fhub", json={"body": "x"}).status_code, (404, 405))
        self.assertEqual(self.client.get(self.url).json()["body"], "write the report")

    def test_delete_goes_to_the_trash_and_restores(self):
        reply = self.client.delete(self.url)
        self.assertEqual(reply.status_code, 200)
        trash_id = reply.json()["trash_id"]
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get("/api/notes").json()["notes"], [])
        self.assertTrue(any(e["event"] == "note_delete" and e["id"] == self.note["id"] for e in self.events()))
        listing = self.client.get("/api/fileops/trash").json()["items"]
        self.assertEqual([i["id"] for i in listing], [trash_id])
        restored = self.client.post("/api/fileops/trash/restore", json={"id": trash_id})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.client.get(self.url).json()["body"], "write the report")

    def test_delete_refusals(self):
        self.assertEqual(self.client.delete("/api/notes/nope").status_code, 404)
        self.assertIn(self.client.delete("/api/notes/..%2Fdocs%2Fa").status_code, (404, 405))
        self.assertTrue((self.root / "docs" / "a.md").exists())
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.assertEqual(self.client.delete(self.url).status_code, 404)

    def test_login_and_x_hub_are_required(self):
        anon = TestClient(self.client.app, base_url="http://testserver", headers={"X-Hub": "1"})
        self.assertEqual(anon.delete(self.url).status_code, 401)
        self.assertEqual(anon.patch(self.url, json={"body": "x"}).status_code, 401)
        no_header = TestClient(self.client.app, base_url="http://testserver", cookies=self.client.cookies)
        self.assertIn(no_header.delete(self.url).status_code, (400, 401, 403))
        self.assertEqual(self.client.get(self.url).status_code, 200)


if __name__ == "__main__":
    unittest.main()
