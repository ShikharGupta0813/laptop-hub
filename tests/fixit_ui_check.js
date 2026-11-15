// Phone checks for the Friday / Jarvis fix-it kit. Run: node tests/fixit_ui_check.js <base-url>  (dev server, token devtoken)
// A fake phone: its voice list arrives late (like Android Chrome) and every spoken sentence is recorded.
const { chromium } = require("playwright");
const base = process.argv[2];
const fail = [];
const check = (ok, what) => { if (!ok) fail.push(what); };

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => fail.push("pageerror " + e));
  await page.addInitScript(() => {
    window.__spoken = [];
    let voices = [];
    const synth = {
      getVoices: () => voices, listeners: {},
      addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }, removeEventListener() {},
      cancel() {}, resume() {},
      speak(u) { window.__spoken.push(u.text); setTimeout(() => { u.onstart && u.onstart(); setTimeout(() => u.onend && u.onend(), 20); }, 10); },
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth });
    window.SpeechSynthesisUtterance = function (t) { this.text = t; };
    setTimeout(() => { voices = [{ lang: "en-US", name: "x", default: true, localService: true }]; (synth.listeners.voiceschanged || []).forEach((f) => f()); }, 800);
  });
  await page.goto(base);
  await page.fill("#token", "devtoken");
  await page.click("#login-submit");
  await page.waitForSelector("#tabs");

  // Friday: Test voice speaks per sentence even though the voice list loads late; Provider check lists providers with Test buttons.
  await page.goto(base + "/#friday").catch(() => {});
  await page.waitForSelector(".fk-voice");
  await page.click("text=Test voice");
  await page.waitForTimeout(1500);
  const spoken = await page.evaluate(() => window.__spoken.filter((t) => t.trim()));
  check(spoken.length >= 2 && /Friday/.test(spoken[0]), "Test voice speaks sentence by sentence: " + JSON.stringify(spoken));
  check(await page.evaluate(() => /phone's voice/.test(document.querySelector(".fk-voice .fk-note").textContent)), "voice note says which voice played");
  await page.waitForSelector("#provider-check .fk-item");
  check(await page.evaluate(() => document.querySelectorAll("#provider-check .fk-item").length >= 3), "provider check lists providers");
  check(await page.evaluate(() => [...document.querySelectorAll("#provider-check button")].some((b) => /^Test/.test(b.textContent))), "each provider has a Test button");
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "no horizontal overflow on Friday");
  await page.click(".fk-voice .fk-switch");                       // Voice off: Test voice still plays (an explicit test), the switch is remembered
  check(await page.evaluate(() => localStorage.getItem("hub.voice.on") === "0"), "Voice off is remembered");

  // Jarvis: the text box is never dead; with Jarvis stopped Send says so, keeps the text and offers Start.
  await page.click('[data-tab="apps"]');
  await page.waitForTimeout(1000);
  await page.getByText("Jarvis", { exact: true }).first().click({ force: true });
  await page.waitForSelector("#ap-jv-say");
  await page.waitForTimeout(1200);
  check(await page.evaluate(() => !document.querySelector("#ap-jv-say").disabled), "Jarvis text box is enabled");
  await page.fill("#ap-jv-say", "open the Q3 report");
  await page.click(".ap-send");
  await page.waitForTimeout(500);
  const msg = await page.evaluate(() => document.querySelector(".ap-say-msg")?.textContent || "");
  check(/not running|isn't set up|patch/.test(msg), "Send explains what is missing: " + msg);
  check(await page.evaluate(() => document.querySelector("#ap-jv-say").value === "open the Q3 report"), "typed text is kept");
  check(await page.evaluate(() => !!document.querySelector(".ap-jarvis .fk #provider-check")), "Jarvis shows Provider check");
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "no horizontal overflow on Jarvis");
  await browser.close();
  if (fail.length) { console.error(fail.join("\n")); process.exit(1); }
  console.log("ok");
})();
