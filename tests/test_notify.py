"""Tests for the notification center: the list, read/clear, retention, the live stream and the optional
ntfy phone push. Nothing here touches the network: the phone push goes through an injected fake sender."""
import asyncio
import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import notify
from hub.common import Ctx
from hub.server import create_app
from tests.helpers import TOKEN, make_config

logging.getLogger("hub.notify").addHandler(logging.NullHandler())   # expected failures stay quiet


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


def original_notify(*args, **kwargs):
    return None


class FakeClock:
    def __init__(self, ts=1_800_000_000.0):
        self.ts = ts

    def __call__(self):
        return self.ts


def local_epoch(hour, minute=0):
    return datetime(2026, 11, 4, hour, minute).astimezone().timestamp()


class Base(unittest.TestCase):
    def make(self, **config_overrides):
        self.config, self.root = make_config(data_dir="", **config_overrides)
        self.config.data_dir = str(self.root / "data")
        self.audits = []
        self.sent = []
        self.send_error = None
        self.clock = FakeClock()

        def sender(url, headers, content):
            self.sent.append((url, headers, content))
            if self.send_error:
                raise self.send_error

        self.ctx = Ctx(config=self.config, model_for=None, transcriber=None, notes=None, need_auth=need_auth,
                       audit=lambda event, **fields: self.audits.append((event, fields)), notify=original_notify)
        self.router = notify.make_router(self.ctx, clock=self.clock, sender=sender)
        self.center = self.router.center
        app = FastAPI()
        app.include_router(self.router)
        self.client = TestClient(app, base_url="http://testserver", cookies={"hub_session": "yes"})
        self.addCleanup(self.client.close)

    def setUp(self):
        self.make()

    def push(self, title="Hello", **kw):
        return self.ctx.notify(title, **kw)


