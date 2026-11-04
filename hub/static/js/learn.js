// Learn: three sections in one tab. Chat and Quiz share the source picker; English is separate.
import { el } from "./api.js";
import * as studychat from "./studychat.js";
import * as quiz from "./quiz.js";
import * as english from "./english.js";

const SECTIONS = [
  { id: "chat", label: "Chat", mod: studychat },
  { id: "quiz", label: "Quiz", mod: quiz },
  { id: "english", label: "English", mod: english },
];

export function init(container, ctx) {
  const bar = el("div", { class: "learn-seg", role: "tablist", "aria-label": "Learn sections" });
  const panes = {};
  const handles = {};
  let current = null;

  function select(id) {
    if (current && handles[current]?.hide) handles[current].hide();
    current = id;
    for (const section of SECTIONS) {
      panes[section.id].hidden = section.id !== id;
      bar.querySelector(`[data-section="${section.id}"]`).setAttribute("aria-selected", String(section.id === id));
    }
    try { localStorage.setItem("hub.learn", id); } catch (_) { /* ignore */ }
    handles[id]?.show?.();
  }

  for (const section of SECTIONS) {
    bar.append(el("button", { type: "button", role: "tab", "data-section": section.id,
      onclick: () => select(section.id) }, section.label));
    panes[section.id] = el("div", { class: "learn-pane", hidden: true });
  }
  container.append(bar, ...SECTIONS.map((s) => panes[s.id]));
  for (const section of SECTIONS) {
    try {
      handles[section.id] = section.mod.init(panes[section.id], ctx) || {};
    } catch (error) {
      console.error(section.id, error);
      panes[section.id].textContent = `${section.label} failed to start: ${error.message}`;
      handles[section.id] = {};
    }
  }
  let saved = null;
  try { saved = localStorage.getItem("hub.learn"); } catch (_) { /* ignore */ }
  select(SECTIONS.some((s) => s.id === saved) ? saved : "chat");

  return {
    show() { handles[current]?.show?.(); },
    hide() { handles[current]?.hide?.(); },
  };
}
