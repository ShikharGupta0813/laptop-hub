"""Local text to speech (hub/tts.py) and its browser queue. Fakes only: fake runner (no real engine), fake player, fake clock."""
import json
import os
import shutil
import struct
import subprocess
import threading
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import assistants as A
from hub import tts, wakeword
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parent.parent / "hub" / "static"


def wav(tag: str = "") -> bytes:
    return b"RIFF" + struct.pack("<I", 100) + b"WAVE" + b"fmt " + b"\x00" * 30 + tag.encode()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRunner:
    """Records every call. Writes a WAV that carries the spoken text to the {out} / REDLINE_TTS_OUT path, or returns it on stdout."""

    def __init__(self, fail_on=None, stdout_mode=False, rc=0):
        self.calls, self.fail_on, self.stdout_mode, self.rc = [], fail_on, stdout_mode, rc

    def __call__(self, argv, *, stdin_text, env=None, timeout=60):
        self.calls.append({"argv": list(argv), "stdin": stdin_text, "env": env or {}})
        if self.fail_on and self.fail_on in stdin_text:
            return 1, b""
        if "Get" in " ".join(argv) and "GetInstalledVoices" in " ".join(argv):
            return 0, b"Microsoft David Desktop|en-US\nMicrosoft Zira Desktop|en-US\n"
        data = wav(stdin_text)
        if self.stdout_mode:
            return self.rc, data
        out = (env or {}).get("REDLINE_TTS_OUT") or next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("--output_file", "-f")), None) \
            or next((a for a in argv if a.endswith(".wav")), None)
        if out:
            Path(out).write_bytes(data)
        return self.rc, b""


class FakePlayer:
    def __init__(self, on_play=None):
        self.played, self.stops, self.on_play = [], 0, on_play

    def play(self, path):
        self.played.append(Path(path).read_bytes())
        if self.on_play:
            self.on_play(len(self.played))

    def stop(self):
        self.stops += 1


def make_tts(*, platform="win32", **kw):
    config, _root = make_config()
    runner = kw.pop("runner", None) or FakeRunner()
    player = kw.pop("player", None) or FakePlayer()
    clock = kw.pop("clock", None) or Clock()
    svc = tts.TTS(config, runner=runner, player=player, clock=clock, platform=platform,
                  which=kw.pop("which", lambda n: None), **kw)
    return svc, config, runner, player, clock


def seed_piper(svc, voice="en_US-lessac-medium"):
    folder = svc.data / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{voice}.onnx").write_bytes(b"x" * 10)
    return folder


class Engines(unittest.TestCase):
    def test_default_is_browser_and_makes_no_audio(self):
        svc, config, runner, _p, _c = make_tts()
        self.assertTrue(config.tts_enabled)
        self.assertEqual(config.tts_engine, "browser")
        r = svc.synthesize("Hello there, friend.")
        self.assertIsNone(r.wav)
        self.assertEqual(r.reason, "browser")
        self.assertEqual(runner.calls, [])

    def test_sapi_text_on_stdin_never_in_argv(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="sapi")
        runner = svc.runner
        r = svc.synthesize("Secret sentence about the weather.")
        self.assertEqual(r.engine, "sapi")
        self.assertTrue(tts.is_wav(r.wav))
        call = [c for c in runner.calls if "Secret" in c["stdin"]][0]
        self.assertNotIn("Secret", " ".join(call["argv"]))
        self.assertEqual(call["argv"][0], "powershell")
        self.assertIn("REDLINE_TTS_OUT", call["env"])

    def test_sapi_is_unavailable_off_windows(self):
        svc, *_ = make_tts(platform="linux")
        svc.save_settings(engine="sapi")
        r = svc.synthesize("Hello again, how are you.")
        self.assertIsNone(r.wav)
        self.assertIn("Windows", r.reason)
        self.assertFalse([e for e in svc.engine_status() if e["id"] == "sapi"][0]["available"])

    def test_sapi_voice_catalogue(self):
        svc, *_ = make_tts()
        v = svc.voices("sapi")
        self.assertEqual([x["id"] for x in v], ["Microsoft David Desktop", "Microsoft Zira Desktop"])
        self.assertEqual(v[0]["lang"], "en-US")

    def test_piper_runs_model_with_text_on_stdin(self):
        svc, *_ = make_tts(which=lambda n: "/usr/bin/piper" if n == "piper" else None)
        seed_piper(svc)
        svc.save_settings(engine="piper")
        r = svc.synthesize("Piper, say this please.", rate=2.0)
        self.assertTrue(tts.is_wav(r.wav))
        call = svc.runner.calls[-1]
        self.assertEqual(call["argv"][0], "/usr/bin/piper")
        self.assertIn("--model", call["argv"])
        self.assertTrue(call["argv"][call["argv"].index("--model") + 1].endswith("en_US-lessac-medium.onnx"))
        self.assertEqual(call["argv"][call["argv"].index("--length_scale") + 1], "0.50")
        self.assertEqual(call["stdin"], "Piper, say this please.")
        self.assertNotIn("Piper, say", " ".join(call["argv"]))

    def test_piper_without_install_or_model_falls_back(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="piper")
        r = svc.synthesize("Nothing installed here, sorry.")
        self.assertIsNone(r.wav)
        self.assertIn("Piper", r.reason)
        self.assertEqual(svc.last_error, r.reason)

    def test_engine_failure_returns_reason_not_text(self):
        svc, *_ = make_tts(runner=FakeRunner(fail_on="boom"))
        svc.save_settings(engine="sapi")
        r = svc.synthesize("this will boom now")
        self.assertIsNone(r.wav)
        self.assertNotIn("boom", r.reason)

    def test_disabled_makes_nothing(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="sapi", enabled=False)
        self.assertEqual(svc.synthesize("Quiet please, thank you.").reason, "disabled")


