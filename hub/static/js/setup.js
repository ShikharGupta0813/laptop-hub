// Setup checklist: which integrations are connected, what each unlocks, and how to fix the rest.
// Read-only. Every probe is independent and time-limited (4 s); a failure shows amber, never a crash.
// Also exports runChecks(ctx) so Home can show a small "Finish setting up" card.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/setup.css";
const TIMEOUT = 4000;

// ---- probing ---------------------------------------------------------------------------------------
function probe(path) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), TIMEOUT);
  return api(path, { signal: ac.signal }).finally(() => clearTimeout(timer));
}
const cache = new Map();
function get(path) { if (!cache.has(path)) cache.set(path, probe(path)); return cache.get(path); }
const unreachable = (e) => e && (e.name === "AbortError" || e.status === 0);
const missing = (e) => e && (e.status === 404 || e.status === 405);
const sentence = (v) => (typeof v === "string" && v.trim() ? v.trim().slice(0, 240) : "");

const R = (level, detail, extra = {}) => ({ level, detail, ...extra });
const FAIL = (e, what = "this") => R("warn", unreachable(e) ? "Didn't answer in time. Try again in a moment." : (sentence(e?.message) || `Couldn't check ${what} just now.`), { failed: true });

// Each check: id, title, icon, tone, unlocks, required?, run(): {level, detail, fix?}
// level: ok (green) | warn (amber, optional) | bad (red problem) | na (grey, not available here) | info (a link)
const cmd = (text, note) => ({ cmd: text, note });
const CHECKS = [
  { id: "laptop", title: "Laptop connection", icon: "laptop", tone: "good", required: true,
    unlocks: "Everything. The phone talks to the hub running on your laptop.",
    async run() {
      try {
        const s = await get("/api/status");
        return s?.online === false ? R("bad", "The hub answers, but the laptop reports offline.", { fix: laptopFix })
          : R("ok", [s?.hostname || s?.name, "Connected"].filter(Boolean).join(" · "));
      } catch (e) { return R("bad", unreachable(e) ? "Can't reach your laptop. Is it awake and the hub running?" : e.message, { fix: laptopFix }); }
    } },
  { id: "model", title: "Local AI model", icon: "cpu", tone: "info", required: true,
    unlocks: "Chat, quizzes, notes tags and the assistant, privately on your laptop.",
    async run() {
      let m;
      try { m = await get("/api/settings/models"); } catch (e) {
        if (!missing(e)) return FAIL(e, "the AI model");
        try { const s = await get("/api/settings"); m = { reachable: s?.ollama?.reachable, models: (s?.ollama?.models || []).map((n) => ({ name: n })), configured: { chat_model: { name: s?.chat_model, installed: (s?.ollama?.models || []).includes(s?.chat_model) } } }; } catch (e2) { return FAIL(e2, "the AI model"); }
      }
      const name = m?.configured?.chat_model?.name || "the chat model";
      if (!m?.reachable) return R("bad", "Ollama isn't running on the laptop.", { fix: ollamaFix(name) });
      if (m?.configured?.chat_model && !m.configured.chat_model.installed) return R("bad", `${name} isn't downloaded yet.`, { fix: ollamaFix(name) });
      return R("ok", `${name} is ready.`, { fix: ollamaFix(name, true) });
    } },
  { id: "claude", title: "Cloud AI (Gemini or Claude login)", icon: "bot", tone: "orange", optional: true,
    unlocks: "Lets you pick Gemini or your Claude login for harder questions and longer drafts. No API key needed.",
    async run() {
      try {
        const s = await get("/api/settings");
        const p = s?.providers || {};
        const ready = [p.claude?.ready && "Claude (API key)", p.claude_login?.ready && "Claude (my login)", p.gemini?.ready && "Gemini", p.duo?.ready && "Claude + Gemini"].filter(Boolean);
        if (!p.claude && s?.has_claude_key) ready.push("Claude (API key)");
        return ready.length ? R("ok", `Ready: ${ready.join(", ")}.`)
          : R("warn", "Not set up yet. Optional; the local model works without it.", { fix: { steps: ["On the laptop, either install Claude Code and run `claude` once to log in (uses your Claude subscription), or save a free Gemini key from aistudio.google.com/apikey."],
            cmds: [cmd("python -m hub setup --only ai", "Walks you through both")] } });
      } catch (e) { return FAIL(e, "cloud AI"); }
    } },
  { id: "embed", title: "Search by meaning", icon: "layers", tone: "teal", optional: true,
    unlocks: "Ask my files finds notes by meaning, not just exact words.",
    async run() {
      try {
        const k = await get("/api/kb/status");
        return k?.embed_available ? R("ok", `${k.embed_model || "Embedding model"} is installed.`)
          : R("warn", sentence(k?.embed_note) || "Not installed. Search still works by keywords.", { fix: { steps: ["Download the small search model with Ollama, then rescan in Knowledge."], cmds: [cmd("ollama pull nomic-embed-text")], open: ["kb", null, "Open Knowledge"] } });
      } catch (e) { return missing(e) ? R("na", "Ask my files isn't on this laptop.") : FAIL(e, "search"); }
    } },
  { id: "gmail", title: "Gmail reading", icon: "mail", tone: "bad", optional: true,
    unlocks: "Inbox, urgent mail alerts and email in your daily brief. Read-only.",
    async run() {
      try {
        const s = await get("/api/inbox/status");
        return s?.configured ? R("ok", s.address ? `Connected as ${s.address}` : "Connected.")
          : R("warn", sentence(s?.reason) || "Not connected yet.", { fix: gmailFix });
      } catch (e) { return missing(e) ? R("na", "Inbox isn't on this laptop.") : FAIL(e, "Gmail"); }
    } },
  { id: "asana", title: "Asana", icon: "list-checks", tone: "orange", optional: true,
    unlocks: "Your tasks on Home, in Work and in Friday's plan.",
    async run() {
      try {
        const w = await get("/api/work/status");
        const a = w?.asana || {};
        return a.configured ? R("ok", a.user ? `Connected as ${a.user}` : "Connected.")
          : R("warn", sentence(a.reason) || "No Asana token yet.", { fix: { steps: ["Create a personal access token in Asana (profile, Apps, Developer apps), then give it to the hub in one of two ways."],
            cmds: [cmd("setx ASANA_TOKEN \"your-token\"", "Option 1, then restart the hub"), cmd("Set-Content $HOME\\.claude\\.asana_token \"your-token\"", "Option 2, PowerShell")] } });
      } catch (e) { return missing(e) ? R("na", "Work isn't on this laptop.") : FAIL(e, "Asana"); }
    } },
  { id: "calendar", title: "Calendar", icon: "calendar", tone: "info", optional: true,
    unlocks: "Today's meetings with Join buttons, and reminders before they start.",
    async run() {
      try {
        const c = (await get("/api/work/status"))?.calendar || {};
        return c.configured ? R("ok", "Meetings are loading.")
          : R("warn", sentence(c.reason) || "Not connected yet.", { fix: jarvisFix("The calendar comes from the Jarvis agenda (iCal link) setup.", "setup calendar") });
      } catch (e) { return missing(e) ? R("na", "Work isn't on this laptop.") : FAIL(e, "the calendar"); }
    } },
  { id: "chat", title: "Google Chat posting", icon: "send", tone: "teal", optional: true,
    unlocks: "Send a message to a Chat space from Work or by voice.",
    async run() {
      try {
        const c = (await get("/api/work/status"))?.chat || {};
        return c.configured ? R("ok", c.spaces?.length ? `${c.spaces.length} space${c.spaces.length === 1 ? "" : "s"} ready.` : "Ready.")
          : R("warn", sentence(c.reason) || "Not connected yet.", { fix: jarvisFix("Chat spaces are added in the Jarvis app.", "setup chat") });
      } catch (e) { return missing(e) ? R("na", "Work isn't on this laptop.") : FAIL(e, "Google Chat"); }
    } },
  { id: "access", title: "Phone notification access", icon: "phone", tone: "pink", optional: true, phone: true,
    unlocks: "Lets the Android app see payment, mail and chat notifications.",
    async run() {
      const hc = capture();
      if (!hc) return R("na", "Only in the Android app. You are in a browser.", { fix: androidFix });
      try {
        const s = await withTimeout(hc.status());
        return s?.listenerEnabled ? R("ok", "Notification access is on.", { fix: androidFix })
          : R("warn", "Notification access is off.", { fix: { steps: ["Tap the button, then switch REDLINE on in Android's Notification access list."], action: "listener" } });
      } catch (e) { return FAIL(e, "the phone"); }
    } },
  { id: "capture", title: "Payment, mail and chat capture", icon: "trend", tone: "good", optional: true, phone: true,
    unlocks: "Spending is logged automatically; urgent mail and chat reach Watch.",
    async run() {
      const hc = capture();
      if (!hc) return R("na", "Only in the Android app.", { fix: androidFix });
      try {
        const s = await withTimeout(hc.status());
        if (!s?.listenerEnabled) return R("warn", "Waiting for notification access first.", { fix: { steps: ["Turn on Notification access first (row above)."], action: "listener" } });
        const on = [s.enabled !== false ? "payments" : null, s.watchMail ? "mail" : null, s.watchChat ? "chat" : null].filter(Boolean);
        return on.length ? R("ok", `Capturing ${on.join(", ")}.${s.queued ? ` ${s.queued} waiting to send.` : ""}`, { fix: { steps: ["Mail and chat capture are switched on from Watch."], open: ["watch", null, "Open Watch"] } })
          : R("warn", "Everything is switched off.", { fix: { steps: ["Turn payments back on in Settings, Phone capture. Turn mail and chat on in Watch."], open: ["settings", "capture", "Phone capture"] } });
      } catch (e) { return FAIL(e, "the phone"); }
    } },
  { id: "push", title: "Push notifications", icon: "bell", tone: "bad", optional: true,
    unlocks: "Important alerts reach your phone even when the app is closed.",
    async run() {
      try {
        const s = await get("/api/settings");
        return s?.ntfy_topic_set ? R("ok", "A push topic is set.")
          : R("warn", "Not set. Alerts only show inside the app.", { fix: { steps: ["Install the free ntfy app, subscribe to a long random topic name, then paste the same name in Settings, Phone push. Topics on ntfy.sh are public, so never use a short word."], open: ["settings", "push", "Phone push"] } });
      } catch (e) { return FAIL(e, "push"); }
    } },
  { id: "screen", title: "Remote screen", icon: "cast", tone: "warn", optional: true,
    unlocks: "See and control the laptop screen from your phone.",
    async run() {
      try {
        const s = await get("/api/screen/status");
        return s?.available ? R("ok", s.control_available ? "Viewing and control are ready." : "Viewing is ready.")
          : R("warn", sentence(s?.reason) || "Screen viewing isn't installed.", { fix: { steps: ["In the hub folder on the laptop, install the screen packages, then restart the hub."], cmds: [cmd("pip install -r requirements-screen.txt")] } });
      } catch (e) { return missing(e) ? R("na", "Remote screen isn't on this laptop.") : FAIL(e, "the screen"); }
    } },
  { id: "workspace", title: "Cloudflare / NiFi workspace", icon: "grid", tone: "info", optional: true,
    unlocks: "One tap connects the VPN, opens your tools and shows the screen.",
    async run() {
      try {
        const w = await get("/api/workspaces");
        const list = Array.isArray(w) ? w : w?.workspaces || [];
        const has = list.some((x) => Array.isArray(x.steps) && x.steps.some((s) => s.type === "script"));
        return has ? R("ok", "A workspace with a script step exists.", { fix: { steps: [], open: ["launcher", null, "Open Launcher"] } })
          : R("warn", "Optional. No workspace runs a script yet.", { fix: { steps: ["Make a workspace that runs your connect script, then your sites and apps in one tap."], open: ["launcher", null, "Open Launcher"] } });
      } catch (e) { return missing(e) ? R("na", "Launcher isn't on this laptop.") : FAIL(e, "workspaces"); }
    } },
  { id: "friday", title: "Friday", icon: "prompt", tone: "accent", optional: true,
    unlocks: "Your morning brief and, when switched on, alerts that speak up for you.",
    async run() {
      try {
        const f = await get("/api/friday/settings");
        return f?.proactive ? R("ok", `${f.name || "Friday"} is on, alerts included.`, { fix: fridayFix })
          : R("warn", `${f?.name || "Friday"} is ready. Proactive alerts are off.`, { fix: fridayFix });
      } catch (e) { return missing(e) ? R("na", "Friday isn't on this laptop.") : FAIL(e, "Friday"); }
    } },
  { id: "writable", title: "Writable folders", icon: "folder", tone: "orange", optional: true,
    unlocks: "Lets the assistant save and edit files for you in folders you approve.",
    async run() {
      try {
        const s = await get("/api/settings");
        const n = (s?.writable_roots || []).length;
        return n ? R("ok", `${n} folder${n === 1 ? "" : "s"} can be edited.`)
          : R("warn", "None yet. The assistant can read but not change files.", { fix: { steps: ["Choose which folders may be edited. You confirm each one."], open: ["settings", "folders", "Open Folders"] } });
      } catch (e) { return FAIL(e, "folders"); }
    } },
  { id: "backup", title: "Backups", icon: "download", tone: "info", optional: true,
    unlocks: "A zip of your notes and data you can restore from.",
    async run() {
      try {
        const b = await get("/api/settings/backups");
        const n = (b?.backups || []).length;
        return n ? R("ok", `${n} backup${n === 1 ? "" : "s"}, newest ${b.backups[0].created ? ui.relTime(b.backups[0].created) : "saved"}.`)
          : R("warn", "No backup yet.", { fix: { steps: ["Make your first backup. It takes a few seconds."], open: ["settings", "backup", "Open Backups"] } });
      } catch (e) { return missing(e) ? R("na", "Backups aren't on this laptop.") : FAIL(e, "backups"); }
    } },
  { id: "security", title: "Session security", icon: "shield", tone: "teal", info: true,
    unlocks: "Log out every device if a phone is lost.",
    async run() { return R("info", "Review", { fix: { steps: ["Settings has an Account & security group. Log out of all devices there if a phone is lost."], open: ["settings", null, "Open Settings"] } }); } },
];

