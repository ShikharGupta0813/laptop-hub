import json
import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import english
from hub.common import Ctx, JsonFile
from hub.llm import LLMError
from hub.speech import SpeechError
from tests.helpers import make_config

TODAY = date(2026, 11, 4)


def w(word, start, end, prob=0.9):
    return {"word": word, "start": start, "end": end, "prob": prob}


# 12 s recording. Fillers: Um, you know, uh, basically, kind of, sort of, Hmm = 7.
# Real words: so the deploy failed because it was slow like ok = 10.
DETAIL = {"text": "Um so you know the deploy uh failed because basically it was kind of slow like sort of Hmm ok",
          "duration": 12.0,
          "words": [w("Um,", 0.0, 0.3), w("so", 0.4, 0.6, 0.95), w("you", 0.7, 0.9), w("know", 0.9, 1.2),
                    w("the", 1.3, 1.5), w("deploy", 1.6, 2.2, 0.5), w("uh", 3.5, 3.7, 0.3),
                    w("failed", 3.8, 4.4, 0.55), w("because", 4.5, 5.0), w("basically", 5.1, 5.6),
                    w("it", 5.7, 5.8), w("was", 5.9, 6.0), w("kind", 6.1, 6.3), w("of", 6.3, 6.5),
                    w("slow", 7.6, 8.0), w("like", 8.1, 8.3, 0.2), w("sort", 8.4, 8.6), w("of", 8.6, 8.8),
                    w("Hmm.", 9.0, 9.3), w("ok", 10.3, 10.5)]}

SPOKEN = "Yesterday I go to the office and we discuss the plan"


def spoken_detail(text=SPOKEN, duration=6.0):
    words = text.split()
    return {"text": text, "duration": duration,
            "words": [w(x, round(i * 0.4, 2), round(i * 0.4 + 0.3, 2)) for i, x in enumerate(words)]}


GOOD_FEEDBACK = {
    "summary": "Nice and clear.",
    "grammar": [{"said": "I go to the office", "better": "I went to the office", "why": "past tense"}],
    "vocabulary": [{"said": "discuss the plan", "better": "go over the plan"}],
    "rewrite": "Yesterday I went to the office and we discussed the plan.",
    "scores": {"fluency": 7, "grammar": 5, "vocabulary": 6, "overall": 6},
    "tips": ["Use the past tense."]}


class FakeModel:
    """Scripted: `completes` and `streams` are consumed in order; an exception is raised."""

    def __init__(self, completes=(), streams=()):
        self.completes = list(completes)
        self.streams = list(streams)
        self.calls = []

    async def complete(self, system, user, json_mode=False):
        self.calls.append(("complete", system, user))
        if not self.completes:
            raise LLMError("Can't reach Ollama. Is it running?")
        item = self.completes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    async def stream(self, system, user):
        self.calls.append(("stream", system, user))
        item = self.streams.pop(0)
        for piece in item:
            if isinstance(piece, Exception):
                raise piece
            yield piece


class FakeTranscriber:
    def __init__(self, detail=None, error=None):
        self.detail, self.error, self.paths = detail, error, []

    def transcribe_detailed(self, path):
        self.paths.append(path)
        if self.error:
            raise self.error
        return self.detail


def need_auth(request: Request):
    if request.headers.get("x-auth") != "ok":
        raise HTTPException(401, "Log in first.")


