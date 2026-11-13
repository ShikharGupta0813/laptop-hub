"""Doctor extras, fix-deps and setup --all, with fake probes (no git, pip, tailscale or network)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub import __main__ as cli
from hub import doctor_checks as dc


class FakeProbe(dc.Probe):
    def __init__(self, runs=None, versions=None, healthy=True):
        self.runs, self.versions, self.healthy, self.calls = runs or {}, versions or {}, healthy, []

    def run(self, argv, timeout=10, cwd=None):
        self.calls.append(list(argv))
        for key, val in self.runs.items():
            if key in " ".join(argv):
                return val
        return 0, ""

    def health(self, port):
        return self.healthy

    def dist_version(self, name):
        return self.versions.get(name.lower())


class RequirementsTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "requirements.txt").write_text("fastapi>=0.115\npywinpty>=2.0; sys_platform == \"win32\"\n# c\n")
        (self.root / "requirements-voice.txt").write_text("faster-whisper>=1.0\nav>=14.0,<17\n")

    def test_parse(self):
        reqs = dc.parse_requirements("av>=14.0,<17\nuvicorn[standard]>=0.30\n-r x\n")
        self.assertEqual(reqs[0], ("av", ">=14.0,<17", ""))
        self.assertEqual(reqs[1][0], "uvicorn")

    def test_all_good_and_windows_marker_skipped(self):
        p = FakeProbe(versions={"fastapi": "0.115.1", "faster-whisper": "1.0.3", "av": "16.0.1"})
        res = dc.check_requirements(p, self.root, "linux")
        self.assertEqual([r[0] for r in res], [True, True])

    def test_av_17_and_missing_whisper(self):
        p = FakeProbe(versions={"fastapi": "0.115.1", "av": "17.0.0"})
        res = dc.check_requirements(p, self.root, "linux")
        self.assertIs(res[1][0], False)
        self.assertIn("faster-whisper missing", res[1][1])
        self.assertIn("av 17.0.0 (needs >=14.0,<17)", res[1][1])
        self.assertIn("python -m hub fix-deps", res[1][1])

    def test_windows_requires_pywinpty(self):
        p = FakeProbe(versions={"fastapi": "0.115.1", "faster-whisper": "1.0.3", "av": "16.0.1"})
        res = dc.check_requirements(p, self.root, "win32")
        self.assertIs(res[0][0], False)


class ChecksTest(unittest.TestCase):
    def test_git_behind_and_offline(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            good, text = dc.check_git(FakeProbe(runs={"rev-list": (0, "3")}), Path(d))
            self.assertIsNone(good)
            self.assertIn("3 update(s)", text)
            good, text = dc.check_git(FakeProbe(runs={"fetch": (128, "no net"), "rev-list": (0, "0")}), Path(d))
            self.assertIs(good, True)
            self.assertIn("offline", text)
            good, text = dc.check_git(FakeProbe(runs={"rev-list": (128, "no upstream")}), Path(d))
            self.assertIsNone(good)

    def test_hub_and_tailscale(self):
        self.assertIs(dc.check_hub_running(FakeProbe(healthy=True), 8787)[0], True)
        self.assertIn("python -m hub serve", dc.check_hub_running(FakeProbe(healthy=False), 8787)[1])
        good, text = dc.check_tailscale_serve(FakeProbe(runs={"serve": (0, "https://pc.tail1234.ts.net (tailnet only)")}))
        self.assertIs(good, True)
        self.assertIn("https://pc.tail1234.ts.net", text)
        self.assertIsNone(dc.check_tailscale_serve(FakeProbe(runs={"serve": (0, "No serve config")}))[0])
        self.assertIsNone(dc.check_tailscale_serve(FakeProbe(runs={"serve": (127, "")}))[0])

    def test_pip_blocked_advice(self):
        good, text = dc.check_pip(FakeProbe(runs={"pip": (1, "blocked by Application Control")}))
        self.assertIs(good, False)
        self.assertIn("python -m pip install -r requirements.txt", text)
        self.assertIs(dc.check_pip(FakeProbe())[0], True)

    def test_wake_checks_are_data_driven(self):
        import importlib.util as iu
        with mock.patch.object(iu, "find_spec", return_value=None):
            self.assertEqual(dc.check_wake(None), [])

    def test_wizard_lines_never_crash(self):
        with tempfile.TemporaryDirectory() as d:
            lines = dc.wizard_lines(Path(d) / "hub.json")
        self.assertEqual(len(lines), len(dc.WIZARD_CHECKS))


class CliTest(unittest.TestCase):
    def test_fix_deps_uses_interpreter_not_pip_exe(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0)):
            self.assertEqual(cli.cmd_fix_deps(SimpleNamespace()), 0)
        argv = calls[0]
        self.assertEqual(argv[1:4], ["-m", "pip", "install"])
        self.assertIn("requirements.txt", argv)
        self.assertIn("requirements-voice.txt", argv)

    def test_setup_all_runs_the_three_tasks_without_asking(self):
        from hub import setup_wizard as sw
        seen = {}

        def fake_run_wizard(w, only, check, doctor):
            seen["only"], seen["yes"] = only, w.confirm("x?", False)
            return 0
        with mock.patch.object(sw, "run_wizard", fake_run_wizard):
            code = sw.run_setup(SimpleNamespace(config=None, only=None, check=False, all=True), wizard=sw.Wizard(Path("/nonexistent/hub.json")))
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"only": "updater,watchdog,backup", "yes": True})

    def test_parser_accepts_new_commands(self):
        with mock.patch.object(cli, "cmd_fix_deps", return_value=0) as m:
            self.assertEqual(cli.main(["fix-deps"]), 0)
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
