# Workspaces API (`/api/workspaces`)

One-tap "get the laptop ready" launchers and a quick launcher for the whole laptop. A workspace is an ordered list of
typed steps. Every route needs the normal login. Errors are `{"detail": "<one plain sentence>"}`.
Stored in `<data dir>/workspaces.json` as `{"workspaces": [...], "recents": {...}}` (an older plain list is still read).
Needs `"workspaces"` in `FEATURE_MODULES` in `server.py`.

Workspace: `{id, name, icon, steps, arm_minutes, open_in, created, launching, builtin?}` plus read-only legacy fields
derived from the steps for older phones: `url` (first url step), `pre_steps` (script steps), `wait_seconds` (first wait).
`arm_minutes` 1-15 (default 5) is only a suggestion for the phone's Enable control prompt.

## Steps (max 12, run in order)
| type | fields | rule |
|---|---|---|
| `script` | `name` | a quick button in `scripts` in hub.json; runs through `POST /api/run/{name}` (exit code 0 = success) |
| `url` | `url` | http/https with a host, no spaces; opens through `laptop/run` `open_url` |
| `app` | `id` (`name` is filled in by the hub) | an id from `GET /apps` only; see below |
| `folder` / `file` | `root`, `path` | inside a configured root via `files.resolve` (traversal -> 403), must exist and be the right kind; a file may not be a program (`.exe`, `.lnk`...); opens through `laptop/run` `open_path` with the resolved absolute path |
| `wait` | `seconds` 0-120 | all waits in one workspace add up to at most 300 s |

Extra fields on a step are refused (400), so a step can never carry a path or command to run. More than 12 steps: 422.
Send `steps` on create/update. The older body (`url`, `pre_steps`, `wait_seconds`) still works and is converted
(scripts, then a wait only if there are scripts and an address and the wait is above 0, then the url step); do not send both
shapes together (400), and old fields are refused on a workspace that has app/folder/file steps (400). Stored old records
are migrated on read (nothing is rewritten until you save). `GET /templates` items also carry `steps`.

## Routes
- `GET /` -> `{workspaces: [workspace]}`. The first item is always the built-in **Whole laptop** tile
  (`id: "whole-laptop"`, `builtin: true`, `steps: []`, never stored, cannot be edited or deleted: 400).
- `POST /` `{name, steps?, icon?, arm_minutes?}` (or the old fields) -> workspace. 400: no name, a bad step (sentence says which),
  nothing to do (no steps), more than 30 workspaces. 422: numbers out of range / more than 12 steps.
- `GET /{id}` · `PUT /{id}` · `DELETE /{id}`. 404 unknown; 409 while it is launching.
- `GET /templates` -> `{templates: [{key, name, icon, description, url, pre_steps, steps, wait_seconds, arm_minutes, missing_scripts}]}`.
- `GET /search?q=` -> `{results: [{title, snippet, kind: "workspace", open: {view: "screen", section: "workspaces", id}}]}` (max 20).
- `GET /apps?q=` -> `{apps: [{id, name, source, count}], supported}`. Programs the launcher may start: the two Windows Start Menu
  Programs folders (`%ProgramData%` and `%APPDATA%` `\Microsoft\Windows\Start Menu\Programs`, `.lnk`/`.appref-ms` only, up to
  5 levels deep) plus `apps` from hub.json (`source: "hub.json"`, id `cfg:<name>`). `id` is a stable 12-hex hash of the shortcut
  path (`start_menu`). De-duplicated by name (case-insensitive; hub.json wins, then all-users, then the user folder), hidden files and
  junk entries (names containing uninstall, readme, release notes, licence, website, documentation, user guide, changelog...)
  are left out, sorted by name, max 300 after the optional `q` filter. `count` = times launched (recents). Empty list and
  `supported: false` away from Windows (without hub.json apps).
- `GET /recents?limit=12` -> `{recents: [{id, name, source, count, last}]}` most used first, only apps that still exist.
- `POST /quick` `{type: "app"|"url"|"folder"|"file", id|url|root+path, confirm: true}` -> the launch result below (one step, no
  workspace saved). 409 without `confirm: true`; 400 for any step the rules above refuse.

### App launching
An `app` step holds only an id. At save time and again at launch the id is looked up in a fresh discovery; an id that is not in it
(including any path, `..`, or a shortcut that was deleted) is refused with 400 and nothing starts. A Start Menu app is started with
`os.startfile` on the discovered shortcut (the equivalent of double-clicking it; `open_path` refuses `.lnk` on purpose, so this
module has its own tiny injectable layer). A hub.json app goes through `POST /api/laptop/run` `open_app` `{name}`.
Launching an app increments its count in `recents`.

### `POST /{id}/launch`  `{"confirm": true, "skip_steps": false}`
Without `confirm: true`: **409** (nothing runs). `skip_steps: true` ("Open anyway") skips script and wait steps and still opens
the rest. A second launch of the same workspace while one runs: **409**. Everything is re-checked at launch (hub.json, the
Start Menu or the folders may have changed): an unknown button, app, bad URL or escaping path -> **400/403** before anything runs.
Steps run in order; the first failure stops the launch (`stopped: true`). Never builds shell strings and **never arms remote control**
(the phone asks the owner via `/api/screen/arm`). `whole-laptop` has no steps: it only returns the screen status.
```json
{"ok": true, "steps": [{"type": "script", "name": "warp_connect", "label": "Connect Cloudflare", "ok": true, "message": "Done."}],
 "opened": true, "open_message": "Opened on the laptop.", "stopped": false, "screen_ready": true, "arm_minutes": 5, "workspace": {}}
```
`opened` = at least one url/app/folder/file step succeeded. `screen_ready` = `GET /api/screen/status` says `available`.
Audit: `workspace_launch` (name, URL host of the first url step, step count), one `workspace_step` per step (`kind` plus script/app
name, URL **host** only, or the root name; never a URL path, file path or shortcut path); `workspace_create/update/delete`.

## CLI
`python -m hub add-script NAME --label "Label" -- PROGRAM ARG...` writes `scripts[NAME] = {label, argv}` into hub.json
(NAME `^[a-z0-9_-]{1,40}$`; argv a non-empty list). Buttons are hand-edited only by design: there is no API for it.
