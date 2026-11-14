"""Tests for the generalised inbound sources: task sources (hub/tasksources.py), mail accounts beyond Google (IMAP/SMTP in
hub/gmailx.py + hub/mailaccounts.py), calendar feeds (hub/calendars.py) and the generic /api/sources routes (hub/sources.py).
Everything is fake: HTTP is a scripted function, the keyring is a dict, IMAP and SMTP are stub objects, the ICS is synthetic."""
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import alerts, calendars, gmailx, mailaccounts, sources, tasksources
from hub.common import Ctx
from hub.gmailx import MemoryStore
from hub.inbox import MemoryMailBox
from tests.helpers import make_config
from tests.test_autopilot import need_auth

UTC = dt.timezone.utc
SECRET = "SuperSecretToken-0123456789"


def make_ctx(**cfg):
    tmp = Path(tempfile.mkdtemp(prefix="sources-test-"))
    config, root = make_config(data_dir=str(tmp / "data"), display_name="Shikhar", **cfg)
    audits = []
    ctx = Ctx(config=config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
              audit=lambda *a, **k: audits.append((a, k)))
    ctx.audits = audits
    return ctx, [tmp, root]


class Base(unittest.TestCase):
    def setUp(self):
        self.ctx, dirs = make_ctx()
        for d in dirs:
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)