class ListTest(Base):
    def test_ctx_notify_is_replaced_and_usable_from_any_thread(self):
        self.assertIsNot(self.ctx.notify, original_notify)
        threads = [threading.Thread(target=lambda i=i: [self.ctx.notify(f"t{i}-{j}", source="test") for j in range(20)])
                   for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        items = self.client.get("/api/notifications?limit=200").json()["notifications"]
        self.assertEqual(len(items), 160)
        self.assertEqual(len({i["id"] for i in items}), 160)

    def test_newest_first_with_filters(self):
        self.push("first", source="a")
        self.push("second", source="b", level="warn")
        self.push("third", source="a")
        self.client.post("/api/notifications/read", json={"ids": [1]})
        data = self.client.get("/api/notifications").json()
        self.assertEqual([i["title"] for i in data["notifications"]], ["third", "second", "first"])
        self.assertEqual(data["unread"], 2)
        self.assertEqual([i["title"] for i in self.client.get("/api/notifications?unread_only=true").json()["notifications"]],
                         ["third", "second"])
        self.assertEqual([i["title"] for i in self.client.get("/api/notifications?source=a").json()["notifications"]],
                         ["third", "first"])
        self.assertEqual(len(self.client.get("/api/notifications?limit=1").json()["notifications"]), 1)
        self.assertEqual(self.client.get("/api/notifications").json()["notifications"][1]["level"], "warn")

    def test_unread_read_and_read_all(self):
        for i in range(3):
            self.push(f"n{i}")
        self.assertEqual(self.client.get("/api/notifications/unread-count").json(), {"count": 3})
        self.assertEqual(self.client.post("/api/notifications/read", json={"ids": [1, 2]}).json()["updated"], 2)
        self.assertEqual(self.client.get("/api/notifications/unread-count").json(), {"count": 1})
        self.assertEqual(self.client.post("/api/notifications/read", json={"all": True}).json()["updated"], 1)
        self.assertEqual(self.client.get("/api/notifications/unread-count").json(), {"count": 0})

    def test_read_needs_ids_or_all(self):
        for body in ({}, {"ids": []}, {"ids": ["1"]}, {"ids": [True]}, {"ids": "all"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/notifications/read", json=body).status_code, 400)

    def test_clear_needs_confirm(self):
        self.push("keep me")
        for body in ({}, {"confirm": False}, {"confirm": "yes"}):
            self.assertEqual(self.client.post("/api/notifications/clear", json=body).status_code, 409)
        self.assertEqual(len(self.client.get("/api/notifications").json()["notifications"]), 1)
        self.assertEqual(self.client.post("/api/notifications/clear", json={"confirm": True}).json(), {"removed": 1})
        self.assertEqual(self.client.get("/api/notifications").json()["notifications"], [])
        self.assertIn("notifications_cleared", [e for e, _ in self.audits])

    def test_delete_one(self):
        item = self.push("bye")
        self.assertEqual(self.client.delete(f"/api/notifications/{item['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/notifications/{item['id']}").status_code, 404)
        self.assertEqual(self.client.delete("/api/notifications/abc").status_code, 404)

    def test_everything_needs_a_login(self):
        anonymous = TestClient(self.client.app, base_url="http://testserver")
        for method, path in (("get", "/api/notifications"), ("get", "/api/notifications/unread-count"),
                             ("get", "/api/notifications/stream"), ("get", "/api/notifications/settings"),
                             ("post", "/api/notifications/test"), ("post", "/api/notifications/clear"),
                             ("post", "/api/notifications/settings"), ("delete", "/api/notifications/1")):
            with self.subTest(path=path):
                self.assertEqual(getattr(anonymous, method)(path).status_code, 401)

    def test_the_test_button_pushes_one(self):
        item = self.client.post("/api/notifications/test").json()
        self.assertEqual(item["source"], "notify")
        self.assertEqual(self.client.get("/api/notifications/unread-count").json()["count"], 1)

    def test_keeps_only_the_latest_500_and_drops_old_ones(self):
        for i in range(510):
            self.push(f"n{i}")
        items = self.center.list(limit=200)
        self.assertEqual(items[0]["title"], "n509")
        with self.center._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 500)
            self.assertEqual(db.execute("SELECT MIN(id) FROM notifications").fetchone()[0], 11)
        self.clock.ts += 61 * 86400
        self.push("fresh")
        self.assertEqual([i["title"] for i in self.center.list(limit=200)], ["fresh"])

    def test_text_is_cleaned_and_unsafe_links_are_dropped(self):
        item = self.push("Line one\nline two\x07" + "x" * 500, body="a\x00b", level="loud", link="javascript:alert(1)")
        self.assertEqual(item["level"], "info")
        self.assertIsNone(item["link"])
        self.assertNotIn("\n", item["title"])
        self.assertNotIn("\x07", item["title"])
        self.assertEqual(len(item["title"]), 200)
        self.assertEqual(item["body"], "ab")
        self.assertEqual(self.push("a", link="reminders")["link"], "reminders")
        self.assertEqual(self.push("a", link="https://example.com/x")["link"], "https://example.com/x")
        self.assertIsNone(self.push("a", link="data:text/html,hi")["link"])

    def test_a_broken_store_never_raises_into_the_caller(self):
        with mock.patch.object(self.center, "_connect", side_effect=OSError("disk full")):
            self.assertIsNone(self.push("lost"))

    def test_persists_between_restarts(self):
        self.push("remember me")
        other = notify.NotifyCenter(self.center.db_path, self.center.settings.path)
        self.assertEqual(other.list()[0]["title"], "remember me")


class PhonePushTest(Base):
    TOPIC = "my-long-random-topic-123"

    def enable(self, **extra):
        response = self.client.post("/api/notifications/settings", json={"ntfy_topic": self.TOPIC, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_nothing_is_sent_without_a_topic(self):
        self.push("quiet")
        self.center.wait_for_pushes()
        self.assertEqual(self.sent, [])

    def test_sends_title_body_priority_and_a_safe_title_header(self):
        self.enable()
        self.push("Reminder: café ☕", body="Due 9:00 am", level="warn", link="https://example.com/go")
        self.center.wait_for_pushes()
        (url, headers, content), = self.sent
        self.assertEqual(url, f"https://ntfy.sh/{self.TOPIC}")
        self.assertTrue(headers["Title"].isascii())
        self.assertEqual(headers["Priority"], "4")
        self.assertEqual(headers["Click"], "https://example.com/go")
        self.assertEqual(content, b"Due 9:00 am")

    def test_an_in_app_view_name_is_not_sent_as_a_click_link(self):
        self.enable()
        self.push("x", link="reminders")
        self.center.wait_for_pushes()
        self.assertNotIn("Click", self.sent[0][1])

    def test_config_topic_is_used_when_settings_have_none(self):
        self.make(ntfy_topic="config-topic-abcdef", ntfy_server="https://ntfy.example.org/")
        self.push("hi")
        self.center.wait_for_pushes()
        self.assertEqual(self.sent[0][0], "https://ntfy.example.org/config-topic-abcdef")

    def test_failures_are_logged_not_raised(self):
        self.enable()
        self.send_error = OSError("network down")
        with self.assertLogs("hub.notify", level="WARNING") as logs:
            item = self.push("still stored")
            self.center.wait_for_pushes()
        self.assertIsNotNone(item)
        self.assertNotIn(self.TOPIC, "".join(logs.output))
        self.assertEqual(self.audits[-1][0], "ntfy_push")
        self.assertFalse(self.audits[-1][1]["ok"])

    def test_the_real_sender_has_a_five_second_timeout_and_no_redirects(self):
        with mock.patch("httpx.post") as post:
            notify._http_send("https://ntfy.sh/topic12345", {"Title": "x"}, b"body")
        self.assertEqual(post.call_args.kwargs["timeout"], 5.0)
        self.assertFalse(post.call_args.kwargs["follow_redirects"])

    def test_quiet_hours_stop_the_push_but_not_the_list(self):
        self.enable(quiet_hours={"start": "22:00", "end": "07:00"})
        self.clock.ts = local_epoch(23, 30)
        self.push("late")
        self.center.wait_for_pushes()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.center.list()[0]["title"], "late")
        self.clock.ts = local_epoch(6, 59)
        self.push("early")
        self.center.wait_for_pushes()
        self.assertEqual(self.sent, [])
        self.clock.ts = local_epoch(12, 0)
        self.push("noon")
        self.center.wait_for_pushes()
        self.assertEqual(len(self.sent), 1)

    def test_quiet_hours_inside_one_day(self):
        self.enable(quiet_hours={"start": "13:00", "end": "14:00"})
        self.clock.ts = local_epoch(13, 30)
        self.push("lunch")
        self.center.wait_for_pushes()
        self.assertEqual(self.sent, [])

    def test_minimum_level(self):
        self.enable(min_level="warn")
        self.push("info only")
        self.push("a warning", level="warn")
        self.center.wait_for_pushes()
        self.assertEqual(len(self.sent), 1)

    def test_settings_validation(self):
        for body in ({"ntfy_topic": "short"}, {"ntfy_topic": "has spaces in it ok"}, {"ntfy_topic": "x" * 65},
                     {"ntfy_topic": "bad/slash/topic1"}, {"ntfy_topic": 12345678}, {"quiet_hours": {"start": "25:00", "end": "07:00"}},
                     {"quiet_hours": {"start": "22:00", "end": "22:00"}}, {"quiet_hours": "night"}, {"min_level": "loud"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/notifications/settings", json=body).status_code, 400)
        self.assertFalse(self.client.get("/api/notifications/settings").json()["ntfy_enabled"])

    def test_the_topic_is_never_echoed_or_audited(self):
        view = self.enable()
        self.assertTrue(view["ntfy_enabled"])
        self.assertNotIn(self.TOPIC, json.dumps(view))
        self.assertNotIn(self.TOPIC, json.dumps(self.client.get("/api/notifications/settings").json()))
        self.assertNotIn(self.TOPIC, json.dumps(self.audits))
        self.assertEqual(self.audits[-1], ("notify_settings_changed", {"fields": ["ntfy_topic"]}))

    def test_clearing_the_topic_turns_the_push_off(self):
        self.enable()
        self.assertFalse(self.client.post("/api/notifications/settings", json={"ntfy_topic": ""}).json()["ntfy_enabled"])
        self.push("x")
        self.center.wait_for_pushes()
        self.assertEqual(self.sent, [])


class StreamTest(Base):
    def run_async(self, coroutine):
        return asyncio.run(asyncio.wait_for(coroutine, 10))

    def test_a_pushed_item_arrives_on_the_stream(self):
        async def scenario():
            stream = self.center.stream(poll=0.05)
            self.assertEqual(await anext(stream), "retry: 3000\n\n")
            next_frame = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(0.05)
            await asyncio.to_thread(self.ctx.notify, "Live one", "body", "info", "reminders", "test")
            frame = await next_frame
            await stream.aclose()
            return frame

        frame = self.run_async(scenario())
        self.assertTrue(frame.startswith("id: 1\ndata: "))
        self.assertEqual(json.loads(frame.split("data: ", 1)[1])["title"], "Live one")
        self.assertEqual(self.center.subscriber_count(), 0)

    def test_keepalive_comments_while_quiet(self):
        async def scenario():
            stream = self.center.stream(keepalive=0.1, poll=0.02)
            await anext(stream)
            frame = await anext(stream)
            await stream.aclose()
            return frame

        self.assertEqual(self.run_async(scenario()), ": keepalive\n\n")

    def test_reconnect_replays_what_was_missed(self):
        for i in range(3):
            self.push(f"n{i}")

        async def scenario():
            stream = self.center.stream(1, poll=0.05)
            frames = [await anext(stream) for _ in range(3)]
            await stream.aclose()
            return frames

        frames = self.run_async(scenario())
        self.assertEqual([f.split("\n")[0] for f in frames[1:]], ["id: 2", "id: 3"])

    def test_the_route_streams_and_closes_cleanly_when_the_client_leaves(self):
        async def scenario():
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                     "method": "GET", "scheme": "http", "path": "/api/notifications/stream", "raw_path": b"/api/notifications/stream",
                     "query_string": b"", "root_path": "", "server": ("testserver", 80), "client": ("1.2.3.4", 5),
                     "headers": [(b"host", b"testserver"), (b"cookie", b"hub_session=yes")]}
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            task = asyncio.create_task(self.client.app(scope, incoming.get, outgoing.put))
            start = await outgoing.get()
            self.assertEqual(start["status"], 200)
            self.assertIn(b"text/event-stream", dict(start["headers"])[b"content-type"])
            await outgoing.get()   # the retry line
            await asyncio.sleep(0.1)
            self.assertEqual(self.center.subscriber_count(), 1)
            await asyncio.to_thread(self.ctx.notify, "Over the wire", source="test")
            chunk = await outgoing.get()
            self.assertIn(b"Over the wire", chunk["body"])
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 5)
            return self.center.subscriber_count()

        self.assertEqual(self.run_async(scenario()), 0)


class FullAppTest(unittest.TestCase):
    def test_the_real_app_wires_notify_so_other_modules_can_use_it(self):
        config, root = make_config()
        config.data_dir = str(root / "data")
        client = TestClient(create_app(config, audit_path=root / "audit.log"), base_url="http://testserver",
                            headers={"X-Hub": "1"})
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/notifications").status_code, 401)
        self.assertEqual(client.post("/api/login", json={"token": TOKEN}).status_code, 200)
        self.assertEqual(client.post("/api/notifications/test").status_code, 200)
        self.assertEqual(client.get("/api/notifications/unread-count").json(), {"count": 1})
        # a POST without the X-Hub header is refused by the server's middleware
        bare = TestClient(client.app, base_url="http://testserver", cookies=client.cookies)
        self.assertEqual(bare.post("/api/notifications/clear", json={"confirm": True}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
