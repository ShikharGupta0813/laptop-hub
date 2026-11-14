// Applications (APPLYQ): drafts that are ready for YOU to send. Nothing here sends by itself: every Send is a tap.
// Routes: docs/api/applyq.md. Everything shown is text (textContent), never HTML.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { lanePicker, askLane } from "./lanepicker.js";

const CSS = "/css/applyq.css";
const TABS = [["ready", "Ready"], ["sent", "Sent"], ["skipped", "Skipped"]];
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const day = (iso) => String(iso || "").slice(0, 10);

async function copy(text, what = "Copied.") {
  try { await navigator.clipboard.writeText(text); ui.toast(what, { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "aq ui-stack ui-stack--lg" });
  container.append(root);
  let tab = "ready", items = [], summary = null, accounts = [], loadError = "", seq = 0;
  const editing = new Set();
  const slot = { head: el("div", { class: "aq-head" }), list: el("div", { class: "ui-stack" }) };

  async function load() {
    const mine = ++seq;
    try {
      const [list, acc] = await Promise.all([api(`/api/applyq?tab=${tab}`), api("/api/applyq/accounts").catch(() => ({ accounts: [] }))]);
      if (mine !== seq) return;
      items = list.items || []; summary = list.summary || null; accounts = acc.accounts || []; loadError = "";
    } catch (error) {
      if (mine !== seq) return;
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paint();
  }

  async function act(item, path, okText, body) {
    try { await api(`/api/applyq/${item.id}/${path}`, { method: "POST", json: body || {} }); if (okText) ui.toast(okText, { kind: "good" }); load(); } catch (error) { fail(error); }
  }

  // The ONLY send: an explicit tap. Past the daily cap the owner is asked once more.
  async function send(item, account) {
    const to = item.contact_email;
    if (!(await ui.confirm({ title: "Send this email now?", message: `To ${to}. Email cannot be undone.`, confirmLabel: "Send", icon: "send" }))) return;
    const go = async (extra) => api(`/api/applyq/${item.id}/send`, { method: "POST", json: { account, ...extra } });
    try {
      let r;
      try { r = await go({}); } catch (error) {
        if (!(error instanceof ApiError && error.status === 409)) throw error;
        if (!(await ui.confirm({ title: "Over your daily cap", message: error.message, confirmLabel: "Send anyway", danger: true, icon: "alert" }))) return;
        r = await go({ confirm_over_cap: true });
      }
      if (r.ok && r.sent) ui.toast("Sent.", { kind: "good" });
      else if (r.copy) { ui.toast(r.error || r.message || "Not sent.", { kind: "warn" }); if (r.copy.body) copy(`${r.copy.subject}\n\n${r.copy.body}`, "Message copied. Send it yourself."); }
      else ui.toast(r.error || "Not sent.", { kind: "warn" });
      load();
    } catch (error) { fail(error); load(); }
  }

  function header() {
    const t = summary?.today || {}, w = summary?.week || {};
    const stat = (n, label) => el("div", { class: "aq-stat" }, el("b", {}, String(n ?? 0)), el("span", {}, label));
    slot.head.replaceChildren(
      el("h2", { class: "ui-title" }, "Applications"),
      el("div", { class: "aq-stats" }, stat((t.sent || 0) + (t.applied || 0), "sent today"), stat(t.ready, "ready"), stat(t.replied, "replies"),
        stat(summary?.streak, "day streak"), stat((w.sent || 0) + (w.applied || 0), "this week")),
      summary ? el("p", { class: "ui-hint" }, `Nothing is sent without your tap. Daily cap: ${summary.cap || "none"}.`) : null);
  }

  function editor(item, node) {
    const d = item.draft || {};
    const subject = el("input", { class: "ui-input", type: "text", value: d.subject || "", maxlength: "200", "aria-label": "Subject" });
    const body = el("textarea", { class: "ui-input aq-body", maxlength: "8000", "aria-label": "Message" }); body.value = d.body || "";
    const to = el("input", { class: "ui-input", type: "email", value: item.contact_email || "", placeholder: "Apply-by-email address", "aria-label": "Send to", maxlength: "200" });
    const channel = el("select", { class: "ui-input", "aria-label": "Channel" }, ...["email", "web", "message"].map((c) => el("option", { value: c, ...(item.channel === c ? { selected: "" } : {}) }, c)));
    const save = ui.button({ label: "Save", icon: "check", variant: "primary", size: "sm", onClick: () => ui.busy(save, async () => {
      try {
        await api(`/api/applyq/${item.id}`, { method: "PATCH", json: { contact_email: to.value.trim(), channel: channel.value, draft: { subject: subject.value, body: body.value } } });
        editing.delete(item.id); load();
      } catch (error) { fail(error); }
    }) });
    const cancel = ui.button({ label: "Cancel", variant: "ghost", size: "sm", onClick: () => { editing.delete(item.id); paint(); } });
    // Work lanes: write the draft with the template (as before), the free Council, or an AI lane (docs/api/lanes.md). Nothing is sent.
    const writer = lanePicker({ value: "template", label: "Write draft with", options: [{ id: "template", label: "Template" }, { id: "council", label: "Council" }, { id: "auto", label: "AI" }] });
    const redo = ui.button({ label: "Redraft", icon: "refresh", variant: "tonal", size: "sm", onClick: () => ui.busy(redo, async () => {
      if (writer.value === "template") { editing.delete(item.id); await act(item, "draft", "Redrafted."); return; }
      const task = `Write the message body for this job application. Plain text, short, honest, no placeholders.\nRole: ${item.title}\nCompany: ${item.company || ""}\nWhy it fits: ${item.reason || ""}\nCurrent draft:\n${body.value}`;
      const run = await askLane(task, writer.value === "council" ? "council" : "auto", { cls: "routine" });
      if (run?.answer) { body.value = run.answer; ui.toast(`Drafted by ${run.lane}. Check it, then Save.`, { kind: "good" }); }
    }) });
    return el("div", { class: "ui-stack" }, subject, body, el("div", { class: "aq-fields" }, to, channel), writer.el, el("div", { class: "aq-actions" }, save, redo, cancel));
  }

  function accountPicker(item) {
    if (!accounts.length) return { node: null, value: () => item.account || "" };
    const sel = el("select", { class: "ui-input aq-account", "aria-label": "Gmail account" },
      ...accounts.map((a) => el("option", { value: a.id, ...((item.account || accounts[0].id) === a.id ? { selected: "" } : {}) }, a.label || a.address || a.id)));
    return { node: sel, value: () => sel.value };
  }

  function card(item) {
    const d = item.draft || {};
    const node = el("article", { class: "ui-card ui-stack aq-item" });
    const flags = (item.flags || []).map((f) => ui.badge(f, "warn"));
    const missing = (item.missing || []).length ? el("p", { class: "aq-missing" }, "Fill in before sending: " + item.missing.join(", ")) : null;
    node.append(el("div", { class: "aq-item-head" },
      item.score ? ui.badge(String(Math.round(item.score * (item.score <= 1 ? 100 : 1))), "accent") : null,
      el("h3", { class: "aq-title" }, item.title), el("span", { class: "aq-when" }, day(item.sent_at || item.created))),
      el("div", { class: "aq-meta" }, item.company ? el("span", {}, item.company) : null, el("span", {}, item.source), ui.badge(item.channel, ""),
        item.state !== "ready" ? ui.badge(item.state, item.state === "replied" ? "good" : item.state === "failed" ? "bad" : "") : null, ...flags),
      item.reason ? el("p", { class: "aq-reason" }, item.reason) : null);
    if (item.error) node.append(el("p", { class: "aq-missing" }, item.error));
    if (tab === "ready" && editing.has(item.id)) { node.append(editor(item, node)); return node; }
    if (d.body) node.append(el("p", { class: "aq-preview" }, d.body));
    if (missing) node.append(missing);
    const acct = accountPicker(item);
    const actions = el("div", { class: "aq-actions" });
    if (tab === "ready") {
      const web = item.channel !== "email";
      actions.append(
        ui.button({ label: "Edit", icon: "edit", variant: "tonal", size: "sm", onClick: () => { editing.add(item.id); paint(); } }),
        item.channel === "email"
          ? ui.button({ label: "Send", icon: "send", variant: "primary", size: "sm", disabled: !item.contact_email || !!(item.missing || []).length, onClick: () => send(item, acct.value()) })
          : ui.button({ label: "Copy", icon: "copy", variant: "primary", size: "sm", onClick: () => copy([d.cover_note, d.body, ...Object.entries(d.answers || {}).map(([q, a]) => `${q}: ${a}`)].filter(Boolean).join("\n\n")) }),
        web && item.url && /^https?:/.test(item.url) ? ui.button({ label: "Open", icon: "external", variant: "tonal", size: "sm", onClick: () => window.open(item.url, "_blank", "noopener,noreferrer") }) : null,
        item.channel === "email" ? ui.button({ label: "Copy", icon: "copy", variant: "ghost", size: "sm", onClick: () => copy(`${d.subject || ""}\n\n${d.body || ""}`) }) : null,
        ui.button({ label: "I applied", icon: "check", variant: "tonal", size: "sm", onClick: () => act(item, "applied", "Marked applied.") }),
        ui.button({ label: "Skip", icon: "x", variant: "ghost", size: "sm", onClick: () => act(item, "skip", "Skipped.") }));
      if (item.channel === "email" && acct.node) node.append(acct.node);
      if (web && Object.keys(d.answers || {}).length) {
        node.append(el("div", { class: "ui-stack" }, ...Object.entries(d.answers).map(([q, a]) =>
          el("div", { class: "aq-answer" }, el("span", {}, q + ": " + a), ui.button({ label: "Copy answer", icon: "copy", variant: "ghost", size: "sm", iconOnly: true, onClick: () => copy(a) })))));
      }
    } else if (tab === "skipped") {
      actions.append(ui.button({ label: "Put back", icon: "refresh", variant: "tonal", size: "sm", onClick: () => act(item, "restore") }));
    }
    node.append(actions);
    return node;
  }

  function addBox() {
    const text = el("textarea", { class: "ui-input aq-body", placeholder: "Paste a link or the text of a posting", "aria-label": "Link or text", maxlength: "9000" });
    const add = ui.button({ label: "Add", icon: "plus", variant: "primary", onClick: () => ui.busy(add, async () => {
      if (!text.value.trim()) { ui.toast("Paste a link or some text first.", { kind: "warn" }); return; }
      try {
        const r = await api("/api/applyq/manual", { method: "POST", json: { text: text.value } });
        text.value = ""; ui.toast(r.created ? "Added and drafted." : "That one is already in the queue.", { kind: r.created ? "good" : "warn" }); tab = "ready"; load();
      } catch (error) { fail(error); }
    }) });
    return el("section", { class: "ui-card ui-stack aq-add" }, ui.eyebrow("01", "Add one"), text, el("div", { class: "aq-actions" }, add));
  }

  function paint() {
    if (loadError === "off") { slot.list.replaceChildren(ui.emptyState({ icon: "send", title: "Applications are switched off", body: "Set applyq_enabled to true in Settings." })); return; }
    header();
    if (loadError) { slot.list.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load applications", body: loadError, action: { label: "Try again", icon: "refresh", onClick: load } })); return; }
    slot.list.replaceChildren(items.length ? el("div", { class: "ui-stack" }, ...items.map(card))
      : ui.emptyState({ icon: "send", title: tab === "ready" ? "Nothing ready yet" : "Nothing here", body: tab === "ready" ? "The finder queues drafts here, or paste a link below." : "" }));
  }

  const tabs = el("div", { class: "aq-tabs", role: "tablist" });
  function drawTabs() {
    tabs.replaceChildren(...TABS.map(([id, label]) => {
      const b = el("button", { type: "button", role: "tab", class: "aq-tab", "aria-selected": String(tab === id) }, label);
      b.addEventListener("click", () => { tab = id; drawTabs(); load(); });
      return b;
    }));
  }
  drawTabs();
  root.append(slot.head, tabs, slot.list, addBox());
  slot.list.replaceChildren(ui.skeleton("card", 2));
  load();
  ui.pullToRefresh?.(container, load);
  return { show() { load(); }, refresh() { load(); } };
}
