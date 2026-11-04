# Automatic spend capture (`hub/life.py`, expenses)

The Android app (see `capture-android.md`) forwards payment notifications and bank SMS to the hub. The hub parses
them, queues each one as a **pending payment**, tells the user once, and files it as a normal expense when the user
picks a category (or by itself, if the user turned that on). Everything needs the login cookie; every POST needs `X-Hub: 1`.
Errors are `{"detail": "<one plain sentence>"}`. Money is integer cents; `amount_text` is for display.

Categories (`GET /api/expenses/categories`): `food groceries transport bills shopping health entertainment travel rent education transfers other`.
Ids are lower-case; any other word the user types is accepted and remembered too.

## Phone to hub

### `POST /api/expenses/capture`

Body (what the app sends; `ts` is epoch milliseconds, seconds are understood too, a missing/absurd `ts` means "now"):
`{source:"notification"|"sms", package?, sender?, title?, text?, ts?, test?:bool}`.
`text` up to 50,000 characters, `title` 5,000; only the first 2,000 / 300 are ever read. Other fields are optional or null-safe.

Response (always `200` unless noted, so the phone drops the item):

```json
{ "id": "3f9c0a1b2d4e", "parsed": { "amount_cents": 45000, "amount_text": "450.00", "currency": "INR",
    "merchant": "Swiggy", "merchant_raw": "swiggy@ybl", "merchant_key": "swiggy", "app": "Google Pay",
    "bank": "HDFC Bank", "account_last4": "1234", "ref": "612345678901" },
  "duplicate": false, "suggested_category": "food", "status": "pending", "auto": false, "ignored": false }
```

* Not a clear debit (credit, refund, cashback, request, reminder, offer, OTP, failure, "will be debited", a balance alone, no amount),
  or capture switched off: `{id:null, parsed:null, duplicate:false, suggested_category:null, status:null, auto:false, ignored:true}`. Nothing is stored or announced.
* `duplicate:true`: this is the same payment as `id` (same reference and amount; or the exact same item re-sent; or a notification and an SMS
  with the same amount and payee within 3 minutes, one of each source). The existing item gets the missing details (bank, last4, ref, payee) and the new source;
  nobody is notified again.
* On an ignore-listed payee: stored as `dismissed`, `ignored:true`, no notification.
* `status:"confirmed", auto:true`: filed automatically (see settings). `suggested_category` is the category used.
* `429` "Too many payments came in this hour, so I am pausing. They will be tried again later." after 120 calls in a rolling hour (every call counts, parsed or not).
* `422` for a bad `source` or oversized text.

For each **new** item (not duplicates, not ignored) one `ctx.notify(title="₹450 to Swiggy", body="Tap to choose a category", level="info", link="expenses", source="expenses")`.
No merchant gives `"₹450 spent"`; other currencies read `"USD 12.50 to Spotify"`. Auto-filed items notify quietly: body `"Added to Food automatically. Tap to change it."`.
Test payments (`test:true`) are stored, flagged, announced like real ones, never merged with real ones, never auto-filed, and dismissed after one hour.

### What the parser understands

Amounts: `₹450`, `Rs.450`, `Rs 450.50`, `INR 450`, `450 rupees`, `Rs 1,25,000.50` (Indian grouping), `Rs 1,234,567`, `debited by 250.0`; with several amounts it takes the one next
to a debit word and ignores balances/limits/totals (`Avl Bal Rs 12,000`). Payee: `paid to X`, `sent to X`, `to VPA x@ybl`, `You paid X`, `at MERCHANT`,
`trf to X`, `Info: UPI/DR/<ref>/MERCHANT/...`, `...; ZOMATO credited` (bank lists both sides). The name is title-cased for display (`merchant`), the original kept in `merchant_raw`,
and `merchant_key` (`swiggy` for `SWIGGY`, `Swiggy Pvt Ltd`, `swiggy@okaxis`) is what categories are learned under. Also bank/app (from package or SMS sender header), last 3-4 digits of the account/card, and the UPI reference.
Other currency codes seen: USD EUR GBP AED SGD. Hostile or non-UTF text never raises; control characters are stripped, names are capped at 60 characters.

