# Friday (`/api/friday`)

A spoken assistant persona ("Friday, what's the update for today?") for the phone. Module `hub/friday.py`,
assistant tools `hub/friday_tools.py`. Every route needs the hub login (and `X-Hub: 1` for writes). Errors are
`{"detail": "one plain sentence"}`. The phone does speech to text and text to speech; this API returns text.

Friday reads other features only through the hub's own routes (in-process): `GET /api/work/asana/tasks?filter=all`,
`GET /api/work/asana/tasks/{gid}` (comments, notes), `GET /api/work/status` (the owner's Asana name),
`GET /api/work/calendar`, `GET /api/inbox/messages?unread=true&limit=20` (sender and subject only, never the
snippet or body), `GET /api/watch/alerts` (urgent mail, chat), `GET /api/reminders`, `GET /api/todos?done=false`.
Each source may fail on its own; its name is then listed in `sources_failed` and the rest still works.

Security: all text from Asana, mail, chat and calendars is untrusted DATA. It is shown and spoken but never chooses
an action. The model only writes the spoken script, from items already ranked by fixed rules, inside a fenced block
(`===== BEGIN FRIDAY DATA <random> =====` ... `END`). Item text given to the model has links and domain names
removed. Laptop proposals come only from the rules below and always need the owner's approval by id.

## Settings
`GET /settings` -> settings. `POST /settings` with any of these fields (others kept) -> settings; audited as
`friday_settings {changed: [keys]}`. `400` for a bad value or an unknown field.

```json
{"name": "Friday", "address_as": "sir", "spoken_alerts": true, "quiet_hours": null, "brief_model": "local",
 "proactive": false, "work_start": "09:00", "work_end": "18:30", "prep_apps": [], "prep_roots": []}
```
- `name`, `address_as`: 1-20 letters, digits, spaces, `.'-`.
- `quiet_hours`: `null` or `{"enabled": true, "start": "22:00", "end": "07:00"}` (may cross midnight).
- `brief_model`: `local` | `claude` (default model for brief and plan).
- `proactive`: turns the background alerts on. Turning it on starts fresh (the first check looks back 1 hour).
- `work_start`/`work_end`: `HH:MM`, at least an hour apart.
- `prep_apps`: up to 5 names from `apps` in hub.json, always proposed by laptop prep.
- `prep_roots`: shared-folder names scanned for recent files (empty = all roots).

## `GET /brief?model=local|claude`
```json
{"generated_at": "2026-11-09T08:00:00", "greeting": "Good morning, sir.",
 "sections": [{"id": "urgent", "title": "Needs you now",
               "items": [{"text": "Overdue: Fix the arc reactor", "priority": 3, "source": "asana",
                          "link": {"view": "work", "section": "asana", "id": "1201"}}]}],
 "spoken": "Good morning, sir. ... Want me to plan the day?",
 "sources_failed": ["mail"], "spoken_by": "model" | "template"}
```
Sections, in this order and only when non-empty (max 8 items each, highest priority first): `urgent`, `tagged`,
`tasks`, `meetings`, `mail`, `chat`, `reminders`, `other`. `link` is optional. `source` is `asana | todos |
calendar | watch | mail | reminders`. Times are local, no offset.

Priority rules (no model involved):
| Rule | Section | Priority |
|---|---|---|
| Asana task or to-do overdue | urgent | 3 |
| Meeting on now, or starting within 2 hours | urgent | 3 |
| Urgent mail/chat alert from Watch in the last 24 h | urgent | 3 |
| To-do with priority 3 due today | urgent | 3 |
| Someone else @mentioned the owner in a comment, or in the description, of a task assigned to or followed by the owner (24 h) | tagged | 3 |
| Someone else commented on one of the owner's open assigned tasks (24 h) | tagged | 2 |
| Asana task / to-do due today; Asana task with `priority` 3 (custom field "Priority": High, Urgent, Critical, P0, P1) | tasks | 2 |
| Non-urgent Watch alert (24 h) | mail / chat | 2 |
| Reminder already due | reminders | 2 |
| Due tomorrow; meeting later today; reminder later today; unread mail (top 5) | tasks / meetings / reminders / mail | 1 |
| All-day events, count of later tasks, more unread mail | other | 0 |

A mention is "@" plus the owner's full or first Asana name (any case; "@Shikharr" does not match "Shikhar"; e-mail
addresses do not match), or the owner's Asana gid in the comment's rich text (from `GET /api/work/status`, `asana.gid`).
Comments on followed-only tasks count only when they mention the owner. Comments by the owner (matched on the Asana
user name) are ignored; comment bodies are not shown in the brief.
`spoken`: 15-45 seconds, no markdown, no links, numbers and times as words, most important first, ends with
"Want me to plan the day?". The model's script is used only if it passes checks (no links or domains, no markup,
no echo of the data fence, 12-140 words, every number spellable); otherwise, or if the model fails, a fixed
template script is returned (`spoken_by: "template"`).

