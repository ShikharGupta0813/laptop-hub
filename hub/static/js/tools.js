// Tools: calculator, unit converter, world clock, countdowns, snippets and clipboard history.
// All of it talks to /api/units, /api/countdowns and /api/snippets (docs/api/life.md).
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { copyText } from "./laptop.js";

// A sheet button that is still working ignores further taps (a double-tap must not send two requests).
const guard = (actions) => (actions || []).map((a) => {
  if (!a || !a.onClick) return a;
  let working = false;
  return { ...a, onClick: async (x) => { if (working) return false; working = true; try { return await a.onClick(x); } finally { working = false; } } };
});
const gsheet = (opts) => ui.sheet(opts.actions ? { ...opts, actions: guard(opts.actions) } : opts);


const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch (_) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ } },
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const errText = (e) => (e && e.message) || "Something went wrong.";

const SECTIONS = [
  { id: "calculator", label: "Calculator", icon: "hash" },
  { id: "converter", label: "Convert", icon: "arrow-right" },
  { id: "clock", label: "World clock", icon: "globe" },
  { id: "countdowns", label: "Countdowns", icon: "calendar" },
  { id: "snippets", label: "Snippets", icon: "note-plus" },
  { id: "clipboard", label: "Clipboard", icon: "clipboard" },
];
const ALIAS = { calc: "calculator", convert: "converter", units: "converter", time: "clock", "world-clock": "clock",
  countdown: "countdowns", snippet: "snippets", clip: "clipboard", "clipboard-history": "clipboard" };

function errorBox(message, retry) {
  return el("div", { class: "tl-error" }, ui.notice(message, { tone: "bad" }),
    retry ? ui.button({ label: "Try again", icon: "refresh", variant: "secondary", size: "sm", onClick: retry }) : null);
}

