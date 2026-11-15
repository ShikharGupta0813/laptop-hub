"""PUBLISHING DESTINATIONS: the list of places a post can go (dev.to, Hashnode, WordPress, Ghost, a markdown folder, any
webhook, or a 'manual' copy-and-paste site such as LinkedIn, Medium, X, Reddit). See docs/api/destinations.md.

A destination is {id, kind, label, enabled, mode, settings, format, secret reference}. Each kind is an isolated adapter
with the same small surface (test, save_draft, publish, stats), so adding a service means adding one class here, and
adding a SITE needs no code at all: choose `manual` (name + URL) or `webhook`.

Rules (tested)
- Secrets (API keys, application passwords, JWT keys, bearer tokens) live ONLY in the OS keyring, reached through the
  `secrets` object (get/set/delete by reference). They are never written to hub.json or destinations.json, never logged
  or audited, never returned by a route, and are scrubbed from every error text.
- Adapters never publish on their own: `publish` is called only from `Posts.publish` (hub/posts.py), which needs the
  owner's explicit confirmation for ONE destination at a time. Saving a draft always sends a draft/unpublished status.
- Text read back from a destination (comments) is untrusted data.
- Nothing here commits or pushes a git repository: the markdown-file kind only writes a file.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html as html_mod
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .common import JsonFile

log = logging.getLogger("hub.destinations")

SECRET_SERVICE = "redline-posts"          # OS keyring service; one entry per destination (name `dest-<id>`)
LEGACY_REF = "redline-devto/api_key"      # the pre-destinations dev.to key; copied once, never deleted
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_DESTINATIONS = 40
MAX_TAGS_HARD = 30
WARNING = "Do not publish company confidential material."
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
HTML_TAG = re.compile(r"<[^>]+>")


class DestinationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


# ---- small helpers ---------------------------------------------------------------------------------------

def clean(value: Any, limit: int = 200, multiline: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = _CONTROL.sub("", value)
    value = value.strip() if multiline else " ".join(value.split())
    return value[:limit]


def short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: max(1, n - 1)].rstrip() + "…"


def strip_html(text: str) -> str:
    return " ".join(html_mod.unescape(HTML_TAG.sub(" ", text or "")).split())


def clean_tags(value: Any, limit: int = 4) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    out: list[str] = []
    for t in value if isinstance(value, list) else []:
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())[:30]
        if t and t not in out:
            out.append(t)
    return out[:max(0, limit)]


def slugify(text: str, n: int = 60) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:n].strip("-") or "post"


def clean_url(value: Any, *, required: bool = False, label: str = "The address") -> str:
    v = clean(value, 300)
    if not v:
        if required:
            raise DestinationError(f"{label} is needed.", 400)
        return ""
    m = re.match(r"^(https?)://([^/\s?#]+)([^\s]*)$", v, re.I)
    if not m or "@" in m.group(2):
        raise DestinationError(f"{label} must be a web address starting with http:// or https://.", 400)
    return v.rstrip("/") if not m.group(3) or m.group(3) == "/" else v


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def md_to_html(md: str) -> str:
    """A small, safe markdown-to-HTML for WordPress and Ghost (everything is escaped first)."""
    esc = html_mod.escape

    def inline(s: str) -> str:
        s = esc(s, quote=False)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
        return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{esc(m.group(2))}">{m.group(1)}</a>', s)

    out: list[str] = []
    lines = (md or "").replace("\r", "").split("\n")
    i, para, items, kind = 0, [], [], ""

    def flush():
        nonlocal para, items, kind
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{kind}>")
        para, items, kind = [], [], ""

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
        elif (h := re.match(r"^(#{1,6})\s+(.+)$", line)):
            flush()
            out.append(f"<h{len(h.group(1))}>{inline(h.group(2).strip())}</h{len(h.group(1))}>")
        elif (li := re.match(r"^\s*(?:([-*])|(\d+)\.)\s+(.+)$", line)):
            if para:
                flush()
            want = "ul" if li.group(1) else "ol"
            if kind and kind != want:
                flush()
            kind = want
            items.append(li.group(3).strip())
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


# ---- the owner's formatting rules, per destination ---------------------------------------------------------

STYLES = ("article", "short", "thread")
DEFAULT_TEMPLATES = {
    "short": "{hook}\n\n{points}\n\n{cta}\n\n{hashtags}",
    "thread": "{hook}\n\n{points}\n\n{cta} {hashtags}",
}
TEMPLATE_HELP = "Placeholders: {title} {hook} {points} {cta} {link} {hashtags}. Empty uses the built-in text."


def clean_format(value: Any, base: dict) -> dict:
    """Merge the owner's edits over `base`, validating every field. Unknown keys are refused."""
    out = dict(base)
    if value in (None, {}):
        return out
    if not isinstance(value, dict):
        raise DestinationError("Formatting rules must be an object.", 400)
    unknown = [k for k in value if k not in base]
    if unknown:
        raise DestinationError(f"Unknown formatting rule: {', '.join(map(str, unknown))[:60]}.", 400)

    def num(key, lo, hi):
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise DestinationError(f"{key} must be a whole number from {lo} to {hi}.", 400)
        out[key] = v
    for k in value:
        if k == "style":
            if value[k] not in STYLES:
                raise DestinationError("style must be article, short or thread.", 400)
            out[k] = value[k]
        elif k == "max_length":
            num(k, 0, 100000)
        elif k == "tag_limit":
            num(k, 0, MAX_TAGS_HARD)
        elif k == "title_length":
            num(k, 20, 300)
        elif k == "hashtags":
            if not isinstance(value[k], bool):
                raise DestinationError("hashtags must be true or false.", 400)
            out[k] = value[k]
        elif k == "template":
            if not isinstance(value[k], str) or len(value[k]) > 2000:
                raise DestinationError("template must be text up to 2000 characters.", 400)
            out[k] = _CONTROL.sub("", value[k]).strip()
    return out


