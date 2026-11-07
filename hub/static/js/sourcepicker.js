// Shared source picker for Learn (chat, quiz, flashcards): choose what to study.
// Tabs: Note | File | Web page | Paste | Topic (and "no source" when allowed). It is meant to live in a bottom sheet:
// use createSourceSheet() for the whole sheet, or createSourcePicker() to embed the tabs in your own sheet.
// Everything that comes from notes, files, the web or the model is put on screen with textContent, never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

// Load a stylesheet once (index.html already links learn.css; new screens link their own).
export function ensureCss(href) {
  if ([...document.querySelectorAll('link[rel="stylesheet"]')].some((l) => (l.getAttribute("href") || "").split("?")[0] === href)) return;
  document.head.append(el("link", { rel: "stylesheet", href }));
}

const ALIASES = { up: "chevron-left", chevron: "chevron-down", down: "arrow-down", x: "close", half: "circle" };
// Kept for older callers: a ui.js icon by its old short name.
export function icon(name, cls = "") {
  return ui.icon(ALIASES[name] || name, { class: cls });
}

// Storage can throw (private windows), so every use is wrapped.
export const store = {
  get(area, key, fallback = "") {
    try { const v = window[area].getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
  },
  set(area, key, value) {
    try { window[area].setItem(key, value); } catch (_) { /* not remembered, still works */ }
  },
};

// The Local / Claude switch (a ui segmented control), remembered under hub.provider.learn and shared by Learn.
export function createProviderSwitch(container, labelText = "Model") {
  const KEY = "hub.provider.learn";
  const read = () => (store.get("localStorage", KEY, "local") === "claude" ? "claude" : "local");
  let value = read();
  const seg = ui.segmented({ options: [{ value: "local", label: "Local" }, { value: "claude", label: "Claude" }],
    value, label: labelText, onChange(v) { value = v; store.set("localStorage", KEY, v); } });
  seg.classList.add("ln-provider");
  container.append(seg);
  return {
    get: () => value,
    // Re-read storage: another Learn section may have changed it while this one was hidden.
    refresh() { value = read(); seg.value = value; },
  };
}

const TYPES = [
  { value: "note", label: "Note" },
  { value: "file", label: "File" },
  { value: "url", label: "Web page" },
  { value: "text", label: "Paste" },
  { value: "topic", label: "Topic" },
];
const DOC_EXT = new Set(["md", "markdown", "txt", "text", "pdf", "rst", "org", "csv", "log", "html", "htm", "docx"]);

const extOf = (name) => { const i = name.lastIndexOf("."); return i < 0 ? "" : name.slice(i + 1).toLowerCase(); };
const baseName = (path) => String(path || "").split("/").filter(Boolean).pop() || String(path || "");
const parentOf = (path) => { const parts = String(path || "").split("/").filter(Boolean); parts.pop(); return parts.join("/"); };
// ui.sheet runs an action again on a double tap while the first is still running (two decks, two saves).
// This opens a sheet whose action buttons ignore a second tap until the first finishes.
export function guardedSheet(ui, opts) {
  const actions = opts.actions && opts.actions.map((a) => {
    if (!a.onClick) return a;
    let running = false;
    return { ...a, async onClick(...args) {
      if (running) return false;
      running = true;
      try { return await a.onClick.apply(this, args); } finally { running = false; }
    } };
  });
  return ui.sheet(actions ? { ...opts, actions } : opts);
}

export function clip(text, n) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n - 1) + "…" : flat;
}

let uid = 0;

