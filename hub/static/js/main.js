// App shell: login, the view registry, navigation (bottom bar, top bar, Android back), and the small
// toolkit (`ctx`) handed to every screen module.
//
// A screen module exports `init(container, ctx)`. It builds its UI inside `container` (#view-<id>) and may
// return { show(), hide(), openSection(name), openItem(id, section), refresh() }; all optional.
import { api, ApiError } from "./api.js";
import * as ui from "./ui.js";
import { openSearch } from "./omnibox.js";

// ---- the registry: adding a screen is one line ----------------------------------------------------
// id: also the DOM id (#view-<id>) and the hash (#/<id>). path: module, loaded on first open.
// nav: shows in the bottom bar. under: the bottom-bar tab that stays highlighted (and where Back goes).
// A screen whose module is missing shows a friendly "not installed" page instead of breaking.
export const VIEWS = [
  { id: "home", label: "Home", icon: "home", path: "./home.js", nav: true },
  { id: "assistant", label: "Talk", icon: "talk", path: "./assistant.js", nav: true, title: "Talk" },
  { id: "work", label: "Work", icon: "work", path: "./work.js", nav: true },
  { id: "learn", label: "Learn", icon: "learn", path: "./learn.js", nav: true },
  { id: "apps", label: "Apps", icon: "apps", path: "./apps.js", nav: true },
  { id: "notes", label: "Notes", icon: "note", path: "./notes.js", under: "apps" },
  { id: "files", label: "Files", icon: "folder", path: "./files.js", under: "apps" },
  { id: "terminal", label: "Terminal", icon: "terminal", path: "./terminal.js", under: "apps" },
  { id: "hub", label: "Laptop", icon: "laptop", path: "./hub.js", under: "apps" },
  { id: "notifications", label: "Notifications", icon: "bell", path: "./notifications.js", under: "home" },
  { id: "settings", label: "Settings", icon: "settings", path: "./settings.js", under: "home" },
  { id: "reminders", label: "Reminders", icon: "alarm", path: "./reminders.js", under: "work" },
  { id: "inbox", label: "Inbox", icon: "inbox", path: "./inbox.js", under: "work" },
  { id: "screen", label: "Remote screen", icon: "cast", path: "./screen.js", under: "apps" },
  { id: "focus", label: "Focus", icon: "target", path: "./focus.js", under: "home" },
  { id: "automations", label: "Automations", icon: "zap", path: "./automations.js", under: "apps" },
  { id: "kb", label: "Knowledge", icon: "book-open", path: "./kb.js", under: "learn" },
  { id: "todos", label: "To-do", icon: "list-checks", path: "./todos.js", under: "apps" },
  { id: "habits", label: "Habits", icon: "flame", path: "./habits.js", under: "apps" },
  { id: "journal", label: "Journal", icon: "book", path: "./journal.js", under: "apps" },
  { id: "expenses", label: "Spending", icon: "trend", path: "./expenses.js", under: "apps" },
  { id: "bookmarks", label: "Read later", icon: "bookmark", path: "./bookmarks.js", under: "apps" },
  { id: "tools", label: "Tools", icon: "sliders", path: "./tools.js", under: "apps" },
  { id: "cards", label: "Flashcards", icon: "layers", path: "./cards.js", under: "learn" },
  { id: "setup", label: "Setup", icon: "check-circle", path: "./setup.js", under: "home" },
  { id: "launcher", label: "Launcher", icon: "grid", path: "./launcher.js", under: "apps" },
  { id: "friday", label: "Friday", icon: "sparkles", path: "./friday.js", under: "home" },
  { id: "watch", label: "Watch", icon: "eye", path: "./watch.js", under: "apps" },
];
// Screens that ship with the app; any other module is checked before it is imported.
const CORE = new Set(["home", "assistant", "work", "learn", "apps", "notes", "files", "terminal", "hub"]);
const byId = new Map(VIEWS.map((v) => [v.id, v]));
const NAV = VIEWS.filter((v) => v.nav);

