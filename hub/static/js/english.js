// English coach: spoken-English fluency practice. Lives inside the Learn tab as its third section.
// Five parts: Speak (record and get feedback), Role-play, Fix writing, Words, Progress.
// Everything that comes from the transcriber or a model is put on the page with textContent only.
import { api, sse, el } from "./api.js";
import * as ui from "./ui.js";

const MAX_SPEAK_SECONDS = 180;
const MAX_ROLEPLAY_SECONDS = 90;
const SECTIONS = [
  { id: "speak", label: "Speak" },
  { id: "roleplay", label: "Role-play" },
  { id: "correct", label: "Fix writing" },
  { id: "vocab", label: "Words" },
  { id: "progress", label: "Progress" },
];
const SCENARIO_ICONS = ["users", "coffee", "phone", "calendar", "mail", "globe", "star", "heart"];
const SCENARIO_TONES = ["accent", "teal", "violet", "orange", "pink", "info"];
const WAVE_BARS = 34;

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
const icon = (name, size) => ui.icon(name, size ? { size } : {});

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
  return el("div", { class: "eng-loading", role: "status" }, el("span", { class: "ui-spinner", "aria-hidden": "true" }), el("span", {}, text));
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
  const box = el("div", { class: "eng-error", role: "alert" }, ui.notice(friendly(error), { tone: "bad" }));
  if (retry) box.append(ui.button({ label: retryLabel, icon: "refresh", variant: "tonal", size: "sm", onClick: retry }));
  return box;
}