export function createSourcePicker(container, ctx, options = {}) {
  ensureCss("/css/learn.css");
  const allowNone = !!options.allowNone;
  const id = `sp${++uid}`;
  const listeners = [];
  const s = {
    type: allowNone ? "none" : (options.type || "note"),
    topic: "", note: null, file: null, url: "", text: "",
    browse: { root: "", path: "" },
  };

  const root = el("div", { class: "sp-root" });
  if (container) container.append(root);

  const seg = ui.segmented({ options: TYPES, value: s.type === "none" ? "note" : s.type, label: "What to study", block: true,
    onChange: (v) => selectType(v) });
  seg.classList.add("sp-seg");
  const body = el("div", { class: "sp-body" });
  const status = el("div", { class: "sp-status", "aria-live": "polite" });
  const checkButton = ui.button({ label: "Preview what the model reads", icon: "eye", variant: "tonal", size: "sm", onClick: () => check() });
  const checkRow = el("div", { class: "sp-checkrow" }, checkButton);
  const preview = el("div", { class: "sp-preview", "aria-live": "polite" });
  root.append(seg, body, checkRow, status, preview);

  const panels = {};
  const msg = (text, cls = "") => el("div", { class: `sp-msg ${cls}`.trim() }, text);
  const retry = (fn) => ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: fn });

  // ---- topic
  const topicInput = el("input", { id: `${id}-topic`, type: "text", maxlength: "300", autocomplete: "off",
    placeholder: "For example: how DNS works", "aria-label": "Topic" });
  const suggestBox = el("div", { class: "sp-suggest" });
  let topicsState = "idle";
  let topicsList = [];
  topicInput.addEventListener("input", () => { s.topic = topicInput.value; changed(); paintSuggest(); });
  function paintSuggest() {
    suggestBox.replaceChildren();
    if (topicsState === "loading") suggestBox.append(msg("Loading suggestions…", "sp-small"));
    else if (topicsState === "error") suggestBox.append(msg("Suggestions are unavailable.", "sp-small"), retry(() => loadTopics(true)));
    else if (topicsState === "ok" && topicsList.length) {
      const row = el("div", { class: "sp-chiprow", role: "group", "aria-label": "Suggested topics" });
      for (const topic of topicsList) {
        row.append(el("button", { type: "button", class: "ui-chip", "aria-pressed": String(s.topic.trim() === topic.title),
          onclick: () => { s.topic = topic.title; topicInput.value = topic.title; changed(); paintSuggest(); } }, topic.title));
      }
      suggestBox.append(el("span", { class: "sp-small" }, "Suggestions"), row);
    }
  }
  async function loadTopics(force) {
    if (topicsState === "loading" || (topicsState === "ok" && !force)) return;
    topicsState = "loading";
    paintSuggest();
    try {
      const data = await api("/api/learn/topics");
      topicsList = (data.topics || []).filter((t) => t && t.title);
      topicsState = "ok";
    } catch (_) { topicsState = "error"; }
    paintSuggest();
  }
  panels.topic = el("div", { class: "sp-panel ui-stack" },
    el("label", { class: "sp-label", for: `${id}-topic` }, "What do you want to learn about?"), topicInput, suggestBox);

  // ---- note
  const noteSearch = el("input", { type: "search", placeholder: "Search your notes", "aria-label": "Search notes",
    autocomplete: "off", enterkeyhint: "search" });
  const noteList = el("div", { class: "sp-list", role: "listbox", "aria-label": "Notes" });
  let notesTimer = null;
  let notesSeq = 0;
  let notesLoaded = false;
  noteSearch.addEventListener("input", () => { clearTimeout(notesTimer); notesTimer = setTimeout(loadNotes, 250); });
  async function loadNotes() {
    const mine = ++notesSeq;
    noteList.replaceChildren(ui.skeleton("list", 3));
    const q = noteSearch.value.trim();
    try {
      const data = await api("/api/notes" + (q ? `?q=${encodeURIComponent(q)}` : ""));
      if (mine !== notesSeq) return;
      notesLoaded = true;
      const notes = data.notes || [];
      noteList.replaceChildren();
      if (!notes.length) {
        noteList.append(ui.emptyState({ icon: "note", compact: true, title: q ? "No notes match" : "No notes yet",
          body: q ? "Try another word." : "Add one in the Notes tab, or pick another tab." }));
        return;
      }
      for (const note of notes.slice(0, 60)) {
        const title = note.title || clip(note.preview || "", 60) || "Untitled note";
        const picked = !!s.note && s.note.id === note.id;
        noteList.append(el("button", { type: "button", class: "sp-item", role: "option", "aria-selected": String(picked), "data-id": note.id,
          onclick: () => { s.note = { id: note.id, title }; paintNote(); changed(); } },
        ui.tile("note", "accent", "sm"),
        el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, title),
          note.preview ? el("span", { class: "sp-item-sub" }, clip(note.preview, 90)) : null),
        el("span", { class: "sp-mark" }, picked ? ui.icon("check") : null)));
      }
    } catch (error) {
      if (mine !== notesSeq) return;
      noteList.replaceChildren(ui.notice(error.message, { tone: "bad" }), retry(loadNotes));
    }
  }
  function paintNote() {
    for (const item of noteList.querySelectorAll(".sp-item")) {
      const on = !!s.note && item.dataset.id === s.note.id;
      item.setAttribute("aria-selected", String(on));
      item.querySelector(".sp-mark").replaceChildren(...(on ? [ui.icon("check")] : []));
    }
  }
  panels.note = el("div", { class: "sp-panel ui-stack" }, noteSearch, noteList);

  // ---- file
  const rootSelect = el("div", { class: "sp-chiprow", role: "radiogroup", "aria-label": "Shared folder" });
  const crumb = el("div", { class: "sp-crumb" });
  const fileList = el("div", { class: "sp-list", "aria-label": "Files" });
  let roots = null;
  let fileSeq = 0;
  async function loadRoots(force) {
    if (roots && !force) return;
    fileList.replaceChildren(ui.skeleton("list", 3));
    try {
      const data = await api("/api/roots");
      roots = Array.isArray(data) ? data : (data.roots || []);
    } catch (error) {
      roots = null;
      fileList.replaceChildren(ui.notice(error.message, { tone: "bad" }), retry(() => loadRoots(true).then(browseNow)));
      return;
    }
    rootSelect.replaceChildren();
    for (const r of roots) {
      rootSelect.append(el("button", { type: "button", class: "ui-chip", role: "radio", "data-root": r.name,
        "aria-checked": String(r.name === s.browse.root),
        onclick: () => { s.browse = { root: r.name, path: "" }; paintRoots(); browseNow(); } }, r.name));
    }
    if (!roots.length) {
      fileList.replaceChildren(ui.emptyState({ icon: "folder", compact: true, title: "No shared folders", body: "Share a folder on the laptop first." }));
      return;
    }
    if (!s.browse.root || !roots.some((r) => r.name === s.browse.root)) s.browse = { root: roots[0].name, path: "" };
    paintRoots();
  }
  function paintRoots() {
    for (const b of rootSelect.querySelectorAll("button")) {
      const on = b.dataset.root === s.browse.root;
      b.setAttribute("aria-checked", String(on));
      b.classList.toggle("is-on", on);
    }
  }
  async function browseNow() {
    if (!roots || !roots.length || !s.browse.root) return;
    const mine = ++fileSeq;
    const { root: rt, path } = s.browse;
    crumb.textContent = [rt, ...path.split("/").filter(Boolean)].join(" / ");
    fileList.replaceChildren(ui.skeleton("list", 3));
    try {
      const data = await api(`/api/files?${new URLSearchParams({ root: rt, path })}`);
      if (mine !== fileSeq) return;
      fileList.replaceChildren();
      if (path) {
        fileList.append(el("button", { type: "button", class: "sp-item", "aria-label": `Back to ${parentOf(path) || rt}`,
          onclick: () => { s.browse.path = parentOf(path); browseNow(); } },
        ui.tile("arrow-left", "gray", "sm"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, "Back"))));
      }
      let shown = 0;
      for (const entry of data.entries || []) {
        const full = path ? `${path}/${entry.name}` : entry.name;
        if (entry.is_dir) {
          shown++;
          fileList.append(el("button", { type: "button", class: "sp-item", onclick: () => { s.browse.path = full; browseNow(); } },
            ui.tile("folder", "warn", "sm"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, entry.name)),
            ui.icon("chevron-right", { class: "sp-chev" })));
        } else if (DOC_EXT.has(extOf(entry.name))) {
          shown++;
          const on = !!s.file && s.file.root === rt && s.file.path === full;
          fileList.append(el("button", { type: "button", class: "sp-item sp-file", "aria-pressed": String(on), "data-path": full,
            onclick: () => { s.file = { root: rt, path: full }; paintFile(); changed(); } },
          ui.tile("file", "info", "sm"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, entry.name)),
          el("span", { class: "sp-mark" }, on ? ui.icon("check") : null)));
        }
      }
      if (!shown) fileList.append(ui.emptyState({ icon: "folder", compact: true, title: "Nothing to study here", body: "Documents are .md, .txt, .pdf and similar." }));
    } catch (error) {
      if (mine !== fileSeq) return;
      fileList.replaceChildren(ui.notice(error.message, { tone: "bad" }), retry(browseNow));
    }
  }
  function paintFile() {
    for (const item of fileList.querySelectorAll(".sp-file")) {
      const on = !!s.file && s.file.root === s.browse.root && item.dataset.path === s.file.path;
      item.setAttribute("aria-pressed", String(on));
      item.querySelector(".sp-mark").replaceChildren(...(on ? [ui.icon("check")] : []));
    }
  }
  panels.file = el("div", { class: "sp-panel ui-stack" }, rootSelect, crumb, fileList);

  // ---- web page
  const urlInput = el("input", { id: `${id}-url`, type: "url", inputmode: "url", autocapitalize: "off", autocomplete: "off",
    spellcheck: "false", placeholder: "https://example.com/article", "aria-label": "Web address" });
  urlInput.addEventListener("input", () => { s.url = urlInput.value; changed(); });
  panels.url = el("div", { class: "sp-panel ui-stack" },
    el("label", { class: "sp-label", for: `${id}-url` }, "Web address"), urlInput,
    el("p", { class: "sp-small" }, "Only public web pages (http or https) can be read."));

  // ---- pasted text
  const textInput = el("textarea", { id: `${id}-text`, rows: "6", placeholder: "Paste the text you want to study", "aria-label": "Pasted text" });
  const textCount = el("div", { class: "sp-small" });
  textInput.addEventListener("input", () => {
    s.text = textInput.value;
    textCount.textContent = s.text.trim() ? `${s.text.length.toLocaleString()} characters` : "";
    changed();
  });
  panels.text = el("div", { class: "sp-panel ui-stack" }, el("label", { class: "sp-label", for: `${id}-text` }, "Paste text"), textInput, textCount);

  panels.none = el("div", { class: "sp-panel ui-stack" },
    ui.emptyState({ icon: "sparkles", compact: true, title: "No source", body: "You can ask general questions without one.",
      action: { label: "Choose a source", icon: "plus", onClick: () => selectType("note") } }));

  for (const [key, panel] of Object.entries(panels)) { panel.dataset.panel = key; body.append(panel); }

  // ---- state
  const urlWithScheme = (raw) => {
    const t = raw.trim();
    if (!t) return "";
    return /^[a-z][a-z0-9+.-]*:/i.test(t) ? t : `https://${t}`;
  };
  function getSource() {
    switch (s.type) {
      case "topic": { const t = s.topic.trim(); return t ? { type: "topic", topic: t } : null; }
      case "note": return s.note ? { type: "note", id: s.note.id } : null;
      case "file": return s.file ? { type: "file", root: s.file.root, path: s.file.path } : null;
      case "url": { const u = urlWithScheme(s.url); return u ? { type: "url", url: u } : null; }
      case "text": return s.text.trim() ? { type: "text", text: s.text } : null;
      default: return null;
    }
  }
  function label() {
    switch (s.type) {
      case "topic": return s.topic.trim() ? `Topic: ${clip(s.topic, 40)}` : "";
      case "note": return s.note ? `Note: ${clip(s.note.title, 40)}` : "";
      case "file": return s.file ? `File: ${baseName(s.file.path)}` : "";
      case "url": {
        const u = urlWithScheme(s.url);
        if (!u) return "";
        try { return `Web page: ${new URL(u).hostname}`; } catch (_) { return `Web page: ${clip(u, 40)}`; }
      }
      case "text": return s.text.trim() ? `Pasted text (${s.text.length.toLocaleString()} characters)` : "";
      default: return "";
    }
  }
  let previewSeq = 0;
  let previewing = false;
  function clearPreview() { previewSeq++; previewing = false; preview.replaceChildren(); status.textContent = ""; paintCheck(); }
  function paintCheck() {
    checkRow.hidden = s.type === "none" || !getSource();
    checkButton.disabled = previewing;
    checkButton.classList.toggle("is-loading", previewing);
  }
  function changed() {
    clearPreview();
    for (const fn of listeners) { try { fn(getSource()); } catch (error) { console.error(error); } }
  }
  function paintTypes() {
    seg.hidden = s.type === "none";
    if (s.type !== "none") seg.value = s.type;
    for (const [key, panel] of Object.entries(panels)) panel.hidden = key !== s.type;
  }
  function selectType(type) {
    if (type === s.type && type !== "none") return;
    s.type = type;
    paintTypes();
    if (type === "topic") loadTopics();
    if (type === "note") { paintNote(); if (!notesLoaded) loadNotes(); }
    if (type === "file") { paintFile(); loadRoots().then(browseNow); }
    changed();
  }

  async function check() {
    const source = getSource();
    if (!source || previewing) return;
    const mine = ++previewSeq;
    previewing = true;
    paintCheck();
    try {
      const data = await api("/api/learn/source/preview", { method: "POST", json: { source } });
      if (mine !== previewSeq) return;
      previewing = false;
      paintCheck();
      const lines = String(data.excerpt || "").split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 6).join("\n");
      preview.replaceChildren(el("div", { class: "ui-card ui-card--flat sp-card" },
        el("div", { class: "sp-card-head" }, ui.icon("check-circle", { class: "sp-ok" }), el("strong", {}, data.title || "Untitled"),
          data.chars ? el("span", { class: "sp-small" }, `${Number(data.chars).toLocaleString()} characters`) : null),
        lines ? el("pre", { class: "sp-excerpt" }, lines) : null));
    } catch (error) {
      if (mine !== previewSeq) return;
      previewing = false;
      paintCheck();
      preview.replaceChildren(ui.notice(error.message, { tone: "bad" }));
    }
  }

  function setSource(source, hint) {
    if (!source) {
      if (allowNone) { s.type = "none"; paintTypes(); changed(); }
      return;
    }
    s.type = source.type in panels ? source.type : s.type;
    if (source.type === "topic") { s.topic = source.topic || ""; topicInput.value = s.topic; loadTopics(); }
    else if (source.type === "note") { s.note = { id: source.id, title: hint || "Note" }; if (!notesLoaded) loadNotes(); }
    else if (source.type === "file") {
      s.file = { root: source.root, path: source.path };
      s.browse = { root: source.root, path: parentOf(source.path) };
      loadRoots().then(browseNow);
    } else if (source.type === "url") { s.url = source.url || ""; urlInput.value = s.url; }
    else if (source.type === "text") { s.text = source.text || ""; textInput.value = s.text; textCount.textContent = s.text.trim() ? `${s.text.length.toLocaleString()} characters` : ""; }
    paintTypes(); paintNote(); paintFile(); paintSuggest();
    changed();
  }

  paintTypes();
  paintCheck();
  if (s.type === "note") loadNotes();
  if (s.type === "topic") loadTopics();

  return {
    root, getSource, setSource, label,
    onChange(fn) { listeners.push(fn); },
    focus() { seg.querySelector('[aria-checked="true"]')?.focus({ preventScroll: true }); },
  };
}

