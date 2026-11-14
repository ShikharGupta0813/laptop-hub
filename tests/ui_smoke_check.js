// Screen smoke: visit EVERY screen registered in js/main.js at two phone sizes and report console errors,
// uncaught page errors, failed /api calls (4xx other than expected, 5xx) and horizontal overflow.
// Run: node tests/ui_smoke_check.js <base-url> <shots-dir>   (dev server: python -m hub.devserver, token devtoken)
// Exits 0 when the screens are clean. Needs the `playwright` node package and a browser.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const [base, shots] = [process.argv[2], process.argv[3] || "/tmp/claude-0/shots"];
const main = fs.readFileSync(path.join(__dirname, "..", "hub", "static", "js", "main.js"), "utf8");
const SCREENS = [...main.matchAll(/\{\s*id:\s*"([a-z0-9-]+)"[^}]*path:/g)].map((m) => m[1]);
const SIZES = [[390, 844], [360, 740]];
// Answers that mean "not set up on this machine", which is a normal state for the dev hub.
const EXPECTED = [/503/, /501/, /404.*\/api\/(?:profile|work\/asana)/];
fs.mkdirSync(shots, { recursive: true });

(async () => {
  const browser = await chromium.launch({ args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] });
  const problems = [];
  for (const [w, h] of SIZES) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, permissions: ["microphone"] });
    const page = await ctx.newPage();
    let where = "login";
    const note = (msg) => problems.push(`${w}x${h} ${where}: ${msg}`);
    page.on("pageerror", (e) => note(`pageerror ${e}`));
    page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) note(`console ${m.text().slice(0, 200)}`); });
    page.on("response", (r) => {
      const u = r.url(); const s = r.status();
      if (s >= 400 && /\/api\//.test(u) && !EXPECTED.some((re) => re.test(`${s} ${u}`))) note(`${s} ${r.request().method()} ${u.replace(base, "")}`);
      if (s === 404 && !/\/api\//.test(u) && !/favicon|HEAD/.test(u) && r.request().method() !== "HEAD") note(`404 ${u.replace(base, "")}`);
    });
    page.on("requestfailed", (r) => { if (!/ERR_ABORTED/.test(r.failure()?.errorText || "")) note(`request failed ${r.url().replace(base, "")}`); });
    await page.goto(base);
    await page.fill("#token", "devtoken");
    await page.click("#login-submit");
    await page.waitForSelector("#tabs", { timeout: 10000 });
    await page.evaluate(() => {
      // Elements poking out past the right edge of the phone, unless a scroller (overflow auto/scroll) or a fixed layer holds them.
      window.wideElements = (view) => {
        const iw = window.innerWidth; const out = [];
        for (const e of view.querySelectorAll("*")) {
          const r = e.getBoundingClientRect();
          if (r.width === 0 || r.height === 0 || r.right <= iw + 1) continue;
          let skip = false;
          for (let a = e; a && a !== view; a = a.parentElement) {
            const cs = getComputedStyle(a);
            if (/(auto|scroll|hidden|clip)/.test(cs.overflowX) || cs.position === "fixed" || cs.visibility === "hidden" || cs.display === "none") { skip = true; break; }
          }
          if (!skip) out.push(`${e.tagName.toLowerCase()}.${String(e.className).split(" ")[0]} right=${Math.round(r.right)}`);
        }
        return out.slice(0, 4);
      };
    });
    for (const id of SCREENS) {
      where = id;
      await page.evaluate((x) => { location.hash = "#/" + x; }, id);
      await page.waitForTimeout(700);
      const m = await page.evaluate((x) => {
        const wideElements = window.wideElements;
        const v = document.getElementById("view-" + x);
        return { sw: document.scrollingElement.scrollWidth, iw: window.innerWidth, shown: !!v && !v.hidden,
                 text: v ? v.innerText.trim().length : 0,
                 wide: v ? wideElements(v) : [], broken: v ? /couldn't start|isn't installed/i.test(v.innerText) : true };
      }, id);
      await page.screenshot({ path: `${shots}/smoke-${id}-${w}.png` });
      if (m.wide.length) note(`elements past the right edge: ${m.wide.join(", ")}`);
      if (m.sw > m.iw) note(`scrollWidth ${m.sw} > innerWidth ${m.iw}`);
      if (!m.shown) note("view not shown");
      else if (!m.text) note("view is blank");
      if (m.broken) note("shows a 'couldn't start' or 'not installed' page");
    }
    await ctx.close();
  }
  await browser.close();
  if (problems.length) { console.error([...new Set(problems)].join("\n")); process.exit(1); }
  console.log(`${SCREENS.length} screens clean`);
})().catch((e) => { console.error(e); process.exit(2); });
