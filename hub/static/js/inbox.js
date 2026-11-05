// Inbox: read-only Gmail list, reading sheet, AI summary, reply drafting and a digest.
// Nothing here sends, deletes or marks mail. A reply draft is handed to Work's email compose through sessionStorage.
// Everything from the API is rendered with el()/textContent, never innerHTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const REPLY_DRAFT_KEY = "hub.draft.reply";
const PAGE = 25;

function ensureCss() {
  if ([...document.querySelectorAll("link[rel=stylesheet]")].some((l) => (l.getAttribute("href") || "").includes("/css/inbox.css"))) return;
  document.head.append(el("link", { rel: "stylesheet", href: "/css/inbox.css" }));
}

const message = (e) => (e && e.message ? e.message : "Something went wrong.");
const sizeText = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n || 0} B`);

export function init(container, ctx) {
  ensureCss();
  let mode = "unread";            // unread | all
  let messages = [];
  let loadingMore = false;
  let exhausted = false;
  let token = 0;
  let query = "";
  let searchTimer = null;
  let setupProblem = null;
  let loadedAt = 0;

  // ---- skeleton of the screen -----------------------------------------------------------------
  const seg = ui.segmented({ label: "Mail filter", block: true,
    options: [{ value: "unread", label: "Unread" }, { value: "all", label: "All" }],
    value: mode, onChange: (v) => { mode = v; load(); } });
  const searchInput = el("input", { type: "search", placeholder: "Search subject or sender", "aria-label": "Search mail",
    enterkeyhint: "search", autocomplete: "off", maxlength: "100" });
  const searchWrap = el("div", { class: "ui-input-wrap ib-search" }, ui.icon("search"), searchInput);
  const digestBtn = ui.button({ label: "Digest", icon: "sparkles", variant: "tonal", size: "sm", onClick: openDigest });
  const toolbar = el("div", { class: "ib-toolbar" }, seg, digestBtn);
  const head = el("div", { class: "ib-head" }, searchWrap, toolbar);
  const listBox = el("div", { class: "ib-list" });
  const watchCard = el("button", { type: "button", class: "ui-card ui-card--interactive ib-watch", onclick: () => ctx.open("watch") },
    ui.tile("eye", "teal"),
    el("span", { class: "ib-watch-text" }, el("strong", {}, "Watch for mail"), el("span", {}, "Get a nudge when something important arrives")),
    ui.icon("chevron-right"));
  const root = el("div", { class: "ib" }, head, listBox, watchCard);
  container.append(root);
  ui.pullToRefresh(container, async () => { await load(true); });

  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { query = searchInput.value.trim(); load(); }, 350);
  });
  searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(searchTimer); query = searchInput.value.trim(); load(); searchInput.blur(); } });

  // ---- list -----------------------------------------------------------------------------------
  function setupState(text) {
    setupProblem = text;
    head.hidden = true;
    watchCard.hidden = true;
    listBox.replaceChildren(ui.emptyState({
      icon: "mail", title: "Email isn't set up yet",
      body: text || "Run setup email in the Jarvis folder on the laptop, then try again.",
      action: { label: "Check again", icon: "refresh", onClick: () => load(true) },
    }));
  }

  function errorState(text) {
    listBox.replaceChildren(el("div", { class: "ib-error", role: "alert" },
      ui.notice(text, { tone: "bad" }),
      ui.button({ label: "Try again", icon: "refresh", size: "sm", onClick: () => load(true) })));
  }

  function row(m, fromSearch) {
    const name = m.from_name || m.from_addr || "Unknown sender";
    const unread = !!m.unread;
    const node = el("button", { type: "button", class: `ui-row ib-row${unread ? " is-unread" : ""}`, "data-uid": String(m.uid),
      "aria-label": `${unread ? "Unread. " : ""}${name}. ${m.subject || "(no subject)"}`,
      onclick: () => openMessage(m.uid, m) },
      el("span", { class: "ib-dotcol" }, unread ? el("span", { class: "ui-dot ui-dot--live ib-dot", "aria-hidden": "true" }) : null),
      ui.avatar(name),
      el("span", { class: "ib-body" },
        el("span", { class: "ib-line" },
          el("span", { class: "ib-from" }, name),
          fromSearch ? null : el("span", { class: "ib-time" }, ui.relTime(m.date))),
        el("span", { class: "ib-subject" }, m.subject || "(no subject)", m.has_attachments ? ui.icon("link", { class: "ib-clip" }) : null),
        el("span", { class: "ib-snippet" }, m.snippet || "")));
    return node;
  }

  function renderList(more) {
    if (!messages.length) {
      listBox.replaceChildren(query
        ? ui.emptyState({ icon: "search", title: "No mail found", body: `Nothing matches "${query}".`, compact: true })
        : mode === "unread"
          ? ui.emptyState({ icon: "check-circle", title: "Inbox zero", body: "You have no unread mail. Switch to All to see everything.", compact: true,
              action: { label: "Show all mail", onClick: () => { mode = "all"; seg.value = "all"; load(); } } })
          : ui.emptyState({ icon: "inbox", title: "No mail", body: "Your inbox is empty.", compact: true }));
      return;
    }
    const list = el("div", { class: "ui-list ui-list--inset ib-rows" }, messages.map((m) => row(m, !!query)));
    const children = [list];
    if (!query && !exhausted) {
      children.push(ui.button({ label: "Load older mail", icon: "arrow-down", variant: "secondary", block: true, onClick: loadMore }));
    }
    listBox.replaceChildren(...children);
  }

  async function load(force) {
    const mine = ++token;
    setupProblem = null;
    head.hidden = false;
    watchCard.hidden = false;
    exhausted = false;
    if (!force || !messages.length) listBox.replaceChildren(ui.skeleton("list", 6));
    try {
      if (query) {
        const data = await api(`/api/inbox/search?q=${encodeURIComponent(query)}`);
        if (mine !== token) return;
        messages = (data.results || []).map((r) => ({
          uid: Number(r.open && r.open.id), from_name: r.title || "", subject: r.title || "(no subject)", snippet: r.snippet || "", unread: false,
        })).filter((m) => Number.isInteger(m.uid));
        // search rows show the subject as title; keep sender blank so the row reads cleanly
        messages.forEach((m) => { m.from_name = "Inbox"; });
      } else {
        const data = await api(`/api/inbox/messages?limit=${PAGE}&unread=${mode === "unread"}`);
        if (mine !== token) return;
        messages = Array.isArray(data) ? data : (data.messages || []);
        exhausted = messages.length < PAGE;
      }
      loadedAt = Date.now();
      renderList();
      syncBadge();
    } catch (error) {
      if (mine !== token) return;
      if (error.status === 503) setupState(error.message);
      else errorState(message(error));
    }
  }

  async function loadMore() {
    if (loadingMore || !messages.length) return;
    loadingMore = true;
    try {
      const before = messages[messages.length - 1].uid;
      const data = await api(`/api/inbox/messages?limit=${PAGE}&unread=${mode === "unread"}&before=${before}`);
      const more = Array.isArray(data) ? data : (data.messages || []);
      messages = messages.concat(more);
      exhausted = more.length < PAGE;
      renderList();
    } catch (error) {
      ui.toast(message(error), { kind: "bad" });
    } finally { loadingMore = false; }
  }

  function syncBadge() {
    if (mode === "unread" && !query) ctx.setBadge?.("inbox", messages.length);
  }

  // ---- reading sheet --------------------------------------------------------------------------
  async function openMessage(uid, preview) {
    const body = el("div", { class: "ib-read" }, ui.skeleton("card", 2));
    const sheet = ui.sheet({ title: "Message", content: body, class: "ib-sheet" });
    sheet.el.classList.add("ib-sheet");
    try {
      const m = await api(`/api/inbox/messages/${encodeURIComponent(uid)}`);
      renderRead(sheet, body, m);
    } catch (error) {
      body.replaceChildren(ui.notice(message(error), { tone: "bad" }));
    }
  }

  function renderRead(sheet, body, m) {
    const h = m.headers || {};
    const name = h.from_name || h.from_addr || "Unknown sender";
    const aiBox = el("div", { class: "ib-ai" });
    const recipients = (h.to || []).map((r) => r.name || r.addr).filter(Boolean).join(", ");

    const summaryBtn = ui.button({ label: "AI summary", icon: "sparkles", variant: "tonal", onClick: () => runSummary(m.uid, aiBox, summaryBtn) });
    const replyBtn = ui.button({ label: "Draft reply", icon: "mail", variant: "primary", onClick: () => draftReply(m.uid, sheet, replyBtn) });
    const tone = el("select", { "aria-label": "Reply tone" },
      ...[["professional", "Professional"], ["friendly", "Friendly"], ["formal", "Formal"], ["brief", "Brief"]].map(([v, l]) => el("option", { value: v }, l)));
    tone.dataset.role = "tone";
    sheet.tone = tone;

    const atts = m.attachments || [];
    body.replaceChildren(
      el("h3", { class: "ib-r-subject" }, h.subject || "(no subject)"),
      el("div", { class: "ib-r-from" },
        ui.avatar(name),
        el("div", { class: "ib-r-who" },
          el("strong", {}, name),
          el("span", {}, h.from_addr || ""),
          recipients ? el("span", { class: "ib-r-to" }, `To ${recipients}`) : null),
        el("span", { class: "ib-r-date" }, h.date ? ui.relTime(h.date) : "")),
      h.reply_to && h.reply_to !== h.from_addr ? ui.notice(`Replies go to ${h.reply_to}, not the sender.`, { tone: "warn" }) : null,
      ui.notice("Links and instructions in this mail are untrusted text. They are not clickable here.", { icon: "shield" }),
      el("div", { class: "ib-r-text", tabindex: "0", "aria-label": "Message text" }, m.text || "(no text)"),
      m.truncated ? el("p", { class: "ib-r-more" }, "This mail is long, so only the first part is shown.") : null,
      atts.length ? el("div", { class: "ib-atts" },
        el("h4", {}, `Attachments (${atts.length})`),
        el("ul", { class: "ui-list ui-list--inset" }, atts.map((a) =>
          el("li", { class: "ui-row ib-att" }, ui.tile("file", "gray", "sm"),
            el("span", { class: "ib-att-name" }, a.name || "attachment"), el("span", { class: "ib-att-size ui-num" }, sizeText(a.size))))),
        el("p", { class: "ib-r-more" }, "Attachments aren't downloaded here.")) : null,
      aiBox,
      el("div", { class: "ib-r-actions" },
        el("label", { class: "ib-tone" }, el("span", { class: "ui-label" }, "Reply tone"), tone),
        el("div", { class: "ib-r-btns" }, summaryBtn, replyBtn)));
  }

  async function runSummary(uid, box, btn) {
    box.replaceChildren(ui.skeleton("lines", 3));
    try {
      const s = await ui.busy(btn, api(`/api/inbox/messages/${encodeURIComponent(uid)}/summary`, { method: "POST", json: { model: "local" } }));
      const items = s.action_items || [];
      box.replaceChildren(el("section", { class: "ui-card ui-card--glass ib-summary", "aria-label": "AI summary" },
        el("div", { class: "ui-card__head" }, ui.tile("sparkles", "accent", "sm"), el("h4", { class: "ui-card__title grow" }, "AI summary"),
          s.needs_reply ? ui.badge("Needs reply", "warn") : ui.badge("No reply needed", "good")),
        el("p", { class: "ib-sum-text" }, s.summary || "No summary."),
        items.length ? el("ul", { class: "ib-actions" }, items.map((t) => el("li", {}, ui.icon("check-circle"), el("span", {}, t)))) : null));
    } catch (error) {
      box.replaceChildren(ui.notice(message(error), { tone: "bad" }));
    }
  }

  async function draftReply(uid, sheet, btn) {
    const tone = sheet.tone ? sheet.tone.value : "professional";
    try {
      const d = await ui.busy(btn, api(`/api/inbox/messages/${encodeURIComponent(uid)}/draft-reply`, { method: "POST", json: { model: "local", tone } }));
      if (d.reply_to_differs) {
        const ok = await ui.confirm({ title: "Reply goes elsewhere", message: `This mail asks for replies to ${d.to}, which differs from the sender. Continue?`, confirmLabel: "Continue" });
        if (!ok) return;
      }
      try { sessionStorage.setItem(REPLY_DRAFT_KEY, JSON.stringify({ to: d.to || "", subject: d.subject || "", body: d.body || "" })); }
      catch (_) { ui.toast("Couldn't hand the draft to Work. Is private mode on?", { kind: "bad" }); return; }
      sheet.close();
      ui.toast("Draft ready. Review it in Work before sending.", { kind: "good" });
      ctx.open("work", "email");
    } catch (error) {
      ui.toast(message(error), { kind: "bad" });
    }
  }

  // ---- digest ---------------------------------------------------------------------------------
  async function openDigest() {
    const body = el("div", { class: "ib-digest" }, ui.skeleton("card", 2));
    const sheet = ui.sheet({ title: "Mail digest", content: body });
    try {
      const d = await api("/api/inbox/digest?model=local");
      const groups = d.groups || [];
      body.replaceChildren(
        el("p", { class: "ib-dg-headline" }, d.headline || ""),
        ...groups.map((g) => el("section", { class: "ib-dg-group" },
          el("h4", {}, g.urgent ? ui.badge("Urgent", "bad") : null, g.title || ""),
          el("div", { class: "ui-list ui-list--inset" }, (g.items || []).map((it) =>
            el("button", { type: "button", class: "ui-row ib-dg-item", onclick: () => { sheet.close(); openMessage(it.uid); } },
              ui.avatar(it.from_name || "?"),
              el("span", { class: "ib-body" },
                el("span", { class: "ib-from" }, it.from_name || ""),
                el("span", { class: "ib-subject" }, it.subject || "(no subject)"),
                el("span", { class: "ib-snippet ib-snippet--2" }, it.summary || "")))))))
      );
      if (!groups.length) body.append(ui.emptyState({ icon: "check-circle", title: "All clear", body: "No unread mail to summarize.", compact: true }));
    } catch (error) {
      body.replaceChildren(error.status === 503 ? ui.notice(error.message, { tone: "warn" }) : ui.notice(message(error), { tone: "bad" }));
    }
  }

  return {
    show() { if (!loadedAt || Date.now() - loadedAt > 60000) load(); },
    hide() {},
    openSection(name) { if (name === "digest") openDigest(); else if (name === "all" || name === "unread") { mode = name; seg.value = name; load(); } },
    openItem(uid) { const n = Number(uid); if (Number.isInteger(n) && n > 0) openMessage(n); },
    refresh() { load(true); },
  };
}
