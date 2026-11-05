// Full-screen text and markdown editor for the Files tab. Saves through /api/fileops/write with the
// etag from /api/fileops/read, so a file that changed on the laptop is never overwritten silently.
//
//   import { openEditor } from "./fileeditor.js";
//   openEditor({ root, path, onSaved({path, etag, copy}), onClose({dirty:false}) })  -> { close() }
import { api, el, renderMarkdown, fileUrl } from "./api.js";
import * as ui from "./ui.js";

const KEY_MONO = "hub.files.editor.mono";
const DRAFT_PREFIX = "hub.draft.";
const MARKDOWN = new Set(["md", "markdown"]);

const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } },
  del(key) { try { localStorage.removeItem(key); } catch (_) { /* private mode */ } },
};

const baseName = (p) => p.split("/").pop() || p;
const dirOf = (p) => p.split("/").slice(0, -1).join("/");
const extOf = (p) => {
  const n = baseName(p);
  const dot = n.lastIndexOf(".");
  return dot > 0 ? n.slice(dot + 1).toLowerCase() : "";
};

function countWords(text) {
  const m = text.match(/\S+/g);
  return m ? m.length : 0;
}

function copyName(name, n) {
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  return n <= 1 ? `${stem} (copy)${ext}` : `${stem} (copy ${n})${ext}`;
}

// Resolve a relative image address from the document, without leaving the shared folder.
function resolveRel(docPath, href) {
  const parts = dirOf(docPath).split("/").filter(Boolean);
  for (const seg of href.split("#")[0].split("?")[0].split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") { if (!parts.length) return null; parts.pop(); } else parts.push(seg);
  }
  return parts.join("/");
}

