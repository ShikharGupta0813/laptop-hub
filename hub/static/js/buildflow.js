// Build: tell Jarvis what you want built; he asks a few questions, writes a spec and a checkbox plan in your folder, then
// the Council works through the plan one step at a time while you only approve. Routes: docs/api/buildflow.md.
// Everything shown is text (textContent), never HTML.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/buildflow.css";
const STAGES = [["brainstorm", "Brainstorm"], ["spec", "Spec"], ["plan", "Plan"], ["build", "Build"]];
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "bf ui-persona-jarvis ui-stack ui-stack--lg" });
  container.append(root);
  let flows = [], workspaces = [], loadError = "", seq = 0, timer = null;
  const list = el("div", { class: "ui-stack ui-stack--lg" });
  const drafts = new Map();   // flow id -> { kind: "spec" | "plan", preview } waiting for the owner's OK

  async function load() {
    const mine = ++seq;
    try {
      const [data, ws] = await Promise.all([api("/api/buildflow"), api("/api/buildflow/workspaces").catch(() => ({ workspaces: [] }))]);
      if (mine !== seq) return;
      flows = data.flows || []; workspaces = ws.workspaces || [];
      loadError = data.enabled === false ? "off" : "";
    } catch (error) {
      if (mine !== seq) return;
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paint();
    clearTimeout(timer);
    if (flows.some((f) => f.job_id)) timer = setTimeout(load, 8000);   // a step is running: look again soon
  }

  async function post(flow, path, json, btn) {
    try {
      const run = () => api(`/api/buildflow/${flow.id}/${path}`, { method: "POST", json: json || {} });
      const out = btn ? await ui.busy(btn, run) : await run();
      return out;
    } catch (error) { fail(error); return null; }
  }

  function chips(flow) {
    return el("ol", { class: "bf-stages", "aria-label": "Stage" }, ...STAGES.map(([key, label], i) => {
      const at = flow.stage === "done" ? STAGES.length : STAGES.findIndex(([k]) => k === flow.stage);
      const state = i < at ? "is-done" : i === at ? "is-now" : "";
      return el("li", { class: "bf-stage " + state, "aria-current": i === at ? "step" : null }, label);
    }));
  }

  function questionBlock(flow) {
    if (flow.stage !== "brainstorm") return null;
    if (flow.ready) {
      return el("div", { class: "ui-stack" }, el("p", { class: "bf-q" }, "That is enough. Ready to write the spec."),
        specButton(flow));
    }
    const q = flow.question;
    if (!q) return null;
    const input = el("input", { class: "ui-input", type: "text", maxlength: "800", placeholder: "Or type your own answer", "aria-label": "Your answer", enterkeyhint: "send" });
    const send = async (text, btn) => {
      if (!text.trim()) { ui.toast("Write an answer first.", { kind: "warn" }); return; }
      const out = await post(flow, "answer", { answer: text }, btn);
      if (out) load();
    };
    const go = ui.button({ label: "Answer", icon: "send", onClick: () => send(input.value, go) });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(input.value, go); } });
    const answered = flow.answers.length;
    return el("div", { class: "ui-stack" },
      el("p", { class: "ui-caps bf-label" }, `Question ${answered + 1} of up to ${flow.max_questions}`),
      el("p", { class: "bf-q" }, q.question),
      el("div", { class: "bf-opts", role: "group", "aria-label": "Suggested answers" },
        ...(q.options || []).map((o) => { const b = el("button", { type: "button", class: "bf-opt" }, o); b.addEventListener("click", () => send(o, b)); return b; })),
      el("div", { class: "ui-row" }, input, go),
      answered ? el("div", { class: "bf-actions" }, specButton(flow, "Skip the rest and write the spec")) : null);
  }

  // The spec and the plan are files: show the path and the first lines, and write only when the owner taps Write.
  function fileStep(flow, kind, label, doneLabel) {
    const draft = drafts.get(flow.id);
    if (draft && draft.kind === kind) {
      const write = ui.button({ label: "Write it", icon: "check", onClick: async () => {
        const out = await post(flow, kind, {}, write);
        drafts.delete(flow.id);
        if (out) { ui.toast(doneLabel, { kind: "good" }); load(); }
      } });
      return el("div", { class: "ui-stack bf-preview" },
        el("p", { class: "bf-path" }, draft.preview.path),
        el("pre", { class: "bf-pre" }, draft.preview.first_lines),
        el("div", { class: "bf-actions" }, write, ui.button({ label: "Not now", variant: "ghost", onClick: () => { drafts.delete(flow.id); paint(); } })));
    }
    const btn = ui.button({ label, icon: "pen", onClick: async () => {
      const preview = await post(flow, kind, { preview: true }, btn);
      if (preview) { drafts.set(flow.id, { kind, preview }); paint(); }
    } });
    return btn;
  }
  const specButton = (flow, label = "Write the spec") => fileStep(flow, "spec", label, "Spec written.");

  function steps(flow) {
    if (!flow.steps.length) return null;
    return el("div", { class: "ui-stack" },
      el("div", { class: "bf-prog" }, el("span", { class: "ui-caps bf-label" }, `${flow.done} of ${flow.total} done`), ui.progressBar(flow.total ? flow.done / flow.total : 0, "Build progress")),
      el("ul", { class: "bf-steps" }, ...flow.steps.map((s) => el("li", { class: "bf-step" + (s.done ? " is-done" : "") + (s.running ? " is-running" : "") },
        el("span", { class: "bf-box", "aria-hidden": "true" }, s.done ? "x" : s.running ? "..." : ""),
        el("div", { class: "bf-step-body" },
          el("span", { class: "bf-step-text" }, `${s.n}. ${s.text}`),
          s.check ? el("span", { class: "bf-check" }, "Done when: " + s.check) : null,
          s.running ? el("span", { class: "bf-note" }, "The Council is working on this now.") : null,
          s.failure ? el("span", { class: "bf-fail" }, "Last try: " + s.failure) : null)))));
  }

  function nextButton(flow) {
    if (!flow.plan_path || flow.stage === "done") return null;
    const next = ui.button({ label: flow.job_id ? "Working on a step..." : "Do next step", icon: "play", disabled: !!flow.job_id || !flow.next, onClick: async () => {
      if (!(await ui.confirm({ title: "Hand this step to the Council?", message: flow.next, confirmLabel: "Do it", icon: "play" }))) return;
      const out = await post(flow, "next", {}, next);
      if (out) { ui.toast("Started. It runs in the background.", { kind: "good" }); load(); }
    } });
    return next;
  }

  async function remove(flow) {
    if (!(await ui.confirm({ title: "Delete this build?", message: "Your spec and plan files stay in the folder.", confirmLabel: "Delete", danger: true, icon: "trash" }))) return;
    try { await api(`/api/buildflow/${flow.id}`, { method: "DELETE" }); drafts.delete(flow.id); load(); } catch (error) { fail(error); }
  }

  function card(flow) {
    return el("article", { class: "ui-card ui-stack bf-card" + (flow.stage === "done" ? " is-done" : "") },
      el("div", { class: "bf-head" }, el("h3", { class: "bf-title" }, flow.title), flow.stage === "done" ? ui.badge("Finished", "good") : null,
        el("span", { class: "bf-where" }, flow.workspace_name)),
      chips(flow),
      el("p", { class: "bf-summary" }, flow.summary),
      questionBlock(flow),
      flow.stage === "spec" ? fileStep(flow, "plan", "Write the plan", "Plan written.") : null,
      flow.spec_path || flow.plan_path ? el("div", { class: "bf-paths" },
        flow.spec_path ? el("span", {}, "Spec: ", el("code", {}, flow.spec_path)) : null,
        flow.plan_path ? el("span", {}, "Plan: ", el("code", {}, flow.plan_path)) : null) : null,
      steps(flow),
      el("div", { class: "bf-actions" }, nextButton(flow), ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: () => remove(flow) })));
  }

  function newBuild() {
    const goal = el("textarea", { class: "ui-input", rows: "3", maxlength: "1500", placeholder: "What should be built? e.g. a small page that tracks my habits", "aria-label": "What to build" });
    const where = el("select", { class: "ui-input", "aria-label": "Folder to build in" },
      ...(workspaces.length ? workspaces.map((w) => el("option", { value: w.name }, w.name)) : [el("option", { value: "" }, "No shared folders")]));
    const go = ui.button({ label: "Start", icon: "wand", onClick: async () => {
      if (!goal.value.trim()) { ui.toast("Say what you want built.", { kind: "warn" }); return; }
      try {
        await ui.busy(go, () => api("/api/buildflow", { method: "POST", json: { goal: goal.value, workspace: where.value } }));
        goal.value = ""; load();
      } catch (error) { fail(error); }
    } });
    return el("section", { class: "ui-card ui-stack bf-new" }, ui.eyebrow("01", "Build me something"), goal,
      el("div", { class: "ui-row" }, where, go),
      el("p", { class: "bf-note" }, "Jarvis asks a few questions first, then writes a spec and a plan inside that folder."));
  }

  function paint() {
    if (loadError === "off") { list.replaceChildren(ui.emptyState({ icon: "wand", title: "Build is switched off", body: "Set buildflow_enabled to true in Settings." })); return; }
    if (loadError) { list.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load your builds", body: loadError, action: { label: "Try again", icon: "refresh", onClick: load } })); return; }
    list.replaceChildren(newBuild(), ...(flows.length ? flows.map(card) : [ui.emptyState({ icon: "wand", title: "No builds yet", body: 'Say "Jarvis, build me ..." or describe it above.' })]));
  }

  root.append(el("div", { class: "bf-top" }, el("h2", { class: "ui-title" }, "Build")), list);
  list.replaceChildren(ui.skeleton("card", 2));
  load();
  ui.pullToRefresh?.(container, load);
  return { show() { load(); }, refresh() { load(); } };
}
