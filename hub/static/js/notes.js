// Notes: record or type notes, browse by day and #tag, search, read and edit.
// Voice -> POST /api/notes/voice, typed -> POST /api/notes, tags and titles are added later by the local model.
// Sections: openSection("new") opens the typed-note sheet, openSection("voice") starts recording; openItem(id) opens a note.
import { api, renderMarkdown, el } from "./api.js";
import * as ui from "./ui.js";

// A sheet button that is still working ignores further taps (a double-tap must not send two requests).
const guard = (actions) => actions.map((a) => {
  if (!a || !a.onClick) return a;
  let working = false;
  return { ...a, onClick: async (x) => { if (working) return false; working = true; try { return await a.onClick(x); } finally { working = false; } } };
});


const MAX_RECORD_SECONDS = 600;
const POLL_MS = 3000;
const POLL_WINDOW_MS = 45000;

function pad(n) { return String(n).padStart(2, "0"); }
function clock(seconds) { return `${Math.floor(seconds / 60)}:${pad(seconds % 60)}`; }
function parseDate(text) { const d = new Date(text); return Number.isNaN(d.getTime()) ? null : d; }
function dayKey(d) { return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; }
function dayLabel(d) {
  if (!d) return "Undated";
  const now = new Date();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (dayKey(d) === dayKey(now)) return "Today";
  if (dayKey(d) === dayKey(yesterday)) return "Yesterday";
  const opts = { weekday: "short", day: "numeric", month: "short" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-GB", opts).replace(",", "");
}
function timeLabel(d) { return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : ""; }
function fullDate(d) { return d ? `${dayLabel(d)}, ${timeLabel(d)}` : ""; }

function pickMimeType() {
  if (typeof MediaRecorder === "undefined") return "";
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    try { if (MediaRecorder.isTypeSupported(type)) return type; } catch (_) { /* ignore */ }
  }
  return "";
}
function extensionFor(type) {
  if (type.includes("mp4")) return "m4a";
  if (type.includes("ogg")) return "ogg";
  return "webm";
}

