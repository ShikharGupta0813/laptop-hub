// "What did the agent do?": a readable timeline over the audit log with filters, search, details and CSV export.
// Backed by GET /api/audit/search (docs/api/audit.md). All text is set with textContent (via el()), never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/audit.css";
export const KINDS = [
  ["", "Everything"], ["tool", "Tool calls"], ["approval", "Approvals"], ["denial", "Denials"], ["n3", "Critical (N3)"],
  ["outbound", "Sent out"], ["mcp", "MCP calls"], ["panic", "Panic and stops"], ["trust", "Trust and permissions"],
  ["setting", "Setting changes"], ["login", "Logins"],
];
export const LEVELS = [["", "Any level"], ["N1", "N1 read"], ["N2", "N2 change"], ["N3", "N3 critical"]];
export const OUTCOMES = [["", "Any outcome"], ["ran", "Ran"], ["approved", "Approved"], ["denied", "Denied"], ["blocked", "Blocked"],
  ["asked", "Asked"], ["cancelled", "Cancelled"], ["failed", "Failed"]];
const TONE = { ran: "good", approved: "good", denied: "bad", blocked: "bad", failed: "bad", asked: "warn", cancelled: "warn", info: "gray" };
const ICON = { tool: "terminal", approval: "check-circle", denial: "x", n3: "alert", outbound: "arrow-up-right", mcp: "layers",
  panic: "stop", trust: "key", setting: "settings", login: "lock", redaction: "eye-off", other: "history" };

function ensureCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

export function query(f, extra = {}) {
  const q = new URLSearchParams();
  for (const k of ["since", "until", "kind", "level", "outcome", "q"]) if (f[k]) q.set(k, f[k]);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  return q.toString();
}

function select(label, options, onChange) {
  const node = el("select", { "aria-label": label }, ...options.map(([v, t]) => el("option", { value: v }, t)));
  node.addEventListener("change", () => onChange(node.value));
  return node;
}

function rowNode(r) {
  const fields = Object.entries(r.detail || {});
  const detail = el("dl", { class: "au-fields", hidden: true },
    el("dt", {}, "event"), el("dd", {}, r.event), r.by ? el("dt", {}, "approved by") : null, r.by ? el("dd", {}, r.by) : null,
    ...fields.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, typeof v === "string" ? v : JSON.stringify(v))]));
  const node = el("button", { type: "button", class: "ui-row ui-row--interactive au-row", "aria-expanded": "false" },
    ui.tile(ICON[r.kind] || "history", TONE[r.outcome] || "gray"),
    el("span", { class: "ui-row__body" },
      el("span", { class: "ui-row__title ui-clamp-2" }, r.summary || r.event),
      el("span", { class: "ui-row__sub" }, [r.by && `by ${r.by}`, r.level, r.outcome].filter(Boolean).join(" · ")), detail),
    el("span", { class: "ui-row__meta" }, r.time ? r.time.replace("T", " ").slice(5, 16) : ""));
  node.addEventListener("click", () => { const open = detail.hidden; detail.hidden = !open; node.setAttribute("aria-expanded", String(open)); });
  return node;
}

export function openAudit(ctx) {
  ensureCss();
  const f = { since: "", until: "", kind: "", level: "", outcome: "", q: "" };
  const state = { before: null, done: false, loading: false, gen: 0 };
  const list = el("div", { class: "ui-list ui-list--inset au-list" });
  const status = el("div", { class: "au-status" });
  const search = el("input", { type: "search", placeholder: "Search", "aria-label": "Search what the agent did", autocomplete: "off", spellcheck: "false" });
  const day = el("input", { type: "date", "aria-label": "Day" });
  day.addEventListener("change", () => { f.since = day.value; f.until = day.value; reload(); });
  const bar = el("div", { class: "au-bar" },
    el("div", { class: "ui-input-wrap" }, ui.icon("search"), search),
    el("div", { class: "au-filters" }, day,
      select("Kind", KINDS, (v) => { f.kind = v; reload(); }),
      select("Level", LEVELS, (v) => { f.level = v; reload(); }),
      select("Outcome", OUTCOMES, (v) => { f.outcome = v; reload(); })));
  const exportBtn = ui.button({ label: "Export CSV", icon: "download", variant: "tonal", size: "sm", onClick: () => exportCsv() });
  const sheet = ui.sheet({ title: "What did the agent do?", class: "au-sheet",
    content: el("div", { class: "ui-stack" }, bar, list, status, exportBtn) });

  async function exportCsv() {
    try {
      const response = await fetch(`/api/audit/search?${query(f, { format: "csv", limit: 5000 })}`, { credentials: "same-origin", headers: { "X-Hub": "1" } });
      if (!response.ok) throw new Error("The export failed.");
      const url = URL.createObjectURL(await response.blob());
      const a = el("a", { href: url, download: "redline-audit.csv" });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (error) { ui.toast(error.message || "The export failed.", { kind: "bad" }); }
  }

  async function more() {
    if (state.loading || state.done) return;
    state.loading = true;
    const gen = state.gen;
    status.replaceChildren(el("span", { class: "ui-spinner", role: "status", "aria-label": "Loading" }));
    try {
      const data = await api(`/api/audit/search?${query(f, { limit: 40, before: state.before })}`);
      if (gen !== state.gen) return;
      const rows = Array.isArray(data.rows) ? data.rows : [];
      list.append(...rows.map(rowNode));
      state.before = data.next_before;
      state.done = data.next_before == null || !rows.length;
      status.replaceChildren();
      if (!list.children.length) status.append(ui.emptyState({ icon: "history", title: "Nothing matches", body: "Try another day or clear a filter.", compact: true }));
      else if (state.done) status.append(el("p", { class: "ui-hint" }, data.truncated ? "Older entries are too far back to load." : "That's everything."));
      else status.append(ui.button({ label: "Load more", variant: "tonal", size: "sm", onClick: () => more() }));
    } catch (error) {
      if (gen === state.gen) status.replaceChildren(ui.notice(error.message, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => more() }));
    } finally { if (gen === state.gen) state.loading = false; }
  }
  function reload() { state.gen += 1; state.before = null; state.done = false; state.loading = false; list.replaceChildren(); more(); }
  let timer = null;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { f.q = search.value.trim(); reload(); }, 300); });
  reload();
  return sheet;
}
