# Settings API (`hub/settings.py`)

All routes need the login cookie. Errors are `{"detail": "<one plain sentence>"}`.

## What can and cannot be changed

`PATCH /api/settings` accepts only these keys: `chat_model`, `assistant_model`, `claude_model`, `embed_model`,
`whisper_model`, `kb_roots`, `writable_roots`, `ntfy_server`, `ntfy_topic`.
Everything else is refused with `400`. The security-relevant options (`token_hash`, `session_secret`,
`allowed_hosts`, `allowed_origins`, `scripts`, `apps`, `shells`, `jarvis_dir`) are never returned and never
accepted: they are edited by hand in hub.json. `GET` lists them by name in `locked` so the UI can say so.

## Routes

- `GET /api/settings` →
  `{chat_model, assistant_model, claude_model, whisper_model, embed_model, kb_roots:[name], writable_roots:[name],
  roots:[{name, exists}], ntfy_server, ntfy_topic_set:bool, jarvis_dir_set:bool, shells:[name], apps:[name],
  scripts:[name], has_claude_key:bool, ollama:{reachable, models:[name]}, version, data_dir_exists:bool, platform,
  config_persistable:bool, locked:[key], editable:[key]}`.
  Folder paths, the Claude key, the ntfy topic and the token are never included.
- `PATCH /api/settings` `{<allowed keys>..., confirm?}` → the same body as `GET` plus `persisted:bool` (and `note` when
  the change only lasts until restart).
  - Models: `^[A-Za-z0-9._:/-]{1,80}$`, no `..`, not starting with `/` or `-`.
  - `kb_roots`, `writable_roots`: lists of existing root names (duplicates dropped). **Adding** a root to
    `writable_roots` needs `confirm: true` (exactly the JSON boolean), else `409`; the folder must exist. Removing needs nothing.
  - `ntfy_server`: `https://` URL with a host, no user/password, query or fragment (trailing `/` removed).
  - `ntfy_topic`: 8-64 of `[A-Za-z0-9_-]`, or `""` to switch push off.
  - `400` for a locked/unknown key, bad value, or empty body. A request with any bad key changes nothing.
  - Audited as `settings_change {changes:{key:{old,new}}, persisted}`; `ntfy_topic` is logged as `{changed:true}` only.
  - The change is applied to the running hub at once. It is also written to hub.json by loading the file, changing just those keys and saving,
    so hand edits and the token are untouched. The file is found through `ctx.config_path` (if the server sets it), else
    `HUB_CONFIG` / `~/.laptop-hub/hub.json` **only if that file has this hub's token hash**. Otherwise `persisted:false`.
- `GET /api/settings/models` → `{reachable, models:[{name,size_mb,modified,params}], configured:{chat_model:{name,installed}, embed_model:{...}}, error?}`
  (Ollama asked with a 3 s timeout; down gives `reachable:false` and a sentence, not an error status).
- `POST /api/settings/models/pull` `{name, confirm:true}` → SSE `data: {"status":"pulling ...","percent":42.5}` ... `data: {"error":"..."}` ... `data: [DONE]`.
  `400` bad name (`^[A-Za-z0-9._:/-]{1,80}$`), `409` without `confirm`. Audited `model_pull` / `model_pull_done {ok}`.
- `POST /api/settings/models/test` `{model}` → `{ok:true, model, latency_ms, reply}` or `{ok:false, model, error}` (always 200 for model problems; `400` bad name).
- `GET /api/settings/diagnostics` → `[{name, ok:bool, detail, fix, level:"ok"|"warn"|"fail"}]`, the checks of `python -m hub doctor` plus data/notes/audit folders,
  disk space, the Jarvis folder and the settings file. `warn` = optional thing missing.
- `GET /api/settings/audit?limit=100&q=&event=&before=` → `{rows:[{time,event,...fields,pos}], count, next_before|null, truncated:bool}`
  newest first, max 500 rows. `q` = case-insensitive text match on the whole line, `event` = exact event name,
  `before` = a `next_before` cursor (a number) or a time string (rows older than it). Bad lines are skipped. The file is read from the end in 64 KB
  blocks, at most 16 MB back (`truncated:true` if that limit stopped the scan). String values are cut at 500 characters.
- `GET /api/settings/audit/events` → `{events:[sorted names], counts:{name:n}}` (from the last 2 MB).
- `POST /api/settings/backup` `{confirm:true}` → `{name, size, files, excluded}`. `409` without `confirm` or while another backup runs; `413` over 2 GB.
  Zip layout: `data/...` (the data folder), `notes/...`, `audit/audit.log`, `MANIFEST.json`. Not included: `hub.json`, the config file wherever it is,
  any file up to 2 MB containing the token hash or session secret, symlinks, temp files, the `backups` folder. Stored in `<data_dir>/backups/hub-backup-YYYYMMDD-HHMMSS[-n].zip`; the newest 5 are kept.
  Audited `backup_create {name,size,files}`.
- `GET /api/settings/backups` → `{backups:[{name,size,created}]}` newest first.
- `GET /api/settings/backups/{name}` → the zip as a download. `name` must match `hub-backup-\d{8}-\d{6}(-\d+)?.zip` (`400` otherwise), `404` if missing or a symlink. Audited `backup_download`.
- `GET /api/settings/about` → `{version, uptime_s, python, platform}`.

Not here: `POST /logout-all` (the server does it).
