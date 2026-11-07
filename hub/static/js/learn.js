// Learn: Chat | Quiz | Cards | Ask files | English in one tab, under a "continue learning" strip.
// Chat and Quiz share the source picker (a bottom sheet); English is a separate module with its own sub-sections.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { ensureCss } from "./sourcepicker.js";
import * as studychat from "./studychat.js";
import * as quiz from "./quiz.js";
import * as cards from "./cards.js";
import * as kb from "./kb.js";
import * as english from "./english.js";

const SECTIONS = [
  { id: "chat", label: "Chat", mod: studychat },
  { id: "quiz", label: "Quiz", mod: quiz },
  { id: "cards", label: "Flashcards", mod: cards },
  { id: "kb", label: "Ask files", mod: kb },
  { id: "english", label: "English", mod: english },
];
const ALIASES = { files: "kb", ask: "kb", flashcards: "cards", roadmaps: "cards" };

export function init(container, ctx) {
  ensureCss("/css/learn.css");
  const panes = {};
  const handles = {};
  let current = null;

  // ---- header: continue-learning strip + section switch
  const strip = el("div", { class: "ln-strip ui-hscroll", role: "group", "aria-label": "Continue learning" });
  const seg = ui.segmented({ options: SECTIONS.map((s) => ({ value: s.id, label: s.label })), value: "chat",
    label: "Learn sections", block: true, onChange: (id) => select(id) });
  seg.classList.add("ln-seg");
  const head = el("div", { class: "ln-head" }, strip, seg);

  function select(id, fromUrl) {
    if (!panes[id]) id = "chat";
    if (current && current !== id) handles[current]?.hide?.();
    current = id;
    for (const section of SECTIONS) panes[section.id].hidden = section.id !== id;
    seg.value = id;
    seg.querySelector('[aria-checked="true"]')?.scrollIntoView?.({ inline: "nearest", block: "nearest" });
    head.classList.toggle("is-chat", id === "chat");
    try { localStorage.setItem("hub.learn", id); } catch (_) { /* ignore */ }
    handles[id]?.show?.();
    if (!fromUrl) container.closest(".view")?.scrollTo?.({ top: 0 });
  }

  for (const section of SECTIONS) panes[section.id] = el("div", { class: "learn-pane", "data-section": section.id, hidden: true });
  container.append(head, ...SECTIONS.map((s) => panes[s.id]));
  for (const section of SECTIONS) {
    try {
      handles[section.id] = section.mod.init(panes[section.id], ctx) || {};
    } catch (error) {
      console.error(section.id, error);
      panes[section.id].replaceChildren(ui.emptyState({ icon: "alert", title: `${section.label} couldn't start`, body: error.message }));
      handles[section.id] = {};
    }
  }
  let saved = null;
  try { saved = localStorage.getItem("hub.learn"); } catch (_) { /* ignore */ }
  select(SECTIONS.some((s) => s.id === saved) ? saved : "chat", true);

  // ---- continue learning
  let stripSeq = 0;
  function pill(icon, tone, text, sub, onClick) {
    return el("button", { type: "button", class: "ln-pill", "data-tone": tone, onclick: onClick },
      ui.tile(icon, tone, "sm"),
      el("span", { class: "ln-pill-body" }, el("span", { class: "ln-pill-title ui-truncate" }, text), el("span", { class: "ln-pill-sub ui-truncate" }, sub)));
  }
  async function loadStrip() {
    const mine = ++stripSeq;
    const [history, summary] = await Promise.all([
      api("/api/learn/history").catch(() => null),
      api("/api/cards/summary").catch(() => null),
    ]);
    if (mine !== stripSeq) return;
    let nextTask = null;
    const road = summary?.roadmap_progress?.find((r) => r.percent < 100) || null;
    if (road) {
      try {
        const rm = await api(`/api/roadmaps/${encodeURIComponent(road.id)}`);
        for (const week of rm.weeks || []) {
          const t = (week.tasks || []).find((x) => !x.done);
          if (t) { nextTask = { text: t.text, title: rm.title, id: rm.id }; break; }
        }
      } catch (_) { /* strip still shows the rest */ }
      if (mine !== stripSeq) return;
    }
    const pills = [];
    const days = history?.streak_days ?? summary?.streak ?? 0;
    pills.push(pill("flame", days ? "orange" : "gray", days ? `${days} day streak` : "Start a streak", "Finish a quiz or review", () => select("quiz")));
    if (summary) {
      const due = summary.due_today || 0;
      pills.push(pill("layers", due ? "accent" : "good", due ? `${due} card${due === 1 ? "" : "s"} due` : "Cards all done",
        due ? "Review now" : "Nothing due today", () => { select("cards"); if (due) handles.cards?.openSection?.("review"); }));
    }
    if (nextTask) {
      pills.push(pill("target", "teal", nextTask.text, nextTask.title,
        () => { select("cards"); handles.cards?.openItem?.(nextTask.id, "roadmaps"); }));
    }
    strip.replaceChildren(...pills);
    strip.hidden = !pills.length;
  }
  strip.replaceChildren(el("span", { class: "ui-skel ln-skel" }), el("span", { class: "ui-skel ln-skel" }));
  loadStrip();

  return {
    // "quiz", "cards/roadmaps" or "english/speak": pick the section, pass the rest to it.
    openSection(name) {
      const [raw, rest] = String(name || "").split("/");
      const section = ALIASES[raw] || raw;
      if (panes[section]) select(section);
      const sub = rest || (ALIASES[raw] === "cards" && raw === "roadmaps" ? "roadmaps" : "");
      if (sub) handles[section]?.openSection?.(sub);
    },
    openItem(id, section) {
      const sec = ALIASES[section] || section || "cards";
      if (panes[sec]) select(sec);
      handles[sec]?.openItem?.(id, section);
    },
    refresh() { loadStrip(); handles[current]?.refresh?.(); },
    show() { loadStrip(); handles[current]?.show?.(); },
    hide() { handles[current]?.hide?.(); },
  };
}
