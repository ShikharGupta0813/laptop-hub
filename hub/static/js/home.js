// Home: the landing dashboard. Greeting, laptop status, quick actions, today's agenda/tasks/reminders,
// streak rings, continue learning, notifications and recent notes.
//
// Data: GET /api/home -> {agenda, tasks, reminders, notifications, streaks, laptop, learning?, notes?}.
// Every key is optional. When /api/home is missing or a key is absent, the card falls back to the older
// routes (/api/status, /api/work/..., /api/learn/history, /api/english/overview, /api/notes), and every
// card goes skeleton -> content | friendly empty state on its own, so one failure never blanks the page.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

// ---- quick actions (also listed in the search overlay) -------------------------------------------
// open: [view, section] opened with ctx.open; run(ctx): an action. Add one by adding a line.
export const QUICK_ACTIONS = [
  { id: "talk", label: "Ask Talk", icon: "prompt", tone: "accent", hint: "Talk to your assistant", keywords: "assistant ai chat voice", open: ["assistant"] },
  { id: "note", label: "New note", icon: "note-plus", tone: "orange", hint: "Write a quick note", keywords: "write memo jot", open: ["notes", "new"] },
  { id: "voice", label: "Voice note", icon: "mic", tone: "pink", hint: "Say it, it's written down", keywords: "record dictate audio", open: ["notes", "voice"] },
  { id: "lock", label: "Lock laptop", icon: "lock", tone: "info", hint: "Lock the laptop screen now", keywords: "lock screen secure away", run: (ctx) => lockLaptop(ctx) },
  { id: "screenshot", label: "Screenshot", icon: "screenshot", tone: "teal", hint: "See the laptop screen", keywords: "screen capture picture", run: (ctx) => screenshot(ctx) },
  { id: "jarvis", label: "Start Jarvis", icon: "jarvis", tone: "violet", hint: "Wake your voice assistant", keywords: "hey jarvis voice assistant", run: (ctx) => startJarvis(ctx) },
  { id: "focus", label: "Start focus", icon: "target", tone: "good", hint: "A distraction-free timer", keywords: "pomodoro timer deep work", open: ["focus", "start"] },
  { id: "terminal", label: "Terminal", icon: "terminal", tone: "gray", hint: "A shell on the laptop", keywords: "shell powershell bash command", open: ["terminal"] },
  { id: "screen", label: "Remote screen", icon: "cast", tone: "warn", hint: "View and control the laptop", keywords: "remote desktop view control", open: ["screen"] },
];

export function runQuickAction(action, ctx) {
  ui.haptic("light");
  if (action.run) return action.run(ctx);
  const [view, section] = action.open;
  return ctx.open(view, section);
}

async function laptopRun(action, params = {}) {
  try {
    return await api("/api/laptop/run", { method: "POST", json: { action, params } });
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      const ok = await ui.confirm({ title: "Are you sure?", message: error.message, confirmLabel: "Do it", danger: true });
      if (!ok) return null;
      return api("/api/laptop/run", { method: "POST", json: { action, params, confirm: true } });
    }
    throw error;
  }
}

async function lockLaptop() {
  const t = ui.toast("Locking your laptop…", { icon: "lock", ms: 8000 });
  try {
    const result = await laptopRun("lock_screen");
    t.close();
    if (result) ui.toast(result.message || "Laptop locked", { kind: "good", icon: "lock" });
  } catch (error) { t.close(); ui.toast(error.message, { kind: "bad" }); }
}

