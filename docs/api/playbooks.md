# Playbooks (hub/playbooks.py)

"When X happens, do Y": an editable list of rules the owner writes in Apps > Playbooks. All routes need the login cookie except the webhook. There are **no playbooks by default** (`playbooks_enabled` is on, but nothing is enabled until the owner turns one on).

A playbook: `{id, name, enabled, trigger, match: all|any, conditions[], window, presence[], steps[], limits, notes, template}`.

## Triggers (registry)
`playbooks.register_trigger(name, label, schema, *, untrusted=True, sample, fields, match, check)` lets any module add one; `playbooks.emit(ctx, trigger, event)` announces an event (never raises, never blocks). Built in:

| Trigger | Filters | Event fields | Source of events |
|---|---|---|---|
| `schedule` | kind daily / weekdays / weekly / interval / once (the Automations scheduling code) | scheduled_for | the clock (20 s tick; a slot over 10 min late is skipped; enabling never catches up) |
| `alert` | source[], vip, min_urgency, kind[], sender_contains[], text_contains[] | alert_id, source, sender, urgency, kind, vip, mention, title, text | `alerts.ingest` after a real alert is raised |
| `message` | source[], mention, sender_contains[], text_contains[] | source, sender, mention, title, text | `alerts.ingest` for every watched, non-duplicate message |
| `calendar` | when starts/ends, minutes_before, title_contains[] | title, start, end, when, minutes | polls the calendar feeds each minute, once per event |
| `meeting-finished` | title_contains[] | meeting_id, title, actions, mine, text | `meetings.process` when the notes are ready |
| `task-due` | which due/overdue/either, text_contains[] | task_id, title, due, overdue | polls Memory every 5 min, once per task per day |
| `mail-arrived` | account, sender_contains[], subject_contains[] | account, sender, subject, text, mail_id | polls `gmailx.recent` every 5 min; the first look only learns |
| `opportunity-ready` | kind, text_contains[] | kind, item_id, title, text | `applyq` when a draft is ready |
| `presence-change` | to, from | to, from | polls presence each tick; the first look only learns |
| `panic-resume` | none | text | the Resume button |
| `manual` | none | text | the Run button or the `playbook_run` tool |
| `webhook` | none | title, text, sender | `POST /hooks/playbook/{id}` |

`untrusted` triggers (everything except schedule, presence-change, panic-resume, manual) start the run **tainted**.

## Conditions
Rows `{field, op, value}` with op `equals, not_equals, contains, not_contains, matches (safe pattern, hub/safepattern.py), gt, gte, lt, lte, is_set`, combined with `match`. Also `window` `{days:[0..6], start, end}` (an end before the start is an overnight window) and `presence` (allowed presence states). `limits`: `max_per_day` (default 20, max 500), `cooldown_min` (0), `timeout_s` (900). A condition that does not match starts nothing and is not logged.

## Steps (max 20)
`notify` (title, text, speak, level), `draft_reply` (alert_id; runs the `draft_reply` tool), `ask_assistant` (prompt, class routine|private; goes through `/api/lanes/run`, redacted when not private, event text wrapped in `<data id=...>` markers), `create_task` and `add_memory` (the `remember` tool, **always asks the owner**), `run_tool` (tool, args), `set_focus` (minutes), `wait` (seconds, max 300), `if` (left, op, value, otherwise stop|skip, skip). Each also has `on_error` stop|continue and an optional `label`.

`{event.field}`, `{stepN.output}`, `{stepN.ok}`, `{stepN.status}` are substituted **as plain data in one pass**: control characters removed, 1500 characters at most, never evaluated, never expanded again. Unknown names become empty.

Steps never take shell strings (`command`, `shell`, `script`, ... are refused). `run_tool` may not name `run_command`, `run_script`, `type_text`, anything about settings, Full Control, Panic, computer control or playbooks; the tool and its argument names are checked at save time and the arguments again by the tool's own schema validation at run time.

## Safety (the assistant's gate is reused, not copied)
Every tool step runs through `AssistantService._call_tool` in a session `playbook:<run id>`: validation, ALWAYS_ASK (send_email, send_application, post_publish, submit_form, chat_send always wait), safety levels (N3 needs the typed CONFIRM and its countdown), Panic, **trust rules** (hub/trust.py: only for the exact shape, never when tainted) and the **taint rule**. A step that needs the owner becomes a pending approval on the run and a push (link `playbooks`; the notification layer has no Approve/Deny buttons, so the push opens the Playbooks screen where they are); the run waits (up to 10 minutes, then the step fails as "no answer"). `create_task` / `add_memory` force an approval (`session.force_gate`, the one two-line change in `hub/assistant.py`).

