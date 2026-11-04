# Fileops API (`/api/fileops`)

The write side of the Files tab. All routes need a login cookie; POSTs also need the `X-Hub: 1` header.
Errors are `{"detail": "<one plain sentence>"}`.

## Rules
- **Writable roots only.** Changes (mkdir, write, rename, move, copy destination, delete, restore, upload) are
  allowed only in roots listed in `writable_roots` in `hub.json` (default `notes`, `inbox`). Otherwise:
  `403 "That folder is read-only. Add it to writable_roots in hub.json to change files there."`
  An unknown root is `404`. Reading, copying *from*, zipping, searching and stats work on every root.
- **Safe paths.** Paths go through `hub.files.resolve`: `..` and links that point outside the root are `403`
  ("That path is outside the shared folder."). A NUL in a path is `400`. rename, move and delete act on a link
  itself, never on its target. The shared folder itself cannot be renamed, moved or deleted (`400`).
- **The hub's own files** (data folder, audit log, `hub.json`) can't be changed, moved or deleted (`403`) and
  are skipped by copy and zip.
- **Names.** New names (mkdir, new files, rename) must be a single clean name: no `/` or `\`, no NUL or control
  characters, none of `< > : " | ? *`, no trailing dot or space, not `.`/`..`, not a Windows device name (CON,
  PRN, AUX, NUL, COM1-9, LPT1-9, with or without extension), max 200 characters. Otherwise `400`.
  Uploads are *sanitized* instead of refused (see upload).
- **Confirm.** delete and trash/empty need `"confirm": true` (a real boolean), else `409`.
- **Audit.** Every change writes `fileops_<mkdir|write|rename|move|copy|delete|restore|trash_empty|upload>`
  with `root` and the relative `path` (never file content).
- Changes run one at a time inside the hub.

## Routes

### `GET /roots`
`[{"name": "notes", "writable": true}, ...]`

### `GET /read?root=&path=`
`{root, path, content, etag, mtime, size, writable}`. `etag` is the SHA-256 (hex) of the file's bytes, `mtime`
is epoch seconds. `413` over 2 MB, `415` if not UTF-8 text.

### `POST /mkdir` `{root, path}`
Creates the folder and missing parents. `{ok, path, created}`; an existing folder is `created: false`.
`409` if a file has that name.

### `POST /write` `{root, path, content, etag?, create_only?}`
Text, UTF-8, at most 2 MB (`413`). The parent folder must exist (`404`). Atomic (temp file + `os.replace`).
- New file: send no `etag`.
- Existing file: send the `etag` from `/read`. A stale etag (or an etag for a file that has since been deleted)
  is `409 "This file changed on the laptop since you opened it"`. **Writing over an existing file without an
  etag is also a `409`**, so nothing is ever overwritten silently.
- `create_only: true`: `409` if the name is taken.
Returns `{ok, path, etag, mtime, size}` (use the new `etag` for the next save).

### `POST /rename` `{root, path, new_name}`
`new_name` is a name only. `409` if taken. `{ok, path}`.

### `POST /move` `{root, path, dest_root, dest_dir}`
Both roots must be writable. `dest_dir` must exist. `409` if the name is taken there, `400` for moving a folder
into itself. `{ok, root, path}`.

### `POST /copy` `{root, path, dest_root, dest_dir}`
The source may be in a read-only root; the destination must be writable. A taken name gets ` (2)`, ` (3)`...
Folders are copied recursively; links inside are skipped, a link that leaves the root is `403`; over 500 MB is
`413`. `{ok, root, path}`.

### `POST /delete` `{root, path, confirm: true}`
Moves to the trash (never a hard delete): `<data>/trash/<root>/<timestamp>-<name>` plus a sidecar
`<data>/trash/<root>/.meta/<that name>.json` with the original root, path, name, size, time.
`{ok, id}`.

### `GET /trash`
`{"items": [{id, root, path, name, is_dir, size, deleted_at}]}` newest first (`path` is the original location).

### `POST /trash/restore` `{id}`
Back to the original place (missing folders are recreated; a taken name gets ` (2)`). The original root must
be writable. Unknown id `404`. `{ok, root, path}`.

### `POST /trash/empty` `{confirm: true, root?}`
Really deletes trash contents (only inside the trash folder; `root` limits it to one root). `{ok, removed}`.

### `POST /upload` (multipart)
Fields: `file` (one file), `root` (default `inbox`), `dir` (default the top of the root, must exist).
Streamed to disk in 64 KB chunks and cut off at **100 MB** (`413`, nothing left behind). The filename is
sanitized: only the last path piece is kept (`../../x` -> `x`, `/etc/passwd` -> `passwd`), NUL and control
characters removed, forbidden characters become `_`, trailing dots/spaces dropped, Windows device names get a
`_` prefix (`CON` -> `_CON`), empty -> `upload`. A taken name becomes `name (2).ext`; nothing is overwritten.
`{ok, root, path, name, size}`.

### `GET /zip?root=&path=`
Streams `<name>.zip` of a folder (or one file); `path` empty zips the whole root. Works on read-only roots.
Refused up front: more than 500 MB (`413`), more than 100,000 entries (`413`), a link that points outside the
root (`403`). Links that stay inside are not followed or included.

### `GET /recent?limit=20`
`{"files": [{root, path, name, size, modified, writable}]}`, newest first, `limit` 1-100. Skips hidden files
and folders, `node_modules`, `__pycache__`, `.git`, and links that leave a root.

### `GET /search?q=`
Filename search over all roots, max 20:
`{"results": [{"title": name, "snippet": "<root>/<rel>", "kind": "file", "open": {"view": "files", "id": "<root>:<rel>"}}]}`

### `GET /stats?root=`
`{"roots": [{name, writable, files, folders, bytes, truncated, trash_bytes, disk: {total, used, free}}]}`
for the given root, or every root when `root` is left out. `truncated` is true if the folder was too big to
count completely.
