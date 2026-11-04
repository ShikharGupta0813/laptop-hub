// Adds the permissions Laptop Hub needs to the Android manifest. Safe to run many times.
const fs = require("fs");
const path = require("path");
const file = path.join(__dirname, "..", "android", "app", "src", "main", "AndroidManifest.xml");
let xml = fs.readFileSync(file, "utf8");
const wanted = [
  "android.permission.RECORD_AUDIO",          // voice notes, talking to the assistant, English practice
  "android.permission.MODIFY_AUDIO_SETTINGS", // lets the web view open the microphone
];
let changed = false;
for (const name of wanted) {
  if (!xml.includes(`"${name}"`)) {
    xml = xml.replace("</manifest>", `    <uses-permission android:name="${name}" />\n</manifest>`);
    changed = true;
  }
}
if (changed) fs.writeFileSync(file, xml);
console.log(changed ? "Manifest updated." : "Manifest already has the permissions.");
