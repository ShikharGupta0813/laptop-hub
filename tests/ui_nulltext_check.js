// No stray "null" / "undefined" text anywhere on screen (a null child passed to append()/replaceChildren() prints "null").
// Visits every screen registered in js/main.js at 390x844 on the dev hub (fake data), opens the Settings sheets (Voices,
// Dictation, ...) and fails when any visible text contains the bare words null / undefined / [object Object].
//   node tests/ui_nulltext_check.js <base-url>
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const base = process.argv[2];
const main = fs.readFileSync(path.join(__dirname, "..", "hub", "static", "js", "main.js"), "utf8");
const SCREENS = [...main.matchAll(/\{\s*id:\s*"([a-z0-9-]+)"[^}]*path:/g)].map((m) => m[1]);
const BAD = /(^|[^A-Za-z0-9_])(null|undefined)([^A-Za-z0-9_]|$)|\[object Object\]/;
const problems = [];

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  await page.goto(base);
  await page.fill("#token", "devtoken");
  await page.click("#login-submit");
  await page.waitForSelector("#tabs button");
  const scan = async (where) => {
    const texts = await page.evaluate(() => [...document.querySelectorAll(".ui-view:not([hidden]), [id^=view-]:not([hidden]), .ui-sheet:not(.is-leaving), .ui-modal")]
      .map((n) => n.innerText || ""));
    for (const t of texts) {
      const hit = t.split("\n").find((line) => BAD.test(line));
      if (hit) problems.push(`${where}: "${hit.trim().slice(0, 80)}"`);
    }
  };
  const closeSheets = async () => {
    for (let i = 0; i < 4; i++) {
      if (!(await page.evaluate(() => !!document.querySelector(".ui-sheet:not(.is-leaving), .ui-modal")))) break;
      await page.keyboard.press("Escape");
      await page.waitForTimeout(450);
    }
  };
  for (const id of SCREENS) {
    await page.evaluate((x) => { location.hash = "#/" + x; }, id);
    await page.waitForTimeout(700);
    await scan(`screen ${id}`);
  }
  // Settings: open every row that opens a sheet, plus Voices straight from its module.
  await page.evaluate(() => { location.hash = "#/settings"; });
  await page.waitForTimeout(900);
  const count = await page.evaluate(() => document.querySelectorAll("#view-settings button, #view-settings [role=button]").length);
  for (let i = 0; i < Math.min(count, 80); i++) {
    const label = await page.evaluate((n) => {
      const b = document.querySelectorAll("#view-settings button, #view-settings [role=button]")[n];
      if (!b || b.disabled || b.closest("[hidden]")) return null;
      const t = (b.innerText || b.getAttribute("aria-label") || "").trim();
      if (/delete|reset|restore|sign out|log ?out|wipe|clear|panic|stop|uninstall|update/i.test(t)) return null;
      b.click();
      return t || `button ${n}`;
    }, i);
    if (label === null) continue;
    await page.waitForTimeout(450);
    await scan(`settings > ${label.slice(0, 40)}`);
    await closeSheets();
    if (!/#\/settings/.test(await page.evaluate(() => location.hash))) {
      await page.evaluate(() => { location.hash = "#/settings"; });
      await page.waitForTimeout(700);
    }
  }
  await page.evaluate(async () => { (await import("/js/voices.js")).openVoices(); });
  await page.waitForTimeout(1200);
  await scan("settings > Voices sheet");
  await closeSheets();
  await browser.close();
  if (problems.length) { console.log([...new Set(problems)].join("\n")); process.exit(1); }
  console.log(`${SCREENS.length} screens and the Settings sheets show no null/undefined`);
})().catch((e) => { console.log(e); process.exit(2); });
