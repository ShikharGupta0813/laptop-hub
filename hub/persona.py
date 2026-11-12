"""Jarvis and Friday: two voices on one assistant.

Jarvis is the laptop operator (Talk, terminal, files). Friday is the day assistant (briefs, mail, plans).
A message that starts with a name ("Jarvis, run the tests", "hey Friday what's my day") is routed to that
persona and the name is removed from the text. The prompts stay short on purpose: every token costs latency.
"""
from __future__ import annotations

import re

PERSONAS = ("jarvis", "friday")
_NOT_A_NAME = r"(?:at|is|was|we|i|the|morning|afternoon|evening|night|next|this|last|after|before|during|off|and)"


def _pattern(names: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(n) for n in names)
    # name, then punctuation or end of text; or whitespace and a word that does not make it a day-of-week phrase
    return re.compile(
        rf"^\s*(?:(?:hey|hi|ok|okay|yo)[,\s]+)?(?P<name>{alt})(?![\w'’])"
        rf"(?:(?P<punct>[\s]*[,:;.!?\-—]+\s*)|(?P<space>\s+)(?!{_NOT_A_NAME}\b)|\s*$)",
        re.I)


def split_persona(text: str, jarvis_name: str = "Jarvis") -> tuple[str, str]:
    """(persona, text without the leading name). persona is "" when the text names nobody."""
    raw = str(text or "")
    jname = (jarvis_name or "Jarvis").strip().lower()
    names = tuple(dict.fromkeys(n for n in ("jarvis", jname, "friday") if n))
    m = _pattern(names).match(raw)
    if not m:
        tail = re.search(r",\s*(?P<name>" + "|".join(re.escape(n) for n in names) + r")\s*[.!?]*\s*$", raw, re.I)
        if tail:   # "run the tests, Jarvis"
            return ("friday" if tail.group("name").lower() == "friday" else "jarvis"), raw[:tail.start()].strip()
        return "", raw.strip()
    name = m.group("name").lower()
    persona = "friday" if name == "friday" else "jarvis"
    return persona, raw[m.end():].strip()


def clean_persona(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in PERSONAS else ""


def persona_prompt(persona: str, call_sign: str, jarvis_name: str = "Jarvis") -> str:
    """A few lines that set the voice. Added in front of the working instructions."""
    call = (call_sign or "Chief").strip()
    if persona == "friday":
        return (f"You are Friday, the owner's day assistant: briefs, mail, calendar, plans, reminders. Warm, quick and "
                f"proactive; lead with what matters today. Call the owner \"{call}\".")
    return (f"You are {jarvis_name or 'Jarvis'}, the owner's laptop operator: terminal, files, apps, system. Precise and "
            f"calm; lead with the result, then one line of detail; dry wit only if it costs no time. "
            f"Call the owner \"{call}\".")


_GREETING = re.compile(
    r"^\s*(?:(?:hey|hi|hello|hola|yo|ok|okay|good\s+(?:morning|afternoon|evening|night))[,\s!.]*)*"
    r"(?:{names})?[\s,!.?]*(?:are\s+you\s+there|you\s+there|wake\s+up|thanks?|thank\s+you)?[\s,!.?]*$", re.I)


def greeting_reply(text: str, persona: str = "", call_sign: str = "Chief", jarvis_name: str = "Jarvis") -> str:
    """A short fixed answer when the whole message is just a hello (or only a name). "" when it is anything else.

    No model and no tools are involved, so a greeting can never open files or ask for approvals."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 60:
        return ""
    names = "|".join(re.escape(n) for n in dict.fromkeys(("jarvis", (jarvis_name or "Jarvis").strip().lower(), "friday")) if n)
    if not re.fullmatch(_GREETING.pattern.replace("{names}", names), raw, re.I):
        return ""
    who = persona or ("friday" if "friday" in raw.lower() else "jarvis")
    call = (call_sign or "Chief").strip()
    if re.search(r"thank", raw, re.I):
        return "Any time."
    if who == "friday":
        return f"Hi {call}! Want a look at your day, or shall I get something done?"
    return f"At your service, {call}. What do you need?"
