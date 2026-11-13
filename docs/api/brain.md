# Brain API (Memory + task desk)

`hub/brain.py` (store, routes, hooks), `hub/brain_tools.py` (assistant tools), screen `js/brain.js` + `css/brain.css` (Apps > Memory),
Today integration in `hub/today.py`. Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. `404` while `brain_enabled` is false.

## Config (`hub.json`)
| Key | Default | Meaning | Settings |
|---|---|---|---|
| `brain_enabled` | `true` | routes, tools, fast path, Today section | yes |
| `brain_context` | `true` | add the best memories to question-like assistant turns | yes |

## Store
`brain.sqlite` in the hub data folder, table `items(id, kind, title, body, tags, who, project, persona, created, remind_at, reminder_id, status, source, meta)`.
`kind`: note, fact, decision, rca, task, research. `status`: open, done, blocked, dropped. Body <= 8000 chars, title <= 200, <= 10 tags, <= 5000 items.
`meta` holds a task's assessment JSON. Search uses FTS5 (porter) when SQLite has it, else LIKE; the score (0..1, share of the query words found, titles count most) is computed the same way for both.

## Personas (who owns what)
Every item has `persona`: `jarvis` (technical: rca, decision, fact, research, tasks with no person), `friday` (people and time: a task from a person, anything with a follow-up time), `both` (plain notes).
Chosen automatically from kind/who/remind_at, or set by hand. `persona=jarvis|friday` in a search returns that persona's items plus `both`.
`hub/persona.py`: Jarvis is told to use `recall` for technical questions; Friday to use `recall` and the follow-ups list for people/time questions and to mention due follow-ups.

## Routes
- `GET /api/brain/items?q=&kind=&status=open|done|blocked|dropped|all&persona=&limit=` -> `{items[], count, total, fts}`. Items carry `assessment` (tasks) and, for a query, `score`.
- `POST /api/brain/items {text, title?, kind?, tags?, who?, project?, remind_at?, persona?}` -> the item. `remind_at` is an ISO time or a phrase ("tomorrow 9am"); the Reminders parser makes a real reminder ("Follow up: ...", source `brain`) so the ntfy push fires; `reminder_id` is stored.
- `PATCH /api/brain/items/{id}` fields: title, body, tags, who, project, kind, persona, status, note (appended with the date), remind_at ("" clears). `done`/`dropped` also finish the linked reminder.
- `DELETE /api/brain/items/{id}` (also removes the linked reminder).
- `POST /api/brain/intake {from, request, project?, remind_at?}` -> `{item, assessment, spoken, error}`. Asks the configured AI (Autopilot's provider rule: Gemini if ready, else local, NEVER the paid Claude API) with the request plus related memories
  for `{doable: yes|partly|no, blockers[], risks[], questions[], plan[], estimate}`; stores a `task` (persona friday); `spoken` is the short verdict ("Chief, from Ravi: ... I think this can't be done yet because ..."). If the AI fails the task is still saved and `assessment` is null.
- `GET /api/brain/due` -> `{due[], open_tasks[], count}`: open items with a follow-up time up to the end of today (late ones flagged `overdue`).

## Assistant
- Tools (all `needs_approval=False`: they only write local records): `remember(text, kind?, tags?, remind_at?, who?, project?, persona?)`, `recall(query, kind?, persona?)` (answers carry dates), `task_intake(from?, request, project?, remind_at?)`, `task_update(id, status?, note?)`, `tasks_list(status?)`.
  Routing: ONE `"brain"` entry in `TOOL_GROUPS` (hub/assistant.py).
- Fast path (hub hook `AssistantService._brain_save` -> `brain.fast_save`): a message starting "remember ..." / "note ..." is saved at once with NO model and answered "Saved.". Questions ("remember when ...?"), and anything naming a time
  ("remember to call Ravi tomorrow") go to the normal assistant so a follow-up reminder can be made.
- Context hook (`AssistantService._brain_memory` -> `brain.context_for(text, config, persona)`): only for question-like text, only for matches with score >= 0.6, at most 3 items / 600 characters, added to the system prompt as data. Never for greetings or fast commands.

## Today
`GET /api/today` gains `brain: {due[], tasks[]}`, timeline entries of kind `followup`, numbers `followups_due` and `memory_tasks`; the morning text says "Follow-ups due: N, starting with ..." (so the Friday brief reads them), the evening text lists what is still open. The Today screen shows a Follow-ups section.
