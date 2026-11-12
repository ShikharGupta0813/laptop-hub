// Laptop actions: a generic runner. The server lists actions (id, title, params, risk); this module turns
// one into a form, asks first when it is risky, runs it and shows the result by `kind`.
// Also exports the small helpers shared by apps.js and jarvis.js (icons, spinner, copy to clipboard).
import { api, ApiError, el } from "./api.js";
import { icon as uiIcon, confirm as uiConfirm, switchControl, toast as uiToast } from "./ui.js";

// ---- shared helpers -----------------------------------------------------------------------
// Directory-category and glyph names used by this module, mapped to the shared icon set in icons.js.
const ICON_MAP = {
  assistant: "mic", work: "work", learn: "learn", notes: "note", laptop: "laptop", system: "info", processes: "activity",
  launch: "external", screen: "monitor", clipboard: "clipboard", media: "volume", apps: "grid", buttons: "zap",
  jarvis: "jarvis", star: "star", back: "arrow-left", search: "search", close: "close", refresh: "refresh", copy: "copy",
  stop: "stop", play: "play", send: "send", info: "info", warn: "alert", check: "check",
};
export function icon(name, size = 20) {
  return uiIcon(ICON_MAP[name] || name, { size, class: "ap-ico" });
}

export function spinner(label) {
  return el("span", { class: "ui-spinner ap-spin", role: "img", "aria-label": label || "Working" });
}

// Copy text with the Clipboard API, then the old execCommand route. Returns true on success.
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) { /* blocked or unavailable: fall through */ }
  try {
    const area = el("textarea", { "aria-hidden": "true", tabindex: "-1", readonly: true,
      style: "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0" });
    area.value = text;
    document.body.append(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    return !!done;
  } catch (_) {
    return false;
  }
}

