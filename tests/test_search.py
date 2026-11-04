"""Tests for global SEARCH and the HOME dashboard. The other features are faked as plain routes on a small
app (the search module reaches them through the real in-process client), so nothing here needs them to exist."""
import asyncio
import json
import time
import unittest
from collections import Counter
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import auth, search
from hub.common import Ctx
from hub.notes import NoteStore
from tests.helpers import make_config

NOW = datetime(2026, 11, 4, 9, 0, 0)


def item(title, snippet="", kind="x", **open_):
    return {"title": title, "snippet": snippet, "kind": kind, "open": open_ or {"view": "v", "id": title}}


class Fake:
    """Routes standing in for the other features. `answers[path]` is what the route returns; a callable runs
    first, an Exception class makes the route fail; paths not in `answers` do not exist (404)."""

    def __init__(self):
        self.answers: dict[str, object] = {}
        self.hits = Counter()
        self.delays: dict[str, float] = {}

    def install(self, app: FastAPI):
        fake = self

        @app.api_route("/api/{rest:path}", methods=["GET"])
        async def any_route(rest: str, request: Request):
            path = "/api/" + rest
            if path not in fake.answers:
                raise HTTPException(404, "")
            fake.hits[path] += 1
            if fake.delays.get(path):
                await asyncio.sleep(fake.delays[path])
            answer = fake.answers[path]
            if isinstance(answer, type) and issubclass(answer, Exception):
                raise HTTPException(500, "it broke")
            return answer


class Base(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.config.data_dir = str(self.root / "data")
        self.config.apps = {"Chrome": {"target": "chrome.exe"}}
        self.config.scripts = {"hello": {"label": "Say hello", "argv": ["echo", "hi"]},
                               "backup-db": {"label": "Backup database", "argv": ["echo"]}}
        self.fake = Fake()
        self.clock_value = [1000.0]
        self.build()

    def build(self, **kwargs):
        app = FastAPI()
        self.app = app

        def need_auth(request: Request):
            if not auth.session_valid(self.config.session_secret, request.cookies.get(auth.COOKIE)):
                raise HTTPException(401, "Log in first.")

        self.audits = []
        self.notes = NoteStore(self.config.notes_dir)
        self.ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=self.notes, need_auth=need_auth,
                       audit=lambda e, **f: self.audits.append((e, f)), get_app=lambda: app)
        options = dict(timeout=2.5, section_timeout=3.0, clock=lambda: self.clock_value[0], now=lambda: NOW)
        options.update(kwargs)
        app.include_router(search.make_router(self.ctx, **options))
        self.fake.install(app)   # after the search router, so its own /api/search routes win over the catch-all
        self.client = TestClient(app, base_url="http://testserver")
        self.addCleanup(self.client.close)
        self.client.cookies.set(auth.COOKIE, auth.make_session(self.config.session_secret))

    def get(self, q, **params):
        return self.client.get("/api/search", params={"q": q, **params})


