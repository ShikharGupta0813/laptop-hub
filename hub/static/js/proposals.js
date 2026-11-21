// Freelance proposals: a draft built only from facts you confirmed, with three opening lines, and a record of which opening wins more.
// COPY ONLY: you copy the text and submit it on the platform yourself; the hub never submits or automates a platform. You tell the screen
// which opening you used and whether it won, lost or got no reply. The screen makes ONE data request when it opens (GET /api/proposals);
// every other request happens only when you tap a button, and nothing polls. Routes: docs/api/proposals.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/winoffer.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const field = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "300", ...attrs }); i.value = value ?? ""; return i; };
const area = (value, rows, attrs = {}) => { const t = el("textarea", { class: "ui-input wo-area", rows: String(rows), ...attrs }); t.value = value ?? ""; return t; };
const list = (items) => el("ul", { class: "wo-list" }, ...items.map((t) => el("li", {}, t)));
async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied. Paste it on the platform yourself.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "wo ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", draft = null, chosen = "";
  let form = { item_id: "", title: "", company: "", description: "", rate: "", currency: "", unit: "", timeline: "", name: "" };

  async function load() {
    try { data = await api("/api/proposals"); error = ""; } catch (e) { error = e.message; }
    paint();
  }

  function paint() {
    root.replaceChildren();
    if (error) { root.append(ui.notice(error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load })); return; }
    if (!data) { root.append(ui.skeleton("list", 3)); return; }
    root.append(ui.notice("Copy only. You paste the proposal on the platform and submit it yourself. The hub never submits, and the freelance platforms are never automated.", { tone: "info" }),
      formCard(), draft ? draftCard() : null, statsCard(), historyCard());
    ui.renumberEyebrows(root);
  }

  function formCard() {
    const task = el("select", { class: "ui-input" }, el("option", { value: "" }, "Paste a task instead"), ...data.tasks.map((t) => el("option", { value: t.id }, [t.title, t.company].filter(Boolean).join(" - ") + (t.pay ? ` (${t.pay})` : ""))));
    task.value = form.item_id; task.addEventListener("change", () => { form.item_id = task.value; draft = null; paint(); });
    const f = { title: input(form.title, { placeholder: "Task title" }), company: input(form.company, { placeholder: "Client (optional)" }), description: area(form.description, 5, { placeholder: "Paste the task description" }),
      rate: input(form.rate, { type: "number", min: "0", step: "any", inputmode: "decimal" }), currency: input(form.currency, { maxlength: "3", placeholder: "USD" }), unit: input(form.unit, { maxlength: "20", placeholder: "per hour, fixed price" }),
      timeline: input(form.timeline, { maxlength: "80", placeholder: "about two weeks" }), name: input(form.name, { maxlength: "60", placeholder: "Your name for the sign-off" }) };
    const go = ui.button({ label: "Draft the proposal", icon: "pen", variant: "primary", onClick: () => ui.busy(go, async () => {
      form = { item_id: task.value, title: f.title.value, company: f.company.value, description: f.description.value, rate: f.rate.value, currency: f.currency.value, unit: f.unit.value, timeline: f.timeline.value, name: f.name.value };
      const json = form.item_id ? { item_id: form.item_id } : { title: form.title, company: form.company, description: form.description };
      Object.assign(json, { rate: form.rate, currency: form.currency, unit: form.unit, timeline: form.timeline, name: form.name });
      try { draft = await api("/api/proposals/draft", { method: "POST", json }); chosen = draft.opening; data = await api("/api/proposals"); paint(); } catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", "The task"), field("Task from your sources", task),
      ...(form.item_id ? [] : [field("Title", f.title), field("Client", f.company), field("Description", f.description)]),
      el("p", { class: "wo-note" }, "Your rate and timeline are yours to type. If you leave them empty the draft shows a blank to fill and Copy stays off. Nothing is guessed."),
      field("Your rate", f.rate), field("Currency", f.currency), field("How it is charged", f.unit), field("Timeline", f.timeline), field("Name", f.name), go);
  }

  function draftCard() {
    const alts = draft.openings, cur = alts.find((o) => o.key === chosen) || alts[0];
    const seg = ui.segmented({ options: alts.map((o) => ({ value: o.key, label: o.label })), value: cur.key, label: "Opening line", onChange: (v) => { chosen = v; paint(); } });
    const used = ui.button({ label: "I pasted this one", icon: "check", variant: "tonal", size: "sm", disabled: cur.blocked, onClick: async () => {
      try { await api(`/api/proposals/${draft.id}/used`, { method: "POST", json: { opening: cur.key } }); ui.toast("Noted. Tell me later whether it won.", { kind: "good" }); data = await api("/api/proposals"); paint(); } catch (e) { fail(e); }
    } });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Your draft"), el("p", { class: "wo-note" }, "Pick the opening line you want to try. Each version is a full proposal."), seg,
      el("p", { class: "wo-script" }, cur.text),
      cur.blocked ? ui.notice(cur.blockers.join(" "), { tone: "warn" }) : null,
      draft.no_facts ? ui.notice("There are no confirmed facts yet, so the draft has no proof line. Add your resume in Apps > Opportunities > Profile.", { tone: "info" }) : null,
      el("p", { class: "wo-note" }, draft.note),
      el("div", { class: "wo-actions" }, ui.button({ label: "Copy", icon: "copy", variant: "primary", size: "sm", disabled: cur.blocked, onClick: () => copy(cur.text) }), used));
  }

  function statsCard() {
    const s = data.stats;
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Which opening wins more"), el("p", { class: "wo-text" }, s.verdict),
      ...s.openings.map((o) => el("div", { class: "wo-row" }, el("p", { class: "wo-text" }, el("b", {}, o.label), ` - used ${o.used}, decided ${o.decided}`),
        el("div", { class: "wo-bar", "aria-hidden": "true" }, el("span", { style: `width:${o.win_rate ?? 0}%` })),
        el("p", { class: "wo-note" }, o.decided ? `Won ${o.won}, lost ${o.lost}, no reply ${o.no_reply}` + (o.win_rate != null ? `: ${o.win_rate} percent won` + (o.enough ? "" : " (too few to compare)") : "") : "No decided bids yet"))));
  }

  function historyCard() {
    const rows = data.proposals;
    const outcome = async (p, value) => { try { await api(`/api/proposals/${p.id}/outcome`, { method: "POST", json: { outcome: value } }); data = await api("/api/proposals"); paint(); } catch (e) { fail(e); } };
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("04", "My proposals"), rows.length ? el("div", { class: "ui-stack" }, ...rows.map((p) => el("div", { class: "wo-row" },
      el("p", { class: "wo-text" }, el("b", {}, p.title || "Task"), p.company ? ` - ${p.company}` : ""),
      el("p", { class: "wo-note" }, [p.created.slice(0, 10), p.used ? `used: ${p.openings.find((o) => o.key === p.opening)?.label || p.opening}` : "not marked as used", p.outcome ? `result: ${p.outcome.replace("_", " ")}` : ""].filter(Boolean).join(" - ")),
      el("div", { class: "wo-actions" },
        p.used && !p.outcome ? ui.button({ label: "Won", icon: "check", variant: "tonal", size: "sm", onClick: () => outcome(p, "won") }) : null,
        p.used && !p.outcome ? ui.button({ label: "Lost", icon: "x", variant: "tonal", size: "sm", onClick: () => outcome(p, "lost") }) : null,
        p.used && !p.outcome ? ui.button({ label: "No reply", icon: "clock", variant: "tonal", size: "sm", onClick: () => outcome(p, "no_reply") }) : null,
        ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: async () => { try { await api(`/api/proposals/${p.id}`, { method: "DELETE" }); data = await api("/api/proposals"); paint(); } catch (e) { fail(e); } } }))))) : el("p", { class: "wo-note" }, "None yet."));
  }

  load();
}
