# Notifications API

`hub/notify.py`. One list of things the hub wants you to know. Every other module sends to it through
`ctx.notify(title, body="", level="info", link=None, source="")`; this module replaces `ctx.notify` with its
own push function when the router is built, so callers must read `ctx.notify` at the moment they use it.
The push is synchronous, thread-safe and never raises.

Needs the login cookie. Errors are `{"detail": "plain sentence"}`.

## Notification object
`{id, title, body, level: info|warn|error, link, source, read, created}`

- `created` is ISO time with the local offset. `link` is a view name inside the app (`"reminders"`) or an
  http(s) address; anything else (for example `javascript:`) is dropped when the notification is stored.
- Title is cut to 200 characters and one line, body to 2000. Control characters are removed.
- Stored in `<data dir>/notifications.db` (sqlite). The latest 500 are kept and anything older than 60 days is deleted.

## Routes (`/api/notifications`)
- `GET /?unread_only=&limit=&source=` -> `{notifications: [...newest first], unread}`. `limit` 1 to 200 (default 50).
- `GET /unread-count` -> `{count}`
- `POST /read` `{ids: [int, ...]}` or `{all: true}` -> `{updated, unread}` · `400` if neither is given.
- `POST /clear` `{confirm: true}` -> `{removed}` · `409` without `confirm: true`. Audited as `notifications_cleared`.
- `DELETE /{id}` -> `{ok: true}` · `404` if it is not there.
- `GET /stream` -> `text/event-stream`. First line `retry: 3000`. Each new notification is one event:
  `id: 12` and `data: {notification json}`. A comment line `: keepalive` is sent after 15 seconds of silence.
  On reconnect the browser sends `Last-Event-ID` and everything newer (up to 50) is replayed first. The stream
  ends when the client disconnects.
- `POST /test` -> the notification it created (title "Test notification", source `notify`).
- `GET /settings` -> `{ntfy_enabled, ntfy_topic_hint, ntfy_topic_from: "settings"|"config"|null, ntfy_server, quiet_hours: {start,end}|null, min_level}`.
  The topic itself is never returned, only a hint ("ab… (24 characters)").
- `POST /settings` `{ntfy_topic?, quiet_hours?, min_level?}` -> the same object as `GET`.
  - `ntfy_topic`: 8 to 64 characters from `A-Za-z0-9_-`; `""` or `null` removes it (the `ntfy_topic` in hub.json, if any, is used again).
  - `quiet_hours`: `{"start": "22:00", "end": "07:00"}` (local time, may cross midnight) or `null` to turn off.
  - `min_level`: lowest level that is pushed to the phone (`info`, `warn` or `error`).
  - `400` with a sentence for anything else. Audited as `notify_settings_changed` with only the field names.

## Phone push (ntfy), optional
If a topic is set (settings page, or `ntfy_topic` in hub.json) and `ntfy_server` in hub.json is an http(s) address,
each stored notification is also POSTed to `<ntfy_server>/<topic>` from a background thread (timeout 5 seconds, no
redirects, at most 8 in flight). Headers: `Title` (ASCII only), `Priority` (info 3, warn 4, error 5), `Tags`, and
`Click` only when `link` is an http(s) address. The body is the notification body (max 500 characters), or the title.
A failure is logged (without the address) and audited as `ntfy_push` with `ok: false`; it never reaches the caller.
Quiet hours and `min_level` hold back the phone push only. The in-app list always gets the notification.

**Warning: ntfy topics are public.** Anyone who knows or guesses the topic name can read what you send and can
send you fake notifications. Use a long random name (for example 24 or more random letters and digits), never a
word like `alerts`, and do not put secrets in titles or bodies. The hub never sends file contents, tokens or
command output to ntfy, only the title and body that the module that raised the notification chose.
