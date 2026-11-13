// Wake phrase matching for the hands-free "Hey Jarvis" / "Hey Friday". Pure: no DOM, no storage, so node can test it.
// Conservative on purpose: it needs a greeting word (hey, hi, ok, okay, yo) before the name, so a sentence that merely
// contains the name ("Jarvis is a nice name", "friday evening plans") never wakes the assistant.
// Name rules match hub/persona.py and speechchunk.js splitName: Jarvis, the owner's custom Jarvis name, and Friday.
const JARVIS_HEARD = ["jarvis", "jarves", "jarvas"];   // common mis-hearings of the name; "service" is NOT one of them
const FRIDAY_HEARD = ["friday"];
// A name followed by one of these reads as a normal sentence ("friday evening plans", "jarvis is a nice name").
const NOT_A_NAME = "(?:at|is|was|we|i|the|morning|afternoon|evening|night|next|this|last|after|before|during|off|and)";

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function matchWake(transcript, { jarvisName = "Jarvis" } = {}) {
  const raw = String(transcript || "");
  const custom = String(jarvisName || "").trim().toLowerCase();
  const jarvis = [...new Set([...JARVIS_HEARD, ...(custom && !FRIDAY_HEARD.includes(custom) ? [custom] : [])])];
  const names = [...jarvis, ...FRIDAY_HEARD].map(esc).join("|");
  const re = new RegExp(
    `^\\s*(?:hey|hi|ok|okay|yo)[,.!\\s]+(${names})(?![\\w'’])` +
    `(?:\\s*[,:;.!?\\-—]+\\s*|\\s+(?!${NOT_A_NAME}\\b)|\\s*$)`, "i");
  const m = re.exec(raw);
  if (!m) return null;
  const heard = m[1].toLowerCase();
  return { persona: FRIDAY_HEARD.includes(heard) ? "friday" : "jarvis", rest: raw.slice(m[0].length).trim() };
}
