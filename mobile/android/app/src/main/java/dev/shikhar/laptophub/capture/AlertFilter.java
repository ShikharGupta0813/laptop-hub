package dev.shikhar.laptophub.capture;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Pure helpers for the general alerts capture: noise filtering, de-duplication and the mention hint.
 * No Android imports (the Android flag values are copied below), so it runs on a plain JVM.
 */
public final class AlertFilter {
    private AlertFilter() {}

    // Copies of android.app.Notification flag values (stable public API constants).
    public static final int FLAG_ONGOING_EVENT = 0x00000002;
    public static final int FLAG_FOREGROUND_SERVICE = 0x00000040;
    public static final int FLAG_GROUP_SUMMARY = 0x00000200;

    public static final int MAX_TITLE = 200;
    public static final int MAX_TEXT = 1000;
    public static final long DEDUPE_WINDOW_MS = 300000L;

    /** Notification categories that are never messages: media controls, progress bars, services, system. */
    private static final String[] NOISE_CATEGORIES = {"transport", "progress", "service", "sys", "status", "alarm", "navigation"};

    /** True for group summaries, ongoing / foreground-service / media / progress notifications. */
    public static boolean isNoise(int flags, String category, boolean mediaStyle) {
        if ((flags & (FLAG_GROUP_SUMMARY | FLAG_ONGOING_EVENT | FLAG_FOREGROUND_SERVICE)) != 0) return true;
        if (mediaStyle) return true;
        if (category != null) {
            String c = category.toLowerCase(Locale.ROOT);
            for (String n : NOISE_CATEGORIES) {
                if (c.equals(n)) return true;
            }
        }
        return false;
    }

    public static String cap(String s, int max) {
        if (s == null) return "";
        s = s.trim();
        return s.length() > max ? s.substring(0, max) : s;
    }

    /** The longer of text / big text (big text is the unabbreviated one). */
    public static String body(String text, String big) {
        String t = text == null ? "" : text.trim();
        String b = big == null ? "" : big.trim();
        return b.length() > t.length() ? b : t;
    }

    /** Group chat when the extra says so, else when a conversation title exists. */
    public static boolean isGroup(Boolean extraIsGroup, String conversationTitle) {
        if (extraIsGroup != null) return extraIsGroup;
        return conversationTitle != null && !conversationTitle.trim().isEmpty();
    }

    private static final Pattern EXPLICIT_MENTION = Pattern.compile(
        "\\b(?:mentioned you|tagged you|mentioned @you|replied to you)\\b", Pattern.CASE_INSENSITIVE);

    /**
     * "true", "false" or "unknown". A hint only. True: a message that says "mentioned you", or a group message that
     * contains one of the owner's names (whole word, optionally with @). False: a one-to-one chat, or a group
     * message with names known and none found. Unknown: a group message but no names configured.
     */
    public static String mention(boolean group, String title, String text, Set<String> names) {
        String hay = ((title == null ? "" : title) + " " + (text == null ? "" : text)).toLowerCase(Locale.ROOT);
        if (EXPLICIT_MENTION.matcher(hay).find()) return "true";
        if (!group) return "false";
        if (names == null || names.isEmpty()) return "unknown";
        for (String n : names) {
            if (n == null || n.trim().length() < 2) continue;
            String name = n.trim().toLowerCase(Locale.ROOT);
            Pattern p = Pattern.compile("(?<![\\p{L}\\p{N}_])@?" + Pattern.quote(name) + "(?![\\p{L}\\p{N}_])");
            if (p.matcher(hay).find()) return "true";
        }
        return "false";
    }

    // ---- de-dupe: the same key + text inside the window is forwarded once ----

    /** Remembers recent (key, title, text) triples. Bounded: the oldest entry goes first. Instance, so tests are isolated. */
    public static final class Dedupe {
        private final int max;
        private final long windowMs;
        private final LinkedHashMap<String, Long> seen = new LinkedHashMap<>();

        public Dedupe() {
            this(300, DEDUPE_WINDOW_MS);
        }

        public Dedupe(int max, long windowMs) {
            this.max = max;
            this.windowMs = windowMs;
        }

        private static String norm(String s) {
            return s == null ? "" : s.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
        }

        /** True the first time; false for a repeat of the same key and text within the window. */
        public synchronized boolean firstSeen(String key, String title, String text, long nowMillis) {
            for (Iterator<Map.Entry<String, Long>> it = seen.entrySet().iterator(); it.hasNext(); ) {
                if (Math.abs(nowMillis - it.next().getValue()) >= windowMs) it.remove();
            }
            String k = (key == null ? "" : key) + "|" + norm(title) + "|" + norm(text);
            Long prev = seen.get(k);
            if (prev != null && Math.abs(nowMillis - prev) < windowMs) return false;
            seen.remove(k);
            seen.put(k, nowMillis);
            while (seen.size() > max) {
                Iterator<String> it = seen.keySet().iterator();
                it.next();
                it.remove();
            }
            return true;
        }

        public synchronized int size() {
            return seen.size();
        }
    }
}