const laptopFix = { steps: ["Make sure the laptop is awake and connected. Start the hub if it isn't running."], cmds: [cmd("python -m hub", "In the hub folder"), cmd("python -m hub doctor", "Checks everything and says what to fix")] };
const ollamaFix = (name, ok) => ({ steps: ok ? ["Everything is fine. For faster answers on a modest laptop, a smaller model helps."] : ["Start Ollama on the laptop and download the model once."],
  cmds: [...(ok ? [] : [cmd("ollama serve", "Only if Ollama isn't already running")]), cmd(`ollama pull ${ok ? "qwen3:1.7b" : name}`, ok ? "Optional: a smaller, faster model" : "Downloads the chat model")],
  open: ["settings", "models", "Open Models"] });
const gmailFix = { steps: ["Create a Gmail app password (Google account, Security, 2-step verification, App passwords).", "Store it with the Jarvis app, which keeps it in the Windows keyring. Nothing is saved in the hub."],
  cmds: [cmd("setup email", "Run in the Jarvis folder and follow the prompts")] };
const jarvisFix = (why, command) => ({ steps: [why, "Set the Jarvis folder when starting the hub (--jarvis-dir), then run the setup in that folder."], cmds: [cmd("pip install -r requirements-work.txt"), cmd(command, "Run in the Jarvis folder")] });
const fridayFix = { steps: ["Name, wake hours and alerts are in Friday's settings."], open: ["friday", "settings", "Open Friday"] };
const androidFix = { steps: ["Notification access and payment capture only exist in the installed Android app. Open the hub there and come back to this page."] };

