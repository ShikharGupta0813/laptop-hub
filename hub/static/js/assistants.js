// The owner's assistants list (GET /api/assistants), cached, with Jarvis and Friday as the fallback.
// Used by the wake word (wake.js) so a renamed or added assistant wakes the app without a code change.
import { api } from "./api.js";

export const FALLBACK = [
  { id: "jarvis", name: "Jarvis", wake_names: ["jarvis"], heard_as: ["jarves", "jarvas"], accent: "cyan", enabled: true },
  { id: "friday", name: "Friday", wake_names: ["friday"], heard_as: [], accent: "amber", enabled: true },
];
const TTL_MS = 5 * 60 * 1000;
let cache = null, at = 0, pending = null;

// The cached list right now (never waits). Use refreshAssistants() to load it.
export function cachedAssistants() { return cache || FALLBACK; }
// The list as the hub sent it, or null before it did (callers then use their own Jarvis and Friday rules, so a custom jarvis_name keeps working).
export function loadedAssistants() { return cache; }

export function refreshAssistants(force = false) {
  if (!force && cache && Date.now() - at < TTL_MS) return Promise.resolve(cache);
  if (pending) return pending;
  pending = api("/api/assistants").then((d) => {
    const list = Array.isArray(d?.assistants) && d.assistants.length ? d.assistants : FALLBACK;
    cache = list; at = Date.now();
    window.dispatchEvent(new CustomEvent("hub:assistants", { detail: { assistants: list } }));
    return list;
  }).catch(() => cache || FALLBACK).finally(() => { pending = null; });
  return pending;
}

// Settings calls this after an edit so the wake word picks the change up at once.
export function forgetAssistants() { cache = null; at = 0; }

export function assistantById(id) { return cachedAssistants().find((a) => a.id === id) || null; }