export function init(container, ctx) {
  if (!document.querySelector('link[data-css="notes"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/notes.css", "data-css": "notes" }));
  }
  const state = {
    notes: [], tags: [], tag: "", q: "", loaded: false, error: "",
    pollUntil: 0, pollTimer: null, signature: "", visible: false, loadSeq: 0,
  };
  const rec = { phase: "idle", recorder: null, stream: null, chunks: [], started: 0, tick: null, limit: null, cancelled: false };
  let detail = null;

  // ---------- skeleton ----------
  const listPane = el("div", { class: "nt-pane nt-list-pane" });
  const detailPane = el("div", { class: "nt-pane nt-detail-pane", hidden: true });
  container.replaceChildren(listPane, detailPane);

  const searchInput = el("input", { type: "search", placeholder: "Search notes", "aria-label": "Search notes", enterkeyhint: "search", autocomplete: "off" });
  const newBtn = ui.button({ variant: "tonal", iconOnly: true, icon: "note-plus", label: "New typed note", onClick: () => openTyped() });
  const searchBar = el("div", { class: "nt-searchbar" },
    el("div", { class: "ui-input-wrap nt-search" }, ui.icon("search", { class: "nt-search__icon" }), searchInput), newBtn);
  const tagBar = el("div", { class: "nt-tags ui-hscroll", role: "group", "aria-label": "Filter by tag", hidden: true });
  const recBanner = el("div", { class: "nt-recbar", hidden: true, role: "status", "aria-live": "polite" });
  const voiceNote = el("div", { class: "nt-voice-note", hidden: true });
  const resultBox = el("div", { class: "nt-result ui-card ui-card--flat", hidden: true, "aria-live": "polite" });
  const listBox = el("div", { class: "nt-list" });

  // record FAB: lives in a zero-height sticky dock so it stays on screen while the view scrolls
  const fabTimer = el("span", { class: "nt-fab__time ui-num" }, "0:00");
  const fabIcon = el("span", { class: "nt-fab__icon" }, ui.icon("mic"));
  const fab = el("button", { class: "ui-fab nt-fab", type: "button", "aria-label": "Record a voice note" },
    el("span", { class: "nt-fab__ring", "aria-hidden": "true" }), fabIcon, fabTimer);
  const dock = el("div", { class: "nt-dock" }, fab);

  listPane.append(searchBar, tagBar, recBanner, voiceNote, resultBox, listBox, dock);

  // ---------- list rendering ----------
  function renderTags() {
    tagBar.replaceChildren();
    const tags = state.tags.slice();
    if (state.tag && !tags.some((t) => t.tag === state.tag)) tags.unshift({ tag: state.tag, count: 0 });
    tagBar.hidden = tags.length === 0;
    for (const t of tags) {
      tagBar.append(el("button", {
        class: "ui-chip", type: "button", "aria-pressed": String(t.tag === state.tag),
        onclick: () => { state.tag = state.tag === t.tag ? "" : t.tag; renderTags(); refresh(); },
      }, `#${t.tag}`, el("span", { class: "nt-count ui-num" }, String(t.count))));
    }
  }

  function noteRow(note) {
    const d = parseDate(note.created);
    const voice = note.source === "voice";
    const tagsLine = (note.tags || []).slice(0, 3).map((t) => el("span", { class: "nt-tag" }, `#${t}`));
    const sub = el("span", { class: "nt-row__meta" },
      el("span", { class: "nt-time ui-num" }, timeLabel(d)),
      note.tagged === false ? el("span", { class: "nt-pending" }, ui.icon("sparkles"), "tagging") : null,
      ...tagsLine);
    return el("button", { class: "ui-row ui-row--interactive nt-row", type: "button", onclick: () => openDetail(note) },
      ui.tile(voice ? "mic" : "note", voice ? "pink" : "orange"),
      el("span", { class: "ui-row__body" },
        el("span", { class: "ui-row__title" }, note.title || "Untitled note"),
        note.preview ? el("span", { class: "ui-row__sub nt-preview" }, note.preview) : null,
        sub),
      el("span", { class: "ui-row__chev" }, ui.icon("chevron-right")));
  }

  function renderList() {
    listBox.replaceChildren();
    if (!state.loaded && !state.error) { listBox.append(ui.skeleton("list", 5)); return; }
    if (state.error) {
      listBox.append(ui.notice(state.error, { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => refresh({ showLoading: true }) }));
      return;
    }
    if (!state.notes.length) {
      const filtering = state.q || state.tag;
      listBox.append(ui.emptyState(filtering
        ? { icon: "search", title: "Nothing matches", body: "Try another word or clear the tag filter.", compact: true }
        : { icon: "note", title: "No notes yet", body: "Tap the mic and say what's on your mind, or type a note.",
          action: { label: "Type a note", icon: "note-plus", onClick: () => openTyped() } }));
      return;
    }
    let lastKey = null;
    let list = null;
    for (const note of state.notes) {
      const d = parseDate(note.created);
      const key = d ? dayKey(d) : "none";
      if (key !== lastKey) {
        lastKey = key;
        list = el("div", { class: "ui-list ui-list--inset ui-list--icons nt-group" });
        listBox.append(el("h2", { class: "nt-day ui-caps" }, dayLabel(d)), list);
      }
      list.append(noteRow(note));
    }
  }

  // ---------- fetching ----------
  async function refresh({ showLoading = false, silent = false } = {}) {
    const seq = ++state.loadSeq;
    if (showLoading) { state.loaded = false; state.error = ""; renderList(); }
    const params = new URLSearchParams();
    if (state.tag) params.set("tag", state.tag);
    if (state.q) params.set("q", state.q);
    const qs = params.toString();
    try {
      const data = await api("/api/notes" + (qs ? `?${qs}` : ""));
      if (seq !== state.loadSeq) return;
      const sig = JSON.stringify(data);
      state.error = "";
      const first = !state.loaded;
      state.loaded = true;
      if (sig !== state.signature || first) {
        state.signature = sig;
        state.notes = data.notes || [];
        state.tags = data.tags || [];
        renderTags();
        renderList();
      }
      schedulePoll();
    } catch (error) {
      if (seq !== state.loadSeq || silent) return;
      state.error = error.message;
      state.loaded = true;
      renderList();
      if (state.notes.length) ctx.toast(error.message, { kind: "bad" });
    }
  }
  const anyPending = () => state.notes.some((n) => n.tagged === false);
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!state.visible || detail || Date.now() > state.pollUntil || !anyPending()) return;
    state.pollTimer = setTimeout(() => refresh({ silent: true }), POLL_MS);
  }
  function startPolling() { state.pollUntil = Date.now() + POLL_WINDOW_MS; schedulePoll(); }

  // ---------- search ----------
  let searchTimer = null;
  const runSearch = () => { state.q = searchInput.value.trim(); refresh(); };
  searchInput.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 300); });
  searchInput.addEventListener("search", () => { clearTimeout(searchTimer); runSearch(); });

  // ---------- typed note ----------
  function openTyped() {
    const area = el("textarea", { class: "nt-typed", rows: "7", placeholder: "What's on your mind?", "aria-label": "Note text", enterkeyhint: "enter" });
    const s = ui.sheet({
      title: "New note", content: [area, el("p", { class: "ui-hint nt-hint" }, "A title and #tags are added for you in a few seconds.")],
      actions: guard([{
        label: "Save note", variant: "primary", icon: "check",
        async onClick() {
          const text = area.value.trim();
          if (!text) { ctx.toast("Type something first.", { kind: "warn" }); area.focus(); return false; }
          try {
            await api("/api/notes", { method: "POST", json: { text, source: "text" } });
          } catch (error) { ctx.toast(error.message, { kind: "bad" }); return false; }
          ui.haptic("success");
          ctx.toast("Note saved", { kind: "good" });
          await refresh();
          startPolling();
          return true;
        },
      }]),
    });
    return s;
  }

  // ---------- voice ----------
  const voiceProblem = (() => {
    if (!ctx.secure) return "The microphone only works on the secure (HTTPS) address, the Tailscale one. Open the hub from that address to talk. You can still type notes.";
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
      return "This browser can't record audio here, so voice notes are off. You can still type notes.";
    }
    return "";
  })();

  const recLabel = el("span", { class: "nt-recbar__label" }, "Recording");
  const recTime = el("span", { class: "nt-recbar__time ui-num" }, "0:00");
  const recCancel = ui.button({ label: "Cancel", variant: "ghost", size: "sm", onClick: () => finishRecording(true) });
  recBanner.append(el("span", { class: "ui-dot ui-dot--bad ui-dot--live" }), recLabel, recTime, recCancel);

  function setPhase(phase) {
    rec.phase = phase;
    const recording = phase === "recording";
    fab.classList.toggle("is-recording", recording);
    fab.classList.toggle("is-uploading", phase === "uploading");
    fab.disabled = phase === "uploading";
    fab.setAttribute("aria-label", recording ? "Stop recording and save the note" : "Record a voice note");
    fabIcon.replaceChildren(ui.icon(recording ? "stop" : "mic"));
    recBanner.hidden = !(recording || phase === "uploading");
    recLabel.textContent = recording ? "Recording" : "Writing it down";
    recTime.hidden = !recording;
    recCancel.hidden = !recording;
    if (phase === "idle") { fabTimer.textContent = "0:00"; recTime.textContent = "0:00"; }
  }
  function showVoiceProblem(message) {
    voiceNote.replaceChildren();
    voiceNote.hidden = !message;
    if (message) voiceNote.append(ui.notice(message, { tone: "warn", icon: "mic-off" }));
  }
  let resultTimer = null;
  function showResult({ transcript, title }) {
    clearTimeout(resultTimer);
    resultBox.replaceChildren(
      el("div", { class: "nt-result__head" }, ui.icon("check-circle"), el("strong", {}, title ? `Saved: ${title}` : "Saved")),
      transcript ? el("p", { class: "nt-result__text" }, transcript) : null);
    resultBox.hidden = false;
    resultTimer = setTimeout(() => { resultBox.hidden = true; }, 9000);
  }
  function releaseStream() {
    if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop());
    rec.stream = null;
  }
  function updateTimer() {
    const text = clock(Math.floor((Date.now() - rec.started) / 1000));
    fabTimer.textContent = text;
    recTime.textContent = text;
  }

  async function startRecording() {
    if (voiceProblem) { showVoiceProblem(voiceProblem); return; }
    showVoiceProblem("");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      const name = error && error.name;
      if (name === "NotAllowedError" || name === "SecurityError" || name === "NotSupportedError") {
        showVoiceProblem("Microphone permission was blocked. Tap the lock icon in the address bar, allow Microphone for this site, then try again. You can still type notes.");
      } else if (name === "NotFoundError" || name === "OverconstrainedError") {
        showVoiceProblem("No microphone was found on this device.");
      } else if (name === "NotReadableError") {
        showVoiceProblem("The microphone is busy in another app. Close it and try again.");
      } else {
        showVoiceProblem(`Couldn't start the microphone: ${(error && error.message) || name || "unknown error"}`);
      }
      return;
    }
    const mimeType = pickMimeType();
    let recorder;
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch (error) {
      stream.getTracks().forEach((t) => t.stop());
      showVoiceProblem(`This browser can't record audio: ${error.message}`);
      return;
    }
    rec.stream = stream; rec.recorder = recorder; rec.chunks = []; rec.cancelled = false;
    recorder.addEventListener("dataavailable", (event) => { if (event.data && event.data.size) rec.chunks.push(event.data); });
    recorder.addEventListener("error", () => { showVoiceProblem("Recording stopped unexpectedly."); finishRecording(true); });
    recorder.addEventListener("stop", onRecorderStop);
    recorder.start(1000);
    rec.started = Date.now();
    setPhase("recording");
    ui.haptic("medium");
    updateTimer();
    clearInterval(rec.tick);
    rec.tick = setInterval(updateTimer, 500);
    clearTimeout(rec.limit);
    rec.limit = setTimeout(() => { ctx.toast("Reached the 10 minute limit, saving your note.", { kind: "warn" }); finishRecording(false); }, MAX_RECORD_SECONDS * 1000);
  }

  function finishRecording(cancel) {
    clearInterval(rec.tick); clearTimeout(rec.limit);
    rec.tick = null; rec.limit = null;
    rec.cancelled = cancel;
    const recorder = rec.recorder;
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch (_) { onRecorderStop(); }
    } else onRecorderStop();
  }

  async function onRecorderStop() {
    const recorder = rec.recorder;
    rec.recorder = null;
    releaseStream();
    clearInterval(rec.tick); clearTimeout(rec.limit);
    if (rec.cancelled || !recorder) { setPhase("idle"); return; }
    const type = recorder.mimeType || pickMimeType() || "audio/webm";
    const blob = new Blob(rec.chunks, { type });
    rec.chunks = [];
    if (blob.size < 200) {
      setPhase("idle");
      showVoiceProblem("That recording was empty. Hold on a little longer and try again.");
      return;
    }
    setPhase("uploading");
    const form = new FormData();
    form.append("file", blob, `note.${extensionFor(type)}`);
    try {
      const data = await api("/api/notes/voice", { method: "POST", form });
      ui.haptic("success");
      showResult({ transcript: data.transcript, title: data.note && data.note.title });
      setPhase("idle");
      await refresh();
      startPolling();
    } catch (error) {
      setPhase("idle");
      showVoiceProblem(error.message);
      ctx.toast(error.message, { kind: "bad" });
    }
  }

  fab.addEventListener("click", () => {
    if (rec.phase === "idle") startRecording();
    else if (rec.phase === "recording") finishRecording(false);
  });
  if (voiceProblem) showVoiceProblem(voiceProblem);

  // ---------- detail ----------
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise((resolve, reject) => {
      const area = el("textarea", { class: "nt-offscreen", "aria-hidden": "true", tabindex: "-1" });
      area.value = text;
      container.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ }
      area.remove();
      ok ? resolve() : reject(new Error("Couldn't copy. Select the text and copy it by hand."));
    });
  }

  function closeDetail({ fromPop = false, refreshList = true } = {}) {
    if (!detail) return;
    detail = null;
    detailPane.hidden = true;
    detailPane.replaceChildren();
    listPane.hidden = false;
    if (!fromPop && history.state && history.state.notesDetail) { try { history.back(); } catch (_) { /* ignore */ } }
    if (refreshList) refresh({ silent: true });
  }

  function openDetail(summary) {
    const myId = summary.id;
    const titleInput = el("input", { id: "nt-title", class: "nt-title-input", type: "text", value: summary.title || "", autocomplete: "off", maxlength: "200", placeholder: "Title", "aria-label": "Title" });
    const tagsInput = el("input", { id: "nt-tags", type: "text", value: (summary.tags || []).join(", "), autocomplete: "off", autocapitalize: "none", spellcheck: "false", placeholder: "idea, work, shopping" });
    const body = el("div", { class: "markdown nt-body" }, ui.skeleton("lines", 4));
    const d = parseDate(summary.created);
    const voice = summary.source === "voice";
    const saveBtn = ui.button({ label: "Save", icon: "check", variant: "primary", disabled: true });
    const copyBtn = ui.button({ label: "Copy text", icon: "copy", variant: "tonal", disabled: true });
    const editBtn = ui.button({ label: "Edit text", icon: "edit", variant: "tonal", disabled: true });
    const deleteBtn = ui.button({ label: "Delete", icon: "trash", variant: "danger", disabled: true });
    const textArea = el("textarea", { class: "nt-textarea", rows: "8", maxlength: "20000", "aria-label": "Note text" });
    const saveTextBtn = ui.button({ label: "Save text", icon: "check", variant: "primary" });
    const cancelTextBtn = ui.button({ label: "Cancel", variant: "ghost" });
    const editRow = el("div", { class: "nt-edit", hidden: true }, textArea, el("div", { class: "nt-actions" }, saveTextBtn, cancelTextBtn));
    const actionRow = el("div", { class: "nt-actions nt-actions-wrap" }, saveBtn, copyBtn, editBtn, deleteBtn);
    let bodyText = "";

    const metaLine = el("div", { class: "nt-meta" }, ui.tile(voice ? "mic" : "note", voice ? "pink" : "orange", "sm"),
      el("span", {}, fullDate(d)), ui.badge(summary.source || "text", voice ? "accent" : undefined));
    const back = ui.button({ label: "Notes", icon: "chevron-left", variant: "ghost", size: "sm", onClick: () => closeDetail() });

    detailPane.replaceChildren(
      el("div", { class: "nt-detail-bar" }, back),
      el("article", { class: "ui-card nt-card" },
        metaLine, titleInput,
        summary.tagged === false ? ui.notice("The laptop is still adding a title and tags. If you edit them yourself, yours stay.", { tone: "warn", icon: "sparkles" }) : null,
        body, editRow,
        el("div", { class: "ui-field nt-tagfield" }, el("label", { class: "ui-label", for: "nt-tags" }, "Tags, separated by commas"), tagsInput)),
      actionRow);
    listPane.hidden = true;
    detailPane.hidden = false;
    container.scrollTop = 0;
    detail = { id: myId };
    try { history.pushState({ notesDetail: myId }, ""); } catch (_) { /* ignore */ }
    clearTimeout(state.pollTimer);

    api(`/api/notes/${encodeURIComponent(myId)}`).then((note) => {
      if (!detail || detail.id !== myId) return;
      bodyText = note.body || "";
      body.innerHTML = renderMarkdown(bodyText);
      titleInput.value = note.title || "";
      tagsInput.value = (note.tags || []).join(", ");
      saveBtn.disabled = false;
      copyBtn.disabled = false;
      editBtn.disabled = false;
      deleteBtn.disabled = false;
    }).catch((error) => {
      if (!detail || detail.id !== myId) return;
      body.replaceChildren(ui.notice(error.message, { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => { closeDetail({ refreshList: false }); openDetail(summary); } }));
    });

    copyBtn.addEventListener("click", async () => {
      try { await copyText(bodyText); ctx.toast("Copied", { kind: "good" }); } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    });
    function setEditing(on) {
      body.hidden = on; editRow.hidden = !on; actionRow.hidden = on;
      if (on) { textArea.value = bodyText; textArea.focus(); }
    }
    editBtn.addEventListener("click", () => setEditing(true));
    cancelTextBtn.addEventListener("click", () => setEditing(false));
    saveTextBtn.addEventListener("click", async () => {
      const text = textArea.value.trim();
      if (!text) { ctx.toast("The note can't be empty. Use Delete to remove it.", { kind: "warn" }); return; }
      try {
        const note = await ui.busy(saveTextBtn, api(`/api/notes/${encodeURIComponent(myId)}`, { method: "PATCH", json: { body: text } }));
        if (!detail || detail.id !== myId) return;
        bodyText = note.body || text;
        body.innerHTML = renderMarkdown(bodyText);
        setEditing(false);
        ui.haptic("success");
        ctx.toast("Text saved", { kind: "good" });
      } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    });
    deleteBtn.addEventListener("click", async () => {
      if (!(await ui.confirm({ title: "Delete this note?", message: "It moves to the trash in the Files tab (Notes folder), where you can restore it.", confirmLabel: "Delete", danger: true, icon: "trash" }))) return;
      try {
        await ui.busy(deleteBtn, api(`/api/notes/${encodeURIComponent(myId)}`, { method: "DELETE" }));
        ui.haptic("success");
        ctx.toast("Moved to the trash", { kind: "good" });
        closeDetail();
      } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    });
    saveBtn.addEventListener("click", async () => {
      try {
        await ui.busy(saveBtn, api(`/api/notes/${encodeURIComponent(myId)}`, { method: "PATCH", json: { title: titleInput.value, tags: tagsInput.value } }));
        ui.haptic("success");
        ctx.toast("Saved", { kind: "good" });
        closeDetail();
      } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    });
  }

  window.addEventListener("popstate", () => { if (detail) closeDetail({ fromPop: true }); });

  // ---------- lifecycle ----------
  setPhase("idle");
  renderList();
  return {
    show() {
      state.visible = true;
      if (rec.phase === "recording") { updateTimer(); clearInterval(rec.tick); rec.tick = setInterval(updateTimer, 500); }
      refresh({ showLoading: !state.loaded });
      startPolling();
    },
    hide() {
      state.visible = false;
      clearTimeout(state.pollTimer); state.pollTimer = null;
      clearTimeout(searchTimer);
      clearInterval(rec.tick); rec.tick = null; // the recording keeps going; the 10 minute limit still applies
    },
    refresh() { return refresh({ showLoading: false }); },
    openSection(name) {
      if (detail) closeDetail({ refreshList: false });
      if (name === "new") openTyped();
      else if (name === "voice" && rec.phase === "idle") startRecording();
    },
    openItem(id) {
      if (id == null || detail?.id === id) return;
      if (detail) closeDetail({ refreshList: false });
      const known = state.notes.find((n) => n.id === id);
      openDetail(known || { id, title: "", tags: [], source: "text" });
    },
  };
}