class CommandEngine(unittest.TestCase):
    GOOD = {"id": "kokoro", "name": "Kokoro", "argv": ["/usr/bin/say-onnx", "--voice", "af"], "output": "stdout"}

    def bad(self, **change):
        with self.assertRaises(tts.TTSError):
            tts.clean_command({**self.GOOD, **change})

    def test_valid_command_is_normalised(self):
        c = tts.clean_command(self.GOOD)
        self.assertEqual(c["argv"], self.GOOD["argv"])
        self.assertEqual(c["output"], "stdout")

    def test_no_shell_as_program(self):
        for prog in ("bash", "/bin/sh", "powershell.exe", "cmd", "C:\\Windows\\System32\\cmd.exe", "pwsh", "wsl"):
            self.bad(argv=[prog, "x"])

    def test_no_eval_flags(self):
        self.bad(argv=["tool", "-c", "echo hi"])
        self.bad(argv=["tool", "-Command", "x"])
        self.bad(argv=["tool", "/c", "x"])

    def test_no_text_placeholder_only_out(self):
        self.bad(argv=["tool", "{text}"])
        self.bad(argv=["tool", "--say={input}"])
        self.bad(argv=["tool", "{out}"])                      # stdout mode cannot use {out}
        self.bad(output="file")                               # file mode needs {out}
        ok = tts.clean_command({**self.GOOD, "argv": ["tool", "-o", "{out}"], "output": "file"})
        self.assertEqual(ok["output"], "file")

    def test_argv_shape(self):
        self.bad(argv="tool --x")
        self.bad(argv=[])
        self.bad(argv=["tool", "line\nbreak"])
        self.bad(argv=["tool"] * 30)
        self.bad(id="Bad Id")
        self.bad(output="pipe")

    def test_stdout_command_runs_with_stdin_and_env(self):
        svc, *_ = make_tts(runner=FakeRunner(stdout_mode=True), which=lambda n: n)
        svc.save_settings(commands=[self.GOOD], engine="command")
        r = svc.synthesize("Say it with kokoro.", voice="kokoro", rate=1.5, pitch=0.8)
        self.assertTrue(tts.is_wav(r.wav))
        call = svc.runner.calls[-1]
        self.assertEqual(call["stdin"], "Say it with kokoro.")
        self.assertNotIn("kokoro.", " ".join(call["argv"]))
        self.assertEqual(call["env"]["REDLINE_TTS_RATE"], "1.50")
        self.assertEqual(call["env"]["REDLINE_TTS_PITCH"], "0.80")

    def test_file_command_gets_out_path_in_argv_only(self):
        svc, *_ = make_tts(which=lambda n: n)
        svc.save_settings(commands=[{"id": "tofile", "name": "To file", "argv": ["tool", "--out", "{out}"], "output": "file"}], engine="command")
        r = svc.synthesize("Write me a file, thanks.", voice="tofile")
        self.assertTrue(tts.is_wav(r.wav))
        argv = svc.runner.calls[-1]["argv"]
        self.assertTrue(argv[2].endswith(".wav"))
        self.assertNotIn("{out}", argv)

    def test_unknown_command_or_missing_program(self):
        svc, *_ = make_tts(runner=FakeRunner(stdout_mode=True), which=lambda n: None)
        svc.save_settings(commands=[self.GOOD], engine="command")
        self.assertIsNone(svc.synthesize("Hello there you.", voice="nope").wav)
        self.assertIn("not found", svc.synthesize("Hello there you.", voice="kokoro").reason)

    def test_non_wav_output_is_rejected(self):
        svc, *_ = make_tts(runner=lambda argv, **k: (0, b"not audio at all, just words" * 5), which=lambda n: n)
        svc.save_settings(commands=[self.GOOD], engine="command")
        self.assertIn("WAV", svc.synthesize("Hello there you.", voice="kokoro").reason)

    def test_the_default_runner_uses_no_shell(self):
        import inspect
        src = inspect.getsource(tts.default_runner)
        self.assertIn("shell=False", src)
        self.assertNotIn("shell=True", inspect.getsource(tts))


class Cache(unittest.TestCase):
    def test_hit_skips_the_engine(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="sapi")
        first = svc.synthesize("Done.")
        n = len(svc.runner.calls)
        second = svc.synthesize("Done.")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(len(svc.runner.calls), n)
        self.assertEqual(first.wav, second.wav)

    def test_key_depends_on_engine_voice_rate_and_text(self):
        k = tts.PhraseCache.key
        self.assertNotEqual(k("sapi", "a", 1, 1, "x"), k("sapi", "b", 1, 1, "x"))
        self.assertNotEqual(k("sapi", "a", 1, 1, "x"), k("piper", "a", 1, 1, "x"))
        self.assertNotEqual(k("sapi", "a", 1, 1, "x"), k("sapi", "a", 1.2, 1, "x"))
        self.assertNotEqual(k("sapi", "a", 1, 1, "x"), k("sapi", "a", 1, 1, "y"))

    def test_evicts_oldest_beyond_the_limit(self):
        clock = Clock()
        cache = tts.PhraseCache(Path(make_config()[1]) / "c", max_files=3, clock=clock)
        for i in range(5):
            clock.t += 10
            cache.put(f"k{i}", wav(str(i)))
        self.assertEqual(sorted(p.stem for p in cache.dir.glob("*.wav")), ["k2", "k3", "k4"])

    def test_a_hit_refreshes_so_it_survives(self):
        clock = Clock()
        cache = tts.PhraseCache(Path(make_config()[1]) / "c", max_files=2, clock=clock)
        cache.put("a", wav("a"))
        clock.t += 10
        cache.put("b", wav("b"))
        clock.t += 10
        self.assertIsNotNone(cache.get("a"))
        clock.t += 10
        cache.put("c", wav("c"))
        self.assertEqual(sorted(p.stem for p in cache.dir.glob("*.wav")), ["a", "c"])

    def test_old_entries_expire(self):
        clock = Clock()
        cache = tts.PhraseCache(Path(make_config()[1]) / "c", max_files=50, max_age_s=100, clock=clock)
        cache.put("old", wav())
        clock.t += 500
        cache.put("new", wav())
        self.assertEqual([p.stem for p in cache.dir.glob("*.wav")], ["new"])

    def test_filenames_reveal_no_text(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="sapi")
        svc.synthesize("Highly personal sentence.")
        names = " ".join(p.name for p in (svc.data / "cache").glob("*"))
        self.assertNotIn("personal", names.lower())


