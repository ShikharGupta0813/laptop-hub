// Playbooks: "when X happens, do Y". The owner's list of rules, a step-by-step editor built from forms, a dry-run
// ("Test with a sample event"), run history and starter templates. Routes: docs/api/playbooks.md.
// Every string goes in with textContent (el()), never as markup. Playbooks are edited here only: the assistant cannot.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor } from "./listeditor.js";

const CSS = "/css/playbooks.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const loadCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };
const when = (iso) => String(iso || "").slice(0, 16).replace("T", " ");
const STATUS = { running: ["Running", "accent"], waiting: ["Waiting for you", "warn"], done: ["Done", "good"], partial: ["Done, some steps failed", "warn"],
  failed: ["Failed", "bad"], cancelled: ["Cancelled", "gray"], timeout: ["Timed out", "bad"], skipped: ["Skipped", "gray"], interrupted: ["Interrupted", "warn"] };
const STEP_STATUS = { ok: ["Done", "good"], failed: ["Failed", "bad"], denied: ["You said no", "warn"], blocked: ["Refused", "bad"], waiting: ["Waiting for you", "warn"],
  running: ["Running", "accent"], would_run: ["Would run", "accent"], would_block: ["Would be refused", "warn"], cancelled: ["Cancelled", "gray"],
  timeout: ["Timed out", "bad"], interrupted: ["Interrupted", "warn"], skipped: ["Skipped", "gray"] };
const DAYS = [[0, "Mon"], [1, "Tue"], [2, "Wed"], [3, "Thu"], [4, "Fri"], [5, "Sat"], [6, "Sun"]];

// ---- one form field from a schema entry -------------------------------------------------------------------
// -> {node, get(), set(v)}. Types: text, textarea, number, select, chips, bool.
function field(spec, value, onChange) {
  const emit = () => onChange?.();
  let input, get, set;
  if (spec.type === "select") {
    input = el("select", { class: "ui-input" }, ...(spec.options || []).map(([v, t]) => el("option", { value: v }, t)));
    set = (v) => { input.value = v ?? (spec.options?.[0]?.[0] ?? ""); };
    get = () => input.value;
  } else if (spec.type === "bool") {
    const sw = ui.switchControl({ checked: !!value, label: spec.label, onChange: emit });
    input = sw; set = (v) => { sw.input.checked = !!v; }; get = () => sw.input.checked;
  } else if (spec.type === "textarea") {
    input = el("textarea", { class: "ui-input", rows: "3", maxlength: "1500" });
    set = (v) => { input.value = v ?? ""; }; get = () => input.value;
  } else if (spec.type === "number") {
    input = el("input", { class: "ui-input", type: "number", inputmode: "numeric", min: spec.min, max: spec.max });
    set = (v) => { input.value = v ?? ""; }; get = () => (input.value === "" ? null : Number(input.value));
  } else if (spec.type === "chips") {
    input = el("input", { class: "ui-input", type: "text", maxlength: "400", placeholder: spec.hint || "word, another word" });
    set = (v) => { input.value = Array.isArray(v) ? v.join(", ") : (v ?? ""); };
    get = () => input.value.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
  } else {
    input = el("input", { class: "ui-input", type: "text", maxlength: "200" });
    set = (v) => { input.value = v ?? ""; }; get = () => input.value.trim();
  }
  set(value);
  if (spec.type !== "bool") input.addEventListener("change", emit);
  const node = spec.type === "bool"
    ? el("div", { class: "ui-row pb-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, spec.label)), input)
    : el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, spec.label), input, spec.hint && spec.type !== "chips" ? el("span", { class: "ui-hint" }, spec.hint) : null);
  return { node, get, set, input };
}

function chipsPick(options, selected, label) {
  const picked = new Set(selected);
  const wrap = el("div", { class: "pb-chips", role: "group", "aria-label": label });
  for (const [value, text] of options) {
    const b = el("button", { type: "button", class: "pb-chip" + (picked.has(value) ? " is-on" : ""), "aria-pressed": String(picked.has(value)) }, text);
    b.addEventListener("click", () => {
      if (picked.has(value)) picked.delete(value); else picked.add(value);
      b.classList.toggle("is-on", picked.has(value)); b.setAttribute("aria-pressed", String(picked.has(value)));
    });
    wrap.append(b);
  }
  return { node: wrap, get: () => options.map(([v]) => v).filter((v) => picked.has(v)) };
}

