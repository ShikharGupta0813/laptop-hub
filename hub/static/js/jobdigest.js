// Job digest: today's top 10 new jobs (best match first, with who you know there), the weekly review, and follow-up drafts.
// One call (GET /api/jobdigest) when the screen opens; no timer. The only taps that change anything are "Add to apply queue",
// "Done" on a draft and "Interview time". Nothing here sends, applies or contacts anyone. Every string goes in with textContent.
// Routes: docs/api/jobdigest.md.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/jobdigest.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);
const VERDICT = { apply: "Go", maybe: "Maybe" };

async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "jd ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", tab = "today";

  async function load() {
    try { data = await api("/api/jobdigest"); error = ""; } catch (e) { error = e.message; }
    paint();
  }
  async function act(call, ok) {
    try { await call(); if (ok) ui.toast(ok, { kind: "good" }); await load(); } catch (e) { fail(e); }
  }

  function itemCard(i) {
    const href = safeHref(i.url);
    return el("article", { class: "ui-card ui-stack jd-card" },
      el("p", { class: "jd-title" }, i.title), el("p", { class: "jd-sub" }, [i.company, i.source].filter(Boolean).join(" · ")),
      el("div", { class: "jd-badges" },
        ui.badge(`${i.match.score}% ${i.match.kind === "ats" ? "ATS match" : "fit"}`, i.match.score >= 70 ? "good" : "accent"),
        i.verdict && VERDICT[i.verdict] ? ui.badge(VERDICT[i.verdict]) : null,
        i.warm?.length ? ui.badge("You know someone", "good") : null),
      i.reason ? el("p", { class: "jd-sub" }, i.reason) : null,
      ...(i.warm || []).map((w) => el("p", { class: "jd-warm" }, `${w.name}: ${w.why}`)),
      el("div", { class: "jd-actions" },
        i.queued ? ui.badge("In the apply queue", "gray")
          : ui.button({ label: "Add to apply queue", icon: "send", variant: "primary", size: "sm", onClick: () => act(() => api(`/api/jobdigest/items/${i.id}/queue`, { method: "POST", json: {} }), "Added to the apply queue. Nothing was sent.") }),
        href ? el("a", { class: "ui-btn ui-btn--ghost ui-btn--sm", href, target: "_blank", rel: "noopener noreferrer" }, "Open posting") : null));
  }

  function todayTab() {
    const d = data.digest;
    if (!d.items) return [el("p", { class: "ui-hint" }, `Your digest is built after ${String(data.settings.hour).padStart(2, "0")}:00 each morning.`)];
    const head = el("p", { class: "ui-hint" }, d.items.length
      ? `${d.items.length} of ${d.passed} new jobs that passed your go or no-go check, best match first. Built ${d.generated_at.replace("T", " ").slice(0, 16)}.`
      : "Nothing new since the last digest passed your go or no-go check.");
    return [head, ...d.items.map(itemCard),
      el("div", { class: "jd-actions" }, ui.button({ label: "Refresh", icon: "refresh", variant: "tonal", size: "sm", onClick: () => act(() => api("/api/jobdigest/refresh", { method: "POST", json: {} }), "Refreshed.") }))];
  }

  function weekTab() {
    const w = data.weekly;
    const stat = (n, label) => el("div", { class: "jd-stat" }, el("b", {}, String(n)), el("span", {}, label));
    const best = (label, b) => (b ? el("p", { class: "jd-sub" }, `${label}: ${b.key} (${b.replies} replies from ${b.applied})`) : null);
    return [el("div", { class: "jd-stats" }, stat(w.applied, "sent"), stat(w.replies, "replies"), stat(w.interviews, "interviews"), stat(w.offers, "offers")),
      el("article", { class: "ui-card ui-stack jd-card" }, ui.eyebrow("", `Last 7 days (${w.from} to ${w.to})`),
        w.reply_rate.applied ? el("p", { class: "jd-sub" }, `Overall: ${w.reply_rate.replies} of ${w.reply_rate.applied} applications got a reply (${w.reply_rate.percent}%).`) : null,
        best("Best resume", w.best_resume), best("Best kind of opening", w.best_opening), best("Best source", w.best_source),
        el("div", { class: "jd-suggest" }, el("span", {}, "One change to try: " + w.suggestion)),
        el("div", { class: "jd-actions" }, ui.button({ label: "Copy review", icon: "copy", variant: "tonal", size: "sm", onClick: () => copy(w.text) }))),
      el("p", { class: "ui-hint" }, w.note)];
  }

  function interviewSheet(n) {
    const input = el("input", { class: "ui-input", type: "datetime-local", "aria-label": "Interview date and time" });
    ui.sheet({ title: "Interview time", content: el("div", { class: "ui-stack" },
      el("p", { class: "ui-hint" }, "When was the interview? The thank-you reminder moves to a couple of hours after it, and the status check to a week after."), input),
    actions: [{ label: "Save", variant: "primary", onClick: () => {
      if (!input.value) return false;
      act(() => api(`/api/jobdigest/interviews/${n.rid}`, { method: "POST", json: { when: input.value } }), "Reminders moved.");
      return undefined;
    } }] });
  }

  function nudgeCard(n) {
    return el("article", { class: "ui-card ui-stack jd-card" }, el("p", { class: "jd-title" }, `${n.label}: ${n.role}`),
      el("p", { class: "jd-sub" }, `${n.company} · due ${n.due.replace("T", " ")}`),
      el("p", { class: "jd-sub" }, n.subject), el("pre", { class: "jd-draft" }, n.body),
      el("p", { class: "ui-hint" }, n.undo),
      el("div", { class: "jd-actions" },
        ui.button({ label: "Copy draft", icon: "copy", variant: "primary", size: "sm", onClick: () => copy(`${n.subject}\n\n${n.body}`) }),
        n.kind !== "noreply" ? ui.button({ label: "Interview time", variant: "ghost", size: "sm", onClick: () => interviewSheet(n) }) : null,
        ui.button({ label: "Done", icon: "check", variant: "tonal", size: "sm", onClick: () => act(() => api(`/api/jobdigest/nudges/${n.id}/done`, { method: "POST", json: {} }), "Marked done.") })));
  }

  function followTab() {
    const list = data.nudges;
    return list.length ? [el("p", { class: "ui-hint" }, "Reminders with a draft ready. You copy it and send it yourself."), ...list.map(nudgeCard)]
      : [el("p", { class: "ui-hint" }, `Nothing to send. A draft appears here ${data.settings.nudge_days} days after an application with no reply, and after an interview.`)];
  }

  function paint() {
    if (error) { root.replaceChildren(el("p", { class: "ui-hint" }, error)); return; }
    if (!data) { root.replaceChildren(el("p", { class: "ui-hint" }, "Loading…")); return; }
    const seg = ui.segmented({ label: "Job digest sections", value: tab, onChange: (v) => { tab = v; paint(); },
      options: [{ value: "today", label: "Top 10" }, { value: "week", label: "Week" }, { value: "follow", label: data.nudges.length ? `Drafts (${data.nudges.length})` : "Drafts" }] });
    root.replaceChildren(el("h2", { class: "ui-title" }, "Job digest"), seg, ...(tab === "today" ? todayTab() : tab === "week" ? weekTab() : followTab()));
    ui.renumberEyebrows(root);
  }

  paint();
  load();
}
