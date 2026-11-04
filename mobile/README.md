# Laptop Hub for Android

A real Android app (an APK) that opens your Laptop Hub. It is a thin native shell around the hub's own
HTTPS page, so every screen you use in the browser works the same here, with a normal app icon, no browser
bars, and a native microphone permission. All logic stays on your laptop.

Why a shell and not a rewrite: the whole interface is already built and tested as a web app. A shell reuses
all of it, builds in minutes, and stays in sync with the laptop. Features that need real native code (a
background "Hey Jarvis" wake word, notifications) are listed under **Later** below.

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

## Changing the hub address later

Run `npm run sync` again with the new `HUB_URL`, rebuild, reinstall. The address is built into the app.

## Later (not built yet)

- Background wake word ("Hey Jarvis") that works with the screen off: needs a native plugin.
- Push notifications for approvals and reminders.
- A custom splash screen (the launcher icon is the Laptop Hub icon; regenerate it with `python scripts/make-icons.py`).
- A release-signed build with a keystore kept in GitHub secrets, so updates install over the old app (today's builds are debug-signed for your own phone).

## Not tested

The cloud build has run successfully (about 2 minutes, producing a 3.7 MB debug APK), but that test build used a placeholder address, so build your own with your real one. The APK has **not been installed on a real phone yet**. If the app opens to a blank or error page, check that
Tailscale is on, the hub is running, and the address you built with is exactly the one from `tailscale serve status`.
