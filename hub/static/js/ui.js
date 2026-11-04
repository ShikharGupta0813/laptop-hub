// Shared UI components for every screen. Pair with the `ui-` classes in css/design.css; see docs/DESIGN.md.
//
//   import { icon, sheet, toast, confirm, skeleton, emptyState, ringProgress, segmented, haptic } from "./ui.js";
//
// Everything here builds DOM with createElement/textContent, so it is safe for text from files or models.
// (Only icon() uses innerHTML, and only with the constant markup in icons.js.)
import { el } from "./api.js";
import { ICONS, ICON_NAMES } from "./icons.js";

export { el, ICON_NAMES };

const SVG_NS = "http://www.w3.org/2000/svg";
const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  || document.documentElement.dataset.motion === "reduce";

// ---- icons -------------------------------------------------------------------------------------
// icon("bell") | icon("bell", 20) | icon("bell", {size: 20, label: "Notifications", class: "x"})
export function icon(name, opts = {}) {
  if (typeof opts === "number") opts = { size: opts };
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "ui-icon" + (opts.class ? " " + opts.class : ""));
  svg.setAttribute("focusable", "false");
  if (opts.size) { svg.setAttribute("width", String(opts.size)); svg.setAttribute("height", String(opts.size)); }
  if (opts.label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", opts.label); }
  else svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || ICONS.circle; // constant markup from icons.js only
  return svg;
}

