// Learn > Chat: ask questions about a source (or nothing), streamed from the laptop's model.
// Model text is shown with textContent or renderMarkdown (sanitised); never raw innerHTML.
import { api, sse, renderMarkdown, el } from "./api.js";
import { createSourcePicker, createProviderSwitch, icon, store } from "./sourcepicker.js";

const STARTERS = ["Explain this simply", "What are the key points?", "Test me with 3 questions", "Give an example"];
const SESSION_KEY = "hub.learn.chat.session";
const LOG_KEY = "hub.learn.chat.log";
const SOURCE_KEY = "hub.learn.chat.source";
const RENDER_EVERY_MS = 100; // about 10 markdown renders per second while streaming
const MAX_SAVED = 40;

function newSessionId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return "l" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function init(container, ctx) {
  let sessionId = store.get("sessionStorage", SESSION_KEY, "") || newSessionId();
  store.set("sessionStorage", SESSION_KEY, sessionId);

  let source = null;          // committed source object or null
  let sourceLabel = "";       // what to show for it
  let messages = [];          // [{role:"user"|"assistant"|"error", text}]
  let streaming = false;
  let controller = null;
  let stick = true;
  let sheetOpen = false;

  container.classList.add("sc-pane");
  const root = el("div", { class: "sc-root" });
  container.append(root);

  // ------------------------------------------------------------ header
  const sourceText = el("span", { class: "sc-source-text" });
  const changeButton = el("button", { type: "button", class: "sc-link", "aria-haspopup": "dialog",
    onclick: () => openSheet() }, icon("edit"), "Change");
  const sourceBar = el("div", { class: "sc-source" },
    el("span", { class: "sc-source-label muted" }, "Source:"), sourceText, changeButton);

  const providerHost = el("div", { class: "sc-provider" });
  const provider = createProviderSwitch(providerHost, "Model for chat");
  const newButton = el("button", { type: "button", class: "btn sc-new", onclick: () => newChat() },
    icon("refresh"), "New chat");
  const controls = el("div", { class: "sc-controls" }, providerHost, newButton);
  const head = el("div", { class: "sc-head" }, sourceBar, controls);

  // ------------------------------------------------------------ list
  const list = el("div", { class: "sc-list", role: "log", "aria-live": "polite", "aria-relevant": "additions",
    "aria-label": "Chat messages", tabindex: "0" });
  const jump = el("button", { type: "button", class: "sc-jump", hidden: true, "aria-label": "Jump to latest message",
    onclick: () => scrollToBottom(true) }, icon("down"), "Latest");
  const listWrap = el("div", { class: "sc-listwrap" }, list, jump);

  // ------------------------------------------------------------ composer
  const input = el("textarea", { class: "sc-input", rows: "1", placeholder: "Ask about your source…",
    "aria-label": "Message", enterkeyhint: "send", maxlength: "4000" });
  const sendButton = el("button", { type: "button", class: "sc-send", "aria-label": "Send message", disabled: true,
    onclick: () => send(input.value) }, icon("send"));
  const stopButton = el("button", { type: "button", class: "sc-send sc-stop", "aria-label": "Stop answer", hidden: true,
    onclick: () => stop() }, icon("stop"));
  const composer = el("div", { class: "sc-composer" }, input, sendButton, stopButton);

  root.append(head, listWrap, composer);

  // ------------------------------------------------------------ source sheet
  const sheetHost = el("div", { class: "sc-sheet-host" });
  const backdrop = el("div", { class: "sc-backdrop", hidden: true, onclick: () => closeSheet(false) });
  const sheetBody = el("div", { class: "sc-sheet-body" });
  const sheet = el("div", { class: "sheet sc-sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "sc-sheet-title", hidden: true },
    el("div", { class: "sc-sheet-head" },
      el("h2", { id: "sc-sheet-title" }, "Chat source"),
      el("button", { type: "button", class: "sc-iconbtn", "aria-label": "Close without changing", onclick: () => closeSheet(false) }, icon("x"))),
    sheetBody,
    el("div", { class: "sc-sheet-actions" },
      el("button", { type: "button", class: "btn", onclick: () => closeSheet(false) }, "Cancel"),
      el("button", { type: "button", class: "btn primary", onclick: () => closeSheet(true) }, "Use this source")));
  sheetHost.append(backdrop, sheet);
  container.append(sheetHost);
  const picker = createSourcePicker(sheetBody, ctx, { required: false, allowNone: true });

  function openSheet() {
    sheetOpen = true;
    backdrop.hidden = false;
    sheet.hidden = false;
    picker.focus();
  }
  function closeSheet(apply) {
    if (!sheetOpen) return;
    sheetOpen = false;
    if (apply) {
      source = picker.getSource();
      sourceLabel = source ? picker.label() : "";
      store.set("sessionStorage", SOURCE_KEY, JSON.stringify({ source, label: sourceLabel }));
      paintSource();
    } else {
      picker.setSource(source, sourceLabel.replace(/^[^:]*: /, ""));
    }
    backdrop.hidden = true;
    sheet.hidden = true;
    changeButton.focus();
  }
  sheet.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.stopPropagation(); closeSheet(false); return; }
    if (event.key !== "Tab") return;
    const focusable = [...sheet.querySelectorAll("button, input, textarea, [tabindex='0']")].filter((n) => !n.disabled && n.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  function paintSource() {
    sourceText.textContent = source ? sourceLabel : "None";
    sourceText.title = source ? sourceLabel : "";
    input.placeholder = source ? "Ask about your source…" : "Ask a question…";
  }

  // ------------------------------------------------------------ messages
  function bubble(kind, text) {
    if (kind === "user") return el("div", { class: "sc-msg sc-user" }, el("div", { class: "sc-bubble" }, text));
    if (kind === "error") {
      return el("div", { class: "sc-msg sc-err" },
        el("div", { class: "sc-bubble", role: "alert" }, icon("alert"), el("span", {}, text)));
    }
    const body = el("div", { class: "sc-bubble markdown" });
    if (text) body.innerHTML = renderMarkdown(text); // sanitised by DOMPurify
    return el("div", { class: "sc-msg sc-bot" }, body);
  }

  function paintEmpty() {
    if (messages.length || streaming) return;
    list.replaceChildren(el("div", { class: "sc-empty" },
      el("div", { class: "sc-empty-icon" }, icon("edit")),
      el("h2", {}, "Study chat"),
      el("p", { class: "muted" }, source
        ? "Ask anything about your source, or start with one of these."
        : "Pick a source with Change, or just ask a question."),
      el("div", { class: "sc-starters" }, STARTERS.map((text) =>
        el("button", { type: "button", class: "sc-starter", onclick: () => send(text) }, text)))));
  }

  function repaintAll() {
    list.replaceChildren();
    for (const m of messages) list.append(bubble(m.role, m.text));
    paintEmpty();
    scrollToBottom(true);
  }

  function save() {
    store.set("sessionStorage", LOG_KEY, JSON.stringify(messages.slice(-MAX_SAVED)));
  }

  function nearBottom() { return list.scrollHeight - list.scrollTop - list.clientHeight < 70; }
  function scrollToBottom(force = false) {
    if (force) stick = true;
    if (stick) list.scrollTop = list.scrollHeight;
    jump.hidden = stick || !list.firstChild;
  }
  list.addEventListener("scroll", () => { stick = nearBottom(); jump.hidden = stick; }, { passive: true });

  // ------------------------------------------------------------ sending
  function syncSend() {
    sendButton.disabled = !input.value.trim();
    sendButton.hidden = streaming;
    stopButton.hidden = !streaming;
    input.disabled = false;
    newButton.disabled = false;
  }
  function growInput() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  }
  input.addEventListener("input", () => { growInput(); syncSend(); });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    // Hardware keyboards send on Enter; a phone keyboard keeps Enter for a new line.
    if (event.ctrlKey || event.metaKey || window.matchMedia("(pointer: fine)").matches) {
      event.preventDefault();
      send(input.value);
    }
  });

  function stop() { if (controller) controller.abort(); }

  async function send(raw) {
    const text = String(raw || "").trim();
    if (!text || streaming) return;
    input.value = "";
    growInput();
    if (!messages.length) list.replaceChildren();
    messages.push({ role: "user", text });
    list.append(bubble("user", text));

    const reply = bubble("assistant", "");
    const replyBody = reply.firstChild;
    replyBody.setAttribute("aria-busy", "true");
    replyBody.append(el("span", { class: "sc-dots", role: "status", "aria-label": "The model is thinking" }, el("i"), el("i"), el("i")));
    list.append(reply);

    streaming = true;
    syncSend();
    scrollToBottom(true);
    controller = new AbortController();
    const signal = controller.signal;
    let answer = "";
    let failure = "";
    let timer = null;
    let dirty = false;
    const paintReply = () => {
      timer = null;
      if (!dirty) return;
      dirty = false;
      replyBody.innerHTML = renderMarkdown(answer); // sanitised by DOMPurify
      scrollToBottom();
    };
    try {
      for await (const event of sse("/api/learn/chat", {
        session_id: sessionId, source, message: text, provider: provider.get(),
      }, signal)) {
        if (event.error) { failure = String(event.error); continue; }
        if (typeof event.t === "string") {
          answer += event.t;
          dirty = true;
          if (!timer) timer = setTimeout(paintReply, RENDER_EVERY_MS);
        }
      }
    } catch (error) {
      failure = error.message || "Something went wrong.";
    }
    clearTimeout(timer);
    const stopped = signal.aborted;
    dirty = true;
    if (answer) paintReply();
    replyBody.removeAttribute("aria-busy");
    streaming = false;
    controller = null;
    if (answer) {
      messages.push({ role: "assistant", text: answer + (stopped ? "\n\n*(stopped)*" : "") });
      if (stopped) { replyBody.innerHTML = renderMarkdown(messages[messages.length - 1].text); }
    } else {
      reply.remove();
      if (stopped && !failure) {
        messages.push({ role: "error", text: "Stopped before any answer arrived." });
        list.append(bubble("error", "Stopped before any answer arrived."));
      } else if (!failure) {
        failure = "The model sent no answer. Try again.";
      }
    }
    if (failure) {
      messages.push({ role: "error", text: failure });
      list.append(bubble("error", failure));
    }
    save();
    syncSend();
    scrollToBottom();
    if (!stopped) input.focus({ preventScroll: true });
  }

  async function newChat() {
    if (streaming) stop();
    const old = sessionId;
    messages = [];
    stick = true;
    store.set("sessionStorage", LOG_KEY, "[]");
    repaintAll();
    try {
      await api("/api/learn/chat/reset", { method: "POST", json: { session_id: old } });
    } catch (error) {
      // The old history may linger on the laptop; start under a new id so it can't leak in.
      sessionId = newSessionId();
      store.set("sessionStorage", SESSION_KEY, sessionId);
      ctx.toast(error.message);
    }
    input.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------ fit the pane to the screen
  // The Learn view scrolls, but a chat needs a fixed composer: size the pane to the free height,
  // less whatever the on-screen keyboard covers.
  function fit() {
    const view = container.closest(".view");
    const seg = view && view.querySelector(".learn-seg");
    if (!view || !seg || container.hidden) return;
    const cs = getComputedStyle(view);
    let free = view.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - seg.offsetHeight;
    const vv = window.visualViewport;
    if (vv) {
      const covered = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      const tabs = document.getElementById("tabs");
      if (covered > 80) free -= Math.max(0, covered - (tabs ? tabs.offsetHeight : 0));
    }
    container.style.height = Math.max(240, Math.floor(free)) + "px";
    if (stick) list.scrollTop = list.scrollHeight;
  }
  window.addEventListener("resize", fit);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fit);
    window.visualViewport.addEventListener("scroll", fit);
  }

  // ------------------------------------------------------------ restore saved state
  try {
    const saved = JSON.parse(store.get("sessionStorage", LOG_KEY, "[]"));
    if (Array.isArray(saved)) {
      messages = saved.filter((m) => m && ["user", "assistant", "error"].includes(m.role) && typeof m.text === "string");
    }
    const savedSource = JSON.parse(store.get("sessionStorage", SOURCE_KEY, "null"));
    if (savedSource && savedSource.source) {
      source = savedSource.source;
      sourceLabel = savedSource.label || "Saved source";
      picker.setSource(source, sourceLabel.replace(/^[^:]*: /, ""));
    }
  } catch (_) { /* start empty */ }
  paintSource();
  syncSend();
  repaintAll();

  return {
    show() {
      provider.refresh();
      requestAnimationFrame(() => { fit(); scrollToBottom(); });
    },
    hide() {
      if (sheetOpen) closeSheet(false);
    },
  };
}
