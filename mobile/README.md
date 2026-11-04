# Laptop Hub for Android

A real Android app (an APK) that opens your Laptop Hub. It is a thin native shell around the hub's own
HTTPS page, so every screen you use in the browser works the same here, with a normal app icon, no browser
bars, and a native microphone permission. All logic stays on your laptop.

Why a shell and not a rewrite: the whole interface is already built and tested as a web app. A shell reuses
all of it, builds in minutes, and stays in sync with the laptop. Features that need real native code (a
background "Hey Jarvis" wake word, notifications) are listed under **Later** below.

## What you need on the laptop that builds the app

1. **Node.js 22 or newer** (`node --version`).
2. **Android Studio** (it brings the Android SDK and a Java runtime). Open it once and let it finish
   installing the SDK.
3. Your hub's **HTTPS address** from `tailscale serve status`, like `https://yourlaptop.tail1234.ts.net`.
   The app cannot work without HTTPS (the microphone needs it).

## Build the APK (Windows PowerShell)

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
- A custom app icon and splash screen (it uses Capacitor's default icon).
- A signed release build for sharing; this guide makes a debug build for your own phone.

## Not tested

This was generated and checked for correct configuration, but **no APK has been built or run yet**: the
build needs the Android SDK, which was not available where this was written. If the build fails, the first
suspects are the Java version and the Android SDK path (`ANDROID_HOME`, or `android/local.properties`
containing `sdk.dir=C\:\\Users\\YOU\\AppData\\Local\\Android\\Sdk`).
