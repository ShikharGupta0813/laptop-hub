// Terminal tab: a real shell on the laptop, drawn with xterm.js (loaded as globals by index.html).
// The session lives as long as the page: hide() keeps the socket and the terminal alive.
import { el } from "./api.js";

const FONT_MIN = 10;
const FONT_MAX = 22;
const FONT_DEFAULT = 14;
const CONNECT_TIMEOUT_MS = 10000;
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
  const light = window.matchMedia("(prefers-color-scheme: light)").matches;
  const accent = cssVar("--accent") || "#4fb3ff";
  return {
    background: cssVar("--bg") || "#0f1217",
    foreground: cssVar("--text") || "#e8edf4",
    cursor: accent,
    cursorAccent: cssVar("--bg") || "#0f1217",
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
  const sessionBtn = el("button", { type: "button", class: "btn term-session" }, "New session");
  const fontDown = el("button", { type: "button", class: "btn term-font-btn", "aria-label": "Smaller text" }, "A−");
  const fontUp = el("button", { type: "button", class: "btn term-font-btn", "aria-label": "Larger text" }, "A+");
  const fontLabel = el("span", { class: "term-font-size", "aria-hidden": "true" }, String(fontSize));
  const toolbar = el("div", { class: "term-toolbar" },
    select, sessionBtn,
    el("div", { class: "term-font", role: "group", "aria-label": "Text size" }, fontDown, fontLabel, fontUp));

  const dot = el("span", { class: "term-dot", "aria-hidden": "true" });
  const statusText = el("span", { class: "term-status-text" }, "Disconnected");
  const statusDetail = el("span", { class: "term-status-detail muted" });
  const statusLine = el("div", { class: "term-status", role: "status", "aria-live": "polite" }, dot, statusText, statusDetail);

  const host = el("div", { class: "term-host" });
  const screen = el("div", { class: "term-screen" }, host);

  // paste fallback panel (only shown when the clipboard can't be read directly)
  const pasteBox = el("textarea", {
    class: "term-paste-box", rows: "3", "aria-label": "Text to paste into the terminal",
    placeholder: "Long-press here and choose Paste", autocapitalize: "off", autocomplete: "off", spellcheck: "false",
  });
  const pasteSend = el("button", { type: "button", class: "btn primary" }, "Send");
  const pasteCancel = el("button", { type: "button", class: "btn" }, "Cancel");
  const pastePanel = el("div", { class: "term-paste", hidden: true },
    el("p", { class: "muted term-paste-note" }, "This browser would not let the page read the clipboard. Paste here instead."),
    pasteBox, el("div", { class: "row" }, pasteSend, pasteCancel));

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

    socket.onopen = () => {
      if (ws !== socket) return;
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
      } else if (event.code === 1006 || event.code === 1001 || event.code === 1011 || event.code === 1012) {
        detail = "Connection lost. Is the laptop awake and the hub running?";
        line = "Connection lost. Press Reconnect to start a new shell (the old shell's state is gone).";
      } else {
        detail = "Session ended (press Reconnect).";
        line = "Session ended (press Reconnect). A new connection starts a fresh shell.";
      }
      setStatus("disconnected", kept || detail);
      if (kept) statusDetail.classList.add("is-error"); else statusDetail.classList.toggle("is-error", event.code !== 1000);
      if (!gotError) note(line, event.code === 1000 || event.code === 1005 ? "33" : "31");
    };
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
    if (state !== "connected") { ctx.toast("Not connected. Press Reconnect first."); return; }
    try {
      if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error("no clipboard API");
      const text = await navigator.clipboard.readText();
      if (!text) { ctx.toast("The clipboard is empty."); return; }
      term.paste(text);
      term.focus();
    } catch (error) {
      pastePanel.hidden = false;
      pasteBox.value = "";
      pasteBox.focus();
      ctx.toast(ctx.secure ? "Clipboard access was blocked. Paste into the box instead."
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
    if (!text) { ctx.toast("Nothing to copy."); return; }
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
      if (!ok) { ctx.toast("The browser blocked copying."); return; }
    }
    ctx.toast(whole ? "Nothing was selected, so the visible screen was copied." : "Copied the selection.");
  }

  function makeKey(label, aria, onPress, opts = {}) {
    const attrs = { type: "button", class: "term-key" + (opts.cls ? " " + opts.cls : ""), "aria-label": aria };
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
    add("paste", makeKey("Paste", "Paste from clipboard", doPaste, { cls: "wide", noFocus: true }));
    add("copy", makeKey("Copy", "Copy selection", doCopy, { cls: "wide" }));
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

  function scheduleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { layoutKeyboard(); sendResize(); }, 100);
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
    window.visualViewport.addEventListener("resize", scheduleResize);
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
  async function loadShells() {
    try {
      const status = await ctx.api("/api/status");
      shells = Array.isArray(status.shells) ? status.shells : [];
      shellsLoaded = true;
    } catch (error) {
      shellsLoaded = false;
      ctx.toast(error.message || "Could not load the list of shells.");
    }
    select.textContent = "";
    if (!shells.length) {
      select.append(el("option", { value: "" }, "Default shell"));
    } else {
      for (const name of shells) select.append(el("option", { value: name }, name));
      const saved = store.get("hub.term.shell");
      select.value = saved && shells.includes(saved) ? saved : shells[0];
    }
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
    window.matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => {
      term.options.theme = buildTheme();
    });
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
