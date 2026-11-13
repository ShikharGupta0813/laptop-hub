package dev.shikhar.laptophub.wake;

import java.io.UnsupportedEncodingException;
import java.net.URLEncoder;

/** Pure helpers that build what the app is told when the wake phrase is heard. */
public final class WakeLaunch {
    private WakeLaunch() {}

    public static final int MAX_TEXT = 300;

    public static String persona(String p) {
        return "friday".equals(p) ? "friday" : "jarvis";
    }

    /** Trims, collapses whitespace, drops control characters and caps the length (never splits a character). */
    public static String clean(String text) {
        if (text == null) return "";
        String t = text.replaceAll("[\\p{Cntrl}]", " ").replaceAll("\\s+", " ").trim();
        if (t.length() > MAX_TEXT) {
            int end = MAX_TEXT;
            if (Character.isHighSurrogate(t.charAt(end - 1))) end--;
            t = t.substring(0, end).trim();
        }
        return t;
    }

    /** Percent-encodes (spaces as %20, UTF-8). */
    public static String encode(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8").replace("+", "%20");
        } catch (UnsupportedEncodingException e) {
            throw new IllegalStateException(e);   // UTF-8 always exists
        }
    }

    /** e.g. "#/voice?persona=jarvis&text=what%20time%20is%20it" (no text key when there is no text). */
    public static String hash(String persona, String rest) {
        String t = clean(rest);
        StringBuilder sb = new StringBuilder("#/voice?persona=").append(persona(persona));
        if (!t.isEmpty()) sb.append("&text=").append(encode(t));
        return sb.toString();
    }
}
