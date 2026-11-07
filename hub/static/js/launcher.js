// Launcher: start anything on the laptop from the phone, then use it on the live screen.
//
// Data: docs/api/workspaces.md (GET /apps, GET /recents, POST /quick, POST /whole-laptop/launch) and docs/api/screen.md.
// Every launch asks first (confirm), is audited by the hub, and ends on the Remote screen with its Control button.
// Apps are only ever started by an id from the hub's own list; this file never sends a path to run.
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

// Big tiles for these when they are installed and have not been used enough to show up as recents yet.
const FAVOURITES = [
  /^google chrome$/i, /^microsoft edge$/i, /^file explorer$|^explorer$/i, /^visual studio code$|^vs ?code$/i,
  /^windows terminal$|^terminal$|^powershell$|^command prompt$/i, /^firefox$|^mozilla firefox$/i, /^notepad$/i, /^calculator$/i,
];
const ICONS = [[/chrome|edge|firefox|browser|brave|opera/i, "globe", "info"], [/explorer|files|folder/i, "folder", "orange"],
  [/code|studio|editor|pycharm|intellij|sublime/i, "code", "violet"], [/terminal|powershell|command|cmd|bash|shell/i, "terminal", "gray"],
  [/notepad|notes|word|onenote/i, "note", "warn"], [/mail|outlook|teams|slack|zoom|discord/i, "mail", "teal"],
  [/music|spotify|media|vlc|video|photos/i, "play", "pink"], [/calculator|excel|sheet/i, "chart", "good"]];
const appVisual = (name) => { const m = ICONS.find(([re]) => re.test(name)); return m ? { icon: m[1], tone: m[2] } : { icon: "grid", tone: "accent" }; };