function selectAll(node) {
  try {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch (_) { /* nothing to select */ }
}

// ---- the runner ---------------------------------------------------------------------------
// The sentence asked before a risky action. Anything not listed falls back to "<title>?".
const ASK = {
  shutdown: () => "Shut down the laptop?",
  restart: () => "Restart the laptop?",
  sleep: () => "Put the laptop to sleep?",
  kill_process: (values, opts) => (opts.label
    ? `End the program "${opts.label}" (process id ${values.pid})?`
    : `End the program with process id ${values.pid}?`),
};

// Actions whose text field gets a "Paste from my phone" helper.
const PASTE_FIELD = { set_clipboard: "text" };

let fieldCounter = 0;

export function createRunner(container, ctx, { getAction } = {}) {
  container.classList.add("ap-runner");
  let runId = 0;
  let abort = null;

  function close() {
    runId += 1;
    if (abort) abort.abort();
    abort = null;
    container.replaceChildren();
  }

  function open(action, opts = {}) {
    close();
    const params = action.params || [];
    const fields = new Map(); // param name -> {param, input, error}

    // -- skeleton
    const heading = el("h2", { class: "ap-title", tabindex: "-1" }, action.title);
    const head = el("div", { class: "ap-runner-head" },
      el("div", { class: "ap-runner-title" },
        heading,
        action.risk === "confirm" ? el("span", { class: "ui-badge ui-badge--warn" }, "Asks first") : null),
      action.description ? el("p", { class: "muted ap-desc" }, action.description) : null);
    const form = el("form", { class: "ap-form", novalidate: true, autocomplete: "off" });
    const formError = el("p", { class: "ap-form-error", role: "alert", hidden: true });
    const status = el("div", { class: "ap-status", "aria-live": "polite" });
    const result = el("div", { class: "ap-result", "aria-live": "polite" });
    const runButton = el("button", { type: "submit", class: "ui-btn ui-btn--primary ap-run" }, "Run");

    const needsForm = params.length > 0 || action.risk === "confirm";
    container.append(head);
    if (needsForm) container.append(form);
    container.append(status, result);
    if (opts.returnTo) {
      container.append(el("button", { type: "button", class: "ui-btn ui-btn--ghost ui-btn--sm ap-linkbtn", onclick: () => open(opts.returnTo) },
        icon("back", 18), `Back to ${opts.returnTo.title}`));
    }

    // -- form fields from params
    for (const param of params) {
      const id = `ap-f-${++fieldCounter}`;
      const label = el("label", { for: id, class: "ui-label ap-label" }, param.label || param.name,
        param.required ? el("span", { class: "ap-req", "aria-hidden": "true" }, " *") : null,
        param.required ? el("span", { class: "sr-only" }, " (required)") : null);
      const error = el("p", { class: "ui-hint ap-field-error", id: `${id}-err`, hidden: true });
      let input;
      let wrapped;
      if (param.type === "bool") {
        const sw = switchControl({ label: param.label || param.name });
        input = sw.input;
        input.id = id;
        input.checked = opts.prefill && opts.prefill[param.name] != null ? !!opts.prefill[param.name] : false;
        wrapped = el("div", { class: "ui-field ap-field ap-field-bool" },
          el("label", { for: id, class: "ui-label ap-label" }, param.label || param.name), input);
      } else {
        if (param.type === "choice") {
          input = el("select", { id });
          const choices = (param.choices || []).map((c) => (typeof c === "object" && c
            ? { value: String(c.value), label: String(c.label ?? c.value) } : { value: String(c), label: String(c) }));
          input.append(el("option", { value: "" }, param.required ? "Choose one…" : "Default"));
          for (const c of choices) input.append(el("option", { value: c.value }, c.label));
        } else if (param.type === "number") {
          input = el("input", { id, type: "number", inputmode: "decimal", step: "any", enterkeyhint: "go" });
        } else if (param.multiline || param.name === "text") {
          input = el("textarea", { id, rows: "3" });
        } else {
          input = el("input", { id, type: "text", autocapitalize: "off", spellcheck: "false", enterkeyhint: "go" });
        }
        if (param.placeholder) input.setAttribute("placeholder", param.placeholder);
        if (opts.prefill && opts.prefill[param.name] != null) input.value = String(opts.prefill[param.name]);
        wrapped = el("div", { class: "ui-field ap-field" }, label, input);
        if (PASTE_FIELD[action.id] === param.name) {
          const note = el("p", { class: "muted ap-hint", "aria-live": "polite" });
          wrapped.append(el("div", { class: "ap-row ap-paste-row" },
            el("button", { type: "button", class: "ui-btn ui-btn--tonal ui-btn--sm", onclick: () => pasteInto(input, note) },
              icon("clipboard", 18), "Paste from my phone"), note));
        }
      }
      if (param.required) input.setAttribute("aria-required", "true");
      input.setAttribute("aria-describedby", error.id);
      input.addEventListener("input", () => { error.hidden = true; input.removeAttribute("aria-invalid"); });
      wrapped.append(error);
      form.append(wrapped);
      fields.set(param.name, { param, input, error });
    }
    form.append(formError);
    if (needsForm) form.append(el("div", { class: "ap-row" }, runButton));
    form.addEventListener("submit", (event) => { event.preventDefault(); submit(); });

    async function pasteInto(input, note) {
      note.textContent = "";
      try {
        const text = await navigator.clipboard.readText();
        input.value = text;
        input.dispatchEvent(new Event("input"));
        note.textContent = text ? "Pasted." : "Your phone's clipboard is empty.";
      } catch (_) {
        note.textContent = "Your browser didn't allow reading the clipboard. Press and hold in the box and choose Paste.";
        input.focus();
      }
    }

    // -- collecting and validating
    function collect() {
      const values = {};
      let first = null;
      for (const { param, input, error } of fields.values()) {
        let value;
        if (param.type === "bool") {
          value = input.checked;
        } else {
          const raw = input.value;
          if (raw.trim() === "") {
            if (param.required) {
              error.textContent = `${param.label || param.name} is required.`;
              error.hidden = false;
              input.setAttribute("aria-invalid", "true");
              first = first || input;
            }
            continue;
          }
          if (param.type === "number") {
            value = Number(raw);
            if (!Number.isFinite(value)) {
              error.textContent = `${param.label || param.name} must be a number.`;
              error.hidden = false;
              input.setAttribute("aria-invalid", "true");
              first = first || input;
              continue;
            }
          } else {
            value = raw.trim();
          }
        }
        values[param.name] = value;
      }
      if (first) { first.focus(); return null; }
      return values;
    }

    function setBusy(busy) {
      runButton.disabled = busy;
      for (const { input } of fields.values()) input.disabled = busy;
      status.replaceChildren();
      if (busy) {
        status.append(el("div", { class: "ap-running" }, spinner("Running"), el("span", {}, "Running…")));
      }
    }

    // -- run / confirm
    function submit() {
      formError.hidden = true;
      const values = collect();
      if (!values) return;
      if (action.risk === "confirm") showConfirm(values);
      else send(values, false);
    }

    function describeValues(values) {
      const items = [];
      for (const { param } of fields.values()) {
        if (!(param.name in values)) continue;
        const value = param.type === "bool" ? (values[param.name] ? "yes" : "no") : String(values[param.name]);
        items.push(el("div", { class: "ap-kv" }, el("dt", {}, param.label || param.name), el("dd", {}, value)));
      }
      return items;
    }

    async function showConfirm(values, sentence) {
      result.replaceChildren();
      status.replaceChildren();
      const ask = (ASK[action.id] ? ASK[action.id](values, opts) : `${action.title}?`);
      const detail = fields.size ? [...fields.values()].filter(({ param }) => param.name in values)
        .map(({ param }) => `${param.label || param.name}: ${param.type === "bool" ? (values[param.name] ? "yes" : "no") : String(values[param.name])}`).join("\n") : "";
      const message = [sentence, action.description, detail].filter(Boolean).join("\n\n");
      runButton.disabled = true;
      const ok = await uiConfirm({ title: ask, message, confirmLabel: "Yes, do it", danger: true });
      runButton.disabled = false;
      if (ok) send(values, true);
      else if (needsForm) runButton.focus();
    }

    async function send(values, confirmed) {
      const id = ++runId;
      if (abort) abort.abort();
      abort = new AbortController();
      result.replaceChildren();
      setBusy(true);
      try {
        const body = { action: action.id, params: values };
        if (confirmed) body.confirm = true;
        if (action.id === "script:update") uiToast("Updating… the app will reconnect in ~20 seconds", { kind: "info" });
        const data = await api("/api/laptop/run", { method: "POST", json: body, signal: abort.signal });
        if (id !== runId) return;
        setBusy(false);
        showResult(data, values);
      } catch (error) {
        if (id !== runId || (error && error.name === "AbortError")) return;
        setBusy(false);
        showError(error, values);
      }
    }

    // -- results
    function runAgainButton(values, label) {
      return el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", onclick: () => {
        if (action.risk === "confirm") showConfirm(values); else send(values, false);
      } }, icon("refresh", 18), label);
    }

    function showResult(data, values) {
      const kind = data.kind || "none";
      const parts = [el("p", { class: "ap-msg" }, icon("check", 18), el("span", {}, data.message || "Done."))];
      const actions = el("div", { class: "ap-row wrap ap-result-actions" });
      let again = "Run again";

      if (kind === "text") {
        const text = String(data.text ?? "");
        const pre = el("pre", { class: "ap-pre", tabindex: "0" }, text || "(empty)");
        const note = el("p", { class: "muted ap-hint", "aria-live": "polite" });
        const isClip = action.id === "get_clipboard";
        parts.push(pre);
        if (text) {
          actions.append(el("button", { type: "button", class: "ui-btn ui-btn--tonal ui-btn--sm", onclick: async () => {
            const ok = await copyText(text);
            if (ok) { note.textContent = isClip ? "Copied to your phone." : "Copied."; }
            else {
              selectAll(pre);
              note.textContent = "Your browser blocked copying. The text is selected: press and hold it, then tap Copy.";
            }
          } }, icon("copy", 18), isClip ? "Copy to my phone" : "Copy"));
        }
        parts.push(actions, note);
      } else if (kind === "image") {
        const mime = /^image\/[\w.+-]+$/.test(data.mime || "") ? data.mime : "image/png";
        const b64 = /^[A-Za-z0-9+/=\s]+$/.test(data.image_b64 || "") ? data.image_b64 : "";
        if (!b64) {
          parts.push(el("p", { class: "ap-form-error" }, "The laptop sent a picture that can't be shown."));
        } else {
          const wrap = el("div", { class: "ap-imgwrap", tabindex: "0", role: "region", "aria-label": "Picture, scroll to pan" });
          const img = el("img", { class: "ap-img", alt: data.message || "Picture from the laptop",
            src: `data:${mime};base64,${b64}` });
          wrap.append(img);
          const zoom = el("button", { type: "button", class: "ui-btn ui-btn--tonal ui-btn--sm", "aria-pressed": "false", onclick: () => {
            const full = wrap.classList.toggle("full");
            zoom.setAttribute("aria-pressed", String(full));
            zoom.lastChild.textContent = full ? "Fit to screen" : "Full size";
          } }, icon("search", 18), el("span", {}, "Full size"));
          actions.append(zoom);
          parts.push(wrap, el("p", { class: "muted ap-hint" }, "Press and hold the picture to save it to your phone."));
        }
        again = "Refresh";
      } else if (kind === "table") {
        parts.push(buildTable(data));
        again = "Refresh";
      }
      actions.append(runAgainButton(values, again));
      if (kind !== "text") parts.push(actions);
      result.replaceChildren(...parts);
    }

    function buildTable(data) {
      const columns = Array.isArray(data.columns) ? data.columns : [];
      const rows = Array.isArray(data.rows) ? data.rows : [];
      const killer = action.id === "list_processes" && getAction ? getAction("kill_process") : null;
      const canKill = !!(killer && killer.available !== false);
      let pidCol = columns.findIndex((c) => String(c).toLowerCase() === "pid");
      if (pidCol < 0) pidCol = 0;
      let nameCol = columns.findIndex((c) => String(c).toLowerCase() === "name");
      if (nameCol < 0) nameCol = 1;
      if (!rows.length) return el("p", { class: "muted" }, "Nothing to show.");
      const head = el("tr", {}, columns.map((c) => el("th", { scope: "col" }, String(c))),
        canKill ? el("th", { scope: "col", class: "ap-act" }, el("span", { class: "sr-only" }, "Actions")) : null);
      const body = rows.map((row) => {
        const cells = row.map((cell) => el("td", { class: typeof cell === "number" ? "num" : "" }, cell == null ? "" : String(cell)));
        if (canKill) {
          const pid = row[pidCol];
          const label = row[nameCol] != null ? String(row[nameCol]) : "";
          cells.push(el("td", { class: "ap-act" }, el("button", { type: "button", class: "ui-btn ui-btn--danger-soft ui-btn--sm ap-kill",
            "aria-label": `End ${label || "program"} (process id ${pid})`,
            onclick: () => open(killer, { prefill: { pid }, ask: true, label, returnTo: action }) }, "Kill")));
        }
        return el("tr", {}, cells);
      });
      return el("div", { class: "ap-tablewrap", tabindex: "0", role: "region", "aria-label": `${action.title} table` },
        el("table", { class: "ap-table" }, el("thead", {}, head), el("tbody", {}, body)));
    }

    function showError(error, values) {
      const status_ = error instanceof ApiError ? error.status : 0;
      const message = error.message || "Something went wrong.";
      result.replaceChildren();
      if (status_ === 409) {
        showConfirm(values, message);
      } else if (status_ === 501) {
        result.append(el("div", { class: "ap-card-info", role: "status" }, icon("info", 20),
          el("div", {}, el("strong", {}, "Not available here"), el("p", {}, message))));
      } else if (status_ === 400 && needsForm) {
        formError.textContent = message;
        formError.hidden = false;
        const first = fields.values().next().value;
        if (first) first.input.focus();
      } else {
        result.append(el("div", { class: "ap-card-error", role: "alert" }, icon("warn", 20),
          el("div", {}, el("strong", {}, status_ === 0 ? "Can't reach the laptop" : "That didn't work"), el("p", {}, message),
            el("button", { type: "button", class: "ui-btn ui-btn--secondary ui-btn--sm", onclick: () => {
              if (action.risk === "confirm") showConfirm(values); else send(values, false);
            } }, icon("refresh", 18), "Retry"))));
      }
    }

    // -- start
    heading.focus({ preventScroll: true });
    if (action.available === false) {
      container.append(el("div", { class: "ap-card-info", role: "status" }, icon("info", 20),
        el("div", {}, el("strong", {}, "Not available on this laptop"), el("p", {}, action.unavailable_reason || "This action can't run here."))));
      form.hidden = true;
    } else if (opts.ask) {
      submit();
    } else if (!needsForm && action.risk !== "confirm") {
      send({}, false);
    }
  }

  return { open, close };
}
