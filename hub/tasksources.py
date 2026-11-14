"""TASK SOURCES: a LIST of places tasks and messages come from, each one an adapter the owner can add, edit, switch off or delete.

Every item is {id, kind, label, enabled, settings, ...} kept in `tasksources.json` in the data folder. Its secret (token or
API key) is kept ONLY in the OS keyring (service `redline-sources`, key `task:<id>`), never in that file, never in a log line,
never in a response (a view says `has_secret: true`). Ready-made entry: "asana" (the poller from hub/asana.py, unchanged).

An adapter is one function, `poll(ctx, source) -> [event]`, returning only NEW events (use `fresh(source, events)` to get that
from a full listing: the first poll is a silent baseline so an old backlog does not flood the phone). `source` carries
`.settings`, `.secret`, `.state` (a dict the registry saves between polls) and `.http(method, url, headers, body)`. An event is
the shape hub.alerts.triage already takes: {source, app, sender, title, text, link, kind_hint?, mention?, mail_id, uid}.
Events are handed to `alerts.triage(..., trusted=True)`: switching the source on IS the owner's choice to watch it.

Kinds: asana, jira (Jira Cloud REST v3), trello, linear (GraphQL), github (notifications), rss (an RSS or Atom URL) and
webhook (an inbound `POST /hooks/<id>` with a shared secret: {title, text, sender, url}). rss + webhook are the "any tool,
no code" route. Read-only everywhere: nothing here ever writes to a service.

API DETAILS NOT VERIFIED AGAINST LIVE ACCOUNTS (built from public docs + fake HTTP in tests; the first real poll is the real
test): Jira's search endpoint (Atlassian is moving /rest/api/3/search to /rest/api/3/search/jql; both are tried), Trello's
member notifications filter names, Linear's `assignedIssues` filter shape, GitHub's notification reasons, and the Asana calls
documented in hub/asana.py.

Routes: the list itself is served by hub/sources.py (`/api/sources/task`). This module adds the PUBLIC inbound hook
`POST /hooks/{id}` (header `X-Webhook-Secret: <secret>` or `Authorization: Bearer <secret>`; a wrong secret is refused and
counted, 10 wrong tries a minute lock the hook for a minute).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from .common import Ctx, JsonFile

log = logging.getLogger("hub.tasksources")

SECRET_SERVICE = "redline-sources"
MAX_ITEMS = 40
MIN_POLL, MAX_POLL = 1, 120
LOOP_TICK = 30.0
MAX_SEEN = 500
MAX_BODY = 40_000
MAX_READ = 2_000_000
TIMEOUT = 20.0
LABEL_MAX = 60
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
EMAIL_RE = re.compile(r"^[^@\s<>,;]{1,64}@[^@\s<>,;]{1,255}\.[A-Za-z]{2,}$")
SITE_RE = re.compile(r"^https://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")
URL_RE = re.compile(r"^https?://[^\s<>\"']{3,500}$")
GID_RE = re.compile(r"^[0-9]{1,30}$")

KINDS: dict[str, dict] = {
    "asana": {"label": "Asana", "secret_label": "Personal access token", "poll": True,
              "fields": [{"name": "workspace", "label": "Workspace id (optional)", "pattern": "digits"}]},
    "jira": {"label": "Jira Cloud", "secret_label": "API token", "poll": True,
             "fields": [{"name": "site", "label": "Site, e.g. https://yourcompany.atlassian.net"},
                        {"name": "email", "label": "Your Atlassian account email"}]},
    "trello": {"label": "Trello", "secret_label": "API key and token as KEY:TOKEN", "poll": True, "fields": []},
    "linear": {"label": "Linear", "secret_label": "Personal API key", "poll": True, "fields": []},
    "github": {"label": "GitHub issues and pull requests", "secret_label": "Personal access token (notifications scope)",
               "poll": True, "fields": []},
    "rss": {"label": "RSS or Atom feed", "secret_label": "", "poll": True,
            "fields": [{"name": "url", "label": "Feed address (https://...)"}]},
    "webhook": {"label": "Inbound webhook", "secret_label": "Shared secret (16 or more characters)", "poll": False, "fields": []},
}

# transport(method, url, headers, body: bytes | None) -> (status, bytes)
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]


class SourceProblem(Exception):
    """A sentence the UI can show. Never contains a token. `status` is the HTTP status the routes use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - never follow a redirect with a token attached
        return None


