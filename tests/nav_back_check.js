// Android Back behaviour in a real browser: node tests/nav_back_check.js <base-url>
// Uses page.goBack (the same popstate the system Back button produces) and the hub:back event the native shell sends.
const { chromium } = require("playwright");
const base = process.argv[2];
const fail = [];
const eq = (a, b, m) => { if (a !== b) fail.push(`${m}: expected ${b}, got ${a}`); };
const cur = (p) => p.evaluate(() => document.getElementById("app").dataset.view);
const toastText = (p) => p.evaluate(() => [...document.querySelectorAll(".ui-toast")].map((n) => n.textContent).join("|"));

async function open(browser, hash = "") {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  await page.goto(base + hash);
  await page.fill("#token", "devtoken");
  await page.click("#login-submit");
  await page.waitForSelector("#tabs button");
  await page.waitForTimeout(900);
  return page;
}

(async () => {
  const browser = await chromium.launch();
  // 1. sub-view back goes one level; tab -> Home; first Home back toasts and stays.
  let page = await open(browser);
  eq(await cur(page), "home", "start");
  await page.click('#tabs [data-tab="apps"]'); await page.waitForTimeout(300);
  await page.evaluate(() => { location.hash = "#/notes"; }); await page.waitForTimeout(500);
  eq(await cur(page), "notes", "notes opened");
  await page.goBack(); await page.waitForTimeout(400);
  eq(await cur(page), "apps", "back from sub-view");
  await page.click('#tabs [data-tab="work"]'); await page.waitForTimeout(300);
  await page.click('#tabs [data-tab="learn"]'); await page.waitForTimeout(300);
  await page.goBack(); await page.waitForTimeout(400);
  eq(await cur(page), "home", "tab switches replace: back from a tab goes Home");
  await page.goBack(); await page.waitForTimeout(500);
  eq(await cur(page), "home", "first back at Home stays");
  eq(/Press back again to exit/.test(await toastText(page)), true, "exit toast shown");
  // 2. a sheet closes before the screen changes
  await page.waitForTimeout(2300);
  await page.click('#tabs [data-tab="apps"]'); await page.waitForTimeout(300);
  await page.evaluate(async () => { const ui = await import("/js/ui.js"); window.__s = ui.sheet({ title: "T", content: document.createElement("div") }); });
  await page.waitForTimeout(300);
  eq(await page.evaluate(() => !!document.querySelector(".ui-sheet")), true, "sheet open");
  await page.goBack(); await page.waitForTimeout(500);
  eq(await page.evaluate(() => !!document.querySelector(".ui-sheet:not(.is-leaving)")), false, "sheet closed by back");
  eq(await cur(page), "apps", "screen kept after closing sheet");
  // 3. native event path: screen -> home -> arm -> exit
  await page.evaluate(() => window.dispatchEvent(new Event("hub:back"))); await page.waitForTimeout(500);
  eq(await cur(page), "home", "hub:back from top-level goes Home");
  await page.evaluate(() => { window.__exit = 0; window.Capacitor = { Plugins: { App: { exitApp() { window.__exit++; } } } }; });
  await page.evaluate(() => window.dispatchEvent(new Event("hub:back")));
  eq(await page.evaluate(() => window.__exit), 0, "hub:back at Home first time does not exit");
  await page.evaluate(() => window.dispatchEvent(new Event("hub:back")));
  eq(await page.evaluate(() => window.__exit), 1, "second hub:back within 2s exits");
  await page.close();
  // 4. refresh / deep link restores the screen from the hash
  page = await open(browser, "#/settings");
  eq(await cur(page), "settings", "deep link");
  await page.reload(); await page.waitForTimeout(1200);
  eq(await cur(page), "settings", "refresh keeps screen");
  await page.goBack(); await page.waitForTimeout(500);
  eq(await cur(page), "home", "back from deep-linked screen goes Home");
  await browser.close();
  if (fail.length) { console.log(fail.join("\n")); process.exit(1); }
  console.log("nav back ok");
})().catch((e) => { console.log(e); process.exit(2); });
