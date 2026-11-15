# Job tracker API (applications, follow-ups, what is working)

`hub/jobtrack.py` (records, follow-ups, stats, routes, assistant tool). Screen `js/jobtrack.js` + `css/jobtrack.css` (Apps > Job tracker). Today and Friday's brief get one line.
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Data: `jobtrack.json` in the hub data folder (private, local, no secrets).

## Rules
- **Statuses change only by the owner's tap** (`POST .../status`). Time passing, replies seen by the application queue, and the screen's suggestions never change one.
- **Nothing is sent, posted or applied.** The nudge is a draft with a Copy button. The module imports no mail, chat, HTTP or browser code; its only outbound hop is the assistant tool reading its own `GET /summary`. The one thing a suggestion can change is the owner's search profile, and only on the owner's tap (see Learning loop).
- **No model sees job data.** Stats and suggestions are plain code. LinkedIn and Indeed are never opened or read (they stay manual).
- No polling. The screen makes ONE call (`GET /api/jobtrack`). The only background work is `start(ctx)`: one local pass every 30 minutes (import newly sent applications, make sure each waiting one has its follow-up reminders).
- Switched off with `jobtrack_enabled: false` (routes answer 404, the Today line is empty).

## Record
`{id, company, role, source, url, applied_at (YYYY-MM-DD), posted_at, stale_posting, resume_version, status, history[{status, at, note}], notes, salary_note, origin (manual|applyq), applyq_id, call, followups[{day, due, done, cancelled, reminder_id}], created}`.
Statuses: `applied, viewed, replied, interview, offer, rejected, ghosted, withdrawn`. Waiting statuses are `applied` and `viewed`.
Records of the application queue (`hub/applyq.py`, state sent / applied / replied) are imported once (source = the finder source id, else the queue's source). Deleting an imported record keeps it deleted. Add your own with `POST /applications`.

## Routes
- `GET /api/jobtrack` -> `{records[] (newest first; each also has status_label, suggest{status, text}|null, followups_due[], duplicates[]), counts{status: n}, due[{id, company, role, day, due}], stats, suggestions[], summary, statuses, settings{enabled, followup_days, ghost_days, stale_days, duplicate_days}, reminders}`. Imports from the queue and schedules missing follow-ups first.
- `POST /applications {company, role, source?, url?, applied_at?, posted_at?, resume_version?, salary_note?, notes?, status?, call?}` -> `{record, warnings[]}`. Always adds; warnings say "already applied N days ago" (same company AND role within 90 days) and "old posting" (older than 30 days when you applied).
- `POST /check {company, role, applied_at?, posted_at?}` -> `{duplicates[], stale, stale_days}`. Stores nothing (the screen asks before adding).
- `PATCH /applications/{id}` (any of company, role, source, url, applied_at, posted_at, resume_version, salary_note, notes; a new `applied_at` moves the reminders). `DELETE /applications/{id}` (also clears its reminders).
- `POST /applications/{id}/status {status, note?}` -> record. 400 for an unknown status or the same one. Leaving a waiting status removes its pending reminders; going back to one re-creates them.
- `POST /applications/{id}/followups/{day}/done` -> record ("I followed up"; removes that reminder).
- `POST /applications/{id}/call {call: bool}` -> record. Sets the `call` flag on its pending reminders (the reminders service rings them like a call).
- `GET /applications/{id}/draft?day=5` -> `{subject, body, facts_used[], note, sent: false}`. Built from the fact base (`hub/factbase.py`): your name, the application details, and your most relevant confirmed bullet if one matches the role; otherwise the note says so. A missing name shows `[FILL: your name]`. The second and later follow-up days use a shorter "once more" wording.
- `GET /stats`, `GET /summary` -> weekly text `{text, applied, replies, interviews, followups_due, waiting, best_source, since}`.
- `POST /suggestions/{id}/accept {profiles?[]}` -> `{ok, changed[]}`; `POST /suggestions/{id}/dismiss`.

## Follow-ups
When a record is added with a waiting status, one reminder per day in `jobtrack_followup_days` (default `[5, 10]`; `[]` = none) is created through `hub/reminders.py` at 09:00 local, source `jobtrack`, text "Follow up: role at company (day N)". A day already more than 3 days past when the record is made (old imports) is not chased. Today's due count ("N follow-ups due") is read from `jobtrack.json` only: a waiting record whose follow-up date has come and that you have not marked done.
Suggestion on a card: waiting for `jobtrack_ghost_days` (default 14) with no change shows "No reply for N days. Mark it ghosted?"; the queue seeing a reply shows "Mark it replied?". Both are buttons that open the status sheet; nothing is applied.

## Learning loop
`stats`: replies (replied, interview, offer or rejected ever recorded) and interviews (interview or offer) per source, per role word (3+ applications), per resume version, days to first reply (median, mean). Withdrawn records are left out.
`suggestions` (plain rules, 10+ applications for "nothing worked"): `weak_source` (0 replies from 10+), `good_source` (2+ interviews), `good_keyword` (5+ applications, 2+ replies, 40%+ reply rate), `weak_keyword` and `weak_resume` (notes only). Accepting changes ONLY enabled search profiles through `hub/jobprefs.py` `update`: `weak_source` removes the source from the `sources` list of profiles that list it (never leaving it empty, which would mean all sources); `good_source` adds it to profiles that restrict sources; `good_keyword` adds the word to `include` (soft score). Job preferences has no per-source weight, so when no profile can take the change `adjustable` is false and the card says so; it stays a note. Accepted or dismissed suggestions do not return.

## Assistant tool
`jobtrack_summary` (read-only, no approval, results marked untrusted because names come from postings). Tool group `jobtrack`, registered in `hub/jobtrack.py`.

## Config (Settings allow-list)
`jobtrack_enabled` (bool, default true), `jobtrack_followup_days` (list of up to 4 ints 1..120, default `[5, 10]`), `jobtrack_ghost_days` (int 3..120, default 14).

## Not verified
Real reminders ringing at the due time (tests use a fake reminders service), reading a real application queue with many items, the screen on a phone (checked at 390 px width in the dev server's browser only).
