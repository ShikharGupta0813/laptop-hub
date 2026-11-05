// Learn > Chat: ask questions about a source (or nothing), streamed from the laptop's model.
// Model text is shown with textContent or renderMarkdown (sanitised); never raw innerHTML.
import { api, sse, renderMarkdown, el } from "./api.js";
import * as ui from "./ui.js";
import { createSourceSheet, createProviderSwitch, ensureCss, store } from "./sourcepicker.js";

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
  ensureCss("/css/learn.css");
  let sessionId = store.get("sessionStorage", SESSION_KEY, "") || newSessionId();
  store.set("sessionStorage", SESSION_KEY, sessionId);

  let source = null;          // committed source object or null
  let sourceLabel = "";       // what to show for it
  let messages = [];          // [{role:"user"|"assistant"|"error", text}]
  let streaming = false;
  let controller = null;
  let stick = true;

  container.classList.add("sc-pane");
  const root = el("div", { class: "sc-root" });
  container.append(root);

  // ------------------------------------------------------------ header: source chip, model, new chat
  const sourceText = el("span", { class: "sc-source-text ui-truncate" });
  const sourceChip = el("button", { type: "button", class: "ui-chip sc-source", "aria-haspopup": "dialog",
    "aria-label": "Change chat source", onclick: () => sources.open() }, ui.icon("book-open"), sourceText, ui.icon("chevron-down", { class: "sc-source-chev" }));
  const providerHost = el("div", { class: "sc-provider" });
  const provider = createProviderSwitch(providerHost, "Model for chat");
  const newButton = ui.button({ icon: "refresh", variant: "ghost", iconOnly: true, label: "New chat", onClick: () => newChat() });
  const head = el("div", { class: "sc-head" }, el("div", { class: "sc-head-row" }, sourceChip, newButton), providerHost);

  // ------------------------------------------------------------ list
  const list = el("div", { class: "sc-list", role: "log", "aria-live": "polite", "aria-relevant": "additions",
    "aria-label": "Chat messages", tabindex: "0" });
  const jump = ui.button({ label: "Latest", icon: "arrow-down", variant: "secondary", size: "sm", class: "sc-jump", hidden: true,
    onClick: () => scrollToBottom(true) });
  const listWrap = el("div", { class: "sc-listwrap" }, list, jump);

  // ------------------------------------------------------------ composer
  const input = el("textarea", { class: "sc-input", rows: "1", placeholder: "Ask about your source…",
    "aria-label": "Message", enterkeyhint: "send", maxlength: "4000" });
  const sendButton = ui.button({ icon: "send", variant: "primary", iconOnly: true, label: "Send message", class: "sc-send", disabled: true,
    onClick: () => send(input.value) });
  const stopButton = ui.button({ icon: "stop", variant: "danger", iconOnly: true, label: "Stop answer", class: "sc-send", hidden: true,
    onClick: () => stop() });
  const composer = el("div", { class: "sc-composer" }, input, sendButton, stopButton);

  root.append(head, listWrap, composer);

  // ------------------------------------------------------------ source sheet (bottom sheet with tabs)
  const sources = createSourceSheet(ctx, { title: "Chat source", allowNone: true, applyLabel: "Use this source",
    onApply(next, label) {
      source = next;
      sourceLabel = next ? label : "";
      store.set("sessionStorage", SOURCE_KEY, JSON.stringify({ source, label: sourceLabel }));
      paintSource();
      paintEmpty();
    } });

  function paintSource() {
    sourceText.textContent = source ? sourceLabel : "No source";
    sourceChip.classList.toggle("is-on", !!source);
    input.placeholder = source ? "Ask about your source…" : "Ask a question…";
  }

  // ------------------------------------------------------------ messages
  function bubble(kind, text) {
    if (kind === "user") return el("div", { class: "sc-msg sc-user" }, el("div", { class: "sc-bubble" }, text));
    if (kind === "error") {
      return el("div", { class: "sc-msg sc-err" },
        el("div", { class: "sc-bubble", role: "alert" }, ui.icon("alert-circle"), el("span", {}, text)));
    }
    const body = el("div", { class: "sc-bubble markdown" });
    if (text) body.innerHTML = renderMarkdown(text); // sanitised by DOMPurify
    return el("div", { class: "sc-msg sc-bot" }, body);
  }

  function paintEmpty() {
    if (messages.length || streaming) return;
    list.replaceChildren(el("div", { class: "sc-empty" },
      el("div", { class: "sc-empty-art" }, ui.icon("sparkles")),
      el("h2", {}, "Study chat"),
      el("p", {}, source
        ? "Ask anything about your source, or start with one of these."
        : "Pick a source with the chip above, or just ask a question."),
      el("div", { class: "sc-starters" }, STARTERS.map((text) =>
        el("button", { type: "button", class: "ui-chip", onclick: () => send(text) }, text)))));
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
    const seg = view && view.querySelector(".ln-head");
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
      sources.set(source, sourceLabel);
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
    hide() {},
  };
}
