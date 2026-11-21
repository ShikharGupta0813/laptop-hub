// Offers and negotiation: record job offers, compare up to four side by side (India in-hand ESTIMATE from assumptions you can change), draft a
// counter-offer from YOUR numbers, check contract clauses, and check a posting's pay against your target. Everything is typed by you; the
// hub never guesses market pay. Drafts only: nothing is sent. The screen makes ONE data request when it opens (GET /api/offers); every other
// request happens only when you tap a button, and nothing polls. Routes: docs/api/offers.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/winoffer.css";
const TABS = [["offers", "Offers"], ["compare", "Compare"], ["counter", "Counter"], ["clauses", "Clauses"], ["pay", "Pay check"], ["assume", "Assumptions"]];
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const field = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "200", ...attrs }); i.value = value ?? ""; return i; };
const num = (value, attrs = {}) => input(value, { type: "number", min: "0", step: "any", inputmode: "decimal", ...attrs });
const area = (value, rows, attrs = {}) => { const t = el("textarea", { class: "ui-input wo-area", rows: String(rows), ...attrs }); t.value = value ?? ""; return t; };
const list = (items) => el("ul", { class: "wo-list" }, ...items.map((t) => el("li", {}, t)));
const money = (v, cur) => (v == null || v === "" ? "-" : `${cur && cur !== "INR" ? cur + " " : ""}${Number(v).toLocaleString("en-IN")}`);
const lakh = (v) => (v == null ? "" : ` (${(Number(v) / 100000).toFixed(2)} lakh)`);
async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied. Paste it where you want to send it.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "wo ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", tab = "offers", cmp = null, counter = null, pay = null;
  const picked = new Set(), competing = new Set();
  let cf = { offer: "", ask_fixed: "", ask_variable: "", ask_joining: "", notice: "", start: "", other: "", reasons: "", recruiter: "", owner: "", channel: "email", name: false };
  let pf = { title: "", company: "", description: "", pay_text: "" };
  let clauseOffer = "";

  async function load() {
    try { data = await api("/api/offers"); error = ""; } catch (e) { error = e.message; }
    paint();
  }
  const offer = (id) => data.offers.find((o) => o.id === id);

  function paint() {
    root.replaceChildren();
    if (error) { root.append(ui.notice(error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load })); return; }
    if (!data) { root.append(ui.skeleton("list", 3)); return; }
    root.append(ui.notice("You type every number. In-hand figures are ESTIMATES from the assumptions on the last tab. Drafts only: nothing is sent.", { tone: "info" }),
      ui.segmented({ options: TABS.map(([value, label]) => ({ value, label })), value: tab, label: "Offer sections", onChange: (v) => { tab = v; paint(); } }),
      { offers: offersTab, compare: compareTab, counter: counterTab, clauses: clausesTab, pay: payTab, assume: assumeTab }[tab]());
    ui.renumberEyebrows(root);
  }

  // ---- the offers ----
  const FIELDS = [["company", "Company", "text"], ["role", "Role", "text"], ["currency", "Currency", "text", "INR"], ["fixed", "Fixed pay per year (includes employer PF if it is CTC)", "num"],
    ["variable", "Variable pay per year (target)", "num"], ["variable_payout_pct", "Expected payout, percent (blank: your assumption)", "num"], ["bonus", "Annual bonus", "num"], ["joining_bonus", "Joining bonus (once)", "num"],
    ["benefits_value", "Value of benefits per year (insurance etc.), if you know it", "num"], ["notice_days", "Notice period, days", "num"], ["notice_buyout_days", "Notice days you would buy out", "num"],
    ["probation_months", "Probation, months", "num"], ["location", "Location", "text"], ["work_mode", "Remote, hybrid or onsite", "text"], ["start_date", "Start date (YYYY-MM-DD)", "text"],
    ["reply_by", "Reply by (YYYY-MM-DD)", "text"], ["relocation_cost", "Relocation cost to you", "num"], ["relocation_support", "Relocation money the company gives", "num"],
    ["recruiter", "Recruiter's name", "text"], ["benefits", "Benefits (words)", "text"], ["notes", "Notes", "text"]];
  function offerSheet(o) {
    const cur = o || { currency: "INR", esop: {} };
    const ctl = {};
    for (const [k, label, kind] of FIELDS) ctl[k] = (kind === "num" ? num : input)(cur[k] ?? "", { "aria-label": label });
    const esop = { grant_value: num(cur.esop?.grant_value ?? ""), vesting_years: num(cur.esop?.vesting_years ?? ""), cliff_months: num(cur.esop?.cliff_months ?? "") };
    const covered = ui.switchControl({ checked: !!cur.buyout_covered, label: "The company pays my notice buy-out" });
    ui.sheet({ title: o ? "Edit offer" : "Add an offer", content: el("div", { class: "ui-stack" },
      el("p", { class: "wo-note" }, "Only what the offer letter or the recruiter told you. Leave the rest empty: an empty part counts as zero and is never guessed."),
      ...FIELDS.map(([k, label]) => field(label, ctl[k])),
      field("ESOP grant value (total)", esop.grant_value, "The paper value. It may be worth nothing, so it is not counted in cash unless you say so in Assumptions."), field("ESOP vesting years", esop.vesting_years), field("ESOP cliff, months", esop.cliff_months),
      el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Notice buy-out paid by the company")), covered)),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const json = Object.fromEntries(FIELDS.map(([k]) => [k, ctl[k].value]));
      json.esop = { grant_value: esop.grant_value.value, vesting_years: esop.vesting_years.value, cliff_months: esop.cliff_months.value };
      json.buyout_covered = covered.input.checked;
      if (o) json.clauses = o.clauses;
      try { await api(o ? `/api/offers/${o.id}` : "/api/offers", { method: o ? "PUT" : "POST", json }); ui.toast("Saved on this laptop.", { kind: "good" }); await load(); return true; } catch (e) { fail(e); return false; }
    } }] });
  }

  function offersTab() {
    const add = ui.button({ label: "Add an offer", icon: "plus", variant: "primary", onClick: () => offerSheet(null) });
    const cards = data.offers.map((o) => {
      const e = o.estimate, t = o.totals;
      return el("div", { class: "wo-row" }, el("p", { class: "wo-text" }, el("b", {}, [o.role, o.company].filter(Boolean).join(" at ")), "  ", ui.badge(o.status, o.status === "accepted" ? "good" : "gray")),
        el("p", { class: "wo-note" }, [o.location, o.work_mode, o.notice_days ? `${o.notice_days} days notice` : "", o.start_date ? "start " + o.start_date : "", o.reply_by ? "reply by " + o.reply_by : ""].filter(Boolean).join(" - ")),
        el("div", { class: "wo-grid" },
          el("p", { class: "wo-text" }, `Fixed: ${money(o.fixed, o.currency)}` + (o.currency === "INR" ? lakh(o.fixed) : "")),
          el("p", { class: "wo-text" }, `Cash value per year: ${money(t.annual_recurring, o.currency)}`),
          e ? el("p", { class: "wo-text" }, `In hand per month (estimate): ${money(e.monthly_in_hand)}`) : el("p", { class: "wo-note" }, o.currency === "INR" ? "No in-hand estimate: add the fixed pay." : "In-hand estimates are for INR offers only."),
          o.variable ? el("p", { class: "wo-text" }, `Variable, expected value: ${money(t.variable_expected, o.currency)}`) : null,
          o.esop_per_year ? el("p", { class: "wo-text" }, `ESOP paper value per year: ${money(o.esop_per_year, o.currency)} (not counted)`) : null,
          t.buyout_cost != null && t.buyout_cost > 0 ? el("p", { class: "wo-text" }, `Notice buy-out to you: ${money(t.buyout_cost, o.currency)}`) : null),
        e ? el("p", { class: "wo-note" }, `${e.regime === "new" ? "New" : "Old"} tax regime, tax ${money(e.income_tax)} a year. ${e.label}`) : null,
        el("div", { class: "wo-actions" }, ui.button({ label: "Edit", icon: "edit", variant: "tonal", size: "sm", onClick: () => offerSheet(o) }),
          ui.button({ label: "Counter", icon: "pen", variant: "tonal", size: "sm", onClick: () => { cf.offer = o.id; tab = "counter"; counter = null; paint(); } }),
          ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
            if (await ui.confirm({ title: "Delete this offer?", message: o.company, confirmLabel: "Delete", danger: true })) { try { await api(`/api/offers/${o.id}`, { method: "DELETE" }); load(); } catch (er) { fail(er); } }
          } })));
    });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "My offers"), add, cards.length ? el("div", { class: "ui-stack" }, ...cards) : el("p", { class: "wo-note" }, "No offers recorded yet."));
  }

  // ---- compare ----
  function compareTab() {
    const boxes = data.offers.map((o) => {
      const box = el("input", { type: "checkbox", "aria-label": `Compare ${o.company}` }); box.checked = picked.has(o.id);
      box.addEventListener("change", () => { if (box.checked) { if (picked.size >= data.max_compare) { box.checked = false; ui.toast(`Compare up to ${data.max_compare} offers.`, { kind: "warn" }); return; } picked.add(o.id); } else picked.delete(o.id); });
      return el("label", { class: "wo-check" }, box, el("span", {}, `${o.role} at ${o.company}`));
    });
    const go = ui.button({ label: "Compare side by side", icon: "list-checks", variant: "primary", onClick: () => ui.busy(go, async () => {
      try { cmp = await api("/api/offers/compare", { method: "POST", json: { ids: [...picked] } }); paint(); } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Pick 2 to 4 offers"), boxes.length ? el("div", { class: "ui-stack" }, ...boxes) : el("p", { class: "wo-note" }, "Record offers first."), go)];
    if (cmp) {
      const fmt = (r, v) => (v === null || v === undefined || v === "" ? "-" : r.numeric ? money(v, cmp.offers[0].currency) : String(v));
      const table = el("table", { class: "wo-table" }, el("thead", {}, el("tr", {}, el("th", {}, ""), ...cmp.offers.map((o) => el("th", {}, `${o.company} (${o.role})`)))),
        el("tbody", {}, ...cmp.rows.filter((r) => r.values.some((v) => v !== null && v !== "")).map((r) => el("tr", {}, el("td", {}, r.label), ...r.values.map((v, i) => el("td", { class: r.best.includes(i) ? "is-best" : "" }, fmt(r, v)))))));
      out.push(el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Side by side"), el("div", { class: "wo-scroll" }, table), list(cmp.notes),
        el("p", { class: "wo-note" }, `Tax rules: ${cmp.rules}`)));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out);
  }

  // ---- counter-offer ----
  function counterTab() {
    if (!data.offers.length) return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Counter-offer"), el("p", { class: "wo-note" }, "Record an offer first."));
    const sel = el("select", { class: "ui-input" }, ...data.offers.map((o) => el("option", { value: o.id }, `${o.role} at ${o.company}`)));
    sel.value = cf.offer && offer(cf.offer) ? cf.offer : data.offers[0].id; cf.offer = sel.value;
    sel.addEventListener("change", () => { cf.offer = sel.value; counter = null; paint(); });
    const f = { fixed: num(cf.ask_fixed), variable: num(cf.ask_variable), joining: num(cf.ask_joining), notice: num(cf.notice), start: input(cf.start, { placeholder: "YYYY-MM-DD" }), other: input(cf.other, { placeholder: "For example: a review after six months" }),
      reasons: area(cf.reasons, 3, { placeholder: "One reason per line, in your own words. Only true things." }), recruiter: input(cf.recruiter), owner: input(cf.owner) };
    const others = data.offers.filter((o) => o.id !== cf.offer).map((o) => { const box = el("input", { type: "checkbox", "aria-label": `Mention ${o.company}'s offer` }); box.checked = competing.has(o.id);
      box.addEventListener("change", () => { if (box.checked) competing.add(o.id); else competing.delete(o.id); }); return el("label", { class: "wo-check" }, box, el("span", {}, `Mention my offer from ${o.company} (${money(o.fixed, o.currency)} fixed)`)); });
    const name = ui.switchControl({ checked: cf.name, label: "Name the other company" });
    const channel = ui.segmented({ options: [{ value: "email", label: "Email" }, { value: "call", label: "Call script" }], value: cf.channel, label: "Draft type", onChange: (v) => { cf.channel = v; } });
    const go = ui.button({ label: "Draft it", icon: "pen", variant: "primary", onClick: () => ui.busy(go, async () => {
      cf = { ...cf, ask_fixed: f.fixed.value, ask_variable: f.variable.value, ask_joining: f.joining.value, notice: f.notice.value, start: f.start.value, other: f.other.value, reasons: f.reasons.value, recruiter: f.recruiter.value, owner: f.owner.value, name: name.input.checked };
      const ask = {}; if (cf.ask_fixed) ask.fixed = cf.ask_fixed; if (cf.ask_variable) ask.variable = cf.ask_variable; if (cf.ask_joining) ask.joining_bonus = cf.ask_joining;
      if (cf.notice) ask.notice_days = cf.notice; if (cf.start) ask.start_date = cf.start; if (cf.other) ask.other = cf.other;
      try { counter = await api(`/api/offers/${cf.offer}/counter`, { method: "POST", json: { ask, use_competing: [...competing].filter((x) => x !== cf.offer), name_competitor: cf.name, reasons: cf.reasons.split("\n").map((x) => x.trim()).filter(Boolean), recruiter: cf.recruiter, owner_name: cf.owner, channel: cf.channel } }); paint(); } catch (e) { fail(e); }
    }) });
    const tgt = data.pay_target?.target != null ? `Your target on the Interview coach pay tab: ${money(data.pay_target.target, data.pay_target.currency)}.` : "";
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Counter-offer"),
      el("p", { class: "wo-note" }, "Type what you want. Every number in the draft comes from the offer, from what you type here, or from an offer of yours you tick. A number you leave empty becomes a blank to fill. " + tgt),
      field("Which offer", sel), field("Fixed pay you want (per year)", f.fixed), field("Variable pay you want", f.variable), field("Joining bonus you want", f.joining), field("Notice days you can offer", f.notice), field("Start date you can offer", f.start), field("Anything else", f.other),
      field("Your reasons", f.reasons), others.length ? el("div", { class: "ui-stack" }, el("span", { class: "ui-label" }, "Competing offers you really hold"), ...others, el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Name the other company")), name)) : null,
      field("Recruiter's name", f.recruiter), field("Your name for the signature", f.owner), channel, go)];
    if (counter) {
      out.push(el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", counter.channel === "call" ? "Call script" : "Email draft"), counter.subject ? el("p", { class: "wo-text" }, el("b", {}, "Subject: " + counter.subject)) : null,
        el("p", { class: "wo-script" }, counter.text), counter.blocked ? ui.notice("Fill in: " + counter.fills.join(", ") + ". Copy is off until you have filled them in the fields above and drafted again.", { tone: "warn" }) : null,
        el("p", { class: "wo-note" }, counter.note), ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", disabled: counter.blocked, onClick: () => copy(counter.text) })));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out);
  }

  // ---- clauses ----
  function clausesTab() {
    if (!data.offers.length) return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Contract clauses"), el("p", { class: "wo-note" }, "Record an offer first."));
    const sel = el("select", { class: "ui-input" }, ...data.offers.map((o) => el("option", { value: o.id }, `${o.role} at ${o.company}`)));
    sel.value = clauseOffer && offer(clauseOffer) ? clauseOffer : data.offers[0].id; clauseOffer = sel.value;
    sel.addEventListener("change", () => { clauseOffer = sel.value; paint(); });
    const o = offer(clauseOffer), state = { ...(o.clauses || {}) };
    const rows = o.clause_review.map((c) => {
      const s = el("select", { class: "ui-input", "aria-label": c.label }, ...[["unknown", "Not checked"], ["yes", "Yes, it is there"], ["no", "No, it is not there"]].map(([v, l]) => el("option", { value: v }, l)));
      s.value = c.state; s.addEventListener("change", () => { state[c.id] = s.value; });
      return el("div", { class: "wo-row" }, el("p", { class: "wo-text" }, el("b", {}, c.label)), el("p", { class: "wo-note" }, c.risk), field("In the contract?", s), el("p", { class: "wo-note" }, "Ask: " + c.ask));
    });
    const save = ui.button({ label: "Save", icon: "check", variant: "primary", onClick: () => ui.busy(save, async () => {
      try { await api(`/api/offers/${o.id}`, { method: "PUT", json: { company: o.company, role: o.role, clauses: state } }); ui.toast("Saved.", { kind: "good" }); await load(); } catch (e) { fail(e); }
    }) });
    const ask = o.clause_review.filter((c) => c.state === "unknown").map((c) => c.ask);
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Contract clauses"), el("p", { class: "wo-note" }, "Read the offer letter and the contract for each of these before you sign. This is a checklist, not legal advice."), field("Offer", sel), ...rows, save,
      ask.length ? ui.button({ label: "Copy the questions I have not checked", icon: "copy", variant: "tonal", size: "sm", onClick: () => copy(ask.map((q) => "- " + q).join("\n")) }) : null);
  }

  // ---- pay check ----
  function payTab() {
    const f = { title: input(pf.title, { placeholder: "Job title" }), company: input(pf.company, { placeholder: "Company" }), description: area(pf.description, 5, { placeholder: "Paste the job description (the pay line is read from it)" }), pay_text: input(pf.pay_text, { placeholder: "Or just the pay line, such as Rs 18-24 LPA" }) };
    const go = ui.button({ label: "Check the pay", icon: "trend", variant: "primary", onClick: () => ui.busy(go, async () => {
      pf = { title: f.title.value, company: f.company.value, description: f.description.value, pay_text: f.pay_text.value };
      try { pay = await api("/api/offers/pay-check", { method: "POST", json: pf }); paint(); } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Pay check for a posting"),
      el("p", { class: "wo-note" }, "Compares the pay the posting advertises with YOUR target (Interview coach > Pay) and with similar postings already in the hub. Nothing is looked up on the internet."),
      field("Title", f.title), field("Company", f.company), field("Description", f.description), field("Pay line", f.pay_text), go)];
    if (pay) {
      const label = { below: "Below your target", in: "Your target is inside the range", above: "At or above your target", unknown: "Cannot tell" }[pay.verdict];
      out.push(el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Verdict"), el("div", { class: `wo-verdict is-${pay.verdict}` }, label), el("p", { class: "wo-text" }, pay.why),
        pay.verdict_basis ? el("p", { class: "wo-note" }, "Based on: " + pay.verdict_basis) : null,
        list([pay.advertised ? `Advertised: ${money(pay.advertised.min, pay.advertised.currency)} to ${money(pay.advertised.max, pay.advertised.currency)} a year (${pay.advertised.label})` : "Advertised: no range found in the posting",
          pay.target.target != null ? `Your target: ${money(pay.target.target, pay.target.currency)}` + (pay.target.low != null ? `, lowest ${money(pay.target.low)}` : "") + (pay.target.high != null ? `, top ${money(pay.target.high)}` : "") : "Your target: not set",
          pay.similar ? `Similar postings in the hub (${pay.similar.count}): typically ${money(pay.similar.typical_min)} to ${money(pay.similar.typical_max)}, from ${money(pay.similar.lowest)} to ${money(pay.similar.highest)}` : "Similar postings in the hub: none with a pay range"]),
        el("p", { class: "wo-note" }, pay.note)));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out);
  }

  // ---- assumptions ----
  function assumeTab() {
    const a = data.assumptions, ctl = {};
    for (const k of Object.keys(data.defaults)) {
      if (k === "regime") { ctl[k] = el("select", { class: "ui-input" }, el("option", { value: "new" }, "New regime"), el("option", { value: "old" }, "Old regime")); ctl[k].value = a[k]; }
      else if (typeof data.defaults[k] === "boolean") ctl[k] = ui.switchControl({ checked: !!a[k], label: data.labels[k] });
      else ctl[k] = num(a[k] ?? "");
    }
    const save = ui.button({ label: "Save assumptions", icon: "check", variant: "primary", onClick: () => ui.busy(save, async () => {
      const json = {}; for (const [k, c] of Object.entries(ctl)) json[k] = c.input ? c.input.checked : c.value;
      try { await api("/api/offers/assumptions", { method: "PUT", json }); ui.toast("Saved. Estimates use these now.", { kind: "good" }); await load(); } catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Assumptions behind the estimates"),
      el("p", { class: "wo-note" }, `${data.rules} Surcharge, HRA, 80D and other exemptions are not modelled: type them as "other deductions" if you want them counted. Payslips differ.`),
      ...Object.keys(data.defaults).map((k) => ctl[k].input ? el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, data.labels[k])), ctl[k]) : field(data.labels[k], ctl[k])), save);
  }

  load();
}
