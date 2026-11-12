// Pure text helpers for voice mode (no DOM, no imports, so node can test them): the markdown-to-speech
// cleaner, the streaming sentence splitter, and the leading-name router ("Jarvis, ..." / "Friday, ...").
// The name router mirrors hub/persona.py; keep the two in step.
export const SPEAK_CAP = 700;

// Markdown to plain speakable text, capped at a sentence boundary.
export function speakable(markdown, cap = SPEAK_CAP) {
  let s = String(markdown || "");
  s = s.replace(/```[\s\S]*?(```|$)/g, " (code omitted) ");
  s = s.replace(/`([^`]*)`/g, "$1");
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  s = s.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  s = s.replace(/<[^>]+>/g, " ");
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  s = s.replace(/^\s{0,3}>\s?/gm, "");
  s = s.replace(/^\s*([-*+]|\d+[.)])\s+/gm, "");
  s = s.replace(/^\s*\|?[\s:|-]{3,}\|?\s*$/gm, " ");
  s = s.replace(/\|/g, ", ");
  s = s.replace(/(\*\*|__|\*|_|~~)/g, "");
  s = s.replace(/https?:\/\/\S+/g, "a link");
  s = s.replace(/\s*\n+\s*/g, ". ").replace(/\.\s*\./g, ".").replace(/([:;,])\./g, "$1");
  s = s.replace(/\s+/g, " ").trim();
  if (s.length > cap) {
    const cut = s.slice(0, cap);
    const end = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
    s = (end > cap * 0.4 ? cut.slice(0, end + 1) : cut.replace(/\s+\S*$/, "") + "…") + " The rest is in the chat.";
  }
  return s;
}

// Streaming text -> sentences that can be spoken now. Returns {done: [sentence...], rest}. A sentence is complete
// when . ! ? (or a newline) is followed by whitespace, so "3.5" and "e.g." are not cut. Short fragments are held
// back (minChars) so the voice does not chop "Okay." from the rest, unless `final` flushes everything. Text inside
// an unfinished code fence is never released.
export function splitStream(buffer, { final = false, minChars = 28 } = {}) {
  const text = String(buffer || "");
  const fences = (text.match(/```/g) || []).length;
  if (fences % 2 === 1 && !final) return { done: [], rest: text };
  const done = [];
  let start = 0, last = 0;
  const re = /[.!?]+["')\]]*(?=\s)|\n+/g;
  let m;
  while ((m = re.exec(text))) {
    const end = m.index + m[0].length;
    if (text.slice(start, end).trim().length >= minChars) { done.push(text.slice(start, end).trim()); start = end; }
    last = end;
  }
  void last;
  let rest = text.slice(start);
  if (final) { if (rest.trim()) done.push(rest.trim()); rest = ""; }
  return { done, rest };
}

// "Jarvis, run the tests" -> {persona: "jarvis", text: "run the tests"}. persona is "" when nobody is named.
const NOT_A_NAME = "(?:at|is|was|we|i|the|morning|afternoon|evening|night|next|this|last|after|before|during|off|and)";
export function splitName(input, jarvisName = "Jarvis") {
  const raw = String(input || "");
  const names = [...new Set(["jarvis", String(jarvisName || "Jarvis").trim().toLowerCase(), "friday"].filter(Boolean))]
    .map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const lead = new RegExp(`^\\s*(?:(?:hey|hi|ok|okay|yo)[,\\s]+)?(${names})(?![\\w'\u2019])(?:([\\s]*[,:;.!?\\-\u2014]+\\s*)|(\\s+)(?!${NOT_A_NAME}\\b)|\\s*$)`, "i");
  const m = lead.exec(raw);
  if (m) return { persona: m[1].toLowerCase() === "friday" ? "friday" : "jarvis", text: raw.slice(m[0].length).trim() };
  const tail = new RegExp(`,\\s*(${names})\\s*[.!?]*\\s*$`, "i").exec(raw);
  if (tail) return { persona: tail[1].toLowerCase() === "friday" ? "friday" : "jarvis", text: raw.slice(0, tail.index).trim() };
  return { persona: "", text: raw.trim() };
}
