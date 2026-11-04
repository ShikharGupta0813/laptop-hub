// Apps: a searchable directory of every feature, the generic runner for the laptop's actions, and the
// Jarvis screen. Adding an option is one entry: built-in ones are the ENTRIES array below, laptop actions
// come from GET /api/laptop/actions and appear automatically.
import { api, el } from "./api.js";
import { createRunner, icon } from "./laptop.js";
import { createJarvis } from "./jarvis.js";

// ---- the data -------------------------------------------------------------------------------------
// Entry: {id, title, description, category, keywords, kind: "view"|"jarvis"|"action", target, featured?, icon?}
// "view" targets are {view, section?} and open with ctx.open(view, section).
const BUILTIN_CATEGORIES = [
  { key: "assistant", title: "Assistant", icon: "assistant" },
  { key: "work", title: "Work", icon: "work" },
  { key: "learn", title: "Learn", icon: "learn" },
  { key: "notes", title: "Notes", icon: "notes" },
  { key: "laptop", title: "Laptop", icon: "laptop" },
];

const view = (id, title, description, category, keywords, target) =>
  ({ id, title, description, category, keywords, kind: "view", target });

const ENTRIES = [
  { id: "jarvis", title: "Jarvis", description: "Start or stop your voice assistant on the laptop, and tell it what to do.",
    category: "assistant", keywords: ["voice", "hey jarvis", "assistant", "control laptop", "start", "stop", "speak", "command"],
    kind: "jarvis", featured: true, icon: "jarvis" },
  view("assistant", "Talk to assistant", "Ask for anything by voice or text. It can use your laptop.", "assistant",
    ["chat", "ai", "claude", "ask", "help", "speak"], { view: "assistant" }),

  view("tasks", "Asana tasks", "What is due today, overdue and coming up.", "work",
    ["todo", "work items", "assigned", "asana", "due", "overdue", "project"], { view: "work", section: "tasks" }),
  view("calendar", "Calendar", "Today's meetings, with join links.", "work",
    ["meetings", "schedule", "agenda", "events", "zoom", "meet", "teams", "join"], { view: "work", section: "calendar" }),
  view("email", "Send email", "Write a draft with AI, check it, then send.", "work",
    ["mail", "gmail", "compose", "write", "message", "draft"], { view: "work", section: "email" }),
  view("gchat", "Google Chat", "Send a message to one of your chat spaces.", "work",
    ["chat", "message", "spaces", "team", "slack"], { view: "work", section: "chat" }),

  view("study-chat", "Study chat", "Ask questions about a note, a file or any topic.", "learn",
    ["study", "tutor", "explain", "questions", "learn", "ask"], { view: "learn", section: "chat" }),
  view("quiz", "Quiz", "Test yourself on your notes or a topic.", "learn",
    ["test", "mcq", "practice", "exam", "revise", "questions"], { view: "learn", section: "quiz" }),
  view("english-speak", "English coach", "Speak for a minute and get feedback on how you sounded.", "learn",
    ["speaking", "pronunciation", "fluency", "practice", "talk", "english"], { view: "learn", section: "english/speak" }),
  view("roleplay", "Role-play", "Practise a real conversation with a partner.", "learn",
    ["conversation", "interview", "dialogue", "english", "practice"], { view: "learn", section: "english/roleplay" }),
  view("correct", "Fix my writing", "Corrects your grammar and explains why.", "learn",
    ["grammar", "proofread", "rewrite", "spelling", "edit", "english"], { view: "learn", section: "english/correct" }),
  view("vocab", "Vocabulary", "New words every day, with quick reviews.", "learn",
    ["words", "flashcards", "dictionary", "meaning", "english", "review"], { view: "learn", section: "english/vocab" }),
  view("progress", "English progress", "Streaks, scores and trends over time.", "learn",
    ["stats", "streak", "history", "score", "english", "trend"], { view: "learn", section: "english/progress" }),

  view("notes", "Notes", "Your notes, tagged for you.", "notes",
    ["memo", "write", "jot", "ideas", "tags", "search notes"], { view: "notes" }),
  view("voice-note", "Voice note", "Say a thought and it is written down.", "notes",
    ["record", "dictate", "microphone", "audio", "speak", "mic"], { view: "notes" }),

  view("files", "Files", "Browse and read the folders you share. Select text to explain it.", "laptop",
    ["documents", "folder", "browse", "pdf", "read", "download", "images"], { view: "files" }),
  view("terminal", "Terminal", "A real shell on the laptop: PowerShell, WSL or bash.", "laptop",
    ["shell", "powershell", "bash", "command line", "cmd", "wsl", "console", "ssh"], { view: "terminal" }),
  view("hub", "Hub status and upload", "Laptop health, quick buttons, send files to the laptop.", "laptop",
    ["status", "battery", "cpu", "disk", "upload", "inbox", "logout", "install", "settings", "send file"], { view: "hub" }),
];

