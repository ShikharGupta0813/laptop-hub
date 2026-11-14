package dev.shikhar.laptophub.wake;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Pure: builds the restricted Vosk grammar (a JSON array of phrases) from the assistants' wake names.
 * For each name X: "hey X", "ok X", "okay X", "X", plus "[unk]" so everything else is "something else".
 * Words that are not in the speech model's vocabulary cannot be recognised (Vosk ignores such a phrase and logs it),
 * so when the vocabulary is known they are dropped here.
 */
public final class WakeGrammar {
    private WakeGrammar() {}

    public static final String NOTICE_PARTIAL = "some names are not in the speech model; accuracy lower";
    /** The grammar the app always shipped with: known to work. */
    public static final String DEFAULT_JSON =
        "[\"hey jarvis\",\"hey friday\",\"ok jarvis\",\"okay jarvis\",\"ok friday\",\"okay friday\",\"jarvis\",\"friday\",\"[unk]\"]";
    public static final int MAX_PHRASE_NAMES = 120;

    public static final class Result {
        public final String json;
        public final List<String> dropped;        // names left out because the model does not know a word of them
        public final List<String> lostAssistants; // assistant ids with no name left at all
        public final boolean usedDefault;         // nothing usable: the built-in grammar is used instead

        Result(String json, List<String> dropped, List<String> lostAssistants, boolean usedDefault) {
            this.json = json;
            this.dropped = dropped;
            this.lostAssistants = lostAssistants;
            this.usedDefault = usedDefault;
        }

        /** "" when everything is fine, otherwise a plain sentence for the Settings card. */
        public String notice() {
            return (usedDefault || !lostAssistants.isEmpty()) ? NOTICE_PARTIAL : "";
        }
    }

    /** vocab == null means the vocabulary is unknown: nothing is filtered. */
    public static Result build(List<WakeMatcher.Assistant> list, Set<String> vocab) {
        WakeMatcher.Table table = WakeMatcher.table(list);
        List<String> dropped = new ArrayList<>();
        List<String> kept = new ArrayList<>();
        Set<String> idsWithName = new HashSet<>();
        Set<String> allIds = new HashSet<>();
        for (WakeMatcher.Entry e : table.entries) {
            allIds.add(e.id);
            boolean known = true;
            if (vocab != null) {
                for (String w : e.words) {
                    if (!vocab.contains(w)) {
                        known = false;
                        break;
                    }
                }
            }
            if (!known) {
                dropped.add(e.phrase());
            } else if (kept.size() < MAX_PHRASE_NAMES) {
                kept.add(e.phrase());
                idsWithName.add(e.id);
            }
        }
        List<String> lost = new ArrayList<>();
        for (String id : allIds) {
            if (!idsWithName.contains(id)) lost.add(id);
        }
        Collections.sort(lost);
        if (kept.isEmpty()) return new Result(DEFAULT_JSON, dropped, lost, true);
        StringBuilder sb = new StringBuilder("[");
        for (String n : kept) {
            for (String p : new String[] {"hey " + n, "ok " + n, "okay " + n, n}) {
                sb.append('"').append(jsonEscape(p)).append("\",");
            }
        }
        sb.append("\"[unk]\"]");
        return new Result(sb.toString(), dropped, lost, false);
    }

    static String jsonEscape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    /**
     * The model's word list (one "word id" per line) from graph/words.txt or words.txt in the model folder, or null
     * when the model does not ship one (the caller then keeps every name and relies on the fallback in the service).
     */
    public static Set<String> loadVocab(File modelDir) {
        if (modelDir == null) return null;
        File f = new File(modelDir, "graph/words.txt");
        if (!f.isFile()) f = new File(modelDir, "words.txt");
        if (!f.isFile()) return null;
        Set<String> out = new HashSet<>();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(new FileInputStream(f), StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) {
                int sp = line.indexOf(' ');
                String w = (sp < 0 ? line : line.substring(0, sp)).trim();
                if (!w.isEmpty()) out.add(w);
            }
        } catch (IOException | RuntimeException e) {
            return null;
        }
        return out.isEmpty() ? null : out;
    }
}
