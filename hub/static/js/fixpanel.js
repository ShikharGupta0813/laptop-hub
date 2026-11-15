// "Fix it" kit for the Friday and Jarvis screens: Voice on/off + Test voice, Fix-it checklist, Provider check (a Test
// button per AI) and a live Log viewer with a Doctor button. Backend: /api/fixit/* (hub/fixit.py, docs/api/fixit.md).
//   const kit = createFixKit(ctx, { assistant: "friday", name: "Friday" }); container.append(kit.node); kit.refresh();
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { voiceControls, voiceStatus } from "./voicekit.js";

const CSS = "/css/fixkit.css";
const LOG_MS = 3000;
const SOURCES = [["jarvis", "Jarvis log"], ["output", "Jarvis start-up output"], ["hub", "Hub console"]];

function ensureCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

const dot = (state) => el("span", { class: `ui-dot ui-dot--${state === true ? "good" : state === false ? "bad" : ""}`, "aria-hidden": "true" });

// What only the browser can know: secure address, microphone permission, a phone voice.
export async function browserChecks(assistant) {
  const items = [];
  const secure = window.isSecureContext;
  items.push({ id: "https", label: "Secure (HTTPS) address", ok: secure, detail: secure ? "Using HTTPS." : "Plain http: the microphone is blocked.",
    step: secure ? "" : "Open the hub by its Tailscale HTTPS address (on the laptop: tailscale serve --bg 8765), not the plain address." });
  let mic = null, micDetail = "Permission is asked the first time you tap the microphone.";
  try {
    if (navigator.permissions?.query) {
      const st = await navigator.permissions.query({ name: "microphone" });
      mic = st.state === "granted" ? true : st.state === "denied" ? false : null;
      micDetail = st.state === "granted" ? "Allowed." : st.state === "denied" ? "Blocked for this site." : micDetail;
    }
  } catch (_) { /* Safari and some WebViews cannot say */ }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") { mic = false; micDetail = "This browser cannot record audio."; }
  items.push({ id: "mic", label: "Microphone permission", ok: mic, detail: micDetail,
    step: mic === false ? "Tap the lock icon in the address bar, allow Microphone for this site, then reload. (Android app: Settings > Apps > Redline > Permissions > Microphone.)" : "" });
  const v = await voiceStatus(assistant);
  const voiceOk = v.hub || (v.supported && v.voices > 0);
  items.push({ id: "tts-voice", label: "A voice to speak with", ok: voiceOk,
    detail: v.hub ? `Laptop voice (${v.engine}).` : v.supported ? `${v.voices} voice(s) on this phone.` : "This browser cannot speak.",
    step: voiceOk ? "" : "Install a text-to-speech voice: Android Settings > System > Languages > Text-to-speech output (Google Speech Services), or pick a laptop voice in Settings > Voices (Piper or Windows)." });
  return items;
}

