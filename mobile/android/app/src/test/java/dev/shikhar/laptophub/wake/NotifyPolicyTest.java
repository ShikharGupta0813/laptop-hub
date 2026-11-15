package dev.shikhar.laptophub.wake;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class NotifyPolicyTest {
    @Test
    public void listeningNotificationIsTheQuietestKind() {
        assertEquals(NotifyPolicy.IMPORTANCE_LOW, NotifyPolicy.listenImportance());
        assertTrue(NotifyPolicy.listenOnlyAlertOnce());
    }

    @Test
    public void noChannelPlaysASoundOrVibrates() {
        for (String id : new String[] {NotifyPolicy.CH_LISTEN, NotifyPolicy.CH_ALERT}) {
            assertFalse(id, NotifyPolicy.channelHasSound(id));
            assertFalse(id, NotifyPolicy.channelVibrates(id));
        }
    }

    @Test
    public void theLoudOldAlertChannelIsRetiredAndNotReused() {
        assertTrue(NotifyPolicy.isLegacy("wake_alert"));
        assertNotEquals("wake_alert", NotifyPolicy.CH_ALERT);   // channel sound settings stick to the id, so the id changed
        assertFalse(NotifyPolicy.isLegacy(NotifyPolicy.CH_ALERT));
        assertFalse(NotifyPolicy.isLegacy(NotifyPolicy.CH_LISTEN));
    }
}
