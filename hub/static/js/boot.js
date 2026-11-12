// Runs before first paint (a classic, blocking script): applies the saved theme and accent so the app
// never flashes the wrong colours. The CSP forbids inline scripts, so this lives in its own file.
// ARC-Glass themes are arc | paper (| auto, resolved here). Every older saved name maps onto them:
// redline / slate / midnight / aurora -> arc, daylight -> paper, and the old crimson accent -> cyan, so the
// CSS only ever sees arc | paper and cyan | amber | green | violet | white. Keep in sync with ui.js.
(function () {
  var THEMES = { arc: 1, paper: 1, auto: 1 };
  var OLD_THEMES = { redline: "arc", slate: "arc", midnight: "arc", aurora: "arc", daylight: "paper" };
  var ACCENTS = { cyan: 1, amber: 1, green: 1, violet: 1, white: 1 };
  var OLD_ACCENTS = { crimson: "cyan", indigo: "violet", rose: "cyan", blue: "cyan", teal: "cyan" };
  var root = document.documentElement, theme = "arc", accent = null;
  try {
    theme = localStorage.getItem("hub.theme") || theme;
    accent = localStorage.getItem("hub.accent");
    if (OLD_ACCENTS[accent]) { accent = OLD_ACCENTS[accent]; localStorage.setItem("hub.accent", accent); }
    if (OLD_THEMES[theme]) { theme = OLD_THEMES[theme]; localStorage.setItem("hub.theme", theme); }
    if (localStorage.getItem("hub.motion") === "reduce") root.setAttribute("data-motion", "reduce");
  } catch (e) { /* private mode */ }
  if (!THEMES[theme]) theme = "arc";
  if (!ACCENTS[accent]) accent = "cyan";   // no choice saved, or an unknown one
  if (theme === "auto") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "paper" : "arc";
  }
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-accent", accent);
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "paper" ? "#E9F0F7" : "#05070C");
})();
