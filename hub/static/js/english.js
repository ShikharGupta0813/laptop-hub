// English coach: spoken-English fluency practice. Lives inside the Learn tab as its third section.
// Five parts: Speak (record and get feedback), Role-play, Fix writing, Words, Progress.
// Everything that comes from the transcriber or a model is put on the page with textContent only.
import { api, sse, el } from "./api.js";

const MAX_SPEAK_SECONDS = 180;
const MAX_ROLEPLAY_SECONDS = 90;
const SECTIONS = [
  { id: "speak", label: "Speak" },
  { id: "roleplay", label: "Role-play" },
  { id: "correct", label: "Fix writing" },
  { id: "vocab", label: "Words" },
  { id: "progress", label: "Progress" },
];

const ICONS = {
  mic: "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V22h2v-3.1A7 7 0 0 0 19 12h-2Z",
  stop: "M7 7h10v10H7z",
  send: "M3 20.5v-6.7l9-1.8-9-1.8V3.5L22 12 3 20.5Z",
  speaker: "M3 9v6h4l5 4V5L7 9H3Zm13.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4Z",
  copy: "M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1Zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2Zm0 16H8V7h11v14Z",
};

// ---------- small helpers ----------

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null && value !== false) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}
function icon(name) {
  return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false", class: "eng-icon" },
    svg("path", { d: ICONS[name] }));
}

const store = {
  get(key, fallback = "") {
    try { const v = localStorage.getItem(key); return v == null ? fallback : v; } catch (_) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch (_) { /* private mode */ }
  },
  sget(key) {
    try { return sessionStorage.getItem(key) || ""; } catch (_) { return ""; }
  },
  sset(key, value) {
    try { sessionStorage.setItem(key, value); } catch (_) { /* ignore */ }
  },
};

function randomId() {
  const bytes = new Uint8Array(12);
  if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return "rp-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function clock(seconds) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function sr(text) { return el("span", { class: "eng-sr" }, text); }

function loadingBox(text) {
  return el("div", { class: "eng-loading", role: "status" }, el("span", { class: "eng-spinner", "aria-hidden": "true" }), el("span", {}, text));
}

// A calm message for a failed request. 422 and 503 mean the server understood but could not do it.
function friendly(error) {
  const text = (error && error.message) || "Something went wrong.";
  if (error && error.status === 422) return text;
  if (error && error.status === 503) {
    return `${text} The model on your laptop is not ready right now. Wait a moment and try again, or switch to Claude at the top.`;
  }
  return text;
}

function errorBox(error, retry, retryLabel = "Try again") {
  const box = el("div", { class: "eng-error", role: "alert" }, el("p", {}, friendly(error)));
  if (retry) box.append(el("button", { class: "btn", type: "button", onclick: retry }, retryLabel));
  return box;
}

function emptyBox(title, text) {
  return el("div", { class: "eng-empty" }, el("strong", {}, title), text ? el("p", { class: "muted" }, text) : null);
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through to the old way */ }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  area.remove();
  return ok;
}

function parseDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
function shortDate(iso) {
  const d = parseDay(iso);
  return d ? d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "";
}
function friendlyDue(iso) {
  const d = parseDay(iso);
  if (!d) return String(iso || "");
  const now = new Date();
  const days = Math.round((d - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
  if (days <= 0) return "later today";
  if (days === 1) return "tomorrow";
  if (days <= 14) return `in ${days} days`;
  return shortDate(iso);
}

// ---------- shared pieces: correction, score meter, word card ----------

function correctionBlock(item, compact = false) {
  const block = el("div", { class: "eng-corr" + (compact ? " compact" : "") },
    el("div", { class: "eng-corr-line" },
      el("span", { class: "said" }, sr("You said: "), String(item.said || item.from || "")),
      el("span", { class: "arrow", "aria-hidden": "true" }, "→"),
      el("span", { class: "better" }, sr(" Better: "), String(item.better || item.to || ""))));
  if (item.why) block.append(el("div", { class: "why" }, String(item.why)));
  return block;
}

function meter(label, value, max = 10) {
  const n = Number(value);
  const ok = Number.isFinite(n);
  const pct = ok ? Math.max(0, Math.min(100, (n / max) * 100)) : 0;
  const bar = el("div", {
    class: "eng-meter-bar", role: "meter", "aria-label": `${label} score`,
    "aria-valuemin": "0", "aria-valuemax": String(max), "aria-valuenow": ok ? String(n) : null,
    "aria-valuetext": ok ? `${n} out of ${max}` : "no score",
  }, el("span", { class: "fill", style: `width:${pct}%` }));
  return el("div", { class: "eng-meter" },
    el("div", { class: "eng-meter-head" }, el("span", {}, label), el("strong", {}, ok ? `${n}/${max}` : "-")), bar);
}

function wordCard(w, ctx) {
  return el("div", { class: "card eng-word" },
    el("div", { class: "eng-word-head" },
      el("h4", { class: "eng-word-title" }, String(w.word)),
      w.level ? el("span", { class: "eng-badge" }, sr("Level "), String(w.level)) : null,
      el("button", { class: "eng-iconbtn", type: "button", "aria-label": `Listen to the word ${w.word}`,
        onclick: () => ctx.speak(`${w.word}. ${w.example || ""}`) }, icon("speaker"))),
    el("p", { class: "eng-word-meaning" }, String(w.meaning || "")),
    w.example ? el("p", { class: "eng-word-example" }, String(w.example)) : null);
}

// ---------- recorder: tap to start, tap to stop ----------

const recorders = new Set();

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

function createRecorder(ctx, { big, name, maxSeconds, startLabel, stopLabel, idleText = "", onAudio, onStateChange }) {
  const problem = (() => {
    if (!ctx.secure) {
      return "The microphone needs the secure (HTTPS) address of your laptop, the Tailscale one. This page is on a plain address, so it can't record here. Open the hub from the HTTPS address to speak.";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
      return "This browser can't record audio on this page, so the microphone is off here.";
    }
    return "";
  })();

  const button = el("button", { class: "eng-mic" + (big ? " big" : ""), type: "button", "aria-label": startLabel });
  const micIcon = icon("mic");
  button.append(micIcon);
  const timer = el("span", { class: "eng-timer", "aria-hidden": "true" }, "0:00");
  const status = el("p", { class: "eng-mic-status", role: "status", "aria-live": "polite" }, idleText);
  const note = el("p", { class: "eng-note", role: "status", hidden: !problem }, problem);
  if (problem) button.disabled = true;

  const rec = { recorder: null, stream: null, chunks: [], started: 0, tick: null, limit: null, cancelled: false, busy: false };

  function setIcon(recording) {
    micIcon.firstChild.setAttribute("d", recording ? ICONS.stop : ICONS.mic);
    button.classList.toggle("recording", recording);
    button.setAttribute("aria-label", recording ? stopLabel : startLabel);
    timer.classList.toggle("on", recording);
    button.setAttribute("aria-pressed", recording ? "true" : "false");
  }
  function showProblem(message) {
    note.textContent = message;
    note.hidden = !message;
  }
  function release() {
    if (rec.stream) rec.stream.getTracks().forEach((t) => { try { t.stop(); } catch (_) { /* ignore */ } });
    rec.stream = null;
    clearInterval(rec.tick); clearTimeout(rec.limit);
    rec.tick = null; rec.limit = null;
  }
  function tickTimer() {
    timer.textContent = clock(Math.floor((Date.now() - rec.started) / 1000));
  }

  async function start() {
    if (problem || rec.busy || rec.recorder) return;
    showProblem("");
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      const errName = error && error.name;
      if (errName === "NotAllowedError" || errName === "SecurityError" || errName === "NotSupportedError") {
        showProblem("The microphone is blocked for this site. Tap the lock icon in the address bar, allow Microphone, then try again.");
      } else if (errName === "NotFoundError" || errName === "OverconstrainedError") {
        showProblem("No microphone was found on this device.");
      } else if (errName === "NotReadableError") {
        showProblem("The microphone is busy in another app. Close that app and try again.");
      } else {
        showProblem(`Couldn't start the microphone: ${(error && error.message) || errName || "unknown error"}`);
      }
      return;
    }
    let recorder;
    try {
      const mimeType = pickMimeType();
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    } catch (error) {
      stream.getTracks().forEach((t) => t.stop());
      showProblem(`This browser can't record audio: ${error.message}`);
      return;
    }
    rec.stream = stream; rec.recorder = recorder; rec.chunks = []; rec.cancelled = false;
    recorder.addEventListener("dataavailable", (event) => { if (event.data && event.data.size) rec.chunks.push(event.data); });
    recorder.addEventListener("error", () => { showProblem("Recording stopped unexpectedly. Please try again."); stop(true); });
    recorder.addEventListener("stop", onStop);
    recorder.start(1000);
    rec.started = Date.now();
    setIcon(true);
    status.textContent = `Recording. Tap again to stop. Up to ${clock(maxSeconds)}.`;
    tickTimer();
    rec.tick = setInterval(tickTimer, 500);
    rec.limit = setTimeout(() => {
      ctx.toast(`Reached the ${clock(maxSeconds)} limit, stopping.`);
      stop(false);
    }, maxSeconds * 1000);
    if (onStateChange) onStateChange("recording");
  }

  function stop(cancel) {
    rec.cancelled = cancel;
    clearInterval(rec.tick); clearTimeout(rec.limit);
    const recorder = rec.recorder;
    if (recorder && recorder.state !== "inactive") {
      try { recorder.stop(); } catch (_) { onStop(); }
    } else {
      onStop();
    }
  }

  function onStop() {
    const recorder = rec.recorder;
    rec.recorder = null;
    release();
    setIcon(false);
    timer.textContent = "0:00";
    status.textContent = idleText;
    if (onStateChange) onStateChange("idle");
    if (rec.cancelled || !recorder) { rec.chunks = []; return; }
    const type = recorder.mimeType || pickMimeType() || "audio/webm";
    const blob = new Blob(rec.chunks, { type });
    rec.chunks = [];
    if (blob.size < 200) {
      showProblem("That recording was empty. Try again and speak for a little longer.");
      return;
    }
    onAudio(blob, `${name}.${extensionFor(type)}`);
  }

  button.addEventListener("click", () => { if (rec.recorder) stop(false); else start(); });

  const api2 = {
    button, timer, status, note, problem,
    isRecording: () => !!rec.recorder,
    cancel() { if (rec.recorder) stop(true); else release(); },
    setBusy(busy) {
      rec.busy = busy;
      button.disabled = !!problem || busy;
    },
    showProblem,
  };
  recorders.add(api2);
  return api2;
}

// ---------- transcript marking ----------

const FILLER_VARIANTS = {
  um: ["um", "umm"], uh: ["uh", "uhh"], er: ["er", "erm"], ah: ["ah"], hmm: ["hmm", "hmmm"],
  basically: ["basically"], actually: ["actually"],
};
function cleanToken(word) {
  return word.toLowerCase().replace(/’/g, "'").replace(/^[^\p{L}\p{N}']+|[^\p{L}\p{N}']+$/gu, "").replace(/^'+|'+$/g, "");
}

// Returns a list of nodes: plain text, <mark class="filler"> and <mark class="unclear">.
function markTranscript(text, fillerKeys, lowWords) {
  const pieces = text.split(/(\s+)/);          // words and the spaces between them
  const wordAt = [];                            // indexes in `pieces` that are words
  pieces.forEach((p, i) => { if (p && !/^\s+$/.test(p)) wordAt.push(i); });
  const clean = wordAt.map((i) => cleanToken(pieces[i]));
  const singles = new Set();
  const doubles = [];
  for (const key of fillerKeys) {
    const k = String(key).toLowerCase();
    if (k.includes(" ")) doubles.push(k.split(" "));
    else (FILLER_VARIANTS[k] || [k]).forEach((v) => singles.add(v));
  }
  const low = new Set(lowWords.map(cleanToken).filter(Boolean));
  const kind = new Array(wordAt.length).fill("");
  const span = new Array(wordAt.length).fill(1);
  for (let i = 0; i < wordAt.length; i += 1) {
    const pair = doubles.find((d) => d.length === 2 && clean[i] === d[0] && clean[i + 1] === d[1]);
    if (pair) { kind[i] = "filler"; span[i] = 2; i += 1; continue; }
    if (singles.has(clean[i])) kind[i] = "filler";
    else if (low.has(clean[i])) kind[i] = "unclear";
  }
  const out = [];
  let cursor = 0;
  for (let w = 0; w < wordAt.length; w += 1) {
    if (!kind[w]) continue;
    const startPiece = wordAt[w];
    const endPiece = wordAt[w + span[w] - 1];
    if (startPiece > cursor) out.push(pieces.slice(cursor, startPiece).join(""));
    const label = kind[w] === "filler" ? " (filler word)" : " (may have been unclear)";
    out.push(el("mark", { class: kind[w] }, pieces.slice(startPiece, endPiece + 1).join(""), sr(label)));
    cursor = endPiece + 1;
    w += span[w] - 1;
  }
  if (cursor < pieces.length) out.push(pieces.slice(cursor).join(""));
  return out;
}

// ---------- charts (inline SVG) ----------

function niceMax(value, step) { return Math.max(step, Math.ceil(value / step) * step); }

function lineChart({ title, caption, unit, values, dates, max, ticks, format, tone }) {
  const W = 320, H = 190, L = 40, R = 16, T = 20, B = 46;
  const plotW = W - L - R, plotH = H - T - B;
  const n = values.length;
  const xAt = (i) => (n === 1 ? L + plotW / 2 : L + (plotW * i) / (n - 1));
  const yAt = (v) => T + plotH - (Math.max(0, Math.min(max, v)) / max) * plotH;
  const present = values.map((v, i) => ({ v, i })).filter((p) => Number.isFinite(p.v));
  const first = present[0], last = present[present.length - 1];
  const noData = present.length === 0;
  const summary = present.length
    ? `${title}. ${present.length} sessions. First ${format(first.v)}, latest ${format(last.v)}.`
    : `${title}. No data yet.`;
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: `eng-chart ${tone}`, role: "img", "aria-label": summary, preserveAspectRatio: "xMidYMid meet" });
  // grid lines and y axis labels
  for (const t of ticks) {
    const y = yAt(t);
    chart.append(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid" }));
    chart.append(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "tick" }, format(t, true)));
  }
  chart.append(svg("text", { x: 4, y: 10, "text-anchor": "start", class: "axis-title" }, unit));
  chart.append(svg("line", { x1: L, x2: L, y1: T, y2: T + plotH, class: "axis" }));
  chart.append(svg("line", { x1: L, x2: W - R, y1: T + plotH, y2: T + plotH, class: "axis" }));
  // the line, broken where a session has no value
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (!Number.isFinite(v)) { pen = false; return; }
    d += `${pen ? "L" : "M"}${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)} `;
    pen = true;
  });
  if (d) chart.append(svg("path", { d, class: "line", fill: "none" }));
  values.forEach((v, i) => {
    if (Number.isFinite(v)) chart.append(svg("circle", { cx: xAt(i), cy: yAt(v), r: 3.5, class: "dot" }));
  });
  if (last) {
    const lx = xAt(last.i);
    const anchor = lx > W - 50 ? "end" : "middle";
    chart.append(svg("text", { x: anchor === "end" ? lx + 4 : lx, y: yAt(last.v) - 9, "text-anchor": anchor, class: "val" }, format(last.v)));
  }
  // x axis: first and latest date, and the axis title
  if (n > 0) {
    chart.append(svg("text", { x: xAt(0), y: T + plotH + 16, "text-anchor": n === 1 ? "middle" : "start", class: "tick" }, `#1 ${shortDate(dates[0])}`.trim()));
    if (n > 1) chart.append(svg("text", { x: xAt(n - 1), y: T + plotH + 16, "text-anchor": "end", class: "tick" }, `#${n} ${shortDate(dates[n - 1])}`.trim()));
  }
  if (noData) chart.append(svg("text", { x: L + plotW / 2, y: T + plotH / 2, "text-anchor": "middle", class: "tick" }, "No data for this yet"));
  chart.append(svg("text", { x: L + plotW / 2, y: H - 8, "text-anchor": "middle", class: "axis-title" },
    n > 1 ? `Sessions, oldest to newest (${n})` : "Session"));
  return el("figure", { class: "card eng-chart-card" },
    el("figcaption", {}, el("strong", {}, title), el("span", { class: "muted" }, caption)), chart);
}

