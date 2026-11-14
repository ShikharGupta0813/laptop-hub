"""Privacy redaction: sensitive tokens never leave the laptop for a cloud model.

Before text goes to Gemini, Claude (login or API key) or the duo, this module swaps emails, phone numbers, card
numbers, Aadhaar and PAN numbers, IBANs, API keys, passwords and the owner's own `redact_names` for stable
placeholders such as [EMAIL_1]. The model's reply (including tool-call arguments) has the placeholders put back
before a tool runs or text is shown. The placeholder map lives only for one turn (`begin_turn` .. `end_turn`).
The local Ollama model is never redacted. Audit rows hold COUNTS per kind, never values.

What it cannot do: it matches patterns, not meaning. A name, an address, a secret that has no label or
recognisable shape, text inside an image (screen vision), or anything the model could infer is not hidden.
See docs/api/privacy-usage.md.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter
from contextvars import ContextVar
from typing import Any, AsyncIterator, Callable

KINDS = ("email", "phone", "key", "card", "aadhaar", "pan", "iban", "password", "name")
PREFIX = {k: k.upper() for k in KINDS}
TOOL_OPEN, TOOL_CLOSE = "<tool_call>", "</tool_call>"
MAX_NAMES = 50
MAX_KNOWN = 400
CUSTOM_KIND = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
MAX_CUSTOM_RULES = 50
CUSTOM_LABEL_MAX = 40

_MARK_RE = re.compile(r"(<tool_call>|</tool_call>)")
_PH_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9]{1,15})_(\d{1,6})\]")   # built-in kinds and the owner's own (custom rules)

# ---- patterns (ordered: most specific first) ------------------------------------------------------------

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,30}PRIVATE KEY-----|\Z)", re.S)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_KEY_SHAPES = re.compile(
    r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}")
_LABEL_KEY = re.compile(r"(\b(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|key|token|secret)\b\s*(?:[:=]|is)\s*[\"']?)"
                        r"([A-Za-z0-9+/_=.-]{20,})", re.I)
_LABEL_PASS = re.compile(r"(\b(?:password|passwd|pwd)\b\s*(?:[:=]|is)\s*[\"']?)([^\s\"',;]{4,})", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_AADHAAR = re.compile(r"(?<![\d-])(?<!\d[ -])[2-9]\d{3}[ -]\d{4}[ -]\d{4}(?![\d-])(?![ -]\d)|(?<![\d.-])[2-9]\d{11}(?![\d.-])")
_PAN = re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")
_PHONE = re.compile(r"(?<![\w+.-])(?:\+91[ -]?|91[ -]|0)?[6-9]\d{4}[ -]?\d{5}(?![\w.-])"
                    r"|(?<![\w+])\+\d{1,3}[ -]?\(?\d{2,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}(?![\w-])")

_VERHOEFF_D = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
               (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
               (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
               (9, 8, 7, 6, 5, 4, 3, 2, 1, 0))
_VERHOEFF_P = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
               (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
               (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8))


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_ok(text: str) -> bool:
    s = text.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _card_ok(m: str) -> bool:
    d = _digits(m)
    return 13 <= len(d) <= 19 and luhn_ok(d) and len(set(d)) > 1


def _aadhaar_ok(m: str) -> bool:
    d = _digits(m)
    if len(d) != 12:
        return False
    return (" " in m or "-" in m) or verhoeff_ok(d)


def _phone_ok(m: str) -> bool:
    d = _digits(m)
    return 10 <= len(d) <= 15 and len(set(d)) > 2


class Redactor:
    """One turn's placeholder map. Same value -> same placeholder; restore() undoes it."""

    def __init__(self, kinds=KINDS, names=(), custom=()):
        self.kinds = frozenset(k for k in (kinds or ()) if k in KINDS)
        # the owner's own patterns (Settings > My redaction rules): [(compiled pattern, placeholder kind in lower case)]
        self._custom = [(p, k) for p, k in (custom or ()) if isinstance(k, str) and CUSTOM_KIND.match(k.upper())]
        self.kinds |= frozenset(k for _p, k in self._custom)
        words = []
        for n in (names or ())[:MAX_NAMES]:
            n = str(n).strip()
            if 2 <= len(n) <= 60 and n.lower() not in (w.lower() for w in words):
                words.append(n)
        self._names = None
        if words and "name" in self.kinds:
            words.sort(key=len, reverse=True)
            self._names = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I)
        self._fwd: dict[tuple[str, str], str] = {}
        self._back: dict[str, str] = {}
        self._next: Counter = Counter()
        self._reserved: dict[str, set[int]] = {}
        self.counts: Counter = Counter()
        self._known_re = None
        self._known_map: dict = {}
        self._known_n = -1
        self._lock = threading.Lock()

    # -- redact -------------------------------------------------------------------------------------

    def _ph(self, kind: str, value: str) -> str:
        key = (kind, value.lower() if kind in ("email", "name") else value)
        hit = self._fwd.get(key)
        self.counts[kind] += 1
        if hit:
            return hit
        reserved = self._reserved.get(kind, ())
        n = self._next[kind] + 1
        while n in reserved:
            n += 1
        self._next[kind] = n
        ph = f"[{PREFIX.get(kind) or kind.upper()}_{n}]"
        self._fwd[key] = ph
        self._back[ph] = value
        return ph

    def _sub(self, pattern, kind: str, text: str, ok: Callable[[str], bool] | None = None, group: int = 0) -> str:
        if kind not in self.kinds:
            return text

        def repl(m):
            whole = m.group(0)
            val = m.group(group) if group else whole
            if ok is not None and not ok(val):
                return whole
            ph = self._ph(kind, val)
            return whole[:m.start(group) - m.start(0)] + ph + whole[m.end(group) - m.start(0):] if group else ph
        return pattern.sub(repl, text)

    def redact(self, text: str) -> str:
        if not text or not self.kinds or not isinstance(text, str):
            return text
        with self._lock:
            for m in _PH_RE.finditer(text):          # a literal "[EMAIL_1]" in the input keeps its meaning
                self._reserved.setdefault(m.group(1).lower(), set()).add(int(m.group(2)))
            for pattern, kind in self._custom:       # the owner's own words and patterns first: they win over the built-ins
                text = self._sub(pattern, kind, text)
            if "-----BEGIN" in text:
                text = self._sub(_PRIVATE_KEY, "key", text)
            if "eyJ" in text:
                text = self._sub(_JWT, "key", text)
            if "sk-" in text or "AIza" in text or "gh" in text or "AKIA" in text or "xox" in text:
                text = self._sub(_KEY_SHAPES, "key", text, _not_placeholder)
            low = text.lower()
            if "key" in low or "token" in low or "secret" in low:
                text = self._sub(_LABEL_KEY, "key", text, _has_digit_and_alpha, group=2)
            if "pass" in low or "pwd" in low:
                text = self._sub(_LABEL_PASS, "password", text, _not_placeholder, group=2)
            if "@" in text:
                text = self._sub(_EMAIL, "email", text)
            if any(c.isdigit() for c in text):
                if re.search(r"[A-Z]{2}\d{2}", text):
                    text = self._sub(_IBAN, "iban", text, iban_ok)
                text = self._sub(_CARD, "card", text, _card_ok)
                text = self._sub(_AADHAAR, "aadhaar", text, _aadhaar_ok)
                if re.search(r"[A-Z]{5}\d{4}", text):
                    text = self._sub(_PAN, "pan", text)
                text = self._sub(_PHONE, "phone", text, _phone_ok)
            if self._names is not None:
                text = self._sub(self._names, "name", text)
            return self._known(text)

    def _known(self, text: str) -> str:
        """A value already hidden this turn is hidden wherever it shows up again (a tool echoing a path or an
        error message), also in its JSON-escaped form."""
        if len(self._fwd) > MAX_KNOWN or not self._fwd:
            return text
        if self._known_re is None or self._known_n != len(self._fwd):
            forms: dict[str, tuple[str, str]] = {}
            for (kind, value), ph in self._fwd.items():
                if kind == "name" or not 5 <= len(value) <= 300:
                    continue
                forms[value] = (kind, ph)
                esc = json.dumps(value)[1:-1]
                if esc != value:
                    forms.setdefault(esc, (kind, ph))
            self._known_map = forms
            self._known_re = re.compile("|".join(re.escape(v) for v in sorted(forms, key=len, reverse=True))) if forms else False
            self._known_n = len(self._fwd)
        if not self._known_re:
            return text

        def repl(m):
            kind, ph = self._known_map[m.group(0)]
            self.counts[kind] += 1
            return ph
        return self._known_re.sub(repl, text)

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    # -- restore ------------------------------------------------------------------------------------

    def restore(self, text: str, json_safe: bool = False) -> str:
        if not text or not self._back or not isinstance(text, str) or "[" not in text:
            return text

        def repl(m):
            ph = f"[{m.group(1).upper()}_{int(m.group(2))}]"
            orig = self._back.get(ph)
            if orig is None:
                return m.group(0)
            return json.dumps(orig)[1:-1] if json_safe else orig
        return _PH_RE.sub(repl, text)

    def restore_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, dict):
            return {k: self.restore_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.restore_obj(v) for v in obj]
        return obj

    def restorer(self) -> "StreamRestorer":
        return StreamRestorer(self)

    def clear(self) -> None:
        with self._lock:
            self._fwd.clear()
            self._back.clear()
            self._known_re, self._known_n = None, -1
            self._reserved.clear()

    def summary(self) -> dict[str, int]:
        return {k: v for k, v in self.counts.items() if v}

    def redact_messages(self, messages: list) -> list:
        """Anthropic-style message list -> a redacted copy. Text, tool_use input and tool_result content are
        changed; thinking and other opaque blocks are passed through untouched."""
        out = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                new = self.redact(content)
                out.append(msg if new == content else {**msg, "content": new})
            elif isinstance(content, list):
                blocks = [self._block(b) for b in content]
                out.append(msg if all(x is y for x, y in zip(blocks, content)) else {**msg, "content": blocks})
            else:
                out.append(msg)
        return out

    def _block(self, b: Any) -> Any:
        kind = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        get = (lambda k, d=None: b.get(k, d)) if isinstance(b, dict) else (lambda k, d=None: getattr(b, k, d))
        if kind == "text":
            old = get("text") or ""
            new = self.redact(old)
            return b if new == old else {"type": "text", "text": new}
        if kind == "tool_use":
            old = get("input") or {}
            new = self.redact_obj(old)
            return b if new == old else {"type": "tool_use", "id": get("id"), "name": get("name"), "input": new}
        if kind == "tool_result":
            old = get("content")
            new = self.redact_obj(old)
            if new == old:
                return b
            item = {"type": "tool_result", "tool_use_id": get("tool_use_id"), "content": new}
            if get("is_error"):
                item["is_error"] = True
            return item
        return b


