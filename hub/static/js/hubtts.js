// Browser side of the hub's local text to speech (hub/tts.py, docs/api/tts.md): ask for audio, play it, stop it.
// Used by voice mode (voicemode.js, through speechqueue.js) and by the Talk tab's "Read answers aloud".
// The laptop makes the WAV (Piper, else Windows voices) and this phone only plays it, so the phone needs no voice at all.
// With the engine "This phone's voice" every call answers {kind: "browser"} and the phone's own speechSynthesis speaks.
import { api } from "./api.js";
import * as ui from "./ui.js";

let cached = null, cachedAt = 0, pending = null;
const TTL_MS = 30000;

// GET /api/tts/status, cached for 30 s. Resolves null when the hub is old or unreachable (callers then use the phone's voice).
export function loadVoiceStatus(force = false) {
  if (!force && cached && Date.now() - cachedAt < TTL_MS) return Promise.resolve(cached);
  if (pending) return pending;
  pending = api("/api/tts/status").then((s) => { cached = s; cachedAt = Date.now(); return s; })
    .catch(() => null).finally(() => { pending = null; });
  return pending;
}
export function forgetVoiceStatus() { cached = null; cachedAt = 0; }

// The profile ({engine, voice, rate, pitch, hub}) for an assistant id, or null. hub is true when the laptop makes the audio.
export function profileFor(status, persona) {
  if (!status || status.enabled === false || status.silenced) return null;
  const p = status.profiles && (status.profiles[persona] || status.profiles.jarvis);
  return p || null;
}

// What went wrong with the hub's voice the last time, for the one-tap fix in the UI: {reason, detail} or null.
let lastProblem = null;
export function voiceProblem() { return lastProblem; }

// One sentence to audio. {kind:"audio", data: Blob} | {kind:"browser"} | {kind:"silent", reason, detail} (panic: say nothing).
// A browser answer may carry {why:"engine", detail} when the laptop's voice failed: the caller uses the phone's voice and,
// if the phone has none, shows the detail with the "Use Windows voice" fix.
export async function hubSynth(text, { assistant = "", signal } = {}) {
  let r;
  try {
    r = await fetch("/api/tts", {
      method: "POST", credentials: "same-origin", signal,
      headers: { "Content-Type": "application/json", "X-Hub": "1" }, body: JSON.stringify({ text, assistant: assistant || undefined }),
    });
  } catch (_) { return { kind: "browser" }; }
  if (r.status === 423) {
    let body = {};
    try { body = await r.json(); } catch (_) { /* an old hub: a bare 423 */ }
    lastProblem = { reason: body.reason || "panic", detail: body.detail || "Voice is paused because Panic is on. Tap Resume." };
    return { kind: "silent", reason: lastProblem.reason, detail: lastProblem.detail };
  }
  if (r.status === 502) {
    let body = {};
    try { body = await r.json(); } catch (_) { /* not JSON */ }
    lastProblem = { reason: "engine", detail: body.detail || "The laptop's voice failed." };
    return { kind: "browser", why: "engine", detail: lastProblem.detail };
  }
  if (r.status !== 200) return { kind: "browser" };
  lastProblem = null;
  const blob = await r.blob();
  return blob.size > 44 ? { kind: "audio", data: blob } : { kind: "browser" };
}

// One tap: the laptop speaks with Windows' own voices (they need nothing installed); the phone needs no voice.
export async function useWindowsVoice() {
  await api("/api/tts/settings", { method: "PUT", json: { engine: "sapi", enabled: true } });
  forgetVoiceStatus();
  lastProblem = null;
}

// Panic really is on: resume it from here.
export async function resumeFromPanic() {
  const r = await api("/api/panic/resume", { method: "POST" });
  forgetVoiceStatus();
  lastProblem = null;
  return r;
}

// A toast that says the REAL reason with the one-tap fix: Resume (panic) or Use Windows voice (the laptop's voice failed).
export function explainProblem(problem, { onFixed } = {}) {
  const p = problem || lastProblem;
  if (!p) return;
  const done = (msg) => { ui.toast(msg, { kind: "good" }); onFixed?.(); };
  const fail = (e) => ui.toast(e?.message || "That did not work.", { kind: "bad" });
  if (p.reason === "panic") {
    ui.toast(p.detail, { kind: "warn", ms: 9000, action: { label: "Resume", onClick: () => resumeFromPanic().then(() => done("Resumed. Voice is back.")).catch(fail) } });
  } else {
    ui.toast(p.detail || "The voice did not work.", { kind: "warn", ms: 9000,
      action: { label: "Use Windows voice", onClick: () => useWindowsVoice().then(() => done("Now using the Windows voice on the laptop.")).catch(fail) } });
  }
}

