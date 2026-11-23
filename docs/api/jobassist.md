# Job assist API (interview times, call card, referral drafts)

`hub/jobassist.py` (free-time search, the three builders, routes, one assistant tool). Screens: **Apps > Interview helper** (`js/jobassist.js`, `css/jobassist.css`), plus buttons in the job tracker (**Call card**, **Referral draft**) and on an interview-invite card in the Replies card (**Offer times**). Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Data: `jobassist.json` in the hub data folder (your salary ask, one per application if you set it, and the dates you tapped "I asked them").

## Rules
- **Nothing is sent, opened or changed.** Every result is a draft or a card you read. A booking link ("pick a slot", Calendly and similar) is never opened or fetched; only its host name (for example `calendly.com`) is kept on the reply. A status never changes here. "I asked them" only writes today's date down, and can be undone.
- **Only confirmed facts.** The strongest points and the one line in a referral draft are your own confirmed fact-base bullets (`hub/factbase.py`), shown as written. If none match, the card and draft say so; nothing is invented. The **salary ask is a field you type**; it is never guessed and an empty ask is shown as "not set". The notice period is read from Job preferences (`notice_days`).
- **Light.** No background task, no polling, no network, no model, no phone permission. The screen makes one local call (`GET /api/jobassist`, which reads no mail and no calendar). Your calendar feeds are read once when you tap **Offer times**.
- Switched off with `jobassist_enabled: false` (routes answer 404).

## Free times
- Window: Monday to Friday, `jobassist_work_start` to `jobassist_work_end` (default 10:00 to 19:00, **IST**). Slots start on the hour or half hour and last `jobassist_slot_minutes` (default 60). The next five working days are searched; nothing sooner than two hours from now is offered.
- Busy time comes from the calendar feeds you enabled (`hub/calendars.py`, ICS). A timed event blocks its time plus `jobassist_buffer_minutes` (default 30) on both sides. An all-day event blocks the day only when its title says you are away (out of office, leave, holiday).
- Up to three slots, on different days first. Google Calendar events are not read by the hub, so when Google Calendar is switched on the result says "check it before you send". No feed switched on or a feed that cannot be read is also said plainly; the times then follow only your working hours.
- If no slot is free in five working days the draft asks them for times instead.

## Routes
- `GET /api/jobassist` -> `{invites[{id, kind, text, company, role, sender, subject, snippet, date, record_id, schedule_host}], records[{id, company, role, status, applied_at, referral_asked}], salary_ask, notice_days, settings{enabled, work_start, work_end, buffer_minutes, slot_minutes}, note}`. Local files only.
- `POST /api/jobassist/schedule {suggestion_id}` -> `{slots[{start, end, label}], draft{subject, body}|null, link_host, message, notes[], window, sends:false}`. 404 for an unknown reply, 400 when the reply is not an interview invite. With a booking link `draft` is null and `link_host` is set.
- `GET /api/jobassist/call-card?record_id=|item_id=|query=` -> `{role, company, came_from, status, applied_at, link, notice_days, notice_text, salary_ask, salary_ask_set, salary_ask_note, their_pay_note, points[{text, fact_id}], questions[], red_flags[], triage_verdict, notes[], sends:false}`. `item_id` is an apply-queue item; `query` finds the best tracked application by company or role words. Red flags come from the plain-code triage (`hub/triage.py`) on the saved posting text.
- `PUT /api/jobassist/salary-ask {salary_ask, record_id?}` -> `{ok, salary_ask}`. Empty clears it. With `record_id` it is that application's ask, otherwise your default.
- `POST /api/jobassist/referral {record_id, contact_id?}` -> `{candidates[], contact_id, draft{subject, body, words, facts_used, blockers[], to, why[], note}|null, asked{contact_id: date}, tips[], message, sends:false}`. The contact comes from your Networking contacts (works there now, used to work there, or the notes mention it). No linked contact: `draft` is null. `blockers` lists what to fill before copying (the job link, your name).
- `POST /api/jobassist/referral/asked {record_id, contact_id, asked?}` -> `{ok, asked}`. `asked: false` undoes it.

## Friday
Assistant tool `call_card` (read-only, no approval; the posting text is marked untrusted): "call card for Acme". It reads the card aloud; it cannot save a salary ask, mark a referral or send anything.

## Settings
`jobassist_enabled` (true), `jobassist_work_start` ("10:00"), `jobassist_work_end` ("19:00"), `jobassist_buffer_minutes` (30, 0 to 120), `jobassist_slot_minutes` (60, 15 to 180).
