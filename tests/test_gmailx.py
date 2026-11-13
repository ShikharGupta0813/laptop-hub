"""Tests for hub/gmailx.py (two Gmail accounts), its routes, tools and the Autopilot hooks. Everything is fake:
Google is a scripted function, the keyring is a dict, the legacy app-password mailbox is a stub. No network."""
import asyncio
import base64
import json
import shutil
import tempfile
import unittest
from email import message_from_bytes
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import gmailx, gmailx_tools
from hub.common import Ctx
from hub.gmailx import SCOPE_READ, SCOPE_SEND, Gmailx, GmailxError, MemoryStore
from tests.helpers import make_config
from tests.test_autopilot import need_auth


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGoogle:
    """Just enough of Google's OAuth and Gmail REST endpoints."""

    def __init__(self):
        self.codes = {}          # code -> (address, scopes)
        self.tokens = {}         # access token -> address
        self.refresh = {}        # refresh token -> address
        self.mail = {}           # address -> list of message dicts (Gmail "metadata" shape + body)
        self.calls = []
        self.sent = []           # (address, raw bytes)
        self.revoked = []
        self.n = 0
        self.invalid_grant = set()

    def grant(self, code, address, scopes):
        self.codes[code] = (address, list(scopes))

    def add_mail(self, address, mid, sender, subject, snippet, body, ts=1_900_000_000_000, labels=("INBOX", "UNREAD")):
        self.mail.setdefault(address, []).append({
            "id": mid, "threadId": "t" + mid, "snippet": snippet, "labelIds": list(labels), "internalDate": str(ts),
            "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                                    {"name": "Date", "value": "Mon, 9 Nov 2026 10:00:00 +0000"}],
                        "mimeType": "text/plain", "body": {"data": b64(body)}}})

    def __call__(self, method, url, *, headers=None, params=None, data=None, json_body=None):
        self.calls.append((method, url, dict(data or {}), dict(params or {})))
        if url == gmailx.TOKEN_URL:
            if data["grant_type"] == "authorization_code":
                if data["code"] not in self.codes or not data.get("code_verifier"):
                    return 400, {"error": "invalid_grant"}
                address, scopes = self.codes[data["code"]]
                self.refresh["rt-" + address] = address
                return 200, {"access_token": self._at(address), "refresh_token": "rt-" + address, "expires_in": 3600,
                             "scope": " ".join(scopes)}
            if data["refresh_token"] in self.invalid_grant:
                return 400, {"error": "invalid_grant"}
            return 200, {"access_token": self._at(self.refresh[data["refresh_token"]]), "expires_in": 3600}
        if url == gmailx.REVOKE_URL:
            self.revoked.append(data["token"])
            return 200, {}
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        address = self.tokens.get(token)
        if address is None:
            return 401, {}
        path = url[len(gmailx.API):]
        if path == "/profile":
            return 200, {"emailAddress": address}
        msgs = sorted(self.mail.get(address, []), key=lambda m: -int(m["internalDate"]))
        if path == "/labels/INBOX":
            return 200, {"messagesUnread": sum(1 for m in msgs if "UNREAD" in m["labelIds"])}
        if path == "/messages" and method == "GET":
            q = params.get("q", "")
            hit = [m for m in msgs if not q or q.startswith(("after:", "is:")) or q.lower() in m["snippet"].lower()
                   or q.lower() in m["payload"]["headers"][1]["value"].lower()]
            if "important" in q:
                hit = hit[:1]
            return 200, {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in hit[:params["maxResults"]]],
                         "resultSizeEstimate": len(hit)}
        if path.startswith("/messages/") and path != "/messages/send" and method == "GET":
            m = next((x for x in msgs if x["id"] == path.rsplit("/", 1)[1]), None)
            return (200, m) if m else (404, {})
        if path == "/messages/send":
            self.sent.append((address, base64.urlsafe_b64decode(json_body["raw"] + "==")))
            return 200, {"id": "sent-1"}
        return 404, {}

    def _at(self, address):
        self.n += 1
        token = f"at-{address}-{self.n}"
        self.tokens[token] = address
        return token


