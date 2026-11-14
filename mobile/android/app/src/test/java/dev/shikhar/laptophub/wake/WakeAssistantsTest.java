package dev.shikhar.laptophub.wake;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import org.junit.Test;

/** Matcher, grammar builder and list storage for configurable assistants. Pure Java: no Android classes. */
public class WakeAssistantsTest {

    private static WakeMatcher.Assistant a(String id, String... names) {
        return new WakeMatcher.Assistant(id, new ArrayList<>(Arrays.asList(names)));
    }

    private static WakeMatcher.Table table(WakeMatcher.Assistant... list) {
        return WakeMatcher.table(new ArrayList<>(Arrays.asList(list)));
    }

    private static String who(WakeMatcher.Table t, String text, boolean fin) {
        WakeMatcher.Match m = WakeMatcher.matchText(t, text, fin);
        return m == null ? null : m.assistant;
    }

    // ---- matcher ----------------------------------------------------------------------------------
    @Test
    public void severalAssistantsAndACustomThirdName() {
        WakeMatcher.Table t = table(a("jarvis", "jarvis", "jarves"), a("friday", "friday"), a("atlas", "atlas", "atlus"));
        assertEquals("jarvis", who(t, "hey jarvis", false));
        assertEquals("friday", who(t, "okay friday", true));
        assertEquals("atlas", who(t, "hey atlas what is the capital of peru", false));
        assertEquals("what is the capital of peru", WakeMatcher.matchText(t, "hey atlas what is the capital of peru", false).rest);
        assertEquals("atlas", who(t, "hey atlus", false));      // a variant of the third name
        assertEquals("atlas", who(t, "atlas", true));            // bare name, final and alone
    }

    @Test
    public void renamedAssistantReplacesTheOldName() {
        WakeMatcher.Table t = table(a("jarvis", "nova"), a("friday", "friday"));
        assertEquals("jarvis", who(t, "hey nova", false));
        assertNull(who(t, "hey jarvis", false));                 // the old name no longer wakes anything
    }

    @Test
    public void falsePositiveSentences() {
        WakeMatcher.Table t = table(a("jarvis", "jarvis"), a("friday", "friday"), a("atlas", "atlas"));
        assertNull(who(t, "atlas is a nice name", true));
        assertNull(who(t, "atlas is a nice name", false));
        assertNull(who(t, "i said hey atlas", true));
        assertNull(who(t, "the atlas mountains", true));
        assertNull(who(t, "see you on friday", true));
        assertNull(who(t, "hey atlas is a book", true));         // greeting + name + everyday word reads as a sentence
        assertNull(who(t, "hey friday evening plans", true));
        assertNull(who(t, "hey atlas and jarvis", true));
        assertNull(who(t, "atlas [unk]", true));
        assertNull(who(t, "atlas", false));                      // bare name on a partial result
        assertNull(who(t, "hey", true));
    }

    @Test
    public void emptyOrBrokenListFallsBackToJarvisAndFriday() {
        assertEquals("jarvis", who(WakeMatcher.table(null), "hey jarvis", false));
        assertEquals("friday", who(WakeMatcher.table(new ArrayList<WakeMatcher.Assistant>()), "hey friday", false));
        // only unusable entries (bad id, empty names, greeting words): still the built-ins
        WakeMatcher.Table t = table(a("X!", "atlas"), a("noname"), a("greet", "hey", "ok", "okay"));
        assertEquals("jarvis", who(t, "hey jarvis", false));
        assertNull(who(t, "hey atlas", false));
        assertEquals(WakeMatcher.defaults().size(), 2);
    }

    @Test
    public void nameCollisionsFirstAssistantWins() {
        WakeMatcher.Table t = table(a("one", "sol"), a("two", "sol", "luna"));
        assertEquals("one", who(t, "hey sol", false));
        assertEquals("two", who(t, "hey luna", false));
    }

    @Test
    public void prefixesMatchWholeWordsOnly() {
        WakeMatcher.Table t = table(a("jarvis", "jar"), a("atlas", "atlas"));
        assertEquals("jarvis", who(t, "hey jar", false));
        assertNull(who(t, "hey jarvis", false));                 // "jar" must not match the start of "jarvis"
        assertNull(who(t, "hey atlast", false));
        WakeMatcher.Table t2 = table(a("aa", "jarvis"), a("bb", "jarvis prime"));
        WakeMatcher.Match m = WakeMatcher.matchText(t2, "hey jarvis prime open the door", false);
        assertEquals("bb", m.assistant);                          // the longest name wins
        assertEquals("open the door", m.rest);
        assertEquals("aa", who(t2, "hey jarvis open the door", false));
    }

