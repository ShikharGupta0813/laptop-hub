// Settings > "Dictation (laptop)": hold a hotkey on the laptop, speak, let go, and the words are typed into whatever window
// has focus. The browser here only edits settings and shows status; the hotkey, the microphone and the typing all happen on
// the laptop (hub/dictation.py, docs/api/dictation.md). Every string goes in with textContent (el()), never as markup.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";

const CSS = "/css/dictation.css";
const good = (text) => ui.toast(text, { kind: "good" });
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const loadCss = () => { if (!document.querySelector(`link[href="${CSS}"]`)) document.head.append(el("link", { rel: "stylesheet", href: CSS })); };

export const STATE_TEXT = { off: "Off", starting: "Starting", ready: "On", recording: "Listening", transcribing: "Writing it out", typing: "Typing",
  thinking: "Asking the assistant", paused: "Paused", missing_dependency: "Needs a package", no_mic: "No microphone", error: "Problem" };

export async function dictationStatus() { return api("/api/dictation/status"); }

// "ctrl+alt+space" from a keydown event; null for a lone modifier. Only builds the string, the laptop validates it.
export function comboFromEvent(e) {
  const code = e.code || "";
  let key = null;
  if (/^Key[A-Z]$/.test(code)) key = code.slice(3).toLowerCase();
  else if (/^Digit\d$/.test(code)) key = code.slice(5);
  else if (/^F\d{1,2}$/.test(code)) key = code.toLowerCase();
  else if (code === "Space") key = "space";
  else if (/^(Control|Alt|Shift|Meta|OS)/.test(code)) key = null;
  else if (code) key = code.replace(/([a-z])([A-Z])/g, "$1_$2").toLowerCase().slice(0, 12);
  if (!key) return null;
  return [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "cmd", key].filter(Boolean).join("+");
}

// The row for the Settings list: shows "On / Off / Paused" so the state is visible without opening the sheet.
export function dictationRow() {
  loadCss();
  const badge = ui.badge("...", "");
  const row = ui.listRow({ icon: "mic", tone: "violet", title: "Dictation (laptop)", chevron: true, meta: badge,
    sub: "Hold a key on the laptop, speak, and it types into the window in front", onClick: () => openDictation(refresh) });
  async function refresh() {
    try {
      const st = await dictationStatus();
      badge.textContent = !st.enabled ? "Off" : st.paused ? "Paused" : (STATE_TEXT[st.state] || st.state);
      badge.className = "ui-badge" + (st.enabled && !st.paused && st.active ? " ui-badge--good" : st.state === "missing_dependency" || st.state === "error" ? " ui-badge--warn" : "");
    } catch (_) { badge.textContent = ""; }
  }
  refresh();
  return row;
}

