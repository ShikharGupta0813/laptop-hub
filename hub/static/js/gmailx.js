// Mail accounts card (Settings): Gmail (Google sign-in or app password) and any IMAP/SMTP server, with free-text tag chips and a
// per-account cloud-AI switch. Routes: docs/api/gmailx.md. All text via textContent (el()).
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
  const card = el("section", { class: "ui-card ui-hud ui-stack gx-card", "aria-label": "Mail accounts" },
    el("h2", { class: "ui-caps" }, "Mail accounts"), body);

  async function refresh() {
    let info;
    try { info = await api("/api/gmail/accounts"); } catch (error) {
      body.replaceChildren(el("p", { class: "ui-hint" }, error?.message || "Mail accounts are not available."));
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
      actions.push(ui.button({ label: "Tags", icon: "edit", variant: "ghost", onClick: () => tagsSheet(a) }));
      if (a.kind === "imap") actions.push(ui.button({ label: "Server", icon: "edit", variant: "ghost", onClick: () => imapSheet(a) }));
      if (a.kind !== "legacy") {
        actions.push(ui.button({ label: a.kind === "imap" ? "Remove" : "Disconnect", icon: "x", variant: "ghost", onClick: async () => {
          if (!await ui.confirm({ title: `Remove ${a.label}?`, message: "REDLINE forgets this account's sign-in or password (and asks Google to revoke a Google sign-in).", confirmLabel: "Remove", danger: true })) return;
          try { await api(`/api/gmail/accounts/${a.id}`, { method: "DELETE" }); ui.toast("Removed.", { kind: "good" }); refresh(); onChanged?.(); } catch (error) { fail(error); }
        } }));
      }
      const cloudSw = ui.switchControl({ checked: !!a.cloud_ai_allowed, label: `Allow ${a.label} mail text to reach cloud AI`, onChange: async (on) => {
        try { await api(`/api/gmail/accounts/${a.id}`, { method: "PUT", json: { cloud_ai_allowed: on } }); ui.toast(on ? "This account's mail text may go to cloud AI." : "This account's mail text stays on this laptop.", { kind: on ? "warn" : "good" }); refresh(); }
        catch (error) { fail(error); cloudSw.input.checked = !on; }
      } });
      return el("div", { class: "ui-stack" },
        el("div", { class: "ap-head" }, ...(a.tags || [a.label]).map((t) => ui.badge(t, a.protected ? "warn" : "accent")), ui.badge(a.connected ? "connected" : "not connected", a.connected ? "good" : "warn"),
          el("span", {}, a.address || (a.kind === "legacy" ? "App password (Jarvis setup)" : ""))),
        el("p", { class: "ui-hint" }, `Allowed to: ${scopeText(a)}.`),
        a.connected ? switchRow("Allow sending from this account", a.kind === "legacy" ? "Used by the assistant's send email tool (you still approve each mail)." : "Off means read-only. Sending only ever happens when you ask for it.", send) : null,
        switchRow("Allow this account's mail text to reach cloud AI", a.cloud_ai_explicit ? "Set by you." : (a.cloud_ai_default ? "Default: on. Mail tagged company or work defaults to off." : "Default: off, because it is tagged company or work."), cloudSw),
        actions.length ? el("div", { class: "ap-actions" }, ...actions) : null);
    });
    body.replaceChildren(...rows,
      el("p", { class: "ui-hint" }, "When an account's cloud-AI switch is off, the text of its mail is never sent to Gemini or Claude: only the sender, subject and short snippet are used, and only the local model reads the body. Check your company's policy before switching it on."),
      el("div", { class: "ap-actions" },
        ui.button({ label: "Connect a Gmail account", icon: "plus", variant: "secondary", onClick: () => connectSheet(null, info) }),
        ui.button({ label: "Add a mail server (Outlook, Yahoo, Zoho, company)", icon: "plus", variant: "secondary", onClick: () => imapSheet(null, info) })));
  }

  function tagsSheet(a) {
    const tags = el("input", { class: "ui-input", value: (a.tags || []).join(", "), placeholder: "personal, company, client-x", "aria-label": "Tags" });
    ui.sheet({ title: `Tags for ${a.label}`, class: "st-sheet", content: el("div", { class: "ui-stack" },
      ui.notice("Tags are your own words, separated by commas. A tag of company or work turns the cloud-AI switch off by default.", { tone: "info" }), tags),
    actions: [{ label: "Save", variant: "primary", onClick: async () => {
      try { await api(`/api/gmail/accounts/${a.id}`, { method: "PUT", json: { tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) } }); refresh(); onChanged?.(); return true; }
      catch (error) { fail(error); return false; } } }] });
  }

  function imapSheet(existing, info) {
    const presets = info?.presets || existing?.presets || { custom: { label: "Another mail server" } };
    const preset = el("select", { class: "ui-input", "aria-label": "Provider" }, ...Object.entries(presets).map(([k, v]) => el("option", { value: k }, v.label)));
    const text = (v, ph, attrs = {}) => el("input", { class: "ui-input", value: v ?? "", placeholder: ph, autocomplete: "off", ...attrs });
    const label = text(existing?.label, "Label, e.g. work", { maxlength: "30" });
    const address = text(existing?.address, "Email address");
    const user = text(existing?.username, "User name (empty = the address)");
    const pass = text("", existing ? "New password (leave empty to keep)" : "Password or app password", { type: "password" });
    const imapHost = text(existing?.imap_host, "IMAP server"), imapPort = text(existing?.imap_port, "993", { inputmode: "numeric" });
    const smtpHost = text(existing?.smtp_host, "SMTP server"), smtpPort = text(existing?.smtp_port, "587", { inputmode: "numeric" });
    const sec = (v) => el("select", { class: "ui-input" }, ...["ssl", "starttls", "none"].map((o) => el("option", { value: o, selected: o === v }, o)));
    const imapSec = sec(existing?.imap_security || "ssl"), smtpSec = sec(existing?.smtp_security || "starttls");
    const tags = text((existing?.tags || []).join(", "), "Tags: personal, company, anything");
    const send = el("input", { type: "checkbox" });
    preset.addEventListener("change", () => { const p = presets[preset.value]; if (!p) return; imapHost.value = p.imap_host || ""; imapPort.value = p.imap_port || ""; imapSec.value = p.imap_security || "ssl";
      smtpHost.value = p.smtp_host || ""; smtpPort.value = p.smtp_port || ""; smtpSec.value = p.smtp_security || "starttls"; });
    const f = (name, node) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, name), node);
    ui.sheet({ title: existing ? `Server settings for ${existing.label}` : "Add a mail server account", class: "st-sheet", content: el("div", { class: "ui-stack" },
      ui.notice("Works with Outlook / Office 365, Yahoo, Zoho, iCloud or a company server. Use an app password where the provider asks for one. The password is kept in the OS keyring only. Some Office 365 companies switch off this kind of sign-in; then ask your IT team.", { tone: "info" }),
      existing ? null : f("Provider", preset), f("Label", label), f("Email address", address), f("User name", user), f("Password", pass),
      f("IMAP server", imapHost), f("IMAP port", imapPort), f("IMAP security", imapSec), f("SMTP server (for sending, optional)", smtpHost), f("SMTP port", smtpPort), f("SMTP security", smtpSec),
      f("Tags", tags), existing ? null : el("label", { class: "pv-kind" }, send, el("span", {}, "Also allow sending from this account (only when you ask)"))),
    actions: [{ label: existing ? "Save" : "Add", variant: "primary", onClick: async () => {
      const num = (v) => (v.value.trim() ? Number(v.value) : null);
      const server = { imap_host: imapHost.value.trim(), imap_port: num(imapPort), imap_security: imapSec.value, smtp_host: smtpHost.value.trim() || null,
        smtp_port: num(smtpPort), smtp_security: smtpSec.value, username: user.value.trim() || null };
      try {
        if (existing) await api(`/api/gmail/accounts/${existing.id}`, { method: "PUT", json: { label: label.value.trim(), ...server, password: pass.value || null, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) } });
        else await api("/api/gmail/accounts/imap", { method: "POST", json: { label: label.value.trim(), address: address.value.trim(), password: pass.value, send_enabled: send.checked,
          tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean), ...server } });
        pass.value = ""; ui.toast("Saved.", { kind: "good" }); refresh(); onChanged?.(); return true;
      } catch (error) { pass.value = ""; fail(error); return false; } } }] });
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