const blankStep = (type = "notify") => ({ type, on_error: "stop" });

// ---- the editor ----------------------------------------------------------------------------------------------
async function openEditor(data, existing, onSaved) {
  let tools = [];
  try { tools = (await api("/api/playbooks/tools")).tools || []; } catch (_) { /* the picker stays empty */ }
  const toolByName = Object.fromEntries(tools.map((t) => [t.name, t]));
  const pb = existing ? JSON.parse(JSON.stringify(existing)) : { name: "", enabled: false, trigger: { type: "alert" }, match: "all", conditions: [], window: null, presence: [], steps: [blankStep()], limits: { ...data.limits }, notes: "" };
  const trigOf = (name) => data.triggers.find((t) => t.name === name) || data.triggers[0];

  const name = field({ type: "text", label: "Name" }, pb.name);
  const trigSel = field({ type: "select", label: "When this happens", options: data.triggers.map((t) => [t.name, t.label]) }, pb.trigger.type);
  const trigBox = el("div", { class: "ui-stack" });
  const trigNote = el("p", { class: "ui-hint pb-fields" });
  let trigFields = [];
  function drawTrigger(initial) {
    const t = trigOf(trigSel.get());
    const vals = initial || {};
    trigFields = t.schema.map((spec) => ({ key: spec.key, f: field(spec, spec.type === "chips" ? vals[spec.key] : vals[spec.key] ?? (spec.type === "select" ? spec.options?.[0]?.[0] : "")) }));
    trigBox.replaceChildren(...trigFields.map((x) => x.f.node));
    const names = t.fields.map(([k]) => `{event.${k}}`).join("  ");
    trigNote.textContent = (t.untrusted ? "Text in this event comes from outside, so steps that change things are refused. " : "") + (names ? `Event fields: ${names}` : "");
  }
  trigSel.input.addEventListener("change", () => { drawTrigger(); drawConds(); drawSteps(); });

  // conditions
  const matchSel = field({ type: "select", label: "Run when", options: [["all", "all of these are true"], ["any", "any of these is true"]] }, pb.match);
  const condBox = el("div", { class: "ui-stack" });
  function drawConds() {
    const t = trigOf(trigSel.get());
    const known = t.fields.map(([k, l]) => [k, l]);
    condBox.replaceChildren(...pb.conditions.map((c, i) => {
      const fSel = el("input", { class: "ui-input", type: "text", list: "pb-fields-" + i, "aria-label": "Event field", value: c.field, maxlength: "30", placeholder: "field" });
      const dl = el("datalist", { id: "pb-fields-" + i }, ...known.map(([k, l]) => el("option", { value: k }, l)));
      const op = el("select", { class: "ui-input", "aria-label": "Test" }, ...data.ops.map(([v, t2]) => el("option", { value: v }, t2)));
      op.value = c.op || "contains";
      const val = el("input", { class: "ui-input", type: "text", "aria-label": "Value", value: c.value || "", maxlength: "200", placeholder: "value" });
      fSel.addEventListener("input", () => { c.field = fSel.value.trim().toLowerCase(); });
      op.addEventListener("change", () => { c.op = op.value; val.hidden = op.value === "is_set"; });
      val.addEventListener("input", () => { c.value = val.value; });
      val.hidden = op.value === "is_set";
      c.op = op.value;
      return el("div", { class: "pb-cond" }, fSel, dl, op, val,
        ui.button({ label: "Remove condition", icon: "trash", variant: "ghost", size: "sm", iconOnly: true, onClick: () => { pb.conditions.splice(i, 1); drawConds(); } }));
    }));
    if (!pb.conditions.length) condBox.append(el("p", { class: "ui-hint" }, "No conditions: every matching event runs it."));
  }

  // time window and presence
  const win = pb.window || { days: [0, 1, 2, 3, 4], start: "09:00", end: "18:00" };
  const winOn = field({ type: "bool", label: "Only inside a time window" }, !!pb.window);
  const winDays = chipsPick(DAYS.map(([v, t]) => [v, t]), win.days, "Days");
  const winStart = el("input", { class: "ui-input", type: "time", value: win.start, "aria-label": "From" });
  const winEnd = el("input", { class: "ui-input", type: "time", value: win.end, "aria-label": "To" });
  const winBox = el("div", { class: "ui-stack pb-win" }, winDays.node, el("div", { class: "pb-twin" }, winStart, el("span", {}, "to"), winEnd));
  winBox.hidden = !pb.window;
  winOn.input.input.addEventListener("change", () => { winBox.hidden = !winOn.get(); });
  const presence = chipsPick(data.presence_states.map((s) => [s, s.replace("_", " ")]), pb.presence || [], "Only while I am");

  // steps
  const stepBox = el("div", { class: "ui-stack" });
  function stepTypes() { return data.step_types; }
  function drawSteps() {
    stepBox.replaceChildren(...pb.steps.map((st, i) => stepCard(st, i)));
  }
  function stepCard(st, i) {
    const spec = stepTypes().find((s) => s.type === st.type) || stepTypes()[0];
    const typeSel = el("select", { class: "ui-input", "aria-label": `Step ${i + 1} type` }, ...stepTypes().map((s) => el("option", { value: s.type }, s.label)));
    typeSel.value = st.type;
    typeSel.addEventListener("change", () => { pb.steps[i] = blankStep(typeSel.value); drawSteps(); });
    const body = el("div", { class: "ui-stack" });
    const bind = (fspec, value) => {
      const f = field(fspec, value, () => { st[fspec.key] = f.get(); });
      st[fspec.key] = f.get();
      return f;
    };
    for (const fs of spec.fields) {
      if (fs.type === "tool") {
        const sel = el("select", { class: "ui-input", "aria-label": "Tool" }, el("option", { value: "" }, "Pick a tool..."),
          ...tools.map((t) => el("option", { value: t.name }, t.name + (t.asks ? " (asks you)" : ""))));
        sel.value = st.tool || "";
        sel.addEventListener("change", () => { st.tool = sel.value; st.args = {}; drawSteps(); });
        body.append(el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, fs.label), sel,
          st.tool && toolByName[st.tool] ? el("span", { class: "ui-hint" }, toolByName[st.tool].description) : null));
      } else if (fs.type === "args") {
        const t = toolByName[st.tool];
        if (!t) continue;
        st.args = st.args || {};
        for (const [key, p] of Object.entries(t.props)) {
          const kind = p.enum ? { type: "select", options: [["", "(not set)"], ...p.enum.map((v) => [String(v), String(v)])] }
            : p.type === "boolean" ? { type: "select", options: [["", "(not set)"], ["true", "yes"], ["false", "no"]] }
            : p.type === "integer" || p.type === "number" ? { type: "number" } : p.type === "array" ? { type: "chips" } : { type: "text" };
          const cur = st.args[key];
          const f = field({ ...kind, label: key + (t.required.includes(key) ? " (needed)" : ""), hint: p.description }, cur === undefined ? "" : (typeof cur === "boolean" ? String(cur) : cur),
            () => {
              let v = f.get();
              if (p.type === "boolean") v = v === "" ? undefined : v === "true";
              if (v === "" || v === null || (Array.isArray(v) && !v.length)) delete st.args[key]; else st.args[key] = v;
            });
          if (cur !== undefined) { /* keep */ }
          body.append(f.node);
        }
      } else {
        const cur = st[fs.key];
        const f = bind(fs, cur === undefined ? (fs.type === "select" ? fs.options?.[0]?.[0] : fs.type === "bool" ? false : "") : cur);
        body.append(f.node);
      }
    }
    const errSel = el("select", { class: "ui-input", "aria-label": `Step ${i + 1} if it fails` },
      el("option", { value: "stop" }, "If it fails: stop the playbook"), el("option", { value: "continue" }, "If it fails: carry on"));
    errSel.value = st.on_error || "stop";
    errSel.addEventListener("change", () => { st.on_error = errSel.value; });
    const move = (d) => { const j = i + d; if (j < 0 || j >= pb.steps.length) return; [pb.steps[i], pb.steps[j]] = [pb.steps[j], pb.steps[i]]; drawSteps(); };
    return el("div", { class: "ui-card pb-step" },
      el("div", { class: "pb-step__head" }, el("b", { class: "pb-step__n" }, `Step ${i + 1}`), typeSel,
        el("span", { class: "pb-step__tools" },
          ui.button({ label: "Move up", icon: "arrow-up", variant: "ghost", size: "sm", iconOnly: true, onClick: () => move(-1) }),
          ui.button({ label: "Move down", icon: "arrow-down", variant: "ghost", size: "sm", iconOnly: true, onClick: () => move(1) }),
          ui.button({ label: "Remove step", icon: "trash", variant: "ghost", size: "sm", iconOnly: true, onClick: () => { pb.steps.splice(i, 1); drawSteps(); } }))),
      body, errSel);
  }

  // limits and notes
  const lim = pb.limits || data.limits;
  const perDay = field({ type: "number", label: "At most this many runs a day", min: 1, max: 500 }, lim.max_per_day);
  const cool = field({ type: "number", label: "Wait this many minutes between runs", min: 0, max: 1440 }, lim.cooldown_min);
  const tout = field({ type: "number", label: "Give up after this many seconds", min: 10, max: 3600 }, lim.timeout_s);
  const notes = field({ type: "textarea", label: "Notes (for you)" }, pb.notes);

  drawTrigger(pb.trigger);
  drawConds();
  drawSteps();

  function collect() {
    const trig = { type: trigSel.get() };
    for (const { key, f } of trigFields) {
      const v = f.get();
      if (v === "" || v === null || v === false || (Array.isArray(v) && !v.length)) continue;
      trig[key] = v;
    }
    return { name: name.get(), trigger: trig, match: matchSel.get(),
      conditions: pb.conditions.filter((c) => c.field).map((c) => ({ field: c.field, op: c.op, value: c.op === "is_set" ? "" : (c.value || "") })),
      window: winOn.get() ? { days: winDays.get(), start: winStart.value, end: winEnd.value } : null,
      presence: presence.get(), steps: pb.steps.map((s) => { const out = { ...s }; if (out.type === "notify") { out.speak = !!out.speak; } return out; }),
      limits: { max_per_day: perDay.get() ?? 20, cooldown_min: cool.get() ?? 0, timeout_s: tout.get() ?? 900 }, notes: notes.get() };
  }

  const heading = (t, hint) => el("div", { class: "pb-h" }, el("h3", {}, t), hint ? el("p", { class: "ui-hint" }, hint) : null);
  const content = el("div", { class: "ui-stack ui-stack--lg pb-editor" },
    name.node,
    el("section", { class: "ui-stack" }, heading("1. When", "Pick what starts it."), trigSel.node, trigBox, trigNote),
    el("section", { class: "ui-stack" }, heading("2. Only if", "Optional: narrow it down."), matchSel.node, condBox,
      ui.button({ label: "Add a condition", icon: "plus", variant: "tonal", size: "sm", onClick: () => { pb.conditions.push({ field: "text", op: "contains", value: "" }); drawConds(); } }),
      winOn.node, winBox, el("p", { class: "ui-label" }, "Only while I am (leave empty for anywhere)"), presence.node),
    el("section", { class: "ui-stack" }, heading("3. Then do", `Up to ${data.max_steps} steps, in order. Use {event.sender} or {step1.output} in text: it is inserted as plain data.`), stepBox,
      ui.button({ label: "Add a step", icon: "plus", variant: "tonal", size: "sm", onClick: () => { if (pb.steps.length >= data.max_steps) return fail({ message: `At most ${data.max_steps} steps.` }); pb.steps.push(blankStep()); drawSteps(); } }),
      ui.notice("Anything that changes something asks you first, exactly as it does in chat. Steps never run shell commands.", { icon: "shield" })),
    el("section", { class: "ui-stack" }, heading("4. Limits"), perDay.node, cool.node, tout.node, notes.node));

  const sheet = ui.sheet({ title: existing ? "Edit playbook" : "New playbook", class: "pb-sheet", content,
    actions: [
      { label: "Save", variant: "primary", icon: "check", onClick: async () => {
        try {
          const body = collect();
          const saved = existing ? await api(`/api/playbooks/${existing.id}`, { method: "PUT", json: body }) : await api("/api/playbooks", { method: "POST", json: { ...body, enabled: false } });
          ui.toast(existing ? "Saved." : "Saved. It is off until you turn it on.", { kind: "good" });
          onSaved?.(saved);
        } catch (e) { fail(e); return false; }
      } },
      { label: "Save and test", variant: "tonal", icon: "play", onClick: async () => {
        try {
          const body = collect();
          const saved = existing ? await api(`/api/playbooks/${existing.id}`, { method: "PUT", json: body }) : await api("/api/playbooks", { method: "POST", json: { ...body, enabled: false } });
          onSaved?.(saved, { test: true });
        } catch (e) { fail(e); return false; }
      } }] });
  return sheet;
}

