"""Dictation hotkey (hub/dictation.py). Fakes only: fake hotkey source, microphone, transcriber, keyboard, window title."""
import io
import logging
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import dictation
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

FRAME = b"\x01\x00" * dictation.FRAME


class FakeMic:
    def __init__(self):
        self.reads, self.closed = 0, False

    def read(self, n):
        time.sleep(0.001)
        self.reads += 1
        return FRAME

    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self, *texts):
        self.texts, self.sources = list(texts), []

    def transcribe(self, src):
        self.sources.append(src)
        return self.texts.pop(0) if self.texts else ""


class FakeTyper:
    def __init__(self):
        self.typed, self.backspaces, self.pastes = [], [], 0

    def type_text(self, text):
        self.typed.append(text)

    def backspace(self, n):
        self.backspaces.append(n)

    def paste(self):
        self.pastes += 1

    @property
    def text(self):
        return "".join(self.typed)


class FakeHotkeys:
    def __init__(self):
        self.cb, self.stopped = None, False

    def start(self, cb):
        self.cb = cb

    def stop(self):
        self.stopped = True


class Clip:
    def __init__(self, v="old"):
        self.v = v

    def get(self):
        return self.v

    def set(self, t):
        self.v = t


class Rig:
    def __init__(self, *texts, title="Notepad | notepad.exe", **cfg):
        cfg.setdefault("dictation_enabled", True)
        self.config, _ = make_config(**cfg)
        self.audits, self.chimes, self.asked, self.said = [], [], [], []
        self.title = title
        self.ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda e, **k: self.audits.append((e, k)), notify=lambda *a, **k: None)
        self.tr, self.typer, self.mic, self.keys = FakeTranscriber(*texts), FakeTyper(), FakeMic(), FakeHotkeys()

        def assistant(text, persona, say=None):
            self.asked.append((text, persona))
            return "Done."
        self.d = dictation.Dictation(self.ctx, hotkey_factory=lambda: self.keys, source_factory=lambda dev: self.mic,
                                     transcriber=self.tr, typer_factory=lambda: self.typer, title_provider=lambda: self.title,
                                     assistant=assistant, speaker=self.said.append, chime=self.chimes.append,
                                     clipboard=Clip(), sleep=lambda s: None, run_async=False)
        self.d.min_s = 0
        self.d.start()

    def keydown(self, *names):
        for n in names:
            self.keys.cb("down", n)

    def keyup(self, *names):
        for n in names:
            self.keys.cb("up", n)

    def speak(self, hold=0.03):
        self.keydown("ctrl_l", "alt_l", "space")
        time.sleep(hold)
        self.keyup("space")
        self.keyup("alt_l", "ctrl_l")


