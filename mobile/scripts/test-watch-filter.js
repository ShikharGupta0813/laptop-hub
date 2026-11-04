// Tests the Chat / Gmail watch routing. Run: node scripts/test-watch-filter.js
//
// Mirrors android/app/src/main/java/dev/shikhar/laptophub/capture/WatchFilter.java. The first test PARSES that
// Java file and fails if the package names or limits differ. `--dump` prints the scenarios as JSON; the real Java
// class is cross-checked on a plain JVM (see the end of this file for the command).
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const JAVA = path.join(__dirname, "..", "android", "app", "src", "main", "java", "dev", "shikhar", "laptophub", "capture", "WatchFilter.java");

const PKG_CHAT = "com.google.android.apps.dynamite", PKG_MAIL = "com.google.android.gm";
const MAX_TITLE = 200, MAX_TEXT = 1000, WINDOW = 120000;
const OWN = "dev.shikhar.laptophub";

const sourceFor = (pkg) => (pkg === PKG_CHAT ? "chat" : pkg === PKG_MAIL ? "mail" : null);
function route(pkg, own, summary, ongoing, mailOn, chatOn) {
  const s = sourceFor(pkg);
  if (!s) return null;
  if (pkg === own) return null;
  if (summary || ongoing) return null;
  if (s === "mail" && !mailOn) return null;
  if (s === "chat" && !chatOn) return null;
  return s;
}
const cap = (s, n) => ((s || "").trim().length > n ? (s || "").trim().slice(0, n) : (s || "").trim());
function composeText(source, text, big) {
  const t = (text || "").trim(), b = (big || "").trim();
  if (!b) return t;
  if (!t) return b;
  if (source === "mail") return b.includes(t) ? b : t.includes(b) ? t : `${t} - ${b}`;
  return b.length > t.length ? b : t;
}
let seen = new Map();
const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
function firstSeen(pkg, title, text, now) {
  for (const [k, v] of seen) if (Math.abs(now - v) >= WINDOW) seen.delete(k);
  const key = `${pkg}|${norm(title)}|${norm(text)}`;
  if (seen.has(key)) return false;
  seen.set(key, now);
  return true;
}

// Each scenario: { name, pkg, summary, ongoing, mailOn, chatOn, title, text, big, t, expect }
// expect = null (dropped) or the source it is forwarded as. Dedupe state is shared in order, `t` is ms.
const base = { pkg: PKG_CHAT, summary: false, ongoing: false, mailOn: true, chatOn: true, title: "Team space", text: "hello", big: "", t: 1000000 };
const SCEN = [
  { name: "chat message, chat on", expect: "chat" },
  { name: "chat message, chat flag off", chatOn: false, text: "off-1", expect: null },
  { name: "chat message, only mail on", chatOn: false, mailOn: true, text: "off-2", expect: null },
  { name: "gmail message, mail on", pkg: PKG_MAIL, title: "Alice <alice@x.com>", text: "Invoice due", big: "Please pay by Friday", expect: "mail" },
  { name: "gmail message, mail flag off", pkg: PKG_MAIL, mailOn: false, title: "Bob", text: "Hi", expect: null },
  { name: "gmail message, only chat on", pkg: PKG_MAIL, mailOn: false, chatOn: true, title: "Bob", text: "Hi 2", expect: null },
  { name: "both flags off", mailOn: false, chatOn: false, text: "off-3", expect: null },
  { name: "chat group summary skipped", summary: true, text: "2 new messages", expect: null },
  { name: "gmail group summary skipped", pkg: PKG_MAIL, summary: true, title: "Gmail", text: "5 new messages", expect: null },
  { name: "ongoing chat notification skipped", ongoing: true, text: "Chat is running", expect: null },
  { name: "own app notification skipped", pkg: OWN, text: "own", expect: null },
  { name: "Slack is not watched", pkg: "com.Slack", text: "slack msg", expect: null },
  { name: "WhatsApp is not watched", pkg: "com.whatsapp", text: "wa msg", expect: null },
  { name: "payment app is not a watch package", pkg: "com.phonepe.app", text: "Paid Rs 5", expect: null },
  { name: "dedupe: same chat message 30s later", text: "dup-A", t: 2000000, expect: "chat" },
  { name: "dedupe: repeat is dropped", text: "dup-A", t: 2030000, expect: "dedupe" },
  { name: "dedupe: whitespace/case differences still match", text: "  DUP-a ", t: 2060000, expect: "dedupe" },
  { name: "dedupe: same text from the other app is separate", pkg: PKG_MAIL, text: "dup-A", t: 2070000, expect: "mail" },
  { name: "dedupe: after 2 minutes it is forwarded again", text: "dup-A", t: 2200000, expect: "chat" },
  { name: "dedupe: a dropped (flag off) message does not poison dedupe", chatOn: false, text: "dup-B", t: 3000000, expect: null },
  { name: "dedupe: ...so it is forwarded once the flag is on", text: "dup-B", t: 3001000, expect: "chat" },
];

