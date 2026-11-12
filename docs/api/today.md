# Today API

`hub/today.py` (routes, collecting, the spoken template), tools `today_summary`, `autopilot_pending`, `autopilot_decide` in `hub/friday_tools.py`,
screen `js/today.js`, evening push in `hub/autopilot.py`. Read-only: Today never writes, sends or runs anything. Needs the login cookie.
Errors are `{"detail": "one plain sentence"}`. `404` while `today_enabled` is false.

## Config (`hub.json`)
| Key | Default | Meaning | Settings |
|---|---|---|---|
| `today_enabled` | `true` | the routes, the tool and the evening summary | yes |
| `today_evening_time` | `"19:30"` | weekday time of the evening push + card; `""` = off. Needs `autopilot_enabled` | yes (HH:MM or empty) |
| `autopilot_research` | `true` | Autopilot reads thread / task / notes before drafting (see autopilot.md) | yes |

## Routes
- `GET /api/today` -> one JSON timeline for the local day: `date, weekday, generated_at, meetings[{title,start,start_time,end_time,all_day,link,platform,past}],
  tasks{due_today,overdue,updated,commented,completed}, expenses{by_currency,items,count}, mail{configured,unread,replied[]}, done{commits,tasks,cards,council},
  pending{autopilot,cards,next_meeting}, timeline[{start,time,kind,title,sub}], numbers{...}, sources_failed[]`.
  Sources: the calendar, Asana, expenses, inbox, Autopilot and Council routes (in-process) and `git log` of `autopilot_repos` (Autopilot's helper). One failing source is named in
  `sources_failed`; the rest still show. Council missing is not a failure. `timeline` is time-ordered; spending has no clock time so it closes the day.
  Asana completions are counted from the audit log (`work_asana` / `complete`), because the Work API cannot list completed tasks. "Mails replied" are the Autopilot replies that were sent today.
- `GET /api/today/text?style=morning|evening` -> `{text, words, style, numbers, sources_failed}`. At most 180 words, plain sentences, NO AI call (a template: the same data gives the same text).

## Triggers
- Words: "summary", "my day", "what's the update", "day end", "end of day", "what did I do today", "recap", "wrap up" (typed or spoken, same text path) route to tool `today_summary`.
- Day start: the `friday_brief` tool appends `Today so far: ...` to the brief it reads out.
- Day end: when Autopilot is on, the sweep loop fires once per weekday at/after `today_evening_time`: a push (`Your day, in short`, first 200 characters) and an FYI card "Day review"
  (source `today`) in Autopilot that holds the full text, so it is readable, not voice-only.

## Approvals by voice or text
- `autopilot_pending` -> numbered list (ids, titles, drafts). Read-only. The numbers are remembered for one hour.
- `autopilot_decide {number_or_id, decision: approve|skip, edited_text?}` -> runs the normal approve/skip route with `confirm: true`; the owner's explicit command is the approval.
  `number_or_id` is a number or id from the last listing, or a short description that fits exactly one card ("the mail one"). Refused: no listing yet, a number not in the list,
  a card that is not pending any more, ambiguous descriptions. The recipient guard still applies (mail goes only to the original sender).
