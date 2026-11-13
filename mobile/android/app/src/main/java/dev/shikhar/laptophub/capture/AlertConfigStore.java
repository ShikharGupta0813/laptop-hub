package dev.shikhar.laptophub.capture;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * The hub's capture-config (GET /api/alerts/capture-config), cached in SharedPreferences, as an AlertPolicy.
 *
 * Expected shape (extra keys are ignored, missing keys fall back):
 *   { "mode": "allowlist" | "all-but-blocklist",
 *     "packages": [ { "package": "com.whatsapp", "label": "WhatsApp", "enabled": true }, ... ],
 *     "deny": ["com.example.vault"],          // extra deny entries (exact package or fragment)
 *     "names": ["Shikhar"] }                  // the owner's names, for the mention hint
 * Without a cache (hub never reached, or the endpoint 404s) AlertPolicy.defaults() is used.
 */
final class AlertConfigStore {
    private AlertConfigStore() {}

    static final String PATH_CONFIG = "/api/alerts/capture-config";
    static final long REFRESH_MS = 15L * 60L * 1000L;
    private static final String K_CONFIG = "alertConfig";
    private static final String K_FETCHED = "alertConfigAt";
    private static final String QUEUE_FILE = "alert-events.jsonl";

    private static volatile AlertPolicy cached;
    private static volatile EventQueue queue;

    private static SharedPreferences prefs(Context c) {
        return c.getApplicationContext().getSharedPreferences(HubStore.PREFS, Context.MODE_PRIVATE);
    }

    /** Parses the hub's JSON; null when it is not a usable object. */
    static AlertPolicy parse(String json) {
        if (json == null) return null;
        try {
            JSONObject o = new JSONObject(json);
            Map<String, Boolean> listed = new LinkedHashMap<>();
            Map<String, String> labels = new LinkedHashMap<>();
            JSONArray pk = o.optJSONArray("packages");
            if (pk != null) {
                for (int i = 0; i < pk.length(); i++) {
                    Object item = pk.opt(i);
                    if (item instanceof JSONObject) {
                        JSONObject e = (JSONObject) item;
                        String p = e.optString("package", "");
                        if (p.isEmpty()) continue;
                        listed.put(p, e.optBoolean("enabled", true));
                        String label = e.optString("label", "");
                        if (!label.isEmpty()) labels.put(p, label);
                    } else if (item instanceof String) {
                        listed.put((String) item, Boolean.TRUE);
                    }
                }
            }
            return new AlertPolicy(o.optString("mode", AlertPolicy.MODE_ALLOWLIST), listed, labels,
                    stringSet(o.optJSONArray("deny")), stringSet(o.optJSONArray("names")));
        } catch (Exception e) {
            return null;
        }
    }

    private static Set<String> stringSet(JSONArray a) {
        Set<String> out = new HashSet<>();
        if (a != null) {
            for (int i = 0; i < a.length(); i++) {
                String s = a.optString(i, "");
                if (!s.isEmpty()) out.add(s);
            }
        }
        return out;
    }

    /** The policy to apply now: the cached hub list, else the built-in defaults. Never null. */
    static AlertPolicy policy(Context c) {
        AlertPolicy p = cached;
        if (p != null) return p;
        synchronized (AlertConfigStore.class) {
            if (cached == null) {
                AlertPolicy parsed = parse(prefs(c).getString(K_CONFIG, null));
                cached = parsed != null ? parsed : AlertPolicy.defaults();
            }
            return cached;
        }
    }

    /** Stores a freshly fetched config. Returns false (and keeps the old cache) when it does not parse. */
    static boolean save(Context c, String json) {
        AlertPolicy p = parse(json);
        if (p == null) return false;
        prefs(c).edit().putString(K_CONFIG, json).putLong(K_FETCHED, System.currentTimeMillis()).apply();
        cached = p;
        return true;
    }

    static boolean isStale(Context c) {
        long at = prefs(c).getLong(K_FETCHED, 0L);
        return System.currentTimeMillis() - at >= REFRESH_MS;
    }

    /** Alert events waiting for the hub (own file, so they never push payments out of the payment queue). */
    static EventQueue queue(Context c) {
        EventQueue q = queue;
        if (q != null) return q;
        synchronized (AlertConfigStore.class) {
            if (queue == null) queue = new EventQueue(new File(c.getApplicationContext().getFilesDir(), QUEUE_FILE));
            return queue;
        }
    }
}