class FakeBox:
    def list(self, folder, query, limit, preview):
        return []

    def unread_count(self, folder):
        return 0


class FakeLegacy:
    """Stands in for the Jarvis app-password mailbox."""

    def __init__(self, address="me@gmail.com", configured=True):
        self.addr, self.ok, self.sent = address, configured, []

    def configured(self):
        return self.ok

    def address(self):
        return self.addr

    def account(self):
        return self.addr, "app-password"

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def box(self):
        return FakeBox()


class Base(unittest.TestCase):
    def setUp(self):
        self.config, self.root = make_config()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audits = []
        self.ctx = Ctx(config=self.config, model_for=lambda n: None, transcriber=None, notes=None,
                       need_auth=need_auth, audit=lambda *a, **k: self.audits.append((a, k)))
        self.google, self.store, self.legacy = FakeGoogle(), MemoryStore(), FakeLegacy()
        self.store.set(gmailx.CLIENT_KEY, json.dumps({"client_id": "client-id-123456", "client_secret": "secret-xyz"}))
        self.clock = [1_900_000_000.0]
        self.svc = gmailx.get_service(self.ctx, secrets_store=self.store, http=self.google, legacy=self.legacy,
                                      now=lambda: self.clock[0], listen=False)

    def connect(self, label, address, send=False, code=None):
        scopes = [SCOPE_READ] + ([SCOPE_SEND] if send else [])
        code = code or "code-" + address
        self.google.grant(code, address, scopes)
        flow = self.svc.begin_connect(label, send_enabled=send)
        return self.svc.complete_connect(flow["state"], code=code), flow


class MigrationTest(Base):
    def test_existing_single_connection_becomes_default_personal(self):
        accounts = gmailx.list_accounts(self.ctx)
        self.assertEqual([(a["id"], a["label"], a["connected"], a["address"]) for a in accounts],
                         [("default", "personal", True, "me@gmail.com")])
        self.assertTrue(accounts[0]["scopes"])                      # honest: the app-password abilities
        self.assertEqual(accounts[0]["kind"], "legacy")
        self.assertEqual(self.store.data.get("default"), None)      # nothing copied or moved; the old keyring is untouched

    def test_legacy_not_set_up_is_listed_not_connected(self):
        self.legacy.ok = False
        a = gmailx.list_accounts(self.ctx)[0]
        self.assertFalse(a["connected"])
        self.assertEqual(a["scopes"], [])

    def test_legacy_sending_still_works_without_relogin(self):
        r = gmailx.send_mail(self.ctx, "default", "bob@example.com", "Hi", "Hello")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.legacy.sent, [("bob@example.com", "Hi", "Hello")])

    def test_old_registry_file_without_default_is_migrated_and_keeps_extra_accounts(self):
        self.connect("company", "me@corp.com")
        path = Path(self.config.data_path()) / "gmailx.json"
        data = json.loads(path.read_text())
        data["accounts"].pop("default", None)
        path.write_text(json.dumps(data))
        ids = [a["id"] for a in self.svc.list_accounts()]
        self.assertEqual(ids[0], "default")
        self.assertEqual(len(ids), 2)

    def test_default_cannot_be_disconnected_here(self):
        with self.assertRaises(GmailxError):
            self.svc.disconnect("default")


