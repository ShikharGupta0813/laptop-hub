# Watch (mail and chat alerts)

Keeps an eye on Gmail and Google Chat and alerts the owner about what they care about: mail or chat that matches a
rule, and mail the AI judges urgent. All routes need the login cookie and (for writes) the `X-Hub: 1` header. Errors
are `{"detail": "one plain sentence"}`.

Watch only ever raises notifications (`ctx.notify(..., link="inbox", source="watch")`) and keeps a history. It never
replies to, moves, deletes or marks any mail.

## How it works

- **Mail** is read through the hub's own inbox routes (`GET /api/inbox/messages?unread=1&limit=100` and
  `GET /api/inbox/messages/{uid}`), never by importing the inbox module. If the inbox answers 404/503 or anything else
  that is not OK, Watch records the sentence as `last_error`, sets `mail_configured` to false and tries again on the
  next round; alerts and settings are untouched.
- **Google Chat** cannot be read by the hub (Jarvis only has send webhooks). The Android app's notification listener
  posts what it sees to `POST /api/watch/ingest`.
- **Poller**: started with the hub. After a 10 second pause, and then every `interval_seconds`, it checks mail if
  `mail_monitor` is on. Changing settings wakes it at once. A failing round never stops the loop.
- **High-water mark**: the largest uid seen is saved. Only unread mail with a larger uid is looked at. The first run
  looks only at the newest 20 unread mails. Mail dated more than 24 hours ago never alerts (first run or later).
- **Seen ids** (`mail:<uid>`, last 2000, saved): the same mail never alerts twice, even if the mark is lost. A mail
  that already alerted through the phone (same sender and subject within 10 minutes) is not alerted again.
- **Rules first, then the AI**: each new message is run through every enabled rule whose `source` fits. If a rule
  matches, one alert is raised (the highest `level` wins) and the model is not asked. Otherwise, if any applicable
  enabled rule has `ai_urgent`, the message is classified by `ctx.model_for(settings.model)`.
- **AI**: at most 10 mails per cycle (the newest), 1500 characters of text each. Prompt: the system prompt says the
  message is untrusted DATA and must never be obeyed; the message sits in a fenced block with a random code
  (`===== BEGIN MESSAGE DATA <code> =====` ... `END`), so it cannot fake the end of the block. Reply JSON:
  `{"urgent": bool, "reason": "short", "category": "..."}`. Only a real true/false `urgent`, a `reason` (stripped of
  markup and links, 120 characters) and a `category` from a fixed list are used; anything else in the answer is
  ignored. Urgent means time-sensitive or needs a reply today, security alerts, failing payments, boss/family/deadline.
- **If the model fails** (not running, unreadable answer, timeout) the keyword check runs instead: `urgent`, `asap`,
  `deadline`, `overdue`, `action required`, `security alert`, `payment failed`, `final notice`, `immediately`. The
  alert says so (`by: "keywords"`, reason "Keyword check (the AI was unavailable) ..."). Mails past the 10 per cycle
  cap get the same keyword check ("a busy moment").
- Matching is case-insensitive and unicode-normalised (accents, full-width letters, ligatures and `ß` fold; invisible
  characters are removed), by substring.
- `model: "claude"` sends the (clipped) mail text to Claude; the default `local` keeps it on the laptop.
- **Quiet hours**: inside the window an alert is still stored (`quiet: true`, `notified: false`) but no notification is
  sent, unless a matching rule has `quiet_override`.
- **Notification limit**: at most 20 notifications per hour; the rest are stored with `notified: false`, and one
  "Lots of alerts from Watch" notice is sent when the limit is hit.
- Alerts never hold the whole message: subject is cut to 200 characters, snippet to 140.
- Audit entries (`watch_settings`, `watch_rule_add/edit/delete`, `watch_alert`, `watch_ai`) carry no mail or chat text.

## Status and settings

`GET /api/watch/status`
```json
{"mail_monitor": false, "chat_monitor": false, "interval_seconds": 120, "quiet_hours": null, "model": "local",
 "last_check": "2026-11-04T09:00:00" | null, "last_error": "sentence" | null,
 "mail_configured": false, "alerts_today": 0}
```
`mail_configured` is true when the last mail check could read the inbox. Both monitors are off until the owner turns
them on, so the built-in "Urgent mail" rule does nothing until `mail_monitor` is on.

`POST /api/watch/settings` any of `mail_monitor` (bool), `chat_monitor` (bool), `interval_seconds` (int 60-900),
`model` (`local` or `claude`), `quiet_hours` (`null`, or `{"enabled": true, "start": "22:00", "end": "07:00"}`; the
window may cross midnight; `enabled` defaults to true). Fields not sent are kept. Returns the status. `400` for a bad
value.

`POST /api/watch/check-now` runs a mail check immediately. `409` "Mail monitoring is off. Turn it on first." if
`mail_monitor` is off. Returns `{"checked": true, "new": 2, "alerts": 1, ...status}` (plus `"error"` if the inbox
could not be read; the response is still 200 and `last_error` explains).

## Rules

```json
{"id": "r-1a2b3c4d", "name": "Boss mail", "enabled": true, "source": "mail", "level": "warn",
 "match": {"from_contains": ["boss"], "subject_contains": [], "keywords": ["asap"], "any_of": true,
           "chat_space_contains": []},
 "ai_urgent": false, "quiet_override": false, "created": "2026-11-04T09:00:00", "builtin": false}
```
`source`: `mail`, `chat` or `any`. `level`: `info`, `warn`, `error`. Up to 20 words per list, 100 characters each, 50
rules, names up to 60 characters. A rule needs at least one word list or `ai_urgent`.

