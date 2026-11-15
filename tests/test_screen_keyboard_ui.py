"""Runs tests/screen_keyboard_check.js (Remote screen keyboard + explain flow at 390px, fake hub) when node, playwright
and a browser are available."""
import subprocess
import unittest

from tests.test_ui_overflow import ENV, NODE, ROOT, _browser_ready


@unittest.skipUnless(NODE and _browser_ready(ENV), "node + playwright + a browser are not available")
class ScreenKeyboardUiTest(unittest.TestCase):
    def test_keyboard_bar_and_explain_flow(self):
        r = subprocess.run([NODE, str(ROOT / "tests" / "screen_keyboard_check.js")], env=ENV, capture_output=True,
                           text=True, timeout=120, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class JsSyntaxTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is not available")
    def test_screen_js_parses(self):
        r = subprocess.run([NODE, "--check", str(ROOT / "hub" / "static" / "js" / "screen.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
