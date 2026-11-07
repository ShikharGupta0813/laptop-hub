"""A small Asana REST client (standard library only) for the owner's own tasks.

The personal access token never leaves this module: it is sent only as a Bearer header and
never put in an error message. The transport is injectable so tests need no network.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

API_BASE = "https://app.asana.com/api/1.0"
TOKEN_FILES = (".claude/.asana_token", ".config/task-agent/asana_token")
CUSTOM_FIELDS = ("custom_fields.name,custom_fields.type,custom_fields.display_value,"
                 "custom_fields.enum_value.name,custom_fields.text_value,custom_fields.number_value")
TASK_FIELDS = ("name,due_on,completed,permalink_url,projects.name,memberships.project.name,"
               "assignee.gid,followers.gid," + CUSTOM_FIELDS)
DETAIL_FIELDS = TASK_FIELDS + ",notes,html_notes,modified_at,created_by.name"
STORY_FIELDS = "resource_subtype,text,html_text,created_at,created_by.name,created_by.gid"
FOLLOWED_TTL = 90.0          # seconds the "tasks I follow" search result is reused (rate limits)
MAX_FIELDS = 8
MAX_PAGES = 50
MAX_RETRY_WAIT = 30.0

NOT_SET_UP = ("Asana isn't set up. Set the ASANA_TOKEN environment variable, or save your "
              "personal access token in ~/.claude/.asana_token, then try again.")

# transport(method, url, headers, body) -> (status, headers with lower-case names, body bytes)
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, dict, bytes]"]


class AsanaError(Exception):
    """An Asana problem with a sentence the UI can show. `status` is Asana's HTTP status
    (or 400 for a bad id we refused to send, 0 when Asana could not be reached)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


def find_token(env: dict | None = None, home: Path | None = None) -> str | None:
    """ASANA_TOKEN, else the first existing token file. Whitespace is stripped."""
    env = os.environ if env is None else env
    token = (env.get("ASANA_TOKEN") or "").strip()
    if token:
        return token
    home = Path.home() if home is None else Path(home)
    for rel in TOKEN_FILES:
        path = home / rel
        if path.is_file():
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            return token or None   # the first file that exists wins, even if it is empty
    return None


