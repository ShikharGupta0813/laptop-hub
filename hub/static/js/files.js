// Files tab: browse the shared folders, read documents, and ask the model about a passage.
import { api, sse, fileUrl, renderMarkdown, el } from "./api.js";

const MAX_INLINE = 2 * 1024 * 1024; // bigger files are offered as a download instead
const MAX_ASK = 12000; // characters sent to the model
const RENDER_EVERY_MS = 100; // answer re-render throttle (~10 per second)
const KEY_ROOT = "hub.files.root";
const KEY_PATHS = "hub.files.paths";
const KEY_PROVIDER = "hub.files.provider";

const MARKDOWN = new Set(["md", "markdown"]);
const IMAGES = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico"]);
const WRAPPED_TEXT = new Set(["txt", "log", "text", "rst"]);
const TEXT = new Set([
  "txt", "text", "log", "json", "jsonl", "csv", "tsv", "yml", "yaml", "toml", "ini", "cfg", "conf",
  "xml", "html", "htm", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "sh", "bash", "zsh", "bat",
  "ps1", "c", "h", "cpp", "hpp", "cs", "java", "kt", "go", "rs", "rb", "php", "sql", "tex", "rst",
  "env", "gitignore", "lock", "diff", "patch", "ipynb",
]);

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

const ICONS = {
  folder: "M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2Z",
  file: "M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm7 1.5V9h5.5L13 3.5Z",
  image: "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm1 14h12l-4-5.5-3 4-2-2.5L6 17Zm2.5-8.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z",
  back: "M15.4 5.4 14 4l-8 8 8 8 1.4-1.4L8.8 12l6.6-6.6Z",
  chevron: "M8.6 5.4 10 4l8 8-8 8-1.4-1.4 6.6-6.6-6.6-6.6Z",
  search: "M10 3a7 7 0 1 0 4.2 12.6l4.9 4.9 1.4-1.4-4.9-4.9A7 7 0 0 0 10 3Zm0 2a5 5 0 1 1 0 10 5 5 0 0 1 0-10Z",
  close: "M5.7 4.3 4.3 5.7 10.6 12l-6.3 6.3 1.4 1.4L12 13.4l6.3 6.3 1.4-1.4-6.3-6.3 6.3-6.3-1.4-1.4L12 10.6 5.7 4.3Z",
};

function icon(name, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `fl-icon ${cls}`.trim());
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

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

