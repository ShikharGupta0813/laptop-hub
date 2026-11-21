// Go / no-go label and small badges for a find or a queued application (hub/triage.py, docs/api/triage.md).
// Shared by the opportunities screen and the application queue. Everything shown is text (textContent), never HTML.
// A skip is only a label with a reason: nothing is hidden, and the filter chips bring every group back into view.
import { el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/triage.css";
export const VERDICT_TONE = { apply: "good", maybe: "warn", skip: "bad" };
export const VERDICT_NAME = { apply: "Apply", maybe: "Maybe", skip: "Skip" };
const GHOST_SHORT = { repost: "Reposted", reopened: "Listed again", long_open: "Open long", vague: "Vague", noreply: "Never replies" };

export function loadCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

// The label shape is the same on a find (find.triage) and on a queued item (item.meta.triage).
export const triageOf = (row) => (row && (row.triage || row.meta?.triage)) || null;

export function verdictLine(t) {
  if (!t || !t.verdict) return null;
  return el("p", { class: "tg-line" },
    ui.badge(VERDICT_NAME[t.verdict] || t.verdict, VERDICT_TONE[t.verdict]),
    el("span", { class: "tg-reason" }, t.reason || ""));
}

// Lock, ghost and gig badges, short. A lock that conflicts with your preferences is red, one that is only worth knowing is plain.
export function flagBadges(t) {
  if (!t) return [];
  const out = [];
  for (const l of t.locks || []) out.push(ui.badge((l.conflict ? "Lock: " : "Note: ") + l.label, l.conflict ? "bad" : undefined));
  const g = t.ghost || {};
  if (g.likely) out.push(ui.badge("Likely ghost", "warn"));
  for (const s of g.signals || []) {
    if (GHOST_SHORT[s.code] && !(g.likely && s.code === "ghost")) out.push(ui.badge(s.label.length > 38 ? GHOST_SHORT[s.code] : s.label, undefined));
  }
  if (t.gig && typeof t.gig.score === "number") out.push(ui.badge(`Gig ${t.gig.score}`, t.gig.score >= 65 ? "good" : t.gig.score >= 35 ? "warn" : "bad"));
  return out;
}

// Why a find is likely a ghost, as one plain line (the label of the first ghost signal).
export function ghostWhy(t) {
  const s = (t?.ghost?.signals || []).find((x) => x.code === "ghost");
  return s && t.ghost.likely ? el("p", { class: "tg-why ui-hint" }, s.label) : null;
}

export function aiLine(t) {
  if (!t?.ai) return null;
  return el("p", { class: "tg-ai ui-hint" }, `AI second opinion: ${VERDICT_NAME[t.ai.verdict] || t.ai.verdict}. ${t.ai.reason || ""}`);
}

// Filter chips: All / Apply / Maybe / Skip, each with its count. `counts` is {apply, maybe, skip, none}.
export function chipRow({ value = "", counts = {}, total, onChange }) {
  loadCss();
  const all = total ?? ((counts.apply || 0) + (counts.maybe || 0) + (counts.skip || 0) + (counts.none || 0));
  const defs = [["", "All", all], ["apply", "Apply", counts.apply || 0], ["maybe", "Maybe", counts.maybe || 0], ["skip", "Skip", counts.skip || 0]];
  const row = el("div", { class: "tg-chips", role: "group", "aria-label": "Filter by go or no-go" });
  for (const [v, label, n] of defs) {
    const b = el("button", { type: "button", class: "tg-chip" + (v ? ` tg-chip--${v}` : ""), "aria-pressed": String(value === v) }, `${label} ${n}`);
    b.addEventListener("click", () => onChange?.(value === v ? "" : v));
    row.append(b);
  }
  return row;
}
