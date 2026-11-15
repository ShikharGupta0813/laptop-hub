# Study library (`/api/learn/library`)

Material for the Learn tab: uploaded from a phone or browser, picked from the hub's shared folders, or pasted.
Every route needs the login cookie (and `X-Hub: 1` on writes). Code: `hub/studylib.py`, UI `js/studylib.js`.

## Getting material in
- `POST /upload` multipart, one file per request (`file`, optional `title`, `tags` comma separated) → `{doc}`. The client
  sends files one after another, so each has its own progress (XHR) and a failed one is retried alone. Types: `pdf docx pptx epub
  txt md markdown rst org csv log html htm` and images `png jpg jpeg webp gif bmp`. Limits: 100 MB per file, 15 MB per image.
  Errors: `415` unsupported type (names the type), `413` too big (says the size and the limit), `422` empty.
- `POST /import` `{root, paths:[...], tags?}` (up to 200 files, individual files only, no folders) → `{results:[{path, ok, doc|error, status}], added}`.
  Reads files in place on the laptop; nothing is uploaded or copied. Uses the Files tab's rules: `files.resolve` against
  `roots`, so `..`, links that leave the root (`403`) and unknown roots (`404`, whole request) are refused. One bad file
  does not stop the rest.
- `POST /import-path` `{absolute_path, confirmed_outside?, tags?}` → `{doc, inside_shared_folder}`. A path inside a shared
  folder is read like `/import`; any other path is `403` unless `confirmed_outside` is true, which only the assistant tool sends after
  the owner approved its card. Credential folders (`~/.ssh`, `.aws`, `.claude`, `.gnupg`, `.kube`, `.azure`, `gcloud`) and
  secret-looking names are always refused.
- `POST /paste` `{title?, text}` or `{title?, url}` (public http/https only, same safe fetcher as Learn).
- The same file added twice (same name and size, or same path and modified time) returns the existing document with `duplicate: true`.

## Reading and indexing
Each document is read by a worker thread (2 at a time), so the hub never blocks. PDFs are read page by page (up to 3000
pages), slides are pages, EPUB chapters are pages, Word and text files are cut into virtual pages of about 3000 characters.
Then the pages are cut into ~1000-character chunks and, if the Ollama `embed_model` answers, embedded
(`search: "embeddings"`); otherwise `search: "keywords"` (BM25). Images are read by Tesseract (`pytesseract`) or the first
vision provider of Settings > Vision providers (that sends the image to that provider); with neither, the file fails with a plain message.
Scanned PDFs are not OCR'd: add their pages as images.

`status`: `queued` → `indexing` (`progress` 0..1) → `ready`, or `error` (with `error` text) / `cancelled`. A hub restart during a job marks it `error`.

## Managing
- `GET ?q=` → `{docs:[{id,title,filename,ext,source,size,pages,unit,chars,chunks,search,tags,subject,added,status,progress,error}], subjects, busy, limits}`
- `GET /{id}` → `{doc, excerpt}` · `GET /{id}/text?page_from&page_to&max_chars` → `{title, text, chars}` (used by English "Add material")
- `PATCH /{id}` `{title?, tags?, subject?}` rename and tag · `DELETE /{id}`
- `POST /{id}/reindex` re-chunks and re-embeds the stored pages · `POST /{id}/cancel` stops a running read (`409` if none).

## Using it as a source
Everywhere Learn takes a source (`/api/learn/source/preview`, `/chat`, `/quiz`, `/api/cards/.../generate`, roadmaps):
`{"type": "library", "ids": ["..."], "page_from": 3, "page_to": 12}`. The range works with one document. Text over the
model's limit is sampled evenly across the pages. Chat searches the whole chosen documents for the best chunks each turn.
Flashcard generation also takes `difficulty` (`easy|medium|hard`); duplicate fronts are skipped as before.
`POST /api/kb/ask` takes `library: [ids]` to answer from only those documents (sources are `library:<id>`, "title, p. N").

## Assistant tools (voice and text)
- `study_library_list {query?}`: read-only list (marked untrusted: titles can come from web pages).
- `study_import_path {path, tags?}`: adds one laptop file. No approval inside a shared folder; the owner is asked for any other path.
Audit events: `study_add`, `study_import`, `study_delete` (counts only, no file names or text).
