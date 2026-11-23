// Adds the permissions and the payment-capture components Laptop Hub needs to the Android manifest.
// Safe to run many times: every piece is only added if it is not there yet.
const fs = require("fs");
const path = require("path");
const file = path.join(__dirname, "..", "android", "app", "src", "main", "AndroidManifest.xml");
let xml = fs.readFileSync(file, "utf8");
const wanted = [
  "android.permission.RECORD_AUDIO",          // voice notes, talking to the assistant, English practice
  "android.permission.MODIFY_AUDIO_SETTINGS", // lets the web view open the microphone
  "android.permission.RECEIVE_SMS",           // optional bank-SMS capture (asked at runtime; never READ_SMS)
  // Background "Hey Jarvis" (HubWake plugin). No BOOT_COMPLETED: Android 14+ will not start a microphone service from boot.
  "android.permission.FOREGROUND_SERVICE", "android.permission.FOREGROUND_SERVICE_MICROPHONE",
  "android.permission.POST_NOTIFICATIONS", "android.permission.WAKE_LOCK", "android.permission.VIBRATE",
  "android.permission.USE_FULL_SCREEN_INTENT",
];
let changed = false;
for (const name of wanted) {
  if (!xml.includes(`"${name}"`)) {
    xml = xml.replace("</manifest>", `    <uses-permission android:name="${name}" />\n</manifest>`);
    changed = true;
  }
}

// Components of the HubCapture plugin (automatic spend capture).
const components = [
  {
    marker: ".capture.PaymentNotificationListener",
    xml: `        <!-- Reads notifications from an allow-list of payment apps only (see CaptureFilter.java). -->
        <service
            android:name=".capture.PaymentNotificationListener"
            android:label="@string/app_name"
            android:exported="true"
            android:permission="android.permission.BIND_NOTIFICATION_LISTENER_SERVICE">
            <intent-filter>
                <action android:name="android.service.notification.NotificationListenerService" />
            </intent-filter>
        </service>
`,
  },
  {
    marker: ".capture.BankSmsReceiver",
    xml: `        <!-- Optional bank/UPI debit SMS. Does nothing until the user grants SMS permission in the app. -->
        <receiver
            android:name=".capture.BankSmsReceiver"
            android:exported="true"
            android:permission="android.permission.BROADCAST_SMS">
            <intent-filter>
                <action android:name="android.provider.Telephony.SMS_RECEIVED" />
            </intent-filter>
        </receiver>
`,
  },
  {
    marker: ".wake.WakeService",
    xml: `        <!-- Background wake word. Runs only after the owner switches it on in the app. -->
        <service
            android:name=".wake.WakeService"
            android:exported="false"
            android:foregroundServiceType="microphone" />
`,
  },
];
// Share > REDLINE: ACTION_SEND text/plain on the main activity (handled in MainActivity.java / ShareIn.java).
if (!xml.includes("android.intent.action.SEND")) {
  const anchor = /([ \t]*)<\/activity>/;
  if (!anchor.test(xml)) throw new Error("AndroidManifest.xml has no </activity>");
  xml = xml.replace(anchor, `$1    <!-- Share > REDLINE: a job shared as text from another app. Text only, never files. -->
$1    <intent-filter>
$1        <action android:name="android.intent.action.SEND" />
$1        <category android:name="android.intent.category.DEFAULT" />
$1        <data android:mimeType="text/plain" />
$1    </intent-filter>
$1</activity>`);
  changed = true;
}
for (const c of components) {
  if (!xml.includes(`"${c.marker}"`)) {
    if (!xml.includes("</application>")) throw new Error("AndroidManifest.xml has no </application>");
    xml = xml.replace(/[ \t]*<\/application>/, `${c.xml}    </application>`);
    changed = true;
  }
}
if (changed) fs.writeFileSync(file, xml);
console.log(changed ? "Manifest updated." : "Manifest already has the permissions and capture components.");