## `POST /plan` `{model?, focus_hours?}`
```json
{"date": "2026-11-09", "blocks": [{"id": "a1b2c3d4e5", "start": "2026-11-09T09:00:00",
  "end": "2026-11-09T10:00:00", "title": "Fix the arc reactor", "kind": "meeting|task|focus|break",
  "source": "calendar|asana|todos|friday"}], "spoken": "Here's the plan, sir. ...", "sources_failed": []}
```
Deterministic scheduler: real meetings stay as they are (all-day events never block time; a meeting past midnight
is clipped to today). Work goes into free time between now (rounded up to 5 minutes) and the working hours, tasks
by priority (overdue 60 min, due today 45, else 30; split across gaps), blocks of 25-90 minutes, a 10 minute break
after 50+ minutes of work when there is room, and lunch 12:30-13:00 if free. `focus_hours` (0-12) caps total work
and fills spare time with "Focus time" blocks. After working hours only remaining meetings are returned. The model
only writes `spoken` (template fallback). The plan is kept as today's plan for `/plan/apply`. `400` bad focus_hours.

## `POST /plan/apply` `{confirm: true, block_ids?: [...]}`
`409` without `confirm: true`; `409` if there is no plan for today; `400` for ids not in today's plan. Default: all
blocks except breaks. Task/focus blocks become a to-do (`POST /api/todos`, list Today, due = block start) plus a
reminder at the start; meetings get a reminder 5 minutes before (only future times). Never touches Asana or the
calendar. A block already added is skipped. Audited `friday_plan_apply {blocks, added, failed}`.
`{"applied": 3, "results": [{"id", "ok", "todo_id"?, "reminder_id"?, "error"?, "skipped"?}]}`

## Laptop prep
`POST /laptop-prep` `{}` -> `{"items": [Item], "sources_failed": []}` with
`Item = {"id": "12 hex", "kind": "url|file|app", "target": "...", "why": "...", "needs_approval": true,
"created": "2026-11-09T08:00:00", "status": "pending"}`. Proposals, from these rules only:
- join links of today's meetings that have not ended (http/https only);
- http/https links written in the notes of Asana tasks due today or overdue (tracking parameters such as `utm_*`,
  `fbclid`, `gclid` removed; max 8; shown in full; bare domains in text are never turned into links);
- apps from `prep_apps`, plus configured apps whose name matches a meeting platform (e.g. Zoom); max 5;
- up to 5 files changed in the last 24 h inside the shared folders (resolved through the safe resolver; hidden
  files, program files and links leaving the folder are skipped). `target` is the full path.
A proposal identical to one still pending keeps its id. Proposals expire after 12 hours.

`GET /pending` -> `{"items": [Item]}` (pending only).

`POST /laptop-prep/run` `{confirm: true, ids: ["..."]}` -> `{"results": [{"id", "kind", "target", "ok", "error"?}]}`.
`409` without confirm; `400` for malformed ids. Ids that are unknown, expired, dismissed or already run get
`ok: false` and nothing is opened for them. Each target is checked again (URL must be http/https; file must still
resolve inside its shared folder and not be a program; app must still be configured), then opened through
`POST /api/laptop/run` (`open_url`, `open_path`, `open_app`, with `confirm: true`). Each item is audited as
`friday_prep_run {id, kind, target (host / root/rel / app name), ok}`.

`POST /laptop-prep/dismiss` `{ids: [...]}` -> `{"dismissed": n}`.

## Proactive alerts
Off until `POST /settings {"proactive": true}`. Every 3 minutes:
- **Tagged**: new comments by other people on the owner's open Asana tasks (the up to 12 soonest-due tasks), plus
  @mentions in comments and descriptions of up to 6 tasks the owner follows (`GET /api/work/asana/followed`; per-task
  high-water marks; first run looks back 1 hour, later runs at most 24 hours). Title `"Sir, you were tagged"`, body
  `"<person> on <task>: “<first 120 characters>”"` or `"<person> mentioned you on <task>: ..."` (description:
  `"... mentioned you in the description of <task>"`), level `warn`.
- **Priority**: a task that newly became due today, overdue or high priority (`priority` 3) (first run is a silent baseline). One
  alert per task per day. Title `"Sir, <task> is due today|is overdue|is now high priority"`, level `warn`.
- **Meeting**: a meeting starting within about 10 minutes. Title `"Sir, <meeting> starts in 10 minutes"`, body with
  the time and join link, level `info`.

Each alert fires once (seen ids kept, last 2000). A round records up to 50 alerts (the rest wait: the check time
only advances past what was processed) but sends at most 5 notifications, mentions first; the others are stored with
`batched: true` and one summary notification `"Sir, and N more"` follows. Meeting alerts always notify. Notifications use
`ctx.notify(title, body, level, link="friday", source="friday")`; text is built from fields, cleaned of control
characters and capped (title 120, body 300). In quiet hours the alert is stored with `quiet: true, notified: false`.

`GET /alerts?limit=50` (1-200, newest first, last 200 kept):
`{"alerts": [{"id", "ts", "kind": "tagged|priority|meeting", "title", "body", "level", "notified", "quiet"?,
"spoken"?}]}`. `spoken` (when `spoken_alerts` is on) is the text for the phone to read aloud.

## Assistant tools (`hub/friday_tools.py`, `build(get_app, config)`)
`friday_brief`, `friday_plan`, `friday_laptop_prep` (read-only; prep stores proposals), `friday_apply_plan` and
`friday_run_laptop_prep` (need approval; the approval card shows the blocks / the exact targets; the model can only
pass stored pending ids).

## Data
`<data>/friday.json`: settings, pending proposals, alerts, seen ids, marks, today's plan.
