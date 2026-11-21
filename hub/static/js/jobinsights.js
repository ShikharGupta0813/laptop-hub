// Insights tab for the job tracker: funnel, rejection patterns, does the score predict replies, skill gaps, this week's experiment.
// One call (GET /api/jobinsights) when the tab opens; no timer. Everything is worked out on this laptop from your tracker; nothing is
// sent or changed except starting or skipping the experiment. Every string goes in with textContent. Routes: docs/api/jobinsights.md.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const num = (v) => (v === null || v === undefined ? "n/a" : String(v));

function funnelTable(rows, withFinder) {
  if (!rows.length) return null;
  const cols = withFinder ? ["found", "apply", "sent", "reply", "int.", "offer"] : ["sent", "reply", "int.", "offer"];
  return el("div", { class: "jt-table jt-table--wide", style: `grid-template-columns: minmax(0,1fr) repeat(${cols.length}, 2.9rem)` },
    el("span", { class: "h" }, ""), ...cols.map((c) => el("span", { class: "h n" }, c)),
    ...rows.flatMap((r) => [el("span", { class: "k" }, r.key),
      ...(withFinder ? [el("span", { class: "n" }, num(r.found)), el("span", { class: "n" }, num(r.triaged_apply))] : []),
      el("span", { class: "n" }, String(r.applied)), el("span", { class: "n" }, `${r.replies}`), el("span", { class: "n" }, String(r.interviews)), el("span", { class: "n" }, String(r.offers))]));
}

export async function insightsTab(rerender) {
  let d;
  try { d = await api("/api/jobinsights"); } catch (e) { return [el("p", { class: "ui-hint" }, e.message)]; }
  const reload = () => rerender();
  const act = async (call, ok) => { try { await call(); if (ok) ui.toast(ok, { kind: "good" }); reload(); } catch (e) { fail(e); } };
  const f = d.funnel, x = d.experiment, cur = x.current;

  const exp = el("article", { class: "ui-card ui-stack jt-card" }, ui.eyebrow("", "This week's experiment"),
    el("p", { class: "jt-title" }, cur.text), el("p", { class: "jt-sub" }, cur.why),
    cur.state === "suggested" ? el("div", { class: "jt-actions" },
      ui.button({ label: "Start it", icon: "check", variant: "primary", size: "sm", onClick: () => act(() => api(`/api/jobinsights/experiment/${cur.id}/start`, { method: "POST", json: {} }), "Started. Next week the result check shows here.") }),
      ui.button({ label: "Skip", variant: "ghost", size: "sm", onClick: () => act(() => api(`/api/jobinsights/experiment/${cur.id}/skip`, { method: "POST", json: {} })) }))
      : el("p", { class: "ui-hint" }, cur.state === "active" ? `Running since ${cur.started}. ${cur.result?.text || ""}` : "Skipped this week."),
    x.last ? el("div", { class: "jt-suggest" }, el("span", {}, `Last week: ${x.last.text} Result: ${x.last.result.text}`)) : null);

  const score = el("article", { class: "ui-card ui-stack jt-card" }, ui.eyebrow("", "Does the score predict replies?"), el("p", { class: "jt-note" }, d.score_check.text),
    d.score_check.bands.length ? el("div", { class: "jt-table", style: "grid-template-columns: minmax(0,1fr) repeat(3, 3.6rem)" },
      el("span", { class: "h" }, ""), el("span", { class: "h n" }, "sent"), el("span", { class: "h n" }, "replies"), el("span", { class: "h n" }, "rate"),
      ...d.score_check.bands.flatMap((b) => [el("span", { class: "k" }, `${b.key} (${b.score_from} to ${b.score_to})`), el("span", { class: "n" }, String(b.applied)), el("span", { class: "n" }, String(b.replies)), el("span", { class: "n" }, `${b.reply_rate}%`)])) : null);

  const rej = d.rejections;
  const rejCard = el("article", { class: "ui-card ui-stack jt-card" }, ui.eyebrow("", "Rejection patterns"), el("p", { class: "jt-note" }, rej.text),
    ...rej.stages.map((s) => el("p", { class: "jt-sub" }, `${s.n} ${s.stage}`)),
    ...rej.traits.map((t) => el("p", { class: "jt-sub" }, `Common: ${t.trait} (${t.rejected_share}% of rejections, ${t.all_share}% of all applications)`)));

  const gaps = d.skill_gaps;
  const gapCard = el("article", { class: "ui-card ui-stack jt-card" }, ui.eyebrow("", `Skills postings ask for (${gaps.postings} postings)`),
    gaps.gaps.length ? el("p", { class: "jt-note" }, "Asked for, not in your facts: " + gaps.gaps.map((g) => `${g.skill} (${g.count})`).join(", "))
      : el("p", { class: "ui-hint" }, gaps.postings ? "Nothing asked for that your facts do not already cover." : "No postings to read yet."),
    gaps.have.length ? el("p", { class: "jt-sub" }, "Asked for and in your facts: " + gaps.have.map((g) => `${g.skill} (${g.count})`).join(", ")) : null,
    el("p", { class: "ui-hint" }, gaps.note));

  return [exp,
    el("p", { class: "ui-hint" }, d.note),
    ui.eyebrow("", "Funnel"), funnelTable([f.total], f.total.found !== null) ,
    ui.eyebrow("", "By source"), funnelTable(f.by_source, f.total.found !== null),
    ui.eyebrow("", "By role family"), funnelTable(f.by_role_family, false),
    ui.eyebrow("", "By resume version"), funnelTable(f.by_resume, false),
    ui.eyebrow("", "By time of day"), funnelTable(f.by_hour, false),
    ui.eyebrow("", "By weekday"), funnelTable(f.by_weekday, false),
    score, rejCard, gapCard].filter(Boolean);
}
