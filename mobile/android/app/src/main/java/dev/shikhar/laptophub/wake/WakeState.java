package dev.shikhar.laptophub.wake;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.List;

/** The owner's on/off choice, the last problem, and the one pending "wake" waiting for the page to pick it up. */
final class WakeState {
    private WakeState() {}

    private static final String PREFS = "hub_wake";
    private static final long PENDING_TTL_MS = 120_000L;

    private static String pendingAssistant;
    private static String pendingText;
    private static long pendingAt;
    private static volatile String lastError = "";

    private static SharedPreferences prefs(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean enabled(Context c) {
        return prefs(c).getBoolean("enabled", false);
    }

    static void setEnabled(Context c, boolean on) {
        prefs(c).edit().putBoolean("enabled", on).apply();
        if (on) lastError = "";
    }

    /** Answer by voice (phone speech) when the wake phrase is heard with the app closed. On unless the owner switched it off. */
    static boolean backgroundReply(Context c) {
        return prefs(c).getBoolean("bg_reply", true);
    }

    static void setBackgroundReply(Context c, boolean on) {
        prefs(c).edit().putBoolean("bg_reply", on).apply();
    }

    static String lastError() {
        return lastError;
    }

    static void setError(String message) {
        lastError = message == null ? "" : message;
    }

    // ---- the assistants list (names that wake the phone), kept between hub refreshes ---------------------
    private static final String KEY_ASSISTANTS = "assistants";
    private static volatile int assistantsVersion = 0;
    private static volatile String notice = "";

    /** The cached list; the built-in Jarvis and Friday when nothing was ever cached. */
    static List<WakeMatcher.Assistant> assistants(Context c) {
        List<WakeMatcher.Assistant> l = WakeAssistants.decode(prefs(c).getString(KEY_ASSISTANTS, ""));
        return l.isEmpty() ? WakeMatcher.defaults() : l;
    }

    /** Stores a list from the hub. true (and a new version, which the running service notices) only when it changed. */
    static synchronized boolean setAssistants(Context c, List<WakeMatcher.Assistant> list) {
        String enc = WakeAssistants.encode(list);
        if (enc.equals(prefs(c).getString(KEY_ASSISTANTS, ""))) return false;
        prefs(c).edit().putString(KEY_ASSISTANTS, enc).apply();
        assistantsVersion++;
        return true;
    }

    static int assistantsVersion() {
        return assistantsVersion;
    }

    /** A plain-language remark for the Settings card ("some names are not in the speech model..."), or "". */
    static String notice() {
        return notice;
    }

    static void setNotice(String message) {
        notice = message == null ? "" : message;
    }

    static synchronized void putPending(String assistant, String text) {
        pendingAssistant = WakeLaunch.assistant(assistant);
        pendingText = WakeLaunch.clean(text);
        pendingAt = System.currentTimeMillis();
    }

    /** Returns {assistant, text} once, or null when nothing (fresh) is waiting. */
    static synchronized String[] takePending() {
        if (pendingAssistant == null || System.currentTimeMillis() - pendingAt > PENDING_TTL_MS) {
            pendingAssistant = null;
            pendingText = null;
            return null;
        }
        String[] out = new String[] {pendingAssistant, pendingText == null ? "" : pendingText};
        pendingAssistant = null;
        pendingText = null;
        return out;
    }
}