export function createFixKit(ctx, { assistant = "jarvis", name = "Jarvis", onStart } = {}) {
  ensureCss();
  const voice = voiceControls({ assistant, name });

  // ---- checklist
  const checkBody = el("div", { class: "fk-list" });
  const checkTitle = el("h3", { class: "ui-card__title" }, "Fix-it checklist");
  const checkCard = el("section", { class: "ui-card fk-card", "aria-label": "Fix-it checklist" }, checkTitle, checkBody);

  function itemRow(i) {
    const row = el("div", { class: `fk-item ${i.ok === false ? "is-bad" : ""}` }, dot(i.ok),
      el("div", { class: "fk-item__txt" }, el("strong", {}, i.label), i.detail ? el("p", { class: "muted" }, i.detail) : null,
        i.ok === false && i.step ? el("p", { class: "fk-step" }, el("b", {}, "Do this: "), i.step) : null));
    const act = i.action;
    if (i.ok === false && act) {
      row.querySelector(".fk-item__txt").append(ui.button({ label: act.label, size: "sm", variant: "tonal", onClick: () => {
        if (act.kind === "settings") ctx.open("settings");
        else if (act.kind === "open") ctx.open(act.target);
        else if (act.kind === "scroll") providerCard.scrollIntoView({ behavior: "smooth", block: "start" });
      } }));
    }
    return row;
  }

  async function loadChecklist() {
    let items = [], error = "";
    try { items = (await api("/api/fixit/checklist")).items || []; } catch (e) { error = e.message || "Could not reach the laptop."; }
    try { items = items.concat(await browserChecks(assistant)); } catch (_) { /* ignore */ }
    const bad = items.filter((i) => i.ok === false).length;
    checkTitle.textContent = bad ? `Fix-it checklist: ${bad} thing${bad === 1 ? "" : "s"} to fix` : "Fix-it checklist: all set";
    checkBody.replaceChildren(...(error ? [ui.notice(`Could not read the laptop's checklist. ${error}`, { tone: "bad" })] : []),
      ...[...items.filter((i) => i.ok === false), ...items.filter((i) => i.ok !== false)].map(itemRow));
  }

  // ---- provider check
  const provBody = el("div", { class: "fk-list" });
  const provSummary = el("p", { class: "fk-summary", role: "status" });
  const checkAll = ui.button({ label: "Test all", icon: "refresh", size: "sm", variant: "tonal", onClick: () => testAll() });
  const providerCard = el("section", { id: "provider-check", class: "ui-card fk-card", "aria-label": "Provider check" },
    el("div", { class: "fk-head" }, el("h3", { class: "ui-card__title" }, "Provider check"), checkAll), provSummary, provBody);
  const rows = new Map();

  function copy(text) {
    try { navigator.clipboard?.writeText(text); ctx.toast("Copied. Paste it in a terminal on the laptop."); } catch (_) { ctx.toast(text); }
  }

  function provRow(p) {
    const result = el("p", { class: "fk-result", role: "status", "aria-live": "polite" });
    const test = ui.button({ label: p.action?.label || "Test", size: "sm", variant: "secondary", onClick: () => runTest(p, test, result) });
    const fix = !p.ready && p.fix ? el("p", { class: "fk-step" }, el("b", {}, "Fix: "), p.fix,
      p.command ? [" ", ui.button({ label: p.command, size: "sm", variant: "ghost", class: "fk-cmd", title: "Copy command", onClick: () => copy(p.command) })] : null) : null;
    const node = el("div", { class: `fk-item ${p.ready ? "" : "is-bad"}` }, dot(p.ready),
      el("div", { class: "fk-item__txt" },
        el("strong", {}, p.label, p.chosen ? el("span", { class: "ui-badge ui-badge--accent fk-chip" }, "answering") : null),
        el("p", { class: "muted" }, p.reason), fix, result),
      test);
    rows.set(p.id, { node, test, result, p });
    return node;
  }

  async function runTest(p, btn, result) {
    result.textContent = "Testing…"; result.className = "fk-result";
    btn.disabled = true;
    try {
      const path = p.action?.test_path || `/api/fixit/providers/${p.id}/test`;
      const r = await api(path, { method: "POST", json: path.includes("/api/settings/models/test") ? { model: "" } : {} });
      if (r.ok) { result.textContent = `Works. It said: "${r.reply || "ok"}"${r.latency_ms ? ` (${(r.latency_ms / 1000).toFixed(1)} s)` : ""}.`; result.className = "fk-result good"; }
      else {
        const say = r.error || r.message || "It did not answer.";
        result.textContent = `${say}${r.fix ? " Fix: " + r.fix : ""}`; result.className = "fk-result bad";
      }
    } catch (e) { result.textContent = e.message || "The test could not run."; result.className = "fk-result bad"; }
    btn.disabled = false;
    return result.classList.contains("good");
  }

  async function testAll() {
    checkAll.disabled = true;
    for (const { p, test, result } of rows.values()) if (p.ready) await runTest(p, test, result);
    checkAll.disabled = false;
    loadProviders(true);
  }

  async function loadProviders(keep) {
    let data;
    try { data = await api("/api/fixit/providers"); } catch (e) {
      provSummary.textContent = `Could not read providers. ${e.message || ""}`; provSummary.className = "fk-summary bad"; return;
    }
    provSummary.textContent = data.summary || "";
    provSummary.className = "fk-summary" + (data.providers.some((p) => p.chosen) ? " good" : " bad");
    const keepText = new Map([...rows].map(([k, v]) => [k, [v.result.textContent, v.result.className]]));
    rows.clear();
    provBody.replaceChildren(...data.providers.map(provRow));
    if (keep) for (const [k, v] of rows) { const t = keepText.get(k); if (t) { v.result.textContent = t[0]; v.result.className = t[1]; } }
  }

  // ---- live log + doctor
  let source = "jarvis", live = true, timer = null, fetching = false;
  const logPre = el("pre", { class: "ap-log fk-log", tabindex: "0", role: "log", "aria-label": "Live log" }, "Loading…");
  const logNote = el("p", { class: "muted fk-note" });
  const sel = el("select", { class: "ui-input fk-select", "aria-label": "Which log", onchange: () => { source = sel.value; loadLog(); } },
    ...SOURCES.map(([v, l]) => el("option", { value: v }, l)));
  const liveSw = ui.switchControl({ checked: true, label: "Auto-refresh", onChange: (on) => { live = on; schedule(); } });
  const doctorBtn = ui.button({ label: "Doctor", size: "sm", variant: "tonal", onClick: () => runDoctor() });
  const doctorOut = el("div", { class: "fk-list", hidden: true });
  const logCard = el("section", { class: "ui-card fk-card", "aria-label": "Live log" },
    el("h3", { class: "ui-card__title" }, "Live log"),
    el("div", { class: "fk-row" }, sel, el("label", { class: "fk-switch" }, liveSw, el("span", {}, "Auto-refresh")), doctorBtn),
    logNote, logPre, doctorOut);

  async function loadLog() {
    if (fetching) return;
    fetching = true;
    try {
      const d = await api(`/api/fixit/log?source=${encodeURIComponent(source)}&lines=120`);
      const atBottom = logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 30;
      const text = d.lines.length ? d.lines.join("\n") : (d.note || "Nothing logged yet.");
      if (logPre.textContent !== text) { logPre.textContent = text; if (atBottom || logPre.textContent.length < 400) logPre.scrollTop = logPre.scrollHeight; }
      logNote.textContent = d.lines.length ? "" : (d.note || "");
    } catch (e) { logPre.textContent = `Could not read the log. ${e.message || ""}`; }
    fetching = false;
  }

  async function runDoctor() {
    doctorOut.hidden = false;
    doctorOut.replaceChildren(el("p", { class: "muted" }, "Checking…"));
    try {
      const d = await api("/api/fixit/doctor");
      doctorOut.replaceChildren(el("h4", {}, "Doctor"), ...d.lines.map((l) => el("div", { class: `fk-item ${l.ok === false ? "is-bad" : ""}` }, dot(l.ok), el("div", { class: "fk-item__txt" }, el("p", {}, l.text)))));
    } catch (e) { doctorOut.replaceChildren(ui.notice(`Doctor could not run. ${e.message || ""}`, { tone: "bad" })); }
  }

  function schedule() {
    clearTimeout(timer); timer = null;
    if (!live) return;
    timer = setTimeout(async () => {
      if (!node.isConnected) return;                              // the screen was closed: stop for good
      if (!document.hidden && node.offsetParent !== null) await loadLog();
      schedule();
    }, LOG_MS);
  }

  const node = el("div", { class: "fk ui-stack" }, voice, checkCard, providerCard, logCard);

  function refresh() {
    voice.refresh?.();
    loadChecklist();
    loadProviders(true);
    loadLog();
    schedule();
  }
  return { node, refresh, loadChecklist, loadProviders, stop() { clearTimeout(timer); timer = null; } };
}
