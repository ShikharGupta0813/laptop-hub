# Brief for every screen-building agent

Project: laptop-hub, a mobile-first PWA (vanilla ES modules, no build step) in this folder. Read `docs/DESIGN.md` (tokens, ui.js
helpers, how to build a screen, restyle checklist) and `docs/UI.md` first, then the `docs/api/<module>.md` files named in your task.
The look must feel like a premium modern consumer app (see the Home screen and screenshots from `js/home.js`), never a utility.

Rules
- Own ONLY the files named in your task. Never edit main.js, ui.js, icons.js, index.html, design.css, sw.js, server Python, or
  other agents' files. If you need an icon, use an existing name from DESIGN.md "Icon names".
- A new screen is `js/<id>.js` exporting `init(container, ctx)` returning `{show, hide, openSection, openItem, refresh}` (all optional)
  plus `css/<id>.css`. index.html does not list new CSS: the screen's `init` appends `<link rel="stylesheet" href="/css/<id>.css">`
  to document.head once. The view ids are already registered in `main.js`.
- Tokens only (no hex), CSS scoped under `#view-<id>`, 44px targets, 16px inputs, no horizontal scroll, reduced motion respected.
- Network text goes in via `el()`/`textContent` (markdown only through `renderMarkdown`). Every request has skeleton, empty and error
  states. Risky actions use `ui.confirm({danger:true})`. Non-GET requests need the `X-Hub: 1` header, which `api()` adds.
- Keep all existing behaviour when restyling an old screen: change presentation, not protocol.
- Verify with Playwright (require('playwright'), chromium.launch(), viewport 390x844, deviceScaleFactor 2) against
  `python -m hub.devserver --port <your port>` started in the background (token `devtoken`, demo data seeded, fake models); log in with
  the token, exercise every state, screenshot dark (Midnight) and Daylight themes (set localStorage `hub.theme`), READ the PNGs
  yourself and iterate until polished. Check there are no console errors or CSP violations. Kill your devserver at the end.
  Save screenshots under /tmp/claude-0/shots/<your-area>/.
- Do not git commit. Final report under 200 words: files, what works, what is unverified.

## If you are CONTINUING an interrupted run
The files named in your task may already exist (written by an agent that was stopped mid-way). Do not start over: read what is there,
list what the task asks for that is missing or broken, finish it, then verify in the browser as above (exercise every state and
action, not just the first screen). Keep what already works. Report what you added and what you verified.
