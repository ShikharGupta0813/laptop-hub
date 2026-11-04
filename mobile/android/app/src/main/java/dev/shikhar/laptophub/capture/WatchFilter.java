package dev.shikhar.laptophub.capture;

import java.util.HashMap;
import java.util.Iterator;
import java.util.Locale;
import java.util.Map;

/**
 * Pure routing logic for the "watch" feature: message notifications from Google Chat and Gmail are
 * forwarded to POST /api/watch/ingest. No Android imports on purpose, so it can be unit tested on a plain JVM.
 *
 * Mirrored in mobile/scripts/test-watch-filter.js, which parses THIS file and fails if the constants drift.
 * Keep every String constant on ONE line as a single string literal, and every int constant as a plain number.
 * The payment filter (CaptureFilter) is separate and unchanged.
 */
public final class WatchFilter {
    private WatchFilter() {}

    /** Google Chat. Forwarded only while the "chat" flag is on, as source "chat". */
    public static final String PKG_CHAT = "com.google.android.apps.dynamite";
    /** Gmail. Forwarded only while the "mail" flag is on, as source "mail". */
    public static final String PKG_MAIL = "com.google.android.gm";

    public static final int MAX_TITLE = 200;
    public static final int MAX_TEXT = 1000;
    /** The same (package, title, text) inside this window is forwarded once. */
    public static final long DEDUPE_WINDOW_MS = 120000L;

    /** "chat", "mail", or null when the package is not a watched app. */
    public static String sourceFor(String pkg) {
        if (PKG_CHAT.equals(pkg)) return "chat";
        if (PKG_MAIL.equals(pkg)) return "mail";
        return null;
    }

    public static boolean isWatchPackage(String pkg) {
        return sourceFor(pkg) != null;
    }

    /**
     * The source ("chat" / "mail") to forward this notification as, or null to drop it.
     * Drops: other packages, this app's own notifications, group summaries, ongoing notifications,
     * and anything whose flag is off.
     */
    public static String route(String pkg, String ownPkg, boolean groupSummary, boolean ongoing,
                               boolean mailOn, boolean chatOn) {
        String source = sourceFor(pkg);
        if (source == null) return null;
        if (pkg.equals(ownPkg)) return null;
        if (groupSummary || ongoing) return null;
        if (source.equals("mail") && !mailOn) return null;
        if (source.equals("chat") && !chatOn) return null;
        return source;
    }

    public static String cap(String s, int max) {
        if (s == null) return "";
        s = s.trim();
        return s.length() > max ? s.substring(0, max) : s;
    }

    /**
     * Mail: subject + snippet. Notification text is usually the subject and the big text the snippet (or the
     * subject plus snippet). Chat: the message, preferring the longer of text / big text.
     */
    public static String composeText(String source, String text, String big) {
        String t = text == null ? "" : text.trim();
        String b = big == null ? "" : big.trim();
        if (b.isEmpty()) return t;
        if (t.isEmpty()) return b;
        if ("mail".equals(source)) {
            if (b.contains(t)) return b;
            if (t.contains(b)) return t;
            return t + " - " + b;
        }
        return b.length() > t.length() ? b : t;
    }

    // ---- dedupe: the same (package, title, text) inside DEDUPE_WINDOW_MS is forwarded once ----

    private static final int DEDUPE_MAX = 200;
    private static final Map<String, Long> SEEN = new HashMap<>();

    private static String norm(String s) {
        return s == null ? "" : s.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
    }

    /** True the first time a (pkg, title, text) is seen within the window. nowMillis is the notification time. */
    public static synchronized boolean firstSeen(String pkg, String title, String text, long nowMillis) {
        // Forget old entries, and bound the table.
        for (Iterator<Map.Entry<String, Long>> it = SEEN.entrySet().iterator(); it.hasNext(); ) {
            if (Math.abs(nowMillis - it.next().getValue()) >= DEDUPE_WINDOW_MS) it.remove();
        }
        if (SEEN.size() >= DEDUPE_MAX) SEEN.clear();
        String key = (pkg == null ? "" : pkg) + "|" + norm(title) + "|" + norm(text);
        Long prev = SEEN.get(key);
        if (prev != null && Math.abs(nowMillis - prev) < DEDUPE_WINDOW_MS) return false;
        SEEN.put(key, nowMillis);
        return true;
    }

    /** Test helper: forget everything seen so far. */
    public static synchronized void resetSeen() {
        SEEN.clear();
    }
}
