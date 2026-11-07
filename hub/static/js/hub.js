// Hub tab: laptop status, setup check, quick buttons, file upload, install help, log out.
import { api, el } from "./api.js";
import * as ui from "./ui.js";

const REFRESH_MS = 15000;

function gaugeLevel(kind, value) {
  // "used" gauges go bad when high; battery goes bad when low.
  if (kind === "battery") return value <= 15 ? "bad" : value <= 30 ? "warn" : "good";
  return value >= 90 ? "bad" : value >= 75 ? "warn" : "good";
}
const LEVEL_WORD = { good: "OK", warn: "Getting high", bad: "Critical" };
const BATTERY_WORD = { good: "OK", warn: "Low", bad: "Critical" };

function gauge(label, kind, value, text, detail, ringText) {
  const level = gaugeLevel(kind, value);
  const word = (kind === "battery" ? BATTERY_WORD : LEVEL_WORD)[level];
  const pct = Math.max(0, Math.min(100, value));
  const ring = ui.ringProgress({ value: pct / 100, size: 76, stroke: 8, tone: level, big: ringText || `${Math.round(pct)}%`, label });
  ring.setAttribute("role", "meter");
  ring.setAttribute("aria-valuemin", "0");
  ring.setAttribute("aria-valuemax", "100");
  ring.setAttribute("aria-valuenow", String(Math.round(pct)));
  ring.setAttribute("aria-valuetext", `${text}${detail ? ", " + detail : ""}, ${word}`);
  return el("div", { class: `gauge ui-card ui-card--flat ${level}` },
    ring,
    el("span", { class: "g-label" }, label),
    el("span", { class: "g-num ui-num" }, text),
    el("span", { class: `g-state ${level}` }, word),
    detail ? el("span", { class: "g-detail" }, detail) : null);
}

function formatAgo(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  return `${Math.floor(s / 60)} min ago`;
}

