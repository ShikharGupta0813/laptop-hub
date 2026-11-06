// Reminders: quick add with a parsed preview, grouped list, snooze/done/edit, and live timers.
// Everything from the API is rendered with el()/textContent, never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

// replaceChildren that skips null/false entries (conditional children).
function fill(node, ...kids) { node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false)); }

function ensureCss() {
  if ([...document.querySelectorAll("link[rel=stylesheet]")].some((l) => (l.getAttribute("href") || "").includes("/css/reminders.css"))) return;
  document.head.append(el("link", { rel: "stylesheet", href: "/css/reminders.css" }));
}

const message = (e) => (e && e.message ? e.message : "Something went wrong.");
const pad = (n) => String(n).padStart(2, "0");
const PRESETS = [{ min: 5, label: "5 min" }, { min: 10, label: "10 min" }, { min: 25, label: "25 min" }];
const REPEATS = [["none", "Does not repeat"], ["daily", "Every day"], ["weekdays", "Weekdays"], ["weekly", "Every week"],
  ["monthly", "Every month"], ["yearly", "Every year"], ["every_n_minutes", "Every N minutes"]];

function repeatLabel(r) {
  if (!r || r.repeat === "none") return "";
  if (r.repeat_text) return r.repeat_text;
  if (r.repeat === "every_n_minutes") return r.n % 60 === 0 ? `Every ${r.n / 60} h` : `Every ${r.n} min`;
  return (REPEATS.find(([v]) => v === r.repeat) || [, r.repeat])[1];
}