class AccountsTest(Base):
    def test_two_accounts_have_isolated_tokens(self):
        a, _ = self.connect("company", "me@corp.com")
        b, _ = self.connect("side", "me@side.com")
        self.assertNotEqual(a["id"], b["id"])
        ta, tb = json.loads(self.store.data[a["id"]]), json.loads(self.store.data[b["id"]])
        self.assertEqual(ta["refresh_token"], "rt-me@corp.com")
        self.assertEqual(tb["refresh_token"], "rt-me@side.com")
        self.google.add_mail("me@corp.com", "c1", "A <a@x.com>", "Corp only", "s", "b")
        self.google.add_mail("me@side.com", "s1", "B <b@x.com>", "Side only", "s", "b")
        self.assertEqual([i["subject"] for i in self.svc.search(a["id"], "")], ["Corp only"])
        self.assertEqual([i["subject"] for i in self.svc.search(b["id"], "")], ["Side only"])
        self.assertEqual(self.google.tokens[ta["access_token"]], "me@corp.com")

    def test_scopes_company_is_read_only_by_default_and_listed_honestly(self):
        acct, flow = self.connect("company", "me@corp.com")
        scope_param = parse_qs(urlsplit(flow["auth_url"]).query)["scope"][0]
        self.assertEqual(scope_param.split(), [SCOPE_READ])
        self.assertNotIn("mail.google.com", flow["auth_url"])
        self.assertEqual(acct["scopes"], [SCOPE_READ])
        self.assertFalse(acct["send_enabled"])

    def test_send_scope_requested_only_when_asked(self):
        acct, flow = self.connect("company", "me@corp.com", send=True)
        self.assertEqual(set(parse_qs(urlsplit(flow["auth_url"]).query)["scope"][0].split()), {SCOPE_READ, SCOPE_SEND})
        self.assertTrue(acct["send_enabled"])
        self.assertIn(SCOPE_SEND, acct["scopes"])

    def test_auth_url_uses_pkce_and_offline_access(self):
        _, flow = self.connect("company", "me@corp.com")
        q = parse_qs(urlsplit(flow["auth_url"]).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["state"], [flow["state"]])

    def test_granting_less_than_asked_is_reported_honestly(self):
        self.google.grant("c", "me@corp.com", [SCOPE_READ])           # Google granted read only
        flow = self.svc.begin_connect("company", send_enabled=True)
        acct = self.svc.complete_connect(flow["state"], code="c")
        self.assertEqual(acct["scopes"], [SCOPE_READ])
        self.assertFalse(acct["send_enabled"])

    def test_same_address_twice_is_refused_and_bad_state_too(self):
        self.connect("company", "me@corp.com")
        self.google.grant("again", "me@corp.com", [SCOPE_READ])
        flow = self.svc.begin_connect("other")
        with self.assertRaises(GmailxError):
            self.svc.complete_connect(flow["state"], code="again")
        with self.assertRaises(GmailxError):
            self.svc.complete_connect("nope", code="again")

    def test_paste_back_address_completes_the_flow(self):
        self.google.grant("pc", "me@corp.com", [SCOPE_READ])
        flow = self.svc.begin_connect("company")
        acct = self.svc.complete_connect(flow["state"], redirect_url=f"http://127.0.0.1:1/?state={flow['state']}&code=pc")
        self.assertEqual(acct["address"], "me@corp.com")
        self.assertEqual(self.svc.flow_status(flow["state"])["status"], "done")

    def test_missing_client_credentials_is_a_plain_error(self):
        self.store.delete(gmailx.CLIENT_KEY)
        with self.assertRaises(GmailxError) as cm:
            self.svc.begin_connect("company")
        self.assertIn("client id", str(cm.exception))

    def test_expired_access_token_is_refreshed_and_revoked_grant_marks_reconnect(self):
        acct, _ = self.connect("company", "me@corp.com")
        self.google.add_mail("me@corp.com", "c1", "A <a@x.com>", "Hello", "s", "b")
        self.clock[0] += 7200
        self.assertEqual(len(self.svc.search(acct["id"], "")), 1)
        self.assertTrue(any(c[2].get("grant_type") == "refresh_token" for c in self.google.calls))
        self.clock[0] += 7200
        self.google.invalid_grant.add("rt-me@corp.com")
        with self.assertRaises(GmailxError):
            self.svc.search(acct["id"], "")
        self.assertFalse(self.svc.list_accounts()[1]["connected"])

    def test_disconnect_wipes_tokens_and_revokes(self):
        acct, _ = self.connect("company", "me@corp.com")
        self.svc.disconnect(acct["id"])
        self.assertNotIn(acct["id"], self.store.data)
        self.assertEqual([a["id"] for a in self.svc.list_accounts()], ["default"])
        self.assertEqual(self.google.revoked, ["rt-me@corp.com"])
        self.assertNotIn("rt-me@corp.com", json.dumps(self.audits))

    def test_tokens_never_land_in_the_registry_file_or_audit_log(self):
        self.connect("company", "me@corp.com", send=True)
        text = (Path(self.config.data_path()) / "gmailx.json").read_text() + json.dumps(self.audits)
        self.assertNotIn("rt-me@corp.com", text)
        self.assertNotIn("at-me@corp.com", text)
        self.assertNotIn("secret-xyz", text)

    def test_update_label_and_validation(self):
        acct, _ = self.connect("company", "me@corp.com")
        self.assertEqual(self.svc.update(acct["id"], label="work")["label"], "work")
        with self.assertRaises(GmailxError):
            self.svc.update(acct["id"], label="<script>")
        with self.assertRaises(GmailxError):
            self.svc.update("missing", label="x")

    def test_enabling_send_without_the_scope_asks_for_reconsent_then_works(self):
        acct, _ = self.connect("company", "me@corp.com")
        view = self.svc.update(acct["id"], send_enabled=True)
        self.assertTrue(view["reconsent_required"])
        r = self.svc.send_mail(acct["id"], "bob@example.com", "Hi", "x")
        self.assertFalse(r["ok"])
        self.assertIn("Re-consent", r["error"])
        self.google.grant("rc", "me@corp.com", [SCOPE_READ, SCOPE_SEND])
        flow = self.svc.begin_connect("company", account_id=acct["id"])
        self.assertIn(SCOPE_SEND, parse_qs(urlsplit(flow["auth_url"]).query)["scope"][0])
        back = self.svc.complete_connect(flow["state"], code="rc")
        self.assertEqual(back["id"], acct["id"])
        self.assertFalse(back["reconsent_required"])
        self.assertTrue(self.svc.send_mail(acct["id"], "bob@example.com", "Hi", "x")["ok"])


