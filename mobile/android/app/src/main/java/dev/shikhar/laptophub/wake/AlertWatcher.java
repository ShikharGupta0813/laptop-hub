package dev.shikhar.laptophub.wake;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.webkit.CookieManager;

import androidx.core.app.NotificationCompat;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.List;

import javax.net.ssl.HttpsURLConnection;

import dev.shikhar.laptophub.MainActivity;
import dev.shikhar.laptophub.capture.HubStore;

/**
 * While the always-on service runs and the app is NOT on screen: every 30 s asks the hub for urgent alerts
 * (GET /api/alerts/pending), speaks them with the phone's voice, rings like an alarm for the most urgent ones and
 * "call" reminders, then acks them (POST /api/alerts/pending/ack). Runs in its own thread; never touches the main or
 * audio thread. Uses the login cookie the app already holds (nothing is stored). Alert text is never logged.
 */
final class AlertWatcher {
    /** What the service offers: say something with the phone's voice (serialised with voice replies). */
    interface Voice {
        void say(String text, boolean alarm);
    }

    static final long POLL_MS = 30_000L;        // normal check interval (easy to change)
    static final long BACKOFF_MS = 120_000L;    // after a failure or a logout
    static final long FIRST_RING_MS = 6_000L;   // ring, then read the text, then ring again
    static final long SECOND_RING_MS = 8_000L;  // about 20 s in all with the reading in between
    static final String CH_ALARM = "wake_alarm";
    static final int ID_ALARM = 4103;
    static final String ACTION_ALERT_STOP = "dev.shikhar.laptophub.wake.ALERT_STOP";
    static final String EXTRA_OPEN_HASH = "open_hash";
    private static final String HASH_ALERTS = "#/alerts";

    private enum Outcome { OK, AUTH, FAIL }

    private static final class Fetched {
        final Outcome outcome;
        final String body;

        Fetched(Outcome o, String b) {
            outcome = o;
            body = b;
        }
    }

    private final Context ctx;
    private final Voice voice;
    private final AlertRinger ringer;
    private final AlertSpeech.Seen seen = new AlertSpeech.Seen(200);
    private volatile boolean stopped = false;
    private volatile HttpURLConnection active;
    private Thread thread;

    AlertWatcher(Context c, Voice voice) {
        this.ctx = c.getApplicationContext();
        this.voice = voice;
        this.ringer = new AlertRinger(c);
    }

