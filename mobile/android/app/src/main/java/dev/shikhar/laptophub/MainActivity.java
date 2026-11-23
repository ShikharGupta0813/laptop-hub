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
        takeShare(getIntent());                // Share > REDLINE on a cold start
    }

    /** The script that hands a shared job to the hub page (see ShareIn.java). Null when nothing is waiting. */
    private String pendingShare;

    /** True while the app screen is in front; the wake service then leaves the answer to the page. */
    public static volatile boolean visible = false;

    @Override
    public void onResume() {
        super.onResume();
        visible = true;
        openAlertsIfAsked();
        if (pendingShare != null) deliverShare(0);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);   // so onResume sees the extras of the newest launch (the alert notification's "Open")
        takeShare(intent);   // Share > REDLINE while the app is already open
    }

    /** Share > REDLINE: ACTION_SEND text/plain. Only the shared text goes to the hub page; the app never opens the link. */
    private void takeShare(Intent in) {
        if (in == null || !ShareIn.accepts(in.getAction(), in.getType())) return;
        CharSequence text = in.getCharSequenceExtra(Intent.EXTRA_TEXT);
        String script = ShareIn.script(text == null ? null : text.toString(), in.getStringExtra(Intent.EXTRA_SUBJECT));
        in.setAction(Intent.ACTION_MAIN);   // once: a rotation or a resume must not hand the same share over again
        if (script == null) return;
        pendingShare = script;
        deliverShare(0);
    }

    /** Waits (a short, bounded number of tries) until the hub page has built its screens, then hands the share over. */
    private void deliverShare(final int attempt) {
        final String js = pendingShare;
        final WebView wv = getBridge() == null ? null : getBridge().getWebView();
        if (js == null || wv == null || attempt > 20) return;   // still pending after 20 tries: tried again on the next resume
        wv.postDelayed(() -> {
            if (!js.equals(pendingShare)) return;
            wv.evaluateJavascript(ShareIn.READY_CHECK, ready -> {
                if ("true".equals(ready)) {
                    pendingShare = null;
                    wv.evaluateJavascript(js, null);
                } else {
                    deliverShare(attempt + 1);
                }
            });
        }, attempt == 0 ? 700 : 600);
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
