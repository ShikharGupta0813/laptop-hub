package dev.shikhar.laptophub.capture;

import android.app.Notification;
import android.content.Context;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;

/**
 * Reads notifications from two fixed allow-lists ONLY: payment / bank apps (CaptureFilter.ALLOWED_PACKAGES) and,
 * when the user has switched it on, Google Chat and Gmail (WatchFilter). Every other package is dropped on the
 * first lines, before its content is touched.
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
            if (WatchFilter.isWatchPackage(pkg)) {
                handleWatch(sbn, pkg);
                return;
            }
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

    /** Google Chat / Gmail message notifications -> POST /api/watch/ingest. Nothing is read while the flag is off. */
    private void handleWatch(StatusBarNotification sbn, String pkg) throws Exception {
        final Context ctx = getApplicationContext();
        Notification n = sbn.getNotification();
        if (n == null || n.extras == null) return;
        boolean summary = (n.flags & Notification.FLAG_GROUP_SUMMARY) != 0;
        boolean ongoing = (n.flags & Notification.FLAG_ONGOING_EVENT) != 0;
        String source = WatchFilter.route(pkg, ctx.getPackageName(), summary, ongoing,
                HubStore.watchMail(ctx), HubStore.watchChat(ctx));
        if (source == null) return;

        Bundle ex = n.extras;
        String title = WatchFilter.cap(str(ex.getCharSequence(Notification.EXTRA_TITLE)), WatchFilter.MAX_TITLE);
        String text = WatchFilter.cap(WatchFilter.composeText(source,
                str(ex.getCharSequence(Notification.EXTRA_TEXT)),
                str(ex.getCharSequence(Notification.EXTRA_BIG_TEXT))), WatchFilter.MAX_TEXT);
        if (title.isEmpty() && text.isEmpty()) return;
        long ts = sbn.getPostTime() > 0 ? sbn.getPostTime() : System.currentTimeMillis();
        if (!WatchFilter.firstSeen(pkg, title, text, ts)) return;

        final JSONObject p = new JSONObject();
        p.put("source", source);
        p.put("app", pkg);
        p.put("title", title);
        p.put("text", text);
        if (source.equals("mail") && !title.isEmpty()) p.put("sender", title); // Gmail: title is the sender
        p.put("ts", ts);
        Delivery.EXEC.execute(() -> Delivery.enqueueAndFlush(ctx, Delivery.PATH_WATCH, p));
    }
}
