# Inbound sources API

`hub/sources.py` (the generic routes, the app list, `capture_config`), `hub/tasksources.py` (task sources + webhook),
`hub/calendars.py` (calendar feeds), `hub/gmailx.py` + `hub/mailaccounts.py` (mail accounts), `js/sources.js` (Alerts > Settings).
All `/api/...` routes need the login cookie (every POST/PUT/DELETE also needs `X-Hub: 1`). Errors are `{"detail": "one plain sentence"}`.

**Design rule:** nothing is hard-wired to one tool. Every kind is a LIST the owner can add to, edit, switch off or delete, and the
existing items (Asana, Google calendar, the Gmail app-password account, WhatsApp/Slack/...) are ready-made entries in it.

## Kinds and routes
`GET /api/sources` returns every list at once (`{app, task, mail, calendar}`, each `{items: [...]}`).
`GET|POST /api/sources/{kind}` list / add, `PUT|DELETE /api/sources/{kind}/{id}` edit / delete, `POST /api/sources/{kind}/{id}/test`,
`POST /api/sources/{task|webhook}/{id}/poll` (poll now). `{kind}` is one of:

| kind | items | stored in | secret (OS keyring, never returned) |
|---|---|---|---|
| `app` | notification apps: `{id, label, package, enabled, builtin, match}`. Ready-made: WhatsApp, Slack, Teams, Gmail, Google Chat, Asana, Jira, Trello, Telegram, Outlook, three SMS packages | `<data>/sources.json` | none |
| `task` | `{id, kind, label, enabled, settings, has_secret, secret_ref, last_poll, last_error, builtin}`; `kinds` in the list answer describes the fields. Kinds: `asana`, `jira`, `trello`, `linear`, `github`, `rss`, `webhook`. Ready-made: `asana` (the existing poller; deleting it signs it out and switches it off) | `<data>/tasksources.json`, poll state `tasksources_state.json` | service `redline-sources`, key `task:<id>` |
| `webhook` | the `task` list filtered to webhooks; `POST` here always creates a webhook | same | same |
| `mail` | the accounts of `/api/gmail/accounts` (Gmail app password, Google sign-in, generic IMAP/SMTP). `POST` here adds an IMAP/SMTP account (`type: "imap"`); a Google sign-in is started from `POST /api/gmail/accounts/connect` | `<data>/gmailx.json` | see gmailx.md |
| `calendar` | `{id, kind: google\|ics, label, color, enabled, address_hint, has_secret}`. Ready-made: `google` (can be switched off, not deleted) | `<data>/calendars.json` | service `redline-calendars`, key `cal:<id>` (the feed address, which can contain a secret) |

Add bodies: `task` `{kind, label, settings: {...}, secret, enabled?}` (jira: `settings.site` https URL + `settings.email`, secret = API token;
trello: secret `KEY:TOKEN`; linear: API key; github: PAT with notifications; rss: `settings.url`; asana: optional `settings.workspace`, secret = PAT;
webhook: a secret of 16+ characters that the OWNER chooses, the Settings screen can generate one in the browser); every polled kind takes
`settings.poll_minutes` 1-120 (default 5). `calendar` `{label, url, color: "#rrggbb"}` (`webcal://` is read as https). `app` `{label, package}`.
`mail`: see gmailx.md. A secret is sent once and never comes back: views say `has_secret: true`.

## Adapter interface (`hub/tasksources.py`)
`poll(ctx, source) -> [event]` returns only NEW events. `source` has `.settings`, `.secret`, `.state` (a dict the registry saves),
`.http(method, url, headers, body)` and `get_json(...)`; `fresh(source, events)` turns a full listing into "new since last time"
(the first poll is a silent baseline, so a backlog never floods the phone). An event is what `hub.alerts.triage` already takes:
`{source, app, sender, title, text, link, mail_id, uid, kind_hint?, mention?, due_on?}`. Events go to `alerts.triage(ctx, event,
trusted=True)`: a source the owner switched on is trusted, so the watched-words check is skipped; events from the phone and from
`POST /api/alerts/ingest` are never trusted. To add a service in code: write `poll_x`, add it to `ADAPTERS` and `KINDS`.