class SearchTest(Base):
    def test_login_is_required(self):
        self.client.cookies.clear()
        for method, path in [("get", "/api/search?q=hello"), ("get", "/api/search/all?q=hello"), ("get", "/api/home"),
                             ("get", "/api/search/suggest"), ("post", "/api/search/recent"), ("delete", "/api/search/recent")]:
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)

    def test_merges_and_ranks_exact_prefix_substring_fuzzy(self):
        self.notes.add("Q3 budget review")
        self.fake.answers["/api/kb/search"] = {"results": [item("The budget plan", kind="kb"), item("budget", kind="kb")]}
        self.fake.answers["/api/cards/search"] = {"results": [item("budgets 101", kind="card"), item("Buget typo", kind="card")]}
        self.fake.answers["/api/inbox/search"] = {"results": [item("Unrelated name", "mentions budget here", kind="inbox")]}
        data = self.get("budget").json()
        titles = [r["title"] for r in data["results"]]
        self.assertEqual(titles[0], "budget")                       # exact
        self.assertEqual(titles[1], "budgets 101")                  # prefix
        self.assertLess(titles.index("The budget plan"), titles.index("Q3 budget review") + 1)
        self.assertLess(titles.index("Q3 budget review"), titles.index("Unrelated name"))   # title beats snippet-only
        self.assertIn("Buget typo", titles)                         # fuzzy still found, ranked below substrings
        self.assertLess(titles.index("The budget plan"), titles.index("Buget typo"))
        scores = [r["score"] for r in data["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(data["query"], "budget")
        self.assertEqual(data["groups"]["kb"], 2)
        self.assertEqual(data["groups"]["card"], 2)
        self.assertEqual(data["groups"]["note"], 1)

    def test_title_score_order(self):
        s = search.title_score
        self.assertGreater(s("plan", "Plan"), s("plan", "Planning day"))
        self.assertGreater(s("plan", "Planning day"), s("plan", "My plan"))
        self.assertGreater(s("plan", "My plan"), s("plan", "Complan"))
        self.assertGreater(s("plan", "Complan"), s("plan", "plna"))
        self.assertGreater(s("plan", "plna"), 0)
        self.assertEqual(s("plan", "zzz"), 0)

    def test_ranking_is_stable(self):
        self.fake.answers["/api/kb/search"] = {"results": [item(f"note {i} about trip", kind="kb") for i in range(8)]}
        first = [r["title"] for r in self.get("trip").json()["results"]]
        for _ in range(3):
            self.assertEqual([r["title"] for r in self.get("trip").json()["results"]], first)
        self.assertEqual(first, [f"note {i} about trip" for i in range(8)])

    def test_sources_run_in_parallel(self):
        for _, routes, kind in search.SOURCES:
            self.fake.answers[routes[0]] = {"results": [item(f"{kind} thing", kind=kind)]}
            self.fake.delays[routes[0]] = 0.4
        started = time.perf_counter()
        data = self.get("thing").json()
        self.assertLess(time.perf_counter() - started, 1.6)          # eleven 0.4 s sources in series would be 4.4 s
        self.assertEqual(len(data["results"]), len(search.SOURCES))

    def test_a_slow_source_is_dropped_after_the_timeout(self):
        self.build(timeout=0.3)
        self.fake.answers["/api/kb/search"] = {"results": [item("fast kb hit", kind="kb")]}
        self.fake.answers["/api/fileops/search"] = {"results": [item("slow hit", kind="file")]}
        self.fake.delays["/api/fileops/search"] = 5
        started = time.perf_counter()
        data = self.get("hit").json()
        self.assertLess(time.perf_counter() - started, 2.5)
        self.assertEqual([r["title"] for r in data["results"]], ["fast kb hit"])
        self.assertEqual(data["timed_out"], ["fileops"])

    def test_broken_missing_and_malformed_sources_are_ignored(self):
        self.fake.answers["/api/kb/search"] = Exception
        self.fake.answers["/api/cards/search"] = {"results": "not a list"}
        self.fake.answers["/api/inbox/search"] = ["not", "a", "dict"]
        self.fake.answers["/api/reminders/search"] = {"results": [
            None, 5, "str", {"title": ""}, {"nope": 1}, {"title": "Dentist call", "kind": "BAD KIND!", "open": "x"},
            {"title": "Dentist <script>alert(1)</script>", "snippet": "s" * 2000, "kind": "reminder",
             "open": {"view": "reminders", "id": "r1", "evil": "x", "section": None}}]}
        data = self.get("dentist").json()
        by_title = {r["title"]: r for r in data["results"]}
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(by_title["Dentist call"]["kind"], "reminder")     # the bad kind fell back to the source's
        self.assertEqual(by_title["Dentist call"]["open"], {})
        weird = by_title["Dentist <script>alert(1)</script>"]
        self.assertEqual(weird["open"], {"view": "reminders", "id": "r1"})
        self.assertEqual(len(weird["snippet"]), 300)

    def test_everything_missing_still_200(self):
        data = self.get("anything").json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["groups"], {})

    def test_duplicates_are_merged(self):
        same = item("Pay rent", kind="todo", view="todos", id="7")
        self.fake.answers["/api/productivity/todos/search"] = {"results": [same, dict(same)]}
        self.fake.answers["/api/kb/search"] = {"results": [item("Pay rent", kind="todo", view="todos", id="7"),
                                                           item("Pay rent", kind="todo", view="todos", id="8")]}
        data = self.get("rent").json()
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["groups"], {"todo": 2})

    def test_kinds_filter_skips_other_sources(self):
        self.fake.answers["/api/kb/search"] = {"results": [item("alpha kb", kind="kb")]}
        self.fake.answers["/api/cards/search"] = {"results": [item("alpha card", kind="card")]}
        self.notes.add("alpha note body")
        data = self.get("alpha", kinds="cards").json()
        self.assertEqual([r["title"] for r in data["results"]], ["alpha card"])
        self.assertEqual(self.fake.hits["/api/kb/search"], 0)
        data = self.get("alpha", kinds="note,kb").json()
        self.assertEqual({r["kind"] for r in data["results"]}, {"note", "kb"})
        self.assertEqual(self.fake.hits["/api/cards/search"], 1)   # only the first request asked cards

    def test_q_length_and_limit(self):
        self.assertEqual(self.get("a").status_code, 400)
        self.assertEqual(self.get("   ").status_code, 400)
        self.assertEqual(self.get("").status_code, 400)
        self.assertEqual(self.get("x" * 201).status_code, 400)
        self.assertEqual(self.get("x" * 200).status_code, 200)
        self.fake.answers["/api/kb/search"] = {"results": [item(f"match {i}", kind="kb") for i in range(20)]}
        self.fake.answers["/api/cards/search"] = {"results": [item(f"match c{i}", kind="card") for i in range(20)]}
        data = self.get("match", limit=5).json()
        self.assertEqual(len(data["results"]), 5)
        self.assertEqual(data["total"], 40)
        self.assertEqual(data["groups"], {"kb": 20, "card": 20})
        self.assertEqual(len(self.get("match", limit=1000).json()["results"]), 40)
        self.assertEqual(self.get("match", limit=0).status_code, 422)

    def test_the_query_goes_to_the_sources_as_a_parameter(self):
        seen = []

        class Spy:
            async def call(self, method, path, *, params=None, body=None):
                seen.append((method, path, params))
                return False, "nope"

        self.build(client=Spy())
        self.get("a&b=1 ; drop")
        self.assertTrue(all(m == "GET" and p == {"q": "a&b=1 ; drop"} for m, _, p in seen))
        self.assertEqual({path for _, path, _ in seen}, {r for _, routes, _ in search.SOURCES for r in routes})

    def test_the_older_route_names_are_used_when_the_new_ones_are_missing(self):
        self.fake.answers["/api/productivity/todos/search"] = {"results": [item("Fallback todo", kind="todo")]}
        self.fake.answers["/api/bookmarks/search"] = {"results": [item("Real bookmark todo", kind="bookmark")]}
        data = self.get("todo").json()
        self.assertEqual({r["title"] for r in data["results"]}, {"Fallback todo", "Real bookmark todo"})

    def test_notes_are_searched_by_title_tag_and_body(self):
        a = self.notes.add("Buy oat milk\n\nand some bread")
        b = self.notes.add("Meeting about the roadmap\n\nlong text with oat budget inside")
        self.notes.edit(b.id, tags=["finance"])
        c = self.notes.add("something else entirely")
        data = self.get("oat").json()
        self.assertEqual({r["open"]["id"] for r in data["results"]}, {a.id, b.id})
        self.assertTrue(all(r["open"]["view"] == "notes" for r in data["results"]))
        self.assertIn("oat", [r for r in data["results"] if r["open"]["id"] == b.id][0]["snippet"])
        self.assertEqual([r["open"]["id"] for r in self.get("finance").json()["results"]], [b.id])
        self.assertEqual(self.get("zzzzz").json()["results"], [])
        self.assertNotIn(c.id, json.dumps(data))

    def test_apps_scripts_and_settings(self):
        data = self.get("chrome").json()
        self.assertEqual([(r["kind"], r["title"]) for r in data["results"]], [("app", "Chrome")])
        data = self.get("backup").json()
        kinds = {r["kind"]: r for r in data["results"]}
        self.assertIn("button", kinds)
        self.assertEqual(kinds["setting"]["open"], {"view": "settings", "section": "backup"})
        self.assertEqual(self.get("ntfy").json()["results"][0]["open"]["section"], "notifications")
        self.assertEqual(self.get("diag").json()["results"][0]["title"], "Diagnostics")

    def test_search_all_alias(self):
        self.assertEqual(self.client.get("/api/search/all", params={"q": "chrome"}).json()["results"][0]["title"], "Chrome")

    def test_file_search_by_root_still_works(self):
        data = self.client.get("/api/search", params={"root": "docs", "q": "a"}).json()
        self.assertEqual([r["name"] for r in data["results"]], ["a.md"])
        self.assertEqual(self.client.get("/api/search", params={"root": "nope", "q": "a"}).status_code, 404)
        escape = self.client.get("/api/search", params={"root": "docs", "q": "a", "path": "../.."})
        self.assertEqual(escape.status_code, 403)

    def test_recent_queries(self):
        self.assertEqual(self.client.get("/api/search/suggest").json(), {"recent": [], "suggestions": []})
        for q in ["alpha", "beta", "ALPHA", "gamma"]:
            self.client.post("/api/search/recent", json={"q": q})
        self.assertEqual(self.client.get("/api/search/suggest").json()["recent"], ["gamma", "ALPHA", "beta"])
        self.assertEqual(self.client.get("/api/search/suggest", params={"q": "be"}).json()["recent"], ["beta"])
        for i in range(15):
            self.client.post("/api/search/recent", json={"q": f"query {i}"})
        recent = self.client.get("/api/search/suggest").json()["recent"]
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0], "query 14")
        self.assertEqual(self.client.post("/api/search/recent", json={"q": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/search/recent", json={"q": "y" * 201}).status_code, 400)
        self.assertEqual(self.client.delete("/api/search/recent").json(), {"recent": []})
        self.assertEqual(self.client.get("/api/search/suggest").json()["recent"], [])

    def test_suggestions_include_titles(self):
        self.assertIn("Backup", self.client.get("/api/search/suggest", params={"q": "ba"}).json()["suggestions"])
        self.assertIn("Chrome", self.client.get("/api/search/suggest", params={"q": "chr"}).json()["suggestions"])


HOME_ANSWERS = {
    "/api/status": {"hostname": "pc", "battery": {"percent": 80, "plugged": True}, "cpu_percent": 12.5,
                    "memory": {"percent": 40.0, "total_gb": 16}},
    "/api/work/status": {"asana": {"configured": True, "user": "Priya Sharma"}},
    "/api/work/calendar": {"meetings": [{"title": "Standup", "start": "2026-11-04T09:30:00", "end": "2026-11-04T09:45:00",
                                         "all_day": False, "link": "https://meet.example/x", "platform": "meet"}]},
    "/api/work/asana/tasks": {"tasks": [
        {"gid": "1", "name": "File taxes", "due_on": "2026-11-01", "completed": False},
        {"gid": "2", "name": "Ship feature", "due_on": "2026-11-04", "completed": False},
        {"gid": "3", "name": "Done thing", "due_on": "2026-11-02", "completed": True},
        {"gid": "4", "name": "No date", "due_on": None, "completed": False}]},
    "/api/todos": {"tasks": [{"id": "t1", "title": "Buy milk", "due": "2026-11-05", "done": False},
                             {"id": "t2", "title": "Old", "due": None, "done": True}]},
    "/api/reminders": {"reminders": [{"id": "r2", "title": "Call mum", "when": "2026-11-04T18:00:00"},
                                     {"id": "r1", "title": "Take pill", "when": "2026-11-04T12:00:00"},
                                     {"id": "r0", "title": "Done one", "when": "2026-11-03T12:00:00", "done": True}]},
    "/api/notifications": {"unread": 2, "notifications": [{"id": "n1", "title": "Backup done", "body": "ok", "read": False},
                                                          {"id": "n2", "title": "Hi", "read": True}]},
    "/api/learn/history": {"streak_days": 4, "quizzes": []},
    "/api/english/overview": {"streak_days": 2},
    "/api/focus/stats": {"streak_days": 1, "today_minutes": 25},
    "/api/productivity/summary": {"todos_due_today": 1, "overdue": 3, "focus_today_minutes": 25, "habits_done_today": 1,
                                  "habits_total": 3, "best_streak": 6, "journal_streak": 0, "big": list(range(500))},
    "/api/journal/stats": {"by_day": [{"date": "2026-11-03", "entries": 1}, {"date": "2026-11-04", "entries": 0}]},
    "/api/life/summary": {"bookmarks_unread": 4, "month_spend": 123.5, "currency": "USD",
                          "next_countdown": {"id": "c1", "title": "Trip", "days_remaining": 5}},
    "/api/cards/summary": {"due_today": 7, "streak": 3, "roadmap_progress": [{"id": "r", "title": "Rust", "percent": 10}]},
    "/api/jarvis/status": {"configured": True, "running": True},
}


class HomeTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.answers.update(json.loads(json.dumps(HOME_ANSWERS)))

    def home(self, **params):
        response = self.client.get("/api/home", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_everything_present(self):
        self.notes.add("First note")
        self.notes.add("Second note")
        data = self.home()
        self.assertEqual(data["sections_failed"], [])
        self.assertEqual(data["greeting"], {"period": "morning", "name": "Priya"})
        self.assertEqual(data["now"], "2026-11-04T09:00:00")
        self.assertEqual(data["laptop"], {"online": True, "battery": {"percent": 80, "plugged": True}, "cpu": 12.5, "mem": 40.0})
        self.assertEqual(data["agenda"][0]["title"], "Standup")
        self.assertEqual([t["title"] for t in data["tasks"]], ["File taxes", "Ship feature", "Buy milk", "No date"])
        self.assertTrue(data["tasks"][0]["overdue"])
        self.assertFalse(data["tasks"][1]["overdue"])
        self.assertEqual([r["title"] for r in data["reminders"]], ["Take pill", "Call mum"])
        self.assertEqual(data["notifications"]["unread"], 2)
        self.assertEqual(data["notifications"]["latest"][0]["title"], "Backup done")
        self.assertEqual(data["streaks"], {"learn": 4, "english": 2, "habits": 6, "focus": 1, "journal": 0})
        self.assertEqual(data["summaries"]["cards"], {"due_today": 7, "streak": 3,
                                                      "roadmap_progress": [{"id": "r", "title": "Rust", "percent": 10}]})
        self.assertEqual(data["summaries"]["life"]["month_spend"], 123.5)
        self.assertEqual(data["summaries"]["life"]["next_countdown"]["title"], "Trip")
        self.assertNotIn("big", data["summaries"]["productivity"])        # long lists are not copied into Home
        self.assertEqual(data["jarvis"], {"running": True})
        self.assertEqual({n["title"] for n in data["recent_notes"]}, {"First note", "Second note"})
        self.assertEqual(data["recent_notes"][0]["open"]["view"], "notes")
        self.assertLessEqual(len(data["suggestions"]), 4)

    def test_tasks_are_capped_at_five(self):
        self.fake.answers["/api/work/asana/tasks"] = {"tasks": [{"gid": str(i), "name": f"T{i}", "due_on": f"2026-12-0{i}"}
                                                                for i in range(1, 9)]}
        self.assertEqual(len(self.home()["tasks"]), 5)

    def test_suggestions_are_rule_based(self):
        data = self.home()
        text = " | ".join(data["suggestions"])
        self.assertIn("Standup starts at 09:30", data["suggestions"][0])
        self.assertIn("overdue task \"File taxes\" and 2 more", text)
        self.assertIn("7 flashcards are due", text)
        self.assertEqual(len(data["suggestions"]), 4)                      # capped; habits and journal come after
        self.fake.answers["/api/work/calendar"] = {"meetings": []}
        self.fake.answers["/api/work/asana/tasks"] = {"tasks": []}
        self.fake.answers["/api/cards/summary"] = {"due_today": 0}
        self.fake.answers["/api/productivity/summary"] = {**HOME_ANSWERS["/api/productivity/summary"], "overdue": 0}
        self.clock_value[0] += 100
        text = " | ".join(self.home()["suggestions"])
        self.assertIn("2 habits are not checked off", text)
        self.assertIn("Write today's journal entry.", text)
        self.assertNotIn("flashcard", text)
        self.assertNotIn("overdue", text)

    def test_demo_app_with_no_modules_at_all(self):
        self.fake.answers.clear()
        data = self.home()
        self.assertEqual(data["agenda"], [])
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["reminders"], [])
        self.assertEqual(data["notifications"], {"unread": 0, "latest": []})
        self.assertEqual(data["streaks"], {"learn": None, "english": None, "habits": None, "focus": None, "journal": None})
        self.assertEqual(data["summaries"], {"productivity": None, "life": None, "cards": None})
        self.assertEqual(data["jarvis"], {"running": False})
        self.assertEqual(data["suggestions"], [])
        self.assertEqual(data["laptop"]["online"], True)
        self.assertEqual(data["greeting"]["period"], "morning")
        self.assertEqual(set(data["sections_failed"]),
                         {"laptop", "agenda", "tasks", "reminders", "notifications", "streaks", "summaries", "jarvis"})

    def test_partial_failure_names_the_broken_sections_only(self):
        self.fake.answers["/api/work/calendar"] = Exception
        self.fake.answers["/api/reminders"] = Exception
        del self.fake.answers["/api/notifications"]
        data = self.home()
        self.assertEqual(set(data["sections_failed"]), {"agenda", "reminders", "notifications"})
        self.assertEqual(data["agenda"], [])
        self.assertEqual(data["tasks"][0]["title"], "File taxes")          # the rest is intact
        self.assertEqual(data["streaks"]["learn"], 4)

    def test_tasks_survive_with_one_source_down(self):
        self.fake.answers["/api/work/asana/tasks"] = Exception
        data = self.home()
        self.assertEqual([t["title"] for t in data["tasks"]], ["Buy milk"])
        self.assertNotIn("tasks", data["sections_failed"])

    def test_the_older_route_names_still_feed_home(self):
        for new, old in (("/api/todos", "/api/productivity/todos"), ("/api/notifications", "/api/notify")):
            self.fake.answers[old] = self.fake.answers.pop(new)
        data = self.home()
        self.assertEqual(data["sections_failed"], [])
        self.assertIn("Buy milk", [t["title"] for t in data["tasks"]])
        self.assertEqual(data["notifications"]["unread"], 2)

    def test_a_slow_section_does_not_hold_up_home(self):
        self.build(section_timeout=0.3)
        self.fake.answers.update(json.loads(json.dumps(HOME_ANSWERS)))
        self.fake.delays["/api/reminders"] = 5
        started = time.perf_counter()
        data = self.home()
        self.assertLess(time.perf_counter() - started, 3)
        self.assertEqual(data["sections_failed"], ["reminders"])
        self.assertEqual(data["reminders"], [])

    def test_sections_are_cached_for_20_seconds(self):
        self.home()
        first = Counter(self.fake.hits)
        self.clock_value[0] += 19
        self.home()
        self.assertEqual(self.fake.hits, first)
        self.clock_value[0] += 2
        self.home()
        self.assertEqual(self.fake.hits["/api/reminders"], 2)
        self.assertEqual(self.fake.hits["/api/status"], 2)

    def test_a_failed_section_is_retried_at_once(self):
        self.fake.answers["/api/reminders"] = Exception
        self.assertEqual(self.home()["sections_failed"], ["reminders"])
        self.fake.answers["/api/reminders"] = HOME_ANSWERS["/api/reminders"]
        data = self.home()
        self.assertEqual(data["sections_failed"], [])
        self.assertEqual(len(data["reminders"]), 2)

    def test_shared_calls_are_made_once_per_request(self):
        self.home()
        self.assertEqual(self.fake.hits["/api/productivity/summary"], 1)   # used by streaks and summaries

    def test_odd_shapes_do_not_break_home(self):
        self.fake.answers["/api/reminders"] = "a string"
        self.fake.answers["/api/notifications"] = [{"title": "plain list", "read": False}, "junk", None]
        self.fake.answers["/api/cards/summary"] = [1, 2]
        self.fake.answers["/api/work/calendar"] = {"meetings": [None, {"nope": 1}, {"title": "Ok", "start": "garbage"}]}
        data = self.home()
        self.assertEqual(data["reminders"], [])
        self.assertEqual(data["notifications"]["unread"], 1)
        self.assertEqual([m["title"] for m in data["agenda"]], ["Ok"])
        self.assertEqual(data["summaries"]["cards"], {})

    def test_model_parameter_is_accepted(self):
        self.assertEqual(self.client.get("/api/home", params={"model": "claude"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
