"""ASSISTANTS: the list of voices the owner talks to. Jarvis and Friday are the two ready-made entries. See docs/api/assistants.md.

Each assistant has an id (never changes), a display name, wake names and mis-hearing variants ("jarves" for
"jarvis"), an optional role prompt (empty = the built-in prompt for Jarvis and Friday, or a plain one for a new
assistant), voice settings for the speaker, the tool groups it focuses on, a colour accent and an on/off switch.
The owner can rename them, add more (for example "Atlas" for study) and delete or switch off any, but at least one
must stay on. The list lives in `assistants.json` in the hub data folder; nothing is written until the owner edits.

Wake matching (browser js/wakephrase.js, laptop hub/wakeword.py), routing "Jarvis, ..." (hub/persona.py) and the
persona prompt all read this one list. Without a file, or when it is broken, the two defaults apply, so the
behaviour is exactly what it was before the list existed.

Routes (login needed): GET /api/assistants, POST /api/assistants, PATCH /api/assistants/{id},
DELETE /api/assistants/{id}, POST /api/assistants/reset.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .common import Ctx, JsonFile

log = logging.getLogger("hub.assistants")

ACCENTS = ("cyan", "amber", "green", "violet", "white")
MAX_ASSISTANTS = 8
MAX_NAMES = 12
MAX_PROMPT = 1500
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
WORD_RE = re.compile(r"^[a-z0-9][a-z0-9'’-]{1,29}$")
# a name followed by one of these reads as an ordinary sentence ("friday evening"), so it can never be a wake name
RESERVED_WORDS = frozenset("""at is was we i the morning afternoon evening night next this last after before during off and
hey hi ok okay yo a an it to of in on you me my""".split())
BUILTIN_IDS = ("jarvis", "friday")

DEFAULTS: tuple[dict, ...] = (
    {"id": "jarvis", "name": "Jarvis", "wake_names": ["jarvis"], "heard_as": ["jarves", "jarvas"], "role_prompt": "",
     "voice": {"rate": 1.0, "pitch": 1.0, "voice": ""}, "tool_groups": [], "accent": "cyan", "enabled": True},
    {"id": "friday", "name": "Friday", "wake_names": ["friday"], "heard_as": [], "role_prompt": "",
     "voice": {"rate": 1.0, "pitch": 1.0, "voice": ""}, "tool_groups": [], "accent": "amber", "enabled": True},
)


class AssistantError(ValueError):
    """Something the owner can fix; the message is one plain sentence."""


def defaults(jarvis_name: str = "Jarvis") -> list[dict]:
    out = [{**d, "wake_names": list(d["wake_names"]), "heard_as": list(d["heard_as"]), "tool_groups": list(d["tool_groups"]),
            "voice": dict(d["voice"])} for d in DEFAULTS]
    jn = str(jarvis_name or "Jarvis").strip()[:40] or "Jarvis"
    if jn.lower() != "jarvis":                 # the older single setting (Settings > jarvis_name) still works
        out[0]["name"] = jn
        out[0]["wake_names"] = [w for w in dict.fromkeys(["jarvis", jn.lower()]) if WORD_RE.match(w)]
    return out


def tool_group_names() -> list[str]:
    try:
        from .assistant import TOOL_GROUPS
        return sorted(TOOL_GROUPS)
    except Exception:  # noqa: BLE001
        return []


def _word(value: Any) -> str:
    return str(value or "").strip().lower()


def _words(value: Any, what: str) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, (list, tuple)):
        raise AssistantError(f"{what} must be a list of words.")
    out: list[str] = []
    for raw in value:
        w = _word(raw)
        if not w:
            continue
        if not WORD_RE.match(w):
            raise AssistantError(f"'{str(raw)[:30]}' cannot be used in {what}: one word, letters and digits.")
        if w in RESERVED_WORDS:
            raise AssistantError(f"'{w}' is too common a word to wake on.")
        if w not in out:
            out.append(w)
    if len(out) > MAX_NAMES:
        raise AssistantError(f"At most {MAX_NAMES} {what}.")
    return out


def _first_word(name: str) -> str:
    w = _word(str(name).split()[0]) if str(name).split() else ""
    return w if WORD_RE.match(w) and w not in RESERVED_WORDS else ""


def all_words(a: dict) -> list[str]:
    """Every word that wakes this assistant: its name, wake names and mis-hearings."""
    return list(dict.fromkeys([*a["wake_names"], *a.get("heard_as", []), *([_first_word(a["name"])] if _first_word(a["name"]) else [])]))


def clean_entry(raw: Any, *, known_groups: list[str] | None = None) -> dict:
    """One entry, validated and normalised. AssistantError when it cannot be used."""
    if not isinstance(raw, dict):
        raise AssistantError("An assistant must be an object.")
    aid = _word(raw.get("id"))
    if not ID_RE.match(aid):
        raise AssistantError("The id must be 2 to 24 characters: letters, digits, - or _, starting with a letter.")
    name = " ".join(str(raw.get("name") or "").split())[:40]
    if not name or not re.match(r"^\w[\w '’.-]{0,39}$", name):
        raise AssistantError("Give the assistant a name (letters and digits, up to 40 characters).")
    wake = _words(raw.get("wake_names") if raw.get("wake_names") is not None else [], "wake names")
    if not wake and _first_word(name):
        wake = [_first_word(name)]
    if not wake:
        raise AssistantError("Give at least one wake name.")
    heard = [w for w in _words(raw.get("heard_as") or [], "variants") if w not in wake]
    prompt = str(raw.get("role_prompt") or "").replace("\x00", "").strip()
    if len(prompt) > MAX_PROMPT:
        raise AssistantError(f"The role prompt is too long (at most {MAX_PROMPT} characters).")
    voice_in = raw.get("voice") if isinstance(raw.get("voice"), dict) else {}
    try:
        rate = float(voice_in.get("rate", 1.0))
        pitch = float(voice_in.get("pitch", 1.0))
    except (TypeError, ValueError):
        raise AssistantError("Voice rate and pitch must be numbers.") from None
    if not (0.5 <= rate <= 2.0 and 0.0 <= pitch <= 2.0):
        raise AssistantError("Voice rate must be 0.5 to 2 and pitch 0 to 2.")
    voice = {"rate": round(rate, 2), "pitch": round(pitch, 2), "voice": " ".join(str(voice_in.get("voice") or "").split())[:80]}
    groups = raw.get("tool_groups") or []
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise AssistantError("tool_groups must be a list of group names.")
    known = known_groups if known_groups is not None else tool_group_names()
    groups = list(dict.fromkeys(groups))
    for g in groups:
        if known and g not in known:
            raise AssistantError(f"There is no tool group called '{g[:30]}'.")
    accent = _word(raw.get("accent")) or "cyan"
    if accent not in ACCENTS:
        raise AssistantError("accent must be one of: " + ", ".join(ACCENTS) + ".")
    return {"id": aid, "name": name, "wake_names": wake, "heard_as": heard, "role_prompt": prompt, "voice": voice,
            "tool_groups": groups, "accent": accent, "enabled": raw.get("enabled") is not False}


def clean_list(items: Any) -> list[dict]:
    """A whole list, validated together: unique ids, no wake word shared by two enabled assistants, one stays on."""
    if not isinstance(items, list) or not items:
        raise AssistantError("There must be at least one assistant.")
    if len(items) > MAX_ASSISTANTS:
        raise AssistantError(f"At most {MAX_ASSISTANTS} assistants.")
    groups = tool_group_names()
    out = [clean_entry(i, known_groups=groups) for i in items]
    ids = [a["id"] for a in out]
    if len(set(ids)) != len(ids):
        raise AssistantError("Two assistants have the same id.")
    if not any(a["enabled"] for a in out):
        raise AssistantError("At least one assistant must stay switched on.")
    owner: dict[str, str] = {}
    for a in out:
        if not a["enabled"]:
            continue
        for w in all_words(a):
            if owner.setdefault(w, a["id"]) != a["id"]:
                other = next(x["name"] for x in out if x["id"] == owner[w])
                raise AssistantError(f"'{w}' is already used by {other}.")
    return out


# ---- store ---------------------------------------------------------------------------------------------------

def _file(config) -> JsonFile:
    return JsonFile(config.data_path() / "assistants.json")


def stored(config) -> list[dict] | None:
    """The saved list, or None when nothing valid is saved (then the defaults apply)."""
    data = _file(config).load(None)
    items = data.get("assistants") if isinstance(data, dict) else None
    if not items:
        return None
    try:
        return clean_list(items)
    except AssistantError:
        log.warning("assistants.json is not valid; using the defaults")
        return None


def load(config) -> list[dict]:
    """All assistants (enabled or not). Never raises: the defaults are the fallback."""
    try:
        saved = stored(config)
    except Exception:  # noqa: BLE001
        saved = None
    jn = str(getattr(config, "jarvis_name", "Jarvis") or "Jarvis")
    if saved is None:
        return defaults(jn)
    if jn.strip().lower() != "jarvis":          # the older jarvis_name setting still renames an untouched Jarvis
        for a in saved:
            if a["id"] == "jarvis" and a["name"] == "Jarvis" and a["wake_names"] == ["jarvis"]:
                a["name"] = jn.strip()[:40]
                a["wake_names"] = [w for w in dict.fromkeys(["jarvis", jn.strip().lower()]) if WORD_RE.match(w)]
    return saved


def save(config, items: list[dict]) -> list[dict]:
    items = clean_list(items)
    _file(config).save({"version": 1, "assistants": items})
    return items


def reset(config) -> list[dict]:
    try:
        _file(config).path.unlink()
    except OSError:
        pass
    return load(config)


def active(items: list[dict]) -> list[dict]:
    return [a for a in items if a.get("enabled") is not False]


def find(items: list[dict], aid: str) -> dict | None:
    return next((a for a in items if a["id"] == aid), None)


def wake_table(items: list[dict]) -> dict[str, str]:
    """Every word that wakes an assistant (name, wake names, mis-hearings) -> its id. Enabled ones only."""
    table: dict[str, str] = {}
    for a in active(items):
        for w in all_words(a):
            table.setdefault(w, a["id"])
    return table


def ids(items: list[dict]) -> list[str]:
    return [a["id"] for a in active(items)]


def public(items: list[dict]) -> list[dict]:
    from .persona import default_prompt
    return [{**a, "builtin": a["id"] in BUILTIN_IDS, "default_prompt": default_prompt(a["id"]), "words": all_words(a)}
            for a in items]


# ---- routes --------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx) -> APIRouter:
    router = APIRouter(prefix="/api/assistants", dependencies=[Depends(ctx.need_auth)])
    config = ctx.config

    def view() -> dict:
        items = load(config)
        return {"assistants": public(items), "tool_groups": tool_group_names(), "accents": list(ACCENTS),
                "max": MAX_ASSISTANTS, "customised": stored(config) is not None}

    def commit(items: list[dict], event: str, **fields) -> dict:
        try:
            save(config, items)
        except AssistantError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(500, "The list could not be saved.") from None
        ctx.audit(event, **fields)
        return view()

    @router.get("")
    @router.get("/")
    def get_all():
        return view()

    @router.post("")
    def add(body: dict = Body(default_factory=dict)):
        items = load(config)
        if find(items, _word(body.get("id"))) is not None:
            raise HTTPException(400, "There is already an assistant with that id.")
        return commit([*items, body], "assistant_add", id=_word(body.get("id")))

    @router.post("/reset")
    def reset_all():
        reset(config)
        ctx.audit("assistants_reset")
        return view()

    @router.patch("/{aid}")
    def patch(aid: str, body: dict = Body(default_factory=dict)):
        items = load(config)
        cur = find(items, aid)
        if cur is None:
            raise HTTPException(404, "No assistant with that id.")
        body = {k: v for k, v in body.items() if k != "id"}
        merged = {**cur, **body}
        if isinstance(body.get("voice"), dict):
            merged["voice"] = {**cur["voice"], **body["voice"]}
        return commit([merged if a["id"] == aid else a for a in items], "assistant_update", id=aid, fields=sorted(body))

    @router.delete("/{aid}")
    def delete(aid: str):
        items = load(config)
        if find(items, aid) is None:
            raise HTTPException(404, "No assistant with that id.")
        rest = [a for a in items if a["id"] != aid]
        if not rest:
            raise HTTPException(400, "At least one assistant must remain.")
        return commit(rest, "assistant_delete", id=aid)

    return router
