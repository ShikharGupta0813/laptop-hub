# Mail accounts API (Gmailx): Gmail and any IMAP/SMTP server

`hub/gmailx.py` (service, routes, the functions other modules import), `hub/gmailx_tools.py` (assistant tools),
`hub/mailaccounts.py` (IMAP/SMTP helpers), `js/gmailx.js` (Settings card "Mail accounts"). All routes need the login cookie; every POST/PUT/DELETE also needs `X-Hub: 1`.
Errors are `{"detail": "one plain sentence"}`.

## Accounts
| id | what it is | tokens |
|---|---|---|
| `default` | The mailbox the hub already had: Gmail address + **app password** kept by the Jarvis mailer (keyring service `jarvis-gmail`). Label `personal`. Read over IMAP (same code as `hub/inbox.py`), sent through the Jarvis mailer. Nothing is migrated, copied or re-asked, so it keeps working after the upgrade. `kind: "legacy"`, cannot be disconnected here. | untouched |
| `g<8 hex>` | Extra accounts connected with Google OAuth (installed-app flow, PKCE, refresh token). `kind: "oauth"`. Up to 4. | OS keyring, service `redline-gmail`, key = account id (JSON: refresh token, access token, expiry, granted scopes). Never in `hub.json`, the registry file, the audit log or any response. |

| `imap-<slug>` | A generic mail-server account (Outlook / Office 365, Yahoo, Zoho, iCloud, a company server): `kind: "imap"`, host, port and security (`ssl`, `starttls`, `none`) for IMAP and for SMTP, a user name and a password or app password. Reading goes through `inbox.ImapMailBox` (readonly folder, `BODY.PEEK`). Up to 12. | OS keyring, service `redline-gmail`, key `imap:<id>`. Never in a file, log or response (a view has `has_password`). |

The registry (labels, tags, `cloud_ai_allowed`, `alerts`, server settings, `send_enabled`, address, `needs_reconnect`; no secrets) is `<data dir>/gmailx.json`. A missing `default`
entry is recreated on read (label `personal`, send on).

NOTE: the repo had no Google OAuth flow before this module (the single connection was the app password), so the OAuth flow below
is new. Google's "limited-input device" flow does not allow Gmail scopes, so the loopback flow is used.

## Scopes (never broader than needed)
`gmail.readonly` always. `gmail.send` only after "allow sending from this account" is on and the owner has consented again.
Never `mail.google.com`. `scopes` in the account list is what Google actually granted (the app-password account lists
`imap: read-only (app password)` and `smtp: send (app password)`).

## Routes
- `GET /api/gmail/accounts` -> `{accounts: [{id, label, address, connected, scopes, kind, send_enabled, reconsent_required, tags, cloud_ai_allowed, cloud_ai_default, cloud_ai_explicit, alerts, protected}], company_ai_allowed, client_configured, presets}`
- `POST /api/gmail/accounts/connect` `{label, send_enabled?: false, account_id?, client_id?, client_secret?}` -> `{auth_url, state, redirect_uri, scopes, expires_in}`.
  Starts the sign-in: a one-shot listener on `127.0.0.1` (random port, 10 min) catches Google's redirect. `account_id` = re-consent that account
  (label and address must match). `client_id`/`client_secret` (a Google "Desktop app" OAuth client) are saved to the keyring when given.
- `GET /api/gmail/accounts/connect/{state}` -> `{status: pending|done|error, error, account_id}` (the UI polls this).
- `POST /api/gmail/accounts/connect/complete` `{state, code | redirect_url}`: finish by hand when the phone and laptop differ (paste the final address).
- `POST /api/gmail/accounts/imap` `{label, address, password, preset?: outlook|yahoo|zoho|icloud|custom, imap_host, imap_port, imap_security, smtp_host?, smtp_port?, smtp_security?, username?, tags?, send_enabled?, cloud_ai_allowed?}` -> the account. Signs in once to check the password; a failed login is not kept. `GET /accounts` also returns `presets`.
- `PUT /api/gmail/accounts/{id}` `{label?, send_enabled?, tags?: [free text chips], cloud_ai_allowed?: true|false|null (null = default), alerts?: bool, password?, imap_host?, ...}` -> the account (`reconsent_required: true` when sending was switched on but Google has not granted `gmail.send`).
- `DELETE /api/gmail/accounts/{id}`: forgets the tokens (keyring entry deleted), asks Google to revoke the refresh token, removes the account. `404` unknown, `400` for `default`.
- `GET /api/gmail/summary` -> `{account_id: {label, address, unread, important, items: [top 5 unread]}}`
- `GET /api/gmail/search?q=&account=&limit=` -> `{results: [item]}`
- `POST /api/gmail/send` `{account, to, subject, body, confirm: true}` -> `{ok, error, message_id}`; `403` when the account is not send-enabled.