// Compose / trim cases: [source, text, big, expectedText]
const COMPOSE = [
  ["mail", "Invoice due", "Please pay by Friday", "Invoice due - Please pay by Friday"],
  ["mail", "Invoice due", "Invoice due - Please pay by Friday", "Invoice due - Please pay by Friday"],
  ["mail", "Invoice due", "", "Invoice due"],
  ["chat", "short", "a much longer expanded message", "a much longer expanded message"],
  ["chat", "only text", "", "only text"],
];

function run(sc) {
  const s = { ...base, ...sc };
  const r = route(s.pkg, OWN, s.summary, s.ongoing, s.mailOn, s.chatOn);
  if (!r) return null;
  return firstSeen(s.pkg, s.title, s.text, s.t) ? r : "dedupe";
}

if (process.argv.includes("--dump")) {
  console.log(JSON.stringify({
    own: OWN,
    scenarios: SCEN.map((s) => ({ ...base, ...s })),
    compose: COMPOSE,
    trim: { title: MAX_TITLE, text: MAX_TEXT },
  }));
  process.exit(0);
}

let failed = 0;
function test(name, fn) {
  try { fn(); console.log("ok   ", name); } catch (e) { failed++; console.log("FAIL ", name, "-", e.message); }
}

test("Java constants equal this table", () => {
  const src = fs.readFileSync(JAVA, "utf8");
  const str = (n) => { const m = new RegExp(`String ${n} = "([^"]*)";`).exec(src); assert.ok(m, `${n} missing`); return m[1]; };
  const num = (n) => { const m = new RegExp(`${n} = (\\d+)L?;`).exec(src); assert.ok(m, `${n} missing`); return Number(m[1]); };
  assert.strictEqual(str("PKG_CHAT"), PKG_CHAT);
  assert.strictEqual(str("PKG_MAIL"), PKG_MAIL);
  assert.strictEqual(num("MAX_TITLE"), MAX_TITLE);
  assert.strictEqual(num("MAX_TEXT"), MAX_TEXT);
  assert.strictEqual(num("DEDUPE_WINDOW_MS"), WINDOW);
});

for (const sc of SCEN) {
  test(sc.name, () => {
    const got = run(sc);
    assert.strictEqual(got, sc.expect);
  });
}
for (const [src, t, b, exp] of COMPOSE) {
  test(`compose ${src}: ${t} + ${b}`.slice(0, 80), () => assert.strictEqual(composeText(src, t, b), exp));
}
test("trim: title 200, text 1000", () => {
  assert.strictEqual(cap("x".repeat(500), MAX_TITLE).length, 200);
  assert.strictEqual(cap("  hi  ", MAX_TITLE), "hi");
  assert.strictEqual(cap("y".repeat(5000), MAX_TEXT).length, 1000);
});

console.log(failed ? `\n${failed} FAILED` : `\nall ${SCEN.length + COMPOSE.length + 2} passed`);
process.exit(failed ? 1 : 0);

// Java cross-check (no Android SDK needed), as for the payment filter:
//   node scripts/test-watch-filter.js --dump > dump.json
//   javac -d out android/app/src/main/java/dev/shikhar/laptophub/capture/WatchFilter.java <a small main() that
//   replays dump.json through WatchFilter.route / firstSeen / composeText / cap and compares to `expect`>