// ---- run detail ----------------------------------------------------------------------------------------------------------
function statusBadge(table, key) { const [t, tone] = table[key] || [key, "gray"]; return ui.badge(t, tone); }

export function runCard(run, { reload, closeable } = {}) {
  const box = el("div", { class: "ui-stack pb-run" });
  box.append(el("div", { class: "pb-badges" }, statusBadge(STATUS, run.status), run.dry ? ui.badge("Test: nothing was done", "accent") : null,
    run.tainted ? ui.badge("Outside text: changes refused", "warn") : null, run.rerun_of ? ui.badge("Re-run", "gray") : null),
    el("p", { class: "ui-hint" }, `${run.trigger} · ${when(run.started)}${run.source && run.source !== "trigger" ? " · " + run.source : ""}`),
    run.event_summary ? el("p", { class: "pb-event" }, run.event_summary) : null,
    run.reason ? ui.notice(run.reason, { tone: run.status === "failed" ? "bad" : undefined }) : null);
  const list = el("ol", { class: "pb-steps" });
  for (const s of run.steps || []) {
    list.append(el("li", { class: "pb-steprow" }, el("div", { class: "pb-badges" }, el("b", {}, `${s.n}. ${s.label}`), statusBadge(STEP_STATUS, s.status), s.level ? ui.badge(s.level, "gray") : null),
      s.reason && s.status !== "ok" ? el("p", { class: "ui-hint pb-why" }, s.reason) : null, s.output ? el("p", { class: "pb-out" }, s.output) : null));
  }
  box.append(list);
  for (const a of run.approvals || []) {
    box.append(el("p", { class: "ui-hint" }, `Asked: ${a.tool} (${a.level}) · ${a.state}`));
  }
  const acts = el("div", { class: "pb-actions" });
  if (run.status === "running" || run.status === "waiting") {
    acts.append(ui.button({ label: "Cancel run", icon: "stop", variant: "danger", size: "sm", onClick: async () => { try { await api(`/api/playbooks/runs/${run.id}/cancel`, { method: "POST" }); ui.toast("Cancelling.", { kind: "good" }); reload?.(); } catch (e) { fail(e); } } }));
  } else if (!run.dry && run.status !== "skipped") {
    acts.append(ui.button({ label: "Run again", icon: "refresh", variant: "tonal", size: "sm", onClick: async () => { try { await api(`/api/playbooks/runs/${run.id}/rerun`, { method: "POST" }); ui.toast("Started again.", { kind: "good" }); reload?.(); } catch (e) { fail(e); } } }));
  }
  if (acts.children.length) box.append(acts);
  return box;
}

