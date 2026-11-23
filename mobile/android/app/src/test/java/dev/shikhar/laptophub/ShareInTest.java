package dev.shikhar.laptophub;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class ShareInTest {
    @Test
    public void onlyPlainTextSharesAreTaken() {
        assertTrue(ShareIn.accepts("android.intent.action.SEND", "text/plain"));
        assertTrue(ShareIn.accepts("android.intent.action.SEND", "TEXT/PLAIN"));
        assertFalse(ShareIn.accepts("android.intent.action.SEND", "image/png"));
        assertFalse(ShareIn.accepts("android.intent.action.SEND", null));
        assertFalse(ShareIn.accepts("android.intent.action.MAIN", "text/plain"));
        assertFalse(ShareIn.accepts("android.intent.action.VIEW", "text/plain"));
    }

    @Test
    public void nothingSharedMeansNoScript() {
        assertNull(ShareIn.script(null, "x"));
        assertNull(ShareIn.script("   \n ", "x"));
    }

    @Test
    public void textIsAnEscapedLiteralNeverCode() {
        String s = ShareIn.script("Job \"A\"\n</script><script>alert(1)</script>\\   end", "Subject");
        assertTrue(s.contains("text:\"Job \\\"A\\\"\\n\\u003c/script\\u003e\\u003cscript\\u003ealert(1)\\u003c/script\\u003e\\\\ \\u2028 end\""));
        assertFalse(s.contains("</script>"));
        assertFalse(s.contains("\n"));
        assertTrue(s.contains("location.hash='#/share'"));
        assertTrue(s.contains("hub-share"));
    }

    @Test
    public void controlCharactersAreEscaped() {
        assertEquals("\"a\\u0001b\"", ShareIn.jsString("a\u0001b"));
        assertEquals("\"a\\tb\"", ShareIn.jsString("a\tb"));
    }

    @Test
    public void textAndSubjectAreCut() {
        StringBuilder big = new StringBuilder();
        for (int i = 0; i < 30000; i++) big.append('x');
        assertEquals(ShareIn.MAX_TEXT, ShareIn.clip(big.toString(), ShareIn.MAX_TEXT).length());
        String s = ShareIn.script(big.toString(), big.toString());
        assertTrue(s.length() < ShareIn.MAX_TEXT + ShareIn.MAX_SUBJECT + 400);
    }
}