class Hotkeys(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(dictation.parse_hotkey("Ctrl+Alt+Space"), ["ctrl", "alt", "space"])
        self.assertEqual(dictation.parse_hotkey("F9"), ["f9"])
        for bad in ["", "a", "ctrl++x", "ctrl+ctrl", "a+b+c+d+e", "ctrl+!"]:
            with self.assertRaises(ValueError, msg=bad):
                dictation.parse_hotkey(bad)

    def test_tracker_ignores_auto_repeat(self):
        out = []
        t = dictation.HotkeyTracker(["ctrl", "space"], out.append)
        for _ in range(3):
            t.event("down", "ctrl_l")
            t.event("down", "space")
        t.event("up", "space")
        self.assertEqual(out, ["press", "release"])

    def test_hold_records_until_release(self):
        r = Rig("hello world")
        r.keydown("ctrl_l", "alt_l", "space")
        self.assertTrue(r.d._recording)
        self.assertEqual(r.d.status()["state"], "recording")
        time.sleep(0.03)
        r.keyup("space")
        self.assertFalse(r.d._recording)
        self.assertEqual(r.typer.text, "Hello world ")
        self.assertTrue(r.mic.closed)
        self.assertEqual(r.chimes, ["start", "done"])
        self.assertEqual(r.d.status()["state"], "ready")

    def test_toggle_press_twice(self):
        r = Rig("one two", dictation_mode="toggle")
        r.speak()                                  # first press starts, release does nothing
        self.assertTrue(r.d._recording)
        self.assertEqual(r.typer.text, "")
        r.speak()                                  # second press stops
        self.assertFalse(r.d._recording)
        self.assertEqual(r.typer.text, "One two ")

    def test_pause_and_off_ignore_the_hotkey(self):
        r = Rig("x", dictation_paused=True)
        r.speak()
        self.assertFalse(r.d._recording)
        self.assertEqual(r.d.status()["state"], "paused")
        r2 = Rig("x", dictation_enabled=False)
        r2.d.on_combo("press")
        self.assertFalse(r2.d._recording)

    def test_changed_hotkey_is_used_live(self):
        r = Rig("go", dictation_hotkey="f9")
        r.keydown("f9")
        self.assertTrue(r.d._recording)
        time.sleep(0.02)
        r.keyup("f9")
        self.assertEqual(r.typer.text, "Go ")


class Text(unittest.TestCase):
    def test_table(self):
        sp = dictation.DEFAULT_SPOKEN
        rows = [("hello world", "Hello world "),
                ("hello comma world", "Hello, world "),
                ("Hello, comma, how are you question mark", "Hello, how are you? "),
                ("this is it full stop", "This is it. "),
                ("Okay. Full stop. next one", "Okay. Next one "),
                ("wow exclamation mark", "Wow! "),
                ("note colon buy milk", "Note: buy milk "),
                ("a semicolon b", "A; b "),
                ("first new line second", "First second "),            # newline off: a space, never Enter
                ("first comma new line second", "First, second "),
                ("  ", ""), ("", "")]
        for text, want in rows:
            self.assertEqual(dictation.postprocess(text, spoken=sp), want, text)

    def test_newline_only_when_enabled(self):
        self.assertEqual(dictation.postprocess("a new line b", spoken=dictation.DEFAULT_SPOKEN, newline_ok=True), "A\nB ")
        self.assertNotIn("\n", dictation.postprocess("a\nb\r\nc\td", spoken={}, newline_ok=False))

    def test_options(self):
        self.assertEqual(dictation.postprocess("hello comma world", spoken=dictation.DEFAULT_SPOKEN, punctuate=False), "Hello comma world ")
        self.assertEqual(dictation.postprocess("hello", add_space=False), "Hello")
        self.assertEqual(dictation.postprocess("say dash now", spoken={"dash": "-"}), "Say - now ")
        self.assertEqual(dictation.postprocess("x" * 50, max_chars=10, add_space=False), "X" + "x" * 9)

    def test_spoken_validation(self):
        self.assertEqual(dictation.clean_spoken({"New  Line": "\\n"}), {"new line": "\n"})
        for bad in [{"a1": "x"}, {"ok": ""}, {"ok": "x" * 9}, [], {"": "x"}]:
            with self.assertRaises(ValueError):
                dictation.clean_spoken(bad)


class Pipeline(unittest.TestCase):
    def test_blocklist_stops_typing_and_recording(self):
        r = Rig("secret words", title="Log in - Bitwarden | chrome.exe")
        r.speak()
        self.assertEqual(r.typer.typed, [])
        self.assertEqual(r.tr.sources, [])                   # not even recorded
        self.assertIn("blocklist", r.d.status()["message"])
        self.assertIn("error", r.chimes)

    def test_window_turning_blocked_before_typing(self):
        r = Rig("secret words")
        titles = iter(["Notepad", "Enter Password | app.exe"])
        r.d.title_provider = lambda: next(titles, "Enter Password | app.exe")
        r.speak()
        self.assertEqual(r.typer.typed, [])

    def test_blocklist_is_editable(self):
        r = Rig("fine", title="My Bank", dictation_blocklist=["bank"])
        r.speak()
        self.assertEqual(r.typer.typed, [])
        r2 = Rig("fine", title="Bitwarden", dictation_blocklist=[])
        r2.speak()
        self.assertEqual(r2.typer.text, "Fine ")

    def test_blocklist_skipped_when_title_unknown(self):
        r = Rig("fine", title="")
        r.speak()
        self.assertEqual(r.typer.text, "Fine ")

    def test_never_presses_enter(self):
        r = Rig("line one new line line two", dictation_newline=False)
        r.speak()
        self.assertNotIn("\n", r.typer.text)
        self.assertFalse(hasattr(r.typer, "enter"))
        self.assertTrue(all("\n" not in c and "\r" not in c for c in r.typer.typed))

    def test_chunks(self):
        r = Rig(" ".join(["word"] * 30))
        r.speak()
        self.assertGreater(len(r.typer.typed), 1)
        self.assertTrue(all(len(c) <= dictation.CHUNK for c in r.typer.typed))

    def test_window_change_midway_stops(self):
        r = Rig(" ".join(["word"] * 30))
        calls = []

        def title():
            calls.append(1)
            return "A" if len(calls) <= 3 else "B"
        r.d.title_provider = title
        r.speak()
        self.assertLess(len(r.typer.text), 150)
        self.assertIn("changed", r.d.status()["message"])

    def test_max_chars(self):
        r = Rig("a" * 500, dictation_max_chars=50)
        r.speak()
        self.assertLessEqual(len(r.typer.text.strip()), 50)

    def test_max_seconds_stops_recording(self):
        r = Rig("long one", dictation_max_seconds=1)
        with mock.patch.object(dictation, "RATE", dictation.FRAME * 5):       # one second = 5 frames
            r.keydown("ctrl_l", "alt_l", "space")
            for _ in range(300):
                if not r.d._recording:
                    break
                time.sleep(0.01)
            self.assertFalse(r.d._recording)                                   # stopped by itself, key still down
        self.assertEqual(r.typer.text, "Long one ")
        self.assertLessEqual(r.mic.reads, 6)

    def test_short_tap_is_ignored(self):
        r = Rig("hi")
        r.d.min_s = 5
        r.speak()
        self.assertEqual((r.typer.typed, r.tr.sources), ([], []))

    def test_audio_stays_in_memory_and_is_dropped(self):
        r = Rig("hello")
        with mock.patch.object(tempfile, "mkstemp", side_effect=AssertionError("wrote a file")), \
                mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=AssertionError("wrote a file")):
            r.speak()
        self.assertEqual(r.typer.text, "Hello ")
        self.assertIsInstance(r.tr.sources[0], io.BytesIO)
        self.assertEqual(r.d._frames, [])

    def test_no_dictated_text_in_logs_or_audit(self):
        r = Rig("my secret sentence")
        with self.assertLogs("hub.dictation", level="DEBUG") as cm:
            logging.getLogger("hub.dictation").debug("probe")
            r.speak()
            r.tr.transcribe = mock.Mock(side_effect=RuntimeError("boom my secret sentence"))
            r.speak()
        self.assertNotIn("secret", " ".join(cm.output))
        self.assertNotIn("secret", repr(r.audits))
        self.assertEqual(r.audits[0], ("dictation", {"chars": len("My secret sentence ")}))
        self.assertNotIn("secret", r.d.status()["message"])

    def test_clipboard_mode_restores(self):
        r = Rig("paste me", dictation_use_clipboard=True)
        r.speak()
        self.assertEqual(r.typer.pastes, 1)
        self.assertEqual(r.typer.typed, [])
        self.assertEqual(r.d.clipboard.v, "old")

    def test_language_is_passed_to_whisper(self):
        class Model:
            def transcribe(self, src, **kw):
                self.kw = kw
                return [mock.Mock(text=" hola ")], None

        class Tr:
            _lock = threading.Lock()
            m = Model()

            def _load(self):
                return self.m
        tr = Tr()
        self.assertEqual(dictation.transcribe_pcm(tr, FRAME * 4, "es"), "hola")
        self.assertEqual(tr.m.kw["language"], "es")