def _not_placeholder(v: str) -> bool:
    return not _PH_RE.fullmatch(v)


def _has_digit_and_alpha(v: str) -> bool:
    return any(c.isdigit() for c in v) and any(c.isalpha() for c in v) and _not_placeholder(v)


class StreamRestorer:
    """Restores placeholders in streamed text, holding back a half-written "[EMAIL_" at a chunk edge. Inside a
    <tool_call> block the original is JSON-escaped so the call still parses."""

    def __init__(self, rd: Redactor):
        self.rd, self._buf, self._inside = rd, "", False

    def _emit(self, text: str) -> str:
        out = []
        for part in _MARK_RE.split(text):
            if part in (TOOL_OPEN, TOOL_CLOSE):
                self._inside = part == TOOL_OPEN
                out.append(part)
            else:
                out.append(self.rd.restore(part, json_safe=self._inside))
        return "".join(out)

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        buf = self._buf
        keep = 0
        at = buf.rfind("[")
        if at >= 0 and "]" not in buf[at:] and len(buf) - at <= 16 and re.fullmatch(r"\[[A-Za-z_0-9]*", buf[at:]):
            keep = len(buf) - at
        for size in range(min(len(TOOL_CLOSE) - 1, len(buf)), keep, -1):     # a half-written <tool_call> tag
            tail = buf[-size:]
            if TOOL_OPEN.startswith(tail) or TOOL_CLOSE.startswith(tail):
                keep = size
                break
        out, self._buf = buf[:len(buf) - keep], buf[len(buf) - keep:]
        return self._emit(out) if out else ""

    def finish(self) -> str:
        out, self._buf = self._buf, ""
        return self._emit(out) if out else ""