function iconFor(entry) {
  if (entry.is_dir) return "folder";
  return kindOf(entry.name) === "image" ? "image" : "file";
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

// ---- the module ----------------------------------------------------------------------------

export function init(container, ctx) {
  // State
  let roots = [];
  let root = null;
  let path = "";
  let entries = null;
  let searchResults = null; // null = not searching
  let searchQuery = "";
  let listToken = 0;
  let rootsPromise = null;
  let browserScroll = 0;

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
  let provider = store.get(KEY_PROVIDER) === "claude" ? "claude" : "local";

  // ---- Browser pane DOM
  const rootChips = el("div", { class: "fl-roots", role: "group", "aria-label": "Shared folders" });
  const searchInput = el("input", {
    type: "search", class: "fl-search-input", placeholder: "Search names in this folder",
    "aria-label": "Search file names in this folder", enterkeyhint: "search",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false",
  });
  const searchForm = el("form", { class: "fl-search", role: "search" },
    searchInput,
    el("button", { type: "submit", class: "btn fl-iconbtn", "aria-label": "Search" }, icon("search")));
  const crumbs = el("nav", { class: "fl-crumbs", "aria-label": "Folder path" });
  const listBox = el("div", { class: "fl-listbox", "aria-live": "polite" });
  const browser = el("div", { class: "fl-browser" }, rootChips, searchForm, crumbs, listBox);

  // ---- Reader pane DOM
  const backBtn = el("button", { type: "button", class: "btn fl-back", "aria-label": "Back" },
    icon("back"), el("span", {}, "Back"));
  const titleEl = el("h2", { class: "fl-title" });
  const subEl = el("div", { class: "fl-sub muted" });
  const head = el("header", { class: "fl-head" }, backBtn, el("div", { class: "grow" }, titleEl, subEl));
  const docEl = el("article", { class: "fl-doc" });
  const scroller = el("div", { class: "fl-scroll" }, docEl);

  const scopeLabel = el("span", { class: "fl-scope", "aria-live": "polite" }, "Whole page");
  const provLocal = el("button", { type: "button", class: "fl-seg", "data-p": "local" }, "Local");
  const provClaude = el("button", { type: "button", class: "fl-seg", "data-p": "claude" }, "Claude");
  const provBox = el("div", { class: "fl-prov", role: "group", "aria-label": "Model" }, provLocal, provClaude);
  const askInput = el("input", {
    type: "text", class: "fl-ask-input", placeholder: "Ask a question about this",
    "aria-label": "Your question", enterkeyhint: "send", autocomplete: "off", maxlength: "500",
  });
  const askForm = el("form", { class: "fl-askform", hidden: true },
    askInput,
    el("button", { type: "submit", class: "btn primary" }, "Send"));
  const aiButtons = [
    ["explain", "Explain"], ["summarise", "Summarise"], ["quiz", "Quiz"], ["custom", "Ask..."],
  ].map(([mode, label]) => el("button", { type: "button", class: "btn fl-ai", "data-mode": mode }, label));
  const bar = el("div", { class: "fl-bar" },
    el("div", { class: "fl-barhead" }, scopeLabel, provBox),
    askForm,
    el("div", { class: "fl-aibtns" }, aiButtons));
  const reader = el("div", { class: "fl-reader", hidden: true }, head, scroller, bar);

  // ---- Answer sheet DOM
  const sheetTitle = el("h3", { class: "fl-sheet-title" }, "Answer");
  const sheetStatus = el("div", { class: "fl-status muted", role: "status" });
  const answerEl = el("div", { class: "markdown fl-answer" });
  const sheetError = el("p", { class: "error fl-sheet-error", role: "alert", hidden: true });
  const stopBtn = el("button", { type: "button", class: "btn danger" }, "Stop");
  const copyBtn = el("button", { type: "button", class: "btn" }, "Copy");
  const saveBtn = el("button", { type: "button", class: "btn" }, "Save to notes");
  const closeBtn = el("button", { type: "button", class: "btn primary" }, "Close");
  const sheet = el("div", { class: "sheet fl-sheet", role: "dialog", "aria-label": "Answer", hidden: true },
    el("div", { class: "fl-sheet-head" }, sheetTitle, sheetStatus),
    el("div", { class: "fl-sheet-body" }, answerEl, sheetError),
    el("div", { class: "fl-sheet-actions" }, stopBtn, copyBtn, saveBtn, closeBtn));

  container.replaceChildren(browser, reader, sheet);

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
        const list = await api("/api/roots");
        roots = list.map((r) => r.name);
        if (!roots.length) { renderRootsEmpty(); return false; }
        const wanted = store.get(KEY_ROOT);
        root = roots.includes(wanted) ? wanted : roots[0];
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
    rootChips.replaceChildren(...roots.map((name) => el("button", {
      type: "button", class: "chip fl-chip", "aria-pressed": String(name === root),
      onclick: () => switchRoot(name),
    }, name)));
  }

  function renderRootsEmpty() {
    rootChips.replaceChildren();
    crumbs.replaceChildren();
    listBox.replaceChildren(el("p", { class: "fl-note muted" }, "No folders are shared yet. Add one on your laptop, then reopen this tab."));
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
    return el("p", { class: "fl-note muted", role: "status" }, "Loading...");
  }

  function errorBox(message, retry, extra) {
    return el("div", { class: "fl-errorbox card", role: "alert" },
      el("p", { class: "error" }, message),
      el("div", { class: "row wrap" },
        el("button", { type: "button", class: "btn primary", onclick: retry }, "Retry"),
        extra));
  }

  function switchRoot(name) {
    if (name === root) return;
    root = name;
    path = savedPaths()[root] || "";
    clearSearch(false);
    renderRoots();
    rememberLocation();
    loadList();
  }

  function goTo(folder) {
    path = folder;
    clearSearch(false);
    rememberLocation();
    loadList();
  }

  function renderCrumbs() {
    const parts = path ? path.split("/") : [];
    const nodes = [crumb(root, "", parts.length === 0)];
    parts.forEach((part, i) => {
      nodes.push(el("span", { class: "fl-sep", "aria-hidden": "true" }, "/"));
      nodes.push(crumb(part, parts.slice(0, i + 1).join("/"), i === parts.length - 1));
    });
    crumbs.replaceChildren(...nodes);
    crumbs.scrollLeft = crumbs.scrollWidth;
  }

  function crumb(label, target, current) {
    return el("button", {
      type: "button", class: "fl-crumb", "aria-current": current ? "page" : false,
      onclick: () => { if (!current || searchResults) goTo(target); },
    }, label);
  }

  async function loadList({ silent = false } = {}) {
    if (!root) return;
    const mine = ++listToken;
    renderCrumbs();
    if (searchResults) return;
    if (!silent || !entries) listBox.replaceChildren(loadingBox());
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
      entries = null;
      const top = path ? el("button", {
        type: "button", class: "btn", onclick: () => goTo(""),
      }, "Go to top folder") : null;
      listBox.replaceChildren(errorBox(error.message, () => loadList(), top));
    }
  }

  function rowFor(entry, fullPath, subtitle) {
    const isDir = entry.is_dir;
    return el("li", {},
      el("button", {
        type: "button", class: "fl-item",
        onclick: () => (isDir ? goTo(fullPath) : openReader(root, fullPath, { size: entry.size, reset: true })),
      },
      icon(iconFor(entry), isDir ? "dir" : ""),
      el("span", { class: "fl-main" },
        el("span", { class: "fl-name" }, entry.name),
        subtitle),
      isDir ? icon("chevron", "fl-go") : null));
  }

  function renderList() {
    if (!entries.length) {
      listBox.replaceChildren(el("p", { class: "fl-note muted" }, "This folder is empty."));
      return;
    }
    const items = entries.map((entry) => {
      const meta = entry.is_dir ? fmtDate(entry.modified)
        : [fmtSize(entry.size), fmtDate(entry.modified)].filter(Boolean).join("  ·  ");
      return rowFor(entry, path ? `${path}/${entry.name}` : entry.name,
        el("span", { class: "fl-meta muted" }, meta));
    });
    listBox.replaceChildren(el("ul", { class: "fl-list" }, items));
  }

  // ---- Search ------------------------------------------------------------------------------

  function clearSearch(reload = true) {
    searchResults = null;
    searchQuery = "";
    searchInput.value = "";
    if (reload) loadList();
  }

  async function runSearch(query) {
    const mine = ++listToken;
    searchQuery = query;
    searchResults = [];
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
        el("button", { type: "button", class: "btn", onclick: () => clearSearch() }, "Clear search")));
    }
  }

  function renderResults() {
    const where = path ? `${root}/${path}` : root;
    const head = el("div", { class: "fl-resulthead row" },
      el("span", { class: "grow muted" },
        `${searchResults.length ? searchResults.length : "No"} result${searchResults.length === 1 ? "" : "s"} for “${searchQuery}” in ${where}`),
      el("button", { type: "button", class: "btn", onclick: () => clearSearch() }, "Clear"));
    if (!searchResults.length) {
      listBox.replaceChildren(head, el("p", { class: "fl-note muted" }, "Nothing matched that name. Try fewer letters."));
      return;
    }
    const items = searchResults.map((result) => rowFor(
      { name: result.name, is_dir: result.is_dir, size: undefined },
      result.path,
      el("span", { class: "fl-meta fl-relpath muted" }, result.path)));
    listBox.replaceChildren(head, el("ul", { class: "fl-list" }, items));
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
    browser.hidden = visible;
  }

  async function openReader(r, p, { size, reset = false, push = true, restoreScroll = 0 } = {}) {
    const previous = doc;
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
    docEl.replaceChildren(el("p", { class: "fl-note muted", role: "status" }, "Opening..."));
    bar.hidden = true;
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
        showCard("PDF files open in your browser's viewer.");
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

  // The folder listing usually knows the size; links and other tabs do not.
  async function lookupSize(r, p) {
    if (r === root && entries && parentOf(p) === path) {
      const hit = entries.find((e) => e.name === baseName(p));
      if (hit) return hit.size;
    }
    try {
      const query = new URLSearchParams({ root: r, path: parentOf(p) });
      const data = await api(`/api/files?${query}`);
      const hit = data.entries.find((e) => e.name === baseName(p));
      return hit ? hit.size : null;
    } catch (_) {
      return null;
    }
  }

  function actionButtons() {
    const url = fileUrl(doc.root, doc.path);
    return el("div", { class: "row wrap" },
      el("a", { class: "btn primary", href: url, target: "_blank", rel: "noopener noreferrer" }, "Open"),
      el("a", { class: "btn", href: fileUrl(doc.root, doc.path, true), download: baseName(doc.path) }, "Download"));
  }

  function showCard(message) {
    docEl.replaceChildren(el("div", { class: "card fl-card" },
      el("p", { class: "fl-cardname" }, baseName(doc.path)),
      el("p", { class: "muted" }, message),
      actionButtons()));
  }

  function showImage() {
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
    docEl.replaceChildren(text ? pre : el("p", { class: "fl-note muted" }, "This file is empty."));
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
        img.replaceWith(el("span", { class: "fl-brokenimg muted" }, `[image: ${img.alt || "not found"}]`));
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
      const typing = bar.contains(document.activeElement) || sheet.contains(document.activeElement);
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
    for (const button of [provLocal, provClaude]) {
      button.setAttribute("aria-pressed", String(button.dataset.p === provider));
    }
  }

  // ---- AI help -----------------------------------------------------------------------------

  const touchPanel = () => { panelTouchedAt = Date.now(); };
  bar.addEventListener("pointerdown", touchPanel, true);
  sheet.addEventListener("pointerdown", touchPanel, true);
  // Keep the selection alive while a button is pressed (the text input still needs focus).
  for (const button of [...aiButtons, provLocal, provClaude, stopBtn, copyBtn, saveBtn, closeBtn]) {
    button.addEventListener("pointerdown", (event) => event.preventDefault());
  }

  for (const button of [provLocal, provClaude]) {
    button.addEventListener("click", () => {
      provider = button.dataset.p;
      store.set(KEY_PROVIDER, provider);
      updateBar();
    });
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
    const body = sheet.querySelector(".fl-sheet-body");
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
    stopBtn.hidden = !running;
    copyBtn.disabled = running || !answer;
    saveBtn.disabled = running || !answer;
  }

  function showSheetError(message) {
    sheetError.textContent = message;
    sheetError.hidden = false;
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
    answerEl.replaceChildren();
    sheetError.hidden = true;
    sheetTitle.textContent = question ? "Your question" : TITLES[mode];
    sheetStatus.textContent = `${scope} · ${provider === "claude" ? "Claude" : "Local model"}`;
    sheet.hidden = false;
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
      if (error.name !== "AbortError") showSheetError(error.message);
    } finally {
      if (askAbort === controller) {
        clearTimeout(renderTimer);
        renderAnswer();
        askAbort = null;
        setRunning(false);
        if (controller.signal.aborted) sheetStatus.textContent = "Stopped";
        else if (!got && sheetError.hidden) sheetStatus.textContent = "No answer came back.";
        else sheetStatus.textContent = `${scope} · done`;
      }
    }
  }

  function closeSheet() {
    askAbort?.abort();
    askAbort = null;
    clearTimeout(renderTimer);
    renderTimer = null;
    sheet.hidden = true;
  }

  stopBtn.addEventListener("click", () => askAbort?.abort());
  closeBtn.addEventListener("click", closeSheet);

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back */ }
    }
    const area = el("textarea", { class: "fl-copyarea", "aria-hidden": "true", tabindex: "-1" });
    area.value = text;
    sheet.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
    area.remove();
    return ok;
  }

  copyBtn.addEventListener("click", async () => {
    ctx.toast((await copyText(answer)) ? "Copied" : "Could not copy. Select the text and copy it by hand.");
  });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      await api("/api/notes", { method: "POST", json: { text: answer, source: "ai" } });
      ctx.toast("Saved");
    } catch (error) {
      ctx.toast(error.message);
    } finally {
      saveBtn.disabled = !answer || !!askAbort;
    }
  });

  setRunning(false);
  updateBar();
  listBox.replaceChildren(loadingBox());

  // ---- Public handle -----------------------------------------------------------------------

  return {
    async show() {
      if (readerOpen) { attachSelection(); return; }
      const ready = await loadRoots();
      if (ready && !searchResults) loadList({ silent: !!entries });
    },
    hide() {
      detachSelection();
    },
    async openFile(r, p) {
      const ready = await loadRoots();
      if (!ready) return;
      if (!roots.includes(r)) { ctx.toast(`The folder "${r}" is not shared.`); return; }
      const folder = parentOf(p);
      if (r !== root || folder !== path || searchResults) {
        root = r;
        path = folder;
        clearSearch(false);
        renderRoots();
        rememberLocation();
        loadList({ silent: false });
      }
      openReader(r, p, { reset: true });
    },
  };
}
