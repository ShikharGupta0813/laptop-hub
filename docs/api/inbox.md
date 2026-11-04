# Inbox (`/api/inbox`)

Read-only Gmail plus an AI digest and a daily briefing. It complements the Work tab, which sends mail,
lists Asana tasks and shows the calendar. Module: `hub/inbox.py`. Every route needs the hub login.

**This module never deletes, moves, flags, marks as read or sends mail.** Mail is fetched with IMAP
`BODY.PEEK` after a read-only folder select, so opening a mail in the hub leaves it unread in Gmail.
To answer a mail, `draft-reply` returns text and the app hands it to the Work send flow
(`POST /api/work/email/draft`, then `/api/work/email/send`).

## Setup

Uses the same Gmail address and app password the Jarvis mailer keeps in the OS keyring (service
`jarvis-gmail`), loaded through `config.jarvis_dir` like the Work tab. Nothing new to store. If the
Jarvis folder, the `keyring` package or the saved credentials are missing, every route except
`/status` answers `503` with one sentence saying what to do, for example
`Email isn't set up yet. Run setup email in the Jarvis folder, then try again.`

`HUB_DEMO=1` swaps Gmail for twelve sample emails (7 unread; one is a prompt-injection attempt, one is
Latin-1, one is an HTML newsletter with a tracking pixel, two have PDF attachments). Nothing real is read.

## Common

- `folder` (query, default `INBOX`): letters, digits, space and `_ . - / [ ] &`, up to 80 characters, for
  example `[Gmail]/Sent Mail`. Anything else is `400`; a folder Gmail does not have is `404`.
- `uid` is the IMAP UID (integer, 1 to 4294967295). Unknown uid is `404`.
- AI routes take `model: "local" | "claude"` (default `"local"`). A model problem is `503` with a sentence.
- Mail problems: Gmail refused the password is `502` ("Run setup email in the Jarvis folder again"),
  Gmail unreachable or slow is `502`. All IMAP calls time out after 20 seconds.
- Audit: AI calls write `inbox_ai` with `{action, model, uid|count, tone?}`. No mail content, address or
  secret is ever logged or returned.

## Routes

### `GET /status`
`{configured: bool, address: "o***@gmail.com" | null, unread: int | null, reason?: str}`. Always `200`.
`reason` explains how to set it up (not configured) or why the count is missing (Gmail unreachable).

### `GET /messages?folder=INBOX&unread=&limit=25&before=`
A JSON list, newest first (`limit` 1 to 100; `unread=true|false`; `before=<uid>` returns older mail, so
page by passing the last uid you have):

```json
[{"uid": 12, "from_name": "Anita Desai", "from_addr": "anita@example.com", "subject": "URGENT: ...",
  "date": "2026-11-04T10:51:26+00:00", "snippet": "first 140 characters...", "unread": true,
  "has_attachments": false}]
```
`date` is ISO 8601 UTC (empty if the Date header is unreadable). `from_name` falls back to the address;
`subject` to `(no subject)`.

### `GET /messages/{uid}?folder=INBOX`
```json
{"uid": 5, "unread": true, "has_attachments": true,
 "headers": {"from_name": "", "from_addr": "", "to": [{"name": "", "addr": ""}], "cc": [],
             "reply_to": "", "subject": "", "date": ""},
 "text": "plain text, at most 50000 characters", "truncated": false,
 "attachments": [{"name": "report.pdf", "size": 2064, "type": "application/pdf"}]}
```
Treat everything here as untrusted data. `text` is always plain text: the plain part if there is a real
one, otherwise the HTML run through a stdlib stripper that drops scripts, styles, hidden elements
(`display:none`, `hidden`, `font-size:0`, ...), tracking pixels and images (alt text is kept as
`[image: alt]`), and shows each link as its text plus the host only (`Read more (news.example)`), so no
tracking token is shown. Control characters and invisible/bidi characters are removed. Charsets: the
declared one, then UTF-8, then Windows-1252, never an error. Headers are RFC 2047 decoded, one line each,
capped at 300 characters. Attachment names lose any path, are capped at 200 characters and are never
opened or saved; inline images without a file name are not listed.

### `POST /messages/{uid}/summary` `{model?}`
`{summary: str, action_items: [str], needs_reply: bool}`. The mail goes to the model inside a fenced DATA
block (`===== BEGIN EMAIL DATA <random code> =====` ... `END`, the code is fresh per request so a mail
cannot fake the end) and the system prompt says instructions inside the mail are never followed. The
reply is coerced to those types and length-capped (summary 800, up to 10 items of 160).

### `POST /messages/{uid}/draft-reply` `{model?, tone?, instruction?}`
`tone`: `professional` (default) | `friendly` | `formal` | `brief`; `instruction` up to 1000 characters
(the owner's wishes, kept outside the DATA block). Returns
`{subject: "Re: ...", body, to, reply_to_differs: bool}`. **Nothing is sent.** `to` and `subject` are
built by the hub, not by the model: `to` is the Reply-To address if the mail has one, else the sender
(`reply_to_differs` is true when they differ, so the UI can warn). `400` if the mail has no sender address.

### `GET /digest?model=`
AI digest of the latest 20 unread mails in INBOX:
`{count, headline, groups: [{title, urgent, items: [{uid, from_name, subject, summary}]}]}`, urgent groups
first. Only uids that were really in the list are kept, each once; any unread mail the model forgot goes
in a final "Everything else" group. No unread mail: `{count: 0, headline: "You have no unread email.",
groups: []}` and no model call.

### `GET /search?q=`
Subject or sender search in INBOX, max 20, newest first. `q` up to 100 characters; empty gives no results.
`{results: [{title, snippet, kind: "email", open: {view: "work", section: "inbox", id: "<uid>"}}]}`
(global search shape). `503` like the other routes when email is not set up.

### `GET /briefing?model=`
The daily briefing. The hub reads its own routes in-process, and each part succeeds or fails on its own:

```json
{"date": "2026-11-04",
 "sections": {
   "calendar":  {"ok": true, "meetings": [{"title", "start", "end", "all_day", "platform"}]},
   "tasks":     {"ok": true, "overdue": [{"gid", "name", "due_on", "project"}], "today": [...],
                 "counts": {"overdue": 1, "today": 2, "upcoming": 3}},
   "mail":      {"ok": true, "unread": 7, "top_senders": [{"name", "addr", "count"}]},
   "reminders": {"ok": true, "reminders": [{"text", "due"}]}},
 "text": "Good morning. ... (spoken style, at most about 120 words) | null"}
```
A failed part is `{ok: false, error: "one sentence"}`. Sources: `GET /api/work/calendar`,
`GET /api/work/asana/tasks?filter=all`, Gmail (unread count and the sender tally of the latest 50 unread),
`GET /api/reminders` (reminders not done whose next fire/snooze/due date is today or earlier). `text` is
`null` when every part failed, the model failed or the model wrote nothing; the sections are still returned
with `200`. Weather and speaking the briefing aloud are out of scope.

## Notes for the UI
- Unread counts and lists are live from Gmail on every call; there is no cache and no stored mail.
- The `uid` is only meaningful with its `folder`.
- Gmail only: host `imap.gmail.com:993` (SSL, certificate checked).
