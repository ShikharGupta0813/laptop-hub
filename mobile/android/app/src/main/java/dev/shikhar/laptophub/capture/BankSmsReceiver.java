package dev.shikhar.laptophub.capture;

import android.Manifest;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.provider.Telephony;
import android.telephony.SmsMessage;

import androidx.core.content.ContextCompat;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Optional: bank / UPI debit SMS. Works only after the user grants RECEIVE_SMS from the app.
 * Only the SMS broadcast is received (no READ_SMS, the inbox is never read).
 */
public class BankSmsReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(final Context context, Intent intent) {
        try {
            if (intent == null || !Telephony.Sms.Intents.SMS_RECEIVED_ACTION.equals(intent.getAction())) return;
            if (!HubStore.enabled(context)) return;
            if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECEIVE_SMS)
                    != PackageManager.PERMISSION_GRANTED) return;
            SmsMessage[] msgs = Telephony.Sms.Intents.getMessagesFromIntent(intent);
            if (msgs == null || msgs.length == 0) return;

            // Multi-part SMS arrive as several pieces from the same sender: join them.
            Map<String, StringBuilder> bySender = new LinkedHashMap<>();
            long ts = System.currentTimeMillis();
            for (SmsMessage m : msgs) {
                if (m == null) continue;
                String from = m.getOriginatingAddress() == null ? "" : m.getOriginatingAddress();
                StringBuilder sb = bySender.get(from);
                if (sb == null) {
                    sb = new StringBuilder();
                    bySender.put(from, sb);
                }
                if (m.getMessageBody() != null) sb.append(m.getMessageBody());
            }
            final List<JSONObject> out = new ArrayList<>();
            for (Map.Entry<String, StringBuilder> e : bySender.entrySet()) {
                String sender = e.getKey();
                String body = e.getValue().toString().trim();
                if (!CaptureFilter.shouldForwardSms(sender, body)) continue;
                if (!CaptureFilter.firstTime(sender, body, ts)) continue;
                JSONObject p = new JSONObject();
                p.put("source", "sms");
                p.put("sender", sender);
                p.put("title", "");
                p.put("text", body.length() > 1000 ? body.substring(0, 1000) : body);
                p.put("ts", ts);
                out.add(p);
            }
            if (out.isEmpty()) return;
            final PendingResult pr = goAsync();
            final Context app = context.getApplicationContext();
            Delivery.EXEC.execute(() -> {
                try {
                    for (JSONObject p : out) Delivery.enqueueAndFlush(app, p);
                } finally {
                    pr.finish();
                }
            });
        } catch (Throwable ignored) {
            // never crash the receiver, never log content
        }
    }
}
