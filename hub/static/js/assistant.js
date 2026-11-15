// Talk tab: chat with the assistant that can act on the laptop, by typing, dictating or in hands-free voice mode.
// Everything the model, the tools or files produce is put on screen with textContent or
// through renderMarkdown (sanitised). innerHTML is never fed raw text.
import { sse, renderMarkdown, el, ApiError } from "./api.js";
import * as ui from "./ui.js";
import { startLaneRun, waitForRun, resultSheet } from "./lanepicker.js";
import { defaultProvider, loadProviders, providerLabel } from "./providers.js";
import { aiButton, normalizeChoice, isLaneChoice, laneOf, chooseLane, loadChoices, councilSetupSheet, COUNCIL_OFF } from "./aichat.js";

const SUGGESTIONS = [
  "What's using my disk space?",
  "Show my newest notes",
  "Is Ollama running?",
  "Summarise today's changes in my project",
];
const MAX_RECORDING_MS = 120000;
const OUTPUT_LIMIT = 20000;

function loadCss() {
  if (document.querySelector('link[data-css="assistant"]')) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/css/assistant.css";
  link.dataset.css = "assistant";
  document.head.append(link);
}

// ---- small storage helpers (storage can throw in private windows) ----
const store = {
  get(area, key, fallback) {
    try { const v = window[area].getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
  },
  set(area, key, value) {
    try { window[area].setItem(key, value); } catch (_) { /* not remembered, still works */ }
  },
};

// The AI chosen for each conversation ("AI for this chat"): {sessionId: choice}, newest last, 40 kept. The last choice
// also becomes the start for the next new chat.
const AI_MAP_KEY = "hub.assistant.chatai";
function readAiMap() {
  try { const m = JSON.parse(store.get("localStorage", AI_MAP_KEY, "{}")); return m && typeof m === "object" && !Array.isArray(m) ? m : {}; } catch (_) { return {}; }
}
function rememberAi(sid, choice) {
  const map = readAiMap();
  delete map[sid];
  map[sid] = choice;
  for (const old of Object.keys(map).slice(0, Math.max(0, Object.keys(map).length - 40))) delete map[old];
  store.set("localStorage", AI_MAP_KEY, JSON.stringify(map));
  store.set("localStorage", "hub.assistant.provider", choice);
}

function newSessionId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return "s" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function clockTime(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function firstLines(text, n) {
  const lines = String(text).split("\n");
  return lines.length > n ? lines.slice(0, n).join("\n") + `\n… (${lines.length - n} more lines)` : lines.join("\n");
}

function pretty(value) {
  try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
}

function oneLine(text, max = 140) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max) + "…" : flat;
}

function summarise(input) {
  if (input == null || typeof input !== "object") return input == null ? "" : oneLine(input);
  for (const key of ["command", "path", "query", "name", "title", "text", "content"]) {
    if (typeof input[key] === "string" && input[key]) return oneLine(input[key]);
  }
  const keys = Object.keys(input);
  if (!keys.length) return "";
  const first = input[keys[0]];
  return oneLine(typeof first === "string" ? first : pretty(input));
}

function toolIcon(name) {
  const n = String(name || "").toLowerCase();
  if (n.includes("command") || n.includes("shell") || n.includes("run")) return "terminal";
  if (n.includes("dir") || n.includes("list") || n.includes("folder")) return "folder";
  if (n.includes("write") || n.includes("note") || n.includes("save") || n.includes("edit")) return "edit";
  if (n.includes("file") || n.includes("read") || n.includes("search")) return "file";
  return "zap";
}

// What exactly will run, as label/text pairs for the approval card.
// The approval card must show EVERYTHING that will happen, so the owner is never approving text they
// cannot see: every field, in full, in the order that reads best.
const FIELD_LABELS = {
  to: "To", resolved_to: "Sends to (resolved address)", subject: "Subject", body: "Message", space: "Chat space",
  text: "Text", gid: "Task id", command: "Command", shell: "Shell", cwd: "Folder", path: "File", content: "Content",
  append: "Append", name: "Name", action: "Action", params: "Settings", id: "Id", tags: "Tags", timeout_seconds: "Timeout (s)",
};
const FIELD_ORDER = ["to", "resolved_to", "subject", "space", "gid", "action", "name", "shell", "cwd", "command",
  "path", "append", "params", "body", "text", "content"];

function approvalParts(input) {
  const parts = [];
  if (input && typeof input === "object") {
    const keys = [...FIELD_ORDER.filter((k) => k in input), ...Object.keys(input).filter((k) => !FIELD_ORDER.includes(k))];
    for (const key of keys) {
      const value = input[key];
      const text = typeof value === "string" ? value : pretty(value);
      const label = key === "command" && input.shell ? `Command (${input.shell})` : FIELD_LABELS[key] || key;
      parts.push({ label, text });
    }
  }
  if (!parts.length) parts.push({ label: "Details", text: pretty(input) });
  return parts;
}


const STATE_BADGE = {
  running: ["Running", "accent"], approval: ["Needs OK", "warn"], ok: ["Done", "good"],
  failed: ["Failed", "bad"], denied: ["Declined", "warn"],
};

