# Workspaces API (`/api/workspaces`)

A one-tap "get the laptop ready" launcher: run some of your own quick buttons (`scripts` in hub.json), wait, open a
web address in the laptop's default browser. Every route needs the normal login. Errors are `{"detail": "<one plain sentence>"}`.
Stored in `<data dir>/workspaces.json`. Needs `"workspaces"` in `FEATURE_MODULES` in `server.py`.

Workspace: `{id, name, icon, url, pre_steps: [{type: "script", name}], wait_seconds, open_in: "default_browser", arm_minutes, created, launching}`.
`url` is `""` or http/https with a host. `pre_steps` are names of existing quick buttons (`GET /api/scripts` lists them;
at most 5). `wait_seconds` 0-120. `arm_minutes` 1-15 (default 5) is only a suggestion for the phone's Enable control prompt.

## Routes
- `GET /` -> `{workspaces: [workspace]}`
- `POST /` `{name, url?, icon?, pre_steps?, wait_seconds?, arm_minutes?}` -> workspace. 400: no name, bad/non-http(s) URL, unknown script name
  (the sentence shows the `add-script` command), nothing to do (needs a URL or a button), more than 30 workspaces. 422: numbers out of range.
- `GET /{id}` · `PUT /{id}` (any create field; `"url": ""` clears it) · `DELETE /{id}`. 404 unknown; 409 while it is launching.
- `GET /templates` -> `{templates: [{key, name, icon, description, url, pre_steps, wait_seconds, arm_minutes, missing_scripts}]}`.
  `NiFi (company VPN)`: `https://nifi.onestack.in`, `[warp_connect]`, wait 8. `missing_scripts` lists buttons not in hub.json yet.
- `GET /search?q=` -> `{results: [{title, snippet, kind: "workspace", open: {view: "screen", section: "workspaces", id}}]}` (max 20).
  Add `("workspaces", ("/api/workspaces/search",), "workspace")` to `SOURCES` in `search.py` to include it in global search.

### `POST /{id}/launch`  `{"confirm": true, "skip_steps": false}`
Without `confirm: true`: **409** (nothing runs). `skip_steps: true` ("Open anyway") skips the buttons and only opens the address.
A second launch of the same workspace while one runs: **409**. Everything is re-checked at launch (hub.json may have changed):
unknown button or non-http(s) URL -> **400** before anything runs.

Runs `pre_steps` in order through `POST /api/run/{name}` (success = exit code 0; the first failure stops the launch), waits
`wait_seconds` (only if a button ran), then opens the URL through `POST /api/laptop/run` `open_url` with `confirm: true`.
Never builds shell strings and **never arms remote control** (the phone asks the owner via `/api/screen/arm`).
```json
{"ok": true, "steps": [{"name": "warp_connect", "label": "Connect Cloudflare", "ok": true, "message": "Done."}],
 "opened": true, "open_message": "Opened on the laptop.", "stopped": false, "screen_ready": true, "arm_minutes": 5, "workspace": {}}
```
`screen_ready` = `GET /api/screen/status` says `available`. Audit `workspace_launch` (name, URL host only, step count);
`workspace_create/update/delete` (name, host).

## CLI
`python -m hub add-script NAME --label "Label" -- PROGRAM ARG...` writes `scripts[NAME] = {label, argv}` into hub.json
(NAME `^[a-z0-9_-]{1,40}$`; argv a non-empty list). Buttons are hand-edited only by design: there is no API for it.