def render_template(template: str, values: dict) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def split_parts(text: str, limit: int) -> list[str]:
    """One numbered part per line; a line longer than `limit` is wrapped at word boundaries."""
    room = max(20, limit - 8)
    parts: list[str] = []
    for line in [l.strip() for l in text.splitlines() if l.strip()]:
        while len(line) > room:
            cut = line.rfind(" ", 0, room)
            cut = cut if cut > room // 2 else room
            parts.append(line[:cut].rstrip())
            line = line[cut:].strip()
        if line:
            parts.append(line)
    n = len(parts)
    return [f"{k}/{n} {p}" for k, p in enumerate(parts, 1)] if n > 1 else parts


def make_variant(draft: dict, dest: dict, link: str = "") -> dict:
    """The version of one draft for one destination, from that destination's formatting rules. Pure: sends nothing."""
    fmt = dest["format"]
    style = fmt.get("style", "article")
    tags = clean_tags(draft.get("tags") or [], fmt.get("tag_limit", 4)) if fmt.get("tag_limit", 4) else []
    title = short(draft.get("title") or "", fmt.get("title_length", 150))
    body = draft.get("body") or ""
    canonical = clean(draft.get("canonical_url"), 300)
    base = {"destination": dest["id"], "label": dest["label"], "kind": dest["kind"], "style": style, "title": title,
            "tags": tags, "description": clean(draft.get("description"), 200), "series": clean(draft.get("series"), 80),
            "canonical_url": canonical, "open": (dest.get("settings") or {}).get("url", "") if dest["mode"] == "manual" else ""}
    if style == "article":
        limit = fmt.get("max_length", 0)
        text = body
        return {**base, "body": text, "text": text, "parts": [], "chars": len(text), "over_limit": bool(limit and len(text) > limit),
                "max_length": limit}
    points = [re.sub(r"^[-*\d.\s]+", "", l).strip() for l in body.splitlines() if re.match(r"^\s*(?:[-*]|\d+\.)\s+\S", l)]
    heads = [l.lstrip("# ").strip() for l in body.splitlines() if l.startswith("## ")]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", strip_html(re.sub(r"```.*?```", " ", body, flags=re.S))) if len(s.strip()) > 30]
    hook = short(draft.get("description") or (sentences[0] if sentences else title), 140)
    key = (points or heads or sentences[1:])[:4]
    tagged = [t for t in (tags or (["softwareengineering"] if fmt.get("tag_limit", 4) else []))][:fmt.get("tag_limit", 4)]
    hashtags = " ".join("#" + t for t in tagged) if fmt.get("hashtags") else ""
    url = link or canonical
    cta = f"Full write-up: {url}" if url else "Full write-up: [add the link after you publish]"
    template = fmt.get("template") or DEFAULT_TEMPLATES[style]
    text = render_template(template, {"title": title, "hook": hook, "points": "\n".join(f"• {short(p, 160)}" for p in key),
                                      "cta": cta, "link": url, "hashtags": hashtags})
    limit = fmt.get("max_length", 0)
    if style == "thread":
        parts = split_parts(text, limit) if limit else [text]
        return {**base, "body": text, "text": "\n\n".join(parts), "parts": parts, "chars": max((len(p) for p in parts), default=0),
                "over_limit": False, "max_length": limit}
    over = bool(limit and len(text) > limit)
    return {**base, "body": text, "text": text, "parts": [], "chars": len(text), "over_limit": over, "max_length": limit}


# ---- HTTP -------------------------------------------------------------------------------------------------

