package dev.shikhar.laptophub.capture;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.io.IOException;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;

import org.junit.Test;

/** JVM tests for the pure alert-capture logic: AlertPolicy, AlertFilter (noise, dedupe, mention) and EventQueue. */
public class AlertLogicTest {

    private static final String OWN = "dev.shikhar.laptophub";

    // ---- AlertPolicy ----------------------------------------------------------------------------
    @Test
    public void defaultsForwardWorkAppsAndIgnoreTheRest() {
        AlertPolicy p = AlertPolicy.defaults();
        for (String pkg : new String[] {"com.whatsapp", "com.Slack", "com.microsoft.teams", "com.asana.app",
                "com.google.android.gm", "com.google.android.apps.messaging", "org.telegram.messenger"}) {
            assertTrue(pkg, p.allows(pkg, OWN));
        }
        assertFalse(p.allows("com.example.game", OWN));
        assertFalse(p.allows(null, OWN));
        assertFalse(p.allows("", OWN));
    }

    @Test
    public void allowlistUsesOnlyEnabledPackagesCaseInsensitively() {
        Map<String, Boolean> m = new HashMap<>();
        m.put("com.Slack", true);
        m.put("com.whatsapp", false);
        AlertPolicy p = new AlertPolicy("allowlist", m, null, null, null);
        assertTrue(p.allows("com.slack", OWN));
        assertFalse(p.allows("com.whatsapp", OWN));
        assertFalse(p.allows("com.microsoft.teams", OWN));
    }

    @Test
    public void allButBlocklistForwardsEverythingExceptDisabled() {
        Map<String, Boolean> m = new HashMap<>();
        m.put("com.example.noisy", false);
        AlertPolicy p = new AlertPolicy("all-but-blocklist", m, null, null, null);
        assertTrue(p.allows("com.anything.else", OWN));
        assertFalse(p.allows("com.example.noisy", OWN));
        assertEquals("all-but-blocklist", p.mode());
        assertEquals("allowlist", new AlertPolicy("bogus", null, null, null, null).mode());
    }

    @Test
    public void denyListAlwaysWins() {
        Map<String, Boolean> m = new HashMap<>();
        m.put("com.x8bit.bitwarden", true);
        m.put("com.mybank.app", true);
        m.put("com.example.vault", true);
        AlertPolicy p = new AlertPolicy("allowlist", m, null, new HashSet<>(Arrays.asList("example.vault")), null);
        assertFalse(p.allows("com.x8bit.bitwarden", OWN));
        assertFalse(p.allows("com.mybank.app", OWN));
        assertFalse(p.allows("com.example.vault", OWN));
        AlertPolicy all = new AlertPolicy("all-but-blocklist", null, null, null, null);
        assertFalse(all.allows("com.google.android.apps.authenticator2", OWN));
        assertFalse(all.allows("com.snapwork.hdfc", OWN));
    }

    @Test
    public void ownAppAndPaymentAppsAreNeverForwardedHere() {
        AlertPolicy all = new AlertPolicy("all-but-blocklist", null, null, null, null);
        assertFalse(all.allows(OWN, OWN));
        assertFalse(all.allows("com.phonepe.app", OWN));   // spend-only path
        Map<String, Boolean> m = new HashMap<>();
        m.put(OWN, true);
        assertFalse(new AlertPolicy("allowlist", m, null, null, null).allows(OWN, OWN));
    }

    @Test
    public void labelsComeFromTheHub() {
        Map<String, String> labels = new HashMap<>();
        labels.put("com.whatsapp", "WhatsApp");
        AlertPolicy p = new AlertPolicy("allowlist", null, labels, null, null);
        assertEquals("WhatsApp", p.label("COM.WHATSAPP"));
        assertEquals("", p.label("com.other"));
    }

    // ---- AlertFilter: noise ---------------------------------------------------------------------
    @Test
    public void groupSummaryOngoingForegroundMediaAndProgressAreDropped() {
        assertTrue(AlertFilter.isNoise(AlertFilter.FLAG_GROUP_SUMMARY, null, false));
        assertTrue(AlertFilter.isNoise(AlertFilter.FLAG_ONGOING_EVENT, "msg", false));
        assertTrue(AlertFilter.isNoise(AlertFilter.FLAG_FOREGROUND_SERVICE, null, false));
        assertTrue(AlertFilter.isNoise(0, null, true));
        assertTrue(AlertFilter.isNoise(0, "transport", false));
        assertTrue(AlertFilter.isNoise(0, "PROGRESS", false));
        assertFalse(AlertFilter.isNoise(0, "msg", false));
        assertFalse(AlertFilter.isNoise(0, null, false));
        assertFalse(AlertFilter.isNoise(0x10, "email", false));   // an unrelated flag
    }

