"""FORMFILL: pre-fill a web application form in a browser on the laptop, then STOP for the owner's tap. See docs/api/formfill.md.

Flow: the owner taps "Fill the form on my laptop" on a ready web item. A Chromium window (Playwright, optional
dependency) opens the application page, fills what the owner's profile and draft safely answer, lists what it did not
fill ("needs you"), and keeps a screenshot. NOTHING is submitted until the owner looks at that screenshot and taps
Submit; the click only happens if the page and the filled values are unchanged since the screenshot they saw.

Rules that are code, not policy:
- Only public https:// pages (the research SSRF rules; localhost only through the test-only `allow_local` switch) and not
  on the owner's never list (default: linkedin.com).
- Fields that are sensitive (ID numbers, date of birth, demographics, bank, passwords, legal consent) or unknown are never
  filled; they are listed so the owner does it. `[FILL: x]` markers in a draft are never typed into a page.
- Cookies and passwords stay in the browser profile on this laptop (`<data>/formfill_profile`). No code here reads, exports
  or sends them. Captchas, logins and 2FA are never solved or bypassed; there is no stealth or evasion code.
- One session at a time, 5 minutes idle closes it, 20 s between starts, the daily cap is applyq_daily_cap.
- Page text is data from outside. It is never sent to a model, and audit entries hold the host and item id only.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import importlib.util
import ipaddress
import json
import logging
import re
import secrets
import shutil
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse, Response

from . import applyq as aq
from . import research
from .common import Ctx
from . import panic as _panic

log = logging.getLogger("hub.formfill")

IDLE_SECONDS = 300.0           # a form nobody touches for 5 minutes is closed
STALE_SECONDS = 1800.0         # a run older than this cannot be submitted, and its screenshot is deleted
LOGIN_SECONDS = 600.0          # a login window stays open this long (the owner is typing in it, not calling the hub)
MIN_GAP_SECONDS = 20.0         # between two form starts
MAX_HTML = 2_000_000
MAX_DEPTH = 200
DEFAULT_NEVER = ("linkedin.com",)
INSTALL_HINT = "pip install playwright, then python -m playwright install chromium"
_RUN_RE = re.compile(r"^[0-9a-f]{16}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?\.[a-z]{2,24}$")


class FormfillError(Exception):
    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


# ---- the URL rule ----------------------------------------------------------------------------------------------

def url_problem(url: str, *, resolver: Callable[[str], list[str]] | None = None, allow_local: bool = False) -> str:
    """"" when the page may be opened, else one plain reason. Public https hosts only. This reuses the research SSRF
    rules (private, loopback, link-local, tailnet, reserved ranges and local names are refused after DNS resolution).
    `allow_local` exists for the tests only: it lets http and loopback through and nothing else."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "That is not a valid address."
    if parts.scheme != "https" and not (allow_local and parts.scheme == "http"):
        return "Only https:// pages can be filled."
    if not host:
        return "The address has no host."
    if parts.username or parts.password:
        return "Addresses with a login inside are not opened."
    if allow_local:
        try:
            if ipaddress.ip_address(host.strip("[]")).is_loopback:
                return ""
        except ValueError:
            pass
    if host == "localhost" or ("." not in host and not research._is_ip(host)) or host.endswith(research.BLOCKED_SUFFIXES):
        return "That is a private or local name."
    if research._is_ip(host):
        addresses = [host.strip("[]")]
    else:
        try:
            addresses = (resolver or research.default_resolver)(host)
        except (OSError, UnicodeError):
            return "The host could not be resolved."
    if not addresses:
        return "The host could not be resolved."
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(str(raw).split("%")[0])
        except ValueError:
            return "The host resolved to something unusable."
        if research.ip_blocked(ip):
            return "That address is private, local or not public."
    return ""


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def never_match(host: str, never: list[str]) -> str:
    host = (host or "").lower()
    for d in never:
        if host == d or host.endswith("." + d):
            return d
    return ""


# ---- reading a page: a small tolerant HTML tree ----------------------------------------------------------------

VOID = frozenset("input br img hr meta link source wbr col area base embed param track".split())
SKIP = frozenset({"script", "style", "head", "template", "noscript", "svg"})
IMPLIED = {"option": {"option"}, "p": {"p"}, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}}
BUTTON_INPUTS = frozenset({"submit", "button", "image", "reset"})
SKIP_INPUTS = frozenset({"hidden", "submit", "button", "image", "reset"})


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "tok")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None"):
        self.tag, self.attrs, self.children, self.parent, self.tok = tag, attrs, [], parent, -1

    def get(self, name: str) -> str:
        return (self.attrs.get(name) or "").strip()


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        implied = IMPLIED.get(tag)
        if implied and self.cur.tag in implied and self.cur.parent is not None:
            self.cur, self.depth = self.cur.parent, self.depth - 1
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID and self.depth < MAX_DEPTH:
            self.cur, self.depth = node, self.depth + 1

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        n, d = self.cur, 0
        while n is not None and n.tag != tag:
            n, d = n.parent, d + 1
        if n is not None and n.parent is not None:
            self.cur, self.depth = n.parent, max(0, self.depth - d - 1)

    def handle_data(self, data):
        if data:
            self.cur.children.append(data)


def build_tree(html: str) -> Node:
    b = _Builder()
    try:
        b.feed((html or "")[:MAX_HTML])
        b.close()
    except Exception:  # noqa: BLE001 - a broken page is read as far as it goes
        log.debug("formfill: html parse stopped early")
    return b.root


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", str(text or "").lower().replace("'", "")).split())