class Http:
    """One JSON request with the standard error mapping. `hide` are secret strings to strip from any message."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    @staticmethod
    def scrub(text: str, hide: list[str]) -> str:
        for h in hide:
            if h and len(h) >= 4:
                text = text.replace(h, "***")
        return text

    async def json(self, method: str, url: str, name: str, hide: list[str], *, headers: dict | None = None,
                   params: dict | None = None, body: Any = None) -> Any:
        h = {"User-Agent": "REDLINE-hub", **(headers or {})}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=20.0, headers=h) as http:
                r = await http.request(method, url, params=params, json=body)
        except httpx.HTTPError as exc:
            log.info("%s request failed: %s", name, type(exc).__name__)
            raise DestinationError(f"{name} could not be reached ({type(exc).__name__}).", 502) from None
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code in (401, 403):
            raise DestinationError(f"{name} refused the key or password. Make a new one and save it again.", 502)
        if r.status_code == 429:
            raise DestinationError(f"{name} says too many requests. Try again in a minute.", 502)
        if r.status_code >= 400:
            detail = ""
            if isinstance(data, dict):
                err = data.get("error") or data.get("message") or data.get("errors") or ""
                if isinstance(err, list) and err:
                    err = err[0].get("message") if isinstance(err[0], dict) else err[0]
                detail = self.scrub(clean(str(err), 200), hide)
            raise DestinationError(f"{name} answered {r.status_code}" + (f": {detail}" if detail else "."), 502)
        return data


# ---- adapters ---------------------------------------------------------------------------------------------

class Adapter:
    """One kind of destination. Methods are async and return plain dicts; they raise DestinationError."""
    kind = ""
    label = ""
    help = ""
    mode = "api"                 # api | file | manual
    drafts = False               # can hold an unpublished draft remotely (or in a file)
    publishes = True             # has a publish step the owner taps
    stats = False
    comments = False
    secret_label = ""            # "" = this kind needs no secret
    secret_re = re.compile(r"^\S{8,300}$")
    secret_optional = False
    fields: tuple = ()           # ({"key","label","type","required","placeholder"}, ...)
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 10, "title_length": 200, "template": ""}

    def hide(self, secret: str) -> list[str]:
        return [secret]

    async def test(self, dest: dict, secret: str, http: Http) -> dict:
        raise NotImplementedError

    async def save_draft(self, dest: dict, secret: str, post: dict, target: dict, http: Http) -> dict:
        raise DestinationError("This destination has no drafts.", 400)

    async def publish(self, dest: dict, secret: str, post: dict, target: dict, http: Http) -> dict:
        raise DestinationError("This destination cannot publish for you.", 400)

    async def fetch_stats(self, dest: dict, secret: str, http: Http, known: dict, baselined: bool) -> tuple[list[dict], list[dict]]:
        return [], []

    @staticmethod
    def art(**kw) -> dict:
        return {"id": kw["id"], "title": clean(kw.get("title"), 160), "url": clean(str(kw.get("url") or ""), 300),
                "published": kw.get("published") is True, "published_at": clean(str(kw.get("published_at") or ""), 40),
                "views": int(kw.get("views") or 0), "reactions": int(kw.get("reactions") or 0), "comments": int(kw.get("comments") or 0)}

    @staticmethod
    def com(c: dict, art: dict) -> dict:
        return {**c, "article_id": art["id"], "article_title": art["title"], "article_url": art["url"]}


class DevtoAdapter(Adapter):
    kind, label, drafts, stats, comments = "devto", "dev.to", True, True, True
    secret_label = "dev.to API key"
    help = "Make a key at dev.to/settings/extensions."
    API = "https://dev.to/api"
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 4, "title_length": 140, "template": ""}

    def head(self, secret):
        return {"api-key": secret, "Accept": "application/vnd.forem.api-v1+json"}

    async def call(self, http, secret, method, path, **kw):
        return await http.json(method, self.API + path, "dev.to", [secret], headers=self.head(secret), **kw)

    async def test(self, dest, secret, http):
        data = await self.call(http, secret, "GET", "/users/me")
        return {"ok": True, "name": clean((data or {}).get("username") if isinstance(data, dict) else "", 60)}

    @staticmethod
    def article(post: dict, **extra) -> dict:
        a = {"title": post["title"], "body_markdown": post["body"], "tags": post["tags"], "description": post["description"], **extra}
        if post.get("series"):
            a["series"] = post["series"]
        if post.get("canonical_url"):
            a["canonical_url"] = post["canonical_url"]
        return a

    async def save_draft(self, dest, secret, post, target, http):
        art = self.article(post, published=False)
        if target.get("remote_id"):
            data = await self.call(http, secret, "PUT", f"/articles/{int(target['remote_id'])}", body={"article": art})
            data = data if isinstance(data, dict) else {}
            return {"remote_id": data.get("id", target["remote_id"]), "url": clean(str(data.get("url") or target.get("url") or ""), 300)}
        data = await self.call(http, secret, "POST", "/articles", body={"article": art})
        if not isinstance(data, dict) or "id" not in data:
            raise DestinationError("dev.to answered, but not with the new article.", 502)
        return {"remote_id": data["id"], "url": clean(str(data.get("url") or ""), 300)}

    async def publish(self, dest, secret, post, target, http):
        data = await self.call(http, secret, "PUT", f"/articles/{int(target['remote_id'])}", body={"article": {"published": True}})
        data = data if isinstance(data, dict) else {}
        return {"url": clean(str(data.get("url") or target.get("url") or ""), 300),
                "published_at": clean(str(data.get("published_at") or ""), 40)}

    @staticmethod
    def flatten(tree: Any) -> list[dict]:
        out: list[dict] = []
        for c in tree if isinstance(tree, list) else []:
            if not isinstance(c, dict):
                continue
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            out.append({"id": clean(str(c.get("id_code") or c.get("id") or ""), 40),
                        "who": clean(str(user.get("username") or user.get("name") or ""), 60),
                        "text": clean(strip_html(str(c.get("body_html") or c.get("body") or "")), 500),
                        "at": clean(str(c.get("created_at") or ""), 40)})
            out.extend(DevtoAdapter.flatten(c.get("children")))
        return [c for c in out if c["id"]]

    async def fetch_stats(self, dest, secret, http, known, baselined):
        rows = await self.call(http, secret, "GET", "/articles/me/all", params={"per_page": 100})
        arts, comments = [], []
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and r.get("id") is not None:
                arts.append(self.art(id=r["id"], title=r.get("title"), url=r.get("url"), published=r.get("published"),
                                     published_at=r.get("published_at"), views=r.get("page_views_count"),
                                     reactions=r.get("public_reactions_count") or r.get("positive_reactions_count"),
                                     comments=r.get("comments_count")))
        for a in arts:
            if not a["published"] or a["comments"] <= 0:
                continue
            if baselined and str(a["id"]) in known and known[str(a["id"])].get("comments") == a["comments"]:
                continue                                        # unchanged since the last check: no extra request
            try:
                tree = await self.call(http, secret, "GET", "/comments", params={"a_id": a["id"]})
            except DestinationError:
                continue
            comments += [self.com(c, a) for c in self.flatten(tree)]
        return arts, comments


class HashnodeAdapter(Adapter):
    """UNVERIFIED against the live API (written from Hashnode's public GraphQL docs, no network at build time)."""
    kind, label, drafts, stats, comments = "hashnode", "Hashnode", True, True, True
    secret_label = "Hashnode personal access token"
    help = "Token: hashnode.com/settings/developer. Publication id: your blog's dashboard URL or the API explorer (gql.hashnode.com)."
    GQL = "https://gql.hashnode.com"
    fields = ({"key": "publication_id", "label": "Publication id", "type": "text", "required": True, "placeholder": "24-character id"},)
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 5, "title_length": 150, "template": ""}

    async def q(self, http, secret, query, variables=None):
        data = await http.json("POST", self.GQL, "Hashnode", [secret], headers={"Authorization": secret},
                               body={"query": query, "variables": variables or {}})
        if isinstance(data, dict) and data.get("errors"):
            first = data["errors"][0]
            raise DestinationError("Hashnode answered: " + http.scrub(clean(str(first.get("message") if isinstance(first, dict) else first), 200), [secret]), 502)
        return (data or {}).get("data") or {}

    async def test(self, dest, secret, http):
        d = await self.q(http, secret, "query { me { username } }")
        return {"ok": True, "name": clean(((d.get("me") or {}).get("username")) or "", 60)}

    async def save_draft(self, dest, secret, post, target, http):
        if target.get("remote_id"):
            raise DestinationError("A draft is already on Hashnode. Edit it there, then publish it from here.", 409)
        inp = {"title": post["title"], "contentMarkdown": post["body"], "publicationId": dest["settings"].get("publication_id", ""),
               "tags": [{"slug": t, "name": t} for t in post["tags"]]}
        if post.get("canonical_url"):
            inp["originalArticleURL"] = post["canonical_url"]
        d = await self.q(http, secret, "mutation($input: CreateDraftInput!){ createDraft(input:$input){ draft{ id slug title } } }", {"input": inp})
        draft = ((d.get("createDraft") or {}).get("draft")) or {}
        if not draft.get("id"):
            raise DestinationError("Hashnode answered, but not with the new draft.", 502)
        return {"remote_id": str(draft["id"]), "url": f"https://hashnode.com/draft/{draft['id']}"}

    async def publish(self, dest, secret, post, target, http):
        d = await self.q(http, secret, "mutation($input: PublishDraftInput!){ publishDraft(input:$input){ post{ id url } } }",
                         {"input": {"draftId": str(target["remote_id"])}})
        p = ((d.get("publishDraft") or {}).get("post")) or {}
        return {"url": clean(str(p.get("url") or ""), 300), "published_at": ""}

    async def fetch_stats(self, dest, secret, http, known, baselined):
        pid = dest["settings"].get("publication_id", "")
        d = await self.q(http, secret, "query($id: ObjectId!){ publication(id:$id){ posts(first:20){ edges{ node{ id title url publishedAt views reactionCount responseCount } } } } }", {"id": pid})
        arts, comments = [], []
        for e in (((d.get("publication") or {}).get("posts") or {}).get("edges")) or []:
            n = e.get("node") or {}
            if n.get("id"):
                arts.append(self.art(id=n["id"], title=n.get("title"), url=n.get("url"), published=True, published_at=n.get("publishedAt"),
                                     views=n.get("views"), reactions=n.get("reactionCount"), comments=n.get("responseCount")))
        for a in arts:
            if a["comments"] <= 0 or (baselined and str(a["id"]) in known and known[str(a["id"])].get("comments") == a["comments"]):
                continue
            try:
                cd = await self.q(http, secret, "query($id: ID!){ post(id:$id){ comments(first:20){ edges{ node{ id content dateAdded author{ username } } } } } }", {"id": a["id"]})
            except DestinationError:
                continue
            for e in ((((cd.get("post") or {}).get("comments")) or {}).get("edges")) or []:
                n = e.get("node") or {}
                if n.get("id"):
                    comments.append(self.com({"id": clean(str(n["id"]), 40), "who": clean(str((n.get("author") or {}).get("username") or ""), 60),
                                              "text": clean(strip_html(str(n.get("content") or "")), 500), "at": clean(str(n.get("dateAdded") or ""), 40)}, a))
        return arts, comments


