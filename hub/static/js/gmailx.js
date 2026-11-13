// Gmail accounts card (Settings): personal + company mail. Routes: docs/api/gmailx.md. All text via textContent (el()).
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const SCOPE_WORDS = { "https://www.googleapis.com/auth/gmail.readonly": "read mail", "https://www.googleapis.com/auth/gmail.send": "send mail" };
const scopeText = (a) => (a.scopes?.length ? a.scopes.map((s) => SCOPE_WORDS[s] || s).join(", ") : "none");

function switchRow(title, sub, control) {
  return el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, title),
    el("span", { class: "ui-row__sub" }, sub)), control);
}

export function gmailCard({ S, onChanged } = {}) {
  const body = el("div", { class: "ui-stack" }, ui.skeleton("list", 2));
  const card = el("section", { class: "ui-card ui-hud ui-stack", "aria-label": "Gmail accounts" },
    el("h2", { class: "ui-caps" }, "Gmail accounts"), body);

  async function refresh() {
    let info;
    try { info = await api("/api/gmail/accounts"); } catch (error) {
      body.replaceChildren(el("p", { class: "ui-hint" }, error?.message || "Gmail accounts are not available."));
      return;
    }
    const rows = info.accounts.map((a) => {
      const send = ui.switchControl({ checked: !!a.send_enabled, label: `Allow sending from ${a.label}`, onChange: async (on) => {
        try {
          const r = await api(`/api/gmail/accounts/${a.id}`, { method: "PUT", json: { send_enabled: on } });
          if (on && r.reconsent_required) ui.toast("Saved. Press Re-consent so Google can grant the send permission.", { kind: "warn" });
          refresh();
        } catch (error) { fail(error); send.input.checked = !on; }
      } });
      const actions = [];
      if (a.reconsent_required) actions.push(ui.button({ label: "Re-consent", icon: "refresh", variant: "tonal", onClick: () => connectSheet(a) }));
      if (a.kind !== "legacy") {
        actions.push(ui.button({ label: "Disconnect", icon: "x", variant: "ghost", onClick: async () => {
          if (!await ui.confirm({ title: `Disconnect ${a.label}?`, message: "REDLINE forgets this account's sign-in and asks Google to revoke it.", confirmLabel: "Disconnect", danger: true })) return;
          try { await api(`/api/gmail/accounts/${a.id}`, { method: "DELETE" }); ui.toast("Disconnected.", { kind: "good" }); refresh(); onChanged?.(); } catch (error) { fail(error); }
        } }));
      }
      return el("div", { class: "ui-stack" },
        el("div", { class: "ap-head" }, ui.badge(a.label, a.protected ? "warn" : "accent"), ui.badge(a.connected ? "connected" : "not connected", a.connected ? "good" : "warn"),
          el("span", {}, a.address || (a.kind === "legacy" ? "App password (Jarvis setup)" : ""))),
        el("p", { class: "ui-hint" }, `Allowed to: ${scopeText(a)}.`),
        a.connected ? switchRow("Allow sending from this account", a.kind === "legacy" ? "Used by the assistant's send email tool (you still approve each mail)." : "Asks Google for the extra send permission. Off means read-only.", send) : null,
        actions.length ? el("div", { class: "ap-actions" }, ...actions) : null);
    });
    const cloud = ui.switchControl({ checked: !!info.company_ai_allowed, label: "Allow company mail to reach cloud AI", onChange: async (on) => {
      try { await api("/api/settings", { method: "PATCH", json: { gmail_company_ai_allowed: on } }); ui.toast(on ? "Company mail text may now go to cloud AI." : "Company mail text stays on this laptop.", { kind: on ? "warn" : "good" }); refresh(); }
      catch (error) { fail(error); cloud.input.checked = !on; }
    } });
    body.replaceChildren(...rows,
      switchRow("Allow company mail to reach cloud AI", "Off (recommended): the text of company mail is never sent to Gemini or Claude. Only the sender, subject and Gmail's short snippet are used, and only the local model reads the body. Check your company's policy (for example OneStack's) before switching this on.", cloud),
      ui.button({ label: "Connect another account", icon: "plus", variant: "secondary", onClick: () => connectSheet(null, info) }));
  }

  function connectSheet(existing, info) {
    const label = el("input", { class: "ui-input", value: existing?.label || "company", maxlength: "30", "aria-label": "Account label" });
    if (existing) label.disabled = true;
    const send = el("input", { type: "checkbox" });
    const cid = el("input", { class: "ui-input", placeholder: "OAuth client id", autocomplete: "off" });
    const sec = el("input", { class: "ui-input", type: "password", placeholder: "OAuth client secret", autocomplete: "off" });
    const status = el("p", { class: "ui-hint", "aria-live": "polite" }, "");
    const paste = el("input", { class: "ui-input", placeholder: "If the page cannot be reached, paste its address here" });
    let state = null, timer = null;
    const stop = () => { if (timer) clearInterval(timer); timer = null; };
    const content = el("div", { class: "ui-stack ui-stack--lg" },
      ui.notice("Sign in with Google on the laptop's browser. Company accounts are read-only unless you tick the box. REDLINE never asks for full mailbox access.", { tone: "info" }),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Label"), label),
      existing ? null : el("label", { class: "pv-kind" }, send, el("span", {}, "Also allow sending from this account (asks Google for send permission)")),
      (info && info.client_configured && !info.reset) ? null : el("div", { class: "ui-stack" }, el("span", { class: "ui-label" }, "Google OAuth client (Desktop app)"), cid, sec,
        el("span", { class: "ui-hint" }, "Saved in the Windows keyring. Leave empty if you saved it before.")),
      status, paste);
    ui.sheet({ title: existing ? `Re-consent ${existing.label}` : "Connect a Gmail account", class: "st-sheet", content, onClose: stop, actions: [
      { label: "Open Google sign-in", variant: "primary", icon: "external", onClick: async () => {
        try {
          const r = await api("/api/gmail/accounts/connect", { method: "POST", json: {
            label: label.value.trim(), send_enabled: send.checked, account_id: existing?.id || null,
            client_id: cid.value.trim() || null, client_secret: sec.value.trim() || null } });
          state = r.state;
          window.open(r.auth_url, "_blank", "noopener");
          status.textContent = "Waiting for you to finish in the other tab...";
          stop();
          timer = setInterval(async () => {
            try {
              const f = await api(`/api/gmail/accounts/connect/${state}`);
              if (f.status === "done") { stop(); ui.toast("Account connected.", { kind: "good" }); refresh(); onChanged?.(); status.textContent = "Connected. You can close this."; }
              else if (f.status === "error") { stop(); status.textContent = f.error || "Sign-in failed."; }
            } catch (_) { /* keep waiting */ }
          }, 2000);
        } catch (error) { fail(error); }
        return false;
      } },
      { label: "Finish with pasted address", variant: "secondary", onClick: async () => {
        if (!state || !paste.value.trim()) { status.textContent = "Press Open Google sign-in first, then paste the address you land on."; return false; }
        try { await api("/api/gmail/accounts/connect/complete", { method: "POST", json: { state, redirect_url: paste.value.trim() } }); stop(); ui.toast("Account connected.", { kind: "good" }); refresh(); onChanged?.(); return true; }
        catch (error) { fail(error); return false; }
      } }] });
  }

  refresh();
  card.refresh = refresh;
  return card;
}
