// Settings lists the owner edits: Assistants, Notification preferences, My safety rules, My redaction rules and Memory kinds.
// Each is one sheet built from the shared list editor (js/listeditor.js). The ready-made behaviour is always the first
// entry or the default; nothing here is hard-coded to Jarvis, Friday or a fixed set of types.
// Routes: docs/api/assistants.md, docs/api/notify.md (prefs), docs/api/assistant-safety.md, docs/api/privacy-usage.md, docs/api/brain.md.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";
import { forgetAssistants, refreshAssistants } from "./assistants.js";
import { forgetLanes, savedTile } from "./lanepicker.js";
import { openVoices } from "./voices.js";

const good = (text) => ui.toast(text, { kind: "good" });
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TONE = { cyan: "accent", amber: "orange", green: "good", violet: "violet", white: "gray" };
const open = (title, ...content) => ui.sheet({ title, class: "st-sheet st-sheet--tall", content: el("div", { class: "ui-stack ui-stack--lg" }, ...content) });

// ---- Assistants -------------------------------------------------------------------------------------------
export async function openAssistants() {
  let data = await api("/api/assistants").catch((e) => { fail(e); return null; });
  if (!data) return;
  const groups = data.tool_groups || [];
  const accents = (data.accents || ["cyan"]).map((a) => [a, a[0].toUpperCase() + a.slice(1)]);
  const apply = (next) => { data = next; ed.set(data.assistants); forgetAssistants(); refreshAssistants(true).catch(() => {}); };

  const fields = (isNew, a) => [
    ...(isNew ? [{ key: "id", label: "Id", type: "text", maxlength: 24, placeholder: "atlas", hint: "Lower-case letters and digits. It never changes once saved." }] : []),
    { key: "name", label: "Display name", type: "text", maxlength: 40 },
    { key: "wake_names", label: "Wake names", type: "chips", hint: 'Say "Hey <name>". One word each, separated by commas.' },
    { key: "heard_as", label: "Mis-hearings", type: "chips", hint: "How the microphone often mis-hears the name (jarves for jarvis)." },
    { key: "role_prompt", label: "Role prompt", type: "textarea", rows: 5, maxlength: 1500,
      hint: a?.builtin ? "Empty keeps the built-in prompt. You may use {name} and {call}." : "What this assistant is for. You may use {name} and {call}.",
      placeholder: a?.default_prompt || "" },
    { key: "accent", label: "Colour", type: "select", options: accents },
    { key: "rate", label: "Voice speed (0.5 to 2)", type: "number", min: 0.5, max: 2, step: 0.1 },
    { key: "pitch", label: "Voice pitch (0 to 2)", type: "number", min: 0, max: 2, step: 0.1 },
    { key: "voice", label: "Voice name (optional)", type: "text", maxlength: 80, hint: "A voice installed on the phone; empty uses the default." },
    { key: "tool_groups", label: "Tool focus", type: "chips", hint: `Areas it always has tools for. Empty = no special focus. Known: ${groups.join(", ")}.` },
    { key: "enabled", label: "On", type: "switch" },
  ];
  const toForm = (a) => ({ ...a, rate: a.voice?.rate ?? 1, pitch: a.voice?.pitch ?? 1, voice: a.voice?.voice || "" });
  const fromForm = (v) => ({ ...(v.id ? { id: v.id } : {}), name: v.name, wake_names: v.wake_names, heard_as: v.heard_as, role_prompt: v.role_prompt,
    accent: v.accent, tool_groups: v.tool_groups, enabled: v.enabled, voice: { rate: v.rate ?? 1, pitch: v.pitch ?? 1, voice: v.voice || "" } });

  const ed = listEditor({
    title: "Your assistants", items: data.assistants,
    hint: "Jarvis and Friday are ready-made. Rename them, add your own (for example Atlas for study) or switch one off. At least one stays on.",
    addLabel: "Add assistant", resetLabel: "Back to Jarvis and Friday", empty: "No assistants.",
    rowOf: (a) => ({ icon: a.builtin ? "jarvis" : "bot", tone: TONE[a.accent] || "accent", title: a.name, enabled: a.enabled,
      sub: `Wakes on: ${(a.words || a.wake_names).join(", ")}${a.tool_groups?.length ? " · Focus: " + a.tool_groups.join(", ") : ""}${a.role_prompt ? " · Custom prompt" : ""}`,
      meta: el("span", {}, el("span", { class: "le-accent", "data-accent": a.accent }), a.id) }),
    onToggle: async (a, on) => apply(await api(`/api/assistants/${a.id}`, { method: "PATCH", json: { enabled: on } })),
    onEdit: (a) => formSheet({ title: `Edit ${a.name}`, fields: fields(false, a), values: toForm(a), tall: true,
      onSave: async (v) => { apply(await api(`/api/assistants/${a.id}`, { method: "PATCH", json: fromForm(v) })); good("Saved."); } }),
    onAdd: () => formSheet({ title: "New assistant", fields: fields(true, null), tall: true,
      values: { accent: "green", enabled: true, rate: 1, pitch: 1 },
      onSave: async (v) => { apply(await api("/api/assistants", { method: "POST", json: fromForm(v) })); good("Added."); } }),
    onDelete: async (a) => { apply(await api(`/api/assistants/${a.id}`, { method: "DELETE" })); good("Deleted."); },
    onReset: async () => { apply(await api("/api/assistants/reset", { method: "POST" })); good("Back to the defaults."); },
  });
  open("Assistants", ed.el, ui.notice("Wake words work while the app is open, and on the laptop microphone if you turned that on. The always-on phone listener (Android app) still knows only the words it was built with.", { tone: "info" }));
}