function emptyBox(title, text, ic = "sparkles", action) {
  return el("div", { class: "ui-card ui-card--flat" }, ui.emptyState({ icon: ic, title, body: text, action, compact: true }));
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

// ---------- shared pieces: correction cards, score rings, flip cards ----------

// Before/after card. Compact is the gentle one-line version shown under a role-play message.
function correctionBlock(item, compact = false) {
  const said = String(item.said || item.from || "");
  const better = String(item.better || item.to || "");
  if (compact) {
    const block = el("div", { class: "eng-corr compact" },
      el("span", { class: "eng-corr-ico", "aria-hidden": "true" }, icon("sparkles", 16)),
      el("div", { class: "eng-corr-body" },
        el("div", { class: "eng-corr-line" },
          sr("A gentler way to say it: "), el("span", { class: "better" }, better),
          said ? el("span", { class: "said" }, sr(" You said: "), said) : null),
        item.why ? el("div", { class: "why" }, String(item.why)) : null));
    return block;
  }
  const block = el("div", { class: "eng-corr" },
    el("div", { class: "eng-corr-pair" },
      el("div", { class: "eng-corr-card before" }, el("span", { class: "eng-corr-tag" }, "You said"), el("span", { class: "said" }, said)),
      el("span", { class: "eng-corr-arrow", "aria-hidden": "true" }, icon("arrow-down", 18)),
      el("div", { class: "eng-corr-card after" }, el("span", { class: "eng-corr-tag" }, "Better"), el("span", { class: "better" }, better))));
  if (item.why) block.append(el("div", { class: "why" }, String(item.why)));
  return block;
}

function toneFor(fraction) {
  if (fraction >= 0.7) return "good";
  if (fraction >= 0.5) return "accent";
  return "warn";
}

// A ring showing a 1-10 score. Returns a labelled block.
function scoreRing(label, value, { size = 64, stroke = 7, max = 10 } = {}) {
  const n = Number(value);
  const ok = Number.isFinite(n);
  const ring = ui.ringProgress({ value: ok ? n / max : 0, size, stroke, tone: ok ? toneFor(n / max) : "gray",
    big: ok ? String(Math.round(n * 10) / 10) : "-", label: `${label} score` });
  ring.setAttribute("aria-label", ok ? `${label}: ${n} out of ${max}` : `${label}: no score`);
  return el("div", { class: "eng-score" }, ring, el("span", { class: "eng-score-label" }, label));
}

// Word card that flips on tap: word on the front, meaning and example on the back.
function flipCard(w, ctx, { tall = false, onFlip } = {}) {
  const wrap = el("div", { class: "eng-flip" + (tall ? " eng-flip--tall" : "") });
  const front = el("span", { class: "eng-face eng-face--front" },
    w.level ? el("span", { class: "ui-badge ui-badge--accent eng-level" }, sr("Level "), String(w.level)) : null,
    el("span", { class: "eng-flip-word" }, String(w.word)),
    el("span", { class: "eng-flip-hint" }, icon("refresh", 14), "Tap to see the meaning"));
  const back = el("span", { class: "eng-face eng-face--back", "aria-hidden": "true" },
    el("span", { class: "eng-flip-word eng-flip-word--sm" }, String(w.word)),
    el("span", { class: "eng-word-meaning" }, String(w.meaning || "")),
    w.example ? el("span", { class: "eng-word-example" }, String(w.example)) : null);
  const inner = el("button", { class: "eng-flip__inner", type: "button", "aria-pressed": "false",
    "aria-label": `${w.word}. Show the meaning` }, front, back);
  function setFlipped(flipped, user) {
    wrap.classList.toggle("is-flipped", flipped);
    inner.setAttribute("aria-pressed", String(flipped));
    inner.setAttribute("aria-label", flipped ? `${w.word}. Meaning shown. Tap to flip back` : `${w.word}. Show the meaning`);
    front.setAttribute("aria-hidden", String(flipped));
    back.setAttribute("aria-hidden", String(!flipped));
    if (user) ui.haptic("select");
    if (onFlip) onFlip(flipped);
  }
  inner.addEventListener("click", () => setFlipped(!wrap.classList.contains("is-flipped"), true));
  const listen = el("button", { class: "ui-btn ui-btn--icon ui-btn--ghost eng-flip-listen", type: "button",
    "aria-label": `Listen to the word ${w.word}`, onclick: () => ctx.speak(`${w.word}. ${w.example || ""}`) }, icon("volume"));
  wrap.append(inner, listen);
  wrap.flip = (v) => setFlipped(v, false);
  return wrap;
}

// ---------- recorder: tap to start, tap to stop, with a live level and waveform ----------

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
  const micIcon = el("span", { class: "eng-mic-ico" }, icon("mic"));
  button.append(micIcon);
  const timer = el("span", { class: "eng-timer ui-num", "aria-hidden": "true" }, "0:00");
  const status = el("p", { class: "eng-mic-status", role: "status", "aria-live": "polite" }, idleText);
  const note = el("div", { class: "eng-note", role: "status", hidden: !problem });
  if (problem) note.append(ui.notice(problem, { tone: "warn", icon: "mic-off" }));
  if (problem) button.disabled = true;

  // live waveform: a scrolling strip of recent loudness values
  const bars = [];
  const wave = el("div", { class: "eng-wave", "aria-hidden": "true" });
  for (let i = 0; i < WAVE_BARS; i += 1) { const b = el("span", { class: "eng-wave-bar" }); bars.push(b); wave.append(b); }
  const levels = new Array(WAVE_BARS).fill(0);

  const rec = { recorder: null, stream: null, chunks: [], started: 0, tick: null, limit: null, cancelled: false, busy: false,
    audio: null, raf: 0 };

  function paintLevels(current) {
    bars.forEach((b, i) => b.style.setProperty("--l", (0.1 + levels[i] * 0.9).toFixed(2)));
    button.style.setProperty("--level", current.toFixed(2));
  }
  function startMeter(stream) {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const audio = new AC();
      const source = audio.createMediaStreamSource(stream);
      const analyser = audio.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      rec.audio = audio;
      const buf = new Uint8Array(analyser.fftSize);
      let last = 0;
      const loop = (t) => {
        rec.raf = requestAnimationFrame(loop);
        if (t - last < 70) return;
        last = t;
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i += 1) { const d = (buf[i] - 128) / 128; sum += d * d; }
        const level = Math.min(1, Math.sqrt(sum / buf.length) * 4.5);
        levels.push(level); levels.shift();
        paintLevels(level);
      };
      rec.raf = requestAnimationFrame(loop);
    } catch (_) { /* the level display is optional */ }
  }
  function stopMeter() {
    cancelAnimationFrame(rec.raf);
    if (rec.audio) { rec.audio.close().catch(() => {}); rec.audio = null; }
    levels.fill(0);
    paintLevels(0);
  }

  function setIcon(recording) {
    micIcon.replaceChildren(icon(recording ? "stop" : "mic"));
    button.classList.toggle("recording", recording);
    wave.classList.toggle("on", recording);
    button.setAttribute("aria-label", recording ? stopLabel : startLabel);
    timer.classList.toggle("on", recording);
    button.setAttribute("aria-pressed", recording ? "true" : "false");
  }
  function showProblem(message) {
    note.replaceChildren();
    if (message) note.append(ui.notice(message, { tone: "warn", icon: "mic-off" }));
    note.hidden = !message;
  }
  function release() {
    if (rec.stream) rec.stream.getTracks().forEach((t) => { try { t.stop(); } catch (_) { /* ignore */ } });
    rec.stream = null;
    clearInterval(rec.tick); clearTimeout(rec.limit);
    rec.tick = null; rec.limit = null;
    stopMeter();
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
    ui.haptic("medium");
    startMeter(stream);
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
    ui.haptic("light");
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

  // The big recorder is a stage: waveform, button inside a pulsing ring, timer, status.
  const stage = el("div", { class: "eng-stage" },
    wave,
    el("div", { class: "eng-mic-wrap" }, el("span", { class: "eng-mic-halo", "aria-hidden": "true" }), button),
    timer, status, note);

  const api2 = {
    button, timer, status, note, stage, problem,
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


// ---------- word diff (Fix writing) ----------

// Returns [{t:"eq"|"del"|"add", w}] turning the words of `a` into the words of `b`.
function diffWords(a, b) {
  const x = a.split(/\s+/).filter(Boolean);
  const y = b.split(/\s+/).filter(Boolean);
  const n = x.length, m = y.length;
  if (n * m > 600000) return [...x.map((w) => ({ t: "del", w })), ...y.map((w) => ({ t: "add", w }))];
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { ops.push({ t: "eq", w: x[i] }); i += 1; j += 1; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { ops.push({ t: "del", w: x[i] }); i += 1; }
    else { ops.push({ t: "add", w: y[j] }); j += 1; }
  }
  while (i < n) { ops.push({ t: "del", w: x[i] }); i += 1; }
  while (j < m) { ops.push({ t: "add", w: y[j] }); j += 1; }
  return ops;
}

function diffNodes(ops) {
  const out = [];
  ops.forEach((op, i) => {
    if (i) out.push(" ");
    if (op.t === "eq") out.push(op.w);
    else if (op.t === "del") out.push(el("del", { class: "eng-del" }, sr("removed: "), op.w));
    else out.push(el("ins", { class: "eng-ins" }, sr("added: "), op.w));
  });
  return out;
}

// ---------- charts ----------

function niceMax(value, step) { return Math.max(step, Math.ceil(value / step) * step); }

function lineChart({ title, unit, values, dates, max, ticks, format, tone }) {
  const W = 320, H = 180, L = 36, R = 14, T = 18, B = 34;
  const plotW = W - L - R, plotH = H - T - B;
  const n = values.length;
  const xAt = (i) => (n === 1 ? L + plotW / 2 : L + (plotW * i) / (n - 1));
  const yAt = (v) => T + plotH - (Math.max(0, Math.min(max, v)) / max) * plotH;
  const present = values.map((v, i) => ({ v, i })).filter((p) => Number.isFinite(p.v));
  const first = present[0], last = present[present.length - 1];
  const summary = present.length
    ? `${title}. ${present.length} sessions. First ${format(first.v)}, latest ${format(last.v)}.`
    : `${title}. No data yet.`;
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: `eng-chart ${tone}`, role: "img", "aria-label": summary, preserveAspectRatio: "xMidYMid meet" });
  for (const t of ticks) {
    const y = yAt(t);
    chart.append(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid" }));
    chart.append(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "tick" }, format(t, true)));
  }
  let d = "";
  let pen = false;
  let area = "";
  let runStart = -1;
  values.forEach((v, i) => {
    if (!Number.isFinite(v)) {
      if (runStart >= 0) area += `L${xAt(i - 1).toFixed(1)} ${T + plotH} L${xAt(runStart).toFixed(1)} ${T + plotH}Z `;
      runStart = -1; pen = false; return;
    }
    d += `${pen ? "L" : "M"}${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)} `;
    if (runStart < 0) { runStart = i; area += `M${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)} `; } else area += `L${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)} `;
    pen = true;
  });
  if (runStart >= 0) area += `L${xAt(n - 1).toFixed(1)} ${T + plotH} L${xAt(runStart).toFixed(1)} ${T + plotH}Z`;
  if (d && present.length > 1) chart.append(svg("path", { d: area, class: "area" }));
  if (d) chart.append(svg("path", { d, class: "line", fill: "none" }));
  values.forEach((v, i) => {
    if (Number.isFinite(v)) chart.append(svg("circle", { cx: xAt(i), cy: yAt(v), r: i === (last && last.i) ? 4.5 : 3, class: "dot" }));
  });
  if (last) {
    const lx = xAt(last.i);
    const anchor = lx > W - 50 ? "end" : "middle";
    chart.append(svg("text", { x: anchor === "end" ? lx + 4 : lx, y: yAt(last.v) - 10, "text-anchor": anchor, class: "val" }, format(last.v)));
  }
  if (n > 0) {
    chart.append(svg("text", { x: xAt(0), y: H - 10, "text-anchor": n === 1 ? "middle" : "start", class: "tick" }, `#1 ${shortDate(dates[0])}`.trim()));
    if (n > 1) chart.append(svg("text", { x: xAt(n - 1), y: H - 10, "text-anchor": "end", class: "tick" }, `#${n} ${shortDate(dates[n - 1])}`.trim()));
  }
  if (!present.length) chart.append(svg("text", { x: L + plotW / 2, y: T + plotH / 2, "text-anchor": "middle", class: "tick" }, "No data for this yet"));
  chart.append(svg("title", {}, `${title} (${unit})`));
  return chart;
}

