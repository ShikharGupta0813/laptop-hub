package dev.shikhar.laptophub.wake;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Pure decision logic: does a piece of recognised text start with the wake phrase of one of the owner's assistants?
 * No Android imports on purpose, so it runs in plain JVM unit tests.
 *
 * The names come from the owner's assistants list (Settings > Assistants on the hub): for each ENABLED assistant its
 * id and every word that wakes it (name, wake names, mis-hearings). Without a list the built-in Jarvis and Friday apply.
 *
 * Rules (kept strict so ordinary talk does not trigger; they mirror hub/static/js/wakephrase.js):
 *  - "hey/ok/okay/hi/hello/yo" + a name at the START of the utterance triggers, even on a partial result. Whatever
 *    follows the name is the "rest". A name followed by an everyday word ("friday evening", "atlas is a city")
 *    reads as a normal sentence and does not trigger.
 *  - A bare name only triggers on a FINAL result that contains nothing else. "jarvis is a nice name" never does.
 *  - The name must be the first word (after at most one greeting). "I said hey jarvis" never triggers.
 *  - Names match whole words only ("jarv" never matches "jarvis"); the longest name wins ("jarvis prime" over "jarvis").
 *  - If two assistants share a word, the first one in the list owns it.
 */
public final class WakeMatcher {
    private WakeMatcher() {}

    /** One assistant as the phone needs it: its id and the words (lower case) that wake it. */
    public static final class Assistant {
        public final String id;
        public final List<String> names;

        public Assistant(String id, List<String> names) {
            this.id = id == null ? "" : id;
            this.names = names == null ? new ArrayList<String>() : names;
        }
    }

    public static final class Match {
        public final String assistant; // the assistant id, e.g. "jarvis"
        public final String rest;      // words after the wake phrase, lower case, "" when none

        Match(String assistant, String rest) {
            this.assistant = assistant;
            this.rest = rest;
        }
    }

    /** A usable wake name: its words (one or more) and the assistant that owns it. */
    static final class Entry {
        final String[] words;
        final String id;

        Entry(String[] words, String id) {
            this.words = words;
            this.id = id;
        }

        String phrase() {
            return String.join(" ", words);
        }
    }

    /** The prepared list of names; build once with {@link #table}, reuse for every recognised sentence. */
    public static final class Table {
        final List<Entry> entries;

        Table(List<Entry> entries) {
            this.entries = entries;
        }

        public boolean isEmpty() {
            return entries.isEmpty();
        }

        public int size() {
            return entries.size();
        }
    }

    public static final int MAX_NAME_WORDS = 4;
    public static final int MAX_NAME_CHARS = 40;

