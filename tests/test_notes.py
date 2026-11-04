import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from hub import notes


class SpokenTest(unittest.TestCase):
    def test_note_it_is_removed_from_either_end(self):
        self.assertEqual(notes.clean_spoken("Note it. Buy milk tomorrow"), "Buy milk tomorrow")
        self.assertEqual(notes.clean_spoken("hey, note this: call Ravi"), "call Ravi")
        self.assertEqual(notes.clean_spoken("Buy milk, note it."), "Buy milk")
        self.assertEqual(notes.clean_spoken("Take a note - read chapter 4"), "read chapter 4")

    def test_ordinary_sentences_are_untouched(self):
        self.assertEqual(notes.clean_spoken("Please denote it carefully"), "Please denote it carefully")
        self.assertEqual(notes.clean_spoken("I will note it later in the doc"), "I will note it later in the doc")

    def test_tags_are_tidied(self):
        self.assertEqual(notes.normalise_tags(["Study Plan", "study-plan", " RAG!! ", "", "a_b"]),
                         ["study-plan", "rag", "a-b"])
        self.assertEqual(notes.normalise_tags("[one, Two Words]"), ["one", "two-words"])
        self.assertEqual(len(notes.normalise_tags([f"t{i}" for i in range(20)])), notes.MAX_TAGS)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.clock = [datetime(2026, 11, 4, 9, 0, 0)]
        self.store = notes.NoteStore(Path(tempfile.mkdtemp()) / "notes", now=lambda: self.clock[0])

    def tick(self, seconds=60):
        self.clock[0] = datetime.fromtimestamp(self.clock[0].timestamp() + seconds)

    def test_newest_first_and_ids_sort_by_time(self):
        first = self.store.add("first thought")
        self.tick()
        second = self.store.add("second thought")
        self.assertEqual([n.id for n in self.store.all()], [second.id, first.id])
        self.assertTrue(first.id.startswith("2026-11-04_090000_"))

    def test_two_notes_in_one_second_do_not_collide(self):
        a, b = self.store.add("same words"), self.store.add("same words")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(len(self.store.all()), 2)

    def test_awkward_titles_survive_a_round_trip(self):
        note = self.store.add("x")
        self.store.edit(note.id, title='He said "hi": a\nnew line --- and a colon', tags=["a"])
        again = self.store.get(note.id)
        self.assertEqual(again.title, 'He said "hi": a\nnew line --- and a colon')
        self.assertEqual(again.body, "x")

    def test_the_model_never_overwrites_what_the_user_set(self):
        note = self.store.add("meeting about sharding")
        self.store.edit(note.id, title="My title", tags=["mine"])
        self.assertIsNone(self.store.apply_auto(note.id, "Model title", ["model"]))
        kept = self.store.get(note.id)
        self.assertEqual((kept.title, kept.tags), ("My title", ["mine"]))

    def test_the_model_labels_an_untouched_note_once(self):
        note = self.store.add("meeting about sharding")
        labelled = self.store.apply_auto(note.id, "Sharding meeting", ["db", "meeting"])
        self.assertEqual((labelled.title, labelled.tags, labelled.tagged), ("Sharding meeting", ["db", "meeting"], True))
        self.assertIsNone(self.store.apply_auto(note.id, "Again", ["x"]))

    def test_filters_by_tag_and_text(self):
        a = self.store.add("learn about vector databases")
        self.tick()
        b = self.store.add("buy milk")
        self.store.edit(a.id, tags=["study"])
        self.assertEqual([n.id for n in self.store.all(tag="study")], [a.id])
        self.assertEqual([n.id for n in self.store.all(query="MILK")], [b.id])
        self.assertEqual(self.store.all_tags(), [{"tag": "study", "count": 1}])

    def test_bad_ids_and_empty_notes_are_refused(self):
        for bad in ["../x", "a/b", "", "a.b"]:
            with self.subTest(bad=bad), self.assertRaises(notes.NoteError) as caught:
                self.store.get(bad)
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(notes.NoteError):
            self.store.add("   ")
        with self.assertRaises(notes.NoteError) as caught:
            self.store.get("2026-01-01_000000_missing")
        self.assertEqual(caught.exception.status, 404)

    def test_a_hand_edited_header_still_loads(self):
        note = self.store.add("body text")
        path = self.store.dir / f"{note.id}.md"
        path.write_text("---\ntitle: Plain title\ntags: [alpha, Beta Gamma]\n---\n\nbody text\n", encoding="utf-8")
        loaded = self.store.get(note.id)
        self.assertEqual((loaded.title, loaded.tags, loaded.body), ("Plain title", ["alpha", "beta-gamma"], "body text"))


if __name__ == "__main__":
    unittest.main()
