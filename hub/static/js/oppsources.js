// Job and gig sources (Settings > "Job and gig sources"): the owner's editable list of sources, the one-tap catalogue,
// the company watch list, timing, and a Test button per source. Routes: docs/api/oppsources.md.
// Everything that came from a website or an email is untrusted and only ever shown with textContent.
// Freelance and AI-task platforms are email-alert or open-link only: auto-submit is off for them and for everything else.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";

const good = (text) => ui.toast(text, { kind: "good" });
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const ADAPTER_NAME = { rss: "RSS or Atom feed", json: "JSON API (field mapping)", html: "Public page (CSS selectors)", email: "Email alerts", webhook: "Webhook",
  greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", workable: "Workable", smartrecruiters: "SmartRecruiters", recruitee: "Recruitee",
  teamtailor: "Teamtailor", personio: "Personio", breezy: "Breezy HR", workday: "Workday", adzuna: "Adzuna (key)", jooble: "Jooble (key)", findwork: "Findwork (key)",
  hn: "Hacker News: Who is hiring", link: "Open link (paste listings)" };
const STATE_NAME = { ok: "Worked", unchanged: "Nothing new", manual_check: "Needs a manual check", error: "Failed, will retry" };
const GROUP_NAME = { ats: "Company job systems (public, no login)", search: "Search sites with salary data (your own free key)", remote: "Remote job boards", freelance: "Freelance (email alerts or open link only)", task: "AI-training and task platforms (email alerts or open link only)" };
const lines = (text) => String(text || "").split("\n").map((x) => x.trim()).filter(Boolean);
const when = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }); };

function describe(s) {
  const st = s.status || {};
  const bits = [ADAPTER_NAME[s.adapter] || s.adapter, s.watch ? `watch, every ${s.interval_effective} min` : `every ${s.interval_effective} min`];
  if (st.state) bits.push(`${STATE_NAME[st.state] || st.state}${st.at ? " " + when(st.at) : ""}`, `${st.items ?? 0} item(s)`);
  else if (!["webhook", "link"].includes(s.adapter)) bits.push("not fetched yet");
  if (st.error) bits.push(st.error);
  return bits.join(" · ");
}

function resultNode(r) {
  const box = el("div", { class: "ui-stack" });
  if (r.error) { box.append(ui.notice(r.error, { tone: "warn" })); return box; }
  box.append(el("p", { class: "ui-hint" }, `Worked: ${r.found} item(s) found.${r.note ? " " + r.note : ""}`));
  for (const s of r.sample || []) {
    box.append(el("p", { class: "ui-hint" }, `${s.title}${s.company ? " at " + s.company : ""} · ${s.work_mode} · ${s.employment}${s.pay_label ? " · " + s.pay_label : ""} · ${s.apply_method}`));
  }
  return box;
}

