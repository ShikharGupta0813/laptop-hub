# Laptop Hub for Android

A real Android app (an APK) that opens your Laptop Hub. It is a thin native shell around the hub's own
HTTPS page, so every screen you use in the browser works the same here, with a normal app icon, no browser
bars, and a native microphone permission. All logic stays on your laptop.

Why a shell and not a rewrite: the whole interface is already built and tested as a web app. A shell reuses
all of it, builds in minutes, and stays in sync with the laptop. Features that need real native code (a
background "Hey Jarvis" wake word, notifications) are native plugins; see the sections below and **Later**.

## Easiest: let GitHub build it (no Android Studio, nothing to install)

1. Get your hub's **HTTPS address**: on the laptop run `tailscale serve status`. It looks like
   `https://yourlaptop.tail1234.ts.net`. The app needs HTTPS (the microphone requires it).
2. Open the repo on GitHub: **Actions** tab, **Build Android APK** in the list on the left, **Run workflow**,
   paste the address, press the green button.
3. Wait 5 to 10 minutes. Open the finished run (green tick) and download **laptop-hub-debug-apk** at the bottom
   (a zip with `app-debug.apk` inside). Open it on the phone.
4. Install it (see below), open **Laptop Hub**, allow the microphone, and log in with your hub token.

If you change the address later, run the workflow again with the new one.

**Updating the app:** every build is signed with a fresh debug key, so Android will not install a new build over an
old one ("App not installed"). Uninstall the old Laptop Hub first. Your data is on the laptop, so nothing is lost.

## Or build it yourself on your laptop

### What you need

1. **Node.js 22 or newer** (`node --version`).
2. **Android Studio** (it brings the Android SDK and a Java runtime). Open it once and let it finish
   installing the SDK.
3. Your hub's **HTTPS address** from `tailscale serve status`, like `https://yourlaptop.tail1234.ts.net`.
   The app cannot work without HTTPS (the microphone needs it).

### Build the APK (Windows PowerShell)

```powershell
cd mobile
npm install
$env:HUB_URL = "https://yourlaptop.tail1234.ts.net"
npm run sync
cd android
.\gradlew.bat assembleDebug
```