class Base(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config(data_dir=str(self.root_data()))
        self.model = FakeModel()
        self.transcriber = FakeTranscriber(spoken_detail())
        self.audits = []
        ctx = Ctx(config=self.config, model_for=lambda provider: self.model, transcriber=self.transcriber,
                  notes=None, need_auth=need_auth, audit=lambda event, **f: self.audits.append((event, f)))
        app = FastAPI()
        app.include_router(english.make_router(ctx))
        self.client = TestClient(app, headers={"x-auth": "ok"})
        self.addCleanup(self.client.close)
        patcher = patch("hub.english.today", return_value=TODAY)
        self.today = patcher.start()
        self.addCleanup(patcher.stop)
        self.history = JsonFile(self.config.data_path() / "english.json")
        self.vocab = JsonFile(self.config.data_path() / "vocab.json")

    def root_data(self):
        import tempfile
        return tempfile.mkdtemp(prefix="hub-english-")

    def set_today(self, d):
        self.today.return_value = d

    def speak(self, **fields):
        return self.client.post("/api/english/speak", files={"file": ("rec.webm", b"audio")}, data=fields)

    def sse(self, response):
        out = []
        for line in response.text.splitlines():
            if line.startswith("data: "):
                payload = line[6:]
                out.append(payload if payload == "[DONE]" else json.loads(payload))
        return out


class AuthTest(Base):
    def test_every_route_needs_the_login(self):
        anon = TestClient(self.client.app)
        for method, path in [("get", "/scenarios"), ("get", "/overview"), ("get", "/vocab/today"),
                             ("post", "/practice-prompt"), ("post", "/speak"), ("post", "/roleplay"),
                             ("post", "/roleplay/reset"), ("post", "/correct"), ("post", "/vocab/add"),
                             ("post", "/vocab/abc/review")]:
            with self.subTest(path=path):
                self.assertEqual(getattr(anon, method)("/api/english" + path).status_code, 401)


class MetricsTest(unittest.TestCase):
    def test_exact_numbers(self):
        m = english.compute_metrics(DETAIL)
        self.assertEqual(m["words"], 10)
        self.assertEqual(m["fillers"]["count"], 7)
        self.assertEqual(m["fillers"]["items"], {"um": 1, "you know": 1, "uh": 1, "basically": 1,
                                                 "kind of": 1, "sort of": 1, "hmm": 1})
        self.assertEqual(m["wpm"], 50)                # 10 words in 12 seconds
        self.assertEqual(m["fillers_per_100"], 70.0)  # 7 fillers per 10 words
        self.assertEqual(m["long_pauses"], 2)         # 1.3 s and 1.1 s; the 1.0 s gap is not over 1.0
        self.assertEqual(m["rate"], "slow")

    def test_low_confidence_words_skip_fillers_and_keep_real_words(self):
        m = english.compute_metrics(DETAIL)
        # "uh" has prob 0.3 but is a filler. "like" is a real word here.
        self.assertEqual(m["low_confidence_words"], [{"word": "like", "prob": 0.2},
                                                     {"word": "deploy", "prob": 0.5},
                                                     {"word": "failed", "prob": 0.55}])

    def test_low_confidence_is_capped_at_eight_and_deduplicated(self):
        words = [w(f"word{i}", i, i + 0.5, 0.1 + i / 100) for i in range(12)] + [w("word0", 20, 20.5, 0.05)]
        m = english.compute_metrics({"duration": 30, "words": words})
        self.assertEqual(len(m["low_confidence_words"]), 8)
        self.assertEqual(m["low_confidence_words"][0], {"word": "word0", "prob": 0.05})
        self.assertEqual(len({x["word"] for x in m["low_confidence_words"]}), 8)

    def test_two_word_fillers_match_whole_pairs_only(self):
        words = "I mean it is kind of fine, you really know it, You Know? and sort of. I MEAN yes kind".split()
        m = english.compute_metrics({"duration": 60, "words": [w(x, i, i + 0.5) for i, x in enumerate(words)]})
        self.assertEqual(m["fillers"]["items"], {"i mean": 2, "kind of": 1, "you know": 1, "sort of": 1})
        # 'you' and 'know' apart, and a trailing 'kind', stay real words
        self.assertEqual(m["words"], len(words) - 2 * 5)

    def test_like_is_not_a_filler_and_case_and_punctuation_are_ignored(self):
        words = ["I", "like", "it,", "UM...", "Uh,", "ER", "ah!", "Actually,", "like"]
        m = english.compute_metrics({"duration": 6, "words": [w(x, i, i + 0.5) for i, x in enumerate(words)]})
        self.assertEqual(m["fillers"]["items"], {"um": 1, "uh": 1, "er": 1, "ah": 1, "actually": 1})
        self.assertEqual(m["words"], 4)
        self.assertEqual(m["wpm"], 40)

    def test_rate_advice_boundaries(self):
        self.assertEqual(english.rate_advice(99)["rate"], "slow")
        self.assertEqual(english.rate_advice(100)["rate"], "good")
        self.assertEqual(english.rate_advice(160)["rate"], "good")
        self.assertEqual(english.rate_advice(161)["rate"], "fast")

    def test_wpm_uses_the_whole_recording_not_the_speaking_time(self):
        words = [w(f"w{i}", 0.1 * i, 0.1 * i + 0.05) for i in range(10)]
        self.assertEqual(english.compute_metrics({"duration": 5, "words": words})["wpm"], 120)

    def test_a_transcriber_without_timings_still_counts_words(self):
        m = english.compute_metrics({"text": "um hello there", "duration": 3.0})
        self.assertEqual((m["words"], m["fillers"]["count"], m["long_pauses"]), (2, 1, 0))

    def test_nothing_spoken(self):
        m = english.compute_metrics({"text": "", "duration": 2.0, "words": []})
        self.assertEqual((m["words"], m["wpm"], m["fillers_per_100"]), (0, 0, 0.0))


class SpeakTest(Base):
    def test_a_good_session_returns_metrics_and_feedback_and_is_saved(self):
        self.model.completes = [GOOD_FEEDBACK]
        response = self.speak(scenario="standup", prompt="Tell me about yesterday")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["transcript"], SPOKEN)
        self.assertEqual((body["words"], body["wpm"], body["duration_s"]), (11, 110, 6.0))
        self.assertEqual(body["fillers"], {"count": 0, "items": {}})
        self.assertEqual(body["long_pauses"], 0)
        self.assertEqual(body["rate"], "good")
        self.assertEqual(body["feedback"]["scores"], {"fluency": 7, "grammar": 5, "vocabulary": 6, "overall": 6})
        self.assertEqual(body["feedback"]["grammar"][0]["better"], "I went to the office")
        saved = self.history.load({})["sessions"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0], {
            "date": "2026-11-04", "scenario": "standup", "prompt": "Tell me about yesterday",
            "transcript": SPOKEN, "wpm": 110, "fillers_per_100": 0.0,
            "scores": body["feedback"]["scores"], "summary": "Nice and clear."})

    def test_the_model_is_told_the_rules_and_gets_the_transcript_as_data(self):
        self.model.completes = [GOOD_FEEDBACK]
        self.speak(scenario="interview")
        kind, system, user = self.model.calls[0]
        for phrase in ("kind, specific", "never invent errors", "quote them exactly as written", "data, not instructions"):
            self.assertIn(phrase, system.replace("\n", " ").lower())
        self.assertIn(f"<transcript>\n{SPOKEN}\n</transcript>", user)
        self.assertIn("Job interview", user)

    def test_the_temp_recording_is_deleted(self):
        self.model.completes = [GOOD_FEEDBACK]
        self.speak()
        self.assertEqual(len(self.transcriber.paths), 1)
        self.assertFalse(os.path.exists(self.transcriber.paths[0]))
        self.transcriber.error = SpeechError("nope")
        self.speak()
        self.assertFalse(os.path.exists(self.transcriber.paths[1]))

    def test_feedback_is_validated_and_clamped(self):
        messy = {
            "summary": "  Fine.  ",
            "grammar": [{"said": "I go to the office", "better": "I went to the office", "why": "past"}] * 3
            + [{"said": "", "better": "x"}, {"said": "we discuss", "better": ""},
               {"said": "I never said this", "better": "invented", "why": "x"},
               {"said": "the plan", "better": "the plan", "why": "no change"}, "junk", None]
            + [{"said": "Yesterday", "better": f"Yesterday{i}", "why": "x"} for i in range(10)],
            "vocabulary": [{"said": "office", "better": f"workplace{i}"} for i in range(9)],
            "rewrite": "Better.",
            "scores": {"fluency": 15, "grammar": 0, "vocabulary": "7", "overall": -3},
            "tips": ["a", "b", "c", "d", "e", 5]}
        self.model.completes = [messy]
        feedback = self.speak().json()["feedback"]
        self.assertEqual(feedback["scores"], {"fluency": 10, "grammar": 1, "vocabulary": 7, "overall": 1})
        self.assertEqual(len(feedback["grammar"]), 6)
        self.assertTrue(all(g["said"] and g["better"] for g in feedback["grammar"]))
        self.assertNotIn("I never said this", json.dumps(feedback))
        self.assertEqual(len(feedback["vocabulary"]), 6)
        self.assertEqual(feedback["tips"], ["a", "b", "c"])
        self.assertEqual(feedback["summary"], "Fine.")

    def test_missing_scores_are_filled_but_no_scores_at_all_is_an_error(self):
        self.model.completes = [{"summary": "ok", "scores": {"fluency": 8, "grammar": 6}}]
        self.assertEqual(self.speak().json()["feedback"]["scores"],
                         {"fluency": 8, "grammar": 6, "vocabulary": 7, "overall": 7})
        self.model.completes = [{"summary": "ok"}]
        self.assertEqual(self.speak().status_code, 503)
        self.assertEqual(len(self.history.load({})["sessions"]), 1)

    def test_a_clean_session_with_no_mistakes_is_allowed(self):
        self.model.completes = [{**GOOD_FEEDBACK, "grammar": [], "vocabulary": [], "summary": "No mistakes."}]
        feedback = self.speak().json()["feedback"]
        self.assertEqual((feedback["grammar"], feedback["vocabulary"]), ([], []))

    def test_empty_transcript_is_422_and_the_model_is_not_asked(self):
        for detail in ({"text": "", "duration": 3.0, "words": []},
                       {"text": "um uh", "duration": 3.0, "words": [w("um", 0, 0.2), w("uh", 0.5, 0.7)]}):
            self.transcriber.detail = detail
            response = self.speak()
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"].split(".")[0], "I couldn't hear any words")
        self.assertEqual(self.model.calls, [])
        self.assertIsNone(self.history.load(None))

    def test_voice_not_set_up_is_503_with_the_fix(self):
        self.transcriber.error = SpeechError("Voice notes need faster-whisper. Run: pip install -r requirements-voice.txt")
        response = self.speak()
        self.assertEqual(response.status_code, 503)
        self.assertIn("faster-whisper", response.json()["detail"])

    def test_garbage_json_is_a_clear_error_and_nothing_is_saved(self):
        self.model.completes = ["Sorry, I cannot do that.", "Still not JSON."]
        response = self.speak()
        self.assertEqual(response.status_code, 503)
        self.assertIn("could not be read", response.json()["detail"])
        self.assertIsNone(self.history.load(None))

    def test_json_of_the_wrong_shape_is_an_error_too(self):
        self.model.completes = ["[1, 2, 3]"]
        self.assertEqual(self.speak().status_code, 503)
        self.assertIsNone(self.history.load(None))

    def test_a_model_that_is_down_is_a_503_with_its_message(self):
        response = self.speak()  # nothing scripted: the fake model raises LLMError
        self.assertEqual(response.status_code, 503)
        self.assertIn("Ollama", response.json()["detail"])
        self.assertIsNone(self.history.load(None))

    def test_bad_input(self):
        self.assertEqual(self.speak(scenario="nonsense").status_code, 422)
        self.assertEqual(self.speak(provider="gpt").status_code, 422)
        self.assertEqual(self.client.post("/api/english/speak", files={"file": ("a.webm", b"")}).status_code, 422)
        self.assertEqual(self.client.post("/api/english/speak").status_code, 422)

    def test_recordings_over_25_mb_are_refused(self):
        big = b"0" * (25 * 1024 * 1024 + 1)
        response = self.client.post("/api/english/speak", files={"file": ("a.webm", big)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.transcriber.paths, [])

    def test_history_keeps_the_newest_500(self):
        old = [{"date": "2026-01-01", "scenario": "", "prompt": "", "transcript": f"t{i}", "wpm": 100,
                "fillers_per_100": 0, "scores": {"overall": 5}, "summary": ""} for i in range(500)]
        self.history.save({"sessions": old})
        self.model.completes = [GOOD_FEEDBACK]
        self.speak()
        saved = self.history.load({})["sessions"]
        self.assertEqual(len(saved), 500)
        self.assertEqual(saved[0]["transcript"], "t1")
        self.assertEqual(saved[-1]["transcript"], SPOKEN)

    def test_fillers_are_reported_through_the_endpoint(self):
        self.transcriber.detail = DETAIL
        self.model.completes = [{**GOOD_FEEDBACK, "grammar": [], "vocabulary": []}]
        body = self.speak().json()
        self.assertEqual(body["fillers"]["count"], 7)
        self.assertEqual(body["long_pauses"], 2)
        self.assertEqual(body["fillers_per_100"], 70.0)
        self.assertEqual([x["word"] for x in body["low_confidence_words"]], ["like", "deploy", "failed"])


class OverviewTest(Base):
    def session(self, day, wpm=100, overall=6):
        return {"date": day.isoformat(), "scenario": "", "prompt": "", "transcript": "x", "wpm": wpm,
                "fillers_per_100": 2.5, "scores": {"overall": overall}, "summary": "s"}

    def test_empty(self):
        body = self.client.get("/api/english/overview").json()
        self.assertEqual(body, {"streak_days": 0, "sessions_total": 0, "last_session": None, "trend": [],
                                "vocab_due": 0})

    def test_streak_counts_consecutive_days_ending_today(self):
        days = [TODAY, TODAY - timedelta(1), TODAY - timedelta(2), TODAY - timedelta(4)]
        self.history.save({"sessions": [self.session(d) for d in sorted(days)]})
        self.assertEqual(self.client.get("/api/english/overview").json()["streak_days"], 3)

    def test_a_vocabulary_review_keeps_the_streak_going(self):
        self.history.save({"sessions": [self.session(TODAY - timedelta(2))]})
        self.vocab.save({"words": [], "daily": {}, "review_days": [(TODAY - timedelta(1)).isoformat(), TODAY.isoformat()]})
        self.assertEqual(self.client.get("/api/english/overview").json()["streak_days"], 3)

    def test_streak_survives_until_a_day_is_missed(self):
        self.history.save({"sessions": [self.session(TODAY - timedelta(1)), self.session(TODAY - timedelta(2))]})
        self.assertEqual(self.client.get("/api/english/overview").json()["streak_days"], 2)
        self.set_today(TODAY + timedelta(1))  # yesterday is now two days ago: the streak is over
        self.assertEqual(self.client.get("/api/english/overview").json()["streak_days"], 0)

    def test_trend_is_the_last_14_sessions_oldest_first(self):
        sessions = [self.session(TODAY - timedelta(20 - i), wpm=100 + i, overall=1 + i % 10) for i in range(20)]
        self.history.save({"sessions": sessions})
        body = self.client.get("/api/english/overview").json()
        self.assertEqual(body["sessions_total"], 20)
        self.assertEqual(len(body["trend"]), 14)
        self.assertEqual(body["trend"][0], {"date": sessions[6]["date"], "wpm": 106, "fillers_per_100": 2.5,
                                            "overall": 7})
        self.assertEqual(body["trend"][-1]["wpm"], 119)
        self.assertEqual(body["last_session"]["wpm"], 119)

    def test_a_speak_call_shows_up_in_the_overview(self):
        self.model.completes = [GOOD_FEEDBACK]
        self.speak()
        body = self.client.get("/api/english/overview").json()
        self.assertEqual((body["streak_days"], body["sessions_total"]), (1, 1))
        self.assertEqual(body["trend"], [{"date": "2026-11-04", "wpm": 110, "fillers_per_100": 0.0, "overall": 6}])

    def test_a_damaged_history_file_does_not_break_it(self):
        self.history.path.parent.mkdir(parents=True, exist_ok=True)
        self.history.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.client.get("/api/english/overview").json()["sessions_total"], 0)


