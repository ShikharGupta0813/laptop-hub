"""Pure helpers for hub/oppsources.py: turn messy listing text into the common item schema. No network, no clock unless passed.

- parse_remote(text, location)   -> (mode, geo)      mode: remote | hybrid | onsite | unknown; geo: {worldwide, regions, text}
- parse_pay(text)                -> {min, max, unit, currency, text} or None   (unit: hour|day|week|month|year|project)
- parse_employment(text, hint, kind) -> full-time | part-time | contract | freelance | task-based | internship | unspecified
- parse_posted(value, now)       -> epoch seconds or 0.0
- fingerprint(company, title)    -> cross-source duplicate key
- Selector / select / parse_dom  -> a small CSS selector engine over the stdlib HTML parser (owner-given selectors)
Everything here treats its input as untrusted text.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

WORK_MODES = ("remote", "hybrid", "onsite", "unknown")
EMPLOYMENT = ("full-time", "part-time", "contract", "freelance", "task-based", "internship", "unspecified")
APPLY_METHODS = ("email", "ats_form", "external_manual", "platform_manual")
PAY_UNITS = ("hour", "day", "week", "month", "year", "project")

# ---- remote / geo -----------------------------------------------------------------------------------------------

_NOT_REMOTE = re.compile(r"\b(?:not|non|no)[- ]remote\b|\bremote (?:work )?(?:is )?not (?:possible|available|offered)\b|"
                         r"\b(?:on-?site|in[- ]office|in[- ]person) (?:only|required|full[- ]time)\b|\boffice[- ]based\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b|\b\d days? (?:a|per|in the) (?:week|office)\b", re.I)
_REMOTE = re.compile(r"\b(?:fully[- ]|100% |all[- ]|remote[- ]first|remote[- ]friendly)?remote(?:ly)?\b|\bwork(?:ing)? from (?:home|anywhere)\b|"
                     r"\bwfh\b|\btelecommut\w*|\bdistributed (?:team|company)\b|\banywhere in the world\b|\bworldwide\b", re.I)
_ONSITE = re.compile(r"\bon-?site\b|\bin[- ]person\b|\bin[- ]office\b", re.I)
_WORLDWIDE = re.compile(r"\bworldwide\b|\banywhere\b|\bglobal(?:ly)?\b|\bany(?: location| country| timezone| time zone)\b|\bwork from anywhere\b", re.I)
# Region words that commonly restrict a remote role. A plain data table: extend it, nothing else depends on its content.
REGION_TOKENS = ("US", "USA", "United States", "UK", "United Kingdom", "EU", "European Union", "Europe", "EMEA", "APAC", "LATAM",
                 "Latin America", "North America", "Canada", "India", "Australia", "Germany", "France", "Spain", "Netherlands",
                 "Ireland", "Brazil", "Mexico", "Africa", "Asia", "Singapore", "Japan", "Poland", "Portugal")
_REGION_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(REGION_TOKENS, key=len, reverse=True)) + r")(?![\w])")
_GEO_CONTEXT = re.compile(r"(?:only|based|located|residents?|eligible|authori[sz]ed|timezones?|time zones?|within|must (?:live|reside)|"
                          r"candidates (?:in|from)|applicants (?:in|from)|hiring in|open to)", re.I)
_REGION_ALIAS = {"usa": "US", "united states": "US", "united kingdom": "UK", "european union": "EU"}


def _norm_region(token: str) -> str:
    return _REGION_ALIAS.get(token.lower(), token)


def parse_remote(text: str = "", location: str = "") -> tuple[str, dict]:
    """Work mode and geo restriction from free text plus the board's location field. Never raises."""
    text, location = text or "", location or ""
    both = f"{location}\n{text}"
    if _NOT_REMOTE.search(both):
        mode = "onsite"
    elif _HYBRID.search(both):
        mode = "hybrid"
    elif _REMOTE.search(both):
        mode = "remote"
    elif _ONSITE.search(both):
        mode = "onsite"
    elif location.strip():
        mode = "onsite"
    else:
        mode = "unknown"
    regions: list[str] = []
    if mode in ("remote", "hybrid"):
        # the location field is a restriction on its own ("Remote - US"); in free text only sentences that talk about it count
        for chunk in [location] + [s for s in re.split(r"[.\n;]", text) if _GEO_CONTEXT.search(s) or _REMOTE.search(s)]:
            if chunk is location or _GEO_CONTEXT.search(chunk) or "remote" in chunk.lower():
                for m in _REGION_RE.finditer(chunk):
                    r = _norm_region(m.group(1))
                    if r not in regions:
                        regions.append(r)
    worldwide = bool(_WORLDWIDE.search(location) or (not regions and mode == "remote" and _WORLDWIDE.search(text)))
    if regions:
        worldwide = False
    label = ", ".join(regions) if regions else ("Worldwide" if worldwide else "")
    return mode, {"worldwide": worldwide, "regions": regions[:8], "text": label}


