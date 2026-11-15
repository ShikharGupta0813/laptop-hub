// "AI for this chat" in a real browser at phone widths (390 and 320), against the dev hub with a scripted fake hub
// (page.route answers /api/status, /api/lanes and the chat stream), so every state is deterministic.
//   node tests/chat_ai_check.js <base-url>
// Checks: the strip is one readable button (no overlap, ellipsis, inside the screen); choosing Gemini changes the provider of the
// NEXT turn and the reply appears IN the chat with "answered by Gemini"; the choice survives a reload; Council not set up shows
// a "How to set up" button instead of a failed sheet; Council / "Send to lane" answers are posted into the chat (no "Work lane
// result" sheet); not-ready providers are greyed with a reason; no visible "null" or "undefined".
const { chromium } = require("playwright");
const base = process.argv[2];
const fail = [];
const ok = (cond, msg) => { if (!cond) fail.push(msg); };

const STATUS = (over = {}) => ({
  hostname: "fake", ollama: { up: true, models: ["m"] }, default_provider: "local", cli: [
    { id: "agy", label: "Antigravity (agy)", ready: true, reason: "" },
    { id: "long", label: "A very long login name that will not fit on a phone strip at all", ready: false, reason: "not logged in: open it once and sign in" }],
  providers: {
    local: { ready: true, reason: "Runs on this laptop." }, claude: { ready: false, reason: "No ANTHROPIC_API_KEY." },
    claude_login: { ready: true, reason: "Claude Code is installed." }, gemini: { ready: true, reason: "A Gemini key is saved." },
    duo: { ready: false, reason: "Needs a Gemini key." }, router: {} },
  ...over });
const LANES = (councilReady) => ({
  lanes: [
    { id: "council", label: "Council", enabled: true, cost: "free", notes: "background", why_not: councilReady ? "" : "Council is not set up on this laptop." },
    { id: "local", label: "Local model", enabled: true, cost: "free", notes: "", why_not: "" },
    { id: "local-reasoning", label: "Local deep thinking", enabled: true, cost: "free", notes: "", why_not: "" }],
  classes: [], council: { ready: councilReady, why: councilReady ? "" : "not set up" }, saved: { week: 0 } });
const sse = (events) => events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
const visibleBad = (page) => page.evaluate(() => {
  const t = [...document.querySelectorAll("#view-assistant, .ui-sheet")].map((n) => n.innerText).join("\n");
  return (t.match(/\bnull\b|\bundefined\b|\[object Object\]/g) || []).length;
});