export function init(container, ctx) {
  if (!document.querySelector('link[href="/css/launcher.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: "/css/launcher.css" }));
  }
  const S = { apps: null, supported: true, recents: [], windows: null, windowsAll: false, busy: false, error: "", q: "", loaded: false };

  const heroCard = el("section", { class: "ui-card lc-hero", "aria-label": "Whole laptop" });
  const quickCard = el("section", { class: "ui-card lc-card", "aria-label": "Quick launch" });
  const folderCard = el("section", { class: "ui-card lc-card", "aria-label": "Open a folder" });
  const linkCard = el("section", { class: "ui-card lc-card", "aria-label": "Open a link" });
  const windowsCard = el("section", { class: "ui-card lc-card", "aria-label": "Open windows", hidden: true });
  const moreCard = el("section", { class: "ui-card lc-card", "aria-label": "Workspaces" });
  const root = el("div", { class: "lc-root ui-stack ui-stack--lg" }, heroCard, quickCard, folderCard, linkCard, windowsCard, moreCard);
  container.replaceChildren(root);

  const head = (iconName, tone, title, sub, right) => el("div", { class: "lc-head" }, ui.tile(iconName, tone),
    el("div", { class: "lc-head__text" }, el("div", { class: "ui-card__title" }, title), sub ? el("div", { class: "ui-card__sub" }, sub) : null), right || null);

  // ---- hand over to the live screen ----------------------------------------------------------
  const goLive = () => ctx.open("screen", "live");

  async function runQuick(step, label, verb) {
    if (S.busy) return;
    const ok = await ui.confirm({ title: `${verb} ${label}?`, icon: "laptop",
      message: `This will ${verb.toLowerCase()} ${label} on your laptop. Then you will see the laptop screen here, with a Control button.`, confirmLabel: verb });
    if (!ok) return;
    S.busy = true;
    ui.toast(`${verb.replace(/e$/, "")}ing ${label}…`, { kind: "info" });
    try {
      const r = await api("/api/workspaces/quick", { method: "POST", json: { ...step, confirm: true } });
      if (!r.opened) { ctx.toast(r.steps?.[0]?.message || "The laptop could not open that.", { kind: "bad" }); return; }
      ui.haptic("success");
      if (step.type === "app") loadApps(true);
      goLive();
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
    finally { S.busy = false; }
  }

  // ---- whole laptop --------------------------------------------------------------------------
  function renderHero() {
    heroCard.replaceChildren(
      el("div", { class: "lc-hero__top" }, ui.tile("laptop", "violet", "lg"),
        el("div", { class: "lc-head__text" }, el("div", { class: "lc-hero__title" }, "Use my whole laptop"), el("div", { class: "lc-hero__sub" }, "See the screen and take over. Start anything from there."))),
      ui.button({ label: "Show my laptop", icon: "cast", variant: "primary", size: "lg", block: true, onClick: wholeLaptop }));
  }
  async function wholeLaptop() {
    const ok = await ui.confirm({ title: "Open the whole laptop?", icon: "laptop",
      message: "This shows your laptop screen here and then asks if you want to turn control on. Nothing is started on the laptop.", confirmLabel: "Show my laptop" });
    if (!ok) return;
    try {
      await api("/api/workspaces/whole-laptop/launch", { method: "POST", json: { confirm: true } });
      ui.haptic("success");
      goLive();
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
  }

  // ---- quick launch --------------------------------------------------------------------------
  async function loadApps(quiet) {
    try {
      const [apps, recents] = await Promise.all([api("/api/workspaces/apps"), api("/api/workspaces/recents?limit=8").catch(() => ({ recents: [] }))]);
      S.apps = apps.apps || []; S.supported = apps.supported !== false; S.recents = recents.recents || []; S.error = "";
    } catch (error) {
      if (!quiet || !S.apps) S.error = error instanceof ApiError && error.status === 404 ? "This hub has no launcher yet. Update the hub on your laptop." : error.message;
    }
    S.loaded = true;
    renderQuick();
  }
  function favourites() {
    const used = S.recents.filter((r) => r.count > 0).map((r) => ({ id: r.id, name: r.name }));
    const seen = new Set(used.map((a) => a.id));
    const extra = [];
    for (const re of FAVOURITES) {
      const hit = (S.apps || []).find((a) => re.test(a.name) && !seen.has(a.id));
      if (hit) { seen.add(hit.id); extra.push(hit); }
    }
    return [...used, ...extra].slice(0, 8);
  }
  function appTile(a) {
    const v = appVisual(a.name);
    return el("button", { type: "button", class: "lc-tile", "aria-label": `Open ${a.name}`, onclick: () => runQuick({ type: "app", id: a.id }, a.name, "Start") },
      ui.tile(v.icon, v.tone, "lg"), el("span", { class: "lc-tile__name" }, a.name));
  }
  function renderQuick() {
    const search = el("input", { type: "search", class: "lc-search", placeholder: "Search your apps", "aria-label": "Search installed apps", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: "100", value: S.q });
    const results = el("div", { class: "lc-results", "aria-live": "polite" });
    const draw = () => {
      S.q = search.value.trim();
      if (S.error) { results.replaceChildren(ui.notice(S.error, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: () => { S.error = ""; S.apps = null; renderQuick(); loadApps(); } })); return; }
      if (!S.apps) { results.replaceChildren(ui.skeleton("tiles", 4)); return; }
      if (S.q) {
        const needle = S.q.toLowerCase();
        const hits = S.apps.filter((a) => a.name.toLowerCase().includes(needle)).slice(0, 40);
        results.replaceChildren(hits.length
          ? el("div", { class: "ui-list ui-list--inset" }, hits.map((a) => { const v = appVisual(a.name); return ui.listRow({ icon: v.icon, tone: v.tone, title: a.name, sub: a.source === "hub.json" ? "From your list" : "Installed", meta: "Start", onClick: () => runQuick({ type: "app", id: a.id }, a.name, "Start") }); }))
          : ui.emptyState({ icon: "search", title: "No app called that", body: "Check the spelling, or use Whole laptop and open it from the Start menu.", compact: true }));
        return;
      }
      if (!S.apps.length) {
        results.replaceChildren(ui.emptyState({ icon: "grid", title: S.supported ? "No apps found" : "App list needs Windows",
          body: S.supported ? "The Start Menu has nothing to show yet." : "Installed apps are read from the Start Menu of a Windows laptop. Use Whole laptop to open anything meanwhile.", compact: true }));
        return;
      }
      const fav = favourites();
      results.replaceChildren(
        fav.length ? el("div", { class: "lc-grid" }, fav.map(appTile)) : el("span", { hidden: true }),
        el("p", { class: "lc-hint" }, `${S.apps.length} apps found. Type to search them all.`));
    };
    search.addEventListener("input", draw);
    quickCard.replaceChildren(head("zap", "accent", "Quick launch", "One tap opens it on the laptop."), search, results);
    draw();
  }

  // ---- folder --------------------------------------------------------------------------------
  function renderFolder() {
    folderCard.replaceChildren(head("folder", "orange", "Open a folder", "Pick one of your shared folders.",
      ui.button({ label: "Choose", icon: "chevron-right", variant: "tonal", size: "sm", onClick: folderSheet })));
  }
  async function folderSheet() {
    const st = { roots: [], root: "", path: "" };
    const out = el("div", { class: "ui-stack" }, ui.skeleton("list", 3));
    const sheet = ui.sheet({ title: "Open a folder", content: out });
    const draw = async () => {
      try {
        const r = await api(`/api/files?root=${encodeURIComponent(st.root)}&path=${encodeURIComponent(st.path)}`);
        const here = st.path.split("/").filter(Boolean);
        const dirs = (r.entries || []).filter((e) => e.is_dir).sort((a, b) => a.name.localeCompare(b.name)).slice(0, 200);
        out.replaceChildren(
          st.roots.length > 1 ? el("div", { class: "lc-chips" }, st.roots.map((n) => el("button", { type: "button", class: "ui-chip" + (n === st.root ? " is-on" : ""), onclick: () => { st.root = n; st.path = ""; draw(); } }, n))) : el("span", { hidden: true }),
          el("div", { class: "lc-crumb" }, here.length ? ui.button({ label: "Up", icon: "arrow-up", variant: "tonal", size: "sm", onClick: () => { st.path = here.slice(0, -1).join("/"); draw(); } }) : "",
            el("span", { class: "lc-hint" }, `${st.root}${here.length ? " / " + here.join(" / ") : ""}`)),
          ui.button({ label: "Open this folder on the laptop", icon: "external", variant: "primary", block: true, onClick: () => { sheet.close(); runQuick({ type: "folder", root: st.root, path: st.path }, here[here.length - 1] || st.root, "Open"); } }),
          dirs.length ? el("div", { class: "ui-list ui-list--inset" }, dirs.map((e) => ui.listRow({ icon: "folder", tone: "orange", title: e.name, chevron: true, onClick: () => { st.path = [...here, e.name].join("/"); draw(); } })))
            : ui.emptyState({ icon: "folder", title: "No folders inside", compact: true }));
      } catch (error) { out.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
    };
    try {
      st.roots = ((await api("/api/roots")) || []).map((r) => r.name);
      if (!st.roots.length) { out.replaceChildren(ui.emptyState({ icon: "folder", title: "No shared folders", body: "Add folders under 'roots' in hub.json.", compact: true })); return; }
      st.root = st.roots[0];
      draw();
    } catch (error) { out.replaceChildren(ui.notice(error.message, { tone: "bad" })); }
  }

  // ---- link ----------------------------------------------------------------------------------
  function renderLink() {
    const input = el("input", { type: "url", inputmode: "url", autocapitalize: "off", autocorrect: "off", spellcheck: "false", maxlength: "2000", placeholder: "https://example.com", "aria-label": "Web address to open on the laptop" });
    const go = () => {
      let v = input.value.trim();
      if (v && !/^[a-z][a-z0-9+.-]*:/i.test(v) && /^[^\s/]+\.[^\s/]+/.test(v)) v = "https://" + v;
      if (!/^https?:\/\/[^\s/]+/i.test(v)) { ctx.toast("Type an address like example.com or https://example.com", { kind: "warn" }); return; }
      let host = v; try { host = new URL(v).hostname; } catch (_) { /* server validates */ }
      runQuick({ type: "url", url: v }, host, "Open");
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    linkCard.replaceChildren(head("link", "info", "Open a link", "In the laptop's own browser."),
      el("div", { class: "lc-linkrow" }, input, ui.button({ label: "Open", icon: "external", variant: "primary", onClick: go })));
  }

  // ---- windows -------------------------------------------------------------------------------
  async function loadWindows() {
    try { S.windows = (await api("/api/screen/windows")).windows || []; }
    catch (_) { S.windows = null; }       // 501 (not Windows / no pygetwindow), 404 or offline: hide the switcher
    renderWindows();
  }
  function renderWindows() {
    windowsCard.hidden = S.windows == null;
    if (S.windows == null) return;
    const shown = S.windowsAll ? S.windows : S.windows.slice(0, 6);
    windowsCard.replaceChildren(head("layers", "teal", "Open windows", "Bring one to the front. Needs control on.",
      ui.button({ label: "Refresh windows", icon: "refresh", variant: "ghost", iconOnly: true, onClick: loadWindows })),
      shown.length ? el("div", { class: "ui-list ui-list--inset" }, shown.map((w) => ui.listRow({ icon: "layers", tone: "teal", title: w.title || "(untitled)", meta: "Focus", onClick: () => focusWindow(w) })))
        : ui.emptyState({ icon: "layers", title: "No windows open", compact: true }),
      S.windows.length > 6 ? ui.button({ label: S.windowsAll ? "Show fewer" : `Show all ${S.windows.length}`, variant: "tonal", size: "sm", onClick: () => { S.windowsAll = !S.windowsAll; renderWindows(); } }) : el("span", { hidden: true }));
  }
  async function ensureArmed() {
    let status;
    try { status = await api("/api/screen/status"); } catch (error) { ctx.toast(error.message, { kind: "bad" }); return false; }
    if (!status.available) { ctx.toast(status.reason || "The screen view isn't set up on the laptop.", { kind: "warn" }); return false; }
    if (!status.control_available) { ctx.toast(status.control_reason || "Control needs pyautogui on the laptop.", { kind: "warn" }); return false; }
    if (status.control_armed) return true;
    const minutes = 5;
    const ok = await ui.confirm({ title: "Enable control?", icon: "unlock", confirmLabel: "Enable control",
      message: `For ${minutes} minutes, anyone holding this phone can click, type and press keys on your laptop. It switches off by itself, and you can disarm any time.` });
    if (!ok) return false;
    try { await api("/api/screen/arm", { method: "POST", json: { confirm: true, minutes } }); return true; }
    catch (error) { ctx.toast(error.message, { kind: "bad" }); return false; }
  }
  async function focusWindow(w) {
    if (!(await ensureArmed())) return;
    try {
      await api("/api/screen/windows/focus", { method: "POST", json: { id: w.id } });
      ui.haptic("success");
      goLive();
    } catch (error) {
      ctx.toast(error.status === 404 ? "That window has closed. Refreshing the list." : error.message, { kind: "bad" });
      if (error.status === 404) loadWindows();
    }
  }

  // ---- workspaces link -----------------------------------------------------------------------
  function renderMore() {
    moreCard.replaceChildren(head("layers", "gray", "Workspaces", "Several steps in one tap, like VPN then NiFi.",
      ui.button({ label: "Open", icon: "chevron-right", variant: "tonal", size: "sm", onClick: () => ctx.open("screen", "workspaces") })));
  }

  renderHero(); renderQuick(); renderFolder(); renderLink(); renderMore();

  function show() {
    if (!S.loaded) loadApps(); else loadApps(true);
    loadWindows();
  }
  return { show, refresh: () => { S.apps = null; renderQuick(); loadApps(); loadWindows(); } };
}
