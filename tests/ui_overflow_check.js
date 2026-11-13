// No-horizontal-overflow check for every main screen, plus "page width unchanged after closing voice mode".
// Run: node tests/ui_overflow_check.js <base-url> <shots-dir>   (dev server: python -m hub.devserver, token devtoken)
// Exits 0 when nothing overflows. Needs the `playwright` node package and a browser.
const { chromium } = require("playwright");
const [base, shots] = [process.argv[2], process.argv[3] || "/tmp/claude-0/shots"];
const SCREENS = ["home", "assistant", "today", "autopilot", "brain", "research", "skills", "ccsessions", "buildflow", "devto", "settings", "terminal"];
const SIZES = [[390, 844], [360, 740]];

(async () => {
  const browser = await chromium.launch({ args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] });
  const problems = [];
  for (const [w, h] of SIZES) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, permissions: ["microphone"] });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => problems.push(`${w}: pageerror ${e}`));
    await page.goto(base);
    await page.fill("#token", "devtoken");
    await page.click("#login-submit");
    await page.waitForSelector("#tabs", { timeout: 10000 });
    const widths = () => page.evaluate(() => ({ sw: document.scrollingElement.scrollWidth, iw: window.innerWidth,
                                                 cw: document.documentElement.clientWidth }));
    for (const id of SCREENS) {
      await page.evaluate((x) => { location.hash = "#/" + x; }, id);
      await page.waitForTimeout(900);
      const m = await widths();
      await page.screenshot({ path: `${shots}/ov-${id}-${w}.png` });
      if (m.sw > m.iw) problems.push(`${w}x${h} ${id}: scrollWidth ${m.sw} > innerWidth ${m.iw}`);
    }
    if (w === 390) {   // voice mode open + close must not change the page width
      await page.evaluate(() => { location.hash = "#/assistant"; });
      await page.waitForTimeout(700);
      const before = await widths();
      const btn = page.locator('[aria-label*="oice mode" i]').first();
      if (await btn.count()) {
        await btn.click();
        await page.waitForSelector(".vm", { timeout: 5000 }).catch(() => {});
        await page.waitForTimeout(600);
        await page.screenshot({ path: `${shots}/ov-voicemode-${w}.png` });
        await page.click(".vm-close").catch(() => {});
        await page.waitForTimeout(700);
        const after = await widths();
        if (after.sw !== before.sw || after.cw !== before.cw) problems.push(`voice close changed width: ${JSON.stringify({ before, after })}`);
      } else console.log("note: no voice mode button found on Talk");
    }
    await ctx.close();
  }
  await browser.close();
  if (problems.length) { console.error(problems.join("\n")); process.exit(1); }
  console.log("no horizontal overflow");
})().catch((e) => { console.error(e); process.exit(2); });
