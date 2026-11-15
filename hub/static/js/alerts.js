// Alerts: urgent messages and tasks that Friday flagged, with a plan, an evidence list and a draft reply.
// Routes: docs/api/alerts.md. Everything shown is text (textContent), never HTML.
// HARD RULE: nothing is sent from here. "Copy" and "Open <app>" hand the draft to the OWNER, who taps send in that app.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { sourcesPanel } from "./sources.js";

const CSS = "/css/alerts.css";
const GROUPS = [["urgent", "Urgent"], ["today", "Today"], ["later", "Later"]];
const KIND = { task: "Task", question: "Question", meeting: "Meeting", mention: "Mention", other: "Message" };
const RULE_LABELS = [["urgent_words", "Urgent words", "Each one adds a lot of priority."], ["time_words", "Time-pressure words", "Deadlines and 'by when'."],
  ["ask_words", "Request words", "Phrases that mean someone wants something from you."], ["ignore_words", "Ignore words", "Marketing and codes. Ignored unless it is a mention, a VIP or a task."],
  ["mention_words", "Extra names for you", "Besides your display name. A message that says one of them counts as a mention."]];
const URGENCY = ["Low", "Today", "Urgent", "Critical"];
const TONE = ["gray", "info", "warn", "bad"];
const SNOOZES = [["15 minutes", 15], ["1 hour", 60], ["3 hours", 180], ["Tomorrow morning", 0]];
const SAFE_LINK = /^(https:\/\/wa\.me\/|intent:#Intent;|mailto:)/;
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const good = (text) => ui.toast(text, { kind: "good" });

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
  const area = el("textarea", { class: "al-offscreen", "aria-hidden": "true" });
  area.value = text; document.body.append(area); area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  area.remove();
  return ok;
}

