"""SEARCH PROVIDERS: the owner's editable, ordered list that Research searches with. See docs/api/searchprov.md.

Kinds (each entry has a label, an on/off switch, its place in the list, settings and, where it needs one, a key in the
OS keyring):
- `ddg-html`   DuckDuckGo's HTML endpoint (no key). Ready-made entry 1.
- `wikipedia`  Wikipedia's search API (no key, `lang` setting). Ready-made entry 2.
- `searxng`    the owner's own SearXNG instance (`base_url`), its JSON API (`format=json` must be enabled there).
- `brave`      the official Brave Search API (key in the keyring).
- `json-api`   ANY search API: a URL template with {query} and {limit}, where the key goes (header, bearer, query
               string, or a JSON body field), and which JSON fields hold the results, titles, links and snippets.
               Presets for Bing and Google Custom Search are included; Tavily/Serper-style POST bodies work too.
- `site`       restrict to some places: `stackexchange` (the public Stack Exchange API, e.g. Stack Overflow) or
               `domains` (DuckDuckGo limited to the given domains, e.g. a docs site).

The first ENABLED provider that returns results wins; a provider that errors or finds nothing falls through to the
next, and a note says which one answered ("Searched with X") and which were skipped and why. A provider that failed is
skipped with a growing backoff (1 min, doubling, 1 h max) and the screen shows its last error.
Safety: public hosts go through the SSRF guard of hub/research.py (the owner's own SearXNG / a `json-api` entry may be
marked `allow_private`); redirects are never followed; keys live only in the keyring and are never logged or returned;
every result is UNTRUSTED text (cleaned, capped, http(s) links only) and is later fetched through the page guard.
"""
from __future__ import annotations

import asyncio
import contextvars
import html
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from . import provlist
from .common import Ctx
from .provlist import Backoff, ListError, clean

log = logging.getLogger("hub.searchprov")

KINDS = ("ddg-html", "wikipedia", "searxng", "brave", "json-api", "site")
TIMEOUT = httpx.Timeout(8.0, read=15.0)
USER_AGENT = "Mozilla/5.0 (compatible; REDLINE-research/1.0; personal assistant)"
MAX_RESULTS = 20
KEY_PLACES = ("none", "header", "bearer", "query", "body")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
SITE_RE = re.compile(r"^[a-z0-9.]{2,40}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,8})?$")
PATH_RE = re.compile(r"^[A-Za-z0-9_.\-]{0,80}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,60}$")
FIELD_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class SearchError(Exception):
    """A provider failed; the text is a plain reason with no address and no key in it."""


# ---- settings per kind ------------------------------------------------------------------------------------------

def _domains(value: Any) -> list[str]:
    items = re.split(r"[,\s]+", value) if isinstance(value, str) else value if isinstance(value, list) else []
    out = []
    for item in items:
        d = clean(str(item), 80).lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
        if d and d not in out:
            if not DOMAIN_RE.match(d):
                raise ListError(f"'{clean(str(item), 40)}' is not a domain like docs.python.org.")
            out.append(d)
    return out[:10]


def _path(value: Any, what: str) -> str:
    text = clean(value, 80)
    if not PATH_RE.match(text):
        raise ListError(f"{what} may only hold letters, digits, dots, dashes and underscores.")
    return text