class WordPressAdapter(Adapter):
    kind, label, drafts, stats, comments = "wordpress", "WordPress", True, True, True
    secret_label = "WordPress application password"
    secret_re = re.compile(r"^[\x20-\x7e]{8,300}$")
    help = "In WordPress: Users, Profile, Application Passwords. Needs the REST API on (the default)."
    fields = ({"key": "url", "label": "Site address", "type": "url", "required": True, "placeholder": "https://myblog.com"},
              {"key": "username", "label": "Username", "type": "text", "required": True, "placeholder": "your WordPress user"})
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 10, "title_length": 200, "template": ""}

    def hide(self, secret):
        return [secret, secret.replace(" ", "")]

    def head(self, dest, secret):
        raw = f"{dest['settings'].get('username', '')}:{secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    async def call(self, http, dest, secret, method, path, **kw):
        base = dest["settings"].get("url", "")
        hide = self.hide(secret) + [self.head(dest, secret)["Authorization"].split(" ", 1)[1]]
        return await http.json(method, f"{base}/wp-json/wp/v2{path}", "WordPress", hide, headers=self.head(dest, secret), **kw)

    async def test(self, dest, secret, http):
        d = await self.call(http, dest, secret, "GET", "/users/me", params={"context": "edit"})
        return {"ok": True, "name": clean((d or {}).get("name") if isinstance(d, dict) else "", 60)}

    async def save_draft(self, dest, secret, post, target, http):
        body = {"title": post["title"], "content": md_to_html(post["body"]), "status": "draft", "excerpt": post["description"]}
        if target.get("remote_id"):
            d = await self.call(http, dest, secret, "POST", f"/posts/{int(target['remote_id'])}", body=body)
        else:
            d = await self.call(http, dest, secret, "POST", "/posts", body=body)
        if not isinstance(d, dict) or "id" not in d:
            raise DestinationError("WordPress answered, but not with the post.", 502)
        return {"remote_id": d["id"], "url": clean(str(d.get("link") or ""), 300)}

    async def publish(self, dest, secret, post, target, http):
        d = await self.call(http, dest, secret, "POST", f"/posts/{int(target['remote_id'])}", body={"status": "publish"})
        d = d if isinstance(d, dict) else {}
        return {"url": clean(str(d.get("link") or target.get("url") or ""), 300), "published_at": clean(str(d.get("date_gmt") or ""), 40)}

    async def fetch_stats(self, dest, secret, http, known, baselined):
        rows = await self.call(http, dest, secret, "GET", "/posts", params={"per_page": 50, "status": "any", "_fields": "id,title,link,status,date_gmt"})
        arts = []
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and r.get("id") is not None:
                t = r.get("title")
                arts.append(self.art(id=r["id"], title=strip_html(t.get("rendered") if isinstance(t, dict) else str(t or "")), url=r.get("link"),
                                     published=r.get("status") == "publish", published_at=r.get("date_gmt")))
        by_id = {str(a["id"]): a for a in arts}
        try:
            rows = await self.call(http, dest, secret, "GET", "/comments", params={"per_page": 50, "orderby": "date", "order": "desc"})
        except DestinationError:
            rows = []
        comments = []
        for c in rows if isinstance(rows, list) else []:
            a = by_id.get(str(c.get("post"))) if isinstance(c, dict) else None
            if a is None:
                continue
            a["comments"] += 1
            body = c.get("content")
            comments.append(self.com({"id": clean(str(c.get("id")), 40), "who": clean(str(c.get("author_name") or ""), 60),
                                      "text": clean(strip_html(body.get("rendered") if isinstance(body, dict) else str(body or "")), 500),
                                      "at": clean(str(c.get("date_gmt") or ""), 40)}, a))
        return arts, comments