class ScenarioAndPromptTest(Base):
    def test_scenarios(self):
        scenarios = self.client.get("/api/english/scenarios").json()["scenarios"]
        self.assertGreaterEqual(len(scenarios), 8)
        self.assertTrue(all(set(s) == {"id", "title", "description"} and all(s.values()) for s in scenarios))
        self.assertEqual(len({s["id"] for s in scenarios}), len(scenarios))

    def test_prompt_from_the_model(self):
        self.model.completes = [{"prompt": "Talk about your last release."}]
        body = self.client.post("/api/english/practice-prompt", json={"scenario": "standup"}).json()
        self.assertEqual(body["prompt"], "Talk about your last release.")

    def test_every_scenario_has_an_offline_prompt(self):
        for s in english.SCENARIOS:
            with self.subTest(scenario=s["id"]):
                for reply in (LLMError("down"), "not json", "not json again"):
                    self.model.completes = [reply] if isinstance(reply, Exception) else [reply, reply]
                    body = self.client.post("/api/english/practice-prompt", json={"scenario": s["id"]}).json()
                    self.assertIn(body["prompt"], s["prompts"])

    def test_unknown_scenario(self):
        self.assertEqual(self.client.post("/api/english/practice-prompt", json={"scenario": "x"}).status_code, 422)