export function init(container, ctx) {
  if (!document.querySelector('link[href="/css/tools.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/tools.css" }));
  }
  container.classList.add("tl-root");
  let current = "calculator";
  let visible = false;
  const panels = {};
  const handles = {};

  const tabs = el("div", { class: "tl-tabs ui-hscroll", role: "tablist", "aria-label": "Tools" });
  const tabButtons = {};
  for (const s of SECTIONS) {
    const b = el("button", { type: "button", class: "ui-chip tl-tab", role: "tab", "data-sec": s.id, onclick: () => go(s.id) }, ui.icon(s.icon), s.label);
    tabButtons[s.id] = b;
    tabs.append(b);
  }
  const body = el("div", { class: "tl-body" });
  container.append(el("div", { class: "tl-wrap" }, tabs, body));

  function go(id) {
    id = ALIAS[id] || id;
    if (!SECTIONS.some((s) => s.id === id)) id = "calculator";
    current = id;
    for (const s of SECTIONS) {
      const on = s.id === id;
      tabButtons[s.id].classList.toggle("is-on", on);
      tabButtons[s.id].setAttribute("aria-selected", String(on));
      tabButtons[s.id].tabIndex = on ? 0 : -1;
    }
    if (!panels[id]) {
      panels[id] = el("section", { class: "tl-panel", role: "tabpanel", "aria-label": SECTIONS.find((s) => s.id === id).label });
      body.append(panels[id]);
      handles[id] = BUILDERS[id](panels[id]);
    }
    for (const [k, p] of Object.entries(panels)) p.hidden = k !== id;
    if (visible) handles[id]?.show?.();
    for (const [k, h] of Object.entries(handles)) if (k !== id) h?.hide?.();
    tabButtons[id].scrollIntoView?.({ inline: "center", block: "nearest" });
  }

  // ===== calculator ===================================================================================
  function buildCalculator(root) {
    const HIST = "hub.tools.calc";
    let history = store.get(HIST, []);
    if (!Array.isArray(history)) history = [];
    const input = el("input", { type: "text", class: "tl-expr ui-input--lg", inputmode: "text", autocomplete: "off", autocapitalize: "off",
      spellcheck: "false", enterkeyhint: "go", placeholder: "12 * (3 + 4) or sqrt(2)", "aria-label": "Expression" });
    const out = el("div", { class: "tl-result ui-num", "aria-live": "polite" }, "0");
    const hint = el("p", { class: "tl-hint" }, "Press = or Enter. Try 15% of 480 as 480*0.15.");
    const histBox = el("div", { class: "tl-hist" });
    let busy = false;

    function paintHistory() {
      histBox.replaceChildren();
      if (!history.length) { histBox.append(el("p", { class: "tl-hint" }, "Your last calculations appear here.")); return; }
      histBox.append(el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "History"),
        el("button", { type: "button", class: "ui-link", onclick: () => { history = []; store.set(HIST, history); paintHistory(); } }, "Clear")));
      histBox.append(el("div", { class: "ui-list ui-list--inset" }, history.map((h) =>
        el("button", { type: "button", class: "ui-row ui-row--interactive tl-hrow", onclick: () => { input.value = h.expr; out.textContent = h.result; input.focus(); } },
          el("div", { class: "ui-row__body" }, el("div", { class: "ui-row__sub ui-truncate" }, h.expr), el("div", { class: "ui-row__title ui-num" }, h.result))))));
    }

    async function calc() {
      const expr = input.value.trim();
      if (!expr || busy) return;
      busy = true;
      try {
        const data = await api(`/api/units/calc?expr=${encodeURIComponent(expr)}`);
        const text = String(data.result_text ?? data.result);
        out.textContent = text;
        out.classList.remove("is-error");
        hint.textContent = "Tap the result to copy it.";
        history = [{ expr, result: text }, ...history.filter((h) => h.expr !== expr)].slice(0, 12);
        store.set(HIST, history);
        paintHistory();
        ui.haptic("light");
      } catch (e) {
        out.textContent = "—";
        out.classList.add("is-error");
        hint.textContent = errText(e);
        ui.haptic("error");
      }
      busy = false;
    }
    out.addEventListener("click", async () => {
      if (out.classList.contains("is-error") || out.textContent === "0") return;
      ui.toast((await copyText(out.textContent)) ? "Copied" : "Couldn't copy", { kind: "good" });
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); calc(); } });

    const insert = (t) => {
      const s = input.selectionStart ?? input.value.length, e2 = input.selectionEnd ?? s;
      input.value = input.value.slice(0, s) + t + input.value.slice(e2);
      const pos = s + t.length;
      try { input.setSelectionRange(pos, pos); } catch (_) { /* ignore */ }
      ui.haptic("select");
    };
    const keys = [
      ["C", "clear"], ["(", "("], [")", ")"], ["÷", "/"],
      ["7", "7"], ["8", "8"], ["9", "9"], ["×", "*"],
      ["4", "4"], ["5", "5"], ["6", "6"], ["−", "-"],
      ["1", "1"], ["2", "2"], ["3", "3"], ["+", "+"],
      ["0", "0"], [".", "."], ["⌫", "back"], ["=", "eq"],
    ];
    const pad = el("div", { class: "tl-pad" }, keys.map(([label, val]) => {
      const kind = val === "eq" ? "tl-key eq" : /[0-9.]/.test(val) ? "tl-key num" : "tl-key op";
      return el("button", { type: "button", class: kind, "aria-label": { clear: "Clear", back: "Backspace", eq: "Equals", "/": "Divide", "*": "Multiply", "-": "Minus", "+": "Plus" }[val] || val,
        onclick: () => {
          if (val === "clear") { input.value = ""; out.textContent = "0"; out.classList.remove("is-error"); }
          else if (val === "back") input.value = input.value.slice(0, -1);
          else if (val === "eq") calc();
          else insert(val);
        } }, label);
    }));
    const fn = el("div", { class: "tl-fn ui-hscroll" }, ["sqrt(", "pi", "^", "%", "sin(", "log(", "abs(", "round("].map((t) =>
      el("button", { type: "button", class: "ui-chip", onclick: () => insert(t === "%" ? "%" : t) }, t)));
    root.append(el("div", { class: "ui-card ui-card--elevated tl-display" }, input, out, hint), fn, pad, histBox);
    paintHistory();
    return { show() { /* keep keyboard closed on phones */ } };
  }

  // ===== converter ====================================================================================
  function buildConverter(root) {
    let units = null;
    const value = el("input", { type: "number", inputmode: "decimal", step: "any", class: "ui-input--lg", value: "1", "aria-label": "Value" });
    const cat = el("select", { "aria-label": "What are you measuring?" });
    const from = el("select", { "aria-label": "From unit" });
    const to = el("select", { "aria-label": "To unit" });
    const out = el("div", { class: "tl-result ui-num", "aria-live": "polite" }, "—");
    const sub = el("p", { class: "tl-hint" }, "");
    const swap = ui.button({ icon: "refresh", iconOnly: true, variant: "tonal", label: "Swap units", onClick: () => {
      const a = from.value; from.value = to.value; to.value = a; convert();
    } });
    const card = el("div", { class: "ui-card ui-card--elevated tl-display" },
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Value"), value),
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Type"), cat),
      el("div", { class: "tl-fromto" }, el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "From"), from), swap,
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "To"), to)),
      out, sub);
    const slot = el("div", {});
    root.append(slot);
    let token = 0;

    function fillUnits(keepFrom) {
      const list = (units && units[cat.value]) || [];
      for (const sel of [from, to]) sel.replaceChildren(...list.map((u) => el("option", { value: u }, u)));
      if (list.length) { from.value = keepFrom && list.includes(keepFrom) ? keepFrom : list[0]; to.value = list[1] || list[0]; }
    }
    const convert = debounce(async () => {
      const v = value.value.trim();
      if (v === "" || !from.value || !to.value) { out.textContent = "—"; sub.textContent = ""; return; }
      const my = ++token;
      try {
        const q = new URLSearchParams({ value: v, from: from.value, to: to.value });
        const d = await api(`/api/units/convert?${q}`);
        if (my !== token) return;
        out.textContent = `${d.result_text ?? d.result} ${to.value}`;
        out.classList.remove("is-error");
        sub.textContent = `${v} ${from.value} =`;
      } catch (e) {
        if (my !== token) return;
        out.textContent = "—";
        out.classList.add("is-error");
        sub.textContent = errText(e);
      }
    }, 220);
    for (const n of [value, from, to]) n.addEventListener("input", convert);
    cat.addEventListener("change", () => { fillUnits(); convert(); });
    out.addEventListener("click", async () => { if (out.textContent !== "—") ui.toast((await copyText(out.textContent)) ? "Copied" : "Couldn't copy", { kind: "good" }); });

    async function load() {
      slot.replaceChildren(ui.skeleton("card", 1));
      try {
        const d = await api("/api/units/list");
        units = d.units || {};
        const cats = Object.keys(units);
        if (!cats.length) { slot.replaceChildren(ui.emptyState({ icon: "sliders", title: "No units available", compact: true })); return; }
        cat.replaceChildren(...cats.map((c) => el("option", { value: c }, c.charAt(0).toUpperCase() + c.slice(1))));
        cat.value = cats.includes("length") ? "length" : cats[0];
        fillUnits();
        if (units[cat.value]?.includes("km")) { from.value = "km"; to.value = units[cat.value].includes("mi") ? "mi" : to.value; }
        slot.replaceChildren(card);
        convert();
      } catch (e) { slot.replaceChildren(errorBox(errText(e), load)); }
    }
    load();
    return {};
  }

  // ===== world clock ==================================================================================
  function buildClock(root) {
    const KEY = "hub.tools.zones";
    let zones = store.get(KEY, ["UTC", "Asia/Kolkata"]);
    if (!Array.isArray(zones) || !zones.length) zones = ["UTC", "Asia/Kolkata"];
    let timer = null;
    let loadedOnce = false;
    const list = el("div", { class: "tl-clocks ui-stack" });
    const add = el("input", { type: "text", list: "tl-zones", placeholder: "Add a city, e.g. Europe/London", autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": "Time zone to add", enterkeyhint: "done" });
    const dl = el("datalist", { id: "tl-zones" });
    try { for (const z of (Intl.supportedValuesOf?.("timeZone") || [])) dl.append(el("option", { value: z })); } catch (_) { /* older browsers */ }
    const addBtn = ui.button({ label: "Add", icon: "plus", variant: "primary", onClick: () => addZone() });
    root.append(el("div", { class: "tl-add" }, el("div", { class: "ui-input-wrap" }, add), addBtn, dl), list);
    add.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addZone(); } });

    async function addZone() {
      let z = add.value.trim();
      if (!z) return;
      z = z.replace(/\s+/g, "_");
      const canon = (Intl.supportedValuesOf?.("timeZone") || []).find((x) => x.toLowerCase() === z.toLowerCase());
      z = canon || z;
      if (zones.includes(z)) { ctx.toast("That one is already on the list."); return; }
      try {
        await api(`/api/units/time?zones=${encodeURIComponent(z)}`);
      } catch (e) { ctx.toast(errText(e), { kind: "bad" }); return; }
      zones.push(z);
      zones = zones.slice(0, 12);
      store.set(KEY, zones);
      add.value = "";
      load();
    }

    async function load() {
      if (!loadedOnce) list.replaceChildren(ui.skeleton("list", 2));
      try {
        const d = await api(`/api/units/time?zones=${encodeURIComponent(zones.join(","))}`);
        loadedOnce = true;
        list.replaceChildren(...(d.clocks || []).map((c) => {
          const label = c.zone.split("/").pop().replace(/_/g, " ");
          return el("div", { class: "ui-card tl-clock" },
            el("div", { class: "tl-clock-main" },
              el("div", { class: "tl-clock-zone" }, label, c.dst ? ui.badge("DST", "warn") : null),
              el("div", { class: "tl-clock-sub" }, `${c.weekday}, ${c.date} · ${c.abbreviation || ""} ${c.offset || ""}`.trim())),
            el("div", { class: "tl-clock-time ui-num" }, c.time),
            ui.button({ iconOnly: true, icon: "close", variant: "ghost", size: "sm", label: `Remove ${label}`, onClick: () => {
              if (zones.length <= 1) { ctx.toast("Keep at least one clock."); return; }
              zones = zones.filter((z) => z !== c.zone); store.set(KEY, zones); load();
            } }));
        }));
      } catch (e) {
        if (!loadedOnce) list.replaceChildren(errorBox(errText(e), load));
      }
    }
    function tick() { clearInterval(timer); timer = setInterval(load, 30000); }
    load();
    return { show() { load(); tick(); }, hide() { clearInterval(timer); timer = null; } };
  }

  // ===== countdowns ===================================================================================
  function buildCountdowns(root) {
    const list = el("div", { class: "ui-stack tl-cds" });
    const head = el("div", { class: "tl-sechead" }, el("p", { class: "tl-hint" }, "Days left to the things you are waiting for."),
      ui.button({ label: "New", icon: "plus", variant: "primary", size: "sm", onClick: () => edit() }));
    root.append(head, list);

    function edit() {
      const title = el("input", { type: "text", maxlength: "80", placeholder: "Trip to Goa", autocomplete: "off", "aria-label": "Title" });
      const date = el("input", { type: "date", "aria-label": "Date" });
      const emoji = el("input", { type: "text", maxlength: "4", placeholder: "🎉", "aria-label": "Emoji" });
      const repeat = ui.segmented({ label: "Repeat", block: true, value: "none", options: [{ value: "none", label: "Once" }, { value: "yearly", label: "Every year" }] });
      const err = el("p", { class: "tl-hint is-error", hidden: true });
      const form = el("div", { class: "ui-stack" },
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "What is it?"), title),
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Date"), date),
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Emoji (optional)"), emoji),
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Repeat"), repeat), err);
      const sh = gsheet({ title: "New countdown", content: form, actions: [
        { label: "Cancel", variant: "secondary" },
        { label: "Save", variant: "primary", onClick: async () => {
          if (!title.value.trim() || !date.value) { err.textContent = "Give it a name and a date."; err.hidden = false; return false; }
          const body = { title: title.value.trim(), date: date.value, repeat: repeat.value };
          if (emoji.value.trim()) body.emoji = emoji.value.trim();
          try { await api("/api/countdowns", { method: "POST", json: body }); ui.toast("Countdown added", { kind: "good" }); ui.haptic("success"); load(); }
          catch (e) { err.textContent = errText(e); err.hidden = false; return false; }
        } }] });
      void sh;
    }

    async function remove(c) {
      if (!(await ui.confirm({ title: `Delete "${c.title}"?`, message: "This countdown will be removed.", confirmLabel: "Delete", danger: true }))) return;
      try { await api(`/api/countdowns/${c.id}`, { method: "DELETE" }); ui.toast("Deleted", { kind: "good" }); load(); }
      catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
    }

    function item(c) {
      const days = c.days_remaining;
      const tone = c.past ? "gray" : days <= 7 ? "warn" : "accent";
      const label = c.past ? `${Math.abs(days)} days ago` : days === 0 ? "Today" : days === 1 ? "Tomorrow" : `${days} days`;
      const when = new Date(`${c.next_date || c.date}T00:00:00`);
      const dateText = Number.isNaN(when.getTime()) ? (c.next_date || c.date) : when.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
      return el("div", { class: `ui-card tl-cd${c.past ? " is-past" : ""}`, "data-tone": tone },
        el("span", { class: "tl-cd-emoji", "aria-hidden": "true" }, c.emoji || "📅"),
        el("div", { class: "tl-cd-main" },
          el("div", { class: "tl-cd-title" }, c.title),
          el("div", { class: "tl-cd-sub" }, dateText, c.repeat === "yearly" ? " · yearly" : "", c.years ? ` · turns ${c.years}` : "")),
        el("div", { class: "tl-cd-days ui-num" }, el("b", {}, c.past || days <= 1 ? label : String(days)), c.past || days <= 1 ? null : el("span", {}, days === 1 ? "day" : "days")),
        ui.button({ iconOnly: true, icon: "trash", variant: "ghost", size: "sm", label: `Delete ${c.title}`, onClick: () => remove(c) }));
    }

    async function load() {
      list.replaceChildren(ui.skeleton("list", 3));
      try {
        const d = await api("/api/countdowns");
        const items = d.countdowns || [];
        list.replaceChildren(...(items.length ? items.map(item) : [ui.emptyState({ icon: "calendar", title: "No countdowns yet", body: "Add a birthday, a trip or a deadline.",
          action: { label: "Add countdown", icon: "plus", onClick: () => edit() } })]));
      } catch (e) { list.replaceChildren(errorBox(errText(e), load)); }
    }
    load();
    return { show() { load(); } };
  }

  // ===== snippets =====================================================================================
  function buildSnippets(root) {
    const search = el("input", { type: "search", placeholder: "Search snippets", autocomplete: "off", "aria-label": "Search snippets" });
    const list = el("div", { class: "ui-stack tl-snips" });
    let q = "";
    root.append(el("div", { class: "tl-sechead" }, el("div", { class: "ui-input-wrap tl-grow" }, ui.icon("search"), search),
      ui.button({ label: "New", icon: "plus", variant: "primary", size: "sm", onClick: () => edit() })), list);
    search.addEventListener("input", debounce(() => { q = search.value.trim(); load(); }, 250));

    function edit(sn) {
      const title = el("input", { type: "text", maxlength: "80", placeholder: "Email signature", "aria-label": "Title", value: sn ? sn.title : "" });
      const text = el("textarea", { rows: "6", placeholder: "The text to paste", "aria-label": "Text" });
      text.value = sn ? sn.text : "";
      const tags = el("input", { type: "text", placeholder: "work, reply", "aria-label": "Tags", autocapitalize: "off", value: sn ? (sn.tags || []).join(", ") : "" });
      const err = el("p", { class: "tl-hint is-error", hidden: true });
      const form = el("div", { class: "ui-stack" },
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Title"), title),
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Text"), text),
        el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Tags (comma separated)"), tags), err);
      gsheet({ title: sn ? "Edit snippet" : "New snippet", content: form, actions: [
        { label: "Cancel", variant: "secondary" },
        { label: "Save", variant: "primary", onClick: async () => {
          if (!title.value.trim() || !text.value.trim()) { err.textContent = "A snippet needs a title and some text."; err.hidden = false; return false; }
          const body = { title: title.value.trim(), text: text.value, tags: tags.value.split(",").map((t) => t.trim().replace(/^#/, "")).filter(Boolean) };
          try {
            if (sn) await api(`/api/snippets/${sn.id}`, { method: "PATCH", json: body });
            else await api("/api/snippets", { method: "POST", json: body });
            ui.toast("Saved", { kind: "good" }); load();
          } catch (e) { err.textContent = errText(e); err.hidden = false; return false; }
        } }] });
    }

    async function use(sn) {
      const ok = await copyText(sn.text);
      if (!ok) { ctx.toast("Your browser blocked copying. Open the snippet and select the text.", { kind: "warn" }); return; }
      ui.toast("Copied to your phone", { kind: "good" });
      ui.haptic("success");
      api(`/api/snippets/${sn.id}/use`, { method: "POST" }).then(load).catch(() => {});
    }
    async function pin(sn) {
      try { await api(`/api/snippets/${sn.id}`, { method: "PATCH", json: { pinned: !sn.pinned } }); load(); }
      catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
    }
    async function remove(sn) {
      if (!(await ui.confirm({ title: `Delete "${sn.title}"?`, message: "This snippet will be removed.", confirmLabel: "Delete", danger: true }))) return;
      try { await api(`/api/snippets/${sn.id}`, { method: "DELETE" }); ui.toast("Deleted", { kind: "good" }); load(); }
      catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
    }

    function item(sn) {
      return el("div", { class: "ui-card tl-snip" },
        el("div", { class: "tl-snip-head" },
          el("div", { class: "tl-snip-title" }, sn.pinned ? ui.icon("star", 14) : null, sn.title),
          el("span", { class: "tl-hint" }, sn.uses ? `used ${sn.uses}×` : "")),
        el("p", { class: "tl-snip-text" }, sn.text),
        sn.tags && sn.tags.length ? el("div", { class: "tl-tags" }, sn.tags.map((t) => el("span", { class: "ui-chip ui-chip--static" }, `#${t}`))) : null,
        el("div", { class: "tl-snip-actions" },
          ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", onClick: () => use(sn) }),
          ui.button({ iconOnly: true, icon: "star", variant: "ghost", size: "sm", label: sn.pinned ? "Unpin" : "Pin", "aria-pressed": String(!!sn.pinned), class: sn.pinned ? "is-pinned" : "", onClick: () => pin(sn) }),
          ui.button({ iconOnly: true, icon: "edit", variant: "ghost", size: "sm", label: "Edit", onClick: () => edit(sn) }),
          ui.button({ iconOnly: true, icon: "trash", variant: "ghost", size: "sm", label: "Delete", onClick: () => remove(sn) })));
    }

    let token = 0;
    async function load() {
      const my = ++token;
      if (!list.children.length) list.replaceChildren(ui.skeleton("list", 3));
      try {
        const d = await api(`/api/snippets${q ? `?q=${encodeURIComponent(q)}` : ""}`);
        if (my !== token) return;
        const items = d.snippets || [];
        list.replaceChildren(...(items.length ? items.map(item) : [ui.emptyState(q
          ? { icon: "search", title: `Nothing matches "${q}"`, compact: true }
          : { icon: "note-plus", title: "No snippets yet", body: "Save text you paste often.", action: { label: "New snippet", icon: "plus", onClick: () => edit() } })]));
      } catch (e) { if (my === token) list.replaceChildren(errorBox(errText(e), load)); }
    }
    load();
    return { show() { load(); } };
  }

  // ===== clipboard history ============================================================================
  function buildClipboard(root) {
    const list = el("div", { class: "ui-list ui-list--inset tl-clips" });
    const holder = el("div", {});
    let count = 0;
    const clearBtn = ui.button({ label: "Clear", icon: "trash", variant: "danger-soft", size: "sm", onClick: () => clear() });
    clearBtn.hidden = true;
    const sendBtn = ui.button({ label: "Send phone clipboard", icon: "send", variant: "primary", block: true, onClick: () => sendPhone() });
    root.append(el("div", { class: "ui-card tl-send" },
      el("p", { class: "tl-hint" }, "Copy something on this phone, then send it so it shows up in the laptop's history. Passwords and keys are skipped."), sendBtn),
    el("div", { class: "tl-sechead" }, el("h3", { class: "ui-section-title" }, "History"), clearBtn), holder);

    async function capture(text) {
      const d = await api("/api/snippets/clip/capture", { method: "POST", json: { text } });
      if (d.saved) { ui.toast("Saved to clipboard history", { kind: "good" }); ui.haptic("success"); load(); }
      else ctx.toast(d.reason ? `Not saved: ${d.reason}` : "Not saved.", { kind: "warn" });
    }
    function pasteSheet() {
      const area = el("textarea", { rows: "5", placeholder: "Long-press here and choose Paste", "aria-label": "Text to send" });
      const err = el("p", { class: "tl-hint is-error", hidden: true });
      gsheet({ title: "Paste what you copied", content: el("div", { class: "ui-stack" }, area, err), actions: [
        { label: "Cancel", variant: "secondary" },
        { label: "Send", variant: "primary", onClick: async () => {
          if (!area.value.trim()) { err.textContent = "Paste something first."; err.hidden = false; return false; }
          try { await capture(area.value); } catch (e) { err.textContent = errText(e); err.hidden = false; return false; }
        } }] });
    }
    async function sendPhone() {
      let text = "";
      try { text = await navigator.clipboard.readText(); } catch (_) { pasteSheet(); return; }
      if (!text.trim()) { ctx.toast("Your phone's clipboard is empty.", { kind: "warn" }); return; }
      try { await ui.busy(sendBtn, capture(text)); } catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
    }
    async function clear() {
      if (!(await ui.confirm({ title: "Clear clipboard history?", message: `All ${count} saved clips will be deleted from the laptop.`, confirmLabel: "Clear all", danger: true }))) return;
      try { await api("/api/snippets/clip", { method: "DELETE", json: { confirm: true } }); ui.toast("History cleared", { kind: "good" }); load(); }
      catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
    }
    async function saveAsSnippet(c) {
      try {
        await api("/api/snippets", { method: "POST", json: { title: c.text.replace(/\s+/g, " ").slice(0, 40) || "Clip", text: c.text } });
        ui.toast("Saved as a snippet", { kind: "good" });
      } catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
    }

    async function load() {
      if (!holder.children.length) holder.replaceChildren(ui.skeleton("list", 3));
      try {
        const d = await api("/api/snippets/clip");
        const clips = d.clips || [];
        count = clips.length;
        clearBtn.hidden = !clips.length;
        if (!clips.length) {
          holder.replaceChildren(ui.emptyState({ icon: "clipboard", title: "Nothing copied yet", body: "Things you copy on the laptop show up here.", compact: true }));
          return;
        }
        list.replaceChildren(...clips.map((c) => el("div", { class: "ui-row tl-clip" },
          el("div", { class: "ui-row__body" }, el("div", { class: "tl-clip-text" }, c.text), el("div", { class: "ui-row__sub" }, ui.relTime(c.time))),
          ui.button({ iconOnly: true, icon: "copy", variant: "ghost", size: "sm", label: "Copy", onClick: async () => ui.toast((await copyText(c.text)) ? "Copied" : "Couldn't copy", { kind: "good" }) }),
          ui.button({ iconOnly: true, icon: "note-plus", variant: "ghost", size: "sm", label: "Save as snippet", onClick: () => saveAsSnippet(c) }))));
        holder.replaceChildren(list);
      } catch (e) { holder.replaceChildren(errorBox(errText(e), load)); }
    }
    load();
    return { show() { load(); } };
  }

  const BUILDERS = { calculator: buildCalculator, converter: buildConverter, clock: buildClock, countdowns: buildCountdowns, snippets: buildSnippets, clipboard: buildClipboard };
  go("calculator");

  return {
    show() { visible = true; handles[current]?.show?.(); },
    hide() { visible = false; for (const h of Object.values(handles)) h?.hide?.(); },
    openSection(name) { go(name || "calculator"); },
    openItem(id, section) { go(section || "snippets"); },
    refresh() { handles[current]?.show?.(); },
  };
}