async function screenshot() {
  const s = ui.sheet({ title: "Laptop screen", content: ui.skeleton("card", 1) });
  try {
    const result = await laptopRun("screenshot");
    if (!result) { s.close(); return; }
    if (result.kind === "image" && result.image_b64) {
      const src = `data:${result.mime || "image/png"};base64,${result.image_b64}`;
      s.body.replaceChildren(el("img", { class: "hm-shot", src, alt: "Screenshot of the laptop screen" }),
        el("p", { class: "ui-hint hm-shot-meta" }, `Taken ${ui.clockTime(new Date())}`));
      s.setActions([
        { label: "Retake", icon: "refresh", variant: "secondary", onClick: () => { s.close(); screenshot(); } },
        { label: "Save", icon: "download", variant: "primary", onClick: () => {
          const a = el("a", { href: src, download: `laptop-${Date.now()}.png` }); document.body.append(a); a.click(); a.remove(); return false;
        } },
      ]);
    } else {
      s.body.replaceChildren(ui.emptyState({ icon: "image", title: "No picture came back", body: result.message || "The laptop didn't send an image." }));
    }
  } catch (error) {
    s.body.replaceChildren(ui.emptyState({ icon: "wifi-off", title: "Couldn't take a screenshot", body: error.message }));
  }
}

async function startJarvis() {
  try {
    const status = await api("/api/jarvis/start", { method: "POST" });
    if (status?.running) ui.toast("Jarvis is listening on your laptop", { kind: "good", icon: "jarvis" });
    else ui.toast(status?.reason || "Jarvis didn't start. Open Apps → Jarvis for details.", { kind: "warn" });
  } catch (error) { ui.toast(error.message, { kind: "bad" }); }
}

// ---- helpers ---------------------------------------------------------------------------------------
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
function greeting(d = new Date()) {
  const h = d.getHours();
  if (h < 5) return "Good night";
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}
const arr = (v) => (Array.isArray(v) ? v : []);
const text = (...vals) => vals.find((v) => typeof v === "string" && v.trim()) || "";
const softFail = (p) => p.then((v) => ({ ok: true, v }), (e) => ({ ok: false, e }));

function dueLabel(day) {
  if (!day) return "";
  const d = new Date(String(day).length === 10 ? day + "T00:00:00" : day);
  if (Number.isNaN(d.getTime())) return String(day);
  const diff = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
  if (diff === 0) return "Due today";
  if (diff === 1) return "Due tomorrow";
  if (diff === -1) return "Due yesterday";
  if (diff < 0) return `${-diff} days late`;
  return `Due ${d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}`;
}

// A numbered section the portfolio way: `02 / TODAY ──── WORK ›` over a terminal window titled `~/today`.
function card(title, { n, path, action, cls = "" } = {}) {
  const body = el("div", { class: "hm-card-body ui-window__body" }, ui.skeleton("list", 2));
  const head = el("div", { class: "hm-head" },
    el("h2", { class: "ui-eyebrow hm-eyebrow" }, n ? el("b", {}, n) : null, title),
    el("span", { class: "hm-head-rule", "aria-hidden": "true" }),
    action ? el("button", { type: "button", class: "ui-link", onclick: action.onClick }, action.label, ui.icon("chevron-right")) : null);
  const win = el("div", { class: "ui-window hm-card" },
    el("div", { class: "ui-window__bar", "aria-hidden": "true" }, el("span", { class: "ui-window__dots" }),
      el("span", { class: "ui-window__title" }, path || `~/${title.toLowerCase().replace(/\s+/g, "-")}`)), body);
  const node = el("section", { class: `hm-sec ${cls}`.trim(), "aria-label": title }, head, win);
  return { node, head, body };
}
// `key: value` row for the hero; a row with onClick is a button (44px)
function kvRow(k, v, { onClick, label, tone } = {}) {
  const parts = [el("span", { class: "ui-kv__k" }, k), el("span", { class: "ui-kv__v", ...(tone ? { "data-tone": tone } : {}) }, v)];
  if (!onClick) return el("div", { class: "ui-kv__row hm-kv" }, ...parts);
  return el("button", { type: "button", class: "ui-kv__row hm-kv hm-kv--btn", onclick: onClick, "aria-label": label || k }, ...parts, ui.icon("chevron-right"));
}

