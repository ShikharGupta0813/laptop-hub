// Laptop actions: a generic runner. The server lists actions (id, title, params, risk); this module turns
// one into a form, asks first when it is risky, runs it and shows the result by `kind`.
// Also exports the small helpers shared by apps.js and jarvis.js (icons, spinner, copy to clipboard).
import { api, ApiError, el } from "./api.js";

// ---- shared helpers -----------------------------------------------------------------------
// 24x24 icon paths, one per directory category plus a few UI glyphs.
export const ICONS = {
  assistant: "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V22h2v-3.1A7 7 0 0 0 19 12h-2Z",
  work: "M10 2h4a2 2 0 0 1 2 2v2h4a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4V4a2 2 0 0 1 2-2Zm0 2v2h4V4h-4Zm-6 8v2h16v-2H4Z",
  learn: "M12 3 1 9l11 6 9-4.9V17h2V9L12 3ZM5 13.2v4L12 21l7-3.8v-4L12 17l-7-3.8Z",
  notes: "M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm8 1.5V8h4.5L14 3.5ZM7 12v2h10v-2H7Zm0 4v2h7v-2H7Z",
  laptop: "M20 18c1.1 0 1.99-.9 1.99-2L22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4ZM4 6h16v10H4V6Z",
  system: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15h-2v-6h2v6Zm0-8h-2V7h2v2Z",
  processes: "M3 13h2v-2H3v2Zm0 4h2v-2H3v2Zm0-8h2V7H3v2Zm4 4h14v-2H7v2Zm0 4h14v-2H7v2ZM7 7v2h14V7H7Z",
  launch: "M19 19H5V5h7V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14c1.1 0 2-.9 2-2v-7h-2v7ZM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7Z",
  screen: "M21 2H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h7v2H8v2h8v-2h-2v-2h7c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2Zm0 14H3V4h18v12Z",
  clipboard: "M19 3h-4.18C14.4 1.84 13.3 1 12 1s-2.4.84-2.82 2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2Zm-7 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2Zm2 14H7v-2h7v2Zm3-4H7v-2h10v2Zm0-4H7V7h10v2Z",
  media: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm-2 14.5v-9l6 4.5-6 4.5Z",
  apps: "M4 8h4V4H4v4Zm6 12h4v-4h-4v4Zm-6 0h4v-4H4v4Zm0-6h4v-4H4v4Zm6 0h4v-4h-4v4Zm6-10v4h4V4h-4Zm-6 4h4V4h-4v4Zm6 6h4v-4h-4v4Zm0 6h4v-4h-4v4Z",
  buttons: "M4 7h16a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Zm0 2v6h16V9H4Zm5 2h6v2H9v-2Z",
  jarvis: "M7 2v11h3v9l7-12h-4l4-8H7Z",
  star: "M12 17.3 6.2 21l1.5-6.6L2.5 9.9l6.8-.6L12 3l2.7 6.3 6.8.6-5.2 4.5 1.5 6.6L12 17.3Z",
  back: "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2Z",
  search: "M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5Zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14Z",
  close: "M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12 19 6.41Z",
  refresh: "M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.74 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35Z",
  copy: "M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1Zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2Zm0 16H8V7h11v14Z",
  stop: "M6 6h12v12H6V6Z",
  play: "M8 5v14l11-7L8 5Z",
  send: "M2.01 21 23 12 2.01 3 2 10l15 2-15 2 .01 7Z",
  info: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15h-2v-6h2v6Zm0-8h-2V7h2v2Z",
  warn: "M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z",
  check: "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17Z",
};

const SVG_NS = "http://www.w3.org/2000/svg";
export function icon(name, size = 22) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", "ap-ico");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name] || ICONS.apps);
  svg.append(path);
  return svg;
}

