package dev.shikhar.laptophub.wake;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

import org.junit.Test;

public class WakeLogicTest {

    // ---- WakeMatcher ----------------------------------------------------------------------------
    @Test
    public void greetingPlusNameTriggersEvenOnPartial() {
        WakeMatcher.Match m = WakeMatcher.match("{ \"partial\" : \"hey jarvis\" }", false);
        assertNotNull(m);
        assertEquals("jarvis", m.persona);
        assertEquals("", m.rest);
        assertEquals("friday", WakeMatcher.match("{\"text\":\"okay friday\"}", true).persona);
        assertEquals("friday", WakeMatcher.matchText("ok friday", false).persona);
    }

    @Test
    public void misheardNamesStillMatch() {
        assertEquals("jarvis", WakeMatcher.matchText("hey jarves", false).persona);
        assertEquals("jarvis", WakeMatcher.matchText("hey jarvas", false).persona);
        assertEquals("jarvis", WakeMatcher.matchText("Hey, Jarvis!", false).persona);
    }

    @Test
    public void restIsKept() {
        WakeMatcher.Match m = WakeMatcher.matchText("hey jarvis what's the time [unk]", false);
        assertEquals("what's the time", m.rest);
    }

    @Test
    public void barNameOnlyOnFinalAndAlone() {
        assertNull(WakeMatcher.matchText("jarvis", false));
        assertNotNull(WakeMatcher.matchText("jarvis", true));
        assertNotNull(WakeMatcher.matchText("friday", true));
    }

    @Test
    public void ordinaryTalkDoesNotTrigger() {
        assertNull(WakeMatcher.matchText("jarvis is a nice name", true));
        assertNull(WakeMatcher.matchText("jarvis is a nice name", false));
        assertNull(WakeMatcher.matchText("jarvis [unk] [unk]", true));
        assertNull(WakeMatcher.matchText("i said hey jarvis", true));
        assertNull(WakeMatcher.matchText("see you on friday", true));
        assertNull(WakeMatcher.matchText("it is friday", true));
        assertNull(WakeMatcher.matchText("hey", true));
        assertNull(WakeMatcher.matchText("", true));
        assertNull(WakeMatcher.match("{\"text\":\"\"}", true));
        assertNull(WakeMatcher.match(null, true));
    }

    @Test
    public void textOfHandlesEscapes() {
        assertEquals("say \"hi\"", WakeMatcher.textOf("{\"text\":\"say \\\"hi\\\"\"}"));
        assertEquals("", WakeMatcher.textOf("{}"));
    }

