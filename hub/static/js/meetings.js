// Meetings: record a meeting on the LAPTOP (consent first), read the notes, pick which action items go to your tasks.
// Routes: docs/api/meetings.md. Every string goes in with textContent (el()), never as markup.
// Exports init() for the screen and mountIndicator() for the always-visible "recording" pill (one line in main.js).
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";
import { subscribe } from "./poller.js";

const CSS = "/css/meetings.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const loadCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };
const clock = (s) => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m).padStart(2, "0")) + ":" + String(x).padStart(2, "0"); };
const when = (iso) => String(iso || "").slice(0, 16).replace("T", " ");
const OWNER = { me: ["You", "accent"], others: ["Others", "warn"], unknown: ["Unknown", ""] };
const STATUS = { recording: "Recording", processing: "Making notes", ready: "Ready", stopped: "Stopped", interrupted: "Interrupted", failed: "Failed" };

// ---- the persistent indicator -------------------------------------------------------------------------
const ind = { node: null, timer: null, base: 0, at: 0, go: null };

function paintIndicator(st) {
  if (!ind.node) return;
  if (!st || !st.recording) { ind.node.hidden = true; document.body.classList.remove("is-recording-meeting"); clearInterval(ind.timer); ind.timer = null; return; }
  if (!ind.timer) ind.timer = setInterval(() => { if (!document.hidden) tickIndicator(); }, 1000);   // only while recording
  ind.base = st.elapsed_s || 0; ind.at = Date.now();
  ind.node.hidden = false;
  document.body.classList.add("is-recording-meeting");
  tickIndicator();
}
function tickIndicator() {
  if (!ind.node || ind.node.hidden) return;
  ind.label.textContent = `Recording ${clock(ind.base + (Date.now() - ind.at) / 1000)}`;
}
export async function refreshIndicator() {
  try { paintIndicator(await api("/api/meetings/status")); } catch (_) { /* meetings not on this hub, or logged out */ }
}
export function mountIndicator(ctx) {
  if (ind.node) return;
  loadCss();
  ind.label = el("span", {}, "Recording");
  ind.node = el("button", { type: "button", class: "mt-ind", hidden: true, "aria-label": "A meeting is being recorded. Open Meetings.",
    onclick: () => ctx?.goto?.("meetings") }, el("span", { class: "mt-ind__dot", "aria-hidden": "true" }), ind.label);
  ind.node.setAttribute("role", "status");
  document.body.append(ind.node);
  subscribe((t) => { if (t.meeting) paintIndicator(t.meeting); });   // the shared poll; no timer of its own
}

