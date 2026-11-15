// Study library UI for Learn: the "Add material" sheet (this device | the laptop's shared folders | paste or link)
// and a multi-select list of the library's documents. Routes: /api/learn/library/* (docs/api/studylib.md).
// Names and text from files and web pages go on screen with textContent only, never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const KINDS = {
  pdf: "PDF", docx: "Word", pptx: "Slides", epub: "EPUB", txt: "Text", md: "Text", markdown: "Text", html: "Web", htm: "Web",
  png: "Image", jpg: "Image", jpeg: "Image", webp: "Image",
};
const FILTERS = [
  { id: "all", label: "All", exts: null },
  { id: "pdf", label: "PDF", exts: ["pdf"] },
  { id: "word", label: "Word", exts: ["docx"] },
  { id: "slides", label: "Slides", exts: ["pptx"] },
  { id: "text", label: "Text", exts: ["txt", "md", "markdown", "rst", "html", "htm", "epub"] },
];
const FALLBACK_EXTS = ["pdf", "docx", "pptx", "epub", "txt", "md", "markdown", "rst", "org", "csv", "log", "html", "htm", "png", "jpg", "jpeg", "webp"];
const FALLBACK_LIMIT = 100 * 1024 * 1024;
const IMAGE_LIMIT = 15 * 1024 * 1024;

const extOf = (name) => { const i = String(name).lastIndexOf("."); return i < 0 ? "" : name.slice(i + 1).toLowerCase(); };
export function bytes(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(n >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}
const when = (iso) => { try { return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); } catch (_) { return ""; } };

let limitsCache = null;
async function limits() {
  if (limitsCache) return limitsCache;
  try {
    const data = await api("/api/learn/library");
    limitsCache = data.limits || {};
  } catch (_) { limitsCache = {}; }
  return limitsCache;
}

// ------------------------------------------------------------------------------------------ add material

// Upload one file with a progress callback (fetch cannot report upload progress, XHR can).
function uploadOne(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/learn/library/upload");
    xhr.setRequestHeader("X-Hub", "1");
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onerror = () => reject(new Error("Can't reach your laptop. Is the hub running and the laptop awake?"));
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
      if (xhr.status === 401) window.dispatchEvent(new Event("hub:unauthorized"));
      if (xhr.status >= 200 && xhr.status < 300) resolve(body || {});
      else reject(new Error(typeof body?.detail === "string" ? body.detail : `The laptop answered with an error (${xhr.status}).`));
    };
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

