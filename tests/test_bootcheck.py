"""Boot check: every feature module loads and mounts, routes do not clash, tools are well formed, the shell
files exist, JS parses, settings and config line up. Catches integration bugs that per-module tests miss."""
import dataclasses
import importlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from collections import defaultdict
from pathlib import Path

from hub import config as cfg
from hub import settings as settings_mod
from hub.assistant import TOOL_GROUPS
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import FEATURE_MODULES, STATIC, Deps, create_app
from tests.apiwalk import flat_routes
from tests.helpers import make_config


def make_app(real_assistant=False):
    config, root = make_config()
    deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(),
                assistant=None if real_assistant else FakeAssistant())
    return create_app(config, deps, audit_path=root / "audit.log"), config, root


class ModulesTest(unittest.TestCase):
    def test_every_feature_module_loads(self):
        self.assertEqual(len(FEATURE_MODULES), len(set(FEATURE_MODULES)), "duplicate module name")
        for name in FEATURE_MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(f"hub.{name}")
                self.assertTrue(hasattr(module, "make_router") or hasattr(module, "start"), name)

    def test_routes_do_not_clash(self):
        app, _config, _root = make_app()
        seen = defaultdict(list)
        for path, methods, endpoint in flat_routes(app):
            for method in methods - {"HEAD"}:
                seen[(method, path)].append(endpoint.__module__ if endpoint else "static")
        dupes = {k: v for k, v in seen.items() if len(v) > 1 and k[1] != "/"}
        self.assertEqual(dupes, {}, "the same method+path is registered twice")
        self.assertGreater(len(seen), 300)


class ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app, cls.config, cls.root = make_app(real_assistant=True)
        cls.assistant = app.state.assistant
        cls.specs = cls.assistant._static_specs

    def test_tool_names_unique_and_schemas_valid(self):
        names = [s["name"] for s in self.specs]
        self.assertEqual([n for n in set(names) if names.count(n) > 1], [], "duplicate tool names")
        for spec in self.specs:
            with self.subTest(tool=spec["name"]):
                self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec["name"]))
                self.assertTrue(str(spec.get("description", "")).strip())
                schema = spec["input_schema"]
                self.assertEqual(schema.get("type"), "object")
                props = schema.get("properties", {})
                self.assertIsInstance(props, dict)
                self.assertIn("required", schema, "the assistant's validator reads schema['required']")
                self.assertLessEqual(set(schema.get("required", [])), set(props))
                json.dumps(schema)
                for pname, p in props.items():
                    self.assertTrue(isinstance(p, dict) and ("type" in p or "enum" in p or "anyOf" in p),
                                    f"{spec['name']}.{pname} has no type")

    def test_tool_groups_name_real_tools(self):
        names = {s["name"] for s in self.specs}
        for group, (tools, pattern) in TOOL_GROUPS.items():
            with self.subTest(group=group):
                self.assertLessEqual(tools - names, set(), f"group {group} lists unknown tools")
                pattern.search("hello")

    def test_extra_tools_callable(self):
        for name, tool in self.assistant._extra.items():
            self.assertTrue(callable(tool.run), name)


class StaticTest(unittest.TestCase):
    def test_sw_shell_files_exist(self):
        text = (STATIC / "sw.js").read_text(encoding="utf-8")
        block = text.split("const SHELL = [", 1)[1].split("];", 1)[0]
        for path in re.findall(r'"([^"]+)"', block):
            with self.subTest(path=path):
                if path == "/":
                    continue
                self.assertTrue((STATIC / path.lstrip("/")).is_file(), f"sw.js SHELL lists missing {path}")

    def test_main_views_exist(self):
        text = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
        paths = re.findall(r'path:\s*"\./([^"]+)"', text)
        self.assertGreater(len(paths), 30)
        for p in paths:
            with self.subTest(path=p):
                self.assertTrue((STATIC / "js" / p).is_file(), f"main.js VIEWS lists missing js/{p}")

    def test_relative_imports_resolve(self):
        for js in (STATIC / "js").glob("*.js"):
            for rel in re.findall(r'''(?:from|import)\s*\(?\s*["'](\./[^"']+)["']''', js.read_text(encoding="utf-8")):
                with self.subTest(file=js.name, imp=rel):
                    self.assertTrue((js.parent / rel).is_file(), f"{js.name} imports missing {rel}")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_every_js_file_parses(self):
        tmp = Path(tempfile.mkdtemp())
        for js in sorted(STATIC.rglob("*.js")):
            if "vendor" in js.parts:
                continue
            with self.subTest(file=js.name):
                copy = tmp / (js.stem + ".mjs")   # ES modules: node checks .mjs as a module
                copy.write_bytes(js.read_bytes())
                r = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


class SettingsConfigTest(unittest.TestCase):
    def test_allowed_keys_exist_on_config_and_do_not_collide(self):
        keys = list(settings_mod.ALLOWED_KEYS)
        self.assertEqual([k for k in set(keys) if keys.count(k) > 1], [], "ALLOWED_KEYS has duplicates")
        fields = cfg.Config.__dataclass_fields__
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key, fields)
                self.assertNotIn(key, settings_mod.LOCKED_KEYS)

    def test_every_config_key_is_saved_and_round_trips(self):
        config, root = make_config()
        path = root / "hub.json"
        cfg.save(config, path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        for f in dataclasses.fields(cfg.Config):
            with self.subTest(field=f.name):
                self.assertIn(f.name, saved, f"Config.{f.name} is never saved")
        loaded = cfg.load(path)
        for f in dataclasses.fields(cfg.Config):
            if f.name != "session_secret":
                self.assertEqual(getattr(loaded, f.name), getattr(config, f.name), f.name)

    def test_non_default_scalars_round_trip(self):
        config, root = make_config()
        path = root / "hub.json"
        changed = []
        for f in dataclasses.fields(cfg.Config):
            value = getattr(config, f.name)
            if f.name == "port":
                continue
            if isinstance(value, bool):
                setattr(config, f.name, not value)
            elif isinstance(value, int):
                setattr(config, f.name, value + 7)
            elif isinstance(value, float):
                setattr(config, f.name, value + 7.5)
            else:
                continue
            changed.append(f.name)
        cfg.save(config, path)
        loaded = cfg.load(path)
        for name in changed:
            with self.subTest(field=name):
                self.assertEqual(getattr(loaded, name), getattr(config, name))


class StartupTest(unittest.TestCase):
    def test_create_app_is_fast(self):
        make_app(real_assistant=True)      # warm imports
        t = time.time()
        make_app(real_assistant=True)
        self.assertLess(time.time() - t, 3.0)


if __name__ == "__main__":
    unittest.main()
