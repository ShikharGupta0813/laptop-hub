package dev.shikhar.laptophub.wake;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.List;

import org.junit.Test;

public class AlertSpeechTest {

    private static final String CALL_ITEM = "{\"alerts\":[{\"id\":\"c3f9a1b2c4d5e\",\"urgency\":3,"
        + "\"speech\":\"Reminder: Call the bank about the card\",\"title\":\"Call reminder\",\"kind\":\"reminder\","
        + "\"source\":\"reminder\",\"preview\":\"Call the bank about the card\",\"call\":true}]}";

    private static AlertSpeech.Item one(String json) {
        List<AlertSpeech.Item> l = AlertSpeech.parse(json);
        assertEquals(1, l.size());
        return l.get(0);
    }

    @Test
    public void exactHubCallReminderShape() {
        AlertSpeech.Item i = one(CALL_ITEM);
        assertEquals("c3f9a1b2c4d5e", i.id);
        assertEquals(3, i.urgency);
        assertTrue(i.call);
        assertTrue(AlertSpeech.shouldSpeak(i));
        assertTrue(AlertSpeech.shouldRing(i));
        assertEquals("Reminder: Call the bank about the card", AlertSpeech.fullText(i));
    }

    @Test
    public void urgencyTwoSpeaksOnly() {
        AlertSpeech.Item i = one("{\"alerts\":[{\"id\":\"a1\",\"urgency\":2,\"speech\":\"Meeting in 5 minutes\","
            + "\"kind\":\"meeting\",\"source\":\"calendar\",\"call\":false}]}");
        assertTrue(AlertSpeech.shouldSpeak(i));
        assertFalse(AlertSpeech.shouldRing(i));
        assertEquals("Meeting in 5 minutes", AlertSpeech.shortText(i));
    }

    @Test
    public void lowUrgencyIsLeftAlone() {
        AlertSpeech.Item i = one("{\"alerts\":[{\"id\":\"a2\",\"urgency\":1,\"preview\":\"hello\"}]}");
        assertFalse(AlertSpeech.shouldSpeak(i));
        assertFalse(AlertSpeech.shouldRing(i));
    }

    @Test
    public void urgencyThreeRingsEvenWithoutCallFlag() {
        AlertSpeech.Item i = one("{\"alerts\":[{\"id\":\"a3\",\"urgency\":3,\"source\":\"hub\",\"preview\":\"Disk is full\"}]}");
        assertTrue(AlertSpeech.shouldRing(i));
        assertEquals("hub: Disk is full", AlertSpeech.fullText(i));
    }

    @Test
    public void callFlagRingsEvenIfUrgencyMissing() {
        AlertSpeech.Item i = one("{\"alerts\":[{\"id\":\"a4\",\"call\":true,\"preview\":\"Pay rent\"}]}");
        assertEquals(0, i.urgency);
        assertTrue(AlertSpeech.shouldSpeak(i));
        assertTrue(AlertSpeech.shouldRing(i));
    }

    @Test
    public void fallbackUsesSenderThenSourceAndStripsMarkdown() {
        AlertSpeech.Item i = one("{\"alerts\":[{\"id\":\"a5\",\"urgency\":2,\"sender\":\"Priya\",\"source\":\"slack\","
            + "\"preview\":\"**Server** is [down](http://x.test/y) now\"}]}");
        assertEquals("Priya: Server is down now", AlertSpeech.shortText(i));
    }

    @Test
    public void capsAreApplied() {
        StringBuilder b = new StringBuilder();
        for (int k = 0; k < 300; k++) b.append("word ");
        AlertSpeech.Item i = one("{\"alerts\":[{\"id\":\"a6\",\"urgency\":3,\"speech\":\"" + b + "\"}]}");
        assertTrue(AlertSpeech.shortText(i).length() <= AlertSpeech.SHORT_CAP);
        assertTrue(AlertSpeech.fullText(i).length() <= AlertSpeech.FULL_CAP);
        assertTrue(AlertSpeech.fullText(i).length() > AlertSpeech.SHORT_CAP);
    }

    @Test
    public void tolerantParsing() {
        assertTrue(AlertSpeech.parse(null).isEmpty());
        assertTrue(AlertSpeech.parse("").isEmpty());
        assertTrue(AlertSpeech.parse("not json").isEmpty());
        assertTrue(AlertSpeech.parse("{\"alerts\":null}").isEmpty());
        assertTrue(AlertSpeech.parse("{\"alerts\":[{\"urgency\":3}]}").isEmpty());   // no id: skipped
        assertTrue(AlertSpeech.parse("{\"alerts\":[1,\"x\",null,[]]}").isEmpty());
        AlertSpeech.Item i = one("{\"extra\":{\"a\":[1,2]},\"alerts\":[{\"id\":\"x\",\"urgency\":\"3\",\"new_field\":{\"k\":[true]},"
            + "\"preview\":\"caf\\u00e9 \\\"ok\\\"\"}]}");
        assertEquals(0, i.urgency);                 // a string is not a number: default
        assertEquals("café \"ok\"", i.preview);
        assertEquals(3, one("{\"alerts\":[{\"id\":\"y\",\"urgency\":9}]}").urgency);   // clamped
        assertEquals(1, AlertSpeech.parse("[{\"id\":\"z\",\"urgency\":2}]").size());      // bare list
    }

    @Test
    public void truncatedJsonGivesNothingNotACrash() {
        assertTrue(AlertSpeech.parse(CALL_ITEM.substring(0, 60)).isEmpty());
    }

    @Test
    public void dedupeRemembersAndIsBounded() {
        AlertSpeech.Seen s = new AlertSpeech.Seen(2);
        assertTrue(s.add("a"));
        assertFalse(s.add("a"));
        assertTrue(s.add("b"));
        assertTrue(s.add("c"));          // "a" falls out
        assertFalse(s.has("a"));
        assertTrue(s.has("c"));
    }

    @Test
    public void ackBodyIsValidJson() {
        assertEquals("{\"ids\":[\"c1\",\"c2\"]}", AlertSpeech.ackBody(Arrays.asList("c1", "", "c2")));
        assertEquals("{\"ids\":[\"a\\\"b\"]}", AlertSpeech.ackBody(Arrays.asList("a\"b")));
        assertEquals("{\"ids\":[]}", AlertSpeech.ackBody(Arrays.<String>asList()));
    }

    @Test
    public void spokenAlertsSwitch() {
        assertFalse(AlertSpeech.parseSpokenAlerts("{\"name\":\"Friday\",\"spoken_alerts\":false}"));
        assertTrue(AlertSpeech.parseSpokenAlerts("{\"spoken_alerts\":true}"));
        assertTrue(AlertSpeech.parseSpokenAlerts("{}"));
        assertTrue(AlertSpeech.parseSpokenAlerts("garbage"));
    }
}
