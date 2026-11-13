// Privacy and usage UI: the "Usage today" glass card (Settings), the compact gauge row (Home), the caps sheet and
// the redaction sheet with its "what will be hidden?" preview. Routes: docs/api/privacy-usage.md.
// All text is set with textContent (via el()), never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/privacy.css";
const CAP_ROWS = [
  ["gemini", "Gemini"], ["claude-login", "Claude (my login)"], ["claude", "Claude (API key)"], ["duo", "Claude + Gemini"],
];
const KIND_LABELS = { email: "Email addresses", phone: "Phone numbers", key: "API keys, tokens, private keys", card: "Card numbers",
  aadhaar: "Aadhaar numbers", pan: "PAN numbers", iban: "IBANs", password: "Passwords after a label", name: "Your own words (below)" };

function ensureCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });

function patch(json) { return api("/api/settings", { method: "PATCH", json }); }

// One provider line: label, "n / cap" and a thin bar against the cap (an empty track when there is no cap).
function gaugeLine(row, { compact = false } = {}) {
  const hasCap = row.cap > 0;
  const bar = ui.progressBar(hasCap ? Math.min(1, row.today / row.cap) : 0, `${row.label} requests today`);
  if (!hasCap) bar.classList.add("pv-bar--nocap");
  else if (row.today >= row.cap) bar.classList.add("pv-bar--full");
  else if (row.today >= row.cap * 0.8) bar.classList.add("pv-bar--warn");
  return el("div", { class: "pv-line" + (compact ? " pv-line--compact" : "") },
    el("span", { class: "pv-name" }, compact ? row.label.replace(" (my login)", "").replace(" (API key)", " API") : row.label),
    el("span", { class: "pv-count" }, hasCap ? `${row.today} / ${row.cap}` : String(row.today)),
    bar);
}

const AI_ROWS = (u) => (u?.providers || []).filter((r) => ["gemini", "claude-login", "claude", "duo", "local"].includes(r.provider));

// ---- Settings: the glass card --------------------------------------------------------------------
export function usageCard({ onEdit } = {}) {
  ensureCss();
  const body = el("div", { class: "pv-body" }, ui.skeleton("list", 3));
  const edit = ui.button({ label: "Edit caps", icon: "edit", variant: "secondary", onClick: () => onEdit?.() });
  const card = el("section", { class: "ui-card ui-hud pv-card", "aria-label": "Usage today" },
    el("h2", { class: "ui-caps pv-title" }, "Usage today"), body, edit);
  async function refresh() {
    try {
      const u = await api("/api/usage");
      const rows = [...AI_ROWS(u), ...(u.providers || []).filter((r) => ["council", "research", "vision", "autopilot"].includes(r.provider) && r.today > 0)];
      const notes = [];
      if (u.cloud?.cap) notes.push(el("p", { class: "ui-hint pv-note" }, `Cloud total: ${u.cloud.used} of ${u.cloud.cap} today, ${u.cloud.remaining} left. At the cap the local model answers instead.`));
      if (u.claude_login_5h) notes.push(el("p", { class: "ui-hint pv-note" }, `Claude login, last 5 hours: about ${u.claude_login_5h.calls} of ${u.claude_login_5h.soft_cap} (an estimate, not a limit Claude reports).`));
      body.replaceChildren(...rows.map((r) => gaugeLine(r)), ...notes,
        el("p", { class: "ui-hint pv-note" }, `Requests this week: ${Object.values(u.week || {}).reduce((a, b) => a + b, 0)}. Counts only, never what was sent.`));
      card.usage = u;
    } catch (error) { body.replaceChildren(el("p", { class: "ui-hint" }, error?.message || "Usage is not available.")); }
  }
  refresh();
  card.refresh = refresh;
  return card;
}

// ---- Home: a compact row ------------------------------------------------------------------------
export function gaugeRow({ onOpen } = {}) {
  ensureCss();
  const row = el("button", { type: "button", class: "pv-home", hidden: true, "aria-label": "AI usage today. Open settings",
    onclick: () => { ui.haptic("light"); onOpen?.(); } });
  async function refresh() {
    try {
      const u = await api("/api/usage");
      const rows = AI_ROWS(u).filter((r) => r.today > 0 || r.cap > 0);
      row.hidden = !rows.length;
      row.replaceChildren(el("span", { class: "ui-caps pv-home__title" }, "AI today"),
        el("span", { class: "pv-home__lines" }, rows.map((r) => gaugeLine(r, { compact: true }))));
    } catch (_) { row.hidden = true; }
  }
  refresh();
  row.refresh = refresh;
  return row;
}

