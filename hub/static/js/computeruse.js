// Computer: Jarvis looks at the laptop screen and clicks and types in apps YOU approve, one step at a time. OFF until you turn it on.
// Routes: docs/api/computeruse.md. Every string goes in with textContent (el()), never as markup. A big red Stop is always visible.
// Flow: goal + scope -> approval card (the exact scope, with a digest) -> live view -> per-step taps for anything destructive.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";

const CSS = "/css/computeruse.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const loadCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };
const STATE = { pending: "Waiting for your approval", running: "Working", paused: "Paused", waiting_confirm: "Waiting for your tap", done: "Done",
  stopped: "Stopped", failed: "Failed", expired: "Expired" };
const TONE = { running: "good", done: "good", paused: "warn", waiting_confirm: "warn", pending: "warn", stopped: "", failed: "bad", expired: "" };

export function init(container, ctx) {
  loadCss();
  const root = el("div", { class: "cu ui-stack ui-stack--lg" });
  container.append(root);
  let st = null, loadError = "", timer = null, windows = null, draft = { goal: "", apps: new Set(), steps: 25, typing: false, provider: "" }, busyNew = false;
  let shotFor = "", shotAt = 0, lastSig = "", loadingWin = false;

  const stopTimer = () => { clearTimeout(timer); timer = null; };
  const schedule = (ms) => { stopTimer(); timer = setTimeout(() => { if (root.isConnected) load(); }, ms); };
  const live = () => (st?.jobs || []).find((j) => ["running", "paused", "waiting_confirm"].includes(j.state)) || null;
  const pending = () => (st?.jobs || []).find((j) => j.state === "pending") || null;

  async function load() {
    try { st = await api("/api/computer/status"); loadError = ""; }
    catch (error) { loadError = error instanceof ApiError && error.status === 404 ? "off" : (error.message || "Could not load."); }
    const sig = JSON.stringify(st) + loadError;
    if (live() || sig !== lastSig) paint();           // an idle screen is not repainted under the owner's typing
    lastSig = sig;
    schedule(live() ? 1200 : pending() ? 3000 : 8000);
  }
  async function post(path, json, message) {
    try { await api(path, { method: "POST", json: json ?? {} }); if (message) ui.toast(message, { kind: "good" }); } catch (error) { fail(error); }
    lastSig = "";
    await load();
  }
  async function setSetting(body, message) {
    try { st = await api("/api/computer/settings", { method: "PUT", json: body }); if (message) ui.toast(message, { kind: "good" }); } catch (error) { fail(error); }
    lastSig = "";
    paint();
  }

  // ---- chips + the big red Stop ---------------------------------------------------------------------------
  function chips() {
    const row = el("div", { class: "cu-chips" });
    const add = (text, tone) => row.append(ui.badge(text, tone));
    if (!st) return row;
    add(st.enabled ? "On" : "Off", st.enabled ? "good" : "");
    if (st.panic) add("Blocked: Panic", "bad");
    if (st.unattended_blocked) add("Unattended: blocked", "warn");
    else if (st.presence === "away") add("Unattended: allowed", "warn");
    const j = live();
    if (j?.state === "paused") add("Paused", "warn");
    if (j?.state === "waiting_confirm") add("Needs your tap", "warn");
    if (j) add(j.cloud ? "Cloud AI sees the screen" : "Local AI", j.cloud ? "warn" : "good");
    if (!st.available?.ok) add("Not ready", "bad");
    if (st.available?.ok && !st.available.uia) add("Coordinates only", "");
    return row;
  }

  function stopBar() {
    const j = live();
    if (!j && !pending()) return null;
    return el("div", { class: "cu-stopbar" },
      ui.button({ label: "STOP", icon: "stop", variant: "danger", block: true, class: "cu-stop", onClick: async () => { await post("/api/computer/stop-all", {}, "Stopped."); } }),
      el("p", { class: "ui-hint" }, `Emergency: throw the mouse into the top-left corner, or press ${st.hotkey || "Ctrl+Alt+Shift+Esc"}.`));
  }

  // ---- off / warning ----------------------------------------------------------------------------------------
  function offCard() {
    return el("section", { class: "ui-card ui-card--accent cu-card ui-stack" }, ui.eyebrow("01", "Computer control"),
      el("p", { class: "cu-lead" }, "Let Jarvis use this PC for you, step by step."),
      ui.notice("Careful: this moves the real mouse and types on the real keyboard. It only works in apps you pick, asks before anything that deletes, sends, pays or installs, "
        + "and never touches password fields. Try it first in Notepad.", { tone: "warn", icon: "alert" }),
      !st.available?.ok ? ui.notice(st.available?.reason || "This computer cannot be controlled yet.", { tone: "bad" }) : null,
      ui.button({ label: "Turn on computer control", icon: "check", variant: "primary", onClick: async () => {
        if (!(await ui.confirm({ title: "Turn on computer control?", message: "Jarvis will be able to propose clicks and typing on this laptop. Nothing runs until you approve each job's scope, "
          + "and the red Stop is always on screen.", confirmLabel: "Turn on", icon: "alert" }))) return;
        setSetting({ enabled: true, confirm: true }, "Computer control is on. Nothing runs until you approve a task.");
      } }));
  }

  // ---- new task -----------------------------------------------------------------------------------------------
  async function loadWindows() {
    if (loadingWin) return;
    loadingWin = true;
    try { windows = (await api("/api/computer/windows")).apps || []; } catch (error) { windows = []; ui.toast(error.message || "Could not list the open apps.", { kind: "warn" }); }
    loadingWin = false;
    paint();
  }

  function newTask() {
    const goal = el("textarea", { class: "ui-input cu-goal", rows: "3", maxlength: "500", placeholder: "For example: in Notepad, type 'hello' and save it as test.txt on the Desktop", "aria-label": "What should Jarvis do?" });
    goal.value = draft.goal;
    goal.addEventListener("input", () => { draft.goal = goal.value; });
    const provs = st.providers || [];
    const sel = el("select", { class: "ui-input", "aria-label": "Which AI looks at the screen" },
      el("option", { value: "" }, provs.some((p) => p.local) ? "A local model on this laptop (recommended)" : "No local model set up"),
      ...provs.map((p) => el("option", { value: p.id }, `${p.label}${p.cloud ? " (cloud: screenshots leave the laptop)" : " (local)"}`)));
    sel.value = draft.provider;
    sel.addEventListener("change", () => { draft.provider = sel.value; paint(); });
    const cloud = provs.find((p) => p.id === draft.provider)?.cloud;
    const appsBox = el("div", { class: "cu-apps" });
    if (windows === null) appsBox.append(el("p", { class: "ui-hint" }, "Loading open apps..."));
    else if (!windows.length) appsBox.append(el("p", { class: "ui-hint" }, "No open windows were found. Open the app you want Jarvis to use, then refresh."));
    for (const a of windows || []) {
      const cb = el("input", { type: "checkbox", id: "cu-app-" + a.app, "aria-label": `Allow ${a.app}` });
      cb.checked = draft.apps.has(a.app) && !a.blocked; cb.disabled = !!a.blocked;
      cb.addEventListener("change", () => { cb.checked ? draft.apps.add(a.app) : draft.apps.delete(a.app); });
      appsBox.append(el("label", { class: "cu-app" + (a.blocked ? " is-blocked" : ""), for: "cu-app-" + a.app }, cb,
        el("span", { class: "cu-app__body" }, el("span", { class: "cu-app__name" }, a.app), el("span", { class: "ui-hint" }, a.blocked ? `Never controlled: ${a.blocked}` : a.titles.join(" | ")))));
    }
    const steps = el("input", { class: "ui-input cu-num", type: "number", min: "1", max: "100", "aria-label": "Maximum steps" });
    steps.value = String(draft.steps || st.settings.max_steps);
    steps.addEventListener("input", () => { draft.steps = parseInt(steps.value, 10) || st.settings.max_steps; });
    const typing = ui.switchControl({ checked: draft.typing, label: "Allow typing", onChange: (v) => { draft.typing = v; } });
    const review = ui.button({ label: "Review the scope", icon: "shield", variant: "primary", block: true, disabled: busyNew || !st.available?.ok, onClick: async () => {
      if (!draft.goal.trim()) { ui.toast("Tell Jarvis what to do first.", { kind: "warn" }); return; }
      if (!draft.apps.size) { ui.toast("Pick at least one app Jarvis may use.", { kind: "warn" }); return; }
      busyNew = true;
      try {
        await api("/api/computer/jobs", { method: "POST", json: { goal: draft.goal.trim(), scope: { apps: [...draft.apps], max_steps: draft.steps || st.settings.max_steps, allow_typing: draft.typing, provider: draft.provider } } });
        draft = { goal: "", apps: new Set(), steps: st.settings.max_steps, typing: false, provider: "" };
      } catch (error) { fail(error); }
      busyNew = false;
      await load();
    } });
    return el("section", { class: "ui-card cu-card ui-stack" }, ui.eyebrow("01", "New task"),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "What should Jarvis do?"), goal),
      el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Which open apps may Jarvis use?"), appsBox,
        ui.button({ label: "Refresh the list", icon: "refresh", variant: "ghost", size: "sm", onClick: loadWindows })),
      el("div", { class: "ui-row cu-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Allow typing"),
        el("span", { class: "ui-row__sub" }, "Off: Jarvis can only click, scroll and use arrow keys.")), typing),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Most steps (each one is a look at the screen and one action)"), steps),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Which AI looks at the screen?"), sel),
      cloud ? ui.notice("This is a cloud AI. Pictures of your screen are sent to it (password fields are hidden first). Choose a local model to keep them on the laptop.", { tone: "warn", icon: "cloud" }) : null,
      review);
  }

  // ---- approval ------------------------------------------------------------------------------------------------
  function approvalCard(j) {
    const facts = el("ul", { class: "cu-facts" },
      el("li", {}, `Apps: ${j.scope.apps.join(", ")}`), el("li", {}, `At most ${j.scope.max_steps} steps, ${Math.round(j.scope.max_seconds / 60)} minutes`),
      el("li", {}, j.scope.allow_typing ? "Typing is allowed" : "Typing is not allowed"), el("li", {}, `AI: ${j.provider} (${j.cloud ? "cloud" : "local"})`),
      el("li", {}, "Deleting, sending, paying, installing and security changes ask you first, each time"));
    return el("section", { class: "ui-card ui-card--accent cu-card ui-stack", role: "group", "aria-label": "Approve this scope" }, ui.eyebrow("02", "Approve this scope"),
      el("p", { class: "cu-lead" }, j.summary), el("p", { class: "cu-goalshow" }, `Task: ${j.goal}`), facts,
      j.cloud ? ui.notice(j.cloud_note, { tone: "warn", icon: "cloud" }) : null,
      st.unattended_blocked ? ui.notice("You look away from the laptop, so this will not start (unattended PCs are not driven).", { tone: "bad" }) : null,
      el("p", { class: "ui-hint" }, j.source === "assistant" ? "Jarvis asked for this. Only you can approve it." : "Check the apps and limits above, then approve."),
      el("div", { class: "cu-actions" },
        ui.button({ label: "Approve and start", icon: "check", variant: "primary", onClick: async () => { await post(`/api/computer/jobs/${j.id}/approve`, { digest: j.digest }, "Started. Watch below; Stop is always on screen."); } }),
        ui.button({ label: "Cancel", variant: "ghost", onClick: () => post(`/api/computer/jobs/${j.id}/stop`, {}) })));
  }

  // ---- live ---------------------------------------------------------------------------------------------------
  function shot(j) {
    if (!j.has_shot) return el("p", { class: "ui-hint" }, "Waiting for the first picture...");
    const img = el("img", { class: "cu-img", alt: "What Jarvis sees on the laptop right now", decoding: "async" });
    if (shotFor !== j.id || Date.now() - shotAt > 900) { shotFor = j.id; shotAt = Date.now(); }
    img.src = `/api/computer/shots/${j.id}/latest?t=${shotAt}`;
    const wrap = el("div", { class: "cu-shot" }, img);
    const p = j.planned;
    if (p && p.x != null && p.img_w) {
      wrap.append(el("span", { class: "cu-mark", style: `left:${Math.min(100, Math.max(0, (p.x / p.img_w) * 100)).toFixed(2)}%;top:${Math.min(100, Math.max(0, (p.y / p.img_h) * 100)).toFixed(2)}%`, "aria-hidden": "true" }));
    }
    if (p) wrap.append(el("span", { class: "cu-plan" }, `Next: ${p.action}${p.reason ? " - " + p.reason : ""}`));
    return wrap;
  }

  function stepLog(j) {
    const rows = (j.steps || []).slice(-14).reverse().map((s) => el("li", { class: "cu-step" + (/^refused|rejected|failed|declined/.test(s.outcome || "") ? " is-bad" : "") },
      el("span", { class: "cu-step__n" }, String(s.step)), el("span", { class: "cu-step__body" },
        el("span", { class: "cu-step__what" }, `${s.action || "-"} ${s.target && s.target !== "-" ? s.target : ""}`.trim()),
        el("span", { class: "ui-hint" }, `${s.outcome}${s.window ? " - " + s.window : ""}`)),
      ui.badge(s.level === "confirm" ? "your tap" : s.level, s.level === "confirm" ? "warn" : "")));
    return el("ol", { class: "cu-steps", "aria-label": "Steps so far" }, ...(rows.length ? rows : [el("li", { class: "ui-hint" }, "No steps yet.")]));
  }

  function liveCard(j) {
    const parts = [el("div", { class: "cu-head" }, ui.badge(STATE[j.state] || j.state, TONE[j.state] || ""), el("span", { class: "ui-hint" }, `Step ${j.step} of ${j.max_steps}`)),
      el("p", { class: "cu-goalshow" }, j.goal), shot(j)];
    if (j.state === "waiting_confirm" && j.confirm) {
      const c = j.confirm;
      parts.push(el("div", { class: "cu-confirm", role: "alertdialog", "aria-label": "Jarvis needs your tap" },
        el("p", { class: "cu-lead" }, "Jarvis wants to do something that needs your tap"),
        el("p", {}, `${c.action} ${c.target !== "-" ? c.target : ""}`.trim()), el("p", { class: "ui-hint" }, `In: ${c.window} (${c.app})`),
        c.reason ? el("p", { class: "ui-hint" }, `Why: ${c.reason}`) : null, el("p", { class: "ui-hint" }, `Looks like: ${c.why.join(", ")}`),
        el("div", { class: "cu-actions" },
          ui.button({ label: "Allow this one step", icon: "check", variant: "primary", onClick: () => post(`/api/computer/jobs/${j.id}/confirm-step`, { step: c.step, confirm: true, digest: c.digest }) }),
          ui.button({ label: "No", variant: "secondary", onClick: () => post(`/api/computer/jobs/${j.id}/confirm-step`, { step: c.step, confirm: false, digest: c.digest }) }))));
    }
    if (j.state === "paused") {
      parts.push(el("div", { class: "cu-paused", role: "status" }, el("p", { class: "cu-lead" }, j.ask ? "Jarvis needs you" : "Paused"),
        j.ask ? el("p", {}, j.ask) : null, el("p", { class: "ui-hint" }, j.pause_reason),
        el("div", { class: "cu-actions" }, ui.button({ label: "Resume", icon: "play", variant: "primary", onClick: () => post(`/api/computer/jobs/${j.id}/resume`) }))));
    }
    if (j.state === "running") {
      parts.push(el("div", { class: "cu-actions" },
        ui.button({ label: "Pause", icon: "pause", variant: "secondary", onClick: () => post(`/api/computer/jobs/${j.id}/pause`) }),
        ui.button({ label: "Take over", icon: "crosshair", variant: "secondary", onClick: () => post(`/api/computer/jobs/${j.id}/takeover`, {}, "The mouse and keyboard are yours. Tap Resume to hand them back.") })));
    }
    parts.push(stepLog(j));
    return el("section", { class: "ui-card cu-card cu-card--live ui-stack", "aria-live": "polite" }, ui.eyebrow("03", "Live"), ...parts);
  }

  function pastCard() {
    const rows = (st.jobs || []).filter((j) => !["pending", "running", "paused", "waiting_confirm"].includes(j.state)).slice(0, 8).map((j) => ui.listRow({
      icon: "task", tone: j.state === "done" ? "accent" : "warn", title: j.goal.slice(0, 80), sub: `${j.actions} actions, ${j.outcome || ""}`.slice(0, 120), meta: ui.badge(STATE[j.state] || j.state, TONE[j.state] || "") }));
    if (!rows.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("04", "Earlier tasks"), el("div", { class: "ui-card ui-card--flat cu-list" }, ...rows));
  }

  // ---- settings -----------------------------------------------------------------------------------------------
  function settingsFold() {
    const s = st.settings;
    const unatt = ui.switchControl({ checked: s.allow_unattended, label: "Allow it while I am away", onChange: (v) => setSetting({ allow_unattended: v }, v ? "It may now run while you are away. Not recommended." : "Off.") });
    const keep = ui.switchControl({ checked: s.keep_shots, label: "Keep the last pictures after a task", onChange: (v) => setSetting({ keep_shots: v }, v ? "The last 20 pictures stay on this laptop after a task." : "Pictures are deleted when a task ends.") });
    const max = el("input", { class: "ui-input cu-num", type: "number", min: "1", max: "100", "aria-label": "Default step limit" });
    max.value = String(s.max_steps);
    max.addEventListener("change", () => setSetting({ max_steps: parseInt(max.value, 10) || 25 }, "Saved."));
    const editor = listEditor({ title: "Never controlled", hint: "A window whose title or program contains one of these words is never controlled, even inside an app you allowed.",
      items: (s.blocklist || []).map((w) => ({ id: w })), rowOf: (i) => ({ icon: "lock", tone: "warn", title: i.id }),
      onAdd: () => formSheet({ title: "Never control", fields: [{ key: "w", label: "Word in the window title or program name", type: "text", maxlength: 60, placeholder: "banking" }],
        onSave: async (v) => { const w = (v.w || "").trim().toLowerCase(); if (w.length < 2) { ui.toast("Write a word of at least two letters.", { kind: "warn" }); return false; }
          await setSetting({ blocklist: [...new Set([...(s.blocklist || []), w])] }, "Added."); } }),
      onDelete: async (i) => { await setSetting({ blocklist: (s.blocklist || []).filter((w) => w !== i.id) }, "Removed."); },
      onReset: async () => { await setSetting({ blocklist: null }, "Back to the built-in list."); }, addLabel: "Never control a word", resetLabel: "Back to the built-in list", empty: "Nothing is blocked. Not recommended." });
    return el("details", { class: "ui-card cu-card" }, el("summary", {}, "Settings and safety"),
      el("div", { class: "ui-stack cu-settings" },
        el("div", { class: "ui-row cu-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Allow it while I am away"),
          el("span", { class: "ui-row__sub" }, "Off (recommended): it will not start, and pauses, when the laptop says you are not there.")), unatt),
        el("div", { class: "ui-row cu-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Keep the last 20 pictures"),
          el("span", { class: "ui-row__sub" }, "Off: pictures live in memory and a folder that is deleted when the task ends.")), keep),
        el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Default most steps"), max),
        editor.el,
        ui.button({ label: "Turn computer control off", variant: "ghost", onClick: () => setSetting({ enabled: false }, "Computer control is off.") })));
  }

  // ---- paint ---------------------------------------------------------------------------------------------------
  function paint() {
    const kids = [];
    if (!st) { root.replaceChildren(loadError ? (loadError === "off" ? ui.notice("Computer control is not on this hub.", { tone: "warn" }) : ui.notice(loadError, { tone: "bad" })) : ui.skeleton("list", 2)); return; }
    kids.push(stopBar(), chips());
    const j = live(), p = pending();
    if (!st.enabled) kids.push(offCard());
    else if (j) kids.push(liveCard(j));
    else if (p) kids.push(approvalCard(p));
    else {
      if (windows === null) loadWindows();
      kids.push(newTask());
    }
    if (st.enabled) kids.push(pastCard(), settingsFold());
    root.replaceChildren(...kids.filter(Boolean));
  }

  load();
  return { destroy() { stopTimer(); }, refresh: load };
}
