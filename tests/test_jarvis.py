"""Tests for the JARVIS module. Starting and stopping use a REAL but harmless child process: a
stand-in jarvis.py that only sleeps, launched through the injectable command."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import jarvis
from hub.common import Ctx
from tests.helpers import make_config

STAND_IN = '''\
import os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(os.path.join(here, "child.pid"), "w") as fh:
    fh.write(str(child.pid))
with open(os.path.join(here, "jarvis.log"), "a", encoding="utf-8") as fh:
    fh.write("2026-11-04 10:00:00  Jarvis online.\\n")
print("stand-in started", flush=True)
time.sleep(600)
'''


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


def stand_in_command(folder: Path):
    return [sys.executable, "jarvis.py"]


def jarvis_processes(folder: Path) -> list[psutil.Process]:
    """Live processes running a jarvis.py from `folder`."""
    found = []
    for proc in psutil.process_iter():
        try:
            if proc.status() != psutil.STATUS_ZOMBIE and "jarvis.py" in " ".join(proc.cmdline()) \
                    and os.path.samefile(proc.cwd(), folder):
                found.append(proc)
        except (psutil.Error, OSError):
            continue
    return found


def is_gone(pid: int) -> bool:
    """The process has ended (a zombie waiting for its parent to reap it counts as ended)."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


class JarvisCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
        self.dir = self.tmp / "jarvis"
        self.dir.mkdir()
        (self.dir / "jarvis.py").write_text(STAND_IN, encoding="utf-8")
        self.data = self.tmp / "data"
        self.audits = []
        self.addCleanup(self.reap)

    def reap(self):
        """No test may leave a stand-in running."""
        for proc in jarvis_processes(self.dir):
            for p in proc.children(recursive=True) + [proc]:
                try:
                    p.kill()
                except psutil.Error:
                    pass
        child = self.dir / "child.pid"
        if child.exists():
            try:
                psutil.Process(int(child.read_text())).kill()
            except (psutil.Error, ValueError):
                pass
        for popen in list(jarvis._spawned):          # let the OS finish them before the next test
            try:
                popen.wait(10)
            except subprocess.TimeoutExpired:
                pass
        jarvis._spawned.clear()

    def build(self, jarvis_dir="default", authed=True, command=stand_in_command, **kw):
        folder = str(self.dir) if jarvis_dir == "default" else jarvis_dir
        config, _ = make_config(jarvis_dir=folder, data_dir=str(self.data))
        ctx = Ctx(config=config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda event, **f: self.audits.append((event, f)))
        app = FastAPI()
        app.include_router(jarvis.make_router(ctx, command=command, **kw))
        client = TestClient(app)
        if authed:
            client.cookies.set("hub_session", "yes")
        self.addCleanup(client.close)
        return client

    def start(self, client):
        r = client.post("/api/jarvis/start")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def stop(self, client):
        return client.post("/api/jarvis/stop", json={"confirm": True})

    def wait_for(self, predicate, what, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"Timed out waiting for {what}")


