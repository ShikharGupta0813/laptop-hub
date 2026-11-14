"""Jarvis and Friday: two voices on one assistant (and any others the owner adds in Settings > Assistants).

Jarvis is the laptop operator (Talk, terminal, files). Friday is the day assistant (briefs, mail, plans).
A message that starts with a name ("Jarvis, run the tests", "hey Friday what's my day") is routed to that
persona and the name is removed from the text. The prompts stay short on purpose: every token costs latency.

The names, prompts and which assistants exist come from the owner's list (hub/assistants.py). Every function takes
that list as `assistants`; without it the two built-in assistants apply, exactly as before the list existed.
"""
from __future__ import annotations

import datetime
import re

PERSONAS = ("jarvis", "friday")      # the two ready-made assistants; the owner's list may hold more
_NOT_A_NAME = r"(?:at|is|was|we|i|the|morning|afternoon|evening|night|next|this|last|after|before|during|off|and)"

# The built-in prompts. {name} and {call} are filled in; a custom role prompt may use them too.
JARVIS_PROMPT = ("You are {name}, the owner's laptop operator: terminal, files, apps, system. Precise and "
                 "calm; lead with the result; dry wit only if free. "
                 "For technical questions (RCA, decisions, the fix, can this be done) use recall first; help with technical answers for applications. "
                 "Routine work: run_in_lane. "
                 "Call the owner \"{call}\".")
FRIDAY_PROMPT = ("You are {name}, the owner's day assistant: briefs, mail, calendar, plans, reminders. Warm, quick and "
                 "proactive; lead with what matters today. For people, commitments, \"who asked me what\" and \"tell me tomorrow\" "
                 "use recall and the follow-ups list, and mention due follow-ups in the brief. You also own applications and urgent alerts (list_ready_applications, list_alerts, draft_reply); nothing is sent without the owner's tap. Routine work: run_in_lane. Call the owner \"{call}\".")
GENERIC_PROMPT = ("You are {name}, one of the owner's assistants. Be brief and useful; lead with the result. "
                  "Call the owner \"{call}\".")


def default_prompt(persona_id: str) -> str:
    """The built-in prompt text (with {name} and {call}) for an assistant id, for the Settings editor."""
    return {"jarvis": JARVIS_PROMPT, "friday": FRIDAY_PROMPT}.get(persona_id, GENERIC_PROMPT)


def _fill(text: str, name: str, call: str) -> str:
    return text.replace("{name}", name).replace("{call}", call)


def _pattern(names: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    # name, then punctuation or end of text; or whitespace and a word that does not make it a day-of-week phrase
    return re.compile(
        rf"^\s*(?:(?:hey|hi|ok|okay|yo)[,\s]+)?(?P<name>{alt})(?![\w'’])"
        rf"(?:(?P<punct>[\s]*[,:;.!?\-—]+\s*)|(?P<space>\s+)(?!{_NOT_A_NAME}\b)|\s*$)",
        re.I)


def _table(assistants) -> dict[str, str]:
    from .assistants import wake_table
    return wake_table(assistants)


def split_persona(text: str, jarvis_name: str = "Jarvis", assistants=None) -> tuple[str, str]:
    """(persona, text without the leading name). persona is "" when the text names nobody.

    `assistants` is the owner's list (hub/assistants.py); without it the two built-in names apply."""
    raw = str(text or "")
    if assistants is not None:
        table = _table(assistants)
        if not table:
            return "", raw.strip()
        names = tuple(table)
        m = _pattern(names).match(raw)
        if m:
            return table[m.group("name").lower()], raw[m.end():].strip()
        tail = re.search(r",\s*(?P<name>" + "|".join(re.escape(n) for n in names) + r")\s*[.!?]*\s*$", raw, re.I)
        if tail:   # "run the tests, Jarvis"
            return table[tail.group("name").lower()], raw[:tail.start()].strip()
        return "", raw.strip()
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


def clean_persona(value, assistants=None) -> str:
    v = str(value or "").strip().lower()
    if assistants is None:
        return v if v in PERSONAS else ""
    from .assistants import ids
    return v if v in ids(assistants) or v in PERSONAS else ""


def persona_prompt(persona: str, call_sign: str, jarvis_name: str = "Jarvis", assistants=None) -> str:
    """A few lines that set the voice. Added in front of the working instructions."""
    call = (call_sign or "Chief").strip()
    entry = next((a for a in (assistants or []) if a["id"] == persona), None)
    if entry is not None:
        name = entry["name"]
        if entry.get("role_prompt"):
            return _fill(entry["role_prompt"], name, call)
        text = _fill(default_prompt(entry["id"]), name, call)
        if entry["id"] not in PERSONAS and entry.get("tool_groups"):
            text += " You focus on: " + ", ".join(entry["tool_groups"]) + "."
        return text
    if persona == "friday":
        return _fill(FRIDAY_PROMPT, "Friday", call)
    return _fill(JARVIS_PROMPT, jarvis_name or "Jarvis", call)


_GREETING = re.compile(
    r"^\s*(?:(?:hey|hi|hello|hola|yo|ok|okay|good\s+(?:morning|afternoon|evening|night))[,\s!.]*)*"
    r"(?:{names})?[\s,!.?]*(?:are\s+you\s+there|you\s+there|wake\s+up|thanks?|thank\s+you)?[\s,!.?]*$", re.I)


def day_part(now: datetime.datetime) -> str:
    return "morning" if 5 <= now.hour < 12 else "afternoon" if 12 <= now.hour < 17 else "evening"


def greeting_reply(text: str, persona: str = "", call_sign: str = "Chief", jarvis_name: str = "Jarvis",
                   clock=None, assistants=None) -> str:
    """A short fixed answer when the whole message is just a hello (or only a name). "" when it is anything else.

    No model and no tools are involved, so a greeting can never open files or ask for approvals."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 60:
        return ""
    table = _table(assistants) if assistants is not None else {}
    base = tuple(table) if table else ("jarvis", (jarvis_name or "Jarvis").strip().lower(), "friday")
    names = "|".join(re.escape(n) for n in dict.fromkeys(base) if n)
    if not re.fullmatch(_GREETING.pattern.replace("{names}", names), raw, re.I):
        return ""
    if table:
        who = persona or next((table[w] for w in table if re.search(rf"\b{re.escape(w)}\b", raw, re.I)), "jarvis")
    else:
        who = persona or ("friday" if "friday" in raw.lower() else "jarvis")
    call = (call_sign or "Chief").strip()
    if re.search(r"thank", raw, re.I):
        return "Any time."
    if who == "friday":
        now = (clock or datetime.datetime.now)()
        return f"Good {day_part(now)}, {call}! Want a look at your day, or shall I get something done?"
    return f"At your service, {call}. What do you need?"
