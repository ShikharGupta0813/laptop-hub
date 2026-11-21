// Job preferences: named search profiles (work mode, remote only, kinds, places, minimum pay, keywords, companies to avoid,
// seniority, languages, sources, daily quota, schedule) and your region. They are HARD filters in the finder and the batches:
// a posting that does not match is never shown. Routes: docs/api/jobprefs.md. Every string goes in with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor } from "./listeditor.js";

const CSS = "/css/jobprefs.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const DAYS = [[0, "Mon"], [1, "Tue"], [2, "Wed"], [3, "Thu"], [4, "Fri"], [5, "Sat"], [6, "Sun"]];
const MODE_NAME = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };
const csv = (v) => (Array.isArray(v) ? v.join(", ") : "");
const split = (text) => String(text || "").split(/[,\n;]/).map((x) => x.trim()).filter(Boolean);

function toggles(options, selected, label) {
  const picked = new Set(selected);
  const wrap = el("div", { class: "jp-chips", role: "group", "aria-label": label });
  for (const [value, text] of options) {
    const b = el("button", { type: "button", class: "jp-chip" + (picked.has(value) ? " is-on" : ""), "aria-pressed": String(picked.has(value)) }, text);
    b.addEventListener("click", () => {
      if (picked.has(value)) picked.delete(value); else picked.add(value);
      b.classList.toggle("is-on", picked.has(value)); b.setAttribute("aria-pressed", String(picked.has(value)));
    });
    wrap.append(b);
  }
  return { node: wrap, get: () => options.map(([v]) => v).filter((v) => picked.has(v)) };
}