class Commands(unittest.TestCase):
    def test_scratch_that(self):
        r = Rig("hello world", "scratch that")
        r.speak()
        n = len(r.typer.text)
        r.speak()
        self.assertEqual(r.typer.backspaces, [n])
        r.tr.texts = ["scratch that"]
        r.speak()                                              # nothing left to undo
        self.assertEqual(r.typer.backspaces, [n])

    def test_scratch_needs_same_window(self):
        r = Rig("hello", "scratch that")
        r.speak()
        r.title = "Chrome | chrome.exe"
        r.speak()
        self.assertEqual(r.typer.backspaces, [])
        self.assertIn("Nothing to scratch", r.d.status()["message"])

    def test_scratch_phrase_is_not_typed_and_punctuation_tolerated(self):
        r = Rig("Hi there", "Scratch that.")
        r.speak()
        r.speak()
        self.assertEqual(r.typer.text, "Hi there ")

    def test_assistant_command_off_by_default_types_text(self):
        r = Rig("send to Jarvis what time is it")
        r.speak()
        self.assertEqual(r.asked, [])
        self.assertTrue(r.typer.text.startswith("Send to Jarvis"))

    def test_assistant_command_on(self):
        r = Rig("Send to Jarvis, what time is it?", "ask jarvis summarise my day", dictation_assistant_commands=True)
        r.speak()
        r.speak()
        self.assertEqual(r.asked, [("what time is it?", "jarvis"), ("summarise my day", "jarvis")])
        self.assertEqual(r.said, ["Done.", "Done."])
        self.assertEqual(r.typer.typed, [])
        self.assertEqual(r.audits[0], ("dictation_assistant", {"chars": len("what time is it?")}))

    def test_bare_prefix_asks_nothing(self):
        r = Rig("send to jarvis", dictation_assistant_commands=True)
        r.speak()
        self.assertEqual((r.asked, r.typer.typed), ([], []))

    def test_assistant_command_respects_blocklist(self):
        r = Rig("ask jarvis x", title="KeePass", dictation_assistant_commands=True)
        r.speak()
        self.assertEqual(r.asked, [])


