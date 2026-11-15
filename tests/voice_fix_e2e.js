// Browser check for the phone-voice bug-fix round. Run: node tests/voice_fix_e2e.js <base-url>
// 1. /api/tts 423 (panic) and 502 JSON bodies reach the UI with their REAL reason (not "paused by the panic button" for everything)
// 2. the hub's WAV plays through ONE shared <audio> element that the first tap unlocks (no phone voice needed)
// 3. inside the Android shell (window.Capacitor native) the in-page wake word is OFF by default, never restarts faster than
//    4 s, and is off while the native service runs
const { chromium } = require("playwright");
const base = process.argv[2];

const FAKE_SR = `(() => {
  window.__starts = [];
  class FakeSR {
    constructor() { this.continuous = false; }
    start() { this.live = true; window.__starts.push(Date.now()); setTimeout(() => { this.live = false; this.onend && this.onend(); }, 20); }
    abort() { this.live = false; }
    stop() { this.live = false; }
  }
  window.SpeechRecognition = window.webkitSpeechRecognition = FakeSR;
})();`;

async function login(page) {
  await page.goto(base);
  await page.fill("#token", "devtoken");
  await page.click("#login-submit");
  await page.waitForSelector("#appbar", { timeout: 10000 });
}

function wav() {
  return Buffer.concat([Buffer.from("RIFF"), Buffer.from([0xb4, 1, 0, 0]), Buffer.from("WAVEfmt "), Buffer.from([16, 0, 0, 0, 1, 0, 1, 0, 0x40, 0x1f, 0, 0, 0x40, 0x1f, 0, 0, 1, 0, 8, 0]),
    Buffer.from("data"), Buffer.from([0x90, 1, 0, 0]), Buffer.alloc(400, 128)]);
}

(async () => {
  const browser = await chromium.launch({ args: ["--autoplay-policy=document-user-activation-required"] });
  const errors = [];

  // ---------- 1 + 2: the hub's voice answers
  {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    await login(page);
    let mode = "panic";
    await page.route("**/api/tts", async (route) => {
      if (mode === "panic") return route.fulfill({ status: 423, contentType: "application/json", body: JSON.stringify({ detail: "Voice is paused because Panic is on. Tap Resume.", reason: "panic" }) });
      if (mode === "bare423") return route.fulfill({ status: 423, body: "" });
      if (mode === "engine") return route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "No laptop voice is ready.", reason: "engine" }) });
      return route.fulfill({ status: 200, contentType: "audio/wav", body: wav() });
    });
    const r1 = await page.evaluate(async () => { const m = await import("/js/hubtts.js"); return m.hubSynth("Hello there"); });
    if (r1.kind !== "silent" || r1.reason !== "panic" || !/Resume/.test(r1.detail)) throw new Error("423 not explained: " + JSON.stringify(r1));
    mode = "bare423";
    const r2 = await page.evaluate(async () => { const m = await import("/js/hubtts.js"); return m.hubSynth("Hello there"); });
    if (r2.kind !== "silent" || r2.reason !== "panic") throw new Error("old-hub bare 423 mishandled: " + JSON.stringify(r2));
    mode = "engine";
    const r3 = await page.evaluate(async () => { const m = await import("/js/hubtts.js"); return m.hubSynth("Hello there"); });
    if (r3.kind !== "browser" || r3.why !== "engine" || !/No laptop voice/.test(r3.detail)) throw new Error("502 not explained: " + JSON.stringify(r3));
    // the toast says the real reason and has the one-tap button
    await page.evaluate(async () => { const m = await import("/js/hubtts.js"); m.explainProblem({ reason: "engine", detail: "No laptop voice is ready." }); });
    const btn = await page.textContent(".ui-toast__action");
    if (!/Use Windows voice/.test(btn)) throw new Error("no Use Windows voice button: " + btn);
    await page.evaluate(async () => { const m = await import("/js/hubtts.js"); m.explainProblem({ reason: "panic", detail: "Voice is paused because Panic is on. Tap Resume." }); });
    const btns = await page.$$eval(".ui-toast__action", (n) => n.map((x) => x.textContent));
    if (!btns.includes("Resume")) throw new Error("no Resume button: " + btns);
    // audio: one shared element, unlocked by the first tap
    mode = "wav";
    const before = await page.evaluate(async () => { const m = await import("/js/hubtts.js"); return m.audioUnlocked(); });
    if (before) throw new Error("audio unlocked before any tap");
    await page.mouse.click(200, 400);
    await page.waitForFunction(async () => (await import("/js/hubtts.js")).audioUnlocked(), null, { timeout: 5000 });
    const played = await page.evaluate(async () => {
      const m = await import("/js/hubtts.js");
      const r = await m.hubSynth("Hello there");
      if (r.kind !== "audio") return "not audio: " + r.kind;
      const p1 = m.createAudioPlayer(), p2 = m.createAudioPlayer();
      const same = m.sharedAudio() === m.sharedAudio();
      let started = false;
      const done = p1.play(r.data);
      started = p1.playing;
      await Promise.race([done, new Promise((res) => setTimeout(res, 4000))]);
      await p2.play(r.data);
      return { same, started };
    });
    if (typeof played === "string" || !played.same || !played.started) throw new Error("audio did not play on the shared element: " + JSON.stringify(played));
    await ctx.close();
  }

  // ---------- 3: the Android shell
  {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, permissions: ["microphone"] });
    await ctx.addInitScript(FAKE_SR);
    await ctx.addInitScript(() => {
      window.Capacitor = { isNativePlatform: () => true, Plugins: {} };
      try { localStorage.setItem("hub.wake", "1"); } catch (_) { /* ignore */ }     // the OLD in-page switch: must not start anything in the app
    });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    await login(page);
    await page.waitForTimeout(1500);
    const n0 = await page.evaluate(() => window.__starts.length);
    if (n0 !== 0) throw new Error("in-page wake word started inside the app without the explicit switch (" + n0 + " starts)");
    // explicit opt-in: starts, but never faster than every 4 s
    await page.evaluate(async () => { const m = await import("/js/wake.js"); m.setWakeEnabled(true); });
    await page.waitForTimeout(6000);
    const starts = await page.evaluate(() => window.__starts.slice());
    if (starts.length < 1) throw new Error("opt-in did not start the recogniser");
    for (let i = 1; i < starts.length; i += 1) {
      if (starts[i] - starts[i - 1] < 3900) throw new Error("restarted after " + (starts[i] - starts[i - 1]) + " ms (< 4 s): that is the beep loop");
    }
    // the native service takes over: the in-page one stays off
    await page.evaluate(async () => { const m = await import("/js/wake.js"); m.setNativeWakeRunning(true); });
    await page.waitForTimeout(300);
    const frozen = await page.evaluate(() => window.__starts.length);
    await page.waitForTimeout(5000);
    const after = await page.evaluate(() => window.__starts.length);
    if (after !== frozen) throw new Error("recogniser kept restarting while the native wake service runs");
    await ctx.close();
  }

  await browser.close();
  if (errors.length) throw new Error("page errors: " + errors.join("; "));
  console.log("VOICE FIX E2E OK");
})().catch((e) => { console.error("VOICE FIX E2E FAIL", e.message); process.exit(1); });
