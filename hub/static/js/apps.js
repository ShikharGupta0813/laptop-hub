// Apps: a searchable directory of every feature, the generic runner for the laptop's actions, and the
// Jarvis screen. Adding an option is one entry: built-in ones are the ENTRIES array below, laptop actions
// come from GET /api/laptop/actions and appear automatically.
import { api, el } from "./api.js";
import { createRunner } from "./laptop.js";
import * as ui from "./ui.js";
import { createJarvis } from "./jarvis.js";

// ---- the data -------------------------------------------------------------------------------------
// Entry: {id, title, description, category, keywords, kind: "view"|"jarvis"|"action", target, featured?, isNew?, icon?}
// "view" targets are {view, section?} and open with ctx.open(view, section).
// Laptop actions from the API all land in the "files" category (Files & Laptop), tagged with their own group.
const BUILTIN_CATEGORIES = [
  { key: "talk", title: "Talk & AI", icon: "talk", tone: "violet" },
  { key: "work", title: "Work", icon: "work", tone: "info" },
  { key: "learn", title: "Learn", icon: "learn", tone: "teal" },
  { key: "plan", title: "Notes & Planning", icon: "note", tone: "orange" },
  { key: "money", title: "Money", icon: "trend", tone: "good" },
  { key: "files", title: "Files & Laptop", icon: "laptop", tone: "accent" },
  { key: "monitor", title: "Monitoring", icon: "eye", tone: "pink" },
  { key: "system", title: "System", icon: "settings", tone: "gray" },
];

const view = (id, title, description, category, keywords, target, extra = {}) =>
  ({ id, title, description, category, keywords, kind: "view", target, ...extra });

