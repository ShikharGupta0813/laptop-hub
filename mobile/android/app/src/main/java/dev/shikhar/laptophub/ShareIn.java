package dev.shikhar.laptophub;

/**
 * Share > REDLINE. Pure logic (no Android classes) so it runs in a plain JVM test.
 *
 * Another app (LinkedIn, Naukri, a browser...) sends ACTION_SEND text/plain to MainActivity. The shared text is handed to
 * the hub web page as window.__hubShare, the page is sent to "#/share", and the hub's Shared job screen does the rest
 * (docs/api/sharein.md). The hub then scores the TEXT. The app itself never opens or reads the shared link.
 *
 * The text is untrusted: it is cut to a fixed length and written into the script as an escaped string literal, never
 * as code.
 */
final class ShareIn {
    private ShareIn() {}

    static final int MAX_TEXT = 20000;
    static final int MAX_SUBJECT = 200;

    /** True when the page has been built enough to take the hand-over. */
    static final String READY_CHECK = "(function(){return !!document.getElementById('views');})()";

    /** Only plain text shares are taken (a mime type of text/plain or text/*, never a file). */
    static boolean accepts(String action, String mime) {
        return "android.intent.action.SEND".equals(action) && mime != null && mime.toLowerCase(java.util.Locale.ROOT).startsWith("text/");
    }

    static String clip(String s, int max) {
        if (s == null) return "";
        s = s.trim();
        return s.length() <= max ? s : s.substring(0, max);
    }

    /** A JavaScript string literal, double-quoted. Safe inside a script: quotes, backslashes, line breaks, "<" and control characters are escaped. */
    static String jsString(String s) {
        StringBuilder b = new StringBuilder(s.length() + 16).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                case '<': b.append("\\u003c"); break;
                case '>': b.append("\\u003e"); break;
                case ' ': b.append("\\u2028"); break;
                case ' ': b.append("\\u2029"); break;
                default:
                    if (c < 0x20 || c == 0x7f) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    /** The one script run in the hub page, or null when nothing usable was shared. */
    static String script(String text, String subject) {
        String t = clip(text, MAX_TEXT);
        if (t.isEmpty()) return null;
        return "(function(){window.__hubShare={text:" + jsString(t) + ",url:\"\",subject:" + jsString(clip(subject, MAX_SUBJECT)) + "};"
                + "if(location.hash==='#/share'){window.dispatchEvent(new Event('hub-share'));}else{location.hash='#/share';}})()";
    }
}
