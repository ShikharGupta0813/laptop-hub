// Talk tab: chat with the assistant that can act on the laptop, by typing or by voice.
// Everything the model, the tools or files produce is put on screen with textContent or
// through renderMarkdown (sanitised). innerHTML is never fed raw text.
import { sse, renderMarkdown, el, ApiError } from "./api.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  send: "M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z",
  stop: "M7 7h10v10H7z",
  mic: "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v1a7 7 0 0 1-14 0v-1M12 18v4M8 22h8",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  sliders: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  speaker: "M11 5 6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14",
  terminal: "M4 17l6-6-6-6M12 19h8",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  folder: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
  wrench: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  chevron: "M6 9l6 6 6-6",
  down: "M12 5v14M19 12l-7 7-7-7",
  alert: "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
};

function icon(name, filled = false) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", "as-ico");
  svg.setAttribute("fill", filled ? "currentColor" : "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name] || ICONS.wrench);
  svg.append(path);
  return svg;
}

const SUGGESTIONS = [
  "What's using my disk space?",
  "Show my newest notes",
  "Is Ollama running?",
  "Summarise today's changes in my project",
];
const MAX_RECORDING_MS = 120000;
const OUTPUT_LIMIT = 20000;

// ---- small storage helpers (storage can throw in private windows) ----
const store = {
  get(area, key, fallback) {
    try { const v = window[area].getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
  },
  set(area, key, value) {
    try { window[area].setItem(key, value); } catch (_) { /* not remembered, still works */ }
  },
};

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
  return "wrench";
}

// What exactly will run, as label/text pairs for the approval card.
function approvalParts(input) {
  const parts = [];
  if (input && typeof input === "object") {
    if (typeof input.command === "string") {
      parts.push({ label: input.shell ? `Command (${input.shell})` : "Command", text: input.command });
    }
    if (typeof input.path === "string") parts.push({ label: "File", text: input.path });
    for (const key of ["content", "text", "body", "new_text"]) {
      if (typeof input[key] === "string") parts.push({ label: "Content (first lines)", text: firstLines(input[key], 8) });
    }
  }
  if (!parts.length) parts.push({ label: "Details", text: pretty(input) });
  return parts;
}