// ---- sound on Android (WebView and Chrome): one <audio> element that a tap has unlocked ----------------------------
// Android lets an element that was started from a tap play later without one, so ONE shared element is unlocked on the
// first gesture and reused for every sentence (a fresh `new Audio()` per sentence would be refused again). A refused
// play fires "hub:audio-blocked" and the next tap unlocks again.
let shared = null, unlocked = false, armed = false, takeOver = null, unlockCtx = null;
function silentWavUrl() {
  const n = 800, buf = new Uint8Array(44 + n), v = new DataView(buf.buffer);
  const tag = (o, t) => { for (let i = 0; i < 4; i += 1) buf[o + i] = t.charCodeAt(i); };
  tag(0, "RIFF"); v.setUint32(4, 36 + n, true); tag(8, "WAVE"); tag(12, "fmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 8000, true); v.setUint32(28, 8000, true);
  v.setUint16(32, 1, true); v.setUint16(34, 8, true); tag(36, "data"); v.setUint32(40, n, true); buf.fill(128, 44);
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
}
export function sharedAudio() {
  if (!shared) { shared = new Audio(); shared.preload = "auto"; try { shared.setAttribute("playsinline", ""); } catch (_) { /* ignore */ } }
  return shared;
}
export function audioUnlocked() { return unlocked; }
const UNLOCK_EVENTS = ["pointerdown", "touchend", "click", "keydown"];
export function installAudioUnlock() {
  if (typeof document === "undefined" || unlocked || armed) return;
  armed = true;
  const go = () => {
    try {
      const a = sharedAudio();
      const url = silentWavUrl();
      a.src = url; a.volume = 0.01;
      const finish = () => {
        unlocked = true; armed = false; a.volume = 1;
        try { a.pause(); } catch (_) { /* ignore */ }
        try { URL.revokeObjectURL(url); } catch (_) { /* ignore */ }
        UNLOCK_EVENTS.forEach((t) => document.removeEventListener(t, go, true));
      };
      const started = a.play();
      if (started && started.then) started.then(finish, () => { /* not a real gesture yet: stay armed for the next tap */ }); else finish();
    } catch (_) { /* stay armed */ }
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) { unlockCtx = unlockCtx || new AC(); unlockCtx.resume?.(); }
    } catch (_) { /* optional */ }
  };
  UNLOCK_EVENTS.forEach((t) => document.addEventListener(t, go, true));
}
installAudioUnlock();

// Plays one Blob at a time on the shared, unlocked <audio> element; stop() ends it at once. onStart/onPulse are optional hooks.
export function createAudioPlayer({ onStart = () => {}, onPulse = () => {} } = {}) {
  let cur = null;
  const end = () => {
    if (!cur) return;
    const c = cur; cur = null;
    if (takeOver === end) takeOver = null;
    try { c.audio.pause(); } catch (_) { /* ignore */ }
    c.audio.onended = c.audio.onerror = c.audio.ontimeupdate = null;
    try { URL.revokeObjectURL(c.url); } catch (_) { /* ignore */ }
    c.resolve();
  };
  return {
    play(blob) {
      return new Promise((resolve) => {
        end();
        if (takeOver) takeOver();                       // another player on the same element ends first; its promise resolves
        let url = "", audio;
        try { url = URL.createObjectURL(blob); audio = sharedAudio(); audio.src = url; audio.volume = 1; } catch (_) { resolve(); return; }
        cur = { audio, url, resolve };
        takeOver = end;
        audio.onended = end;
        audio.onerror = end;
        audio.ontimeupdate = () => onPulse();
        let started;
        try { started = audio.play(); } catch (_) { started = Promise.reject(new Error("play")); }
        const blocked = (e) => {
          end();
          if (e && e.name === "NotAllowedError") {
            unlocked = false; installAudioUnlock();      // listen for the next tap
            try { window.dispatchEvent(new CustomEvent("hub:audio-blocked")); } catch (_) { /* ignore */ }
          }
        };
        if (started && started.then) started.then(() => onStart(), blocked); else onStart();
      });
    },
    stop: end,
    get playing() { return !!cur; },
  };
}

// The Talk tab: read one reply aloud through the assistant's hub voice, else the phone's (fallback(text) is the old way).
const talk = { player: null, gen: 0 };
export async function speakReply(text, persona, fallback) {
  const status = await loadVoiceStatus();
  const p = profileFor(status, persona || "jarvis");
  if (!p || !p.hub) { fallback?.(text); return; }
  const mine = ++talk.gen;
  talk.player = talk.player || createAudioPlayer();
  const spoken = String(text || "").replace(/```[\s\S]*?(```|$)/g, " I put the details on screen. ").replace(/https?:\/\/\S+/g, "a link")
    .replace(/[*_`#>|]+/g, "").replace(/\s+/g, " ").trim().slice(0, 700);
  for (const sentence of spoken.match(/[^.!?]+[.!?]*\s*/g) || [spoken]) {
    if (mine !== talk.gen || !sentence.trim()) return;
    const r = await hubSynth(sentence.trim(), { assistant: persona });
    if (mine !== talk.gen) return;
    if (r.kind === "silent") { explainProblem(r); return; }
    if (r.kind === "audio") await talk.player.play(r.data); else fallback?.(sentence);
  }
}
export function stopReply() { talk.gen += 1; talk.player?.stop(); }