// ---------- the module ----------

export function init(container, ctx) {
  const root = el("div", { class: "english-root" });
  container.append(root);

  let provider = store.get("hub.provider.english", "local") === "claude" ? "claude" : "local";

  // ----- scenarios (shared by Speak and Role-play)
  let scenarioPromise = null;
  function loadScenarios() {
    if (!scenarioPromise) {
      scenarioPromise = api("/api/english/scenarios").then((d) => d.scenarios || []);
      scenarioPromise.catch(() => { scenarioPromise = null; });
    }
    return scenarioPromise;
  }
  function fillScenarioSelect(select, { allowFree, remember }) {
    select.replaceChildren();
    if (allowFree) select.append(el("option", { value: "" }, "Free talk (no scenario)"));
    return loadScenarios().then((list) => {
      for (const s of list) select.append(el("option", { value: s.id }, s.title));
      const saved = store.get(remember);
      if (saved && list.some((s) => s.id === saved)) select.value = saved;
      else if (!allowFree && list[0]) select.value = list[0].id;
      select.dispatchEvent(new Event("change"));
      return list;
    });
  }
  function describe(list, id) {
    const found = list.find((s) => s.id === id);
    return found ? found.description : "";
  }

  // ----- top: model switch and section bar
  const providerBar = el("div", { class: "eng-provider", role: "group", "aria-label": "Which model gives the feedback" },
    el("span", { class: "muted" }, "Feedback from"));
  const providerButtons = {};
  for (const [id, label] of [["local", "Local"], ["claude", "Claude"]]) {
    const button = el("button", { class: "chip", type: "button", "aria-pressed": String(provider === id),
      onclick: () => {
        provider = id;
        store.set("hub.provider.english", id);
        for (const [pid, b] of Object.entries(providerButtons)) b.setAttribute("aria-pressed", String(pid === id));
      } }, label);
    providerButtons[id] = button;
    providerBar.append(button);
  }

  const nav = el("div", { class: "eng-seg", role: "tablist", "aria-label": "English practice" });
  root.append(providerBar, nav);

  const parts = {};
  const panes = {};
  let current = null;

  function select(id, { focus = false } = {}) {
    if (!parts[id]) id = "speak";
    if (current && current !== id) {
      for (const r of recorders) r.cancel();
      parts[current].leave?.();
    }
    current = id;
    for (const s of SECTIONS) {
      panes[s.id].hidden = s.id !== id;
      const tab = nav.querySelector(`[data-part="${s.id}"]`);
      tab.setAttribute("aria-selected", String(s.id === id));
      tab.tabIndex = s.id === id ? 0 : -1;
    }
    store.set("hub.english.section", id);
    parts[id].enter?.();
    if (focus) nav.querySelector(`[data-part="${id}"]`).focus();
  }
  for (const s of SECTIONS) {
    nav.append(el("button", { class: "eng-tab", type: "button", role: "tab", "data-part": s.id, onclick: () => select(s.id) }, s.label));
  }
  nav.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    const index = SECTIONS.findIndex((s) => s.id === current);
    const next = SECTIONS[(index + (event.key === "ArrowRight" ? 1 : SECTIONS.length - 1)) % SECTIONS.length];
    event.preventDefault();
    select(next.id, { focus: true });
  });

  // =========================================================================================
  // 1. SPEAK
  // =========================================================================================
  function buildSpeak() {
    const pane = el("section", { class: "eng-pane", "aria-label": "Speak" });
    const scenarioSelect = el("select", { id: "eng-speak-scenario", "aria-describedby": "eng-speak-scenario-desc" });
    const scenarioDesc = el("p", { class: "muted eng-small", id: "eng-speak-scenario-desc" });
    const promptBtn = el("button", { class: "btn", type: "button" }, "Give me a topic");
    const promptBox = el("div", { class: "eng-prompt-slot", "aria-live": "polite" });
    const result = el("div", { class: "eng-result" });
    const announce = el("div", { class: "eng-sr", role: "status", "aria-live": "polite" });
    let promptText = "";
    let loaded = false;
    let lastBlob = null;
    let busy = false;
    let scenarioList = [];

    function setBusy(b) {
      busy = b;
      recorder.setBusy(b);
      result.setAttribute("aria-busy", String(b));
    }

    const recorder = createRecorder(ctx, {
      big: true, name: "speaking", maxSeconds: MAX_SPEAK_SECONDS,
      startLabel: "Start recording", stopLabel: "Stop recording and get feedback",
      idleText: `Tap to start, tap to stop. Up to ${MAX_SPEAK_SECONDS / 60} minutes.`,
      onAudio: (blob, filename) => { lastBlob = { blob, filename }; submit(); },
      onStateChange: (state) => { if (state === "recording") result.replaceChildren(); },
    });

    scenarioSelect.addEventListener("change", () => {
      store.set("hub.english.scenario.speak", scenarioSelect.value);
      scenarioDesc.textContent = scenarioSelect.value ? describe(scenarioList, scenarioSelect.value) : "Talk about anything you like.";
    });

    promptBtn.addEventListener("click", async () => {
      promptBtn.disabled = true;
      promptBox.replaceChildren(loadingBox("Thinking of a topic..."));
      try {
        const data = await api("/api/english/practice-prompt", {
          method: "POST", json: { scenario: scenarioSelect.value, provider } });
        promptText = String(data.prompt || "");
        promptBox.replaceChildren(el("div", { class: "card eng-prompt" },
          el("div", { class: "eng-label" }, "Your topic"),
          el("p", { class: "eng-prompt-text" }, promptText),
          data.source === "builtin"
            ? el("p", { class: "muted eng-small" }, "This is a built-in prompt, because the model wasn't available just now.")
            : null));
      } catch (error) {
        promptBox.replaceChildren(errorBox(error));
      } finally {
        promptBtn.disabled = false;
      }
    });

    async function submit() {
      if (!lastBlob || busy) return;
      setBusy(true);
      announce.textContent = "";
      result.replaceChildren(el("div", { class: "card" }, loadingBox("Listening to what you said..."),
        el("p", { class: "muted eng-small" }, "This can take up to a minute on a laptop without a graphics card.")));
      result.scrollIntoView({ block: "nearest" });
      const form = new FormData();
      form.append("file", lastBlob.blob, lastBlob.filename);
      form.append("scenario", scenarioSelect.value);
      form.append("prompt", promptText);
      form.append("provider", provider);
      try {
        const data = await api("/api/english/speak", { method: "POST", form });
        renderResult(data);
        const overall = data.feedback && data.feedback.scores && data.feedback.scores.overall;
        announce.textContent = `Your feedback is ready.${overall != null ? ` Overall score ${overall} out of 10.` : ""}`;
      } catch (error) {
        result.replaceChildren(errorBox(error, lastBlob ? submit : null, "Try this recording again"));
      } finally {
        setBusy(false);
      }
    }

    function metricCard(label, value, unit, extra) {
      return el("div", { class: "card eng-metric" },
        el("div", { class: "eng-label" }, label),
        el("div", { class: "eng-metric-value" }, el("strong", {}, String(value)), unit ? el("span", { class: "muted" }, ` ${unit}`) : null),
        extra || null);
    }

    function renderResult(data) {
      const fb = data.feedback || {};
      const scores = fb.scores || {};
      const fillerItems = (data.fillers && data.fillers.items) || {};
      const fillerCount = (data.fillers && data.fillers.count) || 0;
      const low = Array.isArray(data.low_confidence_words) ? data.low_confidence_words : [];
      const rate = String(data.rate || "");
      const rateWord = { slow: "A little slow", good: "Good pace", fast: "A little fast" }[rate] || rate;

      const fillerList = Object.entries(fillerItems).length
        ? el("ul", { class: "eng-pills" }, Object.entries(fillerItems).map(([word, count]) => el("li", {}, `${word} ×${count}`)))
        : el("p", { class: "muted eng-small" }, "None. Nice and clean.");

      const metrics = el("div", { class: "eng-metrics" },
        metricCard("Pace", data.wpm, "words/min", el("div", {},
          el("span", { class: `eng-tag ${rate}` }, rateWord),
          el("p", { class: "eng-small" }, String(data.rate_advice || "")))),
        metricCard("Filler words", fillerCount, `(${data.fillers_per_100 ?? 0} per 100 words)`, fillerList),
        metricCard("Long pauses", data.long_pauses ?? 0, "", el("p", { class: "muted eng-small" }, "Gaps longer than about a second and a half.")),
        metricCard("Total words", data.words ?? 0, "", el("p", { class: "muted eng-small" }, `${Math.round(Number(data.duration_s) || 0)} seconds of speaking.`)));

      const transcript = el("div", { class: "card eng-transcript" },
        el("h3", {}, "What I heard"),
        el("p", { class: "eng-transcript-text", lang: "en" }, markTranscript(String(data.transcript || ""), Object.keys(fillerItems), low.map((w) => String(w.word)))),
        el("p", { class: "eng-legend" },
          el("span", {}, el("mark", { class: "unclear" }, "word"), " may have been unclear"),
          el("span", {}, el("mark", { class: "filler" }, "um"), " filler word")),
        el("p", { class: "muted eng-small" }, "Highlighted words are only a hint that they may have been unclear to the speech recogniser. They are not a grade."));

      const meters = el("div", { class: "card" },
        el("h3", {}, "Your scores"),
        el("div", { class: "eng-meters" },
          meter("Fluency", scores.fluency), meter("Grammar", scores.grammar),
          meter("Vocabulary", scores.vocabulary), meter("Overall", scores.overall)));

      const out = [metrics, transcript, meters];

      if (fb.summary) out.push(el("div", { class: "card" }, el("h3", {}, "Coach summary"), el("p", {}, String(fb.summary))));

      if (Array.isArray(fb.grammar) && fb.grammar.length) {
        out.push(el("div", { class: "card" }, el("h3", {}, "Fix these"),
          el("ul", { class: "eng-list" }, fb.grammar.map((g) => el("li", {}, correctionBlock(g))))));
      }
      if (Array.isArray(fb.vocabulary) && fb.vocabulary.length) {
        out.push(el("div", { class: "card" }, el("h3", {}, "Better words"),
          el("ul", { class: "eng-list" }, fb.vocabulary.map((v) => el("li", {}, correctionBlock(v))))));
      }
      if (fb.rewrite) {
        const rewrite = String(fb.rewrite);
        out.push(el("div", { class: "card" }, el("h3", {}, "A clearer way to say it"),
          el("p", { class: "eng-rewrite", lang: "en" }, rewrite),
          el("div", { class: "row wrap" },
            el("button", { class: "btn", type: "button", onclick: async () => ctx.toast(await copyText(rewrite) ? "Copied" : "Couldn't copy. Select the text instead.") },
              icon("copy"), "Copy"),
            el("button", { class: "btn", type: "button", onclick: () => ctx.speak(rewrite) }, icon("speaker"), "Listen"))));
      }
      if (Array.isArray(fb.tips) && fb.tips.length) {
        out.push(el("div", { class: "card" }, el("h3", {}, "Tips for next time"),
          el("ul", { class: "eng-tips" }, fb.tips.map((t) => el("li", {}, String(t))))));
      }
      result.replaceChildren(...out);
      result.scrollIntoView({ block: "start" });
    }

    pane.append(
      el("div", { class: "card eng-setup" },
        el("label", { for: "eng-speak-scenario" }, "Scenario"), scenarioSelect, scenarioDesc,
        promptBtn, promptBox),
      el("div", { class: "eng-recorder" },
        recorder.button, recorder.timer, recorder.status,
        recorder.note),
      announce, result);

    return {
      node: pane,
      enter() {
        if (loaded) return;
        loaded = true;
        fillScenarioSelect(scenarioSelect, { allowFree: true, remember: "hub.english.scenario.speak" })
          .then((list) => { scenarioList = list; scenarioSelect.dispatchEvent(new Event("change")); })
          .catch((error) => { loaded = false; scenarioDesc.textContent = ""; scenarioDesc.append(friendly(error), " ",
            el("button", { class: "eng-link", type: "button", onclick: () => { scenarioDesc.textContent = ""; this.enter(); } }, "Retry")); });
      },
    };
  }

  // =========================================================================================
  // 2. ROLE-PLAY
  // =========================================================================================
  function buildRoleplay() {
    const pane = el("section", { class: "eng-pane", "aria-label": "Role-play" });
    const scenarioSelect = el("select", { id: "eng-rp-scenario", "aria-describedby": "eng-rp-desc" });
    const scenarioDesc = el("p", { class: "muted eng-small", id: "eng-rp-desc" });
    const startBtn = el("button", { class: "btn primary", type: "button" }, "Start");
    const setupError = el("div", { "aria-live": "polite" });
    const setup = el("div", { class: "card eng-setup" },
      el("label", { for: "eng-rp-scenario" }, "Pick a conversation"), scenarioSelect, scenarioDesc, startBtn, setupError);

    const log = el("div", { class: "eng-log", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
    const speakToggle = el("input", { type: "checkbox", id: "eng-rp-aloud" });
    speakToggle.checked = store.get("hub.english.aloud") === "1";
    speakToggle.addEventListener("change", () => store.set("hub.english.aloud", speakToggle.checked ? "1" : "0"));
    const resetBtn = el("button", { class: "btn", type: "button" }, "New conversation");
    const title = el("strong", { class: "grow" }, "");
    const head = el("div", { class: "eng-rp-head" },
      el("div", { class: "row" }, title, resetBtn),
      el("label", { class: "eng-check", for: "eng-rp-aloud" }, speakToggle, "Read partner replies aloud"));

    const composerInput = el("textarea", { id: "eng-rp-input", rows: "2", placeholder: "Type your answer, or tap the microphone", "aria-label": "Your answer", maxlength: "2000" });
    const sendBtn = el("button", { class: "btn primary eng-send", type: "button", "aria-label": "Send your answer" }, icon("send"));
    const micStatus = el("p", { class: "eng-small muted", role: "status", "aria-live": "polite" });
    const recorder = createRecorder(ctx, {
      big: false, name: "answer", maxSeconds: MAX_ROLEPLAY_SECONDS,
      startLabel: "Record your answer", stopLabel: "Stop recording",
      onAudio: (blob, filename) => transcribe(blob, filename),
    });
    const composer = el("div", { class: "eng-composer" },
      recorder.note,
      micStatus,
      el("div", { class: "eng-composer-row" }, recorder.button, recorder.timer, composerInput, sendBtn));
    const conversation = el("div", { class: "eng-conversation", hidden: true }, head, log, composer);

    let scenarioList = [];
    let loaded = false;
    let sessionId = "";
    let scenarioId = "";
    let streaming = false;
    let transcribing = false;
    let controller = null;
    let started = false;

    function sessionKey(id) { return `hub.english.rp.${id}`; }
    function sessionFor(id) {
      let value = store.sget(sessionKey(id));
      if (!value) { value = randomId(); store.sset(sessionKey(id), value); }
      return value;
    }
    function refresh() {
      sendBtn.disabled = streaming || transcribing;
      composerInput.disabled = streaming;
      recorder.setBusy(streaming || transcribing);
      resetBtn.disabled = false;
    }

    scenarioSelect.addEventListener("change", () => {
      store.set("hub.english.scenario.roleplay", scenarioSelect.value);
      scenarioDesc.textContent = describe(scenarioList, scenarioSelect.value);
    });

    function bubble(role, text) {
      const body = el("div", { class: "eng-bubble-text", lang: "en" }, text);
      const node = el("div", { class: `eng-msg ${role}` },
        el("div", { class: "eng-bubble" }, el("span", { class: "eng-sr" }, role === "user" ? "You: " : "Partner: "), body));
      log.append(node);
      node.scrollIntoView({ block: "nearest" });
      return { node, body };
    }

    // One turn: send `message` (or null for the opening) and stream the partner's reply.
    async function turn(message, userBubble) {
      streaming = true;
      refresh();
      const partner = bubble("partner", "");
      partner.node.classList.add("waiting");
      const wait = el("span", { class: "eng-dots", "aria-hidden": "true" }, "...");
      partner.body.append(wait);
      let checking = null;
      if (userBubble) {
        checking = el("div", { class: "eng-corr-slot muted eng-small" }, "Checking your English...");
        userBubble.node.append(checking);
      }
      controller = new AbortController();
      let reply = "";
      let failed = "";
      let gotCorrection = false;
      try {
        for await (const event of sse("/api/english/roleplay",
          { session_id: sessionId, scenario: scenarioId, message, provider }, controller.signal)) {
          if (typeof event.t === "string") {
            if (!reply) wait.remove();
            reply += event.t;
            partner.body.textContent = reply;
            partner.node.scrollIntoView({ block: "nearest" });
          } else if (event.error) {
            failed = String(event.error);
          } else if ("correction" in event && userBubble) {
            gotCorrection = true;
            showCorrection(checking, event.correction);
          }
        }
      } catch (error) {
        failed = friendly(error);
      }
      partner.node.classList.remove("waiting");
      if (failed) {
        wait.remove();
        if (!reply) partner.node.remove();
        else partner.body.append(" ");
        log.append(el("div", { class: "eng-error", role: "alert" }, el("p", {}, failed)));
        if (userBubble && message) {
          // Not answered: put the text back so nothing is lost.
          userBubble.node.remove();
          composerInput.value = message;
        } else if (!reply && !userBubble) {
          log.append(el("button", { class: "btn", type: "button", onclick: () => { log.replaceChildren(); turn(null, null); } }, "Try the opening again"));
        }
      } else if (reply && speakToggle.checked) {
        ctx.speak(reply);
      }
      if (checking && !gotCorrection) checking.remove();
      streaming = false;
      controller = null;
      refresh();
    }

    function showCorrection(slot, correction) {
      if (!slot) return;
      if (!correction) {
        slot.replaceChildren(el("span", { class: "eng-good" }, "Sounded good"));
        slot.className = "eng-corr-slot";
        return;
      }
      slot.className = "eng-corr-slot";
      slot.replaceChildren(correctionBlock(correction, true));
    }

    async function send() {
      const text = composerInput.value.trim();
      if (!text || streaming || transcribing) return;
      composerInput.value = "";
      const userBubble = bubble("user", text);
      await turn(text, userBubble);
    }
    sendBtn.addEventListener("click", send);
    composerInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); }
    });

    async function transcribe(blob, filename) {
      transcribing = true;
      refresh();
      micStatus.textContent = "Writing down what you said...";
      const form = new FormData();
      form.append("file", blob, filename);
      try {
        const data = await api("/api/transcribe", { method: "POST", form });
        const text = String((data && data.text) || "").trim();
        if (!text) {
          micStatus.textContent = "I didn't catch any words. Try again, a little closer to the microphone.";
        } else {
          composerInput.value = composerInput.value.trim() ? `${composerInput.value.trim()} ${text}` : text;
          micStatus.textContent = "Here is what I heard. Edit it if you like, then send.";
          composerInput.focus();
        }
      } catch (error) {
        micStatus.textContent = friendly(error);
      } finally {
        transcribing = false;
        refresh();
      }
    }

    async function begin() {
      scenarioId = scenarioSelect.value;
      if (!scenarioId) return;
      sessionId = sessionFor(scenarioId);
      setupError.replaceChildren();
      started = true;
      setup.hidden = true;
      conversation.hidden = false;
      title.textContent = (scenarioList.find((s) => s.id === scenarioId) || {}).title || "Conversation";
      log.replaceChildren();
      composerInput.value = "";
      micStatus.textContent = "";
      await turn(null, null);
    }
    startBtn.addEventListener("click", begin);

    resetBtn.addEventListener("click", async () => {
      if (controller) controller.abort();
      recorder.cancel();
      resetBtn.disabled = true;
      const oldId = sessionId;
      try {
        await api("/api/english/roleplay/reset", { method: "POST", json: { session_id: oldId } });
      } catch (_) { /* the server forgets old conversations by itself */ }
      store.sset(sessionKey(scenarioId), randomId());
      started = false;
      streaming = false;
      transcribing = false;
      conversation.hidden = true;
      setup.hidden = false;
      log.replaceChildren();
      refresh();
      ctx.toast("Conversation cleared. Pick one to start again.");
      scenarioSelect.focus();
    });

    pane.append(setup, conversation);
    refresh();

    return {
      node: pane,
      enter() {
        if (loaded) return;
        loaded = true;
        fillScenarioSelect(scenarioSelect, { allowFree: false, remember: "hub.english.scenario.roleplay" })
          .then((list) => { scenarioList = list; scenarioSelect.dispatchEvent(new Event("change")); })
          .catch((error) => {
            loaded = false;
            setupError.replaceChildren(errorBox(error, () => this.enter(), "Try again"));
          });
      },
      get started() { return started; },
    };
  }

  // =========================================================================================
  // 3. FIX WRITING
  // =========================================================================================
  function buildCorrect() {
    const pane = el("section", { class: "eng-pane", "aria-label": "Fix writing" });
    const input = el("textarea", { id: "eng-correct-text", rows: "6", maxlength: "5000",
      placeholder: "Paste or write an email, message or paragraph here." });
    const button = el("button", { class: "btn primary", type: "button" }, "Fix my English");
    const clear = el("button", { class: "btn", type: "button" }, "Clear");
    const result = el("div", { class: "eng-result", "aria-live": "polite" });

    async function run() {
      const text = input.value.trim();
      if (!text) { result.replaceChildren(emptyBox("Nothing to check yet", "Write or paste some text first.")); input.focus(); return; }
      button.disabled = true;
      result.setAttribute("aria-busy", "true");
      result.replaceChildren(el("div", { class: "card" }, loadingBox("Reading your text...")));
      try {
        const data = await api("/api/english/correct", { method: "POST", json: { text, provider } });
        const corrected = String(data.corrected || "");
        const changes = Array.isArray(data.changes) ? data.changes : [];
        const out = [el("div", { class: "card" }, el("h3", {}, "Corrected text"),
          el("p", { class: "eng-rewrite", lang: "en" }, corrected),
          el("div", { class: "row wrap" },
            el("button", { class: "btn", type: "button", onclick: async () => ctx.toast(await copyText(corrected) ? "Copied" : "Couldn't copy. Select the text instead.") }, icon("copy"), "Copy"),
            el("button", { class: "btn", type: "button", onclick: () => ctx.speak(corrected) }, icon("speaker"), "Listen")))];
        if (changes.length) {
          out.push(el("div", { class: "card" }, el("h3", {}, "What changed"),
            el("ul", { class: "eng-list" }, changes.map((c) => el("li", {}, correctionBlock(c))))));
        } else {
          out.push(el("div", { class: "card" }, el("h3", {}, "What changed"), el("p", { class: "muted" }, "Nothing needed changing. Well written.")));
        }
        if (data.tone_note) out.push(el("div", { class: "card" }, el("h3", {}, "Tone"), el("p", {}, String(data.tone_note))));
        result.replaceChildren(...out);
      } catch (error) {
        result.replaceChildren(errorBox(error, run));
      } finally {
        button.disabled = false;
        result.setAttribute("aria-busy", "false");
      }
    }
    button.addEventListener("click", run);
    clear.addEventListener("click", () => { input.value = ""; result.replaceChildren(); input.focus(); });

    pane.append(
      el("div", { class: "card eng-setup" },
        el("label", { for: "eng-correct-text" }, "Your text"), input,
        el("div", { class: "row wrap" }, button, clear)),
      result);
    return { node: pane };
  }

  // =========================================================================================
  // 4. WORDS
  // =========================================================================================
  function buildVocab() {
    const pane = el("section", { class: "eng-pane", "aria-label": "Words" });
    const today = el("div", { class: "eng-today" });
    const reviewBox = el("div", { class: "eng-review" });
    const addInput = el("input", { id: "eng-add-word", type: "text", maxlength: "60", placeholder: "A word or phrase, like \"touch base\"", autocomplete: "off", autocapitalize: "none", "aria-label": "Word or phrase to add" });
    const addBtn = el("button", { class: "btn primary", type: "submit" }, "Add");
    const addForm = el("form", { class: "card eng-setup" },
      el("h3", {}, "Add a word"),
      el("p", { class: "muted eng-small" }, "Type a word or phrase you met today. I'll explain it and add it to your review."),
      el("div", { class: "row" }, el("div", { class: "grow" }, addInput), addBtn));
    const added = el("div", { class: "eng-added", "aria-live": "polite" });
    let data = null;
    let loading = false;
    let loadedOnce = false;

    async function load() {
      if (loading) return;
      loading = true;
      today.replaceChildren(el("div", { class: "card" }, loadingBox("Getting today's words..."),
        el("p", { class: "muted eng-small" }, "The first time each day this can take a little while.")));
      reviewBox.replaceChildren();
      try {
        data = await api(`/api/english/vocab/today?provider=${provider}`);
        loadedOnce = true;
        renderToday();
        renderReviewStart();
      } catch (error) {
        data = null;
        today.replaceChildren(errorBox(error, load));
      } finally {
        loading = false;
      }
    }

    function renderToday() {
      const words = data.new_words || [];
      today.replaceChildren(el("h3", {}, "New words today"));
      if (!words.length) today.append(emptyBox("No new words today", "Add a word of your own below."));
      else today.append(...words.map((w) => wordCard(w, ctx)));
    }

    function renderReviewStart() {
      const due = data.due || [];
      reviewBox.replaceChildren(el("h3", {}, "Review"));
      if (!due.length) {
        reviewBox.append(el("div", { class: "card" },
          el("p", {}, "Nothing is due for review right now."),
          el("p", { class: "muted eng-small" }, "Come back tomorrow, or after you have added more words.")));
        return;
      }
      reviewBox.append(el("div", { class: "card" },
        el("p", {}, `${due.length} ${due.length === 1 ? "word is" : "words are"} ready to review.`),
        el("button", { class: "btn primary", type: "button", onclick: () => startReview(due) }, "Start review")));
    }

    // Flashcards: word -> Show meaning -> Again / Hard / Good / Easy.
    function startReview(due) {
      const queue = due.slice();
      let index = 0;
      const total = queue.length;
      const stage = el("div", { class: "card eng-flash" });
      reviewBox.replaceChildren(el("h3", {}, "Review"), stage);

      function renderCard() {
        if (index >= total) {
          stage.replaceChildren(
            el("h4", {}, "All done"),
            el("p", {}, `You reviewed ${total} ${total === 1 ? "word" : "words"}. Nicely done.`),
            el("button", { class: "btn primary", type: "button", onclick: () => {
              data.due = [];
              renderReviewStart();
            } }, "Finish"));
          return;
        }
        const w = queue[index];
        const meaning = el("div", { class: "eng-flash-answer", hidden: true, tabindex: "-1" },
          el("p", { class: "eng-word-meaning" }, String(w.meaning || "")),
          w.example ? el("p", { class: "eng-word-example" }, String(w.example)) : null);
        const feedback = el("p", { class: "eng-small eng-good", role: "status", "aria-live": "polite" });
        const showBtn = el("button", { class: "btn primary", type: "button" }, "Show meaning");
        const ratings = el("div", { class: "eng-ratings", hidden: true, role: "group", "aria-label": "How well did you know it?" });
        const buttons = [];
        for (const [value, label] of [["again", "Again"], ["hard", "Hard"], ["good", "Good"], ["easy", "Easy"]]) {
          const b = el("button", { class: `btn eng-rate ${value}`, type: "button", onclick: () => rate(value) }, label);
          buttons.push(b);
          ratings.append(b);
        }
        async function rate(value) {
          buttons.forEach((b) => { b.disabled = true; });
          feedback.className = "eng-small muted";
          feedback.textContent = "Saving...";
          try {
            const res = await api(`/api/english/vocab/${encodeURIComponent(w.id)}/review`, { method: "POST", json: { rating: value } });
            feedback.className = "eng-small eng-good";
            feedback.textContent = `Saved. You'll see it again ${friendlyDue(res.next_due)}.`;
            setTimeout(() => { index += 1; renderCard(); }, 1300);
          } catch (error) {
            buttons.forEach((b) => { b.disabled = false; });
            feedback.className = "eng-small error";
            feedback.textContent = friendly(error);
          }
        }
        showBtn.addEventListener("click", () => {
          showBtn.hidden = true;
          meaning.hidden = false;
          ratings.hidden = false;
          meaning.focus();
        });
        stage.replaceChildren(
          el("div", { class: "eng-flash-top" },
            el("span", { class: "muted eng-small" }, `Word ${index + 1} of ${total}`),
            el("div", { class: "eng-progress", role: "progressbar", "aria-label": "Review progress",
              "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(index) },
              el("span", { style: `width:${(index / total) * 100}%` }))),
          el("div", { class: "eng-flash-word" },
            el("h4", {}, String(w.word)),
            w.level ? el("span", { class: "eng-badge" }, sr("Level "), String(w.level)) : null,
            el("button", { class: "eng-iconbtn", type: "button", "aria-label": `Listen to the word ${w.word}`, onclick: () => ctx.speak(String(w.word)) }, icon("speaker"))),
          showBtn, meaning, ratings, feedback);
        showBtn.focus();
      }
      renderCard();
    }

    addForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const word = addInput.value.trim();
      if (!word) { addInput.focus(); return; }
      addBtn.disabled = true;
      added.replaceChildren(loadingBox(`Looking up "${word}"...`));
      try {
        const entry = await api("/api/english/vocab/add", { method: "POST", json: { word, provider } });
        addInput.value = "";
        added.replaceChildren(el("div", { class: "eng-label" }, "Added to your words"), wordCard(entry, ctx));
      } catch (error) {
        added.replaceChildren(errorBox(error));
      } finally {
        addBtn.disabled = false;
      }
    });

    pane.append(today, reviewBox, addForm, added);
    return {
      node: pane,
      enter() { if (!loadedOnce) load(); },
    };
  }

  // =========================================================================================
  // 5. PROGRESS
  // =========================================================================================
  function buildProgress() {
    const pane = el("section", { class: "eng-pane", "aria-label": "Progress" });
    const body = el("div", { class: "eng-progress-body", "aria-live": "polite" });
    pane.append(body);
    let loading = false;

    function stat(label, value, hint) {
      return el("div", { class: "card eng-stat" },
        el("div", { class: "eng-label" }, label),
        el("strong", { class: "eng-stat-value" }, String(value)),
        hint ? el("span", { class: "muted eng-small" }, hint) : null);
    }

    async function load() {
      if (loading) return;
      loading = true;
      body.setAttribute("aria-busy", "true");
      if (!body.children.length) body.replaceChildren(el("div", { class: "card" }, loadingBox("Loading your progress...")));
      try {
        const [data, scenarios] = await Promise.all([api("/api/english/overview"), loadScenarios().catch(() => [])]);
        render(data, scenarios);
      } catch (error) {
        body.replaceChildren(errorBox(error, load));
      } finally {
        loading = false;
        body.setAttribute("aria-busy", "false");
      }
    }

    function render(data, scenarios) {
      const streak = data.streak_days || 0;
      const out = [el("div", { class: "eng-stats" },
        stat("Streak", streak, streak === 1 ? "day in a row" : "days in a row"),
        stat("Sessions", data.sessions_total || 0, "speaking practices"),
        stat("Words due", data.vocab_due || 0, "to review"))];

      const last = data.last_session;
      if (!last) {
        out.push(el("div", { class: "card eng-empty" },
          el("strong", {}, "No speaking sessions yet"),
          el("p", { class: "muted" }, "Record your first practice and your trends will show up here."),
          el("button", { class: "btn primary", type: "button", onclick: () => select("speak", { focus: false }) }, "Go to Speak")));
        body.replaceChildren(...out);
        return;
      }
      const sc = scenarios.find((s) => s.id === last.scenario);
      const scores = last.scores || {};
      out.push(el("div", { class: "card" },
        el("h3", {}, "Last session"),
        el("p", { class: "muted eng-small" }, [shortDate(last.date), sc ? sc.title : last.scenario ? last.scenario : "Free talk"].filter(Boolean).join(" - ")),
        last.summary ? el("p", {}, String(last.summary)) : null,
        el("div", { class: "eng-meters" },
          meter("Fluency", scores.fluency), meter("Grammar", scores.grammar),
          meter("Vocabulary", scores.vocabulary), meter("Overall", scores.overall)),
        el("p", { class: "muted eng-small" }, `${last.wpm ?? "-"} words per minute, ${last.fillers_per_100 ?? "-"} fillers per 100 words.`)));

      const trend = Array.isArray(data.trend) ? data.trend : [];
      out.push(el("h3", { class: "eng-section-title" }, "Trends"));
      if (!trend.length) {
        out.push(emptyBox("No trend yet", "Do a speaking session to see how you are improving."));
      } else {
        const dates = trend.map((t) => t.date);
        const num = (v) => (v == null || Number.isNaN(Number(v)) ? NaN : Number(v));
        const wpm = trend.map((t) => num(t.wpm));
        const fill = trend.map((t) => num(t.fillers_per_100));
        const overall = trend.map((t) => num(t.overall));
        const maxWpm = niceMax(Math.max(...wpm.filter(Number.isFinite), 120), 40);
        const maxFill = niceMax(Math.max(...fill.filter(Number.isFinite), 5), 5);
        const mid = (m) => [0, m / 2, m];
        if (trend.length < 2) out.push(el("p", { class: "muted eng-small" }, "One session so far. Do another to see a line."));
        out.push(
          lineChart({ title: "Pace", caption: "words per minute (about 110 to 160 is comfortable)", unit: "words/min", values: wpm, dates, max: maxWpm, ticks: mid(maxWpm), format: (v) => String(Math.round(v)), tone: "pace" }),
          lineChart({ title: "Filler words", caption: "per 100 words (lower is better)", unit: "fillers per 100 words", values: fill, dates, max: maxFill, ticks: mid(maxFill), format: (v, axis) => (axis ? String(Math.round(v * 10) / 10) : String(Math.round(v * 10) / 10)), tone: "fillers" }),
          lineChart({ title: "Overall score", caption: "out of 10 (higher is better)", unit: "score out of 10", values: overall, dates, max: 10, ticks: [0, 5, 10], format: (v) => String(Math.round(v * 10) / 10), tone: "score" }));
        out.push(el("details", { class: "card eng-table" },
          el("summary", {}, "Show the numbers"),
          el("div", { class: "eng-table-wrap" }, el("table", {},
            el("thead", {}, el("tr", {}, ["Date", "Words/min", "Fillers/100", "Overall"].map((h) => el("th", { scope: "col" }, h)))),
            el("tbody", {}, trend.map((t) => el("tr", {},
              el("td", {}, shortDate(t.date)), el("td", {}, t.wpm ?? "-"), el("td", {}, t.fillers_per_100 ?? "-"), el("td", {}, t.overall ?? "-"))))))));
      }
      body.replaceChildren(...out);
    }

    return {
      node: pane,
      enter() { load(); },
    };
  }

  // ----- assemble
  parts.speak = buildSpeak();
  parts.roleplay = buildRoleplay();
  parts.correct = buildCorrect();
  parts.vocab = buildVocab();
  parts.progress = buildProgress();
  for (const s of SECTIONS) {
    panes[s.id] = parts[s.id].node;
    panes[s.id].hidden = true;
    root.append(panes[s.id]);
  }

  let visible = false;
  const savedSection = store.get("hub.english.section", "speak");
  current = null;
  const first = SECTIONS.some((s) => s.id === savedSection) ? savedSection : "speak";
  // Mark the saved tab now; its content loads when the section is first shown.
  for (const s of SECTIONS) {
    const tab = nav.querySelector(`[data-part="${s.id}"]`);
    tab.setAttribute("aria-selected", String(s.id === first));
    tab.tabIndex = s.id === first ? 0 : -1;
  }
  panes[first].hidden = false;
  current = first;

  return {
    show() {
      visible = true;
      parts[current]?.enter?.();
    },
    hide() {
      visible = false;
      for (const r of recorders) r.cancel();
    },
    openSection(name) {
      if (SECTIONS.some((s) => s.id === name)) select(name);
    },
  };
}
