# REDLINE design system (Laptop Hub)

The app is branded **REDLINE** (tagline: *YOUR LAPTOP. FROM YOUR POCKET.*). `Laptop Hub` stays only in code identifiers.
The look is the owner's own identity: a developer's dark-crimson terminal, not a generic AI app. No violet/indigo gradients, no glass blur,
no pill-shaped everything, no sparkle icons. Read "Identity" below before you style anything.

Read this before you restyle a screen. Pair it with `docs/UI.md`, which covers the module contract and screen rules.
There is no build step and there are no dependencies. Everything lives in these files:

| File | What it holds |
|---|---|
| `static/css/design.css` | Tokens, the 3 themes, 5 accents, base elements, every `ui-` component, identity motifs, legacy aliases |
| `static/css/shell.css` | Login, top bar, bottom navigation, view transitions, search overlay |
| `static/css/home.css` | The Home screen (`#view-home`) |
| `static/js/ui.js` | Component helpers (sheet, toast, confirm, skeleton, …) and the theme API |
| `static/js/icons.js` | 130 inline SVG icons (24px grid, 1.8 stroke, round caps), drawn for this app |
| `static/js/boot.js` | Sets the saved theme and accent before first paint (and maps old saved values). It is its own file because the CSP has no `unsafe-inline` for scripts. |
| `static/js/main.js` | View registry, routing and history, `ctx` |
| `static/js/omnibox.js` | Global search overlay |
| `static/vendor/fonts/inter-var-latin.woff2` | Inter variable font, latin subset, 48 KB (OFL, licence next to it). Body text. |
| `static/vendor/fonts/jetbrains-mono-var-latin.woff2` | JetBrains Mono variable, latin subset, 40 KB (OFL, `LICENSE-jetbrains-mono.txt`). Labels, numbers, code, status. |
| `static/icons/icon.svg`, `mobile/scripts/make-icons.py` | The crimson `R▮` monogram on #05060A. `python scripts/make-icons.py --web` (from `mobile/`) redraws the Android icons, splash screens and the PWA PNGs. |

## Identity (read this first)
- **Palette**: backgrounds #05060A / #08090D / #0A0B10, surfaces #0E0F16, tinted surfaces #12070C #1A0A10 #2A0A14 (`--c-tint-1..3`),
  crimson #FF5368 (deep #FF2D4A, shadow #B0152C), amber #FFB454, cyan #3DE0FF, green #3DFF8A, text #F2F3F5, muted #8B929E. Screens use tokens, never these hex values.
- **Type**: Inter for reading. JetBrains Mono (`--font-mono`, `.ui-mono`) for eyebrows, labels, badges, tabs, numbers, code, status and the wordmark.
  Mono micro-labels are UPPERCASE with wide tracking (`.ui-caps`, `.ui-label`, `.ui-eyebrow`).
- **Shape**: flat surfaces, 1px hairlines (`--c-line`, `--c-line-strong`) instead of shadows, radii 3-10px (sheets 14px). Nothing is pill-shaped except dots and LEDs.
- **Accent, sparingly**: one accent per surface. The recurring "tick" is a short 2px accent rule on the top-left edge of a hero card, sheet, modal and the login window,
  plus the small square after the wordmark and top-bar title. `--glow` is for that tick, LEDs and the active nav rule only.
- **Motifs**: numbered section labels `01 / WHOAMI` (`ui-eyebrow`), YAML rows `key: value` (`ui-kv`), terminal blocks with a `$` prompt (`ui-term`),
  a blinking block caret (`ui-cursor`), status LEDs (`ui-led`), a faint 24px grid on the page background, scanlines and a sweeping crimson read-head on login.
- **Voice**: short, lowercase-friendly developer copy in mono (`$ auth --token`, `private link over tailscale`). Taglines: `YOUR LAPTOP. FROM YOUR POCKET.`, `BUILD. DEBUG. OPTIMIZE. SHIP.`
- The `sparkles` icon (used for every AI action) is drawn as a shell prompt with a lit cursor block. Prefer `prompt`, `crosshair` or `terminal` in new work.

