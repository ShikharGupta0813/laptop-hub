package dev.shikhar.laptophub;

import android.content.Intent;
import android.os.Bundle;
import android.webkit.WebView;

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
        openAlertsIfAsked();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);   // so onResume sees the extras of the newest launch (the alert notification's "Open")
    }

    /** The alert notification opens the app on its alerts page. Only this one fixed address is ever accepted. */
    private void openAlertsIfAsked() {
        Intent in = getIntent();
        if (in == null || !"#/alerts".equals(in.getStringExtra("open_hash"))) return;
        in.removeExtra("open_hash");   // once
        final WebView wv = getBridge() == null ? null : getBridge().getWebView();
        if (wv != null) wv.postDelayed(() -> wv.evaluateJavascript("location.hash='#/alerts'", null), 700);
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