def check_settings(kind: str, raw: dict) -> dict:
    if kind in ("ddg-html",):
        return {}
    if kind == "wikipedia":
        lang = clean(raw.get("lang"), 12).lower() or "en"
        if not LANG_RE.match(lang):
            raise ListError("The Wikipedia language must be a code like en or de.")
        return {"lang": lang}
    if kind == "searxng":
        return {"base_url": provlist.check_url(raw.get("base_url"), "SearXNG address").rstrip("/"),
                "allow_private": raw.get("allow_private", True) is not False}
    if kind == "brave":
        return {}
    if kind == "site":
        mode = raw.get("mode") if raw.get("mode") in ("stackexchange", "domains") else "stackexchange"
        if mode == "stackexchange":
            site = clean(raw.get("site"), 40).lower() or "stackoverflow"
            if not SITE_RE.match(site):
                raise ListError("The Stack Exchange site must be a name like stackoverflow or superuser.")
            return {"mode": mode, "site": site}
        domains = _domains(raw.get("domains"))
        if not domains:
            raise ListError("Give at least one domain, for example docs.python.org.")
        return {"mode": mode, "domains": domains}
    if kind == "json-api":
        url = provlist.check_url(raw.get("url_template"), "URL template")
        method = "POST" if str(raw.get("method") or "GET").upper() == "POST" else "GET"
        body_template = raw.get("body_template")
        if method == "POST":
            try:
                parsed = json.loads(body_template) if isinstance(body_template, str) else body_template
            except ValueError:
                raise ListError("The body template must be valid JSON, for example {\"q\": \"{query}\"}.") from None
            if not isinstance(parsed, dict):
                raise ListError("The body template must be a JSON object.")
            body_template = json.dumps(parsed)[:2000]
        elif "{query}" not in url:
            raise ListError("The URL template must contain {query} where the search words go.")
        key_in = raw.get("key_in") if raw.get("key_in") in KEY_PLACES else "none"
        key_name = clean(raw.get("key_name"), 60)
        if key_in == "header" and not HEADER_RE.match(key_name or "X-API-Key"):
            raise ListError("The header name may only hold letters, digits and dashes.")
        if key_in in ("query", "body") and not FIELD_RE.match(key_name or "key"):
            raise ListError("The key field name may only hold letters, digits, dashes and underscores.")
        return {"url_template": url, "method": method, "body_template": body_template if method == "POST" else "",
                "key_in": key_in, "key_name": key_name or {"header": "X-API-Key", "query": "key", "body": "api_key"}.get(key_in, ""),
                "results_path": _path(raw.get("results_path"), "The results path"),
                "title_field": _path(raw.get("title_field"), "The title field") or "title",
                "url_field": _path(raw.get("url_field"), "The link field") or "url",
                "snippet_field": _path(raw.get("snippet_field"), "The snippet field") or "snippet",
                "allow_private": raw.get("allow_private") is True}
    raise ListError("Unknown kind.")


def seed_for(config) -> list[dict]:
    legacy = str(getattr(config, "research_search_backend", "auto") or "auto")
    return [{"id": "duckduckgo", "kind": "ddg-html", "label": "DuckDuckGo", "enabled": legacy != "wikipedia", "settings": {}},
            {"id": "wikipedia", "kind": "wikipedia", "label": "Wikipedia", "enabled": legacy != "ddg", "settings": {"lang": "en"}}]


def list_for(config, secrets: Any = None) -> provlist.ProviderList:
    return provlist.ProviderList("search", config.data_path() / "search_providers.json", KINDS,
                                 lambda: seed_for(config), check_settings, secrets)


PRESETS = [
    {"label": "SearXNG (my instance)", "kind": "searxng", "settings": {"base_url": "http://127.0.0.1:8888", "allow_private": True}},
    {"label": "Brave Search", "kind": "brave", "settings": {}},
    {"label": "Bing Web Search API", "kind": "json-api", "settings": {
        "url_template": "https://api.bing.microsoft.com/v7.0/search?q={query}&count={limit}", "key_in": "header",
        "key_name": "Ocp-Apim-Subscription-Key", "results_path": "webPages.value", "title_field": "name",
        "url_field": "url", "snippet_field": "snippet"}},
    {"label": "Google Custom Search", "kind": "json-api", "settings": {
        "url_template": "https://www.googleapis.com/customsearch/v1?q={query}&num={limit}&cx=YOUR_ENGINE_ID", "key_in": "query",
        "key_name": "key", "results_path": "items", "title_field": "title", "url_field": "link", "snippet_field": "snippet"}},
    {"label": "Stack Overflow", "kind": "site", "settings": {"mode": "stackexchange", "site": "stackoverflow"}},
    {"label": "Python docs", "kind": "site", "settings": {"mode": "domains", "domains": ["docs.python.org"]}},
]

