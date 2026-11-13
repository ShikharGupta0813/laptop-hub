package dev.shikhar.laptophub.wake;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Unzips with zip-slip and zip-bomb protection. Pure Java, unit tested. */
public final class SafeUnzip {
    private SafeUnzip() {}

    /** Returns the number of files written. Throws IOException on any unsafe entry or when limits are exceeded. */
    public static int unzip(InputStream in, File destDir, int maxEntries, long maxTotalBytes) throws IOException {
        if (!destDir.isDirectory() && !destDir.mkdirs()) throw new IOException("Cannot create " + destDir);
        String base = destDir.getCanonicalPath() + File.separator;
        int entries = 0, files = 0;
        long total = 0;
        byte[] buf = new byte[64 * 1024];
        try (ZipInputStream zin = new ZipInputStream(in)) {
            ZipEntry e;
            while ((e = zin.getNextEntry()) != null) {
                if (++entries > maxEntries) throw new IOException("Too many entries");
                String name = e.getName();
                if (name.indexOf('\0') >= 0 || name.startsWith("/") || name.startsWith("\\") || name.contains("\\")
                        || name.matches("^[A-Za-z]:.*")) {
                    throw new IOException("Unsafe path in archive: " + name);
                }
                File out = new File(destDir, name);
                String canon = out.getCanonicalPath();
                if (!(canon + File.separator).startsWith(base)) {
                    throw new IOException("Unsafe path in archive: " + name);
                }
                if (e.isDirectory()) {
                    if (!out.isDirectory() && !out.mkdirs()) throw new IOException("Cannot create " + name);
                    continue;
                }
                File parent = out.getParentFile();
                if (parent != null && !parent.isDirectory() && !parent.mkdirs()) throw new IOException("Cannot create " + name);
                try (OutputStream os = new FileOutputStream(out)) {
                    int n;
                    while ((n = zin.read(buf)) > 0) {
                        total += n;
                        if (total > maxTotalBytes) throw new IOException("Archive too large");
                        os.write(buf, 0, n);
                    }
                }
                files++;
            }
        }
        return files;
    }
}
