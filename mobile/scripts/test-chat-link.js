// Tests which addresses "Reply in Google Chat" may open. Run: node scripts/test-chat-link.js
//
// Mirrors android/app/src/main/java/dev/shikhar/laptophub/capture/ChatLink.java and the hub's chatchan.chat_url_ok.
// The first test PARSES the Java file and fails if the package, hosts or limit differ.
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const JAVA = path.join(__dirname, "..", "android", "app", "src", "main", "java", "dev", "shikhar", "laptophub", "capture", "ChatLink.java");
const PKG = "com.google.android.apps.dynamite", HOST_CHAT = "chat.google.com", HOST_MAIL = "mail.google.com", MAIL_PATH = "/chat", MAX_TEXT = 4000;

function allowed(url) {
  if (typeof url !== "string" || url.length > 400) return false;
  let u;
  try { u = new URL(url.trim()); } catch (_) { return false; }
  if (u.protocol !== "https:" || u.username || u.password) return false;
  const host = u.hostname.toLowerCase();
  if (host === HOST_CHAT) return true;
  return host === HOST_MAIL && (u.pathname === MAIL_PATH || u.pathname.startsWith(MAIL_PATH + "/"));
}
const clip = (t) => (t == null ? "" : t.length > MAX_TEXT ? t.slice(0, MAX_TEXT) : t);

let failed = 0;
function test(name, fn) {
  try { fn(); console.log("ok   ", name); } catch (e) { failed++; console.log("FAIL ", name, "-", e.message); }
}

test("Java constants equal this table", () => {
  const src = fs.readFileSync(JAVA, "utf8");
  const str = (n) => { const m = new RegExp(`String ${n} = "([^"]*)";`).exec(src); assert.ok(m, `${n} missing`); return m[1]; };
  const num = (n) => { const m = new RegExp(`${n} = (\\d+);`).exec(src); assert.ok(m, `${n} missing`); return Number(m[1]); };
  assert.strictEqual(str("PKG_CHAT"), PKG);
  assert.strictEqual(str("HOST_CHAT"), HOST_CHAT);
  assert.strictEqual(str("HOST_MAIL"), HOST_MAIL);
  assert.strictEqual(str("MAIL_CHAT_PATH"), MAIL_PATH);
  assert.strictEqual(num("MAX_TEXT"), MAX_TEXT);
});

const CASES = [
  ["https://chat.google.com/room/AAAA", true],
  ["https://chat.google.com/", true],
  ["https://CHAT.GOOGLE.COM/dm/xyz", true],
  ["https://mail.google.com/chat/u/0/#chat/space/AAAA", true],
  ["https://mail.google.com/chat", true],
  ["https://mail.google.com/mail/u/0/", false],
  ["https://mail.google.com/chatter", false],
  ["http://chat.google.com/room/A", false],
  ["https://evil.example.com/chat.google.com", false],
  ["https://chat.google.com.evil.example/room", false],
  ["https://user:pw@chat.google.com/room", false],
  ["intent://chat.google.com/#Intent;scheme=https;end", false],
  ["javascript:alert(1)", false],
  ["", false],
  [null, false],
];
for (const [url, expect] of CASES) test(`allowed(${JSON.stringify(url)}) = ${expect}`, () => assert.strictEqual(allowed(url), expect));
test("clip keeps 4000 characters", () => {
  assert.strictEqual(clip("x".repeat(9000)).length, 4000);
  assert.strictEqual(clip(null), "");
  assert.strictEqual(clip("hi"), "hi");
});

if (process.argv.includes("--dump")) console.log(JSON.stringify(CASES));
console.log(failed ? `\n${failed} FAILED` : `\nall ${CASES.length + 2} passed`);
process.exit(failed ? 1 : 0);

// Java cross-check on a plain JVM (no Android SDK needed):
//   node scripts/test-chat-link.js --dump | tail -1 > cases.json
//   javac -d out android/app/src/main/java/dev/shikhar/laptophub/capture/ChatLink.java <a small main() that replays cases.json
//   through ChatLink.isAllowed and compares to the expected booleans>