def default_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, response.read(MAX_READ)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_READ)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


# ---- secrets ------------------------------------------------------------------------------------------------

def make_store():
    from .gmailx import KeyringStore
    return KeyringStore(SECRET_SERVICE)


class Source:
    """What an adapter gets: settings, the secret, a dict of state it may keep, and the HTTP function."""

    def __init__(self, item: dict, secret: str, state: dict, transport: Transport):
        self.id, self.kind, self.label = item["id"], item["kind"], item.get("label") or item["kind"]
        self.settings: dict = dict(item.get("settings") or {})
        self.secret, self.state, self._transport = secret, state, transport

    def http(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None) -> tuple[int, bytes]:
        try:
            return self._transport(method, url, dict(headers or {}), body)
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise SourceProblem(f"Can't reach {KINDS.get(self.kind, {}).get('label', 'the service')} ({type(exc).__name__}).", 502) from None

    def get_json(self, url: str, headers: dict | None = None, body: dict | None = None) -> Any:
        h = {"Accept": "application/json", **(headers or {})}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        status, data = self.http("POST" if body is not None else "GET", url, h, raw)
        name = KINDS.get(self.kind, {}).get("label", "The service")
        if status == 401:
            raise SourceProblem(f"{name} rejected the saved token. Edit the source and enter a new one.", 401)
        if status == 403:
            raise SourceProblem(f"{name} says this token has no access to that.", 403)
        if status == 404:
            raise SourceProblem(f"{name} has nothing at that address (404).", 404)
        if status == 429:
            raise SourceProblem(f"{name} is rate-limiting. The next poll will try again.", 429)
        if not 200 <= status < 300:
            raise SourceProblem(f"{name} answered {status}.", status)
        try:
            return json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            raise SourceProblem(f"{name}'s answer could not be read.", 502) from None


def event(source: Source, *, uid: str, sender: str, title: str, text: str = "", link: str = "", **extra) -> dict:
    ev = {"source": source.kind, "app": source.label, "sender": _clean(sender, 120), "title": _clean(title, 200),
          "text": str(text or "")[:1500], "link": _clean(link, 300), "uid": str(uid)[:200],
          "mail_id": f"{source.id}:{str(uid)[:100]}"}
    ev.update(extra)
    return ev


def fresh(source: Source, events: list[dict]) -> list[dict]:
    """The events not seen before. The first call only records them (baseline) and returns []."""
    seen = [str(x) for x in source.state.get("seen") or []]
    uids = [e["uid"] for e in events]
    baseline = not source.state.get("baseline_done")
    new = [] if baseline else [e for e in events if e["uid"] not in seen]
    source.state["seen"] = (seen + [u for u in uids if u not in seen])[-MAX_SEEN:]
    source.state["baseline_done"] = True
    return new


# ---- adapters -----------------------------------------------------------------------------------------------

def poll_jira(ctx: Ctx, source: Source) -> list[dict]:
    """Jira Cloud REST v3: issues assigned to me and updated in the last day. Basic auth: account email + API token."""
    import base64
    site, email = source.settings.get("site", ""), source.settings.get("email", "")
    auth = base64.b64encode(f"{email}:{source.secret}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": "Basic " + auth}
    params = urllib.parse.urlencode({"jql": "assignee = currentUser() AND updated >= -1d ORDER BY updated DESC", "maxResults": "50",
                                     "fields": "summary,description,updated,status,reporter,duedate"})
    try:
        data = source.get_json(f"{site}/rest/api/3/search/jql?{params}", headers)
    except SourceProblem as exc:
        if exc.status not in (404, 410):
            raise
        data = source.get_json(f"{site}/rest/api/3/search?{params}", headers)        # the older endpoint
    out = []
    for issue in (data.get("issues") or []) if isinstance(data, dict) else []:
        if not isinstance(issue, dict) or not re.match(r"^[A-Z][A-Z0-9_]+-\d+$", str(issue.get("key") or "")):
            continue
        f = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        reporter = f.get("reporter") if isinstance(f.get("reporter"), dict) else {}
        status = (f.get("status") or {}).get("name", "") if isinstance(f.get("status"), dict) else ""
        out.append(event(source, uid=f"{issue['key']}:{f.get('updated', '')}", sender=reporter.get("displayName") or "Jira",
                         title=f"{issue['key']} {f.get('summary') or ''}", text=_adf_text(f.get("description")) or f"Status: {status}",
                         link=f"{site}/browse/{issue['key']}", due_on=_clean(f.get("duedate"), 10), kind_hint="task"))
    return fresh(source, out)


