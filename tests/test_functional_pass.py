"""Source-level guards for screen bugs found by a click-through (the JS has no unit runner for these two spots)."""
import re
import unittest

from hub.server import STATIC


def js(name: str) -> str:
    return (STATIC / "js" / name).read_text(encoding="utf-8")


class LearnStreak(unittest.TestCase):
    def test_quiz_streak_of_zero_does_not_hide_the_card_streak(self):
        src = js("learn.js")
        # `history.streak_days ?? summary.streak` made a quiz streak of 0 win over a 1+ day card streak.
        self.assertNotIn("history?.streak_days ??", src)
        self.assertRegex(src, r"Math\.max\(history\?\.streak_days \|\| 0, summary\?\.streak \|\| 0\)")


class BrainEmptyState(unittest.TestCase):
    def test_filtered_empty_list_does_not_claim_nothing_was_remembered(self):
        src = js("brain.js")
        self.assertIn("Nothing fits this filter", src)
        # the "remembered yet" text must only be reachable when no filter is active
        self.assertTrue(re.search(r'kind \|\| persona \|\| status !== "open" \? "Nothing here" : "Nothing remembered yet"', src))


if __name__ == "__main__":
    unittest.main()
