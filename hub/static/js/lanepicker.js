// Work lanes UI helpers (routes: docs/api/lanes.md). One compact chip row ("Auto | Council | Gemini | ...") that other
// screens drop next to their own button, plus the small helpers they share: start a lane run, wait for it, show the answer.
//   const picker = lanePicker({ value: "auto", onChange: (id) => {...} });  row.append(picker.el);  picker.value
//   lanePicker({ options: [{ id: "template", label: "Template" }, { id: "council", label: "Council" }, { id: "auto", label: "AI" }] })
//   await runInLane(text, laneId, { cls, link })       -> shows the result (a sheet) or a "started" toast; true when handled
//   await sendToCouncil(text, { link })                -> the "Send to Council" button action
//   await askLane(text, laneId, { cls })               -> the finished run (waits), or null when it failed
// Everything shown is text (textContent), never HTML. "Auto" means: the class order (routine: Council, then local, then Gemini).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/lanes.css";
const POLL_MS = 3000;
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
let cache = null, cachedAt = 0;

function css() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

export async function loadLanes(force = false) {
  if (!force && cache && Date.now() - cachedAt < 30000) return cache;
  cache = await api("/api/lanes");
  cachedAt = Date.now();
  return cache;
}
export const forgetLanes = () => { cache = null; };

// ---- the chip row ------------------------------------------------------------------------------------------
export function lanePicker({ value = "auto", onChange, options = null, label = "Run with", compact = true, only = null } = {}) {
  css();
  let current = value;
  const row = el("div", { class: "lp" + (compact ? " lp--compact" : ""), role: "group", "aria-label": label });
  const caption = el("span", { class: "lp-label" }, label);
  let choices = options || [{ id: "auto", label: "Auto" }];

  function paint() {
    row.replaceChildren(caption, ...choices.map((c) => {
      const chip = el("button", { type: "button", class: "ui-chip lp-chip", "aria-pressed": String(c.id === current), title: c.hint || "" }, c.label);
      chip.addEventListener("click", () => {
        if (current === c.id) return;
        current = c.id; paint();
        if (onChange) onChange(current);
      });
      return chip;
    }));
  }
  paint();
  if (!options) {
    loadLanes().then((d) => {
      const lanes = d.lanes.filter((l) => l.enabled && (!only || only.includes(l.id)));
      choices = [{ id: "auto", label: "Auto", hint: "The usual order, free lanes first" },
        ...lanes.map((l) => ({ id: l.id, label: l.label.replace(/ \(.*\)$/, ""), hint: `${l.cost}${l.why_not ? " · " + l.why_not : ""}` }))];
      if (!choices.some((c) => c.id === current)) current = "auto";
      paint();
    }).catch(() => { /* the row keeps just Auto */ });
  }
  return { el: row, get value() { return current; }, set(v) { current = v; paint(); } };
}

// ---- runs --------------------------------------------------------------------------------------------------
export function startLaneRun({ task, lane = "auto", cls, link, verify }) {
  const json = { task, ...(lane && lane !== "auto" ? { lane } : {}), ...(cls ? { class: cls } : {}), ...(link ? { link } : {}), ...(verify ? { verify } : {}) };
  return api("/api/lanes/run", { method: "POST", json });
}

export async function waitForRun(run, { onUpdate, stopped = () => false } = {}) {
  let current = run;
  while (current.running && !stopped()) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    current = await api(`/api/lanes/runs/${current.id}`);
    if (onUpdate) onUpdate(current);
  }
  return current;
}

