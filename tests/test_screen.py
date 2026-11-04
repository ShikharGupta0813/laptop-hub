"""Tests for the SCREEN module. The screen, mouse, keyboard and clock are all faked: nothing here
captures a real screen, moves a real mouse, types, or reaches the network."""
import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import screen
from hub.common import Ctx
from hub.llm import LLMError
from tests.helpers import make_config

logging.getLogger("hub.screen").addHandler(logging.NullHandler())
logging.getLogger("hub.screen").propagate = False          # expected failures stay quiet

ROOT = Path(__file__).resolve().parent.parent
JPEG = b"\xff\xd8fake-jpeg-bytes\xff\xd9"
SECRET = "hunter2-TOP-SECRET-password"
DISARMED = "Remote control is off. Tap Enable control first."

MONITORS = [{"id": 1, "left": 0, "top": 0, "width": 1920, "height": 1080},
            {"id": 2, "left": 1920, "top": 0, "width": 1280, "height": 720}]


def need_auth(request: Request):
    if not request.cookies.get("hub_session"):
        raise HTTPException(401, "Log in first.")


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeScreen:
    """Records what the module asks the operating system to do, and does none of it."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.ok, self.reason = True, ""
        self.control_ok, self.control_reason = True, ""
        self.windows_ok, self.windows_reason = True, ""
        self.mons = [dict(m) for m in MONITORS]
        self.capture_error: Exception | None = None
        self.input_error: Exception | None = None
        self.window_list = [{"id": "101", "title": "Notes - Editor"}, {"id": "202", "title": "Browser"}]

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def availability(self):
        return self.ok, self.reason

    def control_availability(self):
        return self.control_ok, self.control_reason

    def windows_availability(self):
        return self.windows_ok, self.windows_reason

    def monitors(self):
        return self.mons

    def capture_jpeg(self, monitor, max_side, quality):
        self.calls.append(("capture", monitor, max_side, quality))
        if self.capture_error:
            raise self.capture_error
        return JPEG, (640, 360)

    def _act(self, *call):
        self.calls.append(call)
        if self.input_error:
            raise self.input_error

    def move(self, x, y):
        self._act("move", x, y)

    def click(self, x, y, button, clicks):
        self._act("click", x, y, button, clicks)

    def drag(self, x1, y1, x2, y2, button):
        self._act("drag", x1, y1, x2, y2, button)

    def scroll(self, dx, dy):
        self._act("scroll", dx, dy)

    def type_text(self, text):
        self._act("type", text)

    def press(self, key):
        self._act("press", key)

    def hotkey(self, keys):
        self._act("hotkey", tuple(keys))

    def set_clipboard(self, text):
        self._act("clipboard", text)

    def windows(self):
        return self.window_list

    def focus_window(self, window_id):
        self._act("focus", window_id)
        if window_id not in {w["id"] for w in self.window_list}:
            raise screen.ScreenError("That window is gone. Refresh the list.", 404)


class FakeSdkClient:
    """Stands in for anthropic.AsyncAnthropic: records the request, returns a canned answer."""

    def __init__(self, answer="It is a code editor.", error=None):
        self.requests: list[dict] = []
        self.answer, self.error = answer, error
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return types.SimpleNamespace(stop_reason="end_turn",
                                     content=[types.SimpleNamespace(type="text", text=self.answer)])


class FakeClaude:
    """Shaped like hub.llm.ClaudeClient: a `.model` name and `_get()` giving the SDK client."""

    def __init__(self, sdk=None, get_error=None):
        self.model = "claude-test"
        self.sdk = sdk or FakeSdkClient()
        self.get_error = get_error

    def _get(self):
        if self.get_error:
            raise self.get_error
        return self.sdk


class ScreenCase(unittest.TestCase):
    def setUp(self):
        self.audits: list[tuple] = []
        self.notices: list[tuple] = []
        self.clock = FakeClock()
        self.system = FakeScreen()
        self.claude = None

    def build(self, authed=True, notify=None, **kw):
        cfg, _ = make_config()
        ctx = Ctx(config=cfg, model_for=lambda choice: self.claude if choice == "claude" else None,
                  transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda event, **f: self.audits.append((event, f)),
                  notify=notify or (lambda *a, **k: self.notices.append((a, k))))
        app = FastAPI()
        app.include_router(screen.make_router(ctx, system=kw.get("system", self.system), clock=self.clock,
                                              sleep=self.clock.sleep))
        client = TestClient(app)
        if authed:
            client.cookies.set("hub_session", "yes")
        self.addCleanup(client.close)
        return client

    def armed_client(self, minutes=5, **kw):
        client = self.build(**kw)
        r = client.post("/api/screen/arm", json={"confirm": True, "minutes": minutes})
        self.assertEqual(r.status_code, 200, r.text)
        self.system.calls.clear()
        self.audits.clear()
        return client

    def audit_dump(self):
        return json.dumps(self.audits) + json.dumps(self.notices, default=str)


# ------------------------------------------------------------------------------------ basics + status

class StatusTests(ScreenCase):
    def test_login_is_required_everywhere(self):
        client = self.build(authed=False)
        for method, path, body in [("get", "/status", None), ("get", "/frame", None), ("get", "/stream", None),
                                   ("post", "/arm", {"confirm": True}), ("post", "/disarm", {}),
                                   ("post", "/input", {"action": "move", "x": .5, "y": .5}),
                                   ("post", "/paste", {"text": "x"}), ("get", "/windows", None),
                                   ("post", "/windows/focus", {"id": "1"}),
                                   ("post", "/ask", {"question": "?", "model": "claude", "confirm": True})]:
            r = getattr(client, method)("/api/screen" + path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 401, path)
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.audits, [])

    def test_status_shape_view_only_by_default(self):
        data = self.build().get("/api/screen/status").json()
        self.assertTrue(data["available"])
        self.assertEqual(data["monitors"], [{"id": 1, "width": 1920, "height": 1080},
                                            {"id": 2, "width": 1280, "height": 720}])
        self.assertFalse(data["control_armed"])
        self.assertIsNone(data["armed_until"])

    def test_status_when_libraries_are_missing(self):
        self.system.ok, self.system.reason = False, "Viewing the screen needs the mss and Pillow packages, which are not installed."
        client = self.build()
        data = client.get("/api/screen/status").json()
        self.assertFalse(data["available"])
        self.assertIn("not installed", data["reason"])
        self.assertEqual(data["monitors"], [])
        for path in ("/frame", "/stream"):
            r = client.get("/api/screen" + path)
            self.assertEqual(r.status_code, 501, path)
            self.assertIn("not installed", r.json()["detail"])


# ----------------------------------------------------------------------------------------- arming

class ArmingTests(ScreenCase):
    def test_arm_needs_confirm(self):
        client = self.build()
        for body in ({}, {"minutes": 5}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}):
            self.assertEqual(client.post("/api/screen/arm", json=body).status_code, 409, body)
        self.assertFalse(client.get("/api/screen/status").json()["control_armed"])
        self.assertEqual(self.audits, [])

    def test_arm_default_five_minutes_audit_and_warn_notice(self):
        client = self.build()
        data = client.post("/api/screen/arm", json={"confirm": True}).json()
        self.assertEqual(data["armed_until"], self.clock.now + 300)
        self.assertEqual(self.audits, [("screen_arm", {"minutes": 5})])
        (args, kwargs), = self.notices
        self.assertEqual(args[0], "Remote control enabled for 5 minutes")
        self.assertEqual((kwargs["level"], kwargs["source"]), ("warn", "screen"))
        status = client.get("/api/screen/status").json()
        self.assertTrue(status["control_armed"])
        self.assertEqual(status["armed_until"], self.clock.now + 300)

    def test_minutes_must_be_one_to_fifteen(self):
        client = self.build()
        for bad in (0, 16, -1, 2.5, "5", True):
            r = client.post("/api/screen/arm", json={"confirm": True, "minutes": bad})
            self.assertEqual(r.status_code, 400, bad)
        for ok in (1, 15):
            self.assertEqual(client.post("/api/screen/arm", json={"confirm": True, "minutes": ok}).status_code, 200)
        self.assertEqual(client.get("/api/screen/status").json()["armed_until"], self.clock.now + 900)

    def test_arming_expires_with_the_clock(self):
        client = self.armed_client(minutes=2)
        move = {"action": "move", "x": .5, "y": .5}
        self.assertEqual(client.post("/api/screen/input", json=move).status_code, 200)
        self.clock.now += 119
        self.assertTrue(client.get("/api/screen/status").json()["control_armed"])
        self.assertEqual(client.post("/api/screen/input", json=move).status_code, 200)
        self.clock.now += 2          # now past the two minutes
        r = client.post("/api/screen/input", json=move)
        self.assertEqual((r.status_code, r.json()["detail"]), (409, DISARMED))
        status = client.get("/api/screen/status").json()
        self.assertFalse(status["control_armed"])
        self.assertIsNone(status["armed_until"])
        self.assertEqual(len(self.system.of("move")), 2)           # the refused one did not move anything
        self.assertEqual([a for a in self.audits if a[0] == "screen_control_expired"], [("screen_control_expired", {})])

    def test_disarm_stops_control_at_once(self):
        client = self.armed_client()
        self.assertEqual(client.post("/api/screen/disarm").status_code, 200)
        r = client.post("/api/screen/input", json={"action": "key", "key": "enter"})
        self.assertEqual(r.status_code, 409)
        self.assertIn(("screen_disarm", {"was_armed": True}), self.audits)
        self.assertEqual(client.post("/api/screen/disarm").status_code, 200)     # harmless when already off

    def test_cannot_arm_when_control_library_is_missing(self):
        self.system.control_ok, self.system.control_reason = False, "Remote control needs the pyautogui package, which is not installed."
        client = self.build()
        r = client.post("/api/screen/arm", json={"confirm": True})
        self.assertEqual(r.status_code, 501)
        self.assertIn("not installed", r.json()["detail"])
        self.assertFalse(client.get("/api/screen/status").json()["control_armed"])


# --------------------------------------------------------------------------------- disarmed refusals

class DisarmedTests(ScreenCase):
    BODIES = [
        {"action": "move", "x": .5, "y": .5}, {"action": "click", "x": .5, "y": .5}, {"action": "click"},
        {"action": "double_click"}, {"action": "right_click"},
        {"action": "drag", "x": .1, "y": .1, "x2": .9, "y2": .9}, {"action": "scroll", "dy": 3},
        {"action": "type", "text": "hello"}, {"action": "key", "key": "enter"},
        {"action": "hotkey", "keys": ["ctrl", "c"]}, {"action": "hotkey", "keys": ["alt", "f4"], "confirm": True},
        {"action": "bogus"}, {}, {"action": "type", "text": "\x1b"},
    ]

    def test_every_input_is_refused_when_off(self):
        client = self.build()
        for body in self.BODIES:
            r = client.post("/api/screen/input", json=body)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, DISARMED), body)
        self.assertEqual(self.system.calls, [])

    def test_every_input_kind_works_when_armed(self):
        client = self.armed_client()
        for body in [b for b in self.BODIES if b.get("action") in screen.INPUT_ACTIONS and b != {"action": "type", "text": "\x1b"}]:
            r = client.post("/api/screen/input", json=body)
            self.assertEqual(r.status_code, 200, (body, r.text))

    def test_paste_and_focus_are_refused_when_off(self):
        client = self.build()
        for path, body in [("/paste", {"text": "hello"}), ("/windows/focus", {"id": "101"})]:
            r = client.post("/api/screen" + path, json=body)
            self.assertEqual((r.status_code, r.json()["detail"]), (409, DISARMED), path)
        self.assertEqual(self.system.calls, [])

    def test_refusal_is_audited_without_the_body(self):
        client = self.build()
        client.post("/api/screen/input", json={"action": "type", "text": SECRET})
        self.assertEqual(self.audits, [("screen_input_refused", {"action": "type"})])
        self.assertNotIn(SECRET, self.audit_dump())


# ------------------------------------------------------------------------------------------ keyboard

class KeyboardTests(ScreenCase):
    def setUp(self):
        super().setUp()
        self.client = self.armed_client()

    def send(self, **body):
        return self.client.post("/api/screen/input", json=body)

    def test_named_keys_are_allowed(self):
        names = ["enter", "esc", "tab", "backspace", "delete", "up", "down", "left", "right", "home", "end",
                 "pageup", "pagedown", "space"] + [f"f{n}" for n in range(1, 13)]
        for name in names:
            self.assertEqual(self.send(action="key", key=name).status_code, 200, name)
        self.assertEqual([c[1] for c in self.system.of("press")], names)

    def test_aliases_and_case(self):
        self.assertEqual(self.send(action="key", key=" Escape ").status_code, 200)
        self.assertEqual(self.send(action="key", key="PgDn").status_code, 200)
        self.assertEqual([c[1] for c in self.system.of("press")], ["esc", "pagedown"])

    def test_other_keys_are_refused(self):
        for key in ("a", "f13", "printscreen", "ctrl", "win", "alt", "capslock", "ctrl+c", "", "  ", "enter; calc", "\x1b"):
            r = self.send(action="key", key=key)
            self.assertEqual(r.status_code, 400, repr(key))
        for bad in (None, 5, ["enter"], True):
            self.assertEqual(self.send(action="key", key=bad).status_code, 400, repr(bad))
        self.assertEqual(self.system.of("press"), [])

    def test_allowed_hotkeys(self):
        combos = [["ctrl", "c"], ["ctrl", "v"], ["ctrl", "x"], ["ctrl", "a"], ["ctrl", "z"], ["ctrl", "y"],
                  ["ctrl", "s"], ["ctrl", "f"], ["ctrl", "t"], ["ctrl", "w"], ["ctrl", "tab"], ["alt", "tab"],
                  ["win", "d"], ["win", "tab"]]
        for combo in combos:
            self.assertEqual(self.send(action="hotkey", keys=combo).status_code, 200, combo)
        self.assertEqual([list(c[1]) for c in self.system.of("hotkey")], combos)
        self.assertEqual(self.send(action="hotkey", keys=["Control", "C"]).status_code, 200)     # case + alias
        self.assertEqual(self.system.of("hotkey")[-1][1], ("ctrl", "c"))

    def test_other_hotkeys_are_refused(self):
        for combo in (["ctrl", "alt", "delete"], ["ctrl", "shift", "esc"], ["win", "r"], ["ctrl", "q"],
                      ["alt", "f4", "x"], ["ctrl"], ["f4"], [], ["win", "l"], ["ctrl", "n"], ["c", "ctrl"],
                      ["ctrl", "c", "v", "x"], "ctrl+c", None, [1, 2]):
            self.assertEqual(self.send(action="hotkey", keys=combo, confirm=True).status_code, 400, combo)
        self.assertEqual(self.system.of("hotkey"), [])

    def test_alt_f4_needs_confirm(self):
        r = self.send(action="hotkey", keys=["alt", "f4"])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.send(action="hotkey", keys=["alt", "f4"], confirm="yes").status_code, 409)
        self.assertEqual(self.system.of("hotkey"), [])
        self.assertEqual(self.send(action="hotkey", keys=["alt", "f4"], confirm=True).status_code, 200)
        self.assertEqual(self.system.of("hotkey"), [("hotkey", ("alt", "f4"))])

    def test_typing_rules(self):
        self.assertEqual(self.send(action="type", text="Hello, world!\nSecond line é").status_code, 200)
        self.assertEqual(self.system.of("type"), [("type", "Hello, world!\nSecond line é")])
        self.system.calls.clear()
        for text in ("tab\there", "esc\x1b[A", "carriage\rreturn", "nul\x00", "del\x7f", "bell\x07", "\u0085next-line"):
            self.assertEqual(self.send(action="type", text=text).status_code, 400, repr(text))
        for bad in ("", None, 5, ["a"]):
            self.assertEqual(self.send(action="type", text=bad).status_code, 400, repr(bad))
        self.assertEqual(self.send(action="type", text="x" * 501).status_code, 400)
        self.assertEqual(self.system.calls, [])
        self.assertEqual(self.send(action="type", text="x" * 500).status_code, 200)

    def test_paste(self):
        r = self.client.post("/api/screen/paste", json={"text": "multi\nline\ttext"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.system.calls, [("clipboard", "multi\nline\ttext"), ("hotkey", ("ctrl", "v"))])
        self.system.calls.clear()
        for body in ({}, {"text": ""}, {"text": 5}, {"text": "a\x00b"}, {"text": "x" * (screen.PASTE_MAX_CHARS + 1)}):
            self.assertEqual(self.client.post("/api/screen/paste", json=body).status_code, 400, body)
        self.assertEqual(self.system.calls, [])


# ---------------------------------------------------------------------------------------- coordinates

class CoordinateTests(ScreenCase):
    def test_to_pixels_maps_into_the_chosen_monitor_and_clamps(self):
        mon = MONITORS[1]            # 1280x720 sitting at x=1920
        self.assertEqual(screen.to_pixels(mon, 0, 0), (1920, 0))
        self.assertEqual(screen.to_pixels(mon, 1, 1), (1920 + 1279, 719))
        self.assertEqual(screen.to_pixels(mon, 5, 9), (1920 + 1279, 719))
        self.assertEqual(screen.to_pixels(mon, -3, -0.1), (1920, 0))
        self.assertEqual(screen.to_pixels(MONITORS[0], 0.5, 0.5)[0], round(0.5 * 1919))
        self.assertEqual(screen.to_pixels({"left": -1920, "top": 0, "width": 1920, "height": 1080}, 0, 1), (-1920, 1079))

    def test_unit_value_rejects_junk(self):
        for bad in (True, "0.5", None, float("nan"), float("inf"), [0.5]):
            with self.assertRaises(screen.BadInput, msg=repr(bad)):
                screen.unit_value(bad, "x")
        self.assertEqual(screen.unit_value(7, "x"), 1.0)
        self.assertEqual(screen.unit_value(-math.pi, "x"), 0.0)

    def test_move_uses_the_monitor_origin(self):
        client = self.armed_client()
        client.post("/api/screen/input", json={"action": "move", "x": 0, "y": 0, "monitor": 2})
        client.post("/api/screen/input", json={"action": "move", "x": 1, "y": 1, "monitor": 2})
        client.post("/api/screen/input", json={"action": "move", "x": 1, "y": 1})            # default monitor 1
        self.assertEqual(self.system.of("move"), [("move", 1920, 0), ("move", 3199, 719), ("move", 1919, 1079)])

    def test_out_of_range_numbers_are_clamped_not_trusted(self):
        client = self.armed_client()
        client.post("/api/screen/input", json={"action": "move", "x": 1.7, "y": -4, "monitor": 2})
        client.post("/api/screen/input", json={"action": "move", "x": 1e308, "y": 1e-308})
        self.assertEqual(self.system.of("move"), [("move", 3199, 0), ("move", 1919, 0)])

    def test_bad_coordinates_and_monitors(self):
        client = self.armed_client()
        for body in ({"action": "move"}, {"action": "move", "x": .5}, {"action": "move", "x": "0.5", "y": .5},
                     {"action": "move", "x": True, "y": .5}, {"action": "click", "x": .5},
                     {"action": "drag", "x": .1, "y": .1}, {"action": "drag", "x": .1, "y": .1, "x2": .5}):
            self.assertEqual(client.post("/api/screen/input", json=body).status_code, 400, body)
        self.assertEqual(client.post("/api/screen/input", json={"action": "move", "x": .5, "y": .5, "monitor": 9}).status_code, 404)
        self.assertEqual(client.post("/api/screen/input", json={"action": "move", "x": .5, "y": .5, "monitor": "x"}).status_code, 400)
        self.assertEqual(self.system.calls, [])

    def test_click_variants(self):
        client = self.armed_client()
        post = lambda **b: client.post("/api/screen/input", json=b)
        post(action="click", x=0, y=0)
        post(action="click", x=0, y=0, button="middle")
        post(action="double_click", x=1, y=1)
        post(action="right_click", x=0, y=0, button="left")      # the action wins
        post(action="click")                                      # where the pointer already is
        self.assertEqual(post(action="click", x=0, y=0, button="back").status_code, 400)
        self.assertEqual(self.system.of("click"), [("click", 0, 0, "left", 1), ("click", 0, 0, "middle", 1),
                                                   ("click", 1919, 1079, "left", 2), ("click", 0, 0, "right", 1),
                                                   ("click", None, None, "left", 1)])

    def test_drag_and_scroll(self):
        client = self.armed_client()
        post = lambda **b: client.post("/api/screen/input", json=b)
        self.assertEqual(post(action="drag", x=0, y=0, x2=9, y2=9).status_code, 200)
        self.assertEqual(self.system.of("drag"), [("drag", 0, 0, 1919, 1079, "left")])
        self.assertEqual(post(action="scroll", dy=500, dx=-500).status_code, 200)
        self.assertEqual(self.system.of("scroll"), [("scroll", -30, 30)])
        self.assertEqual(post(action="scroll", dy=2, x=.5, y=.5).status_code, 200)
        self.assertEqual([c[0] for c in self.system.calls][-2:], ["move", "scroll"])
        for bad in ({"action": "scroll"}, {"action": "scroll", "dy": 0}, {"action": "scroll", "dy": "3"},
                    {"action": "scroll", "dy": True}):
            self.assertEqual(client.post("/api/screen/input", json=bad).status_code, 400, bad)

    def test_system_failures_become_sentences(self):
        client = self.armed_client()
        self.system.input_error = screen.ScreenError("The laptop stopped the action because the mouse hit a screen corner.")
        r = client.post("/api/screen/input", json={"action": "key", "key": "enter"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("screen corner", r.json()["detail"])
        self.system.input_error = RuntimeError("boom")
        r = client.post("/api/screen/input", json={"action": "key", "key": "enter"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("boom", r.json()["detail"])

    def test_input_unavailable_when_pyautogui_is_missing(self):
        client = self.armed_client()
        self.system.control_ok, self.system.control_reason = False, "Remote control needs the pyautogui package, which is not installed."
        r = client.post("/api/screen/input", json={"action": "key", "key": "enter"})
        self.assertEqual(r.status_code, 501)


# -------------------------------------------------------------------------------------------- audit

class AuditTests(ScreenCase):
    def test_typed_and_pasted_text_never_reach_the_audit_log(self):
        client = self.armed_client()
        client.post("/api/screen/input", json={"action": "type", "text": SECRET})
        client.post("/api/screen/paste", json={"text": SECRET * 3})
        client.post("/api/screen/input", json={"action": "type", "text": SECRET + "\x1b"})        # refused
        self.assertIn(("screen_input", {"action": "type", "chars": len(SECRET)}), self.audits)
        self.assertIn(("screen_paste", {"chars": len(SECRET) * 3}), self.audits)
        self.assertNotIn(SECRET, self.audit_dump())
        self.assertNotIn("hunter2", self.audit_dump())

    def test_other_inputs_are_audited_with_action_and_coordinates(self):
        client = self.armed_client()
        client.post("/api/screen/input", json={"action": "click", "x": 0, "y": 0, "monitor": 2})
        client.post("/api/screen/input", json={"action": "key", "key": "enter"})
        client.post("/api/screen/input", json={"action": "hotkey", "keys": ["ctrl", "c"]})
        self.assertEqual(self.audits, [
            ("screen_input", {"action": "click", "monitor": 2, "x": 1920, "y": 0, "button": "left"}),
            ("screen_input", {"action": "key", "key": "enter"}),
            ("screen_input", {"action": "hotkey", "keys": "ctrl+c"})])


# --------------------------------------------------------------------------------------------- frames

class FrameTests(ScreenCase):
    def test_frame_is_a_jpeg_that_is_never_cached(self):
        r = self.build().get("/api/screen/frame")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.content, JPEG)
        self.assertEqual(self.system.of("capture"), [("capture", 1, 900, 55)])

    def test_query_values_are_clamped(self):
        client = self.build()
        self.clock.now += 0
        client.get("/api/screen/frame", params={"max_side": 99999, "quality": 1, "monitor": 2})
        self.clock.now += 1.1
        client.get("/api/screen/frame", params={"max_side": 5, "quality": 500})
        self.assertEqual(self.system.of("capture"), [("capture", 2, 3000, 10), ("capture", 1, 100, 95)])

    def test_bad_query_and_monitor(self):
        client = self.build()
        self.assertEqual(client.get("/api/screen/frame", params={"quality": "high"}).status_code, 400)
        self.clock.now += 2
        self.assertEqual(client.get("/api/screen/frame", params={"monitor": 7}).status_code, 404)
        self.assertEqual(self.system.of("capture"), [])

    def test_rate_limit_six_per_second_per_session(self):
        client = self.build()
        codes = [client.get("/api/screen/frame").status_code for _ in range(8)]
        self.assertEqual(codes, [200] * 6 + [429, 429])
        r = client.get("/api/screen/frame")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many", r.json()["detail"])
        self.assertTrue(r.json()["detail"].endswith("."))
        self.assertEqual(len(self.system.of("capture")), 6)           # refused ones did not capture
        self.clock.now += 0.5
        self.assertEqual(client.get("/api/screen/frame").status_code, 429)
        self.clock.now += 0.6
        self.assertEqual(client.get("/api/screen/frame").status_code, 200)

    def test_rate_limit_is_per_session(self):
        client = self.build()
        for _ in range(6):
            client.get("/api/screen/frame")
        self.assertEqual(client.get("/api/screen/frame").status_code, 429)
        other = self.build()
        other.cookies.clear()
        other.cookies.set("hub_session", "someone-else")
        self.assertEqual(other.get("/api/screen/frame").status_code, 200)

    def test_capture_failure_is_a_sentence(self):
        self.system.capture_error = OSError("display went away")
        r = self.build().get("/api/screen/frame")
        self.assertEqual(r.status_code, 500)
        self.assertIn("display went away", r.json()["detail"])


# -------------------------------------------------------------------------------------------- streams

def run(coro):
    return asyncio.run(coro)


class StreamTests(ScreenCase):
    def test_http_stream_is_mjpeg_and_ends_at_ten_minutes(self):
        client = self.build()
        r = client.get("/api/screen/stream", params={"fps": 1})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("multipart/x-mixed-replace"))
        self.assertIn(f"boundary={screen.BOUNDARY}", r.headers["content-type"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        body = r.content
        self.assertEqual(body.count(b"Content-Type: image/jpeg"), 600)
        self.assertIn(b"stopped after 10 minutes", body)
        self.assertIn(b"Open it again", body)
        self.assertTrue(body.endswith(f"--{screen.BOUNDARY}--\r\n".encode()))
        self.assertIn(("screen_stream", {"monitor": 1, "fps": 1}), self.audits)

    def test_fps_is_capped_between_one_and_six(self):
        client = self.build()
        client.get("/api/screen/stream", params={"fps": 99})
        self.assertEqual(set(self.clock.slept), {1 / 6})
        self.clock.slept.clear()
        client.get("/api/screen/stream", params={"fps": 0})
        self.assertEqual(set(self.clock.slept), {1.0})
        self.clock.slept.clear()
        client.get("/api/screen/stream")
        self.assertEqual(set(self.clock.slept), {1 / 3})
        self.assertEqual(client.get("/api/screen/stream", params={"fps": "fast"}).status_code, 400)
        self.assertEqual(client.get("/api/screen/stream", params={"monitor": 5}).status_code, 404)

    def frames(self, hub, token, clock, **kw):
        async def grab():
            return JPEG

        async def never():
            return False
        return screen.mjpeg_frames(hub, token, grab, kw.get("fps", 2), kw.get("gone", never), clock, clock.sleep,
                                   kw.get("max_seconds", screen.STREAM_MAX_SECONDS))

    def test_a_second_stream_replaces_the_first(self):
        hub, clock = screen.StreamHub(), FakeClock()

        async def scenario():
            first_token = hub.claim()
            first = self.frames(hub, first_token, clock)
            self.assertIn(b"image/jpeg", await anext(first))
            self.assertTrue(hub.active)
            second_token = hub.claim()                 # a new request arrives
            second = self.frames(hub, second_token, clock)
            old_end = await anext(first)               # the first one closes itself with a sentence
            self.assertIn(b"newer one was opened", old_end)
            with self.assertRaises(StopAsyncIteration):
                await anext(first)
            self.assertTrue(hub.active, "closing the old stream must not mark the new one as finished")
            self.assertIn(b"image/jpeg", await anext(second))
            self.assertTrue(hub.is_current(second_token) and not hub.is_current(first_token))
            await second.aclose()
            self.assertFalse(hub.active)
        run(scenario())

    def test_stream_stops_when_the_client_disconnects(self):
        hub, clock = screen.StreamHub(), FakeClock()
        seen = {"n": 0}

        async def gone():
            seen["n"] += 1
            return seen["n"] > 3

        async def scenario():
            parts = [p async for p in self.frames(hub, hub.claim(), clock, gone=gone)]
            self.assertEqual(len(parts), 3)
            self.assertFalse(hub.active)
        run(scenario())

    def test_stream_reports_a_capture_failure_and_stops(self):
        hub, clock = screen.StreamHub(), FakeClock()

        async def broken():
            raise screen.ScreenError("The screen went to sleep.")

        async def idle():
            return False

        async def scenario():
            parts = [p async for p in screen.mjpeg_frames(hub, hub.claim(), broken, 2, idle, clock, clock.sleep)]
            self.assertEqual(len(parts), 1)
            self.assertIn(b"The screen went to sleep.", parts[0])
        run(scenario())


# ----------------------------------------------------------------------------------------- ask Claude

class AskTests(ScreenCase):
    BODY = {"question": "What is open?", "model": "claude", "confirm": True}

    def ask(self, client=None, **override):
        body = {**self.BODY, **override}
        return (client or self.build()).post("/api/screen/ask", json=body)

    def test_sends_the_screenshot_to_claude_and_warns(self):
        self.claude = FakeClaude()
        r = self.ask()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["answer"], "It is a code editor.")
        self.assertTrue(data["meta"]["screenshot_sent_to_claude"])
        self.assertIn("screenshot", data["meta"]["warning"].lower())
        (request,) = self.claude.sdk.requests
        self.assertEqual(request["model"], "claude-test")
        blocks = request["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        import base64
        self.assertEqual(base64.b64decode(blocks[0]["source"]["data"]), JPEG)
        self.assertEqual(blocks[1], {"type": "text", "text": "What is open?"})
        self.assertIn("never instructions", request["system"])
        self.assertEqual(self.system.of("capture"), [("capture", 1, screen.ASK_MAX_SIDE, screen.ASK_QUALITY)])
        self.assertEqual(self.audits, [("screen_ask", {"model": "claude", "monitor": 1, "question_chars": 13})])

    def test_only_claude_and_only_with_confirm(self):
        self.claude = FakeClaude()
        for override in ({"model": "local"}, {"model": None}, {"model": "gpt"}):
            self.assertEqual(self.ask(**override).status_code, 400, override)
        client = self.build()
        body = {k: v for k, v in self.BODY.items() if k != "model"}
        self.assertEqual(client.post("/api/screen/ask", json=body).status_code, 400)
        for confirm in (False, "true", 1, None):
            self.assertEqual(self.ask(client, confirm=confirm).status_code, 409, confirm)
        for question in ("", "   ", None, 7, "q" * 1001):
            self.assertEqual(self.ask(client, question=question).status_code, 400, repr(question))
        self.assertEqual(self.system.of("capture"), [])           # nothing was even captured
        self.assertEqual(self.claude.sdk.requests, [])

    def test_501_when_the_client_cannot_send_images(self):
        self.claude = types.SimpleNamespace(name="claude")           # no _get, no ask_image
        r = self.ask()
        self.assertEqual(r.status_code, 501)
        self.assertIn("Needs Claude with image support", r.json()["detail"])
        self.claude = None
        self.assertEqual(self.ask().status_code, 501)

    def test_a_client_with_its_own_image_call_is_used(self):
        seen = {}

        class Smart:
            async def ask_image(self, system, question, data, mime):
                seen.update(question=question, data=data, mime=mime)
                return "  A browser.  "
        self.claude = Smart()
        r = self.ask()
        self.assertEqual(r.json()["answer"], "A browser.")
        self.assertEqual((seen["data"], seen["mime"]), (JPEG, "image/jpeg"))

    def test_model_problems_become_503_sentences(self):
        self.claude = FakeClaude(get_error=LLMError("Claude needs ANTHROPIC_API_KEY set in the environment."))
        r = self.ask()
        self.assertEqual(r.status_code, 503)
        self.assertIn("ANTHROPIC_API_KEY", r.json()["detail"])

    def test_question_text_is_not_audited(self):
        self.claude = FakeClaude()
        self.ask(question=f"Is the password {SECRET} visible?")
        self.assertNotIn(SECRET, self.audit_dump())

    def test_works_while_control_is_off(self):
        self.claude = FakeClaude()
        self.assertEqual(self.ask().status_code, 200)


# ---------------------------------------------------------------------------------------------- windows

class WindowTests(ScreenCase):
    def test_list_windows(self):
        r = self.build().get("/api/screen/windows")
        self.assertEqual(r.json(), {"windows": [{"id": "101", "title": "Notes - Editor"}, {"id": "202", "title": "Browser"}]})

    def test_windows_unavailable_sentence(self):
        self.system.windows_ok, self.system.windows_reason = False, "Listing windows needs the pygetwindow package on a Windows laptop."
        client = self.build()
        r = client.get("/api/screen/windows")
        self.assertEqual(r.status_code, 501)
        self.assertEqual(client.post("/api/screen/windows/focus", json={"id": "101"}).status_code, 409)

    def test_focus_needs_arming_and_a_valid_id(self):
        client = self.armed_client()
        self.assertEqual(client.post("/api/screen/windows/focus", json={"id": "101"}).status_code, 200)
        self.assertEqual(self.system.of("focus"), [("focus", "101")])
        for bad in ({}, {"id": ""}, {"id": "a b"}, {"id": "x;calc"}, {"id": "a" * 65}, {"id": True}, {"id": ["1"]}):
            self.assertEqual(client.post("/api/screen/windows/focus", json=bad).status_code, 400, bad)
        r = client.post("/api/screen/windows/focus", json={"id": "999"})
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "That window is gone. Refresh the list."))
        self.assertIn(("screen_focus_window", {"id": "101"}), self.audits)


# --------------------------------------------------------------------------- real system, no libraries

class FakePyAutoGui:
    class FailSafeException(Exception):
        pass

    def __init__(self):
        self.FAILSAFE, self.PAUSE = True, 0.1
        self.calls: list[tuple] = []
        self.raise_failsafe = False

    def _do(self, *call):
        self.calls.append(call)
        if self.raise_failsafe:
            raise self.FailSafeException()

    def moveTo(self, x, y, duration=0):
        self._do("moveTo", x, y)

    def click(self, **kw):
        self._do("click", kw["button"], kw["clicks"])

    def scroll(self, n):
        self._do("scroll", n)

    def hscroll(self, n):
        self._do("hscroll", n)

    def write(self, text, interval=0):
        self._do("write", text)

    def press(self, key):
        self._do("press", key)

    def hotkey(self, *keys):
        self._do("hotkey", keys)


class RealSystemTests(unittest.TestCase):
    def test_module_imports_cleanly_without_the_libraries(self):
        code = ("import sys\n"
                "for name in ('mss','PIL','pyautogui','pygetwindow'): sys.modules[name] = None\n"
                "import hub.screen as s\n"
                "sysm = s.RealScreenSystem()\n"
                "print(sysm.availability()[1]); print(sysm.control_availability()[1]); print(sysm.windows_availability()[1])\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIn("pip install -r requirements-screen.txt", line)
        self.assertIn("not installed", lines[0])
        self.assertIn("not installed", lines[1])

    def test_router_works_end_to_end_with_no_libraries(self):
        blocked = {name: None for name in ("mss", "PIL", "PIL.Image", "pyautogui", "pygetwindow")}
        cfg, _ = make_config()
        ctx = Ctx(config=cfg, model_for=None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)
        with mock.patch.dict(sys.modules, blocked):
            app = FastAPI()
            app.include_router(screen.make_router(ctx, system=screen.RealScreenSystem()))
            client = TestClient(app)
            client.cookies.set("hub_session", "yes")
            status = client.get("/api/screen/status").json()
            self.assertFalse(status["available"])
            self.assertIn("not installed", status["reason"])
            self.assertIn("not installed", status["control_reason"])
            self.assertEqual(client.get("/api/screen/frame").status_code, 501)
            self.assertEqual(client.post("/api/screen/arm", json={"confirm": True}).status_code, 501)
            self.assertEqual(client.get("/api/screen/windows").status_code, 501)
            client.close()

    def test_pyautogui_failsafe_is_switched_off_explicitly(self):
        fake = FakePyAutoGui()
        with mock.patch.dict(sys.modules, {"pyautogui": fake}):
            real = screen.RealScreenSystem()
            self.assertTrue(real.control_availability()[0])
            real.move(0, 0)                                  # a corner: would abort with the failsafe on
        self.assertFalse(fake.FAILSAFE)
        self.assertEqual(fake.PAUSE, 0)
        self.assertEqual(fake.calls, [("moveTo", 0, 0)])

    def test_failsafe_exception_is_handled_as_a_sentence(self):
        fake = FakePyAutoGui()
        fake.raise_failsafe = True
        with mock.patch.dict(sys.modules, {"pyautogui": fake}):
            with self.assertRaises(screen.ScreenError) as caught:
                screen.RealScreenSystem().move(0, 0)
        self.assertIn("corner", str(caught.exception))

    def test_scroll_direction_and_ascii_typing(self):
        fake = FakePyAutoGui()
        with mock.patch.dict(sys.modules, {"pyautogui": fake}), mock.patch.object(screen.sys, "platform", "linux"):
            real = screen.RealScreenSystem()
            real.scroll(2, 3)               # down 3, right 2
            real.type_text("hi\nthere")
            real.hotkey(["ctrl", "c"])
        self.assertEqual(fake.calls, [("scroll", -3), ("hscroll", 2), ("write", "hi\nthere"), ("hotkey", ("ctrl", "c"))])


class HelperTests(unittest.TestCase):
    def test_parse_int_clamps(self):
        self.assertEqual(screen.parse_int(None, 3, 1, 6, "x"), 3)
        self.assertEqual(screen.parse_int("99", 3, 1, 6, "x"), 6)
        self.assertEqual(screen.parse_int("-4", 3, 1, 6, "x"), 1)
        self.assertEqual(screen.parse_int("2.9", 3, 1, 6, "x"), 2)
        for bad in ("nan", "inf", "abc"):
            with self.assertRaises(screen.BadInput):
                screen.parse_int(bad, 3, 1, 6, "x")

    def test_control_state_uses_the_injected_clock(self):
        clock = FakeClock()
        events = []
        state = screen.ControlState(clock, lambda e, **f: events.append(e))
        self.assertFalse(state.armed())
        state.arm(1)
        self.assertEqual(state.seconds_left(), 60)
        clock.now += 59.9
        self.assertTrue(state.armed())
        clock.now += 0.2
        self.assertFalse(state.armed())
        self.assertEqual(events, ["screen_control_expired"])


if __name__ == "__main__":
    unittest.main()
