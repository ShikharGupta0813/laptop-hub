// Google Chat channels (Settings > Alerts > "Google Chat channels") and the "Chat replies" screen. Routes: docs/api/chatchan.md.
// For a PERSONAL Google account there is no Chat API path that is known to work, so: READ from notifications and Chat emails,
// REPLY by copying your draft and opening Google Chat. HARD RULE: nothing is sent from here. You paste and press Send.
// Every drafted reply has its OWN text (one box per conversation, never copied to the others). All text goes through textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";

const good = (text) => ui.toast(text, { kind: "good" });
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const capture = () => { try { return window.Capacitor?.Plugins?.HubCapture || window.HubCapture || null; } catch (_) { return null; } };
const SAFE_WEB = /^https:\/\/(?:chat\.google\.com\/|mail\.google\.com\/chat(?:[/?#]|$))/;
const SAFE_INTENT = /^intent:\/\/(?:chat\.google\.com|mail\.google\.com)\/[^\s]*#Intent;scheme=https;package=com\.google\.android\.apps\.dynamite;/;
const KIND_NAME = { space: "Space or person", "email-source": "Chat emails", "web-draft": "Chat web page draft" };

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
  const area = el("textarea", { "aria-hidden": "true", style: "position:fixed;left:-9999px;top:0" });
  area.value = text; document.body.append(area); area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  area.remove();
  return ok;
}

// The hand-over: copy the text, open Chat. The native app does both in one call; the browser copies first, then opens.
async function handOver(prep) {
  const plan = prep.open || {};
  const url = SAFE_WEB.test(plan.url || "") ? plan.url : "https://chat.google.com/";
  const native = capture()?.openChat;
  if (native) {
    try {
      const r = await capture().openChat({ text: prep.text, url, share: false });
      return r?.copied !== false ? "Copied and opened Google Chat. Paste your reply and press Send." : "Opened Chat, but the copy failed: select the draft and copy it.";
    } catch (_) { /* fall back to the browser way */ }
  }
  const copied = await copyText(prep.text);
  const isAndroid = /Android/i.test(navigator.userAgent || "");
  if (isAndroid && SAFE_INTENT.test(plan.android_intent || "")) window.location.href = plan.android_intent;
  else window.open(url, "_blank", "noopener,noreferrer");
  return copied ? "Copied. Paste your reply in Google Chat and press Send." : "Chat opened, but copying failed: select the draft text and copy it.";
}

function replyCard(item, reload, webOn) {
  const box = el("textarea", { class: "ui-input", rows: "5", maxlength: "4000", "aria-label": `Your reply to ${item.label || "this conversation"}`,
    placeholder: "Write your reply (this one only)", spellcheck: "true" });
  box.value = item.draft || "";
  const out = el("p", { class: "ui-hint", role: "status" });
  const save = async () => { await api(`/api/chatch/pending/${item.id}/draft`, { method: "PUT", json: { text: box.value } }); };
  const run = (btn, fn) => { const go = async () => { try { await fn(); } catch (error) { fail(error); } }; return btn ? ui.busy(btn, go) : go(); };
  const reply = ui.button({ label: "Copy and open Google Chat", icon: "share", variant: "primary", onClick: () => run(reply, async () => {
    await save();
    const prep = await api(`/api/chatch/pending/${item.id}/prepare`, { method: "POST" });
    out.textContent = await handOver(prep);
    if (prep.open?.note && !prep.open.exact) out.textContent += " " + prep.open.note;
  }) });
  const laptop = ui.button({ label: "Open on the laptop", icon: "laptop", variant: "tonal", onClick: () => run(laptop, async () => {
    await save();
    const r = await api(`/api/chatch/pending/${item.id}/open-laptop`, { method: "POST" });
    out.textContent = r.note;
  }) });
  const fresh = ui.button({ label: "New draft", icon: "refresh", variant: "ghost", onClick: () => run(fresh, async () => {
    const r = await api(`/api/chatch/pending/${item.id}/auto-draft`, { method: "POST" });
    box.value = r.draft || ""; good("New draft for this conversation only.");
  }) });
  const keep = ui.button({ label: "Save", icon: "check", variant: "ghost", onClick: () => run(keep, async () => { await save(); good("Saved."); }) });
  const buttons = [reply, laptop, keep, fresh];
  if (webOn) {
    const web = ui.button({ label: "Draft in Chat web page", icon: "globe", variant: "ghost", onClick: () => run(web, async () => {
      if (!await ui.confirm({ title: "Draft in the Chat web page?", confirmLabel: "Open the window", icon: "alert",
        message: "EXPERIMENTAL. A browser window opens on the laptop and the draft is typed into Chat's box. It never sends: you press Send. Google may refuse to sign in to an automated browser, and automating a Google web app may be against its terms (your account could be limited)." })) return;
      await save();
      const r = await api(`/api/chatch/pending/${item.id}/web-draft`, { method: "POST", json: { confirm: true } });
      out.textContent = r.note;
    }) });
    buttons.push(web);
  }
  buttons.push(ui.button({ label: "Done", icon: "check-circle", variant: "tonal", onClick: () => run(null, async () => { await api(`/api/chatch/pending/${item.id}/status`, { method: "POST", json: { status: "done" } }); await reload(); }) }));
  buttons.push(ui.button({ label: "Dismiss", icon: "x", variant: "ghost", onClick: () => run(null, async () => { await api(`/api/chatch/pending/${item.id}/status`, { method: "POST", json: { status: "dismissed" } }); await reload(); }) }));
  if (!item.entry_id) buttons.push(ui.button({ label: "Add to my channels", icon: "plus", variant: "ghost", onClick: () => run(null, async () => { await api(`/api/chatch/pending/${item.id}/add-channel`, { method: "POST" }); good("Added to Google Chat channels."); await reload(); }) }));
  const badges = [item.dm ? ui.badge("Direct message") : ui.badge("Space"), item.mention ? ui.badge("Mentions you", "warn") : null,
    item.urgency >= 2 ? ui.badge("Urgent", "bad") : null, item.via === "email" ? ui.badge("From an email") : null, item.status === "opened" ? ui.badge("Opened", "info") : null];
  return el("article", { class: "ui-card ui-stack" },
    el("div", { class: "al-badges" }, ...badges),
    el("h3", { class: "ui-title" }, item.label || "Google Chat"),
    el("p", { class: "ui-hint" }, `${item.dm ? "" : item.sender ? item.sender + ": " : ""}${item.preview}`),
    item.open?.exact ? null : el("p", { class: "ui-hint" }, "No link saved for this space: Chat opens on its home screen and you pick the conversation. Edit its channel entry to add the link."),
    box, out, el("div", { class: "al-actions" }, ...buttons));
}

export async function openReplies(focusId) {
  const holder = el("div", { class: "ui-stack ui-stack--lg" });
  const sheet = ui.sheet({ title: "Google Chat replies", class: "st-sheet st-sheet--tall", content: holder });
  async function load() {
    let data, snap;
    try { [data, snap] = await Promise.all([api("/api/chatch/pending"), api("/api/chatch")]); } catch (error) { holder.replaceChildren(ui.notice(error.message, { tone: "bad" })); return; }
    const webOn = (snap.entries || []).some((e) => e.kind === "web-draft" && e.enabled);
    let items = data.items || [];
    if (focusId) items = [...items.filter((i) => i.id === focusId), ...items.filter((i) => i.id !== focusId)];
    holder.replaceChildren(
      ui.notice("Nothing is sent from here. Each conversation has its own text. Reply copies it and opens Google Chat: you paste and press Send.", { tone: "info" }),
      ...(items.length ? items.map((i) => replyCard(i, load, webOn)) : [ui.emptyState({ icon: "talk", title: "No Google Chat messages waiting", compact: true })]),
      ui.button({ label: "Channel settings", icon: "settings", variant: "ghost", onClick: () => { sheet.close(); openChannels(); } }));
  }
  await load();
  return sheet;
}

// For one alert card: make sure it has a pending reply, then show the replies screen on it.
export async function replyToAlert(alert) {
  let id = alert.chat_pending;
  try {
    if (!id) { const r = await api(`/api/chatch/from-alert/${alert.id}`, { method: "POST" }); id = r.id; }
  } catch (error) { fail(error); return; }
  return openReplies(id);
}

const CAP_TONE = { true: "good", false: "warn" };

export async function openChannels() {
  let data;
  try { data = await api("/api/chatch"); } catch (error) { fail(error); return; }
  let ed = null;
  const reload = async () => { data = await api("/api/chatch"); ed?.set(data.entries); paintHead(); };
  const headBox = el("div", { class: "ui-stack" });

  function paintHead() {
    const sw = (key, label, hint) => el("div", { class: "ui-row le-switchfield" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, label), el("span", { class: "ui-row__sub" }, hint)),
      ui.switchControl({ checked: !!data.settings[key], label, onChange: async (on) => { try { await api("/api/chatch/settings", { method: "POST", json: { [key]: on } }); await reload(); } catch (error) { fail(error); await reload(); } } }));
    headBox.replaceChildren(
      ui.notice(data.alerts_watch_chat ? "Google Chat notifications from the phone app are being read (Alerts is watching Google Chat)." : "Google Chat is not on your Alerts watch list. Add \"gchat\" under Alerts settings > sources to read its notifications.", { tone: data.alerts_watch_chat ? "good" : "warn" }),
      sw("enabled", "Understand Google Chat messages", "Pull out sender, space, mentions; mute spaces; keep a reply draft for each."),
      sw("email_parse", "Also read Chat notification emails", "Chat emails you about messages you missed; the same message is shown once."),
      el("div", { class: "le-foot" },
        ui.button({ label: "Chat replies", icon: "talk", variant: "primary", size: "sm", onClick: () => openReplies() }),
        ui.button({ label: "Check a sample", icon: "search", variant: "tonal", size: "sm", onClick: sampleSheet })));
  }

  function sampleSheet() {
    const out = el("div", { class: "ui-stack" });
    formSheet({ title: "Check a sample", saveLabel: "Read it", fields: [
      { key: "kind", label: "It is a", type: "select", options: [["notification", "Phone notification"], ["email", "Chat notification email"]] },
      { key: "title", label: "Notification title (the space or person)", type: "text", maxlength: 200 },
      { key: "text", label: "Notification text", type: "textarea", rows: 3, maxlength: 1000 },
      { key: "sender", label: "Email: from", type: "text", maxlength: 200 },
      { key: "subject", label: "Email: subject", type: "text", maxlength: 300 },
      { key: "body", label: "Email: body", type: "textarea", rows: 4, maxlength: 2000 },
      { key: "group", label: "It was a group conversation (notifications only)", type: "switch", default: false }],
      values: { kind: "notification", group: false }, extra: out,
      onSave: async (v) => {
        const r = await api("/api/chatch/parse-test", { method: "POST", json: v });
        const p = r.parsed;
        out.replaceChildren(p ? el("p", { class: "ui-hint" }, `${p.dm ? "Direct message" : "Space"}: ${p.space || "(none)"} · from ${p.sender || "(unknown)"} · ${p.mention ? "mentions you · " : ""}text: ${p.text}`) : ui.notice(r.note, { tone: "warn" }));
        return false;
      } });
  }

  const describe = (e) => {
    const s = e.settings || {};
    if (e.kind === "space") return [s.match?.length ? "name has: " + s.match.join(", ") : "matches its name", s.link ? "has a Chat link" : "no link", s.mention_only ? "mentions only" : "all messages"].join(" · ");
    if (e.kind === "email-source") return [s.from_contains?.length ? "from: " + s.from_contains.join(", ") : "", s.subject_contains?.length ? "subject: " + s.subject_contains.join(", ") : ""].filter(Boolean).join(" · ") || "no patterns";
    return "EXPERIMENTAL" + (data.playwright === false ? " · Playwright is not installed" : "");
  };
  const toForm = (e) => ({ label: e.label, ...e.settings });
  const fieldsFor = (kind) => [{ key: "label", label: "Name", type: "text", maxlength: 60 }, ...(data.kinds[kind]?.fields || []).map((f) => ({ ...f, maxlength: 400 }))];
  const cleanBody = (kind, v) => { const { label, ...settings } = v; return { label, settings }; };
  const confirmWeb = () => ui.confirm({ title: "Turn on the Chat web page draft?", confirmLabel: "Turn on", icon: "alert", danger: true,
    message: "EXPERIMENTAL. It opens a visible browser window and types your draft into Google Chat's page; it never presses Send. Google may refuse to sign in to an automated browser, and automating a Google web app may be against its terms: your account could be limited. Use Copy and open instead if unsure." });

  ed = listEditor({
    title: "Your Google Chat channels", items: data.entries,
    hint: "Spaces and people you want handled your way (mute, mentions only, a link so Reply opens that conversation), where Chat emails come from, and the experimental web draft.",
    empty: "Nothing here.", addLabel: "Add a space or person",
    rowOf: (e) => ({ icon: e.kind === "email-source" ? "inbox" : e.kind === "web-draft" ? "globe" : "talk", tone: e.kind === "web-draft" ? "orange" : "accent",
      title: e.label, sub: `${KIND_NAME[e.kind] || e.kind} · ${describe(e)}${e.enabled ? "" : e.kind === "space" ? " · muted" : " · off"}`, enabled: e.enabled }),
    onToggle: async (e, on) => { if (e.kind === "web-draft" && on && !await confirmWeb()) { await reload(); return; }
      await api(`/api/chatch/${e.id}`, { method: "PUT", json: { enabled: on } }); await reload(); },
    onEdit: (e) => formSheet({ title: e.label, fields: fieldsFor(e.kind), values: toForm(e), tall: true,
      onSave: async (v) => { await api(`/api/chatch/${e.id}`, { method: "PUT", json: cleanBody(e.kind, v) }); await reload(); good("Saved."); } }),
    onDelete: async (e) => { await api(`/api/chatch/${e.id}`, { method: "DELETE" }); await reload(); good("Deleted."); },
    onAdd: () => formSheet({ title: "Add a space or person", fields: fieldsFor("space"), values: { mention_only: false }, saveLabel: "Add",
      onSave: async (v) => { await api("/api/chatch", { method: "POST", json: { kind: "space", ...cleanBody("space", v) } }); await reload(); good("Added."); } }),
    onReset: async () => { await api("/api/chatch/reset", { method: "POST", json: { confirm: true } }); await reload(); good("Back to the defaults."); },
  });
  paintHead();

  const caps = el("section", { class: "ui-stack" }, el("h2", { class: "ui-caps st-caps" }, "What each way can and cannot do"),
    ...data.capabilities.map((c) => el("div", { class: "ui-stack" },
      el("p", { class: "ui-row__title" }, c.label, " ", ui.badge(c.works === true ? "works" : c.works === false ? "not offered" : "depends", c.works === true ? "good" : c.works === false ? "warn" : "info")),
      el("p", { class: "ui-hint" }, c.limits), el("p", { class: "ui-hint" }, "Checked: " + c.verified))));
  ui.sheet({ title: "Google Chat channels", class: "st-sheet st-sheet--tall", content: el("div", { class: "ui-stack ui-stack--lg" }, headBox, ed.el, caps,
    ui.notice("With a personal Google account there is no Chat webhook or Chat app, so REDLINE never sends a Chat message for you. It reads, drafts, and hands the text to you.", { tone: "info" })) });
}