const FAV_KEY = "hub.apps.fav";
const RECENT_KEY = "hub.apps.recent";
const DEFAULT_FAVS = ["jarvis", "terminal", "files", "tasks"];
const RECENT_MAX = 6;
const STALE_MS = 60000;

function readList(key) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const value = JSON.parse(raw);
    return Array.isArray(value) ? value.filter((x) => typeof x === "string") : null;
  } catch (_) {
    return null;
  }
}
function writeList(key, list) {
  try { localStorage.setItem(key, JSON.stringify(list)); } catch (_) { /* private mode */ }
}

export function init(container, ctx) {
  container.classList.add("ap-root");
  let favs = readList(FAV_KEY) || [...DEFAULT_FAVS];
  let recent = readList(RECENT_KEY) || [];
  let actionEntries = [];
  let actionCategories = [];
  let actionState = "idle"; // idle | loading | ok | error
  let actionError = "";
  let actionsAt = 0;
  let loading = null;
  let category = "all";
  let panel = "dir";
  let dirScroll = 0;

  // ---- panels -------------------------------------------------------------------------------------
  const search = el("input", { type: "search", id: "ap-search", class: "ap-search-input", placeholder: "Search every feature",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "search", "aria-controls": "ap-results" });
  const clearBtn = el("button", { type: "button", class: "ap-clear", "aria-label": "Clear search", hidden: true,
    onclick: () => { search.value = ""; renderAll(); search.focus(); } }, icon("close", 20));
  const chips = el("div", { class: "ap-chips", role: "group", "aria-label": "Categories" });
  const note = el("div", { class: "ap-note", hidden: true });
  const live = el("p", { class: "muted ap-live", "aria-live": "polite" });
  const results = el("div", { class: "ap-results", id: "ap-results" });
  const title = el("h2", { class: "ap-title", tabindex: "-1" }, "Apps");
  const subtitle = el("p", { class: "muted ap-desc" }, "");
  const dirPanel = el("section", { class: "ap-panel", "data-panel": "dir" },
    el("div", { class: "ap-runner-head" }, title, subtitle),
    el("div", { class: "ap-search" }, icon("search", 20), el("label", { class: "sr-only", for: "ap-search" }, "Search apps and actions"), search, clearBtn),
    chips, note, live, results);

  const runnerBody = el("div", {});
  const jarvisBody = el("div", {});
  const mkBack = () => el("button", { type: "button", class: "btn ap-linkbtn ap-back", onclick: () => back() }, icon("back", 18), "Back to Apps");
  const runnerPanel = el("section", { class: "ap-panel", "data-panel": "runner", hidden: true }, mkBack(), runnerBody);
  const jarvisPanel = el("section", { class: "ap-panel", "data-panel": "jarvis", hidden: true }, mkBack(), jarvisBody);
  container.append(dirPanel, runnerPanel, jarvisPanel);

  const runner = createRunner(runnerBody, ctx, { getAction: (id) => actionEntries.find((e) => e.target === id)?.action });
  const jarvis = createJarvis(jarvisBody, ctx);
  const panels = { dir: dirPanel, runner: runnerPanel, jarvis: jarvisPanel };

  function showPanel(name) {
    if (panel === "dir") dirScroll = container.scrollTop;
    panel = name;
    for (const [key, node] of Object.entries(panels)) node.hidden = key !== name;
    if (name !== "runner") runner.close();
    if (name !== "jarvis") jarvis.close();
    container.scrollTop = name === "dir" ? dirScroll : 0;
  }

  function back() {
    showPanel("dir");
    renderAll();
    search.focus({ preventScroll: true });
  }

  // ---- entries ------------------------------------------------------------------------------------
  const allEntries = () => [...ENTRIES, ...actionEntries];
  const allCategories = () => [...BUILTIN_CATEGORIES, ...actionCategories];
  const byId = (id) => allEntries().find((e) => e.id === id);
  const categoryOf = (e) => allCategories().find((c) => c.key === e.category);

  function haystack(e) {
    if (!e._hay) {
      const cat = categoryOf(e);
      e._hay = [e.title, e.description, ...(e.keywords || []), cat ? cat.title : ""].join(" ").toLowerCase();
    }
    return e._hay;
  }
  function matches(e, query) {
    if (!query) return true;
    const hay = haystack(e);
    return query.split(/\s+/).every((word) => hay.includes(word));
  }

  async function loadActions() {
    if (loading) return loading;
    actionState = "loading";
    renderNote();
    loading = (async () => {
      try {
        const data = await api("/api/laptop/actions");
        const titles = new Map((data.categories || []).map((c) => [c.id, c.title]));
        const order = (data.categories || []).map((c) => c.id);
        for (const a of data.actions || []) if (!order.includes(a.category)) order.push(a.category);
        actionCategories = order
          .filter((id) => (data.actions || []).some((a) => a.category === id))
          .map((id) => ({ key: `x-${id}`, title: titles.get(id) || id, icon: id }));
        actionEntries = (data.actions || []).map((a) => ({
          id: `action:${a.id}`, title: a.title, description: a.description || "", category: `x-${a.category}`,
          keywords: [a.id.replace(/[_:]/g, " "), a.category, titles.get(a.category) || ""],
          kind: "action", target: a.id, action: a, available: a.available !== false,
          reason: a.unavailable_reason || "Not available on this laptop.",
        }));
        actionState = "ok";
        actionError = "";
        actionsAt = Date.now();
      } catch (error) {
        if (error && error.name === "AbortError") return;
        actionState = "error";
        actionError = error.message || "";
      } finally {
        loading = null;
      }
      renderAll();
    })();
    return loading;
  }

  // ---- favourites and recents -----------------------------------------------------------------------
  const isFav = (id) => favs.includes(id);
  function toggleFav(id) {
    favs = isFav(id) ? favs.filter((x) => x !== id) : [...favs, id];
    writeList(FAV_KEY, favs);
  }
  function pushRecent(id) {
    recent = [id, ...recent.filter((x) => x !== id)].slice(0, RECENT_MAX);
    writeList(RECENT_KEY, recent);
  }

  // ---- opening --------------------------------------------------------------------------------------
  function openEntry(e) {
    if (e.kind === "action" && e.available === false) {
      ctx.toast(e.reason);
      return;
    }
    pushRecent(e.id);
    if (e.kind === "view") {
      ctx.open(e.target.view, e.target.section);
    } else if (e.kind === "jarvis") {
      showPanel("jarvis");
      jarvis.open();
      jarvisPanel.querySelector(".ap-title")?.focus({ preventScroll: true });
    } else if (e.kind === "action") {
      showPanel("runner");
      runner.open(e.action);
    }
  }

  // ---- rendering ------------------------------------------------------------------------------------
  function renderNote() {
    note.replaceChildren();
    if (actionState === "error") {
      note.hidden = false;
      note.append(el("span", {}, "Laptop actions couldn't be loaded. The built-in apps still work."),
        el("button", { type: "button", class: "btn", onclick: () => loadActions() }, "Retry"));
      if (actionError) note.append(el("span", { class: "muted ap-note-why" }, actionError));
    } else if (actionState === "loading") {
      note.hidden = false;
      note.append(el("span", { class: "muted" }, "Loading laptop actions…"));
    } else {
      note.hidden = true;
    }
  }

  function card(e, section) {
    const off = e.kind === "action" && e.available === false;
    const cat = categoryOf(e);
    const fav = isFav(e.id);
    const open = el("button", { type: "button", class: "ap-open", "data-id": e.id, "aria-disabled": off ? "true" : null,
      onclick: () => openEntry(e) },
      el("span", { class: "ap-ico-wrap", "aria-hidden": "true" }, icon(e.icon || (cat && cat.icon) || "apps", 22)),
      el("span", { class: "ap-card-title" }, e.title),
      el("span", { class: "ap-card-desc" }, off ? `Unavailable: ${e.reason}` : e.description));
    const star = el("button", { type: "button", class: "ap-star", "data-id": e.id, "aria-pressed": String(fav),
      "aria-label": `Favourite ${e.title}`, onclick: () => {
        toggleFav(e.id);
        renderAll();
        const again = results.querySelector(`[data-section="${section}"] .ap-star[data-id="${CSS.escape(e.id)}"]`)
          || results.querySelector(`.ap-star[data-id="${CSS.escape(e.id)}"]`);
        if (again) again.focus({ preventScroll: true });
      } }, icon("star", 20));
    return el("div", { class: `ap-card${off ? " off" : ""}${e.featured ? " featured" : ""}`, "data-kind": e.kind }, open, star);
  }

  function section(key, heading, entries) {
    return el("section", { class: "ap-section", "data-section": key, "aria-labelledby": `ap-h-${key}` },
      el("h3", { id: `ap-h-${key}` }, heading, el("span", { class: "ap-count" }, String(entries.length))),
      el("div", { class: "ap-grid" }, entries.map((e) => card(e, key))));
  }

  function rank(e, query) {
    const t = e.title.toLowerCase();
    return t === query ? 0 : t.startsWith(query) ? 1 : t.includes(query) ? 2 : 3;
  }

  function renderChips(query) {
    const all = allEntries();
    const counts = new Map();
    let total = 0;
    for (const e of all) {
      if (!matches(e, query)) continue;
      total += 1;
      counts.set(e.category, (counts.get(e.category) || 0) + 1);
    }
    const make = (key, label, count) => el("button", { type: "button", class: "chip ap-chip", "aria-pressed": String(category === key),
      "data-cat": key, onclick: () => { category = key; renderAll(); } }, label, el("span", { class: "ap-chip-n" }, String(count)));
    chips.replaceChildren(make("all", "All", total),
      ...allCategories().map((c) => make(c.key, c.title, counts.get(c.key) || 0)));
    return total;
  }

  function renderAll() {
    const query = search.value.trim().toLowerCase();
    clearBtn.hidden = !search.value;
    if (category !== "all" && !allCategories().some((c) => c.key === category)) category = "all";
    renderNote();
    renderChips(query);
    subtitle.textContent = `${allEntries().length} options in one place. Star the ones you use most.`;

    const pool = allEntries().filter((e) => matches(e, query) && (category === "all" || e.category === category));
    results.replaceChildren();
    if (!pool.length) {
      live.textContent = query ? `Nothing matches "${search.value.trim()}"` : "Nothing here yet.";
      results.append(el("div", { class: "ap-empty card" },
        el("p", {}, query ? `Nothing matches "${search.value.trim()}"` : "Nothing in this category yet."),
        query ? el("button", { type: "button", class: "btn", onclick: () => { search.value = ""; category = "all"; renderAll(); search.focus(); } }, "Clear search") : null));
      return;
    }
    live.textContent = query
      ? `${pool.length} ${pool.length === 1 ? "match" : "matches"} for "${search.value.trim()}"`
      : (category === "all" ? "" : `${pool.length} in ${allCategories().find((c) => c.key === category)?.title || category}`);

    const featured = pool.filter((e) => e.featured);
    const rest = pool.filter((e) => !e.featured);
    if (featured.length) {
      results.append(el("section", { class: "ap-section ap-featured", "data-section": "featured", "aria-label": "Featured" },
        el("div", { class: "ap-grid one" }, featured.map((e) => card(e, "featured")))));
    }
    if (query) {
      rest.sort((a, b) => rank(a, query) - rank(b, query));
      results.append(section("results", "Results", rest));
      return;
    }
    if (category === "all") {
      const favEntries = favs.map(byId).filter((e) => e && !e.featured);
      const recentEntries = recent.map(byId).filter(Boolean);
      if (favEntries.length) results.append(section("fav", "Favourites", favEntries));
      if (recentEntries.length) results.append(section("recent", "Recent", recentEntries));
      for (const cat of allCategories()) {
        const inCat = rest.filter((e) => e.category === cat.key);
        if (inCat.length) results.append(section(`cat-${cat.key}`, cat.title, inCat));
      }
    } else if (rest.length) {
      results.append(section("list", allCategories().find((c) => c.key === category)?.title || "Apps", rest));
    }
  }

  search.addEventListener("input", renderAll);
  search.addEventListener("keydown", (event) => { if (event.key === "Escape" && search.value) { search.value = ""; renderAll(); } });

  renderAll();
  loadActions();

  return {
    show() {
      if (panel === "jarvis") jarvis.open();
      if (panel === "dir") renderAll();
      if (actionState === "error" || (actionState === "ok" && Date.now() - actionsAt > STALE_MS)) loadActions();
    },
    hide() {
      jarvis.close();
    },
    async openSection(name) {
      if (name === "jarvis") {
        openEntry(byId("jarvis"));
      } else if (typeof name === "string" && name.startsWith("action:")) {
        if (actionState === "loading" && loading) await loading;
        else if (actionState !== "ok") await loadActions();
        const entry = byId(name);
        if (entry) openEntry(entry);
        else ctx.toast("That laptop action isn't available.");
      } else {
        back();
      }
    },
  };
}
