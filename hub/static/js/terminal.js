// Terminal tab: a real shell on the laptop, drawn with xterm.js (loaded as globals by index.html).
// The session lives as long as the page: hide() keeps the socket and the terminal alive.
import { el } from "./api.js";
import { icon, segmented, resolvedTheme } from "./ui.js";

const FONT_MIN = 10;
const FONT_MAX = 22;
const FONT_DEFAULT = 14;
const CONNECT_TIMEOUT_MS = 10000;
const COPY_LINES = 300;
const KEYBOARD_THRESHOLD = 120; // px the visual viewport must shrink before we call it "keyboard open"

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } },
};

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ANSI palettes tuned for a dark and a light background.
const ANSI_DARK = {
  black: "#2b3340", red: "#f87171", green: "#4ade80", yellow: "#fbbf24", blue: "#60a5fa",
  magenta: "#c084fc", cyan: "#22d3ee", white: "#d5dbe4", brightBlack: "#6b7787", brightRed: "#fca5a5",
  brightGreen: "#86efac", brightYellow: "#fde68a", brightBlue: "#93c5fd", brightMagenta: "#d8b4fe",
  brightCyan: "#67e8f9", brightWhite: "#ffffff",
};
const ANSI_LIGHT = {
  black: "#17202b", red: "#c0362c", green: "#16803c", yellow: "#8a5a00", blue: "#0b5cb5",
  magenta: "#8e3fbf", cyan: "#0e7490", white: "#566271", brightBlack: "#6b7787", brightRed: "#d9473c",
  brightGreen: "#1f9d4d", brightYellow: "#a06a00", brightBlue: "#1d74d6", brightMagenta: "#a855d6",
  brightCyan: "#0891b2", brightWhite: "#17202b",
};

function buildTheme() {
  const light = resolvedTheme() === "daylight";
  const accent = cssVar("--c-accent") || cssVar("--accent") || "#4fb3ff";
  return {
    background: cssVar("--c-bg") || cssVar("--bg") || "#0f1217",
    foreground: cssVar("--c-text") || cssVar("--text") || "#e8edf4",
    cursor: accent,
    cursorAccent: cssVar("--c-bg") || cssVar("--bg") || "#0f1217",
    selectionBackground: light ? "rgba(11,107,203,.28)" : "rgba(79,179,255,.35)",
    ...(light ? ANSI_LIGHT : ANSI_DARK),
  };
}

// Strip control characters from text that came from the network before it goes into the terminal.
const clean = (text) => String(text).replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim();

// Ctrl+<char>: letters and the classic punctuation set map to C0 control codes.
function controlCode(ch) {
  if (ch.length !== 1) return null;
  const code = ch.toUpperCase().charCodeAt(0);
  if (code >= 65 && code <= 90) return String.fromCharCode(code - 64);
  const punct = { " ": 0, "@": 0, "[": 27, "\\": 28, "]": 29, "^": 30, "_": 31, "?": 127 };
  return ch in punct ? String.fromCharCode(punct[ch]) : null;
}

