// Flashcards and roadmaps (Learn > Cards). Decks with due counts, a full-screen review with spaced repetition,
// AI card generation, import/export, stats, and learning roadmaps. Routes: /api/cards/*, /api/roadmaps/*.
// Card text, deck names and model output are only ever shown with textContent (via el()).
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { createSourcePicker, createProviderSwitch, ensureCss, store, clip } from "./sourcepicker.js";

const COLORS = ["#6366f1", "#3b82f6", "#14b8a6", "#22c55e", "#f59e0b", "#f97316", "#ef4444", "#ec4899", "#8b5cf6", "#64748b"];
const GRADES = [
  { id: "again", label: "Again", tone: "bad", key: "1" },
  { id: "hard", label: "Hard", tone: "warn", key: "2" },
  { id: "good", label: "Good", tone: "good", key: "3" },
  { id: "easy", label: "Easy", tone: "info", key: "4" },
];
const STATE_LABEL = { new: "New", learning: "Learning", review: "Review", relearning: "Relearning" };

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const model = () => (store.get("localStorage", "hub.provider.learn", "local") === "claude" ? "claude" : "local");
const qs = (obj) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(obj)) if (v !== "" && v != null) p.set(k, v); const t = p.toString(); return t ? `?${t}` : ""; };
function dueLabel(card) {
  if (card.suspended) return "Suspended";
  if (card.state === "new") return "New";
  const d = new Date(card.due);
  if (Number.isNaN(d.getTime())) return STATE_LABEL[card.state] || "";
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(new Date().setHours(0, 0, 0, 0))) / 864e5);
  if (days <= 0) return card.state === "review" ? "Due" : "Due soon";
  return days === 1 ? "Tomorrow" : `In ${days}d`;
}
function stateTone(card) {
  if (card.suspended) return "";
  if (card.state === "new") return "accent";
  const l = dueLabel(card);
  return l.startsWith("Due") ? "warn" : "";
}

