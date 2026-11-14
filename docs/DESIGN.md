# REDLINE design system (Laptop Hub): ARC-Glass

The app is branded **REDLINE** (tagline: *YOUR LAPTOP. FROM YOUR POCKET.*). `Laptop Hub` stays only in code identifiers; never show it in the UI.
The look is **ARC-Glass**, the owner-approved mix of two concepts: **ARC HUD** (identity: near-black space, arc-cyan and plasma-amber, thin letter-spaced
HUD labels, fine telemetry ticks, Orbitron display type) and **ORBIT GLASS** (surfaces: frosted glass panels over a deep navy-violet aurora, large radii,
soft depth). Crimson was rejected: red exists only as a muted `--c-bad` for real errors and destructive actions. Read "Identity" below before you style anything.

Read this before you restyle a screen. Pair it with `docs/UI.md`, which covers the module contract and screen rules.
There is no build step and there are no dependencies. Everything lives in these files:

| File | What it holds |
|---|---|
| `static/css/design.css` | Tokens, the 2 themes (arc, paper), 5 accents, base elements, every `ui-` component, identity motifs (window chrome, kv, eyebrow), the **ARC-Glass layer at the end** (glass surfaces, HUD brackets, persona cores, `@supports` fallback), the reading-font list, legacy aliases |
| `static/css/shell.css` | Login, glass top bar, floating glass dock, the fixed aurora (`.sh::before`), view transitions, search overlay |
| `static/css/wheel.css`, `static/js/wheel.js` | The floating assistant wheel (orb + quarter-circle fan of 5 shortcuts) |
| `static/css/home.css` | The Home screen (`#view-home`): greeting, the reactor (conic status ring + 6 glass nodes), telemetry window, cards |
| `static/js/ui.js` | Component helpers (sheet, toast, confirm, skeleton, …) and the theme API |
| `static/js/icons.js` | 130 inline SVG icons (24px grid, 1.8 stroke, round caps), drawn for this app |
| `static/js/boot.js` | Sets the saved theme and accent before first paint (and maps old saved values). It is its own file because the CSP has no `unsafe-inline` for scripts. |
| `static/js/main.js` | View registry, routing and history, `ctx` |
| `static/js/omnibox.js` | Global search overlay |
| `static/vendor/fonts/jetbrains-mono-var-latin.woff2` | JetBrains Mono variable, latin subset, 40 KB (OFL, `LICENSE-jetbrains-mono.txt`). **The UI font, everywhere.** |
| `static/vendor/fonts/orbitron-var-latin.woff2` | Orbitron variable, latin subset, 12 KB (OFL, `LICENSE-orbitron.txt`). **Display type only**: the wordmark, top-bar title, voice-mode title, persona names, big numerals. Vendored because the CSP allows only same-origin fonts. |
| `static/vendor/fonts/inter-var-latin.woff2` | Inter variable font, latin subset, 48 KB (OFL, licence next to it). Only for long reading blocks (see Type). |
| `static/icons/icon.svg`, `icon-maskable.svg`, `icon-*.png` | The cyan `R▮` monogram on #05070C (the PNGs were rasterised from the SVGs). `mobile/` has its own icon script and was not touched. |

## Identity (read this first)
- **Space and aurora**: the world is `#05070c`. A fixed backdrop (`--c-bg-glow`, painted by `.sh::before`) layers a cyan haze top-left, a violet haze right, an amber whisper bottom-left and a
  navy-violet glow at the foot, over a 28px grid at 2% opacity. Glass panels float over it, so never give a view its own opaque page background.
- **Colour**: `--jarvis #5ee7ff` (arc-cyan, also the default accent) and `--friday #ffb02e` (plasma-amber) are **fixed persona colours** that ignore the accent picker.
  Semantic: ok `--c-good #4ff0c8` (cyan-green), warn `--c-warn #ffb02e`, error `--c-bad #ee7b83` (muted red, errors and destructive actions only), info `--c-info` = cyan.
  Text `--c-text #e6f2f8`, `--c-text-2 #9db1c4`, `--c-text-3 #8496ab` (AA), `--c-mute` decoration only. Screens use tokens, never hex.
