"""Presence-aware delivery (hub/presence.py). Fakes only: fake clock, idle source, calendar, notifier, toaster, speaker."""
import datetime as dt
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import presence, proactive
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOON = dt.datetime(2026, 11, 9, 12, 0).timestamp()
NIGHT = dt.datetime(2026, 11, 9, 23, 30).timestamp()


class Clock:
    def __init__(self, t=NOON):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += dt.timedelta(**kw).total_seconds()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="presence-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock()
        self.idle = 10.0
        self.locked = False
        self.meeting = None
        self.pushed, self.toasts, self.spoken = [], [], []

        def notify(title, body="", level="info", link=None, source="", phone=True):
            self.pushed.append({"title": title, "body": body, "level": level, "phone": phone})
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda *a, **k: None, notify=notify)
        self.app = FastAPI()
        self.app.include_router(presence.make_router(
            self.ctx, clock=self.clock, idle_source=lambda: self.idle, lock_source=lambda: self.locked,
            calendar_source=lambda now: self.meeting, toaster=self.toasts.append, speaker=self.spoken.append, sync=True))
        self.eng = self.ctx.presence
        self.addCleanup(presence.ENGINES.pop, id(self.config), None)
        self.app.include_router(proactive.make_router(ctx=self.ctx, clock=lambda: dt.datetime.fromtimestamp(self.clock())))
        self.pro = self.ctx.proactive
        self.client = TestClient(self.app, cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)


class StateMachine(Env):
    def test_table(self):
        c = presence.compute_state
        base = dict(idle=None, locked=None, phone_active=False, meeting=False, focus=False, override=None, quiet=False)
        rows = [
            ({"idle": 10}, "at_laptop"), ({"idle": 179}, "at_laptop"), ({"idle": 180}, "near"), ({"idle": 899}, "near"),
            ({"idle": 900}, "away"), ({"idle": 10, "locked": True}, "away"), ({"idle": 10, "locked": True, "phone_active": True}, "phone_active"),
            ({"idle": 400, "phone_active": True}, "phone_active"), ({"idle": 10, "phone_active": True}, "at_laptop"),
            ({"phone_active": True}, "phone_active"), ({}, "unknown"), ({"locked": True}, "away"),
            ({"idle": 10, "meeting": True}, "in_meeting"), ({"idle": 10, "meeting": True, "focus": True}, "focus"),
            ({"idle": 10, "quiet": True}, "quiet_hours"), ({"idle": 10, "meeting": True, "quiet": True}, "in_meeting"),
            ({"idle": 10, "override": "away"}, "away"), ({"idle": 2000, "override": "at_laptop"}, "at_laptop"),
            ({"idle": 10, "override": "away", "meeting": True}, "away"),
        ]
        for kw, want in rows:
            self.assertEqual(c(**{**base, **kw}), want, kw)

    def test_hysteresis_no_flapping(self):
        self.idle = 10
        self.assertEqual(self.eng.observe(force=True), "at_laptop")
        self.idle = 500
        self.clock.advance(seconds=20)
        self.eng.observe(force=True)
        self.assertEqual(self.eng.committed, "at_laptop" if self.eng.committed == "at_laptop" else "near")
        # leaving at_laptop is a location change: it has to hold for the dwell time
        self.clock.advance(seconds=61)
        self.assertEqual(self.eng.observe(force=True), "near")
        self.idle = 1000                              # near -> away must hold 60 s
        self.clock.advance(seconds=20)
        self.assertEqual(self.eng.observe(force=True), "near")
        self.idle = 500                               # a blip back drops the candidate
        self.clock.advance(seconds=20)
        self.eng.observe(force=True)
        self.idle = 1000
        self.clock.advance(seconds=20)
        self.assertEqual(self.eng.observe(force=True), "near")
        self.clock.advance(seconds=61)
        self.assertEqual(self.eng.observe(force=True), "away")
        self.idle = 3                                 # input: at the laptop at once
        self.clock.advance(seconds=15)
        self.assertEqual(self.eng.observe(force=True), "at_laptop")

    def test_phone_heartbeat_and_expiry(self):
        self.idle = 600
        self.eng.dwell = 0
        self.eng.phone_beat(True, True)
        self.assertEqual(self.eng.observe(force=True), "phone_active")
        self.clock.advance(seconds=presence.PHONE_TTL_S + 1)
        self.assertFalse(self.eng.phone_active())
        self.eng.phone_beat(True, False)              # screen off
        self.assertFalse(self.eng.phone_active())
        self.assertTrue(self.eng.app_open())
        self.eng.phone_beat(False, None)
        self.assertFalse(self.eng.app_open())

    def test_unreadable_sensors_are_unknown(self):
        def boom():
            raise RuntimeError("no api")
        self.eng.idle_source = boom
        self.eng.lock_source = lambda: None
        self.assertEqual(self.eng.observe(force=True), "unknown")

    def test_meeting_from_calendar_and_end(self):
        self.meeting = {"title": "Standup"}
        self.assertEqual(self.eng.observe(force=True), "in_meeting")
        self.meeting = None
        self.clock.advance(seconds=61)               # past the calendar cache
        self.assertEqual(self.eng.observe(force=True), "at_laptop")

    def test_meeting_in_rows(self):
        now = dt.datetime(2026, 11, 9, 12, 0)
        rows = [{"title": "All day", "all_day": True, "start": "2026-11-09T00:00:00", "end": "2026-11-10T00:00:00"},
                {"title": "Sync", "all_day": False, "start": "2026-11-09T11:30:00", "end": "2026-11-09T12:30:00"}]
        self.assertEqual(presence.meeting_in(rows, now)["title"], "Sync")
        self.assertIsNone(presence.meeting_in(rows, dt.datetime(2026, 11, 9, 13, 0)))
        self.assertIsNone(presence.meeting_in(rows[:1], now))

    def test_quiet_hours_state(self):
        self.clock.t = NIGHT
        self.assertEqual(self.eng.observe(force=True), "quiet_hours")


