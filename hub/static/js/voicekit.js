// Speaking replies on the PHONE, reliably (Friday, Jarvis, alerts). One place for everything Android Chrome is picky about:
//   - speechSynthesis voices load lazily (getVoices() is [] at first, then "voiceschanged"): loadVoices() waits for them
//   - sound needs a user gesture once per page: unlock runs a silent utterance on the first tap
//   - one long utterance stalls on Android: replies are spoken sentence by sentence
//   - cancel() immediately followed by speak() can drop the new utterance: a short pause is kept between them
//   - a hub voice (Settings > Voices: Piper/Windows) is used when configured; the phone's voice is the fallback
// Plus the "Voice on/off" switch and "Test voice" button every screen shows (voiceControls) and plain-language messages.
import { el } from "./api.js";
import * as ui from "./ui.js";
import { loadVoiceStatus, profileFor, speakReply, stopReply } from "./hubtts.js";
import { speakable } from "./speechchunk.js";

const KEY = "hub.voice.on";
export const MESSAGES = {
  "no-api": "This browser can't speak. Open the app in Chrome, or choose a laptop voice in Settings > Voices.",
  "no-voice": "This phone has no text-to-speech voice installed. On Android: Settings > System > Languages > Text-to-speech output, "
    + "install Google Speech Services and a voice. Or choose a laptop voice (Piper or Windows) in Settings > Voices.",
  blocked: "The browser blocked sound until you tap something on the page. Tap Test voice once.",
  off: "Voice is switched off. Turn on Voice to hear replies.",
  error: "The voice stopped with an error. Try Test voice again.",
};

export function voiceOn() {
  try { return localStorage.getItem(KEY) !== "0"; } catch (_) { return true; }
}
export function setVoiceOn(on) {
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (_) { /* private mode */ }
  if (!on) stop();
}

const synth = () => {
  try { return ("speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined") ? window.speechSynthesis : null; } catch (_) { return null; }
};

let voicesPromise = null;
// The phone's voices, waiting for the lazy list (up to timeoutMs). Resolves [] when there are none.
export function loadVoices(timeoutMs = 2500) {
  const s = synth();
  if (!s) return Promise.resolve([]);
  const have = () => { try { return s.getVoices() || []; } catch (_) { return []; } };
  if (have().length) return Promise.resolve(have());
  if (voicesPromise) return voicesPromise;
  voicesPromise = new Promise((resolve) => {
    let done = false, poll = null, timer = null;
    const finish = () => {
      if (done) return;
      done = true; clearInterval(poll); clearTimeout(timer);
      try { s.removeEventListener?.("voiceschanged", check); } catch (_) { /* ignore */ }
      voicesPromise = null;
      resolve(have());
    };
    const check = () => { if (have().length) finish(); };
    try { s.addEventListener?.("voiceschanged", check); } catch (_) { /* ignore */ }
    poll = setInterval(check, 250);
    timer = setTimeout(finish, timeoutMs);
  });
  return voicesPromise;
}

export function pickVoice(voices, lang = (navigator.language || "en-US")) {
  if (!voices || !voices.length) return null;
  const want = String(lang).toLowerCase(), base = want.split("-")[0];
  const score = (v) => {
    const l = String(v.lang || "").toLowerCase().replace("_", "-");
    return (l === want ? 4 : l.startsWith(base) ? 2 : 0) + (v.default ? 1 : 0) + (v.localService ? 0.5 : 0);
  };
  return [...voices].sort((a, b) => score(b) - score(a))[0];
}

// Sentences of a reply, speakable (no markdown, no URLs), each short enough for Android.
export function sentences(text, cap = 700) {
  const plain = speakable(text, cap);
  const parts = plain.match(/[^.!?\n]+[.!?]*\s*/g) || [plain];
  const out = [];
  for (let p of parts) {
    p = p.trim();
    while (p.length > 220) {                     // a run-on: cut at a comma or space
      const at = Math.max(p.lastIndexOf(", ", 200), p.lastIndexOf(" ", 200));
      out.push(p.slice(0, at > 40 ? at + 1 : 200).trim());
      p = p.slice(at > 40 ? at + 1 : 200).trim();
    }
    if (p) out.push(p);
  }
  return out;
}

