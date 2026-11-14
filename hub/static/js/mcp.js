// MCP servers (Settings): the owner's list of Model Context Protocol servers whose tools Jarvis and Friday may use.
// Built on the shared list editor (js/listeditor.js) and the provider-list conventions (js/provlists.js). Routes: docs/api/mcp.md.
// Every string from a server (names, descriptions, errors) is put in with textContent via el(); a server can never make markup.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor } from "./listeditor.js";

const CSS = "/css/mcp.css";
const BASE = "/api/mcp";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const ensureCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };
const words = (e) => (e.kind === "http" ? e.settings.url : (e.settings.argv || []).join(" "));
const TRUST_TEXT = { "ask-every-time": "Ask me every time", "read-only-free": "Let tools I mark read-only run without asking" };

function statusLine(e) {
  const s = e.status || {};
  const bits = [e.kind === "http" ? "web address" : "program on this laptop"];
  bits.push(s.running ? "running" : "stopped");
  bits.push(s.tool_count ? `${s.tool_count} tools found` : "no tools listed yet: press Test");
  if (s.last_error) bits.push(`last problem: ${s.last_error}`);
  return bits.join(" · ");
}

async function confirmRun(e, what) {
  return ui.confirm({ title: what, message: `${words(e)}\n\nThis runs with your rights on this laptop (or talks to that server). Only continue if you trust it.`,
    confirmLabel: "Continue", icon: "shield" });
}