## Themes and accents
`<html data-theme="redline|slate|paper" data-accent="crimson|amber|cyan|green|white">`. **Redline** (near-black + crimson) is the default,
**Slate** is neutral graphite whose signature accent is amber, **Paper** is a warm off-white light theme with crimson (all text tokens meet AA).
The saved choice (localStorage `hub.theme`) may also be `auto`; boot.js and ui.js resolve it to Redline or Paper, so `data-theme` never says `auto`.
Old saved values are mapped on load: midnight→redline, daylight→paper, aurora→slate; indigo/violet/rose→crimson, blue/teal→cyan.
With no saved accent, a theme uses its signature (Slate: amber, others: crimson); picking a theme in the panel moves the accent with it unless you chose your own.
To change it from code, call `ui.setTheme(id)` / `ui.setAccent(id)`. Each fires a `hub:theme` event and updates `<meta theme-color>`.
`ui.resolvedTheme()` returns redline|slate|paper and `ui.isLightTheme()` is true on Paper. `ui.appearancePanel()` returns the picker (theme cards plus accent swatches).
Accent source colours are `--a --a-strong --a-deep --a-text --a-ink --a-tint`, set per accent and again for Paper; everything else derives from them.

## Tokens (always use these; never write hex values in screen CSS)
- **Colour**: `--c-bg`, `--c-bg-deep` (darkest, for insets and terminals), `--c-surface-1/2/3` (higher numbers are raised or pressed), `--c-tint-1/2/3` (accent-tinted surfaces),
  `--c-line` (hairline), `--c-line-strong` (input and card border), `--c-text`, `--c-text-2` (secondary), `--c-text-3` (faint meta and placeholders; still AA),
  `--c-accent`, `--c-accent-strong`, `--c-accent-text` (accent used as text on a surface), `--c-accent-ink` (text on an accent fill), `--c-accent-soft` (tint),
  `--c-accent-line` (accent hairline), `--c-accent-grad` (a flat two-stop accent, not a rainbow), `--c-card-grad` (deep accent-tinted surface, white text reads),
  `--c-good|warn|bad|info` and `-soft` for each, `--c-term-bg|text|dim` (terminal blocks, dark in every theme), `--c-grid`, `--c-glass`/`--c-glass-strong` (now near-opaque), `--c-scrim`.
- **Type**: `--font-sans`, `--font-mono`; `--fs-2xs 11 · xs 12 · sm 13.5 · md 15 · base 16 · lg 18 · xl 21 · 2xl 26 · 3xl 32 · display 40`; weights `--fw-regular|medium|semibold|bold`;
  `--tracking-tight|snug|caps|mono`. Inputs stay at 16px so phones don't zoom in. Use `.ui-num` for tabular figures, `.ui-mono` for mono text.
- **Space**: `--sp-0..10` = 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64. The page gutter is `--gutter` (16).
- **Radius**: `--r-xs 3 · sm 5 · md 6 · lg 8 · xl 10 (cards) · 2xl 14 (sheets) · full` (dots only).
- **Elevation**: hairlines do the work. `--shadow-1` is none, `-2` a 1px edge, `-3` floating (toasts, FAB), `-4` sheets; `--shadow-accent` and `--glow` are the only coloured light.
- **Blur**: `--blur-sm|md|lg` are 0 (kept so old rules parse). **Motion**: `--dur-instant|fast|base|slow|xslow`, `--ease-out` (default), `--ease-in-out`, `--ease-spring` (mild).
  Reduced motion turns off every animation, including the caret blink, LED blink and login scan. It follows the OS setting, or `html[data-motion=reduce]` (localStorage `hub.motion=reduce`).
- **Layout**: `--tap 44`, `--appbar-h 56`, `--nav-h 64`, `--content-max 680`.
- **Legacy aliases** still work and follow the theme: `--bg --surface --surface-2 --line --text --muted --accent --accent-ink --good --warn --bad --radius --tab-h`, and `--hue` (set per accent).
  Legacy classes are restyled: `.btn` (`.primary .danger .ghost`), `.chip` (`.on`, `aria-pressed`), `.card`, `.row`, `.wrap`, `.grow`, `.muted`, `.error`, `.sheet`, `.markdown`, `.sr-only`.

