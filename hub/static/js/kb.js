// Ask my files (Learn > Ask): search the owner's documents (keywords plus meaning) or ask a question and get a streamed
// answer with numbered citations that open the source file. Routes: /api/kb/*.
// File text, snippets and model output are shown with textContent or renderMarkdown (sanitised) only.
import { api, sse, renderMarkdown, el } from "./api.js";
import * as ui from "./ui.js";
import { createLibraryPicker } from "./studylib.js";
import { createProviderSwitch, ensureCss, store, clip } from "./sourcepicker.js";

const EMBED_CMD = "ollama pull nomic-embed-text";

function splitPath(path) {
  const i = String(path).indexOf(":");
  return i < 0 ? { root: "", rel: String(path) } : { root: path.slice(0, i), rel: path.slice(i + 1) };
}
const baseName = (rel) => String(rel).split("/").filter(Boolean).pop() || String(rel);
const num = (n) => Number(n || 0).toLocaleString();

export function init(container, ctx) {
  ensureCss("/css/learn.css");
  ensureCss("/css/kb.css");
  const root = el("div", { class: "kb" });
  container.append(root);

  let mode = store.get("sessionStorage", "hub.kb.mode", "search") === "ask" ? "ask" : "search";
  let status = null;
  let pollTimer = null;
  let askController = null;

  // ------------------------------------------------------------ status card
  const statusHost = el("div", { class: "kb-status" });
  async function loadStatus() {
    try {
      status = await api("/api/kb/status");
      paintStatus();
      if (status.indexing) schedulePoll(); else stopPoll();
    } catch (error) {
      statusHost.replaceChildren(ui.notice(error.message, { tone: "bad" }));
    }
  }
  function schedulePoll() { if (!pollTimer) pollTimer = setInterval(loadStatus, 1500); }
  function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  async function reindex(button) {
    try {
      const run = () => api("/api/kb/reindex", { method: "POST" });
      const res = await (button ? ui.busy(button, run()) : run());
      ctx.toast(res.started ? "Re-scan started" : "A scan is already running", { kind: res.started ? "good" : "info" });
      await loadStatus();
      schedulePoll();
    } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
  }

  function copyCommand() {
    navigator.clipboard?.writeText(EMBED_CMD).then(() => ctx.toast("Command copied", { kind: "good" }), () => ctx.toast("Copy blocked. Select the text instead.", { kind: "warn" }));
  }

  function paintStatus() {
    const st = status;
    if (!st) return;
    const stat = (label, value) => el("div", { class: "kb-stat" }, el("b", { class: "ui-num" }, value), el("span", {}, label));
    const last = st.last_indexed ? ui.relTime(new Date(st.last_indexed)) : "never";
    const pct = st.progress?.total ? st.progress.done / st.progress.total : null;
    const reindexBtn = ui.button({ label: st.indexing ? "Scanning…" : "Re-scan", icon: "refresh", variant: "tonal", size: "sm", disabled: st.indexing, onClick: (e) => reindex(e.currentTarget) });
    if (st.indexing) reindexBtn.classList.add("is-loading");
    const card = el("section", { class: "ui-card kb-statuscard ui-stack", "aria-label": "Index status" },
      el("div", { class: "kb-statushead" }, ui.tile("layers", st.files ? "accent" : "gray"),
        el("div", { class: "grow" }, el("div", { class: "ui-card__title" }, "Your index"),
          el("div", { class: "ui-card__sub" }, st.indexing ? "Reading your files…" : `Last scanned ${last}`)),
        ui.badge(st.mode === "hybrid" ? "Hybrid search" : "Keywords only", st.mode === "hybrid" ? "good" : "warn"), reindexBtn),
      st.indexing ? ui.progressBar(pct, "Indexing progress") : null,
      el("div", { class: "kb-stats" }, stat("files", num(st.files)), stat("chunks", num(st.chunks)), stat("embeddings", num(st.embedded))),
      st.roots?.length ? el("div", { class: "kb-roots" }, st.roots.map((r) => el("span", { class: "ui-chip ui-chip--static" }, ui.icon("folder"), r))) : null,
      st.last_error ? ui.notice(st.last_error, { tone: "bad" }) : null,
      st.skipped ? el("p", { class: "kb-small" }, `${num(st.skipped)} files were skipped (too large, hidden or private).`) : null);
    const parts = [card];
    if (!st.embed_available) {
      parts.push(el("section", { class: "ui-card kb-embed ui-stack" },
        el("div", { class: "kb-embedhead" }, ui.tile("sparkles", "violet", "sm"), el("div", {}, el("div", { class: "ui-card__title" }, "Add search by meaning"),
          el("div", { class: "ui-card__sub" }, "Right now search matches words. With an embedding model it also finds related ideas."))),
        el("div", { class: "kb-cmd" }, el("code", {}, EMBED_CMD), ui.button({ icon: "copy", variant: "ghost", iconOnly: true, label: "Copy command", onClick: copyCommand })),
        el("p", { class: "kb-small" }, `Run it on the laptop (Ollama must be running, model: ${st.embed_model || "nomic-embed-text"}), then tap Re-scan.${st.embed_note ? ` ${st.embed_note}` : ""}`)));
    }
    statusHost.replaceChildren(...parts);
    paintBody();
  }

  // ------------------------------------------------------------ mode switch, provider
  const seg = ui.segmented({ options: [{ value: "search", label: "Search", icon: "search" }, { value: "ask", label: "Ask", icon: "sparkles" }],
    value: mode, label: "Ask my files mode", block: true, onChange(v) { mode = v; store.set("sessionStorage", "hub.kb.mode", v); paintBody(); } });

  // Ask can answer from the shared folders (default) or from chosen documents of the study library.
  let scope = store.get("sessionStorage", "hub.kb.scope", "files") === "library" ? "library" : "files";
  const libPick = createLibraryPicker(ctx, { onChange() {} });
  const scopeSeg = ui.segmented({ options: [{ value: "files", label: "Shared folders" }, { value: "library", label: "Study library" }],
    value: scope, label: "Answer from", block: true, onChange(v) { scope = v; store.set("sessionStorage", "hub.kb.scope", v); paintBody(); } });
  const body = el("div", { class: "kb-body ui-stack ui-stack--lg" });
  root.append(seg, body, statusHost);

  // ------------------------------------------------------------ search
  const searchInput = el("input", { type: "search", placeholder: "Search your documents", "aria-label": "Search your files", enterkeyhint: "search", autocomplete: "off", maxlength: "500" });
  const searchResults = el("div", { class: "kb-results ui-stack" });
  let searchTimer = null;
  let searchSeq = 0;
  searchInput.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 320); });
  searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(searchTimer); runSearch(); } });

  function emptyIndex() {
    return status && !status.files && !status.indexing;
  }

  async function runSearch() {
    const q = searchInput.value.trim();
    const mine = ++searchSeq;
    if (!q) { paintSearchIdle(); return; }
    searchResults.replaceChildren(ui.skeleton("list", 3));
    try {
      const data = await api(`/api/kb/search?${new URLSearchParams({ q, limit: "12" })}`);
      if (mine !== searchSeq) return;
      const hits = data.hits || [];
      if (!hits.length) {
        searchResults.replaceChildren(ui.emptyState({ icon: "search", compact: true, title: "Nothing found", body: status?.mode === "keywords" ? "Search matches words right now. Try a different word, or add the embedding model for related ideas." : "Try different words, or ask a question instead.",
          action: { label: "Ask instead", icon: "sparkles", onClick: () => { askInput.value = q; seg.value = "ask"; mode = "ask"; paintBody(); } } }));
        return;
      }
      searchResults.replaceChildren(el("div", { class: "kb-count" }, `${hits.length} ${hits.length === 1 ? "result" : "results"}`),
        ...hits.map(hitCard));
    } catch (error) {
      if (mine === searchSeq) searchResults.replaceChildren(ui.notice(error.message, { tone: "bad" }), ui.button({ label: "Try again", icon: "refresh", variant: "tonal", size: "sm", onClick: runSearch }));
    }
  }
  function paintSearchIdle() {
    searchResults.replaceChildren(ui.emptyState({ icon: "search", compact: true, title: "Search all your documents", body: "Type a few words. Results come from the folders you share, ranked by how well they match." }));
  }

  function openHit(rootName, rel) {
    if (rootName === "library") { ctx.toast("This came from your study library.", { kind: "info" }); return; }
    try { ctx.openFile(rootName, rel); } catch (error) { ctx.toast(error.message, { kind: "bad" }); }
  }
  function hitCard(h) {
    const { root: r, rel } = h.root && h.rel ? { root: h.root, rel: h.rel } : splitPath(h.path);
    const where = [h.heading, h.line ? `line ${h.line}` : ""].filter(Boolean).join(" · ");
    const score = el("span", { class: "kb-score", role: "img", "aria-label": `Match ${Math.round((h.score || 0) * 100)} percent` }, el("i"));
    score.firstChild.style.setProperty("--v", String(Math.max(0.05, Math.min(1, h.score || 0))));
    return el("button", { type: "button", class: "ui-card ui-card--interactive kb-hit", onclick: () => openHit(r, rel) },
      el("div", { class: "kb-hit-head" }, ui.tile("file", "info", "sm"), el("div", { class: "kb-hit-title" }, el("strong", { class: "ui-truncate" }, baseName(rel)),
        el("span", { class: "kb-path ui-truncate" }, `${r} / ${rel}`)), score),
      where ? el("div", { class: "kb-where" }, where) : null,
      el("p", { class: "kb-snippet ui-clamp-2" }, String(h.snippet || "").replace(/\s+/g, " ")));
  }

  // ------------------------------------------------------------ ask
  const askInput = el("textarea", { rows: "2", maxlength: "2000", placeholder: "Ask a question about your files", "aria-label": "Your question", enterkeyhint: "send" });
  askInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && (e.ctrlKey || e.metaKey || window.matchMedia("(pointer: fine)").matches)) { e.preventDefault(); ask(); }
  });
  const providerHost = el("div", { class: "kb-provider" });
  const provider = createProviderSwitch(providerHost, "Model for answers");
  const askBtn = ui.button({ label: "Ask", icon: "send", variant: "primary", onClick: () => ask() });
  const stopBtn = ui.button({ label: "Stop", icon: "stop", variant: "danger-soft", hidden: true, onClick: () => askController?.abort() });
  const askOut = el("div", { class: "kb-answer-host ui-stack" });
  const askEmpty = ui.emptyState({ icon: "sparkles", compact: true, title: "Ask in your own words",
    body: "The answer comes only from your files, with numbered sources you can open." });

  const cite = (n, src) => {
    const b = el("button", { type: "button", class: "kb-cite", "aria-label": `Source ${n}: ${src ? baseName(src.rel) : ""}`, onclick: () => src && openHit(src.root, src.rel) }, String(n));
    return b;
  };
  // Turn "[1]" in rendered markdown into tappable citation chips (DOM nodes, never HTML strings).
  function linkCitations(node, sources) {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, { acceptNode: (t) => (t.parentElement?.closest("code, pre, a, button") ? NodeFilter.FILTER_REJECT : /\[\d+\]/.test(t.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT) });
    const targets = [];
    while (walker.nextNode()) targets.push(walker.currentNode);
    for (const t of targets) {
      const frag = document.createDocumentFragment();
      let last = 0;
      for (const m of t.nodeValue.matchAll(/\[(\d+)\]/g)) {
        frag.append(document.createTextNode(t.nodeValue.slice(last, m.index)));
        const n = Number(m[1]);
        frag.append(cite(n, sources.find((s) => s.n === n)));
        last = m.index + m[0].length;
      }
      frag.append(document.createTextNode(t.nodeValue.slice(last)));
      t.replaceWith(frag);
    }
  }

  async function ask() {
    const question = askInput.value.trim();
    if (!question || askController) return;
    const libIds = scope === "library" ? libPick.getIds() : [];
    if (scope === "library" && !libIds.length) { ctx.toast("Tick at least one document from your study library.", { kind: "warn" }); return; }
    if (scope === "files" && emptyIndex()) { ctx.toast("Nothing is indexed yet. Tap Re-scan first.", { kind: "warn" }); return; }
    askController = new AbortController();
    const signal = askController.signal;
    askBtn.hidden = true; stopBtn.hidden = false; askEmpty.hidden = true;
    const answerBody = el("div", { class: "kb-answer markdown", "aria-live": "polite" }, el("span", { class: "sc-dots", role: "status", "aria-label": "Searching your files" }, el("i"), el("i"), el("i")));
    const sourcesHost = el("div", { class: "kb-sources ui-stack" });
    askOut.replaceChildren(el("div", { class: "kb-q" }, ui.icon("sparkles"), el("span", {}, question)),
      el("section", { class: "ui-card kb-answercard ui-stack" }, answerBody), sourcesHost);
    let sources = [];
    let text = "";
    let failure = "";
    let timer = null;
    const paint = () => {
      timer = null;
      answerBody.innerHTML = renderMarkdown(text); // sanitised by DOMPurify
      linkCitations(answerBody, sources);
    };
    try {
      for await (const ev of sse("/api/kb/ask", { question, model: provider.get(), limit: 6, ...(scope === "library" ? { library: libIds } : {}) }, signal)) {
        if (ev.type === "sources") {
          sources = ev.sources || [];
          paintSources(sourcesHost, sources);
        } else if (ev.type === "token" || typeof ev.t === "string") {
          text += ev.t ?? ev.text ?? "";
          if (!timer) timer = setTimeout(paint, 100);
        } else if (ev.type === "error" || ev.error) {
          failure = String(ev.error || "The model had a problem.");
        }
      }
    } catch (error) { failure = error.message || "Something went wrong."; }
    clearTimeout(timer);
    if (text) paint(); else answerBody.replaceChildren();
    if (signal.aborted && text) answerBody.append(el("p", { class: "kb-small" }, "Stopped."));
    if (failure) answerBody.parentElement.append(ui.notice(failure, { tone: "bad" }));
    else if (!text) answerBody.replaceChildren(el("p", { class: "kb-small" }, signal.aborted ? "Stopped before an answer arrived." : "The model sent no answer. Try again."));
    askController = null;
    askBtn.hidden = false; stopBtn.hidden = true;
  }

  function paintSources(host, sources) {
    if (!sources.length) { host.replaceChildren(); return; }
    host.replaceChildren(el("h3", { class: "ui-section-title" }, "Sources"), el("div", { class: "ui-list ui-list--inset" }, sources.map((s) =>
      ui.listRow({ lead: el("span", { class: "kb-num ui-num", "aria-hidden": "true" }, String(s.n)), title: baseName(s.rel || s.path),
        sub: el("span", { class: "kb-srcsub" }, [s.heading, clip(String(s.snippet || ""), 90)].filter(Boolean).join(" · ")), chevron: true, onClick: () => openHit(s.root, s.rel) }))));
  }

  // ------------------------------------------------------------ body paint
  function paintBody() {
    if (status && !status.files && !status.indexing && !(mode === "ask" && scope === "library")) {
      body.replaceChildren(ui.button({ label: "Use my study library instead", icon: "book-open", variant: "tonal", onClick: () => { mode = "ask"; scope = "library"; scopeSeg.value = "library"; seg.value = "ask"; paintBody(); } }), ui.emptyState({ icon: "book-open", title: "Nothing indexed yet",
        body: "Ask my files reads the folders you share on the laptop (notes, PDFs, Word and text files) and builds a private index on that laptop. Tap Re-scan to start.",
        action: { label: "Re-scan now", icon: "refresh", onClick: () => reindex(null) } }));
      return;
    }
    if (mode === "search") {
      body.replaceChildren(el("div", { class: "ui-input-wrap" }, ui.icon("search"), searchInput), searchResults);
      if (!searchInput.value.trim()) paintSearchIdle();
    } else {
      body.replaceChildren(scopeSeg, ...(scope === "library" ? [libPick.root] : []), el("section", { class: "ui-card kb-askcard ui-stack" }, askInput, el("div", { class: "kb-askrow" }, providerHost, el("div", { class: "kb-askbtns" }, stopBtn, askBtn))),
        askOut, askEmpty);
      askEmpty.hidden = askOut.childNodes.length > 0;
    }
  }

  loadStatus();
  if (!status) body.replaceChildren(ui.skeleton("card", 1), ui.skeleton("list", 3));

  return {
    show() { provider.refresh(); loadStatus(); },
    hide() { stopPoll(); },
    refresh() { loadStatus(); },
    openSection(name) { if (name === "ask" || name === "search") { mode = name; seg.value = name; paintBody(); } },
  };
}
