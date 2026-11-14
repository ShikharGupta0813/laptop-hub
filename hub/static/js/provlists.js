// Owner-editable provider LISTS: add, edit, switch off, reorder, test, delete. One editor serves Settings > Providers
// (search providers docs/api/searchprov.md, vision providers docs/api/vision.md, AI endpoints docs/api/endpoints.md),
// the Skills "Sources" tab (docs/api/skills.md) and the Claude screen's "Sources" sheet (docs/api/ccsessions.md).
// Every list API answers { entries: [{id, kind, label, enabled, settings, has_secret, ...}], kinds: {kind: {label, fields, secret}}, presets? }.
// All text goes through el() (textContent); a secret is typed into a password field and is never shown again.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/provlists.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const ensureCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };

export const LISTS = {
  search: { title: "Search providers", icon: "search", base: "/api/search-providers", testable: true, noun: "provider",
    intro: "Research uses the first switched-on provider that finds results. If one fails or finds nothing, the next is tried. A failing provider is skipped for a while." },
  vision: { title: "Vision providers", icon: "eye", base: "/api/vision/providers", testable: true, noun: "provider",
    intro: "\"What's on my screen?\" asks the first switched-on provider that can take a picture. The test button sends a tiny generated picture, never your screen." },
  ai: { title: "AI endpoints", icon: "bot", base: "/api/ai-endpoints", testable: true, noun: "endpoint", choice: true,
    intro: "Your own OpenAI-compatible APIs (OpenAI, OpenRouter, LM Studio...). Pick one for routine drafts below. The main assistant is not changed. Text sent to a cloud endpoint is redacted like other cloud calls." },
};

const fieldValue = (f, node) => f.type === "bool" ? node.checked : f.type === "list" ? node.value.split(",").map((s) => s.trim()).filter(Boolean) : node.value.trim();

// Add / edit form in its own sheet. cfg.base is the list API; `entry` is null when adding.
function formSheet(cfg, data, entry, done) {
  const kinds = data.kinds || {};
  let kind = entry?.kind || Object.keys(kinds)[0];
  const label = el("input", { class: "ui-input", maxlength: "60", "aria-label": "Name", value: entry?.label || "" });
  const kindSel = el("select", { class: "ui-input", "aria-label": "Kind" }, ...Object.entries(kinds).map(([k, v]) => el("option", { value: k }, v.label || k)));
  kindSel.value = kind;
  if (entry) kindSel.disabled = true;
  const presetSel = (data.presets || []).length && !entry
    ? el("select", { class: "ui-input", "aria-label": "Start from a template" }, el("option", { value: "" }, "Start from a template..."),
      ...data.presets.map((p, i) => el("option", { value: String(i) }, p.label))) : null;
  const holder = el("div", { class: "ui-stack" });
  let inputs = {}, secret = null;
  let values = { ...(entry?.settings || {}) };

  function renderFields() {
    inputs = {};
    const info = kinds[kind] || { fields: [] };
    const rows = [];
    for (const f of info.fields || []) {
      let node;
      const current = values[f.key];
      if (f.type === "bool") { node = el("input", { type: "checkbox" }); node.checked = !!current; }
      else if (f.type === "select") { node = el("select", { class: "ui-input" }, ...(f.options || []).map((o) => el("option", { value: o }, o))); node.value = current || f.options?.[0] || ""; }
      else { node = el("input", { class: "ui-input", type: f.type === "url" ? "url" : "text", placeholder: f.placeholder || "", maxlength: "500", autocapitalize: "off", spellcheck: "false" }); node.value = Array.isArray(current) ? current.join(", ") : (current ?? ""); }
      node.setAttribute("aria-label", f.label);
      inputs[f.key] = [f, node];
      rows.push(el("label", { class: "ui-field pl-field" + (f.type === "bool" ? " pl-field--bool" : "") }, el("span", { class: "ui-label" }, f.label + (f.required ? " *" : "")), node));
    }
    if (info.secret) {
      secret = el("input", { class: "ui-input", type: "password", autocomplete: "new-password", maxlength: "500", placeholder: entry?.has_secret ? "Saved. Leave blank to keep it." : "Paste the key" });
      secret.setAttribute("aria-label", info.secret);
      rows.push(el("label", { class: "ui-field pl-field" }, el("span", { class: "ui-label" }, info.secret), secret,
        el("span", { class: "ui-hint" }, "Kept in the laptop's secure store (OS keyring). It is never shown again.")));
    } else secret = null;
    if (info.note) rows.push(el("p", { class: "ui-hint" }, info.note));
    holder.replaceChildren(...rows);
  }
  kindSel.addEventListener("change", () => { kind = kindSel.value; values = {}; renderFields(); });
  presetSel?.addEventListener("change", () => {
    const p = data.presets[Number(presetSel.value)];
    if (!p) return;
    kind = p.kind; kindSel.value = kind; values = { ...p.settings }; label.value = p.label; renderFields();
  });
  renderFields();

  const sh = ui.sheet({ title: entry ? `Edit ${entry.label}` : `Add a ${cfg.noun || "provider"}`,
    content: el("div", { class: "ui-stack" }, presetSel, el("label", { class: "ui-field pl-field" }, el("span", { class: "ui-label" }, "Name"), label),
      entry ? null : el("label", { class: "ui-field pl-field" }, el("span", { class: "ui-label" }, "Kind"), kindSel), holder),
    actions: [{ label: "Save", icon: "check", variant: "primary", onClick: async () => {
      const settings = {};
      for (const [key, [f, node]] of Object.entries(inputs)) settings[key] = fieldValue(f, node);
      const body = { label: label.value.trim(), settings };
      if (secret?.value.trim()) body.secret = secret.value.trim();
      try {
        if (entry) await api(`${cfg.base}/${encodeURIComponent(entry.id)}`, { method: "PUT", json: body });
        else await api(cfg.base, { method: "POST", json: { ...body, kind } });
      } catch (error) { fail(error); return false; }
      ui.toast("Saved.", { kind: "good" });
      done();
    } }] });
  return sh;
}