class Mapping(unittest.TestCase):
    def test_per_assistant_voice_mapping(self):
        svc, config, *_ = make_tts(which=lambda n: n)
        svc.save_settings(engine="sapi", commands=[CommandEngine.GOOD])
        lst = A.load(config)
        for a in lst:
            if a["id"] == "friday":
                a["voice"] = {"rate": 1.2, "pitch": 1.0, "voice": "Microsoft Zira Desktop", "engine": "sapi"}
            if a["id"] == "jarvis":
                a["voice"] = {"rate": 0.9, "pitch": 1.0, "voice": "Microsoft David Desktop", "engine": ""}
        A.save(config, lst)
        j, f = svc.profile_for("jarvis"), svc.profile_for("friday")
        self.assertEqual((j["engine"], j["voice"], j["rate"]), ("sapi", "Microsoft David Desktop", 0.9))     # "" -> the default engine
        self.assertEqual((f["engine"], f["voice"], f["rate"]), ("sapi", "Microsoft Zira Desktop", 1.2))
        svc.synthesize("Hello from Jarvis.", "jarvis")
        svc.synthesize("Hello from Friday.", "friday")
        envs = {c["stdin"]: c["env"]["REDLINE_TTS_VOICE"] for c in svc.runner.calls if c["stdin"]}
        self.assertEqual(envs["Hello from Jarvis."], "Microsoft David Desktop")
        self.assertEqual(envs["Hello from Friday."], "Microsoft Zira Desktop")

    def test_assistants_voice_engine_is_validated_and_additive(self):
        d = A.defaults()
        self.assertEqual(d[0]["voice"]["engine"], "")
        e = A.clean_entry({**d[0], "voice": {"rate": 1, "pitch": 1, "voice": "x", "engine": "piper"}}, known_groups=[])
        self.assertEqual(e["voice"]["engine"], "piper")
        with self.assertRaises(A.AssistantError):
            A.clean_entry({**d[0], "voice": {"engine": "rm -rf"}}, known_groups=[])
        old = A.clean_entry({**d[0], "voice": {"rate": 1, "pitch": 1, "voice": ""}}, known_groups=[])      # a saved list from before this field
        self.assertEqual(old["voice"]["engine"], "")

    def test_profiles_say_which_assistants_use_the_hub(self):
        svc, config, *_ = make_tts()
        self.assertFalse(any(p["hub"] for p in svc.profiles().values()))
        svc.save_settings(engine="sapi")
        self.assertTrue(svc.profiles()["jarvis"]["hub"])


