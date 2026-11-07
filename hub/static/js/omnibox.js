// Global search overlay: screens and quick actions match instantly on the phone; everything else comes
// from GET /api/search?q= -> {results:[{title, snippet, kind, open:{view, section, id}}]}.
// A hub without that route (404) or an older one that only searches files still gets local results.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { QUICK_ACTIONS, runQuickAction } from "./home.js";

const RECENT_KEY = "hub.search.recent";
const KIND = {
  workspace: { label: "Workspaces", icon: "cast", tone: "teal" },
  note: { label: "Notes", icon: "note", tone: "orange" },
  notes: { label: "Notes", icon: "note", tone: "orange" },
  task: { label: "Tasks", icon: "task", tone: "accent" },
  tasks: { label: "Tasks", icon: "task", tone: "accent" },
  event: { label: "Calendar", icon: "calendar", tone: "info" },
  meeting: { label: "Calendar", icon: "calendar", tone: "info" },
  email: { label: "Email", icon: "mail", tone: "info" },
  file: { label: "Files", icon: "file", tone: "gray" },
  folder: { label: "Files", icon: "folder", tone: "gray" },
  reminder: { label: "Reminders", icon: "alarm", tone: "warn" },
  notification: { label: "Notifications", icon: "bell", tone: "bad" },
  quiz: { label: "Quizzes", icon: "quiz", tone: "violet" },
  vocab: { label: "Vocabulary", icon: "language", tone: "teal" },
  word: { label: "Vocabulary", icon: "language", tone: "teal" },
  chat: { label: "Conversations", icon: "talk", tone: "accent" },
  kb: { label: "Knowledge", icon: "book-open", tone: "violet" },
  doc: { label: "Knowledge", icon: "book-open", tone: "violet" },
  action: { label: "Laptop actions", icon: "zap", tone: "warn" },
  app: { label: "Apps", icon: "grid", tone: "gray" },
  setting: { label: "Settings", icon: "settings", tone: "gray" },
  todo: { label: "To-dos", icon: "task", tone: "good" },
  journal: { label: "Journal", icon: "book", tone: "orange" },
  button: { label: "Quick buttons", icon: "zap", tone: "warn" },
  bookmark: { label: "Bookmarks", icon: "bookmark", tone: "info" },
  snippet: { label: "Snippets", icon: "code", tone: "gray" },
  expense: { label: "Expenses", icon: "chart", tone: "teal" },
  card: { label: "Flashcards", icon: "layers", tone: "violet" },
  cards: { label: "Flashcards", icon: "layers", tone: "violet" },
  automation: { label: "Automations", icon: "zap", tone: "warn" },
  inbox: { label: "Inbox", icon: "inbox", tone: "info" },
  routine: { label: "Routines", icon: "zap", tone: "violet" },
  journal_entry: { label: "Journal", icon: "book", tone: "orange" },
  alert: { label: "Alerts", icon: "eye", tone: "bad" },
  alerts: { label: "Alerts", icon: "eye", tone: "bad" },
  reminders: { label: "Reminders", icon: "alarm", tone: "warn" },
  journals: { label: "Journal", icon: "book", tone: "orange" },
  bookmarks: { label: "Bookmarks", icon: "bookmark", tone: "info" },
  expenses: { label: "Expenses", icon: "chart", tone: "teal" },
  snippets: { label: "Snippets", icon: "code", tone: "gray" },
  automations: { label: "Automations", icon: "zap", tone: "warn" },
  emails: { label: "Email", icon: "mail", tone: "info" },
  files: { label: "Files", icon: "file", tone: "gray" },
  kb_chunk: { label: "Knowledge", icon: "book-open", tone: "violet" },
  habit: { label: "Habits", icon: "flame", tone: "good" },
};
const kindInfo = (k) => KIND[k] || { label: k ? k[0].toUpperCase() + k.slice(1) : "Other", icon: "circle", tone: "gray" };

function readRecent() {
  try { const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, 6) : []; }
  catch (_) { return []; }
}
function saveRecent(q) {
  const list = [q, ...readRecent().filter((x) => x.toLowerCase() !== q.toLowerCase())].slice(0, 6);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_) { /* private mode */ }
}