let gen = 0;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// Say `text` with the phone's voice. Resolves {ok, reason?, spoken}; never rejects. A newer call or stop() ends the old one.
export async function speakBrowser(text, { rate = 1, pitch = 1 } = {}) {
  const s = synth();
  if (!s) return { ok: false, reason: "no-api", spoken: 0 };
  const parts = sentences(text);
  if (!parts.length) return { ok: true, spoken: 0 };
  const mine = ++gen;
  const voices = await loadVoices();
  if (mine !== gen) return { ok: true, spoken: 0 };
  const voice = pickVoice(voices);
  try { s.cancel(); } catch (_) { /* ignore */ }
  await wait(60);                                // Android Chrome drops a speak() that follows cancel() at once
  let spoken = 0;
  for (const part of parts) {
    if (mine !== gen) return { ok: true, spoken };
    const r = await new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(part);
      if (voice) { u.voice = voice; u.lang = voice.lang; } else u.lang = navigator.language || "en-US";
      u.rate = rate; u.pitch = pitch; u.volume = 1;
      let started = false;
      const guard = setTimeout(() => { if (!started) { try { s.cancel(); } catch (_) { /* ignore */ } resolve(voices.length ? "blocked" : "no-voice"); } }, spoken ? 6000 : 5000);
      u.onstart = () => { started = true; clearTimeout(guard); };
      u.onend = () => { clearTimeout(guard); resolve("done"); };
      u.onerror = (e) => {
        clearTimeout(guard);
        const kind = e && e.error;
        resolve(kind === "not-allowed" ? "blocked" : kind === "interrupted" || kind === "canceled" ? "done" : "error");
      };
      try { s.resume?.(); s.speak(u); } catch (_) { clearTimeout(guard); resolve("error"); }
    });
    if (r === "done") { spoken += 1; continue; }
    return { ok: false, reason: r, spoken };
  }
  return { ok: true, spoken };
}

export function stop() {
  gen += 1;
  try { synth()?.cancel(); } catch (_) { /* ignore */ }
  try { stopReply(); } catch (_) { /* ignore */ }
}

// The hub voice profile for this assistant when the laptop makes the audio, else null.
export async function hubProfile(assistant = "jarvis") {
  try {
    const p = profileFor(await loadVoiceStatus(), assistant);
    return p && p.hub ? p : null;
  } catch (_) { return null; }
}

// Speak a reply: hub voice when configured (phone voice as its fallback), else the phone's voice. Respects Voice on/off.
export async function speak(text, { assistant = "jarvis", force = false } = {}) {
  if (!force && !voiceOn()) return { ok: false, reason: "off", spoken: 0 };
  if (!text || !String(text).trim()) return { ok: true, spoken: 0 };
  if (await hubProfile(assistant)) {
    let fell = null;
    await speakReply(text, assistant, (t) => { fell = speakBrowser(t); });
    const r = fell ? await fell : { ok: true, spoken: 1 };
    return { ...r, via: fell ? "phone" : "hub" };
  }
  return { ...(await speakBrowser(text)), via: "phone" };
}

export async function voiceStatus(assistant = "jarvis") {
  const s = synth();
  const voices = s ? await loadVoices(1500) : [];
  const hub = await hubProfile(assistant);
  return { supported: !!s, voices: voices.length, on: voiceOn(), hub: !!hub, engine: hub ? hub.engine : "browser" };
}

// The first tap on the page unlocks speech for the rest of the visit (Android Chrome refuses sound before a gesture).
let unlocked = false;
export function installUnlock() {
  if (unlocked || typeof document === "undefined") return;
  const go = () => {
    if (unlocked) return;
    unlocked = true;
    for (const t of ["pointerdown", "touchend", "keydown"]) document.removeEventListener(t, go, true);
    const s = synth();
    if (!s) return;
    try { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; s.speak(u); } catch (_) { /* ignore */ }
    loadVoices(1500);
  };
  for (const t of ["pointerdown", "touchend", "keydown"]) document.addEventListener(t, go, true);
}
installUnlock();

// "Voice on/off" switch, "Test voice" button and a message line. One per screen: voiceControls({assistant, name}).
export function voiceControls({ assistant = "jarvis", name = "Jarvis" } = {}) {
  const note = el("p", { class: "fk-note", role: "status", "aria-live": "polite" });
  const sw = ui.switchControl({ checked: voiceOn(), label: "Voice", onChange: (on) => { setVoiceOn(on); paint(); } });
  const test = ui.button({ label: "Test voice", icon: "play", variant: "tonal", size: "sm", onClick: async () => {
    note.textContent = "Playing…"; note.classList.remove("bad");
    const r = await speak(`This is ${name}. If you can hear this, my voice works on this phone.`, { assistant, force: true });
    if (r.ok && r.spoken) { note.textContent = r.via === "hub" ? "Played through the laptop's voice." : "Played with this phone's voice."; note.classList.remove("bad"); }
    else { note.textContent = MESSAGES[r.reason] || MESSAGES.error; note.classList.add("bad"); }
  } });
  const node = el("section", { class: "ui-card fk-voice", "aria-label": `${name} voice` },
    el("div", { class: "fk-row" }, el("label", { class: "fk-switch" }, sw, el("span", {}, "Voice on")), test), note);
  async function paint() {
    sw.input.checked = voiceOn();
    const st = await voiceStatus(assistant);
    if (!voiceOn()) { note.textContent = MESSAGES.off; note.classList.remove("bad"); return; }
    if (!st.hub && !st.supported) { note.textContent = MESSAGES["no-api"]; note.classList.add("bad"); }
    else if (!st.hub && st.voices === 0) { note.textContent = MESSAGES["no-voice"]; note.classList.add("bad"); }
    else { note.textContent = st.hub ? `Speaks through the laptop's ${st.engine} voice, this phone's voice as backup.` : `Speaks with this phone's voice (${st.voices} found).`; note.classList.remove("bad"); }
  }
  paint();
  node.refresh = paint;
  return node;
}
