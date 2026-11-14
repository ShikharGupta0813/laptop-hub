"""GMAILX: more than one Gmail account (personal + company). See docs/api/gmailx.md.

Accounts
- `default` (label "personal") is the connection the hub already had: the Gmail address + app password kept in the
  OS keyring by the Jarvis mailer (service "jarvis-gmail"). It is NOT touched or re-authorised: reading goes through
  the same IMAP code as hub/inbox.py, sending through the Jarvis mailer. Nothing is migrated away, nothing is lost.
- Extra accounts (label "company" or any word) are connected with Google OAuth (installed-app / loopback flow with
  PKCE). Their tokens live in the OS keyring (service "redline-gmail", one entry per account id) and nowhere else.

Scopes are the smallest that do the job: `gmail.readonly` always; `gmail.send` ONLY after the owner switches
"allow sending from this account" on (that needs a fresh consent). Never `mail.google.com`.

Third kind of account (hub/mailaccounts.py): a generic IMAP/SMTP account (Outlook / Office 365, Yahoo, Zoho, iCloud, a company
server). Its password lives in the keyring under `imap:<id>`. It is read-only unless "allow sending" is on, and sending is only
ever an explicit send_mail call.

Tags are free text chips (personal, company, anything). Privacy is a PER-ACCOUNT switch `cloud_ai_allowed`: when the owner never
set it, it defaults to False for an account tagged company or work (then `gmail_company_ai_allowed` in settings, the old global
switch, may still allow it) and True for any other tag. When it is False the message BODIES of that account are never given to
a cloud model (only sender, subject and the snippet; only a local model may read a body). hub.redact is applied to anything
that does go to a cloud model.

The module-level functions at the bottom are the interface other modules import (inside try/except ImportError).
They are BLOCKING (they do network calls): from async code use `await asyncio.to_thread(...)`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import threading
import time
import email.utils
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool

from .common import Ctx, JsonFile

log = logging.getLogger("hub.gmailx")

SECRET_SERVICE = "redline-gmail"
CLIENT_KEY = "oauth_client"
DEFAULT_ID = "default"
DEFAULT_LABEL = "personal"
SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
LEGACY_SCOPES = ["imap: read-only (app password)", "smtp: send (app password)"]
MAX_ACCOUNTS = 4
MAX_IMAP_ACCOUNTS = 12
MAX_TAGS = 8
PROTECTED_TAGS = ("company", "work")
IMAP_PREFIX = "imap:"
_UNSET = object()
FLOW_TTL = 600
MAX_ATTACH_BYTES = 20 * 1024 * 1024
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,29}$")
ADDR_RE = re.compile(r"^[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}\.[A-Za-z]{2,}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
LOCAL_PROVIDERS = ("local", "ollama")


class GmailxError(Exception):
    """A problem in plain words; `status` is the HTTP status the routes use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- secret store ---------------------------------------------------------------------------------------------

class KeyringStore:
    """The OS keyring, the same place the hub keeps every other secret."""

    def __init__(self, service: str = SECRET_SERVICE):
        self.service = service

    def _kr(self):
        try:
            import keyring
        except ImportError:
            raise GmailxError("The OS keyring isn't available. Run: pip install -r requirements-work.txt", 503) from None
        return keyring

    def get(self, key: str) -> str | None:
        try:
            return self._kr().get_password(self.service, key)
        except GmailxError:
            raise
        except Exception:  # noqa: BLE001 - no backend, locked keyring...
            return None

    def set(self, key: str, value: str) -> None:
        try:
            self._kr().set_password(self.service, key, value)
        except GmailxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GmailxError(f"Couldn't save to the OS keyring ({type(exc).__name__}).", 503) from None

    def delete(self, key: str) -> None:
        try:
            self._kr().delete_password(self.service, key)
        except GmailxError:
            raise
        except Exception:  # noqa: BLE001 - already gone
            pass


class MemoryStore:
    """For tests and the dev server: a dict standing in for the keyring."""

    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


# ---- http ----------------------------------------------------------------------------------------------------

def default_http(method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
                 data: dict | None = None, json_body: Any = None) -> tuple[int, Any]:
    """(status, parsed json or {}). The only place this module touches the network."""
    import httpx
    try:
        r = httpx.request(method, url, headers=headers, params=params, data=data, json=json_body, timeout=20.0)
    except httpx.HTTPError as exc:
        raise GmailxError(f"Couldn't reach Google ({type(exc).__name__}). Check the internet connection.", 502) from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


class LegacyBridge:
    """The existing app-password Gmail connection, through the Jarvis helper modules (hub/work/jarvis_bridge.py)."""

    def __init__(self, config):
        self.config = config
        self._bridge = None

    def _b(self):
        if self._bridge is None:
            from .work.jarvis_bridge import Bridge
            self._bridge = Bridge(getattr(self.config, "jarvis_dir", "") or "")
        return self._bridge

    def configured(self) -> bool:
        if not (getattr(self.config, "jarvis_dir", "") or "").strip():
            return False
        try:
            return bool(self._b().state("email").configured)
        except Exception:  # noqa: BLE001
            return False

    def account(self) -> tuple[str, str]:
        state = self._b().state("email")
        if not state.configured:
            raise GmailxError(state.reason or "Email isn't set up yet. Run setup email in the Jarvis folder.", 503)
        try:
            return state.module.account()
        except Exception:  # noqa: BLE001
            raise GmailxError("Couldn't read your saved email settings.", 503) from None

    def address(self) -> str:
        return self.account()[0]

    def box(self):
        from .inbox import ImapMailBox
        address, password = self.account()
        return ImapMailBox(address, password)

    def send(self, to: str, subject: str, body: str) -> None:
        state = self._b().state("email")
        if not state.configured:
            raise GmailxError(state.reason or "Email isn't set up yet.", 503)
        state.module.send(to, subject, body)