class GhostAdapter(Adapter):
    """UNVERIFIED against a live Ghost (written from the Admin API docs)."""
    kind, label, drafts, stats = "ghost", "Ghost", True, True
    secret_label = "Ghost Admin API key (id:secret)"
    secret_re = re.compile(r"^[0-9a-fA-F]{8,40}:[0-9a-fA-F]{32,128}$")
    help = "Ghost admin: Settings, Integrations, add a custom integration, copy the Admin API key."
    fields = ({"key": "url", "label": "Site address", "type": "url", "required": True, "placeholder": "https://myblog.ghost.io"},)
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 10, "title_length": 255, "template": ""}

    @staticmethod
    def token(key: str, now: int | None = None) -> str:
        kid, secret = key.split(":", 1)
        now = int(time.time()) if now is None else now
        head = b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
        body = b64url(json.dumps({"iat": now, "exp": now + 300, "aud": "/admin/"}, separators=(",", ":")).encode())
        sig = hmac.new(bytes.fromhex(secret), f"{head}.{body}".encode(), hashlib.sha256).digest()
        return f"{head}.{body}.{b64url(sig)}"

    async def call(self, http, dest, secret, method, path, **kw):
        tok = self.token(secret)
        return await http.json(method, f"{dest['settings'].get('url', '')}/ghost/api/admin{path}", "Ghost", [secret, secret.split(":", 1)[-1], tok],
                               headers={"Authorization": f"Ghost {tok}", "Accept-Version": "v5.0"}, **kw)

    async def test(self, dest, secret, http):
        d = await self.call(http, dest, secret, "GET", "/site/")
        site = (d or {}).get("site") if isinstance(d, dict) else {}
        return {"ok": True, "name": clean((site or {}).get("title") or "", 60)}

    @staticmethod
    def row(d: Any) -> dict:
        posts = (d or {}).get("posts") if isinstance(d, dict) else None
        if not posts or not isinstance(posts[0], dict) or "id" not in posts[0]:
            raise DestinationError("Ghost answered, but not with the post.", 502)
        return posts[0]

    async def latest(self, http, dest, secret, remote_id):
        return self.row(await self.call(http, dest, secret, "GET", f"/posts/{remote_id}/"))

    async def save_draft(self, dest, secret, post, target, http):
        post_body = {"title": post["title"], "html": md_to_html(post["body"]), "status": "draft", "custom_excerpt": post["description"][:300] or None,
                     "tags": [{"name": t} for t in post["tags"]]}
        if post.get("canonical_url"):
            post_body["canonical_url"] = post["canonical_url"]
        if target.get("remote_id"):
            cur = await self.latest(http, dest, secret, target["remote_id"])
            row = self.row(await self.call(http, dest, secret, "PUT", f"/posts/{target['remote_id']}/", params={"source": "html"},
                                           body={"posts": [{**post_body, "updated_at": cur.get("updated_at")}]}))
        else:
            row = self.row(await self.call(http, dest, secret, "POST", "/posts/", params={"source": "html"}, body={"posts": [post_body]}))
        return {"remote_id": row["id"], "url": clean(str(row.get("url") or ""), 300), "meta": {"updated_at": row.get("updated_at")}}

    async def publish(self, dest, secret, post, target, http):
        cur = await self.latest(http, dest, secret, target["remote_id"])
        row = self.row(await self.call(http, dest, secret, "PUT", f"/posts/{target['remote_id']}/",
                                       body={"posts": [{"status": "published", "updated_at": cur.get("updated_at")}]}))
        return {"url": clean(str(row.get("url") or target.get("url") or ""), 300), "published_at": clean(str(row.get("published_at") or ""), 40)}

    async def fetch_stats(self, dest, secret, http, known, baselined):
        d = await self.call(http, dest, secret, "GET", "/posts/", params={"limit": 50, "order": "updated_at desc"})
        arts = []
        for r in (d or {}).get("posts", []) if isinstance(d, dict) else []:
            if isinstance(r, dict) and r.get("id"):
                arts.append(self.art(id=r["id"], title=r.get("title"), url=r.get("url"), published=r.get("status") == "published",
                                     published_at=r.get("published_at")))
        return arts, []