class SendTest(Base):
    def test_company_account_refuses_to_send_by_default(self):
        acct, _ = self.connect("company", "me@corp.com")
        r = gmailx.send_mail(self.ctx, acct["id"], "bob@example.com", "Hi", "x")
        self.assertEqual((r["ok"], r["message_id"]), (False, ""))
        self.assertIn("switched off", r["error"])
        self.assertEqual(self.google.sent, [])

    def test_enabled_account_sends_a_real_mime_message_with_attachment(self):
        acct, _ = self.connect("company", "me@corp.com", send=True)
        r = gmailx.send_mail(self.ctx, acct["id"], "bob@example.com", "Hello", "Body text",
                             attachments=[{"filename": "a.txt", "data": b"hi", "mime_type": "text/plain"}])
        self.assertEqual((r["ok"], r["message_id"]), (True, "sent-1"))
        address, raw = self.google.sent[0]
        msg = message_from_bytes(raw)
        self.assertEqual((address, msg["To"], msg["From"], msg["Subject"]), ("me@corp.com", "bob@example.com", "me@corp.com", "Hello"))
        self.assertEqual([p.get_filename() for p in msg.walk() if p.get_filename()], ["a.txt"])

    def test_bad_input_is_refused_before_anything_is_sent(self):
        acct, _ = self.connect("company", "me@corp.com", send=True)
        for to, subject in (("not-an-address", "x"), ("a@b.com", "line1\nBcc: evil@x.com"), ("", "x")):
            self.assertFalse(gmailx.send_mail(self.ctx, acct["id"], to, subject, "b")["ok"])
        self.assertFalse(gmailx.send_mail(self.ctx, "nope", "a@b.com", "x", "b")["ok"])
        self.assertEqual(self.google.sent, [])

    def test_send_is_audited_without_content(self):
        acct, _ = self.connect("company", "me@corp.com", send=True)
        gmailx.send_mail(self.ctx, acct["id"], "bob@example.com", "Secret subject", "Secret body")
        self.assertNotIn("Secret", json.dumps(self.audits))
        self.assertTrue(any(a[0][0] == "gmail_send" for a in self.audits))

    def test_legacy_default_can_be_switched_off(self):
        self.svc.update("default", send_enabled=False)
        r = gmailx.send_mail(self.ctx, "default", "bob@example.com", "Hi", "x")
        self.assertFalse(r["ok"])
        self.assertEqual(self.legacy.sent, [])