If Gradle says it cannot find Java, set it to the one Android Studio ships:

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
```

The APK is `mobile\android\app\build\outputs\apk\debug\app-debug.apk`.

## Install it on your phone

- **Cable:** turn on Developer options and USB debugging, plug in, then `adb install -r app\build\outputs\apk\debug\app-debug.apk`
  (`adb` is in the Android SDK `platform-tools` folder).
- **No cable:** copy the APK to the phone (Drive, Telegram, USB), open it, and allow "Install unknown apps"
  for that app when Android asks.

Open **Laptop Hub**, allow the microphone when asked, and log in with your hub token. Tailscale must be
running on the phone.

## Automatic spend capture (optional)

The app can add your spending to the hub's expense list by itself, from the notifications your payment apps
already show (GPay, PhonePe, Paytm, BHIM, CRED, Amazon and common bank apps) and, if you choose, from bank SMS.

**What is captured.** Only notifications from that fixed list of apps, and only when the text looks like money
you spent (for example "Paid Rs 250 to ..."). Money you received, refunds, cashback, payment requests, reminders,
offers and OTPs are ignored on the phone and never sent. Other apps are only read through the separate, owner-controlled alerts list described in **Alerts from any app** below.

**Privacy.** Only payment-app notifications and bank SMS that pass those checks are forwarded, and only to your own
laptop hub, over HTTPS, using the login you already have inside the app. Nothing goes to any other server, no new
password is stored, and notification text is never written to the phone's log. Not-yet-sent items wait in a small
private queue on the phone (200 at most) and are sent when the hub is reachable again. The SMS permission is
RECEIVE_SMS only (the app cannot read your inbox) and is off until you grant it.

**Turn it on** (the hub page's capture settings call the app for these steps):

1. Open **Laptop Hub**, log in, and open the capture settings in the hub. Press the button that opens
   **Notification access**, or go there yourself: **Settings, Notifications, Device and app notifications**
   (on some phones **Settings, Apps, Special app access, Notification access**). Switch **Laptop Hub** on.
2. Android 13 and newer block this for apps installed from an APK ("Restricted setting"). If the switch is greyed out
   or shows a warning: **Settings, Apps, Laptop Hub, the three-dot menu (top right), Allow restricted settings**, then
   try step 1 again. The same applies to the SMS permission.
3. Optional, for bank SMS: press the SMS button in the hub's capture settings and allow SMS.
4. Press **Test** in the hub. A fake "Paid Rs 1" item should appear in your expenses.

**Keep it running.** Some phones stop background apps. Set **Settings, Apps, Laptop Hub, Battery** to
**Unrestricted** (or "Don't optimise"), and on Xiaomi, Oppo, Vivo, OnePlus and Samsung also allow auto-start / disable
"put unused apps to sleep". If a capture is missed because the phone was asleep or offline it is not recovered, but
anything that was captured and could not be sent is retried when you next open the app.

Developers: the JS calls are documented in `docs/api/capture-android.md`. The filter rules are in
`android/app/src/main/java/dev/shikhar/laptophub/capture/CaptureFilter.java`, and `node scripts/test-capture-filter.js`
checks them against sample notifications and confirms the Java and JS copies match.

## Notification-capture plugin: quick facts

The app includes a native plugin called **HubCapture** (`android/app/src/main/java/dev/shikhar/laptophub/capture/`).

- **What it forwards, and only to your own hub:** (1) payment-app notifications that look like money you spent, and optional bank SMS, to `/api/expenses/capture`; (2) only if you switch watching on in the hub, Gmail and Google Chat message notifications to `/api/watch/ingest`. Every other app is read only if you enabled it in the Alerts list (see **Alerts from any app**), and then it goes to `/api/alerts/ingest`.
- **Permissions:** `INTERNET`, `RECORD_AUDIO` (microphone), `RECEIVE_SMS` (optional, receive-only, off until you grant it) and the **Notification access** special permission (`BIND_NOTIFICATION_LISTENER_SERVICE`), which you switch on yourself.
- **How to grant:** Settings, Notifications, Device and app notifications, switch **Laptop Hub** on (or use the hub's Settings > Phone capture button, which opens that screen). On Android 13+ first do Settings, Apps, Laptop Hub, three-dot menu, **Allow restricted settings**. Then press **Test** in the hub: a fake "Paid Rs 1" item should appear in Spending.
- **Control from the page:** `HubCapture.status / openListenerSettings / requestSms / setEnabled / setWatch / flush / test`, documented in `docs/api/capture-android.md`.
- **Not tested:** this Java code had not been compiled when this was written (the first cloud build that includes it is the first real compile) and has never run on a real phone.

## Watching Gmail and Google Chat (optional)

The same Notification access can also pass **Gmail** and **Google Chat** message notifications to your hub, so it can
tell you about things you might miss. This is off until you switch on monitoring in the hub page (mail and chat are
separate switches, and independent of payment capture). When a switch is off the app does not read those
notifications at all. When on, it forwards the sender or space, the message text (a Gmail subject and snippet),
and the time, only to your own hub over HTTPS. Summary ("3 new messages") and ongoing notifications are skipped,
repeats within 2 minutes are sent once, and other apps follow the alerts list below, not this switch. Turning it on needs
the same Notification access steps as above. Developers: see `docs/api/capture-android.md` and
`node scripts/test-watch-filter.js`.

## Alerts from any app (WhatsApp, Slack, Teams, Asana, Jira, Telegram, SMS and more)

The same Notification access also feeds the hub's **Alerts** triage, for **any app you choose**, not a fixed pair.

**Which apps.** The list lives on the hub (Alerts settings, served as `GET /api/alerts/capture-config`) and is edited there:
switch an app on or off, or add any other app by its package name. The phone caches the list, refreshes it when the app
opens and about every 15 minutes while notifications arrive (only when the hub is reachable), and uses the last cache if
the hub cannot be reached. With no list at all (first run, or an older hub without that endpoint) it uses a safe built-in
default: WhatsApp, Slack, Teams, Gmail, Google Chat, Asana, Jira, Trello, Telegram, Outlook and Messages / SMS apps.
Anything not on the list is ignored before its content is read. The hub may also set `mode: "all-but-blocklist"` (forward
every app except the ones switched off), `deny` (extra packages never to forward) and `names` (your names, for the mention hint).

**What is read.** Only the notification **preview** (title, one or two lines of text, the sender, the chat name), never
the full chat, the app or any other data. Sent to the hub: package, app name, title, sender, text, a conversation id,
whether it looks like a group, a `mentioned: true|false|unknown` hint (true when a group message contains one of your
names or says "mentioned you"), the time and the notification key. Group summaries ("3 new messages"), ongoing, media and
foreground-service notifications and notifications the app marks secret on the lock screen are dropped; the same update
of the same notification is sent once (5 minute window).

**Never forwarded:** this app's own notifications, password managers and authenticators, and anything that looks like a
banking app (built-in deny list, extendable from the hub, applied even if you add them). Payment apps keep their separate
spend-only path above.

**Privacy.** Everything goes only to your own hub, over the same HTTPS login the app already uses; nothing else is stored
on the phone except an unsent queue (a small file, 200 events at most, oldest dropped first) that is retried when the hub is
reachable. Notification text is never written to the phone's log.

**Battery.** No extra background service or wake lock: Android hands notifications to the listener, and the app does a
short request only when one passes the list. Set Laptop Hub's battery to Unrestricted so Android does not stop the listener.

**After updating.** This needs a new APK: run the **Build Android APK** workflow again, **uninstall the old Laptop Hub
first** (debug builds are signed with a fresh key), install the new one, and switch Notification access on again if Android
turned it off. Not tested on a real phone or in a Gradle build: the logic classes (`AlertPolicy`, `AlertFilter`,
`EventQueue`) have JVM tests (`app/src/test/.../capture/AlertLogicTest.java`); how each app words its notification
(sender versus chat name, group flags) is unverified.

## Opening and downloading files

The app handles files natively (`android/.../HubWeb.java`). The page must **navigate** (not use fetch) to:

`/api/file?root=<root>&path=<path>&download=1` (e.g. `location.href = url`, or an `<a href>` / `window.open` with that URL)

- Same-origin URLs are downloaded with Android DownloadManager to the public Downloads folder, using the session cookie and `X-Hub: 1`. A "Downloading name..." toast shows and Android's completed notification opens the file when tapped. No storage permission is needed (API 29+).
- Opening a PDF inline (the same URL without `download=1`) only works in desktop browsers; in the app, always use `download=1`.
- `target=_blank` / `window.open` to the hub origin loads in the app (or downloads if `download=1`); other http(s), mailto: and tel: links open in the system browser; other schemes are refused. Foreign origins never load inside the app.
- Not compiled here (no Android SDK); the GitHub Actions build is the first compile.

## Hey Jarvis in the background

An optional, **off by default** native feature: the phone keeps listening with the screen off and the app closed, and
opens the app in voice mode when you say "Hey" and the name of one of your assistants: Jarvis and Friday, or whatever you
renamed or added in **Settings, Assistants** (plus the rest of the sentence, if you say one).

**Turn it on:** open the app, **Settings**, the card **Always-on Hey Jarvis (background)**. Press **Download voice model**
(one time, about 40 MB, from `alphacephei.com/vosk/models`, kept in the app's private storage), then switch it on and allow
the microphone and notifications when Android asks. A notification "Listening for your wake word" with a **Stop**
button stays up while it listens.

**Renamed or added assistants.** The phone does not read your hub by itself: the page inside the app does. When you open
the app (and again on resume, at most every 15 minutes, or at once after you edit Assistants in the app) it reads
`GET /api/assistants` and hands the names of the **enabled** assistants (name, wake names, mis-hearings) to the native
service. The phone keeps the last list (SharedPreferences `hub_wake`, key `assistants`), so it keeps working offline; with
nothing stored yet it uses the built-in Jarvis and Friday. A running service rebuilds its recogniser by itself, no restart.
After you change an assistant on another device, **open the app once** so the phone learns it. This needs a **new APK**:
an older build only knows Jarvis and Friday (the page then simply cannot send the list).
**Accuracy caveat.** The small English model hears common words well and rare or invented names poorly. Add mis-hearings
(Settings, Assistants, variants) for a name that is often missed. Names the model has no word for are left out of the
grammar (when the model ships a word list) and the Settings card says "some names are not in the speech model; accuracy
lower"; if Vosk still refuses the grammar the built-in Jarvis/Friday grammar is used. Same rule as everywhere else: a name
inside a normal sentence ("Atlas is a nice name") does not wake.

**Privacy.** Recognition is offline with [Vosk](https://alphacephei.com/vosk/) (Apache-2.0) on the phone. Sound is never
stored or sent anywhere. Only the text after the wake phrase reaches your hub, the same as if you had typed it.
**Battery.** Continuous listening costs battery (a wake lock keeps the CPU awake). It has not been measured; if it is too
much, switch it off or use the browser wake word while the app is open.

**Answer by voice (screen off, app closed).** When you say "Hey Friday, what is on my plate today" and the app is **not** in
front, the phone no longer waits for you to open it: the service calls your hub (`POST /api/assistant/message`, the same
login cookie the app already holds, provider `auto`, `auto_approve` false, session `wake-<assistant>`) and **speaks the answer
with the phone's own voice** (Android text to speech). The microphone is off while it works and speaks, then listens again
after the usual 10 s pause. Details:
- "Hey Friday" alone: the phone says "Yes?" and listens again (say the full sentence next time). "Open RedLine" / "open the app"
  does not go to the AI: it opens the app as before and says "Opening RedLine".
- One reply at a time: a wake phrase said while a reply is being fetched or spoken is not heard (the simplest safe choice).
- Anything that needs your approval (sending mail, writing files...) is **never done in the background**: the phone says "I need
  your tap for that" and shows the "tap to talk" notification; the request then times out on the hub.
- Hub not reachable: "I can't reach the laptop". Logged out: "Please open the app and log in". The sentence and the answer are
  never logged; no token or password is stored by this feature.
- With the app open on screen, nothing changes: the page answers (and speaks with the hub voice, if you set one).
- **The switch:** Settings, "Answer by voice while the screen is off" (SharedPreferences `hub_wake`, key `bg_reply`, default on).
  Off gives the old behaviour (open the app / tap the notification).
- **Limits:** needs Tailscale and the hub reachable from the phone; the app must have been logged in at least once (the cookie
  is shared with the WebView); the voice is the phone's, not the hub's; the whole call is capped at 120 s; spoken text is cut at
  about 900 characters.
- **Needs a NEW APK:** build it with the GitHub Actions workflow "Build Android APK", **uninstall the old APK first** (builds are
  debug-signed, so they do not install over each other), then log in again and switch the wake word on.
- Not run on a phone: only the pure parts (`WakeReply`: event parsing, speech cleanup, "open the app" detection) have JVM tests
  (`WakeReplyTest`).

**How it works** (`android/app/src/main/java/dev/shikhar/laptophub/wake/`): `WakeService` (foreground service, type
`microphone`, `AudioRecord` 16 kHz) feeds a Vosk recognizer limited to a grammar built from the assistants' names ("hey X", "ok X", "okay X", "X", `[unk]`;
`WakeGrammar`, `WakeMatcher`, `WakeAssistants`). On a match it
vibrates, re-listens for up to 4 s with free recognition to catch the rest of the sentence, stores it in memory, tries to
open the app and posts a high-priority "tap to talk" notification (with a full-screen intent). It then pauses 10 s so it
does not hear itself. The page (`hub/static/js/wakebridge.js`) asks the plugin `consumePending()` whenever it becomes
visible and calls `voicemode.setPreload(text, assistantId)`; a `#/voice?assistant=atlas&text=...` link does the same (the launch
extras are `wake_assistant`, `wake_text`, `wake_hash`).
Plugin `HubWake`: `status / enable / disable / modelStatus / downloadModel / consumePending / setAssistants / setBackgroundReply`.
Pure logic with JVM tests (`app/src/test/java/dev/shikhar/laptophub/wake/WakeLogicTest.java`): phrase matching for several assistants, grammar building, URL
encoding (300 character cap), zip-slip-safe unzip, the model state machine. Dependency: `com.alphacephei:vosk-android:0.3.75`
(Maven Central, no extra repository needed). The model is `vosk-model-small-en-us-0.15`.

