// Memory (Brain): what Jarvis and Friday remember (RCAs, decisions, notes, what people asked you) and the task desk.
// Routes: docs/api/brain.md. Everything shown is text (textContent), never HTML.
import { api, ApiError, el, sse } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/brain.css";
const KINDS = [["", "All"], ["rca", "RCA"], ["decision", "Decisions"], ["task", "Tasks"], ["note", "Notes"]];
const PERSONAS = [["", "Both voices"], ["jarvis", "Jarvis"], ["friday", "Friday"]];
const KIND_NAME = { note: "Note", fact: "Fact", decision: "Decision", rca: "RCA", task: "Task", research: "Research" };
const VERDICT = { yes: ["Can do", "good"], partly: ["Partly", "warn"], no: ["Can't yet", "bad"] };
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const day = (iso) => String(iso || "").slice(0, 10);
const at = (iso) => String(iso || "").slice(0, 16).replace("T", " ");

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "bn ui-stack ui-stack--lg" });
  container.append(root);
  let kind = "", persona = "", query = "", status = "open", items = [], total = 0, loadError = "", due = [];
  let timer = null, seq = 0;
  const open = new Set();
  const sid = "brain-" + Math.random().toString(36).slice(2, 10);
  let askOut = null;
  const slot = { list: el("div", { class: "ui-stack" }), intake: null, head: null };

  async function load() {
    const mine = ++seq;
    try {
      const params = new URLSearchParams({ status, limit: "50" });
      if (query) params.set("q", query);
      if (kind) params.set("kind", kind);
      if (persona) params.set("persona", persona);
      const [list, dueNow] = await Promise.all([api(`/api/brain/items?${params}`), api("/api/brain/due").catch(() => ({ due: [] }))]);
      if (mine !== seq) return;
      items = list.items || []; total = list.total || 0; due = dueNow.due || []; loadError = "";
    } catch (error) {
      if (mine !== seq) return;
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paintList();
  }

  async function patch(item, body, okText) {
    try { await api(`/api/brain/items/${item.id}`, { method: "PATCH", json: body }); if (okText) ui.toast(okText, { kind: "good" }); load(); } catch (error) { fail(error); }
  }
  async function remove(item) {
    if (!(await ui.confirm({ title: "Delete this memory?", message: item.title, confirmLabel: "Delete", danger: true, icon: "trash" }))) return;
    try { await api(`/api/brain/items/${item.id}`, { method: "DELETE" }); load(); } catch (error) { fail(error); }
  }

  function listOf(title, rows) {
    if (!rows?.length) return null;
    return el("div", { class: "ui-stack" }, el("p", { class: "ui-caps bn-label" }, title),
      el("ul", { class: "bn-list" }, ...rows.map((r) => el("li", {}, r))));
  }

  function card(item) {
    const a = item.assessment;
    const v = a && VERDICT[a.doable];
    const body = el("p", { class: "bn-body" }, item.body || "");
    const node = el("article", { class: "ui-card ui-stack bn-item" + (open.has(item.id) ? " is-open" : "") + (item.status === "done" ? " is-done" : "") });
    body.addEventListener("click", () => { open.has(item.id) ? open.delete(item.id) : open.add(item.id); node.classList.toggle("is-open"); });
    const meta = el("div", { class: "bn-meta" },
      ui.badge(item.persona === "both" ? "Both" : item.persona === "friday" ? "Friday" : "Jarvis", item.persona === "friday" ? "warn" : "accent"),
      item.who ? el("span", {}, "from " + item.who) : null,
      item.project ? el("span", {}, item.project) : null,
      item.remind_at ? el("span", {}, "follow-up " + at(item.remind_at)) : null,
      ...(item.tags || []).map((t) => el("span", {}, "#" + t)));
    node.append(
      el("div", { class: "bn-item-head" },
        ui.badge(KIND_NAME[item.kind] || item.kind, "accent"),
        v ? el("span", { class: "ui-badge ui-badge--" + v[1] + " bn-verdict" }, v[0]) : null,
        item.status !== "open" ? ui.badge(item.status, "") : null,
        el("h3", { class: "bn-title" }, item.title),
        el("span", { class: "bn-when" }, day(item.created))),
      meta, body,
      a ? el("div", { class: "ui-stack" },
        listOf("Blockers", a.blockers), listOf("Risks", a.risks), listOf("Questions", a.questions), listOf("Plan", a.plan),
        a.estimate ? el("p", { class: "bn-meta" }, "Estimate: " + a.estimate) : null) : null,
      el("div", { class: "bn-actions" },
        item.status === "open"
          ? ui.button({ label: item.kind === "task" ? "Done" : "Archive", icon: "check", variant: "tonal", size: "sm", onClick: () => patch(item, { status: "done" }, "Marked done.") })
          : ui.button({ label: "Reopen", icon: "refresh", variant: "tonal", size: "sm", onClick: () => patch(item, { status: "open" }) }),
        item.kind === "task" && item.status === "open"
          ? ui.button({ label: "Blocked", icon: "alert", variant: "ghost", size: "sm", onClick: () => patch(item, { status: "blocked" }) }) : null,
        ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: () => remove(item) })));
    return node;
  }

  function paintList() {
    if (loadError === "off") { slot.list.replaceChildren(ui.emptyState({ icon: "book-open", title: "Memory is switched off", body: "Set brain_enabled to true in Settings." })); return; }
    if (loadError) { slot.list.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load memory", body: loadError, action: { label: "Try again", icon: "refresh", onClick: load } })); return; }
    const nodes = [];
    if (due.length && !query) {
      nodes.push(el("section", { class: "ui-stack" }, ui.eyebrow("FU", "Follow-ups due today"),
        el("div", { class: "ui-list ui-list--inset" }, ...due.map((d) => ui.listRow({ icon: "bell", tone: d.overdue ? "warn" : "accent", title: d.title, sub: (d.who ? "from " + d.who + " · " : "") + at(d.remind_at) })))));
    }
    nodes.push(items.length ? el("div", { class: "ui-stack" }, ...items.map(card))
      : ui.emptyState({ icon: "book-open", title: query ? "Nothing like that" : "Nothing remembered yet",
        body: query ? "Try other words." : 'Say "Jarvis, remember ..." or add something above.' }));
    slot.list.replaceChildren(...nodes);
    if (slot.count) slot.count.textContent = `${items.length} of ${total}`;
  }

  // ---- search and filters
  function chips(options, get, set, extra = "") {
    const row = el("div", { class: "bn-chips", role: "group" });
    const draw = () => row.replaceChildren(...options.map(([value, label]) => {
      const b = el("button", { type: "button", class: "bn-chip" + (extra && value ? ` bn-chip--${value}` : ""), "aria-pressed": String(get() === value) }, label);
      b.addEventListener("click", () => { set(value); draw(); load(); });
      return b;
    }));
    draw();
    return row;
  }

  function searchBar() {
    const input = el("input", { class: "ui-input", type: "search", placeholder: "Search memory: what was the RCA for ...", "aria-label": "Search memory", enterkeyhint: "search" });
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { query = input.value.trim(); load(); }, 250); });
    return input;
  }

  // ---- quick add
  function quickAdd() {
    const text = el("textarea", { class: "ui-input", placeholder: "Remember this ... (an RCA, a decision, what someone asked)", "aria-label": "What to remember", maxlength: "8000" });
    const kindSel = el("select", { class: "ui-input", "aria-label": "Kind" }, ...["note", "rca", "decision", "fact", "research", "task"].map((k) => el("option", { value: k }, KIND_NAME[k])));
    const who = el("input", { class: "ui-input", type: "text", placeholder: "From (optional)", "aria-label": "From", maxlength: "80" });
    const when = el("input", { class: "ui-input", type: "text", placeholder: "Tell me when (tomorrow 9am)", "aria-label": "Follow-up time", maxlength: "60" });
    const save = ui.button({ label: "Save", icon: "plus", variant: "primary", onClick: () => ui.busy(save, async () => {
      if (!text.value.trim()) { ui.toast("Write what to remember first.", { kind: "warn" }); return; }
      const json = { text: text.value, kind: kindSel.value, source: "app" };
      if (who.value.trim()) json.who = who.value.trim();
      if (when.value.trim()) json.remind_at = when.value.trim();
      try { await api("/api/brain/items", { method: "POST", json }); text.value = ""; who.value = ""; when.value = ""; ui.toast("Saved.", { kind: "good" }); load(); } catch (error) { fail(error); }
    }) });
    return el("section", { class: "ui-card ui-stack bn-add" }, ui.eyebrow("01", "Remember"), text,
      el("div", { class: "bn-fields" }, kindSel, who, when), el("div", { class: "bn-actions" }, save));
  }

  // ---- task intake: "someone gave me this; can it be done?"
  function intake() {
    const from = el("input", { class: "ui-input", type: "text", placeholder: "Who asked?", "aria-label": "Who asked", maxlength: "80" });
    const req = el("textarea", { class: "ui-input", placeholder: "What did they ask for?", "aria-label": "The task", maxlength: "8000" });
    const out = el("div", { class: "ui-stack" });
    const go = ui.button({ label: "Can it be done?", icon: "sparkles", variant: "primary", onClick: () => ui.busy(go, async () => {
      if (!req.value.trim()) { ui.toast("Write the task first.", { kind: "warn" }); return; }
      out.replaceChildren(el("p", { class: "ui-hint" }, "Thinking it through with your memory ..."));
      try {
        const r = await api("/api/brain/intake", { method: "POST", json: { from: from.value.trim(), request: req.value } });
        const v = r.assessment && VERDICT[r.assessment.doable];
        out.replaceChildren(v ? el("span", { class: "ui-badge ui-badge--" + v[1] + " bn-verdict" }, v[0]) : null, el("p", { class: "bn-spoken" }, r.spoken));
        req.value = ""; from.value = "";
        load();
      } catch (error) { out.replaceChildren(); fail(error); }
    }) });
    return el("section", { class: "ui-card ui-stack bn-intake" }, ui.eyebrow("02", "Someone gave me a task"), from, req, el("div", { class: "bn-actions" }, go), out);
  }

  // ---- ask Jarvis about this memory
  function screenText() {
    const L = ["MEMORY screen, newest first (what the owner has saved):"];
    for (const i of items.slice(0, 12)) L.push(`- [${i.kind}, ${day(i.created)}${i.who ? ", from " + i.who : ""}] ${i.title}: ${String(i.body || "").slice(0, 200)}`);
    for (const d of due) L.push(`Follow-up due: ${d.title}`);
    return L.join("\n");
  }

  async function sendAsk(text, input, btn) {
    text = (text || "").trim();
    if (!text) return;
    input.value = "";
    askOut.replaceChildren(el("p", { class: "ui-hint" }, "You: " + text), el("p", { class: "bn-answer" }, "Thinking…"));
    const answer = askOut.lastChild;
    let got = "";
    btn.disabled = true;
    try {
      for await (const ev of sse("/api/assistant/message", { session_id: sid, text, context: screenText(), provider: "auto", auto_approve: false, persona: "jarvis", voice: false })) {
        if (ev.type === "text") { got += ev.t || ""; answer.textContent = got; }
        else if (ev.type === "approval_needed") {
          const row = el("div", { class: "ui-row" });
          const done = (ok) => { api("/api/assistant/approve", { method: "POST", json: { session_id: sid, call_id: ev.id, approve: ok } }).catch(fail); row.remove(); };
          row.append(el("span", { class: "ui-hint" }, `Wants to use “${ev.name}”.`),
            ui.button({ label: "Approve", icon: "check", onClick: () => done(true) }),
            ui.button({ label: "Deny", variant: "secondary", icon: "x", onClick: () => done(false) }));
          askOut.append(row);
        } else if (ev.type === "error") answer.textContent = ev.message || "Something went wrong.";
      }
      if (!got && answer.textContent === "Thinking…") answer.textContent = "Done.";
      load();
    } catch (error) { answer.textContent = error.message; }
    btn.disabled = false;
  }

  function askBar() {
    const input = el("input", { class: "ui-input", type: "text", placeholder: "Ask Jarvis about this: what was the fix for ...", enterkeyhint: "send", "aria-label": "Ask Jarvis about this memory" });
    const send = ui.button({ label: "Ask", icon: "send", onClick: () => sendAsk(input.value, input, send) });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendAsk(input.value, input, send); } });
    askOut = el("div", { class: "ui-stack" });
    return el("section", { class: "ui-card ui-stack bn-ask" }, ui.eyebrow("AI", "Ask Jarvis about this"), el("div", { class: "ui-row" }, input, send), askOut);
  }

  slot.count = el("span", { class: "bn-count" }, "");
  const statusChips = chips([["open", "Open"], ["done", "Done"], ["all", "All"]], () => status, (v) => { status = v; });
  root.append(
    el("div", { class: "bn-head" }, el("h2", { class: "ui-title" }, "Memory"), slot.count),
    searchBar(),
    chips(KINDS, () => kind, (v) => { kind = v; }),
    chips(PERSONAS, () => persona, (v) => { persona = v; }, "persona"),
    statusChips,
    slot.list, quickAdd(), intake(), askBar());
  slot.list.replaceChildren(ui.skeleton("card", 2));
  load();
  ui.pullToRefresh?.(container, load);
  return { show() { load(); }, refresh() { load(); } };
}