export async function openDictation(onChange) {
  loadCss();
  let st;
  try { st = await dictationStatus(); } catch (error) { fail(error); return; }
  const put = async (body, message) => {
    try { st = await api("/api/dictation/settings", { method: "PUT", json: body }); if (message) good(message); paint(); onChange?.(); }
    catch (error) { fail(error); paint(); }
  };
  const toggle = async (body) => {
    try { st = await api("/api/dictation/toggle", { method: "POST", json: body }); paint(); onChange?.(); if (st.message) ui.toast(st.message, { kind: "warn" }); }
    catch (error) { fail(error); paint(); }
  };
  const body = el("div", { class: "ui-stack ui-stack--lg dc" });
  const sheet = ui.sheet({ title: "Dictation (laptop)", class: "st-sheet st-sheet--tall", content: body });

  const switchRow = (title, sub, checked, onChange2) => {
    const sw = ui.switchControl({ checked, label: title, onChange: onChange2 });
    return el("div", { class: "ui-row le-switchfield" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, title),
      sub ? el("span", { class: "ui-row__sub" }, sub) : null), sw);
  };

  function hotkeyField() {
    const input = el("input", { class: "ui-input dc-key", type: "text", maxlength: "40", value: st.settings.hotkey, "aria-label": "Hotkey" });
    input.value = st.settings.hotkey;
    let stop = null;
    const record = ui.button({ label: "Press the keys", icon: "terminal", variant: "tonal", size: "sm", onClick: () => {
      if (stop) { stop(); return; }
      record.querySelector("span:last-child") && (record.querySelector("span:last-child").textContent = "Press the keys now (Esc cancels)");
      const onKey = (e) => {
        e.preventDefault(); e.stopPropagation();
        if (e.key === "Escape") return stop();
        const combo = comboFromEvent(e);
        if (combo) { input.value = combo; stop(); }
      };
      stop = () => { window.removeEventListener("keydown", onKey, true); stop = null; if (record.isConnected) paintRecord(); };
      window.addEventListener("keydown", onKey, true);
    } });
    const paintRecord = () => { const t = record.querySelector("span:last-child"); if (t) t.textContent = "Press the keys"; };
    const save = ui.button({ label: "Save hotkey", icon: "check", variant: "primary", size: "sm", onClick: () => put({ hotkey: input.value }, "Hotkey saved.") });
    return el("section", { class: "ui-card ui-stack" }, ui.eyebrow("01", "Hotkey"),
      el("p", { class: "ui-hint" }, "Click Press the keys, then press the combination on THIS computer's keyboard. This page only fills in the text; the laptop checks it. Use at least one modifier (ctrl, alt) or a function key."),
      input, el("div", { class: "dc-actions" }, record, save));
  }

  function listsSection() {
    const bl = listEditor({ title: "Windows where nothing is ever typed", hint: "A word from the window title or program name. If the window in front contains one of these, dictation does not even record. It cannot tell a password box by itself, so add any app you do not want typed into.",
      items: st.settings.blocklist.map((w) => ({ id: w })), addLabel: "Add window word", resetLabel: "Back to the ready-made list",
      rowOf: (i) => ({ icon: "shield", tone: "bad", title: i.id }),
      onAdd: () => formSheet({ title: "Block a window", fields: [{ key: "w", label: "Word in the window title or program name", type: "text", maxlength: 40, placeholder: "banking" }],
        onSave: async (v) => { if (!v.w.trim()) return false; await put({ blocklist: [...st.settings.blocklist, v.w] }, "Added."); } }),
      onEdit: (i) => formSheet({ title: "Edit", fields: [{ key: "w", label: "Word", type: "text", maxlength: 40 }], values: { w: i.id },
        onSave: async (v) => { await put({ blocklist: st.settings.blocklist.map((x) => (x === i.id ? v.w : x)) }, "Saved."); } }),
      onDelete: async (i) => { await put({ blocklist: st.settings.blocklist.filter((x) => x !== i.id) }, "Removed."); },
      onReset: async () => { await put({ blocklist: st.defaults.blocklist }, "Back to the defaults."); } });
    const sp = listEditor({ title: "Spoken punctuation", hint: "Say the phrase and the symbol is typed instead. New line only presses Enter when you switch that on below; otherwise it becomes a space.",
      items: Object.entries(st.settings.spoken).map(([phrase, symbol]) => ({ id: phrase, phrase, symbol })), addLabel: "Add phrase", resetLabel: "Back to the ready-made list",
      rowOf: (i) => ({ icon: "edit", tone: "accent", title: i.phrase, meta: i.symbol === "\\n" ? "line break" : i.symbol }),
      onAdd: () => form(null), onEdit: (i) => form(i),
      onDelete: async (i) => { const next = { ...st.settings.spoken }; delete next[i.id]; await put({ spoken: next }, "Removed."); },
      onReset: async () => { await put({ spoken: st.defaults.spoken }, "Back to the defaults."); } });
    function form(item) {
      formSheet({ title: item ? "Edit phrase" : "New phrase", values: item || {}, fields: [
        { key: "phrase", label: "When I say", type: "text", maxlength: 30, placeholder: "open bracket" },
        { key: "symbol", label: "Type", type: "text", maxlength: 8, placeholder: "(", hint: "Up to 8 characters. Type \\n for a line break." }],
      onSave: async (v) => { const next = { ...st.settings.spoken }; if (item) delete next[item.id]; next[v.phrase] = v.symbol; await put({ spoken: next }, "Saved."); } });
    }
    return [bl.el, sp.el];
  }

  function commandsSheet() {
    formSheet({ title: "Voice commands", values: { scratch: st.settings.scratch_phrases, prefixes: st.settings.assistant_prefixes, on: st.settings.assistant_commands },
      fields: [
        { key: "scratch", label: "Undo the last dictation", type: "chips", hint: "Say it alone, like 'scratch that'. Backspaces what was typed, only if the window is still the same one." },
        { key: "on", label: "Let me talk to the assistant", type: "switch", default: false, hint: "Say 'send to Jarvis ...' or 'ask Jarvis ...' and the rest goes to the assistant instead of being typed. Replies are spoken. Changes still wait for your tap on the phone." },
        { key: "prefixes", label: "Assistant phrases", type: "chips", hint: "How such a sentence starts." }],
      onSave: async (v) => { await put({ scratch_phrases: v.scratch, assistant_prefixes: v.prefixes, assistant_commands: !!v.on }, "Saved."); } });
  }

  function paint() {
    const s = st.settings;
    const notice = st.state === "missing_dependency" || st.state === "no_mic" || st.state === "error"
      ? ui.notice(st.message || STATE_TEXT[st.state], { tone: "warn", icon: "alert" })
      : ui.notice(!st.enabled ? "Dictation is off." : st.paused ? "Dictation is on but paused: the hotkey does nothing." : `Dictation on. ${STATE_TEXT[st.state] || ""}${st.message ? ": " + st.message : ""}`,
        { tone: st.enabled && !st.paused ? "good" : undefined, icon: "mic" });
    notice.setAttribute("role", "status");
    const num = (key, label, min, max) => {
      const input = el("input", { class: "ui-input", type: "number", min: String(min), max: String(max), step: "1" });
      input.value = s[key];
      input.addEventListener("change", () => put({ [key]: Number(input.value) }, "Saved."));
      return el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input);
    };
    const mode = el("select", { class: "ui-input" }, el("option", { value: "hold" }, "Hold to talk, let go to type"), el("option", { value: "toggle" }, "Press once to start, again to stop"));
    mode.value = s.mode;
    mode.addEventListener("change", () => put({ mode: mode.value }, "Saved."));
    const lang = el("input", { class: "ui-input", type: "text", maxlength: "8", placeholder: "empty = detect (en, de, es...)" });
    lang.value = s.language;
    lang.addEventListener("change", () => put({ language: lang.value }, "Saved."));
    body.replaceChildren(
      notice,
      el("p", { class: "ui-hint" }, "Hold the hotkey on the laptop, say a sentence, let go. The words are typed into WHATEVER WINDOW HAS FOCUS at that moment: an editor, a browser, a chat. It never presses Enter by itself."),
      el("div", { class: "ui-list ui-list--inset ui-list--icons" },
        switchRow("Dictation on", "Needs the optional packages: python -m pip install pynput sounddevice", s.enabled, (on) => toggle({ enabled: on })),
        switchRow("Pause", "Keeps the settings but ignores the hotkey.", s.paused, (on) => toggle({ paused: on }))),
      hotkeyField(),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("02", "How it behaves"),
        el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Hotkey style"), mode),
        el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, "Language"), lang),
        num("max_seconds", "Longest recording (seconds)", 5, 600), num("max_chars", "Most characters typed at once", 50, 10000)),
      el("div", { class: "ui-list ui-list--inset ui-list--icons" },
        switchRow("Spoken punctuation", "Say comma, full stop, question mark and they become symbols.", s.punctuate, (on) => put({ punctuate: on })),
        switchRow("Space after each phrase", "So the next dictation does not stick to the last.", s.add_space, (on) => put({ add_space: on })),
        switchRow("'New line' may press Enter", "Off by default: in a chat box Enter sends the message.", s.newline, (on) => put({ newline: on })),
        switchRow("Paste instead of typing", "Only for apps that miss typed keys. Briefly uses the clipboard and puts the old contents back.", s.use_clipboard, (on) => put({ use_clipboard: on }))),
      ui.listRow({ icon: "jarvis", tone: "accent", title: "Voice commands", sub: "Scratch that, and talking to the assistant", chevron: true, onClick: commandsSheet }),
      ...listsSection(),
      el("section", { class: "ui-card ui-stack" }, ui.eyebrow("PV", "Privacy"),
        el("p", { class: "ui-hint" }, "Your voice is turned into text by Whisper on the laptop. No audio leaves it, none is saved to disk, and neither the audio nor the words are logged. Only if you switch on the assistant command does the sentence after 'send to Jarvis' go to the AI you chose. Windows in the blocklist are never recorded or typed into.")));
  }
  paint();
  return sheet;
}
