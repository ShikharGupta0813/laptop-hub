"""The owner's lists: assistants, notification preferences, custom safety rules, custom redaction rules, memory
kinds and tags, and the shared safe-pattern validator. Fakes only: fake clock, fake notifier, no network, no AI."""
import datetime as dt
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import assistants as A
from hub import brain, brain_tools, persona, proactive, redact, rules, safepattern, safety, wakeword
from hub.assistant import TOOL_GROUPS, route_local_tools
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_autopilot import need_auth
from tests.test_brain import FakeModel, NOW
from tests.test_wake import NODE, STATIC

NOON = dt.datetime(2026, 11, 9, 12, 0)
NIGHT = dt.datetime(2026, 11, 9, 23, 30)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ownerlists-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"), display_name="Chief")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits, self.pushed = [], []
        self.ctx = Ctx(config=self.config, model_for=lambda p: FakeModel(), transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda e, **k: self.audits.append((e, k)),
                       notify=lambda title, body="", level="info", link=None, source="": self.pushed.append(
                           {"title": title, "body": body, "source": source}))
        self.app = FastAPI()
        self.addCleanup(safety.set_custom_rules, [])

    def mount(self, *routers):
        for r in routers:
            self.app.include_router(r)
        self.client = TestClient(self.app, cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)
        return self.client

    def anon(self):
        c = TestClient(self.app)
        self.addCleanup(c.close)
        return c


# ---- the safe-pattern validator --------------------------------------------------------------------------------------

class SafePattern(unittest.TestCase):
    def test_plain_is_a_case_insensitive_substring_with_either_slash(self):
        p = safepattern.compile_pattern("D:\\payroll")
        self.assertTrue(safepattern.search(p, "type d:/Payroll/jan.csv", plain=True))
        self.assertTrue(safepattern.search(p, "cd D:\\PAYROLL\\x", plain=True))
        self.assertFalse(safepattern.search(p, "D:\\other", plain=True))

    def test_catastrophic_and_bad_patterns_are_refused(self):
        for bad in ["(a+)+", "(.*)*", "(a|aa)+", r"(\w+\s?)*$", "a{1,5000}", r"(a)\1", "[", "(", "", "   ", "x" * 201]:
            with self.assertRaises(safepattern.PatternError, msg=bad):
                safepattern.compile_pattern(bad, regex=True)

    def test_ordinary_regexes_work_and_matching_is_bounded(self):
        p = safepattern.compile_pattern(r"deploy\s+(?:prod|live)", regex=True)
        self.assertTrue(safepattern.search(p, "Deploy   PROD now"))
        self.assertFalse(safepattern.search(p, "x" * 10000 + "deploy prod"))       # beyond the bounded subject

    def test_words_pattern_is_whole_word(self):
        w = safepattern.words_pattern(["Acme", "Project X"])
        self.assertTrue(safepattern.search(w, "email acme about project x"))
        self.assertFalse(safepattern.search(w, "acmeish"))
        with self.assertRaises(safepattern.PatternError):
            safepattern.words_pattern(["", " "])


# ---- assistants ------------------------------------------------------------------------------------------------------

class AssistantsDefaults(Base):
    def test_defaults_route_exactly_like_the_old_constants(self):
        lst = A.load(self.config)
        self.assertEqual([a["id"] for a in lst], ["jarvis", "friday"])
        texts = ["Jarvis, run the tests", "hey Friday what's my day", "run the tests, Jarvis", "Friday evening plans",
                 "Jarvis is great", "hello there", "jarvis", "Friday", "ok friday plan tomorrow", "what's up friday?"]
        for t in texts:
            self.assertEqual(persona.split_persona(t, "Jarvis", lst), persona.split_persona(t), t)

    def test_default_prompts_and_greetings_are_unchanged(self):
        lst = A.load(self.config)
        for who in ("jarvis", "friday"):
            self.assertEqual(persona.persona_prompt(who, "Chief", "Jarvis", lst), persona.persona_prompt(who, "Chief"))
        self.assertIn("You are Friday, the owner's day assistant", persona.persona_prompt("friday", "Chief"))
        self.assertIn("You are Jarvis, the owner's laptop operator", persona.persona_prompt("jarvis", "Chief"))
        clock = lambda: dt.datetime(2026, 11, 9, 9, 0)
        for text in ["hey friday", "good morning jarvis", "Jarvis", "thanks friday", "hello"]:
            self.assertEqual(persona.greeting_reply(text, "", "Chief", "Jarvis", clock, assistants=lst),
                             persona.greeting_reply(text, "", "Chief", "Jarvis", clock), text)

    def test_the_old_jarvis_name_setting_still_works(self):
        self.config.jarvis_name = "Tony"
        lst = A.load(self.config)
        self.assertEqual(lst[0]["name"], "Tony")
        self.assertEqual(persona.split_persona("Tony, open notes", "Tony", lst), ("jarvis", "open notes"))
        self.assertIn("You are Tony", persona.persona_prompt("jarvis", "Chief", "Tony", lst))

    def test_broken_file_falls_back_to_the_defaults(self):
        path = self.config.data_path() / "assistants.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual([a["id"] for a in A.load(self.config)], ["jarvis", "friday"])
        path.write_text(json.dumps({"assistants": [{"id": "x"}]}), encoding="utf-8")
        self.assertEqual([a["id"] for a in A.load(self.config)], ["jarvis", "friday"])


