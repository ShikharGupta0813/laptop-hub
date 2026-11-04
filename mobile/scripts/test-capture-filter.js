// Tests the spend-capture filter. Run: node scripts/test-capture-filter.js
//
// The regex table below is a mirror of the constants in
//   android/app/src/main/java/dev/shikhar/laptophub/capture/CaptureFilter.java
// The first test PARSES that Java file and fails if any constant differs, so the two cannot drift.
// `--dump` prints the samples as JSON (used to cross-check the real Java class on a plain JVM).
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const JAVA = path.join(__dirname, "..", "android", "app", "src", "main", "java", "dev", "shikhar", "laptophub", "capture", "CaptureFilter.java");

const T = {
  DEBIT_WORD: "\\b(?:paid|sent|debited|spent|withdrawn)\\b|payment (?:of|successful)",
  AMOUNT: "(?:₹|\\brs\\.?|\\binr)\\s*\\d|\\b(?:debited|spent)\\s+(?:by|for|with)\\s+\\d",
  HARD_REJECT: "received|refund|cashback|request|reminder|offer|\\botp\\b|reward|scratch card|sent you|paid you|failed|declined|reversed|reversal",
  CREDITED: "credited",
  DEBITED: "\\bdebited\\b",
  SMS_PATTERN: "\\bupi\\b|\\bvpa\\b|a/c|\\bacct?\\b|\\bcard\\b|\\bdebited\\b|\\bimps\\b|\\bneft\\b",
  SMS_SENDER: "[a-z]",
};
const R = Object.fromEntries(Object.entries(T).map(([k, v]) => [k, new RegExp(v, "i")]));

const ALLOWED = [
  "com.google.android.apps.nbu.paisa.user", "com.phonepe.app", "net.one97.paytm", "in.org.npci.upiapp",
  "com.dreamplug.androidapp", "com.amazon.mShop.android.shopping", "com.snapwork.hdfc", "com.sbi.SBIFreedomPlus",
  "com.csam.icici.bank.imobile", "com.axis.mobile", "com.kotak.mobile", "com.infrasofttech.centralbank",
  "com.msf.kbank.mobile", "com.idfcfirstbank.optimus", "com.yesbank.yesmobile", "com.freecharge.android",
  "com.mobikwik_new", "com.slice.app", "com.jupiter.money",
];

function looksLikeSpend(title, body) {
  const t = `${title || ""}\n${body || ""}`.trim();
  if (!t) return false;
  if (R.HARD_REJECT.test(t)) return false;
  if (R.CREDITED.test(t) && !R.DEBITED.test(t)) return false;
  return R.DEBIT_WORD.test(t) && R.AMOUNT.test(t);
}
const forwardNotification = (pkg, title, body) => ALLOWED.includes(pkg) && looksLikeSpend(title, body);
const forwardSms = (sender, body) =>
  !!sender && R.SMS_SENDER.test(sender) && !!body && R.SMS_PATTERN.test(body) && looksLikeSpend("", body);