class Manual(Env):
    def test_focus_and_expiry(self):
        self.eng.set_focus(60)
        self.assertEqual(self.eng.state(), "focus")
        self.assertTrue(self.eng.focus_view()["on"])
        self.clock.advance(minutes=59)
        self.eng.observe(force=True)
        self.assertEqual(self.eng.committed, "focus")
        self.clock.advance(minutes=2)
        self.eng.observe(force=True)
        self.assertEqual(self.eng.committed, "at_laptop")
        self.assertFalse(self.eng.focus_view()["on"])

    def test_focus_off(self):
        self.eng.set_focus(30)
        self.eng.set_focus(None)
        self.assertEqual(self.eng.state(), "at_laptop")

    def test_away_and_back_overrides_expire(self):
        self.eng.set_override("away")
        self.assertEqual(self.eng.state(), "away")
        self.clock.advance(minutes=presence.AWAY_DEFAULT_MIN + 1)
        self.eng.observe(force=True)
        self.assertEqual(self.eng.committed, "at_laptop")      # sensors say at the laptop again
        self.idle = 5000
        self.eng.set_override("back", 10)
        self.assertEqual(self.eng.state(), "at_laptop")
        self.clock.advance(minutes=11)
        self.eng.dwell = 0
        self.eng.observe(force=True)
        self.assertEqual(self.eng.committed, "away")

    def test_voice_phrases(self):
        m = presence.match_phrase
        self.assertEqual(m("Jarvis, I am going out"), {"kind": "away", "minutes": None})
        self.assertEqual(m("I'm back!"), {"kind": "back", "minutes": None})
        self.assertEqual(m("focus for an hour"), {"kind": "focus", "minutes": 60})
        self.assertEqual(m("Friday, focus for 45 minutes"), {"kind": "focus", "minutes": 45})
        self.assertEqual(m("focus for half an hour")["minutes"], 30)
        self.assertEqual(m("I am going out for 2 hours")["minutes"], 120)
        self.assertEqual(m("stop focus"), {"kind": "focus_off", "minutes": None})
        self.assertIsNone(m("I am going out of my mind with this bug"))
        self.assertIsNone(m("what is the weather"))
        self.assertIsNone(m("focus on the quarterly report and summarise it for the team please now"))

    def test_say_sets_flags(self):
        self.assertIn("Focus is on for 60", self.eng.say("focus for an hour"))
        self.assertEqual(self.eng.state(), "focus")
        self.assertEqual(self.eng.say("focus off"), "Focus is off.")
        self.assertIn("you are out", self.eng.say("I am going out"))
        self.assertEqual(self.eng.state(), "away")
        self.assertEqual(self.eng.say("I'm back"), "Welcome back.")
        self.assertEqual(self.eng.state(), "at_laptop")
        self.assertEqual(self.eng.say("hello there"), "")
        self.assertEqual(presence.fast_reply(self.config, "focus for an hour")[:8], "Focus is")
        self.assertEqual(presence.fast_reply(types.SimpleNamespace(), "focus for an hour"), "")

    def test_editable_phrases(self):
        self.client.put("/api/presence/phrases", json={"phrases": {"away": ["heading to the shops"]}})
        self.assertEqual(presence.match_phrase("heading to the shops", self.eng.phrases())["kind"], "away")
        self.assertIsNone(presence.match_phrase("I am going out", self.eng.phrases()))
        self.assertEqual(self.client.get("/api/presence/phrases").json()["phrases"]["away"], ["heading to the shops"])


