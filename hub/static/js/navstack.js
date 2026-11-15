// Pure decisions behind Android Back (kept free of the DOM so node can test them).
export const EXIT_WINDOW_MS = 2000;

// What a Back press should do. layers: open sheets/modals; view: the registry entry of the current screen.
export function backAction({ layers = 0, current = "home", view = null, armed = false } = {}) {
  if (layers > 0) return "layer";
  if (current !== "home") return view && !view.nav ? "history" : "home";
  return armed ? "exit" : "arm";
}

// Switching between bottom-bar tabs replaces the entry (so Back from a tab is Home, not the previous tab).
export function shouldReplace(prev, next) {
  return !!(prev && prev.nav && prev.id !== "home" && next.nav);
}