def _adf_text(node: Any, depth: int = 0) -> str:
    """Plain text of an Atlassian Document Format value (or a plain string)."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict) or depth > 12:
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    return " ".join(t for t in (_adf_text(c, depth + 1) for c in node.get("content") or []) if t)


def poll_trello(ctx: Ctx, source: Source) -> list[dict]:
    """Trello: my open cards (new ones) and my unread notifications (comments, mentions). Key + token sent as a header."""
    key, _, token = source.secret.partition(":")
    headers = {"Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'}
    base = "https://api.trello.com/1/members/me"
    cards = source.get_json(base + "/cards?" + urllib.parse.urlencode({"filter": "open", "fields": "name,desc,due,url,dateLastActivity"}), headers)
    notes = source.get_json(base + "/notifications?" + urllib.parse.urlencode(
        {"read_filter": "unread", "limit": "30", "filter": "commentCard,mentionedOnCard,addedToCard,changeCard"}), headers)
    out = []
    for c in cards if isinstance(cards, list) else []:
        if isinstance(c, dict) and c.get("id"):
            out.append(event(source, uid="card:" + str(c["id"]), sender="Trello", title=str(c.get("name") or ""), text=str(c.get("desc") or "")[:300],
                             link=str(c.get("url") or ""), due_on=_clean(c.get("due"), 10), kind_hint="task"))
    for n in notes if isinstance(notes, list) else []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        data = n.get("data") if isinstance(n.get("data"), dict) else {}
        card = data.get("card") if isinstance(data.get("card"), dict) else {}
        who = n.get("memberCreator") if isinstance(n.get("memberCreator"), dict) else {}
        kind = str(n.get("type") or "")
        out.append(event(source, uid="note:" + str(n["id"]), sender=who.get("fullName") or who.get("username") or "Trello",
                         title=str(card.get("name") or "Trello card"), text=str(data.get("text") or kind),
                         link=f"https://trello.com/c/{card['shortLink']}" if card.get("shortLink") else "",
                         mention=kind == "mentionedOnCard"))
    return fresh(source, out)


LINEAR_QUERY = ("{ viewer { assignedIssues(first: 50, orderBy: updatedAt) { nodes { id identifier title description url updatedAt "
                "dueDate state { type } creator { name } } } } }")


def poll_linear(ctx: Ctx, source: Source) -> list[dict]:
    """Linear GraphQL: viewer.assignedIssues. The personal API key goes in Authorization as is (no 'Bearer')."""
    data = source.get_json("https://api.linear.app/graphql", {"Authorization": source.secret}, {"query": LINEAR_QUERY})
    if isinstance(data, dict) and data.get("errors"):
        raise SourceProblem("Linear refused the query. Check the API key.", 400)
    nodes = (((data or {}).get("data") or {}).get("viewer") or {}).get("assignedIssues", {}).get("nodes") or []
    out = []
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        if isinstance(n.get("state"), dict) and n["state"].get("type") in ("completed", "canceled"):
            continue
        creator = n.get("creator") if isinstance(n.get("creator"), dict) else {}
        out.append(event(source, uid=f"{n['id']}:{n.get('updatedAt', '')}", sender=creator.get("name") or "Linear",
                         title=f"{n.get('identifier', '')} {n.get('title') or ''}", text=str(n.get("description") or "")[:300],
                         link=str(n.get("url") or ""), due_on=_clean(n.get("dueDate"), 10), kind_hint="task"))
    return fresh(source, out)


def github_web_url(api_url: str, repo: str) -> str:
    m = re.match(r"^https://api\.github\.com/repos/([^/]+/[^/]+)/(issues|pulls)/(\d+)$", str(api_url or ""))
    if m:
        return f"https://github.com/{m.group(1)}/{'pull' if m.group(2) == 'pulls' else 'issues'}/{m.group(3)}"
    return f"https://github.com/{repo}" if re.match(r"^[\w.-]+/[\w.-]+$", repo or "") else ""


def poll_github(ctx: Ctx, source: Source) -> list[dict]:
    """GitHub notifications where I take part (mentions, assignments, review requests, replies)."""
    headers = {"Authorization": "Bearer " + source.secret, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "redline-hub"}
    data = source.get_json("https://api.github.com/notifications?participating=true&per_page=50", headers)
    out = []
    for n in data if isinstance(data, list) else []:
        if not isinstance(n, dict) or not n.get("id"):
            continue
        subject = n.get("subject") if isinstance(n.get("subject"), dict) else {}
        repo = str((n.get("repository") or {}).get("full_name") or "") if isinstance(n.get("repository"), dict) else ""
        reason = str(n.get("reason") or "")
        out.append(event(source, uid=f"{n['id']}:{n.get('updated_at', '')}", sender=repo or "GitHub", title=str(subject.get("title") or ""),
                         text=f"{reason.replace('_', ' ')} ({subject.get('type', '')})", link=github_web_url(subject.get("url"), repo),
                         mention=reason in ("mention", "team_mention"),
                         kind_hint="task" if reason in ("assign", "review_requested") else ""))
    return fresh(source, out)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw: bytes) -> list[dict]:
    """Entries of an RSS or Atom document: [{id, title, text, link, author}]. A document with a DOCTYPE or entities is refused."""
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw[:20000].lower():
        raise SourceProblem("That feed uses features that are not allowed (DTD or entities).")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SourceProblem("That address does not return an RSS or Atom feed.") from None
    entries = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        row = {"id": "", "title": "", "text": "", "link": "", "author": ""}
        for ch in node:
            name, text = _local(ch.tag), (ch.text or "").strip()
            if name in ("guid", "id"):
                row["id"] = text
            elif name == "title":
                row["title"] = text
            elif name in ("description", "summary", "content") and not row["text"]:
                row["text"] = re.sub(r"<[^>]+>", " ", text)
            elif name == "link":
                row["link"] = ch.get("href") or text or row["link"]
            elif name in ("author", "creator"):
                row["author"] = text or "".join((a.text or "") for a in ch if _local(a.tag) == "name").strip()
        entries.append(row)
    return entries[:50]


def poll_rss(ctx: Ctx, source: Source) -> list[dict]:
    url = source.settings.get("url", "")
    status, raw = source.http("GET", url, {"Accept": "application/rss+xml, application/atom+xml, text/xml", "User-Agent": "redline-hub"})
    if not 200 <= status < 300:
        raise SourceProblem(f"The feed answered {status}.", status)
    out = []
    for e in parse_feed(raw):
        uid = e["id"] or e["link"] or e["title"]
        if uid:
            out.append(event(source, uid=uid, sender=e["author"] or source.label, title=e["title"], text=e["text"], link=e["link"]))
    return fresh(source, out)


def poll_asana(ctx: Ctx, source: Source) -> list[dict]:
    """Asana through the existing poller (hub/asana.py), with this entry's own token, workspace and baseline."""
    from . import asana

    class Mem:
        def load(self, default):
            return source.state.get("asana", default)

        def save(self, data):
            source.state["asana"] = data

    class Tok:
        def get(self):
            return source.secret

    def http(method: str, url: str, headers: dict) -> tuple[int, dict]:
        status, raw = source.http(method, url, headers)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return status, data if isinstance(data, dict) else {}

    poller = asana.Poller(ctx, http=http, store=Tok(), state_file=Mem(), workspace=str(source.settings.get("workspace") or ""),
                          use_work_token=False)
    try:
        events, _baseline = poller.fetch_events()
    except asana.AsanaProblem as exc:
        raise SourceProblem(str(exc), exc.status or 400) from None
    out = []
    for ev in events:
        ev = dict(ev)
        ev["uid"] = f"{ev.get('gid')}:{ev.get('text', '')[:40]}"
        ev["mail_id"] = f"{source.id}:{ev['uid']}"[:120]
        out.append(ev)
    return out