    @Test
    public void longNamesAndHyphens() {
        WakeMatcher.Table t = table(a("study", "professor oak", "mary-jane"), a("tiny", "ab"));
        assertEquals("study", who(t, "hey professor oak what's due", false));
        assertEquals("study", who(t, "hey mary jane", false));   // a hyphen reads as a space
        assertEquals("study", who(t, "ok mary-jane", false));
        assertNull(who(t, "hey professor", false));
        StringBuilder longName = new StringBuilder();
        for (int i = 0; i < 60; i++) longName.append('x');
        WakeMatcher.Table t2 = table(a("big", longName.toString(), "one two three four five"), a("friday", "friday"));
        assertNull(who(t2, "hey " + longName, false));           // too long: never usable
        assertNull(who(t2, "hey one two three four five", false)); // more than four words: not usable
        assertEquals("friday", who(t2, "hey friday", false));
        assertEquals("tiny", who(t, "hey ab", false));
    }

    @Test
    public void namesAreCleanedAndCaseInsensitive() {
        WakeMatcher.Table t = table(a("atlas", "Atlas", "ATLAS’S"));
        assertEquals("atlas", who(t, "Hey, ATLAS!", false));
        assertEquals("atlas", who(t, "hey atlas's", false));
    }

    @Test
    public void voskJsonStillWorksWithATable() {
        WakeMatcher.Table t = table(a("atlas", "atlas"));
        WakeMatcher.Match m = WakeMatcher.match(t, "{ \"partial\" : \"hey atlas what time is it\" }", false);
        assertNotNull(m);
        assertEquals("atlas", m.assistant);
        assertEquals("what time is it", m.rest);
    }

    // ---- grammar ----------------------------------------------------------------------------------
    @Test
    public void grammarHasGreetingsForEveryName() {
        WakeGrammar.Result g = WakeGrammar.build(Arrays.asList(a("atlas", "atlas"), a("friday", "friday")), null);
        assertEquals("[\"hey atlas\",\"ok atlas\",\"okay atlas\",\"atlas\",\"hey friday\",\"ok friday\",\"okay friday\",\"friday\",\"[unk]\"]", g.json);
        assertEquals("", g.notice());
        assertFalse(g.usedDefault);
    }

    @Test
    public void grammarForTheBuiltInsContainsTheShippedPhrases() {
        WakeGrammar.Result g = WakeGrammar.build(null, null);
        assertTrue(g.json.contains("\"hey jarvis\""));
        assertTrue(g.json.contains("\"okay friday\""));
        assertTrue(g.json.endsWith("\"[unk]\"]"));
        assertTrue(g.json.startsWith("["));
    }

    @Test
    public void unknownWordsAreDroppedFromTheGrammar() {
        Set<String> vocab = new HashSet<>(Arrays.asList("atlas", "jarvis", "friday", "hey", "ok", "okay"));
        WakeGrammar.Result g = WakeGrammar.build(
            Arrays.asList(a("jarvis", "jarvis", "jarves"), a("atlas", "atlas", "zzyzx"), a("friday", "friday")), vocab);
        assertFalse(g.json.contains("jarves"));
        assertFalse(g.json.contains("zzyzx"));
        assertTrue(g.json.contains("\"hey atlas\""));
        assertEquals(Arrays.asList("jarves", "zzyzx"), g.dropped);
        assertEquals("", g.notice());                             // every assistant still has a name: no warning
    }

    @Test
    public void anAssistantWhoseNamesAreAllUnknownIsReported() {
        Set<String> vocab = new HashSet<>(Arrays.asList("jarvis", "friday"));
        WakeGrammar.Result g = WakeGrammar.build(Arrays.asList(a("jarvis", "jarvis"), a("zed", "xqzt")), vocab);
        assertEquals(Arrays.asList("zed"), g.lostAssistants);
        assertEquals(WakeGrammar.NOTICE_PARTIAL, g.notice());
        assertTrue(g.json.contains("\"hey jarvis\""));
        assertFalse(g.usedDefault);
    }

