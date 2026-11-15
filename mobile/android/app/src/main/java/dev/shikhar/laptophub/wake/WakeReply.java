package dev.shikhar.laptophub.wake;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Pure logic for the spoken reply (no Android classes, so it runs in plain JVM tests): reading the hub's
 * Server-Sent Events, turning an answer into something nice to say out loud, and spotting "open the app".
 * It carries no user text anywhere except in the objects it returns; nothing is logged.
 */
final class WakeReply {
    private WakeReply() {}

    static final int MAX_SPOKEN = 900;

    /** One event from the hub stream. type is "text", "tool_start", "error", "done" or anything else the hub sends. */
    static final class Event {
        final String type;
        final String text;            // "t" of a text event
        final String message;         // "message" of an error event
        final boolean needsApproval;  // tool_start (or approval_needed) that waits for a tap

        Event(String type, String text, String message, boolean needsApproval) {
            this.type = type;
            this.text = text;
            this.message = message;
            this.needsApproval = needsApproval;
        }
    }

    /** Incremental parser: feed it chunks as they arrive (they may split a line anywhere). */
    static final class SseParser {
        private final StringBuilder pending = new StringBuilder();

        List<Event> feed(String chunk) {
            List<Event> out = new ArrayList<>();
            if (chunk == null || chunk.isEmpty()) return out;
            pending.append(chunk);
            int start = 0;
            for (int i = 0; i < pending.length(); i++) {
                if (pending.charAt(i) == '\n') {
                    line(pending.substring(start, i), out);
                    start = i + 1;
                }
            }
            pending.delete(0, start);
            return out;
        }

        /** Call at the end of the stream to read a last line that had no newline. */
        List<Event> finish() {
            List<Event> out = new ArrayList<>();
            if (pending.length() > 0) line(pending.toString(), out);
            pending.setLength(0);
            return out;
        }

        private static void line(String raw, List<Event> out) {
            String l = raw.endsWith("\r") ? raw.substring(0, raw.length() - 1) : raw;
            if (!l.startsWith("data:")) return;
            String json = l.substring(5).trim();
            if (json.isEmpty() || json.charAt(0) != '{') return;
            Map<String, Object> m = flatObject(json);
            if (m == null) return;
            Object type = m.get("type");
            if (!(type instanceof String)) return;
            String t = (String) type;
            Object tx = m.get("t");
            Object msg = m.get("message");
            boolean needs = Boolean.TRUE.equals(m.get("needs_approval")) || "approval_needed".equals(t);
            out.add(new Event(t, tx instanceof String ? (String) tx : "", msg instanceof String ? (String) msg : "", needs));
        }
    }

    static SseParser parseSse() {
        return new SseParser();
    }

    /** Parses all complete lines of {@code data} in one go (convenience for tests and one-shot use). */
    static List<Event> parseSse(String data) {
        SseParser p = new SseParser();
        List<Event> out = p.feed(data);
        out.addAll(p.finish());
        return out;
    }

    // ---- tiny JSON reader: the top-level strings and booleans of one object; nested values are skipped -------------
    private static Map<String, Object> flatObject(String s) {
        Map<String, Object> m = new HashMap<>();
        int[] pos = {0};
        int n = s.length();
        skipWs(s, pos);
        if (pos[0] >= n || s.charAt(pos[0]) != '{') return null;
        pos[0]++;
        while (true) {
            skipWs(s, pos);
            if (pos[0] >= n) return null;
            char c = s.charAt(pos[0]);
            if (c == '}') return m;
            if (c == ',') {
                pos[0]++;
                continue;
            }
            if (c != '"') return null;
            String key = readString(s, pos);
            if (key == null) return null;
            skipWs(s, pos);
            if (pos[0] >= n || s.charAt(pos[0]) != ':') return null;
            pos[0]++;
            skipWs(s, pos);
            if (pos[0] >= n) return null;
            char v = s.charAt(pos[0]);
            if (v == '"') {
                String val = readString(s, pos);
                if (val == null) return null;
                m.put(key, val);
            } else if (s.startsWith("true", pos[0])) {
                m.put(key, Boolean.TRUE);
                pos[0] += 4;
            } else if (s.startsWith("false", pos[0])) {
                m.put(key, Boolean.FALSE);
                pos[0] += 5;
            } else if (v == '{' || v == '[') {
                if (!skipNested(s, pos)) return null;
            } else {   // number or null
                while (pos[0] < n && ",}".indexOf(s.charAt(pos[0])) < 0) pos[0]++;
            }
        }
    }