    @Test
    public void bodyPrefersTheLongerAndCapTrims() {
        assertEquals("the full message", AlertFilter.body("the full", "the full message"));
        assertEquals("short", AlertFilter.body("short", ""));
        assertEquals("", AlertFilter.body(null, null));
        assertEquals("abc", AlertFilter.cap("  abcdef ", 3));
        assertEquals("", AlertFilter.cap(null, 3));
    }

    // ---- AlertFilter: dedupe --------------------------------------------------------------------
    @Test
    public void repeatedUpdateOfSameKeyAndTextIsForwardedOnce() {
        AlertFilter.Dedupe d = new AlertFilter.Dedupe();
        assertTrue(d.firstSeen("k1", "Alice", "hello  there", 1000L));
        assertFalse(d.firstSeen("k1", "alice", "Hello there", 5000L));
        assertTrue(d.firstSeen("k1", "Alice", "a new message", 6000L));   // same key, new text
        assertTrue(d.firstSeen("k2", "Alice", "hello there", 6000L));     // other key
        assertTrue(d.firstSeen("k1", "Alice", "hello there", 1000L + AlertFilter.DEDUPE_WINDOW_MS + 1));
    }

    @Test
    public void dedupeTableIsBounded() {
        AlertFilter.Dedupe d = new AlertFilter.Dedupe(3, 1000000L);
        for (int i = 0; i < 10; i++) d.firstSeen("k" + i, "t", "x", 1000L);
        assertEquals(3, d.size());
        assertTrue(d.firstSeen("k0", "t", "x", 1001L));   // evicted long ago: counts as new
    }

    // ---- AlertFilter: mention hint --------------------------------------------------------------
    @Test
    public void mentionHint() {
        HashSet<String> names = new HashSet<>(Arrays.asList("Shikhar"));
        assertEquals("true", AlertFilter.mention(true, "Team", "@shikhar can you check?", names));
        assertEquals("true", AlertFilter.mention(true, "Team", "Shikhar, ETA?", names));
        assertEquals("false", AlertFilter.mention(true, "Team", "lunch anyone?", names));
        assertEquals("false", AlertFilter.mention(true, "Team", "Shikharan said hi", names));   // whole word only
        assertEquals("unknown", AlertFilter.mention(true, "Team", "hello", new HashSet<String>()));
        assertEquals("unknown", AlertFilter.mention(true, "Team", "hello", null));
        assertEquals("false", AlertFilter.mention(false, "Alice", "hello", names));
        assertEquals("true", AlertFilter.mention(true, "Bob mentioned you in #dev", "", null));
        assertEquals("true", AlertFilter.mention(false, "Slack", "Bob mentioned you", null));
    }

    @Test
    public void groupDetection() {
        assertTrue(AlertFilter.isGroup(Boolean.TRUE, ""));
        assertFalse(AlertFilter.isGroup(Boolean.FALSE, "Some title"));
        assertTrue(AlertFilter.isGroup(null, "Project chat"));
        assertFalse(AlertFilter.isGroup(null, " "));
        assertFalse(AlertFilter.isGroup(null, null));
    }

    // ---- EventQueue -----------------------------------------------------------------------------
    private static File tempFile() throws IOException {
        File f = File.createTempFile("alertq", ".jsonl");
        f.delete();
        f.deleteOnExit();
        return f;
    }

    @Test
    public void queueIsFifoAndPersistsInTheFile() throws IOException {
        File f = tempFile();
        EventQueue q = new EventQueue(f);
        assertNull(q.peek());
        q.add("{\"a\":1}");
        q.add("{\"a\":2}");
        assertEquals(2, q.size());
        assertEquals("{\"a\":1}", new EventQueue(f).peek());   // a fresh instance sees the same file
        q.removeHead();
        assertEquals("{\"a\":2}", q.peek());
        q.removeHead();
        q.removeHead();   // empty: no error
        assertEquals(0, q.size());
    }

    @Test
    public void queueCapsAtTwoHundredDroppingOldest() throws IOException {
        EventQueue q = new EventQueue(tempFile());
        for (int i = 0; i < 250; i++) q.add("{\"n\":" + i + "}");
        assertEquals(200, q.size());
        assertEquals("{\"n\":50}", q.peek());
    }

    @Test
    public void queueFlattensNewlinesAndIgnoresBlank() throws IOException {
        EventQueue q = new EventQueue(tempFile(), 5);
        q.add("a\nb");
        q.add("   ");
        q.add(null);
        assertEquals(1, q.size());
        assertEquals("a b", q.peek());
    }
}
