// Remote screen keyboard + explain flow at 390px against a FAKE hub (no laptop, no AI).
// Run: node tests/screen_keyboard_check.js   (needs the `playwright` package and a Chromium)
const http = require("http");
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const STATIC = path.join(__dirname, "..", "hub", "static");
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==", "base64");
const MIME = { ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html", ".json": "application/json" };
const calls = [];
const json = (res, code, obj) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(obj)); };
const PAGE = `<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><link rel=stylesheet href=/css/design.css><link rel=stylesheet href=/style.css><link rel=stylesheet href=/css/screen.css>
<main><section id="view-screen"><div id="root"></div></section></main>
<script type=module>import { init } from "/js/screen.js"; const ctx = { toast: (m) => { (window.toasts ||= []).push(m); } };
window.sc = init(document.getElementById("root"), ctx); window.sc.show();</script>`;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    const p = url.pathname;
    if (p === "/") { res.writeHead(200, { "content-type": "text/html" }); return res.end(PAGE); }
    if (!p.startsWith("/api/")) {
      const f = path.join(STATIC, p);
      if (f.startsWith(STATIC) && fs.existsSync(f) && fs.statSync(f).isFile()) { res.writeHead(200, { "content-type": MIME[path.extname(f)] || "application/octet-stream" }); return res.end(fs.readFileSync(f)); }
      res.writeHead(404); return res.end();
    }
    const data = body ? JSON.parse(body) : {};
    if (req.method === "POST") calls.push({ p, data });
    if (p === "/api/screen/status") return json(res, 200, { available: true, monitors: [{ id: 1, width: 1920, height: 1080 }], control_available: true, control_armed: false, armed_seconds_left: 0 });
    if (p === "/api/screen/frame") { res.writeHead(200, { "content-type": "image/png", "x-screen-size": "1920x1080" }); return res.end(PNG); }
    if (p === "/api/screen/stream") { res.writeHead(404); return res.end(); }
    if (p === "/api/screen/arm") return json(res, 200, { ok: true, armed_seconds_left: 300, message: "on" });
    if (p === "/api/screen/input" || p === "/api/screen/paste") return json(res, 200, { ok: true });
    if (p === "/api/vision/explain") return data.source === "phone" ? json(res, 200, { answer: "This is the remote screen page.", provider: "text model" }) : json(res, 503, { detail: "No vision AI is set up, so the screen picture cannot be explained yet. moondream" });
    if (p === "/api/vision/setup") return json(res, 200, { ready: false, providers: [], ollama: { up: true, host: "http://127.0.0.1:11434", vision: [] }, pull_options: [{ tag: "moondream", label: "moondream" }], kinds: ["Local vision model"] });
    if (p === "/api/vision/test") return json(res, 200, { ok: true, answer: "Red.", provider: "Ollama moondream" });
    if (p === "/api/models/pull" && req.method === "POST") return json(res, 200, { status: "running" });
    if (p === "/api/models/pull") return json(res, 200, { pull: { tag: "moondream", status: "done" } });
    json(res, 404, { detail: "no" });
  });
});

const fail = (m) => { console.error("FAIL: " + m); process.exitCode = 1; };
const ok = (c, m) => { if (!c) fail(m); };
(async () => {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true })).newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(base);
  await page.waitForSelector(".sc-kbopen", { state: "visible" });
  await page.waitForFunction(() => document.querySelector(".sc-img")?.src?.startsWith("blob:"), null, { timeout: 8000 });   // stream 404 -> polling fallback
  // disarmed: Keyboard asks to enable control first
  await page.click(".sc-kbopen");
  await page.click(".ui-modal__actions .ui-btn--primary");
  await page.waitForFunction(() => document.querySelector(".sc-chip--armed:not([hidden])"));
  ok(calls.some((c) => c.p === "/api/screen/arm" && c.data.confirm === true), "arm request not sent");
  await page.click(".sc-kbopen");
  await page.waitForSelector(".sc-typer", { state: "visible" });
  ok(await page.evaluate(() => document.activeElement?.classList.contains("sc-typer")), "typing field is not focused");
  ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "horizontal overflow at 390px");
  await page.keyboard.type("hi");
  await page.keyboard.press("Backspace");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(300);
  const typed = calls.filter((c) => c.p === "/api/screen/input").map((c) => c.data);
  ok(typed.some((d) => d.action === "type" && d.text === "h") && typed.some((d) => d.action === "type" && d.text === "i"), "typed characters not forwarded: " + JSON.stringify(typed));
  ok(typed.some((d) => d.action === "key" && d.key === "backspace"), "backspace not sent");
  ok(typed.some((d) => d.action === "key" && d.key === "enter"), "enter not sent");
  // whole text via clipboard paste
  await page.fill(".sc-sendtext", "café 日本");
  await page.click(".sc-kbpanel .ui-btn--primary");
  await page.waitForTimeout(200);
  ok(calls.some((c) => c.p === "/api/screen/paste" && c.data.text === "café 日本"), "send text did not call /paste");
  // key bar
  for (const label of ["PgDn", "Win", "Ctrl+C", "Alt+Tab", "F5", "Esc"]) await page.click(`.sc-keys:not(.sc-keys--browser) [aria-label="${label}"]`);
  await page.waitForTimeout(200);
  const all = calls.filter((c) => c.p === "/api/screen/input").map((c) => JSON.stringify(c.data));
  ok(all.some((d) => d.includes('"key":"pagedown"')) && all.some((d) => d.includes('"key":"win"')) && all.some((d) => d.includes('["ctrl","c"]')) && all.some((d) => d.includes('["alt","tab"]')), "key bar requests missing: " + all.join(" "));
  // explain: laptop source fails -> visible reason, Set up vision, fallback to the app screen
  await page.click(".sc-explain-btn");
  await page.click(".ui-modal__actions .ui-btn--primary");
  await page.waitForSelector(".ui-sheet .ui-notice--bad");
  ok((await page.textContent(".ui-sheet .ui-notice--bad")).includes("No vision AI is set up"), "exact reason not shown");
  await page.click(".ui-sheet .sc-setupvision");
  await page.waitForSelector(".sc-pull");
  await page.click(".sc-vtest");
  await page.waitForFunction(() => /Works: Ollama moondream/.test(document.querySelector(".sc-vsetup")?.textContent || ""));
  await page.click(".sc-pull");
  await page.click(".ui-modal__actions .ui-btn--primary");
  await page.waitForFunction(() => /moondream is downloaded/.test(document.querySelector(".sc-vsetup")?.textContent || ""));
  ok(calls.some((c) => c.p === "/api/models/pull" && c.data.tag === "moondream" && c.data.confirm === true), "pull request missing");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);
  await page.click('#view-screen button[data-value="phone"]');
  await page.click(".sc-explain-btn");
  await page.click(".ui-modal__actions .ui-btn--primary");
  await page.waitForFunction(() => /remote screen page/.test(document.querySelector(".ui-sheet")?.textContent || ""));
  const phoneCall = calls.filter((c) => c.p === "/api/vision/explain").pop();
  ok(phoneCall.data.source === "phone" && phoneCall.data.text.length > 0, "phone explain did not send the screen text");
  ok(errors.length === 0, "page errors: " + errors.join("; "));
  await browser.close();
  server.close();
  if (!process.exitCode) console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
