// Hands-free wake word: say "Hey Jarvis ..." or "Hey Friday ..." and voice mode opens with the rest already sent.
// Uses the browser's own SpeechRecognition (continuous), so it only works while the app is open and the screen is on:
// Android stops the mic in the background. Off by default (localStorage "hub.wake" = "1" turns it on): a mic that
// stays open is a privacy and battery choice. Only one recognizer runs at a time: wake pauses while voice mode or a
// dictation mic is active and comes back afterwards. Matching lives in wakephrase.js (pure, tested with node).
import { el } from "./api.js";
import * as ui from "./ui.js";
import { matchWake } from "./wakephrase.js";

const KEY = "hub.wake";
const KEEP_KEY = "hub.wake.keepawake";      // "0" = off; anything else (or unset) = on while wake is on
const CHIP_TEXT = "Listening for Hey Jarvis or Hey Friday";
const SETTLE_MS = 1100;            // after a match, wait this long for the rest of the sentence (a final result skips the wait)
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 30000;
const HEALTHY_MS = 8000;           // a session that lasted this long was fine: reset the backoff

export function wakeSupported() { return !!(window.SpeechRecognition || window.webkitSpeechRecognition); }
export function wakeEnabled() { try { return localStorage.getItem(KEY) === "1"; } catch (_) { return false; } }
export function setWakeEnabled(on) {
  try { if (on) localStorage.setItem(KEY, "1"); else localStorage.removeItem(KEY); } catch (_) { /* not remembered */ }
  window.dispatchEvent(new CustomEvent("hub:wake", { detail: { on: !!on } }));
}

// ---- keep the screen on (Screen Wake Lock API): Android stops the microphone when the screen turns off -----
export function wakeLockSupported() { try { return !!navigator.wakeLock?.request; } catch (_) { return false; } }
export function keepAwakeEnabled() { try { return localStorage.getItem(KEEP_KEY) !== "0"; } catch (_) { return true; } }
export function setKeepAwake(on) {
  try { if (on) localStorage.removeItem(KEEP_KEY); else localStorage.setItem(KEEP_KEY, "0"); } catch (_) { /* not remembered */ }
  window.dispatchEvent(new CustomEvent("hub:wake", { detail: { on: wakeEnabled() } }));
}

// ---- chime (WebAudio, no asset files): Jarvis is lower, Friday higher ----------------------------------
let audio = null;
function unlockAudio() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    audio = audio || new AC();
    if (audio.state === "suspended") audio.resume().catch(() => {});
  } catch (_) { /* no sound is fine */ }
}
export function chime(persona) {
  try {
    unlockAudio();
    if (!audio) return;
    const [a, b] = persona === "friday" ? [784, 1175] : [523, 784];
    const t = audio.currentTime;
    [a, b].forEach((f, i) => {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + i * 0.11);
      g.gain.exponentialRampToValueAtTime(0.16, t + i * 0.11 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.11 + 0.22);
      o.connect(g); g.connect(audio.destination);
      o.start(t + i * 0.11); o.stop(t + i * 0.11 + 0.25);
    });
  } catch (_) { /* silent */ }
}