function toLocalInput(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function clock(sec) {
  sec = Math.max(0, Math.ceil(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function init(container, ctx) {
  ensureCss();
  let reminders = [];
  let timers = [];
  let loadedAt = 0;
  let tick = null;
  let previewToken = 0;
  let previewTimer = null;
  let previewOk = false;
  let visible = false;

  // ---- quick add ------------------------------------------------------------------------------
  const input = el("input", { type: "text", class: "rm-input", placeholder: "Call mom tomorrow 9am", maxlength: "300",
    "aria-label": "New reminder or timer", enterkeyhint: "done", autocomplete: "off", autocapitalize: "sentences" });
  const addBtn = ui.button({ label: "Add", icon: "plus", variant: "primary", onClick: save, disabled: true });
  const echo = el("div", { class: "rm-echo", "aria-live": "polite" });
  const quick = el("section", { class: "ui-card ui-card--glass rm-quick", "aria-label": "Quick add" },
    el("div", { class: "rm-quick-row" }, input, addBtn), echo,
    el("p", { class: "rm-examples" }, "Say it naturally: \"in 10 minutes check oven\", \"every weekday 8:30 standup\", \"timer 10 minutes pasta\"."));

  function setEcho(kind, text) {
    if (!text) { fill(echo, ); return; }
    const tone = kind === "ok" ? "good" : kind === "err" ? "warn" : "";
    fill(echo, el("div", { class: `rm-echo-chip${tone ? ` is-${tone}` : ""}` },
      ui.icon(kind === "ok" ? "check-circle" : kind === "err" ? "info" : "clock"), el("span", {}, text)));
  }

  input.addEventListener("input", () => {
    clearTimeout(previewTimer);
    previewOk = false; addBtn.disabled = true;
    const text = input.value.trim();
    if (!text) { setEcho(); return; }
    setEcho("wait", "Understanding…");
    previewTimer = setTimeout(() => preview(text), 380);
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (!addBtn.disabled) save(); } });

  async function preview(text) {
    const mine = ++previewToken;
    try {
      const r = await api("/api/reminders/quick", { method: "POST", json: { text, preview: true } });
      if (mine !== previewToken) return;
      previewOk = true; addBtn.disabled = false;
      setEcho("ok", `${r.kind === "timer" ? "Timer · " : ""}${r.echo || "Looks good"}`);
    } catch (error) {
      if (mine !== previewToken) return;
      previewOk = false; addBtn.disabled = true;
      setEcho("err", message(error));
    }
  }

  async function save() {
    const text = input.value.trim();
    if (!text || !previewOk) return;
    try {
      const r = await ui.busy(addBtn, api("/api/reminders/quick", { method: "POST", json: { text } }));
      ui.toast(r.echo || "Saved", { kind: "good", icon: r.kind === "timer" ? "timer" : "alarm" });
      input.value = ""; previewOk = false; addBtn.disabled = true; setEcho();
      await load(true);
    } catch (error) {
      ui.toast(message(error), { kind: "bad" });
    }
  }

  // ---- timers ---------------------------------------------------------------------------------
  const timersBox = el("section", { class: "rm-timers", "aria-label": "Timers" });
  const timerRings = new Map();    // id -> {ring, label, ends}

  function remainingOf(t) {
    if (t.state === "finished") return 0;
    const ends = new Date(t.ends_at).getTime();
    return Number.isNaN(ends) ? t.remaining_seconds : (ends - Date.now()) / 1000;
  }

  async function startTimer(minutes) {
    try {
      await api("/api/reminders/timers", { method: "POST", json: { seconds: minutes * 60, label: `${minutes} min timer` } });
      ui.haptic("success");
      await loadTimers();
    } catch (error) { ui.toast(message(error), { kind: "bad" }); }
  }

  async function cancelTimer(t) {
    try {
      await api(`/api/reminders/timers/${encodeURIComponent(t.id)}`, { method: "DELETE" });
      await loadTimers();
    } catch (error) { ui.toast(message(error), { kind: "bad" }); }
  }

  function renderTimers() {
    timerRings.clear();
    const presets = el("div", { class: "ui-hscroll rm-presets", role: "group", "aria-label": "Start a timer" },
      ...PRESETS.map((p) => el("button", { type: "button", class: "ui-chip", onclick: () => startTimer(p.min) }, ui.icon("timer"), p.label)));
    const running = timers.filter((t) => t.state !== "finished" || remainingOf(t) <= 0);
    const cards = running.map((t) => {
      const total = Math.max(1, t.seconds);
      const done = t.state === "finished" || remainingOf(t) <= 0;
      const ring = ui.ringProgress({ value: done ? 1 : remainingOf(t) / total, size: 72, stroke: 7, tone: done ? "good" : "accent",
        big: done ? "✓" : clock(remainingOf(t)), label: `${t.label}, ${done ? "finished" : "running"}` });
      ring.classList.add("rm-ring");
      timerRings.set(t.id, { ring, total, t });
      return el("div", { class: `ui-card rm-timer${done ? " is-done" : ""}` },
        ring,
        el("div", { class: "rm-timer-body" },
          el("strong", {}, t.label || "Timer"),
          el("span", {}, done ? "Finished" : `of ${clock(total)}`)),
        ui.button({ label: done ? "Dismiss timer" : "Cancel timer", icon: done ? "check" : "close", variant: "ghost", iconOnly: true, onClick: () => cancelTimer(t) }));
    });
    fill(timersBox, 
      el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, "Timers")),
      presets,
      cards.length ? el("div", { class: "rm-timer-list" }, ...cards) : null);
    startTick();
  }

  function startTick() {
    clearInterval(tick);
    if (!timerRings.size) return;
    tick = setInterval(() => {
      let anyFinished = false;
      for (const [id, item] of timerRings) {
        const left = remainingOf(item.t);
        if (left <= 0 && item.t.state !== "finished") { item.t.state = "finished"; anyFinished = true; continue; }
        if (item.t.state === "finished") continue;
        item.ring.set(left / item.total, clock(left));
      }
      if (anyFinished) { ui.haptic("success"); renderTimers(); }
    }, 1000);
  }

  async function loadTimers() {
    try {
      const data = await api("/api/reminders/timers");
      timers = data.timers || [];
    } catch (_) { timers = []; }
    renderTimers();
  }

  // ---- reminders list -------------------------------------------------------------------------
  const listBox = el("div", { class: "rm-groups" });

  function groupOf(r) {
    if (r.status === "done" || r.done) return "done";
    const now = new Date();
    const next = new Date(r.snoozed_until || r.next_fire || r.due);
    if (r.status === "fired" || next < now) return "overdue";
    const endToday = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    return next < endToday ? "today" : "upcoming";
  }

  function row(r, group) {
    const check = el("button", { type: "button", class: `rm-check${group === "done" ? " is-done" : ""}`, "aria-label": group === "done" ? "Done" : `Mark done: ${r.text}`,
      disabled: group === "done" ? true : null, onclick: (e) => { e.stopPropagation(); markDone(r, check); } }, ui.icon("check"));
    const rep = repeatLabel(r);
    const badges = el("span", { class: "rm-badges" },
      r.status === "snoozed" ? ui.badge("Snoozed", "accent") : null,
      rep ? el("span", { class: "ui-badge" }, ui.icon("refresh"), rep) : null);
    const snooze = group === "done" ? null
      : ui.button({ label: `Snooze ${r.text}`, icon: "clock", variant: "ghost", iconOnly: true, onClick: (e) => { e.stopPropagation(); openSnooze(r); } });
    return el("li", { class: `rm-row${group === "overdue" ? " is-overdue" : ""}${group === "done" ? " is-done" : ""}` },
      check,
      el("button", { type: "button", class: "rm-main", onclick: () => openEdit(r) },
        el("span", { class: "rm-text" }, r.text),
        el("span", { class: "rm-when" }, r.when || "", badges)),
      snooze);
  }

  function renderList() {
    if (!reminders.length) {
      fill(listBox, ui.emptyState({ icon: "alarm", title: "No reminders yet", body: "Type one above, like \"remind me to stretch at 3pm\".", compact: true }));
      return;
    }
    const groups = { overdue: [], today: [], upcoming: [], done: [] };
    for (const r of reminders) groups[groupOf(r)].push(r);
    const titles = { overdue: "Overdue", today: "Today", upcoming: "Upcoming", done: "Done" };
    const nodes = [];
    for (const key of ["overdue", "today", "upcoming", "done"]) {
      if (!groups[key].length) continue;
      nodes.push(el("section", { class: `rm-group rm-group--${key}`, "aria-label": titles[key] },
        el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, titles[key]),
          el("span", { class: "rm-count ui-num" }, String(groups[key].length))),
        el("ul", { class: "ui-list ui-list--inset rm-list" }, groups[key].map((r) => row(r, key)))));
    }
    if (!groups.overdue.length && !groups.today.length && !groups.upcoming.length) {
      nodes.unshift(ui.emptyState({ icon: "check-circle", title: "All clear", body: "Nothing coming up. Add a reminder above.", compact: true }));
    }
    fill(listBox, ...nodes);
  }

  async function load(force) {
    if (!force && !reminders.length) fill(listBox, ui.skeleton("list", 4));
    try {
      const data = await api("/api/reminders/?status=all");
      reminders = data.reminders || [];
      loadedAt = Date.now();
      renderList();
      ctx.setBadge?.("reminders", reminders.filter((r) => groupOf(r) === "overdue").length);
    } catch (error) {
      fill(listBox, el("div", { class: "rm-error", role: "alert" }, ui.notice(message(error), { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", size: "sm", onClick: () => load() })));
    }
  }

  async function loadAll(force) { await Promise.all([load(force), loadTimers()]); }

  async function markDone(r, check) {
    check.classList.add("is-done");
    try {
      await api(`/api/reminders/${encodeURIComponent(r.id)}/done`, { method: "POST" });
      ui.haptic("success");
      ui.toast(r.repeat && r.repeat !== "none" ? "Done. Next one is scheduled." : "Done", { kind: "good" });
      setTimeout(() => load(true), 250);
    } catch (error) {
      check.classList.remove("is-done");
      ui.toast(message(error), { kind: "bad" });
    }
  }

  function minutesUntilTomorrow() {
    const t = new Date(); t.setDate(t.getDate() + 1); t.setHours(9, 0, 0, 0);
    return Math.max(1, Math.round((t - Date.now()) / 60000));
  }

  async function snooze(r, minutes, label) {
    try {
      await api(`/api/reminders/${encodeURIComponent(r.id)}/snooze`, { method: "POST", json: { minutes } });
      ui.toast(`Snoozed until ${label}`, { kind: "good", icon: "clock" });
      load(true);
    } catch (error) { ui.toast(message(error), { kind: "bad" }); }
  }

  function openSnooze(r) {
    const opts = [["10 minutes", "In 10 minutes", 10], ["1 hour", "In 1 hour", 60], ["tomorrow 9:00", "Tomorrow at 9:00", minutesUntilTomorrow()]];
    const s = ui.sheet({ title: "Snooze", content: el("div", { class: "ui-list ui-list--inset" },
      ...opts.map(([short, long, min]) => el("button", { type: "button", class: "ui-row rm-snooze-opt", onclick: () => { s.close(); snooze(r, min, short); } },
        ui.tile("clock", "accent", "sm"), el("span", { class: "grow" }, long), ui.icon("chevron-right")))) });
    s.body.prepend(el("p", { class: "rm-snooze-for" }, r.text));
  }

  function openEdit(r) {
    const text = el("input", { type: "text", value: r.text, maxlength: "300", "aria-label": "Reminder text" });
    const due = el("input", { type: "datetime-local", value: toLocalInput(r.due), "aria-label": "Date and time" });
    const initialDue = due.value;
    const repeat = el("select", { "aria-label": "Repeat" }, ...REPEATS.map(([v, l]) => el("option", { value: v }, l)));
    repeat.value = r.repeat || "none";
    const n = el("input", { type: "number", min: "1", max: "10080", value: String(r.n || 60), inputmode: "numeric", "aria-label": "Minutes between repeats" });
    const nField = el("label", { class: "ui-field", hidden: repeat.value !== "every_n_minutes" }, el("span", { class: "ui-label" }, "Every how many minutes"), n);
    repeat.addEventListener("change", () => { nField.hidden = repeat.value !== "every_n_minutes"; });
    const err = el("p", { class: "rm-err", role: "alert", hidden: true });
    const body = el("div", { class: "rm-edit" },
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Reminder"), text),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "When"), due),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Repeat"), repeat),
      nField, err,
      ui.button({ label: "Delete reminder", icon: "trash", variant: "danger-soft", block: true, onClick: async () => {
        const ok = await ui.confirm({ title: "Delete this reminder?", message: `"${r.text}" will be removed.`, confirmLabel: "Delete", danger: true });
        if (!ok) return;
        try { await api(`/api/reminders/${encodeURIComponent(r.id)}`, { method: "DELETE" }); sheet.close(); ui.toast("Deleted", { kind: "good" }); load(true); }
        catch (error) { err.textContent = message(error); err.hidden = false; }
      } }));
    const sheet = ui.sheet({ title: "Edit reminder", content: body, actions: [
      { label: "Save", icon: "check", variant: "primary", onClick: async () => {
        const patch = {};
        const t = text.value.trim();
        if (!t) { err.textContent = "Write what to be reminded about."; err.hidden = false; return false; }
        if (t !== r.text) patch.text = t;
        if (due.value && due.value !== initialDue) patch.due = due.value;
        if (repeat.value !== (r.repeat || "none")) patch.repeat = repeat.value;
        if (repeat.value === "every_n_minutes" && Number(n.value) !== r.n) { patch.n = Number(n.value); patch.repeat = repeat.value; }
        if (!Object.keys(patch).length) return true;
        try {
          await api(`/api/reminders/${encodeURIComponent(r.id)}`, { method: "PATCH", json: patch });
          ui.toast("Saved", { kind: "good" });
          load(true);
          return true;
        } catch (error) { err.textContent = message(error); err.hidden = false; return false; }
      } }] });
  }

  // ---- shell ----------------------------------------------------------------------------------
  const root = el("div", { class: "rm" }, quick, timersBox, listBox);
  container.append(root);
  ui.pullToRefresh(container, async () => { await loadAll(true); });

  return {
    show() { visible = true; if (!loadedAt || Date.now() - loadedAt > 20000) loadAll(); else startTick(); },
    hide() { visible = false; clearInterval(tick); },
    openSection(name) {
      if (name === "timers") timersBox.scrollIntoView?.({ block: "start", behavior: "smooth" });
      else if (name === "new") input.focus();
    },
    openItem(id) {
      const find = () => reminders.find((r) => r.id === id);
      if (find()) return openEdit(find());
      load(true).then(() => { const r = find(); if (r) openEdit(r); else ui.toast("That reminder is gone.", { kind: "warn" }); });
    },
    refresh() { loadAll(true); },
  };
}
