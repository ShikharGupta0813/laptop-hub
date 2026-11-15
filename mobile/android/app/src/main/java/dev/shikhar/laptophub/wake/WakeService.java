package dev.shikhar.laptophub.wake;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.webkit.CookieManager;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import org.vosk.Model;
import org.vosk.Recognizer;

import java.io.File;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.Timer;
import java.util.TimerTask;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import javax.net.ssl.HttpsURLConnection;

import org.json.JSONObject;

import dev.shikhar.laptophub.MainActivity;
import dev.shikhar.laptophub.capture.HubStore;

/**
 * Foreground service (type microphone) that listens for "Hey <name>" (the names of the owner's enabled assistants;
 * Jarvis and Friday until the hub list has reached the phone) while the screen is off.
 * Audio is processed on the phone by Vosk and is never stored or sent anywhere. Started only from the visible
 * app (Android 14 forbids starting a microphone service from the background) and only when the owner switched
 * it on; it is NOT restarted by the system or at boot.
 */
public class WakeService extends Service {
    static final String ACTION_STOP = "dev.shikhar.laptophub.wake.STOP";

    private static final String CH_LISTEN = NotifyPolicy.CH_LISTEN;
    private static final String CH_ALERT = NotifyPolicy.CH_ALERT;
    private static final int ID_LISTEN = 4101;
    private static final int ID_ALERT = 4102;
    private static final int SAMPLE_RATE = 16000;
    private static final long PAUSE_AFTER_WAKE_MS = 10_000L;
    private static final long REST_MAX_MS = 4_000L;

    static volatile boolean running = false;