// ---- the controller ------------------------------------------------------------------------------------
export function mountWake(ctx, { bar = document.getElementById("appbar") } = {}) {
  const chip = el("div", { class: "wk-chip", role: "status", "aria-live": "polite", hidden: true },
    el("span", { class: "wk-dot", "aria-hidden": "true" }), el("span", { class: "wk-text" }, CHIP_TEXT));
  if (bar?.parentNode) bar.after(chip); else document.body.append(chip);   // its own row under the top bar: never covers a screen's title
  const text = chip.querySelector(".wk-text");

  let rec = null, running = false, fails = 0, startedAt = 0, restartTimer = null, settleTimer = null;
  let pending = null, problem = "", jarvisName = "Jarvis", dictating = 0, nameLoaded = false;

  const voiceOpen = () => document.documentElement.classList.contains("vm-open");
  const wanted = () => wakeEnabled() && wakeSupported() && !problem && !document.hidden && !voiceOpen() && !dictating && ctx.secure !== false;

  function paint() {
    const show = wakeEnabled() && (!!problem || (wakeSupported() && !voiceOpen() && !document.hidden));
    chip.hidden = !show;
    chip.classList.toggle("is-problem", !!problem);
    chip.classList.toggle("is-live", running && !problem);
    text.textContent = problem || CHIP_TEXT;
  }

  function loadName() {
    if (nameLoaded) return;
    nameLoaded = true;
    import("./fullcontrol.js").then((fc) => fc.refresh()).then((info) => { if (info?.jarvis_name) jarvisName = info.jarvis_name; }).catch(() => {});
  }

  function stopRec() {
    clearTimeout(settleTimer); pending = null;
    const r = rec; rec = null; running = false;
    if (r) { r.onend = r.onerror = r.onresult = null; try { r.abort(); } catch (_) { /* already stopped */ } }
  }

  function fire(match) {
    stopRec();
    chip.dataset.persona = match.persona;
    chime(match.persona);
    ui.haptic?.("success");
    import("./voicemode.js").then((vm) => {
      vm.setPreload(match.rest, match.persona);
      // Friday with nothing after her name opens in her greeting; everything else just opens voice mode.
      return ctx.open("assistant", match.persona === "friday" && !match.rest ? "friday" : "voice");
    }).catch((error) => ctx.toast?.(`Could not open voice mode: ${error.message}`, { kind: "bad" }));
    paint();
  }

  function onResult(e) {
    let best = null, final = false;
    for (let i = e.resultIndex; i < e.results.length; i += 1) {
      const m = matchWake(e.results[i][0]?.transcript || "", { jarvisName });
      if (m) { best = m; final = !!e.results[i].isFinal; }
    }
    fails = 0;
    if (!best) return;
    pending = best;
    clearTimeout(settleTimer);
    if (final) fire(pending);
    else settleTimer = setTimeout(() => pending && fire(pending), SETTLE_MS);
  }

  function onError(e) {
    const code = e?.error || "";
    if (code === "not-allowed" || code === "service-not-allowed") {
      problem = "Hey Jarvis is off: the microphone is blocked for this site.";
      ctx.toast?.("The wake word needs the microphone. Allow it for this site (lock icon next to the address), then switch it on again in Settings.", { kind: "warn" });
    } else if (code === "audio-capture") {
      problem = "Hey Jarvis is off: no microphone found.";
    } else if (code === "language-not-supported") {
      lang = "en-US";
    }
    // no-speech, aborted and network are routine: onend restarts with a backoff
  }

  let lang = "en-IN";
  function begin() {
    if (running || !wanted()) { paint(); return; }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    try {
      const r = new SR();
      r.continuous = true; r.interimResults = true; r.maxAlternatives = 1; r.lang = lang;
      r.onresult = onResult;
      r.onerror = onError;
      r.onend = () => {
        if (rec !== r) return;
        rec = null; running = false;
        if (Date.now() - startedAt > HEALTHY_MS) fails = 0; else fails += 1;
        paint();
        schedule();
      };
      rec = r; running = true; startedAt = Date.now();
      r.start();
    } catch (_) {
      rec = null; running = false; fails += 1; schedule();
    }
    paint();
  }

  // Android Chrome ends a continuous session every so often: start again, slower each time it ends at once, so a
  // broken recognizer never turns into a restart storm.
  function schedule() {
    clearTimeout(restartTimer);
    if (!wanted()) return;
    const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.min(fails, 8)) + (fails ? 0 : 150);
    restartTimer = setTimeout(begin, delay);
  }

  // The lock is held only while wake listening is on, the page is visible and the switch allows it. The browser drops it
  // when the page is hidden, so every sync (visibilitychange included) asks again. Unsupported or refused: silently nothing.
  let lock = null, locking = false;
  function syncLock() {
    const want = wakeEnabled() && keepAwakeEnabled() && wakeLockSupported() && !document.hidden;
    if (!want) {
      const l = lock; lock = null;
      if (l) { try { Promise.resolve(l.release()).catch(() => {}); } catch (_) { /* gone already */ } }
      return;
    }
    if (lock || locking) return;
    locking = true;
    Promise.resolve().then(() => navigator.wakeLock.request("screen")).then((l) => {
      locking = false;
      if (!(wakeEnabled() && keepAwakeEnabled() && !document.hidden)) { try { l.release(); } catch (_) { /* ignore */ } return; }
      lock = l;
      l.addEventListener?.("release", () => { if (lock === l) lock = null; });
    }).catch(() => { locking = false; });
  }

  function sync({ fresh = false } = {}) {
    syncLock();
    if (fresh) { problem = ""; fails = 0; }
    if (wanted()) { loadName(); if (!running) { clearTimeout(restartTimer); begin(); } }
    else { clearTimeout(restartTimer); stopRec(); }
    paint();
  }

  document.addEventListener("visibilitychange", () => sync());
  window.addEventListener("hub:wake", () => { unlockAudio(); sync({ fresh: true }); });
  // voice mode open/close flips this class on <html>
  new MutationObserver(() => sync()).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  // dictation mics (Talk tab, Today) say when they start and stop: window.dispatchEvent(new CustomEvent("hub:mic", {detail: {on}}))
  window.addEventListener("hub:mic", (e) => { dictating = Math.max(0, dictating + (e.detail?.on ? 1 : -1)); sync(); });
  window.addEventListener("pointerdown", unlockAudio, { once: true });
  sync();
  return { sync, isRunning: () => running, hasWakeLock: () => !!lock };
}
