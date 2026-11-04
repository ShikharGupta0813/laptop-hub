package dev.shikhar.laptophub.capture;

import android.Manifest;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.provider.Settings;
import android.service.notification.NotificationListenerService;

import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import org.json.JSONObject;

/**
 * JS name: HubCapture. Called by the hub web page as window.Capacitor.Plugins.HubCapture.*
 * See docs/api/capture-android.md.
 */
@CapacitorPlugin(
    name = "HubCapture",
    permissions = { @Permission(alias = "sms", strings = { Manifest.permission.RECEIVE_SMS }) }
)
public class HubCapturePlugin extends Plugin {

    @Override
    public void load() {
        try {
            HubStore.saveOrigin(getContext(), getBridge().getConfig().getServerUrl());
        } catch (Throwable ignored) {
        }
    }

    @Override
    protected void handleOnResume() {
        final Context c = getContext().getApplicationContext();
        Delivery.EXEC.execute(() -> Delivery.flush(c));
    }

    private boolean listenerEnabled() {
        Context c = getContext();
        return NotificationManagerCompat.getEnabledListenerPackages(c).contains(c.getPackageName());
    }

    private boolean smsGranted() {
        return ContextCompat.checkSelfPermission(getContext(), Manifest.permission.RECEIVE_SMS)
                == PackageManager.PERMISSION_GRANTED;
    }

    private JSObject statusObject() {
        JSObject ret = new JSObject();
        Object origin = HubStore.origin(getContext());
        ret.put("listenerEnabled", listenerEnabled());
        ret.put("smsGranted", smsGranted());
        ret.put("enabled", HubStore.enabled(getContext()));
        ret.put("watchMail", HubStore.watchMail(getContext()));
        ret.put("watchChat", HubStore.watchChat(getContext()));
        ret.put("queued", HubStore.queueSize(getContext()));
        ret.put("hubOrigin", origin == null ? JSONObject.NULL : origin);
        return ret;
    }

    @PluginMethod
    public void status(PluginCall call) {
        call.resolve(statusObject());
    }

    @PluginMethod
    public void openListenerSettings(PluginCall call) {
        try {
            Intent i = new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not open Notification access settings");
        }
    }

    @PluginMethod
    public void requestSms(PluginCall call) {
        if (smsGranted()) {
            JSObject r = new JSObject();
            r.put("granted", true);
            call.resolve(r);
            return;
        }
        requestPermissionForAlias("sms", call, "smsPermissionCallback");
    }

    @PermissionCallback
    private void smsPermissionCallback(PluginCall call) {
        JSObject r = new JSObject();
        r.put("granted", smsGranted());
        call.resolve(r);
    }

    @PluginMethod
    public void setEnabled(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("enabled", true));
        HubStore.setEnabled(getContext(), on);
        if (on) {
            try {
                NotificationListenerService.requestRebind(
                    new ComponentName(getContext(), PaymentNotificationListener.class));
            } catch (Throwable ignored) {
            }
        }
        call.resolve(statusObject());
    }

    /** setWatch({ mail, chat }): independent of payment capture; a key that is left out keeps its value. */
    @PluginMethod
    public void setWatch(PluginCall call) {
        Boolean mail = call.getBoolean("mail", null);
        Boolean chat = call.getBoolean("chat", null);
        if (mail != null) HubStore.setWatchMail(getContext(), mail);
        if (chat != null) HubStore.setWatchChat(getContext(), chat);
        if (HubStore.watchMail(getContext()) || HubStore.watchChat(getContext())) {
            try {
                NotificationListenerService.requestRebind(
                    new ComponentName(getContext(), PaymentNotificationListener.class));
            } catch (Throwable ignored) {
            }
        }
        call.resolve(statusObject());
    }

    @PluginMethod
    public void flush(final PluginCall call) {
        final Context c = getContext().getApplicationContext();
        Delivery.EXEC.execute(() -> {
            Delivery.Result r = Delivery.flush(c);
            call.resolve(resultObject(r));
        });
    }

    /** Fakes one local payment so the whole path (phone, cookie, hub) can be verified. */
    @PluginMethod
    public void test(final PluginCall call) {
        final Context c = getContext().getApplicationContext();
        Delivery.EXEC.execute(() -> {
            try {
                JSONObject p = new JSONObject();
                p.put("source", "notification");
                p.put("package", c.getPackageName());
                p.put("title", "Test payment");
                p.put("text", "Paid ₹1 to Laptop Hub Test");
                p.put("ts", System.currentTimeMillis());
                p.put("test", true);
                call.resolve(resultObject(Delivery.enqueueAndFlush(c, p)));
            } catch (Exception e) {
                call.reject("Test failed");
            }
        });
    }

    private JSObject resultObject(Delivery.Result r) {
        JSObject ret = new JSObject();
        Object err = r.error == null ? JSONObject.NULL : r.error;
        ret.put("sent", r.sent);
        ret.put("queued", r.queued);
        ret.put("error", err);
        return ret;
    }
}