async function openRun(runId, reload) {
  const body = el("div", { class: "ui-stack" }, ui.skeleton("list", 3));
  const sheet = ui.sheet({ title: "Run", content: body, class: "pb-sheet" });
  async function paint() {
    try {
      const run = await api(`/api/playbooks/runs/${runId}`);
      sheet.setTitle(run.name);
      body.replaceChildren(runCard(run, { reload: () => { paint(); reload?.(); } }));
    } catch (e) { body.replaceChildren(ui.notice(e.message || "Couldn't load.", { tone: "bad" })); }
  }
  paint();
}

// ---- approvals ----------------------------------------------------------------------------------------------------------------
function approvalCard(a, reload) {
  const confirm = a.critical ? el("input", { class: "ui-input", type: "text", placeholder: "Type CONFIRM", "aria-label": "Type CONFIRM", autocomplete: "off" }) : null;
  const ans = async (approve) => {
    try {
      await api(`/api/playbooks/approvals/${a.call_id}`, { method: "POST", json: { run_id: a.run_id, approve, confirm_text: confirm ? confirm.value.trim() : "" } });
      reload();
    } catch (e) { fail(e); }
  };
  return el("div", { class: "ui-card pb-approval" },
    el("p", { class: "ui-card__title" }, `${a.playbook} wants to run ${a.tool}`),
    el("p", { class: "ui-card__sub" }, `${a.level}${a.critical ? " critical" : ""} · step ${a.step}`),
    el("pre", { class: "pb-args" }, JSON.stringify(a.input || {}, null, 1).slice(0, 1200)), confirm,
    el("div", { class: "pb-actions" }, ui.button({ label: "Approve", icon: "check", variant: "primary", size: "sm", onClick: () => ans(true) }),
      ui.button({ label: "Deny", icon: "x", variant: "secondary", size: "sm", onClick: () => ans(false) })));
}

