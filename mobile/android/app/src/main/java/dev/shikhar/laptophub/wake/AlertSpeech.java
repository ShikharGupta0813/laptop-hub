package dev.shikhar.laptophub.wake;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

/**
 * Pure logic for urgent alerts heard with the screen off (no Android classes, so it runs in plain JVM tests):
 * reading GET /api/alerts/pending, what to say, whether to ring, and "already handled" bookkeeping.
 * Alert text is private: nothing here logs it, and it only ever lives in the objects it returns.
 */
final class AlertSpeech {
    private AlertSpeech() {}

    static final int SHORT_CAP = 300;   // urgency 2: one short sentence
    static final int FULL_CAP = 900;    // alarm: the whole text
    static final int MAX_ITEMS = 20;    // never read more than this per poll

    /** One pending alert. Missing fields get safe defaults; unknown fields are ignored. */
    static final class Item {
        final String id;
        final int urgency;       // 0-3
        final String speech;     // ready-to-speak text from the hub ("" when absent)
        final String title;
        final String kind;
        final String source;
        final String sender;
        final String preview;
        final boolean call;

        Item(String id, int urgency, String speech, String title, String kind, String source, String sender,
             String preview, boolean call) {
            this.id = id;
            this.urgency = urgency;
            this.speech = speech;
            this.title = title;
            this.kind = kind;
            this.source = source;
            this.sender = sender;
            this.preview = preview;
            this.call = call;
        }
    }

    // ---- decisions ---------------------------------------------------------------------------------------------
    /** Urgency 2 and 3 are spoken; everything else is left to the normal notification. */
    static boolean shouldSpeak(Item i) {
        return i != null && (i.urgency >= 2 || i.call);
    }

    /** Urgency 3 or a "call" reminder rings like an alarm; urgency 2 only speaks. */
    static boolean shouldRing(Item i) {
        return i != null && (i.urgency >= 3 || i.call);
    }

    /** What the phone says for a plain (urgency 2) alert: about one sentence. "" when there is nothing to say. */
    static String shortText(Item i) {
        return cap(raw(i), SHORT_CAP);
    }

    /** What the phone says after the first ring: the whole text. */
    static String fullText(Item i) {
        return cap(raw(i), FULL_CAP);
    }

    private static String raw(Item i) {
        if (i == null) return "";
        String s = WakeReply.speakable(i.speech);
        if (!s.isEmpty()) return s;
        String who = !i.sender.isEmpty() ? i.sender : !i.source.isEmpty() ? i.source : i.title;
        String what = !i.preview.isEmpty() ? i.preview : i.title;
        String body = WakeReply.speakable(what);
        String name = WakeReply.speakable(who);
        if (body.isEmpty()) return name;
        return name.isEmpty() || name.equals(body) ? body : name + ": " + body;
    }

    /** At most n characters, cut at a sentence end if one is late enough, else at a space. */
    static String cap(String s, int n) {
        if (s == null) return "";
        String t = s.trim();
        if (t.length() <= n) return t;
        String cut = t.substring(0, n);
        int end = -1;
        for (int k = cut.length() - 1; k > n / 3; k--) {
            char c = cut.charAt(k);
            if ((c == '.' || c == '!' || c == '?') && (k + 1 == cut.length() || cut.charAt(k + 1) == ' ')) {
                end = k;
                break;
            }
        }
        if (end > 0) return cut.substring(0, end + 1).trim();
        int sp = cut.lastIndexOf(' ');
        return (sp > 0 ? cut.substring(0, sp) : cut).trim();
    }

    /** The body of POST /api/alerts/pending/ack. Ids are JSON-escaped. */
    static String ackBody(List<String> ids) {
        StringBuilder b = new StringBuilder("{\"ids\":[");
        boolean first = true;
        for (String id : ids) {
            if (id == null || id.isEmpty()) continue;
            if (!first) b.append(',');
            first = false;
            b.append('"');
            for (int k = 0; k < id.length(); k++) {
                char c = id.charAt(k);
                if (c == '"' || c == '\\') b.append('\\').append(c);
                else if (c < 0x20) b.append(' ');
                else b.append(c);
            }
            b.append('"');
        }
        return b.append("]}").toString();
    }

    // ---- already handled in this service run -------------------------------------------------------------------
    /** Ids already spoken or rung, so a failed ack never makes the phone repeat itself. Bounded. */
    static final class Seen {
        private final LinkedHashSet<String> ids = new LinkedHashSet<>();
        private final int max;

        Seen(int max) {
            this.max = max;
        }

        boolean has(String id) {
            return ids.contains(id);
        }

        /** Remembers the id; false when it was already there. */
        boolean add(String id) {
            if (!ids.add(id)) return false;
            while (ids.size() > max) {
                ids.remove(ids.iterator().next());
            }
            return true;
        }
    }

