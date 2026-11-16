"""The fake-data dev server must not read the owner's real folders (Claude transcripts hold real session names)."""
import shutil
import unittest
from unittest import mock

from hub import devserver


class DevServerIsolation(unittest.TestCase):
    def test_ccsessions_dir_is_inside_the_temp_root(self):
        seen = {}

        def fake_create_app(config, deps, audit_path=None):
            seen["config"] = config
            return object()

        with mock.patch.object(devserver, "create_app", fake_create_app):
            _app, root = devserver.build(0)
        try:
            config = seen["config"]
            self.assertTrue(config.ccsessions_dir)
            self.assertEqual(config.ccsessions_dir, str(root / "ccsessions"))
            self.assertTrue(config.data_dir.startswith(str(root)))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class AutoChainIsFake(unittest.TestCase):
    def test_every_provider_is_a_fake_and_auto_means_local(self):
        seen = {}

        def fake_create_app(config, deps, audit_path=None):
            seen["config"], seen["deps"] = config, deps
            return object()

        with mock.patch.object(devserver, "create_app", fake_create_app):
            _app, root = devserver.build(0)
        shutil.rmtree(root, ignore_errors=True)
        deps = seen["deps"]
        for name in ("llm", "claude", "claude_login", "gemini", "duo"):
            self.assertIsInstance(getattr(deps, name), devserver.FakeLLM, name)
        self.assertEqual(seen["config"].default_provider, "local")

    def test_the_real_auto_chain_answers_with_the_fake_and_no_cli_program_starts(self):
        from fastapi.testclient import TestClient
        from hub import cliprov
        started = []

        async def watch(*argv, **kw):
            started.append(argv)
            raise OSError("no real program in tests")

        with mock.patch.object(cliprov, "thread_spawn", watch):
            app, root = devserver.build(0)
            try:
                with TestClient(app, base_url="http://127.0.0.1:8799", headers={"X-Hub": "1"}) as client:
                    client.post("/api/login", json={"token": devserver.DEV_TOKEN})
                    note = client.post("/api/notes", json={"text": "check the chain"}).json()
                    self.assertTrue(note["id"])
            finally:
                shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(started, [])

    def test_fake_cli_providers_blocks_starting_programs(self):
        import asyncio
        from hub import cliprov
        with mock.patch.object(cliprov, "thread_spawn", cliprov.thread_spawn),                 mock.patch.object(cliprov, "build_guarded", cliprov.build_guarded):
            devserver._fake_cli_providers()
            with self.assertRaises(OSError):
                asyncio.run(cliprov.thread_spawn("anything"))
            self.assertIsInstance(cliprov.build_guarded(None, None, "agy"), devserver.FakeLLM)


if __name__ == "__main__":
    unittest.main()