export function init(container, ctx) {
  container.classList.add("hub-view");
  let timer = null;
  let tickTimer = null;
  let visible = false;
  let lastOk = 0;
  let inFlight = false;
  let installEvent = null;

  // ---- skeleton ---------------------------------------------------------------------
  const laptopBody = el("div", { class: "hub-body" }, ui.skeleton("card", 1));
  const updated = el("span", { class: "muted hub-updated", "aria-live": "off" }, "");
  const refreshBtn = el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--icon hub-refresh", "aria-label": "Refresh status", onclick: () => refresh(true) }, ui.icon("refresh"));
  const laptopCard = el("section", { class: "ui-card", "aria-labelledby": "hub-h-laptop" },
    el("div", { class: "hub-card-head" },
      el("h2", { class: "ui-card__title", id: "hub-h-laptop" }, "This laptop"),
      refreshBtn,
    ),
    laptopBody,
    el("div", { class: "hub-updated-row" }, updated),
  );

  const setupBody = el("div", { class: "hub-body" });
  const setupCard = el("section", { class: "ui-card", "aria-labelledby": "hub-h-setup" },
    el("h2", { class: "ui-card__title", id: "hub-h-setup" }, "Setup check"), setupBody);

  const scriptsBody = el("div", { class: "hub-body" }, ui.skeleton("tiles", 4));
  const scriptsCard = el("section", { class: "ui-card", "aria-labelledby": "hub-h-scripts" },
    el("h2", { class: "ui-card__title", id: "hub-h-scripts" }, "Quick buttons"), scriptsBody);

  const uploadCard = buildUpload();
  const installCard = buildInstall();

  const versionEl = el("span", { class: "muted" }, "");
  const logoutBtn = el("button", { type: "button", class: "ui-btn ui-btn--danger-soft", onclick: logout }, ui.icon("logout"), "Log out");
  const footer = el("footer", { class: "hub-footer" }, logoutBtn, versionEl);

  container.append(el("div", { class: "hub-stack ui-stack ui-stack--lg" }, laptopCard, scriptsCard, uploadCard, setupCard, installCard, footer));

  // ---- status -----------------------------------------------------------------------
  function renderError(message) {
    laptopBody.replaceChildren(
      el("div", { class: "hub-error", role: "alert" },
        el("strong", {}, "Can't reach your laptop"),
        el("p", {}, message),
        el("button", { type: "button", class: "ui-btn ui-btn--primary ui-btn--sm", onclick: () => refresh(true) }, "Retry"),
      ));
    setupBody.replaceChildren(el("p", { class: "muted" }, "The setup check needs the laptop to answer."));
    updated.textContent = lastOk ? `Last good update ${formatAgo(Date.now() - lastOk)}` : "";
  }

  function renderStatus(s) {
    const items = [
      gauge_battery(s.battery),
      s.cpu_percent == null ? unavailable("CPU") : gauge("CPU", "used", s.cpu_percent, `${Math.round(s.cpu_percent)}%`, ""),
      s.memory ? gauge("Memory", "used", s.memory.percent, `${Math.round(s.memory.percent)}%`, `of ${s.memory.total_gb} GB`) : unavailable("Memory"),
    ];
    if (s.disk && s.disk.total_gb) {
      const used = ((s.disk.total_gb - s.disk.free_gb) / s.disk.total_gb) * 100;
      items.push(gauge("Disk used", "used", used, `${s.disk.free_gb} GB free`, `of ${s.disk.total_gb} GB`));
    }
    laptopBody.replaceChildren(
      el("dl", { class: "hub-facts" },
        el("div", {}, el("dt", {}, "Name"), el("dd", {}, s.hostname || "unknown")),
        el("div", {}, el("dt", {}, "Time"), el("dd", {}, s.time || "unknown")),
        el("div", {}, el("dt", {}, "System"), el("dd", {}, s.platform || "unknown")),
      ),
      el("div", { class: "hub-gauges" }, ...items),
    );
    renderSetup(s);
    tickUpdated();
  }

  function gauge_battery(b) {
    if (!b) return el("div", { class: "gauge ui-card ui-card--flat" },
      ui.tile("battery", "gray", "lg"), el("span", { class: "g-label" }, "Battery"), el("span", { class: "g-num" }, "None"),
      el("span", { class: "g-detail" }, "Mains power only"));
    return gauge("Battery", "battery", b.percent, `${b.percent}%`, b.plugged ? "Plugged in" : "On battery");
  }

  function unavailable(label) {
    return el("div", { class: "gauge ui-card ui-card--flat" },
      ui.tile("cpu", "gray", "lg"), el("span", { class: "g-label" }, label), el("span", { class: "g-num" }, "Not available"),
      el("span", { class: "g-detail" }, "Install psutil on the laptop"));
  }

  function check(ok, title, detail, fix) {
    return el("li", { class: `hub-check ${ok ? "ok" : "no"}` },
      el("span", { class: "hub-mark", "aria-hidden": "true" }, ui.icon(ok ? "check" : "alert", 16)),
      el("div", { class: "grow" },
        el("div", { class: "hub-check-title" }, title,
          el("span", { class: "sr-only" }, ok ? " — ready" : " — needs attention")),
        detail ? el("div", { class: "hub-check-detail" }, detail) : null,
        !ok && fix ? el("div", { class: "hub-fix" }, fix) : null,
      ));
  }

  function renderSetup(s) {
    const models = (s.ollama && s.ollama.models) || [];
    const shells = s.shells || [];
    const list = el("ul", { class: "hub-checks" },
      check(ctx.secure, "Secure (HTTPS) connection",
        ctx.secure ? "Microphone and installing as an app will work." : "Needed for the microphone and for installing as an app.",
        el("span", {}, "Open the ", el("code", {}, "https://…ts.net"), " address shown by ", el("code", {}, "tailscale serve"), ".")),
      check(!!(s.ollama && s.ollama.up), "Local AI running",
        s.ollama && s.ollama.up ? (models.length ? `Models: ${models.join(", ")}` : "Running, but no models are installed yet.") : "Ollama did not answer.",
        "Start the Ollama app on the laptop."),
      check(!!s.voice_ready, "Voice notes ready", s.voice_ready ? "Speech to text is available." : "",
        el("span", {}, "Run ", el("code", {}, "pip install -r requirements-voice.txt"), ".")),
      check(!!s.claude_ready, "Cloud AI ready", s.claude_ready ? "Ready: " + Object.values(s.providers || {}).filter((p) => p.ready && p.label !== "Local model").map((p) => p.label).join(", ") : "",
        el("span", {}, "Run ", el("code", {}, "python -m hub setup --only ai"), " to use Gemini or your Claude login, or use the Local model.")),
      check(shells.length > 0, "Terminal shells available", shells.length ? shells.join(", ") : "",
        "No shell was found. Check the \"shells\" setting in hub.json."),
    );
    setupBody.replaceChildren(list);
  }

  function tickUpdated() {
    if (!lastOk) { updated.textContent = ""; return; }
    updated.textContent = `Updated ${formatAgo(Date.now() - lastOk)}`;
  }

  async function refresh(manual = false) {
    if (inFlight) return;
    inFlight = true;
    if (manual) refreshBtn.disabled = true;
    try {
      const s = await api("/api/status");
      lastOk = Date.now();
      renderStatus(s);
    } catch (error) {
      renderError(error.message || "No answer from the laptop.");
    } finally {
      inFlight = false;
      refreshBtn.disabled = false;
    }
  }

  // ---- quick buttons ------------------------------------------------------------------
  let scriptsLoaded = false;
  async function loadScripts() {
    try {
      const list = await api("/api/scripts");
      scriptsLoaded = true;
      renderScripts(list);
    } catch (error) {
      scriptsLoaded = false;
      scriptsBody.replaceChildren(ui.notice(error.message, { tone: "bad" }),
        el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", onclick: loadScripts }, ui.icon("refresh"), "Retry"));
    }
  }

  function renderScripts(list) {
    if (!list.length) {
      scriptsBody.replaceChildren(
        el("p", { class: "muted" }, "No quick buttons yet. Define them in ", el("code", {}, "hub.json"), " under \"scripts\", for example:"),
        el("pre", { tabindex: "0", "aria-label": "Example hub.json" }, el("code", {},
          '"scripts": {\n  "hello": {\n    "label": "Say hello",\n    "argv": ["echo", "hello"]\n  }\n}')),
      );
      return;
    }
    const grid = el("div", { class: "hub-buttons ui-stagger" });
    const result = el("div", { class: "hub-result", "aria-live": "polite" });
    let running = false;
    const buttons = [];
    const TONES = ["accent", "teal", "violet", "orange", "info", "pink"];

    function setBusy(busy) {
      running = busy;
      for (const b of buttons) b.disabled = busy;
    }
    async function ask(item) {
      const ok = await ui.confirm({ title: `Run '${item.label}'?`, message: "This runs the quick button on the laptop.", confirmLabel: "Run" });
      if (ok) run(item);
    }
    async function run(item) {
      setBusy(true);
      result.replaceChildren(el("div", { class: "hub-running" }, el("span", { class: "ui-spinner", "aria-hidden": "true" }), `Running '${item.label}'…`));
      try {
        const out = await api(`/api/run/${encodeURIComponent(item.name)}`, { method: "POST" });
        const ok = out.exit === 0;
        ui.haptic(ok ? "success" : "error");
        result.replaceChildren(
          el("div", { class: "hub-result-head" },
            el("strong", {}, item.label),
            ui.badge(ok ? "Success" : "Failed", ok ? "good" : "bad"),
            el("span", { class: "muted" }, `exit code ${out.exit}`)),
          el("pre", { class: "hub-output", tabindex: "0", "aria-label": `Output of ${item.label}` }, out.output || "(no output)"),
        );
      } catch (error) {
        result.replaceChildren(ui.notice(`Could not run '${item.label}': ${error.message}`, { tone: "bad" }));
      } finally {
        setBusy(false);
      }
    }
    list.forEach((item, i) => {
      const b = el("button", { type: "button", class: "ui-card ui-card--interactive hub-bigbtn", style: `--i:${i}`,
        onclick: () => { if (!running) ask(item); } },
        ui.tile("zap", TONES[i % TONES.length]), el("span", { class: "hub-bigbtn-label" }, item.label));
      buttons.push(b);
      grid.append(b);
    });
    scriptsBody.replaceChildren(grid, result);
    setTimeout(() => grid.classList.remove("ui-stagger"), 1000);
  }

  // ---- upload -------------------------------------------------------------------------
  function buildUpload() {
    const input = el("input", { type: "file", multiple: true, class: "sr-only", id: "hub-file-input", tabindex: "-1" });
    const pick = el("label", { class: "ui-btn ui-btn--primary ui-btn--lg ui-btn--block hub-pick", for: "hub-file-input" }, ui.icon("upload"), "Choose files");
    const list = el("ul", { class: "hub-uploads", "aria-live": "polite" });
    input.addEventListener("focus", () => pick.classList.add("focus"));
    input.addEventListener("blur", () => pick.classList.remove("focus"));
    input.addEventListener("change", async () => {
      const files = [...input.files];
      input.value = "";
      for (const file of files) await send(file);
    });

    function send(file) {
      const bar = ui.progressBar(0, `Uploading ${file.name}`);
      const status = el("div", { class: "muted hub-up-status" }, "Waiting…");
      const row = el("li", { class: "hub-upload ui-card ui-card--flat" }, ui.tile("file", "accent", "sm"), el("div", { class: "hub-up-body" }, el("div", { class: "hub-up-name ui-truncate" }, file.name), bar, status));
      list.prepend(row);
      return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        const fail = (message) => {
          bar.remove();
          row.classList.add("failed");
          status.className = "hub-up-status bad";
          status.textContent = `Failed: ${message}`;
          resolve();
        };
        xhr.open("POST", "/api/upload");
        xhr.withCredentials = true;
        xhr.setRequestHeader("X-Hub", "1");
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            bar.set(e.loaded / e.total);
            status.textContent = `Sending… ${Math.round((e.loaded / e.total) * 100)}%`;
          }
        };
        xhr.onload = () => {
          let body = null;
          try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
          if (xhr.status === 401) { window.dispatchEvent(new Event("hub:unauthorized")); return fail("Please log in again."); }
          if (xhr.status >= 200 && xhr.status < 300 && body && body.saved_as) {
            bar.remove();
            row.classList.add("done");
            status.className = "hub-up-status good";
            status.textContent = `Saved as ${body.saved_as} (${formatSize(body.size)})`;
            resolve();
          } else {
            fail(body && typeof body.detail === "string" ? body.detail : `The laptop answered with an error (${xhr.status}).`);
          }
        };
        xhr.onerror = () => fail("Can't reach your laptop.");
        xhr.onabort = () => fail("Cancelled.");
        xhr.ontimeout = () => fail("Timed out.");
        const form = new FormData();
        form.append("file", file, file.name);
        status.textContent = "Sending…";
        xhr.send(form);
      });
    }

    return el("section", { class: "ui-card", "aria-labelledby": "hub-h-upload" },
      el("h2", { class: "ui-card__title", id: "hub-h-upload" }, "Send a file to the laptop"),
      el("p", { class: "muted" }, "Files land in the ", el("code", {}, "HubInbox"), " folder on the laptop. A file with a name that is already there is saved under a new name."),
      el("div", { class: "hub-pick-wrap" }, input, pick),
      list,
    );
  }

  function formatSize(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }

  // ---- install ------------------------------------------------------------------------
  function buildInstall() {
    const body = el("div", { class: "hub-body" });
    const card = el("section", { class: "ui-card", "aria-labelledby": "hub-h-install" },
      el("h2", { class: "ui-card__title", id: "hub-h-install" }, "Install this app"), body);
    const standalone = window.matchMedia && window.matchMedia("(display-mode: standalone)").matches;

    function render() {
      if (standalone || window.navigator.standalone) {
        body.replaceChildren(el("p", {}, "This app is installed on this phone."));
        return;
      }
      const parts = [];
      if (installEvent) {
        parts.push(el("button", { type: "button", class: "ui-btn ui-btn--primary", onclick: async () => {
          const ev = installEvent;
          installEvent = null;
          try { await ev.prompt(); await ev.userChoice; } catch (_) { /* dismissed */ }
          render();
        } }, "Install"));
      }
      parts.push(el("p", { class: "muted" }, installEvent
        ? "Or use the Chrome menu: Install app."
        : "In Chrome on Android, open the ⋮ menu and choose Install app (or Add to Home screen)."
          + (ctx.secure ? "" : " This needs the secure https:// address first.")));
      body.replaceChildren(...parts);
    }
    window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvent = e; render(); });
    window.addEventListener("appinstalled", () => { installEvent = null; body.replaceChildren(el("p", {}, "Installed. Open it from your home screen.")); });
    render();
    return card;
  }

  // ---- footer / logout -----------------------------------------------------------------
  async function logout() {
    if (!(await ui.confirm({ title: "Log out?", message: "You will need your token to sign in again.", confirmLabel: "Log out", danger: true }))) return;
    logoutBtn.disabled = true;
    try {
      await api("/api/logout", { method: "POST" });
      window.dispatchEvent(new Event("hub:unauthorized"));
    } catch (error) {
      ctx.toast(`Could not log out: ${error.message}`);
    } finally {
      logoutBtn.disabled = false;
    }
  }

  api("/api/session").then((s) => { versionEl.textContent = s.version ? `Laptop Hub v${s.version}` : ""; })
    .catch(() => { versionEl.textContent = ""; });

  // ---- lifecycle -----------------------------------------------------------------------
  function stop() {
    clearInterval(timer); clearInterval(tickTimer);
    timer = tickTimer = null;
  }
  window.addEventListener("hub:unauthorized", stop);
  return {
    show() {
      visible = true;
      stop();
      refresh();
      if (!scriptsLoaded) loadScripts();
      timer = setInterval(() => { if (visible) refresh(); }, REFRESH_MS);
      tickTimer = setInterval(tickUpdated, 1000);
    },
    hide() {
      visible = false;
      stop();
    },
  };
}