class Routes(unittest.TestCase):
    def setUp(self):
        self.svc, self.config, self.runner, _p, _c = make_tts(which=lambda n: n)
        self.audits = []
        ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda e, **k: self.audits.append((e, k)))
        app = FastAPI()
        app.include_router(tts.make_router(ctx, service=self.svc))
        self.ctx = ctx
        self.anon = TestClient(app)
        self.c = TestClient(app, cookies={"hub_session": "yes"})

    def test_login_is_required_everywhere(self):
        for method, path, kw in (("get", "/api/tts/status", {}), ("post", "/api/tts", {"json": {"text": "hello there"}}),
                                 ("post", "/api/tts/stream", {"content": b"{}\n"}), ("get", "/api/tts/voices", {}),
                                 ("put", "/api/tts/settings", {"json": {"enabled": True}}), ("get", "/api/tts/piper", {}),
                                 ("post", "/api/tts/piper/download", {"json": {}}), ("post", "/api/tts/stop", {}),
                                 ("put", "/api/tts/commands", {"json": {}})):
            self.assertEqual(getattr(self.anon, method)(path, **kw).status_code, 401, path)

    def test_browser_engine_is_204(self):
        r = self.c.post("/api/tts", json={"text": "Hello there, friend."})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["x-tts-engine"], "browser")

    def test_wav_for_a_hub_engine_and_cache_header(self):
        self.svc.save_settings(engine="sapi")
        r = self.c.post("/api/tts", json={"text": "Hello there, friend.", "assistant": "jarvis"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/wav")
        self.assertEqual(r.headers["x-tts-cached"], "0")
        self.assertTrue(tts.is_wav(r.content))
        self.assertEqual(self.c.post("/api/tts", json={"text": "Hello there, friend.", "assistant": "jarvis"}).headers["x-tts-cached"], "1")

    def test_text_is_validated_and_capped(self):
        self.assertEqual(self.c.post("/api/tts", json={}).status_code, 400)
        self.assertEqual(self.c.post("/api/tts", json={"text": "  "}).status_code, 400)
        self.assertEqual(self.c.post("/api/tts", json={"text": "a" * (tts.MAX_TEXT + 1)}).status_code, 413)
        self.assertEqual(self.c.post("/api/tts", json={"text": "hello", "engine": "festival"}).status_code, 400)

    def test_engine_failure_is_502_with_a_plain_sentence(self):
        self.svc.save_settings(engine="piper")
        r = self.c.post("/api/tts", json={"text": "No piper here, sorry."})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("No piper here", r.text)

    def test_overrides_are_for_test_voice(self):
        r = self.c.post("/api/tts", json={"text": "Test this one.", "engine": "sapi", "voice": "Microsoft Zira Desktop", "rate": 1.3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.runner.calls[-1]["env"]["REDLINE_TTS_VOICE"], "Microsoft Zira Desktop")

    def test_text_is_never_logged_or_audited(self):
        self.svc.save_settings(engine="sapi")
        with self.assertLogs("hub.tts", level="DEBUG") as cm:
            import logging
            logging.getLogger("hub.tts").debug("probe")
            self.c.post("/api/tts", json={"text": "My private diary line."})
        self.assertNotIn("diary", " ".join(cm.output))
        self.assertNotIn("diary", json.dumps(self.audits))

    def test_stream_emits_one_frame_per_sentence_in_order(self):
        self.svc.save_settings(engine="sapi")
        lines = b"".join(json.dumps({"text": t}).encode() + b"\n" for t in ("First sentence here.", "Second sentence here.", "Third one right here."))
        r = self.c.post("/api/tts/stream", content=lines)
        self.assertEqual(r.status_code, 200)
        data, frames = r.content, []
        while data:
            kind, n = struct.unpack(">BI", data[:5])
            frames.append((kind, data[5:5 + n]))
            data = data[5 + n:]
        self.assertEqual([k for k, _ in frames], [0, 0, 0])
        self.assertEqual([p.endswith(t.encode()) for (_, p), t in zip(frames, ("First sentence here.", "Second sentence here.", "Third one right here."))], [True] * 3)

    def test_stream_browser_and_bad_lines(self):
        r = self.c.post("/api/tts/stream", content=b'{"text": "Hello friend, how are you."}\nnot json\n')
        kinds, data = [], r.content
        while data:
            kind, n = struct.unpack(">BI", data[:5])
            kinds.append(kind)
            data = data[5 + n:]
        self.assertEqual(kinds, [1, 3])

    def test_status_voices_and_settings(self):
        s = self.c.get("/api/tts/status").json()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["engine"], "browser")
        self.assertEqual({e["id"] for e in s["engines"]}, set(tts.ENGINES))
        self.assertIn("jarvis", s["profiles"])
        self.assertEqual(self.c.put("/api/tts/settings", json={"engine": "sapi", "headphones": True}).json()["engine"], "sapi")
        self.assertTrue(self.c.get("/api/tts/status").json()["headphones"])
        self.assertEqual(self.c.put("/api/tts/settings", json={"engine": "nope"}).status_code, 400)
        self.assertEqual(self.c.put("/api/tts/settings", json={}).status_code, 400)
        self.assertEqual(len(self.c.get("/api/tts/voices?engine=sapi").json()["voices"]), 2)
        self.assertEqual(self.c.get("/api/tts/voices?engine=bogus").status_code, 400)

    def test_commands_need_confirm_and_validation(self):
        body = {"commands": [CommandEngine.GOOD]}
        self.assertEqual(self.c.put("/api/tts/commands", json=body).status_code, 400)
        self.assertEqual(self.c.put("/api/tts/commands", json={**body, "confirm": True}).status_code, 200)
        self.assertEqual(self.c.get("/api/tts/commands").json()["commands"][0]["id"], "kokoro")
        bad = {"commands": [{"id": "x1", "argv": ["bash", "-c", "x"]}], "confirm": True}
        self.assertEqual(self.c.put("/api/tts/commands", json=bad).status_code, 400)
        self.assertTrue(any(e == "tts_commands" for e, _ in self.audits))

    def test_stop_route_stops_the_player(self):
        self.assertEqual(self.c.post("/api/tts/stop").status_code, 200)
        self.assertGreaterEqual(self.svc.player.stops, 1)

    def test_panic_silenced_is_423_not_a_phone_fallback(self):
        self.svc.save_settings(engine="sapi")
        self.svc.silence()
        r = self.c.post("/api/tts", json={"text": "Should not be heard."})
        self.assertEqual(r.status_code, 423)
        self.assertTrue(self.c.get("/api/tts/status").json()["silenced"])
        self.svc.resume()
        self.assertEqual(self.c.post("/api/tts", json={"text": "Should be heard now."}).status_code, 200)


class PiperDownload(unittest.TestCase):
    def setUp(self):
        self.fetched = []

        def fetch(url, dest, max_bytes, progress):
            self.fetched.append(url)
            if url.endswith(".json"):
                Path(dest).write_text(json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8")
            else:
                Path(dest).write_bytes(b"\x00" * (2 * 1024 * 1024))
                progress(2 * 1024 * 1024, 2 * 1024 * 1024)
        self.svc, self.config, *_ = make_tts(fetch=fetch)
        ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(tts.make_router(ctx, service=self.svc))
        self.c = TestClient(app, cookies={"hub_session": "yes"})

    def test_url_pattern(self):
        a, b = tts.piper_urls("en_US-lessac-medium")
        self.assertEqual(a, "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx")
        self.assertEqual(b, a + ".json")
        with self.assertRaises(tts.TTSError):
            tts.piper_urls("../../etc/passwd")
        with self.assertRaises(tts.TTSError):
            tts.piper_urls("en_US-lessac-huge")

    def test_nothing_downloads_without_confirm(self):
        self.assertEqual(self.c.post("/api/tts/piper/download", json={"voice": "en_US-lessac-medium"}).status_code, 400)
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.c.get("/api/tts/piper").json()["models"], [])

    def test_owner_started_download_is_checked_and_stored(self):
        self.assertEqual(self.c.post("/api/tts/piper/download", json={"voice": "en_US-lessac-medium", "confirm": True}).status_code, 200)
        self.svc._job_thread.join(10)
        state = self.c.get("/api/tts/piper").json()
        self.assertEqual(state["job"]["state"], "done", state["job"])
        self.assertEqual([m["id"] for m in state["models"]], ["en_US-lessac-medium"])
        self.assertTrue((self.svc.data / "piper" / "en_US-lessac-medium.onnx.sha256").exists())
        self.assertEqual(len(self.fetched), 2)
        self.assertFalse(list((self.svc.data / "piper").glob("*.part")))
        self.assertEqual(self.c.delete("/api/tts/piper/en_US-lessac-medium").json()["models"], [])

    def test_wrong_checksum_keeps_nothing(self):
        self.svc.start_download("en_US-lessac-medium", "0" * 64, wait=True)
        self.assertEqual(self.svc.job["state"], "error")
        self.assertIn("checksum", self.svc.job["message"])
        self.assertEqual(self.svc._piper_models(), [])

    def test_tiny_file_is_refused(self):
        def tiny(url, dest, max_bytes, progress):
            Path(dest).write_text(json.dumps({"audio": {}}) if url.endswith(".json") else "tiny", encoding="utf-8")
        svc, *_ = make_tts(fetch=tiny)
        svc.start_download("en_US-amy-low", wait=True)
        self.assertEqual(svc.job["state"], "error")
        self.assertIn("too small", svc.job["message"])
        self.assertEqual(svc._piper_models(), [])

    def test_bad_ids_and_single_job(self):
        self.assertEqual(self.c.post("/api/tts/piper/download", json={"voice": "x", "confirm": True}).status_code, 400)
        self.assertEqual(self.c.delete("/api/tts/piper/..%2Fx").status_code in (400, 404), True)

    def test_default_fetch_only_talks_to_huggingface(self):
        with self.assertRaises(tts.TTSError):
            tts.default_fetch("https://evil.example/x.onnx", Path("/tmp/x"), 10, lambda *a: None)


class TextRules(unittest.TestCase):
    def test_code_is_not_read(self):
        out = tts.speakable_text("Here it is.\n```py\nprint(1)\n```\nAll set.")
        self.assertIn(tts.DETAILS_LINE, out)
        self.assertNotIn("print", out)

    def test_urls_become_a_link(self):
        self.assertEqual(tts.speakable_text("See https://example.com/a?b=1 now."), "See a link now.")

    def test_long_list_is_summarised_once(self):
        text = "Steps:\n" + "\n".join(f"- item number {i}" for i in range(1, 8))
        out = tts.speakable_text(text)
        self.assertIn("item number 3", out)
        self.assertNotIn("item number 4", out)
        self.assertEqual(out.count(tts.DETAILS_LINE), 1)

    def test_sentence_split_keeps_order_and_joins_fragments(self):
        s = tts.split_sentences("Okay. This is the first full sentence. And here is the second one! Third?")
        self.assertEqual(s[0], "Okay. This is the first full sentence.")
        self.assertEqual(" ".join(s), "Okay. This is the first full sentence. And here is the second one! Third?")
        long = tts.split_sentences("word " * 200)
        self.assertTrue(all(len(x) <= tts.MAX_SENTENCE for x in long))


class LaptopSpeaker(unittest.TestCase):
    def test_sentences_play_in_order_and_first_starts_before_the_rest_is_made(self):
        gate = threading.Event()
        order = []

        class SlowRunner(FakeRunner):
            def __call__(self, argv, *, stdin_text, env=None, timeout=60):
                order.append(("made", stdin_text))
                if "third" in stdin_text:
                    self.third_waited = not gate.is_set()
                    gate.wait(5)
                return super().__call__(argv, stdin_text=stdin_text, env=env, timeout=timeout)
        runner = SlowRunner()
        runner.third_waited = False
        player = FakePlayer(on_play=lambda n: (order.append(("played", n)), gate.set() if n == 2 else None))
        svc, *_ = make_tts(runner=runner, player=player)
        svc.save_settings(engine="sapi")
        res = svc.speak_aloud("The first sentence is right here. The second sentence is right here. The third sentence is right here.")
        self.assertEqual(res, "spoken")
        sentences = ("The first sentence is right here.", "The second sentence is right here.", "The third sentence is right here.")
        self.assertEqual([p.decode().rsplit("WAVE", 1)[-1][-len(t):] for p, t in zip(player.played, sentences)], list(sentences))
        self.assertEqual(len(player.played), 3)
        self.assertLess(order.index(("played", 1)), order.index(("made", "The third sentence is right here.")))

    def test_browser_engine_uses_the_old_speaker(self):
        svc, *_ = make_tts()
        said = []
        res = svc.speak_aloud("Hello there, this is a test.", fallback=said.append)
        self.assertEqual(res, "fallback")
        self.assertEqual(said, ["Hello there, this is a test."])
        self.assertEqual(svc.player.played, [])

    def test_engine_failure_falls_back_with_the_unsaid_rest(self):
        svc, *_ = make_tts(runner=FakeRunner(fail_on="second"))
        svc.save_settings(engine="sapi")
        said = []
        res = svc.speak_aloud("The first sentence is right here. The second sentence is right here. The third sentence is right here.", fallback=said.append)
        self.assertEqual(res, "fallback")
        self.assertEqual(len(svc.player.played), 1)
        self.assertTrue(said and said[0].startswith("The second sentence"))

    def test_player_failure_falls_back(self):
        class Broken(FakePlayer):
            def play(self, path):
                raise tts.TTSError("no player")
        svc, *_ = make_tts(player=Broken())
        svc.save_settings(engine="sapi")
        said = []
        self.assertEqual(svc.speak_aloud("Hello there, a sentence to say.", fallback=said.append), "fallback")
        self.assertTrue(said)

    def test_code_is_replaced_by_the_details_line(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="sapi")
        svc.speak_aloud("Run this one please.\n```sh\nrm -rf x\n```\nThen you are done, my friend.")
        spoken = b" ".join(svc.player.played).decode("latin1")
        self.assertIn("I put the details on screen", spoken)
        self.assertNotIn("rm -rf", spoken)

    def test_stop_cuts_speech_between_sentences(self):
        holder = {}
        player = FakePlayer(on_play=lambda n: holder["svc"].stop_speaking() if n == 1 else None)
        svc, *_ = make_tts(player=player)
        holder["svc"] = svc
        svc.save_settings(engine="sapi")
        res = svc.speak_aloud("The first sentence is right here. The second sentence is right here. The third sentence is right here.")
        self.assertEqual(res, "spoken")
        self.assertEqual(len(player.played), 1)
        self.assertGreaterEqual(player.stops, 1)


class PanicAndPresence(unittest.TestCase):
    def test_panic_silences_clears_and_resumes(self):
        svc, *_ = make_tts()
        svc.save_settings(engine="sapi")
        svc.silence()
        self.assertEqual(svc.speak_aloud("Nobody hears this one."), "suppressed")
        self.assertEqual(svc.synthesize("Nobody hears this one.").reason, "silenced")
        self.assertEqual(svc.player.played, [])
        self.assertGreaterEqual(svc.player.stops, 1)
        svc.resume()
        self.assertEqual(svc.speak_aloud("Everybody hears this one."), "spoken")

    def test_register_hook_is_called_when_panic_exists(self):
        import sys
        import types
        calls = []
        fake = types.ModuleType("hub.panic")
        fake.register = lambda name, stop, resume, ctx=None: calls.append((name, stop, resume, ctx))
        import hub
        sys.modules["hub.panic"] = fake
        hub.panic = fake
        try:
            svc, config, *_ = make_tts()
            ctx = Ctx(config=config, model_for=None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
            tts.make_router(ctx, service=svc)
            self.assertEqual(calls[0][0], "voice")
            calls[0][1]()
            self.assertTrue(svc.silenced)
            calls[0][2]()
            self.assertFalse(svc.silenced)
            self.assertIs(calls[0][3], ctx)
        finally:
            sys.modules.pop("hub.panic", None)
            if hasattr(hub, "panic"):
                del hub.panic

    def test_no_panic_module_is_fine(self):
        svc, config, *_ = make_tts()
        ctx = Ctx(config=config, model_for=None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        tts.make_router(ctx, service=svc)                      # must not raise

    def test_presence_in_meeting_or_focus_keeps_the_laptop_quiet(self):
        state = {"v": "available"}
        svc, *_ = make_tts(presence=lambda: state["v"])
        svc.save_settings(engine="sapi")
        self.assertEqual(svc.speak_aloud("Hello out loud, now."), "spoken")
        for quiet in ("in_meeting", "focus", {"state": "in_meeting"}):
            state["v"] = quiet
            played = len(svc.player.played)
            self.assertEqual(svc.speak_aloud("Silence please, quiet."), "suppressed")
            self.assertEqual(len(svc.player.played), played)

    def test_presence_errors_never_silence_or_crash(self):
        def boom():
            raise RuntimeError("x")
        svc, *_ = make_tts(presence=boom)
        svc.save_settings(engine="sapi")
        self.assertEqual(svc.speak_aloud("Still speaking away."), "spoken")

    def test_presence_module_is_optional(self):
        svc, *_ = make_tts()
        self.assertFalse(svc.quiet_by_presence())


class WakewordWiring(unittest.TestCase):
    def rig(self, **svc_kw):
        from tests.test_wakeword import Rig
        r = Rig("Hey Jarvis, go")
        svc, *_ = make_tts(**svc_kw)
        svc.config = r.config                                  # the same hub data folder (assistants.json, tts.json)
        r.ctx.tts = svc
        return r, svc

    def test_listener_speaker_goes_through_the_engine(self):
        from tests.test_wakeword import LOUD  # noqa: F401
        r, svc = self.rig()
        svc.save_settings(engine="sapi")
        lis = wakeword.WakeListener(r.ctx, assistant=lambda *a, **k: "", chime=lambda p: None, clock=r.clock)
        lis.say_aloud("Hello from the laptop, friend.")
        self.assertEqual(len(svc.player.played), 1)
        self.assertEqual(lis.state, "off")

    def test_default_behaviour_is_the_old_speaker(self):
        r, svc = self.rig()
        spoken = []
        old = wakeword.default_speaker
        wakeword.default_speaker = spoken.append
        try:
            lis = wakeword.WakeListener(r.ctx, assistant=lambda *a, **k: "", chime=lambda p: None, clock=r.clock)
            lis.say_aloud("Hello from the laptop, friend.")
        finally:
            wakeword.default_speaker = old
        self.assertEqual(spoken, ["Hello from the laptop, friend."])
        self.assertEqual(svc.player.played, [])

    def test_persona_picks_the_voice(self):
        r, svc = self.rig(which=lambda n: n)
        svc.save_settings(engine="sapi")
        lst = A.load(r.config)
        for a in lst:
            if a["id"] == "friday":
                a["voice"] = {"rate": 1.0, "pitch": 1.0, "voice": "Microsoft Zira Desktop", "engine": "sapi"}
        A.save(r.config, lst)
        lis = wakeword.WakeListener(r.ctx, assistant=lambda *a, **k: "Hi there, I am here now.", chime=lambda p: None, clock=r.clock,
                                    transcriber=r.ctx.transcriber)
        lis._persona = "friday"
        lis.say_aloud("Hello from Friday, my friend.")
        self.assertEqual(svc.runner.calls[-1]["env"]["REDLINE_TTS_VOICE"], "Microsoft Zira Desktop")

    def test_barge_in_stops_the_laptop_voice_only_in_headphones_mode(self):
        from tests.test_wakeword import LOUD
        r, svc = self.rig()
        lis = wakeword.WakeListener(r.ctx, assistant=lambda *a, **k: "", chime=lambda p: None, clock=r.clock)
        lis.speaking = True
        loud = wakeword.array.array("h", [5000 if i % 2 else -5000 for i in range(wakeword.FRAME)]).tobytes()
        for _ in range(wakeword.BARGE_FRAMES + 2):
            lis.feed(loud)
        self.assertEqual(svc.player.stops, 0)                  # speakers: its own voice would trigger this, so never
        svc.save_settings(headphones=True)
        for _ in range(wakeword.BARGE_FRAMES - 1):
            lis.feed(loud)
        self.assertEqual(svc.player.stops, 0)
        lis.feed(loud)
        self.assertEqual(svc.player.stops, 1)
        self.assertTrue(svc._stop.is_set())
        del LOUD

    def test_quiet_frames_never_barge(self):
        r, svc = self.rig()
        svc.save_settings(headphones=True)
        lis = wakeword.WakeListener(r.ctx, assistant=lambda *a, **k: "", chime=lambda p: None, clock=r.clock)
        lis.speaking = True
        for _ in range(50):
            lis.feed(wakeword.array.array("h", [10] * wakeword.FRAME).tobytes())
        self.assertEqual(svc.player.stops, 0)

    def test_alerts_speak_through_the_engine(self):
        from hub import alerts
        r, svc = self.rig()
        svc.save_settings(engine="sapi")
        r.ctx.wakeword = None
        alerts.default_speaker(r.ctx)("Urgent message from a person.")
        self.assertEqual(len(svc.player.played), 1)


@unittest.skipUnless(NODE, "node is not installed")
class BrowserQueue(unittest.TestCase):
    def run_js(self, body: str):
        script = ('import { createSpeechQueue } from %s;\nimport { createSpeechPlanner, DETAILS_LINE } from %s;\n%s' %
                  (json.dumps((STATIC / "js" / "speechqueue.js").as_uri()), json.dumps((STATIC / "js" / "speechchunk.js").as_uri()), body))
        out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    HEAD = """
const tick = () => new Promise((r) => setImmediate(r));
// fake clock: timers fire when advance() passes them
let now = 0; const timers = [];
const setTimer = (fn, ms) => { const t = { at: now + ms, fn, live: true }; timers.push(t); return t; };
const clearTimer = (t) => { if (t) t.live = false; };
const advance = async (ms) => { now += ms; for (const t of timers) if (t.live && t.at <= now) { t.live = false; t.fn(); } await tick(); await tick(); };
const flush = async () => { for (let i = 0; i < 20; i++) await tick(); };
"""

    def test_plays_in_order_even_when_audio_arrives_out_of_order(self):
        out = self.run_js(self.HEAD + """
const played = []; const resolvers = {};
const q = createSpeechQueue({ setTimer, clearTimer,
  synth: (t) => new Promise((r) => { resolvers[t] = () => r({ kind: "audio", data: t }); }),
  play: async (d) => { played.push(d); }, playBrowser: async (t) => { played.push("B:" + t); } });
q.push("one"); q.push("two"); q.push("three");
resolvers.three(); resolvers.two(); await flush(); const before = [...played];
resolvers.one(); await q.idle();
console.log(JSON.stringify({ before, played }));
""")
        self.assertEqual(out["before"], [])
        self.assertEqual(out["played"], ["one", "two", "three"])

    def test_first_sentence_plays_while_later_ones_are_still_being_made(self):
        out = self.run_js(self.HEAD + """
const log = []; let release;
const q = createSpeechQueue({ setTimer, clearTimer,
  synth: (t) => { log.push("make:" + t); return t === "b" ? new Promise((r) => { release = () => r({ kind: "audio", data: "b" }); }) : Promise.resolve({ kind: "audio", data: t }); },
  play: async (d) => { log.push("play:" + d); } , playBrowser: async () => {} });
q.push("a"); q.push("b"); await flush();
const mid = [...log]; release(); await q.idle();
console.log(JSON.stringify({ mid, log }));
""")
        self.assertEqual(out["mid"], ["make:a", "make:b", "play:a"])
        self.assertEqual(out["log"][-1], "play:b")

    def test_slow_hub_falls_back_to_the_browser_voice_after_3s_and_keeps_order(self):
        out = self.run_js(self.HEAD + """
const played = []; let aborted = false;
const q = createSpeechQueue({ setTimer, clearTimer, timeoutMs: 3000,
  synth: (t, m, signal) => t === "slow" ? new Promise(() => { signal.addEventListener("abort", () => { aborted = true; }); }) : Promise.resolve({ kind: "audio", data: t }),
  play: async (d) => { played.push(d); }, playBrowser: async (t) => { played.push("B:" + t); } });
q.push("slow"); q.push("fast"); await advance(2900); const early = [...played]; await advance(200); await q.idle();
console.log(JSON.stringify({ early, played, aborted }));
""")
        self.assertEqual(out["early"], [])
        self.assertEqual(out["played"], ["B:slow", "fast"])
        self.assertTrue(out["aborted"])

    def test_errors_and_204_use_the_browser_and_silent_says_nothing(self):
        out = self.run_js(self.HEAD + """
const played = [];
const q = createSpeechQueue({ setTimer, clearTimer,
  synth: (t) => t === "err" ? Promise.reject(new Error("x")) : t === "b" ? Promise.resolve({ kind: "browser" }) : Promise.resolve({ kind: "silent" }),
  play: async (d) => { played.push(d); }, playBrowser: async (t) => { played.push("B:" + t); } });
q.push("err"); q.push("b"); q.push("quiet"); await q.idle();
console.log(JSON.stringify(played));
""")
        self.assertEqual(out, ["B:err", "B:b"])

    def test_barge_in_cancel_stops_playback_clears_the_queue_and_aborts_requests(self):
        out = self.run_js(self.HEAD + """
const played = []; let stops = 0, aborted = 0, release; const active = [];
const q = createSpeechQueue({ setTimer, clearTimer, onActive: (on) => active.push(on),
  stop: () => { stops++; release && release(); },
  synth: (t, m, signal) => { if (t === "two" || t === "three") signal.addEventListener("abort", () => { aborted++; }); return t === "two" || t === "three" ? new Promise(() => {}) : Promise.resolve({ kind: "audio", data: t }); },
  play: (d) => new Promise((r) => { played.push(d); release = r; }), playBrowser: async () => {} });
const done1 = q.push("one"); q.push("two"); q.push("three"); await flush();
const speakingBefore = q.speaking;
q.cancel(); await flush(); await done1;
const after = { speaking: q.speaking, pending: q.pending };
// the queue is reusable after a barge-in and does not replay the old sentences
const q2played = [];
q.push("fresh"); await flush();
console.log(JSON.stringify({ played, stops, aborted, speakingBefore, after, active }));
""")
        self.assertEqual(out["played"], ["one", "fresh"])
        self.assertEqual(out["stops"], 1)
        self.assertEqual(out["aborted"], 2)
        self.assertTrue(out["speakingBefore"])
        self.assertEqual(out["after"], {"speaking": False, "pending": 0})
        self.assertEqual(out["active"][:2], [True, False])

    def test_planner_streams_sentences_and_skips_code_urls_and_long_lists(self):
        out = self.run_js("""
const p = createSpeechPlanner();
const text = "Sure thing, here is how it works. Run this:\\n```sh\\nnpm install\\nnpm test\\n```\\nThen visit https://example.com/a/b for more. Steps are:\\n- one thing to do now\\n- two things to do now\\n- three things to do now\\n- four things to do now\\n- five things to do now\\nAll done, enjoy the result.";
const spoken = []; const firstAt = [];
for (let i = 0; i < text.length; i += 5) { const got = p.feed(text.slice(i, i + 5)); if (got.length && !firstAt.length) firstAt.push(i + 5); spoken.push(...got); }
spoken.push(...p.feed("", true));
console.log(JSON.stringify({ spoken, firstAt: firstAt[0], total: text.length, DETAILS_LINE }));
""")
        sp = out["spoken"]
        self.assertEqual(sp[0], "Sure thing, here is how it works.")
        self.assertLess(out["firstAt"], 60)                              # speech starts long before the reply ends
        self.assertNotIn("npm", " ".join(sp))
        self.assertNotIn("http", " ".join(sp))
        self.assertIn("Then visit a link for more.", sp)
        self.assertEqual(sp.count(out["DETAILS_LINE"]), 1)
        self.assertIn("three things to do now", sp)
        self.assertNotIn("four things to do now", sp)
        self.assertEqual(sp[-1], "All done, enjoy the result.")

    def test_planner_holds_an_unfinished_code_fence(self):
        out = self.run_js("""
const p = createSpeechPlanner();
const a = p.feed("Look at this carefully now, please. ```js\\nlet x = 1;"); const b = p.feed("\\n```\\nThat is all of it, thanks.", false); const c = p.feed("", true);
console.log(JSON.stringify({ a, b, c }));
""")
        self.assertEqual(out["a"], ["Look at this carefully now, please."])
        joined = " ".join(out["b"] + out["c"])
        self.assertNotIn("let x", joined)
        self.assertIn("That is all of it, thanks.", joined)

    def test_planner_caps_total_speech(self):
        out = self.run_js("""
const p = createSpeechPlanner({ cap: 100 }); const s = [];
for (let i = 0; i < 8; i++) s.push(...p.feed("This is sentence number " + i + " of a long answer. "));
s.push(...p.feed("", true));
console.log(JSON.stringify({ s, capped: p.state.capped }));
""")
        self.assertTrue(out["capped"])
        self.assertEqual(out["s"][-1], "The rest is in the chat.")

    def test_stop_words_include_okay_okay(self):
        src = (STATIC / "js" / "voicemode.js").read_text(encoding="utf-8")
        self.assertIn("ok(ay)?( ok(ay)?)+", src)
        self.assertIn("quiet", src)


class Wiring(unittest.TestCase):
    def test_module_registered_and_assets_cached(self):
        from hub import server
        self.assertIn("tts", server.FEATURE_MODULES)
        sw = (STATIC / "sw.js").read_text(encoding="utf-8")
        for f in ("/js/speechqueue.js", "/js/hubtts.js", "/js/voices.js"):
            self.assertIn(f, sw)
        self.assertIn('const VERSION = "v28-lanes-formfill-mcp-2026.11"', sw)

    def test_config_keys_default_to_unchanged_behaviour(self):
        config, _ = make_config()
        self.assertIs(config.tts_enabled, True)
        self.assertEqual(config.tts_engine, "browser")

    def test_static_files_have_no_innerhtml(self):
        for f in ("voices.js", "hubtts.js", "speechqueue.js"):
            self.assertNotIn("innerHTML", (STATIC / "js" / f).read_text(encoding="utf-8"), f)


if __name__ == "__main__":
    unittest.main()