    static void createChannel(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel ch = new NotificationChannel(CH_ALARM, "Urgent alerts and call reminders",
            NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Heads-up screen for an urgent alert; the app rings and speaks it itself.");
        ch.setShowBadge(false);
        ch.setSound(null, null);          // the alarm sound is played by the app at alarm volume, not by this channel
        ch.enableVibration(false);
        nm.createNotificationChannel(ch);
    }

    void start() {
        if (thread != null) return;
        thread = new Thread(this::loop, "wake-alerts");
        thread.setDaemon(true);
        thread.start();
    }

    void stop() {
        stopped = true;
        ringer.dismiss();
        try {
            HttpURLConnection c = active;
            if (c != null) c.disconnect();
        } catch (Exception ignored) {
        }
        if (thread != null) thread.interrupt();
    }

    /** The Stop button of the alarm notification. */
    void dismissAlarm() {
        ringer.dismiss();
        cancelNotification();
    }

    // ---- the loop -----------------------------------------------------------------------------------------------
    private void loop() {
        boolean needSettings = true;     // read "spoken_alerts" once at the start and again after a logout was fixed
        boolean spokenAlerts = true;
        long delay = POLL_MS;
        while (!stopped) {
            sleepResponsive(delay);
            if (stopped) break;
            delay = POLL_MS;
            // The app on screen handles alerts itself; the owner can switch the phone side off.
            if (MainActivity.visible || !WakeState.urgentVoice(ctx)) continue;
            try {
                if (needSettings) {
                    Fetched s = get("/api/friday/settings");
                    if (s.outcome == Outcome.AUTH) {
                        delay = BACKOFF_MS;
                        continue;
                    }
                    if (s.outcome == Outcome.FAIL) {
                        delay = BACKOFF_MS;
                        continue;
                    }
                    spokenAlerts = AlertSpeech.parseSpokenAlerts(s.body);
                    needSettings = false;
                }
                if (!spokenAlerts) continue;      // the owner switched spoken alerts off on the hub (seen again after a restart)
                Fetched f = get("/api/alerts/pending");
                if (f.outcome == Outcome.AUTH) {
                    needSettings = true;           // logged out: stay quiet, look at the switch again once it works
                    delay = BACKOFF_MS;
                    continue;
                }
                if (f.outcome == Outcome.FAIL) {
                    delay = BACKOFF_MS;
                    continue;
                }
                handle(AlertSpeech.parse(f.body));
            } catch (Throwable t) {
                delay = BACKOFF_MS;               // never let the loop die; the text is not logged
            }
        }
    }

    private void handle(List<AlertSpeech.Item> items) {
        for (AlertSpeech.Item it : items) {
            if (stopped || MainActivity.visible || !WakeState.urgentVoice(ctx)) return;   // the page takes over
            if (seen.has(it.id)) {
                ack(it.id);                       // an earlier ack failed: try again, do not speak again
                continue;
            }
            if (!AlertSpeech.shouldSpeak(it)) continue;
            seen.add(it.id);                      // before speaking: a crash must not make it repeat
            if (AlertSpeech.shouldRing(it)) alarm(it);
            else voice.say(AlertSpeech.shortText(it), false);
            ack(it.id);
        }
    }

    private void alarm(AlertSpeech.Item it) {
        ringer.reset();
        String text = AlertSpeech.fullText(it);
        postNotification(AlertSpeech.cap(text, AlertSpeech.SHORT_CAP));
        ringer.ring(FIRST_RING_MS, () -> stopped);
        if (!ringer.isDismissed() && !stopped) voice.say(text, true);
        if (!ringer.isDismissed() && !stopped) ringer.ring(SECOND_RING_MS, () -> stopped);
    }

    // ---- notification (heads-up / full screen on the lock screen) -----------------------------------------------
    private PendingIntent openIntent(int code) {
        Intent open = new Intent(ctx, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(EXTRA_OPEN_HASH, HASH_ALERTS);
        return PendingIntent.getActivity(ctx, code, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private void postNotification(String shortText) {
        try {
            Intent stop = new Intent(ctx, WakeService.class).setAction(ACTION_ALERT_STOP);
            PendingIntent stopPi = PendingIntent.getService(ctx, 5, stop,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            Notification publicVersion = new NotificationCompat.Builder(ctx, CH_ALARM)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("Urgent alert")
                .build();
            Notification n = new NotificationCompat.Builder(ctx, CH_ALARM)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("Urgent alert")
                .setContentText(shortText)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(shortText))
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setPublicVersion(publicVersion)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setTimeoutAfter(10 * 60_000L)
                .setContentIntent(openIntent(6))
                .setFullScreenIntent(openIntent(7), true)
                .addAction(0, "Open", openIntent(8))
                .addAction(0, "Stop", stopPi)
                .build();
            ((NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE)).notify(ID_ALARM, n);
        } catch (Exception ignored) {   // notifications refused: the ring and the voice still work
        }
    }

    private void cancelNotification() {
        try {
            ((NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE)).cancel(ID_ALARM);
        } catch (Exception ignored) {
        }
    }

    // ---- hub calls ----------------------------------------------------------------------------------------------
    private Fetched get(String path) {
        return call("GET", path, null);
    }

    private void ack(String id) {
        try {
            call("POST", "/api/alerts/pending/ack", AlertSpeech.ackBody(Collections.singletonList(id)));
        } catch (Exception ignored) {
        }
    }

    private Fetched call(String method, String path, String json) {
        HttpURLConnection conn = null;
        try {
            String origin = HubStore.origin(ctx);
            if (origin == null) return new Fetched(Outcome.FAIL, "");
            String cookie = CookieManager.getInstance().getCookie(origin + path);
            if (cookie == null || cookie.isEmpty()) return new Fetched(Outcome.AUTH, "");
            conn = (HttpsURLConnection) new URL(origin + path).openConnection();
            active = conn;
            conn.setRequestMethod(method);
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(15_000);
            conn.setInstanceFollowRedirects(false);
            conn.setRequestProperty("Accept", "application/json");
            conn.setRequestProperty("X-Hub", "1");
            conn.setRequestProperty("Cookie", cookie);
            if (json != null) {
                byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json");
                conn.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream out = conn.getOutputStream()) {
                    out.write(bytes);
                }
            }
            int code = conn.getResponseCode();
            if (code == 401 || code == 403) return new Fetched(Outcome.AUTH, "");
            if (code < 200 || code >= 300) return new Fetched(Outcome.FAIL, "");
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            try (InputStream in = conn.getInputStream()) {
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0 && buf.size() < 512 * 1024) buf.write(b, 0, n);
            }
            return new Fetched(Outcome.OK, buf.toString("UTF-8"));
        } catch (Exception e) {
            return new Fetched(Outcome.FAIL, "");
        } finally {
            active = null;
            if (conn != null) conn.disconnect();
        }
    }

    private void sleepResponsive(long ms) {
        long end = System.currentTimeMillis() + ms;
        try {
            while (!stopped && System.currentTimeMillis() < end) Thread.sleep(200);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
