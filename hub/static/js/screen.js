// Remote screen: see the laptop screen from the phone and, only after "Enable control", drive it.
//
// Data: docs/api/screen.md. Viewing = adaptive polling of GET /api/screen/frame (2-4 fps, backs off on 429)
// or the MJPEG /api/screen/stream when "Smooth" is on. Control is off until armed (POST /arm), expires by
// itself, and every input uses normalized 0..1 coordinates. Polling stops whenever the screen is hidden.
import { api, ApiError, el, renderMarkdown } from "./api.js";
import * as ui from "./ui.js";

// A sheet button that is still working ignores further taps (a double-tap must not send two requests).
const guard = (actions) => (actions || []).map((a) => {
  if (!a || !a.onClick) return a;
  let working = false;
  return { ...a, onClick: async (x) => { if (working) return false; working = true; try { return await a.onClick(x); } finally { working = false; } } };
});
const gsheet = (opts) => ui.sheet(opts.actions ? { ...opts, actions: guard(opts.actions) } : opts);


const KEYS = [
  { label: "Esc", key: "esc" }, { label: "Tab", key: "tab" }, { label: "Enter", key: "enter" },
  { label: "Back", key: "backspace", icon: "arrow-left" }, { label: "Del", key: "delete" },
  { label: "Up", key: "up", icon: "arrow-up" }, { label: "Down", key: "down", icon: "arrow-down" },
  { label: "Left", key: "left", icon: "chevron-left" }, { label: "Right", key: "right", icon: "chevron-right" },
  { label: "Home", key: "home" }, { label: "End", key: "end" },
  { label: "Ctrl+C", keys: ["ctrl", "c"] }, { label: "Ctrl+V", keys: ["ctrl", "v"] },
  { label: "Ctrl+A", keys: ["ctrl", "a"] }, { label: "Ctrl+Z", keys: ["ctrl", "z"] },
  { label: "Alt+Tab", keys: ["alt", "tab"] }, { label: "Win+D", keys: ["win", "d"] },
];
// Keys for driving the laptop's browser from the phone. Only shortcuts on the hub's allow-list (hub/screen.py HOTKEYS)
// may be sent. Ones it does not have yet are listed in MISSING_ON_SERVER and left out; delete a name from that set
// once screen.py allows it and the button appears.
const BROWSER_KEYS = [
  { id: "ctrl+l", label: "Address bar", keys: ["ctrl", "l"] }, { id: "ctrl+t", label: "New tab", keys: ["ctrl", "t"] },
  { id: "ctrl+w", label: "Close tab", keys: ["ctrl", "w"] }, { id: "ctrl+tab", label: "Next tab", keys: ["ctrl", "tab"] },
  { id: "f5", label: "Refresh", key: "f5" },
  { id: "ctrl+plus", label: "Zoom in", keys: ["ctrl", "plus"] }, { id: "ctrl+minus", label: "Zoom out", keys: ["ctrl", "minus"] },
  { id: "ctrl+0", label: "Reset zoom", keys: ["ctrl", "0"] },
];
const MISSING_ON_SERVER = new Set([]);   // every key above is on the server allow-list now
const hostOf = (url) => { try { return new URL(url).hostname; } catch (_) { return ""; } };
const store = {
  get(k, d) { try { const v = localStorage.getItem("hub.screen." + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("hub.screen." + k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};

export function init(container, ctx) {
  if (!document.querySelector('link[href="/css/screen.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/screen.css" }));
  }

  // ---- state ----------------------------------------------------------------------------------
  const S = {
    status: null, monitor: 1, smooth: false, saver: store.get("saver", true), mode: store.get("mode", "trackpad"),
    minutes: store.get("minutes", 5), armedUntil: 0, visible: false, viewing: false,
    scale: 1, tx: 0, ty: 0, nw: 16, nh: 9, cursor: null, full: false, windows: null, fps: 0,
  };
  let pollTimer = null, pollAbort = null, interval = 380, objectUrl = null, tickTimer = null, statusTimer = null;
  let streamTimer = null, releaseFull = null, lastHint = 0, frameCount = 0, fpsStamp = performance.now();
  const armed = () => S.armedUntil > Date.now();

  // ---- DOM ------------------------------------------------------------------------------------
  const statusCard = el("div", { class: "ui-card sc-status" });
  const img = el("img", { class: "sc-img", alt: "Your laptop screen", draggable: "false" });
  const cursorDot = el("span", { class: "sc-cursor", hidden: true });
  const canvas = el("div", { class: "sc-canvas" }, img, cursorDot);
  const overlay = el("div", { class: "sc-overlay" });
  const stage = el("div", { class: "sc-stage", tabindex: "0", "aria-label": "Laptop screen. Pinch to zoom." }, canvas, overlay);
  const liveChip = el("span", { class: "sc-chip sc-chip--live" }, el("span", { class: "ui-dot ui-dot--live" }), el("span", { class: "sc-fps ui-num" }, "Live"));
  const armChip = el("span", { class: "sc-chip sc-chip--armed", hidden: true });
  const ctrlBtn = ui.button({ label: "Control", icon: "unlock", variant: "primary", size: "sm", class: "sc-hudctl", hidden: true, onClick: () => promptArm() });
  const hudLeft = el("div", { class: "sc-hud__side" }, liveChip, ctrlBtn);
  const fullBtn = ui.button({ label: "Full screen", icon: "external", variant: "ghost", iconOnly: true, class: "sc-hudbtn" });
  const kbBtn = ui.button({ label: "Keyboard", icon: "command", variant: "ghost", iconOnly: true, class: "sc-hudbtn" });
  const hudRight = el("div", { class: "sc-hud__side" }, armChip, kbBtn, fullBtn);
  const hud = el("div", { class: "sc-hud" }, hudLeft, hudRight);
  const typer = el("textarea", { class: "sc-typer", rows: "1", autocapitalize: "off", autocomplete: "off", autocorrect: "off", spellcheck: "false", "aria-label": "Type on the laptop", tabindex: "-1" });
  const keyRow = el("div", { class: "sc-keys ui-hscroll", hidden: true, role: "toolbar", "aria-label": "Keys" });
  const browserRow = el("div", { class: "sc-keys sc-keys--browser ui-hscroll", hidden: true, role: "toolbar", "aria-label": "Browser keys" });
  const viewer = el("div", { class: "sc-viewer", hidden: true }, stage, hud, keyRow, browserRow, typer);
  const wsCard = el("section", { class: "ui-card sc-card sc-ws", "aria-label": "Workspaces" });
  const controls = el("div", { class: "sc-controls ui-stack", hidden: true });
  const root = el("div", { class: "sc-root ui-stack ui-stack--lg" }, wsCard, statusCard, viewer, controls);
  container.replaceChildren(root);

  // ---- status ---------------------------------------------------------------------------------
  function renderStatus() {
    const s = S.status;
    if (!s) {
      statusCard.replaceChildren(ui.skeleton("card", 1));
      return;
    }
    if (!s.available) {
      statusCard.replaceChildren(
        el("div", { class: "sc-status__head" }, ui.tile("monitor", "warn", "lg"),
          el("div", { class: "ui-row__body" }, el("div", { class: "ui-row__title" }, "Screen view isn't set up"),
            el("div", { class: "ui-row__sub" }, s.reason || "The screen libraries are not installed on the laptop."))),
        el("p", { class: "sc-hint" }, "On the laptop, run this once, then restart the hub:"),
        el("code", { class: "sc-code" }, "pip install -r requirements-screen.txt"),
        el("div", { class: "sc-actions" }, ui.button({ label: "Check again", icon: "refresh", variant: "tonal", onClick: (e) => ui.busy(e.currentTarget, loadStatus()) })));
      viewer.hidden = true; controls.hidden = true;
      return;
    }
    const m = s.monitors || [];
    const sub = armed() ? "Control is on" : (s.control_available ? "View only. Control is off." : (s.control_reason || "View only."));
    statusCard.replaceChildren(el("div", { class: "sc-status__head" },
      ui.tile("monitor", armed() ? "bad" : "good", "lg"),
      el("div", { class: "ui-row__body" }, el("div", { class: "ui-row__title" }, `${m.length || 1} ${m.length > 1 ? "screens" : "screen"} ready`),
        el("div", { class: "ui-row__sub" }, sub)),
      ui.badge(armed() ? "Control on" : "View only", armed() ? "bad" : "good")));
    statusCard.classList.toggle("sc-status--compact", S.viewing);
    viewer.hidden = false; controls.hidden = false;
  }

  async function loadStatus() {
    try {
      S.status = await api("/api/screen/status");
      S.armedUntil = S.status.control_armed ? Date.now() + (S.status.armed_seconds_left || 0) * 1000 : 0;
      if (S.status.available && !S.status.monitors?.some((m) => m.id === S.monitor)) S.monitor = S.status.monitors?.[0]?.id || 1;
    } catch (error) {
      if (error instanceof ApiError && error.status === 501) S.status = { available: false, reason: error.message };
      else {
        statusCard.replaceChildren(ui.notice(error.message, { tone: "bad" }),
          el("div", { class: "sc-actions" }, ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => { S.status = null; renderStatus(); loadStatus(); } })));
        return;
      }
    }
    renderStatus();
    buildControls();
    renderArm();
    if (S.status.available && S.visible) { startView(); loadWindows(); }
  }

  // ---- live view ------------------------------------------------------------------------------
  const frameUrl = () => `/api/screen/frame?monitor=${S.monitor}&max_side=${S.saver ? 720 : 1280}&quality=${S.saver ? 38 : 62}&t=${Date.now()}`;
  const streamUrl = () => `/api/screen/stream?monitor=${S.monitor}&fps=${S.saver ? 2 : 4}&max_side=${S.saver ? 720 : 1280}&quality=${S.saver ? 38 : 62}&t=${Date.now()}`;

  function stopView() {
    S.viewing = false;
    clearTimeout(pollTimer); pollTimer = null;
    clearTimeout(streamTimer); streamTimer = null;
    pollAbort?.abort(); pollAbort = null;
    if (S.smooth) img.removeAttribute("src");
    liveChip.classList.add("is-paused");
  }
  function startView() {
    if (!S.status?.available || !S.visible || document.hidden) return;
    stopView();
    S.viewing = true;
    overlay.replaceChildren();
    liveChip.classList.remove("is-paused");
    if (S.smooth) startStream(); else { interval = 380; poll(); }
  }
  function startStream() {
    img.onload = () => { sizeFrom(); stage.classList.remove("is-loading"); };
    img.onerror = () => showOverlay("The live stream stopped.", "Restart stream", startView);
    stage.classList.add("is-loading");
    img.src = streamUrl();
    liveChip.querySelector(".sc-fps").textContent = "Smooth";
    // The hub ends a stream after 10 minutes; offer a restart a little before the cut.
    streamTimer = setTimeout(() => { stopView(); showOverlay("Paused after 10 minutes to save your battery.", "Keep watching", startView); }, 9.5 * 60 * 1000);
  }
  function sizeFrom() {
    if (img.naturalWidth && (img.naturalWidth !== S.nw || img.naturalHeight !== S.nh)) {
      S.nw = img.naturalWidth; S.nh = img.naturalHeight;
      stage.style.setProperty("--sc-ratio", `${S.nw} / ${S.nh}`);
    }
  }
  async function poll() {
    if (!S.viewing || S.smooth) return;
    pollAbort = new AbortController();
    const started = performance.now();
    let wait = interval;
    try {
      const response = await fetch(frameUrl(), { credentials: "same-origin", headers: { "X-Hub": "1" }, signal: pollAbort.signal, cache: "no-store" });
      if (response.status === 401) { window.dispatchEvent(new Event("hub:unauthorized")); return; }
      if (response.status === 429) {
        const retry = Number(response.headers.get("Retry-After")) || 1;
        interval = Math.min(2000, interval * 1.6 + 120);
        wait = retry * 1000;
        liveChip.querySelector(".sc-fps").textContent = "Slowing down";
      } else if (!response.ok) {
        let msg = "The laptop couldn't send a picture.";
        try { msg = (await response.json()).detail || msg; } catch (_) { /* not JSON */ }
        throw new ApiError(response.status, msg);
      } else {
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        await new Promise((resolve) => { img.onload = resolve; img.onerror = resolve; img.src = url; });
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = url;
        sizeFrom();
        stage.classList.remove("is-loading");
        overlay.replaceChildren();
        // Aim for 2-4 frames a second: faster when frames arrive quickly, slower on a weak link.
        const took = performance.now() - started;
        interval = Math.max(250, Math.min(500, took * 1.4));
        const label = liveChip.querySelector(".sc-fps");
        if (label.textContent === "Slowing down") label.textContent = S.fps ? `${S.fps.toFixed(1)} fps` : "Live";
        wait = Math.max(0, interval - took);
        frameCount++;
        const now = performance.now();
        if (now - fpsStamp > 2000) {
          S.fps = frameCount / ((now - fpsStamp) / 1000); frameCount = 0; fpsStamp = now;
          liveChip.querySelector(".sc-fps").textContent = `${S.fps.toFixed(1)} fps`;
        }
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      stopView();
      showOverlay(error.message || "Lost the connection to the laptop.", "Try again", startView);
      return;
    }
    if (S.viewing) pollTimer = setTimeout(poll, wait);
  }
  function showOverlay(text, label, onClick) {
    overlay.replaceChildren(el("div", { class: "sc-overlay__card" }, el("p", {}, text), ui.button({ label, icon: "refresh", variant: "primary", size: "sm", onClick })));
  }

  // ---- zoom, pan, coordinates -------------------------------------------------------------------
  function applyTransform() {
    const w = stage.clientWidth, h = stage.clientHeight;
    S.scale = Math.max(1, Math.min(5, S.scale));
    S.tx = Math.min(0, Math.max(w * (1 - S.scale), S.tx));
    S.ty = Math.min(0, Math.max(h * (1 - S.scale), S.ty));
    canvas.style.transform = `translate(${S.tx}px, ${S.ty}px) scale(${S.scale})`;
    stage.classList.toggle("is-zoomed", S.scale > 1.02);
  }
  function zoomAt(cx, cy, factor) {
    const r = stage.getBoundingClientRect();
    const px = cx - r.left, py = cy - r.top;
    const next = Math.max(1, Math.min(5, S.scale * factor));
    const k = next / S.scale;
    S.tx = px - (px - S.tx) * k; S.ty = py - (py - S.ty) * k; S.scale = next;
    applyTransform();
  }
  // Where the picture sits inside the canvas (object-fit: contain), as fractions of the canvas.
  function fit() {
    const w = stage.clientWidth || 1, h = stage.clientHeight || 1, ir = S.nw / S.nh;
    return w / h > ir ? { x: (1 - (h * ir) / w) / 2, y: 0, w: (h * ir) / w, h: 1 } : { x: 0, y: (1 - w / ir / h) / 2, w: 1, h: w / ir / h };
  }
  function norm(clientX, clientY) {
    const r = canvas.getBoundingClientRect(), f = fit();
    const cx = (clientX - r.left) / r.width, cy = (clientY - r.top) / r.height;
    return { x: Math.min(1, Math.max(0, (cx - f.x) / f.w)), y: Math.min(1, Math.max(0, (cy - f.y) / f.h)) };
  }
  function showCursor(p) {
    S.cursor = p;
    const f = fit();
    cursorDot.hidden = false;
    cursorDot.style.left = `${(f.x + p.x * f.w) * 100}%`;
    cursorDot.style.top = `${(f.y + p.y * f.h) * 100}%`;
    cursorDot.style.setProperty("--inv", String(1 / S.scale));
  }

  // ---- sending input ----------------------------------------------------------------------------
  async function send(body, { quiet } = {}) {
    try {
      return await api("/api/screen/input", { method: "POST", json: { monitor: S.monitor, ...body } });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setArmed(0);
        if (!quiet) promptArm();
      } else if (!quiet) ctx.toast(error.message, { kind: "bad" });
      return null;
    }
  }
  let moveTimer = null, moveLast = 0, pendingMove = null;
  function sendMove(p) {
    pendingMove = p;
    const wait = 70 - (performance.now() - moveLast);
    if (wait <= 0) flushMove(); else if (!moveTimer) moveTimer = setTimeout(flushMove, wait);
  }
  function flushMove() {
    moveTimer = null;
    if (!pendingMove || !armed()) return;
    const p = pendingMove; pendingMove = null; moveLast = performance.now();
    send({ action: "move", x: p.x, y: p.y }, { quiet: true });
  }
  function hintViewOnly() {
    if (Date.now() - lastHint < 4000) return;
    lastHint = Date.now();
    ctx.toast("View only. Tap Enable control to click and type.", { kind: "info" });
  }

  // ---- gestures ---------------------------------------------------------------------------------
  const ptrs = new Map();
  let g = null, tapPending = null, lastTap = null;
  stage.addEventListener("pointerdown", (e) => {
    stage.setPointerCapture?.(e.pointerId);
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (ptrs.size === 1) {
      g = { sx: e.clientX, sy: e.clientY, lx: e.clientX, ly: e.clientY, moved: false, multi: false, long: false, startN: norm(e.clientX, e.clientY), scrollAcc: 0 };
      g.timer = setTimeout(() => {
        if (g && !g.moved && !g.multi && armed()) {
          g.long = true; ui.haptic("medium");
          const p = S.mode === "direct" ? g.startN : S.cursor;
          send({ action: "right_click", ...(p ? { x: p.x, y: p.y } : {}) });
        }
      }, 550);
    } else if (g) {
      g.multi = true; clearTimeout(g.timer);
      const [a, b] = [...ptrs.values()];
      g.dist = Math.hypot(a.x - b.x, a.y - b.y); g.cx = (a.x + b.x) / 2; g.cy = (a.y + b.y) / 2;
    }
  });
  stage.addEventListener("pointermove", (e) => {
    if (!ptrs.has(e.pointerId) || !g) return;
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (ptrs.size >= 2) {
      const [a, b] = [...ptrs.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y), cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
      const dd = dist - g.dist, dcx = cx - g.cx, dcy = cy - g.cy;
      if (Math.abs(dd) > Math.hypot(dcx, dcy) * 1.2 || !armed()) {
        if (g.dist > 0) zoomAt(cx, cy, dist / g.dist);
        S.tx += dcx; S.ty += dcy; applyTransform();
      } else if (Math.abs(dcy) >= Math.abs(dcx) || S.scale <= 1.02) {
        g.scrollAcc += dcy;
        const steps = Math.trunc(g.scrollAcc / 22);
        if (steps) {
          g.scrollAcc -= steps * 22;
          // Dragging fingers up scrolls the page down, like a phone.
          send({ action: "scroll", dy: Math.max(-30, Math.min(30, -steps)), ...(S.cursor ? { x: S.cursor.x, y: S.cursor.y } : {}) }, { quiet: true });
        }
      } else { S.tx += dcx; applyTransform(); }
      g.dist = dist; g.cx = cx; g.cy = cy;
      return;
    }
    const dx = e.clientX - g.lx, dy = e.clientY - g.ly;
    if (!g.moved && Math.hypot(e.clientX - g.sx, e.clientY - g.sy) > 8) { g.moved = true; clearTimeout(g.timer); }
    if (!g.moved) return;
    g.lx = e.clientX; g.ly = e.clientY;
    if (!armed()) {
      if (S.scale > 1.02) { S.tx += dx; S.ty += dy; applyTransform(); }
      return;
    }
    if (S.mode === "direct") {
      const p = norm(e.clientX, e.clientY); showCursor(p); sendMove(p);
    } else {
      const r = canvas.getBoundingClientRect(), f = fit();
      const base = S.cursor || { x: 0.5, y: 0.5 };
      const p = { x: Math.min(1, Math.max(0, base.x + (dx * 1.7) / (r.width * f.w))), y: Math.min(1, Math.max(0, base.y + (dy * 1.7) / (r.height * f.h))) };
      showCursor(p); sendMove(p);
    }
  });
  const end = (e) => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.delete(e.pointerId);
    if (!g) return;
    if (ptrs.size > 0) return;
    clearTimeout(g.timer);
    const done = g; g = null;
    if (e.type === "pointercancel" || done.multi || done.long) return;
    if (done.moved) {
      if (armed() && S.mode === "direct") {
        const p = norm(e.clientX, e.clientY);
        if (Math.hypot(e.clientX - done.sx, e.clientY - done.sy) > 24) send({ action: "drag", x: done.startN.x, y: done.startN.y, x2: p.x, y2: p.y });
      }
      return;
    }
    tap(e, done);
  };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
  stage.addEventListener("dblclick", (e) => e.preventDefault());

  function tap(e, down) {
    if (!armed()) { hintViewOnly(); return; }
    const p = S.mode === "direct" ? down.startN : S.cursor;
    if (S.mode === "direct") showCursor(p);
    const pos = p ? { x: p.x, y: p.y } : {};
    const now = performance.now();
    if (lastTap && now - lastTap.t < 320 && Math.hypot(e.clientX - lastTap.x, e.clientY - lastTap.y) < 40) {
      clearTimeout(tapPending); lastTap = null; tapPending = null;
      ui.haptic("medium"); send({ action: "double_click", ...pos });
      return;
    }
    lastTap = { t: now, x: e.clientX, y: e.clientY };
    tapPending = setTimeout(() => { tapPending = null; lastTap = null; ui.haptic("light"); send({ action: "click", ...pos }); }, 250);
  }
  stage.addEventListener("wheel", (e) => { e.preventDefault(); if (e.ctrlKey || !armed()) zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15); else send({ action: "scroll", dy: Math.sign(e.deltaY) * 3 }, { quiet: true }); }, { passive: false });

  // ---- keyboard, keys, paste ----------------------------------------------------------------------
  function openKeyboard() {
    if (!armed()) { promptArm(); return; }
    typer.tabIndex = 0; typer.focus({ preventScroll: true });
  }
  typer.addEventListener("input", () => {
    const text = typer.value;
    typer.value = "";
    if (text) send({ action: "type", text: text.slice(0, 500) });
  });
  typer.addEventListener("keydown", (e) => {
    const map = { Enter: "enter", Backspace: "backspace", Tab: "tab", Escape: "esc", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
    if (map[e.key] && !e.isComposing) { e.preventDefault(); send({ action: "key", key: map[e.key] }); }
  });
  kbBtn.addEventListener("click", openKeyboard);

  const keyButton = (k) => el("button", {
    type: "button", class: "sc-key", "aria-label": k.label,
    onclick: () => { ui.haptic("light"); send(k.keys ? { action: "hotkey", keys: k.keys } : { action: "key", key: k.key }); },
  }, k.icon ? ui.icon(k.icon) : null, k.icon ? null : k.label);
  function buildKeys() {
    keyRow.replaceChildren(...KEYS.map(keyButton));
    browserRow.replaceChildren(el("span", { class: "sc-keys__label" }, "Browser"), ...BROWSER_KEYS.filter((k) => !MISSING_ON_SERVER.has(k.id)).map(keyButton));
  }
  buildKeys();

  function pasteSheet() {
    const area = el("textarea", { class: "sc-paste", rows: "5", maxlength: "20000", placeholder: "Text to put on the laptop and paste (Ctrl+V)" });
    const fromPhone = navigator.clipboard?.readText ? ui.button({ label: "Use phone clipboard", icon: "clipboard", variant: "tonal", size: "sm", onClick: async () => {
      try { area.value = await navigator.clipboard.readText(); } catch (_) { ctx.toast("Allow clipboard access, or long-press the box to paste.", { kind: "warn" }); }
    } }) : null;
    gsheet({
      title: "Paste to laptop", content: el("div", { class: "ui-stack" }, el("p", { class: "sc-hint" }, "This replaces the laptop's clipboard, then presses Ctrl+V where the cursor is."), area, fromPhone),
      actions: [{ label: "Paste on laptop", variant: "primary", icon: "send", onClick: async () => {
        if (!area.value) return false;
        try { await api("/api/screen/paste", { method: "POST", json: { text: area.value } }); ui.toast("Pasted on the laptop", { kind: "good" }); }
        catch (error) { if (error.status === 409) { setArmed(0); promptArm(); } else ctx.toast(error.message, { kind: "bad" }); }
      } }],
    });
  }

  // ---- arm / disarm -----------------------------------------------------------------------------
  function setArmed(untilMs) {
    S.armedUntil = untilMs;
    if (S.status) S.status.control_armed = untilMs > Date.now();
    renderArm(); renderStatus();
  }
  async function promptArm() {
    const s = S.status;
    if (s && !s.control_available) { ctx.toast(s.control_reason || "Control needs pyautogui on the laptop.", { kind: "warn" }); return; }
    const ok = await ui.confirm({
      title: "Enable control?",
      message: `For ${S.minutes} ${S.minutes === 1 ? "minute" : "minutes"}, anyone holding this phone can click, type and press keys on your laptop. It switches off by itself, and you can disarm any time.`,
      confirmLabel: "Enable control", icon: "unlock",
    });
    if (!ok) return;
    try {
      const r = await api("/api/screen/arm", { method: "POST", json: { confirm: true, minutes: S.minutes } });
      setArmed(Date.now() + (r.armed_seconds_left ?? S.minutes * 60) * 1000);
      ui.haptic("success"); ui.toast(r.message || "Control is on", { kind: "warn" });
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
  }
  async function disarm() {
    try { await api("/api/screen/disarm", { method: "POST", json: {} }); } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    setArmed(0); ui.toast("Control is off", { kind: "good" });
  }
  function renderArm() {
    const on = armed();
    armChip.hidden = !on;
    keyRow.hidden = !on;
    browserRow.hidden = !on;
    kbBtn.hidden = !on;
    ctrlBtn.hidden = on || !S.status?.control_available;
    stage.classList.toggle("is-armed", on);
    if (!on) cursorDot.hidden = true;
    if (on) {
      const left = Math.max(0, Math.ceil((S.armedUntil - Date.now()) / 1000));
      armChip.replaceChildren(ui.icon("unlock"), el("span", { class: "ui-num" }, `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`),
        el("button", { type: "button", class: "sc-chip__x", onclick: disarm }, "Disarm"));
    }
    if (armBtn) {
      armBtn.hidden = on; minutesSeg.hidden = on; disarmBtn.hidden = !on;
    }
  }
  function tick() {
    if (S.armedUntil && !armed()) { setArmed(0); ui.toast("Control switched off", { kind: "info" }); }
    else if (armed()) renderArm();
  }

  // ---- controls card ----------------------------------------------------------------------------
  let armBtn, minutesSeg, disarmBtn, modeRow, monitorSeg;
  function buildControls() {
    const s = S.status;
    if (!s?.available) return;
    armBtn = ui.button({ label: "Enable control", icon: "unlock", variant: "primary", block: true, onClick: promptArm });
    disarmBtn = ui.button({ label: "Disarm now", icon: "lock", variant: "danger-soft", block: true, onClick: disarm });
    minutesSeg = ui.segmented({ label: "Control time", options: [1, 5, 10, 15].map((m) => ({ value: m, label: `${m} min` })), value: S.minutes, block: true, onChange: (v) => { S.minutes = v; store.set("minutes", v); } });
    const controlCard = el("div", { class: "ui-card sc-card" },
      el("div", { class: "ui-card__head" }, el("div", {}, el("div", { class: "ui-card__title" }, "Control"), el("div", { class: "ui-card__sub" }, "Off until you enable it. It expires by itself."))),
      s.control_available ? [minutesSeg, armBtn, disarmBtn] : ui.notice(s.control_reason || "Control needs pyautogui on the laptop (requirements-screen.txt).", { tone: "warn" }));
    modeRow = ui.segmented({ label: "Touch mode", block: true, value: S.mode, options: [{ value: "trackpad", label: "Trackpad" }, { value: "direct", label: "Direct touch" }], onChange: (v) => { S.mode = v; store.set("mode", v); } });
    const smooth = ui.switchControl({ checked: S.smooth, label: "Smooth video", onChange: (v) => { S.smooth = v; startView(); } });
    const saver = ui.switchControl({ checked: S.saver, label: "Data saver", onChange: (v) => { S.saver = v; store.set("saver", v); startView(); } });
    const toggle = (title, sub, sw) => el("div", { class: "ui-row sc-toggle" }, el("div", { class: "ui-row__body" }, el("div", { class: "ui-row__title" }, title), el("div", { class: "ui-row__sub" }, sub)), sw);
    const mons = s.monitors || [];
    monitorSeg = mons.length > 1 ? ui.segmented({ label: "Monitor", block: true, value: S.monitor, options: mons.map((m) => ({ value: m.id, label: m.id === 1 ? "Main" : `Screen ${m.id}` })), onChange: (v) => { S.monitor = v; S.cursor = null; cursorDot.hidden = true; resetZoom(); startView(); } }) : null;
    const viewCard = el("div", { class: "ui-card sc-card" },
      el("div", { class: "ui-card__head" }, el("div", { class: "ui-card__title" }, "View")),
      monitorSeg,
      el("div", { class: "ui-list ui-list--inset sc-list" },
        toggle("Smooth video", "Continuous stream, uses more data", smooth),
        toggle("Data saver", "Smaller, lower quality pictures", saver)),
      el("div", { class: "sc-label" }, "Touch mode"), modeRow,
      el("div", { class: "sc-actions" },
        ui.button({ label: "Reset zoom", icon: "refresh", variant: "tonal", size: "sm", onClick: resetZoom }),
        ui.button({ label: "Paste to laptop", icon: "clipboard", variant: "tonal", size: "sm", onClick: () => (armed() ? pasteSheet() : promptArm()) })));
    windowsBtn = ui.button({ label: "Windows", icon: "layers", variant: "tonal", size: "sm", hidden: true, onClick: windowsSheet });
    const askCard = el("div", { class: "ui-card ui-card--glass sc-card" },
      el("div", { class: "ui-card__head" }, ui.tile("sparkles", "violet"), el("div", {}, el("div", { class: "ui-card__title" }, "Ask about my screen"), el("div", { class: "ui-card__sub" }, "Claude looks at a screenshot and answers."))),
      el("div", { class: "sc-actions" }, ui.button({ label: "Ask Claude", icon: "sparkles", variant: "primary", size: "sm", onClick: askSheet }), windowsBtn));
    const zoomHas = !MISSING_ON_SERVER.has("ctrl+plus");
    const zoomCard = el("div", { class: "ui-card sc-card sc-zoomnote" },
      el("div", { class: "ui-card__head" }, ui.tile("eye", "info"), el("div", {}, el("div", { class: "ui-card__title" }, "Zoom the laptop browser"),
        el("div", { class: "ui-card__sub" }, zoomHas ? "Text too small? Use Zoom in and Zoom out in the Browser keys." : "Text too small? Pinch the picture to zoom this view, then double-tap to click. Bigger page text needs Ctrl and + on the laptop's own keyboard."))));
    controls.replaceChildren(controlCard, viewCard, zoomCard, askCard);
    renderArm();
    if (S.windows) windowsBtn.hidden = false;
  }
  let windowsBtn;
  function resetZoom() { S.scale = 1; S.tx = 0; S.ty = 0; applyTransform(); }

  // ---- ask Claude -------------------------------------------------------------------------------
  function askSheet() {
    const q = el("input", { type: "text", class: "sc-ask", maxlength: "500", value: "What is on my screen?", "aria-label": "Your question" });
    const out = el("div", { class: "sc-answer" });
    const sheet = gsheet({
      title: "Ask about my screen", content: el("div", { class: "ui-stack" }, q, out),
      actions: [{ label: "Ask Claude", icon: "sparkles", variant: "primary", onClick: async () => {
        const question = q.value.trim();
        if (!question) return false;
        const ok = await ui.confirm({ title: "Send a screenshot to Claude?", message: "A picture of your laptop screen will leave the laptop and be sent to Claude. Close anything private first.", confirmLabel: "Send screenshot", icon: "eye" });
        if (!ok) return false;
        out.replaceChildren(ui.skeleton("lines", 3));
        try {
          const r = await api("/api/screen/ask", { method: "POST", json: { question, model: "claude", confirm: true, monitor: S.monitor } });
          const md = el("div", { class: "markdown" });
          md.innerHTML = renderMarkdown(r.answer || ""); // sanitized by renderMarkdown
          out.replaceChildren(md, r.meta?.warning ? ui.notice(r.meta.warning, { tone: "warn" }) : el("p", { class: "sc-hint" }, "This screenshot was sent to Claude."));
        } catch (error) { out.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
        return false;
      } }],
    });
    return sheet;
  }

  // ---- windows ----------------------------------------------------------------------------------
  async function loadWindows() {
    try { S.windows = (await api("/api/screen/windows")).windows || []; if (windowsBtn) windowsBtn.hidden = false; }
    catch (_) { S.windows = null; if (windowsBtn) windowsBtn.hidden = true; }
  }
  async function windowsSheet() {
    const body = el("div", { class: "ui-stack" }, ui.skeleton("list", 4));
    gsheet({ title: "Windows", content: body });
    try {
      S.windows = (await api("/api/screen/windows")).windows || [];
      body.replaceChildren(...(S.windows.length ? [
        ...(armed() ? [] : [ui.notice("Enable control to bring a window to the front.", { tone: "warn" })]),
        el("div", { class: "ui-list ui-list--inset" }, S.windows.map((w) => ui.listRow({ icon: "layers", title: w.title || "(untitled)", meta: "Focus", onClick: async () => {
          if (!armed()) { promptArm(); return; }
          try { await api("/api/screen/windows/focus", { method: "POST", json: { id: w.id } }); ui.toast("Brought to front", { kind: "good" }); }
          catch (error) { if (error.status === 409) { setArmed(0); promptArm(); } else ctx.toast(error.message, { kind: "bad" }); }
        } }))),
      ] : [ui.emptyState({ icon: "layers", title: "No windows found", compact: true })]));
    } catch (error) { body.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
  }

  // ---- fullscreen / landscape -------------------------------------------------------------------
  async function setFull(on) {
    if (on === S.full) return;
    S.full = on;
    viewer.classList.toggle("is-full", on);
    document.documentElement.classList.toggle("sc-noscroll", on);
    fullBtn.replaceChildren(ui.icon(on ? "close" : "external"));
    fullBtn.setAttribute("aria-label", on ? "Exit full screen" : "Full screen");
    if (on) {
      releaseFull = ui.pushLayer(() => { releaseFull = null; setFull(false); });
      try { await viewer.requestFullscreen?.({ navigationUI: "hide" }); } catch (_) { /* CSS fallback is enough */ }
      try { await screen.orientation?.lock?.("landscape"); } catch (_) { /* not supported */ }
    } else {
      if (releaseFull) { const r = releaseFull; releaseFull = null; r(); }
      try { if (document.fullscreenElement) await document.exitFullscreen(); } catch (_) { /* ignore */ }
      try { screen.orientation?.unlock?.(); } catch (_) { /* ignore */ }
    }
    requestAnimationFrame(() => { resetZoom(); if (S.cursor) showCursor(S.cursor); });
  }
  fullBtn.addEventListener("click", () => setFull(!S.full));
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && S.full) setFull(false); });
  window.addEventListener("resize", () => { if (S.visible) { applyTransform(); if (S.cursor) showCursor(S.cursor); } });


  // ---- workspaces -------------------------------------------------------------------------------
  // A workspace is a list of typed steps (quick button, web address, app, folder, file, wait). One tap: confirm, run them on
  // the laptop, show the live view, then ask before turning control on. The hub never arms control by itself (docs/api/workspaces.md).
  const W = { list: null, error: "", hidden: false, scripts: null };
  const STEP_ICON = { script: "zap", url: "globe", app: "grid", folder: "folder", file: "file", wait: "timer" };
  const STEP_KIND = { script: "Quick button", url: "Web address", app: "App", folder: "Folder", file: "File", wait: "Wait" };
  const baseName = (p) => String(p || "").split(/[\\/]/).filter(Boolean).pop() || "";
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  function stepName(s, labels = {}) {
    if (s.type === "script") return labels[s.name] || s.name;
    if (s.type === "url") return hostOf(s.url) || s.url;
    if (s.type === "app") return s.name || "app";
    if (s.type === "folder") return baseName(s.path) || s.root;
    if (s.type === "file") return baseName(s.path);
    return plural(s.seconds || 0, "second");
  }
  function stepWords(s, labels) {
    const n = stepName(s, labels);
    switch (s.type) {
      case "script": return { plan: `run "${n}"`, doing: `Running "${n}"`, done: `"${n}" finished` };
      case "url": return { plan: `open ${n} in its browser`, doing: `Opening ${n}`, done: `Opened ${n}` };
      case "app": return { plan: `start ${n}`, doing: `Starting ${n}`, done: `Started ${n}` };
      case "folder": return { plan: `open the folder ${n}`, doing: `Opening folder ${n}`, done: `Opened folder ${n}` };
      case "file": return { plan: `open the file ${n}`, doing: `Opening ${n}`, done: `Opened ${n}` };
      default: return { plan: `wait ${n}`, doing: `Waiting ${n}`, done: `Waited ${n}` };
    }
  }
  async function loadWorkspaces() {
    try {
      W.list = (await api("/api/workspaces")).workspaces || [];
      W.error = "";
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) W.hidden = true; // this hub has no workspaces module
      else W.error = error.message;
    }
    renderWorkspaces();
  }
  function renderWorkspaces() {
    wsCard.hidden = W.hidden;
    if (W.hidden) return;
    const head = el("div", { class: "sc-ws__head" },
      el("div", {}, el("div", { class: "ui-card__title" }, "Workspaces"), el("div", { class: "ui-card__sub" }, "One tap sets the laptop up and shows it here.")),
      ui.button({ label: "New", icon: "plus", variant: "tonal", size: "sm", onClick: () => workspaceSheet() }));
    let body;
    if (W.error) body = [ui.notice(W.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => { W.error = ""; W.list = null; renderWorkspaces(); loadWorkspaces(); } })];
    else if (!W.list) body = [ui.skeleton("tiles", 3)];
    else {
      const custom = W.list.filter((w) => !w.builtin);
      body = [el("div", { class: "sc-ws__row ui-hscroll", role: "list" }, W.list.map(wsTile)),
        custom.length ? el("p", { class: "sc-hint sc-ws__tip" }, "Press and hold a tile to edit it.")
          : el("p", { class: "sc-hint sc-ws__tip" }, "Tap New to build your own, or add the NiFi template to connect the company VPN and open NiFi in one tap."),
        custom.length ? null : ui.button({ label: "Add NiFi template", icon: "sparkles", variant: "tonal", size: "sm", onClick: addTemplate })];
    }
    wsCard.replaceChildren(head, ...body.filter(Boolean));
  }
  function wsTile(w) {
    const n = (w.steps || []).length;
    const sub = w.builtin ? "Just show it" : (hostOf(w.url) && n === 1 ? hostOf(w.url) : plural(n, "step"));
    const node = el("button", { type: "button", class: "sc-ws__tile" + (w.builtin ? " sc-ws__tile--whole" : ""), role: "listitem", "aria-label": w.builtin ? "Open the whole laptop" : `Open workspace ${w.name}` },
      ui.tile(w.icon && /^[a-z0-9-]+$/.test(w.icon) ? w.icon : "globe", w.builtin ? "violet" : "accent", "lg"),
      el("span", { class: "sc-ws__name" }, w.name),
      el("span", { class: "sc-ws__sub" }, sub));
    if (w.builtin) { node.addEventListener("click", () => wholeLaptop(w)); return node; }
    let timer = null, held = false;
    const cancel = () => { clearTimeout(timer); timer = null; };
    node.addEventListener("pointerdown", () => { held = false; timer = setTimeout(() => { held = true; ui.haptic("medium"); workspaceSheet(w); }, 550); });
    ["pointerup", "pointerleave", "pointercancel", "pointermove"].forEach((t) => node.addEventListener(t, cancel));
    node.addEventListener("contextmenu", (e) => { e.preventDefault(); });
    node.addEventListener("click", () => { if (!held) launchWorkspace(w); held = false; });
    return node;
  }
  // The built-in tile: launch nothing, just show the laptop and offer Enable control.
  async function wholeLaptop(w) {
    if (launchBusy) return;
    launchBusy = true;
    try { await wholeLaptopNow(w); } finally { launchBusy = false; }
  }
  async function wholeLaptopNow(w) {
    const ok = await ui.confirm({ title: "Open the whole laptop?", icon: "laptop",
      message: "This shows your laptop screen here and then asks if you want to turn control on. Nothing is started on the laptop." , confirmLabel: "Show my laptop" });
    if (!ok) return;
    let r;
    try { r = await api(`/api/workspaces/${w.id}/launch`, { method: "POST", json: { confirm: true } }); }
    catch (error) { ctx.toast(error.message, { kind: "bad" }); return; }
    ui.haptic("success");
    await showLive(r);
  }
  async function addTemplate() {
    try {
      const t = (await api("/api/workspaces/templates")).templates?.[0];
      if (!t) { ctx.toast("No template is available.", { kind: "warn" }); return false; }
      const missing = t.missing_scripts || [];
      const body = { name: t.name, icon: t.icon, arm_minutes: t.arm_minutes,
        steps: (t.steps || []).filter((x) => !(x.type === "script" && missing.includes(x.name))) };
      await api("/api/workspaces", { method: "POST", json: body });
      ui.haptic("success");
      if (missing.length) {
        gsheet({ title: "Template added without the VPN step", content: el("div", { class: "ui-stack" },
          el("p", { class: "sc-hint" }, `The quick button "${missing[0]}" does not exist yet, so for now this only opens the address. To add it, run this once on the laptop, then edit the workspace and add it as a step:`),
          el("code", { class: "sc-code" }, `python -m hub add-script ${missing[0]} --label "Connect Cloudflare" -- "C:\\Program Files\\Cloudflare\\Cloudflare WARP\\warp-cli.exe" connect`)) });
      } else ui.toast("NiFi added", { kind: "good" });
      await loadWorkspaces();
      return true;
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); return false; }
  }
  async function loadScripts() {
    if (W.scripts) return W.scripts;
    try { W.scripts = await api("/api/scripts"); } catch (_) { W.scripts = []; }
    return W.scripts;
  }

  // ---- the editor: build a workspace out of steps ---------------------------------------------
  async function workspaceSheet(existing) {
    const w = existing || {};
    const draft = { steps: (w.steps || []).map((s) => ({ ...s })) };
    const labels = {};
    const name = el("input", { type: "text", maxlength: "60", value: w.name || "", placeholder: "For example Morning setup", "aria-label": "Name", autocomplete: "off" });
    const list = el("ol", { class: "sc-steps" });
    const field = (label, hint, input) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input, hint ? el("span", { class: "ui-hint" }, hint) : null);
    const move = (i, d) => { const j = i + d; if (j < 0 || j >= draft.steps.length) return; [draft.steps[i], draft.steps[j]] = [draft.steps[j], draft.steps[i]]; drawSteps(); };
    function drawSteps() {
      if (!draft.steps.length) { list.replaceChildren(el("li", { class: "sc-steps__empty" }, "No steps yet. Add the first one below.")); return; }
      list.replaceChildren(...draft.steps.map((s, i) => {
        const detail = s.type === "wait" ? null : el("span", { class: "sc-step__sub" }, STEP_KIND[s.type] + (s.type === "folder" || s.type === "file" ? ` · ${s.root}` : ""));
        const secs = s.type === "wait" ? el("input", { type: "number", inputmode: "numeric", min: "0", max: "120", value: String(s.seconds ?? 5), class: "sc-step__num", "aria-label": "Seconds to wait" }) : null;
        secs?.addEventListener("input", () => { s.seconds = Math.max(0, Math.min(120, parseInt(secs.value, 10) || 0)); });
        return el("li", { class: "sc-step" },
          el("span", { class: "sc-step__n ui-num" }, String(i + 1)),
          ui.tile(STEP_ICON[s.type] || "zap", s.type === "wait" ? "gray" : "accent", "sm"),
          el("span", { class: "sc-step__text" }, el("span", { class: "sc-step__name" }, s.type === "wait" ? "Wait (seconds)" : stepName(s, labels)), secs || detail),
          el("span", { class: "sc-step__btns" },
            ui.button({ label: "Move up", icon: "arrow-up", variant: "ghost", size: "sm", iconOnly: true, disabled: i === 0, onClick: () => move(i, -1) }),
            ui.button({ label: "Move down", icon: "arrow-down", variant: "ghost", size: "sm", iconOnly: true, disabled: i === draft.steps.length - 1, onClick: () => move(i, 1) }),
            ui.button({ label: "Remove step", icon: "trash", variant: "ghost", size: "sm", iconOnly: true, onClick: () => { draft.steps.splice(i, 1); drawSteps(); } })));
      }));
    }
    const add = (step) => { draft.steps.push(step); drawSteps(); list.lastElementChild?.scrollIntoView({ block: "nearest" }); };
    const addable = (type) => () => {
      if (draft.steps.length >= 12) { ctx.toast("A workspace can have up to 12 steps.", { kind: "warn" }); return; }
      ({ script: pickScript, url: pickUrl, app: pickApp, folder: pickPath, file: pickPath, wait: pickWait })[type](add, type);
    };
    const addRow = el("div", { class: "sc-ws__chips", role: "group", "aria-label": "Add a step" },
      Object.keys(STEP_KIND).map((t) => ui.button({ label: STEP_KIND[t], icon: STEP_ICON[t], variant: "tonal", size: "sm", onClick: addable(t) })));
    const content = el("div", { class: "ui-stack" },
      existing ? null : ui.button({ label: "Add NiFi template", icon: "sparkles", variant: "tonal", block: true, onClick: async () => { if (await addTemplate()) sheet.close(); } }),
      field("Name", "", name),
      el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Add a step"), addRow),
      el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Steps, in order"), list,
        el("span", { class: "ui-hint" }, "They run one after the other. If one fails the rest wait for you.")));
    async function save() {
      const body = { name: name.value.trim(), steps: draft.steps };
      if (!body.name) { ctx.toast("Give it a name.", { kind: "warn" }); return null; }
      if (!body.steps.length) { ctx.toast("Add at least one step.", { kind: "warn" }); return null; }
      try {
        const saved = await api(existing ? `/api/workspaces/${existing.id}` : "/api/workspaces", { method: existing ? "PUT" : "POST", json: body });
        ui.haptic("success"); loadWorkspaces();
        return saved;
      } catch (error) { ctx.toast(error.message, { kind: "bad" }); return null; }
    }
    const actions = [
      { label: existing ? "Save" : "Add workspace", variant: "primary", icon: "check", onClick: async () => ((await save()) ? undefined : false) },
      { label: "Test launch", variant: "tonal", icon: "play", onClick: async () => { const saved = await save(); if (!saved) return false; setTimeout(() => launchWorkspace(saved), 250); } },
    ];
    if (existing) actions.push({ label: "Delete", variant: "danger-soft", icon: "trash", onClick: async () => {
      if (!(await ui.confirm({ title: `Delete ${existing.name}?`, message: "This only removes the shortcut. Nothing on the laptop changes.", confirmLabel: "Delete", danger: true }))) return false;
      try { await api(`/api/workspaces/${existing.id}`, { method: "DELETE" }); loadWorkspaces(); } catch (error) { ctx.toast(error.message, { kind: "bad" }); return false; }
    } });
    const sheet = gsheet({ title: existing ? "Edit workspace" : "New workspace", content, actions });
    drawSteps();
    (await loadScripts()).forEach((sc) => { labels[sc.name] = sc.label || sc.name; });
    drawSteps();
  }
  async function pickScript(add) {
    const body = el("div", { class: "ui-stack" }, ui.skeleton("list", 2));
    const s = gsheet({ title: "Run a quick button", content: body });
    const scripts = await loadScripts();
    body.replaceChildren(...(scripts.length
      ? [el("div", { class: "ui-list ui-list--inset" }, scripts.map((sc) => ui.listRow({ icon: "zap", title: sc.label || sc.name, sub: sc.name, onClick: () => { add({ type: "script", name: sc.name }); s.close(); } })))]
      : [el("p", { class: "sc-hint" }, "No quick buttons yet. On the laptop, run:"), el("code", { class: "sc-code" }, 'python -m hub add-script NAME --label "Label" -- PROGRAM ARGS')]));
  }
  function pickUrl(add) {
    const input = el("input", { type: "url", inputmode: "url", autocapitalize: "off", autocorrect: "off", spellcheck: "false", maxlength: "2000", placeholder: "https://example.com", "aria-label": "Web address" });
    gsheet({ title: "Open a web address", content: el("div", { class: "ui-stack" }, input, el("p", { class: "sc-hint" }, "Opens in the laptop's default browser. Only http and https work.")),
      actions: [{ label: "Add step", icon: "plus", variant: "primary", onClick: () => {
        const v = input.value.trim();
        if (!/^https?:\/\/[^\s/]+/i.test(v)) { ctx.toast("Type an address starting with http:// or https://", { kind: "warn" }); return false; }
        add({ type: "url", url: v });
      } }] });
  }
  function pickWait(add) {
    const input = el("input", { type: "number", inputmode: "numeric", min: "0", max: "120", value: "5", "aria-label": "Seconds to wait" });
    gsheet({ title: "Wait a little", content: el("div", { class: "ui-stack" }, input, el("p", { class: "sc-hint" }, "Useful after a VPN or an app, so it is ready before the next step. 0 to 120 seconds.")),
      actions: [{ label: "Add step", icon: "plus", variant: "primary", onClick: () => add({ type: "wait", seconds: Math.max(0, Math.min(120, parseInt(input.value, 10) || 0)) }) }] });
  }
  function pickApp(add) {
    const q = el("input", { type: "search", placeholder: "Search your apps", "aria-label": "Search apps", autocomplete: "off", maxlength: "100" });
    const out = el("div", { class: "ui-stack" }, ui.skeleton("list", 3));
    const s = gsheet({ title: "Start an app", content: el("div", { class: "ui-stack" }, q, out) });
    let timer = null, seq = 0;
    const run = async () => {
      const my = ++seq;
      try {
        const r = await api(`/api/workspaces/apps?q=${encodeURIComponent(q.value.trim())}`);
        if (my !== seq) return;
        out.replaceChildren(...(r.apps?.length
          ? [el("div", { class: "ui-list ui-list--inset" }, r.apps.slice(0, 60).map((a) => ui.listRow({ icon: "grid", title: a.name, sub: a.source === "hub.json" ? "From your list" : "Installed", onClick: () => { add({ type: "app", id: a.id, name: a.name }); s.close(); } })))]
          : [ui.emptyState({ icon: "grid", title: q.value.trim() ? "No app matches" : (r.supported ? "No apps found" : "App list needs Windows"), body: r.supported ? "Try another name." : "Installed apps are read from the Start Menu of a Windows laptop. You can also list apps under 'apps' in hub.json.", compact: true })]));
      } catch (error) { if (my === seq) out.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
    };
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 220); });
    run();
  }
  async function pickPath(add, type) {
    const st = { root: "", path: "", roots: [] };
    const out = el("div", { class: "ui-stack" }, ui.skeleton("list", 3));
    const s = gsheet({ title: type === "folder" ? "Open a folder" : "Open a file", content: out });
    const draw = async () => {
      try {
        const r = await api(`/api/files?root=${encodeURIComponent(st.root)}&path=${encodeURIComponent(st.path)}`);
        const here = st.path ? st.path.split("/").filter(Boolean) : [];
        const rows = (r.entries || []).filter((e) => type === "file" || e.is_dir).sort((a, b) => (b.is_dir - a.is_dir) || a.name.localeCompare(b.name)).slice(0, 200);
        out.replaceChildren(
          st.roots.length > 1 ? el("div", { class: "sc-ws__chips" }, st.roots.map((n) => el("button", { type: "button", class: "ui-chip" + (n === st.root ? " is-on" : ""), onclick: () => { st.root = n; st.path = ""; draw(); } }, n))) : el("span", { hidden: true }),
          el("div", { class: "sc-crumb" }, here.length ? ui.button({ label: "Up", icon: "arrow-up", variant: "tonal", size: "sm", onClick: () => { st.path = here.slice(0, -1).join("/"); draw(); } }) : null,
            el("span", { class: "sc-hint" }, `${st.root}${here.length ? " / " + here.join(" / ") : ""}`)),
          type === "folder" ? ui.button({ label: "Use this folder", icon: "check", variant: "primary", block: true, onClick: () => { add({ type: "folder", root: st.root, path: st.path }); s.close(); } }) : el("span", { hidden: true }),
          rows.length ? el("div", { class: "ui-list ui-list--inset" }, rows.map((e) => ui.listRow({ icon: e.is_dir ? "folder" : "file", title: e.name, chevron: e.is_dir,
            onClick: () => { const p = [...here, e.name].join("/"); if (e.is_dir) { st.path = p; draw(); } else { add({ type: "file", root: st.root, path: p }); s.close(); } } })))
            : ui.emptyState({ icon: "folder", title: type === "folder" ? "No folders in here" : "Nothing in here", compact: true }));
      } catch (error) { out.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
    };
    try {
      st.roots = ((await api("/api/roots")) || []).map((r) => r.name);
      if (!st.roots.length) { out.replaceChildren(ui.emptyState({ icon: "folder", title: "No shared folders", body: "Add folders under 'roots' in hub.json.", compact: true })); return; }
      st.root = st.roots[0];
      draw();
    } catch (error) { out.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
  }

  // ---- launching ------------------------------------------------------------------------------
  let launchBusy = false;   // two quick taps on a tile must not stack two confirm dialogs or two launches
  async function launchWorkspace(w, opts) {
    if (launchBusy) return;
    launchBusy = true;
    try { await launchWorkspaceNow(w, opts); } finally { launchBusy = false; }
  }
  async function launchWorkspaceNow(w, { skipSteps = false } = {}) {
    const labels = {};
    (await loadScripts()).forEach((sc) => { labels[sc.name] = sc.label || sc.name; });
    const todo = (w.steps || []).filter((s) => !(skipSteps && (s.type === "script" || s.type === "wait")));
    if (!todo.length) { ctx.toast("This workspace has nothing to do.", { kind: "warn" }); return; }
    const ok = await ui.confirm({ title: `Open ${w.name}?`, icon: "laptop",
      message: `On your laptop this will ${todo.map((s) => stepWords(s, labels).plan).join(", then ")}. Then the live view starts here, and you decide whether to turn control on.`, confirmLabel: "Open on laptop" });
    if (!ok) return;
    const rows = todo.map((s) => stepWords(s, labels));
    rows.push({ doing: "Starting the live view", done: "Live view ready" });
    const list = el("ol", { class: "sc-prog" });
    const draw = (cur, failedAt, notes = {}) => list.replaceChildren(...rows.map((r, i) => {
      const state = failedAt === i ? "bad" : i < cur ? "done" : i === cur ? "run" : "wait";
      return el("li", { class: `sc-prog__row is-${state}` },
        state === "run" ? el("span", { class: "ui-spinner" }) : ui.icon(state === "done" ? "check-circle" : state === "bad" ? "alert-circle" : "circle"),
        el("span", { class: "sc-prog__text" }, el("span", {}, state === "done" ? r.done : r.doing + (state === "run" ? "…" : "")), notes[i] ? el("span", { class: "sc-prog__note" }, notes[i]) : null));
    }));
    const foot = el("div", { class: "ui-stack" });
    const sheet = gsheet({ title: w.name, content: el("div", { class: "ui-stack" }, list, foot), dismissible: false });
    draw(0);
    // The hub runs everything in one request; walk the pending row forward while it works so the list feels alive.
    let cur = 0, walking = true, walker = null;
    const stepMs = (i) => (todo[i]?.type === "wait" ? (todo[i].seconds || 0) * 1000 : 1500);
    const advance = () => { if (!walking || cur >= todo.length - 1) return; walker = setTimeout(() => { cur++; draw(cur); advance(); }, stepMs(cur)); };
    advance();
    const stopWalk = () => { walking = false; clearTimeout(walker); };
    let r;
    try {
      r = await api(`/api/workspaces/${w.id}/launch`, { method: "POST", json: { confirm: true, skip_steps: skipSteps } });
    } catch (error) {
      stopWalk();
      draw(cur, cur);
      foot.replaceChildren(ui.notice(error.message, { tone: "bad" }), ui.button({ label: "Close", variant: "secondary", block: true, onClick: () => sheet.close() }));
      return;
    }
    stopWalk();
    const bad = (r.steps || []).findIndex((x) => !x.ok);
    if (bad >= 0 || r.stopped) {
      const at = bad >= 0 ? bad : Math.max(0, (r.steps || []).length - 1);
      const notes = {};
      if (r.steps?.[at]) notes[at] = r.steps[at].message;
      draw(at, at, notes);
      const canSkip = todo[at] && (todo[at].type === "script" || todo[at].type === "wait") && todo.some((s) => ["url", "app", "folder", "file"].includes(s.type));
      foot.replaceChildren(
        ui.notice(canSkip ? "A step failed, so the rest did not run. If your company blocks this from the command line, open the app on the laptop once by hand, then use Open anyway." : "A step failed, so the rest did not run.", { tone: "bad" }),
        canSkip ? ui.button({ label: "Open anyway", icon: "external", variant: "primary", block: true, onClick: () => { sheet.close(); launchWorkspace(w, { skipSteps: true }); } }) : null,
        ui.button({ label: "Close", variant: "secondary", block: true, onClick: () => sheet.close() }));
      return;
    }
    draw(rows.length, -1);
    ui.haptic("success");
    setTimeout(() => sheet.close(), 700);
    await showLive(r);
  }
  // After a launch: refresh the status, start the live view, scroll to it, then offer to turn control on.
  async function showLive(r) {
    S.status = null;
    await loadStatus();
    if (!S.status?.available) { ctx.toast("Opened on the laptop, but the screen view isn't set up.", { kind: "warn" }); return; }
    startView();
    viewer.scrollIntoView({ behavior: "smooth", block: "start" });
    if (S.status.control_available && !armed()) {
      S.minutes = [1, 5, 10, 15].includes(r?.arm_minutes) ? r.arm_minutes : S.minutes;
      if (minutesSeg) minutesSeg.value = S.minutes;
      setTimeout(promptArm, 900);
    }
  }

  // ---- lifecycle --------------------------------------------------------------------------------
  function onVisibility() {
    if (document.hidden) stopView(); else if (S.visible) startView();
  }
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", stopView);

  function show() {
    S.visible = true;
    clearInterval(tickTimer); clearInterval(statusTimer);
    tickTimer = setInterval(tick, 1000);
    statusTimer = setInterval(() => { if (!document.hidden) api("/api/screen/status").then((s) => { S.status = s; S.armedUntil = s.control_armed ? Date.now() + (s.armed_seconds_left || 0) * 1000 : 0; renderStatus(); renderArm(); }).catch(() => {}); }, 20000);
    if (!S.status) { renderStatus(); loadStatus(); } else { renderStatus(); startView(); }
    loadWorkspaces();
  }
  function hide() {
    S.visible = false;
    stopView();
    clearInterval(tickTimer); clearInterval(statusTimer);
    if (S.full) setFull(false);
  }
  renderStatus();
  renderWorkspaces();

  // Another screen (the Launcher, search) hands over: "live" shows the screen and offers Enable control; "workspaces" is just this card.
  function openSection(name) {
    if (name === "live") showLive({});
    else if (name === "workspaces") wsCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  return { show, hide, openSection, openItem: () => wsCard.scrollIntoView({ behavior: "smooth", block: "start" }), refresh: () => { S.status = null; renderStatus(); loadStatus(); } };
}
