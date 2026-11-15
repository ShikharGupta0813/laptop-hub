package dev.shikhar.laptophub.wake;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;

/**
 * Rings like an alarm: the phone's default alarm sound at alarm volume (AudioAttributes.USAGE_ALARM) plus vibration.
 * This is NOT a phone call; it only plays a local sound. Used from the alert thread only (never the main or audio thread).
 */
final class AlertRinger {
    private final Context ctx;
    private volatile boolean dismissed = false;
    private Ringtone tone;

    AlertRinger(Context c) {
        this.ctx = c.getApplicationContext();
    }

    /** A new alarm starts: forget an earlier Stop. */
    void reset() {
        dismissed = false;
    }

    boolean isDismissed() {
        return dismissed;
    }

    /** The owner tapped Stop (or the service is ending): silence at once and ignore the rest of this alarm. */
    void dismiss() {
        dismissed = true;
        silence();
    }

    /** Rings for at most ms or until dismissed. Blocks the calling (alert) thread. true when it was not dismissed. */
    boolean ring(long ms, java.util.function.BooleanSupplier stopped) {
        if (dismissed) return false;
        start();
        long end = System.currentTimeMillis() + ms;
        try {
            while (!dismissed && !stopped.getAsBoolean() && System.currentTimeMillis() < end) {
                Thread.sleep(100);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            silence();
        }
        return !dismissed;
    }

    private synchronized void start() {
        try {
            Uri uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
            if (uri == null) uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
            if (uri != null) {
                Ringtone r = RingtoneManager.getRingtone(ctx, uri);
                if (r != null) {
                    r.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build());
                    if (Build.VERSION.SDK_INT >= 28) r.setLooping(true);
                    r.play();
                    tone = r;
                }
            }
        } catch (Exception ignored) {
        }
        try {
            long[] pattern = {0, 700, 500};
            Vibrator v = vibrator();
            if (Build.VERSION.SDK_INT >= 26) v.vibrate(VibrationEffect.createWaveform(pattern, 0));
            else v.vibrate(pattern, 0);
        } catch (Exception ignored) {
        }
    }

    private synchronized void silence() {
        try {
            if (tone != null) tone.stop();
        } catch (Exception ignored) {
        }
        tone = null;
        try {
            vibrator().cancel();
        } catch (Exception ignored) {
        }
    }

    private Vibrator vibrator() {
        if (Build.VERSION.SDK_INT >= 31) {
            return ((VibratorManager) ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE)).getDefaultVibrator();
        }
        return (Vibrator) ctx.getSystemService(Context.VIBRATOR_SERVICE);
    }
}