class StatusAndConfigTests(JarvisCase):
    def test_login_is_required(self):
        client = self.build(authed=False)
        for method, url in (("get", "/api/jarvis/status"), ("post", "/api/jarvis/start"),
                            ("post", "/api/jarvis/stop"), ("get", "/api/jarvis/log"),
                            ("post", "/api/jarvis/say")):
            self.assertEqual(getattr(client, method)(url).status_code, 401, url)
        self.assertEqual(jarvis_processes(self.dir), [])

    def test_status_shape_when_stopped(self):
        status = self.build().get("/api/jarvis/status").json()
        self.assertEqual(status["configured"], True)
        self.assertEqual((status["running"], status["pid"], status["started_at"]), (False, None, None))
        self.assertEqual((status["log_tail"], status["remote_ready"]), ([], False))
        self.assertNotIn("reason", status)

    def test_not_configured_has_a_one_sentence_reason_and_other_routes_are_503(self):
        broken = {
            "empty": "",
            "missing folder": str(self.tmp / "nowhere"),
            "no jarvis.py": str(self.tmp),
        }
        for label, folder in broken.items():
            client = self.build(jarvis_dir=folder)
            status = client.get("/api/jarvis/status").json()
            self.assertEqual(status["configured"], False, label)
            self.assertTrue(status["reason"].endswith("."), label)
            self.assertEqual((status["running"], status["pid"], status["log_tail"], status["remote_ready"]),
                             (False, None, [], False))
            for method, url, kw in (("post", "/api/jarvis/start", {}), ("post", "/api/jarvis/stop", {"json": {"confirm": True}}),
                                    ("get", "/api/jarvis/log", {}), ("post", "/api/jarvis/say", {"json": {"text": "hi"}})):
                r = getattr(client, method)(url, **kw)
                self.assertEqual(r.status_code, 503, (label, url))
                self.assertEqual(r.json()["detail"], status["reason"])
        self.assertEqual(self.audits, [])

    @unittest.skipIf(sys.platform == "win32", "this is the not-Windows rule")
    def test_not_windows_and_no_test_launcher_is_not_configured(self):
        status = self.build(command=None).get("/api/jarvis/status").json()
        self.assertFalse(status["configured"])
        self.assertIn("Windows", status["reason"])

    def test_missing_venv_python_is_not_configured(self):
        status = self.build(command=lambda folder: None).get("/api/jarvis/status").json()
        self.assertFalse(status["configured"])
        self.assertIn(".venv", status["reason"])

    def test_default_command_is_what_jarvis_cmd_runs(self):
        self.assertIsNone(jarvis.default_command(self.dir))
        python = self.dir / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_text("")
        self.assertEqual(jarvis.default_command(self.dir), [str(python), "jarvis.py"])

    def test_remote_ready_follows_remote_inbox_py(self):
        client = self.build()
        self.assertFalse(client.get("/api/jarvis/status").json()["remote_ready"])
        (self.dir / "remote_inbox.py").write_text("")
        self.assertTrue(client.get("/api/jarvis/status").json()["remote_ready"])