// ---- Notification preferences -------------------------------------------------------------------------------
const CHANNEL_LABEL = { now: "Now", batch: "Batch", "quiet-only": "Quiet only", off: "Off" };
export async function openNotifyPrefs() {
  let data = await api("/api/proactive/prefs").catch((e) => { fail(e); return null; });
  if (!data) return;
  const save = async (type, value) => { data = await api("/api/proactive/prefs", { method: "PUT", json: { prefs: { [type]: value } } }); ed.set(data.types); };
  const picker = (t) => {
    const sel = el("select", { class: "ui-input le-select", "aria-label": `${t.label}: how it reaches you` },
      el("option", { value: "default" }, `Default (${CHANNEL_LABEL[t.default] || "smart"})`),
      ...data.channels.map((c) => el("option", { value: c }, CHANNEL_LABEL[c] || c)));
    sel.value = t.value;
    sel.addEventListener("change", async () => { try { await save(t.type, sel.value); } catch (e) { fail(e); sel.value = t.value; } });
    return sel;
  };
  const ed = listEditor({
    title: "How each thing reaches you", items: data.types, resetLabel: "Reset all to defaults",
    hint: "Now = push at once. Batch = one short summary every half hour. Quiet only = listed in the app, never pushed. Off = dropped. Quiet hours still hold everything back except meetings and reminders.",
    rowOf: (t) => ({ icon: "bell", tone: t.custom ? "accent" : "gray", title: t.label, sub: t.type, meta: picker(t) }),
    onReset: async () => { data = await api("/api/proactive/prefs/reset", { method: "POST" }); ed.set(data.types); good("Back to the defaults."); },
  });
  const start = el("input", { class: "ui-input", type: "time", value: data.quiet.start, "aria-label": "Quiet hours start" });
  const end = el("input", { class: "ui-input", type: "time", value: data.quiet.end, "aria-label": "Quiet hours end" });
  const saveQuiet = ui.button({ label: "Save quiet hours", icon: "check", variant: "tonal", onClick: () => ui.busy(saveQuiet, async () => {
    try { await api("/api/settings", { method: "PATCH", json: { proactive_quiet_start: start.value, proactive_quiet_end: end.value } }); good("Quiet hours saved."); } catch (e) { fail(e); }
  }) });
  open("Notification preferences", ed.el,
    el("section", { class: "ui-card ui-stack" }, ui.eyebrow("QH", "Quiet hours"),
      el("div", { class: "st-times" }, el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "From"), start),
        el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "To"), end)), saveQuiet));
}