ADAPTERS: dict[str, Callable[[Ctx, Source], list[dict]]] = {
    "asana": poll_asana, "jira": poll_jira, "trello": poll_trello, "linear": poll_linear, "github": poll_github, "rss": poll_rss,
}


# ---- validation ---------------------------------------------------------------------------------------------

def clean_settings(kind: str, raw: Any, base: dict | None = None) -> dict:
    out = dict(base or {})
    raw = raw if isinstance(raw, dict) else {}
    if kind == "jira":
        for k in ("site", "email"):
            if k in raw:
                out[k] = str(raw[k] or "").strip().rstrip("/")
        if "site" in raw and not SITE_RE.match(out.get("site", "")):
            raise SourceProblem("The Jira site is an https address like https://yourcompany.atlassian.net.")
        if "email" in raw and not EMAIL_RE.match(out.get("email", "")):
            raise SourceProblem("Give the email address of your Atlassian account.")
    elif kind == "rss":
        if "url" in raw:
            out["url"] = str(raw["url"] or "").strip()
            if not URL_RE.match(out["url"]):
                raise SourceProblem("The feed address must start with https:// (or http://).")
    elif kind == "asana":
        if "workspace" in raw:
            out["workspace"] = str(raw["workspace"] or "").strip()
            if out["workspace"] and not GID_RE.match(out["workspace"]):
                raise SourceProblem("The workspace id is a number.")
    if "poll_minutes" in raw:
        m = raw["poll_minutes"]
        if not isinstance(m, int) or isinstance(m, bool) or not MIN_POLL <= m <= MAX_POLL:
            raise SourceProblem(f"Poll every {MIN_POLL} to {MAX_POLL} minutes.")
        out["poll_minutes"] = m
    if kind == "jira" and not (out.get("site") and out.get("email")):
        raise SourceProblem("Jira needs the site and your Atlassian email.")
    if kind == "rss" and not out.get("url"):
        raise SourceProblem("Give the feed address.")
    return out


