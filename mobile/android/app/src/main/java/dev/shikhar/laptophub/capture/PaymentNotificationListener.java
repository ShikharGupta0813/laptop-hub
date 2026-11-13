package dev.shikhar.laptophub.capture;

import android.app.Notification;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Parcelable;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;

/**
 * Reads notifications in three lanes, each decided on the package name before any content is touched:
 * (1) payment / bank apps (CaptureFilter.ALLOWED_PACKAGES) -> spend-only expense capture;
 * (2) Google Chat / Gmail while the legacy watch switch is on (WatchFilter) -> /api/watch/ingest;
 * (3) every other app the owner enabled in the hub's Alerts settings (AlertPolicy, cached from
 *     /api/alerts/capture-config) -> /api/alerts/ingest. Apps not on that list are ignored.
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
            if (WatchFilter.isWatchPackage(pkg) && handleWatch(sbn, pkg)) return;
            if (!CaptureFilter.isAllowedPackage(pkg)) {
                handleAlert(sbn, pkg);   // any other app: only if the owner enabled it (AlertPolicy)
                return;
            }
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

    private static final AlertFilter.Dedupe ALERT_SEEN = new AlertFilter.Dedupe();

    @Override
    public void onListenerConnected() {
        super.onListenerConnected();
        final Context ctx = getApplicationContext();
        Delivery.EXEC.execute(() -> Delivery.refreshConfigIfStale(ctx));
    }

    private static String lastSegment(String pkg) {
        int i = pkg.lastIndexOf('.');
        return i >= 0 && i < pkg.length() - 1 ? pkg.substring(i + 1) : pkg;
    }

    private static String appLabel(Context ctx, AlertPolicy policy, String pkg) {
        String l = policy.label(pkg);
        if (!l.isEmpty()) return l;
        try {
            PackageManager pm = ctx.getPackageManager();
            return str(pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)));
        } catch (Throwable e) {
            return lastSegment(pkg);
        }
    }

    /**
     * Any other app: forwarded to POST /api/alerts/ingest only when AlertPolicy (the owner's list from the hub,
     * or the built-in defaults) allows the package. Group summaries, ongoing / media / progress notifications,
     * lock-screen-secret notifications and repeats are dropped. Only the preview text is ever read.
     */
    private void handleAlert(StatusBarNotification sbn, String pkg) throws Exception {
        final Context ctx = getApplicationContext();
        AlertPolicy policy = AlertConfigStore.policy(ctx);
        if (!policy.allows(pkg, ctx.getPackageName())) return;   // first check: nothing is read for other apps
        Notification n = sbn.getNotification();
        if (n == null || n.extras == null) return;
        Bundle ex = n.extras;
        if (n.visibility == Notification.VISIBILITY_SECRET) return;
        boolean media = ex.containsKey(Notification.EXTRA_MEDIA_SESSION);
        if (AlertFilter.isNoise(n.flags, n.category, media)) return;

        if (AlertConfigStore.isStale(ctx)) Delivery.EXEC.execute(() -> Delivery.refreshConfigIfStale(ctx));

        String title = str(ex.getCharSequence(Notification.EXTRA_TITLE));
        String convTitle = str(ex.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE));
        String body = AlertFilter.body(str(ex.getCharSequence(Notification.EXTRA_TEXT)),
                str(ex.getCharSequence(Notification.EXTRA_BIG_TEXT)));
        String lastSender = "";
        Parcelable[] msgs = ex.getParcelableArray(Notification.EXTRA_MESSAGES);
        if (msgs != null && msgs.length > 0 && msgs[msgs.length - 1] instanceof Bundle) {
            Bundle last = (Bundle) msgs[msgs.length - 1];
            lastSender = str(last.getCharSequence("sender"));
            if (body.isEmpty()) body = str(last.getCharSequence("text"));
        }
        if (title.isEmpty() && body.isEmpty()) return;
        Boolean extraGroup = ex.containsKey(Notification.EXTRA_IS_GROUP_CONVERSATION)
                ? Boolean.valueOf(ex.getBoolean(Notification.EXTRA_IS_GROUP_CONVERSATION)) : null;
        boolean group = AlertFilter.isGroup(extraGroup, convTitle);
        String sender = group && !lastSender.isEmpty() ? lastSender : (lastSender.isEmpty() ? title : lastSender);
        String key = sbn.getKey() == null ? pkg + ":" + sbn.getId() : sbn.getKey();
        long ts = sbn.getPostTime() > 0 ? sbn.getPostTime() : System.currentTimeMillis();
        title = AlertFilter.cap(title, AlertFilter.MAX_TITLE);
        body = AlertFilter.cap(body, AlertFilter.MAX_TEXT);
        if (!ALERT_SEEN.firstSeen(key, title, body, ts)) return;

        String conversation = "";
        if (Build.VERSION.SDK_INT >= 26) {
            String sc = n.getShortcutId();
            if (sc != null) conversation = sc;
        }
        if (conversation.isEmpty()) conversation = pkg + ":" + (sbn.getTag() == null ? "" : sbn.getTag()) + ":" + sbn.getId();
        String mention = AlertFilter.mention(group, title, body, policy.names());
        String label = appLabel(ctx, policy, pkg);

        final JSONObject p = new JSONObject();
        p.put("source", "notification");
        p.put("app", AlertFilter.cap(label + " (" + pkg + ")", 100));
        p.put("package", pkg);
        p.put("app_label", label);
        p.put("title", title);
        p.put("sender", AlertFilter.cap(sender, 120));
        p.put("text", body);
        p.put("conversation", AlertFilter.cap(conversation, 200));
        p.put("group", group);
        p.put("mentioned", mention);               // "true" | "false" | "unknown": a hint, not a fact
        if ("true".equals(mention)) p.put("mention", true);   // the field hub/alerts.py reads
        p.put("key", key);
        p.put("ts", ts);
        Delivery.EXEC.execute(() -> Delivery.enqueueAlertAndFlush(ctx, p));
    }

    /** Google Chat / Gmail message notifications -> POST /api/watch/ingest. Nothing is read while the flag is off. */
    private boolean handleWatch(StatusBarNotification sbn, String pkg) throws Exception {
        final Context ctx = getApplicationContext();
        Notification n = sbn.getNotification();
        if (n == null || n.extras == null) return false;
        boolean summary = (n.flags & Notification.FLAG_GROUP_SUMMARY) != 0;
        boolean ongoing = (n.flags & Notification.FLAG_ONGOING_EVENT) != 0;
        String source = WatchFilter.route(pkg, ctx.getPackageName(), summary, ongoing,
                HubStore.watchMail(ctx), HubStore.watchChat(ctx));
        if (source == null) return false;   // flag off: fall through to the general alerts path

        Bundle ex = n.extras;
        String title = WatchFilter.cap(str(ex.getCharSequence(Notification.EXTRA_TITLE)), WatchFilter.MAX_TITLE);
        String text = WatchFilter.cap(WatchFilter.composeText(source,
                str(ex.getCharSequence(Notification.EXTRA_TEXT)),
                str(ex.getCharSequence(Notification.EXTRA_BIG_TEXT))), WatchFilter.MAX_TEXT);
        if (title.isEmpty() && text.isEmpty()) return true;
        long ts = sbn.getPostTime() > 0 ? sbn.getPostTime() : System.currentTimeMillis();
        if (!WatchFilter.firstSeen(pkg, title, text, ts)) return true;

        final JSONObject p = new JSONObject();
        p.put("source", source);
        p.put("app", pkg);
        p.put("title", title);
        p.put("text", text);
        if (source.equals("mail") && !title.isEmpty()) p.put("sender", title); // Gmail: title is the sender
        p.put("ts", ts);
        Delivery.EXEC.execute(() -> Delivery.enqueueAndFlush(ctx, Delivery.PATH_WATCH, p));
        return true;
    }
}
