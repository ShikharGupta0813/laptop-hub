// Council: run the multi-llm-lab "council" (several models working on one task) as a background job.
// Routes: docs/api/council.md. Everything the lab prints is shown as text (textContent), never as HTML.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { savedTile } from "./lanepicker.js";

const CSS = "/css/council.css";
const POLL_MS = 3000;
const TONE = { running: "accent", accepted: "good", failed: "bad", stopped: "warn", cancelled: "warn", timeout: "warn", error: "bad", interrupted: "warn" };
const LABEL = { running: "Running", accepted: "Accepted", failed: "Failed", stopped: "Stopped", cancelled: "Cancelled", timeout: "Timed out", error: "Error", interrupted: "Interrupted" };
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const mins = (s) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min` : s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`);

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back below */ }
  const ta = el("textarea", { class: "cn-offscreen", "aria-hidden": "true", readonly: true });
  ta.value = text; document.body.append(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "cn ui-stack ui-stack--lg" });
  container.append(root);
  let status = null, jobs = [], models = null, modelsError = "", saved = null;
  let selected = null;           // id of the run shown in the result card
  let detail = null;             // that run in full
  let loadError = "";
  let timer = null, visible = true;
  const draft = { task: "", verify: "", notes: "", only: new Set() };

  // ---- data -----------------------------------------------------------------------------------------
  async function load({ quiet = false } = {}) {
    if (!status && !quiet) root.replaceChildren(ui.skeleton("card", 1), ui.skeleton("list", 3));
    try {
      status = await api("/api/council/status");
      loadError = "";
      saved = await api("/api/lanes/saved").catch(() => null);      // the tile is optional: an older hub has no lanes
      if (status.enabled) {
        jobs = (await api("/api/council/jobs")).jobs || [];
        const running = jobs.find((j) => j.running);
        if (!selected || !jobs.some((j) => j.id === selected)) selected = (running || jobs[0])?.id || null;
        detail = selected ? await api(`/api/council/jobs/${selected}`) : null;
      }
    } catch (error) { loadError = error instanceof ApiError && (error.status === 404 || error.status === 405) ? "missing" : error.message; }
    schedule();
    paint();
    if (status?.enabled && !models && !modelsError) loadModels();
  }

  async function loadModels() {
    try { models = (await api("/api/council/models")).models || []; modelsError = ""; }
    catch (error) { modelsError = error.message; }
    paint();
  }

  function schedule() {
    clearInterval(timer); timer = null;
    if (visible && jobs.some((j) => j.running)) {
      timer = setInterval(() => { if (!root.isConnected) { clearInterval(timer); return; } if (!document.hidden) load({ quiet: true }); }, POLL_MS);
    }
  }

  // ---- pieces ---------------------------------------------------------------------------------------
  function notSetUp() {
    return el("div", { class: "ui-card cn-setup" },
      ui.emptyState({ icon: "users", title: "Council isn't set up yet", body: "Council runs your multi-llm-lab project (several models working together) in the background. It needs the lab folder on the laptop." }),
      el("p", { class: "ui-hint" }, "On the laptop, in PowerShell, run:"),
      el("pre", { class: "cn-cmd" }, status?.setup_command || "python -m hub setup --only council"),
      el("p", { class: "ui-hint" }, "Then restart the hub. The setup can download the lab and prepare it for you."));
  }

  function newRun() {
    const running = jobs.find((j) => j.running);
    const task = el("textarea", { class: "cn-task", rows: 4, maxlength: status.limits?.task || 4000, placeholder: "What should the council work on?", "aria-label": "Task" });
    task.value = draft.task;
    task.addEventListener("input", () => { draft.task = task.value; });
    const verify = el("input", { type: "text", maxlength: status.limits?.verify || 500, placeholder: "Optional command that checks the result, e.g. pytest -q", "aria-label": "Verify command", autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
    verify.value = draft.verify;
    verify.addEventListener("input", () => { draft.verify = verify.value; });
    const start = ui.button({ label: running ? "A run is going" : "Start", icon: "play", variant: "primary", block: true, disabled: !!running, onClick: () => ui.busy(start, async () => {
      if (!draft.task.trim()) { fail({ message: "Write what the council should work on." }); return; }
      try {
        const body = { task: draft.task, ...(draft.verify.trim() ? { verify_command: draft.verify.trim() } : {}), ...(draft.only.size ? { only: [...draft.only] } : {}) };
        const job = await api("/api/council/run", { method: "POST", json: body });
        draft.task = ""; draft.verify = ""; selected = job.id;
        ui.toast("Council started. You'll get a notification when it finishes.", { kind: "good" });
        await load({ quiet: true });
      } catch (error) { fail(error); }
    }) });
    return el("section", { class: "ui-card cn-new ui-stack" },
      el("p", { class: "ui-caps" }, "New run"), task, verify,
      el("p", { class: "ui-hint" }, `Runs on the laptop, up to ${status.timeout_min} minutes, one at a time. Free and local models only unless you allowed Claude in hub.json.`),
      onlyPicker(), start);
  }

  function onlyPicker() {
    const ready = (models || []).filter((m) => m.enabled);
    if (!ready.length) return null;
    const box = el("details", { class: "cn-only" }, el("summary", {}, draft.only.size ? `Use only ${draft.only.size} chosen model${draft.only.size === 1 ? "" : "s"}` : "Use only these models (optional)"));
    const list = el("div", { class: "cn-only__list" });
    for (const m of ready) {
      const cb = el("input", { type: "checkbox", "aria-label": m.name });
      cb.checked = draft.only.has(m.name);
      cb.addEventListener("change", () => {
        if (cb.checked) draft.only.add(m.name); else draft.only.delete(m.name);
        box.querySelector("summary").textContent = draft.only.size ? `Use only ${draft.only.size} chosen model${draft.only.size === 1 ? "" : "s"}` : "Use only these models (optional)";
      });
      list.append(el("label", { class: "cn-only__item" }, cb, el("span", {}, m.name), m.ready ? null : ui.badge("not ready", "warn")));
    }
    box.append(el("p", { class: "ui-hint" }, "Leave all unticked to let the council use every model that is switched on."), list);
    return box;
  }

  function runningCard(job) {
    const cancel = ui.button({ label: "Cancel run", icon: "x", variant: "danger-soft", onClick: () => ui.busy(cancel, async () => {
      if (!await ui.confirm({ title: "Cancel this run?", message: "The council stops now. Work so far is not kept in the answer.", confirmLabel: "Cancel run", cancelLabel: "Keep going", danger: true })) return;
      try { await api(`/api/council/jobs/${job.id}/cancel`, { method: "POST", json: {} }); ui.toast("Cancelled.", { kind: "good" }); await load({ quiet: true }); } catch (error) { fail(error); }
    }) });
    const lines = (job.log_tail || "").split("\n").filter((l) => l.trim()).slice(-14).map((text) => ({ text, dim: true }));
    return el("section", { class: "ui-card cn-running ui-stack" },
      el("div", { class: "ui-section-head" }, el("p", { class: "ui-caps" }, "Running"), el("span", { class: "cn-elapsed" }, ui.led("accent", { live: true, label: "Running" }), mins(job.elapsed_s))),
      el("p", { class: "cn-task-text" }, job.task),
      ui.term({ title: "progress", lines: lines.length ? lines : [{ text: "Waiting for the first message...", dim: true }], cursor: true }),
      cancel);
  }

  function resultCard(job) {
    const r = job.result;
    const head = el("div", { class: "cn-head" }, ui.badge(LABEL[job.status] || job.status, TONE[job.status]),
      el("span", { class: "cn-sub" }, `${mins(job.elapsed_s)}${job.resume_of ? " · rerun with notes" : ""}`));
    const card = el("section", { class: "ui-card cn-result ui-stack" }, el("p", { class: "ui-caps" }, "Result"), head, el("p", { class: "cn-task-text" }, job.task));
    if (job.error) card.append(ui.notice(job.error, { tone: job.status === "cancelled" ? undefined : "warn" }));
    if (r) {
      if (r.stopped) card.append(ui.notice(`Stopped: ${r.stopped}`, { tone: "warn" }));
      card.append(el("pre", { class: "cn-answer" }, r.answer || "(no answer)"));
      if (r.files?.length) card.append(el("div", { class: "cn-files" }, el("p", { class: "ui-caps" }, "Files"), ...r.files.map((f) => el("code", {}, f))));
      const calls = Object.entries(r.calls || {});
      if (calls.length) card.append(el("div", { class: "cn-calls" }, el("p", { class: "ui-caps" }, "Model calls"),
        ...calls.map(([name, n]) => el("span", { class: "cn-call" }, el("span", {}, name), el("b", {}, String(n))))));
    }
    if (job.log_tail && !r) card.append(ui.term({ title: "last progress", lines: job.log_tail.split("\n").filter((l) => l.trim()).slice(-8).map((text) => ({ text, dim: true })) }));
    if (job.run_dir) card.append(reviewLoop(job));
    return card;
  }

  // Copy a packet for Claude, then paste Claude's notes back and rerun with them (notes go over stdin on the laptop).
  function reviewLoop(job) {
    const copy = ui.button({ label: "Copy review packet", icon: "copy", variant: "tonal", onClick: () => ui.busy(copy, async () => {
      try {
        const d = await api(`/api/council/jobs/${job.id}/review-packet`);
        if (await copyText(d.packet)) ui.toast(d.truncated ? "Copied (shortened). Paste it into Claude." : "Copied. Paste it into Claude.", { kind: "good" });
        else ui.sheet({ title: "Review packet", content: el("textarea", { class: "cn-packet", readonly: true, rows: 12, value: d.packet }), actions: [{ label: "Done", variant: "primary" }] });
      } catch (error) { fail(error); }
    }) });
    const notes = el("textarea", { class: "cn-notes", rows: 4, maxlength: status.limits?.notes || 8000, placeholder: "Paste Claude's notes here, then rerun.", "aria-label": "Claude's notes" });
    notes.value = draft.notes;
    notes.addEventListener("input", () => { draft.notes = notes.value; });
    const running = jobs.some((j) => j.running);
    const rerun = ui.button({ label: running ? "A run is going" : "Rerun with notes", icon: "refresh", variant: "primary", disabled: running, onClick: () => ui.busy(rerun, async () => {
      if (!draft.notes.trim()) { fail({ message: "Paste Claude's notes first." }); return; }
      try {
        const next = await api("/api/council/run", { method: "POST", json: { resume_job_id: job.id, notes: draft.notes } });
        draft.notes = ""; selected = next.id;
        ui.toast("Rerun started.", { kind: "good" });
        await load({ quiet: true });
      } catch (error) { fail(error); }
    }) });
    return el("div", { class: "cn-review ui-stack" }, el("p", { class: "ui-caps" }, "Claude review"),
      el("p", { class: "ui-hint" }, "Copy a short summary, paste it into Claude yourself, then paste Claude's notes back here. Claude is never called by the hub."),
      copy, el("p", { class: "ui-caps" }, "Claude's notes"), notes, rerun);
  }

  function history() {
    if (jobs.length < 2) return null;
    return el("section", { class: "cn-history" }, el("p", { class: "ui-caps cn-caps" }, "Earlier runs"),
      el("div", { class: "ui-list ui-list--inset" }, ...jobs.map((j) => ui.listRow({
        icon: j.running ? "loader" : j.status === "accepted" ? "check-circle" : "alert-circle", tone: TONE[j.status] || "accent",
        title: j.task, sub: `${LABEL[j.status] || j.status} · ${ui.relTime ? ui.relTime(j.created * 1000) : ""}${j.answer_preview ? " · " + j.answer_preview : ""}`,
        chevron: true, onClick: () => { selected = j.id; load({ quiet: true }); },
      }))));
  }

  function modelsSection() {
    const rows = [];
    if (modelsError) rows.push(ui.notice(modelsError, { tone: "warn" }));
    else if (!models) rows.push(ui.skeleton("list", 2));
    else if (!models.length) rows.push(ui.notice("The council reports no models.", { tone: "warn" }));
    else for (const m of models) {
      const sw = ui.switchControl({ checked: m.enabled, label: `Use ${m.name}`, onChange: async (on) => {
        sw.input.disabled = true;
        try { models = (await api(`/api/council/models/${encodeURIComponent(m.name)}`, { method: "POST", json: { enabled: on } })).models; if (!on) draft.only.delete(m.name); }
        catch (error) { fail(error); sw.input.checked = !on; }
        paint();
      } });
      if (m.locked) sw.input.disabled = true;
      const sub = [m.provider, m.model].filter(Boolean).join(" · ");
      rows.push(el("div", { class: "ui-row cn-model" + (m.enabled ? "" : " is-off") },
        el("span", { class: "ui-row__body" },
          el("span", { class: "ui-row__title" }, m.name, " ", ui.badge(m.paid ? "paid" : "free", m.paid ? "warn" : "good"), " ",
            m.ready ? null : ui.badge("not ready", "bad")),
          sub ? el("span", { class: "ui-row__sub" }, sub) : null,
          !m.ready && m.reason ? el("span", { class: "ui-row__sub cn-reason" }, m.reason) : null,
          m.note ? el("span", { class: "ui-row__sub cn-note" }, m.note) : null), sw));
    }
    return el("section", { class: "cn-models" }, el("p", { class: "ui-caps cn-caps" }, "Models"),
      el("div", { class: "ui-list ui-list--inset" }, ...rows),
      el("p", { class: "ui-hint cn-caps" }, "Switched-off models are left out of every run. Claude stays off unless you allow it in hub.json."));
  }

  function paint() {
    if (loadError === "missing") { root.replaceChildren(ui.emptyState({ icon: "users", title: "Council isn't on this laptop", body: "Update the hub to get it." })); return; }
    if (loadError && !status) { root.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load Council", body: loadError, action: { label: "Try again", icon: "refresh", onClick: () => load() } })); return; }
    if (!status?.enabled) { root.replaceChildren(notSetUp()); return; }
    const parts = [];
    if (!status.ready) parts.push(ui.notice("The council folder is set but has no 'lab' folder in it. Run the setup again.", { tone: "bad" }));
    parts.push(newRun());
    if (saved) parts.push(savedTile(saved));
    const job = detail;
    if (job?.running) parts.push(runningCard(job));
    else if (job) parts.push(resultCard(job));
    parts.push(history(), modelsSection());
    root.replaceChildren(...parts.filter(Boolean));
  }

  load();
  return {
    show() { visible = true; load({ quiet: true }); },
    hide() { visible = false; clearInterval(timer); timer = null; },
    refresh() { models = null; modelsError = ""; load(); },
  };
}