# ---- pay ------------------------------------------------------------------------------------------------------------

_CUR_WORD = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "₹": "INR", "inr": "INR",
             "rs": "INR", "rs.": "INR", "cad": "CAD", "c$": "CAD", "aud": "AUD", "a$": "AUD", "chf": "CHF", "sgd": "SGD"}
_CUR = r"(?:US\$|C\$|A\$|\$|€|£|₹|Rs\.?|USD|EUR|GBP|INR|CAD|AUD|CHF|SGD)"
_AMT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT_WORD = {"hour": "hour", "hr": "hour", "h": "hour", "hourly": "hour", "day": "day", "daily": "day", "week": "week", "wk": "week",
              "weekly": "week", "month": "month", "mo": "month", "monthly": "month", "year": "year", "yr": "year", "annum": "year",
              "pa": "year", "annual": "year", "annually": "year", "yearly": "year", "project": "project", "task": "project",
              "lpa": "year"}
_PAY = re.compile(
    rf"(?P<c1>{_CUR})?\s?(?P<a1>{_AMT})\s?(?P<k1>[kK])?(?:\s?(?P<l1>lpa|lakhs?))?"
    rf"(?:\s?(?:-|–|—|to)\s?(?P<c2>{_CUR})?\s?(?P<a2>{_AMT})\s?(?P<k2>[kK])?(?:\s?(?P<l2>lpa|lakhs?))?)?"
    rf"(?:\s?(?P<c3>USD|EUR|GBP|INR|CAD|AUD|CHF|SGD)\b)?"
    r"(?:\s?(?:/|per|a|an|every|each|p\.)\s?(?P<unit>hour|hr|h|day|week|wk|month|mo|year|yr|annum|project|task)\b|\s?(?P<u2>lpa|pa|annually|yearly|hourly|monthly|weekly|daily)\b)?",
    re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _currency(*cands: str | None) -> str:
    for c in cands:
        if c:
            return _CUR_WORD.get(c.lower().rstrip("."), _CUR_WORD.get(c.lower(), ""))
    return ""


def pay_struct(lo: Any = None, hi: Any = None, currency: str = "", unit: str = "") -> dict | None:
    """From already-structured numbers (ATS fields). None when there is no usable amount."""
    try:
        lo_n = float(lo) if lo not in (None, "") else None
        hi_n = float(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if lo_n is None and hi_n is None:
        return None
    lo_n = lo_n if lo_n is not None else hi_n
    hi_n = hi_n if hi_n is not None else lo_n
    if lo_n <= 0 and hi_n <= 0:
        return None
    if lo_n > hi_n:
        lo_n, hi_n = hi_n, lo_n
    unit = _UNIT_WORD.get((unit or "").lower().replace("_", "").replace("per", "").strip(), unit if unit in PAY_UNITS else "")
    return {"min": lo_n, "max": hi_n, "unit": unit or _guess_unit(hi_n, ""), "currency": (currency or "").upper()[:3]}


def _guess_unit(amount: float, context: str) -> str:
    if re.search(r"\bhourly\b|\bper hour\b", context, re.I):
        return "hour"
    if re.search(r"\bfixed[- ]price\b|\bbudget\b|\bone[- ]time\b|\bper project\b|\bflat fee\b", context, re.I):
        return "project"
    if re.search(r"\bsalary\b|\bannual(?:ly)?\b|\byearly\b|\bper annum\b|\bOTE\b", context, re.I):
        return "year"
    return "year" if amount >= 10000 else ""


def parse_pay(text: str) -> dict | None:
    """The first believable pay range in `text`, or None. Understands $90-120k, $90,000 - $120,000 per year, EUR 50/hr,
    £400 a day, 60 to 80 USD per hour, ₹10-15 LPA, 'Budget: $500'. A bare number never counts: a currency or a unit is needed."""
    text = text or ""
    best = None
    for m in _PAY.finditer(text):
        cur = _currency(m.group("c1"), m.group("c2"), m.group("c3"))
        unit_raw = (m.group("unit") or m.group("u2") or "").lower()
        lakh = bool(m.group("l1") or m.group("l2") or unit_raw == "lpa")
        if not cur and lakh:
            cur = "INR"
        if not cur and not unit_raw:
            continue
        if not cur and unit_raw and not (m.group("a2") or m.group("k1") or m.group("k2")) and _num(m.group("a1")) < 5:
            continue                                   # "2 per day" is not pay
        try:
            a1 = _num(m.group("a1"))
            a2 = _num(m.group("a2")) if m.group("a2") else a1
        except ValueError:
            continue
        k1, k2 = bool(m.group("k1")), bool(m.group("k2"))
        if k2 and not k1 and a1 < a2:
            k1 = True
        mult1, mult2 = (1000 if k1 else 1), (1000 if k2 else 1)
        if lakh:
            mult1 = mult2 = 100000
        lo = a1 * mult1
        hi = a2 * mult2 if m.group("a2") else lo
        if lo > hi:
            lo, hi = hi, lo
        if hi <= 0 or hi > 5_000_000_000:
            continue
        unit = _UNIT_WORD.get(unit_raw, "")
        # a year-looking number tagged "per hour" is a typo in the posting; keep what it says
        ctx = text[max(0, m.start() - 60):m.end() + 60]
        explicit = bool(unit)
        unit = unit or _guess_unit(hi, ctx)
        if not cur and not explicit:
            continue
        found = {"min": lo, "max": hi, "unit": unit, "currency": cur, "text": " ".join(m.group(0).split())[:60]}
        if best is None or (explicit and not best.get("_explicit")):
            found["_explicit"] = explicit
            best = found
            if explicit:
                break
    if best:
        best.pop("_explicit", None)
    return best


def pay_label(pay: dict | None) -> str:
    if not pay:
        return ""
    cur = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(pay.get("currency", ""), (pay.get("currency") or "") + " ")

    def fmt(v: float) -> str:
        return f"{v / 1000:g}k" if v >= 10000 else f"{v:g}"
    lo, hi = pay["min"], pay["max"]
    body = f"{cur}{fmt(lo)}" + (f"-{fmt(hi)}" if hi != lo else "")
    return body + (f"/{pay['unit']}" if pay.get("unit") else "")


# ---- employment ---------------------------------------------------------------------------------------------------

_EMP_RULES = (
    ("internship", re.compile(r"\bintern(?:ship)?s?\b|\bapprentice(?:ship)?\b|\bco-?op\b|\bwerkstudent\b", re.I)),
    ("task-based", re.compile(r"\btask[- ]based\b|\bper[- ]task\b|\bmicro-?tasks?\b|\bannotat(?:or|ion)\b|\bdata labell?ing\b|\bAI trainer\b|\brater\b|\bevaluator\b", re.I)),
    ("freelance", re.compile(r"\bfreelanc\w*|\bgig\b|\bone[- ]off project\b|\bindependent contractor\b|\bself[- ]employed\b", re.I)),
    ("contract", re.compile(r"\bcontract(?:or|ual)?\b|\bfixed[- ]term\b|\bc2c\b|\bcorp[- ]to[- ]corp\b|\btemp(?:orary)?\b|\bconsultant\b", re.I)),
    ("part-time", re.compile(r"\bpart[- _]?time\b", re.I)),
    ("full-time", re.compile(r"\bfull[- _]?time\b|\bpermanent\b|\bFTE\b|\bregular employee\b", re.I)),
)


def parse_employment(text: str = "", hint: str = "", kind: str = "") -> str:
    """The structured `hint` (an ATS field) wins, then the title/text. `kind` decides the fallback."""
    for source in (hint or "", text or ""):
        if not source:
            continue
        for name, rule in _EMP_RULES:
            if source is text and name in ("contract",) and len(source) > 400:
                source = source[:400]        # 'contract' deep in a long description is usually a legal clause
            if rule.search(source):
                return name
    return {"freelance": "freelance", "llm-task": "task-based"}.get(kind, "unspecified")


# ---- posted time ----------------------------------------------------------------------------------------------------

_REL = re.compile(r"(\d+)\s*(second|sec|minute|min|hour|hr|h|day|d|week|wk|w|month|mo|year|yr|y)s?\.?\s*ago", re.I)
_REL_SEC = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "h": 3600, "day": 86400, "d": 86400,
            "week": 604800, "wk": 604800, "w": 604800, "month": 2592000, "mo": 2592000, "year": 31536000, "yr": 31536000, "y": 31536000}


def parse_posted(value: Any, now: float) -> float:
    """Epoch seconds, or 0.0 when the value is empty, unreadable or in the future by more than a day."""
    if value in (None, "", 0):
        return 0.0
    ts = 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value) / 1000.0 if value > 1e11 else float(value)
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{9,13}(?:\.\d+)?", text):
            return parse_posted(float(text), now)
        low = text.lower()
        m = _REL.search(low)
        if m:
            ts = now - int(m.group(1)) * _REL_SEC[m.group(2).lower()]
        elif low in ("today", "just now", "now"):
            ts = now
        elif low == "yesterday":
            ts = now - 86400
        else:
            try:
                d = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace(" UTC", "+00:00"))
                ts = (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
            except ValueError:
                try:
                    d = email.utils.parsedate_to_datetime(text)
                    ts = (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
                except (TypeError, ValueError, IndexError):
                    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%d/%m/%Y"):
                        try:
                            ts = dt.datetime.strptime(text, fmt).replace(tzinfo=dt.timezone.utc).timestamp()
                            break
                        except ValueError:
                            continue
    if ts <= 0 or ts > now + 86400:
        return 0.0
    return ts


# ---- duplicates ---------------------------------------------------------------------------------------------------

_CORP = re.compile(r"\b(?:inc|llc|ltd|limited|gmbh|corp|corporation|co|plc|s\.?a\.?|b\.?v\.?|pty|ag|oy|ab|pvt|private|technologies|technology|labs?)\b\.?", re.I)
_TITLE_NOISE = re.compile(r"\((?:[^)]*)\)|\[(?:[^\]]*)\]|\b(?:m/f/d|f/m/d|m/w/d|remote|hybrid|on-?site|urgent|hiring|new|immediate(?:ly)?)\b", re.I)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def company_key(company: str) -> str:
    return slug(_CORP.sub(" ", company or ""))


def title_key(title: str) -> str:
    t = _TITLE_NOISE.sub(" ", title or "")
    t = re.sub(r"\s[-–|@]\s.*$", "", t) if re.search(r"\s[-–|@]\s", t) and len(t.split()) > 4 else t
    return slug(t)


def fingerprint(company: str, title: str) -> str:
    """Same company and (noise-stripped) title across boards gives the same key; '' when there is no company to anchor on."""
    c, t = company_key(company), title_key(title)
    return f"{c}|{t}" if c and t else ""


# ---- a small CSS selector engine over the stdlib HTML parser ------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
MAX_NODES = 20000


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node] = []
        self.text: list[str] = []       # text pieces directly inside, interleaved order is not kept (not needed)

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def all_text(self) -> str:
        parts = list(self.text)
        for c in self.children:
            parts.append(c.all_text())
        return " ".join(" ".join(parts).split())


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root
        self.count = 0
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
            return
        self.count += 1
        if self.count > MAX_NODES:
            return
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            return
        self.count += 1
        if self.count <= MAX_NODES:
            self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.skip = max(0, self.skip - 1)
            return
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.cur.text.append(data.strip())


def parse_dom(markup: str) -> Node:
    b = _DomBuilder()
    try:
        b.feed(markup or "")
        b.close()
    except Exception:  # noqa: BLE001 - broken markup yields whatever was built
        pass
    return b.root


_COMPOUND = re.compile(r"""(?P<tag>[a-zA-Z][\w-]*|\*)?(?P<rest>(?:[#.][\w-]+|\[[^\]]+\])*)""")
_PART = re.compile(r"""#([\w-]+)|\.([\w-]+)|\[\s*([\w:-]+)\s*(?:([~^$*|]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]*)))?\s*\]""")


class SelectorError(ValueError):
    pass


class Selector:
    """tag, #id, .class, [attr], [attr=v], [attr^=v], [attr$=v], [attr*=v], descendant (space) and child (>) combinators, a, b lists."""

    def __init__(self, text: str):
        text = (text or "").strip()
        if not text or len(text) > 300 or re.search(r"[:()+~{}]", re.sub(r"\[[^\]]*\]", "", text)):
            raise SelectorError("That selector is not supported. Use tags, .classes, #ids, [attributes], spaces and >.")
        self.alternatives = [self._chain(p) for p in text.split(",") if p.strip()]
        if not self.alternatives:
            raise SelectorError("Empty selector.")

    @staticmethod
    def _chain(text: str) -> list[tuple[str, dict]]:
        tokens = re.findall(r"""(?:\[[^\]]*\]|[^\s>\[])+|>""", text.replace(">", " > "))
        chain, comb = [], " "
        for tok in tokens:
            if tok == ">":
                comb = ">"
                continue
            m = _COMPOUND.fullmatch(tok)
            if not m:
                raise SelectorError(f"Cannot read the selector part '{tok[:30]}'.")
            spec = {"tag": (m.group("tag") or "*").lower(), "id": None, "classes": [], "attrs": []}
            for pm in _PART.finditer(m.group("rest") or ""):
                if pm.group(1):
                    spec["id"] = pm.group(1)
                elif pm.group(2):
                    spec["classes"].append(pm.group(2))
                else:
                    val = next((g for g in (pm.group(5), pm.group(6), pm.group(7)) if g is not None), None)
                    spec["attrs"].append((pm.group(3).lower(), pm.group(4), val))
            chain.append((comb, spec))
            comb = " "
        if not chain:
            raise SelectorError("Empty selector.")
        return chain

    @staticmethod
    def _match(node: Node, spec: dict) -> bool:
        if spec["tag"] != "*" and node.tag != spec["tag"]:
            return False
        if spec["id"] and node.attrs.get("id") != spec["id"]:
            return False
        if spec["classes"] and not set(spec["classes"]) <= node.classes():
            return False
        for name, op, val in spec["attrs"]:
            have = node.attrs.get(name)
            if have is None:
                return False
            if op is None:
                continue
            if op == "=" and have != val:
                return False
            if op == "^=" and not have.startswith(val or "\0"):
                return False
            if op == "$=" and not have.endswith(val or "\0"):
                return False
            if op == "*=" and (val or "\0") not in have:
                return False
            if op == "~=" and val not in have.split():
                return False
        return True

    def _matches(self, node: Node, chain: list, scope: Node) -> bool:
        comb, spec = chain[-1]
        if not self._match(node, spec):
            return False
        if len(chain) == 1:
            return True
        rest = chain[:-1]
        parent = node.parent
        if comb == ">":
            return parent is not None and parent.tag != "#root" and self._matches(parent, rest, scope)
        while parent is not None and parent.tag != "#root":
            if self._matches(parent, rest, scope):
                return True
            parent = parent.parent
        return False

    def select(self, scope: Node) -> list[Node]:
        out, seen = [], set()
        stack = list(reversed(scope.children))
        order = []
        while stack:
            n = stack.pop()
            order.append(n)
            stack.extend(reversed(n.children))
        for n in order:
            if id(n) in seen:
                continue
            if any(self._matches(n, chain, scope) for chain in self.alternatives):
                seen.add(id(n))
                out.append(n)
        return out


def extract_field(node: Node, spec: str, base: str = "") -> str:
    """spec is 'css', 'css@attr' or '@attr' (the node itself). Empty/missing gives ''. Links are resolved against `base`."""
    spec = (spec or "").strip()
    if not spec:
        return ""
    css, _, attr = spec.partition("@")
    targets = Selector(css).select(node) if css.strip() else [node]
    if not targets:
        return ""
    t = targets[0]
    if attr:
        value = t.attrs.get(attr.strip().lower(), "")
        return urljoin(base, value) if attr.strip().lower() in ("href", "src") and base else value
    return t.all_text()
