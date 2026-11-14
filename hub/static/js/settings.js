// Settings: grouped inset lists that drill into sheets.
// Routes: docs/api/settings.md, docs/api/notify.md (push), docs/api/capture-android.md (HubCapture plugin).
// Sections (openSection): appearance, models, folders, push (alias notifications), capture, diagnostics, audit, backup, about,
// and the owner's lists (js/ownerlists.js): assistants, notifyprefs, safetyrules, redactrules, memorykinds.
import { wakeEnabled, setWakeEnabled, wakeSupported, keepAwakeEnabled, setKeepAwake, wakeLockSupported } from "./wake.js";
import { api, ApiError, sse, el } from "./api.js";
import * as ui from "./ui.js";
import { usageCard, capsSheet, redactionSheet } from "./privacy.js";
import { gmailCard } from "./gmailx.js";
import { OWNER_SECTIONS } from "./ownerlists.js";
import { providersCard } from "./provlists.js";
import { mcpCard } from "./mcp.js";
import { modelsPanel } from "./models.js";
import { dictationRow } from "./dictation.js";

// A sheet button that is still working ignores further taps (a double-tap must not send two requests).
const guard = (actions) => (actions || []).map((a) => {
  if (!a || !a.onClick) return a;
  let working = false;
  return { ...a, onClick: async (x) => { if (working) return false; working = true; try { return await a.onClick(x); } finally { working = false; } } };
});
const gsheet = (opts) => ui.sheet(opts.actions ? { ...opts, actions: guard(opts.actions) } : opts);


const CSS = "/css/settings.css";
const SECTION_ALIAS = { notifications: "push", ntfy: "push", backups: "backup", log: "audit", activity: "audit", phone: "capture" };
const MODEL_SLOTS = [
  { key: "chat_model", label: "Chat", sub: "Everyday answers, Learn and Notes", icon: "talk", tone: "accent", source: "ollama" },
  { key: "assistant_model", label: "Assistant", sub: "Talk, tools and actions", icon: "prompt", tone: "info", source: "ollama" },
  { key: "claude_model", label: "Claude", sub: "Used only when you pick Claude", icon: "bot", tone: "orange", source: "text" },
  { key: "embed_model", label: "Search and memory", sub: "Lets the hub find things by meaning", icon: "layers", tone: "teal", source: "ollama" },
  { key: "whisper_model", label: "Speech to text", sub: "Voice notes and dictation", icon: "mic", tone: "pink", source: "whisper" },
];
const WHISPER_CHOICES = ["tiny", "base", "small", "medium", "large-v3"];

