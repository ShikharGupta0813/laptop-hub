// Autopilot: drafts the hub prepared (mail replies, chat replies, Asana comments, the standup) waiting for one tap.
// Routes: docs/api/autopilot.md. Nothing runs until Approve is tapped. Everything shown is text (textContent).
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/autopilot.css";
const SOURCE = { "watch-gmail": "Gmail alert", "watch-chat": "Chat", asana: "Asana", inbox: "Gmail", standup: "Standup", manual: "Note" };
const KIND_HINT = {
  send_email: "Sends this reply to the sender only.",
  asana_comment: "Posts this as a comment on the task.",
  copy_text: "Copies the text so you can paste it where you want.",
  none: "For your information. Nothing to send.",
};
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back below */ }
  const ta = el("textarea", { class: "ap-offscreen", "aria-hidden": "true", readonly: true });
  ta.value = text; document.body.append(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "ap ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, history = [], loadError = "", showHistory = false;
  const edits = new Map();   // card id -> the text being edited (kept when the list repaints)

  async function load({ quiet = false } = {}) {
    if (!data && !quiet) root.replaceChildren(ui.skeleton("card", 2));
    try {
      data = await api("/api/autopilot");
      history = data.enabled || data.counts?.executed ? (await api("/api/autopilot/history?limit=30")).cards || [] : [];
      loadError = "";
    } catch (error) { loadError = error instanceof ApiError && (error.status === 404 || error.status === 405) ? "missing" : error.message; }
    paint();
  }

  function offState() {
    const on = ui.button({ label: "Turn Autopilot on", icon: "zap", variant: "primary", block: true, onClick: () => ui.busy(on, async () => {
      try { await api("/api/settings", { method: "PATCH", json: { autopilot_enabled: true } }); ui.toast("Autopilot is on. It looks every few minutes.", { kind: "good" }); await load({ quiet: true }); }
      catch (error) { fail(error); }
    }) });
    return el("div", { class: "ui-card ap-off ui-stack" },
      ui.emptyState({ icon: "zap", title: "Autopilot is off", body: "When it is on, the hub reads your mail, chat alerts and Asana, writes draft replies with a free AI (never Claude) and lines them up here. You tap Approve, Edit or Skip. Nothing is sent by itself." }),
      on,
      el("p", { class: "ui-hint" }, data?.setup_hint || "Or on the laptop: python -m hub setup --only autopilot"));
  }

  function card(c) {
    const sends = c.action_kind === "send_email" || c.action_kind === "asana_comment";
    const hasDraft = c.action_kind !== "none" && !!c.draft;
    const area = hasDraft ? el("textarea", { class: "ap-draft", rows: 5, "aria-label": "Draft", maxlength: 4000 }) : null;
    if (area) {
      area.value = edits.has(c.id) ? edits.get(c.id) : c.draft;
      area.addEventListener("input", () => edits.set(c.id, area.value));
    }
    const text = () => (area ? area.value : undefined);
    const approve = ui.button({ label: c.action_kind === "send_email" ? "Approve and send" : c.action_kind === "asana_comment" ? "Approve and post" : c.action_kind === "copy_text" ? "Done" : "Got it",
      icon: "check", variant: "primary", onClick: () => ui.busy(approve, async () => {
        try {
          const r = await api(`/api/autopilot/${c.id}/approve`, { method: "POST", json: { confirm: true, ...(area ? { text: text() } : {}) } });
          edits.delete(c.id);
          if (r.error) ui.toast(`Not done: ${r.error}`, { kind: "bad" });
          else if (c.action_kind === "send_email") ui.toast("Sent.", { kind: "good" });
          else if (c.action_kind === "asana_comment") ui.toast("Posted to Asana.", { kind: "good" });
          else ui.toast("Done.", { kind: "good" });
          await load({ quiet: true });
        } catch (error) { fail(error); await load({ quiet: true }); }
      }) });
    const skip = ui.button({ label: "Skip", icon: "x", variant: "ghost", onClick: () => ui.busy(skip, async () => {
      try { await api(`/api/autopilot/${c.id}/skip`, { method: "POST", json: {} }); edits.delete(c.id); await load({ quiet: true }); } catch (error) { fail(error); }
    }) });
    const copy = c.action_kind === "copy_text" && area ? ui.button({ label: "Copy", icon: "copy", variant: "tonal", onClick: async () => {
      if (await copyText(text())) ui.toast("Copied.", { kind: "good" }); else fail({ message: "Couldn't copy. Select the text and copy it by hand." });
    } }) : null;
    return el("article", { class: "ui-card ap-card ui-stack", "data-id": c.id },
      el("div", { class: "ap-head" }, ui.badge(SOURCE[c.source] || c.source, "accent"),
        c.ai_written ? ui.badge("AI-written draft", "warn") : null,
        el("span", { class: "ap-when" }, ui.relTime ? ui.relTime(c.created) : "")),
      el("h3", { class: "ap-title" }, c.title),
      c.summary ? el("p", { class: "ap-summary" }, c.summary) : null,
      c.note ? ui.notice(c.note, { tone: "warn" }) : null,
      sends && c.recipient ? el("p", { class: "ap-to" }, el("span", { class: "ui-caps" }, "To "), c.recipient, c.subject ? el("span", { class: "ap-subject" }, " · " + c.subject) : null) : null,
      area,
      el("p", { class: "ui-hint" }, area ? `You can edit it first. ${KIND_HINT[c.action_kind]}` : KIND_HINT[c.action_kind]),
      el("div", { class: "ap-actions" }, approve, copy, skip));
  }

  function historySection() {
    if (!history.length) return null;
    const toggle = ui.button({ label: showHistory ? "Hide history" : `History (${history.length})`, icon: "history", variant: "ghost", onClick: () => { showHistory = !showHistory; paint(); } });
    const rows = showHistory ? el("div", { class: "ui-list ui-list--inset" }, ...history.map((c) => ui.listRow({
      icon: c.status === "executed" ? "check-circle" : c.status === "failed" ? "alert-circle" : c.status === "skipped" ? "x" : "clock",
      tone: c.status === "executed" || c.status === "approved" || c.status === "edited" ? "good" : c.status === "failed" ? "bad" : "accent",
      title: c.title, sub: `${c.status}${c.edited ? " (edited)" : ""} · ${SOURCE[c.source] || c.source}${c.error ? " · " + c.error : ""}` }))) : null;
    return el("section", { class: "ap-history ui-stack" }, toggle, rows);
  }

  function footer() {
    const st = data;
    const check = ui.button({ label: "Check now", icon: "refresh", variant: "tonal", onClick: () => ui.busy(check, async () => {
      try {
        const r = await api("/api/autopilot/sweep", { method: "POST", json: {} });
        ui.toast(r.new ? `${r.new} new card${r.new === 1 ? "" : "s"}.` : "Nothing new.", { kind: "good" });
        await load({ quiet: true });
      } catch (error) { fail(error); }
    }) });
    const off = ui.button({ label: "Turn off", icon: "power", variant: "ghost", onClick: () => ui.busy(off, async () => {
      if (!await ui.confirm({ title: "Turn Autopilot off?", message: "It stops looking for new things. Cards already here stay until they expire.", confirmLabel: "Turn off" })) return;
      try { await api("/api/settings", { method: "PATCH", json: { autopilot_enabled: false } }); await load({ quiet: true }); } catch (error) { fail(error); }
    }) });
    return el("section", { class: "ui-card ap-foot ui-stack" },
      el("p", { class: "ui-hint" }, `Looks every ${st.sweep_min} min. AI used today: ${st.ai_calls_today} of ${st.daily_ai_calls}. Using ${st.provider === "gemini" ? "Gemini" : "the local model"}, never Claude.`
        + (st.last_sweep ? ` Last look: ${st.last_sweep.replace("T", " ")}.` : "")),
      Object.keys(st.notes || {}).length ? ui.notice(Object.values(st.notes).join(" "), { tone: "warn" }) : null,
      el("div", { class: "ap-actions" }, check, off));
  }

  function paint() {
    if (loadError === "missing") { root.replaceChildren(ui.emptyState({ icon: "zap", title: "Autopilot isn't on this laptop", body: "Update the hub to get it." })); return; }
    if (loadError && !data) { root.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load Autopilot", body: loadError, action: { label: "Try again", icon: "refresh", onClick: () => load() } })); return; }
    if (!data.enabled && !data.cards?.length) { root.replaceChildren(offState(), historySection()); return; }
    const parts = [];
    if (!data.enabled) parts.push(ui.notice("Autopilot is off. These cards were made earlier.", { tone: "warn" }));
    if (data.cards.length) parts.push(...data.cards.map(card));
    else parts.push(ui.emptyState({ icon: "check-circle", title: "Nothing to approve", body: "When something needs you, a draft appears here and you get a notification.", compact: true }));
    parts.push(historySection(), data.enabled ? footer() : null);
    root.replaceChildren(...parts.filter(Boolean));
  }

  load();
  return {
    show() { load({ quiet: true }); },
    refresh() { load(); },
  };
}
