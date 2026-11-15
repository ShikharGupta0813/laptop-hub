"""Runs tests/chat_ai_check.js ("AI for this chat" strip and reply path at 390px and 320px, with a scripted fake hub) and
tests/ui_nulltext_check.js (no visible 'null'/'undefined' on any screen or Settings sheet at 390px) against the dev server when
node, playwright and a browser are available; skips cleanly otherwise."""
import socket
import subprocess
import sys
import time
import unittest

from tests.test_ui_overflow import ENV, NODE, ROOT, _browser_ready


@unittest.skipUnless(NODE and _browser_ready(ENV), "node + playwright + a browser are not available")
class ChatAiUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = subprocess.Popen([sys.executable, "-m", "hub.devserver", "--port", str(cls.port)], cwd=ROOT,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.5).close()
                break
            except OSError:
                time.sleep(0.25)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)

    def _run(self, script, timeout):
        r = subprocess.run([NODE, str(ROOT / "tests" / script), f"http://127.0.0.1:{self.port}"],
                           env=ENV, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_ai_for_this_chat_strip_and_reply_path(self):
        self._run("chat_ai_check.js", 240)

    def test_no_null_or_undefined_text_on_any_screen(self):
        self._run("ui_nulltext_check.js", 300)


if __name__ == "__main__":
    unittest.main()
