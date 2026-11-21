// Interview coach: likely questions from a posting, practice with feedback, a STAR story bank, "tell me about yourself" (60 and 90 s),
// a pre-interview brief and a negotiation cheat sheet. Practice material only: nothing is sent anywhere. Your practice answers are
// stored on this laptop and can be deleted. The screen makes ONE data request when it opens (GET /api/interviews); every other
// request happens only when you tap a button, and nothing polls. Routes: docs/api/interviews.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/interviews.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TABS = [["questions", "Questions"], ["practice", "Practice"], ["stories", "Stories"], ["about", "About me"], ["brief", "Brief"], ["day", "Day pack"], ["debrief", "Debrief"], ["pay", "Pay"]];
const SILENT = 0.012;          // loudness (0 to 1) under which the microphone is counted as silent
const MIN_PAUSE = 1.0;         // seconds of silence counted as a pause
const MAX_RECORD = 300;        // seconds: a recording stops by itself
const KIND_TONE = { technical: "accent", behavioural: "good", motivation: "gray" };

async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}
const field = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "300", ...attrs }); i.value = value ?? ""; return i; };
const area = (value, rows, attrs = {}) => { const t = el("textarea", { class: "ui-input iv-area", rows: String(rows), ...attrs }); t.value = value ?? ""; return t; };
const list = (items) => el("ul", { class: "iv-list" }, ...items.map((t) => el("li", {}, t)));

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "iv ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", tab = "questions";
  let job = { application_id: "", title: "", company: "", description: "" };
  let ai = false, questions = null, drill = { question: "", kind: "behavioural", answer: "", result: null, spoken: null }, about = null, brief = null;
  let pack = null, packForm = { when: "", place: "", who: "", round: "", fetch: false, save: true }, note = null;
  let debrief = { company: "", role: "", round: "", interviewer: "", date: "", rating: "", asked: "", went_well: "", to_improve: "", follow: "", used: [], story: { title: "", situation: "", task: "", action: "", result: "" } };
  let lastDebrief = null;
  const ticked = new Set();

  async function load() {
    try { data = await api("/api/interviews"); error = ""; ai = ai || data.settings.ai; } catch (e) { error = e.message; }
    paint();
  }
  const jobBody = () => {
    const b = job.application_id ? { application_id: job.application_id } : { title: job.title, company: job.company, description: job.description };
    return b;
  };
  const hasJob = () => !!(job.application_id || job.title.trim() || job.description.trim());

  function paint() {
    root.replaceChildren();
    if (error) { root.append(ui.notice(error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load })); return; }
    if (!data) { root.append(ui.skeleton("list", 3)); return; }
    root.append(ui.notice("Practice material only. Nothing is sent. Answers you practise stay on this laptop; you can delete them any time.", { tone: "info" }),
      jobPicker(), ui.segmented({ options: TABS.map(([value, label]) => ({ value, label })), value: tab, label: "Interview coach sections", onChange: (v) => { tab = v; paint(); } }),
      { questions: questionsTab, practice: practiceTab, stories: storiesTab, about: aboutTab, brief: briefTab, day: dayTab, debrief: debriefTab, pay: payTab }[tab]());
    ui.renumberEyebrows(root);
  }

  // ---- the posting ----
  function jobPicker() {
    const app = el("select", { class: "ui-input" }, el("option", { value: "" }, "Paste a job instead"),
      ...data.applications.map((a) => el("option", { value: a.id }, [a.title, a.company].filter(Boolean).join(" at ") || a.id)));
    app.value = job.application_id;
    app.addEventListener("change", () => { job.application_id = app.value; questions = null; brief = null; about = null; paint(); });
    const nodes = [field("Application", app)];
    if (!job.application_id) {
      const title = input(job.title, { placeholder: "Job title", maxlength: "200" }), company = input(job.company, { placeholder: "Company", maxlength: "120" });
      const desc = area(job.description, 4, { placeholder: "Paste the job description" });
      const sync = () => { job.title = title.value; job.company = company.value; job.description = desc.value; };
      [title, company, desc].forEach((n) => n.addEventListener("change", sync));
      nodes.push(field("Job title", title), field("Company", company), field("Job description", desc));
    }
    const aiSwitch = ui.switchControl({ checked: ai, label: "Use AI for extra questions and critique", onChange: (v) => { ai = v; } });
    nodes.push(el("div", { class: "ui-row iv-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Use AI (optional)"),
      el("span", { class: "ui-row__sub" }, "Questions: only the posting text is sent. Critique: also the answer you type. Off, everything still works.")), aiSwitch));
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", "The job"), ...nodes);
  }

  // ---- (a) questions ----
  function questionsTab() {
    const go = ui.button({ label: "Find likely questions", icon: "quiz", variant: "primary", onClick: () => ui.busy(go, async () => {
      if (!hasJob()) { ui.toast("Pick an application or paste a job first.", { kind: "warn" }); return; }
      try { questions = await api("/api/interviews/questions", { method: "POST", json: { ...jobBody(), ai } }); paint(); } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Likely questions"), go)];
    if (questions) {
      if (questions.ai_note) out[0].append(el("p", { class: "iv-note" }, questions.ai_note));
      if (questions.skills.length) out[0].append(el("p", { class: "iv-note" }, "Skills the posting names: " + questions.skills.join(", ")));
      for (const q of questions.questions) {
        out[0].append(el("div", { class: "iv-q" }, el("div", { class: "iv-badges" }, ui.badge(q.kind, KIND_TONE[q.kind] || "gray"), q.source === "ai" ? ui.badge("AI", "accent") : null),
          el("p", { class: "iv-qtext" }, q.text), el("p", { class: "iv-note" }, q.why),
          q.fact ? el("p", { class: "iv-note" }, "Your fact that fits: " + q.fact.text) : null,
          ui.button({ label: "Practise this", icon: "mic", variant: "tonal", size: "sm", onClick: () => { drill = { question: q.text, kind: q.kind, answer: "", result: null }; tab = "practice"; paint(); } })));
      }
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out);
  }

  // ---- (b) practice ----
  function practiceTab() {
    const question = area(drill.question, 2, { placeholder: "Pick a question on the Questions tab, or type one" });
    const kind = el("select", { class: "ui-input" }, ...["behavioural", "technical", "motivation"].map((k) => el("option", { value: k }, k))); kind.value = drill.kind;
    const answer = area(drill.answer, 8, { placeholder: "Type your answer, or tap the microphone and say it." });
    answer.addEventListener("input", () => { drill.spoken = null; });      // editing the text makes it a typed answer: the recording numbers no longer fit
    const keep = ui.switchControl({ checked: true, label: "Keep this answer on this laptop" });
    const mic = micButton(answer);
    const sendFeedback = async (spoken) => {
      drill = { question: question.value, kind: kind.value, answer: answer.value, result: null, spoken: spoken ?? drill.spoken };
      const json = { question: drill.question, kind: drill.kind, answer: drill.answer, ai, role: job.title, company: job.company };
      if (drill.spoken) { json.seconds = drill.spoken.seconds; json.pauses = drill.spoken.pauses; }
      drill.result = await api(keep.input.checked ? "/api/interviews/practice" : "/api/interviews/feedback", { method: "POST", json });
      if (keep.input.checked) data = await api("/api/interviews");
      paint();
    };
    const go = ui.button({ label: "Get feedback", icon: "check", variant: "primary", onClick: () => ui.busy(go, async () => { try { await sendFeedback(); } catch (e) { fail(e); } }) });
    const speak = recorder(async (text, spoken) => { answer.value = text; await sendFeedback(spoken); });
    const box = el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Practise an answer"), field("Question", question), field("Kind", kind),
      field("Your answer", answer, "Aim to speak for 30 to 90 seconds."), speak, mic,
      el("div", { class: "ui-row iv-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Keep this answer on this laptop")), keep), go);
    const out = [box];
    if (drill.result) out.push(feedbackCard(drill.result));
    out.push(historyCard());
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out);
  }

  function micButton(target) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return el("p", { class: "iv-note" }, "To dictate, use the microphone key on your phone keyboard.");
    let rec = null;
    const b = ui.button({ label: "Dictate", icon: "mic", variant: "tonal", size: "sm", onClick: () => {
      if (rec) { rec.stop(); return; }
      rec = new SR(); rec.lang = navigator.language || "en-US"; rec.interimResults = false; rec.continuous = true;
      rec.onresult = (e) => { for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) target.value = (target.value + " " + e.results[i][0].transcript).trim(); };
      rec.onend = () => { rec = null; b.classList.remove("is-live"); };
      rec.onerror = () => { rec = null; b.classList.remove("is-live"); ui.toast("Dictation stopped. You can type instead.", { kind: "warn" }); };
      b.classList.add("is-live"); rec.start();
    } });
    return el("div", { class: "iv-actions" }, b, el("span", { class: "iv-note" }, "Tap again to stop."));
  }

  // ---- (b2) spoken practice: record, transcribe (POST /api/transcribe), measure ----
  function recorder(onDone) {
    const problem = !window.isSecureContext ? "The microphone needs the secure (HTTPS) address of your laptop. This page is on a plain address, so it cannot record here. You can still type or dictate."
      : (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") ? "This browser cannot record audio on this page. You can still type or dictate." : "";
    if (problem) return el("p", { class: "iv-note" }, problem);
    const meter = el("span", {}), status = el("p", { class: "iv-note", role: "status", "aria-live": "polite" }, "Tap, answer out loud, tap again. The recording is turned into text on your laptop, then discarded.");
    let rec = null;
    const stopAll = () => { try { rec?.recorder.state !== "inactive" && rec?.recorder.stop(); } catch { /* already stopped */ } };
    const btn = ui.button({ label: "Record and measure", icon: "mic", variant: "tonal", onClick: () => (rec ? stopAll() : start()) });
    async function start() {
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch { ui.toast("The microphone is not allowed. You can type instead.", { kind: "warn" }); return; }
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find((m) => { try { return MediaRecorder.isTypeSupported(m); } catch { return false; } }) || "";
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      const chunks = [], t0 = performance.now();
      // loudness every 100 ms WHILE recording only: first and last speech give the speaking time, silences of 1 s or more are the pauses
      let analyser = null, audio = null, tick = null, first = null, last = null, silentFrom = null; const pauses = [];
      try {
        const AC = window.AudioContext || window.webkitAudioContext; audio = new AC();
        analyser = audio.createAnalyser(); analyser.fftSize = 1024; audio.createMediaStreamSource(stream).connect(analyser);
      } catch { analyser = null; }
      const buf = analyser ? new Float32Array(analyser.fftSize) : null;
      const sample = () => {
        if (!root.isConnected) { stopAll(); return; }
        const now = (performance.now() - t0) / 1000;
        if (now >= MAX_RECORD) { stopAll(); return; }
        if (!analyser) return;
        analyser.getFloatTimeDomainData(buf);
        let sum = 0; for (const v of buf) sum += v * v;
        const level = Math.sqrt(sum / buf.length);
        meter.style.width = Math.min(100, Math.round(level * 400)) + "%";
        if (level >= SILENT) {
          if (first === null) first = now;
          if (silentFrom !== null && first !== null && now - silentFrom >= MIN_PAUSE) pauses.push(Math.round((now - silentFrom) * 10) / 10);
          silentFrom = null; last = now;
        } else if (silentFrom === null && first !== null) silentFrom = now;
      };
      recorder.ondataavailable = (e) => { if (e.data?.size) chunks.push(e.data); };
      recorder.onstop = async () => {
        clearInterval(tick); stream.getTracks().forEach((t) => t.stop()); try { audio?.close(); } catch { /* ignore */ }
        meter.style.width = "0"; btn.classList.remove("is-live"); rec = null;
        const total = (performance.now() - t0) / 1000;
        const seconds = analyser && first !== null && last !== null && last > first ? Math.round((last - first) * 10) / 10 : Math.round(total * 10) / 10;
        const spoken = { seconds: Math.max(1, seconds), pauses: analyser ? pauses : null };
        const type = recorder.mimeType || mime || "audio/webm";
        const form = new FormData();
        form.append("file", new Blob(chunks, { type }), "answer." + (type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm"));
        status.textContent = "Turning your answer into text...";
        try {
          const res = await api("/api/transcribe", { method: "POST", form });
          const text = String(res?.text || "").trim();
          if (text.split(/\s+/).length < 3) { status.textContent = "Nothing was heard. Try again, closer to the microphone."; return; }
          status.textContent = "Done."; await onDone(text, spoken);
        } catch (e) { status.textContent = ""; fail(e); }
      };
      recorder.start(); rec = { recorder }; btn.classList.add("is-live");
      status.textContent = "Recording... tap again when you finish. It stops by itself after 5 minutes.";
      tick = setInterval(sample, 100);
    }
    return el("div", { class: "iv-rec" }, el("div", { class: "iv-actions" }, btn), el("div", { class: "iv-meter", "aria-hidden": "true" }, meter), status);
  }

  function spokenCard(sp) {
    const stat = (title, value, ok, sub) => el("div", { class: "iv-stat " + (ok === true ? "is-ok" : ok === false ? "is-no" : "") }, el("span", {}, title), el("b", {}, String(value)), sub ? el("span", { class: "iv-note" }, sub) : null);
    const t = sp.time, f = sp.fillers;
    const stats = [stat("Speaking time", `${Math.round(sp.seconds)} s`, t.ok, `aim for ${t.lo} to ${t.hi} s`),
      stat("Words a minute", sp.wpm ?? "n/a", sp.wpm_band ? sp.wpm_band === "good" : null, `calm: ${sp.wpm_target[0]} to ${sp.wpm_target[1]}`),
      stat("Filler words", f.total, f.ok, Object.entries(f.by_word).map(([k, v]) => `${k} ${v}`).join(", ") || "none heard"),
      sp.pauses ? stat("Long pauses", sp.pauses.long, sp.pauses.ok, `longest ${sp.pauses.longest} s`) : stat("Pauses", "n/a", null, "no timing data")];
    return el("div", { class: "ui-stack" }, el("div", { class: "iv-stats" }, ...stats),
      sp.tips.length ? list(sp.tips) : null,
      el("p", { class: "iv-note" }, "The transcriber can drop \"um\" and \"uh\", so the filler count may be lower than what you said."));
  }

  function feedbackCard(r) {
    const f = r.feedback;
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Feedback"),
      el("p", { class: "iv-summary" }, r.spoken ? r.spoken.summary : f.summary), r.spoken ? spokenCard(r.spoken) : el("p", { class: "iv-note" }, `${f.words} words, about ${f.seconds} seconds spoken`),
      el("ul", { class: "iv-checks" }, ...(r.spoken ? r.spoken.checks : f.checks).map((c) => el("li", { class: c.ok ? "is-ok" : "is-no" },
        el("span", { class: "iv-mark", "aria-hidden": "true" }, c.ok ? "OK" : "FIX"), el("span", {}, c.label + (c.tip ? ". " + c.tip : ""))))),
      f.unverified_numbers?.length ? ui.notice("These numbers are not in your confirmed facts: " + f.unverified_numbers.join(", ") + ". Say them only if they are true.", { tone: "warn" }) : null,
      r.critique ? el("div", { class: "iv-critique" }, el("strong", {}, "AI critique"), el("p", {}, r.critique)) : null,
      r.ai_note ? el("p", { class: "iv-note" }, r.ai_note) : null);
  }

  function historyCard() {
    const rows = data.practice;
    const del = async (id) => { try { await api(`/api/interviews/practice/${id}`, { method: "DELETE" }); data = await api("/api/interviews"); paint(); } catch (e) { fail(e); } };
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("04", "Your saved answers"),
      rows.length ? el("div", { class: "ui-stack" }, ...rows.map((p) => el("div", { class: "iv-q" }, el("p", { class: "iv-qtext" }, p.question),
        el("p", { class: "iv-note" }, `${String(p.created).slice(0, 10)} - ${p.feedback.passed} of ${p.feedback.total} checks`),
        el("p", { class: "iv-answer" }, p.answer.length > 260 ? p.answer.slice(0, 260) + "..." : p.answer),
        el("div", { class: "iv-actions" },
          ui.button({ label: "Practise again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => { drill = { question: p.question, kind: p.kind, answer: "", result: null }; paint(); } }),
          ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: () => del(p.id) })))))
        : el("p", { class: "iv-note" }, "Nothing saved yet."),
      rows.length ? ui.button({ label: "Delete all my answers", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
        if (!(await ui.confirm({ title: "Delete all practice answers?", message: "They are removed from this laptop and cannot be brought back.", confirmLabel: "Delete all", danger: true }))) return;
        try { await api("/api/interviews/practice", { method: "DELETE" }); data = await api("/api/interviews"); paint(); } catch (e) { fail(e); }
      } }) : null);
  }

  // ---- (c) stories ----
  function storySheet(s) {
    const cur = s || { title: "", situation: "", task: "", action: "", result: "", fact_ids: [] };
    const f = { title: input(cur.title, { maxlength: "100" }), situation: area(cur.situation, 2), task: area(cur.task, 2), action: area(cur.action, 3), result: area(cur.result, 2) };
    ui.sheet({ title: s?.id ? "Edit story" : "Save a story", content: el("div", { class: "ui-stack" },
      el("p", { class: "iv-note" }, "S T A R: Situation, Task, Action, Result. Fill the empty parts in your own words, with true numbers."),
      field("Title", f.title), field("Situation: where and when", f.situation), field("Task: what you had to do", f.task), field("Action: what YOU did", f.action), field("Result: how it ended", f.result)),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const json = { title: f.title.value, situation: f.situation.value, task: f.task.value, action: f.action.value, result: f.result.value, fact_ids: cur.fact_ids };
      try { await api(s?.id ? `/api/interviews/stories/${s.id}` : "/api/interviews/stories", { method: s?.id ? "PUT" : "POST", json }); ui.toast("Saved.", { kind: "good" }); await load(); return true; }
      catch (e) { fail(e); return false; }
    } }] });
  }

  function storiesTab() {
    const mine = data.stories.map((s) => el("div", { class: "iv-q" }, el("p", { class: "iv-qtext" }, s.title),
      ...[["S", s.situation], ["T", s.task], ["A", s.action], ["R", s.result]].map(([k, v]) => el("p", { class: "iv-star" }, el("b", {}, k + " "), v || "(empty: add it)")),
      s.unverified_numbers?.length ? ui.notice("Not in your facts: " + s.unverified_numbers.join(", ") + ". Keep only what is true.", { tone: "warn" }) : null,
      el("div", { class: "iv-actions" }, ui.button({ label: "Edit", icon: "edit", variant: "tonal", size: "sm", onClick: () => storySheet(s) }),
        ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
          if (await ui.confirm({ title: "Delete this story?", message: s.title, confirmLabel: "Delete", danger: true })) { try { await api(`/api/interviews/stories/${s.id}`, { method: "DELETE" }); load(); } catch (e) { fail(e); } }
        } }))));
    const sug = data.suggestions.map((s) => el("div", { class: "iv-q" }, el("p", { class: "iv-qtext" }, s.title), el("p", { class: "iv-note" }, [s.situation, s.metrics.length ? "numbers in your fact: " + s.metrics.join(", ") : ""].filter(Boolean).join(" - ")),
      ui.button({ label: "Save and fill in", icon: "plus", variant: "tonal", size: "sm", onClick: () => storySheet({ ...s, id: null }) })));
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "My STAR stories"), mine.length ? el("div", { class: "ui-stack" }, ...mine) : el("p", { class: "iv-note" }, "No stories yet."),
        ui.button({ label: "Add my own", icon: "plus", variant: "tonal", size: "sm", onClick: () => storySheet(null) })),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Starters from your facts"),
        data.has_facts ? el("p", { class: "iv-note" }, "Each starter is one of your own achievements. The task and the result are left for you to write.") : el("p", { class: "iv-note" }, "No facts yet. Add your resume in Apps > Opportunities > Profile."),
        ...sug));
  }

  // ---- (d) about me ----
  function aboutTab() {
    const make = (s) => ui.button({ label: `${s} seconds`, icon: "clock", variant: about?.seconds === s ? "primary" : "tonal", onClick: async () => {
      try { about = await api("/api/interviews/about-me", { method: "POST", json: { seconds: s, ...(hasJob() ? jobBody() : {}) } }); paint(); } catch (e) { fail(e); }
    } });
    const box = el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Tell me about yourself"),
      el("p", { class: "iv-note" }, "Built only from your confirmed facts. If you picked a job, the skills it asks for come first."),
      el("div", { class: "iv-actions" }, make(60), make(90)));
    if (about) {
      box.append(el("p", { class: "iv-note" }, `${about.words} words, about ${about.est_seconds} seconds spoken`));
      if (about.empty) box.append(ui.notice(about.note, { tone: "warn" }));
      else {
        box.append(el("p", { class: "iv-script" }, about.text), el("p", { class: "iv-note" }, about.note));
        if (about.blocked) box.append(ui.notice(about.blockers.join(" "), { tone: "warn" }));
        box.append(ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", disabled: about.blocked, onClick: () => copy(about.text) }));
      }
    }
    return box;
  }

  // ---- (e) brief ----
  function briefTab() {
    const go = ui.button({ label: "Build the brief", icon: "book-open", variant: "primary", onClick: () => ui.busy(go, async () => {
      if (!hasJob()) { ui.toast("Pick an application or paste a job first.", { kind: "warn" }); return; }
      try { brief = await api("/api/interviews/brief", { method: "POST", json: jobBody() }); ticked.clear(); paint(); } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Before the interview"), go)];
    if (brief) {
      const sec = (title, items, node) => (items?.length || node) ? el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, title), node || list(items)) : null;
      out.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, [brief.title, brief.company].filter(Boolean).join(" at ") || "The role"),
        brief.location ? el("p", { class: "iv-note" }, brief.location) : null, brief.summary ? el("p", {}, brief.summary) : null, el("p", { class: "iv-note" }, brief.note)),
      sec("What you would do", brief.duties), sec("What they ask for", brief.must_haves), sec("Where you match", brief.matched),
      sec("Gaps: no confirmed fact (prepare an honest answer)", brief.gaps), sec("Questions to ask them", brief.ask_them),
      el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, "Checklist"), ...brief.checklist.map((c, i) => {
        const box = el("input", { type: "checkbox", "aria-label": c }); box.checked = ticked.has(i);
        box.addEventListener("change", () => { if (box.checked) ticked.add(i); else ticked.delete(i); });
        return el("label", { class: "iv-check" }, box, el("span", {}, c));
      })));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out.filter(Boolean));
  }

  // ---- (e2) interview day pack ----
  function dayTab() {
    const f = { when: input(packForm.when, { placeholder: "Tue 24 Nov, 3 pm IST" }), place: input(packForm.place, { placeholder: "Meeting link or address", maxlength: "300" }),
      who: input(packForm.who, { placeholder: "Dana Lee (manager), Ravi Shah" }), round: input(packForm.round, { placeholder: "Technical round" }) };
    const fetchBrief = ui.switchControl({ checked: packForm.fetch, label: "Read the company's public pages now" }), save = ui.switchControl({ checked: packForm.save, label: "Keep this interview day" });
    const go = ui.button({ label: "Build the pack", icon: "clock", variant: "primary", onClick: () => ui.busy(go, async () => {
      if (!hasJob()) { ui.toast("Pick an application or paste a job first.", { kind: "warn" }); return; }
      packForm = { when: f.when.value, place: f.place.value, who: f.who.value, round: f.round.value, fetch: fetchBrief.input.checked, save: save.input.checked };
      try {
        pack = await api("/api/interviews/day-pack", { method: "POST", json: { ...jobBody(), when: packForm.when, place: packForm.place, who: packForm.who, round: packForm.round, fetch_brief: packForm.fetch, save: packForm.save } });
        if (packForm.save) data = await api("/api/interviews");
        paint();
      } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "Interview day"), el("p", { class: "iv-note" }, "Everything for the day on one page. Friday can read it out: ask for \"my interview pack\"."),
      field("When", f.when), field("Link or place", f.place), field("Who you meet", f.who, "Separate names with commas."), field("Round", f.round),
      el("div", { class: "ui-row iv-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Company brief"),
        el("span", { class: "ui-row__sub" }, "Uses the saved brief if you have one. Reading now makes a few polite requests to the company's own public pages.")), fetchBrief),
      el("div", { class: "ui-row iv-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Keep this interview day")), save), go)];
    if (pack) {
      const sec = (title, items, node) => (items?.length || node) ? el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, title), node || list(items)) : null;
      const d = pack.day, cb = pack.company_brief;
      out.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, [pack.role.title, pack.role.company].filter(Boolean).join(" at ") || "The role"),
        el("p", { class: "iv-note" }, [d.when, d.place, d.round, d.who?.length ? "with " + d.who.join(", ") : ""].filter(Boolean).join(" - ") || "No time or place given."),
        pack.role.summary ? el("p", {}, pack.role.summary) : null,
        el("div", { class: "iv-actions" }, ui.button({ label: "Copy as text", icon: "copy", variant: "tonal", size: "sm", onClick: () => copy(pack.read_aloud) }),
          window.speechSynthesis ? ui.button({ label: "Read it out", icon: "volume", variant: "tonal", size: "sm", onClick: () => { try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(pack.read_aloud)); } catch { /* no voice */ } } }) : null,
          window.speechSynthesis ? ui.button({ label: "Stop", icon: "stop", variant: "ghost", size: "sm", onClick: () => { try { speechSynthesis.cancel(); } catch { /* ignore */ } } }) : null)),
      sec("About the company", cb.available ? [cb.summary, ...cb.red_flags.map((x) => "Watch for: " + x), ...cb.news.map((x) => "News: " + x)] : [cb.hint]),
      sec("Likely questions", pack.questions.map((q) => `[${q.kind}] ${q.text}`)),
      sec("Your three best stories", null, el("div", { class: "ui-stack" }, ...pack.stories.map((s) => el("div", { class: "iv-q" }, el("p", { class: "iv-qtext" }, s.title + (s.starter ? " (starter)" : "")),
        ...[["S", s.situation], ["T", s.task], ["A", s.action], ["R", s.result]].map(([k, v]) => el("p", { class: "iv-star" }, el("b", {}, k + " "), v || "(write this part before the day)")))))),
      sec("Questions to ask them", pack.ask_them), sec("Gaps to prepare an honest answer for", pack.role.gaps),
      pack.earlier?.length ? sec("From your earlier interviews here", pack.earlier.map((e) => [e.date, e.round, e.to_improve ? "improve: " + e.to_improve : "", ...(e.follow_ups || []).map((x) => "follow up: " + x)].filter(Boolean).join(" - "))) : null,
      el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, "Checklist"), ...pack.checklist.map((c, i) => {
        const box = el("input", { type: "checkbox", "aria-label": c }); box.checked = ticked.has("d" + i);
        box.addEventListener("change", () => { if (box.checked) ticked.add("d" + i); else ticked.delete("d" + i); });
        return el("label", { class: "iv-check" }, box, el("span", {}, c));
      })));
    }
    if (data.days?.length) {
      const del = async (id) => { try { await api(`/api/interviews/days/${id}`, { method: "DELETE" }); data = await api("/api/interviews"); paint(); } catch (e) { fail(e); } };
      out.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, "Kept interview days"), ...data.days.map((d) => el("div", { class: "iv-q" },
        el("p", { class: "iv-qtext" }, [d.role, d.company].filter(Boolean).join(" at ") || "Interview"), el("p", { class: "iv-note" }, [d.when, d.round].filter(Boolean).join(" - ")),
        el("div", { class: "iv-actions" }, ui.button({ label: "Debrief", icon: "edit", variant: "tonal", size: "sm", onClick: () => { debrief = { ...debrief, company: d.company, role: d.role, round: d.round || "", day_id: d.id }; tab = "debrief"; paint(); } }),
          ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: () => del(d.id) }))))));
    }
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out.filter(Boolean));
  }

  // ---- (e3) debrief after the interview ----
  function debriefTab() {
    const company = input(debrief.company || job.company, { placeholder: "Company", maxlength: "120" }), role = input(debrief.role || job.title, { placeholder: "Role", maxlength: "200" });
    const round = input(debrief.round, { placeholder: "Round 1", maxlength: "60" }), who = input(debrief.interviewer, { placeholder: "Interviewer's name", maxlength: "80" });
    const date = el("input", { class: "ui-input", type: "date", "aria-label": "Interview date" }); date.value = debrief.date;
    const rating = el("select", { class: "ui-input" }, el("option", { value: "" }, "How did it go? (optional)"), ...[[5, "5 - very well"], [4, "4 - well"], [3, "3 - okay"], [2, "2 - not well"], [1, "1 - badly"]].map(([v, l]) => el("option", { value: String(v) }, l)));
    rating.value = debrief.rating;
    const asked = area(debrief.asked, 5, { placeholder: "One question per line" }), well = area(debrief.went_well, 2), improve = area(debrief.to_improve, 2), follow = area(debrief.follow, 2, { placeholder: "One per line: send the code sample..." });
    const used = data.stories.map((s) => { const box = el("input", { type: "checkbox", "aria-label": "I told: " + s.title }); box.checked = debrief.used.includes(s.id); box.addEventListener("change", () => { debrief.used = box.checked ? [...debrief.used, s.id] : debrief.used.filter((x) => x !== s.id); }); return el("label", { class: "iv-check" }, box, el("span", {}, s.title)); });
    const st = { title: input(debrief.story.title, { maxlength: "100", placeholder: "Title (optional)" }), situation: area(debrief.story.situation, 2), task: area(debrief.story.task, 2), action: area(debrief.story.action, 3), result: area(debrief.story.result, 2) };
    const lines = (t) => t.value.split("\n").map((x) => x.trim()).filter(Boolean);
    const go = ui.button({ label: "Save the debrief", icon: "check", variant: "primary", onClick: () => ui.busy(go, async () => {
      const story = Object.fromEntries(Object.entries(st).map(([k, n]) => [k, n.value.trim()]));
      const json = { company: company.value, role: role.value, round: round.value, interviewer: who.value, date: date.value, rating: rating.value ? Number(rating.value) : null,
        asked: lines(asked), went_well: well.value, to_improve: improve.value, follow_ups: lines(follow), stories_used: debrief.used, day_id: debrief.day_id || "",
        new_stories: story.title || story.action ? [story] : [] };
      try {
        lastDebrief = await api("/api/interviews/debriefs", { method: "POST", json });
        debrief = { ...debrief, asked: "", went_well: "", to_improve: "", follow: "", used: [], rating: "", day_id: "", story: { title: "", situation: "", task: "", action: "", result: "" } };
        note = null; data = await api("/api/interviews"); ui.toast("Saved on this laptop.", { kind: "good" }); paint();
      } catch (e) { fail(e); }
    }) });
    const out = [el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "After the interview"),
      el("p", { class: "iv-note" }, "Write it down while you remember. The questions are added to this company's question list. Stories you told, or write below, join your STAR bank."),
      field("Company", company), field("Role", role), field("Round", round), field("Interviewer", who), field("Date", date), field("Overall", rating),
      field("Questions you were asked", asked), field("What went well", well), field("What to improve", improve), field("Follow-ups", follow),
      used.length ? el("div", { class: "ui-stack" }, el("span", { class: "ui-label" }, "Stories you told"), ...used) : null,
      el("details", {}, el("summary", {}, "Write down a new story from this interview"), el("div", { class: "ui-stack" }, field("Title", st.title), field("Situation", st.situation), field("Task", st.task), field("Action: what YOU did", st.action), field("Result", st.result))), go)];
    if (lastDebrief) out.push(ui.notice(`Saved: ${lastDebrief.questions_saved} question(s) added to ${lastDebrief.company}'s list, ${lastDebrief.stories_added} story added to your bank.`, { tone: "good" }));
    const thanks = async (d) => {
      try { note = { for: d.id, draft: await api(`/api/interviews/debriefs/${d.id}/thank-you`, { method: "POST", json: { ai } }) }; paint(); } catch (e) { fail(e); }
    };
    const rows = data.debriefs.map((d) => el("div", { class: "iv-q" }, el("p", { class: "iv-qtext" }, [d.role, d.company].filter(Boolean).join(" at ") + (d.round ? ` (${d.round})` : "")),
      el("p", { class: "iv-note" }, [d.date, d.rating ? `went ${d.rating} of 5` : "", `${d.asked.length} question(s)`].filter(Boolean).join(" - ")),
      d.to_improve ? el("p", { class: "iv-answer" }, "Improve: " + d.to_improve) : null, ...(d.follow_ups || []).map((x) => el("p", { class: "iv-answer" }, "Follow up: " + x)),
      el("div", { class: "iv-actions" }, ui.button({ label: "Draft a thank-you note", icon: "pen", variant: "tonal", size: "sm", onClick: () => thanks(d) }),
        ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
          if (await ui.confirm({ title: "Delete this debrief?", message: "The questions it added are taken back from the list. Stories stay.", confirmLabel: "Delete", danger: true })) { try { await api(`/api/interviews/debriefs/${d.id}`, { method: "DELETE" }); data = await api("/api/interviews"); paint(); } catch (e) { fail(e); } }
        } })),
      note?.for === d.id ? el("div", { class: "ui-stack" }, el("p", { class: "iv-script" }, note.draft.text),
        note.draft.blocked ? ui.notice((note.draft.blockers || []).join(" "), { tone: "warn" }) : null,
        el("p", { class: "iv-note" }, "Draft only. Copy it and send it yourself. It is also in Networking."),
        ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", disabled: !!note.draft.blocked, onClick: () => copy(note.draft.text) })) : null));
    out.push(el("section", { class: "ui-card ui-stack" }, el("h3", { class: "iv-h" }, "Your debriefs"), rows.length ? el("div", { class: "ui-stack" }, ...rows) : el("p", { class: "iv-note" }, "None yet.")));
    return el("div", { class: "ui-stack ui-stack--lg" }, ...out);
  }

  // ---- (f) negotiation ----
  function payTab() {
    const n = data.negotiation;
    const cur = input(n.currency, { maxlength: "8", placeholder: "EUR" });
    const num = (v, label) => { const i = el("input", { class: "ui-input", type: "number", min: "0", inputmode: "decimal", step: "any", "aria-label": label }); i.value = v ?? ""; return i; };
    const low = num(n.low, "Lowest"), target = num(n.target, "Target"), high = num(n.high, "Top");
    const notice = input(n.notice, { maxlength: "60", placeholder: "Example: one month" });
    const save = ui.button({ label: "Save my range", icon: "check", variant: "primary", onClick: () => ui.busy(save, async () => {
      try { data.negotiation = await api("/api/interviews/negotiation", { method: "PUT", json: { currency: cur.value, low: low.value, target: target.value, high: high.value, notice: notice.value } }); ui.toast("Saved on this laptop.", { kind: "good" }); paint(); }
      catch (e) { fail(e); }
    }) });
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "My range"), el("p", { class: "iv-note" }, "Type your own numbers. REDLINE does not know market pay and will not guess."),
        field("Currency", cur), el("div", { class: "iv-pay" }, field("Lowest", low), field("Target", target), field("Top", high)), field("Notice period", notice), save),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Things to say"), ...n.scripts.map((t) => el("div", { class: "iv-q" }, el("p", { class: "iv-script" }, t),
        t.includes("[FILL") ? null : ui.button({ label: "Copy", icon: "copy", variant: "ghost", size: "sm", onClick: () => copy(t) })))),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("04", "Tips"), list(n.tips), el("p", { class: "iv-note" }, n.note)));
  }

  load();
}