// ---- My safety rules ----------------------------------------------------------------------------------------
export async function openSafetyRules() {
  let data = await api("/api/safety/rules").catch((e) => { fail(e); return null; });
  if (!data) return;
  const put = async (rules) => { data = await api("/api/safety/rules", { method: "PUT", json: { rules } }); ed.set(data.rules); };
  const fields = [
    { key: "pattern", label: "Pattern", type: "text", maxlength: 200, hint: "Plain text matches anywhere (D:\\payroll). Turn on regex for a pattern." },
    { key: "regex", label: "Pattern is a regular expression", type: "switch", default: false },
    { key: "applies_to", label: "Applies to", type: "select", options: [["command", "A command line"], ["path", "A file path (also checks commands)"], ["tool", "A tool name"]] },
    { key: "level", label: "Make it", type: "select", options: [["N3", "N3 Critical: type CONFIRM, then a countdown"], ["N2", "N2 Ask: the normal approval card"]],
      hint: "A rule can only make something stricter, never looser." },
    { key: "note", label: "Note (shown on the card)", type: "text", maxlength: 200 },
    { key: "enabled", label: "On", type: "switch" },
  ];
  const edit = (rule, index) => formSheet({ title: rule ? "Edit rule" : "New safety rule", fields, values: rule || { applies_to: "path", level: "N3", enabled: true, regex: false },
    onSave: async (v) => { const next = [...data.rules]; if (index == null) next.push(v); else next[index] = v; await put(next); good("Saved."); } });
  const ed = listEditor({
    title: "Rules that make things stricter", items: data.rules, addLabel: "Add rule", resetLabel: "Remove all my rules", empty: "No rules yet. The built-in safety checks always apply.",
    hint: "Examples: path D:\\payroll is critical; command containing deploy asks first; tool send_ asks first.",
    rowOf: (r) => ({ icon: "shield", tone: r.level === "N3" ? "bad" : "orange", title: r.pattern, enabled: r.enabled,
      sub: `${r.applies_to}${r.regex ? " · regex" : ""}${r.note ? " · " + r.note : ""}`, meta: ui.badge(r.level === "N3" ? "Critical" : "Ask", r.level === "N3" ? "bad" : "warn") }),
    onToggle: async (r, on) => put(data.rules.map((x) => (x === r || x.id === r.id ? { ...x, enabled: on } : x))),
    onEdit: (r) => edit(r, data.rules.findIndex((x) => x.id === r.id)),
    onAdd: () => edit(null, null),
    onDelete: async (r) => { await put(data.rules.filter((x) => x.id !== r.id)); good("Deleted."); },
    onReset: async () => { await put([]); good("Rules removed."); },
  });
  const kind = el("select", { class: "ui-input", "aria-label": "What to test" },
    el("option", { value: "command" }, "Command"), el("option", { value: "path" }, "File path"), el("option", { value: "tool" }, "Tool name"));
  const text = el("input", { class: "ui-input", type: "text", maxlength: "2000", placeholder: "Type a command to see its level", "aria-label": "Text to test" });
  const out = el("p", { class: "ui-hint", "aria-live": "polite" }, "");
  const go = ui.button({ label: "Test", icon: "shield", variant: "secondary", onClick: () => ui.busy(go, async () => {
    try {
      const r = await api("/api/safety/rules/test", { method: "POST", json: { kind: kind.value, text: text.value } });
      const word = { N1: "N1: runs freely", N2: "N2: asks first", N3: "N3: critical (typed CONFIRM, then a countdown)" }[r.level] || r.level;
      out.textContent = `${word}.${r.rules.length ? " Your rule(s) fired: " + r.rules.map((x) => x.pattern).join(", ") + "." : ""}${r.built_in !== r.level ? ` Without your rules: ${r.built_in}.` : ""}`;
    } catch (e) { fail(e); }
  }) });
  open("My safety rules", ed.el, el("section", { class: "ui-card ui-stack" }, ui.eyebrow("TS", "Test"), el("div", { class: "ui-row" }, kind), text, go, out));
}

