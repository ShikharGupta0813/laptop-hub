// Pure text helpers for voice mode (no DOM, no imports, so node can test them): the markdown-to-speech
// cleaner, the streaming sentence splitter, and the leading-name router ("Jarvis, ..." / "Friday, ...").
// The name router mirrors hub/persona.py; keep the two in step.
import { wakeTable } from "./wakephrase.js";   // pure too, so node can still run this file
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
// `assistants` is the owner's list (assistants.js): their names and wake words route instead of Jarvis and Friday only.
export function splitName(input, jarvisName = "Jarvis", assistants = null) {
  const raw = String(input || "");
  const table = Array.isArray(assistants) && assistants.length ? wakeTable(assistants) : null;   // word -> assistant id
  const words = table ? Object.keys(table) : [...new Set(["jarvis", String(jarvisName || "Jarvis").trim().toLowerCase(), "friday"].filter(Boolean))];
  if (!words.length) return { persona: "", text: raw.trim() };
  const who = (w) => (table ? table[w.toLowerCase()] : w.toLowerCase() === "friday" ? "friday" : "jarvis");
  const names = words.sort((a, b) => b.length - a.length).map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const lead = new RegExp(`^\\s*(?:(?:hey|hi|ok|okay|yo)[,\\s]+)?(${names})(?![\\w'\u2019])(?:([\\s]*[,:;.!?\\-\u2014]+\\s*)|(\\s+)(?!${NOT_A_NAME}\\b)|\\s*$)`, "i");
  const m = lead.exec(raw);
  if (m) return { persona: who(m[1]), text: raw.slice(m[0].length).trim() };
  const tail = new RegExp(`,\\s*(${names})\\s*[.!?]*\\s*$`, "i").exec(raw);
  if (tail) return { persona: who(tail[1]), text: raw.slice(0, tail.index).trim() };
  return { persona: "", text: raw.trim() };
}

// ---- streaming speech planner (voice mode) ---------------------------------------------------------------------
// Turns the reply as it streams (markdown deltas) into short plain sentences that can be spoken at once. Code blocks,
// tables and the tail of long lists are not read: one "I put the details on screen." is said instead. Links become "a
// link". The text on screen is never touched; this only decides what is said. Total spoken length is capped like
// speakable() so a long answer ends with "The rest is in the chat.".
export const DETAILS_LINE = "I put the details on screen.";
const MARK = "\u0001";
export function createSpeechPlanner({ cap = SPEAK_CAP, listItems = 3 } = {}) {
  let buf = "", chars = 0, noted = false, run = 0;
  const state = { capped: false, any: false };
  // Remove complete code fences (leave a marker). Text from an unfinished fence onwards is held back.
  function scrub(text, final) {
    let out = "", rest = text, held = "";
    for (;;) {
      const i = rest.indexOf("```");
      if (i < 0) { out += rest; break; }
      const j = rest.indexOf("```", i + 3);
      if (j < 0) {
        if (final) { out += rest.slice(0, i) + `\n${MARK}\n`; } else { out += rest.slice(0, i); held = rest.slice(i); }
        break;
      }
      out += rest.slice(0, i) + `\n${MARK}\n`;
      rest = rest.slice(j + 3);
    }
    return { out, held };
  }
  function lines(raw, out) {
    for (const line of raw.split(/\n+/)) {
      let l = line, hadMark = false;
      if (l.includes(MARK)) { hadMark = true; l = l.split(MARK).join(" "); }
      let skip = false;
      if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) { run += 1; if (run > listItems) skip = true; } else if (l.trim()) run = 0;
      if (/^\s*\|.*\|\s*$/.test(l)) skip = true;
      if (skip) hadMark = true;
      const plain = skip ? "" : speakable(l, cap).replace(/ The rest is in the chat\.$/, "");
      if (plain && !state.capped) {
        if (chars + plain.length > cap) { out.push("The rest is in the chat."); state.capped = true; }
        else { chars += plain.length; out.push(plain); state.any = true; }
      }
      if (hadMark && !noted && !state.capped) { noted = true; out.push(DETAILS_LINE); state.any = true; }
    }
  }
  return {
    state,
    feed(chunk, final = false) {
      if (state.capped) { buf = ""; return []; }
      const { out, held } = scrub(buf + String(chunk || ""), final);
      const { done, rest } = splitStream(out, { final });
      buf = rest + held;
      const spoken = [];
      for (const s of done) lines(s, spoken);
      return spoken;
    },
    reset() { buf = ""; chars = 0; noted = false; run = 0; state.capped = false; state.any = false; },
  };
}
