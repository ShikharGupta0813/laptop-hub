"""Runs tests/fixit_ui_check.js (Friday voice test, provider check, Jarvis text box) against the dev server on a 390px phone
when node, playwright and a browser are available; skips cleanly otherwise."""
import socket
import subprocess
import sys
import time
import unittest

from tests.test_ui_overflow import ENV, NODE, ROOT, _browser_ready


@unittest.skipUnless(NODE and _browser_ready(ENV), "node + playwright + a browser are not available")
class FixitUiTest(unittest.TestCase):
    def test_phone_voice_provider_check_and_jarvis_box(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen([sys.executable, "-m", "hub.devserver", "--port", str(port)], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.5).close()
                    break
                except OSError:
                    time.sleep(0.25)
            r = subprocess.run([NODE, str(ROOT / "tests" / "fixit_ui_check.js"), f"http://127.0.0.1:{port}"],
                               env=ENV, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
