// Batches: the slots you schedule (name, time, count, profile, applications or outreach), the batch each slot builds, and ONE
// tap to send what you left ticked. Nothing here sends by itself. Manual-only sites (LinkedIn, Indeed, your list) show as
// "Open + copy" and are never sent from here. Routes: docs/api/batches.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor } from "./listeditor.js";

const CSS = "/css/jobprefs.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const DAYS = [[0, "Mon"], [1, "Tue"], [2, "Wed"], [3, "Thu"], [4, "Fri"], [5, "Sat"], [6, "Sun"]];
const MODE_TONE = { remote: "good", hybrid: "warn", onsite: "gray", unknown: "gray" };
const RESULT = { sent: ["Sent", "good"], applied: ["Applied", "good"], needs_check: ["Needs a check", "bad"], stopped: ["Stopped", "bad"], failed: ["Failed", "bad"],
  not_attempted: ["Not attempted", ""], skipped: ["Skipped", ""], needs_you: ["Needs you", ""] };
const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);
const when = (iso) => String(iso || "").slice(0, 16).replace("T", " ");
const csv = (v) => (Array.isArray(v) ? v.join(", ") : "");
const split = (t) => String(t || "").split(/[,\n;]/).map((x) => x.trim()).filter(Boolean);

async function copy(textValue) {
  try { await navigator.clipboard.writeText(textValue); ui.toast("Copied.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "bt ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, open = null, error = "", poll = 0;
  const editing = new Set();

  async function load() {
    try { data = await api("/api/batches"); error = ""; if (open) open = await api(`/api/batches/${open.id}`).catch(() => null); }
    catch (e) { error = e.message; }
    paint();
    clearTimeout(poll);
    if (open?.state === "sending" || data?.sending) poll = setTimeout(load, 2000);
  }

  // ---- slots ----
  function slotSheet(s) {
    const cur = s || { name: "", kind: "applications", count: 5, profile: "", enabled: true, schedule: null };
    const name = el("input", { class: "ui-input", type: "text", maxlength: "60", placeholder: "Morning applications" }); name.value = cur.name;
    const kind = el("select", { class: "ui-input" }, el("option", { value: "applications" }, "Applications"), el("option", { value: "outreach" }, "Outreach messages")); kind.value = cur.kind;
    const count = el("input", { class: "ui-input", type: "number", min: "1", max: "50", inputmode: "numeric" }); count.value = cur.count;
    const prof = el("select", { class: "ui-input" }, el("option", { value: "" }, "Any profile"), ...data.profiles.map((p) => el("option", { value: p.id }, p.name))); prof.value = cur.profile || "";
    const own = s ? (s.schedule_own ? s.schedule : null) : { kind: "daily", time: "09:00" };
    const time = el("input", { class: "ui-input", type: "time" }); time.value = own?.time || "";
    const days = new Set(own?.kind === "weekly" ? own.days : []);
    const dayRow = el("div", { class: "jp-chips", role: "group", "aria-label": "Days" }, ...DAYS.map(([n, t]) => {
      const b = el("button", { type: "button", class: "jp-chip" + (days.has(n) ? " is-on" : ""), "aria-pressed": String(days.has(n)) }, t);
      b.addEventListener("click", () => { if (days.has(n)) days.delete(n); else days.add(n); b.classList.toggle("is-on", days.has(n)); b.setAttribute("aria-pressed", String(days.has(n))); });
      return b;
    }));
    const on = ui.switchControl({ checked: cur.enabled !== false, label: "On" });
    const field = (label, c, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), c, hint ? el("span", { class: "ui-hint" }, hint) : null);
    ui.sheet({ title: s ? "Edit slot" : "New slot", class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
      field("Name", name), field("What to build", kind), field("How many", count), field("Job profile", prof, "Only items that pass this profile's rules go in."),
      field("Time", time, "Leave empty to use the profile's schedule."), el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, "Days"), dayRow, el("span", { class: "ui-hint" }, "None selected means every day.")),
      el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "On")), on)),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const body = { name: name.value, kind: kind.value, count: Number(count.value), profile: prof.value, enabled: on.input.checked,
        schedule: time.value ? (days.size ? { kind: "weekly", time: time.value, days: [...days].sort() } : { kind: "daily", time: time.value }) : null };
      try { await api(s ? `/api/batches/slots/${s.id}` : "/api/batches/slots", { method: s ? "PUT" : "POST", json: body }); ui.toast("Saved.", { kind: "good" }); load(); return true; }
      catch (e) { fail(e); return false; }
    } }] });
  }

  function slotsSection() {
    const ed = listEditor({ title: "Slots", hint: "At each time the hub builds the batch and sends you ONE notification. It never sends anything itself.", items: data.slots,
      addLabel: "Add slot", empty: "No slots yet. Add one, for example 09:00 with 5 applications.",
      rowOf: (s) => ({ icon: "clock", tone: "accent", title: s.name, enabled: s.enabled !== false,
        sub: `${s.schedule ? (s.schedule.kind === "weekly" ? `${s.schedule.time} on ${s.schedule.days.map((d) => DAYS[d][1]).join(", ")}` : s.schedule.time + (s.schedule.kind === "weekdays" ? " weekdays" : " daily")) : "no time"} · ${s.count} ${s.kind}${s.profile ? " · " + (data.profiles.find((p) => p.id === s.profile)?.name || "profile") : ""}`,
        meta: s.missed ? "missed" : `today ${s.today.sent}/${s.today.built}` }),
      onToggle: async (s, v) => { await api(`/api/batches/slots/${s.id}`, { method: "PUT", json: { enabled: v } }); load(); },
      onEdit: slotSheet, onAdd: () => slotSheet(null),
      onDelete: async (s) => { await api(`/api/batches/slots/${s.id}`, { method: "DELETE" }); load(); } });
    const build = ui.button({ label: "Prepare a batch now", icon: "plus", variant: "tonal", size: "sm", onClick: () => ui.busy(build, async () => {
      try {
        const r = await api("/api/batches/build", { method: "POST", json: { kind: "applications", count: 5 } });
        if (!r.batch) ui.toast(r.message, { kind: "warn" }); else { open = r.batch; ui.toast(r.message, { kind: "good" }); }
        load();
      } catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-stack" }, ed.el, el("div", { class: "bt-actions" }, build));
  }

  function settingsSection() {
    const s = data.settings;
    const catchup = el("input", { class: "ui-input", type: "number", min: "0", max: "1440", inputmode: "numeric" }); catchup.value = s.catchup_min;
    const cap = el("input", { class: "ui-input", type: "number", min: "0", max: "200", inputmode: "numeric" }); cap.value = s.outreach_cap;
    const hosts = el("input", { class: "ui-input", type: "text", maxlength: "600", placeholder: "example.com, another.org" }); hosts.value = csv(s.manual_hosts);
    const on = ui.switchControl({ checked: s.enabled, label: "Scheduled batches on" });
    const save = ui.button({ label: "Save settings", icon: "check", variant: "primary", size: "sm", onClick: () => ui.busy(save, async () => {
      try { await api("/api/batches/settings", { method: "PUT", json: { enabled: on.input.checked, catchup_min: Number(catchup.value), outreach_cap: Number(cap.value), manual_hosts: split(hosts.value) } }); ui.toast("Saved.", { kind: "good" }); load(); }
      catch (e) { fail(e); }
    }) });
    const f = (label, c, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), c, hint ? el("span", { class: "ui-hint" }, hint) : null);
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Settings"),
      el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Scheduled batches on"), el("span", { class: "ui-row__sub" }, "Panic also pauses them.")), on),
      f("Catch up after sleep (minutes)", catchup, "If the laptop slept through a slot, it still builds the batch once when it wakes, up to this long after the time. 0 = never."),
      f("Outreach messages per day (0 = no cap)", cap),
      f("Sites that are always Open + copy", hosts, `Never automated, always: ${s.fixed_manual_hosts.join(", ")}. Add more here.`), save);
  }

  // ---- one batch ----
  function card(batch, c) {
    const ticked = el("input", { type: "checkbox", "aria-label": `Send ${c.title}` });
    ticked.checked = !!c.ticked; ticked.disabled = !c.sendable || batch.state === "sending" || !!(c.result && ["sent", "applied", "needs_check"].includes(c.result.status));
    ticked.addEventListener("change", async () => {
      try { open = await api(`/api/batches/${batch.id}/items/${c.item_id}`, { method: "PATCH", json: { ticked: ticked.checked } }); paint(); } catch (e) { ticked.checked = !ticked.checked; fail(e); }
    });
    const href = safeHref(c.url);
    const badges = el("div", { class: "bt-badges" }, ui.badge(c.mode === "onsite" ? "On-site" : c.mode[0].toUpperCase() + c.mode.slice(1), MODE_TONE[c.mode]),
      c.geo && c.geo !== "worldwide" ? ui.badge(`only ${c.geo}`, "warn") : c.geo ? ui.badge("worldwide", "good") : null,
      c.pay ? ui.badge(c.pay) : null, ui.badge(c.route === "email" ? "Email" : c.route === "form" ? "Form on laptop" : "Open + copy", c.route === "manual" ? "gray" : "accent"),
      c.score ? ui.badge(`fit ${c.score}`) : null);
    const node = el("article", { class: "ui-card ui-stack bt-card" + (c.sendable ? "" : " is-off") },
      el("div", { class: "bt-head" }, c.sendable ? ticked : null, el("div", { class: "ui-stack", style: "flex:1;min-width:0" },
        el("h3", { class: "bt-title" }, c.title), el("span", { class: "bt-sub" }, [c.company || "Unknown company", c.channel === "email" && c.contact_email ? `to ${c.contact_email}` : ""].filter(Boolean).join(" · ")))),
      badges,
      c.why?.length ? el("p", { class: "bt-why" }, "Why: " + c.why.join("; ")) : null,
      ...(c.warnings || []).map((w) => el("p", { class: "bt-warn" }, w)),
      el("p", { class: "bt-note" }, c.route_note),
      c.draft?.preview ? el("p", { class: "bt-preview" }, (c.draft.subject ? c.draft.subject + "\n" : "") + c.draft.preview) : null,
      el("p", { class: "bt-note" }, "Resume: " + c.resume));
    if (c.result) {
      const [label, tone] = RESULT[c.result.status] || [c.result.status, ""];
      node.append(el("p", { class: "bt-result" + (tone === "good" ? " is-good" : tone === "bad" ? " is-bad" : "") }, `${label}: ${c.result.message}`));
    }
    const actions = el("div", { class: "bt-actions" });
    if (c.route === "manual") {
      if (href) actions.append(el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm", href, target: "_blank", rel: "noopener noreferrer" }, "Open"));
      actions.append(ui.button({ label: "Copy text", icon: "copy", variant: "tonal", size: "sm", onClick: () => copy([c.copy?.subject, c.copy?.body].filter(Boolean).join("\n\n")) }));
      if (c.state === "ready") actions.append(ui.button({ label: "I applied", icon: "check", variant: "ghost", size: "sm", onClick: async () => {
        try { await api(`/api/applyq/${c.item_id}/applied`, { method: "POST", json: {} }); load(); } catch (e) { fail(e); }
      } }));
    } else if (batch.state !== "sending" && !c.result) {
      actions.append(ui.button({ label: editing.has(c.item_id) ? "Close editor" : "Edit", icon: "edit", variant: "ghost", size: "sm", onClick: () => { if (editing.has(c.item_id)) editing.delete(c.item_id); else editing.add(c.item_id); paint(); } }));
      if (href) actions.append(el("a", { class: "ui-btn ui-btn--ghost ui-btn--sm", href, target: "_blank", rel: "noopener noreferrer" }, "Posting"));
    }
    node.append(actions);
    if (editing.has(c.item_id)) {
      const subject = el("input", { class: "ui-input", type: "text", maxlength: "200", "aria-label": "Subject" }); subject.value = c.draft.subject || "";
      const body = el("textarea", { class: "ui-input bt-body", maxlength: "8000", "aria-label": "Message" }); body.value = c.draft.body || "";
      const save = ui.button({ label: "Save", icon: "check", variant: "primary", size: "sm", onClick: () => ui.busy(save, async () => {
        try { open = await api(`/api/batches/${batch.id}/items/${c.item_id}`, { method: "PATCH", json: { draft: { subject: subject.value, body: body.value } } }); editing.delete(c.item_id); paint(); } catch (e) { fail(e); }
      }) });
      node.append(subject, body, el("div", { class: "bt-actions" }, save));
    }
    return node;
  }

  function batchView(b) {
    const back = ui.button({ label: "All batches", icon: "arrow-left", variant: "ghost", size: "sm", onClick: () => { open = null; paint(); } });
    const ticked = b.items.filter((c) => c.ticked && c.sendable && !(c.result && ["sent", "applied", "needs_check"].includes(c.result.status)));
    const emails = ticked.filter((c) => c.route === "email").length, forms = ticked.filter((c) => c.route === "form").length;
    const send = ui.button({ label: `Send ${ticked.length} ticked`, icon: "send", variant: "primary", onClick: async () => {
      const ok = await ui.confirm({ title: `Send ${ticked.length} item${ticked.length === 1 ? "" : "s"}?`, confirmLabel: "Send", icon: "send",
        message: `${emails} email${emails === 1 ? "" : "s"} through Gmail and ${forms} form${forms === 1 ? "" : "s"} on your laptop, one after another. It stops at a captcha, a login or any error. Nothing here can be undone.` });
      if (!ok) return;
      try { await api(`/api/batches/${b.id}/send`, { method: "POST", json: { confirm: true, item_ids: ticked.map((c) => c.item_id) } }); ui.toast("Sending started.", { kind: "good" }); load(); } catch (e) { fail(e); }
    } });
    send.disabled = !ticked.length || b.state !== "ready";
    const discard = ui.button({ label: "Discard batch", variant: "ghost", size: "sm", onClick: async () => {
      try { await api(`/api/batches/${b.id}/discard`, { method: "POST", json: {} }); open = null; load(); } catch (e) { fail(e); }
    } });
    discard.disabled = b.state === "sending";
    const prog = b.progress ? el("p", { class: "ui-hint" }, `Sending: ${b.progress.done} of ${b.progress.total} done…`) : null;
    root.replaceChildren(back, el("h2", { class: "ui-title" }, `${b.slot_name}: ${b.items.length} ${b.kind}`),
      el("p", { class: "ui-hint" }, `Built ${when(b.created)}. Untick anything you do not want to send. ${b.undo}`),
      ...(b.notes || []).map((n) => el("p", { class: "bt-note" }, "Left out: " + n)), prog,
      ...b.items.map((c) => (c.gone ? el("p", { class: "ui-hint" }, c.title) : card(b, c))), el("div", { class: "bt-sendbar" }, send, discard));
  }

  function paint() {
    if (error) { root.replaceChildren(el("p", { class: "ui-hint" }, error)); return; }
    if (!data) { root.replaceChildren(el("p", { class: "ui-hint" }, "Loading…")); return; }
    if (open) { batchView(open); return; }
    const c = data.counters;
    const stat = (n, label) => el("div", { class: "bt-stat" }, el("b", {}, String(n)), el("span", {}, label));
    const rows = data.batches.map((b) => el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" },
      el("span", { class: "ui-row__title" }, `${b.slot_name}: ${b.items} ${b.kind}`),
      el("span", { class: "ui-row__sub" }, `${when(b.created)} · ${b.state}${b.sent ? ` · ${b.sent} sent` : ""}${b.stopped ? " · stopped" : ""}`)),
      b.state === "ready" || b.state === "sending" || b.state === "done" ? ui.button({ label: b.state === "done" ? "View" : "Review", variant: "tonal", size: "sm", onClick: async () => {
        try { open = await api(`/api/batches/${b.id}`); paint(); } catch (e) { fail(e); }
      } }) : null));
    root.replaceChildren(el("h2", { class: "ui-title" }, "Batches"),
      el("div", { class: "bt-stats" }, stat(`${c.applications}${c.applications_cap ? "/" + c.applications_cap : ""}`, "applied today"), stat(`${c.messages}${c.messages_cap ? "/" + c.messages_cap : ""}`, "messages today")),
      data.paused ? el("p", { class: "bt-warn" }, "Panic is on: no batch is built or sent.") : null,
      !data.formfill.enabled ? el("p", { class: "ui-hint" }, "Form filling is off, so web applications show as Open + copy. Emails still go from the batch.") : null,
      el("section", { class: "ui-stack" }, ui.eyebrow("01", "Batches"), rows.length ? el("div", { class: "ui-list ui-list--inset" }, ...rows) : el("p", { class: "ui-hint" }, "No batches yet.")),
      el("section", {}, ui.eyebrow("02", "Schedule"), slotsSection()), settingsSection());
  }

  paint();
  load();
}
