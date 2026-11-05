// Hands-free voice mode: a full-screen overlay that loops  listen -> transcribe -> assistant -> speak -> listen.
// Opened by assistant.js, which passes a `host` (see openVoiceMode). Nothing is ever approved automatically:
// a tool approval shows big Approve / Deny buttons here and the loop simply waits for the tap.
import { el, ApiError } from "./api.js";

const SILENCE_MS_DEFAULT = 1200;   // quiet this long after speech ends the recording
const MAX_UTTERANCE_MS = 30000;    // a single spoken message is cut at this length
const MIN_SPEECH_MS = 200;         // loud for this long in total before it counts as speech (ignores clicks)
const IDLE_ROLL_MS = 20000;        // with no speech, restart the recorder so it never grows without bound
const SPEAK_CAP = 700;             // characters read aloud at most
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

// Markdown to plain speakable text, capped at a sentence boundary.
export function speakable(markdown, cap = SPEAK_CAP) {
  let s = String(markdown || "");
  s = s.replace(/```[\s\S]*?(```|$)/g, " (code omitted) ");
  s = s.replace(/`([^`]*)`/g, "$1");
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  s = s.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  s = s.replace(/<[^>]+>/g, " ");
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  s = s.replace(/^\s{0,3}>\s?/gm, "");
  s = s.replace(/^\s*([-*+]|\d+[.)])\s+/gm, "");
  s = s.replace(/^\s*\|?[\s:|-]{3,}\|?\s*$/gm, " ");
  s = s.replace(/\|/g, ", ");
  s = s.replace(/(\*\*|__|\*|_|~~)/g, "");
  s = s.replace(/https?:\/\/\S+/g, "a link");
  s = s.replace(/\s*\n+\s*/g, ". ").replace(/\.\s*\./g, ".").replace(/([:;,])\./g, "$1");
  s = s.replace(/\s+/g, " ").trim();
  if (s.length > cap) {
    const cut = s.slice(0, cap);
    const end = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
    s = (end > cap * 0.4 ? cut.slice(0, end + 1) : cut.replace(/\s+\S*$/, "") + "…") + " The rest is in the chat.";
  }
  return s;
}

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

// host: {ctx, ui, provider(), isBusy(), ask(text, hooks) -> Promise<{text, stopped, hadError, error}>,
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
  let release = null;  // ui.pushLayer release
  const buf = new Float32Array(1024);

  // ------------------------------------------------------------------ DOM
  const orb = el("button", { type: "button", class: "vm-orb", "aria-label": "Voice orb", onclick: () => onOrbTap() },
    el("span", { class: "vm-orb__ring vm-orb__ring--a", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__ring vm-orb__ring--b", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__glow", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__core", "aria-hidden": "true" }),
    el("span", { class: "vm-orb__icon", "aria-hidden": "true" }));
  const title = el("h2", { class: "vm-title", id: "vm-title" });
  const hint = el("p", { class: "vm-hint" });
  const userCap = el("p", { class: "vm-cap vm-cap--user" });
  const aiCap = el("p", { class: "vm-cap vm-cap--ai", "aria-live": "polite" });
  const sub = el("p", { class: "vm-sub" });
  const captions = el("div", { class: "vm-captions" }, userCap, aiCap, sub);
  const soundBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "volume", class: "vm-sound", "aria-label": "Spoken replies", onClick: () => setTts(!ttsOn) });
  const closeBtn = ui.button({ variant: "ghost", iconOnly: true, icon: "x", class: "vm-close", "aria-label": "Close voice mode", onClick: () => close() });
  const badge = el("span", { class: "vm-provider ui-badge ui-badge--accent" }, host.provider() === "local" ? "Local" : "Claude");
  const top = el("div", { class: "vm-top" }, soundBtn, el("div", { class: "vm-top__mid" }, "Voice mode ", badge), closeBtn);

  const approvalBody = el("div", { class: "vm-approval__body" });
  const approveBtn = ui.button({ variant: "danger", size: "lg", icon: "check", label: "Approve", class: "vm-approve" });
  const denyBtn = ui.button({ variant: "secondary", size: "lg", icon: "x", label: "Deny", class: "vm-deny" });
  const approvalMsg = el("p", { class: "vm-approval__msg", role: "status" });
  const approval = el("div", { class: "vm-approval ui-card ui-card--elevated", hidden: true, role: "alertdialog", "aria-label": "Approval needed" },
    el("div", { class: "vm-approval__head" }, ui.icon("shield"), el("strong", {}, "Approve this?")),
    approvalBody, approvalMsg, el("div", { class: "vm-approval__btns" }, denyBtn, approveBtn));

  const errBox = el("div", { class: "vm-error", hidden: true, role: "alert" });
  const foot = el("p", { class: "vm-foot" }, "Say “stop”, “cancel” or “new chat”. Tap the orb to interrupt.");

  const root = el("div", { class: "vm", id: "vm-overlay", role: "dialog", "aria-modal": "true", "aria-labelledby": "vm-title", "data-state": "listening" },
    top, el("div", { class: "vm-stage" }, orb, title, hint, captions, errBox), approval, foot);
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
    if (utter) { utter.cancel(); utter = null; }
    try { window.speechSynthesis?.cancel(); } catch (_) { /* ignore */ }
  }

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
      try { synth.cancel(); } catch (_) { /* ignore */ }
      next();
    });
  }

  // ------------------------------------------------------------------ approvals (inside the overlay)
  const hooks = {
    onEvent(ev) {
      if (!alive) return;
      if (ev.type === "text") {
        aiCap.textContent = (aiCap.dataset.raw = (aiCap.dataset.raw || "") + (ev.t || "")).slice(-420);
        aiCap.scrollTop = aiCap.scrollHeight;
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
      approval.hidden = false;
      root.classList.add("has-approval");
      cancelSpeech();
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
      let text = "";
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

      const cmd = commandFor(text);
      if (cmd === "stop") { userCap.textContent = `“${text}”`; aiCap.textContent = ""; aiCap.dataset.raw = ""; sub.textContent = "Stopped."; await host.stopTurn(); continue; }
      if (cmd === "new") {
        userCap.textContent = `“${text}”`; aiCap.textContent = ""; aiCap.dataset.raw = "";
        await host.newChat();
        sub.textContent = "Started a new chat.";
        if (ttsOn) { setState("speaking"); await speak("New chat."); }
        continue;
      }
      if (cmd === "close") { close(); return; }

      userCap.textContent = `“${text}”`;
      aiCap.textContent = "";
      aiCap.dataset.raw = "";
      setState("thinking");
      const result = await host.ask(text, hooks);
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
        setState("speaking");
        await speak(plain);
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
    loop().catch((error) => { if (alive) fail("Something went wrong", String((error && error.message) || error)); });
  }

  let closed = false;
  function close(fromBack = false) {
    if (closed) return;
    closed = true;
    alive = false;
    wake?.("cancel");
    resumeSignal?.();
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
