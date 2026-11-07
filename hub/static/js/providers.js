// Which AI providers this laptop can use, shared by every screen that has a model picker.
// Values: "local" | "claude" (Anthropic API key) | "claude-login" (the `claude` program, your Claude login) | "gemini".
// The list comes from /api/status (providers + default_provider) and is cached for a minute.
import { api } from "./api.js";
import * as ui from "./ui.js";

const IDS = ["local", "claude", "claude-login", "gemini"];
const SHORT = { local: "Local", claude: "Claude", "claude-login": "Claude login", gemini: "Gemini" };
const LONG = { local: "Local model", claude: "Claude (API key)", "claude-login": "Claude (my login)", gemini: "Gemini" };
const listeners = new Set();
let info = null;
let loadedAt = 0;
let pending = null;

export const isProvider = (v) => IDS.includes(v);
export const providerLabel = (id, long = false) => (long ? LONG : SHORT)[id] || SHORT.local;
export const normalizeProvider = (v) => (isProvider(v) ? v : "local");

export function loadProviders(force = false) {
  if (!force && info && Date.now() - loadedAt < 60000) return Promise.resolve(info);
  if (!pending) {
    pending = api("/api/status").then((s) => {
      info = { providers: s?.providers || null, default: s?.default_provider || "local" };
      loadedAt = Date.now();
      listeners.forEach((fn) => { try { fn(); } catch (_) { /* a closed screen */ } });
      return info;
    }).catch(() => info).finally(() => { pending = null; });
  }
  return pending;
}

// The provider to start with when nothing was chosen: the hub's default (auto -> what is ready).
export const defaultProvider = () => normalizeProvider(info?.default);

function ready(id) {
  const p = info?.providers;
  if (!p) return id === "local" || id === "claude";   // not loaded yet: the two that always existed
  return !!p[id.replace("-", "_")]?.ready;
}

// Choices to show: Local, plus every cloud AI that is ready (or the one in use). If none is ready, plain "Claude".
export function providerOptions({ current, icons = false, localLabel } = {}) {
  const clouds = ["claude", "claude-login", "gemini"];
  let show = clouds.filter((id) => ready(id) || id === current);
  if (!show.length) show = ["claude"];
  const label = (id) => (id === "local" && localLabel ? localLabel : SHORT[id]);
  const icon = { local: "laptop", claude: "sparkles", "claude-login": "sparkles", gemini: "sparkles" };
  return ["local", ...show].map((id) => ({ value: id, label: label(id), ...(icons ? { icon: icon[id] } : {}) }));
}

// ui.segmented for the provider choice. It refreshes its options when the list arrives.
export function providerSegmented(opts = {}) {
  const { value, onChange, label = "Model", block = false, icons = false, localLabel } = opts;
  const seg = ui.segmented({ options: providerOptions({ current: value, icons, localLabel }), value, onChange, label, block });
  let seen = false;
  const refresh = () => {
    if (seg.isConnected) seen = true; else if (seen) { listeners.delete(refresh); return; }
    seg.setOptions(providerOptions({ current: seg.value, icons, localLabel }));
  };
  listeners.add(refresh);
  loadProviders();
  return seg;
}