## Components (CSS classes)
| Component | Classes |
|---|---|
| Button | `ui-btn` + `--primary` `--secondary` `--tonal` `--ghost` `--danger` `--danger-soft`; sizes `--sm` (36px, with a 44px hit area) `--lg` (52px); `--icon` (square, 44px) `--block` `--mono` (uppercase mono label); `.is-loading` shows a spinner |
| Input | Plain `input/select/textarea` are already styled. Also `ui-field` > `ui-label` + input + `ui-hint` (`.is-error`), `ui-input-wrap` (leading `ui-icon`, trailing `ui-btn--icon`), `ui-input--lg`, `aria-invalid="true"` |
| Segmented control | `ui-seg` (`--block`) with `ui-seg__thumb`; buttons use `aria-checked`. Use `segmented()` |
| Chip / badge / count | `ui-chip` (`.is-on`, `--static`), `ui-badge` (`--accent --good --warn --bad --solid`), `ui-count` (red number on an icon), `ui-dot` (`--good --warn --bad --live`) |
| Card | `ui-card` + `--flat` `--elevated` `--glass` (now solid) `--gradient` (deep accent hero with the tick and a grid) `--accent` (accent hairline + tint) `--interactive` `--pad-lg` `--pad-0`; `ui-card__head`, `ui-card__title`, `ui-card__sub`; section heading `ui-section-head` > `ui-section-title` + `ui-link` |
| Icon tile | `ui-tile` (`--sm --lg`) + `data-tone="accent|good|warn|bad|info|violet|pink|teal|orange|gray"` |
| List | `ui-list` (`--inset` grouped card, `--icons` indents dividers) > `ui-row` (`--interactive`) with `ui-row__body` > `ui-row__title` + `ui-row__sub`, `ui-row__meta`, `ui-row__chev` |
| Avatar | `ui-avatar` (`--sm --lg`), hue from the name |
| Progress | `ui-ring` (SVG), `ui-bar` (`--v` 0..1, `--indeterminate`), `ui-spinner` |
| Skeleton / empty / notice | `ui-skel` (`--line --title --circle`), `ui-empty` (`--compact`), `ui-notice` (`--warn --bad --good`) |
| Sheet / modal | `ui-scrim`, `ui-sheet` (`__grab __head __title __body __foot`), `ui-modal` |
| Toast | `ui-toasts` > `ui-toast` (`--good --warn --bad`) |
| Identity | `ui-eyebrow` (`<b>01</b>Label` or `data-n="01"`, `--rule` adds a hairline), `ui-kv` > `ui-kv__row` > `ui-kv__k` + `ui-kv__v` (`--ruled`, `data-tone` on a value),
`ui-led` (`--accent --good --warn --bad --info --off`, `--live` blinks), `ui-term` > `ui-term__bar` + `ui-term__body` > `ui-term__line` (`--dim`) with `ui-term__prompt`, `ui-cursor`,
`ui-hairline` (`--strong --accent --top --bottom`), `ui-wordmark` (REDLINE + tick), `ui-tagline` |
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
8. Check the screen at 390×844 in Redline, Slate and Paper. Look for contrast, overflow and the keyboard-open state.

## Restyle checklist, screen by screen
- **Talk** (`assistant`): drop the `.as-head` background (the top bar is chrome now). The model switch becomes `ui-seg`. Bubbles use surface-2 for the assistant and an accent-soft tint for the user.
  Tool cards use `ui-card--flat` with a `ui-badge` for status. The composer is a pill input with a `ui-btn--icon` mic and a `ui-btn--primary` send. Approvals use `ui.confirm` or an inline card with danger and secondary buttons.
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
`sw.js` precaches the shell (it is network-first, with the cache as fallback). **Bump `VERSION`** whenever you add or rename a shell file, and add new core files to `SHELL`.
Lazily loaded screens are cached the first time they load. If one file fails to download, the install still completes.
