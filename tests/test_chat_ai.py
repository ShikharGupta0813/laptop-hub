"""'AI for this chat': the provider chosen for a turn really is the one used for it, and the reply says who answered
(an `answered_by` event). Also the shared setup-all hint and the DOM null-child guard are present in the static files."""
import re
import unittest
from pathlib import Path

from hub import assistant as A
from hub import common
from tests.fakes_assistant import reply
from tests.test_assistant import Base, collect, of

STATIC = Path(__file__).resolve().parent.parent / "hub" / "static"


class FakeText:
    """A text provider client (Gemini / a CLI login): stream() yields a canned answer."""

    def __init__(self, name, answer, used=None):
        self.name, self.answer, self.calls = name, answer, 0
        if used is not None:
            self.used = used

    async def stream(self, system, prompt):
        self.calls += 1
        yield self.answer


class ChatAiTest(Base):
    def models(self, **clients):
        return lambda provider: clients.get(provider)

    async def test_choosing_gemini_then_claude_changes_who_answers(self):
        gemini = FakeText("gemini", "Gemini says hi.")
        svc = self.service([reply("Claude says hi.")])
        svc.model_for = self.models(gemini=gemini)
        first = await collect(svc.run("s", "tell me a story about rivers", "gemini", False))
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(of(first, "answered_by")[0]["provider"], "gemini")
        second = await collect(svc.run("s", "and one about hills", "claude", False))
        self.assertEqual(gemini.calls, 1, "the next turn must not reach Gemini once Claude was chosen")
        self.assertEqual(len(self.claude.calls), 1)
        self.assertEqual(of(second, "answered_by")[0]["provider"], "claude")

    async def test_a_fallback_is_reported_as_the_provider_that_really_answered(self):
        client = FakeText("chain", "Answered by the fallback.", used="cli:agy")
        svc = self.service()
        svc.model_for = self.models(gemini=client)
        events = await collect(svc.run("s", "explain tides to me please", "gemini", False))
        by = of(events, "answered_by")
        self.assertEqual(by[0]["provider"], "cli:agy")
        self.assertTrue(by[0]["label"])

    async def test_a_cli_login_answers_as_its_own_provider(self):
        agy = FakeText("cli", "agy here.")
        svc = self.service()
        svc.model_for = self.models(**{"cli:agy": agy})
        events = await collect(svc.run("s", "summarise the moon landing", "cli:agy", False))
        self.assertEqual(agy.calls, 1)
        self.assertEqual(of(events, "answered_by")[0]["provider"], "cli:agy")

    async def test_no_answered_by_for_a_greeting_or_an_error(self):
        svc = self.service()
        svc.model_for = self.models(gemini=FakeText("gemini", "x"))
        events = await collect(svc.run("s", "hello", "gemini", False))
        self.assertFalse(of(events, "answered_by"))


class SharedHelpersTest(unittest.TestCase):
    def test_setup_hint_names_the_script(self):
        self.assertIn("scripts\\setup-all.ps1 (installs everything)", common.setup_all_hint("Dictation"))
        self.assertTrue(common.setup_all_hint().startswith("Run "))

    def test_the_js_helper_matches(self):
        js = (STATIC / "js" / "setuphint.js").read_text(encoding="utf8")
        self.assertIn("setup-all.ps1", js)
        self.assertIn("installs everything", js)

    def test_no_screen_tells_the_owner_to_pip_install_optional_packages(self):
        for name in ("dictation.js", "screen.js", "hub.js"):
            text = (STATIC / "js" / name).read_text(encoding="utf8")
            self.assertNotRegex(text, r"pynput sounddevice|requirements-screen\.txt|requirements-voice\.txt", name)

    def test_the_null_child_guard_is_in_api_js_and_list_editor_filters(self):
        api = (STATIC / "js" / "api.js").read_text(encoding="utf8")
        self.assertRegex(api, r"replaceChildren")
        self.assertRegex(api, r"n !== null && n !== undefined")
        editor = (STATIC / "js" / "listeditor.js").read_text(encoding="utf8")
        self.assertIn(".filter(Boolean)", editor)

    def test_new_static_files_are_in_the_service_worker_shell(self):
        sw = (STATIC / "sw.js").read_text(encoding="utf8")
        for path in ("/js/aichat.js", "/css/aichat.css", "/css/sheets.css", "/js/setuphint.js"):
            self.assertIn(f'"{path}"', sw)
        index = (STATIC / "index.html").read_text(encoding="utf8")
        self.assertTrue(re.search(r'href="/css/sheets\.css"', index))


if __name__ == "__main__":
    unittest.main()
