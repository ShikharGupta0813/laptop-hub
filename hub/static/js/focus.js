// Focus: a pomodoro-style timer. The countdown is computed from the server's start time (GET /api/focus/current)
// so it survives a reload; the server clock offset is measured from `now_ts`. Breaks are a local countdown
// (stored in localStorage) so they never pollute the stats. API: docs/api/productivity.md (Focus).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const PRESETS = [15, 25, 45, 50];
const BREAK_KEY = "hub.focus.break";
const pad = (n) => String(n).padStart(2, "0");
const mmss = (sec) => { const s = Math.max(0, Math.ceil(sec)); return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`; };
const dayShort = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-GB", { weekday: "narrow" });
function readBreak() { try { const v = JSON.parse(localStorage.getItem(BREAK_KEY) || "null"); return v && v.end > Date.now() ? v : null; } catch (_) { return null; } }
function writeBreak(v) { try { if (v) localStorage.setItem(BREAK_KEY, JSON.stringify(v)); else localStorage.removeItem(BREAK_KEY); } catch (_) { /* storage blocked */ } }

export function init(container, ctx) {
  if (!document.querySelector('link[data-css="focus"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/focus.css", "data-css": "focus" }));
  }
  const state = {
    current: null, offset: 0, loaded: false, error: "", stats: null, history: null, tasks: [],
    minutes: 25, breakMin: 5, label: "", taskId: "", tick: null, brk: readBreak(), finished: null, announced: null, visible: false, seq: 0,
  };
  const root = el("div", { class: "fc" });
  container.replaceChildren(root);

  const ring = ui.ringProgress({ value: 0, size: 232, stroke: 13, tone: "accent", big: "25:00", small: "ready", label: "Focus timer" });
  ring.classList.add("fc-ring");
  const bigNode = ring.querySelector(".ui-ring__big");
  const smallNode = ring.querySelector(".ui-ring__small");
  const stage = el("section", { class: "ui-card fc-stage" });
  const lower = el("div", { class: "fc-lower" });
  root.append(stage, lower);

  const serverNow = () => (Date.now() + state.offset) / 1000;

  // ---------- data ----------
  async function loadCurrent() {
    const seq = ++state.seq;
    try {
      const c = await api("/api/focus/current");
      if (seq !== state.seq) return;
      state.offset = c.now_ts * 1000 - Date.now();
      state.current = c; state.error = ""; state.loaded = true;
      if (c.just_finished && state.announced !== c.just_finished.id) { state.finished = c.just_finished; state.announced = c.just_finished.id; }
    } catch (e) { if (seq !== state.seq) return; state.error = e.message; state.loaded = true; }
    renderStage();
    syncTick();
  }
  async function loadLower() {
    const [s, h, t] = await Promise.allSettled([api("/api/focus/stats?days=14"), api("/api/focus/history?limit=12"), api("/api/todos?done=false")]);
    state.stats = s.status === "fulfilled" ? s.value : null;
    state.history = h.status === "fulfilled" ? h.value.sessions : null;
    state.statsError = s.status === "rejected" ? s.reason.message : "";
    state.tasks = t.status === "fulfilled" ? t.value.tasks : [];
    renderLower();
    if (state.visible && !state.current?.running) renderStage();
  }
  const reload = () => Promise.all([loadCurrent(), loadLower()]);

  // ---------- ticking ----------
  function syncTick() {
    clearInterval(state.tick); state.tick = null;
    const running = state.current?.running;
    if (!state.visible || !(running || state.brk)) return;
    state.tick = setInterval(tick, 250);
    tick();
  }
  function tick() {
    if (state.brk) {
      const left = (state.brk.end - Date.now()) / 1000;
      if (left <= 0) { state.brk = null; writeBreak(null); ui.haptic("success"); ctx.toast("Break over. Ready for another round?", { kind: "good", icon: "timer" }); renderStage(); syncTick(); return; }
      bigNode.textContent = mmss(left);
      ring.set(1 - left / state.brk.total);
      return;
    }
    const s = state.current?.session;
    if (!state.current?.running || !s) return;
    const left = s.ends_ts - serverNow();
    const total = s.planned_minutes * 60;
    if (left <= 0) {
      clearInterval(state.tick); state.tick = null;
      bigNode.textContent = "00:00"; ring.set(1); smallNode.textContent = "wrapping up";
      ui.haptic("success");
      setTimeout(async () => { await reload(); }, 1200);
      return;
    }
    bigNode.textContent = mmss(left);
    ring.set(1 - left / total);
  }

  // ---------- stage ----------
  function setRing(tone, big, small, value) {
    ring.setAttribute("data-tone", tone);
    ring.querySelector(".ui-ring__value").style.stroke = "var(--_tone)";
    bigNode.textContent = big; smallNode.textContent = small; ring.set(value);
  }

  function renderStage() {
    stage.replaceChildren();
    stage.classList.remove("is-running", "is-break", "is-done");
    if (!state.loaded && !state.error) { stage.append(ui.skeleton("card", 1)); return; }
    if (state.error) {
      stage.append(ui.notice(state.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: loadCurrent }));
      return;
    }
    const cur = state.current;
    if (cur.running && cur.session) { renderRunning(cur.session); return; }
    if (state.brk) { renderBreak(); return; }
    if (state.finished) { renderFinished(state.finished); return; }
    renderSetup();
  }

  function renderRunning(s) {
    stage.classList.add("is-running");
    setRing("accent", mmss(s.ends_ts - serverNow()), "focusing", 1 - (s.ends_ts - serverNow()) / (s.planned_minutes * 60));
    const stop = ui.button({ label: "Stop session", icon: "stop", variant: "secondary", block: true, onClick: async () => {
      const elapsed = Math.round((serverNow() - s.started_ts) / 60);
      if (!await ui.confirm({ title: "Stop this session?", message: `You've focused for ${elapsed} min. That time is still recorded.`, confirmLabel: "Stop session" })) return;
      try { await ui.busy(stop, api("/api/focus/stop", { method: "POST", json: {} })); state.finished = null; ctx.toast("Session stopped", { kind: "good" }); await reload(); }
      catch (e) { ctx.toast(e.message, { kind: "bad" }); }
    } });
    stage.append(ring, el("div", { class: "fc-what" }, el("h2", { class: "fc-what__t" }, s.label || "Focus session"),
      el("p", { class: "fc-what__s" }, `${s.planned_minutes} minute session`, s.task_id ? " · linked to a task" : "")), stop);
  }

  function renderBreak() {
    stage.classList.add("is-break");
    setRing("good", mmss((state.brk.end - Date.now()) / 1000), "break", 1 - (state.brk.end - Date.now()) / 1000 / state.brk.total);
    stage.append(ring, el("div", { class: "fc-what" }, el("h2", { class: "fc-what__t" }, "Take a break"), el("p", { class: "fc-what__s" }, "Stretch, drink some water, look away from the screen.")),
      ui.button({ label: "Skip break", variant: "secondary", block: true, onClick: () => { state.brk = null; writeBreak(null); syncTick(); renderStage(); } }));
  }

  function renderFinished(s) {
    stage.classList.add("is-done");
    setRing("good", "Done", "well focused", 1);
    const brk = s.break_minutes ?? state.breakMin;
    stage.append(ring, el("div", { class: "fc-what" }, el("h2", { class: "fc-what__t" }, "Session complete"),
      el("p", { class: "fc-what__s" }, `${s.label ? s.label + " · " : ""}${s.actual_minutes ?? s.planned_minutes} minutes of focus`)),
    el("div", { class: "fc-actions" },
      brk > 0 ? ui.button({ label: `Start ${brk}-min break`, icon: "coffee", variant: "primary", block: true, onClick: () => { state.brk = { end: Date.now() + brk * 60000, total: brk * 60 }; writeBreak(state.brk); state.finished = null; renderStage(); syncTick(); } }) : null,
      ui.button({ label: "Another round", icon: "refresh", variant: brk > 0 ? "secondary" : "primary", block: true, onClick: () => { state.finished = null; if (s.label) state.label = s.label; renderStage(); } })));
  }

  function renderSetup() {
    setRing("accent", mmss(state.minutes * 60), "ready", 0);
    const presets = el("div", { class: "fc-presets", role: "radiogroup", "aria-label": "Session length" },
      ...PRESETS.map((m) => el("button", { type: "button", class: "ui-chip fc-preset", role: "radio", "aria-checked": String(state.minutes === m), "aria-pressed": String(state.minutes === m),
        onclick: () => { state.minutes = m; ui.haptic("select"); renderSetup2(); } }, `${m} min`)));
    const stepper = el("div", { class: "fc-stepper" },
      ui.button({ iconOnly: true, icon: "minus", variant: "tonal", label: "5 minutes shorter", onClick: () => { state.minutes = Math.max(5, state.minutes - 5); renderSetup2(); } }),
      el("span", { class: "fc-stepper__v ui-num" }, `${state.minutes} min`),
      ui.button({ iconOnly: true, icon: "plus", variant: "tonal", label: "5 minutes longer", onClick: () => { state.minutes = Math.min(240, state.minutes + 5); renderSetup2(); } }));
    const label = el("input", { type: "text", value: state.label, maxlength: "80", placeholder: "What are you working on?", "aria-label": "Session label", autocomplete: "off" });
    label.addEventListener("input", () => { state.label = label.value; });
    const task = el("select", { "aria-label": "Link a task" }, el("option", { value: "" }, "No linked task"),
      ...state.tasks.map((t) => el("option", { value: t.id }, t.title)));
    task.value = state.taskId;
    task.addEventListener("change", () => { state.taskId = task.value; const t = state.tasks.find((x) => x.id === task.value); if (t && !label.value) { label.value = t.title; state.label = t.title; } });
    const brk = el("select", { "aria-label": "Break length" }, ...[0, 5, 10, 15].map((m) => el("option", { value: String(m) }, m ? `${m}m break` : "No break")));
    brk.value = String(state.breakMin);
    brk.addEventListener("change", () => { state.breakMin = Number(brk.value); });
    const start = ui.button({ label: "Start focus", icon: "play", variant: "primary", size: "lg", block: true, onClick: async () => {
      const json = { minutes: state.minutes, break_minutes: state.breakMin };
      if (label.value.trim()) json.label = label.value.trim();
      if (state.taskId) json.task_id = state.taskId;
      try { await ui.busy(start, api("/api/focus/start", { method: "POST", json })); ui.haptic("medium"); state.finished = null; await reload(); }
      catch (e) { ctx.toast(e.message, { kind: "bad" }); if (/already running/i.test(e.message)) reload(); }
    } });
    stage.replaceChildren(ring, presets, stepper,
      el("div", { class: "fc-form" }, label, el("div", { class: "fc-two" }, task, brk)), start);
  }
  const renderSetup2 = () => { renderSetup(); };

  // ---------- stats + history ----------
  function renderLower() {
    lower.replaceChildren();
    const s = state.stats;
    if (s) {
      const stat = (iconName, tone, v, l) => el("div", { class: "fc-stat" }, ui.tile(iconName, tone, "sm"), el("b", { class: "ui-num" }, v), el("span", {}, l));
      const days = s.by_day || [];
      const max = Math.max(30, ...days.map((d) => d.minutes));
      const bars = el("div", { class: "fc-bars", role: "img", "aria-label": `Focus minutes for the last ${days.length} days` },
        ...days.map((d, i) => {
          const bar = el("i", { class: "fc-bar" + (d.minutes ? "" : " is-zero") + (i === days.length - 1 ? " is-today" : "") });
          bar.style.setProperty("--h", String(d.minutes / max));
          return el("div", { class: "fc-bar__col", title: `${d.date}: ${d.minutes} min` }, el("span", { class: "fc-bar__track" }, bar), el("span", { class: "fc-bar__d" }, dayShort(d.date)));
        }));
      lower.append(el("section", { class: "ui-card fc-statscard" },
        el("div", { class: "fc-stats" },
          stat("timer", "accent", `${s.today_minutes}m`, "today"),
          stat("calendar", "teal", `${s.week_minutes}m`, "last 7 days"),
          stat("flame", "orange", String(s.streak_days), "day streak")),
        bars,
        s.by_label?.length ? el("div", { class: "fc-labels" }, ...s.by_label.slice(0, 4).map((l) => el("span", { class: "ui-chip ui-chip--static" }, `${l.label} · ${l.minutes}m`))) : null));
    } else if (state.statsError) lower.append(ui.notice(state.statsError, { tone: "bad" }));
    else lower.append(ui.skeleton("card", 1));

    lower.append(el("div", { class: "ui-section-head fc-head" }, el("h2", { class: "ui-section-title" }, "History")));
    if (state.history == null) { lower.append(ui.skeleton("list", 3)); return; }
    if (!state.history.length) { lower.append(ui.emptyState({ icon: "target", title: "No sessions yet", body: "Start your first focus session above. Finished sessions show up here.", compact: true })); return; }
    lower.append(el("div", { class: "ui-list ui-list--inset ui-list--icons" }, ...state.history.map((h) => {
      const done = h.state === "completed";
      return ui.listRow({ icon: done ? "check-circle" : h.state === "running" ? "timer" : "stop", tone: done ? "good" : h.state === "running" ? "accent" : "gray",
        title: h.label || "Focus session", sub: `${ui.relTime(h.started_at)} · ${h.state === "running" ? "in progress" : done ? "completed" : "stopped early"}`,
        meta: `${h.actual_minutes ?? h.planned_minutes}m` });
    })));
  }

  ui.pullToRefresh(container, reload);
  renderStage();
  renderLower();
  return {
    show() { state.visible = true; reload(); },
    hide() { state.visible = false; clearInterval(state.tick); state.tick = null; },
    refresh: reload,
    openSection() { /* "start": the setup card is the first thing on screen */ container.scrollTo?.({ top: 0 }); },
  };
}
