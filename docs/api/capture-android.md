# Android automatic spend capture (`HubCapture` plugin)

The Android app (`mobile/`) contains a native Capacitor plugin named **HubCapture**. It watches payment-app
notifications (and, if you allow it, bank SMS), keeps only the ones that look like money you spent, and posts
each one to your own hub. The hub page controls it from JavaScript.

## How the page calls it

The app opens the remote hub URL inside the Capacitor WebView, so the plugin is reached through the object
Capacitor injects into the page. There is no npm package to import.

```js
// Only inside the Android app. In a normal browser Capacitor is undefined.
const cap = window.Capacitor;
const HubCapture =
  cap && typeof cap.isNativePlatform === "function" && cap.isNativePlatform()
    ? cap.Plugins && cap.Plugins.HubCapture
    : null;
if (HubCapture) window.HubCapture = HubCapture;      // optional alias for the hub front end

await HubCapture.status();
```

Fallback if `Capacitor.Plugins.HubCapture` is ever missing:
`await window.Capacitor.nativePromise("HubCapture", "status", {})`.

The plugin is registered in `MainActivity.java` (`registerPlugin(HubCapturePlugin.class)` before
`super.onCreate`). All methods return Promises.

## Methods

| Call | Result | What it does |
|---|---|---|
| `HubCapture.status()` | `{ listenerEnabled, smsGranted, enabled, watchMail, watchChat, queued, hubOrigin }` | `listenerEnabled`: Android "Notification access" is on for the app. `smsGranted`: RECEIVE_SMS granted. `enabled`: the in-app switch (default `true`). `watchMail` / `watchChat`: Gmail / Google Chat watching (default `false`, see below). `queued`: items waiting to be sent (payments and watch items). `hubOrigin`: the `https://host[:port]` the app was built for, or `null`. |
| `HubCapture.openListenerSettings()` | `{}` | Opens Android's Notification access screen. The user switches **Laptop Hub** on. |
| `HubCapture.requestSms()` | `{ granted }` | Shows Android's SMS permission dialog (RECEIVE_SMS only, never READ_SMS). Optional. |
| `HubCapture.setEnabled({ enabled: true })` | same as `status()` | Turns capture on or off without touching Android settings. |
| `HubCapture.setWatch({ mail, chat })` | same as `status()` | Turns Gmail (`mail`) and Google Chat (`chat`) watching on or off, independently of payment capture and of each other. A key that is left out keeps its current value. Both are **off** until the hub page turns them on. Needs the same Notification access as payment capture. |
| `HubCapture.flush()` | `{ sent, queued, error }` | Tries to send everything in the queue now. |
| `HubCapture.test()` | `{ sent, queued, error }` | Fakes one payment (`Paid ₹1 to Laptop Hub Test`, `test: true`) and sends it through the full path. |

`error` in `sent/queued/error` results is `null` or one of `no-https-origin`, `not-logged-in` (the WebView holds no
hub cookie yet), `network`, `http-401`, `http-403`, `http-5xx` and similar. When `error` is set, the item stays queued.

Typical set-up flow in the page:

```js
let s = await HubCapture.status();
if (!s.listenerEnabled) await HubCapture.openListenerSettings();   // then re-check status() when the page regains focus
if (!s.smsGranted)      await HubCapture.requestSms();             // optional
const r = await HubCapture.test();                                  // expect { sent: true, queued: 0, error: null }
```

## What the phone sends to the hub

`POST <hub origin>/api/expenses/capture`

Headers: `Content-Type: application/json`, `X-Hub: 1`, `Cookie: <the hub login cookie from the WebView>`.
No new credential is stored; if the user is not logged in inside the app, items wait in the queue.

Body (one item per request, `ts` is epoch milliseconds):

```json
{ "source": "notification", "package": "com.phonepe.app", "title": "Payment Successful",
  "text": "Paid ₹120 to Cafe Coffee Day", "ts": 1767225600000 }
```
```json
{ "source": "sms", "sender": "VM-HDFCBK", "title": "",
  "text": "Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO On 01/01/25 ...", "ts": 1767225600000 }
```
`test()` adds `"test": true` (and `package` is the app's own id).

Response handling: any 2xx removes the item from the queue. 401, 403, 408, 429, 5xx and network failures keep it
queued for a retry. Any other 4xx means the hub rejected that item for good, so it is dropped (it does not block the
rest). Redirects are not followed.

## What is forwarded (and what never is)

* Notifications: only from an allow-list of packages (`CaptureFilter.ALLOWED_PACKAGES`): GPay, PhonePe, Paytm, BHIM,
  CRED, Amazon, and common bank apps. Any other app is dropped before its text is read.
* SMS: only after the user grants the permission; the sender must be a lettered header (not a phone number) and the
  body must look like a UPI/bank debit.
* Text must contain a debit word (paid, sent, debited, spent, withdrawn, payment of/successful) **and** an amount
  (₹, Rs, INR followed by a number). Anything mentioning received, refund, cashback, request, reminder, offer, OTP,
  reward, failed, declined, reversed, "sent you" or "paid you" is dropped. "credited" is dropped unless the same text
  also says "debited" (some banks list both sides).
* The same (app or sender, text, minute) is sent once. The hub should still dedupe on its side (a notification and
  the matching bank SMS arrive separately).
* Queue: stored in the app's private SharedPreferences, at most 200 items, oldest dropped first. Flushed after every
  capture, when the app comes to the foreground, and on `flush()`. HTTPS only, 10 second timeouts, nothing is logged.

The regex table lives in `CaptureFilter.java` and is mirrored and checked by `mobile/scripts/test-capture-filter.js`.

## Mail and chat watching (`POST /api/watch/ingest`)

The same notification listener can also forward message notifications from **Google Chat**
(`com.google.android.apps.dynamite`) and **Gmail** (`com.google.android.gm`) to a second endpoint. It is separate
from payment capture: its own allow-list (`WatchFilter`), its own flags, and nothing is read or sent for an app
whose flag is off. The hub front end calls `HubCapture.setWatch({ mail: true, chat: true })` when the user enables
monitoring. Slack and WhatsApp are not watched.

`POST <hub origin>/api/watch/ingest`, same headers and cookie as above. One item per request:

```json
{ "source": "chat", "app": "com.google.android.apps.dynamite", "title": "Team space",
  "text": "Alice: standup moved to 11", "ts": 1767225600000 }
```
```json
{ "source": "mail", "app": "com.google.android.gm", "title": "Alice <alice@example.com>",
  "text": "Invoice due - Please pay by Friday", "sender": "Alice <alice@example.com>", "ts": 1767225600000 }
```

* `source` is `"chat"` or `"mail"`; `app` is the package. `ts` is epoch milliseconds. `sender` is only sent for mail
  (Gmail puts the sender in the notification title); for chat the title is the space or person and the sender, if
  any, is inside `text`.
* Mail `text` is the subject plus the snippet. `title` is trimmed to 200 characters and `text` to 1000.
* Dropped on the phone: group-summary notifications ("3 new messages"), ongoing notifications, this app's own
  notifications, and anything with an empty title and text.
* The same (package, title, text) within 2 minutes is sent once. A message dropped because its flag was off does not
  count as seen.
* Delivery uses the same queue and retry rules as payments (response handling above). Each queued entry remembers its
  path, and entries queued by older app versions (no path) are sent to `/api/expenses/capture`. When the 200-item
  cap is exceeded the oldest watch item is dropped before any payment item.

The routing rules live in `WatchFilter.java` and are mirrored and checked by `mobile/scripts/test-watch-filter.js`.