// Title with the query highlighted, built from text nodes (results are untrusted text).
function highlight(text, q) {
  const span = el("span", { class: "ui-row__title" });
  const s = String(text ?? "");
  const i = q ? s.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (i < 0) { span.textContent = s; return span; }
  span.append(s.slice(0, i), el("mark", {}, s.slice(i, i + q.length)), s.slice(i + q.length));
  return span;
}

function localMatches(ctx, q) {
  const needle = q.toLowerCase();
  const screens = ctx.views.filter((v) => !v.dynamic && (v.label.toLowerCase().includes(needle) || v.id.includes(needle)))
    .map((v) => ({ title: v.label, snippet: v.nav ? "Main section" : "Screen", icon: v.icon, run: () => ctx.open(v.id) }));
  const actions = QUICK_ACTIONS.filter((a) => (a.label + " " + (a.keywords || "")).toLowerCase().includes(needle))
    .map((a) => ({ title: a.label, snippet: a.hint, icon: a.icon, tone: a.tone, run: () => runQuickAction(a, ctx) }));
  return { screens, actions };
}

let openInstance = null;

export function openSearch(ctx, initial = "") {
  if (openInstance) { openInstance.input.focus(); return; }
  const input = el("input", { type: "search", class: "ob-input", placeholder: "Search notes, tasks, files, screens…",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "search", "aria-label": "Search everything",
    "aria-controls": "ob-results" });
  const clear = ui.button({ label: "Clear", icon: "close", variant: "ghost", iconOnly: true, class: "ob-clear", hidden: true });
  const cancel = el("button", { type: "button", class: "ui-btn ui-btn--ghost ob-cancel" }, "Cancel");
  const body = el("div", { class: "ob-body", id: "ob-results", "aria-live": "polite" });
  const node = el("div", { class: "ob", role: "dialog", "aria-modal": "true", "aria-label": "Search" },
    el("div", { class: "ob-head" }, el("div", { class: "ui-input-wrap" }, ui.icon("search"), input, clear), cancel), body);
  document.body.append(node);
  const previous = document.activeElement;
  let release = null;
  let closed = false;
  let controller = null;
  let timer = null;
  let firstRun = null;

  function close(opts = {}) {
    if (closed) return;
    closed = true;
    openInstance = null;
    controller?.abort();
    if (!opts.fromHistory && release) release();
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 170);
    previous?.focus?.({ preventScroll: true });
  }
  const go = (fn, q) => { if (q) saveRecent(q); close(); ui.historySettled().then(fn); };

  function group(title, rows, iconName) {
    return el("section", { class: "ob-group" },
      el("h3", { class: "ui-caps" }, iconName ? ui.icon(iconName, 14) : null, title),
      el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows));
  }
  function row({ title, snippet, icon, tone, run }, q) {
    const r = el("button", { type: "button", class: "ui-row ui-row--interactive ob-row" },
      ui.tile(icon || "circle", tone || "accent"),
      el("span", { class: "ui-row__body" }, highlight(title, q), snippet ? el("span", { class: "ui-row__sub" }, String(snippet)) : null),
      ui.icon("arrow-up-right", { class: "ui-row__chev" }));
    r.addEventListener("click", () => go(run, q));
    return r;
  }

  function renderIdle() {
    const recents = readRecent();
    const jumps = ctx.views.filter((v) => !v.dynamic).slice(0, 12).map((v) =>
      el("button", { type: "button", onclick: () => go(() => ctx.open(v.id)) }, ui.tile(v.icon, v.nav ? "accent" : "gray"), v.label));
    body.replaceChildren(...[
      recents.length ? el("section", { class: "ob-group" }, el("h3", { class: "ui-caps" }, "Recent"),
        el("div", { class: "ob-recents" }, recents.map((q) => el("button", { type: "button", class: "ui-chip",
          onclick: () => { input.value = q; update(); } }, ui.icon("history"), q)))) : null,
      el("section", { class: "ob-group" }, el("h3", { class: "ui-caps" }, "Quick actions"),
        el("div", { class: "ui-hscroll" }, QUICK_ACTIONS.map((a) => el("button", { type: "button", class: "ui-chip",
          onclick: () => go(() => runQuickAction(a, ctx)) }, ui.icon(a.icon), a.label)))),
      el("section", { class: "ob-group" }, el("h3", { class: "ui-caps" }, "Jump to"), el("div", { class: "ob-jump" }, jumps))].filter(Boolean));
  }

  async function update() {
    const q = input.value.trim();
    clear.hidden = !q;
    clearTimeout(timer);
    controller?.abort();
    firstRun = null;
    if (!q) { renderIdle(); return; }
    const { screens, actions } = localMatches(ctx, q);
    const local = [];
    if (actions.length) local.push(group("Actions", actions.map((m) => row(m, q)), "zap"));
    if (screens.length) local.push(group("Screens", screens.map((m) => row({ ...m, tone: "gray" }, q)), "grid"));
    firstRun = (actions[0] || screens[0])?.run || null;
    const remote = el("div", { class: "ui-stack ui-stack--lg" }, ui.skeleton("list", 3));
    body.replaceChildren(remote, ...local);
    if (q.length < 2) { remote.replaceChildren(); if (!local.length) remote.append(el("p", { class: "ob-status" }, ui.icon("search"), "Keep typing…")); return; }
    timer = setTimeout(async () => {
      controller = new AbortController();
      const signal = controller.signal;
      try {
        let data;
        try {
          data = await api(`/api/search?q=${encodeURIComponent(q)}`, { signal });
        } catch (error) {
          // An older hub only has the file-name search at /api/search (it wants ?root=): use /api/search/all.
          if (!(error instanceof ApiError) || ![400, 404, 422].includes(error.status)) throw error;
          data = await api(`/api/search/all?q=${encodeURIComponent(q)}`, { signal });
        }
        const results = Array.isArray(data?.results) ? data.results.filter((r) => r && (r.title || r.name)) : [];
        const groups = new Map();
        for (const r of results) {
          const kind = r.kind || (r.is_dir != null ? (r.is_dir ? "folder" : "file") : "other");
          const info = kindInfo(kind);
          if (!groups.has(info.label)) groups.set(info.label, { info, items: [] });
          const target = r.open || (r.path != null ? { view: "files" } : null);
          groups.get(info.label).items.push({
            title: r.title || r.name, snippet: r.snippet || r.path || "", icon: info.icon, tone: info.tone,
            run: () => {
              if (!target?.view) return;
              if (target.view === "files" && r.root && r.path) ctx.openFile(r.root, r.path);
              else ctx.open(target.view, target.section || undefined, { id: target.id });
            },
          });
        }
        if (!groups.size) {
          remote.replaceChildren(local.length ? el("p", { class: "ob-status" }, ui.icon("search"), `Nothing else matches “${q}”.`)
            : ui.emptyState({ icon: "search", title: "No results", body: `Nothing matches “${q}”. Try fewer words.`, compact: true }));
          return;
        }
        const first = [...groups.values()][0].items[0];
        firstRun = first.run;
        remote.replaceChildren(...[...groups.values()].map(({ info, items }) => group(info.label, items.slice(0, 8).map((m) => row(m, q)), info.icon)));
      } catch (error) {
        if (error.name === "AbortError") return;
        const unsupported = error instanceof ApiError && [404, 405, 422, 400].includes(error.status);
        remote.replaceChildren(el("p", { class: "ob-status" }, ui.icon(unsupported ? "info" : "wifi-off"),
          unsupported ? "Searching inside your notes and tasks arrives with the next hub update. Screens and actions work now."
            : (error.message || "Search is unavailable right now.")));
        if (!local.length) remote.append(ui.emptyState({ icon: "search", title: "No matching screens", body: "Try a word like notes, quiz or terminal.", compact: true }));
      }
    }, 200);
  }

  input.addEventListener("input", update);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && firstRun) { e.preventDefault(); go(firstRun, input.value.trim()); }
    if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  clear.addEventListener("click", () => { input.value = ""; update(); input.focus(); });
  cancel.addEventListener("click", () => close());
  release = ui.pushLayer(() => close({ fromHistory: true }));
  openInstance = { input, close };
  input.value = initial;
  update();
  requestAnimationFrame(() => input.focus());
  ui.haptic("select");
}
