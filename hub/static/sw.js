// Keeps the app shell available when the laptop is off, so the app still opens and says so.
// API calls and the terminal socket are never cached.
const CACHE = "hub-shell-v2";
const SHELL = ["/", "/style.css", "/js/main.js", "/js/api.js", "/js/assistant.js", "/js/files.js",
  "/js/notes.js", "/js/terminal.js", "/js/hub.js", "/js/work.js", "/js/learn.js", "/js/studychat.js",
  "/js/quiz.js", "/js/sourcepicker.js", "/js/english.js", "/js/more.js", "/css/work.css", "/css/learn.css",
  "/css/english.css", "/css/more.css", "/css/assistant.css", "/css/files.css",
  "/css/notes.css", "/css/terminal.css", "/css/hub.css", "/vendor/marked.js",
  "/vendor/purify.min.js", "/vendor/xterm.js", "/vendor/xterm.css", "/vendor/addon-fit.js",
  "/manifest.webmanifest", "/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin ||
      url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((c) => c.put(event.request, copy)); }
      return response;
    }).catch(() => caches.match(event.request, { ignoreSearch: true }))
  );
});
