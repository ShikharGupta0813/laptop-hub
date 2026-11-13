// Research: ask a question, watch the progress, read the report with numbered sources. Routes: docs/api/research.md.
// Jarvis (cyan) = technical research, Friday (amber) = life and work briefs. Everything shown is text (textContent);
// source links show their URL as text, open in a new tab and carry rel="noopener noreferrer".
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/research.css";
const PERSONA = { technical: "jarvis", brief: "friday" };
const STYLE_HINT = {
  technical: "Errors, libraries, how-tos, comparing tools. Answers with code and commands.",
  brief: "Prep for a meeting, person or company; trips and purchases. Can use your calendar and tasks.",
};
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "rs ui-stack ui-stack--lg" });
  container.append(root);
  let style = "technical", jobs = [], current = null, loadError = "", timer = null, budget = null;

  const stop = () => { if (timer) { clearTimeout(timer); timer = null; } };

  async function loadHistory() {
    try {
      const r = await api("/api/research");
      jobs = r.jobs || []; budget = r.budget_left; loadError = "";
    } catch (error) {
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paint();
  }

  async function open(id) {
    stop();
    try { current = await api(`/api/research/${id}`); } catch (error) { fail(error); return; }
    paint();
    if (current.status === "running") poll(id);
  }

  function poll(id) {
    stop();
    timer = setTimeout(async () => {
      try {
        const job = await api(`/api/research/${id}`);
        if (!current || current.id !== id) return;
        current = job;
        paint();
        if (job.status === "running") poll(id); else loadHistory();
      } catch (error) { if (current && current.id === id) poll(id); }
    }, 2000);
  }

  async function ask(input, btn) {
    const question = input.value.trim();
    if (question.length < 3) { ui.toast("Write what to research first.", { kind: "warn" }); return; }
    await ui.busy(btn, async () => {
      try {
        const r = await api("/api/research", { method: "POST", json: { question, style } });
        input.value = "";
        await open(r.id);
      } catch (error) { fail(error); }
    });
  }

  function chooser() {
    const seg = ui.segmented({ label: "Who researches", value: style, options: [
      { value: "technical", label: "Jarvis", icon: "terminal" }, { value: "brief", label: "Friday", icon: "sparkles" }],
      onChange: (v) => { style = v; root.dataset.persona = PERSONA[v]; hint.textContent = STYLE_HINT[v]; } });
    seg.classList.add("rs-seg");
    const hint = el("p", { class: "ui-hint" }, STYLE_HINT[style]);
    return el("div", { class: "ui-stack" }, seg, hint);
  }

  function askCard() {
    const input = el("textarea", { class: "ui-input rs-q", rows: "3", maxlength: "600", "aria-label": "What should I research?",
      placeholder: "What should I find out? e.g. why does pip fail with error X, or what should I know before my 3pm with Acme" });
    const btn = ui.button({ label: "Research", icon: "search", variant: "primary", onClick: () => ask(input, btn) });
    return el("section", { class: "ui-card ui-hud rs-ask ui-stack" }, ui.eyebrow("01", "Ask"), chooser(), input,
      el("div", { class: "ui-row" }, btn, budget != null ? el("span", { class: "ui-hint" }, `${budget} AI calls left today`) : null));
  }

  function sourceList(job) {
    const rows = (job.sources || []).map((s) => {
      const link = el("a", { href: s.url, target: "_blank", rel: "noopener noreferrer", class: "rs-link" }, s.url);
      return el("li", { class: "rs-src", id: `src-${job.id}-${s.n}` },
        el("span", { class: "rs-n ui-num" }, `[${s.n}]`),
        el("span", { class: "rs-src__body" }, el("span", { class: "rs-src__title" }, s.title || s.domain),
          link, s.read ? null : el("span", { class: "ui-hint" }, "snippet only")));
    });
    return el("ol", { class: "rs-sources" }, ...rows);
  }

  function reportCard() {
    const job = current;
    const persona = PERSONA[job.style] || "jarvis";
    const head = el("div", { class: "ui-row" }, ui.badge(persona === "friday" ? "Friday" : "Jarvis", persona === "friday" ? "warn" : "accent"),
      ui.badge(job.status, job.status === "done" ? "good" : job.status === "failed" ? "bad" : "accent"),
      ui.button({ icon: "x", iconOnly: true, variant: "ghost", "aria-label": "Close", onClick: () => { stop(); current = null; paint(); } }));
    const parts = [head, el("h2", { class: "rs-title" }, job.question)];
    const steps = el("ul", { class: "rs-steps", "aria-live": "polite" }, ...(job.steps || []).slice(-12).map((s) => el("li", {}, s)));
    if (job.status === "running") parts.push(ui.eyebrow("02", "Working"), steps);
    if (job.status === "failed") parts.push(ui.notice(job.error || "The research failed.", { tone: "bad" }));
    if (job.status === "done") {
      if (job.degraded) parts.push(ui.notice("The AI was not available, so this lists what was found instead of a written summary.", { tone: "warn" }));
      if (job.answer_short) parts.push(ui.eyebrow("02", "In short"), el("p", { class: "rs-short" }, job.answer_short));
      parts.push(ui.eyebrow("03", "Report"));
      for (const para of (job.report || "").split(/\n{2,}/)) if (para.trim()) parts.push(el("p", { class: "rs-para" }, para.trim()));
      if ((job.sources || []).length) parts.push(ui.eyebrow("04", `Sources (${job.sources.length})`), sourceList(job));
      parts.push(el("details", { class: "rs-log" }, el("summary", {}, "How it was found"), steps));
    }
    return el("section", { class: "ui-card ui-card--accent rs-report ui-stack" }, ...parts);
  }

  function history() {
    if (!jobs.length) return ui.emptyState({ icon: "search", title: "No research yet", body: "Ask a question above, or say “Jarvis, research …”.", compact: true });
    return el("div", { class: "ui-list ui-list--inset" }, ...jobs.map((j) => {
      const row = ui.listRow({ icon: j.status === "failed" ? "alert-circle" : "search", tone: j.status === "failed" ? "bad" : j.style === "brief" ? "warn" : "accent",
        title: j.question, sub: j.status === "running" ? "Working…" : (j.answer_short || j.status).slice(0, 120),
        meta: j.status === "done" ? `${j.sources} src` : "", chevron: true, onClick: () => open(j.id) });
      const del = ui.button({ icon: "trash", iconOnly: true, variant: "ghost", "aria-label": "Delete this research", onClick: async (e) => {
        e.stopPropagation();
        if (!(await ui.confirm({ title: "Delete this research?", confirmLabel: "Delete", danger: true }))) return;
        try { await api(`/api/research/${j.id}`, { method: "DELETE" }); if (current?.id === j.id) current = null; await loadHistory(); } catch (error) { fail(error); }
      } });
      return el("div", { class: "rs-hrow" }, row, del);
    }));
  }

  function paint() {
    root.dataset.persona = PERSONA[style];
    if (loadError === "off") { root.replaceChildren(ui.emptyState({ icon: "search", title: "Research is off", body: "Turn it on in Settings (research_enabled)." })); return; }
    const kids = [askCard()];
    if (loadError) kids.push(ui.notice(loadError, { tone: "bad" }));
    if (current) kids.push(reportCard());
    kids.push(el("section", { class: "ui-stack" }, ui.eyebrow("05", "History"), history()));
    root.replaceChildren(...kids);
  }

  paint();
  loadHistory();
  return { show() { loadHistory(); if (current?.status === "running") poll(current.id); }, hide: stop };
}
