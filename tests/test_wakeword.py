"""Laptop-side wake word (hub/wakeword.py). Fakes only: fake audio, transcriber, assistant, speaker, clock."""
import array
import logging
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import wakeword
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth


def frame(level, n=wakeword.FRAME):
    return array.array("h", [level if i % 2 else -level for i in range(n)]).tobytes()


LOUD, QUIET = frame(3000), frame(10)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeTranscriber:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), 0

    def transcribe(self, path):
        self.calls += 1
        return self.texts.pop(0) if self.texts else ""


class Rig:
    def __init__(self, *texts, reply="Done.", **cfg):
        self.config, _ = make_config(**cfg)
        self.audits, self.notes, self.said, self.chimes, self.asked = [], [], [], [], []
        self.ctx = Ctx(config=self.config, model_for=None, transcriber=FakeTranscriber(*texts), notes=None, need_auth=need_auth,
                       audit=lambda e, **k: self.audits.append((e, k)),
                       notify=lambda *a, **k: self.notes.append((a, k)))
        self.clock = Clock()
        self.reply = reply

        def assistant(text, persona, say=None):
            self.asked.append((text, persona))
            return self.reply
        self.lis = wakeword.WakeListener(self.ctx, assistant=assistant, speaker=self.said.append,
                                         chime=self.chimes.append, clock=self.clock)

    def utter(self):
        self.lis.handle(LOUD * 12)


class Gate(unittest.TestCase):
    def test_silence_never_triggers(self):
        seg = wakeword.Segmenter()
        self.assertTrue(all(seg.feed(QUIET) is None for _ in range(200)))

    def test_speech_then_silence_ends_the_utterance(self):
        seg = wakeword.Segmenter()
        out = [seg.feed(QUIET) for _ in range(10)] + [seg.feed(LOUD) for _ in range(20)]
        self.assertTrue(all(o is None for o in out))
        got = None
        for i in range(40):                                   # 0.8 s is 25 frames of 32 ms
            got = seg.feed(QUIET)
            if got:
                self.assertGreaterEqual(i, 23)
                break
        self.assertIsNotNone(got)
        self.assertGreater(len(got), 20 * len(LOUD))

    def test_click_is_not_speech(self):
        seg = wakeword.Segmenter()
        out = [seg.feed(LOUD)] + [seg.feed(QUIET) for _ in range(40)]
        self.assertTrue(all(o is None for o in out))

    def test_twelve_second_cap(self):
        seg = wakeword.Segmenter()
        got = [seg.feed(LOUD) for _ in range(int(12 / 0.032) + 5)]
        self.assertTrue(any(g for g in got))

    def test_adapts_to_a_noisy_room(self):
        seg = wakeword.Segmenter()
        for _ in range(200):
            seg.feed(frame(300))
        self.assertGreater(seg.threshold, 350)


class Matching(unittest.TestCase):
    def test_table(self):
        wakes = [("hey jarvis", "jarvis", ""), ("Hey Jarvis.", "jarvis", ""), ("hey jarvis what's the time", "jarvis", "what's the time"),
                 ("Hey Jarvis, remember this", "jarvis", "remember this"), ("ok jarvis run the tests", "jarvis", "run the tests"),
                 ("okay jarvis", "jarvis", ""), ("yo jarvis open notes", "jarvis", "open notes"),
                 ("hey jarvas remember this", "jarvis", "remember this"), ("hey jarves what's up", "jarvis", "what's up"),
                 ("hi friday what's my day", "friday", "what's my day"), ("  HEY FRIDAY  ", "friday", "")]
        for text, persona, rest in wakes:
            self.assertEqual(wakeword.match_wake(text), (persona, rest), text)
        for text in ["Jarvis is a nice name", "jarvis", "friday evening plans", "hey friday evening plans", "hey service",
                     "see you friday", "I said hey to jarvis", "hey jarvis is", "hey there jarvis", "thank you jarvis",
                     "hey jarvisx", "hey fridays", "", "hey", "hey jarvis is a nice name", "jarvis hey friday"]:
            self.assertIsNone(wakeword.match_wake(text), text)

    def test_custom_name(self):
        self.assertEqual(wakeword.match_wake("hey tony run it", "Tony"), ("jarvis", "run it"))
        self.assertIsNone(wakeword.match_wake("hey tony run it"))


