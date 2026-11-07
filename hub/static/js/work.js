// Work screen: Asana tasks, calendar, email and Google Chat in one tab, with a daily briefing on top.
// Everything from the API is rendered with el()/textContent, never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { providerSegmented, normalizeProvider, defaultProvider, providerLabel } from "./providers.js";

// ui.busy does not stop a second tap while the first is running, so a double tap sent two requests.
const busy = (btn, fn) => (btn.getAttribute("aria-busy") === "true" ? Promise.resolve() : ui.busy(btn, fn));
const asking = new Set(); // confirm dialogs already open, so a double tap does not stack two

// replaceChildren that skips null/false entries (conditional children).
function fill(node, ...kids) { node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false)); }

const SECTIONS = [
  { id: "tasks", label: "Tasks", key: "asana", name: "Asana" },
  { id: "calendar", label: "Calendar", key: "calendar", name: "Calendar" },
  { id: "email", label: "Email", key: "email", name: "Email" },
  { id: "chat", label: "Chat", key: "chat", name: "Google Chat" },
];
const STATUS_TTL_MS = 30000;
const REPLY_DRAFT_KEY = "hub.draft.reply";

// ---- small helpers -------------------------------------------------------------------------

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { /* private mode */ }
  return null;
}

function takeReplyDraft() {
  try {
    const raw = sessionStorage.getItem(REPLY_DRAFT_KEY);
    if (raw == null) return null;
    sessionStorage.removeItem(REPLY_DRAFT_KEY);
    const d = JSON.parse(raw);
    return d && typeof d === "object" ? d : null;
  } catch (_) { return null; }
}

function pad(n) { return String(n).padStart(2, "0"); }
function isoDay(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function parseDay(s) {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}
function addDays(s, n) {
  const d = parseDay(s);
  return isoDay(new Date(d.getFullYear(), d.getMonth(), d.getDate() + n));
}
function dayDiff(s) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((parseDay(s) - today) / 86400000);
}

// -> {text, level: bad|warn|accent|none}
function dueInfo(due) {
  if (!due) return { text: "No date", level: "none" };
  const diff = dayDiff(due);
  if (diff < 0) return { text: diff === -1 ? "Yesterday" : `${-diff} days late`, level: "bad" };
  if (diff === 0) return { text: "Today", level: "warn" };
  if (diff === 1) return { text: "Tomorrow", level: "accent" };
  const date = parseDay(due).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return { text: date, level: "none" };
}
function dueBadge(due) {
  const info = dueInfo(due);
  return el("span", { class: `ui-badge${info.level === "none" ? "" : ` ui-badge--${info.level}`}` }, info.text);
}

function whenText(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function timeText(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return ui.clockTime(d);
}

const isHttp = (url) => typeof url === "string" && /^https?:\/\//i.test(url);
const PLATFORMS = { zoom: "Zoom", meet: "Meet", teams: "Teams" };

function message(error) {
  return error && error.message ? error.message : "Something went wrong.";
}

function errorBlock(text, onRetry) {
  return el("div", { class: "wk-error", role: "alert" },
    ui.notice(text, { tone: "bad" }),
    onRetry ? ui.button({ label: "Try again", icon: "refresh", variant: "secondary", size: "sm", onClick: onRetry }) : null);
}

function emptyBlock(icon, title, body) {
  return ui.emptyState({ icon, title, body, compact: true });
}

function inlineMsg(tone) {
  const p = el("p", { class: `wk-msg${tone ? ` wk-msg--${tone}` : ""}`, role: "alert", hidden: true });
  p.show = (text, t) => { p.textContent = text; p.className = `wk-msg${t || tone ? ` wk-msg--${t || tone}` : ""}`; p.hidden = false; };
  p.clear = () => { p.hidden = true; };
  return p;
}

function field(label, control, extra) {
  return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, extra);
}

function sectionHead(title, trailing) {
  return el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, title), trailing || null);
}

// ---- module --------------------------------------------------------------------------------