const handles = {};   // id -> what init() returned
const mounting = {};  // id -> promise while the module loads
let current = null;
let started = false;
let navToken = 0;

const $ = (id) => document.getElementById(id);

function speak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

// An id that is not registered still works if js/<id>.js exists (convention over configuration).
function viewFor(id) {
  if (byId.has(id)) return byId.get(id);
  if (typeof id === "string" && /^[a-z][a-z0-9-]{0,30}$/.test(id)) {
    const label = id.replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase());
    const v = { id, label, icon: "grid", path: `./${id}.js`, under: "apps", dynamic: true };
    byId.set(id, v);
    return v;
  }
  return byId.get("home");
}

// ---- mounting --------------------------------------------------------------------------------------
function sectionFor(v) {
  let node = $("view-" + v.id);
  if (!node) {
    node = document.createElement("section");
    node.id = "view-" + v.id;
    node.className = "view";
    node.hidden = true;
    node.setAttribute("aria-label", v.label);
    node.addEventListener("scroll", onViewScroll, { passive: true });
    $("views").append(node);
  }
  return node;
}

function missing(container, v, error, installed) {
  container.replaceChildren(ui.el("div", { class: "sh-missing" }, ui.emptyState(installed ? {
    icon: "alert", title: `${v.label} couldn't start`,
    body: error?.message || "Something went wrong while opening this screen.",
    action: { label: "Try again", icon: "refresh", onClick: () => { delete handles[v.id]; delete mounting[v.id]; show(v.id, { push: false }); } },
  } : {
    icon: v.icon || "grid", title: `${v.label} isn't installed yet`,
    body: "This screen is on its way. Everything else keeps working in the meantime.",
    action: { label: "Back to Home", icon: "home", onClick: () => show("home") },
  })));
}

async function mount(v) {
  if (handles[v.id]) return handles[v.id];
  if (mounting[v.id]) return mounting[v.id];
  const container = sectionFor(v);
  mounting[v.id] = (async () => {
    const timer = setTimeout(() => { if (!handles[v.id]) container.replaceChildren(ui.el("div", { class: "sh-loading" }, ui.skeleton("card", 1), ui.skeleton("list", 4))); }, 120);
    let mod;
    const url = new URL(v.path, import.meta.url);
    try {
      // Ask first so a screen that isn't installed costs one quiet 404, not a failed module load.
      // A network error (offline) falls through to import(), which the service worker can answer.
      if (!CORE.has(v.id)) {
        let head = null;
        try { head = await fetch(url, { method: "HEAD", cache: "no-store" }); } catch (_) { /* offline */ }
        if (head && head.status === 404) throw Object.assign(new Error("not installed"), { notInstalled: true });
      }
      mod = await import(v.path);
    } catch (error) {
      clearTimeout(timer);
      let installed = !error.notInstalled;
      if (installed && !CORE.has(v.id)) {
        try { installed = (await fetch(url, { method: "HEAD" })).status !== 404; } catch (_) { /* keep */ }
      }
      if (installed) console.error(v.id, error);
      if (!installed && v.id === "settings") return mountFallbackSettings(container);
      missing(container, v, error, installed);
      return {};
    }
    clearTimeout(timer);
    container.replaceChildren();
    try {
      return (await mod.init(container, ctx)) || {};
    } catch (error) {
      console.error(v.id, error);
      missing(container, v, error, true);
      return {};
    }
  })();
  handles[v.id] = await mounting[v.id];
  delete mounting[v.id];
  return handles[v.id];
}