class ReadTest(Base):
    def setUp(self):
        super().setUp()
        self.acct, _ = self.connect("company", "me@corp.com")
        g = self.google
        g.add_mail("me@corp.com", "m1", "Ann <ann@corp.com>", "Budget review", "Please review the budget", "Full budget text 4111 1111 1111 1111", ts=1_900_000_100_000)
        g.add_mail("me@corp.com", "m2", "Bo <bo@corp.com>", "Lunch", "Lunch at noon?", "Lunch body", ts=1_900_000_200_000)

    def test_search_item_shape_and_tags(self):
        rows = gmailx.search(self.ctx, self.acct["id"], "budget")
        self.assertEqual(len(rows), 1)
        item = rows[0]
        for key in ("account", "id", "thread", "sender", "subject", "snippet", "date", "labels"):
            self.assertIn(key, item)
        self.assertEqual((item["account"], item["tag"], item["thread"], item["id"]), (self.acct["id"], "company", "tm1", "m1"))
        self.assertNotIn("body", item)                             # metadata only

    def test_search_all_accounts_merges_newest_first(self):
        self.google.add_mail("me@corp.com", "m3", "Cy <cy@x.com>", "Third", "s", "b", ts=1_900_000_300_000)
        rows = gmailx.search(self.ctx, None, "", max_results=2)
        self.assertEqual([r["id"] for r in rows], ["m3", "m2"])

    def test_recent_uses_an_after_query(self):
        rows = gmailx.recent(self.ctx, self.acct["id"], hours=24)
        self.assertEqual({r["id"] for r in rows}, {"m1", "m2"})
        sent = [c for c in self.google.calls if c[1].endswith("/messages") and "after:" in c[3].get("q", "")]
        self.assertTrue(sent)
        self.assertEqual(sent[-1][3]["q"], f"after:{int(self.clock[0] - 24 * 3600)}")

    def test_summary_counts_and_top_items(self):
        s = gmailx.summary(self.ctx)
        self.assertEqual(set(s), {"default", self.acct["id"]})
        row = s[self.acct["id"]]
        self.assertEqual((row["unread"], row["important"]), (2, 1))
        self.assertLessEqual(len(row["items"]), 5)
        self.assertEqual(row["label"], "company")

    def test_unconnected_or_unknown_account_is_an_error(self):
        with self.assertRaises(GmailxError):
            gmailx.search(self.ctx, "nope", "x")


class PrivacyTest(Base):
    def setUp(self):
        super().setUp()
        self.acct, _ = self.connect("company", "me@corp.com")
        self.google.add_mail("me@corp.com", "m1", "Ann <ann@corp.com>", "Budget", "snip", "SECRET BODY mail ann@corp.com")

    def test_company_body_withheld_from_cloud_when_flag_off(self):
        self.assertFalse(self.config.gmail_company_ai_allowed)
        m = gmailx.get_message(self.ctx, self.acct["id"], "m1", destination="cloud")
        self.assertEqual((m["body"], m["body_withheld"]), ("", True))
        self.assertEqual(m["subject"], "Budget")                    # metadata still there
        self.assertFalse(gmailx.cloud_ai_allowed(self.ctx, self.acct["id"]))

    def test_company_body_still_readable_by_local_model(self):
        m = gmailx.get_message(self.ctx, self.acct["id"], "m1", destination="local")
        self.assertIn("SECRET BODY", m["body"])
        self.assertFalse(m["body_withheld"])

    def test_flag_on_allows_cloud_but_redacts(self):
        self.config.gmail_company_ai_allowed = True
        m = gmailx.get_message(self.ctx, self.acct["id"], "m1", destination="gemini")
        self.assertFalse(m["body_withheld"])
        self.assertIn("SECRET BODY", m["body"])
        self.assertNotIn("ann@corp.com", m["body"])                 # hub.redact hides the address

    def test_personal_default_is_not_restricted(self):
        self.assertTrue(gmailx.cloud_ai_allowed(self.ctx, "default"))

    def test_relabelling_default_as_company_protects_it(self):
        self.svc.update("default", label="company")
        self.assertFalse(gmailx.cloud_ai_allowed(self.ctx, "default"))
        self.assertEqual(gmailx.account_tag(self.ctx, "default"), "company")

    def test_bad_message_id_is_refused(self):
        with self.assertRaises(GmailxError):
            gmailx.get_message(self.ctx, self.acct["id"], "../x")


