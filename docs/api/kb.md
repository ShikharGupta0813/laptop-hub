# Ask my files (`/api/kb`)

A local knowledge base over the owner's documents. Every route needs the login cookie (and `X-Hub: 1` on POST).

## What is indexed
- Folders: the root names in `kb_roots` in hub.json (empty = every root in `roots`). Unknown names are ignored.
- Types: `.md .txt .rst .py .js .ts .json .csv .html .pdf .docx`.
- Never read: files over 5 MB, hidden files and folders (leading `.`), `node_modules`, `__pycache__`, `venv`,
  `site-packages`, `*.pem *.key *.p12 *.pfx *.kdbx`, `hub.json`, and names that look like credentials
  (`secrets`, `credentials`, `password(s)`, `token(s)`, `api_key`, `private_key`, `id_rsa`/`id_ed25519`...).
  Symlinks that leave the shared folder are skipped. The hub's own data folder is skipped.
- Chunks: about 800 characters with 100 overlap. Markdown (and Word headings) split at headings; each chunk
  carries its heading path.
- Index file: `<data_dir>/kb.sqlite` (SQLite FTS5 with BM25, plus optional float32 vectors).
- Incremental: a file is read again only when its (mtime, size) changed; deleted files and folders that are no
  longer shared leave the index. A shared folder that is missing on disk (unplugged drive) keeps its entries.
- Rescan: at hub start and every 10 minutes in a worker thread, or on demand with `POST /reindex`.

## Two ways to search
1. Keywords: FTS5 + BM25, always on. Query words are OR-ed (common words dropped); file names are searchable.
2. Meaning: when Ollama has `embed_model` (`POST {ollama_host}/api/embed`), each chunk gets a vector and the
   query is compared by cosine similarity (numpy if installed, pure Python otherwise; matches under 0.4 are
   dropped). Both rankings are merged with reciprocal rank fusion.
If the embedding model or Ollama is missing the hub silently uses keywords only. `GET /status` says so
(`embed_available: false`, `embed_note`, `mode: "keywords"`). After a failure it asks Ollama again at most once a minute;
the next rescan fills in the missing vectors. Changing `embed_model` discards old vectors.

## Routes

### `GET /status`
```json
{"files": 12, "chunks": 80, "embedded": 80, "indexing": false, "last_indexed": "2026-11-04T10:00:00Z",
 "embed_available": true, "embed_model": "nomic-embed-text", "mode": "hybrid", "embed_note": "",
 "roots": ["docs", "notes"], "progress": {"done": 12, "total": 12}, "skipped": 0, "last_error": ""}
```
`last_indexed` is null until the first scan. `mode` is `"hybrid"` or `"keywords"`.

### `POST /reindex`
No body. Starts a rescan in the background: `{"started": true, "indexing": true, "message": "..."}`.
If one is already running: `{"started": false, "indexing": true, ...}`. Poll `/status` until `indexing` is false.

### `GET /search?q=&limit=`
`limit` 1-50 (default 10); `q` up to 500 characters (422 beyond). An empty `q` returns empty lists. At most 3 hits per file.
```json
{"hits": [{"path": "docs:notes/wifi.md", "root": "docs", "rel": "notes/wifi.md", "snippet": "...router password...",
           "score": 0.83, "chunk_id": 41, "line": 7, "heading": "Wifi"}],
 "results": [{"title": "wifi.md", "snippet": "...", "kind": "file",
              "open": {"view": "files", "id": "docs:notes/wifi.md"}}]}
```
- `path` is `"<root>:<rel>"`, the same id the Files view opens. No absolute paths leave the hub.
- `score` is 0..1 (best possible rank in every active layer = 1).
- `line` is the 1-based first line of the chunk; null for PDF and Word.
- `results` is the global-search shape (one per file, max 20, `kind: "file"`).

### `POST /ask`
Body: `{"question": "...", "model": "local"|"claude", "limit": 6}` (`model` defaults to `local`, `limit` 1-10,
question up to 2000 characters). Answers as a Server-Sent Events stream, each event `data: <json>`, ending with `data: [DONE]`:
```
data: {"type":"sources","sources":[{"n":1,"path":"docs:wifi.md","root":"docs","rel":"wifi.md","snippet":"...","score":0.8,"chunk_id":41,"line":7,"heading":"Wifi"}]}
data: {"type":"token","t":"The router ","text":"The router "}      # t and text are identical
data: {"type":"token","t":"is in the hall [1].","text":"is in the hall [1]."}
data: {"type":"done"}
data: [DONE]
```
- Source numbers `n` match the `[1]`, `[2]` citations the model is asked to write.
- No results: `sources` is `[]`, one `token` says "I couldn't find anything about that in your files." (or, if nothing is
  indexed yet, to tap Re-scan), then `done`. The model is not called.
- A model problem arrives as `{"type":"error","error":"<one sentence>"}` followed by `[DONE]`.
- The model is told the file text is DATA inside `<sources>` tags, never instructions, to answer only from it, and to
  cite `[n]`. Any `<sources>` or `</sources>` inside a file is neutralised so a file cannot close the block.
- With `model: "claude"` the retrieved excerpts (not whole files) go to Anthropic. Audited as `kb_ask` (model,
  number of sources, question length; never content).
- Errors: 400 empty/too long question, 422 bad `model`.

### `GET /file-context?path=<root>:<rel>`
The indexed chunks of one file (max 60): `{"path","root","rel","chunks":[{"chunk_id","heading","line","text"}]}`.
Errors: 400 malformed path, 403 outside the shared folder (`..`, links out), a hidden/credential-looking file, or a folder not in
`kb_roots`; 404 unknown folder or a file that is not indexed yet. It never reads the live file.

## Audit events
`kb_reindex`, `kb_ask`.

## Test hook
`hub.kb.EMBEDDER = fn(texts, config) -> list[vector]` replaces the Ollama call (raise `hub.kb.EmbedError` for "unavailable").
`hub.kb.DEV_FAKES` is a canned answer for `hub.devserver`.
