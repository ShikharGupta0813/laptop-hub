// Hands-free voice mode: a full-screen overlay that loops  listen -> transcribe -> assistant -> speak -> listen.
// Opened by assistant.js, which passes a `host` (see openVoiceMode). Nothing is ever approved automatically:
// a tool approval shows big Approve / Deny buttons here and the loop simply waits for the tap.
import { el, ApiError } from "./api.js";
import { providerLabel } from "./providers.js";
import { speakable, splitStream, splitName } from "./speechchunk.js";

const SILENCE_MS_DEFAULT = 1200;   // quiet this long after speech ends the recording
const MAX_UTTERANCE_MS = 30000;    // a single spoken message is cut at this length
const MIN_SPEECH_MS = 200;         // loud for this long in total before it counts as speech (ignores clicks)
const IDLE_ROLL_MS = 20000;        // with no speech, restart the recorder so it never grows without bound
const SPEAK_CAP = 700;             // characters read aloud at most (per turn, across streamed sentences)
const WARM_EVERY_MS = 8 * 60 * 1000; // keep the models loaded while voice mode is open
const TICK_MS = 50;

function loadCss() {
  if (document.querySelector('link[data-css="voicemode"]')) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/css/voicemode.css";
  link.dataset.css = "voicemode";
  document.head.append(link);
}

function store(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
}
function storeSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* not remembered, still works */ }
}

export { speakable };

// Wake word hook (js/wake.js): hand voice mode some words, and optionally a persona, before it opens.
// Non-empty text is sent as if it had just been spoken; with no text voice mode simply starts listening.
let preload = null;
export function setPreload(text, persona = "") {
  preload = { text: String(text || "").trim(), persona: persona === "friday" ? "friday" : persona === "jarvis" ? "jarvis" : "", at: Date.now() };
}
function takePreload() { const p = preload; preload = null; return p && p.text && Date.now() - p.at < 15000 ? p : null; }
function peekPreload() { return preload && Date.now() - preload.at < 15000 ? preload : null; }

function sentences(text, max = 220) {
  const parts = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
  const out = [];
  let cur = "";
  for (const p of parts) {
    if ((cur + p).length > max && cur) { out.push(cur.trim()); cur = p; } else cur += p;
  }
  if (cur.trim()) out.push(cur.trim());
  return out.flatMap((s) => (s.length > max * 1.5 ? s.match(new RegExp(`.{1,${max}}(\\s|$)`, "g")) || [s] : [s]));
}

