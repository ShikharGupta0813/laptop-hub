package dev.shikhar.laptophub.wake;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import org.vosk.Model;
import org.vosk.Recognizer;

import java.io.File;

import dev.shikhar.laptophub.MainActivity;

/**
 * Foreground service (type microphone) that listens for "Hey Jarvis" / "Hey Friday" while the screen is off.
 * Audio is processed on the phone by Vosk and is never stored or sent anywhere. Started only from the visible
 * app (Android 14 forbids starting a microphone service from the background) and only when the owner switched
 * it on; it is NOT restarted by the system or at boot.
 */
public class WakeService extends Service {
    static final String ACTION_STOP = "dev.shikhar.laptophub.wake.STOP";

    private static final String CH_LISTEN = "wake_listen";
    private static final String CH_ALERT = "wake_alert";
    private static final int ID_LISTEN = 4101;
    private static final int ID_ALERT = 4102;
    private static final int SAMPLE_RATE = 16000;
    private static final long PAUSE_AFTER_WAKE_MS = 10_000L;
    private static final long REST_MAX_MS = 4_000L;
    private static final String GRAMMAR =
        "[\"hey jarvis\",\"hey friday\",\"ok jarvis\",\"okay jarvis\",\"ok friday\",\"okay friday\",\"jarvis\",\"friday\",\"[unk]\"]";

    static volatile boolean running = false;

    private volatile boolean stopped = false;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;