export async function openSources() {
  let data = await api("/api/oppsources").catch((e) => { fail(e); return null; });
  if (!data) return;
  let ed = null;
  const reload = async () => { data = await api("/api/oppsources"); ed?.set(data.sources); head(); };

  const timing = () => formSheet({
    title: "Timing and clean-up", tall: false,
    fields: [
      { key: "oppsources_enabled", label: "Fetch sources in the background", type: "switch", hint: "Off until you turn it on. Test and Run now always work." },
      { key: "oppsources_interval_min", label: "Check normal sources every (minutes, 5 to 1440)", type: "number", min: 5, max: 1440 },
      { key: "oppsources_watch_interval_min", label: "Check watched companies every (minutes, 2 to 120)", type: "number", min: 2, max: 120, hint: "New roles from companies you watch are flagged Be early." },
      { key: "oppsources_stale_days", label: "Hide listings older than (days)", type: "number", min: 7, max: 365 },
      { key: "oppsources_ghost_days", label: "Suspect a ghost listing after (days still open)", type: "number", min: 14, max: 730 },
      { key: "triage_ai", label: "Allow an AI second opinion on a find", type: "switch", hint: "Off by default. The go / no-go label is always worked out on this laptop by plain rules. With this on, an Ask AI button appears on a find; only that find's text is sent, redacted, and the plain label stays." },
    ],
    values: { oppsources_enabled: data.enabled, oppsources_interval_min: data.interval_min, oppsources_watch_interval_min: data.watch_interval_min,
      oppsources_stale_days: data.stale_days, oppsources_ghost_days: data.ghost_days, triage_ai: data.triage_ai === true },
    onSave: async (v) => { await api("/api/settings", { method: "PATCH", json: Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null)) }); await reload(); good("Saved."); },
  });

  const headBox = el("div", { class: "ui-stack" });
  function head() {
    headBox.replaceChildren(
      ui.notice(data.enabled ? "Background fetching is on." : "Background fetching is off. Turn it on under Timing, or use Run now.", { tone: data.enabled ? "good" : "info" }),
      data.early ? el("p", { class: "ui-hint" }, `${data.early} new role(s) from watched companies: be early.`) : null,
      el("p", { class: "ui-hint" }, `Items: ${data.counts.new} open, ${data.counts.blocked} held back as scams, ${data.counts.expired} expired.`),
      el("div", { class: "le-foot" },
        ui.button({ label: "Timing", icon: "clock", variant: "tonal", size: "sm", onClick: timing }),
        ui.button({ label: "Run now", icon: "refresh", variant: "tonal", size: "sm", onClick: async () => {
          try { const r = await api("/api/oppsources/run", { method: "POST" }); await reload(); good(`${r.ran || 0} source(s) checked, ${r.new || 0} new.`); } catch (e) { fail(e); }
        } }),
        ui.button({ label: "Watch a company", icon: "eye", variant: "tonal", size: "sm", onClick: watchSheet }),
        ui.button({ label: "From the catalogue", icon: "plus", variant: "tonal", size: "sm", onClick: catalogue }),
      ));
  }

  function watchSheet() {
    formSheet({
      title: "Watch a company", saveLabel: "Watch",
      fields: [
        { key: "url", label: "Careers page address", type: "text", placeholder: "https://boards.greenhouse.io/yourcompany", maxlength: 400,
          hint: "REDLINE finds which public job system the company uses (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Teamtailor) and checks it often. New roles are flagged Be early." },
        { key: "label", label: "Name", type: "text", maxlength: 60 },
      ],
      onSave: async (v) => { const r = await api("/api/oppsources/watch", { method: "POST", json: v }); await reload(); good(`Watching ${r.label} (${r.detected.adapter}).`); },
    });
  }

  async function catalogue() {
    const cat = await api("/api/oppsources/presets").catch((e) => { fail(e); return null; });
    if (!cat) return;
    const node = el("div", { class: "ui-stack ui-stack--lg" }, ui.notice(cat.policy, { tone: "info" }));
    for (const group of cat.groups) {
      const rows = cat.presets.filter((p) => p.group === group);
      const bulk = (group === "freelance" || group === "task") && rows.some((p) => !p.added)
        ? ui.button({ variant: "secondary", size: "sm", icon: "plus", label: "Add all in this group", onClick: async () => {
          try { const r = await api("/api/oppsources/presets/add-group", { method: "POST", json: { group } }); sheet.close(); await reload(); good(`Added ${r.added.length} platforms.`); }
          catch (e) { fail(e); }
        } }) : null;
      node.append(el("section", { class: "st-group" }, el("h2", { class: "ui-caps st-caps" }, GROUP_NAME[group] || group), bulk,
        el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows.map((p) => ui.listRow({
          icon: p.never_automated ? "shield" : "globe", tone: p.never_automated ? "orange" : "accent", title: p.label, sub: `${p.verification}. ${p.note}`,
          meta: p.added ? ui.badge("Added", "good") : p.disabled_reason ? ui.badge("Needs key", "warn") : ui.badge("Add", "info"), chevron: true, onClick: () => addPreset(p, sheet, cat.keys) })))));
    }
    const sheet = ui.sheet({ title: "Source catalogue", class: "st-sheet st-sheet--tall", content: node });
  }

  const NEED_LABEL = { board: "Company board name", tenant: "Workday tenant (first part of the careers address)", site: "Workday site name (last part of the address)",
    host: "Workday data centre (wd1, wd3, wd5...)", country: "Country code (in, gb, us, au, ca, de...)", what: "Search words (for example: python developer)",
    keywords: "Search words", search: "Search words", where: "Place (optional)", location: "Place (optional)" };

  function addPreset(p, parent, keys) {
    const fields = [];
    const spec = p.needs_key ? keys?.[p.needs_key] : null;
    if (spec && !spec.set) {
      fields.push({ type: "note", text: `This source needs your own free ${spec.label} key. Get it at ${spec.signup}, then paste it here. It is saved in this laptop's secure keyring, never shown again and never written to a file. The source stays off until a key is saved.` });
      for (const f of spec.fields) fields.push({ key: "key:" + f.name, label: f.label, type: "password", maxlength: 200, hint: "Paste it exactly as given. It is not shown back." });
    } else if (spec && spec.set) {
      fields.push({ type: "note", text: `${spec.label} key: saved in your keyring (not shown). To replace it, remove it from the source list screen and add the source again.` });
    }
    for (const need of p.needs) fields.push({ key: need, label: NEED_LABEL[need] || need, type: "text", maxlength: 80,
      hint: need === "board" ? "The company's name in its job-board address, for example the part after boards.greenhouse.io/." : undefined });
    for (const opt of p.optional || []) fields.push({ key: opt, label: NEED_LABEL[opt] || opt, type: "text", maxlength: 80 });
    if (p.modes.length) fields.push({ key: "mode", label: "Mode", type: "select", options: p.modes.map((m) => [m, m === "email-alert" ? "Email alerts (read from your mail)" : "Open link (paste listings, prepare a proposal)"]),
      hint: "No logins and no bots. You submit by hand." });
    if (p.adapter === "lever") fields.push({ key: "region", label: "Lever region", type: "select", options: [["", "Global"], ["eu", "EU"]] });
    fields.push({ key: "label", label: "Name (optional)", type: "text", maxlength: 60 });
    formSheet({ title: `Add ${p.label}`, saveLabel: "Add", fields, values: { mode: p.modes[0] || "", region: "" },
      onSave: async (v) => {
        const keyValues = {};
        for (const [k, x] of Object.entries(v)) if (k.startsWith("key:")) { if (x) keyValues[k.slice(4)] = x; delete v[k]; }
        if (spec && !spec.set) {
          if (!Object.keys(keyValues).length) throw new Error(`Paste your ${spec.label} key first, or close this sheet.`);
          await api(`/api/oppsources/keys/${p.needs_key}`, { method: "PUT", json: { values: keyValues } });
        }
        await api(`/api/oppsources/presets/${p.id}/add`, { method: "POST", json: Object.fromEntries(Object.entries(v).filter(([, x]) => x)) });
        parent?.close(); await reload(); good("Added.");
      } });
  }

  const kinds = [["job", "Job"], ["freelance", "Freelance"], ["llm-task", "AI task"]];
  const fieldsFor = (adapter) => {
    const f = [{ key: "label", label: "Name", type: "text", maxlength: 60 }, { key: "kind", label: "Kind", type: "select", options: kinds },
      { key: "interval_min", label: "Check every (minutes, 0 = default)", type: "number", min: 0, max: 1440 }];
    if (["rss", "json", "html"].includes(adapter)) f.splice(1, 0, { key: "url", label: "Address", type: "text", maxlength: 400, hint: "A public address. No login inside it." });
    if (adapter === "rss") f.push({ key: "split_company", label: "Titles look like 'Company: Role'", type: "switch", default: false });
    if (adapter === "json") f.push({ key: "list_path", label: "Where the list is (dotted path, empty = top level)", type: "text", maxlength: 80 },
      { key: "mapping", label: "Field mapping (one 'field = path' per line)", type: "textarea", rows: 7, maxlength: 1200,
        hint: "Fields: title, company, url, description, location, posted, employment, remote, pay_min, pay_max, pay_currency, pay_unit, pay_text, id, tags. Start a value with = for a fixed value (for example pay_currency = =USD)." });
    if (adapter === "html") f.push({ key: "item_selector", label: "Item selector", type: "text", maxlength: 200, placeholder: "li.job" },
      { key: "mapping", label: "Field selectors (one 'field = selector' per line)", type: "textarea", rows: 6, maxlength: 800,
        hint: "Example: title = a.title, url = a.title@href, company = .co, location = .loc. Tags, .class, #id, [attr], spaces and > work." });
    if (["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "teamtailor", "personio", "breezy"].includes(adapter)) f.push({ key: "board", label: "Company board name", type: "text", maxlength: 80 },
      { key: "company", label: "Company display name", type: "text", maxlength: 100 }, { key: "watch", label: "On the watch list (early-bird)", type: "switch", default: false });
    if (adapter === "email") f.push({ key: "senders", label: "Senders (address or domain)", type: "chips", hint: "Only mail from these is read, on this laptop." },
      { key: "subject_contains", label: "Or subject contains", type: "chips" }, { key: "account", label: "Mail account id (empty = all)", type: "text", maxlength: 60 });
    f.push({ key: "enabled", label: "On", type: "switch" });
    return f;
  };
  const toMap = (text) => Object.fromEntries(lines(text).map((l) => l.split(/\s*=\s*/, 2).length === 2 ? [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()] : null).filter(Boolean));
  const fromMap = (m) => Object.entries(m || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
  const toBody = (adapter, v) => {
    const settings = {};
    for (const k of ["url", "list_path", "item_selector", "board", "company", "account"]) if (v[k]) settings[k] = v[k];
    if (v.split_company) settings.split_company = true;
    if (v.mapping) settings.fields = toMap(v.mapping);
    if (v.senders?.length) settings.senders = v.senders;
    if (v.subject_contains?.length) settings.subject_contains = v.subject_contains;
    return { label: v.label, adapter, kind: v.kind, enabled: v.enabled, watch: !!v.watch, interval_min: v.interval_min || 0, settings };
  };
  const toForm = (s) => ({ ...s, ...s.settings, mapping: fromMap(s.settings.fields), interval_min: s.interval_min || 0 });

  function detail(s) {
    const out = el("div", { class: "ui-stack" });
    const extra = el("div", { class: "ui-stack" }, el("p", { class: "ui-hint" }, describe(s)), out,
      el("div", { class: "le-foot" },
        ui.button({ label: "Test now", icon: "play", variant: "tonal", size: "sm", onClick: async () => {
          out.replaceChildren(el("p", { class: "ui-hint" }, "Testing..."));
          try { out.replaceChildren(resultNode(await api(`/api/oppsources/sources/${s.id}/test`, { method: "POST" }))); } catch (e) { out.replaceChildren(); fail(e); }
        } }),
        !["webhook", "link"].includes(s.adapter) ? ui.button({ label: "Run now", icon: "refresh", variant: "tonal", size: "sm", onClick: async () => {
          try { const r = await api(`/api/oppsources/sources/${s.id}/run`, { method: "POST" }); await reload(); good(`${r.results?.[0]?.new ?? 0} new.`); } catch (e) { fail(e); }
        } }) : null,
        s.adapter === "webhook" ? ui.button({ label: s.webhook?.has_secret ? "New secret" : "Make secret", icon: "key", variant: "tonal", size: "sm", onClick: async () => {
          try { const r = await api(`/api/oppsources/sources/${s.id}/webhook`, { method: "POST" }); out.replaceChildren(el("p", { class: "ui-hint" }, `Secret (shown once): ${r.secret}`), el("p", { class: "ui-hint" }, `${r.how} Address: ${r.path}`)); await reload(); } catch (e) { fail(e); }
        } }) : null));
    return extra;
  }

  ed = listEditor({
    title: "Your sources", items: data.sources, empty: "No sources yet. Add one from the catalogue, watch a company or add your own.",
    hint: "Nothing is switched on for you. Freelance and AI-task platforms are email-alert or open-link only, and nothing is ever submitted without your tap.",
    addLabel: "Add my own source", rowOf: (s) => ({ icon: s.watch ? "eye" : s.adapter === "email" ? "inbox" : "globe", tone: s.watch ? "orange" : "accent", title: s.label,
      sub: describe(s), enabled: s.enabled }),
    onToggle: async (s, on) => { await api(`/api/oppsources/sources/${s.id}`, { method: "PUT", json: { enabled: on } }); await reload(); },
    onEdit: (s) => formSheet({ title: s.label, tall: true, fields: fieldsFor(s.adapter), values: toForm(s), extra: detail(s),
      onSave: async (v) => { await api(`/api/oppsources/sources/${s.id}`, { method: "PUT", json: toBody(s.adapter, v) }); await reload(); good("Saved."); } }),
    onDelete: async (s) => { await api(`/api/oppsources/sources/${s.id}`, { method: "DELETE" }); await reload(); good("Deleted."); },
    onAdd: () => formSheet({ title: "Add my own source", fields: [{ key: "adapter", label: "Type", type: "select", options: ["rss", "json", "html", "email", "webhook"].map((a) => [a, ADAPTER_NAME[a]]) }],
      saveLabel: "Next", values: { adapter: "rss" }, onSave: (v) => { const a = v.adapter;
        setTimeout(() => formSheet({ title: ADAPTER_NAME[a], tall: true, fields: fieldsFor(a), values: { kind: "job", enabled: true, interval_min: 0 },
          onSave: async (b) => { await api("/api/oppsources/sources", { method: "POST", json: toBody(a, b) }); await reload(); good("Added."); } }), 50); } }),
  });
  head();
  ui.sheet({ title: "Job and gig sources", class: "st-sheet st-sheet--tall", content: el("div", { class: "ui-stack ui-stack--lg" }, headBox, ed.el,
    ui.notice("Freelance and AI-task platforms (Upwork, Fiverr, Freelancer, Toptal, Contra, data-annotation marketplaces) forbid bots, so REDLINE only reads their alert emails or a link you paste, prepares a proposal for you, and you submit by hand.", { tone: "info" })) });
}