export function openEditor({ root, path, onSaved, onClose } = {}) {
  const isMd = MARKDOWN.has(extOf(path));
  let etag = null;
  let base = "";
  let text = "";
  let ready = false;
  let closed = false;
  let saving = false;
  let draftTimer = null;
  let previewTimer = null;
  let release = null;
  let guardOpen = false;
  let mono = store.get(KEY_MONO, isMd ? "0" : "1") === "1";
  let pane = "write";
  let curPath = path;
  const draftKey = () => `${DRAFT_PREFIX}${root}:${curPath}`;
  const isDirty = () => ready && text !== base;

  // ---- DOM ---------------------------------------------------------------------------------
  const closeBtn = ui.button({ label: "Close editor", icon: "close", variant: "ghost", iconOnly: true });
  const titleEl = el("h2", { class: "fe-title" }, baseName(path));
  const dotEl = el("span", { class: "fe-dot", hidden: true, title: "Unsaved changes" });
  const subEl = el("div", { class: "fe-sub" }, `${root}/${path}`);
  const saveBtn = ui.button({ label: "Save", icon: "check", variant: "primary", size: "sm", disabled: true });
  const head = el("header", { class: "fe-head" }, closeBtn,
    el("div", { class: "fe-headtext" }, el("div", { class: "fe-titlerow" }, titleEl, dotEl), subEl), saveBtn);

  const monoBtn = el("button", { type: "button", class: "ui-chip fe-mono", "aria-pressed": String(mono) },
    ui.icon("code"), "Mono");
  const seg = isMd ? ui.segmented({
    options: [{ value: "write", label: "Write" }, { value: "preview", label: "Preview" }],
    value: "write", label: "Editor view", onChange: (v) => { pane = v; syncPane(); if (v === "preview") renderPreview(); },
  }) : null;
  const tools = el("div", { class: "fe-tools" }, seg, monoBtn);

  const notice = el("div", { class: "fe-notice", hidden: true });
  const area = el("textarea", {
    class: "fe-area", "aria-label": `Edit ${baseName(path)}`, spellcheck: "false", autocapitalize: "off",
    autocomplete: "off", autocorrect: "off", disabled: true,
  });
  const preview = el("div", { class: "fe-preview markdown", "aria-label": "Preview" });
  const body = el("div", { class: "fe-body" }, area, isMd ? preview : null);

  const statusEl = el("span", { class: "fe-status", role: "status" }, "Opening...");
  const countEl = el("span", { class: "fe-count ui-num" }, "0 words");
  const foot = el("footer", { class: "fe-foot" }, statusEl, countEl);

  const node = el("div", { class: "fe", role: "dialog", "aria-modal": "true", "aria-label": `Editing ${baseName(path)}`, "data-md": isMd ? "1" : "0" },
    head, tools, notice, body, foot);
  node.classList.toggle("is-mono", mono);
  document.body.append(node);
  syncPane();

  function syncPane() {
    node.dataset.pane = pane;
  }

  function showNotice(content, tone) {
    notice.replaceChildren(...[].concat(content));
    notice.dataset.tone = tone || "info";
    notice.hidden = false;
  }

  function refreshMeta() {
    const dirty = isDirty();
    dotEl.hidden = !dirty;
    saveBtn.disabled = !ready || !dirty || saving;
    node.classList.toggle("is-dirty", dirty);
    const words = countWords(text);
    countEl.textContent = `${words.toLocaleString()} word${words === 1 ? "" : "s"} · ${text.length.toLocaleString()} chars`;
    if (!ready || saving) return;
    statusEl.textContent = dirty ? "Unsaved changes" : "All changes saved";
    statusEl.dataset.state = dirty ? "dirty" : "saved";
  }

  function renderPreview() {
    previewTimer = null;
    if (!isMd) return;
    if (!text.trim()) {
      preview.replaceChildren(el("p", { class: "fe-previewempty" }, "Nothing to preview yet."));
      return;
    }
    const parsed = new DOMParser().parseFromString(renderMarkdown(text), "text/html");
    for (const img of parsed.body.querySelectorAll("img")) {
      const src = img.getAttribute("src") || "";
      if (src && !/^[a-z][a-z0-9+.-]*:/i.test(src) && !src.startsWith("//")) {
        const resolved = resolveRel(curPath, src);
        if (resolved) img.setAttribute("src", fileUrl(root, resolved)); else img.removeAttribute("src");
      }
    }
    for (const a of parsed.body.querySelectorAll("a[href]")) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
    preview.replaceChildren(...parsed.body.childNodes);
  }

  function queuePreview() {
    if (!isMd) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, 120);
  }

  function queueDraft() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => {
      if (isDirty()) store.set(draftKey(), JSON.stringify({ text, etag, at: Date.now() }));
      else store.del(draftKey());
    }, 500);
  }

  area.addEventListener("input", () => {
    text = area.value;
    refreshMeta();
    queueDraft();
    queuePreview();
  });
  area.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); save(); }
    if (event.key === "Tab" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) {
      event.preventDefault();
      const { selectionStart: s, selectionEnd: e } = area;
      area.setRangeText("  ", s, e, "end");
      area.dispatchEvent(new Event("input"));
    }
  });
  monoBtn.addEventListener("click", () => {
    mono = !mono;
    store.set(KEY_MONO, mono ? "1" : "0");
    monoBtn.setAttribute("aria-pressed", String(mono));
    node.classList.toggle("is-mono", mono);
  });

  // ---- load ---------------------------------------------------------------------------------
  function setText(value) {
    text = value;
    area.value = value;
    refreshMeta();
    renderPreview();
  }

  async function load({ keepDraft = true } = {}) {
    statusEl.textContent = "Opening...";
    try {
      const data = await api(`/api/fileops/read?${new URLSearchParams({ root, path: curPath })}`);
      etag = data.etag;
      base = data.content;
      ready = true;
      area.disabled = false;
      notice.hidden = true;
      let draft = null;
      if (keepDraft) {
        try { draft = JSON.parse(store.get(draftKey()) || "null"); } catch (_) { draft = null; }
      }
      if (draft && typeof draft.text === "string" && draft.text !== base) {
        setText(draft.text);
        const stale = draft.etag && draft.etag !== etag;
        const discard = ui.button({ label: "Discard draft", variant: "ghost", size: "sm", onClick: () => {
          store.del(draftKey());
          setText(base);
          notice.hidden = true;
          queueDraft();
        } });
        showNotice([el("span", { class: "grow" }, stale
          ? "Restored your unsaved draft. The file changed on the laptop since, so saving will ask what to do."
          : "Restored your unsaved draft."), discard], stale ? "warn" : "info");
      } else {
        store.del(draftKey());
        setText(base);
      }
      refreshMeta();
      if (!draft) area.focus({ preventScroll: true });
    } catch (error) {
      ready = false;
      area.disabled = true;
      statusEl.textContent = "Could not open";
      showNotice([el("span", { class: "grow" }, error.status === 415
        ? "This file is not plain text, so it can't be edited here."
        : error.status === 413 ? "This file is bigger than 2 MB, too big to edit here." : error.message), ui.button({
        label: "Close", variant: "secondary", size: "sm", onClick: () => finish(),
      })], "bad");
    }
  }

  // ---- save ----------------------------------------------------------------------------------
  async function save() {
    if (!ready || saving || !isDirty()) return true;
    saving = true;
    const sent = text;
    statusEl.textContent = "Saving...";
    statusEl.dataset.state = "saving";
    saveBtn.classList.add("is-loading");
    saveBtn.disabled = true;
    try {
      const result = await api("/api/fileops/write", {
        method: "POST", json: { root, path: curPath, content: sent, etag },
      });
      etag = result.etag;
      base = sent;
      if (text === sent) store.del(draftKey()); else queueDraft();
      notice.hidden = true;
      ui.haptic("success");
      onSaved?.({ path: curPath, etag, copy: false });
      return true;
    } catch (error) {
      if (error.status === 409) conflict(sent);
      else { ui.toast(error.message, { kind: "bad" }); statusEl.textContent = "Not saved"; statusEl.dataset.state = "dirty"; }
      return false;
    } finally {
      saving = false;
      saveBtn.classList.remove("is-loading");
      refreshMeta();
    }
  }
  saveBtn.addEventListener("click", () => save());

  function conflict(mine) {
    statusEl.textContent = "Not saved: file changed";
    statusEl.dataset.state = "dirty";
    ui.sheet({
      title: "This file changed on the laptop",
      content: [
        el("p", { class: "fe-sheetp" }, `“${baseName(curPath)}” was edited somewhere else after you opened it. Nothing was overwritten, and your edits are still here.`),
        el("p", { class: "fe-sheetp fe-sheetp--dim" }, "Reload shows the laptop's version and drops your edits. Save as copy keeps your edits in a new file next to it."),
      ],
      actions: [
        { label: "Reload", icon: "refresh", variant: "secondary", onClick: async () => {
          store.del(draftKey());
          await load({ keepDraft: false });
        } },
        { label: "Save as copy", icon: "copy", variant: "primary", onClick: async () => {
          const ok = await saveAsCopy(mine);
          return ok ? undefined : false;
        } },
      ],
    });
  }

  async function saveAsCopy(content) {
    const name = baseName(curPath);
    const folder = dirOf(curPath);
    for (let n = 1; n <= 20; n += 1) {
      const target = folder ? `${folder}/${copyName(name, n)}` : copyName(name, n);
      try {
        const result = await api("/api/fileops/write", {
          method: "POST", json: { root, path: target, content, create_only: true },
        });
        store.del(draftKey());
        curPath = target;
        etag = result.etag;
        base = content;
        titleEl.textContent = baseName(target);
        subEl.textContent = `${root}/${target}`;
        ui.toast(`Saved as ${baseName(target)}`, { kind: "good" });
        onSaved?.({ path: target, etag, copy: true });
        refreshMeta();
        return true;
      } catch (error) {
        if (error.status !== 409) { ui.toast(error.message, { kind: "bad" }); return false; }
      }
    }
    ui.toast("Couldn't find a free name for the copy.", { kind: "bad" });
    return false;
  }

  // ---- closing -------------------------------------------------------------------------------
  function finish() {
    if (closed) return;
    closed = true;
    clearTimeout(draftTimer);
    clearTimeout(previewTimer);
    window.removeEventListener("beforeunload", onUnload);
    document.removeEventListener("keydown", onKey, true);
    if (release) { const r = release; release = null; r(); }
    node.remove();
    onClose?.({ path: curPath });
  }

  function requestClose(fromHistory) {
    if (closed) return;
    if (!isDirty()) { if (fromHistory) release = null; finish(); return; }
    if (guardOpen) return;
    guardOpen = true;
    // The back button already used up our history entry. Register a new one so the editor keeps its own.
    if (fromHistory) release = ui.pushLayer(() => requestClose(true));
    ui.sheet({
      title: "Unsaved changes",
      content: el("p", { class: "fe-sheetp" }, "You have edits that aren't saved to the laptop yet."),
      actions: [
        { label: "Keep editing", variant: "secondary" },
        { label: "Discard", variant: "danger-soft", onClick: () => { store.del(draftKey()); text = base; finish(); } },
        { label: "Save", variant: "primary", onClick: async () => {
          const ok = await save();
          if (ok) finish();
          return ok ? undefined : false;
        } },
      ],
      onClose: () => { guardOpen = false; },
    });
  }
  closeBtn.addEventListener("click", () => requestClose(false));
  release = ui.pushLayer(() => { release = null; requestClose(true); });

  function onUnload(event) {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  }
  window.addEventListener("beforeunload", onUnload);
  function onKey(event) {
    if (event.key === "Escape" && document.activeElement === area && ui.openLayers() <= 1) requestClose(false);
  }
  document.addEventListener("keydown", onKey, true);

  refreshMeta();
  load();
  return { close: () => requestClose(false), isDirty };
}