class MarkdownFileAdapter(Adapter):
    """Writes a markdown file with front matter into a folder (a GitHub Pages / Hugo / Jekyll repo, or any folder).
    It never runs git: committing and pushing stay with the owner."""
    kind, label, mode, drafts = "markdown-file", "Markdown file / GitHub Pages", "file", True
    secret_label = ""
    help = "Writes a .md file with front matter (draft: true until you publish). You commit and push it yourself."
    fields = ({"key": "folder", "label": "Folder", "type": "path", "required": True, "placeholder": "C:\\sites\\blog\\_posts"},)
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 10, "title_length": 200, "template": ""}

    @staticmethod
    def folder(dest: dict) -> Path:
        raw = dest["settings"].get("folder", "")
        p = Path(raw).expanduser()
        if not raw or not p.is_dir():
            raise DestinationError("The folder does not exist on this laptop. Check the path in this destination.", 400)
        return p.resolve()

    async def test(self, dest, secret, http):
        p = self.folder(dest)
        if not os.access(p, os.W_OK):
            raise DestinationError("The hub cannot write into that folder.", 400)
        return {"ok": True, "name": p.name, "note": "git folder: commit and push yourself" if (p / ".git").exists() or (p.parent / ".git").exists() else ""}

    @staticmethod
    def render(post: dict, draft: bool) -> str:
        q = json.dumps
        lines = ["---", f"title: {q(post['title'], ensure_ascii=False)}", f"date: {post.get('date', '')}",
                 f"description: {q(post['description'], ensure_ascii=False)}", "tags: [" + ", ".join(post["tags"]) + "]"]
        if post.get("series"):
            lines.append(f"series: {q(post['series'], ensure_ascii=False)}")
        if post.get("canonical_url"):
            lines.append(f"canonical_url: {q(post['canonical_url'])}")
        lines += [f"draft: {'true' if draft else 'false'}", "---", "", post["body"].strip(), ""]
        return "\n".join(lines)

    def path_for(self, dest: dict, post: dict, target: dict) -> Path:
        folder = self.folder(dest)
        name = target.get("remote_id") or f"{post.get('date', '')[:10] or 'post'}-{slugify(post['title'])}.md"
        name = Path(str(name)).name
        path = (folder / name).resolve()
        if path.parent != folder:
            raise DestinationError("That file name is not allowed.", 400)
        return path

    async def write(self, dest, post, target, draft):
        path = self.path_for(dest, post, target)
        text = self.render(post, draft)
        try:
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
        except OSError:
            raise DestinationError("The file could not be written (is the folder read-only?).", 500) from None
        return {"remote_id": path.name, "url": str(path)}

    async def save_draft(self, dest, secret, post, target, http):
        return await self.write(dest, post, target, True)

    async def publish(self, dest, secret, post, target, http):
        if not self.path_for(dest, post, target).exists():
            raise DestinationError("The draft file is gone. Save the draft again first.", 409)
        out = await self.write(dest, post, target, False)
        return {**out, "published_at": post.get("date", "")}


class WebhookAdapter(Adapter):
    kind, label, drafts = "webhook", "Webhook (any site or automation)", False
    secret_label = "Bearer token (optional)"
    secret_optional = True
    help = "Sends {title, body_markdown, tags, canonical_url} as JSON when YOU tap Publish. Works with Zapier, n8n, Make, a GitHub Action, your own site."
    fields = ({"key": "url", "label": "Webhook address", "type": "url", "required": True, "placeholder": "https://example.com/hook"},)
    format = {"style": "article", "max_length": 0, "hashtags": False, "tag_limit": 10, "title_length": 200, "template": ""}

    async def test(self, dest, secret, http):
        return {"ok": True, "name": "", "note": "Nothing was sent: a webhook can only be tried by publishing."}

    async def publish(self, dest, secret, post, target, http):
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        body = {"title": post["title"], "body_markdown": post["body"], "tags": post["tags"], "canonical_url": post.get("canonical_url", "")}
        try:
            async with httpx.AsyncClient(transport=http.transport, timeout=20.0, headers={"User-Agent": "REDLINE-hub", **headers}) as c:
                r = await c.post(dest["settings"]["url"], json=body)
        except httpx.HTTPError as exc:
            raise DestinationError(f"The webhook could not be reached ({type(exc).__name__}).", 502) from None
        if r.status_code >= 300:
            raise DestinationError(f"The webhook answered {r.status_code}.", 502)
        url = ""
        try:
            data = r.json()
            url = clean(str(data.get("url") or ""), 300) if isinstance(data, dict) else ""
        except ValueError:
            pass
        return {"url": url, "published_at": ""}


class ManualAdapter(Adapter):
    kind, label, mode, publishes = "manual", "Manual (copy and paste)", "manual", False
    secret_label = ""
    help = "For LinkedIn, Medium, X, Reddit or any site: the hub makes the text, opens the composer, you paste and tap 'I posted'."
    fields = ({"key": "url", "label": "Composer address", "type": "url", "required": False, "placeholder": "https://www.linkedin.com/feed/"},)
    format = {"style": "short", "max_length": 3000, "hashtags": True, "tag_limit": 5, "title_length": 150, "template": ""}

    async def test(self, dest, secret, http):
        return {"ok": True, "name": "", "note": "Nothing to connect: you copy and paste."}


ADAPTERS: dict[str, Adapter] = {a.kind: a for a in (DevtoAdapter(), HashnodeAdapter(), WordPressAdapter(), GhostAdapter(),
                                                    MarkdownFileAdapter(), WebhookAdapter(), ManualAdapter())}


def kinds_view() -> list[dict]:
    return [{"kind": a.kind, "label": a.label, "mode": a.mode, "help": a.help, "fields": list(a.fields),
             "secret_label": a.secret_label, "secret_optional": a.secret_optional,
             "drafts": a.drafts, "publishes": a.publishes, "stats": a.stats, "comments": a.comments, "format": dict(a.format)}
            for a in ADAPTERS.values()]


# ---- the starter list ---------------------------------------------------------------------------------------

def _starter() -> list[dict]:
    def s(id_, kind, label, settings=None, **fmt):
        return {"id": id_, "kind": kind, "label": label, "enabled": True, "settings": settings or {}, "format": fmt}
    return [
        s("devto", "devto", "dev.to"),
        s("hashnode", "hashnode", "Hashnode"),
        s("wordpress", "wordpress", "WordPress"),
        s("ghost", "ghost", "Ghost"),
        s("medium", "manual", "Medium (manual)", {"url": "https://medium.com/new-story"}, style="article", max_length=0, hashtags=False, tag_limit=5, title_length=100),
        s("linkedin", "manual", "LinkedIn (manual)", {"url": "https://www.linkedin.com/feed/"}, style="short", max_length=3000, hashtags=True, tag_limit=5),
        s("x", "manual", "X (manual)", {"url": "https://x.com/home"}, style="thread", max_length=280, hashtags=True, tag_limit=2),
        s("reddit", "manual", "Reddit (manual)", {"url": "https://www.reddit.com/submit"}, style="article", max_length=40000, hashtags=False, tag_limit=0, title_length=300),
        s("blog-webhook", "webhook", "My blog (webhook)"),
    ]


