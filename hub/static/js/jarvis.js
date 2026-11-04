// Jarvis screen: start and stop the owner's Jarvis voice assistant on the laptop, send it a typed or
// dictated command, read its log. Status is polled every 5 s, only while this panel is open and visible.
import { api, ApiError, el } from "./api.js";
import { icon, spinner } from "./laptop.js";

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
  const sent = [];
  const rec = { phase: "idle", stream: null, recorder: null, chunks: [], started: 0, tick: null, limit: null, cancelled: false };

  // ---- skeleton -------------------------------------------------------------------------------
  const heading = el("h2", { class: "ap-title", tabindex: "-1" }, "Jarvis");
  const stateCard = el("div", { class: "card ap-state", role: "status" });
  const startBtn = el("button", { type: "button", class: "btn primary ap-big", onclick: () => startJarvis() });
  const stopBtn = el("button", { type: "button", class: "btn ap-big ap-stop", onclick: () => askStop() });
  const controls = el("div", { class: "ap-bigrow" }, startBtn, stopBtn);
  const stopConfirm = el("div", { class: "ap-confirm", role: "alertdialog", "aria-labelledby": "ap-jv-stop-h", hidden: true });
  const actionNote = el("p", { class: "ap-form-error", role: "alert", hidden: true });

  const composerNote = el("div", { class: "ap-card-info", hidden: true });
  const input = el("input", { type: "text", class: "ap-say-input", id: "ap-jv-say", autocomplete: "off",
    autocapitalize: "sentences", enterkeyhint: "send", placeholder: "e.g. open the Q3 report" });
  const sendBtn = el("button", { type: "submit", class: "btn primary ap-send" }, icon("send", 18), "Send");
  const micBtn = el("button", { type: "button", class: "btn ap-mic", "aria-label": "Record a command", onclick: () => toggleMic() }, icon("assistant", 22));
  const micCancel = el("button", { type: "button", class: "btn", hidden: true, onclick: () => finishRecording(true) }, "Cancel");
  const micStatus = el("p", { class: "muted ap-hint", "aria-live": "polite" });
  const autoBox = el("input", { type: "checkbox", role: "switch", class: "ap-switch", id: "ap-jv-auto" });
  try { autoBox.checked = localStorage.getItem(AUTO_KEY) === "1"; } catch (_) { /* ignore */ }
  autoBox.addEventListener("change", () => {
    try { localStorage.setItem(AUTO_KEY, autoBox.checked ? "1" : "0"); } catch (_) { /* ignore */ }
  });
  const sayMessage = el("p", { class: "ap-say-msg", role: "status", hidden: true });
  const sentList = el("ul", { class: "ap-sent", "aria-label": "Commands sent from this phone" });
  const sayForm = el("form", { class: "ap-say-form", autocomplete: "off" },
    el("label", { class: "sr-only", for: "ap-jv-say" }, "Command for Jarvis"),
    el("div", { class: "ap-say-row" }, input, sendBtn),
    el("div", { class: "ap-say-tools" }, micBtn, micCancel,
      el("label", { class: "ap-auto", for: "ap-jv-auto" }, autoBox, el("span", {}, "Send automatically after dictating"))),
    micStatus, sayMessage);
  sayForm.addEventListener("submit", (event) => { event.preventDefault(); say(); });
  const composerCard = el("section", { class: "card ap-composer", "aria-labelledby": "ap-jv-tell-h" },
    el("h3", { id: "ap-jv-tell-h" }, "Tell Jarvis"), composerNote, sayForm, sentList);

  const exampleButtons = EXAMPLES.map((text) => el("button", { type: "button", class: "ap-example",
    onclick: () => { input.value = text; input.focus(); } }, text));
  const helpCard = el("section", { class: "card", "aria-labelledby": "ap-jv-help-h" },
    el("h3", { id: "ap-jv-help-h" }, "Commands you can say"),
    el("p", { class: "muted" }, "Say \"Hey Jarvis\" near the laptop, then one of these. Or type it above."),
    el("div", { class: "ap-examples" }, exampleButtons));

  const whyPre = el("pre", { class: "ap-log ap-why-pre", tabindex: "0" });
  const whyCard = el("section", { class: "card", "aria-labelledby": "ap-jv-why-h", hidden: true },
    el("h3", { id: "ap-jv-why-h" }, "Why it may have stopped"), whyPre);

  const logPre = el("pre", { class: "ap-log", tabindex: "0", role: "log", "aria-label": "Jarvis log" });
  const moreBtn = el("button", { type: "button", class: "btn", "aria-pressed": "false", onclick: () => toggleLog() }, "Show more");
  const logRefresh = el("button", { type: "button", class: "btn", onclick: () => refresh(true) }, icon("refresh", 18), "Refresh");
  const logCard = el("section", { class: "card", "aria-labelledby": "ap-jv-log-h" },
    el("h3", { id: "ap-jv-log-h" }, "Log"), logPre, el("div", { class: "ap-row wrap" }, logRefresh, moreBtn));

  container.append(
    el("div", { class: "ap-runner-head" }, heading, el("p", { class: "muted ap-desc" }, "Your voice assistant on the laptop. Start or stop it, and send it commands from here.")),
    stateCard, controls, stopConfirm, actionNote, composerCard, helpCard, whyCard, logCard);

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
          el("button", { type: "button", class: "btn", onclick: () => refresh(true) }, icon("refresh", 18), "Retry"))));
    } else if (!status.configured) {
      nodes.push(el("div", { class: "ap-statehead" }, el("span", { class: "ap-dot off", "aria-hidden": "true" }),
        el("strong", {}, "Jarvis isn't set up")),
        el("p", {}, status.reason || "Tell the hub where the Jarvis folder is in its settings (jarvis_dir)."));
    } else if (status.running) {
      const bits = [status.pid != null ? `pid ${status.pid}` : "", sinceText(status.started_at)].filter(Boolean);
      nodes.push(el("div", { class: "ap-statehead" }, el("span", { class: "ap-dot on", "aria-hidden": "true" }),
        el("strong", {}, "Jarvis is running")), bits.length ? el("p", { class: "muted" }, bits.join(" · ")) : null);
    } else {
      nodes.push(el("div", { class: "ap-statehead" }, el("span", { class: "ap-dot off", "aria-hidden": "true" }),
        el("strong", {}, "Jarvis is stopped")), el("p", { class: "muted" }, "Tap Start Jarvis to begin listening on the laptop."));
    }
    if (status && loadError) {
      nodes.push(el("p", { class: "ap-form-error" }, `Can't reach the laptop right now. Showing the last known state. ${loadError}`));
    }
    stateCard.replaceChildren(...nodes.filter(Boolean));
  }

  function renderControls() {
    const key = JSON.stringify([status && status.configured, status && status.running, busy, stopConfirm.hidden]);
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

  function renderComposer() {
    const configured = !!(status && status.configured);
    const running = !!(status && status.running);
    const ready = canSay();
    composerCard.hidden = !configured && !!status;
    input.disabled = !ready;
    sendBtn.disabled = !ready || sending;
    for (const b of exampleButtons) b.disabled = !ready;
    micBtn.disabled = !ready || rec.phase === "uploading";
    if (!status) {
      composerNote.hidden = true;
    } else if (!running) {
      composerNote.hidden = false;
      composerNote.replaceChildren(icon("info", 20), el("div", {}, el("strong", {}, "Start it first"),
        el("p", {}, "Jarvis has to be running before it can take a command.")));
    } else if (!status.remote_ready) {
      composerNote.hidden = false;
      composerNote.replaceChildren(icon("info", 20), el("div", {}, el("strong", {}, "Typing commands needs a small patch"),
        el("p", {}, "Jarvis is running and still listens to the laptop's microphone, so you can talk to it in the room. To send commands from this phone, Jarvis needs a small optional patch."),
        el("p", {}, "It's described in the file docs/jarvis-remote-inbox.md in the laptop-hub folder: it adds remote_inbox.py next to jarvis.py and a few lines in jarvis.py. Until then this box stays off.")));
    } else {
      composerNote.hidden = true;
    }
    input.placeholder = ready ? "e.g. open the Q3 report" : "Not available yet";
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

  function askStop() {
    if (busy) return;
    const cancel = el("button", { type: "button", class: "btn", onclick: () => hideStop() }, "Cancel");
    const yes = el("button", { type: "button", class: "btn danger", onclick: () => stopJarvis() }, "Yes, stop Jarvis");
    stopConfirm.replaceChildren(
      el("h3", { id: "ap-jv-stop-h" }, "Stop Jarvis?"),
      el("p", { class: "muted" }, "It will stop listening on the laptop until you start it again."),
      el("div", { class: "ap-row ap-confirm-row" }, cancel, yes));
    stopConfirm.hidden = false;
    controlKey = "";
    renderControls();
    cancel.focus();
  }

  function hideStop() {
    stopConfirm.hidden = true;
    stopConfirm.replaceChildren();
    controlKey = "";
    renderControls();
    stopBtn.focus();
  }

  async function stopJarvis() {
    if (busy) return;
    stopConfirm.hidden = true;
    stopConfirm.replaceChildren();
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
    sentList.replaceChildren(...sent.map((item) => el("li", {}, el("span", { class: "muted" }, item.time), " ", item.text)));
    sentList.hidden = sent.length === 0;
  }

  async function say() {
    if (sending) return;
    const text = input.value.trim();
    if (!text) { sayMessageShow("Type or say a command first.", true); input.focus(); return; }
    sending = true;
    sendBtn.disabled = true;
    sayMessageShow("");
    try {
      const data = await api("/api/jarvis/say", { method: "POST", json: { text } });
      const now = new Date();
      sent.unshift({ text, time: `${pad(now.getHours())}:${pad(now.getMinutes())}` });
      sent.length = Math.min(sent.length, 4);
      renderSent();
      input.value = "";
      sayMessageShow(data && data.queued ? "Queued. Jarvis will run it and answer out loud on the laptop." : "Sent.", false);
    } catch (error) {
      sayMessageShow(error.message, true);
      if (error instanceof ApiError && error.status === 409) refresh();
    }
    sending = false;
    sendBtn.disabled = !canSay();
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
    refresh().then(() => { if (opened) schedule(); });
  }

  function close() {
    opened = false;
    clearTimeout(timer);
    timer = null;
    if (abort) abort.abort();
    abort = null;
    fetching = false;
    if (rec.phase === "recording") finishRecording(true);
    else releaseStream();
    if (rec.phase === "recording") setPhase("idle");
    stopConfirm.hidden = true;
    stopConfirm.replaceChildren();
    controlKey = "";
  }

  return { open, close };
}