const STATUS = { done: "Done", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", needs_owner: "Needs you", running: "Running" };

export function resultSheet(run, { onUse, useLabel = "Use this" } = {}) {
  css();
  const body = el("div", { class: "ui-stack lp-result" });
  const draw = (r) => {
    run = r;
    const parts = [
      el("div", { class: "lp-head" }, ui.badge(STATUS[r.status] || r.status, r.status === "done" ? "good" : r.status === "running" ? "accent" : "warn"),
        r.lane ? el("span", { class: "lp-sub" }, `by ${r.lane}${r.saved ? " · free, a limited call was saved" : ""}`) : null)];
    if (r.answer) parts.push(el("pre", { class: "lp-answer" }, r.answer));
    if (r.note) parts.push(ui.notice(r.note, { tone: "warn" }));
    if (r.running) parts.push(el("p", { class: "ui-hint" }, r.council?.log_tail ? r.council.log_tail.split("\n").filter((l) => l.trim()).slice(-3).join(" · ") : "Working in the background..."));
    if ((r.attempts || []).length) {
      parts.push(el("p", { class: "ui-caps" }, "What was tried"),
        ...r.attempts.map((a) => el("p", { class: "lp-try" }, `${a.label || a.lane || "lane"}: ${a.status}${a.why ? " (" + a.why + ")" : ""}`)));
    }
    body.replaceChildren(...parts);
  };
  draw(run);
  const actions = [];
  if (onUse) actions.push({ label: useLabel, variant: "primary", icon: "check", onClick: () => { if (run.answer) onUse(run.answer); return true; } });
  actions.push({ label: "Try a stronger model", variant: "tonal", icon: "arrow-up", onClick: async () => {
    try { draw(await api(`/api/lanes/runs/${run.id}/stronger`, { method: "POST", json: {} })); } catch (error) { fail(error); }
    return false;
  } });
  actions.push({ label: "Close", variant: "ghost" });
  ui.sheet({ title: "Work lane result", content: body, actions });
  return draw;
}

// Starts a run and tells the owner what happened. Council lanes run in the background (a toast, then a toast when done).
export async function runInLane(task, lane = "auto", { cls, link, onUse, useLabel } = {}) {
  if (!String(task || "").trim()) { fail({ message: "Write what to work on." }); return false; }
  let run;
  try { run = await startLaneRun({ task, lane, cls, link }); } catch (error) { fail(error); return false; }
  if (!run.running) { resultSheet(run, { onUse, useLabel }); return true; }
  ui.toast("Given to the Council. It runs in the background; you'll get a notification.", { kind: "good" });
  waitForRun(run).then((done) => {
    ui.toast(done.status === "done" ? "Council finished. Open Council to read it." : (done.note || "The work lane could not finish."), { kind: done.status === "done" ? "good" : "warn", ms: 6000 });
  }).catch(() => {});
  return true;
}

export const sendToCouncil = (task, { link, cls = "routine" } = {}) => runInLane(task, "council", { link, cls });

// Waits for the answer (used where the answer fills a field). Returns the finished run or null.
export async function askLane(task, lane = "auto", { cls, onUpdate } = {}) {
  let run;
  try { run = await startLaneRun({ task, lane, cls }); } catch (error) { fail(error); return null; }
  try { run = await waitForRun(run, { onUpdate }); } catch (error) { fail(error); return null; }
  if (run.status !== "done") { fail({ message: run.note || "The work lane could not finish." }); return null; }
  return run;
}

// "Saved calls this week" tile (Council screen). `saved` is GET /api/lanes/saved.
export function savedTile(saved) {
  css();
  const n = saved?.week ?? 0;
  const by = Object.entries(saved?.by_lane || {}).map(([lane, count]) => `${lane} ${count}`).join(" · ");
  return el("section", { class: "ui-card lp-saved", "aria-label": "Saved calls this week" },
    el("b", {}, String(n)),
    el("span", {}, n === 1 ? "call kept off Gemini and Claude this week" : "calls kept off Gemini and Claude this week"),
    el("small", {}, by ? `Free lanes answered: ${by}.` : "A call counts as saved when a free lane (Council or local) produced the accepted answer."));
}

// A phone-friendly "Send to Council" button for cards. Pass the text to hand over (and optionally a brain link).
export function councilButton(getTask, { link, size = "sm", label = "Send to Council" } = {}) {
  css();
  const btn = ui.button({ label, icon: "users", variant: "tonal", size, class: "lp-send", onClick: () => ui.busy(btn, async () => {
    await sendToCouncil(typeof getTask === "function" ? getTask() : getTask, { link: typeof link === "function" ? link() : link });
  }) });
  return btn;
}