# ---- config + per-turn state ----------------------------------------------------------------------------------

def enabled(config) -> bool:
    return getattr(config, "redact_enabled", True) is not False


# ---- the owner's own rules (redact_rules.json in the data folder) ----------------------------------------------------
# {label, pattern (plain words, one per line or comma-separated, or a regular expression when regex is true),
#  kind (the placeholder, e.g. PAYROLL -> [PAYROLL_1]), enabled}. Validated by hub/safepattern.py.

def rules_path(config):
    return config.data_path() / "redact_rules.json"


def _split_words(pattern: str) -> list[str]:
    return re.split(r"[,\n]", pattern)


def clean_rule(raw: Any) -> dict:
    """One rule, validated. ValueError (a plain sentence) when it cannot be used."""
    from .safepattern import PatternError, compile_pattern, words_pattern
    if not isinstance(raw, dict):
        raise ValueError("A rule must be an object.")
    label = " ".join(str(raw.get("label") or "").split())[:CUSTOM_LABEL_MAX]
    pattern = str(raw.get("pattern") or "").strip()
    regex = raw.get("regex") is True
    kind = str(raw.get("kind") or "").strip().upper() or re.sub(r"[^A-Z0-9]", "", label.upper())[:12]
    if not label:
        raise ValueError("Give the rule a label.")
    if not CUSTOM_KIND.match(kind):
        raise ValueError("The placeholder must be 2 to 16 capital letters or digits, starting with a letter (like PAYROLL).")
    if kind in PREFIX.values():
        raise ValueError(f"{kind} is already used by a built-in kind; choose another placeholder.")
    try:
        if regex:
            compile_pattern(pattern, True)
        else:
            words_pattern(_split_words(pattern))
    except PatternError as exc:
        raise ValueError(str(exc)) from None
    return {"label": label, "pattern": pattern, "regex": regex, "kind": kind, "enabled": raw.get("enabled") is not False}


