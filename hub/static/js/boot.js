// Runs before first paint (a classic, blocking script): applies the saved theme and accent so the app
// never flashes the wrong colours. The CSP forbids inline scripts, so this lives in its own file.
(function () {
  var root = document.documentElement, theme = "midnight", accent = "indigo";
  try {
    theme = localStorage.getItem("hub.theme") || theme;
    accent = localStorage.getItem("hub.accent") || accent;
    if (localStorage.getItem("hub.motion") === "reduce") root.setAttribute("data-motion", "reduce");
  } catch (e) { /* private mode */ }
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-accent", accent);
})();