class FakeNet:
    """A scripted HTTP layer: the first route whose key is in the URL answers."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def add(self, key, body, status=200):
        self.routes.append((key, status, body if isinstance(body, bytes) else json.dumps(body).encode()))

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        for key, status, data in self.routes:
            if key in url:
                return status, data
        return 404, b""


def registry(ctx, net, store=None, triaged=None):
    reg = tasksources.TaskSources(ctx, store=store or MemoryStore(), transport=net,
                                  triage=(lambda c, ev: triaged.append(ev) or ev) if triaged is not None else (lambda c, ev: ev))
    ctx.tasksources = reg
    return reg


# ---- task sources ---------------------------------------------------------------------------------------------

class TaskAdapters(Base):
    def add(self, reg, kind, secret=SECRET, **settings):
        return reg.add({"kind": kind, "label": kind.title(), "secret": secret, "settings": settings})

    def test_jira_baseline_then_new_update(self):
        net, got = FakeNet(), []
        issues = {"issues": [{"key": "ABC-1", "fields": {"summary": "Fix login", "updated": "2026-11-09T10:00:00.000+0000",
                                                         "status": {"name": "To Do"}, "reporter": {"displayName": "Ravi"},
                                                         "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Prod is down"}]}]}}}]}
        net.add("/rest/api/3/search/jql", issues)
        reg = registry(self.ctx, net, triaged=got)
        item = self.add(reg, "jira", site="https://acme.atlassian.net", email="me@example.com")
        self.assertEqual(reg.poll_item(item["id"]), {"events": 0, "alerts": 0})          # baseline: silent
        issues["issues"][0]["fields"]["updated"] = "2026-11-09T11:00:00.000+0000"
        net.routes[0] = ("/rest/api/3/search/jql", 200, json.dumps(issues).encode())
        self.assertEqual(reg.poll_item(item["id"])["events"], 1)
        ev = got[0]
        self.assertEqual((ev["source"], ev["sender"], ev["title"], ev["link"]), ("jira", "Ravi", "ABC-1 Fix login", "https://acme.atlassian.net/browse/ABC-1"))
        self.assertIn("Prod is down", ev["text"])
        method, url, headers, _ = net.calls[0]
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertNotIn(SECRET, url)
        self.assertIn("currentUser()", __import__("urllib.parse").parse.unquote(url))

    def test_jira_falls_back_to_the_older_endpoint(self):
        net = FakeNet()
        net.add("/search/jql", b"", 404)
        net.add("/rest/api/3/search", {"issues": []})
        reg = registry(self.ctx, net)
        item = self.add(reg, "jira", site="https://acme.atlassian.net", email="me@example.com")
        self.assertTrue(reg.test(item["id"])["ok"])

    def test_trello_cards_and_comment_notifications(self):
        net, got = FakeNet(), []
        net.add("/members/me/cards", [{"id": "c1", "name": "Ship it", "desc": "by Friday", "url": "https://trello.com/c/abc", "due": "2026-11-12T00:00:00Z"}])
        net.add("/members/me/notifications", [{"id": "n1", "type": "commentCard", "data": {"text": "please review", "card": {"name": "Ship it", "shortLink": "abc"}},
                                               "memberCreator": {"fullName": "Dana"}}])
        reg = registry(self.ctx, net, triaged=got)
        item = self.add(reg, "trello", secret="KEYKEYKEY12:TOKENTOKENTOKEN12")
        reg.poll_item(item["id"])
        net.add("/members/me/notifications", [])      # unused: routes are first-match, so rebuild instead
        net.routes = [r for r in net.routes if "notifications" not in r[0]]
        net.add("/members/me/notifications", [{"id": "n2", "type": "mentionedOnCard", "data": {"text": "@you look", "card": {"name": "Ship it", "shortLink": "abc"}},
                                               "memberCreator": {"fullName": "Dana"}}])
        reg.poll_item(item["id"])
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["sender"], got[0]["mention"], got[0]["link"]), ("Dana", True, "https://trello.com/c/abc"))
        for _m, url, headers, _b in net.calls:
            self.assertNotIn("TOKENTOKEN", url)                      # credentials travel in the header, never the URL
            self.assertIn("oauth_token", headers["Authorization"])

    def test_linear_graphql(self):
        net, got = FakeNet(), []
        node = {"id": "L1", "identifier": "ENG-5", "title": "Crash on start", "description": "stack", "url": "https://linear.app/x/issue/ENG-5",
                "updatedAt": "2026-11-09T09:00:00Z", "dueDate": "2026-11-10", "state": {"type": "started"}, "creator": {"name": "Priya"}}
        done = dict(node, id="L2", identifier="ENG-6", state={"type": "completed"})
        net.add("api.linear.app/graphql", {"data": {"viewer": {"assignedIssues": {"nodes": [node, done]}}}})
        reg = registry(self.ctx, net, triaged=got)
        item = self.add(reg, "linear")
        reg.poll_item(item["id"])
        node["updatedAt"] = "2026-11-09T12:00:00Z"
        net.routes = [("api.linear.app/graphql", 200, json.dumps({"data": {"viewer": {"assignedIssues": {"nodes": [node, done]}}}}).encode())]
        reg.poll_item(item["id"])
        self.assertEqual([e["title"] for e in got], ["ENG-5 Crash on start"])           # the completed issue is skipped
        method, url, headers, body = net.calls[0]
        self.assertEqual((method, headers["Authorization"]), ("POST", SECRET))
        self.assertIn(b"assignedIssues", body)

    def test_github_notifications(self):
        net, got = FakeNet(), []
        row = {"id": "9", "reason": "mention", "updated_at": "2026-11-09T10:00:00Z", "repository": {"full_name": "acme/app"},
               "subject": {"title": "Flaky test", "type": "PullRequest", "url": "https://api.github.com/repos/acme/app/pulls/12"}}
        net.add("api.github.com/notifications", [row])
        reg = registry(self.ctx, net, triaged=got)
        item = self.add(reg, "github")
        reg.poll_item(item["id"])
        net.routes = [("api.github.com/notifications", 200, json.dumps([row, dict(row, id="10", reason="review_requested")]).encode())]
        reg.poll_item(item["id"])
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["link"], got[0]["kind_hint"]), ("https://github.com/acme/app/pull/12", "task"))
        self.assertIn("participating=true", net.calls[0][1])
        self.assertEqual(net.calls[0][2]["Authorization"], "Bearer " + SECRET)

    def test_rss_and_atom(self):
        rss = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
               b'<item><guid>g1</guid><title>Deploy failed</title><link>https://ex.com/1</link><description>&lt;b&gt;prod&lt;/b&gt; is down</description></item></channel></rss>')
        atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Build red</title><link href="https://ex.com/a"/>'
                b'<author><name>CI</name></author><summary>broken</summary></entry></feed>')
        for body, title, sender in ((rss, "Deploy failed", "Status page"), (atom, "Build red", "CI")):
            net, got = FakeNet(), []
            net.add("ex.com/feed", body)
            reg = registry(self.ctx, net, triaged=got)
            item = reg.add({"kind": "rss", "label": "Status page", "settings": {"url": "https://ex.com/feed"}})
            reg.poll_item(item["id"])
            self.assertEqual(got, [])                                                  # baseline
            net.routes = [("ex.com/feed", 200, body.replace(b"</channel>", b'<item><guid>g2</guid><title>Another</title></item></channel>')
                          .replace(b"</feed>", b"<entry><id>a2</id><title>Another</title></entry></feed>"))]
            reg.poll_item(item["id"])
            self.assertEqual(got[0]["title"], "Another")
            self.ctx.tasksources = None

    def test_rss_refuses_entities_and_non_feeds(self):
        with self.assertRaises(tasksources.SourceProblem):
            tasksources.parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel><item><title>&a;</title></item></channel></rss>')
        with self.assertRaises(tasksources.SourceProblem):
            tasksources.parse_feed(b"<html>nope")

    def test_asana_item_uses_the_existing_poller_with_its_own_token(self):
        net, got = FakeNet(), []
        net.add("/users/me", {"data": {"gid": "1", "name": "Me", "workspaces": [{"gid": "55", "name": "W"}]}})
        net.add("/tasks/100/stories", {"data": []})
        tasks = {"data": [{"gid": "100", "name": "Write report", "modified_at": "t1", "permalink_url": "https://app.asana.com/0/1/100"}]}
        net.add("/tasks?", tasks)
        reg = registry(self.ctx, net, triaged=got)
        item = reg.add({"kind": "asana", "label": "Client Asana", "secret": "1/1234567890123456:abcdefabcdefabcdef"})
        reg.poll_item(item["id"])
        net.routes = [r for r in net.routes if r[0] != "/tasks?"]
        net.add("/tasks?", {"data": tasks["data"] + [{"gid": "101", "name": "Send invoice", "modified_at": "t2"}]})
        reg.poll_item(item["id"])
        self.assertEqual([e["title"] for e in got], ["Send invoice"])
        self.assertEqual(got[0]["source"], "asana")

    def test_secret_never_in_view_state_audit_or_logs(self):
        net = FakeNet()
        net.add("api.github.com", [])
        store = MemoryStore()
        reg = registry(self.ctx, net, store=store)
        with self.assertLogs("hub.tasksources", level="DEBUG") as logs:
            tasksources.log.info("marker")                       # assertLogs needs at least one record
            item = self.add(reg, "github")
            reg.poll_item(item["id"])
            reg.list_items()
        blob = json.dumps(reg.list_items()) + json.dumps(self.ctx.audits, default=str) + "".join(logs.output)
        blob += Path(self.ctx.config.data_path(), "tasksources.json").read_text()
        for f in Path(self.ctx.config.data_path()).glob("*.json"):
            blob += f.read_text()
        self.assertNotIn(SECRET, blob)
        self.assertTrue(reg.list_items()[-1]["has_secret"])
        self.assertEqual(store.data["task:" + item["id"]], SECRET)                # only the keyring has it

    def test_errors_are_plain_and_do_not_leak(self):
        net = FakeNet()
        net.add("api.github.com", b"", 401)
        reg = registry(self.ctx, net)
        item = self.add(reg, "github")
        with self.assertRaises(tasksources.SourceProblem) as cm:
            reg.poll_item(item["id"])
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertIn("rejected", reg.list_items()[-1]["last_error"])

    def test_validation(self):
        reg = registry(self.ctx, FakeNet())
        for bad in ({"kind": "nope"}, {"kind": "jira", "secret": SECRET, "settings": {"site": "http://x.com", "email": "a@b.co"}},
                    {"kind": "jira", "secret": SECRET, "settings": {"site": "https://a.atlassian.net", "email": "nope"}},
                    {"kind": "github", "secret": "short"}, {"kind": "github"}, {"kind": "trello", "secret": SECRET},
                    {"kind": "rss", "settings": {"url": "ftp://x"}}, {"kind": "webhook", "secret": "tooshort"}):
            with self.assertRaises(tasksources.SourceProblem, msg=bad):
                reg.add(bad)

    def test_edit_switch_delete_and_the_ready_made_asana_entry(self):
        store = MemoryStore()
        reg = registry(self.ctx, FakeNet(), store=store)
        ids = [i["id"] for i in reg.list_items()]
        self.assertEqual(ids, ["asana"])                                          # ready-made entry
        item = self.add(reg, "github")
        reg.update(item["id"], {"enabled": False, "label": "Mine", "secret": "NewTokenValue-9876543210"})
        view = [i for i in reg.list_items() if i["id"] == item["id"]][0]
        self.assertEqual((view["enabled"], view["label"]), (False, "Mine"))
        self.assertEqual(store.data["task:" + item["id"]], "NewTokenValue-9876543210")
        reg.delete(item["id"])
        self.assertNotIn("task:" + item["id"], store.data)
        reg.delete("asana")                                                         # cannot vanish; it is signed out and off
        self.assertEqual([i["id"] for i in reg.list_items()], ["asana"])

    def test_poll_due_respects_enabled_and_interval(self):
        net, now = FakeNet(), [1_800_000_000.0]
        net.add("api.github.com", [])
        reg = tasksources.TaskSources(self.ctx, store=MemoryStore(), transport=net, triage=lambda c, e: e, clock=lambda: now[0])
        a = self.add(reg, "github")
        b = reg.add({"kind": "linear", "label": "Lin", "secret": SECRET, "enabled": False})
        self.assertEqual(reg.poll_due(), 1)
        self.assertEqual(reg.poll_due(), 0)                                        # interval not passed
        now[0] += 6 * 60
        self.assertEqual(reg.poll_due(), 1)
        self.assertEqual(len([c for c in net.calls if "linear" in c[1]]), 0)       # the disabled one never polled
        self.assertTrue(a["id"] and b["id"])


class WebhookTests(Base):
    def setUp(self):
        super().setUp()
        self.got = []
        self.reg = registry(self.ctx, FakeNet(), triaged=self.got)
        self.item = self.reg.add({"kind": "webhook", "label": "Monitoring", "secret": "shared-secret-0123456789"})
        app = FastAPI()
        app.include_router(tasksources.make_router(self.ctx))
        self.client = TestClient(app)                                              # no login cookie: the hook is public
        self.addCleanup(self.client.close)
        self.url = "/hooks/" + self.item["id"]

    def test_accepts_with_the_secret(self):
        r = self.client.post(self.url, headers={"X-Webhook-Secret": "shared-secret-0123456789"},
                             json={"title": "Disk full", "text": "db-1 at 99%", "sender": "nagios", "url": "https://mon.example.com/1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.got[0]["title"], self.got[0]["sender"], self.got[0]["link"], self.got[0]["source"]),
                         ("Disk full", "nagios", "https://mon.example.com/1", "webhook"))
        r = self.client.post(self.url, headers={"Authorization": "Bearer shared-secret-0123456789"}, json={"text": "again"})
        self.assertEqual(r.status_code, 200)

    def test_bad_secret_rejected_and_counted(self):
        for headers in ({}, {"X-Webhook-Secret": "wrong-secret-wrong-secret"}, {"Authorization": "Bearer nope"}):
            r = self.client.post(self.url, headers=headers, json={"text": "x"})
            self.assertEqual(r.status_code, 401)
        self.assertEqual(self.got, [])
        r = self.client.post("/hooks/missing", headers={"X-Webhook-Secret": "shared-secret-0123456789"}, json={"text": "x"})
        self.assertEqual(r.status_code, 401)                                       # unknown id looks the same as a bad secret
        for _ in range(10):
            self.client.post(self.url, headers={"X-Webhook-Secret": "bad-bad-bad-bad-bad"}, json={"text": "x"})
        r = self.client.post(self.url, headers={"X-Webhook-Secret": "shared-secret-0123456789"}, json={"text": "x"})
        self.assertEqual(r.status_code, 429)                                       # locked after repeated failures

    def test_disabled_hook_and_bad_payloads(self):
        r = self.client.post(self.url, headers={"X-Webhook-Secret": "shared-secret-0123456789"}, content=b"not json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(self.url, headers={"X-Webhook-Secret": "shared-secret-0123456789"}, json={"title": "", "text": ""})
        self.assertEqual(r.status_code, 400)
        self.reg.update(self.item["id"], {"enabled": False})
        r = self.client.post(self.url, headers={"X-Webhook-Secret": "shared-secret-0123456789"}, json={"text": "x"})
        self.assertEqual(r.status_code, 401)

    def test_event_reaches_real_alert_triage_as_trusted(self):
        reg = tasksources.TaskSources(self.ctx, store=MemoryStore(), transport=FakeNet())
        item = reg.add({"kind": "webhook", "label": "Monitoring", "secret": "shared-secret-0123456789"})
        eng = alerts.Alerts(self.ctx, speaker=lambda t: None, spawn=lambda fn: fn())
        self.ctx.alerts = eng
        self.ctx.config.alerts_sources = ["whatsapp"]                               # webhook is not in the watched words
        a = reg.receive(item["id"], "shared-secret-0123456789", {"title": "URGENT prod is down", "text": "please fix asap", "sender": "pager"})
        self.assertIsNotNone(a)
        self.assertEqual(a["source"], "webhook")
        self.assertIsNone(eng.ingest({"source": "webhook", "sender": "x", "text": "URGENT please fix asap"}))   # untrusted path still filtered


# ---- mail accounts ---------------------------------------------------------------------------------------------

def mail_bytes(sender, subject, body):
    return (f"From: {sender}\r\nTo: me@corp.example\r\nSubject: {subject}\r\nDate: Mon, 9 Nov 2026 10:00:00 +0000\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode()


class FakeSmtp:
    def __init__(self):
        self.logins, self.sent, self.quit_called = [], [], False

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


class ImapAccounts(Base):
    def setUp(self):
        super().setUp()
        self.box = MemoryMailBox("me@corp.example")
        self.box.add(mail_bytes("Boss <boss@corp.example>", "Quarterly numbers", "Please send the numbers by EOD."))
        self.smtp = FakeSmtp()
        self.store = MemoryStore()
        self.opened = []

        def factory(entry, password):
            self.opened.append((entry["imap_host"], password))
            return self.box
        self.svc = gmailx.Gmailx(self.ctx, secrets_store=self.store, legacy=_NoLegacy(), listen=False, imap_factory=factory,
                                 smtp_factory=lambda entry: self.smtp)
        self.ctx._gmailx = self.svc

    def add(self, **kw):
        args = dict(label="work", address="me@corp.example", password="app-pass word", preset="outlook")
        args.update(kw)
        return self.svc.add_imap(args.pop("label"), args.pop("address"), args.pop("password"), **args)

    def test_add_list_search_recent_summary(self):
        v = self.add()
        self.assertEqual((v["kind"], v["connected"], v["imap_host"], v["smtp_host"]), ("imap", True, "outlook.office365.com", "smtp.office365.com"))
        self.assertNotIn("app-pass", json.dumps(v))
        self.assertEqual(self.opened[0][1], "app-pass word")                       # the factory got the keyring's password
        rows = self.svc.search(v["id"], "", 5)
        self.assertEqual((rows[0]["subject"], rows[0]["tag"]), ("Quarterly numbers", "work"))
        self.assertEqual(len(gmailx.recent(self.ctx, v["id"], 24 * 365 * 5)), 1)
        s = self.svc.summary()[v["id"]]
        self.assertEqual(s["unread"], 1)
        self.assertIn(v["id"], [a["id"] for a in gmailx.list_accounts(self.ctx)])

    def test_password_only_in_keyring_never_in_files(self):
        v = self.add()
        self.assertEqual(self.store.data["imap:" + v["id"]], "app-pass word")
        for f in Path(self.ctx.config.data_path()).glob("*"):
            self.assertNotIn("app-pass", f.read_text())

    def test_bad_login_is_not_kept(self):
        class Bad:
            def unread_count(self, folder):
                from hub.inbox import MailBoxError
                raise MailBoxError("Gmail didn't accept the saved address and app password.")
        svc = gmailx.Gmailx(self.ctx, secrets_store=self.store, legacy=_NoLegacy(), listen=False, imap_factory=lambda e, p: Bad())
        with self.assertRaises(gmailx.GmailxError) as cm:
            svc.add_imap("work", "me@corp.example", "pw-123456", preset="yahoo")
        self.assertNotIn("pw-123456", str(cm.exception))
        self.assertEqual([a["kind"] for a in svc.list_accounts()], ["legacy"])
        self.assertEqual(self.store.data, {})

    def test_tags_are_free_chips_and_cloud_switch_defaults(self):
        work = self.add(label="work", tags=["company", "client"])
        self.assertEqual((work["tags"], work["cloud_ai_allowed"], work["cloud_ai_default"], work["cloud_ai_explicit"]), (["company", "client"], False, False, False))
        side = self.add(label="side", address="s@yahoo.com", tags=["freelance"], preset="yahoo")
        self.assertEqual((side["cloud_ai_allowed"], side["tags"]), (True, ["freelance"]))
        self.assertTrue(gmailx.cloud_ai_allowed(self.ctx, side["id"]))
        self.assertFalse(gmailx.cloud_ai_allowed(self.ctx, work["id"]))
        # edit: pin it on, then back to the default
        v = self.svc.update(work["id"], cloud_ai_allowed=True)
        self.assertEqual((v["cloud_ai_allowed"], v["cloud_ai_explicit"]), (True, True))
        self.assertTrue(gmailx.cloud_ai_allowed(self.ctx, work["id"]))
        v = self.svc.update(work["id"], cloud_ai_allowed=None)
        self.assertEqual((v["cloud_ai_allowed"], v["cloud_ai_explicit"]), (False, False))
        # re-tagging a personal account as "work" flips the default; "personal" stays open
        v = self.svc.update(side["id"], tags=["personal", "work"])
        self.assertFalse(v["cloud_ai_allowed"])
        with self.assertRaises(gmailx.GmailxError):
            self.svc.update(side["id"], tags=["<script>"])

    def test_body_withheld_from_cloud_unless_account_allows(self):
        work = self.add(label="work", tags=["company"])
        uid = str(self.svc.search(work["id"], "", 1)[0]["id"])
        cloud = self.svc.get_message(work["id"], uid, destination="gemini")
        self.assertEqual((cloud["body"], cloud["body_withheld"]), ("", True))
        local = self.svc.get_message(work["id"], uid, destination="local")
        self.assertIn("numbers", local["body"])
        self.svc.update(work["id"], cloud_ai_allowed=True)
        self.assertFalse(self.svc.get_message(work["id"], uid, destination="gemini")["body_withheld"])

    def test_global_company_switch_is_still_the_default_for_company_tags(self):
        work = self.add(label="work")
        self.assertFalse(self.svc.cloud_ai_allowed(work["id"]))
        self.ctx.config.gmail_company_ai_allowed = True
        self.assertTrue(self.svc.cloud_ai_allowed(work["id"]))

    def test_sending_only_when_switched_on_and_explicit(self):
        v = self.add()
        r = self.svc.send_mail(v["id"], "a@example.com", "Hi", "Body")
        self.assertFalse(r["ok"])
        self.assertIn("switched off", r["error"])
        self.assertEqual(self.smtp.sent, [])
        self.svc.update(v["id"], send_enabled=True)
        r = self.svc.send_mail(v["id"], "a@example.com", "Hi", "Body")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.smtp.logins, [("me@corp.example", "app-password")])    # spaces of an app password are dropped
        self.assertEqual((self.smtp.sent[0]["To"], self.smtp.sent[0]["Subject"]), ("a@example.com", "Hi"))
        self.assertTrue(self.smtp.quit_called)
        self.assertFalse(self.svc.send_mail(v["id"], "not-an-address", "Hi", "x")["ok"])

    def test_reading_never_sends_and_send_email_stays_always_ask(self):
        from hub import safety
        self.assertIn("send_email", safety.ALWAYS_ASK)
        v = self.add()
        self.svc.search(v["id"], "", 5)
        self.svc.summary()
        self.assertEqual(self.smtp.sent, [])
        self.assertTrue(all(call[0] in ("unread_count", "list", "get") for call in self.box.calls))

    def test_validation_and_removal(self):
        for bad in (dict(address="nope"), dict(label="<b>"), dict(preset=None, imap_host="bad host!", imap_port=993),
                    dict(preset=None, imap_host="imap.x.com", imap_port=99999), dict(preset=None, imap_host="imap.x.com", imap_port=993, imap_security="weird")):
            with self.assertRaises(gmailx.GmailxError, msg=bad):
                self.add(**bad)
        v = self.add()
        self.svc.disconnect(v["id"])
        self.assertNotIn("imap:" + v["id"], self.store.data)
        self.assertEqual([a["kind"] for a in self.svc.list_accounts()], ["legacy"])

    def test_edit_server_and_new_password(self):
        v = self.add()
        v2 = self.svc.update(v["id"], imap={"imap_host": "imap.example.org"}, password="another-pass")
        self.assertEqual(v2["imap_host"], "imap.example.org")
        self.assertEqual(self.store.data["imap:" + v["id"]], "another-pass")
        self.assertNotIn("another-pass", json.dumps(v2))

    def test_starttls_and_ssl_open_the_right_connection(self):
        import imaplib
        entry = {"imap_host": "mail.example.com", "imap_port": 143, "imap_security": "starttls", "username": "u"}
        made = []

        class FakeImap:
            def __init__(self, host, port, timeout=None):
                made.append((host, port))
                self.tls = False

            def starttls(self, ssl_context=None):
                self.tls = True
        orig = imaplib.IMAP4
        imaplib.IMAP4 = FakeImap
        try:
            box = mailaccounts.open_box(entry, "pw")
            conn = box._open()
        finally:
            imaplib.IMAP4 = orig
        self.assertEqual((made, conn.tls), ([("mail.example.com", 143)], True))
        self.assertEqual(repr(box).count("pw"), 0)

    def test_alert_poll_skips_accounts_with_alerts_off(self):
        v = self.add()
        self.svc.update(v["id"], alerts=False)
        views = {a["id"]: a for a in self.svc.list_accounts()}
        self.assertFalse(views[v["id"]]["alerts"])


class _NoLegacy:
    def configured(self):
        return False


# ---- calendar feeds -------------------------------------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup@x
DTSTART;TZID=Europe/Berlin:20261102T090000
DTEND;TZID=Europe/Berlin:20261102T091500
SUMMARY:Team standup
LOCATION:https://meet.google.com/abc-defg-hij
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231T000000Z
EXDATE;TZID=Europe/Berlin:20261111T090000
END:VEVENT
BEGIN:VEVENT
UID:standup@x
RECURRENCE-ID;TZID=Europe/Berlin:20261116T090000
DTSTART;TZID=Europe/Berlin:20261116T140000
DTEND;TZID=Europe/Berlin:20261116T141500
SUMMARY:Team standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:daily@x
DTSTART:20261109T070000Z
DURATION:PT30M
SUMMARY:Daily check\\, with comma
RRULE:FREQ=DAILY;COUNT=3;INTERVAL=2
END:VEVENT
BEGIN:VEVENT
UID:monthly@x
DTSTART;TZID=Pacific Standard Time:20261013T100000
DTEND;TZID=Pacific Standard Time:20261013T110000
SUMMARY:Board (2nd Tuesday)
RRULE:FREQ=MONTHLY;BYDAY=2TU
END:VEVENT
BEGIN:VEVENT
UID:allday@x
DTSTART;VALUE=DATE:20261110
DTEND;VALUE=DATE:20261112
SUMMARY:Offsite
END:VEVENT
BEGIN:VEVENT
UID:floating@x
DTSTART:20261110T180000
DTEND:20261110T190000
SUMMARY:Dinner
DESCRIPTION:Join https://zoom.us/j/123456 now
END:VEVENT
BEGIN:VEVENT
UID:cancelled@x
DTSTART:20261110T120000Z
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:broken@x
DTSTART:garbage
SUMMARY:Broken
END:VEVENT
BEGIN:VEVENT
UID:yearly@x
DTSTART;VALUE=DATE:20200315
SUMMARY:Anniversary
RRULE:FREQ=YEARLY
END:VEVENT
END:VCALENDAR
"""


