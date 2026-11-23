// Job assist: an interview-scheduling reply DRAFT, a recruiter call card and a referral request DRAFT. Nothing here sends, opens a booking
// link or changes a status. You copy a draft and send it from your own mail. Used by the job tracker (Call card, Referral draft) and by the
// Replies card (Offer times); also its own screen (Apps > Interview helper). The screen makes ONE data call when it opens (GET /api/jobassist);
// the sheets make one call each when you tap. Nothing polls. Routes: docs/api/jobassist.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/jobassist.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const field = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);

function css() { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); }
async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied. Paste it into your own mail.", { kind: "good" }); }
  catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}
const list = (items) => el("ul", { class: "ja-list" }, ...items.map((t) => el("li", {}, t)));

// ---- 1. offer times for an interview invite ----
export async function scheduleSheet(suggestion) {
  css();
  let d;
  try { d = await api("/api/jobassist/schedule", { method: "POST", json: { suggestion_id: suggestion.id } }); } catch (e) { fail(e); return; }
  const body = el("textarea", { class: "ui-input ja-draft", "aria-label": "Draft reply", rows: "11" });
  const subject = el("input", { class: "ui-input", type: "text", maxlength: "200", "aria-label": "Subject" });
  if (d.draft) { body.value = d.draft.body; subject.value = d.draft.subject; }
  const actions = d.draft ? [{ label: "Copy reply", variant: "primary", icon: "copy", onClick: () => { copy(body.value); return false; } },
    { label: "Copy subject", variant: "tonal", icon: "copy", onClick: () => { copy(subject.value); return false; } }] : [];
  ui.sheet({ title: `Interview: ${d.company || suggestion.company || "reply"}`, class: "st-sheet", actions, content: el("div", { class: "ui-stack ui-stack--lg ja" },
    el("p", { class: "ui-hint" }, d.message),
    d.link_host ? ui.notice(`Booking link from ${d.link_host}. REDLINE does not open it. Open it from your own mail and pick a time.`, { tone: "info" }) : null,
    d.slots.length ? el("div", { class: "ui-stack" }, ui.eyebrow("", "Your free times"), list(d.slots.map((s) => s.label)), el("p", { class: "ui-hint" }, d.window)) : null,
    ...d.notes.map((n) => el("p", { class: "ja-warn" }, n)),
    d.draft ? field("Subject", subject) : null, d.draft ? field("Draft reply (edit it, copy it, send it yourself)", body) : null,
    el("p", { class: "ui-hint" }, "A draft only. Nothing is sent.")) });
}

// ---- 2. recruiter call card ----
export async function callCardSheet(rec) {
  css();
  const qs = rec.item_id ? `item_id=${encodeURIComponent(rec.item_id)}` : `record_id=${encodeURIComponent(rec.id)}`;
  let d;
  try { d = await api(`/api/jobassist/call-card?${qs}`); } catch (e) { fail(e); return; }
  const ask = el("input", { class: "ui-input", type: "text", maxlength: "120", placeholder: "Example: 18 LPA fixed", "aria-label": "Your salary ask" });
  ask.value = d.salary_ask || "";
  const row = (k, v) => el("div", { class: "ja-row" }, el("span", { class: "ja-k" }, k), el("span", { class: "ja-v" }, v));
  const save = ui.button({ label: "Save my ask", variant: "tonal", size: "sm", icon: "check", onClick: async () => {
    try { await api("/api/jobassist/salary-ask", { method: "PUT", json: { salary_ask: ask.value, record_id: d.record_id } }); ui.toast("Saved on this laptop.", { kind: "good" }); } catch (e) { fail(e); }
  } });
  ui.sheet({ title: `Call card: ${d.company}`, class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg ja" },
    el("h3", { class: "ja-title" }, d.role),
    el("div", { class: "ja-rows" }, row("Company", d.company), row("Came from", d.came_from), row("Status", d.status || "unknown"), d.applied_at ? row("Applied", d.applied_at) : null,
      row("Notice period", d.notice_text), d.their_pay_note ? row("Pay they mentioned", d.their_pay_note) : null,
      d.link ? el("div", { class: "ja-row" }, el("span", { class: "ja-k" }, "Posting"), el("a", { class: "ja-v", href: d.link, target: "_blank", rel: "noopener noreferrer" }, "Open posting")) : null),
    el("div", { class: "ui-stack" }, ui.eyebrow("", "Your salary ask"), field("What you will say if asked", ask, d.salary_ask_set ? "Your own words. Saved only on this laptop." : d.salary_ask_note), save),
    el("div", { class: "ui-stack" }, ui.eyebrow("", "Your strongest points for this job"),
      d.points.length ? list(d.points.map((p) => p.text)) : el("p", { class: "ui-hint" }, "No confirmed fact matched yet."), el("p", { class: "ui-hint" }, d.facts_only)),
    el("div", { class: "ui-stack" }, ui.eyebrow("", "Questions to ask"), list(d.questions)),
    el("div", { class: "ui-stack" }, ui.eyebrow("", "Red flags"), d.red_flags.length ? list(d.red_flags) : el("p", { class: "ui-hint" }, "None found by the plain checks.")),
    ...d.notes.map((n) => el("p", { class: "ja-warn" }, n))) });
}

