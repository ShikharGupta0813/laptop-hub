// The assistant wheel: a floating orb bottom-right (above the tab bar) that fans out a quarter-circle of five
// shortcuts. Tap the orb to open, tap a shortcut to go, tap anywhere else or press Esc to close. It lives inside
// #views, so the bottom tab bar is never covered. On by default; Settings > Appearance can turn it off
// (localStorage "hub.wheel" = "0"). Views with their own bottom-right controls hide it via css/wheel.css.
import { el } from "./api.js";
import * as ui from "./ui.js";

export const WHEEL_ITEMS = [
  { id: "assistant", label: "Talk", icon: "talk" },
  { id: "apps", label: "Apps", icon: "apps" },
  { id: "files", label: "Files", icon: "folder" },
  { id: "terminal", label: "Terminal", icon: "terminal" },
  { id: "screen", label: "Screen", icon: "cast" },
];
const KEY = "hub.wheel";
const RADIUS = 150;   // px from the orb's centre; five 58px nodes 22.5deg apart stay 58px+ away from each other

export function wheelEnabled() {
  try { return localStorage.getItem(KEY) !== "0"; } catch (_) { return true; }
}
export function setWheelEnabled(on) {
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (_) { /* private mode */ }
  window.dispatchEvent(new CustomEvent("hub:wheel", { detail: { on: !!on } }));
}

// host: the element to mount in (#views). ctx: the shell's toolkit (ctx.open).
export function mountWheel(host, ctx) {
  if (!host || host.querySelector(".wl")) return null;
  let open = false;
  const items = WHEEL_ITEMS.map((it, i) => {
    const theta = (90 + i * 22.5) * Math.PI / 180;   // from straight up, sweeping to straight left
    const dx = Math.round(RADIUS * Math.cos(theta)), dy = Math.round(-RADIUS * Math.sin(theta));
    return el("button", { type: "button", class: "wl-item", role: "menuitem", tabindex: "-1", "data-view": it.id,
      style: `--dx:${dx}px;--dy:${dy}px;--i:${i}`, "aria-label": it.label,
      onclick: () => { ui.haptic("light"); close(false); ctx.open(it.id); } },
    ui.icon(it.icon), el("span", { class: "wl-label" }, it.label));
  });
  const orb = el("button", { type: "button", class: "wl-orb", "aria-label": "Assistant wheel", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: () => (open ? close(true) : show()) },
  el("span", { class: "wl-orb__core", "aria-hidden": "true" }), el("span", { class: "wl-orb__icon", "aria-hidden": "true" }, ui.icon("sparkles")));
  const fan = el("div", { class: "wl-fan", role: "menu", "aria-label": "Shortcuts" }, ...items);
  const scrim = el("div", { class: "wl-scrim", "aria-hidden": "true", onclick: () => close(true) });
  const root = el("div", { class: "wl", "data-open": "false" }, scrim, fan, orb);
  host.append(root);

  function show() {
    open = true; ui.haptic("select");
    root.dataset.open = "true"; orb.setAttribute("aria-expanded", "true");
    orb.querySelector(".wl-orb__icon").replaceChildren(ui.icon("x"));
    items.forEach((b) => b.removeAttribute("tabindex"));
    document.addEventListener("keydown", onKey, true);
    setTimeout(() => items[0]?.focus({ preventScroll: true }), 60);
  }
  function close(refocus) {
    if (!open) return;
    open = false;
    root.dataset.open = "false"; orb.setAttribute("aria-expanded", "false");
    orb.querySelector(".wl-orb__icon").replaceChildren(ui.icon("sparkles"));
    items.forEach((b) => b.setAttribute("tabindex", "-1"));
    document.removeEventListener("keydown", onKey, true);
    if (refocus) orb.focus({ preventScroll: true });
  }
  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
    if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === "ArrowLeft" || e.key === "ArrowUp") {
      const at = items.indexOf(document.activeElement);
      const step = (e.key === "ArrowRight" || e.key === "ArrowDown") ? -1 : 1;
      const next = items[(Math.max(0, at) + step + items.length) % items.length];
      if (next) { e.preventDefault(); next.focus({ preventScroll: true }); }
    }
  }
  const sync = () => { const on = wheelEnabled(); root.hidden = !on; if (!on) close(false); };
  window.addEventListener("hub:wheel", sync);
  window.addEventListener("hashchange", () => close(false));
  sync();
  return { close: () => close(false), isOpen: () => open };
}
