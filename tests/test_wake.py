"""Wake word ("Hey Jarvis" / "Hey Friday"): the pure phrase matcher (run through node when it exists), the shell files,
and an optional browser check with a fake SpeechRecognition (skipped without node + playwright)."""
import json
import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "hub" / "static"
NODE = shutil.which("node")

RUNNER = """
import { matchWake } from %(url)s;
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cases.map(([t, opts]) => matchWake(t, opts || {}))));
"""


def run_matcher(cases):
    script = RUNNER % {"url": json.dumps((STATIC / "js" / "wakephrase.js").as_uri())}
    out = subprocess.run([NODE, "--input-type=module", "-e", script, json.dumps(cases)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class WakePhraseTests(unittest.TestCase):
    def test_wakes(self):
        cases = [
            ("hey jarvis", "jarvis", ""), ("Hey Jarvis", "jarvis", ""), ("hey jarvis what's the time", "jarvis", "what's the time"),
            ("Hey Jarvis, remember that the RCA was a bad deploy", "jarvis", "remember that the RCA was a bad deploy"),
            ("ok jarvis run the tests", "jarvis", "run the tests"), ("okay jarvis", "jarvis", ""), ("hi jarvis!", "jarvis", ""),
            ("yo jarvis open notes", "jarvis", "open notes"), ("hey jarvas remember this", "jarvis", "remember this"),
            ("hey jarves what's up", "jarvis", "what's up"), ("hey, jarvis do it", "jarvis", "do it"),
            ("hey friday", "friday", ""), ("Hey Friday, what's my day", "friday", "what's my day"),
            ("hey friday what's my day", "friday", "what's my day"), ("ok friday plan tomorrow", "friday", "plan tomorrow"),
            ("  HEY FRIDAY  ", "friday", ""),
        ]
        got = run_matcher([[c[0]] for c in cases])
        for (text, persona, rest), m in zip(cases, got):
            self.assertEqual(m, {"persona": persona, "rest": rest}, text)

    def test_does_not_wake(self):
        for text in ["Jarvis is a nice name", "jarvis", "friday evening plans", "hey friday evening plans", "hey service",
                     "the service is slow", "see you friday", "I said hey to jarvis", "hey jarvis is", "hey jar", "what is friday",
                     "friday", "", "hey", "hey there jarvis", "thank you jarvis", "hey jarvisx", "hey fridays"]:
            self.assertIsNone(run_matcher([[text]])[0], text)

    def test_custom_name(self):
        got = run_matcher([["hey tony run it", {"jarvisName": "Tony"}], ["hey tony run it"], ["hey jarvis run it", {"jarvisName": "Tony"}],
                           ["Hey Friday hi", {"jarvisName": "Tony"}], ["tony is great", {"jarvisName": "Tony"}],
                           ["hey friday hi", {"jarvisName": "Friday"}]])
        self.assertEqual(got[0], {"persona": "jarvis", "rest": "run it"})
        self.assertIsNone(got[1])
        self.assertEqual(got[2], {"persona": "jarvis", "rest": "run it"})
        self.assertEqual(got[3], {"persona": "friday", "rest": "hi"})
        self.assertIsNone(got[4])
        self.assertEqual(got[5]["persona"], "friday")

    def test_matches_python_persona_rules(self):
        """The wake phrase minus its greeting is exactly what hub/persona.py would route."""
        from hub.persona import split_persona
        for text in ["jarvis what's the time", "friday, what's my day"]:
            p, rest = split_persona(text)
            m = run_matcher([["hey " + text]])[0]
            self.assertEqual((m["persona"], m["rest"]), (p, rest))


class WakeFilesTests(unittest.TestCase):
    FILES = ["/js/wake.js", "/js/wakephrase.js", "/css/wake.css"]

    def test_files_exist_and_are_in_the_shell(self):
        sw = (STATIC / "sw.js").read_text(encoding="utf-8")
        shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
        for f in self.FILES:
            self.assertTrue((STATIC / f.lstrip("/")).is_file(), f)
            self.assertIn(f'"{f}"', shell, f)

    def test_wired_in(self):
        self.assertIn("/css/wake.css", (STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertIn("mountWake", (STATIC / "js" / "main.js").read_text(encoding="utf-8"))
        settings = (STATIC / "js" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("setWakeEnabled", settings)
        self.assertIn("screen is on", settings)   # the honest limit is spelled out next to the switch
        wake = (STATIC / "js" / "wake.js").read_text(encoding="utf-8")
        self.assertIn('"hub.wake"', wake)
        self.assertIn("visibilitychange", wake)
        self.assertIn("setPreload", (STATIC / "js" / "voicemode.js").read_text(encoding="utf-8"))

    def test_default_is_off(self):
        wake = (STATIC / "js" / "wake.js").read_text(encoding="utf-8")
        self.assertIn('getItem(KEY) === "1"', wake)   # only an explicit "1" turns it on

    @unittest.skipUnless(NODE, "node is not installed")
    def test_node_check(self):
        for name in ("wake.js", "wakephrase.js", "voicemode.js", "settings.js", "main.js"):
            src = (STATIC / "js" / name).read_text(encoding="utf-8")
            r = subprocess.run([NODE, "--input-type=module", "--check"], input=src, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


def _playwright_dir():
    if not NODE:
        return None
    r = subprocess.run([NODE, "-e", "console.log(require.resolve('playwright'))"], capture_output=True, text=True)
    return r.returncode == 0


@unittest.skipUnless(NODE and _playwright_dir(), "node + playwright are not available")
class WakeBrowserTests(unittest.TestCase):
    def test_fake_recognizer_opens_voice_mode_with_the_words(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen(["python", "-m", "hub.devserver", "--port", str(port)], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(60):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.5).close()
                    break
                except OSError:
                    time.sleep(0.25)
            shots = Path("/tmp/claude-0/shots")
            shots.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([NODE, str(ROOT / "tests" / "wake_e2e.js"), f"http://127.0.0.1:{port}", str(shots)],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
