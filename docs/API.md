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

---

# Work, Learn and English (added in round 2)

Every route below needs the login cookie. A feature that isn't set up returns
`503 {"detail": "<what to do>"}`. Every AI route takes `provider: "local" | "claude"` (default
`local`); Claude needs `ANTHROPIC_API_KEY` on the laptop or returns `503` saying so.
Each backend module also exports `DEV_FAKES: dict[str, str]`: a unique substring of each of its
system prompts mapped to a valid canned model reply, which `hub.devserver` uses so the UI can be
developed without a real model.

## Work

- `GET /api/work/status` → `{asana:{configured,user|null}, calendar:{configured}, email:{configured,address|null}, chat:{configured,spaces:[name]}}`
- `GET /api/work/asana/tasks?filter=today|overdue|upcoming|all` → `{tasks:[{gid,name,due_on|null,project|null,completed,permalink_url}], counts:{overdue,today,upcoming}}` (my open tasks, soonest due first)
- `GET /api/work/asana/tasks/{gid}` → `{task:{gid,name,due_on,project,completed,permalink_url,notes,subtasks:[{gid,name,completed}],comments:[{author,text,created_at}]}}`
- `POST /api/work/asana/tasks/{gid}/comment` `{text}` → `{ok:true}` · `POST /api/work/asana/tasks/{gid}/complete` → `{ok:true}`
- `GET /api/work/calendar?day=YYYY-MM-DD&days=1` → `{meetings:[{title,start,end,all_day,link,platform}]}` (ISO times; `platform` is zoom|meet|teams|"")
- `GET /api/work/email/contacts` → `{contacts:[{name,address}]}`
- `POST /api/work/email/ai-draft` `{instruction, provider}` → `{to_hint, subject, body}` (AI writes a draft from "write a mail to Rahul about the demo")
- `POST /api/work/email/draft` `{to, subject, body}` → `{draft_id, to, subject, body}` (resolves a contact name or address; nothing is sent)
- `POST /api/work/email/send` `{draft_id}` → `{ok:true,to}` sends exactly the stored draft, once
- `GET /api/work/chat/spaces` → `{spaces:[name]}` · `POST /api/work/chat/send` `{space,text}` → `{ok:true}`

## Learn: sources, chat, quizzes

A **source** is one of: `{type:"note", id}`, `{type:"file", root, path}`, `{type:"text", text}`,
`{type:"url", url}` (public http/https only; private and loopback addresses are refused),
`{type:"topic", topic}` (the model's own knowledge).

- `GET /api/learn/topics` → `{topics:[{id,title}]}` suggested generic topics
- `POST /api/learn/source/preview` `{source}` → `{title, chars, excerpt}`
- `POST /api/learn/chat` `{session_id, source|null, message, provider}` → SSE `data: {"t":"..."}` … `data: [DONE]` (errors as `{"error":"..."}`); keeps history per `session_id`; `POST /api/learn/chat/reset` `{session_id}`
- `POST /api/learn/quiz` `{source, count:3..15, kinds:["mcq","short"], difficulty:"easy|medium|hard", provider}` → `{quiz_id, title, questions:[{id,kind:"mcq|short",question,choices|null,topic}]}` (answers stay on the server)
- `POST /api/learn/quiz/{quiz_id}/answer` `{question_id, answer, provider}` (mcq: choice index number; short: text) → `{correct:bool, score:0..1, correct_answer, explanation, feedback|null}`
- `POST /api/learn/quiz/{quiz_id}/finish` → `{quiz_id,title,score,total,percent,weak_topics:[..],missed:[{question,your_answer,correct_answer,explanation,topic}]}` and the result is saved
- `GET /api/learn/history` → `{quizzes:[{quiz_id,title,date,percent,source_label}], streak_days}`

## English coach

- `GET /api/english/scenarios` → `{scenarios:[{id,title,description}]}`
- `GET /api/english/overview` → `{streak_days, sessions_total, last_session|null, trend:[{date,wpm,fillers_per_100,overall}], vocab_due:int}`
- `POST /api/english/practice-prompt` `{scenario, provider}` → `{prompt}` (what to talk about)
- `POST /api/english/speak` multipart `file` (audio) + fields `scenario?`, `prompt?`, `provider?` → `{transcript, duration_s, words, wpm, fillers:{count,items:{um:3}}, long_pauses, low_confidence_words:[{word,prob}], feedback:{summary, grammar:[{said,better,why}], vocabulary:[{said,better}], rewrite, scores:{fluency,grammar,vocabulary,overall}, tips:[..]}}` (scores 1-10; saved to history; `low_confidence_words` is a hint about unclear pronunciation, not a grade)
- `POST /api/english/roleplay` `{session_id, scenario, message|null, provider}` → SSE: `{"t":".."}` pieces of the partner's reply, then `{"correction":{"said","better","why"}|null}`, then `[DONE]`; `message:null` makes the partner open the conversation; `POST /api/english/roleplay/reset` `{session_id}`
- `POST /api/english/correct` `{text, provider}` → `{corrected, changes:[{from,to,why}], tone_note}`
- `GET /api/english/vocab/today` → `{new_words:[{id,word,meaning,example,level}], due:[{id,word,meaning,example,level}]}`
- `POST /api/english/vocab/add` `{word, provider}` → `{id,word,meaning,example,level}`
- `POST /api/english/vocab/{id}/review` `{rating:"again|hard|good|easy"}` → `{next_due}`