class StartStopTests(JarvisCase):
    def test_start_runs_a_real_detached_process_from_the_jarvis_folder(self):
        client = self.build()
        status = self.start(client)
        self.assertTrue(status["running"])
        pid = status["pid"]
        self.assertTrue(psutil.pid_exists(pid))
        proc = psutil.Process(pid)
        self.assertIn("jarvis.py", " ".join(proc.cmdline()))
        self.assertTrue(os.path.samefile(proc.cwd(), self.dir))
        self.assertTrue(status["started_at"])
        # Output goes to a file in the hub's data folder; the pid is remembered there.
        self.wait_for(lambda: "stand-in started" in (self.data / "jarvis-output.log").read_text(), "output file")
        self.assertEqual(json.loads((self.data / "jarvis.json").read_text())["pid"], pid)
        self.assertEqual(self.audits, [("jarvis_start", {"pid": pid})])
        self.assertEqual(self.stop(client).status_code, 200)

    def test_start_twice_never_makes_a_second_process(self):
        client = self.build()
        first = self.start(client)
        second = self.start(client)
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(len(jarvis_processes(self.dir)), 1)
        self.assertEqual([a[0] for a in self.audits], ["jarvis_start"])        # only one real start
        self.stop(client)

    def test_simultaneous_starts_make_one_process(self):
        client = self.build()
        results = []

        def go():
            results.append(client.post("/api/jarvis/start").json())
        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len({r["pid"] for r in results}), 1, results)
        self.assertEqual(len(jarvis_processes(self.dir)), 1)
        self.stop(client)

    def test_stop_needs_confirmation_then_ends_jarvis_and_its_children(self):
        client = self.build()
        pid = self.start(client)["pid"]
        child_file = self.dir / "child.pid"
        self.wait_for(child_file.exists, "the stand-in's child")
        self.wait_for(lambda: child_file.read_text().strip() != "", "child pid")
        child = int(child_file.read_text())
        self.assertTrue(psutil.pid_exists(child))
        for body in (None, {}, {"confirm": False}, {"confirm": "yes"}):
            r = client.post("/api/jarvis/stop", json=body) if body is not None else client.post("/api/jarvis/stop")
            self.assertEqual(r.status_code, 409 if body != {"confirm": "yes"} else 422, body)
        self.assertTrue(psutil.pid_exists(pid))                       # still running
        r = self.stop(client)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["running"], r.json()["pid"]), (False, None))
        self.assertTrue(is_gone(pid))                                 # really gone
        self.assertTrue(is_gone(child))                               # and so is its child
        self.assertEqual(jarvis_processes(self.dir), [])
        self.assertIn(("jarvis_stop", {"pid": pid}), self.audits)
        self.assertFalse(client.get("/api/jarvis/status").json()["running"])

    def test_stop_when_not_running_is_fine(self):
        client = self.build()
        r = self.stop(client)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["running"])
        self.assertEqual(self.audits, [])

    def test_can_start_again_after_a_stop(self):
        client = self.build()
        first = self.start(client)["pid"]
        self.stop(client)
        second = self.start(client)
        self.assertTrue(second["running"])
        self.assertNotEqual(first, second["pid"])
        self.stop(client)

    def test_a_process_that_ignores_terminate_is_killed(self):
        (self.dir / "jarvis.py").write_text(
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open('ready','w').write('1')\ntime.sleep(600)\n")
        client = self.build()
        pid = self.start(client)["pid"]
        self.wait_for((self.dir / "ready").exists, "the stand-in to ignore SIGTERM")
        original = jarvis.STOP_GRACE_SECONDS
        jarvis.STOP_GRACE_SECONDS = 0.5
        self.addCleanup(setattr, jarvis, "STOP_GRACE_SECONDS", original)
        self.assertEqual(self.stop(client).status_code, 200)
        self.assertTrue(is_gone(pid))

    def test_a_launcher_that_fails_is_a_500_sentence(self):
        def broken(argv, cwd, output):
            raise OSError("The system cannot find the file specified")
        r = self.build(launcher=broken).post("/api/jarvis/start")
        self.assertEqual(r.status_code, 500)
        self.assertIn("could not be started", r.json()["detail"])
        self.assertEqual(self.audits, [])

    def test_launcher_gets_the_command_cwd_and_an_output_file_in_the_data_folder(self):
        seen = []

        def fake(argv, cwd, output):
            seen.append((argv, cwd, output))
            return os.getpid()          # pretend: the test process is "Jarvis" (it is not jarvis.py)
        self.build(launcher=fake).post("/api/jarvis/start")
        argv, cwd, output = seen[0]
        self.assertEqual((argv, cwd), ([sys.executable, "jarvis.py"], self.dir))
        self.assertEqual(output.parent, self.data)

    def test_default_launcher_flags_on_windows_are_detached(self):
        if sys.platform == "win32":
            self.assertTrue(hasattr(subprocess, "DETACHED_PROCESS"))
        else:
            self.skipTest("the Windows creation flags can only be checked on Windows")


class PidPersistenceTests(JarvisCase):
    def test_a_new_router_still_knows_the_running_jarvis(self):
        first = self.build()
        pid = self.start(first)["pid"]
        second = self.build()                                          # a hub restart
        status = second.get("/api/jarvis/status").json()
        self.assertEqual((status["running"], status["pid"]), (True, pid))
        self.assertEqual(self.start(second)["pid"], pid)               # no second process
        self.assertEqual(len(jarvis_processes(self.dir)), 1)
        self.assertEqual(self.stop(second).status_code, 200)
        self.assertTrue(is_gone(pid))
        self.assertFalse(first.get("/api/jarvis/status").json()["running"])

    def test_a_remembered_pid_that_is_not_jarvis_does_not_count(self):
        self.data.mkdir(parents=True)
        (self.data / "jarvis.json").write_text(json.dumps({"pid": os.getpid()}))
        status = self.build().get("/api/jarvis/status").json()
        self.assertEqual((status["running"], status["pid"]), (False, None))

    def test_a_dead_remembered_pid_does_not_count(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.data.mkdir(parents=True)
        (self.data / "jarvis.json").write_text(json.dumps({"pid": gone.pid}))
        self.assertFalse(self.build().get("/api/jarvis/status").json()["running"])

    def test_a_reused_pid_is_not_mistaken_for_jarvis(self):
        client = self.build()
        pid = self.start(client)["pid"]
        saved = json.loads((self.data / "jarvis.json").read_text())
        saved["create_time"] -= 3600            # as if this pid had belonged to an older process
        (self.data / "jarvis.json").write_text(json.dumps(saved))
        # The scan still finds it (same folder, jarvis.py), so no duplicate is made.
        self.assertEqual(client.get("/api/jarvis/status").json()["pid"], pid)
        self.stop(client)

    def test_a_jarvis_started_by_hand_is_found_and_not_duplicated(self):
        hand = subprocess.Popen([sys.executable, "jarvis.py"], cwd=self.dir, stdout=subprocess.DEVNULL)
        self.addCleanup(lambda: (hand.poll() is None and hand.kill(), hand.wait(10)))
        client = self.build()
        self.wait_for(lambda: client.get("/api/jarvis/status").json()["running"], "the scan to find it")
        self.assertEqual(self.start(client)["pid"], hand.pid)
        self.assertEqual(len([p for p in jarvis_processes(self.dir) if p.pid == hand.pid]), 1)
        self.assertEqual(self.audits, [])                              # the hub started nothing
        self.assertEqual(self.stop(client).status_code, 200)
        hand.wait(10)


class LogTests(JarvisCase):
    def write_log(self, count, name="jarvis.log"):
        (self.dir / name).write_text("".join(f"line {i}\n" for i in range(1, count + 1)), encoding="utf-8")

    def test_status_log_tail_is_the_last_30_lines(self):
        self.write_log(100)
        tail = self.build().get("/api/jarvis/status").json()["log_tail"]
        self.assertEqual(len(tail), 30)
        self.assertEqual((tail[0], tail[-1]), ("line 71", "line 100"))

    def test_log_route_default_n_and_cap(self):
        self.write_log(700)
        client = self.build()
        lines = client.get("/api/jarvis/log").json()["lines"]
        self.assertEqual((len(lines), lines[0], lines[-1]), (100, "line 601", "line 700"))
        self.assertEqual(client.get("/api/jarvis/log?lines=3").json()["lines"], ["line 698", "line 699", "line 700"])
        capped = client.get("/api/jarvis/log?lines=99999").json()["lines"]
        self.assertEqual((len(capped), capped[0]), (500, "line 201"))
        self.assertEqual(client.get("/api/jarvis/log?lines=0").status_code, 422)

    def test_short_missing_and_odd_files(self):
        client = self.build()
        self.assertEqual(client.get("/api/jarvis/log").json()["lines"], [])        # no log yet
        self.write_log(3)
        self.assertEqual(client.get("/api/jarvis/log").json()["lines"], ["line 1", "line 2", "line 3"])
        (self.dir / "jarvis.log").write_bytes("héllo wörld\r\nno newline at the end".encode("utf-8") + b"\xff")
        lines = client.get("/api/jarvis/log").json()["lines"]
        self.assertEqual(lines[0], "héllo wörld")
        self.assertTrue(lines[1].startswith("no newline at the end"))
        (self.dir / "jarvis.log").write_bytes(b"")
        self.assertEqual(client.get("/api/jarvis/log").json()["lines"], [])

    def test_tail_reads_only_the_end_of_a_big_file(self):
        path = self.dir / "jarvis.log"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(200_000):
                fh.write(f"entry number {i} " + "x" * 30 + "\n")
        self.assertGreater(path.stat().st_size, 8_000_000)
        started = time.perf_counter()
        lines = jarvis.tail_lines(path, 30)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(len(lines), 30)
        self.assertTrue(lines[-1].startswith("entry number 199999 "))
        self.assertTrue(lines[0].startswith("entry number 199970 "))

    def test_when_jarvis_died_at_startup_its_printed_output_is_shown(self):
        (self.dir / "jarvis.py").write_text("print('ModuleNotFoundError: no module named numpy')\n")
        client = self.build()
        self.start(client)
        self.wait_for(lambda: not client.get("/api/jarvis/status").json()["running"], "the stand-in to exit")
        status = client.get("/api/jarvis/status").json()
        self.assertTrue(any("numpy" in line for line in status["output_tail"]), status)


class SayTests(JarvisCase):
    def ready(self):
        (self.dir / "remote_inbox.py").write_text("# patch installed\n")

    def test_say_when_jarvis_is_not_running_is_409(self):
        self.ready()
        r = self.build().post("/api/jarvis/say", json={"text": "open notepad"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("not running", r.json()["detail"])
        self.assertFalse((self.dir / "remote_inbox").exists())
        self.assertEqual(self.audits, [])

    def test_say_without_the_patch_is_409_with_a_pointer_to_the_doc(self):
        client = self.build()
        self.start(client)
        r = client.post("/api/jarvis/say", json={"text": "open notepad"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("jarvis-remote-inbox.md", r.json()["detail"])
        self.assertFalse((self.dir / "remote_inbox").exists())
        self.stop(client)

    def test_say_queues_an_atomic_text_file(self):
        self.ready()
        client = self.build()
        self.start(client)
        r = client.post("/api/jarvis/say", json={"text": "  open notepad, please ✓ "})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["queued"], True)
        inbox = self.dir / "remote_inbox"
        self.assertEqual([p.name for p in inbox.iterdir()], [f"{body['id']}.txt"])   # no temp file left behind
        self.assertEqual((inbox / f"{body['id']}.txt").read_text(encoding="utf-8"), "open notepad, please ✓")
        self.stop(client)

    def test_say_ids_sort_in_the_order_they_were_sent(self):
        self.ready()
        client = self.build()
        self.start(client)
        ids = [client.post("/api/jarvis/say", json={"text": f"command {i}"}).json()["id"] for i in range(5)]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual(len(set(ids)), 5)
        self.stop(client)

    def test_say_validates_the_text_and_audits_only_the_length(self):
        self.ready()
        client = self.build()
        self.start(client)
        for bad in ("", "   ", "x" * 2001):
            self.assertEqual(client.post("/api/jarvis/say", json={"text": bad}).status_code, 400, len(bad))
        self.assertEqual(client.post("/api/jarvis/say", json={"text": "x" * 2000}).status_code, 200)
        client.post("/api/jarvis/say", json={"text": "my secret plan"})
        says = [a for a in self.audits if a[0] == "jarvis_say"]
        self.assertEqual([a[1]["chars"] for a in says], [2000, 14])
        self.assertNotIn("secret", json.dumps(self.audits))
        self.stop(client)

    def test_the_inbox_is_cleared_of_nothing_by_the_hub(self):
        """The hub only ever adds files; deleting them is Jarvis's job."""
        self.ready()
        client = self.build()
        self.start(client)
        client.post("/api/jarvis/say", json={"text": "one"})
        client.post("/api/jarvis/say", json={"text": "two"})
        self.assertEqual(len(list((self.dir / "remote_inbox").glob("*.txt"))), 2)
        self.stop(client)


if __name__ == "__main__":
    unittest.main()