// ---------- the module ----------

export function init(container, ctx) {
  const root = el("div", { class: "english-root" });
  container.append(root);
  if (!document.querySelector('link[href$="english.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/english.css", "data-eng-css": "1" }));
  }

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

  // Scenario picker: chips (Speak) or cards (Role-play). `value` is the scenario id ("" is free talk).
  function createPicker({ allowFree, remember, mode, label, onChange }) {
    const node = el("div", { class: `eng-picker eng-picker--${mode}` + (mode === "chips" ? " ui-hscroll" : ""), role: "radiogroup", "aria-label": label });
    const desc = el("p", { class: "eng-picker-desc" });
    let list = [];
    let value = "";
    const items = () => (allowFree ? [{ id: "", title: "Free talk", description: "Talk about anything you like." }] : []).concat(list);
    function render(refocus) {
      node.replaceChildren();
      items().forEach((s, i) => {
        const on = s.id === value;
        const ic = s.id === "" ? "waveform" : SCENARIO_ICONS[i % SCENARIO_ICONS.length];
        const tone = s.id === "" ? "gray" : SCENARIO_TONES[i % SCENARIO_TONES.length];
        const common = { type: "button", role: "radio", "aria-checked": String(on), onclick: () => set(s.id, true) };
        node.append(mode === "chips"
          ? el("button", { ...common, class: "ui-chip" + (on ? " is-on" : "") }, icon(ic), s.title)
          : el("button", { ...common, class: "eng-scn" + (on ? " is-on" : "") },
            ui.tile(ic, tone),
            el("span", { class: "eng-scn-body" }, el("strong", {}, s.title), el("span", { class: "eng-scn-desc" }, s.description || "")),
            el("span", { class: "eng-scn-check", "aria-hidden": "true" }, on ? icon("check-circle") : null)));
      });
      const found = items().find((s) => s.id === value);
      desc.textContent = found ? found.description || "" : "";
      desc.hidden = mode !== "chips";
      if (refocus) node.querySelector('[aria-checked="true"]')?.focus();
    }
    function set(id, user) {
      value = id;
      store.set(remember, id);
      render(user);
      if (onChange) onChange(id);
    }
    function load() {
      node.replaceChildren(ui.skeleton(mode === "chips" ? "tiles" : "list", mode === "chips" ? 3 : 3));
      return loadScenarios().then((l) => {
        list = l;
        const saved = store.get(remember, null);
        if (saved != null && items().some((s) => s.id === saved)) value = saved;
        else value = items()[0] ? items()[0].id : "";
        render(false);
        if (onChange) onChange(value);
        return l;
      }).catch((error) => {
        node.replaceChildren(errorBox(error, () => load()));
        throw error;
      });
    }
    return { node, desc, load, get value() { return value; }, get list() { return list; } };
  }

  // ----- top: model switch and section tabs
  const providerSeg = ui.segmented({
    options: [{ value: "local", label: "Local", icon: "laptop" }, { value: "claude", label: "Claude", icon: "sparkles" }],
    value: provider, label: "Which model gives the feedback",
    onChange: (v) => { provider = v; store.set("hub.provider.english", v); },
  });
  const providerBar = el("div", { class: "eng-provider" },
    el("span", { class: "eng-provider-label" }, "Feedback from"), providerSeg);

  const nav = el("div", { class: "ui-tabs eng-tabs", role: "tablist", "aria-label": "English practice" });
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
    const tab = nav.querySelector(`[data-part="${id}"]`);
    if (tab && tab.scrollIntoView) tab.scrollIntoView({ inline: "center", block: "nearest" });
    parts[id].enter?.();
    if (focus) tab.focus();
  }
  for (const s of SECTIONS) {
    nav.append(el("button", { type: "button", role: "tab", "data-part": s.id, onclick: () => { ui.haptic("select"); select(s.id); } }, s.label));
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
    const picker = createPicker({
      allowFree: true, remember: "hub.english.scenario.speak", mode: "chips", label: "Scenario",
      onChange: (id) => { if (id !== lastScenario) { lastScenario = id; } },
    });
    let lastScenario = null;
    const promptBtn = ui.button({ label: "Give me a topic", icon: "sparkles", variant: "tonal", size: "sm" });
    const promptBody = el("div", { class: "eng-prompt-body", "aria-live": "polite" },
      el("p", { class: "eng-prompt-empty" }, "Not sure what to say? Get a topic to talk about."));
    const promptCard = el("div", { class: "ui-card eng-prompt" },
      el("div", { class: "eng-prompt-head" },
        el("span", { class: "ui-caps" }, "Your topic"), promptBtn),
      promptBody);
    const result = el("div", { class: "eng-result" });
    const announce = el("div", { class: "eng-sr", role: "status", "aria-live": "polite" });
    let promptText = "";
    let loaded = false;
    let lastBlob = null;
    let busy = false;

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

    promptBtn.addEventListener("click", async () => {
      promptText = "";
      promptBody.replaceChildren(ui.skeleton("lines", 2));
      try {
        const data = await ui.busy(promptBtn, api("/api/english/practice-prompt", {
          method: "POST", json: { scenario: picker.value, provider } }));
        promptText = String(data.prompt || "");
        promptBody.replaceChildren(
          el("p", { class: "eng-prompt-text" }, promptText),
          data.source === "builtin"
            ? el("p", { class: "eng-small muted" }, "A built-in prompt, because the model wasn't available just now.")
            : null);
        promptBtn.lastChild.textContent = "New topic";
      } catch (error) {
        promptBody.replaceChildren(errorBox(error));
      }
    });

    function analysing() {
      const steps = ["Listening to what you said", "Counting pace and filler words", "Writing your feedback"];
      const status = el("span", {}, steps[0]);
      const card = el("div", { class: "ui-card eng-analysing" },
        el("div", { class: "eng-analysing-head" }, el("span", { class: "ui-spinner", "aria-hidden": "true" }), status),
        ui.progressBar(null, "Analysing your recording"),
        ui.skeleton("lines", 3),
        el("p", { class: "eng-small muted" }, "This can take up to a minute on a laptop without a graphics card."));
      let i = 0;
      const timer = setInterval(() => {
        if (!card.isConnected) { clearInterval(timer); return; }
        i = Math.min(steps.length - 1, i + 1);
        status.textContent = steps[i];
      }, 6000);
      return card;
    }

    async function submit() {
      if (!lastBlob || busy) return;
      setBusy(true);
      announce.textContent = "";
      result.replaceChildren(analysing());
      result.scrollIntoView({ block: "nearest" });
      const form = new FormData();
      form.append("file", lastBlob.blob, lastBlob.filename);
      form.append("scenario", picker.value);
      form.append("prompt", promptText);
      form.append("provider", provider);
      try {
        const data = await api("/api/english/speak", { method: "POST", form });
        renderResult(data);
        ui.haptic("success");
        const overall = data.feedback && data.feedback.scores && data.feedback.scores.overall;
        announce.textContent = `Your feedback is ready.${overall != null ? ` Overall score ${overall} out of 10.` : ""}`;
      } catch (error) {
        result.replaceChildren(errorBox(error, lastBlob ? submit : null, "Try this recording again"));
      } finally {
        setBusy(false);
      }
    }

    function section(title, ic, tone, ...body) {
      return el("div", { class: "ui-card eng-block" },
        el("div", { class: "eng-block-head" }, ui.tile(ic, tone, "sm"), el("h3", {}, title)), ...body);
    }

    function renderResult(data) {
      const fb = data.feedback || {};
      const scores = fb.scores || {};
      const fillerItems = (data.fillers && data.fillers.items) || {};
      const fillerCount = (data.fillers && data.fillers.count) || 0;
      const low = Array.isArray(data.low_confidence_words) ? data.low_confidence_words : [];
      const rate = String(data.rate || "");
      const rateWord = { slow: "A little slow", good: "Good pace", fast: "A little fast" }[rate] || rate;
      const wpm = Number(data.wpm) || 0;

      // hero: overall score ring plus the three sub-scores
      const overallNum = Number(scores.overall);
      const hero = el("div", { class: "ui-card ui-card--elevated eng-hero" },
        el("div", { class: "eng-hero-top" },
          scoreRing("Overall", scores.overall, { size: 104, stroke: 9 }),
          el("div", { class: "eng-hero-text" },
            el("span", { class: "ui-caps" }, "Your result"),
            el("h3", {}, Number.isFinite(overallNum) ? (overallNum >= 8 ? "Excellent speaking" : overallNum >= 6 ? "Good job" : "Keep practising") : "Session saved"),
            fb.summary ? el("p", { class: "eng-summary" }, String(fb.summary)) : null)),
        el("div", { class: "eng-subscores" },
          scoreRing("Fluency", scores.fluency, { size: 56, stroke: 6 }),
          scoreRing("Grammar", scores.grammar, { size: 56, stroke: 6 }),
          scoreRing("Vocabulary", scores.vocabulary, { size: 56, stroke: 6 })));

      // pace gauge
      const pace = el("div", { class: "ui-card eng-pace" },
        ui.ringProgress({ value: Math.min(1, wpm / 200), size: 104, stroke: 9, tone: rate === "good" ? "good" : "warn",
          big: String(Math.round(wpm)), small: "words/min", label: "Speaking pace" }),
        el("div", { class: "eng-pace-text" },
          el("span", { class: "ui-caps" }, "Pace"),
          el("span", { class: `ui-badge ui-badge--${rate === "good" ? "good" : "warn"}` }, rateWord),
          el("p", { class: "eng-small" }, String(data.rate_advice || "")),
          el("p", { class: "eng-small muted" }, "A comfortable pace is about 110 to 160.")));

      // filler words
      const fillerEntries = Object.entries(fillerItems);
      const fillers = el("div", { class: "ui-card eng-fillers" },
        el("div", { class: "eng-fillers-head" },
          el("div", {}, el("span", { class: "ui-caps" }, "Filler words"),
            el("div", { class: "eng-bignum" }, el("strong", { class: "ui-num" }, String(fillerCount)),
              el("span", { class: "muted" }, ` ${data.fillers_per_100 ?? 0} per 100 words`))),
          ui.tile("waveform", fillerCount ? "warn" : "good")),
        fillerEntries.length
          ? el("ul", { class: "eng-chips" }, fillerEntries.map(([word, count]) =>
            el("li", { class: "ui-chip ui-chip--static eng-filler-chip" }, word, el("span", { class: "eng-count ui-num" }, `×${count}`))))
          : el("p", { class: "eng-small eng-good" }, "None. Nice and clean."));

      const stats = el("div", { class: "ui-card eng-trio" },
        [["Words", data.words ?? 0, "spoken"], ["Pauses", data.long_pauses ?? 0, "over 1.5 s"], ["Time", `${Math.round(Number(data.duration_s) || 0)}s`, "speaking"]].map(([label, value, hint]) =>
          el("div", { class: "eng-trio-item" }, el("strong", { class: "ui-num" }, String(value)), el("span", {}, label), el("span", { class: "muted eng-hint" }, hint))));

      const transcript = section("What I heard", "note", "info",
        el("p", { class: "eng-transcript-text", lang: "en" }, markTranscript(String(data.transcript || ""), Object.keys(fillerItems), low.map((w) => String(w.word)))),
        el("p", { class: "eng-legend" },
          el("span", {}, el("mark", { class: "unclear" }, "word"), " may have been unclear"),
          el("span", {}, el("mark", { class: "filler" }, "um"), " filler word")),
        el("p", { class: "muted eng-small" }, "Highlighted words are only a hint that they may have been unclear to the speech recogniser. They are not a grade."));

      const out = [hero, pace, fillers, stats, transcript];

      if (Array.isArray(fb.grammar) && fb.grammar.length) {
        out.push(section("Fix these", "pen", "bad", el("ul", { class: "eng-list" }, fb.grammar.map((g) => el("li", {}, correctionBlock(g))))));
      }
      if (Array.isArray(fb.vocabulary) && fb.vocabulary.length) {
        out.push(section("Better words", "sparkles", "violet", el("ul", { class: "eng-list" }, fb.vocabulary.map((v) => el("li", {}, correctionBlock(v))))));
      }
      if (fb.rewrite) {
        const rewrite = String(fb.rewrite);
        out.push(section("A clearer way to say it", "wand", "teal",
          el("p", { class: "eng-rewrite", lang: "en" }, rewrite),
          el("div", { class: "eng-actions" },
            ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", onClick: async () => ctx.toast(await copyText(rewrite) ? "Copied" : "Couldn't copy. Select the text instead.") }),
            ui.button({ label: "Listen", icon: "volume", variant: "tonal", size: "sm", onClick: () => ctx.speak(rewrite) }))));
      }
      if (Array.isArray(fb.tips) && fb.tips.length) {
        out.push(section("Tips for next time", "target", "orange", el("ul", { class: "eng-tips" }, fb.tips.map((t) => el("li", {}, String(t))))));
      }
      out.push(ui.button({ label: "Try again", icon: "mic", variant: "primary", size: "lg", block: true, onClick: () => {
        result.replaceChildren();
        announce.textContent = "";
        recorder.stage.scrollIntoView({ block: "center", behavior: "smooth" });
        recorder.button.focus({ preventScroll: true });
      } }));
      result.replaceChildren(...out);
      hero.scrollIntoView({ block: "start" });
    }

    pane.append(
      el("div", { class: "ui-card eng-setup" },
        el("span", { class: "ui-caps" }, "Scenario"), picker.node, picker.desc),
      promptCard, recorder.stage, announce, result);

    return {
      node: pane,
      enter() {
        if (loaded) return;
        loaded = true;
        picker.load().catch(() => { loaded = false; });
      },
    };
  }

  // =========================================================================================
  // 2. ROLE-PLAY
  // =========================================================================================
  function buildRoleplay() {
    const pane = el("section", { class: "eng-pane", "aria-label": "Role-play" });
    const picker = createPicker({ allowFree: false, remember: "hub.english.scenario.roleplay", mode: "cards", label: "Pick a conversation" });
    const startBtn = ui.button({ label: "Start conversation", icon: "play", variant: "primary", size: "lg", block: true });
    const setup = el("div", { class: "eng-rp-setup" },
      el("div", { class: "eng-rp-intro" }, el("h3", {}, "Practise a real conversation"),
        el("p", { class: "muted" }, "Pick a scene. I'll play the other person and gently fix your English as we go.")),
      picker.node, startBtn);

    const log = el("div", { class: "eng-log", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
    let aloud = store.get("hub.english.aloud") === "1";
    const aloudSwitch = ui.switchControl({ checked: aloud, label: "Read partner replies aloud", onChange: (v) => { aloud = v; store.set("hub.english.aloud", v ? "1" : "0"); } });
    const resetBtn = ui.button({ label: "New", icon: "refresh", variant: "ghost", size: "sm", "aria-label": "Start a new conversation" });
    const titleNode = el("strong", { class: "eng-rp-title ui-truncate" }, "");
    let sceneTile = el("span", {});
    const head = el("div", { class: "ui-card ui-card--flat eng-rp-head" },
      el("div", { class: "eng-rp-headrow" }, sceneTile, el("div", { class: "eng-rp-headtext" }, el("span", { class: "ui-caps" }, "Scene"), titleNode), resetBtn),
      el("label", { class: "eng-aloud" }, el("span", {}, "Read partner replies aloud"), aloudSwitch));

    const composerInput = el("textarea", { id: "eng-rp-input", rows: "1", placeholder: "Type your answer, or tap the microphone", "aria-label": "Your answer", maxlength: "2000" });
    const sendBtn = el("button", { class: "ui-btn ui-btn--primary ui-btn--icon eng-send", type: "button", "aria-label": "Send your answer" }, icon("send"));
    const micStatus = el("p", { class: "eng-small muted eng-mic-line", role: "status", "aria-live": "polite" });
    const recorder = createRecorder(ctx, {
      big: false, name: "answer", maxSeconds: MAX_ROLEPLAY_SECONDS,
      startLabel: "Record your answer", stopLabel: "Stop recording",
      onAudio: (blob, filename) => transcribe(blob, filename),
    });
    const composer = el("div", { class: "eng-composer" },
      recorder.note, micStatus,
      el("div", { class: "eng-composer-row" },
        recorder.button,
        el("div", { class: "eng-input-wrap" }, composerInput, recorder.timer),
        sendBtn));
    const conversation = el("div", { class: "eng-conversation", hidden: true }, head, log, composer);

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
    function autosize() {
      composerInput.style.height = "auto";
      composerInput.style.height = `${Math.min(140, composerInput.scrollHeight)}px`;
    }
    composerInput.addEventListener("input", autosize);

    function bubble(role, text) {
      const body = el("div", { class: "eng-bubble-text", lang: "en" }, text);
      const bub = el("div", { class: "eng-bubble" }, sr(role === "user" ? "You: " : "Partner: "), body);
      const node = el("div", { class: `eng-msg ${role}` },
        role === "partner" ? ui.tile("bot", "accent", "sm") : null, el("div", { class: "eng-msg-col" }, bub));
      log.append(node);
      node.scrollIntoView({ block: "nearest" });
      return { node, body, col: node.querySelector(".eng-msg-col") };
    }

    // One turn: send `message` (or null for the opening) and stream the partner's reply.
    async function turn(message, userBubble) {
      streaming = true;
      refresh();
      const partner = bubble("partner", "");
      partner.node.classList.add("waiting");
      const wait = el("span", { class: "eng-dots", "aria-hidden": "true" }, el("i"), el("i"), el("i"));
      partner.body.append(wait);
      let checking = null;
      if (userBubble) {
        checking = el("div", { class: "eng-corr-slot muted eng-small" }, el("span", { class: "ui-spinner eng-mini-spin", "aria-hidden": "true" }), "Checking your English...");
        userBubble.col.append(checking);
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
        log.append(errorBox({ message: failed }));
        if (userBubble && message) {
          // Not answered: put the text back so nothing is lost.
          userBubble.node.remove();
          composerInput.value = message;
          autosize();
        } else if (!reply && !userBubble) {
          log.append(ui.button({ label: "Try the opening again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => { log.replaceChildren(); turn(null, null); } }));
        }
      } else if (reply && aloud) {
        ctx.speak(reply);
      }
      if (checking && !gotCorrection) checking.remove();
      streaming = false;
      controller = null;
      refresh();
    }

    function showCorrection(slot, correction) {
      if (!slot) return;
      slot.className = "eng-corr-slot";
      if (!correction) {
        slot.replaceChildren(el("span", { class: "eng-good-tag" }, icon("check-circle", 15), "Sounded natural"));
        return;
      }
      slot.replaceChildren(correctionBlock(correction, true));
    }

    async function send() {
      const text = composerInput.value.trim();
      if (!text || streaming || transcribing) return;
      composerInput.value = "";
      autosize();
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
          autosize();
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
      scenarioId = picker.value;
      if (!scenarioId) return;
      sessionId = sessionFor(scenarioId);
      started = true;
      setup.hidden = true;
      conversation.hidden = false;
      const idx = picker.list.findIndex((s) => s.id === scenarioId);
      titleNode.textContent = (picker.list[idx] || {}).title || "Conversation";
      sceneTile.replaceWith(sceneTile = ui.tile(SCENARIO_ICONS[Math.max(0, idx) % SCENARIO_ICONS.length], SCENARIO_TONES[Math.max(0, idx) % SCENARIO_TONES.length]));
      log.replaceChildren();
      composerInput.value = "";
      autosize();
      micStatus.textContent = "";
      await turn(null, null);
    }
    startBtn.addEventListener("click", begin);

    async function resetConversation() {
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
      picker.node.querySelector('[aria-checked="true"]')?.focus();
    }
    resetBtn.addEventListener("click", async () => {
      if (log.querySelector(".eng-msg.user")) {
        const ok = await ui.confirm({ title: "Start a new conversation?", message: "This clears the current conversation.", confirmLabel: "Start over", danger: true });
        if (!ok) return;
      }
      resetConversation();
    });

    pane.append(setup, conversation);
    refresh();

    return {
      node: pane,
      enter() {
        if (loaded) return;
        loaded = true;
        picker.load().catch(() => { loaded = false; });
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
      placeholder: "Paste or write an email, message or paragraph here.", "aria-label": "Your text" });
    const counter = el("span", { class: "eng-counter ui-num muted" }, "0 / 5000");
    input.addEventListener("input", () => { counter.textContent = `${input.value.length} / 5000`; });
    const button = ui.button({ label: "Fix my English", icon: "wand", variant: "primary", size: "lg", block: true });
    const clear = ui.button({ label: "Clear", variant: "ghost" });
    const result = el("div", { class: "eng-result", "aria-live": "polite" });

    async function run() {
      const text = input.value.trim();
      if (!text) { result.replaceChildren(emptyBox("Nothing to check yet", "Write or paste some text first.", "pen")); input.focus(); return; }
      result.setAttribute("aria-busy", "true");
      result.replaceChildren(el("div", { class: "ui-card eng-analysing" }, loadingBox("Reading your text..."), ui.skeleton("lines", 4)));
      try {
        const data = await ui.busy(button, api("/api/english/correct", { method: "POST", json: { text, provider } }));
        const corrected = String(data.corrected || "");
        const changes = Array.isArray(data.changes) ? data.changes : [];
        const ops = diffWords(text, corrected);
        const edits = ops.filter((o) => o.t !== "eq").length;
        const view = el("p", { class: "eng-diff", lang: "en" });
        const showClean = (clean) => {
          if (clean) view.replaceChildren(corrected);
          else view.replaceChildren(...diffNodes(ops));
        };
        showClean(false);
        const seg = ui.segmented({ options: [{ value: "diff", label: "Changes" }, { value: "clean", label: "Clean" }], value: "diff",
          label: "How to show the corrected text", onChange: (v) => showClean(v === "clean") });
        const out = [el("div", { class: "ui-card eng-block" },
          el("div", { class: "eng-block-head" }, ui.tile("check-circle", "good", "sm"), el("h3", {}, "Corrected text"), edits ? null : ui.badge("No changes", "good")),
          edits ? seg : null,
          view,
          edits ? el("p", { class: "eng-legend" }, el("span", {}, el("del", { class: "eng-del" }, "removed")), el("span", {}, el("ins", { class: "eng-ins" }, "added"))) : null,
          el("div", { class: "eng-actions" },
            ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", onClick: async () => ctx.toast(await copyText(corrected) ? "Copied" : "Couldn't copy. Select the text instead.") }),
            ui.button({ label: "Listen", icon: "volume", variant: "tonal", size: "sm", onClick: () => ctx.speak(corrected) })))];
        if (changes.length) {
          out.push(el("div", { class: "ui-card eng-block" },
            el("div", { class: "eng-block-head" }, ui.tile("pen", "bad", "sm"), el("h3", {}, "What changed"), ui.badge(String(changes.length), "accent")),
            el("ul", { class: "eng-list" }, changes.map((c) => el("li", {}, correctionBlock(c))))));
        } else {
          out.push(emptyBox("Nothing needed changing", "Well written.", "check-circle"));
        }
        if (data.tone_note) {
          out.push(el("div", { class: "ui-card eng-block" },
            el("div", { class: "eng-block-head" }, ui.tile("smile", "violet", "sm"), el("h3", {}, "Tone")), el("p", {}, String(data.tone_note))));
        }
        result.replaceChildren(...out);
        ui.haptic("success");
      } catch (error) {
        result.replaceChildren(errorBox(error, run));
      } finally {
        result.setAttribute("aria-busy", "false");
      }
    }
    button.addEventListener("click", run);
    clear.addEventListener("click", () => { input.value = ""; counter.textContent = "0 / 5000"; result.replaceChildren(); input.focus(); });

    pane.append(
      el("div", { class: "ui-card eng-setup" },
        el("div", { class: "eng-field-head" }, el("label", { for: "eng-correct-text", class: "ui-caps" }, "Your text"), counter),
        input,
        button, el("div", { class: "eng-center" }, clear)),
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
    const addBtn = ui.button({ label: "Add", icon: "plus", variant: "primary", type: "submit" });
    const addForm = el("form", { class: "ui-card eng-setup" },
      el("div", { class: "eng-block-head" }, ui.tile("plus", "accent", "sm"), el("h3", {}, "Add a word")),
      el("p", { class: "muted eng-small" }, "Type a word or phrase you met today. I'll explain it and add it to your review."),
      el("div", { class: "eng-add-row" }, el("div", { class: "grow" }, addInput), addBtn));
    const added = el("div", { class: "eng-added", "aria-live": "polite" });
    let data = null;
    let loading = false;
    let loadedOnce = false;

    async function load() {
      if (loading) return;
      loading = true;
      today.replaceChildren(ui.skeleton("card", 2), el("p", { class: "muted eng-small" }, "The first time each day this can take a little while."));
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
      today.replaceChildren(el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "New words today"),
        words.length ? el("span", { class: "muted eng-small" }, "Tap a card to flip") : null));
      if (!words.length) today.append(emptyBox("No new words today", "Add a word of your own below.", "book"));
      else today.append(el("div", { class: "eng-cards" }, words.map((w) => flipCard(w, ctx))));
    }

    function renderReviewStart() {
      const due = data.due || [];
      reviewBox.replaceChildren(el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "Review")));
      if (!due.length) {
        reviewBox.append(emptyBox("Nothing due right now", "Come back tomorrow, or after you have added more words.", "check-circle"));
        return;
      }
      reviewBox.append(el("div", { class: "ui-card ui-card--elevated eng-due" },
        ui.ringProgress({ value: 1, size: 64, stroke: 7, tone: "accent", big: String(due.length), label: "Words due" }),
        el("div", { class: "eng-due-text" },
          el("strong", {}, `${due.length} ${due.length === 1 ? "word is" : "words are"} ready`),
          el("span", { class: "muted eng-small" }, "A few minutes keeps them fresh.")),
        ui.button({ label: "Start review", icon: "play", variant: "primary", onClick: () => startReview(due) })));
    }

    // Flashcards: word -> flip to the meaning -> Again / Hard / Good / Easy.
    function startReview(due) {
      const queue = due.slice();
      let index = 0;
      const total = queue.length;
      const stage = el("div", { class: "eng-flash" });
      reviewBox.replaceChildren(el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "Review")), stage);

      function renderCard() {
        if (index >= total) {
          stage.replaceChildren(el("div", { class: "ui-card ui-card--elevated eng-done" },
            ui.ringProgress({ value: 1, size: 96, stroke: 8, tone: "good", big: String(total), small: total === 1 ? "word" : "words", label: "Words reviewed" }),
            el("h3", {}, "All done"),
            el("p", { class: "muted" }, `You reviewed ${total} ${total === 1 ? "word" : "words"}. Nicely done.`),
            ui.button({ label: "Finish", icon: "check", variant: "primary", size: "lg", block: true, onClick: () => {
              data.due = [];
              renderReviewStart();
            } })));
          ui.haptic("success");
          return;
        }
        const w = queue[index];
        const feedback = el("p", { class: "eng-small eng-good eng-feedback", role: "status", "aria-live": "polite" });
        const showBtn = ui.button({ label: "Show meaning", icon: "eye", variant: "primary", size: "lg", block: true });
        const ratings = el("div", { class: "eng-ratings", hidden: true, role: "group", "aria-label": "How well did you know it?" });
        const card = flipCard(w, ctx, { tall: true, onFlip: (flipped) => {
          if (flipped && ratings.hidden) { ratings.hidden = false; showBtn.hidden = true; }
        } });
        const buttons = [];
        for (const [value, label] of [["again", "Again"], ["hard", "Hard"], ["good", "Good"], ["easy", "Easy"]]) {
          const b = el("button", { class: `ui-btn ui-btn--secondary eng-rate ${value}`, type: "button", onclick: () => rate(value) }, label);
          buttons.push(b);
          ratings.append(b);
        }
        async function rate(value) {
          buttons.forEach((b) => { b.disabled = true; });
          feedback.className = "eng-small muted eng-feedback";
          feedback.textContent = "Saving...";
          try {
            const res = await api(`/api/english/vocab/${encodeURIComponent(w.id)}/review`, { method: "POST", json: { rating: value } });
            ui.haptic("select");
            feedback.className = "eng-small eng-good eng-feedback";
            feedback.textContent = `Saved. You'll see it again ${friendlyDue(res.next_due)}.`;
            setTimeout(() => { index += 1; renderCard(); }, 1300);
          } catch (error) {
            buttons.forEach((b) => { b.disabled = false; });
            feedback.className = "eng-small error eng-feedback";
            feedback.textContent = friendly(error);
          }
        }
        showBtn.addEventListener("click", () => { card.flip(true); showBtn.hidden = true; ratings.hidden = false; });
        const bar = ui.progressBar(index / total, "Review progress");
        stage.replaceChildren(
          el("div", { class: "eng-flash-top" },
            el("span", { class: "muted eng-small ui-num" }, `Word ${index + 1} of ${total}`), bar),
          card, showBtn, ratings, feedback);
        showBtn.focus({ preventScroll: true });
      }
      renderCard();
    }

    addForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const word = addInput.value.trim();
      if (!word) { addInput.focus(); return; }
      added.replaceChildren(loadingBox(`Looking up "${word}"...`));
      try {
        const entry = await ui.busy(addBtn, api("/api/english/vocab/add", { method: "POST", json: { word, provider } }));
        addInput.value = "";
        ui.haptic("success");
        added.replaceChildren(el("span", { class: "ui-caps" }, "Added to your words"), flipCard(entry, ctx));
      } catch (error) {
        added.replaceChildren(errorBox(error));
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

    async function load() {
      if (loading) return;
      loading = true;
      body.setAttribute("aria-busy", "true");
      if (!body.children.length) body.replaceChildren(ui.skeleton("card", 3));
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

    function weekBars(trend) {
      const days = [];
      const now = new Date();
      for (let i = 6; i >= 0; i -= 1) {
        const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
        days.push({ d, count: 0, today: i === 0 });
      }
      for (const t of trend) {
        const d = parseDay(t.date);
        if (!d) continue;
        const hit = days.find((x) => x.d.getTime() === d.getTime());
        if (hit) hit.count += 1;
      }
      const max = Math.max(2, ...days.map((x) => x.count));
      const total = days.reduce((s, x) => s + x.count, 0);
      return el("div", { class: "ui-card eng-block" },
        el("div", { class: "eng-block-head" }, ui.tile("calendar", "teal", "sm"), el("h3", {}, "This week"),
          el("span", { class: "ui-badge" }, `${total} ${total === 1 ? "session" : "sessions"}`)),
        el("div", { class: "eng-week", role: "img", "aria-label": `Sessions in the last 7 days: ${days.map((x) => `${x.d.toLocaleDateString(undefined, { weekday: "long" })} ${x.count}`).join(", ")}` },
          days.map((x) => el("div", { class: "eng-day" + (x.today ? " is-today" : "") },
            el("span", { class: "eng-day-count ui-num" }, x.count ? String(x.count) : ""),
            el("span", { class: "eng-day-bar" + (x.count ? " has" : ""), style: `--h:${x.count ? Math.max(0.18, x.count / max) : 0.06}` }),
            el("span", { class: "eng-day-label" }, x.d.toLocaleDateString(undefined, { weekday: "narrow" }))))));
    }

    function badges(data, trend) {
      const streak = data.streak_days || 0;
      const sessions = data.sessions_total || 0;
      const last = data.last_session || {};
      const best = Math.max(0, ...trend.map((t) => Number(t.overall) || 0));
      const list = [
        { icon: "mic", tone: "accent", title: "First session", hint: "Record one practice", on: sessions >= 1 },
        { icon: "flame", tone: "orange", title: "3-day streak", hint: "Practise 3 days in a row", on: streak >= 3 },
        { icon: "zap", tone: "warn", title: "Week warrior", hint: "A 7-day streak", on: streak >= 7 },
        { icon: "award", tone: "violet", title: "Ten sessions", hint: "Complete 10 practices", on: sessions >= 10 },
        { icon: "waveform", tone: "teal", title: "Smooth talker", hint: "Under 3 fillers per 100 words", on: sessions > 0 && Number.isFinite(Number(last.fillers_per_100)) && Number(last.fillers_per_100) <= 3 },
        { icon: "timer", tone: "info", title: "Steady pace", hint: "110 to 160 words a minute", on: Number(last.wpm) >= 110 && Number(last.wpm) <= 160 },
        { icon: "star", tone: "pink", title: "High scorer", hint: "An overall score of 8 or more", on: best >= 8 },
        { icon: "book", tone: "good", title: "Words on track", hint: "No words waiting for review", on: sessions > 0 && (data.vocab_due || 0) === 0 },
      ];
      const earned = list.filter((b) => b.on).length;
      return el("div", { class: "ui-card eng-block" },
        el("div", { class: "eng-block-head" }, ui.tile("award", "violet", "sm"), el("h3", {}, "Badges"), el("span", { class: "ui-badge" }, `${earned} of ${list.length}`)),
        el("ul", { class: "eng-badges" }, list.map((b) => el("li", { class: "eng-badge-item" + (b.on ? " is-on" : " is-locked") },
          el("span", { class: "eng-badge-art" }, ui.tile(b.on ? b.icon : "lock", b.on ? b.tone : "gray")),
          el("strong", {}, b.title),
          el("span", { class: "eng-badge-hint" }, sr(b.on ? "Earned. " : "Locked. "), b.hint)))));
    }

    function render(data, scenarios) {
      const streak = data.streak_days || 0;
      const trend = Array.isArray(data.trend) ? data.trend : [];
      const hero = el("div", { class: "ui-card ui-card--elevated eng-streak" },
        el("div", { class: "eng-flame" + (streak ? " is-lit" : ""), "aria-hidden": "true" }, icon("flame", 34)),
        el("div", { class: "eng-streak-text" },
          el("div", { class: "eng-bignum" }, el("strong", { class: "ui-num" }, String(streak))),
          el("span", { class: "eng-streak-label" }, streak === 1 ? "day streak" : "day streak"),
          el("span", { class: "muted eng-small" }, streak ? "Keep it going: practise today." : "Practise today to start a streak.")),
        el("div", { class: "eng-streak-stats" },
          el("div", {}, el("strong", { class: "ui-num" }, String(data.sessions_total || 0)), el("span", {}, "sessions")),
          el("button", { class: "eng-due-link", type: "button", onclick: () => select("vocab") },
            el("strong", { class: "ui-num" }, String(data.vocab_due || 0)), el("span", {}, "words due"))));
      const out = [hero, weekBars(trend)];

      const last = data.last_session;
      if (!last) {
        out.push(el("div", { class: "ui-card" }, ui.emptyState({ icon: "mic", title: "No speaking sessions yet",
          body: "Record your first practice and your trends will show up here.", action: { label: "Go to Speak", icon: "mic", variant: "primary", onClick: () => select("speak") } })));
        out.push(badges(data, trend));
        body.replaceChildren(...out);
        return;
      }
      const sc = scenarios.find((s) => s.id === last.scenario);
      const scores = last.scores || {};
      out.push(el("div", { class: "ui-card eng-block" },
        el("div", { class: "eng-block-head" }, ui.tile("clock", "info", "sm"), el("h3", {}, "Last session")),
        el("p", { class: "muted eng-small" }, [shortDate(last.date), sc ? sc.title : last.scenario ? last.scenario : "Free talk"].filter(Boolean).join(" - ")),
        last.summary ? el("p", {}, String(last.summary)) : null,
        el("div", { class: "eng-subscores eng-subscores--4" },
          scoreRing("Overall", scores.overall, { size: 56, stroke: 6 }), scoreRing("Fluency", scores.fluency, { size: 56, stroke: 6 }),
          scoreRing("Grammar", scores.grammar, { size: 56, stroke: 6 }), scoreRing("Vocab", scores.vocabulary, { size: 56, stroke: 6 })),
        el("p", { class: "muted eng-small" }, `${last.wpm ?? "-"} words per minute, ${last.fillers_per_100 ?? "-"} fillers per 100 words.`)));

      // trend: one chart, switchable
      if (!trend.length) {
        out.push(emptyBox("No trend yet", "Do a speaking session to see how you are improving.", "trend"));
      } else {
        const dates = trend.map((t) => t.date);
        const num = (v) => (v == null || Number.isNaN(Number(v)) ? NaN : Number(v));
        const wpm = trend.map((t) => num(t.wpm));
        const fill = trend.map((t) => num(t.fillers_per_100));
        const overall = trend.map((t) => num(t.overall));
        const maxWpm = niceMax(Math.max(...wpm.filter(Number.isFinite), 120), 40);
        const maxFill = niceMax(Math.max(...fill.filter(Number.isFinite), 5), 5);
        const r1 = (v) => String(Math.round(v * 10) / 10);
        const metrics = {
          pace: { title: "Pace", caption: "Words per minute. About 110 to 160 is comfortable.", unit: "words/min", values: wpm, max: maxWpm, ticks: [0, maxWpm / 2, maxWpm], format: (v) => String(Math.round(v)), tone: "pace" },
          fillers: { title: "Filler words", caption: "Per 100 words. Lower is better.", unit: "fillers per 100 words", values: fill, max: maxFill, ticks: [0, maxFill / 2, maxFill], format: r1, tone: "fillers" },
          score: { title: "Overall score", caption: "Out of 10. Higher is better.", unit: "score out of 10", values: overall, max: 10, ticks: [0, 5, 10], format: r1, tone: "score" },
        };
        const chartBox = el("div", { class: "eng-chart-box" });
        const caption = el("p", { class: "muted eng-small" });
        const draw = (key) => {
          const m = metrics[key];
          caption.textContent = m.caption;
          chartBox.replaceChildren(lineChart({ ...m, dates }));
        };
        const seg = ui.segmented({ options: [{ value: "pace", label: "Pace" }, { value: "fillers", label: "Fillers" }, { value: "score", label: "Score" }],
          value: "pace", block: true, label: "Trend to show", onChange: draw });
        draw("pace");
        out.push(el("div", { class: "ui-card eng-block" },
          el("div", { class: "eng-block-head" }, ui.tile("trend", "good", "sm"), el("h3", {}, "Trends")),
          seg, caption, chartBox,
          trend.length < 2 ? el("p", { class: "muted eng-small" }, "One session so far. Do another to see a line.") : null,
          el("details", { class: "eng-table" },
            el("summary", {}, "Show the numbers"),
            el("div", { class: "eng-table-wrap" }, el("table", {},
              el("thead", {}, el("tr", {}, ["Date", "Words/min", "Fillers/100", "Overall"].map((h) => el("th", { scope: "col" }, h)))),
              el("tbody", {}, trend.map((t) => el("tr", {},
                el("td", {}, shortDate(t.date)), el("td", {}, t.wpm ?? "-"), el("td", {}, t.fillers_per_100 ?? "-"), el("td", {}, t.overall ?? "-")))))))));
      }
      out.push(badges(data, trend));
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
      parts[current]?.enter?.();
    },
    hide() {
      for (const r of recorders) r.cancel();
    },
    openSection(name) {
      if (SECTIONS.some((s) => s.id === name)) select(name);
    },
  };
}
