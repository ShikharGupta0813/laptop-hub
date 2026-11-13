// Claude: the owner's Claude Code sessions on the laptop (what each is doing, whether it waits on you) and a box to
// nudge one. Routes: docs/api/ccsessions.md. Transcript text is untrusted: everything is shown with textContent only.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/ccsessions.css";
const LABEL = { working: "Working", waiting: "Waiting on you", idle: "Idle" };
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const age = (s) => (s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`);

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "cc ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, loadError = "", openId = null, detail = null, timer = null;
  const drafts = new Map(), jobs = new Map();   // session id -> unsent text / last job result line

  async function load({ quiet = false } = {}) {
    if (!data && !quiet) root.replaceChildren(ui.skeleton("card", 3));
    try { data = await api("/api/ccsessions"); loadError = ""; }
    catch (error) { loadError = error instanceof ApiError && (error.status === 404 || error.status === 405) ? "missing" : error.message; }
    if (openId) { try { detail = await api(`/api/ccsessions/${encodeURIComponent(openId)}`); } catch (_) { detail = null; } }
    paint();
  }

  async function watchJob(id, sid, out) {
    for (let i = 0; i < 400; i += 1) {
      await new Promise((r) => setTimeout(r, 3000));
      if (!out.isConnected) return;
      try {
        const j = await api(`/api/ccsessions/jobs/${encodeURIComponent(id)}`);
        if (j.status === "done") { jobs.set(sid, `Claude replied: ${j.result || "(no text)"}`); out.textContent = jobs.get(sid); load({ quiet: true }); return; }
        if (j.status === "failed") { jobs.set(sid, `Failed: ${j.error || "unknown error"}`); out.textContent = jobs.get(sid); return; }
      } catch (_) { return; }
    }
  }

  function tellBox(s) {
    const area = el("textarea", { class: "cc-say", rows: 3, maxlength: 4000, placeholder: "Tell it…", "aria-label": "Message to send to this Claude session" });
    area.value = drafts.get(s.id) || "";
    area.addEventListener("input", () => drafts.set(s.id, area.value));
    const out = el("p", { class: "cc-job ui-hint", role: "status" }, jobs.get(s.id) || "");
    const working = s.status === "working";
    const send = ui.button({ label: working ? "Working, wait" : "Send", icon: "send", variant: "primary", disabled: working, onClick: () => ui.busy(send, async () => {
      const text = area.value.trim();
      if (!text) return;
      if (!await ui.confirm({ title: `Send to ${s.project_name}?`, message: `Claude Code will resume this session in ${s.project} and receive exactly: ${text}`, confirmLabel: "Send" })) return;
      try {
        const r = await api(`/api/ccsessions/${encodeURIComponent(s.id)}/send`, { method: "POST", json: { message: text, confirm: true } });
        drafts.delete(s.id); area.value = "";
        out.textContent = "Sent. Waiting for Claude…";
        watchJob(r.job, s.id, out);
      } catch (error) { fail(error); }
    }) });
    return el("div", { class: "cc-tell ui-stack" }, area, el("div", { class: "cc-actions" }, send), out);
  }

  function turns(d) {
    if (!d?.turns?.length) return el("p", { class: "ui-hint" }, "No readable turns yet.");
    return el("ol", { class: "cc-turns" }, ...d.turns.map((t) => el("li", { class: `cc-turn cc-turn--${t.role}` },
      el("span", { class: "ui-caps" }, t.role === "user" ? "You" : "Claude"), el("span", { class: "cc-turn-text" }, t.text || "…"))));
  }

  function card(s) {
    const open = openId === s.id;
    const head = el("button", { class: "cc-head", type: "button", "aria-expanded": String(open), onclick: () => { openId = open ? null : s.id; detail = null; paint(); if (!open) load({ quiet: true }); } },
      el("span", { class: `cc-dot cc-dot--${s.status}`, role: "img", "aria-label": LABEL[s.status] || s.status }),
      el("span", { class: "cc-project" }, s.project_name || s.project),
      el("span", { class: "cc-age" }, age(s.age_seconds)));
    return el("article", { class: `ui-card cc-card cc-card--${s.status} ui-stack`, "data-id": s.id },
      head,
      el("p", { class: "cc-state" }, (LABEL[s.status] || s.status) + (s.permission_pending ? " · a tool is waiting for a yes" : "")),
      s.last_user ? el("p", { class: "cc-line" }, el("span", { class: "ui-caps" }, "You "), s.last_user) : null,
      s.last_assistant ? el("p", { class: "cc-line" }, el("span", { class: "ui-caps" }, "Claude "), s.last_assistant) : null,
      open ? turns(detail) : null,
      open ? tellBox(s) : null);
  }

  function paint() {
    if (loadError === "missing") { root.replaceChildren(ui.emptyState({ icon: "code", title: "Claude sessions aren't on this laptop", body: "Update the hub to get them." })); return; }
    if (loadError && !data) { root.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load sessions", body: loadError, action: { label: "Try again", icon: "refresh", onClick: () => load() } })); return; }
    if (!data.sessions.length) {
      root.replaceChildren(ui.emptyState({ icon: "code", title: "No Claude Code sessions found", body: data.dir_exists ? "Start Claude Code on the laptop and it appears here." : "The Claude Code folder (~/.claude/projects) does not exist on this laptop yet." }));
      return;
    }
    const keep = document.activeElement?.classList?.contains("cc-say");
    root.replaceChildren(...data.sessions.map(card));
    if (keep) root.querySelector(".cc-say")?.focus();
  }

  load();
  return {
    show() { load({ quiet: true }); if (!timer) timer = setInterval(() => { if (!document.hidden && !document.activeElement?.classList?.contains("cc-say")) load({ quiet: true }); }, 15000); },
    hide() { clearInterval(timer); timer = null; },
    refresh() { load(); },
  };
}