def clean_secret(kind: str, secret: Any) -> str:
    s = str(secret or "").strip()
    if not 8 <= len(s) <= 400 or any(c.isspace() or ord(c) < 32 for c in s):
        raise SourceProblem("That token or key does not look right (8 to 400 characters, no spaces).")
    if kind == "trello" and not re.match(r"^[A-Za-z0-9]{8,64}:[A-Za-z0-9_-]{8,200}$", s):
        raise SourceProblem("Trello needs the API key and the token joined by a colon: KEY:TOKEN.")
    if kind == "webhook" and len(s) < 16:
        raise SourceProblem("A webhook secret needs at least 16 characters.")
    return s


# ---- the registry -------------------------------------------------------------------------------------------

class TaskSources:
    def __init__(self, ctx: Ctx, *, store: Any = None, transport: Transport | None = None, triage: Callable | None = None,
                 clock: Callable[[], float] = time.time):
        self.ctx = ctx
        self.store = store or make_store()
        self.transport = transport or default_transport
        self._triage = triage
        self.clock = clock
        self.file = JsonFile(ctx.config.data_path() / "tasksources.json")
        self.state_file = JsonFile(ctx.config.data_path() / "tasksources_state.json")
        self.failures: dict[str, list[float]] = {}

    # -- list
    def _load(self) -> list[dict]:
        data = self.file.load({})
        items = data.get("items") if isinstance(data, dict) else None
        items = [i for i in items if isinstance(i, dict) and i.get("kind") in KINDS] if isinstance(items, list) else []
        if not any(i.get("id") == "asana" for i in items):            # the ready-made entry (the existing Asana poller)
            items.insert(0, {"id": "asana", "kind": "asana", "label": "Asana", "enabled": False, "settings": {}, "builtin": True})
        return items

    def _save(self, items: list[dict]) -> None:
        self.file.save({"items": items})

    def _states(self) -> dict:
        d = self.state_file.load({})
        return d if isinstance(d, dict) else {}

    def _secret(self, item_id: str) -> str:
        try:
            return (self.store.get("task:" + item_id) or "").strip()
        except Exception:  # noqa: BLE001 - keyring locked or missing
            return ""

    def _poller(self):
        poller = getattr(self.ctx, "asana", None)
        if poller is None:
            from . import asana
            poller = asana.Poller(self.ctx)
        return poller

    def view(self, item: dict) -> dict:
        iid, kind = item["id"], item["kind"]
        builtin = item.get("builtin") is True and iid == "asana"
        st = self._states().get(iid) or {}
        out = {"id": iid, "kind": kind, "label": item.get("label") or KINDS[kind]["label"], "enabled": bool(item.get("enabled")),
               "settings": dict(item.get("settings") or {}), "builtin": item.get("builtin") is True,
               "kind_label": KINDS[kind]["label"], "has_secret": bool(self._secret(iid)),
               "secret_ref": f"keyring:{SECRET_SERVICE}/task:{iid}", "last_poll": st.get("last_poll") or None,
               "last_error": st.get("last_error") or None}
        if kind == "webhook":
            out["hook_path"] = f"/hooks/{iid}"
        if builtin:
            p = self._poller()
            out["enabled"] = p.enabled
            out["has_secret"] = bool(p.token())
            out["secret_ref"] = "keyring:redline-asana/pat"
            out["last_error"] = p.last_error or None
            out["last_poll"] = p.last_poll or None
            out["settings"] = {"workspace": str(getattr(self.ctx.config, "asana_workspace", "") or ""),
                               "poll_minutes": p.minutes()}
        return out

    def list_items(self) -> list[dict]:
        return [self.view(i) for i in self._load()]

    def _find(self, items: list[dict], item_id: str) -> dict:
        for i in items:
            if i["id"] == item_id:
                return i
        raise SourceProblem("There is no task source with that id.", 404)

    # -- add / edit / delete
    def add(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise SourceProblem("Send the source as a JSON object.")
        kind = str(body.get("kind") or "")
        if kind not in KINDS:
            raise SourceProblem("Kind is one of: " + ", ".join(KINDS) + ".")
        label = _clean(body.get("label") or KINDS[kind]["label"], LABEL_MAX)
        if not label:
            raise SourceProblem("Give the source a name.")
        settings = clean_settings(kind, body.get("settings"))
        secret = ""
        if KINDS[kind]["secret_label"] and (kind == "webhook" or body.get("secret")):
            secret = clean_secret(kind, body.get("secret"))
        elif KINDS[kind]["secret_label"]:
            raise SourceProblem(f"{KINDS[kind]['label']} needs a {KINDS[kind]['secret_label'].lower()}.")
        items = self._load()
        if len(items) >= MAX_ITEMS:
            raise SourceProblem(f"At most {MAX_ITEMS} task sources.")
        base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or kind
        iid, n = (base if ID_RE.match(base) else kind), 1
        while any(i["id"] == iid for i in items):
            n += 1
            iid = f"{base}-{n}"[:40]
        item = {"id": iid, "kind": kind, "label": label, "enabled": body.get("enabled") is not False, "settings": settings}
        if secret:
            try:
                self.store.set("task:" + iid, secret)
            except Exception:  # noqa: BLE001
                raise SourceProblem("Secure storage (the OS keyring) is not available, so the secret was not saved.", 503) from None
        items.append(item)
        self._save(items)
        self.ctx.audit("tasksource_add", id=iid, kind=kind)
        return self.view(item)

    def update(self, item_id: str, body: Any) -> dict:
        if not isinstance(body, dict):
            raise SourceProblem("Send the changes as a JSON object.")
        items = self._load()
        item = self._find(items, item_id)
        kind = item["kind"]
        builtin = item.get("builtin") is True and item_id == "asana"
        if "label" in body:
            label = _clean(body["label"], LABEL_MAX)
            if not label:
                raise SourceProblem("Give the source a name.")
            item["label"] = label
        if "settings" in body:
            item["settings"] = clean_settings(kind, body["settings"], item.get("settings"))
        if body.get("secret"):
            secret = clean_secret(kind, body["secret"])
            if builtin:
                self._poller().connect(secret, str((item.get("settings") or {}).get("workspace") or ""))
            else:
                try:
                    self.store.set("task:" + item_id, secret)
                except Exception:  # noqa: BLE001
                    raise SourceProblem("Secure storage (the OS keyring) is not available.", 503) from None
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise SourceProblem("enabled is true or false.")
            item["enabled"] = body["enabled"]
            if builtin:
                minutes = (body.get("settings") or {}).get("poll_minutes") if isinstance(body.get("settings"), dict) else None
                self._poller().set_enabled(body["enabled"], minutes)
        if builtin and isinstance(body.get("settings"), dict) and "enabled" not in body:
            p = self._poller()
            if "workspace" in body["settings"] and body["settings"]["workspace"]:
                p.set_workspace(str(body["settings"]["workspace"]))
            if "poll_minutes" in body["settings"]:
                p.set_enabled(p.enabled, body["settings"]["poll_minutes"])
        self._save(items)
        self.ctx.audit("tasksource_update", id=item_id, kind=kind)
        return self.view(item)

    def delete(self, item_id: str) -> None:
        items = self._load()
        item = self._find(items, item_id)
        if item.get("builtin") is True and item_id == "asana":
            self._poller().disconnect()                  # the ready-made entry cannot vanish: it is signed out and off
            item["enabled"] = False
            self._save(items)
            return
        self.store.delete("task:" + item_id)
        self._save([i for i in items if i["id"] != item_id])
        states = self._states()
        if states.pop(item_id, None) is not None:
            self.state_file.save(states)
        self.ctx.audit("tasksource_delete", id=item_id)

    # -- polling
    def _source(self, item: dict, state: dict) -> Source:
        return Source(item, self._secret(item["id"]), state, self.transport)

    def test(self, item_id: str) -> dict:
        """One harmless read with a throw-away state: nothing is triaged, nothing is remembered."""
        item = self._find(self._load(), item_id)
        kind = item["kind"]
        if kind == "webhook":
            return {"ok": True, "message": f"Send a POST to /hooks/{item_id} with the header X-Webhook-Secret.",
                    "has_secret": bool(self._secret(item_id))}
        if item.get("builtin") is True and item_id == "asana":
            return {"ok": True, **self._poller().test()}
        if not self._secret(item_id) and KINDS[kind]["secret_label"]:
            raise SourceProblem("No token is saved for this source. Edit it and enter one.", 409)
        scratch: dict = {"baseline_done": True, "seen": []}
        events = ADAPTERS[kind](self.ctx, self._source(item, scratch))
        return {"ok": True, "found": len(events) if kind != "asana" else None,
                "message": "Connected." if kind == "asana" else f"Connected. {len(events)} item(s) in the list right now."}

    def poll_item(self, item_id: str) -> dict:
        items = self._load()
        item = self._find(items, item_id)
        kind = item["kind"]
        if kind == "webhook":
            raise SourceProblem("A webhook is pushed to the hub; it is never polled.", 409)
        if item.get("builtin") is True and item_id == "asana":
            return self._poller().poll_once()
        states = self._states()
        state = states.get(item_id) if isinstance(states.get(item_id), dict) else {}
        raised = 0
        try:
            events = ADAPTERS[kind](self.ctx, self._source(item, state))
            state["last_error"] = ""
        except SourceProblem as exc:
            state["last_error"] = str(exc)
            states[item_id] = state
            self.state_file.save(states)
            raise
        state["last_poll"] = dt.datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")
        states[item_id] = state
        self.state_file.save(states)
        for ev in events:
            try:
                raised += 1 if self.triage(ev) else 0
            except Exception:  # noqa: BLE001 - one bad event must not lose the rest
                log.info("task source event could not be triaged")
        return {"events": len(events), "alerts": raised}

    def triage(self, ev: dict):
        if self._triage is not None:
            return self._triage(self.ctx, ev)
        from . import alerts
        return alerts.triage(self.ctx, ev, trusted=True)

    def poll_due(self) -> int:
        """Poll every enabled, non-webhook, non-builtin item whose interval has passed. Never raises."""
        n = 0
        states = self._states()
        for item in self._load():
            if item.get("builtin") is True or not item.get("enabled") or item["kind"] == "webhook":
                continue
            minutes = (item.get("settings") or {}).get("poll_minutes") or 5
            last = (states.get(item["id"]) or {}).get("last_poll") or ""
            try:
                due = not last or self.clock() - dt.datetime.fromisoformat(last).timestamp() >= minutes * 60
            except ValueError:
                due = True
            if not due:                                            # an error is retried at the normal interval, not every tick
                continue
            try:
                self.poll_item(item["id"])
                n += 1
            except SourceProblem as exc:
                log.info("task source poll problem (%s): %s", item["kind"], exc.status or "error")
            except Exception:  # noqa: BLE001
                log.info("task source poll failed (%s)", item["kind"])
        return n

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(LOOP_TICK)
            await asyncio.to_thread(self.poll_due)

    # -- inbound webhook
    def receive(self, item_id: str, provided: str, payload: Any) -> dict | None:
        """The public hook. Raises SourceProblem(401/413/429) without saying which part was wrong."""
        now = self.clock()
        recent = [t for t in self.failures.get(item_id, []) if now - t < 60]
        self.failures[item_id] = recent
        if len(recent) >= 10:
            raise SourceProblem("Too many wrong tries. Wait a minute.", 429)
        item = next((i for i in self._load() if i["id"] == item_id and i["kind"] == "webhook"), None)
        secret = self._secret(item_id) if item else ""
        ok = bool(item and item.get("enabled") and secret and provided and hmac.compare_digest(secret.encode(), provided.encode()))
        if not ok:
            recent.append(now)
            raise SourceProblem("Not accepted.", 401)
        if not isinstance(payload, dict):
            raise SourceProblem("Send {\"title\", \"text\", \"sender\", \"url\"} as JSON.")
        title, text = _clean(payload.get("title"), 200), str(payload.get("text") or payload.get("message") or "")[:1500]
        if not (title or text.strip()):
            raise SourceProblem("The message has no title or text.")
        link = _clean(payload.get("url") or payload.get("link"), 300)
        ev = {"source": "webhook", "app": item.get("label") or "Webhook", "sender": _clean(payload.get("sender"), 120) or item.get("label") or "Webhook",
              "title": title, "text": text, "link": link if URL_RE.match(link or "") else "",
              "mention": payload.get("mention") is True}
        return self.triage(ev)


def get(ctx: Ctx, **overrides) -> TaskSources:
    reg = getattr(ctx, "tasksources", None)
    if reg is None or overrides:
        reg = TaskSources(ctx, **overrides)
        ctx.tasksources = reg                      # type: ignore[attr-defined]
    return reg


# ---- the kind interface used by hub/sources.py -------------------------------------------------------------

def kind_list(ctx: Ctx) -> dict:
    return {"items": get(ctx).list_items(), "kinds": KINDS}


def kind_add(ctx: Ctx, body: Any) -> dict:
    return get(ctx).add(body)


def kind_update(ctx: Ctx, item_id: str, body: Any) -> dict:
    return get(ctx).update(item_id, body)


def kind_delete(ctx: Ctx, item_id: str) -> None:
    get(ctx).delete(item_id)


def kind_test(ctx: Ctx, item_id: str) -> dict:
    return get(ctx).test(item_id)


# ---- routes (only the public hook; the list is under /api/sources) ----------------------------------------------

def make_router(ctx: Ctx, **overrides) -> APIRouter:
    reg = get(ctx, **overrides) if overrides else get(ctx)
    router = APIRouter()

    @router.post("/hooks/{item_id}")
    async def hook(item_id: str, request: Request):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            raise HTTPException(413, "That message is too large.")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "That message is too large.")
        auth = request.headers.get("authorization") or ""
        provided = request.headers.get("x-webhook-secret") or (auth[7:] if auth.lower().startswith("bearer ") else "")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        try:
            alert = await asyncio.to_thread(reg.receive, item_id[:40], provided.strip(), payload)
        except SourceProblem as exc:
            raise HTTPException(exc.status, str(exc)) from None
        return {"accepted": True, "alert_id": alert.get("id") if isinstance(alert, dict) else None}

    return router


async def start(ctx: Ctx):
    reg = getattr(ctx, "tasksources", None)
    if reg is None:
        return None
    task = asyncio.get_running_loop().create_task(reg.loop())

    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return stop
