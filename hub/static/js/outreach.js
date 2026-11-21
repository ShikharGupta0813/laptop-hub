// Outreach: pick up to 20 people a day, read the drafted emails as a list, send the whole batch with ONE tap (a few minutes apart).
// Nothing is sent until you tap Send on a reviewed batch. You type every address yourself; the hub never guesses or looks one up.
// Anyone on the opt-out list is never written to. One data call (GET /api/outreach) fills every tab; no timer.
// Every string goes in with textContent. Routes: docs/api/outreach.md.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/outreach.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const field = (label, c, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), c, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "200", ...attrs }); i.value = value || ""; return i; };
const safeHref = (url) => (/^https:\/\//i.test(url || "") ? url : null);

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "oc ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", tab = "people";
  const picked = new Set(), dueSel = new Set();

  async function load() {
    try { data = await api("/api/outreach"); error = ""; } catch (e) { error = e.message; }
    paint();
  }
  async function act(call, okMsg) {
    try { const r = await call(); if (okMsg) ui.toast(okMsg, { kind: "good" }); await load(); return r; } catch (e) { fail(e); return null; }
  }

  // ---- add a person ----
  function personSheet(pre = {}) {
    const name = input(pre.name, { placeholder: "Full name", maxlength: "80" });
    const email = input("", { type: "email", inputmode: "email", placeholder: "name@company.com", autocomplete: "off" });
    const company = input(pre.company, { placeholder: "Company", maxlength: "120" });
    const role = input(pre.role, { placeholder: "Role you are writing about (optional)", maxlength: "120" });
    const known = input(pre.how_known, { placeholder: "How you know them (optional)", maxlength: "160" });
    const link = input(pre.url || "", { type: "url", inputmode: "url", placeholder: "https://", maxlength: "500" });
    const box = el("input", { type: "checkbox", id: "oc-confirm" });
    const origin = pre.contact_id ? "network" : pre.url ? "watch" : "manual";
    ui.sheet({ title: "Add a person", class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
      el("p", { class: "ui-hint" }, "Type the address yourself. The hub never guesses an address from a name or a company, and never looks one up."),
      field("Name", name), field("Email address", email), field("Company", company), field("Role", role), field("How you know them", known),
      field(origin === "network" ? "Public page (optional)" : "Public page where you found them", link, "An https link to the page or profile. It is shown in the email and never opened by the hub."),
      el("label", { class: "oc-check" }, box, el("span", {}, "I got this address from the person or from their own public page. I did not guess it."))),
    actions: [{ label: "Add", variant: "primary", icon: "check", onClick: async () => {
      try {
        await api("/api/outreach/people", { method: "POST", json: { name: name.value, email: email.value, company: company.value, role: role.value, known_from: known.value,
          source_url: link.value, address_confirmed: box.checked, origin, contact_id: pre.contact_id || "" } });
        ui.toast("Added.", { kind: "good" }); load(); return true;
      } catch (e) { fail(e); return false; }
    } }] });
  }

  // ---- tabs ----
  function peopleTab() {
    const free = data.people.filter((p) => p.state === "not written to yet");
    const left = data.room.left;
    const cap = Math.min(left, data.settings.hard_batch);
    const cands = data.candidates;
    const chips = [...cands.contacts.slice(0, 12).map((c) => ui.button({ label: c.name, variant: "ghost", size: "sm", onClick: () => personSheet({ name: c.name, company: c.company, role: c.role, how_known: c.how_known, contact_id: c.id }) })),
      ...cands.companies.slice(0, 12).map((c) => ui.button({ label: c.label, variant: "ghost", size: "sm", onClick: () => personSheet({ company: c.label, url: c.url }) }))];
    const rows = data.people.map((p) => {
      const can = p.state === "not written to yet";
      const cb = el("input", { type: "checkbox", "aria-label": `Pick ${p.name}`, disabled: !can || null });
      cb.checked = can && picked.has(p.id);
      cb.addEventListener("change", () => { if (cb.checked) picked.add(p.id); else picked.delete(p.id); paint(); });
      return el("article", { class: "ui-card ui-stack oc-card" }, el("div", { class: "oc-row" }, cb, el("div", { class: "oc-who" },
        el("p", { class: "oc-title" }, p.name), el("span", { class: "oc-sub" }, [p.company, p.role].filter(Boolean).join(" · ") || p.email), el("span", { class: "oc-sub" }, p.email)),
        ui.badge(p.state, p.state === "opted out" ? "warn" : p.state === "replied" ? "good" : "")),
        el("div", { class: "oc-actions" },
          safeHref(p.source_url) ? el("a", { class: "ui-btn ui-btn--ghost ui-btn--sm", href: p.source_url, target: "_blank", rel: "noopener noreferrer" }, "Source page") : null,
          ui.button({ label: "Never write to them", variant: "ghost", size: "sm", onClick: async () => {
            const ok = await ui.confirm({ title: "Add to the opt-out list?", message: `${p.name} will never be drafted or sent to again.`, confirmLabel: "Add", danger: true });
            if (ok) act(() => api("/api/outreach/optout", { method: "POST", json: { person_id: p.id } }), "Added to your opt-out list.");
          } }),
          ui.button({ label: "Remove", variant: "ghost", size: "sm", onClick: () => act(() => api(`/api/outreach/people/${p.id}`, { method: "DELETE" }), "Removed.") })));
    });
    const n = [...picked].filter((id) => free.some((p) => p.id === id)).length;
    return [el("p", { class: "ui-hint" }, `${left} of ${data.room.limit} emails left today (${data.room.sent_today} sent, ${data.room.queued} waiting). At most ${data.settings.hard_batch} people per batch.`),
      el("div", { class: "oc-actions" }, ui.button({ label: "Add a person", icon: "plus", variant: "primary", onClick: () => personSheet() }),
        ui.button({ label: n ? `Draft ${n} email${n === 1 ? "" : "s"}` : "Draft emails", icon: "edit", variant: "tonal", disabled: !n || n > cap || null, onClick: async () => {
          const r = await act(() => api("/api/outreach/prepare", { method: "POST", json: { person_ids: [...picked] } }), "Drafted. Nothing is sent yet: read them first.");
          if (r) { picked.clear(); tab = "batch"; paint(); }
        } })),
      n > cap ? el("p", { class: "oc-warn" }, `You picked ${n}, but only ${cap} fit today.`) : null,
      chips.length ? el("section", { class: "ui-stack" }, ui.eyebrow("", "Start from"), el("div", { class: "oc-actions" }, ...chips)) : null,
      ...(rows.length ? rows : [el("p", { class: "ui-hint" }, "Nobody yet. Add people you want to write to, with a link to where you found them.")])];
  }

  function draftSheet(batch, card) {
    const subject = input(card.draft.subject, { maxlength: "200", "aria-label": "Subject" });
    const body = el("textarea", { class: "ui-input oc-draft", "aria-label": "Email text" }); body.value = card.draft.body;
    ui.sheet({ title: `Edit: ${card.title.slice(0, 40)}`, class: "st-sheet", content: el("div", { class: "ui-stack ui-stack--lg" },
      el("p", { class: "ui-hint" }, `To ${card.contact_email}. Keep the line that offers to stop.`), field("Subject", subject), body),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      try { await api(`/api/batches/${batch.id}/items/${card.item_id}`, { method: "PATCH", json: { draft: { subject: subject.value, body: body.value } } }); load(); return true; } catch (e) { fail(e); return false; }
    } }] });
  }

  function batchCard(b) {
    const sendable = b.items.filter((c) => c.ticked && c.sendable && !["sent", "applied"].includes(c.result?.status));
    const sending = b.state === "sending";
    const cards = b.items.map((c) => {
      const cb = el("input", { type: "checkbox", "aria-label": `Send to ${c.contact_email}`, disabled: sending || !c.sendable || null });
      cb.checked = !!c.ticked;
      cb.addEventListener("change", () => act(() => api(`/api/batches/${b.id}/items/${c.item_id}`, { method: "PATCH", json: { ticked: cb.checked } })));
      return el("article", { class: "ui-card ui-stack oc-card" }, el("div", { class: "oc-row" }, cb, el("div", { class: "oc-who" },
        el("p", { class: "oc-title" }, c.draft.subject), el("span", { class: "oc-sub" }, `To ${c.contact_email}`)),
        c.result ? ui.badge(c.result.status.replace("_", " "), c.result.status === "sent" ? "good" : "warn") : null),
        el("p", { class: "oc-preview" }, c.draft.preview), ...(c.warnings || []).map((w) => el("p", { class: "oc-warn" }, w)),
        c.result?.message ? el("p", { class: "oc-sub" }, c.result.message) : null,
        el("div", { class: "oc-actions" }, ui.button({ label: "Read and edit", variant: "tonal", size: "sm", disabled: sending || null, onClick: () => draftSheet(b, c) })));
    });
    const send = ui.button({ label: sending ? "Sending…" : `Send ${sendable.length} email${sendable.length === 1 ? "" : "s"} with one tap`, icon: "send", variant: "primary", disabled: sending || !sendable.length || null, onClick: async () => {
      const ok = await ui.confirm({ title: `Send ${sendable.length} email${sendable.length === 1 ? "" : "s"}?`, confirmLabel: "Send",
        message: `They go from your own Gmail, one by one, about ${Math.round(data.settings.gap_seconds / 60)} minutes apart. An email cannot be taken back. Nobody on your opt-out list is sent to.` });
      if (!ok) return;
      act(() => api(`/api/outreach/batches/${b.id}/send`, { method: "POST", json: { confirm: true } }), "Sending started. Open this screen later to see each result.");
    } });
    return el("section", { class: "ui-stack" }, ui.eyebrow("", `${b.slot_name} (${b.counts.items})`), ...b.notes.map((n) => el("p", { class: "ui-hint" }, n)), ...cards,
      el("div", { class: "oc-actions" }, send, ui.button({ label: "Refresh", icon: "refresh", variant: "ghost", onClick: load }),
        ui.button({ label: "Discard batch", variant: "ghost", disabled: sending || null, onClick: async () => {
          const ok = await ui.confirm({ title: "Discard this batch?", message: "The drafts are thrown away. Nothing was sent.", confirmLabel: "Discard", danger: true });
          if (ok) act(() => api(`/api/outreach/batches/${b.id}/discard`, { method: "POST", json: {} }), "Discarded.");
        } })));
  }

  function batchTab() {
    return data.batches.length ? data.batches.map(batchCard) : [el("p", { class: "ui-hint" }, "No batch to review. Pick people on the first tab and tap Draft emails.")];
  }

  function followTab() {
    const due = data.followups_due;
    const n = [...dueSel].filter((id) => due.some((d) => d.id === id)).length;
    return [el("p", { class: "ui-hint" }, `One follow-up per person, ${data.settings.followup_days} days after the first email, and only if they have not replied. It is drafted into its own batch and needs its own tap.`),
      ...(due.length ? due.map((d) => {
        const cb = el("input", { type: "checkbox", "aria-label": `Follow up with ${d.name}` }); cb.checked = dueSel.has(d.id);
        cb.addEventListener("change", () => { if (cb.checked) dueSel.add(d.id); else dueSel.delete(d.id); paint(); });
        return el("article", { class: "ui-card ui-stack oc-card" }, el("div", { class: "oc-row" }, cb, el("div", { class: "oc-who" }, el("p", { class: "oc-title" }, d.name),
          el("span", { class: "oc-sub" }, `${d.company || ""} · first email ${d.first_sent}`))));
      }) : [el("p", { class: "ui-hint" }, "No follow-ups due.")]),
      due.length ? ui.button({ label: n ? `Draft ${n} follow-up${n === 1 ? "" : "s"}` : "Draft follow-ups", icon: "edit", variant: "primary", disabled: !n || null, onClick: async () => {
        const r = await act(() => api("/api/outreach/followups/prepare", { method: "POST", json: { person_ids: [...dueSel] } }), "Drafted. Nothing is sent yet.");
        if (r) { dueSel.clear(); tab = "batch"; paint(); }
      } }) : null];
  }

  function optoutTab() {
    const addr = input("", { type: "email", inputmode: "email", placeholder: "Address that must never be written to" });
    return [el("p", { class: "ui-hint" }, "Anyone here is never drafted and never sent to. A reply like \"no thanks\" shows up as a suggestion on the Job tracker; you confirm it."),
      el("div", { class: "oc-actions" }, addr, ui.button({ label: "Add", icon: "plus", variant: "tonal", onClick: () => act(() => api("/api/outreach/optout", { method: "POST", json: { address: addr.value } }), "Added.") })),
      ...(data.optout.length ? data.optout.map((o) => el("article", { class: "ui-card ui-stack oc-card" }, el("div", { class: "oc-row" }, el("div", { class: "oc-who" },
        el("p", { class: "oc-title" }, o.name || o.address), el("span", { class: "oc-sub" }, `${o.address} · ${o.why} · ${o.at.slice(0, 10)}`)),
        ui.button({ label: "Remove", variant: "ghost", size: "sm", onClick: async () => {
          const ok = await ui.confirm({ title: "Take them off the opt-out list?", message: "Only do this if they asked you to write again.", confirmLabel: "Remove" });
          if (ok) act(() => api(`/api/outreach/optout/${o.id}`, { method: "DELETE" }), "Removed.");
        } })))) : [el("p", { class: "ui-hint" }, "The list is empty.")])];
  }

  function paint() {
    if (error) { root.replaceChildren(el("p", { class: "ui-hint" }, error)); return; }
    if (!data) { root.replaceChildren(el("p", { class: "ui-hint" }, "Loading…")); return; }
    const seg = ui.segmented({ label: "Outreach sections", value: tab, onChange: (v) => { tab = v; paint(); },
      options: [{ value: "people", label: "People" }, { value: "batch", label: `Batch${data.batches.length ? " (" + data.batches.length + ")" : ""}` },
        { value: "follow", label: `Follow-ups${data.followups_due.length ? " (" + data.followups_due.length + ")" : ""}` }, { value: "optout", label: "Opt-out" }] });
    root.replaceChildren(el("h2", { class: "ui-title" }, "Outreach"), el("p", { class: "ui-hint" }, data.rules.join(" ")), seg,
      ...(tab === "people" ? peopleTab() : tab === "batch" ? batchTab() : tab === "follow" ? followTab() : optoutTab()));
    ui.renumberEyebrows(root);
  }

  paint();
  load();
}