def _own_text(node: Node, *, stop: Node | None = None) -> str:
    """Visible text under node, without scripts, option lists, textareas or the control `stop` itself."""
    out: list[str] = []

    def walk(n: Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                out.append(c)
            elif c is stop or c.tag in SKIP or c.tag in ("select", "textarea", "option"):
                continue
            elif c.tag in ("input",):
                continue
            else:
                walk(c)
    walk(node)
    return " ".join(" ".join(out).split())


def _flatten(root: Node) -> list[tuple]:
    """Document order tokens: ("t", text, in_label) and ("el", node). Sets node.tok for controls."""
    tokens: list[tuple] = []

    def walk(n: Node, in_label: bool) -> None:
        for c in n.children:
            if isinstance(c, str):
                t = " ".join(c.split())
                if t:
                    tokens.append(("t", t, in_label))
                continue
            if c.tag in SKIP:
                continue
            if c.tag in ("input", "select", "textarea", "button"):
                c.tok = len(tokens)
                tokens.append(("el", c))
                if c.tag == "button":
                    walk(c, True)
                continue
            if c.tag == "option":
                continue
            walk(c, in_label or c.tag == "label")
    walk(root, False)
    return tokens


def _ids(root: Node) -> tuple[dict[str, Node], dict[str, list[Node]]]:
    by_id: dict[str, Node] = {}
    labels: dict[str, list[Node]] = {}

    def walk(n: Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                continue
            if c.attrs.get("id"):
                by_id.setdefault(c.attrs["id"], c)
            if c.tag == "label" and c.attrs.get("for"):
                labels.setdefault(c.attrs["for"], []).append(c)
            walk(c)
    walk(root)
    return by_id, labels


def _humanize(name: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "")
    s = re.sub(r"[\[\]_\-.:]+", " ", s)
    return " ".join(s.split())


def _clip(text: str, n: int = 160) -> str:
    return " ".join(str(text or "").split())[:n]


def _is_visible_flag(node: Node, attr: str = "data-rl-vis") -> bool:
    v = node.attrs.get(attr)
    if v is not None:
        return v != "0"
    if node.attrs.get("hidden") is not None or node.get("aria-hidden") == "true":
        return False
    style = re.sub(r"\s+", "", node.get("style").lower())
    return not ("display:none" in style or "visibility:hidden" in style)


def parse_page(html: str) -> dict:
    """Pure. -> {fields, buttons, text, tree markers}. Each field: idx, tag, type, name, id, label, signals[], required,
    disabled, readonly, visible, maxlength, autocomplete, placeholder, options[{idx,value,label,checked}] (select, radio).
    Radios with one name are one field. Controls get `data-rl-idx` from the live page; fixtures without it are numbered
    in document order."""
    root = build_tree(html)
    tokens = _flatten(root)
    by_id, labels_for = _ids(root)
    fields: list[dict] = []
    buttons: list[dict] = []
    groups: dict[str, dict] = {}
    counter = {"f": 0, "b": 0}

    def ancestors(n: Node):
        p = n.parent
        while p is not None:
            yield p
            p = p.parent

    def explicit_label(c: Node) -> str:
        texts = [_own_text(l, stop=c) for l in labels_for.get(c.get("id"), [])] if c.get("id") else []
        for a in ancestors(c):
            if a.tag == "label":
                texts.append(_own_text(a, stop=c))
                break
        ref = c.get("aria-labelledby")
        if ref:
            texts.append(" ".join(_own_text(by_id[i], stop=c) for i in ref.split() if i in by_id))
        texts.append(c.get("aria-label"))
        return _clip(" ".join(t for t in texts if t))

    def preceding_text(c: Node) -> str:
        i = c.tok - 1
        while i >= 0:
            kind = tokens[i]
            if kind[0] == "el":
                return ""
            if not kind[2]:
                return _clip(kind[1][-120:])
            i -= 1
        return ""

    def following_text(c: Node) -> str:
        i = c.tok + 1
        if i < len(tokens) and tokens[i][0] == "t":
            return _clip(tokens[i][1][:120])
        return ""

    def group_label(c: Node) -> str:
        for a in ancestors(c):
            if a.tag == "fieldset":
                for ch in a.children:
                    if not isinstance(ch, str) and ch.tag == "legend":
                        return _clip(_own_text(ch))
            if a.get("role") in ("radiogroup", "group"):
                lab = a.get("aria-label")
                ref = a.get("aria-labelledby")
                if ref:
                    lab = " ".join(_own_text(by_id[i]) for i in ref.split() if i in by_id) or lab
                if lab:
                    return _clip(lab)
        return ""

    def scan(n: Node) -> None:
        for c in n.children:
            if isinstance(c, str) or c.tag in SKIP:
                continue
            if c.tag in ("input", "textarea", "select"):
                add_field(c)
            elif c.tag == "button" or (c.get("role") == "button" and c.tag != "input"):
                add_button(c, _own_text(c) or c.get("aria-label") or c.get("title"), c.get("type") or ("submit" if c.tag == "button" else "button"))
                scan(c) if c.tag != "button" else None
                continue
            scan(c)

    def in_form(c: Node) -> bool:
        return any(a.tag == "form" for a in ancestors(c))

    def add_button(c: Node, text: str, btype: str) -> None:
        idx_attr = c.attrs.get("data-rl-btn")
        idx = int(idx_attr) if idx_attr and idx_attr.isdigit() else counter["b"]
        counter["b"] += 1
        buttons.append({"idx": idx, "text": _clip(text, 80), "type": btype.lower(), "in_form": in_form(c),
                        "disabled": c.attrs.get("disabled") is not None or c.get("aria-disabled") == "true",
                        "visible": _is_visible_flag(c, "data-rl-bvis")})

    def add_field(c: Node) -> None:
        idx_attr = c.attrs.get("data-rl-idx")
        idx = int(idx_attr) if idx_attr and idx_attr.isdigit() else counter["f"]
        counter["f"] += 1
        tag = c.tag
        ftype = (c.get("type") or "text").lower() if tag == "input" else tag
        if tag == "input" and ftype in BUTTON_INPUTS:
            if ftype != "hidden":
                add_button(c, c.get("value") or c.get("alt") or c.get("aria-label"), ftype)
            return
        if ftype == "hidden":
            return
        expl = explicit_label(c)
        near = "" if expl else preceding_text(c)
        base = {"idx": idx, "tag": tag, "type": ftype, "name": c.get("name"), "id": c.get("id"),
                "placeholder": _clip(c.get("placeholder")), "autocomplete": c.get("autocomplete").lower(),
                "required": c.attrs.get("required") is not None or c.get("aria-required") == "true",
                "disabled": c.attrs.get("disabled") is not None, "readonly": c.attrs.get("readonly") is not None,
                "visible": _is_visible_flag(c), "maxlength": int(c.get("maxlength")) if c.get("maxlength").isdigit() else 0,
                "multiple": c.attrs.get("multiple") is not None, "options": []}
        if re.search(r"(\*|\(required\))\s*$", expl or near, re.I):
            base["required"] = True
        humanized = _humanize(c.get("name") or c.get("id"))
        if ftype in ("radio", "checkbox"):
            own = expl or following_text(c) or near or _humanize(c.get("value"))
            opt = {"idx": idx, "value": c.get("value") or "on", "label": own, "checked": c.attrs.get("checked") is not None}
            if ftype == "radio" and c.get("name"):
                key = "radio:" + c.get("name")
                g = groups.get(key)
                if g is None:
                    lab = group_label(c) or near or ""
                    g = groups[key] = {**base, "label": lab, "signals": [], "_own": []}
                    fields.append(g)
                g["options"].append(opt)
                g["required"] = g["required"] or base["required"]
                g["_own"].append(own)
                g["visible"] = g["visible"] or base["visible"]
                g["disabled"] = g["disabled"] and base["disabled"]
                return
            base.update(label=own, options=[opt], signals=[own, group_label(c), humanized])
            fields.append(base)
            return
        if tag == "select":
            base["options"] = [{"idx": idx, "value": o.get("value") if o.attrs.get("value") is not None else _own_text(o),
                                "label": _own_text(o), "checked": o.attrs.get("selected") is not None, "disabled": o.attrs.get("disabled") is not None}
                               for o in _walk_options(c)]
        label = expl or near or base["placeholder"] or humanized
        signals = [s for s in dict.fromkeys([expl, near, base["placeholder"], humanized]) if s]
        base.update(label=_clip(label), signals=signals)
        fields.append(base)

    def _walk_options(sel: Node):
        for ch in sel.children:
            if isinstance(ch, str):
                continue
            if ch.tag == "option":
                yield ch
            elif ch.tag == "optgroup":
                yield from _walk_options(ch)
    scan(root)
    for f in fields:
        f.pop("_own", None)
        if f["type"] == "radio":
            f["signals"] = [s for s in dict.fromkeys([f["label"], f["placeholder"], _humanize(f["name"])]) if s]
            if not f["label"]:
                f["label"] = _humanize(f["name"]) or "(unlabelled choice)"
        elif not f.get("label"):
            f["label"] = "(unlabelled field)"
    visible_text = _own_text(root)
    return {"fields": fields, "buttons": buttons, "text": visible_text, "root": root}


# ---- blockers, steps, buttons ----------------------------------------------------------------------------------

CAPTCHA_TEXT = re.compile(r"verify (that )?you are (a )?human|i.?m not a robot|i am not a robot|are you a robot|unusual traffic|"
                          r"press (and|&) hold|checking your browser", re.I)
BLOCK_TEXT = re.compile(r"access denied|you have been blocked|request blocked|automated (access|traffic|requests)|bot (detected|traffic)|"
                        r"forbidden", re.I)
LOGIN_URL = re.compile(r"/(log-?in|sign-?in|sso|authenticate|auth)(/|$|\?)", re.I)
LOGIN_TEXT = re.compile(r"sign in to (continue|apply)|log ?in to (continue|apply)|create an account to apply|sign in or create", re.I)
STEP_RE = [re.compile(r"\bstep\s+(\d{1,2})\s*(?:of|/)\s*(\d{1,2})\b", re.I), re.compile(r"\b(\d{1,2})\s+of\s+(\d{1,2})\s+steps?\b", re.I),
           re.compile(r"\bpage\s+(\d{1,2})\s+of\s+(\d{1,2})\b", re.I)]


def detect_blockers(page: dict, url: str = "", status: int | None = None) -> list[dict]:
    out: list[dict] = []
    root: Node = page["root"]
    text = page["text"]
    fields = [f for f in page["fields"] if f["visible"]]
    captcha = bool(CAPTCHA_TEXT.search(text))

    def walk(n: Node) -> None:
        nonlocal captcha
        for c in n.children:
            if isinstance(c, str):
                continue
            cls = c.get("class").lower()
            if any(k in cls.split() or k in cls for k in ("g-recaptcha", "h-captcha", "cf-turnstile")):
                captcha = True
            if c.tag == "iframe" and re.search(r"captcha|turnstile|arkoselabs|funcaptcha|challenges\.cloudflare", c.get("src").lower()):
                captcha = True
            walk(c)
    walk(root)
    if captcha:
        out.append({"kind": "captcha", "message": "The page shows a captcha or a 'verify you are human' check. I never solve those: do it yourself in the laptop window."})
    passwords = [f for f in fields if f["type"] == "password"]
    others = [f for f in fields if f["type"] not in ("password", "checkbox", "radio")]
    if passwords or (LOGIN_TEXT.search(text) and len(others) <= 2) or (LOGIN_URL.search(urlsplit(url).path + "/") and len(others) <= 2):
        out.append({"kind": "login_required", "message": "The page wants you to sign in or create an account. Use 'Open a login window', log in yourself, then fill again."})
    if (status is not None and status >= 400) or (BLOCK_TEXT.search(text) and len(fields) <= 1):
        out.append({"kind": "blocked", "message": "The site refused automated access. Copy the answers and apply yourself."})
    if not fields and not any(b["kind"] == "blocked" for b in out if "kind" in b):
        out.append({"kind": "no_form", "message": "I found no form fields on the visible page. Maybe it needs a click on 'Apply' first; do that in the laptop window and tap Refresh."})
    return out


def wizard_info(page: dict, buttons: dict) -> dict:
    cur = total = None
    for rx in STEP_RE:
        m = rx.search(page["text"])
        if m and int(m.group(2)) >= int(m.group(1)) >= 1:
            cur, total = int(m.group(1)), int(m.group(2))
            break
    return {"current": cur, "total": total, "has_next": buttons.get("next") is not None}


NEXT_RE = re.compile(r"^(next|continue|next step|save and continue|save and next|proceed|continue to .{2,30}|next .{2,30})$")
SUBMIT_STRONG = re.compile(r"^(submit( my| your| the)? application|submit|send( my)? application|complete application|finish( application)?|"
                           r"apply for (this|the) (job|position|role)|apply now|apply|confirm and submit|submit and apply)$")
CONTROL_EXCLUDE = re.compile(r"cancel|back|previous|reset|clear|search|log ?in|sign ?in|sign ?up|register|save draft|save for later|upload|attach|"
                             r"remove|delete|share|preview|edit|autofill|apply with|linkedin|indeed|google|import|use my|add another|add more|"
                             r"download|print|subscribe|newsletter|menu|close|dismiss|accept cookies|cookie|reject")


def find_controls(buttons: list[dict]) -> dict:
    """Pure. -> {submit: button|None, next: button|None, ambiguous: bool}. Only visible, enabled controls count."""
    submit_score: list[tuple[int, dict]] = []
    nxt = None
    for b in buttons:
        if not b["visible"] or b["disabled"]:
            continue
        t = norm(b["text"])
        if not t or CONTROL_EXCLUDE.search(t) or b["type"] == "reset":
            continue
        if NEXT_RE.match(t):
            nxt = nxt or b
            continue
        if SUBMIT_STRONG.match(t) and t not in ("apply", "apply now") or t in ("apply", "apply now") and b["type"] == "submit":
            submit_score.append((3, b))
        elif b["type"] == "submit" and b["in_form"]:
            submit_score.append((2, b))
        elif "submit" in t.split() or "apply" in t.split():
            submit_score.append((1, b))
    submit, ambiguous = None, False
    if submit_score:
        top = max(s for s, _ in submit_score)
        best = [b for s, b in submit_score if s == top]
        if len(best) == 1:
            submit = best[0]
        else:
            ambiguous = True
    return {"submit": submit, "next": nxt, "ambiguous": ambiguous}


SUCCESS_TEXT = re.compile(r"thank you for (applying|your application|submitting)|thanks for (applying|your application)|"
                          r"application (has been |was |is )?(received|submitted|sent|complete)|we(?:'|’)?ve received your application|"
                          r"we have received your application|successfully (submitted|applied|sent)|you(?:'|’)?ve applied|"
                          r"your application (is|has been) (in|received|submitted)", re.I)
SUCCESS_URL = re.compile(r"/(thank|thanks|thankyou|thank-you|confirm|confirmation|success|submitted|applied|complete)(/|$|\?|-|\.)", re.I)


def judge_result(before_url: str, after_url: str, text: str, remaining_fields: int) -> str:
    """"applied" when the page says so, else "needs_check". Never "applied" on silence."""
    if SUCCESS_TEXT.search(text or ""):
        return "applied"
    if after_url != before_url and SUCCESS_URL.search(urlsplit(after_url).path + "/") and remaining_fields == 0:
        return "applied"
    return "needs_check"


# ---- facts about the owner -------------------------------------------------------------------------------------

STOP = frozenset("a an the of for to in on at is are do does you your please enter provide what which how any if and or with be can "
                 "would will have has period details info information describe tell us me my our this that".split())
CONCEPTS = {
    "rate": {"rate", "salary", "compensation", "ctc", "pay", "remuneration", "expectation", "expectations", "expected", "hourly"},
    "notice": {"notice", "availability", "available", "joining", "join", "start", "starting"},
    "relocate": {"relocate", "relocation", "relocating"},
    "remote": {"remote", "onsite", "hybrid", "wfh"},
    "years": {"years", "year", "yrs"},
    "linkedin": {"linkedin"},
}
_CONCEPT_OF = {w: k for k, ws in CONCEPTS.items() for w in ws}
SCALE_GUARD = re.compile(r"rate your|rating|scale of|out of \d|how would you rate|on a scale|1 to \d|1 5|1 10")


def concept_set(text: str) -> frozenset:
    out = set()
    for w in norm(text).split():
        if w in STOP:
            continue
        if len(w) > 3 and w.endswith("s") and w not in _CONCEPT_OF:
            w = w[:-1]
        out.add(_CONCEPT_OF.get(w, w))
    return frozenset(out)


def _first(d: dict, names: tuple[str, ...]) -> str:
    keyed = {norm(k): v for k, v in d.items()}
    for n in names:
        v = keyed.get(n)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
            return str(v).strip()
    return ""


def _years_from(experience: Any, this_year: int) -> str:
    starts, ends = [], []
    for e in experience if isinstance(experience, list) else []:
        if not isinstance(e, dict):
            continue
        blob = " ".join(str(e.get(k) or "") for k in ("period", "dates", "start", "end"))
        years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", blob)]
        if not years:
            continue
        starts.append(min(years))
        ends.append(this_year if re.search(r"present|current|now|ongoing|today", blob, re.I) else max(years))
    if not starts:
        return ""
    n = max(ends) - min(starts)
    return str(n) if 0 <= n <= 60 else ""


def current_job(experience: Any) -> tuple[str, str]:
    for e in experience if isinstance(experience, list) else []:
        if isinstance(e, dict) and re.search(r"present|current|now|ongoing|today", " ".join(str(e.get(k) or "") for k in ("period", "dates", "end")), re.I):
            return str(e.get("title") or "").strip(), str(e.get("company") or "").strip()
    return "", ""


def build_facts(profile: dict, draft: dict | None, *, this_year: int, resume: dict | None = None) -> dict:
    """What may be typed into a form, each with where it came from. Only profile and draft; nothing is invented."""
    profile = profile if isinstance(profile, dict) else {}
    draft = draft if isinstance(draft, dict) else {}
    std = profile.get("standard_answers") if isinstance(profile.get("standard_answers"), dict) else {}
    answers = {**std, **(draft.get("answers") if isinstance(draft.get("answers"), dict) else {})}
    facts: dict[str, dict] = {}

    def put(key: str, value: str, source: str) -> None:
        if isinstance(value, str) and value.strip() and key not in facts:
            facts[key] = {"value": value.strip(), "source": source}
    name = aq.clean(str(profile.get("name") or ""), 120)
    put("full_name", name, "profile name")
    parts = name.split()
    if len(parts) == 2:
        put("first_name", parts[0], "profile name")
        put("last_name", parts[1], "profile name")
    put("first_name", _first(answers, ("first name", "given name")), "standard answer")
    put("last_name", _first(answers, ("last name", "surname", "family name")), "standard answer")
    put("email", _first({**answers, **{k: profile.get(k) for k in ("email",) if profile.get(k)}}, ("email", "email address", "e mail")), "standard answer")
    put("phone", _first({**answers, **{k: profile.get(k) for k in ("phone",) if profile.get(k)}}, ("phone", "phone number", "mobile", "mobile number", "telephone")), "standard answer")
    put("location", _first({**answers, **{k: profile.get(k) for k in ("location",) if profile.get(k)}}, ("location", "city", "current location", "based in")), "standard answer")
    links = profile.get("links")
    pairs = list(links.items()) if isinstance(links, dict) else [("", l) for l in links] if isinstance(links, list) else []
    for label, url in pairs:
        url = str(url or "").strip()
        h, lab = host_of(url if "//" in url else "https://" + url), norm(label)
        if not url:
            continue
        full = url if "//" in url else "https://" + url
        if "linkedin.com" in h or lab == "linkedin":
            put("linkedin", full, "profile link")
        elif "github.com" in h or lab == "github":
            put("github", full, "profile link")
        else:
            put("portfolio", full, "profile link")
    put("headline", aq.clean(str(profile.get("headline") or ""), 200), "profile headline")
    put("about_you", aq.clean(str(profile.get("summary") or ""), 1500), "profile summary")
    title, company = current_job(profile.get("experience"))
    put("current_title", _first(answers, ("current title", "current job title", "current position", "current role")) or title, "profile")
    put("current_company", _first(answers, ("current company", "current employer")) or company, "profile")
    yrs = _first(answers, ("years of experience", "experience years", "total experience", "years experience"))
    put("years_experience", yrs or _years_from(profile.get("experience"), this_year), "standard answer" if yrs else "derived from your resume dates")
    left = aq.unresolved(draft) if draft else []
    if draft and not left:
        put("cover_letter", str(draft.get("body") or ""), "your application draft")
        put("why_us", str(draft.get("cover_note") or draft.get("body") or ""), "your application draft")
    facts["_unresolved"] = {"value": ", ".join(left), "source": "draft"} if left else {"value": "", "source": ""}
    if resume:
        facts["resume"] = resume
    facts["_answers"] = {"value": answers, "source": "standard answers"}
    return facts


# ---- classifying and planning ----------------------------------------------------------------------------------

SENSITIVE = [
    ("salary history", re.compile(r"salary history|current (salary|ctc|compensation|pay|package)|previous (salary|compensation|ctc)|last drawn|present salary|present ctc|existing (salary|ctc)")),
    ("an ID number", re.compile(r"social security|\bssn\b|national (id|insurance)|aadhaar|aadhar|\bpan (card|number|no)\b|passport|tax (id|number|identification)|\btin\b|driver s? licen[cs]e|"
                                r"\bid (number|no)\b|identification number|identity (card|number)|\bnin\b|document number|visa number")),
    ("date of birth", re.compile(r"date of birth|\bdob\b|birth ?date|birthday|born on|year of birth|\bage\b")),
    ("a demographic question", re.compile(r"gender|\bsex\b|ethnic|\brace\b|racial|disabilit|veteran|religio|sexual orientation|marital|pronoun|\bcaste\b|hispanic|latino|transgender")),
    ("a legal eligibility question", re.compile(r"citizen|nationality|work authori[sz]ation|authori[sz]ed to work|right to work|sponsorship|visa status|work permit|immigration|eligible to work|legally")),
    ("a financial detail", re.compile(r"\bbank\b|account number|\biban\b|routing|sort code|\bswift\b|credit card|card number|\bcvv\b|\bcvc\b|expir")),
    ("a password or code", re.compile(r"password|passcode|\botp\b|verification code|\b2fa\b|security code|pin code|one time")),
    ("a criminal-record question", re.compile(r"convict|criminal|felony|arrest|background check")),
]
CONSENT = re.compile(r"\bagree|consent|terms|privacy (policy|notice|statement)|certify|i confirm|i declare|acknowledge|authori[sz]e|accept|attest|gdpr|"
                     r"data processing|i understand|i have read|i am over|i hereby|true and (accurate|correct)|truthful")

# (key, pattern, priority, exclusion). Higher priority wins; two matches at the same top priority are ambiguous.
RULES: list[tuple[str, re.Pattern, int, re.Pattern | None]] = [
    ("first_name", re.compile(r"first name|given name|forename|\bfname\b"), 4, None),
    ("last_name", re.compile(r"last name|surname|family name|\blname\b"), 4, None),
    ("full_name", re.compile(r"^((your|full|legal|candidate|applicant|preferred) )?name$|full name|legal name|your name|candidate name|applicant name|^name \d?$"), 2,
     re.compile(r"company|employer|organi|school|university|college|reference|referr|manager|supervisor|file|user name|username|business|job|position|project|emergency|spouse|parent")),
    ("email", re.compile(r"\be ?mail\b|\bemail\b"), 4, re.compile(r"reference|referr|manager|supervisor|emergency|friend")),
    ("phone", re.compile(r"phone|mobile|telephone|\bcell\b|contact number|whatsapp"), 4, re.compile(r"country code|dial code|reference|referr|emergency|extension|\bext\b")),
    ("location", re.compile(r"current location|\blocation\b|\bcity\b|where are you based|where do you live|\btown\b|based in"), 3,
     re.compile(r"relocat|office|preferred location|willing|work location|desired location")),
    ("linkedin", re.compile(r"linked ?in"), 5, None),
    ("github", re.compile(r"git ?hub"), 5, None),
    ("portfolio", re.compile(r"portfolio|personal (web ?)?site|personal url|your website|\bwebsite\b|\bblog\b|work samples?|other (url|link)s?"), 3, re.compile(r"company|employer")),
    ("headline", re.compile(r"headline|tagline"), 4, None),
    ("about_you", re.compile(r"tell us about yourself|about (you|yourself)|introduce yourself|professional summary|short bio|\bsummary\b|\bbio\b"), 3, None),
    ("current_title", re.compile(r"current (job |position |role )?(title|position|role|designation)|present (job )?(title|position)|current designation"), 4, None),
    ("current_company", re.compile(r"current (company|employer|organi[sz]ation|workplace)|present (company|employer)|most recent (company|employer)|^(company|employer|organi[sz]ation)( name)?$"), 4, None),
    ("years_experience", re.compile(r"years? of (professional |relevant |total |work |industry )?experience|experience (in )?years|total experience|how many years"), 4, None),
    ("resume", re.compile(r"resume|r[eé]sum[eé]|\bcv\b|curriculum vitae"), 5, re.compile(r"cover letter")),
    ("cover_letter", re.compile(r"cover letter|covering letter|motivation(al)? letter|letter of (motivation|interest)|cover note"), 5, None),
    ("why_us", re.compile(r"why (do you want|would you like|are you interested|us\b|this (role|job|company|position)|do you want to (work|join))|what (excites|interests|attracts|motivates) you|"
                          r"why .{0,40}(join|work (for|at|with)|apply)|interest in (this|the|our)|message to (the )?(hiring|recruit)|note to (the )?(hiring|recruit)"), 4, None),
]
TEXTUAL = frozenset({"text", "email", "tel", "url", "number", "search", "textarea", ""})
FILL_TYPES = TEXTUAL | {"select", "radio"}
NOT_FILLABLE = frozenset({"date", "datetime-local", "month", "time", "week", "range", "color", "image"})


def sensitive_reason(field: dict) -> str:
    """The category when any signal of the field is sensitive, else "". Checked on every label signal, conservatively."""
    if field["type"] == "password":
        return "a password"
    for sig in field["signals"] or [field["label"]]:
        t = norm(sig)
        for reason, rx in SENSITIVE:
            if rx.search(t):
                return reason
        if field["type"] in ("checkbox", "radio") and CONSENT.search(t):
            return "legal consent"
    for opt in field["options"] if field["type"] == "checkbox" else []:
        if CONSENT.search(norm(opt["label"])):
            return "legal consent"
    if field["type"] == "date" and re.search(r"birth|dob", norm(" ".join(field["signals"]))):
        return "date of birth"
    return ""


AUTOCOMPLETE_KEYS = {"given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email", "tel": "phone",
                     "tel-national": "phone", "url": "portfolio", "organization-title": "current_title", "organization": "current_company"}


def match_key(field: dict) -> tuple[str | None, bool]:
    """(key, ambiguous). Autocomplete token first, then each label signal in order."""
    ac = [t for t in field["autocomplete"].split() if t not in ("section-", "shipping", "billing", "home", "work", "mobile")]
    for t in ac:
        if t in AUTOCOMPLETE_KEYS:
            return AUTOCOMPLETE_KEYS[t], False
    for sig in field["signals"]:
        t = norm(sig)
        hits = [(prio, key) for key, rx, prio, excl in RULES if rx.search(t) and not (excl and excl.search(t))]
        if not hits:
            continue
        top = max(p for p, _ in hits)
        best = sorted({k for p, k in hits if p == top})
        if len(best) > 1:
            return None, True
        return best[0], False
    return None, False


def match_option(value: str, options: list[dict]) -> dict | None:
    v = norm(value)
    if not v:
        return None
    usable = [o for o in options if not o.get("disabled") and (o["value"] != "" or norm(o["label"]))]
    for o in usable:
        if v in (norm(o["label"]), norm(o["value"])) and norm(o["label"]):
            return o
    lead = [o for o in usable if norm(o["label"]).split()[:1] == v.split()[:1] and v.split()[0] in ("yes", "no") and norm(o["label"])]
    if len(lead) == 1:
        return lead[0]
    loose = [o for o in usable if len(v) >= 3 and norm(o["label"]) and (v in norm(o["label"]) or (len(norm(o["label"])) >= 3 and norm(o["label"]) in v))]
    return loose[0] if len(loose) == 1 else None


def _answer_for(field: dict, answers: dict) -> tuple[str, str, bool]:
    """(answer, key, ambiguous) from standard answers by fuzzy concept match on the label."""
    for sig in field["signals"]:
        if SCALE_GUARD.search(norm(sig)):
            return "", "", False
    best: list[tuple[int, str, str]] = []
    for sig in field["signals"][:2]:
        lab = concept_set(sig)
        lab_norm = norm(sig)
        for key, val in answers.items():
            if not isinstance(val, (str, int, float)) or isinstance(val, bool) or not str(val).strip():
                continue
            ks = concept_set(str(key))
            if not ks:
                continue
            if norm(key) == lab_norm or ks <= lab:
                best.append((len(ks) + (10 if norm(key) == lab_norm else 0), str(key), str(val).strip()))
        if best:
            break
    if not best:
        return "", "", False
    top = max(s for s, _, _ in best)
    winners = [(k, v) for s, k, v in best if s == top]
    if len({v for _, v in winners}) > 1:
        return "", "", True
    return winners[0][1], winners[0][0], False


def _text_problem(field: dict, value: str) -> str:
    if field["type"] == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "The saved value does not look like an email address."
    if field["type"] == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
        return "This field wants a number and the saved answer is not one."
    if field["type"] == "url" and not re.match(r"https?://", value):
        return "This field wants a full web address."
    if field["maxlength"] and len(value) > field["maxlength"]:
        return f"My text is longer than the field allows ({field['maxlength']} characters). Write a shorter one."
    return ""


def plan_field(field: dict, facts: dict) -> dict:
    """Decide one field. -> {status: fill|needs_you|skipped, key, value, display, reason, action, target}. Pure."""
    base = {"idx": field["idx"], "label": field["label"], "type": field["type"], "required": field["required"],
            "key": "", "value": "", "display": "", "reason": "", "action": "", "target": field["idx"]}

    def out(status: str, reason: str = "", **kw) -> dict:
        return {**base, "status": status, "reason": reason, **kw}

    def unknown(why: str) -> dict:
        if field["required"]:
            return out("needs_you", why)
        return out("skipped", why + " (optional, left blank)")
    if field["disabled"] or field["readonly"]:
        return out("skipped", "The field is locked.")
    if not field["visible"]:
        return out("skipped", "Not visible on the page (never filled).")
    sens = sensitive_reason(field)
    if sens:
        return out("needs_you", f"This is {sens}. I never fill that; it is yours to answer.")
    ftype = field["type"]
    if ftype in NOT_FILLABLE:
        return unknown("I do not fill date, time or slider fields.")
    if ftype == "checkbox":
        return unknown("I never tick checkboxes for you.")
    if ftype not in FILL_TYPES and ftype != "file":
        return unknown("I do not know this kind of field.")
    key, ambiguous = match_key(field)
    if ambiguous:
        return out("needs_you", "The label could mean more than one thing, so I did not guess.")
    answers = facts.get("_answers", {}).get("value", {})
    value, source = "", ""
    if ftype == "file":
        if key == "resume":
            res = facts.get("resume")
            if not res:
                return out("needs_you", "I have no resume file. Put your resume next to the hub as formfill_resume.pdf or save your resume text in your profile.", key="resume")
            return out("fill", "", key="resume", value=res["value"], display=res["name"], action="upload")
        return unknown("I only upload your resume.")
    if key in ("cover_letter", "why_us") and ftype in TEXTUAL:
        if facts.get("_unresolved", {}).get("value"):
            return out("needs_you", "Your draft still has [FILL: " + facts["_unresolved"]["value"] + "] markers. Resolve them first.", key=key)
        fact = facts.get(key)
        if not fact:
            return out("needs_you", "There is no draft text to use.", key=key)
        value, source = fact["value"], fact["source"]
    elif key == "resume":
        return unknown("This asks for your resume as text or a link; I only upload the file.")
    elif key:
        fact = facts.get(key)
        if not fact or key in ("cover_letter", "why_us"):
            std, std_key, amb = _answer_for(field, answers)
            if std and not amb:
                value, source, key = std, "standard answer", key
            else:
                nice = key.replace("_", " ")
                return out("needs_you", f"I do not have your {nice}. Add it to your profile (standard answers).", key=key)
        else:
            value, source = fact["value"], fact["source"]
    else:
        std, std_key, amb = _answer_for(field, answers)
        if amb:
            return out("needs_you", "Two of your saved answers could fit this question and they differ.")
        if not std:
            return unknown("I do not have an answer for this in your profile.")
        value, source, key = std, f"standard answer '{std_key}'", "standard_answer"
    if ftype in ("select", "radio"):
        opt = match_option(value, field["options"])
        if opt is None:
            return out("needs_you", f"My answer '{_clip(value, 40)}' is not one of the choices.", key=key)
        return out("fill", "", key=key, value=opt["value"], display=_clip(opt["label"] or opt["value"], 120), action="select" if ftype == "select" else "choose",
                   target=opt["idx"])
    problem = _text_problem(field, value)
    if problem:
        return out("needs_you", problem, key=key)
    return out("fill", "", key=key, value=value, display=_clip(value, 160), action="text")


def plan_form(fields: list[dict], facts: dict) -> list[dict]:
    plans = []
    for n, f in enumerate(fields, 1):
        p = plan_field(f, facts)
        p["n"] = n
        plans.append(p)
    return plans


def digest_of(url: str, fields: list[dict], values: dict[int, str], submit: dict | None) -> str:
    """Hash of the page address, the step's field structure and every value currently in the page's fields (passwords are
    never read). If the owner or the page changes anything after the screenshot, the digest changes."""
    parts = urlsplit(url)
    shape = [[f["idx"], f["type"], f["name"], f["label"][:60]] for f in fields if f["visible"]]
    vals = sorted((int(k), str(v)) for k, v in values.items())
    blob = json.dumps([f"{parts.scheme}://{parts.netloc}{parts.path}", shape, vals, [submit["idx"], submit["text"]] if submit else None], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ---- the browser (Playwright, optional) --------------------------------------------------------------------------

STAMP_JS = """() => {
  const shown = (e, loose) => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden') return loose;
    if (loose) return true;
    return r.width > 0 && r.height > 0 && r.right + scrollX > 0 && r.bottom + scrollY > 0 && s.opacity !== '0';
  };
  let i = 0;
  document.querySelectorAll('input,textarea,select').forEach((e) => {
    const loose = ['checkbox', 'radio', 'file'].includes((e.type || '').toLowerCase());
    e.setAttribute('data-rl-idx', String(i++));
    e.setAttribute('data-rl-vis', shown(e, loose) ? '1' : '0');
  });
  let j = 0;
  document.querySelectorAll('button,input[type=submit],input[type=button],input[type=image],[role=button]').forEach((e) => {
    e.setAttribute('data-rl-btn', String(j++));
    e.setAttribute('data-rl-bvis', shown(e, false) ? '1' : '0');
  });
}"""
READ_JS = """() => {
  const out = {};
  document.querySelectorAll('[data-rl-idx]').forEach((e) => {
    const t = (e.type || '').toLowerCase();
    if (t === 'password') { out[e.getAttribute('data-rl-idx')] = ''; return; }
    out[e.getAttribute('data-rl-idx')] = (t === 'checkbox' || t === 'radio') ? (e.checked ? '1' : '0')
      : t === 'file' ? Array.from(e.files || []).map((f) => f.name).join(',') : String(e.value || '');
  });
  return out;
}"""


class Driver:
    """What the flow needs from a browser. Methods run on ONE worker thread (Playwright's sync API is thread-bound).
    The real implementation is PlaywrightDriver; tests use a fake with the same methods."""

    def open(self, url: str, headed: bool) -> int | None: raise NotImplementedError          # launch + navigate; HTTP status
    def url(self) -> str: raise NotImplementedError
    def html(self) -> str: raise NotImplementedError                                          # stamps controls, returns the page HTML
    def values(self) -> dict[int, str]: raise NotImplementedError
    def fill_text(self, idx: int, value: str) -> None: raise NotImplementedError
    def select(self, idx: int, value: str) -> None: raise NotImplementedError
    def choose(self, idx: int) -> None: raise NotImplementedError
    def upload(self, idx: int, path: str) -> None: raise NotImplementedError
    def click(self, idx: int) -> None: raise NotImplementedError
    def settle(self) -> None: raise NotImplementedError
    def screenshot(self) -> bytes: raise NotImplementedError
    def blocked_navigation(self) -> str: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError


def playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


class PlaywrightDriver(Driver):
    def __init__(self, profile_dir: Path, *, resolver=None, allow_local: bool = False, sync_factory=None, launch_options: dict | None = None):
        self.profile_dir, self.resolver, self.allow_local = Path(profile_dir), resolver, allow_local
        self._factory, self._launch_options = sync_factory, dict(launch_options or {})
        self._pw = self._ctx = self._page = None
        self._blocked = ""

    def _route(self, route, request) -> None:
        try:
            if request.is_navigation_request():
                reason = url_problem(request.url, resolver=self.resolver, allow_local=self.allow_local)
                if reason:
                    self._blocked = reason
                    route.abort("blockedbyclient")
                    return
            route.continue_()
        except Exception:  # noqa: BLE001 - a route that cannot answer must not crash the page
            try:
                route.abort()
            except Exception:  # noqa: BLE001
                pass

    def open(self, url: str, headed: bool) -> int | None:
        factory = self._factory
        if factory is None:
            try:
                from playwright.sync_api import sync_playwright as factory  # lazy: optional dependency
            except ImportError:
                raise FormfillError("Playwright is not installed. Run: " + INSTALL_HINT, 503) from None
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = factory().start()
        base = dict(user_data_dir=str(self.profile_dir), headless=not headed, viewport={"width": 1100, "height": 1500},
                    accept_downloads=False, **self._launch_options)
        ctx, last = None, ""
        for extra in ({}, {"channel": "chrome"}, {"channel": "msedge"}):       # bundled Chromium first, then the owner's own browser
            if "executable_path" in self._launch_options and extra:
                break
            try:
                ctx = self._pw.chromium.launch_persistent_context(**{**base, **extra})
                break
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
        if ctx is None:
            self.close()
            if "Executable doesn't exist" in last or "playwright install" in last or "not found" in last.lower():
                raise FormfillError("The browser is not installed. Run: python -m playwright install chromium", 503)
            raise FormfillError("The browser could not be started: " + _clip(last.splitlines()[0] if last else "unknown error", 160), 503)
        self._ctx = ctx
        ctx.route("**/*", self._route)
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            if self._blocked:
                raise FormfillError("That page was refused: " + self._blocked, 400) from None
            raise FormfillError("The page did not open: " + _clip(str(exc).splitlines()[0], 160), 502) from None
        self.settle()
        return resp.status if resp is not None else None

    def url(self) -> str:
        return self._page.url

    def html(self) -> str:
        self._page.evaluate(STAMP_JS)
        return self._page.content()

    def values(self) -> dict[int, str]:
        return {int(k): v for k, v in (self._page.evaluate(READ_JS) or {}).items()}

    def _loc(self, attr: str, idx: int):
        return self._page.locator(f'[{attr}="{int(idx)}"]').first

    def fill_text(self, idx: int, value: str) -> None:
        self._loc("data-rl-idx", idx).fill(value, timeout=5000)

    def select(self, idx: int, value: str) -> None:
        self._loc("data-rl-idx", idx).select_option(value=value, timeout=5000)

    def choose(self, idx: int) -> None:
        loc = self._loc("data-rl-idx", idx)
        try:
            loc.check(timeout=3000)
        except Exception:  # noqa: BLE001 - styled radios are often visually hidden
            loc.check(force=True, timeout=3000)

    def upload(self, idx: int, path: str) -> None:
        self._loc("data-rl-idx", idx).set_input_files(path, timeout=8000)

    def click(self, idx: int) -> None:
        self._loc("data-rl-btn", idx).click(timeout=5000)

    def settle(self) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001 - chatty pages never go idle; that is fine
            pass

    def screenshot(self) -> bytes:
        try:
            data = self._page.screenshot(full_page=True, type="png")
            if len(data) <= 8_000_000:
                return data
        except Exception:  # noqa: BLE001
            pass
        return self._page.screenshot(type="png")

    def blocked_navigation(self) -> str:
        return self._blocked

    def close(self) -> None:
        for obj, method in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._pw = self._page = None


# ---- the service ---------------------------------------------------------------------------------------------------

class Run:
    def __init__(self, run_id: str, item_id: str, host: str, kind: str, now: float, headed: bool):
        self.id, self.item_id, self.host, self.kind, self.headed = run_id, item_id, host, kind, headed
        self.created = self.last = now
        self.state = "ready" if kind == "fill" else "login"
        self.driver: Driver | None = None
        self.plans: list[dict] = []
        self.blockers: list[dict] = []
        self.step = {"current": None, "total": None, "has_next": False}
        self.steps_done = 0
        self.controls: dict = {"submit": None, "next": None, "ambiguous": False}
        self.digest = ""
        self.seen = ""
        self.shot: Path | None = None
        self.result = ""
        self.result_note = ""
        self.before_url = ""
        self.url = ""
        self.tmp_files: list[Path] = []


class Formfill:
    def __init__(self, ctx: Ctx, *, svc: aq.Applyq | None = None, driver_factory: Callable[..., Driver] | None = None,
                 clock: Callable[[], float] | None = None, resolver=None, allow_local: bool = False,
                 profile_fn=None, reaper: bool = True, available: Callable[[], bool] | None = None):
        self.ctx, self.clock = ctx, clock or time.monotonic
        self.svc = svc or aq.Applyq(ctx, profile_fn=profile_fn)
        self.profile_fn = profile_fn or (lambda c: aq.load_profile(c))
        self.driver_factory, self.resolver, self.allow_local = driver_factory, resolver, allow_local
        self._available = available or playwright_available
        self.run: Run | None = None
        self.ended: dict[str, str] = {}
        self.last_start = -1e9
        self.idle, self.stale, self.gap, self.login_for = IDLE_SECONDS, STALE_SECONDS, MIN_GAP_SECONDS, LOGIN_SECONDS
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="formfill")
        self._stop = threading.Event()
        self._reaper = reaper
        self._reaper_thread: threading.Thread | None = None
        self.data = Path(ctx.config.data_path())
        self.shots = self.data / "formfill_shots"
        self.profile_dir = self.data / "formfill_profile"

    # -- config ---------------------------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return getattr(self.ctx.config, "formfill_enabled", False) is True and not _panic.on(self.ctx.config)

    def headed(self) -> bool:
        return getattr(self.ctx.config, "formfill_headed", True) is not False

    def never_hosts(self) -> list[str]:
        path = self.data / "formfill_never.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("hosts"), list):
                return [str(h).lower() for h in data["hosts"]]
        except (OSError, ValueError):
            pass
        cfg = getattr(self.ctx.config, "formfill_never", None)
        return [str(h).lower() for h in (cfg if isinstance(cfg, list) else DEFAULT_NEVER)]

    def set_never(self, hosts: Any) -> list[str]:
        if not isinstance(hosts, list) or len(hosts) > 50:
            raise FormfillError("Send a list of up to 50 site names, like linkedin.com.", 400)
        clean: list[str] = []
        for h in hosts:
            h = str(h or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
            if not _DOMAIN_RE.match(h):
                raise FormfillError(f"'{_clip(str(h), 40)}' is not a site name like example.com.", 400)
            if h not in clean:
                clean.append(h)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "formfill_never.json").write_text(json.dumps({"hosts": clean}), encoding="utf-8")
        return clean

    def status(self) -> dict:
        ok = self._available() or self.driver_factory is not None
        return {"enabled": self.enabled(), "playwright": ok, "headed": self.headed(), "never": self.never_hosts(),
                "install": None if ok else INSTALL_HINT, "busy": self.run is not None and self.run.state in ("ready", "login", "submitting"),
                "reason": "" if self.enabled() else "Turn on form filling from the application card (Settings key formfill_enabled)."}

    # -- plumbing -------------------------------------------------------------------------------------------------
    async def call(self, fn, *args):
        """Everything that touches the browser or the run runs on the one worker thread, so operations never overlap."""
        loop = asyncio.get_running_loop()
        self._ensure_reaper()
        return await loop.run_in_executor(self.pool, lambda: fn(*args))

    def _ensure_reaper(self) -> None:
        if self._reaper and (self._reaper_thread is None or not self._reaper_thread.is_alive()):
            t = threading.Thread(target=self._reap_loop, name="formfill-reaper", daemon=True)
            self._reaper_thread = t
            t.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(10.0):
            try:
                self.pool.submit(self.reap)
            except RuntimeError:
                return

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.pool.submit(self._end, "shutdown").result(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        self.pool.shutdown(wait=False)

    def _driver(self) -> Driver:
        if self.driver_factory is not None:
            return self.driver_factory(self.profile_dir)
        return PlaywrightDriver(self.profile_dir, resolver=self.resolver, allow_local=self.allow_local)

    def _end(self, reason: str, *, keep_shot: bool = False) -> None:
        run = self.run
        if run is None:
            return
        if run.driver is not None:
            try:
                run.driver.close()
            except Exception:  # noqa: BLE001
                pass
            run.driver = None
        for f in run.tmp_files:
            try:
                f.unlink()
            except OSError:
                pass
        if not keep_shot:
            self._drop_shot(run)
            self.run = None
        self.ended[run.id] = reason
        if len(self.ended) > 20:
            self.ended.pop(next(iter(self.ended)))
        if run.state not in ("finished",):
            run.state = "closed"

    def _drop_shot(self, run: Run) -> None:
        if run.shot is not None:
            try:
                run.shot.unlink()
            except OSError:
                pass
            run.shot = None

    def purge_old_shots(self) -> None:
        try:
            for f in self.shots.glob("*.png"):
                if self.run is None or self.run.shot != f:
                    f.unlink()
        except OSError:
            pass

    def reap(self) -> None:
        """Close what is idle or old. Called on every request and by a timer; safe to call any time (on the worker)."""
        run, now = self.run, self.clock()
        if run is None:
            return
        if run.state == "finished":
            if now - run.last > self.stale:
                self._end("expired")
            return
        limit = self.login_for if run.kind == "login" else self.idle
        if now - run.last > limit:
            self._end("idle")
        elif now - run.created > self.stale:
            self._end("stale")

    def _need_run(self, run_id: str) -> Run:
        self.reap()
        run = self.run
        if not _RUN_RE.match(run_id or "") or run is None or run.id != run_id:
            why = self.ended.get(run_id or "")
            if why:
                raise FormfillError({"idle": "That form was closed after 5 idle minutes.", "stale": "That form session was older than 30 minutes and was closed.",
                                     "cancelled": "That form was closed.", "expired": "That result has expired."}.get(why, "That form session has ended."), 410)
            raise FormfillError("No such form session.", 404)
        return run

    def _touch(self, run: Run) -> None:
        run.last = self.clock()

    # -- capture -----------------------------------------------------------------------------------------------------
    def _capture(self, run: Run, status: int | None = None, *, fill: bool = False) -> None:
        """Read the page, (optionally) fill it, then take the digest and the screenshot together."""
        drv = run.driver
        assert drv is not None
        run.url = drv.url()
        page = parse_page(drv.html())
        run.blockers = detect_blockers(page, run.url, status)
        run.controls = find_controls(page["buttons"])
        run.step = wizard_info(page, run.controls)
        run.step["current"] = run.step["current"] or run.steps_done + 1
        hard = any(b["kind"] in ("login_required", "blocked", "no_form") for b in run.blockers)
        fields = [f for f in page["fields"] if f["visible"] or f["type"] == "radio"]
        if fill:
            if hard:
                run.plans = []
            else:
                profile = self.svc.profile_fn(self.ctx) or {}
                item = self.svc.get(run.item_id)
                facts = build_facts(profile, item.get("draft"), this_year=self.svc.clock().year, resume=self._resume_fact(run, profile))
                run.plans = plan_form(fields, facts)
                self._execute(run)
        values = drv.values()
        run.digest = digest_of(run.url, page["fields"], values, run.controls["submit"])
        self._drop_shot(run)
        self.shots.mkdir(parents=True, exist_ok=True)
        run.shot = self.shots / f"{run.id}.png"
        run.shot.write_bytes(drv.screenshot())
        run.seen = ""
        run._page_fields = page["fields"]       # type: ignore[attr-defined]

    def _resume_fact(self, run: Run, profile: dict) -> dict | None:
        for ext in ("pdf", "docx", "doc", "txt", "rtf"):
            p = self.data / f"formfill_resume.{ext}"
            if p.is_file():
                return {"value": str(p), "name": p.name, "source": "resume file"}
        text = str(profile.get("resume_text") or "").strip()
        if text:
            tmp = self.data / "formfill_tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            p = tmp / f"resume-{run.id}.txt"
            p.write_text(text, encoding="utf-8")
            run.tmp_files.append(p)
            return {"value": str(p), "name": "resume.txt (from your profile text)", "source": "profile resume text"}
        return None

    def _execute(self, run: Run) -> None:
        drv = run.driver
        assert drv is not None
        for p in run.plans:
            if p["status"] != "fill":
                continue
            try:
                if p["action"] == "text":
                    drv.fill_text(p["idx"], p["value"])
                elif p["action"] == "select":
                    drv.select(p["idx"], p["value"])
                elif p["action"] == "choose":
                    drv.choose(p["target"])
                elif p["action"] == "upload":
                    drv.upload(p["idx"], p["value"])
            except Exception:  # noqa: BLE001 - one stubborn field must not stop the rest
                p.update(status="needs_you", reason="I could not fill this one on the page.")
        # Fields that a page script cleared or refused show up as empty on read-back.
        values = drv.values()
        for p in run.plans:
            if p["status"] == "fill" and p["action"] in ("text", "select") and not values.get(p["idx"], "x"):
                p.update(status="needs_you", reason="The page did not keep my answer. Type it yourself.")

    # -- views -------------------------------------------------------------------------------------------------------
    def view(self, run: Run) -> dict:
        now = self.clock()
        counts = {"filled": 0, "needs_you": 0, "skipped": 0}
        fields = []
        for p in run.plans:
            counts[{"fill": "filled"}.get(p["status"], p["status"])] += 1
            fields.append({"n": p["n"], "label": p["label"], "type": p["type"], "status": "filled" if p["status"] == "fill" else p["status"],
                           "value": p["display"] if p["status"] == "fill" else "", "reason": p["reason"], "required": p["required"], "key": p["key"]})
        st = run.step
        label = ""
        if run.kind == "fill" and run.plans:
            label = f"step {st['current']} of {st['total']} filled" if st["total"] else (
                f"step {st['current']} filled; there is a Next button" if st["has_next"] else "form filled")
        limit = self.login_for if run.kind == "login" else self.idle
        ctrl = run.controls
        return {"run": run.id, "item_id": run.item_id, "host": run.host, "kind": run.kind, "state": run.state, "step": {**st, "label": label},
                "fields": fields, "counts": counts, "blockers": run.blockers, "digest": run.digest, "seen": bool(run.digest and run.seen == run.digest),
                "can_next": run.state == "ready" and ctrl["next"] is not None, "can_submit": run.state == "ready" and ctrl["submit"] is not None and not self._needs_human(run),
                "submit_label": ctrl["submit"]["text"] if ctrl["submit"] else "", "submit_ambiguous": ctrl["ambiguous"],
                "screenshot_url": f"/api/applyq/{run.item_id}/formfill/{run.id}/screenshot" if run.shot else "",
                "headed": run.headed, "idle_seconds_left": max(0, int(limit - (now - run.last))) if run.state in ("ready", "login") else 0,
                "result": run.result, "result_note": run.result_note}

    def _needs_human(self, run: Run) -> bool:
        return any(b["kind"] == "captcha" for b in run.blockers) and not run.headed

    # -- operations (all on the worker thread) --------------------------------------------------------------------------
    def check_item(self, item_id: str) -> dict:
        item = self.svc.get(item_id)
        if item["state"] not in ("ready", "failed"):
            raise FormfillError(f"This one is {item['state']}, so there is nothing to fill.", 409)
        if aq.infer_channel(item) != "web" or not str(item.get("url") or "").startswith(("http://", "https://")):
            raise FormfillError("Only web applications with a page address can be filled.", 400)
        if not item.get("draft"):
            raise FormfillError("Draft it first.", 400)
        return item

    def _guard_url(self, url: str) -> str:
        reason = url_problem(url, resolver=self.resolver, allow_local=self.allow_local)
        if reason:
            raise FormfillError("I will not open that page. " + reason, 400)
        hit = never_match(host_of(url), self.never_hosts())
        if hit:
            raise FormfillError(f"{hit} is on your never-fill list. Apply there yourself (use Copy). You can edit the list.", 400)
        return host_of(url)

    def _open_sessions(self) -> None:
        self.reap()
        if self.run is not None and self.run.state in ("ready", "login"):
            what = "A login window is" if self.run.kind == "login" else "A form is"
            raise FormfillError(f"{what} already open. Close it first.", 409, run=self.run.id)

    def start(self, item_id: str, *, confirm_over_cap: bool = False) -> dict:
        if not self.enabled():
            raise FormfillError("Form filling is switched off (formfill_enabled is false).", 404)
        if not (self._available() or self.driver_factory is not None):
            raise FormfillError("Playwright is not installed. Run: " + INSTALL_HINT, 503)
        item = self.check_item(item_id)
        host = self._guard_url(item["url"])
        self._open_sessions()
        wait = self.gap - (self.clock() - self.last_start)
        if wait > 0:
            raise FormfillError(f"Wait {int(wait) + 1} seconds before opening another form.", 429)
        today = self.svc.summary()["today"]
        cap, done = aq.daily_cap(self.ctx.config), today["sent"] + today["applied"]
        if cap and done >= cap and not confirm_over_cap:
            raise FormfillError(f"You already applied to {done} today (cap {cap}). Confirm to go on.", 409, needs_confirm=True)
        if self.run is not None:       # a finished run's result is dismissed by starting the next one
            self._end("replaced")
        self.purge_old_shots()
        self.last_start = self.clock()
        run = Run(secrets.token_hex(8), item_id, host, "fill", self.clock(), self.headed())
        self.run = run
        self.ctx.audit("formfill_start", id=item_id, host=host)
        try:
            run.driver = self._driver()
            status = run.driver.open(item["url"], run.headed)
            self._capture(run, status, fill=True)
        except FormfillError:
            self._end("failed")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("formfill: start failed (%s)", type(exc).__name__)
            self._end("failed")
            raise FormfillError("The page could not be opened or read. Use Copy and apply yourself.", 502) from None
        return self.view(run)

    def login(self, item_id: str) -> dict:
        if not self.enabled():
            raise FormfillError("Form filling is switched off (formfill_enabled is false).", 404)
        if not (self._available() or self.driver_factory is not None):
            raise FormfillError("Playwright is not installed. Run: " + INSTALL_HINT, 503)
        item = self.svc.get(item_id)
        host = self._guard_url(item["url"]) if str(item.get("url") or "").startswith("http") else ""
        if not host:
            raise FormfillError("This application has no page address.", 400)
        self._open_sessions()
        if self.run is not None:
            self._end("replaced")
        run = Run(secrets.token_hex(8), item_id, host, "login", self.clock(), True)
        self.run = run
        self.ctx.audit("formfill_login", id=item_id, host=host)
        try:
            run.driver = self._driver()
            run.driver.open(item["url"], True)          # always a visible window: the owner types the password, not me
        except FormfillError:
            self._end("failed")
            raise
        except Exception:  # noqa: BLE001
            self._end("failed")
            raise FormfillError("The login window could not be opened.", 502) from None
        return self.view(run)

    def next_step(self, run_id: str, confirm: bool) -> dict:
        run = self._need_run(run_id)
        if run.kind != "fill" or run.state != "ready":
            raise FormfillError("This session cannot go to a next step.", 409)
        if confirm is not True:
            raise FormfillError("Confirm to go to the next step.", 400)
        nxt = run.controls["next"]
        if nxt is None:
            raise FormfillError("I found no Next button on this page.", 400)
        self._touch(run)
        drv = run.driver
        assert drv is not None
        try:
            drv.click(nxt["idx"])
            drv.settle()
            if drv.blocked_navigation():
                raise FormfillError("The next page was refused: " + drv.blocked_navigation(), 400)
            run.steps_done += 1
            self._capture(run, None, fill=True)
        except FormfillError:
            raise
        except Exception:  # noqa: BLE001
            self._end("failed")
            raise FormfillError("The next step could not be read. The session was closed.", 502) from None
        return self.view(run)

    def refresh(self, run_id: str) -> dict:
        run = self._need_run(run_id)
        if run.state not in ("ready", "login"):
            return self.view(run)
        self._touch(run)
        if run.kind == "fill" and run.driver is not None:
            try:
                self._capture(run, None, fill=False)
            except Exception:  # noqa: BLE001
                self._end("failed")
                raise FormfillError("The page could not be read any more. The session was closed.", 502) from None
        return self.view(run)

    def current(self, item_id: str) -> dict:
        self.reap()
        run = self.run
        if run is None or run.item_id != item_id:
            return {"run": None}
        return self.view(run)

    def screenshot(self, run_id: str) -> bytes:
        run = self._need_run(run_id)
        if run.shot is None or not run.shot.exists():
            raise FormfillError("There is no screenshot.", 404)
        self._touch(run)
        run.seen = run.digest
        return run.shot.read_bytes()

    def cancel(self, run_id: str) -> dict:
        run = self._need_run(run_id)
        self._end("cancelled")
        return {"ok": True, "run": run.id}

    def submit(self, run_id: str, field_digest: Any, confirm: Any) -> dict:
        """The ONLY click that submits an application. Needs confirm, the digest the owner saw, an unchanged page, a
        screenshot that was actually served, and a run under 30 minutes old."""
        run = self._need_run(run_id)
        if run.kind != "fill" or run.state != "ready" or run.driver is None:
            raise FormfillError("This session cannot be submitted.", 409)
        if confirm is not True:
            raise FormfillError("Submitting needs your explicit confirm.", 400)
        if self.clock() - run.created > self.stale:
            self._end("stale")
            raise FormfillError("This form session is older than 30 minutes. Start again.", 409, stale=True)
        if not isinstance(field_digest, str) or not field_digest:
            raise FormfillError("Send the field_digest from the screenshot you looked at.", 400)
        self._touch(run)
        drv = run.driver
        page = parse_page(drv.html())
        live = digest_of(drv.url(), page["fields"], drv.values(), find_controls(page["buttons"])["submit"])
        if live != run.digest or field_digest != run.digest:
            self._capture(run, None, fill=False)
            raise FormfillError("The page changed since the screenshot you saw. Look at the new one and confirm again.", 409,
                                changed=True, digest=run.digest)
        if run.seen != run.digest:
            raise FormfillError("Look at the screenshot first, then confirm.", 409, needs_look=True, digest=run.digest)
        ctrl = run.controls["submit"]
        if ctrl is None:
            raise FormfillError("I could not tell which button submits this form" + (" (more than one fits)" if run.controls["ambiguous"] else "") +
                                ". Submit it yourself in the laptop window.", 422)
        if self._needs_human(run):
            raise FormfillError("The page shows a captcha and the window is hidden. I never solve those.", 422)
        run.state = "submitting"
        self.ctx.audit("formfill_submit", id=run.item_id, host=run.host)
        before = drv.url()
        try:
            drv.click(ctrl["idx"])
            drv.settle()
            after_page = parse_page(drv.html())
            after = drv.url()
            run.result = judge_result(before, after, after_page["text"], sum(1 for f in after_page["fields"] if f["visible"] and f["type"] not in ("hidden",)))
            self._drop_shot(run)
            self.shots.mkdir(parents=True, exist_ok=True)
            run.shot = self.shots / f"{run.id}.png"
            run.shot.write_bytes(drv.screenshot())
        except Exception:  # noqa: BLE001
            run.result = "needs_check"
        note = "Submitted through the laptop form fill." if run.result == "applied" else \
            "I clicked Submit but the page did not clearly say it worked. Check the site before applying again."
        run.result_note = note
        try:
            self.svc.formfill_result(run.item_id, run.result == "applied", note)
        except Exception:  # noqa: BLE001
            log.warning("formfill: could not record the result")
        self.ctx.audit("formfill_result", id=run.item_id, host=run.host, result=run.result)
        run.state = "finished"
        run.last = self.clock()
        self._end("submitted", keep_shot=True)
        run.state = "finished"
        return self.view(run)


# ---- the router --------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, **kw: Any) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(ctx.need_auth)])
    svc = Formfill(ctx, **kw)

    def _panic_stop():
        if svc.run is None:
            return None
        svc._end("panic")
        return "form-filling session closed (nothing was submitted)"
    _panic.register("Form filling", _panic_stop, ctx=ctx)   # hub/panic.py
    router.service = svc                       # type: ignore[attr-defined]
    ctx.formfill = svc                         # hub/batches.py drives the same service one form after another

    async def guarded(fn, *args):
        try:
            return await svc.call(fn, *args)
        except FormfillError as exc:
            body = {"detail": exc.message, **exc.extra}
            return JSONResponse(body, status_code=exc.status)
        except LookupError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)

    @router.get("/formfill/status")
    async def status():
        return svc.status()

    @router.get("/formfill/never")
    async def never_get():
        return {"never": svc.never_hosts()}

    @router.put("/formfill/never")
    async def never_put(body: dict = Body(default_factory=dict)):
        try:
            return {"never": svc.set_never(body.get("never"))}
        except FormfillError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @router.post("/applyq/{item_id}/formfill/start")
    async def start(item_id: str, body: dict = Body(default_factory=dict)):
        _panic.guard(ctx.config)
        return await guarded(lambda: svc.start(item_id, confirm_over_cap=body.get("confirm_over_cap") is True))

    @router.post("/applyq/{item_id}/formfill/login")
    async def login(item_id: str):
        return await guarded(svc.login, item_id)

    @router.get("/applyq/{item_id}/formfill")
    async def current(item_id: str):
        return await guarded(svc.current, item_id)

    @router.get("/applyq/{item_id}/formfill/{run_id}")
    async def one(item_id: str, run_id: str):
        return await guarded(lambda: svc.view(svc._need_run(run_id)))

    @router.get("/applyq/{item_id}/formfill/{run_id}/screenshot")
    async def shot(item_id: str, run_id: str):
        res = await guarded(svc.screenshot, run_id)
        if isinstance(res, JSONResponse):
            return res
        return Response(res, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/applyq/{item_id}/formfill/{run_id}/refresh")
    async def refresh(item_id: str, run_id: str):
        return await guarded(svc.refresh, run_id)

    @router.post("/applyq/{item_id}/formfill/{run_id}/next")
    async def nxt(item_id: str, run_id: str, body: dict = Body(default_factory=dict)):
        return await guarded(svc.next_step, run_id, body.get("confirm") is True)

    @router.post("/applyq/{item_id}/formfill/{run_id}/submit")
    async def submit(item_id: str, run_id: str, body: dict = Body(default_factory=dict)):
        _panic.guard(ctx.config)
        return await guarded(svc.submit, run_id, body.get("field_digest"), body.get("confirm"))

    @router.post("/applyq/{item_id}/formfill/{run_id}/cancel")
    async def cancel(item_id: str, run_id: str):
        return await guarded(svc.cancel, run_id)

    return router