# ---- the store ----------------------------------------------------------------------------------------------

class KeyringSecrets:
    """Secrets in the OS keyring, by reference `service/name`. Tests pass a fake with get/set/delete(ref)."""

    @staticmethod
    def _split(ref: str) -> tuple[str, str]:
        service, _, name = ref.partition("/")
        return service, name

    def get(self, ref: str) -> str:
        try:
            import keyring
            return (keyring.get_password(*self._split(ref)) or "").strip()
        except Exception:  # noqa: BLE001 - no keyring installed or no backend
            return ""

    def set(self, ref: str, value: str) -> None:
        try:
            import keyring
            keyring.set_password(*self._split(ref), value)
        except Exception:  # noqa: BLE001
            raise DestinationError("The key could not be saved: the OS keyring is not available "
                                   "(pip install -r requirements-work.txt).", 503) from None

    def delete(self, ref: str) -> None:
        try:
            import keyring
            keyring.delete_password(*self._split(ref))
        except Exception:  # noqa: BLE001 - nothing stored is fine
            pass


class Destinations:
    def __init__(self, path: Path, secrets: Any = None, transport: httpx.AsyncBaseTransport | None = None):
        self.file = JsonFile(path)
        self.secrets = secrets or KeyringSecrets()
        self.http = Http(transport)
        self.lock = threading.RLock()

    # -- storage
    @staticmethod
    def ref(dest_id: str) -> str:
        return f"{SECRET_SERVICE}/dest-{dest_id}"

    def _full(self, row: dict) -> dict:
        """A stored row with its kind's defaults filled in."""
        a = ADAPTERS[row["kind"]]
        fmt = {**a.format, **(row.get("format") or {})}
        return {"id": row["id"], "kind": row["kind"], "label": row["label"], "enabled": row.get("enabled") is not False,
                "mode": a.mode, "settings": dict(row.get("settings") or {}), "format": fmt}

    def _seed(self) -> dict:
        items = _starter()
        data = {"version": 1, "items": items}
        try:                                       # migration: the old dev.to key moves to the dev.to destination
            legacy = self.secrets.get(LEGACY_REF)
            if legacy and not self.secrets.get(self.ref("devto")):
                self.secrets.set(self.ref("devto"), legacy)
        except DestinationError:
            pass
        try:
            self.file.save(data)
        except OSError:
            log.warning("destinations could not be saved")
        return data

    def load(self) -> dict:
        with self.lock:
            d = self.file.load(None)
            if not isinstance(d, dict) or not isinstance(d.get("items"), list):
                d = self._seed()
            d["items"] = [r for r in d["items"] if isinstance(r, dict) and r.get("kind") in ADAPTERS and ID_RE.match(str(r.get("id") or ""))]
            return d

    def _save(self, d: dict) -> None:
        try:
            self.file.save(d)
        except OSError:
            log.warning("destinations could not be saved")

    def all(self) -> list[dict]:
        return [self._full(r) for r in self.load()["items"]]

    def get(self, ident: Any) -> dict:
        for d in self.all():
            if d["id"] == ident:
                return d
        raise DestinationError("There is no destination with that id.", 404)

    def find(self, token: Any) -> dict:
        """By id, or by name (any case)."""
        t = str(token or "").strip().lower()
        rows = self.all()
        for d in rows:
            if d["id"] == t or d["label"].lower() == t:
                return d
        raise DestinationError("There is no destination with that name. Ask for post_destinations to see them.", 404)

    def first_of_kind(self, kind: str) -> dict | None:
        return next((d for d in self.all() if d["kind"] == kind), None)

    # -- secrets
    def secret(self, dest: dict) -> str:
        return self.secrets.get(self.ref(dest["id"])) if ADAPTERS[dest["kind"]].secret_label else ""

    def set_secret(self, dest_id: str, value: Any) -> None:
        dest = self.get(dest_id)
        a = ADAPTERS[dest["kind"]]
        if not a.secret_label:
            raise DestinationError("This kind of destination has no key or password.", 400)
        v = value.strip() if isinstance(value, str) else ""
        if not a.secret_re.match(v):
            raise DestinationError(f"That does not look like a valid {a.secret_label}. Paste it again without extra characters.", 400)
        self.secrets.set(self.ref(dest_id), v)

    def clear_secret(self, dest_id: str) -> None:
        self.secrets.delete(self.ref(dest_id))

    def connected(self, dest: dict) -> bool:
        a = ADAPTERS[dest["kind"]]
        if any(f.get("required") and not dest["settings"].get(f["key"]) for f in a.fields):
            return False
        if a.secret_label and not a.secret_optional:
            return bool(self.secret(dest))
        return True

    def view(self, dest: dict) -> dict:
        a = ADAPTERS[dest["kind"]]
        return {**dest, "connected": self.connected(dest), "has_secret": bool(a.secret_label and self.secret(dest)),
                "needs_secret": bool(a.secret_label and not a.secret_optional), "kind_label": a.label,
                "drafts": a.drafts, "publishes": a.publishes, "stats": a.stats, "comments": a.comments}

    # -- CRUD
    def _settings(self, kind: str, value: Any, base: dict | None = None) -> dict:
        a = ADAPTERS[kind]
        if value is not None and not isinstance(value, dict):
            raise DestinationError("settings must be an object.", 400)
        value = value or {}
        unknown = [k for k in value if k not in {f["key"] for f in a.fields}]
        if unknown:
            raise DestinationError(f"Unknown setting: {', '.join(map(str, unknown))[:60]}.", 400)
        out = dict(base or {})
        for f in a.fields:
            k = f["key"]
            if k in value:
                v = value[k]
                if f["type"] == "url":
                    out[k] = clean_url(v, required=f.get("required", False), label=f["label"])
                elif f["type"] == "path":
                    out[k] = clean(v, 300)
                else:
                    out[k] = clean(v, 120)
            if f.get("required") and not out.get(k) and (base is None or k in value):
                raise DestinationError(f"{f['label']} is needed for {a.label}.", 400)
        return out

    @staticmethod
    def _label(value: Any) -> str:
        v = clean(value, 60)
        if not v:
            raise DestinationError("Give the destination a name.", 400)
        return v

    def create(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise DestinationError("Send the kind and the name of the destination.", 400)
        kind = body.get("kind")
        if not isinstance(kind, str) or kind not in ADAPTERS:
            raise DestinationError("kind must be one of: " + ", ".join(ADAPTERS) + ".", 400)
        a = ADAPTERS[kind]
        with self.lock:
            d = self.load()
            if len(d["items"]) >= MAX_DESTINATIONS:
                raise DestinationError(f"At most {MAX_DESTINATIONS} destinations.", 400)
            label = self._label(body.get("label"))
            if any(r["label"].lower() == label.lower() for r in d["items"]):
                raise DestinationError("A destination with that name already exists.", 409)
            base_id = slugify(label, 30)
            ids = {r["id"] for r in d["items"]}
            new_id = base_id if base_id not in ids else f"{base_id}-{uuid.uuid4().hex[:6]}"
            row = {"id": new_id, "kind": kind, "label": label, "enabled": body.get("enabled") is not False,
                   "settings": self._settings(kind, body.get("settings")),
                   "format": clean_format(body.get("format"), a.format)}
            if body.get("enabled") is not None and not isinstance(body.get("enabled"), bool):
                raise DestinationError("enabled must be true or false.", 400)
            d["items"].append(row)
            self._save(d)
        if body.get("secret"):
            try:
                self.set_secret(new_id, body["secret"])
            except DestinationError:
                self.delete(new_id)
                raise
        return self.view(self._full(row))

    def update(self, dest_id: str, body: Any) -> dict:
        if not isinstance(body, dict):
            raise DestinationError("Send the fields to change.", 400)
        allowed = {"label", "enabled", "settings", "format", "secret", "clear_secret"}
        unknown = [k for k in body if k not in allowed]
        if unknown:
            raise DestinationError(f"Unknown field: {', '.join(unknown)[:60]}.", 400)
        with self.lock:
            d = self.load()
            row = next((r for r in d["items"] if r["id"] == dest_id), None)
            if row is None:
                raise DestinationError("There is no destination with that id.", 404)
            a = ADAPTERS[row["kind"]]
            if "label" in body:
                label = self._label(body["label"])
                if any(r["label"].lower() == label.lower() and r["id"] != dest_id for r in d["items"]):
                    raise DestinationError("A destination with that name already exists.", 409)
                row["label"] = label
            if "enabled" in body:
                if not isinstance(body["enabled"], bool):
                    raise DestinationError("enabled must be true or false.", 400)
                row["enabled"] = body["enabled"]
            if "settings" in body:
                row["settings"] = self._settings(row["kind"], body["settings"], row.get("settings") or {})
            if "format" in body:
                row["format"] = clean_format(body["format"], {**a.format, **(row.get("format") or {})})
            self._save(d)
        if body.get("secret"):
            self.set_secret(dest_id, body["secret"])
        if body.get("clear_secret") is True:
            self.clear_secret(dest_id)
        return self.view(self.get(dest_id))

    def delete(self, dest_id: str) -> None:
        with self.lock:
            d = self.load()
            if not any(r["id"] == dest_id for r in d["items"]):
                raise DestinationError("There is no destination with that id.", 404)
            d["items"] = [r for r in d["items"] if r["id"] != dest_id]
            self._save(d)
        self.clear_secret(dest_id)

    def reset(self) -> list[dict]:
        """Back to the starter list. Keys of the starter entries stay; keys of removed custom entries are deleted."""
        with self.lock:
            old = self.load()["items"]
            starter = _starter()
            keep = {r["id"] for r in starter}
            self._save({"version": 1, "items": starter})
        for r in old:
            if r["id"] not in keep:
                self.clear_secret(r["id"])
        return self.all()

    # -- calls (the engine decides WHEN; these just do it)
    def _need(self, dest: dict) -> tuple[Adapter, str]:
        a = ADAPTERS[dest["kind"]]
        if not self.connected(dest):
            raise DestinationError(f"Finish setting up {dest['label']} first (Posts, Destinations).", 409)
        return a, self.secret(dest)

    async def test(self, dest_id: str) -> dict:
        dest = self.get(dest_id)
        a = ADAPTERS[dest["kind"]]
        if any(f.get("required") and not dest["settings"].get(f["key"]) for f in a.fields):
            raise DestinationError("Fill in the required fields first.", 409)
        if a.secret_label and not a.secret_optional and not self.secret(dest):
            raise DestinationError(f"Save the {a.secret_label} first.", 409)
        return await a.test(dest, self.secret(dest), self.http)

    async def save_draft(self, dest: dict, post: dict, target: dict) -> dict:
        a, secret = self._need(dest)
        if not a.drafts:
            raise DestinationError(f"{dest['label']} has no drafts. {'Copy the text and tap I posted.' if a.mode == 'manual' else 'Publish sends it directly.'}", 400)
        return await a.save_draft(dest, secret, post, target, self.http)

    async def publish_remote(self, dest: dict, post: dict, target: dict) -> dict:
        """The ONLY place an adapter's publish is called. Reached from Posts.publish (explicit confirm) only."""
        a, secret = self._need(dest)
        if not a.publishes:
            raise DestinationError(f"{dest['label']} is posted by hand: copy the text, post it, then tap I posted.", 400)
        return await a.publish(dest, secret, post, target, self.http)

    async def stats(self, dest: dict, known: dict, baselined: bool) -> tuple[list[dict], list[dict]]:
        a, secret = self._need(dest)
        return await a.fetch_stats(dest, secret, self.http, known, baselined)
