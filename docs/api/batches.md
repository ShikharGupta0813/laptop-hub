# Batches API (scheduled batches and the one-tap send)

`hub/batches.py` (slots, builder, sender, routes, assistant tools), screen `js/batches.js` + `css/jobprefs.css` (Apps > Batches). Needs the login cookie.
Errors are `{"detail": "one plain sentence"}`. Data: `batches.json` in the hub data folder.

**Hard rule: building never sends.** Sending happens only from `POST /api/batches/{id}/send` with `confirm: true` (the tap on the reviewed list) or the assistant tool `batch_send` (in `safety.ALWAYS_ASK`: asks every time, even in Full Control; refused in a turn that read posting text). Panic stops builds and sends (`423`; a send in progress stops after the current item).

## Config (`hub.json`, appended; all three in the Settings allow-list and in `PUT /api/batches/settings`)
| Key | Default | Meaning |
|---|---|---|
| `batches_enabled` | `true` | slots fire (also needs Panic off) |
| `batches_catchup_min` | `360` | a slot missed while the laptop slept still builds once if within this many minutes (0 = never) |
| `batches_outreach_cap` | `10` | outreach messages sent per day through batches (0 = none). Applications use `applyq_daily_cap`. |

## Slots
`{id, name, kind applications|outreach, count 1-50, profile id or "", enabled, schedule{daily|weekdays|weekly}|null}`. A slot with no schedule uses its profile's schedule; with neither it is refused. Up to 12. Creating or editing a slot never fires for a time already past.
The loop (every 30 s) calls `tick()`: per slot, the latest due time; fired once (state in `fired`); later than the catch-up window = recorded as missed (`batch_slot_missed` audit) and skipped, never replayed. A new batch from a slot expires that slot's earlier untouched batch.

## Building
Candidates = applyq items in `ready` with a draft (queued by the finder from the profile only), of the slot's kind (outreach = kind message/dm/gig_message/outreach or channel message), minus: items in another open batch, applied/sent/skipped, `needs check`, any draft with a `[FILL: ...]` marker, and anything failing the job profile's HARD filters (pasted links exempt). Ranked by score plus the profile's soft points. Capped by the count, the daily limit left, and each profile's `daily_quota`. If short, finds the finder held back (`new`) are queued and drafted first. Then ONE notification (type `batch_ready`, through proactive and presence rules). Empty result = no batch, no noise. `notes[]` says what was left out and why.

## Sending
Per ticked item, one after another:
- **email** (applications and outreach): `Applyq.send` through gmailx. Any error stops the batch.
- **form**: only when form filling is enabled and Playwright is installed, the page is https and not on the form never list. Starts the form (honouring its 20 s gap), then: a captcha, login wall or block stops the whole batch with the page left open on the laptop; "no form found", a required field only you can answer, or a multi-step form skips that item (nothing submitted) and goes on; otherwise it captures the screenshot (so the form's "seen" gate is met), checks the unchanged digest and submits. `applied` only when the page says so; an unclear result is `needs_check` and stops the batch.
- **manual**: LinkedIn and Indeed (fixed, cannot be removed) plus the owner's list, items with missing info, items without an email, copy-only messages. Shown as "Open + copy", unticked, never sent. A client that ticks one anyway still gets nothing sent for it.
The batch tap stands for the per-form submit tap: this is the one place a form is submitted without you looking at its screenshot first. The checks above (no required gaps, no blocker, a single Submit, same digest) still apply.
Results per item: `sent, applied, needs_check, stopped, failed, not_attempted, skipped, needs_you` with a message. `sent/applied/needs_check` are final and are never repeated; a later tap on Send only tries items without a final result (never an automatic retry). Nothing in a batch can be undone: an email cannot be recalled and a submitted form stays submitted (the view says so).
Limits checked before each item: the daily limit for its kind (`stopped` when reached). One batch sends at a time (`409`).

## Routes
- `GET /api/batches` -> `{enabled, paused, slots[{..., schedule, schedule_own, next_run, last_fired, missed, today{built, sent}}], batches[summary], counters{applications, messages, applications_cap, messages_cap, applications_left, messages_left}, settings{enabled, catchup_min, outreach_cap, manual_hosts, fixed_manual_hosts}, profiles[], formfill{enabled, playwright}, sending}`.
- `PUT /api/batches/settings {enabled?, catchup_min?, outreach_cap?, manual_hosts?}`.
- `POST /api/batches/slots`, `PUT|DELETE /api/batches/slots/{id}`. Create/edit accept `time` (+ `days`) or `schedule`.
- `POST /api/batches/build {slot_id? | kind, count, profile}` -> `{batch|null, message, skipped}` (no notification when built by hand).
- `GET /api/batches/{id}` -> batch view: `items[{item_id, ticked, title, company, url, channel, route (email|form|manual), route_note, sendable, mode, geo, pay, score, profile, why[], warnings[], state, contact_email, draft{subject, body, preview}, resume, result, copy?}]`, `counts`, `notes`, `progress`, `undo`.
- `PATCH /api/batches/{id}/items/{item_id} {ticked?, draft{subject, body}?, contact_email?}` (edits go to the application queue; not while sending).
- `POST /api/batches/{id}/send {confirm: true, item_ids?}` -> `{started, id, items}` and a background send; poll `GET /{id}` (`state: sending` -> `done` when every sendable ticked item is final, else `ready`).
- `POST /api/batches/{id}/discard`.

## Assistant
`batch_list` (read-only, untrusted text), `batch_prepare` (builds; nothing sent), `batch_send` (ALWAYS_ASK, N2; the card lists count, items and "cannot be undone"). Group `batches` in the tool router.

## Not verified
Real Gmail and real job sites through the batch path (fakes in tests), a laptop waking from sleep (the clock is faked), the notification routing through presence on a phone, the screens at 390px.