    // ---- WakeLaunch -----------------------------------------------------------------------------
    @Test
    public void hashIsEncodedAndCapped() {
        assertEquals("#/voice?persona=jarvis", WakeLaunch.hash("jarvis", ""));
        assertEquals("#/voice?persona=friday&text=what%20time%20is%20it%3F%20%26%20more",
            WakeLaunch.hash("friday", "what time is it? & more"));
        assertEquals("#/voice?persona=jarvis", WakeLaunch.hash("evil", null));
        StringBuilder long1 = new StringBuilder();
        for (int i = 0; i < 400; i++) long1.append('a');
        assertEquals(300, WakeLaunch.clean(long1.toString()).length());
        // an emoji (surrogate pair) on the cut line is not split in half
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < 299; i++) s.append('a');
        s.append("😀more");
        String c = WakeLaunch.clean(s.toString());
        assertFalse(Character.isHighSurrogate(c.charAt(c.length() - 1)));
        assertEquals("a b", WakeLaunch.clean("a\n\t b\u0000"));
    }

    // ---- SafeUnzip ------------------------------------------------------------------------------
    private static byte[] zip(String[] names, String[] bodies) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        try (ZipOutputStream z = new ZipOutputStream(bos)) {
            for (int i = 0; i < names.length; i++) {
                z.putNextEntry(new ZipEntry(names[i]));
                z.write(bodies[i].getBytes(StandardCharsets.UTF_8));
                z.closeEntry();
            }
        }
        return bos.toByteArray();
    }

    @Test
    public void unzipExtractsNormalFiles() throws IOException {
        File dir = Files.createTempDirectory("wake-unzip").toFile();
        int n = SafeUnzip.unzip(new ByteArrayInputStream(zip(new String[] {"m/am/final.mdl", "m/conf/x"},
            new String[] {"abc", "def"})), dir, 100, 1000);
        assertEquals(2, n);
        assertEquals("abc", new String(Files.readAllBytes(new File(dir, "m/am/final.mdl").toPath()), StandardCharsets.UTF_8));
    }

    @Test
    public void unzipRejectsZipSlipAndLimits() throws IOException {
        for (String bad : new String[] {"../evil.txt", "m/../../evil.txt", "/abs.txt", "a\\..\\b.txt"}) {
            File dir = Files.createTempDirectory("wake-slip").toFile();
            try {
                SafeUnzip.unzip(new ByteArrayInputStream(zip(new String[] {bad}, new String[] {"x"})), dir, 100, 1000);
                fail("should reject " + bad);
            } catch (IOException expected) {
                assertTrue(expected.getMessage(), expected.getMessage().contains("Unsafe"));
            }
            assertFalse(new File(dir.getParentFile(), "evil.txt").exists());
        }
        File dir = Files.createTempDirectory("wake-lim").toFile();
        try {
            SafeUnzip.unzip(new ByteArrayInputStream(zip(new String[] {"a"}, new String[] {"0123456789"})), dir, 100, 5);
            fail("too large");
        } catch (IOException expected) {
            // ok
        }
        try {
            SafeUnzip.unzip(new ByteArrayInputStream(zip(new String[] {"a", "b"}, new String[] {"1", "2"})), dir, 1, 100);
            fail("too many");
        } catch (IOException expected) {
            // ok
        }
    }

    // ---- ModelStateMachine ----------------------------------------------------------------------
    @Test
    public void modelStateHappyPathAndFailure() {
        ModelStateMachine.State s = ModelStateMachine.State.ABSENT;
        s = ModelStateMachine.next(s, ModelStateMachine.Event.START);
        assertEquals(ModelStateMachine.State.DOWNLOADING, s);
        s = ModelStateMachine.next(s, ModelStateMachine.Event.DOWNLOADED);
        assertEquals(ModelStateMachine.State.UNPACKING, s);
        s = ModelStateMachine.next(s, ModelStateMachine.Event.UNPACKED);
        assertEquals(ModelStateMachine.State.READY, s);
        s = ModelStateMachine.next(s, ModelStateMachine.Event.DELETE);
        assertEquals(ModelStateMachine.State.ABSENT, s);
        s = ModelStateMachine.next(ModelStateMachine.next(s, ModelStateMachine.Event.START), ModelStateMachine.Event.FAIL);
        assertEquals(ModelStateMachine.State.FAILED, s);
        assertEquals(ModelStateMachine.State.DOWNLOADING, ModelStateMachine.next(s, ModelStateMachine.Event.START));
    }

    @Test
    public void modelStateRejectsNonsense() {
        try {
            ModelStateMachine.next(ModelStateMachine.State.READY, ModelStateMachine.Event.START);
            fail();
        } catch (IllegalStateException expected) {
            // ok
        }
        try {
            ModelStateMachine.next(ModelStateMachine.State.DOWNLOADING, ModelStateMachine.Event.START);
            fail();
        } catch (IllegalStateException expected) {
            // ok
        }
        assertTrue(ModelStateMachine.sizeLooksRight(40L * 1024 * 1024));
        assertFalse(ModelStateMachine.sizeLooksRight(1000));
        assertFalse(ModelStateMachine.sizeLooksRight(5000L * 1024 * 1024));
    }
}