class RoleplayTest(Base):
    def post(self, message, session="s1", scenario="client_call"):
        return self.client.post("/api/english/roleplay", json={"session_id": session, "scenario": scenario,
                                                               "message": message})

    def last_prompt(self):
        return [c for c in self.model.calls if c[0] == "stream"][-1][2]

    def test_the_partner_opens_and_there_is_no_correction(self):
        self.model.streams = [["Hello! ", "Thanks for ", "joining. How is it going?"]]
        events = self.sse(self.post(None))
        self.assertEqual(events, [{"t": "Hello! "}, {"t": "Thanks for "}, {"t": "joining. How is it going?"}, "[DONE]"])
        self.assertEqual(self.model.completes, [])
        system, user = self.model.calls[0][1:]
        self.assertIn("at most 60 words", system)
        self.assertIn("client project manager", system)
        self.assertIn("Start the conversation", user)

    def test_a_reply_then_the_correction_then_done(self):
        self.model.streams = [["Sure. ", "And you?"]]
        self.model.completes = [{"said": "I am agree", "better": "I agree", "why": "No 'am'."}]
        events = self.sse(self.post("I am agree with your plan"))
        self.assertEqual(events, [{"t": "Sure. "}, {"t": "And you?"},
                                  {"correction": {"said": "I am agree", "better": "I agree", "why": "No 'am'."}},
                                  "[DONE]"])
        check = self.model.calls[1]
        self.assertIn("<message>\nI am agree with your plan\n</message>", check[2])
        self.assertIn("ONLY the text inside <message> tags", check[1])

    def test_no_mistake_gives_a_null_correction(self):
        for reply in ({"correction": None}, {"said": "something never said", "better": "x", "why": "y"},
                      {"said": "same", "better": "same"}):
            self.model.streams = [["Fine."]]
            self.model.completes = [reply]
            events = self.sse(self.post("This sentence is fine"))
            self.assertEqual(events[-2:], [{"correction": None}, "[DONE]"])

    def test_a_failed_correction_check_is_null_not_an_error(self):
        self.model.streams = [["Fine."], ["Fine."]]
        self.model.completes = [LLMError("down")]
        events = self.sse(self.post("Hello there"))
        self.assertEqual(events, [{"t": "Fine."}, {"correction": None}, "[DONE]"])
        self.model.completes = ["garbage", "garbage"]
        events = self.sse(self.post("Hello again"))
        self.assertEqual(events[-2:], [{"correction": None}, "[DONE]"])

    def test_history_is_kept_per_session_and_trimmed_to_12_turns(self):
        for i in range(10):  # 10 user + 10 partner turns
            self.model.streams.append([f"reply{i}"])
            self.post(f"message{i}")
        user_prompt = [c for c in self.model.calls if c[0] == "stream"][-1][2]
        lines = [l for l in user_prompt.splitlines() if l.startswith(("Learner:", "You:"))]
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[-1], "Learner: message9")
        self.assertIn("You: reply8", user_prompt)
        self.assertNotIn("message0", user_prompt)
        # another session is separate
        self.model.streams.append(["hi"])
        self.post("other", session="s2")
        other = [c for c in self.model.calls if c[0] == "stream"][-1][2]
        self.assertNotIn("message9", other)

    def test_an_opening_starts_the_conversation_afresh(self):
        self.model.streams = [["one"], ["open again"]]
        self.post("remember this")
        self.post(None)
        self.assertNotIn("remember this", self.last_prompt())

    def test_reset_forgets_the_conversation(self):
        self.model.streams = [["a"], ["b"]]
        self.post("remember this")
        self.assertEqual(self.client.post("/api/english/roleplay/reset", json={"session_id": "s1"}).json(), {"ok": True})
        self.post("second")
        self.assertNotIn("remember this", self.last_prompt())

    def test_only_the_last_30_sessions_are_kept(self):
        for i in range(31):
            self.model.streams.append(["ok"])
            self.post(f"first{i}", session=f"sess{i}")
        self.model.streams.append(["ok"])
        self.post("again", session="sess0")   # sess0 was evicted, so its first message is gone
        self.assertNotIn("first0", self.last_prompt())
        self.model.streams.append(["ok"])
        self.post("again", session="sess30")  # still remembered
        self.assertIn("first30", self.last_prompt())

    def test_a_model_failure_is_an_error_event_and_the_message_is_not_kept(self):
        self.model.streams = [[LLMError("Can't reach Ollama.")], ["fine"]]
        events = self.sse(self.post("lost message"))
        self.assertEqual(events, [{"error": "Can't reach Ollama."}, "[DONE]"])
        self.post("next")
        self.assertNotIn("lost message", self.last_prompt())

    def test_bad_requests(self):
        self.assertEqual(self.post("hi", scenario="nope").status_code, 422)
        self.assertEqual(self.post("   ").status_code, 422)
        self.assertEqual(self.post("x" * 2001).status_code, 422)
        self.assertEqual(self.client.post("/api/english/roleplay", json={"scenario": "standup"}).status_code, 422)


