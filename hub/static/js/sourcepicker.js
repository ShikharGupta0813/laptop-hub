// Shared source picker for Learn (chat and quiz): choose what to study.
// Types: Topic, Note, File, Web page, Pasted text (and None when allowed). Everything that comes
// from notes, files, the web or the model is put on screen with textContent, never innerHTML.
import { api, el } from "./api.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  folder: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  up: "M15 18l-6-6 6-6",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  alert: "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  send: "M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z",
  stop: "M7 7h10v10H7z",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  chevron: "M6 9l6 6 6-6",
  down: "M12 5v14M19 12l-7 7-7-7",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  half: "M12 3a9 9 0 1 0 0 18V3z",
  flame: "M12 2c1 4 5 5.5 5 10a5 5 0 0 1-10 0c0-2 1-3 2-4 0 2 1 3 2 3 0-3-1-5 1-9z",
  refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
};

// Small inline icon. Exported for studychat.js and quiz.js so all three share one set.
export function icon(name, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", `lr-ico ${cls}`.trim());
  for (const [k, v] of Object.entries({ fill: name === "stop" || name === "half" ? "currentColor" : "none",
    stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" })) {
    svg.setAttribute(k, v);
  }
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name] || ICONS.alert);
  svg.append(path);
  return svg;
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

// The Local / Claude switch, remembered under hub.provider.learn and shared by chat and quiz.
export function createProviderSwitch(container, labelText = "Model") {
  const KEY = "hub.provider.learn";
  const read = () => (store.get("localStorage", KEY, "local") === "claude" ? "claude" : "local");
  let value = read();
  const buttons = {};
  const group = el("div", { class: "lr-seg", role: "radiogroup", "aria-label": labelText });
  for (const [id, text] of [["local", "Local"], ["claude", "Claude"]]) {
    const button = el("button", { type: "button", role: "radio", "data-provider": id,
      onclick: () => { value = id; store.set("localStorage", KEY, id); paint(); } }, text);
    buttons[id] = button;
    group.append(button);
  }
  function paint() {
    for (const [id, button] of Object.entries(buttons)) button.setAttribute("aria-checked", String(id === value));
  }
  paint();
  container.append(group);
  return {
    get: () => value,
    // Re-read storage: the other Learn section may have changed it while this one was hidden.
    refresh() { value = read(); paint(); },
  };
}

const TYPES = [
  { id: "topic", label: "Topic" },
  { id: "note", label: "Note" },
  { id: "file", label: "File" },
  { id: "url", label: "Web page" },
  { id: "text", label: "Pasted text" },
];
const DOC_EXT = new Set(["md", "markdown", "txt", "text", "pdf", "rst", "org", "csv", "log", "html", "htm"]);

function extOf(name) {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}
function baseName(path) {
  return String(path || "").split("/").filter(Boolean).pop() || String(path || "");
}
function parentOf(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}
function clip(text, n) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n - 1) + "…" : flat;
}

let uid = 0;