class Pipeline(unittest.TestCase):
    def test_rest_of_sentence_is_forwarded_and_spoken(self):
        r = Rig("Hey Jarvis, what's the time", reply="It is noon.")
        r.utter()
        self.assertEqual(r.asked, [("what's the time", "jarvis")])
        self.assertEqual(r.chimes, ["jarvis"])
        self.assertEqual(r.said, ["It is noon."])
        self.assertEqual(r.audits, [("wakeword", {"persona": "jarvis", "chars": 15})])   # counts only, no text

    def test_bare_wake_then_command(self):
        r = Rig("hey friday", "plan my day")
        r.utter()
        self.assertEqual(r.asked, [])
        self.assertEqual(r.chimes, ["friday"])
        r.clock.t += 3
        r.utter()
        self.assertEqual(r.asked, [("plan my day", "friday")])

    def test_command_window_expires(self):
        r = Rig("hey jarvis", "what a nice day")
        r.utter()
        r.clock.t += wakeword.COMMAND_WAIT_S + 1
        r.utter()
        self.assertEqual(r.asked, [])

    def test_non_wake_speech_goes_nowhere_and_leaks_nothing(self):
        secret = "my password is hunter2 and jarvis is a nice name"
        r = Rig(secret)
        with self.assertLogs("hub.wakeword", level="DEBUG") as cm:
            logging.getLogger("hub.wakeword").debug("probe")        # assertLogs needs one record
            r.utter()
        self.assertEqual(r.asked, [])
        self.assertEqual(r.said, [])
        self.assertEqual(r.chimes, [])
        self.assertEqual(r.audits, [])
        self.assertEqual(r.notes, [])
        self.assertNotIn("hunter2", "".join(cm.output))
        self.assertIsNone(r.lis.awaiting)
        self.assertNotIn(secret, repr(vars(r.lis)))

    def test_recording_file_is_deleted(self):
        import os
        r = Rig("hey jarvis hi")
        seen = []
        orig = r.ctx.transcriber.transcribe
        r.ctx.transcriber.transcribe = lambda p: (seen.append(p), orig(p))[1]
        r.utter()
        self.assertTrue(seen)
        self.assertFalse(os.path.exists(seen[0]))

    def test_echo_guard(self):
        r = Rig("hey jarvis tell me a story", reply="Once upon a time.")
        during = []
        r.lis.speaker = lambda t: during.append((r.lis.speaking, r.lis.feed(LOUD), r.lis.state))
        r.utter()
        self.assertEqual(during, [(True, None, "speaking")])
        self.assertFalse(r.lis.speaking)
        # the tail after speaking is ignored too, then listening resumes
        self.assertTrue(all(r.lis.feed(LOUD) is None for _ in range(50)))
        r.clock.t += wakeword.ECHO_TAIL_S + 0.1
        out = [r.lis.feed(LOUD) for _ in range(20)] + [r.lis.feed(QUIET) for _ in range(40)]
        self.assertTrue(any(out))

    def test_a_failing_assistant_does_not_kill_the_listener(self):
        r = Rig("hey jarvis do it")
        r.lis.assistant = lambda *a, **k: 1 / 0
        r.utter()
        self.assertEqual(r.lis.state, "listening")


class Approvals(unittest.TestCase):
    def test_request_body_never_auto_approves(self):
        b = wakeword.message_body("delete everything", "jarvis")
        self.assertIs(b["auto_approve"], False)
        self.assertIs(b["voice"], True)
        self.assertEqual(b["persona"], "jarvis")

    def test_approval_event_is_a_push_not_an_action(self):
        notes, said = [], []
        events = [{"type": "text", "t": "I will write it. "}, {"type": "approval_needed", "id": "1", "name": "write_file", "input": {}},
                  {"type": "text", "t": "Waiting."}, {"type": "done"}]
        reply = wakeword.consume(events, lambda *a, **k: notes.append((a, k)), said.append)
        self.assertEqual(reply, "I will write it. Waiting.")
        self.assertEqual(len(notes), 1)
        self.assertIn("write_file", notes[0][1]["body"])
        self.assertEqual(len(said), 1)

    def test_error_event_is_spoken_plainly(self):
        self.assertEqual(wakeword.consume([{"type": "error", "message": "No AI is available."}], print, print), "No AI is available.")

    def test_speaker_text_is_never_in_argv(self):
        argv = wakeword.speak_argv()
        if argv:
            self.assertNotIn("password", " ".join(argv))