// ---- 3. referral request draft ----
export async function referralSheet(rec, contactId = "") {
  css();
  let d;
  try { d = await api("/api/jobassist/referral", { method: "POST", json: { record_id: rec.id, contact_id: contactId } }); } catch (e) { fail(e); return; }
  if (!d.draft) { ui.sheet({ title: `Referral: ${d.company}`, content: el("div", { class: "ui-stack ja" }, el("p", {}, d.message)) }); return; }
  const body = el("textarea", { class: "ui-input ja-draft", "aria-label": "Referral request draft", rows: "10" }); body.value = d.draft.body;
  const subject = el("input", { class: "ui-input", type: "text", maxlength: "200", "aria-label": "Subject" }); subject.value = d.draft.subject;
  const asked = d.asked[d.contact_id];
  const who = d.candidates.find((c) => c.id === d.contact_id);
  const pick = d.candidates.length > 1 ? el("select", { class: "ui-input", "aria-label": "Who to ask" }, ...d.candidates.map((c) => el("option", { value: c.id }, `${c.name}: ${c.reasons[0]}`))) : null;
  let sh = null;
  if (pick) { pick.value = d.contact_id; pick.addEventListener("change", () => { sh?.close(); referralSheet(rec, pick.value); }); }
  sh = ui.sheet({ title: `Referral request: ${d.company}`, class: "st-sheet",
    actions: [{ label: "Copy request", variant: "primary", icon: "copy", onClick: () => { if (d.draft.blockers.length) { ui.toast(d.draft.blockers[0], { kind: "warn" }); return false; } copy(body.value); return false; } },
      { label: asked ? `Asked on ${asked}. Undo` : "I asked them", variant: "tonal", icon: "check", onClick: async () => {
        try {
          await api("/api/jobassist/referral/asked", { method: "POST", json: { record_id: rec.id, contact_id: d.contact_id, asked: !asked } });
          ui.toast(asked ? "Marked as not asked." : "Marked as asked. Nothing was sent.", { kind: "good" }); return true;
        } catch (e) { fail(e); return false; }
      } }],
    content: el("div", { class: "ui-stack ui-stack--lg ja" },
      el("p", { class: "ui-hint" }, `To ${who ? who.name : d.draft.to} (${d.draft.why.join(", ")}). A draft only: copy it and send it yourself. "I asked them" just writes today's date down.`),
      pick ? field("Who to ask", pick) : null, ...d.tips.map((t) => el("p", { class: "ja-warn" }, t)),
      d.draft.blockers.length ? el("p", { class: "ja-warn" }, d.draft.blockers.join(" ")) : null, d.draft.note ? el("p", { class: "ja-warn" }, d.draft.note) : null,
      field("Subject", subject), field("Message (3 lines, from your confirmed facts)", body)) });
}

// ---- the screen: Apps > Interview helper ----
export function init(container) {
  css();
  const root = el("div", { class: "ja ja-screen ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "";
  async function load() {
    try { data = await api("/api/jobassist"); error = ""; } catch (e) { error = e.message; }
    paint();
  }
  function paint() {
    root.replaceChildren();
    if (error) { root.append(ui.notice(error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load })); return; }
    if (!data) { root.append(ui.skeleton("list", 3)); return; }
    const ask = el("input", { class: "ui-input", type: "text", maxlength: "120", placeholder: "Example: 18 LPA fixed", "aria-label": "Your default salary ask" }); ask.value = data.salary_ask || "";
    root.append(ui.notice("Drafts and cards only. Nothing is sent, opened or changed from here. Booking links (Calendly and similar) are never opened.", { tone: "info" }),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", "Interview invites to answer"),
        ...(data.invites.length ? data.invites.map((s) => el("div", { class: "ja-item" },
          el("div", { class: "ja-item__text" }, el("strong", {}, s.company || s.sender), el("span", { class: "ja-sub" }, `${s.subject} (${s.date})`),
            s.schedule_host ? ui.badge("Booking link: " + s.schedule_host, "accent") : null),
          ui.button({ label: "Offer times", icon: "calendar", variant: "tonal", size: "sm", onClick: () => scheduleSheet(s) })))
          : [el("p", { class: "ui-hint" }, "No interview invites waiting. They appear here when reply sorting finds one in your mail.")])),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Your applications"),
        ...(data.records.length ? data.records.map((r) => el("div", { class: "ja-item" },
          el("div", { class: "ja-item__text" }, el("strong", {}, r.role), el("span", { class: "ja-sub" }, `${r.company} · ${r.status}${r.referral_asked ? " · referral asked" : ""}`)),
          el("div", { class: "ja-actions" }, ui.button({ label: "Call card", icon: "phone", variant: "tonal", size: "sm", onClick: () => callCardSheet(r) }),
            ui.button({ label: "Referral", icon: "users", variant: "ghost", size: "sm", onClick: () => referralSheet(r) }))))
          : [el("p", { class: "ui-hint" }, "No tracked applications yet. Add one in the Job tracker.")])),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Your numbers"),
        el("p", { class: "ui-hint" }, `Notice period: ${data.notice_days == null ? "not set (Job preferences)" : data.notice_days + " days"}. Free times: ${data.settings.work_start} to ${data.settings.work_end} IST, Monday to Friday, ${data.settings.buffer_minutes} minutes clear around events.`),
        field("Default salary ask (yours to set; never guessed)", ask),
        ui.button({ label: "Save", icon: "check", variant: "tonal", size: "sm", onClick: async () => {
          try { await api("/api/jobassist/salary-ask", { method: "PUT", json: { salary_ask: ask.value } }); ui.toast("Saved on this laptop.", { kind: "good" }); } catch (e) { fail(e); }
        } })));
    ui.renumberEyebrows?.(root);
  }
  load();
}