export function init(container, ctx) {
  loadCss();
  // One id per conversation, kept across app restarts (localStorage) so the hub can reload the saved conversation (hub/chatstore.py).
  let sessionId = store.get("localStorage", "hub.assistant.session", "") || store.get("sessionStorage", "hub.assistant.session", "") || newSessionId();
  store.set("localStorage", "hub.assistant.session", sessionId);

  const prefs = {
    provider: normalizeChoice(readAiMap()[sessionId] || store.get("localStorage", "hub.assistant.provider", "") || defaultProvider()),
    speak: store.get("localStorage", "hub.assistant.speak", "1") !== "0",
    autoSend: store.get("localStorage", "hub.assistant.autosend", "1") !== "0",
    // Per browser session only (sessionStorage), never localStorage: off again after a restart.
    autoApprove: store.get("sessionStorage", "hub.assistant.autoapprove", "0") === "1",
  };

  let streaming = false;
  let controller = null;
  let turn = null;
  let stick = true;
  let visible = false;
  let voice = null; // the open voice-mode controller, if any
  let fullControl = false; // hub-wide Full Control: no approval prompts at all (fullcontrol.js keeps this in step)

  // ---------------------------------------------------------------- toolbar (no header chrome: the top bar is the header)
  const providerSeg = aiButton({ value: prefs.provider, ctx, onChange: (v) => setProvider(v) });
  const optionsButton = ui.button({
    variant: "ghost", iconOnly: true, icon: "sliders", "aria-label": "Assistant options", "aria-expanded": "false",
    "aria-controls": "as-options", onClick: () => toggleOptions(),
  });
  const newChatButton = ui.button({
    variant: "ghost", iconOnly: true, icon: "edit", "aria-label": "New chat", "data-tip": "New chat", onClick: () => newChat(),
  });

  const autoApproveSwitch = ui.switchControl({
    checked: prefs.autoApprove, label: "Run commands without asking", onChange: (on) => onAutoApproveToggle(on),
  });
  const speakSwitch = ui.switchControl({
    checked: prefs.speak, label: "Read answers aloud", onChange: (on) => {
      prefs.speak = on;
      store.set("localStorage", "hub.assistant.speak", on ? "1" : "0");
      if (!on) stopSpeech();
    },
  });
  const autoSendSwitch = ui.switchControl({
    checked: prefs.autoSend, label: "Send automatically after I speak", onChange: (on) => {
      prefs.autoSend = on;
      store.set("localStorage", "hub.assistant.autosend", on ? "1" : "0");
    },
  });
  function optionRow(title, sub, sw) {
    return el("label", { class: "ui-row as-opt" },
      el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, title), sub ? el("span", { class: "ui-row__sub" }, sub) : null), sw);
  }
  const optionsPanel = el("div", { class: "as-options ui-card ui-card--flat ui-card--pad-0", id: "as-options", hidden: true },
    el("div", { class: "ui-list" },
      optionRow("Run commands without asking", "Off by default. Asks every time.", autoApproveSwitch),
      optionRow("Read answers aloud", null, speakSwitch),
      optionRow("Send automatically after I speak", "For the mic button", autoSendSwitch)));

  const toolbar = el("div", { class: "as-bar" }, providerSeg.el, optionsButton, newChatButton);

  const autoBannerText = el("span", { class: "grow" }, "Commands run without asking, and files can change.");
  const autoBannerBtn = ui.button({ variant: "ghost", size: "sm", label: "Turn off", onClick: () => setAutoApprove(false) });
  const autoBanner = el("div", { class: "as-autobanner", hidden: true, role: "status" }, ui.icon("alert"), autoBannerText, autoBannerBtn);

  // ---------------------------------------------------------------- list
  const list = el("div", {
    class: "as-list", role: "log", "aria-live": "polite", "aria-relevant": "additions text",
    "aria-label": "Conversation", tabindex: "0",
  });
  const empty = el("div", { class: "as-empty ui-enter" },
    el("div", { class: "as-empty-orb", "aria-hidden": "true" }, ui.icon("sparkles")),
    el("h2", {}, "What can I do for you?"),
    el("p", { class: "muted" }, "Type, dictate, or start a hands-free conversation. I can look around your laptop, run commands (I always ask first) and take notes."),
    el("div", { class: "as-suggest" },
      ...SUGGESTIONS.map((s) => el("button", { type: "button", class: "ui-chip as-suggest-chip", onclick: () => send(s) }, s))),
    ui.button({ variant: "tonal", icon: "waveform", label: "Start voice mode", onClick: () => openVoice() }));
  const jump = ui.button({
    variant: "secondary", size: "sm", iconOnly: true, icon: "arrow-down", class: "as-jump", hidden: true,
    "aria-label": "Scroll to latest", onClick: () => scrollToBottom(true),
  });
  const listWrap = el("div", { class: "as-listwrap" }, list, jump);

  // ---------------------------------------------------------------- composer
  const notice = el("div", { class: "as-notice", hidden: true, role: "status" });
  const recBar = el("div", { class: "as-recbar", hidden: true, role: "status" });
  const input = el("textarea", {
    class: "as-input", rows: "1", placeholder: "Message your laptop…", "aria-label": "Message",
    enterkeyhint: "enter", autocomplete: "off",
  });
  const micButton = ui.button({
    variant: "ghost", iconOnly: true, icon: "mic", class: "as-mic", "aria-label": "Dictate a message", onClick: () => onMicTap(),
  });
  const sendButton = ui.button({
    variant: "primary", iconOnly: true, icon: "arrow-up", class: "as-send", "aria-label": "Send message", onClick: () => send(input.value),
  });
  const voiceButton = ui.button({
    variant: "tonal", iconOnly: true, icon: "waveform", class: "as-voice", "aria-label": "Start hands-free voice mode", onClick: () => openVoice(),
  });
  const stopButton = ui.button({
    variant: "danger", iconOnly: true, icon: "stop", class: "as-stop", "aria-label": "Stop the assistant", hidden: true, onClick: () => stop(),
  });
  // Friday entry point: opens her screen (brief, plan, laptop prep).
  const fridayChip = el("button", { type: "button", class: "ui-chip as-friday", onclick: () => ctx.open("friday") },
    ui.icon("sparkles"), "Friday");
  const pill = el("div", { class: "as-pill" }, micButton, input, voiceButton, sendButton, stopButton);
  // Work lanes (docs/api/lanes.md) are a separate, explicit action: "Send to lane" hands the written message to one lane
  // and posts the answer in the chat (the result sheet is only an optional detail). Which AI answers normally is the
  // "AI for this chat" button in the toolbar.
  const laneChip = el("button", { type: "button", class: "ui-chip as-lane", onclick: () => sendToLane() }, ui.icon("share"), "Send to lane");
  const composer = el("div", { class: "as-composer" }, notice, recBar, el("div", { class: "as-friday-row" }, fridayChip, laneChip), pill);

  // The core: Jarvis = cyan orb, Friday = amber orb. It follows whoever answered last (persona routing is server-side,
  // by a leading name), pulses while the assistant works and shows a waveform line while words stream in.
  const coreOrb = el("span", { class: "ui-core as-core__orb", "data-persona": "jarvis", "data-state": "idle", "aria-hidden": "true" });
  const coreName = el("span", { class: "as-core__name" }, "Jarvis");
  const coreStatus = el("span", { class: "as-core__status" }, "Ready");
  const coreWave = el("span", { class: "ui-wave as-core__wave", "aria-hidden": "true" }, ...Array.from({ length: 7 }, () => el("i")));
  const core = el("div", { class: "as-core", "data-persona": "jarvis", "data-state": "idle", role: "status", "aria-live": "off" },
    coreOrb, el("div", { class: "as-core__txt" }, coreName, coreStatus), coreWave);
  let curPersona = "jarvis";
  const PERSONA_NAME = { jarvis: "Jarvis", friday: "Friday" };
  function setCore(state, persona) {
    if (persona) {
      curPersona = persona === "friday" ? "friday" : "jarvis";
      core.dataset.persona = curPersona; coreOrb.dataset.persona = curPersona; coreName.textContent = PERSONA_NAME[curPersona];
    }
    if (state) {
      core.dataset.state = state; coreOrb.dataset.state = state;
      coreStatus.textContent = { idle: "Ready", thinking: "Working…", speaking: "Answering", listening: "Listening" }[state] || "Ready";
    }
  }
  // "Friday, …" / "Jarvis, …" route on the server; mirror that so the right core lights up
  function personaFor(text, explicit) {
    if (explicit === "friday" || explicit === "jarvis") return explicit;
    const m = /^\s*(?:(?:hey|ok|okay)[,\s]+)?(friday|jarvis)\b/i.exec(text || "");
    return m ? m[1].toLowerCase() : "jarvis";
  }

  const root = el("div", { class: "as-root" }, core, toolbar, optionsPanel, autoBanner, listWrap, composer);
  container.replaceChildren(root);
  list.append(empty);
  // No saved choice: use the hub's default AI (Claude key, Gemini, Claude login or local) once it is known.
  if (!store.get("localStorage", "hub.assistant.provider", "")) {
    loadProviders().then(() => { if (!readAiMap()[sessionId]) { prefs.provider = normalizeChoice(defaultProvider()); providerSeg.value = prefs.provider; } });
  }

  // ---------------------------------------------------------------- prefs / controls
  function syncControls() {
    providerSeg.value = prefs.provider;
    autoApproveSwitch.input.checked = prefs.autoApprove;
    speakSwitch.input.checked = prefs.speak;
    autoSendSwitch.input.checked = prefs.autoSend;
    autoBanner.hidden = !prefs.autoApprove && !fullControl;
    autoBannerText.textContent = fullControl ? "FULL CONTROL is on: everything runs without asking." : "Commands run without asking, and files can change.";
    autoBannerBtn.onclick = () => (fullControl ? import("./fullcontrol.js").then((fc) => fc.turnOff()) : setAutoApprove(false));
  }

  function setProvider(id) {
    prefs.provider = id;
    rememberAi(sessionId, id);                 // remembered for THIS conversation, and as the start of the next one
    input.placeholder = "Message your laptop…";
  }

  function toggleOptions(force) {
    const open = force ?? optionsPanel.hidden;
    optionsPanel.hidden = !open;
    optionsButton.setAttribute("aria-expanded", String(open));
  }

  function setAutoApprove(on) {
    prefs.autoApprove = on;
    store.set("sessionStorage", "hub.assistant.autoapprove", on ? "1" : "0");
    syncControls();
  }

  async function onAutoApproveToggle(on) {
    if (!on) { setAutoApprove(false); return; }
    syncControls(); // stays off until the owner confirms
    const yes = await ui.confirm({
      title: "Run commands without asking?",
      message: "The assistant will run commands and change files on your laptop without waiting for your tap. It stays on until you turn it off, or until you restart the app. Only do this when you trust what you are asking for.",
      confirmLabel: "Run without asking", cancelLabel: "Keep asking me", danger: true,
    });
    setAutoApprove(!!yes);
  }

  function stopSpeech() {
    try { if ("speechSynthesis" in window) window.speechSynthesis.cancel(); } catch (_) { /* ignore */ }
    import("./hubtts.js").then((m) => m.stopReply()).catch(() => {});
  }

  // ---------------------------------------------------------------- scrolling
  function nearBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight < 70;
  }
  function scrollToBottom(force = false) {
    if (force) stick = true;
    if (stick) list.scrollTop = list.scrollHeight;
    jump.hidden = stick;
  }
  list.addEventListener("scroll", () => {
    stick = nearBottom();
    jump.hidden = stick;
  }, { passive: true });

  // Keep the composer above the on-screen keyboard. Chrome on Android only shrinks the
  // visual viewport, so measure how much of the layout viewport the keyboard covers.
  function fitToKeyboard() {
    const vv = window.visualViewport;
    if (!vv) return;
    const covered = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    const tabs = document.getElementById("tabs");
    const tabsHeight = tabs ? tabs.offsetHeight : 0;
    const shrink = covered > 80 ? Math.max(0, covered - tabsHeight) : 0;
    root.style.height = shrink ? `calc(100% - ${shrink}px)` : "";
    if (shrink && document.activeElement === input) {
      requestAnimationFrame(() => { scrollToBottom(true); input.scrollIntoView({ block: "nearest" }); });
    } else if (stick) {
      scrollToBottom();
    }
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitToKeyboard);
    window.visualViewport.addEventListener("scroll", fitToKeyboard);
  }

  // ---------------------------------------------------------------- composer behaviour
  function resizeInput() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 144) + "px";
  }
  // Right-hand slot of the pill: stop while working, send when there is text, otherwise the voice-mode button.
  function syncSend() {
    const has = !!input.value.trim();
    sendButton.disabled = streaming || micState !== "idle" || !has;
    sendButton.hidden = streaming || !has;
    voiceButton.hidden = streaming || has;
    stopButton.hidden = !streaming;
  }
  input.addEventListener("input", () => { resizeInput(); syncSend(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(input.value); }
  });

  function setBusy(busy) {
    streaming = busy;
    setCore(busy ? "thinking" : "idle");
    micButton.disabled = busy || micState === "transcribing";
    root.classList.toggle("is-busy", busy);
    list.setAttribute("aria-busy", String(busy));
    syncSend();
  }

  function showNotice(text) {
    notice.textContent = text;
    notice.hidden = !text;
  }

  // ---------------------------------------------------------------- message rendering
  function append(node) {
    if (!empty.hidden) empty.hidden = true;
    list.append(node);
    scrollToBottom();
    return node;
  }

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      ui.toast("Copied", { kind: "good" });
    } catch (_) {
      // Plain-http pages have no clipboard API: fall back to a temporary selection.
      try {
        const ta = el("textarea", { class: "as-copybuf", "aria-hidden": "true" });
        ta.value = text;
        document.body.append(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        ui.toast(ok ? "Copied" : "Could not copy", { kind: ok ? "good" : "bad" });
      } catch (_2) { ui.toast("Could not copy", { kind: "bad" }); }
    }
    if (btn) btn.blur();
  }

  function addUser(text) {
    return append(el("div", { class: "as-msg as-user" }, el("div", { class: "as-bubble" }, text)));
  }

  function addError(message) {
    return append(el("div", { class: "as-msg as-errmsg", role: "alert" },
      el("div", { class: "as-bubble" }, ui.icon("alert-circle"), el("span", {}, message))));
  }

  function addNote(message) {
    return append(el("div", { class: "as-note" }, message));
  }

  function makeBubble() {
    const node = el("div", { class: "as-bubble markdown", "aria-busy": "true" });
    const wrap = el("div", { class: "as-msg as-assistant", "data-persona": curPersona }, node);
    append(wrap);
    setCore("speaking");
    return { node, wrap, raw: "", timer: null, last: 0 };
  }

  function flushBubble(b) {
    if (b.timer) { clearTimeout(b.timer); b.timer = null; }
    b.last = performance.now();
    b.dirty = false;
    b.node.innerHTML = renderMarkdown(b.raw); // sanitised by DOMPurify inside renderMarkdown
    for (const a of b.node.querySelectorAll("a")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
    scrollToBottom();
  }
  function queueBubble(b) {
    if (b.timer) return;
    b.timer = setTimeout(() => flushBubble(b), Math.max(0, 100 - (performance.now() - b.last)));
  }
  function closeBubble(t) {
    if (!t.bubble) return;
    const b = t.bubble;
    if (b.timer || b.dirty) flushBubble(b);
    b.node.removeAttribute("aria-busy");
    const copy = ui.button({
      variant: "ghost", size: "sm", iconOnly: true, icon: "copy", class: "as-copy", "aria-label": "Copy message",
      onClick: () => copyText(b.raw, copy),
    });
    b.wrap.append(copy);
    t.bubble = null;
  }

  // ---------------------------------------------------------------- tool cards
  function makeCard(t, id, name, input) {
    const badge = el("span", { class: "as-tool-badge" });
    const chevron = ui.icon("chevron-down", { class: "as-chevron" });
    const detail = el("div", { class: "as-tool-detail", hidden: true });
    const head = el("button", { type: "button", class: "as-tool-head", "aria-expanded": "false" },
      ui.tile(toolIcon(name), "gray", "sm"),
      el("span", { class: "as-tool-main" },
        el("span", { class: "as-tool-name" }, name || "tool"),
        el("span", { class: "as-tool-sum" }, summarise(input))),
      badge, chevron);
    const approvalBox = el("div", { class: "as-approval", hidden: true });
    const node = el("div", { class: "as-tool ui-card ui-card--flat ui-card--pad-0", "data-state": "running" }, head, approvalBox, detail);
    const card = { id, name, input, node, head, badge, approvalBox, detail, outputPre: null, answering: false, done: false };
    head.addEventListener("click", () => {
      const open = detail.hidden;
      detail.hidden = !open;
      head.setAttribute("aria-expanded", String(open));
      if (open) scrollToBottom();
    });
    fillDetail(card);
    setCardState(card, "running", "Running");
    append(el("div", { class: "as-msg as-toolrow" }, node));
    t.cards.set(id, card);
    return card;
  }

  function fillDetail(card) {
    card.detail.replaceChildren(
      el("div", { class: "as-label" }, "Input"),
      el("pre", { class: "as-pre" }, pretty(card.input)),
    );
    if (card.outputPre) card.detail.append(el("div", { class: "as-label" }, "Output"), card.outputPre);
  }

  function setCardState(card, state, label) {
    card.node.dataset.state = state;
    const [defText, tone] = STATE_BADGE[state] || STATE_BADGE.running;
    const b = ui.badge(label || defText, tone);
    if (state === "running") b.prepend(el("span", { class: "as-spinner", "aria-hidden": "true" }));
    card.badge.replaceChildren(b);
  }

  function getCard(t, ev) {
    return t.cards.get(ev.id) || makeCard(t, ev.id, ev.name, ev.input);
  }

  // `ev` is the approval_needed event: ev.critical (level N3) makes this a red "Critical action" card that needs the
  // typed word CONFIRM and then a cancellable countdown; ev.can_remember (level N2) adds "Allow this for 1 hour".
  function showApproval(t, card, ev = {}) {
    const box = card.approvalBox;
    box.hidden = false;
    card.critical = ev.critical === true;
    card.level = ev.level || "N2";
    setCardState(card, "approval", card.critical ? "Critical" : "Needs OK");
    box.classList.toggle("as-approval--critical", card.critical);
    const what = card.name === "run_command" ? "run this command on your laptop" : `use “${card.name}”`;
    const confirmWord = ev.confirm_word || "CONFIRM";
    const typed = card.critical ? el("input", { class: "as-confirm", type: "text", autocomplete: "off", autocapitalize: "characters",
      spellcheck: "false", placeholder: `Type ${confirmWord} to enable`, "aria-label": `Type ${confirmWord} to approve this critical action` }) : null;
    const approve = ui.button({ variant: "danger", icon: "check", label: card.critical ? "Confirm and start countdown" : "Approve and run", class: "as-approve",
      onClick: () => answer(t, card, true, card.critical ? { confirm_text: typed.value.trim() } : {}) });
    const deny = ui.button({ variant: "secondary", icon: "x", label: "Deny", class: "as-deny", onClick: () => answer(t, card, false) });
    const syncConfirm = () => { if (typed && !card.answering) approve.disabled = typed.value.trim() !== confirmWord; };
    if (typed) { approve.disabled = true; typed.addEventListener("input", syncConfirm); }
    const status = el("div", { class: "as-approval-status", role: "status" },
      el("span", { class: "as-spinner", "aria-hidden": "true" }), card.critical ? `Waiting for you to type ${confirmWord}` : "Waiting for your OK");
    const parts = approvalParts(card.input);
    const shownParts = card.critical && ev.command && !parts.some((p) => p.text === ev.command)
      ? [{ label: "Exact command", text: String(ev.command) }, ...parts] : parts;
    const remember = !card.critical && ev.can_remember === true ? el("div", { class: "as-remember" },
      ui.button({ variant: "ghost", size: "sm", icon: "clock", label: "Allow this for 1 hour", class: "as-remember-hour", onClick: () => answer(t, card, true, { remember: "hour" }) }),
      ui.button({ variant: "ghost", size: "sm", icon: "clock", label: "for this session", class: "as-remember-session", onClick: () => answer(t, card, true, { remember: "session" }) })) : null;
    box.replaceChildren(
      card.critical ? el("div", { class: "as-critical", role: "alert" }, ui.icon("alert"),
        el("div", {}, el("strong", {}, "Critical action"),
          el("p", {}, "This can lose data or take the laptop away. It never runs on its own, even in Full Control. "
            + `Type ${confirmWord}, then you get ${ev.delay_seconds || 10} seconds to cancel.`))) : null,
      el("div", { class: "as-approval-title" }, ui.icon("shield"), `The assistant wants to ${what}`),
      ...shownParts.flatMap((p) => [
        el("div", { class: "as-label" }, p.label),
        el("pre", { class: "as-pre as-code" }, p.text),
      ]),
      typed,
      status,
      el("div", { class: "as-approval-actions" }, deny, approve),
      remember);
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", card.critical ? "Critical action: confirmation needed" : "Approval needed");
    card.ui = { approve, deny, status, typed, syncConfirm, remember };
    card.respond = (ok) => (card.critical && ok ? Promise.resolve() : answer(t, card, ok));   // a spoken yes never confirms a critical action
    t.pending = card;
    scrollToBottom(true);
    if (t.voice) t.voice.onApproval?.(card);
    else if (!visible) ctx.toast("The assistant is waiting for your OK on the Talk tab.", { kind: "warn" });
    if (typed && !t.voice) setTimeout(() => typed.focus({ preventScroll: true }), 50);
  }

  // The server's 10 second delay after CONFIRM: "Running in 10... Cancel". The server enforces it; this only shows it.
  function startCountdown(t, card, ev) {
    if (!card.ui) return;
    clearCountdown(card);
    let left = Math.max(1, Math.round(ev.seconds || 10));
    const text = el("span", {});
    const cancel = ui.button({ variant: "secondary", size: "sm", icon: "x", label: "Cancel", class: "as-cancel", onClick: async () => {
      cancel.disabled = true;
      try { await ctx.api("/api/assistant/cancel", { method: "POST", json: { session_id: t.sid, call_id: card.id } }); text.textContent = "Cancelled. Nothing was run."; clearCountdown(card); }
      catch (error) { cancel.disabled = false; text.textContent = `Could not cancel: ${error.message}`; }
    } });
    card.cancelBtn = cancel;
    const paint = () => { text.textContent = `Running in ${left}…`; };
    paint();
    card.ui.status.replaceChildren(text, cancel);
    card.ui.approve.disabled = true;
    card.countdownTimer = setInterval(() => { left -= 1; if (left <= 0) { clearCountdown(card); text.textContent = "Running…"; cancel.disabled = true; } else paint(); }, 1000);
  }

  function clearCountdown(card) {
    if (card.countdownTimer) { clearInterval(card.countdownTimer); card.countdownTimer = null; }
  }

  function resolveApprovalUi(card, text) {
    if (!card.ui) return;
    clearCountdown(card);
    card.ui.remember?.remove();
    card.ui.approve.disabled = true;
    card.ui.deny.disabled = true;
    card.ui.status.replaceChildren(text);
    card.node.classList.add("answered");
    if (turn && turn.voice && card.approvalShown) { card.approvalShown = false; turn.voice.onApprovalDone?.(card); }
  }

  async function postApprove(sid, callId, approve, extra = {}) {
    return ctx.api("/api/assistant/approve", { method: "POST", json: { session_id: sid, call_id: callId, approve, ...extra } });
  }

  async function answer(t, card, approve, extra = {}) {
    if (card.answering || !card.ui) return;
    card.answering = true;
    card.ui.approve.disabled = true;
    card.ui.deny.disabled = true;
    card.ui.status.replaceChildren(el("span", { class: "as-spinner", "aria-hidden": "true" }), "Sending your answer…");
    try {
      await postApprove(t.sid, card.id, approve, extra);
      if (t.pending === card) t.pending = null;
      if (card.critical && approve) {
        // Confirmed: the countdown event follows from the server. Keep the card open so it can be cancelled.
        card.ui.status.replaceChildren(el("span", { class: "as-spinner", "aria-hidden": "true" }), "Confirmed. Starting the countdown…");
        card.ui.typed.disabled = true;
        t.voice?.onApprovalDone?.(card);
        return;
      }
      if (!card.done) resolveApprovalUi(card, approve ? (extra.remember ? "You approved this and it is remembered (Settings > Active permissions). Running…" : "You approved this. Running…") : "You declined this.");
      t.voice?.onApprovalDone?.(card);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (t.pending === card) t.pending = null;
        resolveApprovalUi(card, "This request is no longer waiting for an answer (it may have timed out).");
        t.voice?.onApprovalDone?.(card);
      } else {
        card.answering = false;
        card.ui.approve.disabled = false;
        card.ui.deny.disabled = false;
        card.ui.syncConfirm?.();
        card.ui.status.replaceChildren(el("span", { class: "as-inline-err", role: "alert" },
          `Your answer did not reach the laptop: ${error.message} Tap Approve or Deny to try again.`));
        t.voice?.onApprovalError?.(card, error);
      }
    }
  }

  function finishCard(card, ev) {
    card.done = true;
    clearCountdown(card);
    const output = ev.output == null ? "" : String(ev.output);
    const shown = output.length > OUTPUT_LIMIT ? output.slice(0, OUTPUT_LIMIT) + `\n… (${output.length - OUTPUT_LIMIT} more characters not shown)` : output;
    card.outputPre = el("pre", { class: "as-pre as-output" }, shown || "(no output)");
    fillDetail(card);
    if (ev.blocked === "taint") {
      setCardState(card, "failed", "Blocked: untrusted content was read");
    } else if (ev.denied) {
      setCardState(card, "denied", "Declined");
      resolveApprovalUi(card, "Declined. Nothing was run.");
    } else if (ev.ok) {
      setCardState(card, "ok", "Done");
      if (card.ui) resolveApprovalUi(card, "Approved and finished.");
    } else {
      setCardState(card, "failed", "Failed");
      if (card.ui) resolveApprovalUi(card, "Approved, but it failed. Open the card for details.");
    }
    card.approvalBox.hidden = !card.ui;
  }

  // ---------------------------------------------------------------- turn handling
  function handleEvent(t, ev) {
    if (t.typing) { t.typing.remove(); t.typing = null; }
    switch (ev.type) {
      case "text": {
        if (!t.bubble) { t.bubble = makeBubble(); t.bubbles.push(t.bubble); }
        t.bubble.raw += ev.t || "";
        t.bubble.dirty = true;
        queueBubble(t.bubble);
        break;
      }
      case "tool_start": {
        closeBubble(t);
        const card = getCard(t, ev);
        if (ev.needs_approval && !card.ui) setCardState(card, "running", "Waiting");
        break;
      }
      case "approval_needed": {
        closeBubble(t);
        const card = getCard(t, ev);
        card.approvalShown = true;
        showApproval(t, card, ev);
        break;
      }
      case "countdown": {
        startCountdown(t, getCard(t, ev), ev);
        break;
      }
      case "tool_result": {
        closeBubble(t);
        const card = getCard(t, ev);
        finishCard(card, ev);
        if (t.pending === card) t.pending = null;
        break;
      }
      case "file_ref": {
        closeBubble(t);
        const label = ev.name || ev.path;
        append(el("div", { class: "as-msg as-fileref" },
          el("button", {
            type: "button", class: "ui-chip as-filechip", onclick: () => { voice?.close(); ctx.openFile(ev.root, ev.path); },
          }, ui.icon("file"), el("span", { class: "as-filechip-text" }, `Open ${label}`))));
        break;
      }
      case "note":
        if (ev.text) showNotice(ev.text);
        break;
      case "answered_by": {
        // under the reply: which AI really wrote it (a not-ready pick falls back to the next one that works)
        const picked = t.picked || prefs.provider;
        const last = t.bubbles[t.bubbles.length - 1];
        const same = !ev.provider || picked === "auto" || picked === ev.provider || isLaneChoice(picked);
        const caption = el("div", { class: "as-by", "data-provider": ev.provider || "" },
          el("span", {}, `answered by ${ev.label || providerLabel(ev.provider)}` + (same ? "" : ` (${providerLabel(picked)} wasn't available)`)),
          ev.detail ? el("button", { type: "button", class: "as-by__link", onclick: ev.detail }, "Details") : null);
        (last ? last.wrap : list.lastElementChild || list).after(caption);
        t.by = caption;
        scrollToBottom();
        break;
      }
      case "stopped":
        t.stopped = true;
        break;
      case "error": {
        closeBubble(t);
        t.hadError = true;
        addError(ev.message || "Something went wrong.");
        break;
      }
      case "done":
        t.gotDone = true;
        break;
      default:
        break;
    }
    if (t.voice) t.voice.onEvent?.(ev, t);
  }

  // `vh` (optional) are the hooks voice mode passes in: {onEvent, onApproval, onApprovalDone, onApprovalError}.
  // Resolves with {text, stopped, hadError} once the turn is over.
  // `lane` (optional) = hand THIS message to that work lane ("Send to lane"); otherwise the AI chosen for this chat answers,
  // and a lane choice ("Council", "Local deep thinking") also answers through its lane. Either way the reply lands in the chat.
  async function send(raw, vh = null, persona = "auto", lane = "") {
    const text = (raw || "").trim();
    if (!text || streaming) return null;
    const viaLane = lane || (isLaneChoice(prefs.provider) ? laneOf(prefs.provider) : "");
    if (micState === "recording") cancelRecording();
    showNotice("");
    stopSpeech();
    const userNode = addUser(text);
    if (!vh) { input.value = ""; resizeInput(); }
    scrollToBottom(true);

    const t = {
      sid: sessionId, bubble: null, bubbles: [], cards: new Map(), pending: null, typing: null, voice: vh,
      gotEvent: false, gotDone: false, stopped: false, hadError: false, picked: viaLane ? `lane:${viaLane}` : prefs.provider,
    };
    turn = t;
    controller = new AbortController();
    const signal = controller.signal;
    setBusy(true);
    setCore(null, personaFor(text, persona));
    t.typing = append(el("div", { class: "as-msg as-assistant as-typing", "data-persona": curPersona, "aria-label": "The assistant is working" },
      el("div", { class: "as-bubble" }, el("span", { class: "as-dots", "aria-hidden": "true" }, el("i"), el("i"), el("i")))));

    try {
      const payload = { session_id: t.sid, text, provider: prefs.provider, auto_approve: prefs.autoApprove, persona, voice: !!vh };
      if (viaLane) await laneTurn(t, text, viaLane, signal);
      else for (let attempt = 1; ; attempt++) {
        try {
          for await (const ev of sse("/api/assistant/message", payload, signal)) {
            t.gotEvent = true;
            handleEvent(t, ev);
          }
          break;
        } catch (error) {
          // Voice SSE dropped before a single event arrived (a phone switching networks): try once more, quietly.
          if (!(vh && attempt === 1 && !t.gotEvent && !signal.aborted && !(error instanceof ApiError))) throw error;
          await new Promise((r) => setTimeout(r, 700));
        }
      }
      if (!t.gotDone && !t.stopped && !signal.aborted) {
        if (t.typing) { t.typing.remove(); t.typing = null; }
        t.hadError = true;
        addError("The connection to your laptop ended before the assistant finished. The answer above may be incomplete.");
      }
    } catch (error) {
      if (t.typing) { t.typing.remove(); t.typing = null; }
      t.hadError = true;
      if (!signal.aborted) {
        if (error instanceof ApiError) {
          if (!t.gotEvent) { userNode.remove(); if (!vh) { input.value = text; resizeInput(); } }
          addError(error.message);
        } else {
          addError("The connection to your laptop dropped while the assistant was answering. The answer above may be incomplete. Check that the laptop is awake, then try again.");
        }
        t.errorMessage = error.message;
      }
    } finally {
      finishTurn(t);
    }
    return { text: t.bubbles.map((b) => b.raw).join("\n\n").trim(), stopped: t.stopped, hadError: t.hadError, error: t.errorMessage };
  }

  // A work-lane answer, posted into the chat exactly like a streamed reply (so it is spoken when voice is on, and voice mode
  // hears it). Council runs in the background: the typing dots stay until it finishes, and Stop cancels it.
  async function laneTurn(t, text, laneId, signal) {
    t.gotEvent = true;                              // the user's message stays on screen whatever happens next
    const fail = (message) => { handleEvent(t, { type: "error", message }); handleEvent(t, { type: "done" }); };
    let label = laneId === "council" ? "Council" : laneId === "local-reasoning" ? "Local deep thinking" : laneId;
    try {
      const choice = (await loadChoices(true)).find((c) => c.id === `lane:${laneId}`);
      if (choice) label = choice.label;
      if (choice && !choice.ready) {
        if (t.typing) { t.typing.remove(); t.typing = null; }
        if (laneId === "council") {
          append(el("div", { class: "as-msg as-noterow" }, el("div", { class: "as-note" }, `${COUNCIL_OFF}.`),
            ui.button({ label: "How to set up", icon: "help", variant: "tonal", size: "sm", onClick: () => councilSetupSheet(ctx) })));
          t.hadError = true; t.gotDone = true;
        } else fail(`${label} can't answer right now: ${choice.reason || "it isn't ready"}`);
        return;
      }
      let run = await startLaneRun({ task: text, lane: laneId });
      if (run.running) {
        showNotice(`${label} is working in the background. This can take a few minutes; the answer will appear here.`);
        try { run = await waitForRun(run, { stopped: () => signal.aborted || t.stopped }); } finally { showNotice(""); }
      }
      if (signal.aborted || t.stopped) {
        if (run?.id && run.running !== false) ctx.api(`/api/lanes/runs/${run.id}/cancel`, { method: "POST", json: {} }).catch(() => {});
        t.gotDone = true;
        return;
      }
      if (run.status === "done" && run.answer) {
        handleEvent(t, { type: "text", t: run.answer });
        handleEvent(t, { type: "answered_by", provider: `lane:${laneId}`, label: run.lane && run.lane !== laneId ? `${label} (${run.lane})` : label,
          detail: () => resultSheet(run) });
        handleEvent(t, { type: "done" });
      } else fail(run.note || `${label} could not finish.`);
    } catch (error) {
      if (!signal.aborted) fail(error?.message || `${label} could not answer.`); else t.gotDone = true;
    }
  }

  // "Send to lane": one explicit hand-over of the written message. The chat AI stays as it was.
  async function sendToLane() {
    const text = input.value.trim();
    if (!text) { ui.toast("Write the message first, then choose a lane.", { kind: "warn" }); return; }
    if (streaming) return;
    const laneId = await chooseLane({ ctx });
    if (laneId) send(text, null, "auto", laneId);
  }

  function finishTurn(t) {
    if (t.typing) { t.typing.remove(); t.typing = null; }
    closeBubble(t);
    for (const card of t.cards.values()) {
      if (!card.done) {
        setCardState(card, "failed", t.stopped ? "Stopped" : "No result");
        if (card.ui) resolveApprovalUi(card, t.stopped ? "Stopped before it ran." : "No answer was received.");
      }
    }
    if (t.stopped) addNote("Stopped.");
    const spoken = t.bubbles.map((b) => b.node.textContent.trim()).filter(Boolean).join(" ");
    if (turn === t) {
      turn = null;
      controller = null;
      setBusy(false);
      if (!t.voice && prefs.speak && spoken && !t.stopped && !t.hadError) {
        // the assistant's hub voice when the owner picked one (Settings > Voices), else the phone's, as before
        import("./hubtts.js").then((m) => m.speakReply(spoken, curPersona, (x) => ctx.speak(x))).catch(() => ctx.speak(spoken));
      }
    }
  }

  async function stop() {
    const t = turn;
    if (!t || !streaming) return;
    t.stopped = true;
    stopButton.disabled = true;
    t.pending = null;
    // Tell the laptop too: it cancels the generation or the tool wait and denies what waits for a tap.
    try { await ctx.api("/api/assistant/stop", { method: "POST", json: { session_id: t.sid } }); } catch (_) { /* the abort below still ends the stream */ }
    for (const card of t.cards.values()) {
      if (!card.ui || card.done || card.answering) continue;
      card.answering = true;
      try { await postApprove(t.sid, card.id, false); } catch (_) { /* the stream is closing anyway */ }
      setCardState(card, "denied", "Declined");
      resolveApprovalUi(card, "You stopped the assistant, so this was declined.");
    }
    if (controller) controller.abort();
    stopButton.disabled = false;
  }

  async function newChat() {
    const old = sessionId;
    if (streaming) await stop();
    cancelRecording(true);
    stopSpeech();
    sessionId = newSessionId();
    store.set("localStorage", "hub.assistant.session", sessionId);
    rememberAi(sessionId, prefs.provider);      // a new chat starts with the AI you were using
    // A fresh chat starts safe: commands need a tap again.
    if (prefs.autoApprove) { prefs.autoApprove = false; store.set("sessionStorage", "hub.assistant.autoapprove", "0"); syncControls(); }
    list.replaceChildren(empty);
    empty.hidden = false;
    stick = true;
    jump.hidden = true;
    showNotice("");
    input.value = "";
    resizeInput();
    syncSend();
    try {
      await ctx.api("/api/assistant/reset", { method: "POST", json: { session_id: old } });
    } catch (error) {
      ctx.toast(`Started a new chat, but the laptop could not forget the old one: ${error.message}`, { kind: "warn" });
    }
  }

  // ---------------------------------------------------------------- hands-free voice mode (js/voicemode.js)
  let voiceLoading = false;
  async function openVoice(opts = {}) {
    if (voice || voiceLoading) return;
    voiceLoading = true;
    try {
      if (micState === "recording") cancelRecording(true);
      const mod = await import("./voicemode.js");
      voice = mod.openVoiceMode({
        ctx, ui, friday: !!opts.friday,
        provider: () => prefs.provider,
        isBusy: () => streaming,
        ask: (text, hooks, persona) => send(text, hooks, persona || "auto"),
        stopTurn: () => stop(),
        newChat: () => newChat(),
        approvalParts,
        onClose: () => { voice = null; },
      });
    } catch (error) {
      ctx.toast(`Voice mode could not start: ${error.message}`, { kind: "bad" });
    } finally {
      voiceLoading = false;
    }
  }

  // ---------------------------------------------------------------- dictation (mic button)
  let micState = "idle"; // idle | recording | transcribing
  let recorder = null;
  let stream = null;
  let chunks = [];
  let recStart = 0;
  let recTimer = null;
  let recCancelled = false;

  function setMicState(state) {
    const wasRec = micState === "recording";
    micState = state;
    if (wasRec !== (state === "recording")) window.dispatchEvent(new CustomEvent("hub:mic", { detail: { on: state === "recording" } }));   // wake word pauses meanwhile
    const rec = state === "recording";
    micButton.classList.toggle("is-recording", rec);
    micButton.replaceChildren(ui.icon(rec ? "stop" : "mic"));
    micButton.setAttribute("aria-label", rec ? "Stop recording" : "Dictate a message");
    micButton.disabled = state === "transcribing" || streaming;
    recBar.hidden = state === "idle";
    syncSend();
  }

  function micProblem() {
    if (!ctx.secure) {
      return "The microphone only works on the secure (HTTPS) address of your laptop, and this page was opened on a plain one. Open the https:// address instead. You can keep typing meanwhile.";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof window.MediaRecorder === "undefined") {
      return "This browser cannot record audio here. You can keep typing.";
    }
    return "";
  }

  async function onMicTap() {
    if (micState === "recording") { stopRecording(); return; }
    if (micState !== "idle" || streaming) return;
    const problem = micProblem();
    if (problem) { showNotice(problem); return; }
    showNotice("");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      if (error && (error.name === "NotAllowedError" || error.name === "SecurityError")) {
        showNotice("Microphone permission was denied. Tap the lock icon next to the address, allow the microphone for this site, then try again. You can keep typing.");
      } else if (error && error.name === "NotFoundError") {
        showNotice("No microphone was found on this phone.");
      } else {
        showNotice(`Could not start the microphone: ${(error && error.message) || error}`);
      }
      return;
    }
    const preferred = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
    const mime = preferred.find((m) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || "";
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch (error) {
      releaseStream();
      showNotice(`Could not start recording: ${error.message}`);
      return;
    }
    chunks = [];
    recCancelled = false;
    recorder.addEventListener("dataavailable", (e) => { if (e.data && e.data.size) chunks.push(e.data); });
    recorder.addEventListener("stop", () => onRecorded(recorder && recorder.mimeType));
    recorder.addEventListener("error", () => {
      showNotice("The recording failed. Please try again.");
      cancelRecording(true);
    });
    recorder.start();
    recStart = performance.now();
    setMicState("recording");
    renderRecBar();
    recTimer = setInterval(() => {
      const elapsed = performance.now() - recStart;
      renderRecBar();
      if (elapsed >= MAX_RECORDING_MS) {
        ctx.toast("Reached the 2 minute limit, so I stopped recording.");
        stopRecording();
      }
    }, 250);
  }

  function renderRecBar() {
    if (micState === "recording") {
      recBar.replaceChildren(
        el("span", { class: "as-recdot", "aria-hidden": "true" }),
        el("span", { class: "grow" }, `Recording ${clockTime(performance.now() - recStart)} of 2:00. Tap the mic to stop.`),
        ui.button({ variant: "ghost", size: "sm", label: "Cancel", onClick: () => cancelRecording() }));
    } else if (micState === "transcribing") {
      recBar.replaceChildren(el("span", { class: "as-spinner", "aria-hidden": "true" }), el("span", { class: "grow" }, "Turning your voice into text…"));
    }
  }

  function releaseStream() {
    if (stream) { for (const track of stream.getTracks()) track.stop(); stream = null; }
  }

  function stopRecording() {
    clearInterval(recTimer);
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  function cancelRecording(silent = false) {
    if (micState !== "recording") return;
    recCancelled = true;
    clearInterval(recTimer);
    const r = recorder;
    if (r && r.state !== "inactive") r.stop();
    releaseStream();
    recorder = null;
    chunks = [];
    setMicState("idle");
    if (!silent) showNotice("Recording cancelled.");
  }

  async function onRecorded(mime) {
    clearInterval(recTimer);
    releaseStream();
    recorder = null;
    if (recCancelled) return;
    const blob = new Blob(chunks, { type: mime || "audio/webm" });
    chunks = [];
    if (!blob.size) {
      setMicState("idle");
      showNotice("Nothing was recorded. Please try again.");
      return;
    }
    setMicState("transcribing");
    renderRecBar();
    const form = new FormData();
    const ext = /mp4/.test(blob.type) ? "m4a" : "webm";
    form.append("file", blob, `voice.${ext}`);
    let result;
    try {
      result = await ctx.api("/api/transcribe", { method: "POST", form });
    } catch (error) {
      setMicState("idle");
      showNotice(`Could not turn your voice into text: ${error.message}`);
      return;
    }
    setMicState("idle");
    const spoken = ((result && result.text) || "").trim();
    if (!spoken) { showNotice("I did not catch any words. Try again, a little closer to the microphone."); return; }
    input.value = input.value.trim() ? `${input.value.trim()} ${spoken}` : spoken;
    resizeInput();
    syncSend();
    if (prefs.autoSend) send(input.value);
    else input.focus();
  }

  // ---------------------------------------------------------------- start
  import("./fullcontrol.js").then((fc) => { fc.refresh(); fc.onChange((on) => { fullControl = on; syncControls(); }); }).catch(() => {});
  syncControls();
  syncSend();
  if (micProblem()) micButton.classList.add("is-unavailable");

  return {
    show() {
      visible = true;
      fitToKeyboard();
      if (stick) requestAnimationFrame(() => scrollToBottom());
    },
    hide() {
      visible = false;
      if (voice) voice.close();
      if (micState === "recording") {
        cancelRecording(true);
        ctx.toast("Recording cancelled because you left the Talk tab.");
      }
    },
    openSection(name) {
      if (name === "chat") {   // the Jarvis screen hands a typed command over (hub.assistant.prefill) when its own inbox is not set up
        let pre = "";
        try { pre = sessionStorage.getItem("hub.assistant.prefill") || ""; sessionStorage.removeItem("hub.assistant.prefill"); } catch (_) { /* ignore */ }
        if (pre) { input.value = pre; resizeInput(); syncSend(); input.focus(); }
      } else if (name === "voice") openVoice();
      else if (name === "friday") openVoice({ friday: true }); // the Friday screen's mic: greets in her voice
    },
  };
}
