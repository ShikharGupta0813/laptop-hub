// Notes tab: record or type a note, browse, search and edit notes.
import { api, renderMarkdown, el } from "./api.js";

const MAX_RECORD_SECONDS = 600;
const POLL_MS = 3000;
const POLL_WINDOW_MS = 45000;
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(path) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", path);
  svg.append(p);
  return svg;
}
const MIC_PATH = "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V22h2v-3.1A7 7 0 0 0 19 12h-2Z";
const STOP_PATH = "M7 7h10v10H7z";
const BACK_PATH = "M15.4 5.4 14 4l-8 8 8 8 1.4-1.4L8.8 12z";

function pad(n) { return String(n).padStart(2, "0"); }
function clock(seconds) { return `${Math.floor(seconds / 60)}:${pad(seconds % 60)}`; }

function parseDate(text) {
  const d = new Date(text);
  return Number.isNaN(d.getTime()) ? null : d;
}
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
function fullDate(d) {
  return d ? `${dayLabel(d)}, ${timeLabel(d)}` : "";
}

function pickMimeType() {
  if (typeof MediaRecorder === "undefined") return "";
  const wanted = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  for (const type of wanted) {
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
  // ---------- state ----------
  const state = {
    notes: [], tags: [], tag: "", q: "", loaded: false, error: "",
    pollUntil: 0, pollTimer: null, signature: "",
    visible: false, loadSeq: 0,
  };
  const rec = {
    phase: "idle", // idle | recording | uploading
    recorder: null, stream: null, chunks: [], started: 0, tick: null, limit: null, cancelled: false,
    clearResult: null,
  };
  let detail = null; // { id, el }

  // ---------- skeleton ----------
  const listPane = el("div", { class: "notes-list-pane" });
  const detailPane = el("div", { class: "notes-detail-pane", hidden: true });
  container.replaceChildren(listPane, detailPane);

  // capture card
  const micButton = el("button", { class: "notes-mic", type: "button", "aria-label": "Start recording a voice note" });
  const micIcon = icon(MIC_PATH);
  micButton.append(el("span", { class: "notes-ring", "aria-hidden": "true" }), micIcon);
  const timerText = el("div", { class: "notes-timer", "aria-hidden": "true" }, "0:00");
  const statusText = el("p", { class: "notes-status", role: "status", "aria-live": "polite" }, "Tap the mic and say what's on your mind.");
  const cancelButton = el("button", { class: "btn", type: "button", hidden: true }, "Cancel");
  const voiceNote = el("p", { class: "notes-voice-note error", hidden: true });
  const resultBox = el("div", { class: "notes-result", hidden: true, "aria-live": "polite" });
  const typed = el("textarea", { id: "notes-typed", rows: "3", placeholder: "Type a note", "aria-label": "Type a note", enterkeyhint: "enter" });
  const saveButton = el("button", { class: "btn primary", type: "button" }, "Save");
  const captureCard = el("section", { class: "card notes-capture", "aria-label": "Capture a note" },
    el("div", { class: "notes-mic-wrap" }, micButton, timerText, statusText, cancelButton),
    voiceNote, resultBox,
    el("div", { class: "notes-type" }, typed, el("div", { class: "row notes-type-actions" }, saveButton)));

  // search + tags
  const searchInput = el("input", { type: "search", placeholder: "Search notes", "aria-label": "Search notes", enterkeyhint: "search", autocomplete: "off" });
  const tagBar = el("div", { class: "notes-tags", role: "group", "aria-label": "Filter by tag", hidden: true });
  const listBox = el("div", { class: "notes-list", "aria-live": "off" });

  listPane.append(captureCard,
    el("div", { class: "notes-search" }, searchInput),
    tagBar, listBox);

  // ---------- list rendering ----------
  function sourceBadge(source) {
    const label = source === "voice" ? "voice" : source === "text" ? "text" : source || "text";
    return el("span", { class: `notes-badge notes-badge-${label.replace(/[^a-z]/g, "")}` }, label);
  }

  function renderTags() {
    tagBar.replaceChildren();
    const active = state.tag;
    const tags = state.tags.slice();
    if (active && !tags.some((t) => t.tag === active)) tags.unshift({ tag: active, count: 0 });
    tagBar.hidden = tags.length === 0;
    for (const t of tags) {
      const chip = el("button", {
        class: "chip", type: "button", "aria-pressed": String(t.tag === active),
        onclick: () => {
          state.tag = state.tag === t.tag ? "" : t.tag;
          renderTags();
          refresh({ showLoading: false });
        },
      }, `${t.tag} `, el("span", { class: "notes-count" }, String(t.count)));
      tagBar.append(chip);
    }
  }

  function noteRow(note) {
    const d = parseDate(note.created);
    const meta = el("div", { class: "notes-row-meta" },
      sourceBadge(note.source),
      el("span", { class: "muted" }, timeLabel(d)),
      note.tagged === false ? el("span", { class: "notes-pending muted" }, "tagging...") : null,
      ...(note.tags || []).map((t) => el("span", { class: "notes-tag" }, t)));
    return el("button", {
      class: "notes-row", type: "button", onclick: () => openDetail(note),
    }, el("span", { class: "notes-row-title" }, note.title || "Untitled note"), meta,
    note.preview ? el("span", { class: "notes-preview muted" }, note.preview) : null);
  }

  function renderList() {
    listBox.replaceChildren();
    if (!state.loaded && !state.error) {
      listBox.append(el("p", { class: "muted notes-state" }, "Loading notes..."));
      return;
    }
    if (state.error) {
      listBox.append(el("div", { class: "notes-state" },
        el("p", { class: "error", role: "alert" }, state.error),
        el("button", { class: "btn", type: "button", onclick: () => refresh({ showLoading: true }) }, "Retry")));
      return;
    }
    if (!state.notes.length) {
      const filtering = state.q || state.tag;
      listBox.append(el("p", { class: "muted notes-state" },
        filtering ? "No notes match that." : "No notes yet. Tap the mic and say what's on your mind."));
      return;
    }
    let lastKey = null;
    let group = null;
    for (const note of state.notes) {
      const d = parseDate(note.created);
      const key = d ? dayKey(d) : "none";
      if (key !== lastKey) {
        lastKey = key;
        group = el("div", { class: "notes-group" }, el("h2", { class: "notes-day" }, dayLabel(d)));
        listBox.append(group);
      }
      group.append(noteRow(note));
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
      if (seq !== state.loadSeq) return;
      if (silent) return; // background refresh: try again next tick
      state.error = error.message;
      state.loaded = true;
      renderList();
      if (state.notes.length) ctx.toast(error.message);
    }
  }

  function anyPending() { return state.notes.some((n) => n.tagged === false); }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!state.visible || detail) return;
    if (Date.now() > state.pollUntil) return;
    if (!anyPending()) return;
    state.pollTimer = setTimeout(() => refresh({ silent: true }), POLL_MS);
  }

  function startPolling() {
    state.pollUntil = Date.now() + POLL_WINDOW_MS;
    schedulePoll();
  }

  // ---------- search ----------
  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = searchInput.value.trim();
      refresh({ showLoading: false });
    }, 300);
  });
  searchInput.addEventListener("search", () => {
    clearTimeout(searchTimer);
    state.q = searchInput.value.trim();
    refresh({ showLoading: false });
  });

  // ---------- typed note ----------
  let saving = false;
  saveButton.addEventListener("click", async () => {
    const text = typed.value.trim();
    if (!text) { ctx.toast("Type something first."); typed.focus(); return; }
    if (saving) return;
    saving = true;
    saveButton.disabled = true;
    saveButton.textContent = "Saving...";
    try {
      await api("/api/notes", { method: "POST", json: { text, source: "text" } });
      typed.value = "";
      showResult({ saved: "Note saved." });
      await refresh();
      startPolling();
    } catch (error) {
      ctx.toast(error.message);
    } finally {
      saving = false;
      saveButton.disabled = false;
      saveButton.textContent = "Save";
    }
  });

  // ---------- voice ----------
  const voiceProblem = (() => {
    if (!ctx.secure) {
      return "The microphone only works on the secure (HTTPS) address, the Tailscale one. Open the hub from that address to talk. You can still type notes below.";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
      return "This browser can't record audio here, so voice notes are off. You can still type notes below.";
    }
    return "";
  })();

  function setPhase(phase) {
    rec.phase = phase;
    captureCard.dataset.phase = phase;
    const recording = phase === "recording";
    micButton.classList.toggle("recording", recording);
    micButton.disabled = phase === "uploading" || !!voiceProblem;
    micButton.setAttribute("aria-label", recording ? "Stop recording and save the note" : "Start recording a voice note");
    micIcon.firstChild.setAttribute("d", recording ? STOP_PATH : MIC_PATH);
    cancelButton.hidden = !recording;
    timerText.classList.toggle("on", recording);
    if (phase === "idle") {
      timerText.textContent = "0:00";
      statusText.textContent = "Tap the mic and say what's on your mind.";
    } else if (recording) {
      statusText.textContent = "Recording. Tap again to stop.";
    } else {
      statusText.textContent = "Writing it down...";
    }
  }

  function showVoiceProblem(message) {
    voiceNote.textContent = message;
    voiceNote.hidden = !message;
  }

  let resultTimer = null;
  function showResult({ transcript, title, saved }) {
    clearTimeout(resultTimer);
    resultBox.replaceChildren();
    if (transcript) resultBox.append(el("p", { class: "notes-result-line" }, el("strong", {}, "Heard: "), transcript));
    if (title) resultBox.append(el("p", { class: "notes-result-line" }, el("strong", {}, "Saved: "), title));
    if (saved) resultBox.append(el("p", { class: "notes-result-line" }, saved));
    resultBox.hidden = false;
    resultTimer = setTimeout(() => { resultBox.hidden = true; }, 9000);
  }

  function releaseStream() {
    if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop());
    rec.stream = null;
  }

  function updateTimer() {
    const seconds = Math.floor((Date.now() - rec.started) / 1000);
    timerText.textContent = clock(seconds);
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
        showVoiceProblem("Microphone permission was blocked. Tap the lock icon in the address bar, allow Microphone for this site, then try again. You can still type notes below.");
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
    rec.stream = stream;
    rec.recorder = recorder;
    rec.chunks = [];
    rec.cancelled = false;
    recorder.addEventListener("dataavailable", (event) => { if (event.data && event.data.size) rec.chunks.push(event.data); });
    recorder.addEventListener("error", () => { showVoiceProblem("Recording stopped unexpectedly."); finishRecording(true); });
    recorder.addEventListener("stop", onRecorderStop);
    recorder.start(1000);
    rec.started = Date.now();
    setPhase("recording");
    updateTimer();
    clearInterval(rec.tick);
    rec.tick = setInterval(updateTimer, 500);
    clearTimeout(rec.limit);
    rec.limit = setTimeout(() => {
      ctx.toast("Reached the 10 minute limit, saving your note.");
      finishRecording(false);
    }, MAX_RECORD_SECONDS * 1000);
  }

  function finishRecording(cancel) {
    clearInterval(rec.tick);
    clearTimeout(rec.limit);
    rec.tick = null; rec.limit = null;
    rec.cancelled = cancel;
    const recorder = rec.recorder;
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch (_) { onRecorderStop(); }
    } else {
      onRecorderStop();
    }
  }

  async function onRecorderStop() {
    const recorder = rec.recorder;
    rec.recorder = null;
    releaseStream();
    clearInterval(rec.tick);
    clearTimeout(rec.limit);
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
      showResult({ transcript: data.transcript, title: data.note && data.note.title });
      setPhase("idle");
      await refresh();
      startPolling();
    } catch (error) {
      setPhase("idle");
      showVoiceProblem(error.message);
      ctx.toast(error.message);
    }
  }

  micButton.addEventListener("click", () => {
    if (rec.phase === "idle") startRecording();
    else if (rec.phase === "recording") finishRecording(false);
  });
  cancelButton.addEventListener("click", () => finishRecording(true));

  if (voiceProblem) {
    setPhase("idle");
    showVoiceProblem(voiceProblem);
    statusText.textContent = "Voice notes are unavailable here.";
  }

  // ---------- detail ----------
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const area = el("textarea", { class: "notes-offscreen", "aria-hidden": "true", tabindex: "-1" });
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
    if (!fromPop && history.state && history.state.notesDetail) {
      try { history.back(); } catch (_) { /* ignore */ }
    }
    if (refreshList) refresh({ silent: true });
  }

  function openDetail(summary) {
    const titleInput = el("input", { id: "notes-title", type: "text", value: summary.title || "", autocomplete: "off", maxlength: "200" });
    const tagsInput = el("input", { id: "notes-tags", type: "text", value: (summary.tags || []).join(", "), autocomplete: "off", autocapitalize: "none", spellcheck: "false", placeholder: "idea, work, shopping" });
    const body = el("div", { class: "markdown notes-body" }, el("p", { class: "muted" }, "Loading..."));
    const errorLine = el("p", { class: "error", role: "alert", hidden: true });
    const d = parseDate(summary.created);
    const metaLine = el("p", { class: "muted notes-detail-meta" }, `${fullDate(d)} `, sourceBadge(summary.source));
    const saveBtn = el("button", { class: "btn primary", type: "button", disabled: true }, "Save");
    const copyBtn = el("button", { class: "btn", type: "button", disabled: true }, "Copy text");
    let bodyText = "";

    const back = el("button", { class: "btn notes-back", type: "button", "aria-label": "Back to notes", onclick: () => closeDetail() },
      icon(BACK_PATH), "Back");

    detailPane.replaceChildren(
      el("div", { class: "row notes-detail-bar" }, back),
      el("div", { class: "card notes-detail" },
        metaLine,
        el("label", { for: "notes-title" }, "Title"), titleInput,
        el("label", { for: "notes-tags" }, "Tags (separate with commas)"), tagsInput,
        summary.tagged === false ? el("p", { class: "muted notes-pending-hint" }, "The laptop is still adding a title and tags. If you edit them yourself, yours stay.") : null,
        body, errorLine,
        el("div", { class: "row wrap notes-detail-actions" }, saveBtn, copyBtn)));
    listPane.hidden = true;
    detailPane.hidden = false;
    container.scrollTop = 0;
    detail = { id: summary.id };
    try { history.pushState({ notesDetail: summary.id }, ""); } catch (_) { /* ignore */ }
    clearTimeout(state.pollTimer);

    const myId = summary.id;
    api(`/api/notes/${encodeURIComponent(myId)}`).then((note) => {
      if (!detail || detail.id !== myId) return;
      bodyText = note.body || "";
      body.innerHTML = renderMarkdown(bodyText);
      titleInput.value = note.title || "";
      tagsInput.value = (note.tags || []).join(", ");
      saveBtn.disabled = false;
      copyBtn.disabled = false;
    }).catch((error) => {
      if (!detail || detail.id !== myId) return;
      body.replaceChildren(el("p", { class: "error" }, error.message),
        el("button", { class: "btn", type: "button", onclick: () => { closeDetail({ refreshList: false }); openDetail(summary); } }, "Retry"));
      ctx.toast(error.message);
    });

    copyBtn.addEventListener("click", async () => {
      try { await copyText(bodyText); ctx.toast("Copied."); } catch (error) { ctx.toast(error.message); }
    });
    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving...";
      errorLine.hidden = true;
      try {
        await api(`/api/notes/${encodeURIComponent(myId)}`, {
          method: "PATCH", json: { title: titleInput.value, tags: tagsInput.value },
        });
        ctx.toast("Saved.");
        closeDetail();
      } catch (error) {
        errorLine.textContent = error.message;
        errorLine.hidden = false;
        ctx.toast(error.message);
        saveBtn.disabled = false;
        saveBtn.textContent = "Save";
      }
    });
  }

  window.addEventListener("popstate", () => { if (detail) closeDetail({ fromPop: true }); });

  // ---------- lifecycle ----------
  renderList();
  return {
    show() {
      state.visible = true;
      if (rec.phase === "recording") {
        updateTimer();
        clearInterval(rec.tick);
        rec.tick = setInterval(updateTimer, 500);
      }
      refresh({ showLoading: !state.loaded });
      startPolling();
    },
    hide() {
      state.visible = false;
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
      clearTimeout(searchTimer);
      clearInterval(rec.tick); // the recording itself keeps going; the 10 minute limit still applies
      rec.tick = null;
    },
  };
}