export function openAddMaterial(ctx, { onDone } = {}) {
  let added = 0;
  const root = el("div", { class: "sl-add ui-stack" });
  const seg = ui.segmented({ label: "Where from", block: true, value: "device", onChange: (v) => { show(v); if (v === "laptop") loadRoots(); },
    options: [{ value: "device", label: "This device" }, { value: "laptop", label: "Laptop files" }, { value: "paste", label: "Paste or link" }] });
  const panels = { device: el("div", { class: "sl-panel ui-stack" }), laptop: el("div", { class: "sl-panel ui-stack" }), paste: el("div", { class: "sl-panel ui-stack" }) };
  root.append(seg, ...Object.values(panels));
  const show = (id) => { for (const [k, p] of Object.entries(panels)) p.hidden = k !== id; };
  show("device");

  // ---- this device (phone picker, or drag and drop on a computer)
  const queue = el("div", { class: "sl-queue", "aria-live": "polite" });
  const input = el("input", { type: "file", multiple: true, class: "sl-file-input", tabindex: "-1", "aria-label": "Choose files to add",
    accept: [...FALLBACK_EXTS.map((e) => "." + e), "image/*"].join(",") });
  const chooseBtn = ui.button({ label: "Choose files", icon: "upload", variant: "primary", onClick: () => input.click() });
  const drop = el("div", { class: "sl-drop", tabindex: "-1" },
    el("p", { class: "sl-drop-title" }, "Add PDFs, Word, slides, text and screenshots"),
    el("p", { class: "sl-small" }, "Pick many at once. On a computer you can also drag files here. Up to 100 MB each (images 15 MB)."),
    chooseBtn);
  for (const evt of ["dragenter", "dragover"]) drop.addEventListener(evt, (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  for (const evt of ["dragleave", "drop"]) drop.addEventListener(evt, (e) => { e.preventDefault(); drop.classList.remove("is-over"); });
  drop.addEventListener("drop", (e) => { if (e.dataTransfer?.files?.length) enqueue([...e.dataTransfer.files]); });
  input.addEventListener("change", () => { enqueue([...input.files]); input.value = ""; });
  panels.device.append(drop, input, queue);

  const rows = [];
  let running = false;
  async function enqueue(list) {
    const lim = await limits();
    const allowed = new Set(lim.extensions || FALLBACK_EXTS);
    for (const file of list) {
      const ext = extOf(file.name);
      const cap = ["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ext) ? (lim.max_image_bytes || IMAGE_LIMIT) : (lim.max_bytes || FALLBACK_LIMIT);
      const row = makeRow(file);
      rows.push(row);
      queue.append(row.node);
      if (!allowed.has(ext)) row.fail(`.${ext || "?"} files can't be added. Use PDF, Word, PowerPoint, EPUB, text, HTML or an image.`, false);
      else if (file.size > cap) row.fail(`That file is ${bytes(file.size)}; the limit is ${bytes(cap)}.`, false);
      else if (file.size === 0) row.fail("That file is empty.", false);
    }
    pump();
  }
  function makeRow(file) {
    const status = el("span", { class: "sl-row-status" }, "Waiting");
    const bar = ui.progressBar(0, `Uploading ${file.name}`);
    const retryBtn = ui.button({ label: "Retry", icon: "refresh", variant: "tonal", size: "sm", hidden: true, onClick: () => { row.state = "waiting"; retryBtn.hidden = true; bar.hidden = false; status.textContent = "Waiting"; pump(); } });
    const node = el("div", { class: "sl-row" },
      el("div", { class: "sl-row-main" }, el("span", { class: "sl-row-name ui-truncate" }, file.name), el("span", { class: "sl-small" }, bytes(file.size))),
      bar, status, retryBtn);
    const row = { file, node, state: "waiting",
      progress(v) { bar.set(v); status.textContent = v >= 1 ? "Reading…" : `Uploading ${Math.round(v * 100)}%`; },
      done(doc) { row.state = "done"; bar.hidden = true; status.textContent = doc?.duplicate ? "Already in your library" : "Added. Reading it now"; status.dataset.tone = "good"; },
      fail(message, retryable = true) { row.state = retryable ? "failed" : "rejected"; bar.hidden = true; status.textContent = message; status.dataset.tone = "bad"; retryBtn.hidden = !retryable; } };
    return row;
  }
  async function pump() {
    if (running) return;
    running = true;
    try {
      for (;;) {
        const row = rows.find((r) => r.state === "waiting");
        if (!row) break;
        row.state = "uploading";
        try {
          const data = await uploadOne(row.file, (v) => row.progress(v));
          row.done(data.doc);
          added++;
        } catch (error) { row.fail(error.message, true); }
      }
    } finally { running = false; }
  }

  // ---- laptop (the Files tab's shared folders; files are read in place, nothing is uploaded)
  const chosen = new Map();        // "root/path" -> {root, path, size}
  let roots = null;
  let cur = { root: "", path: "" };
  let filterId = "all";
  const rootRow = el("div", { class: "sp-chiprow", role: "radiogroup", "aria-label": "Shared folder" });
  const filterRow = el("div", { class: "sp-chiprow", role: "group", "aria-label": "File type" });
  const crumb = el("div", { class: "sp-crumb" });
  const list = el("div", { class: "sp-list sl-files", "aria-label": "Files on the laptop" });
  const summary = el("div", { class: "sl-summary", "aria-live": "polite" });
  const selectAll = ui.button({ label: "Select all files shown", icon: "check", variant: "ghost", size: "sm", onClick: () => toggleAll() });
  const importBtn = ui.button({ label: "Add selected", icon: "plus", variant: "primary", disabled: true, onClick: () => importChosen() });
  const result = el("div", { class: "sl-result ui-stack", "aria-live": "polite" });
  let shown = [];
  const keyOf = (root, path) => `${root}/${path}`;
  const parentOf = (p) => { const a = p.split("/").filter(Boolean); a.pop(); return a.join("/"); };

  for (const f of FILTERS) {
    filterRow.append(el("button", { type: "button", class: "ui-chip" + (f.id === filterId ? " is-on" : ""), "aria-pressed": String(f.id === filterId), "data-filter": f.id,
      onclick: () => { filterId = f.id; for (const b of filterRow.children) { const on = b.dataset.filter === f.id; b.classList.toggle("is-on", on); b.setAttribute("aria-pressed", String(on)); } browse(); } }, f.label));
  }
  async function loadRoots() {
    if (roots) return;
    list.replaceChildren(ui.skeleton("list", 3));
    try {
      const data = await api("/api/roots");
      roots = Array.isArray(data) ? data : (data.roots || []);
    } catch (error) { list.replaceChildren(ui.notice(error.message, { tone: "bad" })); return; }
    rootRow.replaceChildren();
    for (const r of roots) {
      rootRow.append(el("button", { type: "button", class: "ui-chip", role: "radio", "data-root": r.name, "aria-checked": "false",
        onclick: () => { cur = { root: r.name, path: "" }; paintRoots(); browse(); } }, r.name));
    }
    if (!roots.length) { list.replaceChildren(ui.emptyState({ icon: "folder", compact: true, title: "No shared folders", body: "Share a folder on the laptop first (Files tab)." })); return; }
    cur = { root: roots[0].name, path: "" };
    paintRoots();
    browse();
  }
  function paintRoots() { for (const b of rootRow.children) { const on = b.dataset.root === cur.root; b.setAttribute("aria-checked", String(on)); b.classList.toggle("is-on", on); } }
  function allowedHere(entry) {
    const ext = extOf(entry.name);
    if (!FALLBACK_EXTS.includes(ext)) return false;
    const f = FILTERS.find((x) => x.id === filterId);
    return !f.exts || f.exts.includes(ext);
  }
  let seq = 0;
  async function browse() {
    if (!cur.root) return;
    const mine = ++seq;
    crumb.textContent = [cur.root, ...cur.path.split("/").filter(Boolean)].join(" / ");
    list.replaceChildren(ui.skeleton("list", 3));
    try {
      const data = await api(`/api/files?${new URLSearchParams({ root: cur.root, path: cur.path })}`);
      if (mine !== seq) return;
      list.replaceChildren();
      shown = [];
      if (cur.path) list.append(el("button", { type: "button", class: "sp-item", onclick: () => { cur.path = parentOf(cur.path); browse(); } }, ui.tile("arrow-left", "gray", "sm"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, "Back"))));
      let any = false;
      for (const entry of data.entries || []) {
        const full = cur.path ? `${cur.path}/${entry.name}` : entry.name;
        if (entry.is_dir) {
          any = true;
          list.append(el("button", { type: "button", class: "sp-item", onclick: () => { cur.path = full; browse(); } },
            ui.tile("folder", "warn", "sm"), el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, entry.name)), ui.icon("chevron-right", { class: "sp-chev" })));
        } else if (allowedHere(entry)) {
          any = true;
          shown.push({ root: cur.root, path: full, size: entry.size || 0 });
          const on = chosen.has(keyOf(cur.root, full));
          const btn = el("button", { type: "button", class: "sp-item sp-file", role: "checkbox", "aria-checked": String(on), "data-key": keyOf(cur.root, full),
            onclick: () => toggle(cur.root, full, entry.size || 0, btn) },
          ui.tile("file", "info", "sm"),
          el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, entry.name), el("span", { class: "sp-item-sub" }, `${KINDS[extOf(entry.name)] || "File"} · ${bytes(entry.size || 0)}`)),
          el("span", { class: "sp-mark" }, on ? ui.icon("check") : null));
          list.append(btn);
        }
      }
      if (!any) list.append(ui.emptyState({ icon: "folder", compact: true, title: "Nothing to add here", body: "Open a folder, or change the file type above." }));
      paintSummary();
    } catch (error) {
      if (mine !== seq) return;
      list.replaceChildren(ui.notice(error.message, { tone: "bad" }));
    }
  }
  function paintBtn(btn, on) { btn.setAttribute("aria-checked", String(on)); btn.querySelector(".sp-mark")?.replaceChildren(...(on ? [ui.icon("check")] : [])); }
  function toggle(root, path, size, btn) {
    const k = keyOf(root, path);
    if (chosen.has(k)) chosen.delete(k); else chosen.set(k, { root, path, size });
    paintBtn(btn, chosen.has(k));
    paintSummary();
  }
  function toggleAll() {
    const every = shown.length && shown.every((f) => chosen.has(keyOf(f.root, f.path)));
    for (const f of shown) { if (every) chosen.delete(keyOf(f.root, f.path)); else chosen.set(keyOf(f.root, f.path), f); }
    for (const b of list.querySelectorAll(".sp-file")) paintBtn(b, chosen.has(b.dataset.key));
    paintSummary();
  }
  function paintSummary() {
    const total = [...chosen.values()].reduce((a, f) => a + f.size, 0);
    summary.textContent = chosen.size ? `${chosen.size} file${chosen.size === 1 ? "" : "s"} selected, ${bytes(total)}` : "Tick the files to add.";
    importBtn.disabled = !chosen.size;
    selectAll.hidden = !shown.length;
  }
  async function importChosen() {
    if (!chosen.size) return;
    importBtn.disabled = true;
    importBtn.classList.add("is-loading");
    result.replaceChildren();
    const byRoot = new Map();
    for (const f of chosen.values()) { if (!byRoot.has(f.root)) byRoot.set(f.root, []); byRoot.get(f.root).push(f.path); }
    const errors = [];
    let ok = 0;
    let dupes = 0;
    for (const [root, paths] of byRoot) {
      for (let i = 0; i < paths.length; i += 50) {
        const batch = paths.slice(i, i + 50);
        try {
          const data = await api("/api/learn/library/import", { method: "POST", json: { root, paths: batch } });
          for (const r of data.results || []) {
            if (r.ok) { ok++; if (r.doc?.duplicate) dupes++; chosen.delete(keyOf(root, r.path)); } else errors.push(`${r.path.split("/").pop()}: ${r.error}`);
          }
        } catch (error) { for (const p of batch) errors.push(`${p.split("/").pop()}: ${error.message}`); }
      }
    }
    added += ok;
    importBtn.classList.remove("is-loading");
    for (const b of list.querySelectorAll(".sp-file")) paintBtn(b, chosen.has(b.dataset.key));
    paintSummary();
    result.replaceChildren(
      ok ? ui.notice(`${ok} file${ok === 1 ? "" : "s"} added${dupes ? ` (${dupes} already there)` : ""}. They are being read now.`, { tone: "good" }) : null,
      ...errors.slice(0, 8).map((m) => ui.notice(m, { tone: "bad" })),
      errors.length > 8 ? el("p", { class: "sl-small" }, `...and ${errors.length - 8} more could not be added. Tick them again to retry.`) : null);
  }
  panels.laptop.append(
    el("p", { class: "sl-small" }, "Choose files from the folders you share on the laptop. The hub reads them there; nothing is uploaded."),
    rootRow, filterRow, crumb, list, el("div", { class: "sl-bar" }, summary, selectAll), importBtn, result);
  paintSummary();

  // ---- paste text or a link
  const titleIn = el("input", { type: "text", maxlength: "200", placeholder: "Name (optional)", "aria-label": "Name" });
  const textIn = el("textarea", { rows: "5", placeholder: "Paste the text you want to study", "aria-label": "Pasted text" });
  const urlIn = el("input", { type: "url", inputmode: "url", autocapitalize: "off", autocomplete: "off", spellcheck: "false", placeholder: "or a web address, https://...", "aria-label": "Web address" });
  const pasteOut = el("div", { class: "sl-result ui-stack", "aria-live": "polite" });
  const pasteBtn = ui.button({ label: "Add to library", icon: "plus", variant: "primary", onClick: async () => {
    const url = urlIn.value.trim();
    const text = textIn.value.trim();
    if (!url && !text) { pasteOut.replaceChildren(ui.notice("Paste some text or a web address first.", { tone: "warn" })); return; }
    pasteBtn.disabled = true;
    try {
      const json = { title: titleIn.value.trim() };
      if (url) json.url = /^[a-z][a-z0-9+.-]*:/i.test(url) ? url : `https://${url}`; else json.text = text;
      const data = await api("/api/learn/library/paste", { method: "POST", json });
      added++;
      textIn.value = ""; urlIn.value = ""; titleIn.value = "";
      pasteOut.replaceChildren(ui.notice(data.doc?.duplicate ? "That is already in your library." : "Added. It is being read now.", { tone: "good" }));
    } catch (error) { pasteOut.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
    pasteBtn.disabled = false;
  } });
  panels.paste.append(titleIn, textIn, urlIn, el("p", { class: "sl-small" }, "Only public web pages (http or https) can be read."), pasteBtn, pasteOut);

  return ui.sheet({ title: "Add material", content: root, class: "sp-sheet",
    actions: [{ label: "Done", variant: "primary", icon: "check" }],
    onClose: () => { if (added) onDone?.(added); } });
}