- **Glass** (the surfaces from Orbit Glass): `--c-surface-1` is a translucent navy (`rgb(17 26 46 / .56)`), `-2` raised, `-3` tracks and wells. Cards, windows, inset lists, notices, the dock, the top bar,
  sheets, modals and toasts add `backdrop-filter: var(--glass-filter)` (blur 18px + saturate), a 1px `--c-line` (white 8%) border and `--shadow-2`. Sheets, modals, toasts and the dock use the stronger
  `--c-glass-strong`/`--blur-lg` so text over scrolling content stays crisp. Inputs are sunken wells (`--c-input`). **Fallbacks**: `@supports not (backdrop-filter…)` and
  `prefers-reduced-transparency` swap the translucent fills for solid ones, so text never sits on raw aurora.
- **Shape**: large radii: `--r-xs 5 · sm 8 · md 12 (buttons, inputs) · lg 16 · xl 18 (cards) · 2xl 22 (sheets, modals, login)`; chips, badges, the search field and the composer are pill-shaped; avatars, the nodes,
  the orb and the top-bar icon buttons are circles.
- **HUD details (subtle, not everywhere)**: thin uppercase letter-spaced labels (`.18em`: `ui-eyebrow`, `ui-caps`, `ui-label`, `ui-section-title`, the dock, window bars), a 14px cyan rule before section titles,
  and **corner brackets**: 1px L-shaped ticks at the top-left and bottom-right of key panels. They are automatic on `.ui-window--accent`, `.ui-card--accent`, `.ui-card--gradient` and opt-in with `.ui-hud`
  (colour from `--_hud`). The Home ring also carries a ring of 1px ticks, and the top bar has a cyan hairline fading out from the left.
- **Type**: **Orbitron** (`--font-display`, class `.ui-display`) for display only: the wordmark, the top-bar title, the voice title, persona names, big numerals (ring values, `ui-ring__big`, `ui-eyebrow > b`).
  **JetBrains Mono** (`--font-ui`) for everything else, and **Inter** (`--font-read`) only for long reading blocks (list at the end of design.css), exactly as before. Do not put Orbitron on long text or on
  free-form headings that can wrap (it is wide).
- **Persona cores**: Jarvis = cyan radial orb, Friday = amber orb. `.ui-core[data-persona][data-state]` (idle, listening, thinking, speaking) is the shared orb; `.ui-wave` is a 7 to 9 bar waveform line that only
  moves while an ancestor has `data-state="speaking"`. They pulse softly while listening and speaking and respect reduced motion. Voice mode (`.vm[data-persona]`) and the Talk header (`.as-core`) use them, and
  each assistant bubble carries `data-persona` (a mini orb and a coloured edge). The persona follows the same routing the server uses (a leading "Friday," / "Jarvis,"; voice mode via `splitName`).
  `.ui-persona-friday` / `.ui-persona-jarvis` re-point the accent tokens inside a card (the Home Friday card is amber this way).
- **Home = the reactor**: a greeting, then a circular status ring (conic-gradient gauge, battery % or hub LIVE/DOWN in the middle, label under) with six glass nodes on a hexagon around it
  (Talk, Friday, Today, Autopilot, Files, Terminal), all positioned in % of the stage so it holds from 300 to 430px; under 340px it folds to a ring on top and a 2x3 grid. Below: a telemetry window
  (`key: value` rows), the jump bar, and the existing cards as glass windows. The ring turns amber on low battery and muted red when the laptop is offline.
- **Assistant wheel** (`js/wheel.js`): a 52px cyan orb bottom-right inside `#views` (so it sits above the dock and never covers it) that fans out a quarter circle of five 58px glass nodes
  (Talk, Apps, Files, Terminal, Screen). Closes on outside tap, Esc, a choice or a hash change; arrow keys move between nodes. Default ON; Settings > Appearance > Shortcuts toggles it
  (`localStorage hub.wheel`, event `hub:wheel`). It steps aside (`#app[data-view]` in wheel.css) on Talk, Terminal, Screen, Files, Notes, Automations and Friday, which own the bottom-right corner,
  and while voice mode is open. Every `.view` has 56px of extra bottom padding so the orb never hides the last row.
