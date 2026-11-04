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
 * Sends captured items to the hub, using the login cookie the WebView already holds.
 * Anything that cannot be delivered stays in the persisted queue and is retried on the next
 * capture, on app resume and on flush(). Nothing here logs notification text.
 */
final class Delivery {
    private Delivery() {}

    static final String PATH = "/api/expenses/capture";
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

    /** Queue the item, then try to send everything. Blocking: call from EXEC or a worker thread. */
    static Result enqueueAndFlush(Context c, JSONObject payload) {
        HubStore.enqueue(c, payload);
        return flush(c);
    }

    static synchronized Result flush(Context c) {
        String origin = HubStore.origin(c);
        if (origin == null) return new Result(false, HubStore.queueSize(c), "no-https-origin");
        String endpoint = origin + PATH;
        String cookie = null;
        try {
            cookie = CookieManager.getInstance().getCookie(endpoint);
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
            item.remove("_id");
            int code = post(endpoint, cookie, item.toString());
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
        return new Result(any, HubStore.queueSize(c), null);
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
