// Phone performance guard: node tests/perf_check.js <base-url>
// Home and Talk at 390px with the CPU throttled 4x: few long tasks, few timers, no separate background polls
// (the shell asks /api/ticker instead), and no backdrop blur on small controls.
const { chromium } = require("playwright");
const base = process.argv[2];
const fail = [];
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const page = await ctx.newPage();
  const hits = {};
  page.on("request", (r) => { const p = new URL(r.url()).pathname; hits[p] = (hits[p] || 0) + 1; });
  await page.addInitScript(() => {
    window.__lt = []; window.__iv = new Set();
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__lt.push(Math.round(e.duration)); }).observe({ entryTypes: ["longtask"] });
    const si = window.setInterval, ci = window.clearInterval;
    window.setInterval = (f, ms, ...a) => { const id = si(f, ms, ...a); window.__iv.add(id); return id; };
    window.clearInterval = (id) => { window.__iv.delete(id); return ci(id); };
  });
  const cdp = await ctx.newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  await page.goto(base);
  await page.fill("#token", "devtoken");
  await page.click("#login-submit");
  await page.waitForSelector("#tabs button");
  await page.waitForTimeout(2500);
  await page.click('#tabs [data-tab="assistant"]');
  await page.waitForTimeout(2500);
  const r = await page.evaluate(() => ({
    lt: window.__lt, iv: window.__iv.size,
    bf: [...document.querySelectorAll(".ui-btn, .ui-chip, .hm-qa")].filter((e) => getComputedStyle(e).backdropFilter !== "none").length,
  }));
  if (r.lt.length > 4 || Math.max(0, ...r.lt) > 400) fail.push(`long tasks too many/long: ${JSON.stringify(r.lt)}`);
  if (r.iv > 4) fail.push(`too many live setInterval timers: ${r.iv}`);
  if (r.bf) fail.push(`${r.bf} small controls still use backdrop-filter`);
  for (const p of ["/api/alerts/pending", "/api/presence/pending", "/api/notifications/unread-count", "/api/meetings/status", "/api/panic"]) {
    if ((hits[p] || 0) > 0) fail.push(`${p} polled separately (${hits[p]}x); it belongs in /api/ticker`);
  }
  if ((hits["/api/ticker"] || 0) > 3) fail.push(`ticker asked ${hits["/api/ticker"]}x in ~5 s`);
  const dup = Object.entries(hits).filter(([p, n]) => /^\/api\//.test(p) && n > 2 && !/ticker|presence\/phone/.test(p));
  if (dup.length) fail.push("duplicate fetches: " + dup.map(([p, n]) => `${p} x${n}`).join(", "));
  await browser.close();
  if (fail.length) { console.log(fail.join("\n")); process.exit(1); }
  console.log("perf ok", JSON.stringify(r));
})().catch((e) => { console.log(e); process.exit(2); });