export function spinner(label) {
  return el("span", { class: "ap-spin", role: "img", "aria-label": label || "Working" });
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
        action.risk === "confirm" ? el("span", { class: "ap-badge warn" }, "Asks first") : null),
      action.description ? el("p", { class: "muted ap-desc" }, action.description) : null);
    const form = el("form", { class: "ap-form", novalidate: true, autocomplete: "off" });
    const formError = el("p", { class: "ap-form-error", role: "alert", hidden: true });
    const confirmBox = el("div", { class: "ap-confirm", role: "alertdialog", hidden: true });
    const status = el("div", { class: "ap-status", "aria-live": "polite" });
    const result = el("div", { class: "ap-result", "aria-live": "polite" });
    const runButton = el("button", { type: "submit", class: "btn primary ap-run" }, "Run");

    const needsForm = params.length > 0 || action.risk === "confirm";
    container.append(head);
    if (needsForm) container.append(form);
    container.append(confirmBox, status, result);
    if (opts.returnTo) {
      container.append(el("button", { type: "button", class: "btn ap-linkbtn", onclick: () => open(opts.returnTo) },
        icon("back", 18), `Back to ${opts.returnTo.title}`));
    }

    // -- form fields from params
    for (const param of params) {
      const id = `ap-f-${++fieldCounter}`;
      const label = el("label", { for: id, class: "ap-label" }, param.label || param.name,
        param.required ? el("span", { class: "ap-req", "aria-hidden": "true" }, " *") : null,
        param.required ? el("span", { class: "sr-only" }, " (required)") : null);
      const error = el("p", { class: "ap-field-error", id: `${id}-err`, hidden: true });
      let input;
      let wrapped;
      if (param.type === "bool") {
        input = el("input", { type: "checkbox", id, role: "switch", class: "ap-switch" });
        input.checked = opts.prefill && opts.prefill[param.name] != null ? !!opts.prefill[param.name] : false;
        wrapped = el("div", { class: "ap-field ap-field-bool" },
          el("label", { for: id, class: "ap-label" }, param.label || param.name), input);
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
        wrapped = el("div", { class: "ap-field" }, label, input);
        if (PASTE_FIELD[action.id] === param.name) {
          const note = el("p", { class: "muted ap-hint", "aria-live": "polite" });
          wrapped.append(el("div", { class: "ap-row ap-paste-row" },
            el("button", { type: "button", class: "btn", onclick: () => pasteInto(input, note) },
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

    function showConfirm(values, sentence) {
      result.replaceChildren();
      status.replaceChildren();
      const ask = (ASK[action.id] ? ASK[action.id](values, opts) : `${action.title}?`);
      const titleId = `ap-ask-${++fieldCounter}`;
      confirmBox.setAttribute("aria-labelledby", titleId);
      const cancel = el("button", { type: "button", class: "btn", onclick: () => hideConfirm() }, "Cancel");
      const yes = el("button", { type: "button", class: "btn danger", onclick: () => { hideConfirm(false); send(values, true); } },
        "Yes, do it");
      confirmBox.replaceChildren(...[
        el("h3", { id: titleId }, ask),
        sentence ? el("p", {}, sentence) : null,
        action.description ? el("p", { class: "muted" }, action.description) : null,
        values && Object.keys(values).length ? el("dl", { class: "ap-kvs" }, describeValues(values)) : null,
        el("div", { class: "ap-row ap-confirm-row" }, cancel, yes)].filter(Boolean));
      confirmBox.hidden = false;
      runButton.disabled = true;
      cancel.focus();
    }

    function hideConfirm(refocus = true) {
      confirmBox.hidden = true;
      confirmBox.replaceChildren();
      runButton.disabled = false;
      if (refocus && needsForm) runButton.focus();
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
      return el("button", { type: "button", class: "btn", onclick: () => {
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
          actions.append(el("button", { type: "button", class: "btn", onclick: async () => {
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
          const zoom = el("button", { type: "button", class: "btn", "aria-pressed": "false", onclick: () => {
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
          cells.push(el("td", { class: "ap-act" }, el("button", { type: "button", class: "btn ap-kill",
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
            el("button", { type: "button", class: "btn", onclick: () => {
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