// Until the Settings screen ships, the gear opens the Appearance controls so themes are reachable.
function mountFallbackSettings(container) {
  container.append(ui.el("div", { class: "sh-settings-lite ui-stack ui-stack--lg" },
    ui.el("div", { class: "ui-card ui-card--pad-lg" },
      ui.el("div", { class: "ui-card__head" }, ui.tile("palette", "violet"), ui.el("div", { class: "grow" },
        ui.el("h2", { class: "ui-card__title" }, "Appearance"), ui.el("p", { class: "ui-card__sub" }, "Saved on this phone."))),
      ui.appearancePanel()),
    ui.el("div", { class: "ui-card ui-card--pad-0" }, ui.el("div", { class: "ui-list ui-list--icons" },
      ui.listRow({ icon: "laptop", tone: "info", title: "Laptop and hub", sub: "Status, quick buttons, upload, log out", chevron: true, onClick: () => show("hub") }),
      ui.listRow({ icon: "apps", tone: "gray", title: "All features", sub: "The Apps directory", chevron: true, onClick: () => show("apps") }))),
  ));
  return {};
}

// ---- navigation ------------------------------------------------------------------------------------
function navIndex(v) { return NAV.findIndex((n) => n.id === (v.nav ? v.id : v.under)); }

async function show(id, { section, item, push = true, replace = false } = {}) {
  const token = ++navToken;
  await ui.historySettled();
  const v = viewFor(id);
  const prev = current ? byId.get(current) : null;
  const container = sectionFor(v);
  if (current !== v.id) {
    if (prev) {
      handles[prev.id]?.hide?.();
      const old = $("view-" + prev.id);
      if (old) old.hidden = true;
    }
    current = v.id;
    container.hidden = false;
    const a = prev ? navIndex(prev) : -1, b = navIndex(v);
    const dir = !prev ? "fade" : (prev.nav && v.nav) ? (b > a ? "fwd" : "back") : (v.nav ? "back" : "fwd");
    container.classList.remove("sh-enter-fwd", "sh-enter-back", "sh-enter-fade");
    void container.offsetWidth;
    container.classList.add("sh-enter-" + dir);
    container.addEventListener("animationend", () => container.classList.remove("sh-enter-" + dir), { once: true });
    paintChrome(v);
  }
  const hash = "#/" + v.id + (section ? "/" + section : "");
  try {
    if (replace) history.replaceState({ view: v.id, section: section || null }, "", hash);
    else if (push && (history.state?.view !== v.id || (section && history.state?.section !== section))) {
      history.pushState({ view: v.id, section: section || null }, "", hash);
    }
  } catch (_) { /* sandboxed */ }
  const handle = await mount(v);
  if (token !== navToken || current !== v.id) return handle;
  handle.show?.();
  if (section) handle.openSection?.(section);
  if (item != null) handle.openItem?.(item, section);
  return handle;
}

function paintChrome(v) {
  const owner = v.nav ? v.id : v.under;
  const i = NAV.findIndex((n) => n.id === owner);
  for (const button of document.querySelectorAll("#tabs [data-tab]")) {
    const on = button.dataset.tab === owner;
    if (on) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    button.setAttribute("aria-selected", String(on));
  }
  const pill = document.querySelector(".sh-pill");
  if (pill) { pill.style.setProperty("--i", String(Math.max(0, i))); pill.classList.toggle("is-off", i < 0); }
  const bar = $("appbar");
  const title = bar.querySelector("h1");
  const text = v.id === "home" ? "Laptop Hub" : (v.title || v.label);
  if (title.textContent !== text) {
    title.textContent = text;
    title.classList.remove("is-changing"); void title.offsetWidth; title.classList.add("is-changing");
  }
  const sub = !v.nav;
  bar.classList.toggle("has-back", sub);
  bar.querySelector(".sh-back").hidden = !sub;
  bar.querySelector(".sh-logo").hidden = sub;
  bar.classList.toggle("is-scrolled", ($("view-" + v.id)?.scrollTop || 0) > 4);
  document.title = v.id === "home" ? "Laptop Hub" : `${text} · Laptop Hub`;
}

function onViewScroll(event) {
  if (event.currentTarget.id !== "view-" + current) return;
  $("appbar").classList.toggle("is-scrolled", event.currentTarget.scrollTop > 4);
}

function goBack() {
  const v = byId.get(current);
  if (history.state?.view && history.length > 1 && history.state.view !== "home") history.back();
  else show(v?.under || "home");
}

