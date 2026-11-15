# Google Chat channels API (personal Google account)

`hub/chatchan.py` (parsers, the owner's list, pending replies, routes, tools), hooks in `hub/alerts.py` (`ingest`), screen `js/chatch.js`
(Settings > Alerts > Google Chat channels, the Chat replies sheet, the "Reply in Google Chat" button on a Chat alert), Android
`HubCapture.openChat` (`ChatLink.java`, tests `mobile/scripts/test-chat-link.js`). Data (hub data folder): `chat_channels.json` (the list),
`chat_pending.json` (pending replies and the de-dupe memory). All routes need the login cookie. Errors are `{"detail": "one plain sentence"}`.

**The hard rule: this feature never sends a Chat message.** The owner has a personal Google account: no Workspace, no Chat incoming webhook, no
Chat app. REDLINE reads, drafts, and hands the text to the owner, who pastes and presses Send. `chat_send` (the older Jarvis-helper tool) is
untouched and stays in `safety.ALWAYS_ASK`; the two new tools cannot send.

## What each way can and cannot do
Also returned by `GET /api/chatch` as `capabilities` and shown in the Settings screen. "Verified" says how far the claim was checked.

| Way | Does | Cannot / limits | Verified |
|---|---|---|---|
| Read: Android notifications (`com.google.android.apps.dynamite`) | sender, space or DM, topic when the title has one, mention, de-dupe, a draft slot | only the preview line(s) the app shows, only with notification access and Chat on the capture list; Chat notifications carry no thread id (the listener's `conversation` is the space/shortcut id) | in this code and tests; not on a device |
| Read: Chat notification emails | the same fields from mail Chat sends about missed messages, through the mail modules (`gmailx` poll, or the Android Gmail notification) | needs Chat email notifications on in Chat and a mail account connected; late by nature; the subject/body shapes are a best guess | **NOT verified** against real Chat emails: use "Check a sample" |
| Reply: phone | the app copies the draft and opens Google Chat (the conversation if its link opens in the app, else Chat's home); you paste and Send | cannot paste or send for you; whether the Chat app claims `chat.google.com/...` links is unknown | Android intents are standard; app link handling **NOT verified** |
| Reply: laptop | the hub copies the draft to the laptop clipboard and opens `chat.google.com` (or the saved space link) in your own browser | same: you paste and Send; Linux needs `wl-copy`, `xclip` or `xsel` | yes (plain clipboard + browser open, tested with fakes) |
| Chat REST API with your own OAuth | **not offered** | whether Google lets a personal account call `spaces.messages.create` as a user is unknown, and Chat apps / incoming webhooks are Workspace features; no dead code shipped | **NOT verified**: developers.google.com was unreachable (blocked sandbox) when this was built. If you confirm a personal account works, a `rest` kind can be added to the list |
| Draft in the Chat web page (experimental, off) | a visible Playwright browser opens the space link and types the draft into the compose box, then stops | Google may refuse sign-in in an automated browser; automating a Google web app may breach its terms (account risk); the compose box selector may change (editable); never sends | **NOT verified** against the live page |

## Config
| Key | Default | Meaning |
|---|---|---|
| `chatch_enabled` | `true` | parse Chat messages into space/sender/mention, apply the list, keep pending replies (Panic pauses it) |
| `chatch_email_parse` | `true` | treat Chat notification emails as Chat messages |

Both are in the Settings allow-list and also `POST /api/chatch/settings {enabled?, email_parse?}`. The watched-source list of Alerts (`alerts_sources`)
must still contain `gchat` (it does by default) for notifications to be read.

## The list (`/api/chatch`)
Same list conventions as the provider lists (`hub/provlist.py`): `{id, kind, label, enabled, settings, has_secret:false}`, order is priority, nothing secret.

- `space` settings `{match: [words], link, mention_only}`. `match`: any word inside the space (or, for a DM, the person) name; empty = the entry's name. `link`: a
  `https://chat.google.com/...` or `https://mail.google.com/chat/...` address (anything else is refused), so Reply opens that conversation. **Switching a space
  off mutes it**: its messages raise no alert. `mention_only`: only mentions raise one (DMs always count).
- `email-source` settings `{from_contains: [...], subject_contains: [...]}`: which mail is a Chat notification. Starts with one entry, `chat-noreply@google.com` (a guess).
- `web-draft` settings `{selector}` (plain CSS, default `[contenteditable="true"][role="textbox"]`). Starts **off**.

Routes: `GET /api/chatch` (`entries`, `kinds` with form fields, `settings`, `capabilities`, `playwright`) ; `POST ""` `{kind, label, settings}` ; `PUT /order {ids}` ;
`POST /reset {confirm:true}` ; `PUT/DELETE /{id}` ; `GET /capabilities` ; `POST /parse-test` (a pasted sample, stores nothing: `{kind:"notification", title, text, sender, group, mention}`
or `{kind:"email", sender, subject, body}` returns `{parsed, note}`).

## Pending replies
Every alert that came from Google Chat gets one pending reply with its **own** draft: no field applies to more than one conversation.
`{id, alert_id, space, space_key, sender, topic, conversation, dm, mention, kind, intent, urgency, preview, via notification|email, created, status new|opened|done|dismissed,
draft, draft_by auto|owner|"", entry_id, link, opened_at, due}` plus on read `label` and `open` `{url, exact, android_intent, package, share_intent, note}`.

- `GET /api/chatch/pending?status=open|new|opened|done|dismissed|all` ; `GET /pending/{id}`
- `PUT /pending/{id}/draft {text}` the owner's words for this conversation only (max 4000 chars) ; `POST /pending/{id}/auto-draft` a fresh draft from Memory and the task plan (the Alerts
  engine; replaces only this item's draft)
- `POST /pending/{id}/prepare` -> `{text, open, item, instructions}`; the client copies `text` and opens `open.url` (phone: `HubCapture.openChat({text, url, share:false})`, else clipboard +
  `android_intent` on Android or `window.open`). Marks the item `opened`. Audit `chat_reply_prepared` holds the id and length, never the text.
- `POST /pending/{id}/open-laptop` -> `{copied, opened, url, note}`: the laptop's clipboard (text on stdin) and `webbrowser.open` of the space link or `https://chat.google.com/`. Only addresses that pass the Chat allow-list are opened.
- `POST /pending/{id}/web-draft {confirm:true}` experimental: needs an enabled `web-draft` entry, `confirm`, a draft, a saved space link (so the draft cannot land in the wrong conversation), Playwright installed
  (`pip install playwright` then `python -m playwright install chromium`; it reuses `hub/formfill.py`'s browser launch, its public-https navigation guard) and a dedicated profile folder `chatweb_profile`
  (sign in yourself in that window; the hub never reads credentials). The window is always visible, the code only calls `fill` on the compose box (a test fails if it ever presses a key or clicks), and the window stays
  open 30 minutes or until you close it. Result `state`: `typed`, `needs_login`, `not_found`, `failed`.
- `POST /pending/{id}/status {status}` ; `POST /pending/{id}/add-channel` (adds a `space` entry for it) ; `POST /api/chatch/from-alert/{alert_id}` (a pending reply for an older Chat alert).

## How Alerts uses it
`Alerts.ingest` calls `chatchan.pre_ingest` after cleaning the event: a `gchat` event is parsed (title/text/sender/`group`/`conversation`/`mention` from the Android listener, or the older watch capture where
a leading `Name: ` marks a space message), a mail event that matches an enabled `email-source` entry is parsed as a Chat email and re-labelled `gchat`. Muted spaces and mention-only spaces drop the event. One
message seen both ways is one alert: a **6 hour** de-dupe on (space, sender, text) outlives the Alerts engine's 10 minute one. After the alert is built, `chatchan.register` stores its pending reply
(`alert.chat_pending`, `alert.chat` {space, sender, dm, topic, mention, via, space_key}). `chatch_enabled: false` leaves Alerts exactly as before.
`alerts_rules.mention_words` and the display name are what "mention" means. `@all`, `@everyone`, `@here` are flagged `broadcast`, not a personal mention.

## Assistant tools (voice and text use the same ones)
- `chat_list_pending` - open Chat messages, each with whether it has its own draft (text is data, taints the turn).
- `chat_draft_reply {id, text?}` - set the exact words for one conversation, or without `text` a fresh draft from Memory. Never sent: the owner taps Reply on the Chat replies screen.
Tool group `chatchan` (pattern: google chat, chat reply/draft, "mentioned me in"). There is no send tool; `chat_send` still asks every time.

## Android
`HubCapture.openChat({text, url, share})` copies `text` to the clipboard, then opens Chat: `url` if `ChatLink.isAllowed` (https, `chat.google.com`, or `mail.google.com/chat`) with the Chat package first and the browser as a fallback,
or the Chat share picker when `share` is true, else the Chat app launcher. Resolves `{copied, opened: app|browser|launcher|none}`. No accessibility service, no auto-paste, no send. `ChatLink.java` is plain Java (compiled and
checked on a JVM, mirrored by `node mobile/scripts/test-chat-link.js`); the plugin method itself was not built here (no Android SDK).