def clean_rules(items: Any) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("The rules must be a list.")
    if len(items) > MAX_CUSTOM_RULES:
        raise ValueError(f"At most {MAX_CUSTOM_RULES} rules.")
    out = [clean_rule(i) for i in items]
    for n, rule in enumerate(out):
        rule["id"] = f"r{n + 1}"
    return out


def load_rules(config) -> list[dict]:
    try:
        data = json.loads(rules_path(config).read_text(encoding="utf-8"))
        return clean_rules(data.get("rules") if isinstance(data, dict) else [])
    except (OSError, ValueError, AttributeError):
        return []


def save_rules(config, items: Any) -> list[dict]:
    import os
    clean = clean_rules(items)
    path = rules_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "rules": clean}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return clean


def compile_rules(rules: list[dict]) -> list[tuple]:
    """[(compiled pattern, kind in lower case)] for the enabled rules in the list. Bad ones are skipped."""
    from .safepattern import PatternError, compile_pattern, words_pattern
    out = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        try:
            pat = compile_pattern(rule["pattern"], True) if rule["regex"] else words_pattern(_split_words(rule["pattern"]))
        except PatternError:
            continue
        out.append((pat, rule["kind"].lower()))
    return out


def compiled_rules(config) -> list[tuple]:
    try:
        return compile_rules(load_rules(config))
    except Exception:  # noqa: BLE001 - a config without a data folder just has no custom rules
        return []