// ------------------------------------------------------------------------------------------ library list

// A multi-select list of library documents. `onChange(ids, range)` fires when the selection or page range changes.
export function createLibraryPicker(ctx, { onChange, selectable = true, single = false } = {}) {
  const root = el("div", { class: "sl-lib ui-stack" });
  const search = el("input", { type: "search", placeholder: "Search your library", "aria-label": "Search library", autocomplete: "off", enterkeyhint: "search" });
  const addBtn = ui.button({ label: "Add material", icon: "upload", variant: "tonal", size: "sm", onClick: () => openAddMaterial(ctx, { onDone: () => load() }) });
  const head = el("div", { class: "sl-head" }, search, addBtn);
  const list = el("div", { class: "sp-list sl-docs", role: "group", "aria-label": "Library documents" });
  const rangeBox = el("div", { class: "sl-range", hidden: true });
  const from = el("input", { type: "number", min: "1", inputmode: "numeric", "aria-label": "From page", placeholder: "1" });
  const to = el("input", { type: "number", min: "1", inputmode: "numeric", "aria-label": "To page", placeholder: "end" });
  const unitLabel = el("span", { class: "sl-small" }, "Pages");
  rangeBox.append(unitLabel, from, el("span", { class: "sl-small" }, "to"), to);
  root.append(head, list, rangeBox);

  const picked = new Set();
  let docs = [];
  let timer = null;
  let seq = 0;
  let loaded = false;

  const fire = () => { try { onChange?.(getIds(), getRange()); } catch (e) { console.error(e); } };
  const getIds = () => [...picked].filter((id) => docs.some((d) => d.id === id && d.status === "ready"));
  const getRange = () => ({ page_from: Number(from.value) > 0 ? Math.floor(Number(from.value)) : undefined, page_to: Number(to.value) > 0 ? Math.floor(Number(to.value)) : undefined });
  from.addEventListener("input", fire);
  to.addEventListener("input", fire);
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });

  function sub(d) {
    const bits = [];
    if (d.status === "ready") bits.push(`${d.pages} ${d.unit}${d.pages === 1 ? "" : "s"}`, when(d.added));
    if (d.subject) bits.push(d.subject);
    if (d.tags?.length) bits.push(d.tags.join(", "));
    return bits.filter(Boolean).join(" · ");
  }
  function row(d) {
    const ready = d.status === "ready";
    const on = picked.has(d.id);
    const main = el("span", { class: "sp-item-main" }, el("span", { class: "sp-item-title" }, d.title), el("span", { class: "sp-item-sub" }, sub(d)));
    const extra = [];
    if (d.status === "queued" || d.status === "indexing") {
      const bar = ui.progressBar(d.progress || 0, `Reading ${d.title}`);
      extra.push(bar, ui.button({ label: "Cancel", icon: "close", variant: "ghost", size: "sm", onClick: () => act(`/${d.id}/cancel`, "POST") }));
    } else if (d.status === "error" || d.status === "cancelled") {
      main.append(el("span", { class: "sl-err" }, d.error || "Couldn't be read."));
      extra.push(ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => act(`/${d.id}/reindex`, "POST") }));
    }
    const pick = el("button", { type: "button", class: "sp-item sp-file", role: selectable ? "checkbox" : "button", "aria-checked": String(on), disabled: !ready && selectable,
      onclick: () => {
        if (single) picked.clear();
        if (picked.has(d.id)) picked.delete(d.id); else picked.add(d.id);
        paint(); fire();
      } }, ui.tile("file", ready ? "info" : "gray", "sm"), main, el("span", { class: "sp-mark" }, on ? ui.icon("check") : null));
    const more = ui.button({ label: `Manage ${d.title}`, icon: "more", variant: "ghost", size: "sm", iconOnly: true, onClick: () => manage(d) });
    return el("div", { class: "sl-doc" }, pick, ...extra, more);
  }
  function paint() {
    list.replaceChildren();
    if (!docs.length) {
      list.append(ui.emptyState({ icon: "book-open", compact: true, title: search.value ? "Nothing matches" : "Your study library is empty",
        body: search.value ? "Try another word." : "Add PDFs and documents from this device or the laptop, then study from them here.",
        action: search.value ? undefined : { label: "Add material", icon: "upload", onClick: () => openAddMaterial(ctx, { onDone: () => load() }) } }));
    }
    for (const d of docs) list.append(row(d));
    const ids = getIds();
    const one = ids.length === 1 ? docs.find((d) => d.id === ids[0]) : null;
    rangeBox.hidden = !one;
    unitLabel.textContent = one ? `${one.unit[0].toUpperCase()}${one.unit.slice(1)}s (of ${one.pages})` : "Pages";
    if (!one) { from.value = ""; to.value = ""; }
  }
  async function act(path, method) {
    try { await api(`/api/learn/library${path}`, { method }); } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    load();
  }
  function manage(d) {
    const name = el("input", { type: "text", maxlength: "200", value: d.title, "aria-label": "Name" });
    const subject = el("input", { type: "text", maxlength: "60", value: d.subject || "", placeholder: "Subject, like Networking", "aria-label": "Subject" });
    const tags = el("input", { type: "text", value: (d.tags || []).join(", "), placeholder: "Tags, separated by commas", "aria-label": "Tags" });
    const sheet = ui.sheet({ title: "Manage document", content: el("div", { class: "ui-stack" }, name, subject, tags,
      el("p", { class: "sl-small" }, `${d.filename || d.source} · ${bytes(d.size || 0)}${d.search ? ` · searched by ${d.search}` : ""}`)),
    actions: [
      { label: "Delete", variant: "danger-soft", icon: "trash", async onClick() {
        try { await api(`/api/learn/library/${d.id}`, { method: "DELETE" }); picked.delete(d.id); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        load(); fire();
      } },
      { label: "Re-index", variant: "tonal", icon: "refresh", async onClick() {
        try { await api(`/api/learn/library/${d.id}/reindex`, { method: "POST" }); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        load();
      } },
      { label: "Save", variant: "primary", icon: "check", async onClick() {
        try {
          await api(`/api/learn/library/${d.id}`, { method: "PATCH", json: { title: name.value, subject: subject.value, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) } });
        } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        load();
      } },
    ] });
    return sheet;
  }
  async function load() {
    clearTimeout(poll);
    const mine = ++seq;
    if (!loaded) list.replaceChildren(ui.skeleton("list", 3));
    try {
      const q = search.value.trim();
      const data = await api("/api/learn/library" + (q ? `?q=${encodeURIComponent(q)}` : ""));
      if (mine !== seq) return;
      loaded = true;
      docs = data.docs || [];
      for (const id of [...picked]) if (!docs.some((d) => d.id === id)) picked.delete(id);
      paint();
      fire();
      if (data.busy && root.isConnected) poll = setTimeout(load, 1500);
    } catch (error) {
      if (mine !== seq) return;
      list.replaceChildren(ui.notice(error.message, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: load }));
    }
  }
  let poll = null;
  load();

  return {
    root, refresh: load,
    getIds, getRange,
    select(ids) { picked.clear(); for (const id of ids || []) picked.add(id); paint(); },
    count: () => docs.length,
    docs: () => docs,
  };
}
