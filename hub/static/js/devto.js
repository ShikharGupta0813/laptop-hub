// Posts: post ideas, drafts, dev.to publishing, copy-paste packs for LinkedIn / X / Hashnode, and your dev.to stats.
// Routes: docs/api/devto.md. Everything shown is text (textContent); comments from dev.to are untrusted and only ever
// shown as text. Publishing is its own button with its own confirmation. LinkedIn is draft-and-paste only.
import { api, ApiError, el, renderMarkdown } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/devto.css";
const WARNING = "Do not publish company confidential material.";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TABS = [
  { value: "ideas", label: "Ideas" }, { value: "drafts", label: "Drafts" },
  { value: "published", label: "Published" }, { value: "settings", label: "Settings" },
];

async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    ui.toast(`${label} copied.`, { kind: "good" });
  } catch (_) {
    ui.toast("Copy was blocked. Select the text and copy it by hand.", { kind: "warn" });
  }
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "dv ui-stack ui-stack--lg" });
  container.append(root);
  const S = { tab: "ideas", status: null, ideas: [], drafts: [], current: null, pack: null, preview: false,
              published: null, settings: null, polish: null, error: "" };

  const run = async (fn) => {
    try { return await fn(); } catch (error) {
      if (error instanceof ApiError && error.status === 404) { S.error = "off"; paint(); } else fail(error);
      return null;
    }
  };

  async function load() {
    try {
      S.status = await api("/api/devto/status");
      S.ideas = (await api("/api/devto/ideas")).ideas || [];
      S.drafts = (await api("/api/devto/drafts")).drafts || [];
      S.error = "";
    } catch (error) { S.error = error instanceof ApiError && error.status === 404 ? "off" : error.message; }
    paint();
  }

  // ---- Ideas ---------------------------------------------------------------------------------------
  function ideasTab() {
    const topic = el("input", { class: "ui-input", maxlength: "300", "aria-label": "Topic", placeholder: "Or write about… e.g. why uploads time out behind a proxy" });
    const gen = ui.button({ label: "Find ideas from my work", icon: "sparkles", variant: "primary", onClick: () => ui.busy(gen, () => run(async () => {
      const r = await api("/api/devto/ideas/refresh", { method: "POST" });
      S.ideas = r.ideas;
      ui.toast(r.via === "ai" ? "Ideas ready." : "Ideas ready (made without the AI).", { kind: "good" });
      paint();
    })) });
    const write = ui.button({ label: "Write a draft", icon: "pen", onClick: () => ui.busy(write, () => draftFrom({ topic: topic.value }, topic)) });
    const rows = S.ideas.map((i) => el("li", { class: "dv-idea ui-card ui-stack" },
      el("p", { class: "dv-idea__title" }, i.title), el("p", { class: "dv-idea__angle" }, i.angle || ""),
      el("div", { class: "ui-row" }, ui.button({ label: "Draft this", icon: "pen", size: "sm", onClick: (e) => ui.busy(e.currentTarget, () => draftFrom({ idea_id: i.id })) }))));
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("section", { class: "ui-card ui-hud ui-stack dv-card" }, ui.eyebrow("01", "Ideas from your real work"),
        el("p", { class: "ui-hint" }, "Built from your memories (RCAs, decisions), build specs and commit titles. Anything marked private or confidential is skipped."),
        el("div", { class: "ui-row" }, gen)),
      S.ideas.length ? el("ul", { class: "dv-list" }, ...rows) : ui.emptyState({ icon: "sparkles", title: "No ideas yet", body: "Tap the button above.", compact: true }),
      el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("02", "Write about a topic"), topic, el("div", { class: "ui-row" }, write)));
  }

  async function draftFrom(payload, input) {
    await run(async () => {
      const d = await api("/api/devto/drafts", { method: "POST", json: payload });
      if (input) input.value = "";
      S.drafts = (await api("/api/devto/drafts")).drafts;
      S.current = d; S.tab = "drafts"; S.pack = null; S.preview = false;
      paint();
    });
  }

  // ---- Drafts --------------------------------------------------------------------------------------
  function statusBadge(d) {
    return ui.badge(d.status === "local" ? "local only" : d.status === "saved" ? "draft on dev.to" : "published",
      d.status === "published" ? "good" : d.status === "saved" ? "accent" : "warn");
  }

  function draftList() {
    if (!S.drafts.length) return ui.emptyState({ icon: "pen", title: "No drafts yet", body: "Pick an idea, or write about a topic.", compact: true });
    return el("div", { class: "ui-list ui-list--inset" }, ...S.drafts.map((d) => ui.listRow({
      icon: "pen", tone: d.blocked?.length ? "bad" : "accent", title: d.title, sub: `${d.words} words${d.blocked?.length ? " - blocked word" : ""}`,
      meta: d.status === "published" ? "live" : d.status === "saved" ? "on dev.to" : "local", chevron: true,
      onClick: () => { S.current = d; S.pack = null; S.preview = false; paint(); } })));
  }

  function field(label, node) { return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), node); }

  function editor() {
    const d = S.current;
    const locked = d.status === "published";
    const title = el("input", { class: "ui-input", maxlength: "140", value: d.title, disabled: locked, "aria-label": "Title" });
    const tags = el("input", { class: "ui-input", value: (d.tags || []).join(", "), disabled: locked, "aria-label": "Tags", placeholder: "up to 4, comma separated" });
    const desc = el("input", { class: "ui-input", maxlength: "200", value: d.description || "", disabled: locked, "aria-label": "Description" });
    const text = el("textarea", { class: "ui-input dv-text", rows: "16", disabled: locked, "aria-label": "Post text (markdown)" });
    text.value = d.body;
    const preview = el("div", { class: "dv-preview md" });
    if (S.preview) preview.innerHTML = renderMarkdown(d.body); // sanitised by DOMPurify inside renderMarkdown
    const toggle = ui.segmented({ label: "Editor or preview", value: S.preview ? "preview" : "edit",
      options: [{ value: "edit", label: "Edit" }, { value: "preview", label: "Preview" }],
      onChange: async (v) => { if (v === "preview") await save(true); S.preview = v === "preview"; paint(); } });

    async function save(quiet) {
      if (locked) return;
      await run(async () => {
        S.current = await api(`/api/devto/drafts/${d.id}`, { method: "PATCH", json: { title: title.value, body: text.value, tags: tags.value, description: desc.value } });
        S.drafts = (await api("/api/devto/drafts")).drafts;
        if (!quiet) { ui.toast("Saved on this laptop.", { kind: "good" }); paint(); }
      });
    }
    const saveBtn = ui.button({ label: "Save", icon: "check", onClick: () => ui.busy(saveBtn, () => save(false)) });
    const toDevto = ui.button({ label: d.status === "saved" ? "Update dev.to draft" : "Save as dev.to draft", icon: "upload", variant: "primary",
      disabled: locked || !S.status?.connected,
      onClick: () => ui.busy(toDevto, () => run(async () => {
        await save(true);
        S.current = await api(`/api/devto/drafts/${d.id}/save`, { method: "POST" });
        S.drafts = (await api("/api/devto/drafts")).drafts;
        ui.toast("Saved on dev.to as an unpublished draft. Check it there.", { kind: "good" });
        paint();
      })) });
    const publish = ui.button({ label: "Publish on dev.to", icon: "send", variant: "danger", disabled: d.status !== "saved",
      onClick: async () => {
        await save(true);
        const yes = await ui.confirm({ title: "Publish this post publicly?", message: `"${S.current.title}" goes live on dev.to now. ${WARNING}`, confirmLabel: "Publish", danger: true });
        if (!yes) return;
        await ui.busy(publish, () => run(async () => {
          S.current = await api(`/api/devto/drafts/${d.id}/publish`, { method: "POST", json: { confirm: true } });
          S.drafts = (await api("/api/devto/drafts")).drafts;
          ui.toast("Published.", { kind: "good" });
          paint();
        }));
      } });
    const packBtn = ui.button({ label: "Post pack", icon: "share", onClick: () => ui.busy(packBtn, () => run(async () => {
      await save(true);
      S.pack = await api(`/api/devto/drafts/${d.id}/pack`);
      paint();
    })) });
    const del = ui.button({ icon: "trash", iconOnly: true, variant: "ghost", "aria-label": "Delete this draft", onClick: async () => {
      if (!(await ui.confirm({ title: "Delete this draft?", message: "Only the copy on this laptop. dev.to is not touched.", confirmLabel: "Delete", danger: true }))) return;
      await run(async () => { await api(`/api/devto/drafts/${d.id}`, { method: "DELETE" }); S.current = null; S.pack = null; await load(); });
    } });
    const close = ui.button({ icon: "x", iconOnly: true, variant: "ghost", "aria-label": "Close the editor", onClick: () => { S.current = null; S.pack = null; paint(); } });
    const kids = [
      el("div", { class: "ui-row" }, statusBadge(d), d.generated_by === "template" ? ui.badge("template, no AI", "warn") : null, el("span", { class: "grow" }), del, close),
      ui.notice(WARNING, { tone: "warn" }),
    ];
    if (d.blocked?.length) kids.push(ui.notice(`Blocked: this text contains ${d.blocked.join(", ")} (your blocklist). It cannot be saved to dev.to, published or packed until you remove it.`, { tone: "bad" }));
    if (d.devto_url) kids.push(el("p", { class: "ui-hint dv-url" }, d.devto_url));
    kids.push(field("Title", title), toggle, S.preview ? preview : field("Text (markdown)", text), field("Tags", tags), field("Description", desc),
      el("div", { class: "ui-row dv-actions" }, saveBtn, toDevto, packBtn), el("div", { class: "ui-row dv-actions" }, publish),
      el("p", { class: "ui-hint" }, "Publishing is separate: save it on dev.to first, look at it there, then publish. Nothing here publishes by itself."));
    return el("section", { class: "ui-card ui-card--accent ui-stack dv-card dv-editor" }, ...kids);
  }

  function copyBlock(label, text) {
    return el("div", { class: "dv-copy ui-stack" },
      el("div", { class: "ui-row" }, el("span", { class: "ui-label grow" }, label),
        ui.button({ label: "Copy", icon: "copy", size: "sm", onClick: () => copy(text, label) })),
      el("pre", { class: "dv-pre" }, text));
  }

  function packPanel() {
    const p = S.pack;
    const link = (href, label) => el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm", href, target: "_blank", rel: "noopener noreferrer" }, label);
    return el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("03", "Post pack"),
      ui.notice(p.note, { tone: "info" }),
      copyBlock("LinkedIn post", p.linkedin.text), el("div", { class: "ui-row" }, link(p.linkedin.open, "Open LinkedIn composer")),
      copyBlock("X thread", p.x_thread.tweets.join("\n\n")), el("div", { class: "ui-row" }, link(p.x_thread.open, "Open X")),
      copyBlock("Hashnode / Medium markdown", p.hashnode_medium.markdown),
      el("div", { class: "ui-row" }, link(p.hashnode_medium.open_hashnode, "Open Hashnode"), link(p.hashnode_medium.open_medium, "Open Medium")));
  }

  function polishPanel() {
    const card = el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("04", "Profile polish"));
    if (!S.polish) {
      const btn = ui.button({ label: "Show the checklist", icon: "list-checks", onClick: () => ui.busy(btn, () => run(async () => { S.polish = await api("/api/devto/polish"); paint(); })) });
      card.append(el("p", { class: "ui-hint" }, "Headline, about, featured projects, skills, with wording you can copy."), btn);
      return card;
    }
    card.append(el("p", { class: "ui-hint" }, (S.polish.from_profile ? "Based on your saved profile. " : "No saved profile found, so these are templates. ") + S.polish.note));
    for (const i of S.polish.items) {
      card.append(el("div", { class: "dv-pol ui-stack" },
        el("div", { class: "ui-row" }, ui.badge(i.status === "ok" ? "looks fine" : "improve", i.status === "ok" ? "good" : "warn"), el("span", { class: "ui-label grow" }, i.label)),
        el("p", { class: "ui-hint" }, i.tip), copyBlock("Suggestion", i.suggestion)));
    }
    return card;
  }

  function draftsTab() {
    const kids = [];
    if (S.current) { kids.push(editor()); if (S.pack) kids.push(packPanel()); }
    else kids.push(el("section", { class: "ui-stack" }, ui.eyebrow("01", "Your drafts"), draftList()));
    kids.push(polishPanel());
    return el("div", { class: "ui-stack ui-stack--lg" }, ...kids);
  }

  // ---- Published (stats and comments) ---------------------------------------------------------------
  async function loadPublished(refresh) {
    await run(async () => {
      S.published = refresh ? await api("/api/devto/refresh", { method: "POST" }) : await api("/api/devto/published");
      paint();
    });
  }

  function publishedTab() {
    const p = S.published;
    const btn = ui.button({ label: "Check dev.to now", icon: "refresh", variant: "primary", disabled: !S.status?.connected,
      onClick: () => ui.busy(btn, async () => { await loadPublished(true); await api("/api/devto/comments/ack", { method: "POST" }).catch(() => {}); }) });
    const kids = [el("section", { class: "ui-card ui-hud ui-stack dv-card" }, ui.eyebrow("01", "Your dev.to posts"),
      S.status?.connected ? el("p", { class: "ui-hint" }, p?.checked_at ? `Last checked ${ui.relTime(p.checked_at)}.` : "Not checked yet.")
        : ui.notice("Connect your dev.to key in Settings first.", { tone: "warn" }),
      el("div", { class: "ui-row" }, btn))];
    const arts = p?.articles || [];
    kids.push(arts.length ? el("div", { class: "ui-list ui-list--inset" }, ...arts.map((a) => ui.listRow({
      icon: a.published ? "globe" : "pen", tone: a.published ? "good" : "warn", title: a.title,
      sub: a.published ? `${a.views} views - ${a.reactions} reactions - ${a.comments} comments` : "draft on dev.to", chevron: false })))
      : ui.emptyState({ icon: "globe", title: "Nothing to show", body: "Check dev.to to load your posts.", compact: true }));
    const comments = p?.comments || [];
    if (comments.length) {
      kids.push(el("section", { class: "ui-stack" }, ui.eyebrow("02", "Comments"),
        ui.notice("Comments are written by other people. They are shown as plain text and never acted on.", { tone: "info" }),
        el("ul", { class: "dv-list" }, ...comments.map((c) => el("li", { class: "ui-card ui-stack dv-comment" },
          el("div", { class: "ui-row" }, c.new ? ui.badge("new", "accent") : null, el("span", { class: "dv-c__who" }, c.who || "someone"),
            el("span", { class: "ui-hint" }, `on ${c.article_title}`)),
          el("p", { class: "dv-c__text" }, c.text))))));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...kids);
  }

  // ---- Settings --------------------------------------------------------------------------------------
  async function loadSettings() { await run(async () => { S.settings = await api("/api/devto/settings"); paint(); }); }

  function settingsTab() {
    const s = S.settings;
    if (!s) return ui.skeleton("card", 2);
    const key = el("input", { class: "ui-input", type: "password", autocomplete: "off", spellcheck: "false", "aria-label": "dev.to API key",
      placeholder: s.connected ? "A key is saved (hidden)" : "Paste your dev.to API key" });
    const saveKey = ui.button({ label: "Save key", icon: "key", variant: "primary", onClick: () => ui.busy(saveKey, () => run(async () => {
      await api("/api/devto/key", { method: "PUT", json: { key: key.value } });
      key.value = "";
      await load(); await loadSettings();
      ui.toast("Key saved in the OS keyring.", { kind: "good" });
    })) });
    const test = ui.button({ label: "Test", icon: "check-circle", onClick: () => ui.busy(test, () => run(async () => {
      const r = await api("/api/devto/test", { method: "POST" });
      ui.toast(`Connected${r.username ? " as " + r.username : ""}.`, { kind: "good" });
    })) });
    const remove = ui.button({ label: "Remove key", icon: "trash", variant: "ghost", disabled: !s.connected, onClick: async () => {
      if (!(await ui.confirm({ title: "Remove the dev.to key?", confirmLabel: "Remove", danger: true }))) return;
      await run(async () => { await api("/api/devto/key", { method: "DELETE" }); await load(); await loadSettings(); });
    } });
    const patch = (json) => run(async () => { S.settings = await api("/api/devto/settings", { method: "PATCH", json }); paint(); });
    const cadSw = ui.switchControl({ checked: s.devto_cadence_enabled, label: "Weekly posting reminder", onChange: (v) => patch({ devto_cadence_enabled: v }) });
    const statSw = ui.switchControl({ checked: s.devto_stats_enabled, label: "Check dev.to stats and comments", onChange: (v) => patch({ devto_stats_enabled: v }) });
    const days = el("input", { class: "ui-input", type: "number", min: "1", max: "90", value: String(s.devto_cadence_days), "aria-label": "Remind after this many days without a post" });
    days.addEventListener("change", () => patch({ devto_cadence_days: Number(days.value) }));
    const hours = el("input", { class: "ui-input", type: "number", min: "1", max: "168", value: String(s.devto_stats_hours), "aria-label": "Hours between checks" });
    hours.addEventListener("change", () => patch({ devto_stats_hours: Number(hours.value) }));
    const block = el("textarea", { class: "ui-input dv-block", rows: "4", "aria-label": "Blocklist words, one per line", placeholder: "One word or name per line" });
    block.value = (s.devto_blocklist_words || []).join("\n");
    const saveBlock = ui.button({ label: "Save blocklist", icon: "shield", onClick: () => ui.busy(saveBlock, async () => {
      await patch({ devto_blocklist_words: block.value.split("\n").map((w) => w.trim()).filter(Boolean) });
      ui.toast("Blocklist saved.", { kind: "good" });
    }) });
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("section", { class: "ui-card ui-hud ui-stack dv-card" }, ui.eyebrow("01", "dev.to connection"),
        ui.badge(s.connected ? "connected" : "not connected", s.connected ? "good" : "warn"),
        el("p", { class: "ui-hint" }, "Make a key at dev.to/settings/extensions. It is stored only in this laptop's keyring and is never shown again."),
        key, el("div", { class: "ui-row" }, saveKey, test, remove)),
      el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("02", "Blocklist"), ui.notice(WARNING, { tone: "warn" }),
        el("p", { class: "ui-hint" }, "Words that must never appear in a post (a company, a project, a customer). A draft with one cannot be saved to dev.to, published or packed."),
        block, el("div", { class: "ui-row" }, saveBlock)),
      el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("03", "Reminders and checks (off by default)"),
        el("div", { class: "ui-row" }, cadSw, el("span", { class: "grow" }, "Weekly posting reminder (Friday)")), field("Remind after this many days without a post", days),
        el("div", { class: "ui-row" }, statSw, el("span", { class: "grow" }, "Check dev.to stats and new comments")), field("Hours between checks", hours),
        el("p", { class: "ui-hint" }, "Reminders and checks only tell you things. They never publish anything.")));
  }

  // ---- shell -----------------------------------------------------------------------------------------
  const tabs = ui.segmented({ label: "Posts sections", value: S.tab, options: TABS, block: true, onChange: (v) => {
    S.tab = v;
    if (v === "published" && !S.published) loadPublished(false);
    if (v === "settings") loadSettings();
    paint();
  } });
  tabs.classList.add("dv-tabs");

  function paint() {
    if (S.error === "off") { root.replaceChildren(ui.emptyState({ icon: "pen", title: "Posts is off", body: "Turn it on with devto_enabled in hub.json." })); return; }
    tabs.value = S.tab;
    const view = S.tab === "ideas" ? ideasTab() : S.tab === "drafts" ? draftsTab() : S.tab === "published" ? publishedTab() : settingsTab();
    root.replaceChildren(tabs, S.error ? ui.notice(S.error, { tone: "bad" }) : null, S.tab === "ideas" ? ui.notice(WARNING, { tone: "warn" }) : null, view);
  }

  paint();
  load();
  return { show() { load(); }, refresh() { load(); } };
}