const labelled = (label, control, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const group = (label, control, hint) => el("div", { class: "ui-field" }, el("span", { class: "ui-label" }, label), control, hint ? el("span", { class: "ui-hint" }, hint) : null);
const text = (value, attrs = {}) => { const i = el("input", { class: "ui-input", type: "text", maxlength: "400", ...attrs }); i.value = value ?? ""; return i; };

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "jp ui-stack ui-stack--lg" });
  container.append(root);
  let data = null, sources = [], error = "";

  async function load() {
    try {
      const [d, s] = await Promise.all([api("/api/jobprefs"), api("/api/opps/sources").catch(() => ({ sources: [] }))]);
      data = d; sources = s.sources || []; error = "";
    } catch (e) { error = e.message; }
    paint();
  }

  function summary(p) {
    const bits = [];
    bits.push(p.remote_only ? "Remote only" : (p.work_modes.length ? p.work_modes.map((m) => MODE_NAME[m]).join(" / ") : "Any mode"));
    if (p.kinds.length) bits.push(p.kinds.map((k) => (data.options.kinds.find(([v]) => v === k) || [k, k])[1]).join(", "));
    if (p.min_pay) bits.push(`from ${p.min_pay.amount} ${p.min_pay.currency || ""}/${p.min_pay.unit}`.replace("  ", " "));
    if (p.daily_quota) bits.push(`${p.daily_quota} a day`);
    if (p.schedule) bits.push(`at ${p.schedule.time}`);
    return bits.join(" · ");
  }

  function profileSheet(p) {
    const cur = p || { name: "", enabled: true, remote_only: false, work_modes: [], kinds: [], locations: [], timezones: [], include: [], exclude: [],
      avoid_companies: [], seniority: [], languages: [], sources: [], min_pay: null, daily_quota: 0, schedule: null, include_required: false };
    const name = text(cur.name, { maxlength: "60", placeholder: "Remote AI training gigs" });
    const enabled = ui.switchControl({ checked: cur.enabled !== false, label: "On" });
    const remoteOnly = ui.switchControl({ checked: cur.remote_only === true, label: "Remote only" });
    const modes = toggles(data.options.modes.map((m) => [m, MODE_NAME[m]]), cur.work_modes, "Work modes");
    const kinds = toggles(data.options.kinds, cur.kinds, "Employment kinds");
    const levels = toggles(data.options.levels.map((l) => [l, l[0].toUpperCase() + l.slice(1)]), cur.seniority, "Seniority");
    const srcPick = toggles(sources.map((s) => [s.id, s.name]), cur.sources, "Sources");
    const places = text(csv(cur.locations), { placeholder: "Berlin, Remote EU" });
    const tzs = text(csv(cur.timezones), { placeholder: "CET, UTC+1" });
    const include = text(csv(cur.include), { placeholder: "python, api" });
    const exclude = text(csv(cur.exclude), { placeholder: "crypto, unpaid" });
    const avoid = text(csv(cur.avoid_companies), { placeholder: "Company names" });
    const langs = text(csv(cur.languages), { placeholder: "English, German" });
    const incReq = ui.switchControl({ checked: cur.include_required === true, label: "Must mention one of them" });
    const amount = el("input", { class: "ui-input", type: "number", min: "0", step: "any", inputmode: "decimal", placeholder: "Amount" }); amount.value = cur.min_pay?.amount ?? "";
    const currency = text(cur.min_pay?.currency || "", { maxlength: "3", placeholder: "USD" });
    const unit = el("select", { class: "ui-input" }, ...data.options.units.map((u) => el("option", { value: u }, `per ${u}`))); unit.value = cur.min_pay?.unit || "hour";
    const quota = el("input", { class: "ui-input", type: "number", min: "0", max: "200", inputmode: "numeric" }); quota.value = cur.daily_quota || 0;
    const time = el("input", { class: "ui-input", type: "time" }); time.value = cur.schedule?.time || "";
    const kind = el("select", { class: "ui-input" }, el("option", { value: "daily" }, "Every day"), el("option", { value: "weekdays" }, "Weekdays")); kind.value = cur.schedule?.kind === "weekdays" ? "weekdays" : "daily";
    const body = el("div", { class: "ui-stack ui-stack--lg" },
      labelled("Name", name),
      el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "On")), enabled),
      el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Remote only"),
        el("span", { class: "ui-row__sub" }, "A posting must clearly say remote. Remote jobs limited to another region are skipped too.")), remoteOnly),
      group("Work mode", modes.node, "Leave all off for any mode."),
      group("Employment", kinds.node, "Leave all off for any kind."),
      labelled("Places (for hybrid and on-site)", places, "Comma separated."),
      labelled("Time zones", tzs, "Only used to warn you."),
      group("Minimum pay", el("div", { class: "jp-pay" }, amount, currency, unit), "Used only when a posting states pay in the same currency."),
      labelled("Keywords to include", include), el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Must mention one of them")), incReq),
      labelled("Keywords to exclude", exclude, "A posting that mentions one is never shown."),
      labelled("Companies to avoid", avoid),
      group("Seniority", levels.node, "Leave all off for any level."),
      labelled("Languages you work in", langs, "A posting that requires another language is skipped."),
      group("Sources", srcPick.node, "Leave all off to use every source."),
      labelled("Daily quota (0 = none)", quota, "The most items this profile puts in batches per day."),
      group("Default schedule", el("div", { class: "jp-pay" }, kind, time), "A batch slot that uses this profile and has no time of its own runs then."));
    ui.sheet({ title: p ? "Edit profile" : "New profile", class: "st-sheet st-sheet--tall", content: body, actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
      const payload = { name: name.value, enabled: enabled.input.checked, remote_only: remoteOnly.input.checked, work_modes: modes.get(), kinds: kinds.get(),
        locations: split(places.value), timezones: split(tzs.value), include: split(include.value), exclude: split(exclude.value), include_required: incReq.input.checked,
        avoid_companies: split(avoid.value), seniority: levels.get(), languages: split(langs.value), sources: srcPick.get(), daily_quota: Number(quota.value || 0),
        min_pay: amount.value ? { amount: Number(amount.value), currency: currency.value.trim().toUpperCase(), unit: unit.value } : null,
        schedule: time.value ? { kind: kind.value, time: time.value } : null };
      try { await api(p ? `/api/jobprefs/profiles/${p.id}` : "/api/jobprefs/profiles", { method: p ? "PUT" : "POST", json: payload }); ui.toast("Saved.", { kind: "good" }); load(); return true; }
      catch (e) { fail(e); return false; }
    } }] });
  }

  function ownerCard() {
    const regions = text(csv(data.owner.regions), { placeholder: "India, EU" });
    const tzs = text(csv(data.owner.timezones), { placeholder: "IST" });
    const honour = ui.switchControl({ checked: data.owner.honour_region !== false, label: "Skip remote jobs limited to other regions" });
    const auth = text(csv(data.owner.work_auth), { placeholder: "India" });
    const notice = text(data.owner.notice_days ?? "", { placeholder: "30", inputmode: "numeric", maxlength: "3" });
    const langs = text(csv(data.owner.languages), { placeholder: "Hindi, English" });
    const relocate = ui.switchControl({ checked: data.owner.relocate === true, label: "Willing to relocate" });
    const save = ui.button({ label: "Save", icon: "check", variant: "primary", size: "sm", onClick: () => ui.busy(save, async () => {
      const nd = notice.value.trim();
      if (nd && !/^\d{1,3}$/.test(nd)) { ui.toast("Notice period is a whole number of days.", { kind: "warn" }); return; }
      try { data = await api("/api/jobprefs/owner", { method: "PUT", json: { regions: split(regions.value), timezones: split(tzs.value), honour_region: honour.input.checked,
        work_auth: split(auth.value), notice_days: nd ? Number(nd) : null, languages: split(langs.value), relocate: relocate.input.checked } }); ui.toast("Saved.", { kind: "good" }); paint(); }
      catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", "Where you live"),
      el("p", { class: "ui-hint" }, "Countries or regions you may work from. A posting that says remote but only for the US is skipped when you are not in the US."),
      labelled("Your region(s)", regions, "Country or region names, comma separated."), labelled("Your time zone", tzs),
      el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Skip remote jobs limited to other regions")), honour),
      el("p", { class: "ui-hint" }, "Used by the go / no-go label on every find: a posting that asks for something you cannot give is marked skip, with the reason. Nothing is hidden."),
      labelled("Countries you may work in without sponsorship", auth, "For example India. Used for lines like 'must be authorised to work in the US'."),
      labelled("Your notice period (days)", notice, "Used for 'immediate joiners' and 'notice of 30 days max'. Leave empty if not set."),
      labelled("Languages you speak", langs, "English is always counted. Used for 'fluent German required'."),
      el("div", { class: "ui-row jp-switchrow" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Willing to relocate")), relocate),
      save);
  }

  function checker() {
    const title = text("", { placeholder: "Job title", maxlength: "200" });
    const desc = el("textarea", { class: "ui-input", rows: "5", maxlength: "6000", placeholder: "Paste the posting text" });
    const out = el("div", { class: "ui-stack" });
    const run = ui.button({ label: "Check against my profiles", icon: "search", variant: "tonal", onClick: () => ui.busy(run, async () => {
      try {
        const r = await api("/api/jobprefs/check", { method: "POST", json: { title: title.value, description: desc.value } });
        const a = r.analysis;
        out.replaceChildren(
          el("p", {}, `Read as: ${a.mode}` + (a.geo.restricted ? `, limited to ${a.geo.text}` : a.geo.worldwide ? ", worldwide" : "") + (a.pay_text ? `, ${a.pay_text}` : "") + (a.kinds.length ? `, ${a.kinds.join("/")}` : "") + (a.level ? `, ${a.level} level` : "")),
          ...r.profiles.map((p) => el("p", { class: "jp-verdict " + (p.ok ? "is-ok" : "is-no") }, `${p.profile_name}: ` + (p.ok ? "matches" + (p.warns.length ? ` (${p.warns.join("; ")})` : "") : `skipped, ${p.fails.join("; ")}`))));
      } catch (e) { fail(e); }
    }) });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("03", "Test a posting"), el("p", { class: "ui-hint" }, "Nothing is saved. See how a posting would be read and which profiles accept it."), title, desc, run, out);
  }

  function paint() {
    if (error) { root.replaceChildren(el("p", { class: "ui-hint" }, error)); return; }
    if (!data) { root.replaceChildren(el("p", { class: "ui-hint" }, "Loading…")); return; }
    const ed = listEditor({ title: "Search profiles", hint: "With no profile on, nothing is filtered. A posting that fails ANY rule of a profile is not shown for it; it needs to pass at least one profile.",
      items: data.profiles, addLabel: "Add profile", empty: "No profiles yet. Add one, or start from a template below.",
      rowOf: (p) => ({ icon: "target", tone: "accent", title: p.name, sub: summary(p), enabled: p.enabled !== false }),
      onToggle: async (p, on) => { await api(`/api/jobprefs/profiles/${p.id}`, { method: "PUT", json: { enabled: on } }); load(); },
      onEdit: (p) => profileSheet(p), onAdd: () => profileSheet(null),
      onDelete: async (p) => { await api(`/api/jobprefs/profiles/${p.id}`, { method: "DELETE" }); load(); } });
    const templates = el("div", { class: "jp-templates" }, ...data.templates.map((t) => ui.button({ label: `Start from: ${t.name}`, variant: "ghost", size: "sm", onClick: async () => {
      try { await api(`/api/jobprefs/profiles/from-template/${t.key}`, { method: "POST", json: {} }); load(); } catch (e) { fail(e); }
    } })));
    root.replaceChildren(el("h2", { class: "ui-title" }, "Job preferences"), ownerCard(), el("section", {}, ui.eyebrow("02", "Profiles"), ed.el, templates), checker());
  }

  paint();
  load();
}
