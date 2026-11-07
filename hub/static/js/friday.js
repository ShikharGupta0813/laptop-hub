// Friday: a warm daily companion on the phone. Brief, plan, get the laptop ready, spoken alerts.
// Backend contract (hub/friday): GET/POST /api/friday/settings, GET /brief, POST /plan, POST /plan/apply,
// POST /laptop-prep, GET /pending, POST /laptop-prep/run, POST /laptop-prep/dismiss, GET /alerts.
// A 404 means the module is not installed yet: every screen here shows a friendly state instead of an error.
// This file also exports the shared "brain" (state + actions + speech) that voicemode.js and home.js reuse.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { guardedSheet } from "./sourcepicker.js";

// ------------------------------------------------------------------------------------------------ css
const CSS = "/css/friday.css";
export function ensureCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

// ------------------------------------------------------------------------------------------------ shared state
export const FR = {
  settings: null,
  available: null,      // null unknown | true | false (404)
  brief: null,
  plan: null,
  planApplied: null,
  prep: null,           // {items:[{id,kind,target,why,status?}], results: Map(id -> {ok,message}), done}
  lastSpoken: "",
};
const listeners = new Set();
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function emit(what) { for (const fn of listeners) { try { fn(what); } catch (_) { /* a view is gone */ } } }

const DEFAULTS = { name: "Friday", address_as: "sir", spoken_alerts: false, proactive: false, brief_model: "local" };
export const isMissing = (e) => e instanceof ApiError && (e.status === 404 || e.status === 405);

export async function loadSettings(ctx = null, force = false) {
  if (FR.settings && !force) return FR.settings;
  try {
    const s = await api("/api/friday/settings");
    FR.settings = { ...DEFAULTS, ...(s || {}) };
    FR.available = true;
  } catch (error) {
    if (isMissing(error)) FR.available = false;
    FR.settings = FR.settings || { ...DEFAULTS };
    if (!isMissing(error)) throw error;
  }
  emit("settings");
  return FR.settings;
}
export async function saveSettings(next) {
  const s = await api("/api/friday/settings", { method: "POST", json: next });
  FR.settings = { ...DEFAULTS, ...FR.settings, ...next, ...(s && typeof s === "object" ? s : {}) };
  emit("settings");
  syncAlertWatcher();
  return FR.settings;
}
const S = () => FR.settings || DEFAULTS;
export const personaName = () => S().name || "Friday";
export const addressAs = () => S().address_as || "";

export function daypart(d = new Date()) {
  const h = d.getHours();
  return h < 5 ? "night" : h < 12 ? "morning" : h < 17 ? "afternoon" : "evening";
}
export function greetingText(d = new Date()) {
  const p = daypart(d);
  const word = p === "night" ? "Still up" : `Good ${p}`;
  return word + (addressAs() ? `, ${addressAs()}` : "") + ".";
}

// ------------------------------------------------------------------------------------------------ actions (shared)
const modelQuery = () => (S().brief_model === "claude" ? "claude" : "local");

export async function loadBrief() {
  const b = await api(`/api/friday/brief?model=${encodeURIComponent(modelQuery())}`);
  FR.brief = b || { sections: [], spoken: "", sources_failed: [] };
  FR.lastSpoken = FR.brief.spoken || "";
  emit("brief");
  return FR.brief;
}
export async function loadPlan() {
  const p = await api("/api/friday/plan", { method: "POST", json: { model: modelQuery() } });
  FR.plan = p || { blocks: [], spoken: "" };
  FR.planApplied = null;
  FR.lastSpoken = FR.plan.spoken || FR.lastSpoken;
  emit("plan");
  return FR.plan;
}
export async function applyPlan(blockIds) {
  const json = { confirm: true };
  if (blockIds && blockIds.length) json.block_ids = blockIds;
  const r = await api("/api/friday/plan/apply", { method: "POST", json });
  FR.planApplied = Number(r && r.created) || 0;
  emit("plan");
  return FR.planApplied;
}
export async function loadPrep() {
  const r = await api("/api/friday/laptop-prep", { method: "POST", json: { model: modelQuery() } });
  const items = ((r && r.items) || []).filter((i) => i && i.id != null);
  FR.prep = { items, results: new Map(), done: false };
  emit("prep");
  return FR.prep;
}
export function setPrepItems(items) {
  FR.prep = { items: items.filter((i) => i && i.id != null), results: new Map(), done: false };
  emit("prep");
}
export async function runPrep(ids) {
  const r = await api("/api/friday/laptop-prep/run", { method: "POST", json: { confirm: true, ids } });
  if (FR.prep) {
    for (const x of (r && r.results) || []) FR.prep.results.set(x.id, x);
    FR.prep.done = true;
  }
  emit("prep");
  return (r && r.results) || [];
}
export async function dismissPrep(ids) {
  await api("/api/friday/laptop-prep/dismiss", { method: "POST", json: { ids } });
  FR.prep = null;
  emit("prep");
}
export async function loadPending() {
  const r = await api("/api/friday/pending");
  return ((r && r.items) || []);
}
export async function loadAlerts(limit = 8) {
  const r = await api(`/api/friday/alerts?limit=${limit}`);
  return ((r && r.alerts) || []);
}

// What the owner will see and hear for a proposed item, in full.
export const KIND = {
  url: { icon: "globe", label: "Website", verb: "Open the website" },
  file: { icon: "file", label: "File", verb: "Open the file" },
  app: { icon: "laptop", label: "App", verb: "Start the app" },
};
export const kindOf = (k) => KIND[k] || { icon: "external", label: "Item", verb: "Open" };
export function prepLines(items) {
  return items.map((i) => `${kindOf(i.kind).label}: ${i.target}`);
}

