# Automations (routines)

Named routines ("Good morning", "Leaving desk", "Study mode") that chain steps. They run on demand or on a schedule.
All routes need the login cookie and (for writes) the `X-Hub: 1` header. Errors are `{"detail": "one plain sentence"}`.
Every step is carried out by calling the hub's own routes in-process, exactly as the phone would, so each route's own
checks and audit entries still apply.

## Routine

```json
{"id": "good-morning", "name": "Good morning", "icon": "sun", "description": "...",
 "steps": [{"type": "briefing", "on_error": "continue"}, {"type": "notify", "title": "Hi", "body": "", "on_error": "stop"}],
 "schedule": {"kind": "weekdays", "time": "08:30"} | null,
 "enabled": true, "confirmed_dangerous": false,
 "last_run": "2026-11-04T08:30:00" | null, "last_status": "ok|partial|failed|interrupted" | null,
 "next_run": "2026-11-05T08:30" | null, "running": false}
```

`next_run` is computed (only when enabled with a schedule). `confirmed_dangerous` is set by the server, never by the
request (see Dangerous steps). Names are at most 60 characters, descriptions 300, up to 30 steps, up to 50 routines.
`icon` is a short word (`sun`, `book`, `door`, `moon`, `timer`, `calendar`, `check`, `briefcase`, default `bolt`).

### Schedules
| kind | fields |
|---|---|
| `daily` | `time` "HH:MM" (24 h, laptop's local time) |
| `weekdays` | `time` (Mon to Fri) |
| `weekly` | `time`, `days` (list of `mon`..`sun` or 0..6, Monday = 0) |
| `interval` | `every_minutes` 5..1440, counted from when the schedule was saved or enabled |
| `once` | `at` "YYYY-MM-DDTHH:MM" (must be in the future when saved; the routine switches itself off after it fires) |

Scheduler: checks every 20 seconds, starts each due routine exactly once per slot (the slot is recorded before the run
starts, so a restart cannot repeat it). A slot missed by more than 10 minutes (hub off, laptop asleep) is skipped, not
replayed. A new, changed or re-enabled schedule never fires for a slot that had already passed. A failed or partly
failed scheduled run sends `ctx.notify(level="warn")`; a manual run reports in its response instead.

### Step types
`GET /api/automations/step-types` lists them with field specs so the editor can be built generically. Every step also
takes `on_error`: `"stop"` (default; the rest are marked skipped) or `"continue"`. Unknown types and unknown fields are `400`.

| type | fields | what it does |
|---|---|---|
| `laptop_action` | `action_id`, `params` | `POST /api/laptop/run` (see `GET /api/laptop/actions` for ids and params) |
| `script` | `name` | a quick button: `POST /api/run/{name}`; a non-zero exit fails the step |
| `jarvis_say` | `text` (max 2000) | `POST /api/jarvis/say` |
| `jarvis_start` | none | `POST /api/jarvis/start` |
| `open_url` | `url` (http/https only) | the laptop `open_url` action |
| `notify` | `title` (120), `body` (500, optional) | `ctx.notify` |
| `note` | `title` (120), `body` (5000, optional) | `POST /api/notes` with "title, blank line, body" |
| `wait` | `seconds` 1..300 | pauses |
| `briefing` | none | `GET /api/inbox/briefing`, text shown as a notification; "not available" is tolerated (step marked `skipped`, still ok) |
| `reminder` | `text` (300), `in_minutes` 1..10080 | `POST /api/reminders/quick` `{text, in_minutes}`; "not available" is tolerated (`skipped`) |

### Dangerous steps
A step is dangerous when it is `lock_screen`, `sleep`, `shutdown`, `restart` or `kill_process`, or when the laptop registry
marks its action `risk: "confirm"` (for example `open_path`).
- Saving a routine with any dangerous step needs `confirm: true` in the body, otherwise `409`. The server then stores
  `confirmed_dangerous: true`. Editing a routine so it gains a dangerous step asks again. At run time a dangerous step in a
  routine that was not saved confirmed fails instead of running.
- `shutdown`, `restart` and `kill_process` can never have a schedule: `400` when saving a schedule on such a routine, and
  the scheduler refuses (and warns) if one ever appears. A manual run of such a routine needs `confirm: true` in the run
  body, otherwise `409` before anything starts.

## Routes

- `GET /api/automations` -> `{routines: [routine]}`
- `POST /api/automations` `{name, steps, icon?, description?, schedule?, enabled?, confirm?}` -> routine
  (`400` bad input, with the step number in the sentence · `409` dangerous without `confirm`)
- `GET /api/automations/{id}` -> routine · `404`
- `PUT /api/automations/{id}` any of the create fields (missing ones keep their value; `"schedule": null` removes it) -> routine ·
  `409` while it is running
- `DELETE /api/automations/{id}` -> `{ok: true}` (also deletes its history) · `409` while it is running
- `POST /api/automations/{id}/enable` `{enabled?: true}` -> routine
- `POST /api/automations/{id}/run` `{confirm?: false, wait?: true}` -> `{run_id, status, results: [...]}`
  - `wait: true` (default) answers when the run is over; `wait: false` answers at once with `status: "running"` and
    `results: []`, then poll the runs list.
  - `409` already running (one run per routine at a time) · `409` needs `confirm` (see above) · `404`
  - `results[]`: `{index, type, ok, message, skipped?}`; `status` is `ok`, `failed` (a step failed with `on_error: stop`)
    or `partial` (a step failed but later steps ran)
- `GET /api/automations/{id}/runs` -> `{runs: [{run_id, routine_id, name, trigger: "manual"|"schedule", started, finished,
  status: "running"|"ok"|"partial"|"failed"|"interrupted", results}]}` newest first, last 20 kept
- `GET /api/automations/templates` -> `{templates: [{key, name, icon, description, steps, suggested_schedule?, dangerous}]}`:
  Good morning, Start work, Study mode, Leaving desk, Bedtime, Focus 25, Meeting prep, Wrap up day. To add one, POST it
  to `/api/automations` as is (with `confirm: true` when `dangerous` is true).
- `GET /api/automations/step-types` -> `{types: [{type, label, description, fields: [{name, label, type, required,
  max_length?, minimum?, maximum?, placeholder?, choices_from?}]}], on_error, schedule_kinds, dangerous_actions,
  never_scheduled}`. Field types: `text`, `textarea`, `number`, `params` (a flat name/value object);
  `choices_from` names the route that lists the allowed values.
- `GET /api/automations/search?q=` -> `{results: [{title, snippet, kind: "routine", open: {view: "automations",
  section: "routines", id}}]}` (name, description and step text; max 20)

## Audit
`automation_save` / `automation_enable` / `automation_delete` / `automation_run` / `automation_run_done` /
`automation_skipped` carry the routine name, step types, trigger and status. Step texts (notification bodies, note text,
what Jarvis is told) are never written by this module. The routes a step calls write their own audit entries.

## Demo
Under `HUB_DEMO`, three routines are added once: Good morning (weekdays 08:30), Study mode, Leaving desk.
