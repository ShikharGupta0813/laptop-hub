// Automations: named routines that chain steps and run on demand or on a schedule.
//
// Data: docs/api/automations.md. The editor is generic: it is built from GET /api/automations/step-types
// (field specs), so a new step type on the server needs no change here. Dangerous steps ask with a danger
// confirm and send confirm:true; shutdown / restart / kill_process can never have a schedule.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const ICONS = { sun: "sun", book: "book", door: "logout", moon: "moon", timer: "timer", calendar: "calendar", check: "check-circle", briefcase: "work", bolt: "zap" };
const ICON_TONES = { sun: "orange", book: "info", door: "violet", moon: "violet", timer: "teal", calendar: "accent", check: "good", briefcase: "info", bolt: "accent" };
const STEP_ICONS = { laptop_action: "laptop", script: "terminal", jarvis_say: "jarvis", jarvis_start: "jarvis", open_url: "globe", notify: "bell", note: "note", wait: "clock", briefing: "inbox", reminder: "alarm" };
const STATUS_TONE = { ok: "good", partial: "warn", failed: "bad", interrupted: "warn", running: "accent" };
const STATUS_WORD = { ok: "Worked", partial: "Partly worked", failed: "Failed", interrupted: "Interrupted", running: "Running" };
const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];
const KINDS = [["", "No schedule"], ["daily", "Every day"], ["weekdays", "Weekdays"], ["weekly", "Certain days"], ["interval", "Every so often"], ["once", "Once"]];
const NEVER_FALLBACK = ["kill_process", "restart", "shutdown"];

const pad = (n) => String(n).padStart(2, "0");
function dayWord(date) {
  const now = new Date(), t = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diff = Math.round((d - t) / 86400000);
  if (diff === 0) return "today";
  if (diff === 1) return "tomorrow";
  if (diff === -1) return "yesterday";
  return date.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}
