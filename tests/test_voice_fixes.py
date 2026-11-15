"""Bug-fix round from the owner's real phone: Friday/Jarvis silent, 'dot' beeps, Piper missing, one-shot Windows setup.

Python side (panic state, fallback chain) lives in tests/test_tts.py; here: JS syntax, the browser checks, the Android
notification policy mirror, the requirements reconciliation and the setup-all.ps1 script's shape."""
import re
import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "hub" / "static"
NODE = shutil.which("node")
PWSH = shutil.which("pwsh")
WAKE_DIR = ROOT / "mobile" / "android" / "app" / "src" / "main" / "java" / "dev" / "shikhar" / "laptophub" / "wake"


@unittest.skipUnless(NODE, "node is not installed")
class JsSyntax(unittest.TestCase):
    def test_changed_modules_parse(self):
        for name in ("hubtts.js", "voicekit.js", "voices.js", "voicemode.js", "friday.js", "wake.js", "wakebridge.js", "settings.js"):
            src = (STATIC / "js" / name).read_text(encoding="utf-8")
            r = subprocess.run([NODE, "--input-type=module", "--check"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


class StaticChecks(unittest.TestCase):
    def js(self, name):
        return (STATIC / "js" / name).read_text(encoding="utf-8")

    def test_voices_no_longer_maps_every_423_to_the_panic_button(self):
        src = self.js("voices.js")
        self.assertNotIn("paused by the panic button", src)
        self.assertIn("body.reason", src)

    def test_wake_never_restarts_faster_than_four_seconds(self):
        src = self.js("wake.js")
        self.assertIn("MIN_RESTART_MS = 4000", src)
        self.assertNotIn("BACKOFF_BASE_MS = 500;", src)
        self.assertIn("hub.wake.inpage", src)          # the explicit in-app opt-in
        self.assertIn("setNativeWakeRunning", self.js("wakebridge.js"))
        self.assertIn("Use in-page wake word", self.js("settings.js"))

    def test_voice_queue_waits_long_enough_for_windows_voices(self):
        self.assertIn("timeoutMs: 8000", self.js("voicemode.js"))

    def test_notification_channels_are_silent(self):
        policy = (WAKE_DIR / "NotifyPolicy.java").read_text(encoding="utf-8")
        service = (WAKE_DIR / "WakeService.java").read_text(encoding="utf-8")
        self.assertNotIn("IMPORTANCE_HIGH", service)    # the loud channel is gone from the service itself
        self.assertIn("NotifyPolicy.listenImportance()", service)
        self.assertIn("setSound(null, null)", service)
        self.assertIn("enableVibration(false)", service)
        self.assertEqual(service.count("setOnlyAlertOnce(") >= 2, True)
        self.assertIn('CH_ALERT = "wake_alert_quiet"', policy)       # a NEW id: old channel sound settings stick to the id
        self.assertIn('"wake_alert"', policy.split("LEGACY_CHANNELS")[1])
        self.assertTrue((ROOT / "mobile/android/app/src/test/java/dev/shikhar/laptophub/wake/NotifyPolicyTest.java").is_file())

    def test_no_other_notification_channel_in_the_app(self):
        for p in (ROOT / "mobile" / "android" / "app" / "src" / "main").rglob("*.java"):
            if p.name in ("WakeService.java", "NotifyPolicy.java", "AlertWatcher.java"):   # AlertWatcher: the urgent-alert channel (#30)
                continue
            self.assertNotIn("NotificationChannel(", p.read_text(encoding="utf-8"), p.name)


class Requirements(unittest.TestCase):
    def test_everything_the_app_says_not_installed_about_is_listed(self):
        text = "\n".join((ROOT / n).read_text(encoding="utf-8") for n in (
            "requirements.txt", "requirements-voice.txt", "requirements-work.txt", "requirements-laptop.txt",
            "requirements-extra.txt", "requirements-screen.txt")).lower()
        for pkg in ("piper-tts", "pynput", "sounddevice", "pywinpty", "pyautogui", "playwright", "mss", "pillow", "keyring",
                    "faster-whisper", "pypdf", "pygetwindow", "pywinauto", "soundcard", "numpy"):
            self.assertIn(pkg, text, pkg)
        self.assertIn("av==16.1.0", (ROOT / "requirements-voice.txt").read_text(encoding="utf-8"))

    def test_doctor_reads_every_requirements_file(self):
        from hub import doctor_checks as dc
        for name in ("requirements.txt", "requirements-voice.txt", "requirements-work.txt", "requirements-laptop.txt",
                     "requirements-extra.txt", "requirements-screen.txt"):
            self.assertIn(name, dc.REQUIREMENT_FILES)

    def test_doctor_voice_check_prefers_something_that_works(self):
        from hub import doctor_checks as dc
        from unittest import mock
        with mock.patch.object(dc.importlib.util, "find_spec", return_value=None):   # whatever is really installed here
            ok, text = dc.check_voice("win32", which=lambda n: None)
        self.assertTrue(ok)
        self.assertIn("setup-all", text)               # tells the owner how to get Piper too

    def test_in_app_hints_point_at_the_script(self):
        for rel in ("hub/formfill.py", "hub/computeruse.py", "hub/dictation.py", "hub/screen.py", "hub/laptop.py", "hub/tts.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertTrue("setup-all.ps1" in text or "setup_all_hint" in text, rel)   # the shared helper builds the sentence


class SetupScript(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "scripts" / "setup-all.ps1").read_text(encoding="utf-8")

    def test_plain_ascii_and_balanced(self):
        for name in ("setup-all.ps1", "start-hub.ps1"):
            t = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertEqual([c for c in t if ord(c) > 127], [], name)      # Windows PowerShell 5.1 reads BOM-less files as ANSI
            self.assertEqual(t.count("{"), t.count("}"), name)
            self.assertEqual(t.count("("), t.count(")"), name)

    def test_does_everything_that_was_asked(self):
        for needle in ("3.11", "winget install -e --id Python.Python.3.12", ".venv", "requirements-voice.txt", "requirements-work.txt",
                       "requirements-laptop.txt", "requirements-extra.txt", "requirements-screen.txt", "av==16.1.0", "piper-tts", "pynput",
                       "sounddevice", "pywinpty", "pyautogui", "playwright install chromium", "Ollama.Ollama", "moondream", "tailscale",
                       "agy --help", "hub setup --all", "hub voice --download", "start-hub.ps1", "CHECKLIST", "next:"):
            self.assertIn(needle, self.src, needle)

    def test_asks_before_installing_and_never_touches_secrets_or_deletes(self):
        self.assertIn("Ask 'Install Python", self.src)
        self.assertIn("Ask 'Install Ollama", self.src)
        self.assertNotRegex(self.src, r"Remove-Item|rm -rf|Set-ExecutionPolicy|GEMINI_API_KEY|Invoke-Expression|iex ")

    def test_start_script_uses_the_venv_python(self):
        t = (ROOT / "scripts" / "start-hub.ps1").read_text(encoding="utf-8")
        self.assertIn(".venv\\Scripts\\python.exe", t)
        self.assertIn("-m hub serve", t)

    @unittest.skipUnless(PWSH, "pwsh is not installed: the script was reviewed by hand, not parsed")
    def test_powershell_parses(self):
        for name in ("setup-all.ps1", "start-hub.ps1"):
            cmd = f"$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('{ROOT / 'scripts' / name}',[ref]$null,[ref]$e) | Out-Null; if ($e) {{ $e; exit 1 }}"
            r = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def _playwright():
    if not NODE:
        return False
    return subprocess.run([NODE, "-e", "require.resolve('playwright')"], capture_output=True, text=True).returncode == 0


@unittest.skipUnless(NODE and _playwright(), "node + playwright are not available")
class BrowserChecks(unittest.TestCase):
    def test_voice_fix_e2e(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen(["python", "-m", "hub.devserver", "--port", str(port)], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.5).close()
                    break
                except OSError:
                    time.sleep(0.25)
            r = subprocess.run([NODE, str(ROOT / "tests" / "voice_fix_e2e.js"), f"http://127.0.0.1:{port}"],
                               capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
