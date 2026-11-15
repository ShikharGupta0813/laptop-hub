// Settings > Voices: which engine speaks, a voice for each assistant (Jarvis and Friday can sound different), the owner's
// own text-to-speech commands, and Piper voice downloads (started only by a tap, with a size warning).
// Routes: docs/api/tts.md and docs/api/assistants.md. Every string goes in with textContent (el() text nodes) only.
import { api, el } from "./api.js";
import * as ui from "./ui.js";
import { listEditor, formSheet } from "./listeditor.js";
import { forgetAssistants, refreshAssistants } from "./assistants.js";
import { forgetVoiceStatus, createAudioPlayer, explainProblem } from "./hubtts.js";
import { SETUP_ALL, setupHint } from "./setuphint.js";

const good = (text) => ui.toast(text, { kind: "good" });
const fail = (error) => ui.toast(error?.message || "Something went wrong.", { kind: "bad" });
const TONE = { cyan: "accent", amber: "orange", green: "good", violet: "violet", white: "gray" };
const ENGINE_LABEL = { "": "Default", browser: "This phone's voice", sapi: "Windows voices", piper: "Piper", command: "My command" };

// Plays the voice the way the app will: the hub's audio when the engine makes some, else this phone's own voice.
const testPlayer = createAudioPlayer();
export async function testVoice({ assistant, engine, voice, rate, pitch, text = "Hello, this is how I sound. Is that voice all right?" }) {
  let response;
  try {
    response = await fetch("/api/tts", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Hub": "1" },
      body: JSON.stringify({ text, assistant: assistant || undefined, engine: engine || undefined, voice: voice ?? undefined, rate, pitch }) });
  } catch (_) { throw new Error("Can't reach your laptop."); }
  if (response.status === 200) {
    const blob = await response.blob();
    testPlayer.play(blob);                      // the shared, tap-unlocked <audio> element (hubtts.js)
    return;
  }
  if (response.status === 204) {
    if (!("speechSynthesis" in window) || !window.speechSynthesis.getVoices().length) {
      const e = new Error("This phone has no voice of its own. Pick Windows voices (SAPI) as the engine, or tap Use Windows voice.");
      e.problem = { reason: "no-voice", detail: e.message };
      throw e;
    }
    const u = new SpeechSynthesisUtterance(text);
    if (rate && rate !== 1) u.rate = rate;
    if (typeof pitch === "number" && pitch !== 1) u.pitch = pitch;
    const found = voice ? window.speechSynthesis.getVoices().find((v) => v.name === voice) : null;
    if (found) u.voice = found;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
    return;
  }
  let body = {};
  try { body = await response.json(); } catch (_) { /* not JSON */ }
  const e = new Error(body.detail || (response.status === 423 ? "Voice is paused (the hub did not say why)." : `The hub answered ${response.status}.`));
  e.problem = { reason: body.reason || (response.status === 423 ? "panic" : "engine"), detail: e.message };
  throw e;
}
// Shows the real reason with its one-tap fix (Resume for Panic, Use Windows voice otherwise).
const testFail = (error) => (error?.problem ? explainProblem(error.problem) : fail(error));