Item shape (search, recent, summary items): `{account (id), tag (personal|company|label), id, thread, sender, subject, snippet, date, labels}`.
Bodies are never in these lists.

## Functions for other modules (blocking; from async code use `asyncio.to_thread`)
`list_accounts(ctx)`, `send_mail(ctx, account_id, to, subject, body, attachments=None) -> {ok, error, message_id}` (never raises),
`search(ctx, account_id_or_None, query, max_results=20)`, `recent(ctx, account_id_or_None, hours=24)`, `summary(ctx)`,
plus `get_message(ctx, account_id, message_id, destination="local"|"cloud"|<provider>)`, `account_tag(ctx, id)`,
`cloud_ai_allowed(ctx, id)`. `None` as the account = every connected account, merged newest first. Errors raise `GmailxError(message, status)`.
`attachments`: file paths or `{filename, data (bytes) | path, mime_type}`, 20 MB total.

## Tags and the per-account cloud-AI switch
Tags are free text chips (`personal`, `company`, `client-x`, anything; up to 8 per account); `label` is just the display name (the tag list defaults to the label).
`cloud_ai_allowed` is a switch PER ACCOUNT. When the owner never set it, it defaults to `false` for an account tagged `company` or `work` (then the old
global `gmail_company_ai_allowed` may still turn it on) and to `true` for any other tag; `PUT {cloud_ai_allowed: null}` goes back to the default. `protected` in
a view means "cloud AI not allowed". With the switch off, `get_message(... destination != local)` returns the body empty with `body_withheld: true`;
Autopilot does not fetch a body or ask a cloud AI for it; `/api/inbox` summary, digest and draft-reply refuse a cloud model (403) for the default mailbox.
Sender, subject and the snippet are still used (metadata). When the switch is on the body goes through `hub.redact` first. Check your company's policy first.

## Config (hub.json)
| key | default | meaning | Settings |
|---|---|---|---|
| `gmail_company_ai_allowed` | `false` | default for accounts tagged company or work that have no switch of their own | yes (the per-account switches in the Mail accounts card are the main control) |

## Sending
`send_mail` only runs when called. IMAP/SMTP accounts send through `smtplib` (SSL or STARTTLS) only when "allow sending" is on and an explicit call is made; reading never sends. The assistant's `send_email` tool is still approval-gated (N2) and now takes `account`: required when two or more
accounts are connected; refused with a clear sentence when the account is unknown, not connected or not send-enabled. The approval card shows
`from_account`. The `default` account still sends through the Work/Jarvis mailer (draft then send).

## Assistant tools (`hub/gmailx_tools.py`, read-only, free, untrusted)
`gmail_summary(account?)`, `gmail_search(query, account?)`: results are tagged `[personal]`/`[company]`, contain only sender, subject, snippet and date,
and taint the turn like other mail reads (`hub/assistant.py` taint rules).

## Autopilot / Today
Autopilot cards show an account chip (`account` in the card view). Connected OAuth accounts add cards (`source: "inbox"`, key `inbox:<account>:<message id>`,
`action_kind: "none"`) with no AI call. Today's `mail.accounts` lists unread per account when there are two or more.

## Not verified here
Real Outlook / Office 365 / Yahoo / Zoho / iCloud sign-ins (the presets come from the providers' public help pages; Microsoft is switching off basic-auth IMAP/SMTP for many tenants), STARTTLS and SMTP sending against a real server (tests use fake mailbox and SMTP objects). Real Google sign-in, consent-screen rules, token refresh against Google, a real Workspace admin policy, the loopback listener in a real browser, the
Settings sheet on a phone. Tests use a fake Google, a dict for the keyring and a stub for the app-password mailbox (`tests/test_gmailx.py`,
`tests/test_gmailx_autopilot.py`).
