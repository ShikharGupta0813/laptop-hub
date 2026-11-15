# Notes: edit and delete

Notes are plain `.md` files in the notes folder (`notes_dir`). Create, list, get and the voice route are in `docs/API.md`.

- `PATCH /api/notes/{id}` `{body?, title?, tags?}` -> full note. `body` replaces the note's words (at most 20000 characters, not empty, no NUL characters; `400` / `413` otherwise). A body-only edit leaves title, tags and the `tagged` flag as they were. Audit event: `note_edit` (id and which fields changed, never the text).
- `DELETE /api/notes/{id}` -> `{ok, trash_id}`. The file is moved (never hard-deleted) to `<data_dir>/trash/notes/` with the same layout the Files tab uses, so it shows in `GET /api/fileops/trash` and comes back with `POST /api/fileops/trash/restore {id}` (needs `notes` in `writable_roots`). Audit event: `note_delete`.
- An id is letters, digits, `_` and `-` only; anything else is `400`. An unknown id is `404`. Login and the `X-Hub` header are required as for every other route. The screen asks for confirmation before it sends the delete.