function capture() {
  try {
    const cap = window.Capacitor;
    if (!cap) return null;
    if (typeof cap.isNativePlatform === "function" && !cap.isNativePlatform()) return null;
    return cap.Plugins?.HubCapture || null;
  } catch (_) { return null; }
}
function withTimeout(p) { return Promise.race([Promise.resolve(p), new Promise((_, rej) => setTimeout(() => rej(Object.assign(new Error("Timed out"), { name: "AbortError" })), TIMEOUT))]); }

export async function runChecks(ctx, force) {
  if (force) cache.clear();
  return Promise.all(CHECKS.map(async (c) => {
    let r;
    try { r = await c.run(); } catch (e) { r = FAIL(e); }
    return { ...c, ...r };
  }));
}

// Summary used by the Home card.
export function summarize(rows) {
  const counted = rows.filter((r) => r.level !== "na" && r.level !== "info");
  const ready = counted.filter((r) => r.level === "ok").length;
  const optional = counted.filter((r) => r.optional);
  const optOk = optional.filter((r) => r.level === "ok").length;
  const laptopOk = rows.find((r) => r.id === "laptop")?.level === "ok";
  const modelOk = ["model", "claude"].some((id) => rows.find((r) => r.id === id)?.level === "ok");
  return { ready, total: counted.length, requiredOk: laptopOk && modelOk, optOk, optTotal: optional.length,
    needs: !(laptopOk && modelOk) || (optional.length > 0 && optOk / optional.length < 0.5) };
}

