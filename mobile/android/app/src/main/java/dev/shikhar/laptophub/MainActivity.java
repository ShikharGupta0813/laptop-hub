package dev.shikhar.laptophub;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

import dev.shikhar.laptophub.capture.HubCapturePlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Custom (non-npm) plugins must be registered before super.onCreate.
        registerPlugin(HubCapturePlugin.class);
        super.onCreate(savedInstanceState);
    }

    @Override
    public void onStart() {
        super.onStart();
        // Downloads and new-window handling for the hub origin (see HubWeb.java).
        HubWeb.install(this, getBridge());
    }
}