export function init(container, ctx) {
  container.textContent = "";

  // ---------- state ----------
  let term = null;
  let fit = null;
  let ws = null;
  let state = "disconnected"; // connecting | connected | disconnected
  let shells = [];
  let shellsLoaded = false;
  let booted = false; // first connection attempt made
  let ready = null; // promise: terminal built and shell list loaded
  let visible = false;
  let lastClose = null; // close code of the last socket, for re-login handling
  let gotError = false;
  let ctrl = false;
  let alt = false;
  let fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, parseInt(store.get("hub.term.font"), 10) || FONT_DEFAULT));
  let connectTimer = null;
  let resizeTimer = null;
  let confirmTimer = null;
  let lastSent = { rows: 0, cols: 0 };

  // ---------- DOM ----------
  const select = el("select", { class: "term-shell", "aria-label": "Shell" });
  const shellSlot = el("div", { class: "term-shell-slot" }, select);
  const sessionBtn = el("button", { type: "button", class: "ui-btn ui-btn--tonal ui-btn--sm term-session" }, "New session");
  const fontDown = el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--icon term-font-btn", "aria-label": "Smaller text" }, "A−");
  const fontUp = el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--icon term-font-btn", "aria-label": "Larger text" }, "A+");
  const fontLabel = el("span", { class: "term-font-size", "aria-hidden": "true" }, String(fontSize));
  const toolbar = el("div", { class: "term-toolbar" },
    shellSlot, sessionBtn,
    el("div", { class: "term-font", role: "group", "aria-label": "Text size" }, fontDown, fontLabel, fontUp));

  const dot = el("span", { class: "term-dot ui-dot", "aria-hidden": "true" });
  const statusText = el("span", { class: "term-status-text" }, "Disconnected");
  const statusDetail = el("span", { class: "term-status-detail muted" });
  const statusLine = el("div", { class: "term-status", role: "status", "aria-live": "polite" }, dot, statusText, " ", statusDetail);

  const host = el("div", { class: "term-host" });
  const screen = el("div", { class: "term-screen" }, host);

  // paste fallback panel (only shown when the clipboard can't be read directly)
  const pasteBox = el("textarea", {
    class: "term-paste-box", rows: "3", "aria-label": "Text to paste into the terminal",
    placeholder: "Long-press here and choose Paste", autocapitalize: "off", autocomplete: "off", spellcheck: "false",
  });
  const pasteSend = el("button", { type: "button", class: "ui-btn ui-btn--primary ui-btn--sm" }, "Send");
  const pasteCancel = el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm" }, "Cancel");
  const pastePanel = el("div", { class: "term-paste", hidden: true },
    el("p", { class: "muted term-paste-note" }, "This browser would not let the page read the clipboard. Paste here instead."),
    pasteBox, el("div", { class: "row" }, pasteSend, pasteCancel));

  const toBottom = el("button", { type: "button", class: "ui-btn ui-btn--primary ui-btn--icon term-bottom", "aria-label": "Scroll to bottom", hidden: true }, icon("arrow-down", 20));
  screen.append(toBottom);
  const keybar = el("div", { class: "term-keys", role: "toolbar", "aria-label": "Extra keys" });
  container.append(toolbar, statusLine, screen, pastePanel, keybar);

  // ---------- status ----------
  function setStatus(next, detail = "") {
    state = next;
    const word = { connecting: "Connecting", connected: "Connected", disconnected: "Disconnected" }[next];
    statusText.textContent = word;
    statusDetail.textContent = detail;
    dot.dataset.state = next;
    const busy = next === "connecting";
    sessionBtn.textContent = next === "connected" ? "New session" : (busy ? "Connecting…" : "Reconnect");
    sessionBtn.disabled = busy;
    sessionBtn.removeAttribute("data-confirm");
    clearTimeout(confirmTimer);
  }

  // Write a coloured notice into the terminal on its own line.
  function note(text, sgr = "2") {
    if (!term) return;
    term.write(`\r\n\x1b[${sgr}m${clean(text)}\x1b[0m\r\n`);
  }

  // Tell the person something short: a toast, and the status line as a backup that always stays visible.
  function tell(message) {
    ctx.toast(message);
    statusDetail.classList.remove("is-error");
    statusDetail.textContent = message;
  }

  // ---------- socket ----------
  function dims() {
    const rows = term && term.rows > 1 ? term.rows : 24;
    const cols = term && term.cols > 1 ? term.cols : 80;
    return { rows, cols };
  }

  function closeSocket() {
    clearTimeout(connectTimer);
    if (ws) {
      const old = ws;
      ws = null; // handlers below ignore sockets that are no longer current
      try { old.close(); } catch (_) { /* already closed */ }
    }
  }

  function connect(fresh) {
    closeSocket();
    gotError = false;
    lastClose = null;
    ctrl = alt = false;
    refreshMods();
    if (fresh) {
      term.reset();
      note("New session. The previous shell and everything in it is gone.");
    }
    fitNow();
    const { rows, cols } = dims();
    const params = new URLSearchParams({ rows: String(rows), cols: String(cols) });
    if (select.value) params.set("shell", select.value);
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/term?${params}`;
    setStatus("connecting", select.value ? `Starting ${select.value}…` : "");
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      setStatus("disconnected", "Could not open a connection: " + error.message);
      note("Could not open a connection: " + error.message, "31");
      return;
    }
    socket.binaryType = "arraybuffer";
    ws = socket;
    lastSent = { rows, cols };
    connectTimer = setTimeout(() => {
      if (ws === socket && socket.readyState === WebSocket.CONNECTING) {
        ws = null;
        try { socket.close(); } catch (_) { /* ignore */ }
        setStatus("disconnected", "No answer from the laptop. Is it awake and on the same network?");
        note("No answer from the laptop (timed out). Press Reconnect to try again.", "31");
      }
    }, CONNECT_TIMEOUT_MS);

    let opened = false;
    socket.onopen = () => {
      if (ws !== socket) return;
      opened = true;
      clearTimeout(connectTimer);
      setStatus("connected", select.value || "");
      if (visible) term.focus();
      scheduleResize(); // the phone may have changed size while connecting
    };
    socket.onmessage = (event) => {
      if (ws !== socket) return;
      if (typeof event.data === "string") {
        let message = event.data;
        try { message = JSON.parse(event.data).error || event.data; } catch (_) { /* plain text */ }
        gotError = true;
        const text = clean(message);
        note("Error: " + text, "31");
        setStatus(state, text);
        statusDetail.classList.add("is-error");
        return;
      }
      term.write(new Uint8Array(event.data));
    };
    socket.onerror = () => { /* onclose always follows and carries the details */ };
    socket.onclose = (event) => {
      if (ws !== socket) return;
      clearTimeout(connectTimer);
      ws = null;
      lastClose = event.code;
      ctrl = alt = false;
      refreshMods();
      const kept = gotError ? statusDetail.textContent : "";
      let detail;
      let line;
      let diagnosing = false;
      if (event.code === 4401) {
        detail = "You are signed out. Log in again.";
        line = "Signed out. Log in again, then press Reconnect.";
        window.dispatchEvent(new Event("hub:unauthorized"));
      } else if (event.code === 4403) {
        detail = "The laptop refused this connection because it came from another address.";
        line = "Connection refused: the page was opened from an address the hub does not trust. Open the hub from the address you set it up with.";
      } else if (event.code === 4400) {
        detail = "The laptop did not accept that shell or window size. Pick a shell and try again.";
        line = "The laptop rejected the request (unknown shell or bad size). Pick a shell and press Reconnect.";
      } else if (!opened && !gotError) {
        // The server rejects bad connections before the handshake finishes, so a browser reports
        // them all as code 1006 with no reason. Ask the hub what is wrong.
        detail = "The laptop did not accept the connection. Checking why\u2026";
        line = "The connection was refused. Checking why\u2026";
        diagnosing = true;
        diagnose();
      } else if (event.code === 1006 || event.code === 1001 || event.code === 1011 || event.code === 1012) {
        detail = "Connection lost. Is the laptop awake and the hub running?";
        line = "Connection lost. Press Reconnect to start a new shell (the old shell's state is gone).";
      } else {
        detail = "Session ended (press Reconnect).";
        line = "Session ended (press Reconnect). It starts a fresh shell.";
      }
      setStatus("disconnected", kept || detail);
      if (kept) statusDetail.classList.add("is-error"); else statusDetail.classList.toggle("is-error", event.code !== 1000);
      if (!gotError && !diagnosing) note(line, event.code === 1000 || event.code === 1005 ? "33" : "31");
    };
  }

  // A refused handshake carries no reason, so work out what it was: signed out, laptop
  // unreachable, or (most likely) a shell/address problem.
  async function diagnose() {
    let detail;
    let line;
    try {
      const session = await ctx.api("/api/session");
      if (ws !== null || state !== "disconnected") return; // the user already started another attempt
      if (!session.authenticated) {
        detail = "You are signed out. Log in again.";
        line = "Signed out. Log in again, then the terminal reconnects.";
        lastClose = 4401;
        window.dispatchEvent(new Event("hub:unauthorized"));
      } else {
        detail = "The laptop refused the connection. Pick a shell from the list, or open the hub from its usual address.";
        line = "The laptop refused the connection (the shell name or the address was not accepted). Pick a shell and press Reconnect.";
      }
    } catch (error) {
      if (ws !== null || state !== "disconnected") return;
      detail = error.message || "Can't reach your laptop.";
      line = detail;
    }
    setStatus("disconnected", detail);
    statusDetail.classList.add("is-error");
    note(line, "31");
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  // ---------- input (with sticky Ctrl / Alt) ----------
  const keyButtons = {};
  function refreshMods() {
    for (const [name, on] of [["ctrl", ctrl], ["alt", alt]]) {
      const b = keyButtons[name];
      if (!b) continue;
      b.setAttribute("aria-pressed", String(on));
      b.classList.toggle("on", on);
    }
  }

  function modParam() { return 1 + (alt ? 2 : 0) + (ctrl ? 4 : 0); }

  function sendInput(data) {
    if (!data) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (state !== "connecting") statusDetail.textContent = "Not connected. Press Reconnect to start a new shell.";
      return;
    }
    let out = data;
    if ((ctrl || alt) && [...data].length === 1) {
      if (ctrl) {
        const code = controlCode(data);
        if (code !== null) out = code;
      }
      if (alt) out = "\x1b" + out;
    }
    if (ctrl || alt) { ctrl = alt = false; refreshMods(); }
    send({ type: "input", data: out });
  }

  // Keys that send an escape sequence and honour the sticky modifiers.
  function csiKey(final, tilde) {
    const m = modParam();
    if (tilde) return m > 1 ? `\x1b[${tilde};${m}~` : `\x1b[${tilde}~`;
    if (m > 1) return `\x1b[1;${m}${final}`;
    const appMode = term && term.modes && term.modes.applicationCursorKeysMode && "ABCD".includes(final);
    return appMode ? `\x1bO${final}` : `\x1b[${final}`;
  }
  // Multi-character sequences already carry their modifiers; sendInput then just clears the sticky keys.
  const sendSequence = (seq) => sendInput(seq);

  async function doPaste() {
    if (state !== "connected") { tell("Not connected. Press Reconnect first."); return; }
    try {
      if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error("no clipboard API");
      const text = await navigator.clipboard.readText();
      if (!text) { tell("The clipboard is empty."); return; }
      term.paste(text);
      term.focus();
    } catch (error) {
      pastePanel.hidden = false;
      pasteBox.value = "";
      pasteBox.focus();
      tell(ctx.secure ? "Clipboard access was blocked. Paste into the box instead."
        : "Clipboard needs HTTPS here. Paste into the box instead.");
      layoutSoon();
    }
  }

  async function doCopy() {
    let text = term.getSelection();
    let whole = false;
    if (!text) {
      // Touch screens can't drag-select in xterm, so fall back to what is on screen.
      const buf = term.buffer.active;
      const lines = [];
      for (let i = 0; i < term.rows; i++) {
        const line = buf.getLine(buf.viewportY + i);
        if (line) lines.push(line.translateToString(true));
      }
      text = lines.join("\n").replace(/\s+$/, "");
      whole = true;
    }
    if (!text) { tell("Nothing to copy."); return; }
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const area = el("textarea", { "aria-hidden": "true", readonly: true, class: "term-copy-scratch" });
      area.value = text;
      document.body.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (__) { ok = false; }
      area.remove();
      if (!ok) { tell("The browser blocked copying."); return; }
    }
    tell(whole ? "Nothing was selected, so the visible screen was copied." : "Copied the selection.");
  }

  // Copy the selection, or the last COPY_LINES lines of output (scrollback included).
  async function doCopyOutput() {
    let text = term.getSelection();
    if (!text) {
      const buf = term.buffer.active;
      const start = Math.max(0, buf.length - COPY_LINES);
      const lines = [];
      for (let i = start; i < buf.length; i++) {
        const line = buf.getLine(i);
        if (line) lines.push(line.translateToString(true));
      }
      text = lines.join("\n").replace(/\s+$/, "");
    }
    if (!text) { tell("Nothing to copy."); return; }
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const area = el("textarea", { "aria-hidden": "true", readonly: true, class: "term-copy-scratch" });
      area.value = text;
      document.body.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (__) { ok = false; }
      area.remove();
      if (!ok) { tell("The browser blocked copying."); return; }
    }
    tell(`Copied ${text.split("\n").length} lines of output.`);
  }

  function makeKey(label, aria, onPress, opts = {}) {
    const attrs = { type: "button", class: "ui-btn ui-btn--secondary term-key" + (opts.cls ? " " + opts.cls : ""), "aria-label": aria };
    if (opts.toggle) attrs["aria-pressed"] = "false";
    const b = el("button", attrs, label);
    let armed = null;
    b.addEventListener("pointerdown", (event) => {
      event.preventDefault(); // keeps focus in the terminal so the keyboard stays open
      armed = { id: event.pointerId, x: event.clientX, y: event.clientY };
      b.classList.add("down");
    });
    b.addEventListener("mousedown", (event) => event.preventDefault());
    b.addEventListener("pointermove", (event) => {
      if (armed && Math.hypot(event.clientX - armed.x, event.clientY - armed.y) > 12) { armed = null; b.classList.remove("down"); }
    });
    const cancel = () => { armed = null; b.classList.remove("down"); };
    b.addEventListener("pointercancel", cancel); // the bar started scrolling: not a press
    b.addEventListener("pointerleave", cancel);
    b.addEventListener("pointerup", (event) => {
      const ok = armed && armed.id === event.pointerId;
      cancel();
      if (!ok) return;
      event.preventDefault();
      onPress();
      if (!opts.noFocus) term.focus();
    });
    b.addEventListener("keydown", (event) => { // real keyboard / switch access: Enter or Space
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onPress(); }
    });
    b.addEventListener("contextmenu", (event) => event.preventDefault());
    return b;
  }

  function buildKeys() {
    const add = (name, node) => { keyButtons[name] = node; keybar.append(node); return node; };
    add("esc", makeKey("Esc", "Escape", () => sendSequence("\x1b")));
    add("tab", makeKey("Tab", "Tab", () => sendSequence("\t")));
    add("ctrl", makeKey("Ctrl", "Control, sticky", () => { ctrl = !ctrl; refreshMods(); }, { toggle: true }));
    add("alt", makeKey("Alt", "Alt, sticky", () => { alt = !alt; refreshMods(); }, { toggle: true }));
    add("up", makeKey("↑", "Arrow up", () => sendSequence(csiKey("A"))));
    add("down", makeKey("↓", "Arrow down", () => sendSequence(csiKey("B"))));
    add("left", makeKey("←", "Arrow left", () => sendSequence(csiKey("D"))));
    add("right", makeKey("→", "Arrow right", () => sendSequence(csiKey("C"))));
    add("home", makeKey("Home", "Home", () => sendSequence(csiKey("H"))));
    add("end", makeKey("End", "End", () => sendSequence(csiKey("F"))));
    add("pgup", makeKey("PgUp", "Page up", () => sendSequence(csiKey("", 5))));
    add("pgdn", makeKey("PgDn", "Page down", () => sendSequence(csiKey("", 6))));
    for (const ch of ["|", "~", "/", "-", "_", "\\"]) {
      keybar.append(makeKey(ch, `Type ${{ "|": "pipe", "~": "tilde", "/": "slash", "-": "dash", "_": "underscore", "\\": "backslash" }[ch]}`,
        () => sendInput(ch), { cls: "sym" }));
    }
    add("paste", makeKey(el("span", { class: "term-key-ico" }, icon("clipboard", 18), "Paste"), "Paste from clipboard", doPaste, { cls: "wide", noFocus: true }));
    add("output", makeKey(el("span", { class: "term-key-ico" }, icon("copy", 18), "Output"), "Copy output", doCopyOutput, { cls: "wide", noFocus: true }));
    add("copy", makeKey(el("span", { class: "term-key-ico" }, icon("copy", 18), "Copy"), "Copy selection", doCopy, { cls: "wide" }));
  }
  buildKeys();

  pasteSend.addEventListener("click", () => {
    const text = pasteBox.value;
    pastePanel.hidden = true;
    layoutSoon();
    if (text) term.paste(text);
    term.focus();
  });
  pasteCancel.addEventListener("click", () => { pastePanel.hidden = true; layoutSoon(); term.focus(); });

  // ---------- sizing ----------
  function usable() { return visible && host.clientWidth > 20 && host.clientHeight > 20; }

  function fitNow() {
    if (!term || !usable()) return false;
    try { fit.fit(); } catch (_) { return false; }
    return true;
  }

  function sendResize() {
    if (!term || !usable()) return;
    if (!fitNow()) return;
    const { rows, cols } = term;
    if (rows === lastSent.rows && cols === lastSent.cols) return;
    if (send({ type: "resize", rows, cols })) lastSent = { rows, cols };
  }

  function isAtBottom() { const b = term.buffer.active; return b.viewportY >= b.baseY; }

  function scheduleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const atBottom = !term || isAtBottom();
      layoutKeyboard(); sendResize();
      if (term && atBottom) term.scrollToBottom();
    }, 100);
  }

  // The phone keyboard shrinks only the visual viewport. When it does, pin this view to the
  // visible area so the terminal and the key bar stay on screen and the page does not scroll.
  function layoutKeyboard() {
    const vv = window.visualViewport;
    if (!visible || !vv) { container.classList.remove("kb-open"); container.style.cssText = ""; return; }
    const open = window.innerHeight - vv.height > KEYBOARD_THRESHOLD;
    container.classList.toggle("kb-open", open);
    if (open) {
      container.style.top = vv.offsetTop + "px";
      container.style.left = vv.offsetLeft + "px";
      container.style.width = vv.width + "px";
      container.style.height = vv.height + "px";
      if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
    } else {
      container.style.top = container.style.left = container.style.width = container.style.height = "";
    }
  }
  function layoutSoon() { requestAnimationFrame(() => { layoutKeyboard(); sendResize(); }); }

  window.addEventListener("resize", scheduleResize);
  window.addEventListener("orientationchange", scheduleResize);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => {
      const atBottom = !term || isAtBottom();
      layoutKeyboard();
      if (fitNow() && atBottom) term.scrollToBottom();
      scheduleResize();
    });
    window.visualViewport.addEventListener("scroll", () => { if (container.classList.contains("kb-open")) layoutKeyboard(); });
  }
  if (typeof ResizeObserver === "function") new ResizeObserver(scheduleResize).observe(host);

  // ---------- fonts ----------
  function setFont(size) {
    fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, size));
    store.set("hub.term.font", String(fontSize));
    fontLabel.textContent = String(fontSize);
    fontDown.disabled = fontSize <= FONT_MIN;
    fontUp.disabled = fontSize >= FONT_MAX;
    if (term) { term.options.fontSize = fontSize; scheduleResize(); }
  }
  fontDown.addEventListener("click", () => { setFont(fontSize - 1); term?.focus(); });
  fontUp.addEventListener("click", () => { setFont(fontSize + 1); term?.focus(); });
  setFont(fontSize);

  // ---------- session buttons ----------
  sessionBtn.addEventListener("click", () => {
    if (!term) return;
    if (state === "connected") {
      // Ending a live shell loses its state, so ask with a second tap instead of a modal.
      if (sessionBtn.dataset.confirm) { startSession(true); return; }
      sessionBtn.dataset.confirm = "1";
      sessionBtn.textContent = "Tap again to end this shell";
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => {
        sessionBtn.removeAttribute("data-confirm");
        if (state === "connected") sessionBtn.textContent = "New session";
      }, 3500);
    } else if (state === "disconnected") {
      startSession(true);
    }
  });

  select.addEventListener("change", () => {
    store.set("hub.term.shell", select.value);
    if (term && booted && state !== "connecting") startSession(true);
  });

  function startSession(fresh) {
    if (!term) return;
    statusDetail.classList.remove("is-error");
    connect(fresh);
    term.focus();
  }

  // ---------- shells ----------
  // Up to three shells show as a segmented control; the (hidden) select stays the single source of truth.
  let shellSeg = null;
  function buildShellSeg() {
    shellSeg?.remove();
    shellSeg = null;
    shellSlot.classList.toggle("has-seg", false);
    if (shells.length < 2 || shells.length > 3) return;
    shellSeg = segmented({ label: "Shell", value: select.value,
      options: shells.map((name) => ({ value: name, label: name })),
      onChange: (value) => { select.value = value; select.dispatchEvent(new Event("change")); } });
    shellSeg.classList.add("term-seg");
    shellSlot.append(shellSeg);
    shellSlot.classList.add("has-seg");
  }

  async function loadShells() {
    try {
      const status = await ctx.api("/api/status");
      shells = Array.isArray(status.shells) ? status.shells : [];
      shellsLoaded = true;
    } catch (error) {
      shellsLoaded = false;
      tell(error.message || "Could not load the list of shells.");
    }
    select.textContent = "";
    if (!shells.length) {
      select.append(el("option", { value: "" }, "Default shell"));
    } else {
      for (const name of shells) select.append(el("option", { value: name }, name));
      const saved = store.get("hub.term.shell");
      select.value = saved && shells.includes(saved) ? saved : shells[0];
    }
    buildShellSeg();
  }

  // ---------- touch scrolling ----------
  // xterm's canvas swallows touch drags, so turn vertical finger drags into scrollLines, with momentum.
  // A tap (no movement) still reaches the click handler; a long press is left to the system.
  function setupTouchScroll() {
    let t = null; // { y, lastY, lastT, v, scrolling, rem }
    let raf = 0;
    const lineH = () => Math.max(8, host.clientHeight / Math.max(1, term.rows));
    const stop = () => { cancelAnimationFrame(raf); raf = 0; };
    host.addEventListener("touchstart", (e) => {
      stop();
      if (e.touches.length !== 1) { t = null; return; }
      const y = e.touches[0].clientY;
      t = { x: e.touches[0].clientX, y, lastY: y, lastT: performance.now(), v: 0, scrolling: false, rem: 0 };
    }, { passive: true, capture: true });
    host.addEventListener("touchmove", (e) => {
      if (!t || e.touches.length !== 1) return;
      const y = e.touches[0].clientY;
      if (!t.scrolling) {
        if (Math.abs(y - t.y) < 8 || Math.abs(y - t.y) < Math.abs(e.touches[0].clientX - t.x)) return;
        t.scrolling = true;
      }
      if (e.cancelable) e.preventDefault();
      e.stopPropagation();
      const now = performance.now();
      const dy = t.lastY - y; // finger up = content moves up = scroll toward newer lines
      t.rem += dy / lineH();
      const whole = Math.trunc(t.rem);
      if (whole) { term.scrollLines(whole); t.rem -= whole; }
      const dt = Math.max(1, now - t.lastT);
      t.v = 0.8 * t.v + 0.2 * (dy / dt); // px per ms
      t.lastY = y; t.lastT = now;
    }, { passive: false, capture: true });
    const end = () => {
      if (!t) return;
      const cur = t; t = null;
      if (!cur.scrolling || performance.now() - cur.lastT > 120) return;
      let v = cur.v * 16; // px per frame
      let rem = 0;
      const step = () => {
        v *= 0.95;
        rem += v / lineH();
        const whole = Math.trunc(rem);
        if (whole) { term.scrollLines(whole); rem -= whole; }
        if (Math.abs(v) > 0.5) raf = requestAnimationFrame(step); else raf = 0;
      };
      raf = requestAnimationFrame(step);
    };
    host.addEventListener("touchend", end, { passive: true, capture: true });
    host.addEventListener("touchcancel", () => { t = null; }, { passive: true, capture: true });
  }

  // ---------- terminal creation ----------
  function createTerminal() {
    if (!window.Terminal || !window.FitAddon) {
      const msg = "The terminal library did not load. Check your connection and reload the page.";
      statusDetail.textContent = msg;
      statusDetail.classList.add("is-error");
      host.append(el("p", { class: "error term-fatal", role: "alert" }, msg));
      return false;
    }
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    term = new window.Terminal({
      fontSize, fontFamily: 'ui-monospace, "Cascadia Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace',
      cursorBlink: !reduced, scrollback: 5000, theme: buildTheme(), allowProposedApi: true,
      convertEol: false, macOptionIsMeta: true,
    });
    fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(host);
    if (term.textarea) {
      term.textarea.setAttribute("aria-label", "Terminal input");
      for (const [k, v] of [["autocapitalize", "off"], ["autocorrect", "off"], ["autocomplete", "off"], ["spellcheck", "false"]]) {
        term.textarea.setAttribute(k, v);
      }
    }
    term.onData((data) => sendInput(data));
    host.addEventListener("click", () => term.focus());
    setupTouchScroll();
    const syncBottom = () => { toBottom.hidden = isAtBottom(); };
    term.onScroll(syncBottom);
    term.onWriteParsed?.(syncBottom);
    toBottom.addEventListener("click", () => { term.scrollToBottom(); toBottom.hidden = true; term.focus(); });
    const retheme = () => { term.options.theme = buildTheme(); };
    window.matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", retheme);
    window.addEventListener("hub:theme", () => setTimeout(retheme, 30));
    return true;
  }

  // ---------- public handle ----------

  return {
    async show() {
      visible = true;
      if (!ready) {
        ready = (async () => {
          if (!createTerminal()) return false;
          await loadShells();
          return true;
        })();
      }
      if (!(await ready)) return;
      if (!visible) return; // the user moved on while the list loaded; connect when they return
      if (!booted) {
        booted = true;
        startSession(false);
        return;
      }
      layoutKeyboard();
      fitNow();
      sendResize();
      // After a re-login the old socket died with 4401: start again.
      if (state === "disconnected" && lastClose === 4401) startSession(true);
      else term.focus();
    },
    hide() {
      visible = false;
      container.classList.remove("kb-open");
      container.style.top = container.style.left = container.style.width = container.style.height = "";
      if (term) term.blur();
      // The socket and terminal stay alive on purpose: the shell survives tab switches.
    },
  };
}