// ---- UI --------------------------------------------------------------------------------------------
const LEVEL = {
  ok: { label: "Connected", badge: "good", dot: "good" },
  warn: { label: "Optional", badge: "warn", dot: "warn" },
  bad: { label: "Needs fixing", badge: "bad", dot: "bad" },
  na: { label: "Not available", badge: null, dot: "na" },
  info: { label: "Review", badge: "accent", dot: "info" },
};

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
  try {
    const ta = el("textarea", { class: "su-copy-ta", readonly: "", "aria-hidden": "true", tabindex: "-1" });
    ta.value = text; document.body.append(ta); ta.select(); ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy"); ta.remove(); return ok;
  } catch (_) { return false; }
}

export function init(container, ctx) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "su" });
  container.append(root);
  let rows = null;
  let loading = null;

  const ringHost = el("div", { class: "su-ring-host" });
  const heroTitle = el("h2", { class: "su-hero-title" }, "Checking your setup…");
  const heroSub = el("p", { class: "su-hero-sub" }, "Looking at each connection. This takes a few seconds.");
  const hero = el("section", { class: "ui-card ui-card--glass su-hero", "aria-live": "polite" }, ringHost, el("div", { class: "su-hero-text" }, heroTitle, heroSub));
  const list = el("div", { class: "su-list" });
  root.append(hero, list);
  ui.pullToRefresh(container, () => load(true));

  function paintHero() {
    const s = summarize(rows);
    const ring = ui.ringProgress({ value: s.total ? s.ready / s.total : 0, size: 84, stroke: 8, tone: s.requiredOk ? "good" : "warn", big: String(s.ready), small: `of ${s.total}`, label: "Ready" });
    ringHost.replaceChildren(ring);
    heroTitle.textContent = `${s.ready} of ${s.total} ready`;
    heroSub.textContent = !s.requiredOk ? "Fix the red items first. Everything else is optional."
      : s.ready === s.total ? "All set. Nothing left to connect." : "The essentials work. Connect the rest whenever you like.";
  }

  function groupOf(title, ids, hint) {
    const items = rows.filter((r) => ids.includes(r.id));
    if (!items.length) return null;
    return el("section", { class: "su-group", "aria-label": title },
      el("div", { class: "ui-section-head" }, el("h2", { class: "ui-section-title" }, title)),
      hint ? el("p", { class: "su-hint" }, hint) : null,
      el("div", { class: "ui-list ui-list--inset" }, items.map(rowNode)));
  }

  function rowNode(r) {
    const lv = LEVEL[r.level] || LEVEL.warn;
    const meta = el("span", { class: `su-pill su-pill--${r.level}` }, el("span", { class: `su-dot su-dot--${r.level}`, "aria-hidden": "true" }), r.level === "warn" && r.failed ? "Unchecked" : lv.label);
    return ui.listRow({
      icon: r.icon, tone: r.level === "na" ? "gray" : r.tone, title: r.title,
      sub: el("span", { class: "su-sub" }, meta, el("span", { class: "su-unlocks" }, r.unlocks), el("span", { class: `su-detail su-detail--${r.level}` }, r.detail)),
      chevron: true, attrs: { "data-status": r.level, "data-id": r.id },
      onClick: () => openFix(r),
    });
  }

  function paint() {
    paintHero();
    list.replaceChildren(
      groupOf("Essentials", ["laptop", "model", "claude", "embed"]),
      groupOf("Your accounts", ["gmail", "asana", "calendar", "chat"]),
      groupOf("On your phone", ["access", "capture", "push"]),
      groupOf("Laptop extras", ["screen", "workspace", "friday", "writable", "backup", "security"]));
    list.classList.add("ui-stagger"); setTimeout(() => list.classList.remove("ui-stagger"), 1000);
  }

  function openFix(r) {
    const fix = r.fix || {};
    const body = el("div", { class: "su-sheet" },
      el("div", { class: "su-sheet-top" }, ui.tile(r.icon, r.level === "na" ? "gray" : r.tone, "lg"),
        el("div", { class: "grow" }, el("span", { class: `su-pill su-pill--${r.level}` }, el("span", { class: `su-dot su-dot--${r.level}`, "aria-hidden": "true" }), (LEVEL[r.level] || LEVEL.warn).label), el("p", { class: "su-sheet-detail" }, r.detail))),
      el("p", { class: "su-sheet-unlocks" }, r.unlocks));
    if (fix.steps?.length) body.append(el("ol", { class: "su-steps" }, fix.steps.map((t) => el("li", {}, t))));
    for (const c of fix.cmds || []) {
      const btn = ui.button({ label: "Copy", icon: "copy", variant: "tonal", size: "sm" });
      btn.addEventListener("click", async () => {
        const ok = await copyText(c.cmd);
        ui.toast(ok ? "Copied. Paste it on your laptop." : "Couldn't copy. Select the text instead.", { kind: ok ? "good" : "warn" });
        if (ok) ui.haptic("success");
      });
      body.append(el("div", { class: "su-cmd" }, c.note ? el("p", { class: "su-cmd-note" }, c.note) : null,
        el("div", { class: "su-cmd-row" }, el("code", { class: "su-code" }, c.cmd), btn)));
    }
    if (!fix.steps?.length && !fix.cmds?.length && r.level === "ok") body.append(el("p", { class: "su-sheet-ok" }, "Nothing to do here."));
    const actions = [];
    if (fix.action === "listener" && capture()) actions.push({ label: "Open Android settings", icon: "phone", variant: "primary", onClick: async (s) => { try { await capture().openListenerSettings(); } catch (_) { /* ignore */ } s.close(); } });
    if (fix.open) actions.push({ label: fix.open[2] || "Open", icon: "arrow-up-right", variant: "primary", onClick: (s) => { s.close(); ctx.open(fix.open[0], fix.open[1] || undefined); } });
    actions.push({ label: "Re-check", icon: "refresh", variant: "secondary", onClick: (s) => { s.close(); load(true); } });
    ui.sheet({ title: r.title, content: body, actions });
  }

  async function load(force) {
    if (loading) return loading;
    if (!rows) list.replaceChildren(ui.skeleton("list", 6));
    loading = runChecks(ctx, force).then((r) => { rows = r; paint(); }).catch(() => {
      list.replaceChildren(ui.notice("Couldn't run the checks. Pull down to try again.", { tone: "warn" }));
    }).finally(() => { loading = null; });
    return loading;
  }
  load(true);
  return { show() { if (rows) load(true); }, refresh() { return load(true); } };
}
