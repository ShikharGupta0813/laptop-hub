# Chat history API

`hub/chatstore.py` (the SQLite file), wired into `AssistantService` in `hub/assistant.py`. No new HTTP route: the phone keeps one
conversation id in `localStorage` (`hub.assistant.session`, `js/assistant.js`) and sends it as `session_id` as before.

## What happens
- After a turn finishes (never while streaming) the owner's words and the assistant's final text are written to
  `<data folder>/chat_history.sqlite` in a background thread (`asyncio.to_thread`); the stream is not held up.
- The first message for a session id that is not in memory (after a restart, update or crash) loads its rows once, off the event
  loop, and rebuilds the in-memory history. The usual `fit_history` budget still applies at send time. History is context only:
  it never runs a tool and never skips an approval; Panic is checked before anything is loaded.
- `POST /api/assistant/reset` (the "new conversation" action) deletes the stored rows too, also when the hub restarted and the
  conversation exists only on disk. Same auth and `X-Hub` rules as before.

## What is stored (and not)
Stored per message: session id, provider key, role (`user` or `assistant`), text (cut at 4000 characters), time, and for the
assistant a short `used: tool_a, tool_b` note. Not stored: raw tool output (mail bodies, files, screens), approval cards, and any
answer written in a turn that read untrusted content (the taint gate); the question is kept with a one-line placeholder answer.
Failed or stopped turns are not stored. The file stays on the laptop.

## Limits
Last 40 messages per conversation; a conversation idle for more than `chat_history_days` is dropped (pruned on write, ignored on
load). A broken file means no persistence (logged once); the chat itself keeps working.

## Config (`hub.json`)
| Key | Default | Meaning | Settings |
|---|---|---|---|
| `chat_history_enabled` | `true` | keep conversations across restarts; `false` stores and loads nothing | yes |
| `chat_history_days` | `30` | days an idle conversation is kept (1 to 365) | yes |

Tests: `tests/test_chatstore.py`.
