// Files tab: browse the shared folders, read documents, ask the model about a passage, and (in writable
// folders) create, edit, rename, move, copy, delete, upload and download through /api/fileops.
import { api, sse, fileUrl, renderMarkdown, el } from "./api.js";
import * as ui from "./ui.js";
import { providerSegmented, normalizeProvider, defaultProvider, providerLabel } from "./providers.js";
import { openEditor } from "./fileeditor.js";

const MAX_INLINE = 2 * 1024 * 1024; // bigger files are offered as a download instead
const MAX_ASK = 12000; // characters sent to the model
const MAX_UPLOAD = 100 * 1024 * 1024; // the server cuts uploads off here
const RENDER_EVERY_MS = 100; // answer re-render throttle (~10 per second)
const LONG_PRESS_MS = 480;
const KEY_ROOT = "hub.files.root";
const KEY_PATHS = "hub.files.paths";
const KEY_PROVIDER = "hub.files.provider";
const KEY_VIEW = "hub.files.view";
const KEY_SORT = "hub.files.sort";

const MARKDOWN = new Set(["md", "markdown"]);
const IMAGES = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico"]);
const WRAPPED_TEXT = new Set(["txt", "log", "text", "rst"]);
const TEXT = new Set([
  "txt", "text", "log", "json", "jsonl", "csv", "tsv", "yml", "yaml", "toml", "ini", "cfg", "conf",
  "xml", "html", "htm", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "sh", "bash", "zsh", "bat",
  "ps1", "c", "h", "cpp", "hpp", "cs", "java", "kt", "go", "rs", "rb", "php", "sql", "tex", "rst",
  "env", "gitignore", "lock", "diff", "patch", "ipynb",
]);

const SORTS = {
  "name-asc": { label: "Name", long: "Name, A to Z" },
  "name-desc": { label: "Name (Z-A)", long: "Name, Z to A" },
  "date-desc": { label: "Newest", long: "Date modified, newest first" },
  "date-asc": { label: "Oldest", long: "Date modified, oldest first" },
  "size-desc": { label: "Largest", long: "Size, largest first" },
  "size-asc": { label: "Smallest", long: "Size, smallest first" },
};

// ---- small helpers -------------------------------------------------------------------------

const store = {
  get(key, fallback = null) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (_) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch (_) { /* private mode */ }
  },
};