function minutesToTomorrow() {
  const t = new Date(); t.setDate(t.getDate() + 1); t.setHours(9, 0, 0, 0);
  return Math.max(1, Math.round((t - Date.now()) / 60000));
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "al ui-stack ui-stack--lg" });
  container.append(root);
  let tab = "list", data = { alerts: [], counts: {}, settings: {} }, loadError = "", asana = null, timer = null, seq = 0, panel = null;
  const open = new Set();
  const head = el("div", { class: "al-head" });
  const body = el("div", { class: "ui-stack ui-stack--lg" });
  const seg = ui.segmented({ label: "Alerts or settings", value: "list", options: [{ value: "list", label: "Alerts" }, { value: "settings", label: "Settings" }],
    onChange: (v) => { tab = v; paint(); if (v === "settings") loadAsana(); } });
  root.append(el("div", { class: "al-top" }, el("h1", { class: "ui-title" }, "Alerts"), seg), head, body);

  async function load() {
    const mine = ++seq;
    try {
      const d = await api("/api/alerts?status=open");
      if (mine !== seq) return;
      data = d; loadError = "";
    } catch (error) {
      if (mine !== seq) return;
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paint();
  }
  async function loadAsana() { try { asana = await api("/api/asana/status"); } catch (_) { asana = null; } if (tab === "settings") paint(); }

  async function act(path, json, okText) {
    try { await api(path, { method: "POST", json }); if (okText) good(okText); await load(); } catch (error) { fail(error); }
  }

  function snoozeSheet(a) {
    const rows = SNOOZES.map(([label, mins]) => ui.listRow({ icon: "alarm", title: label, onClick: async () => {
      s.close(); await act(`/api/alerts/${a.id}/snooze`, { minutes: mins || minutesToTomorrow() }, "Snoozed. It will come back."); } }));
    const s = ui.sheet({ title: "Snooze for how long?", content: el("div", { class: "ui-list ui-list--inset" }, ...rows) });
  }

  function bullets(items) { return el("ul", { class: "al-list" }, ...items.map((t) => el("li", {}, t))); }

  function detail(a) {
    const parts = [];
    if (a.preview_only) parts.push(el("p", { class: "al-note" }, "Only the notification preview reached the hub. Open the app to read the whole message."));
    if (a.reasons?.length) parts.push(el("p", { class: "al-why" }, "Why: " + a.reasons.join("; ")));
    if (a.due) parts.push(el("p", { class: "al-why" }, "Due " + a.due.slice(0, 16).replace("T", " ")));
    if (a.plan?.length) parts.push(el("section", {}, el("p", { class: "ui-caps" }, "Plan"), el("ol", { class: "al-list" }, ...a.plan.map((s) => el("li", {}, s)))));
    if (a.evidence?.length) parts.push(el("section", {}, el("p", { class: "ui-caps" }, "You will need"), bullets(a.evidence)));
    if (a.reply_draft) {
      parts.push(el("section", { class: "ui-stack" }, el("p", { class: "ui-caps" }, "Draft reply (not sent)"),
        el("pre", { class: "al-draft" }, a.reply_draft),
        a.note ? el("p", { class: "al-note" }, a.note) : null));
    } else if (a.note) parts.push(el("p", { class: "al-note" }, a.note));
    const buttons = [];
    if (a.actions?.copy) buttons.push(ui.button({ label: "Copy reply", icon: "copy", variant: "primary", onClick: async () => good((await copyText(a.actions.copy)) ? "Copied. Paste it into the app and send it yourself." : "Could not copy. Select the text instead.") }));
    const link = a.actions?.open;
    if (link && SAFE_LINK.test(link.url)) {
      buttons.push(el("a", { class: "ui-btn ui-btn--secondary", href: link.url, target: "_blank", rel: "noopener noreferrer" }, ui.icon("share"), link.label));
    }
    if (a.source === "gchat") buttons.push(ui.button({ label: "Reply in Google Chat", icon: "talk", variant: "primary", onClick: () => import("./chatch.js").then((m) => m.replyToAlert(a)) }));
    if (a.actions?.save_note) buttons.push(ui.button({ label: "Save as note", icon: "note", onClick: () => act(`/api/alerts/${a.id}/save-note`, undefined, "Saved in Memory. Fill in the answer there.") }));
    if (a.task_id) buttons.push(ui.button({ label: "Open in Memory", icon: "book-open", variant: "ghost", onClick: () => ctx?.open?.("brain") }));
    if (a.kind !== "other") buttons.push(ui.button({ label: "New draft", icon: "refresh", variant: "ghost", onClick: () => act(`/api/alerts/${a.id}/draft`, undefined, "Draft refreshed.") }));
    buttons.push(ui.button({ label: "Handled", icon: "check-circle", variant: "tonal", onClick: () => act(`/api/alerts/${a.id}/status`, { status: "handled" }, "Marked handled.") }));
    buttons.push(ui.button({ label: "Snooze", icon: "alarm", variant: "ghost", onClick: () => snoozeSheet(a) }));
    parts.push(el("div", { class: "al-actions" }, ...buttons));
    return el("div", { class: "al-detail ui-stack" }, ...parts);
  }

  function card(a) {
    const isOpen = open.has(a.id);
    const top = el("button", { type: "button", class: "al-card-head", "aria-expanded": String(isOpen), onclick: () => {
      if (open.has(a.id)) open.delete(a.id);
      else { open.add(a.id); if (a.status === "new") api(`/api/alerts/${a.id}/status`, { method: "POST", json: { status: "seen" } }).catch(() => {}); }
      paint();
    } },
      el("span", { class: "al-badges" }, ui.badge(URGENCY[a.urgency] || "Low", TONE[a.urgency]), ui.badge(KIND[a.kind] || a.kind), a.status === "new" ? ui.badge("New", "accent") : null),
      el("span", { class: "al-sender" }, a.sender),
      el("span", { class: "al-preview" }, a.preview),
      el("span", { class: "al-meta" }, `${a.source_label} · ${ui.relTime(a.created)}`));
    return el("article", { class: "ui-card al-card" + (a.urgency >= 2 ? " is-urgent" : "") }, top, isOpen ? detail(a) : null);
  }

  function paintList() {
    if (loadError === "off") return [ui.emptyState({ icon: "bell", title: "Alerts are not installed on this hub" })];
    if (loadError) return [ui.notice(loadError, { tone: "bad" })];
    const out = [];
    for (const [id, label] of GROUPS) {
      const rows = (data.alerts || []).filter((a) => a.group === id);
      if (!rows.length) continue;
      out.push(el("section", { class: "ui-stack" }, ui.eyebrow(String(rows.length), label), ...rows.map(card)));
    }
    if (!out.length) out.push(ui.emptyState({ icon: "check-circle", title: "Nothing urgent", body: "Friday will tell you out loud and push to your phone the moment something is." }));
    return out;
  }

  // ---- settings ----
  async function saveSettings(json, okText) {
    try { data.settings = await api("/api/alerts/settings", { method: "POST", json }); if (okText) good(okText); paint(); } catch (error) { fail(error); }
  }

  function asanaCard() {
    const a = asana;
    const token = el("input", { class: "ui-input", type: "password", autocomplete: "off", placeholder: "Asana personal access token", "aria-label": "Asana personal access token" });
    const rows = [el("h3", { class: "ui-card__title" }, "Asana"),
      el("p", { class: "ui-card__sub" }, a?.connected ? `Connected${a.name ? " as " + a.name : ""}. Polls every ${a.poll_minutes} min while on.` : "Optional. Reads your assigned tasks and new comments (read-only). The token is kept in the OS keyring, never in a settings file.")];
    if (a?.connected) {
      if (a.workspaces?.length > 1) {
        const sel = el("select", { class: "ui-input", "aria-label": "Workspace" }, ...a.workspaces.map((w) => el("option", { value: w.gid, selected: w.gid === a.workspace }, w.name)));
        sel.addEventListener("change", async () => { try { asana = await api("/api/asana/workspace", { method: "POST", json: { gid: sel.value } }); good("Workspace set."); } catch (e) { fail(e); } });
        rows.push(el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Workspace"), sel));
      }
      rows.push(el("div", { class: "al-row" }, el("span", {}, "Poll Asana"), ui.switchControl({ checked: !!a.enabled, label: "Poll Asana",
        onChange: async (on) => { try { asana = await api("/api/asana/enable", { method: "POST", json: { enabled: on } }); } catch (e) { fail(e); } paint(); } })));
      if (a.last_error) rows.push(ui.notice(a.last_error, { tone: "warn" }));
      rows.push(el("div", { class: "al-actions" },
        ui.button({ label: "Test", icon: "zap", variant: "tonal", onClick: async (e) => { try { const r = await ui.busy(e.currentTarget, api("/api/asana/test", { method: "POST" })); asana = r; good(`Asana works${r.name ? " for " + r.name : ""}.`); paint(); } catch (er) { fail(er); loadAsana(); } } }),
        ui.button({ label: "Disconnect", icon: "trash", variant: "ghost", onClick: async () => {
          if (!(await ui.confirm({ title: "Disconnect Asana?", message: "The saved token is deleted from the keyring.", confirmLabel: "Disconnect", danger: true }))) return;
          try { asana = await api("/api/asana/disconnect", { method: "POST" }); paint(); } catch (er) { fail(er); } } })));
    } else {
      rows.push(el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Token"), token));
      rows.push(ui.button({ label: "Connect", icon: "link", variant: "primary", onClick: async (e) => {
        try { asana = await ui.busy(e.currentTarget, api("/api/asana/connect", { method: "POST", json: { token: token.value } })); token.value = ""; good("Asana connected."); paint(); } catch (er) { token.value = ""; fail(er); } } }));
    }
    return el("div", { class: "ui-card ui-stack al-set" }, ...rows);
  }

  const lines = (box) => box.value.split("\n").map((x) => x.trim()).filter(Boolean);

  function paintSettings() {
    const s = data.settings || {};
    const vips = el("textarea", { class: "ui-input al-vips", rows: "4", "aria-label": "VIP senders", placeholder: "One per line: a name, a phone number or an address" });
    vips.value = (s.vips || []).join("\n");
    const mk = (rows, label) => { const t = el("textarea", { class: "ui-input al-vips", rows: String(rows), "aria-label": label }); return t; };
    const ruleBoxes = {};
    for (const [key, label] of RULE_LABELS) { ruleBoxes[key] = mk(4, label); ruleBoxes[key].value = ((s.rules || {})[key] || []).join("\n"); }
    return [
      el("div", { class: "ui-card ui-stack al-set" }, el("h3", { class: "ui-card__title" }, "Who always matters"),
        el("p", { class: "ui-card__sub" }, "Your manager and other important people. A message from them that asks for something is always urgent."), vips,
        ui.button({ label: "Save VIPs", icon: "check-circle", variant: "primary", onClick: () => saveSettings({ vips: vips.value.split("\n").map((x) => x.trim()).filter(Boolean) }, "VIPs saved.") })),
      (panel ||= sourcesPanel({ onChanged: () => api("/api/alerts/settings").then((v) => { data.settings = v; }).catch(() => {}) })),
      el("div", { class: "ui-card ui-stack al-set" }, el("h3", { class: "ui-card__title" }, "Rules"),
        el("p", { class: "ui-card__sub" }, "Priority is worked out from who sent it (VIP list), the words and the time pressure. Edit the lists, one word or phrase per line."),
        ...RULE_LABELS.map(([key, label, sub]) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), el("span", { class: "al-note" }, sub), ruleBoxes[key])),
        el("div", { class: "al-actions" },
          ui.button({ label: "Save rules", icon: "check-circle", variant: "primary", onClick: () => saveSettings({ rules: Object.fromEntries(RULE_LABELS.map(([k]) => [k, lines(ruleBoxes[k])])) }, "Rules saved.") }),
          ui.button({ label: "Reset to defaults", icon: "refresh", variant: "ghost", onClick: () => saveSettings({ reset_rules: true }, "Rules reset.") }))),
      el("div", { class: "ui-card ui-stack al-set" }, el("h3", { class: "ui-card__title" }, "Out loud"),
        el("div", { class: "al-row" }, el("span", {}, "Say urgent alerts on the laptop"), ui.switchControl({ checked: !!s.speak_on_laptop, label: "Say urgent alerts on the laptop",
          onChange: (on) => saveSettings({ speak_on_laptop: on }) })),
        el("p", { class: "ui-card__sub" }, `The phone reads them aloud while this app is open. Quiet hours (${s.quiet_start || "22:00"} to ${s.quiet_end || "07:00"}, set in Settings > Notifications) hold everything except critical alerts.`)),
      el("div", { class: "ui-card ui-stack al-set" }, el("h3", { class: "ui-card__title" }, "Test it end to end"),
        el("p", { class: "ui-card__sub" }, "Fires a fake critical alert: classification, plan, push and voice. Nothing is sent to anyone."),
        ui.button({ label: "Test alert", icon: "bell", variant: "tonal", onClick: async (e) => {
          try { const r = await ui.busy(e.currentTarget, api("/api/alerts/test", { method: "POST" })); good(`Test alert raised (${r.delivery}).`); tab = "list"; seg.value = "list"; await load(); } catch (er) { fail(er); } } })),
      el("div", { class: "ui-card ui-stack al-set" }, el("h3", { class: "ui-card__title" }, "What the phone can and cannot see"),
        el("p", { class: "ui-card__sub" }, "Android only shows the hub the preview line of a notification, and only for apps the capture listens to. If you force-stop the REDLINE app, Android stops the capture until you open it again. Nothing is ever replied for you: you copy the draft and send it yourself.")),
    ];
  }

  function paint() {
    head.replaceChildren(tab === "list" && data.total_open ? el("p", { class: "al-count" }, `${data.counts?.urgent || 0} urgent · ${data.counts?.today || 0} today · ${data.counts?.later || 0} later`) : "");
    body.replaceChildren(...(tab === "list" ? paintList() : paintSettings()));
  }

  paint();
  load();
  const tick = () => { if (!document.hidden && tab === "list") load(); };
  return {
    show() { load(); clearInterval(timer); timer = setInterval(tick, 30000); },
    hide() { clearInterval(timer); timer = null; },
    refresh() { return load(); },
    openSection(name) { if (name === "settings") { tab = "settings"; seg.value = "settings"; paint(); loadAsana(); } },
  };
}
