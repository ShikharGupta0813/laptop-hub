// Trust ladder UI: suggestion cards ("Do this automatically from now on") and the Trust rules list (view, revoke, extend, use counts).
// Routes: docs/api/trust.md. All text through el() (textContent).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/panic.css";
const fail = (e) => ui.toast(e?.message || "Something went wrong.", { kind: "bad" });
function ensureCss() { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); }

export function suggestionCard(s, reload) {
  return el("div", { class: "ui-card tr-card" },
    el("p", { class: "ui-card__title" }, s.label),
    el("p", { class: "ui-card__sub" }, `You approved this ${s.approvals} times in a row. It would apply to ${s.scope}, for 30 days, at most 20 a day. It never applies to critical actions, sends, MCP tools or after reading web or mail content.`),
    el("div", { class: "tr-actions" },
      ui.button({ label: "Do this automatically from now on", variant: "primary", size: "sm", onClick: async () => { try { await api(`/api/trust/suggestions/${s.id}/accept`, { method: "POST", json: {} }); ui.toast("Trust rule created.", { kind: "good" }); reload(); } catch (e) { fail(e); } } }),
      ui.button({ label: "Not now", variant: "ghost", size: "sm", onClick: async () => { try { await api(`/api/trust/suggestions/${s.id}/dismiss`, { method: "POST" }); reload(); } catch (e) { fail(e); } } })));
}

export function ruleRow(r, reload) {
  const left = r.state === "active" ? `${r.expires_in_days} days left` : r.state;
  return ui.listRow({ icon: "key", tone: r.state === "active" ? "warn" : "gray", title: r.label,
    sub: `${r.uses} runs (${r.uses_today}/${r.daily_cap} today) · ${left}${r.revoked ? ` (${r.revoked})` : ""}`,
    meta: r.state === "active" ? el("span", { class: "tr-actions" },
      ui.button({ label: "Extend", size: "sm", variant: "secondary", onClick: async (e) => { e.stopPropagation(); try { await api(`/api/trust/rules/${r.id}/extend`, { method: "POST", json: { days: 30 } }); reload(); } catch (x) { fail(x); } } }),
      ui.button({ label: "Revoke", size: "sm", variant: "secondary", onClick: async (e) => { e.stopPropagation(); try { await api(`/api/trust/rules/${r.id}/revoke`, { method: "POST" }); reload(); } catch (x) { fail(x); } } })) : null });
}

export function openTrust() {
  ensureCss();
  const body = el("div", { class: "ui-stack ui-stack--lg" }, ui.skeleton("list", 3));
  const sheet = ui.sheet({ title: "Trust rules", content: body, class: "au-sheet" });
  async function reload() {
    try {
      const d = await api("/api/trust");
      body.replaceChildren(
        ui.notice(d.enabled ? "After you approve the same kind of change many times with no denial, REDLINE offers to do it for you. A denial, a Panic or any Full Control change revokes a rule at once." : "The trust ladder is switched off (trust_enabled).", { icon: "shield" }),
        ...(d.suggestions || []).map((s) => suggestionCard(s, reload)),
        (d.rules || []).length ? el("div", { class: "ui-list ui-list--inset" }, ...d.rules.map((r) => ruleRow(r, reload))) : ui.emptyState({ icon: "key", title: "No trust rules", compact: true }),
        (d.rules || []).some((r) => r.state === "active") ? ui.button({ label: "Revoke all", icon: "x", variant: "danger", onClick: async () => { try { await api("/api/trust/revoke-all", { method: "POST" }); reload(); } catch (e) { fail(e); } } }) : null);
    } catch (e) { body.replaceChildren(ui.notice(e.message || "Couldn't load.", { tone: "bad" })); }
  }
  reload();
  return sheet;
}
