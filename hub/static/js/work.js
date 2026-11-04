// Work screen: Asana tasks, calendar, email and Google Chat in one tab.
// Everything from the API is rendered with el()/textContent, never innerHTML.
import { api, el } from "./api.js";

const SECTIONS = [
  { id: "tasks", label: "Tasks", key: "asana", name: "Asana" },
  { id: "calendar", label: "Calendar", key: "calendar", name: "Calendar" },
  { id: "email", label: "Email", key: "email", name: "Email" },
  { id: "chat", label: "Chat", key: "chat", name: "Google Chat" },
];
const STATUS_TTL_MS = 30000;

// ---- small helpers -------------------------------------------------------------------------

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { /* private mode */ }
  return null;
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

// -> {text, level: bad|warn|soon|none}
function dueInfo(due) {
  if (!due) return { text: "No date", level: "none" };
  const diff = dayDiff(due);
  if (diff < 0) return { text: `Overdue ${-diff} d`, level: "bad" };
  if (diff === 0) return { text: "Today", level: "warn" };
  if (diff === 1) return { text: "Tomorrow", level: "soon" };
  const date = parseDay(due).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return { text: date, level: "soon" };
}

function whenText(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function timeText(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const isHttp = (url) => typeof url === "string" && /^https?:\/\//i.test(url);
const PLATFORMS = { zoom: "Zoom", meet: "Meet", teams: "Teams" };

function message(error) {
  return error && error.message ? error.message : "Something went wrong.";
}

// Disable a button (or several) while an async job runs.
async function busy(buttons, job) {
  const list = [buttons].flat();
  for (const b of list) b.disabled = true;
  try { return await job(); } finally { for (const b of list) b.disabled = false; }
}

function loadingBlock(text = "Loading…") {
  return el("p", { class: "muted w-loading", role: "status" }, text);
}

function errorBlock(text, onRetry) {
  return el("div", { class: "w-error", role: "alert" },
    el("strong", {}, "That didn't work"),
    el("p", {}, text),
    onRetry ? el("button", { type: "button", class: "btn primary", onclick: onRetry }, "Retry") : null,
  );
}

function emptyBlock(text) { return el("p", { class: "muted w-empty" }, text); }

function field(label, control, extra) {
  return el("label", { class: "w-field" }, el("span", { class: "w-label" }, label), control, extra);
}

// ---- module --------------------------------------------------------------------------------

export function init(container, ctx) {
  let current = null;
  let visible = false;
  let status = null;
  let statusAt = 0;
  let statusPromise = null;

  // In-page confirmation. Resolves true only when the owner taps the confirm button.
  function confirmPanel({ title, lines = [], detail, confirmLabel, danger = false }) {
    return new Promise((resolve) => {
      const previous = document.activeElement;
      const cancel = el("button", { type: "button", class: "btn" }, "Cancel");
      const ok = el("button", { type: "button", class: `btn ${danger ? "danger" : "primary"}` }, confirmLabel);
      const titleId = `w-confirm-${Date.now()}`;
      const sheet = el("div", { class: "sheet w-confirm", role: "alertdialog", "aria-modal": "true", "aria-labelledby": titleId },
        el("h3", { id: titleId }, title),
        ...lines.map((line) => el("p", { class: "w-confirm-line" }, line)),
        detail ? el("pre", { class: "w-confirm-detail" }, detail) : null,
        el("div", { class: "row w-actions" }, cancel, ok),
      );
      const scrim = el("div", { class: "w-scrim" });
      function done(value) {
        document.removeEventListener("keydown", onKey, true);
        sheet.remove();
        scrim.remove();
        if (previous && previous.isConnected) previous.focus();
        resolve(value);
      }
      function onKey(event) {
        if (event.key === "Escape") { event.preventDefault(); done(false); return; }
        if (event.key === "Tab") {
          const items = [cancel, ok].filter((b) => !b.disabled);
          const index = items.indexOf(document.activeElement);
          event.preventDefault();
          const next = event.shiftKey ? (index <= 0 ? items.length - 1 : index - 1) : (index + 1) % items.length;
          items[next].focus();
        }
      }
      cancel.addEventListener("click", () => done(false));
      ok.addEventListener("click", () => done(true));
      scrim.addEventListener("click", () => done(false));
      document.addEventListener("keydown", onKey, true);
      container.append(scrim, sheet);
      cancel.focus();
    });
  }

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
    const gate = el("div", { class: "w-gate" });
    const body = el("div", { class: "w-body" });
    const pane = el("div", { class: "w-pane", role: "tabpanel", "aria-labelledby": `w-tab-${def.id}`, hidden: true }, gate, body);
    const api_ = buildBody(body);
    async function show(force = false) {
      let s;
      try {
        if (!status) gate.replaceChildren(loadingBlock("Checking what is set up…"));
        s = await loadStatus(force);
      } catch (error) {
        body.hidden = true;
        gate.hidden = false;
        gate.replaceChildren(errorBlock(message(error), () => show(true)));
        return;
      }
      const part = s[def.key] || { configured: false };
      if (!part.configured) {
        body.hidden = true;
        gate.hidden = false;
        gate.replaceChildren(el("section", { class: "card w-info", role: "status", "aria-label": `${def.name} is not set up` },
          el("h3", {}, `${def.name} isn't set up yet`),
          el("p", {}, part.reason || `${def.name} is not configured on this laptop.`),
          el("button", { type: "button", class: "btn", onclick: () => show(true) }, "Check again"),
        ));
        return;
      }
      gate.replaceChildren();
      gate.hidden = true;
      body.hidden = false;
      api_.refresh(part, force);
    }
    return { pane, show };
  }

  // ============================== TASKS =====================================================
  function buildTasks(body) {
    const FILTERS = [["today", "Today"], ["overdue", "Overdue"], ["upcoming", "Upcoming"], ["all", "All"]];
    let filter = "today";
    let counts = null;
    let allCount = null;
    let token = 0;

    const chipBar = el("div", { class: "w-chips", role: "group", "aria-label": "Task filter" });
    const refreshBtn = el("button", { type: "button", class: "btn", "aria-label": "Refresh tasks", onclick: () => loadList() }, "Refresh");
    const listBox = el("div", { class: "w-list" });
    const listView = el("div", { class: "w-stack" },
      el("div", { class: "row w-bar" }, chipBar, refreshBtn),
      listBox,
    );
    const detailView = el("div", { class: "w-stack", hidden: true });
    body.append(listView, detailView);

    function renderChips() {
      chipBar.replaceChildren(...FILTERS.map(([id, label]) => {
        const n = id === "all" ? allCount : counts ? counts[id] : null;
        const chip = el("button", { type: "button", class: "chip w-chip", "aria-pressed": String(filter === id),
          onclick: () => { filter = id; renderChips(); loadList(); } },
          label, n != null ? el("span", { class: "w-count" }, String(n)) : null);
        if (n != null) chip.setAttribute("aria-label", `${label}, ${n} tasks`);
        return chip;
      }));
    }

    function row(task) {
      const info = dueInfo(task.due_on);
      return el("li", {},
        el("button", { type: "button", class: `w-row${task.completed ? " done" : ""}`, onclick: () => openTask(task.gid) },
          el("span", { class: "w-row-main" },
            el("span", { class: "w-row-title" }, task.name),
            el("span", { class: "w-row-sub muted" }, task.project || "No project")),
          el("span", { class: `w-pill ${info.level}` }, info.text),
        ));
    }

    async function loadList() {
      const mine = ++token;
      refreshBtn.disabled = true;
      listBox.replaceChildren(loadingBlock("Loading tasks…"));
      try {
        const data = await api(`/api/work/asana/tasks?filter=${filter}`);
        if (mine !== token) return;
        counts = data.counts || counts;
        if (filter === "all") allCount = data.tasks.length;
        renderChips();
        if (!data.tasks.length) {
          listBox.replaceChildren(emptyBlock(filter === "all" ? "No open tasks. Nice." : `No ${filter} tasks.`));
        } else {
          listBox.replaceChildren(el("ul", { class: "w-ul" }, data.tasks.map(row)));
        }
      } catch (error) {
        if (mine !== token) return;
        listBox.replaceChildren(errorBlock(message(error), loadList));
      } finally {
        if (mine === token) refreshBtn.disabled = false;
      }
    }

    function backToList() {
      detailView.hidden = true;
      listView.hidden = false;
      detailView.replaceChildren();
      loadList();
    }

    async function openTask(gid) {
      listView.hidden = true;
      detailView.hidden = false;
      detailView.replaceChildren(backBar(), loadingBlock("Loading task…"));
      detailView.scrollIntoView?.({ block: "start" });
      try {
        const { task } = await api(`/api/work/asana/tasks/${encodeURIComponent(gid)}`);
        renderDetail(task);
      } catch (error) {
        detailView.replaceChildren(backBar(), errorBlock(message(error), () => openTask(gid)));
      }
    }

    function backBar() {
      return el("div", { class: "row w-bar" },
        el("button", { type: "button", class: "btn", onclick: backToList }, "← Back"));
    }

    function renderDetail(task) {
      const info = dueInfo(task.due_on);
      const comments = el("div", { class: "w-comments" });
      const renderComments = (list) => {
        comments.replaceChildren(...(list.length
          ? list.map((c) => el("div", { class: "w-comment" },
              el("div", { class: "w-comment-head" },
                el("strong", {}, c.author || "Someone"),
                el("span", { class: "muted" }, whenText(c.created_at))),
              el("p", { class: "w-text" }, c.text || "")))
          : [emptyBlock("No comments yet.")]));
      };
      renderComments(task.comments || []);

      const subtasks = task.subtasks || [];
      const subList = subtasks.length
        ? el("ul", { class: "w-subtasks" }, subtasks.map((s) =>
            el("li", { class: s.completed ? "done" : "" },
              el("span", { class: "w-box", "aria-hidden": "true" }, s.completed ? "✓" : ""),
              el("span", { class: "sr-only" }, s.completed ? "Done: " : "Not done: "),
              el("span", {}, s.name))))
        : emptyBlock("No subtasks.");

      const textarea = el("textarea", { rows: "3", placeholder: "Write a comment…", "aria-label": "Comment" });
      const sendBtn = el("button", { type: "button", class: "btn primary", onclick: sendComment }, "Send");
      const completeBtn = el("button", { type: "button", class: "btn", onclick: completeTask }, "Mark complete");
      const msg = el("p", { class: "error w-msg", role: "alert", hidden: true });

      async function sendComment() {
        const text = textarea.value.trim();
        if (!text) { msg.textContent = "Write something to comment."; msg.hidden = false; textarea.focus(); return; }
        msg.hidden = true;
        await busy(sendBtn, async () => {
          try {
            await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}/comment`, { method: "POST", json: { text } });
            textarea.value = "";
            ctx.toast("Comment added");
            try {
              const fresh = await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}`);
              renderComments(fresh.task.comments || []);
            } catch (_) { /* the comment is saved; the list will refresh next time */ }
          } catch (error) {
            msg.textContent = message(error); msg.hidden = false;
          }
        });
      }

      async function completeTask() {
        const yes = await confirmPanel({
          title: "Mark this task complete?",
          lines: [`“${task.name}” will be marked complete in Asana.`],
          confirmLabel: "Mark complete",
        });
        if (!yes) return;
        await busy(completeBtn, async () => {
          try {
            await api(`/api/work/asana/tasks/${encodeURIComponent(task.gid)}/complete`, { method: "POST" });
            ctx.toast("Task completed");
            backToList();
          } catch (error) {
            msg.textContent = message(error); msg.hidden = false;
          }
        });
      }

      detailView.replaceChildren(
        backBar(),
        el("section", { class: "card w-stack" },
          el("h2", { class: `w-task-title${task.completed ? " done" : ""}` }, task.name),
          el("div", { class: "row wrap" },
            task.project ? el("span", { class: "chip w-static" }, task.project) : null,
            el("span", { class: `w-pill ${info.level}` }, info.text),
            task.completed ? el("span", { class: "w-pill none" }, "Completed") : null),
          isHttp(task.permalink_url)
            ? el("a", { class: "btn w-link", href: task.permalink_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Asana")
            : null,
        ),
        el("section", { class: "card w-stack" }, el("h3", {}, "Notes"),
          task.notes ? el("p", { class: "w-text" }, task.notes) : emptyBlock("No notes.")),
        el("section", { class: "card w-stack" }, el("h3", {}, "Subtasks"), subList),
        el("section", { class: "card w-stack" }, el("h3", {}, "Comments"), comments,
          el("div", { class: "w-stack" }, textarea, el("div", { class: "row w-actions" }, sendBtn)), msg),
        task.completed ? null : el("div", { class: "row w-actions" }, completeBtn),
      );
    }

    return {
      refresh() {
        renderChips();
        if (!detailView.hidden) return;   // keep the open task
        loadList();
      },
    };
  }

  // ============================== CALENDAR ==================================================
  function buildCalendar(body) {
    let day = isoDay(new Date());
    let token = 0;
    const label = el("div", { class: "w-daylabel", "aria-live": "polite" });
    const prev = el("button", { type: "button", class: "btn w-icon", "aria-label": "Previous day", onclick: () => go(addDays(day, -1)) }, "‹");
    const next = el("button", { type: "button", class: "btn w-icon", "aria-label": "Next day", onclick: () => go(addDays(day, 1)) }, "›");
    const today = el("button", { type: "button", class: "btn", onclick: () => go(isoDay(new Date())) }, "Today");
    const list = el("div", { class: "w-list" });
    body.append(el("div", { class: "w-stack" },
      el("div", { class: "row w-daynav" }, prev, today, next),
      label, list));

    function go(value) { day = value; load(); }

    async function load() {
      const mine = ++token;
      const d = parseDay(day);
      const rel = dayDiff(day) === 0 ? "Today · " : dayDiff(day) === 1 ? "Tomorrow · " : dayDiff(day) === -1 ? "Yesterday · " : "";
      label.textContent = rel + d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
      list.replaceChildren(loadingBlock("Loading meetings…"));
      try {
        const data = await api(`/api/work/calendar?day=${day}&days=1`);
        if (mine !== token) return;
        const meetings = data.meetings || [];
        if (!meetings.length) { list.replaceChildren(emptyBlock("No meetings")); return; }
        list.replaceChildren(el("ul", { class: "w-ul" }, meetings.map((m) => {
          const platform = PLATFORMS[m.platform];
          return el("li", { class: "card w-meeting" },
            el("div", { class: "w-meeting-main" },
              el("div", { class: "w-time" }, m.all_day ? "All day" : `${timeText(m.start)} – ${timeText(m.end)}`),
              el("div", { class: "w-row-title" }, m.title || "Untitled meeting"),
              platform ? el("span", { class: "chip w-static w-platform" }, platform) : null),
            isHttp(m.link)
              ? el("a", { class: "btn primary", href: m.link, target: "_blank", rel: "noopener noreferrer",
                  "aria-label": `Join ${m.title || "meeting"}${platform ? " on " + platform : ""}` }, "Join")
              : null);
        })));
      } catch (error) {
        if (mine !== token) return;
        list.replaceChildren(errorBlock(message(error), load));
      }
    }

    return { refresh() { load(); } };
  }

  // ============================== EMAIL =====================================================
  function buildEmail(body) {
    let contactsLoaded = false;
    let contacts = [];
    let provider = store("hub.work.provider") === "claude" ? "claude" : "local";
    let reviewing = null;   // {draft_id, to, subject, body, typed}

    // (a) Write with AI
    const instruction = el("textarea", { rows: "3", placeholder: "e.g. write a mail to Rahul about the demo on Thursday",
      "aria-label": "Instruction for the AI" });
    const providerBar = el("div", { class: "w-chips", role: "group", "aria-label": "AI provider" });
    function renderProvider() {
      providerBar.replaceChildren(...[["local", "Local"], ["claude", "Claude"]].map(([id, text]) =>
        el("button", { type: "button", class: "chip w-chip", "aria-pressed": String(provider === id),
          onclick: () => { provider = id; store("hub.work.provider", id); renderProvider(); } }, text)));
    }
    renderProvider();
    const aiMsg = el("p", { class: "error w-msg", role: "alert", hidden: true });
    const aiBtn = el("button", { type: "button", class: "btn primary", onclick: aiDraft }, "Write with AI");
    const aiCard = el("section", { class: "card w-stack", "aria-label": "Write with AI" },
      el("h3", {}, "Write with AI"), instruction,
      el("div", { class: "row wrap w-spread" }, providerBar, aiBtn), aiMsg);

    // (b) fields
    const to = el("input", { type: "text", list: "w-contacts", placeholder: "Name or email address", autocomplete: "off",
      autocapitalize: "off", spellcheck: "false", "aria-describedby": "w-to-error" });
    const datalist = el("datalist", { id: "w-contacts" });
    const toError = el("p", { class: "error w-msg", id: "w-to-error", role: "alert", hidden: true });
    const subject = el("input", { type: "text", placeholder: "Subject" });
    const text = el("textarea", { rows: "8", placeholder: "Write your message…" });
    const reviewBtn = el("button", { type: "button", class: "btn primary", onclick: review }, "Review draft");
    const formMsg = el("p", { class: "w-msg", role: "status", hidden: true });
    const form = el("section", { class: "card w-stack", "aria-label": "Email" },
      el("h3", {}, "Email"),
      field("To", to, [datalist, toError]),
      field("Subject", subject),
      field("Body", text),
      formMsg,
      el("div", { class: "row w-actions" }, reviewBtn));

    // review panel
    const reviewBox = el("section", { class: "card w-stack", hidden: true, "aria-label": "Review draft" });

    const formView = el("div", { class: "w-stack" }, aiCard, form);
    body.append(formView, reviewBox);

    async function loadContacts() {
      if (contactsLoaded) return;
      try {
        const data = await api("/api/work/email/contacts");
        contacts = data.contacts || [];
        contactsLoaded = true;
        datalist.replaceChildren(...contacts.map((c) => el("option", { value: c.name, label: c.address })));
      } catch (error) {
        // not fatal: the owner can still type an address
        formMsg.className = "muted w-msg";
        formMsg.textContent = `Contacts could not be loaded (${message(error)}). You can still type an email address.`;
        formMsg.hidden = false;
      }
    }

    async function aiDraft() {
      const ask = instruction.value.trim();
      if (!ask) { aiMsg.textContent = "Tell the AI what to write first."; aiMsg.hidden = false; instruction.focus(); return; }
      aiMsg.hidden = true;
      aiBtn.textContent = "Writing…";
      await busy(aiBtn, async () => {
        try {
          const d = await api("/api/work/email/ai-draft", { method: "POST", json: { instruction: ask, provider } });
          if (d.to_hint) to.value = d.to_hint;
          subject.value = d.subject || "";
          text.value = d.body || "";
          toError.hidden = true;
          ctx.toast("Draft written. Check it, then review.");
        } catch (error) {
          aiMsg.textContent = message(error); aiMsg.hidden = false;
        }
      });
      aiBtn.textContent = "Write with AI";
    }

    async function review() {
      toError.hidden = true; to.removeAttribute("aria-invalid");
      formMsg.hidden = true;
      const typed = to.value.trim();
      if (!typed) { toError.textContent = "Who is this for? Enter a name or an email address."; toError.hidden = false; to.focus(); return; }
      if (!subject.value.trim() && !text.value.trim()) { formMsg.className = "error w-msg"; formMsg.textContent = "Write a subject or a message first."; formMsg.hidden = false; return; }
      await busy(reviewBtn, async () => {
        try {
          const d = await api("/api/work/email/draft", { method: "POST", json: { to: typed, subject: subject.value, body: text.value } });
          reviewing = { ...d, typed };
          renderReview();
        } catch (error) {
          if (error.status === 400) {
            toError.textContent = message(error); toError.hidden = false; to.setAttribute("aria-invalid", "true"); to.focus();
          } else {
            formMsg.className = "error w-msg"; formMsg.textContent = message(error); formMsg.hidden = false;
          }
        }
      });
    }

    function showForm(note) {
      reviewing = null;
      reviewBox.hidden = true;
      reviewBox.replaceChildren();
      formView.hidden = false;
      if (note) { formMsg.className = "error w-msg"; formMsg.textContent = note; formMsg.hidden = false; }
    }

    function renderReview() {
      const d = reviewing;
      const editBtn = el("button", { type: "button", class: "btn", onclick: () => showForm() }, "Edit");
      const sendBtn = el("button", { type: "button", class: "btn primary", onclick: send }, "Send");
      const msg = el("p", { class: "error w-msg", role: "alert", hidden: true });
      async function send() {
        const draft = reviewing;
        if (!draft) return;
        const who = draft.typed && draft.typed.toLowerCase() !== draft.to.toLowerCase()
          ? `${draft.typed} <${draft.to}>` : draft.to;
        const yes = await confirmPanel({ title: `Send to ${who}?`,
          lines: [`Subject: ${draft.subject || "(no subject)"}`], confirmLabel: "Send now" });
        if (!yes) return;
        await busy([sendBtn, editBtn], async () => {
          reviewing = null;   // this draft can only ever be sent once from here
          try {
            await api("/api/work/email/send", { method: "POST", json: { draft_id: draft.draft_id } });
            ctx.toast(`Email sent to ${draft.to}`);
            to.value = ""; subject.value = ""; text.value = ""; instruction.value = "";
            showForm();
          } catch (error) {
            const unsure = error.status === 0;
            const note = unsure
              ? "The laptop did not answer, so it is unknown whether the email went out. Check your Sent folder before trying again. The draft was discarded and must be reviewed again."
              : `${message(error)} The email was not sent. The draft was discarded and must be reviewed again.`;
            showForm(note);
          }
        });
      }
      reviewBox.replaceChildren(
        el("h3", {}, "Review before sending"),
        el("p", { class: "muted" }, "This is exactly what will be sent."),
        el("dl", { class: "w-facts" },
          el("div", {}, el("dt", {}, "To"), el("dd", { class: "w-to" }, d.to)),
          el("div", {}, el("dt", {}, "Subject"), el("dd", {}, d.subject || "(no subject)"))),
        el("div", { class: "w-text w-preview", tabindex: "0", "aria-label": "Message body" }, d.body || "(empty message)"),
        msg,
        el("div", { class: "row w-actions" }, editBtn, sendBtn));
      formView.hidden = true;
      reviewBox.hidden = false;
      sendBtn.focus();
    }

    return {
      refresh() {
        loadContacts();
      },
    };
  }

  // ============================== CHAT ======================================================
  function buildChat(body) {
    let spaces = [];
    let space = null;
    let reviewing = null;   // {space, text}
    let loaded = false;

    const spaceBar = el("div", { class: "w-chips", role: "group", "aria-label": "Google Chat space" });
    const spaceBox = el("div", { class: "w-stack" });
    const text = el("textarea", { rows: "5", placeholder: "Write a message…", "aria-label": "Message" });
    const msg = el("p", { class: "error w-msg", role: "alert", hidden: true });
    const reviewBtn = el("button", { type: "button", class: "btn primary", onclick: review }, "Review");
    const formView = el("section", { class: "card w-stack", "aria-label": "Google Chat message" },
      el("h3", {}, "Google Chat"),
      el("span", { class: "w-label" }, "Space"), spaceBox,
      field("Message", text), msg,
      el("div", { class: "row w-actions" }, reviewBtn));
    const reviewBox = el("section", { class: "card w-stack", hidden: true, "aria-label": "Review message" });
    body.append(formView, reviewBox);

    function renderSpaces() {
      spaceBar.replaceChildren(...spaces.map((name) =>
        el("button", { type: "button", class: "chip w-chip", "aria-pressed": String(space === name),
          onclick: () => { space = name; renderSpaces(); } }, name)));
    }

    async function loadSpaces() {
      spaceBox.replaceChildren(loadingBlock("Loading spaces…"));
      reviewBtn.disabled = true;
      try {
        const data = await api("/api/work/chat/spaces");
        spaces = data.spaces || [];
        loaded = true;
        if (!spaces.length) {
          spaceBox.replaceChildren(emptyBlock("No Google Chat spaces are set up."));
          return;
        }
        if (!spaces.includes(space)) space = spaces.length === 1 ? spaces[0] : null;
        renderSpaces();
        spaceBox.replaceChildren(spaceBar);
        reviewBtn.disabled = false;
      } catch (error) {
        spaceBox.replaceChildren(errorBlock(message(error), loadSpaces));
      }
    }

    function review() {
      msg.hidden = true;
      if (!space) { msg.textContent = "Pick a space first."; msg.hidden = false; return; }
      const t = text.value.trim();
      if (!t) { msg.textContent = "Write a message first."; msg.hidden = false; text.focus(); return; }
      reviewing = { space, text: t };
      const editBtn = el("button", { type: "button", class: "btn", onclick: backToForm }, "Edit");
      const postBtn = el("button", { type: "button", class: "btn primary", onclick: post }, "Post");
      const err = el("p", { class: "error w-msg", role: "alert", hidden: true });
      async function post() {
        const item = reviewing;
        const yes = await confirmPanel({ title: `Post to ${item.space}?`, lines: ["Everyone in the space will see this message."],
          detail: item.text, confirmLabel: "Post now" });
        if (!yes) return;
        await busy([postBtn, editBtn], async () => {
          try {
            await api("/api/work/chat/send", { method: "POST", json: { space: item.space, text: item.text } });
            ctx.toast(`Posted to ${item.space}`);
            text.value = "";
            backToForm();
          } catch (error) {
            err.textContent = `${message(error)} Nothing was posted.`; err.hidden = false;
          }
        });
      }
      reviewBox.replaceChildren(
        el("h3", {}, "Review before posting"),
        el("p", { class: "muted" }, "This is exactly what will be posted."),
        el("dl", { class: "w-facts" }, el("div", {}, el("dt", {}, "Space"), el("dd", {}, reviewing.space))),
        el("div", { class: "w-text w-preview", tabindex: "0", "aria-label": "Message" }, reviewing.text),
        err,
        el("div", { class: "row w-actions" }, editBtn, postBtn));
      formView.hidden = true;
      reviewBox.hidden = false;
      postBtn.focus();
    }

    function backToForm() {
      reviewing = null;
      reviewBox.hidden = true;
      reviewBox.replaceChildren();
      formView.hidden = false;
    }

    return {
      refresh() { if (!loaded || !spaces.length) loadSpaces(); },
    };
  }

  // ---- shell -----------------------------------------------------------------------------
  const builders = { tasks: buildTasks, calendar: buildCalendar, email: buildEmail, chat: buildChat };
  const sections = {};
  const bar = el("div", { class: "w-seg", role: "tablist", "aria-label": "Work sections" });
  for (const def of SECTIONS) {
    bar.append(el("button", { type: "button", role: "tab", id: `w-tab-${def.id}`, "data-section": def.id,
      onclick: () => select(def.id) }, def.label));
    sections[def.id] = makeSection(def, builders[def.id]);
  }
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const ids = SECTIONS.map((s) => s.id);
    const i = ids.indexOf(current);
    const nextId = ids[(i + (event.key === "ArrowRight" ? 1 : ids.length - 1)) % ids.length];
    select(nextId);
    bar.querySelector(`[data-section="${nextId}"]`).focus();
  });
  container.classList.add("work-view");
  container.append(el("div", { class: "w-wrap" }, bar, ...SECTIONS.map((s) => sections[s.id].pane)));

  function select(id, force = false) {
    if (!sections[id]) return;
    current = id;
    for (const def of SECTIONS) {
      sections[def.id].pane.hidden = def.id !== id;
      const tab = bar.querySelector(`[data-section="${def.id}"]`);
      tab.setAttribute("aria-selected", String(def.id === id));
      tab.tabIndex = def.id === id ? 0 : -1;
    }
    store("hub.work", id);
    sections[id].show(force);
  }

  const saved = store("hub.work");
  // Draw the first section without hitting the network until the tab is shown.
  current = SECTIONS.some((s) => s.id === saved) ? saved : "tasks";
  for (const def of SECTIONS) {
    sections[def.id].pane.hidden = def.id !== current;
    const tab = bar.querySelector(`[data-section="${def.id}"]`);
    tab.setAttribute("aria-selected", String(def.id === current));
    tab.tabIndex = def.id === current ? 0 : -1;
  }

  return {
    show() { visible = true; sections[current].show(Date.now() - statusAt > STATUS_TTL_MS); },
    hide() { visible = false; },
    openSection(name) { select(name); },
    get visible() { return visible; },
  };
}
