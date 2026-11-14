// Panic: the always-reachable red button in the header, the "Agent paused" banner with Resume, and a 10 second undo.
// Routes: docs/api/panic.md. One tap works in an emergency; `panic_confirm` (Settings) adds an "are you sure" step.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/panic.css";
const state = { active: false, paused: [], errors: [], confirm: false, btn: null, banner: null };

function ensureCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

export function current() { return state; }

function paint() {
  if (state.btn) { state.btn.classList.toggle("is-active", state.active); state.btn.setAttribute("aria-pressed", String(state.active)); }
  const b = state.banner;
  if (!b) return;
  b.hidden = !state.active;
  if (!state.active) { b.replaceChildren(); return; }
  const items = (state.paused || []).map((p) => el("li", {}, p.detail ? `${p.name}: ${p.detail}` : p.name));
  const errs = (state.errors || []).map((e) => el("li", {}, `${e.name} could not be stopped: ${e.error}`));
  b.replaceChildren(
    el("div", { class: "pn-banner__head" }, el("p", { class: "pn-banner__title" }, "Agent paused"),
      ui.button({ label: "Resume", icon: "play", variant: "primary", size: "sm", onClick: () => resume() })),
    items.length || errs.length ? el("ul", {}, ...items, ...errs) : el("p", { class: "ui-hint" }, "Nothing was running. Writes and sends are blocked until you resume."));
}

export async function refresh() {
  try { Object.assign(state, await api("/api/panic")); } catch (_) { /* old hub, or logged out */ }
  paint();
  return state;
}

export async function resume() {
  try {
    const r = await api("/api/panic/resume", { method: "POST" });
    Object.assign(state, r); paint();
    ui.toast(r.resumed && r.resumed.length ? `Resumed: ${r.resumed.join(", ")}. Full Control stays off.` : "Resumed. Full Control stays off.", { kind: "good" });
  } catch (error) { ui.toast(error.message || "Could not resume.", { kind: "bad" }); }
}

export async function trigger(source = "button") {
  try {
    Object.assign(state, await api("/api/panic", { method: "POST", json: { source } }));
    paint();
    ui.toast("Agent paused. Everything is stopped.", { kind: "warn", ms: 10000, action: { label: "Undo", onClick: () => resume() } });
  } catch (error) { ui.toast(error.message || "Panic failed. Try again.", { kind: "bad" }); }
}

export async function press() {
  if (state.active) { ui.toast("The agent is already paused.", { kind: "info" }); return; }
  if (state.confirm && !await ui.confirm({ title: "Stop the agent everywhere?", message: "Every running turn, approval, listener and background job stops until you tap Resume.", confirmLabel: "Panic", danger: true })) return;
  ui.haptic("warn");
  await trigger("button");
}

export function mountPanic(ctx, { bar } = {}) {
  ensureCss();
  state.btn = el("button", { type: "button", class: "sh-iconbtn pn-btn", "aria-label": "Panic: stop the agent everywhere", "data-tip": "Panic", onclick: () => press() }, ui.icon("stop"));
  (bar || document).querySelector(".sh-actions")?.prepend(state.btn);
  state.banner = el("div", { class: "pn-banner", role: "alert", hidden: true });
  document.body.append(state.banner);
  refresh();
  setInterval(refresh, 30000);
}