// A whole bottom sheet around a persistent picker. The picker keeps its state between openings.
//   const src = createSourceSheet(ctx, {title, allowNone, applyLabel, onApply(source, label)});
//   src.open(); src.source; src.label; src.set(source, label)
export function createSourceSheet(ctx, { title = "Choose a source", allowNone = false, applyLabel = "Use this source", onApply } = {}) {
  const picker = createSourcePicker(null, ctx, { allowNone });
  let source = null;
  let label = "";
  const api2 = {
    get source() { return source; },
    get label() { return label; },
    set(next, nextLabel = "") {
      source = next || null;
      label = nextLabel;
      picker.setSource(source, label.replace(/^[^:]*: /, ""));
    },
    open() {
      const s = ui.sheet({ title, content: picker.root, class: "sp-sheet",
        onClose: () => { picker.root.remove(); },
        actions: [
          ...(allowNone ? [{ label: "No source", variant: "ghost", onClick() { source = null; label = ""; picker.setSource(null); onApply?.(null, ""); } }] : []),
          { label: applyLabel, variant: "primary", icon: "check", onClick() {
            const picked = picker.getSource();
            if (!picked && !allowNone) { ctx.toast("Pick or type something first.", { kind: "warn" }); return false; }
            source = picked;
            label = picked ? picker.label() : "";
            onApply?.(source, label);
          } },
        ] });
      return s;
    },
  };
  return api2;
}