class RouteTest(Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(gmailx.make_router(self.ctx))
        self.client = TestClient(app, headers={"X-Hub": "1"})
        self.client.cookies.set("hub_session", "yes")
        self.anon = TestClient(app, headers={"X-Hub": "1"})

    def test_every_route_needs_login(self):
        for method, path in (("get", "/api/gmail/accounts"), ("post", "/api/gmail/accounts/connect"),
                             ("put", "/api/gmail/accounts/default"), ("delete", "/api/gmail/accounts/x"),
                             ("get", "/api/gmail/summary"), ("get", "/api/gmail/search?q=a"), ("post", "/api/gmail/send"),
                             ("post", "/api/gmail/accounts/connect/complete"), ("get", "/api/gmail/accounts/connect/zz")):
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)

    def test_connect_list_update_delete_roundtrip(self):
        self.google.grant("rc", "me@corp.com", [SCOPE_READ])
        r = self.client.post("/api/gmail/accounts/connect", json={"label": "company"})
        self.assertEqual(r.status_code, 200, r.text)
        flow = r.json()
        self.assertIn("accounts.google.com", flow["auth_url"])
        r = self.client.post("/api/gmail/accounts/connect/complete", json={"state": flow["state"], "code": "rc"})
        self.assertEqual(r.status_code, 200, r.text)
        aid = r.json()["id"]
        self.assertEqual(self.client.get(f"/api/gmail/accounts/connect/{flow['state']}").json()["status"], "done")
        listing = self.client.get("/api/gmail/accounts").json()
        self.assertEqual([a["label"] for a in listing["accounts"]], ["personal", "company"])
        self.assertFalse(listing["company_ai_allowed"])
        self.assertNotIn("rt-", json.dumps(listing))
        r = self.client.put(f"/api/gmail/accounts/{aid}", json={"label": "work", "send_enabled": True})
        self.assertEqual((r.json()["label"], r.json()["reconsent_required"]), ("work", True))
        self.assertEqual(self.client.delete(f"/api/gmail/accounts/{aid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/gmail/accounts/{aid}").status_code, 404)
        self.assertEqual(len(self.client.get("/api/gmail/accounts").json()["accounts"]), 1)

    def test_connect_with_client_credentials_saves_them(self):
        self.store.delete(gmailx.CLIENT_KEY)
        r = self.client.post("/api/gmail/accounts/connect", json={"label": "company", "client_id": "abcdefghij12345", "client_secret": "s3cr3t-value"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("abcdefghij12345", self.store.data[gmailx.CLIENT_KEY])

    def test_summary_and_search_routes(self):
        acct, _ = self.connect("company", "me@corp.com")
        self.google.add_mail("me@corp.com", "m1", "Ann <ann@x.com>", "Budget", "snip", "b")
        self.assertEqual(self.client.get("/api/gmail/summary").json()[acct["id"]]["unread"], 1)
        rows = self.client.get("/api/gmail/search", params={"q": "budget", "account": acct["id"]}).json()["results"]
        self.assertEqual(rows[0]["subject"], "Budget")

    def test_send_route_needs_confirm_and_respects_gating(self):
        acct, _ = self.connect("company", "me@corp.com")
        body = {"account": acct["id"], "to": "bob@example.com", "subject": "Hi", "body": "x"}
        self.assertEqual(self.client.post("/api/gmail/send", json=body).status_code, 400)
        r = self.client.post("/api/gmail/send", json={**body, "confirm": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn("switched off", r.json()["detail"])

    def test_company_flag_is_a_settings_key(self):
        from hub import settings
        self.assertIn("gmail_company_ai_allowed", settings.ALLOWED_KEYS)
        changes, _ = settings.validate_patch(self.config, {"gmail_company_ai_allowed": True})
        self.assertEqual(changes, {"gmail_company_ai_allowed": True})
        with self.assertRaises(Exception):
            settings.validate_patch(self.config, {"gmail_company_ai_allowed": "yes"})


class FakeHubCalls:
    """InProcessClient stand-in for the assistant tools: answers from the real router."""

    def __init__(self, client):
        self.client = client

    async def call(self, method, path, params=None, body=None):
        r = self.client.request(method, path, params=params, json=body)
        data = r.json()
        if r.status_code >= 400:
            return False, data.get("detail", "error")
        return True, data


class ToolTest(RouteTest):
    def setUp(self):
        super().setUp()
        self.tools = {t.spec["name"]: t for t in gmailx_tools.build(lambda: None, self.config)}
        self.hub = FakeHubCalls(self.client)

    def run_tool(self, name, **args):
        tool = self.tools[name]
        with mock.patch("hub.gmailx_tools.InProcessClient", return_value=self.hub):
            tools = {t.spec["name"]: t for t in gmailx_tools.build(lambda: None, self.config)}
        return asyncio.run(tools[name].run(args))

    def test_tools_exist_are_untrusted_and_free(self):
        for name in ("gmail_summary", "gmail_search"):
            tool = self.tools[name]
            self.assertTrue(tool.untrusted({}))
            self.assertFalse(tool.needs_approval({}))

    def test_summary_and_search_text_has_tags_and_no_bodies(self):
        acct, _ = self.connect("company", "me@corp.com")
        self.google.add_mail("me@corp.com", "m1", "Ann <ann@x.com>", "Budget", "short snippet", "SECRET BODY")
        ok, text = self.run_tool("gmail_summary", account="company")
        self.assertTrue(ok)
        self.assertIn("[company]", text)
        self.assertIn("Budget", text)
        ok, text = self.run_tool("gmail_search", query="budget", account="company")
        self.assertTrue(ok)
        self.assertIn("[company]", text)
        self.assertNotIn("SECRET BODY", text)
        ok, text = self.run_tool("gmail_search", query="x", account="nonesuch")
        self.assertFalse(ok)


class SendEmailToolTest(RouteTest):
    """The existing approval-gated send_email tool must name the account and respect send gating."""

    def build(self):
        from hub import assistant_tools
        with mock.patch("hub.assistant_tools.Hub", return_value=FakeHubCalls(self.client)), \
                mock.patch("hub.assistant_tools.InProcessClient", return_value=FakeHubCalls(self.client)):
            tools = assistant_tools.build(lambda: None, self.config)
        return next(t for t in tools if t.spec["name"] == "send_email")

    def test_still_approval_gated_and_schema_names_account(self):
        tool = self.build()
        self.assertTrue(tool.needs_approval({}))
        self.assertIn("account", tool.spec["input_schema"]["properties"])

    def test_two_accounts_require_a_named_account(self):
        self.connect("company", "me@corp.com", send=True)
        tool = self.build()
        ok, msg = asyncio.run(tool.run({"to": "bob@example.com", "subject": "Hi", "body": "x"}))
        self.assertFalse(ok)
        self.assertIn("which account", msg.lower())

    def test_send_enabled_company_account_sends_through_gmailx(self):
        acct, _ = self.connect("company", "me@corp.com", send=True)
        tool = self.build()
        ok, msg = asyncio.run(tool.run({"to": "bob@example.com", "subject": "Hi", "body": "x", "account": "company"}))
        self.assertTrue(ok, msg)
        self.assertEqual(self.google.sent[0][0], "me@corp.com")

    def test_read_only_account_is_refused_with_a_clear_message(self):
        self.connect("company", "me@corp.com")
        tool = self.build()
        ok, msg = asyncio.run(tool.run({"to": "bob@example.com", "subject": "Hi", "body": "x", "account": "company"}))
        self.assertFalse(ok)
        self.assertIn("switched off", msg)
        self.assertEqual(self.google.sent, [])
        self.assertEqual(self.legacy.sent, [])


if __name__ == "__main__":
    unittest.main()
