"""Runs tests/nav_back_check.js (Android Back behaviour) and tests/perf_check.js (phone performance guard) against the dev
server when node, playwright and a browser are available; skips cleanly otherwise. Also tests the pure back rules."""
import socket
import subprocess
import sys
import time
import unittest

from tests.test_ui_overflow import ENV, NODE, ROOT, _browser_ready


@unittest.skipUnless(NODE and _browser_ready(ENV), "node + playwright + a browser are not available")
class BrowserChecks(unittest.TestCase):
    def setUp(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = subprocess.Popen([sys.executable, "-m", "hub.devserver", "--port", str(self.port)], cwd=ROOT,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.5).close()
                break
            except OSError:
                time.sleep(0.25)

    def _stop(self):
        self.server.terminate()
        self.server.wait(timeout=10)

    def run_js(self, name):
        r = subprocess.run([NODE, str(ROOT / "tests" / name), f"http://127.0.0.1:{self.port}"], env=ENV,
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_android_back(self):
        self.run_js("nav_back_check.js")

    def test_phone_performance(self):
        self.run_js("perf_check.js")


@unittest.skipUnless(NODE, "node is not available")
class BackRules(unittest.TestCase):
    def test_rules(self):
        code = """
import { backAction, shouldReplace } from '%s';
const sub = {id:'notes', nav:false, under:'apps'}, top = {id:'apps', nav:true};
const out = [
 backAction({layers:1, current:'notes', view:sub}),
 backAction({current:'notes', view:sub}),
 backAction({current:'apps', view:top}),
 backAction({current:'home', view:{id:'home',nav:true}}),
 backAction({current:'home', armed:true}),
 shouldReplace(top, {id:'work', nav:true}), shouldReplace({id:'home', nav:true}, top), shouldReplace(sub, top)];
console.log(JSON.stringify(out));
""" % (ROOT / "hub/static/js/navstack.js").as_uri()
        r = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), '["layer","history","home","arm","exit",true,false,false]', r.stderr)


if __name__ == "__main__":
    unittest.main()
