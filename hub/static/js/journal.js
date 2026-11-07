// Journal: mood picker, entry editor, month calendar with mood dots, entries, On this day,
// mood trend and an AI weekly reflection. API: docs/api/productivity.md (Journal).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

// A sheet button that is still working ignores further taps (a double-tap must not send two requests).
const guard = (actions) => actions.map((a) => {
  if (!a || !a.onClick) return a;
  let working = false;
  return { ...a, onClick: async (x) => { if (working) return false; working = true; try { return await a.onClick(x); } finally { working = false; } } };
});


const MOODS = [
  { v: 1, emoji: "😣", label: "Awful", tone: "bad" },
  { v: 2, emoji: "🙁", label: "Low", tone: "orange" },
  { v: 3, emoji: "😐", label: "Okay", tone: "warn" },
  { v: 4, emoji: "🙂", label: "Good", tone: "teal" },
  { v: 5, emoji: "😄", label: "Great", tone: "good" },
];
const moodOf = (v) => MOODS[Math.max(1, Math.min(5, Math.round(v))) - 1];
const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const monthKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = (s) => {
  const t = ymd(new Date());
  if (s === t) return "Today";
  const y = new Date(); y.setDate(y.getDate() - 1);
  if (s === ymd(y)) return "Yesterday";
  return parseDay(s).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
};
const cleanTags = (text) => text.split(/[,\s]+/).map((t) => t.replace(/^#/, "").trim().toLowerCase()).filter(Boolean).slice(0, 10);

export function init(container, ctx) {
  if (!document.querySelector('link[data-css="journal"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/journal.css", "data-css": "journal" }));
  }
  const now = new Date();
  const state = {
    tab: "journal", month: new Date(now.getFullYear(), now.getMonth(), 1), selected: null,
    entries: [], loaded: false, error: "", onThisDay: [], stats: null, statsError: "", reflection: null, seq: 0,
  };
  const root = el("div", { class: "jr" });
  container.replaceChildren(root);
  const seg = ui.segmented({ options: [{ value: "journal", label: "Journal", icon: "book" }, { value: "insights", label: "Insights", icon: "trend" }],
    value: "journal", label: "Journal sections", block: true, onChange: (v) => { state.tab = v; render(); if (v === "insights" && !state.stats) loadStats(); } });
  const pane = el("div", { class: "jr-pane" });
  root.append(seg, pane);

  // ---------- data ----------
  async function loadMonth({ showLoading = false } = {}) {
    const seq = ++state.seq;
    if (showLoading) { state.loaded = false; state.error = ""; render(); }
    try {
      const data = await api(`/api/journal?month=${monthKey(state.month)}`);
      if (seq !== state.seq) return;
      state.entries = data.entries || []; state.error = ""; state.loaded = true;
    } catch (e) { if (seq !== state.seq) return; state.error = e.message; state.loaded = true; }
    render();
  }
  async function loadOnThisDay() {
    try { state.onThisDay = (await api("/api/journal/on-this-day")).entries || []; render(); } catch (_) { /* optional card */ }
  }
  async function loadStats() {
    state.statsError = "";
    try { state.stats = await api("/api/journal/stats?days=30"); } catch (e) { state.statsError = e.message; }
    render();
  }
  const reloadAll = () => { state.stats = null; loadMonth(); loadOnThisDay(); if (state.tab === "insights") loadStats(); };

  // ---------- pieces ----------
  function moodPrompt() {
    const row = el("div", { class: "jr-moods", role: "group", "aria-label": "How are you feeling?" },
      ...MOODS.map((m) => el("button", { type: "button", class: "jr-mood", "data-tone": m.tone, "aria-label": m.label, onclick: () => { ui.haptic("select"); openEditor({ mood: m.v }); } },
        el("span", { class: "jr-mood__e" }, m.emoji), el("span", { class: "jr-mood__l" }, m.label))));
    return el("section", { class: "ui-card jr-prompt" }, el("h2", { class: "jr-prompt__t" }, "How are you feeling?"),
      el("p", { class: "jr-prompt__s" }, "Tap a mood to write today's entry."), row);
  }

  function entryRow(e) {
    const m = e.mood ? moodOf(e.mood) : null;
    const lead = m ? el("span", { class: "jr-lead", "data-tone": m.tone }, m.emoji) : ui.tile("pen", "gray");
    return el("button", { type: "button", class: "ui-row ui-row--interactive jr-row", onclick: () => openEditor(e) }, lead,
      el("span", { class: "ui-row__body" },
        el("span", { class: "ui-row__title jr-day" }, fmtDay(e.date), m ? el("span", { class: "jr-moodname" }, m.label) : null),
        el("span", { class: "jr-text" }, e.text),
        e.tags?.length ? el("span", { class: "jr-tags" }, ...e.tags.map((t) => el("span", { class: "jr-tag" }, `#${t}`))) : null),
      el("span", { class: "ui-row__chev" }, ui.icon("chevron-right")));
  }

  function onThisDayCard() {
    if (!state.onThisDay.length) return null;
    return el("section", { class: "ui-card jr-otd" },
      el("div", { class: "jr-otd__head" }, ui.tile("history", "violet", "sm"), el("h2", { class: "ui-section-title" }, "On this day")),
      ...state.onThisDay.map((e) => el("button", { type: "button", class: "jr-otd__item", onclick: () => openEditor(e) },
        el("span", { class: "jr-otd__when" }, e.years_ago === 1 ? "1 year ago" : `${e.years_ago} years ago`, e.mood ? ` ${moodOf(e.mood).emoji}` : ""),
        el("span", { class: "jr-otd__text" }, e.text))));
  }

  function calendar() {
    const byDate = new Map();
    for (const e of state.entries) {
      const slot = byDate.get(e.date) || { n: 0, moods: [] };
      slot.n++; if (e.mood) slot.moods.push(e.mood);
      byDate.set(e.date, slot);
    }
    const y = state.month.getFullYear(), mo = state.month.getMonth();
    const first = new Date(y, mo, 1);
    const lead = (first.getDay() + 6) % 7;
    const days = new Date(y, mo + 1, 0).getDate();
    const today = ymd(new Date());
    const nextDisabled = monthKey(state.month) >= monthKey(new Date());
    const grid = el("div", { class: "jr-cal", role: "grid" },
      ...["M", "T", "W", "T", "F", "S", "S"].map((d) => el("span", { class: "jr-cal__dow", role: "columnheader" }, d)));
    for (let i = 0; i < lead; i++) grid.append(el("span", { class: "jr-cal__pad" }));
    for (let d = 1; d <= days; d++) {
      const key = `${y}-${pad(mo + 1)}-${pad(d)}`;
      const slot = byDate.get(key);
      const future = key > today;
      const avg = slot?.moods.length ? slot.moods.reduce((a, b) => a + b, 0) / slot.moods.length : 0;
      const dot = slot ? el("i", { class: "jr-dot", ...(avg ? { "data-tone": moodOf(avg).tone } : { "data-tone": "gray" }) }) : null;
      grid.append(el("button", { type: "button", class: "jr-cal__day" + (key === today ? " is-today" : "") + (key === state.selected ? " is-sel" : ""),
        disabled: future, "aria-pressed": String(key === state.selected),
        "aria-label": `${parseDay(key).toLocaleDateString("en-GB", { day: "numeric", month: "long" })}${slot ? `, ${slot.n} entr${slot.n > 1 ? "ies" : "y"}` : ""}`,
        onclick: () => { state.selected = state.selected === key ? null : key; ui.haptic("select"); render(); } },
      el("span", { class: "ui-num" }, String(d)), dot));
    }
    const shift = (n) => { state.month = new Date(y, mo + n, 1); state.selected = null; loadMonth({ showLoading: true }); };
    return el("section", { class: "ui-card jr-calcard" },
      el("div", { class: "jr-calcard__head" },
        ui.button({ iconOnly: true, icon: "chevron-left", variant: "ghost", label: "Previous month", onClick: () => shift(-1) }),
        el("h2", { class: "jr-calcard__title" }, state.month.toLocaleDateString("en-GB", { month: "long", year: "numeric" })),
        ui.button({ iconOnly: true, icon: "chevron-right", variant: "ghost", label: "Next month", disabled: nextDisabled, onClick: () => shift(1) })),
      grid);
  }

  function entriesSection() {
    const frag = [];
    const sel = state.selected;
    const list = sel ? state.entries.filter((e) => e.date === sel) : state.entries;
    frag.push(el("div", { class: "ui-section-head jr-head" },
      el("h2", { class: "ui-section-title" }, sel ? fmtDay(sel) : "Entries"),
      sel ? ui.button({ label: "Show all", variant: "ghost", size: "sm", onClick: () => { state.selected = null; render(); } }) : null));
    if (!state.loaded && !state.error) { frag.push(ui.skeleton("list", 3)); return frag; }
    if (state.error) {
      frag.push(ui.notice(state.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => loadMonth({ showLoading: true }) }));
      return frag;
    }
    if (!list.length) {
      frag.push(ui.emptyState(sel
        ? { icon: "pen", title: "Nothing written this day", body: "Add an entry for it.", compact: true, action: { label: "Write entry", icon: "plus", onClick: () => openEditor({ date: sel }) } }
        : { icon: "book", title: "No entries this month", body: "Pick a mood above or write a few lines about your day.", compact: true, action: { label: "Write entry", icon: "plus", onClick: () => openEditor({}) } }));
      return frag;
    }
    frag.push(el("div", { class: "ui-list ui-list--inset jr-list" }, ...list.map(entryRow)));
    return frag;
  }

  // ---------- insights ----------
  function trendCard() {
    const s = state.stats;
    const card = el("section", { class: "ui-card jr-trend" }, el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, "Mood, last 30 days")));
    const days = s.by_day || [];
    const W = 300, H = 90, bw = W / Math.max(1, days.length);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "jr-trend__svg"); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", s.average_mood ? `Average mood ${s.average_mood.toFixed(1)} out of 5` : "No mood data yet");
    days.forEach((d, i) => {
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      const h = d.mood ? (d.mood / 5) * (H - 6) : 3;
      r.setAttribute("x", String(i * bw + 1.2)); r.setAttribute("width", String(Math.max(2, bw - 2.4)));
      r.setAttribute("y", String(H - h)); r.setAttribute("height", String(h)); r.setAttribute("rx", "2.5");
      r.setAttribute("class", "jr-bar" + (d.mood ? "" : " is-empty"));
      if (d.mood) r.setAttribute("data-tone", moodOf(d.mood).tone);
      svg.append(r);
    });
    card.append(svg, el("div", { class: "jr-trend__axis" }, el("span", {}, days.length ? parseDay(days[0].date).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : ""), el("span", {}, "Today")));
    const dist = el("div", { class: "jr-dist" }, ...MOODS.map((m) => {
      const n = s.distribution?.[String(m.v)] || 0;
      const total = Math.max(1, s.mood_entries || 0);
      const bar = el("i", { class: "jr-dist__bar", "data-tone": m.tone }); bar.style.setProperty("--w", String(n / total));
      return el("div", { class: "jr-dist__row" }, el("span", { class: "jr-dist__e" }, m.emoji), el("span", { class: "jr-dist__track" }, bar), el("span", { class: "jr-dist__n ui-num" }, String(n)));
    }));
    card.append(dist);
    return card;
  }

  function reflectCard() {
    const card = el("section", { class: "ui-card jr-reflect" });
    const model = ui.segmented({ options: [{ value: "local", label: "Local model" }, { value: "claude", label: "Claude" }], value: "local", label: "Model" });
    const hint = el("p", { class: "ui-hint", hidden: true }, "Claude sends your journal text to Anthropic.");
    model.addEventListener("click", () => { hint.hidden = model.value !== "claude"; });
    const out = el("div", { class: "jr-reflect__out" });
    const btn = ui.button({ label: state.reflection ? "Reflect again" : "Reflect on my week", icon: "sparkles", variant: "primary", block: true, onClick: async () => {
      out.replaceChildren(ui.skeleton("lines", 4));
      try {
        state.reflection = await ui.busy(btn, api("/api/journal/reflect", { method: "POST", json: { model: model.value, days: 7 } }));
        showReflection(out); ui.haptic("success");
      } catch (e) { out.replaceChildren(ui.notice(e.message, { tone: "bad" })); }
    } });
    card.append(el("div", { class: "jr-reflect__head" }, ui.tile("sparkles", "violet"), el("div", {}, el("h2", { class: "ui-section-title" }, "Weekly reflection"), el("p", { class: "jr-reflect__sub" }, "An AI summary of your last 7 days."))),
      out, model, hint, btn);
    if (state.reflection) showReflection(out);
    return card;
  }
  function showReflection(out) {
    const r = state.reflection;
    if (!r) return;
    if (r.empty || !r.reflection) { out.replaceChildren(ui.emptyState({ icon: "book", title: "Nothing to reflect on yet", body: r.message || "Write a few entries first.", compact: true })); return; }
    const x = r.reflection;
    out.replaceChildren(el("div", { class: "jr-ref" },
      el("p", { class: "jr-ref__sum" }, x.summary),
      x.themes?.length ? el("div", { class: "jr-ref__themes" }, ...x.themes.map((t) => el("span", { class: "ui-chip ui-chip--static" }, t))) : null,
      x.mood_note ? el("p", { class: "jr-ref__note" }, ui.icon("smile"), x.mood_note) : null,
      x.suggestion ? el("div", { class: "jr-ref__tip" }, ui.icon("sparkles"), el("span", {}, x.suggestion)) : null,
      el("p", { class: "jr-ref__meta" }, `Based on ${r.entries} entr${r.entries === 1 ? "y" : "ies"}`)));
  }

  function statsRow() {
    const s = state.stats;
    const tile = (iconName, tone, v, l) => el("div", { class: "jr-st" }, ui.tile(iconName, tone, "sm"), el("b", { class: "ui-num" }, v), el("span", {}, l));
    return el("div", { class: "jr-stats" },
      tile("smile", "teal", s.average_mood ? s.average_mood.toFixed(1) : "-", "avg mood"),
      tile("flame", "orange", String(s.streak), "day streak"),
      tile("award", "warn", String(s.best_streak), "best streak"),
      tile("pen", "accent", String(s.entries), "entries"));
  }

  // ---------- render ----------
  function render() {
    pane.replaceChildren();
    if (state.tab === "journal") {
      pane.append(...[moodPrompt(), onThisDayCard(), calendar(), ...entriesSection()].filter(Boolean));
      return;
    }
    if (state.statsError) { pane.append(ui.notice(state.statsError, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: loadStats })); pane.append(reflectCard()); return; }
    if (!state.stats) { pane.append(ui.skeleton("card", 2)); return; }
    pane.append(statsRow(), trendCard(), reflectCard());
  }

  // ---------- editor ----------
  async function openEditor(entry = {}) {
    const editing = !!entry.id;
    let mood = entry.mood || 0;
    const today = ymd(new Date());
    const text = el("textarea", { rows: "8", maxlength: "10000", placeholder: "What happened today? How did it feel?", "aria-label": "Entry text", class: "jr-ed-text" });
    text.value = entry.text || "";
    const date = el("input", { type: "date", value: entry.date || state.selected || today, max: today, "aria-label": "Date" });
    const tags = el("input", { type: "text", value: (entry.tags || []).map((t) => `#${t}`).join(" "), placeholder: "#work #family", "aria-label": "Tags", autocapitalize: "none", spellcheck: "false" });
    const row = el("div", { class: "jr-moods jr-moods--sm", role: "radiogroup", "aria-label": "Mood" });
    function syncMood() {
      row.replaceChildren(...MOODS.map((m) => el("button", { type: "button", class: "jr-mood", role: "radio", "data-tone": m.tone, "aria-checked": String(m.v === mood), "aria-label": m.label,
        onclick: () => { mood = mood === m.v ? 0 : m.v; ui.haptic("select"); syncMood(); } }, el("span", { class: "jr-mood__e" }, m.emoji), el("span", { class: "jr-mood__l" }, m.label))));
    }
    syncMood();
    const field = (l, ...n) => el("div", { class: "ui-field jr-field" }, el("span", { class: "ui-label" }, l), ...n);
    const actions = [];
    if (editing) actions.push({ label: "Delete", icon: "trash", variant: "danger-soft", async onClick() {
      if (!await ui.confirm({ title: "Delete this entry?", message: "A journal entry can't be recovered once it's gone.", confirmLabel: "Delete entry", danger: true })) return false;
      try { await api(`/api/journal/${entry.id}?confirm=true`, { method: "DELETE" }); ctx.toast("Entry deleted", { kind: "good" }); reloadAll(); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
      return true;
    } });
    actions.push({ label: editing ? "Save" : "Save entry", icon: "check", variant: "primary", async onClick() {
      const t = text.value.trim();
      if (!t) { ctx.toast("Write a few words first.", { kind: "warn" }); text.focus(); return false; }
      const json = { text: t, date: date.value || today, mood: mood || null, tags: cleanTags(tags.value) };
      try {
        if (editing) await api(`/api/journal/${entry.id}`, { method: "PATCH", json });
        else await api("/api/journal", { method: "POST", json });
        ui.haptic("success"); ctx.toast("Entry saved", { kind: "good" });
        if (json.date.slice(0, 7) !== monthKey(state.month)) { const d = parseDay(json.date); state.month = new Date(d.getFullYear(), d.getMonth(), 1); }
        reloadAll();
      } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
      return true;
    } });
    ui.sheet({ title: editing ? "Edit entry" : "New entry", class: "jr-sheet", actions: guard(actions),
      content: el("div", { class: "ui-stack jr-form" }, field("Mood", row), text, field("Date", date), field("Tags", tags)) });
  }

  ui.pullToRefresh(container, async () => { reloadAll(); });
  render();
  return {
    show() { loadMonth({ showLoading: !state.loaded }); loadOnThisDay(); },
    refresh() { reloadAll(); },
    openSection(name) { if (name === "new" || name === "write") openEditor({}); else if (name === "insights" || name === "reflect") { seg.value = "insights"; state.tab = "insights"; render(); loadStats(); } },
    async openItem(id) {
      try { openEditor(await api(`/api/journal/${encodeURIComponent(id)}`)); } catch (e) { ctx.toast(e.message, { kind: "bad" }); }
    },
  };
}
