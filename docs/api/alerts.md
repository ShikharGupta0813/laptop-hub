# Alerts (urgent-alert triage) and the Asana poller

Friday tells the owner the moment something urgent arrives, with a plan or a draft reply ready, spoken and pushed.
All routes need the login cookie (and `X-Hub: 1` for writes). Code: `hub/alerts.py`, `hub/asana.py`, `hub/alerts_tools.py`,
screen `js/alerts.js`. State: `alerts.json`, `asana_state.json` in the data folder.

## HARD RULE: nothing is sent for you
A reply is only a DRAFT. The screen offers **Copy** and an **Open the app** link (`https://wa.me/<number>?text=...`,
an Android share intent for Slack/Teams/Gmail/Google Chat, or `mailto:`) that the owner taps. WhatsApp cannot be sent from
the hub at all. No code path posts a message, comment or mail; there is no send tool for the assistant.

## Where events come from
1. Android notification capture: what already reaches `POST /api/watch/ingest` (Google Chat, Gmail) is also shown to
   triage through a one-line hook in `hub/watch.py`. `POST /api/alerts/ingest` takes the same shape for any other app
   (`{source, app, sender, title, text}`).
2. The Asana poller (below). 3. Mail through `hub/gmailx.py` when that module exists (`gmailx.list_accounts(ctx)`,
   `gmailx.search(ctx, account, query)`; result shape is UNVERIFIED, dict items with from/subject/snippet/id are assumed).
   Without gmailx this part is simply skipped.

## Android limits (honest)
Android shows the hub only the notification PREVIEW (a line or two) and only for apps the Java capture forwards. Today the
app's filter forwards Google Chat and Gmail; WhatsApp, Slack, Teams and the Asana app are recognised by the hub but need the
Android filter extended (a Java change not made here). Force-stopping the app makes Android stop the capture until it is
opened again. Alerts say "preview only" and never claim to know more.

## Classification (general, rules first, all editable)
Anything that needs the owner's attention or a reply raises an alert: **every mention or tag** (`@name`, "mentioned you",
your display name or an extra name, `mention: true` from the event), direct questions, requests ("send your ETA now",
"what is going on", "why is it breaking"), deadlines, urgent wording, VIP senders, assigned tasks. The owner can ignore an
alert; the hub never drops one of these. Marketing/codes (the `ignore_words` list) are dropped unless it is a mention, a VIP
or a task.

Priority (urgency 0-3) is computed from sender importance (VIP list), wording and time pressure: urgent words +2 (+1 for a
second), time pressure +1, due today +1, VIP +1 (+1 if they ask something), mention +1; min(3, points). Reasons are listed on
the card. Kind: `task | question | meeting | mention | other`; intent: `eta` or `why` (what a reply needs).

Everything is editable in Alerts > Settings and stored in config (nothing is hard-coded beyond the defaults an empty config
falls back to): `alerts_vips`, `alerts_sources` (the watched apps: a list the owner adds to and removes from; a name like
`whatsapp` or any fragment of an app package name; Asana, notifications, SMS and email all use this same pipeline),
`alerts_rules` (`urgent_words`, `time_words`, `ask_words`, `ignore_words`, `mention_words`; `reset_rules: true` restores the
defaults). An optional AI hook (constructor `classifier`, redacted through `hub.redact`) may only raise urgency, to at most 2.
Same sender + text within 10 minutes is one alert.

## Suggested next step (one generic path)
For every alert the hub drafts a reply from Brain memory plus the task plan, never sent:
- an **ETA / status** question finds the matching open Brain task and uses its plan and due time;
- **"why is it breaking" / any question or mention** searches memory (RCAs, decisions, notes, facts) and quotes the best match
  AS IT IS, labelled with its kind, title, date and id (`memory` field). An RCA request is just this case;
- both can appear in one draft. Nothing matched: an honest holding reply and `offer_note`; `POST /{id}/save-note` saves the
  message as an empty Memory note (tagged `alert-stub`, never quoted back) for the owner to fill in.
- **Task** alerts (Asana/Jira, or urgency >= 2): due date inferred, 3-6 step plan (template; with `alerts_ai` the free AI
  writes it from redacted text), an evidence list ("you will need: logs for ...") and a Brain task holding both.

## Delivery
Through `proactive.py` (event type `alert`): urgency 2-3 NOW (ntfy push, tap opens `#/alerts`), 1 batched, 0 listed only.
Quiet hours hold all but urgency 3. NOW alerts are spoken: the phone app polls `GET /api/alerts/pending` and speaks (only
while open) then acks; the laptop speaker says "<display_name>, urgent: ..." when `alerts_speak_on_laptop` is true (uses the
wake-word listener's speaker when present). Note: the ntfy layer has its own quiet hours (Settings > Notifications) that can
still mute the phone push.

## Routes (prefix /api/alerts)
`GET ""?status=open|all|new|seen|handled|snoozed` ; `GET|POST /settings` {vips, sources, rules, reset_rules, speak_on_laptop, enabled, ai, email_poll} ;
`POST /ingest` ; `POST /test` (fake critical alert end to end) ; `GET /pending` ; `POST /pending/ack` {ids} ; `GET /{id}` ;
`POST /{id}/status` {status} ; `POST /{id}/snooze` {minutes} ; `POST /{id}/draft` ; `POST /{id}/save-note`.

## Assistant tools
`list_alerts`, `show_alert(id)`, `draft_reply(id)`, `mark_alert(id,status)`, `snooze_alert(id,minutes)`; TOOL_GROUPS entry
`triage`. Alert text is untrusted data.

## Asana poller (`/api/asana`, off by default)
Config `asana_enabled`, `asana_poll_minutes` (5), `asana_workspace`. The Personal Access Token lives in the OS keyring
(service `redline-asana`), never in hub.json or a log; the Work tab's ASANA_TOKEN/file token is reused read-only if none is
saved. Routes: `GET /status`, `POST /connect {token}`, `/disconnect`, `/workspace {gid}`, `/enable`, `/test`, `/poll-now`.
Endpoints used: `/users/me`, `/tasks?assignee=me&workspace=..&completed_since=now`, `/tasks/{gid}/stories` (comments are
`resource_subtype: comment_added`). UNVERIFIED against a live account (built from docs and a fake HTTP layer). The first
poll only records a baseline; later polls alert on new tasks and on comments not written by the owner.

## Inbound sources (generalised)
Everything the alerts listen to is now a list in Alerts > Settings: notification apps (WhatsApp, Slack, Teams, Gmail, Google Chat, Asana, Jira, Trello,
Telegram, Outlook, SMS...), task sources (Asana, Jira, Trello, Linear, GitHub, RSS/Atom, inbound webhooks), mail accounts and calendar feeds, each
with add, switch, test and delete. See `docs/api/sources.md`. `alerts_sources` is kept in step with the enabled apps. New route:
`GET /api/alerts/capture-config` -> `{version, packages: [{package, label}]}` for the phone capture app. `hub.alerts.triage(ctx, event, trusted=False)`
gained `trusted` (only in-process pollers of a source the owner switched on pass it). The Asana poller is now the ready-made `asana` entry of the
task-source list; its routes and behaviour are unchanged. The mail poll (`poll_email`) now passes account ids (it used to pass the whole account
dict) and skips accounts whose `alerts` flag is off.