def span(start, days=1, tz=UTC):
    s = dt.datetime.combine(start, dt.time(), tz)
    return s, s + dt.timedelta(days=days)


class IcsParsing(unittest.TestCase):
    def day(self, y, m, d, days=1):
        ev = calendars.parse_ics(ICS, UTC)
        s, e = span(dt.date(y, m, d), days)
        return calendars.expand(ev, s, e, UTC)

    def titles(self, rows):
        return [(r["title"], r["start"][11:16]) for r in rows]

    def test_parser_skips_broken_events_and_unfolds_lines(self):
        ev = calendars.parse_ics("BEGIN:VEVENT\r\nDTSTART:20261110T100000Z\r\nSUMMARY:Long\r\n  title\r\nEND:VEVENT\r\n", UTC)
        self.assertEqual(ev[0]["summary"], "Long title")
        self.assertEqual(calendars.parse_ics("not a calendar", UTC), [])
        self.assertEqual(len(calendars.parse_ics(ICS, UTC)), 8)                    # the broken one is dropped

    def test_weekly_byday_tz_exdate_and_override(self):
        # Mon 2 Nov: Berlin 09:00 = 08:00 UTC
        self.assertEqual(self.titles(self.day(2026, 11, 2))[0], ("Team standup", "08:00"))
        self.assertEqual([t for t in self.titles(self.day(2026, 11, 4)) if t[0].startswith("Team")], [("Team standup", "08:00")])   # Wednesday
        self.assertEqual([t for t in self.titles(self.day(2026, 11, 3)) if t[0].startswith("Team")], [])                            # Tuesday: none
        self.assertEqual([t for t in self.titles(self.day(2026, 11, 11)) if t[0].startswith("Team")], [])                           # EXDATE
        moved = [t for t in self.titles(self.day(2026, 11, 16)) if t[0].startswith("Team")]
        self.assertEqual(moved, [("Team standup (moved)", "13:00")])                                                                # override replaces the instance
        self.assertEqual([t for t in self.titles(self.day(2027, 1, 4)) if t[0].startswith("Team")], [])                             # after UNTIL

    def test_dst_keeps_the_wall_clock(self):
        s, e = span(dt.date(2026, 3, 30))
        ics = ICS.replace("20261102T090000", "20260316T090000").replace("20261102T091500", "20260316T091500")
        rows = calendars.expand(calendars.parse_ics(ics, UTC), s, e, UTC)
        self.assertEqual([r["start"][11:16] for r in rows if r["title"] == "Team standup"], ["07:00"])    # Berlin is on summer time: 09:00 = 07:00 UTC

    def test_daily_interval_and_count(self):
        days = [(d, [r for r in self.day(2026, 11, d) if r["title"].startswith("Daily")]) for d in range(9, 16)]
        self.assertEqual([d for d, rows in days if rows], [9, 11, 13])              # every 2 days, COUNT=3
        self.assertEqual(days[0][1][0]["title"], "Daily check, with comma")
        self.assertEqual(days[0][1][0]["end"][11:16], "07:30")                      # DURATION

    def test_monthly_second_tuesday_with_windows_tz_name(self):
        rows = [r for r in self.day(2026, 11, 10) if r["title"].startswith("Board")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start"][11:16], "18:00")                          # Pacific (UTC-8) 10:00 in November
        self.assertEqual([r for r in self.day(2026, 11, 17) if r["title"].startswith("Board")], [])

    def test_all_day_floating_links_cancelled_yearly(self):
        rows = self.day(2026, 11, 10)
        by = {r["title"]: r for r in rows}
        self.assertTrue(by["Offsite"]["all_day"])
        self.assertEqual(len(self.day(2026, 11, 11)) and [r for r in self.day(2026, 11, 11) if r["title"] == "Offsite"].__len__(), 1)   # spans two days
        self.assertEqual([r for r in self.day(2026, 11, 12) if r["title"] == "Offsite"], [])
        self.assertEqual((by["Dinner"]["link"], by["Dinner"]["platform"], by["Dinner"]["start"][11:16]), ("https://zoom.us/j/123456", "zoom", "18:00"))
        self.assertNotIn("Cancelled thing", by)
        self.assertEqual([r["title"] for r in self.day(2026, 3, 15)], ["Anniversary"])
        self.assertEqual(rows[0]["all_day"], True)                                  # all-day first

    def test_runaway_rule_is_capped(self):
        ics = "BEGIN:VEVENT\nDTSTART:19900101T100000Z\nSUMMARY:Old\nRRULE:FREQ=DAILY\nEND:VEVENT\n"
        s, e = span(dt.date(2026, 11, 9))
        self.assertEqual(calendars.expand(calendars.parse_ics(ics, UTC), s, e, UTC), [])   # beyond the iteration cap: dropped, not hung


class CalendarFeeds(Base):
    def setUp(self):
        super().setUp()
        self.net, self.store = [], MemoryStore()
        self.status = 200
        self.cal = calendars.Calendars(self.ctx, store=self.store, tz=UTC, clock=lambda: 1_800_000_000.0,
                                       fetch=lambda url: (self.net.append(url) or (self.status, ICS.encode())))
        self.ctx.calendars = self.cal

    def test_add_edit_switch_delete_and_secret_address(self):
        self.assertEqual([c["id"] for c in self.cal.list_items()], ["google"])      # ready-made
        v = self.cal.add({"label": "Company", "url": "webcal://cal.example.com/private/abcSECRET/basic.ics", "color": "#E67C73"})
        self.assertEqual((v["color"], v["address_hint"]), ("#e67c73", "cal.example.com/..."))
        self.assertNotIn("abcSECRET", json.dumps(self.cal.list_items()))
        self.assertEqual(self.store.data["cal:" + v["id"]], "https://cal.example.com/private/abcSECRET/basic.ics")
        for f in Path(self.ctx.config.data_path()).glob("*.json"):
            self.assertNotIn("abcSECRET", f.read_text())
        rows = self.cal.meetings_between(dt.date(2026, 11, 10), 1)
        self.assertTrue(rows and all(r["feed"] == "Company" and r["color"] == "#e67c73" for r in rows))
        self.cal.update(v["id"], {"enabled": False})
        self.assertEqual(self.cal.meetings_between(dt.date(2026, 11, 10), 1), [])
        self.cal.update(v["id"], {"enabled": True, "color": "#039be5", "label": "Work"})
        self.assertEqual(self.cal.meetings_between(dt.date(2026, 11, 10), 1)[0]["feed"], "Work")
        self.cal.delete(v["id"])
        self.assertNotIn("cal:" + v["id"], self.store.data)
        self.cal.delete("google")
        self.assertFalse(self.cal.google_item()["enabled"])
        self.assertFalse(calendars.google_enabled(self.ctx))

    def test_validation_and_broken_feed_does_not_break_others(self):
        for bad in ({"label": "", "url": "https://x.com/a.ics"}, {"label": "x", "url": "ftp://x"}, {"label": "x", "url": "https://x.com/a.ics", "color": "red"}):
            with self.assertRaises(calendars.CalendarProblem):
                self.cal.add(bad)
        good = self.cal.add({"label": "Good", "url": "https://x.com/good.ics"})
        bad = self.cal.add({"label": "Bad", "url": "https://x.com/bad.ics"})
        self.cal.fetch = lambda url: (200, ICS.encode()) if "good" in url else (404, b"")
        rows = self.cal.meetings_between(dt.date(2026, 11, 10), 1)
        self.assertTrue(rows and all(r["feed"] == "Good" for r in rows))
        err = [c for c in self.cal.list_items() if c["id"] == bad["id"]][0]["last_error"]
        self.assertIn("404", err)
        self.assertNotIn("bad.ics", err)
        self.cal.fetch = lambda url: (200, b"<html>login</html>")
        self.cal.cache.clear()
        with self.assertRaises(calendars.CalendarProblem):
            self.cal.test(good["id"])

    def test_fetch_is_cached(self):
        v = self.cal.add({"label": "C", "url": "https://x.com/a.ics"})
        self.cal.meetings_between(dt.date(2026, 11, 10), 1)
        self.cal.meetings_between(dt.date(2026, 11, 11), 1)
        self.assertEqual(len(self.net), 1)
        self.assertTrue(v["id"])

    def test_work_calendar_route_merges_google_and_feeds(self):
        from hub import work
        self.cal.add({"label": "Company", "url": "https://x.com/a.ics", "color": "#8e24aa"})

        class Meeting:
            def __init__(self, title, start, link=""):
                self.title, self.start, self.end, self.all_day, self.link = title, start, start + dt.timedelta(hours=1), False, link

            def platform(self):
                return "meet"

        class Agenda:
            def meetings(self, start, days):
                return [Meeting("Google call", dt.datetime(2026, 11, 10, 12, 0, tzinfo=UTC), "https://meet.google.com/x")]

        class Bridge:
            def state(self, feature):
                class S:
                    configured, reason, module = True, "", Agenda()
                return S()
        app = FastAPI()
        app.include_router(work.make_router(self.ctx, bridge=Bridge(), today=lambda: dt.date(2026, 11, 10)))
        client = TestClient(app, cookies={"hub_session": "yes"})
        got = client.get("/api/work/calendar", params={"day": "2026-11-10"}).json()["meetings"]
        self.assertIn("Google call", [m["title"] for m in got])
        self.assertIn("Dinner", [m["title"] for m in got])
        self.assertEqual({m["feed"] for m in got}, {"Google", "Company"})
        starts = [m["start"] for m in got if not m["all_day"]]
        self.assertEqual(starts, sorted(starts))
        self.cal.update("google", {"enabled": False})
        got = client.get("/api/work/calendar", params={"day": "2026-11-10"}).json()["meetings"]
        self.assertNotIn("Google call", [m["title"] for m in got])
        self.cal.cal = None
        self.ctx.calendars = None

    def test_feed_only_works_when_google_is_not_set_up(self):
        from hub import work
        self.cal.add({"label": "Company", "url": "https://x.com/a.ics"})

        class Bridge:
            def state(self, feature):
                class S:
                    configured, reason, module = False, "Run setup calendar.", None
                return S()
        app = FastAPI()
        app.include_router(work.make_router(self.ctx, bridge=Bridge(), today=lambda: dt.date(2026, 11, 10)))
        client = TestClient(app, cookies={"hub_session": "yes"})
        r = client.get("/api/work/calendar", params={"day": "2026-11-10"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["meetings"])


# ---- the generic routes ---------------------------------------------------------------------------------------

class Routes(Base):
    def setUp(self):
        super().setUp()
        self.net = FakeNet()
        self.net.add("api.github.com", [])
        self.store = MemoryStore()
        registry(self.ctx, self.net, store=self.store)
        self.ctx.calendars = calendars.Calendars(self.ctx, store=MemoryStore(), tz=UTC, fetch=lambda u: (200, ICS.encode()))
        box = MemoryMailBox("me@corp.example")
        self.ctx._gmailx = gmailx.Gmailx(self.ctx, secrets_store=MemoryStore(), legacy=_NoLegacy(), listen=False,
                                         imap_factory=lambda e, p: box, smtp_factory=lambda e: FakeSmtp())
        app = FastAPI()
        for mod in (alerts, sources, calendars, tasksources, gmailx):
            kw = {}
            if mod is gmailx:
                kw = {}
            app.include_router(mod.make_router(self.ctx, **kw) if mod is not gmailx else gmailx.make_router(self.ctx))
        self.anon = TestClient(app)
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.addCleanup(self.anon.close)
        self.addCleanup(self.client.close)

    def test_every_source_route_requires_login(self):
        calls = [("get", "/api/sources"), ("get", "/api/sources/app"), ("post", "/api/sources/app"), ("put", "/api/sources/app/x"),
                 ("delete", "/api/sources/app/x"), ("post", "/api/sources/task/x/test"), ("get", "/api/sources/task"),
                 ("get", "/api/sources/mail"), ("get", "/api/sources/calendar"), ("post", "/api/sources/webhook"),
                 ("get", "/api/alerts/capture-config"), ("get", "/api/calendars/agenda"), ("post", "/api/gmail/accounts/imap")]
        for method, path in calls:
            r = getattr(self.anon, method)(path, **({"json": {}} if method in ("post", "put") else {}))
            self.assertEqual(r.status_code, 401, (method, path))

    def test_unknown_kind(self):
        self.assertEqual(self.client.get("/api/sources/nothing").status_code, 404)

    def test_overview_lists_every_kind(self):
        d = self.client.get("/api/sources").json()
        self.assertEqual(set(d), {"app", "task", "mail", "calendar"})
        self.assertEqual([i["id"] for i in d["task"]["items"]], ["asana"])
        self.assertEqual([i["id"] for i in d["calendar"]["items"]], ["google"])
        self.assertEqual([i["id"] for i in d["mail"]["items"]], ["default"])
        self.assertIn("whatsapp", [i["id"] for i in d["app"]["items"]])

    def test_default_app_list_and_capture_config(self):
        d = self.client.get("/api/alerts/capture-config").json()
        pk = {p["package"]: p["label"] for p in d["packages"]}
        for pkg in ("com.whatsapp", "com.Slack", "com.microsoft.teams", "com.google.android.gm", "com.google.android.apps.dynamite",
                    "com.asana.app", "com.atlassian.android.jira.core", "com.trello", "org.telegram.messenger",
                    "com.microsoft.office.outlook", "com.google.android.apps.messaging"):
            self.assertIn(pkg, pk)
        self.assertEqual(pk["com.whatsapp"], "WhatsApp")

    def test_app_crud_updates_capture_config_and_watched_words(self):
        r = self.client.post("/api/sources/app", json={"label": "Signal", "package": "org.thoughtcrime.securesms"})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["id"]
        pk = [p["package"] for p in self.client.get("/api/alerts/capture-config").json()["packages"]]
        self.assertIn("org.thoughtcrime.securesms", pk)
        self.assertIn("org.thoughtcrime.securesms", self.ctx.config.alerts_sources)        # alerts_sources follows the list
        self.client.put("/api/sources/app/whatsapp", json={"enabled": False})
        pk = [p["package"] for p in self.client.get("/api/alerts/capture-config").json()["packages"]]
        self.assertNotIn("com.whatsapp", pk)
        self.assertNotIn("whatsapp", self.ctx.config.alerts_sources)
        self.assertIn("slack", self.ctx.config.alerts_sources)
        self.assertEqual(self.client.put("/api/sources/app/" + sid, json={"label": "Signal app", "package": "org.signal.app"}).json()["package"], "org.signal.app")
        self.assertEqual(self.client.delete("/api/sources/app/" + sid).status_code, 200)
        self.assertNotIn("org.signal.app", [p["package"] for p in self.client.get("/api/alerts/capture-config").json()["packages"]])
        self.assertEqual(self.client.post("/api/sources/app", json={"label": "x", "package": "bad package"}).status_code, 400)
        self.assertEqual(self.client.put("/api/sources/app/none", json={"enabled": True}).status_code, 404)
        self.assertEqual(self.client.post("/api/sources/app/whatsapp/test").status_code, 200)

    def test_existing_alerts_sources_become_the_initial_enabled_list(self):
        self.ctx.config.alerts_sources = ["slack", "com.example.chat"]
        d = {i["id"]: i for i in self.client.get("/api/sources/app").json()["items"]}
        self.assertTrue(d["slack"]["enabled"])
        self.assertFalse(d["whatsapp"]["enabled"])
        custom = [i for i in d.values() if i["package"] == "com.example.chat"]
        self.assertEqual(len(custom), 1)
        self.assertTrue(custom[0]["enabled"])

    def test_task_routes_add_test_poll_edit_delete(self):
        r = self.client.post("/api/sources/task", json={"kind": "github", "label": "GH", "secret": SECRET})
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()
        self.assertNotIn(SECRET, r.text)
        self.assertTrue(item["has_secret"])
        self.assertTrue(self.client.post(f"/api/sources/task/{item['id']}/test").json()["ok"])
        self.assertEqual(self.client.post(f"/api/sources/task/{item['id']}/poll").json(), {"events": 0, "alerts": 0})
        r = self.client.put(f"/api/sources/task/{item['id']}", json={"enabled": False})
        self.assertFalse(r.json()["enabled"])
        listing = self.client.get("/api/sources/task").json()
        self.assertIn("jira", listing["kinds"])
        self.assertNotIn(SECRET, json.dumps(listing))
        self.assertEqual(self.client.delete(f"/api/sources/task/{item['id']}").status_code, 200)
        self.assertEqual(self.client.post("/api/sources/task", json={"kind": "github"}).status_code, 400)
        self.assertEqual(self.client.post("/api/sources/task/nope/test").status_code, 404)

    def test_webhook_kind_creates_and_filters(self):
        r = self.client.post("/api/sources/webhook", json={"label": "CI", "secret": "ci-shared-secret-01234"})
        self.assertEqual((r.status_code, r.json()["kind"], r.json()["hook_path"]), (200, "webhook", "/hooks/ci"))
        self.assertNotIn("ci-shared-secret", r.text)
        hooks = self.client.get("/api/sources/webhook").json()["items"]
        self.assertEqual([h["id"] for h in hooks], ["ci"])
        self.assertEqual(self.client.post("/api/sources/webhook/ci/poll").status_code, 409)
        ok = self.anon.post("/hooks/ci", headers={"X-Webhook-Secret": "ci-shared-secret-01234"}, json={"title": "x", "text": "URGENT fix asap please"})
        self.assertEqual(ok.status_code, 200)

    def test_calendar_routes(self):
        r = self.client.post("/api/sources/calendar", json={"label": "Home", "url": "https://x.com/s/abc.ics", "color": "#0b8043"})
        self.assertEqual(r.status_code, 200, r.text)
        cid = r.json()["id"]
        self.assertNotIn("abc.ics", r.text)
        self.assertTrue(self.client.post(f"/api/sources/calendar/{cid}/test").json()["ok"])
        got = self.client.get("/api/calendars/agenda", params={"day": "2026-11-10"}).json()["meetings"]
        self.assertIn("Dinner", [m["title"] for m in got])
        self.assertEqual(self.client.put(f"/api/sources/calendar/{cid}", json={"color": "nope"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/sources/calendar/{cid}").status_code, 200)

    def test_mail_routes_add_edit_tags_cloud_switch_delete(self):
        r = self.client.post("/api/sources/mail", json={"type": "imap", "label": "work", "address": "me@corp.example", "password": "pw-secret-123",
                                                          "preset": "outlook", "tags": ["company", "client-x"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("pw-secret-123", r.text)
        v = r.json()
        self.assertEqual((v["tags"], v["cloud_ai_allowed"]), (["company", "client-x"], False))
        r = self.client.put(f"/api/sources/mail/{v['id']}", json={"cloud_ai_allowed": True, "tags": ["company", "ok"], "alerts": False})
        self.assertEqual((r.json()["cloud_ai_allowed"], r.json()["alerts"]), (True, False))
        self.assertTrue(self.client.post(f"/api/sources/mail/{v['id']}/test").json()["ok"])
        self.assertEqual(self.client.post("/api/sources/mail", json={"type": "gmail", "label": "x"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/sources/mail/{v['id']}").status_code, 200)
        # the gmail routes carry the same data
        r = self.client.post("/api/gmail/accounts/imap", json={"label": "yahoo", "address": "me@yahoo.com", "password": "pw-secret-123", "preset": "yahoo"})
        self.assertEqual(r.status_code, 200, r.text)
        listing = self.client.get("/api/gmail/accounts").json()
        self.assertIn("outlook", listing["presets"])
        r = self.client.put(f"/api/gmail/accounts/{r.json()['id']}", json={"cloud_ai_allowed": None, "tags": ["a", "b"]})
        self.assertEqual(r.json()["cloud_ai_explicit"], False)

    def test_mail_alert_poll_uses_account_ids(self):
        # the engine's mail poll gets account VIEWS from gmailx.list_accounts: it must pass the id and honour `alerts`
        box = MemoryMailBox("me@corp.example")
        box.add(mail_bytes("Boss <boss@corp.example>", "URGENT please send the report today", "asap"))
        svc = gmailx.Gmailx(self.ctx, secrets_store=MemoryStore(), legacy=_NoLegacy(), listen=False, imap_factory=lambda e, p: box)
        self.ctx._gmailx = svc
        v = svc.add_imap("work", "me@corp.example", "pw-secret-123", preset="outlook")
        eng = alerts.Alerts(self.ctx, speaker=lambda t: None, spawn=lambda fn: fn(), gmailx_mod=gmailx)
        import asyncio
        n = asyncio.run(eng.poll_email())
        self.assertEqual(n, 1)
        svc.update(v["id"], alerts=False)
        box.add(mail_bytes("Boss <boss@corp.example>", "URGENT second one please today", "asap"))
        self.assertEqual(asyncio.run(eng.poll_email()), 0)


if __name__ == "__main__":
    unittest.main()
