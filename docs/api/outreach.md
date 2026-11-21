# Outreach API (batched personal emails)

`hub/outreach.py` (people, drafts, opt-out list, follow-ups, routes, assistant tool) plus the outreach parts of `hub/batches.py` (batch creation, spacing, limits, send-time checks). Screen: `js/outreach.js` + `css/outreach.css` (Apps > Outreach). Needs the login cookie. Data: `outreach.json` (people, opt-out list), the application queue (`applyq`) for the drafts, `batches.json` for the batch.

## Rules (hard limits are in code, not settings)
- **Nothing is sent until you tap Send on a reviewed batch.** Preparing drafts; sending is one explicit call that needs `confirm: true` (and the approved `batch_send` assistant tool, which asks you every time, works on the same batches). Panic stops a send.
- **One tap, many emails, spaced out.** The batch sends one by one through your own Gmail (`Applyq.send`), `outreach_gap_seconds` apart (default 180 s, 0 to 1800).
- **At most 20 people per batch and 20 outreach emails per day.** `batches_outreach_cap` can only lower the day limit (0 or anything above 20 still means 20). The day room counts what was sent today and what waits in open batches. The batch sender checks the limit again before every email.
- **No address guessing, no scraping.** An address is only ever one you type, with a tick that you got it from the person or their public page. Nothing is looked up, derived from a name or company, or fetched. A person added by hand needs the https link of the public page where you found them (it is shown in the email and never opened by the hub). No-reply style addresses are refused.
- **No attachments** unless you tick "attach my resume" for the batch; then only your resume file (`formfill_resume.pdf` or `.docx` in the data folder).
- **Opt-out list.** Anyone on it is never added, drafted or sent to; the check runs again at the moment of sending, so adding someone after the batch was built still stops that email. A reply of "no thanks" or "unsubscribe" is only *suggested* for the list (see replies.md); you confirm it. You can also mark anyone yourself. Every email ends with a line offering to stop.
- **One follow-up per person**, `outreach_followup_days` (default 5, 1 to 60) after the first email, only if they have not replied and no reply is waiting for your review. It is drafted into its own batch ("Follow-ups") and needs its own tap.
- Drafts use only your confirmed facts (the fact base): your name, the company and role you typed, one relevant fact if one fits. No fact fits, no claim. A missing name shows `[FILL: your name]` and that email is not sent until you fill it.
- Switched off with `outreach_enabled: false` (404).

## Routes
- `GET /api/outreach` -> `{people[{id, name, email, company, role, origin, source_url, known_from, added, state}], optout[], followups_due[{id, name, company, first_sent, due}], batches[] (open outreach batches, as in batches.md), room{limit, sent_today, queued, left}, candidates{contacts[], companies[]}, settings{hard_batch, hard_day, day_limit, gap_seconds, followup_days, enabled}, rules[]}`. Candidates are your networking contacts and watch-list companies; they carry no address.
- `POST /api/outreach/people {name, email, company?, role?, known_from?, source_url?, origin (manual | network | watch), address_confirmed: true, contact_id?}` -> person. 400 when the address is missing, not confirmed, not plain, on the opt-out list, or a link is missing (not needed for `network`).
- `DELETE /api/outreach/people/{id}`; `POST /api/outreach/people/{id}/replied`.
- `POST /api/outreach/prepare {person_ids[], attach_resume?}` -> the batch (same shape as `GET /api/batches/{id}`). 400 over 20 people, over today's room, for an opted-out person, or for someone already written to.
- `POST /api/outreach/followups/prepare {person_ids[]}` -> a batch of follow-up drafts. 400 for anyone not due.
- `POST /api/outreach/batches/{id}/send {confirm: true}` -> `{started, id, items}`. 400 without `confirm: true`. Results appear per email on the batch (`GET /api/batches/{id}`); edit a draft or untick an email with `PATCH /api/batches/{id}/items/{item}`.
- `POST /api/outreach/batches/{id}/discard`: throws the drafts away.
- `GET /api/outreach/optout`; `POST /api/outreach/optout {address | person_id, name?}`; `DELETE /api/outreach/optout/{id}`.

## Assistant tool
`outreach_prepare {person_ids?[]}`: drafts only, never sends. Without ids it lists who can be drafted. The reply says to open the Outreach screen to read and send.

## Settings
`outreach_enabled` (true), `outreach_followup_days` (5), `outreach_gap_seconds` (180), and the existing `batches_outreach_cap` (10, can only lower the hard limit of 20).
