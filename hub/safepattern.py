"""Owner-written patterns (custom safety rules, custom redaction): validate them and match them safely.

The owner may type a plain word or a regular expression. Python's `re` cannot be interrupted, so safety here is
by construction rather than by a timer:

- a length limit (MAX_PATTERN characters),
- the pattern must compile,
- shapes that can backtrack without limit are refused: a repeat inside a repeat (`(a+)+`, `(.*)*`, `(\\w+\\s?)*`),
  a repeat over alternatives (`(a|aa)+`), back-references, and big counted repeats (`a{1,5000}`),
- every match runs on a bounded subject (MAX_SUBJECT characters), so even a slow shape stays quick.

A plain pattern is escaped and matched as a case-insensitive substring (slashes and backslashes count as the same).
"""
from __future__ import annotations

import re
from typing import Any

try:                                    # Python 3.11+ moved the parser
    from re import _constants as _c, _parser as _p
except ImportError:                     # pragma: no cover
    import sre_constants as _c          # type: ignore
    import sre_parse as _p              # type: ignore

MAX_PATTERN = 200
MAX_SUBJECT = 4000
MAX_COUNT = 100
_UNBOUNDED = _c.MAXREPEAT


class PatternError(ValueError):
    """The pattern is not acceptable; the message is one plain sentence for the owner."""


def _repeats(tree) -> list:
    return [item for item in tree if item[0] in (_c.MAX_REPEAT, _c.MIN_REPEAT)]


def _walk(tree, in_repeat: bool) -> str | None:
    for op, av in tree:
        if op in (_c.MAX_REPEAT, _c.MIN_REPEAT):
            lo, hi, sub = av
            if hi != _UNBOUNDED and hi > MAX_COUNT:
                return "A counted repeat is too large (at most %d)." % MAX_COUNT
            wide = hi == _UNBOUNDED or hi > 1
            if in_repeat and wide:
                return "A repeat inside a repeat can hang the hub."
            bad = _walk(sub, in_repeat or wide)
            if bad:
                return bad
        elif op == _c.BRANCH:
            if in_repeat:
                return "Alternatives inside a repeat can hang the hub."
            for branch in av[1]:
                bad = _walk(branch, in_repeat)
                if bad:
                    return bad
        elif op == _c.SUBPATTERN:
            bad = _walk(av[3], in_repeat)
            if bad:
                return bad
        elif op in (_c.ASSERT, _c.ASSERT_NOT):
            bad = _walk(av[1], in_repeat)
            if bad:
                return bad
        elif op == _c.GROUPREF:
            return "Back-references are not allowed."
        elif op == getattr(_c, "ATOMIC_GROUP", None):
            bad = _walk(av, in_repeat)
            if bad:
                return bad
    return None


def compile_pattern(pattern: Any, regex: bool = False) -> re.Pattern:
    """A compiled, case-insensitive pattern, or PatternError."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise PatternError("The pattern is empty.")
    if len(pattern) > MAX_PATTERN:
        raise PatternError(f"The pattern is too long (at most {MAX_PATTERN} characters).")
    if not regex:
        text = pattern.strip().replace("\\", "/")
        return re.compile(re.escape(text).replace("\\/", "/"), re.I)
    try:
        tree = _p.parse(pattern, re.I)
    except (re.error, RecursionError, OverflowError) as exc:
        raise PatternError(f"That is not a valid pattern: {exc}") from None
    bad = _walk(tree, False)
    if bad:
        raise PatternError(bad)
    try:
        return re.compile(pattern, re.I)
    except (re.error, RecursionError, OverflowError) as exc:
        raise PatternError(f"That is not a valid pattern: {exc}") from None


def words_pattern(words: list[str]) -> re.Pattern:
    """Whole-word, case-insensitive match of any of the words (plain text, escaped)."""
    clean = [w.strip() for w in words if isinstance(w, str) and w.strip()]
    if not clean:
        raise PatternError("Give at least one word.")
    if sum(len(w) for w in clean) > MAX_PATTERN * 3:
        raise PatternError("Too many words in one rule.")
    clean.sort(key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in clean) + r")(?!\w)", re.I)


def search(pattern: re.Pattern, subject: Any, plain: bool = False) -> bool:
    """True when the pattern matches (a bounded part of) the subject. Never raises."""
    text = str(subject if subject is not None else "")[:MAX_SUBJECT]
    if plain:
        text = text.replace("\\", "/")
    try:
        return pattern.search(text) is not None
    except (re.error, RecursionError):
        return False
