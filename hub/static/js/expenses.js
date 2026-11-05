// Spending: month summary, a "Needs a category" queue of captured UPI payments, quick add, expense list,
// budgets and capture settings.
//
// API: docs/api/life.md (expenses, budgets, summary, export) and docs/api/life-capture.md (pending queue, categories, settings).
// Money is integer cents everywhere. Text from the network is only ever set through el()/textContent.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const SVG_NS = "http://www.w3.org/2000/svg";

// ---- small helpers -----------------------------------------------------------------------------------
const pad = (n) => String(n).padStart(2, "0");
const monthKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
const addMonths = (key, n) => { const [y, m] = key.split("-").map(Number); return monthKey(new Date(y, m - 1 + n, 1)); };
const monthLabel = (key, long = true) => {
  const [y, m] = key.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: long ? "long" : "short", year: "numeric" });
};
const monthName = (key) => { const [y, m] = key.split("-").map(Number); return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long" }); };
const parseDay = (s) => new Date(`${String(s).slice(0, 10)}T00:00:00`);
const dayStamp = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function dayLabel(s) {
  const d = parseDay(s);
  if (Number.isNaN(d.getTime())) return String(s);
  const now = new Date();
  if (dayStamp(d) === dayStamp(now)) return "Today";
  if (dayStamp(d) === dayStamp(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1))) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

function money(cents, currency = "INR", { compact = false } = {}) {
  const value = (Number(cents) || 0) / 100;
  const whole = Math.round(value * 100) % 100 === 0;
  try {
    return new Intl.NumberFormat("en-IN", {
      style: "currency", currency, currencyDisplay: "narrowSymbol",
      minimumFractionDigits: whole || compact ? 0 : 2, maximumFractionDigits: compact ? 0 : 2,
    }).format(value);
  } catch (_) {
    return `${currency} ${value.toFixed(whole ? 0 : 2)}`;
  }
}
// "450", "450.5", "1,200" -> cents, or null when it is not a positive number
function toCents(text) {
  const n = Number(String(text).replace(/[,\s]/g, ""));
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.round(n * 100);
}
const centsToInput = (c) => { const v = (Number(c) || 0) / 100; return Math.round(v * 100) % 100 === 0 ? String(Math.round(v)) : v.toFixed(2); };
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
const softFail = (p) => p.then((v) => ({ ok: true, v }), (e) => ({ ok: false, e }));

// ---- categories: label, icon, tone -------------------------------------------------------------------
const CAT_STYLE = {
  food: ["coffee", "orange"], groceries: ["list", "good"], transport: ["arrow-right", "info"], bills: ["clipboard", "warn"],
  shopping: ["tag", "pink"], health: ["heart", "bad"], entertainment: ["play", "violet"], travel: ["globe", "teal"],
  rent: ["home", "accent"], education: ["book", "info"], transfers: ["send", "gray"], other: ["circle", "gray"],
};
const TONES = ["accent", "orange", "teal", "pink", "violet", "info", "good", "warn"];
function catStyle(id) {
  if (CAT_STYLE[id]) return CAT_STYLE[id];
  let h = 0;
  for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return ["tag", TONES[h % TONES.length]];
}
const FALLBACK_CATS = Object.keys(CAT_STYLE).map((id) => ({ id, label: cap(id) }));

let cssLoaded = false;
function loadCss() {
  if (cssLoaded || document.querySelector('link[data-sp-css]')) { cssLoaded = true; return; }
  cssLoaded = true;
  document.head.append(el("link", { rel: "stylesheet", href: "/css/expenses.css", "data-sp-css": "1" }));
}

function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

// ---- the screen --------------------------------------------------------------------------------------
export function init(container, ctx) {
  loadCss();
  const root = el("div", { class: "sp" });
  container.append(root);

  const now = new Date();
  const thisMonth = monthKey(now);
  let month = thisMonth;
  let cats = FALLBACK_CATS;
  let catLabel = (id) => (cats.find((c) => c.id === id)?.label) || cap(id);
  let summary = null;
  let expenses = [];
  let pending = [];
  let filterCat = null;
  let settings = null;
  let visible = false;
  let poll = null;
  let firstPaint = true;
  let token = 0; // bumps on month change so a slow answer for an old month is ignored

  // ---- skeleton of the page ----
  const pickerLabel = el("div", { class: "sp-month__label", "aria-live": "polite" });
  const prevBtn = ui.button({ label: "Previous month", icon: "chevron-left", variant: "ghost", iconOnly: true, onClick: () => setMonth(addMonths(month, -1)) });
  const nextBtn = ui.button({ label: "Next month", icon: "chevron-right", variant: "ghost", iconOnly: true, onClick: () => setMonth(addMonths(month, 1)) });
  const gearBtn = ui.button({ label: "Capture settings", icon: "sliders", variant: "ghost", iconOnly: true, onClick: () => openCaptureSettings() });
  const header = el("header", { class: "sp-head" },
    el("div", { class: "sp-month" }, prevBtn, pickerLabel, nextBtn), gearBtn);

  const pendingSlot = el("section", { class: "sp-pending-slot", "aria-label": "Payments that need a category" });
  const addSlot = el("section", { class: "sp-add", "aria-label": "Quick add" });
  const totalSlot = el("div", { class: "sp-slot" }, ui.skeleton("card", 1));
  const chartSlot = el("div", { class: "sp-slot" });
  const daySlot = el("div", { class: "sp-slot" });
  const budgetSlot = el("div", { class: "sp-slot" });
  const listSlot = el("div", { class: "sp-slot" }, ui.skeleton("list", 4));
  const captureSlot = el("div", { class: "sp-slot" });
  const stack = el("div", { class: "sp-stack" }, pendingSlot, addSlot, totalSlot, chartSlot, daySlot, budgetSlot, listSlot, captureSlot);
  root.append(header, stack);

  // ---- data ----
  const errText = (e) => (e instanceof ApiError || e?.message ? e.message : "Something went wrong.");

  async function loadCats() {
    const r = await softFail(api("/api/expenses/categories"));
    if (r.ok && Array.isArray(r.v?.categories) && r.v.categories.length) cats = r.v.categories;
  }
  async function loadPending() {
    const r = await softFail(api("/api/expenses/pending"));
    if (r.ok) { pending = (r.v.items || []).filter((i) => i.status === "pending"); renderPending(); }
    else if (firstPaint) { pendingSlot.replaceChildren(); }
    ctx.setBadge?.("expenses", pending.length);
  }
  async function loadMonth() {
    const mine = ++token;
    const m = month;
    const [s, l] = await Promise.all([
      softFail(api(`/api/expenses/summary?month=${m}`)),
      softFail(api(`/api/expenses?month=${m}`)),
    ]);
    if (mine !== token) return;
    if (s.ok) summary = s.v; else summary = null;
    if (l.ok) expenses = l.v.expenses || []; else expenses = null;
    renderSummary(s.ok ? null : s.e);
    renderList(l.ok ? null : l.e);
    firstPaint = false;
  }
  async function refresh() {
    await Promise.all([loadPending(), loadMonth()]);
  }

  function setMonth(key) {
    if (key > thisMonth) return;
    month = key;
    filterCat = null;
    paintMonthPicker();
    totalSlot.replaceChildren(ui.skeleton("card", 1));
    chartSlot.replaceChildren(); daySlot.replaceChildren(); budgetSlot.replaceChildren();
    listSlot.replaceChildren(ui.skeleton("list", 4));
    loadMonth();
  }
  function paintMonthPicker() {
    pickerLabel.textContent = monthLabel(month);
    nextBtn.disabled = month >= thisMonth;
    prevBtn.disabled = month <= addMonths(thisMonth, -36);
  }
  const currency = () => summary?.currency || "INR";

  // ---- pending queue ("Needs a category") ----
  function renderPending() {
    if (!pending.length) { pendingSlot.replaceChildren(); return; }
    const head = el("div", { class: "sp-pending__head" },
      el("div", { class: "sp-pending__title" },
        ui.tile("alert-circle", "warn", "sm"),
        el("div", { class: "grow" },
          el("h2", { class: "ui-section-title" }, "Needs a category ", ui.badge(String(pending.length), "warn")),
          el("p", { class: "sp-sub" }, pending.length === 1 ? "A payment was captured from your phone." : "Payments captured from your phone."))),
      pending.length > 1 ? ui.button({ label: "Dismiss all", variant: "ghost", size: "sm", onClick: dismissAll }) : null);
    const cards = el("div", { class: "sp-pending__cards" }, pending.map(pendingCard));
    pendingSlot.replaceChildren(el("div", { class: "sp-pending" }, head, cards));
  }

  function pendingCard(item) {
    const cur = item.currency || "INR";
    const suggested = item.suggested_category || null;
    const order = [...cats].sort((a, b) => (b.id === suggested) - (a.id === suggested));
    const chips = order.map((c) => {
      const [ic, tone] = catStyle(c.id);
      const on = c.id === suggested;
      const b = el("button", { type: "button", class: "ui-chip sp-chip" + (on ? " is-on" : ""), "data-tone": tone, "data-cat": c.id,
        "aria-pressed": on ? "true" : "false", "aria-label": `${on ? "Suggested: " : ""}File under ${c.label}` },
        ui.icon(on ? "check" : ic), c.label);
      b.addEventListener("click", () => confirmItem(item, { category: c.id }, card));
      return b;
    });
    const metaBits = [item.app, item.bank ? `${item.bank}${item.account_last4 ? ` ····${item.account_last4}` : ""}` : null,
      item.ts ? ui.relTime(item.ts) : dayLabel(item.date)].filter(Boolean);
    const name = item.merchant || "UPI payment";
    const card = el("article", { class: "ui-card sp-pcard", "data-id": item.id },
      el("div", { class: "sp-pcard__top" },
        ui.avatar(name),
        el("div", { class: "sp-pcard__who" },
          el("h3", { class: "sp-pcard__name ui-truncate" }, name, item.test ? ui.badge("Test", "info") : null),
          el("p", { class: "sp-sub ui-truncate" }, metaBits.join(" · "))),
        el("div", { class: "sp-pcard__amt ui-num" }, money(item.amount_cents, cur))),
      el("p", { class: "sp-pcard__hint" }, suggested ? `Looks like ${catLabel(suggested)}. Tap to file it, or swipe for others.` : "Tap a category to file it."),
      el("div", { class: "sp-chips sp-chips--row", role: "group", "aria-label": "Category" }, chips),
      el("div", { class: "sp-pcard__foot" },
        ui.button({ label: "Edit", icon: "pen", variant: "ghost", size: "sm", onClick: () => editPending(item, card) }),
        ui.button({ label: "Dismiss", icon: "x", variant: "ghost", size: "sm", onClick: () => dismissItem(item, card) })));
    return card;
  }

  function lock(card, on) {
    card.classList.toggle("is-busy", on);
    card.querySelectorAll("button").forEach((b) => { b.disabled = on; });
  }
  function leaveCard(card) {
    card.classList.add("is-leaving");
    return new Promise((res) => setTimeout(res, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 260));
  }

  async function confirmItem(item, body, card, sheet) {
    if (card) lock(card, true);
    try {
      const r = await api(`/api/expenses/pending/${item.id}/confirm`, { method: "POST", json: body });
      const cur = item.currency || "INR";
      const cat = r.expense?.category || body.category;
      const name = (body.merchant || item.merchant || "").trim();
      let msg;
      if (item.test) msg = "Test payment cleared. Nothing was added to your spending.";
      else msg = `${money(r.expense?.amount ?? body.amount_cents ?? item.amount_cents, cur)}${name ? ` to ${name}` : ""} filed under ${catLabel(cat)}.`
        + (name ? ` ${name} will default to ${catLabel(cat)} from now on.` : "");
      ui.toast(msg, { kind: "good", ms: 5200 });
      sheet?.close();
      if (card) await leaveCard(card);
      pending = pending.filter((p) => p.id !== item.id);
      renderPending();
      ctx.setBadge?.("expenses", pending.length);
      loadMonth();
    } catch (error) {
      if (card) lock(card, false);
      ui.toast(errText(error), { kind: "bad" });
      if (error.status === 404 || error.status === 409) loadPending();
      return false;
    }
    return true;
  }

  async function dismissItem(item, card) {
    lock(card, true);
    try {
      await api(`/api/expenses/pending/${item.id}/dismiss`, { method: "POST" });
      ui.toast(`Dismissed${item.merchant ? ` ${item.merchant}` : ""}. Nothing was added to your spending.`, { icon: "check" });
      await leaveCard(card);
      pending = pending.filter((p) => p.id !== item.id);
      renderPending();
      ctx.setBadge?.("expenses", pending.length);
    } catch (error) {
      lock(card, false);
      ui.toast(errText(error), { kind: "bad" });
      if (error.status === 404 || error.status === 409) loadPending();
    }
  }

  async function dismissAll() {
    const n = pending.length;
    const ok = await ui.confirm({
      title: `Dismiss ${n} payment${n === 1 ? "" : "s"}?`,
      message: "They won't be added to your spending. This can't be undone from here.",
      confirmLabel: "Dismiss all", danger: true, icon: "trash",
    });
    if (!ok) return;
    try {
      const r = await api("/api/expenses/pending/dismiss-all", { method: "POST", json: { confirm: true } });
      ui.toast(`Dismissed ${r.dismissed ?? n}. Nothing was added to your spending.`, { icon: "check" });
      pending = [];
      renderPending();
      ctx.setBadge?.("expenses", 0);
    } catch (error) { ui.toast(errText(error), { kind: "bad" }); loadPending(); }
  }

  // shared form pieces for the two edit sheets
  function field(label, input, hint) {
    return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input, hint ? el("span", { class: "ui-hint" }, hint) : null);
  }
  function catPicker(value) {
    const id = "sp-cats-" + Math.random().toString(36).slice(2, 7);
    const input = el("input", { type: "text", list: id, value: value || "", maxlength: "40", autocomplete: "off", autocapitalize: "none", placeholder: "food, rent, or your own" });
    const list = el("datalist", { id }, cats.map((c) => el("option", { value: c.id }, c.label)));
    const wrap = el("span", { class: "sp-pick" }, input, list);
    wrap.input = input;
    return wrap;
  }

  function editPending(item, card) {
    const cur = item.currency || "INR";
    const amount = el("input", { type: "text", inputmode: "decimal", value: centsToInput(item.amount_cents), autocomplete: "off", "aria-label": "Amount" });
    const merchant = el("input", { type: "text", value: item.merchant || "", maxlength: "60", autocomplete: "off" });
    const note = el("input", { type: "text", value: "", maxlength: "200", autocomplete: "off", placeholder: item.merchant || "UPI payment" });
    const catIn = catPicker(item.suggested_category || "");
    const err = el("p", { class: "ui-hint is-error sp-err", role: "alert" });
    const body = el("div", { class: "ui-stack" },
      el("p", { class: "sp-sub" }, [item.app, item.raw_snippet].filter(Boolean).join(" · ")),
      field(`Amount (${cur})`, amount), field("Merchant", merchant), field("Note", note, "Shown in your list. Leave empty to use the merchant."),
      field("Category", catIn), err);
    const sheet = ui.sheet({
      title: "Edit payment", content: body,
      actions: [
        { label: "Dismiss", variant: "ghost", icon: "x", onClick: () => { dismissItem(item, card); } },
        { label: "File it", variant: "primary", icon: "check", onClick: async () => {
          const cents = toCents(amount.value);
          const category = catIn.input.value.trim().toLowerCase();
          if (!cents) { err.textContent = "Enter an amount above zero."; return false; }
          if (!category) { err.textContent = "Pick or type a category."; return false; }
          const payload = { category };
          if (cents !== item.amount_cents) payload.amount_cents = cents;
          if (merchant.value.trim() && merchant.value.trim() !== (item.merchant || "")) payload.merchant = merchant.value.trim();
          if (note.value.trim()) payload.note = note.value.trim();
          const done = await confirmItem(item, payload, card);
          return done ? undefined : false;
        } },
      ],
    });
    return sheet;
  }

  // ---- quick add ----
  function buildAdd() {
    const input = el("input", { type: "text", class: "sp-add__input", placeholder: "450 lunch swiggy", maxlength: "200", autocomplete: "off",
      enterkeyhint: "done", "aria-label": "Add an expense in words" });
    const go = ui.button({ label: "Add", icon: "plus", variant: "primary", type: "submit" });
    const echo = el("div", { class: "sp-echo", "aria-live": "polite" });
    const form = el("form", { class: "sp-add__form" },
      el("div", { class: "ui-input-wrap sp-add__wrap" }, ui.icon("plus"), input), go);
    let echoTimer = null;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) { input.focus(); return; }
      echo.replaceChildren();
      try {
        const x = await ui.busy(go, api("/api/expenses/quick", { method: "POST", json: { text } }));
        input.value = "";
        const [ic, tone] = catStyle(x.category);
        echo.replaceChildren(el("div", { class: "sp-echo__card", "data-tone": tone },
          ui.tile(ic, tone, "sm"),
          el("div", { class: "grow" },
            el("strong", {}, `Added ${money(x.amount, x.currency)}`),
            el("span", { class: "sp-sub" }, ` ${catLabel(x.category)}${x.note ? ` · ${x.note}` : ""} · ${dayLabel(x.date)}${x.date.slice(0, 7) !== month ? ` (in ${monthLabel(x.date.slice(0, 7), false)})` : ""}`))));
        ui.haptic("success");
        clearTimeout(echoTimer);
        echoTimer = setTimeout(() => echo.replaceChildren(), 9000);
        loadMonth();
      } catch (error) {
        echo.replaceChildren(ui.notice(error.message, { tone: "bad" }));
        ui.haptic("error");
      }
    });
    addSlot.replaceChildren(form, echo,
      el("p", { class: "sp-tip" }, "Try “1.2k rent #home”, “₹60 chai yesterday” or “$5 coffee”."));
    addSlot.input = input;
  }

  // ---- summary: total, donut, by-day, budgets ----
  function renderSummary(error) {
    if (error || !summary) {
      for (const s of [chartSlot, daySlot, budgetSlot]) s.replaceChildren();
      totalSlot.replaceChildren(ui.notice(error ? errText(error) : "Couldn't load the summary.", { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: loadMonth }));
      return;
    }
    const cur = summary.currency || "INR";
    // total card
    const vs = summary.vs_last_month || {};
    let change = null;
    if (vs.last_total > 0 || summary.total > 0) {
      const diff = vs.change ?? summary.total - (vs.last_total || 0);
      const pct = vs.change_percent;
      let text;
      if (!vs.last_total) text = `Nothing to compare with last month`;
      else if (diff === 0) text = `Same as last month`;
      else text = `${money(Math.abs(diff), cur, { compact: true })}${pct != null ? ` (${Math.abs(Math.round(pct))}%)` : ""} ${diff > 0 ? "more" : "less"} than last month`;
      change = el("p", { class: "sp-total__chg" },
        vs.last_total && diff !== 0 ? ui.icon(diff > 0 ? "arrow-up-right" : "arrow-down", { size: 16 }) : null, text);
    }
    const others = Object.entries(summary.other_currencies || {});
    const total = el("section", { class: "ui-card ui-card--gradient sp-total", "aria-label": "Total spent" },
      el("p", { class: "sp-total__label" }, `Spent in ${monthName(month)}${month === thisMonth ? " so far" : ""}`),
      el("p", { class: "sp-total__amt ui-num" }, money(summary.total, cur)),
      change,
      others.length ? el("p", { class: "sp-total__more" }, "Also in ", others.map(([c, v]) => money(v, c)).join(", "), ". Not added to the total.") : null);
    totalSlot.replaceChildren(total);

    // categories
    const entries = Object.entries(summary.by_category || {}).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
    chartSlot.replaceChildren(entries.length ? categoryCard(entries, cur) : "");
    // by day
    daySlot.replaceChildren(summary.total > 0 ? dayCard(cur) : "");
    // budgets
    renderBudgets();
  }

  function categoryCard(entries, cur) {
    const total = entries.reduce((s, [, v]) => s + v, 0);
    const R = 52, C = 2 * Math.PI * R, GAP = entries.length > 1 ? 3 : 0;
    const donut = svg("svg", { viewBox: "0 0 140 140", class: "sp-donut", role: "img", "aria-label": `Spending by category, ${entries.length} categories` });
    donut.append(svg("circle", { cx: 70, cy: 70, r: R, class: "sp-donut__track" }));
    let acc = 0;
    const segs = [];
    entries.forEach(([id, v]) => {
      const len = (v / total) * C;
      const [, tone] = catStyle(id);
      const seg = svg("circle", { cx: 70, cy: 70, r: R, class: "sp-donut__seg", "data-tone": tone, "data-cat": id,
        "stroke-dasharray": `${Math.max(0.5, len - GAP)} ${C - Math.max(0.5, len - GAP)}`, "stroke-dashoffset": -acc, transform: "rotate(-90 70 70)" });
      seg.addEventListener("click", () => toggleFilter(id));
      donut.append(seg);
      segs.push(seg);
      acc += len;
    });
    const centre = el("div", { class: "sp-donut__centre" },
      el("span", { class: "sp-donut__big ui-num" }, filterCat && summary.by_category[filterCat] != null ? money(summary.by_category[filterCat], cur, { compact: true }) : money(total, cur, { compact: true })),
      el("span", { class: "sp-donut__small" }, filterCat ? catLabel(filterCat) : `${entries.length} categor${entries.length === 1 ? "y" : "ies"}`));
    const legend = el("ul", { class: "sp-legend" }, entries.map(([id, v]) => {
      const [, tone] = catStyle(id);
      const pct = Math.round((v / total) * 100);
      const row = el("button", { type: "button", class: "sp-legend__row" + (filterCat === id ? " is-on" : ""), "data-tone": tone, "aria-pressed": String(filterCat === id),
        "aria-label": `${catLabel(id)}, ${money(v, cur)}, ${pct} percent. Show only this category` },
        el("span", { class: "sp-dot" }),
        el("span", { class: "sp-legend__name ui-truncate" }, catLabel(id)),
        el("span", { class: "sp-legend__pct" }, `${pct}%`),
        el("span", { class: "sp-legend__amt ui-num" }, money(v, cur, { compact: true })),
        el("span", { class: "sp-legend__bar" }, el("i", { style: `--v:${v / entries[0][1]}` })));
      row.addEventListener("click", () => toggleFilter(id));
      return el("li", {}, row);
    }));
    if (filterCat) donut.classList.add("is-filtered");
    segs.forEach((s) => s.classList.toggle("is-dim", !!filterCat && s.getAttribute("data-cat") !== filterCat));
    return el("section", { class: "ui-card sp-card", "aria-label": "By category" },
      el("div", { class: "ui-card__head" }, el("h2", { class: "ui-card__title grow" }, "By category"),
        filterCat ? ui.button({ label: "Show all", variant: "ghost", size: "sm", onClick: () => toggleFilter(filterCat) }) : null),
      el("div", { class: "sp-donut-wrap" }, el("div", { class: "sp-donut-box" }, donut, centre)),
      legend);
  }

  function toggleFilter(id) {
    filterCat = filterCat === id ? null : id;
    ui.haptic("select");
    renderSummary();
    renderList();
  }

  function dayCard(cur) {
    const [y, m] = month.split("-").map(Number);
    const daysIn = new Date(y, m, 0).getDate();
    const last = month === thisMonth ? now.getDate() : daysIn;
    const by = summary.by_day || {};
    const vals = [];
    for (let d = 1; d <= last; d += 1) vals.push(by[`${month}-${pad(d)}`] || 0);
    const max = Math.max(...vals, 1);
    const W = 300, H = 64, padY = 6;
    const x = (i) => (last === 1 ? W / 2 : (i / (last - 1)) * W);
    const yv = (v) => H - padY - (v / max) * (H - padY * 2);
    const pts = vals.map((v, i) => [x(i), yv(v)]);
    const line = pts.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`).join(" ");
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "sp-spark", preserveAspectRatio: "none", role: "img",
      "aria-label": `Spending per day. Highest day ${money(max, cur)}.` });
    chart.append(svg("path", { d: `${line} L${x(last - 1).toFixed(1)} ${H} L0 ${H} Z`, class: "sp-spark__area" }),
      svg("path", { d: line, class: "sp-spark__line", fill: "none" }));
    const peakIdx = vals.indexOf(Math.max(...vals));
    const peakDay = `${month}-${pad(peakIdx + 1)}`;
    const days = Object.values(by).filter((v) => v > 0).length;
    const avg = Math.round(summary.total / Math.max(1, last));
    return el("section", { class: "ui-card sp-card", "aria-label": "Spending by day" },
      el("div", { class: "ui-card__head" }, el("h2", { class: "ui-card__title grow" }, "Day by day"),
        el("span", { class: "sp-sub" }, `${money(avg, cur, { compact: true })} a day`)),
      el("div", { class: "sp-spark-wrap" }, chart,
        el("span", { class: "sp-spark__peak", style: `left:${last === 1 ? 50 : (peakIdx / (last - 1)) * 100}%;top:${(yv(max) / H) * 100}%` })),
      el("div", { class: "sp-spark__axis" }, el("span", {}, "1"), el("span", {}, `${last}`)),
      el("p", { class: "sp-sub" }, `Biggest day: ${dayLabel(peakDay)}, ${money(max, cur)}. Spent on ${days} day${days === 1 ? "" : "s"}.`));
  }

  function renderBudgets() {
    const cur = currency();
    const list = (summary?.budgets || []);
    const head = el("div", { class: "ui-card__head" }, el("h2", { class: "ui-card__title grow" }, "Budgets"),
      ui.button({ label: list.length ? "Edit" : "Set up", icon: list.length ? "sliders" : "plus", variant: "ghost", size: "sm", onClick: openBudgets }));
    let body;
    if (!list.length) {
      body = el("p", { class: "sp-sub sp-budget-empty" }, "Set a monthly limit for a category like Food and you'll see here how much is left. You get a nudge when you cross it.");
    } else {
      body = el("ul", { class: "sp-budgets" }, list.map((b) => {
        const [, tone] = catStyle(b.category);
        const state = b.status === "over" ? "over" : b.near ? "near" : "ok";
        const used = Math.min(1, (b.spent || 0) / (b.limit || 1));
        const msg = state === "over" ? `${money(b.spent - b.limit, b.currency || cur, { compact: true })} over` : `${money(b.remaining, b.currency || cur, { compact: true })} left`;
        return el("li", { class: `sp-budget is-${state}`, "data-tone": tone },
          el("div", { class: "sp-budget__row" },
            el("span", { class: "sp-dot" }), el("span", { class: "sp-budget__name grow ui-truncate" }, catLabel(b.category)),
            el("span", { class: "sp-budget__msg" }, msg)),
          el("div", { class: "ui-bar sp-budget__bar", role: "progressbar", "aria-valuenow": String(Math.min(100, b.percent)), "aria-valuemin": "0", "aria-valuemax": "100",
            "aria-label": `${catLabel(b.category)} budget used` }, el("i", { style: `--v:${used}` })),
          el("p", { class: "sp-sub" }, `${money(b.spent, b.currency || cur, { compact: true })} of ${money(b.limit, b.currency || cur, { compact: true })} · ${b.percent}%${state === "near" ? " · almost there" : ""}`));
      }));
    }
    budgetSlot.replaceChildren(el("section", { class: "ui-card sp-card", "aria-label": "Budgets" }, head, body));
  }

  // ---- expense list ----
  function renderList(error) {
    if (error || expenses == null) {
      listSlot.replaceChildren(ui.notice(error ? errText(error) : "Couldn't load your expenses.", { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: loadMonth }));
      return;
    }
    const shown = filterCat ? expenses.filter((x) => x.category === filterCat) : expenses;
    const exportHref = `/api/expenses/export.csv?month=${month}`;
    const head = el("div", { class: "ui-section-head sp-list-head" },
      el("h2", { class: "ui-section-title grow" }, filterCat ? `${catLabel(filterCat)} in ${monthName(month)}` : "Expenses"),
      filterCat ? ui.button({ label: "Show all", variant: "ghost", size: "sm", onClick: () => toggleFilter(filterCat) }) : null,
      el("a", { class: "ui-link sp-export", href: exportHref, download: `expenses-${month}.csv` }, ui.icon("download"), "CSV"));
    if (!shown.length) {
      listSlot.replaceChildren(head, expenses.length ? ui.emptyState({ icon: "filter", title: "Nothing here", body: "No expenses in this category this month.", compact: true }) : emptyMonth());
      return;
    }
    const groups = new Map();
    for (const x of shown) { if (!groups.has(x.date)) groups.set(x.date, []); groups.get(x.date).push(x); }
    const days = [...groups.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
    const nodes = days.map(([day, items]) => {
      const sum = items.reduce((s, x) => s + (x.currency === currency() ? x.amount : 0), 0);
      return el("div", { class: "sp-day" },
        el("div", { class: "sp-day__head" }, el("h3", {}, dayLabel(day)), el("span", { class: "ui-num" }, money(sum, currency(), { compact: true }))),
        el("ul", { class: "ui-list ui-list--inset ui-list--icons sp-rows" }, items.map(expenseRow)));
    });
    listSlot.replaceChildren(head, el("div", { class: "sp-days" }, nodes));
  }

  function emptyMonth() {
    return ui.emptyState({
      icon: "trend", title: month === thisMonth ? "No spending yet this month" : `Nothing spent in ${monthLabel(month)}`,
      body: "Type an expense above, like “450 lunch swiggy”. Or let your phone do it: the Android app spots UPI payments and bank texts and lists them here under “Needs a category”. You tap a category once and it learns the merchant.",
      action: { label: "How capture works", icon: "phone", onClick: openCaptureSettings },
    });
  }

  function expenseRow(x) {
    const [ic, tone] = catStyle(x.category);
    const row = el("li", {}, el("button", { type: "button", class: "ui-row ui-row--interactive sp-row", "data-id": x.id },
      ui.tile(ic, tone),
      el("span", { class: "ui-row__body" },
        el("span", { class: "ui-row__title ui-truncate" }, x.note || catLabel(x.category)),
        el("span", { class: "ui-row__sub ui-truncate" }, [catLabel(x.category), ...(x.tags || []).map((t) => `#${t}`)].join(" · "))),
      el("span", { class: "ui-row__meta ui-num sp-amt" }, money(x.amount, x.currency))));
    row.firstChild.addEventListener("click", () => editExpense(x));
    return row;
  }

  function editExpense(x) {
    const amount = el("input", { type: "text", inputmode: "decimal", value: centsToInput(x.amount), autocomplete: "off" });
    const note = el("input", { type: "text", value: x.note || "", maxlength: "200", autocomplete: "off" });
    const date = el("input", { type: "date", value: x.date, max: dayStamp(new Date()) });
    const catIn = catPicker(x.category);
    const err = el("p", { class: "ui-hint is-error sp-err", role: "alert" });
    const chips = el("div", { class: "sp-chips sp-chips--sm", role: "group", "aria-label": "Category shortcuts" },
      cats.map((c) => {
        const b = el("button", { type: "button", class: "ui-chip", "aria-pressed": String(c.id === x.category) }, c.label);
        b.addEventListener("click", () => { catIn.input.value = c.id; chips.querySelectorAll("button").forEach((o) => o.setAttribute("aria-pressed", String(o === b))); });
        return b;
      }));
    const body = el("div", { class: "ui-stack" }, field(`Amount (${x.currency})`, amount), field("Note", note), field("Date", date), field("Category", catIn), chips, err);
    ui.sheet({
      title: "Edit expense", content: body,
      actions: [
        { label: "Delete", icon: "trash", variant: "danger-soft", onClick: async () => {
          const ok = await ui.confirm({ title: "Delete this expense?", message: `${money(x.amount, x.currency)}${x.note ? ` for ${x.note}` : ""} will be removed from your spending.`, confirmLabel: "Delete", danger: true, icon: "trash" });
          if (!ok) return false;
          try { await api(`/api/expenses/${x.id}`, { method: "DELETE" }); ui.toast("Expense deleted.", { icon: "check" }); loadMonth(); }
          catch (e) { ui.toast(errText(e), { kind: "bad" }); return false; }
        } },
        { label: "Save", icon: "check", variant: "primary", onClick: async () => {
          const cents = toCents(amount.value);
          if (!cents) { err.textContent = "Enter an amount above zero."; return false; }
          const category = catIn.input.value.trim().toLowerCase() || "other";
          try {
            await api(`/api/expenses/${x.id}`, { method: "PATCH", json: { amount: cents, note: note.value.trim(), date: date.value || x.date, category } });
            ui.toast("Saved.", { kind: "good" });
            loadMonth();
          } catch (e) { err.textContent = errText(e); return false; }
        } },
      ],
    });
  }

  // ---- budgets / categories sheet ----
  function openBudgets() {
    const cur = currency();
    const existing = new Map((summary?.budgets || []).map((b) => [b.category, b]));
    const ids = [...new Set([...existing.keys(), ...cats.map((c) => c.id)])];
    const rows = new Map();
    const list = el("div", { class: "sp-bsheet" });
    const err = el("p", { class: "ui-hint is-error sp-err", role: "alert" });
    function addRow(id) {
      if (rows.has(id)) return;
      const b = existing.get(id);
      const [ic, tone] = catStyle(id);
      const input = el("input", { type: "text", inputmode: "decimal", value: b ? centsToInput(b.limit) : "", placeholder: "No limit", autocomplete: "off", "aria-label": `${catLabel(id)} monthly limit in ${cur}` });
      const row = el("div", { class: "sp-brow", "data-tone": tone }, ui.tile(ic, tone, "sm"), el("span", { class: "sp-brow__name grow ui-truncate" }, catLabel(id)), input);
      rows.set(id, { input, had: !!b });
      list.append(row);
    }
    ids.forEach(addRow);
    const newCat = el("input", { type: "text", maxlength: "40", placeholder: "New category, e.g. pets", autocomplete: "off", autocapitalize: "none", "aria-label": "New category name" });
    const addCat = ui.button({ label: "Add", icon: "plus", variant: "tonal", onClick: () => {
      const id = newCat.value.trim().toLowerCase();
      if (!id) return;
      if (!cats.some((c) => c.id === id)) cats = [...cats, { id, label: cap(id) }];
      addRow(id); newCat.value = ""; rows.get(id).input.focus();
    } });
    const body = el("div", { class: "ui-stack" },
      el("p", { class: "sp-sub" }, `Set a monthly limit in ${cur} per category. Leave it empty for no limit. A category you add here is remembered once you use it.`),
      list, el("div", { class: "sp-addcat" }, newCat, addCat), err);
    ui.sheet({
      title: "Budgets and categories", content: body,
      actions: [{ label: "Save budgets", variant: "primary", icon: "check", onClick: async () => {
        const calls = [];
        for (const [id, r] of rows) {
          const v = r.input.value.trim();
          if (v) {
            const cents = toCents(v);
            if (!cents) { err.textContent = `${catLabel(id)}: enter an amount above zero, or leave it empty.`; return false; }
            if (!existing.has(id) || existing.get(id).limit !== cents) calls.push(api("/api/expenses/budgets", { method: "POST", json: { category: id, limit: cents, currency: cur } }));
          } else if (r.had) {
            calls.push(api(`/api/expenses/budgets/${encodeURIComponent(id)}?currency=${cur}`, { method: "DELETE" }));
          }
        }
        if (!calls.length) return;
        try { await Promise.all(calls); ui.toast("Budgets saved.", { kind: "good" }); loadMonth(); }
        catch (e) { err.textContent = errText(e); return false; }
      } }],
    });
  }

  // ---- capture settings ----
  async function loadSettings() {
    const r = await softFail(api("/api/expenses/capture/settings"));
    if (r.ok) settings = r.v;
    renderCaptureCard(r.ok ? null : r.e);
  }
  function renderCaptureCard(error) {
    const on = settings?.enabled !== false;
    const sub = error ? "Couldn't read the capture settings."
      : !settings ? "Checking…"
        : on ? `On${settings.auto_confirm_after ? `, files known merchants after ${settings.auto_confirm_after} matching confirmations` : ""}` : "Off. Payments from your phone are ignored";
    captureSlot.replaceChildren(el("section", { class: "ui-card sp-capture", "aria-label": "Automatic capture" },
      ui.tile("phone", on ? "good" : "gray"),
      el("div", { class: "grow sp-capture__body" }, el("h2", { class: "ui-card__title" }, "Automatic capture"), el("p", { class: "sp-sub" }, sub)),
      ui.button({ label: "Settings", variant: "tonal", size: "sm", onClick: openCaptureSettings })));
  }

  async function openCaptureSettings() {
    const content = el("div", { class: "ui-stack" }, ui.skeleton("lines", 4));
    const sheet = ui.sheet({ title: "Automatic capture", content });
    if (!settings) { await loadSettings(); }
    if (!settings) { content.replaceChildren(ui.notice("Couldn't read the capture settings.", { tone: "bad" })); return; }
    const st = { enabled: settings.enabled !== false, after: Number(settings.auto_confirm_after) || 0, ignore: [...(settings.ignore_merchants || [])] };
    const err = el("p", { class: "ui-hint is-error sp-err", role: "alert" });
    const sw = ui.switchControl({ checked: st.enabled, label: "Capture payments", onChange: (v) => { st.enabled = v; } });
    const after = el("input", { type: "number", min: "0", max: "50", inputmode: "numeric", value: String(st.after), "aria-label": "Auto-file after this many confirmations" });
    const chipsBox = el("div", { class: "sp-chips" });
    const paintIgnore = () => {
      chipsBox.replaceChildren(...(st.ignore.length ? st.ignore.map((n) => {
        const b = el("button", { type: "button", class: "ui-chip", "aria-label": `Stop ignoring ${n}` }, n, ui.icon("x"));
        b.addEventListener("click", () => { st.ignore = st.ignore.filter((o) => o !== n); paintIgnore(); });
        return b;
      }) : [el("span", { class: "sp-sub" }, "None. Every payment that looks like a spend is listed.")]));
    };
    paintIgnore();
    const ignoreIn = el("input", { type: "text", maxlength: "60", placeholder: "e.g. Amit Kumar", autocomplete: "off", "aria-label": "Merchant to ignore" });
    const addIgnore = () => {
      const v = ignoreIn.value.trim();
      if (v && !st.ignore.some((o) => o.toLowerCase() === v.toLowerCase()) && st.ignore.length < 100) st.ignore.push(v);
      ignoreIn.value = ""; paintIgnore();
    };
    ignoreIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addIgnore(); } });
    const link = el("button", { type: "button", class: "ui-link sp-setup-link" }, "Set up the Android app", ui.icon("chevron-right"));
    link.addEventListener("click", () => { sheet.close(); ctx.open("settings", "capture"); });
    content.replaceChildren(
      el("div", { class: "sp-status" }, ui.tile("phone", "info", "sm"),
        el("div", { class: "grow" }, el("strong", {}, "Capture works through the Android app"),
          el("p", { class: "sp-sub" }, "It forwards UPI notifications and bank texts that look like payments. Nothing is read from your messages beyond that, and OTPs and refunds are skipped."), link)),
      el("div", { class: "sp-setting" }, el("div", { class: "grow" }, el("strong", {}, "Capture payments"), el("p", { class: "sp-sub" }, "When off, nothing from your phone is stored.")), sw),
      field("Auto-file after N confirmations", after, "0 turns this off. With 3, a merchant you confirmed 3 times into one category is filed without asking. You can still fix it later."),
      el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Ignore these merchants"), chipsBox,
        el("div", { class: "sp-addcat" }, ignoreIn, ui.button({ label: "Add", icon: "plus", variant: "tonal", onClick: addIgnore })),
        el("span", { class: "ui-hint" }, "Payments to them are dropped quietly. Matching works on the name, so “Swiggy” also covers swiggy@ybl.")),
      err);
    sheet.setActions([{ label: "Save", icon: "check", variant: "primary", onClick: async () => {
      addIgnore();
      const n = Number(after.value);
      if (!Number.isInteger(n) || n < 0 || n > 50) { err.textContent = "Use a whole number from 0 to 50."; return false; }
      try {
        settings = await api("/api/expenses/capture/settings", { method: "POST", json: { enabled: st.enabled, auto_confirm_after: n, ignore_merchants: st.ignore } });
        ui.toast("Capture settings saved.", { kind: "good" });
        renderCaptureCard();
      } catch (e) { err.textContent = errText(e); return false; }
    } }]);
  }

  // ---- public hooks ----
  function startPoll() {
    stopPoll();
    poll = setInterval(() => { if (visible && !document.hidden) loadPending(); }, 45000);
  }
  function stopPoll() { clearInterval(poll); poll = null; }

  async function findExpense(id) {
    let hit = (expenses || []).find((x) => x.id === id);
    if (hit) return hit;
    for (let i = 0; i < 6; i += 1) {
      const key = addMonths(thisMonth, -i);
      if (key === month) continue;
      const r = await softFail(api(`/api/expenses?month=${key}`));
      hit = r.ok && (r.v.expenses || []).find((x) => x.id === id);
      if (hit) { setMonth(key); return hit; }
    }
    return null;
  }

  paintMonthPicker();
  buildAdd();
  renderCaptureCard();
  ui.pullToRefresh?.(container, refresh);
  (async () => { await loadCats(); await refresh(); loadSettings(); })();

  return {
    show() { visible = true; startPoll(); if (!firstPaint) { loadPending(); loadMonth(); } },
    hide() { visible = false; stopPoll(); },
    refresh() { return refresh(); },
    async openSection(name) {
      if (name === "pending") {
        await loadPending();
        if (pending.length) pendingSlot.scrollIntoView({ block: "start", behavior: "smooth" });
        else { window.scrollTo?.({ top: 0 }); ui.toast("Nothing is waiting for a category.", { icon: "check" }); }
      } else if (name === "add") {
        addSlot.scrollIntoView({ block: "center", behavior: "smooth" });
        setTimeout(() => addSlot.input?.focus(), 250);
      } else if (name === "budgets") {
        if (!summary) await loadMonth();
        openBudgets();
      } else if (name === "capture" || name === "settings") openCaptureSettings();
    },
    async openItem(id) {
      if (!id) return;
      const p = pending.find((i) => i.id === id);
      if (p) { pendingSlot.scrollIntoView({ block: "start", behavior: "smooth" }); return; }
      const x = await findExpense(id);
      if (x) { editExpense(x); return; }
      ui.toast("That expense is no longer there.", { kind: "warn" });
    },
  };
}