class Delivery(Env):
    def urgent(self, **kw):
        return {"type": "reminder", "title": "Call Asha", "body": "From Asha", **kw}

    def go_away(self):
        self.idle = 5000
        self.eng.dwell = 0
        self.eng.observe(force=True)
        self.clock.advance(seconds=20)
        self.eng.observe(force=True)
        self.assertEqual(self.eng.state(), "away")

    def test_plan_table(self):
        rules = self.eng.rules()
        want = {("urgent", "at_laptop"): ["toast-on-laptop", "push-silent"], ("urgent", "away"): ["push-phone", "speak-phone"],
                ("urgent", "in_meeting"): ["hold-until-free"], ("critical", "in_meeting"): ["push-silent"],
                ("critical", "focus"): ["push-phone"], ("urgent", "focus"): ["hold-until-free"], ("normal", "at_laptop"): ["toast-on-laptop"],
                ("normal", "away"): ["batch"], ("normal", "in_meeting"): ["hold-until-free"], ("normal", "focus"): ["hold-until-free"]}
        for (u, s), acts in want.items():
            self.assertEqual(presence.plan(rules, u, s), acts, (u, s))
        self.assertIsNone(presence.plan(rules, "urgent", "unknown"))
        self.assertIsNone(presence.plan(rules, "urgent", "quiet_hours"))
        self.assertEqual(presence.event_class({"type": "alert", "urgency": 3}, "now"), "critical")
        self.assertEqual(presence.event_class({"type": "alert", "urgency": 2}, "now"), "urgent")
        self.assertEqual(presence.event_class({"type": "x", "vip": True}, "now"), "critical")
        self.assertEqual(presence.event_class({"type": "research_done"}, "batch"), "normal")

    def test_at_laptop_urgent_is_silent_push_and_toast(self):
        self.config.presence_laptop_toasts = True
        self.assertEqual(self.pro.submit(self.urgent()), "now")
        self.assertEqual(self.pushed[0]["phone"], "silent")
        self.assertEqual(self.toasts, ["Call Asha"])          # the title only
        self.assertEqual(self.spoken, [])

    def test_toasts_off_by_default(self):
        self.pro.submit(self.urgent())
        self.assertEqual(self.toasts, [])

    def test_normal_at_laptop_no_push(self):
        self.assertEqual(self.pro.submit({"type": "research_done", "title": "Research"}), "handled")
        self.assertIs(self.pushed[0]["phone"], False)

    def test_away_urgent_push_and_speak_when_app_open(self):
        self.go_away()
        self.assertEqual(self.pro.submit(self.urgent()), "now")
        self.assertIs(self.pushed[0]["phone"], True)
        self.assertEqual(self.eng.pending_speech(), [])           # app not open: push only
        self.eng.phone_beat(True, True)
        self.pro.submit(self.urgent(title="Second", body="Pay rent"))
        self.assertEqual([s["text"] for s in self.eng.pending_speech()], ["Pay rent"])
        sid = self.eng.pending_speech()[0]["id"]
        self.assertEqual(self.client.post("/api/presence/pending/ack", json={"ids": [sid]}).json()["acked"], 1)

    def test_normal_away_is_batched(self):
        self.go_away()
        self.assertEqual(self.pro.submit({"type": "autopilot_ready", "count": 2}), "batch")
        self.assertEqual(self.pushed[0]["body"], "Chief, 2 Autopilot drafts are waiting for you.")

    def test_meeting_holds_then_digest(self):
        self.meeting = {"title": "Standup"}
        self.eng.observe(force=True)
        self.assertEqual(self.pro.submit(self.urgent()), "held")
        self.assertEqual(self.pro.submit({"type": "research_done", "title": "Research done"}), "held")
        self.assertEqual(self.pro.submit({"type": "alert", "urgency": 3, "title": "Urgent from Boss", "body": "now"}), "now")
        self.assertEqual(self.pushed[0]["phone"], "silent")        # critical: silent push only
        self.assertEqual(len(self.pushed), 1)
        self.assertFalse(self.eng.release())                      # still in the meeting
        self.meeting = None
        self.clock.advance(seconds=61)
        self.eng.tick()
        self.assertEqual(len(self.pushed), 2)
        self.assertEqual(self.pushed[1]["title"], "You missed 2 things in the meeting")
        self.assertIn("Call Asha", self.pushed[1]["body"])
        self.eng.tick()
        self.assertEqual(len(self.pushed), 2)                      # released once

    def test_focus_holds_all_but_critical(self):
        self.eng.set_focus(60)
        self.assertEqual(self.pro.submit(self.urgent()), "held")
        self.assertEqual(self.pro.submit({"type": "alert", "urgency": 3, "title": "P0", "body": "x"}), "now")
        self.assertIs(self.pushed[-1]["phone"], True)
        self.client.post("/api/presence/focus", json={"off": True})
        self.assertEqual(self.pushed[-1]["title"], "You missed 1 thing while you were in focus mode")

    def test_unknown_state_is_old_behaviour(self):
        self.eng.idle_source = lambda: None
        self.assertEqual(self.eng.state(), "unknown")
        self.assertEqual(self.pro.submit(self.urgent()), "now")
        self.assertIs(self.pushed[0]["phone"], True)
        self.assertEqual(self.pro.submit({"type": "autopilot_ready"}), "batch")

    def test_quiet_hours_keep_old_rules(self):
        self.clock.t = NIGHT
        self.assertEqual(self.pro.submit({"type": "autopilot_ready"}), "quiet")
        self.assertEqual(self.pro.submit(self.urgent()), "now")
        self.assertIs(self.pushed[0]["phone"], True)

    def test_feature_off_is_old_behaviour(self):
        self.config.presence_enabled = False
        self.meeting = {"title": "x"}
        self.assertEqual(self.pro.submit(self.urgent()), "now")
        self.assertEqual(self.eng.view()["state"], "unknown")

    def test_no_presence_module_means_old(self):
        self.ctx.presence = None
        self.assertEqual(self.pro.submit(self.urgent()), "now")

    def test_panic_pauses_rules(self):
        self.meeting = {"title": "x"}
        self.eng.observe(force=True)
        fake = types.ModuleType("hub.panic")
        fake.on = lambda config=None: True
        with mock.patch.dict(sys.modules, {"hub.panic": fake}):
            self.assertEqual(self.pro.submit(self.urgent()), "now")
            self.assertIs(self.pushed[0]["phone"], True)
        fake.on = lambda config=None: False
        with mock.patch.dict(sys.modules, {"hub.panic": fake}):
            self.assertEqual(self.pro.submit(self.urgent(title="Other")), "held")

    def test_rules_edit_and_disable(self):
        rules = self.client.get("/api/presence/rules").json()["rules"]
        rules = [dict(r, enabled=False) if (r["urgency"], r["state"]) == ("urgent", "at_laptop") else r for r in rules]
        bad = {"urgency": "bogus", "state": "away", "actions": ["push-phone"]}
        self.client.put("/api/presence/rules", json={"rules": rules + [bad]})
        self.assertEqual(self.pro.submit(self.urgent()), "now")     # no matching rule: old behaviour
        self.assertIs(self.pushed[0]["phone"], True)
        self.assertEqual(len(self.client.get("/api/presence/rules").json()["rules"]), len(rules))
        self.client.post("/api/presence/rules/reset")
        self.assertEqual(self.eng.rules()[0]["id"], "critical-at_laptop")
        self.assertEqual(self.client.put("/api/presence/rules", json={"rules": "x"}).status_code, 400)

    def test_alert_speech_follows_presence(self):
        ev = {"type": "alert", "urgency": 2, "title": "t", "body": "b"}
        self.assertEqual(presence.alert_actions(self.ctx, ev), ["toast-on-laptop", "push-silent"])
        self.go_away()
        self.assertIn("speak-phone", presence.alert_actions(self.ctx, ev))