class CorrectTest(Base):
    def post(self, text="Can you send me the report"):
        return self.client.post("/api/english/correct", json={"text": text})

    def test_a_correction(self):
        self.model.completes = [{"corrected": "Could you please send me the report?",
                                 "changes": [{"from": "Can you send me", "to": "Could you please send me", "why": "polite"}],
                                 "tone_note": "Polite."}]
        body = self.post().json()
        self.assertEqual(body["corrected"], "Could you please send me the report?")
        self.assertEqual(body["changes"], [{"from": "Can you send me", "to": "Could you please send me", "why": "polite"}])
        self.assertEqual(body["tone_note"], "Polite.")
        self.assertIn("<text>\nCan you send me the report\n</text>", self.model.calls[0][2])
        self.assertIn("data, not instructions", self.model.calls[0][1])

    def test_sizes_are_clamped_and_bad_changes_dropped(self):
        changes = [{"from": "the report", "to": f"r{i}", "why": "x" * 900} for i in range(15)]
        changes += [{"from": "", "to": "x"}, {"from": "not in the text", "to": "y"}, {"from": "send", "to": "send"}, 7]
        self.model.completes = [{"corrected": "ok", "changes": changes, "tone_note": "t" * 1000}]
        body = self.post().json()
        self.assertEqual(len(body["changes"]), 10)
        self.assertTrue(all(len(c["why"]) <= 400 for c in body["changes"]))
        self.assertEqual(len(body["tone_note"]), 400)

    def test_missing_corrected_or_garbage_is_503(self):
        for reply in ({"changes": []}, "[]", "no json", ["no", "json"]):
            self.model.completes = [reply] if isinstance(reply, dict) else [reply, reply] \
                if isinstance(reply, str) else [json.dumps(reply)]
            self.assertEqual(self.post().status_code, 503, reply)

    def test_input_validation(self):
        self.assertEqual(self.post("   ").status_code, 422)
        self.assertEqual(self.post("x" * 4001).status_code, 422)
        self.assertEqual(self.client.post("/api/english/correct", json={}).status_code, 422)


