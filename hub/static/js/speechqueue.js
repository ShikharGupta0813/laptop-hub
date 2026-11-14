// The playback queue behind streamed speech (pure: no DOM, no imports, node tests run it with fakes).
//
//   const q = createSpeechQueue({ synth, play, playBrowser, stop, timeoutMs: 3000, onItem, onActive });
//   q.push("A sentence.", meta)   // starts making the audio NOW (so it is ready when the one before ends)
//   q.cancel()                    // barge-in: queue emptied, requests aborted, playback stopped at once
//   await q.idle()                // resolves when nothing is queued or playing
//
// synth(text, meta, signal) -> {kind:"audio", data} | {kind:"browser"} | {kind:"silent"} (or rejects: the phone voice is used).
// Each sentence gets timeoutMs to be made by the hub; after that the phone's own voice says it (play order is kept).
// play(data, item) and playBrowser(text, meta, item) resolve when the sound ends (or after stop() was called).
export function createSpeechQueue({ synth, play, playBrowser, stop = () => {}, timeoutMs = 3000, setTimer = setTimeout, clearTimer = clearTimeout,
  onItem = () => {}, onActive = () => {} } = {}) {
  let gen = 0, items = [], pumping = false, active = false;
  const waiters = [];
  const flush = () => { if (!items.length && !pumping) waiters.splice(0).forEach((w) => w()); };

  function make(item) {
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    item.ctl = ctl;
    item.result = new Promise((resolve) => {
      let settled = false, timer = null;
      const done = (r) => { if (!settled) { settled = true; clearTimer(timer); resolve(r); } };
      timer = setTimer(() => { try { ctl?.abort(); } catch (_) { /* ignore */ } done({ kind: "browser", why: "timeout" }); }, timeoutMs);
      let p;
      try { p = Promise.resolve(synth(item.text, item.meta, ctl?.signal)); } catch (e) { p = Promise.reject(e); }
      p.then((r) => done(r && r.kind ? r : { kind: "browser" }), () => done({ kind: "browser", why: "error" }));
    });
  }

  async function pump() {
    if (pumping) return;
    pumping = true;
    const mine = gen;
    try {
      while (items.length && mine === gen) {
        const item = items[0];
        const r = await item.result;
        if (mine !== gen) break;
        if (!active) { active = true; onActive(true); }
        onItem(item, r);
        try {
          if (r.kind === "audio") await play(r.data, item);
          else if (r.kind === "browser") await playBrowser(item.text, item.meta, item);
        } catch (_) { /* a failed sound must not block the ones after it */ }
        if (mine !== gen) break;
        items.shift();
        item.finish();
      }
    } finally {
      if (mine === gen) {                         // after cancel() a newer pump owns the state
        pumping = false;
        if (!items.length && active) { active = false; onActive(false); }
        flush();
      }
    }
  }

  return {
    push(text, meta = {}) {
      const t = String(text || "").trim();
      if (!t) return Promise.resolve();
      let finish; const done = new Promise((r) => { finish = r; });
      const item = { text: t, meta, finish, done, result: null, ctl: null };
      items.push(item);
      make(item);
      pump();
      return done;
    },
    cancel() {
      gen += 1;
      const gone = items; items = [];
      for (const it of gone) { try { it.ctl?.abort(); } catch (_) { /* ignore */ } it.finish(); }
      try { stop(); } catch (_) { /* ignore */ }
      if (active) { active = false; onActive(false); }
      pumping = false;
      flush();
    },
    idle() { return items.length || pumping ? new Promise((r) => waiters.push(r)) : Promise.resolve(); },
    get pending() { return items.length; },
    get speaking() { return active; },
  };
}
