// App shell: login, the five tabs, and the small toolkit (`ctx`) handed to every tab module.
//
// A tab module exports `init(container, ctx)`. It builds its UI inside `container` and may
// return an object with optional `show()` (tab became visible), `hide()`, and any methods
// other modules call through ctx (for example files returns `openFile(root, path)`).
import { api, ApiError } from "./api.js";
import * as assistant from "./assistant.js";
import * as files from "./files.js";
import * as notes from "./notes.js";
import * as terminal from "./terminal.js";
import * as hub from "./hub.js";

const TABS = [
  { id: "assistant", label: "Talk", mod: assistant, icon: "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V22h2v-3.1A7 7 0 0 0 19 12h-2Z" },
  { id: "files", label: "Files", mod: files, icon: "M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2Z" },
  { id: "notes", label: "Notes", mod: notes, icon: "M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm8 1.5V8h4.5L14 3.5ZM7 12v2h10v-2H7Zm0 4v2h7v-2H7Z" },
  { id: "terminal", label: "Terminal", mod: terminal, icon: "M3 4h18a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm3.7 4.3L5.3 9.7 7.6 12l-2.3 2.3 1.4 1.4L10.4 12 6.7 8.3ZM12 15v2h6v-2h-6Z" },
  { id: "hub", label: "Hub", mod: hub, icon: "M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z" },
];

const handles = {};
let current = null;
let started = false;

const $ = (id) => document.getElementById(id);

function toast(message, ms = 3500) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, ms);
}

function speak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

function show(tabId) {
  const tab = TABS.find((t) => t.id === tabId) || TABS[0];
  if (current && handles[current]?.hide) handles[current].hide();
  current = tab.id;
  for (const t of TABS) {
    $("view-" + t.id).hidden = t.id !== tab.id;
    const button = document.querySelector(`#tabs [data-tab="${t.id}"]`);
    button.setAttribute("aria-selected", String(t.id === tab.id));
  }
  try { localStorage.setItem("hub.tab", tab.id); } catch (_) { /* private mode */ }
  handles[tab.id]?.show?.();
}

const ctx = {
  api,
  toast,
  speak,
  goto: show,
  secure: window.isSecureContext,
  // Switch to the Files tab and open a file in the reader.
  openFile(root, path) {
    show("files");
    handles.files?.openFile?.(root, path);
  },
};

function buildTabs() {
  const nav = $("tabs");
  for (const tab of TABS) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.tab = tab.id;
    button.setAttribute("role", "tab");
    button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${tab.icon}"/></svg><span>${tab.label}</span>`;
    button.addEventListener("click", () => show(tab.id));
    nav.append(button);
  }
}

function startApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  if (!started) {
    started = true;
    buildTabs();
    for (const tab of TABS) {
      try {
        handles[tab.id] = tab.mod.init($("view-" + tab.id), ctx) || {};
      } catch (error) {
        console.error(tab.id, error);
        $("view-" + tab.id).textContent = `The ${tab.label} tab failed to start: ${error.message}`;
        handles[tab.id] = {};
      }
    }
  }
  let saved = null;
  try { saved = localStorage.getItem("hub.tab"); } catch (_) { /* ignore */ }
  show(saved || "assistant");
}

function showLogin(message) {
  $("app").hidden = true;
  $("login").hidden = false;
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message || "";
  $("token").focus();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/login", { method: "POST", json: { token: $("token").value } });
    $("token").value = "";
    startApp();
  } catch (error) {
    showLogin(error.message);
  }
});

window.addEventListener("hub:unauthorized", () => showLogin("Please log in again."));

(async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    const session = await api("/api/session");
    if (session.authenticated) startApp();
    else showLogin();
  } catch (error) {
    showLogin(error instanceof ApiError ? error.message : "Something went wrong starting the app.");
  }
})();