class AssistantsRoutes(Base):
    def setUp(self):
        super().setUp()
        self.c = self.mount(A.make_router(self.ctx))

    def test_routes_need_login(self):
        anon = self.anon()
        self.assertEqual(anon.get("/api/assistants").status_code, 401)
        self.assertEqual(anon.post("/api/assistants", json={}).status_code, 401)
        self.assertEqual(anon.patch("/api/assistants/jarvis", json={}).status_code, 401)
        self.assertEqual(anon.delete("/api/assistants/jarvis").status_code, 401)
        self.assertEqual(anon.post("/api/assistants/reset").status_code, 401)

    def test_get_lists_the_two_defaults_with_their_default_prompt(self):
        d = self.c.get("/api/assistants").json()
        self.assertEqual([a["id"] for a in d["assistants"]], ["jarvis", "friday"])
        self.assertFalse(d["customised"])
        self.assertIn("{name}", d["assistants"][0]["default_prompt"])
        self.assertIn("core", d["tool_groups"])
        self.assertEqual(d["assistants"][0]["words"], ["jarvis", "jarves", "jarvas"])

    def test_rename_add_disable_delete_reset(self):
        r = self.c.patch("/api/assistants/jarvis", json={"name": "Tony", "wake_names": ["tony"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get("/api/assistants").json()["assistants"][0]["name"], "Tony")
        add = self.c.post("/api/assistants", json={"id": "atlas", "name": "Atlas", "wake_names": ["atlas"], "heard_as": ["atlass"],
                                                   "accent": "green", "tool_groups": ["english", "cards" if "cards" in TOOL_GROUPS else "notes"],
                                                   "role_prompt": "You are {name}, the study coach. Call the owner {call}."})
        self.assertEqual(add.status_code, 200, add.text)
        self.assertEqual(len(add.json()["assistants"]), 3)
        self.assertEqual(self.c.patch("/api/assistants/friday", json={"enabled": False}).status_code, 200)
        lst = A.load(self.config)
        self.assertEqual(persona.split_persona("Friday, plan", "Jarvis", lst), ("", "Friday, plan"))       # switched off: no longer a name
        self.assertEqual(persona.split_persona("Atlas, quiz me", "Jarvis", lst), ("atlas", "quiz me"))
        self.assertEqual(self.c.delete("/api/assistants/friday").status_code, 200)
        self.assertEqual([a["id"] for a in A.load(self.config)], ["jarvis", "atlas"])
        r = self.c.post("/api/assistants/reset").json()
        self.assertEqual([a["id"] for a in r["assistants"]], ["jarvis", "friday"])
        self.assertTrue([e for e, _ in self.audits if e == "assistant_add"])

    def test_at_least_one_stays(self):
        self.assertEqual(self.c.delete("/api/assistants/jarvis").status_code, 200)
        self.assertEqual(self.c.delete("/api/assistants/friday").status_code, 400)          # the last one
        self.assertEqual(self.c.patch("/api/assistants/friday", json={"enabled": False}).status_code, 400)
        self.assertEqual(self.c.delete("/api/assistants/nobody").status_code, 404)

    def test_validation(self):
        bad = [{"id": "Bad Id", "name": "X", "wake_names": ["xx"]}, {"id": "ok1", "name": "", "wake_names": ["okay1"]},
               {"id": "ok2", "name": "Ok Two", "wake_names": ["the"]},                 # too common to wake on
               {"id": "ok3", "name": "Ok3", "wake_names": ["friday"]},                 # already Friday's
               {"id": "ok4", "name": "Ok4", "wake_names": ["ok4x"], "accent": "pink"},
               {"id": "ok5", "name": "Ok5", "wake_names": ["ok5x"], "tool_groups": ["nonsense"]},
               {"id": "ok6", "name": "Ok6", "wake_names": ["ok6x"], "voice": {"rate": 9}},
               {"id": "jarvis", "name": "Dup", "wake_names": ["dupx"]}]
        for body in bad:
            self.assertEqual(self.c.post("/api/assistants", json=body).status_code, 400, body)
        self.assertEqual(len(self.c.get("/api/assistants").json()["assistants"]), 2)

    def test_focus_groups_are_always_routed_for_that_assistant(self):
        self.assertEqual(self.c.post("/api/assistants", json={"id": "atlas", "name": "Atlas", "tool_groups": ["english"]}).status_code, 200)
        entry = A.find(A.load(self.config), "atlas")
        self.assertEqual(entry["wake_names"], ["atlas"])                                   # defaulted from the name
        specs = [{"name": n, "description": "", "input_schema": {}} for n in sorted(TOOL_GROUPS["english"][0] | TOOL_GROUPS["core"][0])]
        hist = [{"role": "user", "content": "hello there"}]
        self.assertEqual(route_local_tools(specs, hist), [])
        got = {s["name"] for s in route_local_tools(specs, hist, focus={"english"})}
        self.assertEqual(got, TOOL_GROUPS["english"][0])
        text = persona.persona_prompt("atlas", "Chief", "Jarvis", A.load(self.config))
        self.assertIn("You are Atlas", text)
        self.assertIn("english", text)


class WakeMatching(Base):
    def lst(self):
        return A.clean_list([*A.defaults(), {"id": "atlas", "name": "Atlas", "wake_names": ["atlas"], "heard_as": ["atlass"]}])

    def test_python_wake_uses_the_list(self):
        lst = self.lst()
        self.assertEqual(wakeword.match_wake("hey atlas quiz me on verbs", "Jarvis", lst), ("atlas", "quiz me on verbs"))
        self.assertEqual(wakeword.match_wake("hey atlass quiz me", "Jarvis", lst), ("atlas", "quiz me"))      # mis-hearing
        self.assertEqual(wakeword.match_wake("hey jarves open notes", "Jarvis", lst), ("jarvis", "open notes"))
        self.assertEqual(wakeword.match_wake("hi friday what's my day", "Jarvis", lst), ("friday", "what's my day"))
        self.assertIsNone(wakeword.match_wake("atlas is a good name", "Jarvis", lst))
        self.assertIsNone(wakeword.match_wake("hey atlas evening", "Jarvis", [a for a in lst if a["id"] != "atlas"]))
        self.assertIsNone(wakeword.match_wake("hey tony go", "Jarvis", lst))

    def test_without_a_list_the_old_rules_apply(self):
        self.assertEqual(wakeword.match_wake("hey jarves what's up"), ("jarvis", "what's up"))
        self.assertIsNone(wakeword.match_wake("hey atlas go"))

    def test_disabled_assistant_does_not_wake(self):
        lst = A.clean_list([*A.defaults(), {"id": "atlas", "name": "Atlas", "wake_names": ["atlas"], "enabled": False}])
        self.assertIsNone(wakeword.match_wake("hey atlas go", "Jarvis", lst))

    def test_listener_reads_the_saved_list(self):
        from tests.test_wakeword import Rig
        r = Rig("Hey Atlas, quiz me")
        A.save(r.config, self.lst())
        r.utter()
        self.assertEqual(r.asked, [("quiz me", "atlas")])
        self.assertEqual(r.chimes, ["atlas"])
        r2 = Rig("Hey Atlas, quiz me")                                   # no saved list: the old behaviour
        r2.utter()
        self.assertEqual(r2.asked, [])

    @unittest.skipUnless(NODE, "node is not installed")
    def test_browser_matcher_uses_the_list(self):
        script = """
import { matchWake } from %s;
import { splitName } from %s;
const lst = JSON.parse(process.argv[1]);
const out = [
  matchWake("hey atlas quiz me", { assistants: lst }), matchWake("hey atlass quiz me", { assistants: lst }),
  matchWake("hey jarves go", { assistants: lst }), matchWake("hey friday", { assistants: lst }),
  matchWake("atlas is a name", { assistants: lst }), matchWake("hey atlas go"), matchWake("hey tony go", { assistants: lst }),
  matchWake("hey atlas go", { assistants: lst.map((a) => ({ ...a, enabled: a.id !== "atlas" })) }),
  matchWake("hey jarvis do it"), matchWake("hey tony do it", { jarvisName: "Tony" }),
  splitName("Atlas, quiz me", "Jarvis", lst), splitName("Atlas, quiz me"), splitName("Friday, my day?", "Jarvis", lst),
];
console.log(JSON.stringify(out));
""" % (json.dumps((STATIC / "js" / "wakephrase.js").as_uri()), json.dumps((STATIC / "js" / "speechchunk.js").as_uri()))
        out = subprocess.run([NODE, "--input-type=module", "-e", script, json.dumps(self.lst())], capture_output=True, text=True,
                             timeout=30, check=True)
        got = json.loads(out.stdout)
        self.assertEqual(got[0], {"persona": "atlas", "rest": "quiz me"})
        self.assertEqual(got[1], {"persona": "atlas", "rest": "quiz me"})
        self.assertEqual(got[2], {"persona": "jarvis", "rest": "go"})
        self.assertEqual(got[3], {"persona": "friday", "rest": ""})
        self.assertIsNone(got[4])
        self.assertIsNone(got[5])                                   # no list: Jarvis and Friday only
        self.assertIsNone(got[6])
        self.assertIsNone(got[7])                                   # switched off
        self.assertEqual(got[8], {"persona": "jarvis", "rest": "do it"})
        self.assertEqual(got[9], {"persona": "jarvis", "rest": "do it"})
        self.assertEqual(got[10], {"persona": "atlas", "text": "quiz me"})
        self.assertEqual(got[11], {"persona": "", "text": "Atlas, quiz me"})
        self.assertEqual(got[12], {"persona": "friday", "text": "my day?"})


# ---- notification preferences ----------------------------------------------------------------------------------------

class NotifyPrefs(Base):
    def setUp(self):
        super().setUp()
        self.c = self.mount(proactive.make_router(self.ctx, clock=lambda: NOON))
        self.engine = self.ctx.proactive

    def test_defaults_equal_todays_behaviour(self):
        for event, want in [({"type": "reminder"}, "now"), ({"type": "autopilot_ready"}, "batch"), ({"type": "weather"}, "quiet"),
                            ({"type": "meeting", "minutes": 5}, "now"), ({"type": "alert", "urgency": 1}, "batch")]:
            self.assertEqual(proactive.classify(event, prefs={}), proactive.classify(event), event)
            self.assertEqual(proactive.classify(event), want)

    def test_prefs_route_now_batch_quiet_and_off(self):
        p = {"reminder": "batch", "autopilot_ready": "now", "backup_done": "quiet-only", "research_done": "off"}
        self.assertEqual(proactive.classify({"type": "reminder"}, prefs=p), "batch")
        self.assertEqual(proactive.classify({"type": "autopilot_ready"}, prefs=p), "now")
        self.assertEqual(proactive.classify({"type": "backup_done"}, prefs=p), "quiet")
        self.assertEqual(proactive.classify({"type": "research_done"}, prefs=p), "off")
        self.assertEqual(proactive.classify({"type": "research_done"}, now=NIGHT, prefs=p), "off")
        self.assertEqual(proactive.classify({"type": "autopilot_ready"}, now=NIGHT, prefs=p), "quiet")     # quiet hours still hold 'now'
        self.assertEqual(proactive.classify({"type": "mail"}, prefs={"mail": "now"}), "now")              # overrides the urgent check

    def test_engine_follows_the_saved_choices(self):
        r = self.c.put("/api/proactive/prefs", json={"prefs": {"reminder": "off", "autopilot_ready": "now", "backup_done": "quiet-only"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.engine.submit({"type": "reminder", "title": "Call"}), "off")
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.engine.load()["events"], [])                      # dropped, not even stored
        self.assertEqual(self.engine.submit({"type": "autopilot_ready", "title": "x"}), "now")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.engine.submit({"type": "backup_done", "title": "x"}), "quiet")
        self.assertEqual(len(self.pushed), 1)
        self.c.put("/api/proactive/prefs", json={"prefs": {"reminder": "default"}})
        self.assertEqual(self.engine.submit({"type": "reminder", "title": "Call"}), "now")

    def test_settings_lists_every_known_type_with_its_default(self):
        d = self.c.get("/api/proactive/prefs").json()
        by = {t["type"]: t for t in d["types"]}
        for t in ("reminder", "autopilot_ready", "applyq_ready", "applyq_daily", "post_comment", "post_cadence", "alert", "meeting",
                  "backup_done", "cc_waiting", "morning"):
            self.assertIn(t, by, t)
        self.assertEqual(by["reminder"]["default"], "now")
        self.assertEqual(by["applyq_ready"]["default"], "batch")
        self.assertEqual(by["alert"]["default"], "auto")
        self.assertEqual(by["reminder"]["value"], "default")
        self.assertEqual(d["channels"], ["now", "batch", "quiet-only", "off"])
        self.assertEqual(d["quiet"], {"start": "22:00", "end": "07:00"})

    def test_register_type_adds_a_row_and_is_guarded(self):
        self.assertTrue(proactive.register_type("weather_alert", "Weather warnings", "batch"))
        by = {t["type"]: t for t in self.c.get("/api/proactive/prefs").json()["types"]}
        self.assertEqual(by["weather_alert"]["label"], "Weather warnings")
        self.assertEqual(by["weather_alert"]["default"], "batch")
        self.assertFalse(proactive.register_type("Bad Name!", "x"))
        self.assertFalse(proactive.register_type(None))                           # never raises
        self.assertTrue(proactive.register_type("another_one", "Another", "nonsense"))
        self.assertEqual(proactive.known_types()["another_one"]["default"], "quiet-only")

    def test_an_unregistered_type_shows_up_after_its_first_event(self):
        self.engine.submit({"type": "gizmo_ready", "title": "x"})
        by = {t["type"]: t for t in self.c.get("/api/proactive/prefs").json()["types"]}
        self.assertEqual(by["gizmo_ready"]["label"], "Gizmo ready")
        self.c.put("/api/proactive/prefs", json={"prefs": {"gizmo_ready": "batch"}})
        self.assertEqual(self.engine.submit({"type": "gizmo_ready", "title": "A gizmo is ready"}), "batch")

    def test_batched_unknown_types_get_a_generic_sentence(self):
        text = proactive.batch_sentence([{"type": "weather_alert", "title": "Storm tonight", "count": 1}], "Chief")
        self.assertEqual(text, "Chief, storm tonight.")
        text = proactive.batch_sentence([{"type": "reminder", "title": "a", "count": 1}, {"type": "reminder", "title": "b", "count": 1}], "Chief")
        self.assertIn("2 reminders that are due updates are waiting", text)

    def test_bad_input_and_reset(self):
        self.assertEqual(self.c.put("/api/proactive/prefs", json={"prefs": {"reminder": "loudly"}}).status_code, 400)
        self.assertEqual(self.c.put("/api/proactive/prefs", json={"prefs": {"Bad Type": "now"}}).status_code, 400)
        self.assertEqual(self.c.put("/api/proactive/prefs", json={"prefs": []}).status_code, 400)
        self.c.put("/api/proactive/prefs", json={"prefs": {"reminder": "off"}})
        self.assertEqual(self.engine.prefs(), {"reminder": "off"})
        self.c.post("/api/proactive/prefs/reset")
        self.assertEqual(self.engine.prefs(), {})

    def test_routes_need_login(self):
        anon = self.anon()
        self.assertEqual(anon.get("/api/proactive/prefs").status_code, 401)
        self.assertEqual(anon.put("/api/proactive/prefs", json={"prefs": {}}).status_code, 401)
        self.assertEqual(anon.post("/api/proactive/prefs/reset").status_code, 401)


# ---- custom safety rules ---------------------------------------------------------------------------------------------

class SafetyRules(Base):
    def setUp(self):
        super().setUp()
        self.c = self.mount(rules.make_router(self.ctx))

    def put(self, rule_list):
        return self.c.put("/api/safety/rules", json={"rules": rule_list})

    def test_a_path_rule_raises_commands_and_file_reads_to_critical(self):
        self.assertEqual(safety.classify("run_command", {"command": "type D:\\payroll\\jan.csv"}), "N2")
        self.assertEqual(self.put([{"pattern": "D:\\payroll", "applies_to": "path", "level": "N3", "note": "payroll"}]).status_code, 200)
        safety.refresh_custom(self.config)
        self.assertEqual(safety.classify_with_custom("run_command", {"command": "type d:/PAYROLL/jan.csv"})[0], "N3")
        self.assertEqual(safety.classify_with_custom("read_file", {"path": "D:\\payroll\\jan.csv"})[0], "N3")
        self.assertEqual(safety.classify_with_custom("read_file", {"path": "D:\\notes\\a.txt"})[0], "N1")

    def test_rules_only_raise_never_lower(self):
        self.put([{"pattern": "shutdown", "applies_to": "command", "level": "N2"}])
        safety.refresh_custom(self.config)
        level, hits = safety.classify_with_custom("run_command", {"command": "shutdown /s /t 0"})
        self.assertEqual(level, "N3")                                            # the built-in critical level stays
        self.assertEqual(safety.classify_with_custom("list_dir", {"path": "."})[0], "N1")
        self.assertEqual(safety.apply_custom("N3", "x", {})[0], "N3")
        self.assertEqual(self.put([{"pattern": "x", "level": "N1"}]).status_code, 400)       # a rule cannot lower or equal N1
        self.assertEqual(self.put([{"pattern": "x", "level": "n0"}]).status_code, 400)

    def test_tool_and_command_rules(self):
        self.put([{"pattern": "^send_", "regex": True, "applies_to": "tool", "level": "N2"},
                  {"pattern": "deploy", "applies_to": "command", "level": "N3", "note": "deploys"}])
        safety.refresh_custom(self.config)
        self.assertEqual(safety.classify_with_custom("send_email", {})[0], "N2")
        self.assertEqual(safety.classify_with_custom("read_file", {"path": "send_x"})[0], "N1")      # a tool rule looks at the tool name only
        self.assertEqual(safety.classify_with_custom("run_command", {"command": "make DEPLOY prod"})[0], "N3")
        self.assertEqual(safety.classify_with_custom("run_command", {"command": "make build"})[0], "N2")

    def test_disabled_rules_do_nothing_and_ids_are_assigned(self):
        d = self.put([{"pattern": "deploy", "level": "N3", "enabled": False}, {"pattern": "other", "level": "N2"}]).json()
        self.assertEqual([r["id"] for r in d["rules"]], ["r1", "r2"])
        safety.refresh_custom(self.config)
        self.assertEqual(safety.classify_with_custom("run_command", {"command": "make deploy"})[0], "N2")

    def test_bad_and_catastrophic_regexes_are_rejected(self):
        for bad in ["(a+)+$", "(.*)*x", "(a|aa)+", "[unclosed", "x" * 300, ""]:
            r = self.put([{"pattern": bad, "regex": True, "applies_to": "command", "level": "N3"}])
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.put([{"pattern": "a", "applies_to": "network", "level": "N3"}]).status_code, 400)
        self.assertEqual(self.put([{"pattern": str(i), "level": "N2"} for i in range(51)]).status_code, 400)
        self.assertEqual(self.put("nope").status_code, 400)
        self.assertEqual(self.c.get("/api/safety/rules").json()["rules"], [])               # nothing was saved

    def test_test_box_shows_the_level_and_the_rule(self):
        self.put([{"pattern": "payroll", "applies_to": "command", "level": "N3", "note": "n"}])
        r = self.c.post("/api/safety/rules/test", json={"kind": "command", "text": "dir D:\\Payroll"}).json()
        self.assertEqual((r["level"], r["built_in"]), ("N3", "N2"))
        self.assertEqual(r["rules"][0]["pattern"], "payroll")
        r = self.c.post("/api/safety/rules/test", json={"kind": "command", "text": "dir ."}).json()
        self.assertEqual((r["level"], r["rules"]), ("N2", []))
        r = self.c.post("/api/safety/rules/test", json={"kind": "command", "text": "shutdown /s"}).json()
        self.assertEqual(r["level"], "N3")
        r = self.c.post("/api/safety/rules/test", json={"kind": "command", "text": "dir .", "rules": [{"pattern": "dir", "level": "N3"}]}).json()
        self.assertEqual(r["level"], "N3")                                                  # an unsaved draft can be tried first
        self.assertEqual(self.c.post("/api/safety/rules/test", json={"text": 5}).status_code, 400)
        self.assertEqual(self.c.post("/api/safety/rules/test", json={"text": "x", "rules": [{"pattern": "(a+)+", "regex": True}]}).status_code, 400)

    def test_saving_is_audited_without_patterns_and_survives_a_restart(self):
        self.put([{"pattern": "secret-project-name", "level": "N3"}])
        event, fields = self.audits[-1]
        self.assertEqual(event, "safety_rules_update")
        self.assertNotIn("secret-project-name", json.dumps(fields))
        safety.set_custom_rules([])
        safety._CUSTOM_STAMP[0] = None
        safety.refresh_custom(self.config)                                                  # reloaded from the file
        self.assertEqual(len(safety.custom_rules_live()), 1)

    def test_the_assistant_level_uses_the_rules_and_audits_when_one_fires(self):
        import asyncio
        from hub.assistant import AssistantService, _Session
        audits = []
        svc = AssistantService(self.config, audit=lambda e, **k: audits.append((e, k)))
        self.put([{"pattern": "payroll", "applies_to": "path", "level": "N3", "note": "payroll is critical"}])
        self.assertEqual(svc._level("read_file", {"path": "D:\\payroll\\a.csv"}), "N3")
        self.assertEqual(svc._level("read_file", {"path": "D:\\other\\a.csv"}), "N1")

        async def run():
            svc._sessions["s"] = _Session()
            events, holder = [], []
            async for ev in svc._call_tool("s", "c1", "read_file", {"path": "D:\\payroll\\a.csv"}, False, holder):
                events.append(ev)
                if ev.get("type") == "approval_needed":
                    svc.resolve("s", "c1", False)
            return events
        events = asyncio.run(run())
        card = next(e for e in events if e["type"] == "approval_needed")
        self.assertEqual((card["level"], card["critical"]), ("N3", True))
        self.assertIn("payroll is critical", card["reason"])
        fired = [f for e, f in audits if e == "assistant_custom_rule"]
        self.assertEqual(fired[0]["rules"], ["r1"])
        self.assertNotIn("a.csv", json.dumps(fired))                                   # ids only, never the path

    def test_routes_need_login(self):
        anon = self.anon()
        self.assertEqual(anon.get("/api/safety/rules").status_code, 401)
        self.assertEqual(anon.put("/api/safety/rules", json={"rules": []}).status_code, 401)
        self.assertEqual(anon.post("/api/safety/rules/test", json={"text": "x"}).status_code, 401)


# ---- custom redaction rules ------------------------------------------------------------------------------------------

class RedactRules(Base):
    def setUp(self):
        super().setUp()
        self.c = self.mount(rules.make_router(self.ctx))

    def put(self, rule_list):
        return self.c.put("/api/redact/rules", json={"rules": rule_list})

    def test_words_and_patterns_are_hidden_and_restored(self):
        r = self.put([{"label": "Clients", "pattern": "Acme Corp, Globex", "kind": "CLIENT"},
                      {"label": "Ticket", "pattern": r"TCK-\d{4}", "regex": True, "kind": "TICKET"}])
        self.assertEqual(r.status_code, 200, r.text)
        rd = redact.make(self.config)
        out = rd.redact("Send acme corp the file for TCK-1234, then ping globex. Mail a@b.co too.")
        self.assertIn("[CLIENT_1]", out)
        self.assertIn("[CLIENT_2]", out)
        self.assertIn("[TICKET_1]", out)
        self.assertIn("[EMAIL_1]", out)                                           # the built-in kinds still work
        self.assertNotIn("Acme", out)
        self.assertNotIn("TCK-1234", out)
        self.assertEqual(rd.restore(out), "Send acme corp the file for TCK-1234, then ping globex. Mail a@b.co too.")
        self.assertEqual(rd.summary(), {"client": 2, "ticket": 1, "email": 1})

    def test_preview_route_covers_custom_rules(self):
        from hub import privacy
        self.app.include_router(privacy.make_router(self.ctx))
        self.put([{"label": "Clients", "pattern": "Acme", "kind": "CLIENT"}])
        client = TestClient(self.app, cookies={"hub_session": "yes"})
        r = client.post("/api/redact/preview", json={"text": "call Acme"}).json()
        self.assertEqual((r["redacted"], r["counts"]), ("call [CLIENT_1]", {"client": 1}))
        d = self.c.post("/api/redact/rules/test", json={"text": "call Zeta", "rules": [{"label": "Z", "pattern": "Zeta", "kind": "ZZ"}]}).json()
        self.assertEqual(d["redacted"], "call [ZZ_1]")
        self.assertEqual(self.c.post("/api/redact/rules/test", json={"text": "x", "rules": [{"label": "Z", "pattern": "(a+)+", "regex": True}]}).status_code, 400)

    def test_validation(self):
        bad = [{"label": "", "pattern": "x"}, {"label": "a", "pattern": "", "kind": "AB"}, {"label": "a", "pattern": "word", "kind": "EMAIL"},
               {"label": "a", "pattern": "word", "kind": "1ab"}, {"label": "a", "pattern": "(x*)*", "regex": True, "kind": "AB"},
               {"label": "a", "pattern": "[", "regex": True, "kind": "AB"}, {"label": "a", "pattern": "w" * 700, "kind": "AB"}]
        for rule in bad:
            self.assertEqual(self.put([rule]).status_code, 400, rule)
        self.assertEqual(self.put([{"label": "x", "pattern": "word", "kind": "AB"}] * 51).status_code, 400)
        self.assertEqual(self.c.get("/api/redact/rules").json()["rules"], [])

    def test_a_label_makes_the_default_placeholder_and_off_means_off(self):
        d = self.put([{"label": "Project Falcon", "pattern": "falcon", "enabled": True}]).json()
        self.assertEqual(d["rules"][0]["kind"], "PROJECTFALCO")
        self.put([{"label": "Project Falcon", "pattern": "falcon", "enabled": False}])
        self.assertEqual(redact.make(self.config).redact("falcon"), "falcon")

    def test_audit_is_counts_only(self):
        self.put([{"label": "Clients", "pattern": "Acme", "kind": "CLIENT"}])
        self.assertEqual(self.audits[-1], ("redact_rules_update", {"count": 1}))
        redact.begin_turn(self.config)
        redact.current().redact("hello Acme")
        redact.end_turn(lambda e, **k: self.audits.append((e, k)))
        event, fields = self.audits[-1]
        self.assertEqual((event, fields["counts"]), ("redaction", {"client": 1}))
        self.assertNotIn("Acme", json.dumps(self.audits))

    def test_streamed_placeholders_of_custom_kinds_are_restored(self):
        self.put([{"label": "Clients", "pattern": "Acme", "kind": "CLIENT"}])
        rd = redact.make(self.config)
        rd.redact("Acme")
        sr = rd.restorer()
        self.assertEqual(sr.feed("Hello [CLI") + sr.feed("ENT_1], ok") + sr.finish(), "Hello Acme, ok")

    def test_routes_need_login(self):
        anon = self.anon()
        self.assertEqual(anon.get("/api/redact/rules").status_code, 401)
        self.assertEqual(anon.put("/api/redact/rules", json={"rules": []}).status_code, 401)
        self.assertEqual(anon.post("/api/redact/rules/test", json={"text": "x"}).status_code, 401)


# ---- memory kinds and tags -------------------------------------------------------------------------------------------

class MemoryKindsAndTags(Base):
    def setUp(self):
        super().setUp()
        self.brain = brain.Brain(self.ctx, clock=lambda: NOW)
        self.app.include_router(brain.make_router(self.ctx, clock=lambda: NOW))
        self.app.include_router(A.make_router(self.ctx))
        self.c = self.mount()

    def test_old_items_survive_and_the_default_kinds_are_the_six(self):
        old = self.brain.store.add({"kind": "rca", "title": "Old RCA", "body": "b", "tags": ["login", "proxy"], "who": "", "project": "",
                                    "persona": "jarvis", "created": "2026-01-01T10:00:00", "remind_at": None, "reminder_id": None,
                                    "status": "open", "source": "app", "meta": ""})
        d = self.c.get("/api/brain/kinds").json()
        self.assertEqual([k["id"] for k in d["kinds"]], list(brain.KINDS))
        self.assertEqual({k["id"]: k["persona"] for k in d["kinds"]}["note"], "both")
        self.assertEqual({k["id"]: k["count"] for k in d["kinds"]}["rca"], 1)
        self.assertEqual(self.c.get("/api/brain/items?tag=proxy").json()["items"][0]["id"], old["id"])

    def test_tags_are_free_form_many_and_filterable(self):
        a = self.brain.add(text="Payroll run moved to Friday", tags="Payroll, Q4 ;urgent, payroll")
        b = self.brain.add(text="Payroll audit notes", tags=["payroll", "audit"])
        self.brain.add(text="Lunch menu", tags=[])
        self.assertEqual(a["tags"], ["payroll", "q4", "urgent"])
        names = lambda q: sorted(i["title"] for i in self.c.get("/api/brain/items?status=all&" + q).json()["items"])
        self.assertEqual(len(names("tag=payroll")), 2)
        self.assertEqual(names("tag=payroll,urgent"), [a["title"]])                  # every tag listed must match
        self.assertEqual(names("tag=nothing"), [])
        self.assertEqual(len(names("tag=payroll&q=audit")), 1)
        counts = {t["tag"]: t["count"] for t in self.c.get("/api/brain/tags").json()["tags"]}
        self.assertEqual((counts["payroll"], counts["audit"]), (2, 1))
        many = self.brain.add(text="many", tags=[f"t{i}" for i in range(30)])
        self.assertEqual(len(many["tags"]), brain.MAX_TAGS)
        r = self.c.patch(f"/api/brain/items/{b['id']}", json={"add_tags": ["Done", "audit"], "remove_tags": ["payroll"]}).json()
        self.assertEqual(r["tags"], ["audit", "done"])
        r = self.c.patch(f"/api/brain/items/{b['id']}", json={"tags": "x, y"}).json()
        self.assertEqual(r["tags"], ["x", "y"])

    def test_recall_tool_filters_by_tags_and_remember_takes_tags(self):
        a = self.brain.add(text="Payroll run moved", tags=["payroll"])
        self.brain.add(text="Payroll audit", tags=["audit"])
        tools = {t.spec["name"]: t for t in brain_tools.build(lambda: self.app, self.config)}
        self.assertIn("tags", tools["recall"].spec["input_schema"]["properties"])
        self.assertIn("tags", tools["remember"].spec["input_schema"]["properties"])
        self.assertIn("tags", tools["task_update"].spec["input_schema"]["properties"])
        # the route the tool calls (the tool goes through InProcessClient, which needs the full app)
        found = self.c.get("/api/brain/items?status=all&q=payroll&tag=payroll").json()["items"]
        self.assertEqual([i["id"] for i in found], [a["id"]])

    def test_custom_kinds_with_a_default_persona(self):
        r = self.c.put("/api/brain/kinds", json={"kinds": [{"id": "note", "label": "Note", "persona": "both"},
                                                          {"id": "idea", "label": "Idea", "persona": "friday"},
                                                          {"id": "rca", "label": "RCA", "persona": "friday"}]})
        self.assertEqual(r.status_code, 200, r.text)
        idea = self.c.post("/api/brain/items", json={"text": "A new idea", "kind": "idea"}).json()
        self.assertEqual(idea["persona"], "friday")
        rca = self.c.post("/api/brain/items", json={"text": "RCA of the outage", "kind": "rca"}).json()
        self.assertEqual(rca["persona"], "friday")                                   # the owner moved RCA to Friday
        self.assertEqual(self.c.post("/api/brain/items", json={"text": "x", "kind": "fact"}).status_code, 400)       # removed kind
        self.assertEqual(self.c.get("/api/brain/items?kind=idea").json()["count"], 1)
        self.assertEqual(self.c.get("/api/brain/items?kind=fact").status_code, 400)
        # a kind in use cannot be dropped, and reset refuses while custom kinds are in use
        gone = self.c.put("/api/brain/kinds", json={"kinds": [{"id": "note", "label": "Note", "persona": "both"}]})
        self.assertEqual(gone.status_code, 400)
        self.assertIn("item", gone.json()["detail"])
        self.assertEqual(self.c.post("/api/brain/kinds/reset").status_code, 400)
        self.c.delete(f"/api/brain/items/{idea['id']}")
        self.c.delete(f"/api/brain/items/{rca['id']}")
        self.assertEqual(self.c.post("/api/brain/kinds/reset").status_code, 200)
        self.assertEqual([k["id"] for k in self.c.get("/api/brain/kinds").json()["kinds"]], list(brain.KINDS))

    def test_kind_validation_and_persona_must_exist(self):
        bad = [[], [{"id": "A b", "label": "x"}], [{"id": "dup", "label": "a"}, {"id": "dup", "label": "b"}],
               [{"id": "ok1", "label": "a", "persona": "nobody"}], "nope"]
        for kinds in bad:
            self.assertEqual(self.c.put("/api/brain/kinds", json={"kinds": kinds}).status_code, 400, kinds)
        self.assertEqual(self.c.put("/api/brain/kinds", json={"kinds": [{"label": "Big Idea", "persona": "both"}]}).json()["kinds"][0]["id"], "big-idea")

    def test_a_new_assistant_can_be_a_persona_filter_and_kind_default(self):
        self.c.post("/api/assistants", json={"id": "atlas", "name": "Atlas"})
        self.c.put("/api/brain/kinds", json={"kinds": [{"id": "note", "label": "Note", "persona": "both"}, {"id": "study", "label": "Study", "persona": "atlas"}]})
        item = self.c.post("/api/brain/items", json={"text": "Verbs", "kind": "study"}).json()
        self.assertEqual(item["persona"], "atlas")
        self.assertEqual(self.c.get("/api/brain/items?persona=atlas").json()["count"], 1)
        self.assertEqual(self.c.get("/api/brain/items?persona=jarvis").json()["count"], 0)
        self.assertEqual(self.c.get("/api/brain/items?persona=ghost").status_code, 400)

    def test_the_ready_made_persona_rules_are_unchanged(self):
        self.assertEqual(brain.persona_for("rca"), "jarvis")
        self.assertEqual(brain.persona_for("task", "Ravi"), "friday")
        self.assertEqual(brain.persona_for("note", has_reminder=True), "friday")
        kinds = brain.default_kinds()
        self.assertEqual(brain.persona_for("task", "Ravi", kinds=kinds), "friday")
        self.assertEqual(brain.persona_for("note", has_reminder=True, kinds=kinds), "friday")

    def test_routes_need_login(self):
        anon = self.anon()
        for method, path in (("get", "/api/brain/tags"), ("get", "/api/brain/kinds"), ("put", "/api/brain/kinds"),
                             ("post", "/api/brain/kinds/reset")):
            self.assertEqual(getattr(anon, method)(path).status_code, 401, path)


class Misc(unittest.TestCase):
    def test_new_files_are_in_the_service_worker_shell(self):
        sw = (STATIC / "sw.js").read_text(encoding="utf-8")
        for name in ("/js/listeditor.js", "/js/ownerlists.js", "/js/assistants.js"):
            self.assertIn(name, sw)
            self.assertTrue((STATIC / name.lstrip("/")).exists())

    def test_list_editor_never_uses_innerhtml(self):
        for name in ("listeditor.js", "ownerlists.js", "assistants.js"):
            self.assertNotIn("innerHTML", (STATIC / "js" / name).read_text(encoding="utf-8"), name)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_node_check(self):
        for name in ("listeditor.js", "ownerlists.js", "assistants.js", "brain.js", "settings.js", "speechchunk.js", "voicemode.js"):
            src = (STATIC / "js" / name).read_text(encoding="utf-8")
            r = subprocess.run([NODE, "--input-type=module", "--check"], input=src, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