export function init(container, ctx) {
  let current = null;
  let status = null;
  let statusAt = 0;
  let statusPromise = null;

  // ---- status (which parts are set up) ----------------------------------------------------
  function loadStatus(force = false) {
    if (!force && status && Date.now() - statusAt < STATUS_TTL_MS) return Promise.resolve(status);
    if (statusPromise) return statusPromise;
    statusPromise = api("/api/work/status").then((s) => {
      status = s; statusAt = Date.now(); return s;
    }).finally(() => { statusPromise = null; });
    return statusPromise;
  }

  // ---- generic section shell: gate (not set up / status error) + body ---------------------
  function makeSection(def, buildBody) {
    const gate = el("div", { class: "wk-gate" });
    const body = el("div", { class: "wk-body" });
    const pane = el("section", { class: "wk-pane", "aria-label": def.label, hidden: true }, gate, body);
    const handle = buildBody(body);
    async function show(force = false) {
      let s;
      try {
        if (!status) { gate.hidden = false; fill(gate, ui.skeleton("list", 3)); }
        s = await loadStatus(force);
      } catch (error) {
        body.hidden = true;
        gate.hidden = false;
        fill(gate, errorBlock(message(error), () => show(true)));
        return;
      }
      const part = s[def.key] || { configured: false };
      if (!part.configured) {
        body.hidden = true;
        gate.hidden = false;
        fill(gate, ui.emptyState({
          icon: "settings", title: `${def.name} isn't set up yet`,
          body: part.reason || `${def.name} is not configured on this laptop.`,
          action: { label: "Check again", icon: "refresh", onClick: () => show(true) },
        }));
        return;
      }
      fill(gate, );
      gate.hidden = true;
      body.hidden = false;
      handle.refresh(part, force);
    }
    return { pane, show, handle };
  }

  // ============================== BRIEFING ==================================================
  function buildBriefing() {
    const card = el("section", { class: "ui-card ui-card--gradient wk-brief", "aria-label": "Today's briefing", hidden: true });
    let loaded = 0;
    let speaking = false;

    function spokenFallback(sections) {
      const parts = [];
      const c = sections.calendar;
      if (c && c.ok) parts.push(c.meetings.length ? `You have ${c.meetings.length} ${c.meetings.length === 1 ? "meeting" : "meetings"} today.` : "No meetings today.");
      const t = sections.tasks;
      if (t && t.ok) parts.push(`${t.counts?.overdue || 0} overdue and ${t.counts?.today || 0} due today.`);
      const m = sections.mail;
      if (m && m.ok) parts.push(`${m.unread} unread emails.`);
      return parts.join(" ");
    }

    function stat(icon, value, label, onClick) {
      return el("button", { type: "button", class: "wk-brief-stat", onclick: onClick },
        el("span", { class: "wk-brief-num ui-num" }, String(value)),
        el("span", { class: "wk-brief-label" }, ui.icon(icon), label));
    }

    function render(data) {
      const s = data.sections || {};
      const stats = [];
      if (s.calendar && s.calendar.ok) stats.push(stat("calendar", (s.calendar.meetings || []).length, "meetings", () => select("calendar")));
      if (s.tasks && s.tasks.ok) {
        const c = s.tasks.counts || {};
        stats.push(stat("list-checks", (c.overdue || 0) + (c.today || 0), (c.overdue ? `tasks, ${c.overdue} late` : "tasks due"), () => select("tasks")));
      }
      if (s.mail && s.mail.ok) stats.push(stat("mail", s.mail.unread ?? 0, "unread", () => ctx.open("inbox")));
      if (s.reminders && s.reminders.ok) stats.push(stat("alarm", (s.reminders.reminders || []).length, "reminders", () => ctx.open("reminders")));
      const failed = Object.entries(s).filter(([, v]) => v && v.ok === false);
      const text = (typeof data.text === "string" && data.text.trim()) ? data.text.trim() : "";
      const spoken = text || spokenFallback(s);
      const listen = spoken
        ? ui.button({ label: "Read it to me", icon: "volume", variant: "tonal", size: "sm", class: "wk-brief-listen",
            onClick: () => { ui.haptic("light"); ctx.speak(spoken); } })
        : null;
      fill(card, 
        el("div", { class: "wk-brief-top" },
          ui.tile("sparkles", "accent", "sm"),
          el("h2", { class: "wk-brief-title grow" }, "Your briefing"),
          listen),
        text ? el("p", { class: "wk-brief-text" }, text) : null,
        stats.length ? el("div", { class: "wk-brief-stats" }, ...stats) : null,
        failed.length ? el("p", { class: "wk-brief-note" }, `Couldn't load: ${failed.map(([k]) => k).join(", ")}.`) : null);
      card.hidden = !(text || stats.length);
    }

    async function load(force = false) {
      if (!force && loaded && Date.now() - loaded < 60000) return;
      loaded = Date.now();
      try {
        const data = await api("/api/inbox/briefing");
        render(data || {});
      } catch (_) {
        // 404 (no inbox module) or any failure: the briefing is optional, so stay hidden.
        if (!card.childElementCount) card.hidden = true;
      }
    }
    return { node: card, load };
  }

  // ============================== TASKS =====================================================
  function buildTasks(body) {
    const FILTERS = [["today", "Today"], ["overdue", "Overdue"], ["upcoming", "Upcoming"], ["all", "All"]];
    let filter = "today";
    let counts = null;
    let allCount = null;
    let token = 0;

    const chipBar = el("div", { class: "ui-hscroll wk-chips", role: "group", "aria-label": "Task filter" });
    const listBox = el("div", { class: "wk-list" });
    const listView = el("div", { class: "ui-stack" }, chipBar, listBox);
    const detailView = el("div", { class: "ui-stack", hidden: true });
    body.append(listView, detailView);

    function renderChips() {
      fill(chipBar, ...FILTERS.map(([id, label]) => {
        const n = id === "all" ? allCount : counts ? counts[id] : null;
        const chip = el("button", { type: "button", class: `ui-chip${id === "overdue" && n ? " wk-chip--bad" : ""}`, "aria-pressed": String(filter === id),
          onclick: () => { filter = id; renderChips(); loadList(); } },
          label, n != null ? el("span", { class: "wk-count ui-num" }, String(n)) : null);
        if (n != null) chip.setAttribute("aria-label", `${label}, ${n} tasks`);
        return chip;
      }));
    }

    async function quickComplete(task, check) {
      if (asking.has(task.gid) || check.classList.contains("is-busy")) return;
      asking.add(task.gid);
      const yes = await ui.confirm({ title: "Mark this task complete?", message: `"${task.name}" will be marked complete in Asana.`, confirmLabel: "Mark complete" }).finally(() => asking.delete(task.gid));
      if (!yes) return;
      check.classList.add("is-busy");
      try {
        await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}/complete`, { method: "POST" });
        check.classList.remove("is-busy"); check.classList.add("is-done");
        ui.toast("Task completed", { kind: "good" });
        setTimeout(loadList, 450);
      } catch (error) {
        check.classList.remove("is-busy");
        ui.toast(message(error), { kind: "bad" });
      }
    }

    function row(task) {
      const info = dueInfo(task.due_on);
      const check = el("button", { type: "button", class: `wk-check${task.completed ? " is-done" : ""}`,
        "aria-label": `Complete ${task.name}`, disabled: task.completed ? true : null,
        onclick: (e) => { e.stopPropagation(); quickComplete(task, check); } },
        ui.icon("check"));
      return el("li", { class: `wk-task${info.level === "bad" ? " is-overdue" : ""}` },
        check,
        el("button", { type: "button", class: "wk-task-main", onclick: () => openTask(task.gid) },
          el("span", { class: "wk-task-title" }, task.name),
          el("span", { class: "wk-task-sub" }, task.project || "No project")),
        dueBadge(task.due_on));
    }

    async function loadList() {
      const mine = ++token;
      fill(listBox, ui.skeleton("list", 4));
      try {
        const data = await api(`/api/work/asana/tasks?filter=${filter}`);
        if (mine !== token) return;
        counts = data.counts || counts;
        if (filter === "all") allCount = data.tasks.length;
        renderChips();
        if (!data.tasks.length) {
          fill(listBox, ui.emptyState({
            icon: "check-circle",
            title: filter === "all" ? "No open tasks" : `Nothing ${filter}`,
            body: filter === "overdue" ? "You're all caught up." : "Enjoy the clear space.",
            compact: true,
          }));
        } else {
          fill(listBox, el("ul", { class: "ui-list ui-list--inset wk-tasks" }, data.tasks.map(row)));
        }
      } catch (error) {
        if (mine !== token) return;
        fill(listBox, errorBlock(message(error), loadList));
      }
    }

    function backToList() {
      detailView.hidden = true;
      listView.hidden = false;
      fill(detailView, );
      loadList();
    }

    function backBar() {
      return el("div", { class: "wk-back" },
        ui.button({ label: "Tasks", icon: "chevron-left", variant: "ghost", size: "sm", onClick: backToList }));
    }

    async function openTask(gid) {
      listView.hidden = true;
      detailView.hidden = false;
      fill(detailView, backBar(), ui.skeleton("card", 2));
      container.scrollTop = 0;
      try {
        const { task } = await api(`/api/work/asana/tasks/${encodeURIComponent(gid)}`);
        renderDetail(task);
      } catch (error) {
        fill(detailView, backBar(), errorBlock(message(error), () => openTask(gid)));
      }
    }

    function renderDetail(task) {
      const comments = el("div", { class: "wk-comments" });
      const renderComments = (list) => {
        fill(comments, ...(list.length
          ? list.map((c) => el("div", { class: "wk-comment" },
              ui.avatar(c.author || "?", { size: "sm" }),
              el("div", { class: "wk-comment-body" },
                el("div", { class: "wk-comment-head" },
                  el("strong", {}, c.author || "Someone"),
                  el("span", { class: "wk-muted" }, whenText(c.created_at))),
                el("p", { class: "wk-text" }, c.text || ""))))
          : [el("p", { class: "wk-muted" }, "No comments yet.")]));
      };
      renderComments(task.comments || []);

      const subtasks = task.subtasks || [];
      const subList = subtasks.length
        ? el("ul", { class: "ui-list wk-subtasks" }, subtasks.map((s) =>
            el("li", { class: `wk-sub${s.completed ? " is-done" : ""}` },
              el("span", { class: `wk-check wk-check--static${s.completed ? " is-done" : ""}`, "aria-hidden": "true" }, ui.icon("check")),
              el("span", { class: "sr-only" }, s.completed ? "Done: " : "Not done: "),
              el("span", {}, s.name))))
        : el("p", { class: "wk-muted" }, "No subtasks.");

      const textarea = el("textarea", { rows: "3", placeholder: "Write a comment…", maxlength: "8000", "aria-label": "Comment" });
      const sendBtn = ui.button({ label: "Comment", icon: "send", variant: "primary", size: "sm", onClick: sendComment });
      const completeBtn = ui.button({ label: "Mark complete", icon: "check-circle", variant: "primary", block: true, onClick: completeTask });
      const msg = inlineMsg("bad");

      async function sendComment() {
        const text = textarea.value.trim();
        if (!text) { msg.show("Write something to comment."); textarea.focus(); return; }
        msg.clear();
        await busy(sendBtn, async () => {
          try {
            await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}/comment`, { method: "POST", json: { text } });
            textarea.value = "";
            ui.toast("Comment added", { kind: "good" });
            try {
              const fresh = await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}`);
              renderComments(fresh.task.comments || []);
            } catch (_) { /* the comment is saved; the list will refresh next time */ }
          } catch (error) {
            msg.show(message(error));
          }
        });
      }

      async function completeTask() {
        if (asking.has(task.gid)) return;
        asking.add(task.gid);
        const yes = await ui.confirm({ title: "Mark this task complete?", message: `"${task.name}" will be marked complete in Asana.`, confirmLabel: "Mark complete" }).finally(() => asking.delete(task.gid));
        if (!yes) return;
        await busy(completeBtn, async () => {
          try {
            await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}/complete`, { method: "POST" });
            ui.toast("Task completed", { kind: "good" });
            backToList();
          } catch (error) {
            msg.show(message(error));
          }
        });
      }

      fill(detailView, 
        backBar(),
        el("section", { class: "ui-card wk-stack" },
          el("h2", { class: `wk-task-h${task.completed ? " is-done" : ""}` }, task.name),
          el("div", { class: "wk-meta" },
            task.project ? el("span", { class: "ui-badge ui-badge--accent" }, task.project) : null,
            dueBadge(task.due_on),
            task.completed ? ui.badge("Completed", "good") : null),
          isHttp(task.permalink_url)
            ? el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm wk-open", href: task.permalink_url, target: "_blank", rel: "noopener noreferrer" }, ui.icon("external"), "Open in Asana")
            : null),
        el("section", { class: "ui-card wk-stack" }, el("h3", { class: "ui-card__title" }, "Notes"),
          task.notes ? el("p", { class: "wk-text" }, task.notes) : el("p", { class: "wk-muted" }, "No notes.")),
        el("section", { class: "ui-card wk-stack" }, el("h3", { class: "ui-card__title" }, "Subtasks"), subList),
        el("section", { class: "ui-card wk-stack" }, el("h3", { class: "ui-card__title" }, "Comments"), comments,
          textarea, el("div", { class: "wk-actions" }, sendBtn), msg),
        task.completed ? null : completeBtn,
      );
    }

    return {
      refresh() {
        renderChips();
        if (!detailView.hidden) return;   // keep the open task
        loadList();
      },
      openTask,
    };
  }

  // ============================== CALENDAR ==================================================
  function buildCalendar(body) {
    let day = isoDay(new Date());
    let token = 0;
    const label = el("div", { class: "wk-daylabel", "aria-live": "polite" });
    const prev = ui.button({ label: "Previous day", icon: "chevron-left", variant: "secondary", iconOnly: true, onClick: () => go(addDays(day, -1)) });
    const next = ui.button({ label: "Next day", icon: "chevron-right", variant: "secondary", iconOnly: true, onClick: () => go(addDays(day, 1)) });
    const today = ui.button({ label: "Today", variant: "tonal", size: "sm", onClick: () => go(isoDay(new Date())) });
    const list = el("div", { class: "wk-list" });
    body.append(el("div", { class: "ui-stack" },
      el("div", { class: "wk-daynav" }, prev, label, today, next),
      list));

    function go(value) { day = value; load(); }

    function eventRow(m, now) {
      const platform = PLATFORMS[m.platform];
      const start = new Date(m.start), end = new Date(m.end);
      const isToday = dayDiff(day) === 0;
      const live = isToday && !m.all_day && start <= now && now < end;
      const past = isToday && !live && (m.all_day ? false : end <= now);
      return el("li", { class: `wk-event${live ? " is-live" : ""}${past ? " is-past" : ""}` },
        el("div", { class: "wk-event-time ui-num" }, m.all_day ? "All day" : timeText(m.start),
          !m.all_day ? el("span", {}, timeText(m.end)) : null),
        el("span", { class: "wk-event-bar", "aria-hidden": "true" }),
        el("div", { class: "wk-event-body" },
          el("div", { class: "wk-event-title" }, m.title || "Untitled meeting"),
          el("div", { class: "wk-event-sub" }, live ? ui.badge("Now", "good") : null, platform ? ui.badge(platform) : null)),
        isHttp(m.link)
          ? el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm wk-join", href: m.link, target: "_blank", rel: "noopener noreferrer",
              "aria-label": `Join ${m.title || "meeting"}${platform ? " on " + platform : ""}` }, ui.icon("video"), "Join")
          : null);
    }

    async function load() {
      const mine = ++token;
      const d = parseDay(day);
      const rel = dayDiff(day) === 0 ? "Today" : dayDiff(day) === 1 ? "Tomorrow" : dayDiff(day) === -1 ? "Yesterday" : d.toLocaleDateString(undefined, { weekday: "long" });
      fill(label, el("strong", {}, rel), el("span", {}, d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })));
      fill(list, ui.skeleton("list", 3));
      try {
        const data = await api(`/api/work/calendar?day=${day}&days=1`);
        if (mine !== token) return;
        const meetings = (data.meetings || []).slice().sort((a, b) => (b.all_day ? 1 : 0) - (a.all_day ? 1 : 0) || new Date(a.start) - new Date(b.start));
        if (!meetings.length) {
          fill(list, ui.emptyState({ icon: "calendar", title: "No meetings", body: "Nothing on the calendar for this day.", compact: true }));
          return;
        }
        const now = new Date();
        const isToday = dayDiff(day) === 0;
        const items = [];
        let marked = !isToday;
        const marker = () => el("li", { class: "wk-now", "aria-label": `Now, ${ui.clockTime(new Date())}` },
          el("span", { class: "wk-now-time ui-num" }, ui.clockTime(now)), el("span", { class: "wk-now-line", "aria-hidden": "true" }));
        for (const m of meetings) {
          if (!marked && !m.all_day && new Date(m.start) > now) { items.push(marker()); marked = true; }
          items.push(eventRow(m, now));
        }
        if (!marked) items.push(marker());
        fill(list, el("ul", { class: "ui-card wk-timeline" }, items));
      } catch (error) {
        if (mine !== token) return;
        fill(list, errorBlock(message(error), load));
      }
    }

    return { refresh() { load(); } };
  }

  // ============================== EMAIL =====================================================
  function buildEmail(body) {
    let contactsLoaded = false;
    let contacts = [];
    let provider = normalizeProvider(store("hub.work.provider"));
    let reviewing = null;   // {draft_id, to, subject, body, typed}

    // (a) Write with AI: a glass card
    const instruction = el("textarea", { rows: "3", maxlength: "2000", placeholder: "e.g. write a mail to Rahul about the demo on Thursday",
      "aria-label": "Instruction for the AI" });
    const providerSeg = providerSegmented({ label: "AI provider",
      value: provider, onChange: (v) => { provider = v; store("hub.work.provider", v); } });
    providerSeg.classList.add("wk-provider");
    const aiMsg = inlineMsg("bad");
    const aiBtn = ui.button({ label: "Write with AI", icon: "wand", variant: "primary", onClick: aiDraft });
    const aiCard = el("section", { class: "ui-card ui-card--glass wk-stack", "aria-label": "Write with AI" },
      el("div", { class: "ui-card__head" }, ui.tile("sparkles", "accent", "sm"), el("h3", { class: "ui-card__title grow" }, "Write with AI")),
      instruction, el("div", { class: "wk-split" }, providerSeg, aiBtn), aiMsg);

    // (b) fields
    const to = el("input", { type: "text", maxlength: "200", list: "wk-contacts", placeholder: "Name or email address", autocomplete: "off",
      autocapitalize: "off", spellcheck: "false", "aria-describedby": "wk-to-error" });
    const datalist = el("datalist", { id: "wk-contacts" });
    const toError = el("p", { class: "ui-hint wk-to-error", id: "wk-to-error", role: "alert", hidden: true });
    const subject = el("input", { type: "text", maxlength: "300", placeholder: "Subject" });
    const text = el("textarea", { rows: "8", maxlength: "20000", placeholder: "Write your message…" });
    const reviewBtn = ui.button({ label: "Review draft", icon: "eye", variant: "primary", block: true, onClick: review });
    const formMsg = inlineMsg("bad");
    const form = el("section", { class: "ui-card wk-stack", "aria-label": "Email" },
      field("To", to, [datalist, toError]),
      field("Subject", subject),
      field("Message", text),
      formMsg,
      reviewBtn);

    const reviewBox = el("section", { class: "ui-card wk-stack", hidden: true, "aria-label": "Review draft" });
    const banner = el("div", { class: "wk-banner", hidden: true });
    const formView = el("div", { class: "ui-stack" }, banner, aiCard, form);
    body.append(formView, reviewBox);

    async function loadContacts() {
      if (contactsLoaded) return;
      try {
        const data = await api("/api/work/email/contacts");
        contacts = data.contacts || [];
        contactsLoaded = true;
        fill(datalist, ...contacts.map((c) => el("option", { value: c.name, label: c.address })));
      } catch (error) {
        formMsg.show(`Contacts could not be loaded (${message(error)}). You can still type an email address.`, "muted");
      }
    }

    // A reply drafted in Inbox waits in sessionStorage. Read it once, then clear it. Never sends.
    function consumeReply() {
      const d = takeReplyDraft();
      if (!d) return;
      if (reviewing) showForm();
      to.value = String(d.to || "");
      subject.value = String(d.subject || "");
      text.value = String(d.body || "");
      toError.hidden = true;
      fill(banner, ui.notice("Reply draft loaded from Inbox. Read it, edit it, then review. Nothing has been sent.", { tone: "good", icon: "mail" }));
      banner.hidden = false;
      text.focus({ preventScroll: true });
    }

    async function aiDraft() {
      const ask = instruction.value.trim();
      if (!ask) { aiMsg.show("Tell the AI what to write first."); instruction.focus(); return; }
      aiMsg.clear();
      await busy(aiBtn, async () => {
        try {
          const d = await api("/api/work/email/ai-draft", { method: "POST", json: { instruction: ask, provider } });
          if (d.to_hint) to.value = d.to_hint;
          subject.value = d.subject || "";
          text.value = d.body || "";
          toError.hidden = true;
          banner.hidden = true;
          ui.toast("Draft written. Check it, then review.", { kind: "good" });
        } catch (error) {
          aiMsg.show(message(error));
        }
      });
    }

    async function review() {
      toError.hidden = true; to.removeAttribute("aria-invalid");
      formMsg.clear();
      const typed = to.value.trim();
      if (!typed) { toError.textContent = "Who is this for? Enter a name or an email address."; toError.hidden = false; to.setAttribute("aria-invalid", "true"); to.focus(); return; }
      if (!subject.value.trim() && !text.value.trim()) { formMsg.show("Write a subject or a message first."); return; }
      await busy(reviewBtn, async () => {
        try {
          const d = await api("/api/work/email/draft", { method: "POST", json: { to: typed, subject: subject.value, body: text.value } });
          reviewing = { ...d, typed };
          renderReview();
        } catch (error) {
          if (error.status === 400) {
            toError.textContent = message(error); toError.hidden = false; to.setAttribute("aria-invalid", "true"); to.focus();
          } else {
            formMsg.show(message(error));
          }
        }
      });
    }

    function showForm(note) {
      reviewing = null;
      reviewBox.hidden = true;
      fill(reviewBox, );
      formView.hidden = false;
      if (note) formMsg.show(note);
    }

    function renderReview() {
      const d = reviewing;
      const editBtn = ui.button({ label: "Edit", icon: "pen", variant: "secondary", onClick: () => showForm() });
      const sendBtn = ui.button({ label: "Send", icon: "send", variant: "primary", onClick: send });
      const msg = inlineMsg("bad");
      async function send() {
        const draft = reviewing;
        if (!draft || asking.has("send")) return;
        const who = draft.typed && draft.typed.toLowerCase() !== draft.to.toLowerCase()
          ? `${draft.typed} <${draft.to}>` : draft.to;
        asking.add("send");
        const yes = await ui.confirm({ title: `Send to ${who}?`, message: `Subject: ${draft.subject || "(no subject)"}`, confirmLabel: "Send now" }).finally(() => asking.delete("send"));
        if (!yes || reviewing !== draft) return;
        await busy(sendBtn, async () => {
          reviewing = null;   // this draft can only ever be sent once from here
          try {
            await api("/api/work/email/send", { method: "POST", json: { draft_id: draft.draft_id } });
            ui.toast(`Email sent to ${draft.to}`, { kind: "good" });
            to.value = ""; subject.value = ""; text.value = ""; instruction.value = ""; banner.hidden = true;
            showForm();
          } catch (error) {
            const unsure = error.status === 0;
            const note = unsure
              ? "The laptop did not answer, so it is unknown whether the email went out. Check your Sent folder before trying again. The draft was discarded and must be reviewed again."
              : `${message(error)}${/not sent|n't sent/i.test(message(error)) ? "" : " The email was not sent."} The draft was discarded and must be reviewed again.`;
            showForm(note);
          }
        });
      }
      fill(reviewBox, 
        el("div", { class: "ui-card__head" }, ui.tile("eye", "warn", "sm"), el("h3", { class: "ui-card__title grow" }, "Review before sending")),
        el("p", { class: "wk-muted" }, "This is exactly what will be sent."),
        el("dl", { class: "wk-facts" },
          el("div", {}, el("dt", {}, "To"), el("dd", {}, d.to)),
          el("div", {}, el("dt", {}, "Subject"), el("dd", {}, d.subject || "(no subject)"))),
        el("div", { class: "wk-preview wk-text", tabindex: "0", "aria-label": "Message body" }, d.body || "(empty message)"),
        msg,
        el("div", { class: "wk-actions wk-actions--split" }, editBtn, sendBtn));
      formView.hidden = true;
      reviewBox.hidden = false;
      sendBtn.focus();
    }

    return {
      refresh() {
        loadContacts();
        consumeReply();
      },
    };
  }

  // ============================== CHAT ======================================================
  function buildChat(body) {
    let spaces = [];
    let space = null;
    let reviewing = null;   // {space, text}
    let loaded = false;

    const spaceBar = el("div", { class: "ui-hscroll wk-chips", role: "group", "aria-label": "Google Chat space" });
    const spaceBox = el("div", { class: "wk-stack" });
    const text = el("textarea", { rows: "5", maxlength: "4000", placeholder: "Write a message…", "aria-label": "Message" });
    const msg = inlineMsg("bad");
    const reviewBtn = ui.button({ label: "Review", icon: "eye", variant: "primary", block: true, onClick: review });
    const formView = el("section", { class: "ui-card wk-stack", "aria-label": "Google Chat message" },
      el("span", { class: "ui-label" }, "Space"), spaceBox,
      field("Message", text), msg, reviewBtn);
    const reviewBox = el("section", { class: "ui-card wk-stack", hidden: true, "aria-label": "Review message" });
    body.append(formView, reviewBox);

    function renderSpaces() {
      fill(spaceBar, ...spaces.map((name) =>
        el("button", { type: "button", class: "ui-chip", "aria-pressed": String(space === name),
          onclick: () => { space = name; renderSpaces(); } }, name)));
    }

    async function loadSpaces() {
      fill(spaceBox, ui.skeleton("lines", 1));
      reviewBtn.disabled = true;
      try {
        const data = await api("/api/work/chat/spaces");
        spaces = data.spaces || [];
        loaded = true;
        if (!spaces.length) {
          fill(spaceBox, emptyBlock("users", "No spaces", "No Google Chat spaces are set up."));
          return;
        }
        if (!spaces.includes(space)) space = spaces.length === 1 ? spaces[0] : null;
        renderSpaces();
        fill(spaceBox, spaceBar);
        reviewBtn.disabled = false;
      } catch (error) {
        fill(spaceBox, errorBlock(message(error), loadSpaces));
      }
    }

    function review() {
      msg.clear();
      if (!space) { msg.show("Pick a space first."); return; }
      const t = text.value.trim();
      if (!t) { msg.show("Write a message first."); text.focus(); return; }
      reviewing = { space, text: t };
      const editBtn = ui.button({ label: "Edit", icon: "pen", variant: "secondary", onClick: backToForm });
      const postBtn = ui.button({ label: "Post", icon: "send", variant: "primary", onClick: post });
      const err = inlineMsg("bad");
      async function post() {
        const item = reviewing;
        if (!item || asking.has("post")) return;
        asking.add("post");
        const yes = await ui.confirm({ title: `Post to ${item.space}?`, message: "Everyone in the space will see this message.", confirmLabel: "Post now" }).finally(() => asking.delete("post"));
        if (!yes || reviewing !== item) return;
        await busy(postBtn, async () => {
          try {
            await api("/api/work/chat/send", { method: "POST", json: { space: item.space, text: item.text } });
            ui.toast(`Posted to ${item.space}`, { kind: "good" });
            text.value = "";
            backToForm();
          } catch (error) {
            err.show(`${message(error)} Nothing was posted.`);
          }
        });
      }
      fill(reviewBox, 
        el("div", { class: "ui-card__head" }, ui.tile("eye", "warn", "sm"), el("h3", { class: "ui-card__title grow" }, "Review before posting")),
        el("p", { class: "wk-muted" }, "This is exactly what will be posted."),
        el("dl", { class: "wk-facts" }, el("div", {}, el("dt", {}, "Space"), el("dd", {}, reviewing.space))),
        el("div", { class: "wk-preview wk-text", tabindex: "0", "aria-label": "Message" }, reviewing.text),
        err,
        el("div", { class: "wk-actions wk-actions--split" }, editBtn, postBtn));
      formView.hidden = true;
      reviewBox.hidden = false;
      postBtn.focus();
    }

    function backToForm() {
      reviewing = null;
      reviewBox.hidden = true;
      fill(reviewBox, );
      formView.hidden = false;
    }

    return {
      refresh() { if (!loaded || !spaces.length) loadSpaces(); },
    };
  }

  // ---- shell -----------------------------------------------------------------------------
  const builders = { tasks: buildTasks, calendar: buildCalendar, email: buildEmail, chat: buildChat };
  const sections = {};
  for (const def of SECTIONS) sections[def.id] = makeSection(def, builders[def.id]);
  const briefing = buildBriefing();

  const seg = ui.segmented({
    label: "Work sections", block: true,
    options: [{ value: "tasks", label: "Tasks" }, { value: "calendar", label: "Calendar" }, { value: "email", label: "Email" }, { value: "chat", label: "Chat" }],
    value: "tasks", onChange: (id) => select(id),
  });
  const header = el("div", { class: "wk-head" }, seg);
  container.classList.add("work-view");
  container.append(el("div", { class: "wk" }, briefing.node, header, ...SECTIONS.map((s) => sections[s.id].pane)));
  ui.pullToRefresh(container, async () => {
    await Promise.allSettled([briefing.load(true), sections[current].show(true)]);
  });

  function select(id, force = false) {
    if (!sections[id]) return;
    current = id;
    seg.value = id;
    for (const def of SECTIONS) sections[def.id].pane.hidden = def.id !== id;
    store("hub.work", id);
    sections[id].show(force);
  }

  const saved = store("hub.work");
  // Draw the first section without hitting the network until the tab is shown.
  current = SECTIONS.some((s) => s.id === saved) ? saved : "tasks";
  seg.value = current;
  for (const def of SECTIONS) sections[def.id].pane.hidden = def.id !== current;

  return {
    show() { briefing.load(); sections[current].show(Date.now() - statusAt > STATUS_TTL_MS); },
    hide() {},
    openSection(name) { if (name) select(name); },
    // A search result or Home row opens an Asana task by gid.
    openItem(gid, section) {
      if (!gid || (section && section !== "tasks")) return;
      select("tasks");
      sections.tasks.handle.openTask(String(gid));
    },
    refresh() { briefing.load(true); sections[current].show(true); },
  };
}
