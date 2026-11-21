// Keeps the app shell available when the laptop is off, so the app still opens and says so.
// API calls and the terminal socket are never cached. Bump VERSION whenever a shell file changes.
const VERSION = "v47-freelance-presets-2026.11";
const CACHE = `hub-shell-${VERSION}`;
const SHELL = [
  "/js/models.js", "/css/models.css", "/js/dictation.js", "/css/dictation.css", "/js/provlists.js",
  "/css/provlists.css", "/js/mcp.js", "/css/mcp.css", "/", "/index.html", "/style.css", "/manifest.webmanifest",
  "/css/apps.css", "/css/assistant.css", "/css/automations.css", "/css/bookmarks.css", "/css/cards.css",
  "/css/design.css", "/css/english.css", "/css/expenses.css", "/css/fileops.css", "/css/files.css", "/css/focus.css",
  "/css/habits.css", "/css/home.css", "/css/hub.css", "/css/inbox.css", "/css/journal.css", "/css/kb.css",
  "/css/learn.css", "/css/notes.css", "/css/notifications.css", "/css/reminders.css", "/css/screen.css",
  "/css/settings.css", "/css/shell.css", "/css/terminal.css", "/css/todos.css", "/css/tools.css",
  "/css/voicemode.css", "/css/watch.css", "/css/work.css", "/css/council.css", "/js/council.js",
  "/css/autopilot.css", "/js/autopilot.js", "/css/ccsessions.css", "/js/ccsessions.js", "/js/today.js",
  "/css/research.css", "/js/research.js", "/css/opps.css", "/js/opps.js", "/js/oppsources.js", "/css/skills.css", "/js/skills.js",
  "/css/brain.css", "/js/brain.js", "/css/buildflow.css", "/js/buildflow.js", "/css/applyq.css", "/js/applyq.js",
  "/js/privacy.js", "/css/privacy.css", "/js/fullcontrol.js", "/js/wheel.js", "/css/wheel.css",
  "/vendor/fonts/orbitron-var-latin.woff2", "/js/gmailx.js", "/js/speechchunk.js", "/js/speechqueue.js",
  "/js/hubtts.js", "/js/voices.js", "/js/wake.js", "/js/wakephrase.js", "/js/assistants.js", "/js/listeditor.js",
  "/js/ownerlists.js", "/css/wake.css", "/js/api.js", "/js/apps.js", "/js/assistant.js", "/js/automations.js",
  "/js/bookmarks.js", "/js/boot.js", "/js/cards.js", "/js/english.js", "/js/expenses.js", "/js/fileeditor.js",
  "/js/files.js", "/js/focus.js", "/js/habits.js", "/js/home.js", "/js/hub.js", "/js/icons.js", "/js/inbox.js",
  "/js/jarvis.js", "/js/journal.js", "/js/kb.js", "/js/laptop.js", "/js/learn.js", "/js/main.js", "/css/setup.css",
  "/js/setup.js", "/vendor/fonts/jetbrains-mono-var-latin.woff2", "/css/launcher.css", "/js/launcher.js",
  "/css/fixkit.css", "/js/fixpanel.js", "/js/voicekit.js", "/css/friday.css", "/js/friday.js", "/js/notes.js", "/js/notifications.js", "/js/omnibox.js", "/js/providers.js",
  "/js/quiz.js", "/js/reminders.js", "/js/screen.js", "/js/settings.js", "/js/sourcepicker.js", "/js/studychat.js",
  "/js/lanepicker.js", "/js/presence.js", "/css/lanes.css", "/js/terminal.js", "/js/todos.js", "/js/tools.js",
  "/js/ui.js", "/js/voicemode.js", "/js/wakebridge.js", "/css/posts.css", "/js/posts.js", "/js/devto.js",
  "/js/watch.js", "/js/work.js", "/vendor/marked.js", "/vendor/purify.min.js", "/vendor/xterm.js",
  "/vendor/xterm.css", "/vendor/addon-fit.js", "/vendor/fonts/inter-var-latin.woff2", "/icons/icon.svg",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png", "/css/alerts.css", "/js/alerts.js",
  "/js/sources.js", "/js/audit.js", "/css/audit.css", "/js/panic.js", "/css/panic.css", "/js/trust.js",
  "/js/meetings.js", "/css/meetings.css", "/js/computeruse.js", "/css/computeruse.css",
  "/js/meetings.js", "/css/meetings.css", "/js/playbooks.js", "/css/playbooks.css",
  "/js/jobprefs.js", "/js/batches.js", "/css/jobprefs.css",
  "/js/tailor.js", "/css/tailor.css", "/js/studylib.js",
  "/js/chatch.js", "/js/aichat.js", "/css/aichat.css", "/css/sheets.css", "/js/setuphint.js",
  "/js/tailor.js", "/css/tailor.css", "/css/perf.css", "/js/poller.js", "/js/navstack.js",
  "/js/networking.js", "/css/networking.css",
  "/js/interviews.js", "/css/interviews.css",
  "/js/jobtrack.js", "/css/jobtrack.css",
  "/js/ats.js", "/css/ats.css",
  "/js/triagebadges.js", "/css/triage.css",
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
  // Vendor libraries, fonts and icons never change between releases: serve the cached copy at once, refresh it quietly.
  if (/^\/(vendor|icons)\//.test(url.pathname)) {
    event.respondWith(caches.open(CACHE).then((c) => c.match(event.request).then((hit) => {
      const fresh = fetch(event.request).then((r) => { if (r.ok && r.type === "basic") c.put(event.request, r.clone()); return r; });
      return hit || fresh;
    })));
    return;
  }
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