// ---- the screen ----------------------------------------------------------------------------------------
export function init(container, ctx) {
  loadCss();
  const root = el("div", { class: "mt ui-stack ui-stack--lg" });
  container.append(root);
  let st = null, list = [], detail = null, loadError = "", timer = null, tick = null, editing = false, proposal = null, picks = {};

  const stopTimers = () => { clearTimeout(timer); timer = null; };
  const schedule = (ms) => { stopTimers(); timer = setTimeout(() => { if (root.isConnected) load(); }, ms); };

  async function load() {
    try {
      st = await api("/api/meetings/status");
      list = (await api("/api/meetings")).meetings || [];
      loadError = "";
      if (detail) {
        const fresh = await api(`/api/meetings/${detail.id}`).catch(() => null);
        if (!fresh) { detail = null; } else {
          const was = detail.status;
          detail = fresh;
          if (!editing && (was !== fresh.status || !proposal)) await loadProposal();
        }
      }
    } catch (error) { loadError = error instanceof ApiError && error.status === 404 ? "off" : (error.message || "Could not load."); }
    paint();
    refreshIndicator();
    schedule(st?.recording || detail?.status === "processing" || list.some((m) => m.status === "processing") ? 3000 : 20000);
  }

  async function loadProposal() {
    proposal = null;
    if (!detail || detail.status !== "ready") return;
    try {
      proposal = (await api(`/api/meetings/${detail.id}/proposal`)).items || [];
      for (const p of proposal) if (!(p.key in picks)) picks[p.key] = p.default && !p.already;
    } catch (_) { proposal = []; }
  }

  // ---- consent sheet ----
  async function openStart(prefillTitle = "", fromPrompt = false) {
    const title = el("input", { class: "ui-input", type: "text", maxlength: "120", placeholder: "Meeting title (the calendar event name)", "aria-label": "Meeting title", value: prefillTitle });
    title.value = prefillTitle;
    const tick1 = el("input", { type: "checkbox", id: "mt-told" });
    const tickRow = el("label", { class: "mt-tick", for: "mt-told" }, tick1, el("span", {}, "I have told participants that this meeting is being recorded."));
    const priv = ui.switchControl({ checked: false, label: "Keep the notes on this laptop only" });
    const warn = el("div", { class: "ui-stack" });
    const body = el("div", { class: "ui-stack" },
      ui.notice(st?.consent_text || "Recording meeting. Make sure everyone present knows.", { tone: "warn", icon: "mic" }),
      el("p", { class: "ui-hint" }, st?.legal || ""),
      title, tickRow,
      el("div", { class: "ui-row mt-switch" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Private notes"),
        el("span", { class: "ui-row__sub" }, "Only local models write the notes; nothing is sent to a cloud AI.")), priv),
      warn);
    const src = st?.sources || {};
    if (src.me && !src.me.ok) warn.append(ui.notice(`Microphone: ${src.me.why}`, { tone: "bad" }));
    if (src.others && !src.others.ok) warn.append(ui.notice(`Other participants will not be recorded. ${src.others.why}`, { tone: "warn" }));
    if (src.whisper && !src.whisper.ok) warn.append(ui.notice(src.whisper.why, { tone: "bad" }));
    let needed = true;
    const startBtn = { label: "Start recording", icon: "mic", variant: "primary", onClick: async () => {
      if (needed && !tick1.checked) { ui.toast("Tick that you have told the participants first.", { kind: "warn" }); return false; }
      try {
        st = await api("/api/meetings/start", { method: "POST", json: { title: title.value.trim(), confirm: true, told_participants: tick1.checked, private: priv.input.checked } });
        ui.toast("Recording. The other people should know.", { kind: "warn" });
        if (fromPrompt) api("/api/meetings/prompt/dismiss", { method: "POST" }).catch(() => {});
        load();
      } catch (error) { fail(error); return false; }
    } };
    const sheet = ui.sheet({ title: "Record this meeting?", content: body, actions: [startBtn, { label: "Cancel", variant: "secondary" }] });
    async function check() {
      try {
        const c = await api(`/api/meetings/consent?title=${encodeURIComponent(title.value.trim())}`);
        needed = c.needed; tickRow.hidden = !needed;
        if (!needed) tick1.checked = true;
      } catch (_) { needed = true; }
    }
    title.addEventListener("input", () => { clearTimeout(title._t); title._t = setTimeout(check, 250); });
    check();
    return sheet;
  }

  async function stopRecording() {
    try { await api("/api/meetings/stop", { method: "POST" }); ui.toast("Stopped. Making the notes now.", { kind: "good" }); } catch (error) { fail(error); }
    load();
  }

  async function setSetting(body, message) {
    try { st = await api("/api/meetings/settings", { method: "PUT", json: body }); if (message) ui.toast(message, { kind: "good" }); paint(); } catch (error) { fail(error); paint(); }
  }

  // ---- list screen ----
  function statusCard() {
    if (!st) return ui.skeleton("list", 1);
    if (!st.enabled) {
      return el("section", { class: "ui-card ui-card--accent mt-card ui-stack" }, ui.eyebrow("01", "Meetings"),
        el("p", { class: "mt-lead" }, "Record a meeting on this laptop and get notes, decisions and action items."),
        el("p", { class: "ui-hint" }, "Off until you turn it on. Audio is turned into text on this laptop and thrown away; only the text is kept."),
        ui.notice(st.legal, { tone: "warn" }),
        ui.button({ label: "Turn on meetings", icon: "check", variant: "primary", onClick: () => setSetting({ enabled: true }, "Meetings are on. Nothing records until you start it.") }));
    }
    if (st.recording) {
      const elapsed = el("strong", { class: "mt-elapsed" }, clock(st.elapsed_s));
      const base = st.elapsed_s || 0, t0 = Date.now();
      clearInterval(tick); tick = setInterval(() => { if (!elapsed.isConnected) { clearInterval(tick); return; } elapsed.textContent = clock(base + (Date.now() - t0) / 1000); }, 1000);
      return el("section", { class: "ui-card mt-card mt-card--rec ui-stack", role: "status" },
        el("div", { class: "mt-rec" }, el("span", { class: "mt-ind__dot", "aria-hidden": "true" }), el("span", {}, "Recording"), elapsed),
        el("p", { class: "mt-title" }, st.title),
        el("p", { class: "ui-hint" }, `Stops by itself after ${st.max_minutes} min. Tracks: ${(st.tracks || []).map((t) => (t === "me" ? "you" : "others")).join(" and ")}. ${st.words || 0} words so far.`),
        ...(st.warnings || []).map((w) => ui.notice(w, { tone: "warn" })),
        ui.button({ label: "Stop recording", icon: "stop", variant: "danger", block: true, onClick: stopRecording }));
    }
    clearInterval(tick);
    const parts = [ui.eyebrow("01", "Meetings")];
    if (st.panic) parts.push(ui.notice("The agent is paused (Panic). Recording is blocked until you resume.", { tone: "bad" }));
    if (st.prompt) {
      parts.push(el("div", { class: "mt-prompt" }, el("p", { class: "mt-lead" }, "Meeting starting: record it?"), el("p", { class: "ui-hint" }, st.prompt.title),
        el("div", { class: "mt-actions" },
          ui.button({ label: "Record it", icon: "mic", variant: "primary", onClick: () => openStart(st.prompt.title, true) }),
          ui.button({ label: "Not now", variant: "ghost", onClick: async () => { await api("/api/meetings/prompt/dismiss", { method: "POST" }).catch(() => {}); load(); } }))));
    }
    parts.push(ui.button({ label: "Record a meeting", icon: "mic", variant: st.prompt ? "secondary" : "primary", block: true, disabled: st.panic, onClick: () => openStart() }));
    return el("section", { class: "ui-card mt-card ui-stack" }, ...parts);
  }

  function settingsFold() {
    if (!st?.enabled) return null;
    const s = st.settings || {};
    const keep = ui.switchControl({ checked: !!s.keep_audio, label: "Keep the audio files", onChange: (v) => setSetting({ keep_audio: v }, v ? "Audio will be kept beside the transcripts." : "Audio is no longer kept.") });
    const model = ui.segmented({ options: (s.models || ["tiny", "base", "small", "medium"]).map((m) => ({ value: m, label: m })), value: s.model || "base", label: "Whisper model", onChange: (v) => setSetting({ model: v }) });
    const mins = el("input", { class: "ui-input mt-mins", type: "number", min: "1", max: "480", value: String(s.max_minutes || 120), "aria-label": "Stop after minutes" });
    mins.value = String(s.max_minutes || 120);
    mins.addEventListener("change", () => setSetting({ max_minutes: parseInt(mins.value, 10) || 120 }, "Saved."));
    return el("details", { class: "ui-card mt-card" }, el("summary", {}, "Settings"),
      el("div", { class: "ui-stack mt-settings" },
        el("div", { class: "ui-row mt-switch" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Keep audio files"),
          el("span", { class: "ui-row__sub" }, "Off: only text is stored. On: WAV files are saved beside the transcript.")), keep),
        s.keep_audio ? ui.notice("Audio files are kept on this laptop. Delete the meeting to remove them.", { tone: "warn" }) : null,
        el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Stop recording after (minutes)"), mins),
        el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Transcription model (smaller is faster)"), model),
        ui.button({ label: "Turn meetings off", variant: "ghost", onClick: () => setSetting({ enabled: false }, "Meetings are off.") })));
  }

  function listCard() {
    if (!st?.enabled && !list.length) return null;
    const rows = list.map((m) => ui.listRow({ icon: "note", tone: m.status === "ready" ? "accent" : "warn", title: m.title || "Meeting",
      sub: `${when(m.started)} - ${Math.round((m.duration_s || 0) / 60)} min - ${m.actions} actions${m.mine ? ` (${m.mine} yours)` : ""}`,
      meta: ui.badge(STATUS[m.status] || m.status, m.status === "ready" ? "good" : m.status === "failed" ? "bad" : ""), chevron: true,
      onClick: () => openDetail(m.id) }));
    return el("section", { class: "ui-stack" }, ui.eyebrow("02", "Past meetings"),
      rows.length ? el("div", { class: "ui-card ui-card--flat mt-list" }, ...rows) : ui.emptyState({ icon: "note", title: "No meetings yet", body: "When you record one, its notes appear here.", compact: true }));
  }

  // ---- meeting page ----
  async function openDetail(id) {
    try { detail = await api(`/api/meetings/${id}`); picks = {}; editing = false; await loadProposal(); paint(); window.scrollTo?.(0, 0); schedule(detail.status === "processing" ? 3000 : 20000); } catch (error) { fail(error); }
  }

  function bullets(title, rows) {
    if (!rows?.length) return null;
    return el("section", { class: "ui-stack mt-sec" }, el("h3", { class: "mt-h" }, title), el("ul", { class: "mt-ul" }, ...rows.map((r) => el("li", {}, r))));
  }

  function proposalCard() {
    if (!proposal) return null;
    const rows = proposal.map((p) => {
      const cb = el("input", { type: "checkbox", id: "mt-pick-" + p.key.replace(/[^a-z0-9]/gi, "_"), "aria-label": `Add: ${p.title}` });
      cb.checked = !!picks[p.key] && !p.already; cb.disabled = !!p.already;
      cb.addEventListener("change", () => { picks[p.key] = cb.checked; });
      const ttl = el("input", { class: "ui-input mt-ttl", type: "text", maxlength: "200", value: p.title, "aria-label": "Title", disabled: !!p.already });
      ttl.value = p.title;
      ttl.addEventListener("input", () => { p._title = ttl.value; });
      let due = null;
      if (p.type === "action") {
        due = el("input", { class: "ui-input mt-due", type: "date", value: p.due || "", "aria-label": "Due date", disabled: !!p.already });
        due.value = p.due || "";
        due.addEventListener("input", () => { p._due = due.value; });
      }
      return el("div", { class: "mt-pick" + (p.already ? " is-done" : "") }, cb,
        el("div", { class: "mt-pick__body" }, el("div", { class: "mt-pick__top" }, ui.badge(p.label, p.owner === "me" ? "accent" : p.owner === "others" ? "warn" : ""),
          p.already ? ui.badge("Added", "good") : null), ttl, due));
    });
    const toItems = (chosen) => chosen.map((p) => ({ key: p.key, title: p._title ?? p.title, ...(p.type === "action" ? { due: p._due ?? p.due } : {}) }));
    const send = async (all) => {
      const chosen = proposal.filter((p) => !p.already && (all || picks[p.key]));
      if (!chosen.length) { ui.toast("Tick at least one item first.", { kind: "warn" }); return; }
      try {
        const r = await api(`/api/meetings/${detail.id}/apply`, { method: "POST", json: { items: toItems(chosen) } });
        ui.toast(r.failed?.length ? `Added ${r.added.length}. ${r.failed[0].why}` : `Added ${r.added.length} to your tasks and memory.`, { kind: r.failed?.length ? "warn" : "good" });
        picks = {}; await openDetail(detail.id);
      } catch (error) { fail(error); }
    };
    const open = proposal.some((p) => !p.already);
    return el("section", { class: "ui-card ui-card--accent mt-card ui-stack" }, el("h3", { class: "mt-h" }, "Add to my tasks and memory"),
      el("p", { class: "ui-hint" }, "Nothing is saved until you tap. Edit a title or date first if you like."),
      ...rows,
      open ? el("div", { class: "mt-actions" }, ui.button({ label: "Add picked", icon: "check", variant: "primary", onClick: () => send(false) }),
        ui.button({ label: "Add all", variant: "secondary", onClick: () => send(true) })) : el("p", { class: "ui-hint" }, "Everything here has been added."));
  }

  function editForm() {
    const n = detail.notes || {};
    const area = (label, rows) => { const t = el("textarea", { class: "ui-input mt-area", rows: "4", "aria-label": label }); t.value = (rows || []).join("\n"); return t; };
    const f = { summary: area("Summary, one per line", n.summary), decisions: area("Decisions", n.decisions), risks: area("Risks", n.risks), questions: area("Questions", n.questions) };
    const acts = (n.actions || []).map((a) => ({ ...a }));
    const actBox = el("div", { class: "ui-stack" });
    const drawActs = () => actBox.replaceChildren(...acts.map((a, i) => {
      const t = el("input", { class: "ui-input", type: "text", maxlength: "240", value: a.text, "aria-label": "Action" }); t.value = a.text; t.addEventListener("input", () => { a.text = t.value; });
      const o = el("select", { class: "ui-input mt-sel", "aria-label": "Owner" }, ...["me", "others", "unknown"].map((v) => el("option", { value: v }, OWNER[v][0])));
      o.value = a.owner; o.addEventListener("change", () => { a.owner = o.value; });
      const w = el("input", { class: "ui-input mt-who", type: "text", maxlength: "40", placeholder: "Who", value: a.who || "", "aria-label": "Who" }); w.value = a.who || ""; w.addEventListener("input", () => { a.who = w.value; });
      const d = el("input", { class: "ui-input mt-due", type: "date", value: a.due || "", "aria-label": "Due" }); d.value = a.due || ""; d.addEventListener("input", () => { a.due = d.value; });
      return el("div", { class: "mt-edit-act" }, t, el("div", { class: "mt-edit-row" }, o, w, d,
        ui.button({ label: "Remove", icon: "trash", variant: "ghost", iconOnly: true, onClick: () => { acts.splice(i, 1); drawActs(); } })));
    }));
    drawActs();
    const lines = (t) => t.value.split("\n").map((s) => s.trim()).filter(Boolean);
    return el("section", { class: "ui-card mt-card ui-stack" }, el("h3", { class: "mt-h" }, "Edit notes"),
      el("span", { class: "ui-label" }, "Summary (one per line)"), f.summary, el("span", { class: "ui-label" }, "Decisions"), f.decisions,
      el("span", { class: "ui-label" }, "Risks and blockers"), f.risks, el("span", { class: "ui-label" }, "Open questions"), f.questions,
      el("span", { class: "ui-label" }, "Action items"), actBox,
      ui.button({ label: "Add an action item", icon: "plus", variant: "tonal", size: "sm", onClick: () => { acts.push({ text: "", owner: "me", who: "", due: "" }); drawActs(); } }),
      el("div", { class: "mt-actions" },
        ui.button({ label: "Save", icon: "check", variant: "primary", onClick: async () => {
          try {
            detail = await api(`/api/meetings/${detail.id}`, { method: "PATCH", json: { notes: { summary: lines(f.summary), decisions: lines(f.decisions), risks: lines(f.risks), questions: lines(f.questions), actions: acts.filter((a) => a.text.trim()) } } });
            editing = false; await loadProposal(); ui.toast("Saved.", { kind: "good" }); paint();
          } catch (error) { fail(error); }
        } }),
        ui.button({ label: "Cancel", variant: "ghost", onClick: () => { editing = false; paint(); } })));
  }

  function detailView() {
    const d = detail, n = d.notes;
    const back = ui.button({ label: "All meetings", icon: "arrow-left", variant: "ghost", size: "sm", onClick: () => { detail = null; proposal = null; editing = false; paint(); load(); } });
    const out = [back, el("div", { class: "mt-dhead" }, el("h2", { class: "mt-dtitle" }, d.title),
      el("p", { class: "ui-hint" }, `${when(d.started)} - ${Math.round((d.duration_s || 0) / 60)} min - ${d.words} words - ${(d.tracks || []).map((t) => (t === "me" ? "you" : "others")).join(" + ")}`),
      el("div", { class: "mt-badges" }, ui.badge(STATUS[d.status] || d.status, d.status === "ready" ? "good" : d.status === "failed" ? "bad" : "warn"),
        n?.method ? ui.badge(n.method === "ai" ? "AI notes" : n.method === "fallback" ? "Plain extract, no AI" : "Edited", n.method === "fallback" ? "warn" : "") : null,
        d.private ? ui.badge("Private", "accent") : null, d.audio ? ui.badge("Audio kept", "warn") : null))];
    for (const w of d.warnings || []) out.push(ui.notice(w, { tone: "warn" }));
    if (d.error) out.push(ui.notice(d.error, { tone: "bad" }));
    if (d.status === "processing") out.push(ui.notice("Making the notes. This can take a few minutes with the free models.", { icon: "clock" }));
    if (n?.note && n.method === "fallback") out.push(ui.notice(n.note, { tone: "warn" }));
    if (n && !editing) {
      out.push(el("section", { class: "ui-card mt-card ui-stack" },
        bullets("Summary", n.summary), bullets("Decisions", n.decisions), bullets("Risks and blockers", n.risks), bullets("Open questions", n.questions),
        n.actions?.length ? el("section", { class: "ui-stack mt-sec" }, el("h3", { class: "mt-h" }, "Action items"),
          el("ul", { class: "mt-acts" }, ...n.actions.map((a) => el("li", { class: a.done ? "is-done" : "" },
            ui.badge(a.who && a.owner !== "me" ? a.who : OWNER[a.owner][0], OWNER[a.owner][1]),
            el("span", { class: "mt-acttext" }, a.text), a.due ? el("span", { class: "ui-hint" }, ` due ${a.due}`) : null,
            a.t ? el("span", { class: "ui-hint" }, ` at ${clock(a.t)}`) : null, a.added ? ui.badge("Added", "good") : null)))) : el("p", { class: "ui-hint" }, "No action items were found."),
        ui.button({ label: "Edit notes", icon: "edit", variant: "tonal", size: "sm", onClick: () => { editing = true; paint(); } })));
      const pc = proposalCard();
      if (pc) out.push(pc);
    }
    if (n && editing) out.push(editForm());
    if (d.segments?.length) {
      const lines = d.segments.map((s) => el("p", { class: "mt-line mt-line--" + s.track }, el("b", {}, `${clock(s.t)} ${s.track === "me" ? "You" : "Others"}`), ` ${s.text}`));
      out.push(el("details", { class: "ui-card mt-card" }, el("summary", {}, `Full transcript (${d.segments.length} parts)`), el("div", { class: "mt-transcript" }, ...lines)));
    }
    if (d.status !== "recording" && d.status !== "processing") {
      const priv = ui.switchControl({ checked: !!d.private, label: "Private: local models only" });
      out.push(el("section", { class: "ui-card mt-card ui-stack" },
        el("div", { class: "ui-row mt-switch" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Private notes"), el("span", { class: "ui-row__sub" }, "Local models only")), priv),
        el("div", { class: "mt-actions" },
          ui.button({ label: d.notes ? "Make the notes again" : "Make the notes", icon: "refresh", variant: "secondary", onClick: async (e) => {
            try { await ui.busy(e.currentTarget, api(`/api/meetings/${d.id}/process`, { method: "POST", json: { private: priv.input.checked } })); picks = {}; await openDetail(d.id); } catch (error) { fail(error); }
          } }),
          ui.button({ label: "Delete this meeting", icon: "trash", variant: "danger", onClick: async () => {
            if (!(await ui.confirm({ title: "Delete this meeting?", message: "The transcript, notes and any audio are removed from this laptop. Tasks you already added stay.", confirmLabel: "Delete", danger: true, icon: "trash" }))) return;
            try { await api(`/api/meetings/${d.id}`, { method: "DELETE" }); ui.toast("Deleted.", { kind: "good" }); detail = null; proposal = null; load(); } catch (error) { fail(error); }
          } }))));
    }
    return out;
  }

  function paint() {
    if (loadError === "off") { root.replaceChildren(ui.emptyState({ icon: "mic-off", title: "Meetings are not on this hub", body: "Update the hub, then open this screen again." })); return; }
    if (loadError && !st) { root.replaceChildren(ui.notice(loadError, { tone: "bad" })); return; }
    if (detail) { root.replaceChildren(...detailView()); return; }
    root.replaceChildren(statusCard(), listCard(), settingsFold());
    if (loadError) root.prepend(ui.notice(loadError, { tone: "bad" }));
  }

  paint();
  load();
  return { show() { load(); }, refresh() { load(); } };
}
