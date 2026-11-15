// Tailor: a resume and a cover note / proposal for ONE posting, built only from your own facts, with a fit check and an
// honesty check; plus the answer bank (your saved answers to standard application questions), your facts and preferences.
// Routes: docs/api/tailor.md. Every string goes in with textContent (el()), never as markup. Nothing here sends anything:
// "Use in application" only writes the draft into the Applications queue, where Send still needs your tap.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { formSheet, listEditor } from "./listeditor.js";

const CSS = "/css/tailor.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const loadCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };
const SEV = { high: "bad", medium: "warn", low: "gray" };
const REQ = { matched: ["Matched", "good"], partial: ["Partial", "warn"], missing: ["Missing", "bad"], info: ["Info", "gray"] };
const CHANNELS = [["", "Choose for me"], ["email", "Email"], ["form", "Application form"], ["freelance", "Freelance proposal"], ["outreach", "Cold outreach"], ["message", "Short message"]];
const STATE = { ready: "ready", empty: "needs your answer", pending: "needs your OK", owner_only: "always yours" };

function download(href) {
  const a = el("a", { href, download: "" });
  document.body.append(a); a.click(); a.remove();
}

function copyText(text) {
  (navigator.clipboard?.writeText(text) || Promise.reject()).then(() => ui.toast("Copied.", { kind: "good" }), () => ui.toast("Could not copy; select the text instead.", { kind: "bad" }));
}