FIELDS = {
    "ddg-html": [],
    "wikipedia": [{"key": "lang", "label": "Language code", "type": "text", "placeholder": "en"}],
    "searxng": [{"key": "base_url", "label": "Instance address", "type": "url", "placeholder": "http://127.0.0.1:8888", "required": True},
                {"key": "allow_private", "label": "It is on my own network", "type": "bool"}],
    "brave": [],
    "site": [{"key": "mode", "label": "Mode", "type": "select", "options": ["stackexchange", "domains"]},
             {"key": "site", "label": "Stack Exchange site", "type": "text", "placeholder": "stackoverflow"},
             {"key": "domains", "label": "Domains (comma separated)", "type": "list", "placeholder": "docs.python.org, developer.mozilla.org"}],
    "json-api": [{"key": "url_template", "label": "URL template", "type": "url", "required": True,
                  "placeholder": "https://api.example.com/search?q={query}&n={limit}"},
                 {"key": "method", "label": "Method", "type": "select", "options": ["GET", "POST"]},
                 {"key": "body_template", "label": "POST body (JSON, with {query})", "type": "text"},
                 {"key": "key_in", "label": "Where the key goes", "type": "select", "options": list(KEY_PLACES)},
                 {"key": "key_name", "label": "Header or field name", "type": "text", "placeholder": "X-API-Key"},
                 {"key": "results_path", "label": "Results list at", "type": "text", "placeholder": "web.results"},
                 {"key": "title_field", "label": "Title field", "type": "text", "placeholder": "title"},
                 {"key": "url_field", "label": "Link field", "type": "text", "placeholder": "url"},
                 {"key": "snippet_field", "label": "Snippet field", "type": "text", "placeholder": "snippet"},
                 {"key": "allow_private", "label": "Allow a private address", "type": "bool"}],
}
KIND_INFO = {
    "ddg-html": {"label": "DuckDuckGo (no key)", "secret": ""},
    "wikipedia": {"label": "Wikipedia (no key)", "secret": ""},
    "searxng": {"label": "SearXNG instance", "secret": ""},
    "brave": {"label": "Brave Search API", "secret": "Brave API key"},
    "json-api": {"label": "Any JSON search API", "secret": "API key (if it needs one)"},
    "site": {"label": "Only some places (Stack Overflow, a docs site)", "secret": ""},
}


# ---- helpers ----------------------------------------------------------------------------------------------------

