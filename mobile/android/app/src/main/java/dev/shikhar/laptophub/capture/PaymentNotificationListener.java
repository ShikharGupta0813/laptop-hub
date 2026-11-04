package dev.shikhar.laptophub.capture;

import android.app.Notification;
import android.content.Context;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;

/**
 * Reads notifications from the allow-listed payment / bank apps ONLY (see CaptureFilter.ALLOWED_PACKAGES).
 * Every other package is dropped on the first line, before its content is touched.
 */
public class PaymentNotificationListener extends NotificationListenerService {

    private static String str(CharSequence cs) {
        return cs == null ? "" : cs.toString().trim();
    }

    private static String cap(String s, int max) {
        return s.length() > max ? s.substring(0, max) : s;
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        try {
            if (sbn == null) return;
            String pkg = sbn.getPackageName();
            if (!CaptureFilter.isAllowedPackage(pkg)) return;
            final Context ctx = getApplicationContext();
            if (!HubStore.enabled(ctx)) return;
            Notification n = sbn.getNotification();
            if (n == null || n.extras == null) return;
            Bundle ex = n.extras;
            String title = str(ex.getCharSequence(Notification.EXTRA_TITLE));
            String text = str(ex.getCharSequence(Notification.EXTRA_TEXT));
            String big = str(ex.getCharSequence(Notification.EXTRA_BIG_TEXT));
            String body = big.length() > text.length() ? big : text;
            if (!CaptureFilter.looksLikeSpend(title, body)) return;
            long ts = sbn.getPostTime() > 0 ? sbn.getPostTime() : System.currentTimeMillis();
            if (!CaptureFilter.firstTime(pkg, title + " " + body, ts)) return;

            final JSONObject p = new JSONObject();
            p.put("source", "notification");
            p.put("package", pkg);
            p.put("title", cap(title, 200));
            p.put("text", cap(body, 1000));
            p.put("ts", ts);
            Delivery.EXEC.execute(() -> Delivery.enqueueAndFlush(ctx, p));
        } catch (Throwable ignored) {
            // never crash the listener, never log content
        }
    }
}
