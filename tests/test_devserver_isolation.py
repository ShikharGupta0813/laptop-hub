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


if __name__ == "__main__":
    unittest.main()
