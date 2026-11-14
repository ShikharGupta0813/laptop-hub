package dev.shikhar.laptophub.capture;

import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Pure decision logic for the general "alerts" capture: which apps may be forwarded. No Android or JSON imports, so
 * it is unit tested on a plain JVM (AlertLogicTest). The owner's list comes from the hub (AlertConfigStore); this
 * class only applies it.
 *
 * Modes: ALLOWLIST (default) forwards only packages the owner enabled. ALL_BUT_BLOCKLIST forwards every app except
 * the ones marked disabled. In both modes the built-in deny list (password managers, authenticators, banking
 * apps) and the hub's extra deny entries always win, and the app's own notifications are never forwarded.
 */
public final class AlertPolicy {

    public static final String MODE_ALLOWLIST = "allowlist";
    public static final String MODE_ALL_BUT_BLOCKLIST = "all-but-blocklist";

    /** Used until the hub list has been fetched at least once. Package names, exact match. */
    public static final String[] DEFAULT_PACKAGES = {
        "com.whatsapp", "com.whatsapp.w4b", "com.Slack", "com.microsoft.teams",
        "com.google.android.gm", "com.google.android.apps.dynamite", "com.asana.app",
        "com.atlassian.android.jira.core", "com.trello", "org.telegram.messenger",
        "com.microsoft.office.outlook", "com.google.android.apps.messaging", "com.samsung.android.messaging",
        "com.android.mms",
    };

    /** Deny by exact package: password managers, authenticators. Never forwarded even if the owner lists them. */
    public static final String[] BUILTIN_DENY_PACKAGES = {
        "com.agilebits.onepassword", "com.x8bit.bitwarden", "com.bitwarden.authenticator", "com.lastpass.lpandroid",
        "com.dashlane", "com.google.android.apps.authenticator2", "com.authy.authy", "com.azure.authenticator",
        "org.keepassdroid", "keepass2android.keepass2android", "com.callpod.android_apps.keeper", "me.proton.android.pass",
    };

    /**
     * Deny by fragment of the package name (lower case): anything that looks like banking or a password / OTP vault.
     * Payment apps have their own, separate, spend-only path (CaptureFilter) and are not forwarded here at all.
     */
    public static final String[] BUILTIN_DENY_FRAGMENTS = {
        "bank", "password", "authenticator", "keepass", "bitwarden", "onepassword", "lastpass",
        "hdfc", "icici", "sbifreedom", "kotak", "axis.mobile",
    };

    private final String mode;
    private final Map<String, Boolean> listed;     // lower-case package -> enabled
    private final Map<String, String> labels;      // lower-case package -> label from the hub
    private final Set<String> extraDeny;           // lower-case; exact package or fragment
    private final Set<String> names;               // the owner's names, for the mention hint

    public AlertPolicy(String mode, Map<String, Boolean> listed, Map<String, String> labels,
                       Set<String> extraDeny, Set<String> names) {
        this.mode = MODE_ALL_BUT_BLOCKLIST.equals(mode) ? MODE_ALL_BUT_BLOCKLIST : MODE_ALLOWLIST;
        this.listed = new HashMap<>();
        if (listed != null) {
            for (Map.Entry<String, Boolean> e : listed.entrySet()) {
                if (e.getKey() != null && !e.getKey().trim().isEmpty()) {
                    this.listed.put(lc(e.getKey()), Boolean.TRUE.equals(e.getValue()));
                }
            }
        }
        this.labels = new HashMap<>();
        if (labels != null) {
            for (Map.Entry<String, String> e : labels.entrySet()) {
                if (e.getKey() != null && e.getValue() != null) this.labels.put(lc(e.getKey()), e.getValue());
            }
        }
        this.extraDeny = lowerSet(extraDeny);
        this.names = lowerSet(names);
    }

    /** The built-in fallback: allowlist mode with DEFAULT_PACKAGES. */
    public static AlertPolicy defaults() {
        Map<String, Boolean> m = new LinkedHashMap<>();
        for (String p : DEFAULT_PACKAGES) m.put(p, Boolean.TRUE);
        return new AlertPolicy(MODE_ALLOWLIST, m, null, null, null);
    }

    private static String lc(String s) {
        return s == null ? "" : s.trim().toLowerCase(Locale.ROOT);
    }

    private static Set<String> lowerSet(Set<String> in) {
        Set<String> out = new HashSet<>();
        if (in != null) {
            for (String s : in) {
                String v = lc(s);
                if (!v.isEmpty()) out.add(v);
            }
        }
        return Collections.unmodifiableSet(out);
    }

    public String mode() {
        return mode;
    }

    public Set<String> names() {
        return names;
    }

    /** The hub's label for the package, or "" when it gave none. */
    public String label(String pkg) {
        String l = labels.get(lc(pkg));
        return l == null ? "" : l;
    }

    /** True when the package is on the built-in deny list or the hub's extra deny entries. */
    public boolean isDenied(String pkg) {
        String p = lc(pkg);
        if (p.isEmpty()) return true;
        for (String d : BUILTIN_DENY_PACKAGES) {
            if (p.equals(d.toLowerCase(Locale.ROOT))) return true;
        }
        for (String f : BUILTIN_DENY_FRAGMENTS) {
            if (p.contains(f)) return true;
        }
        for (String d : extraDeny) {
            if (p.contains(d)) return true;
        }
        return false;
    }

    /**
     * True when a notification from {@code pkg} may be read and forwarded. Own app, denied apps, payment apps
     * (spend-only path) and, in allowlist mode, apps that are not enabled, all return false.
     */
    public boolean allows(String pkg, String ownPkg) {
        String p = lc(pkg);
        if (p.isEmpty() || p.equals(lc(ownPkg))) return false;
        if (isDenied(p)) return false;
        if (CaptureFilter.isAllowedPackage(pkg)) return false;
        Boolean on = listed.get(p);
        if (MODE_ALL_BUT_BLOCKLIST.equals(mode)) return on == null || on;
        return on != null && on;
    }
}
