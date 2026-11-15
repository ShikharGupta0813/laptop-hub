// Which AI providers this laptop can use, shared by every screen that has a model picker.
// Values: "local" | "claude" (Anthropic API key) | "claude-login" (the `claude` program, your Claude login) | "gemini" | "duo" (Claude plans and checks, Gemini does the work).
// The list comes from /api/status (providers + default_provider) and is cached for a minute.
import { api } from "./api.js";
import * as ui from "./ui.js";

const IDS = ["local", "claude", "claude-login", "gemini", "duo"];
const SHORT = { local: "Local", claude: "Claude", "claude-login": "Claude login", gemini: "Gemini", duo: "Claude+Gemini" };
const LONG = { local: "Local model", claude: "Claude (API key)", "claude-login": "Claude (my login)", gemini: "Gemini", duo: "Claude + Gemini" };
const listeners = new Set();
let info = null;
let loadedAt = 0;
let pending = null;

const cliEntry = (id) => (typeof id === "string" && id.startsWith("cli:") ? (info?.cli || []).find((c) => `cli:${c.id}` === id) : null);
export const isProvider = (v) => IDS.includes(v) || (typeof v === "string" && v.startsWith("cli:"));
const LANE_LABEL = { "lane:council": "Council", "lane:local-reasoning": "Local deep thinking" };
export const providerLabel = (id, long = false) => (long ? LONG : SHORT)[id] || cliEntry(id)?.label || (id === "auto" ? "Auto" : LANE_LABEL[id]) || SHORT.local;
export const normalizeProvider = (v) => (isProvider(v) ? v : "local");

export function loadProviders(force = false) {
  if (!force && info && Date.now() - loadedAt < 60000) return Promise.resolve(info);
  if (!pending) {
    pending = api("/api/status").then((s) => {
      info = { providers: s?.providers || null, default: s?.default_provider || "local", cli: Array.isArray(s?.cli) ? s.cli : [] };
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
  const clouds = ["claude", "claude-login", "gemini", "duo"];
  let show = clouds.filter((id) => ready(id) || id === current);
  if (!show.length) show = ["claude"];
  const label = (id) => (id === "local" && localLabel ? localLabel : SHORT[id]);
  const icon = { local: "laptop", claude: "sparkles", "claude-login": "sparkles", gemini: "sparkles", duo: "sparkles" };
  const logins = (info?.cli || []).filter((c) => c.ready || `cli:${c.id}` === current).map((c) => ({ value: `cli:${c.id}`, label: c.label, ...(icons ? { icon: "bot" } : {}) }));
  return [...["local", ...show].map((id) => ({ value: id, label: label(id), ...(icons ? { icon: icon[id] } : {}) })), ...logins];
}

// Every real provider with whether it can answer now and why not: [{id, label, ready, reason}]. Local first, then the
// clouds, then the CLI logins (Antigravity...). Not-ready ones are returned too (the "AI for this chat" sheet greys them).
const WHY = { claude: "No Anthropic API key is saved.", "claude-login": "Claude Code isn't installed or logged in.", gemini: "No Gemini key is saved.", duo: "Needs a Gemini key." };
export function providerChoices() {
  const p = info?.providers;
  const row = (id) => {
    const r = p?.[id.replace("-", "_")];
    return { id, label: id === "local" ? "Local model" : id === "claude-login" ? "Claude (login)" : SHORT[id], ready: ready(id),
      reason: (r?.reason || (id === "local" ? "Runs on this laptop." : WHY[id]) || "") };
  };
  const cli = (info?.cli || []).map((c) => ({ id: `cli:${c.id}`, label: c.label, ready: !!c.ready, reason: c.reason || "" }));
  return [row("local"), ...["claude", "claude-login", "gemini", "duo"].map(row), ...cli];
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