export function init(container) {
  loadCss();
  const root = el("div", { class: "tl ui-stack ui-stack--lg" });
  container.append(root);
  let tab = "tailor", prefs = null, variant = null, lastItems = [];
  const body = el("div", { class: "ui-stack ui-stack--lg" });

  const seg = ui.segmented({ label: "Tailor", value: tab, options: [["tailor", "Tailor"], ["answers", "Answers"], ["facts", "Facts"], ["prefs", "Preferences"]]
    .map(([value, label]) => ({ value, label })), onChange: (v) => { tab = v; paint(); } });
  root.append(el("p", { class: "ui-hint" }, "Everything here comes from your own resume and the facts you confirm. Nothing is sent from this screen."), seg, body);

  async function paint() {
    body.replaceChildren(ui.skeleton("list", 2));
    try {
      if (tab === "tailor") body.replaceChildren(...await tailorTab());
      else if (tab === "answers") body.replaceChildren(...await answersTab());
      else if (tab === "facts") body.replaceChildren(...await factsTab());
      else body.replaceChildren(...await prefsTab());
    } catch (e) { body.replaceChildren(ui.notice(e.message || "Couldn't load.", { tone: "bad" })); }
  }

  // ---- tailor ---------------------------------------------------------------------------------------------------------
  async function tailorTab() {
    try { lastItems = ((await api("/api/applyq?tab=ready")).items || []).slice(0, 50); } catch (_) { lastItems = []; }
    prefs = (await api("/api/tailor/prefs"));
    const pick = el("select", { class: "ui-input", "aria-label": "Application" }, el("option", { value: "" }, "Paste a posting instead"),
      ...lastItems.map((i) => el("option", { value: i.id }, `${i.title || "Untitled"} (${i.company || "unknown"})`)));
    const title = el("input", { class: "ui-input", type: "text", maxlength: "200", "aria-label": "Job title", placeholder: "Job title" });
    const company = el("input", { class: "ui-input", type: "text", maxlength: "120", "aria-label": "Company", placeholder: "Company" });
    const desc = el("textarea", { class: "ui-input", rows: "7", maxlength: "12000", "aria-label": "Posting text", placeholder: "Paste the posting text here" });
    const channel = el("select", { class: "ui-input", "aria-label": "Channel" }, ...CHANNELS.map(([v, t]) => el("option", { value: v }, t)));
    const ai = ui.switchControl({ checked: !!prefs.ai, label: "Polish the wording with my AI (still checked against my facts)" });
    const go = ui.button({ label: "Tailor", icon: "wand", variant: "primary", onClick: async () => {
      go.disabled = true;
      try {
        const bodyJson = { channel: channel.value || undefined, ai: ai.input.checked };
        if (pick.value) bodyJson.application_id = pick.value; else bodyJson.job = { title: title.value, company: company.value, description: desc.value };
        variant = await api("/api/tailor/preview", { method: "POST", json: bodyJson });
        paint();
      } catch (e) { fail(e); } finally { go.disabled = false; }
    } });
    pick.addEventListener("change", () => { for (const n of [title, company, desc]) n.disabled = !!pick.value; });
    const form = el("section", { class: "ui-card ui-hud ui-stack tl-form" }, ui.eyebrow("01", "Pick a posting"),
      field("Application", pick), field("Job title", title), field("Company", company), field("Posting", desc), field("Format", channel), ai, go);
    const parts = [form];
    if (variant) parts.push(...variantView(variant));
    const rows = (await api("/api/tailor/variants")).variants || [];
    if (rows.length) parts.push(el("section", { class: "le st-group" }, el("h2", { class: "ui-caps st-caps" }, "Earlier variants"),
      el("div", { class: "ui-list ui-list--inset" }, ...rows.slice(0, 15).map((v) => ui.listRow({ icon: v.blocked ? "alert" : "file", tone: v.blocked ? "warn" : "accent",
        title: `${v.job.title || "Posting"}${v.job.company ? " at " + v.job.company : ""}`, sub: `${v.channel} · fit ${v.fit.score ?? "?"} · ${String(v.created).slice(0, 10)}${v.blocked ? " · needs you" : ""}`,
        onClick: () => { variant = v; tab = "tailor"; paint(); } })))));
    return parts;
  }

  function field(label, control, hint) {
    return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
  }

  function variantView(v) {
    const out = [];
    const fit = v.fit || {};
    out.push(el("section", { class: "ui-card ui-hud ui-stack tl-fit" }, ui.eyebrow("02", "Fit and honesty check"),
      el("div", { class: "tl-score" }, ui.ringProgress({ value: (fit.score ?? 0) / 100, size: 72, big: fit.score == null ? "?" : String(fit.score), small: fit.label || "" }),
        el("p", { class: "tl-sum" }, fit.summary || "")),
      v.blocked ? ui.notice("Blocked until you resolve: " + v.blockers.join(" "), { tone: "warn", icon: "alert" }) : ui.notice("Every sentence below is backed by your facts.", { tone: "good", icon: "check-circle" }),
      ...(fit.flags || []).map((f) => el("div", { class: "tl-flag" }, ui.badge(f.severity, SEV[f.severity] || "gray"), el("span", {}, f.label + (f.detail ? ` (${f.detail})` : "")))),
      ...(fit.unchecked || []).length ? [el("p", { class: "ui-hint" }, "Not checked: " + fit.unchecked.join("; "))] : [],
      fit.risk_blocked && !v.risk_ack ? ui.button({ label: "I checked it", icon: "shield", variant: "secondary", size: "sm", onClick: () => resolve(v, { action: "ack_risk" }) }) : null));
    out.push(el("section", { class: "le st-group" }, el("h2", { class: "ui-caps st-caps" }, "Requirements"),
      el("div", { class: "ui-list ui-list--inset" }, ...(v.requirements || []).map((r) => ui.listRow({ icon: r.status === "matched" ? "check-circle" : r.status === "partial" ? "alert-circle" : "x",
        tone: (REQ[r.status] || [])[1] || "gray", title: r.text, sub: [(REQ[r.status] || [r.status])[0], r.must ? "must have" : "nice to have", r.note].filter(Boolean).join(" · ") })))));
    out.push(coverCard(v));
    out.push(resumeCard(v));
    return out;
  }

  function coverCard(v) {
    const c = v.cover;
    const card = el("section", { class: "ui-card ui-hud ui-stack tl-cover" }, ui.eyebrow("03", c.channel === "freelance" ? "Proposal" : "Cover note"));
    if (c.ai_note) card.append(ui.notice(c.ai_note, { icon: "info" }));
    if (c.subject) card.append(el("p", { class: "tl-subject" }, "Subject: " + c.subject));
    if (c.greeting) card.append(el("p", {}, c.greeting));
    for (const b of c.blocks) {
      if (b.heading) card.append(el("h3", { class: "tl-h" }, b.heading));
      for (const s of b.sentences) card.append(sentenceRow(v, s));
    }
    if (c.closing) card.append(el("p", {}, c.closing + (c.signature ? " " + c.signature : "")));
    card.append(el("p", { class: "ui-hint" }, `${c.words} words · ${c.generator === "ai" ? "AI wording, checked against your facts" : "built from your facts"} · tone: ${c.tone}`));
    const plain = [c.greeting, ...c.blocks.map((b) => b.sentences.map((s) => s.text).join(" ")), c.closing && `${c.closing}\n${c.signature || ""}`].filter(Boolean).join("\n\n");
    card.append(el("div", { class: "tl-actions" },
      ui.button({ label: "Copy", icon: "copy", variant: "secondary", size: "sm", onClick: () => copyText(plain) }),
      ...["md", "docx", "pdf"].map((f) => ui.button({ label: f.toUpperCase(), icon: "download", variant: "secondary", size: "sm", disabled: v.blocked,
        onClick: () => download(`/api/tailor/variants/${v.id}/export?format=${f}&part=cover`) })),
      ui.button({ label: "Use in application", icon: "send", variant: "primary", size: "sm", onClick: () => useIn(v) })));
    return card;
  }

  function sentenceRow(v, s) {
    const bad = s.status === "unbacked";
    const row = el("div", { class: "tl-sent" + (bad ? " is-bad" : "") },
      el("span", { class: "tl-sent__text" }, s.text),
      s.status === "resolved" ? ui.badge("you confirmed", "good") : null);
    if (bad) {
      row.append(el("p", { class: "ui-hint" }, "Not backed by your facts: " + (s.reason || "")),
        el("div", { class: "tl-actions" },
          ui.button({ label: "Remove", icon: "trash", variant: "secondary", size: "sm", onClick: () => resolve(v, { action: "remove", sentence: s.id }) }),
          ui.button({ label: "Rewrite", icon: "edit", variant: "secondary", size: "sm", onClick: () => formSheet({ title: "Rewrite this sentence", fields: [
            { key: "text", label: "Sentence", type: "textarea", rows: 3, maxlength: 600 }, { type: "note", text: "It is checked again against your facts." }], values: { text: s.text },
            onSave: async (val) => { await resolve(v, { action: "edit", sentence: s.id, text: val.text }); } }) }),
          ui.button({ label: "This is true", icon: "check", variant: "tonal", size: "sm", onClick: () => formSheet({ title: "Confirm as a fact", fields: [
            { key: "fact_text", label: "The fact, in your words", type: "textarea", rows: 3, maxlength: 600 },
            { type: "note", text: "Only confirm what is true. It is saved to your facts and can back later drafts." }], values: { fact_text: s.text },
            onSave: async (val) => { await resolve(v, { action: "confirm", sentence: s.id, fact_text: val.fact_text }); } }) })));
    }
    const m = /\[FILL: ([^\]]+)\]/.exec(s.text);
    if (m) {
      row.append(ui.button({ label: `Fill in: ${m[1]}`, icon: "edit", variant: "tonal", size: "sm", onClick: () => formSheet({ title: `Fill in: ${m[1]}`, fields: [{ key: "value", label: m[1], type: "text" }],
        onSave: async (val) => { await resolve(v, { action: "fill", label: m[1], value: val.value }); } }) }));
    }
    return row;
  }

  function resumeCard(v) {
    const card = el("section", { class: "ui-card ui-hud ui-stack tl-resume" }, ui.eyebrow("04", "Resume for this posting"));
    if (v.resume.matched?.length) card.append(el("p", { class: "ui-hint" }, "Matches the posting on: " + v.resume.matched.join(", ")));
    for (const sec of v.resume.sections) {
      card.append(el("h3", { class: "tl-h" }, sec.name));
      for (const ln of sec.lines) card.append(el("p", { class: "tl-line tl-" + ln.style }, (ln.style === "bullet" ? "- " : "") + ln.text));
    }
    const diffBox = el("pre", { class: "tl-diff", hidden: true });
    card.append(diffBox, el("div", { class: "tl-actions" },
      ui.button({ label: "Compare with my base resume", icon: "list", variant: "secondary", size: "sm", onClick: async () => {
        if (!diffBox.hidden) { diffBox.hidden = true; return; }
        try { const d = await api(`/api/tailor/variants/${v.id}/diff`); diffBox.textContent = d.unified || "No differences."; diffBox.hidden = false; } catch (e) { fail(e); }
      } }),
      ...["md", "txt", "docx", "pdf"].map((f) => ui.button({ label: f.toUpperCase(), icon: "download", variant: "secondary", size: "sm",
        onClick: () => download(`/api/tailor/variants/${v.id}/export?format=${f}&part=resume`) }))));
    return card;
  }

  async function resolve(v, bodyJson) {
    variant = await api(`/api/tailor/variants/${v.id}/resolve`, { method: "POST", json: bodyJson });
    paint();
  }

  async function useIn(v) {
    let id = v.application_id;
    if (!id) {
      const sel = el("select", { class: "ui-input", "aria-label": "Application" }, ...lastItems.map((i) => el("option", { value: i.id }, `${i.title || "Untitled"} (${i.company || "unknown"})`)));
      if (!lastItems.length) { ui.toast("Add the posting to Applications first.", { kind: "bad" }); return; }
      ui.sheet({ title: "Use in which application?", content: el("div", { class: "ui-stack" }, field("Application", sel), ui.notice("This writes the draft only. Sending still needs your tap in Applications.", { icon: "info" })),
        actions: [{ label: "Use it", variant: "primary", icon: "check", onClick: async () => { try { await apply(v, sel.value); return true; } catch (e) { fail(e); return false; } } }] });
      return;
    }
    try { await apply(v, id); } catch (e) { fail(e); }
  }

  async function apply(v, id) {
    const r = await api(`/api/tailor/variants/${v.id}/apply`, { method: "POST", json: { application_id: id } });
    ui.toast(r.blocked ? "Written, with a blank that blocks sending until you resolve the flagged claims." : "Written to the application draft. Nothing was sent.", { kind: r.blocked ? "bad" : "good" });
  }

  // ---- answers --------------------------------------------------------------------------------------------------------
  async function answersTab() {
    const d = await api("/api/answerbank");
    const parts = [];
    const q = el("input", { class: "ui-input", type: "text", maxlength: "400", "aria-label": "A question from a form", placeholder: "Type a question from a form to see what I would answer" });
    const result = el("div", { class: "ui-stack" });
    const ask = ui.button({ label: "Check", icon: "search", variant: "tonal", onClick: async () => {
      try {
        const m = await api("/api/answerbank/match", { method: "POST", json: { question: q.value } });
        result.replaceChildren(matchCard(m, q.value));
      } catch (e) { fail(e); }
    } });
    parts.push(el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("01", "Try a question"), field("Question", q), ask, result,
      el("p", { class: "ui-hint" }, `Only approved answers at ${d.min_confidence}+ confidence may be pre-filled; lower ones are suggestions. ${d.embeddings ? "Matching also uses your local model." : "Matching uses words."}`)));
    const pending = d.entries.filter((e) => e.state === "pending");
    if (pending.length) {
      parts.push(el("section", { class: "le st-group" }, el("h2", { class: "ui-caps st-caps" }, "Needs your OK"),
        el("div", { class: "ui-list ui-list--inset" }, ...pending.map((e) => ui.listRow({ icon: "sparkles", tone: "warn", title: e.label, sub: e.answer, onClick: () => approve(e) })))));
    }
    const ed = listEditor({ title: "Your answers", hint: "Used for application forms and drafts. Use {company}, {role}, {rate}, {availability}, {link_github}, {proof} as blanks that fill themselves.",
      items: d.entries.filter((e) => e.state !== "pending"),
      rowOf: (e) => ({ icon: e.owner_only ? "lock" : "note", tone: e.state === "empty" ? "warn" : "accent", title: e.label,
        sub: e.owner_only ? "Always answered by you: " + (e.answer || "") : e.answer ? e.answer.slice(0, 90) : "Needs your answer", meta: STATE[e.state] }),
      onEdit: (e) => editEntry(e), onDelete: async (e) => { await api(`/api/answerbank/${e.id}`, { method: "DELETE" }); paint(); },
      onAdd: () => editEntry(null), addLabel: "Add an answer", onReset: async () => { const r = await api("/api/answerbank/reset-starters", { method: "POST" }); ui.toast(`${r.added} starter(s) added back.`, { kind: "good" }); paint(); },
      resetLabel: "Add back missing starters", empty: "No answers yet." });
    parts.push(ed.el);
    return parts;
  }

  function matchCard(m, question) {
    const tone = m.status === "auto" ? "good" : m.status === "owner_only" ? "warn" : m.status === "suggest" ? "warn" : "gray";
    const label = { auto: "Would fill in", suggest: "Suggestion only", owner_only: "Always yours", none: "No answer" }[m.status] || m.status;
    const card = el("div", { class: "ui-card ui-stack tl-match" }, el("div", { class: "tl-flag" }, ui.badge(label, tone), el("span", {}, `confidence ${m.confidence}`)),
      m.answer ? el("p", { class: "tl-sent__text" }, m.answer) : null, m.reason ? el("p", { class: "ui-hint" }, m.reason) : null);
    if (m.status === "none" || (m.status === "suggest" && !m.answer)) {
      card.append(ui.button({ label: "Draft one from my facts", icon: "sparkles", variant: "secondary", size: "sm", onClick: async () => {
        try {
          const d = await api("/api/answerbank/draft", { method: "POST", json: { question } });
          ui.toast(d.drafted ? "Drafted. It waits for your OK." : (d.reason || "I could not draft one."), { kind: d.drafted ? "good" : "bad" });
          if (d.drafted) paint();
        } catch (e) { fail(e); }
      } }));
    }
    return card;
  }

  function approve(e) {
    formSheet({ title: "Approve this answer?", fields: [{ key: "answer", label: "Answer", type: "textarea", rows: 4, maxlength: 2000 },
      { type: "note", text: "It was drafted from your facts. Edit it if needed; once approved it is saved to your bank and may be used." }], values: { answer: e.answer },
    saveLabel: "Approve", onSave: async (v) => { await api(`/api/answerbank/${e.id}/approve`, { method: "POST", json: { answer: v.answer } }); paint(); } });
  }

  function editEntry(e) {
    formSheet({ title: e ? "Edit answer" : "New answer", tall: true, values: e ? { ...e, questions: (e.questions || []).join("\n") } : { category: "custom", questions: "" },
      fields: [{ key: "label", label: "Label", type: "text", maxlength: 100 },
        { key: "category", label: "Kind", type: "select", options: ["custom", "salary", "rate", "notice", "availability", "work_auth", "relocation", "links", "why_company", "demographic"].map((c) => [c, c.replace("_", " ")]) },
        { key: "questions", label: "Ways the question is asked (one per line)", type: "textarea", rows: 4, maxlength: 1500 },
        { key: "answer", label: "Your answer", type: "textarea", rows: 4, maxlength: 2000, hint: "Blanks like {company} or {proof} fill themselves; a blank that cannot be filled stays visible and stops auto-fill." },
        { type: "note", text: "Demographic, ID, date-of-birth and criminal-record questions are always yours: they are never filled in automatically." }],
      onSave: async (v) => {
        const json = { label: v.label, category: v.category, questions: String(v.questions).split("\n").map((x) => x.trim()).filter(Boolean), answer: v.answer };
        if (e) await api(`/api/answerbank/${e.id}`, { method: "PUT", json }); else await api("/api/answerbank", { method: "POST", json });
        paint();
      } });
  }

  // ---- facts ----------------------------------------------------------------------------------------------------------
  async function factsTab() {
    const d = await api("/api/tailor/facts");
    const NAMES = { name: "Name", headline: "Headline", summary: "Summary", skill: "Skills", role: "Roles", bullet: "Achievements", metric: "Numbers", education: "Education", link: "Links",
      answer: "Your standard answers", confirmed: "Confirmed by you", derived: "Worked out", pref: "Your preferences" };
    const groups = {};
    for (const f of d.facts) (groups[f.type] ||= []).push(f);
    const parts = [el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("01", "Your facts"),
      el("p", { class: "ui-hint" }, `${d.facts.length} facts from your profile${d.years_experience ? `, about ${d.years_experience} years of dated experience` : ""}. Every sentence in a draft cites some of these. Add anything true that your resume misses.`),
      ui.button({ label: "Add a fact", icon: "plus", variant: "primary", onClick: () => formSheet({ title: "Add a fact", fields: [
        { key: "text", label: "A true statement about you", type: "textarea", rows: 3, maxlength: 600, placeholder: "Spoke at a regional meetup in 2023" },
        { key: "tags", label: "Tags (use 'project' to list it under Projects)", type: "chips" }], values: {},
      onSave: async (v) => { await api("/api/tailor/facts", { method: "POST", json: v }); paint(); } }) }))];
    for (const type of Object.keys(NAMES)) {
      const rows = groups[type];
      if (!rows) continue;
      parts.push(el("section", { class: "le st-group" }, el("h2", { class: "ui-caps st-caps" }, NAMES[type]),
        el("div", { class: "ui-list ui-list--inset" }, ...rows.map((f) => ui.listRow({ icon: f.editable ? "pen" : "note", tone: f.editable ? "accent" : "gray", title: f.text, sub: f.source,
          onClick: () => factSheet(f) })))));
    }
    if ((d.hidden || []).length) parts.push(ui.notice(`${d.hidden.length} fact(s) are hidden and never used.`, { icon: "eye-off" }));
    return parts;
  }

  function factSheet(f) {
    if (f.editable) {
      formSheet({ title: "Your confirmed fact", fields: [{ key: "text", label: "Fact", type: "textarea", rows: 3, maxlength: 600 }], values: { text: f.text },
        extra: ui.button({ label: "Delete", icon: "trash", variant: "danger", onClick: async () => { await api(`/api/tailor/facts/${f.id}`, { method: "DELETE" }); paint(); } }),
        onSave: async (v) => { await api(`/api/tailor/facts/${f.id}`, { method: "PUT", json: { text: v.text } }); paint(); } });
    } else {
      ui.sheet({ title: "From your profile", content: el("div", { class: "ui-stack" }, el("p", {}, f.text), el("p", { class: "ui-hint" }, "Source: " + f.source + ". Edit it in Opportunities > Profile."),
        ui.button({ label: "Never use this fact", icon: "eye-off", variant: "secondary", onClick: async () => { await api(`/api/tailor/facts/${f.id}`, { method: "DELETE" }); paint(); } })) });
    }
  }

  // ---- preferences ----------------------------------------------------------------------------------------------------
  async function prefsTab() {
    const d = await api("/api/tailor/prefs");
    let s = {};
    try { s = await api("/api/settings"); } catch (_) { /* the switches stay unchecked */ }
    const p = d.prefs;
    const patch = async (key, value) => { try { await api("/api/settings", { method: "PATCH", json: { [key]: value } }); } catch (e) { fail(e); paint(); } };
    const sw = (key, label) => ui.switchControl({ checked: s[key] !== false && !!s[key], label, onChange: (on) => patch(key, on) });
    const minConf = el("input", { class: "ui-input", type: "number", min: "30", max: "100", value: String(s.answerbank_min_confidence ?? 75), "aria-label": "Lowest confidence to pre-fill" });
    minConf.addEventListener("change", () => patch("answerbank_min_confidence", Number(minConf.value)));
    return [
      el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("01", "Switches"),
        sw("tailor_enabled", "Tailoring on"), sw("tailor_ai", "Polish cover notes with my AI by default (checked against my facts)"),
        sw("answerbank_enabled", "Answer bank on"), sw("answerbank_embeddings", "Match questions with my local model as well as words"),
        field("Lowest confidence that may pre-fill an answer (30 to 100)", minConf)),
      el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("02", "How I write and what to check"),
        el("p", { class: "ui-hint" }, `Tone ${p.tone}${p.rate ? " · rate " + p.rate : ""}${p.location ? " · " + p.location : ""}. Anything left empty is not checked and never guessed.`),
        ui.button({ label: "Edit preferences", icon: "sliders", variant: "primary", onClick: () => editPrefs(d) })),
    ];
  }

  function editPrefs(d) {
    const p = d.prefs;
    const aliases = Object.entries(d.aliases || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
    formSheet({ title: "Preferences", tall: true, values: { ...p, aliases, timezone_offset: p.timezone_offset },
      fields: [
        { key: "tone", label: "Tone", type: "select", options: d.tones.map((t) => [t, t]) },
        { key: "tone_notes", label: "Style notes for the AI (optional)", type: "textarea", rows: 2, maxlength: 400 },
        { key: "sign_off", label: "Sign-off (replaces the tone's closing)", type: "text" },
        { key: "rate", label: "Freelance rate, as you want it written", type: "text", placeholder: "$60/hour" },
        { key: "availability", label: "Availability, as you want it written", type: "text", placeholder: "from the 1st of next month" },
        { key: "location", label: "Where you are", type: "text", placeholder: "City, Country" },
        { key: "remote_ok", label: "Remote work is fine", type: "switch" },
        { key: "relocate", label: "Open to relocating", type: "switch", default: false },
        { key: "timezone_offset", label: "Your time zone, hours from UTC", type: "number", min: -12, max: 14, step: 0.5 },
        { key: "tz_tolerance_hours", label: "How many hours apart is fine", type: "number", min: 0, max: 24 },
        { key: "min_pay_yearly", label: "Lowest yearly pay (as the postings write it)", type: "number", min: 0 },
        { key: "min_pay_hourly", label: "Lowest hourly pay", type: "number", min: 0 },
        { key: "work_auth", label: "Places you may work without sponsorship", type: "chips" },
        { key: "needs_sponsorship", label: "You need visa sponsorship", type: "switch", default: false },
        { key: "max_bullets", label: "Most bullets per recent role on a resume", type: "number", min: 1, max: 8 },
        { key: "aliases", label: "Words that mean the same (one per line: short = long)", type: "textarea", rows: 3, maxlength: 1500 }],
      onSave: async (v) => {
        const al = {};
        for (const line of String(v.aliases || "").split("\n")) { const [a, b] = line.split("="); if (a && b && a.trim() && b.trim()) al[a.trim()] = b.trim(); }
        const json = { ...v, aliases: al };
        for (const k of ["timezone_offset", "min_pay_yearly", "min_pay_hourly"]) if (json[k] === null) json[k] = "";
        await api("/api/tailor/prefs", { method: "PUT", json });
        paint();
      } });
  }

  paint();
  return { refresh: paint, destroy() {} };
}