def make(config) -> Redactor | None:
    if not enabled(config):
        return None
    kinds = getattr(config, "redact_kinds", None)
    kinds = KINDS if kinds is None else kinds
    return Redactor(kinds, getattr(config, "redact_names", None) or (), compiled_rules(config))


_TURN: ContextVar["Redactor | None"] = ContextVar("redact_turn", default=None)


def begin_turn(config) -> Redactor | None:
    rd = make(config)
    _TURN.set(rd)
    return rd


def current() -> Redactor | None:
    return _TURN.get()


def end_turn(audit: Callable[..., None] | None = None, **fields) -> None:
    """Drop the map. Audits only the COUNTS per kind."""
    rd = _TURN.get()
    _TURN.set(None)
    if rd is None:
        return
    counts = rd.summary()
    rd.clear()
    if counts and audit:
        try:
            audit("redaction", counts=counts, **fields)
        except Exception:  # noqa: BLE001  auditing must never break a turn
            pass


def preview(config, text: str) -> dict:
    """What would be hidden in `text`: the redacted text and counts per kind (nothing is stored)."""
    rd = make(config)
    if rd is None:
        return {"enabled": False, "redacted": text, "counts": {}, "total": 0}
    out = rd.redact(text)
    counts = rd.summary()
    return {"enabled": True, "redacted": out, "counts": counts, "total": sum(counts.values())}


class Guarded:
    """A cloud client wrapped for privacy and usage: redacts what goes out, restores what comes back, counts the
    request. Everything else (has_key, model, supervisor...) passes straight through to the real client."""

    def __init__(self, inner, config, audit=None, count_as: str | None = None, redact_text: bool = True):
        self._inner = inner
        self._config = config
        self._audit = audit
        self._count_as = count_as or getattr(inner, "name", "") or ""
        self._redact = redact_text

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name == "ask_image" and callable(attr):
            return self._ask_image
        return attr

    @property
    def name(self):
        return getattr(self._inner, "name", self._count_as)

    def _begin(self):
        from . import usage
        usage.record(self._config, self._count_as)
        if not self._redact:
            return None, False
        rd = current()
        if rd is not None:
            return rd, False
        rd = make(self._config)
        return rd, rd is not None

    def _done(self, rd, owned):
        if owned and rd is not None:
            counts = rd.summary()
            rd.clear()
            if counts and self._audit:
                try:
                    self._audit("redaction", counts=counts, provider=self._count_as)
                except Exception:  # noqa: BLE001
                    pass

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        rd, owned = self._begin()
        try:
            if rd is None:
                return await self._inner.complete(system, user, json_mode)
            out = await self._inner.complete(rd.redact(system), rd.redact(user), json_mode)
            return rd.restore(out, json_safe=json_mode)
        finally:
            self._done(rd, owned)

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        rd, owned = self._begin()
        try:
            if rd is None:
                async for piece in self._inner.stream(system, user):
                    yield piece
                return
            rs = rd.restorer()
            async for piece in self._inner.stream(rd.redact(system), rd.redact(user)):
                out = rs.feed(piece)
                if out:
                    yield out
            out = rs.finish()
            if out:
                yield out
        finally:
            self._done(rd, owned)

    async def _ask_image(self, system: str, question: str, image: bytes, mime: str = "image/jpeg") -> str:
        # The picture itself cannot be redacted; only the words around it are.
        rd, owned = self._begin()
        try:
            if rd is None:
                return await self._inner.ask_image(system, question, image, mime)
            return rd.restore(await self._inner.ask_image(rd.redact(system), rd.redact(question), image, mime))
        finally:
            self._done(rd, owned)