    @Test
    public void nothingUsableGivesTheDefaultGrammarAndANotice() {
        Set<String> vocab = new HashSet<>(Arrays.asList("hey"));
        WakeGrammar.Result g = WakeGrammar.build(Arrays.asList(a("zed", "xqzt")), vocab);
        assertTrue(g.usedDefault);
        assertEquals(WakeGrammar.DEFAULT_JSON, g.json);
        assertEquals(WakeGrammar.NOTICE_PARTIAL, g.notice());
    }

    @Test
    public void multiWordNamesNeedEveryWordInTheModel() {
        Set<String> vocab = new HashSet<>(Arrays.asList("professor", "oak", "jarvis"));
        WakeGrammar.Result ok = WakeGrammar.build(Arrays.asList(a("study", "professor oak")), vocab);
        assertTrue(ok.json.contains("\"hey professor oak\""));
        WakeGrammar.Result bad = WakeGrammar.build(Arrays.asList(a("study", "professor zork"), a("jarvis", "jarvis")), vocab);
        assertFalse(bad.json.contains("zork"));
    }

    @Test
    public void grammarSizeIsBounded() {
        List<WakeMatcher.Assistant> list = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            List<String> names = new ArrayList<>();
            for (int j = 0; j < 30; j++) names.add("name" + (char) ('a' + i) + (char) ('a' + j % 26) + (char) ('a' + j / 26));
            list.add(new WakeMatcher.Assistant("as" + i, names));
        }
        WakeGrammar.Result g = WakeGrammar.build(WakeAssistants.sanitize(list), null);
        int phrases = g.json.split("\",\"").length;
        assertTrue(phrases <= WakeGrammar.MAX_PHRASE_NAMES * 4 + 1);
    }

    @Test
    public void vocabLoadsFromWordsTxtOrIsNull() throws Exception {
        File dir = Files.createTempDirectory("wake-vocab").toFile();
        assertNull(WakeGrammar.loadVocab(dir));
        File graph = new File(dir, "graph");
        assertTrue(graph.mkdirs());
        Files.write(new File(graph, "words.txt").toPath(), "<eps> 0\natlas 11\njarvis 12\n".getBytes(StandardCharsets.UTF_8));
        Set<String> v = WakeGrammar.loadVocab(dir);
        assertNotNull(v);
        assertTrue(v.contains("atlas"));
        assertFalse(v.contains("12"));
        assertNull(WakeGrammar.loadVocab(null));
    }

    // ---- stored list ------------------------------------------------------------------------------
    @Test
    public void listRoundTripsAndIsCleaned() {
        List<WakeMatcher.Assistant> in = Arrays.asList(
            a("jarvis", "Jarvis", "jarves", "jarves"), a("Bad Id", "x"), a("atlas", "atlas;drop", "a:b,c"), a("jarvis", "dup"));
        String enc = WakeAssistants.encode(in);
        assertEquals("jarvis:jarvis,jarves;atlas:atlasdrop,abc", enc);
        List<WakeMatcher.Assistant> out = WakeAssistants.decode(enc);
        assertEquals(2, out.size());
        assertEquals("atlas", out.get(1).id);
        assertEquals(Arrays.asList("atlasdrop", "abc"), out.get(1).names);
    }

    @Test
    public void brokenStoredStringsGiveAnEmptyList() {
        assertTrue(WakeAssistants.decode(null).isEmpty());
        assertTrue(WakeAssistants.decode("").isEmpty());
        assertTrue(WakeAssistants.decode(";;:::").isEmpty());
        assertTrue(WakeAssistants.decode("jarvis").isEmpty());
        assertTrue(WakeAssistants.sanitize(null).isEmpty());
    }

    @Test
    public void listIsBounded() {
        List<WakeMatcher.Assistant> in = new ArrayList<>();
        for (int i = 0; i < 20; i++) in.add(a("as" + i, "name" + i));
        assertEquals(WakeAssistants.MAX_ASSISTANTS, WakeAssistants.sanitize(in).size());
    }

    // ---- launch ids -------------------------------------------------------------------------------
    @Test
    public void launchCarriesAnyValidAssistantId() {
        assertEquals("#/voice?assistant=atlas&text=hi", WakeLaunch.hash("atlas", "hi"));
        assertEquals("#/voice?assistant=study_buddy", WakeLaunch.hash("study_buddy", ""));
        assertEquals("#/voice?assistant=jarvis", WakeLaunch.hash("Bad Id&x=1", ""));
        assertEquals("jarvis", WakeLaunch.assistant(null));
    }
}
