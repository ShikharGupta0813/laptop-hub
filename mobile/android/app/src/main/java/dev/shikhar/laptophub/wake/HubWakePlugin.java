package dev.shikhar.laptophub.wake;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;

import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.ArrayList;
import java.util.List;

/**
 * JS name: HubWake. Called by the hub page (js/wakebridge.js) as window.Capacitor.Plugins.HubWake.*
 * status / enable / disable / modelStatus / downloadModel / consumePending.
 */
@CapacitorPlugin(
    name = "HubWake",
    permissions = {
        @Permission(alias = "mic", strings = { Manifest.permission.RECORD_AUDIO }),
        @Permission(alias = "notif", strings = { Manifest.permission.POST_NOTIFICATIONS })
    }
)
public class HubWakePlugin extends Plugin {

    private boolean micGranted() {
        return ContextCompat.checkSelfPermission(getContext(), Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED;
    }

    /** Only Android 13+ has the notification permission; older versions always allow notifications. */
    private boolean notifGranted() {
        if (Build.VERSION.SDK_INT < 33) return true;
        return ContextCompat.checkSelfPermission(getContext(), Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED;
    }

    private JSObject modelObject() {
        ModelManager mm = ModelManager.get(getContext());
        JSObject o = new JSObject();
        o.put("state", mm.stateName());
        o.put("bytes", mm.bytes());
        o.put("total", mm.total());
        o.put("progress", mm.total() > 0 ? (double) mm.bytes() / (double) mm.total() : 0.0);
        o.put("error", mm.error());
        o.put("ready", mm.isReady());
        return o;
    }

    private JSObject statusObject() {
        Context c = getContext();
        JSObject o = new JSObject();
        o.put("supported", true);
        o.put("enabled", WakeState.enabled(c));
        o.put("running", WakeService.running);
        o.put("micGranted", micGranted());
        o.put("notifGranted", notifGranted());
        o.put("error", WakeState.lastError());
        o.put("model", modelObject());
        return o;
    }

    @PluginMethod
    public void status(PluginCall call) {
        call.resolve(statusObject());
    }

    @PluginMethod
    public void modelStatus(PluginCall call) {
        call.resolve(modelObject());
    }

    @PluginMethod
    public void downloadModel(PluginCall call) {
        ModelManager.get(getContext()).start();
        call.resolve(modelObject());
    }

    @PluginMethod
    public void enable(PluginCall call) {
        if (!ModelManager.get(getContext()).isReady()) {
            call.reject("Download the voice model first.");
            return;
        }
        List<String> need = new ArrayList<>();
        if (!micGranted()) need.add("mic");
        if (!notifGranted()) need.add("notif");
        if (need.isEmpty()) {
            startNow(call);
            return;
        }
        requestPermissionForAliases(need.toArray(new String[0]), call, "permissionsCallback");
    }

    @PermissionCallback
    private void permissionsCallback(PluginCall call) {
        if (!micGranted()) {
            call.reject("Microphone permission was refused. Allow it in Settings, Apps, Laptop Hub, Permissions.");
        } else if (!notifGranted()) {
            call.reject("Notification permission was refused. Android needs it to show that the microphone is on. "
                + "Allow it in Settings, Apps, Laptop Hub, Notifications.");
        } else {
            startNow(call);
        }
    }

    private void startNow(PluginCall call) {
        try {
            WakeState.setEnabled(getContext(), true);
            WakeService.start(getContext());
            call.resolve(statusObject());
        } catch (Exception e) {
            WakeState.setEnabled(getContext(), false);
            call.reject("Android would not start the listening service. Keep the app open and try again.");
        }
    }

    @PluginMethod
    public void disable(PluginCall call) {
        WakeState.setEnabled(getContext(), false);
        WakeService.stop(getContext());
        call.resolve(statusObject());
    }

    /** Returns {persona, text} once if a wake phrase was heard recently, otherwise {}. */
    @PluginMethod
    public void consumePending(PluginCall call) {
        JSObject o = new JSObject();
        String[] p = WakeState.takePending();
        if (p != null) {
            o.put("persona", p[0]);
            o.put("text", p[1]);
        }
        call.resolve(o);
    }
}
