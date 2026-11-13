// Skills: community skills (markdown instructions) installed from GitHub. Routes: docs/api/skills.md.
// A skill is untrusted text: it can never change settings or grant permissions, and its scripts are listed, never run.
// Everything shown is text (textContent).
import { api, ApiError, el } from "./api.js";
import * as ui from "./ui.js";

const CSS = "/css/skills.css";
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const kb = (n) => (n ? `${Math.max(1, Math.round(n / 1024))} KB` : "");
const PERSONA = { jarvis: ["Jarvis", "accent"], friday: ["Friday", "warn"], both: ["Jarvis + Friday", ""] };

function personaBadge(persona, hint) {
  const [label, tone] = PERSONA[persona] || PERSONA.both;
  if (persona === "both" && (hint === "jarvis" || hint === "friday")) {
    return ui.badge(`maybe ${PERSONA[hint][0]}`, PERSONA[hint][1]);   // only a guess from the words
  }
  return ui.badge(label, tone);
}

export function init(container) {
  if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS }));
  const root = el("div", { class: "sk ui-stack ui-stack--lg" });
  container.append(root);
  let installed = null, sources = [], source = "", browse = null, tab = "installed", loadError = "", browseError = "", browsing = false;

  async function load({ quiet = false } = {}) {
    if (!installed && !quiet) root.replaceChildren(ui.skeleton("card", 3));
    try {
      const d = await api("/api/skills");
      installed = d.skills || [];
      sources = d.sources || [];
      if (!source) source = sources[0] || "";
      loadError = "";
    } catch (error) { loadError = error instanceof ApiError && error.status === 404 ? "off" : error.message; }
    paint();
  }

  async function loadBrowse(refresh = false) {
    if (!source) return;
    browsing = true; browseError = ""; paint();
    try { browse = (await api(`/api/skills/catalog?source=${encodeURIComponent(source)}${refresh ? "&refresh=true" : ""}`)).skills || []; }
    catch (error) { browse = []; browseError = error.message; }
    browsing = false; paint();
  }

  async function install(src, label) {
    let facts;
    try { facts = await api("/api/skills/preview", { method: "POST", json: { source: src } }); }
    catch (error) { fail(error); return; }
    const message = `${facts.name}: ${facts.description || "no description"}\nFrom ${facts.source}, ${kb(facts.size)}. `
      + `This is an untrusted community skill: it is only text guidance and can never change your settings or grant permissions.`
      + (facts.scripts?.length ? ` It ships ${facts.scripts.length} script file(s); they are never run.` : "");
    if (!await ui.confirm({ title: `Install "${facts.name}"?`, message, confirmLabel: "Install" })) return;
    try {
      await api("/api/skills/install", { method: "POST", json: { source: src, confirm: true } });
      ui.toast(`Installed ${facts.name}.`, { kind: "good" });
      await load({ quiet: true });
      if (tab === "browse") await loadBrowse();
    } catch (error) { fail(error); }
  }

  async function remove(skill) {
    if (!await ui.confirm({ title: `Remove "${skill.name}"?`, message: "Deletes the skill's folder from this laptop.", confirmLabel: "Remove", danger: true })) return;
    try { await api(`/api/skills/${encodeURIComponent(skill.name)}?confirm=true`, { method: "DELETE" }); await load({ quiet: true }); }
    catch (error) { fail(error); }
  }

  function badges(item, extra) {
    return el("div", { class: "sk-head" }, ui.badge("untrusted community skill", "bad"), personaBadge(item.persona, item.persona_hint), ...extra);
  }

  function installedCard(s) {
    const sw = ui.switchControl({ checked: s.enabled, label: `Use ${s.name}`, onChange: async (on) => {
      try { await api(`/api/skills/${encodeURIComponent(s.name)}/toggle`, { method: "POST", json: { enabled: on } }); await load({ quiet: true }); }
      catch (error) { fail(error); await load({ quiet: true }); }
    } });
    const rm = ui.button({ label: "Remove", icon: "trash", variant: "ghost", onClick: () => remove(s) });
    return el("article", { class: "ui-card sk-card ui-stack" },
      badges(s, [el("span", { class: "sk-switch" }, sw)]),
      el("h3", { class: "sk-name" }, s.name),
      s.description ? el("p", { class: "sk-desc" }, s.description) : null,
      el("p", { class: "ui-hint" }, [s.source ? `From ${s.source}` : "", kb(s.size)].filter(Boolean).join(" · ")),
      s.scripts?.length ? el("p", { class: "ui-hint" }, `Scripts inside (never run): ${s.scripts.join(", ")}`) : null,
      el("div", { class: "sk-actions" }, rm));
  }

  function browseCard(c) {
    const btn = c.installed ? ui.badge("installed", "good")
      : ui.button({ label: "Install", icon: "download", variant: "primary", onClick: () => ui.busy(btn, () => install(c.source, c.name)) });
    return el("article", { class: "ui-card sk-card ui-stack" },
      badges(c, []),
      el("h3", { class: "sk-name" }, c.name),
      el("p", { class: "ui-hint" }, c.source),
      el("div", { class: "sk-actions" }, btn));
  }

  function installedTab() {
    if (!installed.length) return ui.emptyState({ icon: "layers", title: "No skills yet", body: "Skills are instruction files from the community. Open Browse to add one, or say: Install the arxiv skill.", action: { label: "Browse", icon: "search", onClick: () => { tab = "browse"; paint(); loadBrowse(); } } });
    return el("div", { class: "ui-stack" }, ...installed.map(installedCard));
  }

  function browseTab() {
    const pick = ui.segmented({ label: "Catalog", options: sources.map((s) => ({ value: s, label: s.split("/").slice(0, 2).join("/") })), value: source, onChange: (v) => { source = v; browse = null; loadBrowse(); } });
    const own = el("input", { class: "sk-own", type: "text", placeholder: "owner/repo/path/to/skill", "aria-label": "Install from GitHub", maxlength: 300 });
    const go = ui.button({ label: "Install", icon: "download", variant: "tonal", onClick: () => ui.busy(go, () => install(own.value.trim())) });
    const list = browsing ? ui.skeleton("card", 2)
      : browseError ? ui.notice(browseError, { tone: "warn" })
      : browse ? (browse.length ? el("div", { class: "ui-stack" }, ...browse.map(browseCard)) : ui.emptyState({ icon: "search", title: "Nothing listed here", compact: true })) : null;
    return el("div", { class: "ui-stack" }, sources.length ? pick : null, list,
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("", "Install from a GitHub link"), own, el("div", { class: "sk-actions" }, go)));
  }

  function paint() {
    if (loadError === "off") { root.replaceChildren(ui.emptyState({ icon: "layers", title: "Skills are off", body: "Turn skills_enabled on in Settings." })); return; }
    if (loadError && !installed) { root.replaceChildren(ui.emptyState({ icon: "alert", title: "Couldn't load skills", body: loadError, action: { label: "Try again", icon: "refresh", onClick: () => load() } })); return; }
    const tabs = ui.segmented({ label: "Skills", options: [{ value: "installed", label: `Installed (${installed.length})` }, { value: "browse", label: "Browse" }], value: tab,
      onChange: (v) => { tab = v; paint(); if (v === "browse" && browse === null) loadBrowse(); } });
    root.replaceChildren(
      ui.notice("Skills are written by other people. They are only text guidance for the assistant: they can never change your settings or grant permissions, and any tool they suggest still asks you first.", { tone: "warn" }),
      tabs, tab === "installed" ? installedTab() : browseTab());
  }

  load();
  return { show() { load({ quiet: true }); } };
}
