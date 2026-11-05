// Watch: alerts you about important mail and chat. Monitors, rules in plain English, and an alert history.
// Routes: docs/api/watch.md. Chat monitoring also asks the Android app (HubCapture.setWatch) to start forwarding.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/watch.css";
const INTERVALS = [[60, "1 minute"], [120, "2 minutes"], [300, "5 minutes"], [600, "10 minutes"], [900, "15 minutes"]];
const LEVELS = [{ value: "info", label: "Info" }, { value: "warn", label: "Warning" }, { value: "error", label: "Urgent" }];
const LEVEL_WORD = { info: "a normal notification", warn: "a warning", error: "an urgent alert" };
const BY_LABEL = { ai: "AI judged", keywords: "Keywords", rule: "Rule" };

const capture = () => { try { return window.Capacitor?.Plugins?.HubCapture || window.HubCapture || null; } catch (_) { return null; } };
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const quote = (words) => words.map((w) => `“${w}”`).join(", ");
const toList = (text) => String(text || "").split(/[,\n]/).map((s) => s.trim()).filter(Boolean).slice(0, 20);

// "When an email from “boss” or mentioning “asap” arrives, alert me with a warning."
export function describeRule(rule) {
  const m = rule.match || {};
  const noun = rule.source === "chat" ? "a chat message" : rule.source === "mail" ? "an email" : "an email or chat message";
  const bits = [];
  if (m.from_contains?.length) bits.push(`from ${quote(m.from_contains)}`);
  if (m.subject_contains?.length) bits.push(`with ${quote(m.subject_contains)} in the subject`);
  if (m.keywords?.length) bits.push(`mentioning ${quote(m.keywords)}`);
  if (m.chat_space_contains?.length && rule.source !== "mail") bits.push(`in a space named ${quote(m.chat_space_contains)}`);
  const joiner = m.any_of === false ? " and " : " or ";
  let text = bits.length ? `When ${noun} ${bits.join(joiner)} arrives` : `When ${noun} arrives`;
  if (rule.ai_urgent) text = bits.length ? `${text}, or the AI thinks it's urgent` : `When the AI thinks ${noun} is urgent`;
  return `${text}, alert me with ${LEVEL_WORD[rule.level] || "a notification"}.`;
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "wt ui-stack ui-stack--lg" });
  container.append(root);
  let status = null, rules = null, alerts = null;
  let missing = false, loadError = "";
  let highlight = null;
  const sections = {};   // name -> node, for openSection

  // ---- data ----------------------------------------------------------------------------------------
  async function load() {
    if (!status) root.replaceChildren(ui.skeleton("card", 1), ui.skeleton("list", 3));
    try {
      [status, rules, alerts] = await Promise.all([
        api("/api/watch/status"),
        api("/api/watch/rules").then((d) => d.rules || []),
        api("/api/watch/alerts?limit=50").then((d) => d.alerts || []),
      ]);
      missing = false; loadError = "";
      const hub = capture();
      if (hub?.setWatch) hub.setWatch({ chat: !!status.chat_monitor }).catch(() => {});
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) missing = true;
      else loadError = error.message;
    }
    paint();
  }

  async function saveSettings(body, revert) {
    try { status = { ...status, ...(await api("/api/watch/settings", { method: "POST", json: body })) }; paint(); }
    catch (error) { fail(error); revert?.(); paint(); }
  }

  // ---- pieces --------------------------------------------------------------------------------------
  function switchRow({ icon, tone, title, sub, checked, onChange }) {
    const sw = ui.switchControl({ checked, onChange, label: title });
    const row = el("div", { class: "ui-row ui-row--interactive wt-switchrow" }, ui.tile(icon, tone),
      el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, title), sub ? el("span", { class: "ui-row__sub" }, sub) : null), sw);
    row.addEventListener("click", (e) => { if (!sw.contains(e.target)) sw.input.click(); });
    row.switch = sw;
    return row;
  }

  function statusCard() {
    const on = status.mail_monitor || status.chat_monitor;
    const parts = [];
    if (status.last_check) parts.push(`Last checked ${ui.relTime(status.last_check)}`);
    else if (status.mail_monitor) parts.push("Not checked yet");
    parts.push(`${status.alerts_today || 0} alert${status.alerts_today === 1 ? "" : "s"} today`);
    const check = ui.button({ label: "Check now", icon: "refresh", variant: "tonal", size: "sm", disabled: !status.mail_monitor });
    check.addEventListener("click", () => ui.busy(check, async () => {
      try {
        const r = await api("/api/watch/check-now", { method: "POST", json: {} });
        ui.toast(r.error ? r.error : r.new ? `${r.new} new mail checked, ${r.alerts || 0} alert${r.alerts === 1 ? "" : "s"}.` : "Checked. Nothing new.", { kind: r.error ? "warn" : "good" });
        await load();
      } catch (error) { fail(error); }
    }));
    return el("div", { class: "ui-card wt-hero" },
      el("div", { class: "wt-hero__top" }, el("span", { class: "wt-pulse" + (on ? " is-on" : ""), "aria-hidden": "true" }, ui.icon("eye")),
        el("div", { class: "grow" }, el("h2", { class: "ui-card__title" }, on ? "Watching for you" : "Watching is off"),
          el("p", { class: "ui-card__sub", role: "status" }, parts.join(" · ")))),
      check);
  }

  function notices() {
    const out = [];
    if (status.last_error) out.push(ui.notice(status.last_error, { tone: "warn" }));
    if (status.mail_monitor && !status.mail_configured && !status.last_error) out.push(ui.notice("Mail isn't connected yet, so nothing can be checked.", { tone: "warn" }));
    if (status.mail_monitor && !status.mail_configured) {
      const note = out[out.length - 1];
      note.append(ui.button({ label: "Open Inbox", variant: "tonal", size: "sm", onClick: () => ctx.open("inbox") }));
      note.classList.add("wt-notice");
    }
    if (!status.mail_monitor && !status.chat_monitor) out.push(ui.notice("Turn on mail or chat watching below. Until then your rules do nothing.", { icon: "info" }));
    return out;
  }

  function monitors() {
    const mail = switchRow({ icon: "mail", tone: "info", title: "Watch my mail", sub: "Checks your inbox", checked: !!status.mail_monitor,
      onChange: (on) => saveSettings({ mail_monitor: on }, () => { status.mail_monitor = !on; }) });
    const hub = capture();
    const chat = switchRow({ icon: "talk", tone: "teal", title: "Watch Google Chat", sub: hub ? "Reads Chat notifications" : "Needs the Android app", checked: !!status.chat_monitor,
      onChange: async (on) => {
        if (on && !hub) ui.toast("The hub will accept chat alerts, but only the Android app can send them.", { kind: "warn" });
        try { if (hub?.setWatch) { const s = await hub.setWatch({ chat: on }); if (on && s && s.listenerEnabled === false) ui.toast("Turn on Notification access in Settings, Phone capture.", { kind: "warn" }); } }
        catch (error) { fail(error); }
        saveSettings({ chat_monitor: on }, () => { status.chat_monitor = !on; });
      } });
    const select = el("select", { "aria-label": "How often to check mail" }, INTERVALS.map(([v, l]) => el("option", { value: String(v), selected: v === status.interval_seconds }, l)));
    if (!INTERVALS.some(([v]) => v === status.interval_seconds)) select.prepend(el("option", { value: String(status.interval_seconds), selected: true }, `${Math.round(status.interval_seconds / 60)} min`));
    select.addEventListener("change", () => saveSettings({ interval_seconds: Number(select.value) }));
    const interval = el("div", { class: "ui-row" }, ui.tile("clock", "gray"), el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Check every")), select);

    const q = status.quiet_hours;
    const start = el("input", { type: "time", value: q?.start || "22:00", "aria-label": "Quiet from" });
    const end = el("input", { type: "time", value: q?.end || "07:00", "aria-label": "Quiet until" });
    const times = el("div", { class: "ui-row wt-times", hidden: !q?.enabled },
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "From"), start), el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Until"), end));
    const saveQuiet = () => saveSettings({ quiet_hours: { enabled: true, start: start.value, end: end.value } });
    start.addEventListener("change", saveQuiet); end.addEventListener("change", saveQuiet);
    const quiet = switchRow({ icon: "moon", tone: "violet", title: "Quiet hours", sub: "Saved quietly, no buzz", checked: !!q?.enabled,
      onChange: (on) => { times.hidden = !on; saveSettings({ quiet_hours: on ? { enabled: true, start: start.value, end: end.value } : null }); } });

    const model = ui.segmented({ label: "Where urgency checks run", block: true, value: status.model || "local",
      options: [{ value: "local", label: "On my laptop" }, { value: "claude", label: "Claude" }],
      onChange: (v) => saveSettings({ model: v }) });
    return el("section", { class: "wt-section" }, el("h2", { class: "ui-caps wt-caps" }, "Monitoring"),
      el("div", { class: "ui-list ui-list--inset ui-list--icons" }, mail, chat, interval, quiet, times),
      el("div", { class: "wt-model" }, el("p", { class: "ui-hint" }, "The AI reads new mail to spot urgent ones. Where should it do that?"), model,
        status.model === "claude" ? el("p", { class: "ui-hint wt-warnhint" }, "With Claude, clipped mail text leaves your laptop.") : null));
  }

  function ruleCard(rule) {
    const level = rule.level === "error" ? "bad" : rule.level === "warn" ? "warn" : "accent";
    const sw = ui.switchControl({ checked: !!rule.enabled, label: `${rule.name} enabled`,
      onChange: async (on) => {
        try { const r = await api(`/api/watch/rules/${encodeURIComponent(rule.id)}`, { method: "PUT", json: { enabled: on } }); Object.assign(rule, r.rule || r, { enabled: on }); card.classList.toggle("is-off", !on); }
        catch (error) { sw.input.checked = !on; fail(error); }
      } });
    const open = el("button", { type: "button", class: "wt-rule__body" },
      el("span", { class: "wt-rule__name" }, rule.name, rule.builtin ? ui.badge("Built in", "accent") : null),
      el("span", { class: "wt-rule__text" }, describeRule(rule)),
      el("span", { class: "wt-rule__tags" },
        el("span", { class: `ui-badge ui-badge--${level}` }, rule.level === "error" ? "Urgent" : rule.level === "warn" ? "Warning" : "Info"),
        el("span", { class: "ui-badge" }, rule.source === "mail" ? "Mail" : rule.source === "chat" ? "Chat" : "Mail and chat"),
        rule.quiet_override ? el("span", { class: "ui-badge" }, "Ignores quiet hours") : null));
    open.addEventListener("click", () => editRule(rule));
    const card = el("div", { class: "ui-card wt-rule" + (rule.enabled ? "" : " is-off"), "data-tone": level }, open, sw);
    return card;
  }

  function rulesSection() {
    const add = ui.button({ label: "Add rule", icon: "plus", variant: "tonal", size: "sm", onClick: () => editRule(null) });
    return el("section", { class: "wt-section" }, el("div", { class: "ui-section-head" }, el("h2", { class: "ui-caps wt-caps" }, "Rules"), add),
      el("div", { class: "ui-stack" }, rules.length ? rules.map(ruleCard) : ui.emptyState({ icon: "filter", title: "No rules yet", body: "A rule says what is worth an alert, like mail from your boss.", action: { label: "Add a rule", icon: "plus", onClick: () => editRule(null) }, compact: true })));
  }

  function alertsSection() {
    const rows = alerts.map((a) => {
      const tone = a.level === "error" ? "bad" : a.level === "warn" ? "warn" : "info";
            const sub = [a.from, a.reason || a.rule].filter(Boolean).join(" · ");
      const row = ui.listRow({ icon: a.source === "chat" ? "talk" : "mail", tone, title: a.subject || a.snippet || "(no subject)", sub, chevron: !!a.link,
        onClick: a.link ? () => ctx.open(a.link, undefined, a.uid != null ? { id: a.uid } : {}) : undefined, attrs: { "data-id": String(a.id) } });
      const tags = el("span", { class: "wt-rule__tags" }, el("span", { class: "wt-alert__meta" }, ui.relTime(a.ts)), a.by ? el("span", { class: "ui-badge" }, BY_LABEL[a.by] || a.by) : null,
        a.quiet ? ui.badge("Held back, quiet hours", "warn") : null, a.rule && a.by === "rule" ? el("span", { class: "ui-badge" }, a.rule) : null);
      row.querySelector(".ui-row__body").append(tags);
      if (String(highlight) === String(a.id)) { row.classList.add("wt-flash"); highlight = null; requestAnimationFrame(() => row.scrollIntoView({ block: "center" })); }
      return row;
    });
    return el("section", { class: "wt-section" }, el("h2", { class: "ui-caps wt-caps" }, "Recent alerts"),
      alerts.length ? el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows)
        : ui.emptyState({ icon: "bell", title: "No alerts yet", body: status.mail_monitor || status.chat_monitor ? "When something matches a rule, it appears here." : "Turn on watching and alerts will collect here.", compact: true }));
  }

  function paint() {
    if (missing) { root.replaceChildren(ui.emptyState({ icon: "eye", title: "Watch isn't available", body: "This hub doesn't have the Watch service yet." })); return; }
    if (loadError || !status) {
      root.replaceChildren(ui.notice(loadError || "Couldn't load Watch.", { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load }));
      return;
    }
    const nodes = { status: statusCard(), monitors: monitors(), rules: rulesSection(), alerts: alertsSection() };
    Object.assign(sections, nodes);
    root.replaceChildren(nodes.status, ...notices(), nodes.monitors, nodes.rules, nodes.alerts);
  }

  // ---- rule editor ---------------------------------------------------------------------------------
  function editRule(rule) {
    const isNew = !rule;
    const builtin = !!rule?.builtin;
    const m = rule?.match || {};
    const f = { name: rule?.name || "", source: rule?.source || "mail", from: (m.from_contains || []).join(", "), subject: (m.subject_contains || []).join(", "),
      keywords: (m.keywords || []).join(", "), space: (m.chat_space_contains || []).join(", "), any_of: m.any_of !== false, ai: !!rule?.ai_urgent,
      quiet: !!rule?.quiet_override, level: rule?.level || "warn" };
    const field = (label, key, hint, ph) => {
      const input = el("input", { type: "text", value: f[key], placeholder: ph || "", autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": label });
      input.addEventListener("input", () => { f[key] = input.value; refresh(); });
      return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input, hint ? el("span", { class: "ui-hint" }, hint) : null);
    };
    const space = field("Chat space or person", "space", "Part of the chat's name, like “Project Atlas”.", "Project Atlas");
    const preview = el("p", { class: "wt-preview" });
    const build = () => ({
      name: f.name.trim() || "Untitled rule", source: f.source, level: f.level, ai_urgent: f.ai, quiet_override: f.quiet,
      match: { from_contains: toList(f.from), subject_contains: toList(f.subject), keywords: toList(f.keywords), any_of: f.any_of,
        chat_space_contains: f.source === "mail" ? [] : toList(f.space) },
    });
    const refresh = () => { space.hidden = f.source === "mail"; preview.textContent = describeRule(build()); };
    const source = ui.segmented({ label: "Applies to", block: true, value: f.source, options: [{ value: "mail", label: "Mail" }, { value: "chat", label: "Chat" }, { value: "any", label: "Both" }],
      onChange: (v) => { f.source = v; refresh(); } });
    const mode = ui.segmented({ label: "How to match", block: true, value: f.any_of ? "any" : "all", options: [{ value: "any", label: "Any of these" }, { value: "all", label: "All of these" }],
      onChange: (v) => { f.any_of = v === "any"; refresh(); } });
    const level = ui.segmented({ label: "Alert level", block: true, value: f.level, options: LEVELS, onChange: (v) => { f.level = v; refresh(); } });
    const ai = switchRow({ icon: "sparkles", tone: "violet", title: "Also alert when the AI thinks it's urgent", sub: "Even if no words match", checked: f.ai, onChange: (on) => { f.ai = on; refresh(); } });
    const quiet = switchRow({ icon: "moon", tone: "gray", title: "Alert even in quiet hours", checked: f.quiet, onChange: (on) => { f.quiet = on; refresh(); } });

    // Test box: nothing is stored and the model is never asked.
    const t = { from: "", subject: "", text: "", space: "" };
    const tin = (label, key, tag = "input") => {
      const node = el(tag, { ...(tag === "input" ? { type: "text" } : { rows: "2" }), "aria-label": label, placeholder: label, autocomplete: "off" });
      node.addEventListener("input", () => { t[key] = node.value; });
      return node;
    };
    const verdict = el("div", { class: "wt-verdict", "aria-live": "polite" });
    const run = ui.button({ label: "Try it", icon: "play", variant: "tonal", size: "sm" });
    run.addEventListener("click", () => ui.busy(run, async () => {
      try {
        const rb = build();
        const r = await api("/api/watch/rules/test", { method: "POST", json: { rule: rb, sample: { from: t.from, subject: t.subject, text: t.text, ...(t.space ? { space: t.space } : {}), source: f.source === "any" ? "mail" : f.source } } });
        verdict.replaceChildren(ui.notice(r.would_match ? ["This would alert you.", ...(r.reasons || [])].join(" ") : `This would not alert you.${r.ai_urgent ? " The AI may still flag it if it looks urgent." : ""}`, { tone: r.would_match ? "good" : null, icon: r.would_match ? "check-circle" : "info" }),
          r.note ? el("p", { class: "ui-hint" }, r.note) : null);
      } catch (error) { verdict.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
    }));
    const tester = el("details", { class: "ui-card wt-test" }, el("summary", {}, ui.icon("wand"), "Test this rule"),
      el("div", { class: "ui-stack" }, el("p", { class: "ui-hint" }, "Type a sample message. Nothing is saved or sent."),
        tin("From (name or address)", "from"), tin("Subject", "subject"), tin("Message text", "text", "textarea"), tin("Chat space (chat only)", "space"), run, verdict));

    const body = el("div", { class: "ui-stack ui-stack--lg" },
      builtin ? ui.notice("This is the built-in rule. You can turn it on or off and change how loud it is.", { icon: "info" }) : null,
      builtin ? null : field("Name", "name", null, "Boss mail"),
      builtin ? null : el("div", { class: "ui-stack" }, el("p", { class: "ui-caps wt-caps" }, "Applies to"), source),
      builtin ? null : el("div", { class: "ui-stack" }, el("p", { class: "ui-caps wt-caps" }, "When the message has"),
        field("From", "from", "Names or addresses, separated by commas.", "boss, hr@acme.com"), field("Subject contains", "subject", null, "invoice, interview"),
        field("Words anywhere", "keywords", null, "asap, deadline"), space, mode),
      el("div", { class: "ui-list ui-list--inset ui-list--icons" }, builtin ? null : ai, quiet),
      el("div", { class: "ui-stack" }, el("p", { class: "ui-caps wt-caps" }, "Alert me with"), level),
      builtin ? null : preview, builtin ? null : tester,
      !isNew && !builtin ? ui.button({ label: "Delete rule", icon: "trash", variant: "danger-soft", block: true, onClick: async () => {
        if (!await ui.confirm({ title: `Delete “${rule.name}”?`, message: "Alerts it already raised stay in your history.", confirmLabel: "Delete", danger: true })) return;
        try { await api(`/api/watch/rules/${encodeURIComponent(rule.id)}`, { method: "DELETE" }); s.close(); ui.toast("Rule deleted.", { kind: "good" }); load(); } catch (error) { fail(error); }
      } }) : null);
    const s = ui.sheet({ title: isNew ? "New rule" : builtin ? "Built-in rule" : "Edit rule", content: body, class: "wt-sheet", actions: [
      { label: isNew ? "Add rule" : "Save", variant: "primary", icon: "check", onClick: async () => {
        const rb = build();
        if (!builtin && !rb.ai_urgent && !rb.match.from_contains.length && !rb.match.subject_contains.length && !rb.match.keywords.length && !rb.match.chat_space_contains.length) {
          fail({ message: "Add at least one word to look for, or turn on the AI urgency check." }); return false;
        }
        try {
          if (isNew) await api("/api/watch/rules", { method: "POST", json: { ...rb, enabled: true } });
          else await api(`/api/watch/rules/${encodeURIComponent(rule.id)}`, { method: "PUT", json: builtin ? { level: rb.level, quiet_override: rb.quiet_override } : rb });
          ui.toast("Rule saved.", { kind: "good" }); load(); return true;
        } catch (error) { fail(error); return false; }
      } }] });
    refresh();
  }

  function scrollTo(name) {
    requestAnimationFrame(() => sections[name]?.scrollIntoView({ behavior: document.documentElement.dataset.motion === "reduce" ? "auto" : "smooth", block: "start" }));
  }

  load();
  return {
    show() { if (status) load(); },
    openSection(name) { if (name === "new-rule" || name === "add") { (status ? Promise.resolve() : load()).then(() => editRule(null)); } else scrollTo(name); },
    openItem(id) { highlight = id; if (alerts) { paint(); scrollTo("alerts"); } },
    refresh() { load(); },
  };
}
