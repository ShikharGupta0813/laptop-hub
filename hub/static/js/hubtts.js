// Browser side of the hub's local text to speech (hub/tts.py, docs/api/tts.md): ask for audio, play it, stop it.
// Used by voice mode (voicemode.js, through speechqueue.js) and by the Talk tab's "Read answers aloud".
// Nothing here is needed unless the owner picked a hub voice in Settings > Voices: with the default ("This phone's
// voice") every call answers {kind: "browser"} and the phone's own speechSynthesis speaks, exactly as before.
import { api } from "./api.js";

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

// One sentence to audio. {kind:"audio", data: Blob} | {kind:"browser"} | {kind:"silent"} (panic: say nothing).
export async function hubSynth(text, { assistant = "", signal } = {}) {
  let r;
  try {
    r = await fetch("/api/tts", {
      method: "POST", credentials: "same-origin", signal,
      headers: { "Content-Type": "application/json", "X-Hub": "1" }, body: JSON.stringify({ text, assistant: assistant || undefined }),
    });
  } catch (_) { return { kind: "browser" }; }
  if (r.status === 423) return { kind: "silent" };
  if (r.status !== 200) return { kind: "browser" };
  const blob = await r.blob();
  return blob.size > 44 ? { kind: "audio", data: blob } : { kind: "browser" };
}

// Plays one Blob at a time on an <audio> element; stop() ends it at once. onStart/onPulse are optional hooks.
export function createAudioPlayer({ onStart = () => {}, onPulse = () => {} } = {}) {
  let cur = null;
  const end = () => {
    if (!cur) return;
    const c = cur; cur = null;
    try { c.audio.pause(); } catch (_) { /* ignore */ }
    try { URL.revokeObjectURL(c.url); } catch (_) { /* ignore */ }
    c.resolve();
  };
  return {
    play(blob) {
      return new Promise((resolve) => {
        end();
        let url = "", audio;
        try { url = URL.createObjectURL(blob); audio = new Audio(url); } catch (_) { resolve(); return; }
        cur = { audio, url, resolve };
        audio.onended = end;
        audio.onerror = end;
        audio.ontimeupdate = () => onPulse();
        const started = audio.play();
        if (started && started.then) started.then(() => onStart(), end); else onStart();
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
    if (mine !== talk.gen || r.kind === "silent") return;
    if (r.kind === "audio") await talk.player.play(r.data); else fallback?.(sentence);
  }
}
export function stopReply() { talk.gen += 1; talk.player?.stop(); }
