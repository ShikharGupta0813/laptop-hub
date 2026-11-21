// ATS and packets: how a posting scores against a resume (the way an applicant tracking system filters), how an ATS reads your resume
// file, master resumes per role family, a background tailoring queue you review one by one, and a one-page application packet.
// Routes: docs/api/ats.md, docs/api/resumes.md, docs/api/packet.md. Every string goes in with textContent (el()), never as markup.
// Nothing here sends, submits or applies. A missing skill is shown as a suggestion or a gap and is never added to your resume.
// Each tab makes one data call when you open it; there is no polling (use Refresh on the queue).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/ats.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const SEV = { high: "bad", medium: "warn", low: "gray" };
const TONE = { strong: "good", good: "good", fair: "warn", low: "bad" };
const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);

async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}
const field = (label, c, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), c, hint ? el("span", { class: "ui-hint" }, hint) : null);
const text = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "200", ...attrs }); i.value = value || ""; return i; };
const area = (rows, attrs = {}) => el("textarea", { class: "ui-input", rows: String(rows), ...attrs });
const toBase64 = (file) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(String(r.result).split(",")[1] || "");
  r.onerror = () => reject(new Error("Could not read that file."));
  r.readAsDataURL(file);
});

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "ats ui-stack ui-stack--lg" });
  container.append(root);
  let tab = "score";
  const body = el("div", { class: "ui-stack ui-stack--lg" });
  let postings = null;       // the application queue, loaded once when a posting picker is first needed

  // ---- a posting picker: one of your queued applications, or pasted text ----
  async function loadPostings() {
    if (postings) return postings;
    try { postings = (await api("/api/applyq?tab=ready")).items.slice(0, 60); } catch { postings = []; }
    return postings;
  }
  async function picker() {
    const items = await loadPostings();
    const sel = el("select", { class: "ui-input", "aria-label": "Posting" }, el("option", { value: "" }, "Paste a posting instead"),
      ...items.map((i) => el("option", { value: i.id }, `${i.title}${i.company ? " · " + i.company : ""}`.slice(0, 80))));
    const title = text("", { placeholder: "Job title", maxlength: "200" });
    const desc = area(7, { placeholder: "Paste the posting text", maxlength: "12000", "aria-label": "Posting text" });
    if (items.length) sel.value = items[0].id;
    const paste = el("div", { class: "ui-stack" }, field("Job title", title), field("Posting text", desc));
    const sync = () => { paste.hidden = !!sel.value; };
    sel.addEventListener("change", sync); sync();
    return { node: el("div", { class: "ui-stack" }, field("Which posting", sel), paste),
      job() { return sel.value ? { application_id: sel.value } : { job: { title: title.value, description: desc.value } }; } };
  }

  // ---- 1. match score ----
  function scoreView(d) {
    const missing = d.missing || { listed_in_facts: [], gap: [] };
    const chip = (k, tone) => el("li", { class: "ats-chip" + (tone ? " " + tone : "") }, k.term + (k.bucket === "must" ? " *" : ""));
    return el("div", { class: "ui-card ui-stack ats-card" },
      el("div", { class: "ats-score" }, ui.ringProgress({ value: (d.score || 0) / 100, size: 72, stroke: 8, big: String(d.score ?? "?"), tone: TONE[d.label] || "accent", label: "ATS match" }),
        el("div", { class: "ui-stack" }, el("h3", { class: "ats-title" }, `ATS match: ${d.label}`), el("span", { class: "ats-sub" }, `${d.job.title} · ${d.resume}`))),
      el("div", { class: "ats-bars" }, ...d.breakdown.map((b) => el("div", { class: "ats-bar" + (b.score == null ? " off" : "") },
        el("span", { class: "k" }, b.label), el("span", { class: "n" }, b.score == null ? "n/a" : String(b.score)),
        b.score == null ? null : ui.progressBar(b.score / 100, b.label), el("span", { class: "ats-sub" }, b.note || "")))),
      d.matched.length ? el("div", {}, ui.eyebrow("", `Matched (${d.matched.length})`), el("ul", { class: "ats-chips" }, ...d.matched.map((k) => chip(k, "good")))) : null,
      missing.listed_in_facts.length ? el("div", {}, ui.eyebrow("", "You have this: it is in your facts but not on this resume"),
        el("p", { class: "ui-hint" }, "A suggestion. If you want it on the resume, add it yourself. It is not added for you."),
        el("ul", { class: "ats-chips" }, ...missing.listed_in_facts.map((k) => chip(k, "warn")))) : null,
      missing.gap.length ? el("div", {}, ui.eyebrow("", "You do not have this: a gap"), el("p", { class: "ui-hint" }, "Shown so you know. It is never added to anything."),
        el("ul", { class: "ats-chips" }, ...missing.gap.map((k) => chip(k, "bad")))) : null,
      d.tips.length ? el("ul", { class: "ats-tips" }, ...d.tips.map((t) => el("li", {}, t))) : null,
      el("p", { class: "ui-hint" }, "* asked for in the requirements. " + d.note));
  }
  async function scoreTab() {
    const pick = await picker();
    const out = el("div", { class: "ui-stack" });
    const masters = await api("/api/resumes/overview").catch(() => ({ masters: [] }));
    const which = el("select", { class: "ui-input", "aria-label": "Resume to score" }, el("option", { value: "" }, "My base resume (every fact)"),
      ...masters.masters.map((m) => el("option", { value: m.id }, `${m.label} (master)`)));
    const go = ui.button({ label: "Score it", icon: "check", variant: "primary", onClick: () => ui.busy(go, (async () => {
      try { out.replaceChildren(scoreView(await api("/api/ats/score", { method: "POST", json: { ...pick.job(), ...(which.value ? { master_id: which.value } : {}) } }))); } catch (e) { fail(e); }
    })()) });
    return [el("p", { class: "ui-hint" }, "Scores a posting against a resume the way an applicant tracking system does: keywords, job title, years, education, location. Plain code on this laptop; no AI."),
      pick.node, field("Resume", which), go, out];
  }

  // ---- 2. how an ATS reads your resume ----
  function readView(d) {
    const f = d.fields;
    const row = (k, v) => el("div", { class: "ats-row" }, el("span", { class: "k" }, k), el("span", { class: "v" }, v || "(not found)"));
    return el("div", { class: "ui-card ui-stack ats-card" },
      el("h3", { class: "ats-title" }, `Readable: ${d.readable}/100 (${d.label})`), el("span", { class: "ats-sub" }, `${d.format.toUpperCase()}, ${(d.size / 1024).toFixed(0)} KB`),
      ...(d.traps.length ? d.traps.map((t) => el("div", { class: "ats-trap" }, el("div", {}, ui.badge(t.severity, SEV[t.severity]), " ", el("b", {}, t.title)),
        el("p", { class: "ats-sub" }, t.detail), el("p", { class: "ats-fix" }, "Fix: " + t.fix))) : [el("p", { class: "ui-hint" }, "No format problems found.")]),
      ui.eyebrow("", "What an ATS pulls out"),
      row("Name", f.name), row("Email", f.email), row("Phone", f.phone), row("Links", Object.values(f.links || {}).join(", ")),
      row("Job titles", f.titles.join("; ")), row("Companies", f.companies.join("; ")), row("Dates", f.dates.join("; ")),
      row("Skills", f.skills.join(", ")), row("Education", f.education.join("; ")), row("Sections found", f.sections.join(", ")),
      ui.eyebrow("", "The text as an ATS sees it"), el("pre", { class: "ats-pre" }, d.ats_view),
      el("p", { class: "ui-hint" }, d.note + (d.pdf_supported ? "" : " PDF reading needs pypdf.")));
  }
  async function readTab() {
    const out = el("div", { class: "ui-stack" });
    const file = el("input", { type: "file", accept: ".pdf,.docx,.txt,.md,text/plain,application/pdf", class: "ui-input", "aria-label": "Resume file" });
    const pasted = area(6, { placeholder: "Or paste the text of your resume", maxlength: "60000", "aria-label": "Resume text" });
    const run = async (payload) => { try { out.replaceChildren(readView(await api("/api/ats/readcheck", { method: "POST", json: payload }))); } catch (e) { fail(e); } };
    const go = ui.button({ label: "Check this", icon: "check", variant: "primary", onClick: () => ui.busy(go, (async () => {
      if (file.files[0]) return run({ filename: file.files[0].name, data_b64: await toBase64(file.files[0]) });
      if (pasted.value.trim()) return run({ text: pasted.value });
      return run({});
    })()) });
    return [el("p", { class: "ui-hint" }, "See what an applicant tracking system would read from your resume file, and the layout traps that break it. The file is read on this laptop and not kept. With no file and no text, your saved resume text is checked (layout traps need the real file)."),
      field("Resume file (PDF, DOCX or text)", file), pasted, go, out];
  }

  // ---- 3. master resumes ----
  async function mastersTab() {
    const ov = await api("/api/resumes/overview");
    const make = () => {
      const fam = el("select", { class: "ui-input", "aria-label": "Role family" }, ...ov.families.map((f) => el("option", { value: f.key }, f.name)), el("option", { value: "" }, "Custom"));
      const name = text("", { placeholder: "Name (default: the family)", maxlength: "40" });
      const focus = text("", { placeholder: "Custom: focus words, comma separated" });
      ui.sheet({ title: "New master resume", class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" }, field("Role family", fam), field("Name", name), field("Focus words", focus,
        "Only steers which of YOUR facts come first. A word you have not confirmed is never added."),
      ), actions: [{ label: "Create", variant: "primary", icon: "check", onClick: async () => {
        try { await api("/api/resumes/masters", { method: "POST", json: { family: fam.value, name: name.value, focus: focus.value } }); ui.toast("Created.", { kind: "good" }); show("masters"); return true; } catch (e) { fail(e); return false; }
      } }] });
    };
    const card = (m) => el("article", { class: "ui-card ui-stack ats-card" }, el("h3", { class: "ats-title" }, m.label), el("span", { class: "ats-sub" }, `${m.name} · ${m.versions.length} version(s)`),
      m.stale ? el("p", { class: "ats-warn" }, "Your facts changed since this version. Rebuild to pick them up.") : null,
      el("div", { class: "ats-actions" },
        ui.button({ label: "View", variant: "tonal", size: "sm", onClick: async () => {
          try { const v = await api(`/api/resumes/masters/${m.id}/versions/${m.current}`); ui.sheet({ title: v.label, class: "st-sheet", content: el("pre", { class: "ats-pre" }, v.text), actions: [{ label: "Copy", icon: "copy", variant: "primary", onClick: () => { copy(v.text); return false; } }] }); } catch (e) { fail(e); }
        } }),
        ui.button({ label: "Rebuild", icon: "refresh", variant: "tonal", size: "sm", onClick: async () => {
          try { const r = await api(`/api/resumes/masters/${m.id}/rebuild`, { method: "POST", json: {} }); ui.toast(r.changed ? `Now ${r.label}.` : "Nothing changed.", { kind: "good" }); show("masters"); } catch (e) { fail(e); }
        } }),
        ui.button({ label: "Delete", variant: "ghost", size: "sm", onClick: async () => {
          if (await ui.confirm({ title: "Delete this master?", message: "Tailored resumes already made keep their label.", confirmLabel: "Delete", danger: true })) {
            try { await api(`/api/resumes/masters/${m.id}`, { method: "DELETE" }); show("masters"); } catch (e) { fail(e); }
          }
        } })));
    return [el("p", { class: "ui-hint" }, "One master resume per kind of role, built only from your confirmed facts. Each tailored resume records which master and version it came from, so the Job tracker can show which version gets replies."),
      ui.button({ label: "New master resume", icon: "plus", variant: "primary", onClick: make }), ...(ov.masters.length ? ov.masters.map(card) : [el("p", { class: "ui-hint" }, ov.facts ? "No master resumes yet." : "Add your resume in Apps, Opportunities, Profile first.")])];
  }

  // ---- 4. batch tailoring queue ----
  async function queueTab() {
    const [ov, aq] = await Promise.all([api("/api/resumes/queue"), loadPostings()]);
    const review = async (id) => {
      let r;
      try { r = await api(`/api/resumes/queue/${id}`); } catch (e) { fail(e); return; }
      const cover = area(10, { "aria-label": "Cover note" }); cover.value = r.cover_text || ""; cover.readOnly = true;
      const act = (path, msg) => async () => { try { const o = await api(`/api/resumes/queue/${id}/${path}`, { method: "POST", json: {} }); ui.toast(o.message || msg, { kind: "good" }); show("queue"); return true; } catch (e) { fail(e); return false; } };
      ui.sheet({ title: `${r.job.title}${r.job.company ? " · " + r.job.company : ""}`, class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
        r.ats ? el("p", { class: "ats-sub" }, `ATS match ${r.ats.score}/100 (${r.ats.label})${r.master ? " · from " + r.master.label : ""}`) : null,
        r.variant?.blocked ? el("p", { class: "ats-warn" }, r.variant.blockers.join(" ")) : null,
        ui.eyebrow("", "Cover note"), cover, ui.eyebrow("", "Resume"), el("pre", { class: "ats-pre" }, r.resume_text || ""),
        el("p", { class: "ui-hint" }, "Accept only saves the cover note into the application's draft. Nothing is sent. To change a sentence, use Tailor.")),
      actions: [{ label: "Accept", icon: "check", variant: "primary", onClick: act("accept", "Accepted.") }, { label: "Discard", variant: "ghost", onClick: act("discard", "Discarded.") }] });
    };
    const rows = ov.queue.map((q) => el("article", { class: "ui-card ui-stack ats-card" }, el("h3", { class: "ats-title" }, q.title), el("span", { class: "ats-sub" }, q.company || ""),
      el("div", { class: "ats-actions" }, ui.badge(q.state, q.state === "ready" ? "good" : q.state === "failed" ? "bad" : q.state === "accepted" ? "accent" : ""), q.ats ? ui.badge(`ATS ${q.ats.score}`, TONE[q.ats.label]) : null,
        q.master ? ui.badge(q.master.label) : null, q.blocked ? ui.badge("needs a fix", "warn") : null,
        q.state === "ready" ? ui.button({ label: "Review", variant: "primary", size: "sm", onClick: () => review(q.id) }) : null),
      q.error ? el("p", { class: "ats-warn" }, q.error) : null));
    const checks = aq.map((i) => { const c = el("input", { type: "checkbox", value: i.id }); return el("label", { class: "ats-pick" }, c, el("span", {}, `${i.title}${i.company ? " · " + i.company : ""}`.slice(0, 70))); });
    const add = ui.button({ label: "Prepare the ticked ones", icon: "wand", variant: "primary", onClick: () => ui.busy(add, (async () => {
      const ids = checks.map((l) => l.firstChild).filter((c) => c.checked).map((c) => c.value);
      if (!ids.length) return ui.toast("Tick one or more postings.", { kind: "warn" });
      try { const r = await api("/api/resumes/queue", { method: "POST", json: { application_ids: ids } }); ui.toast(`${r.added.length} queued.` + (r.skipped.length ? ` ${r.skipped.length} skipped.` : ""), { kind: "good" }); show("queue"); } catch (e) { fail(e); }
    })()) });
    return [el("p", { class: "ui-hint" }, `The hub prepares a tailored resume, a cover note and an ATS score for each posting you tick, one at a time in the background (up to ${ov.max_pending} waiting). You review them here one by one: accept, or discard. Nothing is sent. Tap Refresh to see progress.`),
      el("div", { class: "ats-actions" }, ui.button({ label: "Refresh", icon: "refresh", variant: "tonal", size: "sm", onClick: () => show("queue") }),
        ov.queue.some((q) => ["accepted", "discarded", "failed"].includes(q.state)) ? ui.button({ label: "Clear finished", variant: "ghost", size: "sm", onClick: async () => { try { await api("/api/resumes/queue/clear", { method: "POST", json: {} }); show("queue"); } catch (e) { fail(e); } } }) : null),
      ...rows, ui.eyebrow("", "Pick postings"), el("div", { class: "ats-picks" }, ...(checks.length ? checks : [el("p", { class: "ui-hint" }, "No postings in your Applications queue yet.")])), add];
  }

  // ---- 5. application packet ----
  function packetView(p, again) {
    const link = (href, label) => safeHref(href) ? el("a", { class: "ui-btn ui-btn--ghost ui-btn--sm", href, target: "_blank", rel: "noopener noreferrer" }, label) : null;
    const dl = p.downloads.map((d) => el("a", { class: "ui-btn ui-btn--ghost ui-btn--sm", href: d.href, download: "" }, d.label));
    const queueBtn = ui.button({ label: p.can_queue ? "Put in the application queue" : "In the queue", icon: "send", variant: "primary", disabled: !p.can_queue, onClick: () => ui.busy(queueBtn, (async () => {
      try { const r = await api(`/api/packet/${p.id}/queue`, { method: "POST", json: {} }); ui.toast(r.message, { kind: r.blocked ? "warn" : "good" }); again(r); } catch (e) { fail(e); }
    })()) });
    return el("div", { class: "ui-stack ui-stack--lg" },
      el("div", { class: "ui-card ui-stack ats-card" }, el("h3", { class: "ats-title" }, `${p.job.title}${p.job.company ? " · " + p.job.company : ""}`),
        el("div", { class: "ats-actions" }, ui.badge(`ATS ${p.ats.score}`, TONE[p.ats.label]), ui.badge(`fit ${p.fit.label}`), p.resume.version ? ui.badge(p.resume.version) : null, p.blocked ? ui.badge("needs a fix", "warn") : null),
        el("p", { class: "ats-sub" }, p.fit.summary), p.blocked ? el("p", { class: "ats-warn" }, p.blockers.join(" ")) : null,
        ...p.fit.flags.map((f) => el("p", { class: "ats-warn" }, f.label)),
        el("div", { class: "ats-actions" }, queueBtn, ui.button({ label: "Copy everything", icon: "copy", variant: "tonal", onClick: async () => { try { copy(await (await fetch(`/api/packet/${p.id}/text`, { credentials: "same-origin", headers: { "X-Hub": "1" } })).text()); } catch (e) { fail(e); } } })),
        el("p", { class: "ui-hint" }, p.note)),
      scoreView({ ...p.ats, job: p.job, resume: p.resume.version || "tailored resume", note: "" }),
      el("div", { class: "ui-card ui-stack ats-card" }, ui.eyebrow("", "Cover note"), el("pre", { class: "ats-pre" }, p.cover.text)),
      el("div", { class: "ui-card ui-stack ats-card" }, ui.eyebrow("", "Resume"), el("pre", { class: "ats-pre" }, p.resume.text)),
      el("div", { class: "ui-card ui-stack ats-card" }, ui.eyebrow("", "Answers for the form"),
        ...p.answers.map((a) => el("div", { class: "ats-ans" }, el("b", {}, a.question), el("p", { class: a.usable ? "" : "ats-sub" }, a.answer || a.reason || "Your answer needed."),
          a.owner_only ? el("span", { class: "ats-sub" }, "Only you answer this one.") : a.usable ? null : el("span", { class: "ats-sub" }, a.reason)))),
      el("div", { class: "ui-card ui-stack ats-card" }, ui.eyebrow("", "The company, from the posting"), el("p", {}, p.company.text || "None given."), el("p", { class: "ats-sub" }, p.company.note),
        ui.eyebrow("", "Pay in the posting"), ...(p.pay.found ? p.pay.lines.map((l) => el("p", {}, l)) : [el("p", { class: "ats-sub" }, p.pay.note)]),
        ui.eyebrow("", "Links"), el("div", { class: "ats-actions" }, link(p.links.posting, "Posting"), ...p.links.yours.slice(0, 4).map((u, i) => link(u, `Yours ${i + 1}`)), ...dl)));
  }
  async function packetTab() {
    const pick = await picker();
    const qs = area(3, { placeholder: "The form's questions, one per line (optional)", maxlength: "2000", "aria-label": "Form questions" });
    const out = el("div", { class: "ui-stack ui-stack--lg" });
    const go = ui.button({ label: "Prepare the packet", icon: "wand", variant: "primary", onClick: () => ui.busy(go, (async () => {
      try {
        const p = await api("/api/packet/prepare", { method: "POST", json: { ...pick.job(), questions: qs.value.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 20) } });
        const draw = (q) => out.replaceChildren(packetView(q.id ? q : p, (r) => api(`/api/packet/${p.id}`).then(draw).catch(fail)));
        draw(p);
      } catch (e) { fail(e); }
    })()) });
    return [el("p", { class: "ui-hint" }, "Everything for one posting on one page: tailored resume, cover note, ATS score, answers, company summary, pay and links. You review it once, then put it in your application queue as a draft or copy it. It never submits anything."),
      pick.node, field("Form questions", qs), go, out];
  }

  const TABS = { score: scoreTab, read: readTab, masters: mastersTab, queue: queueTab, packet: packetTab };
  async function show(which) {
    tab = which;
    paint();
    body.replaceChildren(el("p", { class: "ui-hint" }, "Loading…"));
    try { body.replaceChildren(...(await TABS[tab]())); } catch (e) { body.replaceChildren(el("p", { class: "ui-hint" }, e?.message || "Could not load.")); }
    ui.renumberEyebrows(root);
  }
  function paint() {
    const seg = ui.segmented({ label: "ATS sections", value: tab, onChange: (v) => show(v),
      options: [{ value: "score", label: "Match" }, { value: "read", label: "Check" }, { value: "masters", label: "Masters" }, { value: "queue", label: "Queue" }, { value: "packet", label: "Packet" }] });
    root.replaceChildren(el("h2", { class: "ui-title" }, "ATS and packets"), seg, body);
  }
  show("score");
}
