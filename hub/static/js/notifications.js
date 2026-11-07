// Notifications: everything the hub wanted you to know, grouped by day. Live over EventSource with a polling fallback.
// Routes: docs/api/notify.md (GET /api/notifications, POST /read, POST /clear, DELETE /{id}, GET /stream).
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/notifications.css";
const LEVEL = { info: { icon: "bell", tone: "accent", label: "Info" }, warn: { icon: "alert", tone: "warn", label: "Warning" }, error: { icon: "alert-circle", tone: "bad", label: "Error" } };
const SOURCE_LABEL = { watch: "Watch", notify: "Hub", reminders: "Reminders", automations: "Automations", inbox: "Inbox", expenses: "Spending", kb: "Knowledge", focus: "Focus" };
const srcLabel = (s) => SOURCE_LABEL[s] || (s ? s[0].toUpperCase() + s.slice(1).replace(/[-_]/g, " ") : "Hub");

function dayKey(date) {
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return { key: "unknown", label: "Earlier" };
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(new Date()) - start(d)) / 86400000);
  const label = diff === 0 ? "Today" : diff === 1 ? "Yesterday"
    : d.toLocaleDateString(undefined, { weekday: diff < 7 ? "long" : undefined, day: "numeric", month: "short", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
  return { key: String(start(d)), label };
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "ntf" });
  container.append(root);
  let items = [];
  let unread = 0;
  let filter = "all";          // "all" | "unread" | a source name
  let state = "loading";       // loading | ready | error | missing
  let errorText = "";
  let stream = null, poll = null, active = false, highlight = null;

  const header = el("div", { class: "ntf-head" });
  const chips = el("div", { class: "ui-hscroll ntf-chips", role: "group", "aria-label": "Filter notifications" });
  const list = el("div", { class: "ntf-list" });
  root.append(header, chips, list);

  // ---- data ----------------------------------------------------------------------------------------
  async function load({ quiet = false } = {}) {
    if (!quiet && !items.length) { state = "loading"; render(); }
    try {
      const data = await api("/api/notifications?limit=200");
      items = Array.isArray(data.notifications) ? data.notifications : [];
      unread = Number(data.unread) || items.filter((n) => !n.read).length;
      state = "ready";
    } catch (error) {
      if (quiet && items.length) return;
      state = error instanceof ApiError && (error.status === 404 || error.status === 405) ? "missing" : "error";
      errorText = error.message;
    }
    render();
    ctx.refreshNotifications?.();
  }

  function connect() {
    disconnect();
    if (typeof EventSource === "undefined") { startPolling(); return; }
    try {
      stream = new EventSource("/api/notifications/stream");
      stream.onmessage = (event) => {
        let n; try { n = JSON.parse(event.data); } catch (_) { return; }
        if (!n || n.id == null) return;
        stopPolling();
        if (items.some((x) => x.id === n.id)) return;
        items = [n, ...items].slice(0, 300);
        if (!n.read) unread += 1;
        if (state !== "ready") state = "ready";
        render();
        ctx.refreshNotifications?.();
        ui.haptic("light");
      };
      stream.onerror = () => {
        // The browser retries by itself. A closed stream (404, proxy that buffers) falls back to polling.
        if (stream && stream.readyState === 2) startPolling();
      };
    } catch (_) { startPolling(); }
  }
  function disconnect() { stream?.close(); stream = null; stopPolling(); }
  function startPolling() { if (!poll && active) poll = setInterval(() => { if (document.visibilityState === "visible") load({ quiet: true }); }, 20000); }
  function stopPolling() { clearInterval(poll); poll = null; }

  async function markRead(ids) {
    const fresh = items.filter((n) => ids.includes(n.id) && !n.read);
    if (!fresh.length) return;
    fresh.forEach((n) => { n.read = true; });
    unread = Math.max(0, unread - fresh.length);
    render();
    try { const r = await api("/api/notifications/read", { method: "POST", json: { ids } }); unread = r.unread ?? unread; ctx.refreshNotifications?.(); }
    catch (error) { fresh.forEach((n) => { n.read = false; }); unread += fresh.length; render(); ui.toast(error.message, { kind: "bad" }); }
  }
  async function markAll() {
    const before = items.map((n) => n.read);
    items.forEach((n) => { n.read = true; }); unread = 0; render();
    try { await api("/api/notifications/read", { method: "POST", json: { all: true } }); ui.toast("All caught up.", { kind: "good" }); ctx.refreshNotifications?.(); }
    catch (error) { items.forEach((n, i) => { n.read = before[i]; }); unread = items.filter((n) => !n.read).length; render(); ui.toast(error.message, { kind: "bad" }); }
  }
  let clearing = false;   // two quick taps must not stack two confirm dialogs
  async function clearAll() {
    if (clearing) return;
    clearing = true;
    try { await clearAllNow(); } finally { clearing = false; }
  }
  async function clearAllNow() {
    if (!await ui.confirm({ title: "Clear all notifications?", message: "They are deleted from the hub and can't be brought back.", confirmLabel: "Clear all", danger: true })) return;
    try { await api("/api/notifications/clear", { method: "POST", json: { confirm: true } }); items = []; unread = 0; filter = "all"; render(); ui.toast("Cleared.", { kind: "good" }); ctx.refreshNotifications?.(); }
    catch (error) { ui.toast(error.message, { kind: "bad" }); }
  }
  async function remove(n) {
    const index = items.indexOf(n);
    items = items.filter((x) => x !== n);
    if (!n.read) unread = Math.max(0, unread - 1);
    render();
    try { await api(`/api/notifications/${encodeURIComponent(n.id)}`, { method: "DELETE" }); ctx.refreshNotifications?.(); }
    catch (error) {
      if (error instanceof ApiError && error.status === 404) return;
      items.splice(Math.max(0, index), 0, n); if (!n.read) unread += 1; render(); ui.toast(error.message, { kind: "bad" });
    }
  }

  function openLink(n) {
    const link = String(n.link || "");
    if (/^https?:\/\//i.test(link)) { window.open(link, "_blank", "noopener,noreferrer"); return; }
    if (/^[a-z][a-z0-9-]*(\/[a-z0-9-]+)?$/i.test(link)) {
      const [view, section] = link.split("/");
      ctx.open(view, section);
    }
  }

  // ---- rendering -----------------------------------------------------------------------------------
  function visible() {
    return items.filter((n) => filter === "all" || (filter === "unread" ? !n.read : n.source === filter));
  }

  function renderHeader() {
    if (state !== "ready" || !items.length) { header.replaceChildren(); chips.replaceChildren(); chips.hidden = true; return; }
    header.replaceChildren(
      el("div", { class: "ntf-count" }, el("span", { class: "ntf-count__n ui-num" }, String(unread)), el("span", { class: "ntf-count__l" }, unread === 1 ? "unread" : "unread")),
      el("div", { class: "ntf-actions" },
        ui.button({ label: "Mark all read", icon: "check-circle", variant: "tonal", size: "sm", disabled: unread === 0, onClick: markAll }),
        ui.button({ label: "Clear all", icon: "trash", variant: "danger-soft", size: "sm", onClick: clearAll })));
    const sources = [...new Set(items.map((n) => n.source).filter(Boolean))];
    const defs = [{ id: "all", label: "All" }, { id: "unread", label: "Unread" }, ...sources.map((s) => ({ id: s, label: srcLabel(s) }))];
    if (!defs.some((d) => d.id === filter)) filter = "all";
    chips.hidden = defs.length <= 2 && !unread;
    chips.replaceChildren(...defs.map((d) => el("button", { type: "button", class: "ui-chip" + (filter === d.id ? " is-on" : ""), "aria-pressed": String(filter === d.id),
      onclick: () => { filter = d.id; ui.haptic("select"); render(); } }, d.label)));
  }

  function itemNode(n) {
    const lv = LEVEL[n.level] || LEVEL.info;
    const main = el("button", { type: "button", class: "ntf-main", "aria-label": `${n.read ? "" : "Unread. "}${n.title}` },
      el("span", { class: "ntf-lead" }, ui.tile(lv.icon, lv.tone), n.read ? null : el("i", { class: "ntf-dot", "aria-hidden": "true" })),
      el("span", { class: "ui-row__body" },
        el("span", { class: "ui-row__title ntf-title" }, n.title || "Notification"),
        n.body ? el("span", { class: "ui-row__sub ui-clamp-2" }, n.body) : null,
        el("span", { class: "ntf-meta" }, ui.relTime(n.created), n.source ? el("span", { class: "ntf-src" }, srcLabel(n.source)) : null,
          n.level && n.level !== "info" ? el("span", { class: `ui-badge ui-badge--${n.level === "error" ? "bad" : "warn"}` }, lv.label) : null)));
    main.addEventListener("click", () => { ui.haptic("select"); markRead([n.id]); if (n.link) openLink(n); });
    const del = ui.button({ label: "Delete notification", icon: "x", variant: "ghost", iconOnly: true, class: "ntf-del", onClick: () => remove(n) });
    const row = el("div", { class: "ui-row ntf-row" + (n.read ? "" : " is-unread"), "data-level": n.level || "info", "data-id": String(n.id) }, main, del);
    if (String(highlight) === String(n.id)) { row.classList.add("is-flash"); highlight = null; setTimeout(() => row.classList.remove("is-flash"), 1800); requestAnimationFrame(() => row.scrollIntoView({ block: "center" })); }
    return row;
  }

  function render() {
    renderHeader();
    if (state === "loading") { list.replaceChildren(ui.skeleton("list", 5)); return; }
    if (state === "missing") { list.replaceChildren(ui.emptyState({ icon: "bell", title: "Notifications aren't set up", body: "This hub doesn't have the notifications service yet." })); return; }
    if (state === "error") {
      list.replaceChildren(ui.notice(errorText || "Couldn't load notifications.", { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => load() }));
      return;
    }
    const shown = visible();
    if (!shown.length) {
      list.replaceChildren(items.length
        ? ui.emptyState({ icon: "filter", title: "Nothing here", body: "No notifications match this filter.", action: { label: "Show all", onClick: () => { filter = "all"; render(); } }, compact: true })
        : ui.emptyState({ icon: "bell", title: "You're all caught up", body: "Alerts from reminders, mail watching and automations show up here." }));
      return;
    }
    const groups = new Map();
    for (const n of shown) {
      const d = dayKey(n.created);
      if (!groups.has(d.key)) groups.set(d.key, { label: d.label, rows: [] });
      groups.get(d.key).rows.push(n);
    }
    list.replaceChildren(...[...groups.values()].map((g) => el("section", { class: "ntf-day" },
      el("h2", { class: "ui-caps ntf-day__title" }, g.label),
      el("div", { class: "ui-list ui-list--inset" }, g.rows.map(itemNode)))));
  }

  render();
  load();
  return {
    show() { active = true; load({ quiet: true }); connect(); },
    hide() { active = false; disconnect(); },
    openSection(name) { if (name === "unread" || name === "all") { filter = name; render(); } },
    openItem(id) { highlight = id; if (state === "ready") { filter = "all"; render(); } },
    refresh() { load(); },
  };
}
