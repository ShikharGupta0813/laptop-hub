// Networking drafts: a referral request, a note to a recruiter, a thank-you after an interview, a follow-up after no reply, plus a small
// contacts list with "nudge due" suggestions. Everything here is a DRAFT built only from your confirmed facts: you press Copy and send
// it yourself (LinkedIn and Indeed are never opened or automated). "I sent this" only writes today's date down. The screen makes ONE
// data request when it opens (GET /api/networking); nothing polls. Routes: docs/api/networking.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/networking.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const NEEDS = { referral: ["contact", "how"], recruiter: ["contact", "how"], thank_you: ["contact", "topic"], follow_up: ["contact", "date"] };

async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied. Paste it where you want to send it.", { kind: "good" }); }
  catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}

const field = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const input = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "200", ...attrs }); i.value = value ?? ""; return i; };

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "nw ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, error = "", current = null, kind = "referral";
  let form = { application_id: "", company: "", role: "", contact_id: "", contact_name: "", how_known: "", topic: "", sent_on: "", ai: false };

  async function load() {
    try { data = await api("/api/networking"); error = ""; form.ai = form.ai || data.settings.ai; }
    catch (e) { error = e.message; }
    paint();
  }

  function paint() {
    root.replaceChildren();
    if (error) { root.append(ui.notice(error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: load })); return; }
    if (!data) { root.append(ui.skeleton("list", 3)); return; }
    root.append(ui.notice("Drafts only. Nothing is sent from here: you copy a draft and send it yourself. LinkedIn and Indeed are never opened or automated.", { tone: "info" }),
      nudgeSection(), makeSection(), current ? draftCard(current, true) : null, draftsSection(), contactsSection());
    ui.renumberEyebrows(root);   // a hidden section must not leave a gap (01, 02, 04)
  }

  // ---- nudges (suggestions only) ----
  function nudgeSection() {
    if (!data.nudges.length) return null;
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", `Nudge due (${data.settings.nudge_days}+ days)`),
      el("p", { class: "nw-note" }, "Only a suggestion. Nothing happens unless you tap."),
      ...data.nudges.map((n) => el("div", { class: "nw-row" },
        el("span", { class: "nw-row__text" }, n.text),
        ui.button({ label: "Draft follow-up", icon: "pen", variant: "tonal", size: "sm", onClick: () => {
          kind = "follow_up";
          form = { ...form, application_id: n.application_id || "", contact_id: n.contact_id || "", company: n.company || form.company, role: n.title || "" };
          paint(); root.querySelector(".nw-make")?.scrollIntoView({ block: "start" });
        } }))));
  }

  // ---- make a draft ----
  function makeSection() {
    const seg = ui.segmented({ options: data.kinds.map((k) => ({ value: k.id, label: k.label })), value: kind, label: "Kind of draft", onChange: (v) => { kind = v; paint(); } });
    const need = NEEDS[kind];
    const app = el("select", { class: "ui-input" }, el("option", { value: "" }, "Not from an application"),
      ...data.applications.map((a) => el("option", { value: a.id }, [a.title, a.company].filter(Boolean).join(" at ") || a.id)));
    app.value = form.application_id;
    app.addEventListener("change", () => {
      form.application_id = app.value;
      const a = data.applications.find((x) => x.id === app.value);
      if (a) { form.company = a.company || form.company; form.role = a.title || form.role; paint(); }
    });
    const company = input(form.company, { placeholder: "Company" });
    const role = input(form.role, { placeholder: "Role" });
    const contact = el("select", { class: "ui-input" }, el("option", { value: "" }, "Someone not in my list"),
      ...data.contacts.map((c) => el("option", { value: c.id }, [c.name, c.company].filter(Boolean).join(" at "))));
    contact.value = form.contact_id;
    const name = input(form.contact_name, { placeholder: "First name is enough", maxlength: "80" });
    const how = input(form.how_known, { placeholder: "Example: the Python meetup", maxlength: "200" });
    const topic = input(form.topic, { placeholder: "Example: the data pipeline", maxlength: "160" });
    const date = el("input", { class: "ui-input", type: "date" }); date.value = form.sent_on;
    const ai = ui.switchControl({ checked: form.ai, label: "Reword with AI" });
    const sync = () => { Object.assign(form, { company: company.value, role: role.value, contact_id: contact.value, contact_name: name.value,
      how_known: how.value, topic: topic.value, sent_on: date.value, ai: ai.input.checked }); };
    [company, role, name, how, topic, date, contact].forEach((n) => n.addEventListener("change", sync));
    const go = ui.button({ label: "Make the draft", icon: "pen", variant: "primary", onClick: () => ui.busy(go, async () => {
      sync();
      const body = { kind, ai: form.ai };
      for (const k of ["application_id", "company", "role", "contact_id", "contact_name", "how_known", "topic", "sent_on"]) if (form[k]) body[k] = form[k];
      if (kind === "thank_you" && form.contact_name) body.interviewer = form.contact_name;
      try { current = await api("/api/networking/drafts", { method: "POST", json: body }); ui.toast("Draft ready. Read it before you copy it.", { kind: "good" }); await load(); }
      catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-card ui-stack nw-make" }, ui.eyebrow("02", "Write a draft"), seg,
      field("From an application (optional)", app), field("Company", company), field("Role", role),
      need.includes("contact") ? field("Who is it for?", contact) : null,
      !form.contact_id && need.includes("contact") ? field(kind === "thank_you" ? "Interviewer's first name" : "Their first name", name) : null,
      need.includes("how") ? field("How you know them (optional)", how, "Only if it is true. It goes in the draft in your words.") : null,
      need.includes("topic") ? field("What you talked about (optional)", topic) : null,
      need.includes("date") ? field("When you first wrote (optional)", date) : null,
      el("div", { class: "ui-row nw-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Reword with AI"),
        el("span", { class: "ui-row__sub" }, "Sends only the posting text and your confirmed facts. Anything it invents is dropped.")), ai),
      go);
  }

  // ---- a draft ----
  function draftCard(d, fresh) {
    const sents = (d.cover?.blocks?.[0]?.sentences || []);
    const bad = sents.filter((s) => s.status === "unbacked");
    const text = el("pre", { class: "nw-text" }, d.text);
    const card = el("article", { class: "ui-card ui-stack nw-draft" + (d.blocked ? " is-blocked" : "") },
      el("div", { class: "nw-head" }, el("h3", { class: "nw-title" }, d.label), el("div", { class: "nw-badges" },
        ui.badge(`${d.words} words`, d.over_cap ? "warn" : "gray"), d.sent_on ? ui.badge(`sent ${d.sent_on}`, "good") : null,
        d.cover?.generator === "ai" ? ui.badge("AI wording", "accent") : null)),
      el("p", { class: "nw-sub" }, "Subject or first line: " + d.subject),
      d.cover?.ai_note ? el("p", { class: "nw-note" }, d.cover.ai_note) : null,
      text);
    if (d.blocked) {
      card.append(ui.notice("Not ready to copy: " + d.blockers.join(" "), { tone: "warn" }));
      for (const s of bad) card.append(fixRow(d, s));
      for (const label of (d.blockers.join(" ").match(/Fill in: [^.]+/g) || []).map((x) => x.slice(9))) card.append(fillRow(d, label));
    }
    const actions = el("div", { class: "nw-actions" },
      ui.button({ label: "Copy", icon: "copy", variant: "primary", size: "sm", disabled: d.blocked, onClick: () => copy(`${d.subject ? "Subject: " + d.subject + "\n\n" : ""}${d.text}`) }),
      ui.button({ label: "Copy text only", icon: "copy", variant: "tonal", size: "sm", disabled: d.blocked, onClick: () => copy(d.text) }),
      d.sent_on
        ? ui.button({ label: "Undo sent", icon: "refresh", variant: "ghost", size: "sm", onClick: () => act(`/api/networking/drafts/${d.id}/sent`, { undo: true }) })
        : ui.button({ label: "I sent this", icon: "check", variant: "tonal", size: "sm", onClick: () => act(`/api/networking/drafts/${d.id}/sent`, {}, "Date saved. REDLINE did not send anything.") }),
      ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
        try { await api(`/api/networking/drafts/${d.id}`, { method: "DELETE" }); if (current?.id === d.id) current = null; load(); } catch (e) { fail(e); }
      } }));
    card.append(actions);
    if (fresh) card.append(el("p", { class: "nw-note" }, "Paste it into your own mail or LinkedIn message and send it yourself."));
    return card;
  }

  async function act(path, json, message) {
    try { const r = await api(path, { method: "POST", json }); if (current?.id === r.id) current = r; if (message) ui.toast(message, { kind: "good" }); load(); } catch (e) { fail(e); }
  }

  function fixRow(d, s) {
    const edit = input(s.text, { maxlength: "600" });
    return el("div", { class: "nw-fix ui-stack" }, el("p", { class: "nw-warn" }, `"${s.text}" - ${s.reason || "not backed by your facts"}`), edit,
      el("div", { class: "nw-actions" },
        ui.button({ label: "Remove it", icon: "trash", variant: "tonal", size: "sm", onClick: () => resolve(d, { action: "remove", sentence: s.id }) }),
        ui.button({ label: "Use my rewrite", icon: "edit", variant: "tonal", size: "sm", onClick: () => resolve(d, { action: "edit", sentence: s.id, text: edit.value }) }),
        ui.button({ label: "This is true", icon: "check", variant: "ghost", size: "sm", onClick: async () => {
          if (await ui.confirm({ title: "Save as a fact?", message: "This sentence will be saved as a fact you confirmed and used in your drafts. Only do this if it is true.", confirmLabel: "It is true" })) resolve(d, { action: "confirm", sentence: s.id, fact_text: edit.value });
        } })));
  }

  function fillRow(d, label) {
    const v = input("", { placeholder: label });
    return el("div", { class: "nw-actions" }, v, ui.button({ label: "Fill in", icon: "check", variant: "tonal", size: "sm", onClick: () => resolve(d, { action: "fill", label, value: v.value }) }));
  }

  async function resolve(d, json) {
    try { const r = await api(`/api/networking/drafts/${d.id}/resolve`, { method: "POST", json }); current = r; load(); } catch (e) { fail(e); }
  }

  function draftsSection() {
    const rest = data.drafts.filter((d) => d.id !== current?.id).slice(0, 8);
    if (!rest.length) return null;
    return el("section", { class: "ui-stack" }, ui.eyebrow("03", "Earlier drafts"), ...rest.map((d) => draftCard(d, false)));
  }

  // ---- contacts ----
  function contactSheet(c) {
    const cur = c || { name: "", company: "", role: "", how_known: "", last_contact: "", note: "" };
    const name = input(cur.name, { maxlength: "80" }), company = input(cur.company, { maxlength: "120" }), role = input(cur.role, { maxlength: "120" });
    const how = input(cur.how_known, { maxlength: "200", placeholder: "Example: worked together at a past job" });
    const last = el("input", { class: "ui-input", type: "date" }); last.value = cur.last_contact || "";
    const note = input(cur.note, { maxlength: "300" });
    ui.sheet({ title: c ? "Edit contact" : "Add a contact", content: el("div", { class: "ui-stack" }, field("Name", name), field("Company", company), field("Their role", role),
      field("How you know them", how), field("Last time you wrote or spoke", last, "Leave empty if you have not yet."), field("Note", note)),
    actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      try {
        await api(c ? `/api/networking/contacts/${c.id}` : "/api/networking/contacts", { method: c ? "PUT" : "POST",
          json: { name: name.value, company: company.value, role: role.value, how_known: how.value, last_contact: last.value, note: note.value } });
        ui.toast("Saved.", { kind: "good" }); load(); return true;
      } catch (e) { fail(e); return false; }
    } }] });
  }

  function contactsSection() {
    const rows = data.contacts.map((c) => {
      const due = data.nudges.find((n) => n.contact_id === c.id);
      return el("div", { class: "nw-row" },
        el("div", { class: "nw-row__text" }, el("strong", {}, c.name), el("span", { class: "nw-sub" }, [c.role, c.company].filter(Boolean).join(" at ") || "No company"),
          el("span", { class: "nw-sub" }, [c.how_known, c.last_contact ? "last contact " + c.last_contact : "no contact date"].filter(Boolean).join(" - ")),
          due ? ui.badge(`nudge due: ${due.days} days`, "warn") : null),
        el("div", { class: "nw-actions" },
          ui.button({ label: "Edit", icon: "edit", variant: "ghost", size: "sm", onClick: () => contactSheet(c) }),
          ui.button({ label: "Delete", icon: "trash", variant: "ghost", size: "sm", onClick: async () => {
            if (await ui.confirm({ title: "Delete this contact?", message: c.name, confirmLabel: "Delete", danger: true })) {
              try { await api(`/api/networking/contacts/${c.id}`, { method: "DELETE" }); load(); } catch (e) { fail(e); }
            }
          } })));
    });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("04", "Contacts"),
      rows.length ? el("div", { class: "ui-stack" }, ...rows) : ui.emptyState({ icon: "users", title: "No contacts yet", body: "Add people you may want to write to. The list stays on this laptop.", compact: true }),
      ui.button({ label: "Add a contact", icon: "plus", variant: "tonal", size: "sm", onClick: () => contactSheet(null) }));
  }

  load();
}