    private static final Pattern JSON_TEXT =
        Pattern.compile("\"(?:text|partial)\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
    private static final Pattern ID_RE = Pattern.compile("^[a-z][a-z0-9_-]{1,23}$");

    static boolean isGreeting(String w) {
        return w.equals("hey") || w.equals("hay") || w.equals("hi") || w.equals("hello")
            || w.equals("ok") || w.equals("okay") || w.equals("yo");
    }

    /** After "hey <name>" one of these makes it an ordinary sentence ("hey friday evening plans"). Same list as the web. */
    static boolean isNotAName(String w) {
        switch (w) {
            case "at": case "is": case "was": case "we": case "i": case "the": case "morning": case "afternoon":
            case "evening": case "night": case "next": case "this": case "last": case "after": case "before":
            case "during": case "off": case "and":
                return true;
            default:
                return false;
        }
    }

    /** Lower case, only a-z 0-9 and apostrophes, one space between words; "" when nothing is left. */
    static String normalize(String text) {
        if (text == null) return "";
        return text.toLowerCase(Locale.ROOT).replace('’', '\'').replaceAll("[^a-z0-9'\\s]", " ")
            .replaceAll("\\s+", " ").trim();
    }

    public static boolean validId(String id) {
        return id != null && ID_RE.matcher(id).matches();
    }

    /** The built-in list (what the app did before assistants were configurable). */
    public static List<Assistant> defaults() {
        List<Assistant> out = new ArrayList<>();
        out.add(new Assistant("jarvis", Arrays.asList("jarvis", "jarves", "jarvas", "jarvus", "jarviss", "jervis")));
        out.add(new Assistant("friday", Arrays.asList("friday", "fryday")));
        return out;
    }

    /**
     * Builds the table. Names that cannot be used are skipped (empty, too long, a greeting or an everyday word on its
     * own, one that starts with a greeting, assistants with an invalid id). A word already owned by an earlier
     * assistant is skipped. null, or a list with no usable name, gives the built-in Jarvis and Friday.
     */
    public static Table table(List<Assistant> list) {
        Table t = build(list);
        if (t.entries.isEmpty()) t = build(defaults());
        return t;
    }

    private static Table build(List<Assistant> list) {
        Map<String, Entry> seen = new LinkedHashMap<>();
        if (list != null) {
            for (Assistant a : list) {
                if (a == null || !validId(a.id) || a.names == null) continue;
                for (String raw : a.names) {
                    String n = normalize(raw == null ? "" : raw.replace('-', ' '));
                    if (n.length() < 2 || n.length() > MAX_NAME_CHARS) continue;
                    String[] w = n.split(" ");
                    if (w.length > MAX_NAME_WORDS) continue;
                    if (isGreeting(w[0]) || (w.length == 1 && isNotAName(w[0])) || w[0].equals("unk")) continue;
                    if (!seen.containsKey(n)) seen.put(n, new Entry(w, a.id));
                }
            }
        }
        List<Entry> entries = new ArrayList<>(seen.values());
        // longest name first, so "jarvis prime" is tried before "jarvis"; the sort is stable, so list order wins otherwise
        Collections.sort(entries, new Comparator<Entry>() {
            @Override
            public int compare(Entry x, Entry y) {
                return Integer.compare(y.words.length, x.words.length);
            }
        });
        return new Table(entries);
    }

    /** Pulls the "text" (final) or "partial" field out of a Vosk result without a JSON library. */
    public static String textOf(String voskJson) {
        if (voskJson == null) return "";
        Matcher m = JSON_TEXT.matcher(voskJson);
        if (!m.find()) return "";
        return m.group(1).replace("\\\"", "\"").replace("\\\\", "\\");
    }

    /** voskJson is the raw string from Recognizer.getResult() / getPartialResult(). */
    public static Match match(Table table, String voskJson, boolean isFinal) {
        return matchText(table, textOf(voskJson), isFinal);
    }

    /** Same with the built-in Jarvis and Friday list. */
    public static Match match(String voskJson, boolean isFinal) {
        return match(table(null), voskJson, isFinal);
    }

    public static Match matchText(String text, boolean isFinal) {
        return matchText(table(null), text, isFinal);
    }

    public static Match matchText(Table table, String text, boolean isFinal) {
        if (text == null || table == null) return null;
        String norm = normalize(text);
        if (norm.isEmpty()) return null;
        String[] tok = norm.split(" ");
        int i = 0;
        boolean greeted = false;
        if (isGreeting(tok[0])) {
            greeted = true;
            i = 1;
        }
        if (i >= tok.length) return null;
        for (Entry e : table.entries) {
            int n = e.words.length;
            if (i + n > tok.length) continue;
            boolean same = true;
            for (int k = 0; k < n; k++) {
                if (!tok[i + k].equals(e.words[k])) {
                    same = false;
                    break;
                }
            }
            if (!same) continue;
            int end = i + n;
            if (!greeted && !(isFinal && tok.length == n)) return null;
            if (greeted && end < tok.length && isNotAName(tok[end])) return null;
            List<String> rest = new ArrayList<>();
            for (int j = end; j < tok.length; j++) {
                if (!tok[j].equals("unk")) rest.add(tok[j]);   // "[unk]" is the grammar's "something else"
            }
            return new Match(e.id, String.join(" ", rest));
        }
        return null;
    }
}