function extOf(path) {
  const name = path.split("/").pop();
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function kindOf(path) {
  const ext = extOf(path);
  if (MARKDOWN.has(ext)) return "markdown";
  if (IMAGES.has(ext)) return "image";
  if (ext === "pdf") return "pdf";
  if (TEXT.has(ext)) return "text";
  return "unknown"; // the server tells us (by content type) if it is text after all
}

function isEditable(path) {
  const kind = kindOf(path);
  return kind === "markdown" || kind === "text" || kind === "unknown";
}

const TONES = { markdown: "violet", image: "teal", pdf: "bad", text: "info", unknown: "gray" };
const KIND_ICON = { markdown: "note", image: "image", pdf: "file", text: "file", unknown: "file" };

function toneFor(entry) {
  return entry.is_dir ? "warn" : TONES[kindOf(entry.name)];
}

function iconFor(entry) {
  return entry.is_dir ? "folder" : KIND_ICON[kindOf(entry.name)];
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb < 10 ? kb.toFixed(1) : Math.round(kb)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

function fmtDate(seconds) {
  if (!seconds) return "";
  const date = new Date(seconds * 1000);
  const now = new Date();
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(now) - midnight(date)) / 86400000);
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Yesterday ${time}`;
  const opts = date.getFullYear() === now.getFullYear()
    ? { day: "numeric", month: "short" }
    : { day: "numeric", month: "short", year: "numeric" };
  return date.toLocaleDateString([], opts);
}

function baseName(path) {
  return path.split("/").pop() || path;
}

function parentOf(path) {
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

function joinPath(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

// Resolve a relative link from the document at `docPath`. Returns null if it leaves the root.
function resolvePath(docPath, href) {
  let target = href.split("#")[0].split("?")[0];
  if (!target) return null;
  try { target = decodeURIComponent(target); } catch (_) { /* keep as written */ }
  const parts = target.startsWith("/") ? [] : parentOf(docPath).split("/").filter(Boolean);
  for (const segment of target.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else {
      parts.push(segment);
    }
  }
  return parts.join("/");
}

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

const LAPTOP_DOWN = "Can't reach your laptop. Is the hub running and the laptop awake?";

// Fetch a file as text without trusting its size. Returns {text} | {tooBig} | {binary}.
async function fetchText(url, signal) {
  let response;
  try {
    response = await fetch(url, { credentials: "same-origin", signal });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error(LAPTOP_DOWN);
  }
  if (response.status === 401) window.dispatchEvent(new Event("hub:unauthorized"));
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.json()).detail; } catch (_) { /* not JSON */ }
    throw new Error(typeof detail === "string" && detail ? detail
      : `The laptop answered with an error (${response.status}).`);
  }
  const type = response.headers.get("content-type") || "";
  const length = Number(response.headers.get("content-length")) || 0;
  const textual = /^text\/|json|xml|javascript/.test(type);
  if (!textual) { response.body?.cancel().catch(() => {}); return { binary: true }; }
  if (length > MAX_INLINE) { response.body?.cancel().catch(() => {}); return { tooBig: true }; }
  let text;
  try {
    text = await response.text();
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error(LAPTOP_DOWN);
  }
  if (text.length > MAX_INLINE) return { tooBig: true };
  return { text };
}

// The installed Android app (Capacitor / WebView) cannot open new tabs; it handles downloads natively.
function inApp() {
  try {
    if (window.Capacitor?.isNativePlatform?.()) return true;
  } catch (_) { /* ignore */ }
  return /;\s*wv\)|\bwv\b/.test(navigator.userAgent || "");
}

function triggerDownload(url, name) {
  const a = el("a", { href: url, download: name || "", hidden: true });
  document.body.append(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
  ui.toast("Downloading\u2026");
}

// Open a file outside the reader: a new tab in a browser, a download (which Android then opens) in the app.
function openExternally(root, path) {
  if (inApp()) { triggerDownload(fileUrl(root, path, true), baseName(path)); return; }
  window.open(fileUrl(root, path), "_blank", "noopener");
}

function postJson(path, json) {
  return api(path, { method: "POST", json });
}

// The tinted tile for a file or folder. Images show a real thumbnail through fileUrl.
function entryTile(entry, root, fullPath, size) {
  const tile = ui.tile(iconFor(entry), toneFor(entry), size);
  tile.classList.add("fl-tile");
  if (!entry.is_dir && kindOf(entry.name) === "image" && root) {
    const img = el("img", { class: "fl-thumb", alt: "", loading: "lazy", decoding: "async", src: fileUrl(root, fullPath) });
    img.addEventListener("error", () => img.remove());
    tile.classList.add("has-thumb");
    tile.append(img);
  }
  return tile;
}

function lockBadge(text = "Read-only") {
  return el("span", { class: "ui-badge ui-badge--warn fl-lock" }, ui.icon("lock"), text);
}

// ---- the module ----------------------------------------------------------------------------

export function init(container, ctx) {
  if (!document.head.querySelector('link[href="/css/fileops.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/fileops.css" }));
  }

  // State
  let roots = []; // [{name, writable}]
  let root = null;
  let path = "";
  let entries = null;
  let searchResults = null; // null = not searching
  let searchQuery = "";
  let listToken = 0;
  let rootsPromise = null;
  let browserScroll = 0;
  let viewMode = store.get(KEY_VIEW) === "grid" ? "grid" : "list";
  let sortKey = SORTS[store.get(KEY_SORT)] ? store.get(KEY_SORT) : "name-asc";
  let selecting = false;
  const selected = new Map(); // path -> item
  const rowNodes = new Map(); // path -> {node, check}
  let panelKind = null; // "trash" | "recent" | "storage" | null
  let panelToken = 0;
  let lastMenuAt = 0;

  let readerOpen = false;
  let doc = null; // {root, path, size, text|null}
  let docToken = 0;
  let docAbort = null;
  const stack = []; // documents we came from by following links
  let lastSelection = "";
  let panelTouchedAt = 0;
  let listening = false;

  let askAbort = null;
  let answer = "";
  let renderTimer = null;
  let answerSheet = null;
  let answerEl = null;
  let sheetStatus = null;
  let sheetError = null;
  let provider = normalizeProvider(store.get(KEY_PROVIDER));

  const writable = (r) => !!roots.find((x) => x.name === r)?.writable;
  const writableRoots = () => roots.filter((r) => r.writable);

  // ---- Browser pane DOM
  const rootChips = el("div", { class: "fl-roots ui-hscroll", role: "group", "aria-label": "Shared folders" });
  const moreBtn = ui.button({ label: "More actions", icon: "more", variant: "ghost", iconOnly: true });
  const searchInput = el("input", {
    type: "search", class: "fl-search-input", placeholder: "Search names in this folder",
    "aria-label": "Search file names in this folder", enterkeyhint: "search",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false",
  });
  const searchForm = el("form", { class: "fl-search ui-input-wrap", role: "search" },
    ui.icon("search"), searchInput);
  const topRow = el("div", { class: "fl-top" }, rootChips, moreBtn);
  const crumbs = el("nav", { class: "fl-crumbs", "aria-label": "Folder path" });
  const sortBtn = el("button", { type: "button", class: "ui-chip fl-sortbtn", "aria-label": "Sort" });
  const viewBtn = ui.button({ label: "Grid view", icon: "grid", variant: "ghost", size: "sm", iconOnly: true, class: "fl-viewbtn" });
  const toolInfo = el("div", { class: "fl-info" });
  const tools = el("div", { class: "fl-tools" }, toolInfo, sortBtn, viewBtn);
  const listBox = el("div", { class: "fl-listbox", "aria-live": "polite" });
  const browser = el("div", { class: "fl-browser" }, topRow, searchForm, crumbs, tools, listBox);

  // ---- Secondary panel (trash, recent, storage)
  const panelBack = ui.button({ label: "Back to files", icon: "chevron-left", variant: "ghost", iconOnly: true });
  const panelTitle = el("h2", { class: "fl-panel-title" });
  const panelAction = el("div", { class: "fl-panel-action" });
  const panelBody = el("div", { class: "fl-panel-body" });
  const panel = el("div", { class: "fl-panel", hidden: true },
    el("header", { class: "fl-head" }, panelBack, el("div", { class: "grow" }, panelTitle), panelAction), panelBody);

  // ---- Reader pane DOM
  const backBtn = ui.button({ label: "Back", icon: "chevron-left", variant: "ghost", iconOnly: true, class: "fl-back" });
  const titleEl = el("h2", { class: "fl-title" });
  const subEl = el("div", { class: "fl-sub" });
  const editBtn = ui.button({ label: "Edit", icon: "edit", variant: "tonal", size: "sm", hidden: true, class: "fl-editbtn" });
  const readerMore = ui.button({ label: "File actions", icon: "more", variant: "ghost", iconOnly: true });
  const head = el("header", { class: "fl-head" }, backBtn, el("div", { class: "grow fl-headtext" }, titleEl, subEl), editBtn, readerMore);
  const docEl = el("article", { class: "fl-doc" });
  const scroller = el("div", { class: "fl-scroll" }, el("div", { class: "fl-docard ui-card" }, docEl));

  const scopeLabel = el("span", { class: "fl-scope", "aria-live": "polite" }, "Whole page");
  const clearSel = ui.button({ label: "Use the whole page instead of the selection", icon: "close", variant: "ghost", size: "sm", iconOnly: true, hidden: true, class: "fl-clearsel" });
  const provSeg = providerSegmented({
    value: provider, label: "Model",
    onChange: (v) => { provider = v; store.set(KEY_PROVIDER, provider); updateBar(); },
  });
  provSeg.classList.add("fl-prov");
  const askInput = el("input", {
    type: "text", class: "fl-ask-input", placeholder: "Ask a question about this",
    "aria-label": "Your question", enterkeyhint: "send", autocomplete: "off", maxlength: "500",
  });
  const askForm = el("form", { class: "fl-askform", hidden: true },
    askInput, ui.button({ label: "Send", icon: "send", variant: "primary", type: "submit" }));
  const aiButtons = [
    ["explain", "Explain", "sparkles"], ["summarise", "Summarise", "list"], ["quiz", "Quiz", "quiz"], ["custom", "Ask...", "help"],
  ].map(([mode, label, ic]) => ui.button({ label, icon: ic, variant: "tonal", size: "sm", class: "fl-ai", "data-mode": mode }));
  const bar = el("div", { class: "fl-bar" },
    el("div", { class: "fl-askhead" },
      el("span", { class: "fl-asktitle" }, ui.icon("sparkles"), "Ask about this file"),
      provSeg),
    el("div", { class: "fl-barhead" }, el("div", { class: "fl-scopebox" }, scopeLabel, clearSel)),
    askForm,
    el("div", { class: "fl-aibtns" }, aiButtons));
  const reader = el("div", { class: "fl-reader", hidden: true }, head, scroller, bar);

  // ---- Floating create button, selection bar, hidden upload input
  const fab = el("button", { type: "button", class: "ui-fab ui-fab--round fl-fab", "aria-label": "Add", hidden: true }, ui.icon("plus"));
  const selCount = el("span", { class: "fl-selcount ui-num" });
  const selBtns = el("div", { class: "fl-selbtns" });
  const selbar = el("div", { class: "fl-selbar", hidden: true, role: "toolbar", "aria-label": "Selected items" }, selCount, selBtns);
  const fileInput = el("input", { type: "file", multiple: true, class: "fo-file-input", "aria-label": "Choose files to upload", tabindex: "-1" });

  container.replaceChildren(browser, panel, reader, fab, selbar, fileInput);

  function updateChrome() {
    const inBrowser = !readerOpen && !panelKind;
    browser.hidden = !inBrowser;
    panel.hidden = !panelKind;
    fab.hidden = !(inBrowser && !selecting && root && writable(root) && !searchResults);
    selbar.hidden = !(inBrowser && selecting);
    syncOverlayHistory(!inBrowser);
  }

  // Android Back, in order: close a sheet or search or selection (layers), close the reader or a panel
  // (one layer while open), step back through followed document links, go up through the folders
  // visited (each folder change adds a history entry tagged with its location), and only then leave Files.
  let overlayRelease = null;
  let overlayPending = false;
  function syncOverlayHistory(open) {
    if (open && !overlayRelease && !overlayPending) {
      overlayPending = true;
      ui.historySettled().then(() => {
        overlayPending = false;
        if (!readerOpen && !panelKind) return;
        if (!overlayRelease) overlayRelease = ui.pushLayer(onOverlayBack);
      });
    } else if (!open && overlayRelease) {
      const release = overlayRelease;
      overlayRelease = null;
      release();
    }
  }
  function onOverlayBack() {
    overlayRelease = null;                      // the entry is already gone: close without rewinding again
    if (panelKind) { closePanel(); return; }
    const previous = readerOpen ? stack.pop() : null;
    if (previous) {                             // Back inside the reader steps through followed links first
      openReader(previous.root, previous.path, { size: previous.size, restoreScroll: previous.scroll, push: false });
      syncOverlayHistory(true);
      return;
    }
    if (readerOpen) closeReader();
  }

  // A small helper for modes that live above the folder list (selection, search results).
  function modeLayer(onBack) {
    let release = null;
    let pending = false;
    return {
      open() {
        if (release || pending) return;
        pending = true;
        ui.historySettled().then(() => {
          pending = false;
          if (!release && onBack.active()) release = ui.pushLayer(() => { release = null; onBack.close(); });
        });
      },
      close() {
        if (!release) return;
        const r = release;
        release = null;
        r();
      },
    };
  }
  const selectLayer = modeLayer({ active: () => selecting, close: () => exitSelect() });
  const searchLayer = modeLayer({ active: () => !!searchResults, close: () => clearSearch() });

  // Folder history: location entries on top of the Files entry made by the tab switcher.
  let tracked = null; // {root, path} the history knows about
  async function trackLocation() {
    if (!root) return;
    const loc = { root, path };
    if (tracked && tracked.root === loc.root && tracked.path === loc.path) return;
    const was = tracked;
    tracked = loc;
    await ui.historySettled();
    try {
      const state = history.state || {};
      if (state.view !== "files" || state.layer) return;
      if (!state.fileLoc && was) history.replaceState({ ...state, fileLoc: was }, "");
      history.pushState({ ...(history.state || {}), fileLoc: loc }, "");
    } catch (_) { /* sandboxed */ }
  }
  // First time on screen: make the entry we arrived on a "root" stop so Back climbs out of a restored folder.
  function trackInitial() {
    if (tracked || !root) return;
    try {
      const state = history.state || {};
      if (state.view !== "files" || state.layer) return;
      if (state.fileLoc) { tracked = { root: state.fileLoc.root, path: state.fileLoc.path }; return; }
      history.replaceState({ ...state, fileLoc: { root, path: "" } }, "");
      tracked = { root, path: "" };
      if (path) trackLocation();
    } catch (_) { /* sandboxed */ }
  }
  window.addEventListener("popstate", (event) => {
    const loc = event.state?.fileLoc;
    if (!loc || event.state.view !== "files" || !roots.some((r) => r.name === loc.root)) return;
    if (tracked && tracked.root === loc.root && tracked.path === loc.path) return;
    tracked = { root: loc.root, path: loc.path };
    if (readerOpen) closeReader();
    if (panelKind) closePanel();
    exitSelect();
    root = loc.root;
    path = loc.path;
    clearSearch(false);
    renderRoots();
    rememberLocation();
    loadList();
  });

  // ---- Roots and listing -------------------------------------------------------------------

  function savedPaths() {
    try { return JSON.parse(store.get(KEY_PATHS, "{}")) || {}; } catch (_) { return {}; }
  }

  function rememberLocation() {
    const paths = savedPaths();
    paths[root] = path;
    store.set(KEY_PATHS, JSON.stringify(paths));
    store.set(KEY_ROOT, root);
  }

  function loadRoots() {
    if (rootsPromise) return rootsPromise;
    rootsPromise = (async () => {
      try {
        const [list, ops] = await Promise.all([
          api("/api/roots"),
          api("/api/fileops/roots").catch(() => []), // without fileops everything is read-only
        ]);
        const flags = new Map((Array.isArray(ops) ? ops : []).map((r) => [r.name, !!r.writable]));
        roots = list.map((r) => ({ name: r.name, writable: flags.get(r.name) === true }));
        if (!roots.length) { renderRootsEmpty(); return false; }
        const names = roots.map((r) => r.name);
        const wanted = store.get(KEY_ROOT);
        root = names.includes(wanted) ? wanted : names[0];
        path = savedPaths()[root] || "";
        renderRoots();
        return true;
      } catch (error) {
        rootsPromise = null;
        renderFatal(error.message);
        return false;
      }
    })();
    return rootsPromise;
  }

  function renderRoots() {
    rootChips.replaceChildren(...roots.map((r) => el("button", {
      type: "button", class: "ui-chip fl-chip", "aria-pressed": String(r.name === root),
      "data-writable": r.writable ? "1" : "0",
      onclick: () => switchRoot(r.name),
    }, ui.icon(r.writable ? "folder" : "lock"), r.name)));
    updateTools();
  }

  function renderRootsEmpty() {
    rootChips.replaceChildren();
    crumbs.replaceChildren();
    listBox.replaceChildren(ui.emptyState({
      icon: "folder", title: "No folders are shared yet",
      body: "Add one on your laptop, then reopen this tab.",
    }));
  }

  function renderFatal(message) {
    rootChips.replaceChildren();
    crumbs.replaceChildren();
    listBox.replaceChildren(errorBox(message, async () => {
      listBox.replaceChildren(loadingBox());
      if (await loadRoots()) loadList();
    }));
  }

  function loadingBox() {
    return ui.skeleton("list", 5);
  }

  function errorBox(message, retry, extra) {
    return el("div", { class: "fl-errorbox ui-card ui-card--flat", role: "alert" },
      ui.notice(message, { tone: "bad" }),
      el("div", { class: "fl-errorbtns" },
        ui.button({ label: "Retry", icon: "refresh", variant: "primary", size: "sm", onClick: retry }),
        extra));
  }

  function switchRoot(name) {
    if (name === root) return;
    exitSelect();
    root = name;
    path = savedPaths()[root] || "";
    clearSearch(false);
    renderRoots();
    rememberLocation();
    loadList();
    trackLocation();
  }

  function goTo(folder) {
    exitSelect();
    path = folder;
    clearSearch(false);
    rememberLocation();
    loadList();
    trackLocation();
  }

  function renderCrumbs() {
    const parts = path ? path.split("/") : [];
    const nodes = [crumb(root, "", parts.length === 0, true)];
    parts.forEach((part, i) => {
      nodes.push(ui.icon("chevron-right", { class: "fl-sep" }));
      nodes.push(crumb(part, parts.slice(0, i + 1).join("/"), i === parts.length - 1));
    });
    crumbs.replaceChildren(...nodes);
    crumbs.scrollLeft = crumbs.scrollWidth;
  }

  function crumb(label, target, current, home) {
    return el("button", {
      type: "button", class: "ui-chip fl-crumb", "aria-current": current ? "page" : false,
      onclick: () => { if (!current || searchResults) goTo(target); },
    }, home ? ui.icon("home") : null, label);
  }

  function sorted(list) {
    const [key, dir] = sortKey.split("-");
    const sign = dir === "desc" ? -1 : 1;
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
    return [...list].sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1; // folders first, always
      let diff = 0;
      if (key === "date") diff = (a.modified || 0) - (b.modified || 0);
      else if (key === "size") diff = (a.size || 0) - (b.size || 0);
      if (diff === 0) return collator.compare(a.name, b.name) * (key === "name" ? sign : 1);
      return diff * sign;
    });
  }

  async function loadList({ silent = false } = {}) {
    if (!root) return;
    const mine = ++listToken;
    renderCrumbs();
    updateChrome();
    updateTools();
    if (searchResults) return;
    if (!silent || !entries) { if (!silent) { entries = null; updateTools(); } listBox.replaceChildren(loadingBox()); }
    const scrollKeep = silent ? browser.scrollTop : 0;
    try {
      const query = new URLSearchParams({ root, path });
      const data = await api(`/api/files?${query}`);
      if (mine !== listToken) return;
      entries = data.entries;
      renderList();
      browser.scrollTop = scrollKeep;
    } catch (error) {
      if (mine !== listToken || error.name === "AbortError") return;
      if (silent && entries) { ctx.toast(error.message); return; } // keep what we already show
      entries = null;
      updateTools();
      const top = path ? ui.button({ label: "Go to top folder", variant: "secondary", size: "sm", onClick: () => goTo("") }) : null;
      const message = !path && /not a folder/i.test(error.message) ? `The "${root}" folder doesn't exist on the laptop yet. Create it there, then retry.` : error.message;
      listBox.replaceChildren(errorBox(message, () => loadList(), top));
    }
  }

  function updateTools() {
    if (!root) return;
    const count = searchResults ? searchResults.length : (entries ? entries.length : null);
    toolInfo.replaceChildren(...[
      count == null ? null : el("span", { class: "fl-count ui-num" }, `${count} item${count === 1 ? "" : "s"}`),
      writable(root) ? null : lockBadge()].filter(Boolean));
    sortBtn.replaceChildren(ui.icon("sliders"), SORTS[sortKey].label);
    const grid = viewMode === "grid";
    viewBtn.replaceChildren(ui.icon(grid ? "list" : "grid"));
    viewBtn.setAttribute("aria-label", grid ? "Switch to list view" : "Switch to grid view");
  }

  // ---- Rows ---------------------------------------------------------------------------------

  function itemFrom(entry, fullPath) {
    return { name: entry.name, is_dir: !!entry.is_dir, size: entry.size, modified: entry.modified, path: fullPath };
  }

  function bindPress(node, item) {
    let timer = null;
    let fired = false;
    let sx = 0;
    let sy = 0;
    const cancel = () => { clearTimeout(timer); timer = null; };
    node.addEventListener("pointerdown", (event) => {
      if (event.button > 0 || event.target.closest(".fl-more")) return;
      fired = false;
      sx = event.clientX;
      sy = event.clientY;
      cancel();
      timer = setTimeout(() => { timer = null; fired = true; ui.haptic("medium"); itemMenu(item); }, LONG_PRESS_MS);
    });
    node.addEventListener("pointermove", (event) => {
      if (timer && Math.hypot(event.clientX - sx, event.clientY - sy) > 10) cancel();
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) node.addEventListener(type, cancel);
    node.addEventListener("contextmenu", (event) => { event.preventDefault(); if (!fired) itemMenu(item); });
    node.addEventListener("click", (event) => {
      if (fired) { fired = false; event.preventDefault(); event.stopImmediatePropagation(); return; }
      if (selecting) { toggleSelect(item); return; }
      openItemNode(item);
    });
  }

  function openItemNode(item) {
    if (item.is_dir) goTo(item.path);
    else openReader(root, item.path, { size: item.size, reset: true });
  }

  function moreButton(item) {
    return ui.button({
      label: `Actions for ${item.name}`, icon: "more", variant: "ghost", size: "sm", iconOnly: true,
      class: "fl-more", onClick: (event) => { event.stopPropagation(); itemMenu(item); },
    });
  }

  function checkMark(item) {
    const mark = el("span", { class: "fl-check", "aria-hidden": "true" }, ui.icon("check"));
    mark.hidden = !selecting;
    return mark;
  }

  function metaFor(item, sub) {
    if (sub) return sub;
    return item.is_dir ? fmtDate(item.modified) : [fmtSize(item.size), fmtDate(item.modified)].filter(Boolean).join("  ·  ");
  }

  function listRowFor(item, sub) {
    const check = checkMark(item);
    const open = el("button", { type: "button", class: "fl-open", "aria-label": item.name },
      check,
      entryTile(item, root, item.path),
      el("span", { class: "fl-main" },
        el("span", { class: "fl-name" }, item.name),
        el("span", { class: "fl-meta" }, metaFor(item, sub))));
    const li = el("li", { class: "fl-row" }, open, moreButton(item));
    bindPress(open, item);
    rowNodes.set(item.path, { node: li, check });
    return li;
  }

  function gridCardFor(item, sub) {
    const check = checkMark(item);
    const open = el("button", { type: "button", class: "fl-open", "aria-label": item.name },
      check,
      entryTile(item, root, item.path, "lg"),
      el("span", { class: "fl-name" }, item.name),
      el("span", { class: "fl-meta" }, metaFor(item, sub)));
    const li = el("li", { class: "fl-gcard" }, open, moreButton(item));
    bindPress(open, item);
    rowNodes.set(item.path, { node: li, check });
    return li;
  }

  function renderItems(items, subFor) {
    rowNodes.clear();
    if (viewMode === "grid") {
      return el("ul", { class: "fl-grid" }, items.map((item) => gridCardFor(item, subFor?.(item))));
    }
    return el("ul", { class: "fl-list ui-list ui-list--inset" }, items.map((item) => listRowFor(item, subFor?.(item))));
  }

  function renderList() {
    updateTools();
    if (!entries.length) {
      rowNodes.clear();
      listBox.replaceChildren(ui.emptyState({
        icon: "folder", title: "This folder is empty",
        body: writable(root) ? "Tap + to add a folder, a note or upload files." : "Nothing here yet.",
        action: writable(root) ? { label: "Add something", icon: "plus", onClick: () => createMenu() } : null,
      }));
      return;
    }
    const items = sorted(entries).map((e) => itemFrom(e, joinPath(path, e.name)));
    const ul = renderItems(items);
    ul.classList.add("ui-stagger");
    setTimeout(() => ul.classList.remove("ui-stagger"), 900);
    listBox.replaceChildren(ul);
    syncSelection();
  }

  // ---- Selection ----------------------------------------------------------------------------

  function syncSelection() {
    listBox.classList.toggle("is-selecting", selecting);
    for (const [p, { node, check }] of rowNodes) {
      const on = selected.has(p);
      node.classList.toggle("is-selected", on);
      check.hidden = !selecting;
      check.classList.toggle("is-on", on);
    }
    updateChrome();
    if (!selecting) return;
    selCount.replaceChildren(el("span", { class: "fl-selnum ui-num" }, String(selected.size)), el("span", { class: "sr-only" }, " selected"));
    const any = selected.size > 0;
    const w = writable(root);
    selBtns.replaceChildren(
      w ? ui.button({ label: "Move", icon: "arrow-right", variant: "tonal", size: "sm", disabled: !any, onClick: () => moveItems([...selected.values()]) }) : null,
      ui.button({ label: "Zip", icon: "download", variant: "tonal", size: "sm", disabled: !any, onClick: () => zipItems([...selected.values()]) }),
      w ? ui.button({ label: "Delete", icon: "trash", variant: "danger-soft", size: "sm", disabled: !any, onClick: () => deleteItems([...selected.values()], true) }) : null,
      ui.button({ label: "Cancel selection", icon: "close", variant: "ghost", size: "sm", iconOnly: true, onClick: exitSelect }));
  }

  function enterSelect(item) {
    selecting = true;
    selectLayer.open();
    selected.clear();
    if (item) selected.set(item.path, item);
    syncSelection();
  }

  function exitSelect() {
    selectLayer.close();
    if (!selecting && !selected.size) return;
    selecting = false;
    selected.clear();
    syncSelection();
  }

  function toggleSelect(item) {
    if (selected.has(item.path)) selected.delete(item.path); else selected.set(item.path, item);
    ui.haptic("select");
    syncSelection();
  }

  // ---- Menus and sheets ---------------------------------------------------------------------

  // A sheet of tappable rows. Each item: {icon, tone, title, sub, meta, danger, onClick}. The action runs after the sheet closes.
  function menuSheet(title, items, { head } = {}) {
    const list = el("div", { class: "ui-list ui-list--inset fl-menu" });
    const s = ui.sheet({ title, content: [head, list].filter(Boolean), class: "fl-menusheet" });
    for (const item of items.filter(Boolean)) {
      const row = ui.listRow({
        icon: item.icon, tone: item.danger ? "bad" : (item.tone || "accent"), title: item.title, sub: item.sub, meta: item.meta,
        onClick: async () => {
          s.close();
          await ui.historySettled();
          item.onClick?.();
        },
      });
      if (item.danger) row.classList.add("is-danger");
      list.append(row);
    }
    list.classList.add("ui-list--icons");
    return s;
  }

  function itemMenu(item) {
    if (Date.now() - lastMenuAt < 700) return;
    lastMenuAt = Date.now();
    const w = writable(root);
    const anyWritable = writableRoots().length > 0;
    const head = el("div", { class: "fl-menuhead" },
      entryTile(item, root, item.path),
      el("div", { class: "fl-menuhead-text" },
        el("div", { class: "fl-menuhead-name" }, item.name),
        el("div", { class: "fl-menuhead-sub" }, `${root}/${item.path}${item.size != null && !item.is_dir ? `  ·  ${fmtSize(item.size)}` : ""}`)),
      w ? null : lockBadge());
    menuSheet(item.is_dir ? "Folder" : "File", [
      { icon: item.is_dir ? "folder" : "eye", title: item.is_dir ? "Open folder" : "Open", onClick: () => openItemNode(item) },
      !item.is_dir && w && isEditable(item.name) ? { icon: "edit", title: "Edit", onClick: () => editFile(root, item.path) } : null,
      w ? { icon: "pen", tone: "info", title: "Rename", onClick: () => renameItem(root, item) } : null,
      w ? { icon: "arrow-right", tone: "info", title: "Move to...", onClick: () => moveItems([item]) } : null,
      anyWritable ? { icon: "copy", tone: "info", title: "Copy to...", onClick: () => copyItems([item]) } : null,
      item.is_dir ? { icon: "download", tone: "good", title: "Download as zip", onClick: () => zipItems([item]) }
        : { icon: "download", tone: "good", title: "Download", onClick: () => triggerDownload(fileUrl(root, item.path, true), item.name) },
      { icon: "check-circle", tone: "violet", title: "Select", sub: "Pick several items to move, zip or delete", onClick: () => enterSelect(item) },
      w ? { icon: "trash", danger: true, title: "Delete", sub: "Moves to Trash. You can undo.", onClick: () => deleteItems([item]) } : null,
    ], { head });
  }

  function folderMenu() {
    const w = writable(root);
    const inbox = roots.find((r) => r.name === "inbox" && r.writable);
    menuSheet(`${root}${path ? `/${path}` : ""}`, [
      { icon: "history", tone: "info", title: "Recent files", sub: "Latest changes across all folders", onClick: () => openPanel("recent") },
      { icon: "trash", tone: "orange", title: "Trash", sub: "Restore deleted files", onClick: () => openPanel("trash") },
      { icon: "chart", tone: "teal", title: "Storage", sub: "Space used per folder", onClick: () => openPanel("storage") },
      { icon: "download", tone: "good", title: path ? "Download this folder as zip" : `Download ${root} as zip`, onClick: () => zipItems([{ name: path ? baseName(path) : root, is_dir: true, path }]) },
      !w && inbox ? { icon: "upload", title: "Upload to inbox", sub: "This folder is read-only", onClick: () => fileInput.click() } : null,
      { icon: "check-circle", tone: "violet", title: "Select items", onClick: () => enterSelect(null) },
    ]);
  }

  function createMenu() {
    if (!writable(root)) return;
    menuSheet("Add to this folder", [
      { icon: "folder", tone: "warn", title: "New folder", onClick: newFolder },
      { icon: "note-plus", tone: "violet", title: "New markdown note", sub: ".md, with live preview", onClick: () => newFile("md") },
      { icon: "file", tone: "info", title: "New text file", sub: ".txt", onClick: () => newFile("txt") },
      { icon: "upload", tone: "good", title: "Upload files", sub: "From this phone", onClick: () => fileInput.click() },
    ]);
  }

  fab.addEventListener("click", createMenu);
  moreBtn.addEventListener("click", folderMenu);

  sortBtn.addEventListener("click", () => {
    menuSheet("Sort by", Object.entries(SORTS).map(([key, s]) => ({
      icon: key.startsWith("name") ? "list" : key.startsWith("date") ? "clock" : "chart", tone: key === sortKey ? "accent" : "gray",
      title: s.long, meta: key === sortKey ? ui.icon("check") : null,
      onClick: () => { sortKey = key; store.set(KEY_SORT, key); if (entries && !searchResults) renderList(); else if (searchResults) renderResults(); updateTools(); },
    })));
  });
  viewBtn.addEventListener("click", () => {
    viewMode = viewMode === "grid" ? "list" : "grid";
    store.set(KEY_VIEW, viewMode);
    updateTools();
    if (searchResults) renderResults(); else if (entries) renderList();
  });

  // A small form in a sheet: one text field. onSubmit(value) throws to show an inline error.
  function promptSheet({ title, label, value = "", confirmLabel, hint, selectStem, onSubmit }) {
    const input = el("input", {
      type: "text", class: "fo-input", value, "aria-label": label, autocomplete: "off", autocapitalize: "off",
      spellcheck: "false", maxlength: "200", enterkeyhint: "done",
    });
    const hintEl = el("div", { class: "ui-hint" }, hint || "");
    const field = el("div", { class: "ui-field fo-field" }, el("label", { class: "ui-label" }, label, input), hintEl);
    let working = false;   // a double-tap (or Enter + tap) must not send the request twice
    const s = ui.sheet({
      title, content: field,
      actions: [{
        label: confirmLabel, variant: "primary", onClick: async () => {
          if (working) return false;
          const text = input.value.trim();
          if (!text) { field.classList.add("is-error"); hintEl.textContent = "Type a name first."; input.focus(); return false; }
          working = true;
          try { await onSubmit(text); } catch (error) {
            field.classList.add("is-error"); hintEl.textContent = error.message; input.focus(); return false;
          } finally { working = false; }
          return undefined;
        },
      }],
    });
    input.addEventListener("input", () => { field.classList.remove("is-error"); hintEl.textContent = hint || ""; });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); s.foot.querySelector(".ui-btn--primary")?.click(); }
    });
    requestAnimationFrame(() => {
      input.focus();
      if (selectStem) {
        const dot = value.lastIndexOf(".");
        input.setSelectionRange(0, dot > 0 ? dot : value.length);
      } else input.select();
    });
    return s;
  }

  // ---- Write operations ---------------------------------------------------------------------

  function refreshAfterChange() {
    loadList({ silent: true });
  }

  function newFolder() {
    const dir = path;
    const r = root;
    promptSheet({
      title: "New folder", label: "Folder name", confirmLabel: "Create", value: "",
      onSubmit: async (name) => {
        await postJson("/api/fileops/mkdir", { root: r, path: joinPath(dir, name) });
        ui.toast(`Created ${name}`, { kind: "good" });
        if (r === root && dir === path) refreshAfterChange();
      },
    });
  }

  function newFile(ext) {
    const dir = path;
    const r = root;
    promptSheet({
      title: ext === "md" ? "New markdown note" : "New text file", label: "File name", confirmLabel: "Create",
      value: ext === "md" ? "untitled.md" : "untitled.txt", selectStem: true,
      onSubmit: async (typed) => {
        const name = typed.includes(".") ? typed : `${typed}.${ext}`;
        const target = joinPath(dir, name);
        await postJson("/api/fileops/write", { root: r, path: target, content: "", create_only: true });
        if (r === root && dir === path) refreshAfterChange();
        await ui.historySettled();
        editFile(r, target);
      },
    });
  }

  function renameItem(r, item, after) {
    promptSheet({
      title: item.is_dir ? "Rename folder" : "Rename file", label: "New name", confirmLabel: "Rename",
      value: item.name, selectStem: !item.is_dir,
      onSubmit: async (newName) => {
        if (newName === item.name) return;
        const result = await postJson("/api/fileops/rename", { root: r, path: item.path, new_name: newName });
        ui.toast(`Renamed to ${newName}`, { kind: "good" });
        refreshAfterChange();
        after?.(result.path || joinPath(parentOf(item.path), newName));
      },
    });
  }

  // Destination picker. Resolves {root, path} or null.
  function pickFolder({ title, confirmLabel, exclude = [] }) {
    return new Promise((resolve) => {
      const targets = writableRoots();
      let pr = writable(root) ? root : targets[0].name;
      let pp = writable(root) ? path : "";
      let result = null;
      const chips = el("div", { class: "fo-pickroots ui-hscroll" });
      const crumbRow = el("div", { class: "fo-pickcrumbs" });
      const listEl = el("div", { class: "fo-picklist" });
      const newRow = el("div", { class: "fo-newrow", hidden: true });
      const newBtn = ui.button({ label: "New folder", icon: "plus", variant: "ghost", size: "sm" });
      const where = el("div", { class: "fo-where" });
      const blocked = (p) => exclude.some((x) => p === x || p.startsWith(`${x}/`));

      function renderChips() {
        chips.replaceChildren(...targets.map((r) => el("button", {
          type: "button", class: "ui-chip", "aria-pressed": String(r.name === pr),
          onclick: () => { pr = r.name; pp = ""; refresh(); },
        }, ui.icon("folder"), r.name)));
      }

      async function refresh() {
        renderChips();
        const parts = pp ? pp.split("/") : [];
        crumbRow.replaceChildren(
          el("button", { type: "button", class: "fo-pcrumb", onclick: () => { pp = ""; refresh(); } }, pr),
          ...parts.flatMap((part, i) => [
            el("span", { class: "fo-psep", "aria-hidden": "true" }, "/"),
            el("button", { type: "button", class: "fo-pcrumb", onclick: () => { pp = parts.slice(0, i + 1).join("/"); refresh(); } }, part),
          ]));
        where.textContent = `${confirmLabel.replace(/ here$/, "")} to: ${pr}/${pp}`.replace(/\/$/, "");
        listEl.replaceChildren(ui.skeleton("list", 3));
        try {
          const data = await api(`/api/files?${new URLSearchParams({ root: pr, path: pp })}`);
          const dirs = data.entries.filter((e) => e.is_dir && !(pr === root && blocked(joinPath(pp, e.name))));
          listEl.replaceChildren(dirs.length
            ? el("ul", { class: "ui-list ui-list--inset ui-list--icons" }, dirs.map((e) => el("li", {},
              ui.listRow({
                icon: "folder", tone: "warn", title: e.name, chevron: true,
                onClick: () => { pp = joinPath(pp, e.name); refresh(); },
              }))))
            : el("p", { class: "fo-pickempty" }, "No folders in here. You can still use this one."));
        } catch (error) {
          listEl.replaceChildren(ui.notice(error.message, { tone: "bad" }));
        }
      }

      newBtn.addEventListener("click", () => {
        newRow.hidden = false;
        const input = el("input", { type: "text", class: "fo-input", placeholder: "Folder name", "aria-label": "New folder name", maxlength: "200" });
        const create = ui.button({ label: "Create", variant: "primary", size: "sm" });
        const err = el("div", { class: "ui-hint fo-newerr" });
        const go = async () => {
          const name = input.value.trim();
          if (!name) return;
          try {
            await postJson("/api/fileops/mkdir", { root: pr, path: joinPath(pp, name) });
            pp = joinPath(pp, name);
            newRow.hidden = true;
            refresh();
          } catch (error) { err.textContent = error.message; }
        };
        create.addEventListener("click", go);
        input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); go(); } });
        newRow.replaceChildren(el("div", { class: "fo-newfields" }, input, create), err);
        input.focus();
      });

      ui.sheet({
        title, class: "fo-picksheet",
        content: [chips, crumbRow, el("div", { class: "fo-pickhead" }, where, newBtn), newRow, listEl],
        actions: [{ label: confirmLabel, variant: "primary", icon: "check", onClick: () => { result = { root: pr, path: pp }; } }],
        onClose: () => resolve(result),
      });
      refresh();
    });
  }

  async function moveItems(items) {
    if (!items.length) return;
    const from = root;
    const dest = await pickFolder({
      title: items.length === 1 ? `Move “${items[0].name}”` : `Move ${items.length} items`,
      confirmLabel: "Move here", exclude: items.filter((i) => i.is_dir).map((i) => i.path),
    });
    if (!dest) return;
    let done = 0;
    let failure = null;
    for (const item of items) {
      try {
        await postJson("/api/fileops/move", { root: from, path: item.path, dest_root: dest.root, dest_dir: dest.path });
        done += 1;
      } catch (error) { failure = error; }
    }
    if (failure) ui.toast(done ? `Moved ${done}, but: ${failure.message}` : failure.message, { kind: "bad" });
    else ui.toast(`Moved ${done} item${done === 1 ? "" : "s"} to ${dest.root}${dest.path ? `/${dest.path}` : ""}`, { kind: "good" });
    exitSelect();
    refreshAfterChange();
    if (readerOpen && doc && items.some((i) => i.path === doc.path)) closeReader();
  }

  async function copyItems(items) {
    if (!items.length) return;
    const dest = await pickFolder({
      title: items.length === 1 ? `Copy “${items[0].name}”` : `Copy ${items.length} items`, confirmLabel: "Copy here",
    });
    if (!dest) return;
    let done = 0;
    let failure = null;
    for (const item of items) {
      try {
        await postJson("/api/fileops/copy", { root, path: item.path, dest_root: dest.root, dest_dir: dest.path });
        done += 1;
      } catch (error) { failure = error; }
    }
    if (failure) ui.toast(done ? `Copied ${done}, but: ${failure.message}` : failure.message, { kind: "bad" });
    else ui.toast(`Copied to ${dest.root}${dest.path ? `/${dest.path}` : ""}`, { kind: "good" });
    exitSelect();
    refreshAfterChange();
  }

  async function deleteItems(items, confirmFirst = false) {
    if (!items.length) return;
    const r = root;
    if (confirmFirst || items.length > 1) {
      const yes = await ui.confirm({
        title: items.length === 1 ? `Delete “${items[0].name}”?` : `Delete ${items.length} items?`,
        message: "They go to Trash, where you can restore them.", confirmLabel: "Move to Trash", danger: true, icon: "trash",
      });
      if (!yes) return;
    }
    const ids = [];
    let failure = null;
    for (const item of items) {
      try {
        const res = await postJson("/api/fileops/delete", { root: r, path: item.path, confirm: true });
        ids.push(res.id);
      } catch (error) { failure = error; }
    }
    exitSelect();
    refreshAfterChange();
    if (readerOpen && doc && items.some((i) => i.path === doc.path)) closeReader();
    if (failure) ui.toast(ids.length ? `Deleted ${ids.length}, but: ${failure.message}` : failure.message, { kind: "bad" });
    if (!ids.length) return;
    ui.toast(ids.length === 1 ? `Moved “${items[0].name}” to Trash` : `Moved ${ids.length} items to Trash`, {
      kind: "good", ms: 8000, icon: "trash",
      action: {
        label: "Undo", onClick: async () => {
          let back = 0;
          for (const id of ids) {
            try { await postJson("/api/fileops/trash/restore", { id }); back += 1; } catch (error) { ui.toast(error.message, { kind: "bad" }); }
          }
          if (back) ui.toast(`Restored ${back} item${back === 1 ? "" : "s"}`, { kind: "good" });
          refreshAfterChange();
        },
      },
    });
  }

  function zipItems(items) {
    items.forEach((item, i) => {
      const url = item.is_dir ? `/api/fileops/zip?${new URLSearchParams({ root, path: item.path })}` : fileUrl(root, item.path, true);
      setTimeout(() => triggerDownload(url, item.is_dir ? `${item.name}.zip` : item.name), i * 350);
    });
    ui.toast(items.length === 1 ? "Download started" : `Downloading ${items.length} items, one by one`, { kind: "good" });
    exitSelect();
  }

  // ---- Upload -------------------------------------------------------------------------------

  function uploadTarget() {
    if (root && writable(root)) return { root, dir: path };
    const inbox = roots.find((r) => r.name === "inbox" && r.writable) || writableRoots()[0];
    return inbox ? { root: inbox.name, dir: "" } : null;
  }

  function uploadOne(file, target, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/fileops/upload");
      xhr.setRequestHeader("X-Hub", "1");
      xhr.withCredentials = true;
      xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
      xhr.onerror = () => reject(new Error(LAPTOP_DOWN));
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
        if (xhr.status === 401) window.dispatchEvent(new Event("hub:unauthorized"));
        if (xhr.status >= 200 && xhr.status < 300) resolve(body || {});
        else reject(new Error(typeof body?.detail === "string" ? body.detail : `The laptop answered with an error (${xhr.status}).`));
      };
      const form = new FormData();
      form.append("root", target.root);
      form.append("dir", target.dir);
      form.append("file", file, file.name);
      xhr.send(form);
    });
  }

  async function startUploads(files) {
    const target = uploadTarget();
    if (!target) { ui.toast("No folder here accepts uploads.", { kind: "warn" }); return; }
    const rows = files.map((file) => {
      const bar = ui.progressBar(0, `Uploading ${file.name}`);
      const status = el("span", { class: "fo-up-status" }, "Waiting");
      const node = el("li", { class: "fo-up" },
        ui.tile(kindOf(file.name) === "image" ? "image" : "file", TONES[kindOf(file.name)], "sm"),
        el("div", { class: "fo-up-body" },
          el("div", { class: "fo-up-line" }, el("span", { class: "fo-up-name" }, file.name), status),
          bar));
      return { file, bar, status, node };
    });
    const where = `${target.root}${target.dir ? `/${target.dir}` : ""}`;
    const s = ui.sheet({
      title: `Uploading to ${where}`, class: "fo-upsheet",
      content: el("ul", { class: "fo-uplist" }, rows.map((r) => r.node)),
    });
    let ok = 0;
    let failed = 0;
    for (const row of rows) {
      if (row.file.size > MAX_UPLOAD) {
        row.status.textContent = "Too big (100 MB max)";
        row.node.classList.add("is-bad");
        failed += 1;
        continue;
      }
      row.status.textContent = "0%";
      try {
        const res = await uploadOne(row.file, target, (v) => {
          row.bar.set(v);
          row.status.textContent = `${Math.round(v * 100)}%`;
        });
        row.bar.set(1);
        row.status.textContent = res.name && res.name !== row.file.name ? `Saved as ${res.name}` : "Done";
        row.node.classList.add("is-done");
        ok += 1;
      } catch (error) {
        row.status.textContent = error.message;
        row.node.classList.add("is-bad");
        failed += 1;
      }
    }
    s.setActions([{ label: "Done", variant: "primary" }]);
    if (ok) ui.haptic("success");
    ui.toast(failed ? `${ok} uploaded, ${failed} failed` : `Uploaded ${ok} file${ok === 1 ? "" : "s"} to ${where}`, { kind: failed ? "warn" : "good" });
    if (target.root === root && target.dir === path) refreshAfterChange();
  }

  fileInput.addEventListener("change", () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    if (files.length) startUploads(files);
  });

  // ---- Trash, recent, storage ---------------------------------------------------------------

  function openPanel(kind) {
    exitSelect();
    panelKind = kind;
    updateChrome();
    panelAction.replaceChildren();
    const token = ++panelToken;
    panelBody.replaceChildren(ui.skeleton("list", 5));
    panelBody.scrollTop = 0;
    if (kind === "trash") { panelTitle.textContent = "Trash"; loadTrash(token); }
    else if (kind === "recent") { panelTitle.textContent = "Recent files"; loadRecent(token); }
    else { panelTitle.textContent = "Storage"; loadStorage(token); }
  }

  function closePanel() {
    if (!panelKind) return;
    panelKind = null;
    panelToken++;
    updateChrome();
    browser.scrollTop = browserScroll;
    if (root && entries) loadList({ silent: true });
  }

  panelBack.addEventListener("click", closePanel);

  function panelError(error, token, retry) {
    if (token !== panelToken) return;
    panelBody.replaceChildren(errorBox(error.message, retry));
  }

  async function loadTrash(token) {
    try {
      const data = await api("/api/fileops/trash");
      if (token !== panelToken) return;
      renderTrash(data.items || []);
    } catch (error) { panelError(error, token, () => openPanel("trash")); }
  }

  function renderTrash(items) {
    panelAction.replaceChildren();
    if (!items.length) {
      panelBody.replaceChildren(ui.emptyState({ icon: "trash", title: "Trash is empty", body: "Deleted files and folders wait here until you empty it." }));
      return;
    }
    const total = items.reduce((n, i) => n + (i.size || 0), 0);
    panelAction.replaceChildren(ui.button({
      label: "Empty", icon: "trash", variant: "danger-soft", size: "sm", onClick: async () => {
        const yes = await ui.confirm({
          title: "Empty the Trash?", message: `This permanently deletes ${items.length} item${items.length === 1 ? "" : "s"} (${fmtSize(total)}). It can't be undone.`,
          confirmLabel: "Empty Trash", danger: true, icon: "trash",
        });
        if (!yes) return;
        try {
          const res = await postJson("/api/fileops/trash/empty", { confirm: true });
          ui.toast(`Deleted ${res.removed ?? items.length} item${(res.removed ?? items.length) === 1 ? "" : "s"} for good`, { kind: "good" });
          renderTrash([]);
        } catch (error) { ui.toast(error.message, { kind: "bad" }); }
      },
    }));
    const list = el("ul", { class: "ui-list ui-list--inset ui-list--icons fo-trashlist" });
    for (const item of items) {
      const canRestore = writable(item.root);
      const restore = ui.button({
        label: "Restore", icon: "history", variant: "tonal", size: "sm", disabled: !canRestore,
        onClick: async () => {
          try {
            const res = await ui.busy(restore, postJson("/api/fileops/trash/restore", { id: item.id }));
            ui.toast(`Restored to ${res.root}/${res.path}`, { kind: "good" });
            row.remove();
            if (!list.children.length) renderTrash([]);
          } catch (error) { ui.toast(error.message, { kind: "bad" }); }
        },
      });
      const row = el("li", { class: "fo-trashrow" },
        entryTile({ name: item.name, is_dir: item.is_dir }, null, ""),
        el("div", { class: "fo-trashbody" },
          el("div", { class: "fo-trashname" }, item.name),
          el("div", { class: "fo-trashsub" }, `from ${item.root}/${item.path}`),
          el("div", { class: "fo-trashsub" }, [fmtDate(item.deleted_at), item.is_dir ? "" : fmtSize(item.size)].filter(Boolean).join("  ·  "))),
        canRestore ? restore : lockBadge("Locked"));
      list.append(row);
    }
    panelBody.replaceChildren(
      el("p", { class: "fo-panelnote" }, `${items.length} item${items.length === 1 ? "" : "s"} · ${fmtSize(total)}`), list);
  }

  async function loadRecent(token) {
    try {
      const data = await api("/api/fileops/recent?limit=40");
      if (token !== panelToken) return;
      const files = data.files || [];
      if (!files.length) {
        panelBody.replaceChildren(ui.emptyState({ icon: "history", title: "No recent files", body: "Files you change show up here." }));
        return;
      }
      const list = el("ul", { class: "ui-list ui-list--inset ui-list--icons fo-recentlist" }, files.map((f) => el("li", {},
        el("button", { type: "button", class: "ui-row ui-row--interactive fo-recentrow", onclick: () => handle.openFile(f.root, f.path) },
          entryTile({ name: f.name, is_dir: false }, f.root, f.path),
          el("span", { class: "ui-row__body" },
            el("span", { class: "ui-row__title" }, f.name),
            el("span", { class: "ui-row__sub" }, `${f.root}/${parentOf(f.path) || ""}`.replace(/\/$/, ""))),
          el("span", { class: "fo-recentmeta" },
            el("span", {}, fmtDate(f.modified)),
            el("span", {}, f.writable ? fmtSize(f.size) : el("span", { class: "fo-lockmini" }, ui.icon("lock"), fmtSize(f.size))))))));
      panelBody.replaceChildren(list);
    } catch (error) { panelError(error, token, () => openPanel("recent")); }
  }

  async function loadStorage(token) {
    try {
      const data = await api("/api/fileops/stats");
      if (token !== panelToken) return;
      const cards = (data.roots || []).map((r) => {
        const disk = r.disk || {};
        const frac = disk.total ? disk.used / disk.total : 0;
        const bar = ui.progressBar(frac, `Disk used for ${r.name}`);
        return el("section", { class: "ui-card ui-card--flat fo-stat" },
          el("div", { class: "fo-stathead" },
            ui.tile(r.writable ? "folder" : "lock", r.writable ? "accent" : "warn", "sm"),
            el("div", { class: "fo-statname" }, r.name),
            r.writable ? ui.badge("Writable", "good") : lockBadge()),
          el("div", { class: "fo-stattotal ui-num" }, fmtSize(r.bytes), r.truncated ? el("span", { class: "fo-approx" }, " or more") : null),
          el("div", { class: "fo-statgrid" },
            statCell(r.files.toLocaleString(), "files"), statCell(r.folders.toLocaleString(), "folders"), statCell(fmtSize(r.trash_bytes || 0), "in Trash")),
          disk.total ? el("div", { class: "fo-disk" }, bar,
            el("div", { class: "fo-disktext" }, `Disk: ${fmtSize(disk.free)} free of ${fmtSize(disk.total)}`)) : null);
      });
      panelBody.replaceChildren(...(cards.length ? cards : [ui.emptyState({ icon: "chart", title: "No numbers yet" })]));
    } catch (error) { panelError(error, token, () => openPanel("storage")); }
  }

  function statCell(value, label) {
    return el("div", { class: "fo-cell" }, el("div", { class: "fo-cellv ui-num" }, value), el("div", { class: "fo-celll" }, label));
  }

  // ---- Search ------------------------------------------------------------------------------

  function clearSearch(reload = true) {
    searchResults = null;
    searchLayer.close();
    searchQuery = "";
    searchInput.value = "";
    if (reload) loadList();
  }

  async function runSearch(query) {
    const mine = ++listToken;
    exitSelect();
    searchQuery = query;
    searchResults = [];
    searchLayer.open();
    updateChrome();
    listBox.replaceChildren(loadingBox());
    try {
      const params = new URLSearchParams({ root, q: query });
      if (path) params.set("path", path);
      const data = await api(`/api/search?${params}`);
      if (mine !== listToken) return;
      searchResults = data.results;
      renderResults();
    } catch (error) {
      if (mine !== listToken) return;
      listBox.replaceChildren(errorBox(error.message, () => runSearch(query),
        ui.button({ label: "Clear search", variant: "secondary", size: "sm", onClick: () => clearSearch() })));
    }
  }

  function renderResults() {
    updateTools();
    const where = path ? `${root}/${path}` : root;
    const head = el("div", { class: "fl-resulthead" },
      el("span", { class: "grow" },
        `${searchResults.length ? searchResults.length : "No"} result${searchResults.length === 1 ? "" : "s"} for “${searchQuery}” in ${where}`),
      ui.button({ label: "Clear", variant: "ghost", size: "sm", onClick: () => clearSearch() }));
    if (!searchResults.length) {
      rowNodes.clear();
      listBox.replaceChildren(head, ui.emptyState({ icon: "search", title: "Nothing matched", body: "Try fewer letters.", compact: true }));
      return;
    }
    const items = sorted(searchResults.map((result) => ({
      name: result.name, is_dir: result.is_dir, size: undefined, modified: undefined, path: result.path,
    })));
    listBox.replaceChildren(head, renderItems(items, (item) => item.path));
    syncSelection();
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = searchInput.value.trim();
    if (!root) return;
    if (!query) { if (searchResults) clearSearch(); return; }
    searchInput.blur();
    runSearch(query);
  });
  searchInput.addEventListener("input", () => {
    if (!searchInput.value && searchResults) clearSearch();
  });

  // ---- Reader ------------------------------------------------------------------------------

  function setReaderVisible(visible) {
    readerOpen = visible;
    reader.hidden = !visible;
    updateChrome();
  }

  async function openReader(r, p, { size, reset = false, push = true, restoreScroll = 0 } = {}) {
    const previous = doc;
    if (panelKind) { panelKind = null; panelToken++; }
    if (!readerOpen) {
      browserScroll = browser.scrollTop;
      setReaderVisible(true);
      attachSelection();
    } else if (push && !reset && previous) {
      stack.push({ root: previous.root, path: previous.path, size: previous.size, scroll: scroller.scrollTop });
    }
    if (reset) stack.length = 0;
    closeSheet();
    lastSelection = "";
    askForm.hidden = true;
    docAbort?.abort();
    docAbort = new AbortController();
    const mine = ++docToken;
    doc = { root: r, path: p, size, text: null };
    titleEl.textContent = baseName(p);
    subEl.textContent = `${r}/${p}`;
    docEl.className = "fl-doc";
    docEl.replaceChildren(ui.skeleton("lines", 5));
    bar.hidden = true;
    editBtn.hidden = !(writableNow(r) && isEditable(p));
    scroller.scrollTop = 0;
    backBtn.focus({ preventScroll: true });

    if (size === undefined) size = await lookupSize(r, p);
    if (mine !== docToken) return;
    doc.size = size;
    if (size != null) subEl.textContent = `${r}/${p}  ·  ${fmtSize(size)}`;

    const kind = kindOf(p);
    try {
      if (size != null && size > MAX_INLINE && kind !== "pdf") {
        showCard(`This file is ${fmtSize(size)}, too big to show here.`);
      } else if (kind === "image") {
        showImage();
      } else if (kind === "pdf") {
        if (inApp()) await showPdfText(mine);
        else showCard("PDF files open in your browser's viewer.");
      } else {
        const result = await fetchText(fileUrl(r, p), docAbort.signal);
        if (mine !== docToken) return;
        if (result.tooBig) showCard("This file is too big to show here.");
        else if (result.binary) showCard("This kind of file can't be shown here.");
        else if (kind === "markdown") showMarkdown(result.text);
        else showText(result.text, extOf(p));
      }
    } catch (error) {
      if (error.name === "AbortError" || mine !== docToken) return;
      docEl.replaceChildren(errorBox(error.message, () => openReader(r, p, { size, restoreScroll, push: false })));
      return;
    }
    scroller.scrollTop = restoreScroll;
  }

  function writableNow(r) { return writable(r); }

  // The folder listing usually knows the size; links and other tabs do not.
  async function lookupSize(r, p) {
    if (r === root && entries && parentOf(p) === path) {
      const hit = entries.find((e) => e.name === baseName(p));
      if (hit) return hit.size;
    }
    const hit = await lookupEntry(r, p);
    return hit ? hit.size : null;
  }

  async function lookupEntry(r, p) {
    try {
      const query = new URLSearchParams({ root: r, path: parentOf(p) });
      const data = await api(`/api/files?${query}`);
      return data.entries.find((e) => e.name === baseName(p)) || null;
    } catch (_) {
      return null;
    }
  }

  function actionButtons() {
    const { root: r, path: p } = doc;
    const dl = el("button", { type: "button", class: "ui-btn ui-btn--secondary" }, ui.icon("download"), "Download");
    dl.addEventListener("click", () => triggerDownload(fileUrl(r, p, true), baseName(p)));
    if (inApp()) {
      const open = el("button", { type: "button", class: "ui-btn ui-btn--primary" }, ui.icon("external"), "Open with\u2026");
      open.addEventListener("click", () => openExternally(r, p));
      return el("div", { class: "fl-cardbtns" }, open, dl);
    }
    return el("div", { class: "fl-cardbtns" },
      el("a", { class: "ui-btn ui-btn--primary", href: fileUrl(r, p), target: "_blank", rel: "noopener noreferrer" }, ui.icon("external"), "Open"), dl);
  }

  function showCard(message) {
    docEl.replaceChildren(el("div", { class: "fl-card" },
      entryTile({ name: baseName(doc.path), is_dir: false }, null, "", "lg"),
      el("p", { class: "fl-cardname" }, baseName(doc.path)),
      el("p", { class: "fl-cardmsg" }, message),
      actionButtons()));
  }

  // In the app a PDF cannot open in a tab: show its extracted text (from the Ask-my-files index) plus Download / Open with.
  async function showPdfText(mine) {
    showCard("Reading the text of this PDF\u2026");
    const card = docEl.querySelector(".fl-card");
    const msg = card.querySelector(".fl-cardmsg");
    let chunks = null;
    try {
      const data = await api(`/api/kb/file-context?${new URLSearchParams({ path: `${doc.root}:${doc.path}` })}`);
      chunks = data.chunks;
    } catch (error) {
      if (mine !== docToken) return;
      msg.textContent = "No text preview available. Use Download, then Android opens the PDF.";
      return;
    }
    if (mine !== docToken) return;
    const text = (chunks || []).map((c) => (c.heading ? c.heading + "\n" : "") + c.text).join("\n\n").trim();
    if (!text) { msg.textContent = "This PDF has no readable text. Use Download to open it in another app."; return; }
    msg.textContent = "Text preview (download to see the PDF with its layout and images).";
    card.after(el("pre", { class: "fl-pdftext" }, text));
  }

  function showImage() {
    docEl.classList.add("is-image");
    const img = el("img", { class: "fl-img", alt: baseName(doc.path), src: fileUrl(doc.root, doc.path) });
    img.addEventListener("error", () => {
      docEl.replaceChildren(errorBox("That image could not be loaded.", () => openReader(doc.root, doc.path, { size: doc.size, push: false })));
    });
    docEl.replaceChildren(img);
  }

  function showText(text, ext) {
    doc.text = text;
    docEl.className = "fl-doc";
    const pre = el("pre", { class: WRAPPED_TEXT.has(ext) || !TEXT.has(ext) ? "fl-pre wrap" : "fl-pre" });
    pre.textContent = text;
    docEl.replaceChildren(text ? pre : ui.emptyState({ icon: "file", title: "This file is empty", compact: true }));
    updateBar();
  }

  function showMarkdown(text) {
    doc.text = text;
    docEl.className = "fl-doc markdown";
    // Parse in an inert document so images do not load from the wrong address before rewriting.
    const parsed = new DOMParser().parseFromString(renderMarkdown(text), "text/html");
    for (const img of parsed.body.querySelectorAll("img")) {
      const src = img.getAttribute("src") || "";
      if (src && !SCHEME.test(src) && !src.startsWith("//")) {
        const resolved = resolvePath(doc.path, src);
        if (resolved) img.setAttribute("src", fileUrl(doc.root, resolved));
        else img.removeAttribute("src");
      }
      img.setAttribute("loading", "lazy");
    }
    for (const a of parsed.body.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href") || "";
      if (href.startsWith("#")) continue;
      if (/^https?:/i.test(href) || href.startsWith("//")) {
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener noreferrer");
      } else if (SCHEME.test(href)) {
        if (!/^(mailto|tel):/i.test(href)) a.removeAttribute("href");
      } else {
        const resolved = resolvePath(doc.path, href);
        if (resolved === null) {
          a.removeAttribute("href");
        } else {
          a.setAttribute("href", fileUrl(doc.root, resolved));
          a.dataset.hubPath = resolved;
          a.dataset.hubDir = href.split("#")[0].split("?")[0].endsWith("/") ? "1" : "";
        }
      }
    }
    docEl.replaceChildren(...parsed.body.childNodes);
    for (const img of docEl.querySelectorAll("img")) {
      img.addEventListener("error", () => {
        img.replaceWith(el("span", { class: "fl-brokenimg" }, `[image: ${img.alt || "not found"}]`));
      });
    }
    updateBar();
  }

  docEl.addEventListener("click", (event) => {
    const a = event.target.closest?.("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (href.startsWith("#")) {
      event.preventDefault();
      const id = decodeURIComponent(href.slice(1));
      const target = id && docEl.querySelector(`[id="${CSS.escape(id)}"]`);
      target?.scrollIntoView({ block: "start" });
      return;
    }
    const hubPath = a.dataset.hubPath;
    if (hubPath === undefined) return; // external link: default behaviour
    event.preventDefault();
    if (a.dataset.hubDir) {
      closeReader();
      goTo(hubPath);
    } else {
      openReader(doc.root, hubPath);
    }
  });

  function closeReader() {
    docAbort?.abort();
    docToken++;
    closeSheet();
    detachSelection();
    stack.length = 0;
    setReaderVisible(false);
    doc = null;
    docEl.replaceChildren();
    browser.scrollTop = browserScroll;
  }

  backBtn.addEventListener("click", () => {
    const previous = stack.pop();
    if (previous) {
      openReader(previous.root, previous.path, { size: previous.size, restoreScroll: previous.scroll, push: false });
    } else {
      closeReader();
    }
  });

  // Edit and the file menu in the reader header
  function editFile(r, p) {
    openEditor({
      root: r, path: p,
      onSaved: () => { if (r === root) loadList({ silent: true }); },
      onClose: () => {
        if (readerOpen && doc && doc.root === r && doc.path === p) openReader(r, p, { push: false, restoreScroll: scroller.scrollTop });
        if (r === root) loadList({ silent: true });
      },
    });
  }
  editBtn.addEventListener("click", () => { if (doc) editFile(doc.root, doc.path); });

  readerMore.addEventListener("click", () => {
    if (!doc) return;
    const item = { name: baseName(doc.path), is_dir: false, size: doc.size, path: doc.path };
    const r = doc.root;
    const w = writable(r);
    menuSheet("File", [
      w && isEditable(doc.path) ? { icon: "edit", title: "Edit", onClick: () => editFile(r, doc.path) } : null,
      { icon: "external", tone: "info", title: "Open in browser", onClick: () => openExternally(r, doc.path) },
      { icon: "download", tone: "good", title: "Download", onClick: () => triggerDownload(fileUrl(r, doc.path, true), item.name) },
      w ? { icon: "pen", tone: "info", title: "Rename", onClick: () => renameItem(r, item, (newPath) => openReader(r, newPath, { reset: true })) } : null,
      writableRoots().length ? { icon: "copy", tone: "info", title: "Copy to...", onClick: () => copyItems([item]) } : null,
      w ? { icon: "arrow-right", tone: "info", title: "Move to...", onClick: () => moveItems([item]) } : null,
      w ? { icon: "trash", danger: true, title: "Delete", sub: "Moves to Trash. You can undo.", onClick: () => deleteItems([item]) } : null,
    ], {
      head: w ? null : el("div", { class: "fl-menuhead" }, lockBadge("Read-only folder"), el("span", { class: "fl-menuhead-sub" }, "Editing is off here.")),
    });
  });

  // ---- Selection tracking -----------------------------------------------------------------

  function insideReader(node) {
    return !!node && docEl.contains(node);
  }

  function onSelectionChange() {
    if (!readerOpen) return;
    const selection = document.getSelection();
    const text = selection && !selection.isCollapsed && insideReader(selection.anchorNode)
      && insideReader(selection.focusNode) ? selection.toString().trim() : "";
    if (text) {
      lastSelection = text;
    } else if (!selection || selection.isCollapsed || !selection.toString().trim()) {
      // Tapping the bar or sheet clears the selection on Android; that must not forget it.
      const recent = Date.now() - panelTouchedAt < 800;
      const typing = bar.contains(document.activeElement) || !!answerSheet?.el.contains(document.activeElement);
      if (!recent && !typing) lastSelection = "";
    }
    updateBar();
  }

  function attachSelection() {
    if (listening) return;
    document.addEventListener("selectionchange", onSelectionChange);
    listening = true;
  }

  function detachSelection() {
    if (!listening) return;
    document.removeEventListener("selectionchange", onSelectionChange);
    listening = false;
  }

  function currentText() {
    if (lastSelection) return { text: lastSelection.slice(0, MAX_ASK), scope: "Selected text" };
    return { text: (doc?.text || "").slice(0, MAX_ASK), scope: "Whole page" };
  }

  function updateBar() {
    const hasText = !!(doc && doc.text);
    bar.hidden = !hasText;
    scopeLabel.textContent = currentText().scope;
    scopeLabel.classList.toggle("on", !!lastSelection);
    clearSel.hidden = !lastSelection;
    if (provSeg.value !== provider) provSeg.value = provider;
  }

  // ---- AI help -----------------------------------------------------------------------------

  clearSel.addEventListener("click", () => {
    lastSelection = "";
    try { document.getSelection()?.removeAllRanges(); } catch (_) { /* ignore */ }
    updateBar();
  });
  // A tap in the document with nothing selected means "no selection any more".
  scroller.addEventListener("pointerup", () => {
    setTimeout(() => {
      const selection = document.getSelection();
      if (lastSelection && (!selection || selection.isCollapsed)) { lastSelection = ""; updateBar(); }
    }, 60);
  });

  const touchPanel = () => { panelTouchedAt = Date.now(); };
  bar.addEventListener("pointerdown", touchPanel, true);
  // Keep the selection alive while a button is pressed (the text input still needs focus).
  for (const button of [...aiButtons, clearSel]) {
    button.addEventListener("pointerdown", (event) => event.preventDefault());
  }

  const TITLES = { explain: "Explain", summarise: "Summary", quiz: "Quiz", custom: "Answer" };

  for (const button of aiButtons) {
    button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      if (mode === "custom") {
        askForm.hidden = !askForm.hidden;
        if (!askForm.hidden) askInput.focus();
        return;
      }
      askForm.hidden = true;
      runAsk(mode, "");
    });
  }

  askForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = askInput.value.trim();
    if (!question) { askInput.focus(); return; }
    askInput.blur();
    askForm.hidden = true;
    runAsk("custom", question);
    askInput.value = "";
  });

  function renderAnswer() {
    renderTimer = null;
    if (!answerSheet || !answerEl) return;
    const body = answerSheet.body;
    const follow = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    answerEl.innerHTML = answer ? renderMarkdown(answer) : "";
    for (const a of answerEl.querySelectorAll("a[href]")) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
    if (follow) body.scrollTop = body.scrollHeight;
  }

  function queueRender() {
    if (renderTimer === null) renderTimer = setTimeout(renderAnswer, RENDER_EVERY_MS);
  }

  function setRunning(running) {
    if (!answerSheet) return;
    if (running) {
      answerSheet.setActions([{ label: "Stop", icon: "stop", variant: "danger-soft", onClick: () => { askAbort?.abort(); return false; } }]);
      return;
    }
    const sheetRef = answerSheet;
    answerSheet.setActions([
      answer ? { label: "Copy", icon: "copy", variant: "secondary", onClick: async () => {
        ctx.toast((await copyText(answer)) ? "Copied" : "Could not copy. Select the text and copy it by hand.");
        return false;
      } } : null,
      answer ? { label: "Save to notes", icon: "note-plus", variant: "secondary", onClick: async () => {
        try {
          await api("/api/notes", { method: "POST", json: { text: answer, source: "ai" } });
          ctx.toast("Saved");
        } catch (error) { ctx.toast(error.message); }
        return false;
      } } : null,
      { label: "Close", variant: "primary" },
    ].filter(Boolean));
    return sheetRef;
  }

  function showSheetError(message) {
    sheetError.textContent = message;
    sheetError.hidden = false;
  }

  function openAnswerSheet(title) {
    if (answerSheet) { answerSheet.setTitle(title); return; }
    answerEl = el("div", { class: "markdown fl-answer" });
    sheetStatus = el("div", { class: "fl-status", role: "status" });
    sheetError = el("div", { class: "ui-notice ui-notice--bad fl-sheet-error", role: "alert", hidden: true });
    answerSheet = ui.sheet({
      title, class: "fl-answersheet", content: [sheetStatus, answerEl, sheetError],
      onClose: () => {
        askAbort?.abort();
        askAbort = null;
        clearTimeout(renderTimer);
        renderTimer = null;
        answerSheet = null;
      },
    });
  }

  async function runAsk(mode, question) {
    const { text, scope } = currentText();
    if (!text) { ctx.toast("There is no text to send."); return; }
    askAbort?.abort();
    const controller = new AbortController();
    askAbort = controller;
    answer = "";
    clearTimeout(renderTimer);
    renderTimer = null;
    openAnswerSheet(question ? "Your question" : TITLES[mode]);
    answerEl.replaceChildren();
    sheetError.hidden = true;
    sheetStatus.textContent = `${scope} · ${provider === "local" ? "Local model" : providerLabel(provider, true)}`;
    setRunning(true);
    let got = false;
    try {
      for await (const event of sse("/api/ask", { mode, text, question, provider }, controller.signal)) {
        if (event.error) { showSheetError(event.error); break; }
        if (typeof event.t === "string") {
          if (!got) { got = true; sheetStatus.textContent = `${scope} · answering...`; }
          answer += event.t;
          queueRender();
        }
      }
    } catch (error) {
      if (error.name !== "AbortError" && answerSheet) showSheetError(error.message);
    } finally {
      if (askAbort === controller && answerSheet) {
        clearTimeout(renderTimer);
        renderAnswer();
        askAbort = null;
        setRunning(false);
        if (controller.signal.aborted) sheetStatus.textContent = "Stopped";
        else if (!sheetError.hidden) sheetStatus.textContent = "Failed";
        else if (!got) sheetStatus.textContent = "No answer came back.";
        else sheetStatus.textContent = `${scope} · done`;
      }
    }
  }

  function closeSheet() {
    askAbort?.abort();
    askAbort = null;
    clearTimeout(renderTimer);
    renderTimer = null;
    answerSheet?.close();
    answerSheet = null;
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back */ }
    }
    const area = el("textarea", { class: "fl-copyarea", "aria-hidden": "true", tabindex: "-1" });
    area.value = text;
    container.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
    area.remove();
    return ok;
  }

  updateBar();
  updateChrome();
  listBox.replaceChildren(loadingBox());

  // ---- Public handle -----------------------------------------------------------------------

  async function navigate(r, folder) {
    if (readerOpen) closeReader();
    if (panelKind) closePanel();
    exitSelect();
    root = r;
    path = folder;
    clearSearch(false);
    renderRoots();
    rememberLocation();
    loadList();
    trackLocation();
  }

  const handle = {
    async show() {
      if (readerOpen) { attachSelection(); return; }
      const ready = await loadRoots();
      if (ready) trackInitial();
      if (ready && !searchResults && !panelKind) loadList({ silent: !!entries });
    },
    hide() {
      detachSelection();
    },
    refresh() {
      if (panelKind) { openPanel(panelKind); return; }
      if (root && !readerOpen) loadList({ silent: !!entries });
    },
    openSection(name) {
      if (["recent", "trash", "storage"].includes(name)) loadRoots().then((ok) => { if (ok) openPanel(name); });
    },
    async openFile(r, p) {
      const ready = await loadRoots();
      if (!ready) return;
      if (!roots.some((x) => x.name === r)) { ctx.toast(`The folder "${r}" is not shared.`); return; }
      if (panelKind) { panelKind = null; panelToken++; updateChrome(); }
      exitSelect();
      const folder = parentOf(p);
      if (r !== root || folder !== path || searchResults) {
        root = r;
        path = folder;
        clearSearch(false);
        renderRoots();
        rememberLocation();
        loadList({ silent: false });
        trackLocation();
      }
      openReader(r, p, { reset: true });
    },
    // id is "<root>:<relative path>", as search results give it. A folder opens as a folder.
    async openItem(id) {
      const text = String(id ?? "");
      const at = text.indexOf(":");
      const r = at < 0 ? text : text.slice(0, at);
      const rel = at < 0 ? "" : text.slice(at + 1).replace(/^\/+|\/+$/g, "");
      const ready = await loadRoots();
      if (!ready) return;
      if (!roots.some((x) => x.name === r)) { ctx.toast(`The folder "${r}" is not shared.`); return; }
      if (!rel) { navigate(r, ""); return; }
      const entry = await lookupEntry(r, rel);
      if (entry && entry.is_dir) navigate(r, rel);
      else handle.openFile(r, rel);
    },
  };
  return handle;
}