// ---- small builders ----------------------------------------------------------------------------
// button({label, icon, variant: "primary"|"secondary"|"tonal"|"ghost"|"danger", size: "sm"|"lg", block, onClick, ...attrs})
export function button({ label, icon: ic, variant = "secondary", size, block, iconOnly, onClick, class: cls, ...attrs } = {}) {
  const classes = ["ui-btn", `ui-btn--${variant}`];
  if (size) classes.push(`ui-btn--${size}`);
  if (block) classes.push("ui-btn--block");
  if (iconOnly) classes.push("ui-btn--icon");
  if (cls) classes.push(cls);
  const node = el("button", { type: "button", class: classes.join(" "), ...attrs },
    ic ? icon(ic) : null, iconOnly ? null : label);
  if (iconOnly && label && !attrs["aria-label"]) node.setAttribute("aria-label", label);
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

// Show a spinner in a button while a promise runs. Returns the promise's value.
export async function busy(btn, promise) {
  btn.classList.add("is-loading");
  btn.setAttribute("aria-busy", "true");
  try { return await (typeof promise === "function" ? promise() : promise); }
  finally { btn.classList.remove("is-loading"); btn.removeAttribute("aria-busy"); }
}

export function badge(text, tone) {
  return el("span", { class: "ui-badge" + (tone ? ` ui-badge--${tone}` : "") }, text);
}

// A rounded tinted square holding an icon. tone: accent|good|warn|bad|info|violet|pink|teal|orange|gray
export function tile(name, tone = "accent", size) {
  return el("span", { class: "ui-tile" + (size ? ` ui-tile--${size}` : ""), "data-tone": tone }, icon(name));
}

function hashHue(text) {
  let h = 0;
  for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}
export function avatar(name = "?", { size, src } = {}) {
  const initials = String(name).trim().split(/\s+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase() || "?";
  const node = el("span", { class: "ui-avatar" + (size ? ` ui-avatar--${size}` : ""), "aria-hidden": "true" });
  node.style.setProperty("--_h", String(hashHue(name)));
  if (src) node.append(el("img", { src, alt: "" }));
  else node.textContent = initials;
  return node;
}

// listRow({icon, tone, title, sub, meta, chevron, onClick}) -> <button class="ui-row"> or <div>
export function listRow({ icon: ic, tone = "accent", lead, title, sub, meta, chevron, onClick, attrs = {} } = {}) {
  const tag = onClick ? "button" : "div";
  const node = el(tag, { class: "ui-row" + (onClick ? " ui-row--interactive" : ""), ...(onClick ? { type: "button" } : {}), ...attrs },
    lead || (ic ? tile(ic, tone) : null),
    el("span", { class: "ui-row__body" },
      el("span", { class: "ui-row__title" }, title),
      sub ? el("span", { class: "ui-row__sub" }, sub) : null),
    meta != null ? el("span", { class: "ui-row__meta" }, meta) : null,
    chevron ? icon("chevron-right", { class: "ui-row__chev" }) : null);
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

// ---- haptics -----------------------------------------------------------------------------------
const HAPTICS = { light: 8, medium: 14, heavy: 24, select: 5, success: [10, 40, 14], warn: [18, 60, 18], error: [30, 50, 30, 50, 30] };
export function haptic(kind = "light") {
  try {
    if (localStorage.getItem("hub.haptics") === "off") return;
    navigator.vibrate?.(HAPTICS[kind] ?? HAPTICS.light);
  } catch (_) { /* unsupported */ }
}

// ---- layers: sheets, modals and overlays close on the Android back button ------------------------
const layers = [];
let ignorePops = 0;
let settleWaiters = [];

function settle() {
  if (ignorePops > 0) return;
  const waiting = settleWaiters;
  settleWaiters = [];
  waiting.forEach((fn) => fn());
}
// Resolves once history has caught up with closed layers. main.js awaits this before navigating.
export function historySettled() {
  if (ignorePops === 0) return Promise.resolve();
  return new Promise((resolve) => {
    settleWaiters.push(resolve);
    setTimeout(() => { ignorePops = 0; settle(); }, 400);
  });
}

// pushLayer(close) registers something closable. Returns release(): call it when the layer closes
// itself (button, scrim, swipe), and it rewinds the history entry it added.
export function pushLayer(close) {
  const entry = { close };
  layers.push(entry);
  try { history.pushState({ ...(history.state || {}), layer: layers.length }, ""); } catch (_) { /* sandboxed */ }
  return function release() {
    const i = layers.indexOf(entry);
    if (i < 0) return;
    layers.splice(i, 1);
    ignorePops += 1;
    history.back();
  };
}
export function openLayers() { return layers.length; }

window.addEventListener("popstate", (event) => {
  if (ignorePops > 0) {
    ignorePops -= 1;
    event.stopImmediatePropagation();
    settle();
    return;
  }
  if (layers.length) {
    const top = layers.pop();
    event.stopImmediatePropagation();
    top.close({ fromHistory: true });
  }
});

function trapFocus(container, event) {
  if (event.key !== "Tab") return;
  const items = [...container.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
    .filter((n) => !n.disabled && n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); }
  else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
}

function leave(node, ms = 260) {
  if (!node.isConnected) return;
  if (reducedMotion()) { node.remove(); return; }
  node.classList.add("is-leaving");
  const done = () => node.remove();
  node.addEventListener("animationend", done, { once: true });
  setTimeout(done, ms + 80);
}

// ---- bottom sheet -------------------------------------------------------------------------------
// const s = sheet({ title, content, actions: [{label, variant, icon, onClick(s) -> false keeps it open}], onClose })
// s.body is the scrolling area; s.close(); s.setTitle(text); s.el is the sheet element.
export function sheet({ title = "", content, actions = [], onClose, dismissible = true, label, class: cls } = {}) {
  const previous = document.activeElement;
  const titleId = "ui-sheet-t-" + Math.random().toString(36).slice(2, 8);
  const titleNode = el("h2", { class: "ui-sheet__title", id: titleId }, title);
  const closeBtn = dismissible ? button({ label: "Close", icon: "close", variant: "ghost", iconOnly: true }) : null;
  const body = el("div", { class: "ui-sheet__body" });
  if (content != null) body.append(...(Array.isArray(content) ? content : [content]).map((c) => (c.nodeType ? c : document.createTextNode(String(c)))));
  const foot = el("div", { class: "ui-sheet__foot" });
  const grab = el("div", { class: "ui-sheet__grab", "aria-hidden": "true" });
  const head = el("div", { class: "ui-sheet__head" }, titleNode, closeBtn);
  const node = el("div", { class: "ui-sheet" + (cls ? " " + cls : ""), role: "dialog", "aria-modal": "true",
    ...(title ? { "aria-labelledby": titleId } : { "aria-label": label || "Panel" }) }, grab, head, body);
  const scrim = el("div", { class: "ui-scrim" });
  let closed = false;
  let release = null;

  const api = {
    el: node, body, foot,
    setTitle(text) { titleNode.textContent = text; },
    setActions(list) {
      foot.replaceChildren(...list.map((a) => button({ ...a, onClick: async () => { if ((await a.onClick?.(api)) !== false) api.close(); } })));
      if (list.length && !foot.isConnected) node.append(foot);
      if (!list.length) foot.remove();
    },
    close(opts = {}) {
      if (closed) return;
      closed = true;
      if (!opts.fromHistory && release) release();
      document.removeEventListener("keydown", onKey, true);
      leave(node, 380);
      leave(scrim);
      if (previous && previous.focus && previous.isConnected) previous.focus({ preventScroll: true });
      onClose?.();
    },
  };
  function onKey(event) {
    if (event.key === "Escape" && dismissible) { event.preventDefault(); api.close(); }
    else trapFocus(node, event);
  }
  closeBtn?.addEventListener("click", () => api.close());
  if (dismissible) scrim.addEventListener("click", () => api.close());
  if (actions.length) api.setActions(actions);

  // Drag down to dismiss (on the handle or the header).
  if (dismissible) {
    let startY = 0, dy = 0, t0 = 0, dragging = false;
    const down = (e) => { dragging = true; startY = e.clientY; dy = 0; t0 = performance.now(); node.classList.add("is-dragging"); e.currentTarget.setPointerCapture?.(e.pointerId); };
    const move = (e) => {
      if (!dragging) return;
      dy = Math.max(0, e.clientY - startY);
      node.style.transform = `translateY(${dy}px)`;
      scrim.style.opacity = String(Math.max(0.2, 1 - dy / 400));
    };
    const up = () => {
      if (!dragging) return;
      dragging = false;
      node.classList.remove("is-dragging");
      const velocity = dy / Math.max(1, performance.now() - t0);
      if (dy > 110 || velocity > 0.6) { node.style.transform = ""; api.close(); return; }
      node.style.transition = "transform 240ms cubic-bezier(.22,1,.36,1)";
      node.style.transform = "";
      scrim.style.opacity = "";
      setTimeout(() => { node.style.transition = ""; }, 260);
    };
    for (const zone of [grab, head]) {
      zone.addEventListener("pointerdown", (e) => { if (e.target.closest("button")) return; down(e); });
      zone.addEventListener("pointermove", move);
      zone.addEventListener("pointerup", up);
      zone.addEventListener("pointercancel", up);
    }
  }

  document.body.append(scrim, node);
  document.addEventListener("keydown", onKey, true);
  release = pushLayer(() => api.close({ fromHistory: true }));
  haptic("select");
  requestAnimationFrame(() => {
    const first = body.querySelector("input, textarea, select") || closeBtn || node;
    if (first === node) node.setAttribute("tabindex", "-1");
    first.focus({ preventScroll: true });
  });
  return api;
}

// ---- confirm dialog -----------------------------------------------------------------------------
// if (await confirm({title: "Shut down?", message: "...", confirmLabel: "Shut down", danger: true})) ...
export function confirm({ title = "Are you sure?", message = "", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false, icon: ic } = {}) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const titleId = "ui-modal-t-" + Math.random().toString(36).slice(2, 8);
    const ok = button({ label: confirmLabel, variant: danger ? "danger" : "primary" });
    const cancel = button({ label: cancelLabel, variant: "secondary" });
    const node = el("div", { class: "ui-modal", role: "alertdialog", "aria-modal": "true", "aria-labelledby": titleId },
      el("div", { class: "ui-modal__icon" }, tile(ic || (danger ? "alert" : "help"), danger ? "bad" : "accent", "lg")),
      el("h2", { class: "ui-modal__title", id: titleId }, title),
      message ? el("p", { class: "ui-modal__body" }, message) : null,
      el("div", { class: "ui-modal__actions" }, cancel, ok));
    const scrim = el("div", { class: "ui-scrim" });
    let done = false;
    let release = null;
    const finish = (value, fromHistory) => {
      if (done) return;
      done = true;
      if (!fromHistory && release) release();
      document.removeEventListener("keydown", onKey, true);
      leave(node, 160); leave(scrim);
      previous?.focus?.({ preventScroll: true });
      resolve(value);
    };
    function onKey(event) {
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      else trapFocus(node, event);
    }
    ok.addEventListener("click", () => { haptic(danger ? "warn" : "medium"); finish(true); });
    cancel.addEventListener("click", () => finish(false));
    scrim.addEventListener("click", () => finish(false));
    document.body.append(scrim, node);
    document.addEventListener("keydown", onKey, true);
    release = pushLayer(() => finish(false, true));
    haptic(danger ? "warn" : "select");
    requestAnimationFrame(() => (danger ? cancel : ok).focus());
  });
}

