// Habits: today's ring cards, streaks, a 12-week heatmap per habit, add/edit sheet.
// API: docs/api/productivity.md (Habits). Deleting needs ?confirm=true and ui.confirm.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const COLORS = ["green", "blue", "red", "amber", "violet", "pink", "teal", "orange"];
const TONE_OF = { green: "good", red: "bad", blue: "info", amber: "warn", yellow: "warn", violet: "violet", purple: "violet", pink: "pink", teal: "teal", orange: "orange", gray: "gray", grey: "gray", accent: "accent" };
const EMOJI = ["📚", "💪", "🧘", "💧", "🏃", "🥗", "😴", "✍️", "🎸", "🧹", "🌱", "💊"];
const WEEKDAY_FIRST = 1; // Monday

// Applies a habit colour to a node: a named tone via data-tone, a #rrggbb through the --_tone custom property.
function paint(node, color) {
  if (typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color)) node.style.setProperty("--_tone", color);
  else node.setAttribute("data-tone", TONE_OF[String(color || "").toLowerCase()] || "accent");
  return node;
}
const targetLabel = (t) => (t === "daily" || t === 7 ? "Every day" : `${t}x a week`);
const unitLabel = (h) => (h.streak_unit === "week" ? "wk" : "day");

export function init(container, ctx) {
  if (!document.querySelector('link[data-css="habits"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/habits.css", "data-css": "habits" }));
  }
  const state = { data: null, error: "", loaded: false, seq: 0 };
  const root = el("div", { class: "hb" });
  container.replaceChildren(root);

  // ---------- data ----------
  async function load({ showLoading = false } = {}) {
    const seq = ++state.seq;
    if (showLoading) { state.loaded = false; state.error = ""; render(); }
    try {
      const data = await api("/api/habits/overview?days=84");
      if (seq !== state.seq) return;
      state.data = data; state.error = ""; state.loaded = true;
    } catch (e) {
      if (seq !== state.seq) return;
      state.error = e.message; state.loaded = true;
    }
    render();
  }

  async function check(habit, card) {
    card.classList.add("is-busy");
    ui.haptic(habit.done_today ? "light" : "success");
    try {
      const r = await api(`/api/habits/${habit.id}/check`, { method: "POST", json: {} });
      if (r.checked && ctx.toast && r.current_streak > 1) ctx.toast(`${r.current_streak}-${r.streak_unit} streak`, { kind: "good", icon: "flame" });
      await load();
    } catch (e) { card.classList.remove("is-busy"); ctx.toast(e.message, { kind: "bad" }); }
  }

  // ---------- render ----------
  function summary(habits) {
    const done = habits.filter((h) => h.done_today).length;
    const best = Math.max(0, ...habits.map((h) => (h.streak_unit === "week" ? 0 : h.best_streak)));
    const bestCur = Math.max(0, ...habits.map((h) => (h.streak_unit === "week" ? 0 : h.current_streak)));
    const avg = habits.length ? Math.round(habits.reduce((a, h) => a + h.completion, 0) / habits.length) : 0;
    const ring = ui.ringProgress({ value: habits.length ? done / habits.length : 0, size: 84, stroke: 9, tone: "good", big: `${done}/${habits.length}`, small: "today", label: "Habits done today" });
    ring.classList.add("hb-sum__ring");
    const stat = (iconName, tone, value, label) => el("div", { class: "hb-stat" }, ui.tile(iconName, tone, "sm"),
      el("span", { class: "hb-stat__v ui-num" }, value), el("span", { class: "hb-stat__l" }, label));
    return el("section", { class: "ui-card ui-card--gradient hb-sum", "aria-label": "Summary" }, ring,
      el("div", { class: "hb-sum__stats" },
        stat("flame", "orange", String(bestCur), "day streak now"),
        stat("award", "warn", String(best), "best streak"),
        stat("target", "teal", `${avg}%`, "completion")));
  }

  function habitCard(h) {
    const weekly = h.target !== "daily";
    const target = weekly ? Number(h.target) : 1;
    const value = weekly ? Math.min(1, (h.week_count || 0) / target) : (h.done_today ? 1 : 0);
    const ring = ui.ringProgress({ value, size: 88, stroke: 8, big: h.icon || "✓", label: h.name });
    paint(ring, h.color);
    ring.classList.add("hb-ring");
    const more = ui.button({ iconOnly: true, icon: "more", variant: "ghost", size: "sm", label: `Edit ${h.name}`, class: "hb-more", onClick: (e) => { e.stopPropagation(); openEditor(h); } });
    const card = el("div", { class: "ui-card hb-card" + (h.done_today ? " is-done" : ""), role: "button", tabindex: "0", "aria-pressed": String(h.done_today),
      "aria-label": `${h.name}: ${h.done_today ? "done today, tap to undo" : "tap to check off"}`,
      onclick: () => check(h, card), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); check(h, card); } } },
    more, ring,
    el("div", { class: "hb-card__name" }, h.name),
    el("div", { class: "hb-card__sub" },
      el("span", { class: "hb-flame" + (h.current_streak ? " is-lit" : "") }, ui.icon("flame"), el("b", { class: "ui-num" }, String(h.current_streak)), ` ${unitLabel(h)}`),
      weekly ? el("span", { class: "hb-week ui-num" }, `${h.week_count}/${target} this week`) : null));
    if (h.done_today) card.append(el("span", { class: "hb-card__tick" }, ui.icon("check")));
    return card;
  }

  function heatmap(h) {
    const grid = h.grid || [];
    const first = grid.length ? new Date(grid[0].date + "T00:00:00") : new Date();
    const lead = (first.getDay() - WEEKDAY_FIRST + 7) % 7;
    const cols = Math.ceil((lead + grid.length) / 7);
    const map = el("div", { class: "hb-heat", role: "img", "aria-label": `${h.name}, ${grid.filter((g) => g.done).length} days done in the last 12 weeks` });
    map.style.setProperty("--cols", String(cols));
    for (let i = 0; i < lead; i++) map.append(el("i", { class: "hb-cell is-pad" }));
    for (const g of grid) map.append(el("i", { class: "hb-cell" + (g.done ? " is-on" : "") + (g.date === state.data.today ? " is-today" : ""), title: g.date }));
    const head = el("div", { class: "hb-heatcard__head" },
      el("span", { class: "hb-heatcard__icon" }, h.icon || "✓"),
      el("div", { class: "hb-heatcard__name" }, el("strong", {}, h.name), el("span", {}, targetLabel(h.target))),
      ui.button({ iconOnly: true, icon: "edit", variant: "ghost", size: "sm", label: `Edit ${h.name}`, onClick: () => openEditor(h) }));
    const stats = el("div", { class: "hb-heatcard__stats" },
      el("span", {}, ui.icon("flame"), el("b", { class: "ui-num" }, String(h.current_streak)), ` ${unitLabel(h)} streak`),
      el("span", {}, ui.icon("award"), "best ", el("b", { class: "ui-num" }, String(h.best_streak))),
      el("span", {}, ui.icon("target"), el("b", { class: "ui-num" }, `${h.completion}%`), " done"));
    return paint(el("section", { class: "ui-card hb-heatcard" }, head, map, stats), h.color);
  }

  function render() {
    root.replaceChildren();
    if (!state.loaded && !state.error) { root.append(ui.skeleton("card", 1), ui.skeleton("tiles", 4)); return; }
    if (state.error) {
      root.append(ui.notice(state.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => load({ showLoading: true }) }));
      return;
    }
    const habits = state.data.habits || [];
    if (!habits.length) {
      root.append(ui.emptyState({ icon: "flame", title: "Build a streak", body: "Add a small daily habit and check it off each day. Streaks and a 12-week heatmap appear here.",
        action: { label: "Add a habit", icon: "plus", onClick: () => openEditor(null) } }));
      return;
    }
    root.append(summary(habits));
    root.append(el("div", { class: "ui-section-head hb-head" }, el("h2", { class: "ui-section-title" }, "Today"),
      ui.button({ label: "Add", icon: "plus", variant: "tonal", size: "sm", onClick: () => openEditor(null) })));
    root.append(el("div", { class: "hb-grid" }, ...habits.map(habitCard)));
    root.append(el("div", { class: "ui-section-head hb-head" }, el("h2", { class: "ui-section-title" }, "Last 12 weeks")));
    root.append(el("div", { class: "ui-stack" }, ...habits.map(heatmap)));
  }

  // ---------- add / edit ----------
  function openEditor(habit) {
    const editing = !!habit;
    const name = el("input", { type: "text", value: habit?.name || "", placeholder: "Drink water, Read, Stretch...", maxlength: "60", "aria-label": "Habit name", autocomplete: "off" });
    let icon = habit?.icon || EMOJI[0];
    let color = habit?.color || "green";
    const iconIn = el("input", { type: "text", value: icon, maxlength: "4", "aria-label": "Emoji", class: "hb-emoji-in", inputmode: "text" });
    iconIn.addEventListener("input", () => { icon = iconIn.value.trim() || EMOJI[0]; syncIcons(); });
    const emojiRow = el("div", { class: "hb-emojis ui-hscroll" });
    function syncIcons() {
      emojiRow.replaceChildren(...EMOJI.map((e) => el("button", { type: "button", class: "hb-emoji", "aria-pressed": String(e === icon), "aria-label": `Emoji ${e}`,
        onclick: () => { icon = e; iconIn.value = e; syncIcons(); } }, e)));
    }
    const swatches = el("div", { class: "hb-swatches", role: "radiogroup", "aria-label": "Colour" });
    function syncColors() {
      const list = COLORS.includes(color) ? COLORS : [color, ...COLORS];
      swatches.replaceChildren(...list.map((c) => paint(el("button", { type: "button", class: "hb-swatch", role: "radio", "aria-checked": String(c === color), "aria-label": c.startsWith("#") ? "Current colour" : c,
        onclick: () => { color = c; syncColors(); } }, ui.icon("check")), c)));
    }
    const targetSel = el("select", { "aria-label": "How often" },
      el("option", { value: "daily" }, "Every day"), ...[1, 2, 3, 4, 5, 6].map((n) => el("option", { value: String(n) }, `${n} day${n > 1 ? "s" : ""} a week`)));
    targetSel.value = String(habit?.target ?? "daily");
    syncIcons(); syncColors();
    const field = (label, ...n) => el("div", { class: "ui-field hb-field" }, el("span", { class: "ui-label" }, label), ...n);
    const content = el("div", { class: "ui-stack hb-form" }, field("Name", name),
      field("Icon", el("div", { class: "hb-icon-row" }, iconIn, emojiRow)), field("Colour", swatches), field("How often", targetSel));
    let archiveSw = null;
    if (editing) {
      archiveSw = ui.switchControl({ checked: !!habit.archived, label: "Archive this habit" });
      content.append(el("div", { class: "ui-row hb-archive" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Archive"), el("span", { class: "ui-row__sub" }, "Hide it but keep the history")), archiveSw));
    }
    const actions = [];
    if (editing) actions.push({ label: "Delete", icon: "trash", variant: "danger-soft", async onClick() {
      if (!await ui.confirm({ title: `Delete "${habit.name}"?`, message: "This erases its whole history. Archive it instead to keep the history.", confirmLabel: "Delete forever", danger: true })) return false;
      try { await api(`/api/habits/${habit.id}?confirm=true`, { method: "DELETE" }); ctx.toast("Habit deleted", { kind: "good" }); load(); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
      return true;
    } });
    actions.push({ label: editing ? "Save" : "Add habit", icon: "check", variant: "primary", async onClick() {
      const n = name.value.trim();
      if (!n) { ctx.toast("Give the habit a name.", { kind: "warn" }); name.focus(); return false; }
      const target = targetSel.value === "daily" ? "daily" : Number(targetSel.value);
      const json = { name: n, icon: iconIn.value.trim() || icon, color, target };
      try {
        if (editing) await api(`/api/habits/${habit.id}`, { method: "PATCH", json: { ...json, archived: archiveSw.input.checked } });
        else await api("/api/habits", { method: "POST", json });
        ui.haptic("success"); ctx.toast(editing ? "Saved" : "Habit added", { kind: "good" }); load();
      } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
      return true;
    } });
    ui.sheet({ title: editing ? "Edit habit" : "New habit", content, actions, class: "hb-sheet" });
  }

  ui.pullToRefresh(container, () => load());
  render();
  return {
    show() { load({ showLoading: !state.loaded }); },
    refresh() { return load(); },
    openSection(name) { if (name === "new" || name === "add") openEditor(null); },
    openItem(id) {
      const h = state.data?.habits.find((x) => x.id === id);
      if (h) openEditor(h);
      else api("/api/habits").then((r) => { const f = (r.habits || []).find((x) => x.id === id); if (f) openEditor(f); }).catch(() => {});
    },
  };
}
