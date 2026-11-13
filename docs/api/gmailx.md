# Gmail accounts API (Gmailx)

`hub/gmailx.py` (service, routes, the functions other modules import), `hub/gmailx_tools.py` (assistant tools),
`js/gmailx.js` (Settings card). All routes need the login cookie; every POST/PUT/DELETE also needs `X-Hub: 1`.
Errors are `{"detail": "one plain sentence"}`.

## Accounts
| id | what it is | tokens |
|---|---|---|
| `default` | The mailbox the hub already had: Gmail address + **app password** kept by the Jarvis mailer (keyring service `jarvis-gmail`). Label `personal`. Read over IMAP (same code as `hub/inbox.py`), sent through the Jarvis mailer. Nothing is migrated, copied or re-asked, so it keeps working after the upgrade. `kind: "legacy"`, cannot be disconnected here. | untouched |
| `g<8 hex>` | Extra accounts connected with Google OAuth (installed-app flow, PKCE, refresh token). `kind: "oauth"`. Up to 4. | OS keyring, service `redline-gmail`, key = account id (JSON: refresh token, access token, expiry, granted scopes). Never in `hub.json`, the registry file, the audit log or any response. |

The registry (labels, `send_enabled`, address, `needs_reconnect`; no secrets) is `<data dir>/gmailx.json`. A missing `default`
entry is recreated on read (label `personal`, send on).

NOTE: the repo had no Google OAuth flow before this module (the single connection was the app password), so the OAuth flow below
is new. Google's "limited-input device" flow does not allow Gmail scopes, so the loopback flow is used.

## Scopes (never broader than needed)
`gmail.readonly` always. `gmail.send` only after "allow sending from this account" is on and the owner has consented again.
Never `mail.google.com`. `scopes` in the account list is what Google actually granted (the app-password account lists
`imap: read-only (app password)` and `smtp: send (app password)`).

## Routes
- `GET /api/gmail/accounts` -> `{accounts: [{id, label, address, connected, scopes, kind, send_enabled, reconsent_required, protected}], company_ai_allowed, client_configured}`
- `POST /api/gmail/accounts/connect` `{label, send_enabled?: false, account_id?, client_id?, client_secret?}` -> `{auth_url, state, redirect_uri, scopes, expires_in}`.
  Starts the sign-in: a one-shot listener on `127.0.0.1` (random port, 10 min) catches Google's redirect. `account_id` = re-consent that account
  (label and address must match). `client_id`/`client_secret` (a Google "Desktop app" OAuth client) are saved to the keyring when given.
- `GET /api/gmail/accounts/connect/{state}` -> `{status: pending|done|error, error, account_id}` (the UI polls this).
- `POST /api/gmail/accounts/connect/complete` `{state, code | redirect_url}`: finish by hand when the phone and laptop differ (paste the final address).
- `PUT /api/gmail/accounts/{id}` `{label?, send_enabled?}` -> the account (`reconsent_required: true` when sending was switched on but Google has not granted `gmail.send`).
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

## Company-mail privacy
Any account whose label is not `personal` is protected. With `gmail_company_ai_allowed` false (default), `get_message(... destination != local)`
returns the body empty with `body_withheld: true`; Autopilot does not fetch a body or ask a cloud AI for it; `/api/inbox` summary, digest and
draft-reply refuse a cloud model (403) when the default mailbox is relabelled. Sender, subject and Gmail's snippet are still used (metadata). When
the flag is true the body goes through `hub.redact` first (emails, numbers, keys...). The Settings card says this in plain words.
Check your company's policy first.

## Config (hub.json)
| key | default | meaning | Settings |
|---|---|---|---|
| `gmail_company_ai_allowed` | `false` | company-account mail bodies may be sent to a cloud model | yes (switch in the Gmail accounts card) |

## Sending
`send_mail` only runs when called. The assistant's `send_email` tool is still approval-gated (N2) and now takes `account`: required when two or more
accounts are connected; refused with a clear sentence when the account is unknown, not connected or not send-enabled. The approval card shows
`from_account`. The `default` account still sends through the Work/Jarvis mailer (draft then send).

## Assistant tools (`hub/gmailx_tools.py`, read-only, free, untrusted)
`gmail_summary(account?)`, `gmail_search(query, account?)`: results are tagged `[personal]`/`[company]`, contain only sender, subject, snippet and date,
and taint the turn like other mail reads (`hub/assistant.py` taint rules).

## Autopilot / Today
Autopilot cards show an account chip (`account` in the card view). Connected OAuth accounts add cards (`source: "inbox"`, key `inbox:<account>:<message id>`,
`action_kind: "none"`) with no AI call. Today's `mail.accounts` lists unread per account when there are two or more.

## Not verified here
Real Google sign-in, consent-screen rules, token refresh against Google, a real Workspace admin policy, the loopback listener in a real browser, the
Settings sheet on a phone. Tests use a fake Google, a dict for the keyring and a stub for the app-password mailbox (`tests/test_gmailx.py`,
`tests/test_gmailx_autopilot.py`).
