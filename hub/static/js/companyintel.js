// Company brief: a one-page brief about a company from PUBLIC sources only (its own website, a public news RSS search, the postings the hub
// already holds), red flags, and "who do I know there" from your own contacts. Saved on this laptop for 7 days. LinkedIn, Glassdoor and other
// sites that forbid bots are never read. Nothing is sent: a referral draft is copied and sent by you. The screen makes ONE data request when
// it opens (GET /api/companyintel); every other request happens only when you tap a button, and nothing polls.
// Routes: docs/api/companyintel.md and networking.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/winoffer.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const field = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "300", ...attrs }); i.value = value ?? ""; return i; };
const list = (items) => el("ul", { class: "wo-list" }, ...items.map((t) => el("li", {}, t)));
const safeLink = (url, label) => /^https?:\/\//i.test(url || "") ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label) : el("span", {}, label);
async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied. Paste it where you want to send it.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "wo ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", brief = null, warm = null, form = { application_id: "", company: "", site: "", ai: false };

  async function load() {
    try { data = await api("/api/companyintel"); error = ""; form.ai = form.ai || data.settings.ai; } catch (e) { error = e.message; }
    paint();
  }
  const body = () => form.application_id ? { application_id: form.application_id, ...(form.site.trim() ? { site: form.site } : {}) } : { company: form.company, site: form.site };

  function paint() {
    root.replaceChildren();
    if (error) { root.append(ui.notice(error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load })); return; }
    if (!data) { root.append(ui.skeleton("list", 3)); return; }
    root.append(ui.notice("Public pages only: the company's own site and its own news feed, and postings already in the hub. LinkedIn, Glassdoor and similar sites are never read. Nothing is sent.", { tone: "info" }),
      picker(), brief ? briefCards() : null, brief ? warmSection() : null, saved());
    ui.renumberEyebrows(root);
  }

  function picker() {
    const app = el("select", { class: "ui-input" }, el("option", { value: "" }, "Type a company instead"),
      ...data.applications.map((a) => el("option", { value: a.id }, [a.title, a.company].filter(Boolean).join(" at ") || a.id)));
    app.value = form.application_id;
    app.addEventListener("change", () => { form.application_id = app.value; brief = null; warm = null; paint(); });
    const nodes = [field("Application", app)];
    const company = input(form.company, { placeholder: "Company name", maxlength: "120" }), site = input(form.site, { placeholder: "https://www.example.com (the company's own website)", inputmode: "url" });
    company.addEventListener("change", () => { form.company = company.value; });
    site.addEventListener("change", () => { form.site = site.value; });
    if (!form.application_id) nodes.push(field("Company", company));
    nodes.push(field("Company website", site, "Optional but best: its about, careers and blog pages are read from here. Nothing is guessed."));
    const ai = ui.switchControl({ checked: form.ai, label: "Summarise with AI", onChange: (v) => { form.ai = v; } });
    nodes.push(el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Summarise with AI (optional)"),
      el("span", { class: "ui-row__sub" }, "Only the public text that was read is sent. Off, a plain summary is built by code.")), ai));
    const go = ui.button({ label: "Build the brief", icon: "search", variant: "primary", onClick: () => ui.busy(go, async () => {
      form.company = company.value; form.site = site.value;
      if (!form.application_id && !form.company.trim()) { ui.toast("Type a company or pick an application.", { kind: "warn" }); return; }
      try { brief = await api("/api/companyintel/brief", { method: "POST", json: { ...body(), ai: form.ai } }); warm = null; data = await api("/api/companyintel"); paint(); } catch (e) { fail(e); }
    }) });
    const again = ui.button({ label: "Read again", icon: "refresh", variant: "tonal", onClick: () => ui.busy(again, async () => {
      try { brief = await api("/api/companyintel/brief", { method: "POST", json: { ...body(), ai: form.ai, refresh: true } }); paint(); } catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", "Which company"), ...nodes, el("div", { class: "wo-actions" }, go, brief ? again : null));
  }

  function briefCards() {
    const b = brief, h = b.hints || {};
    const cards = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", b.company), el("p", { class: "wo-text" }, b.summary),
      el("p", { class: "wo-note" }, `Written by ${b.summary_by}. ${b.cached ? "Saved copy, " : "Read now, "}${b.generated_at.slice(0, 10)}. Kept for ${b.expires_in_days} days.`),
      b.ai_note ? el("p", { class: "wo-note" }, b.ai_note) : null,
      ...b.red_flags.map((f) => el("div", { class: "wo-flag" + (f.severity === "info" ? " is-info" : "") }, f.url ? safeLink(f.url, f.text) : f.text)))];
    const facts = [h.founded ? `Founded: ${h.founded}` : "", h.place ? `Place: ${h.place}` : "", ...(h.size || []).map((s) => "Size: " + s), ...(h.funding || []).map((s) => "Funding: " + s)].filter(Boolean);
    if (facts.length) cards.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "wo-h" }, "Size and funding hints"), list(facts), el("p", { class: "wo-note" }, "Quoted from the company's own pages. Not checked: it can be old or marketing.")));
    if (b.news.length) cards.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "wo-h" }, "Recent news"), el("ul", { class: "wo-list" }, ...b.news.map((n) => el("li", {}, safeLink(n.url, n.title), el("span", { class: "wo-note" }, ` ${[n.source, n.date].filter(Boolean).join(", ")}`))))));
    if (b.pages.length) cards.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "wo-h" }, "Pages read"), el("ul", { class: "wo-list" }, ...b.pages.map((p) => el("li", {}, safeLink(p.url, `${p.kind}: ${p.title || p.url}`), p.snippet ? el("span", { class: "wo-note" }, " - " + p.snippet) : null)))));
    const p = b.postings;
    if (p.count || b.tracker.length) cards.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "wo-h" }, "In your hub already"),
      p.count ? el("p", { class: "wo-text" }, `${p.count} public posting(s) from this company` + (p.oldest_days != null ? `, the oldest ${p.oldest_days} days old.` : ".")) : null,
      p.rows.length ? list(p.rows.map((r) => `${r.title}${r.age_days != null ? ` (${r.age_days} days)` : ""}${r.pay ? ", " + r.pay : ""}`)) : null,
      b.tracker.length ? list(b.tracker.map((t) => `Your application: ${t.role} - ${t.status}`)) : null));
    const small = [...b.notes, ...b.refused.map((r) => `Not read: ${r.url} (${r.why})`), ...b.sources.filter((s) => !s.ok).map((s) => `Could not read ${s.url}: ${s.note}`)];
    cards.push(el("section", { class: "ui-card ui-stack" }, el("p", { class: "wo-note" }, b.disclaimer), small.length ? list(small) : null));
    return el("div", { class: "ui-stack ui-stack--lg" }, ...cards);
  }

  // ---- who do I know there ----
  function warmSection() {
    const find = ui.button({ label: "Who do I know here?", icon: "users", variant: "primary", onClick: () => ui.busy(find, async () => {
      try { warm = await api("/api/networking/warm-path", { method: "POST", json: { company: brief.company, ...(form.application_id ? { application_id: form.application_id } : {}) } }); paint(); } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Who do I know here"),
      el("p", { class: "wo-note" }, "Matched from your own contacts (company, past companies, how you know them) and your tracker. Add contacts in Apps > Networking."), find)];
    if (warm) {
      const sec = out[0];
      warm.tips.forEach((t) => sec.append(ui.notice(t, { tone: "info" })));
      if (warm.tracker.length) sec.append(list(warm.tracker.map((t) => `Tracker: ${t.role} - ${t.status}`)));
      warm.candidates.forEach((c, i) => sec.append(el("div", { class: "wo-row" }, el("p", { class: "wo-text" }, el("b", {}, c.contact.name), c.contact.role ? ` - ${c.contact.role}` : "", i === 0 ? "  (best person to ask)" : ""),
        el("p", { class: "wo-note" }, c.reasons.join("; ") + (c.contact.how_known ? `. You know them: ${c.contact.how_known}` : "")))));
      if (warm.candidates.length) {
        const draft = ui.button({ label: "Draft a referral request to " + warm.candidates[0].contact.name.split(" ")[0], icon: "pen", variant: "tonal", onClick: () => ui.busy(draft, async () => {
          try { warm = await api("/api/networking/warm-path", { method: "POST", json: { company: brief.company, draft: true, ...(form.application_id ? { application_id: form.application_id } : {}) } }); paint(); } catch (e) { fail(e); }
        }) });
        sec.append(draft);
      }
      if (warm.draft) {
        sec.append(el("p", { class: "wo-script" }, warm.draft.text));
        if (warm.draft.blocked) sec.append(ui.notice((warm.draft.blockers || []).join(" "), { tone: "warn" }));
        sec.append(el("p", { class: "wo-note" }, "Draft only. Copy it and send it yourself. It is also in Networking."),
          ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", disabled: !!warm.draft.blocked, onClick: () => copy(warm.draft.text) }));
      }
    }
    return out[0];
  }

  function saved() {
    if (!data.briefs.length) return null;
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("04", "Saved briefs"), ...data.briefs.map((b) => el("div", { class: "wo-row" }, el("p", { class: "wo-text" }, b.company),
      el("p", { class: "wo-note" }, `${b.days_left} day(s) left`),
      el("div", { class: "wo-actions" }, ui.button({ label: "Open", icon: "book-open", variant: "tonal", size: "sm", onClick: async () => {
        form.application_id = ""; form.company = b.company; form.site = b.site || "";
        try { brief = await api("/api/companyintel/brief", { method: "POST", json: { company: b.company, site: b.site || "" } }); warm = null; paint(); } catch (e) { fail(e); }
      } }), ui.button({ label: "Forget", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
        try { await api("/api/companyintel/cache?company=" + encodeURIComponent(b.company), { method: "DELETE" }); data = await api("/api/companyintel"); paint(); } catch (e) { fail(e); }
      } })))));
  }

  load();
}
