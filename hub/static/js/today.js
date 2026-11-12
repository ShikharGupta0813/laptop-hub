// Today: the whole day on one page (meetings, tasks, spending, mail, what you did). Read-only; routes: docs/api/today.md.
// Everything shown is text (textContent). The last answer is kept in localStorage so the page opens even offline.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CACHE_KEY = "hub.today.v1";
const KIND_ICON = { meeting: "calendar", commit: "code", mail: "mail", task: "check-circle", card: "zap", council: "users", expense: "chart" };

function readCache() { try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "null"); } catch (_) { return null; } }
function writeCache(data) { try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); } catch (_) { /* private mode: fine */ } }

export function init(container, ctx) {
  const root = el("div", { class: "today ui-stack ui-stack--lg" });
  container.append(root);
  let data = readCache(), stale = !!data, loadError = "", evening = false, words = "";

  async function load({ quiet = false } = {}) {
    if (!data && !quiet) root.replaceChildren(ui.skeleton("card", 3));
    try {
      data = await api("/api/today");
      stale = false; loadError = "";
      writeCache(data);
    } catch (error) {
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paint();
  }

  const stat = (label, value) => el("div", { class: "ui-card" }, el("p", { class: "ui-caps" }, label), el("p", { class: "ui-title" }, String(value ?? "-")));

  function now() {
    const p = data.pending || {};
    const rows = [];
    if (p.next_meeting) rows.push(ui.listRow({ icon: "calendar", tone: "accent", title: `Next: ${p.next_meeting.title}`, sub: `${p.next_meeting.time}${p.next_meeting.in_min != null ? ` · in ${p.next_meeting.in_min} min` : ""}` }));
    else rows.push(ui.listRow({ icon: "calendar", tone: "gray", title: "No more meetings today" }));
    rows.push(ui.listRow({ icon: "zap", tone: p.autopilot ? "warn" : "gray", title: p.autopilot ? `${p.autopilot} draft${p.autopilot === 1 ? "" : "s"} to approve` : "Nothing to approve",
      sub: p.autopilot ? "Open Autopilot" : "", chevron: !!p.autopilot, onClick: p.autopilot ? () => ctx?.open?.("autopilot") : undefined }));
    return el("section", { class: "ui-stack" }, ui.eyebrow("01", "Now"), el("div", { class: "ui-list ui-list--inset" }, ...rows));
  }

  function timeline() {
    const items = data.timeline || [];
    const body = items.length
      ? el("div", { class: "ui-list ui-list--inset" }, ...items.map((i) => ui.listRow({ icon: KIND_ICON[i.kind] || "clock", tone: i.kind === "expense" ? "warn" : "accent", title: i.title, sub: i.sub, meta: i.time || "" })))
      : ui.emptyState({ icon: "clock", title: "Nothing yet today", compact: true });
    return el("section", { class: "ui-stack" }, ui.eyebrow("02", "Timeline"), body);
  }

  function doneToday() {
    const d = data.done || {}, rows = [];
    for (const c of d.commits || []) rows.push(ui.listRow({ icon: "code", title: c.subject, sub: c.repo, meta: c.at }));
    for (const c of d.tasks?.commented || []) rows.push(ui.listRow({ icon: "task", title: `Commented on ${c.name}`, meta: c.at }));
    if (d.tasks?.completed) rows.push(ui.listRow({ icon: "check-circle", tone: "good", title: `Completed ${d.tasks.completed} Asana task${d.tasks.completed === 1 ? "" : "s"}` }));
    for (const r of data.mail?.replied || []) rows.push(ui.listRow({ icon: "mail", title: `Replied to ${r.to || "a mail"}`, sub: r.subject, meta: r.at }));
    for (const c of d.council || []) rows.push(ui.listRow({ icon: "users", title: `Council: ${c.task}`, sub: c.status, meta: c.at }));
    const body = rows.length ? el("div", { class: "ui-list ui-list--inset" }, ...rows) : ui.emptyState({ icon: "check-circle", title: "Nothing done yet", compact: true });
    return el("section", { class: "ui-stack" }, ui.eyebrow("03", "Done today"), body);
  }

  function numbers() {
    const n = data.numbers || {};
    return el("section", { class: "ui-stack" }, ui.eyebrow("04", "Numbers"),
      el("div", { class: "ui-stack" }, stat("Spent today", n.spent_text), stat("Mails handled", n.mails_handled), stat("Commits", n.commits), stat("Cards approved", n.cards_approved)));
  }

  function tasks() {
    const t = data.tasks || {}, rows = [];
    for (const x of t.overdue || []) rows.push(ui.listRow({ icon: "alert-circle", tone: "bad", title: x.name, sub: `Overdue since ${x.due_on}${x.project ? " · " + x.project : ""}` }));
    for (const x of t.due_today || []) rows.push(ui.listRow({ icon: "task", title: x.name, sub: `Due today${x.project ? " · " + x.project : ""}` }));
    if (!rows.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("05", "Tasks"), el("div", { class: "ui-list ui-list--inset" }, ...rows));
  }

  function summary() {
    const text = el("p", { class: "ui-hint" }, words);
    const btn = ui.button({ label: evening ? "Morning summary" : "Evening review", icon: "sparkles", variant: "tonal", onClick: () => ui.busy(btn, async () => {
      try {
        evening = !evening;
        const r = await api(`/api/today/text?style=${evening ? "evening" : "morning"}`);
        words = r.text;
        paint();
      } catch (error) { ui.toast(error.message, { kind: "bad" }); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("06", "In words"), text, btn);
  }

  function paint() {
    if (loadError === "off" && !data) { root.replaceChildren(ui.emptyState({ icon: "clock", title: "Today is switched off", body: "Set today_enabled to true in Settings." })); return; }
    if (!data) { root.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load Today", body: loadError, action: { label: "Try again", icon: "refresh", onClick: () => load() } })); return; }
    const refresh = ui.button({ label: "Refresh", icon: "refresh", variant: "ghost", onClick: () => ui.busy(refresh, () => load({ quiet: true })) });
    root.replaceChildren(...[
      el("div", { class: "ui-row" }, el("h2", { class: "ui-title" }, `${data.weekday || "Today"} · ${data.date || ""}`), refresh),
      stale || loadError ? ui.notice(loadError ? `Showing the last saved copy (${loadError}).` : "Showing the last saved copy while it refreshes.", { tone: "warn" }) : null,
      data.sources_failed?.length ? ui.notice(`Could not read: ${data.sources_failed.join(", ")}.`, { tone: "warn" }) : null,
      now(), timeline(), doneToday(), numbers(), tasks(), summary(),
    ].filter(Boolean));
  }

  if (data) paint();
  load({ quiet: !!data });
  ui.pullToRefresh?.(container, () => load({ quiet: true }));
  return { show() { load({ quiet: true }); }, refresh() { load({ quiet: true }); } };
}
