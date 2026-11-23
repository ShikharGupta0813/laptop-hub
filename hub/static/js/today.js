// Today: the whole day on one page (meetings, tasks, spending, mail, what you did). Read-only; routes: docs/api/today.md.
// Everything shown is text (textContent). The last answer is kept in localStorage so the page opens even offline.
import { api, ApiError, el, sse } from "./api.js";
import * as ui from "./ui.js";
import { councilButton } from "./lanepicker.js";

const CACHE_KEY = "hub.today.v1";
const KIND_ICON = { meeting: "calendar", commit: "code", mail: "mail", task: "check-circle", card: "zap", council: "users", expense: "chart", followup: "bell" };

function readCache() { try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "null"); } catch (_) { return null; } }
function writeCache(data) { try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); } catch (_) { /* private mode: fine */ } }

export function init(container, ctx) {
  const root = el("div", { class: "today ui-stack ui-stack--lg" });
  container.append(root);
  let data = readCache(), stale = !!data, loadError = "", evening = false, words = "";

  async function load({ quiet = false, refresh = false } = {}) {   // refresh: skip the hub's 15-minute memory of calendar, tasks and mail
    if (!data && !quiet) root.replaceChildren(ui.skeleton("card", 3));
    try {
      data = await api(refresh ? "/api/today?refresh=1" : "/api/today");
      stale = false; loadError = "";
      writeCache(data);
    } catch (error) {
      loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message;
    }
    paint();
  }

  const stat = (label, value) => el("div", { class: "ui-card" }, el("p", { class: "ui-caps" }, label), el("p", { class: "ui-title" }, String(value ?? "-")));

  function now() {
    const p = data.pending || {};
    const rows = [];
    if (p.next_meeting) rows.push(ui.listRow({ icon: "calendar", tone: "accent", title: `Next: ${p.next_meeting.title}`, sub: `${p.next_meeting.time}${p.next_meeting.in_min != null ? ` · in ${p.next_meeting.in_min} min` : ""}` }));
    else rows.push(ui.listRow({ icon: "calendar", tone: "gray", title: "No more meetings today" }));
    rows.push(ui.listRow({ icon: "zap", tone: p.autopilot ? "warn" : "gray", title: p.autopilot ? `${p.autopilot} draft${p.autopilot === 1 ? "" : "s"} to approve` : "Nothing to approve",
      sub: p.autopilot ? "Open Autopilot" : "", chevron: !!p.autopilot, onClick: p.autopilot ? () => ctx?.open?.("autopilot") : undefined }));
    return el("section", { class: "ui-stack" }, ui.eyebrow("01", "Now"), el("div", { class: "ui-list ui-list--inset" }, ...rows));
  }

  // Things Friday held back during quiet hours (hub/proactive.py)
  function held() {
    const rows = (data.proactive?.held || []).map((h) => ui.listRow({ icon: "bell", tone: "gray", title: h.title, sub: "Held back (quiet hours)", meta: h.at || "" }));
    if (!rows.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("QH", "While you were away"), el("div", { class: "ui-list ui-list--inset" }, ...rows));
  }

  // Friday's list: follow-ups due today (and late ones) from the Memory screen's store
  function followups() {
    const rows = (data.brain?.due || []).map((f) => ui.listRow({ icon: "bell", tone: f.overdue ? "warn" : "accent", title: f.title,
      sub: (f.who ? `from ${f.who} · ` : "") + (f.date === data.date ? "due today" : `since ${f.date}`), meta: f.time || "", chevron: true, onClick: () => ctx?.open?.("brain") }));
    if (!rows.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("FU", "Follow-ups"), el("div", { class: "ui-list ui-list--inset" }, ...rows));
  }

  // One small line from the application queue (hub/applyq.py); hidden when empty
  // One small line from the job digest (hub/jobdigest.py): new jobs, drafts ready; hidden when empty
  function jobdigestLine() {
    if (!data.jobdigest) return null;
    return el("p", { class: "ui-hint", style: "cursor:pointer", onclick: () => ctx?.open?.("jobdigest") }, data.jobdigest);
  }
  // One small line from the job tracker (hub/jobtrack.py): "N follow-ups due"; hidden when empty
  function jobtrackLine() {
    if (!data.jobtrack) return null;
    return el("p", { class: "ui-hint", style: "cursor:pointer", onclick: () => ctx?.open?.("jobtrack") }, data.jobtrack);
  }

  function applyqLine() {
    if (!data.applyq) return null;
    return el("p", { class: "ui-hint", style: "cursor:pointer", onclick: () => ctx?.open?.("applyq") }, data.applyq);
  }

  function timeline() {
    const items = data.timeline || [];
    const body = items.length
      ? el("div", { class: "ui-list ui-list--inset" }, ...items.map((i) => ui.listRow({ icon: KIND_ICON[i.kind] || "clock", tone: i.kind === "expense" ? "warn" : "accent", title: i.title, sub: i.sub, meta: i.time || "" })))
      : ui.emptyState({ icon: "clock", title: "Nothing yet today", compact: true });
    return el("section", { class: "ui-stack" }, ui.eyebrow("02", "Timeline"), body);
  }

  function doneToday() {
    const d = data.done || {}, rows = [];
    for (const c of d.commits || []) rows.push(ui.listRow({ icon: "code", title: c.subject, sub: c.repo, meta: c.at }));
    for (const c of d.tasks?.commented || []) rows.push(ui.listRow({ icon: "task", title: `Commented on ${c.name}`, meta: c.at }));
    if (d.tasks?.completed) rows.push(ui.listRow({ icon: "check-circle", tone: "good", title: `Completed ${d.tasks.completed} Asana task${d.tasks.completed === 1 ? "" : "s"}` }));
    for (const r of data.mail?.replied || []) rows.push(ui.listRow({ icon: "mail", title: `Replied to ${r.to || "a mail"}`, sub: r.subject, meta: r.at }));
    for (const c of d.council || []) rows.push(ui.listRow({ icon: "users", title: `Council: ${c.task}`, sub: c.status, meta: c.at }));
    const body = rows.length ? el("div", { class: "ui-list ui-list--inset" }, ...rows) : ui.emptyState({ icon: "check-circle", title: "Nothing done yet", compact: true });
    return el("section", { class: "ui-stack" }, ui.eyebrow("03", "Done today"), body);
  }

  function numbers() {
    const n = data.numbers || {};
    return el("section", { class: "ui-stack" }, ui.eyebrow("04", "Numbers"),
      el("div", { class: "ui-stack" }, stat("Spent today", n.spent_text), stat("Mails handled", n.mails_handled), ...(data.mail?.accounts || []).map((a) => stat(`Unread (${a.label})`, a.unread ?? "?")), stat("Commits", n.commits), stat("Cards approved", n.cards_approved)));
  }

  function tasks() {
    const t = data.tasks || {}, rows = [];
    const withCouncil = (row, x) => el("div", { class: "lp-rowwrap" }, row, councilButton(() => `${x.name}${x.project ? " (" + x.project + ")" : ""}`, { size: "sm", label: "Council" }));
    for (const x of t.overdue || []) rows.push(withCouncil(ui.listRow({ icon: "alert-circle", tone: "bad", title: x.name, sub: `Overdue since ${x.due_on}${x.project ? " · " + x.project : ""}` }), x));
    for (const x of t.due_today || []) rows.push(withCouncil(ui.listRow({ icon: "task", title: x.name, sub: `Due today${x.project ? " · " + x.project : ""}` }), x));
    if (!rows.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("05", "Tasks"), el("div", { class: "ui-list ui-list--inset" }, ...rows));
  }

  function summary() {
    const text = el("p", { class: "ui-hint" }, words);
    const btn = ui.button({ label: evening ? "Morning summary" : "Evening review", icon: "sparkles", variant: "tonal", onClick: () => ui.busy(btn, async () => {
      try {
        evening = !evening;
        const r = await api(`/api/today/text?style=${evening ? "evening" : "morning"}`);
        words = r.text;
        paint();
      } catch (error) { ui.toast(error.message, { kind: "bad" }); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("06", "In words"), text, btn);
  }

  // ---- ask the assistant about this very screen -------------------------------------------------
  const sid = "today-" + Math.random().toString(36).slice(2, 10);
  let askOut = null, askNode = null;

  function screenText() {
    const d = data || {}, n = d.numbers || {}, t = d.tasks || {}, p = d.pending || {}, L = [];
    L.push(`TODAY screen, ${d.weekday || ""} ${d.date || ""}`);
    if (p.next_meeting) L.push(`Next meeting: ${p.next_meeting.title} at ${p.next_meeting.time}`);
    L.push(`Autopilot drafts waiting for approval: ${p.autopilot || 0}`);
    for (const i of d.timeline || []) L.push(`- ${i.time || ""} [${i.kind}] ${i.title}${i.sub ? " (" + i.sub + ")" : ""}`);
    for (const x of t.overdue || []) L.push(`Overdue task: ${x.name} (since ${x.due_on})`);
    for (const x of t.due_today || []) L.push(`Task due today: ${x.name}`);
    for (const c of d.done?.commits || []) L.push(`Done: commit "${c.subject}" in ${c.repo}`);
    L.push(`Spent today: ${n.spent_text}; mails handled: ${n.mails_handled}; commits: ${n.commits}; cards approved: ${n.cards_approved}`);
    return L.join("\n");
  }

  async function sendAsk(text, input, btn) {
    text = (text || "").trim();
    if (!text) return;
    input.value = "";
    askOut.replaceChildren(el("p", { class: "ui-hint" }, "You: " + text), el("p", { class: "today-answer" }, "Thinking…"));
    const answer = askOut.lastChild;
    let got = "";
    btn.disabled = true;
    try {
      for await (const ev of sse("/api/assistant/message", { session_id: sid, text, context: screenText(), provider: "auto", auto_approve: false, persona: "auto", voice: false })) {
        if (ev.type === "text") { got += ev.t || ""; answer.textContent = got; }
        else if (ev.type === "approval_needed") {
          const row = el("div", { class: "ui-row" });
          const done = (ok) => { api("/api/assistant/approve", { method: "POST", json: { session_id: sid, call_id: ev.id, approve: ok } }).catch((e) => ui.toast(e.message, { kind: "bad" })); row.remove(); };
          row.append(el("span", { class: "ui-hint" }, `Wants to use “${ev.name}”.`),
            ui.button({ label: "Approve", icon: "check", onClick: () => done(true) }),
            ui.button({ label: "Deny", variant: "secondary", icon: "x", onClick: () => done(false) }));
          askOut.append(row);
        } else if (ev.type === "error") { answer.textContent = ev.message || "Something went wrong."; }
      }
      if (!got && answer.textContent === "Thinking…") answer.textContent = "Done.";
      else if (got) { try { ctx?.speak?.(got.replace(/[*_`#>]/g, "").slice(0, 600)); } catch (_) { /* silent */ } }
      load({ quiet: true });
    } catch (error) { answer.textContent = error.message; }
    btn.disabled = false;
  }

  function askBar() {
    const input = el("input", { class: "ui-input", type: "text", placeholder: "Ask or tell me anything about today…", enterkeyhint: "send", "aria-label": "Ask about this screen" });
    const send = ui.button({ label: "Send", icon: "send", onClick: () => sendAsk(input.value, input, send) });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendAsk(input.value, input, send); } });
    const row = el("div", { class: "ui-row" }, input, send);
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      const mic = ui.button({ icon: "mic", iconOnly: true, variant: "tonal", "aria-label": "Talk", onClick: () => {
        const r = new SR(); r.lang = "en-IN"; r.interimResults = false;
        r.onresult = (e) => sendAsk(e.results[0][0].transcript, input, send);
        r.onerror = () => ui.toast("Couldn't hear you. Try again or type.", { kind: "warn" });
        r.onstart = () => window.dispatchEvent(new CustomEvent("hub:mic", { detail: { on: true } }));
        r.onend = () => window.dispatchEvent(new CustomEvent("hub:mic", { detail: { on: false } }));
        try { r.start(); ui.toast("Listening…", { kind: "info" }); } catch (_) { /* already listening */ }
      } });
      row.insertBefore(mic, send);
    }
    askOut = el("div", { class: "ui-stack" });
    return el("section", { class: "ui-card ui-stack today-ask" }, ui.eyebrow("AI", "Talk about today"), row, askOut);
  }

  function paint() {
    if (loadError === "off" && !data) { root.replaceChildren(ui.emptyState({ icon: "clock", title: "Today is switched off", body: "Set today_enabled to true in Settings." })); return; }
    if (!data) { root.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load Today", body: loadError, action: { label: "Try again", icon: "refresh", onClick: () => load() } })); return; }
    const refresh = ui.button({ label: "Refresh", icon: "refresh", variant: "ghost", onClick: () => ui.busy(refresh, () => load({ quiet: true, refresh: true })) });
    const front = ui.button({ label: "Front page", icon: "file", variant: "tonal", onClick: () => window.open("/api/today/front-page", "_blank", "noopener") });
    root.replaceChildren(...[
      el("div", { class: "td-head" }, el("h2", { class: "ui-title" }, [data.weekday || "Today", data.date].filter(Boolean).join(" · ")),
        el("div", { class: "td-head__actions" }, front, refresh)),
      stale || loadError ? ui.notice(loadError ? `Showing the last saved copy (${loadError}).` : "Showing the last saved copy while it refreshes.", { tone: "warn" }) : null,
      data.cache?.stale?.length ? ui.notice("Calendar, tasks or mail could not be reached, so older copies are shown.", { tone: "warn" }) : null,
      data.cache?.age_min >= 1 ? el("p", { class: "ui-hint" }, `Calendar, tasks and mail are from ${data.cache.age_min} min ago. Tap Refresh for live.`) : null,
      data.sources_failed?.length ? ui.notice(`Could not read: ${data.sources_failed.join(", ")}.`, { tone: "warn" }) : null,
      (askNode ||= askBar()), now(), held(), followups(), applyqLine(), jobtrackLine(), jobdigestLine(), timeline(), doneToday(), numbers(), tasks(), summary(),
    ].filter(Boolean));
  }

  if (data) paint();
  load({ quiet: !!data });
  ui.pullToRefresh?.(container, () => load({ quiet: true, refresh: true }));
  return { show() { load({ quiet: true }); }, refresh() { load({ quiet: true, refresh: true }); } };
}
