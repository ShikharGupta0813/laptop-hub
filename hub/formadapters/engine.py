"""ENGINE: the shared reader and filler behind the SuccessFactors, Taleo, Oracle, iCIMS and Ashby adapters. See docs/api/formfill.md.

Each system is a small `System` description (hosts, markers, class hints for its custom widgets) in its own file. The engine reads the
page the way hub/workday.py does (pure, from the stamped HTML) and plans each field:
- plain inputs, selects, radios and file boxes go through the generic mapper (`formfill.plan_field`), nothing new there;
- `combo` is an input that opens a pop-up list (Ashby and Oracle selects, SuccessFactors UI5 combo boxes): the list is opened, the option
  that clearly matches is clicked, and the page must show it afterwards;
- `yesno` is a pair of Yes / No buttons (Ashby): the matching button is pressed and its pressed state is checked afterwards;
- `date` pickers are never typed (only job and school dates from the owner's entries would be safe, and these adapters add no entries).

Rules that are code, not policy: never sign in or create an account (a login wall is a blocker), never fill voluntary / EEO / demographic /
visa / salary-history / date-of-birth / ID fields, never tick a consent or terms box, never click Next or Submit here (moving on is the
owner's confirmed tap in formfill.py, Submit is the owner's tap with the digest). A choice is only made when it clearly matches a listed
option and the page shows it afterwards; otherwise the field is "needs you".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .. import formfill as ff
from .. import workday as wd

# Anything voluntary, demographic or legal is the owner's own answer, on top of formfill.SENSITIVE.
EXTRA_NEVER = re.compile(r"\bvisa\b|voluntary|self[- ]?identif|\beeo\b|equal (employment|opportunity)|protected (class|veteran)|gender identity|"
                         r"salary history|date of birth|\bdob\b|birth ?date|national origin|affirmative action|disabilit|veteran|ethnic|\brace\b")
NEVER_STEP = re.compile(wd.NEVER_STEP.pattern + r"|affirmative action|equal opportunity|eeo", re.I)
PLACEHOLDERS = wd.PLACEHOLDERS | {"select an option", "select a value", "none selected", "type to search", "start typing", "start typing the name of an option"}
COMBO_DEFAULT = re.compile(r"combobox|combo-box|oj-select|oj-combobox|select-single|selectcombo|sapmcombobox|ui5-combobox", re.I)
DATE_DEFAULT = re.compile(r"datepicker|date-picker|datetimepicker|oj-input-date|oj-inputdate|sapmdp|sapmdatepicker|ui5-date|calendar", re.I)
DATE_PLACEHOLDER = re.compile(r"^(mm|dd|yyyy)\s*[/.-]\s*(mm|dd|yyyy)|^(day|month|year)\s*/\s*(day|month|year)", re.I)
EXTENSION = re.compile(r"\bextension\b|\bext\b")
YESNO = frozenset({"yes", "no"})


@dataclass(frozen=True)
class System:
    key: str
    name: str                                   # what the owner sees ("Ashby")
    hosts: tuple[str, ...]                      # host suffixes: acme.taleo.net matches taleo.net, notataleo.net does not
    path: re.Pattern | None = None              # when set, a host match also needs this path (oraclecloud.com hosts other products too)
    marker: re.Pattern | None = None            # page markup for a custom domain
    login_marker: re.Pattern | None = None      # ids / names / classes that mean a sign-in or create-account wall
    combo: re.Pattern = COMBO_DEFAULT           # class / tag words of this system's pop-up list inputs
    date: re.Pattern = DATE_DEFAULT
    never_title: re.Pattern = NEVER_STEP
    note: str = ""

    # -- is this page ours --------------------------------------------------------------------------------------------
    def applies(self, url: str, html: str = "") -> bool:
        host = ff.host_of(url or "")
        if any(host == s or host.endswith("." + s) for s in self.hosts):
            if self.path is None:
                return True
            if self.path.search(re.sub(r"^[a-z]+://[^/]+", "", (url or "").split("#")[0])):
                return True
        return bool(html and self.marker and self.marker.search(html[:ff.MAX_HTML]))

    def parse(self, page: dict, url: str = "") -> dict:
        return parse(self, page, url)

    def blockers(self, wdr: dict, existing: list[dict]) -> list[dict]:
        out = list(existing)
        if wdr["login"] and not any(b["kind"] == "login_required" for b in out):
            out.append({"kind": "login_required", "message": f"{self.name} wants you to sign in or create an account. Use 'Open a login window', log in yourself, then fill again."})
        return out

    def fill_step(self, ctx: Any, drv: ff.Driver, wdr: dict, facts: dict, *, item: dict, profile: dict, this_year: int, reread: Callable) -> list[dict]:
        return _Filler(drv, wdr, facts, item or {}).run()


# ---- reading the page ---------------------------------------------------------------------------------------------

def _walk(n: ff.Node) -> Iterator[ff.Node]:
    yield from wd._walk(n)


def _within(n: ff.Node, top: ff.Node) -> bool:
    p = n
    while p is not None:
        if p is top:
            return True
        p = p.parent
    return False


def _hints(node: ff.Node | None, rx: re.Pattern, depth: int = 3) -> bool:
    """Does the node, or one of its nearest ancestors, look like this kind of widget (by tag, class, role or id)?"""
    p = node
    for _ in range(depth + 1):
        if p is None:
            return False
        if rx.search(" ".join((p.tag, p.get("class"), p.get("id"), p.get("data-automation-id"), p.get("data-testid")))):
            return True
        p = p.parent
    return False


def _is_combo(system: System, node: ff.Node) -> bool:
    if node.get("role") == "combobox" or node.get("aria-haspopup") in ("listbox", "true", "menu") or node.get("aria-autocomplete") in ("list", "both"):
        return True
    return _hints(node, system.combo)


def _clean_label(text: str) -> tuple[str, bool]:
    required = "*" in text or bool(re.search(r"\(required\)\s*$", text, re.I))
    text = re.sub(r"\s*\(required\)\s*$", "", re.sub(r"\s*\*+", "", text), flags=re.I)
    return ff._clip(" ".join(text.split()), 120), required


def _has_label(node: ff.Node, labelled: set[str]) -> bool:
    p = node
    while p is not None:
        if p.tag == "label":
            return True
        p = p.parent
    return bool(node.get("aria-label") or node.get("aria-labelledby") or (node.get("id") and node.get("id") in labelled))


def _label_hint(node: ff.Node | None) -> str:
    p = node
    for _ in range(4):
        if p is None:
            return ""
        v = p.get("label-hint")
        if v:
            return ff._clip(v, 120)
        p = p.parent
    return ""


def _yesno_label(parent: ff.Node) -> tuple[str, bool]:
    p = parent
    for _ in range(4):
        p = p.parent
        if p is None:
            break
        for n in _walk(p):
            if _within(n, parent):
                continue
            if n.tag in ("label", "legend") or re.search(r"label|heading|question", n.get("class"), re.I):
                text, req = _clean_label(ff._own_text(n))
                if text:
                    return text, req or n.get("aria-required") == "true"
    return "", False


def _blank(idx: int, kind: str, label: str, required: bool) -> dict:
    return {"idx": idx, "tag": "button", "type": kind, "name": "", "id": "", "placeholder": "", "autocomplete": "", "required": required, "disabled": False,
            "readonly": False, "visible": True, "maxlength": 0, "multiple": False, "options": [], "label": label, "signals": [label]}


def parse(system: System, page: dict, url: str = "") -> dict:
    """Pure. -> {fields[], step, controls, login, never, title, url, adapter}. Each field is a generic field dict plus
    `wd`: {widget, btns, node}. widget is text|textarea|select|radio|checkbox|file|combo|yesno|date|password."""
    root: ff.Node = page["root"]
    fidx, bidx = page["index"]["f"], page["index"]["b"]
    buttons = {b["idx"]: b for b in page["buttons"]}
    f_nodes: dict[int, ff.Node] = {}
    b_nodes: dict[int, ff.Node] = {}
    everything = list(_walk(root))
    pos = {id(n): i for i, n in enumerate(everything)}            # document order (Node.tok is only set on some nodes)
    for n in everything:
        if id(n) in fidx:
            f_nodes[fidx[id(n)]] = n
        if id(n) in bidx:
            b_nodes[bidx[id(n)]] = n

    labelled = {n.get("for") for n in everything if n.tag == "label" and n.get("for")}
    fields: list[dict] = []
    for f in page["fields"]:
        if not (f["visible"] or f["type"] in ("radio", "file")):
            continue
        node = f_nodes.get(f["idx"]) or (f_nodes.get(f["options"][0]["idx"]) if f["options"] else None)
        t = f["type"]
        if t in ("file", "password", "select", "radio", "checkbox", "textarea"):
            widget = t
        elif t in ("date", "datetime-local", "month", "time", "week") or _hints(node, system.date) or DATE_PLACEHOLDER.search(f["placeholder"] or ""):
            widget = "date"
        elif node is not None and f["tag"] == "input" and _is_combo(system, node):
            widget = "combo"
        else:
            widget = "text"
        out = {**f}
        hint = _label_hint(node) if node is not None and not _has_label(node, labelled) else ""
        if hint:                                              # no <label> of its own: the component's label-hint beats the text that merely sits above it
            out["label"], out["signals"] = hint, [hint] + [s for s in out["signals"] if s != hint]
        out["label"], star = _clean_label(out["label"])
        out["required"] = bool(out["required"] or star)
        out["wd"] = {"widget": widget, "btns": {}}
        fields.append(out)

    groups: dict[int, list[tuple[int, str]]] = {}
    parents: dict[int, ff.Node] = {}
    for idx, n in b_nodes.items():
        t = ff.norm(buttons[idx]["text"]) if idx in buttons else ""
        if t in YESNO and n.parent is not None and not n.get("aria-haspopup"):
            groups.setdefault(id(n.parent), []).append((idx, t))
            parents[id(n.parent)] = n.parent
    widget_btns: set[int] = set()
    for key, items in groups.items():
        if sorted(t for _, t in items) != ["no", "yes"]:
            continue
        label, req = _yesno_label(parents[key])
        first = min(i for i, _ in items)
        f = _blank(first, "yesno", label or "(unlabelled question)", req)
        f["visible"] = all(buttons[i]["visible"] for i, _ in items)
        f["disabled"] = any(buttons[i]["disabled"] for i, _ in items)
        f["options"] = [{"idx": i, "value": t.title(), "label": t.title(), "checked": False} for i, t in sorted(items, key=lambda x: x[0])]
        f["wd"] = {"widget": "yesno", "btns": {t: i for i, t in items}}
        fields.append(f)
        widget_btns.update(i for i, _ in items)
    for f in fields:                                          # a combo's own toggle / clear buttons are not page controls
        if f["wd"]["widget"] == "combo":
            n = f_nodes.get(f["idx"])
            for m in (_walk(n.parent) if n is not None and n.parent is not None else ()):
                if id(m) in bidx:
                    widget_btns.add(bidx[id(m)])
    fields.sort(key=lambda f: pos.get(id(f_nodes.get(f["idx"]) or (f_nodes.get(f["options"][0]["idx"]) if f["options"] else None)), 0)
                if f["wd"]["widget"] != "yesno" else pos.get(id(b_nodes.get(f["idx"])), 0))

    step = wd._steps(root, page["text"])
    if not step["title"]:
        for n in everything:
            if n.tag in ("h1", "legend"):
                step["title"] = ff._clip(ff._own_text(n), 80)
                break
    first_h1 = next((ff._own_text(n) for n in everything if n.tag == "h1"), "")
    never = any(system.never_title.search(ff.norm(h)) for h in (step["title"], first_h1) if h)       # the step itself is voluntary
    sections = [(pos[id(n)], system.never_title.search(ff.norm(ff._own_text(n))) is not None) for n in everything if n.tag in ("h2", "h3", "h4")]
    for f in fields:                                          # a voluntary section inside an ordinary page: its fields are never filled
        node = f_nodes.get(f["idx"]) or b_nodes.get(f["idx"])
        above = [bad for at, bad in sections if node is not None and at < pos[id(node)]]
        f["wd"]["never"] = bool(above and above[-1])
    markup = [" ".join((n.get("id"), n.get("name"), n.get("class"), n.get("data-automation-id"), n.get("action"))) for n in everything]
    login = any(f["type"] == "password" and f["visible"] for f in page["fields"]) or bool(
        system.login_marker and any(system.login_marker.search(m) for m in markup))
    return {"fields": fields, "step": step, "controls": _controls(page["buttons"], widget_btns), "login": login, "never": never,
            "title": step["title"], "url": url, "adapter": system}


def _controls(buttons: list[dict], widget_btns: set[int]) -> dict:
    usable = [b for b in buttons if b["idx"] not in widget_btns and ff.norm(b["text"]) not in YESNO]
    return ff.find_controls(usable)


# ---- filling ------------------------------------------------------------------------------------------------------

class _Filler:
    def __init__(self, drv: ff.Driver, wdr: dict, facts: dict, item: dict):
        self.drv, self.wd, self.facts, self.item = drv, wdr, facts, item
        self.answers = facts.get("_answers", {}).get("value", {})
        self.plans: list[dict] = []

    def log(self, f: dict, status: str, reason: str = "", **kw: Any) -> dict:
        p = {"idx": f.get("idx", -1), "label": f["label"], "type": f.get("type", "text"), "required": bool(f.get("required")), "key": "", "value": "",
             "display": "", "reason": reason, "action": "", "target": f.get("idx", -1), "status": status}
        p.update(kw)
        self.plans.append(p)
        return p

    def unknown(self, f: dict, why: str) -> dict:
        return self.log(f, "needs_you", why) if f.get("required") else self.log(f, "skipped", why + " (optional, left blank)")

    def run(self) -> list[dict]:
        if self.wd["never"]:
            why = "This step is voluntary self-identification or disclosure. I never fill it, so I left the whole step untouched. It is yours to answer."
            for f in self.wd["fields"]:
                if f["visible"]:
                    self.log(f, "needs_you", why)
            self.log({"label": f"{self.wd['step'].get('title') or 'This step'} (whole step)", "type": "step", "idx": -1, "required": False}, "needs_you", why)
        else:
            for f in self.wd["fields"]:
                self.one(f)
            self.verify()
        for n, p in enumerate(self.plans, 1):
            p["n"] = n
        return self.plans

    # -- one field ---------------------------------------------------------------------------------------------------
    def one(self, f: dict) -> None:
        w = f["wd"]["widget"]
        if f["disabled"] or (f["readonly"] and w != "combo"):         # a combo input is often read-only: it only shows the choice
            return self.log(f, "skipped", "The field is locked.") and None
        if not f["visible"]:
            return self.log(f, "skipped", "Not visible on the page (never filled).") and None
        if f["wd"].get("never"):
            return self.log(f, "needs_you", "This is in a voluntary or self-identification section. I never fill that; it is yours to answer.") and None
        sens = ff.sensitive_reason(f)
        if not sens and any(EXTRA_NEVER.search(ff.norm(s)) for s in f["signals"] or [f["label"]]):
            sens = "a voluntary, demographic or legal question"
        if sens:
            return self.log(f, "needs_you", f"This is {sens}. I never fill that; it is yours to answer.") and None
        if w == "password":
            return self.log(f, "needs_you", "This is a password. I never fill that; it is yours to answer.") and None
        if w == "date":
            return self.log(f, "needs_you", "I never type a date here (no job or school entries are added). Pick it yourself.") and None
        if w == "checkbox":
            return self.unknown(f, "I never tick checkboxes for you.") and None
        label = ff.norm(f["label"])
        if EXTENSION.search(label):
            return self.unknown(f, "I do not fill phone extensions.") and None
        if w in ("combo", "yesno"):
            return self.pick_widget(f)
        if w == "select" and wd.SOURCE_LABEL.search(label):
            return self.source_select(f)
        special = next(((k, rx, nice) for k, rx, nice in wd.SPECIAL if rx.search(label)), None)
        if special and w in ("text", "select"):
            value, why = self.special_value(f, special)
            if not value:
                return self.unknown(f, why) and None
            if w == "select":
                opt = ff.match_option(value, f["options"])
                if opt is None:
                    return self.log(f, "needs_you", f"My answer '{ff._clip(value, 40)}' is not one of the choices.", key=special[0]) and None
                p = self.log(f, "fill", key=special[0], value=opt["value"], display=ff._clip(opt["label"] or opt["value"], 120), action="select", target=opt["idx"])
            else:
                p = self.log(f, "fill", key=special[0], value=value, display=ff._clip(value, 160), action="text")
            return self.do(p, f)
        p = {**ff.plan_field(f, self.facts), "idx": f["idx"]}
        self.plans.append(p)
        self.do(p, f)

    def special_value(self, f: dict, special: tuple) -> tuple[str, str]:
        key, _, nice = special
        if key == "city":
            city = (self.facts.get("location") or {}).get("value", "").split(",")[0].strip()
            if city:
                return city, ""
        noise = frozenset({"territory", "region", "residence"})
        same = ff.concept_set(f["label"]) - noise               # only an answer about exactly this ("Country" is not the phone code)
        std, _, amb = ff._answer_for({**f, "signals": [f["label"]]}, {k: v for k, v in self.answers.items() if ff.concept_set(str(k)) - noise == same})
        if amb:
            return "", "Two of your saved answers could fit this and they differ."
        return (std, "") if std else ("", f"I do not have your {nice}. Add it to your profile (standard answers).")

    def source_select(self, f: dict) -> None:
        labels = [o["label"] or o["value"] for o in f["options"] if not o.get("disabled") and ff.norm(o["label"]) not in PLACEHOLDERS]
        prefs = wd.source_prefs(self.item)
        chosen = wd._pick(prefs, labels)
        opt = next((o for o in f["options"] if (o["label"] or o["value"]) == chosen), None) if chosen else None
        if opt is None:
            return self.unknown(f, "None of the choices clearly fits, so I did not pick one.") and None
        p = self.log(f, "fill", key="source", value=opt["value"], display=ff._clip(opt["label"] or opt["value"], 120), action="select", target=opt["idx"])
        self.do(p, f)

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
        except Exception:  # noqa: BLE001 - one stubborn field must not stop the rest
            p.update(status="needs_you", reason="I could not fill this one on the page.")

    # -- pop-up lists and yes/no buttons ---------------------------------------------------------------------------------
    def pick_widget(self, f: dict) -> None:
        w, label = f["wd"]["widget"], ff.norm(f["label"])
        prefs: list[tuple[str, bool]] | None = None
        source = bool(wd.SOURCE_LABEL.search(label))
        if source:
            prefs = wd.source_prefs(self.item)
            key, value = "source", prefs[0][0]
        else:
            special = next(((k, rx, nice) for k, rx, nice in wd.SPECIAL if rx.search(label)), None)
            if special:
                value, why = self.special_value(f, special)
                key = special[0]
                if not value:
                    return self.unknown(f, why) and None
            else:
                pseudo = {**f, "type": "select" if w == "yesno" else "text", "tag": "input", "maxlength": 0, "autocomplete": ""}
                g = ff.plan_field(pseudo, self.facts)
                if g["status"] != "fill":
                    self.plans.append({**g, "type": f["type"], "idx": f["idx"]})
                    return
                value, key = g["value"], g["key"]
        p = self.log(f, "fill", key=key, value=value, display=ff._clip(value, 160), action=w)
        try:
            if w == "yesno":
                self.press(p, f, value)
            else:
                self.choose(p, f, prefs or [(value, False)], source)
        except Exception:  # noqa: BLE001
            try:
                self.drv.close_list()
            except Exception:  # noqa: BLE001
                pass
            p.update(status="needs_you", reason="I could not use this one on the page.", display="", value="")

    def press(self, p: dict, f: dict, value: str) -> None:
        opt = ff.match_option(value, f["options"])
        if opt is None:
            return p.update(status="needs_you", reason=f"My answer '{ff._clip(value, 40)}' is not Yes or No.", display="", value="") or None
        self.drv.click(opt["idx"])
        self.drv.settle()
        if self.drv.ad_values().get(f"t{opt['idx']}") != "1":
            return p.update(status="needs_you", reason="The page did not keep my choice. Pick it yourself.", display="", value="") or None
        p.update(value=opt["value"], display=opt["label"], target=opt["idx"])

    def choose(self, p: dict, f: dict, prefs: list[tuple[str, bool]], source: bool) -> None:
        drv, idx = self.drv, f["idx"]
        for query in ("", prefs[0][0][:40]):                     # open as is; a search-as-you-type list needs the words typed
            options = drv.combo_options(idx, query)
            if not options:
                continue
            chosen = wd._pick(prefs, options) if source else wd._pick_value(prefs[0][0], options)
            if chosen is not None:
                shown = drv.combo_pick(idx, chosen)
                if not wd._shown_ok(chosen, shown):
                    return p.update(status="needs_you", reason="The page did not keep my choice. Pick it yourself.", display="", value="") or None
                return p.update(display=ff._clip(shown, 120), value=chosen) or None
            drv.close_list()
        p.update(status="needs_you", reason="None of the choices clearly fits, so I did not pick one.", display="", value="")

    def verify(self) -> None:
        values = self.drv.values()
        for p in self.plans:
            if p["status"] == "fill" and p["action"] in ("text", "select") and p["idx"] >= 0 and not values.get(p["idx"], "x"):
                p.update(status="needs_you", reason="The page did not keep my answer. Type it yourself.")
