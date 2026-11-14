// Posts: ideas, drafts, ONE draft to MANY destinations (dev.to, Hashnode, WordPress, Ghost, a markdown folder, a webhook,
// and 'manual' sites like LinkedIn, Medium, X, Reddit), your stats, and the Destinations list you edit yourself.
// Routes: docs/api/destinations.md. Everything shown is text (textContent); comments from destinations are untrusted and only
// ever shown as text. Publishing is its own button with its own confirmation, one destination at a time.
import { api, ApiError, el, renderMarkdown } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/posts.css";
const WARNING = "Do not publish company confidential material.";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TABS = [
  { value: "ideas", label: "Ideas" }, { value: "drafts", label: "Drafts" },
  { value: "published", label: "Published" }, { value: "destinations", label: "Destinations" },
];
const STYLES = [["article", "Article (full markdown)"], ["short", "Short post"], ["thread", "Numbered thread"]];

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
  const S = { tab: "ideas", status: null, ideas: [], drafts: [], dests: [], kinds: [], current: null, pick: new Set(), prep: null,
              preview: false, published: null, settings: null, polish: null, edit: null, error: "" };

  const run = async (fn) => {
    try { return await fn(); } catch (error) {
      if (error instanceof ApiError && error.status === 404 && /switched off/i.test(error.message || "")) { S.error = "off"; paint(); } else fail(error);
      return null;
    }
  };
  const destOf = (id) => S.dests.find((d) => d.id === id);
  const refreshDrafts = async () => { S.drafts = (await api("/api/posts/drafts")).drafts; };

  async function load() {
    try {
      S.status = await api("/api/posts/status");
      S.ideas = (await api("/api/posts/ideas")).ideas || [];
      await refreshDrafts();
      const d = await api("/api/destinations");
      S.dests = d.destinations; S.kinds = d.kinds;
      if (S.current) S.current = S.drafts.find((x) => x.id === S.current.id) || null;
      S.error = "";
    } catch (error) { S.error = error instanceof ApiError && error.status === 404 ? "off" : error.message; }
    paint();
  }

  // ---- Ideas ---------------------------------------------------------------------------------------
  function ideasTab() {
    const topic = el("input", { class: "ui-input", maxlength: "300", "aria-label": "Topic", placeholder: "Or write about… e.g. why uploads time out behind a proxy" });
    const gen = ui.button({ label: "Find ideas from my work", icon: "sparkles", variant: "primary", onClick: () => ui.busy(gen, () => run(async () => {
      const r = await api("/api/posts/ideas/refresh", { method: "POST" });
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
      const d = await api("/api/posts/drafts", { method: "POST", json: payload });
      if (input) input.value = "";
      await refreshDrafts();
      S.current = d; S.tab = "drafts"; S.prep = null; S.preview = false;
      paint();
    });
  }

  // ---- Drafts --------------------------------------------------------------------------------------
  const STATE_LABEL = { saved: "draft saved", published: "published", posted: "posted" };
  const STATE_TONE = { saved: "accent", published: "good", posted: "good" };

  function draftList() {
    if (!S.drafts.length) return ui.emptyState({ icon: "pen", title: "No drafts yet", body: "Pick an idea, or write about a topic.", compact: true });
    return el("div", { class: "ui-list ui-list--inset" }, ...S.drafts.map((d) => {
      const n = Object.keys(d.targets || {}).length;
      return ui.listRow({ icon: "pen", tone: d.blocked?.length ? "bad" : "accent", title: d.title,
        sub: `${d.words} words${d.blocked?.length ? " - blocked word" : ""}`, meta: n ? `${n} place${n === 1 ? "" : "s"}` : "local", chevron: true,
        onClick: () => { S.current = d; S.prep = null; S.preview = false; paint(); } });
    }));
  }

  function field(label, node) { return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), node); }

  function editor() {
    const d = S.current;
    const title = el("input", { class: "ui-input", maxlength: "140", value: d.title, "aria-label": "Title" });
    const tags = el("input", { class: "ui-input", value: (d.tags || []).join(", "), "aria-label": "Tags", placeholder: "comma separated; each destination keeps its own limit" });
    const desc = el("input", { class: "ui-input", maxlength: "200", value: d.description || "", "aria-label": "Description" });
    const canon = el("input", { class: "ui-input", maxlength: "300", value: d.canonical_url || "", "aria-label": "Canonical link (optional)", placeholder: "https://… the original, if it lives elsewhere" });
    const text = el("textarea", { class: "ui-input dv-text", rows: "16", "aria-label": "Post text (markdown)" });
    text.value = d.body;
    const preview = el("div", { class: "dv-preview md" });
    if (S.preview) preview.innerHTML = renderMarkdown(d.body); // sanitised by DOMPurify inside renderMarkdown
    const toggle = ui.segmented({ label: "Editor or preview", value: S.preview ? "preview" : "edit",
      options: [{ value: "edit", label: "Edit" }, { value: "preview", label: "Preview" }],
      onChange: async (v) => { if (v === "preview") await save(true); S.preview = v === "preview"; paint(); } });

    async function save(quiet) {
      await run(async () => {
        S.current = await api(`/api/posts/drafts/${d.id}`, { method: "PATCH", json: { title: title.value, body: text.value, tags: tags.value, description: desc.value, canonical_url: canon.value } });
        await refreshDrafts();
        if (!quiet) { ui.toast("Saved on this laptop.", { kind: "good" }); paint(); }
      });
    }
    const saveBtn = ui.button({ label: "Save", icon: "check", onClick: () => ui.busy(saveBtn, () => save(false)) });
    const del = ui.button({ icon: "trash", iconOnly: true, variant: "ghost", "aria-label": "Delete this draft", onClick: async () => {
      if (!(await ui.confirm({ title: "Delete this draft?", message: "Only the copy on this laptop. Nothing already saved or published elsewhere is touched.", confirmLabel: "Delete", danger: true }))) return;
      await run(async () => { await api(`/api/posts/drafts/${d.id}`, { method: "DELETE" }); S.current = null; S.prep = null; await load(); });
    } });
    const close = ui.button({ icon: "x", iconOnly: true, variant: "ghost", "aria-label": "Close the editor", onClick: () => { S.current = null; S.prep = null; paint(); } });
    const kids = [
      el("div", { class: "ui-row" }, ui.badge(d.status === "local" ? "local only" : d.status === "saved" ? "draft saved somewhere" : "published", d.status === "published" ? "good" : d.status === "saved" ? "accent" : "warn"),
        d.generated_by === "template" ? ui.badge("template, no AI", "warn") : null, el("span", { class: "grow" }), del, close),
      ui.notice(WARNING, { tone: "warn" }),
    ];
    if (d.blocked?.length) kids.push(ui.notice(`Blocked: this text contains ${d.blocked.join(", ")} (your blocklist). It cannot be saved to a destination, published or turned into a post until you remove it.`, { tone: "bad" }));
    kids.push(field("Title", title), toggle, S.preview ? preview : field("Text (markdown)", text), field("Tags", tags), field("Description", desc), field("Canonical link", canon),
      el("div", { class: "ui-row dv-actions" }, saveBtn));
    return el("section", { class: "ui-card ui-card--accent ui-stack dv-card dv-editor" }, ...kids, destinationsPanel(save));
  }

  // One draft to many destinations: pick places, prepare variants (unpublished drafts only), publish each on its own.
  function destinationsPanel(save) {
    const d = S.current;
    const usable = S.dests.filter((x) => x.enabled);
    const rows = usable.map((x) => {
      const t = (d.targets || {})[x.id];
      const box = el("input", { type: "checkbox", checked: S.pick.has(x.id) ? "" : null, "aria-label": `Include ${x.label}` });
      if (S.pick.has(x.id)) box.checked = true;
      box.addEventListener("change", () => { box.checked ? S.pick.add(x.id) : S.pick.delete(x.id); });
      const acts = [];
      if (x.mode === "manual") {
        acts.push(ui.button({ label: "Show text", icon: "copy", size: "sm", onClick: (e) => ui.busy(e.currentTarget, () => run(async () => {
          await save(true); S.prep = { variants: (await api(`/api/posts/drafts/${d.id}/variants?destinations=${encodeURIComponent(x.id)}`)).variants }; paint(); })) }));
      } else {
        if (x.drafts) acts.push(ui.button({ label: t?.status === "saved" ? "Update draft" : "Save draft", icon: "upload", size: "sm", disabled: !x.connected || t?.status === "published",
          onClick: (e) => ui.busy(e.currentTarget, () => run(async () => {
            await save(true);
            S.current = await api(`/api/posts/drafts/${d.id}/destinations/${x.id}/save`, { method: "POST" });
            await refreshDrafts();
            ui.toast(`Saved on ${x.label} as an unpublished draft. Check it there.`, { kind: "good" }); paint(); })) }));
        const can = x.connected && t?.status !== "published" && (!x.drafts || t?.status === "saved");
        acts.push(ui.button({ label: `Publish on ${x.label}`, icon: "send", size: "sm", variant: "danger", disabled: !can,
          onClick: async (e) => {
            const btn = e.currentTarget;
            await save(true);
            const yes = await ui.confirm({ title: `Publish on ${x.label}?`, message: `"${S.current.title}" goes live on ${x.label} now. ${WARNING}`, confirmLabel: "Publish", danger: true });
            if (!yes) return;
            await ui.busy(btn, () => run(async () => {
              S.current = await api(`/api/posts/drafts/${d.id}/destinations/${x.id}/publish`, { method: "POST", json: { confirm: true } });
              await refreshDrafts(); ui.toast(`Published on ${x.label}.`, { kind: "good" }); paint(); }));
          } }));
      }
      return el("div", { class: "dv-dest ui-stack" },
        el("div", { class: "ui-row" }, box, el("span", { class: "ui-label grow dv-wrap" }, x.label),
          t ? ui.badge(STATE_LABEL[t.status] || t.status, STATE_TONE[t.status] || "warn") : null,
          !x.connected ? ui.badge("needs setup", "warn") : null),
        t?.url ? el("p", { class: "ui-hint dv-url" }, t.url) : null,
        el("div", { class: "ui-row dv-actions" }, ...acts));
    });
    const prepBtn = ui.button({ label: "Prepare selected", icon: "layers", variant: "primary", onClick: () => ui.busy(prepBtn, () => run(async () => {
      if (!S.pick.size) { ui.toast("Tick at least one destination.", { kind: "warn" }); return; }
      await save(true);
      const r = await api(`/api/posts/drafts/${d.id}/prepare`, { method: "POST", json: { destinations: [...S.pick] } });
      S.prep = { variants: r.results.filter((x) => x.variant).map((x) => x.variant), results: r.results };
      S.current = await api(`/api/posts/drafts/${d.id}`); await refreshDrafts();
      paint();
    })) });
    const kids = [ui.eyebrow("03", "Send to destinations"),
      el("p", { class: "ui-hint" }, "Tick places and tap Prepare: each gets its own version, and API places get an unpublished draft. Publishing is a separate tap per place. Nothing publishes by itself."),
      usable.length ? el("div", { class: "ui-stack" }, ...rows) : ui.emptyState({ icon: "send", title: "No destinations switched on", body: "Add one in the Destinations tab.", compact: true }),
      el("div", { class: "ui-row dv-actions" }, prepBtn)];
    if (S.prep) kids.push(variantPanel(save));
    return el("div", { class: "ui-stack dv-sub" }, ...kids);
  }

  function copyBlock(label, text) {
    return el("div", { class: "dv-copy ui-stack" },
      el("div", { class: "ui-row" }, el("span", { class: "ui-label grow dv-wrap" }, label),
        ui.button({ label: "Copy", icon: "copy", size: "sm", onClick: () => copy(text, label) })),
      el("pre", { class: "dv-pre" }, text));
  }

  function variantPanel() {
    const kids = [];
    for (const r of (S.prep.results || []).filter((x) => !x.ok)) kids.push(ui.notice(`${r.label || r.destination}: ${r.error}`, { tone: "bad" }));
    for (const v of S.prep.variants) {
      const x = destOf(v.destination);
      const t = (S.current.targets || {})[v.destination];
      const block = [copyBlock(`${v.label}${v.style === "thread" ? " (thread)" : ""} - ${v.chars} characters`, v.style === "article" && x?.mode !== "manual" ? `# ${v.title}\n\n${v.text}` : v.text)];
      if (v.over_limit) block.push(ui.notice(`Over this place's limit (${v.max_length}). Shorten the draft or raise the limit in Destinations.`, { tone: "warn" }));
      if (x?.mode === "manual") {
        const link = el("input", { class: "ui-input", "aria-label": `Link to your ${v.label} post (optional)`, placeholder: "Link to your post (optional)", value: t?.url || "" });
        const row = [];
        if (v.open) row.push(el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm", href: v.open, target: "_blank", rel: "noopener noreferrer" }, `Open ${v.label}`));
        const done = ui.button({ label: t?.status === "posted" ? "Posted (update link)" : "I posted", icon: "check-circle", size: "sm", variant: "primary",
          onClick: () => ui.busy(done, () => run(async () => {
            S.current = await api(`/api/posts/drafts/${S.current.id}/destinations/${v.destination}/posted`, { method: "POST", json: { url: link.value } });
            await refreshDrafts(); ui.toast("Marked as posted.", { kind: "good" }); paint(); })) });
        block.push(link, el("div", { class: "ui-row dv-actions" }, ...row, done));
      }
      kids.push(el("div", { class: "dv-variant ui-stack" }, ...block));
    }
    return el("div", { class: "ui-stack" }, ui.eyebrow("04", "Per-destination text"), ui.notice("Nothing is posted for you. Copy the text, open the site, paste, then tap I posted.", { tone: "info" }), ...kids);
  }

  function polishPanel() {
    const card = el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("05", "Profile polish"));
    if (!S.polish) {
      const btn = ui.button({ label: "Show the checklist", icon: "list-checks", onClick: () => ui.busy(btn, () => run(async () => { S.polish = await api("/api/posts/polish"); paint(); })) });
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
    if (S.current) kids.push(editor());
    else kids.push(el("section", { class: "ui-stack" }, ui.eyebrow("01", "Your drafts"), draftList()));
    kids.push(polishPanel());
    return el("div", { class: "ui-stack ui-stack--lg" }, ...kids);
  }

  // ---- Published (stats and comments, from every destination that has them) ----------------------------
  async function loadPublished(refresh) {
    await run(async () => {
      S.published = refresh ? await api("/api/posts/refresh", { method: "POST" }) : await api("/api/posts/published");
      paint();
    });
  }

  function publishedTab() {
    const p = S.published;
    const readable = S.dests.filter((d) => d.enabled && d.stats && d.connected);
    const btn = ui.button({ label: "Check destinations now", icon: "refresh", variant: "primary", disabled: !readable.length,
      onClick: () => ui.busy(btn, async () => { await loadPublished(true); await api("/api/posts/comments/ack", { method: "POST" }).catch(() => {}); }) });
    const kids = [el("section", { class: "ui-card ui-hud ui-stack dv-card" }, ui.eyebrow("01", "Your published posts"),
      readable.length ? el("p", { class: "ui-hint" }, (p?.checked_at ? `Last checked ${ui.relTime(p.checked_at)}. ` : "Not checked yet. ") + `Reads ${readable.map((d) => d.label).join(", ")}.`)
        : ui.notice("Connect dev.to, Hashnode, WordPress or Ghost in Destinations to see stats.", { tone: "warn" }),
      p?.errors?.length ? ui.notice(p.errors.map((e) => `${e.label}: ${e.error}`).join(" "), { tone: "bad" }) : null,
      el("div", { class: "ui-row" }, btn))];
    const arts = p?.articles || [];
    kids.push(arts.length ? el("div", { class: "ui-list ui-list--inset" }, ...arts.map((a) => ui.listRow({
      icon: a.published ? "globe" : "pen", tone: a.published ? "good" : "warn", title: a.title,
      sub: a.published ? `${a.dest_label || ""} - ${a.views} views - ${a.reactions} reactions - ${a.comments} comments` : `${a.dest_label || ""} - draft`, chevron: false })))
      : ui.emptyState({ icon: "globe", title: "Nothing to show", body: "Check your destinations to load your posts.", compact: true }));
    const comments = p?.comments || [];
    if (comments.length) {
      kids.push(el("section", { class: "ui-stack" }, ui.eyebrow("02", "Comments"),
        ui.notice("Comments are written by other people. They are shown as plain text and never acted on.", { tone: "info" }),
        el("ul", { class: "dv-list" }, ...comments.map((c) => el("li", { class: "ui-card ui-stack dv-comment" },
          el("div", { class: "ui-row" }, c.new ? ui.badge("new", "accent") : null, el("span", { class: "dv-c__who" }, c.who || "someone"),
            el("span", { class: "ui-hint" }, `on ${c.article_title}${c.dest_label ? ` (${c.dest_label})` : ""}`)),
          el("p", { class: "dv-c__text" }, c.text))))));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...kids);
  }

  // ---- Destinations: add, edit, switch off, delete, test, reset ----------------------------------------
  const kindOf = (k) => S.kinds.find((x) => x.kind === k);

  function destTab() {
    if (S.edit) return destForm();
    const add = ui.button({ label: "Add a destination", icon: "plus", variant: "primary", onClick: () => { S.edit = { id: null, kind: "manual", label: "", enabled: true, settings: {}, format: { ...(kindOf("manual")?.format || {}) } }; paint(); } });
    const reset = ui.button({ label: "Reset to starter list", icon: "refresh", variant: "ghost", onClick: async () => {
      if (!(await ui.confirm({ title: "Reset to the starter list?", message: "Your edits to the starter places are undone and destinations you added are removed (with their saved keys). Drafts are not touched.", confirmLabel: "Reset", danger: true }))) return;
      await run(async () => { const r = await api("/api/destinations/reset", { method: "POST" }); S.dests = r.destinations; ui.toast("Starter list restored.", { kind: "good" }); paint(); });
    } });
    const rows = S.dests.map((d) => ui.listRow({ icon: d.mode === "manual" ? "copy" : d.mode === "file" ? "pen" : "globe", tone: !d.enabled ? "warn" : d.connected ? "good" : "accent",
      title: d.label, sub: `${d.kind_label} - ${!d.enabled ? "switched off" : d.connected ? "ready" : "needs setup"}`, meta: d.mode === "manual" ? "manual" : d.drafts ? "drafts" : "direct", chevron: true,
      onClick: () => { S.edit = JSON.parse(JSON.stringify(d)); paint(); } }));
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("section", { class: "ui-card ui-hud ui-stack dv-card" }, ui.eyebrow("01", "Where your posts can go"),
        el("p", { class: "ui-hint" }, "Add any site: pick a kind, or choose Manual and type a name and the composer address (LinkedIn, Medium, X, Reddit and others work this way). Keys are kept only in this laptop's keyring and are never shown again."),
        el("div", { class: "ui-row dv-actions" }, add, reset)),
      rows.length ? el("div", { class: "ui-list ui-list--inset" }, ...rows) : ui.emptyState({ icon: "send", title: "No destinations", body: "Add one, or reset to the starter list.", compact: true }),
      rulesCard());
  }

  function destForm() {
    const e = S.edit;
    const isNew = !e.id;
    const kind = kindOf(e.kind) || S.kinds[0];
    const label = el("input", { class: "ui-input", maxlength: "60", value: e.label || "", "aria-label": "Name", placeholder: "e.g. My blog, LinkedIn" });
    const kindSel = el("select", { class: "ui-input", "aria-label": "Kind", disabled: !isNew ? "" : null },
      ...S.kinds.map((k) => el("option", { value: k.kind, ...(k.kind === e.kind ? { selected: "" } : {}) }, k.label)));
    kindSel.addEventListener("change", () => { e.label = label.value; e.kind = kindSel.value; e.settings = {}; e.format = { ...(kindOf(e.kind)?.format || {}) }; paint(); });
    const inputs = {};
    const fields = kind.fields.map((f) => {
      inputs[f.key] = el("input", { class: "ui-input", value: e.settings?.[f.key] || "", "aria-label": f.label, placeholder: f.placeholder || "", autocomplete: "off", spellcheck: "false" });
      return field(f.label + (f.required ? "" : " (optional)"), inputs[f.key]);
    });
    const secret = kind.secret_label ? el("input", { class: "ui-input", type: "password", autocomplete: "off", spellcheck: "false", "aria-label": kind.secret_label,
      placeholder: e.has_secret ? "A key is saved (hidden). Paste a new one to replace it." : `Paste your ${kind.secret_label}` }) : null;
    const fmt = e.format || {};
    const style = el("select", { class: "ui-input", "aria-label": "Post style" }, ...STYLES.map(([v, l]) => el("option", { value: v, ...(v === fmt.style ? { selected: "" } : {}) }, l)));
    const num = (key, lab, min, max) => { const i = el("input", { class: "ui-input", type: "number", min: String(min), max: String(max), value: String(fmt[key] ?? 0), "aria-label": lab }); return [i, field(lab, i)]; };
    const [maxLen, maxLenF] = num("max_length", "Max length in characters (0 = no limit)", 0, 100000);
    const [tagLim, tagLimF] = num("tag_limit", "Tag limit (0 = no tags)", 0, 30);
    const [titleLen, titleLenF] = num("title_length", "Title length", 20, 300);
    let hashOn = fmt.hashtags === true;
    const hash = ui.switchControl({ checked: hashOn, label: "Add #hashtags", onChange: (v) => { hashOn = v; } });
    const tpl = el("textarea", { class: "ui-input dv-block", rows: "5", "aria-label": "Short-post template", placeholder: "{hook}\n\n{points}\n\n{cta}\n\n{hashtags}" });
    tpl.value = fmt.template || "";
    let on = e.enabled !== false;
    const enSw = ui.switchControl({ checked: on, label: "Switched on", onChange: (v) => { on = v; } });
    const body = () => {
      const settings = {};
      for (const f of kind.fields) settings[f.key] = inputs[f.key].value.trim();
      return { label: label.value, enabled: on, settings, format: { style: style.value, max_length: Number(maxLen.value), tag_limit: Number(tagLim.value),
        title_length: Number(titleLen.value), hashtags: hashOn, template: tpl.value } };
    };
    const saveBtn = ui.button({ label: isNew ? "Add" : "Save", icon: "check", variant: "primary", onClick: () => ui.busy(saveBtn, () => run(async () => {
      const b = body();
      if (secret && secret.value.trim()) b.secret = secret.value.trim();
      const saved = isNew ? await api("/api/destinations", { method: "POST", json: { kind: e.kind, ...b } }) : await api(`/api/destinations/${e.id}`, { method: "PATCH", json: b });
      S.edit = null;
      S.dests = (await api("/api/destinations")).destinations;
      ui.toast(`${saved.label} saved.`, { kind: "good" });
      paint();
    })) });
    const test = ui.button({ label: "Test connection", icon: "check-circle", disabled: isNew, onClick: () => ui.busy(test, () => run(async () => {
      const b = body();
      if (secret && secret.value.trim()) b.secret = secret.value.trim();
      await api(`/api/destinations/${e.id}`, { method: "PATCH", json: b });          // test what is on screen
      const r = await api(`/api/destinations/${e.id}/test`, { method: "POST" });
      S.dests = (await api("/api/destinations")).destinations;
      ui.toast(r.note || `Connected${r.name ? " as " + r.name : ""}.`, { kind: "good" });
    })) });
    const removeKey = secret && e.has_secret ? ui.button({ label: "Remove key", icon: "trash", variant: "ghost", onClick: async () => {
      if (!(await ui.confirm({ title: `Remove the key for ${e.label}?`, confirmLabel: "Remove", danger: true }))) return;
      await run(async () => { await api(`/api/destinations/${e.id}`, { method: "PATCH", json: { clear_secret: true } }); e.has_secret = false; paint(); });
    } }) : null;
    const del = isNew ? null : ui.button({ label: "Delete", icon: "trash", variant: "danger", onClick: async () => {
      if (!(await ui.confirm({ title: `Delete ${e.label}?`, message: "Its saved key is removed too. Drafts and posts already there are not touched.", confirmLabel: "Delete", danger: true }))) return;
      await run(async () => { await api(`/api/destinations/${e.id}`, { method: "DELETE" }); S.edit = null; S.dests = (await api("/api/destinations")).destinations; paint(); });
    } });
    const cancel = ui.button({ label: "Cancel", variant: "ghost", onClick: () => { S.edit = null; paint(); } });
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("section", { class: "ui-card ui-hud ui-stack dv-card" }, ui.eyebrow("01", isNew ? "Add a destination" : `Edit ${e.label}`),
        isNew ? field("Kind", kindSel) : el("p", { class: "ui-hint" }, kind.label),
        el("p", { class: "ui-hint" }, kind.help), field("Name", label), ...fields,
        secret ? field(kind.secret_label + (kind.secret_optional ? " (optional)" : ""), secret) : null,
        !isNew && e.mode !== "manual" ? el("p", { class: "ui-hint" }, e.connected ? "Connected: all required fields and the key are saved." : "Not ready yet: fill in the fields and save the key.") : null,
        el("div", { class: "ui-row" }, enSw, el("span", { class: "grow" }, "Switched on"))),
      el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("02", "How posts are written here"),
        el("p", { class: "ui-hint" }, "These rules shape this destination's version of every draft. The short-post template uses {title} {hook} {points} {cta} {link} {hashtags}; empty uses the built-in text."),
        field("Style", style), maxLenF, titleLenF, tagLimF, el("div", { class: "ui-row" }, hash, el("span", { class: "grow" }, "Add #hashtags")), field("Short-post template", tpl)),
      el("div", { class: "ui-row dv-actions" }, saveBtn, test, removeKey, del, cancel));
  }

  // ---- Rules and reminders (blocklist, weekly reminder, stats check) -----------------------------------
  async function loadSettings() { await run(async () => { S.settings = await api("/api/posts/settings"); paint(); }); }

  function rulesCard() {
    const s = S.settings;
    if (!s) return ui.skeleton("card", 1);
    const patch = (json) => run(async () => { S.settings = await api("/api/posts/settings", { method: "PATCH", json }); paint(); });
    const cadSw = ui.switchControl({ checked: s.devto_cadence_enabled, label: "Weekly posting reminder", onChange: (v) => patch({ devto_cadence_enabled: v }) });
    const statSw = ui.switchControl({ checked: s.devto_stats_enabled, label: "Check stats and comments", onChange: (v) => patch({ devto_stats_enabled: v }) });
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
      el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("02", "Blocklist"), ui.notice(WARNING, { tone: "warn" }),
        el("p", { class: "ui-hint" }, "Words that must never appear in a post (a company, a project, a customer). A draft with one cannot be saved to any destination, published or turned into a post."),
        block, el("div", { class: "ui-row" }, saveBlock)),
      el("section", { class: "ui-card ui-stack dv-card" }, ui.eyebrow("03", "Reminders and checks (off by default)"),
        el("div", { class: "ui-row" }, cadSw, el("span", { class: "grow" }, "Weekly posting reminder (Friday)")), field("Remind after this many days without a post", days),
        el("div", { class: "ui-row" }, statSw, el("span", { class: "grow" }, "Check stats and new comments")), field("Hours between checks", hours),
        el("p", { class: "ui-hint" }, "Reminders and checks only tell you things. They never publish anything.")));
  }

  // ---- shell -----------------------------------------------------------------------------------------
  const tabs = ui.segmented({ label: "Posts sections", value: S.tab, options: TABS, block: true, onChange: (v) => {
    S.tab = v; S.edit = null;
    if (v === "published" && !S.published) loadPublished(false);
    if (v === "destinations") loadSettings();
    paint();
  } });
  tabs.classList.add("dv-tabs");

  function paint() {
    if (S.error === "off") { root.replaceChildren(ui.emptyState({ icon: "pen", title: "Posts is off", body: "Turn it on with devto_enabled in hub.json." })); return; }
    tabs.value = S.tab;
    const view = S.tab === "ideas" ? ideasTab() : S.tab === "drafts" ? draftsTab() : S.tab === "published" ? publishedTab() : destTab();
    root.replaceChildren(...[tabs, S.error ? ui.notice(S.error, { tone: "bad" }) : null, S.tab === "ideas" ? ui.notice(WARNING, { tone: "warn" }) : null, view].filter(Boolean));
  }

  paint();
  load();
  return { show() { load(); }, refresh() { load(); } };
}
