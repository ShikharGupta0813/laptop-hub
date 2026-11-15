# Reminders, timers and alarms API

`hub/reminders.py`. Needs the login cookie. Errors are `{"detail": "plain sentence"}` (`400` for a bad request,
`404` for an unknown id). All ids are 12 lowercase hex characters.

## Reminder object
`{id, text, due, repeat, n, done, done_at, snoozed_until, created, source, last_fired, status, next_fire, when, repeat_text?}`

- `due`, `snoozed_until`, `created`: ISO local time with offset, for example `2026-11-05T09:00:00-05:00`.
  When you send a time without an offset it is taken as local time; `Z` and other offsets are converted.
- `repeat`: `none | daily | weekdays | weekly | monthly | yearly | every_n_minutes`. `n` (1 to 10080) is only used
  by `every_n_minutes`. Repeats follow the wall clock: "daily 8:30" stays 8:30 over a clock change, and
  "monthly on the 31st" goes Jan 31, Feb 28, Mar 31.
- `status`: `upcoming | snoozed | fired | done`. `fired` means a one-off reminder has gone off and is waiting to be
  marked done. `next_fire` is when it will go off next (the snooze time if snoozed). `when` is a short phrase like
  `Tomorrow 9:00 am`. `source` is free text up to 40 characters (`app`, `quick`, `demo`, ...).
- Text: 1 to 300 characters, control characters removed.

## Routes (`/api/reminders`)
- `GET /?status=open|done|all` (default `open`) -> `{reminders: [...]}`, open ones by next fire time, done ones last.
- `POST /` `{text, due, repeat?, n?, source?}` -> reminder. `400` if `due` is more than a minute in the past for a
  one-off. A repeating reminder with a past `due` starts at its next time. For `weekdays`, a weekend start moves to Monday.
- `GET /{id}` -> reminder · `PATCH /{id}` `{text?, due?, repeat?, n?, done?}` -> reminder. Changing `due`, `repeat` or
  `n` re-arms it and clears any snooze. · `DELETE /{id}` -> `{ok: true}` (audited as `reminder_deleted`).
- `POST /{id}/snooze` `{minutes?}` (default 10, 1 to 10080) -> reminder. It fires again at that time.
- `POST /{id}/done` -> reminder. A one-off becomes `done`. A repeating reminder stays: if the current time already
  fired it just acknowledges it (the reminder already points at the next time); if it has not fired yet, that
  occurrence is skipped and `due` moves to the next one.
- `POST /quick` `{text, preview?}` -> `{kind: "reminder", saved, echo, reminder}` (or `kind: "timer"` with `timer`).
  `echo` is a one-line confirmation such as `Tomorrow 9:00 am — call mom`. With `preview: true` nothing is saved
  and `reminder` holds `{text, due, repeat, n}` only. Anything unclear is a `400` with a plain sentence such as
  "Which day of the week? Try 'every Monday 9am'."
- `GET /search?q=` -> `{results: [{title, snippet, kind: "reminder", open: {view: "reminders", section: "", id}}]}` (max 20).

## Timers
- `POST /timers` `{seconds, label?}` -> `{id, label, seconds, created, ends_at, state: running|finished, remaining_seconds}`.
  1 second to 7 days, at most 50 running. Default label `Timer`.
- `GET /timers` -> `{timers: [...]}` running first. Finished timers stay listed for 24 hours.
- `DELETE /timers/{id}` -> `{ok: true}` cancels (or removes) it.

An alarm is a reminder, usually repeating, with the text "Alarm": `alarm 6:30 weekdays`, `wake me up at 7`.
Alarms read bare hours as morning.

## Quick phrases
Understood without any model. Examples: `remind me to call mom tomorrow at 9am`, `in 10 minutes check oven`,
`every weekday 8:30 standup`, `friday 5pm send report`, `on 25 dec at noon buy gift`, `in an hour and a half tea`,
`next friday lunch`, `monthly on the 31st pay rent`, `every 2 hours drink water`, `timer 10 minutes pasta`.

Rules worth knowing:
- Without a day, a time means the next time it happens (today if still ahead, otherwise tomorrow). A bare hour such as
  `at 9` takes the soonest of am/pm; with a day, 7 to 11 mean morning and 1 to 6 mean afternoon. Always check the echo.
- A day with no time means 9:00 am. `today` alone, `next week`, `weekend`, `later`, `yesterday`, `every other day`,
  `every 3 days`, and numeric dates like `3/4` where either order is possible are refused with a sentence.
- `next <weekday>` is that weekday in the following calendar week. A weekday name alone is the coming one.
- Two different days or two different times in one phrase are refused.

## The clock
`start(ctx)` runs a loop that wakes at least every 15 seconds (sooner when something is about to fire). Everything due
is announced exactly once through `ctx.notify(title, body, level="info", link="reminders", source="reminders")`; the
fired state is saved before the notification is sent, so a restart never repeats it. Reminders and timers that came due
while the hub was off are announced as "Missed reminder: ..." / "Missed timer: ..." (more than 2 minutes late). At most 10
are named; the rest become one "More missed reminders" notification. Repeating reminders missed several times fire once.
Done reminders are deleted after 30 days. Data: `<data dir>/reminders.json`. With `HUB_DEMO` set, three sample reminders
are added once.

## Call reminders (`call`)
A reminder can carry `call: true` (default `false`, returned on every reminder object, accepted by `POST /`, `PATCH /{id}` and `POST /quick`; anything that is not a boolean is a `400`). Phrases such as "call me at 5 to ...", "ring me ...", "... by call" or "via a phone call" in `POST /quick` set it (the words are removed from the text; "remind me to call mom" does not). The assistant tool `reminder_add` also takes `call`. A call reminder keeps its full text up to 500 characters (normal reminders stay at 300; a quick phrase may be up to 800 characters for a call).
When it is due it still sends the normal notification, and also queues an urgency 3 item for the phone (`kind: "reminder"`, `source: "reminder"`, `call: true`, `preview` = the full text) in `GET /api/alerts/pending`, see `docs/api/alerts.md`. It rings once per firing (a repeating reminder rings each time), clears on ack, and quiet hours do not hold it.