export async function openVoices() {
  let st = await api("/api/tts/status").catch((e) => { fail(e); return null; });
  let list = await api("/api/assistants").catch((e) => { fail(e); return null; });
  if (!st || !list) return;
  let assistants = list.assistants;
  const accents = TONE;
  const voiceCache = {};
  const voicesFor = async (engine) => {
    if (!["sapi", "piper", "command"].includes(engine)) return [];
    if (!voiceCache[engine]) voiceCache[engine] = (await api(`/api/tts/voices?engine=${engine}`).catch(() => ({ voices: [] }))).voices || [];
    return voiceCache[engine];
  };
  const refreshStatus = async () => { st = await api("/api/tts/status"); forgetVoiceStatus(); Object.keys(voiceCache).forEach((k) => delete voiceCache[k]); return st; };
  const put = async (path, json) => { try { st = await api(path, { method: "PUT", json }); forgetVoiceStatus(); return st; } catch (e) { fail(e); throw e; } };

  // ---- the hint card --------------------------------------------------------------------------------------
  const hint = ui.notice("The laptop makes the voice and this phone just plays it, so the phone needs no voice of its own. The laptop tries Piper first, then Windows voices. For a more natural voice, run scripts\\setup-all.ps1 on the laptop (it installs Piper) and download a small voice below. " +
    "Small Piper voices usually run faster than real time on a CPU-only laptop, so the first sentence starts quickly (not measured on your laptop). " +
    "Windows voices need nothing extra. Nothing is downloaded unless you press Download.", { tone: "info" });

  // ---- engine, on/off, headphones -------------------------------------------------------------------------------
  const engineSel = el("select", { class: "ui-input", "aria-label": "Default voice engine" },
    ...st.engines.map((e) => el("option", { value: e.id }, `${e.name}${e.available ? "" : " (not ready)"}`)));
  engineSel.value = st.engine;
  engineSel.addEventListener("change", () => put("/api/tts/settings", { engine: engineSel.value }).then(() => good("Saved.")).catch(() => { engineSel.value = st.engine; }));
  const enabledSw = ui.switchControl({ checked: st.enabled, label: "Use the laptop's voices", onChange: (on) => put("/api/tts/settings", { enabled: on }).catch(() => { enabledSw.input.checked = !on; }) });
  const phonesSw = ui.switchControl({ checked: st.headphones, label: "Headphones mode", onChange: (on) => put("/api/tts/settings", { headphones: on }).catch(() => { phonesSw.input.checked = !on; }) });
  const general = el("section", { class: "st-group" },
    el("h2", { class: "ui-caps st-caps" }, "Voice engine"),
    el("div", { class: "ui-list ui-list--inset" },
      el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Use the laptop's voices"),
        el("span", { class: "ui-row__sub" }, "Off: every assistant speaks with this phone's voice, as before.")), enabledSw),
      el("label", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Default engine"),
        el("span", { class: "ui-row__sub" }, "Used by an assistant that has no engine of its own.")), engineSel),
      el("div", { class: "ui-row" }, el("span", { class: "ui-row__body" }, el("span", { class: "ui-row__title" }, "Headphones mode"),
        el("span", { class: "ui-row__sub" }, "Talking over the assistant stops it more easily, on the phone and the laptop. Phone speakers can still hear the assistant itself and may cut it off by mistake: use headphones or leave this off.")), phonesSw)));

  // ---- per-assistant voices -----------------------------------------------------------------------------------
  function voiceForm(a) {
    const v = a.voice || {};
    const eng = el("select", { class: "ui-input" }, ...Object.entries(ENGINE_LABEL).map(([k, label]) => el("option", { value: k }, label)));
    eng.value = v.engine || "";
    const voiceSel = el("select", { class: "ui-input" });
    const voiceText = el("input", { class: "ui-input", type: "text", maxlength: "80", placeholder: "voice name on this phone (optional)" });
    const rate = el("input", { class: "ui-input", type: "number", min: "0.5", max: "2", step: "0.1" });
    const pitch = el("input", { class: "ui-input", type: "number", min: "0", max: "2", step: "0.1" });
    rate.value = v.rate ?? 1; pitch.value = v.pitch ?? 1; voiceText.value = v.voice || "";
    const hintP = el("p", { class: "ui-hint" });
    const paint = async () => {
      const e = eng.value || st.engine;
      const hub = ["sapi", "piper", "command"].includes(e);
      voiceSel.hidden = !hub; voiceText.hidden = hub;
      pitch.disabled = hub && e !== "command";
      hintP.textContent = hub ? (e === "command" ? "Pitch is handed to your command as REDLINE_TTS_PITCH." : "Pitch is not used by this engine; speed is.") : "";
      if (hub) {
        const vs = await voicesFor(e);
        voiceSel.replaceChildren(el("option", { value: "" }, vs.length ? "First available" : "No voices found yet"), ...vs.map((x) => el("option", { value: x.id }, x.lang ? `${x.name} (${x.lang})` : x.name)));
        voiceSel.value = vs.some((x) => x.id === v.voice) ? v.voice : "";
      }
    };
    eng.addEventListener("change", () => { v.voice = ""; voiceText.value = ""; paint(); });
    paint();
    const read = () => ({ engine: eng.value, voice: ["sapi", "piper", "command"].includes(eng.value || st.engine) ? voiceSel.value : voiceText.value.trim(),
      rate: Number(rate.value) || 1, pitch: Number(pitch.value) || 1 });
    const testBtn = ui.button({ label: "Test voice", icon: "volume", variant: "tonal", onClick: () => testVoice({ assistant: a.id, ...read() }).catch(testFail) });
    const field = (label, input) => el("label", { class: "ui-field" }, el("span", { class: "ui-label" }, label), input);
    return { read, node: el("div", { class: "ui-stack ui-stack--lg" }, field("Engine", eng), field("Voice", el("div", {}, voiceSel, voiceText)),
      field("Speed (0.5 to 2)", rate), field("Pitch (0 to 2)", pitch), hintP, testBtn) };
  }
  const apply = (next) => { assistants = next.assistants; forgetAssistants(); refreshAssistants(true).catch(() => {}); forgetVoiceStatus(); ed.set(assistants); };
  const ed = listEditor({
    title: "Voice for each assistant", items: assistants, empty: "No assistants.",
    hint: "Give Jarvis and Friday different voices so you can tell them apart. Engines that are not ready fall back to this phone's voice.",
    rowOf: (a) => ({ icon: "volume", tone: accents[a.accent] || "accent", title: a.name,
      sub: `${ENGINE_LABEL[a.voice?.engine || ""]}${a.voice?.voice ? " · " + a.voice.voice : ""} · speed ${a.voice?.rate ?? 1}` }),
    onEdit: (a) => {
      const f = voiceForm(a);
      const sh = ui.sheet({ title: `Voice for ${a.name}`, class: "st-sheet st-sheet--tall", content: f.node, actions: [{ label: "Save", variant: "primary", icon: "check", onClick: async () => {
        try { apply(await api(`/api/assistants/${a.id}`, { method: "PATCH", json: { voice: f.read() } })); await refreshStatus(); good("Saved."); } catch (e) { fail(e); return false; }
      } }] });
      void sh;
    },
  });

  // ---- the owner's commands ---------------------------------------------------------------------------------------
  const cmdFields = (isNew) => [
    ...(isNew ? [{ key: "id", label: "Id", type: "text", maxlength: 24, placeholder: "kokoro", hint: "Lower-case letters and digits. It never changes." }] : []),
    { key: "name", label: "Name", type: "text", maxlength: 40 },
    { key: "argv", label: "Command, one argument per line", type: "textarea", rows: 5, maxlength: 1500,
      hint: "The program first, then its arguments. The text to say arrives on stdin. No shell. Use {out} where the program wants the WAV file path." },
    { key: "output", label: "It writes the WAV to", type: "select", options: [["stdout", "Standard output"], ["file", "The file {out}"]] },
    { key: "lang", label: "Language (optional)", type: "text", maxlength: 10, placeholder: "en_US" },
  ];
  const toCmd = (v, old) => ({ id: v.id || old.id, name: v.name, argv: String(v.argv || "").split("\n").map((x) => x.trim()).filter(Boolean), output: v.output, lang: v.lang || "" });
  const saveCmds = async (next) => {
    if (!(await ui.confirm({ title: "Save this command?", message: "The hub will run this program every time the assistant speaks with it. Only use a program you trust.", confirmLabel: "Save", icon: "alert" }))) return false;
    const out = await api("/api/tts/commands", { method: "PUT", json: { commands: next, confirm: true } });
    st.commands = out.commands; cmdEd.set(st.commands); forgetVoiceStatus(); Object.keys(voiceCache).forEach((k) => delete voiceCache[k]);
    return true;
  };
  const cmdEd = listEditor({
    title: "My own voice commands", items: st.commands, empty: "None. This is for any program that turns text from stdin into a WAV (for example a local ONNX voice).",
    hint: "Advanced. Pick \"My command\" as an assistant's engine and choose it as the voice.",
    addLabel: "Add command", rowOf: (c) => ({ icon: "terminal", tone: "violet", title: c.name, sub: c.argv.join(" "), meta: c.id }),
    onAdd: () => formSheet({ title: "New command", fields: cmdFields(true), values: { output: "stdout" }, tall: true,
      onSave: async (v) => { if ((await saveCmds([...st.commands, toCmd(v, {})])) === false) return false; good("Saved."); } }),
    onEdit: (c) => formSheet({ title: `Edit ${c.name}`, fields: cmdFields(false), values: { ...c, argv: c.argv.join("\n") }, tall: true,
      onSave: async (v) => { if ((await saveCmds(st.commands.map((x) => (x.id === c.id ? toCmd(v, c) : x)))) === false) return false; good("Saved."); } }),
    onDelete: async (c) => { await saveCmds(st.commands.filter((x) => x.id !== c.id)); good("Deleted."); },
  });

  // ---- Piper voices ------------------------------------------------------------------------------------------------
  let piper = await api("/api/tts/piper").catch(() => null);
  const piperBox = el("section", { class: "st-group" });
  const jobLine = el("p", { class: "ui-hint st-note", role: "status" });
  let poll = null;
  const paintPiper = () => {
    if (!piper) { piperBox.replaceChildren(); return; }
    const sel = el("select", { class: "ui-input", "aria-label": "Piper voice to download" },
      ...piper.catalog.filter((c) => !c.installed).map((c) => el("option", { value: c.id }, `${c.label} (${c.size_mb} MB)`)));
    const custom = el("input", { class: "ui-input", type: "text", maxlength: "60", placeholder: "or type an id, e.g. en_US-lessac-medium" });
    const dl = ui.button({ label: "Download voice", icon: "download", variant: "tonal", onClick: async () => {
      const id = custom.value.trim() || sel.value;
      if (!id) return;
      if (!(await ui.confirm({ title: "Download this voice?", message: `${id} comes from huggingface.co (rhasspy/piper-voices). It is a download of tens of megabytes, sometimes over 100, stored on the laptop. Continue?`, confirmLabel: "Download", icon: "download" }))) return;
      try { piper = await api("/api/tts/piper/download", { method: "POST", json: { voice: id, confirm: true } }); startPoll(); paintPiper(); } catch (e) { fail(e); }
    } });
    const models = listEditor({ title: "Voices on this laptop", items: piper.models, empty: "None downloaded yet.",
      rowOf: (m) => ({ icon: "volume", tone: "accent", title: m.id, sub: `${(m.bytes / 1048576).toFixed(0)} MB` }),
      onDelete: async (m) => { piper = await api(`/api/tts/piper/${m.id}`, { method: "DELETE" }); Object.keys(voiceCache).forEach((k) => delete voiceCache[k]); paintPiper(); good("Deleted."); } });
    piperBox.replaceChildren(el("h2", { class: "ui-caps st-caps" }, "Piper voices"),
      ui.notice(piper.warning + " " + piper.note + (piper.installed_exe ? "" : " Piper itself is not installed yet: run scripts\\setup-all.ps1 on the laptop (Windows voices work meanwhile)."), { tone: "warn" }),
      models.el, el("div", { class: "ui-stack" }, sel, custom, dl), jobLine);
    const j = piper.job || {};
    jobLine.textContent = j.state === "running" ? `Downloading ${j.voice}… ${j.total ? Math.round((j.bytes / j.total) * 100) + "%" : (j.bytes / 1048576).toFixed(0) + " MB"}`
      : j.state === "error" ? j.message : j.state === "done" ? j.message : "";
  };
  const startPoll = () => {
    clearInterval(poll);
    poll = setInterval(async () => {
      if (!document.body.contains(piperBox)) { clearInterval(poll); return; }
      try { piper = await api("/api/tts/piper"); } catch (_) { return; }
      if (piper.job?.state !== "running") { clearInterval(poll); Object.keys(voiceCache).forEach((k) => delete voiceCache[k]); st = await api("/api/tts/status").catch(() => st); }
      paintPiper();
    }, 1500);
  };
  paintPiper();
  if (piper?.job?.state === "running") startPoll();

  ui.sheet({ title: "Voices", class: "st-sheet st-sheet--tall", content: el("div", { class: "ui-stack ui-stack--lg" }, hint, general, ed.el, piperBox, cmdEd.el,
    ui.notice("The laptop's voices speak in voice mode and the Talk tab. If Piper is missing the laptop uses its Windows voice, then this phone's voice. The panic button silences all of them until you tap Resume.", { tone: "info" })),
  onClose: () => clearInterval(poll) });
}