| kind | calls (read-only) |
|---|---|
| jira | `GET {site}/rest/api/3/search/jql?jql=assignee = currentUser() AND updated >= -1d` (falls back to `/rest/api/3/search` on 404/410), Basic auth email + API token |
| trello | `GET /1/members/me/cards?filter=open`, `GET /1/members/me/notifications?read_filter=unread&filter=commentCard,mentionedOnCard,addedToCard,changeCard`; key and token in the `Authorization: OAuth ...` header, never the URL |
| linear | `POST https://api.linear.app/graphql` `viewer { assignedIssues }`, personal API key as the raw `Authorization` value |
| github | `GET https://api.github.com/notifications?participating=true`, `Authorization: Bearer <PAT>`; mentions and team mentions are `mention`, assignments and review requests are `kind_hint: task` |
| rss | `GET <url>`: RSS 2.0 `item` and Atom `entry`; a document with a DOCTYPE or entities is refused |
| asana | the poller of `hub/asana.py` with this entry's own token, workspace and baseline |
| webhook | pushed to the hub, see below |

**Not verified against live accounts** (built from public docs plus fake HTTP in tests; the first real poll is the real test): Jira's
search endpoint (Atlassian is moving `/search` to `/search/jql`; both are tried), Trello's notification filter names and the OAuth
header form, Linear's `assignedIssues` shape, GitHub's notification `reason` values, Asana (see alerts.md).

## Inbound webhook (public route, no login)
`POST /hooks/{id}` with header `X-Webhook-Secret: <secret>` (or `Authorization: Bearer <secret>`) and JSON
`{title, text, sender, url, mention?}` (40 KB max). Any tool that can POST can use it, no code. The secret is compared in constant time;
a wrong secret, a wrong id and a disabled hook all answer the same `401 Not accepted.`, and 10 wrong tries a minute lock that hook
for a minute (`429`). The path is outside `/api/` on purpose, so a sender does not need the `X-Hub` header. Expose it beyond the laptop
(Tailscale Funnel or similar) only on purpose: the secret is the only protection.

## Calendar feeds
ICS parser: `VEVENT` with `DTSTART/DTEND/DURATION/SUMMARY/LOCATION/URL/DESCRIPTION`, line folding, UTC, floating and `TZID=` (IANA names
and a few Windows names; anything else is read as local time), all-day dates, `EXDATE`, `RECURRENCE-ID` overrides, `STATUS:CANCELLED`, `RRULE`
`FREQ=DAILY|WEEKLY|MONTHLY|YEARLY` with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (monthly also `2TU`, `-1FR`), `BYMONTHDAY`. Ignored, never an error:
`BYSETPOS`, `RDATE`, `VTIMEZONE` rules, alarms, attendees. A recurrence is expanded at most 4000 steps from its start.
`GET /api/work/calendar` (which Today, Friday and the 10-minute meeting reminder read) now returns Google (if switched on) plus every
enabled feed together, sorted; each row also has `feed` and `color` once a feed exists. `GET /api/calendars/agenda?day=&days=` returns
the feeds only. A broken feed is a `last_error` on its card, never an error for the others. Fetches are cached 5 minutes, 5 MB max.

## Phone capture list
`GET /api/alerts/capture-config` -> `{"version": 1, "packages": [{"package": "com.whatsapp", "label": "WhatsApp"}, ...]}`: the enabled `app`
entries that have a package. The Android capture app should read this instead of a hard-coded list (the Java change is a separate task).

## alerts_sources
Still in `hub.json`, still what `hub.alerts.is_watched` checks, but now written by the app list: the match word of every enabled app
(`whatsapp`, `slack`, ..., `sms` for the SMS packages, the package name for a custom app), plus `email` while a mail account takes part in
alerts and `asana` while the Asana poller is on. The first time the app list is built, existing words that no ready-made entry covers
become custom entries. `POST /api/alerts/settings {sources}` still works, but the list in Settings is the source of truth.

## Tests
`tests/test_sources.py`: a fake HTTP layer for every adapter, webhook auth (bad secret, locked hook, disabled hook), IMAP/SMTP accounts with fake
mailbox and SMTP objects, the per-account cloud switch, synthetic ICS with RRULE/EXDATE/TZID/override, the generic routes and login checks.