export function createSourcePicker(container, ctx, options = {}) {
  const allowNone = !!options.allowNone;
  const id = `sp${++uid}`;
  const listeners = [];

  const types = allowNone ? [{ id: "none", label: "None" }, ...TYPES] : TYPES;
  const s = {
    type: allowNone ? "none" : "topic",
    topic: "", note: null, file: null, url: "", text: "",
    browse: { root: "", path: "" },
  };

  const root = el("div", { class: "sp-root" });
  container.append(root);

  // ---- type chips
  const typeBar = el("div", { class: "sp-types", role: "radiogroup", "aria-label": "What to study" });
  const typeButtons = {};
  for (const t of types) {
    const button = el("button", { type: "button", class: "sp-chip", role: "radio", "data-type": t.id,
      onclick: () => selectType(t.id) }, t.label);
    typeButtons[t.id] = button;
    typeBar.append(button);
  }
  typeBar.addEventListener("keydown", (event) => {
    if (!["ArrowRight", "ArrowLeft"].includes(event.key)) return;
    const ids = types.map((t) => t.id);
    const next = ids[(ids.indexOf(s.type) + (event.key === "ArrowRight" ? 1 : ids.length - 1)) % ids.length];
    selectType(next);
    typeButtons[next].focus();
  });

  const body = el("div", { class: "sp-body" });
  const status = el("div", { class: "sp-status", "aria-live": "polite" });
  const checkButton = el("button", { type: "button", class: "btn sp-check", onclick: () => check() }, "Check");
  const checkRow = el("div", { class: "sp-checkrow" }, checkButton,
    el("span", { class: "muted sp-checkhint" }, "See what the model will read."));
  const preview = el("div", { class: "sp-preview", "aria-live": "polite" });
  root.append(typeBar, body, checkRow, status, preview);

  // ---- per-type panels (built once, shown one at a time)
  const panels = {};

  // topic
  const topicInput = el("input", { id: `${id}-topic`, type: "text", maxlength: "300", autocomplete: "off",
    placeholder: "For example: how DNS works", "aria-label": "Topic" });
  const suggestBox = el("div", { class: "sp-suggest" });
  let topicsState = "idle"; // idle | loading | ok | error
  let topicsList = [];
  let topicsError = "";
  topicInput.addEventListener("input", () => { s.topic = topicInput.value; changed(); paintSuggest(); });
  function paintSuggest() {
    suggestBox.replaceChildren();
    if (topicsState === "loading") {
      suggestBox.append(el("span", { class: "muted sp-small" }, "Loading suggestions…"));
    } else if (topicsState === "error") {
      suggestBox.append(el("span", { class: "muted sp-small" }, `Suggestions unavailable. ${topicsError}`),
        el("button", { type: "button", class: "sp-chip", onclick: () => loadTopics(true) }, "Retry"));
    } else if (topicsState === "ok" && topicsList.length) {
      suggestBox.append(el("span", { class: "muted sp-small", id: `${id}-sugl` }, "Suggestions"));
      const row = el("div", { class: "sp-chiprow", role: "group", "aria-labelledby": `${id}-sugl` });
      for (const topic of topicsList) {
        row.append(el("button", { type: "button", class: "sp-chip", "aria-pressed": String(s.topic.trim() === topic.title),
          onclick: () => { s.topic = topic.title; topicInput.value = topic.title; changed(); paintSuggest(); } }, topic.title));
      }
      suggestBox.append(row);
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
    } catch (error) {
      topicsState = "error";
      topicsError = error.message;
    }
    paintSuggest();
  }
  panels.topic = el("div", { class: "sp-panel" },
    el("label", { class: "sp-label", for: `${id}-topic` }, "What do you want to learn about?"), topicInput, suggestBox);

  // note
  const noteSearch = el("input", { type: "search", class: "sp-search", placeholder: "Search your notes", "aria-label": "Search notes",
    autocomplete: "off", enterkeyhint: "search" });
  const noteList = el("div", { class: "sp-list", role: "listbox", "aria-label": "Notes" });
  const noteChosen = el("div", { class: "sp-chosen", "aria-live": "polite" });
  let notesTimer = null;
  let notesSeq = 0;
  let notesLoaded = false;
  noteSearch.addEventListener("input", () => {
    clearTimeout(notesTimer);
    notesTimer = setTimeout(loadNotes, 250);
  });
  async function loadNotes() {
    const mine = ++notesSeq;
    noteList.replaceChildren(el("div", { class: "sp-msg muted" }, "Loading notes…"));
    const q = noteSearch.value.trim();
    try {
      const data = await api("/api/notes" + (q ? `?q=${encodeURIComponent(q)}` : ""));
      if (mine !== notesSeq) return;
      notesLoaded = true;
      const notes = data.notes || [];
      noteList.replaceChildren();
      if (!notes.length) {
        noteList.append(el("div", { class: "sp-msg muted" }, q ? "No notes match that search." : "You have no notes yet. Add one in the Notes tab."));
        return;
      }
      for (const note of notes.slice(0, 60)) {
        const picked = s.note && s.note.id === note.id;
        const title = note.title || clip(note.preview || "", 60) || "Untitled note";
        noteList.append(el("button", { type: "button", class: "sp-item", role: "option", "aria-selected": String(!!picked),
          "data-id": note.id, onclick: () => { s.note = { id: note.id, title }; paintNote(); changed(); } },
          el("span", { class: "sp-item-main" },
            el("span", { class: "sp-item-title" }, title),
            note.preview ? el("span", { class: "sp-item-sub muted" }, clip(note.preview, 90)) : null),
          el("span", { class: "sp-mark" }, picked ? icon("check") : null)));
      }
    } catch (error) {
      if (mine !== notesSeq) return;
      noteList.replaceChildren(el("div", { class: "sp-msg error", role: "alert" }, error.message),
        el("button", { type: "button", class: "btn", onclick: () => loadNotes() }, "Retry"));
    }
  }
  function paintNote() {
    noteChosen.textContent = s.note ? `Selected: ${s.note.title}` : "Tap a note to choose it.";
    for (const item of noteList.querySelectorAll(".sp-item")) {
      const on = !!s.note && item.dataset.id === s.note.id;
      item.setAttribute("aria-selected", String(on));
      item.querySelector(".sp-mark").replaceChildren(...(on ? [icon("check")] : []));
    }
  }
  panels.note = el("div", { class: "sp-panel" }, noteSearch, noteChosen, noteList);

  // file
  const rootSelect = el("div", { class: "sp-chiprow", role: "radiogroup", "aria-label": "Shared folder" });
  const crumb = el("div", { class: "sp-crumb muted" });
  const fileList = el("div", { class: "sp-list", "aria-label": "Files" });
  const fileChosen = el("div", { class: "sp-chosen", "aria-live": "polite" });
  let roots = null;
  let rootsError = "";
  let fileSeq = 0;
  async function loadRoots(force) {
    if (roots && !force) return;
    fileList.replaceChildren(el("div", { class: "sp-msg muted" }, "Loading folders…"));
    try {
      const data = await api("/api/roots");
      roots = Array.isArray(data) ? data : (data.roots || []);
      rootsError = "";
    } catch (error) {
      roots = null;
      rootsError = error.message;
      fileList.replaceChildren(el("div", { class: "sp-msg error", role: "alert" }, error.message),
        el("button", { type: "button", class: "btn", onclick: () => loadRoots(true).then(browseNow) }, "Retry"));
      return;
    }
    rootSelect.replaceChildren();
    for (const r of roots) {
      rootSelect.append(el("button", { type: "button", class: "sp-chip", role: "radio", "data-root": r.name,
        "aria-checked": String(r.name === s.browse.root),
        onclick: () => { s.browse = { root: r.name, path: "" }; paintRoots(); browseNow(); } }, r.name));
    }
    if (!roots.length) {
      fileList.replaceChildren(el("div", { class: "sp-msg muted" }, "No shared folders are set up on the laptop."));
      return;
    }
    if (!s.browse.root || !roots.some((r) => r.name === s.browse.root)) s.browse = { root: roots[0].name, path: "" };
    paintRoots();
  }
  function paintRoots() {
    for (const b of rootSelect.querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.root === s.browse.root));
  }
  async function browseNow() {
    if (!roots || !roots.length || !s.browse.root) return;
    const mine = ++fileSeq;
    const { root: rt, path } = s.browse;
    crumb.textContent = `${rt} / ${path.split("/").filter(Boolean).join(" / ")}`.replace(/ \/ $/, "");
    fileList.replaceChildren(el("div", { class: "sp-msg muted" }, "Loading…"));
    try {
      const data = await api(`/api/files?${new URLSearchParams({ root: rt, path })}`);
      if (mine !== fileSeq) return;
      fileList.replaceChildren();
      if (path) {
        fileList.append(el("button", { type: "button", class: "sp-item sp-up", "aria-label": `Back to ${parentOf(path) || rt}`,
          onclick: () => { s.browse.path = parentOf(path); browseNow(); } }, icon("up"),
          el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, "Back"))));
      }
      const entries = data.entries || [];
      let shown = 0;
      for (const entry of entries) {
        const full = path ? `${path}/${entry.name}` : entry.name;
        if (entry.is_dir) {
          shown++;
          fileList.append(el("button", { type: "button", class: "sp-item", onclick: () => { s.browse.path = full; browseNow(); } },
            icon("folder"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, entry.name))));
        } else if (DOC_EXT.has(extOf(entry.name))) {
          shown++;
          const on = !!s.file && s.file.root === rt && s.file.path === full;
          fileList.append(el("button", { type: "button", class: "sp-item sp-file", "aria-pressed": String(on), "data-path": full,
            onclick: () => { s.file = { root: rt, path: full }; paintFileChosen(); changed(); } },
            icon("file"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, entry.name)),
            el("span", { class: "sp-mark" }, on ? icon("check") : null)));
        }
      }
      if (!shown) {
        fileList.append(el("div", { class: "sp-msg muted" }, "No folders or documents here. Documents are .md, .txt, .pdf and similar."));
      }
    } catch (error) {
      if (mine !== fileSeq) return;
      fileList.replaceChildren(el("div", { class: "sp-msg error", role: "alert" }, error.message),
        el("button", { type: "button", class: "btn", onclick: () => browseNow() }, "Retry"));
    }
  }
  function paintFileChosen() {
    fileChosen.textContent = s.file ? `Selected: ${s.file.root}/${s.file.path}` : "Open a folder and tap a document.";
    for (const item of fileList.querySelectorAll(".sp-file")) {
      const on = !!s.file && s.file.root === s.browse.root && item.dataset.path === s.file.path;
      item.setAttribute("aria-pressed", String(on));
      item.querySelector(".sp-mark").replaceChildren(...(on ? [icon("check")] : []));
    }
  }
  panels.file = el("div", { class: "sp-panel" }, rootSelect, crumb, fileChosen, fileList);

  // web page
  const urlInput = el("input", { id: `${id}-url`, type: "url", inputmode: "url", autocapitalize: "off", autocomplete: "off",
    spellcheck: "false", placeholder: "https://example.com/article", "aria-label": "Web address" });
  urlInput.addEventListener("input", () => { s.url = urlInput.value; changed(); });
  panels.url = el("div", { class: "sp-panel" },
    el("label", { class: "sp-label", for: `${id}-url` }, "Web address (http or https)"), urlInput,
    el("p", { class: "muted sp-small" }, "Only public web pages can be read."));

  // pasted text
  const textInput = el("textarea", { id: `${id}-text`, rows: "6", placeholder: "Paste the text you want to study", "aria-label": "Pasted text" });
  const textCount = el("div", { class: "muted sp-small", "aria-live": "off" });
  textInput.addEventListener("input", () => {
    s.text = textInput.value;
    textCount.textContent = s.text.trim() ? `${s.text.length.toLocaleString()} characters` : "";
    changed();
  });
  panels.text = el("div", { class: "sp-panel" },
    el("label", { class: "sp-label", for: `${id}-text` }, "Paste text"), textInput, textCount);

  panels.none = el("div", { class: "sp-panel" },
    el("p", { class: "muted" }, "No source. You can ask general questions."));

  for (const [key, panel] of Object.entries(panels)) {
    panel.dataset.panel = key;
    body.append(panel);
  }

  // ---- state
  function urlWithScheme(raw) {
    const t = raw.trim();
    if (!t) return "";
    return /^[a-z][a-z0-9+.-]*:/i.test(t) ? t : `https://${t}`;
  }
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
  function clearPreview() {
    previewSeq++;
    previewing = false;
    preview.replaceChildren();
    status.textContent = "";
    paintCheck();
  }
  function paintCheck() {
    const has = !!getSource();
    checkRow.hidden = s.type === "none";
    checkButton.disabled = !has || previewing;
    checkButton.textContent = previewing ? "Checking…" : "Check";
    checkButton.setAttribute("aria-busy", String(previewing));
  }
  function changed() {
    clearPreview();
    for (const fn of listeners) {
      try { fn(getSource()); } catch (error) { console.error(error); }
    }
  }
  function paintTypes() {
    for (const [tid, button] of Object.entries(typeButtons)) {
      const on = tid === s.type;
      button.setAttribute("aria-checked", String(on));
      button.tabIndex = on ? 0 : -1;
    }
    for (const [key, panel] of Object.entries(panels)) panel.hidden = key !== s.type;
  }
  function selectType(type) {
    if (type === s.type) return;
    s.type = type;
    paintTypes();
    if (type === "topic") loadTopics();
    if (type === "note") { paintNote(); if (!notesLoaded) loadNotes(); }
    if (type === "file") { paintFileChosen(); loadRoots().then(browseNow); }
    changed();
  }

  async function check() {
    const source = getSource();
    if (!source || previewing) return;
    const mine = ++previewSeq;
    previewing = true;
    paintCheck();
    preview.replaceChildren(el("div", { class: "sp-msg muted" }, "Reading the source…"));
    try {
      const data = await api("/api/learn/source/preview", { method: "POST", json: { source } });
      if (mine !== previewSeq) return;
      previewing = false;
      paintCheck();
      const lines = String(data.excerpt || "").split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 6).join("\n");
      preview.replaceChildren(el("div", { class: "sp-card" },
        el("div", { class: "sp-card-head" }, icon("check", "sp-ok"), el("strong", { class: "sp-card-title" }, data.title || "Untitled")),
        data.chars ? el("div", { class: "muted sp-small" }, `${Number(data.chars).toLocaleString()} characters`) : null,
        lines ? el("pre", { class: "sp-excerpt" }, lines) : null));
      status.textContent = "Source checked.";
    } catch (error) {
      if (mine !== previewSeq) return;
      previewing = false;
      paintCheck();
      preview.replaceChildren(el("div", { class: "sp-card sp-bad", role: "alert" }, icon("alert"), el("span", {}, error.message)));
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
    else if (source.type === "text") { s.text = source.text || ""; textInput.value = s.text; textInput.dispatchEvent(new Event("input")); return paintAfterSet(); }
    paintAfterSet();
  }
  function paintAfterSet() {
    paintTypes();
    paintNote();
    paintFileChosen();
    paintSuggest();
    changed();
  }

  paintTypes();
  paintNote();
  paintFileChosen();
  paintCheck();
  if (s.type === "topic") loadTopics();

  return {
    getSource,
    setSource,
    label,
    onChange(fn) { listeners.push(fn); },
    // For the host: put the keyboard focus on the first control.
    focus() { (typeButtons[s.type] || typeBar.querySelector("button"))?.focus(); },
  };
}
