package dev.shikhar.laptophub.capture;

import java.net.URI;
import java.util.Locale;

/**
 * Pure rules for "Reply in Google Chat": which addresses the app may open, and how much text it may put on the
 * clipboard. No Android imports on purpose, so it can be unit tested on a plain JVM.
 *
 * Mirrored in mobile/scripts/test-chat-link.js, which parses THIS file and fails if the constants drift.
 * Keep every String constant on ONE line as a single string literal, and every int constant as a plain number.
 * Nothing here sends anything: the app copies the owner's draft and opens Chat; the owner pastes and presses Send.
 */
public final class ChatLink {
    private ChatLink() {}

    /** Google Chat. */
    public static final String PKG_CHAT = "com.google.android.apps.dynamite";
    public static final String HOST_CHAT = "chat.google.com";
    public static final String HOST_MAIL = "mail.google.com";
    public static final String MAIL_CHAT_PATH = "/chat";
    public static final int MAX_TEXT = 4000;

    /** https only; chat.google.com, or mail.google.com/chat (Chat inside Gmail); no login inside the address. */
    public static boolean isAllowed(String url) {
        if (url == null || url.length() > 400) return false;
        try {
            URI u = new URI(url.trim());
            if (!"https".equalsIgnoreCase(u.getScheme()) || u.getUserInfo() != null) return false;
            String host = u.getHost() == null ? "" : u.getHost().toLowerCase(Locale.ROOT);
            if (host.equals(HOST_CHAT)) return true;
            String path = u.getPath() == null ? "" : u.getPath();
            return host.equals(HOST_MAIL) && (path.equals(MAIL_CHAT_PATH) || path.startsWith(MAIL_CHAT_PATH + "/"));
        } catch (Exception e) {
            return false;
        }
    }

    public static String clip(String text) {
        if (text == null) return "";
        return text.length() > MAX_TEXT ? text.substring(0, MAX_TEXT) : text;
    }
}