class Deps(unittest.TestCase):
    def _ctx(self):
        config, _ = make_config(dictation_enabled=True)
        return Ctx(config=config, model_for=None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None), config

    def test_missing_packages_status(self):
        ctx, _ = self._ctx()

        def missing():
            raise dictation.DepMissing(dictation.INSTALL_HINT)
        d = dictation.Dictation(ctx, hotkey_factory=missing, typer_factory=missing, source_factory=lambda _: FakeMic())
        d.start()
        st = d.status()
        self.assertEqual(st["state"], "missing_dependency")
        self.assertIn("scripts\\setup-all.ps1 (installs everything)", st["message"])
        self.assertFalse(st["active"])

    def test_missing_sounddevice_status(self):
        ctx, _ = self._ctx()
        d = dictation.Dictation(ctx, hotkey_factory=FakeHotkeys, typer_factory=FakeTyper)
        with mock.patch.object(dictation.importlib.util, "find_spec", return_value=None):
            d.start()
        self.assertEqual(d.status()["state"], "missing_dependency")

    def test_default_backends_raise_depmissing_without_packages(self):
        with mock.patch.dict("sys.modules", {"pynput": None, "keyboard": None}):
            with self.assertRaises(dictation.DepMissing):
                dictation.default_hotkeys()
            with self.assertRaises(dictation.DepMissing):
                dictation.default_typer()

    def test_no_microphone_is_a_status(self):
        r = Rig("x")

        def boom(dev):
            raise RuntimeError("No usable microphone")
        r.d.source_factory = boom
        r.speak()
        self.assertEqual(r.d.status()["state"], "no_mic")

    def test_doctor_line(self):
        _, config = self._ctx()
        config.dictation_enabled = False
        self.assertEqual(dictation.doctor_line(config)[0], True)
        config.dictation_enabled = True
        with mock.patch.object(dictation.importlib.util, "find_spec", return_value=None):
            ok, text = dictation.doctor_line(config)
        self.assertFalse(ok)
        self.assertIn("setup-all.ps1", text)


class Routes(unittest.TestCase):
    def setUp(self):
        self.r = Rig("x", dictation_enabled=False)
        self.d = self.r.d
        self.d.stop()
        app = FastAPI()
        app.include_router(dictation.make_router(self.r.ctx, engine=self.d))
        self.c = TestClient(app)

    def login(self):
        self.c.cookies.set("hub_session", "yes")

    def test_login_required(self):
        self.assertEqual(self.c.get("/api/dictation/status").status_code, 401)
        self.assertEqual(self.c.post("/api/dictation/toggle", json={}).status_code, 401)
        self.assertEqual(self.c.put("/api/dictation/settings", json={"mode": "toggle"}).status_code, 401)

    def test_status_defaults(self):
        self.login()
        st = self.c.get("/api/dictation/status").json()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["settings"]["hotkey"], "ctrl+alt+space")
        self.assertIn("keepass", st["settings"]["blocklist"])
        self.assertEqual(st["settings"]["spoken"]["new line"], "\\n")
        self.assertIn("focus", st["target_note"])

    def test_toggle_starts_and_stops_and_pauses(self):
        self.login()
        st = self.c.post("/api/dictation/toggle", json={"enabled": True}).json()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["state"], "ready")
        self.assertTrue(self.c.post("/api/dictation/toggle", json={"paused": True}).json()["paused"])
        self.assertEqual(self.c.get("/api/dictation/status").json()["state"], "paused")
        self.assertEqual(self.c.post("/api/dictation/toggle", json={"paused": False}).json()["state"], "ready")
        st = self.c.post("/api/dictation/toggle", json={}).json()           # flips
        self.assertFalse(st["enabled"])
        self.assertEqual(st["state"], "off")
        self.assertTrue(self.r.keys.stopped)
        self.assertEqual(self.c.post("/api/dictation/toggle", json={"enabled": "yes"}).status_code, 400)

    def test_settings_put(self):
        self.login()
        body = {"hotkey": "Ctrl+Shift+F8", "mode": "toggle", "language": "en", "blocklist": ["Bank", "bank", "vault"],
                "spoken": {"dash": "-", "new line": "\\n"}, "max_seconds": 30, "newline": True}
        st = self.c.put("/api/dictation/settings", json=body).json()
        s = st["settings"]
        self.assertEqual((s["hotkey"], s["mode"], s["language"], s["max_seconds"], s["newline"]), ("ctrl+shift+f8", "toggle", "en", 30, True))
        self.assertEqual(s["blocklist"], ["bank", "vault"])
        self.assertEqual(self.r.config.dictation_spoken, {"dash": "-", "new line": "\n"})
        for bad in [{"hotkey": "x"}, {"mode": "loud"}, {"max_seconds": 1}, {"blocklist": "a"}, {"spoken": {"a1": "x"}},
                    {"nope": 1}, {"enabled": 1}, {"language": "english!"}, {}]:
            self.assertEqual(self.c.put("/api/dictation/settings", json=bad).status_code, 400, bad)
        self.assertEqual(self.r.config.dictation_mode, "toggle")           # unchanged by the bad calls


if __name__ == "__main__":
    unittest.main()