# ---- helpers -------------------------------------------------------------------------------------------------

def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def tags_protected(tags) -> bool:
    """True when any tag is company or work (the default that keeps mail bodies off cloud AI)."""
    return any(str(t).strip().lower() in PROTECTED_TAGS for t in tags or [])


def clean_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise GmailxError(f"Tags are a list of up to {MAX_TAGS} short words.")
    out: list[str] = []
    for t in tags:
        t = str(t).strip()
        if not LABEL_RE.match(t):
            raise GmailxError("A tag is 1 to 30 letters, digits, spaces, . _ -.")
        if t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out


def is_protected_label(label: str) -> bool:
    """Only the label "personal" is ordinary. Anything else (company, a custom word) is treated as work mail."""
    return str(label or "").strip().lower() != DEFAULT_LABEL


def _iso_from_ms(ms: Any) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _ts_of(date: str) -> float:
    try:
        return email.utils.parsedate_to_datetime(date).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---- the service ---------------------------------------------------------------------------------------------

class Gmailx:
    def __init__(self, ctx: Ctx, *, secrets_store=None, http: Callable | None = None, legacy=None,
                 now: Callable[[], float] = time.time, listen: bool = True, imap_factory: Callable | None = None,
                 smtp_factory: Callable | None = None):
        self.ctx = ctx
        self.config = ctx.config
        self.secrets = secrets_store or KeyringStore()
        self.http = http or default_http
        self.legacy = legacy or LegacyBridge(ctx.config)
        self.now = now
        self.listen = listen
        self.imap_factory = imap_factory      # tests: (entry, password) -> a mailbox object
        self.smtp_factory = smtp_factory      # tests: (entry) -> an smtplib.SMTP-like object
        self.lock = threading.RLock()
        self.flows: dict[str, dict] = {}

    # -- registry (labels and flags only; never a secret)

    def _file(self) -> JsonFile:
        return JsonFile(Path(self.config.data_path()) / "gmailx.json")

    def _load(self) -> dict:
        data = self._file().load({})
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, dict):
            accounts = {}
        if DEFAULT_ID not in accounts:   # migration: the existing single connection becomes "default" / personal
            accounts[DEFAULT_ID] = {"label": DEFAULT_LABEL, "kind": "legacy", "send_enabled": True, "address": ""}
        return {"accounts": accounts}

    def _save(self, data: dict) -> None:
        self._file().save(data)

    def company_ai_allowed(self) -> bool:
        return getattr(self.config, "gmail_company_ai_allowed", False) is True

    def _imap_password(self, account_id: str) -> str:
        try:
            return self.secrets.get(IMAP_PREFIX + account_id) or ""
        except GmailxError:
            return ""

    def _imap_box(self, account_id: str):
        from . import mailaccounts
        entry = self._entry(account_id)
        password = self._imap_password(account_id)
        if not password:
            raise GmailxError("This account has no saved password. Edit it and enter the password again.", 409)
        if self.imap_factory is not None:
            return self.imap_factory(entry, password)
        return mailaccounts.open_box(entry, password)

    def _entry(self, account_id: str) -> dict:
        entry = self._load()["accounts"].get(str(account_id))
        if entry is None:
            raise GmailxError("There is no Gmail account with that id.", 404)
        return entry

    def _view(self, account_id: str, entry: dict) -> dict:
        if entry.get("kind") == "legacy":
            connected, address = False, entry.get("address") or ""
            try:
                connected = bool(self.legacy.configured())
                if connected:
                    address = self.legacy.address()
            except Exception:  # noqa: BLE001
                connected = False
            scopes = list(LEGACY_SCOPES) if connected else []
            reconsent = False
        elif entry.get("kind") == "imap":
            connected = bool(self._imap_password(account_id)) and not entry.get("needs_reconnect")
            address = entry.get("address") or entry.get("username") or ""
            scopes = ["imap: read-only"] + (["smtp: send"] if entry.get("smtp_host") and entry.get("send_enabled") else [])
            reconsent = False
        else:
            token = self._token_blob(account_id)
            connected = bool(token and token.get("refresh_token")) and not entry.get("needs_reconnect")
            address = entry.get("address") or ""
            scopes = list(token.get("scopes") or []) if token else []
            reconsent = bool(entry.get("send_enabled")) and SCOPE_SEND not in scopes
        label = entry.get("label") or DEFAULT_LABEL
        view = {"id": account_id, "label": label, "address": address, "connected": connected, "scopes": scopes,
                "kind": entry.get("kind", "oauth"), "send_enabled": bool(entry.get("send_enabled")),
                "reconsent_required": reconsent, "tags": self._tags(entry),
                "cloud_ai_allowed": self._cloud_ok(entry), "cloud_ai_default": self._cloud_default(entry),
                "cloud_ai_explicit": isinstance(entry.get("cloud_ai_allowed"), bool), "alerts": entry.get("alerts") is not False}
        view["protected"] = not view["cloud_ai_allowed"]
        if entry.get("kind") == "imap":
            view.update({k: entry.get(k) for k in ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port",
                                                   "smtp_security", "username")})
            view["has_password"] = bool(self._imap_password(account_id))
        return view

    @staticmethod
    def _tags(entry: dict) -> list[str]:
        t = entry.get("tags")
        if isinstance(t, list) and t:
            return [str(x) for x in t]
        return [str(entry.get("label") or DEFAULT_LABEL)]

    def _cloud_default(self, entry: dict) -> bool:
        if tags_protected(self._tags(entry)):
            return self.company_ai_allowed()
        return True

    def _cloud_ok(self, entry: dict) -> bool:
        v = entry.get("cloud_ai_allowed")
        return v if isinstance(v, bool) else self._cloud_default(entry)

    def list_accounts(self) -> list[dict]:
        accounts = self._load()["accounts"]
        order = sorted(accounts, key=lambda k: (k != DEFAULT_ID, k))
        return [self._view(k, accounts[k]) for k in order]

    def client_configured(self) -> bool:
        return bool(self._client())

    def tag_of(self, account_id: str) -> str:
        try:
            return str(self._entry(account_id).get("label") or DEFAULT_LABEL)
        except GmailxError:
            return DEFAULT_LABEL

    def cloud_ai_allowed(self, account_id: str) -> bool:
        try:
            return self._cloud_ok(self._entry(account_id))
        except GmailxError:
            return True

    # -- tokens

    def _token_blob(self, account_id: str) -> dict | None:
        try:
            raw = self.secrets.get(account_id)
        except GmailxError:
            return None
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _client(self) -> dict | None:
        try:
            raw = self.secrets.get(CLIENT_KEY)
            data = json.loads(raw) if raw else None
        except (GmailxError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("client_id") and data.get("client_secret") else None

    def set_client(self, client_id: str, client_secret: str) -> None:
        client_id, client_secret = str(client_id or "").strip(), str(client_secret or "").strip()
        if not (10 <= len(client_id) <= 300 and 4 <= len(client_secret) <= 300) or any(c.isspace() for c in client_id + client_secret):
            raise GmailxError("That doesn't look like a Google OAuth client id and secret.")
        self.secrets.set(CLIENT_KEY, json.dumps({"client_id": client_id, "client_secret": client_secret}))

    def _access_token(self, account_id: str, force: bool = False) -> str:
        blob = self._token_blob(account_id)
        if not blob or not blob.get("refresh_token"):
            raise GmailxError("That account isn't connected. Connect it again in Settings.", 409)
        if not force and blob.get("access_token") and float(blob.get("expires_at") or 0) > self.now() + 60:
            return blob["access_token"]
        client = self._client()
        if not client:
            raise GmailxError("The Google OAuth client id and secret are missing. Add them in Settings, Gmail accounts.", 409)
        status, body = self.http("POST", TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": blob["refresh_token"], "grant_type": "refresh_token"})
        if status in (400, 401) and isinstance(body, dict) and body.get("error") == "invalid_grant":
            self._mark(account_id, needs_reconnect=True)
            raise GmailxError("Google no longer accepts this account's sign-in. Disconnect it and connect it again.", 409)
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise GmailxError(f"Google refused to refresh the sign-in (status {status}).", 502)
        blob["access_token"] = body["access_token"]
        blob["expires_at"] = self.now() + int(body.get("expires_in") or 3600)
        self.secrets.set(account_id, json.dumps(blob))
        return blob["access_token"]

    def _mark(self, account_id: str, **fields) -> None:
        with self.lock:
            data = self._load()
            if account_id in data["accounts"]:
                data["accounts"][account_id].update(fields)
                self._save(data)

    def _api(self, account_id: str, method: str, path: str, *, params=None, body=None) -> Any:
        for attempt in (0, 1):
            token = self._access_token(account_id, force=attempt == 1)
            status, data = self.http(method, API + path, headers={"Authorization": f"Bearer {token}"},
                                     params=params, json_body=body)
            if status == 401 and attempt == 0:
                continue
            break
        if status == 403:
            raise GmailxError("Google says this account didn't give permission for that. Check the account's scopes in Settings.", 403)
        if status == 404:
            raise GmailxError("Gmail has no such message.", 404)
        if status == 429:
            raise GmailxError("Gmail is rate-limiting requests right now. Try again in a minute.", 429)
        if status >= 400 or not isinstance(data, (dict, list)):
            raise GmailxError(f"Gmail answered with an error (status {status}).", 502)
        return data

    # -- connecting (OAuth installed-app flow with PKCE)

    def begin_connect(self, label: str, *, send_enabled: bool = False, account_id: str | None = None,
                      client_id: str | None = None, client_secret: str | None = None) -> dict:
        label = str(label or "").strip()
        if account_id:
            entry = self._entry(account_id)
            if entry.get("kind") == "legacy":
                raise GmailxError("The default account uses your Gmail app password; it has nothing to re-consent.")
            label = entry.get("label") or label
            send_enabled = bool(entry.get("send_enabled")) or send_enabled
        if not LABEL_RE.match(label):
            raise GmailxError("Give the account a short label such as company (letters, digits, spaces, . _ -).")
        if client_id or client_secret:
            self.set_client(client_id or "", client_secret or "")
        client = self._client()
        if not client:
            raise GmailxError("Add your Google OAuth client id and secret first (Desktop app type; see the Gmail guide).", 409)
        if not account_id and len([a for a in self._load()["accounts"].values() if a.get("kind", "oauth") == "oauth"]) >= MAX_ACCOUNTS:
            raise GmailxError(f"At most {MAX_ACCOUNTS} extra accounts can be connected.")
        scopes = [SCOPE_READ] + ([SCOPE_SEND] if send_enabled else [])
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        flow = {"state": state, "verifier": verifier, "label": label, "scopes": scopes, "send_enabled": bool(send_enabled),
                "account_id": account_id, "created": self.now(), "status": "pending", "error": "", "redirect_uri": "http://127.0.0.1:1/"}
        if self.listen:
            flow["redirect_uri"] = self._start_listener(state)
        with self.lock:
            self._expire_flows()
            self.flows[state] = flow
        params = {"client_id": client["client_id"], "redirect_uri": flow["redirect_uri"], "response_type": "code",
                  "scope": " ".join(scopes), "state": state, "access_type": "offline", "prompt": "consent",
                  "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
        self.ctx.audit("gmail_connect_start", label=label, send=bool(send_enabled), reconsent=bool(account_id))
        return {"auth_url": AUTH_URL + "?" + urlencode(params), "state": state, "redirect_uri": flow["redirect_uri"],
                "scopes": scopes, "expires_in": FLOW_TTL}

    def _expire_flows(self) -> None:
        cutoff = self.now() - FLOW_TTL
        for key in [k for k, f in self.flows.items() if f["created"] < cutoff]:
            self.flows.pop(key, None)

    def flow_status(self, state: str) -> dict:
        flow = self.flows.get(state)
        if not flow:
            raise GmailxError("That sign-in attempt is unknown or has expired. Start again.", 404)
        return {"status": flow["status"], "error": flow["error"], "account_id": flow.get("result")}

    def complete_connect(self, state: str, code: str | None = None, redirect_url: str | None = None) -> dict:
        with self.lock:
            flow = self.flows.get(state)
        if not flow or flow["created"] < self.now() - FLOW_TTL:
            raise GmailxError("That sign-in attempt is unknown or has expired. Start again.", 404)
        if flow["status"] == "done":
            return self._view(flow["result"], self._entry(flow["result"]))
        if not code and redirect_url:
            q = parse_qs(urlsplit(str(redirect_url)).query)
            if (q.get("state") or [""])[0] != state:
                raise GmailxError("That address belongs to a different sign-in attempt.")
            if q.get("error"):
                self._fail_flow(flow, "Google said no: " + _clean(q["error"][0], 60))
            code = (q.get("code") or [""])[0]
        if not code:
            raise GmailxError("No sign-in code was given.")
        client = self._client()
        if not client:
            raise GmailxError("The Google OAuth client id and secret are missing.", 409)
        status, tok = self.http("POST", TOKEN_URL, data={
            "client_id": client["client_id"], "client_secret": client["client_secret"], "code": code,
            "code_verifier": flow["verifier"], "grant_type": "authorization_code", "redirect_uri": flow["redirect_uri"]})
        if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
            self._fail_flow(flow, "Google didn't accept the sign-in code. Start again.")
        if not tok.get("refresh_token"):
            self._fail_flow(flow, "Google gave no long-lived sign-in. Remove REDLINE at myaccount.google.com/permissions and try again.")
        granted = sorted(set(str(tok.get("scope") or " ".join(flow["scopes"])).split()))
        if SCOPE_READ not in granted:
            self._fail_flow(flow, "Read access was not granted, so the account can't be used.")
        status, profile = self.http("GET", API + "/profile", headers={"Authorization": f"Bearer {tok['access_token']}"})
        address = str(profile.get("emailAddress") or "") if status == 200 and isinstance(profile, dict) else ""
        if not ADDR_RE.match(address):
            self._fail_flow(flow, "Couldn't read the account's address from Gmail.")
        with self.lock:
            data = self._load()
            for other_id, other in data["accounts"].items():
                if other.get("address", "").lower() == address.lower() and other_id != flow.get("account_id"):
                    self._fail_flow(flow, f"{address} is already connected as {other.get('label')}.")
            account_id = flow.get("account_id") or "g" + secrets.token_hex(4)
            if flow.get("account_id") and data["accounts"][account_id].get("address", address).lower() != address.lower():
                self._fail_flow(flow, "That is a different Google account. Sign in with the one you are re-consenting.")
            self.secrets.set(account_id, json.dumps({
                "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
                "expires_at": self.now() + int(tok.get("expires_in") or 3600), "scopes": granted}))
            entry = data["accounts"].get(account_id, {})
            entry.update({"label": flow["label"], "kind": "oauth", "address": address,
                          "send_enabled": bool(flow["send_enabled"]) and SCOPE_SEND in granted,
                          "needs_reconnect": False})
            data["accounts"][account_id] = entry
            self._save(data)
            flow["status"], flow["result"] = "done", account_id
        self.ctx.audit("gmail_connected", account=account_id, label=flow["label"], scopes=granted)
        return self._view(account_id, entry)

    def _fail_flow(self, flow: dict, message: str):
        flow["status"], flow["error"] = "error", message
        raise GmailxError(message)

    def _start_listener(self, state: str) -> str:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 - silence
                pass

            def do_GET(self):  # noqa: N802
                q = parse_qs(urlsplit(self.path).query)
                ok, text = False, "Sign-in failed. Go back to REDLINE and try again."
                if (q.get("state") or [""])[0] == state:
                    try:
                        if q.get("error"):
                            raise GmailxError("denied")
                        service.complete_connect(state, code=(q.get("code") or [""])[0])
                        ok, text = True, "Connected. You can close this tab and go back to REDLINE."
                    except GmailxError:
                        pass
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(text.encode())
                service.flows.get(state, {})["_served"] = True

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 1.0

        def serve():
            end = time.time() + FLOW_TTL
            while time.time() < end and not self.flows.get(state, {}).get("_served"):
                server.handle_request()
            server.server_close()

        threading.Thread(target=serve, name="gmailx-oauth", daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}/"

    # -- changing and removing

    def update(self, account_id: str, label: str | None = None, send_enabled: bool | None = None, *, tags: Any = _UNSET,
               cloud_ai_allowed: Any = _UNSET, imap: dict | None = None, password: str | None = None,
               alerts: bool | None = None) -> dict:
        """Edit an account. `tags` replaces the chips; `cloud_ai_allowed` True/False pins the switch, None goes back to the
        default; `imap` (server settings) and `password` apply to IMAP accounts only. The password is never echoed."""
        with self.lock:
            data = self._load()
            entry = data["accounts"].get(account_id)
            if entry is None:
                raise GmailxError("There is no Gmail account with that id.", 404)
            if label is not None:
                label = str(label).strip()
                if not LABEL_RE.match(label):
                    raise GmailxError("A label is 1 to 30 letters, digits, spaces, . _ -.")
                entry["label"] = label
            if send_enabled is not None:
                entry["send_enabled"] = bool(send_enabled)
            if tags is not _UNSET:
                entry["tags"] = clean_tags(tags)
            if alerts is not None:
                entry["alerts"] = bool(alerts)
            if cloud_ai_allowed is not _UNSET:
                if cloud_ai_allowed is None:
                    entry.pop("cloud_ai_allowed", None)
                elif isinstance(cloud_ai_allowed, bool):
                    entry["cloud_ai_allowed"] = cloud_ai_allowed
                else:
                    raise GmailxError("cloud_ai_allowed is true, false or null.")
            if imap or password:
                if entry.get("kind") != "imap":
                    raise GmailxError("Server settings and passwords are only for IMAP accounts.")
                from . import mailaccounts
                try:
                    if imap:
                        entry.update(mailaccounts.clean_settings(imap, entry))
                except mailaccounts.ImapProblem as exc:
                    raise GmailxError(str(exc)) from None
                if password:
                    self._set_imap_password(account_id, password)
                    entry.pop("needs_reconnect", None)
            self._save(data)
        self.ctx.audit("gmail_account_update", account=account_id, label=entry.get("label"), send=bool(entry.get("send_enabled")))
        return self._view(account_id, entry)

    def _set_imap_password(self, account_id: str, password: str) -> None:
        password = str(password or "").strip()
        if not 1 <= len(password) <= 300 or any(c in password for c in "\r\n\x00"):
            raise GmailxError("That password cannot be used.")
        self.secrets.set(IMAP_PREFIX + account_id, password)

    def add_imap(self, label: str, address: str, password: str, *, tags: Any = None, send_enabled: bool = False,
                 cloud_ai_allowed: Any = None, **server) -> dict:
        """Add a generic IMAP/SMTP account. `server` = preset, imap_host, imap_port, imap_security, smtp_host, smtp_port,
        smtp_security, username. Logs in once to check the password unless `server` says otherwise (tests use a fake)."""
        from . import mailaccounts
        label = str(label or "").strip()
        if not LABEL_RE.match(label):
            raise GmailxError("Give the account a short label such as work (letters, digits, spaces, . _ -).")
        address = str(address or "").strip()
        if not ADDR_RE.match(address):
            raise GmailxError("Give the account's email address.")
        server.setdefault("username", address)
        try:
            settings = mailaccounts.clean_settings({k: v for k, v in server.items() if k in (
                "preset", "imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security", "username")})
        except mailaccounts.ImapProblem as exc:
            raise GmailxError(str(exc)) from None
        with self.lock:
            data = self._load()
            if len([a for a in data["accounts"].values() if a.get("kind") == "imap"]) >= MAX_IMAP_ACCOUNTS:
                raise GmailxError(f"At most {MAX_IMAP_ACCOUNTS} mail-server accounts can be added.")
            base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:20] or "mail"
            account_id, n = "imap-" + base, 1
            while account_id in data["accounts"]:
                n += 1
                account_id = f"imap-{base}-{n}"
            entry = {"label": label, "kind": "imap", "address": address, "send_enabled": bool(send_enabled),
                     "tags": clean_tags(tags if tags else [label]), **settings}
            if isinstance(cloud_ai_allowed, bool):
                entry["cloud_ai_allowed"] = cloud_ai_allowed
            data["accounts"][account_id] = entry
            self._set_imap_password(account_id, password)
            self._save(data)
        try:
            self._imap_box(account_id).unread_count("INBOX")      # one harmless call: does the login work?
        except Exception as exc:  # noqa: BLE001 - a login that fails is not kept
            self._forget(account_id)
            msg = str(exc) if exc.__class__.__name__ == "MailBoxError" else "Couldn't sign in to that mail server."
            raise GmailxError(msg.replace("Gmail", "The mail server"), 400) from None
        self.ctx.audit("mail_account_added", account=account_id, kind="imap")
        return self._view(account_id, entry)

    def _forget(self, account_id: str) -> None:
        with self.lock:
            data = self._load()
            data["accounts"].pop(account_id, None)
            self._save(data)
        self.secrets.delete(IMAP_PREFIX + account_id)

    def disconnect(self, account_id: str) -> None:
        with self.lock:
            data = self._load()
            entry = data["accounts"].get(account_id)
            if entry is None:
                raise GmailxError("There is no Gmail account with that id.", 404)
            if entry.get("kind") == "legacy":
                raise GmailxError("The default account is your Gmail app password. Remove it with: python -m hub setup --only gmail "
                                  "(or delete the jarvis-gmail entry in the Windows Credential Manager).")
            blob = self._token_blob(account_id) if entry.get("kind") != "imap" else None
            self.secrets.delete(IMAP_PREFIX + account_id if entry.get("kind") == "imap" else account_id)
            data["accounts"].pop(account_id, None)
            self._save(data)
        if blob and blob.get("refresh_token"):   # best effort: tell Google too
            try:
                self.http("POST", REVOKE_URL, data={"token": blob["refresh_token"]})
            except Exception:  # noqa: BLE001
                pass
        self.ctx.audit("gmail_disconnected", account=account_id)

    # -- reading

    def _connected_ids(self, account_id: str | None) -> list[str]:
        views = self.list_accounts()
        if account_id is not None:
            for v in views:
                if v["id"] == account_id:
                    if not v["connected"]:
                        raise GmailxError("That account isn't connected.", 409)
                    return [account_id]
            raise GmailxError("There is no Gmail account with that id.", 404)
        return [v["id"] for v in views if v["connected"]]

    def _item(self, account_id: str, tag: str, m: dict) -> dict:
        headers = {h.get("name", "").lower(): h.get("value", "") for h in (m.get("payload") or {}).get("headers") or []
                   if isinstance(h, dict)}
        date = _iso_from_ms(m.get("internalDate")) or _clean(headers.get("date"), 60)
        return {"account": account_id, "tag": tag, "id": str(m.get("id") or ""), "thread": str(m.get("threadId") or ""),
                "sender": _clean(headers.get("from"), 200), "subject": _clean(headers.get("subject"), 300),
                "snippet": _clean(m.get("snippet"), 300), "date": date,
                "labels": [str(x) for x in (m.get("labelIds") or [])][:20]}

    def _search_one(self, account_id: str, query: str, limit: int) -> list[dict]:
        tag = self.tag_of(account_id)
        kind = self._entry(account_id).get("kind")
        if kind == "legacy":
            return self._legacy_items(account_id, tag, query, limit)
        if kind == "imap":
            return self._imap_items(account_id, tag, query, limit)
        found = self._api(account_id, "GET", "/messages", params={"q": query, "maxResults": limit})
        out = []
        for ref in (found.get("messages") or [])[:limit] if isinstance(found, dict) else []:
            if not isinstance(ref, dict) or not ID_RE.match(str(ref.get("id") or "")):
                continue
            m = self._api(account_id, "GET", f"/messages/{ref['id']}",
                          params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
            if isinstance(m, dict):
                out.append(self._item(account_id, tag, m))
        return out

    def _imap_items(self, account_id: str, tag: str, query: str, limit: int, *, hours: float | None = None,
                    unread: bool | None = None) -> list[dict]:
        from .inbox import MailBoxError
        try:
            return self._legacy_items(account_id, tag, query, limit, hours=hours, unread=unread, box=self._imap_box(account_id))
        except MailBoxError as exc:
            raise GmailxError(str(exc).replace("Gmail", "The mail server"), exc.status if exc.status in (400, 404, 409) else 502) from None

    def _legacy_items(self, account_id: str, tag: str, query: str, limit: int, *, hours: float | None = None,
                      unread: bool | None = None, box: Any = None) -> list[dict]:
        from .inbox import MailQuery, parse_message
        box = box or self.legacy.box()
        q = query.strip()
        text = "" if q.startswith(("after:", "is:", "in:")) else q
        raws = box.list("INBOX", MailQuery(unread=unread, text=text), max(limit, 50) if hours else limit, 6144)
        out = []
        for raw in raws:
            m = parse_message(raw)
            item = {"account": account_id, "tag": tag, "id": str(m.uid), "thread": "", "sender": m.sender,
                    "subject": m.subject, "snippet": m.snippet, "date": m.date, "labels": ["UNREAD"] if m.unread else []}
            if hours is not None and _ts_of(m.date) and _ts_of(m.date) < self.now() - hours * 3600:
                continue
            out.append(item)
        return out[:limit]

    def search(self, account_id: str | None, query: str, max_results: int = 20) -> list[dict]:
        query = _clean(query, 300)
        limit = max(1, min(int(max_results or 20), 50))
        items: list[dict] = []
        for aid in self._connected_ids(account_id):
            try:
                items += self._search_one(aid, query, limit)
            except GmailxError:
                if account_id is not None:
                    raise
        items.sort(key=lambda i: _ts_of(i["date"]), reverse=True)
        return items[:limit]

    def recent(self, account_id: str | None, hours: float = 24, max_results: int = 30) -> list[dict]:
        hours = max(0.1, min(float(hours or 24), 24 * 30))
        after = int(self.now() - hours * 3600)
        items: list[dict] = []
        for aid in self._connected_ids(account_id):
            try:
                kind = self._entry(aid).get("kind")
                if kind == "legacy":
                    items += self._legacy_items(aid, self.tag_of(aid), "", max_results, hours=hours)
                elif kind == "imap":
                    items += self._imap_items(aid, self.tag_of(aid), "", max_results, hours=hours)
                else:
                    items += self._search_one(aid, f"after:{after}", max_results)
            except GmailxError:
                if account_id is not None:
                    raise
        items.sort(key=lambda i: _ts_of(i["date"]), reverse=True)
        return items[:max_results]

    def summary(self) -> dict:
        out: dict[str, dict] = {}
        for v in self.list_accounts():
            if not v["connected"]:
                continue
            aid = v["id"]
            row: dict = {"label": v["label"], "address": v["address"], "unread": 0, "important": 0, "items": []}
            try:
                if v["kind"] == "legacy":
                    items = self._legacy_items(aid, v["label"], "", 5, unread=True)
                    row["unread"] = self.legacy.box().unread_count("INBOX")
                    row["items"] = items
                elif v["kind"] == "imap":
                    from .inbox import MailBoxError
                    try:
                        row["unread"] = self._imap_box(aid).unread_count("INBOX")
                    except MailBoxError as exc:
                        raise GmailxError(str(exc).replace("Gmail", "The mail server")) from None
                    row["items"] = self._imap_items(aid, v["label"], "", 5, unread=True)
                else:
                    label = self._api(aid, "GET", "/labels/INBOX")
                    row["unread"] = int(label.get("messagesUnread") or 0) if isinstance(label, dict) else 0
                    imp = self._api(aid, "GET", "/messages", params={"q": "is:unread is:important in:inbox", "maxResults": 1})
                    row["important"] = int(imp.get("resultSizeEstimate") or 0) if isinstance(imp, dict) else 0
                    row["items"] = self._search_one(aid, "is:unread in:inbox", 5)
            except GmailxError as exc:
                row["error"] = str(exc)
            out[aid] = row
        return out

    # -- bodies (the privacy gate lives here)

    def get_message(self, account_id: str, message_id: str, *, destination: str = "local") -> dict:
        """One message with its body. destination "cloud" means the text is going to a cloud model: for a protected
        account with `gmail_company_ai_allowed` off the body is withheld (empty, `body_withheld` true)."""
        if not ID_RE.match(str(message_id)):
            raise GmailxError("That isn't a message id.")
        self._connected_ids(account_id)
        tag = self.tag_of(account_id)
        cloud = str(destination).lower() not in LOCAL_PROVIDERS
        kind = self._entry(account_id).get("kind")
        if kind in ("legacy", "imap"):
            from .inbox import MailBoxError, parse_message
            try:
                box = self.legacy.box() if kind == "legacy" else self._imap_box(account_id)
                raw = box.get("INBOX", int(message_id), 3_000_000)
            except MailBoxError as exc:
                raise GmailxError(str(exc).replace("Gmail", "The mail server"), 502) from None
            if raw is None:
                raise GmailxError("Gmail has no such message.", 404)
            m = parse_message(raw)
            item = {"account": account_id, "tag": tag, "id": str(m.uid), "thread": "", "sender": m.sender,
                    "subject": m.subject, "snippet": m.snippet, "date": m.date, "labels": [], "body": m.text}
        else:
            m = self._api(account_id, "GET", f"/messages/{message_id}", params={"format": "full"})
            item = self._item(account_id, tag, m)
            item["body"] = _plain_body(m.get("payload") or {})
        item["body_withheld"] = False
        if cloud:
            if not self.cloud_ai_allowed(account_id):
                item["body"], item["body_withheld"] = "", True
            else:
                item["body"] = self.redact_for_cloud(item["body"])
        return item

    def redact_for_cloud(self, text: str) -> str:
        try:
            from . import redact
        except ImportError:
            return text
        try:
            rd = redact.current() or redact.make(self.config)
            return rd.redact(text) if rd is not None else text
        except Exception:  # noqa: BLE001 - never let redaction failure leak text: drop it instead
            return ""

    # -- sending

    def send_mail(self, account_id: str, to: str, subject: str, body: str, attachments=None) -> dict:
        try:
            views = {v["id"]: v for v in self.list_accounts()}
            v = views.get(str(account_id))
            if v is None:
                raise GmailxError("There is no Gmail account with that id.", 404)
            if not v["connected"]:
                raise GmailxError(f"The {v['label']} account isn't connected.", 409)
            if not v["send_enabled"]:
                raise GmailxError(f"Sending from the {v['label']} account is switched off. Turn on 'Allow sending from this account' "
                                  "in Settings, Mail accounts first.", 403)
            recipients = [a.strip() for a in re.split(r"[;,]", str(to or "")) if a.strip()]
            if not recipients or len(recipients) > 20 or not all(ADDR_RE.match(a) for a in recipients):
                raise GmailxError("The recipient must be one or more plain email addresses.")
            subject = str(subject or "")
            if any(c in subject for c in "\r\n") or len(subject) > 400:
                raise GmailxError("The subject must be one short line.")
            if len(str(body or "")) > 200_000:
                raise GmailxError("That message is too long to send.")
            if v["kind"] == "legacy":
                if attachments:
                    raise GmailxError("Attachments aren't supported on the default (app password) account.")
                self.legacy.send(", ".join(recipients), subject, str(body or ""))
                mid = ""
            elif v["kind"] == "imap":
                from . import mailaccounts
                entry = self._entry(account_id)
                files = []
                total = 0
                for att in attachments or []:
                    name, data, mime = _load_attachment(att)
                    total += len(data)
                    if total > MAX_ATTACH_BYTES:
                        raise GmailxError("The attachments are over 20 MB.")
                    files.append((name, data, mime))
                try:
                    mailaccounts.send_smtp(entry, self._imap_password(account_id), v["address"], recipients, subject,
                                           str(body or ""), attachments=files, smtp_factory=self.smtp_factory)
                except mailaccounts.ImapProblem as exc:
                    raise GmailxError(str(exc), 502) from None
                mid = ""
            else:
                if SCOPE_SEND not in v["scopes"]:
                    raise GmailxError("This account hasn't been given permission to send yet. Press Re-consent in Settings.", 403)
                msg = EmailMessage()
                msg["To"], msg["From"], msg["Subject"] = ", ".join(recipients), v["address"], subject
                msg.set_content(str(body or ""))
                total = 0
                for att in attachments or []:
                    name, data, mime = _load_attachment(att)
                    total += len(data)
                    if total > MAX_ATTACH_BYTES:
                        raise GmailxError("The attachments are over 20 MB.")
                    main, _, sub = mime.partition("/")
                    msg.add_attachment(data, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
                sent = self._api(account_id, "POST", "/messages/send", body={"raw": _b64url(msg.as_bytes())})
                mid = str(sent.get("id") or "") if isinstance(sent, dict) else ""
            self.ctx.audit("gmail_send", account=account_id, label=v["label"], recipients=len(recipients))
            return {"ok": True, "error": "", "message_id": mid}
        except GmailxError as exc:
            return {"ok": False, "error": str(exc), "message_id": ""}
        except Exception as exc:  # noqa: BLE001
            log.info("gmail send failed: %s", type(exc).__name__)
            return {"ok": False, "error": "Sending failed (" + type(exc).__name__ + ").", "message_id": ""}


def _load_attachment(att: Any) -> tuple[str, bytes, str]:
    if isinstance(att, (str, Path)):
        att = {"path": str(att)}
    if not isinstance(att, dict):
        raise GmailxError("An attachment must be a file path or {filename, data}.")
    if att.get("path"):
        p = Path(att["path"])
        if not p.is_file() or p.stat().st_size > MAX_ATTACH_BYTES:
            raise GmailxError("An attachment is missing or over 20 MB.")
        data, name = p.read_bytes(), att.get("filename") or p.name
    else:
        data, name = att.get("data"), att.get("filename") or "attachment"
        if not isinstance(data, (bytes, bytearray)):
            raise GmailxError("An attachment needs its bytes in 'data' or a 'path'.")
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", str(name))[:120] or "attachment"
    return name, bytes(data), att.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _plain_body(payload: dict, depth: int = 0) -> str:
    """The text/plain part of a Gmail payload (HTML is only used when there is nothing else, tags stripped)."""
    def walk(part: dict, depth: int):
        if depth > 8 or not isinstance(part, dict):
            return
        yield part
        for sub in part.get("parts") or []:
            yield from walk(sub, depth + 1)
    plain, html = [], []
    for part in walk(payload, depth):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        try:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        except (ValueError, TypeError):
            continue
        (plain if part.get("mimeType") == "text/plain" else html if part.get("mimeType") == "text/html" else []).append(text)
    if plain:
        return "\n".join(plain)[:50_000]
    return re.sub(r"<[^>]+>", " ", "\n".join(html))[:50_000]


# ---- the interface other modules import ---------------------------------------------------------------------

def get_service(ctx: Ctx, **overrides) -> Gmailx:
    """The one Gmailx for this hub (cached on `ctx`). Tests pass overrides to install fakes."""
    svc = getattr(ctx, "_gmailx", None)
    if svc is None or overrides:
        svc = Gmailx(ctx, **overrides)
        try:
            ctx._gmailx = svc
        except AttributeError:
            pass
    return svc


def list_accounts(ctx) -> list[dict]:
    return get_service(ctx).list_accounts()


def send_mail(ctx, account_id, to, subject, body, attachments=None) -> dict:
    return get_service(ctx).send_mail(account_id, to, subject, body, attachments)


def search(ctx, account_id, query, max_results=20) -> list[dict]:
    return get_service(ctx).search(account_id, query, max_results)


def recent(ctx, account_id, hours=24) -> list[dict]:
    return get_service(ctx).recent(account_id, hours)


def summary(ctx) -> dict:
    return get_service(ctx).summary()


def get_message(ctx, account_id, message_id, destination="local") -> dict:
    return get_service(ctx).get_message(account_id, message_id, destination=destination)


def account_tag(ctx, account_id: str = DEFAULT_ID) -> str:
    return get_service(ctx).tag_of(account_id)


def cloud_ai_allowed(ctx, account_id: str = DEFAULT_ID) -> bool:
    """May this account's mail BODIES go to a cloud model? (Metadata is always allowed.)"""
    return get_service(ctx).cloud_ai_allowed(account_id)


def provider_is_local(name: str) -> bool:
    return str(name or "").lower() in LOCAL_PROVIDERS


# ---- routes ---------------------------------------------------------------------------------------------------

class ConnectBody(BaseModel):
    label: str = "company"
    send_enabled: StrictBool = False
    account_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None


class CompleteBody(BaseModel):
    state: str
    code: str | None = None
    redirect_url: str | None = None


class UpdateBody(BaseModel):
    label: str | None = None
    send_enabled: StrictBool | None = None
    tags: list[str] | None = None
    cloud_ai_allowed: StrictBool | None = None      # null in the JSON = back to the default (see model_fields_set)
    alerts: StrictBool | None = None                # include this account in urgent-alert mail polling
    password: str | None = None                      # IMAP accounts only; never echoed
    imap_host: str | None = None
    imap_port: int | None = None
    imap_security: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_security: str | None = None
    username: str | None = None


class ImapBody(BaseModel):
    label: str
    address: str
    password: str
    preset: str | None = None
    tags: list[str] | None = None
    send_enabled: StrictBool = False
    cloud_ai_allowed: StrictBool | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_security: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_security: str | None = None
    username: str | None = None


class SendBody(BaseModel):
    account: str
    to: str
    subject: str
    body: str
    confirm: StrictBool = False


def make_router(ctx: Ctx, **overrides) -> APIRouter:
    svc = get_service(ctx, **overrides) if overrides else get_service(ctx)
    router = APIRouter(prefix="/api/gmail", dependencies=[Depends(ctx.need_auth)])

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except GmailxError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @router.get("/accounts")
    async def accounts():
        from . import mailaccounts
        return {"accounts": await run(svc.list_accounts), "company_ai_allowed": svc.company_ai_allowed(),
                "client_configured": svc.client_configured(), "presets": mailaccounts.PRESETS}

    @router.post("/accounts/connect")
    async def connect(body: ConnectBody):
        return await run(svc.begin_connect, body.label, send_enabled=body.send_enabled, account_id=body.account_id,
                         client_id=body.client_id, client_secret=body.client_secret)

    @router.post("/accounts/connect/complete")
    async def complete(body: CompleteBody):
        return await run(svc.complete_connect, body.state, body.code, body.redirect_url)

    @router.get("/accounts/connect/{state}")
    async def flow(state: str):
        return await run(svc.flow_status, state)

    @router.post("/accounts/imap")
    async def add_imap(body: ImapBody):
        server = {k: getattr(body, k) for k in ("preset", "imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port",
                                                "smtp_security", "username") if getattr(body, k) is not None}
        return await run(svc.add_imap, body.label, body.address, body.password, tags=body.tags,
                         send_enabled=body.send_enabled, cloud_ai_allowed=body.cloud_ai_allowed, **server)

    @router.put("/accounts/{account_id}")
    async def update(account_id: str, body: UpdateBody):
        server = {k: getattr(body, k) for k in ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port",
                                                "smtp_security", "username") if getattr(body, k) is not None}
        extra: dict = {}
        if body.tags is not None:
            extra["tags"] = body.tags
        if "cloud_ai_allowed" in body.model_fields_set:
            extra["cloud_ai_allowed"] = body.cloud_ai_allowed
        view = await run(svc.update, account_id, body.label, body.send_enabled, imap=server or None,
                         password=body.password, alerts=body.alerts, **extra)
        return view

    @router.delete("/accounts/{account_id}")
    async def delete(account_id: str):
        await run(svc.disconnect, account_id)
        return {"ok": True}

    @router.get("/summary")
    async def summary_route():
        return await run(svc.summary)

    @router.get("/search")
    async def search_route(q: str = Query("", max_length=300), account: str | None = None,
                           limit: int = Query(15, ge=1, le=50)):
        return {"results": await run(svc.search, account, q, limit)}

    @router.post("/send")
    async def send(body: SendBody):
        if not body.confirm:
            raise HTTPException(400, "Sending needs confirm: true.")
        result = await asyncio.to_thread(svc.send_mail, body.account, body.to, body.subject, body.body)
        if not result["ok"]:
            raise HTTPException(403 if "switched off" in result["error"] else 400, result["error"])
        return result

    return router