// ---- My redaction rules -------------------------------------------------------------------------------------
export async function openRedactRules() {
  let data = await api("/api/redact/rules").catch((e) => { fail(e); return null; });
  if (!data) return;
  const put = async (rules) => { data = await api("/api/redact/rules", { method: "PUT", json: { rules } }); ed.set(data.rules); };
  const fields = [
    { key: "label", label: "Label", type: "text", maxlength: 40, placeholder: "Client names" },
    { key: "pattern", label: "Words or pattern", type: "textarea", rows: 3, maxlength: 200, hint: "Plain words, one per line or comma-separated (whole-word, any capitalisation). Or turn on regex." },
    { key: "regex", label: "Pattern is a regular expression", type: "switch", default: false },
    { key: "kind", label: "Placeholder", type: "text", maxlength: 16, placeholder: "CLIENT", hint: "Shown to the cloud AI as [CLIENT_1]. Capital letters and digits." },
    { key: "enabled", label: "On", type: "switch" },
  ];
  const edit = (rule, index) => formSheet({ title: rule ? "Edit rule" : "New redaction rule", fields, values: rule || { regex: false, enabled: true },
    onSave: async (v) => { const next = [...data.rules]; if (index == null) next.push(v); else next[index] = v; await put(next); good("Saved."); } });
  const ed = listEditor({
    title: "Words cloud AI must never see", items: data.rules, addLabel: "Add rule", resetLabel: "Remove all my rules", empty: "No rules yet. The built-in kinds (emails, numbers, keys...) still apply.",
    rowOf: (r) => ({ icon: "eye-off", tone: "good", title: r.label, enabled: r.enabled, sub: `[${r.kind}_1]${r.regex ? " · regex" : ""}` }),
    onToggle: async (r, on) => put(data.rules.map((x) => (x.id === r.id ? { ...x, enabled: on } : x))),
    onEdit: (r) => edit(r, data.rules.findIndex((x) => x.id === r.id)),
    onAdd: () => edit(null, null),
    onDelete: async (r) => { await put(data.rules.filter((x) => x.id !== r.id)); good("Deleted."); },
    onReset: async () => { await put([]); good("Rules removed."); },
  });
  const sample = el("textarea", { class: "ui-input", rows: "3", maxlength: "4000", placeholder: "Paste something to see what would be hidden", "aria-label": "Sample text" });
  const out = el("pre", { class: "le-pre", "aria-live": "polite" }, "");
  const counts = el("p", { class: "ui-hint" }, "");
  const go = ui.button({ label: "What will be hidden?", icon: "eye", variant: "secondary", onClick: () => ui.busy(go, async () => {
    try {
      const r = await api("/api/redact/rules/test", { method: "POST", json: { text: sample.value } });
      out.textContent = r.redacted;
      counts.textContent = r.enabled ? (r.total ? `Hidden: ${Object.entries(r.counts).map(([k, v]) => `${v} ${k}`).join(", ")}.` : "Nothing in this text would be hidden.") : "Redaction is switched off.";
    } catch (e) { fail(e); }
  }) });
  open("My redaction rules", ed.el, el("section", { class: "ui-card ui-stack" }, ui.eyebrow("PV", "Preview"), sample, go, out, counts));
}

// ---- Memory kinds -------------------------------------------------------------------------------------------
export async function openMemoryKinds() {
  let data = await api("/api/brain/kinds").catch((e) => { fail(e.status === 404 ? new Error("Memory is switched off (brain_enabled).") : e); return null; });
  if (!data) return;
  const persona = (p) => (p === "both" ? "Both voices" : p);
  const strip = (list) => list.map(({ id, label, persona: p }) => ({ id, label, persona: p }));
  const put = async (kinds) => { data = await api("/api/brain/kinds", { method: "PUT", json: { kinds } }); ed.set(data.kinds); };
  const form = (kind, index) => formSheet({ title: kind ? `Edit ${kind.label}` : "New memory kind",
    fields: [...(kind ? [] : [{ key: "id", label: "Id", type: "text", maxlength: 20, placeholder: "idea" }]),
      { key: "label", label: "Label", type: "text", maxlength: 30 },
      { key: "persona", label: "Who sees it by default", type: "select", options: data.personas.map((p) => [p, persona(p)]),
        hint: "The assistant a new item of this kind belongs to. Both = shared." }],
    values: kind || { persona: "both" },
    onSave: async (v) => { const next = strip(data.kinds); if (index == null) next.push(v); else next[index] = { ...next[index], ...v }; await put(next); good("Saved."); } });
  const ed = listEditor({
    title: "What kinds of memory exist", items: data.kinds, addLabel: "Add kind", resetLabel: "Back to the ready-made kinds",
    hint: "Each item has one kind, plus any number of free tags (edit tags in Memory).",
    rowOf: (k) => ({ icon: "tag", tone: "accent", title: k.label, sub: `${k.id} · default: ${persona(k.persona)} · ${k.count} item${k.count === 1 ? "" : "s"}` }),
    onEdit: (k) => form(k, data.kinds.findIndex((x) => x.id === k.id)),
    onAdd: () => form(null, null),
    canDelete: (k) => k.count === 0,
    onDelete: async (k) => { await put(strip(data.kinds).filter((x) => x.id !== k.id)); good("Deleted."); },
    onReset: async () => { data = await api("/api/brain/kinds/reset", { method: "POST" }); ed.set(data.kinds); good("Back to the defaults."); },
  });
  open("Memory kinds", ed.el);
}

