// More: a launcher for the screens that don't have a bottom-bar button.
import { el } from "./api.js";

const ENTRIES = [
  { id: "files", title: "Files", text: "Browse and read the folders you share. Select text to explain it." },
  { id: "terminal", title: "Terminal", text: "A real shell on the laptop: PowerShell, WSL or bash." },
  { id: "hub", title: "Hub", text: "Laptop status, quick buttons, send files, setup check, log out." },
];

export function init(container, ctx) {
  container.append(
    el("h2", {}, "More"),
    el("div", { class: "more-list" }, ENTRIES.map((entry) =>
      el("button", { class: "more-item card", type: "button", onclick: () => ctx.goto(entry.id) },
        el("strong", {}, entry.title), el("span", { class: "muted" }, entry.text)))));
  return {};
}