def check_gid(gid: str) -> str:
    """Asana ids are digits. Anything else must never reach a URL path."""
    if not isinstance(gid, str) or not re.fullmatch(r"[0-9]{1,30}", gid):
        raise AsanaError("That isn't a valid Asana task id.", 400)
    return gid


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # the API does not redirect; never follow one with the token attached


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class AsanaClient:
    def __init__(self, token: str, transport: Transport | None = None, *, retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self._token = token
        self._transport = transport or urllib_transport
        self._retries = retries
        self._sleep = sleep
        self._me: dict | None = None
        self._followed: tuple[float, list[dict]] | None = None
        self._clock = time.monotonic

    # ---- plumbing ------------------------------------------------------------------------

    def _call(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
        url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps({"data": data}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        for attempt in range(self._retries + 1):
            try:
                status, reply_headers, raw = self._transport(method, url, headers, body)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise AsanaError("Can't reach Asana. Check the laptop's internet connection "
                                 f"({type(exc).__name__}).", 0) from None
            if status == 429 and attempt < self._retries:
                try:
                    wait = float(reply_headers.get("retry-after") or 2 ** attempt)
                except ValueError:
                    wait = 2 ** attempt
                self._sleep(max(0.0, min(wait, MAX_RETRY_WAIT)))
                continue
            return self._parse(status, raw)
        raise AsanaError("Asana is busy. Try again in a minute.", 429)   # pragma: no cover

    @staticmethod
    def _parse(status: int, raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if 200 <= status < 300:
            return payload
        detail = "; ".join(str(e.get("message", "")) for e in payload.get("errors") or []
                           if isinstance(e, dict) and e.get("message"))
        if status == 401:
            raise AsanaError("Asana rejected the token. Make a new personal access token and "
                             "save it in ASANA_TOKEN or ~/.claude/.asana_token.", status)
        if status == 403:
            raise AsanaError("Asana says you don't have access to that." + (f" ({detail})" if detail else ""), status)
        if status == 404:
            raise AsanaError("Asana can't find that task. It may be deleted or not shared with you.", status)
        if status == 429:
            raise AsanaError("Asana is busy. Try again in a minute.", status)
        raise AsanaError(f"Asana returned an error ({status})." + (f" {detail}" if detail else ""), status)

    def _get_all(self, path: str, params: dict) -> list[dict]:
        out: list[dict] = []
        query = {**params, "limit": 100}
        for _ in range(MAX_PAGES):
            page = self._call("GET", path, query)
            out.extend(r for r in page.get("data") or [] if isinstance(r, dict))
            offset = (page.get("next_page") or {}).get("offset")
            if not offset:
                return out
            query = {**query, "offset": offset}
        return out

    # ---- reads ---------------------------------------------------------------------------

    def me(self) -> dict:
        """{gid, name, workspace_gid|None}. Cached after the first success."""
        if self._me is None:
            data = self._call("GET", "/users/me", {"opt_fields": "gid,name,workspaces.gid,workspaces.name"}).get("data") or {}
            spaces = data.get("workspaces") or []
            self._me = {"gid": str(data.get("gid", "")), "name": data.get("name") or "",
                        "workspace_gid": str(spaces[0].get("gid")) if spaces and spaces[0].get("gid") else None}
        return dict(self._me)

    def open_tasks(self) -> list[dict]:
        """My incomplete tasks in my first workspace, in Asana's shape reduced to the UI's."""
        workspace = self.me()["workspace_gid"]
        if not workspace:
            raise AsanaError("Your Asana account has no workspace.", 0)
        rows = self._get_all("/tasks", {"assignee": "me", "workspace": workspace,
                                        "completed_since": "now", "opt_fields": TASK_FIELDS})
        return [task_summary(r) for r in rows if not r.get("completed")]

    def followed_tasks(self) -> list[dict]:
        """Open tasks I follow but am not assigned (workspace task search; cached for a short while).
        The search needs a paid Asana plan: where Asana refuses it, this is an empty list."""
        now = self._clock()
        if self._followed and now - self._followed[0] < FOLLOWED_TTL:
            return [dict(t) for t in self._followed[1]]
        me = self.me()
        if not me["workspace_gid"]:
            return []
        try:
            rows = self._get_all(f"/workspaces/{me['workspace_gid']}/tasks/search",
                                 {"followers.any": "me", "completed": "false", "opt_fields": TASK_FIELDS})
        except AsanaError as exc:
            if exc.status in (402, 403, 404):
                rows = []
            else:
                raise
        out = [task_summary(r) for r in rows if not r.get("completed")
               and str((r.get("assignee") or {}).get("gid") or "") != me["gid"]]
        self._followed = (now, out)
        return [dict(t) for t in out]

    def task_detail(self, gid: str) -> dict:
        check_gid(gid)
        raw = self._call("GET", f"/tasks/{gid}", {"opt_fields": DETAIL_FIELDS}).get("data") or {}
        subtasks = self._get_all(f"/tasks/{gid}/subtasks", {"opt_fields": "name,completed"})
        stories = self._get_all(f"/tasks/{gid}/stories",
                                {"opt_fields": STORY_FIELDS})
        task = task_summary(raw)
        task["gid"] = task["gid"] or gid
        task["notes"] = raw.get("notes") or ""
        task["notes_mentions"] = mention_gids(raw.get("html_notes"))
        task["modified_at"] = raw.get("modified_at") or ""
        task["created_by"] = (raw.get("created_by") or {}).get("name") or ""
        me = self.me()
        task["assigned_to_me"] = str((raw.get("assignee") or {}).get("gid") or "") == me["gid"] != ""
        task["followed_by_me"] = any(str((f or {}).get("gid")) == me["gid"] for f in raw.get("followers") or []
                                     if isinstance(f, dict)) and me["gid"] != ""
        task["subtasks"] = [{"gid": str(s.get("gid", "")), "name": (s.get("name") or "").strip(),
                             "completed": bool(s.get("completed"))} for s in subtasks]
        task["comments"] = [{"author": (s.get("created_by") or {}).get("name") or "Someone",
                             "text": s.get("text") or "", "created_at": s.get("created_at") or "",
                             "mentions": mention_gids(s.get("html_text"))}
                            for s in stories if s.get("resource_subtype") == "comment_added"]
        return task

    # ---- writes --------------------------------------------------------------------------

    def comment(self, gid: str, text: str) -> None:
        check_gid(gid)
        self._call("POST", f"/tasks/{gid}/stories", data={"text": text})

    def complete(self, gid: str) -> None:
        check_gid(gid)
        self._call("PUT", f"/tasks/{gid}", data={"completed": True})


def task_summary(raw: dict) -> dict[str, Any]:
    project = None
    for p in raw.get("projects") or []:
        if isinstance(p, dict) and p.get("name"):
            project = p["name"]
            break
    if project is None:
        for m in raw.get("memberships") or []:
            name = ((m or {}).get("project") or {}).get("name")
            if name:
                project = name
                break
    link = raw.get("permalink_url") or ""
    return {"gid": str(raw.get("gid", "")), "name": (raw.get("name") or "").strip() or "(untitled)",
            "due_on": raw.get("due_on") or None, "project": project,
            "completed": bool(raw.get("completed")),
            "permalink_url": link if link.startswith("https://") else "",
            "priority": priority_of(raw), "custom_fields": custom_fields_of(raw)}


_MENTION_GID = re.compile(r'data-asana-gid="([0-9]{1,30})"')
_HIGH = re.compile(r"\b(high|urgent|critical|p0|p1)\b", re.I)
_MEDIUM = re.compile(r"\b(medium|p2)\b", re.I)
_LOW = re.compile(r"\b(low|p3)\b", re.I)


def mention_gids(html: Any) -> list[str]:
    """Asana user/object ids linked in rich text (`<A data-asana-gid="123">`), de-duplicated."""
    if not isinstance(html, str):
        return []
    return list(dict.fromkeys(_MENTION_GID.findall(html[:100000])))[:50]


def priority_level(name: Any) -> int:
    """Asana 'Priority' value name -> 3 high (High, Urgent, Critical, P0, P1), 2 medium (Medium, P2),
    1 low (Low, P3), else 0."""
    text = name if isinstance(name, str) else ""
    return 3 if _HIGH.search(text) else 2 if _MEDIUM.search(text) else 1 if _LOW.search(text) else 0


def _field_value(field: dict) -> str:
    enum = field.get("enum_value")
    if isinstance(enum, dict) and enum.get("name"):
        return str(enum["name"])
    for key in ("display_value", "text_value", "number_value"):
        if field.get(key) not in (None, ""):
            return str(field[key])
    return ""


def custom_fields_of(raw: dict) -> list[dict]:
    """Minimal [{name, value}] for custom fields that have a value (read-only)."""
    out = []
    for f in raw.get("custom_fields") or []:
        if isinstance(f, dict) and str(f.get("name") or "").strip():
            value = _field_value(f)
            if value:
                out.append({"name": str(f["name"]).strip()[:60], "value": value[:100]})
        if len(out) >= MAX_FIELDS:
            break
    return out


def priority_of(raw: dict) -> int:
    for f in raw.get("custom_fields") or []:
        if isinstance(f, dict) and str(f.get("name") or "").strip().lower() == "priority":
            return priority_level(_field_value(f))
    return 0
