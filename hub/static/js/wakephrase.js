// Wake phrase matching for the hands-free "Hey Jarvis" / "Hey Friday" (and any assistant the owner adds in Settings).
// Pure: no DOM, no storage, so node can test it.
// Conservative on purpose: it needs a greeting word (hey, hi, ok, okay, yo) before the name, so a sentence that merely
// contains the name ("Jarvis is a nice name", "friday evening plans") never wakes the assistant.
// The names come from the owner's list (GET /api/assistants, see assistants.js): each assistant's name, wake names and
// mis-hearings. Without the list these constants apply: Jarvis, the owner's custom Jarvis name, and Friday.
// Name rules match hub/persona.py and speechchunk.js splitName.
export const JARVIS_HEARD = ["jarvis", "jarves", "jarvas"];   // common mis-hearings of the name; "service" is NOT one of them
export const FRIDAY_HEARD = ["friday"];
// A name followed by one of these reads as a normal sentence ("friday evening plans", "jarvis is a nice name").
const NOT_A_NAME = "(?:at|is|was|we|i|the|morning|afternoon|evening|night|next|this|last|after|before|during|off|and)";

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// word -> assistant id, from an assistants list ([{id, name, wake_names, heard_as, enabled}]).
export function wakeTable(assistants) {
  const table = {};
  for (const a of assistants || []) {
    if (!a || a.enabled === false) continue;
    const first = String(a.name || "").trim().split(/\s+/)[0].toLowerCase();
    for (const w of [...(a.wake_names || []), ...(a.heard_as || []), first]) {
      const word = String(w || "").trim().toLowerCase();
      if (word.length > 1 && !(word in table)) table[word] = a.id;
    }
  }
  return table;
}

function legacyTable(jarvisName) {
  const custom = String(jarvisName || "").trim().toLowerCase();
  const jarvis = [...new Set([...JARVIS_HEARD, ...(custom && !FRIDAY_HEARD.includes(custom) ? [custom] : [])])];
  const table = {};
  for (const w of jarvis) table[w] = "jarvis";
  for (const w of FRIDAY_HEARD) table[w] = "friday";
  return table;
}

export function matchWake(transcript, { jarvisName = "Jarvis", assistants = null } = {}) {
  const raw = String(transcript || "");
  const table = Array.isArray(assistants) && assistants.length ? wakeTable(assistants) : legacyTable(jarvisName);
  const words = Object.keys(table).sort((a, b) => b.length - a.length);
  if (!words.length) return null;
  const names = words.map(esc).join("|");
  const re = new RegExp(
    `^\\s*(?:hey|hi|ok|okay|yo)[,.!\\s]+(${names})(?![\\w'’])` +
    `(?:\\s*[,:;.!?\\-—]+\\s*|\\s+(?!${NOT_A_NAME}\\b)|\\s*$)`, "i");
  const m = re.exec(raw);
  if (!m) return null;
  return { persona: table[m[1].toLowerCase()], rest: raw.slice(m[0].length).trim() };
}
