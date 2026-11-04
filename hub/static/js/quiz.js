// Learn > Quiz: make a quiz from a source, answer one question at a time, see the result and history.
// Questions, answers, explanations and sources come from models, files and the web, so they are
// only ever put on screen with textContent (via el()).
import { api, el } from "./api.js";
import { createSourcePicker, createProviderSwitch, icon, store } from "./sourcepicker.js";

const PREFS_KEY = "hub.quiz.prefs";
const LEVELS = [["easy", "Easy"], ["medium", "Medium"], ["hard", "Hard"]];
const LETTERS = "ABCDEFGHIJ";

function clip(text, n) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n - 1) + "…" : flat;
}
function prettyDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  if (!m) return String(iso || "");
  return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
function pct(score) { return `${Math.round(score * 100)}%`; }

export function init(container, ctx) {
  const root = el("div", { class: "qz-root" });
  container.append(root);
  const stage = el("div", { class: "qz-stage" });
  root.append(stage);

  // -------------------------------------------------------------- state
  let prefs = { count: 5, mcq: true, short: true, difficulty: "medium" };
  try {
    const saved = JSON.parse(store.get("localStorage", PREFS_KEY, "{}"));
    if (saved && typeof saved === "object") {
      if (Number.isInteger(saved.count) && saved.count >= 3 && saved.count <= 15) prefs.count = saved.count;
      if (typeof saved.mcq === "boolean") prefs.mcq = saved.mcq;
      if (typeof saved.short === "boolean") prefs.short = saved.short;
      if (LEVELS.some(([id]) => id === saved.difficulty)) prefs.difficulty = saved.difficulty;
      if (!prefs.mcq && !prefs.short) prefs.mcq = true;
    }
  } catch (_) { /* defaults */ }
  const savePrefs = () => store.set("localStorage", PREFS_KEY, JSON.stringify(prefs));

  let screen = "setup";     // setup | generating | question | result
  let quiz = null;          // {quiz_id, title, questions}
  let idx = 0;
  let answers = [];         // per question: {given, result}
  let selected = null;      // chosen choice index for the current mcq
  let draft = "";           // typed short answer for the current question
  let phase = "answering";  // answering | checking | feedback
  let qError = null;        // {message, status} from the last failed answer/finish
  let finishing = false;
  let result = null;
  let setupError = "";
  let lastSettings = null;
  let genController = null;
  let genTimer = null;
  let confirmQuit = false;

  // The setup form is built once so the picker keeps its state between screens.
  const setupRoot = el("div", { class: "qz-setup" });
  const pickerHost = el("div", { class: "qz-picker" });
  const picker = createSourcePicker(pickerHost, ctx, { required: true, allowNone: false });

  const countValue = el("output", { class: "qz-countval", for: "qz-count" }, String(prefs.count));
  const countInput = el("input", { id: "qz-count", type: "range", min: "3", max: "15", step: "1", value: String(prefs.count),
    class: "qz-range", "aria-label": "Number of questions" });
  countInput.addEventListener("input", () => {
    prefs.count = Number(countInput.value);
    countValue.textContent = String(prefs.count);
    savePrefs();
  });

  const mcqBox = el("input", { type: "checkbox", id: "qz-mcq" });
  const shortBox = el("input", { type: "checkbox", id: "qz-short" });
  const kindHint = el("div", { class: "muted qz-small", "aria-live": "polite" });
  function paintKinds() {
    mcqBox.checked = prefs.mcq;
    shortBox.checked = prefs.short;
    // Keep at least one on: the only checked box is locked.
    mcqBox.disabled = prefs.mcq && !prefs.short;
    shortBox.disabled = prefs.short && !prefs.mcq;
    kindHint.textContent = (mcqBox.disabled || shortBox.disabled) ? "At least one type stays on." : "";
  }
  mcqBox.addEventListener("change", () => { prefs.mcq = mcqBox.checked; if (!prefs.mcq && !prefs.short) prefs.short = true; paintKinds(); savePrefs(); });
  shortBox.addEventListener("change", () => { prefs.short = shortBox.checked; if (!prefs.mcq && !prefs.short) prefs.mcq = true; paintKinds(); savePrefs(); });

  const levelBar = el("div", { class: "qz-chips", role: "radiogroup", "aria-label": "Difficulty" });
  const levelButtons = {};
  for (const [id, text] of LEVELS) {
    const button = el("button", { type: "button", class: "qz-chip", role: "radio", onclick: () => { prefs.difficulty = id; paintLevels(); savePrefs(); } }, text);
    levelButtons[id] = button;
    levelBar.append(button);
  }
  function paintLevels() {
    for (const [id, b] of Object.entries(levelButtons)) b.setAttribute("aria-checked", String(id === prefs.difficulty));
  }

  const providerHost = el("div", { class: "qz-provider" });
  const provider = createProviderSwitch(providerHost, "Model for the quiz");

  const makeButton = el("button", { type: "button", class: "btn primary qz-make", disabled: true, onclick: () => generate(currentSettings()) }, "Make my quiz");
  const makeHint = el("div", { class: "muted qz-small qz-makehint", "aria-live": "polite" });
  const errorBox = el("div", { class: "qz-error", role: "alert", hidden: true });

  function field(labelText, ...children) {
    return el("div", { class: "qz-field" }, el("div", { class: "qz-label" }, labelText), ...children);
  }
  const setupCard = el("section", { class: "card qz-card", "aria-labelledby": "qz-setup-title" },
    el("h2", { id: "qz-setup-title" }, "Make a quiz"),
    field("Study from", pickerHost),
    field("Questions", el("div", { class: "qz-countrow" }, countInput, countValue)),
    el("fieldset", { class: "qz-field qz-kinds" }, el("legend", { class: "qz-label" }, "Question types"),
      el("label", { class: "qz-check", for: "qz-mcq" }, mcqBox, el("span", {}, "Multiple choice")),
      el("label", { class: "qz-check", for: "qz-short" }, shortBox, el("span", {}, "Short answer")),
      kindHint),
    field("Difficulty", levelBar),
    field("Model", providerHost),
    el("div", { class: "qz-makebar" }, errorBox, makeButton, makeHint));
  const historyBox = el("section", { class: "qz-history", "aria-labelledby": "qz-history-title" });
  setupRoot.append(setupCard, historyBox);

  function currentSettings() {
    const kinds = [prefs.mcq ? "mcq" : null, prefs.short ? "short" : null].filter(Boolean);
    return { source: picker.getSource(), label: picker.label(), count: prefs.count, kinds, difficulty: prefs.difficulty };
  }
  function paintMake() {
    const has = !!picker.getSource();
    makeButton.disabled = !has;
    makeHint.textContent = has ? "" : "Choose a source above to make a quiz.";
  }
  picker.onChange(() => { setupError = ""; paintError(); paintMake(); });
  function paintError() {
    errorBox.hidden = !setupError;
    errorBox.replaceChildren(...(setupError ? [icon("alert"), el("span", {}, setupError)] : []));
  }

  // -------------------------------------------------------------- screens
  function showScreen(name) {
    screen = name;
    stopGenTimer();
    if (name === "setup") {
      stage.replaceChildren(setupRoot);
      paintKinds(); paintLevels(); paintMake(); paintError();
      loadHistory();
    } else if (name === "generating") {
      stage.replaceChildren(renderGenerating());
    } else if (name === "question") {
      renderQuestion();
    } else if (name === "result") {
      stage.replaceChildren(renderResult());
      stage.querySelector(".qz-result-title")?.focus({ preventScroll: false });
    }
    container.closest(".view")?.scrollTo?.({ top: 0 });
  }

  // ---- generating
  function stopGenTimer() { if (genTimer) { clearInterval(genTimer); genTimer = null; } }
  function renderGenerating() {
    const elapsed = el("span", {}, "0 s");
    const started = Date.now();
    genTimer = setInterval(() => { elapsed.textContent = `${Math.round((Date.now() - started) / 1000)} s`; }, 1000);
    return el("section", { class: "card qz-card qz-loading", "aria-busy": "true" },
      el("div", { class: "qz-spinner", "aria-hidden": "true" }),
      el("h2", { role: "status" }, "Making your quiz…"),
      el("p", { class: "muted" }, lastSettings ? `From ${lastSettings.label || "your source"}.` : ""),
      el("p", { class: "muted" }, "A local model can take a minute or two. Keep this screen open."),
      el("p", { class: "muted qz-small" }, "Waiting: ", elapsed),
      el("button", { type: "button", class: "btn qz-cancel", onclick: () => { genController?.abort(); } }, "Cancel"));
  }

  async function generate(settings) {
    if (!settings.source || screen === "generating") return;
    lastSettings = settings;
    setupError = "";
    genController = new AbortController();
    const mine = genController;
    showScreen("generating");
    try {
      const data = await api("/api/learn/quiz", {
        method: "POST", signal: mine.signal,
        json: { source: settings.source, count: settings.count, kinds: settings.kinds, difficulty: settings.difficulty, provider: provider.get() },
      });
      if (!data || !Array.isArray(data.questions) || !data.questions.length) throw new Error("The laptop sent a quiz with no questions. Try again.");
      quiz = data;
      idx = 0; answers = []; selected = null; draft = ""; phase = "answering"; qError = null; result = null; confirmQuit = false;
      showScreen("question");
    } catch (error) {
      if (error.name === "AbortError") { setupError = ""; }
      else { setupError = error.message || "Couldn't make the quiz."; }
      showScreen("setup");
      if (setupError) errorBox.scrollIntoView({ block: "center" });
    } finally {
      if (genController === mine) genController = null;
    }
  }

  // ---- one question
  function current() { return quiz.questions[idx]; }

  function renderQuestion() {
    const q = current();
    const total = quiz.questions.length;
    const done = answers[idx];
    const checking = phase === "checking";
    const shownCount = phase === "feedback" ? idx + 1 : idx;

    const quitRow = confirmQuit
      ? el("div", { class: "qz-quit-confirm", role: "group", "aria-label": "Leave this quiz?" },
        el("span", {}, "Leave this quiz? Your answers so far will be lost."),
        el("div", { class: "row wrap" },
          el("button", { type: "button", class: "btn", onclick: () => { confirmQuit = false; renderQuestion(); } }, "Keep going"),
          el("button", { type: "button", class: "btn danger", onclick: () => { confirmQuit = false; quiz = null; showScreen("setup"); } }, "Leave quiz")))
      : null;

    const head = el("div", { class: "qz-progress-head" },
      el("div", { class: "qz-progress-text", "aria-live": "polite" }, `Question ${idx + 1} of ${total}`),
      el("button", { type: "button", class: "qz-quit", "aria-label": "Quit quiz", onclick: () => { confirmQuit = true; renderQuestion(); } }, icon("x"), "Quit"));
    const bar = el("div", { class: "qz-bar", role: "progressbar", "aria-label": "Quiz progress", "aria-valuemin": "0",
      "aria-valuemax": String(total), "aria-valuenow": String(shownCount), "aria-valuetext": `${shownCount} of ${total} answered` },
      el("div", { class: "qz-bar-fill", style: `width:${(shownCount / total) * 100}%` }));

    const questionHead = el("h2", { class: "qz-question", tabindex: "-1" }, q.question);
    const children = [q.topic ? el("div", { class: "qz-topic" }, q.topic) : null, questionHead];

    if (q.kind === "mcq") {
      const group = el("div", { class: "qz-options", role: "radiogroup", "aria-label": "Answer choices" });
      (q.choices || []).forEach((choice, i) => {
        const isSel = selected === i;
        let mark = null;
        let cls = "qz-option";
        if (phase === "feedback" && done) {
          const correctIdx = q.choices.indexOf(done.result.correct_answer);
          if (i === correctIdx) { cls += " right"; mark = ["check", "Correct answer"]; }
          if (i === done.choice && i !== correctIdx) { cls += " wrong"; mark = ["x", "Your answer"]; }
          if (i === done.choice && i === correctIdx) mark = ["check", "Your answer, correct"];
        }
        group.append(el("button", { type: "button", class: cls, role: "radio", "aria-checked": String(isSel),
          disabled: checking || phase === "feedback", "data-choice": String(i),
          onclick: () => { selected = i; qError = null; renderQuestion(); focusOption(i); } },
          el("span", { class: "qz-letter", "aria-hidden": "true" }, LETTERS[i] || String(i + 1)),
          el("span", { class: "qz-option-text" }, choice),
          mark ? el("span", { class: "qz-option-mark" }, icon(mark[0]), el("span", {}, mark[1])) : null));
      });
      children.push(group);
    } else {
      const area = el("textarea", { class: "qz-answer", rows: "3", placeholder: "Type your answer", "aria-label": "Your answer",
        maxlength: "2000", disabled: checking || phase === "feedback" });
      area.value = draft;
      area.addEventListener("input", () => { draft = area.value; qError = null; syncCheck(); });
      children.push(area);
    }

    // Check button, error and retry
    let checkBtn = null;
    const canCheck = q.kind === "mcq" ? selected !== null : draft.trim().length > 0;
    function syncCheck() { if (checkBtn) checkBtn.disabled = !(q.kind === "mcq" ? selected !== null : draft.trim().length > 0); }
    if (phase !== "feedback") {
      if (qError) {
        children.push(el("div", { class: "qz-error", role: "alert" }, icon("alert"),
          el("span", {}, qError.message)));
      }
      if (qError && qError.status === 404) {
        children.push(el("button", { type: "button", class: "btn primary qz-big", onclick: () => { quiz = null; showScreen("setup"); } }, "Back to setup"));
      } else {
        checkBtn = el("button", { type: "button", class: "btn primary qz-big", id: "qz-check",
          disabled: checking || !canCheck, "aria-busy": String(checking), onclick: () => submitAnswer() },
        checking ? "Checking…" : qError ? "Retry" : "Check answer");
        children.push(checkBtn);
        if (checking && q.kind === "short") children.push(el("p", { class: "muted qz-small" }, "The model is marking your answer. This can take a moment."));
      }
    }

    const card = el("section", { class: "card qz-card" }, ...children);
    stage.replaceChildren(...[quitRow, head, bar, card].filter(Boolean));
    if (phase === "feedback" && done) stage.append(renderFeedback(q, done));
    stage.querySelector(".qz-quit-confirm .btn")?.focus();
  }

  function focusOption(i) {
    stage.querySelector(`[data-choice="${i}"]`)?.focus({ preventScroll: true });
  }

  async function submitAnswer() {
    const q = current();
    if (phase === "checking") return;
    const given = q.kind === "mcq" ? selected : draft.trim();
    if (given === null || given === "") return;
    phase = "checking";
    qError = null;
    renderQuestion();
    try {
      const data = await api(`/api/learn/quiz/${encodeURIComponent(quiz.quiz_id)}/answer`, {
        method: "POST", json: { question_id: q.id, answer: given, provider: provider.get() },
      });
      answers[idx] = { choice: q.kind === "mcq" ? given : null, text: q.kind === "mcq" ? q.choices[given] : given, result: data };
      phase = "feedback";
      renderQuestion();
      const fb = stage.querySelector(".qz-feedback-title");
      fb?.focus({ preventScroll: true });
      fb?.scrollIntoView({ block: "nearest" });
    } catch (error) {
      phase = "answering";
      qError = { message: error.message || "That didn't work.", status: error.status };
      renderQuestion();
      stage.querySelector("#qz-check")?.focus({ preventScroll: true });
    }
  }

  function verdict(r) {
    const score = typeof r.score === "number" ? r.score : (r.correct ? 1 : 0);
    if (r.correct && score >= 1) return { cls: "good", ico: "check", text: "Correct" };
    if (r.correct) return { cls: "good", ico: "check", text: `Mostly right (${pct(score)})` };
    if (score > 0) return { cls: "warn", ico: "half", text: `Partly right (${pct(score)})` };
    return { cls: "bad", ico: "x", text: "Not quite" };
  }

  function renderFeedback(q, done) {
    const r = done.result;
    const v = verdict(r);
    const last = idx === quiz.questions.length - 1;
    const rows = [];
    if (q.kind === "short") rows.push(el("p", { class: "qz-fb-row" }, el("strong", {}, "Your answer: "), done.text));
    rows.push(el("p", { class: "qz-fb-row" }, el("strong", {}, "Correct answer: "), r.correct_answer));
    if (r.explanation) rows.push(el("p", { class: "qz-fb-row" }, el("strong", {}, "Why: "), r.explanation));
    if (r.feedback) rows.push(el("p", { class: "qz-fb-row" }, el("strong", {}, "Feedback: "), r.feedback));
    const nextButton = el("button", { type: "button", class: "btn primary qz-big", disabled: finishing, "aria-busy": String(finishing),
      onclick: () => (last ? finishQuiz() : nextQuestion()) }, finishing ? "Finishing…" : (last && qError ? "Retry" : last ? "Finish" : "Next question"));
    return el("section", { class: `card qz-card qz-feedback ${v.cls}`, "aria-live": "polite" },
      el("h3", { class: "qz-feedback-title", tabindex: "-1" }, icon(v.ico), el("span", {}, v.text)),
      ...rows,
      last && qError ? el("div", { class: "qz-error", role: "alert" }, icon("alert"), el("span", {}, qError.message)) : null,
      nextButton);
  }

  function nextQuestion() {
    idx += 1;
    selected = null; draft = ""; phase = "answering"; qError = null;
    renderQuestion();
    stage.querySelector(".qz-question")?.focus({ preventScroll: true });
    container.closest(".view")?.scrollTo?.({ top: 0 });
  }

  async function finishQuiz() {
    if (finishing) return;
    finishing = true;
    qError = null;
    renderQuestion();
    try {
      result = await api(`/api/learn/quiz/${encodeURIComponent(quiz.quiz_id)}/finish`, { method: "POST" });
      finishing = false;
      showScreen("result");
    } catch (error) {
      finishing = false;
      qError = { message: error.message || "Couldn't finish the quiz.", status: error.status };
      renderQuestion();
    }
  }

  // ---- result
  function renderResult() {
    const r = result;
    const percent = Math.max(0, Math.min(100, Number(r.percent) || 0));
    const tone = percent >= 80 ? "Great work" : percent >= 50 ? "Good effort" : "Keep practising";
    const parts = [
      el("div", { class: "qz-score" },
        el("div", { class: "qz-score-main", "aria-label": `Score ${r.score} out of ${r.total}` }, `${r.score} / ${r.total}`),
        el("div", { class: "qz-score-pct" }, `${percent}%`)),
      el("div", { class: "qz-bar", role: "progressbar", "aria-label": "Score", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(percent) },
        el("div", { class: "qz-bar-fill", style: `width:${percent}%` })),
    ];
    const weak = r.weak_topics || [];
    if (weak.length) {
      parts.push(el("div", { class: "qz-field" }, el("div", { class: "qz-label" }, "Topics to revisit"),
        el("div", { class: "qz-chips" }, weak.map((t) => el("span", { class: "qz-tag" }, t)))));
    }
    const missed = r.missed || [];
    if (missed.length) {
      parts.push(el("div", { class: "qz-field" }, el("div", { class: "qz-label" }, `Missed questions (${missed.length})`),
        el("div", { class: "qz-missed" }, missed.map((m) =>
          el("details", { class: "qz-miss" },
            el("summary", {}, el("span", { class: "qz-miss-q" }, clip(m.question, 110))),
            el("div", { class: "qz-miss-body" },
              clip(m.question, 110) !== String(m.question).replace(/\s+/g, " ").trim()
                ? el("p", {}, el("strong", {}, "Question: "), m.question) : null,
              el("p", {}, el("strong", {}, "Your answer: "), m.your_answer),
              el("p", {}, el("strong", {}, "Correct answer: "), m.correct_answer),
              m.explanation ? el("p", {}, el("strong", {}, "Why: "), m.explanation) : null,
              m.topic ? el("p", { class: "muted qz-small" }, `Topic: ${m.topic}`) : null))))));
    } else {
      parts.push(el("p", { class: "qz-perfect" }, icon("check"), "You got every question right."));
    }
    return el("section", { class: "card qz-card qz-result" },
      el("h2", { class: "qz-result-title", tabindex: "-1" }, `${tone}: ${r.title || "Quiz finished"}`),
      ...parts,
      el("div", { class: "qz-actions" },
        el("button", { type: "button", class: "btn qz-big", onclick: () => { quiz = null; showScreen("setup"); } }, "New quiz"),
        el("button", { type: "button", class: "btn primary qz-big", disabled: !lastSettings,
          onclick: () => lastSettings && generate({ ...lastSettings }) }, "Same source again")));
  }

  // -------------------------------------------------------------- history
  let historySeq = 0;
  let showAllHistory = false;
  async function loadHistory() {
    const mine = ++historySeq;
    historyBox.replaceChildren(el("h2", { id: "qz-history-title" }, "History"), el("p", { class: "muted" }, "Loading history…"));
    try {
      const data = await api("/api/learn/history");
      if (mine !== historySeq) return;
      paintHistory(data);
    } catch (error) {
      if (mine !== historySeq) return;
      historyBox.replaceChildren(el("h2", { id: "qz-history-title" }, "History"),
        el("div", { class: "qz-error", role: "alert" }, icon("alert"), el("span", {}, error.message)),
        el("button", { type: "button", class: "btn", onclick: () => loadHistory() }, "Retry"));
    }
  }
  function paintHistory(data) {
    const quizzes = data.quizzes || [];
    const days = data.streak_days || 0;
    const streak = el("div", { class: `qz-streak${days ? " on" : ""}` }, icon("flame"),
      el("span", {}, days ? `${days} day streak` : "No streak yet"));
    const head = el("div", { class: "qz-history-head" }, el("h2", { id: "qz-history-title" }, "History"), streak);
    if (!quizzes.length) {
      historyBox.replaceChildren(head, el("p", { class: "muted qz-empty" }, "No finished quizzes yet. Your results will show up here."));
      return;
    }
    const visible = showAllHistory ? quizzes : quizzes.slice(0, 8);
    const items = visible.map((e) => el("li", { class: "qz-hist-item" },
      el("div", { class: "qz-hist-main" },
        el("div", { class: "qz-hist-title" }, e.title || "Quiz"),
        el("div", { class: "muted qz-small" }, [prettyDate(e.date), e.source_label ? clip(e.source_label, 40) : ""].filter(Boolean).join(" · "))),
      el("div", { class: `qz-hist-pct ${e.percent >= 80 ? "good" : e.percent >= 50 ? "warn" : "bad"}`, "aria-label": `${e.percent} percent` }, `${e.percent}%`)));
    const more = quizzes.length > 8
      ? el("button", { type: "button", class: "btn qz-more", onclick: () => { showAllHistory = !showAllHistory; paintHistory(data); } },
        showAllHistory ? "Show fewer" : `Show all (${quizzes.length})`)
      : null;
    historyBox.replaceChildren(...[head, el("ul", { class: "qz-hist-list" }, items), more].filter(Boolean));
  }

  showScreen("setup");

  return {
    show() {
      provider.refresh();
      if (screen === "setup") loadHistory();
    },
    hide() {},
  };
}
