// Shared job: a job the owner shared from another app (Share > REDLINE on the phone) or pasted here becomes an opportunity.
// Shows the go / no-go label (hub/triage.py) and the ATS match (hub/ats.py) and offers two taps: "Tailor + packet" and
// "Add to apply queue". Route: docs/api/sharein.md. Nothing is sent, applied or queued until a tap here.
// Nothing is fetched from LinkedIn, Naukri or any site that forbids bots: only the shared text is used (the hub says so).
// The Android shell hands the shared text over as window.__hubShare = {text, url, subject} and fires "hub-share" (docs/guide/51).
// One data call when a share arrives or Check is pressed; none when the screen just opens. Every string is set with textContent.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { loadCss as loadTriageCss, verdictLine, flagBadges } from "./triagebadges.js";

const CSS = "/css/share.css";
const TONE = { strong: "good", good: "good", fair: "warn", low: "bad" };
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);

async function copy(text) {
  try { await navigator.clipboard.writeText(text); ui.toast("Copied.", { kind: "good" }); } catch { ui.toast("Could not copy. Select the text and copy it by hand.", { kind: "warn" }); }
}
const field = (label, c, hint) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), c, hint ? el("span", { class: "ui-hint" }, hint) : null);

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  loadTriageCss();
  const root = el("div", { class: "share ui-stack ui-stack--lg" });
  container.append(root);

  const text = el("textarea", { class: "ui-input", rows: "7", maxlength: "20000", placeholder: "Share a job from LinkedIn, Naukri or any app, or paste its text here", "aria-label": "Job text" });
  const url = el("input", { class: "ui-input", type: "url", maxlength: "500", placeholder: "Link (optional)", "aria-label": "Job link", inputmode: "url" });
  const out = el("div", { class: "ui-stack ui-stack--lg" });
  let subject = "";

  const check = ui.button({ label: "Check this job", icon: "target", variant: "primary", onClick: () => ui.busy(check, submit()) });
  const clear = ui.button({ label: "Clear", variant: "ghost", onClick: () => { text.value = ""; url.value = ""; subject = ""; out.replaceChildren(); } });

  async function submit(extra = {}) {
    const body = { text: text.value, url: url.value, subject, ...extra };
    if (!body.text.trim() && !body.url.trim()) { ui.toast("Paste the job text or share a job first.", { kind: "warn" }); return; }
    try { draw(await api("/api/sharein", { method: "POST", json: body })); } catch (e) { fail(e); }
  }

  // ---- the answer ----
  function atsCard(a) {
    if (!a) return null;
    if (a.score == null) return el("div", { class: "ui-card ui-stack share-card" }, ui.eyebrow("", "ATS match"), el("p", { class: "share-sub" }, a.note || "No score yet."));
    const chip = (t, tone) => el("li", { class: "share-chip" + (tone ? " " + tone : "") }, t);
    return el("div", { class: "ui-card ui-stack share-card" },
      el("div", { class: "share-score" }, ui.ringProgress({ value: a.score / 100, size: 64, stroke: 8, big: String(a.score), tone: TONE[a.label] || "accent", label: "ATS match" }),
        el("div", { class: "ui-stack" }, el("h3", { class: "share-h" }, `ATS match: ${a.label}`), el("span", { class: "share-sub" }, a.resume || ""))),
      a.matched.length ? el("div", {}, ui.eyebrow("", `Matched (${a.matched_total})`), el("ul", { class: "share-chips" }, ...a.matched.map((t) => chip(t, "good")))) : null,
      a.listed_in_facts.length ? el("div", {}, ui.eyebrow("", "In your facts, not on the resume"), el("ul", { class: "share-chips" }, ...a.listed_in_facts.map((t) => chip(t, "warn")))) : null,
      a.gap.length ? el("div", {}, ui.eyebrow("", "Gaps (never added for you)"), el("ul", { class: "share-chips" }, ...a.gap.map((t) => chip(t, "bad")))) : null,
      a.tips?.length ? el("ul", { class: "share-tips" }, ...a.tips.map((t) => el("li", {}, t))) : null);
  }

  function packetCard(p) {
    const q = ui.button({ label: p.can_queue ? "Put in the application queue" : "In the queue", icon: "send", variant: "primary", disabled: !p.can_queue, onClick: () => ui.busy(q, (async () => {
      try { const r = await api(`/api/packet/${p.id}/queue`, { method: "POST", json: {} }); ui.toast(r.message, { kind: r.blocked ? "warn" : "good" }); q.disabled = true; } catch (e) { fail(e); }
    })()) });
    return el("div", { class: "ui-card ui-stack share-card" }, ui.eyebrow("", "Packet (a draft)"),
      el("div", { class: "share-actions" }, ui.badge(`ATS ${p.ats.score}`, TONE[p.ats.label]), ui.badge(`fit ${p.fit.label}`), p.blocked ? ui.badge("needs a fix", "warn") : null),
      el("p", { class: "share-sub" }, p.fit.summary), p.blocked ? el("p", { class: "share-warn" }, p.blockers.join(" ")) : null,
      ui.eyebrow("", "Cover note"), el("pre", { class: "share-pre" }, p.cover.text),
      el("div", { class: "share-actions" }, q, ui.button({ label: "Copy everything", icon: "copy", variant: "tonal", onClick: async () => { try { copy(await (await fetch(`/api/packet/${p.id}/text`, { credentials: "same-origin", headers: { "X-Hub": "1" } })).text()); } catch (e) { fail(e); } } })),
      el("p", { class: "ui-hint" }, p.note));
  }

  function draw(d) {
    const f = d.find;
    const packetHost = el("div", { class: "ui-stack" });
    const kids = [];
    if (d.message) kids.push(el("p", { class: "share-msg" + (d.needs_text ? " warn" : "") }, d.message));
    if (f) {
      const title = el("input", { class: "ui-input", type: "text", maxlength: "200", "aria-label": "Job title" });
      const company = el("input", { class: "ui-input", type: "text", maxlength: "100", "aria-label": "Company" });
      title.value = f.note ? "" : f.title;
      company.value = f.company || "";
      const fix = ui.button({ label: "Save title and company", variant: "ghost", size: "sm", onClick: () => ui.busy(fix, submit({ title: title.value, company: company.value, replace_id: f.id })) });
      const link = safeHref(f.url);
      kids.push(el("div", { class: "ui-card ui-stack share-card" },
        el("h3", { class: "share-h" }, f.note ? "Saved as a note" : f.title || "Shared job"),
        el("span", { class: "share-sub" }, [f.company, d.site ? `from ${d.site}` : d.host, d.existing ? "already saved" : ""].filter(Boolean).join(" · ")),
        d.triage ? verdictLine(d.triage) : null,
        d.triage ? el("div", { class: "tg-badges" }, ...flagBadges(d.triage)) : null,
        ...(f.flags || []).filter((x) => x.severity !== "low").map((x) => el("p", { class: "share-warn" }, x.label)),
        f.blocked_reason ? el("p", { class: "share-warn" }, f.blocked_reason) : null,
        link ? el("a", { class: "share-link", href: link, target: "_blank", rel: "noopener noreferrer" }, d.never_automate ? `Open on ${d.site || d.host} yourself` : "Open the posting") : null,
        d.never_automate ? el("p", { class: "ui-hint" }, "REDLINE does not open or read this site. Only the text you shared was used.") : null,
        el("details", { class: "share-fix" }, el("summary", {}, "Fix title or company"), field("Job title", title), field("Company", company), fix)));
    }
    if (d.ats) kids.push(atsCard(d.ats));
    if (d.actions?.packet || d.actions?.queue || d.actions?.queued) {
      const pk = ui.button({ label: "Tailor + packet", icon: "wand", variant: "primary", disabled: !d.actions.packet, onClick: () => ui.busy(pk, (async () => {
        try { const p = await api("/api/packet/prepare", { method: "POST", json: { job: d.job } }); packetHost.replaceChildren(packetCard(p)); packetHost.scrollIntoView?.({ behavior: "smooth", block: "nearest" }); } catch (e) { fail(e); }
      })()) });
      const aq = ui.button({ label: d.actions.queued ? "In the apply queue" : "Add to apply queue", icon: "send", variant: "secondary", disabled: !d.actions.queue, onClick: () => ui.busy(aq, (async () => {
        try { await api(`/api/opps/finds/${f.id}/queue`, { method: "POST", json: {} }); ui.toast("Added to your apply queue as a draft. Nothing was sent.", { kind: "good" }); aq.disabled = true; aq.textContent = "In the apply queue"; } catch (e) { fail(e); }
      })()) });
      kids.push(el("div", { class: "share-actions" }, pk, aq), packetHost);
      kids.push(el("p", { class: "ui-hint" }, "Nothing is sent, applied or queued until you tap. A skill the job asks for is only a suggestion; it is never added to your resume."));
    }
    out.replaceChildren(...kids);
    const view = out.closest(".view");   // the answer is under the paste box: bring it up
    if (view) view.scrollTo({ top: Math.max(0, out.getBoundingClientRect().top - view.getBoundingClientRect().top + view.scrollTop - 60), behavior: "smooth" });
  }

  // ---- a share handed over by the Android shell, or by a paste ----
  function pickup() {
    const s = window.__hubShare;
    if (!s || typeof s !== "object") return;
    delete window.__hubShare;
    text.value = String(s.text || "").slice(0, 20000);
    url.value = String(s.url || "").slice(0, 500);
    subject = String(s.subject || "").slice(0, 200);
    ui.busy(check, submit());
  }
  window.addEventListener("hub-share", () => { const v = document.getElementById("view-share"); if (v && !v.hidden) pickup(); });

  root.append(el("h2", { class: "ui-title" }, "Shared job"),
    el("p", { class: "ui-hint" }, "On the phone, open a job in any app, tap Share, then REDLINE. Or paste the job text here."),
    el("div", { class: "ui-card ui-stack share-card" }, field("Job text", text), field("Link", url), el("div", { class: "share-actions" }, check, clear)),
    out);

  return { show: pickup, refresh: pickup };
}