const ENTRIES = [
  // Talk & AI
  { id: "jarvis", title: "Jarvis", description: "Start or stop your voice assistant on the laptop, and tell it what to do.",
    category: "talk", keywords: ["voice", "hey jarvis", "assistant", "control laptop", "start", "stop", "speak", "command"],
    kind: "jarvis", featured: true, icon: "jarvis" },
  view("assistant", "Talk to assistant", "Ask for anything by voice or text. It can use your laptop.", "talk",
    ["chat", "ai", "claude", "ask", "help", "speak"], { view: "assistant" }, { icon: "talk" }),
  view("assistant-voice", "Voice chat", "Hold a spoken conversation with the assistant.", "talk",
    ["microphone", "speak", "hands free", "voice", "talk", "listen"], { view: "assistant", section: "voice" }, { icon: "mic" }),

  // Work
  view("tasks", "Asana tasks", "What is due today, overdue and coming up.", "work",
    ["todo", "work items", "assigned", "asana", "due", "overdue", "project"], { view: "work", section: "tasks" }, { icon: "list-checks" }),
  view("calendar", "Calendar", "Today's meetings, with join links.", "work",
    ["meetings", "schedule", "agenda", "events", "zoom", "meet", "teams", "join"], { view: "work", section: "calendar" }, { icon: "calendar" }),
  view("email", "Send email", "Write a draft with AI, check it, then send.", "work",
    ["mail", "gmail", "compose", "write", "message", "draft"], { view: "work", section: "email" }, { icon: "mail" }),
  view("gchat", "Google Chat", "Send a message to one of your chat spaces.", "work",
    ["chat", "message", "spaces", "team", "slack"], { view: "work", section: "chat" }, { icon: "talk" }),

  // Learn
  view("study-chat", "Study chat", "Ask questions about a note, a file or any topic.", "learn",
    ["study", "tutor", "explain", "questions", "learn", "ask"], { view: "learn", section: "chat" }, { icon: "book-open" }),
  view("quiz", "Quiz", "Test yourself on your notes or a topic.", "learn",
    ["test", "mcq", "practice", "exam", "revise", "questions"], { view: "learn", section: "quiz" }, { icon: "quiz" }),
  view("cards", "Flashcards", "Review cards made from your notes, spaced for memory.", "learn",
    ["flashcards", "review", "spaced repetition", "memorise", "deck", "anki"], { view: "cards" }, { icon: "layers", isNew: true }),
  view("kb", "Knowledge base", "Ask questions across the documents you added.", "learn",
    ["knowledge", "documents", "library", "search", "rag", "sources", "pdf"], { view: "kb" }, { icon: "book-open", isNew: true }),
  view("english-speak", "English coach", "Speak for a minute and get feedback on how you sounded.", "learn",
    ["speaking", "pronunciation", "fluency", "practice", "talk", "english"], { view: "learn", section: "english/speak" }, { icon: "language" }),
  view("roleplay", "Role-play", "Practise a real conversation with a partner.", "learn",
    ["conversation", "interview", "dialogue", "english", "practice"], { view: "learn", section: "english/roleplay" }, { icon: "users" }),
  view("correct", "Fix my writing", "Corrects your grammar and explains why.", "learn",
    ["grammar", "proofread", "rewrite", "spelling", "edit", "english"], { view: "learn", section: "english/correct" }, { icon: "pen" }),
  view("vocab", "Vocabulary", "New words every day, with quick reviews.", "learn",
    ["words", "flashcards", "dictionary", "meaning", "english", "review"], { view: "learn", section: "english/vocab" }, { icon: "book" }),
  view("progress", "English progress", "Streaks, scores and trends over time.", "learn",
    ["stats", "streak", "history", "score", "english", "trend"], { view: "learn", section: "english/progress" }, { icon: "chart" }),

  // Notes & Planning
  view("notes", "Notes", "Your notes, tagged for you.", "plan",
    ["memo", "write", "jot", "ideas", "tags", "search notes"], { view: "notes" }, { icon: "note" }),
  view("voice-note", "Voice note", "Say a thought and it is written down.", "plan",
    ["record", "dictate", "microphone", "audio", "speak", "mic"], { view: "notes", section: "voice" }, { icon: "mic" }),
  view("todos", "To-do", "A simple list for today, with due dates.", "plan",
    ["tasks", "checklist", "list", "personal", "things to do"], { view: "todos" }, { icon: "list-checks", isNew: true }),
  view("habits", "Habits", "Build streaks one day at a time.", "plan",
    ["streak", "routine", "daily", "track", "goals"], { view: "habits" }, { icon: "flame", isNew: true }),
  view("journal", "Journal", "A private page for each day.", "plan",
    ["diary", "reflect", "mood", "write", "daily"], { view: "journal" }, { icon: "book", isNew: true }),
  view("focus", "Focus timer", "A big timer for deep work, with breaks.", "plan",
    ["pomodoro", "timer", "concentrate", "deep work", "study"], { view: "focus" }, { icon: "target", isNew: true }),
  view("reminders", "Reminders", "Get a nudge at the right time.", "plan",
    ["alarm", "remind", "notify", "later", "schedule"], { view: "reminders" }, { icon: "alarm", isNew: true }),
  view("bookmarks", "Read later", "Save links and get a quick summary of each.", "plan",
    ["bookmarks", "links", "articles", "save", "tldr", "web", "url"], { view: "bookmarks" }, { icon: "bookmark", isNew: true }),
  view("countdowns", "Countdowns", "Days left to birthdays, trips and deadlines.", "plan",
    ["days", "birthday", "event", "date", "anniversary", "trip"], { view: "tools", section: "countdowns" }, { icon: "calendar", isNew: true }),

  // Money
  view("expenses", "Spending", "Type '450 lunch' and it is logged. See the month at a glance.", "money",
    ["expenses", "budget", "money", "rupees", "cost", "finance", "bills"], { view: "expenses" }, { icon: "trend", isNew: true }),

  // Files & Laptop
  view("files", "Files", "Browse and read the folders you share. Select text to explain it.", "files",
    ["documents", "folder", "browse", "pdf", "read", "download", "images"], { view: "files" }, { icon: "folder" }),
  view("terminal", "Terminal", "A real shell on the laptop: PowerShell, WSL or bash.", "files",
    ["shell", "powershell", "bash", "command line", "cmd", "wsl", "console", "ssh"], { view: "terminal" }, { icon: "terminal" }),
  view("hub", "Laptop status and upload", "Laptop health, quick buttons, send files to the laptop.", "files",
    ["status", "battery", "cpu", "disk", "upload", "logout", "install", "settings", "send file", "hub"], { view: "hub" }, { icon: "laptop" }),
  view("inbox", "Inbox", "Files and links your phone sent to the laptop, and back.", "files",
    ["received", "shared", "files", "capture", "share"], { view: "inbox" }, { icon: "inbox", isNew: true }),
  view("screen", "Remote screen", "See the laptop screen and tap through it.", "files",
    ["screenshot", "cast", "mirror", "view", "remote desktop", "display"], { view: "screen" }, { icon: "cast", isNew: true }),

  // Monitoring
  view("watch", "Watch", "Get told when a page, price or file changes.", "monitor",
    ["monitor", "alerts", "track", "change", "website", "price"], { view: "watch" }, { icon: "eye", isNew: true }),
  view("notifications", "Notifications", "Everything the hub has told you, in one place.", "monitor",
    ["alerts", "bell", "messages", "unread", "inbox"], { view: "notifications" }, { icon: "bell" }),

  // System
  view("settings", "Settings", "Appearance, notifications and how the hub behaves.", "system",
    ["theme", "accent", "dark", "light", "preferences", "options", "appearance"], { view: "settings" }, { icon: "settings" }),
  view("automations", "Automations", "When this happens, do that, on the laptop or phone.", "system",
    ["rules", "workflow", "triggers", "schedule", "if this then that"], { view: "automations" }, { icon: "zap", isNew: true }),
  view("tools", "Tools", "Calculator, converter, world clock, snippets and more.", "system",
    ["utilities", "calc", "convert", "clock", "snippets", "clipboard"], { view: "tools" }, { icon: "sliders", isNew: true }),
  view("calculator", "Calculator", "Type a sum like 12% of 480 and get the answer.", "system",
    ["calc", "maths", "arithmetic", "percent", "sum", "expression"], { view: "tools", section: "calculator" }, { icon: "hash", isNew: true }),
  view("converter", "Unit converter", "Length, weight, temperature, data and more.", "system",
    ["convert", "units", "km", "miles", "celsius", "fahrenheit", "kg", "lb"], { view: "tools", section: "converter" }, { icon: "arrow-right", isNew: true }),
  view("world-clock", "World clock", "What time it is in your cities.", "system",
    ["time", "timezone", "zones", "utc", "clock", "india", "london"], { view: "tools", section: "clock" }, { icon: "globe", isNew: true }),
  view("snippets", "Snippets", "Text you paste often, one tap to copy.", "system",
    ["text", "templates", "paste", "saved", "boilerplate", "reply"], { view: "tools", section: "snippets" }, { icon: "note-plus", isNew: true }),
  view("clipboard-history", "Clipboard history", "The last things you copied, and a phone-to-laptop paste.", "system",
    ["clip", "copy", "paste", "history", "clipboard"], { view: "tools", section: "clipboard" }, { icon: "clipboard", isNew: true }),
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
  let actionState = "idle"; // idle | loading | ok | error
  let actionError = "";
  let actionsAt = 0;
  let loading = null;
  let category = "all";
  let panel = "dir";
  let dirScroll = 0;

  // ---- panels -------------------------------------------------------------------------------------
  const search = el("input", { type: "search", id: "ap-search", class: "ap-search-input ui-input--lg", placeholder: "Search every feature",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "search", "aria-controls": "ap-results" });
  const clearBtn = el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--icon ap-clear", "aria-label": "Clear search", hidden: true,
    onclick: () => { search.value = ""; renderAll(); search.focus(); } }, ui.icon("close", 18));
  const chips = el("div", { class: "ap-chips ui-hscroll", role: "group", "aria-label": "Categories" });
  const note = el("div", { class: "ap-note", hidden: true });
  const live = el("p", { class: "ap-live", "aria-live": "polite" });
  const results = el("div", { class: "ap-results", id: "ap-results" });
  const title = el("h2", { class: "ap-title sr-only", tabindex: "-1" }, "Apps");
  const subtitle = el("p", { class: "ap-desc" }, "");
  const dirPanel = el("section", { class: "ap-panel", "data-panel": "dir" },
    el("div", { class: "ap-runner-head" }, title, subtitle),
    el("div", { class: "ap-search ui-input-wrap" }, ui.icon("search", 20), el("label", { class: "sr-only", for: "ap-search" }, "Search apps and actions"), search, clearBtn),
    chips, note, live, results);

  const runnerBody = el("div", {});
  const jarvisBody = el("div", {});
  const mkBack = () => el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--sm ap-back", onclick: () => back() }, ui.icon("arrow-left", 18), "Back to Apps");
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
  }

  // ---- entries ------------------------------------------------------------------------------------
  const allEntries = () => [...ENTRIES, ...actionEntries];
  const allCategories = () => BUILTIN_CATEGORIES;
  const byId = (id) => allEntries().find((e) => e.id === id);
  const categoryOf = (e) => BUILTIN_CATEGORIES.find((c) => c.key === e.category);

  function haystack(e) {
    if (!e._hay) {
      const cat = categoryOf(e);
      e._hay = [e.title, e.description, ...(e.keywords || []), cat ? cat.title : "", e.group || ""].join(" ").toLowerCase();
    }
    return e._hay;
  }
  function matches(e, query) {
    if (!query) return true;
    const hay = haystack(e);
    return query.split(/\s+/).every((word) => hay.includes(word));
  }

  const ACTION_ICON = { system: "info", processes: "activity", launch: "external", screen: "monitor", clipboard: "clipboard",
    media: "volume", apps: "grid", buttons: "zap" };

  async function loadActions() {
    if (loading) return loading;
    actionState = "loading";
    renderNote();
    loading = (async () => {
      try {
        const data = await api("/api/laptop/actions");
        const titles = new Map((data.categories || []).map((c) => [c.id, c.title]));
        actionEntries = (data.actions || []).map((a) => ({
          id: `action:${a.id}`, title: a.title, description: a.description || "", category: "files",
          keywords: [a.id.replace(/[_:]/g, " "), a.category, titles.get(a.category) || "", "laptop", "action"],
          kind: "action", target: a.id, action: a, available: a.available !== false, group: titles.get(a.category) || a.category,
          icon: ACTION_ICON[a.category] || "laptop",
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
    ui.haptic("select");
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
      note.append(ui.notice(el("span", {}, "Laptop actions couldn't be loaded. The built-in apps still work. ", actionError ? el("span", { class: "ap-note-why" }, actionError) : null,
        " ", el("button", { type: "button", class: "ui-link", onclick: () => loadActions() }, "Retry")), { tone: "warn" }));
    } else if (actionState === "loading") {
      note.hidden = false;
      note.append(el("div", { class: "ap-loading" }, el("span", { class: "ui-spinner", "aria-hidden": "true" }), "Loading laptop actions…"));
    } else {
      note.hidden = true;
    }
  }

  function toneFor(e) {
    const cat = categoryOf(e);
    return cat ? cat.tone : "accent";
  }

  function card(e, section) {
    const off = e.kind === "action" && e.available === false;
    const fav = isFav(e.id);
    const featured = !!e.featured;
    const open = el("button", { type: "button", class: "ap-open", "data-id": e.id, "aria-disabled": off ? "true" : null,
      onclick: () => openEntry(e) },
      featured ? el("span", { class: "ap-ico-wrap" }, ui.icon(e.icon || "apps", 28)) : ui.tile(e.icon || "apps", toneFor(e)),
      el("span", { class: "ap-card-text" },
        el("span", { class: "ap-card-title" }, e.title,
          e.isNew && !featured ? ui.badge("New", "accent") : null),
        e.group ? el("span", { class: "ap-card-group" }, e.group) : null,
        el("span", { class: "ap-card-desc" }, off ? `Unavailable: ${e.reason}` : e.description)));
    const star = el("button", { type: "button", class: "ap-star", "data-id": e.id, "aria-pressed": String(fav),
      "aria-label": `Favourite ${e.title}`, onclick: () => {
        toggleFav(e.id);
        renderAll();
        const again = results.querySelector(`[data-section="${section}"] .ap-star[data-id="${CSS.escape(e.id)}"]`)
          || results.querySelector(`.ap-star[data-id="${CSS.escape(e.id)}"]`);
        if (again) again.focus({ preventScroll: true });
      } }, ui.icon("star", 18));
    const cls = featured ? "ui-card ui-card--gradient ui-card--interactive" : "ui-card ui-card--interactive";
    return el("div", { class: `${cls} ap-card${off ? " off" : ""}${featured ? " featured" : ""}`, "data-kind": e.kind }, open, star);
  }

  // A compact tile for the Favourites row.
  function favTile(e) {
    return el("button", { type: "button", class: "ap-fav", "data-id": e.id, onclick: () => openEntry(e) },
      ui.tile(e.icon || "apps", toneFor(e), "lg"), el("span", { class: "ap-fav-label" }, e.title));
  }

  function section(key, heading, entries) {
    return el("section", { class: "ap-section", "data-section": key, "aria-labelledby": `ap-h-${key}` },
      el("div", { class: "ui-section-head" }, el("h3", { id: `ap-h-${key}`, class: "ui-section-title" }, heading),
        el("span", { class: "ap-count" }, String(entries.length))),
      el("div", { class: "ap-grid ui-stagger" }, entries.map((e) => card(e, key))));
  }

  function rank(e, query) {
    const t = e.title.toLowerCase();
    return t === query ? 0 : t.startsWith(query) ? 1 : t.includes(query) ? 2 : 3;
  }

  function renderChips(query) {
    const counts = new Map();
    let total = 0;
    for (const e of allEntries()) {
      if (!matches(e, query)) continue;
      total += 1;
      counts.set(e.category, (counts.get(e.category) || 0) + 1);
    }
    const make = (key, label, count, icon) => el("button", { type: "button", class: `ui-chip ap-chip${category === key ? " is-on" : ""}`,
      "aria-pressed": String(category === key), "data-cat": key, onclick: () => { category = key; renderAll(); } },
      icon ? ui.icon(icon) : null, label, el("span", { class: "ap-chip-n" }, String(count)));
    chips.replaceChildren(make("all", "All", total, "grid"),
      ...allCategories().map((c) => make(c.key, c.title, counts.get(c.key) || 0, c.icon)));
    return total;
  }

  let firstRender = true;
  function renderAll() {
    const query = search.value.trim().toLowerCase();
    clearBtn.hidden = !search.value;
    renderNote();
    renderChips(query);
    subtitle.textContent = `${allEntries().length} things you can do. Star the ones you use most.`;

    const pool = allEntries().filter((e) => matches(e, query) && (category === "all" || e.category === category));
    results.replaceChildren();
    if (!pool.length) {
      live.textContent = "";
      results.append(ui.emptyState({ icon: "search", title: query ? `Nothing matches "${search.value.trim()}"` : "Nothing in this category yet",
        body: query ? "Try a different word, like a feature or an app name." : "",
        action: query ? { label: "Clear search", icon: "close", onClick: () => { search.value = ""; category = "all"; renderAll(); search.focus(); } } : null }));
      return;
    }
    live.textContent = query ? `${pool.length} ${pool.length === 1 ? "match" : "matches"} for "${search.value.trim()}"` : "";

    const featured = pool.filter((e) => e.featured);
    const rest = pool.filter((e) => !e.featured);
    if (featured.length) {
      results.append(el("section", { class: "ap-section ap-featured", "data-section": "featured", "aria-label": "Featured" },
        featured.map((e) => card(e, "featured"))));
    }
    if (query) {
      rest.sort((a, b) => rank(a, query) - rank(b, query));
      results.append(section("results", "Results", rest));
    } else if (category === "all") {
      const favEntries = favs.map(byId).filter((e) => e && !e.featured);
      const recentEntries = recent.map(byId).filter(Boolean);
      if (favEntries.length) {
        results.append(el("section", { class: "ap-section", "data-section": "fav", "aria-labelledby": "ap-h-fav" },
          el("div", { class: "ui-section-head" }, el("h3", { id: "ap-h-fav", class: "ui-section-title" }, "Favourites")),
          el("div", { class: "ap-favs ui-hscroll" }, favEntries.map(favTile))));
      }
      if (recentEntries.length) {
        results.append(el("section", { class: "ap-section", "data-section": "recent", "aria-labelledby": "ap-h-recent" },
          el("div", { class: "ui-section-head" }, el("h3", { id: "ap-h-recent", class: "ui-section-title" }, "Recent")),
          el("div", { class: "ap-recents ui-hscroll" }, recentEntries.map((e) => el("button", { type: "button", class: "ui-chip ap-recent",
            "data-id": e.id, onclick: () => openEntry(e) }, ui.icon(e.icon || "apps"), e.title)))));
      }
      for (const cat of allCategories()) {
        const inCat = rest.filter((e) => e.category === cat.key);
        if (inCat.length) results.append(section(`cat-${cat.key}`, cat.title, inCat));
      }
    } else if (rest.length) {
      results.append(section("list", allCategories().find((c) => c.key === category)?.title || "Apps", rest));
    }
    if (firstRender) {
      firstRender = false;
      setTimeout(() => results.querySelectorAll(".ui-stagger").forEach((n) => n.classList.remove("ui-stagger")), 1000);
    } else {
      results.querySelectorAll(".ui-stagger").forEach((n) => n.classList.remove("ui-stagger"));
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
    refresh() { loadActions(); },
  };
}
