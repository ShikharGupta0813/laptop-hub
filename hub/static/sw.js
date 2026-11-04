// Keeps the app shell available when the laptop is off, so the app still opens and says so.
// API calls and the terminal socket are never cached. Bump VERSION whenever a shell file changes.
const VERSION = "v5-design-2026.11";
const CACHE = `hub-shell-${VERSION}`;
const SHELL = [
  "/", "/index.html", "/style.css", "/manifest.webmanifest",
  "/css/design.css", "/css/shell.css", "/css/home.css",
  "/css/work.css", "/css/learn.css", "/css/english.css", "/css/apps.css", "/css/assistant.css",
  "/css/files.css", "/css/notes.css", "/css/terminal.css", "/css/hub.css",
  "/js/boot.js", "/js/main.js", "/js/api.js", "/js/ui.js", "/js/icons.js", "/js/home.js", "/js/omnibox.js",
  "/js/assistant.js", "/js/files.js", "/js/notes.js", "/js/terminal.js", "/js/hub.js", "/js/work.js",
  "/js/learn.js", "/js/studychat.js", "/js/quiz.js", "/js/sourcepicker.js", "/js/english.js", "/js/apps.js",
  "/js/laptop.js", "/js/jarvis.js",
  "/vendor/marked.js", "/vendor/purify.min.js", "/vendor/xterm.js", "/vendor/xterm.css", "/vendor/addon-fit.js",
  "/vendor/fonts/inter-var-latin.woff2",
  "/icons/icon.svg", "/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png",
];
// Screens other features add later (settings, notifications, ...) are cached the first time they load.

self.addEventListener("install", (event) => {
  // One missing file must not break the whole install, so files are cached one by one.
  event.waitUntil(caches.open(CACHE)
    .then((cache) => Promise.all(SHELL.map((url) => cache.add(new Request(url, { cache: "reload" })).catch(() => null))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("hub-shell-") && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin ||
      url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;
  // Network first (the laptop is the source of truth), cache when it can't answer.
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok && response.type === "basic") {
        const copy = response.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request, { ignoreSearch: true })
      .then((hit) => hit || (event.request.mode === "navigate" ? caches.match("/") : Response.error())))
  );
});
