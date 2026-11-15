package dev.shikhar.laptophub.wake;

/**
 * The one place that says how loud the wake service's notifications are (pure, unit tested).
 * The always-on "listening" notification and the "assistant is listening" tap target must never ring, buzz or re-alert:
 * an old build created the alert channel at IMPORTANCE_HIGH with the default sound, which Android then kept for that
 * channel id forever. So the quiet alert channel has a NEW id and the old one is deleted at start-up.
 */
public final class NotifyPolicy {
    private NotifyPolicy() {}

    // Mirrors of android.app.NotificationManager.IMPORTANCE_* so this class needs no Android jar.
    public static final int IMPORTANCE_LOW = 2;
    public static final int IMPORTANCE_DEFAULT = 3;
    public static final int IMPORTANCE_HIGH = 4;

    public static final String CH_LISTEN = "wake_listen";
    public static final String CH_ALERT = "wake_alert_quiet";
    /** Channels created by earlier builds that may carry a sound: deleted so they cannot ring. */
    public static final String[] LEGACY_CHANNELS = {"wake_alert"};

    /** Foreground "listening" service: the lowest importance that still shows. */
    public static int listenImportance() {
        return IMPORTANCE_LOW;
    }

    /** The "tap to talk" fallback: heads-up for the full-screen intent, but silent. */
    public static int alertImportance() {
        return IMPORTANCE_HIGH;
    }

    public static boolean channelHasSound(String channelId) {
        return false;                                   // no channel of this service ever plays a sound
    }

    public static boolean channelVibrates(String channelId) {
        return false;
    }

    public static boolean isLegacy(String channelId) {
        for (String id : LEGACY_CHANNELS) if (id.equals(channelId)) return true;
        return false;
    }

    /** The foreground notification is posted once; it must never alert again when it is refreshed. */
    public static boolean listenOnlyAlertOnce() {
        return true;
    }
}