export function init(container, ctx) {
  let sessionId = store.get("sessionStorage", "hub.assistant.session", "") || newSessionId();
  store.set("sessionStorage", "hub.assistant.session", sessionId);

  const prefs = {
    provider: store.get("localStorage", "hub.assistant.provider", "claude") === "local" ? "local" : "claude",
    speak: store.get("localStorage", "hub.assistant.speak", "0") === "1",
    autoSend: store.get("localStorage", "hub.assistant.autosend", "1") !== "0",
    // Per browser session only (sessionStorage), never localStorage: off again after a restart.
    autoApprove: store.get("sessionStorage", "hub.assistant.autoapprove", "0") === "1",
  };

  let streaming = false;
  let controller = null;
  let turn = null;
  let stick = true;
  let visible = false;

  // ---------------------------------------------------------------- header
  const providerButtons = {};
  const providerGroup = el("div", { class: "as-seg", role: "radiogroup", "aria-label": "Which assistant answers" },
    ...[["claude", "Claude"], ["local", "Local"]].map(([id, label]) => {
      const b = el("button", { type: "button", role: "radio", "data-provider": id, onclick: () => setProvider(id) }, label);
      providerButtons[id] = b;
      return b;
    }));

  const optionsButton = el("button", {
    type: "button", class: "as-iconbtn", "aria-label": "Assistant options", "aria-expanded": "false",
    "aria-controls": "as-options", onclick: () => toggleOptions(),
  }, icon("sliders"));
  const newChatButton = el("button", {
    type: "button", class: "as-iconbtn", "aria-label": "New chat", title: "New chat", onclick: () => newChat(),
  }, icon("edit"));

  function makeSwitch(label, hint, onclick) {
    const sw = el("button", { type: "button", role: "switch", "aria-checked": "false", class: "as-switch", onclick },
      el("span", { class: "as-switch-text" }, el("span", { class: "as-switch-label" }, label),
        hint ? el("span", { class: "as-switch-hint" }, hint) : null),
      el("span", { class: "as-switch-track", "aria-hidden": "true" }, el("span", { class: "as-switch-knob" })));
    return sw;
  }
  const autoApproveSwitch = makeSwitch("Run commands without asking", "Off by default", () => onAutoApproveTap());
  const speakSwitch = makeSwitch("Read answers aloud", null, () => {
    prefs.speak = !prefs.speak;
    store.set("localStorage", "hub.assistant.speak", prefs.speak ? "1" : "0");
    if (!prefs.speak) stopSpeech();
    syncControls();
  });
  const autoSendSwitch = makeSwitch("Send automatically after I speak", null, () => {
    prefs.autoSend = !prefs.autoSend;
    store.set("localStorage", "hub.assistant.autosend", prefs.autoSend ? "1" : "0");
    syncControls();
  });
  const optionsPanel = el("div", { class: "as-options", id: "as-options", hidden: true },
    autoApproveSwitch, speakSwitch, autoSendSwitch);

  const header = el("div", { class: "as-head" },
    el("div", { class: "as-head-row" }, providerGroup, el("span", { class: "grow" }), optionsButton, newChatButton),
    optionsPanel);

  const autoBanner = el("div", { class: "as-autobanner", hidden: true, role: "status" },
    icon("alert"),
    el("span", { class: "grow" }, "Commands run without asking, and files can change."),
    el("button", { type: "button", class: "as-linkbtn", onclick: () => setAutoApprove(false) }, "Turn off"));

  // ---------------------------------------------------------------- list
  const list = el("div", {
    class: "as-list", role: "log", "aria-live": "polite", "aria-relevant": "additions text",
    "aria-label": "Conversation", tabindex: "0",
  });
  const empty = el("div", { class: "as-empty" },
    el("div", { class: "as-empty-icon" }, icon("mic")),
    el("h2", {}, "What can I do for you?"),
    el("p", { class: "muted" }, "Ask by voice or by typing. I can look around your laptop, run commands (I will ask first) and take notes."),
    el("div", { class: "as-suggest" },
      ...SUGGESTIONS.map((s) => el("button", {
        type: "button", class: "as-suggest-btn",
        onclick: () => { input.value = s; resizeInput(); syncSend(); input.focus(); },
      }, s))));
  const jump = el("button", {
    type: "button", class: "as-jump", hidden: true, onclick: () => scrollToBottom(true),
  }, icon("down"), "Jump to latest");
  const listWrap = el("div", { class: "as-listwrap" }, list, jump);

  // ---------------------------------------------------------------- composer
  const notice = el("div", { class: "as-notice", hidden: true, role: "status" });
  const recBar = el("div", { class: "as-recbar", hidden: true, role: "status" });
  const input = el("textarea", {
    class: "as-input", rows: "1", placeholder: "Ask or tell me something…", "aria-label": "Message",
    enterkeyhint: "enter", autocomplete: "off",
  });
  const micButton = el("button", {
    type: "button", class: "as-roundbtn as-mic", "aria-label": "Record a voice message", onclick: () => onMicTap(),
  }, icon("mic"));
  const sendButton = el("button", {
    type: "button", class: "as-roundbtn as-send", "aria-label": "Send message", onclick: () => send(input.value),
  }, icon("send"));
  const stopButton = el("button", {
    type: "button", class: "as-roundbtn as-stop", "aria-label": "Stop the assistant", hidden: true, onclick: () => stop(),
  }, icon("stop", true));
  const composer = el("div", { class: "as-composer" },
    notice, recBar, el("div", { class: "as-composer-row" }, micButton, input, sendButton, stopButton));

  // ---------------------------------------------------------------- confirm panel
  const confirmTitle = el("h2", { id: "as-confirm-title" }, "Run commands without asking?");
  const keepButton = el("button", { type: "button", class: "btn", onclick: () => closeConfirm() }, "Keep asking me");
  const allowButton = el("button", {
    type: "button", class: "btn danger", onclick: () => { closeConfirm(); setAutoApprove(true); },
  }, "Yes, run without asking");
  const confirmPanel = el("div", { class: "as-confirm", hidden: true },
    el("div", {
      class: "as-confirm-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "as-confirm-title",
    },
      confirmTitle,
      el("p", {}, "The assistant will then run commands and change files on your laptop without waiting for your tap."),
      el("p", { class: "muted" }, "It stays on until you turn it off, or until you restart the app. Only do this when you trust what you are asking for."),
      el("div", { class: "as-confirm-actions" }, keepButton, allowButton)));
  confirmPanel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeConfirm();
    if (event.key === "Tab") {
      const items = [keepButton, allowButton];
      const i = items.indexOf(document.activeElement);
      if (event.shiftKey && i <= 0) { event.preventDefault(); allowButton.focus(); }
      else if (!event.shiftKey && i === items.length - 1) { event.preventDefault(); keepButton.focus(); }
    }
  });

  const root = el("div", { class: "as-root" }, header, autoBanner, listWrap, composer, confirmPanel);
  container.replaceChildren(root);
  list.append(empty);

  // ---------------------------------------------------------------- prefs / controls
  function syncControls() {
    for (const [id, b] of Object.entries(providerButtons)) b.setAttribute("aria-checked", String(prefs.provider === id));
    autoApproveSwitch.setAttribute("aria-checked", String(prefs.autoApprove));
    speakSwitch.setAttribute("aria-checked", String(prefs.speak));
    autoSendSwitch.setAttribute("aria-checked", String(prefs.autoSend));
    autoBanner.hidden = !prefs.autoApprove;
  }

  function setProvider(id) {
    prefs.provider = id;
    store.set("localStorage", "hub.assistant.provider", id);
    syncControls();
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

  function onAutoApproveTap() {
    if (prefs.autoApprove) { setAutoApprove(false); return; }
    openConfirm();
  }
  function openConfirm() {
    confirmPanel.hidden = false;
    keepButton.focus();
  }
  function closeConfirm() {
    confirmPanel.hidden = true;
    autoApproveSwitch.focus();
  }

  function stopSpeech() {
    try { if ("speechSynthesis" in window) window.speechSynthesis.cancel(); } catch (_) { /* ignore */ }
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
  function syncSend() {
    sendButton.disabled = streaming || micState !== "idle" || !input.value.trim();
  }
  input.addEventListener("input", () => { resizeInput(); syncSend(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(input.value); }
  });

  function setBusy(busy) {
    streaming = busy;
    sendButton.hidden = busy;
    stopButton.hidden = !busy;
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

  function addUser(text) {
    return append(el("div", { class: "as-msg as-user" }, el("div", { class: "as-bubble" }, text)));
  }

  function addError(message) {
    return append(el("div", { class: "as-msg as-errmsg", role: "alert" },
      el("div", { class: "as-bubble" }, icon("alert"), el("span", {}, message))));
  }

  function addNote(message) {
    return append(el("div", { class: "as-note muted" }, message));
  }

  function makeBubble() {
    const node = el("div", { class: "as-bubble markdown", "aria-busy": "true" });
    append(el("div", { class: "as-msg as-assistant" }, node));
    return { node, raw: "", timer: null, last: 0 };
  }

  function flushBubble(b) {
    if (b.timer) { clearTimeout(b.timer); b.timer = null; }
    b.last = performance.now();
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
    if (t.bubble.timer || t.bubble.dirty) flushBubble(t.bubble);
    t.bubble.node.removeAttribute("aria-busy");
    t.bubble = null;
  }

  // ---------------------------------------------------------------- tool cards
  function makeCard(t, id, name, input) {
    const stateText = el("span", { class: "as-tool-statetext" }, "Running");
    const stateBox = el("span", { class: "as-tool-state" }, el("span", { class: "as-spinner", "aria-hidden": "true" }), stateText);
    const chevron = icon("chevron");
    chevron.classList.add("as-chevron");
    const detail = el("div", { class: "as-tool-detail", hidden: true });
    const head = el("button", { type: "button", class: "as-tool-head", "aria-expanded": "false" },
      el("span", { class: "as-tool-icon" }, icon(toolIcon(name))),
      el("span", { class: "as-tool-main" },
        el("span", { class: "as-tool-name" }, name || "tool"),
        el("span", { class: "as-tool-sum" }, summarise(input))),
      stateBox, chevron);
    const approvalBox = el("div", { class: "as-approval", hidden: true });
    const node = el("div", { class: "as-tool", "data-state": "running" }, head, approvalBox, detail);
    const card = { id, name, input, node, head, stateBox, stateText, approvalBox, detail, outputPre: null, answering: false, done: false };
    head.addEventListener("click", () => {
      const open = detail.hidden;
      detail.hidden = !open;
      head.setAttribute("aria-expanded", String(open));
      if (open) scrollToBottom();
    });
    fillDetail(card);
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
    const box = card.stateBox;
    box.replaceChildren();
    if (state === "running" || state === "approval") box.append(el("span", { class: "as-spinner", "aria-hidden": "true" }));
    else if (state === "ok") box.append(icon("check"));
    else box.append(icon(state === "denied" ? "x" : "alert"));
    card.stateText = el("span", { class: "as-tool-statetext" }, label);
    box.append(card.stateText);
  }

  function getCard(t, ev) {
    return t.cards.get(ev.id) || makeCard(t, ev.id, ev.name, ev.input);
  }

  function showApproval(t, card) {
    const box = card.approvalBox;
    box.hidden = false;
    setCardState(card, "approval", "Needs OK");
    const what = card.name === "run_command" ? "run this command on your laptop" : `use “${card.name}”`;
    const approve = el("button", { type: "button", class: "btn as-approve", onclick: () => answer(t, card, true) }, "Approve");
    const deny = el("button", { type: "button", class: "btn as-deny", onclick: () => answer(t, card, false) }, "Deny");
    const status = el("div", { class: "as-approval-status", role: "status" },
      el("span", { class: "as-spinner", "aria-hidden": "true" }), "Waiting for your OK");
    box.replaceChildren(
      el("div", { class: "as-approval-title" }, icon("alert"), `The assistant wants to ${what}`),
      ...approvalParts(card.input).flatMap((p) => [
        el("div", { class: "as-label" }, p.label),
        el("pre", { class: "as-pre as-code" }, p.text),
      ]),
      status,
      el("div", { class: "as-approval-actions" }, approve, deny));
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Approval needed");
    card.ui = { approve, deny, status };
    t.pending = card;
    scrollToBottom(true);
    if (!visible) ctx.toast("The assistant is waiting for your OK on the Talk tab.", 6000);
  }

  function resolveApprovalUi(card, text) {
    if (!card.ui) return;
    card.ui.approve.disabled = true;
    card.ui.deny.disabled = true;
    card.ui.status.replaceChildren(text);
    card.node.classList.add("answered");
  }

  async function postApprove(sid, callId, approve) {
    return ctx.api("/api/assistant/approve", { method: "POST", json: { session_id: sid, call_id: callId, approve } });
  }

  async function answer(t, card, approve) {
    if (card.answering || !card.ui) return;
    card.answering = true;
    card.ui.approve.disabled = true;
    card.ui.deny.disabled = true;
    card.ui.status.replaceChildren(el("span", { class: "as-spinner", "aria-hidden": "true" }), "Sending your answer…");
    try {
      await postApprove(t.sid, card.id, approve);
      if (t.pending === card) t.pending = null;
      if (!card.done) resolveApprovalUi(card, approve ? "You approved this. Running…" : "You declined this.");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (t.pending === card) t.pending = null;
        resolveApprovalUi(card, "This request is no longer waiting for an answer (it may have timed out).");
      } else {
        card.answering = false;
        card.ui.approve.disabled = false;
        card.ui.deny.disabled = false;
        card.ui.status.replaceChildren(el("span", { class: "as-inline-err", role: "alert" },
          `Your answer did not reach the laptop: ${error.message} Tap Approve or Deny to try again.`));
      }
    }
  }

  function finishCard(card, ev) {
    card.done = true;
    const output = ev.output == null ? "" : String(ev.output);
    const shown = output.length > OUTPUT_LIMIT ? output.slice(0, OUTPUT_LIMIT) + `\n… (${output.length - OUTPUT_LIMIT} more characters not shown)` : output;
    card.outputPre = el("pre", { class: "as-pre as-output" }, shown || "(no output)");
    fillDetail(card);
    if (ev.denied) {
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
        showApproval(t, getCard(t, ev));
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
            type: "button", class: "as-filechip", onclick: () => ctx.openFile(ev.root, ev.path),
          }, icon("file"), el("span", { class: "as-filechip-text" }, `Open ${label}`))));
        break;
      }
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
  }

  async function send(raw) {
    const text = (raw || "").trim();
    if (!text || streaming) return;
    if (micState === "recording") cancelRecording();
    showNotice("");
    stopSpeech();
    const userNode = addUser(text);
    input.value = "";
    resizeInput();
    scrollToBottom(true);

    const t = {
      sid: sessionId, bubble: null, bubbles: [], cards: new Map(), pending: null, typing: null,
      gotEvent: false, gotDone: false, stopped: false, hadError: false,
    };
    turn = t;
    controller = new AbortController();
    const signal = controller.signal;
    setBusy(true);
    t.typing = append(el("div", { class: "as-msg as-assistant as-typing", "aria-label": "The assistant is working" },
      el("div", { class: "as-bubble" }, el("span", { class: "as-dots", "aria-hidden": "true" }, el("i"), el("i"), el("i")))));

    try {
      for await (const ev of sse("/api/assistant/message",
        { session_id: t.sid, text, provider: prefs.provider, auto_approve: prefs.autoApprove }, signal)) {
        t.gotEvent = true;
        handleEvent(t, ev);
      }
      if (!t.gotDone && !t.stopped && !signal.aborted) {
        if (t.typing) { t.typing.remove(); t.typing = null; }
        addError("The connection to your laptop ended before the assistant finished. The answer above may be incomplete.");
      }
    } catch (error) {
      if (t.typing) { t.typing.remove(); t.typing = null; }
      if (!signal.aborted) {
        if (error instanceof ApiError) {
          if (!t.gotEvent) { userNode.remove(); input.value = text; resizeInput(); }
          addError(error.message);
        } else {
          addError("The connection to your laptop dropped while the assistant was answering. The answer above may be incomplete. Check that the laptop is awake, then try again.");
        }
      }
    } finally {
      finishTurn(t);
    }
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
      if (prefs.speak && spoken && !t.stopped && !t.hadError) ctx.speak(spoken);
    }
  }

  async function stop() {
    const t = turn;
    if (!t || !streaming) return;
    t.stopped = true;
    stopButton.disabled = true;
    t.pending = null;
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
    store.set("sessionStorage", "hub.assistant.session", sessionId);
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
      ctx.toast(`Started a new chat, but the laptop could not forget the old one: ${error.message}`, 6000);
    }
  }

  // ---------------------------------------------------------------- voice
  let micState = "idle"; // idle | recording | transcribing
  let recorder = null;
  let stream = null;
  let chunks = [];
  let recStart = 0;
  let recTimer = null;
  let recCancelled = false;

  function setMicState(state) {
    micState = state;
    const rec = state === "recording";
    micButton.classList.toggle("is-recording", rec);
    micButton.replaceChildren(icon(rec ? "stop" : "mic", rec));
    micButton.setAttribute("aria-label", rec ? "Stop recording" : "Record a voice message");
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
        el("button", { type: "button", class: "as-linkbtn", onclick: () => cancelRecording() }, "Cancel"));
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
  syncControls();
  syncSend();
  const micIssue = micProblem();
  if (micIssue) micButton.classList.add("is-unavailable");

  return {
    show() {
      visible = true;
      fitToKeyboard();
      if (stick) requestAnimationFrame(() => scrollToBottom());
    },
    hide() {
      visible = false;
      if (micState === "recording") {
        cancelRecording(true);
        ctx.toast("Recording cancelled because you left the Talk tab.");
      }
    },
  };
}
