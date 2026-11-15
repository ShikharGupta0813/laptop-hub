// The one background poller of the app shell. It asks GET /api/ticker (panic, unread count, urgent alerts, spoken
// items, meeting status in one small answer) on a single timer, sends If-None-Match so an unchanged answer is an
// empty 304, stops while the page is hidden, and slows down away from Home. Modules subscribe instead of starting
// their own timers: subscribe((data) => ...) is called after every reply that changed.
const FAST_MS = 15000;   // Home
const SLOW_MS = 30000;   // every other screen
let timer = null;
let etag = "";
let last = null;
let started = false;
let onHome = () => true;
let dead = false;
const subs = new Set();

export function subscribe(fn) { subs.add(fn); if (last) { try { fn(last); } catch (_) { /* keep going */ } } return () => subs.delete(fn); }
export function latest() { return last; }

async function once() {
  if (dead) return;
  try {
    const r = await fetch("/api/ticker", { credentials: "same-origin", headers: { "X-Hub": "1", ...(etag ? { "If-None-Match": etag } : {}) } });
    if (r.status === 304) return;
    if (r.status === 401) { window.dispatchEvent(new Event("hub:unauthorized")); return; }
    if (r.status === 404 || r.status === 405) { dead = true; return; }   // an older hub: callers keep their own fallbacks
    if (!r.ok) return;
    etag = r.headers.get("ETag") || "";
    last = await r.json();
    for (const fn of subs) { try { fn(last); } catch (_) { /* one subscriber must not stop the rest */ } }
  } catch (_) { /* hub asleep: try again next tick */ }
}

function schedule() {
  clearTimeout(timer);
  timer = null;
  if (document.hidden || dead) return;
  timer = setTimeout(async () => { await once(); schedule(); }, onHome() ? FAST_MS : SLOW_MS);
}

// nudge(): ask now (after the owner did something that changes a count), then carry on.
export async function nudge() { etag = ""; await once(); schedule(); }

export function startPoller({ isHome } = {}) {
  if (started) return;
  started = true;
  if (isHome) onHome = isHome;
  document.addEventListener("visibilitychange", () => { if (document.hidden) { clearTimeout(timer); timer = null; } else nudge(); });
  window.addEventListener("hashchange", schedule);
  nudge();
}
