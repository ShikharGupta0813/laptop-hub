// To-do: smart lists + custom lists, quick-add with a parsed echo, detail sheet with subtasks, repeat and AI breakdown.
// API: docs/api/productivity.md (Todos). openSection("new") focuses quick add; openItem(id) opens the detail sheet.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

// A sheet button that is still working ignores further taps (a double-tap must not send two requests).
const guard = (actions) => actions.map((a) => {
  if (!a || !a.onClick) return a;
  let working = false;
  return { ...a, onClick: async (x) => { if (working) return false; working = true; try { return await a.onClick(x); } finally { working = false; } } };
});


const SMART = [
  { key: "today", label: "Today", icon: "sun", count: "today", empty: ["sun", "Nothing due today", "Enjoy it, or add something above."] },
  { key: "overdue", label: "Overdue", icon: "alert-circle", count: "overdue", tone: "bad", empty: ["check-circle", "Nothing overdue", "You're on top of things."] },
  { key: "upcoming", label: "Upcoming", icon: "calendar", count: "upcoming", empty: ["calendar", "Nothing coming up", "Tasks with a future date show here."] },
  { key: "no_date", label: "No date", icon: "list", count: "no_date", empty: ["list", "Everything has a date", "Undated tasks show here."] },
  { key: "all", label: "All", icon: "list-checks", count: "total_open", empty: ["list-checks", "No open tasks", "Add one above, try \"call mum tomorrow at 5pm\"."] },
  { key: "done", label: "Done", icon: "check", empty: ["check", "Nothing finished yet", "Completed tasks land here."] },
];
const PRIORITY = [["0", "None"], ["1", "Low"], ["2", "Medium"], ["3", "Urgent"]];
const PRI_TONE = { 1: "info", 2: "warn", 3: "bad" };
const PRI_NAME = { 1: "Low", 2: "Medium", 3: "Urgent" };
const REPEATS = [["none", "Doesn't repeat"], ["daily", "Every day"], ["weekly", "Every week"], ["monthly", "Every month"]];

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const todayStr = () => ymd(new Date());
function parseDue(due) {
  if (!due) return null;
  const [date, time] = due.split("T");
  const [y, m, d] = date.split("-").map(Number);
  return { date, time: time || "", obj: new Date(y, m - 1, d) };
}
function dayDiff(dateStr) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const a = new Date(y, m - 1, d); const n = new Date(); const b = new Date(n.getFullYear(), n.getMonth(), n.getDate());
  return Math.round((a - b) / 86400000);
}
function dueLabel(due) {
  const p = parseDue(due);
  if (!p) return null;
  const diff = dayDiff(p.date);
  let text;
  if (diff === 0) text = "Today";
  else if (diff === 1) text = "Tomorrow";
  else if (diff === -1) text = "Yesterday";
  else if (diff > 1 && diff < 7) text = p.obj.toLocaleDateString("en-GB", { weekday: "long" });
  else text = p.obj.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(p.obj.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
  return { text: p.time ? `${text}, ${p.time}` : text, diff };
}
function cleanTags(text) {
  return text.split(/[,\s]+/).map((t) => t.replace(/^#/, "").trim().toLowerCase().replace(/\s+/g, "-")).filter(Boolean).slice(0, 10);
}

export function init(container, ctx) {
  if (!document.querySelector('link[data-css="todos"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/todos.css", "data-css": "todos" }));
  }
  const state = { view: "today", smart: null, lists: [], tasks: [], loaded: false, error: "", seq: 0, echoTimer: null, visible: false };

  const root = el("div", { class: "td" });
  container.replaceChildren(root);

  // quick add
  const quickInput = el("input", { type: "text", class: "td-quick__input", placeholder: "Add a task: buy milk tomorrow !2 #errands", "aria-label": "Quick add a task", enterkeyhint: "send", autocomplete: "off", maxlength: "260" });
  const quickBtn = ui.button({ variant: "primary", iconOnly: true, icon: "arrow-up", label: "Add task", type: "submit" });
  const quick = el("form", { class: "td-quick", novalidate: true }, ui.icon("plus", { class: "td-quick__plus" }), quickInput, quickBtn);
  const echo = el("div", { class: "td-echo", hidden: true, role: "status", "aria-live": "polite" });
  const chips = el("div", { class: "td-chips ui-hscroll", role: "group", "aria-label": "Task lists" });
  const listHead = el("div", { class: "td-listhead", hidden: true });
  const body = el("div", { class: "td-body" });
  root.append(quick, echo, chips, listHead, body);

  const customList = () => (state.view.startsWith("list:") ? state.view.slice(5) : null);

  // ---------- chips ----------
  function renderChips() {
    chips.replaceChildren();
    const mk = (key, label, iconName, count, tone) => {
      const on = state.view === key;
      const chip = el("button", { type: "button", class: "ui-chip td-chip" + (tone ? ` td-chip--${tone}` : ""), "aria-pressed": String(on),
        onclick: () => { ui.haptic("select"); state.view = key; renderChips(); load({ showLoading: true }); } },
      ui.icon(iconName), label, count != null && count !== 0 ? el("span", { class: "td-chip__n ui-num" }, String(count)) : null);
      chips.append(chip);
      if (on) requestAnimationFrame(() => chip.scrollIntoView?.({ inline: "center", block: "nearest" }));
    };
    for (const s of SMART) mk(s.key, s.label, s.icon, state.smart ? state.smart[s.count] : null, s.count === "overdue" && state.smart?.overdue ? "bad" : "");
    chips.append(el("span", { class: "td-chips__sep", "aria-hidden": "true" }));
    for (const l of state.lists) {
      if (l.name === "Today" && l.default) continue;
      mk("list:" + l.name, l.name, "folder", l.open);
    }
    chips.append(el("button", { type: "button", class: "ui-chip td-chip td-chip--add", onclick: addList }, ui.icon("plus"), "List"));
  }

  function renderListHead() {
    const name = customList();
    listHead.hidden = !name;
    listHead.replaceChildren();
    if (!name) return;
    const info = state.lists.find((l) => l.name === name);
    listHead.append(el("h2", { class: "td-listhead__title" }, name),
      ...(info && !info.default ? [
        ui.button({ label: "Rename", icon: "edit", variant: "ghost", size: "sm", onClick: () => renameList(name) }),
        ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: () => deleteList(name) })] : []));
  }

  async function addList() {
    const input = el("input", { type: "text", placeholder: "List name", maxlength: "40", "aria-label": "List name", autocomplete: "off" });
    ui.sheet({ title: "New list", content: input, actions: guard([{ label: "Create list", variant: "primary", icon: "plus",
      async onClick() {
        const name = input.value.trim();
        if (!name) { input.focus(); return false; }
        try { await api("/api/todos/lists", { method: "POST", json: { name } }); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        state.view = "list:" + name;
        await Promise.all([loadMeta(), load({ showLoading: true })]);
        return true;
      } }]) });
  }
  function renameList(name) {
    const input = el("input", { type: "text", value: name, maxlength: "40", "aria-label": "List name", autocomplete: "off" });
    ui.sheet({ title: "Rename list", content: input, actions: guard([{ label: "Save", variant: "primary", icon: "check",
      async onClick() {
        const next = input.value.trim();
        if (!next || next === name) return true;
        try { await api(`/api/todos/lists/${encodeURIComponent(name)}`, { method: "PATCH", json: { name: next } }); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        state.view = "list:" + next;
        await Promise.all([loadMeta(), load()]);
        return true;
      } }]) });
  }
  async function deleteList(name) {
    if (!await ui.confirm({ title: `Delete "${name}"?`, message: "Its tasks move to Inbox.", confirmLabel: "Delete list", danger: true })) return;
    try {
      await api(`/api/todos/lists/${encodeURIComponent(name)}`, { method: "DELETE" });
      state.view = "all";
      ctx.toast("List deleted", { kind: "good" });
      await Promise.all([loadMeta(), load({ showLoading: true })]);
    } catch (e) { ctx.toast(e.message, { kind: "bad" }); }
  }

  // ---------- rows ----------
  function dueBadge(task) {
    const d = dueLabel(task.due);
    if (!d) return null;
    const tone = task.done ? undefined : d.diff < 0 ? "bad" : d.diff === 0 ? "accent" : undefined;
    const badge = ui.badge(d.text, tone);
    badge.classList.add("td-due");
    return badge;
  }
  function taskRow(task) {
    const check = el("button", { type: "button", class: "td-check" + (task.done ? " is-done" : "") + (task.priority ? ` td-check--p${task.priority}` : ""),
      role: "checkbox", "aria-checked": String(task.done), "aria-label": `${task.done ? "Mark not done" : "Mark done"}: ${task.title}`,
      onclick: (e) => { e.stopPropagation(); toggle(task, check); } }, ui.icon("check"));
    const subs = task.subtasks || [];
    const doneSubs = subs.filter((s) => s.done).length;
    const meta = el("span", { class: "td-meta" },
      dueBadge(task),
      task.repeat && task.repeat !== "none" ? el("span", { class: "td-rep", title: `Repeats ${task.repeat}` }, ui.icon("refresh"), task.repeat) : null,
      task.priority ? el("span", { class: `td-pri td-pri--${PRI_TONE[task.priority]}` }, ui.icon("flame"), PRI_NAME[task.priority]) : null,
      subs.length ? el("span", { class: "td-subs ui-num" }, ui.icon("list-checks"), `${doneSubs}/${subs.length}`, ui.progressBar(doneSubs / subs.length, "Steps done")) : null,
      ...(task.tags || []).slice(0, 3).map((t) => el("span", { class: "td-tag" }, `#${t}`)),
      state.view === "all" || state.view === "done" ? el("span", { class: "td-listname" }, task.list) : null);
    const row = el("div", { class: "ui-row ui-row--interactive td-row" + (task.done ? " is-done" : ""), role: "button", tabindex: "0",
      onclick: () => openDetail(task.id),
      onkeydown: (e) => { if (e.key === "Enter") openDetail(task.id); } },
    check, el("span", { class: "ui-row__body" }, el("span", { class: "td-title" }, task.title), meta));
    return row;
  }

  function tasksForView(list) {
    if (state.view === "upcoming") return list.filter((t) => t.due && dayDiff(parseDue(t.due).date) > 0);
    return list;
  }
  function render() {
    body.replaceChildren();
    if (!state.loaded && !state.error) { body.append(ui.skeleton("list", 5)); return; }
    if (state.error) {
      body.append(ui.notice(state.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => load({ showLoading: true }) }));
      return;
    }
    const tasks = tasksForView(state.tasks);
    if (!tasks.length) {
      const smart = SMART.find((s) => s.key === state.view);
      const [ic, title, text] = smart ? smart.empty : ["folder", "This list is empty", "Add a task above and it lands here."];
      body.append(ui.emptyState({ icon: ic, title, body: text, compact: true }));
      return;
    }
    const open = tasks.filter((t) => !t.done);
    const done = tasks.filter((t) => t.done);
    const group = (items, title) => {
      if (!items.length) return;
      if (title) body.append(el("h2", { class: "td-sec ui-caps" }, title));
      body.append(el("div", { class: "ui-list ui-list--inset td-list" }, ...items.map(taskRow)));
    };
    group(open);
    group(done, open.length ? "Completed" : null);
  }

  // ---------- data ----------
  async function loadMeta() {
    try {
      const [smart, lists] = await Promise.all([api("/api/todos/smart"), api("/api/todos/lists")]);
      state.smart = smart; state.lists = lists.lists || [];
    } catch (_) { /* chips just stay without counts */ }
    if (customList() && !state.lists.some((l) => l.name === customList())) state.view = "all";
    renderChips(); renderListHead();
  }
  async function load({ showLoading = false } = {}) {
    const seq = ++state.seq;
    if (showLoading) { state.loaded = false; state.error = ""; render(); }
    renderListHead();
    const p = new URLSearchParams();
    const v = state.view;
    if (v === "today") p.set("due", "today");
    else if (v === "overdue") p.set("due", "overdue");
    else if (v === "no_date") p.set("due", "none");
    else if (v === "done") p.set("done", "true");
    else if (v.startsWith("list:")) p.set("list", v.slice(5));
    if (v !== "done" && !v.startsWith("list:")) p.set("done", "false");
    try {
      const data = await api("/api/todos?" + p);
      if (seq !== state.seq) return;
      state.tasks = data.tasks || []; state.error = ""; state.loaded = true;
      if (v === "no_date") state.tasks = state.tasks.filter((t) => !t.due);
    } catch (e) {
      if (seq !== state.seq) return;
      state.error = e.message; state.loaded = true;
    }
    render();
  }
  const reload = () => Promise.all([loadMeta(), load()]);

  const toggling = new Set();   // a double-tap must not toggle a task twice
  async function toggle(task, check) {
    if (toggling.has(task.id)) return;
    toggling.add(task.id);
    setTimeout(() => toggling.delete(task.id), 900);
    check.classList.toggle("is-done", !task.done);
    ui.haptic(task.done ? "light" : "success");
    try {
      const res = await api(`/api/todos/${task.id}/toggle`, { method: "POST", json: {} });
      if (res.spawned) ctx.toast(`Next one: ${dueLabel(res.spawned.due)?.text || "scheduled"}`, { kind: "good", icon: "refresh" });
      else if (res.task?.done) ctx.toast("Done", { kind: "good", action: { label: "Undo", onClick: async () => { try { await api(`/api/todos/${task.id}/toggle`, { method: "POST", json: { done: false } }); reload(); } catch (e) { ctx.toast(e.message, { kind: "bad" }); } } } });
      setTimeout(reload, 350);
    } catch (e) { toggling.delete(task.id); check.classList.toggle("is-done", task.done); ctx.toast(e.message, { kind: "bad" }); }
  }

  // ---------- quick add ----------
  function showEcho(task) {
    clearTimeout(state.echoTimer);
    const d = dueLabel(task.due);
    echo.replaceChildren(ui.icon("check-circle"), el("div", { class: "td-echo__body" },
      el("span", { class: "td-echo__title" }, task.title),
      el("span", { class: "td-echo__meta" },
        `in ${task.list}`,
        d ? ui.badge(d.text, "accent") : null,
        task.priority ? ui.badge(PRI_NAME[task.priority], PRI_TONE[task.priority]) : null,
        task.repeat && task.repeat !== "none" ? ui.badge(task.repeat, "") : null,
        ...(task.tags || []).map((t) => el("span", { class: "td-tag" }, `#${t}`)))));
    echo.hidden = false;
    state.echoTimer = setTimeout(() => { echo.hidden = true; }, 6000);
  }
  quick.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = quickInput.value.trim();
    if (!text) { quickInput.focus(); return; }
    if (quickBtn.getAttribute("aria-busy") === "true") return;
    const json = { text };
    if (customList()) json.list = customList();
    try {
      const task = await ui.busy(quickBtn, api("/api/todos/quick", { method: "POST", json }));
      quickInput.value = "";
      ui.haptic("success");
      showEcho(task);
      await reload();
    } catch (err) { ctx.toast(err.message, { kind: "bad" }); }
  });

  // ---------- detail sheet ----------
  async function openDetail(id) {
    const s = ui.sheet({ title: "Task", content: ui.skeleton("lines", 5), class: "td-sheet" });
    let task;
    try { task = await api(`/api/todos/${encodeURIComponent(id)}`); } catch (e) {
      s.body.replaceChildren(ui.notice(e.message, { tone: "bad" }));
      return;
    }
    buildDetail(s, task);
  }

  function buildDetail(s, task) {
    s.setTitle(task.done ? "Completed task" : "Task");
    let subtasks = (task.subtasks || []).map((x) => ({ ...x }));
    const title = el("input", { type: "text", value: task.title, maxlength: "200", "aria-label": "Title", class: "td-d-title" });
    const notes = el("textarea", { rows: "3", maxlength: "5000", placeholder: "Notes", "aria-label": "Notes" });
    notes.value = task.notes || "";
    const p = parseDue(task.due);
    const dateIn = el("input", { type: "date", value: p?.date || "", "aria-label": "Due date" });
    const timeIn = el("input", { type: "time", value: p?.time || "", "aria-label": "Due time" });
    const clearDue = ui.button({ label: "Clear date", icon: "x", variant: "ghost", size: "sm", onClick: () => { dateIn.value = ""; timeIn.value = ""; syncRepeat(); } });
    const pri = ui.segmented({ options: PRIORITY.map(([value, label]) => ({ value, label })), value: String(task.priority || 0), label: "Priority", block: true });
    const listSel = el("select", { "aria-label": "List" }, ...state.lists.map((l) => el("option", { value: l.name }, l.name)));
    if (!state.lists.some((l) => l.name === task.list)) listSel.append(el("option", { value: task.list }, task.list));
    listSel.value = task.list;
    const repeatSel = el("select", { "aria-label": "Repeat" }, ...REPEATS.map(([v, l]) => el("option", { value: v }, l)));
    repeatSel.value = task.repeat || "none";
    const repeatHint = el("p", { class: "ui-hint", hidden: true }, "A repeating task needs a due date.");
    function syncRepeat() { const no = !dateIn.value; repeatSel.disabled = no; if (no) repeatSel.value = "none"; repeatHint.hidden = !no; }
    dateIn.addEventListener("input", syncRepeat);
    syncRepeat();
    const tags = el("input", { type: "text", value: (task.tags || []).map((t) => `#${t}`).join(" "), placeholder: "#work #errands", "aria-label": "Tags", autocapitalize: "none", spellcheck: "false" });

    // subtasks
    const stepList = el("div", { class: "td-steps" });
    const stepBar = el("div", { class: "td-steps__bar" });
    const stepAdd = el("input", { type: "text", placeholder: "Add a step", maxlength: "200", "aria-label": "Add a step", enterkeyhint: "done" });
    const proposal = el("div", { class: "td-proposal", hidden: true });
    function renderSteps() {
      stepList.replaceChildren();
      const done = subtasks.filter((x) => x.done).length;
      stepBar.replaceChildren(subtasks.length ? el("span", { class: "td-steps__n ui-num" }, `${done} of ${subtasks.length}`) : null, subtasks.length ? ui.progressBar(done / subtasks.length, "Steps done") : null);
      for (const st of subtasks) {
        const box = el("button", { type: "button", class: "td-check td-check--sm" + (st.done ? " is-done" : ""), role: "checkbox", "aria-checked": String(st.done), "aria-label": st.text,
          onclick: async () => {
            try { const t = await api(`/api/todos/${task.id}/subtasks/${st.id}/toggle`, { method: "POST", json: {} }); task = t; subtasks = t.subtasks.map((x) => ({ ...x })); renderSteps(); load(); }
            catch (e) { ctx.toast(e.message, { kind: "bad" }); }
          } }, ui.icon("check"));
        stepList.append(el("div", { class: "td-step" + (st.done ? " is-done" : "") }, box, el("span", { class: "td-step__text" }, st.text),
          ui.button({ iconOnly: true, icon: "close", variant: "ghost", size: "sm", label: `Remove step ${st.text}`, onClick: () => removeStep(st.id) })));
      }
    }
    async function saveSteps(list) {
      const t = await api(`/api/todos/${task.id}`, { method: "PATCH", json: { subtasks: list.map((x) => ({ id: x.id, text: x.text, done: x.done })) } });
      task = t; subtasks = t.subtasks.map((x) => ({ ...x })); renderSteps(); load();
    }
    async function removeStep(sid) { try { await saveSteps(subtasks.filter((x) => x.id !== sid)); } catch (e) { ctx.toast(e.message, { kind: "bad" }); } }
    stepAdd.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const text = stepAdd.value.trim();
      if (!text) return;
      try { await saveSteps([...subtasks, { id: undefined, text, done: false }]); stepAdd.value = ""; } catch (err) { ctx.toast(err.message, { kind: "bad" }); }
    });

    // AI breakdown with accept list
    const model = ui.segmented({ options: [{ value: "local", label: "Local model" }, { value: "claude", label: "Claude" }], value: "local", label: "Model" });
    const modelHint = el("p", { class: "ui-hint", hidden: true }, "Claude sends this task's text to Anthropic.");
    model.addEventListener("click", () => { modelHint.hidden = model.value !== "claude"; });
    const breakBtn = ui.button({ label: "Break it down", icon: "wand", variant: "tonal", block: true, onClick: async () => {
      try {
        const res = await ui.busy(breakBtn, api(`/api/todos/${task.id}/breakdown`, { method: "POST", json: { model: model.value } }));
        task = res.task; subtasks = res.task.subtasks.map((x) => ({ ...x })); renderSteps();
        showProposal(res.added || []);
        load();
      } catch (e) { ctx.toast(e.message, { kind: "bad" }); }
    } });
    function showProposal(added) {
      proposal.replaceChildren();
      if (!added.length) { proposal.hidden = false; proposal.append(ui.notice("No new steps to add. The task may already be broken down.", { tone: "warn" })); return; }
      const keep = new Set(added.map((a) => a.id));
      const rows = added.map((a) => {
        const box = el("button", { type: "button", class: "td-check td-check--sm is-done", role: "checkbox", "aria-checked": "true", "aria-label": a.text,
          onclick: () => { if (keep.has(a.id)) keep.delete(a.id); else keep.add(a.id); const on = keep.has(a.id); box.classList.toggle("is-done", on); box.setAttribute("aria-checked", String(on)); } }, ui.icon("check"));
        return el("div", { class: "td-step" }, box, el("span", { class: "td-step__text" }, a.text));
      });
      const accept = ui.button({ label: "Keep selected", icon: "check", variant: "primary", size: "sm", onClick: async () => {
        const drop = added.filter((a) => !keep.has(a.id)).map((a) => a.id);
        try { if (drop.length) await saveSteps(subtasks.filter((x) => !drop.includes(x.id))); proposal.hidden = true; ui.haptic("success"); ctx.toast(`${keep.size} steps kept`, { kind: "good" }); }
        catch (e) { ctx.toast(e.message, { kind: "bad" }); }
      } });
      const discard = ui.button({ label: "Discard all", variant: "ghost", size: "sm", onClick: async () => {
        try { await saveSteps(subtasks.filter((x) => !added.some((a) => a.id === x.id))); proposal.hidden = true; } catch (e) { ctx.toast(e.message, { kind: "bad" }); }
      } });
      proposal.append(el("p", { class: "ui-caps td-proposal__t" }, ui.icon("sparkles"), "Suggested steps"), ...rows, el("div", { class: "td-proposal__act" }, discard, accept));
      proposal.hidden = false;
    }
    renderSteps();

    const field = (label, ...nodes) => el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, label), ...nodes);
    s.body.replaceChildren(el("div", { class: "ui-stack td-form" },
      title, notes,
      field("Priority", pri),
      field("Due", el("div", { class: "td-due-row" }, dateIn, timeIn), clearDue),
      el("div", { class: "td-two" }, field("Repeat", repeatSel, repeatHint), field("List", listSel)),
      field("Tags", tags),
      el("div", { class: "td-stepsbox" },
        el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "Steps")),
        stepBar, stepList, proposal,
        el("div", { class: "ui-input-wrap td-stepadd" }, ui.icon("plus"), stepAdd),
        el("div", { class: "td-ai" }, el("div", { class: "td-ai__row" }, breakBtn), model, modelHint))));
    s.setActions(guard([
      { label: "Delete", icon: "trash", variant: "danger-soft", async onClick() {
        if (!await ui.confirm({ title: "Delete this task?", message: task.title, confirmLabel: "Delete", danger: true })) return false;
        try { await api(`/api/todos/${task.id}`, { method: "DELETE" }); ui.toast("Task deleted", { kind: "good" }); reload(); } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        return true;
      } },
      { label: "Save", icon: "check", variant: "primary", async onClick() {
        const t = title.value.trim();
        if (!t) { ctx.toast("A task needs a title.", { kind: "warn" }); title.focus(); return false; }
        const due = dateIn.value ? dateIn.value + (timeIn.value ? `T${timeIn.value}` : "") : null;
        try {
          await api(`/api/todos/${task.id}`, { method: "PATCH", json: { title: t, notes: notes.value, due, priority: Number(pri.value), list: listSel.value, repeat: due ? repeatSel.value : "none", tags: cleanTags(tags.value) } });
          ui.haptic("success"); ctx.toast("Saved", { kind: "good" }); reload();
        } catch (e) { ctx.toast(e.message, { kind: "bad" }); return false; }
        return true;
      } },
    ]));
  }

  ui.pullToRefresh(container, reload);
  renderChips();
  render();
  return {
    show() { state.visible = true; loadMeta(); load({ showLoading: !state.loaded }); },
    hide() { state.visible = false; },
    refresh: reload,
    openSection(name) { if (name === "new" || name === "add") quickInput.focus(); },
    openItem(id) { if (id != null) openDetail(String(id)); },
  };
}
