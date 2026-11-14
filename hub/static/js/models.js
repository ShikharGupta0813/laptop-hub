// Local models panel: catalogue, install/remove, deep thinking, benchmark. Routes: docs/api/models.md.
// Opened from Settings > Models (js/settings.js). Everything shown is text (textContent via el()); reasoning sits behind a fold.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/models.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const good = (text) => ui.toast(text, { kind: "good" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const gb = (n) => (n == null ? "size unknown" : `${n} GB`);
const KIND_LABEL = { general: "General", rca: "RCA draft", synthesis: "Research synthesis", plan: "Plan", application: "Application draft" };

export function modelsPanel({ patch, onChange } = {}) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "lm" });
  let alive = true, data = null, pollTimer = null;
  root.addEventListener("lm-close", () => { alive = false; clearTimeout(pollTimer); });

  async function load() {
    try { data = await api("/api/models"); } catch (error) { root.replaceChildren(ui.notice(error.message, { tone: "bad" })); return; }
    paint();
    if (data.pull?.status === "running" || data.bench?.running || (data.think || []).some((j) => j.status === "running")) schedule();
  }
  function schedule() { clearTimeout(pollTimer); pollTimer = setTimeout(() => { if (alive) load(); }, 2000); }
  const setting = async (body, message) => { try { await patch(body); good(message); onChange?.(); await load(); } catch (error) { fail(error); } };

  async function install(entry) {
    let est;
    try { est = await api(`/api/models/estimate?tag=${encodeURIComponent(entry.tag)}`); } catch (error) { fail(error); return; }
    const lines = [`Download ${entry.tag}: about ${gb(est.size_gb)} (approximate). Disk free: ${est.disk_free_gb ?? "unknown"} GB.`,
      est.est_memory_gb ? `Needs roughly ${est.est_memory_gb} GB of RAM while running (estimate).` : "", est.cpu_warning].filter(Boolean).join(" ");
    if (est.enough_disk === false) { fail({ message: `Not enough disk space. ${lines}` }); return; }
    if (!await ui.confirm({ title: `Install ${entry.tag}?`, message: lines, confirmLabel: "Install" })) return;
    try { await api("/api/models/pull", { method: "POST", json: { tag: entry.tag, confirm: true } }); await load(); } catch (error) { fail(error); }
  }
  async function remove(entry) {
    if (!await ui.confirm({ title: `Remove ${entry.tag}?`, message: "This deletes the model's files from the laptop. You can download it again later.", confirmLabel: "Remove", danger: true })) return;
    try { await api("/api/models/remove", { method: "POST", json: { name: entry.tag, confirm: true } }); good(`${entry.tag} removed.`); await load(); } catch (error) { fail(error); }
  }
  async function dropEntry(entry) {
    try { await api(`/api/models/catalogue/${encodeURIComponent(entry.tag).replace(/%2F/g, "/")}`, { method: "DELETE" }); await load(); } catch (error) { fail(error); }
  }

  function entryActions(entry) {
    const body = el("div", { class: "ui-stack" },
      el("p", { class: "ui-hint" }, [entry.note, entry.cpu_warning].filter(Boolean).join(" ")),
      ui.kv([["Tag", entry.tag], ["Size", gb(entry.size_gb)], ["Memory (estimate)", entry.est_memory_gb ? `${entry.est_memory_gb} GB` : "unknown"],
        ["Minimum RAM", entry.min_ram_gb ? `${entry.min_ram_gb} GB` : "unknown"], ["Strengths", (entry.strengths || []).join(", ") || "not set"],
        ["Tool calling", entry.tools]]));
    const s = ui.sheet({ title: entry.label || entry.tag, content: body, actions: [] });
    const run = (fn) => async () => { s.close(); await fn(); return false; };
    const acts = [];
    if (!entry.installed) acts.push({ label: "Install", icon: "download", variant: "primary", onClick: run(() => install(entry)) });
    if (entry.installed) {
      acts.push({ label: entry.is_deep ? "Stop deep thinking with it" : "Use for deep thinking", icon: "sparkles", variant: "primary",
        onClick: run(() => setting({ local_reasoning_model: entry.is_deep ? "" : entry.tag }, entry.is_deep ? "Deep thinking is off." : `Deep thinking will use ${entry.tag}.`)) });
      if (!entry.is_default) acts.push({ label: "Use as default local", icon: "check", variant: "tonal", onClick: run(() => setting({ chat_model: entry.tag }, `Default local model is ${entry.tag}.`)) });
      acts.push({ label: "Remove", icon: "trash", variant: "danger", onClick: run(() => remove(entry)) });
    }
    if (!entry.configured_only) {
      acts.push({ label: "Edit", icon: "edit", variant: "tonal", onClick: run(async () => editEntry(entry)) });
      acts.push({ label: "Delete from list", icon: "close", variant: "ghost", onClick: run(() => dropEntry(entry)) });
    }
    s.setActions(acts);
    return s;
  }

  function editEntry(entry = {}) {
    const f = {};
    const field = (key, label, value, type = "text", hint) => {
      f[key] = el("input", { type, value: value ?? "", class: "lm-input", autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": label });
      return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), f[key], hint ? el("span", { class: "ui-hint" }, hint) : null);
    };
    const tools = el("select", { class: "lm-input", "aria-label": "Tool calling" },
      ["unknown", "native", "text-format", "none"].map((t) => el("option", { value: t, ...(t === (entry.tools || "unknown") ? { selected: true } : {}) }, t)));
    const rec = el("input", { type: "checkbox", ...(entry.recommended ? { checked: true } : {}) });
    const body = el("div", { class: "ui-stack" },
      field("tag", "Ollama tag", entry.tag, "text", "Any name from ollama.com/library, like qwen3:8b."),
      field("label", "Label", entry.label), field("size_gb", "Size in GB (approximate)", entry.size_gb, "number"),
      field("min_ram_gb", "Minimum RAM in GB", entry.min_ram_gb, "number"),
      field("strengths", "Strengths", (entry.strengths || []).join(", "), "text", "fast chat, reasoning, coding, vision"),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Tool calling"), tools,
        el("span", { class: "ui-hint" }, "native = Ollama tools; text-format = the model writes tool lines; none = answers only; unknown = not checked.")),
      el("label", { class: "lm-row" }, rec, el("span", {}, "Recommended for this laptop")),
      field("note", "Note", entry.note));
    ui.sheet({ title: entry.tag ? "Edit model" : "Add a model", content: body, actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const num = (k) => (f[k].value === "" ? null : Number(f[k].value));
      try {
        await api("/api/models/catalogue", { method: "POST", json: { tag: f.tag.value.trim(), label: f.label.value.trim(), size_gb: num("size_gb"), min_ram_gb: num("min_ram_gb"),
          strengths: f.strengths.value, tools: tools.value, recommended: rec.checked, note: f.note.value } });
        await load(); return true;
      } catch (error) { fail(error); return false; } } }] });
  }

  function catalogueCard() {
    const rows = (data.catalogue || []).map((e) => {
      const badges = el("span", { class: "lm-badges" });
      if (e.installed) badges.append(ui.badge("Installed", "good"));
      if (e.is_default) badges.append(ui.badge("Default local", "info"));
      if (e.is_deep) badges.append(ui.badge("Deep thinking", "accent"));
      if (e.recommended) badges.append(ui.badge("Suggested", "gray"));
      if (e.fits_ram === false) badges.append(ui.badge("May not fit RAM", "warn"));
      const sub = [gb(e.size_gb), (e.strengths || []).join(", "), e.est_memory_gb ? `~${e.est_memory_gb} GB RAM` : "", `tools: ${e.tools}`].filter(Boolean).join(" · ");
      return ui.listRow({ icon: "layers", tone: "gray", title: e.label || e.tag, sub, meta: badges, chevron: true, onClick: () => entryActions(e) });
    });
    const add = ui.button({ label: "Add a model by tag", icon: "plus", variant: "tonal", onClick: () => editEntry() });
    const reset = ui.button({ label: "Reset list", icon: "refresh", variant: "ghost", onClick: async () => {
      if (!await ui.confirm({ title: "Reset the list?", message: "Puts back the built-in models and drops your edits.", confirmLabel: "Reset" })) return;
      try { await api("/api/models/catalogue-reset", { method: "POST", json: { confirm: true } }); await load(); } catch (error) { fail(error); } } });
    return el("section", { class: "ui-stack" }, el("h2", { class: "ui-caps" }, "Model catalogue"),
      el("p", { class: "ui-hint" }, "Sizes and RAM are approximate. Tool calling and speed stay 'unknown' until you set or measure them."),
      el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows), el("div", { class: "lm-row" }, add, reset));
  }

  function pullCard() {
    const p = data.pull;
    if (!p || (p.status !== "running" && Date.now() / 1000 - (p.started || 0) > 600)) return null;
    const bar = ui.progressBar(p.percent == null ? null : p.percent / 100, "Download progress");
    const text = p.status === "running" ? `${p.tag}: ${p.line}${p.percent != null ? ` · ${Math.round(p.percent)}%` : ""}`
      : p.status === "done" ? `${p.tag} is ready.` : p.status === "cancelled" ? `${p.tag}: cancelled.` : `${p.tag}: ${p.error || "failed"}`;
    const cancel = p.status === "running" ? ui.button({ label: "Cancel", icon: "close", variant: "tonal", size: "sm", onClick: async () => {
      try { await api("/api/models/pull/cancel", { method: "POST", json: {} }); await load(); } catch (error) { fail(error); } } }) : null;
    return el("div", { class: "lm-card" }, el("div", { class: "lm-row" }, el("strong", { class: "grow" }, "Download"), cancel), bar, el("p", { class: "ui-hint" }, text));
  }

  function thinkCard() {
    const model = data.config.local_reasoning_model;
    const kind = el("select", { class: "lm-input", "aria-label": "Kind of job" }, Object.entries(KIND_LABEL).map(([k, v]) => el("option", { value: k }, v)));
    const task = el("textarea", { class: "lm-text", placeholder: "Paste what to think hard about. It runs in the background and you get a notification.", "aria-label": "What to think about" });
    const go = ui.button({ label: "Think harder", icon: "sparkles", variant: "primary", onClick: () => ui.busy(go, async () => {
      try { await api("/api/models/think", { method: "POST", json: { kind: kind.value, task: task.value } }); task.value = ""; await load(); } catch (error) { fail(error); } }) });
    const jobs = (data.think || []).map((j) => {
      const sub = j.status === "running" ? `${j.phase} · ${Math.round(j.elapsed_s || 0)} s · ${j.thought_chars} thinking chars`
        : `${j.status}${j.error ? ": " + j.error : ""} · ${Math.round(j.elapsed_s || 0)} s`;
      return ui.listRow({ icon: "sparkles", tone: "accent", title: j.title || j.kind, sub, chevron: true, onClick: () => showJob(j.id) });
    });
    return el("section", { class: "ui-stack" }, el("h2", { class: "ui-caps" }, "Deep thinking"),
      el("p", { class: "ui-hint" }, model ? `Reasoning model: ${model}. Used only for jobs you start here (or ask Jarvis to "think harder"), never for chat or voice replies.`
        : "Off. Install a reasoning model (for example deepseek-r1:8b), then choose Use for deep thinking."),
      model ? el("div", { class: "lm-card" }, kind, task, go) : null,
      jobs.length ? el("div", { class: "ui-list ui-list--inset ui-list--icons" }, jobs) : null);
  }

  async function showJob(id) {
    const body = el("div", { class: "ui-stack" }, ui.skeleton("list", 2));
    ui.sheet({ title: "Deep thinking result", content: body, actions: [] });
    let folded = true;
    const draw = (j, reasoning) => {
      const kids = [];
      if (j.status === "running") kids.push(ui.notice(`Working: ${j.phase}, ${Math.round(j.elapsed_s || 0)} s so far.`, { icon: "clock" }));
      if (j.error) kids.push(ui.notice(j.error, { tone: "bad" }));
      if (j.answer) kids.push(el("p", { class: "lm-answer" }, j.answer));
      if (j.truncated) kids.push(el("p", { class: "ui-hint" }, "Cut at the length limit."));
      if (j.has_reasoning) {
        kids.push(ui.button({ label: folded ? "Show reasoning" : "Hide reasoning", icon: "eye", variant: "ghost", size: "sm", onClick: async () => {
          folded = !folded;
          try { const full = folded ? j : await api(`/api/models/think/${id}?reasoning=true`); draw(full, folded ? "" : full.reasoning); } catch (error) { fail(error); } } }));
        if (reasoning) kids.push(el("p", { class: "lm-reason" }, reasoning));
      }
      if (j.status === "running") kids.push(ui.button({ label: "Cancel job", icon: "close", variant: "tonal", size: "sm", onClick: async () => {
        try { await api(`/api/models/think/${id}/cancel`, { method: "POST", json: {} }); } catch (error) { fail(error); } } }));
      body.replaceChildren(...kids);
    };
    for (let i = 0; i < 1500; i++) {
      let j;
      try { j = await api(`/api/models/think/${id}`); } catch (error) { body.replaceChildren(ui.notice(error.message, { tone: "bad" })); return; }
      if (!body.isConnected) return;
      if (folded) draw(j, "");
      if (j.status !== "running") { load(); return; }
      await sleep(2000);
    }
  }

  function benchCard() {
    const b = data.bench || {};
    const run = ui.button({ label: b.running ? "Running…" : "Run benchmark", icon: "zap", variant: "tonal", onClick: async () => {
      if (!await ui.confirm({ title: "Run the benchmark?", message: "Times 3 short prompts on every installed model. On this CPU-only laptop a 7B+ model can take many minutes. Close other heavy apps first.", confirmLabel: "Run" })) return;
      try { await api("/api/models/bench", { method: "POST", json: { confirm: true } }); await load(); } catch (error) { fail(error); } } });
    if (b.running) run.disabled = true;
    const stop = b.running ? ui.button({ label: "Stop", icon: "close", variant: "ghost", onClick: async () => {
      try { await api("/api/models/bench/cancel", { method: "POST", json: {} }); } catch (error) { fail(error); } } }) : null;
    const status = b.running ? el("p", { class: "ui-hint" }, `Testing ${b.model || "…"} (${b.n || 0}/${b.total || 0}): ${b.prompt || ""}`)
      : b.error ? ui.notice(b.error, { tone: "warn" }) : null;
    const latest = b.latest || [];
    const head = el("tr", {}, ["Model", "Total s", "tok/s", "Puzzle", "JSON", "Summary s"].map((h) => el("th", {}, h)));
    const mark = (r) => (r == null ? "-" : r.ok === true ? el("span", { class: "lm-pass" }, "pass") : r.ok === false ? el("span", { class: "lm-fail", title: r.note }, "FAIL") : "-");
    const rows = latest.map((m) => {
      const by = Object.fromEntries((m.prompts || []).map((p) => [p.id, p]));
      return el("tr", {}, el("td", {}, m.model), el("td", {}, m.total_seconds ?? "-"), el("td", {}, m.avg_tok_s ?? "-"),
        el("td", {}, mark(by.puzzle)), el("td", {}, mark(by.json)), el("td", {}, by.summary?.seconds ?? "-"));
    });
    return el("section", { class: "ui-stack" }, el("h2", { class: "ui-caps" }, "Benchmark on this laptop"),
      el("p", { class: "ui-hint" }, "Puzzle and JSON are checked automatically; the summary is for you to judge. Also: python -m hub models bench."),
      el("div", { class: "lm-row" }, run, stop), status,
      latest.length ? el("div", { class: "lm-tablewrap" }, el("table", { class: "lm-table" }, el("thead", {}, head), el("tbody", {}, rows)))
        : el("p", { class: "ui-hint" }, "No results yet."));
  }

  function paint() {
    const m = data.machine || {};
    root.replaceChildren(...[
      ui.notice(data.privacy, { tone: "good", icon: "lock" }),
      ui.notice(data.cpu_warning, { tone: "warn" }),
      data.reachable ? null : ui.notice("Ollama is not running. Start the Ollama app on the laptop.", { tone: "warn" }),
      el("p", { class: "ui-hint" }, `This laptop: ${m.ram_gb ?? "?"} GB RAM, ${m.disk_free_gb ?? "?"} GB free on the models drive${m.nvidia_gpu ? "" : ", no NVIDIA GPU (CPU only)"}.`),
      pullCard(), catalogueCard(), thinkCard(), benchCard()].filter(Boolean));
  }

  load();
  return root;
}