def get_path(data: Any, path: str) -> Any:
    """'web.results.0.title' -> the value, or None. Never raises."""
    cur = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def tidy(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return clean(str(value), limit) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
    return clean(html.unescape(re.sub(r"<[^>]+>", "", value)), limit)


def good_link(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return url if parts.scheme in ("http", "https") and parts.hostname and not (parts.username or parts.password) else ""


def map_rows(data: Any, settings: dict, limit: int) -> list[dict]:
    """The JSON-API mapping: results list, then title/link/snippet field per item."""
    items = get_path(data, settings.get("results_path", "")) if settings.get("results_path") else data
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = good_link(get_path(item, settings.get("url_field", "url")))
        if not url:
            continue
        rows.append({"title": tidy(get_path(item, settings.get("title_field", "title")), 160) or urlsplit(url).hostname or url,
                     "url": url, "snippet": tidy(get_path(item, settings.get("snippet_field", "snippet")), 300)})
        if len(rows) >= min(limit, MAX_RESULTS):
            break
    return rows


def fill_json(node: Any, query: str, limit: int) -> Any:
    if isinstance(node, str):
        if node == "{limit}":
            return limit
        return node.replace("{query}", query).replace("{limit}", str(limit))
    if isinstance(node, list):
        return [fill_json(n, query, limit) for n in node]
    if isinstance(node, dict):
        return {k: fill_json(v, query, limit) for k, v in node.items()}
    return node


async def _guard(url: str, allow_private: bool) -> None:
    from .research import BlockedURL, check_url
    try:
        if allow_private:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
                raise BlockedURL("not a plain http(s) address")
        else:
            await check_url(url)
    except BlockedURL as exc:
        raise SearchError(f"address not allowed ({exc})") from None


def _status_problem(status: int) -> SearchError:
    if status in (401, 403):
        return SearchError(f"refused ({status}): check the key, or that this API allows JSON")
    if status == 429:
        return SearchError("rate limit reached (429)")
    return SearchError(f"answered {status}")


async def _request(method: str, url: str, *, allow_private: bool, headers: dict | None = None, params: dict | None = None,
                   body: Any = None, transport=None) -> Any:
    await _guard(url, allow_private)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport, follow_redirects=False,
                                     headers={"User-Agent": USER_AGENT, **(headers or {})}) as client:
            resp = await client.request(method, url, params=params, json=body)
    except httpx.HTTPError as exc:
        raise SearchError(f"could not be reached ({type(exc).__name__})") from None
    if resp.status_code in (301, 302, 303, 307, 308):
        raise SearchError("redirected (not followed)")
    if resp.status_code >= 400:
        raise _status_problem(resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise SearchError("did not send JSON") from None


# ---- one adapter per kind (each returns rows [{title,url,snippet}]) ----------------------------------------------

async def run_ddg(entry, key, query, limit, transport=None):
    from . import research
    return await research.ddg_search(query, limit, **({"transport": transport} if transport else {}))


async def run_wikipedia(entry, key, query, limit, transport=None):
    from . import research
    kw: dict = {"transport": transport} if transport else {}
    lang = entry["settings"].get("lang", "en")
    if lang != "en":
        kw["lang"] = lang
    return await research.wikipedia_search(query, limit, **kw)


async def run_searxng(entry, key, query, limit, transport=None):
    s = entry["settings"]
    data = await _request("GET", s["base_url"] + "/search", allow_private=s.get("allow_private", True),
                          params={"q": query, "format": "json"}, headers={"Accept": "application/json"}, transport=transport)
    return map_rows(data, {"results_path": "results", "title_field": "title", "url_field": "url", "snippet_field": "content"}, limit)


async def run_brave(entry, key, query, limit, transport=None):
    if not key:
        raise SearchError("no API key saved")
    data = await _request("GET", "https://api.search.brave.com/res/v1/web/search", allow_private=False,
                          params={"q": query, "count": str(min(limit, MAX_RESULTS))},
                          headers={"X-Subscription-Token": key, "Accept": "application/json"}, transport=transport)
    return map_rows(data, {"results_path": "web.results", "title_field": "title", "url_field": "url",
                           "snippet_field": "description"}, limit)


async def run_json_api(entry, key, query, limit, transport=None):
    s = entry["settings"]
    url = s["url_template"].replace("{query}", quote(query, safe="")).replace("{limit}", str(limit))
    headers: dict = {"Accept": "application/json"}
    params: dict | None = None
    body = None
    place, name = s.get("key_in", "none"), s.get("key_name", "")
    if s["method"] == "POST":
        body = fill_json(json.loads(s.get("body_template") or "{}"), query, limit)
    if key and place == "header":
        headers[name] = key
    elif key and place == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif key and place == "query":
        params = {name: key}
    elif key and place == "body":
        body = {**(body or {}), name: key}
    elif place != "none" and not key:
        raise SearchError("no API key saved")
    data = await _request(s["method"], url, allow_private=s.get("allow_private", False), headers=headers, params=params,
                          body=body, transport=transport)
    return map_rows(data, s, limit)


def _tags(item: dict) -> str:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    bits = [f"tags: {', '.join(str(t) for t in tags[:4])}"] if tags else []
    if isinstance(item.get("score"), int):
        bits.append(f"score {item['score']}")
    if item.get("is_answered"):
        bits.append("answered")
    return " · ".join(bits)


async def run_site(entry, key, query, limit, transport=None):
    s = entry["settings"]
    if s.get("mode") == "domains":
        from . import research
        scope = " OR ".join(f"site:{d}" for d in s["domains"])
        rows = await research.ddg_search(f"{query} ({scope})", limit * 2, **({"transport": transport} if transport else {}))
        keep = tuple(s["domains"])
        return [r for r in rows if any((urlsplit(r["url"]).hostname or "").lower().removeprefix("www.").endswith(d) for d in keep)][:limit]
    data = await _request("GET", "https://api.stackexchange.com/2.3/search/advanced", allow_private=False,
                          params={"order": "desc", "sort": "relevance", "q": query, "site": s["site"],
                                  "pagesize": str(min(limit, MAX_RESULTS))}, transport=transport)
    rows = []
    for item in (data.get("items") if isinstance(data, dict) else None) or []:
        url = good_link(item.get("link")) if isinstance(item, dict) else ""
        if url:
            rows.append({"title": tidy(item.get("title"), 160), "url": url, "snippet": tidy(_tags(item), 300)})
    return rows[:limit]


ADAPTERS = {"ddg-html": run_ddg, "wikipedia": run_wikipedia, "searxng": run_searxng, "brave": run_brave,
            "json-api": run_json_api, "site": run_site}


# ---- the fallback chain -----------------------------------------------------------------------------------------

class Outcome:
    def __init__(self):
        self.rows: list[dict] = []
        self.via = ""
        self.via_id = ""
        self.notes: list[str] = []


LAST: contextvars.ContextVar[Outcome | None] = contextvars.ContextVar("searchprov_last", default=None)
_BACKOFFS: dict[str, Backoff] = {}


def backoff_for(config) -> Backoff:
    key = str(config.data_path())
    return _BACKOFFS.setdefault(key, Backoff())


def take_outcome() -> Outcome | None:
    out = LAST.get()
    LAST.set(None)
    return out


def _wait(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min" if seconds >= 60 else f"{int(seconds)} s"


async def _one(entry: dict, store: provlist.ProviderList, query: str, limit: int, transport) -> list[dict]:
    key = store.secret(entry["id"]) if entry["kind"] in ("brave", "json-api") else ""
    try:
        rows = await asyncio.wait_for(ADAPTERS[entry["kind"]](entry, key, query, limit, transport), 25)
    except SearchError:
        raise
    except asyncio.TimeoutError:
        raise SearchError("took too long") from None
    except Exception as exc:  # noqa: BLE001 - one provider's bug or format change must not stop the others
        raise SearchError(f"failed ({type(exc).__name__})") from None
    return [r for r in rows if isinstance(r, dict) and good_link(r.get("url"))]


async def search(config, query: str, limit: int = 8, *, secrets: Any = None, transport=None) -> list[dict]:
    """Rows from the first enabled provider that finds something. The outcome (who answered, notes) is left in `LAST`
    for the caller (`take_outcome`). Never raises: no luck gives []."""
    store = list_for(config, secrets)
    back = backoff_for(config)
    out = Outcome()
    entries = store.enabled()
    if not entries:
        out.notes.append("No search provider is switched on (Settings > Search providers).")
    tried = 0
    for ignore_backoff in (False, True):
        for entry in entries:
            wait = back.blocked_for(entry["id"])
            if wait > 0 and not ignore_backoff:
                out.notes.append(f"Skipped {entry['label']} (failed lately, next try in {_wait(wait)})")
                continue
            if ignore_backoff and (wait <= 0):
                continue                            # already tried in the first pass
            tried += 1
            try:
                rows = await _one(entry, store, query, limit, transport)
            except SearchError as exc:
                back.fail(entry["id"], str(exc))
                out.notes.append(f"{entry['label']} failed: {exc}; trying the next one")
                continue
            back.ok(entry["id"])
            if rows:
                out.rows, out.via, out.via_id = rows[:limit], entry["label"], entry["id"]
                out.notes.append(f"Searched with {entry['label']}")
                LAST.set(out)
                return out.rows
            out.notes.append(f"{entry['label']} found nothing; trying the next one")
        if tried:
            break                                   # only fall back to skipped ones when nothing at all could be tried
    LAST.set(out)
    return []


# ---- routes -----------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, secrets: Any = None, transport=None) -> APIRouter:
    router = APIRouter(prefix="/api/search-providers", dependencies=[Depends(ctx.need_auth)])
    store = list_for(ctx.config, secrets)
    back = backoff_for(ctx.config)

    def guarded(fn, *args):
        try:
            return fn(*args)
        except ListError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    def snapshot():
        return {"entries": [{**e, "status": back.view(e["id"])} for e in store.public()],
                "kinds": {k: {**KIND_INFO[k], "fields": FIELDS[k]} for k in KINDS}, "presets": PRESETS,
                "note": "The first switched-on provider that finds results is used; the next one is tried when it fails or finds nothing."}

    @router.get("")
    def listing():
        return snapshot()

    @router.post("")
    def add(body: dict = Body(...)):
        entry = guarded(store.add, body)
        ctx.audit("search_provider_add", id=entry["id"], kind=entry["kind"])
        return {"ok": True, "entry": entry}

    @router.put("/order")
    def order(body: dict = Body(...)):
        return {"entries": guarded(store.reorder, body.get("ids"))}

    @router.post("/reset")
    def reset(body: dict = Body(default={})):
        if body.get("confirm") is not True:
            raise HTTPException(400, "Resetting needs confirm: true.")
        return {"entries": store.reset()}

    @router.post("/search")
    async def try_search(body: dict = Body(...)):
        query = clean(body.get("query"), 200)
        if len(query) < 2:
            raise HTTPException(400, "Write something to search for.")
        rows = await search(ctx.config, query, 8, secrets=secrets, transport=transport)
        out = take_outcome()
        return {"results": rows, "via": out.via if out else "", "notes": out.notes if out else [], "untrusted": True}

    @router.put("/{ident}")
    def edit(ident: str, body: dict = Body(...)):
        entry = guarded(store.update, ident, body)
        back.forget(ident)
        return {"ok": True, "entry": entry}

    @router.delete("/{ident}")
    def remove(ident: str):
        guarded(store.delete, ident)
        back.forget(ident)
        ctx.audit("search_provider_delete", id=ident)
        return {"ok": True}

    @router.post("/{ident}/test")
    async def test(ident: str):
        entry = store.get(ident)
        if entry is None:
            raise HTTPException(404, "No such provider.")
        try:
            rows = await _one(entry, store, "python programming", 5, transport)
        except SearchError as exc:
            back.fail(ident, str(exc))
            return {"ok": False, "error": str(exc)}
        back.ok(ident)
        return {"ok": bool(rows), "count": len(rows), "first": rows[0]["title"] if rows else "",
                "error": "" if rows else "It answered but found nothing for the test search."}

    return router