// ---- the screen -------------------------------------------------------------------------------------------------------------------
export function init(container, ctx) {
  loadCss();
  const root = el("div", { class: "pb ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, runs = [], timer = null;

  const masterSwitch = ui.switchControl({ checked: true, label: "Playbooks on or off", onChange: async (on) => {
    try { await api("/api/playbooks/settings", { method: "PUT", json: { enabled: on } }); load(); } catch (e) { masterSwitch.input.checked = !on; fail(e); }
  } });
  const approvals = el("div", { class: "ui-stack" });
  const listBox = el("div", {});
  const runsBox = el("div", { class: "ui-list ui-list--inset" });
  const ed = listEditor({ title: "Your playbooks", hint: "Each one connects an event to steps. They start off; turn on the ones you want.",
    rowOf: (p) => ({ icon: p.example ? "sparkles" : "zap", tone: p.running ? "accent" : "accent", title: p.name,
      sub: [p.trigger_label, `${p.steps.length} step${p.steps.length === 1 ? "" : "s"}`, p.last_run ? `last: ${(STATUS[p.last_run.status] || [p.last_run.status])[0]}` : "never run",
        p.next_run ? `next ${when(p.next_run)}` : null, p.example ? "example" : null].filter(Boolean).join(" · "), enabled: !!p.enabled }),
    onToggle: async (p, on) => { await api(`/api/playbooks/${p.id}/enable`, { method: "POST", json: { enabled: on } }); load(); },
    onEdit: (p) => openPlaybook(p), onDelete: async (p) => { await api(`/api/playbooks/${p.id}`, { method: "DELETE" }); load(); },
    onAdd: () => edit(null), addLabel: "New playbook", empty: "No playbooks yet. Start from a template, or write your own." });
  const tplBtn = ui.button({ label: "Start from a template", icon: "sparkles", variant: "tonal", size: "sm", onClick: () => openTemplates() });
  ed.el.querySelector(".le-foot")?.append(tplBtn);
  const observer = new MutationObserver(() => { const foot = ed.el.querySelector(".le-foot"); if (foot && !foot.contains(tplBtn)) foot.append(tplBtn); });
  observer.observe(ed.el, { childList: true, subtree: true });

  root.append(
    el("div", { class: "ui-row pb-master" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Playbooks"),
      el("span", { class: "ui-row__sub" }, "When something happens, do something. Changes still ask you first.")), masterSwitch),
    approvals, ed.el,
    el("section", { class: "le st-group" }, el("h2", { class: "ui-caps st-caps" }, "Recent runs"), runsBox));

  function openPlaybook(p) {
    const body = el("div", { class: "ui-stack" });
    const sheet = ui.sheet({ title: p.name, class: "pb-sheet", content: body });
    async function paint() {
      let rows = [];
      try { rows = (await api(`/api/playbooks/runs?playbook=${encodeURIComponent(p.id)}&limit=15`)).runs; } catch (_) { /* empty */ }
      body.replaceChildren(
        el("p", { class: "ui-hint" }, p.notes || ""),
        el("div", { class: "pb-actions" },
          ui.button({ label: "Run now", icon: "play", variant: "primary", size: "sm", onClick: async () => { try { await api(`/api/playbooks/${p.id}/run`, { method: "POST" }); ui.toast("Started.", { kind: "good" }); setTimeout(paint, 500); load(); } catch (e) { fail(e); } } }),
          ui.button({ label: "Test with a sample event", icon: "check-circle", variant: "tonal", size: "sm", onClick: () => test(p) }),
          ui.button({ label: "Edit", icon: "edit", variant: "secondary", size: "sm", onClick: () => { sheet.close(); edit(p); } }),
          p.trigger.type === "webhook" ? ui.button({ label: p.webhook?.set ? "New webhook secret" : "Make webhook secret", icon: "key", variant: "secondary", size: "sm", onClick: () => webhook(p) }) : null),
        el("h3", { class: "pb-h" }, "History"),
        rows.length ? el("div", { class: "ui-list ui-list--inset" }, ...rows.map(runRow)) : ui.emptyState({ icon: "history", title: "No runs yet", compact: true }));
    }
    paint();
  }

  async function test(p) {
    try {
      const run = await api(`/api/playbooks/${p.id}/test`, { method: "POST", json: {} });
      const sheet = ui.sheet({ title: "Test: " + p.name, class: "pb-sheet", content: [ui.notice("This was a test with a sample event. Nothing was sent, saved or run.", { icon: "info" }), runCard(run)] });
      void sheet;
      load();
    } catch (e) { fail(e); }
  }

  async function webhook(p) {
    try {
      const r = await api(`/api/playbooks/${p.id}/webhook`, { method: "POST" });
      ui.sheet({ title: "Webhook secret", class: "pb-sheet", content: el("div", { class: "ui-stack" },
        ui.notice("Copy this now: it is shown once.", { tone: "warn", icon: "key" }),
        el("pre", { class: "pb-args" }, r.secret), el("p", { class: "ui-hint" }, `POST to ${location.origin}${r.path}`), el("p", { class: "ui-hint" }, r.how)) });
      load();
    } catch (e) { fail(e); }
  }

  function edit(p) {
    openEditor(data, p, (saved, opt) => { load(); if (opt?.test) test(saved); });
  }

  function openTemplates() {
    const list = el("div", { class: "ui-list ui-list--inset" }, ...data.templates.map((t) => ui.listRow({ icon: "sparkles", tone: "accent", title: t.name,
      sub: t.about || "", meta: t.added ? "Added" : null, onClick: async () => {
        try { await api(`/api/playbooks/templates/${t.key}/use`, { method: "POST" }); ui.toast("Added, switched off. Open it to edit, then turn it on.", { kind: "good" }); sheet.close(); load(); } catch (e) { fail(e); }
      } })));
    const sheet = ui.sheet({ title: "Start from a template", class: "pb-sheet", content: el("div", { class: "ui-stack" },
      ui.notice("These are examples. Each is added switched off so you can read and change it first.", { icon: "info" }), list) });
  }

  function runRow(r) {
    return ui.listRow({ icon: r.status === "failed" ? "alert" : r.status === "waiting" ? "clock" : r.dry ? "check-circle" : "zap", tone: (STATUS[r.status] || [])[1] || "gray",
      title: r.name + (r.dry ? " (test)" : ""), sub: `${(STATUS[r.status] || [r.status])[0]} · ${r.event_summary || r.trigger}`, meta: when(r.started).slice(5),
      onClick: () => openRun(r.id, load) });
  }

  async function load() {
    try {
      data = await api("/api/playbooks");
      runs = (await api("/api/playbooks/runs?limit=20")).runs || [];
      masterSwitch.input.checked = data.enabled !== false;
      ed.set(data.playbooks);
      approvals.replaceChildren(...(data.paused ? [ui.notice("Panic is on: every playbook is paused until you tap Resume.", { tone: "warn", icon: "stop" })] : []),
        ...(data.pending || []).map((a) => approvalCard(a, load)));
      runsBox.replaceChildren(...(runs.length ? runs.map(runRow) : [el("p", { class: "ui-hint le-empty" }, "Nothing has run yet.")]));
      const live = runs.some((r) => r.status === "running" || r.status === "waiting");
      clearTimeout(timer);
      timer = setTimeout(() => { if (root.isConnected) load(); }, live ? 3000 : 20000);
    } catch (e) { root.append(ui.notice(e.message || "Couldn't load playbooks.", { tone: "bad" })); }
  }
  load();
  return { refresh: load, destroy() { clearTimeout(timer); observer.disconnect(); } };
}