function normalise(text) {
  return String(text).toLowerCase().replace(/[^a-z0-9\s']/g, " ").replace(/\s+/g, " ").trim();
}
function commandFor(text) {
  const n = normalise(text);
  if (/^(please )?(stop|cancel|never ?mind|be quiet|quiet|enough|stop talking)( please)?$/.test(n)) return "stop";
  if (/^(please )?(new chat|new conversation|start (a )?new chat|start over|reset( the)? chat|clear( the)? chat)$/.test(n)) return "new";
  if (/^(close|exit|quit|end)( voice( mode)?)?$|^(good ?bye|bye|that's all|that is all)$/.test(n)) return "close";
  return "";
}

let active = null;

// host: {ctx, ui, friday (bool: greet in the Friday persona), provider(), isBusy(), ask(text, hooks) -> Promise<{text, stopped, hadError, error}>,
//        stopTurn(), newChat(), approvalParts(input), onClose()}
export function openVoiceMode(host) {
  if (active) return active;
  loadCss();
  const { ctx, ui } = host;

  const STATES = {
    listening: ["Listening", "Just talk. I will answer when you pause."],
    paused: ["Paused", "Tap the orb to start listening again."],
    thinking: ["Thinking", "Tap the orb to stop."],
    speaking: ["Speaking", "Tap the orb to interrupt."],
    approval: ["Waiting for you", "I will not do this until you say so."],
    error: ["Voice mode is unavailable", ""],
  };
  const silenceMs = Math.max(400, Math.min(4000, Number(store("hub.voice.silenceMs", SILENCE_MS_DEFAULT)) || SILENCE_MS_DEFAULT));

  let alive = true;
  let state = "listening";
  let paused = false;
  let ttsOn = store("hub.voice.tts", "1") !== "0";
  let level = 0;       // smoothed 0..1 mic level
  let bump = 0;        // synthetic pulse while speaking
  let heard = false;   // listening: speech detected in this utterance
  let pendingApproval = null;
  let audioCtx = null, analyser = null, stream = null, source = null, wakeLock = null;
  let vad = null;      // set while listening: called every tick with the current level
  let wake = null;     // resolves a wait that tapping the orb or closing should end early
  let utter = null;    // current speech job {cancel}
  const tts = { q: [], buf: "", busy: false, cancelled: false, chars: 0, capped: false, waiters: [], any: false };
  let names = { jarvis: "Jarvis", call: "" };   // from /api/assistant/info
  let fcMod = null;    // fullcontrol.js, once loaded: isOn() says whether nothing needs approving
  let warmTimer = null;
  let asking = false, lastText = "", lastAt = 0;   // double-submit guard
  let release = null;  // ui.pushLayer release
  const buf = new Float32Array(1024);

  // ------------------------------------------------------------------ DOM
  const orb = el("button", { type: "button", class: "vm-orb", "aria-label": "Voice orb", onclick: () => onOrbTap() },
    el("span", { class: "vm-orb__ring vm-orb__ring--a", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__ring vm-orb__ring--b", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__glow", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__core", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__icon", "aria-hidden": "true" }));
  // persona: Jarvis = cyan core, Friday = amber core (css keys off data-persona on the overlay)
  const who = el("p", { class: "vm-who ui-caps", "aria-hidden": "true" }, names.jarvis);
  const wave = el("span", { class: "vm-wave ui-wave", "aria-hidden": "true" }, ...Array.from({ length: 9 }, () => el("i")));
  const title = el("h2", { class: "vm-title", id: "vm-title" });
  const hint = el("p", { class: "vm-hint" });
  const userCap = el("p", { class: "vm-cap vm-cap--user" });
  const aiCap = el("p", { class: "vm-cap vm-cap--ai", "aria-live": "polite" });
  const sub = el("p", { class: "vm-sub" });
  const captions = el("div", { class: "vm-captions" }, userCap, aiCap, sub);
  const soundBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "volume", class: "vm-sound", "aria-label": "Spoken replies", onClick: () => setTts(!ttsOn) });
  const closeBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "x", class: "vm-close", "aria-label": "Close voice mode", onClick: () => close() });
  const badge = el("span", { class: "vm-provider ui-badge ui-badge--accent" }, providerLabel(host.provider()));
  const top = el("div", { class: "vm-top" }, soundBtn, el("div", { class: "vm-top__mid" }, "Voice mode ", badge), closeBtn);

  const approvalBody = el("div", { class: "vm-approval__body" });
  const approveBtn = ui.button({ variant: "danger", size: "lg", icon: "check", label: "Approve", class: "vm-approve" });
  const denyBtn = ui.button({ variant: "secondary", size: "lg", icon: "x", label: "Deny", class: "vm-deny" });
  const approvalMsg = el("p", { class: "vm-approval__msg", role: "status" });
  const approval = el("div", { class: "vm-approval ui-card ui-card--elevated", hidden: true, role: "alertdialog", "aria-label": "Approval needed" },
    el("div", { class: "vm-approval__head" }, ui.icon("shield"), el("strong", {}, "Approve this?")),
    approvalBody, approvalMsg, el("div", { class: "vm-approval__btns" }, denyBtn, approveBtn));

  // Friday: the laptop-prep list, shown in full while it is being read out. A tap on "Open on laptop" is a clear approval.
  const frList = el("ul", { class: "vm-fr__list" });
  const frMsg = el("p", { class: "vm-fr__msg", role: "status" });
  const frOpenBtn = ui.button({ variant: "primary", size: "lg", icon: "laptop", label: "Open on laptop", class: "vm-fr__open" });
  const frNoBtn = ui.button({ variant: "secondary", size: "lg", label: "Not now", class: "vm-fr__no" });
  const frPanel = el("div", { class: "vm-fr ui-card ui-card--elevated", hidden: true, role: "group", "aria-label": "Waiting for your OK" },
    el("div", { class: "vm-fr__head" }, ui.icon("laptop"), el("strong", {}, "Ready to open on your laptop")),
    frList, frMsg, el("div", { class: "vm-fr__btns" }, frNoBtn, frOpenBtn));

  const stopBtn = ui.button({ variant: "danger", size: "md", icon: "stop", label: "Stop", class: "vm-stop", hidden: true,
    "aria-label": "Stop the assistant", onClick: () => { cancelSpeech(); aiCap.dataset.raw = ""; host.stopTurn(); } });
  const errBox = el("div", { class: "vm-error", hidden: true, role: "alert" });
  const foot = el("p", { class: "vm-foot" }, "Say “stop”, “cancel” or “new chat”. Tap the orb to interrupt.");

  const root = el("div", { class: "vm", id: "vm-overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "vm-title", "data-state": "listening" },
    top, el("div", { class: "vm-stage" }, who, orb, wave, title, hint, captions, stopBtn, errBox), frPanel, approval, foot);
  document.body.append(root);
  document.documentElement.classList.add("vm-open");
  release = ui.pushLayer(() => close(true));
  root.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  function setTts(on) {
    ttsOn = on;
    storeSet("hub.voice.tts", on ? "1" : "0");
    soundBtn.replaceChildren(ui.icon(on ? "volume" : "volume-off"));
    soundBtn.setAttribute("aria-pressed", String(!on));
    soundBtn.setAttribute("aria-label", on ? "Spoken replies on" : "Spoken replies off");
    if (!on) cancelSpeech();
  }
  setTts(ttsOn);

  function setPersona(p) {
    const key = p === "friday" ? "friday" : "jarvis";
    root.dataset.persona = key;
    who.textContent = key === "friday" ? (fr?.personaName?.() || "Friday") : names.jarvis;
  }
  const wakePersona = peekPreload()?.text ? peekPreload().persona : "";   // opened by the wake word with words to send
  root.dataset.persona = wakePersona || (host.friday ? "friday" : "jarvis");
  if (root.dataset.persona === "friday") who.textContent = "Friday";

  function setState(next, extraHint) {
    if (!alive) return;
    state = next;
    root.dataset.state = next;
    const [t, h] = STATES[next] || ["", ""];
    title.textContent = t;
    hint.textContent = extraHint ?? h;
    const labels = { listening: "Pause listening", paused: "Resume listening", thinking: "Stop the assistant", speaking: "Interrupt and listen", approval: "Stop and decline", error: "Voice orb" };
    orb.setAttribute("aria-label", labels[next] || "Voice orb");
    orb.disabled = next === "error";
    stopBtn.hidden = !(next === "thinking" || next === "approval");   // a visible Stop while a turn runs
    const icon = { listening: "mic", paused: "mic-off", thinking: "stop", speaking: "volume", approval: "stop", error: "mic-off" }[next] || "mic";
    orb.querySelector(".vm-orb__icon").replaceChildren(ui.icon(icon));
  }

  // ------------------------------------------------------------------ microphone + level meter
  function problem() {
    if (!ctx.secure) return { title: "Needs a secure address", text: "The microphone only works on the https:// address of your laptop. This page was opened on a plain one, so the browser blocks it. Open the https:// address and try again." };
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof window.MediaRecorder === "undefined") {
      return { title: "No recording here", text: "This browser cannot record audio in this page. Try Chrome on Android, or Safari on iPhone, on the https:// address." };
    }
    return null;
  }

  function fail(titleText, text, canRetry = true) {
    stopTick();
    cancelSpeech();
    setState("error", "");
    title.textContent = titleText;
    approval.hidden = true;
    errBox.hidden = false;
    errBox.replaceChildren(
      el("p", {}, text),
      el("div", { class: "vm-error__btns" },
        canRetry ? ui.button({ variant: "primary", icon: "refresh", label: "Try again", onClick: () => { errBox.hidden = true; start(); } }) : null,
        ui.button({ variant: "secondary", label: "Close", onClick: () => close() })));
    ui.haptic("warn");
  }

  async function openMic() {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
      audioCtx = new AC();
      if (audioCtx.state === "suspended") { try { await audioCtx.resume(); } catch (_) { /* needs a gesture; the tap that opened this counts */ } }
      source = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
    }
    try { wakeLock = await navigator.wakeLock?.request("screen"); } catch (_) { /* optional */ }
  }

  function closeMic() {
    try { wakeLock?.release(); } catch (_) { /* ignore */ }
    wakeLock = null;
    try { source?.disconnect(); } catch (_) { /* ignore */ }
    if (stream) for (const t of stream.getTracks()) t.stop();
    stream = null;
    try { audioCtx?.close(); } catch (_) { /* ignore */ }
    audioCtx = analyser = source = null;
  }

  let tickTimer = null;
  function startTick() {
    if (tickTimer) return;
    tickTimer = setInterval(() => {
      let raw = 0;
      if (analyser && state === "listening" && !paused) {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        raw = Math.sqrt(sum / buf.length);
      }
      const target = state === "listening" && !paused ? Math.min(1, raw * 7) : 0;
      level += (target - level) * (target > level ? 0.5 : 0.18);
      bump *= 0.9;
      let shown = level;
      if (state === "speaking") shown = Math.max(shown, 0.18 + bump * 0.6 + 0.12 * Math.sin(performance.now() / 160));
      root.style.setProperty("--lvl", shown.toFixed(3));
      if (vad && !paused) vad(raw);
    }, TICK_MS);
  }
  function stopTick() { clearInterval(tickTimer); tickTimer = null; vad = null; }

  // ------------------------------------------------------------------ listening (energy VAD)
  const MIMES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  function pickMime() { return MIMES.find((m) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || ""; }

  // Resolves with {blob, mime} when an utterance ends, or null if cancelled (orb tap, close, pause).
  function listen() {
    return new Promise((resolve) => {
      heard = false;
      let rec = null, chunks = [], finishing = false, finished = false;
      let floor = 0.006, loudMs = 0, speechStart = 0, lastLoud = 0, segStart = performance.now();
      const mime = pickMime();

      const begin = () => {
        chunks = [];
        try { rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
        catch (e) { done(null, e); return; }
        const mine = rec;
        mine.addEventListener("dataavailable", (e) => { if (e.data && e.data.size) chunks.push(e.data); });
        mine.addEventListener("stop", () => {
          if (mine !== rec) return;
          if (finishing) done({ blob: new Blob(chunks, { type: mine.mimeType || mime || "audio/webm" }), mime: mine.mimeType });
          else if (!finished) begin(); // rolled over while idle
        });
        mine.start(250);
        segStart = performance.now();
      };
      const done = (value, err) => {
        if (finished) return;
        finished = true;
        vad = null;
        wake = null;
        if (err) { listenError = err; resolve(null); } else resolve(value);
      };
      const stopRec = (send) => {
        finishing = send;
        if (!send) finished = true;
        if (rec && rec.state !== "inactive") rec.stop();
        else done(null);
        if (!send) { vad = null; wake = null; resolve(null); }
      };

      wake = (why) => { // orb tap: send what was said so far, or cancel
        if (why === "send" && heard) stopRec(true);
        else stopRec(false);
      };

      vad = (raw) => {
        const now = performance.now();
        const thr = Math.max(0.03, floor * 3.2);
        const loud = raw > thr;
        if (!loud && !heard) floor = floor * 0.97 + Math.min(raw, 0.05) * 0.03;
        if (loud) {
          loudMs += TICK_MS;
          lastLoud = now;
          if (!heard && loudMs >= MIN_SPEECH_MS) { heard = true; speechStart = now; root.classList.add("is-heard"); hint.textContent = "Keep going, I am listening…"; }
        }
        if (heard && (now - lastLoud > silenceMs || now - speechStart > MAX_UTTERANCE_MS)) stopRec(true);
        else if (!heard && now - segStart > IDLE_ROLL_MS) { // keep the recording short while nobody speaks
          finishing = false;
          if (rec && rec.state !== "inactive") rec.stop();
        }
      };
      begin();
    });
  }
  let listenError = null;

  // ------------------------------------------------------------------ speaking
  function cancelSpeech() {
    tts.q.length = 0; tts.buf = ""; tts.cancelled = true;
    if (utter) { utter.cancel(); utter = null; }
    try { window.speechSynthesis?.cancel(); } catch (_) { /* ignore */ }
  }

  // Streamed speech: sentences are spoken while the reply is still arriving (no waiting for the full text).
  function ttsReset() { tts.q.length = 0; tts.buf = ""; tts.cancelled = false; tts.chars = 0; tts.capped = false; tts.any = false; }
  function ttsFeed(chunk, final = false) {
    if (!ttsOn || tts.cancelled || tts.capped) return;
    tts.buf += chunk || "";
    const { done, rest } = splitStream(tts.buf, { final });
    tts.buf = rest;
    for (const sentence of done) {
      const plain = speakable(sentence, SPEAK_CAP).replace(/ The rest is in the chat\.$/, "");
      if (!plain) continue;
      if (tts.chars + plain.length > SPEAK_CAP) { tts.q.push("The rest is in the chat."); tts.capped = true; break; }
      tts.chars += plain.length;
      tts.q.push(plain);
    }
    ttsPump();
  }
  async function ttsPump() {
    if (tts.busy) return;
    tts.busy = true;
    try {
      while (alive && !tts.cancelled && tts.q.length) {
        tts.any = true;
        if (state === "thinking") setState("speaking");
        await speak(tts.q.shift());
      }
    } finally {
      tts.busy = false;
      for (const w of tts.waiters.splice(0)) w();
    }
  }
  function ttsIdle() { return tts.busy || tts.q.length ? new Promise((r) => tts.waiters.push(r)) : Promise.resolve(); }

  // Resolves when finished or cancelled. Reads sentence by sentence (long utterances stall in some browsers).
  function speak(text) {
    return new Promise((resolve) => {
      const synth = window.speechSynthesis;
      if (!synth || typeof SpeechSynthesisUtterance === "undefined" || !text) { resolve(); return; }
      const parts = sentences(text);
      let i = 0, ended = false, guard = null;
      const finish = () => { if (ended) return; ended = true; clearTimeout(guard); utter = null; resolve(); };
      utter = { cancel: () => { ended = true; clearTimeout(guard); try { synth.cancel(); } catch (_) { /* ignore */ } utter = null; resolve(); } };
      const next = () => {
        if (ended) return;
        if (i >= parts.length) { finish(); return; }
        const u = new SpeechSynthesisUtterance(parts[i++]);
        let started = false;
        // Browsers without voices never fire events: give up quietly instead of hanging the loop.
        clearTimeout(guard);
        guard = setTimeout(() => { if (!started) { try { synth.cancel(); } catch (_) { /* ignore */ } finish(); } }, 2500);
        u.onstart = () => { started = true; clearTimeout(guard); };
        u.onboundary = () => { bump = 1; };
        u.onend = () => { clearTimeout(guard); next(); };
        u.onerror = () => { clearTimeout(guard); finish(); };
        try { synth.speak(u); } catch (_) { finish(); }
      };
      next();
    });
  }

  // ------------------------------------------------------------------ approvals (inside the overlay)
  const CRITICAL_VOICE_NOTE = "That is a critical action, so I can't take a spoken yes. Type CONFIRM on the card on your screen, then you get a few seconds to cancel.";
  const hooks = {
    onEvent(ev) {
      if (!alive) return;
      if (ev.type === "text") {
        aiCap.textContent = (aiCap.dataset.raw = (aiCap.dataset.raw || "") + (ev.t || "")).slice(-420);
        aiCap.scrollTop = aiCap.scrollHeight;
        ttsFeed(ev.t || "");
      } else if (ev.type === "tool_start") {
        sub.textContent = `Using ${ev.name || "a tool"}…`;
      } else if (ev.type === "tool_result") {
        sub.textContent = "";
      }
    },
    onApproval(card) {
      if (!alive) return;
      pendingApproval = card;
      approvalBody.replaceChildren(
        el("p", { class: "vm-approval__what" }, card.name === "run_command" ? "I want to run this command on your laptop:" : `I want to use “${card.name}”:`),
        ...host.approvalParts(card.input).flatMap((p) => [
          el("div", { class: "vm-approval__label" }, p.label),
          el("pre", { class: "vm-approval__pre" }, p.text),
        ]));
      approvalMsg.textContent = "";
      approveBtn.disabled = denyBtn.disabled = false;
      approveBtn.onclick = () => respond(card, true);
      denyBtn.onclick = () => respond(card, false);
      if (card.critical) {
        // Critical (N3) actions are never approved from voice mode: the typed CONFIRM and the countdown are on the Talk screen.
        approveBtn.hidden = true;
        approvalMsg.textContent = CRITICAL_VOICE_NOTE;
        denyBtn.disabled = false;
        say(CRITICAL_VOICE_NOTE, { raw: true });
      } else approveBtn.hidden = false;
      approval.hidden = false;
      root.classList.add("has-approval");
      cancelSpeech();
      tts.cancelled = false;
      setState("approval");
      ui.haptic("warn");
      approveBtn.focus({ preventScroll: true });
    },
    onApprovalDone(card) {
      if (pendingApproval !== card) return;
      pendingApproval = null;
      approval.hidden = true;
      root.classList.remove("has-approval");
      if (alive && state === "approval") setState("thinking");
    },
    onApprovalError(card, error) {
      approveBtn.disabled = denyBtn.disabled = false;
      approvalMsg.textContent = `Your answer did not reach the laptop: ${error.message} Try again.`;
    },
  };
  async function respond(card, ok) {
    approveBtn.disabled = denyBtn.disabled = true;
    approvalMsg.textContent = ok ? "Approved. Running…" : "Declined.";
    ui.haptic(ok ? "success" : "medium");
    await card.respond?.(ok);
  }

  // ------------------------------------------------------------------ Friday persona + intent shortcuts
  // Handled here, before the phrase would go to the assistant. Never approves an assistant tool call: only the
  // laptop-prep list, and only after the exact items are read back and the owner says a clear yes or taps.
  let fr = null, frOn = false, confirming = false;
  const prepShown = () => !!(frOn && fr.FR.prep && fr.FR.prep.items.length && !fr.FR.prep.done);

  async function say(text, { raw = false } = {}) {
    const plain = raw || !fr ? String(text) : fr.cleanSpeech(text);
    aiCap.dataset.raw = "";
    aiCap.textContent = plain;
    if (fr) fr.FR.lastSpoken = text;
    if (plain && ttsOn) { setState("speaking"); await speak(plain); }
    else if (plain) await new Promise((r) => setTimeout(r, 800));
  }
  function renderPanel() {
    const prep = fr && fr.FR.prep;
    const show = prepShown();
    frPanel.hidden = !show;
    root.classList.toggle("has-fr", show);
    if (!show) return;
    frList.replaceChildren(...prep.items.map((i) => el("li", {},
      el("span", { class: "vm-fr__kind" }, fr.kindOf(i.kind).label), el("code", { class: "vm-fr__target" }, i.target))));
    frMsg.textContent = "";
    frOpenBtn.disabled = frNoBtn.disabled = false;
    frOpenBtn.onclick = () => { confirming = false; runLaptopPrep(); };
    frNoBtn.onclick = () => { confirming = false; frPanel.hidden = true; root.classList.remove("has-fr"); sub.textContent = "Okay. Nothing was opened."; };
  }
  function readout(items, intro) {
    return `${intro} ${items.map((i, n) => `${n + 1}. ${fr.kindOf(i.kind).label}, ${i.target}.`).join(" ")}`;
  }
  async function runLaptopPrep() {
    const prep = fr.FR.prep;
    if (!prep || !prep.items.length) return;
    frOpenBtn.disabled = frNoBtn.disabled = true;
    frMsg.textContent = "Opening on your laptop…";
    setState("thinking");
    try {
      const results = await fr.runPrep(prep.items.map((i) => i.id));
      const bad = results.filter((r) => !r.ok).length;
      renderPanel();
      await say(bad ? `Done, mostly. ${results.length - bad} opened, and ${bad} did not. You can see which in Friday.` : `Done. Everything is open on your laptop.`);
    } catch (error) {
      frOpenBtn.disabled = frNoBtn.disabled = false;
      frMsg.textContent = `That did not reach the laptop: ${error.message}`;
      await say("I could not reach your laptop just now. Nothing was opened.");
    }
  }
  async function fridayHandle(text) {
    const intent = fr.matchIntent(text, { prepShown: prepShown(), confirming });
    if (intent !== "yes" && intent !== "repeat") confirming = false;
    if (!intent) { if (prepShown()) renderPanel(); return false; }
    userCap.textContent = `“${text}”`;
    sub.textContent = "";
    const fail = async (error) => {
      if (fr.isMissing(error)) await say("I am not set up on your laptop yet. Update the hub there and I will be ready.");
      else await say("I could not get that just now. Ask me again in a moment.");
    };
    try {
      if (intent === "brief") {
        setState("thinking", "Looking through your mail, chats and tasks…");
        const b = await fr.loadBrief();
        aiCap.dataset.raw = "";
        await say(`${b.spoken || fr.toneLine(b)} Shall I plan your day, or get your laptop ready?`);
      } else if (intent === "plan") {
        setState("thinking", "Finding a calm shape for your day…");
        const p = await fr.loadPlan();
        await say(`${p.spoken || "Here is a gentle plan."} You can add it to your to-dos on the Friday screen.`);
      } else if (intent === "prep") {
        setState("thinking", "Picking what you will want open…");
        const prep = await fr.loadPrep();
        renderPanel();
        if (!prep.items.length) await say("I could not find anything worth opening right now.");
        else if (fcMod && fcMod.isOn()) {   // Full Control: no approval step, it just goes (and says what it is opening)
          await say(readout(prep.items, `Opening ${prep.items.length} ${prep.items.length === 1 ? "thing" : "things"} on your laptop.`), { raw: true });
          await runLaptopPrep();
        } else await say(readout(prep.items, `I would open ${prep.items.length} ${prep.items.length === 1 ? "thing" : "things"} on your laptop. Nothing opens until you say so.`) + " Say approve when you are happy, or no to leave it.", { raw: true });
      } else if (intent === "approve") {
        const items = fr.FR.prep.items;
        confirming = true;
        renderPanel();
        await say(readout(items, "To be clear, I will open exactly this on your laptop:") + " Say yes to open them, or no to cancel. You can also tap Open on laptop.", { raw: true });
      } else if (intent === "yes") {
        confirming = false;
        await runLaptopPrep();
      } else if (intent === "no") {
        frPanel.hidden = true; root.classList.remove("has-fr");
        await say("Okay. I will not open anything.");
      } else if (intent === "repeat") {
        const last = fr.FR.lastSpoken;
        if (last) await say(last, { raw: /laptop/i.test(last) && prepShown() }); else await say("I have not said anything yet. Ask me for the update.");
      }
    } catch (error) {
      if (alive) await fail(error);
    }
    return true;
  }

  // ------------------------------------------------------------------ orb tap
  async function onOrbTap() {
    ui.haptic("light");
    if (state === "listening") {
      if (heard && wake) wake("send");
      else if (!paused) { paused = true; wake?.("cancel"); setState("paused"); root.classList.remove("is-heard"); }
    } else if (state === "paused") {
      paused = false;
      resumeSignal?.();
    } else if (state === "speaking") {
      cancelSpeech();
    } else if (state === "thinking" || state === "approval") {
      cancelSpeech();
      aiCap.dataset.raw = "";
      await host.stopTurn();
    }
  }
  let resumeSignal = null;

  // ------------------------------------------------------------------ the loop
  async function transcribe(blob, mime) {
    const form = new FormData();
    form.append("file", blob, `voice.${/mp4/.test(mime || blob.type) ? "m4a" : "webm"}`);
    const result = await ctx.api("/api/transcribe", { method: "POST", form });
    return String((result && result.text) || "").trim();
  }

  async function loop() {
    let failures = 0;
    if (!fr) {
      try {
        fr = await import("./friday.js");
        await fr.loadSettings(ctx);
        frOn = fr.FR.available === true;
      } catch (_) { frOn = false; }
    }
    try {
      fcMod = await import("./fullcontrol.js");
      const info = await fcMod.refresh();
      if (info) names = { jarvis: info.jarvis_name || "Jarvis", call: info.display_name || "" };
    } catch (_) { /* older hub: names stay default */ }
    if (!alive) return;
    setPersona(wakePersona || (host.friday ? "friday" : "jarvis"));
    if (frOn) {
      top.querySelector(".vm-top__mid").firstChild.textContent = `${fr.personaName()} `;
      renderPanel();
      if (host.friday && !wakePersona) {
        const line = `${fr.greetingText()} Ready when you are.`;
        await say(line);
        if (!alive) return;
      }
    }
    while (alive) {
      // A chat turn started from the keyboard may still be running: wait for it, never talk over it.
      if (host.isBusy()) {
        setState("thinking", "Finishing what I was doing…");
        while (alive && host.isBusy()) await new Promise((r) => setTimeout(r, 300));
        continue;
      }
      paused = false;
      userCap.textContent = userCap.textContent; // keep the last words visible while listening
      setState("listening");
      root.classList.remove("is-heard");
      let text = "", pre = takePreload();
      if (pre) { text = pre.text; sub.textContent = ""; } else {
        const heardNow = await listen();
        if (!alive) break;
        if (listenError) { const e = listenError; listenError = null; fail("Recording failed", `The microphone could not start recording: ${e.message || e}.`); return; }
        if (!heardNow) {
          if (paused) { await new Promise((r) => { resumeSignal = r; }); resumeSignal = null; }
          continue;
        }
        root.classList.remove("is-heard");
        if (heardNow.blob.size < 1500) continue;

        setState("thinking", "Turning your voice into text…");
        try {
          text = await transcribe(heardNow.blob, heardNow.mime);
          failures = 0;
        } catch (error) {
          if (!alive) break;
          if (error instanceof ApiError && error.status === 503) { fail("Voice is not set up", `${error.message} Install the voice requirements on the laptop, then try again.`); return; }
          failures += 1;
          if (failures >= 3) { fail("Cannot reach your laptop", `Transcribing keeps failing: ${error.message}`); return; }
          sub.textContent = `Could not turn that into text (${error.message}). Listening again…`;
          await new Promise((r) => setTimeout(r, 1500));
          sub.textContent = "";
          continue;
        }
        if (!alive) break;
        if (!text) { sub.textContent = "I did not catch that."; continue; }
        sub.textContent = "";
      }

      // "Jarvis, ..." goes to the laptop operator, "Friday, ..." to the day assistant; no name keeps the old behaviour.
      const routed = pre ? { persona: "", text } : splitName(text, names.jarvis);
      let persona = pre?.persona || (host.friday ? "friday" : "");
      if (pre?.persona) setPersona(persona);
      if (!pre && routed.persona) {
        persona = routed.persona;
        setPersona(persona);
        text = routed.text;
        if (!text) { userCap.textContent = `“${routed.persona === "friday" ? "Friday" : names.jarvis}”`; await say(`Yes${names.call ? ", " + names.call : ""}?`, { raw: true }); continue; }
      }
      if (persona !== "jarvis" && frOn && await fridayHandle(text)) continue;
      if (!alive) break;
      const cmd = commandFor(text);
      if (cmd === "stop") { confirming = false; userCap.textContent = `“${text}”`; aiCap.textContent = ""; aiCap.dataset.raw = ""; sub.textContent = "Stopped."; await host.stopTurn(); continue; }
      if (cmd === "new") {
        userCap.textContent = `“${text}”`; aiCap.textContent = ""; aiCap.dataset.raw = "";
        await host.newChat();
        sub.textContent = "Started a new chat.";
        if (ttsOn) { setState("speaking"); await speak("New chat."); }
        continue;
      }
      if (cmd === "close") { close(); return; }

      // Double-submit guard: one turn at a time, and the same words twice within 1.5 s count once (echo, double tap).
      if (asking || (text === lastText && Date.now() - lastAt < 1500)) continue;
      asking = true; lastText = text; lastAt = Date.now();
      userCap.textContent = `“${text}”`;
      aiCap.textContent = "";
      aiCap.dataset.raw = "";
      ttsReset();
      setState("thinking");
      let result;
      try { result = await host.ask(text, hooks, persona || "auto"); } finally { asking = false; }
      if (!alive) break;
      pendingApproval = null;
      approval.hidden = true;
      root.classList.remove("has-approval");
      if (!result) continue;
      if (result.hadError && !result.text) { sub.textContent = result.error || "Something went wrong."; await new Promise((r) => setTimeout(r, 1200)); continue; }
      if (result.stopped) { sub.textContent = "Stopped."; continue; }
      const plain = speakable(result.text);
      if (plain) aiCap.textContent = plain.replace(/ The rest is in the chat\.$/, "");
      if (plain && ttsOn) {
        if (!tts.cancelled) ttsFeed("", true);                      // speak whatever is left of the reply
        if (!tts.any && !tts.q.length && !tts.cancelled) { tts.q.push(plain); ttsPump(); }   // nothing streamed: read it whole
        await ttsIdle();
      } else if (plain) {
        await new Promise((r) => setTimeout(r, 600));
      }
    }
  }

  // ------------------------------------------------------------------ lifecycle
  async function start() {
    const p = problem();
    if (p) { fail(p.title, p.text, false); return; }
    setState("listening", "Asking for the microphone…");
    try {
      await openMic();
    } catch (error) {
      const name = error && error.name;
      if (name === "NotAllowedError" || name === "SecurityError") {
        fail("Microphone is blocked", "Allow the microphone for this site (tap the lock icon next to the address, then Permissions), and try again. You can keep typing in the chat.");
      } else if (name === "NotFoundError" || name === "OverconstrainedError") {
        fail("No microphone found", "This device has no microphone the browser can use.");
      } else {
        fail("Microphone problem", `The microphone could not start: ${(error && error.message) || error}`);
      }
      return;
    }
    if (!alive) { closeMic(); return; }
    startTick();
    const warm = () => { ctx.api("/api/assistant/warm", { method: "POST" }).catch(() => {}); };
    warm();
    warmTimer = setInterval(warm, WARM_EVERY_MS);   // keep_alive for the whole voice session
    loop().catch((error) => { if (alive) fail("Something went wrong", String((error && error.message) || error)); });
  }

  let closed = false;
  function close(fromBack = false) {
    if (closed) return;
    closed = true;
    alive = false;
    wake?.("cancel");
    resumeSignal?.();
    clearInterval(warmTimer);
    cancelSpeech();
    stopTick();
    closeMic();
    // Leaving voice mode with a request waiting must not leave it hanging, and must never approve it.
    if (host.isBusy()) host.stopTurn();
    root.classList.add("is-closing");
    document.documentElement.classList.remove("vm-open");
    setTimeout(() => root.remove(), 180);
    if (!fromBack) release?.();
    active = null;
    host.onClose?.();
  }

  active = { close };
  start();
  return active;
}