    /** Must be called while the app is visible (a button tap). */
    static void start(Context c) {
        ContextCompat.startForegroundService(c, new Intent(c, WakeService.class));
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, WakeService.class));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            WakeState.setEnabled(this, false);
            stopSelf();
            return START_NOT_STICKY;
        }
        try {
            createChannels();
            int type = Build.VERSION.SDK_INT >= 30 ? ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE : 0;
            ServiceCompat.startForeground(this, ID_LISTEN, listeningNotification(), type);
        } catch (Exception e) {   // no microphone permission, or Android refused a background start
            WakeState.setError("Android would not start the listening service: " + e.getClass().getSimpleName());
            WakeState.setEnabled(this, false);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (worker == null) {
            running = true;
            try {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "laptophub:wake");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire();
            } catch (Exception ignored) {
                wakeLock = null;
            }
            worker = new Thread(this::listenLoop, "wake-listen");
            worker.start();
        }
        return START_NOT_STICKY;   // never resurrected by the system: a microphone service cannot start from the background
    }

    @Override
    public void onDestroy() {
        stopped = true;
        running = false;
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    // ---- notifications ----------------------------------------------------------------------------
    private void createChannels() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel listen = new NotificationChannel(CH_LISTEN, "Listening for the wake word",
            NotificationManager.IMPORTANCE_LOW);
        listen.setShowBadge(false);
        nm.createNotificationChannel(listen);
        NotificationChannel alert = new NotificationChannel(CH_ALERT, "Wake word heard",
            NotificationManager.IMPORTANCE_HIGH);
        nm.createNotificationChannel(alert);
    }

    private PendingIntent openAppIntent(Intent launch, int requestCode) {
        return PendingIntent.getActivity(this, requestCode, launch,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification listeningNotification() {
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        Intent stop = new Intent(this, WakeService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new NotificationCompat.Builder(this, CH_LISTEN)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Listening for Hey Jarvis or Hey Friday")
            .setContentText("Audio stays on this phone. Tap Stop to turn it off.")
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setContentIntent(openAppIntent(open, 0))
            .addAction(0, "Stop", stopPi)
            .build();
    }

    // ---- listening --------------------------------------------------------------------------------
    /** Last few seconds of audio, so the words said right after the wake phrase are not lost. */
    private static final class Ring {
        private final short[] data = new short[SAMPLE_RATE * 3];
        private int head = 0;
        private int count = 0;

        void add(short[] src, int n) {
            for (int i = 0; i < n; i++) {
                data[head] = src[i];
                head = (head + 1) % data.length;
            }
            count = Math.min(data.length, count + n);
        }

        short[] snapshot() {
            short[] out = new short[count];
            int start = (head - count + data.length) % data.length;
            for (int i = 0; i < count; i++) out[i] = data[(start + i) % data.length];
            return out;
        }

        void clear() {
            head = 0;
            count = 0;
        }
    }

    private void listenLoop() {
        Model model = null;
        Recognizer rec = null;
        AudioRecord ar = null;
        try {
            File dir = ModelManager.get(this).modelDirIfReady();
            if (dir == null) throw new IllegalStateException("The voice model is missing. Download it again.");
            model = new Model(dir.getAbsolutePath());
            rec = new Recognizer(model, (float) SAMPLE_RATE, GRAMMAR);
            int min = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            int bufBytes = Math.max(min, SAMPLE_RATE * 2);
            ar = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufBytes);
            if (ar.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IllegalStateException("The microphone is busy or not allowed.");
            }
            ar.startRecording();
            short[] buf = new short[SAMPLE_RATE / 10];
            Ring ring = new Ring();
            int failedReads = 0;
            while (!stopped) {
                int n = ar.read(buf, 0, buf.length);
                if (n < 0) {
                    if (++failedReads > 20) throw new IllegalStateException("The microphone stopped working.");
                    sleepQuietly(100);
                    continue;
                }
                failedReads = 0;
                if (n == 0) continue;
                ring.add(buf, n);
                boolean fin = rec.acceptWaveForm(buf, n);
                WakeMatcher.Match m = WakeMatcher.match(fin ? rec.getResult() : rec.getPartialResult(), fin);
                if (m == null) continue;
                String rest = captureRest(model, ar, ring.snapshot());
                onWake(m.persona, rest.isEmpty() ? m.rest : rest);
                // Do not hear ourselves or the app: release the microphone for a while, then start fresh.
                ar.stop();
                rec.reset();
                ring.clear();
                sleepResponsive(PAUSE_AFTER_WAKE_MS);
                if (!stopped) ar.startRecording();
            }
        } catch (Throwable t) {   // includes UnsatisfiedLinkError if the native Vosk library is missing
            String msg = t.getMessage();
            WakeState.setError(msg == null || msg.isEmpty() ? t.getClass().getSimpleName() : msg);
            WakeState.setEnabled(this, false);
        } finally {
            try {
                if (ar != null) {
                    if (ar.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) ar.stop();
                    ar.release();
                }
            } catch (Throwable ignored) {
            }
            try {
                if (rec != null) rec.close();
                if (model != null) model.close();
            } catch (Throwable ignored) {
            }
            running = false;
            stopSelf();
        }
    }

    /**
     * The wake phrase was heard by the small grammar. Re-listen to the last seconds with a free-speech
     * recognizer (same model) to get the rest of the sentence, for at most a few seconds. "" if nothing.
     */
    private String captureRest(Model model, AudioRecord ar, short[] recent) {
        Recognizer free = null;
        try {
            free = new Recognizer(model, (float) SAMPLE_RATE);
            boolean done = free.acceptWaveForm(recent, recent.length);
            long end = System.currentTimeMillis() + REST_MAX_MS;
            short[] buf = new short[SAMPLE_RATE / 10];
            while (!done && !stopped && System.currentTimeMillis() < end) {
                int n = ar.read(buf, 0, buf.length);
                if (n > 0) done = free.acceptWaveForm(buf, n);
            }
            WakeMatcher.Match m = WakeMatcher.match(done ? free.getResult() : free.getFinalResult(), true);
            return m == null ? "" : m.rest;
        } catch (Throwable t) {
            return "";
        } finally {
            if (free != null) {
                try {
                    free.close();
                } catch (Throwable ignored) {
                }
            }
        }
    }

    private void onWake(String persona, String rest) {
        vibrate();
        String p = WakeLaunch.persona(persona);
        String text = WakeLaunch.clean(rest);
        WakeState.putPending(p, text);   // the page picks this up as soon as it is visible
        Intent launch = new Intent(this, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra("wake_persona", p)
            .putExtra("wake_text", text)
            .putExtra("wake_hash", WakeLaunch.hash(p, text));
        try {
            startActivity(launch);   // allowed only in some situations; the notification below is the fallback
        } catch (Exception ignored) {
        }
        try {
            Notification n = new NotificationCompat.Builder(this, CH_ALERT)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle(p.equals("friday") ? "Friday is listening" : "Jarvis is listening")
                .setContentText("Tap to talk")
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setAutoCancel(true)
                .setTimeoutAfter(15_000L)
                .setContentIntent(openAppIntent(launch, 2))
                .setFullScreenIntent(openAppIntent(launch, 3), true)
                .build();
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.notify(ID_ALERT, n);
        } catch (Exception ignored) {
        }
    }

    private void vibrate() {
        try {
            Vibrator v;
            if (Build.VERSION.SDK_INT >= 31) {
                v = ((VibratorManager) getSystemService(Context.VIBRATOR_MANAGER_SERVICE)).getDefaultVibrator();
            } else {
                v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            }
            if (Build.VERSION.SDK_INT >= 26) {
                v.vibrate(VibrationEffect.createOneShot(150, VibrationEffect.DEFAULT_AMPLITUDE));
            } else {
                v.vibrate(150);
            }
        } catch (Exception ignored) {
        }
    }

    private void sleepResponsive(long ms) {
        long end = System.currentTimeMillis() + ms;
        while (!stopped && System.currentTimeMillis() < end) sleepQuietly(100);
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