- **Terminal stays terminal**: `ui-term`, `pre` and xterm keep a near-black `--c-term-bg`, high-contrast mono text, no glass, in every theme (Paper included).
- **Buttons**: primary = a lit cyan (accent) fill with a soft glow; secondary/ghost are white-6% glass; danger is the muted red. Full Control chip: an amber warning glass pill.
- **Motifs kept**: numbered section labels (`ui-eyebrow`), YAML rows (`ui-kv`), `tag: value` (`ui-tag`), `$` prompts, `ui-term`, the block caret, status LEDs, dashed rules, scanlines and the read-head on login.
  The window-chrome dots are now amber / cyan / mint (`--tl-dots`, no red). `ui-glitch` is a soft glow flicker, not a displacement.
- **Animations** (all off under reduced motion): the ring core breathes, the orb and the listening core pulse, the waveform moves while speaking, nodes rise in turn, LEDs pulse, the caret blinks.
- **Voice**: short, lowercase-friendly developer copy (`$ auth --token`, `arc // standing by`, `# pull down to refresh`). Taglines: `YOUR LAPTOP. FROM YOUR POCKET.`, `BUILD. DEBUG. OPTIMIZE. SHIP.`
- **Icons for AI actions**: `prompt`, `crosshair`, `terminal`, `jarvis`. `sparkles` resolves to the prompt-with-cursor glyph; fine for the persona entry points that already use it.

