// Opportunities: ranked finds (jobs, gigs, LLM-training tasks), the sources the finder watches, and your private
// profile (resume, skills, standard answers). Routes: docs/api/opps.md. Everything fetched from the web is untrusted:
// it is only ever shown as text (textContent), links open in a new tab with rel="noopener noreferrer".
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import * as tg from "./triagebadges.js";

const CSS = "/css/opps.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const KIND_NAME = { "llm-task": "AI tasks", freelance: "Freelance", job: "Jobs", community: "Community" };
const HOW_NAME = { rss: "RSS feed", api: "Public API", html: "Public page", "email-alert": "Email alerts", manual: "Paste links" };
const STATE_TONE = { ok: "good", manual_check: "warn", error: "bad" };
const STATE_NAME = { ok: "Worked", manual_check: "Needs manual check", error: "Failed, will retry" };
const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);

function b64(buffer) {
  let out = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(out);
}

function field(label, control, hint) {
  return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "op ui-stack ui-stack--lg" });
  container.append(root);
  let tab = "finds", finds = null, sources = null, profile = null, status = null, error = "", running = false, vf = "", counts = {};

  async function loadFinds() {
    try { const r = await api("/api/opps/finds?limit=200" + (vf ? `&verdict=${vf}` : "")); finds = r.finds || []; status = r; counts = r.triage_counts || {}; error = ""; }
    catch (e) { error = e.message; }
  }
  async function loadSources() {
    try { sources = (await api("/api/opps/sources")).sources || []; }
    catch (e) { error = e.message; }
  }
  async function loadProfile() {
    try { profile = await api("/api/profile"); } catch (e) { error = e.message; }
  }
  async function refresh() {
    await Promise.all([loadFinds(), loadSources(), loadProfile()]);
    paint();
  }

  // ---- Finds ----
  function flagBadges(f) {
    return (f.flags || []).map((x) => ui.badge(x.label, x.severity === "high" ? "bad" : x.severity === "medium" ? "warn" : undefined));
  }

  function findCard(f) {
    const href = safeHref(f.url);
    const title = href
      ? el("a", { href, target: "_blank", rel: "noopener noreferrer", class: "op-title" }, f.title)
      : el("span", { class: "op-title" }, f.title);
    const actions = el("div", { class: "ui-row op-actions" });
    if (f.status !== "blocked" && f.status !== "queued" && f.status !== "dismissed") {
      actions.append(ui.button({ label: "Send to queue", icon: "send", size: "sm", variant: "tonal", onClick: async (e) => {
        await ui.busy(e.currentTarget, async () => {
          try { await api(`/api/opps/finds/${f.id}/queue`, { method: "POST" }); ui.toast("Added to the ready-to-send queue.", { kind: "good" }); await loadFinds(); paint(); }
          catch (err) { fail(err); }
        });
      } }));
    }
    if (status?.triage_ai && f.triage && !f.triage.ai) {
      actions.append(ui.button({ label: "Ask AI", icon: "sparkles", size: "sm", variant: "ghost", onClick: async (e) => {
        await ui.busy(e.currentTarget, async () => {
          try { await api(`/api/triage/refine/${f.id}`, { method: "POST" }); await loadFinds(); paint(); } catch (err) { fail(err); }
        });
      } }));
    }
    if (f.status !== "dismissed") {
      actions.append(ui.button({ label: "Dismiss", icon: "x", size: "sm", variant: "ghost", onClick: async () => {
        try { await api(`/api/opps/finds/${f.id}/dismiss`, { method: "POST" }); await loadFinds(); paint(); } catch (err) { fail(err); }
      } }));
    }
    const tone = f.status === "blocked" ? "bad" : f.status === "queued" ? "good" : f.score >= 70 ? "good" : f.score >= 40 ? "accent" : undefined;
    return el("li", { class: `ui-card op-find op-find--${f.status}` },
      el("div", { class: "op-find__head" },
        el("span", { class: "op-score ui-num", "data-tone": tone || "" }, String(f.score)),
        el("div", { class: "op-find__main" }, title,
          el("div", { class: "ui-hint" }, [f.company, f.source, ui.relTime ? ui.relTime(f.found_at) : ""].filter(Boolean).join(" · ")))),
      f.status === "blocked" ? ui.notice("Held back, not sent to the queue: " + (f.blocked_reason || "looks like a scam"), { tone: "bad" })
        : el("p", { class: "op-reason" }, f.reason || ""),
      tg.verdictLine(f.triage),
      f.status === "filtered" ? el("p", { class: "ui-hint" }, "Your job preferences filtered this out of the normal list: " + (f.filtered_reason || "no match") + ". It is shown because of the filter you chose.") : null,
      tg.ghostWhy(f.triage), tg.aiLine(f.triage),
      el("div", { class: "ui-row op-flags" }, ui.badge(KIND_NAME[f.kind] || f.kind || "other"), f.status === "queued" ? ui.badge("In queue", "good") : null, ...tg.flagBadges(f.triage), ...flagBadges(f)),
      actions);
  }

  async function addLink(urlInput, noteInput, btn) {
    const url = urlInput.value.trim();
    if (!url) { ui.toast("Paste a link first.", { kind: "warn" }); return; }
    await ui.busy(btn, async () => {
      try {
        const r = await api("/api/opps/add", { method: "POST", json: { url, note: noteInput.value.trim() } });
        urlInput.value = ""; noteInput.value = "";
        ui.toast(r.read ? "Added." : "Added from your note (the page could not be read).", { kind: r.read ? "good" : "warn" });
        await loadFinds(); paint();
      } catch (e) { fail(e); }
    });
  }

  async function runNow(btn) {
    running = true;
    await ui.busy(btn, async () => {
      try {
        const r = await api("/api/opps/run", { method: "POST", json: {} });
        ui.toast(r.busy ? "A run is already in progress." : `Checked ${r.ran} source(s), ${r.new || 0} new.`, { kind: "good" });
      } catch (e) { fail(e); }
    });
    running = false;
    await refresh();
  }

  async function setAuto(on) {
    try { await api("/api/settings", { method: "PATCH", json: { opps_enabled: on } }); await loadFinds(); paint(); }
    catch (e) { fail(e); await loadFinds(); paint(); }
  }

  function findsTab() {
    const parts = [];
    const url = el("input", { class: "ui-input", type: "url", inputmode: "url", maxlength: "500", "aria-label": "Link to add", placeholder: "Paste a link to a job or gig" });
    const note = el("input", { class: "ui-input", type: "text", maxlength: "300", "aria-label": "Note", placeholder: "Note (optional), e.g. task from Outlier" });
    const add = ui.button({ label: "Add a link", icon: "plus", variant: "primary", onClick: () => addLink(url, note, add) });
    const run = ui.button({ label: "Run now", icon: "refresh", variant: "tonal", onClick: () => runNow(run) });
    const auto = ui.switchControl({ checked: !!status?.enabled, label: "Check automatically", onChange: setAuto });
    parts.push(el("section", { class: "ui-card ui-hud ui-stack op-tools" }, ui.eyebrow("01", "Find"),
      el("div", { class: "op-auto" }, auto, el("span", {}, status?.enabled ? `Checking every ${status.interval_min} minutes` : "Automatic checking is off")),
      el("div", { class: "ui-row" }, run, running ? el("span", { class: "ui-hint" }, "Working…") : null),
      field("Add a link", url), field("Note", note), add));
    if (status && !status.queue) parts.push(ui.notice("The ready-to-send queue is not installed on this hub yet, so finds are listed here only.", { tone: "warn" }));
    const list = (finds || []).filter((f) => f.status !== "dismissed");
    // With a go / no-go filter on, every find with that label is listed whatever its status: nothing is out of sight.
    const good = vf ? list : list.filter((f) => f.status !== "blocked" && f.status !== "low");
    const low = vf ? [] : list.filter((f) => f.status === "low");
    const blocked = vf ? [] : list.filter((f) => f.status === "blocked");
    tg.loadCss();
    parts.push(ui.eyebrow("02", `Ranked finds (${good.length})`));
    parts.push(tg.chipRow({ value: vf, counts, onChange: async (v) => { vf = v; await loadFinds(); paint(); } }));
    parts.push(good.length ? el("ul", { class: "op-list" }, ...good.map(findCard))
      : ui.emptyState({ icon: "search", title: "No finds yet", body: "Turn on automatic checking, press Run now, or paste a link.", compact: true }));
    if (low.length) parts.push(el("details", { class: "op-more" }, el("summary", {}, `Below your score threshold (${low.length})`), el("ul", { class: "op-list" }, ...low.map(findCard))));
    if (blocked.length) parts.push(el("details", { class: "op-more", open: true }, el("summary", {}, `Blocked: look like scams (${blocked.length})`), el("ul", { class: "op-list" }, ...blocked.map(findCard))));
    const rows = (sources || []).filter((s) => s.status && s.status.state);
    if (rows.length) {
      parts.push(ui.eyebrow("03", "Last run per source"));
      parts.push(el("ul", { class: "op-runs" }, ...rows.map((s) => el("li", { class: "op-run" },
        ui.badge(STATE_NAME[s.status.state] || s.status.state, STATE_TONE[s.status.state]),
        el("span", { class: "op-run__name" }, s.name),
        el("span", { class: "ui-hint" }, [s.status.at ? ui.relTime(s.status.at) : "", s.status.state === "ok" ? `${s.status.new} new` : s.status.error].filter(Boolean).join(" · "))))));
    }
    return parts;
  }

  // ---- Sources ----
  function sourceForm(existing) {
    const name = el("input", { class: "ui-input", maxlength: "80", "aria-label": "Name", placeholder: "Name" });
    const url = el("input", { class: "ui-input", type: "url", inputmode: "url", maxlength: "500", "aria-label": "Address", placeholder: "https://…" });
    const kind = el("select", { class: "ui-input", "aria-label": "Kind" }, ...Object.entries(KIND_NAME).map(([v, l]) => el("option", { value: v }, l)));
    const how = el("select", { class: "ui-input", "aria-label": "How to read it" }, ...Object.entries(HOW_NAME).map(([v, l]) => el("option", { value: v }, l)));
    const note = el("input", { class: "ui-input", maxlength: "300", "aria-label": "Note", placeholder: "Note (optional)" });
    if (existing) { name.value = existing.name; url.value = existing.url || ""; kind.value = existing.kind; how.value = existing.how; note.value = existing.note || ""; }
    const save = ui.button({ label: existing ? "Save" : "Add site", icon: existing ? "check" : "plus", variant: "primary", onClick: async () => {
      await ui.busy(save, async () => {
        const body = { name: name.value, url: url.value.trim(), kind: kind.value, how: how.value, note: note.value };
        try {
          if (existing) await api(`/api/opps/sources/${existing.id}`, { method: "PUT", json: body });
          else await api("/api/opps/sources", { method: "POST", json: body });
          editing = null; await loadSources(); paint();
        } catch (e) { fail(e); }
      });
    } });
    const cancel = existing ? ui.button({ label: "Cancel", variant: "ghost", onClick: () => { editing = null; paint(); } }) : null;
    return el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow(existing ? "02" : "01", existing ? "Edit site" : "Add site"),
      field("Name", name), field("Address", url, "Public feeds, APIs and pages only. Pages that need a sign-in are never read."),
      el("div", { class: "op-two" }, field("Kind", kind), field("How", how)), field("Note", note), el("div", { class: "ui-row" }, save, cancel));
  }
  let editing = null;

  function sourceRow(s) {
    const st = s.status || {};
    const sw = ui.switchControl({ checked: !!s.enabled, label: `Use ${s.name}`, onChange: async (on) => {
      try { await api(`/api/opps/sources/${s.id}`, { method: "PUT", json: { enabled: on } }); await loadSources(); paint(); } catch (e) { fail(e); await loadSources(); paint(); }
    } });
    const href = safeHref(s.url);
    return el("li", { class: "op-src" },
      el("div", { class: "op-src__main" },
        el("div", { class: "ui-row" }, el("strong", { class: "op-src__name" }, s.name), ui.badge(HOW_NAME[s.how] || s.how), ui.badge(KIND_NAME[s.kind] || s.kind),
          st.state ? ui.badge(STATE_NAME[st.state] || st.state, STATE_TONE[st.state]) : null),
        href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", class: "op-link" }, s.url) : el("span", { class: "ui-hint" }, "No address yet. Edit it to add the link."),
        s.note ? el("p", { class: "ui-hint op-src__note" }, s.note) : null,
        st.error ? el("p", { class: "ui-hint" }, st.error) : null),
      el("div", { class: "op-src__ctl" }, sw,
        ui.button({ icon: "edit", iconOnly: true, variant: "ghost", "aria-label": `Edit ${s.name}`, onClick: () => { editing = s.id; paint(); } }),
        ui.button({ icon: "trash", iconOnly: true, variant: "ghost", "aria-label": `Delete ${s.name}`, onClick: async () => {
          if (!(await ui.confirm({ title: `Delete ${s.name}?`, message: "You can bring it back with Reset to starter list.", confirmLabel: "Delete", danger: true }))) return;
          try { await api(`/api/opps/sources/${s.id}`, { method: "DELETE" }); await loadSources(); paint(); } catch (e) { fail(e); }
        } })));
  }

  function sourcesTab() {
    const rows = sources || [];
    const edit = editing && rows.find((s) => s.id === editing);
    const parts = [edit ? sourceForm(edit) : sourceForm(null)];
    parts.push(ui.eyebrow("02", `Sources (${rows.length})`));
    parts.push(el("ul", { class: "ui-card op-srcs" }, ...rows.map(sourceRow)));
    parts.push(el("div", { class: "ui-row" }, ui.button({ label: "Reset to starter list", icon: "history", variant: "secondary", onClick: async () => {
      if (!(await ui.confirm({ title: "Reset the source list?", message: "Your added or edited sites are replaced by the starter list.", confirmLabel: "Reset", danger: true }))) return;
      try { await api("/api/opps/sources/reset", { method: "POST" }); await loadSources(); paint(); } catch (e) { fail(e); }
    } })));
    parts.push(el("p", { class: "ui-hint" }, "Only public pages and feeds are read, politely and respecting robots.txt. Sites that block automated reading are marked for a manual check; paste their links under Finds instead."));
    return parts;
  }

  // ---- Profile ----
  function profileTab() {
    const p = profile || {};
    const parts = [];
    const manual = new Set(p.manual_fields || []);
    const file = el("input", { type: "file", accept: ".pdf,.docx,.txt,.md,application/pdf", "aria-label": "Resume file", class: "op-file" });
    const paste = el("textarea", { class: "ui-input op-paste", rows: "5", maxlength: "60000", "aria-label": "Resume text", placeholder: "…or paste the text of your resume" });
    const over = el("input", { type: "checkbox", "aria-label": "Overwrite my edits" });
    const model = el("input", { type: "checkbox", "aria-label": "Use the assistant" });
    const send = ui.button({ label: p.resume_chars ? "Replace resume" : "Upload resume", icon: "upload", variant: "primary", onClick: async () => {
      await ui.busy(send, async () => {
        const body = { overwrite: over.checked, use_model: model.checked };
        try {
          if (file.files[0]) { body.filename = file.files[0].name; body.data_b64 = b64(await file.files[0].arrayBuffer()); }
          else if (paste.value.trim()) body.text = paste.value;
          else { ui.toast("Choose a file or paste the text first.", { kind: "warn" }); return; }
          const r = await api("/api/profile/resume", { method: "POST", json: body });
          ui.toast(r.kept?.length ? `Resume read. Kept your edits to: ${r.kept.join(", ")}.` : "Resume read.", { kind: "good" });
          paste.value = ""; file.value = ""; await loadProfile(); paint();
        } catch (e) { fail(e); }
      });
    } });
    parts.push(el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("01", "Resume"),
      el("p", { class: "ui-hint" }, p.resume_chars ? `A resume is stored (${p.resume_chars} characters). It stays on this laptop.` : "No resume yet. PDF, Word, text or Markdown."),
      file, paste,
      el("label", { class: "op-check" }, over, el("span", {}, "Overwrite fields I edited by hand")),
      el("label", { class: "op-check" }, model, el("span", {}, "Let the assistant improve the fields (your text is redacted first)")),
      send));

    const name = el("input", { class: "ui-input", maxlength: "160", "aria-label": "Name" }); name.value = p.name || "";
    const headline = el("input", { class: "ui-input", maxlength: "160", "aria-label": "Headline" }); headline.value = p.headline || "";
    const summary = el("textarea", { class: "ui-input", rows: "4", maxlength: "3000", "aria-label": "Summary" }); summary.value = p.summary || "";
    const skills = el("textarea", { class: "ui-input", rows: "3", "aria-label": "Skills" }); skills.value = (p.skills || []).join(", ");
    const education = el("textarea", { class: "ui-input", rows: "3", maxlength: "2000", "aria-label": "Education" }); education.value = p.education || "";
    const exp = el("textarea", { class: "ui-input op-exp", rows: "8", "aria-label": "Experience" });
    exp.value = (p.experience || []).map((e) => `${e.title} | ${e.company} | ${e.period}\n${(e.bullets || []).map((b) => "- " + b).join("\n")}`).join("\n\n");
    const links = el("textarea", { class: "ui-input", rows: "3", "aria-label": "Links" });
    links.value = Object.entries(p.links || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
    const mark = (k) => (manual.has(k) ? " (edited by you)" : "");
    const saveFields = ui.button({ label: "Save profile", icon: "check", variant: "primary", onClick: async () => {
      await ui.busy(saveFields, async () => {
        const experience = exp.value.split(/\n{2,}/).map((blk) => {
          const lines = blk.split("\n").map((l) => l.trim()).filter(Boolean);
          const [title = "", company = "", period = ""] = (lines.shift() || "").split("|").map((x) => x.trim());
          return { title, company, period, bullets: lines.map((l) => l.replace(/^[-*•]\s*/, "")) };
        }).filter((x) => x.title || x.company);
        const linkMap = {};
        for (const line of links.value.split("\n")) { const i = line.indexOf(":"); if (i > 0) linkMap[line.slice(0, i).trim()] = line.slice(i + 1).trim(); }
        try {
          profile = await api("/api/profile", { method: "PUT", json: { name: name.value, headline: headline.value, summary: summary.value,
            skills: skills.value, education: education.value, experience, links: linkMap } });
          ui.toast("Profile saved.", { kind: "good" }); paint();
        } catch (e) { fail(e); }
      });
    } });
    parts.push(el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("02", "Your details"),
      field("Name" + mark("name"), name), field("Headline" + mark("headline"), headline), field("Summary" + mark("summary"), summary),
      field("Skills" + mark("skills"), skills, "Separate with commas. Used to rank finds."),
      field("Experience" + mark("experience"), exp, "One block per job: Title | Company | Period, then lines starting with -"),
      field("Education" + mark("education"), education), field("Links" + mark("links"), links, "One per line, like github: https://…"), saveFields));

    const answers = { ...(p.standard_answers || {}) };
    const list = el("div", { class: "ui-stack" });
    const keyIn = el("input", { class: "ui-input", maxlength: "80", "aria-label": "Question", placeholder: "e.g. notice period" });
    const valIn = el("input", { class: "ui-input", maxlength: "1500", "aria-label": "Answer", placeholder: "e.g. 2 weeks" });
    async function saveAnswers(next, okMessage) {
      try { profile = await api("/api/profile", { method: "PUT", json: { standard_answers: next } }); ui.toast(okMessage, { kind: "good" }); paint(); } catch (e) { fail(e); }
    }
    for (const [k, v] of Object.entries(answers)) {
      const input = el("input", { class: "ui-input", maxlength: "1500", "aria-label": `Answer for ${k}` }); input.value = v;
      list.append(el("div", { class: "op-ans" }, el("span", { class: "op-ans__k" }, k), input,
        ui.button({ icon: "check", iconOnly: true, variant: "ghost", "aria-label": `Save ${k}`, onClick: () => saveAnswers({ ...answers, [k]: input.value }, "Saved.") }),
        ui.button({ icon: "trash", iconOnly: true, variant: "ghost", "aria-label": `Delete ${k}`, onClick: () => { const n = { ...answers }; delete n[k]; saveAnswers(n, "Removed."); } })));
    }
    const addAns = ui.button({ label: "Add answer", icon: "plus", variant: "tonal", onClick: () => {
      if (!keyIn.value.trim() || !valIn.value.trim()) { ui.toast("Write both the question and the answer.", { kind: "warn" }); return; }
      saveAnswers({ ...answers, [keyIn.value.trim()]: valIn.value.trim() }, "Saved.");
    } });
    parts.push(el("section", { class: "ui-card ui-hud ui-stack" }, ui.eyebrow("03", "Standard answers"),
      el("p", { class: "ui-hint" }, "Reused when you apply: notice period, expected rate, portfolio, why you are interested…"),
      list, keyIn, valIn, addAns));
    return parts;
  }

  function paint() {
    root.replaceChildren();
    const seg = ui.segmented({ label: "Opportunities", value: tab, options: [
      { value: "finds", label: "Finds" }, { value: "sources", label: "Sources" }, { value: "profile", label: "Profile" }],
      onChange: (v) => { tab = v; editing = null; paint(); } });
    root.append(seg);
    if (error) root.append(ui.notice(error, { tone: "bad" }));
    if (tab === "finds") root.append(...findsTab());
    else if (tab === "sources") root.append(...sourcesTab());
    else root.append(...profileTab());
  }

  paint();
  refresh();
  return { refresh, destroy() {} };
}
