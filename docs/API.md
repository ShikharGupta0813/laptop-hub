# Laptop Hub API

All routes are on the hub's own origin. Everything except `/api/session`, `/api/login`
and the static app needs the `hub_session` cookie (HttpOnly, set by login). A request
without it gets `401 {"detail": "..."}`. Errors are `{"detail": "message"}` with a plain
sentence the UI can show as is.

## Session
- `GET /api/session` → `{authenticated: bool, version}`
- `POST /api/login` `{token}` → `{ok: true}` and the cookie · `401` wrong token · `429` locked out for a minute
- `POST /api/logout` → `{ok: true}`

## Status
- `GET /api/status` → `{hostname, platform, time, disk:{free_gb,total_gb}, cpu_percent|null,
  memory:{percent,total_gb}|null, battery:{percent,plugged}|null, ollama:{up,models:[...]},
  shells:[names], scripts:int, claude_ready:bool, voice_ready:bool, chat_model}`

## Files (read-only, inside shared folders)
- `GET /api/roots` → `[{name}]`
- `GET /api/files?root=&path=` → `{root, path, entries:[{name,is_dir,size,modified}]}` (folders first)
- `GET /api/file?root=&path=[&download=1]` → the file. Images and PDFs inline; text as `text/plain`; binary as a download.
- `GET /api/search?root=&q=[&path=]` → `{results:[{name,path,is_dir}]}` (by file name)
- `POST /api/upload` multipart `file` → `{saved_as, size}` (goes to the inbox folder)
- `path` is relative to the root, always with `/`. `403` if it escapes the root.

## Ask the model about a passage (server-sent events)
- `POST /api/ask` `{mode: explain|summarise|quiz|custom, text, question, provider: local|claude}`
- Response is `text/event-stream`. Read it with `fetch` + `getReader()`. Each event is
  `data: {"t": "chunk"}`, or `data: {"error": "message"}`, and the last is `data: [DONE]`.

## Voice
- `POST /api/transcribe` multipart `file` (audio, e.g. webm) → `{text}` · `503` if voice isn't set up

## Notes
- `GET /api/notes[?tag=&q=]` → `{notes:[{id,title,created,source,tags,tagged,preview}], tags:[{tag,count}]}` newest first
- `POST /api/notes` `{text, source?}` → full note. Title and tags are added a few seconds later by the local model: `tagged` turns `true` when that happened, so re-fetch to see them.
- `POST /api/notes/voice` multipart `file` (audio) → `{note, transcript}` (same async tagging)
- `GET /api/notes/{id}` → `{id,title,created,source,tags,tagged,preview,body}`
- `PATCH /api/notes/{id}` `{title?, tags?}` (`tags` as a list or comma text) → full note. After an edit the model never overwrites it.

## Quick buttons
- `GET /api/scripts` → `[{name,label}]`
- `POST /api/run/{name}` → `{exit, output}`

## Assistant (talk to the laptop by voice or text)
- `POST /api/assistant/message` `{session_id, text, provider: claude|local, auto_approve}` → `text/event-stream`.
  Each event is `data: <json>` with a `type`:
  - `{"type":"text","t":"..."}` a piece of the assistant's reply (append them)
  - `{"type":"tool_start","id","name","input":{...},"needs_approval":bool}` a tool is about to run
  - `{"type":"approval_needed","id","name","input"}` the stream **pauses** until the user answers
  - `{"type":"tool_result","id","ok":bool,"output":"...","denied"?:bool}`
  - `{"type":"file_ref","root","path","name"}` a file the phone can open in the reader
  - `{"type":"error","message"}`
  - `{"type":"done"}` always the last event
- `POST /api/assistant/approve` `{session_id, call_id, approve: bool}` → `{ok:true}` · `404` if nothing is waiting
- `POST /api/assistant/reset` `{session_id}` → `{ok:true}` forgets the conversation

## Terminal (WebSocket)
- `GET /ws/term?shell=<name>&rows=&cols=` (same origin; the cookie authenticates it).
  Shell names come from `/api/status` → `shells`.
- Client → server: JSON text `{"type":"input","data":"typed text"}` and `{"type":"resize","rows":n,"cols":n}`.
- Server → client: **binary** frames of terminal output; write them to xterm.js as `Uint8Array`.
  If the shell can't start the server sends one text frame `{"error":"..."}` then closes.
- Close codes: 4401 not logged in, 4403 wrong origin, 4400 bad request.

## Development server
`python -m hub.devserver --port 8799` serves everything above with fake models and temporary
sample folders (roots `docs`, `notes`, `inbox`), login token `devtoken`, and a fake
assistant that sends a list_dir tool and, when the message contains "run", an approval.
