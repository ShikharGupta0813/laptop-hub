# Applyq API (ready-to-send application queue)

`hub/applyq.py` (store, drafting, routes, hooks), `hub/applyq_tools.py` (assistant tools), screen `js/applyq.js` + `css/applyq.css` (Apps > Applications).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. `404` while `applyq_enabled` is false.

**Hard rule: nothing is ever sent automatically.** The only code that sends is `Applyq.send`, reached from `POST /api/applyq/{id}/send` (a tap) or
the assistant tool `send_application` (approval card, level N2). Adding, drafting, summaries, reply checks and the proactive loop never send.

## Config (`hub.json`, appended; both in Settings allow-list)
| Key | Default | Meaning |
|---|---|---|
| `applyq_enabled` | `true` | routes, tools, Today line, notifications |
| `applyq_daily_cap` | `10` | email sends per day before an extra confirm is needed (0 = no cap, max 200) |

## Items
`applyq.sqlite` in the hub data folder. States: `found -> drafting -> ready -> sent | applied | skipped | failed`, plus `replied`.
Fields: `id, title, company, url, source, kind, score, reason, flags[], description, contact_email, draft{subject, body, cover_note, answers{}, missing[], generator}, channel (email|web|message), account, state, created, updated, sent_at, applied_at, skipped_at, replied_at, notes[], error`.
Dedupe key = URL (lower-case host, no fragment, no `utm_*`/`ref`/`fbclid` parameters, no trailing slash). Text with no URL gets a `manual:<hash>` key.

## For the finder
`applyq.add_item(ctx, item) -> stored item | None` (never raises, never sends). `item`: title, company, url, source, description, score, reason, flags, kind (optional: contact_email, channel, account).
A new item is drafted at once and a batch notification (`applyq_ready`) is submitted. A duplicate URL returns the existing item unchanged.

## Drafting (honest by construction)
Default: a template, no AI, always works. It uses ONLY the profile (`profile.get_profile(ctx)`, guarded import): summary, skills that appear in the posting, up to 3 experience lines that share words with the posting, links, standard answers.
Anything it cannot know becomes `[FILL: expected rate]` in the text and an entry in `missing[]` (rate, availability, portfolio, intro, name, ...). Email sending is refused while a `[FILL: ...]` marker is left.
No profile: the draft says so (`missing` gets "profile ...") and nothing is invented.
Optional AI rewrite: `POST /{id}/draft {"ai": true}` or an injected `draft_fn(item, profile, base) -> {body?, cover_note?, subject?} | None`. The model is `ctx.model_for("auto")` (already wrapped by the redaction hook, never the paid Claude API for routine work) and is told to use only the profile facts and keep markers. Failures fall back to the template.

## Routes
- `GET /api/applyq?tab=ready|sent|skipped|all` -> `{items[], count, summary}` (ready tab: found/drafting/ready/failed, best score first; sent tab: sent/applied/replied). Items carry `missing[]`.
- `GET /api/applyq/summary` -> `{today{applied,sent,ready,skipped,replied}, week{applied,sent,skipped,replied}, streak, cap, sent_today}`. Streak = consecutive days (ending today, or yesterday if today is empty) with a send or an "I applied".
- `GET /api/applyq/accounts` -> `{accounts[{id,label,address}], gmail}` from `gmailx.list_accounts` (empty when gmailx is absent).
- `POST /api/applyq/manual {text, company?, contact_email?, channel?}` -> `{item, created}`. A leading link becomes the URL, the rest the description.
- `GET|PATCH|DELETE /api/applyq/{id}`. PATCH: `title, company, contact_email, channel, account, draft{subject,body,cover_note,answers}, note`.
- `POST /{id}/draft {ai?}` redraft. `POST /{id}/skip`, `/restore`, `/applied` (marks "I applied").
- `POST /{id}/send {account?, confirm_over_cap?}`:
  - channel `email`: `gmailx.send_mail(ctx, account, to, subject, body, None)`; success -> state `sent`, audit `applyq_send` (recipient domain only). Email has **no undo** (the UI says so). A failure sets `failed` with the reason and returns the text to copy.
  - over `applyq_daily_cap` without `confirm_over_cap` -> `409 {needs_confirm: true, detail}`.
  - `gmailx` missing -> `{ok:false, reason:"no_gmail", copy{...}}`; offer Copy.
  - channel `web` / `message`: nothing is sent; returns `{ok:true, sent:false, action:"open"|"copy", copy{url, body, cover_note, answers}}`; the owner applies and taps `/applied`.
- `POST /api/applyq/check-replies`: needs `gmailx.search(ctx, account, query)` (guarded). Searches each sent item's contact domain (exact address for gmail.com-style domains); a sender in that domain, or the draft subject inside a message subject, marks the item `replied`.

## Assistant
Tools: `list_ready_applications`, `show_application(id)`, `draft_application(id)`, `mark_applied(id)`, `skip_application(id)`, `send_application(id)`, plus `fill_application_form(id)` and `submit_form(id)` (see formfill.md).
`send_application` needs approval, level **N2** (voice may approve it; N3 is for critical laptop actions), names one item, and its card shows recipient, subject, account and "Email cannot be undone". "Send it" without an item id is refused. List/show return posting text (untrusted: the turn is tainted, so a send in the same turn is refused; ask again).
`TOOL_GROUPS["applyq"]` routes them. Persona: Friday owns the queue and the daily summary; Jarvis helps with technical answers.

## Fill the form on the laptop (optional)
A ready `web` item can be pre-filled in a browser on the laptop: `POST /api/applyq/{id}/formfill/start`, a review sheet with a screenshot, and a separate Submit tap. The hub never submits on its own; the click needs your confirm and an unchanged page. `Applyq.formfill_result(id, applied, note)` records the outcome (`applied` only when the page says so, else a `needs check` flag on the item). Everything is in [formfill.md](formfill.md). Assistant: `fill_application_form(id)` (N2, prepares only) and `submit_form(id)` (`safety.ALWAYS_ASK`).

## Notifications and Today
`applyq_ready` is a BATCH type ("2 applications are ready to review"). `applyq_daily` (a "now" type) is sent once a day after `today_evening_time`: "Today: N applied, M ready, K replies" (skipped when all zero).
`GET /api/today` gains `applyq` (one line or ""), shown as a small line on Today and on the front page.

## Not verified
Real Gmail sending and reply search (`hub/gmailx.py` is another worker's module; tests use a fake), the AI rewrite against a real model, the screen on a real phone.
