"""A bare hello gets a fixed reply: no model call, no tools, no approvals."""
import unittest

from hub import persona
from tests.fakes_assistant import reply
from tests.test_full_control import Base, collect, of


class GreetingReplyTest(unittest.TestCase):
    def test_hellos_are_answered(self):
        for text in ("hey jarvis", "Hi Friday!", "hello", "jarvis", "good morning jarvis", "okay friday?"):
            self.assertTrue(persona.greeting_reply(text), text)

    def test_friday_and_jarvis_sound_different(self):
        self.assertIn("day", persona.greeting_reply("hey friday"))
        self.assertIn("At your service", persona.greeting_reply("hey jarvis"))

    def test_real_requests_are_left_alone(self):
        for text in ("hey jarvis open notes", "run the tests", "how is my day", "hi, what is on my calendar", ""):
            self.assertEqual(persona.greeting_reply(text), "", text)


class GreetingRunTest(Base):
    async def test_hello_never_reaches_the_model(self):
        svc = self.service([reply("this must not be used")])
        events = await collect(svc.run("s", "hey jarvis", "claude", False))
        self.assertEqual(len(svc._claude.calls if hasattr(svc._claude, "calls") else []), 0)
        self.assertFalse(of(events, "tool_start"))
        self.assertFalse(of(events, "approval_needed"))
        self.assertIn("Chief", "".join(e.get("t", "") for e in of(events, "text")))


if __name__ == "__main__":
    unittest.main()