## Pending queue

Item (`public pending`): `{id, amount_cents, amount_text, currency, merchant, merchant_raw, merchant_key, source, sources:[..], app, bank, account_last4, ref,
raw_snippet, ts, date, status:"pending"|"confirmed"|"dismissed", auto, test, suggested_category, category, expense_id, created}`.
`raw_snippet` is at most 160 characters with account numbers, long digit runs and UPI ids masked (`ra•••@okhdfc`, `••••`); the full message text is never stored or logged.
`date` is the payment's local date. `suggested_category` for pending items is live (learned choice first, else a guess from the name, else `null`). `category`/`expense_id` are set once filed.

| Route | Body / query | Response |
|---|---|---|
| `GET /api/expenses/pending` | `?status=pending\|confirmed\|dismissed\|auto\|all` (default `pending`), `&limit=1..500` (default 100) | `{status, items:[item], pending_count}` newest payment first. `auto` = filed automatically and not yet fixed. `400` for another status |
| `POST /api/expenses/pending/{id}/confirm` | `{category, note?, amount_cents?, merchant?}` | `{ok:true, item, expense}` — `expense` is the normal expense (`{id, amount, amount_text, currency, category, note, date, tags:["upi"]}`), dated from the payment time. `note` defaults to the merchant (or "UPI payment"). The choice is learned |
| `POST /api/expenses/pending/{id}/dismiss` | | `{ok:true, item}` (repeatable; `409` if already filed) |
| `POST /api/expenses/pending/dismiss-all` | `{confirm:true}` | `{ok:true, dismissed:n}` — only waiting ones. `409` without `confirm` |
| `GET /api/expenses/categories` | | `{categories:[{id,label}], default:"other"}` built-ins first, then any others in use |

Confirm details: a second confirm of a manually filed item is `409` "That payment is already in your expenses."; a dismissed one can still be filed;
a test payment is marked confirmed but creates no expense (`expense:null`) and teaches nothing. Confirming an **auto-filed** item (`auto:true`) is how the user fixes it: the existing
expense is edited in place, the wrong lesson is taken back, the new one learned, `auto` becomes false. Unknown or malformed ids are `404` "That payment was not found."; `422` for bad bodies.
Over-budget warnings fire as for any new expense.

## Category memory and auto-filing

`merchant_categories {merchant_key, category, count, last_used}`: each confirm adds one to that payee/category. The suggestion is the most used category (ties: most recent).

`GET|POST /api/expenses/capture/settings` -> `{enabled:true, auto_confirm_after:0, ignore_merchants:[]}`. POST takes any subset:
`enabled` (bool; off = nothing is stored), `auto_confirm_after` (0-50; 0 = off), `ignore_merchants` (up to 100 names; matched by merchant key, so `Swiggy` also covers `swiggy@ybl`; duplicates and control characters removed).
With `auto_confirm_after = N`, a new payment is filed without asking only when its payee has been confirmed at least N times into one category and never into another. Auto-filed
items stay visible under `?status=auto`. Test payments and ignored payees are never auto-filed.

## Elsewhere

* `GET /api/life/summary` gains `pending_count` (waiting, non-test payments).
* Quick expense parser: Indian digit grouping (`₹1,25,000 rent`), `yday`, `last night`, `the day before yesterday`, `N weeks ago`, weekday abbreviations with `last/on/this` (`last fri`, `this monday`; a bare `sun` or `sat` stays a word), `tomorrow` is refused as the future.
* Audit: `life_capture` (new item), `life_capture_confirm`, `life_capture_dismiss`, `life_capture_dismiss_all`, `life_capture_settings`; item events carry only `id, amount, merchant`.
* Storage (`life.sqlite`): `pending_expenses` (handled items are forgotten after 90 days, max 5,000 rows), `merchant_categories`, `capture_settings`.