// ---- the screen ------------------------------------------------------------------------------------
export function init(container, ctx) {
  const root = el("div", { class: "hm" });
  const openTarget = (target, fallback) => {
    const t = target && target.view ? target : fallback;
    return ctx.open(t.view, t.section || undefined, { id: t.id });
  };
  container.append(root);
  let loadedAt = 0;
  let userName = "";
  let loading = null;

  // hero: a terminal window running `$ whoami`, like the portfolio's landing
  const hello = el("h1", { class: "hm-hello" });
  const date = el("span", {});
  const status = el("div", { class: "ui-kv hm-status", "aria-live": "polite" }, kvRow("laptop", el("span", { class: "hm-led-v" }, el("span", { class: "ui-led" }), "checking…")));
  const hero = el("header", { class: "ui-window ui-window--accent hm-hero" },
    el("div", { class: "ui-window__bar", "aria-hidden": "true" }, el("span", { class: "ui-window__dots" }),
      el("span", { class: "ui-window__title" }, "~/home — zsh"), el("span", { class: "ui-window__meta" }, "tty/01")),
    el("div", { class: "ui-window__body hm-hero-body" },
      el("p", { class: "ui-prompt hm-cmd", "aria-hidden": "true" }, "whoami"),
      hello,
      el("p", { class: "ui-tag hm-date" }, el("b", {}, "date"), date),
      status,
      el("hr", { class: "ui-dash" }),
      el("p", { class: "ui-tagline hm-tagline" }, "build. debug. optimize. ship.")));
  function paintClock() {
    const now = new Date();
    hello.replaceChildren(el("span", { class: "hm-hello-greet" }, greeting(now) + (userName ? "," : "")),
      userName ? " " : "", userName ? el("span", { class: "hm-name ui-glitch" }, userName) : "", el("span", { class: "ui-cursor", "aria-hidden": "true" }));
    date.textContent = now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }).toLowerCase();
  }

  // jump to anything
  const jump = el("button", { type: "button", class: "hm-jump", onclick: () => ctx.search() },
    el("span", { class: "hm-jump-sym", "aria-hidden": "true" }, "$"), el("span", { class: "grow" }, "jump to anything"), el("kbd", { class: "hm-kbd" }, "/"));

  // quick actions
  const quick = el("div", { class: "ui-hscroll hm-quick", role: "list", "aria-label": "Quick actions" },
    QUICK_ACTIONS.map((a) => el("button", { type: "button", class: "hm-qa", role: "listitem", onclick: () => runQuickAction(a, ctx) },
      ui.tile(a.icon, a.tone, "lg"), el("span", { class: "hm-qa-label" }, a.label))));

  const todayCard = card("Today", { n: "01", action: { label: "Whole day", onClick: () => ctx.open("today") } });
  const ringsCard = card("Streaks", { n: "03", cls: "hm-rings-card" });
  const learnCard = el("div", { class: "ui-window__body hm-learn" }, ui.skeleton("lines", 3));
  const learnSec = el("section", { class: "hm-sec", "aria-label": "Continue learning" },
    el("div", { class: "hm-head" }, el("h2", { class: "ui-eyebrow hm-eyebrow" }, el("b", {}, "04"), "Learn"), el("span", { class: "hm-head-rule", "aria-hidden": "true" })),
    el("div", { class: "ui-window ui-window--accent hm-card" },
      el("div", { class: "ui-window__bar", "aria-hidden": "true" }, el("span", { class: "ui-window__dots" }), el("span", { class: "ui-window__title" }, "~/learn")), learnCard));
  const forYou = el("section", { class: "hm-foryou", "aria-label": "Suggestions", hidden: true });
  let countdown = null;
  function renderSuggestions(list, summaries) {
    const c = summaries?.life?.next_countdown;
    countdown = c && c.title && c.days_remaining != null
      ? kvRow("next", c.days_remaining === 0 ? `${c.title} is today` : `${c.title} in ${c.days_remaining} ${c.days_remaining === 1 ? "day" : "days"}`, { tone: "warn" })
      : null;
    if (countdown && status.querySelector(".ui-led")) status.append(countdown);
    const items = list.filter((x) => typeof x === "string" && x.trim()).slice(0, 3);
    forYou.hidden = !items.length;
    forYou.replaceChildren(el("p", { class: "ui-tag hm-foryou-head" }, el("b", {}, "tag"), el("span", {}, "for-you")),
      ...items.map((t) => el("p", { class: "hm-foryou-item" }, t)));
    cardsDue = Number(summaries?.cards?.due_today) || 0;
  }
  let cardsDue = 0;
  const notesCard = card("Notes", { n: "05", path: "~/notes/recent", action: { label: "See all", onClick: () => ctx.open("notes") } });
  const notifCard = card("Inbox", { n: "06", path: "~/notifications", action: { label: "All", onClick: () => ctx.open("notifications") } });
  notifCard.node.hidden = true;

  // Friday card: only shown when her module exists on the laptop (settings answer 404 otherwise).
  const fridayCard = el("section", { class: "ui-window hm-friday", hidden: true, "aria-label": "Friday" },
    el("div", { class: "ui-window__bar", "aria-hidden": "true" }, el("span", { class: "ui-window__dots" }),
      el("span", { class: "ui-window__title" }, "~/friday — brief"), el("span", { class: "ui-window__meta" }, el("span", { class: "ui-led ui-led--accent ui-led--live" }), "ready")),
    el("div", { class: "ui-window__body hm-friday-body" },
      el("p", { class: "hm-friday-hello" }), el("p", { class: "hm-friday-sub" }, "Your day in one minute. I will keep it short."),
      el("div", { class: "hm-friday-btns" },
        ui.button({ variant: "primary", icon: "prompt", label: "Brief me", onClick: () => ctx.open("friday", "brief") }),
        ui.button({ variant: "secondary", iconOnly: true, icon: "mic", "aria-label": "Talk to Friday", onClick: () => ctx.open("assistant", "friday") }))));
  import("./friday.js").then(async (m) => {
    try { await m.loadSettings(ctx); } catch (_) { return; }
    m.startAlerts(ctx);
    if (m.FR.available !== true) return;
    const f = fridayCard.querySelector(".hm-friday-hello");
    const paint = () => {
      const p = m.daypart(), who = userName || m.addressAs();
      f.textContent = (p === "night" ? "Still up" : `Good ${p}`) + (who ? `, ${who}` : "");
    };
    paint(); fridayCard.hidden = false; setInterval(paint, 60000);
  }).catch(() => { /* Friday is optional */ });

  // "Finish setting up" card: only while the essentials aren't green or under half the optional items are done.
  const setupCard = el("section", { class: "hm-setup", hidden: true, "aria-label": "Finish setting up" });
  let setupChecked = 0;
  function checkSetup() {
    let dismissed = false;
    try { dismissed = localStorage.getItem("hub.setup.dismissed") === "1"; } catch (_) { /* private mode */ }
    if (dismissed || Date.now() - setupChecked < 120000) return;
    setupChecked = Date.now();
    import("./setup.js").then((m) => m.runChecks(ctx, true).then((rows) => {
      const s = m.summarize(rows);
      if (!s.needs) { setupCard.hidden = true; return; }
      setupCard.replaceChildren(
        el("button", { type: "button", class: "hm-setup-go", onclick: () => ctx.open("setup") },
          ui.ringProgress({ value: s.total ? s.ready / s.total : 0, size: 48, stroke: 5, tone: s.requiredOk ? "accent" : "warn", big: String(s.ready), label: "Setup progress" }),
          el("span", { class: "hm-setup-text" }, el("span", { class: "hm-setup-title" }, "Finish setting up"),
            el("span", { class: "hm-setup-sub" }, s.requiredOk ? `${s.ready} of ${s.total} ready. Connect mail, tasks and more.` : `${s.ready} of ${s.total} ready. A couple of essentials need attention.`)),
          ui.icon("chevron-right")),
        el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--sm", onclick: () => {
          try { localStorage.setItem("hub.setup.dismissed", "1"); } catch (_) { /* ignore */ }
          setupCard.hidden = true;
        } }, "Later"));
      setupCard.hidden = false;
    })).catch(() => { /* optional card */ });
  }

  root.append(hero, setupCard, fridayCard, jump, forYou,
    todayCard.node,
    el("section", { class: "hm-sec hm-section", "aria-label": "Quick actions" },
      el("div", { class: "hm-head" }, el("h2", { class: "ui-eyebrow hm-eyebrow" }, el("b", {}, "02"), "Ship"), el("span", { class: "hm-head-rule", "aria-hidden": "true" })), quick),
    el("div", { class: "hm-pair" }, ringsCard.node, learnSec),
    notesCard.node, notifCard.node,
    el("p", { class: "hm-foot" }, "# pull down to refresh"));
  root.classList.add("ui-stagger");
  setTimeout(() => root.classList.remove("ui-stagger"), 1200);
  ui.pullToRefresh(container, () => load(true));

  // ---- loaders ----
  function renderStatus(laptop, error) {
    if (error || !laptop) {
      status.replaceChildren(kvRow("laptop", el("span", { class: "hm-led-v" }, el("span", { class: "ui-led ui-led--bad" }), error?.status === 0 ? "offline" : "unavailable"),
        { onClick: () => ctx.open("hub"), label: "Laptop status. Open Hub", tone: "bad" }));
      if (countdown) status.append(countdown);
      return;
    }
    const online = laptop.online !== false;
    const battery = typeof laptop.battery === "number" ? { percent: laptop.battery } : laptop.battery;
    const cpu = laptop.cpu_percent ?? laptop.cpu;
    const host = text(laptop.hostname, laptop.name);
    const chips = [kvRow("laptop", el("span", { class: "hm-led-v" }, el("span", { class: `ui-led ${online ? "ui-led--good ui-led--live" : "ui-led--bad"}` }),
      (online ? "online" : "offline") + (host ? ` · ${host}` : "")), { onClick: () => ctx.open("hub"), label: "Laptop status. Open Hub" })];
    if (battery?.percent != null) {
      const p = Math.round(battery.percent);
      chips.push(kvRow("battery", el("span", { class: "ui-num" }, `${p}%${battery.plugged ? " · charging" : ""}`), { tone: p <= 20 && !battery.plugged ? "warn" : undefined }));
    }
    if (cpu != null) chips.push(kvRow("cpu", el("span", { class: "ui-num" }, `${Math.round(cpu)}%`)));
    if (countdown) chips.push(countdown);
    status.replaceChildren(...chips);
  }

  function timeLabel(item) {
    if (item.all_day) return "All day";
    const t = item.start || item.time || item.at || item.due;
    if (!t) return "";
    const d = new Date(t);
    return Number.isNaN(d.getTime()) ? String(t) : ui.clockTime(d);
  }

  function renderToday({ agenda, tasks, reminders, notes: hints }) {
    const blocks = [];
    const now = Date.now();
    if (agenda.length) {
      blocks.push(el("div", { class: "hm-block" }, el("p", { class: "ui-caps hm-block-k" }, "agenda"),
        el("div", { class: "hm-timeline" }, agenda.slice(0, 4).map((m) => {
          const start = new Date(m.start || 0).getTime(), end = new Date(m.end || 0).getTime();
          const live = start && end && start <= now && now < end;
          const past = end && end < now;
          return el("div", { class: "hm-event" + (live ? " is-live" : "") + (past ? " is-past" : "") },
            el("span", { class: "hm-event-time ui-num" }, timeLabel(m)),
            el("span", { class: "hm-event-bar", "aria-hidden": "true" }),
            el("span", { class: "hm-event-body" }, el("span", { class: "hm-event-title" }, text(m.title, m.summary) || "Untitled event"),
              live ? el("span", { class: "ui-badge ui-badge--good" }, el("span", { class: "ui-dot ui-dot--good ui-dot--live" }), "Now") : null),
            m.link && /^https:\/\//.test(m.link) ? el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm hm-join", href: m.link, target: "_blank", rel: "noopener noreferrer" }, ui.icon("video"), "Join") : null);
        }))));
    }
    if (tasks.length) {
      blocks.push(el("div", { class: "hm-block" }, el("p", { class: "ui-caps hm-block-k" }, "tasks"),
        el("div", { class: "ui-list" }, tasks.slice(0, 4).map((t) => ui.listRow({
          lead: el("span", { class: "hm-check" + (t.completed ? " is-done" : ""), "aria-hidden": "true" }, ui.icon("check")),
          title: text(t.name, t.title) || "Task",
          sub: [t.source === "todo" ? "To-do" : text(t.project?.name, typeof t.project === "string" ? t.project : ""), dueLabel(t.due || t.due_on)].filter(Boolean).join(" · "),
          meta: t.overdue ? el("span", { class: "ui-badge ui-badge--bad" }, "Overdue") : null,
          onClick: () => openTarget(t.open, { view: "work", section: "tasks", id: t.gid || t.id }) })))));
    }
    if (reminders.length) {
      blocks.push(el("div", { class: "hm-block" }, el("p", { class: "ui-caps hm-block-k" }, "reminders"),
        el("div", { class: "ui-list" }, reminders.slice(0, 3).map((r) => ui.listRow({
          icon: "alarm", tone: "warn", title: text(r.title, r.text, r.body) || "Reminder", meta: text(r.when) || timeLabel(r),
          onClick: () => openTarget(r.open, { view: "reminders", id: r.id }) })))));
    }
    const count = agenda.length + tasks.length + reminders.length;
    todayCard.head.querySelector(".hm-count")?.remove();
    if (count) todayCard.head.querySelector("h2").after(el("span", { class: "ui-badge ui-badge--accent hm-count" }, String(count)));
    if (!blocks.length) {
      todayCard.body.replaceChildren(ui.emptyState({ icon: "coffee", title: "A clear day", compact: true,
        body: hints.length ? hints.join(" ") : "No meetings, tasks or reminders today. Enjoy the calm.",
        action: { label: "Plan something", icon: "plus", onClick: () => ctx.open("work", "tasks") } }));
    } else todayCard.body.replaceChildren(...blocks);
  }

  function streakValue(raw) {
    if (typeof raw === "number") return { days: raw };
    if (raw && typeof raw === "object") return { days: Number(raw.days ?? raw.count ?? raw.value ?? raw.current ?? 0), goal: Number(raw.goal) || undefined, label: raw.label };
    return { days: 0 };
  }
  const STREAK_META = {
    learn: { label: "Learning", tone: "violet" }, quiz: { label: "Quizzes", tone: "violet" }, english: { label: "English", tone: "teal" },
    notes: { label: "Notes", tone: "orange" }, focus: { label: "Focus", tone: "good" }, habits: { label: "Habits", tone: "pink" },
    journal: { label: "Journal", tone: "orange" }, cards: { label: "Cards", tone: "info" },
  };
  function renderRings(streaks) {
    const all = Array.isArray(streaks)
      ? streaks.map((s) => [s.id || s.key || s.label, s])
      : Object.entries(streaks || {});
    const entries = all.filter(([, raw]) => raw != null)
      .map((e, i) => ({ e, i, d: streakValue(e[1]).days })).sort((a, b) => b.d - a.d || a.i - b.i).slice(0, 4).map((x) => x.e);
    const size = entries.length > 3 ? 64 : 72;
    const items = entries.map(([key, raw], i) => {
      const v = streakValue(raw);
      const meta = STREAK_META[key] || { label: v.label || String(key), tone: ["accent", "teal", "orange"][i % 3] };
      const goal = v.goal || 7;
      const ring = ui.ringProgress({ value: Math.min(1, v.days / goal), size, stroke: size > 64 ? 7 : 6, tone: meta.tone, big: String(v.days), small: v.days === 1 ? "day" : "days", label: `${meta.label} streak` });
      return el("div", { class: "hm-ring" }, ring, el("span", { class: "hm-ring-label" }, v.label || meta.label));
    });
    if (!items.length || entries.every(([, raw]) => !streakValue(raw).days)) {
      ringsCard.body.replaceChildren(el("div", { class: "hm-rings hm-rings--empty" }, ...(items.length ? items : ["Learning", "English", "Notes"].map((l, i) =>
        el("div", { class: "hm-ring" }, ui.ringProgress({ value: 0, size: 72, stroke: 7, tone: ["violet", "teal", "orange"][i], big: "0", small: "days", label: l }), el("span", { class: "hm-ring-label" }, l))))),
      el("p", { class: "hm-rings-hint" }, "Do one quiz or speaking practice today to start a streak."));
      return;
    }
    const best = Math.max(...entries.map(([, raw]) => streakValue(raw).days));
    ringsCard.body.replaceChildren(el("div", { class: "hm-rings" }, items),
      el("p", { class: "hm-rings-hint" }, best >= 7 ? "A full week. Keep it going." : `${7 - best} more ${7 - best === 1 ? "day" : "days"} to a full week.`));
  }

  function renderLearn(history, english, learning) {
    const quiz = learning?.last || arr(history?.quizzes)[0];
    const due = learning?.vocab_due ?? english?.vocab_due ?? 0;
    const chips = el("div", { class: "hm-learn-chips" });
    if (cardsDue) chips.append(el("button", { type: "button", class: "ui-chip hm-learn-chip", onclick: () => ctx.open("cards") }, ui.icon("layers"), `${cardsDue} flashcards due`));
    if (due) chips.append(el("button", { type: "button", class: "ui-chip hm-learn-chip", onclick: () => ctx.open("learn", "english/vocab") }, ui.icon("language"), `${due} words to review`));
    chips.append(el("button", { type: "button", class: "ui-chip hm-learn-chip", onclick: () => ctx.open("learn", "english/speak") }, ui.icon("mic"), "Speaking practice"));
    const title = quiz ? text(quiz.title, quiz.source_label) || "Your last quiz" : "Start your first quiz";
    const sub = quiz ? [quiz.percent != null ? `Last score ${Math.round(quiz.percent)}%` : "", quiz.date ? ui.relTime(quiz.date) : ""].filter(Boolean).join(" · ")
      : "Turn any note, PDF or topic into a quick quiz.";
    const pct = quiz?.percent != null ? Math.max(0, Math.min(100, quiz.percent)) / 100 : null;
    learnCard.replaceChildren(...[
      el("p", { class: "ui-tag hm-learn-kicker" }, el("b", {}, "continue"), el("span", {}, quiz ? "last-quiz" : "first-quiz")),
      el("h2", { class: "hm-learn-title" }, title),
      el("p", { class: "hm-learn-sub" }, sub),
      pct != null ? el("div", { class: "hm-learn-bar" }, ui.progressBar(pct, "Last score")) : null,
      el("div", { class: "hm-learn-actions" },
        el("button", { type: "button", class: "ui-btn ui-btn--primary hm-learn-go", onclick: () => ctx.open("learn", "quiz") }, ui.icon(quiz ? "play" : "plus"), quiz ? "Resume" : "New quiz"),
        el("button", { type: "button", class: "ui-btn ui-btn--secondary hm-learn-alt", onclick: () => ctx.open("learn", "chat") }, "Study chat")),
      chips].filter(Boolean));
  }

  function renderNotifications(list) {
    const items = arr(Array.isArray(list) ? list : list?.latest).filter((n) => !n.read).slice(0, 3);
    notifCard.node.hidden = !items.length;
    if (!items.length) return;
    const tone = { warn: "warn", error: "bad", info: "info", good: "good" };
    notifCard.body.replaceChildren(el("div", { class: "ui-list ui-list--icons hm-flush" }, items.map((n) => ui.listRow({
      icon: n.level === "error" ? "alert-circle" : n.level === "warn" ? "alert" : "bell", tone: tone[n.level] || "info",
      title: text(n.title) || "Notification", sub: text(n.body), meta: n.created || n.time ? ui.relTime(n.created || n.time) : null,
      onClick: () => openTarget(n.open || n.link, { view: "notifications", id: n.id }) }))));
  }

  function renderNotes(notes, error) {
    if (error) {
      notesCard.body.replaceChildren(ui.emptyState({ icon: "wifi-off", title: "Notes are out of reach", body: error.message, compact: true }));
      return;
    }
    const list = arr(notes).slice(0, 3);
    if (!list.length) {
      notesCard.body.replaceChildren(ui.emptyState({ icon: "note-plus", title: "No notes yet", body: "Tap the mic and say a thought. It's saved with a title and tags.", compact: true,
        action: { label: "Voice note", icon: "mic", onClick: () => ctx.open("notes", "voice") } }));
      return;
    }
    notesCard.body.replaceChildren(el("div", { class: "ui-list ui-list--icons hm-flush" }, list.map((n) => ui.listRow({
      icon: n.source === "voice" ? "mic" : "note", tone: n.source === "voice" ? "pink" : "orange",
      title: text(n.title, n.preview) || "Untitled note", sub: text(n.preview, arr(n.tags).map((t) => "#" + t).join(" ")),
      meta: n.created ? ui.relTime(n.created) : null, onClick: () => openTarget(n.open, { view: "notes", id: n.id }) }))));
  }

  async function load(force) {
    if (loading) return loading;
    if (!force && Date.now() - loadedAt < 60000) return null;
    loading = (async () => {
      const home = await softFail(api("/api/home"));
      const h = home.ok && home.v && typeof home.v === "object" ? home.v : {};
      const failed = new Set(arr(h.sections_failed));
      const has = (k) => h[k] != null && !failed.has(k);

      // laptop chip
      const laptopP = has("laptop") ? Promise.resolve({ ok: true, v: h.laptop }) : softFail(api("/api/status"));
      // today
      const hints = [];
      const tasksP = has("tasks") ? Promise.resolve(arr(h.tasks))
        : api("/api/work/asana/tasks?filter=today").then((r) => arr(r?.tasks).filter((t) => !t.completed), (e) => { if (e.status === 503) hints.push("Connect Asana to see tasks here."); return []; });
      const agendaP = has("agenda") ? Promise.resolve(arr(h.agenda))
        : api(`/api/work/calendar?day=${today()}&days=1`).then((r) => arr(r?.meetings), (e) => { if (e.status === 503) hints.push("Connect your calendar to see meetings."); return []; });
      const remindersP = Promise.resolve(arr(h.reminders));
      // learning
      const historyP = has("learning") ? Promise.resolve(null) : api("/api/learn/history").catch(() => null);
      const englishP = has("learning") ? Promise.resolve(null) : api("/api/english/overview").catch(() => null);
      // notes
      const notesKey = has("recent_notes") ? "recent_notes" : has("notes") ? "notes" : null;
      const notesP = notesKey ? Promise.resolve({ ok: true, v: { notes: h[notesKey] } }) : softFail(api("/api/notes"));

      const name = text(h.greeting?.name);
      if (name && name !== userName) { userName = name.slice(0, 24); paintClock(); }
      renderSuggestions(arr(h.suggestions), h.summaries || {});
      laptopP.then((r) => renderStatus(r.ok ? r.v : null, r.ok ? null : r.e));
      Promise.all([agendaP, tasksP, remindersP]).then(([agenda, tasks, reminders]) => renderToday({ agenda, tasks, reminders, notes: hints }));
      renderNotifications(h.notifications);
      notesP.then((r) => renderNotes(r.ok ? r.v?.notes : null, r.ok ? null : r.e));
      const [history, english] = await Promise.all([historyP, englishP]);
      renderLearn(history, english, h.learning);
      const notes = await notesP;
      let streaks = has("streaks") ? h.streaks : null;
      if (!streaks || (typeof streaks === "object" && !Object.keys(streaks).length)) {
        const weekAgo = Date.now() - 7 * 86400000;
        const noteDays = new Set(arr(notes.ok ? notes.v?.notes : []).filter((n) => new Date(n.created).getTime() > weekAgo).map((n) => String(n.created).slice(0, 10))).size;
        streaks = { learn: history?.streak_days || 0, english: english?.streak_days || 0, notes: { days: noteDays, goal: 7, label: "Notes" } };
      }
      renderRings(streaks);
      await Promise.allSettled([laptopP, agendaP, tasksP, notesP]);
      loadedAt = Date.now();
    })().finally(() => { loading = null; });
    return loading;
  }

  paintClock();
  load(true);
  checkSetup();
  const clockTimer = setInterval(paintClock, 60000);

  return {
    show() { paintClock(); load(false); checkSetup(); },
    refresh() { return load(true); },
    destroy() { clearInterval(clockTimer); },
  };
}