**Limits you should know.**
- There is **no start-at-boot**: Android 14 and newer do not let an app start a microphone service from the background or
  from boot. After a reboot (or if Android kills the app) open the app and switch it on again. The service is not restarted by
  the system for the same reason.
- Android 10 and newer restrict apps from opening a screen from the background. The app tries, and also posts a
  notification with a full-screen intent. If the app does not open by itself, tap the notification; on some phones also
  allow **Display over other apps** / **Full screen notifications** for Laptop Hub in Android settings.
- Some makers (Xiaomi, Oppo, Vivo, OnePlus, Samsung) kill background services; set Laptop Hub's battery to Unrestricted.
- While another app is using the microphone (a call), Android may silence it.
- **Not tested:** the Android code was compile-checked against the Android API 36 stubs and the real Vosk library classes
  in a sandbox, but the full Gradle build (the first real one is the GitHub Actions run) and a real phone have not been
  tried. Accuracy with an Indian accent, battery drain and the Android 14 service rules are unverified.

## Changing the hub address later

Run `npm run sync` again with the new `HUB_URL`, rebuild, reinstall. The address is built into the app.

## Later (not built yet)

- Push notifications for approvals and reminders.
- A custom splash screen (the launcher icon is the Laptop Hub icon; regenerate it with `python scripts/make-icons.py`).
- A release-signed build with a keystore kept in GitHub secrets, so updates install over the old app (today's builds are debug-signed for your own phone).

## Not tested

The cloud build has run successfully (about 2 minutes, producing a 3.7 MB debug APK), but that test build used a placeholder address, so build your own with your real one. The APK has **not been installed on a real phone yet**. If the app opens to a blank or error page, check that
Tailscale is on, the hub is running, and the address you built with is exactly the one from `tailscale serve status`.