export function init(container, ctx) {
  ensureCss("/css/learn.css");
  ensureCss("/css/cards.css");
  const root = el("div", { class: "cd" });
  container.append(root);

  // ---------------------------------------------------------------- state
  let page = "home";            // home | deck | roadmap
  let tab = store.get("sessionStorage", "hub.cards.tab", "decks");
  if (!["decks", "roadmaps", "stats"].includes(tab)) tab = "decks";
  let deckId = null;
  let roadmapId = null;
  let seq = 0;                  // guards async paints against fast navigation

  const errorBlock = (error, retry) => el("div", { class: "ui-stack" }, ui.notice(error.message || String(error), { tone: "bad" }),
    retry ? ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: retry }) : null);
  const scrollTop = () => container.closest(".view")?.scrollTo?.({ top: 0 });

  function go(next, id) {
    page = next;
    if (next === "deck") deckId = id;
    if (next === "roadmap") roadmapId = id;
    paint();
    scrollTop();
  }
  function paint() {
    if (page === "deck") return renderDeck();
    if (page === "roadmap") return renderRoadmap();
    return renderHome();
  }
  function setTab(next) {
    tab = next;
    store.set("sessionStorage", "hub.cards.tab", next);
    if (page !== "home") { page = "home"; }
    renderHome();
  }

  const backBar = (title, onMenu, subtitle) => el("div", { class: "cd-back" },
    ui.button({ icon: "chevron-left", variant: "ghost", iconOnly: true, label: "Back", onClick: () => go("home") }),
    el("div", { class: "cd-back-text" }, el("h2", { class: "cd-h ui-truncate" }, title), subtitle ? el("p", { class: "cd-sub ui-truncate" }, subtitle) : null),
    onMenu ? ui.button({ icon: "more", variant: "ghost", iconOnly: true, label: "More actions", onClick: onMenu }) : null);

  // ================================================================ HOME
  let stats = null;
  async function renderHome() {
    const mine = ++seq;
    const seg = ui.segmented({ options: [{ value: "decks", label: "Decks" }, { value: "roadmaps", label: "Roadmaps" }, { value: "stats", label: "Stats" }],
      value: tab, label: "Flashcards sections", block: true, onChange: setTab });
    const body = el("div", { class: "cd-body ui-stack ui-stack--lg" }, ui.skeleton("card", 1), ui.skeleton("list", 3));
    root.replaceChildren(seg, body);
    try {
      if (tab === "decks") await paintDecks(body, mine);
      else if (tab === "roadmaps") await paintRoadmaps(body, mine);
      else await paintStats(body, mine);
    } catch (error) {
      if (mine === seq) body.replaceChildren(errorBlock(error, renderHome));
    }
  }

  async function paintDecks(body, mine) {
    const [{ decks }, st] = await Promise.all([api("/api/cards/decks"), api("/api/cards/stats").catch(() => null)]);
    if (mine !== seq) return;
    stats = st;
    const due = decks.reduce((n, d) => n + (d.due || 0), 0);
    const fresh = st ? st.new_today : decks.reduce((n, d) => n + (d.new || 0), 0);
    const hero = el("section", { class: "ui-card ui-card--gradient cd-hero" },
      el("div", { class: "cd-hero-main" },
        el("div", { class: "cd-hero-num ui-num" }, String(due)),
        el("div", { class: "cd-hero-label" }, due === 1 ? "card due today" : "cards due today"),
        el("div", { class: "cd-hero-meta" },
          ui.icon("flame"), el("span", {}, st?.streak_days ? plural(st.streak_days, "day") + " streak" : "No streak yet"),
          el("span", { class: "cd-dot" }), el("span", {}, `${fresh} new available`))),
      ui.button({ label: due || fresh ? "Start review" : "All caught up", icon: "play", variant: "secondary", size: "lg", disabled: !(due || fresh),
        onClick: () => startReview(null) }));
    const head = el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "Decks"),
      el("div", { class: "cd-actions" },
        ui.button({ label: "Import", icon: "upload", variant: "ghost", size: "sm", onClick: () => importSheet(null) }),
        ui.button({ label: "New deck", icon: "plus", variant: "tonal", size: "sm", onClick: () => deckSheet(null) })));
    let list;
    if (!decks.length) {
      list = ui.emptyState({ icon: "layers", title: "No decks yet", body: "Make a deck, then add cards by hand, import them, or let AI write them from a note.",
        action: { label: "Create a deck", icon: "plus", onClick: () => deckSheet(null) } });
    } else {
      list = el("div", { class: "ui-list ui-list--inset ui-stagger" }, decks.map((d) => {
        const dot = el("span", { class: "cd-deckdot", "aria-hidden": "true" }, ui.icon("layers"));
        dot.style.setProperty("--deck", d.color || COLORS[0]);
        return ui.listRow({ lead: dot, title: d.name, sub: `${plural(d.total, "card")}${d.new ? ` · ${d.new} new` : ""}${d.suspended ? ` · ${d.suspended} paused` : ""}`,
          meta: d.due ? ui.badge(String(d.due), "accent") : ui.badge("Done", "good"), chevron: true, onClick: () => go("deck", d.id) });
      }));
      setTimeout(() => list.classList.remove("ui-stagger"), 1000);
    }
    body.replaceChildren(hero, el("div", { class: "ui-stack" }, head, list));
  }

  // ================================================================ STATS
  async function paintStats(body, mine) {
    const [st, settings] = await Promise.all([api("/api/cards/stats"), api("/api/cards/settings").catch(() => null)]);
    if (mine !== seq) return;
    const ret = st.retention_30d;
    const ring = ui.ringProgress({ value: ret == null ? 0 : ret / 100, size: 112, stroke: 10, tone: ret == null ? undefined : ret >= 85 ? "good" : ret >= 70 ? "warn" : "bad",
      big: ret == null ? "–" : `${Math.round(ret)}%`, small: "retention", label: "Retention over 30 days" });
    const tile = (icon, tone, big, small) => el("div", { class: "cd-stat" }, ui.tile(icon, tone, "sm"), el("div", { class: "cd-stat-num ui-num" }, big), el("div", { class: "cd-stat-label" }, small));
    const top = el("section", { class: "ui-card cd-statcard" }, ring,
      el("div", { class: "cd-statgrid" },
        tile("flame", "orange", String(st.streak_days || 0), "day streak"),
        tile("check-circle", "good", String(st.reviewed_today || 0), "reviewed today"),
        tile("clock", "accent", String(st.due_today || 0), "due today"),
        tile("sparkles", "violet", String(st.new_today || 0), "new available")));
    const max = Math.max(1, ...st.forecast.map((f) => f.due));
    const bars = el("div", { class: "cd-forecast", role: "img", "aria-label": `Cards due over the next 14 days: ${st.forecast.map((f) => f.due).join(", ")}` },
      st.forecast.map((f, i) => {
        const day = new Date(`${f.date}T00:00:00`);
        const bar = el("i", { class: "cd-bar" });
        bar.style.setProperty("--h", String(f.due / max));
        return el("div", { class: `cd-barcol${i === 0 ? " is-today" : ""}` },
          el("span", { class: "cd-barval ui-num" }, f.due ? String(f.due) : ""), el("div", { class: "cd-bartrack" }, bar),
          el("span", { class: "cd-barday" }, i === 0 ? "Today" : day.toLocaleDateString(undefined, { weekday: "narrow" })));
      }));
    const forecast = el("section", { class: "ui-card ui-stack" }, el("div", { class: "ui-card__head" }, el("h3", { class: "ui-card__title" }, "Next 14 days"),
      el("span", { class: "ui-card__sub" }, "Reviews due")), bars);

    const decks = el("div", { class: "ui-list ui-list--inset" }, (st.by_deck || []).map((d) => {
      const dot = el("span", { class: "cd-deckdot", "aria-hidden": "true" }, ui.icon("layers"));
      dot.style.setProperty("--deck", d.color || COLORS[0]);
      return ui.listRow({ lead: dot, title: d.name, sub: plural(d.total, "card"), meta: ui.badge(`${d.due || 0} due`, d.due ? "accent" : ""), onClick: () => go("deck", d.deck_id || d.id) });
    }));

    const children = [top, forecast];
    if (settings) {
      const val = el("output", { class: "cd-stepval ui-num" }, String(settings.new_per_day));
      let n = settings.new_per_day;
      const set = async (next) => {
        next = Math.max(0, Math.min(200, next));
        try { await api("/api/cards/settings", { method: "PUT", json: { new_per_day: next } }); n = next; val.textContent = String(n); }
        catch (error) { ctx.toast(error.message, { kind: "bad" }); }
      };
      children.push(el("section", { class: "ui-card cd-stepper" },
        el("div", {}, el("div", { class: "ui-card__title" }, "New cards per day"), el("div", { class: "ui-card__sub" }, "How many unseen cards join each day's review")),
        el("div", { class: "cd-step" }, ui.button({ icon: "minus", variant: "tonal", iconOnly: true, label: "Fewer new cards", onClick: () => set(n - 5) }), val,
          ui.button({ icon: "plus", variant: "tonal", iconOnly: true, label: "More new cards", onClick: () => set(n + 5) }))));
    }
    if ((st.by_deck || []).length) children.push(el("div", { class: "ui-stack" }, el("h3", { class: "ui-section-title" }, "By deck"), decks));
    body.replaceChildren(...children);
  }

  // ================================================================ DECK DETAIL
  const PAGE = 50;
  async function renderDeck() {
    const mine = ++seq;
    root.replaceChildren(backBar("Deck"), ui.skeleton("card", 1), ui.skeleton("list", 4));
    let deck;
    try { deck = await api(`/api/cards/decks/${encodeURIComponent(deckId)}`); }
    catch (error) { if (mine === seq) root.replaceChildren(backBar("Deck"), errorBlock(error, renderDeck)); return; }
    if (mine !== seq) return;
    const dot = el("span", { class: "cd-deckdot cd-deckdot--lg", "aria-hidden": "true" }, ui.icon("layers"));
    dot.style.setProperty("--deck", deck.color || COLORS[0]);
    const count = (n, label) => el("div", { class: "cd-count" }, el("b", { class: "ui-num" }, String(n)), el("span", {}, label));
    const hero = el("section", { class: "ui-card cd-deckhero ui-stack" },
      el("div", { class: "cd-deckhead" }, dot, el("div", { class: "grow" },
        deck.description ? el("p", { class: "cd-sub" }, deck.description) : el("p", { class: "cd-sub" }, "Tap a card below to edit it."))),
      el("div", { class: "cd-counts" }, count(deck.total, "cards"), count(deck.new, "new"), count(deck.due, "due"), count(deck.suspended, "paused")),
      ui.button({ label: deck.due || deck.new ? "Review this deck" : "Nothing due", icon: "play", variant: "primary", size: "lg", block: true, disabled: !(deck.due || deck.new),
        onClick: () => startReview(deck.id) }),
      el("div", { class: "cd-row2" },
        ui.button({ label: "Generate with AI", icon: "sparkles", variant: "tonal", onClick: () => generateSheet(deck) }),
        ui.button({ label: "Add card", icon: "plus", variant: "tonal", onClick: () => cardSheet(deck, null, refreshList) })));

    const search = el("input", { type: "search", placeholder: "Search this deck", "aria-label": "Search cards", enterkeyhint: "search", autocomplete: "off" });
    const listHost = el("div", { class: "ui-stack" });
    let q = "";
    let loaded = [];
    let total = 0;
    let timer = null;
    let listSeq = 0;
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { q = search.value.trim(); refreshList(); }, 250); });

    async function refreshList() { loaded = []; await more(true); }
    async function more(first) {
      const my = ++listSeq;
      if (first) listHost.replaceChildren(ui.skeleton("list", 3));
      try {
        const data = await api(`/api/cards/cards${qs({ deck: deck.id, q, limit: PAGE, offset: loaded.length })}`);
        if (my !== listSeq || mine !== seq) return;
        total = data.total;
        loaded = loaded.concat(data.cards);
        paintList();
      } catch (error) { if (my === listSeq) listHost.replaceChildren(errorBlock(error, () => more(first))); }
    }
    function paintList() {
      if (!loaded.length) {
        listHost.replaceChildren(q ? ui.emptyState({ icon: "search", compact: true, title: "No cards match", body: "Try another word." })
          : ui.emptyState({ icon: "layers", title: "This deck is empty", body: "Add a card, import a list, or have AI write some from your notes.",
            action: { label: "Add a card", icon: "plus", onClick: () => cardSheet(deck, null, refreshList) } }));
        return;
      }
      const rows = loaded.map((c) => ui.listRow({
        title: el("span", { class: "cd-cardfront ui-clamp-2" }, c.front_render || c.front),
        sub: el("span", { class: "ui-truncate" }, c.back_render || c.back),
        meta: ui.badge(dueLabel(c), stateTone(c)), onClick: () => cardSheet(deck, c, refreshList),
      }));
      listHost.replaceChildren(
        el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, q ? `${plural(total, "match", "matches")}` : "Cards")),
        el("div", { class: "ui-list ui-list--inset" }, rows),
        loaded.length < total ? ui.button({ label: `Show more (${total - loaded.length} left)`, variant: "tonal", block: true, onClick: () => more(false) }) : null);
    }

    root.replaceChildren(backBar(deck.name, () => deckMenu(deck), "Deck"), hero, el("div", { class: "ui-field" }, search), listHost);
    more(true);
  }

  function deckMenu(deck) {
    const s = ui.sheet({ title: deck.name, content: el("div", { class: "ui-list ui-list--inset ui-list--icons" }, [
      ui.listRow({ icon: "edit", title: "Rename or recolour", onClick: () => { s.close(); deckSheet(deck); } }),
      ui.listRow({ icon: "upload", tone: "teal", title: "Import cards", sub: "Paste tab-separated or CSV text", onClick: () => { s.close(); importSheet(deck); } }),
      ui.listRow({ icon: "download", tone: "info", title: "Export deck", sub: "Download as TSV, CSV or JSON", onClick: () => { s.close(); exportSheet(deck); } }),
      ui.listRow({ icon: "trash", tone: "bad", title: "Delete deck", sub: `Also deletes its ${plural(deck.total, "card")}`, onClick: async () => {
        s.close();
        if (!(await ui.confirm({ title: `Delete "${deck.name}"?`, message: `This also deletes ${plural(deck.total, "card")} and cannot be undone.`, confirmLabel: "Delete deck", danger: true }))) return;
        try { await api(`/api/cards/decks/${encodeURIComponent(deck.id)}`, { method: "DELETE", json: { confirm: true } }); ctx.toast("Deck deleted", { kind: "good" }); go("home"); }
        catch (error) { ctx.toast(error.message, { kind: "bad" }); }
      } }),
    ]) });
  }

  function exportSheet(deck) {
    const row = (fmt, title, sub) => ui.listRow({ icon: "download", tone: "info", title, sub, onClick: () => {
      const a = el("a", { href: `/api/cards/decks/${encodeURIComponent(deck.id)}/export?format=${fmt}`, download: "" });
      document.body.append(a); a.click(); a.remove();
      s.close();
    } });
    const s = ui.sheet({ title: "Export deck", content: el("div", { class: "ui-list ui-list--inset ui-list--icons" }, [
      row("tsv", "Tab-separated (TSV)", "Front, back, tags. Works with Anki."), row("csv", "CSV", "Opens in spreadsheets."), row("json", "JSON", "Everything, including review history."),
    ]) });
  }

  // ---------------------------------------------------------------- deck new / edit sheet
  function deckSheet(deck) {
    let color = deck?.color || COLORS[0];
    const name = el("input", { type: "text", maxlength: "80", placeholder: "For example: Spanish verbs", "aria-label": "Deck name", value: deck?.name || "" });
    const desc = el("input", { type: "text", maxlength: "300", placeholder: "Optional", "aria-label": "Description", value: deck?.description || "" });
    const swatches = el("div", { class: "cd-swatches", role: "radiogroup", "aria-label": "Colour" });
    const paintSw = () => { for (const b of swatches.children) b.setAttribute("aria-checked", String(b.dataset.c === color)); };
    for (const c of COLORS) {
      const b = el("button", { type: "button", class: "cd-swatch", role: "radio", "data-c": c, "aria-label": `Colour ${c}`, onclick: () => { color = c; paintSw(); } }, ui.icon("check"));
      b.style.setProperty("--deck", c);
      swatches.append(b);
    }
    paintSw();
    const err = el("div", {});
    const s = ui.sheet({ title: deck ? "Edit deck" : "New deck", content: el("div", { class: "ui-stack" },
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Name"), name),
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Description"), desc),
      el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Colour"), swatches), err),
    actions: [{ label: deck ? "Save" : "Create deck", variant: "primary", async onClick() {
      if (!name.value.trim()) { err.replaceChildren(ui.notice("Give the deck a name.", { tone: "warn" })); return false; }
      try {
        const body = { name: name.value.trim(), description: desc.value.trim(), color };
        const saved = deck ? await api(`/api/cards/decks/${encodeURIComponent(deck.id)}`, { method: "PATCH", json: body })
          : await api("/api/cards/decks", { method: "POST", json: body });
        ctx.toast(deck ? "Deck saved" : "Deck created", { kind: "good" });
        if (deck) paint(); else go("deck", saved.id);
      } catch (error) { err.replaceChildren(ui.notice(error.message, { tone: "bad" })); return false; }
    } }] });
    return s;
  }

  // ---------------------------------------------------------------- card add / edit sheet
  function cardSheet(deck, card, onSaved) {
    const front = el("textarea", { rows: "3", maxlength: "2000", placeholder: "Question, or a sentence with {{c1::a hidden part}}", "aria-label": "Front" });
    const back = el("textarea", { rows: "3", maxlength: "4000", placeholder: "Answer (optional for cloze cards)", "aria-label": "Back" });
    const tags = el("input", { type: "text", placeholder: "tags, separated by spaces", "aria-label": "Tags", autocapitalize: "off", autocomplete: "off" });
    front.value = card?.front || ""; back.value = card?.back || ""; tags.value = (card?.tags || []).join(" ");
    const err = el("div", {});
    let suspended = !!card?.suspended;
    const parts = [
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Front"), front,
        el("p", { class: "ui-hint" }, "Cloze: write {{c1::answer}} or {{c1::answer::hint}} to hide part of a sentence.")),
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Back"), back),
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Tags"), tags),
    ];
    if (card) {
      const sw = ui.switchControl({ checked: suspended, label: "Pause this card", onChange(v) { suspended = v; } });
      parts.push(el("div", { class: "cd-switchrow" }, el("div", {}, el("div", { class: "ui-row__title" }, "Pause this card"), el("div", { class: "ui-row__sub" }, "Paused cards never appear in reviews")), sw));
      parts.push(el("div", { class: "cd-row2" },
        ui.button({ label: "Explain", icon: "sparkles", variant: "tonal", onClick: (e) => explainCard(card, e.currentTarget) }),
        ui.button({ label: "Delete", icon: "trash", variant: "danger-soft", onClick: async () => {
          if (!(await ui.confirm({ title: "Delete this card?", message: clip(card.front_render || card.front, 120), confirmLabel: "Delete card", danger: true }))) return;
          try { await api(`/api/cards/cards/${encodeURIComponent(card.id)}`, { method: "DELETE", json: { confirm: true } }); s.close(); ctx.toast("Card deleted", { kind: "good" }); onSaved?.(); }
          catch (error) { err.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
        } })));
    }
    parts.push(err);
    async function save() {
      const f = front.value.trim();
      if (!f) { err.replaceChildren(ui.notice("The front can't be empty.", { tone: "warn" })); return false; }
      const body = { front: f, back: back.value.trim(), tags: tags.value.split(/[\s,]+/).filter(Boolean) };
      try {
        if (card) await api(`/api/cards/cards/${encodeURIComponent(card.id)}`, { method: "PATCH", json: { ...body, suspended } });
        else await api("/api/cards/cards", { method: "POST", json: { ...body, deck_id: deck.id } });
        ui.haptic("success");
        onSaved?.();
        return true;
      } catch (error) { err.replaceChildren(ui.notice(error.message, { tone: "bad" })); return false; }
    }
    const s = ui.sheet({ title: card ? "Edit card" : "New card", content: el("div", { class: "ui-stack" }, ...parts),
      actions: [
        ...(card ? [] : [{ label: "Save and add another", variant: "tonal", async onClick() {
          if (await save()) { front.value = ""; back.value = ""; front.focus(); ctx.toast("Card added", { kind: "good" }); }
          return false;
        } }]),
        { label: card ? "Save" : "Add card", variant: "primary", async onClick() { const ok = await save(); if (ok) ctx.toast(card ? "Card saved" : "Card added", { kind: "good" }); return ok ? undefined : false; } },
      ] });
    return s;
  }

  async function explainCard(card, button) {
    const run = async () => {
      const data = await api(`/api/cards/cards/${encodeURIComponent(card.id)}/explain`, { method: "POST", json: { model: model() } });
      const s = ui.sheet({ title: "Explanation", content: el("div", { class: "ui-stack" },
        el("div", { class: "cd-explain-card" }, el("strong", {}, card.front_render || card.front), el("span", {}, card.back_render || card.back)),
        el("p", { class: "cd-explain" }, data.explanation || "No explanation came back.")) });
      return s;
    };
    try { await (button ? ui.busy(button, run()) : run()); }
    catch (error) { ctx.toast(error.message, { kind: "bad" }); }
  }

  // ---------------------------------------------------------------- AI generate sheet
  function generateSheet(deck) {
    const picker = createSourcePicker(null, ctx, { type: "note" });
    const providerHost = el("div", {});
    const provider = createProviderSwitch(providerHost, "Model for generating cards");
    let count = 10;
    let style = "qa";
    const countSeg = ui.segmented({ options: [5, 10, 20, 30].map((n) => ({ value: n, label: String(n) })), value: count, label: "How many cards", block: true, onChange(v) { count = Number(v); } });
    const styleSeg = ui.segmented({ options: [{ value: "qa", label: "Q and A" }, { value: "cloze", label: "Cloze" }, { value: "definition", label: "Definitions" }], value: style, label: "Card style", block: true, onChange(v) { style = v; } });
    const setup = el("div", { class: "ui-stack" }, el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Make cards from"), picker.root),
      el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "How many"), countSeg),
      el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Style"), styleSeg),
      el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Model"), providerHost));
    let controller = null;
    const s = ui.sheet({ title: "Generate cards", content: setup, class: "cd-gensheet", onClose: () => controller?.abort() });
    s.setActions([{ label: "Generate", icon: "sparkles", variant: "primary", onClick: () => { run(); return false; } }]);

    function showSetup(errorText) {
      s.setTitle("Generate cards");
      s.body.replaceChildren(errorText ? el("div", {}, ui.notice(errorText, { tone: "bad" })) : null, setup);
      s.setActions([{ label: "Generate", icon: "sparkles", variant: "primary", onClick: () => { run(); return false; } }]);
    }
    async function run() {
      const source = picker.getSource();
      if (!source) { ctx.toast("Pick a source first.", { kind: "warn" }); return; }
      controller = new AbortController();
      const mineCtl = controller;
      s.setTitle("Writing cards…");
      s.body.replaceChildren(el("div", { class: "cd-gen-wait ui-stack", role: "status" }, el("div", { class: "ui-spinner" }),
        el("p", { class: "cd-sub" }, `From ${picker.label() || "your source"}. A local model can take a minute or two.`)));
      s.setActions([{ label: "Cancel", variant: "secondary", onClick: () => { mineCtl.abort(); showSetup(); return false; } }]);
      try {
        const data = await api(`/api/cards/decks/${encodeURIComponent(deck.id)}/generate`, { method: "POST", signal: mineCtl.signal,
          json: { source, count, style, model: provider.get() } });
        if (mineCtl.signal.aborted) return;
        proposals(data);
      } catch (error) {
        if (error.name === "AbortError") return;
        showSetup(error.message);
      }
    }
    function proposals(data) {
      const cards = data.cards || [];
      if (!cards.length) { showSetup("The model didn't write any new cards. Try a longer source or another style."); return; }
      const picked = new Set(cards.map((_, i) => i));
      const rows = cards.map((c, i) => {
        const box = el("input", { type: "checkbox", checked: true, "aria-label": `Include card ${i + 1}` });
        box.addEventListener("change", () => { box.checked ? picked.add(i) : picked.delete(i); sync(); });
        return el("label", { class: "cd-prop" }, box, el("span", { class: "cd-prop-body" },
          el("span", { class: "cd-prop-front" }, c.front_render || c.front), el("span", { class: "cd-prop-back" }, c.back_render || c.back),
          c.is_cloze ? ui.badge("Cloze", "accent") : null));
      });
      const all = ui.button({ label: "Select none", variant: "ghost", size: "sm", onClick: () => {
        const none = picked.size > 0;
        picked.clear(); if (!none) cards.forEach((_, i) => picked.add(i));
        rows.forEach((r, i) => { r.querySelector("input").checked = picked.has(i); }); sync();
      } });
      const info = el("span", { class: "cd-sub" });
      function sync() {
        info.textContent = `${picked.size} of ${cards.length} selected${data.skipped_duplicates ? ` · ${data.skipped_duplicates} duplicates skipped` : ""}`;
        all.lastChild.textContent = picked.size ? "Select none" : "Select all";
        s.setActions([
          { label: "Back", variant: "secondary", onClick: () => { showSetup(); return false; } },
          { label: picked.size ? `Add ${picked.size}` : "Add", variant: "primary", icon: "plus", async onClick() {
            if (!picked.size) { ctx.toast("Tick at least one card.", { kind: "warn" }); return false; }
            try {
              const res = await api(`/api/cards/decks/${encodeURIComponent(deck.id)}/cards/bulk`, { method: "POST",
                json: { cards: [...picked].sort((a, b) => a - b).map((i) => ({ front: cards[i].front, back: cards[i].back, tags: cards[i].tags || [] })) } });
              ctx.toast(`Added ${plural(res.added, "card")}${res.skipped_duplicates ? `, ${res.skipped_duplicates} duplicates skipped` : ""}`, { kind: "good" });
              paint();
            } catch (error) { ctx.toast(error.message, { kind: "bad" }); return false; }
          } },
        ]);
      }
      s.setTitle(data.title || "Review the cards");
      s.body.replaceChildren(el("div", { class: "ui-stack" }, el("div", { class: "cd-propbar" }, info, all), el("div", { class: "cd-proplist" }, rows)));
      sync();
    }
  }

  // ---------------------------------------------------------------- import
  async function importSheet(deck) {
    let decks = [];
    try { decks = (await api("/api/cards/decks")).decks || []; } catch (_) { /* a new deck still works */ }
    const text = el("textarea", { rows: "7", placeholder: "front<TAB>back<TAB>tags, one card per line (Anki plain-text export works)", "aria-label": "Cards to import", spellcheck: "false" });
    const pick = el("input", { type: "file", accept: ".txt,.tsv,.csv,text/plain,text/csv", hidden: true });
    pick.addEventListener("change", async () => { const f = pick.files?.[0]; if (f) { text.value = await f.text(); text.dispatchEvent(new Event("input")); } });
    const select = el("select", { "aria-label": "Import into" }, decks.map((d) => el("option", { value: d.id }, d.name)), el("option", { value: "" }, "A new deck…"));
    select.value = deck?.id || decks[0]?.id || "";
    const newName = el("input", { type: "text", maxlength: "80", placeholder: "New deck name", "aria-label": "New deck name" });
    const syncName = () => { newName.hidden = select.value !== ""; };
    select.addEventListener("change", syncName); syncName();
    const result = el("div", {});
    text.addEventListener("input", () => result.replaceChildren());
    const body = (dry) => ({ text: text.value, ...(select.value ? { deck_id: select.value } : { deck_name: newName.value.trim() }), format: "auto", dry_run: dry });
    const check = () => {
      if (!text.value.trim()) { result.replaceChildren(ui.notice("Paste some cards first.", { tone: "warn" })); return false; }
      if (!select.value && !newName.value.trim()) { result.replaceChildren(ui.notice("Name the new deck.", { tone: "warn" })); return false; }
      return true;
    };
    const s = ui.sheet({ title: "Import cards", content: el("div", { class: "ui-stack" },
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Cards"), text,
        el("p", { class: "ui-hint" }, "One per line: front, a tab, back, and optional tags. CSV works too.")),
      ui.button({ label: "Choose a file", icon: "file", variant: "tonal", size: "sm", onClick: () => pick.click() }), pick,
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "Into deck"), select, newName), result),
    actions: [
      { label: "Preview", variant: "secondary", async onClick() {
        if (!check()) return false;
        try {
          const r = await api("/api/cards/import", { method: "POST", json: body(true) });
          result.replaceChildren(ui.notice(`Would add ${plural(r.will_add, "card")}. Skipped ${r.skipped_duplicates} duplicates and ${r.skipped_invalid} invalid rows.`, { tone: r.will_add ? "good" : "warn" }));
        } catch (error) { result.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
        return false;
      } },
      { label: "Import", variant: "primary", icon: "upload", async onClick() {
        if (!check()) return false;
        try {
          const r = await api("/api/cards/import", { method: "POST", json: body(false) });
          ctx.toast(`Imported ${plural(r.added, "card")}${r.skipped_duplicates ? `, ${r.skipped_duplicates} duplicates skipped` : ""}`, { kind: "good" });
          if (r.deck_id) go("deck", r.deck_id); else paint();
        } catch (error) { result.replaceChildren(ui.notice(error.message, { tone: "bad" })); return false; }
      } },
    ] });
  }

  // ================================================================ REVIEW (full screen)
  async function startReview(deckFilter) {
    let data;
    try { data = await api(`/api/cards/due${qs({ deck: deckFilter, limit: 200 })}`); }
    catch (error) { ctx.toast(error.message, { kind: "bad" }); return; }
    reviewScreen(data, deckFilter);
  }

  function reviewScreen(first, deckFilter) {
    let queue = first.cards || [];
    let pos = 0;
    let flipped = false;
    let busy = false;
    let nextDue = first.next_due || null;
    const history = [];                       // [{card, pos, grade}]
    const tally = { again: 0, hard: 0, good: 0, easy: 0 };
    const started = Date.now();
    let undoAvailable = false;
    let closed = false;

    const overlay = el("div", { class: "cd-review", role: "dialog", "aria-modal": "true", "aria-label": "Review flashcards" });
    const counter = el("div", { class: "cd-rv-count ui-num", "aria-live": "polite" });
    const progress = ui.progressBar(0, "Review progress");
    const undoBtn = ui.button({ icon: "history", variant: "ghost", iconOnly: true, label: "Undo last answer", disabled: true, onClick: () => undo() });
    const closeBtn = ui.button({ icon: "close", variant: "ghost", iconOnly: true, label: "Close review", onClick: () => close() });
    const stage = el("div", { class: "cd-rv-stage" });
    const dock = el("div", { class: "cd-rv-dock" });
    overlay.append(el("div", { class: "cd-rv-top" }, closeBtn, counter, undoBtn), progress, stage, dock);
    document.body.append(overlay);
    document.body.classList.add("cd-noscroll");
    const release = ui.pushLayer(() => close(true));
    requestAnimationFrame(() => overlay.classList.add("is-in"));

    function close(fromHistory) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey, true);
      if (!fromHistory) release();
      document.body.classList.remove("cd-noscroll");
      overlay.classList.remove("is-in");
      setTimeout(() => overlay.remove(), 220);
      paint();
    }
    function onKey(e) {
      if (e.target.closest?.("input, textarea, select") || document.querySelector(".ui-sheet")) return;
      if (e.key === "Escape") { e.preventDefault(); close(); return; }
      if (pos >= queue.length) return;
      if (!flipped && (e.key === " " || e.key === "Enter")) { e.preventDefault(); flip(); }
      else if (flipped) { const g = GRADES.find((x) => x.key === e.key); if (g) { e.preventDefault(); grade(g.id); } }
      if ((e.key === "z" || e.key === "Z") && undoAvailable) undo();
    }
    document.addEventListener("keydown", onKey, true);

    function paintTop() {
      const total = queue.length;
      counter.textContent = pos < total ? `${pos + 1} of ${total}` : "Done";
      progress.set(total ? Math.min(1, pos / total) : 1);
      undoBtn.disabled = !undoAvailable || busy;
    }

    function cardView(card) {
      const faceHead = (text) => el("div", { class: "cd-face-kind" }, text);
      const meta = el("div", { class: "cd-face-meta" }, ui.badge(STATE_LABEL[card.state] || "Card", card.state === "new" ? "accent" : ""),
        ...(card.tags || []).slice(0, 3).map((t) => el("span", { class: "cd-tag" }, `#${t}`)));
      const front = el("div", { class: "cd-face cd-face--front" }, faceHead(card.is_cloze ? "Fill the gap" : "Question"), el("div", { class: "cd-face-text" }, card.front_render || card.front), meta,
        el("div", { class: "cd-face-hint" }, "Tap to show the answer"));
      const explain = ui.button({ label: "Explain", icon: "sparkles", variant: "tonal", size: "sm", onClick: (e) => { e.stopPropagation(); explainCard(card, e.currentTarget); } });
      const back = el("div", { class: "cd-face cd-face--back" }, faceHead("Answer"), el("div", { class: "cd-face-text" }, card.back_render || card.back || card.front_render), explain);
      const inner = el("div", { class: "cd-flip-inner" }, front, back);
      const flip = el("div", { class: "cd-flip", role: "button", tabindex: "0", "aria-label": "Flip card", onclick: () => flipNow() }, inner);
      flip.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flipNow(); } });
      return flip;
    }
    const flipNow = () => { if (!flipped) flip(); };

    function show() {
      flipped = false;
      paintTop();
      if (pos >= queue.length) return summary();
      const card = queue[pos];
      const view = cardView(card);
      stage.replaceChildren(view);
      view.classList.add("is-enter");
      dock.replaceChildren(ui.button({ label: "Show answer", icon: "eye", variant: "primary", size: "lg", block: true, onClick: () => flip() }));
    }
    function flip() {
      if (flipped || pos >= queue.length) return;
      flipped = true;
      stage.firstChild?.classList.add("is-flipped");
      stage.firstChild?.setAttribute("aria-label", "Answer shown");
      const iv = queue[pos].intervals || {};
      dock.replaceChildren(el("div", { class: "cd-grades", role: "group", "aria-label": "How well did you know it?" }, GRADES.map((g) =>
        el("button", { type: "button", class: "cd-grade", "data-tone": g.tone, "data-grade": g.id, onclick: () => grade(g.id) },
          el("span", { class: "cd-grade-label" }, g.label), el("span", { class: "cd-grade-iv ui-num" }, iv[g.id] || "")))));
      ui.haptic("select");
    }
    async function grade(g) {
      if (busy || pos >= queue.length) return;
      busy = true;
      const card = queue[pos];
      dock.classList.add("is-busy");
      try {
        const res = await api("/api/cards/review", { method: "POST", json: { card_id: card.id, grade: g } });
        history.push({ card, pos, grade: g });
        tally[g] += 1;
        undoAvailable = res.undo_available !== false;
        pos += 1;
        ui.haptic(g === "again" ? "warn" : "light");
        if (res.next_label) flash(`Next: ${res.next_label}`);
      } catch (error) {
        ctx.toast(error.message, { kind: "bad" });
      }
      busy = false;
      dock.classList.remove("is-busy");
      show();
    }
    async function undo() {
      if (busy || !undoAvailable) return;
      busy = true;
      paintTop();
      try {
        const res = await api("/api/cards/review/undo", { method: "POST" });
        const last = history.pop();
        if (last) { pos = last.pos; queue[pos] = { ...last.card, ...res.card, intervals: last.card.intervals }; tally[last.grade] -= 1; }
        undoAvailable = !!res.undo_available && history.length > 0;
        flash("Undone");
      } catch (error) { ctx.toast(error.message, { kind: "bad" }); undoAvailable = false; }
      busy = false;
      show();
    }
    function flash(text) {
      const n = el("div", { class: "cd-flash", role: "status" }, text);
      overlay.append(n);
      setTimeout(() => n.remove(), 1100);
    }

    async function summary() {
      const total = Object.values(tally).reduce((a, b) => a + b, 0);
      const good = tally.good + tally.easy;
      const mins = Math.max(1, Math.round((Date.now() - started) / 60000));
      const ring = ui.ringProgress({ value: total ? good / total : 1, size: 132, stroke: 11, tone: !total || good / total >= .7 ? "good" : "warn",
        big: total ? `${Math.round(good / total * 100)}%` : "✓", small: total ? "remembered" : "", label: "Session accuracy" });
      progress.set(1);
      undoBtn.disabled = !undoAvailable;
      const bars = GRADES.map((g) => {
        const fill = el("i"); fill.style.setProperty("--v", String(total ? tally[g.id] / total : 0));
        return el("div", { class: "cd-tallyrow", "data-tone": g.tone }, el("span", {}, g.label), el("div", { class: "cd-tallybar" }, fill), el("span", { class: "ui-num" }, String(tally[g.id])));
      });
      const again = el("div", { class: "cd-sum-next" });
      stage.replaceChildren(el("div", { class: "cd-summary ui-stack ui-stack--lg" }, ring,
        el("h2", { class: "cd-h cd-center" }, total ? "Session complete" : "You're all caught up"),
        el("p", { class: "cd-sub cd-center" }, total ? `${plural(total, "card")} in about ${plural(mins, "minute")}` : "Nothing is due right now."),
        total ? el("div", { class: "cd-tally" }, bars) : null, again));
      dock.replaceChildren(el("div", { class: "cd-row2" },
        ui.button({ label: "Done", variant: "secondary", size: "lg", onClick: () => close() }),
        ui.button({ label: "Review more", icon: "refresh", variant: "primary", size: "lg", onClick: async (e) => {
          try {
            const data = await ui.busy(e.currentTarget, api(`/api/cards/due${qs({ deck: deckFilter, limit: 200 })}`));
            if (!(data.cards || []).length) { ctx.toast("Nothing else is due right now.", { kind: "info" }); nextDue = data.next_due || null; paintNext(again); return; }
            queue = data.cards; pos = 0; history.length = 0; undoAvailable = false; show();
          } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
        } })));
      paintNext(again);
      if (total) ui.haptic("success");
    }
    function paintNext(host) {
      if (!nextDue) return;
      const d = new Date(nextDue);
      if (!Number.isNaN(d.getTime())) host.replaceChildren(ui.notice(`Next card is due at ${ui.clockTime(d)}.`, { tone: "" }));
    }
    show();
  }

  // ================================================================ ROADMAPS
  async function paintRoadmaps(body, mine) {
    const { roadmaps } = await api("/api/roadmaps");
    if (mine !== seq) return;
    const head = el("div", { class: "ui-section-head" }, el("h3", { class: "ui-section-title" }, "Your roadmaps"),
      ui.button({ label: "New roadmap", icon: "plus", variant: "tonal", size: "sm", onClick: () => roadmapSheet() }));
    if (!roadmaps.length) {
      body.replaceChildren(ui.emptyState({ icon: "target", title: "No roadmaps yet", body: "Tell the model what you want to learn and how many weeks you have. You get a week-by-week plan with tasks to tick off.",
        action: { label: "Make a roadmap", icon: "sparkles", onClick: () => roadmapSheet() } }));
      return;
    }
    const rows = roadmaps.map((r) => {
      const ring = ui.ringProgress({ value: (r.progress?.percent || 0) / 100, size: 44, stroke: 5, tone: r.progress?.percent >= 100 ? "good" : undefined, label: `${r.title} progress` });
      return ui.listRow({ lead: ring, title: r.title, sub: `${r.level || ""} · ${plural(r.weeks, "week")} · ${r.progress?.done ?? 0}/${r.progress?.total ?? 0} tasks`,
        meta: ui.badge(`${r.progress?.percent ?? 0}%`, r.progress?.percent >= 100 ? "good" : "accent"), chevron: true, onClick: () => go("roadmap", r.id) });
    });
    body.replaceChildren(el("div", { class: "ui-stack" }, head, el("div", { class: "ui-list ui-list--inset" }, rows)));
  }

  function roadmapSheet() {
    const topic = el("input", { type: "text", maxlength: "200", placeholder: "For example: Learn SQL for data analysis", "aria-label": "Topic" });
    let weeks = 4, hours = 5, level = "beginner";
    const stepper = (label, get, set, min, max, step) => {
      const val = el("output", { class: "cd-stepval ui-num" }, String(get()));
      const bump = (d) => { set(Math.max(min, Math.min(max, get() + d))); val.textContent = String(get()); };
      return el("div", { class: "cd-stepper-row" }, el("div", { class: "ui-label" }, label), el("div", { class: "cd-step" },
        ui.button({ icon: "minus", variant: "tonal", iconOnly: true, label: `Less ${label}`, onClick: () => bump(-step) }), val,
        ui.button({ icon: "plus", variant: "tonal", iconOnly: true, label: `More ${label}`, onClick: () => bump(step) })));
    };
    const levelSeg = ui.segmented({ options: [{ value: "beginner", label: "Beginner" }, { value: "intermediate", label: "Some" }, { value: "advanced", label: "Advanced" }], value: level, label: "Your level", block: true, onChange(v) { level = v; } });
    const providerHost = el("div", {});
    const provider = createProviderSwitch(providerHost, "Model for the roadmap");
    const err = el("div", {});
    const form = el("div", { class: "ui-stack" }, el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "What do you want to learn?"), topic),
      stepper("Weeks", () => weeks, (v) => { weeks = v; }, 1, 26, 1), stepper("Hours per week", () => hours, (v) => { hours = v; }, 1, 40, 1),
      el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Your level"), levelSeg),
      el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Model"), providerHost), err);
    let ctl = null;
    const s = ui.sheet({ title: "New roadmap", content: form, onClose: () => ctl?.abort() });
    function showForm(message) {
      s.setTitle("New roadmap");
      s.body.replaceChildren(message ? el("div", {}, ui.notice(message, { tone: "bad" })) : null, form);
      s.setActions([{ label: "Generate roadmap", icon: "sparkles", variant: "primary", onClick: () => { run(); return false; } }]);
    }
    async function run() {
      if (!topic.value.trim()) { err.replaceChildren(ui.notice("Say what you want to learn.", { tone: "warn" })); return; }
      ctl = new AbortController();
      const mineCtl = ctl;
      s.setTitle("Planning…");
      s.body.replaceChildren(el("div", { class: "cd-gen-wait ui-stack", role: "status" }, el("div", { class: "ui-spinner" }), el("p", { class: "cd-sub" }, "Building your week-by-week plan. This can take a minute.")));
      s.setActions([{ label: "Cancel", variant: "secondary", onClick: () => { mineCtl.abort(); showForm(); return false; } }]);
      try {
        const rm = await api("/api/roadmaps/generate", { method: "POST", signal: mineCtl.signal,
          json: { topic: topic.value.trim(), weeks, hours_per_week: hours, level, model: provider.get() } });
        if (mineCtl.signal.aborted) return;
        ctl = null;
        s.close();
        ui.haptic("success");
        tab = "roadmaps"; store.set("sessionStorage", "hub.cards.tab", tab);
        go("roadmap", rm.id);
      } catch (error) { if (error.name !== "AbortError") showForm(error.message); }
    }
    showForm();
  }

  async function renderRoadmap() {
    const mine = ++seq;
    root.replaceChildren(backBar("Roadmap"), ui.skeleton("card", 1), ui.skeleton("list", 4));
    let rm;
    try { rm = await api(`/api/roadmaps/${encodeURIComponent(roadmapId)}`); }
    catch (error) { if (mine === seq) root.replaceChildren(backBar("Roadmap"), errorBlock(error, renderRoadmap)); return; }
    if (mine !== seq) return;

    const ring = ui.ringProgress({ value: (rm.progress.percent || 0) / 100, size: 92, stroke: 9, tone: rm.progress.percent >= 100 ? "good" : undefined,
      big: `${rm.progress.percent}%`, label: "Roadmap progress" });
    const hero = el("section", { class: "ui-card cd-rmhero" }, ring, el("div", { class: "cd-rmhero-text" },
      el("div", { class: "cd-rm-count ui-num" }, `${rm.progress.done} of ${rm.progress.total} tasks`),
      el("div", { class: "cd-sub" }, `${rm.topic || rm.title}`),
      el("div", { class: "cd-rm-badges" }, ui.badge(rm.level || "beginner", "accent"), ui.badge(`${rm.hours_per_week} h/week`), ui.badge(plural(rm.weeks.length, "week")))));
    const setProgress = (p) => { rm.progress = p; ring.set(p.percent / 100, `${p.percent}%`); hero.querySelector(".cd-rm-count").textContent = `${p.done} of ${p.total} tasks`; };

    // Open the first week that still has work; the rest stay collapsed.
    const firstOpen = Math.max(0, rm.weeks.findIndex((w) => w.progress?.percent < 100));
    const weeksHost = el("div", { class: "cd-weeks" }, rm.weeks.map((w, i) => weekCard(rm, w, i === firstOpen, setProgress)));
    root.replaceChildren(backBar(rm.title, () => roadmapMenu(rm), "Roadmap"), hero, weeksHost);
  }

  function weekCard(rm, w, open, setProgress) {
    const mini = ui.progressBar((w.progress?.percent || 0) / 100, `Week ${w.n} progress`);
    const count = el("span", { class: "cd-sub ui-num" }, `${w.progress?.done ?? 0}/${w.progress?.total ?? 0}`);
    const chev = ui.icon("chevron-down", { class: "cd-chev" });
    const head = el("button", { type: "button", class: "cd-weekhead", "aria-expanded": String(open) },
      el("span", { class: "cd-weeknum ui-num" }, String(w.n)), el("span", { class: "cd-weektitle" }, el("strong", { class: "ui-clamp-2" }, w.title), count), chev);
    const body = el("div", { class: "cd-weekbody ui-stack", hidden: !open });
    const card = el("section", { class: `ui-card cd-week${open ? " is-open" : ""}` }, head, el("div", { class: "cd-weekbar" }, mini), body);
    head.addEventListener("click", () => {
      const now = head.getAttribute("aria-expanded") !== "true";
      head.setAttribute("aria-expanded", String(now)); body.hidden = !now; card.classList.toggle("is-open", now);
    });

    if ((w.goals || []).length) body.append(el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Goals"), el("ul", { class: "cd-bullets" }, w.goals.map((g) => el("li", {}, g)))));
    if ((w.resources || []).length) body.append(el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Resources"), el("ul", { class: "cd-bullets" }, w.resources.map((r) => {
      const url = /^https?:\/\/\S+$/i.test(String(r).trim()) ? String(r).trim() : null;
      return el("li", {}, url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, clip(url, 60)) : r);
    }))));
    const tasks = el("div", { class: "cd-tasks" });
    for (const t of w.tasks || []) {
      const box = el("input", { type: "checkbox", checked: !!t.done, "aria-label": t.text });
      const row = el("label", { class: `cd-task${t.done ? " is-done" : ""}` }, box, el("span", { class: "cd-checkmark" }, ui.icon("check")), el("span", { class: "cd-tasktext" }, t.text));
      box.addEventListener("change", async () => {
        const want = box.checked;
        row.classList.toggle("is-done", want);
        try {
          const res = await api(`/api/roadmaps/${encodeURIComponent(rm.id)}/tasks/${encodeURIComponent(t.id)}/toggle`, { method: "POST" });
          t.done = !!res.task?.done; box.checked = t.done; row.classList.toggle("is-done", t.done);
          mini.set((res.week_progress?.percent || 0) / 100); count.textContent = `${res.week_progress?.done}/${res.week_progress?.total}`;
          setProgress(res.progress);
          if (want) ui.haptic("light");
        } catch (error) { box.checked = !want; row.classList.toggle("is-done", !want); ctx.toast(error.message, { kind: "bad" }); }
      });
      tasks.append(row);
    }
    if (tasks.children.length) body.append(el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Tasks"), tasks));
    body.append(el("div", { class: "cd-row2" },
      ui.button({ label: "Make cards for this week", icon: "layers", variant: "tonal", onClick: async (e) => {
        try {
          const r = await ui.busy(e.currentTarget, api(`/api/roadmaps/${encodeURIComponent(rm.id)}/weeks/${w.n}/make-cards`, { method: "POST", json: { model: model(), count: 10 } }));
          ctx.toast(`Added ${plural(r.added, "card")} to "${r.deck?.name || "a new deck"}"`, { kind: "good", action: r.deck?.id ? { label: "Open", onClick: () => go("deck", r.deck.id) } : undefined });
        } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
      } }),
      ui.button({ label: "Redo week", icon: "refresh", variant: "ghost", onClick: () => regenerateSheet(rm, w) })));
    return card;
  }

  function regenerateSheet(rm, w) {
    const note = el("textarea", { rows: "3", maxlength: "300", placeholder: "Optional: for example, more hands-on projects", "aria-label": "What should change" });
    const err = el("div", {});
    ui.sheet({ title: `Redo week ${w.n}`, content: el("div", { class: "ui-stack" },
      el("p", { class: "cd-sub" }, "The model rewrites this week. Its tasks start unticked; other weeks stay as they are."),
      el("div", { class: "ui-field" }, el("label", { class: "ui-label" }, "What should change?"), note), err),
    actions: [{ label: "Rewrite week", icon: "sparkles", variant: "primary", async onClick() {
      try { await api(`/api/roadmaps/${encodeURIComponent(rm.id)}/weeks/${w.n}/regenerate`, { method: "POST", json: { model: model(), note: note.value.trim() || undefined } }); ctx.toast("Week rewritten", { kind: "good" }); renderRoadmap(); }
      catch (error) { err.replaceChildren(ui.notice(error.message, { tone: "bad" })); return false; }
    } }] });
  }

  function roadmapMenu(rm) {
    const s = ui.sheet({ title: rm.title, content: el("div", { class: "ui-list ui-list--inset ui-list--icons" }, [
      ui.listRow({ icon: "edit", title: "Rename", onClick: () => { s.close(); renameSheet(rm); } }),
      ui.listRow({ icon: "trash", tone: "bad", title: "Delete roadmap", sub: "Decks made from it are kept", onClick: async () => {
        s.close();
        if (!(await ui.confirm({ title: `Delete "${rm.title}"?`, message: "The plan and your ticked tasks are removed. Decks you made from it stay.", confirmLabel: "Delete roadmap", danger: true }))) return;
        try { await api(`/api/roadmaps/${encodeURIComponent(rm.id)}`, { method: "DELETE", json: { confirm: true } }); ctx.toast("Roadmap deleted", { kind: "good" }); go("home"); }
        catch (error) { ctx.toast(error.message, { kind: "bad" }); }
      } }),
    ]) });
  }
  function renameSheet(rm) {
    const input = el("input", { type: "text", maxlength: "200", value: rm.title, "aria-label": "Roadmap name" });
    const err = el("div", {});
    ui.sheet({ title: "Rename roadmap", content: el("div", { class: "ui-stack" }, input, err),
      actions: [{ label: "Save", variant: "primary", async onClick() {
        if (!input.value.trim()) { err.replaceChildren(ui.notice("The name can't be empty.", { tone: "warn" })); return false; }
        try { await api(`/api/roadmaps/${encodeURIComponent(rm.id)}`, { method: "PATCH", json: { title: input.value.trim() } }); renderRoadmap(); }
        catch (error) { err.replaceChildren(ui.notice(error.message, { tone: "bad" })); return false; }
      } }] });
  }

  // ================================================================ start
  tab = ["decks", "roadmaps", "stats"].includes(tab) ? tab : "decks";
  renderHome();

  return {
    show() { if (page === "home") renderHome(); },
    hide() {},
    refresh() { paint(); },
    openSection(name) {
      if (name === "review") { startReview(null); return; }
      if (["decks", "roadmaps", "stats"].includes(name)) { page = "home"; setTab(name); }
    },
    async openItem(id, section) {
      if (section === "roadmaps" || section === "roadmap") { go("roadmap", id); return; }
      try {
        const card = await api(`/api/cards/cards/${encodeURIComponent(id)}`);
        const deck = await api(`/api/cards/decks/${encodeURIComponent(card.deck_id)}`);
        go("deck", deck.id);
        cardSheet(deck, card, () => paint());
      } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    },
  };
}