window.addEventListener("popstate", (event) => {
  if (!started) return;
  const state = event.state;
  if (state?.view) show(state.view, { section: state.section || undefined, push: false });
  else show(parseHash().view || "home", { push: false });
});

function parseHash() {
  const path = (location.hash || "").replace(/^#\/?/, "").split("?")[0];   // a ?query (e.g. ?url=) is for the screen
  const [view, ...rest] = path.split("/");
  return { view: view || null, section: rest.join("/") || undefined };
}

// ---- badges ----------------------------------------------------------------------------------------
const badgeCounts = {};
function setBadge(id, count) {
  const n = Math.max(0, Number(count) || 0);
  badgeCounts[id] = n;
  const targets = [...document.querySelectorAll(`[data-badge-for="${id}"]`)];
  for (const host of targets) {
    let node = host.querySelector(".ui-count");
    if (!n) { node?.remove(); continue; }
    if (!node) { node = ui.el("span", { class: "ui-count" }); host.append(node); }
    node.textContent = n > 99 ? "99+" : String(n);
  }
  if (id === "notifications") {
    const bell = $("ab-bell");
    if (bell) bell.setAttribute("aria-label", n ? `Notifications, ${n} unread` : "Notifications");
  }
}

// Unread notifications: polled every 30 s while the app is visible. A hub without the notify module
// answers 404, so polling backs off to every 5 minutes and the bell shows no count.
let pollTimer = null;
let lastUnread = 0;
async function pollUnread() {
  clearTimeout(pollTimer);
  let delay = 30000;
  if (document.visibilityState === "visible" && started) {
    try {
      const { count } = await api("/api/notifications/unread-count");
      const n = Number(count) || 0;
      if (n > lastUnread) { $("ab-bell")?.classList.remove("is-ringing"); void $("ab-bell")?.offsetWidth; $("ab-bell")?.classList.add("is-ringing"); }
      lastUnread = n;
      setBadge("notifications", n);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) delay = 300000;
    }
  }
  pollTimer = setTimeout(pollUnread, delay);
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") pollUnread(); });

// ---- ctx -------------------------------------------------------------------------------------------
const ctx = {
  api,
  ui,
  toast: ui.toast,
  speak,
  secure: window.isSecureContext,
  views: VIEWS,
  goto: (view) => show(view),
  // Open a screen, optionally one section of it, optionally one item: ctx.open("work", "email") or
  // ctx.open("notes", null, {id: "abc"}). Returns a promise of the screen's handle.
  open(view, section, opts = {}) {
    if (view === "search") { openSearch(ctx, section || ""); return Promise.resolve(null); }
    return show(view, { section: section || undefined, item: opts.id ?? opts.item });
  },
  openFile(root, path) {
    return show("files").then((h) => h?.openFile?.(root, path));
  },
  back: goBack,
  search: (q = "") => openSearch(ctx, q),
  setBadge,
  refreshNotifications: pollUnread,
  current: () => current,
};

// ---- chrome ----------------------------------------------------------------------------------------
function buildChrome() {
  const bar = $("appbar");
  const back = ui.el("button", { type: "button", class: "sh-iconbtn sh-back", "aria-label": "Back", hidden: true, onclick: goBack }, ui.icon("chevron-left"));
  const logo = ui.el("span", { class: "sh-logo", "aria-hidden": "true" }, ui.icon("laptop"));
  const search = ui.el("button", { type: "button", class: "sh-iconbtn", "aria-label": "Search", "data-tip": "Search",
    onclick: () => openSearch(ctx) }, ui.icon("search"));
  const bell = ui.el("button", { type: "button", id: "ab-bell", class: "sh-iconbtn", "aria-label": "Notifications", "data-badge-for": "notifications",
    onclick: () => { ui.haptic("select"); show("notifications"); } }, ui.icon("bell"));
  const gear = ui.el("button", { type: "button", class: "sh-iconbtn", "aria-label": "Settings", "data-tip": "Settings",
    onclick: () => show("settings") }, ui.icon("settings"));
  bar.replaceChildren(back, ui.el("div", { class: "sh-title" }, logo, ui.el("h1", {}, "Laptop Hub")),
    ui.el("div", { class: "sh-actions" }, search, bell, gear));

  const nav = $("tabs");
  const dock = ui.el("div", { class: "sh-dock", role: "tablist", "aria-label": "Main sections" }, ui.el("span", { class: "sh-pill", "aria-hidden": "true" }));
  dock.style.setProperty("--n", String(NAV.length));
  for (const v of NAV) {
    dock.append(ui.el("button", { type: "button", class: "sh-tab", role: "tab", "data-tab": v.id, "data-badge-for": v.id,
      "aria-selected": "false",
      onclick: () => {
        ui.haptic("select");
        if (current === v.id) { const s = $("view-" + v.id); s?.scrollTo({ top: 0, behavior: "smooth" }); handles[v.id]?.refresh?.(); return; }
        show(v.id);
      } }, ui.icon(v.icon), ui.el("span", {}, v.label)));
  }
  nav.replaceChildren(dock);

  // "/" or Ctrl+K opens search on a keyboard.
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
    if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) { e.preventDefault(); openSearch(ctx); }
  });
}

function startApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  if (!started) {
    started = true;
    buildChrome();
    $("app").classList.add("is-entering");
    const { view, section } = parseHash();
    const target = view && view !== "search" ? view : "home";
    try { history.replaceState({ view: "home" }, "", "#/home"); } catch (_) { /* ignore */ }
    show("home", { push: false }).then(() => { if (target !== "home") show(target, { section }); });
    setTimeout(pollUnread, 800);
  }
}

// ---- login -----------------------------------------------------------------------------------------
function showLogin(message) {
  $("app").hidden = true;
  const login = $("login");
  login.hidden = false;
  login.classList.remove("is-success");
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message || "";
  $("token").setAttribute("aria-invalid", message ? "true" : "false");
  if (message) {
    const card = $("login-form");
    card.classList.remove("ui-shake"); void card.offsetWidth; card.classList.add("ui-shake");
  }
  if (!window.matchMedia("(pointer: coarse)").matches || message) $("token").focus({ preventScroll: true });
}

$("token-toggle").addEventListener("click", (e) => {
  const input = $("token");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  e.currentTarget.setAttribute("aria-pressed", String(!showing));
  e.currentTarget.setAttribute("aria-label", showing ? "Show token" : "Hide token");
});
$("token").addEventListener("input", () => { $("login-error").hidden = true; $("token").setAttribute("aria-invalid", "false"); });
if (navigator.clipboard?.readText && window.isSecureContext) {
  const paste = $("token-paste");
  paste.hidden = false;
  paste.addEventListener("click", async () => {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text) { $("token").value = text; $("login-form").requestSubmit(); }
    } catch (_) { ui.toast("Clipboard access was blocked. Long-press the field to paste.", { kind: "warn" }); }
  });
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("token").value.trim();
  if (!token) { showLogin("Paste your login token first."); return; }
  const submit = $("login-submit");
  try {
    await ui.busy(submit, api("/api/login", { method: "POST", json: { token } }));
    $("token").value = "";
    $("token").type = "password";
    ui.haptic("success");
    $("login").classList.add("is-success");
    setTimeout(startApp, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 260);
  } catch (error) {
    ui.haptic("error");
    const msg = error instanceof ApiError && error.status === 401 ? "That token didn't match. Check it and try again."
      : error instanceof ApiError && error.status === 429 ? "Too many tries. Wait a minute, then try again."
      : error.message;
    showLogin(msg);
  }
});

window.addEventListener("hub:unauthorized", () => showLogin("Your session ended. Please log in again."));

(async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    const session = await api("/api/session");
    if (session.authenticated) startApp();
    else showLogin();
  } catch (error) {
    showLogin(error instanceof ApiError ? error.message : "Something went wrong starting the app.");
  }
})();