async function run(browser, width) {
  const ctx = await browser.newContext({ viewport: { width, height: 780 } });
  const page = await ctx.newPage();
  const where = `${width}px`;
  const sent = [];
  let councilReady = false;
  const laneRuns = [];
  page.on("pageerror", (e) => fail.push(`${where} pageerror ${e}`));
  await page.route("**/api/status", (r) => r.fulfill({ json: STATUS() }));
  await page.route("**/api/lanes", (r) => r.fulfill({ json: LANES(councilReady) }));
  await page.route("**/api/lanes/run", async (r) => {
    const body = JSON.parse(r.request().postData() || "{}");
    laneRuns.push(body);
    await r.fulfill({ json: { id: "abc123abc123", status: "done", running: false, lane: body.lane, answer: `Lane ${body.lane} answer for: ${body.task}`, attempts: [], note: "" } });
  });
  await page.route("**/api/assistant/message", async (r) => {
    const body = JSON.parse(r.request().postData() || "{}");
    sent.push(body);
    const who = body.provider === "claude-login" ? { provider: "claude-login", label: "Claude (my login)" } : { provider: body.provider, label: body.provider === "gemini" ? "Gemini" : body.provider };
    await r.fulfill({ status: 200, headers: { "content-type": "text/event-stream" },
      body: sse([{ type: "text", t: `Reply from ${who.label}. ` }, { type: "answered_by", ...who }, { type: "done" }]) });
  });
  await page.goto(base);
  await page.fill("#token", "devtoken");
  await page.click("#login-submit");
  await page.waitForSelector("#tabs button");
  await page.evaluate(() => { try { localStorage.clear(); sessionStorage.clear(); } catch (_) { /* ignore */ } location.hash = "#/assistant"; });
  await page.waitForSelector(".ac-btn");
  await page.waitForTimeout(600);

  // 1. the strip: one readable button, inside the screen, not overlapping its neighbours
  const strip = await page.evaluate(() => {
    const b = document.querySelector(".ac-btn").getBoundingClientRect();
    const sibs = [...document.querySelectorAll(".as-bar > *")].filter((n) => !n.classList.contains("ac-btn")).map((n) => n.getBoundingClientRect());
    const name = document.querySelector(".ac-btn__name");
    return { left: b.left, right: b.right, width: b.width, iw: window.innerWidth, overlaps: sibs.some((s) => s.width && s.left < b.right - 1 && s.right > b.left + 1),
      text: name.textContent, ellipsis: getComputedStyle(name).textOverflow === "ellipsis", sw: document.scrollingElement.scrollWidth };
  });
  ok(strip.left >= 0 && strip.right <= strip.iw, `${where}: strip inside the screen (${strip.left}..${strip.right} of ${strip.iw})`);
  ok(!strip.overlaps, `${where}: strip overlaps the options/new chat buttons`);
  ok(strip.ellipsis && strip.width > 120, `${where}: strip truncates with ellipsis and stays wide enough (${strip.width})`);
  ok(strip.sw <= strip.iw, `${where}: no horizontal page scroll`);
  ok(/^(Local model|Claude|Auto|Gemini)/.test(strip.text), `${where}: strip names the current AI (got ${strip.text})`);

  // 2. the picker: not-ready ones greyed with a reason, Council has a setup button, nothing says null/undefined
  await page.click(".ac-btn");
  await page.waitForSelector(".ui-sheet .ac-row");
  const rows = await page.evaluate(() => [...document.querySelectorAll(".ui-sheet .ac-row")].map((r) => ({ id: r.dataset.choice, off: r.classList.contains("is-off"), text: r.innerText })));
  const row = (id) => rows.find((r) => r.id === id);
  ok(row("claude")?.off && /ANTHROPIC/.test(row("claude").text), `${where}: Claude without a key is greyed with its reason`);
  ok(row("gemini") && !row("gemini").off, `${where}: Gemini (ready) is selectable`);
  ok(row("cli:agy") && !row("cli:agy").off, `${where}: Antigravity (agy) is listed`);
  ok(!rows.some((r) => /Gemini CLI/.test(r.text)), `${where}: no Gemini CLI entry`);
  ok(row("lane:council")?.off && /isn't set up yet/.test(row("lane:council").text), `${where}: Council shows "Council isn't set up yet"`);
  ok(await page.locator(".ui-sheet .ac-how").count() === 1, `${where}: one-tap "How to set up" for Council`);
  ok(await visibleBad(page) === 0, `${where}: picker shows null/undefined`);
  await page.click(".ui-sheet .ac-how");
  await page.waitForTimeout(500);
  ok(/Set up the Council/.test(await page.evaluate(() => [...document.querySelectorAll(".ui-sheet__title")].map((n) => n.textContent).join("|"))), `${where}: how-to sheet opens`);
  await page.evaluate(() => document.querySelectorAll(".ui-sheet__foot .ui-btn--ghost").forEach((b) => b.click()));
  await page.waitForTimeout(500);
  await page.evaluate(() => document.querySelectorAll(".ui-sheet__foot .ui-btn--ghost").forEach((b) => b.click()));
  await page.waitForTimeout(500);

  // 3. choose Gemini: the NEXT turn uses it, the reply is in the chat, and it says who answered
  await page.click(".ac-btn");
  await page.waitForSelector(".ui-sheet .ac-row");
  await page.click('.ui-sheet .ac-row[data-choice="gemini"]');
  await page.waitForTimeout(500);
  ok((await page.textContent(".ac-btn__name")).includes("Gemini"), `${where}: strip now shows Gemini`);
  await page.fill(".as-input", "hello gemini");
  await page.click(".as-send");
  await page.waitForSelector(".as-assistant .as-bubble");
  await page.waitForTimeout(500);
  ok(sent.at(-1)?.provider === "gemini", `${where}: the message went with provider gemini (got ${sent.at(-1)?.provider})`);
  ok(/Reply from Gemini/.test(await page.textContent(".as-assistant .as-bubble")), `${where}: the reply is in the chat`);
  ok(/answered by Gemini/.test(await page.textContent(".as-by")), `${where}: "answered by Gemini" under the reply`);

  // 4. switch to Claude (login): the very next turn changes provider
  await page.click(".ac-btn");
  await page.waitForSelector(".ui-sheet .ac-row");
  await page.click('.ui-sheet .ac-row[data-choice="claude-login"]');
  await page.waitForTimeout(500);
  await page.fill(".as-input", "now claude");
  await page.click(".as-send");
  await page.waitForFunction(() => document.querySelectorAll(".as-by").length >= 2);
  ok(sent.at(-1)?.provider === "claude-login", `${where}: next turn used claude-login (got ${sent.at(-1)?.provider})`);
  ok(/answered by Claude/.test(await page.evaluate(() => [...document.querySelectorAll(".as-by")].at(-1).textContent)), `${where}: "answered by Claude"`);

  // 5. the choice is remembered for this conversation across a reload
  await page.reload();
  await page.waitForSelector(".ac-btn");
  await page.waitForTimeout(700);
  ok(/Claude/.test(await page.textContent(".ac-btn__name")), `${where}: choice persists after reload (${await page.textContent(".ac-btn__name")})`);

  // 6. Council ready: the answer lands in the chat, not in a "Work lane result" sheet
  councilReady = true;
  await page.waitForTimeout(2300);                 // api() shares identical GETs for 2 s
  await page.click(".ac-btn");
  await page.waitForSelector(".ui-sheet .ac-row");
  await page.click('.ui-sheet .ac-row[data-choice="lane:council"]');
  await page.waitForTimeout(400);
  await page.fill(".as-input", "council please");
  await page.click(".as-send");
  await page.waitForFunction(() => /Lane council answer/.test(document.querySelector("#view-assistant .as-list").innerText));
  ok(laneRuns.at(-1)?.lane === "council", `${where}: council lane was run`);
  ok(!(await page.evaluate(() => /Work lane result/.test(document.body.innerText))), `${where}: no "Work lane result" sheet`);
  ok(/answered by Council/.test(await page.evaluate(() => [...document.querySelectorAll(".as-by")].at(-1).textContent)), `${where}: "answered by Council"`);

  // 7. explicit "Send to lane": posts into the chat too, and leaves the chat AI alone
  await page.click(".ac-btn");
  await page.waitForSelector(".ui-sheet .ac-row");
  await page.click('.ui-sheet .ac-row[data-choice="local"]');
  await page.waitForTimeout(400);
  await page.fill(".as-input", "to a lane");
  await page.click(".as-lane");
  await page.waitForSelector(".ui-sheet .ac-row");
  const laneRows = await page.evaluate(() => [...document.querySelectorAll(".ui-sheet .ac-row")].map((r) => r.innerText.split("\n")[0]));
  ok(laneRows.includes("Local deep thinking"), `${where}: Send to lane lists the lanes (${laneRows.join(",")})`);
  await page.locator(".ui-sheet .ac-row", { hasText: "Local deep thinking" }).click();
  await page.waitForFunction(() => /Lane local-reasoning answer/.test(document.querySelector("#view-assistant .as-list").innerText));
  ok(/Local/.test(await page.textContent(".ac-btn__name")), `${where}: chat AI unchanged by Send to lane`);
  ok(await visibleBad(page) === 0, `${where}: chat shows null/undefined`);
  await page.screenshot({ path: `${process.env.SHOTS || "/tmp"}/chat-ai-${width}.png` });
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  for (const w of [390, 320]) await run(browser, w);
  await browser.close();
  if (fail.length) { console.log([...new Set(fail)].join("\n")); process.exit(1); }
  console.log("chat AI ok");
})().catch((e) => { console.log(e); process.exit(2); });