const GPAY = "com.google.android.apps.nbu.paisa.user", PHONEPE = "com.phonepe.app", PAYTM = "net.one97.paytm";
// [kind, who, title, text, expected]
const SAMPLES = [
  ["n", GPAY, "Paid ₹250 to Zomato", "Payment successful", true],
  ["n", GPAY, "You paid Swiggy", "₹349.00 paid using HDFC Bank ****1234", true],
  ["n", GPAY, "Rahul sent you ₹500", "Tap to view", false],
  ["n", GPAY, "Payment received", "₹500 received from Rahul", false],
  ["n", GPAY, "Cashback earned", "You got ₹5 cashback on your payment of ₹100", false],
  ["n", GPAY, "Rahul requested ₹300", "Pay now", false],
  ["n", GPAY, "Reminder", "Pay your electricity bill Rs 1,200 by tomorrow", false],
  ["n", PHONEPE, "Payment Successful", "Paid ₹120 to Cafe Coffee Day", true],
  ["n", PHONEPE, "Sent ₹1,500", "Money sent to Amit Kumar", true],
  ["n", PHONEPE, "Money received", "Rs.2000 credited to your account", false],
  ["n", PHONEPE, "Refund processed", "₹399 refund for your order", false],
  ["n", PHONEPE, "Big offer!", "Pay with PhonePe and win up to ₹500", false],
  ["n", PAYTM, "Paid Successfully", "Rs. 75 paid to Metro Smart Card", true],
  ["n", PAYTM, "Payment failed", "Payment of ₹200 failed, money will be refunded", false],
  ["n", PAYTM, "Scratch card", "You won a scratch card worth ₹50 on your payment", false],
  ["n", "com.snapwork.hdfc", "HDFC Bank", "Rs 899 spent on your card ending 1234 at AMAZON", true],
  ["n", "org.example.randomapp", "Paid ₹250", "Payment of ₹250 successful", false],
  ["n", "com.whatsapp", "Rahul", "I paid Rs 500 for lunch", false],
  ["n", PAYTM, "Hello", "Your order is on the way", false],
  ["s", "VM-HDFCBK", "", "Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO On 01/01/25 Ref 123456789 Not You? Call 18002586161", true],
  ["s", "AX-ICICIB", "", "ICICI Bank Acct XX123 debited for Rs 250.00 on 01-Jan-25; ZOMATO credited. UPI:123456789012. Call 18002662 for dispute.", true],
  ["s", "VK-SBIUPI", "", "Dear UPI user A/C X1234 debited by 250.0 on date 01Jan25 trf to ZOMATO Refno 123456789", true],
  ["s", "AD-HDFCBK", "", "Rs.5000.00 credited to HDFC Bank A/C XX1234 on 01-01-25 from VPA rahul@okhdfc (UPI 123456789)", false],
  ["s", "VM-HDFCBK", "", "123456 is your OTP for txn of Rs 250 at AMAZON. Do not share.", false],
  ["s", "JD-AXISBK", "", "Rs 799 spent on Axis Bank Card no. XX1234 at NETFLIX on 01-01-25. Not you? Call 1800", true],
  ["s", "VM-PAYTMB", "", "Get cashback up to Rs 100 when you pay using UPI. Offer valid till Sunday", false],
  ["s", "+919812345678", "", "I paid Rs 500 on UPI, check your A/C", false],
  ["s", "VM-HDFCBK", "", "Rs 500 spent on holiday! Free chai for you", false],
  ["s", "VM-JIOINF", "", "Recharge of Rs 299 paid successfully. Enjoy!", false],
];

if (process.argv.includes("--dump")) {
  console.log(JSON.stringify(SAMPLES.map(([k, w, ti, tx, e]) => ({ kind: k, who: w, title: ti, text: tx, expected: e }))));
  process.exit(0);
}

let failed = 0;
function test(name, fn) {
  try { fn(); console.log("ok   ", name); } catch (e) { failed++; console.log("FAIL ", name, "-", e.message); }
}

test("Java constants equal this table", () => {
  const src = fs.readFileSync(JAVA, "utf8");
  const found = {};
  for (const m of src.matchAll(/public static final String (\w+) = "((?:[^"\\]|\\.)*)";/g)) {
    found[m[1]] = JSON.parse(`"${m[2]}"`); // Java escapes used here (\\ \uXXXX) are valid JSON escapes
  }
  for (const [k, v] of Object.entries(T)) {
    assert.ok(k in found, `${k} missing in CaptureFilter.java`);
    assert.strictEqual(found[k], v, `${k} differs between Java and JS`);
  }
  const block = /ALLOWED_PACKAGES\s*=\s*\{([^}]*)\}/.exec(src);
  assert.ok(block, "ALLOWED_PACKAGES not found");
  const pk = [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  assert.deepStrictEqual(pk, ALLOWED, "ALLOWED_PACKAGES differ between Java and JS");
});

for (const [kind, who, title, text, expected] of SAMPLES) {
  const label = `${kind === "n" ? "notif" : "sms  "} ${expected ? "FORWARD" : "ignore "} ${who} | ${title} ${text}`.slice(0, 110);
  test(label, () => {
    const got = kind === "n" ? forwardNotification(who, title, text) : forwardSms(who, text);
    assert.strictEqual(got, expected, `expected ${expected ? "forward" : "ignore"}`);
  });
}

console.log(failed ? `\n${failed} FAILED` : `\nall ${SAMPLES.length + 1} passed`);
process.exit(failed ? 1 : 0);
