package dev.shikhar.laptophub;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

import dev.shikhar.laptophub.capture.HubCapturePlugin;
import dev.shikhar.laptophub.wake.HubWakePlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Custom (non-npm) plugins must be registered before super.onCreate.
        registerPlugin(HubCapturePlugin.class);
        registerPlugin(HubWakePlugin.class);   // background "Hey Jarvis" (off until switched on)
        super.onCreate(savedInstanceState);
    }

    /** True while the app screen is in front; the wake service then leaves the answer to the page. */
    public static volatile boolean visible = false;

    @Override
    public void onResume() {
        super.onResume();
        visible = true;
    }

    @Override
    public void onPause() {
        visible = false;
        super.onPause();
    }

    @Override
    public void onStart() {
        super.onStart();
        // Downloads and new-window handling for the hub origin (see HubWeb.java).
        HubWeb.install(this, getBridge());
    }
}
