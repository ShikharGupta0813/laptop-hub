// Replies card for the job tracker: mail you got about your applications, sorted on this laptop by plain rules.
// Each card is only a SUGGESTION. A status changes when you tap Confirm. Nothing is sent. Routes: docs/api/replies.md.
// The data comes with the tracker's one call (GET /api/jobtrack, key `replies`); "Check mail now" is a tap, never a timer.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { scheduleSheet } from "./jobassist.js";

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TONE = { interview: "good", offer: "good", assessment: "accent", rejection: "bad", recruiter: "", auto_ack: "gray", opt_out: "warn", other: "" };
const LABEL = { interview: "Interview invite", offer: "Offer", assessment: "Test or assessment", rejection: "Rejection", recruiter: "Recruiter", auto_ack: "Auto reply", opt_out: "Said no to outreach", other: "Reply" };

export function repliesCard(data, { records, statuses, reload }) {
  const r = data.replies;
  if (!r) return null;
  const act = async (call, okMsg) => { try { await call(); if (okMsg) ui.toast(okMsg, { kind: "good" }); await reload(); } catch (e) { fail(e); } };

  function pickSheet(s) {
    const sel = el("select", { class: "ui-input", "aria-label": "Which application" },
      el("option", { value: "" }, "Choose the application"),
      ...(s.candidates.length ? s.candidates : records.filter((x) => ["applied", "viewed", "replied", "interview"].includes(x.status)).slice(0, 40))
        .map((c) => el("option", { value: c.id }, `${c.company}: ${c.role}`)));
    const st = el("select", { class: "ui-input", "aria-label": "New status" }, ...statuses.map(([v, l]) => el("option", { value: v }, l)));
    st.value = s.status || "replied";
    ui.sheet({ title: "Which application is this?", class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
      el("p", { class: "ui-hint" }, `"${s.subject}" from ${s.sender}. Nothing changes until you tap Save.`),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Application"), sel),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Set status to"), st)),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      if (!sel.value) { ui.toast("Choose the application first.", { kind: "warn" }); return false; }
      try { await api(`/api/replies/${s.id}/confirm`, { method: "POST", json: { record_id: sel.value, status: st.value } }); ui.toast("Status updated.", { kind: "good" }); reload(); return true; } catch (e) { fail(e); return false; }
    } }] });
  }

  function card(s) {
    const verb = s.action === "status" ? `Mark ${s.status}` : s.action === "optout" ? "Add to opt-out list" : s.action === "pick" ? "Choose application" : "Got it";
    return el("article", { class: "ui-card ui-stack jt-card" },
      el("div", { class: "jt-badges" }, ui.badge(LABEL[s.kind] || "Reply", TONE[s.kind]), s.date ? ui.badge(s.date, "gray") : null),
      el("p", { class: "jt-title" }, s.text),
      el("p", { class: "jt-sub" }, `${s.sender}: ${s.subject}`), s.snippet ? el("p", { class: "jt-note" }, s.snippet) : null,
      el("div", { class: "jt-actions" },
        ui.button({ label: verb, icon: "check", variant: "primary", size: "sm", onClick: async () => {
          if (s.action === "pick") { pickSheet(s); return; }
          if (s.action === "status") {
            const ok = await ui.confirm({ title: "Change this status?", message: s.text + " Pending follow-up reminders for it stop if you confirm a rejection or an interview.", confirmLabel: verb });
            if (!ok) return;
          }
          act(() => api(`/api/replies/${s.id}/confirm`, { method: "POST", json: {} }), s.action === "optout" ? "Added to your opt-out list." : "Done.");
        } }),
        s.kind === "interview" ? ui.button({ label: "Offer times", icon: "calendar", variant: "tonal", size: "sm", onClick: () => scheduleSheet(s) }) : null,
        ui.button({ label: "Dismiss", variant: "ghost", size: "sm", onClick: () => act(() => api(`/api/replies/${s.id}/dismiss`, { method: "POST", json: {} })) })));
  }

  const c = r.counts;
  const head = c.total ? `${c.total} to review${c.interview ? `, ${c.interview} interview invite${c.interview === 1 ? "" : "s"}` : ""}` : "nothing new";
  const check = ui.button({ label: "Check mail now", icon: "refresh", variant: "tonal", size: "sm", onClick: async () => {
    check.disabled = true;
    try { const out = await api("/api/replies/check", { method: "POST", json: {} }); ui.toast(out.polled?.reason === "mail not connected" ? "Mail is not connected." : "Checked your mail.", { kind: "good" }); await reload(); } catch (e) { fail(e); check.disabled = false; }
  } });
  return el("section", { class: "ui-stack jt-replies" }, ui.eyebrow("", `Replies (${head})`),
    ...(r.pending.length ? r.pending.slice(0, 12).map(card) : [el("p", { class: "ui-hint" }, r.mail_connected
      ? "No replies to review. Your mail is read on this laptop every 30 minutes and sorted by plain rules; nothing changes until you confirm."
      : "Connect a mail account in Settings to sort replies here. Mail never leaves this laptop.")]),
    el("div", { class: "jt-actions" }, check, r.last_poll ? el("span", { class: "jt-sub" }, `Last check ${r.last_poll.replace("T", " ").slice(0, 16)}`) : null));
}