// ---- toasts -------------------------------------------------------------------------------------
// toast("Saved") | toast("Saved", 2000) | toast("Couldn't send", {kind: "bad", action: {label: "Retry", onClick}})
const TOAST_ICONS = { good: "check-circle", warn: "alert", bad: "alert-circle", info: "info" };
let toastHost = null;
export function toast(message, opts = {}) {
  if (typeof opts === "number") opts = { ms: opts };
  const { kind, ms = 3600, action, icon: ic } = opts;
  if (!toastHost || !toastHost.isConnected) {
    toastHost = el("div", { class: "ui-toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  const iconName = ic || TOAST_ICONS[kind];
  const node = el("div", { class: "ui-toast" + (kind ? ` ui-toast--${kind}` : "") },
    iconName ? icon(iconName) : null, el("span", { class: "ui-toast__msg" }, String(message ?? "")));
  const dismiss = () => { clearTimeout(timer); leave(node); };
  if (action) {
    node.append(el("button", { type: "button", class: "ui-toast__action",
      onclick: () => { dismiss(); action.onClick?.(); } }, action.label));
  }
  node.addEventListener("click", (e) => { if (!e.target.closest("button")) dismiss(); });
  while (toastHost.children.length >= 3) toastHost.firstElementChild.remove();
  toastHost.append(node);
  if (kind === "bad") haptic("error"); else if (kind === "good") haptic("success");
  const timer = setTimeout(dismiss, ms);
  return { close: dismiss };
}

// ---- skeletons and empty states ----------------------------------------------------------------
// skeleton("list", 3) | skeleton("lines", 4) | skeleton("card") | skeleton("tiles", 4)
export function skeleton(kind = "list", count = 3) {
  const group = el("div", { class: "ui-skel-group", "aria-hidden": "true" });
  const line = (w, cls = "ui-skel--line") => { const n = el("span", { class: `ui-skel ${cls}` }); n.style.width = w; return n; };
  for (let i = 0; i < count; i += 1) {
    if (kind === "list") {
      const circle = el("span", { class: "ui-skel" });
      circle.style.cssText = "width:40px;height:40px;border-radius:12px;flex:none";
      group.append(el("div", { class: "ui-skel-row" }, circle, el("div", {}, line(`${60 + ((i * 17) % 30)}%`), line(`${35 + ((i * 23) % 25)}%`))));
    } else if (kind === "lines") {
      group.append(line(i === count - 1 ? "60%" : `${88 - ((i * 13) % 20)}%`));
      group.style.gap = "10px";
    } else if (kind === "card") {
      const c = el("span", { class: "ui-skel" });
      c.style.cssText = "height:120px;border-radius:22px";
      group.append(c);
    } else if (kind === "tiles") {
      group.style.cssText = "display:flex;gap:10px";
      const t = el("span", { class: "ui-skel" });
      t.style.cssText = "width:76px;height:84px;border-radius:18px;flex:none";
      group.append(t);
    }
  }
  return el("div", { role: "status", "aria-label": "Loading" }, group);
}

// emptyState({icon, title, body, action: {label, icon, onClick}, compact})
export function emptyState({ icon: ic = "sparkles", title, body, action, compact } = {}) {
  return el("div", { class: "ui-empty" + (compact ? " ui-empty--compact" : "") },
    el("div", { class: "ui-empty__art" }, icon(ic)),
    title ? el("p", { class: "ui-empty__title" }, title) : null,
    body ? el("p", { class: "ui-empty__body" }, body) : null,
    action ? button({ label: action.label, icon: action.icon, variant: action.variant || "tonal", size: "sm", onClick: action.onClick }) : null);
}

// notice("Asana isn't connected", {tone: "warn", icon: "alert"})
export function notice(text, { tone, icon: ic } = {}) {
  return el("div", { class: "ui-notice" + (tone ? ` ui-notice--${tone}` : "") },
    icon(ic || (tone === "bad" ? "alert-circle" : tone === "warn" ? "alert" : tone === "good" ? "check-circle" : "info")),
    el("div", { class: "grow" }, text));
}

// ---- progress -----------------------------------------------------------------------------------
// const r = ringProgress({value: .6, size: 64, stroke: 7, tone: "good", big: "6", small: "days", label: "Learning streak"}); r.set(.8, "8")
export function ringProgress({ value = 0, size = 64, stroke = 7, tone, big, small, label } = {}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(size)); svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`); svg.setAttribute("aria-hidden", "true");
  const mk = (cls) => {
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("class", cls); circle.setAttribute("cx", String(size / 2)); circle.setAttribute("cy", String(size / 2));
    circle.setAttribute("r", String(r)); circle.setAttribute("stroke-width", String(stroke));
    return circle;
  };
  const track = mk("ui-ring__track");
  const val = mk("ui-ring__value");
  val.setAttribute("stroke-dasharray", String(c));
  val.setAttribute("stroke-dashoffset", String(c));
  svg.append(track, val);
  const bigNode = el("span", { class: "ui-ring__big" }, big ?? "");
  const smallNode = el("span", { class: "ui-ring__small" }, small ?? "");
  const node = el("div", { class: "ui-ring", role: "img" }, svg, el("div", { class: "ui-ring__label" }, bigNode, small ? smallNode : null));
  if (tone) node.setAttribute("data-tone", tone);
  if (tone) val.style.stroke = "var(--_tone)";
  node.set = (v, b) => {
    const clamped = Math.max(0, Math.min(1, Number(v) || 0));
    requestAnimationFrame(() => val.setAttribute("stroke-dashoffset", String(c * (1 - clamped))));
    if (b != null) bigNode.textContent = b;
    node.setAttribute("aria-label", `${label || "Progress"}: ${Math.round(clamped * 100)}%`);
  };
  node.set(value, big);
  return node;
}

// const bar = progressBar(.4); bar.set(.9); progressBar(null) is indeterminate.
export function progressBar(value = 0, label = "Progress") {
  const fill = el("i");
  const node = el("div", { class: "ui-bar", role: "progressbar", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
  node.set = (v) => {
    node.classList.toggle("ui-bar--indeterminate", v == null);
    if (v == null) { node.removeAttribute("aria-valuenow"); return; }
    const clamped = Math.max(0, Math.min(1, Number(v) || 0));
    node.style.setProperty("--v", String(clamped));
    node.setAttribute("aria-valuenow", String(Math.round(clamped * 100)));
  };
  node.set(value);
  return node;
}

// ---- segmented control --------------------------------------------------------------------------
// const seg = segmented({options: [{value: "local", label: "Local", icon?}], value: "local", onChange(v), label: "Model", block})
// seg.value reads and writes the selection.
export function segmented({ options = [], value, onChange, label = "Options", block = false } = {}) {
  const thumb = el("span", { class: "ui-seg__thumb", "aria-hidden": "true" });
  const node = el("div", { class: "ui-seg" + (block ? " ui-seg--block" : ""), role: "radiogroup", "aria-label": label }, thumb);
  node.style.setProperty("--n", String(Math.max(1, options.length)));
  let current = value ?? options[0]?.value;
  const buttons = options.map((opt, i) => {
    const b = el("button", { type: "button", role: "radio", "data-value": String(opt.value) },
      opt.icon ? icon(opt.icon) : null, opt.label);
    b.addEventListener("click", () => select(opt.value, true));
    b.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const next = options[(i + d + options.length) % options.length];
      select(next.value, true);
      buttons[options.indexOf(next)].focus();
    });
    node.append(b);
    return b;
  });
  function select(v, user) {
    const i = Math.max(0, options.findIndex((o) => o.value === v));
    current = options[i]?.value;
    buttons.forEach((b, j) => { b.setAttribute("aria-checked", String(j === i)); b.tabIndex = j === i ? 0 : -1; });
    node.style.setProperty("--i", String(i));
    if (user) { haptic("select"); onChange?.(current); }
  }
  select(current, false);
  Object.defineProperty(node, "value", { get: () => current, set: (v) => select(v, false) });
  return node;
}

// ---- switch -------------------------------------------------------------------------------------
// switchControl({checked, onChange(bool), label}) -> <label class="ui-switch"> (the input is node.input)
export function switchControl({ checked = false, onChange, label = "" } = {}) {
  const input = el("input", { type: "checkbox", role: "switch", "aria-label": label });
  input.checked = checked;
  input.addEventListener("change", () => { haptic("select"); onChange?.(input.checked); });
  const node = el("label", { class: "ui-switch" }, input, el("span", { class: "ui-switch__track" }), el("span", { class: "ui-switch__thumb" }));
  node.input = input;
  return node;
}

// ---- pull to refresh -----------------------------------------------------------------------------
// pullToRefresh(scrollingElement, async () => { ... }) adds a pull indicator at the top.
export function pullToRefresh(scroller, onRefresh) {
  const indicator = el("div", { class: "ui-ptr", "aria-hidden": "true" }, icon("arrow-down"));
  scroller.prepend(indicator);
  let startY = null, pulled = 0, busyNow = false;
  scroller.addEventListener("touchstart", (e) => { if (scroller.scrollTop <= 0 && !busyNow) startY = e.touches[0].clientY; }, { passive: true });
  scroller.addEventListener("touchmove", (e) => {
    if (startY == null) return;
    pulled = Math.max(0, Math.min(90, (e.touches[0].clientY - startY) * 0.5));
    indicator.style.transition = "none";
    indicator.style.height = pulled + "px";
    indicator.classList.toggle("is-armed", pulled > 56);
  }, { passive: true });
  scroller.addEventListener("touchend", async () => {
    if (startY == null) return;
    startY = null;
    indicator.style.transition = "";
    if (pulled > 56) {
      busyNow = true;
      haptic("medium");
      indicator.classList.remove("is-armed"); indicator.classList.add("is-busy");
      indicator.style.height = "44px";
      try { await onRefresh(); } finally {
        busyNow = false; indicator.classList.remove("is-busy"); indicator.style.height = "0px";
      }
    } else indicator.style.height = "0px";
    pulled = 0;
  });
  return indicator;
}

// ---- time helpers -------------------------------------------------------------------------------
export function relTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const diff = (Date.now() - date.getTime()) / 1000;
  const abs = Math.abs(diff);
  const fmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (abs < 45) return "just now";
  if (abs < 3600) return fmt.format(-Math.round(diff / 60), "minute");
  if (abs < 86400) return fmt.format(-Math.round(diff / 3600), "hour");
  if (abs < 86400 * 7) return fmt.format(-Math.round(diff / 86400), "day");
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
export function clockTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// ---- themes and accents -------------------------------------------------------------------------
export const THEMES = [
  { id: "midnight", label: "Midnight", icon: "moon" },
  { id: "daylight", label: "Daylight", icon: "sun" },
  { id: "aurora", label: "Aurora", icon: "sparkles" },
  { id: "auto", label: "Auto", icon: "refresh" },
];
export const ACCENTS = [
  { id: "indigo", label: "Indigo", swatch: "oklch(66% .17 272)" },
  { id: "blue", label: "Blue", swatch: "oklch(66% .16 245)" },
  { id: "violet", label: "Violet", swatch: "oklch(66% .19 302)" },
  { id: "teal", label: "Teal", swatch: "oklch(70% .13 182)" },
  { id: "amber", label: "Amber", swatch: "oklch(76% .15 68)" },
  { id: "rose", label: "Rose", swatch: "oklch(66% .19 12)" },
];
const THEME_BG = { midnight: "#0a0b0f", daylight: "#f4f5f8", aurora: "#07060e" };
function read(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (_) { return fallback; } }
function write(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } }

export function getTheme() { return read("hub.theme", "midnight"); }
export function getAccent() { return read("hub.accent", "indigo"); }
export function resolvedTheme() {
  const t = getTheme();
  if (t !== "auto") return t;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "daylight" : "midnight";
}
function syncThemeColor() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_BG[resolvedTheme()] || THEME_BG.midnight);
}
export function setTheme(id) {
  if (!THEMES.some((t) => t.id === id)) return;
  write("hub.theme", id);
  document.documentElement.dataset.theme = id;
  syncThemeColor();
  window.dispatchEvent(new CustomEvent("hub:theme", { detail: { theme: id } }));
}
export function setAccent(id) {
  if (!ACCENTS.some((a) => a.id === id)) return;
  write("hub.accent", id);
  document.documentElement.dataset.accent = id;
  window.dispatchEvent(new CustomEvent("hub:theme", { detail: { accent: id } }));
}
window.matchMedia?.("(prefers-color-scheme: light)").addEventListener?.("change", syncThemeColor);
syncThemeColor();

// A ready-made Appearance block (theme cards + accent swatches) for the Settings screen.
export function appearancePanel() {
  const themeRow = el("div", { class: "ui-theme-grid", role: "radiogroup", "aria-label": "Theme" });
  const accentRow = el("div", { class: "ui-accent-row", role: "radiogroup", "aria-label": "Accent colour" });
  const paint = () => {
    for (const b of themeRow.children) b.setAttribute("aria-checked", String(b.dataset.value === getTheme()));
    for (const b of accentRow.children) b.setAttribute("aria-checked", String(b.dataset.value === getAccent()));
  };
  for (const t of THEMES) {
    themeRow.append(el("button", { type: "button", role: "radio", class: "ui-theme-card", "data-value": t.id,
      onclick: () => { haptic("select"); setTheme(t.id); paint(); } },
    el("span", { class: "ui-theme-card__preview", "data-preview": t.id }, el("i"), el("i"), el("i")),
    el("span", { class: "ui-theme-card__label" }, icon(t.icon), t.label)));
  }
  for (const a of ACCENTS) {
    const sw = el("button", { type: "button", role: "radio", class: "ui-swatch", "data-value": a.id, "aria-label": a.label,
      onclick: () => { haptic("select"); setAccent(a.id); paint(); } }, icon("check"));
    sw.style.setProperty("--sw", a.swatch);
    accentRow.append(sw);
  }
  paint();
  return el("div", { class: "ui-stack" },
    el("p", { class: "ui-caps" }, "Theme"), themeRow,
    el("p", { class: "ui-caps" }, "Accent"), accentRow);
}
