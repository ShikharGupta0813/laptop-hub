// Presence: tells the hub when this phone's app is open (so alerts can be pushed and spoken here when you are away from
// the laptop), says out loud what the hub could not hold back, and offers "focus for ..." on Home and the delivery
// rules in Settings. Routes: docs/api/presence.md. Everything shown is text (textContent), never HTML.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";

const STATE_LABEL = { at_laptop: "at the laptop", near: "near the laptop", away: "away", phone_active: "on the phone", in_meeting: "in a meeting",
  focus: "focus", quiet_hours: "quiet hours", unknown: "unknown" };
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
let started = false;

// The heartbeat: on every visibility change and every 30 s while visible. A hub without the module answers 404: stop quietly.
export function mountPresence(ctx) {
  if (started) return;
  started = true;
  let dead = false;
  const beat = async () => {
    if (dead) return;
    try { await api("/api/presence/phone", { method: "POST", json: { visible: !document.hidden, screen_on: !document.hidden } }); }
    catch (error) { if (error?.status === 404 || error?.status === 405) dead = true; }
  };
  const speakPending = async () => {
    if (dead || document.hidden) return;
    try {
      const { speech } = await api("/api/presence/pending");
      if (!speech?.length) return;
      ctx.speak?.(speech.map((s) => s.text).join(" "));
      await api("/api/presence/pending/ack", { method: "POST", json: { ids: speech.map((s) => s.id) } });
    } catch (_) { /* try again next time */ }
  };
  document.addEventListener("visibilitychange", beat);
  window.addEventListener("pagehide", () => { try { navigator.sendBeacon?.("/api/presence/phone", new Blob([JSON.stringify({ visible: false, screen_on: false })], { type: "application/json" })); } catch (_) { /* ignore */ } });
  setInterval(() => { if (!document.hidden) { beat(); } }, 30000);
  setInterval(speakPending, 20000);
  setTimeout(beat, 1500);
}

// A row for Home: where the hub thinks you are, and a tap to start or end focus.
export function presenceChip(ctx) {
  const v = el("span", { class: "ui-kv__v" }, "...");
  const node = el("button", { type: "button", class: "ui-kv__row hm-kv hm-kv--btn", "aria-label": "Presence and focus",
    onclick: () => focusSheet(refresh) }, el("span", { class: "ui-kv__k" }, "presence"), v, ui.icon("chevron-right"));
  async function refresh() {
    try {
      const p = await api("/api/presence");
      if (!p.enabled) { node.hidden = true; return; }
      v.textContent = p.focus?.on ? `focus · ${p.focus.minutes_left} min` : (STATE_LABEL[p.state] || p.state);
    } catch (_) { node.hidden = true; }
  }
  refresh();
  return node;
}

function focusSheet(done) {
  const go = async (json, msg) => {
    try { await api("/api/presence/focus", { method: "POST", json }); ui.toast(msg, { kind: "good" }); done?.(); } catch (error) { fail(error); }
  };
  ui.sheet({ title: "Focus", content: el("div", { class: "ui-stack ui-stack--lg" },
    el("p", { class: "ui-hint" }, "While focus is on, the hub holds everything except emergencies and gives you one summary afterwards."),
    ...[[30, "30 minutes"], [60, "1 hour"], [120, "2 hours"]].map(([m, label]) => ui.button({ label: `Focus for ${label}`, variant: "primary", block: true, onClick: () => go({ minutes: m }, "Focus is on.") })),
    ui.button({ label: "End focus", block: true, onClick: () => go({ off: true }, "Focus is off.") })) });
}

// Settings > Delivery rules
export async function openDeliveryRules() {
  let data = await api("/api/presence/rules").catch((e) => { fail(e); return null; });
  if (!data) return;
  const save = async (rules) => { data = await api("/api/presence/rules", { method: "PUT", json: { rules } }); ed.set(data.rules); };
  const opt = (list, any) => [...list.map((x) => [x, STATE_LABEL[x] || x]), ...(any ? [["*", "any state"]] : [])];
  const fields = [
    { key: "urgency", label: "How urgent", type: "select", options: data.urgencies.map((u) => [u, u]), hint: "critical = priority 3 or a VIP. urgent = anything pushed at once. normal = the batched kinds." },
    { key: "state", label: "Where you are", type: "select", options: data.states.map((s) => [s, s === "*" ? "any state" : (STATE_LABEL[s] || s)]) },
    { key: "actions", label: "What to do", type: "chips", maxlength: 200, hint: `Any of: ${data.actions.join(", ")}.` },
    { key: "enabled", label: "On", type: "switch" },
  ];
  void opt;
  const ed = listEditor({
    title: "Delivery rules", items: data.rules, addLabel: "Add rule", resetLabel: "Back to the ready-made rules", empty: "No rules: everything is delivered the old way.",
    hint: "How each notification reaches you, by how urgent it is and where you are. Unknown presence and quiet hours keep the old behaviour.",
    rowOf: (r) => ({ icon: "bell", tone: r.urgency === "critical" ? "bad" : r.urgency === "urgent" ? "orange" : "accent", title: `${r.urgency} · ${r.state === "*" ? "any state" : STATE_LABEL[r.state] || r.state}`,
      sub: r.actions.join(", "), enabled: r.enabled }),
    onToggle: (r, on) => save(data.rules.map((x) => (x.id === r.id ? { ...x, enabled: on } : x))),
    onEdit: (r) => formSheet({ title: "Edit rule", fields, values: r, onSave: async (v) => { await save(data.rules.map((x) => (x.id === r.id ? { ...x, ...v } : x))); } }),
    onAdd: () => formSheet({ title: "New rule", fields, values: { urgency: "normal", state: "away", actions: ["batch"], enabled: true },
      onSave: async (v) => { await save([...data.rules, { ...v, id: "" }]); } }),
    onDelete: (r) => save(data.rules.filter((x) => x.id !== r.id)),
    onReset: async () => { data = await api("/api/presence/rules/reset", { method: "POST" }); ed.set(data.rules); },
  });
  ui.sheet({ title: "Delivery rules", class: "st-sheet st-sheet--tall", content: el("div", { class: "ui-stack ui-stack--lg" }, ed.el,
    ui.notice("Say \"I am going out\", \"I'm back\" or \"focus for an hour\" to set where you are by hand. These expire on their own.", { tone: "info" })) });
}
