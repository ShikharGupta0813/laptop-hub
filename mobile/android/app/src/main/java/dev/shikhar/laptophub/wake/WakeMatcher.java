package dev.shikhar.laptophub.wake;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Pure decision logic: does a piece of recognised text start with the wake phrase?
 * No Android imports on purpose, so it runs in plain JVM unit tests.
 *
 * Rules (kept strict so ordinary talk does not trigger):
 *  - "hey/ok/okay/hi/hello" + a name ("jarvis", "friday") at the START of the utterance triggers, even on a
 *    partial result. Whatever follows the name is the "rest".
 *  - A bare name only triggers on a FINAL result that contains nothing else. "jarvis is a nice name" never does.
 *  - The name must be the first word (after at most one greeting). "I said hey jarvis" never triggers.
 */
public final class WakeMatcher {
    private WakeMatcher() {}

    public static final class Match {
        public final String persona; // "jarvis" or "friday"
        public final String rest;    // words after the wake phrase, lower case, "" when none

        Match(String persona, String rest) {
            this.persona = persona;
            this.rest = rest;
        }
    }

    private static final Pattern JSON_TEXT =
        Pattern.compile("\"(?:text|partial)\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");

    private static boolean isGreeting(String w) {
        return w.equals("hey") || w.equals("hay") || w.equals("hi") || w.equals("hello")
            || w.equals("ok") || w.equals("okay") || w.equals("yo");
    }

    /** Maps a (possibly misheard) name to a persona, or null. */
    static String personaOf(String w) {
        switch (w) {
            case "jarvis": case "jarves": case "jarvas": case "jarvus": case "jarviss": case "jervis":
                return "jarvis";
            case "friday": case "fryday":
                return "friday";
            default:
                return null;
        }
    }

    /** Pulls the "text" (final) or "partial" field out of a Vosk result without a JSON library. */
    public static String textOf(String voskJson) {
        if (voskJson == null) return "";
        Matcher m = JSON_TEXT.matcher(voskJson);
        if (!m.find()) return "";
        return m.group(1).replace("\\\"", "\"").replace("\\\\", "\\");
    }

    /** voskJson is the raw string from Recognizer.getResult() / getPartialResult(). */
    public static Match match(String voskJson, boolean isFinal) {
        return matchText(textOf(voskJson), isFinal);
    }

    public static Match matchText(String text, boolean isFinal) {
        if (text == null) return null;
        String norm = text.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9'\\s]", " ").trim();
        if (norm.isEmpty()) return null;
        String[] tok = norm.split("\\s+");
        int i = 0;
        boolean greeted = false;
        if (isGreeting(tok[0])) {
            greeted = true;
            i = 1;
        }
        if (i >= tok.length) return null;
        String persona = personaOf(tok[i]);
        if (persona == null) return null;
        if (!greeted && !(isFinal && tok.length == 1)) return null;
        List<String> rest = new ArrayList<>();
        for (int j = i + 1; j < tok.length; j++) {
            if (!tok[j].equals("unk")) rest.add(tok[j]);   // "[unk]" is the grammar's "something else"
        }
        return new Match(persona, String.join(" ", rest));
    }
}
