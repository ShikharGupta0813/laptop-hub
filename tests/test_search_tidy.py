"""Global search: friendly dates, no raw note headers in snippets, and no junk 'meaning' hits for nonsense."""
import unittest
from datetime import datetime

from hub import search
from tests.test_search import Base, item

NOW = datetime(2026, 11, 16, 9, 0, 0)   # a Monday


class FriendlyTest(unittest.TestCase):
    def test_near_days_are_relative_and_far_days_are_dated(self):
        self.assertEqual(search.friendly_when(datetime(2026, 11, 16, 17, 0), NOW), "Today, 5:00 pm")
        self.assertEqual(search.friendly_when(datetime(2026, 11, 17, 0, 5), NOW), "Tomorrow, 12:05 am")
        self.assertEqual(search.friendly_when(datetime(2026, 11, 15, 12, 30), NOW), "Yesterday, 12:30 pm")
        self.assertEqual(search.friendly_when(datetime(2026, 11, 18, 17, 0), NOW), "Wed 18 Nov, 5:00 pm")
        self.assertEqual(search.friendly_when(datetime(2027, 1, 2, 8, 0), NOW), "Sat 2 Jan 2027, 8:00 am")

    def test_iso_stamps_inside_text_are_replaced(self):
        out = search.friendly_text("Due 2026-11-18T17:00 then 2026-11-16 09:30:00 (2026-02-31T10:00)", NOW)
        self.assertEqual(out, "Due Wed 18 Nov, 5:00 pm then Today, 9:30 am (2026-02-31T10:00)")   # impossible date left alone
        self.assertEqual(search.friendly_text("version 2026-11-18 only", NOW), "version 2026-11-18 only")

    def test_frontmatter_is_dropped_from_snippets(self):
        flat = '--- title: "Buy milk" created: "2026-11-04T10:12:03" source: "voice" tags: ["shopping"] tagged: true --- the words'
        self.assertEqual(search.strip_frontmatter(flat), "the words")
        self.assertEqual(search.strip_frontmatter('---\ntitle: "A"\ntags: []\n---\n\nbody here'), "body here")
        self.assertEqual(search.strip_frontmatter("--- not a header ---"), "--- not a header ---")
        self.assertEqual(search.strip_frontmatter("plain text"), "plain text")


class OverlapTest(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(search.has_word_overlap("budget", "Q3 Budget", ""))
        self.assertTrue(search.has_word_overlap("trip plan", "", "the plan for friday"))
        self.assertFalse(search.has_word_overlap("zzzz", "data.txt", "plain text file"))
        self.assertFalse(search.has_word_overlap("a b", "b a c", ""))   # one-letter words do not count on their own


class RouteTest(Base):
    def test_raw_stamps_and_headers_are_cleaned_in_the_response(self):
        self.fake.answers["/api/reminders/search"] = {"results": [
            item("Call Ravi", "at 2026-11-18T17:00", kind="reminder")]}
        self.fake.answers["/api/kb/search"] = {"results": [
            item("milk.md", '--- title: "Buy milk" created: "2026-11-04T10:12:03" tags: [] tagged: true --- buy milk today', kind="file")]}
        data = self.get("milk").json()
        by_title = {r["title"]: r for r in data["results"]}
        self.assertEqual(by_title["milk.md"]["snippet"], "buy milk today")
        reminders = self.get("Ravi").json()["results"]
        self.assertEqual(reminders[0]["snippet"], "at Wed 18 Nov, 5:00 pm")

    def test_note_titles_and_snippets_get_friendly_dates(self):
        self.notes.add("Quiz with Ravi on 2026-11-06T17:00 sharp")   # the fixed test clock is 2026-11-04
        hit = self.get("Ravi").json()["results"][0]
        self.assertEqual(hit["title"], "Quiz with Ravi on Fri 6 Nov, 5:00 pm sharp")
        self.assertNotIn("T17:00", hit["snippet"])

    def test_nonsense_gets_no_meaning_hits_but_keyword_hits_stay(self):
        self.fake.answers["/api/kb/search"] = {"results": [
            item("data.txt", "plain text file", kind="file"),
            item("report.md", "the sharding report", kind="file")]}
        self.assertEqual(self.get("zzzz").json()["results"], [])
        self.assertEqual(self.get("zzzz").json()["total"], 0)
        titles = [r["title"] for r in self.get("sharding").json()["results"]]
        self.assertEqual(titles, ["report.md"])

    def test_other_sources_are_not_filtered_by_the_floor(self):
        self.fake.answers["/api/todos/search"] = {"results": [item("Pay rent", "", kind="todo")]}
        self.assertEqual([r["title"] for r in self.get("landlord").json()["results"]], ["Pay rent"])


if __name__ == "__main__":
    unittest.main()
