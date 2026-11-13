package dev.shikhar.laptophub.wake;

import android.content.Context;
import android.content.SharedPreferences;

/** The owner's on/off choice, the last problem, and the one pending "wake" waiting for the page to pick it up. */
final class WakeState {
    private WakeState() {}

    private static final String PREFS = "hub_wake";
    private static final long PENDING_TTL_MS = 120_000L;

    private static String pendingPersona;
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

    static String lastError() {
        return lastError;
    }

    static void setError(String message) {
        lastError = message == null ? "" : message;
    }

    static synchronized void putPending(String persona, String text) {
        pendingPersona = WakeLaunch.persona(persona);
        pendingText = WakeLaunch.clean(text);
        pendingAt = System.currentTimeMillis();
    }

    /** Returns {persona, text} once, or null when nothing (fresh) is waiting. */
    static synchronized String[] takePending() {
        if (pendingPersona == null || System.currentTimeMillis() - pendingAt > PENDING_TTL_MS) {
            pendingPersona = null;
            pendingText = null;
            return null;
        }
        String[] out = new String[] {pendingPersona, pendingText == null ? "" : pendingText};
        pendingPersona = null;
        pendingText = null;
        return out;
    }
}
