// Read later: save links, see title / site / reading time / tags, get a TL;DR, mark read, delete.
// Talks to /api/bookmarks (docs/api/life.md). A link can arrive as #/bookmarks?url=... (share target).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const errText = (e) => (e && e.message) || "Something went wrong.";
const safeHref = (u) => { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : ""; } catch (_) { return ""; } };

export function init(container, ctx) {
  if (!document.querySelector('link[href="/css/bookmarks.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/bookmarks.css" }));
  }
  container.classList.add("bm-root");
  let filter = "all";            // all | unread
  let tag = "";
  let q = "";
  let items = [];
  let loaded = false;
  let token = 0;
  const summaryOpen = new Set();
  const summaries = new Map();   // id -> text, so a re-render keeps what was fetched

  const urlInput = el("input", { type: "url", inputmode: "url", class: "ui-input--lg", placeholder: "Paste a link to read later", autocomplete: "off",
    autocapitalize: "off", spellcheck: "false", enterkeyhint: "go", "aria-label": "Link to save" });
  const addBtn = ui.button({ label: "Save", icon: "plus", variant: "primary", onClick: () => add() });
  const addErr = el("p", { class: "bm-hint is-error", role: "alert", hidden: true });
  const addForm = el("form", { class: "ui-card ui-card--elevated bm-add", novalidate: true },
    el("div", { class: "bm-add-row" }, el("div", { class: "ui-input-wrap" }, ui.icon("link"), urlInput), addBtn), addErr);
  addForm.addEventListener("submit", (e) => { e.preventDefault(); add(); });

  const seg = ui.segmented({ label: "Filter", value: "all", block: true, options: [{ value: "all", label: "All" }, { value: "unread", label: "Unread" }],
    onChange: (v) => { filter = v; load(); } });
  const search = el("input", { type: "search", placeholder: "Search saved links", autocomplete: "off", "aria-label": "Search saved links" });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = search.value.trim(); load(); }, 250); });
  const tagRow = el("div", { class: "bm-tags ui-hscroll", hidden: true, role: "group", "aria-label": "Tags" });
  const list = el("div", { class: "ui-stack bm-list", "aria-live": "polite" });
  container.append(el("div", { class: "bm-wrap" }, addForm, seg, el("div", { class: "ui-input-wrap" }, ui.icon("search"), search), tagRow, list));

  function setErr(text) { addErr.textContent = text || ""; addErr.hidden = !text; }

  async function add(url) {
    const value = (url || urlInput.value).trim();
    if (!value) { setErr("Paste a link first."); urlInput.focus(); return; }
    setErr("");
    try {
      const b = await ui.busy(addBtn, api("/api/bookmarks", { method: "POST", json: { url: /^[a-z][a-z0-9+.-]*:/i.test(value) ? value : `https://${value}` } }));
      urlInput.value = "";
      ui.toast(b && b.fetched === false ? "Saved. The page couldn't be read, so it only has its address." : "Saved for later", { kind: "good" });
      ui.haptic("success");
      load();
    } catch (e) {
      setErr(e.status === 409 ? "You already saved that link." : errText(e));
      ui.haptic("error");
    }
  }

  async function toggleRead(b) {
    try {
      const r = await api(`/api/bookmarks/${b.id}/read`, { method: "POST" });
      ui.toast(r.read ? "Marked as read" : "Marked as unread", { kind: "good" });
      load();
    } catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
  }

  async function remove(b) {
    if (!(await ui.confirm({ title: "Delete this link?", message: b.title || b.url, confirmLabel: "Delete", danger: true }))) return;
    try { await api(`/api/bookmarks/${b.id}`, { method: "DELETE" }); ui.toast("Deleted", { kind: "good" }); load(); }
    catch (e) { ctx.toast(errText(e), { kind: "bad" }); }
  }

  async function summarize(b, btn, refresh = false) {
    try {
      const d = await ui.busy(btn, api(`/api/bookmarks/${b.id}/summarize`, { method: "POST", json: refresh ? { refresh: true } : {} }));
      summaries.set(b.id, d.summary || "");
      summaryOpen.add(b.id);
      paint();
    } catch (e) {
      ctx.toast(e.status === 503 ? "The AI model isn't available right now. Try again in a moment." : errText(e), { kind: "bad" });
    }
  }

  function card(b) {
    const href = safeHref(b.url);
    const summary = summaries.get(b.id) ?? b.summary ?? "";
    const open = summaryOpen.has(b.id) && summary;
    const meta = [b.host, b.reading_min ? `${b.reading_min} min read` : "", b.added ? ui.relTime(b.added) : ""].filter(Boolean).join(" · ");
    const sumBtn = ui.button({ label: summary ? (open ? "Hide TL;DR" : "Show TL;DR") : "TL;DR", icon: "sparkles", variant: "tonal", size: "sm", onClick: () => {
      if (summary) { if (open) summaryOpen.delete(b.id); else summaryOpen.add(b.id); paint(); }
      else summarize(b, sumBtn);
    } });
    return el("article", { class: `ui-card bm-card${b.read ? " is-read" : ""}`, "data-id": b.id },
      el("div", { class: "bm-head" },
        ui.tile(b.read ? "check-circle" : "bookmark", b.read ? "good" : "accent"),
        el("div", { class: "bm-main" },
          href ? el("a", { class: "bm-title", href, target: "_blank", rel: "noopener noreferrer" }, b.title || b.host || b.url)
            : el("span", { class: "bm-title" }, b.title || b.url),
          el("div", { class: "bm-meta" }, meta, !b.read ? ui.badge("Unread", "accent") : null))),
      b.description ? el("p", { class: "bm-desc" }, b.description) : null,
      b.tags && b.tags.length ? el("div", { class: "bm-tagline" }, b.tags.map((t) => el("span", { class: "ui-chip ui-chip--static" }, `#${t}`))) : null,
      open ? el("div", { class: "bm-summary" }, el("div", { class: "ui-caps" }, "TL;DR"), el("p", {}, summary),
        el("button", { type: "button", class: "ui-link", onclick: (ev) => summarize(b, ev.currentTarget, true) }, "Redo summary")) : null,
      el("div", { class: "bm-actions" }, sumBtn,
        ui.button({ label: b.read ? "Mark unread" : "Mark read", icon: b.read ? "circle" : "check", variant: "secondary", size: "sm", onClick: () => toggleRead(b) }),
        ui.button({ iconOnly: true, icon: "trash", variant: "ghost", size: "sm", label: `Delete ${b.title || "link"}`, onClick: () => remove(b) })));
  }

  function paint() {
    const tags = [...new Set(items.flatMap((b) => b.tags || []))].sort();
    tagRow.hidden = tags.length === 0;
    tagRow.replaceChildren(...tags.map((t) => el("button", { type: "button", class: `ui-chip${t === tag ? " is-on" : ""}`, "aria-pressed": String(t === tag),
      onclick: () => { tag = t === tag ? "" : t; load(); } }, `#${t}`)));
    if (!items.length) {
      const filtered = q || tag || filter === "unread";
      list.replaceChildren(ui.emptyState(filtered
        ? { icon: "bookmark", title: filter === "unread" && !q && !tag ? "All caught up" : "Nothing matches", body: filter === "unread" && !q && !tag ? "You have read everything you saved." : "Try a different search or filter.", compact: true }
        : { icon: "bookmark", title: "Nothing saved yet", body: "Paste a link above, or share a page from your browser to this app.", action: { label: "Paste a link", icon: "link", onClick: () => urlInput.focus() } }));
      return;
    }
    list.replaceChildren(...items.map(card));
  }

  async function load() {
    const my = ++token;
    if (!loaded) list.replaceChildren(ui.skeleton("list", 3));
    try {
      const p = new URLSearchParams();
      if (q) p.set("q", q);
      if (tag) p.set("tag", tag);
      if (filter === "unread") p.set("unread", "true");
      const d = await api(`/api/bookmarks${p.size ? `?${p}` : ""}`);
      if (my !== token) return;
      items = d.bookmarks || [];
      loaded = true;
      paint();
    } catch (e) {
      if (my !== token) return;
      list.replaceChildren(el("div", { class: "bm-error" }, ui.notice(errText(e), { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "secondary", size: "sm", onClick: load })));
    }
  }

  // #/bookmarks?url=...&title=...&text=... : the phone's share sheet or a bookmarklet.
  async function takeShared() {
    const h = location.hash || "";
    const at = h.indexOf("?");
    if (at < 0) return;
    const params = new URLSearchParams(h.slice(at + 1));
    if (!params.has("url") && !params.has("text")) return;
    try { history.replaceState(history.state, "", h.slice(0, at)); } catch (_) { /* ignore */ }
    try {
      const d = await api(`/api/bookmarks/share?${new URLSearchParams({ url: params.get("url") || "", title: params.get("title") || "", text: params.get("text") || "" })}`);
      if (d.already_saved) { ctx.toast("You already saved that link."); return; }
      urlInput.value = d.url;
      add(d.url);
    } catch (e) {
      const raw = params.get("url") || "";
      if (raw) urlInput.value = raw;
      setErr(errText(e));
    }
  }

  load();
  takeShared();
  return {
    show() { if (loaded) load(); takeShared(); },
    openSection(name) { if (name === "add") urlInput.focus(); else if (name === "unread") { filter = "unread"; seg.value = "unread"; load(); } },
    openItem() { load(); },
    refresh() { load(); },
  };
}