// ---- the caps sheet ------------------------------------------------------------------------------
export function capsSheet(S, onSaved) {
  ensureCss();
  const num = (value, label, hint) => {
    const input = el("input", { class: "ui-input", type: "number", inputmode: "numeric", min: "0", max: "100000", step: "1", value: String(value || 0) });
    return { input, node: el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input, hint ? el("span", { class: "ui-hint" }, hint) : null) };
  };
  const total = num(S.cloud_daily_cap, "All cloud AI together, per day", "0 means no cap. Counts requests to Gemini, Claude and Claude + Gemini.");
  const own = Object.fromEntries(CAP_ROWS.map(([k, label]) => [k, num(S.provider_daily_caps?.[k], label, null)]));
  const soft = num(S.claude_login_5h_soft_cap, "Claude login: calls per 5 hours (your own estimate)", "0 means unknown, so nothing is shown.");
  const content = el("div", { class: "ui-stack ui-stack--lg pv-sheet" },
    el("p", { class: "ui-hint" }, "When a cap is reached the assistant uses the local model for that message and says so. It never switches to a different paid AI. Say \"use gemini anyway\" to go over a cap once."),
    total.node, ...CAP_ROWS.map(([k]) => own[k].node), soft.node);
  const read = (n) => { const v = Number(n.input.value); return Number.isInteger(v) && v >= 0 && v <= 100000 ? v : null; };
  ui.sheet({ title: "Edit caps", class: "st-sheet", content, actions: [
    { label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const caps = {}; let bad = false;
      for (const [k] of CAP_ROWS) { const v = read(own[k]); if (v == null) bad = true; else if (v) caps[k] = v; }
      const t = read(total), s = read(soft);
      if (bad || t == null || s == null) { fail({ message: "Use whole numbers from 0 to 100000." }); return false; }
      try { await patch({ cloud_daily_cap: t, provider_daily_caps: caps, claude_login_5h_soft_cap: s }); ui.toast("Caps saved.", { kind: "good" }); onSaved?.(); return true; }
      catch (error) { fail(error); return false; }
    } }] });
}

// ---- the redaction sheet -------------------------------------------------------------------------
export function redactionSheet(S, onSaved) {
  ensureCss();
  const kinds = new Set(S.redact_kinds || []);
  const all = S.redact_kinds_all || Object.keys(KIND_LABELS);
  const on = ui.switchControl({ checked: S.redact_enabled !== false, label: "Hide private details from cloud AI" });
  const boxes = all.map((k) => {
    const input = el("input", { type: "checkbox", value: k }); input.checked = kinds.has(k);
    return el("label", { class: "pv-kind" }, input, el("span", {}, KIND_LABELS[k] || k));
  });
  const names = el("textarea", { class: "ui-input", rows: "3", placeholder: "One per line, e.g. your employer or a project name", maxlength: "3000" });
  names.value = (S.redact_names || []).join("\n");
  const sample = el("textarea", { class: "ui-input", rows: "3", maxlength: "4000", placeholder: "Paste something to see what would be hidden" });
  const out = el("pre", { class: "pv-out", "aria-live": "polite" }, "");
  const counts = el("p", { class: "ui-hint" }, "");
  const test = ui.button({ label: "What will be hidden?", icon: "eye", variant: "secondary", onClick: async () => {
    try {
      const r = await api("/api/redact/preview", { method: "POST", json: { text: sample.value } });
      out.textContent = r.redacted;
      counts.textContent = r.enabled ? (r.total ? `Hidden: ${Object.entries(r.counts).map(([k, v]) => `${v} ${k}`).join(", ")}.` : "Nothing in this text would be hidden.") : "Redaction is switched off.";
    } catch (error) { fail(error); }
  } });
  const note = ui.notice("This hides patterns (emails, numbers, keys, passwords with a label, and the words you list). It cannot hide names it does not know, addresses, anything that has no recognisable shape, text inside screenshots, or what the AI could guess. The local model is never filtered.", { tone: "info" });
  const content = el("div", { class: "ui-stack ui-stack--lg pv-sheet" }, note,
    el("div", { class: "ui-row pv-switch" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Hide private details from cloud AI"),
      el("span", { class: "ui-row__sub" }, "Before Gemini or Claude sees a message, tool result or file text.")), on),
    el("div", { class: "pv-kinds" }, boxes),
    el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Also hide these words"), names,
      el("span", { class: "ui-hint" }, "Matched whole-word, any capitalisation. Never written to the activity log.")),
    el("div", { class: "ui-stack" }, el("span", { class: "ui-label" }, "Preview"), sample, test, out, counts));
  ui.sheet({ title: "Privacy redaction", class: "st-sheet st-sheet--tall", content, actions: [
    { label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const list = names.value.split("\n").map((x) => x.trim()).filter(Boolean);
      try {
        await patch({ redact_enabled: on.input.checked, redact_kinds: boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value), redact_names: list });
        ui.toast("Privacy settings saved.", { kind: "good" }); onSaved?.(); return true;
      } catch (error) { fail(error); return false; }
    } }] });
}