## Themes and accents
`<html data-theme="arc|paper" data-accent="cyan|amber|green|violet|white">`. **Arc** (dark glass) is the default; **Paper** is the light theme (cool white glass over a pale sky-blue wash, all text tokens meet AA,
accents darkened for contrast, the terminal block stays dark). Settings > Appearance (`ui.appearancePanel()`) lists Arc, Paper and Auto, plus the accent swatches and the wheel switch.
The accents: cyan #5ee7ff (default, JARVIS), amber #ffb02e (FRIDAY), green #4ff0a8, violet #a78bfa, white #e6eef5. There is no crimson.
The saved choice (localStorage `hub.theme`) may also be `auto`; boot.js and ui.js resolve it to Arc or Paper, so `data-theme` never says `auto`.
**Compatibility**: old saved values are mapped on load (boot.js and ui.js, and the CSS also treats `redline`/`slate` as arc): redline/slate/midnight/aurora become arc, daylight becomes paper,
crimson/rose/blue/teal become cyan, indigo becomes violet; an unknown theme becomes arc and an unknown accent becomes cyan. `index.html` ships `data-theme="arc" data-accent="cyan"`.
To change it from code, call `ui.setTheme(id)` / `ui.setAccent(id)`. Each fires a `hub:theme` event and updates `<meta theme-color>` (#05070c, or #e9f0f7 on Paper).
`ui.resolvedTheme()` returns arc|paper and `ui.isLightTheme()` is true on Paper. Accent source colours are `--a --a-strong --a-deep --a-text --a-ink --a-tint`, set per accent and again for Paper;
everything else derives from them.

## Tokens (always use these; never write hex values in screen CSS)
- **Colour**: `--c-bg`, `--c-bg-2` (bars: window chrome, scrolled top bar), `--c-bg-deep` (darkest, for insets), `--c-surface-1/2/3` (higher numbers are raised or pressed), `--c-tint-1/2/3` (accent-tinted surfaces),
  `--c-line` (hairline), `--c-line-strong` (input and card border), `--c-text`, `--c-text-2` (secondary), `--c-text-3` (faint meta and placeholders; still AA), `--c-mute` (decoration only, not text),
  `--c-input` (sunken input wells), `--jarvis|friday` (+ `-text`, `-ink`: fixed persona colours),
  `--c-accent`, `--c-accent-strong`, `--c-accent-text` (accent used as text on a surface), `--c-accent-ink` (text on an accent fill), `--c-accent-soft` (tint),
  `--c-accent-line` (accent hairline), `--c-accent-grad` (a flat two-stop accent, not a rainbow), `--c-card-grad` (deep accent-tinted surface, white text reads),
  `--c-good|warn|bad|info` and `-soft` for each, `--c-term-bg|text|dim` (terminal blocks, dark in every theme), `--c-grid`, `--c-bg-glow` (the aurora), `--c-glass`/`--c-glass-strong` (translucent glass fills), `--c-scrim`,
  `--tl-red|yellow|green` (now amber / cyan / mint) and `--tl-dots` (the three window dots).
- **Type**: `--font-ui` (= `--font-mono`, the default on `html`), `--font-read` (Inter; `--font-sans` is an alias of it); a mono-tuned scale
  `--fs-2xs 10.5 · xs 11.5 · sm 13 · md 14 · base 15 · lg 16.5 · xl 19 · 2xl 23 · 3xl 28 · display 34`; weights `--fw-regular|medium|semibold|bold` (400/500/600/700);
  `--tracking-tight|snug|caps (.14em)|mono`. Inputs stay at 16px so phones don't zoom in. Use `.ui-num` for tabular figures; add a selector to the reading list in design.css for a new long-text block.
- **Space**: `--sp-0..10` = 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64. The page gutter is `--gutter` (16).
- **Radius**: `--r-xs 5 · sm 8 · md 12 · lg 16 · xl 18 (cards) · 2xl 22 (sheets) · full` (pills, dots, orbs).
- **Elevation**: glass + hairline + soft shadow. `--shadow-1` is none, `-2` a top highlight and a soft drop (cards), `-3` floating (toasts, dock, FAB), `-4` sheets; `--shadow-accent` and `--glow` are the coloured light.
- **Blur**: `--blur-sm|md|lg` are 10 / 18 / 28px and `--glass-filter` is the standard panel filter (blur + saturate). **Motion**: `--dur-instant|fast|base|slow|xslow`, `--ease-out` (default), `--ease-in-out`, `--ease-spring` (mild).
  Reduced motion turns off every animation, including the caret blink, LED blink and login scan. It follows the OS setting, or `html[data-motion=reduce]` (localStorage `hub.motion=reduce`).
- **Layout**: `--tap 44`, `--appbar-h 56` (44 in phone landscape), `--nav-h 64`, `--content-max 680`. In phone landscape (`max-height: 500px`) the dock hides its labels and the top bar slims.
- **Legacy aliases** still work and follow the theme: `--bg --surface --surface-2 --line --text --muted --accent --accent-ink --good --warn --bad --radius --tab-h`, and `--hue` (set per accent).
  Legacy classes are restyled: `.btn` (`.primary .danger .ghost`), `.chip` (`.on`, `aria-pressed`), `.card`, `.row`, `.wrap`, `.grow`, `.muted`, `.error`, `.sheet`, `.markdown`, `.sr-only`.

## Components (CSS classes)
| Component | Classes |
|---|---|
| Button | `ui-btn` + `--primary` `--secondary` `--tonal` `--ghost` `--danger` `--danger-soft`; sizes `--sm` (36px, with a 44px hit area) `--lg` (52px); `--icon` (square, 44px) `--block` `--mono` (uppercase mono label); `.is-loading` shows a spinner |
| Input | Plain `input/select/textarea` are already styled. Also `ui-field` > `ui-label` + input + `ui-hint` (`.is-error`), `ui-input-wrap` (leading `ui-icon`, trailing `ui-btn--icon`), `ui-input--lg`, `aria-invalid="true"` |
| Segmented control | `ui-seg` (`--block`) with `ui-seg__thumb`; buttons use `aria-checked` and are 44px tall (the visible plate is inset 3px). Use `segmented()` |
| Chip / badge / count | `ui-chip` (`.is-on`, `--static`; a 36px plate with a 44px hit area from `::after`), `ui-badge` (`--accent --good --warn --bad --solid`), `ui-count` (accent number on an icon), `ui-dot` (`--good --warn --bad --live`) |
| Card | `ui-card` + `--flat` `--elevated` `--glass` (stronger glass) `--gradient` (deep accent hero with the tick and a grid) `--accent` (accent hairline + tint) `--interactive` `--pad-lg` `--pad-0`; `ui-card__head`, `ui-card__title`, `ui-card__sub`; section heading `ui-section-head` > `ui-section-title` + `ui-link` |
| Icon tile | `ui-tile` (`--sm --lg`) + `data-tone="accent|good|warn|bad|info|violet|pink|teal|orange|gray"` |
| List | `ui-list` (`--inset` grouped card, `--icons` indents dividers) > `ui-row` (`--interactive`) with `ui-row__body` > `ui-row__title` + `ui-row__sub`, `ui-row__meta`, `ui-row__chev` |
| Avatar | `ui-avatar` (`--sm --lg`), hue from the name |
| Progress | `ui-ring` (SVG), `ui-bar` (`--v` 0..1, `--indeterminate`), `ui-spinner` |
| Skeleton / empty / notice | `ui-skel` (`--line --title --circle`), `ui-empty` (`--compact`), `ui-notice` (`--warn --bad --good`) |
| Sheet / modal | `ui-scrim`, `ui-sheet` (`__grab __head __title __body __foot`), `ui-modal` |
| Toast | `ui-toasts` > `ui-toast` (`--good --warn --bad`) |
| Identity | `ui-eyebrow` (`<b>01</b>Label` or `data-n="01"`, `--rule` adds a hairline), `ui-kv` > `ui-kv__row` > `ui-kv__k` + `ui-kv__v` (`--ruled`, `data-tone` on a value),
`ui-led` (`--accent --good --warn --bad --info --off`, `--live` blinks), `ui-term` > `ui-term__bar` + `ui-term__body` > `ui-term__line` (`--dim`) with `ui-term__prompt`, `ui-cursor`,
`ui-hairline` (`--strong --accent --top --bottom`), `ui-wordmark` (REDLINE + dot, Orbitron), `ui-tagline`, `ui-glitch` (a soft glow flicker on any element), `ui-core` + `ui-wave` (persona orb and waveform), `ui-hud` (corner brackets), `ui-persona-friday|jarvis` (re-point the accent), `ui-display` (Orbitron),
`ui-window` > `ui-window__bar` (> `ui-window__dots` (three HUD dots) + `ui-window__title` `~/path` + `ui-window__meta`) + `ui-window__body` (`--accent` adds the tick), `ui-dots` (the bare dots),
`ui-tag` > `b` + `span` (`tag: value`), `ui-prompt` (`$ cmd`), `ui-dash` (dashed rule) |
| Tabs / switch / tooltip / FAB | `ui-tabs` (`aria-selected`), `ui-switch` (checkbox inside), `[data-tip="Text"]` (only on devices that hover), `ui-fab` (`--round`; absolute, so put it in a positioned parent) |
| Layout and motion helpers | `ui-stack` (`--lg`), `ui-hstack`, `ui-hscroll` (edge-to-edge horizontal scroller), `ui-truncate`, `ui-clamp-2`, `ui-caps`, `ui-grad-text`, `ui-enter` (`--i` for delay), `ui-stagger` (children rise in turn), `ui-shake` |

## ui.js helpers
```js
import * as ui from "./ui.js";          // or use ctx.ui inside a screen module
ui.icon("bell", {size: 20, label: "Notifications"})  // SVG; aria-hidden unless it has a label
ui.button({label: "Send", icon: "send", variant: "primary", size: "lg", block: true, onClick})
await ui.busy(btn, api(...))            // spinner on the button while the promise runs
ui.toast("Saved", {kind: "good"})       // kinds: good|warn|bad|info; {action:{label,onClick}}; a number is ms (old API)
const s = ui.sheet({title, content: node, actions: [{label, variant, icon, onClick(s) /* return false keeps it open */}], onClose})
s.body.replaceChildren(...); s.setActions([...]); s.close();
if (await ui.confirm({title, message, confirmLabel, danger: true})) { ... }   // use instead of window.confirm
ui.skeleton("list"|"lines"|"card"|"tiles", n); ui.emptyState({icon, title, body, action:{label, icon, onClick}, compact})
ui.notice(text, {tone: "warn"}); ui.listRow({icon, tone, title, sub, meta, chevron, onClick}); ui.tile(icon, tone, size)
ui.badge(text, tone); ui.avatar(name); ui.switchControl({checked, onChange, label})
ui.eyebrow("01", "Whoami", {rule: true}); ui.kv([["status", "online", "good"], ["uptime", "4d"]], {ruled: true}); ui.led("good", {live: true, label: "Online"})
ui.term({title: "~/hub", meta: ui.led("good"), lines: [{text: "# comment", dim: true}, "$ hub status", "ok"], cursor: true}); ui.cursor()
const seg = ui.segmented({options: [{value, label, icon?}], value, onChange, label, block}); seg.value = "x";
const ring = ui.ringProgress({value: .6, size: 64, tone: "good", big: "6", small: "days", label}); ring.set(.8, "8");
const bar = ui.progressBar(.4); bar.set(null /* indeterminate */);
ui.haptic("light|medium|heavy|select|success|warn|error")  // vibrates; localStorage hub.haptics=off disables it
ui.pullToRefresh(scroller, async () => {...}); ui.relTime(date); ui.clockTime(date)
ui.pushLayer(close) -> release   // makes any overlay of yours close on Android Back (sheet, confirm and search already do)
```
Sheets, the confirm dialog and the search overlay trap focus, close on Escape, on the scrim and on Android Back. Sheets can also be swiped down.

## Shell, registry and ctx
- **Bottom bar**: Home, Talk (`assistant`), Work, Learn, Apps. Every other screen sets `under: <tab>`, which keeps that tab highlighted and shows a Back chevron.
- **Add a screen** with one line in `VIEWS` in `main.js`: `{ id, label, icon, path: "./x.js", under: "apps" }`. Modules load lazily with `import()` the first time the screen opens.
  An id that isn't registered still opens `js/<id>.js` if that file exists. A missing module shows "X isn't installed yet". A module that throws shows "X couldn't start" with Try again.
  Already registered, waiting for their modules: `notifications`, `settings` (until it ships, the gear opens an Appearance page), `reminders`, `inbox`, `screen`, `focus`, `automations`, `kb`.
- **Routing**: the URL hash is `#/<view>[/<section>]`. Each navigation pushes a history entry, so Android Back works through screens, sheets and search.
- **ctx** (passed to `init`): `api, ui, toast, speak, secure, views, goto(view), open(view, section?, {id}?), openFile(root, path), back(), search(q?), setBadge(viewId, n), refreshNotifications(), current()`.
  Module return object (every key is optional): `show, hide, openSection(name), openItem(id, section), refresh` (called when the active tab is tapped again).
  `ctx.open(view, section, {id})` → `openSection(section)` then `openItem(id, section)`. Search results and Home rows open items this way, so please implement `openItem`.
- **Badges**: `ctx.setBadge("work", 3)` puts a count on any element with `data-badge-for="work"` (the nav tabs and the bell). Unread notifications are polled from
  `GET /api/notifications/unread-count` every 30 s. On a 404 it backs off to every 5 minutes.
- **Search** (`omnibox.js`): screens and quick actions match on the phone straight away. Then it calls `GET /api/search?q=` (falling back to `/api/search/all`) and groups results by `kind`.
  To give a new kind an icon and a label, add it to `KIND` in `omnibox.js`.
- **Quick actions** (Home and search): the `QUICK_ACTIONS` array in `home.js`. Each one is one line, with either `open: [view, section]` or `run(ctx)`.
- Keep `#tabs` as the bottom bar's id: assistant.js and studychat.js measure it to fit the on-screen keyboard.

## How to build a screen
1. Scope every rule under `#view-<id>` in `css/<id>.css`. Use only tokens. Don't use `!important`, except the existing `[hidden]`.
2. Structure: an optional sticky header with a `ui-seg` for sections, then `ui-stack ui-stack--lg` with cards inside. Content is centred at `max-width: var(--content-max)`.
   The top bar already shows the screen title, so don't repeat a big H1 unless it is a hero.
3. Every request goes skeleton → content, and also handles empty and error. Empty uses `ui.emptyState` with a helpful action. Errors use `ui.notice(..., {tone:"bad"})` inline, or `ctx.toast(msg, {kind:"bad"})`.
4. Tap targets are at least 44px (`--tap`). There is no horizontal page scroll (use `ui-hscroll` for rows of chips or tiles). Inputs are 16px. Text from the network is set with `el()`/`textContent` only.
5. Risky actions use `ui.confirm({danger:true})`. A slow action uses `ui.busy(button, promise)`. When it finishes, call `ui.toast(..., {kind:"good"})` and `ui.haptic("success")`.
6. Motion: add `ui-stagger` on first render (remove it after about 1 s so re-renders don't animate again). Use `--ease-out` and `--dur-base`. Keep it subtle.
7. Use icons from `ui.icon` only. Don't paste SVG paths. The older `icon()` in `laptop.js` can stay until that screen is restyled.
8. Check the screen at 390×844 in Arc and Paper, and at 360×640 and 844×390. Look for contrast, overflow (mono is wide), the keyboard-open state, and
   whether it still reads as ARC-Glass: glass panels over the aurora, thin HUD labels, no crimson, the wheel orb not hiding the last row.

## Restyle checklist, screen by screen
- **Talk** (`assistant`): drop the `.as-head` background (the top bar is chrome now). The model switch becomes `ui-seg`. Bubbles use surface-2 for the assistant and an accent-soft tint for the user.
  Tool cards use `ui-card--flat` with a `ui-badge` for status. The composer is a flat input bar with a `ui-btn--icon` mic and a `ui-btn--primary` send. Approvals use `ui.confirm` or an inline card with danger and secondary buttons.
- **Work**: sections in a `ui-seg--block`. Tasks become a `ui-list--inset` with a round check, due date as `ui-badge`, and overdue in `--bad`. Calendar becomes a timeline (see `.hm-event` in home.css) with a Join `ui-btn--tonal ui-btn--sm`.
  Email: compose in `ui-field`s, the AI draft in a `ui-card--glass`, Send needs confirm. Implement `openItem(gid)`.
- **Learn**: `.learn-seg` becomes `ui-seg--block`. Chat and Quiz share the source picker, which moves to a `ui.sheet`. Quiz shows one question per card, answers as large `ui-btn--secondary` that turn good/bad, and a result `ringProgress`. English shows progress as rings plus a `ui-bar` trend.
- **Apps**: category chips become `ui-chip`. Entries are `ui-card--interactive` with a `ui-tile` (one tone per category). The Jarvis hero is `ui-card--gradient`. The runner form uses `ui-field`, and results go in a sheet.
- **Notes**: list as `ui-list--inset` rows (a mic tile for voice notes), `#tag` chips, and a record `ui-fab` with a pulsing ring. The note reader uses `.markdown` in a card. Support `openSection("new"|"voice")` and `openItem(id)`.
- **Files**: breadcrumbs as chips, rows with folder or file tiles, the reader in a card, the answer as `ui.sheet`.
- **Terminal**: keep the xterm colours, move the toolbar into `ui-seg` and chips, and use `ui-btn--icon` for the special keys.
- **Hub (Laptop)**: stats as `ringProgress` (CPU, memory, disk, battery), quick buttons as a tile grid, upload with `ui-bar`, log out as `ui-btn--danger-soft`.
- **Notifications / Settings / Reminders / Focus / Screen / Inbox / Automations / KB** (new screens): register them (most already are), make them grouped `ui-list--inset` settings rows with `ui-switch`,
  and put `ui.appearancePanel()` at the top of Settings. Focus should be a big `ringProgress` timer.

## Icon names
home talk work learn apps search bell settings back chevron-left chevron-right chevron-down chevron-up arrow-left arrow-right arrow-up arrow-down
arrow-up-right external menu more more-vertical close x plus minus check check-circle circle filter sliders refresh info alert alert-circle help wifi-off
wifi battery battery-charging cpu activity power laptop monitor cast terminal code lock unlock key shield fingerprint camera screenshot image video mic
mic-off waveform headphones play pause stop volume volume-off calendar clock timer alarm mail send inbox list-checks list task note note-plus pen file
folder book book-open bookmark tag clipboard copy trash edit download upload link share globe layers hash at user users smile heart star award sparkles
wand bot jarvis zap flame target focus trend chart language quiz sun moon palette eye eye-off cloud history logout command grid phone coffee
prompt crosshair redline (the monogram). `sparkles` is drawn as a prompt with a cursor block.

## Service worker
`sw.js` precaches the shell (it is network-first, with the cache as fallback; current `VERSION` is `v17-arc-glass-2026.11`, which added `wheel.js`, `wheel.css` and the Orbitron font). **Bump `VERSION`** whenever you add or rename a shell file, and add new core files to `SHELL`.
Lazily loaded screens are cached the first time they load. If one file fails to download, the install still completes.

## List editor (js/listeditor.js)
The one look for every list the owner edits in Settings: an inset `ui-list` of rows (tile, title, sub line, optional meta, an on/off `ui-switch`, edit and delete icon buttons), then a footer with `Add` (tonal) and `Reset to defaults` (ghost, confirmed). Forms open as a `ui.sheet` (`formSheet`). Classes: `.le`, `.le-row`, `.le-actions`, `.le-foot`, `.le-meta`, `.le-accent[data-accent]`, in `settings.css`. Switched-off rows are struck through. Use it for any new list instead of writing a custom editor.
