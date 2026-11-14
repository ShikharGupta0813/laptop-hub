// Inbound sources panel (Alerts > Settings): every place REDLINE listens to, as lists you can add to, switch off, test and delete.
// Routes: docs/api/sources.md. All text goes through el()/textContent, never HTML. Secrets are typed into password fields,
// sent once, and never shown again (the server only says has_secret).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const good = (text) => ui.toast(text, { kind: "good" });
const field = (label, input, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (attrs = {}) => el("input", { class: "ui-input", autocomplete: "off", ...attrs });
const randomSecret = () => { const a = new Uint8Array(24); crypto.getRandomValues(a); return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join(""); };

export function sourcesPanel({ onChanged } = {}) {
  const root = el("div", { class: "ui-stack ui-stack--lg" }, ui.skeleton("list", 3));
  let data = null;

  async function load() {
    try { data = await api("/api/sources"); } catch (error) {
      root.replaceChildren(ui.notice(error?.message || "Sources are not available.", { tone: "bad" }));
      return;
    }
    paint();
  }
  const changed = async () => { await load(); onChanged?.(); };
  const call = async (path, options, okText) => { try { const r = await api(path, options); if (okText) good(okText); await changed(); return r; } catch (error) { fail(error); return null; } };

  function row({ title, sub, badges = [], enabled, onToggle, onTest, onDelete, onEdit, deleteText }) {
    const actions = [];
    if (onTest) actions.push(ui.button({ label: "Test", icon: "zap", variant: "tonal", onClick: async (e) => { try { const r = await ui.busy(e.currentTarget, onTest()); good(r?.message || "Works."); } catch (error) { fail(error); } } }));
    if (onEdit) actions.push(ui.button({ label: "Edit", icon: "edit", variant: "ghost", onClick: onEdit }));
    if (onDelete) actions.push(ui.button({ label: "Delete", icon: "trash", variant: "ghost", onClick: async () => {
      if (!(await ui.confirm({ title: `Delete ${title}?`, message: deleteText || "It is removed from the list. Saved secrets are deleted from the keyring.", confirmLabel: "Delete", danger: true }))) return;
      onDelete(); } }));
    return el("div", { class: "ui-stack src-row" },
      el("div", { class: "al-row" }, el("span", { class: "src-title" }, title, ...badges.map((b) => ui.badge(b.text, b.tone))),
        onToggle ? ui.switchControl({ checked: !!enabled, label: `Listen to ${title}`, onChange: onToggle }) : null),
      sub ? el("p", { class: "al-note" }, sub) : null,
      actions.length ? el("div", { class: "al-actions" }, ...actions) : null);
  }

  function section(title, sub, rows, addLabel, onAdd) {
    return el("div", { class: "ui-card ui-stack al-set" }, el("h3", { class: "ui-card__title" }, title), el("p", { class: "ui-card__sub" }, sub),
      ...rows, ui.button({ label: addLabel, icon: "plus", variant: "secondary", onClick: onAdd }));
  }

  // ---- apps ----
  function appSheet() {
    const label = input({ placeholder: "Name, e.g. Signal", maxlength: "60" });
    const pkg = input({ placeholder: "com.example.app", maxlength: "120" });
    ui.sheet({ title: "Add a notification app", class: "st-sheet", content: el("div", { class: "ui-stack" },
      ui.notice("The phone app listens to the apps on this list. Find a package name in the app's Play Store address (the id= part).", { tone: "info" }),
      field("Name", label), field("Package name", pkg)),
    actions: [{ label: "Add", variant: "primary", onClick: async () => { const r = await call("/api/sources/app", { method: "POST", json: { label: label.value, package: pkg.value } }, "Added. Open the phone app once to refresh its list."); return !!r; } }] });
  }
  const apps = () => section("Notification apps and SMS", "The phone app reads these apps' notification previews. Switch one off to stop listening to it.",
    data.app.items.map((a) => row({ title: a.label, sub: a.package || "any app whose name contains this word", enabled: a.enabled,
      onToggle: (on) => call(`/api/sources/app/${a.id}`, { method: "PUT", json: { enabled: on } }),
      onDelete: () => call(`/api/sources/app/${a.id}`, { method: "DELETE" }) })), "Add an app", appSheet);

  // ---- tasks and webhooks ----
  function taskSheet(existing) {
    const kinds = data.task.kinds || {};
    const kind = el("select", { class: "ui-input", "aria-label": "Kind" }, ...Object.entries(kinds).map(([k, v]) => el("option", { value: k }, v.label)));
    const label = input({ maxlength: "60", placeholder: "Name" });
    const fields = el("div", { class: "ui-stack" });
    const secret = input({ type: "password", placeholder: "Token or key" });
    const poll = input({ type: "number", min: "1", max: "120", placeholder: "Check every N minutes (default 5)" });
    const note = el("p", { class: "ui-hint" }, "");
    let inputs = {};
    const hookBox = el("div", { class: "ui-stack" });
    function draw() {
      const spec = kinds[kind.value] || { fields: [] };
      inputs = {};
      fields.replaceChildren(...spec.fields.map((f) => { inputs[f.name] = input({ placeholder: f.label, value: existing?.settings?.[f.name] || "" }); return field(f.label, inputs[f.name]); }));
      hookBox.replaceChildren();
      if (spec.secret_label) {
        secret.placeholder = existing ? "Leave empty to keep the saved one" : spec.secret_label;
        hookBox.append(field(spec.secret_label, secret, kind.value === "webhook" ? "Anything that can POST JSON can use it. The secret is shown here once; the hub keeps it only in the OS keyring." : "Saved in the OS keyring, never in a settings file."));
        if (kind.value === "webhook") hookBox.append(ui.button({ label: "Generate a secret", icon: "refresh", variant: "tonal", onClick: () => { secret.type = "text"; secret.value = randomSecret(); } }));
      }
      poll.style.display = spec.poll ? "" : "none";
      note.textContent = kind.value === "webhook" ? "After saving, POST {title, text, sender, url} to /hooks/<id> with the header X-Webhook-Secret." : "";
    }
    kind.addEventListener("change", draw);
    if (existing) { kind.value = existing.kind; kind.disabled = true; label.value = existing.label; }
    draw();
    ui.sheet({ title: existing ? `Edit ${existing.label}` : "Add a task source", class: "st-sheet", content: el("div", { class: "ui-stack" },
      field("Kind", kind), field("Name", label), fields, hookBox, field("Poll", poll), note),
    actions: [{ label: existing ? "Save" : "Add", variant: "primary", onClick: async () => {
      const settings = Object.fromEntries(Object.entries(inputs).map(([k, v]) => [k, v.value.trim()]).filter(([, v]) => v !== "" || existing));
      if (poll.value) settings.poll_minutes = Number(poll.value);
      const body = { label: label.value, settings };
      if (secret.value) body.secret = secret.value;
      const r = existing
        ? await call(`/api/sources/task/${existing.id}`, { method: "PUT", json: body }, "Saved.")
        : await call("/api/sources/task", { method: "POST", json: { ...body, kind: kind.value } }, "Added.");
      if (r && !existing && r.kind === "webhook") ui.toast(`Webhook ready: POST /hooks/${r.id}`, { kind: "good" });
      return !!r; } }] });
  }
  const tasks = () => section("Task sources and webhooks", "Asana, Jira, Trello, Linear, GitHub, any RSS or Atom feed, or an inbound webhook for any other tool. New items become alerts when they sound urgent.",
    data.task.items.map((t) => row({ title: t.label, enabled: t.enabled,
      badges: [{ text: t.kind_label, tone: "accent" }, { text: t.has_secret || t.kind === "rss" ? "ready" : "needs token", tone: t.has_secret || t.kind === "rss" ? "good" : "warn" }],
      sub: [t.hook_path ? `POST ${t.hook_path}` : "", t.last_error ? `Last problem: ${t.last_error}` : (t.last_poll ? `Checked ${ui.relTime(t.last_poll)}` : "")].filter(Boolean).join(" · "),
      onToggle: (on) => call(`/api/sources/task/${t.id}`, { method: "PUT", json: { enabled: on } }),
      onTest: () => api(`/api/sources/task/${t.id}/test`, { method: "POST" }), onEdit: () => taskSheet(t),
      onDelete: () => call(`/api/sources/task/${t.id}`, { method: "DELETE" }),
      deleteText: t.builtin ? "The ready-made Asana entry is signed out and switched off, not removed." : undefined })), "Add a task source or webhook", () => taskSheet(null));

  // ---- calendars ----
  function calSheet(existing) {
    const label = input({ maxlength: "40", placeholder: "Name, e.g. Company", value: existing?.label || "" });
    const url = input({ type: "password", placeholder: existing ? "Leave empty to keep the saved address" : "https://... or webcal://... (.ics)" });
    const color = input({ type: "color", value: existing?.color || "#4285f4", "aria-label": "Colour" });
    ui.sheet({ title: existing ? `Edit ${existing.label}` : "Add a calendar", class: "st-sheet", content: el("div", { class: "ui-stack" },
      existing?.kind === "google" ? ui.notice("This one is the Google calendar from the Jarvis setup. You can rename, colour or switch it off.", { tone: "info" })
        : ui.notice("Paste the iCal address: Google Calendar's \"Secret address in iCal format\", Outlook's published calendar link, an Apple or company calendar link. It is kept in the OS keyring.", { tone: "info" }),
      field("Name", label), existing?.kind === "google" ? null : field("Address", url), field("Colour", color)),
    actions: [{ label: existing ? "Save" : "Add", variant: "primary", onClick: async () => {
      const body = { label: label.value, color: color.value };
      if (url.value) body.url = url.value;
      const r = existing ? await call(`/api/sources/calendar/${existing.id}`, { method: "PUT", json: body }, "Saved.") : await call("/api/sources/calendar", { method: "POST", json: body }, "Added.");
      return !!r; } }] });
  }
  const calendarsCard = () => section("Calendars", "Today, the front page and the 10-minute meeting reminder read all the calendars that are switched on.",
    data.calendar.items.map((c) => row({ title: c.label, enabled: c.enabled, badges: [{ text: c.kind === "google" ? "Google" : "iCal", tone: "accent" }],
      sub: c.last_error || c.address_hint || "", onToggle: (on) => call(`/api/sources/calendar/${c.id}`, { method: "PUT", json: { enabled: on } }),
      onTest: () => api(`/api/sources/calendar/${c.id}/test`, { method: "POST" }), onEdit: () => calSheet(c),
      onDelete: c.builtin ? null : () => call(`/api/sources/calendar/${c.id}`, { method: "DELETE" }) })), "Add a calendar", () => calSheet(null));

  // ---- mail ----
  const mailCard = () => section("Mail accounts", "Whether each account takes part in urgent-alert mail checks. Add, tag and edit accounts in Settings > Mail accounts.",
    data.mail.items.map((m) => row({ title: m.label, sub: `${m.address || ""}${m.tags?.length ? " · " + m.tags.join(", ") : ""}`,
      badges: [{ text: m.kind_label || m.kind, tone: "accent" }, { text: m.connected ? "connected" : "not connected", tone: m.connected ? "good" : "warn" }],
      enabled: m.alerts, onToggle: (on) => call(`/api/sources/mail/${m.id}`, { method: "PUT", json: { alerts: on } }),
      onTest: m.connected ? () => api(`/api/sources/mail/${m.id}/test`, { method: "POST" }) : null })), "Add a mail account", () => { location.hash = "#/settings"; });

  function paint() {
    root.replaceChildren(el("h2", { class: "ui-caps" }, "Everything REDLINE listens to"), apps(), tasks(), calendarsCard(), mailCard());
  }
  load();
  root.refresh = load;
  return root;
}
