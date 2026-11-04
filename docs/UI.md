# Phone UI conventions

Read with `docs/API.md`. The app has no build step: ES modules in `hub/static/js`, one CSS file per screen in `hub/static/css`.

## Views and modules
Every screen is a module `js/<name>.js` exporting `init(container, ctx)` that returns an object, all optional:
`{ show(), hide(), openSection(name) }`. `main.js` mounts it into `#view-<name>`.

`ctx` (from `main.js`): `api`, `toast(msg)`, `speak(text)`, `secure`, `goto(view)`, `open(view, section?)`, `openFile(root, path)`.
`ctx.open("work", "email")` switches to Work and calls its `openSection("email")`. Learn routes `"quiz"` or `"english/speak"`.

Section names: work = `tasks|calendar|email|chat`; learn = `chat|quiz|english`; english = `speak|roleplay|correct|vocab|progress`.

## The Apps directory (`js/apps.js`)
A searchable directory of every feature. An entry is `{id, title, description, category, keywords, kind, target}`:
- `kind:"view"`: `target` is `{view, section?}`, opened with `ctx.open`.
- `kind:"action"`: `target` is a laptop action id; opened in the generic action runner (`js/laptop.js`).
- `kind:"jarvis"`: opens the Jarvis screen (`js/jarvis.js`).
Built-in entries are written in `apps.js`; action entries come from `GET /api/laptop/actions`. Favourites and recents live in localStorage.

## Rules (all screens)
Vanilla JS, no new libraries. `el()`/`textContent` for anything from files, models, tools or the laptop (markdown only via
`renderMarkdown`). 360-430 px wide, touch targets at least 44 px, no horizontal page scroll, inputs 16 px, dark and light via the CSS
variables in `style.css`, CSS scoped under `#view-<name>`. Every request has loading, empty and error states (`ctx.toast(error.message)`
or inline text). A risky action asks first (in-page panel, never `window.confirm`). Reduced motion respected.
