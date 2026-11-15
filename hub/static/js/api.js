// Shared helpers for talking to the hub. Every tab module imports from here.

// The browser turns a null/undefined child of append()/prepend()/replaceChildren() into the TEXT "null"/"undefined"
// (e.g. `foot.replaceChildren(onAdd ? button : null)` printed "null" in Settings > Voices). Every screen builds its
// DOM that way, so skip those children once, here (api.js is imported by every screen module).
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const name of ["append", "prepend", "replaceChildren"]) {
    const native = proto[name];
    if (typeof native !== "function" || native.__skipsEmpty) continue;
    const wrapped = function (...nodes) { return native.apply(this, nodes.filter((n) => n !== null && n !== undefined && n !== false)); };
    wrapped.__skipsEmpty = true;
    proto[name] = wrapped;
  }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function problem(status, body) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return "That request was not valid.";
  return `The laptop answered with an error (${status}).`;
}

// Identical GETs made at the same moment (a screen asking for /api/cards/decks three times while it opens) share one
// request, and an answer is reused for 2 s. Any write clears the lot, so nothing shown is older than the last action.
// Not shared: requests with an abort signal, live endpoints (frames, status of a running thing, streams).
const LIVE = /\/(frame|stream|status|session|ticker|logs?|events?)(\?|\/|$)|^\/api\/(screen|terminal|assistant)\b/;
const RECENT_MS = 2000;
const inflight = new Map();
const recent = new Map();
const copy = (v) => (v && typeof v === "object" ? structuredClone(v) : v);

// api("/api/notes") -> parsed JSON. Options: method, json (object body), form (FormData), signal.
export async function api(path, opts = {}) {
  const { method = "GET", signal } = opts;
  if (method !== "GET") { inflight.clear(); recent.clear(); return rawApi(path, opts); }
  if (signal || LIVE.test(path)) return rawApi(path, opts);
  const hit = recent.get(path);
  if (hit && Date.now() - hit.at < RECENT_MS) return copy(hit.body);
  let p = inflight.get(path);
  if (!p) {
    p = rawApi(path, opts).then((body) => { recent.set(path, { at: Date.now(), body }); return body; })
      .finally(() => inflight.delete(path));
    inflight.set(path, p);
  }
  return copy(await p);
}

async function rawApi(path, { method = "GET", json, form, signal } = {}) {
  // X-Hub marks a request as sent by this app's own code. A plain HTML form cannot add it.
  const init = { method, signal, credentials: "same-origin", headers: { "X-Hub": "1" } };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    init.body = form;
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Can't reach your laptop. Is the hub running and the laptop awake?");
  }
  if (response.status === 401 && path !== "/api/login") {
    window.dispatchEvent(new Event("hub:unauthorized"));
  }
  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const body = isJson ? await response.json() : null;
  if (!response.ok) throw new ApiError(response.status, problem(response.status, body));
  return body;
}

// for await (const event of sse("/api/ask", {...}, signal)) { ... }
// Yields each `data:` payload parsed as JSON; stops at [DONE] or when the stream ends.
export async function* sse(path, json, signal) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST", credentials: "same-origin", signal,
      headers: { "Content-Type": "application/json", "X-Hub": "1" }, body: JSON.stringify(json),
    });
  } catch (error) {
    if (error.name === "AbortError") return;
    throw new ApiError(0, "Can't reach your laptop. Is the hub running and the laptop awake?");
  }
  if (response.status === 401) window.dispatchEvent(new Event("hub:unauthorized"));
  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, problem(response.status, body));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6);
          if (data === "[DONE]") return;
          yield JSON.parse(data);
        }
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") throw error;
  } finally {
    reader.cancel().catch(() => {});
  }
}

// URL for a file served by the hub, for <img src>, links and fetch.
export function fileUrl(root, path, download = false) {
  const q = new URLSearchParams({ root, path });
  if (download) q.set("download", "1");
  return `/api/file?${q}`;
}

// Turn markdown into safe HTML. Notes come from files, so they are never trusted.
export function renderMarkdown(text) {
  const html = window.marked.parse(text, { gfm: true, breaks: false });
  // Forms and buttons are removed: injected text could otherwise put an invisible full-screen form
  // on the page that posts to the hub's own routes. Inline styles go too (they hide such traps).
  return window.DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["form", "button", "input", "textarea", "select", "option", "style", "link", "meta"],
    FORBID_ATTR: ["style", "formaction", "form"],
  });
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}
