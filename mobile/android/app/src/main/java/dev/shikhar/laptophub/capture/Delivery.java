package dev.shikhar.laptophub.capture;

import android.content.Context;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.HttpsURLConnection;

/**
 * Sends captured items (payments and watched mail/chat messages) to the hub, using the login cookie the WebView already holds.
 * Anything that cannot be delivered stays in the persisted queue and is retried on the next
 * capture, on app resume and on flush(). Nothing here logs notification text.
 */
final class Delivery {
    private Delivery() {}

    static final String PATH_CAPTURE = "/api/expenses/capture";
    static final String PATH_WATCH = "/api/watch/ingest";
    /** General alerts: notifications from any app the owner enabled (see AlertConfigStore / AlertPolicy). */
    static final String PATH_ALERTS = "/api/alerts/ingest";
    static final int TIMEOUT_MS = 10_000;
    /** One thread, so sends are strictly ordered and the queue head is never sent twice at once. */
    static final ExecutorService EXEC = Executors.newSingleThreadExecutor();

    static final class Result {
        final boolean sent;
        final int queued;
        final String error;
        Result(boolean sent, int queued, String error) {
            this.sent = sent;
            this.queued = queued;
            this.error = error;
        }
    }

    /** Queue a payment item (POST /api/expenses/capture), then try to send everything. Blocking. */
    static Result enqueueAndFlush(Context c, JSONObject payload) {
        return enqueueAndFlush(c, PATH_CAPTURE, payload);
    }

    /** Queue the item for {@code path}, then try to send everything. Blocking: call from EXEC or a worker thread. */
    static Result enqueueAndFlush(Context c, String path, JSONObject payload) {
        HubStore.enqueue(c, path, payload);
        return flush(c);
    }

    /** Only these paths are ever posted to; anything else found in the queue is dropped. */
    private static boolean knownPath(String path) {
        return PATH_CAPTURE.equals(path) || PATH_WATCH.equals(path);
    }

    /** Queue one alert event (a JSON line in its own file, 200 at most, oldest dropped), then try to send. Blocking. */
    static Result enqueueAlertAndFlush(Context c, JSONObject payload) {
        AlertConfigStore.queue(c).add(payload.toString());
        return flush(c);
    }

    /** Sends queued alert events oldest first. Same keep / drop rules as the payment queue. Returns an error code or null. */
    private static String flushAlerts(Context c, String origin, String cookie) {
        EventQueue q = AlertConfigStore.queue(c);
        while (true) {
            String line = q.peek();
            if (line == null) return null;
            int code = post(origin + PATH_ALERTS, cookie, line);
            if (code >= 200 && code < 300) {
                q.removeHead();
            } else if (code == 0 || code == 401 || code == 403 || code == 408 || code == 429 || code >= 500) {
                return code == 0 ? "network" : "http-" + code;
            } else {
                q.removeHead(); // rejected for good: do not block the queue
            }
        }
    }

    /**
     * Fetches the owner's capture list from the hub and caches it. Keeps the old cache on any failure (offline,
     * logged out, 404 from an older hub). Blocking: call from EXEC. Returns true when a new list was stored.
     */
    static synchronized boolean refreshConfig(Context c) {
        try {
            String origin = HubStore.origin(c);
            if (origin == null) return false;
            String cookie = CookieManager.getInstance().getCookie(origin + PATH_CAPTURE);
            if (cookie == null || cookie.isEmpty()) return false;
            String body = get(origin + AlertConfigStore.PATH_CONFIG, cookie);
            return body != null && AlertConfigStore.save(c, body);
        } catch (Throwable ignored) {
            return false;
        }
    }

    /** Refresh when the cache is older than 15 minutes. Blocking. */
    static void refreshConfigIfStale(Context c) {
        if (AlertConfigStore.isStale(c)) refreshConfig(c);
    }

    static synchronized Result flush(Context c) {
        String origin = HubStore.origin(c);
        if (origin == null) return new Result(false, HubStore.queueSize(c), "no-https-origin");
        String cookie = null;
        try {
            cookie = CookieManager.getInstance().getCookie(origin + PATH_CAPTURE);
        } catch (Throwable ignored) {
        }
        if (cookie == null || cookie.isEmpty()) {
            return new Result(false, HubStore.queueSize(c), "not-logged-in");
        }
        boolean any = false;
        while (true) {
            JSONObject item = HubStore.peek(c);
            if (item == null) break;
            String id = item.optString("_id");
            // Items queued by older app versions have no "_path": they are payments.
            String path = item.optString(HubStore.K_PATH, PATH_CAPTURE);
            item.remove("_id");
            item.remove(HubStore.K_PATH);
            if (!knownPath(path)) {
                HubStore.remove(c, id);
                continue;
            }
            int code = post(origin + path, cookie, item.toString());
            if (code >= 200 && code < 300) {
                HubStore.remove(c, id);
                any = true;
            } else if (code == 0 || code == 401 || code == 403 || code == 408 || code == 429 || code >= 500) {
                // Offline, logged out or hub busy: keep the item and stop; try again later.
                return new Result(any, HubStore.queueSize(c), code == 0 ? "network" : "http-" + code);
            } else {
                HubStore.remove(c, id); // the hub rejected this item for good (other 4xx): do not block the queue
            }
        }
        String alertErr = flushAlerts(c, origin, cookie);
        return new Result(any, HubStore.queueSize(c), alertErr);
    }

    /** GET a small JSON document (at most 64 KB); null on any failure or non-2xx status. */
    private static String get(String endpoint, String cookie) {
        if (!endpoint.startsWith("https://")) return null;
        HttpURLConnection conn = null;
        try {
            conn = (HttpsURLConnection) new URL(endpoint).openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(TIMEOUT_MS);
            conn.setReadTimeout(TIMEOUT_MS);
            conn.setInstanceFollowRedirects(false);
            conn.setRequestProperty("Accept", "application/json");
            conn.setRequestProperty("X-Hub", "1");
            conn.setRequestProperty("Cookie", cookie);
            int code = conn.getResponseCode();
            if (code < 200 || code >= 300) return null;
            java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
            try (java.io.InputStream in = conn.getInputStream()) {
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0) {
                    buf.write(b, 0, n);
                    if (buf.size() > 65536) return null;
                }
            }
            return new String(buf.toByteArray(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** Returns the HTTP status, or 0 for a network failure. */
    private static int post(String endpoint, String cookie, String json) {
        if (!endpoint.startsWith("https://")) return 0;
        HttpURLConnection conn = null;
        try {
            conn = (HttpsURLConnection) new URL(endpoint).openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(TIMEOUT_MS);
            conn.setReadTimeout(TIMEOUT_MS);
            conn.setInstanceFollowRedirects(false);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("X-Hub", "1");
            conn.setRequestProperty("Cookie", cookie);
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            return conn.getResponseCode();
        } catch (Exception e) {
            return 0;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }
}
