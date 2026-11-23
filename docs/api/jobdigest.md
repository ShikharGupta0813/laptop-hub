# Job digest API (daily top 10, weekly review, follow-up nudges)

`hub/jobdigest.py`. Screen: **Apps > Job digest** (`js/jobdigest.js`, `css/jobdigest.css`). Needs the login cookie. Data: the finder's own `opps_finds.json` (read only), the tracker's `jobtrack.json` (read only), the contacts in `networking.json` (read only) and this module's `jobdigest.json` (today's digest, the nudges, interview times).

## Rules
- **Nothing is sent, applied or changed by itself.** The only taps that change anything: "Add to apply queue" (the same step as Opportunities > Queue), "Done" on a nudge, and setting an interview time. Statuses of applications are never changed here.
- Plain code on the laptop. No model reads anything. No mail, chat, HTTP or browser code is imported.
- Background work is only `start(ctx)`: one local pass every 20 minutes. Screens make **one call when they open**, no timer.
- Time is the laptop's local clock (IST for the owner), like the tracker and the reminders.

## 1. Daily top 10
After `jobdigest_hour` (default 8) the first pass of the day builds the digest from finds the finder saw **since the previous digest** (first ever: the last day; at most the last 7 days after a long sleep). A find is left out when: its triage label is **skip**, it looks like a scam (blocked / high severity), you dismissed it, a job preference filtered it, you already track the same company and role, or it was shown in an earlier digest. The rest are ranked by the **ATS match score** (`hub/ats.py`, against your base resume from your confirmed facts); with no facts yet the finder's own score is used and the item says `finder` instead of `ats`. The best 10 are kept. An item is marked when a contact works (or used to work) at the company (`hub/networking.py` warm paths); only the name and the reason are stored.

One phone notification a day at most (`jobdigest_notify`), only when the digest has items, only from the background pass (opening the screen never notifies), through the normal notifier (`ctx.notify`, source `jobdigest`).

## 2. Weekly review
Worked out when asked, nothing stored. The last 7 days: applications sent, replies, interviews, offers (from the tracker's status history); all-time reply rate; the resume version, kind of opening (role family) and source with the best reply rate (at least 3 applications and one reply, so a hint, not a rule); and ONE suggested change by plain rules. `is_review_day` is true on `jobdigest_weekly_day` (default 6, Sunday).

## 3. Follow-up nudges
A local reminder (source `jobdigest`) with a draft ready:
| Kind | When | Draft |
|---|---|---|
| `noreply` | `jobdigest_nudge_days` (default 7) after applying, status still applied or viewed | the tracker's own follow-up draft |
| `thanks` | about 2 hours after the interview (09:00 to 21:00; later ones go to the next 09:00), at most 24 h late | thank-you note |
| `status` | 7 days after the interview, status still `interview` | next-steps note |

**No doubling with the tracker.** The tracker already reminds at day 5 and 10 (`jobtrack_followup_days`). A no-reply nudge is **not created** when a tracker follow-up (not cancelled) falls within 2 days of the nudge date. With the default 5 and 10 that means the tracker covers it; set `jobtrack_followup_days` to `[]` or to days far from 7 (for example `[14]`) and this module adds its own. The thank-you and status check are new (the tracker has none).

The interview time is the moment you marked the application as "interview" unless you give it (`POST /interviews/{id}`); giving it replaces the unfinished thank-you and status nudges. A nudge is cancelled (and its reminder removed) when the application moves on (a reply, an offer, a rejection). Old imports are not chased (more than 3 days past due). Drafts say `sent: false`; there is no send route.

## Routes
- `GET /api/jobdigest` -> `{digest, weekly, nudges, settings}` (the one call a screen makes). Builds today's digest first if it is after the digest hour and none exists yet.
  - `digest`: `{date, generated_at, since_ts, considered, passed, notified, items[{id, title, company, url, source, match{score, kind: ats|finder}, verdict (apply|maybe|""), reason, warm[{name, why}], queued}]}`; `{}` before the first one.
  - `nudges[]`: `{id, rid, kind, label, company, role, due, subject, body, state, sent: false, undo}`.
- `GET /api/jobdigest/weekly` -> `{from, to, is_review_day, applied, replies, interviews, offers, reply_rate{percent, replies, applied}, best_resume, best_opening, best_source, suggestion, text, note}`.
- `POST /api/jobdigest/refresh` -> a rebuilt digest with the same "since" point (new finds are added). 400 when called twice within 20 seconds.
- `POST /api/jobdigest/items/{find_id}/queue` -> the item with `queued: true`. 404 when it is not in today's digest, 400 for a scam.
- `GET /api/jobdigest/nudges`, `POST /api/jobdigest/nudges/{id}/done`, `POST /api/jobdigest/interviews/{application_id}` with `{"when": "2026-12-03T15:00"}` (400 unless the application is an interview).
- Every route answers 404 when `jobdigest_enabled` is false.

## Friday tools (read-only)
`job_digest` ("what's new in jobs": the top picks, spoken short) and `job_week_review`. Results are marked untrusted because company and job names come from postings. They cannot queue or apply anything.

## Today
`today.py` gets one more line, `jobdigest`: "3 new jobs in your digest, 1 with someone you know, 1 follow-up draft ready, weekly job review ready" (empty when there is nothing). It reads only `jobdigest.json`.

## Settings
`jobdigest_enabled` (true), `jobdigest_hour` (0 to 23, default 8), `jobdigest_notify` (true), `jobdigest_weekly_day` (0 Monday to 6 Sunday, default 6), `jobdigest_nudge_days` (2 to 60, default 7), `jobdigest_nudges` (true).
