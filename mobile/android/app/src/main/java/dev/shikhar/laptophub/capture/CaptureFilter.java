package dev.shikhar.laptophub.capture;

import java.util.LinkedHashSet;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * Pure decision logic for "is this notification / SMS a spend worth forwarding?".
 * No Android imports on purpose, so it can be unit tested on a plain JVM.
 *
 * The regex strings below are mirrored in mobile/scripts/test-capture-filter.js, which
 * parses THIS file and fails if the two ever drift apart. Keep every constant on ONE line
 * as a single string literal (the test reads them with a regex).
 */
public final class CaptureFilter {
    private CaptureFilter() {}

    /** The ONLY apps whose notifications are ever read. Add a package name here to extend. */
    public static final String[] ALLOWED_PACKAGES = {
        "com.google.android.apps.nbu.paisa.user", "com.phonepe.app", "net.one97.paytm",
        "in.org.npci.upiapp", "com.dreamplug.androidapp", "com.amazon.mShop.android.shopping",
        "com.snapwork.hdfc", "com.sbi.SBIFreedomPlus", "com.csam.icici.bank.imobile",
        "com.axis.mobile", "com.kotak.mobile", "com.infrasofttech.centralbank",
        "com.msf.kbank.mobile", "com.idfcfirstbank.optimus", "com.yesbank.yesmobile",
        "com.freecharge.android", "com.mobikwik_new", "com.slice.app", "com.jupiter.money",
    };

    // Flags for every pattern: case-insensitive. (JS uses the "i" flag.)
    /** A word that says money left the account. */
    public static final String DEBIT_WORD = "\\b(?:paid|sent|debited|spent|withdrawn)\\b|payment (?:of|successful)";
    /** A currency marker followed by a number, or "debited by 250". */
    public static final String AMOUNT = "(?:₹|\\brs\\.?|\\binr)\\s*\\d|\\b(?:debited|spent)\\s+(?:by|for|with)\\s+\\d";
    /** Anything that is not a spend, or that we are not sure about: dropped. */
    public static final String HARD_REJECT = "received|refund|cashback|request|reminder|offer|\\botp\\b|reward|scratch card|sent you|paid you|failed|declined|reversed|reversal";
    /** "credited" is dropped, unless the text also says "debited" (some bank SMS list both sides). */
    public static final String CREDITED = "credited";
    public static final String DEBITED = "\\bdebited\\b";
    /** An SMS must additionally look like a UPI / bank debit message. */
    public static final String SMS_PATTERN = "\\bupi\\b|\\bvpa\\b|a/c|\\bacct?\\b|\\bcard\\b|\\bdebited\\b|\\bimps\\b|\\bneft\\b";
    /** An SMS sender must contain a letter (bank headers like VM-HDFCBK); phone numbers are ignored. */
    public static final String SMS_SENDER = "[a-z]";

    private static final int FLAGS = Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE;
    private static final Pattern P_DEBIT_WORD = Pattern.compile(DEBIT_WORD, FLAGS);
    private static final Pattern P_AMOUNT = Pattern.compile(AMOUNT, FLAGS);
    private static final Pattern P_HARD_REJECT = Pattern.compile(HARD_REJECT, FLAGS);
    private static final Pattern P_CREDITED = Pattern.compile(CREDITED, FLAGS);
    private static final Pattern P_DEBITED = Pattern.compile(DEBITED, FLAGS);
    private static final Pattern P_SMS = Pattern.compile(SMS_PATTERN, FLAGS);
    private static final Pattern P_SENDER = Pattern.compile(SMS_SENDER, FLAGS);

    public static boolean isAllowedPackage(String pkg) {
        if (pkg == null) return false;
        for (String p : ALLOWED_PACKAGES) if (p.equals(pkg)) return true;
        return false;
    }

    /** True when the text looks like money leaving the account. */
    public static boolean looksLikeSpend(String title, String body) {
        String t = ((title == null ? "" : title) + "\n" + (body == null ? "" : body)).trim();
        if (t.isEmpty()) return false;
        if (P_HARD_REJECT.matcher(t).find()) return false;
        if (P_CREDITED.matcher(t).find() && !P_DEBITED.matcher(t).find()) return false;
        return P_DEBIT_WORD.matcher(t).find() && P_AMOUNT.matcher(t).find();
    }

    public static boolean shouldForwardNotification(String pkg, String title, String body) {
        return isAllowedPackage(pkg) && looksLikeSpend(title, body);
    }

    public static boolean shouldForwardSms(String sender, String body) {
        if (sender == null || !P_SENDER.matcher(sender).find()) return false;
        if (body == null || !P_SMS.matcher(body).find()) return false;
        return looksLikeSpend("", body);
    }

    // ---- dedupe: the same (source, text) inside the same minute is forwarded once ----

    private static final int DEDUPE_MAX = 200;
    private static final LinkedHashSet<String> SEEN = new LinkedHashSet<>();

    private static String key(String who, String text, long minute) {
        String norm = text == null ? "" : text.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
        return (who == null ? "" : who) + "|" + norm + "|" + minute;
    }

    /** Returns true the first time a (who, text, minute) is seen. Also treats the previous minute as a duplicate. */
    public static synchronized boolean firstTime(String who, String text, long tsMillis) {
        long m = tsMillis / 60000L;
        if (SEEN.contains(key(who, text, m)) || SEEN.contains(key(who, text, m - 1))) return false;
        SEEN.add(key(who, text, m));
        while (SEEN.size() > DEDUPE_MAX) {
            SEEN.remove(SEEN.iterator().next());
        }
        return true;
    }
}
