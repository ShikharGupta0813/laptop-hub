# Reply sorting API (job mail, sorted on this laptop)

`hub/replies.py` (poll, rules, matching, routes, assistant tool). Screen: the **Replies** card at the top of the job tracker (`js/jobreplies.js`, used by `js/jobtrack.js`). Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Data: `replies.json` in the hub data folder (suggestions with a 200-character snippet, the ids of mails already seen, a thread map; never whole mails).

## Rules
- **Nothing changes by itself.** A mail becomes a *suggestion* ("Acme replied: an interview invite. Mark as interview?"). A status changes only when you tap Confirm, and then through the job tracker's own `set_status` (so a confirmed rejection or interview also cancels the pending follow-up reminders). Dismiss discards a suggestion. Nothing is ever sent.
- **Mail stays on the laptop.** Mail is read through the read-only mail helpers (`hub/gmailx.py`, `recent` for headers, `get_message(destination="local")` for bodies) and sorted by plain rules. No model sees mail text. The optional `replies_ai` (default **off**) lets a **local** model look at mails the rules could not place, first 1500 characters only.
- **Bounded.** The background pass (`start(ctx)`) runs at most every 30 minutes (first one a minute after start), reads at most 40 headers and 12 bodies, only opens a body when the header looks job-related (matches an application, comes from someone you wrote to, or reads like a reply), remembers what it has seen (last 1000), and does nothing when no mail account is connected or `replies_enabled` is false. Opening the screen never reads mail.
- Switched off with `replies_enabled: false` (routes answer 404).

## Kinds
`rejection`, `interview`, `assessment`, `offer`, `recruiter`, `auto_ack`, `opt_out`, `other`. The rules are word and phrase patterns (including phrasings common in India such as "not shortlisted", "HR round", "L1 round", "current CTC", "keep your profile in our database"). A negative phrase wins over a positive word ("unable to offer" is a rejection). A "thank you for applying... if selected for an interview" mail stays an auto acknowledgement. `opt_out` ("no thanks", "not interested", "unsubscribe", "remove me") is only looked for in answers from people you wrote to through Outreach.

## Matching
A mail is matched to an application by the company's own mail domain, the sender's display name, the company name in the subject or first part of the body (job-board senders such as Greenhouse or Lever are matched by the name in the text), role words, and the mail thread (once a mail of a thread was confirmed, later mails of that thread match strongly). Mail dated before you applied never matches. It needs a clear winner; a tie between two roles at one company is broken by the role words or left to you.

## Suggestion
`{id, kind, why, text, action (status | pick | optout | note), status, record_id, company, role, outreach, address, candidates[{id, company, role}], sender, subject, snippet, date, created, state}`.
- `status`: confirm sets the status shown. `pick`: no application matched; you choose one (the screen shows candidates) and the status. `optout`: a person you wrote to said no; confirm adds them to the Outreach opt-out list. `note`: informational (for an outreach reply, confirm records that they answered, so no follow-up goes to them).

## Routes
- `GET /api/replies` -> `{pending[], counts{total, interview, offer, rejection, assessment, other}, last_poll, last_result, settings{enabled, ai, poll_minutes}, mail_connected, note}`. Local file only. (The job tracker's `GET /api/jobtrack` carries the same object under `replies`.)
- `POST /api/replies/check` -> same, after one bounded pass now (your tap on "Check mail now").
- `POST /api/replies/{id}/confirm {record_id?, status?}` -> `{ok, record?, optout?}`. 400 when a `pick` has no application, 404 when already handled.
- `POST /api/replies/{id}/dismiss` -> `{ok}`.

## Friday and Today
The brief and Today get one local line, for example "2 replies to review, 1 interview invite" (joined to the follow-up line of the job tracker). Assistant tool `replies_review` (read-only, no approval) lists the waiting suggestions; it cannot confirm anything.

## Settings
`replies_enabled` (default true), `replies_ai` (default false).