const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const good = (text) => ui.toast(text, { kind: "good" });
const sameModel = (a, b) => a === b || a === b + ":latest" || a + ":latest" === b;
const fmtMb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`);
const fmtBytes = (n) => (n >= 1073741824 ? `${(n / 1073741824).toFixed(1)} GB` : n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const when = (t) => { const d = new Date(t); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }); };
const group = (title, ...rows) => el("section", { class: "st-group" }, title ? el("h2", { class: "ui-caps st-caps" }, title) : null,
  el("div", { class: "ui-list ui-list--inset ui-list--icons" }, rows.flat().filter(Boolean)));

function ensureCss() {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
}

// A settings row with a switch. Clicking anywhere on the row toggles it.
function switchRow({ icon, tone, title, sub, checked, onChange, disabled }) {
  const sw = ui.switchControl({ checked, onChange, label: title });
  if (disabled) sw.input.disabled = true;
  const row = el("div", { class: "ui-row ui-row--interactive st-switchrow" }, ui.tile(icon, tone),
    el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, title), sub ? el("span", { class: "ui-row__sub" }, sub) : null), sw);
  row.addEventListener("click", (e) => { if (!sw.contains(e.target) && !sw.input.disabled) sw.input.click(); });
  row.switch = sw;
  return row;
}

function capture() {
  try { return window.Capacitor?.Plugins?.HubCapture || window.HubCapture || null; } catch (_) { return null; }
}

export function init(container, ctx) {
  ensureCss();
  const root = el("div", { class: "st ui-stack ui-stack--lg" });
  container.append(root);
  let S = null;            // GET /api/settings
  let notify = null;       // GET /api/notifications/settings (lazy)
  let diag = null;         // diagnostics summary (lazy)
  let appearanceCard = null;
  let usageEl = null;      // the "Usage today" card (js/privacy.js), kept across repaints
  let gmailEl = null;      // the "Gmail accounts" card (js/gmailx.js)
  let mcpEl = null;        // the "MCP servers" card (js/mcp.js)
  let providersEl = null;  // the "Providers" card: search, vision, AI endpoints (js/provlists.js)

  // ---- main page ---------------------------------------------------------------------------------
  function appearance() {
    const haptics = switchRow({ icon: "zap", tone: "orange", title: "Haptics", sub: "A tiny buzz on taps", checked: localStorage.getItem("hub.haptics") !== "off",
      onChange: (on) => { try { localStorage.setItem("hub.haptics", on ? "on" : "off"); } catch (_) { /* private */ } if (on) ui.haptic("success"); } });
    const motion = switchRow({ icon: "wand", tone: "teal", title: "Reduce motion", sub: "Fewer animations",
      checked: document.documentElement.dataset.motion === "reduce",
      onChange: (on) => {
        try { if (on) localStorage.setItem("hub.motion", "reduce"); else localStorage.removeItem("hub.motion"); } catch (_) { /* private */ }
        if (on) document.documentElement.dataset.motion = "reduce"; else delete document.documentElement.dataset.motion;
      } });
    const wakeOk = wakeSupported() && window.isSecureContext;
    const wake = switchRow({ icon: "mic", tone: "teal", title: "Hey Jarvis / Hey Friday",
      sub: wakeOk ? "Say it hands-free to start voice mode. Works only while the app is open and the screen is on: Android stops the microphone in the background. Keeps the microphone open and keeps the screen on while listening, so it uses battery."
        : "Needs Chrome on the secure (https) address. This browser cannot listen for a wake word.",
      checked: wakeEnabled() && wakeOk, disabled: !wakeOk, onChange: (on) => setWakeEnabled(on) });
    const keep = switchRow({ icon: "phone", tone: "teal", title: "Keep screen on while listening",
      sub: wakeLockSupported() ? "Stops the screen from turning off while Hey Jarvis is listening, so the microphone keeps working. Uses battery."
        : "This browser cannot keep the screen on.",
      checked: keepAwakeEnabled() && wakeLockSupported(), disabled: !wakeLockSupported(), onChange: (on) => setKeepAwake(on) });
    const lap = switchRow({ icon: "laptop", tone: "violet", title: "Listen on the laptop microphone",
      sub: "Say Hey Jarvis at the desk and the laptop answers out loud, phone untouched. Privacy: audio never leaves the laptop, only the text of what you say after the wake phrase goes to the AI you chose. Anything else it hears is thrown away. Needs the optional sounddevice package.",
      checked: false, onChange: async (on) => {
        try {
          const st = await api("/api/wakeword/toggle", { method: "POST", json: { enabled: on } });
          if (on && st.message) ui.toast(st.message, { kind: "warn" });
        } catch (e) { fail(e); lap.switch.input.checked = !on; }
      } });
    api("/api/wakeword/status").then((st) => {
      lap.switch.input.checked = !!st.enabled;
      if (st.enabled && st.message) lap.querySelector(".ui-row__sub").textContent += " Status: " + st.message;
    }).catch(() => {});
    return el("section", { class: "st-group", id: "st-appearance" },
      el("h2", { class: "ui-caps st-caps" }, "Appearance"),
      el("div", { class: "ui-card ui-card--pad-lg" }, ui.appearancePanel()),
      el("div", { class: "ui-list ui-list--inset ui-list--icons st-after" }, haptics, motion, wake, keep, lap));
  }

  function glance() {
    if (!S) return null;
    const chips = [];
    const chip = (icon, text, tone) => el("span", { class: "ui-chip ui-chip--static st-chip" + (tone ? ` st-chip--${tone}` : "") }, ui.icon(icon), text);
    chips.push(S.ollama?.reachable ? chip("check-circle", "Local models running", "good") : chip("alert", "Local models offline", "warn"));
    const cloud = Object.values(S.providers || {}).filter((p) => p.ready && p.label !== "Local model");
    chips.push(cloud.length || S.has_claude_key ? chip("key", cloud.map((p) => p.label).join(", ") || "Claude connected", "good") : chip("key", "No cloud AI"));
    if (notify) chips.push(notify.ntfy_enabled ? chip("bell", "Phone push on", "good") : chip("bell", "Phone push off"));
    return el("div", { class: "ui-card st-hero" },
      el("div", { class: "st-hero__top" }, ui.tile("laptop", "accent", "lg"),
        el("div", { class: "grow" }, el("h2", { class: "ui-card__title" }, "REDLINE"),
          el("p", { class: "ui-card__sub" }, `Version ${S.version || "?"}${S.platform ? " on " + S.platform : ""}`))),
      el("div", { class: "st-chips" }, chips));
  }

  // Update REDLINE: always present. POST /api/update starts the update (the task if installed, else a detached process).
  let updateInfo = null, updating = false;
  async function loadUpdate() {
    try { updateInfo = await api("/api/update-log"); } catch { updateInfo = null; }
    paint();
  }
  function updateCard() {
    const btn = ui.button({ label: updating ? "Updating..." : "Update REDLINE", icon: "download", variant: "primary", block: true, disabled: updating });
    btn.addEventListener("click", async () => {
      if (updating) return;
      if (!await ui.confirm({ title: "Update REDLINE?", message: "The newest code is pulled from GitHub and the hub restarts. The app will reconnect in about 20 seconds.", confirmLabel: "Update now" })) return;
      updating = true; paint();
      try {
        const r = await api("/api/update", { method: "POST", json: { confirm: true } });
        good(r.message || "Updating now. The app will reconnect in about 20 seconds.");
        setTimeout(() => { updating = false; loadUpdate(); }, 20000);
      } catch (error) { updating = false; fail(error); paint(); }
    });
    const lines = (updateInfo?.lines || []).slice(-6);
    return el("div", { class: "ui-card ui-stack" },
      el("h3", { class: "ui-card__title" }, "Update REDLINE"),
      el("p", { class: "ui-card__sub" }, `Current version ${updateInfo?.version || S?.version || "?"}. The app will reconnect in ~20 s.`),
      btn,
      lines.length ? el("pre", { class: "ui-hint st-note", style: "white-space:pre-wrap;overflow-wrap:anywhere;margin:0" }, lines.join("\n")) : null);
  }

  function main() {
    const rows = [];
    if (!S) return rows;
    rows.push(updateCard());
    const chatName = S.chat_model || "Not set";
    rows.push(group("Intelligence",
      ui.listRow({ icon: "cpu", tone: "info", title: "Models", sub: `Chat: ${chatName}`, chevron: true, onClick: () => openSection("models"),
        meta: S.ollama?.reachable ? null : ui.badge("Offline", "warn") }),
      ui.listRow({ icon: "folder", tone: "orange", title: "Folders", sub: `${S.kb_roots?.length || 0} searchable, ${S.writable_roots?.length || 0} editable`, chevron: true, onClick: () => openSection("folders") })));
    rows.push(group("Privacy and usage",
      ui.listRow({ icon: "shield", tone: "good", title: "Privacy redaction", chevron: true, onClick: () => redactionSheet(S, load),
        sub: S.redact_enabled === false ? "Off: cloud AI sees your text as it is" : "Hides emails, numbers, keys and your own words from cloud AI",
        meta: ui.badge(S.redact_enabled === false ? "Off" : "On", S.redact_enabled === false ? "warn" : "good") })));
    rows.push(group("Assistants and rules",
      ui.listRow({ icon: "jarvis", tone: "accent", title: "Assistants", sub: "Jarvis, Friday and any you add: names, wake words, prompts", chevron: true, onClick: () => openSection("assistants") }),
      ui.listRow({ icon: "bell", tone: "orange", title: "Notification preferences", sub: "Now, batch, quiet only or off, for each kind of alert", chevron: true, onClick: () => openSection("notifyprefs") }),
      ui.listRow({ icon: "bell", tone: "info", title: "Delivery rules", sub: "How alerts reach you at the laptop, away, in a meeting or in focus", chevron: true, onClick: () => openSection("deliveryrules") }),
      ui.listRow({ icon: "shield", tone: "bad", title: "My safety rules", sub: "Make anything touching a path or command stricter", chevron: true, onClick: () => openSection("safetyrules") }),
      ui.listRow({ icon: "eye-off", tone: "good", title: "My redaction rules", sub: "Words and patterns cloud AI must never see", chevron: true, onClick: () => openSection("redactrules") }),
      ui.listRow({ icon: "tag", tone: "info", title: "Memory kinds", sub: "What kinds of memory exist and who sees them", chevron: true, onClick: () => openSection("memorykinds") }),
      ui.listRow({ icon: "users", tone: "good", title: "Work lanes", sub: "Where routine work goes first: free Council and local models before Gemini or Claude", chevron: true, onClick: () => openSection("lanes") })));
    rows.push(group("Dictation", dictationRow()));
    rows.push(group("Alerts",
      ui.listRow({ icon: "bell", tone: "bad", title: "Phone push", sub: notify ? (notify.ntfy_enabled ? "On. Alerts reach your phone." : "Off. Alerts stay in the app.") : "Alerts on your phone through ntfy", chevron: true, onClick: () => openSection("push") }),
      ui.listRow({ icon: "eye", tone: "info", title: "Mail and chat watching", sub: "Rules for important mail and chat", chevron: true, onClick: () => ctx.open("watch") }),
      ui.listRow({ icon: "inbox", tone: "accent", title: "Notifications", sub: "Everything the hub told you", chevron: true, onClick: () => ctx.open("notifications") })));
    rows.push(group("This phone",
      ui.listRow({ icon: "phone", tone: "teal", title: "Phone capture", sub: capture() ? "Spending and message capture" : "Needs the installed Android app", chevron: true, onClick: () => openSection("capture") })));
    rows.push(group("Agent controls",
      ui.listRow({ icon: "stop", tone: "bad", title: "Panic", sub: "Stop the agent everywhere until you tap Resume", chevron: true, onClick: () => import("./panic.js").then((m) => m.press()) }),
      ui.listRow({ icon: "history", tone: "info", title: "What did the agent do?", sub: "Timeline of tool calls, approvals, denials and sends", chevron: true, onClick: () => import("./audit.js").then((m) => m.openAudit(ctx)) }),
      ui.listRow({ icon: "key", tone: "warn", title: "Trust rules", sub: "Things you chose to let the agent do on its own", chevron: true, onClick: () => import("./trust.js").then((m) => m.openTrust()) })));
    rows.push(group("Account & security", fullControlRow(), permissionsRow(), logoutAllRow()));
    rows.push(group("System",
      ui.listRow({ icon: "activity", tone: "good", title: "Diagnostics", sub: "Is everything set up?", chevron: true, onClick: () => openSection("diagnostics"),
        meta: diag && diag.bad ? ui.badge(`${diag.bad} to fix`, "bad") : diag && diag.warn ? ui.badge(`${diag.warn} tips`, "warn") : null }),
      ui.listRow({ icon: "history", tone: "gray", title: "Activity log", sub: "Everything the hub did, newest first", chevron: true, onClick: () => openSection("audit") }),
      ui.listRow({ icon: "download", tone: "info", title: "Backups", sub: "Save your data as a zip", chevron: true, onClick: () => openSection("backup") }),
      ui.listRow({ icon: "info", tone: "gray", title: "About", sub: "Version, uptime, locked options", chevron: true, onClick: () => openSection("about") })));
    return rows;
  }

  // Log out of all devices: POST /api/logout-all kills every session cookie (this phone's too), then the
  // login screen comes back through the same event api.js raises on a 401.
  // Full Control: no approval step anywhere. ON needs the typed phrase (sheet in fullcontrol.js); OFF is one tap.
  function fullControlRow() {
    const on = S && S.full_control === true;
    return ui.listRow({ icon: "shield", tone: on ? "bad" : "gray", title: "Full Control",
      sub: on ? "On: commands and file changes run without asking. Tap to turn off." : "Off: the assistant asks before it changes anything.",
      meta: ui.badge(on ? "ON" : "Off", on ? "bad" : "gray"),
      onClick: async () => {
        const fc = await import("./fullcontrol.js");
        if (on) await fc.turnOff(); else await fc.turnOn();
        await load();
      } });
  }

  // Remembered approvals ("Allow this for 1 hour" on an approval card). Memory only; critical actions are never listed.
  let grantCount = null;
  function fmtLeft(g) {
    if (g.scope !== "hour" || g.expires_in == null) return "this chat session";
    const m = Math.max(1, Math.ceil(g.expires_in / 60));
    return `${m} min left`;
  }
  function permissionsRow() {
    return ui.listRow({ icon: "key", tone: "gray", title: "Active permissions", chevron: true,
      sub: "What you chose to allow without asking again. Each can be revoked.",
      meta: grantCount ? ui.badge(String(grantCount), "warn") : null,
      onClick: () => openPermissions() });
  }
  function openPermissions() {
    return drill("Active permissions", async (s) => {
      const data = await api("/api/assistant/permissions");
      const grants = Array.isArray(data.grants) ? data.grants : [];
      grantCount = grants.length;
      const rows = grants.map((g) => ui.listRow({ icon: "clock", tone: "warn", title: g.label || g.tool, sub: fmtLeft(g),
        meta: ui.button({ label: "Revoke", size: "sm", variant: "secondary", class: "st-revoke", onClick: async (e) => {
          e.stopPropagation();
          try { await api("/api/assistant/permissions/revoke", { method: "POST", json: { id: g.id } }); } catch (error) { if (error.status !== 404) return fail(error); }
          s.reload();
        } }) }));
      const revokeAll = grants.length ? ui.button({ label: "Revoke all", icon: "x", variant: "danger", class: "st-revoke-all", onClick: async () => {
        try { await api("/api/assistant/permissions/revoke", { method: "POST", json: { all: true } }); s.reload(); } catch (error) { fail(error); }
      } }) : null;
      return el("div", { class: "ui-stack ui-stack--lg" },
        ui.notice(data.full_control ? "Full Control is on, so nothing is asking. Remembered approvals only apply when it is off." : "These are kept in memory only and end by themselves. Turning Full Control off, or restarting the hub, clears them. Critical actions (shutdown, deleting folders, installing software...) can never be remembered.", { icon: "shield" }),
        rows.length ? group(`Active (${rows.length})`, rows) : ui.notice("Nothing is remembered right now.", { tone: "good" }), revokeAll);
    }, { tall: false });
  }

  let loggingOut = false;
  function logoutAllRow() {
    return ui.listRow({ icon: "lock", tone: "bad", title: "Log out of all devices", sub: "Every phone and browser must log in again", chevron: true,
      onClick: async () => {
        if (loggingOut) return;
        if (!await ui.confirm({ title: "Log out of all devices?", message: "Every phone and browser signed in to this hub, including this one, will have to enter the token again.", confirmLabel: "Log out everywhere", danger: true })) return;
        if (loggingOut) return;
        loggingOut = true;
        try {
          await api("/api/logout-all", { method: "POST" });
          window.dispatchEvent(new Event("hub:unauthorized"));
        } catch (error) {
          if (error.status !== 401) fail(error);
        } finally { loggingOut = false; }
      } });
  }

  function paint() {
    appearanceCard = appearanceCard || appearance();
    if (S && !usageEl) usageEl = usageCard({ onEdit: () => capsSheet(S, () => { load(); usageEl.refresh(); }) });
    if (S && !gmailEl) gmailEl = gmailCard({ S });
    if (S && !providersEl) providersEl = providersCard();
    if (S && !mcpEl) mcpEl = mcpCard();
    root.replaceChildren(...[glance(), usageEl, gmailEl, providersEl, mcpEl, appearanceCard, ...main()].filter(Boolean));
  }

  async function load() {
    if (!S) root.replaceChildren(appearanceCard = appearanceCard || appearance(), ui.skeleton("list", 5));
    try {
      S = await api("/api/settings");
      paint();
      api("/api/notifications/settings").then((n) => { notify = n; paint(); }).catch(() => {});
      api("/api/assistant/permissions").then((p) => { grantCount = Array.isArray(p.grants) ? p.grants.length : 0; paint(); }).catch(() => {});
      api("/api/settings/diagnostics").then((rows) => {
        const list = Array.isArray(rows) ? rows : [];
        diag = { bad: list.filter((r) => r.level === "fail").length, warn: list.filter((r) => r.level === "warn").length };
        paint();
      }).catch(() => {});
    } catch (error) {
      root.replaceChildren(appearanceCard = appearanceCard || appearance(),
        ui.notice(error.status === 404 ? "This hub has no settings service yet." : error.message, { tone: "bad" }),
        ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: () => load() }));
    }
  }

  // PATCH /api/settings and keep S fresh.
  async function patch(body) {
    const result = await api("/api/settings", { method: "PATCH", json: body });
    S = { ...S, ...result };
    if (result.note) ui.toast(result.note, { kind: "warn" });
    return result;
  }

  // A sheet whose body is rebuilt by `render()` (async, returns nodes).
  function drill(title, render, { tall = true, actions = [] } = {}) {
    const s = gsheet({ title, content: ui.skeleton("list", 4), class: "st-sheet" + (tall ? " st-sheet--tall" : ""), actions,
      onClose: () => { s.closed = true; s.onClose?.(); } });
    s.reload = async () => {
      try { const nodes = await render(s); if (!s.closed) s.body.replaceChildren(...[nodes].flat().filter(Boolean)); }
      catch (error) { if (!s.closed) s.body.replaceChildren(ui.notice(error.message || "Couldn't load this.", { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", onClick: s.reload })); }
    };
    s.reload();
    return s;
  }

  // ---- models ------------------------------------------------------------------------------------
  function openModels() {
    return drill("Models", async (s) => {
      const [m, cfg] = await Promise.all([api("/api/settings/models"), api("/api/settings")]);
      S = { ...S, ...cfg };
      const installed = Array.isArray(m.models) ? m.models : [];
      const rows = MODEL_SLOTS.map((slot) => {
        const name = S[slot.key] || "";
        const isInstalled = slot.source === "ollama" && m.reachable ? installed.some((x) => sameModel(x.name, name)) : null;
        return ui.listRow({ icon: slot.icon, tone: slot.tone, title: slot.label, sub: name || slot.sub, chevron: true,
          meta: isInstalled === false ? ui.badge("Not installed", "warn") : null,
          onClick: () => pickModel(slot, installed, m.reachable, s) });
      });
      const P = S.providers || {};
      const names = [P.claude?.ready && "Claude (API key)", P.claude_login?.ready && "Claude (my login)", P.gemini?.ready && "Gemini", P.duo?.ready && "Claude + Gemini"].filter(Boolean);
      const claude = names.length || S.has_claude_key
        ? ui.notice(`Cloud AI ready: ${names.join(", ") || "Claude"}. Default for everyday jobs: ${S.resolved_provider || "local"}. Keys are never shown here.`, { tone: "good", icon: "key" })
        : ui.notice("No cloud AI yet. On the laptop, run python -m hub setup --only ai to use Gemini (free key) or your Claude login.", { icon: "key" });
      const status = m.reachable ? null : ui.notice(m.error || "The local model server isn't running. Start Ollama on the laptop.", { tone: "warn" });
      const catalogueRow = ui.listRow({ icon: "layers", tone: "accent", title: "Local models, deep thinking, benchmark",
        sub: "Catalogue (DeepSeek, Qwen, Gemma...), install, remove, speed test", chevron: true, onClick: () => openLocalModels(s) });
      return el("div", { class: "ui-stack ui-stack--lg" }, status, claude, group("Which model does what", rows),
        group("Local models", catalogueRow), pullCard(s), installedList(installed));
    });
  }

  function openLocalModels(parent) {
    const panel = modelsPanel({ patch, onChange: () => parent.reload?.() });
    return gsheet({ title: "Local models", content: panel, class: "st-sheet st-sheet--tall",
      onClose: () => { panel.dispatchEvent(new Event("lm-close")); parent.reload?.(); } });
  }

  function installedList(installed) {
    if (!installed.length) return null;
    return group(`On this laptop (${installed.length})`, installed.map((x) => ui.listRow({
      icon: "layers", tone: "gray", title: x.name, sub: [x.params, x.size_mb ? fmtMb(x.size_mb) : ""].filter(Boolean).join(" · ") || null })));
  }

  function pullCard(parent) {
    const input = el("input", { type: "text", placeholder: "e.g. llama3.2:3b", autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": "Model name" });
    const go = ui.button({ label: "Download", icon: "download", variant: "primary" });
    const bar = ui.progressBar(0, "Download progress");
    const line = el("p", { class: "ui-hint st-pullline" });
    const progress = el("div", { class: "st-pullprog", hidden: true }, bar, line);
    let controller = null;
    const prevClose = parent.onClose;
    parent.onClose = () => { controller?.abort(); prevClose?.(); };
    go.addEventListener("click", async () => {
      const name = input.value.trim();
      if (!/^[A-Za-z0-9._:/-]{1,80}$/.test(name) || name.includes("..") || /^[/-]/.test(name)) { fail({ message: "That isn't a valid model name." }); return; }
      if (!await ui.confirm({ title: `Download ${name}?`, message: "Models can be several gigabytes. It downloads on the laptop and may take a while.", confirmLabel: "Download" })) return;
      controller = new AbortController();
      progress.hidden = false; bar.set(null); line.textContent = "Starting…"; line.classList.remove("is-error");
      go.disabled = true;
      try {
        for await (const ev of sse("/api/settings/models/pull", { name, confirm: true }, controller.signal)) {
          if (ev.error) throw new Error(ev.error);
          if (ev.percent != null) bar.set(ev.percent / 100); else bar.set(null);
          line.textContent = `${ev.status || "Downloading"}${ev.percent != null ? ` · ${Math.round(ev.percent)}%` : ""}`;
        }
        if (!parent.closed) { bar.set(1); good(`${name} is ready.`); input.value = ""; parent.reload(); }
      } catch (error) {
        line.textContent = error.message || "The download failed."; line.classList.add("is-error"); bar.set(0);
      } finally { go.disabled = false; controller = null; }
    });
    return group("Download a model",
      el("div", { class: "ui-row st-pull" }, el("div", { class: "ui-stack grow" }, el("div", { class: "st-pullrow" }, input, go),
        el("p", { class: "ui-hint" }, "Names come from ollama.com/library."), progress)));
  }

  function pickModel(slot, installed, reachable, parent) {
    const current = S[slot.key] || "";
    let chosen = current;
    const input = el("input", { type: "text", value: current, autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": `${slot.label} model name` });
    const list = el("div", { class: "ui-list ui-list--inset" });
    const options = slot.source === "ollama" ? installed.map((x) => x.name) : slot.source === "whisper" ? WHISPER_CHOICES : [];
    const paintList = () => list.replaceChildren(...options.map((name) => {
      const on = sameModel(name, chosen);
      const r = el("button", { type: "button", class: "ui-row ui-row--interactive st-choice", role: "radio", "aria-checked": String(on) },
        el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, name)), on ? ui.icon("check", { class: "st-choice__tick" }) : null);
      r.addEventListener("click", () => { chosen = name; input.value = name; paintList(); ui.haptic("select"); });
      return r;
    }));
    paintList();
    input.addEventListener("input", () => { chosen = input.value.trim(); paintList(); });
    const result = el("div", { class: "st-testresult", "aria-live": "polite" });
    const test = ui.button({ label: "Test speed", icon: "zap", variant: "tonal", size: "sm" });
    test.addEventListener("click", () => {
      const name = input.value.trim();
      if (!name) return;
      ui.busy(test, async () => {
        result.replaceChildren();
        try {
          const r = await api("/api/settings/models/test", { method: "POST", json: { model: name } });
          result.replaceChildren(r.ok ? ui.notice(`Works. It answered in ${r.latency_ms} ms${r.reply ? `: “${String(r.reply).slice(0, 80)}”` : "."}`, { tone: "good" })
            : ui.notice(r.error || "That model didn't answer.", { tone: "bad" }));
        } catch (error) { result.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
      });
    });
    const body = el("div", { class: "ui-stack ui-stack--lg" },
      options.length ? el("div", {}, el("p", { class: "ui-caps st-caps" }, slot.source === "ollama" ? "Installed" : "Common sizes"), list)
        : (slot.source === "ollama" ? ui.notice(reachable ? "No models installed yet. Download one first." : "The model server is offline, so type the name.", { tone: "warn" }) : null),
      el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, options.length ? "Or type a name" : "Model name"), input,
        el("span", { class: "ui-hint" }, slot.sub)),
      slot.source === "ollama" ? test : null, result);
    gsheet({ title: slot.label + " model", content: body, class: "st-sheet", actions: [
      { label: "Save", variant: "primary", icon: "check", onClick: async () => {
        const name = input.value.trim();
        if (!name) { fail({ message: "Enter a model name." }); return false; }
        if (name === current) return true;
        try { await patch({ [slot.key]: name }); good(`${slot.label} model set to ${name}.`); parent.reload(); paint(); return true; }
        catch (error) { fail(error); return false; }
      } }] });
  }

  // ---- folders -----------------------------------------------------------------------------------
  function openFolders() {
    return drill("Folders", async (s) => {
      S = { ...S, ...(await api("/api/settings")) };
      const roots = Array.isArray(S.roots) ? S.roots : [];
      if (!roots.length) return ui.emptyState({ icon: "folder", title: "No folders set up", body: "Folders are added in hub.json on the laptop." });
      const kb = new Set(S.kb_roots || []);
      const writable = new Set(S.writable_roots || []);
      const kbRows = roots.map((r) => {
        const row = switchRow({ icon: "folder", tone: "orange", title: r.name, sub: r.exists ? "Search and answer from it" : "Folder missing on the laptop", checked: kb.has(r.name), disabled: !r.exists,
          onChange: async (on) => {
            const next = new Set(kb); if (on) next.add(r.name); else next.delete(r.name);
            try { await patch({ kb_roots: [...next] }); kb.clear(); next.forEach((n) => kb.add(n)); paint(); }
            catch (error) { row.switch.input.checked = !on; fail(error); }
          } });
        return row;
      });
      const wRows = roots.map((r) => {
        const row = switchRow({ icon: "pen", tone: "bad", title: r.name, sub: r.exists ? "The assistant may change files here" : "Folder missing on the laptop", checked: writable.has(r.name), disabled: !r.exists,
          onChange: async (on) => {
            try {
              if (on) {
                const ok = await ui.confirm({ title: `Let the assistant edit “${r.name}”?`, danger: true, confirmLabel: "Allow changes",
                  message: "It will be able to create, move and overwrite files in this folder. Only allow folders you have backed up." });
                if (!ok) { row.switch.input.checked = false; return; }
                await patch({ writable_roots: [...writable, r.name], confirm: true });
                writable.add(r.name);
              } else {
                await patch({ writable_roots: [...writable].filter((n) => n !== r.name) });
                writable.delete(r.name);
              }
              paint();
            } catch (error) { row.switch.input.checked = !on; fail(error); }
          } });
        return row;
      });
      return el("div", { class: "ui-stack ui-stack--lg" },
        group("Knowledge folders", kbRows), el("p", { class: "ui-hint st-note" }, "Notes and documents here can be searched and used to answer questions."),
        group("Editable folders", wRows),
        ui.notice("Editing is off by default. Each folder needs its own confirmation.", { tone: "warn" }));
    });
  }

  // ---- push (ntfy) -------------------------------------------------------------------------------
  const randomTopic = () => {
    const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
    const bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
  };

  function openPush() {
    let freshTopic = "";
    return drill("Phone push", async (s) => {
      notify = await api("/api/notifications/settings");
      if (S) S = { ...S, ntfy_server: notify.ntfy_server || S.ntfy_server };
      paint();
      const topic = el("input", { type: "text", maxlength: "64", placeholder: notify.ntfy_enabled ? (notify.ntfy_topic_hint || "Topic is set") : "A long random name", autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": "ntfy topic" });
      topic.value = freshTopic;
      const server = el("input", { type: "url", value: notify.ntfy_server || (S && S.ntfy_server) || "", placeholder: "https://ntfy.sh", autocapitalize: "off", autocomplete: "off", spellcheck: "false", "aria-label": "ntfy server" });
      const dice = ui.button({ label: "Make a random topic", icon: "crosshair", variant: "tonal", size: "sm" });
      dice.addEventListener("click", () => { topic.value = randomTopic(); ui.haptic("select"); });
      const saveTopic = ui.button({ label: "Save topic", icon: "check", variant: "primary", size: "sm" });
      const steps = el("div", { class: "ui-card st-steps-card" });
      const paintSteps = () => {
        const t = freshTopic || topic.value.trim();
        steps.replaceChildren(el("h3", { class: "ui-card__title" }, "Set up the ntfy app"),
          el("ol", { class: "st-steps" },
            el("li", {}, "Install the free ", el("strong", {}, "ntfy"), " app from Google Play or F-Droid."),
            el("li", {}, "Open it, tap ", el("strong", {}, "+"), " and subscribe to this topic:"),
            el("li", { class: "st-topicline" }, t ? el("code", { class: "st-topic" }, t) : el("span", { class: "ui-hint" }, "Make or enter a topic above first."),
              t ? ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm", onClick: async () => { try { await navigator.clipboard.writeText(t); good("Topic copied."); } catch (_) { fail({ message: "Couldn't copy. Select the text instead." }); } } }) : null),
            el("li", {}, "If you use your own server, set it as the default server in the app.")));
      };
      topic.addEventListener("input", () => { if (!freshTopic) paintSteps(); });
      paintSteps();
      saveTopic.addEventListener("click", () => ui.busy(saveTopic, async () => {
        const v = topic.value.trim();
        if (!/^[A-Za-z0-9_-]{8,64}$/.test(v)) { fail({ message: "Use 8 to 64 letters, numbers, dashes or underscores." }); return; }
        const srv = server.value.trim();
        try {
          if (srv && srv !== (S?.ntfy_server || "")) await patch({ ntfy_server: srv });
          notify = await api("/api/notifications/settings", { method: "POST", json: { ntfy_topic: v } });
          freshTopic = v; good("Push is on."); paint(); s.reload();
        } catch (error) { fail(error); }
      }));
      const saveServer = ui.button({ label: "Save server", variant: "tonal", size: "sm" });
      saveServer.addEventListener("click", () => ui.busy(saveServer, async () => {
        try { await patch({ ntfy_server: server.value.trim() }); good("Server saved."); } catch (error) { fail(error); }
      }));
      const start = notify.quiet_hours?.start || "22:00", end = notify.quiet_hours?.end || "07:00";
      const qStart = el("input", { type: "time", value: start, "aria-label": "Quiet from" });
      const qEnd = el("input", { type: "time", value: end, "aria-label": "Quiet until" });
      const saveQuiet = async (on) => {
        try { notify = await api("/api/notifications/settings", { method: "POST", json: { quiet_hours: on ? { start: qStart.value, end: qEnd.value } : null } }); paint(); }
        catch (error) { fail(error); }
      };
      const quietTimes = el("div", { class: "st-times", hidden: !notify.quiet_hours }, el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "From"), qStart),
        el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Until"), qEnd));
      for (const t of [qStart, qEnd]) t.addEventListener("change", () => saveQuiet(true));
      const quiet = switchRow({ icon: "moon", tone: "violet", title: "Quiet hours", sub: "No phone buzz in this window. The app list still fills.", checked: !!notify.quiet_hours,
        onChange: (on) => { quietTimes.hidden = !on; saveQuiet(on); } });
      const level = ui.segmented({ label: "Lowest level to push", block: true, value: notify.min_level || "info",
        options: [{ value: "info", label: "Everything" }, { value: "warn", label: "Warnings" }, { value: "error", label: "Errors" }],
        onChange: async (v) => { try { notify = await api("/api/notifications/settings", { method: "POST", json: { min_level: v } }); } catch (error) { fail(error); } } });
      const test = ui.button({ label: "Send a test", icon: "send", variant: "secondary", block: true });
      test.addEventListener("click", () => ui.busy(test, async () => {
        try { await api("/api/notifications/test", { method: "POST", json: {} }); ctx.refreshNotifications?.(); good(notify.ntfy_enabled ? "Sent. Check your phone." : "Added to your notifications. Set a topic to get it on your phone."); }
        catch (error) { fail(error); }
      }));
      const remove = notify.ntfy_enabled && notify.ntfy_topic_from === "settings" ? ui.button({ label: "Turn push off", icon: "trash", variant: "danger-soft", block: true,
        onClick: async () => {
          if (!await ui.confirm({ title: "Turn off phone push?", message: "The topic is forgotten. Notifications still appear in the app.", confirmLabel: "Turn off", danger: true })) return;
          try { notify = await api("/api/notifications/settings", { method: "POST", json: { ntfy_topic: "" } }); freshTopic = ""; paint(); s.reload(); } catch (error) { fail(error); }
        } }) : null;
      return el("div", { class: "ui-stack ui-stack--lg" },
        ui.notice(notify.ntfy_enabled ? `Push is on${notify.ntfy_topic_hint ? ` (topic ${notify.ntfy_topic_hint})` : ""}.` : "Push is off. Set a topic to get alerts on your phone.", { tone: notify.ntfy_enabled ? "good" : null, icon: "bell" }),
        ui.notice("ntfy topics are public. Anyone who guesses yours can read what is sent. Use the random topic and keep secrets out of alerts.", { tone: "warn" }),
        el("div", { class: "ui-card ui-stack" }, el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Topic"), topic,
          el("span", { class: "ui-hint" }, "8 to 64 letters, numbers, dashes or underscores. The saved topic is never shown again.")),
        el("div", { class: "ui-hstack st-wrap" }, dice, saveTopic)),
        steps,
        el("div", { class: "ui-card ui-stack" }, el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "ntfy server"), server,
          el("span", { class: "ui-hint" }, "Must start with https://. Leave as is for ntfy.sh.")), el("div", {}, saveServer)),
        group("When to push", quiet), quietTimes,
        el("div", { class: "ui-stack" }, el("p", { class: "ui-caps st-caps" }, "Lowest level"), level),
        test, remove);
    });
  }

  // ---- phone capture -----------------------------------------------------------------------------
  function openCapture() {
    const plugin = capture();
    let onVisible = null;
    const s = drill("Phone capture", async () => {
      const steps = el("div", { class: "ui-card st-steps-card" }, el("h3", { class: "ui-card__title" }, "Set it up"),
        el("ol", { class: "st-steps" },
          el("li", {}, el("strong", {}, "Notification access. "), "Tap Open notification access and switch REDLINE on."),
          el("li", {}, el("strong", {}, "Greyed out? "), "Android 13 and newer block this for apps installed from an APK. Go to Settings, Apps, REDLINE, the three dots at the top right, then ", el("strong", {}, "Allow restricted settings"), ". Try step 1 again."),
          el("li", {}, el("strong", {}, "Bank SMS (optional). "), "Tap Allow SMS. The app can only receive new texts, never read your inbox."),
          el("li", {}, el("strong", {}, "Test. "), "A fake “Paid ₹1” should appear in Spending."),
          el("li", {}, el("strong", {}, "Keep it running. "), "Set the app's battery use to Unrestricted so it isn't put to sleep.")));
      if (!plugin) {
        return el("div", { class: "ui-stack ui-stack--lg" }, ui.emptyState({ icon: "phone", title: "Only works in the installed Android app", body: "Open REDLINE from the Android app on your phone to turn on spending and message capture." }), steps);
      }
      let st;
      try { st = await plugin.status(); } catch (error) { throw new Error("The app didn't answer. " + (error?.message || "")); }
      const act = (fn, okText) => async () => { try { const r = await fn(); if (okText) good(typeof okText === "function" ? okText(r) : okText); } catch (error) { fail(error); } s.reload(); };
      const enabled = switchRow({ icon: "check-circle", tone: "good", title: "Capture payments", sub: "Watch payment apps and bank texts for money you spend", checked: !!st.enabled,
        onChange: act(() => plugin.setEnabled({ enabled: !st.enabled })) });
      const queued = Number(st.queued) || 0;
      const rows = [
        ui.listRow({ icon: "bell", tone: st.listenerEnabled ? "good" : "warn", title: "Notification access", sub: st.listenerEnabled ? "On" : "Off. Needed to see payment notifications.",
          meta: st.listenerEnabled ? ui.badge("On", "good") : null, chevron: !st.listenerEnabled,
          onClick: st.listenerEnabled ? undefined : act(() => plugin.openListenerSettings()) }),
        ui.listRow({ icon: "mail", tone: st.smsGranted ? "good" : "gray", title: "Bank SMS", sub: st.smsGranted ? "Allowed. Receive only." : "Optional. Catches banks that don't send notifications.",
          meta: st.smsGranted ? ui.badge("On", "good") : null, chevron: !st.smsGranted, onClick: st.smsGranted ? undefined : act(() => plugin.requestSms(), (r) => (r?.granted ? "SMS allowed." : "SMS wasn't allowed.")) }),
        enabled,
      ];
      const actions = [
        ui.listRow({ icon: "send", tone: "accent", title: "Send a test payment", sub: "Adds a fake ₹1 to Spending", chevron: true,
          onClick: act(() => plugin.test(), (r) => (r?.sent ? "Test sent. Check Spending." : `Not sent${r?.error ? ` (${r.error})` : ""}. It stays queued.`)) }),
        ui.listRow({ icon: "upload", tone: "info", title: "Send waiting items now", sub: queued ? `${queued} waiting to reach the hub` : "Nothing waiting", chevron: !!queued,
          onClick: queued ? act(() => plugin.flush(), (r) => `${r?.sent || 0} sent, ${r?.queued || 0} still waiting${r?.error ? ` (${r.error})` : ""}.`) : undefined }),
      ];
      const watchNote = ui.notice("Mail and chat watching is turned on from the Watch screen.", { icon: "eye" });
      if (!onVisible) { onVisible = () => { if (document.visibilityState === "visible" && !s.closed) s.reload(); }; document.addEventListener("visibilitychange", onVisible); }
      return el("div", { class: "ui-stack ui-stack--lg" }, group("Status", rows), group("Try it", actions), steps, watchNote);
    });
    s.onClose = () => { if (onVisible) document.removeEventListener("visibilitychange", onVisible); };
    return s;
  }

  // ---- diagnostics -------------------------------------------------------------------------------
  function openDiagnostics() {
    return drill("Diagnostics", async () => {
      const rows = await api("/api/settings/diagnostics");
      const list = Array.isArray(rows) ? rows : [];
      diag = { bad: list.filter((r) => r.level === "fail").length, warn: list.filter((r) => r.level === "warn").length };
      paint();
      if (!list.length) return ui.emptyState({ icon: "activity", title: "No checks reported" });
      const tone = { ok: "good", warn: "warn", fail: "bad" }, icon = { ok: "check-circle", warn: "alert", fail: "alert-circle" };
      let timing = "";
      try { timing = (await api("/api/latency?n=5")).summary || ""; } catch (_) { /* older hub */ }
      const summary = diag.bad ? ui.notice(`${diag.bad} thing${diag.bad > 1 ? "s" : ""} need fixing.`, { tone: "bad" })
        : diag.warn ? ui.notice(`All good. ${diag.warn} optional tip${diag.warn > 1 ? "s" : ""}.`, { tone: "warn" }) : ui.notice("Everything is set up.", { tone: "good" });
      return el("div", { class: "ui-stack ui-stack--lg" }, summary, timing ? el("p", { class: "muted", "data-testid": "latency-line" }, timing) : null, group(null, list.map((r) => {
        const lvl = r.level || (r.ok ? "ok" : "fail");
        const node = ui.listRow({ icon: icon[lvl] || "info", tone: tone[lvl] || "gray", title: r.name, sub: r.detail });
        if (r.fix && lvl !== "ok") node.querySelector(".ui-row__body").append(el("span", { class: "st-fix" }, ui.icon("wand", 14), r.fix));
        return node;
      })), ui.button({ label: "Check again", icon: "refresh", variant: "tonal", block: true, onClick: (e) => ui.busy(e.currentTarget, () => s.reload()) }));
    });
  }

  // ---- audit viewer ------------------------------------------------------------------------------
  function openAudit() {
    const state = { q: "", event: "", before: null, done: false, loading: false, gen: 0 };
    const list = el("div", { class: "ui-list ui-list--inset st-audit" });
    const status = el("div", { class: "st-auditstatus" });
    const sentinel = el("div", { class: "st-sentinel", "aria-hidden": "true" });
    const search = el("input", { type: "search", placeholder: "Search the log", "aria-label": "Search the activity log", autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
    const select = el("select", { "aria-label": "Filter by event" }, el("option", { value: "" }, "All events"));
    const s = gsheet({ title: "Activity log", class: "st-sheet st-sheet--tall", content: el("div", { class: "ui-stack" },
      el("div", { class: "st-auditbar" }, el("div", { class: "ui-input-wrap" }, ui.icon("search"), search), select), list, status, sentinel) });
    let io = null, timer = null;
    const prev = s.close;
    s.close = (o) => { io?.disconnect(); clearTimeout(timer); prev(o); };

    const fieldsOf = (r) => Object.entries(r).filter(([k]) => !["time", "event", "pos"].includes(k));
    const fmt = (v) => (typeof v === "string" ? v : JSON.stringify(v));
    function rowNode(r) {
      const f = fieldsOf(r);
      const summary = f.slice(0, 3).map(([k, v]) => `${k}: ${fmt(v)}`).join(" · ");
      const detail = el("dl", { class: "st-fields", hidden: true }, f.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, fmt(v))]));
      const node = el("button", { type: "button", class: "ui-row ui-row--interactive st-auditrow", "aria-expanded": "false" },
        el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, String(r.event || "event").replace(/_/g, " ")),
          summary ? el("span", { class: "ui-row__sub ui-clamp-2" }, summary) : null, detail),
        el("span", { class: "ui-row__meta" }, ui.relTime(r.time) || ""));
      node.addEventListener("click", () => { const open = detail.hidden; detail.hidden = !open; node.setAttribute("aria-expanded", String(open)); node.querySelector(".ui-row__sub")?.classList.toggle("ui-clamp-2", !open); });
      return node;
    }
    async function more(reset) {
      if (reset) { state.gen += 1; state.before = null; state.done = false; state.loading = false; list.replaceChildren(); }
      if (state.loading || state.done) return;
      state.loading = true;
      const gen = state.gen;
      status.replaceChildren(el("span", { class: "ui-spinner", role: "status", "aria-label": "Loading" }));
      try {
        const q = new URLSearchParams({ limit: "40" });
        if (state.q) q.set("q", state.q);
        if (state.event) q.set("event", state.event);
        if (state.before != null) q.set("before", String(state.before));
        const data = await api(`/api/settings/audit?${q}`);
        if (gen !== state.gen || s.closed) return;
        const rows = Array.isArray(data.rows) ? data.rows : [];
        list.append(...rows.map(rowNode));
        state.before = data.next_before;
        state.done = data.next_before == null || !rows.length;
        status.replaceChildren();
        if (!list.children.length) status.append(ui.emptyState({ icon: "history", title: state.q || state.event ? "Nothing matches" : "The log is empty", compact: true }));
        else if (state.done) status.append(el("p", { class: "ui-hint st-end" }, data.truncated ? "Older entries are too far back to load." : "That's everything."));
        else status.append(ui.button({ label: "Load more", variant: "tonal", size: "sm", onClick: () => more() }));
      } catch (error) { if (gen === state.gen) status.replaceChildren(ui.notice(error.message, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => more() })); }
      finally { if (gen === state.gen) state.loading = false; }
    }
    if ("IntersectionObserver" in window) {
      io = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting) && list.children.length) more(); }, { root: s.body, rootMargin: "240px" });
      io.observe(sentinel);
    }
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = search.value.trim(); more(true); }, 300); });
    select.addEventListener("change", () => { state.event = select.value; more(true); });
    api("/api/settings/audit/events").then((d) => {
      for (const name of d.events || []) select.append(el("option", { value: name }, `${name.replace(/_/g, " ")} (${d.counts?.[name] ?? 0})`));
    }).catch(() => { select.hidden = true; });
    more(true);
    return s;
  }

  // ---- backups -----------------------------------------------------------------------------------
  function openBackup() {
    return drill("Backups", async (s) => {
      const data = await api("/api/settings/backups");
      const items = Array.isArray(data.backups) ? data.backups : [];
      const make = ui.button({ label: "Create a backup now", icon: "download", variant: "primary", block: true });
      make.addEventListener("click", async () => {
        if (!await ui.confirm({ title: "Create a backup?", message: "Your notes, data and activity log are zipped on the laptop. The hub config and token are left out. The newest 5 are kept.", confirmLabel: "Create backup" })) return;
        ui.busy(make, async () => {
          try { const r = await api("/api/settings/backup", { method: "POST", json: { confirm: true } }); good(`Backup saved (${fmtBytes(r.size)}, ${r.files} files).`); await s.reload(); }
          catch (error) { fail(error); }
        });
      });
      return el("div", { class: "ui-stack ui-stack--lg" }, make,
        items.length ? group("Saved backups", items.map((b) => {
          const link = el("a", { class: "ui-btn ui-btn--tonal ui-btn--sm", href: `/api/settings/backups/${encodeURIComponent(b.name)}`, download: b.name, "aria-label": `Download ${b.name}` }, ui.icon("download"), "Get");
          return ui.listRow({ icon: "layers", tone: "info", title: when(b.created) || b.name, sub: `${fmtBytes(b.size)} · ${b.name}`, meta: link });
        })) : ui.emptyState({ icon: "download", title: "No backups yet", body: "Make one before big changes.", compact: true }),
        el("p", { class: "ui-hint st-note" }, "Backups hold your data folder, notes and activity log. They never include hub.json, your token or login secrets."));
    });
  }

  // ---- about -------------------------------------------------------------------------------------
  function openAbout() {
    return drill("About", async () => {
      const [about, cfg] = await Promise.all([api("/api/settings/about"), api("/api/settings")]);
      S = { ...S, ...cfg };
      const up = Number(about.uptime_s) || 0;
      const days = Math.floor(up / 86400), hrs = Math.floor((up % 86400) / 3600), mins = Math.floor((up % 3600) / 60);
      const uptime = days ? `${days}d ${hrs}h` : hrs ? `${hrs}h ${mins}m` : `${mins}m`;
      const kv = (k, v) => ui.listRow({ icon: null, title: k, meta: v, lead: el("span", { class: "st-nolead" }) });
      const locked = Array.isArray(S.locked) ? S.locked : [];
      return el("div", { class: "ui-stack ui-stack--lg" },
        group(null, kv("Version", about.version || S.version || "?"), kv("Running for", uptime), kv("Python", about.python || "?"), kv("System", about.platform || S.platform || "?"),
          kv("Settings saved to file", S.config_persistable ? "Yes" : "Until restart")),
        group("Set up on the laptop", kv("Shell profiles", String(S.shells?.length || 0)), kv("Apps", String(S.apps?.length || 0)), kv("Quick buttons", String(S.scripts?.length || 0))),
        locked.length ? el("div", { class: "ui-card ui-stack" }, el("h3", { class: "ui-card__title" }, "Locked for safety"),
          el("p", { class: "ui-hint" }, "These can only be changed by editing hub.json on the laptop, never from the phone."),
          el("div", { class: "st-lockedlist" }, locked.map((k) => el("code", { class: "st-topic" }, k)))) : null);
    });
  }

  // ---- sections ----------------------------------------------------------------------------------
  const OPENERS = { models: openModels, folders: openFolders, push: openPush, capture: openCapture, diagnostics: openDiagnostics, audit: openAudit, backup: openBackup, about: openAbout, ...OWNER_SECTIONS };
  function openSection(name) {
    const key = SECTION_ALIAS[name] || name;
    if (key === "appearance") {
      requestAnimationFrame(() => {
        const node = root.querySelector("#st-appearance") || appearanceCard;
        node?.scrollIntoView({ behavior: document.documentElement.dataset.motion === "reduce" ? "auto" : "smooth", block: "start" });
      });
      return;
    }
    const open = OPENERS[key];
    if (!open) return;
    const go = () => open();
    if (S) go(); else load().then(go);
  }

  paint();
  load();
  loadUpdate();
  return {
    show() { if (!S) load(); },
    openSection,
    openItem(id, section) { if (section) openSection(section); },
    refresh() { load(); },
  };
}
