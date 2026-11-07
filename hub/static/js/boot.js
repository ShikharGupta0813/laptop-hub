// Runs before first paint (a classic, blocking script): applies the saved theme and accent so the app
// never flashes the wrong colours. The CSP forbids inline scripts, so this lives in its own file.
// Older saved values (Midnight / Daylight / Aurora, and the old accent names) map onto the REDLINE set,
// and "auto" is resolved here so the CSS only ever sees redline | slate | paper. Keep in sync with ui.js.
(function () {
  var THEMES = { redline: 1, slate: 1, paper: 1, auto: 1 };
  var OLD_THEMES = { midnight: "redline", daylight: "paper", aurora: "slate" };
  var ACCENTS = { crimson: 1, amber: 1, cyan: 1, green: 1, white: 1 };
  var OLD_ACCENTS = { indigo: "crimson", violet: "crimson", rose: "crimson", blue: "cyan", teal: "cyan" };
  var root = document.documentElement, theme = "redline", accent = null;
  try {
    theme = localStorage.getItem("hub.theme") || theme;
    accent = localStorage.getItem("hub.accent");
    if (OLD_ACCENTS[accent]) {
      // Aurora's violet/indigo look becomes Slate with its amber signature; everything else maps by colour.
      accent = theme === "aurora" ? "amber" : OLD_ACCENTS[accent];
      localStorage.setItem("hub.accent", accent);
    }
    if (OLD_THEMES[theme]) { theme = OLD_THEMES[theme]; localStorage.setItem("hub.theme", theme); }
    if (localStorage.getItem("hub.motion") === "reduce") root.setAttribute("data-motion", "reduce");
  } catch (e) { /* private mode */ }
  if (!THEMES[theme]) theme = "redline";
  if (!ACCENTS[accent]) accent = theme === "slate" ? "amber" : "crimson";   // no choice saved: the theme's signature
  if (theme === "auto") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "paper" : "redline";
  }
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-accent", accent);
})();