class NoMic(unittest.TestCase):
    def test_missing_dependency_is_a_status_not_a_crash(self):
        r = Rig()

        def boom(_device):
            raise wakeword.NoMicrophone("Needs the optional sounddevice package. Run: pip install sounddevice")
        r.lis.source_factory = boom
        r.lis.start()
        r.lis._thread.join(3)
        st = r.lis.status()
        self.assertEqual(st["state"], "missing_dependency")
        self.assertIn("sounddevice", st["message"])
        self.assertFalse(st["listening"])

    def test_no_device(self):
        r = Rig()
        r.lis.source_factory = lambda d: (_ for _ in ()).throw(wakeword.NoMicrophone("No usable microphone on this laptop (x)."))
        r.lis.start()
        r.lis._thread.join(3)
        self.assertEqual(r.lis.status()["state"], "no_mic")

    def test_real_source_without_package_is_graceful(self):
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"sounddevice": None}):   # as if the package were not installed
            with self.assertRaises(wakeword.NoMicrophone):
                wakeword.SoundDeviceSource("")

    def test_thread_reads_and_handles_frames(self):
        r = Rig("hey jarvis ping", reply="pong")

        class Src:
            def __init__(self):
                self.frames = [QUIET] * 5 + [LOUD] * 20 + [QUIET] * 40
                self.closed = False

            def read(self, n):
                if not self.frames:
                    r.lis._stop.set()
                    return b""
                return self.frames.pop(0)

            def close(self):
                self.closed = True
        src = Src()
        r.lis.source_factory = lambda d: src
        r.lis.start()
        r.lis._thread.join(5)
        self.assertEqual(r.asked, [("ping", "jarvis")])
        self.assertEqual(r.said, ["pong"])
        self.assertTrue(src.closed)


class Routes(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.rig.lis.source_factory = lambda d: (_ for _ in ()).throw(wakeword.NoMicrophone("No usable microphone on this laptop (x)."))
        app = FastAPI()
        app.include_router(wakeword.make_router(self.rig.ctx, listener=self.rig.lis))
        self.c = TestClient(app)
        self.auth = {"cookie": "hub_session=yes"}

    def test_login_required(self):
        self.assertEqual(self.c.get("/api/wakeword/status").status_code, 401)
        self.assertEqual(self.c.post("/api/wakeword/toggle", json={"enabled": True}).status_code, 401)
        self.assertIs(self.rig.config.wake_laptop_enabled, False)

    def test_off_by_default_and_toggle(self):
        st = self.c.get("/api/wakeword/status", headers=self.auth).json()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["state"], "off")
        on = self.c.post("/api/wakeword/toggle", json={"enabled": True}, headers=self.auth)
        self.assertEqual(on.status_code, 200)
        self.assertTrue(self.rig.config.wake_laptop_enabled)
        self.rig.lis._thread and self.rig.lis._thread.join(3)
        self.assertEqual(self.c.get("/api/wakeword/status", headers=self.auth).json()["state"], "no_mic")
        off = self.c.post("/api/wakeword/toggle", json={"enabled": False}, headers=self.auth).json()
        self.assertFalse(off["enabled"])
        self.assertEqual(off["state"], "off")
        self.assertEqual(self.c.post("/api/wakeword/toggle", json={"enabled": "yes"}, headers=self.auth).status_code, 400)
        self.assertEqual(self.c.post("/api/wakeword/toggle", json={}, headers=self.auth).json()["enabled"], True)
        self.c.post("/api/wakeword/toggle", json={"enabled": False}, headers=self.auth)


class Wiring(unittest.TestCase):
    def test_registered_and_config_defaults(self):
        from hub.server import FEATURE_MODULES
        self.assertIn("wakeword", FEATURE_MODULES)
        c, _ = make_config()
        self.assertIs(c.wake_laptop_enabled, False)
        self.assertEqual(c.wake_laptop_device, "")

    def test_optional_dependency_listed(self):
        from pathlib import Path
        txt = (Path(__file__).resolve().parents[1] / "requirements-voice.txt").read_text(encoding="utf-8")
        self.assertIn("sounddevice", txt)
        self.assertIn("av==16.1.0", txt)


if __name__ == "__main__":
    unittest.main()