    private static void skipWs(String s, int[] pos) {
        while (pos[0] < s.length() && Character.isWhitespace(s.charAt(pos[0]))) pos[0]++;
    }

    /** pos is at the opening quote; returns the decoded string and leaves pos after the closing quote (null if broken). */
    private static String readString(String s, int[] pos) {
        StringBuilder b = new StringBuilder();
        int i = pos[0] + 1;
        int n = s.length();
        while (i < n) {
            char c = s.charAt(i++);
            if (c == '"') {
                pos[0] = i;
                return b.toString();
            }
            if (c != '\\') {
                b.append(c);
                continue;
            }
            if (i >= n) return null;
            char e = s.charAt(i++);
            switch (e) {
                case 'n': b.append('\n'); break;
                case 't': b.append('\t'); break;
                case 'r': b.append('\r'); break;
                case 'b': b.append('\b'); break;
                case 'f': b.append('\f'); break;
                case 'u':
                    if (i + 4 > n) return null;
                    try {
                        b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                    } catch (NumberFormatException ex) {
                        return null;
                    }
                    i += 4;
                    break;
                default: b.append(e);   // \" \\ \/
            }
        }
        return null;
    }

    private static boolean skipNested(String s, int[] pos) {
        int depth = 0;
        int i = pos[0];
        int n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (c == '"') {
                int[] p = {i};
                if (readString(s, p) == null) return false;
                i = p[0];
                continue;
            }
            if (c == '{' || c == '[') depth++;
            else if (c == '}' || c == ']') {
                depth--;
                if (depth == 0) {
                    pos[0] = i + 1;
                    return true;
                }
            }
            i++;
        }
        return false;
    }

    // ---- speech text ------------------------------------------------------------------------------------------
    private static final Pattern LINK = Pattern.compile("!?\\[([^\\]]*)\\]\\([^)]*\\)");
    private static final Pattern BARE_URL = Pattern.compile("https?://\\S+");
    private static final Pattern FENCE = Pattern.compile("```");
    private static final Pattern LINE_MARK = Pattern.compile("(?m)^[ \\t]*(?:#{1,6}[ \\t]+|>[ \\t]?|[-*+][ \\t]+|\\d+[.)][ \\t]+)");
    private static final Pattern EMPHASIS = Pattern.compile("[*_~`]+");
    private static final Pattern SPACES = Pattern.compile("\\s+");

    /** Markdown to plain speech: no symbols, one line, at most about 900 characters, cut at a sentence end. "" if nothing. */
    static String speakable(String answer) {
        if (answer == null) return "";
        String s = answer.replace("\r", "");
        s = LINK.matcher(s).replaceAll("$1");
        s = BARE_URL.matcher(s).replaceAll("");
        s = FENCE.matcher(s).replaceAll("");
        s = LINE_MARK.matcher(s).replaceAll("");
        s = EMPHASIS.matcher(s).replaceAll("");
        s = s.replace('|', ' ').replace('#', ' ');
        s = SPACES.matcher(s).replaceAll(" ").trim();
        if (s.length() <= MAX_SPOKEN) return s;
        String cut = s.substring(0, MAX_SPOKEN);
        int end = -1;
        for (int i = cut.length() - 1; i > 0; i--) {
            char c = cut.charAt(i);
            if ((c == '.' || c == '!' || c == '?') && (i + 1 == cut.length() || cut.charAt(i + 1) == ' ')) {
                end = i;
                break;
            }
        }
        if (end > MAX_SPOKEN / 3) return cut.substring(0, end + 1).trim();
        int sp = cut.lastIndexOf(' ');
        return (sp > 0 ? cut.substring(0, sp) : cut).trim();
    }

    private static final Pattern OPEN_APP = Pattern.compile(
        "(?:please\\s+)?(?:open|launch|start)\\s+(?:the\\s+)?(?:red\\s?line(?:\\s+app)?|app)");

    /** True for "open redline", "open the red line app", "launch redline", "open the app" (any case, trailing punctuation). */
    static boolean isOpenApp(String sentence) {
        if (sentence == null) return false;
        String s = sentence.toLowerCase(Locale.ROOT).replaceAll("[\\p{Punct}\\s]+$", "").replaceAll("\\s+", " ").trim();
        return OPEN_APP.matcher(s).matches();
    }
}
