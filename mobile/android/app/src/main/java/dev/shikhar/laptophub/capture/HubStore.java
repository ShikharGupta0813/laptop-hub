package dev.shikhar.laptophub.capture;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Small persisted state: hub origin, on/off switch and the unsent queue. All data stays on the phone. */
public final class HubStore {
    private HubStore() {}

    static final String PREFS = "hubcapture";
    static final int QUEUE_CAP = 200;
    private static final String K_ORIGIN = "origin";
    private static final String K_ENABLED = "enabled";
    private static final String K_QUEUE = "queue";
    private static final String K_WATCH_MAIL = "watchMail";
    private static final String K_WATCH_CHAT = "watchChat";
    /** Queue entries without a "_path" are payment items queued by an older version of the app. */
    static final String K_PATH = "_path";

    private static SharedPreferences prefs(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** "https://host[:port]" or null for anything that is not a plain https origin. */
    static String toOrigin(String url) {
        if (url == null) return null;
        try {
            URL u = new URL(url.trim());
            if (!"https".equalsIgnoreCase(u.getProtocol())) return null;
            String host = u.getHost();
            if (host == null || host.isEmpty()) return null;
            return "https://" + host.toLowerCase() + (u.getPort() == -1 ? "" : ":" + u.getPort());
        } catch (Exception e) {
            return null;
        }
    }

    static void saveOrigin(Context c, String url) {
        String o = toOrigin(url);
        if (o != null) prefs(c).edit().putString(K_ORIGIN, o).apply();
    }

    /** The hub origin saved at plugin load, else the one baked into assets/capacitor.config.json at sync time. */
    public static String origin(Context c) {
        String o = prefs(c).getString(K_ORIGIN, null);
        if (o != null) return o;
        try (InputStream in = c.getAssets().open("capacitor.config.json")) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[4096];
            int n;
            while ((n = in.read(b)) > 0) buf.write(b, 0, n);
            JSONObject cfg = new JSONObject(new String(buf.toByteArray(), StandardCharsets.UTF_8));
            JSONObject server = cfg.optJSONObject("server");
            o = toOrigin(server == null ? null : server.optString("url", null));
            if (o != null) prefs(c).edit().putString(K_ORIGIN, o).apply();
            return o;
        } catch (Exception e) {
            return null;
        }
    }

    static boolean enabled(Context c) {
        return prefs(c).getBoolean(K_ENABLED, true);
    }

    static void setEnabled(Context c, boolean on) {
        prefs(c).edit().putBoolean(K_ENABLED, on).apply();
    }

    /** Mail / chat watching is OFF until the hub page turns it on (independent of payment capture). */
    static boolean watchMail(Context c) {
        return prefs(c).getBoolean(K_WATCH_MAIL, false);
    }

    static boolean watchChat(Context c) {
        return prefs(c).getBoolean(K_WATCH_CHAT, false);
    }

    static void setWatchMail(Context c, boolean on) {
        prefs(c).edit().putBoolean(K_WATCH_MAIL, on).apply();
    }

    static void setWatchChat(Context c, boolean on) {
        prefs(c).edit().putBoolean(K_WATCH_CHAT, on).apply();
    }

    // ---- queue (JSON array of payload objects, each with a private "_id" and "_path") ----

    private static JSONArray readQueue(Context c) {
        try {
            return new JSONArray(prefs(c).getString(K_QUEUE, "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }

    private static void writeQueue(Context c, JSONArray q) {
        prefs(c).edit().putString(K_QUEUE, q.toString()).apply();
    }

    /** Adds an item that will be POSTed to {@code path} (e.g. Delivery.PATH_CAPTURE). */
    static synchronized void enqueue(Context c, String path, JSONObject payload) {
        try {
            JSONObject item = new JSONObject(payload.toString());
            item.put("_id", System.nanoTime() + "-" + Math.abs(payload.toString().hashCode()));
            item.put(K_PATH, path);
            JSONArray q = readQueue(c);
            q.put(item);
            // Over the cap: drop the oldest watch item first so a chatty inbox never pushes out payments.
            while (q.length() > QUEUE_CAP) {
                int drop = 0;
                for (int i = 0; i < q.length(); i++) {
                    JSONObject o = q.optJSONObject(i);
                    if (o != null && Delivery.PATH_WATCH.equals(o.optString(K_PATH))) {
                        drop = i;
                        break;
                    }
                }
                q.remove(drop);
            }
            writeQueue(c, q);
        } catch (JSONException ignored) {
        }
    }

    static synchronized int queueSize(Context c) {
        return readQueue(c).length();
    }

    /** The oldest queued item (with its "_id"), or null. */
    static synchronized JSONObject peek(Context c) {
        JSONArray q = readQueue(c);
        return q.length() == 0 ? null : q.optJSONObject(0);
    }

    static synchronized void remove(Context c, String id) {
        JSONArray q = readQueue(c);
        JSONArray rest = new JSONArray();
        for (int i = 0; i < q.length(); i++) {
            JSONObject o = q.optJSONObject(i);
            if (o != null && !id.equals(o.optString("_id"))) rest.put(o);
        }
        writeQueue(c, rest);
    }
}
