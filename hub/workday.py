"""WORKDAY: read and fill Workday application forms for hub/formfill.py. See docs/api/formfill.md ("Workday").

Workday pages are React apps with their own widgets: pop-up dropdowns (`button[aria-haspopup=listbox]`), type-to-search pickers
(chips), month/year inputs, repeatable "Work Experience" and "Education" sections, and a progress bar of steps. The generic mapper
in formfill.py only knows native inputs, so this module reads the Workday markup (`data-automation-id`) and plans each widget.

formfill.py calls this module ONLY when the address is a Workday host or the page carries Workday markers; every other site takes the
generic path untouched. Nothing here clicks Next or Submit: moving between steps and the final Submit stay the owner's tap in
formfill.py. Rules that are code, not policy:
- Voluntary Disclosures / Self Identify steps, demographics, disability, veteran, work-authorization, sponsorship, salary history,
  date of birth, ID numbers, consent and terms boxes are never filled (the step is left untouched and listed as "needs you").
- Work Experience / Education entries come only from the owner's fact base (profile, minus hidden facts). Nothing is invented;
  an entry already on the page (for example from Workday's own resume read) is matched by company + title and only its empty
  fields are filled, so nothing is added twice.
- A choice is only made when it clearly matches a listed option and the page shows it afterwards; otherwise the field is "needs you".
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterator

from . import formfill as ff

MAX_ENTRIES = 10
HOST_SUFFIXES = ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com")
MARKER = re.compile(r"""data-automation-id\s*=\s*["'](applyFlowPage|progressBar\w*|formField-[^"']*|pageFooterNextButton|bottom-navigation-next-button)["']""", re.I)
NEVER_STEP = re.compile(r"voluntary|self[- ]?identif|disclosure|diversity|equal (employment|opportunity)|demographic|disabilit|veteran|\beeo\b", re.I)
NEVER_AID = re.compile(r"selfidentif|voluntary|disabilit|veteran|ethnicit|diversity|demographic", re.I)
LOGIN_AIDS = frozenset({"signincontent", "signinsubmitbutton", "createaccountsubmitbutton", "createaccountcheckbox", "verifypassword"})
FOOTER_AIDS = frozenset({"pagefooternextbutton", "bottom-navigation-next-button", "pagefootersubmitbutton"})
SUBMIT_TEXT = frozenset({"submit", "submit application", "submit my application", "submit your application", "submit and apply"})
NEXT_TEXT = re.compile(r"^(save and continue|continue|next|next step|save and next|review and continue)$")
SOURCE_LABEL = re.compile(r"how did you hear|hear about (us|this|the)|how did you find|where did you (hear|find)|referral source|source of (your )?(application|referral)")
PLACEHOLDERS = frozenset({"", "select one", "select", "choose", "choose one", "please select", "search"})
SECTION_KINDS = {"workexperiencesection": "work", "educationsection": "education"}
OTHER_SECTIONS = frozenset({"websitesection", "websitessection", "languagesection", "languagessection", "certificationsection", "certificationssection",
                            "skillssection", "skillsection", "trainingsection", "trainingssection"})
ENTRY_RE = re.compile(r"^(workexperience|education|website\w*|language\w*|certification\w*|skills?\w*|training\w*)-\d+$", re.I)
CURRENT_WORDS = re.compile(r"present|current|now|ongoing|today", re.I)
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}

# label -> (key, what to call it when the profile does not have it). Answered ONLY from standard answers, never guessed.
SPECIAL = [
    ("phone_country_code", re.compile(r"country phone code|phone code|dial code|dialing code|country calling code|^country code$"), "country phone code"),
    ("phone_device", re.compile(r"phone device type|device type|phone type"), "phone device type"),
    ("country", re.compile(r"^country( territory| region)?( of residence)?$|^country region$"), "country"),
    ("state", re.compile(r"^(state|province|state province|state or province|region|county)$"), "state"),
    ("postal", re.compile(r"^(postal code|zip|zip code|zip postal code|post code)$"), "postal code"),
    ("address", re.compile(r"^address( line)?( \d)?$|^street( address)?$"), "street address"),
    ("city", re.compile(r"^(city|town|city town)$"), "city"),
]

WORK_KINDS = [("title", re.compile(r"jobtitle|^title$|positiontitle|roletitle"), re.compile(r"^(job )?title$|job title|position title")),
              ("company", re.compile(r"companyname|^company$|employer|organi[sz]ation"), re.compile(r"^company( name)?$|employer|organi[sz]ation")),
              ("location", re.compile(r"^location$|joblocation|worklocation"), re.compile(r"^location$")),
              ("current", re.compile(r"currentlyworkhere|currentlyemployed|iscurrent|currentrole"), re.compile(r"currently work|current(ly)? (employed|role|job)")),
              ("start", re.compile(r"startdate|^from$|datestart|fromdate"), re.compile(r"^from\b|start date")),
              ("end", re.compile(r"enddate|^to$|dateend|todate"), re.compile(r"^to\b|end date")),
              ("desc", re.compile(r"roledescription|description|responsibilit"), re.compile(r"description|responsibilit"))]
EDU_KINDS = [("school", re.compile(r"schoolname|^school$|university|institution"), re.compile(r"school|university|institution|college")),
             ("degree", re.compile(r"^degree$|degreetype|educationlevel|^degreename$"), re.compile(r"^degree$|degree type|level of education")),
             ("study", re.compile(r"fieldofstudy|^major$|discipline|studyfield"), re.compile(r"field of study|^major$|discipline")),
             ("grade", re.compile(r"gradeaverage|^gpa$|^grade$|^result$"), re.compile(r"gpa|grade|result")),
             ("start", re.compile(r"firstyearattended|startyear|^from$|fromyear"), re.compile(r"^from\b|start year|first year")),
             ("end", re.compile(r"lastyearattended|endyear|^to$|graduat|toyear"), re.compile(r"^to\b|end year|last year|graduat"))]
ENTRY_ORDER = {"work": ["title", "company", "location", "start", "current", "end", "desc"],
               "education": ["school", "degree", "study", "start", "end"]}
SECTION_TITLE = {"work": "Work Experience", "education": "Education"}


# ---- is this a Workday page ---------------------------------------------------------------------------------------

def applies(url: str, html: str = "") -> bool:
    host = ff.host_of(url or "")
    if any(host == s or host.endswith("." + s) for s in HOST_SUFFIXES):
        return True
    return bool(html) and bool(MARKER.search(html[:ff.MAX_HTML]))


# ---- reading the page ---------------------------------------------------------------------------------------------

def _walk(n: ff.Node) -> Iterator[ff.Node]:
    for c in n.children:
        if isinstance(c, str) or c.tag in ff.SKIP:        # scripts, templates and svg are not on the page
            continue
        yield c
        yield from _walk(c)


def _aid(n: ff.Node | None) -> str:
    return n.get("data-automation-id") if n is not None else ""


def _ident(aid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"^formfield-?", "", aid.lower()))


def _label_text(n: ff.Node) -> tuple[str, bool]:
    txt = ff._own_text(n)
    required = "*" in txt or any(_title_required(c) for c in _walk(n))
    txt = re.sub(r"\s*\(required\)\s*$", "", re.sub(r"\s*\*+", "", txt), flags=re.I)
    return ff._clip(txt.strip(), 120), required


def _title_required(n: ff.Node) -> bool:
    return n.tag == "abbr" and "required" in n.get("title").lower()


def _container_label(c: ff.Node, skip_for: set[str]) -> tuple[str, bool]:
    for n in _walk(c):
        if n.tag == "legend" or (n.tag == "label" and n.get("for") not in skip_for):
            text, req = _label_text(n)
            if text:
                return text, req
    for n in _walk(c):
        text = n.get("aria-label")
        if text and n.tag in ("input", "textarea", "select", "button"):
            return ff._clip(text, 120), n.get("aria-required") == "true"
    return "", False


def _steps(root: ff.Node, text: str) -> dict:
    """The progress bar -> {current, total, title}. Items are the progressBar*Step nodes; the title is their words minus the
    screen-reader boilerplate ("current step 2 of 6")."""
    items: list[tuple[str, bool]] = []
    for n in _walk(root):
        a = _aid(n).lower()
        if a in ("progressbaractivestep", "progressbarcompletedstep", "progressbarinactivestep"):
            words = ff._own_text(n)
            words = re.sub(r"^(?:(?:current|completed|complete|upcoming|inactive|active)\s+)?step\s+\d+\s+of\s+\d+\s*", "", words, flags=re.I).strip()
            words = re.sub(r"^(current|completed|upcoming)\s+", "", words, flags=re.I).strip()
            items.append((ff._clip(words, 80), a == "progressbaractivestep" or "current step" in ff._own_text(n).lower()))
    cur = total = None
    title = ""
    if items:
        total = len(items)
        for i, (words, active) in enumerate(items, 1):
            if active:
                cur, title = i, words
                break
    if cur is None:
        for rx in ff.STEP_RE:
            m = rx.search(text)
            if m and int(m.group(2)) >= int(m.group(1)) >= 1:
                cur, total = int(m.group(1)), int(m.group(2))
                break
    if not title:
        for n in _walk(root):
            if n.tag == "h2":
                title = ff._clip(ff._own_text(n), 80)
                break
    return {"current": cur, "total": total, "title": title}


def _synthetic(idx: int, kind: str, label: str, node: ff.Node, button: dict | None, required: bool) -> dict:
    return {"idx": idx, "tag": node.tag, "type": kind, "name": "", "id": node.get("id"), "placeholder": "", "autocomplete": "", "required": required,
            "disabled": bool(button and button["disabled"]), "readonly": False, "visible": bool(button["visible"]) if button else True, "maxlength": 0,
            "multiple": False, "options": [], "label": label, "signals": []}


def _kind_of(section: str, ident: str, label: str) -> str:
    table = WORK_KINDS if section == "work" else EDU_KINDS if section == "education" else []
    for kind, rx_id, _ in table:
        if ident and rx_id.search(ident):
            return kind
    lab = ff.norm(label)
    for kind, _, rx_lab in table:
        if lab and rx_lab.search(lab):
            return kind
    return "other"


def parse(page: dict, url: str = "") -> dict:
    """Pure. -> {fields[], sections{work,education: {entries[], add}}, step, controls, login, never, title}. Each field is a generic
    field dict (label, signals, required, options, idx ...) plus `wd`: {widget, ident, section, entry, kind, parts, btn}.
    widget is text|textarea|select|radio|checkbox|file|listbox|prompt|date|stray."""
    root: ff.Node = page["root"]
    fidx, bidx = page["index"]["f"], page["index"]["b"]
    gen = {f["idx"]: f for f in page["fields"]}
    buttons = {b["idx"]: b for b in page["buttons"]}
    b_nodes: dict[int, ff.Node] = {}
    f_nodes: dict[int, ff.Node] = {}
    everything = list(_walk(root))
    for n in everything:
        if id(n) in bidx:
            b_nodes[bidx[id(n)]] = n
        if id(n) in fidx:
            f_nodes[fidx[id(n)]] = n

    containers: list[ff.Node] = []

    def collect(n: ff.Node, inside: bool) -> None:
        for c in n.children:
            if isinstance(c, str):
                continue
            here = _aid(c).lower().startswith("formfield")
            if here and not inside:
                containers.append(c)
            collect(c, inside or here)
    collect(root, False)

    def where(c: ff.Node) -> tuple[str, ff.Node | None, ff.Node | None]:
        entry, p = None, c.parent
        while p is not None:
            a = _aid(p).lower()
            if entry is None and ENTRY_RE.match(a):
                entry = p
            if a in SECTION_KINDS:
                return SECTION_KINDS[a], p, entry
            if a in OTHER_SECTIONS:
                return "other", p, entry
            p = p.parent
        return "", None, None

    fields: list[dict] = []
    covered: set[int] = set()
    for c in containers:
        f = _read_container(c, gen, buttons, fidx, bidx)
        if f is None:
            continue
        section, _, entry = where(c)
        f["wd"].update(section=section, entry=_entry_key(entry, everything) if entry is not None else None,
                       kind=_kind_of(section, f["wd"]["ident"], f["label"]) if section in ("work", "education") else "")
        fields.append(f)
        covered.update(f["wd"].get("_covered", []))
    for f in page["fields"]:                       # an input outside every formField container keeps the generic mapping
        if f["idx"] in covered or f["type"] in ("hidden",) or not f["visible"] and f["type"] not in ("radio", "file"):
            continue
        if f["type"] == "file":                    # Workday hides the real file input behind its drop zone; the live driver treats it as visible
            f = {**f, "visible": True}
        node = f_nodes.get(f["idx"])
        if f["type"] == "file" and node is not None:           # the nearest heading ("Resume/CV") beats a label made from the input's id
            heading = _heading_above(node)
            if heading:
                f = {**f, "label": heading, "signals": [heading]}
        fields.append({**f, "wd": {"widget": "stray", "ident": _ident(f["name"] or f["id"]), "section": "", "entry": None, "kind": "", "parts": {}, "btn": None}})

    sections: dict[str, dict] = {}
    for n in everything:
        a = _aid(n).lower()
        if a in SECTION_KINDS and SECTION_KINDS[a] not in sections:
            kind = SECTION_KINDS[a]
            entries = []
            for m in _walk(n):
                if ENTRY_RE.match(_aid(m)):
                    key = _entry_key(m, everything)
                    num = re.search(r"-(\d+)$", _aid(m))
                    entries.append({"key": key, "num": int(num.group(1)) if num else len(entries) + 1,
                                    "fields": {}, "others": []})
            by_key = {e["key"]: e for e in entries}
            for f in fields:
                if f["wd"]["section"] == kind and f["wd"]["entry"] in by_key:
                    e = by_key[f["wd"]["entry"]]
                    if f["wd"]["kind"] != "other" and f["wd"]["kind"] not in e["fields"]:
                        e["fields"][f["wd"]["kind"]] = f
                    else:
                        e["others"].append(f)
            add = None
            for m in _walk(n):
                if id(m) in bidx and (_aid(m).lower() in ("add", "add-button") or ff.norm(ff._own_text(m) or m.get("aria-label")) in ("add", "add another")):
                    b = buttons.get(bidx[id(m)])
                    if b is not None and not ENTRY_RE.match(_aid(m.parent)):
                        add = b
                        break
            sections[kind] = {"entries": entries, "add": add, "title": SECTION_TITLE[kind]}

    step = _steps(root, page["text"])
    login = any(_aid(n).lower() in LOGIN_AIDS for n in everything)
    never = bool(NEVER_STEP.search(step["title"])) or any(NEVER_AID.search(_aid(n)) for n in everything)
    for f in fields:
        f["wd"].pop("_covered", None)
    return {"fields": fields, "sections": sections, "step": step, "controls": _controls(page["buttons"], b_nodes), "login": login, "never": never,
            "title": step["title"], "url": url}


def _heading_above(node: ff.Node) -> str:
    """The nearest heading or label above a control (Workday's resume box has none of its own)."""
    p = node.parent
    for _ in range(6):
        if p is None:
            break
        for c in p.children:
            if not isinstance(c, str) and c is not node and (c.tag in ("h2", "h3", "h4", "legend", "label") or _aid(c).lower() == "sectionheader"):
                text = _label_text(c)[0]
                if text:
                    return text
        p = p.parent
    return ""


def _entry_key(node: ff.Node, everything: list[ff.Node]) -> str:
    a = _aid(node)
    if a:
        return a
    peers = [n for n in everything if n.parent is node.parent and n.tag == node.tag]
    return f"#{peers.index(node) + 1}" if node in peers else "#0"


def _read_container(c: ff.Node, gen: dict, buttons: dict, fidx: dict, bidx: dict) -> dict | None:
    ident_raw = _aid(c)
    inner = [n for n in _walk(c)]
    ctrls = [n for n in inner if id(n) in fidx and fidx[id(n)] in gen]
    pops = [n for n in inner if n.tag == "button" and n.get("aria-haspopup") in ("listbox", "true") and id(n) in bidx]
    ident = _ident(ident_raw)
    base_extra = {"ident": ident, "parts": {}, "btn": None, "_covered": [fidx[id(n)] for n in ctrls]}

    def finish(field: dict, widget: str, *, label_skip: set[str] | None = None, **wd: Any) -> dict:
        label, req = _container_label(c, label_skip or set())
        label = label or field.get("label") or ff._humanize(ident_raw.split("-", 1)[-1])
        if label == "(unlabelled field)":
            label = ff._humanize(ident_raw.split("-", 1)[-1]) or label
        signals = [s for s in dict.fromkeys([label, ff._humanize(ident_raw.split("-", 1)[-1])]) if s]
        out = {**field, "label": label, "signals": signals, "required": bool(field.get("required") or req)}
        out["wd"] = {**base_extra, "widget": widget, **wd}
        return out

    files = [n for n in ctrls if gen[fidx[id(n)]]["type"] == "file"]
    if files:
        return finish(gen[fidx[id(files[0])]], "file")
    dates = {}
    for n in ctrls:
        a = _aid(n).lower()
        for part in ("month", "day", "year"):
            if "datesection" + part in a:
                dates[part] = fidx[id(n)]
    if dates:
        parts_f = [gen[i] for i in dates.values()]
        field = {**parts_f[0], "type": "date", "idx": min(dates.values()), "visible": all(p["visible"] for p in parts_f),
                 "disabled": any(p["disabled"] for p in parts_f), "required": any(p["required"] for p in parts_f), "options": []}
        return finish(field, "date", parts=dates)
    if pops:
        b = buttons.get(bidx[id(pops[0])])
        if b is None:
            return None
        idx = bidx[id(pops[0])]
        label, req = _container_label(c, set())
        return finish(_synthetic(idx, "listbox", label, pops[0], b, req), "listbox", btn=idx)
    box = next((n for n in ctrls if _aid(n).lower() == "searchbox"), None)
    if box is not None:
        return finish({**gen[fidx[id(box)]], "type": "prompt"}, "prompt")
    radios = [n for n in ctrls if gen[fidx[id(n)]]["type"] == "radio"]
    if radios:
        skip = {n.get("id") for n in radios if n.get("id")}
        return finish(gen[fidx[id(radios[0])]], "radio", label_skip=skip)
    boxes = [n for n in ctrls if gen[fidx[id(n)]]["type"] == "checkbox"]
    if boxes:
        return finish(gen[fidx[id(boxes[0])]], "checkbox")
    for tag, widget in (("select", "select"), ("textarea", "textarea")):
        n = next((x for x in ctrls if x.tag == tag), None)
        if n is not None:
            return finish(gen[fidx[id(n)]], widget)
    n = next((x for x in ctrls if gen[fidx[id(x)]]["type"] != "hidden"), None)
    if n is not None:
        return finish(gen[fidx[id(n)]], "text")
    return None


def _controls(buttons: list[dict], b_nodes: dict[int, ff.Node]) -> dict:
    """The footer button is Next unless its words say Submit (Workday's last step reuses the same button for Submit)."""
    submit, nxt, ambiguous = [], [], False
    plain: list[dict] = []
    for b in buttons:
        n = b_nodes.get(b["idx"])
        aid = _aid(n).lower()
        if n is not None and (n.get("aria-haspopup") or aid in ("add", "add-button", "select-files", "delete_charm", "pagefooterbackbutton")):
            continue
        if not b["visible"] or b["disabled"]:
            continue
        t = ff.norm(b["text"])
        if aid in FOOTER_AIDS:
            if t in SUBMIT_TEXT or "submit" in t.split():
                submit.append(b)
            elif t and "back" not in t.split() and "cancel" not in t.split():
                nxt.append(b)
        else:
            plain.append(b)
    if not submit and not nxt and plain:                                 # a tenant without the footer ids: the generic rules, minus widgets
        found = ff.find_controls([b for b in plain if ff.norm(b["text"]) not in ("add", "add another")])
        return found
    if len(submit) > 1 or len(nxt) > 1:
        ambiguous = True
    return {"submit": submit[0] if len(submit) == 1 else None, "next": nxt[0] if len(nxt) == 1 else None, "ambiguous": ambiguous}


def blockers(wd: dict, existing: list[dict]) -> list[dict]:
    out = list(existing)
    if wd["login"] and not any(b["kind"] == "login_required" for b in out):
        out.append({"kind": "login_required", "message": "Workday wants you to sign in or create an account. Use 'Open a login window', log in yourself, then fill again."})
    return out


# ---- the owner's entries ------------------------------------------------------------------------------------------

def _month_year(text: str) -> tuple[int | None, int] | str | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    if CURRENT_WORDS.search(t):
        return "present"
    m = re.search(r"\b(\d{1,2})\s*[/.\-]\s*((?:19|20)\d{2})\b", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b([a-z]{3,9})\.?,?\s+((?:19|20)\d{2})\b", t)
    if m and m.group(1)[:3] in MONTHS:
        return MONTHS[m.group(1)[:3]], int(m.group(2))
    m = re.search(r"\b((?:19|20)\d{2})\b", t)
    return (None, int(m.group(1))) if m else None


def parse_period(period: str) -> dict:
    """"Jan 2020 - Present" -> {start: (1, 2020), end: "present"}. Only what the text says; a missing month stays None."""
    p = (period or "").strip()
    parts = re.split(r"\s*(?:–|—|\bto\b|\buntil\b|\btill\b)\s*|\s+-\s+", p, maxsplit=1)
    if len(parts) == 1:
        m = re.fullmatch(r"(.*?\b(?:19|20)\d{2})\s*-\s*((?:(?:19|20)\d{2}|present|current|now).*)", p, re.I)
        if m:
            parts = [m.group(1), m.group(2)]
    if len(parts) == 2:
        return {"start": _month_year(parts[0]), "end": _month_year(parts[1])}
    one = _month_year(p)
    return {"start": None if one == "present" else one, "end": "present" if one == "present" else None}


SCHOOL_RE = re.compile(r"universit|college|institute|school|academy|polytechnic|\biit\b|\bnit\b|\biiit\b|\bbits\b", re.I)
DEGREE_RE = re.compile(r"\bb\.?\s?tech\b|\bm\.?\s?tech\b|bachelor|master|\bmba\b|\bb\.?\s?sc\b|\bm\.?\s?sc\b|\bb\.?e\b|\bm\.?e\b|\bb\.?a\b|\bm\.?a\b|\bbca\b|\bmca\b|"
                       r"ph\.?d|doctorate|diploma|associate|\bb\.?com\b|\bm\.?com\b", re.I)


def parse_education(line: str) -> dict:
    """One profile education line -> {school, degree, study, start, end}; only the parts that are clearly there."""
    text = " ".join(str(line or "").split())
    years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", text)]
    rest = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    segs = [s.strip(" ()-") for s in re.split(r"\s*[,;|·•–—]\s*|\s+-\s+|\s+at\s+|\s+from\s+", rest) if s.strip(" ()-")]
    school = next((s for s in segs if SCHOOL_RE.search(s)), "")
    degree = study = ""
    for s in segs:
        if s == school or not DEGREE_RE.search(s):
            continue
        m = re.match(r"(.+?)\s+in\s+(.+)", s, re.I)
        degree, study = (m.group(1).strip(), m.group(2).strip()) if m else (s, "")
        break
    if degree and not study:
        extra = [s for s in segs if s not in (school, degree) and not DEGREE_RE.search(s) and not SCHOOL_RE.search(s) and len(s) > 2]
        study = extra[0] if len(extra) == 1 else ""
    return {"school": school, "degree": degree, "study": study, "start": years[0] if len(years) >= 2 else None,
            "end": years[-1] if years else None, "raw": text}


def profile_entries(ctx: Any, profile: dict, this_year: int) -> tuple[list[dict], list[dict]]:
    """(work roles, education entries) from the fact base: the profile minus anything the owner hid, in profile order."""
    roles: list[dict] = []
    edus: list[dict] = []
    try:
        from . import factbase
        try:
            store = factbase.load(ctx)
        except Exception:  # noqa: BLE001
            store = None
        fb = factbase.FactBase.build(profile if isinstance(profile, dict) else {}, store, this_year=this_year)
        experience = profile.get("experience") if isinstance(profile, dict) and isinstance(profile.get("experience"), list) else []
        for f in fb.facts:
            if f["type"] == "role":
                m = re.search(r"experience (\d+)", f.get("source", ""))
                i = int(m.group(1)) - 1 if m else -1
                loc = ""
                if 0 <= i < len(experience) and isinstance(experience[i], dict):
                    loc = ff._clip(str(experience[i].get("location") or ""), 120)
                roles.append({"title": f.get("title", ""), "company": f.get("company", ""), "location": loc,
                              "period": f.get("period", ""), **parse_period(f.get("period", "")),
                              "bullets": [fb.by_id[b]["text"] for b in f.get("bullets", []) if b in fb.by_id]})
            elif f["type"] == "education":
                e = parse_education(f["text"])
                if e["school"]:
                    edus.append(e)
    except Exception:  # noqa: BLE001 - no entries is the safe answer
        return [], []
    return roles[:MAX_ENTRIES], edus[:MAX_ENTRIES]


def source_prefs(item: dict) -> list[tuple[str, bool]]:
    """(wording, exact-only). The posting's own source first when a list offers exactly that, then Job board, then Company website."""
    prefs: list[tuple[str, bool]] = []
    src = str((item or {}).get("source") or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 .&-]{1,28}", src):
        prefs.append((src, True))
    return prefs + [("Job Board", False), ("Company Website", False), ("Corporate Website", False), ("Careers Site", False), ("Career Site", False), ("Careers Page", False)]


# ---- filling ------------------------------------------------------------------------------------------------------

def _pick(prefs: list[tuple[str, bool]], options: list[str]) -> str | None:
    """The option that clearly matches the first wording that matches anything, else None."""
    for text, exact in prefs:
        pn = ff.norm(text)
        if not pn:
            continue
        for o in options:
            if ff.norm(o) == pn:
                return o
        if exact:
            continue
        starts = [o for o in options if ff.norm(o).startswith(pn + " ") or ff.norm(o) == pn + "s"]
        if len(starts) == 1:
            return starts[0]
        inside = [o for o in options if pn in ff.norm(o)]
        if len(inside) == 1:
            return inside[0]
    return None


def _pick_value(value: str, options: list[str]) -> str | None:
    opt = ff.match_option(value, [{"idx": i, "value": t, "label": t} for i, t in enumerate(options)])
    return opt["label"] if opt else None


def _shown_ok(chosen: str, shown: str) -> bool:
    s, c = ff.norm(shown), ff.norm(chosen)
    return bool(s) and s not in PLACEHOLDERS and (c in s or s in c)


class _Filler:
    def __init__(self, drv: ff.Driver, wd: dict, facts: dict, item: dict, roles: list[dict], edus: list[dict], reread: Callable):
        self.drv, self.wd, self.facts, self.item, self.reread = drv, wd, facts, item, reread
        self.roles, self.edus = roles, edus
        self.answers = facts.get("_answers", {}).get("value", {})
        self.plans: list[dict] = []
        self.values: dict[int, str] = {}

    # -- plan records -----------------------------------------------------------------------------------------------
    def log(self, f: dict, status: str, reason: str = "", *, label: str | None = None, **kw: Any) -> dict:
        p = {"idx": f.get("idx", -1), "label": label or f["label"], "type": f.get("type", "text"), "required": bool(f.get("required")), "key": "",
             "value": "", "display": "", "reason": reason, "action": "", "target": f.get("idx", -1), "status": status}
        p.update(kw)
        self.plans.append(p)
        return p

    def unknown(self, f: dict, why: str, *, label: str | None = None) -> dict:
        if f.get("required"):
            return self.log(f, "needs_you", why, label=label)
        return self.log(f, "skipped", why + " (optional, left blank)", label=label)

    # -- the whole step -----------------------------------------------------------------------------------------------
    def run(self) -> list[dict]:
        wd = self.wd
        if wd["never"]:
            self.never()
        else:
            for f in wd["fields"]:
                sec = f["wd"]["section"]
                if sec in ("work", "education"):
                    continue
                if sec == "other":
                    self.unknown(f, "I do not fill this section.")
                    continue
                self.top(f)
            self.verify_top()
            for kind in ("work", "education"):
                self.section(kind)
        for n, p in enumerate(self.plans, 1):
            p["n"] = n
        return self.plans

    def never(self) -> None:
        why = "This step is voluntary self-identification or disclosure. I never fill it, so I left the whole step untouched. It is yours to answer."
        for f in self.wd["fields"]:
            if f["wd"]["widget"] == "stray" and not f["visible"]:
                continue
            self.log(f, "needs_you", why)
        title = self.wd["step"].get("title") or "This step"
        self.log({"label": f"{title} (whole step)", "type": "step", "idx": -1, "required": False}, "needs_you", why)

    # -- one field outside the sections -------------------------------------------------------------------------------
    def top(self, f: dict) -> None:
        w = f["wd"]["widget"]
        if f["disabled"] or f["readonly"]:
            self.log(f, "skipped", "The field is locked.")
            return
        if not f["visible"]:
            self.log(f, "skipped", "Not visible on the page (never filled).")
            return
        sens = ff.sensitive_reason(f)
        if sens:
            self.log(f, "needs_you", f"This is {sens}. I never fill that; it is yours to answer.")
            return
        if w == "date":
            self.log(f, "needs_you", "I only type job and school dates from your profile, never any other date.")
            return
        if w == "checkbox":
            self.unknown(f, "I never tick checkboxes for you.")
            return
        if w in ("listbox", "prompt"):
            self.top_pick(f)
            return
        label = ff.norm(f["label"])
        if re.search(r"\bextension\b|\bext\b", label):               # a loose "Phone" answer must never land in the extension box
            self.unknown(f, "I do not fill phone extensions.")
            return
        special = next(((k, rx, nice) for k, rx, nice in SPECIAL if rx.search(label)), None)
        if special and w in ("text", "textarea", "stray"):
            value, why = self.special_value(f, special)
            if not value:
                self.unknown(f, why)
                return
            p = self.log(f, "fill", key=special[0], value=value, display=ff._clip(value, 160), action="text")
            self.do(p, f)
            return
        p = ff.plan_field(f, self.facts)
        p = {**p, "idx": f["idx"]}
        self.plans.append(p)
        self.do(p, f)

    def special_value(self, f: dict, special: tuple) -> tuple[str, str]:
        key, _, nice = special
        if key == "city":
            loc = (self.facts.get("location") or {}).get("value", "")
            city = loc.split(",")[0].strip()
            if city:
                return city, ""
        same = ff.concept_set(f["label"])                       # only an answer about exactly this ("Country" is not the phone code)
        std, _, amb = ff._answer_for({**f, "signals": [f["label"]]}, {k: v for k, v in self.answers.items() if ff.concept_set(str(k)) == same})
        if amb:
            return "", "Two of your saved answers could fit this and they differ."
        if std:
            return std, ""
        return "", f"I do not have your {nice}. Add it to your profile (standard answers)."

    def top_pick(self, f: dict) -> None:
        label = ff.norm(f["label"])
        if SOURCE_LABEL.search(label):
            prefs = source_prefs(self.item)
            p = self.log(f, "fill", key="source", value=prefs[0][0] if prefs else "Job Board", display="Job board / Company website", action=f["wd"]["widget"])
            p["_prefs"] = prefs
            self.do(p, f)
            return
        special = next(((k, rx, nice) for k, rx, nice in SPECIAL if rx.search(label)), None)
        if special:
            value, why = self.special_value(f, special)
            if not value:
                self.unknown(f, why)
                return
            key = special[0]
        else:
            pseudo = {**f, "type": "text", "tag": "input", "options": [], "maxlength": 0, "autocomplete": ""}
            g = ff.plan_field(pseudo, self.facts)
            if g["status"] != "fill":
                self.plans.append({**g, "type": f["type"], "idx": f["idx"]})
                return
            value, key = g["value"], g["key"]
        p = self.log(f, "fill", key=key, value=value, display=ff._clip(value, 160), action=f["wd"]["widget"])
        p["_prefs"] = [(value, False)]
        self.do(p, f)

    # -- doing it -------------------------------------------------------------------------------------------------------
    def do(self, p: dict, f: dict) -> None:
        if p["status"] != "fill":
            return
        drv, a = self.drv, p["action"]
        try:
            if a == "text":
                drv.fill_text(f["idx"], p["value"])
            elif a == "upload":
                drv.upload(f["idx"], p["value"])
            elif a == "select":
                drv.select(f["idx"], p["value"])
            elif a == "choose":
                drv.choose(p["target"])
            elif a == "tick":
                drv.choose(f["idx"])
            elif a in ("listbox", "prompt"):
                self.pick(p, f)
        except Exception:  # noqa: BLE001 - one stubborn field must not stop the rest
            p.update(status="needs_you", reason="I could not fill this one on the page.")

    def pick(self, p: dict, f: dict) -> None:
        drv = self.drv
        prefs: list[tuple[str, bool]] = p.pop("_prefs", None) or [(p["value"], False)]
        generic = p["key"] != "source"
        if f["wd"]["widget"] == "listbox":
            btn = f["wd"]["btn"]
            options = drv.open_list(btn)
            chosen = _pick_value(prefs[0][0], options) if generic else _pick(prefs, options)
            if chosen is None:
                drv.close_list()
                p.update(status="needs_you", reason="None of the choices clearly fits, so I did not pick one.", display="", value="")
                return
            shown = drv.pick_listed(btn, chosen)
            if not _shown_ok(chosen, shown):
                p.update(status="needs_you", reason="The page did not keep my choice. Pick it yourself.", display="", value="")
                return
            p.update(display=ff._clip(shown, 120), value=chosen)
            return
        tried = 0
        queries = [(t, e) for t, e in prefs if not e][:3] if not generic else prefs[:1]
        for text, _ in queries:
            tried += 1
            options = drv.search_options(f["idx"], text[:40])
            chosen = _pick_value(text, options) if generic else _pick([(text, False)], options)
            if chosen is None:
                continue
            chips = drv.pick_searched(f["idx"], chosen)
            if not _shown_ok(chosen, chips):
                p.update(status="needs_you", reason="The page did not keep my choice. Pick it yourself.", display="", value="")
                return
            p.update(display=ff._clip(chips, 120), value=chosen)
            return
        p.update(status="needs_you", reason="I found no option that clearly fits, so I did not pick one.", display="", value="")

    def verify_top(self) -> None:
        values = self.drv.values()
        for p in self.plans:
            if p["status"] == "fill" and p["action"] in ("text", "select") and p["idx"] >= 0 and not values.get(p["idx"], "x"):
                p.update(status="needs_you", reason="The page did not keep my answer. Type it yourself.")

    # -- repeatable sections --------------------------------------------------------------------------------------------
    def val(self, f: dict | None, part: str = "") -> str:
        if f is None:
            return ""
        if part:
            idx = f["wd"]["parts"].get(part)
            return (self.values.get(idx, "") if idx is not None else "").strip()
        w = f["wd"]["widget"]
        if w == "date":
            return " ".join(self.values.get(i, "") for i in f["wd"]["parts"].values()).strip()
        if w == "listbox":                                          # a dropdown's value is its button text, not an input
            shown = self.drv.wd_values().get(f"b{f['idx']}", "")
            return "" if ff.norm(shown) in PLACEHOLDERS else shown.strip()
        if w == "prompt":
            return (self.drv.wd_values().get(f"p{f['idx']}", "") or "").replace("|", ", ").strip()
        return (self.values.get(f["idx"], "") or "").strip()

    def section(self, kind: str) -> None:
        sec = self.wd["sections"].get(kind)
        if sec is None:
            return
        items = (self.roles if kind == "work" else self.edus)[:MAX_ENTRIES]
        title = SECTION_TITLE[kind]
        marker = {"label": title, "type": "section", "idx": -1, "required": False}
        if not items:
            self.log(marker, "skipped", f"Your profile has no {title.lower()} entries, so I added none.")
            return
        self.values = self.drv.values()
        used: set[str] = set()
        for src in items:
            sec = self.wd["sections"].get(kind) or sec
            entry = self.find_existing(kind, sec, src, used) or self.find_blank(kind, sec, used)
            if entry is None:
                entry = self.add_entry(kind, sec)
                if entry is None:
                    self.log(marker, "needs_you", "I could not add another entry here. Add it yourself.",
                             label=f"{title}: {self.describe(kind, src)}")
                    break
            used.add(entry["key"])
            self.fill_entry(kind, entry, src)

    def describe(self, kind: str, src: dict) -> str:
        return " at ".join(x for x in ((src.get("title"), src.get("company")) if kind == "work" else (src.get("degree"), src.get("school"))) if x) or "entry"

    def find_existing(self, kind: str, sec: dict, src: dict, used: set[str]) -> dict | None:
        k1, k2 = ("company", "title") if kind == "work" else ("school", "degree")
        want1, want2 = ff.norm(src.get(k1, "")), ff.norm(src.get(k2, ""))
        for e in sec["entries"]:
            if e["key"] in used:
                continue
            have1, have2 = ff.norm(self.val(e["fields"].get(k1))), ff.norm(self.val(e["fields"].get(k2)))
            if want1 and have1 == want1 and (not have2 or not want2 or have2 == want2 or kind == "education"):
                return e
        return None

    def find_blank(self, kind: str, sec: dict, used: set[str]) -> dict | None:
        for e in sec["entries"]:
            if e["key"] not in used and e["fields"] and not any(self.val(f) for f in e["fields"].values()
                                                                  if f["wd"]["widget"] not in ("checkbox", "listbox", "prompt")):
                return e
        return None

    def add_entry(self, kind: str, sec: dict) -> dict | None:
        add = sec.get("add")
        if add is None or not add["visible"] or add["disabled"] or len(sec["entries"]) >= MAX_ENTRIES:
            return None
        before = {e["key"] for e in sec["entries"]}
        self.drv.click(add["idx"])
        self.drv.settle()
        wd, values = self.reread()
        if wd is None:
            return None
        self.wd, self.values = wd, values
        new = [e for e in (wd["sections"].get(kind) or {"entries": []})["entries"] if e["key"] not in before]
        return new[-1] if new else None

    def fill_entry(self, kind: str, entry: dict, src: dict) -> None:
        title = SECTION_TITLE[kind]
        num = entry.get("num") or 1
        tag = f"{title} {num}: "
        fields = entry["fields"]
        for k in ENTRY_ORDER[kind]:
            f = fields.get(k)
            if f is None:
                continue
            name = tag + f["label"]
            if not f["visible"] and k != "end":
                self.log(f, "skipped", "Not visible on the page (never filled).", label=name)
                continue
            if f["disabled"] or f["readonly"]:
                self.log(f, "skipped", "The field is locked.", label=name)
                continue
            if k == "current":
                self.entry_current(f, src, name)
            elif k in ("start", "end"):
                self.entry_date(kind, k, f, src, name)
            else:
                self.entry_text(kind, k, f, src, name)
        for f in entry["others"]:
            if f["visible"]:
                self.unknown(f, "I only fill job and school basics.", label=tag + f["label"])

    def entry_current(self, f: dict, src: dict, name: str) -> None:
        if src.get("end") != "present" or f["wd"]["widget"] != "checkbox":
            return                                                   # not a current job: the box stays as it is
        if self.val(f) in ("1", "true", "on"):
            self.log(f, "skipped", "Already ticked (kept as is).", label=name)
            return
        p = self.log(f, "fill", key="current_job", value="1", display="ticked", action="tick", label=name)
        self.do(p, f)

    def entry_text(self, kind: str, k: str, f: dict, src: dict, name: str) -> None:
        if kind == "work":
            value = {"title": src.get("title", ""), "company": src.get("company", ""), "location": src.get("location", ""),
                     "desc": "\n".join(f"- {b}" for b in src.get("bullets", []))[:2000]}.get(k, "")
        else:
            value = {"school": src.get("school", ""), "degree": src.get("degree", ""), "study": src.get("study", "")}.get(k, "")
        if self.val(f):
            self.log(f, "skipped", "Already filled on the page (kept as is).", label=name)
            return
        if not value:
            if k in ("grade",):
                return
            self.unknown(f, "Your profile does not say this. Type it yourself.", label=name)
            return
        w = f["wd"]["widget"]
        if w in ("text", "textarea"):
            p = self.log(f, "fill", key=f"{kind}_{k}", value=value, display=ff._clip(value, 160), action="text", label=name)
            self.do(p, f)
            if p["status"] == "fill":
                self.values = self.drv.values()
                if not self.values.get(f["idx"], "x"):
                    p.update(status="needs_you", reason="The page did not keep my answer. Type it yourself.")
        elif w in ("listbox", "prompt"):
            p = self.log(f, "fill", key=f"{kind}_{k}", value=value, display=ff._clip(value, 160), action=w, label=name)
            p["_prefs"] = [(value, False)]
            self.do(p, f)
        else:
            self.unknown(f, "I do not know how to fill this kind of field.", label=name)

    def entry_date(self, kind: str, k: str, f: dict, src: dict, name: str) -> None:
        if f["wd"]["widget"] != "date":
            self.unknown(f, "I do not know how to fill this kind of field.", label=name)
            return
        if k == "end" and src.get("end") == "present":
            return                                                   # a current job has no end date
        if any(self.val(f, part) for part in f["wd"]["parts"]):
            self.log(f, "skipped", "Already filled on the page (kept as is).", label=name)
            return
        if kind == "work":
            raw = src.get(k)
            if not isinstance(raw, tuple):
                self.unknown(f, "Your profile does not give this date. Type it yourself.", label=name)
                return
            month, year = raw
        else:
            year, month = src.get(k), None
            if not year:
                self.unknown(f, "Your profile does not give this year. Type it yourself.", label=name)
                return
        parts = f["wd"]["parts"]
        if ("month" in parts and not month) or "day" in parts:
            self.log(f, "needs_you", "Your profile has no exact month and day here, and Workday asks for them. Type this date yourself.", label=name)
            return
        want = {"year": str(year)}
        if "month" in parts:
            want["month"] = f"{int(month):02d}"
        p = self.log(f, "fill", key=f"{kind}_{k}_date", value="/".join(want[x] for x in ("month", "year") if x in want), display=" ".join(
            want[x] for x in ("month", "year") if x in want), action="date", label=name)
        try:
            for part in ("month", "year"):
                if part in want and part in parts:
                    self.drv.type_text(parts[part], want[part])
            self.values = self.drv.values()
            for part, text in want.items():
                got = re.sub(r"\D", "", self.val(f, part))
                if not got or int(got) != int(text):
                    p.update(status="needs_you", reason="The page did not keep my date. Type it yourself.", display="")
                    break
        except Exception:  # noqa: BLE001
            p.update(status="needs_you", reason="I could not type this date on the page.", display="")


def fill_step(ctx: Any, drv: ff.Driver, wd: dict, facts: dict, *, item: dict, profile: dict, this_year: int, reread: Callable) -> list[dict]:
    """Fill what is safe on the current Workday step and return the plan list (what was filled, what needs the owner). Never clicks
    Next or Submit; the only clicks here are a section's own Add button and the dropdown/picker/checkbox the field itself needs."""
    roles, edus = ([], []) if wd["never"] else profile_entries(ctx, profile, this_year)
    return _Filler(drv, wd, facts, item or {}, roles, edus, reread).run()