function envText(e) { return (e?.settings?.env_names || []).map((n) => `${n}=`).join("\n"); }
function parseEnv(text) {
  const out = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

// ---- add / edit a server ---------------------------------------------------------------------------------------------
function serverSheet(data, entry, done) {
  const kinds = data.kinds || {};
  let kind = entry?.kind || "stdio";
  const label = el("input", { class: "ui-input", maxlength: "60", "aria-label": "Name", value: entry?.label || "" });
  const kindSel = el("select", { class: "ui-input", "aria-label": "Kind" }, ...Object.entries(kinds).map(([k, v]) => el("option", { value: k }, v.label)));
  kindSel.value = kind;
  kindSel.disabled = !!entry;
  const presetSel = !entry && (data.presets || []).length
    ? el("select", { class: "ui-input", "aria-label": "Start from an example" }, el("option", { value: "" }, "Start from an example (review before enabling)..."),
      ...data.presets.map((p, i) => el("option", { value: String(i) }, p.label))) : null;
  const note = el("p", { class: "ui-hint" });
  const argv = el("textarea", { class: "ui-input", rows: "4", spellcheck: "false", autocapitalize: "off", "aria-label": "Command, one word per line" });
  const cwd = el("input", { class: "ui-input", type: "text", maxlength: "400", spellcheck: "false", "aria-label": "Start in folder" });
  const url = el("input", { class: "ui-input", type: "url", maxlength: "400", spellcheck: "false", "aria-label": "Address", placeholder: "https://example.com/mcp" });
  const env = el("textarea", { class: "ui-input", rows: "3", spellcheck: "false", autocapitalize: "off", "aria-label": "Environment variables",
    placeholder: "API_KEY=value" });
  const token = el("input", { class: "ui-input", type: "password", autocomplete: "new-password", maxlength: "500", "aria-label": "Access token",
    placeholder: entry?.has_secret ? "Saved. Leave blank to keep it." : "Optional" });
  const trust = el("select", { class: "ui-input", "aria-label": "Trust" }, ...Object.entries(TRUST_TEXT).map(([v, t]) => el("option", { value: v }, t)));
  argv.value = (entry?.settings?.argv || []).join("\n");
  cwd.value = entry?.settings?.cwd || "";
  url.value = entry?.settings?.url || "";
  env.value = envText(entry);
  trust.value = entry?.settings?.trust || "ask-every-time";
  const stdioBox = el("div", { class: "ui-stack" },
    el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Command, one word per line (program first)"), argv),
    el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Start in folder (blank = an empty folder of its own)"), cwd),
    el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Environment variables, NAME=value per line"), env,
      el("span", { class: "ui-hint" }, "Only these are passed on (plus PATH, HOME and a few basics), never the hub's own. Values are kept in the laptop's secure store and never shown again; a blank value keeps the saved one.")));
  const httpBox = el("div", { class: "ui-stack" },
    el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Address"), url),
    el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Access token (sent as a Bearer header)"), token));
  const paint = () => {
    kind = kindSel.value;
    note.textContent = kinds[kind]?.note || "";
    stdioBox.hidden = kind !== "stdio";
    httpBox.hidden = kind !== "http";
  };
  kindSel.addEventListener("change", paint);
  presetSel?.addEventListener("change", () => {
    const p = data.presets[Number(presetSel.value)];
    if (!p) return;
    kindSel.value = p.kind; label.value = p.label.replace(/ \(example\)$/, "");
    paint();
    argv.value = (p.settings.argv || []).join("\n"); url.value = p.settings.url || "";
    note.textContent = p.note;
  });
  paint();
  return ui.sheet({ title: entry ? `Edit ${entry.label}` : "Add an MCP server", class: "mcp-sheet",
    content: el("div", { class: "ui-stack" }, presetSel, el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Name"), label),
      entry ? null : el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Kind"), kindSel), note, stdioBox, httpBox,
      el("label", { class: "ui-field mcp-field" }, el("span", { class: "ui-label" }, "Trust"), trust,
        el("span", { class: "ui-hint" }, "Even with the second choice, each tool must be marked read-only by you, and tools that look like shell, delete or send always ask with a typed CONFIRM."))),
    actions: [{ label: entry ? "Save" : "Add (switched off)", icon: "check", variant: "primary", onClick: async () => {
      const settings = { trust: trust.value };
      if (kind === "stdio") { settings.argv = argv.value.split("\n").map((s) => s.trim()).filter(Boolean); settings.cwd = cwd.value.trim(); }
      else settings.url = url.value.trim();
      const body = { label: label.value.trim(), settings };
      if (kind === "stdio") body.env = parseEnv(env.value);
      if (token.value.trim()) body.secret = token.value.trim();
      try {
        if (entry) {
          const touches = entry.enabled;                   // saving a running server's settings asks again, whatever changed
          if (touches && !(await confirmRun({ ...entry, settings: { ...entry.settings, ...settings } }, "Save changes to a running program?"))) return false;
          await api(`${BASE}/${encodeURIComponent(entry.id)}`, { method: "PUT", json: { ...body, ...(touches ? { confirm: true } : {}) } });
        } else {
          await api(BASE, { method: "POST", json: { ...body, kind, enabled: false } });      // new servers always start switched off
        }
      } catch (error) { fail(error); return false; }
      ui.toast(entry ? "Saved." : "Added, switched off. Press Test, read the tools, then switch it on.", { kind: "good" });
      done();
    } }] });
}

// ---- one server: tools --------------------------------------------------------------------------------------------------
function toolRow(e, t, reload) {
  const trusted = e.settings.trust === "read-only-free";
  const put = async (patch) => {
    try { await api(`${BASE}/${encodeURIComponent(e.id)}/tools/${encodeURIComponent(t.name)}`, { method: "PUT", json: patch }); await reload(); } catch (error) { fail(error); await reload(); }
  };
  const sw = (checked, text, onChange, disabled) => {
    const s = ui.switchControl({ checked, label: `${text} (${t.name})`, onChange });
    if (disabled) s.input.disabled = true;
    return el("label", { class: "mcp-sw" }, s, el("span", {}, text));
  };
  const badges = [];
  if (t.forced) badges.push(ui.badge("Always asks, typed CONFIRM", "warn"));
  else if (t.level === "N3") badges.push(ui.badge("Critical (you raised it)", "warn"));
  else if (t.free) badges.push(ui.badge("Runs without asking", "good"));
  else badges.push(ui.badge("Asks every time"));
  if (t.changed) badges.push(ui.badge("Changed since you marked it: asks again", "warn"));
  if (t.server_says_read_only) badges.push(ui.badge("Server says read-only (not trusted)"));
  if (t.refused) badges.push(ui.badge(`Not offered: ${t.refused}`, "warn"));
  return el("div", { class: "ui-card mcp-tool" + (t.enabled ? "" : " is-off") },
    el("h4", { class: "mcp-tool__name" }, t.title ? `${t.name} (${t.title})` : t.name),
    t.description ? el("p", { class: "ui-hint mcp-tool__desc" }, t.description) : null,
    t.args?.length ? el("p", { class: "ui-hint" }, `Takes: ${t.args.join(", ")}`) : null,
    el("div", { class: "mcp-badges" }, ...badges),
    el("div", { class: "mcp-switches" },
      sw(t.enabled, "Offer to the assistant", (on) => put({ enabled: on }), !!t.refused),
      sw(t.marked_read_only && !t.changed, "Read-only: run without asking", async (on) => {
        if (on && !(await ui.confirm({ title: `Run ${t.name} without asking?`, message: `${t.description || "No description."}\n\nOnly if this tool can never change or send anything.`, confirmLabel: "Mark read-only" }))) { await reload(); return; }
        put({ read_only: on, confirm: true });
      }, !trusted || !!t.forced || !!t.refused),
      sw(t.level === "N3" && !t.forced, "Critical: always typed CONFIRM", async (on) => {
        if (!on && !(await ui.confirm({ title: "Make this less strict?", message: "It goes back to a normal approval card.", confirmLabel: "Yes" }))) { await reload(); return; }
        put({ level: on ? "N3" : "", confirm: true });
      }, !!t.forced),
      sw(t.sensitive, "Sensitive: keep its arguments out of the log", (on) => put({ sensitive: on }), false)));
}

function detailSheet(data, entry, onClose) {
  const body = el("div", { class: "ui-stack mcp" });
  const out = el("p", { class: "ui-hint mcp-out", role: "status" });
  let cur = entry;
  async function reload() {
    try { data = await api(BASE); } catch (error) { fail(error); return; }
    cur = data.entries.find((x) => x.id === entry.id) || cur;
    paint();
  }
  function paint() {
    const test = ui.button({ label: "Test (start, list tools, stop)", icon: "play", variant: "tonal", onClick: () => ui.busy(test, async () => {
      if (!(await confirmRun(cur, "Start this server to list its tools?"))) return;
      out.textContent = "Starting...";
      try {
        const r = await api(`${BASE}/${encodeURIComponent(cur.id)}/test`, { method: "POST", json: { confirm: true } });
        out.textContent = r.ok ? `Works: ${r.server?.name || "server"} ${r.server?.version || ""}, protocol ${r.protocol}, ${r.count} tools.`
          : `Did not work: ${r.error}${r.stderr ? ` (it said: ${r.stderr.split("\n").filter(Boolean).slice(-1)[0]})` : ""}`;
        await reload();
      } catch (error) { out.textContent = error.message; }
    }) });
    const stop = ui.button({ label: "Stop now", icon: "stop", variant: "ghost", disabled: !cur.status?.running, onClick: async () => {
      try { await api(`${BASE}/${encodeURIComponent(cur.id)}/stop`, { method: "POST" }); await reload(); } catch (error) { fail(error); }
    } });
    body.replaceChildren(
      el("p", { class: "ui-hint" }, statusLine(cur)),
      el("p", { class: "ui-hint mcp-cmd" }, words(cur)),
      el("p", { class: "ui-hint" }, TRUST_TEXT[cur.settings.trust] || ""),
      el("div", { class: "mcp-actions" }, test, stop), out,
      cur.tools.length ? el("div", { class: "ui-stack" }, ...cur.tools.map((t) => toolRow(cur, t, reload)))
        : ui.emptyState({ icon: "terminal", title: "No tools listed yet", body: "Press Test to see what this server offers.", compact: true }));
  }
  paint();
  return ui.sheet({ title: `${entry.label}: tools`, content: body, class: "mcp-sheet", onClose });
}

// ---- the list ---------------------------------------------------------------------------------------------------------------
export function openMcpSheet() {
  ensureCss();
  const box = el("div", { class: "ui-stack mcp" });
  let data = { entries: [] };
  const ed = listEditor({
    hint: "MCP servers are programs or web services that give Jarvis and Friday extra tools. Nothing is on until you switch it on. Every tool asks you each time unless you mark that exact tool read-only, and what a tool returns is treated as untrusted text.",
    rowOf: (e) => ({ icon: "terminal", title: e.label, sub: statusLine(e), meta: `${e.tools.filter((t) => t.enabled).length}/${e.tools.length}`, enabled: e.enabled }),
    onToggle: async (e, on) => {
      if (on && !(await confirmRun(e, `Switch on ${e.label}?`))) throw new Error("Not switched on.");
      await api(`${BASE}/${encodeURIComponent(e.id)}`, { method: "PUT", json: { enabled: on, ...(on ? { confirm: true } : {}) } });
      await load();
    },
    onEdit: (e) => detailSheet(data, e, load),
    onDelete: async (e) => { await api(`${BASE}/${encodeURIComponent(e.id)}`, { method: "DELETE" }); await load(); },
    onAdd: () => serverSheet(data, null, load), addLabel: "Add an MCP server",
    empty: "No MCP servers yet. Add one, or start from an example (review it first).",
  });
  async function load() {
    try { data = await api(BASE); } catch (error) { box.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load the list", body: error.message, compact: true })); return; }
    ed.set(data.entries);
    const edits = data.entries.map((e) => ui.button({ label: `Edit ${e.label}`, icon: "edit", variant: "ghost", size: "sm", onClick: () => serverSheet(data, e, load) }));
    box.replaceChildren(data.enabled ? null : ui.notice("MCP is switched off in the hub's settings (mcp_enabled).", { tone: "warn" }), ed.el,
      edits.length ? el("div", { class: "mcp-actions" }, el("span", { class: "ui-hint" }, "Change a server's command or token:"), ...edits) : null);
  }
  const sh = ui.sheet({ title: "MCP servers", content: box, class: "mcp-sheet" });
  load();
  return sh;
}

// The Settings card: one row that opens the sheet.
export function mcpCard() {
  ensureCss();
  return el("section", { class: "ui-card ui-hud ui-stack", "aria-label": "MCP servers" },
    el("h2", { class: "ui-caps" }, "MCP servers"),
    el("div", { class: "ui-list ui-list--inset ui-list--icons" },
      ui.listRow({ icon: "terminal", title: "MCP servers", sub: "Extra tools from programs you trust. Every call asks first.", chevron: true, onClick: () => openMcpSheet() })));
}
