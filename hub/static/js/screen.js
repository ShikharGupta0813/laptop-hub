// Remote screen: see the laptop screen from the phone and, only after "Enable control", drive it.
//
// Data: docs/api/screen.md. Viewing = adaptive polling of GET /api/screen/frame (2-4 fps, backs off on 429)
// or the MJPEG /api/screen/stream when "Smooth" is on. Control is off until armed (POST /arm), expires by
// itself, and every input uses normalized 0..1 coordinates. Polling stops whenever the screen is hidden.
import { api, ApiError, el, renderMarkdown } from "./api.js";
import * as ui from "./ui.js";

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
  const hudLeft = el("div", { class: "sc-hud__side" }, liveChip);
  const fullBtn = ui.button({ label: "Full screen", icon: "external", variant: "ghost", iconOnly: true, class: "sc-hudbtn" });
  const kbBtn = ui.button({ label: "Keyboard", icon: "command", variant: "ghost", iconOnly: true, class: "sc-hudbtn" });
  const hudRight = el("div", { class: "sc-hud__side" }, armChip, kbBtn, fullBtn);
  const hud = el("div", { class: "sc-hud" }, hudLeft, hudRight);
  const typer = el("textarea", { class: "sc-typer", rows: "1", autocapitalize: "off", autocomplete: "off", autocorrect: "off", spellcheck: "false", "aria-label": "Type on the laptop", tabindex: "-1" });
  const keyRow = el("div", { class: "sc-keys ui-hscroll", hidden: true, role: "toolbar", "aria-label": "Keys" });
  const viewer = el("div", { class: "sc-viewer", hidden: true }, stage, hud, keyRow, typer);
  const controls = el("div", { class: "sc-controls ui-stack", hidden: true });
  const root = el("div", { class: "sc-root ui-stack ui-stack--lg" }, statusCard, viewer, controls);
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

  function buildKeys() {
    keyRow.replaceChildren(...KEYS.map((k) => el("button", {
      type: "button", class: "sc-key", "aria-label": k.label,
      onclick: () => { ui.haptic("light"); send(k.keys ? { action: "hotkey", keys: k.keys } : { action: "key", key: k.key }); },
    }, k.icon ? ui.icon(k.icon) : null, k.icon ? null : k.label)));
  }
  buildKeys();

  function pasteSheet() {
    const area = el("textarea", { class: "sc-paste", rows: "5", maxlength: "20000", placeholder: "Text to put on the laptop and paste (Ctrl+V)" });
    const fromPhone = navigator.clipboard?.readText ? ui.button({ label: "Use phone clipboard", icon: "clipboard", variant: "tonal", size: "sm", onClick: async () => {
      try { area.value = await navigator.clipboard.readText(); } catch (_) { ctx.toast("Allow clipboard access, or long-press the box to paste.", { kind: "warn" }); }
    } }) : null;
    ui.sheet({
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
    kbBtn.hidden = !on;
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
    controls.replaceChildren(controlCard, viewCard, askCard);
    renderArm();
    if (S.windows) windowsBtn.hidden = false;
  }
  let windowsBtn;
  function resetZoom() { S.scale = 1; S.tx = 0; S.ty = 0; applyTransform(); }

  // ---- ask Claude -------------------------------------------------------------------------------
  function askSheet() {
    const q = el("input", { type: "text", class: "sc-ask", maxlength: "500", value: "What is on my screen?", "aria-label": "Your question" });
    const out = el("div", { class: "sc-answer" });
    const sheet = ui.sheet({
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
    ui.sheet({ title: "Windows", content: body });
    try {
      S.windows = (await api("/api/screen/windows")).windows || [];
      body.replaceChildren(...(S.windows.length ? [
        armed() ? null : ui.notice("Enable control to bring a window to the front.", { tone: "warn" }),
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
  }
  function hide() {
    S.visible = false;
    stopView();
    clearInterval(tickTimer); clearInterval(statusTimer);
    if (S.full) setFull(false);
  }
  renderStatus();

  return { show, hide, refresh: () => { S.status = null; renderStatus(); loadStatus(); } };
}
