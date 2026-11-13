// Bridge to the Android app's background wake word (native plugin "HubWake", see mobile/README.md).
// Does nothing in a normal browser. Inside the app it (1) adds a Settings card "Always-on Hey Jarvis (background)"
// with the model download and the on/off switch, and (2) opens voice mode when the phone heard "Hey Jarvis" /
// "Hey Friday" while the app was closed (native hands over {persona, text}; a #/voice?persona=&text= link works too).
// Audio is processed on the phone and never leaves it; only the words after the wake phrase reach the hub.

const native = () => { try { return window.Capacitor?.Plugins?.HubWake || null; } catch (_) { return null; } };

const MB = (n) => Math.round((Number(n) || 0) / 1048576);

export function mountWakeBridge(ctx) {
  const hw = native();
  if (!hw) return;                                     // not the Android app: nothing to do
  const ui = ctx.ui;

  // ---- incoming wake: open voice mode for the persona, with the words said after the wake phrase -------------
  let busy = false;
  async function openVoice(persona, text) {
    const p = persona === "friday" ? "friday" : "jarvis";
    const t = String(text || "").slice(0, 300);
    try {
      const vm = await import("./voicemode.js");
      vm.setPreload(t, p);
      // Same as the in-browser wake word: Friday with nothing after her name opens in her greeting.
      await ctx.open("assistant", p === "friday" && !t ? "friday" : "voice");
    } catch (error) {
      ctx.toast?.(`Could not open voice mode: ${error.message}`, { kind: "bad" });
    }
  }

  async function consume() {
    if (busy) return;
    busy = true;
    try {
      const r = await hw.consumePending();
      if (r && r.persona) await openVoice(r.persona, r.text);
    } catch (_) { /* nothing pending, or the plugin is busy */ }
    busy = false;
  }

  function fromHash() {
    const m = /^#\/voice\?(.*)$/.exec(location.hash || "");
    if (!m) return;
    const q = new URLSearchParams(m[1]);
    try { history.replaceState(history.state, "", location.pathname + location.search); } catch (_) { /* ignore */ }
    openVoice(q.get("persona"), q.get("text"));
  }

  setTimeout(() => { fromHash(); consume(); }, 500);   // after the first screen is up
  window.addEventListener("hashchange", fromHash);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") consume(); });
  window.addEventListener("focus", consume);
  window.addEventListener("pageshow", consume);

  // ---- Settings card ----------------------------------------------------------------------------------------
  function buildCard() {
    const el = ui.el;
    const sw = ui.switchControl({ checked: false, label: "Always-on Hey Jarvis (background)", onChange: onToggle });
    const state = el("p", { class: "ui-card__sub", role: "status", "aria-live": "polite" }, "Checking...");
    const bar = ui.progressBar(0, "Voice model download");
    bar.hidden = true;
    const dl = ui.button({ label: "Download voice model (about 40 MB)", variant: "tonal", onClick: () => startDownload() });
    dl.hidden = true;
    const card = el("div", { class: "ui-card ui-card--pad-lg ui-stack" },
      el("div", { class: "ui-row", style: "align-items:center;gap:12px" },
        el("span", { class: "grow" }, el("h3", { class: "ui-card__title" }, "Always-on Hey Jarvis (background)")), sw),
      el("p", { class: "ui-card__sub" },
        "Keeps listening on this phone while the screen is off and the app is closed, and opens voice mode when you say "
        + "\"Hey Jarvis\" or \"Hey Friday\". It recognises the phrase on the phone itself (offline): sound is never sent or saved, "
        + "and only the words you say after the phrase reach your hub. Android shows a notification while it listens, "
        + "and it uses some battery. Turn it off any time here or with Stop on that notification."),
      el("p", { class: "ui-card__sub" },
        "If the app does not open by itself when you say it, tap the \"is listening\" notification that appears: some phones only allow that."),
      state, bar, dl);
    const section = el("section", { class: "st-group", id: "st-wakebg" },
      el("h2", { class: "ui-caps st-caps" }, "Background listening"), card);
    return { section, sw, state, bar, dl };
  }

  let parts = null;
  let timer = 0;
  let last = null;

  function paint(s) {
    last = s;
    if (!parts) return;
    const m = s.model || {};
    const st = m.state || "absent";
    const working = st === "downloading" || st === "unpacking";
    parts.bar.hidden = !working;
    if (st === "downloading") parts.bar.set(m.total > 0 ? m.progress : null);
    if (st === "unpacking") parts.bar.set(null);
    parts.dl.hidden = !(st === "absent" || st === "failed");
    parts.dl.textContent = st === "failed" ? "Try the download again" : "Download voice model (about 40 MB)";
    parts.sw.input.disabled = !(st === "ready");
    parts.sw.input.checked = !!s.enabled;
    let text;
    if (st === "downloading") text = m.total > 0 ? `Downloading the voice model: ${MB(m.bytes)} of ${MB(m.total)} MB...` : "Downloading the voice model...";
    else if (st === "unpacking") text = "Unpacking the voice model...";
    else if (st === "failed") text = `Download failed: ${m.error || "unknown error"}. Check the phone's internet and try again.`;
    else if (st === "absent") text = "Needs a one-time download of the voice model (about 40 MB, Wi-Fi recommended). It is stored only on this phone.";
    else if (s.enabled && s.running) text = "On: listening for Hey Jarvis and Hey Friday, even with the screen off.";
    else if (s.enabled && s.error) text = `Stopped: ${s.error}`;
    else if (s.error) text = `Not running: ${s.error}`;
    else text = "Voice model ready. Switch on to start listening.";
    parts.state.textContent = text;
  }

  async function refresh() {
    try { paint(await hw.status()); } catch (_) { /* plugin not ready */ }
  }

  function poll() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (!parts || !document.body.contains(parts.section)) { clearInterval(timer); timer = 0; parts = null; return; }
      refresh();
    }, 1200);
  }

  async function startDownload() {
    try { await hw.downloadModel(); } catch (error) { ctx.toast?.(error.message || "Could not start the download", { kind: "bad" }); }
    refresh();
  }

  async function onToggle(on) {
    try {
      if (on) await hw.enable(); else await hw.disable();
      ctx.toast?.(on ? "Listening for Hey Jarvis in the background" : "Background listening is off");
    } catch (error) {
      ctx.toast?.(error?.message || "Could not change background listening", { kind: "bad" });
    }
    await refresh();
  }

  function mountCard() {
    const anchor = document.getElementById("st-appearance");
    if (!anchor || anchor.nextElementSibling?.id === "st-wakebg") return;
    document.getElementById("st-wakebg")?.remove();
    parts = buildCard();
    anchor.after(parts.section);
    if (last) paint(last);
    refresh();
    poll();
  }

  // The Settings screen builds itself when opened (and again after changes): add the card whenever it is there.
  const observer = new MutationObserver((records) => {
    if (records.some((r) => r.addedNodes.length)) mountCard();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  mountCard();
}
