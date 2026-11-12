# Autopilot API

`hub/autopilot.py` (routes, sweep, execution), the `autopilot_cards` tool in `hub/friday_tools.py`, screen `js/autopilot.js`.
Autopilot prepares drafts with a free AI and queues them as cards; **nothing executes without a tap**. Needs the login cookie; every
POST also needs `X-Hub: 1`. Errors are `{"detail": "one plain sentence"}`.

## Config (hub.json)
| key | default | meaning | editable from the phone (Settings) |
|---|---|---|---|
| `autopilot_enabled` | `false` | off until turned on; read live, no restart | yes |
| `autopilot_sweep_min` | `15` | minutes between sweeps | yes (5 to 240) |
| `autopilot_daily_ai_calls` | `30` | AI calls per local day; over it, cards have no draft | yes (0 to 500) |
| `autopilot_expire_hours` | `48` | untouched cards become `expired` | hand-edit |
| `autopilot_standup_time` | `"08:30"` | weekday standup draft, made within 6 hours after this local time | hand-edit |
| `autopilot_repos` | `[]` | absolute paths of git folders for the standup (missing ones skipped) | wizard / hand-edit |
| `autopilot_tone` | friendly, short | one line, the voice drafts are written in | wizard / hand-edit |

## Cards
Table `cards` in `<data dir>/autopilot.db` (latest 500 kept, pending ones last to go). Fields: `id`, `created`, `source`
(`watch-gmail | watch-chat | asana | inbox | standup | manual`), `title`, `summary`, `action_kind`
(`send_email | asana_comment | copy_text | none`), `draft`, `ai_written`, `note`, `status`
(`pending | approved | edited | skipped | executed | failed | expired`), `edited`, `executed_at`, `error`; the view adds `recipient`,
`subject` (email cards), `sender`, `original` (first 600 chars). One card per source item (`inbox:<uid>`, `watch:<alert id>`,
`asana:<gid>`, `standup:<date>`): a second sweep never duplicates it.

## The sweep (never sends anything)
Watch alerts of the last 24 h (`GET /api/watch/alerts`), unread Gmail (`/api/inbox/messages`, owner's own mail skipped using
`/api/work/status`), Asana tasks due within 2 days (`/api/work/asana/tasks`), and the standup. A word check drops one-time codes,
no-reply senders and receipts/newsletters, makes meeting invites information-only, and calls the AI only for questions/requests.
The AI is `pick_provider()`: the default provider, but `claude`, `claude-login` and `duo` are replaced by Gemini (if ready) else local.
When a sweep makes actionable cards the phone gets one notification: "Autopilot: N drafts ready" (`link: autopilot`).

## Routes (`/api/autopilot`)
- `GET /` -> `{enabled, pending, ai_calls_today, daily_ai_calls, sweep_min, last_sweep, provider, last_error, notes, cards: [pending, newest first], counts, setup_hint}`.
- `GET /history?limit=` -> `{cards: [not pending, newest first]}`.
- `POST /sweep` -> `{new, drafts, expired, failed, notes, cards}`. `409` while `autopilot_enabled` is false.
- `POST /manual` `{title, summary?, draft?}` -> a card (`copy_text` with a draft, else `none`).
- `POST /{id}/approve` `{confirm: true, text?, to?}` -> `{card, text?, error?}`.
  `send_email` and `asana_comment` need `confirm: true` (`400` otherwise) and non-empty text. `text` is an edited draft and is what runs.
  `send_email` goes through `/api/work/email/draft` then `/send` to the original sender; `to` is an explicit recipient change (checked
  as an address, audited as `autopilot_recipient_changed`). A card whose stored recipient does not match the sender is refused. A
  failure sets `status: failed` with a short `error` and is never retried; a second approve is `409`. `copy_text` only returns `text`
  (status `approved`, or `edited` if changed). `none` just acknowledges. `404` unknown id, `409` not pending/expired.
- `POST /{id}/skip` -> the card (`skipped`); `409` if not pending.
- `/api/status` has `autopilot: {enabled, pending, ai_calls_today, daily_ai_calls}`.

## Audit events
`autopilot_sweep` (counts), `autopilot_ai` (kind, provider, input length), `autopilot_execute` (card, kind, source, chars, edited,
recipient_changed, ok), `autopilot_recipient_changed` (domains only), `autopilot_copy`, `autopilot_ack`, `autopilot_skip`. Never bodies.
The Work routes also write their own `work_email_send` / `work_asana` entries.

## Assistant tool
`autopilot_cards` (read-only, no approval): lists the waiting cards (titles and short drafts, marked as data). Approving is only done by the owner's tap.
