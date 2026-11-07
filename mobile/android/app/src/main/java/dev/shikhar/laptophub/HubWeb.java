package dev.shikhar.laptophub;

import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Environment;
import android.os.Message;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeWebChromeClient;

import java.util.Locale;

/** Downloads and new-window handling for the hub WebView. Only the hub origin is ever loaded in the app. */
final class HubWeb {
    private final Context ctx;
    private final WebView web;
    private final String scheme;
    private final String host;
    private final int port;

    private HubWeb(Context ctx, WebView web, Uri hub) {
        this.ctx = ctx;
        this.web = web;
        this.scheme = hub.getScheme() == null ? "" : hub.getScheme().toLowerCase(Locale.ROOT);
        this.host = hub.getHost() == null ? "" : hub.getHost().toLowerCase(Locale.ROOT);
        this.port = effectivePort(hub);
    }

    static void install(Context ctx, Bridge bridge) {
        if (bridge == null || bridge.getWebView() == null) return;
        String serverUrl = bridge.getConfig() == null ? null : bridge.getConfig().getServerUrl();
        if (serverUrl == null || serverUrl.isEmpty()) return;
        final HubWeb h = new HubWeb(ctx, bridge.getWebView(), Uri.parse(serverUrl));
        if (h.host.isEmpty()) return;
        WebView web = bridge.getWebView();
        web.setDownloadListener(h.downloadListener());
        web.getSettings().setSupportMultipleWindows(true);
        web.getSettings().setJavaScriptCanOpenWindowsAutomatically(true);
        web.setWebChromeClient(new BridgeWebChromeClient(bridge) {
            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture, Message resultMsg) {
                WebView popup = new WebView(view.getContext());
                popup.setWebViewClient(new WebViewClient() {
                    private boolean done = false;
                    private boolean route(WebView v, String url) {
                        if (!done && url != null && !url.equals("about:blank")) {
                            done = true;
                            h.route(url);
                            v.stopLoading();
                            v.destroy();
                        }
                        return true;
                    }
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                        return route(v, r.getUrl().toString());
                    }
                    @Override
                    public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) {
                        route(v, url);
                    }
                });
                WebView.WebViewTransport t = (WebView.WebViewTransport) resultMsg.obj;
                t.setWebView(popup);
                resultMsg.sendToTarget();
                return true;
            }
        });
    }

    private static int effectivePort(Uri u) {
        if (u.getPort() != -1) return u.getPort();
        return "http".equalsIgnoreCase(u.getScheme()) ? 80 : 443;
    }

    private boolean isHub(Uri u) {
        String s = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
        String hst = u.getHost() == null ? "" : u.getHost().toLowerCase(Locale.ROOT);
        return s.equals(scheme) && hst.equals(host) && effectivePort(u) == port;
    }

    /** Decide what to do with a URL opened in a new window. */
    void route(String url) {
        final Uri u;
        try { u = Uri.parse(url); } catch (Exception e) { return; }
        String s = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
        web.post(() -> {
            if (isHub(u)) {
                if ("1".equals(u.getQueryParameter("download"))) startDownload(u.toString(), null, null);
                else web.loadUrl(u.toString());
            } else if (s.equals("http") || s.equals("https") || s.equals("mailto") || s.equals("tel")) {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, u);
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    ctx.startActivity(i);
                } catch (ActivityNotFoundException ignored) { }
            }
            // Any other scheme is refused.
        });
    }

    DownloadListener downloadListener() {
        return (url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri u = Uri.parse(url);
            if (!isHub(u)) { route(url); return; }
            startDownload(url, contentDisposition, mimeType);
        };
    }

    private void startDownload(String url, String contentDisposition, String mimeType) {
        try {
            String name = sanitize(URLUtil.guessFileName(url, contentDisposition, mimeType));
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null && !cookie.isEmpty()) req.addRequestHeader("Cookie", cookie);
            req.addRequestHeader("X-Hub", "1");
            req.setTitle(name);
            if (mimeType != null && !mimeType.isEmpty()) req.setMimeType(mimeType);
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            DownloadManager dm = (DownloadManager) ctx.getSystemService(Context.DOWNLOAD_SERVICE);
            dm.enqueue(req);
            Toast.makeText(ctx, "Downloading " + name + "…", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(ctx, "Download failed: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    static String sanitize(String raw) {
        String n = raw == null ? "" : raw;
        int slash = Math.max(n.lastIndexOf('/'), n.lastIndexOf('\\'));
        if (slash >= 0) n = n.substring(slash + 1);
        n = n.replaceAll("[\\p{Cntrl}:*?\"<>|]", "_").trim();
        while (n.startsWith(".")) n = n.substring(1);
        if (n.length() > 120) n = n.substring(n.length() - 120);
        return n.isEmpty() ? "download" : n;
    }
}
