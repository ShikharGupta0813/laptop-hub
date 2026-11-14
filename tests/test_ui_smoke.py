"""Runs tests/ui_smoke_check.js (every screen at 390x844 and 360x740) against the dev server when node, playwright and
a browser are available; skips cleanly otherwise."""
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_ui_overflow import ENV, NODE, ROOT, _browser_ready


@unittest.skipUnless(NODE and _browser_ready(ENV), "node + playwright + a browser are not available")
class UiSmokeTest(unittest.TestCase):
    def test_every_screen_loads_without_errors(self):
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
            shots = Path(tempfile.gettempdir()) / "redline-ui-smoke-shots"
            shots.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([NODE, str(ROOT / "tests" / "ui_smoke_check.js"), f"http://127.0.0.1:{port}", str(shots)],
                               env=ENV, capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
