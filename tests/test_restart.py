"""Regression for issue #12: the same app object can be started and stopped more than once in one process."""
import unittest

from fastapi.testclient import TestClient

from hub import automations, autopilot, council, friday, lanes, watch
from hub.devserver import FakeAssistant, FakeLLM, FakeTranscriber
from hub.server import Deps, create_app
from tests.helpers import make_config


def _app():
    config, _root = make_config()
    deps = Deps(llm=FakeLLM(), claude=FakeLLM(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
    return create_app(config, deps)


def _run(app):
    return TestClient(app, base_url="http://testserver", headers={"X-Hub": "1"})


class RestartTest(unittest.TestCase):
    def test_same_app_starts_and_stops_twice(self):
        app = _app()
        for _round in range(2):
            with _run(app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)

    def test_registries_keep_the_router_instance_after_stop(self):
        """The router holds its engine/manager; stop() must not drop it from the registry, or a restart
        would fail with KeyError (council, lanes) or build a second engine next to the router's."""
        app = _app()
        registries = (council.MANAGERS, lanes.SERVICES, watch.ENGINES, autopilot.ENGINES, friday.ENGINES,
                      automations.ENGINES)
        with _run(app):
            first = [dict(r) for r in registries]
        with _run(app):
            second = [dict(r) for r in registries]
        for a, b in zip(first, second):
            common = set(a) & set(b)
            self.assertTrue(common)
            for key in common:
                self.assertIs(a[key], b[key])


if __name__ == "__main__":
    unittest.main()