// Fills `box` with the list; returns reload(). `cfg.extraActions(entry, reload)` may add buttons per row,
// `cfg.after(data, box)` may add a footer block (approved hosts, the default endpoint...).
export function mountList(box, cfg) {
  ensureCss();
  let data = null;

  async function load() {
    try { data = await api(cfg.base); } catch (error) { box.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load the list", body: error.message, compact: true })); return; }
    paint();
  }

  async function move(index, step) {
    const ids = data.entries.map((e) => e.id);
    const j = index + step;
    if (j < 0 || j >= ids.length) return;
    [ids[index], ids[j]] = [ids[j], ids[index]];
    try { await api(`${cfg.base}/order`, { method: "PUT", json: { ids } }); await load(); } catch (error) { fail(error); }
  }

  function status(e) {
    const bits = [];
    if (e.unsupported) bits.push(e.unsupported);
    const st = e.status;
    if (st?.failing) bits.push(`Skipped for about ${Math.max(1, Math.round(st.skip_seconds / 60))} min: ${st.last_error || "failed lately"}`);
    else if (st?.last_ok) bits.push("Worked lately");
    return bits.join(" · ");
  }

  function row(e, i) {
    const kindLabel = data.kinds?.[e.kind]?.label || e.kind;
    const sw = ui.switchControl({ checked: e.enabled, label: `Use ${e.label}`, onChange: async (on) => {
      try { await api(`${cfg.base}/${encodeURIComponent(e.id)}`, { method: "PUT", json: { enabled: on } }); await load(); } catch (error) { fail(error); await load(); }
    } });
    const out = el("p", { class: "ui-hint pl-result", role: "status" });
    const buttons = [
      ui.button({ label: "Move up", icon: "chevron-up", variant: "ghost", iconOnly: true, disabled: i === 0, onClick: () => move(i, -1) }),
      ui.button({ label: "Move down", icon: "chevron-down", variant: "ghost", iconOnly: true, disabled: i === data.entries.length - 1, onClick: () => move(i, 1) }),
    ];
    if (cfg.testable) {
      const test = ui.button({ label: "Test", icon: "play", variant: "tonal", onClick: () => ui.busy(test, async () => {
        out.textContent = "Testing...";
        try {
          const r = await api(`${cfg.base}/${encodeURIComponent(e.id)}/test`, { method: "POST" });
          out.textContent = r.ok ? `Works${r.count != null ? `: ${r.count} results, first "${r.first}"` : r.answer ? `: it said "${r.answer}"` : r.reply ? `: it said "${r.reply}"` : ""}.` : `Did not work: ${r.error || "no answer"}`;
        } catch (error) { out.textContent = error.message; }
      }) });
      buttons.push(test);
    }
    buttons.push(ui.button({ label: "Edit", icon: "edit", variant: "ghost", onClick: () => formSheet(cfg, data, e, load) }));
    for (const b of cfg.extraActions?.(e, load) || []) buttons.push(b);
    buttons.push(ui.button({ label: "Delete", icon: "trash", variant: "ghost", onClick: async () => {
      if (!await ui.confirm({ title: `Delete "${e.label}"?`, message: e.has_secret ? "Its saved key is removed too." : "", confirmLabel: "Delete", danger: true })) return;
      try { await api(`${cfg.base}/${encodeURIComponent(e.id)}`, { method: "DELETE" }); await load(); } catch (error) { fail(error); }
    } }));
    return el("article", { class: "ui-card pl-card ui-stack" + (e.enabled ? "" : " pl-card--off") },
      el("div", { class: "pl-head" }, el("span", { class: "pl-no" }, String(i + 1)), el("h3", { class: "pl-name" }, e.label), el("span", { class: "pl-switch" }, sw)),
      el("div", { class: "pl-badges" }, ui.badge(kindLabel), e.experimental ? ui.badge("experimental", "warn") : null, e.read_only ? ui.badge("read-only") : null,
        e.has_secret ? ui.badge("key saved", "good") : null, cfg.choice && data.chosen === e.id ? ui.badge("used for drafts", "accent") : null),
      status(e) ? el("p", { class: "ui-hint" }, status(e)) : null, out,
      el("div", { class: "pl-actions" }, ...buttons));
  }

  function paint() {
    const add = ui.button({ label: `Add a ${cfg.noun || "provider"}`, icon: "plus", variant: "primary", onClick: () => formSheet(cfg, data, null, load) });
    const extras = [];
    if (cfg.choice) {
      const sel = el("select", { class: "ui-input", "aria-label": "Endpoint for routine drafts" }, el("option", { value: "" }, "None (use the built-in free path)"),
        ...data.entries.map((e) => el("option", { value: e.id }, e.label)));
      sel.value = data.chosen || "";
      sel.addEventListener("change", async () => { try { await api(`${cfg.base}/choice`, { method: "PUT", json: { id: sel.value } }); ui.toast("Saved.", { kind: "good" }); await load(); } catch (error) { fail(error); } });
      extras.push(el("label", { class: "ui-field pl-field" }, el("span", { class: "ui-label" }, "Use for routine drafts"), sel,
        el("span", { class: "ui-hint" }, data.note || "")));
    }
    box.replaceChildren(cfg.intro ? el("p", { class: "ui-hint" }, cfg.intro) : null,
      data.entries.length ? el("div", { class: "ui-stack" }, ...data.entries.map(row)) : ui.emptyState({ icon: cfg.icon || "layers", title: "The list is empty", compact: true }),
      add, ...extras, cfg.after?.(data, box, load) || null);
  }

  load();
  return load;
}

export function openListSheet(cfg) {
  const box = el("div", { class: "ui-stack pl" });
  const sh = ui.sheet({ title: cfg.title, content: box, class: "pl-sheet" });
  mountList(box, cfg);
  return sh;
}

// The Settings card: one row per list.
export function providersCard() {
  ensureCss();
  const rows = Object.values(LISTS).map((cfg) => ui.listRow({ icon: cfg.icon, title: cfg.title, sub: cfg.intro.split(". ")[0] + ".", chevron: true, onClick: () => openListSheet(cfg) }));
  return el("section", { class: "ui-card ui-hud ui-stack", "aria-label": "Search, vision and AI providers" },
    el("h2", { class: "ui-caps" }, "Providers"), el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows));
}
