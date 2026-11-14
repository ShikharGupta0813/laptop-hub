package dev.shikhar.laptophub.wake;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Pure helpers for the assistants list the phone keeps (SharedPreferences) between hub refreshes.
 * Stored as one plain string, "id:name,name;id:name": ids and names never contain ':' ',' or ';' once cleaned.
 * No org.json on purpose, so it runs in JVM unit tests.
 */
public final class WakeAssistants {
    private WakeAssistants() {}

    public static final int MAX_ASSISTANTS = 8;
    public static final int MAX_NAMES = 30;

    private static String cleanWord(String w) {
        if (w == null) return "";
        return w.toLowerCase(Locale.ROOT).replace('’', '\'').replaceAll("[^a-z0-9' -]", "").trim();
    }

    /** Cleans a list from the hub: valid ids only, no duplicates, bounded sizes, usable names only. */
    public static List<WakeMatcher.Assistant> sanitize(List<WakeMatcher.Assistant> in) {
        List<WakeMatcher.Assistant> out = new ArrayList<>();
        if (in == null) return out;
        List<String> ids = new ArrayList<>();
        for (WakeMatcher.Assistant a : in) {
            if (a == null || !WakeMatcher.validId(a.id) || ids.contains(a.id) || out.size() >= MAX_ASSISTANTS) continue;
            List<String> names = new ArrayList<>();
            if (a.names != null) {
                for (String n : a.names) {
                    String c = cleanWord(n);
                    if (c.length() >= 2 && c.length() <= WakeMatcher.MAX_NAME_CHARS && !names.contains(c)
                        && names.size() < MAX_NAMES) {
                        names.add(c);
                    }
                }
            }
            if (names.isEmpty()) continue;
            ids.add(a.id);
            out.add(new WakeMatcher.Assistant(a.id, names));
        }
        return out;
    }

    public static String encode(List<WakeMatcher.Assistant> list) {
        StringBuilder sb = new StringBuilder();
        for (WakeMatcher.Assistant a : sanitize(list)) {
            if (sb.length() > 0) sb.append(';');
            sb.append(a.id).append(':').append(String.join(",", a.names));
        }
        return sb.toString();
    }

    /** Never null; an empty or broken string gives an empty list (callers then use the built-ins). */
    public static List<WakeMatcher.Assistant> decode(String s) {
        List<WakeMatcher.Assistant> out = new ArrayList<>();
        if (s == null || s.isEmpty()) return out;
        for (String part : s.split(";")) {
            int c = part.indexOf(':');
            if (c <= 0) continue;
            List<String> names = new ArrayList<>();
            for (String n : part.substring(c + 1).split(",")) {
                if (!n.isEmpty()) names.add(n);
            }
            out.add(new WakeMatcher.Assistant(part.substring(0, c), names));
        }
        return sanitize(out);
    }
}
