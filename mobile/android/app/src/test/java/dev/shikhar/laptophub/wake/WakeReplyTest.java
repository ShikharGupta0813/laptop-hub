package dev.shikhar.laptophub.wake;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;

import org.junit.Test;

public class WakeReplyTest {

    @Test
    public void parsesTextToolAndDone() {
        String data = "data: {\"type\":\"text\",\"t\":\"Hello \"}\n\n"
            + "data: {\"type\":\"text\",\"t\":\"there\"}\n\n"
            + "data: {\"type\":\"done\"}\n\n";
        List<WakeReply.Event> ev = WakeReply.parseSse(data);
        assertEquals(3, ev.size());
        assertEquals("text", ev.get(0).type);
        assertEquals("Hello ", ev.get(0).text);
        assertEquals("done", ev.get(2).type);
    }

    @Test
    public void chunksSplitAnywhereGiveTheSameEvents() {
        String data = "data: {\"type\":\"text\",\"t\":\"a \\\"quoted\\\" \\u00e9 line\\nnext\"}\r\n\r\ndata: {\"type\":\"done\"}\r\n";
        for (int cut = 1; cut < data.length(); cut++) {
            WakeReply.SseParser p = WakeReply.parseSse();
            List<WakeReply.Event> all = new ArrayList<>(p.feed(data.substring(0, cut)));
            all.addAll(p.feed(data.substring(cut)));
            all.addAll(p.finish());
            assertEquals("cut " + cut, 2, all.size());
            assertEquals("a \"quoted\" é line\nnext", all.get(0).text);
            assertEquals("done", all.get(1).type);
        }
    }

    @Test
    public void toolStartNeedsApprovalIsDetectedEvenWithNestedInput() {
        String a = "data: {\"type\":\"tool_start\",\"id\":\"1\",\"name\":\"send_mail\",\"input\":{\"to\":\"x}\",\"list\":[1,\"]\"]},\"needs_approval\":true}\n";
        String b = "data: {\"type\":\"tool_start\",\"id\":\"2\",\"name\":\"read\",\"input\":{},\"needs_approval\":false}\n";
        assertTrue(WakeReply.parseSse(a).get(0).needsApproval);
        assertFalse(WakeReply.parseSse(b).get(0).needsApproval);
    }

    @Test
    public void errorMessageAndJunkLines() {
        String data = ": keepalive\n\nevent: x\ndata: not json\ndata: {\"type\":\"error\",\"message\":\"Provider down\"}\ndata: {broken\n";
        List<WakeReply.Event> ev = WakeReply.parseSse(data);
        assertEquals(1, ev.size());
        assertEquals("error", ev.get(0).type);
        assertEquals("Provider down", ev.get(0).message);
    }

    @Test
    public void lastLineWithoutNewlineIsReadOnFinish() {
        WakeReply.SseParser p = WakeReply.parseSse();
        assertEquals(0, p.feed("data: {\"type\":\"done\"}").size());
        assertEquals(1, p.finish().size());
    }

    @Test
    public void speakableStripsMarkdown() {
        String md = "# Title\n\n**Bold** and `code` text.\n- one\n- two\n1. first\nSee [the docs](https://example.com/x) now.\n```\nblock\n```";
        assertEquals("Title Bold and code text. one two first See the docs now. block", WakeReply.speakable(md));
    }

    @Test
    public void speakableEmptyWhenNothingToSay() {
        assertEquals("", WakeReply.speakable(null));
        assertEquals("", WakeReply.speakable("  \n "));
        assertEquals("", WakeReply.speakable("**  **"));
    }

    @Test
    public void speakableCapsOnASentenceBoundary() {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < 200; i++) b.append("This is sentence number ").append(i).append(". ");
        String out = WakeReply.speakable(b.toString());
        assertTrue(out.length() <= WakeReply.MAX_SPOKEN);
        assertTrue(out.endsWith("."));
        assertTrue(out.length() > 600);
    }

    @Test
    public void speakableCapsALongRunWithoutPunctuationOnAWord() {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < 400; i++) b.append("word ");
        String out = WakeReply.speakable(b.toString());
        assertTrue(out.length() <= WakeReply.MAX_SPOKEN);
        assertTrue(out.endsWith("word"));
    }

    @Test
    public void openAppPositives() {
        String[] yes = {"open redline", "Open Red Line", "open the app", "open the redline app", "launch redline",
            "Open RedLine.", "open the red line app!", "please open redline", "  open   redline  "};
        for (String s : yes) assertTrue(s, WakeReply.isOpenApp(s));
    }

    @Test
    public void openAppNegatives() {
        String[] no = {"open the window", "", "what is open redline", "open redline and tell me my tasks",
            "redline", "open the door", "how do I open the app store"};
        for (String s : no) assertFalse(s, WakeReply.isOpenApp(s));
        assertFalse(WakeReply.isOpenApp(null));
    }
}