// ---- Work lanes ---------------------------------------------------------------------------------------------
// Two lists: the lanes (where work can go) and the task classes (which lanes, in what order). Routes: docs/api/lanes.md.
const COST_TONE = { free: "good", limited: "orange", paid: "bad" };
const BASE_KINDS = [["council", "Council (free models, background)"], ["local", "Local model"], ["local-reasoning", "Local deep thinking"],
  ["gemini", "Gemini"], ["claude-login", "Claude (my login)"], ["claude", "Claude (API key)"], ["cli", "A CLI login AI"], ["endpoint", "My AI endpoint"]];
export async function openLanes() {
  let data = await api("/api/lanes").catch((e) => { fail(e); return null; });
  if (!data) return;
  const apply = (next) => { data = next; laneEd.set(data.lanes); classEd.set(data.classes); forgetLanes(); };
  const splitKind = (kind) => { const [base, rest] = String(kind || "local").split(":"); return rest !== undefined ? [base, rest] : [kind, ""]; };
  const laneFields = (isNew) => [
    ...(isNew ? [{ key: "id", label: "Id", type: "text", maxlength: 40, placeholder: "my-box", hint: "Lower-case letters, digits and dashes. It never changes once saved." }] : []),
    { key: "label", label: "Name", type: "text", maxlength: 60 },
    { key: "base", label: "Kind", type: "select", options: BASE_KINDS },
    { key: "provider", label: "Provider id (CLI or endpoint kinds only)", type: "text", maxlength: 40, placeholder: "gemini-cli", hint: "The id of the entry in Settings > Providers." },
    { key: "cost", label: "Cost class", type: "select", options: [["free", "Free"], ["limited", "Limited (a quota)"], ["paid", "Paid"]], hint: "A call counts as saved when a free lane gives the accepted answer." },
    { key: "daily_cap", label: "Daily cap (0 = none)", type: "number", min: 0, max: 100000, step: 1, hint: "Past it the lane is skipped. The caps in Privacy and usage apply too." },
    { key: "escalate_claude", label: "Council may ask Claude (council kind only)", type: "switch", default: false, hint: "Still needs council_allow_claude in hub.json." },
    { key: "notes", label: "Notes", type: "textarea", rows: 2, maxlength: 300 },
    { key: "enabled", label: "On", type: "switch" },
  ];
  const laneBody = (v, isNew) => {
    const kind = ["cli", "endpoint"].includes(v.base) ? `${v.base}:${(v.provider || "").trim()}` : v.base;
    return { ...(isNew ? { id: v.id } : {}), label: v.label, kind, cost: v.cost, daily_cap: v.daily_cap ?? 0, notes: v.notes, enabled: v.enabled, escalate_claude: !!v.escalate_claude && v.base === "council" };
  };
  const laneEd = listEditor({
    title: "Lanes", items: data.lanes, addLabel: "Add lane", resetLabel: "Back to the ready-made lanes", empty: "No lanes.",
    hint: "Where work can go. Free lanes (Council, local models) come first so Claude and Gemini are kept for what they alone can do.",
    rowOf: (l) => ({ icon: l.private_safe ? "shield" : "users", tone: COST_TONE[l.cost] || "gray", title: l.label, enabled: l.enabled,
      sub: `${l.kind} · ${l.cost}${l.daily_cap ? ` · ${l.used_today}/${l.daily_cap} today` : ""}${l.enabled && l.why_not ? " · " + l.why_not : ""}${l.auto ? " · from Providers" : ""}`,
      meta: l.id }),
    onToggle: async (l, on) => apply(await api(`/api/lanes/${l.id}`, { method: "PUT", json: { enabled: on } })),
    onEdit: (l) => { const [base, provider] = splitKind(l.kind);
      formSheet({ title: `Edit ${l.label}`, fields: laneFields(false), tall: true, values: { ...l, base, provider },
        onSave: async (v) => { apply(await api(`/api/lanes/${l.id}`, { method: "PUT", json: laneBody(v, false) })); good("Saved."); } }); },
    onAdd: () => formSheet({ title: "New lane", fields: laneFields(true), tall: true, values: { base: "endpoint", cost: "free", daily_cap: 0, enabled: true },
      onSave: async (v) => { apply(await api("/api/lanes", { method: "POST", json: laneBody(v, true) })); good("Added."); } }),
    canDelete: (l) => !l.auto,
    onDelete: async (l) => { apply(await api(`/api/lanes/${l.id}`, { method: "DELETE" })); good("Deleted."); },
    onReset: async () => { apply(await api("/api/lanes/reset", { method: "POST" })); good("Back to the defaults."); },
  });
  const classFields = (isNew) => [
    ...(isNew ? [{ key: "id", label: "Id", type: "text", maxlength: 40, placeholder: "mail-drafts" }] : []),
    { key: "label", label: "Name", type: "text", maxlength: 60 },
    { key: "lanes", label: "Lanes in order (best first)", type: "chips", maxlength: 300, hint: `Lane ids, first is tried first, the next only if it fails. Known: ${data.lanes.map((l) => l.id).join(", ")}.` },
    { key: "private", label: "Private: never leaves the laptop", type: "switch", default: false, hint: "Only Council (local models only) and local lanes can serve it, whatever is listed." },
    { key: "notes", label: "Notes", type: "textarea", rows: 2, maxlength: 300 },
  ];
  const classEd = listEditor({
    title: "Task classes", items: data.classes, addLabel: "Add class", empty: "No classes.",
    hint: "What kind of work it is decides where it goes. Say 'give this to the council' for routine work.",
    rowOf: (c) => ({ icon: c.private ? "shield" : "tag", tone: c.private ? "good" : "accent", title: c.label, sub: c.lanes.join(" → ") || "no lanes", meta: c.private ? "private" : c.id }),
    onEdit: (c) => formSheet({ title: `Edit ${c.label}`, fields: classFields(false), tall: true, values: c,
      onSave: async (v) => { apply(await api(`/api/lanes/classes/${c.id}`, { method: "PUT", json: { label: v.label, lanes: v.lanes, private: v.private, notes: v.notes } })); good("Saved."); } }),
    onAdd: () => formSheet({ title: "New class", fields: classFields(true), tall: true, values: { lanes: ["council", "local"] },
      onSave: async (v) => { apply(await api("/api/lanes/classes", { method: "POST", json: { id: v.id, label: v.label, lanes: v.lanes, private: v.private, notes: v.notes } })); good("Added."); } }),
    canDelete: (c, all) => all.length > 1,
    onDelete: async (c) => { apply(await api(`/api/lanes/classes/${c.id}`, { method: "DELETE" })); good("Deleted."); },
  });
  open("Work lanes", laneEd.el, classEd.el, savedTile(data.saved),
    ui.notice("Escalation: a lane is only left when it failed, timed out, was unavailable, the Council's verify step failed, or you ask for a stronger model. Private work never escalates to a cloud lane.", { tone: "info" }));
}

export const OWNER_SECTIONS = { assistants: openAssistants, notifyprefs: openNotifyPrefs, safetyrules: openSafetyRules,
  redactrules: openRedactRules, memorykinds: openMemoryKinds, lanes: openLanes, voices: openVoices,
  deliveryrules: () => import("./presence.js").then((m) => m.openDeliveryRules()) };
