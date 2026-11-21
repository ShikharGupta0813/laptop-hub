// Job tracker: every application with its status, follow-up reminders (and a draft nudge you copy yourself), and a local view of
// what is working. Statuses change only when you tap. Nothing here sends, applies or contacts anyone. Routes: docs/api/jobtrack.md.
// One data call (GET /api/jobtrack) fills all three tabs; there is no polling. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { repliesCard } from "./jobreplies.js";
import { insightsTab } from "./jobinsights.js";

const CSS = "/css/jobtrack.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TONE = { applied: "", viewed: "accent", replied: "good", interview: "good", offer: "good", rejected: "bad", ghosted: "warn", withdrawn: "gray" };
const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}
const field = (label, c, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), c, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "200", ...attrs }); i.value = value || ""; return i; };

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "jt ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", tab = "tracker", filter = "all";

  async function load() {
    try { data = await api("/api/jobtrack"); error = ""; } catch (e) { error = e.message; }
    paint();
  }
  async function act(call, okMsg) {
    try { const r = await call(); if (okMsg) ui.toast(okMsg, { kind: "good" }); await load(); return r; } catch (e) { fail(e); return null; }
  }

  // ---- sheets ----
  function recordSheet(rec) {
    const cur = rec || { company: "", role: "", source: "", url: "", applied_at: today(), posted_at: "", resume_version: "", salary_note: "", notes: "" };
    const company = input(cur.company, { maxlength: "120", placeholder: "Company" });
    const role = input(cur.role, { maxlength: "160", placeholder: "Role" });
    const source = input(cur.source === "manual" ? "" : cur.source, { maxlength: "60", placeholder: "Greenhouse, referral, company site" });
    const url = input(cur.url, { maxlength: "1000", type: "url", inputmode: "url", placeholder: "https://" });
    const applied = input(cur.applied_at, { type: "date" });
    const posted = input(cur.posted_at, { type: "date" });
    const resume = input(cur.resume_version, { maxlength: "80", placeholder: "backend-v3" });
    const salary = input(cur.salary_note, { maxlength: "200", placeholder: "Range, expectation, offer" });
    const notes = el("textarea", { class: "ui-input", maxlength: "2000", rows: "3", "aria-label": "Notes" }); notes.value = cur.notes || "";
    const warn = el("p", { class: "jt-warn" });
    let confirmed = false;
    const body = () => ({ company: company.value, role: role.value, source: source.value, url: url.value, applied_at: applied.value, posted_at: posted.value,
      resume_version: resume.value, salary_note: salary.value, notes: notes.value });
    ui.sheet({ title: rec ? "Edit application" : "I applied", class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
      field("Company", company), field("Role", role), field("Where you found it", source), field("Link", url), field("Date applied", applied),
      field("Posting date (if shown)", posted, "A posting older than 30 days when you applied is flagged."), field("Resume version used", resume),
      field("Salary note", salary), field("Notes", notes), warn),
    actions: [{ label: rec ? "Save" : "Add", variant: "primary", icon: "check", onClick: async () => {
      try {
        if (rec) { await api(`/api/jobtrack/applications/${rec.id}`, { method: "PATCH", json: body() }); ui.toast("Saved.", { kind: "good" }); load(); return true; }
        if (!confirmed) {
          const c = await api("/api/jobtrack/check", { method: "POST", json: { company: company.value, role: role.value, applied_at: applied.value, posted_at: posted.value } });
          const msgs = [];
          if (c.duplicates.length) msgs.push(`You already applied to this ${c.duplicates[0].days_ago} days ago.`);
          if (c.stale) msgs.push(`This posting is over ${c.stale_days} days old.`);
          if (msgs.length) { warn.textContent = msgs.join(" ") + " Tap Add again to add it anyway."; confirmed = true; return false; }
        }
        const r = await api("/api/jobtrack/applications", { method: "POST", json: body() });
        ui.toast(r.warnings[0] || "Added.", { kind: r.warnings.length ? "warn" : "good" }); load(); return true;
      } catch (e) { fail(e); return false; }
    } }] });
  }

  function statusSheet(rec, preset) {
    const sel = el("select", { class: "ui-input" }, ...data.statuses.map(([v, l]) => el("option", { value: v }, l)));
    sel.value = preset || rec.status;
    const note = input("", { maxlength: "300", placeholder: "Optional note" });
    ui.sheet({ title: `${rec.company}: change status`, class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" }, field("Status", sel), field("Note", note),
      el("p", { class: "ui-hint" }, "History: " + rec.history.map((h) => `${h.status} ${h.at.slice(0, 10)}`).join(" > "))),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      try { await api(`/api/jobtrack/applications/${rec.id}/status`, { method: "POST", json: { status: sel.value, note: note.value } }); load(); return true; } catch (e) { fail(e); return false; }
    } }] });
  }

  async function draftSheet(rec, day) {
    let d;
    try { d = await api(`/api/jobtrack/applications/${rec.id}/draft${day ? `?day=${day}` : ""}`); } catch (e) { fail(e); return; }
    const subject = input(d.subject, { maxlength: "200", "aria-label": "Subject" });
    const body = el("textarea", { class: "ui-input jt-draft", "aria-label": "Draft message" }); body.value = d.body;
    ui.sheet({ title: `Follow-up draft: ${rec.company}`, class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
      el("p", { class: "ui-hint" }, "A draft only. Nothing is sent. Built from your own confirmed facts; read it, change it, copy it, send it yourself."),
      d.note ? el("p", { class: "jt-warn" }, d.note) : null, field("Subject", subject), body),
    actions: [{ label: "Copy message", variant: "primary", icon: "copy", onClick: () => { copy(body.value); return false; } },
      { label: "Copy subject", variant: "tonal", icon: "copy", onClick: () => { copy(subject.value); return false; } }] });
  }

  // ---- cards ----
  function recordCard(rec) {
    const href = safeHref(rec.url);
    const next = rec.followups.filter((f) => !f.done && !f.cancelled).slice(0, 2).map((f) => `day ${f.day}: ${f.due}`).join(", ");
    return el("article", { class: "ui-card ui-stack jt-card" },
      el("h3", { class: "jt-title" }, rec.role), el("span", { class: "jt-sub" }, `${rec.company} · applied ${rec.applied_at}`),
      el("div", { class: "jt-badges" }, ui.badge(rec.status_label, TONE[rec.status]), rec.source ? ui.badge(rec.source) : null,
        rec.resume_version ? ui.badge(rec.resume_version) : null, rec.stale_posting ? ui.badge("old posting", "warn") : null,
        rec.followups_due.length ? ui.badge("follow-up due", "warn") : null),
      rec.duplicates.length ? el("p", { class: "jt-warn" }, `Also applied ${rec.duplicates[0].days_ago} days ago to the same role.`) : null,
      rec.suggest ? el("div", { class: "jt-suggest" }, el("span", {}, rec.suggest.text),
        ui.button({ label: `Mark ${rec.suggest.status}`, variant: "tonal", size: "sm", onClick: () => statusSheet(rec, rec.suggest.status) })) : null,
      rec.salary_note ? el("p", { class: "jt-note" }, "Salary: " + rec.salary_note) : null, rec.notes ? el("p", { class: "jt-note" }, rec.notes) : null,
      next && ["applied", "viewed"].includes(rec.status) ? el("p", { class: "jt-sub" }, "Next follow-ups: " + next) : null,
      el("div", { class: "jt-actions" },
        ui.button({ label: "Status", icon: "edit", variant: "tonal", size: "sm", onClick: () => statusSheet(rec) }),
        ui.button({ label: "Nudge draft", icon: "copy", variant: "ghost", size: "sm", onClick: () => draftSheet(rec) }),
        ui.button({ label: "Edit", variant: "ghost", size: "sm", onClick: () => recordSheet(rec) }),
        href ? el("a", { class: "ui-btn ui-btn--ghost ui-btn--sm", href, target: "_blank", rel: "noopener noreferrer" }, "Posting") : null,
        ui.button({ label: "Delete", variant: "ghost", size: "sm", onClick: async () => {
          const ok = await ui.confirm({ title: "Delete this record?", message: "It is removed from the tracker and its reminders are cleared. The job itself is not affected.", confirmLabel: "Delete", danger: true });
          if (ok) act(() => api(`/api/jobtrack/applications/${rec.id}`, { method: "DELETE" }), "Deleted.");
        } })));
  }

  function trackerTab() {
    const sel = el("select", { class: "ui-input", "aria-label": "Filter by status" }, el("option", { value: "all" }, `All (${data.records.length})`),
      ...data.statuses.map(([v, l]) => el("option", { value: v }, `${l} (${data.counts[v]})`)));
    sel.value = filter; sel.addEventListener("change", () => { filter = sel.value; paint(); });
    const rows = data.records.filter((r) => filter === "all" || r.status === filter);
    return [repliesCard(data, { records: data.records, statuses: data.statuses, reload: load }),
      el("div", { class: "jt-actions" }, ui.button({ label: "I applied", icon: "plus", variant: "primary", onClick: () => recordSheet(null) }), sel),
      ...(rows.length ? rows.map(recordCard) : [el("p", { class: "ui-hint" }, data.records.length ? "Nothing with that status." : "No applications yet. Ones you send from Applications appear here by themselves, or tap I applied to add one.")])];
  }

  function followTab() {
    const byId = new Map(data.records.map((r) => [r.id, r]));
    const due = data.due.map((d) => byId.get(d.id)).filter(Boolean);
    const card = (rec, dueNow) => {
      const f = rec.followups.filter((x) => !x.done && !x.cancelled);
      const cur = dueNow ? f.find((x) => x.due <= today()) || f[0] : f[0];
      return el("article", { class: "ui-card ui-stack jt-card" }, el("h3", { class: "jt-title" }, rec.role), el("span", { class: "jt-sub" }, `${rec.company} · applied ${rec.applied_at}`),
        el("p", { class: "jt-sub" }, cur ? `Day ${cur.day} follow-up, ${dueNow ? "due since" : "on"} ${cur.due}` : "No follow-up left"),
        el("div", { class: "jt-actions" },
          ui.button({ label: "Draft", icon: "copy", variant: "primary", size: "sm", onClick: () => draftSheet(rec, cur?.day) }),
          cur ? ui.button({ label: "I followed up", icon: "check", variant: "tonal", size: "sm", onClick: () => act(() => api(`/api/jobtrack/applications/${rec.id}/followups/${cur.day}/done`, { method: "POST", json: {} }), "Marked as followed up.") }) : null,
          ui.button({ label: rec.call ? "Ring me: on" : "Ring me: off", variant: "ghost", size: "sm", onClick: () => act(() => api(`/api/jobtrack/applications/${rec.id}/call`, { method: "POST", json: { call: !rec.call } }),
            rec.call ? "Reminders are plain again." : "Its reminders will ring like a call.") })));
    };
    const soon = data.records.filter((r) => ["applied", "viewed"].includes(r.status) && !data.due.some((d) => d.id === r.id) && r.followups.some((f) => !f.done && !f.cancelled));
    return [el("p", { class: "ui-hint" }, `Reminders at ${data.settings.followup_days.length ? "day " + data.settings.followup_days.join(" and day ") : "no days (switched off)"} after you apply${data.reminders ? "" : ". The reminders service is not available, so only this list shows them"}. Change the days in Settings (jobtrack_followup_days).`),
      ui.eyebrow("01", `Due now (${due.length})`), ...(due.length ? due.map((r) => card(r, true)) : [el("p", { class: "ui-hint" }, "Nothing is due.")]),
      ui.eyebrow("02", `Coming up (${soon.length})`), ...soon.slice(0, 30).map((r) => card(r, false))];
  }

  function table(title, rows) {
    if (!rows.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("", title), el("div", { class: "jt-table" },
      el("span", { class: "h" }, ""), el("span", { class: "h n" }, "sent"), el("span", { class: "h n" }, "replies"), el("span", { class: "h n" }, "interv."),
      ...rows.flatMap((r) => [el("span", { class: "k" }, r.key), el("span", { class: "n" }, String(r.applied)), el("span", { class: "n" }, String(r.replies)), el("span", { class: "n" }, String(r.interviews))])));
  }

  function statsTab() {
    const s = data.stats, d = s.days_to_first_reply;
    const sug = data.suggestions.map((x) => el("div", { class: "ui-card ui-stack jt-card" }, el("p", { class: "jt-title" }, x.text),
      x.note ? el("p", { class: "jt-sub" }, x.note) : x.applies_to.length ? el("p", { class: "jt-sub" }, "Changes: " + x.applies_to.map((p) => p.name).join(", ") + " (your search profile)") : null,
      el("div", { class: "jt-actions" }, x.adjustable ? ui.button({ label: "Apply to my profile", icon: "check", variant: "primary", size: "sm", onClick: async () => {
        const ok = await ui.confirm({ title: "Change your search profile?", message: x.text + " This only edits your Job preferences; you can undo it there.", confirmLabel: "Apply" });
        if (ok) act(() => api(`/api/jobtrack/suggestions/${encodeURIComponent(x.id)}/accept`, { method: "POST", json: {} }), "Profile updated.");
      } }) : null, ui.button({ label: "Dismiss", variant: "ghost", size: "sm", onClick: () => act(() => api(`/api/jobtrack/suggestions/${encodeURIComponent(x.id)}/dismiss`, { method: "POST", json: {} })) }))));
    return [el("div", { class: "ui-card ui-stack jt-card" }, ui.eyebrow("", "This week"), el("p", { class: "jt-note" }, data.summary.text),
        el("div", { class: "jt-actions" }, ui.button({ label: "Copy summary", icon: "copy", variant: "tonal", size: "sm", onClick: () => copy(data.summary.text) }))),
      el("p", { class: "ui-hint" }, `${s.total.applied} applications, ${s.total.replies} replies, ${s.total.interviews} interviews. ` +
        (d.n ? `First reply after ${d.median} days (typical), from ${d.n} replies.` : "No replies recorded yet.") + " Worked out on this laptop; no AI reads your jobs."),
      ui.eyebrow("", `Suggestions (${data.suggestions.length})`), ...(sug.length ? sug : [el("p", { class: "ui-hint" }, "Nothing to suggest yet. It needs about 10 applications per source.")]),
      table("By source", s.by_source), table("By role word", s.by_keyword), table("By resume version", s.by_resume)];
  }

  function paint() {
    if (error) { root.replaceChildren(el("p", { class: "ui-hint" }, error)); return; }
    if (!data) { root.replaceChildren(el("p", { class: "ui-hint" }, "Loading…")); return; }
    const seg = ui.segmented({ label: "Job tracker sections", value: tab, onChange: (v) => { tab = v; paint(); },
      options: [{ value: "tracker", label: "Tracker" }, { value: "follow", label: data.due.length ? `Due (${data.due.length})` : "Follow-ups" }, { value: "stats", label: "Works" }, { value: "insights", label: "Insights" }] });
    const c = data.counts;
    const stat = (n, label) => el("div", { class: "jt-stat" }, el("b", {}, String(n)), el("span", {}, label));
    root.replaceChildren(el("h2", { class: "ui-title" }, "Job tracker"),
      el("div", { class: "jt-stats" }, stat(data.records.length, "tracked"), stat(c.applied + c.viewed, "waiting"), stat(c.replied + c.interview, "replies"), stat(c.offer, "offers")),
      seg, ...(tab === "tracker" ? trackerTab() : tab === "follow" ? followTab() : tab === "stats" ? statsTab() : [el("p", { class: "ui-hint" }, "Working it out…")]));
    ui.renumberEyebrows(root);
    if (tab === "insights") insightsTab(paint).then((nodes) => { if (tab === "insights") { const holder = root.lastElementChild; holder.replaceWith(...nodes); ui.renumberEyebrows(root); } });
  }

  paint();
  load();
}