// ------------------------------------------------------------------------------------------------ speech
export function cleanSpeech(input) {
  let s = String(input || "");
  s = s.replace(/```[\s\S]*?(```|$)/g, " ");
  s = s.replace(/`([^`]*)`/g, "$1");
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  s = s.replace(/<[^>]+>/g, " ");
  s = s.replace(/https?:\/\/\S+/g, "a link");
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "").replace(/^\s*([-*+•]|\d+[.)])\s+/gm, "");
  s = s.replace(/(\*\*|__|\*|~~)/g, "");
  s = s.replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}←-⇿]/gu, "");
  s = s.replace(/[|<>{}[\]\\^~_#=]+/g, " ");
  s = s.replace(/\s*\n+\s*/g, ". ").replace(/\.\s*\./g, ".").replace(/([:;,])\./g, "$1");
  return s.replace(/\s+/g, " ").trim();
}
function chunk(text, max = 200) {
  const parts = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
  const out = [];
  let cur = "";
  for (const p of parts) {
    if ((cur + p).length > max && cur) { out.push(cur.trim()); cur = p; } else cur += p;
  }
  if (cur.trim()) out.push(cur.trim());
  return out.flatMap((s) => (s.length > max * 1.5 ? s.match(new RegExp(`.{1,${max}}(\\s|$)`, "g")) || [s] : [s])).map((s) => s.trim()).filter(Boolean);
}

let voiceCache = null;
export function pickVoice() {
  const synth = window.speechSynthesis;
  if (!synth) return null;
  const all = synth.getVoices ? synth.getVoices() : [];
  if (!all.length) return null;
  if (voiceCache && all.includes(voiceCache)) return voiceCache;
  const lang = (navigator.language || "en-US").toLowerCase();
  const en = all.filter((v) => /^en/i.test(v.lang));
  const pool = en.length ? en : all;
  const score = (v) => {
    let n = 0;
    if (v.lang.toLowerCase() === lang) n += 3;
    if (/female|samantha|aria|jenny|zira|karen|moira|serena|libby|sonia|natural|neural|google uk english female|google us english/i.test(v.name)) n += 4;
    if (/male(?!.*female)|david|mark|fred|alex\b/i.test(v.name)) n -= 2;
    if (v.localService) n += 1;
    return n;
  };
  voiceCache = pool.slice().sort((a, b) => score(b) - score(a))[0] || null;
  return voiceCache;
}
try { window.speechSynthesis?.addEventListener?.("voiceschanged", () => { voiceCache = null; }); } catch (_) { /* optional */ }

// A small cancellable reader: sentence by sentence, with pause / resume / replay and a progress number.
export function createSpeaker(onChange) {
  let parts = [], idx = 0, total = 0, tok = 0, state = "idle", guard = null, text = "", unavailable = false;
  const done = () => parts.slice(0, idx).reduce((n, p) => n + p.length, 0);
  const emitState = () => onChange?.({ state, progress: total ? Math.min(1, done() / total) : 0, unavailable });
  function next() {
    const mine = tok;
    if (state !== "playing") return;
    if (idx >= parts.length) { state = "idle"; idx = 0; emitState(); onChange?.({ state, progress: 1, ended: true, unavailable }); return; }
    const synth = window.speechSynthesis;
    const u = new SpeechSynthesisUtterance(parts[idx]);
    const v = pickVoice();
    if (v) { u.voice = v; u.lang = v.lang; }
    u.rate = 1.0; u.pitch = 1.0; u.volume = 1;
    let started = false;
    clearTimeout(guard);
    guard = setTimeout(() => { // no voices on this device: stay quiet, keep the text on screen
      if (mine !== tok || started) return;
      unavailable = true; try { synth.cancel(); } catch (_) { /* ignore */ }
      state = "idle"; emitState();
    }, 4000);
    u.onstart = () => { started = true; clearTimeout(guard); };
    u.onend = () => { if (mine !== tok) return; clearTimeout(guard); idx += 1; emitState(); next(); };
    u.onerror = () => { if (mine !== tok) return; clearTimeout(guard); state = "idle"; emitState(); };
    try { synth.speak(u); } catch (_) { state = "idle"; emitState(); }
  }
  const api_ = {
    get state() { return state; },
    get unavailable() { return unavailable; },
    play(t) {
      text = cleanSpeech(t);
      api_.stop(true);
      if (!text || !window.speechSynthesis || typeof SpeechSynthesisUtterance === "undefined") { unavailable = !text ? unavailable : true; emitState(); return; }
      parts = chunk(text); total = parts.reduce((n, p) => n + p.length, 0); idx = 0; unavailable = false; tok += 1; state = "playing";
      emitState(); next();
    },
    pause() { if (state !== "playing") return; tok += 1; clearTimeout(guard); try { window.speechSynthesis.cancel(); } catch (_) { /* ignore */ } state = "paused"; emitState(); },
    resume() { if (state !== "paused") return; tok += 1; state = "playing"; emitState(); next(); },
    replay() { if (text) api_.play(text); },
    stop(quiet) {
      tok += 1; clearTimeout(guard);
      try { window.speechSynthesis?.cancel(); } catch (_) { /* ignore */ }
      if (state !== "idle" || !quiet) { state = "idle"; idx = 0; if (!quiet) emitState(); }
    },
    get text() { return text; },
  };
  return api_;
}

// ------------------------------------------------------------------------------------------------ voice intents
const norm = (t) => String(t).toLowerCase().replace(/[^a-z0-9\s']/g, " ").replace(/\s+/g, " ").trim();
// ctx: {prepShown, confirming}. Returns "" when the phrase is not for Friday, so it goes to the assistant.
export function matchIntent(text, { prepShown = false, confirming = false } = {}) {
  const n = norm(text).replace(/^(hey |hi |ok |okay )?friday,? ?/, "").replace(/^(please |could you |can you )/, "").replace(/ please$/, "");
  if (/^(what'?s|what is|what are|give me) (the |my )?(update|up|news|brief|briefing|on)( for)?( today| this morning| the day)?$/.test(n)
    || /^(what'?s|what is) up( for)? (today|this morning|now)$/.test(n)
    || /^(brief me|give me (the |a |my )?(brief|briefing|update|rundown)|morning brief|daily brief|update me|catch me up|what did i miss|what'?s happening( today)?|how'?s my day|how does my day look)$/.test(n)) return "brief";
  if (/^(plan|organi[sz]e|schedule|map out) (out )?(my|the) (day|today)( for me)?$|^plan (everything|it all|my day|today)$|^(plan it|make a plan)$/.test(n)) return "plan";
  if (/^(get|make|set|have|prepare|prep) (my |the )?laptop (ready|set up|prepared|prepped)( for me)?$|^(get|make) (everything )?ready( on (my|the) laptop)?$|^(open|set up) (my )?(things|stuff|tabs|everything)( on (my|the) laptop)?$|^laptop prep$/.test(n)) return "prep";
  if (/^(repeat|say (that|it) again|again|come again|repeat that|read (that|it) again|what was that)$/.test(n)) return "repeat";
  if (confirming && /^(yes|yeah|yep|yes please|confirm|confirmed|go ahead|do it|approve|approved|open them|yes open them|yes open it|go|sure|ok do it)$/.test(n)) return "yes";
  if (prepShown && /^(approve|approve them|approve it|approve all|yes open them|open them|open them on (my|the) laptop|open it on (my|the) laptop|yes approve|go ahead and open them|send them to (my|the) laptop|approve and open)$/.test(n)) return "approve";
  if ((confirming || prepShown) && /^(no|nope|nah|no thanks|not now|don'?t|do not|cancel|never ?mind|dismiss|leave it|not yet|hold on|wait)$/.test(n)) return "no";
  return "";
}

// ------------------------------------------------------------------------------------------------ spoken alerts (app-wide)
let alertCtx = null, alertStream = null, lastAlertSpoken = 0, alertSeen = new Set(), alertSpeaker = null, alertStarted = false;
const ALERT_GAP_MS = 20000;
function truncate(s, n) { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1).replace(/\s+\S*$/, "") + "…" : s; }
export function alertSentence(n) {
  const body = cleanSpeech(`${n.title || ""}${n.body ? ". " + n.body : ""}`);
  const lead = addressAs() ? `${addressAs().replace(/^./, (c) => c.toUpperCase())}, ` : "";
  return truncate(lead + body.replace(/^./, (c) => (lead ? c.toLowerCase() : c)), 140);
}
function maybeSpeakAlert(n) {
  if (!n || n.id == null || alertSeen.has(n.id)) return;
  alertSeen.add(n.id);
  if (alertSeen.size > 200) alertSeen = new Set([...alertSeen].slice(-100));
  const s = S();
  if (!s.spoken_alerts || FR.available !== true) return;
  if (n.level !== "warn" && n.level !== "error") return;
  if (document.hidden || document.documentElement.classList.contains("vm-open")) return;
  const created = Date.parse(n.created || "");
  if (Number.isFinite(created) && Date.now() - created > 120000) return; // replayed history, not news
  if (Date.now() - lastAlertSpoken < ALERT_GAP_MS) return;
  lastAlertSpoken = Date.now();
  alertSpeaker = alertSpeaker || createSpeaker();
  alertSpeaker.play(alertSentence(n));
}
function closeStream() { try { alertStream?.close(); } catch (_) { /* ignore */ } alertStream = null; }
export function syncAlertWatcher() {
  const want = FR.available === true && S().spoken_alerts && typeof EventSource !== "undefined";
  if (!want) { closeStream(); return; }
  if (alertStream) return;
  try {
    alertStream = new EventSource("/api/notifications/stream");
    alertStream.onmessage = (e) => { try { maybeSpeakAlert(JSON.parse(e.data)); } catch (_) { /* not json */ } };
    alertStream.onerror = () => { /* EventSource retries by itself */ };
  } catch (_) { alertStream = null; }
}
// Called once by Home: learns whether Friday exists and, if spoken alerts are on, listens for warnings.
export function startAlerts(ctx) {
  if (alertStarted) return;
  alertStarted = true; alertCtx = ctx;
  document.addEventListener("visibilitychange", () => { if (document.hidden) alertSpeaker?.stop(); });
  loadSettings(ctx).then(syncAlertWatcher).catch(() => { alertStarted = false; });
}
export const _alertCtx = () => alertCtx;

// ------------------------------------------------------------------------------------------------ the screen
const PRIORITY = {
  3: { label: "Urgent", tone: "bad" },
  2: { label: "Important", tone: "warn" },
  1: { label: "Worth a look", tone: "info" },
  0: { label: "", tone: "gray" },
};
function sectionIcon(sec) {
  const k = `${sec.id || ""} ${sec.title || ""}`.toLowerCase();
  if (/mail|inbox/.test(k)) return "mail";
  if (/asana|task|todo|to-do/.test(k)) return "list-checks";
  if (/chat|message|slack|teams|mention|tag/.test(k)) return "talk";
  if (/calendar|meeting|event|schedule/.test(k)) return "calendar";
  if (/remind/.test(k)) return "alarm";
  return "sparkles";
}
function clockOf(v) {
  if (v == null) return "";
  const s = String(v);
  if (/^\d{1,2}:\d{2}/.test(s)) return s.slice(0, 5);
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
const NUM_WORD = ["no", "one", "two", "three", "four", "five", "six"];
export function toneLine(brief) {
  const items = (brief.sections || []).flatMap((s) => s.items || []);
  const urgent = items.filter((i) => Number(i.priority) >= 3).length;
  const matter = items.filter((i) => Number(i.priority) >= 2).length;
  const p = daypart();
  const when = p === "night" ? "tonight" : p === "morning" ? "this morning" : p === "afternoon" ? "this afternoon" : "this evening";
  if (!items.length) return "All clear. Nothing is waiting on you right now.";
  if (urgent) return urgent === 1 ? "One thing needs you first. The rest can wait." : "A few things need you first. The rest can wait.";
  if (matter === 0) return `A gentle ${p === "night" ? "night" : p}. Nothing pressing, just a few things to know.`;
  const word = NUM_WORD[matter] || String(matter);
  return `${p === "morning" ? "Quiet morning" : "Calm " + p}. ${word.replace(/^./, (c) => c.toUpperCase())} ${matter === 1 ? "thing matters" : "things matter"} ${when}.`;
}

export function init(container, ctx) {
  ensureCss();
  const root = el("div", { class: "fr" });
  container.append(root);
  let visible = false;
  let busy = "";                 // "brief" | "plan" | "prep" while a request runs
  let missing = false;
  const selected = new Set();    // ticked prep item ids
  let spk = { state: "idle", progress: 0, unavailable: false };
  const speaker = createSpeaker((s) => { spk = { state: s.state, progress: s.progress, unavailable: !!s.unavailable, ended: !!s.ended }; paintPlayer(); });
  let alerts = null, alertsError = null, pending = null, speakAsk = true;

  // ---------- hero
  const orb = el("div", { class: "fr-orb", "aria-hidden": "true" }, el("span", { class: "fr-orb__ring" }), el("span", { class: "fr-orb__core" }));
  const heroName = el("p", { class: "fr-name" });
  const heroHello = el("h1", { class: "fr-hello" });
  const heroSub = el("p", { class: "fr-sub", "aria-live": "polite" });
  const settingsBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "sliders", "aria-label": "Friday settings", onClick: () => openSettings() });
  const helpBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "help", "aria-label": "How Friday works", onClick: () => openHelp() });
  const hero = el("header", { class: "fr-hero" },
    el("div", { class: "fr-hero__tools" }, helpBtn, settingsBtn), orb, heroName, heroHello, heroSub);

  const briefBtn = ui.button({ variant: "primary", size: "lg", icon: "sparkles", label: "Brief me", class: "fr-brief-btn", onClick: () => runBrief() });
  const micBtn = ui.button({ variant: "secondary", size: "lg", iconOnly: true, icon: "mic", class: "fr-mic-btn", "aria-label": "Talk to Friday", onClick: () => ctx.open("assistant", "friday") });
  const actions = el("div", { class: "fr-actions" }, briefBtn, micBtn);

  // ---------- player
  const playBtn = ui.button({ variant: "tonal", iconOnly: true, icon: "pause", "aria-label": "Pause", onClick: () => (speaker.state === "paused" ? speaker.resume() : speaker.pause()) });
  const stopBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "stop", "aria-label": "Stop speaking", onClick: () => speaker.stop() });
  const replayBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "refresh", "aria-label": "Read it again", onClick: () => speaker.replay() });
  const bar = ui.progressBar(0);
  const playerNote = el("span", { class: "fr-player__note" });
  const player = el("div", { class: "fr-player ui-card ui-card--glass", hidden: true, role: "group", "aria-label": "Spoken brief" },
    el("div", { class: "fr-player__row" }, playBtn, el("div", { class: "fr-player__mid" }, playerNote, bar), stopBtn, replayBtn));
  function paintPlayer() {
    const live = spk.state === "playing" || spk.state === "paused";
    player.hidden = !(live || (spk.state === "idle" && speaker.text && (spk.ended || spk.progress > 0 || spk.unavailable)));
    root.classList.toggle("is-speaking", spk.state === "playing");
    playBtn.hidden = !live; stopBtn.hidden = !live;
    playBtn.replaceChildren(ui.icon(spk.state === "paused" ? "play" : "pause"));
    playBtn.setAttribute("aria-label", spk.state === "paused" ? "Keep going" : "Pause");
    bar.set(live ? spk.progress : spk.ended ? 1 : spk.progress);
    playerNote.textContent = spk.unavailable ? "This phone has no voice to read with. It is all written below."
      : spk.state === "playing" ? "Reading to you…" : spk.state === "paused" ? "Paused" : "Finished. Tap replay to hear it again.";
  }
  paintPlayer();

  // ---------- content slots
  const stage = el("div", { class: "fr-stage ui-stack ui-stack--lg" });
  const pendingSlot = el("section", { class: "fr-slot", "aria-label": "Waiting for you", hidden: true });
  const alertsSlot = el("section", { class: "fr-slot", "aria-label": "Alerts" });
  root.append(hero, actions, player, pendingSlot, stage, alertsSlot);

  function paintHero() {
    heroName.textContent = personaName();
    heroHello.textContent = greetingText();
    if (missing) heroSub.textContent = "";
    else if (busy === "brief") heroSub.textContent = "Looking through your mail, chats and tasks…";
    else if (busy === "plan") heroSub.textContent = "Finding a calm shape for your day…";
    else if (busy === "prep") heroSub.textContent = "Picking what you will want open…";
    else if (FR.brief) heroSub.textContent = toneLine(FR.brief);
    else heroSub.textContent = "Ready when you are. I will keep it short.";
    briefBtn.disabled = !!busy || missing;
    root.classList.toggle("is-busy", !!busy);
  }

  // ---------- states
  function notInstalled() {
    missing = true;
    stage.replaceChildren(ui.emptyState({
      icon: "sparkles", title: `${personaName()} is almost here`,
      body: "Friday is not set up on your laptop yet. Update the hub on the laptop and she will be ready. Everything else keeps working.",
      action: { label: "Try again", icon: "refresh", onClick: () => { missing = false; boot(true); } },
    }));
    actions.hidden = true; pendingSlot.hidden = true; alertsSlot.hidden = true;
    paintHero();
  }
  function problem(e, retry) {
    if (isMissing(e)) { notInstalled(); return; }
    stage.querySelectorAll(".fr-err").forEach((n) => n.remove());
    stage.prepend(el("div", { class: "fr-err" }, ui.notice(`That did not work this time. ${e.message || ""}`.trim(), { tone: "bad" }),
      ui.button({ variant: "tonal", size: "sm", icon: "refresh", label: "Try again", onClick: retry })));
  }
  const clearErr = () => stage.querySelectorAll(".fr-err").forEach((n) => n.remove());

  // ---------- brief
  const briefSlot = el("section", { class: "fr-block", "aria-label": "Your brief", hidden: true });
  const nextSlot = el("section", { class: "fr-block", "aria-label": "What next", hidden: true });
  const planSlot = el("section", { class: "fr-block", "aria-label": "Your day", hidden: true });
  const prepSlot = el("section", { class: "fr-block", "aria-label": "Laptop ready", hidden: true });
  stage.append(briefSlot, nextSlot, planSlot, prepSlot);

  async function runBrief({ speak = true } = {}) {
    if (busy) return;
    busy = "brief"; clearErr(); paintHero();
    briefSlot.hidden = false;
    briefSlot.replaceChildren(ui.skeleton("card", 2));
    speaker.stop(true);
    try {
      await loadBrief();
      busy = ""; paintHero(); renderBrief();
      if (speak) {
        const ask = " Shall I plan your day, or get your laptop ready?";
        speaker.play((FR.brief.spoken || toneLine(FR.brief)) + ask);
      }
      ui.haptic("success");
    } catch (e) {
      busy = ""; paintHero(); briefSlot.hidden = true;
      problem(e, () => runBrief());
    }
  }
  function priorityChip(p) {
    const meta = PRIORITY[Math.max(0, Math.min(3, Number(p) || 0))];
    return meta.label ? el("span", { class: `ui-badge ui-badge--${meta.tone} fr-chip` }, meta.label) : null;
  }
  function renderBrief() {
    const b = FR.brief;
    if (!b) { briefSlot.hidden = true; nextSlot.hidden = true; return; }
    const secs = (b.sections || []).map((s) => ({ ...s, items: [...(s.items || [])].sort((x, y) => (y.priority || 0) - (x.priority || 0)) }))
      .filter((s) => s.items.length)
      .sort((a, c) => (c.items[0].priority || 0) - (a.items[0].priority || 0));
    const nodes = [];
    if (b.generated_at) nodes.push(el("p", { class: "fr-tone__meta" }, `Updated ${ui.relTime(new Date(b.generated_at))}`));
    if ((b.sources_failed || []).length) {
      nodes.push(ui.notice(`I could not reach ${b.sources_failed.join(", ")} this time. The rest is below, and I will try again next time.`, { tone: "warn" }));
    }
    if (!secs.length) {
      nodes.push(ui.emptyState({ icon: "check-circle", title: "Nothing is waiting on you", body: "A clear page. Take a breath, or ask me to plan something nice for the day.", compact: true }));
    }
    for (const sec of secs) {
      nodes.push(el("section", { class: "fr-sec ui-card ui-card--flat", "aria-label": sec.title },
        el("div", { class: "ui-card__head" }, ui.tile(sectionIcon(sec), (sec.items[0].priority || 0) >= 3 ? "bad" : (sec.items[0].priority || 0) >= 2 ? "warn" : "accent", "sm"),
          el("h2", { class: "ui-card__title grow" }, sec.title)),
        el("ul", { class: "fr-items" }, sec.items.map((it) => {
          const link = it.link && it.link.view;
          const body = [
            el("span", { class: "fr-item__text" }, it.text),
            el("span", { class: "fr-item__meta" }, priorityChip(it.priority), it.source ? el("span", { class: "fr-source" }, it.source) : null),
          ];
          return el("li", { class: `fr-item fr-p${Math.max(0, Math.min(3, Number(it.priority) || 0))}` }, link
            ? el("button", { type: "button", class: "fr-item__btn", onclick: () => { speaker.stop(); ctx.open(it.link.view, it.link.section || undefined, { id: it.link.id }); } }, ...body, ui.icon("chevron-right"))
            : el("div", { class: "fr-item__btn fr-item__btn--static" }, ...body));
        }))));
    }
    briefSlot.hidden = false;
    briefSlot.replaceChildren(...nodes);
    nextSlot.hidden = false;
    nextSlot.replaceChildren(el("div", { class: "fr-next ui-card ui-card--glass" },
      el("p", { class: "fr-next__ask" }, "What would you like next?"),
      el("div", { class: "fr-next__btns" },
        ui.button({ variant: "tonal", icon: "calendar", label: "Plan my day", onClick: () => runPlan() }),
        ui.button({ variant: "tonal", icon: "laptop", label: "Get laptop ready", onClick: () => startPrep() }))));
  }

  // ---------- plan
  async function runPlan() {
    if (busy) return;
    busy = "plan"; clearErr(); paintHero();
    planSlot.hidden = false; planSlot.replaceChildren(ui.skeleton("list", 4));
    planSlot.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    speaker.stop(true);
    try {
      await loadPlan();
      busy = ""; paintHero(); renderPlan();
      if (FR.plan.spoken) speaker.play(FR.plan.spoken);
    } catch (e) { busy = ""; paintHero(); planSlot.hidden = true; problem(e, () => runPlan()); }
  }
  const BLOCK_TONE = { focus: "accent", meeting: "info", task: "good", break: "teal", learning: "violet", admin: "orange" };
  function renderPlan() {
    const p = FR.plan;
    if (!p) { planSlot.hidden = true; return; }
    const blocks = p.blocks || [];
    const head = el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, "Your day, gently laid out"));
    if (!blocks.length) {
      planSlot.replaceChildren(head, ui.emptyState({ icon: "calendar", title: "An open day", body: "Nothing to schedule. Your time is your own.", compact: true }));
      return;
    }
    const line = (b) => el("li", { class: "fr-block-row", "data-kind": b.kind || "task" },
      el("div", { class: "fr-time ui-num" }, el("span", {}, clockOf(b.start)), el("span", { class: "fr-time__to" }, clockOf(b.end))),
      el("div", { class: "fr-rail", "aria-hidden": "true" }, el("span", { class: "fr-dot" })),
      el("div", { class: "fr-block-card" },
        el("p", { class: "fr-block-title" }, b.title),
        el("p", { class: "fr-block-meta" }, ui.badge(String(b.kind || "task").replace(/^./, (c) => c.toUpperCase()), BLOCK_TONE[b.kind] === "good" ? "good" : BLOCK_TONE[b.kind] === "info" ? "accent" : "accent"), b.source ? el("span", {}, b.source) : null)));
    const applied = FR.planApplied;
    planSlot.replaceChildren(...[head, el("ol", { class: "fr-timeline" }, blocks.map(line)),
      applied != null
        ? ui.notice(applied ? `Done. ${applied} ${applied === 1 ? "item is" : "items are"} in your to-dos.` : "Those were already in your to-dos.", { tone: "good" })
        : el("div", { class: "fr-row-btns" },
          ui.button({ variant: "primary", icon: "list-checks", label: "Add to my to-dos", onClick: (e) => applyThePlan(e.currentTarget) })),
      applied != null ? ui.button({ variant: "tonal", size: "sm", icon: "list-checks", label: "Open to-dos", onClick: () => ctx.open("todos") }) : null].filter(Boolean));
  }
  let confirming = false;   // a double tap must not stack two confirm dialogs (and so run the action twice)
  async function applyThePlan(btn) {
    if (confirming || btn.getAttribute("aria-busy") === "true") return;
    const blocks = (FR.plan && FR.plan.blocks) || [];
    confirming = true;
    const yes = await ui.confirm({ title: "Add these to your to-dos?", message: `${blocks.length} ${blocks.length === 1 ? "block" : "blocks"} from today's plan will be added as to-dos. Nothing is sent to anyone.`, confirmLabel: "Add them" }).finally(() => { confirming = false; });
    if (!yes) return;
    try { await ui.busy(btn, applyPlan()); ui.toast("Added to your to-dos.", { kind: "good" }); ui.haptic("success"); }
    catch (e) { ui.toast(isMissing(e) ? "Friday is not set up on the laptop yet." : `Could not add them. ${e.message}`, { kind: "bad" }); }
  }

  // ---------- laptop prep
  async function startPrep() {
    if (busy) return;
    busy = "prep"; clearErr(); paintHero();
    prepSlot.hidden = false; prepSlot.replaceChildren(ui.skeleton("list", 3));
    prepSlot.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    speaker.stop(true);
    try {
      await loadPrep();
      busy = ""; paintHero();
      selected.clear(); FR.prep.items.forEach((i) => selected.add(i.id));
      renderPrep();
      speaker.play(FR.prep.items.length ? `I have ${FR.prep.items.length} ${FR.prep.items.length === 1 ? "thing" : "things"} ready to open on your laptop. Have a look, and tell me when you are happy.` : "I could not find anything worth opening right now.");
    } catch (e) { busy = ""; paintHero(); prepSlot.hidden = true; problem(e, () => startPrep()); }
  }
  function renderPrep() {
    const prep = FR.prep;
    if (!prep) { prepSlot.hidden = true; return; }
    prepSlot.hidden = false;
    if (!prep.inited) { prep.inited = true; selected.clear(); prep.items.forEach((i) => selected.add(i.id)); }
    const head = el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, "For your laptop"));
    if (!prep.items.length) {
      prepSlot.replaceChildren(head, ui.emptyState({ icon: "laptop", title: "Nothing to open", body: "Your laptop can stay just as it is.", compact: true }));
      return;
    }
    const rows = prep.items.map((it) => {
      const k = kindOf(it.kind);
      const res = prep.results.get(it.id);
      const check = el("input", { type: "checkbox", class: "fr-check", "aria-label": `${k.label}: ${it.target}` });
      check.checked = selected.has(it.id);
      check.disabled = prep.done;
      check.addEventListener("change", () => { check.checked ? selected.add(it.id) : selected.delete(it.id); updateRunBtn(); ui.haptic("select"); });
      return el("li", { class: "fr-prep ui-card ui-card--flat" + (res ? (res.ok ? " is-ok" : " is-bad") : "") },
        el("label", { class: "fr-prep__label" },
          check, ui.tile(k.icon, "accent", "sm"),
          el("div", { class: "fr-prep__body" },
            el("div", { class: "fr-prep__kind" }, k.label, it.status && it.status !== "pending" ? ui.badge(it.status, "accent") : null),
            el("code", { class: "fr-target" }, it.target),
            it.why ? el("p", { class: "fr-prep__why" }, it.why) : null)),
        res ? el("p", { class: "fr-prep__result" }, ui.icon(res.ok ? "check-circle" : "alert-circle"), res.ok ? (res.message || "Opened on your laptop.") : (res.message || "That one did not open.")) : null);
    });
    const runBtn = ui.button({ variant: "primary", size: "lg", icon: "laptop", label: "Approve & open on laptop", block: true, onClick: () => approveAndOpen(runBtn) });
    function updateRunBtn() {
      const n = selected.size;
      runBtn.disabled = n === 0;
      runBtn.replaceChildren(ui.icon("laptop"), n === prep.items.length ? "Approve & open on laptop" : n ? `Approve & open ${n} on laptop` : "Choose at least one");
    }
    prepSlot.replaceChildren(...[head,
      prep.done ? null : el("p", { class: "fr-prep__intro" }, "Nothing opens until you say so. Untick anything you do not want."),
      el("ul", { class: "fr-preplist" }, rows),
      prep.done
        ? el("div", { class: "fr-row-btns" }, ui.button({ variant: "tonal", icon: "check", label: "All done", onClick: () => { FR.prep = null; renderPrep(); loadPendingStrip(); } }))
        : el("div", { class: "fr-prep__btns" }, runBtn,
          ui.button({ variant: "ghost", label: "Dismiss", onClick: () => dismissThem() }))].filter(Boolean));
    if (!prep.done) updateRunBtn();
  }
  async function approveAndOpen(btn) {
    const prep = FR.prep; if (!prep || confirming || btn.getAttribute("aria-busy") === "true") return;
    const chosen = prep.items.filter((i) => selected.has(i.id));
    if (!chosen.length) return;
    confirming = true;
    const ok = await ui.confirm({
      title: `Open ${chosen.length} ${chosen.length === 1 ? "thing" : "things"} on your laptop?`,
      message: `This will open exactly:\n${prepLines(chosen).map((l) => "• " + l).join("\n")}`,
      confirmLabel: "Yes, open them", icon: "laptop",
    }).finally(() => { confirming = false; });
    if (!ok) return;
    speaker.stop();
    try {
      const results = await ui.busy(btn, runPrep(chosen.map((i) => i.id)));
      const bad = results.filter((r) => !r.ok).length;
      ui.toast(bad ? "Most of it opened. A few things need a look." : "All open on your laptop.", { kind: bad ? "warn" : "good" });
      ui.haptic(bad ? "warn" : "success");
    } catch (e) { ui.toast(`Could not reach the laptop. ${e.message}`, { kind: "bad" }); }
  }
    async function dismissThem() {
    const prep = FR.prep; if (!prep) return;
    try { await dismissPrep(prep.items.map((i) => i.id)); ui.toast("Okay, I put those away.", { kind: "info" }); loadPendingStrip(); }
    catch (e) { ui.toast(`Could not dismiss them. ${e.message}`, { kind: "bad" }); }
  }

  // ---------- pending + alerts
  async function loadPendingStrip() {
    try { pending = await loadPending(); } catch (e) { if (isMissing(e)) return; pending = []; }
    const waiting = (pending || []).filter((i) => !i.status || i.status === "pending");
    pendingSlot.hidden = !waiting.length;
    if (!waiting.length) return;
    pendingSlot.replaceChildren(el("div", { class: "fr-wait ui-card ui-card--glass" },
      el("div", { class: "fr-wait__head" }, ui.tile("clock", "warn", "sm"),
        el("div", { class: "grow" }, el("p", { class: "fr-wait__title" }, "Waiting for you"),
          el("p", { class: "fr-wait__sub" }, `${waiting.length} ${waiting.length === 1 ? "thing is" : "things are"} ready to open on your laptop, whenever you like.`))),
      el("ul", { class: "fr-wait__list" }, waiting.slice(0, 3).map((i) => el("li", {}, ui.icon(kindOf(i.kind).icon), el("span", { class: "fr-wait__t" }, i.target)))),
      ui.button({ variant: "tonal", size: "sm", label: "Review them", onClick: () => { setPrepItems(waiting); selected.clear(); waiting.forEach((i) => selected.add(i.id)); renderPrep(); prepSlot.scrollIntoView?.({ behavior: "smooth", block: "start" }); } })));
  }
  async function loadAlertsList() {
    try { alerts = await loadAlerts(8); alertsError = null; } catch (e) { if (isMissing(e)) return; alerts = null; alertsError = e; }
    renderAlerts();
  }
  function renderAlerts() {
    const head = el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, "Alerts"));
    if (alertsError) { alertsSlot.replaceChildren(head, ui.notice("I could not load your alerts just now.", { tone: "warn" })); return; }
    if (alerts == null) { alertsSlot.replaceChildren(head, ui.skeleton("list", 2)); return; }
    if (!alerts.length) { alertsSlot.replaceChildren(head, el("p", { class: "fr-quiet" }, "All quiet. I will tell you if someone tags you or something becomes urgent.")); return; }
    alertsSlot.replaceChildren(head, el("div", { class: "ui-list ui-list--inset" }, alerts.map((a) => {
      const tone = a.level === "error" ? "bad" : a.level === "warn" ? "warn" : "accent";
      return ui.listRow({
        icon: a.level === "info" ? "bell" : "alert", tone, title: a.text, sub: a.ts ? ui.relTime(new Date(a.ts)) : "",
        chevron: !!(a.link && a.link.view),
        onClick: a.link && a.link.view ? () => ctx.open(a.link.view, a.link.section || undefined, { id: a.link.id }) : undefined,
      });
    })));
  }

  // ---------- settings + help
  function openSettings() {
    const cur = { ...DEFAULTS, ...FR.settings };
    const name = el("input", { type: "text", value: cur.name, maxlength: 20, autocomplete: "off", id: "fr-set-name" });
    const addr = el("input", { type: "text", value: cur.address_as, maxlength: 20, autocomplete: "off", id: "fr-set-addr", placeholder: "sir, boss, or your name" });
    const spoken = ui.switchControl({ checked: !!cur.spoken_alerts, label: "Speak alerts" });
    const proactive = ui.switchControl({ checked: !!cur.proactive, label: "Keep watch for me" });
    const model = ui.segmented({ options: [{ value: "local", label: "Local" }, { value: "claude", label: "Claude" }], value: cur.brief_model === "claude" ? "claude" : "local", label: "Brain for briefs", block: true });
    const row = (title, sub, ctl) => el("div", { class: "fr-set-row" }, el("div", { class: "grow" }, el("p", { class: "fr-set-title" }, title), el("p", { class: "fr-set-sub" }, sub)), ctl);
    const content = el("div", { class: "fr-set ui-stack" },
      el("div", { class: "ui-field" }, el("label", { class: "ui-label", for: "fr-set-name" }, "Her name"), name),
      el("div", { class: "ui-field" }, el("label", { class: "ui-label", for: "fr-set-addr" }, "How she addresses you"), addr,
        el("p", { class: "ui-hint" }, "Used in greetings and spoken alerts.")),
      row("Speak alerts out loud", "When something important arrives while the app is open, I will say it once, briefly. Quiet if the phone is muted or the app is in the background.", spoken),
      row("Keep watch for me", "While the hub is running, I look through your mail, chats and tasks now and then, and tell you when someone tags you or something turns urgent. I only read and tell you. I never reply, send or change anything on my own.", proactive),
      el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Who thinks for me"), model,
        el("p", { class: "ui-hint" }, "Local stays on your laptop. Claude writes a little more warmly but sends the text of your brief to Anthropic.")));
    const sheet = guardedSheet(ui, {
      title: "Friday settings", content,
      actions: [{ label: "Save", variant: "primary", icon: "check", async onClick(s) {
        const next = { name: name.value.trim() || "Friday", address_as: addr.value.trim(), spoken_alerts: spoken.input.checked, proactive: proactive.input.checked, brief_model: model.value };
        try { await saveSettings(next); ui.toast("Saved.", { kind: "good" }); paintHero(); }
        catch (e) { ui.toast(isMissing(e) ? "Friday is not set up on the laptop yet." : `Could not save. ${e.message}`, { kind: "bad" }); return false; }
        return undefined;
      } }],
    });
    return sheet;
  }
  function openHelp() {
    guardedSheet(ui, {
      title: "How Friday works",
      content: el("div", { class: "fr-help ui-stack" },
        el("p", {}, el("strong", {}, "Tap the mic to talk to Friday.")),
        el("p", {}, "Say things like “what’s the update”, “plan my day” or “get my laptop ready”. Anything else goes to your assistant."),
        el("p", {}, "She reads your mail, chats and tasks through the laptop, then tells you what matters. She goes by what is urgent and who needs you, not only by words like “urgent”."),
        el("p", {}, "She uses this phone’s microphone and speaker. Nothing opens on your laptop until you approve the exact list, with a tap or a clear “yes”."),
        el("p", { class: "muted" }, "Tip: she reads aloud best with the phone volume up and the app open.")),
    });
  }

  // ---------- lifecycle
  async function boot(force) {
    paintHero();
    renderAlerts();
    try { await loadSettings(ctx, force); } catch (e) { stage.prepend(ui.notice(`I could not reach your laptop. ${e.message}`, { tone: "bad" })); }
    if (FR.available === false) { notInstalled(); return; }
    missing = false; actions.hidden = false; alertsSlot.hidden = false;
    paintHero();
    syncAlertWatcher();
    if (FR.brief) renderBrief();
    if (FR.plan) renderPlan();
    if (FR.prep) renderPrep();
    loadPendingStrip(); loadAlertsList();
  }
  const unsub = subscribe((what) => {
    if (what === "settings") paintHero();
    else if (what === "prep" && !busy) renderPrep();
    else if (what === "plan" && !busy) renderPlan();
  });
  const onVis = () => { if (document.hidden) speaker.stop(); };
  document.addEventListener("visibilitychange", onVis);
  ui.pullToRefresh(container, async () => { await boot(true); });
  const ready = boot(false);

  return {
    show() { visible = true; paintHero(); if (!missing) { loadPendingStrip(); loadAlertsList(); } },
    hide() { visible = false; speaker.stop(); },
    async openSection(name) {
      await ready;
      if (missing) return;
      if (name === "brief") runBrief();
      else if (name === "plan") runPlan();
      else if (name === "prep" || name === "laptop") startPrep();
    },
    refresh() { return boot(true); },
    destroy() { unsub(); document.removeEventListener("visibilitychange", onVis); speaker.stop(); },
  };
}