class Routes(Env):
    def test_need_login(self):
        anon = TestClient(self.app)
        for method, path in (("get", "/api/presence"), ("post", "/api/presence/phone"), ("post", "/api/presence/focus"),
                             ("get", "/api/presence/rules"), ("get", "/api/presence/pending")):
            self.assertEqual(getattr(anon, method)(path).status_code, 401, path)

    def test_heartbeat_shape(self):
        for bad in ({}, {"visible": "yes"}, {"visible": True, "screen_on": "on"}, {"visible": 1}):
            self.assertEqual(self.client.post("/api/presence/phone", json=bad).status_code, 400, bad)
        r = self.client.post("/api/presence/phone", json={"visible": True, "screen_on": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertTrue(self.client.post("/api/presence/phone", json={"visible": False}).json()["ok"])

    def test_get_presence_shape(self):
        self.client.post("/api/presence/phone", json={"visible": True})
        d = self.client.get("/api/presence").json()
        self.assertEqual(d["state"], "at_laptop")
        for k in ("enabled", "since", "signals", "focus", "held", "override", "paused"):
            self.assertIn(k, d)
        for k in ("laptop_idle_s", "locked", "phone", "meeting", "focus", "quiet_hours"):
            self.assertIn(k, d["signals"])

    def test_focus_route(self):
        r = self.client.post("/api/presence/focus", json={"minutes": 60}).json()
        self.assertTrue(r["focus"]["on"])
        self.assertEqual(r["state"], "focus")
        self.assertEqual(self.client.post("/api/presence/focus", json={"minutes": 99999}).status_code, 400)
        self.assertEqual(self.client.post("/api/presence/focus", json={"minutes": "abc"}).status_code, 400)
        self.assertEqual(self.client.post("/api/presence/focus", json={"minutes": "off"}).json()["state"], "at_laptop")

    def test_override_route(self):
        self.assertEqual(self.client.post("/api/presence/override", json={"state": "away"}).json()["state"], "away")
        self.assertEqual(self.client.post("/api/presence/override", json={"state": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/api/presence/override", json={"state": "clear"}).json()["state"], "at_laptop")


class Toast(unittest.TestCase):
    def test_script_reads_title_from_stdin(self):
        with mock.patch.object(presence.sys, "platform", "win32"), mock.patch("shutil.which", return_value="powershell"):
            argv = presence.toast_argv()
        self.assertTrue(argv)
        self.assertEqual(argv[1:3], ["-NoProfile", "-NonInteractive"])
        self.assertIn("[Console]::In.ReadToEnd()", argv[-1])

    def test_default_toaster_passes_title_on_stdin(self):
        calls = []
        with mock.patch.object(presence, "toast_argv", return_value=["powershell", "-NoProfile", "-Command", "script"]), \
                mock.patch.object(presence.subprocess, "run", side_effect=lambda argv, **kw: calls.append((argv, kw))):
            presence.default_toaster("Secret   title here")
        argv, kw = calls[0]
        self.assertNotIn("Secret", " ".join(argv))
        self.assertEqual(kw["input"], "Secret title here")

    def test_no_toast_off_windows(self):
        with mock.patch.object(presence.sys, "platform", "linux"):
            self.assertIsNone(presence.toast_argv())
            self.assertIsNone(presence.windows_idle_seconds())
            self.assertIsNone(presence.windows_locked())


class Wiring(unittest.TestCase):
    def test_registered_and_in_shell(self):
        from hub import server
        self.assertIn("presence", server.FEATURE_MODULES)
        static = Path(__file__).resolve().parent.parent / "hub" / "static"
        self.assertIn("/js/presence.js", (static / "sw.js").read_text(encoding="utf-8"))
        self.assertIn("presence.js", (static / "js" / "main.js").read_text(encoding="utf-8"))
        self.assertNotIn("innerHTML", (static / "js" / "presence.js").read_text(encoding="utf-8"))

    def test_notify_phone_switch(self):
        from hub import notify
        sent = []
        d = Path(tempfile.mkdtemp(prefix="presence-notify-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        config, root = make_config(data_dir=str(d), ntfy_server="https://ntfy.example", ntfy_topic="abcdefgh1234")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        c = notify.NotifyCenter(d / "n.db", d / "s.json", config=config, sender=lambda url, h, body: sent.append(h))
        c.push("a")
        c.push("b", phone=False)
        c.push("c", phone="silent")
        c.wait_for_pushes()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sorted(h["Priority"] for h in sent), ["2", "3"])


if __name__ == "__main__":
    unittest.main()
