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
import * as work from "./work.js";
import * as learn from "./learn.js";
import * as apps from "./apps.js";

// Every screen is a "view". The bottom bar shows five of them; Files, Terminal and Hub live
// under Apps, so they have no button of their own and count as "Apps" for highlighting.
const VIEWS = [
  { id: "assistant", label: "Talk", mod: assistant, nav: true, icon: "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V22h2v-3.1A7 7 0 0 0 19 12h-2Z" },
  { id: "work", label: "Work", mod: work, nav: true, icon: "M10 2h4a2 2 0 0 1 2 2v2h4a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4V4a2 2 0 0 1 2-2Zm0 2v2h4V4h-4Zm-6 8v2h16v-2H4Z" },
  { id: "learn", label: "Learn", mod: learn, nav: true, icon: "M12 3 1 9l11 6 9-4.9V17h2V9L12 3ZM5 13.2v4L12 21l7-3.8v-4L12 17l-7-3.8Z" },
  { id: "notes", label: "Notes", mod: notes, nav: true, icon: "M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm8 1.5V8h4.5L14 3.5ZM7 12v2h10v-2H7Zm0 4v2h7v-2H7Z" },
  { id: "apps", label: "Apps", mod: apps, nav: true, icon: "M5 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm7 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm7 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z" },
  { id: "files", label: "Files", mod: files, nav: false, under: "apps" },
  { id: "terminal", label: "Terminal", mod: terminal, nav: false, under: "apps" },
  { id: "hub", label: "Hub", mod: hub, nav: false, under: "apps" },
];
const TABS = VIEWS; // the rest of this file loops over every view

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
  }
  const owner = tab.under || tab.id;
  for (const button of document.querySelectorAll("#tabs [data-tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === owner));
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
  // Open a screen, and optionally one section of it (modules may return openSection(name)).
  open(view, section) {
    show(view);
    if (section) handles[view]?.openSection?.(section);
  },
  openFile(root, path) {
    show("files");
    handles.files?.openFile?.(root, path);
  },
};

function buildTabs() {
  const nav = $("tabs");
  for (const tab of TABS.filter((v) => v.nav)) {
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