    private volatile boolean stopped = false;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;
    private TextToSpeech tts;                       // the phone's own voice, used for answers while the app is closed
    private final CountDownLatch ttsReady = new CountDownLatch(1);
    private volatile boolean ttsOk = false;
    private volatile CountDownLatch spoken;          // counted down when the current sentence has been spoken
    private volatile HttpURLConnection activeConn;   // the hub call in progress, closed on destroy
    private volatile AlertWatcher alerts;            // urgent alerts while the app is closed (own thread)
    private final Object speechLock = new Object();  // one voice at a time: replies and alerts never talk over each other
    private volatile boolean micHold = false;        // an alert is being spoken: the wake microphone stays off

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
        if (intent != null && AlertWatcher.ACTION_ALERT_STOP.equals(intent.getAction())) {
            // "Stop" on the alarm notification: silence the ring and the voice; the listening service goes on.
            AlertWatcher w = alerts;
            if (w != null) w.dismissAlarm();
            cancelSpeech();
            if (worker == null) stopSelf();   // the service was not really running: do not stay half started
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
            initTts();
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
            // Urgent alerts and call reminders while the app is closed: its own thread, same TTS, one voice at a time.
            alerts = new AlertWatcher(this, this::sayAlert);
            alerts.start();
        }
        return START_NOT_STICKY;   // never resurrected by the system: a microphone service cannot start from the background
    }

    @Override
    public void onDestroy() {
        stopped = true;
        running = false;
        try {
            if (alerts != null) alerts.stop();
        } catch (Exception ignored) {
        }
        try {
            HttpURLConnection c = activeConn;
            if (c != null) c.disconnect();
        } catch (Exception ignored) {
        }
        try {
            if (tts != null) {
                tts.stop();
                tts.shutdown();
            }
        } catch (Exception ignored) {
        }
        tts = null;
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
        for (String old : NotifyPolicy.LEGACY_CHANNELS) {
            try {
                nm.deleteNotificationChannel(old);      // an earlier build made it loud (high importance with a sound)
            } catch (Exception ignored) {
            }
        }
        NotificationChannel listen = new NotificationChannel(CH_LISTEN, "Listening for the wake word",
            NotifyPolicy.listenImportance());
        listen.setShowBadge(false);
        listen.setSound(null, null);
        listen.enableVibration(false);
        listen.enableLights(false);
        nm.createNotificationChannel(listen);
        NotificationChannel alert = new NotificationChannel(CH_ALERT, "Wake word heard (silent)",
            NotifyPolicy.alertImportance());
        alert.setShowBadge(false);
        alert.setSound(null, null);
        alert.enableVibration(false);
        alert.enableLights(false);
        nm.createNotificationChannel(alert);
        AlertWatcher.createChannel(this);   // heads-up screen for urgent alerts (the app rings by itself)
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
            .setContentTitle("Listening for your wake word")
            .setContentText("Audio stays on this phone. Tap Stop to turn it off.")
            .setOngoing(true)
            .setOnlyAlertOnce(NotifyPolicy.listenOnlyAlertOnce())
            .setSound(null)
            .setVibrate(new long[0])
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
        WakeMatcher.Table table;
        try {
            File dir = ModelManager.get(this).modelDirIfReady();
            if (dir == null) throw new IllegalStateException("The voice model is missing. Download it again.");
            model = new Model(dir.getAbsolutePath());
            Set<String> vocab = WakeGrammar.loadVocab(dir);   // null when the model ships no word list
            int version = WakeState.assistantsVersion();
            table = WakeMatcher.table(WakeState.assistants(this));
            rec = newRecognizer(model, vocab);
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
                if (micHold) {
                    // An alert is being spoken: release the microphone (so it does not hear the alert) and wait.
                    ar.stop();
                    rec.reset();
                    ring.clear();
                    while (micHold && !stopped) sleepQuietly(100);
                    if (!stopped) ar.startRecording();
                    continue;
                }
                int n = ar.read(buf, 0, buf.length);
                if (n < 0) {
                    if (++failedReads > 20) throw new IllegalStateException("The microphone stopped working.");
                    sleepQuietly(100);
                    continue;
                }
                failedReads = 0;
                if (n == 0) continue;
                ring.add(buf, n);
                if (version != WakeState.assistantsVersion()) {
                    // The owner renamed or added an assistant: switch to the new names without stopping the service.
                    version = WakeState.assistantsVersion();
                    try {
                        Recognizer fresh = newRecognizer(model, vocab);
                        table = WakeMatcher.table(WakeState.assistants(this));
                        try {
                            rec.close();
                        } catch (Throwable ignored) {
                        }
                        rec = fresh;
                    } catch (Throwable ignored) {   // keep listening with the old names rather than stop
                    }
                }
                boolean fin = rec.acceptWaveForm(buf, n);
                WakeMatcher.Match m = WakeMatcher.match(table, fin ? rec.getResult() : rec.getPartialResult(), fin);
                if (m == null) continue;
                String rest = captureRest(model, ar, ring.snapshot(), table);
                // Do not hear ourselves or the app: release the microphone first (the spoken answer comes next).
                ar.stop();
                rec.reset();
                ring.clear();
                // Runs to the end (hub call and speech) before listening again, so only one reply at a time:
                // a wake phrase said meanwhile is simply not heard.
                onWake(m.assistant, rest.isEmpty() ? m.rest : rest);
                sleepResponsive(PAUSE_AFTER_WAKE_MS);   // at least this long after the end of the reply
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
     * A recogniser limited to the grammar of the current names. Names the model does not know are left out when the
     * model's word list is available; if Vosk still refuses the grammar, the built-in Jarvis/Friday grammar is used
     * and the Settings card says so (WakeState.notice).
     */
    private Recognizer newRecognizer(Model model, Set<String> vocab) throws java.io.IOException {
        WakeGrammar.Result g = WakeGrammar.build(WakeState.assistants(this), vocab);
        String notice = g.notice();
        Recognizer r;
        try {
            r = new Recognizer(model, (float) SAMPLE_RATE, g.json);
        } catch (Throwable t) {   // the grammar was refused: fall back to the one that always worked
            r = new Recognizer(model, (float) SAMPLE_RATE, WakeGrammar.DEFAULT_JSON);
            notice = WakeGrammar.NOTICE_PARTIAL;
        }
        WakeState.setNotice(notice);
        return r;
    }

    /**
     * The wake phrase was heard by the small grammar. Re-listen to the last seconds with a free-speech
     * recognizer (same model) to get the rest of the sentence, for at most a few seconds. "" if nothing.
     */
    private String captureRest(Model model, AudioRecord ar, short[] recent, WakeMatcher.Table table) {
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
            WakeMatcher.Match m = WakeMatcher.match(table, done ? free.getResult() : free.getFinalResult(), true);
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

    private void onWake(String assistantId, String rest) {
        vibrate();
        String p = WakeLaunch.assistant(assistantId);
        String text = WakeLaunch.clean(rest);
        // App in front, or the owner switched the spoken answer off: today's behaviour, the page answers.
        if (MainActivity.visible || !WakeState.backgroundReply(this)) {
            openApp(p, text, true);
            return;
        }
        if (WakeReply.isOpenApp(text)) {
            openApp(p, "", true);   // just open the app, do not send "open redline" to the assistant
            speak("Opening RedLine");
            return;
        }
        if (text.isEmpty()) {
            speak("Yes?");   // the microphone listens again after the usual pause
            return;
        }
        replyByVoice(p, text);
    }

    /** Stores the sentence for the page, tries to open the app and posts the "tap to talk" notification (full-screen intent). */
    private void openApp(String p, String text, boolean tryLaunch) {
        WakeState.putPending(p, text);   // the page picks this up as soon as it is visible
        Intent launch = new Intent(this, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra("wake_assistant", p)
            .putExtra("wake_text", text)
            .putExtra("wake_hash", WakeLaunch.hash(p, text));
        if (tryLaunch) {
            try {
                startActivity(launch);   // allowed only in some situations; the notification below is the fallback
            } catch (Exception ignored) {
            }
        }
        try {
            Notification n = new NotificationCompat.Builder(this, CH_ALERT)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle(displayName(p) + " is listening")
                .setContentText("Tap to talk")
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setSound(null)
                .setVibrate(new long[0])
                .setTimeoutAfter(15_000L)
                .setContentIntent(openAppIntent(launch, 2))
                .setFullScreenIntent(openAppIntent(launch, 3), true)
                .build();
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.notify(ID_ALERT, n);
        } catch (Exception ignored) {
        }
    }

    // ---- spoken answer while the app is closed -----------------------------------------------------------------
    private static final int REPLY_CAP_MS = 120_000;
    private static final long SPEAK_MAX_MS = 90_000L;

    private void initTts() {
        try {
            tts = new TextToSpeech(getApplicationContext(), status -> {
                TextToSpeech t = tts;
                if (status == TextToSpeech.SUCCESS && t != null) {
                    try {
                        t.setLanguage(Locale.getDefault());
                    } catch (Exception ignored) {
                    }
                    t.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                        @Override public void onStart(String id) {}
                        @Override public void onDone(String id) { done(); }
                        @Override public void onError(String id) { done(); }
                        private void done() {
                            CountDownLatch l = spoken;
                            if (l != null) l.countDown();
                        }
                    });
                    ttsOk = true;
                }
                ttsReady.countDown();
            });
        } catch (Exception e) {
            ttsOk = false;
            ttsReady.countDown();
        }
    }

    /** Says the text with the phone's voice and waits until it is finished (the microphone is off meanwhile). */
    private void speak(String text) {
        speakNow(text, false);
    }

    /** From the alert thread: keeps the wake microphone off while talking (it must not hear the alert). */
    private void sayAlert(String text, boolean alarm) {
        micHold = true;
        try {
            speakNow(text, alarm);
        } finally {
            micHold = false;
        }
    }

    /** The "Stop" button: end whatever is being spoken now. */
    private void cancelSpeech() {
        try {
            if (tts != null) tts.stop();
        } catch (Exception ignored) {
        }
        CountDownLatch l = spoken;
        if (l != null) l.countDown();
    }

    private void speakNow(String text, boolean alarm) {
        if (text == null || text.isEmpty() || stopped) return;
        synchronized (speechLock) {   // replies and alerts take turns
            try {
                if (!ttsReady.await(5, TimeUnit.SECONDS) || !ttsOk || tts == null) return;
                try {
                    tts.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(alarm ? AudioAttributes.USAGE_ALARM : AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build());
                } catch (Exception ignored) {
                }
                CountDownLatch l = new CountDownLatch(1);
                spoken = l;
                if (tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "wake-reply") != TextToSpeech.SUCCESS) return;
                long end = System.currentTimeMillis() + SPEAK_MAX_MS;
                while (!stopped && System.currentTimeMillis() < end) {
                    if (l.await(200, TimeUnit.MILLISECONDS)) break;
                }
                if (l.getCount() > 0 && tts != null) tts.stop();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } catch (Exception ignored) {
            }
        }
    }

    private static final class HubReply {
        String speech = "";          // what to say
        boolean tap = false;         // also post the "tap to talk" notification
    }

    /** Asks the hub (blocking, on the listening thread) and speaks the answer. Never logs the sentence or the answer. */
    private void replyByVoice(String p, String text) {
        HubReply r = askHub(p, text);
        if (r.tap) {
            openApp(p, text, false);   // the sentence waits in the app; nothing is launched from the background
        }
        speak(r.speech);
    }

    private HubReply askHub(String p, String text) {
        HubReply r = new HubReply();
        String unreachable = "I can't reach the laptop";
        HttpURLConnection conn = null;
        Timer cap = null;
        StringBuilder answer = new StringBuilder();
        try {
            String origin = HubStore.origin(this);
            if (origin == null) {
                r.speech = unreachable;
                r.tap = true;
                return r;
            }
            String cookie = CookieManager.getInstance().getCookie(origin + "/api/assistant/message");
            if (cookie == null || cookie.isEmpty()) {
                r.speech = "Please open the app and log in";
                r.tap = true;
                return r;
            }
            JSONObject body = new JSONObject();
            body.put("session_id", "wake-" + p);
            body.put("text", text);
            body.put("provider", "auto");
            body.put("auto_approve", false);
            body.put("persona", p);
            body.put("voice", false);
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            conn = (HttpsURLConnection) new URL(origin + "/api/assistant/message").openConnection();
            final HttpURLConnection hc = conn;
            activeConn = hc;
            cap = new Timer("wake-cap", true);
            cap.schedule(new TimerTask() {
                @Override public void run() {
                    hc.disconnect();   // 120 s overall cap, also frees a read that is stuck
                }
            }, REPLY_CAP_MS);
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(REPLY_CAP_MS);
            conn.setInstanceFollowRedirects(false);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("Accept", "text/event-stream");
            conn.setRequestProperty("X-Hub", "1");
            conn.setRequestProperty("Cookie", cookie);
            conn.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream out = conn.getOutputStream()) {
                out.write(bytes);
            }
            int code = conn.getResponseCode();
            if (code == 401 || code == 403) {
                r.speech = "Please open the app and log in";
                r.tap = true;
                return r;
            }
            if (code < 200 || code >= 300) {
                r.speech = unreachable;
                r.tap = true;
                return r;
            }
            WakeReply.SseParser parser = WakeReply.parseSse();
            char[] buf = new char[2048];
            boolean finished = false;
            try (Reader in = new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8)) {
                int n;
                while (!finished && !stopped && (n = in.read(buf)) > 0) {
                    List<WakeReply.Event> events = parser.feed(new String(buf, 0, n));
                    for (WakeReply.Event ev : events) {
                        if ("text".equals(ev.type)) {
                            answer.append(ev.text);
                        } else if ("tool_start".equals(ev.type) && ev.needsApproval) {
                            conn.disconnect();   // never approve in the background: close the stream and ask for a tap
                            r.speech = "I need your tap for that";
                            r.tap = true;
                            return r;
                        } else if ("error".equals(ev.type)) {
                            String m = WakeReply.speakable(ev.message);
                            r.speech = m.isEmpty() ? "Something went wrong" : "Something went wrong. " + m;
                            return r;
                        } else if ("done".equals(ev.type)) {
                            finished = true;
                            break;
                        }
                    }
                }
            }
            String said = WakeReply.speakable(answer.toString());
            r.speech = said.isEmpty() ? "I have no answer for that" : said;
            return r;
        } catch (Exception e) {
            // Network failure, timeout or the cap closing the stream: say what we already have, else the laptop is unreachable.
            String said = WakeReply.speakable(answer.toString());
            if (!said.isEmpty()) {
                r.speech = said;
            } else {
                r.speech = unreachable;
                r.tap = true;
            }
            return r;
        } finally {
            if (cap != null) cap.cancel();
            activeConn = null;
            if (conn != null) conn.disconnect();
        }
    }

    /** "Jarvis" for "jarvis", "Atlas" for "atlas": a readable label from the id (the phone only stores ids and words). */
    private static String displayName(String id) {
        if (id == null || id.isEmpty()) return "Your assistant";
        return Character.toUpperCase(id.charAt(0)) + id.substring(1).replace('_', ' ').replace('-', ' ');
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
