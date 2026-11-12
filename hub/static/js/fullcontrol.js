// Full Control mode: the header chip, and the typed-confirmation sheet used before turning it ON.
// ON needs the literal text "I UNDERSTAND" (the hub checks it too). OFF is one tap, with no confirmation.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

export const PHRASE = "I UNDERSTAND";
export const WARNING = "Full Control runs every command and file change the assistant or Friday decides on, "
  + "straight away, with no approval step. Anyone holding your unlocked phone with this app open controls your laptop.";

const state = { on: false, chip: null, listeners: new Set() };

function ensureStyle() {
  if (document.getElementById("fc-style")) return;
  const style = document.createElement("style");
  style.id = "fc-style";
  style.textContent = `.fc-chip{display:inline-flex;align-items:center;gap:4px;margin-left:8px;padding:2px 8px;border:1px solid var(--c-bad,#e5484d);
    border-radius:999px;background:transparent;color:var(--c-bad,#e5484d);font:600 11px/1.6 inherit;letter-spacing:.04em;cursor:pointer}
    .fc-chip[hidden]{display:none}
    .fc-chip--float{position:fixed;top:calc(env(safe-area-inset-top,0px) + 6px);right:8px;z-index:50;background:var(--c-bg,#000)}
    .fc-input{width:100%;box-sizing:border-box;padding:12px;font:inherit;border-radius:10px;border:1px solid var(--c-line-strong,#888);background:var(--c-bg-2,transparent);color:inherit}`;
  document.head.append(style);
}

function mountChip() {
  ensureStyle();
  if (!state.chip) {
    state.chip = el("button", { type: "button", class: "fc-chip", "aria-label": "Full Control is on. Tap to turn it off.", title: "Full Control is on: nothing asks first. Tap to turn off.",
      onclick: () => turnOff() }, "FULL CONTROL");
  }
  const host = document.querySelector(".sh-bar .sh-title");
  if (host) { state.chip.classList.remove("fc-chip--float"); if (state.chip.parentNode !== host) host.append(state.chip); }
  else { state.chip.classList.add("fc-chip--float"); if (!state.chip.isConnected) document.body.append(state.chip); }
  state.chip.hidden = !state.on;
}

export function isOn() { return state.on; }
export function onChange(fn) { state.listeners.add(fn); return () => state.listeners.delete(fn); }
export function apply(on) {
  state.on = on === true;
  mountChip();
  for (const fn of state.listeners) { try { fn(state.on); } catch (_) { /* a view is gone */ } }
}

// Ask the hub; keeps the chip right on every screen. Never throws.
export async function refresh() {
  try { const info = await api("/api/assistant/info"); apply(info && info.full_control === true); return info; }
  catch (_) { return null; }
}

export async function turnOff() {
  try { await api("/api/settings", { method: "PATCH", json: { full_control: false } }); apply(false); ui.toast("Full Control is off. Approvals are back.", { kind: "good" }); return true; }
  catch (error) { ui.toast(`Could not turn it off: ${error.message}`, { kind: "bad" }); return false; }
}

// The typed-confirmation sheet. Resolves true once the hub accepted it, false if cancelled or refused.
export function turnOn() {
  return new Promise((resolve) => {
    const input = el("input", { class: "fc-input", type: "text", autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
      "aria-label": `Type ${PHRASE} to confirm`, placeholder: PHRASE });
    const msg = el("p", { class: "muted", role: "status" });
    let sheet = null, finished = false;
    const done = (v) => { if (!finished) { finished = true; resolve(v); } };
    sheet = ui.sheet({ title: "Turn on Full Control?", class: "fc-sheet", onClose: () => done(false),
      content: [el("p", {}, WARNING), el("p", {}, `To switch it on, type ${PHRASE} below. You can switch it off again with one tap.`), input, msg],
      actions: [
        { label: "Cancel", variant: "secondary", onClick: () => { done(false); } },
        { label: "Turn on", variant: "danger", onClick: async () => {
          if (input.value.trim() !== PHRASE) { msg.textContent = `Type exactly ${PHRASE} to continue.`; return false; }
          try {
            await api("/api/settings", { method: "PATCH", json: { full_control: true, full_control_confirm: input.value.trim() } });
            apply(true); ui.toast("Full Control is on.", { kind: "warn" }); done(true);
          } catch (error) { msg.textContent = error.message; return false; }
        } },
      ] });
    setTimeout(() => input.focus(), 50);
  });
}
