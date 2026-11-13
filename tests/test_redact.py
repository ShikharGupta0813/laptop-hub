"""Privacy redaction: every kind, round trips, local untouched, collisions, speed, counts-only audit. Fakes only."""
import json
import os
import shutil
import time
import unittest

from fastapi.testclient import TestClient

from hub import assistant as A
from hub import redact
from hub.devserver import FakeTranscriber
from hub.redact import Guarded, Redactor, verhoeff_ok
from hub.server import Deps, create_app
from tests.fakes_assistant import FakeOllama, ollama_text, reply, tool_block
from tests.helpers import TOKEN, make_config
from tests.test_assistant import Base, collect, of

VALID_CARD = "4111 1111 1111 1111"
VALID_AADHAAR = "2345 6789 0124"      # 4-4-4 spacing pattern


def verhoeff_number():
    """A 12-digit number whose Verhoeff check digit is right."""
    for n in range(23456789000, 23456789000 + 5000):
        s = "2" + str(n)[1:].rjust(11, "0")
        if len(s) == 12 and verhoeff_ok(s):
            return s
    raise AssertionError("none found")


def flat(obj):
    return repr(obj)


class Kinds(unittest.TestCase):
    def hides(self, text, kind, original=None):
        rd = Redactor()
        out = rd.redact(text)
        self.assertIn(f"[{kind.upper()}_1]", out, out)
        self.assertNotIn(original or text.strip(), out)
        self.assertEqual(rd.restore(out), text)
        return out

    def keeps(self, text):
        rd = Redactor()
        self.assertEqual(rd.redact(text), text)
        self.assertEqual(rd.summary(), {})

    def test_email(self):
        self.hides("write to bob.smith+x@example.co.in now", "email", "bob.smith+x@example.co.in")
        self.keeps("the @mention and user@ alone, or a@b are not emails")

    def test_phone(self):
        for number in ("+91 98765 43210", "+91-9876543210", "9876543210", "98765 43210", "+1 415 555 2671",
                       "+44 20 7946 0958"):
            self.hides(f"call {number} today", "phone", number)
        for text in ("version 3.11.2 build 20241113", "order 1234567 shipped", "epoch 1731500000",
                     "0.000123456789", "port 8765 and 8080", "3.14159265358979"):
            self.keeps(text)

    def test_aadhaar(self):
        self.hides(f"my aadhaar is {VALID_AADHAAR}.", "aadhaar", VALID_AADHAAR)
        good = verhoeff_number()
        self.hides(f"id {good} ok", "aadhaar", good)
        bad = good[:-1] + str((int(good[-1]) + 1) % 10)
        self.keeps(f"tracking {bad} maybe")             # 12 plain digits that fail Verhoeff
        self.keeps("1234 5678 9012")                    # cannot start with 0 or 1

    def test_pan(self):
        self.hides("PAN ABCPE1234F is mine", "pan", "ABCPE1234F")
        self.keeps("ABCDE1234F is not a PAN class; HELLO12345 neither")

    def test_card(self):
        self.hides(f"card {VALID_CARD} exp 12/29", "card", VALID_CARD)
        self.hides("pay 4111111111111111 now", "card", "4111111111111111")
        self.hides("amex 3782-822463-10005 ok", "card", "3782-822463-10005")
        self.keeps("card 4111 1111 1111 1112")          # fails Luhn
        self.keeps("0000000000000000")

    def test_iban(self):
        self.hides("send to GB82 WEST 1234 5698 7654 32 please", "iban", "GB82 WEST 1234 5698 7654 32")
        self.hides("DE89370400440532013000", "iban")
        self.keeps("GB82 WEST 1234 5698 7654 33")       # fails mod 97
        self.keeps("AB12 CDEF is a code")

    def test_api_keys(self):
        for key in ("sk-abcdefghijklmnopqrstuvwx", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv", "AIza" + "B" * 35,
                    "ghp_" + "a1B2" * 10, "AKIA" + "ABCDEFGHIJKLMNOP", "xoxb-1234567890-abcdefghij",
                    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"):
            self.hides(f"use {key} here", "key", key)
        self.hides("api_key: a1b2c3d4e5f6a7b8c9d0e1f2", "key", "a1b2c3d4e5f6a7b8c9d0e1f2")
        self.hides("the token = Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZg==", "key", "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZg==")
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
        out = self.hides(f"here:\n{block}\nbye", "key", "MIIEow")
        self.assertIn("bye", out)
        self.keeps("sk-short and the key is under the mat, token: abc")
        self.keeps("keyboard: aaaaaaaaaaaaaaaaaaaaaaaa")  # no digit: not secret-looking

    def test_password(self):
        out = self.hides("login ok, password: hunter2!x", "password", "hunter2!x")
        self.assertIn("password: [PASSWORD_1]", out)
        self.hides("my pwd is S3cr3t#9", "password", "S3cr3t#9")
        self.keeps("password reset email sent; passwords differ")

    def test_names_are_owner_words(self):
        rd = Redactor(names=["Acme Corp", "Zed"])
        out = rd.redact("I work at acme corp, and Zed's team; zedlike stays. Acme Corp again")
        self.assertEqual(out, "I work at [NAME_1], and [NAME_2]'s team; zedlike stays. [NAME_1] again")
        self.assertIn("acme corp", rd.restore(out))

    def test_kinds_switch(self):
        rd = Redactor(kinds=["email"])
        self.assertEqual(rd.redact(f"a@b.com {VALID_CARD}"), f"[EMAIL_1] {VALID_CARD}")
        self.assertEqual(Redactor(kinds=[]).redact("a@b.com"), "a@b.com")

    def test_stable_placeholders_and_counts(self):
        rd = Redactor()
        out = rd.redact("a@x.com b@x.com A@X.com a@x.com")
        self.assertEqual(out, "[EMAIL_1] [EMAIL_2] [EMAIL_1] [EMAIL_1]")
        self.assertEqual(rd.summary(), {"email": 4})
        self.assertEqual(rd.redact("again a@x.com"), "again [EMAIL_1]")

    def test_no_double_redaction_of_placeholders(self):
        rd = Redactor()
        once = rd.redact(f"a@x.com {VALID_CARD}")
        self.assertEqual(rd.redact(once), once)

    def test_literal_placeholder_in_input_is_not_reused(self):
        rd = Redactor()
        out = rd.redact("the doc says [EMAIL_1] and mail me at me@x.com")
        self.assertIn("[EMAIL_2]", out)
        self.assertEqual(rd.restore(out), "the doc says [EMAIL_1] and mail me at me@x.com")
        # a model reply mentioning the literal token is left alone, the allocated one is restored
        self.assertEqual(rd.restore("[EMAIL_1] vs [EMAIL_2]"), "[EMAIL_1] vs me@x.com")

    def test_restore_is_case_tolerant_and_ignores_unknown(self):
        rd = Redactor()
        rd.redact("me@x.com")
        self.assertEqual(rd.restore("[email_1] [EMAIL_9] [CARD_1]"), "me@x.com [EMAIL_9] [CARD_1]")

    def test_json_restore_escapes(self):
        rd = Redactor()
        rd.redact('password: pa\\ss12')
        text = rd.restore('{"p": "[PASSWORD_1]"}', json_safe=True)
        self.assertEqual(json.loads(text)["p"], "pa\\ss12")

    def test_redact_obj_and_restore_obj(self):
        rd = Redactor()
        obj = {"to": ["a@x.com"], "n": 3, "nested": {"c": VALID_CARD}}
        red = rd.redact_obj(obj)
        self.assertNotIn("a@x.com", flat(red))
        self.assertEqual(rd.restore_obj(red), obj)


class Streaming(unittest.TestCase):
    def pieces(self, rd, chunks):
        rs = rd.restorer()
        out = [rs.feed(c) for c in chunks] + [rs.finish()]
        return "".join(out)

    def test_placeholder_split_across_chunks(self):
        rd = Redactor()
        rd.redact("a@x.com")
        text = "Mail [EMAIL_1] and [EM"
        for size in (1, 2, 3, 5):
            chunks = [text[i:i + size] for i in range(0, len(text), size)]
            self.assertEqual(self.pieces(rd, chunks), "Mail a@x.com and [EM")

    def test_brackets_that_are_not_placeholders_flow(self):
        rd = Redactor()
        rd.redact("a@x.com")
        self.assertEqual(self.pieces(rd, ["list[0] ", "[ok] x"]), "list[0] [ok] x")

    def test_inside_tool_call_is_json_escaped(self):
        rd = Redactor()
        rd.redact('password: ab\\cd12')
        raw = self.pieces(rd, ['plain [PASSWORD_1] <tool_call>{"x": "[PASS',
                               'WORD_1]"}</tool_call> after [PASSWORD_1]'])
        self.assertIn('plain ab\\cd12 <tool_call>', raw)
        self.assertTrue(raw.endswith('</tool_call> after ab\\cd12'))
        call = raw[raw.index("<tool_call>") + 11:raw.index("</tool_call>")]
        self.assertEqual(json.loads(call)["x"], 'ab\\cd12')


class Speed(unittest.TestCase):
    def test_one_megabyte_under_a_second(self):
        line = "lorem ipsum dolor sit amet 12345 foo bar, version 3.11.2 at /usr/lib/x_y.py line 42. user@host.com 987654\n"
        text = line * (1_000_000 // len(line))
        rd = Redactor(names=["lorem-co"])
        began = time.perf_counter()
        out = rd.redact(text)
        took = time.perf_counter() - began
        self.assertLess(took, 1.0)
        self.assertNotIn("user@host.com", out)
        self.assertEqual(rd.restore(out), text)

    def test_hostile_inputs_stay_fast(self):
        for text in ("1234 5678 " * 100_000, "password: " + "x" * 1_000_000, "key: " + "a1" * 500_000,
                     "-----BEGIN PRIVATE KEY-----" * 20_000, "[EMAIL_" * 100_000, "a@" * 200_000):
            began = time.perf_counter()
            Redactor().redact(text)
            self.assertLess(time.perf_counter() - began, 3.0, text[:20])


class Preview(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_preview_function(self):
        out = redact.preview(self.config, f"me@x.com {VALID_CARD}")
        self.assertEqual(out["counts"], {"email": 1, "card": 1})
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["redacted"], "[EMAIL_1] [CARD_1]")
        self.config.redact_enabled = False
        self.assertEqual(redact.preview(self.config, "me@x.com")["redacted"], "me@x.com")

    def test_route_needs_login_and_works(self):
        app = create_app(self.config, Deps(transcriber=FakeTranscriber()), audit_path=self.root / "audit.log")
        with TestClient(app, headers={"X-Hub": "1"}) as c:
            self.assertEqual(c.post("/api/redact/preview", json={"text": "me@x.com"}).status_code, 401)
            self.assertEqual(c.get("/api/usage").status_code, 401)
            self.assertEqual(c.post("/api/login", json={"token": TOKEN}).status_code, 200)
            r = c.post("/api/redact/preview", json={"text": "me@x.com and Acme"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["redacted"], "[EMAIL_1] and Acme")
            self.assertEqual(c.post("/api/redact/preview", json={"text": 5}).status_code, 400)
            self.assertEqual(c.post("/api/redact/preview", json={"text": "x" * 30000}).status_code, 413)
            log = self.root / "audit.log"
            self.assertFalse(log.exists() and "me@x.com" in log.read_text())

    def test_settings_accept_and_validate(self):
        app = create_app(self.config, Deps(transcriber=FakeTranscriber()), audit_path=self.root / "audit.log")
        with TestClient(app, headers={"X-Hub": "1"}) as c:
            c.post("/api/login", json={"token": TOKEN})
            r = c.patch("/api/settings", json={"redact_names": ["Acme Corp"], "redact_kinds": ["email", "card"],
                                                "cloud_daily_cap": 50,
                                                "provider_daily_caps": {"gemini": 10, "local": 0}})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.config.redact_names, ["Acme Corp"])
            self.assertEqual(self.config.redact_kinds, ["email", "card"])
            self.assertEqual(self.config.provider_daily_caps, {"gemini": 10})
            self.assertEqual(c.patch("/api/settings", json={"redact_kinds": ["nope"]}).status_code, 400)
            self.assertEqual(c.patch("/api/settings", json={"provider_daily_caps": {"x": 1}}).status_code, 400)
            self.assertEqual(c.patch("/api/settings", json={"cloud_daily_cap": -1}).status_code, 400)
            self.assertNotIn("Acme", (self.root / "audit.log").read_text())     # names are secret in the audit


class FakeGemini:
    name = "gemini"

    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def stream(self, system, user):
        self.calls.append((system, user))
        for piece in self.script.pop(0):
            yield piece

    async def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        return self.script.pop(0)[0]


class GuardedClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.rows = []

    def audit(self, event, **fields):
        self.rows.append({"event": event, **fields})

    async def test_complete_round_trip_and_audit_counts_only(self):
        inner = FakeGemini([['{"to": "[EMAIL_1]"}']])
        g = Guarded(inner, self.config, self.audit)
        out = await g.complete("sys for bob@example.com", "mail bob@example.com card " + VALID_CARD, json_mode=True)
        self.assertEqual(json.loads(out), {"to": "bob@example.com"})
        sent = flat(inner.calls)
        self.assertNotIn("bob@example.com", sent)
        self.assertNotIn("4111", sent)
        self.assertEqual(self.rows[0]["event"], "redaction")
        self.assertEqual(self.rows[0]["counts"], {"email": 2, "card": 1})
        self.assertNotIn("bob", json.dumps(self.rows))
        self.assertNotIn("4111", json.dumps(self.rows))

    async def test_disabled_sends_text_as_is(self):
        self.config.redact_enabled = False
        inner = FakeGemini([["ok"]])
        await Guarded(inner, self.config).complete("s", "bob@example.com")
        self.assertIn("bob@example.com", flat(inner.calls))

    async def test_stream_restores_and_passes_other_attributes(self):
        inner = FakeGemini([["Hi [EMA", "IL_1]!"]])
        inner.has_key = lambda: True
        g = Guarded(inner, self.config, self.audit)
        got = [p async for p in g.stream("s", "bob@example.com")]
        self.assertEqual("".join(got), "Hi bob@example.com!")
        self.assertTrue(g.has_key())
        self.assertFalse(hasattr(g, "ask_image"))       # only when the real client has one

    async def test_ask_image_only_when_inner_has_it(self):
        class Vis(FakeGemini):
            async def ask_image(self, system, question, image, mime="image/jpeg"):
                self.q = question
                return "see [EMAIL_1]"
        inner = Vis([])
        g = Guarded(inner, self.config)
        self.assertEqual(await g.ask_image("s", "who is bob@example.com?", b"x"), "see bob@example.com")
        self.assertNotIn("bob@", inner.q)


class AssistantRoundTrips(Base):
    """Through the real assistant: what the cloud model sees, and what the tools get."""

    def setUp(self):
        super().setUp()
        self.rows = []
        self.target = self.home / "bob@example.com"
        self.target.write_text("card on file " + VALID_CARD + " and nothing else")

    def audit(self, event, **fields):
        self.rows.append({"event": event, **fields})

    async def test_claude_api_tool_args_restored_and_results_redacted(self):
        path_ph = f"{self.home}/[EMAIL_1]"
        svc = self.service([
            reply("Reading [EMAIL_1]", tool_block("t1", "read_file", {"path": path_ph})),
            reply("The file mentions [CARD_1]."),
        ], audit=self.audit)
        events = await collect(svc.run("s", f"read {self.home}/bob@example.com please", "claude", False))
        self.assertFalse(of(events, "error"), events)
        start = of(events, "tool_start")[0]
        self.assertEqual(start["input"]["path"], str(self.target))      # restored BEFORE the tool ran
        ok = of(events, "tool_result")[0]
        self.assertTrue(ok["ok"], ok)
        text = "".join(e["t"] for e in of(events, "text"))
        self.assertIn("bob@example.com", text)                          # shown to the owner restored
        self.assertIn(VALID_CARD, text)
        first, second = self.claude.calls
        self.assertNotIn("bob@example.com", flat(first["messages"]))
        self.assertNotIn("bob@example.com", flat(second["messages"]))
        self.assertNotIn("4111", flat(second["messages"]))               # the tool RESULT was redacted too
        self.assertIn("[CARD_1]", flat(second["messages"]))
        history = svc._sessions["s"].history["claude"]                   # history keeps the real text
        self.assertIn("bob@example.com", flat(history))
        counts = [r for r in self.rows if r["event"] == "redaction"]
        self.assertTrue(counts and "email" in counts[0]["counts"])
        self.assertNotIn("bob@example.com", json.dumps(self.rows))
        self.assertNotIn("4111", json.dumps(self.rows))
        self.assertIsNone(redact.current())                              # the map is gone at turn end

    async def test_text_provider_tool_call_with_quote_in_secret(self):
        secret = 'p\\a\\ss12'
        gemini = FakeGemini([
            ['Opening it. <tool_call>{"name": "read_file", "arguments": {"path": "[PASSWORD_1]"}}</tool_call>'],
            ["Done, tried [PASSWORD_1]."],
        ])
        svc = self.service(audit=self.audit)
        svc.model_for = lambda p: Guarded(gemini, self.config, self.audit, count_as="gemini")
        events = await collect(svc.run("s", f"password: {secret} so try reading it", "gemini", False))
        self.assertFalse(of(events, "error"), events)
        start = of(events, "tool_start")[0]
        self.assertEqual(start["input"]["path"], secret)
        for system, prompt in gemini.calls:
            self.assertNotIn(secret, system + prompt)
        self.assertIn("[PASSWORD_1]", gemini.calls[1][1])
        self.assertIn(secret, "".join(e["t"] for e in of(events, "text")))

    async def test_local_provider_is_untouched(self):
        ollama = FakeOllama([ollama_text("Sure bob@example.com")])
        local = A.LocalChat("http://ollama.test", "qwen3:4b", transport=ollama.transport)
        svc = self.service(local=local, audit=self.audit)
        events = await collect(svc.run("s", f"my mail is bob@example.com and card {VALID_CARD}", "local", False))
        sent = json.dumps(ollama.bodies[0])
        self.assertIn("bob@example.com", sent)
        self.assertIn("4111 1111 1111 1111", sent)
        self.assertIn("Sure bob@example.com", "".join(e["t"] for e in of(events, "text")))
        self.assertEqual([r for r in self.rows if r["event"] == "redaction"], [])

    async def test_failover_leg_to_gemini_is_redacted(self):
        # a local session's history may be re-sent to Gemini: that leg goes through Guarded too
        gemini = FakeGemini([["fine"]])
        g = Guarded(gemini, self.config, self.audit, count_as="gemini")
        rd = redact.begin_turn(self.config)
        self.addCleanup(redact.end_turn)
        out = "".join([p async for p in g.stream("s", "[user] mail bob@example.com")])
        self.assertEqual(out, "fine")
        self.assertNotIn("bob@example.com", flat(gemini.calls))
        self.assertIs(redact.current(), rd)


if __name__ == "__main__":
    unittest.main()