    // ---- reading the hub's answers -----------------------------------------------------------------------------
    /** {"alerts":[...]} (or a bare list) to items. Broken or unexpected JSON gives an empty list. */
    static List<Item> parse(String json) {
        List<Item> out = new ArrayList<>();
        Object root = Json.read(json);
        Object list = root;
        if (root instanceof Map) list = ((Map<?, ?>) root).get("alerts");
        if (!(list instanceof List)) return out;
        for (Object o : (List<?>) list) {
            if (out.size() >= MAX_ITEMS) break;
            if (!(o instanceof Map)) continue;
            Map<?, ?> m = (Map<?, ?>) o;
            String id = str(m.get("id"));
            if (id.isEmpty()) continue;       // no id: cannot be acked, so it would repeat forever
            out.add(new Item(id, urgency(m.get("urgency")), str(m.get("speech")), str(m.get("title")),
                str(m.get("kind")), str(m.get("source")), str(m.get("sender")), str(m.get("preview")),
                Boolean.TRUE.equals(m.get("call"))));
        }
        return out;
    }

    /** "spoken_alerts" of GET /api/friday/settings; true (the hub default) when absent or unreadable. */
    static boolean parseSpokenAlerts(String json) {
        Object root = Json.read(json);
        if (root instanceof Map) {
            Object v = ((Map<?, ?>) root).get("spoken_alerts");
            if (v instanceof Boolean) return (Boolean) v;
        }
        return true;
    }

    private static String str(Object o) {
        return o instanceof String ? (String) o : "";
    }

    private static int urgency(Object o) {
        if (!(o instanceof Double)) return 0;
        int u = (int) Math.round((Double) o);
        return Math.max(0, Math.min(3, u));
    }

    // ---- tiny JSON reader: objects, arrays, strings, numbers, booleans, null; null on any error ------------------
    static final class Json {
        private final String s;
        private int p = 0;

        private Json(String s) {
            this.s = s;
        }

        static Object read(String text) {
            if (text == null) return null;
            try {
                Json j = new Json(text);
                j.ws();
                Object v = j.value(0);
                return v;
            } catch (RuntimeException e) {
                return null;
            }
        }

        private void ws() {
            while (p < s.length() && Character.isWhitespace(s.charAt(p))) p++;
        }

        private Object value(int depth) {
            if (depth > 12 || p >= s.length()) throw new IllegalStateException();
            char c = s.charAt(p);
            if (c == '{') {
                p++;
                Map<String, Object> m = new HashMap<>();
                ws();
                if (peek() == '}') {
                    p++;
                    return m;
                }
                while (true) {
                    ws();
                    if (peek() != '"') throw new IllegalStateException();
                    String k = string();
                    ws();
                    expect(':');
                    ws();
                    m.put(k, value(depth + 1));
                    ws();
                    char d = next();
                    if (d == '}') return m;
                    if (d != ',') throw new IllegalStateException();
                }
            }
            if (c == '[') {
                p++;
                List<Object> l = new ArrayList<>();
                ws();
                if (peek() == ']') {
                    p++;
                    return l;
                }
                while (true) {
                    ws();
                    l.add(value(depth + 1));
                    ws();
                    char d = next();
                    if (d == ']') return l;
                    if (d != ',') throw new IllegalStateException();
                }
            }
            if (c == '"') return string();
            if (s.startsWith("true", p)) {
                p += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", p)) {
                p += 5;
                return Boolean.FALSE;
            }
            if (s.startsWith("null", p)) {
                p += 4;
                return null;
            }
            int st = p;
            while (p < s.length() && "+-0123456789.eE".indexOf(s.charAt(p)) >= 0) p++;
            if (st == p) throw new IllegalStateException();
            return Double.valueOf(s.substring(st, p));
        }

        private char peek() {
            if (p >= s.length()) throw new IllegalStateException();
            return s.charAt(p);
        }

        private char next() {
            char c = peek();
            p++;
            return c;
        }

        private void expect(char c) {
            if (next() != c) throw new IllegalStateException();
        }

        private String string() {
            expect('"');
            StringBuilder b = new StringBuilder();
            while (true) {
                char c = next();
                if (c == '"') return b.toString();
                if (c != '\\') {
                    b.append(c);
                    continue;
                }
                char e = next();
                switch (e) {
                    case 'n': b.append('\n'); break;
                    case 't': b.append('\t'); break;
                    case 'r': b.append('\r'); break;
                    case 'b': b.append('\b'); break;
                    case 'f': b.append('\f'); break;
                    case 'u':
                        if (p + 4 > s.length()) throw new IllegalStateException();
                        b.append((char) Integer.parseInt(s.substring(p, p + 4), 16));
                        p += 4;
                        break;
                    default: b.append(e);
                }
            }
        }
    }
}
