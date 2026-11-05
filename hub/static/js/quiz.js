// Learn > Quiz: make a quiz from a source, answer one question at a time, see the result and history.
// Questions, answers, explanations and sources come from models, files and the web, so they are
// only ever put on screen with textContent (via el()).
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { createSourceSheet, createProviderSwitch, ensureCss, store } from "./sourcepicker.js";

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
  ensureCss("/css/learn.css");
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

  // The setup form is built once so the source keeps its state between screens.
  const setupRoot = el("div", { class: "qz-setup ui-stack ui-stack--lg" });
  let pickedSource = null;
  let pickedLabel = "";
  const sources = createSourceSheet(ctx, { title: "Quiz source", applyLabel: "Use this source",
    onApply(next, label) { pickedSource = next; pickedLabel = label; paintSourceRow(); paintMake(); setupError = ""; paintError(); } });
  const sourceTitle = el("span", { class: "ui-row__title" });
  const sourceSub = el("span", { class: "ui-row__sub" });
  const sourceRow = el("button", { type: "button", class: "ui-card ui-card--interactive qz-source", "aria-haspopup": "dialog",
    onclick: () => sources.open() }, ui.tile("book-open", "accent"), el("span", { class: "ui-row__body" }, sourceTitle, sourceSub),
  ui.icon("chevron-right", { class: "ui-row__chev" }));
  function paintSourceRow() {
    sourceTitle.textContent = pickedSource ? pickedLabel : "Choose what to study";
    sourceSub.textContent = pickedSource ? "Tap to change" : "A note, file, web page, pasted text or topic";
    sourceRow.classList.toggle("is-set", !!pickedSource);
  }

  const countValue = el("output", { class: "qz-countval ui-num", for: "qz-count" }, String(prefs.count));
  const countInput = el("input", { id: "qz-count", type: "range", min: "3", max: "15", step: "1", value: String(prefs.count),
    class: "qz-range", "aria-label": "Number of questions" });
  countInput.addEventListener("input", () => {
    prefs.count = Number(countInput.value);
    countValue.textContent = String(prefs.count);
    savePrefs();
  });

  const kindHint = el("div", { class: "qz-small", "aria-live": "polite" });
  const mcqChip = el("button", { type: "button", class: "ui-chip", onclick: () => toggleKind("mcq") }, ui.icon("list-checks"), "Multiple choice");
  const shortChip = el("button", { type: "button", class: "ui-chip", onclick: () => toggleKind("short") }, ui.icon("pen"), "Short answer");
  function toggleKind(k) {
    prefs[k] = !prefs[k];
    if (!prefs.mcq && !prefs.short) { prefs[k === "mcq" ? "short" : "mcq"] = true; }
    paintKinds(); savePrefs();
  }
  function paintKinds() {
    mcqChip.setAttribute("aria-pressed", String(prefs.mcq));
    shortChip.setAttribute("aria-pressed", String(prefs.short));
    mcqChip.classList.toggle("is-on", prefs.mcq);
    shortChip.classList.toggle("is-on", prefs.short);
  }

  const levelSeg = ui.segmented({ options: LEVELS.map(([value, label]) => ({ value, label })), value: prefs.difficulty,
    label: "Difficulty", block: true, onChange(v) { prefs.difficulty = v; savePrefs(); } });
  function paintLevels() { levelSeg.value = prefs.difficulty; }

  const providerHost = el("div", { class: "qz-provider" });
  const provider = createProviderSwitch(providerHost, "Model for the quiz");

  const makeButton = ui.button({ label: "Make my quiz", icon: "sparkles", variant: "primary", size: "lg", block: true, disabled: true,
    onClick: () => generate(currentSettings()) });
  const makeHint = el("div", { class: "qz-small qz-makehint", "aria-live": "polite" });
  const errorHost = el("div", { class: "qz-error", role: "alert", hidden: true });

  const field = (labelText, ...children) => el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, labelText), ...children);
  const setupCard = el("section", { class: "ui-card qz-card ui-stack", "aria-labelledby": "qz-setup-title" },
    el("h2", { id: "qz-setup-title", class: "qz-h" }, "Make a quiz"),
    sourceRow,
    field("Questions", el("div", { class: "qz-countrow" }, countInput, countValue)),
    field("Question types", el("div", { class: "qz-chips" }, mcqChip, shortChip), kindHint),
    field("Difficulty", levelSeg),
    field("Model", providerHost),
    el("div", { class: "qz-makebar ui-stack" }, errorHost, makeButton, makeHint));
  const historyBox = el("section", { class: "qz-history", "aria-labelledby": "qz-history-title" });
  setupRoot.append(setupCard, historyBox);

  function currentSettings() {
    const kinds = [prefs.mcq ? "mcq" : null, prefs.short ? "short" : null].filter(Boolean);
    return { source: pickedSource, label: pickedLabel, count: prefs.count, kinds, difficulty: prefs.difficulty };
  }
  function paintMake() {
    const has = !!pickedSource;
    makeButton.disabled = !has;
    makeHint.textContent = has ? "" : "Choose a source above to make a quiz.";
  }
  function paintError() {
    errorHost.hidden = !setupError;
    errorHost.replaceChildren(...(setupError ? [ui.notice(setupError, { tone: "bad" })] : []));
  }

  // -------------------------------------------------------------- screens
  function showScreen(name) {
    screen = name;
    stopGenTimer();
    if (name === "setup") {
      stage.replaceChildren(setupRoot);
      paintKinds(); paintLevels(); paintSourceRow(); paintMake(); paintError();
      loadHistory();
    } else if (name === "generating") {
      stage.replaceChildren(renderGenerating());
    } else if (name === "question") {
      renderQuestion();
    } else if (name === "result") {
      stage.replaceChildren(renderResult());
      stage.querySelector(".qz-result-title")?.focus({ preventScroll: true });
    }
    container.closest(".view")?.scrollTo?.({ top: 0 });
  }

  // ---- generating
  function stopGenTimer() { if (genTimer) { clearInterval(genTimer); genTimer = null; } }
  function renderGenerating() {
    const elapsed = el("span", { class: "ui-num" }, "0 s");
    const started = Date.now();
    genTimer = setInterval(() => { elapsed.textContent = `${Math.round((Date.now() - started) / 1000)} s`; }, 1000);
    return el("section", { class: "ui-card qz-card qz-loading ui-stack", "aria-busy": "true" },
      el("div", { class: "ui-spinner", "aria-hidden": "true" }),
      el("h2", { role: "status", class: "qz-h" }, "Making your quiz…"),
      el("p", { class: "qz-small" }, lastSettings ? `From ${lastSettings.label || "your source"}.` : ""),
      el("p", { class: "qz-small" }, "A local model can take a minute or two. Keep this screen open."),
      el("p", { class: "qz-small" }, "Waiting: ", elapsed),
      ui.button({ label: "Cancel", variant: "tonal", onClick: () => { genController?.abort(); } }));
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
      idx = 0; answers = []; selected = null; draft = ""; phase = "answering"; qError = null; result = null;
      showScreen("question");
    } catch (error) {
      if (error.name === "AbortError") { setupError = ""; }
      else { setupError = error.message || "Couldn't make the quiz."; }
      showScreen("setup");
      if (setupError) errorHost.scrollIntoView({ block: "center" });
    } finally {
      if (genController === mine) genController = null;
    }
  }

  // ---- one question
  function current() { return quiz.questions[idx]; }

  async function quit() {
    if (await ui.confirm({ title: "Leave this quiz?", message: "Your answers so far will be lost.", confirmLabel: "Leave quiz", cancelLabel: "Keep going", danger: true })) {
      quiz = null;
      showScreen("setup");
    }
  }

  function renderQuestion() {
    const q = current();
    const total = quiz.questions.length;
    const done = answers[idx];
    const checking = phase === "checking";
    const shownCount = phase === "feedback" ? idx + 1 : idx;

    const progress = ui.progressBar(shownCount / total, "Quiz progress");
    progress.setAttribute("aria-valuetext", `${shownCount} of ${total} answered`);
    const head = el("div", { class: "qz-progress-head" },
      el("div", { class: "qz-progress-text ui-num", "aria-live": "polite" }, `Question ${idx + 1} of ${total}`),
      ui.button({ label: "Quit", icon: "close", variant: "ghost", size: "sm", "aria-label": "Quit quiz", onClick: quit }));

    const children = [
      q.topic ? ui.badge(q.topic, "accent") : null,
      el("h2", { class: "qz-question", tabindex: "-1" }, q.question),
    ];

    if (q.kind === "mcq") {
      const group = el("div", { class: "qz-options", role: "radiogroup", "aria-label": "Answer choices" });
      (q.choices || []).forEach((choice, i) => {
        const isSel = selected === i;
        let cls = "ui-btn ui-btn--secondary qz-option";
        let mark = null;
        if (phase === "feedback" && done) {
          const correctIdx = q.choices.indexOf(done.result.correct_answer);
          if (i === correctIdx) { cls += " is-right"; mark = ["check-circle", "Correct answer"]; }
          else if (i === done.choice) { cls += " is-wrong"; mark = ["x", "Your answer"]; }
          else cls += " is-dim";
        }
        group.append(el("button", { type: "button", class: cls, role: "radio", "aria-checked": String(isSel),
          disabled: checking || phase === "feedback", "data-choice": String(i),
          onclick: () => { selected = i; qError = null; ui.haptic("select"); renderQuestion(); focusOption(i); } },
        el("span", { class: "qz-letter", "aria-hidden": "true" }, LETTERS[i] || String(i + 1)),
        el("span", { class: "qz-option-text" }, choice),
        mark ? el("span", { class: "qz-option-mark" }, ui.icon(mark[0], { label: mark[1] })) : null));
      });
      children.push(group);
    } else {
      const area = el("textarea", { class: "qz-answer", rows: "4", placeholder: "Type your answer", "aria-label": "Your answer",
        maxlength: "2000", disabled: checking || phase === "feedback" });
      area.value = draft;
      area.addEventListener("input", () => { draft = area.value; qError = null; syncCheck(); });
      children.push(area);
    }

    let checkBtn = null;
    const canCheck = q.kind === "mcq" ? selected !== null : draft.trim().length > 0;
    function syncCheck() { if (checkBtn) checkBtn.disabled = !(q.kind === "mcq" ? selected !== null : draft.trim().length > 0); }
    if (phase !== "feedback") {
      if (qError) children.push(ui.notice(qError.message, { tone: "bad" }));
      if (qError && qError.status === 404) {
        children.push(ui.button({ label: "Back to setup", variant: "primary", size: "lg", block: true, onClick: () => { quiz = null; showScreen("setup"); } }));
      } else {
        checkBtn = ui.button({ label: checking ? "Checking…" : qError ? "Retry" : "Check answer", variant: "primary", size: "lg", block: true,
          id: "qz-check", disabled: checking || !canCheck, onClick: () => submitAnswer() });
        if (checking) checkBtn.classList.add("is-loading");
        children.push(checkBtn);
        if (checking && q.kind === "short") children.push(el("p", { class: "qz-small" }, "The model is marking your answer. This can take a moment."));
      }
    }

    const card = el("section", { class: "qz-qcard ui-stack" }, ...children);
    stage.replaceChildren(head, progress, card);
    if (phase === "feedback" && done) stage.append(renderFeedback(q, done));
  }

  function focusOption(i) { stage.querySelector(`[data-choice="${i}"]`)?.focus({ preventScroll: true }); }

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
      ui.haptic(data.correct ? "success" : "warn");
      renderQuestion();
      const fb = stage.querySelector(".qz-feedback-title");
      fb?.focus({ preventScroll: true });
      fb?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (error) {
      phase = "answering";
      qError = { message: error.message || "That didn't work.", status: error.status };
      renderQuestion();
      stage.querySelector("#qz-check")?.focus({ preventScroll: true });
    }
  }

  function verdict(r) {
    const score = typeof r.score === "number" ? r.score : (r.correct ? 1 : 0);
    if (r.correct && score >= 1) return { cls: "good", ico: "check-circle", text: "Correct" };
    if (r.correct) return { cls: "good", ico: "check-circle", text: `Mostly right (${pct(score)})` };
    if (score > 0) return { cls: "warn", ico: "alert", text: `Partly right (${pct(score)})` };
    return { cls: "bad", ico: "alert-circle", text: "Not quite" };
  }

  function renderFeedback(q, done) {
    const r = done.result;
    const v = verdict(r);
    const last = idx === quiz.questions.length - 1;
    const rows = [];
    const row = (k, text) => el("p", { class: "qz-fb-row" }, el("strong", {}, `${k}: `), text);
    if (q.kind === "short") rows.push(row("Your answer", done.text));
    rows.push(row("Correct answer", r.correct_answer));
    if (r.explanation) rows.push(row("Why", r.explanation));
    if (r.feedback) rows.push(row("Feedback", r.feedback));
    const nextButton = ui.button({ label: finishing ? "Finishing…" : (last && qError ? "Retry" : last ? "Finish" : "Next question"),
      icon: last ? "check" : "arrow-right", variant: "primary", size: "lg", block: true, disabled: finishing,
      onClick: () => (last ? finishQuiz() : nextQuestion()) });
    if (finishing) nextButton.classList.add("is-loading");
    return el("section", { class: `qz-feedback is-${v.cls} ui-stack`, "aria-live": "polite" },
      el("h3", { class: "qz-feedback-title", tabindex: "-1" }, ui.icon(v.ico), el("span", {}, v.text)),
      ...rows,
      last && qError ? ui.notice(qError.message, { tone: "bad" }) : null,
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
    const tone = percent >= 80 ? "good" : percent >= 50 ? "warn" : "bad";
    const headline = percent >= 80 ? "Great work" : percent >= 50 ? "Good effort" : "Keep practising";
    const ring = ui.ringProgress({ value: percent / 100, size: 148, stroke: 12, tone, big: `${percent}%`, small: `${r.score} of ${r.total}`, label: "Quiz score" });
    ring.classList.add("qz-ring");
    const parts = [
      el("div", { class: "qz-hero" }, ring,
        el("h2", { class: "qz-result-title qz-h", tabindex: "-1" }, headline),
        el("p", { class: "qz-small" }, r.title || "Quiz finished")),
    ];
    const weak = r.weak_topics || [];
    if (weak.length) {
      parts.push(el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, "Topics to revisit"),
        el("div", { class: "qz-chips" }, weak.map((t) => ui.badge(t, "warn")))));
    }
    const missed = r.missed || [];
    if (missed.length) {
      parts.push(el("div", { class: "ui-field" }, el("div", { class: "ui-label" }, `Missed questions (${missed.length})`),
        el("div", { class: "qz-missed ui-list ui-list--inset" }, missed.map((m) =>
          el("details", { class: "qz-miss" },
            el("summary", {}, el("span", { class: "qz-miss-q" }, clip(m.question, 110)), ui.icon("chevron-down", { class: "qz-miss-chev" })),
            el("div", { class: "qz-miss-body" },
              clip(m.question, 110) !== String(m.question).replace(/\s+/g, " ").trim()
                ? el("p", {}, el("strong", {}, "Question: "), m.question) : null,
              el("p", {}, el("strong", {}, "Your answer: "), m.your_answer),
              el("p", {}, el("strong", {}, "Correct answer: "), m.correct_answer),
              m.explanation ? el("p", {}, el("strong", {}, "Why: "), m.explanation) : null,
              m.topic ? el("p", { class: "qz-small" }, `Topic: ${m.topic}`) : null))))));
    } else {
      parts.push(ui.notice("You got every question right.", { tone: "good" }));
    }
    return el("section", { class: "qz-result ui-stack ui-stack--lg" },
      ...parts,
      el("div", { class: "qz-actions" },
        ui.button({ label: "New quiz", variant: "secondary", size: "lg", onClick: () => { quiz = null; showScreen("setup"); } }),
        ui.button({ label: "Same source again", icon: "refresh", variant: "primary", size: "lg", disabled: !lastSettings,
          onClick: () => lastSettings && generate({ ...lastSettings }) })));
  }

  // -------------------------------------------------------------- history
  let historySeq = 0;
  let showAllHistory = false;
  let lastStreak = 0;
  async function loadHistory() {
    const mine = ++historySeq;
    historyBox.replaceChildren(el("h2", { id: "qz-history-title", class: "qz-h" }, "History"), ui.skeleton("list", 2));
    try {
      const data = await api("/api/learn/history");
      if (mine !== historySeq) return;
      lastStreak = data.streak_days || 0;
      paintHistory(data);
    } catch (error) {
      if (mine !== historySeq) return;
      historyBox.replaceChildren(el("h2", { id: "qz-history-title", class: "qz-h" }, "History"),
        ui.notice(error.message, { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => loadHistory() }));
    }
  }
  function paintHistory(data) {
    const quizzes = data.quizzes || [];
    const days = data.streak_days || 0;
    const streak = el("span", { class: `ui-badge ${days ? "ui-badge--warn" : ""} qz-streak` }, ui.icon("flame"), days ? `${days} day streak` : "No streak yet");
    const head = el("div", { class: "qz-history-head" }, el("h2", { id: "qz-history-title", class: "qz-h" }, "History"), streak);
    if (!quizzes.length) {
      historyBox.replaceChildren(head, ui.emptyState({ icon: "quiz", compact: true, title: "No finished quizzes yet", body: "Your results will show up here." }));
      return;
    }
    const visible = showAllHistory ? quizzes : quizzes.slice(0, 8);
    const rows = visible.map((e) => ui.listRow({ icon: "quiz", tone: e.percent >= 80 ? "good" : e.percent >= 50 ? "warn" : "bad",
      title: e.title || "Quiz",
      sub: [prettyDate(e.date), e.source_label ? clip(e.source_label, 40) : ""].filter(Boolean).join(" · "),
      meta: ui.badge(`${e.percent}%`, e.percent >= 80 ? "good" : e.percent >= 50 ? "warn" : "bad") }));
    const more = quizzes.length > 8
      ? ui.button({ label: showAllHistory ? "Show fewer" : `Show all (${quizzes.length})`, variant: "ghost", size: "sm",
        onClick: () => { showAllHistory = !showAllHistory; paintHistory(data); } })
      : null;
    historyBox.replaceChildren(...[head, el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows), more].filter(Boolean));
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
