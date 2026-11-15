// Jarvis screen: start and stop the owner's Jarvis voice assistant on the laptop, send it a typed or
// dictated command, read its log. Status is polled every 5 s, only while this panel is open and visible.
import { api, ApiError, el } from "./api.js";
import { icon, spinner } from "./laptop.js";
import { confirm as uiConfirm, haptic, tile, switchControl } from "./ui.js";
import { createFixKit } from "./fixpanel.js";
import { speak as vkSpeak, voiceOn } from "./voicekit.js";

const POLL_MS = 5000;
const MAX_RECORD_SECONDS = 60;
const AUTO_KEY = "hub.jarvis.auto";
const EXAMPLES = [
  "open Chrome",
  "what's on my Asana today",
  "write a mail to Rahul about the demo",
  "join my next meeting",
  "what's on my calendar tomorrow",
  "lock the laptop",
];

function pickMimeType() {
  if (typeof MediaRecorder === "undefined") return "";
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    try { if (MediaRecorder.isTypeSupported(type)) return type; } catch (_) { /* ignore */ }
  }
  return "";
}
const extensionFor = (type) => (type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm");
const pad = (n) => String(n).padStart(2, "0");
const clock = (seconds) => `${Math.floor(seconds / 60)}:${pad(seconds % 60)}`;

function sinceText(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return "";
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return `since ${time}`;
  return `since ${d.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
}

export function createJarvis(container, ctx) {
  container.classList.add("ap-jarvis");
  let opened = false;
  let status = null;
  let loadError = "";
  let fetching = false;
  let busy = null; // "start" | "stop" | null
  let timer = null;
  let abort = null;
  let logExpanded = false;
  let fullLog = null;
  let actionMessage = null; // {text, bad}
  let stateKey = "";
  let controlKey = "";
  let sending = false;
  const HIST_KEY = "hub.jarvis.history";
  const sent = [];
  try { const h = JSON.parse(localStorage.getItem(HIST_KEY) || "[]"); if (Array.isArray(h)) sent.push(...h.filter((x) => x && typeof x.text === "string").slice(0, 8)); } catch (_) { /* ignore */ }
  const rec = { phase: "idle", stream: null, recorder: null, chunks: [], started: 0, tick: null, limit: null, cancelled: false };

  // ---- skeleton -------------------------------------------------------------------------------
  const heading = el("h2", { class: "ap-title", tabindex: "-1" }, "Jarvis");
  const stateCard = el("div", { class: "ui-card ui-card--elevated ap-state", role: "status" });
  const startBtn = el("button", { type: "button", class: "ui-btn ui-btn--primary ui-btn--lg ap-big", onclick: () => startJarvis() });
  const stopBtn = el("button", { type: "button", class: "ui-btn ui-btn--danger-soft ui-btn--lg ap-big ap-stop", onclick: () => askStop() });
  const controls = el("div", { class: "ap-bigrow" }, startBtn, stopBtn);
  const actionNote = el("p", { class: "ap-form-error", role: "alert", hidden: true });

  const composerNote = el("div", { class: "ap-card-info", hidden: true });
  const input = el("input", { type: "text", class: "ap-say-input ui-input--lg", id: "ap-jv-say", autocomplete: "off",
    autocapitalize: "sentences", enterkeyhint: "send", placeholder: "e.g. open the Q3 report" });
  const sendBtn = el("button", { type: "submit", class: "ui-btn ui-btn--primary ap-send" }, icon("send", 18), "Send");
  const micBtn = el("button", { type: "button", class: "ui-btn ui-btn--tonal ui-btn--icon ap-mic", "aria-label": "Record a command", onclick: () => toggleMic() }, icon("assistant", 22));
  const micCancel = el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--sm", hidden: true, onclick: () => finishRecording(true) }, "Cancel");
  const micStatus = el("p", { class: "muted ap-hint", "aria-live": "polite" });
  const autoSwitch = switchControl({ label: "Send automatically after dictating" });
  const autoBox = autoSwitch.input;
  autoBox.id = "ap-jv-auto";
  try { autoBox.checked = localStorage.getItem(AUTO_KEY) === "1"; } catch (_) { /* ignore */ }
  autoBox.addEventListener("change", () => {
    try { localStorage.setItem(AUTO_KEY, autoBox.checked ? "1" : "0"); } catch (_) { /* ignore */ }
  });
  const replyPre = el("pre", { class: "ap-log fk-log", role: "log", "aria-label": "Jarvis's reply", hidden: true });
  const sayMessage = el("p", { class: "ap-say-msg", role: "status", hidden: true });
  const sentList = el("div", { class: "ap-sent ui-hscroll", role: "group", "aria-label": "Recent commands, tap to reuse" });
  const sayForm = el("form", { class: "ap-say-form", autocomplete: "off" },
    el("label", { class: "sr-only", for: "ap-jv-say" }, "Command for Jarvis"),
    el("div", { class: "ap-say-row" }, input, sendBtn),
    replyPre,
    el("div", { class: "ap-say-tools" }, micBtn, micCancel,
      el("label", { class: "ap-auto", for: "ap-jv-auto" }, autoSwitch, el("span", {}, "Send automatically after dictating"))),
    micStatus, sayMessage);
  sayForm.addEventListener("submit", (event) => { event.preventDefault(); say(); });
  const composerCard = el("section", { class: "ui-card ap-composer", "aria-labelledby": "ap-jv-tell-h" },
    el("h3", { id: "ap-jv-tell-h", class: "ui-card__title" }, "Tell Jarvis"), composerNote, sayForm, sentList);

  const exampleButtons = EXAMPLES.map((text) => el("button", { type: "button", class: "ui-chip ap-example",
    onclick: () => { input.value = text; input.focus(); } }, text));
  const helpCard = el("section", { class: "ui-card", "aria-labelledby": "ap-jv-help-h" },
    el("h3", { id: "ap-jv-help-h", class: "ui-card__title" }, "Commands you can say"),
    el("p", { class: "muted" }, "Say \"Hey Jarvis\" near the laptop, then one of these. Or type it above."),
    el("div", { class: "ap-examples" }, exampleButtons));

  const whyPre = el("pre", { class: "ap-log ap-why-pre", tabindex: "0" });
  const whyCard = el("section", { class: "ui-card", "aria-labelledby": "ap-jv-why-h", hidden: true },
    el("h3", { id: "ap-jv-why-h" }, "Why it may have stopped"), whyPre);

  const kit = createFixKit(ctx, { assistant: "jarvis", name: "Jarvis" });
  const logPre = el("pre", { class: "ap-log", tabindex: "0", role: "log", "aria-label": "Jarvis log" });
  const moreBtn = el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", "aria-pressed": "false", onclick: () => toggleLog() }, "Show more");
  const logRefresh = el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", onclick: () => refresh(true) }, icon("refresh", 18), "Refresh");
  const logCard = el("section", { class: "ui-card", "aria-labelledby": "ap-jv-log-h" },
    el("h3", { id: "ap-jv-log-h", class: "ui-card__title" }, "Log tail"), logPre, el("div", { class: "ap-row wrap" }, logRefresh, moreBtn));

  container.append(
    el("div", { class: "ap-runner-head" }, heading, el("p", { class: "muted ap-desc" }, "Your voice assistant on the laptop. Start or stop it, and send it commands from here.")),
    stateCard, controls, actionNote, composerCard, helpCard, whyCard, logCard, kit.node);

  // ---- status ----------------------------------------------------------------------------------
  const canSay = () => !!(status && status.configured && status.running && status.remote_ready);

  function renderState() {
    const key = JSON.stringify([!!status, status && status.configured, status && status.running, status && status.pid,
      status && status.started_at, status && status.reason, loadError]);
    if (key === stateKey) return;
    stateKey = key;
    const nodes = [];
    if (!status && !loadError) {
      nodes.push(el("div", { class: "ap-running" }, spinner("Checking"), el("span", {}, "Checking Jarvis…")));
    } else if (!status) {
      nodes.push(el("div", { class: "ap-card-error" }, icon("warn", 20),
        el("div", {}, el("strong", {}, "Can't reach the laptop"), el("p", {}, loadError),
          el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", onclick: () => refresh(true) }, icon("refresh", 18), "Retry"))));
    } else if (!status.configured) {
      nodes.push(el("div", { class: "ap-statehead" }, el("span", { class: "ui-dot ui-dot--bad", "aria-hidden": "true" }),
        el("strong", {}, "Jarvis isn't set up")),
        el("p", {}, status.reason || "Tell the hub where the Jarvis folder is in its settings (jarvis_dir)."));
    } else if (status.running) {
      const bits = [status.pid != null ? `pid ${status.pid}` : "", sinceText(status.started_at)].filter(Boolean);
      nodes.push(el("div", { class: "ap-statehead" }, el("span", { class: "ui-dot ui-dot--good ui-dot--live", "aria-hidden": "true" }),
        el("strong", {}, "Jarvis is running")), bits.length ? el("p", { class: "muted" }, bits.join(" · ")) : null);
    } else {
      nodes.push(el("div", { class: "ap-statehead" }, el("span", { class: "ui-dot", "aria-hidden": "true" }),
        el("strong", {}, "Jarvis is stopped")), el("p", { class: "muted" }, "Tap Start Jarvis to begin listening on the laptop."));
    }
    if (status && loadError) {
      nodes.push(el("p", { class: "ap-form-error" }, `Can't reach the laptop right now. Showing the last known state. ${loadError}`));
    }
    stateCard.replaceChildren(...nodes.filter(Boolean));
  }

  function renderControls() {
    const key = JSON.stringify([status && status.configured, status && status.running, busy]);
    if (key === controlKey) return;
    controlKey = key;
    const configured = !!(status && status.configured);
    controls.hidden = !configured;
    const running = !!(status && status.running);
    startBtn.replaceChildren(busy === "start" ? spinner("Starting") : icon("play", 22), busy === "start" ? "Starting…" : "Start Jarvis");
    stopBtn.replaceChildren(busy === "stop" ? spinner("Stopping") : icon("stop", 22), busy === "stop" ? "Stopping…" : "Stop Jarvis");
    startBtn.disabled = !!busy || running;
    stopBtn.disabled = !!busy || !running;
    startBtn.setAttribute("aria-busy", String(busy === "start"));
    stopBtn.setAttribute("aria-busy", String(busy === "stop"));
  }

  function noteBlock(title, text, ...buttons) {
    composerNote.hidden = false;
    composerNote.replaceChildren(icon("info", 20), el("div", {}, el("strong", {}, title), el("p", {}, text),
      buttons.length ? el("div", { class: "ap-row wrap" }, ...buttons) : null));
  }
  const startHere = () => el("button", { type: "button", class: "ui-btn ui-btn--primary ui-btn--sm", disabled: !!busy, onclick: () => startJarvis() }, "Start Jarvis");
  const talkHere = () => el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", onclick: () => handOver() },
    "Ask Jarvis here instead");

  // The typed command goes to the Talk screen (the hub's own Jarvis) when the separate program can't take it.
  function handOver() {
    const text = input.value.trim();
    try { if (text) sessionStorage.setItem("hub.assistant.prefill", text); } catch (_) { /* ignore */ }
    ctx.open("assistant", "chat");
  }

  function renderComposer() {
    const configured = !!(status && status.configured);
    const running = !!(status && status.running);
    const ready = canSay();
    composerCard.hidden = false;
    input.disabled = false;                       // the box is never dead: Send explains what is missing and offers the fix
    sendBtn.disabled = sending;
    for (const b of exampleButtons) b.disabled = false;
    micBtn.disabled = rec.phase === "uploading";
    if (!status) {
      composerNote.hidden = true;
    } else if (!configured) {
      noteBlock("Jarvis isn't set up on the laptop", (status.reason || "Set jarvis_dir in hub.json.") + " You can still ask the hub's own Jarvis.", talkHere());
    } else if (!running) {
      noteBlock("Jarvis is not running", "Start it, then send your command. Your text stays in the box.", startHere(), talkHere());
    } else if (!status.remote_ready) {
      noteBlock("Typing to the running Jarvis needs a small patch",
        "Jarvis is running and listens to the laptop's microphone. To send commands from this phone, install the optional patch described in docs/jarvis-remote-inbox.md (adds remote_inbox.py next to jarvis.py). Until then, ask here: the hub's own Jarvis answers and speaks on this phone.",
        talkHere());
    } else {
      composerNote.hidden = true;
    }
    input.placeholder = ready ? "e.g. open the Q3 report" : "Type a command (see the note above)";
  }

  function renderWhy() {
    const lines = status && !status.running && status.configured && Array.isArray(status.output_tail) ? status.output_tail : [];
    whyCard.hidden = lines.length === 0;
    if (lines.length) whyPre.textContent = lines.join("\n");
  }

  function renderLog() {
    logCard.hidden = !!(status && !status.configured);
    const lines = logExpanded && Array.isArray(fullLog) ? fullLog : (status && Array.isArray(status.log_tail) ? status.log_tail : []);
    const text = lines.length ? lines.join("\n") : "";
    const placeholder = !status ? "Loading…" : "No log lines yet.";
    const next = text || placeholder;
    logPre.classList.toggle("empty", !text);
    if (logPre.textContent === next) return;
    const atBottom = logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 30 || !logPre.textContent;
    logPre.textContent = next;
    if (atBottom) logPre.scrollTop = logPre.scrollHeight;
  }

  function render() {
    renderState();
    renderControls();
    renderComposer();
    renderWhy();
    renderLog();
  }

  async function refresh(manual = false) {
    if (fetching) return;
    fetching = true;
    if (!abort) abort = new AbortController();
    const signal = abort.signal;
    try {
      status = await api("/api/jarvis/status", { signal });
      loadError = "";
      if (logExpanded) await loadFullLog(signal);
    } catch (error) {
      if (error && error.name === "AbortError") { fetching = false; return; }
      if (error instanceof ApiError && error.status === 401) { fetching = false; close(); return; }
      loadError = error.message || "Something went wrong.";
      if (manual && status) ctx.toast(loadError);
    }
    fetching = false;
    if (opened) render();
  }

  async function loadFullLog(signal) {
    try {
      const data = await api("/api/jarvis/log?lines=200", { signal });
      fullLog = Array.isArray(data.lines) ? data.lines : [];
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      if (error instanceof ApiError && error.status === 401) throw error;
      logExpanded = false;
      moreBtn.textContent = "Show more";
      moreBtn.setAttribute("aria-pressed", "false");
      ctx.toast(error.message);
    }
  }

  async function toggleLog() {
    logExpanded = !logExpanded;
    moreBtn.setAttribute("aria-pressed", String(logExpanded));
    moreBtn.textContent = logExpanded ? "Show less" : "Show more";
    if (logExpanded) {
      moreBtn.disabled = true;
      try { await loadFullLog(abort && abort.signal); } catch (_) { /* aborted */ }
      moreBtn.disabled = false;
    }
    renderLog();
    logPre.scrollTop = logPre.scrollHeight;
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (!opened || document.hidden) return;
    timer = setTimeout(async () => {
      await refresh();
      schedule();
    }, POLL_MS);
  }

  // ---- start / stop ------------------------------------------------------------------------------
  function setActionMessage(text, bad = true) {
    actionNote.textContent = text || "";
    actionNote.hidden = !text;
    actionNote.classList.toggle("good", !bad);
  }

  async function startJarvis() {
    if (busy) return; // a second quick tap does nothing
    busy = "start";
    setActionMessage("");
    render();
    try {
      status = await api("/api/jarvis/start", { method: "POST" });
      loadError = "";
    } catch (error) {
      setActionMessage(error.message);
    }
    busy = null;
    if (opened) render();
  }

  async function askStop() {
    if (busy) return;
    const ok = await uiConfirm({ title: "Stop Jarvis?", message: "It will stop listening on the laptop until you start it again.",
      confirmLabel: "Yes, stop Jarvis", danger: true });
    if (ok) stopJarvis();
    else stopBtn.focus();
  }

  async function stopJarvis() {
    if (busy) return;
    busy = "stop";
    setActionMessage("");
    controlKey = "";
    render();
    try {
      status = await api("/api/jarvis/stop", { method: "POST", json: { confirm: true } });
      loadError = "";
    } catch (error) {
      setActionMessage(error.message);
    }
    busy = null;
    if (opened) render();
  }

  // ---- say ----------------------------------------------------------------------------------------
  let sayTimer = null;
  function sayMessageShow(text, bad) {
    clearTimeout(sayTimer);
    sayMessage.textContent = text || "";
    sayMessage.hidden = !text;
    sayMessage.classList.toggle("bad", !!bad);
    if (text && !bad) sayTimer = setTimeout(() => { sayMessage.hidden = true; }, 8000);
  }

  function renderSent() {
    sentList.replaceChildren(...sent.map((item) => el("button", { type: "button", class: "ui-chip ap-hist", title: item.time || "",
      onclick: () => { input.value = item.text; input.focus(); } }, icon("history", 14), el("span", { class: "ui-truncate" }, item.text))));
    sentList.hidden = sent.length === 0;
  }

  async function say() {
    if (sending) return;
    const text = input.value.trim();
    if (!text) { sayMessageShow("Type or say a command first.", true); input.focus(); return; }
    if (!canSay()) {
      const why = !status || !status.configured ? "Jarvis isn't set up on the laptop."
        : !status.running ? "Jarvis is not running. Tap Start Jarvis, then send again. Your text is kept."
        : "Jarvis can't take typed commands until the remote-inbox patch is installed. Use Ask Jarvis here instead.";
      sayMessageShow(why, true);
      renderComposer();
      composerNote.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
      return;
    }
    sending = true;
    sendBtn.disabled = true;
    sayMessageShow("");
    try {
      const data = await api("/api/jarvis/say", { method: "POST", json: { text } });
      const now = new Date();
      const at = sent.findIndex((x) => x.text === text);
      if (at >= 0) sent.splice(at, 1);
      sent.unshift({ text, time: `${pad(now.getHours())}:${pad(now.getMinutes())}` });
      sent.length = Math.min(sent.length, 8);
      try { localStorage.setItem(HIST_KEY, JSON.stringify(sent)); } catch (_) { /* ignore */ }
      haptic("success");
      renderSent();
      input.value = "";
      sayMessageShow(data && data.queued ? "Queued. Jarvis will run it and answer out loud on the laptop." : "Sent.", false);
      if (data && data.queued) { watchReply(text); if (voiceOn()) vkSpeak("Sent to Jarvis.", { assistant: "jarvis" }); }
    } catch (error) {
      sayMessageShow(error.message, true);
      if (error instanceof ApiError && error.status === 409) refresh();
    }
    sending = false;
    sendBtn.disabled = false;
  }

  // Show what Jarvis wrote to its log after a command was queued (its answer is spoken on the laptop; the log has the text).
  let replyRun = 0;
  async function watchReply(sentText) {
    const run = ++replyRun;
    let base = [];
    try { base = (await api("/api/fixit/log?source=jarvis&lines=40")).lines || []; } catch (_) { /* no log yet */ }
    const last = base.length ? base[base.length - 1] : null;
    replyPre.hidden = false;
    replyPre.textContent = `Sent: ${sentText}\nWaiting for Jarvis...`;
    for (let i = 0; i < 10 && run === replyRun && opened; i += 1) {
      await new Promise((r) => setTimeout(r, 2500));
      try {
        const now = (await api("/api/fixit/log?source=jarvis&lines=40")).lines || [];
        const at = last == null ? -1 : now.lastIndexOf(last);
        const fresh = at >= 0 ? now.slice(at + 1) : (last == null ? now : now.slice(-6));
        if (fresh.length) replyPre.textContent = `Sent: ${sentText}\n` + fresh.join("\n");
      } catch (_) { /* keep waiting */ }
    }
    if (run === replyRun && /Waiting for Jarvis\.\.\.$/.test(replyPre.textContent)) {
      replyPre.textContent += "\nNo log lines yet. Jarvis speaks its answer on the laptop; the full log is below.";
    }
  }

  // ---- voice ---------------------------------------------------------------------------------------
  function voiceProblem() {
    if (!ctx.secure) {
      return "The microphone only works on the secure (HTTPS) address, the Tailscale one. Open the hub from that address to dictate. You can still type the command.";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
      return "This browser can't record audio here. You can still type the command.";
    }
    return "";
  }

  function setPhase(phase) {
    rec.phase = phase;
    const recording = phase === "recording";
    micBtn.classList.toggle("recording", recording);
    micBtn.setAttribute("aria-label", recording ? "Stop recording" : "Record a command");
    micBtn.replaceChildren(...[icon(recording ? "stop" : "assistant", 22), recording ? el("span", { class: "ap-mic-time" }, "0:00") : null].filter(Boolean));
    micCancel.hidden = !recording;
    micBtn.disabled = phase === "uploading" || !canSay();
    if (phase === "idle") micStatus.textContent = micStatus.dataset.keep === "1" ? micStatus.textContent : "";
    else if (recording) micStatus.textContent = "Recording. Tap the microphone again to stop.";
    else micStatus.textContent = "Writing it down…";
  }

  function micProblem(text) { micStatus.textContent = text; micStatus.dataset.keep = text ? "1" : ""; }

  function releaseStream() {
    if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop());
    rec.stream = null;
  }

  function toggleMic() {
    if (rec.phase === "idle") startRecording();
    else if (rec.phase === "recording") finishRecording(false);
  }

  async function startRecording() {
    micProblem("");
    const problem = voiceProblem();
    if (problem) { micProblem(problem); return; }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      const name = error && error.name;
      if (name === "NotAllowedError" || name === "SecurityError" || name === "NotSupportedError") {
        micProblem("Microphone permission was blocked. Tap the lock icon in the address bar, allow Microphone for this site, then try again. You can still type the command.");
      } else if (name === "NotFoundError" || name === "OverconstrainedError") {
        micProblem("No microphone was found on this device.");
      } else if (name === "NotReadableError") {
        micProblem("The microphone is busy in another app. Close it and try again.");
      } else {
        micProblem(`Couldn't start the microphone: ${(error && error.message) || name || "unknown error"}`);
      }
      return;
    }
    if (!opened) { stream.getTracks().forEach((t) => t.stop()); return; }
    const mimeType = pickMimeType();
    let recorder;
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch (error) {
      stream.getTracks().forEach((t) => t.stop());
      micProblem(`This browser can't record audio: ${error.message}`);
      return;
    }
    rec.stream = stream;
    rec.recorder = recorder;
    rec.chunks = [];
    rec.cancelled = false;
    recorder.addEventListener("dataavailable", (event) => { if (event.data && event.data.size) rec.chunks.push(event.data); });
    recorder.addEventListener("error", () => { micProblem("Recording stopped unexpectedly."); finishRecording(true); });
    recorder.addEventListener("stop", onRecorderStop);
    recorder.start(1000);
    rec.started = Date.now();
    setPhase("recording");
    clearInterval(rec.tick);
    rec.tick = setInterval(() => {
      const label = micBtn.querySelector(".ap-mic-time");
      if (label) label.textContent = clock(Math.floor((Date.now() - rec.started) / 1000));
    }, 500);
    clearTimeout(rec.limit);
    rec.limit = setTimeout(() => {
      ctx.toast("Reached the 60 second limit.");
      finishRecording(false);
    }, MAX_RECORD_SECONDS * 1000);
  }

  function finishRecording(cancel) {
    clearInterval(rec.tick);
    clearTimeout(rec.limit);
    rec.tick = null;
    rec.limit = null;
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
    if (rec.cancelled || !recorder) { micProblem(""); setPhase("idle"); return; }
    const type = recorder.mimeType || pickMimeType() || "audio/webm";
    const blob = new Blob(rec.chunks, { type });
    rec.chunks = [];
    if (blob.size < 200) {
      setPhase("idle");
      micProblem("That recording was empty. Hold on a little longer and try again.");
      return;
    }
    setPhase("uploading");
    const form = new FormData();
    form.append("file", blob, `command.${extensionFor(type)}`);
    try {
      const data = await api("/api/transcribe", { method: "POST", form });
      const text = String((data && data.text) || "").trim();
      micProblem("");
      setPhase("idle");
      if (!text) {
        micProblem("I didn't catch anything. Try again a little closer to the microphone.");
        return;
      }
      input.value = text;
      micStatus.textContent = autoBox.checked ? "Heard it. Sending…" : "Check the words, then tap Send.";
      if (autoBox.checked && canSay()) await say();
      else input.focus();
    } catch (error) {
      setPhase("idle");
      micProblem(error.message);
    }
  }

  // ---- open / close -----------------------------------------------------------------------------------
  function onVisibility() {
    if (!opened) return;
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else {
      refresh();
      schedule();
    }
  }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("hub:unauthorized", () => close());

  function open() {
    if (opened) return;
    opened = true;
    abort = new AbortController();
    render();
    renderSent();
    setPhase("idle");
    kit.refresh();
    refresh().then(() => { if (opened) schedule(); });
  }

  function close() {
    opened = false;
    kit.stop();
    clearTimeout(timer);
    timer = null;
    if (abort) abort.abort();
    abort = null;
    fetching = false;
    if (rec.phase === "recording") finishRecording(true);
    else releaseStream();
    if (rec.phase === "recording") setPhase("idle");
    controlKey = "";
  }

  return { open, close };
}