function when(text) {
  const d = text ? new Date(text) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  return `${dayWord(d)} at ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function scheduleWords(s) {
  if (!s) return "Runs when you tap Run";
  if (s.kind === "daily") return `Every day at ${s.time}`;
  if (s.kind === "weekdays") return `Weekdays at ${s.time}`;
  if (s.kind === "weekly") {
    const names = (s.days || []).map((d) => (typeof d === "number" ? DAYS[d]?.[1] : DAYS.find(([k]) => k === String(d).slice(0, 3).toLowerCase())?.[1]) || d);
    return `${names.join(", ") || "Certain days"} at ${s.time}`;
  }
  if (s.kind === "interval") {
    const m = Number(s.every_minutes);
    return m % 60 === 0 ? `Every ${m / 60 === 1 ? "hour" : `${m / 60} hours`}` : `Every ${m} minutes`;
  }
  if (s.kind === "once") return `Once, ${when(s.at)}`;
  return "Scheduled";
}

export function init(container, ctx) {
  if (!document.querySelector('link[href="/css/automations.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/automations.css" }));
  }
  const S = { routines: null, templates: null, spec: null, section: "routines", error: null, actions: null, scripts: null, running: new Set() };
  let pollTimer = null, visible = false, firstRender = true;

  const list = el("div", { class: "au-list ui-stack" });
  const seg = ui.segmented({ label: "Automations", block: true, value: "routines", options: [{ value: "routines", label: "My routines" }, { value: "templates", label: "Templates" }], onChange: (v) => { S.section = v; render(); } });
  const head = el("div", { class: "au-head" }, seg);
  const fab = ui.button({ label: "New routine", icon: "plus", variant: "primary", class: "au-new", onClick: () => openEditor() });
  container.replaceChildren(el("div", { class: "au-root ui-stack ui-stack--lg" }, head, list, fab));

  // ---- loading ----------------------------------------------------------------------------------
  async function load() {
    try {
      const [r, spec] = await Promise.all([api("/api/automations"), S.spec ? S.spec : api("/api/automations/step-types")]);
      S.routines = r.routines || []; S.spec = spec; S.error = null;
    } catch (error) { S.error = error.message; }
    render();
  }
  async function loadTemplates() {
    if (S.templates) return;
    try { S.templates = (await api("/api/automations/templates")).templates || []; } catch (error) { S.templatesError = error.message; }
    render();
  }
  const typeInfo = (t) => S.spec?.types?.find((x) => x.type === t);
  const typeLabel = (t) => typeInfo(t)?.label || t;
  const neverSet = () => new Set(S.spec?.never_scheduled || NEVER_FALLBACK);

  // ---- rendering --------------------------------------------------------------------------------
  function render() {
    fab.hidden = S.section !== "routines" || !!S.error;
    if (S.error && !S.routines) {
      list.replaceChildren(ui.notice(S.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => { S.error = null; render(); load(); } }));
      return;
    }
    if (!S.routines) { list.replaceChildren(ui.skeleton("card", 3)); return; }
    if (S.section === "templates") { renderTemplates(); return; }
    if (!S.routines.length) {
      list.replaceChildren(ui.emptyState({ icon: "zap", title: "No routines yet", body: "A routine is a few steps that run together, on a tap or on a schedule. Start from a template or build your own.",
        action: { label: "Browse templates", icon: "sparkles", onClick: () => { seg.value = "templates"; S.section = "templates"; render(); } } }));
      return;
    }
    list.replaceChildren(...S.routines.map(card));
    if (firstRender) { list.classList.add("ui-stagger"); setTimeout(() => list.classList.remove("ui-stagger"), 1000); firstRender = false; }
  }

  function card(r) {
    const tone = ICON_TONES[r.icon] || "accent";
    const running = r.running || S.running.has(r.id);
    const never = (r.steps || []).some((s) => s.type === "laptop_action" && neverSet().has(s.action_id));
    const sw = ui.switchControl({ checked: !!r.enabled, label: `${r.name} on or off`, onChange: async (on) => {
      if (on && !r.schedule) { ctx.toast("Add a schedule to run this automatically.", { kind: "info" }); }
      try { replace(await api(`/api/automations/${encodeURIComponent(r.id)}/enable`, { method: "POST", json: { enabled: on } })); }
      catch (error) { sw.input.checked = !on; ctx.toast(error.message, { kind: "bad" }); }
    } });
    const runBtn = ui.button({ label: running ? "Running" : "Run", icon: "play", variant: "primary", size: "sm", disabled: running, onClick: (e) => runRoutine(r, e.currentTarget) });
    const status = r.last_status ? ui.badge(`${STATUS_WORD[r.last_status] || r.last_status}${r.last_run ? " " + ui.relTime(new Date(r.last_run)) : ""}`, STATUS_TONE[r.last_status]) : ui.badge("Never run");
    const next = r.enabled && r.next_run ? `Next ${when(r.next_run)}` : r.schedule && !r.enabled ? "Schedule is paused" : "";
    return el("article", { class: "ui-card au-card" + (r.enabled ? "" : " is-off"), "data-id": r.id },
      el("div", { class: "au-card__top" },
        ui.tile(ICONS[r.icon] || "zap", tone, "lg"),
        el("div", { class: "au-card__title" }, el("h3", { class: "ui-truncate" }, r.name), el("p", { class: "ui-clamp-2" }, r.description || `${(r.steps || []).length} ${(r.steps || []).length === 1 ? "step" : "steps"}`)),
        sw),
      el("div", { class: "au-card__meta" },
        el("span", { class: "au-when" }, ui.icon(r.schedule ? "clock" : "play"), scheduleWords(r.schedule)),
        next ? el("span", { class: "au-next" }, next) : null, status),
      el("div", { class: "au-card__steps" }, ...(r.steps || []).slice(0, 6).map((s) => el("span", { class: "au-step-dot", title: typeLabel(s.type) }, ui.icon(STEP_ICONS[s.type] || "circle"))),
        (r.steps || []).length > 6 ? el("span", { class: "au-more" }, `+${r.steps.length - 6}`) : null,
        never ? ui.badge("Run by hand only", "warn") : null),
      el("div", { class: "au-card__foot" },
        el("div", { class: "au-card__actions" }, runBtn,
          ui.button({ label: "History", icon: "history", variant: "tonal", size: "sm", iconOnly: true, onClick: () => historySheet(r) }),
          ui.button({ label: "Edit", icon: "edit", variant: "tonal", size: "sm", iconOnly: true, onClick: () => openEditor(r) }))));
  }
  function replace(r) {
    const i = S.routines.findIndex((x) => x.id === r.id);
    if (i >= 0) S.routines[i] = r; else S.routines.unshift(r);
    render();
  }

  // ---- templates --------------------------------------------------------------------------------
  function renderTemplates() {
    if (!S.templates) {
      if (S.templatesError) list.replaceChildren(ui.notice(S.templatesError, { tone: "bad" }));
      else { list.replaceChildren(ui.skeleton("card", 3)); loadTemplates(); }
      return;
    }
    const have = new Set((S.routines || []).map((r) => r.name.toLowerCase()));
    list.replaceChildren(el("p", { class: "au-lead" }, "Tap Add and it becomes your own routine. You can change anything afterwards."),
      el("div", { class: "au-gallery" }, S.templates.map((t) => {
        const added = have.has(t.name.toLowerCase());
        const btn = ui.button({ label: added ? "Add again" : "Add", icon: added ? "check" : "plus", variant: added ? "tonal" : "primary", size: "sm", onClick: (e) => addTemplate(t, e.currentTarget) });
        return el("article", { class: "ui-card au-tpl" },
          el("div", { class: "au-tpl__top" }, ui.tile(ICONS[t.icon] || "zap", ICON_TONES[t.icon] || "accent"), el("h3", {}, t.name), t.dangerous ? ui.badge("Locks screen", "warn") : null),
          el("p", { class: "ui-clamp-2" }, t.description),
          el("div", { class: "au-tpl__steps" }, t.steps.map((s) => el("span", { class: "au-chip" }, typeLabel(s.type)))),
          el("div", { class: "au-tpl__foot" }, el("span", { class: "au-tpl__when" }, t.suggested_schedule ? scheduleWords(t.suggested_schedule) : "On demand"), btn));
      })));
  }
  async function addTemplate(t, btn) {
    if (t.dangerous && !(await ui.confirm({ title: `Add "${t.name}"?`, message: "This routine includes a step that affects the laptop itself (like locking the screen). It only runs when you say so.", confirmLabel: "Add routine", danger: true }))) return;
    const body = { name: t.name, icon: t.icon, description: t.description, steps: t.steps, enabled: !t.dangerous && !!t.suggested_schedule };
    if (t.suggested_schedule) body.schedule = t.suggested_schedule;
    if (t.dangerous) body.confirm = true;
    try {
      const r = await ui.busy(btn, api("/api/automations", { method: "POST", json: body }));
      S.routines.unshift(r);
      ui.haptic("success");
      ui.toast(`Added ${r.name}`, { kind: "good", action: { label: "Open", onClick: () => { seg.value = "routines"; S.section = "routines"; render(); } } });
      render();
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
  }

  // ---- run and results --------------------------------------------------------------------------
  async function runRoutine(r, btn) {
    const never = (r.steps || []).some((s) => s.type === "laptop_action" && neverSet().has(s.action_id));
    let confirmed = false;
    if (never) {
      if (!(await ui.confirm({ title: `Run "${r.name}" now?`, message: "This routine can shut down, restart or close programs on your laptop. Only run it if you mean to.", confirmLabel: "Run it", danger: true }))) return;
      confirmed = true;
    }
    S.running.add(r.id); render();
    try {
      let result;
      try { result = await api(`/api/automations/${encodeURIComponent(r.id)}/run`, { method: "POST", json: { wait: true, ...(confirmed ? { confirm: true } : {}) } }); }
      catch (error) {
        if (error instanceof ApiError && error.status === 409 && /confirm/i.test(error.message) && !confirmed) {
          if (!(await ui.confirm({ title: `Run "${r.name}" now?`, message: error.message, confirmLabel: "Run it", danger: true }))) return;
          result = await api(`/api/automations/${encodeURIComponent(r.id)}/run`, { method: "POST", json: { wait: true, confirm: true } });
        } else throw error;
      }
      ui.haptic(result.status === "ok" ? "success" : "warn");
      resultsSheet(r.name, result.status, result.results || []);
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    finally { S.running.delete(r.id); try { await load(); } catch (_) { /* shown by load */ } }
  }
  function resultsSheet(name, status, results, sub) {
    const rows = results.length ? results.map((x) => {
      const tone = x.skipped ? "gray" : x.ok ? "good" : "bad";
      return el("li", { class: "au-result" },
        ui.tile(x.skipped ? "minus" : x.ok ? "check" : "close", tone, "sm"),
        el("div", { class: "au-result__body" }, el("strong", {}, `${x.index != null ? x.index + 1 + ". " : ""}${typeLabel(x.type)}`), x.message ? el("span", {}, x.message) : null),
        x.skipped ? ui.badge("Skipped") : null);
    }) : [el("li", { class: "au-result" }, el("div", { class: "au-result__body" }, el("span", {}, "No step results were recorded.")))];
    ui.sheet({ title: name, class: "au-sheet", content: el("div", { class: "ui-stack" },
      el("div", { class: "au-resulthead" }, ui.badge(STATUS_WORD[status] || status, STATUS_TONE[status]), sub ? el("span", {}, sub) : null),
      el("ol", { class: "au-results" }, rows)) });
  }

  // ---- history ----------------------------------------------------------------------------------
  async function historySheet(r) {
    const body = el("div", { class: "ui-stack" }, ui.skeleton("list", 3));
    ui.sheet({ title: `${r.name} history`, class: "au-sheet", content: body });
    try {
      const runs = (await api(`/api/automations/${encodeURIComponent(r.id)}/runs`)).runs || [];
      body.replaceChildren(runs.length ? el("div", { class: "ui-list ui-list--inset" }, runs.map((run) => ui.listRow({
        icon: run.trigger === "schedule" ? "clock" : "play", tone: STATUS_TONE[run.status] || "gray",
        title: `${STATUS_WORD[run.status] || run.status}`, sub: `${run.trigger === "schedule" ? "Scheduled" : "By hand"} · ${ui.relTime(new Date(run.started))}`,
        meta: `${(run.results || []).filter((x) => x.ok && !x.skipped).length}/${(run.results || []).length} steps`, chevron: true,
        onClick: () => resultsSheet(r.name, run.status, run.results || [], `${run.trigger === "schedule" ? "Scheduled" : "By hand"}, ${ui.relTime(new Date(run.started))}`),
      }))) : ui.emptyState({ icon: "history", title: "Nothing has run yet", body: "Runs show up here with the result of every step.", compact: true }));
    } catch (error) { body.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
  }

  // ---- editor -----------------------------------------------------------------------------------
  async function choices(spec) {
    if (spec === "/api/laptop/actions") {
      if (!S.actions) { try { S.actions = (await api(spec)).actions || []; } catch (_) { S.actions = []; } }
      return S.actions;
    }
    if (spec === "/api/scripts") {
      if (!S.scripts) { try { S.scripts = await api(spec); } catch (_) { S.scripts = []; } }
      return S.scripts;
    }
    return [];
  }

  async function openEditor(routine) {
    if (!S.spec) { try { S.spec = await api("/api/automations/step-types"); } catch (error) { ctx.toast(error.message, { kind: "bad" }); return; } }
    await Promise.all([choices("/api/laptop/actions"), choices("/api/scripts")]);
    const d = routine ? JSON.parse(JSON.stringify(routine)) : { name: "", icon: "bolt", description: "", steps: [], schedule: null, enabled: true };
    d.steps = (d.steps || []).map((s) => ({ ...s }));
    const isNew = !routine;
    const form = el("div", { class: "au-form ui-stack" });
    const errBox = el("div", { class: "au-err" });
    const dangerOf = (s) => {
      if (s.type !== "laptop_action") return false;
      const a = S.actions.find((x) => x.id === s.action_id);
      return (S.spec.dangerous_actions || []).includes(s.action_id) || a?.risk === "confirm";
    };
    const neverOf = (s) => s.type === "laptop_action" && neverSet().has(s.action_id);

    function field(label, input, hint) { return el("label", { class: "ui-field au-field" }, el("span", { class: "ui-label" }, label), input, hint ? el("span", { class: "ui-hint" }, hint) : null); }

    function stepFields(step) {
      const info = typeInfo(step.type);
      const nodes = [];
      for (const f of info?.fields || []) {
        if (f.type === "params") {
          const a = S.actions.find((x) => x.id === step.action_id);
          const specs = a?.params || [];
          if (!specs.length) continue;
          step.params = step.params || {};
          for (const p of specs) {
            const input = el("input", { type: p.type === "number" ? "number" : "text", value: step.params[p.name] ?? "", placeholder: p.placeholder || "", inputmode: p.type === "number" ? "numeric" : null });
            input.addEventListener("input", () => { step.params[p.name] = input.value === "" ? undefined : p.type === "number" ? Number(input.value) : input.value; });
            nodes.push(field(p.label || p.name, input));
          }
          continue;
        }
        let input;
        if (f.choices_from) {
          const opts = f.choices_from === "/api/laptop/actions"
            ? S.actions.map((a) => ({ value: a.id, label: a.title + (a.risk === "confirm" ? " (asks first)" : ""), off: a.available === false }))
            : S.scripts.map((s) => ({ value: s.name, label: s.label || s.name }));
          if (opts.length) {
            input = el("select", {}, el("option", { value: "" }, "Choose…"), opts.map((o) => el("option", { value: o.value, disabled: o.off || null }, o.label)));
            input.value = step[f.name] || "";
            input.addEventListener("change", () => { step[f.name] = input.value; if (f.name === "action_id") { step.params = {}; renderSteps(); renderSchedule(); } });
          }
        }
        if (!input) {
          input = f.type === "textarea" ? el("textarea", { rows: "3", maxlength: f.max_length || null }) : el("input", { type: f.type === "number" ? "number" : "text", inputmode: f.type === "number" ? "numeric" : null, min: f.minimum ?? null, max: f.maximum ?? null, maxlength: f.max_length || null });
          input.placeholder = f.placeholder || "";
          input.value = step[f.name] ?? "";
          input.addEventListener("input", () => { step[f.name] = f.type === "number" ? (input.value === "" ? "" : Number(input.value)) : input.value; });
        }
        nodes.push(field(f.label + (f.required ? "" : " (optional)"), input));
      }
      return nodes;
    }

    const stepsBox = el("div", { class: "au-steps ui-stack" });
    function renderSteps() {
      stepsBox.replaceChildren(...(d.steps.length ? [] : [el("p", { class: "au-hint" }, "No steps yet. Add the first one below.")]), ...d.steps.map((s, i) => {
        const info = typeInfo(s.type);
        const move = (dir) => { const j = i + dir; if (j < 0 || j >= d.steps.length) return; [d.steps[i], d.steps[j]] = [d.steps[j], d.steps[i]]; renderSteps(); };
        const onErr = ui.segmented({ label: "If this fails", value: s.on_error || "stop", options: [{ value: "stop", label: "Stop here" }, { value: "continue", label: "Keep going" }], onChange: (v) => { s.on_error = v; } });
        return el("section", { class: "au-stepcard" },
          el("div", { class: "au-stepcard__head" }, el("span", { class: "au-num ui-num" }, i + 1), ui.tile(STEP_ICONS[s.type] || "circle", "accent", "sm"),
            el("div", { class: "au-stepcard__title" }, el("strong", {}, info?.label || s.type), dangerOf(s) ? ui.badge("Careful", "warn") : null),
            ui.button({ label: "Move up", icon: "arrow-up", variant: "ghost", iconOnly: true, size: "sm", disabled: i === 0, onClick: () => move(-1) }),
            ui.button({ label: "Move down", icon: "arrow-down", variant: "ghost", iconOnly: true, size: "sm", disabled: i === d.steps.length - 1, onClick: () => move(1) }),
            ui.button({ label: "Remove step", icon: "trash", variant: "ghost", iconOnly: true, size: "sm", onClick: () => { d.steps.splice(i, 1); renderSteps(); renderSchedule(); } })),
          info?.description ? el("p", { class: "au-stepcard__desc" }, info.description) : null,
          ...stepFields(s), el("div", { class: "au-label" }, "If this step fails"), onErr);
      }));
    }
    function addStepSheet() {
      const sh = ui.sheet({ title: "Add a step", class: "au-sheet", content: el("div", { class: "ui-list ui-list--inset" }, S.spec.types.map((t) => ui.listRow({
        icon: STEP_ICONS[t.type] || "circle", title: t.label, sub: t.description, chevron: true,
        onClick: () => { d.steps.push({ type: t.type, on_error: "stop" }); sh.close(); renderSteps(); renderSchedule(); requestAnimationFrame(() => stepsBox.lastElementChild?.scrollIntoView({ block: "center", behavior: "smooth" })); },
      }))) });
    }

    // schedule
    const schedBox = el("div", { class: "au-sched ui-stack" });
    function renderSchedule() {
      const never = d.steps.some(neverOf);
      if (never) {
        d.schedule = null;
        schedBox.replaceChildren(ui.notice("Routines that shut down, restart or close programs can only be run by hand, never on a schedule.", { tone: "warn" }));
        return;
      }
      const s = d.schedule;
      const kind = el("select", { "aria-label": "How often" }, KINDS.map(([v, l]) => el("option", { value: v }, l)));
      kind.value = s?.kind || "";
      kind.addEventListener("change", () => {
        const k = kind.value;
        d.schedule = !k ? null : k === "interval" ? { kind: k, every_minutes: 60 } : k === "once" ? { kind: k, at: defaultAt() } : k === "weekly" ? { kind: k, time: "09:00", days: ["mon"] } : { kind: k, time: "09:00" };
        renderSchedule();
      });
      const parts = [field("Repeat", kind)];
      if (s && ["daily", "weekdays", "weekly"].includes(s.kind)) {
        const t = el("input", { type: "time", value: s.time || "09:00" });
        t.addEventListener("input", () => { s.time = t.value; });
        parts.push(field("At (laptop time)", t));
      }
      if (s?.kind === "weekly") {
        const chips = DAYS.map(([k, l]) => {
          const b = el("button", { type: "button", class: "ui-chip" + (s.days.includes(k) ? " is-on" : ""), "aria-pressed": String(s.days.includes(k)) }, l);
          b.addEventListener("click", () => { s.days = s.days.includes(k) ? s.days.filter((x) => x !== k) : [...s.days, k]; renderSchedule(); });
          return b;
        });
        parts.push(el("div", { class: "au-days", role: "group", "aria-label": "Days" }, chips));
      }
      if (s?.kind === "interval") {
        const hours = s.every_minutes % 60 === 0 && s.every_minutes >= 60;
        const n = el("input", { type: "number", inputmode: "numeric", min: hours ? 1 : 5, max: hours ? 24 : 1440, value: hours ? s.every_minutes / 60 : s.every_minutes });
        const unit = el("select", { "aria-label": "Unit" }, el("option", { value: "m" }, "minutes"), el("option", { value: "h" }, "hours"));
        unit.value = hours ? "h" : "m";
        const sync = () => { s.every_minutes = Math.round(Number(n.value || 0) * (unit.value === "h" ? 60 : 1)); };
        n.addEventListener("input", sync);
        unit.addEventListener("change", () => { sync(); renderSchedule(); });
        parts.push(el("div", { class: "au-every" }, field("Every", n), field("Unit", unit)));
      }
      if (s?.kind === "once") {
        const a = el("input", { type: "datetime-local", value: s.at || "" });
        a.addEventListener("input", () => { s.at = a.value; });
        parts.push(field("When", a));
      }
      schedBox.replaceChildren(...parts);
    }
    function defaultAt() { const t = new Date(Date.now() + 3600000); return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}T${pad(t.getHours())}:00`; }

    const nameIn = el("input", { type: "text", maxlength: "60", placeholder: "Good morning", value: d.name, autocomplete: "off" });
    nameIn.addEventListener("input", () => { d.name = nameIn.value; });
    const descIn = el("input", { type: "text", maxlength: "300", placeholder: "What it is for (optional)", value: d.description || "" });
    descIn.addEventListener("input", () => { d.description = descIn.value; });
    const iconRow = el("div", { class: "au-icons", role: "radiogroup", "aria-label": "Icon" });
    function renderIcons() {
      iconRow.replaceChildren(...Object.keys(ICONS).map((k) => {
        const b = el("button", { type: "button", role: "radio", "aria-checked": String(d.icon === k), "aria-label": k, class: "au-iconpick" + (d.icon === k ? " is-on" : "") }, ui.icon(ICONS[k]));
        b.addEventListener("click", () => { d.icon = k; renderIcons(); });
        return b;
      }));
    }
    renderIcons(); renderSteps(); renderSchedule();
    const enabledSw = ui.switchControl({ checked: d.enabled !== false, label: "Turn on the schedule", onChange: (v) => { d.enabled = v; } });
    form.append(errBox, field("Name", nameIn), field("Description", descIn), iconRow,
      el("h3", { class: "au-h" }, "Steps"), stepsBox, ui.button({ label: "Add a step", icon: "plus", variant: "tonal", block: true, onClick: addStepSheet }),
      el("h3", { class: "au-h" }, "Schedule"), schedBox,
      el("div", { class: "ui-row au-toggle" }, el("div", { class: "ui-row__body" }, el("div", { class: "ui-row__title" }, "Schedule on"), el("div", { class: "ui-row__sub" }, "Off keeps the routine but never starts it by itself")), enabledSw),
      isNew ? null : ui.button({ label: "Delete routine", icon: "trash", variant: "danger-soft", block: true, onClick: () => del() }));

    function validate() {
      if (!d.name.trim()) return "Give the routine a name.";
      if (!d.steps.length) return "Add at least one step.";
      for (const [i, s] of d.steps.entries()) {
        for (const f of typeInfo(s.type)?.fields || []) {
          if (f.type === "params") continue;
          const v = s[f.name];
          if (f.required && (v === undefined || v === "" || v === null || Number.isNaN(v))) return `Step ${i + 1} (${typeLabel(s.type)}): fill in "${f.label}".`;
          if (f.type === "number" && v !== "" && v !== undefined) {
            if (f.minimum != null && v < f.minimum) return `Step ${i + 1}: "${f.label}" must be at least ${f.minimum}.`;
            if (f.maximum != null && v > f.maximum) return `Step ${i + 1}: "${f.label}" must be at most ${f.maximum}.`;
          }
        }
        const a = S.actions.find((x) => x.id === s.action_id);
        for (const p of a?.params || []) if (p.required && (s.params?.[p.name] === undefined || s.params[p.name] === "")) return `Step ${i + 1}: fill in "${p.label || p.name}".`;
      }
      const sc = d.schedule;
      if (sc?.kind === "weekly" && !sc.days.length) return "Pick at least one day.";
      if (sc?.kind === "interval" && (sc.every_minutes < 5 || sc.every_minutes > 1440)) return "Repeat every 5 minutes to 24 hours.";
      if (sc?.kind === "once" && (!sc.at || new Date(sc.at) <= new Date())) return "Pick a time in the future.";
      if (sc && !sc.time && sc.kind !== "interval" && sc.kind !== "once") return "Pick a time.";
      return "";
    }
    function clean(s) {
      const out = { type: s.type, on_error: s.on_error || "stop" };
      for (const f of typeInfo(s.type)?.fields || []) {
        if (f.type === "params") { const p = Object.fromEntries(Object.entries(s.params || {}).filter(([, v]) => v !== undefined && v !== "")); if (s.type === "laptop_action") out.params = p; continue; }
        const v = s[f.name];
        if (v === undefined || v === "" || (typeof v === "number" && Number.isNaN(v))) continue;
        out[f.name] = f.type === "number" ? Number(v) : v;
      }
      return out;
    }
    async function save(sh) {
      errBox.replaceChildren();
      const problem = validate();
      if (problem) { errBox.replaceChildren(ui.notice(problem, { tone: "bad" })); errBox.scrollIntoView({ block: "nearest" }); ui.haptic("error"); return false; }
      const body = { name: d.name.trim(), icon: d.icon, description: d.description || "", steps: d.steps.map(clean), schedule: d.schedule, enabled: d.enabled !== false };
      if (d.steps.some(dangerOf)) {
        const ok = await ui.confirm({ title: "This routine affects your laptop", message: "One of its steps can lock the screen, put the laptop to sleep or open things on it. Save it only if you trust it.", confirmLabel: "Save anyway", danger: true });
        if (!ok) return false;
        body.confirm = true;
      }
      try {
        const r = await api(isNew ? "/api/automations" : `/api/automations/${encodeURIComponent(routine.id)}`, { method: isNew ? "POST" : "PUT", json: body });
        replace(r); ui.haptic("success"); ui.toast(isNew ? "Routine added" : "Saved", { kind: "good" });
        return true;
      } catch (error) { errBox.replaceChildren(ui.notice(error.message, { tone: "bad" })); errBox.scrollIntoView({ block: "nearest" }); return false; }
    }
    let sheetRef;
    async function del() {
      if (!(await ui.confirm({ title: `Delete "${routine.name}"?`, message: "Its history goes too. This can't be undone.", confirmLabel: "Delete", danger: true }))) return;
      try { await api(`/api/automations/${encodeURIComponent(routine.id)}`, { method: "DELETE" }); S.routines = S.routines.filter((x) => x.id !== routine.id); render(); sheetRef?.close(); ui.toast("Deleted", { kind: "good" }); }
      catch (error) { errBox.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
    }
    sheetRef = ui.sheet({ title: isNew ? "New routine" : "Edit routine", class: "au-sheet au-editor", content: form,
      actions: [{ label: isNew ? "Add routine" : "Save", icon: "check", variant: "primary", onClick: (s) => save(s) }] });
  }

  // ---- lifecycle --------------------------------------------------------------------------------
  function show() {
    visible = true;
    load();
    clearInterval(pollTimer);
    pollTimer = setInterval(() => { if (visible && !document.hidden && S.section === "routines" && !document.querySelector(".ui-sheet")) load(); }, 20000);
  }
  function hide() { visible = false; clearInterval(pollTimer); }
  render();
  return {
    show, hide,
    refresh: () => load(),
    openSection(name) { if (name === "templates" || name === "routines") { S.section = name; seg.value = name; render(); } },
    async openItem(id) {
      if (!S.routines) await load();
      const r = S.routines?.find((x) => x.id === id);
      if (!r) return;
      S.section = "routines"; seg.value = "routines"; render();
      const node = list.querySelector(`[data-id="${CSS.escape(id)}"]`);
      node?.scrollIntoView({ block: "center", behavior: "smooth" });
      node?.classList.add("is-flash"); setTimeout(() => node?.classList.remove("is-flash"), 1600);
    },
  };
}
