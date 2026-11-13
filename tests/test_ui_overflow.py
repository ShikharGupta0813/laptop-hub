"""Runs tests/ui_overflow_check.js against the dev server when node + playwright + a browser are available."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _node_env():
    env = dict(os.environ)
    if NODE:
        g = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True) if shutil.which("npm") else None
        if g and g.returncode == 0 and g.stdout.strip():
            env["NODE_PATH"] = g.stdout.strip() + os.pathsep + env.get("NODE_PATH", "")
    return env


def _browser_ready(env):
    if not NODE:
        return False
    code = "const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();await b.close();})().then(()=>process.exit(0),()=>process.exit(1))"
    try:
        return subprocess.run([NODE, "-e", code], env=env, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


ENV = _node_env()


@unittest.skipUnless(NODE and _browser_ready(ENV), "node + playwright + a browser are not available")
class UiOverflowTest(unittest.TestCase):
    def test_no_horizontal_overflow_on_main_screens(self):
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
            shots = Path(tempfile.gettempdir()) / "redline-ui-shots"
            shots.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([NODE, str(ROOT / "tests" / "ui_overflow_check.js"), f"http://127.0.0.1:{port}", str(shots)],
                               env=ENV, capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