A tainted run refuses every step that changes something (tools that ask or are marked mutating, `set_focus`); reading untrusted tool output taints it from then on; `notify` only reaches the owner and is never refused.

Loop protection: a run cannot start itself, nor a playbook more than 2 hops deep (`A -> B -> C`, not `D`; the chain follows events emitted inside a run), one run per playbook at a time, `playbooks_max_concurrent` runs overall (default 3), at most 10 starts per playbook per minute.

Panic cancels every run and denies pending approvals (status `cancelled`, "Stopped by Panic"), no playbook starts while it is on, and Resume restores nothing but the `panic-resume` trigger (the enabled switches are never touched).

The **assistant cannot create or edit playbooks**: its three tools only list, run (manual trigger, same gate, refused while tainted) and enable/disable (asks first, N2).

## Routes
| Route | |
|---|---|
| `GET /api/playbooks` | `{enabled, paused, playbooks[], triggers[], step_types[], ops, presence_states, limits, max_steps, pending[], templates[]}`; each playbook adds `trigger_label, untrusted, last_run, next_run, runs_today, running, example, webhook` |
| `POST /api/playbooks`, `GET/PUT/DELETE /api/playbooks/{id}` | create (starts **off**), read, change (a partial body keeps the rest), delete. 400 with a plain sentence for anything invalid |
| `POST /api/playbooks/{id}/enable` `{enabled}` | on or off |
| `POST /api/playbooks/{id}/test` `{event?}` | dry run with a sample event (the trigger's sample if none): returns the finished run; nothing outward happens |
| `POST /api/playbooks/{id}/run` | manual run now (no cap or cooldown, same gate); 423 while Panic is on |
| `GET /api/playbooks/runs?playbook=&limit=`, `GET /runs/{rid}`, `POST /runs/{rid}/cancel`, `POST /runs/{rid}/rerun` | history (newest first), one run in full, cancel, run again with the stored event |
| `GET /api/playbooks/approvals`, `POST /approvals/{call_id}` `{run_id, approve, confirm_text?}`, `POST /approvals/{call_id}/cancel` `{run_id}` | pending approvals, answer one, cancel an N3 countdown |
| `GET /api/playbooks/tools` | the assistant tools a step may name (`name, description, props, required, asks`) |
| `GET /api/playbooks/templates`, `POST /templates/{key}/use` | starter templates; using one adds a **disabled** copy |
| `PUT /api/playbooks/settings` `{enabled?, max_concurrent?}` | the master switch and the concurrency cap |
| `POST /api/playbooks/{id}/webhook` | new shared secret (shown once; only a SHA-256 is kept) |
| `POST /hooks/playbook/{id}` | public: header `X-Webhook-Secret` (or `Authorization: Bearer`), JSON `{title, text, sender}`; 401 for anything wrong, 429 after 10 wrong tries a minute, 413 over 20 kB |

A run: `{id, playbook_id, name, trigger, source, dry, status, started, finished, reason, hops, chain, tainted, event, event_summary, steps[{n, type, label, status, output, reason, level}], approvals[{call_id, step, tool, level, input, state, asked, answered}], rerun_of}`. Status `running, waiting, done, partial, failed, cancelled, timeout, skipped, interrupted`. The last 300 runs are kept in `<data>/playbook_runs.json`; a hub restart marks open runs `interrupted`.

## Audit (no event text over 200 characters, never secrets; event fields named secret, token, password, key, cookie are dropped)
`playbook_saved`, `playbook_deleted`, `playbook_enabled`, `playbook_started`, `playbook_step` (type, tool name, status, level, reason; never the arguments), `playbook_finished`, `playbook_skipped`, `playbook_approval_asked`, `playbook_approval`, `playbook_taint_block`, `playbook_cancelled`, `playbook_webhook_secret`, `playbooks_settings`. Tool calls inside a run also leave the normal `assistant_tool` line (session `playbook:<id>`).

## Assistant tools (`TOOL_GROUPS["playbooks"]`)
`playbook_list`, `playbook_run(name)`, `playbook_enable(name, on)` (asks). Nothing to create or edit.

## Config (append-only)
`playbooks_enabled` (true), `playbooks_max_concurrent` (3). Changed with `PUT /api/playbooks/settings`.

## UI
Apps > Playbooks (`js/playbooks.js`, `css/playbooks.css`, in sw.js SHELL, VERSION not bumped): list with switches and last result, pending approvals, a form editor (trigger, conditions, window, presence, steps with a tool picker, limits), "Test with a sample event", run history with Run again and Cancel, "Start from a template". Starter templates (all labelled examples, all added switched off): VIP draft reply, Friday weekly summary, After a meeting: propose action items, Overdue tasks each morning, Hold notifications while in a meeting.

Tests: `tests/test_playbooks.py`.