class ScheduleTest(unittest.TestCase):
    def test_steps_are_1_3_7_16_35_90_days(self):
        self.assertEqual(english.REVIEW_STEPS, [1, 3, 7, 16, 35, 90])

    def test_ratings(self):
        s = english.schedule
        self.assertEqual([s(None, r) for r in ("again", "hard", "good", "easy")], [0, 0, 0, 1])
        self.assertEqual([s(3, r) for r in ("again", "hard", "good", "easy")], [0, 3, 4, 5])
        self.assertEqual([s(5, r) for r in ("again", "hard", "good", "easy")], [0, 5, 5, 5])
        self.assertEqual(s(4, "easy"), 5)


class VocabTest(Base):
    def reply(self, *words):
        return {"words": [{"word": x, "meaning": f"meaning of {x}", "example": f"We {x} it.", "level": "C1"}
                          for x in words]}

    def get_today(self):
        return self.client.get("/api/english/vocab/today").json()

    def test_builtin_list_is_big_and_complete(self):
        self.assertGreaterEqual(len(english.BUILTIN_WORDS), 40)
        self.assertEqual(len({x["word"] for x in english.BUILTIN_WORDS}), len(english.BUILTIN_WORDS))
        for entry in english.BUILTIN_WORDS:
            self.assertTrue(entry["meaning"] and entry["example"])
            self.assertIn(entry["level"], ("B2", "C1"))

    def test_offline_gives_five_builtin_words_and_caches_the_day(self):
        first = self.get_today()
        self.assertEqual(len(first["new_words"]), 5)
        self.assertEqual(first["due"], [])
        builtin = {x["word"] for x in english.BUILTIN_WORDS}
        self.assertTrue(all(x["word"] in builtin and x["id"] and x["meaning"] and x["example"]
                            for x in first["new_words"]))
        self.assertEqual(self.get_today(), first)  # cached for the day
        self.assertEqual(len(self.vocab.load({})["words"]), 5)

    def test_words_come_from_the_model_once_per_day(self):
        self.model.completes = [self.reply("alpha", "beta", "gamma", "delta", "epsilon", "zeta")]
        words = self.get_today()["new_words"]
        self.assertEqual([x["word"] for x in words], ["alpha", "beta", "gamma", "delta", "epsilon"])
        self.assertEqual(len([c for c in self.model.calls if c[0] == "complete"]), 1)
        self.get_today()
        self.assertEqual(len(self.model.calls), 1)
        self.assertIn("B2 to C1", self.model.calls[0][1])

    def test_model_words_are_validated_and_topped_up_from_the_builtin_list(self):
        reply = self.reply("alpha", "ALPHA")
        reply["words"] += [{"word": "nomeaning"}, "junk", {"word": "", "meaning": "m", "example": "e"}]
        self.model.completes = [reply]
        words = self.get_today()["new_words"]
        self.assertEqual(len(words), 5)
        self.assertEqual(words[0]["word"], "alpha")
        self.assertEqual(len({x["word"].lower() for x in words}), 5)

    def test_new_words_are_never_ones_already_learned(self):
        seen = set()
        for day in range(12):  # 12 days x 5 = 60 > 49 builtin: the list runs out gracefully
            self.set_today(TODAY + timedelta(days=day))
            words = [x["word"] for x in self.get_today()["new_words"]]
            self.assertFalse(seen & set(words), day)
            seen |= set(words)
        self.assertEqual(seen, {x["word"] for x in english.BUILTIN_WORDS})

    def test_a_learned_word_from_the_model_is_skipped(self):
        self.model.completes = [self.reply("mitigate", "newword")]
        self.get_today()  # day 1: fallback is not used, model words stored... then day 2
        self.set_today(TODAY + timedelta(1))
        self.model.completes = [self.reply("mitigate", "newword", "another")]
        words = [x["word"] for x in self.get_today()["new_words"]]
        self.assertNotIn("newword", words)
        self.assertIn("another", words)

    def test_add_a_word_with_the_model(self):
        self.model.completes = [{"meaning": "to examine closely", "example": "Scrutinise the logs.", "level": "c1"}]
        body = self.client.post("/api/english/vocab/add", json={"word": "  scrutinise "}).json()
        self.assertEqual({k: body[k] for k in ("word", "meaning", "example", "level")},
                         {"word": "scrutinise", "meaning": "to examine closely",
                          "example": "Scrutinise the logs.", "level": "C1"})
        self.assertIn("<word>scrutinise</word>", self.model.calls[0][2])
        self.assertEqual(self.vocab.load({})["words"][0]["id"], body["id"])
        again = self.client.post("/api/english/vocab/add", json={"word": "Scrutinise"}).json()
        self.assertEqual(again["id"], body["id"])  # no duplicates
        self.assertEqual(len(self.vocab.load({})["words"]), 1)

    def test_add_offline_uses_the_builtin_list_or_says_why_not(self):
        known = self.client.post("/api/english/vocab/add", json={"word": "Mitigate"}).json()
        self.assertTrue(known["meaning"] and known["example"])
        unknown = self.client.post("/api/english/vocab/add", json={"word": "zzyzx"})
        self.assertEqual(unknown.status_code, 503)
        self.assertIn("Ollama", unknown.json()["detail"])
        self.model.completes = [{"meaning": "", "example": ""}]
        self.assertEqual(self.client.post("/api/english/vocab/add", json={"word": "zzyzx"}).status_code, 503)
        self.assertEqual(len(self.vocab.load({})["words"]), 1)

    def test_review_schedule_across_days(self):
        added = self.get_today()["new_words"][0]
        wid = added["id"]

        def review(rating):
            return self.client.post(f"/api/english/vocab/{wid}/review", json={"rating": rating}).json()["next_due"]

        def due_ids():
            return [x["id"] for x in self.get_today()["due"]]

        self.assertEqual(review("good"), "2026-11-05")      # new word, good: 1 day
        self.assertNotIn(wid, due_ids())
        self.set_today(date(2026, 11, 5))
        self.assertIn(wid, due_ids())
        self.assertEqual(review("good"), "2026-11-08")      # step 1: 3 days
        self.set_today(date(2026, 11, 8))
        self.assertEqual(review("hard"), "2026-11-11")      # same step again: 3 days
        self.set_today(date(2026, 11, 11))
        self.assertEqual(review("easy"), "2026-11-27")      # skip a step: 3 -> 16 days
        self.set_today(date(2026, 11, 27))
        self.assertEqual(review("good"), "2027-01-01")      # 35 days
        self.set_today(date(2027, 1, 1))
        self.assertEqual(review("again"), "2027-01-02")     # reset: tomorrow
        self.set_today(date(2027, 1, 2))
        self.assertEqual(review("good"), "2027-01-05")      # back to 3 days
        entry = next(x for x in self.vocab.load({})["words"] if x["id"] == wid)
        self.assertEqual((entry["step"], entry["reviews"]), (1, 7))

    def test_the_last_step_is_90_days_and_stays_there(self):
        wid = self.get_today()["new_words"][0]["id"]
        for _ in range(8):
            next_due = self.client.post(f"/api/english/vocab/{wid}/review", json={"rating": "easy"}).json()["next_due"]
        self.assertEqual(next_due, (TODAY + timedelta(days=90)).isoformat())

    def test_unreviewed_words_become_due_the_next_day_without_duplicating(self):
        today_words = self.get_today()["new_words"]
        self.assertEqual(self.client.get("/api/english/overview").json()["vocab_due"], 0)
        self.set_today(TODAY + timedelta(1))
        tomorrow = self.get_today()
        self.assertEqual({x["id"] for x in tomorrow["due"]}, {x["id"] for x in today_words})
        self.assertFalse({x["id"] for x in tomorrow["new_words"]} & {x["id"] for x in tomorrow["due"]})
        self.assertEqual(self.client.get("/api/english/overview").json()["vocab_due"], 5)

    def test_a_review_counts_for_the_streak(self):
        wid = self.get_today()["new_words"][0]["id"]
        self.client.post(f"/api/english/vocab/{wid}/review", json={"rating": "good"})
        self.assertEqual(self.client.get("/api/english/overview").json()["streak_days"], 1)

    def test_review_errors(self):
        self.assertEqual(self.client.post("/api/english/vocab/nope/review", json={"rating": "good"}).status_code, 404)
        wid = self.get_today()["new_words"][0]["id"]
        self.assertEqual(self.client.post(f"/api/english/vocab/{wid}/review", json={"rating": "perfect"}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/english/vocab/{wid}/review", json={}).status_code, 422)


class DevFakesTest(unittest.TestCase):
    """Each canned reply must be valid for the code that consumes it, and match only its own prompt."""

    PROMPTS = [english.FEEDBACK_SYSTEM, english.ROLEPLAY_SYSTEM + "a partner", english.CORRECTION_CHECK_SYSTEM,
               english.CORRECT_SYSTEM, english.PRACTICE_PROMPT_SYSTEM, english.VOCAB_DAILY_SYSTEM,
               english.VOCAB_ADD_SYSTEM]

    def test_each_marker_matches_exactly_one_system_prompt(self):
        self.assertEqual(len(english.DEV_FAKES), len(self.PROMPTS))
        for marker in english.DEV_FAKES:
            self.assertEqual(sum(marker in p for p in self.PROMPTS), 1, marker)

    def test_canned_replies_are_valid(self):
        fakes = {m: r for m, r in english.DEV_FAKES.items()}
        by_prompt = {p: next(r for m, r in fakes.items() if m in p) for p in self.PROMPTS}
        feedback = english.validate_feedback(json.loads(by_prompt[english.FEEDBACK_SYSTEM]),
                                             "Um so yesterday I I go to the office and uh we discuss the new plan")
        self.assertEqual(len(feedback["grammar"]), 2)
        self.assertEqual(len(feedback["vocabulary"]), 1)
        self.assertIsNotNone(english.validate_correction(
            json.loads(by_prompt[english.CORRECTION_CHECK_SYSTEM]), "Yes I am agree with you"))
        english.validate_correct(json.loads(by_prompt[english.CORRECT_SYSTEM]),
                                 "Can you send me the report by Friday?")
        self.assertTrue(json.loads(by_prompt[english.PRACTICE_PROMPT_SYSTEM])["prompt"])
        self.assertEqual(len(json.loads(by_prompt[english.VOCAB_DAILY_SYSTEM])["words"]), 3)
        self.assertIsNotNone(english._word_fields(json.loads(by_prompt[english.VOCAB_ADD_SYSTEM])))
        self.assertFalse(by_prompt[english.ROLEPLAY_SYSTEM + "a partner"].lstrip().startswith(("{", "[")))


if __name__ == "__main__":
    unittest.main()