Match groups: sender (`from_contains`, name and address for mail; the sender or title for chat), subject
(`subject_contains`; the title for chat), `keywords` (subject plus text) and `chat_space_contains` (chat only; the
notification title). Within a group any word matches. `any_of: true` (default): one matching group is enough;
`false`: every non-empty group must match. A group that does not apply (chat space for mail) is skipped. A rule with
only `ai_urgent` never matches by words, it only enables the AI check for that source.

The built-in rule `urgent-mail` ("Urgent mail", mail, `ai_urgent`, `warn`) is always present and enabled; it can be
turned off or have its `level` and `quiet_override` changed, nothing else. It cannot be deleted. For chat, add your own
rule with `source: "chat"` (or `any`) and `ai_urgent: true`.

| Route | Notes |
|---|---|
| `GET /api/watch/rules` | `{rules: [...]}` |
| `POST /api/watch/rules` | create; body as above (`name` required). `400` for a bad rule or more than 50 rules |
| `PUT /api/watch/rules/{id}` | change the fields sent; `match`, if sent, replaces the whole match. `404` unknown id |
| `DELETE /api/watch/rules/{id}` | `{ok: true}`; `400` for the built-in rule, `404` unknown id |
| `POST /api/watch/rules/test` | `{rule, sample: {from, subject, text, space?, source?}}` returns `{would_match, reasons: ["sender contains “boss”"], ai_urgent, note?}`. Nothing is stored and the model is never asked. `source` defaults to the rule's (mail for `any`). Limits: from/subject/space 300 characters, text 5000 |

## Alerts

`GET /api/watch/alerts?limit=50` (1-200, newest first; the last 200 are kept)
```json
{"alerts": [{"id": "9f8e7d6c5b4a", "source": "mail", "from": "Northwind Bank", "subject": "Card declined",
  "snippet": "We could not take your payment...", "rule": "Urgent mail", "reason": "A payment failed.",
  "urgent": true, "level": "warn", "by": "ai|keywords|rule", "ts": "2026-11-04T09:00:00", "link": "inbox" | null,
  "notified": true, "quiet": true, "uid": 12}]}
```
`link` is `"inbox"` for mail and null for chat. `quiet` is present only when quiet hours held the notification back.
`uid` is present for mail. Notification titles: `Urgent mail from X` (AI or keyword verdict) or `Mail from X matched
<rule>`; for chat `Urgent chat from X` / `Chat from X matched <rule>`. Body: subject (or snippet) and the reason.

`GET /api/watch/search?q=` returns `{results: [{title, snippet, kind: "alert", open: {view: "watch", section:
"alerts", id}}]}`, at most 20, over sender, subject, snippet, reason and rule.

## Phone ingest

`POST /api/watch/ingest`, from the Android notification listener. Everything in it is untrusted data.
```json
{"source": "chat" | "mail", "app": "com.google.android.apps.dynamite", "title": "Project Atlas",
 "text": "Sam: prod is down", "ts": 1762245600000, "sender": "Sam"}
```
`app`, `title`, `text`, `sender`, `ts` are optional but one of `title` and `text` is required. Caps: app 100, title 300,
sender 200, text 4000 characters; whole body 40 KB (`413`). `ts` is epoch seconds or milliseconds, or an ISO time;
older than 24 hours is ignored, a future time counts as now. Control and invisible characters are removed. `400` for
a bad source, wrong types, too-long fields or bad JSON.

- Chat: `from` = `sender`, or the title if there is no sender; the title is the chat space when a sender is given.
  Matched against `chat` and `any` rules. If none match and an enabled rule for chat has `ai_urgent`, the model judges
  it (at most 6 model calls per minute from the phone; after that, the keyword check).
- Mail (from the Gmail app's notifications): `from` = `sender` or the title; the subject is the title when a sender is
  given, else the first line of the text. Matched against `mail` and `any` rules.
- Same `(app, sender, text)` within 2 minutes is one message. A resend with the same `ts` is never alerted twice.

Returns `{"accepted": true, "alerted": false, "alert_id": null}` (also `"duplicate": true` or a `"reason"`). When the
matching monitor is off: `{"accepted": false, "alerted": false, "reason": "Chat monitoring is off."}` with status 200,
so the phone does not retry.

## Data

`<data>/watch.json` (settings, rules, alerts, seen ids, high-water mark, last check). A damaged file is ignored field
by field; the built-in rule is always restored.

## HUB_DEMO

On start, once: a rule "From my manager" (so two rules in all) and three sample alerts (two mail, one chat). Monitoring
stays off.

## One push per message (shared with Alerts)
Watch and Alerts (`hub/alerts.py`) can both see the same Gmail / Google Chat message. `hub/notifydedupe.py` is a small in-memory registry (key = source + sender + hash of the normalised text, 10 minute window, `chat`=`gchat`, `mail`=`gmail`). On `POST /ingest` Alerts triage runs first; when it pushes or batches an alert it claims the message, and Watch then still records its alert but does not push (`deduped: "alerts"`, `notified: false`). If Watch pushed first, Alerts keeps the alert in its list with `delivery: "listed"` and `deduped: "watch"`, no second push and no spoken line. With `alerts_enabled` false nothing claims, so Watch behaves as before. Tests: `tests/test_notifydedupe.py`.
