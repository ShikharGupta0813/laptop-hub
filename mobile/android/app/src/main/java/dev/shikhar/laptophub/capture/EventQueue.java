package dev.shikhar.laptophub.capture;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * A tiny file-backed FIFO of one-line strings (JSON events), capped at {@link #CAP}; when full the OLDEST is dropped.
 * Pure Java (java.io only, so it also works on API 24), runs on a plain JVM. One event per line: newlines in the
 * input are flattened (JSONObject.toString() already escapes them). All methods are synchronized; the file is
 * rewritten through a temporary file.
 */
public final class EventQueue {
    public static final int CAP = 200;

    private final File file;
    private final int cap;

    public EventQueue(File file) {
        this(file, CAP);
    }

    public EventQueue(File file, int cap) {
        this.file = file;
        this.cap = Math.max(1, cap);
    }

    private List<String> read() {
        List<String> out = new ArrayList<>();
        if (!file.exists()) return out;
        try (BufferedReader r = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) {
                if (!line.isEmpty()) out.add(line);
            }
        } catch (IOException | RuntimeException e) {
            // unreadable: start empty rather than crash the listener
        }
        return out;
    }

    private void write(List<String> lines) {
        File tmp = new File(file.getPath() + ".tmp");
        try {
            StringBuilder sb = new StringBuilder();
            for (String l : lines) sb.append(l).append('\n');
            try (FileOutputStream out = new FileOutputStream(tmp)) {
                out.write(sb.toString().getBytes(StandardCharsets.UTF_8));
            }
            if (!tmp.renameTo(file)) {
                // some file systems will not rename over an existing file
                if (file.exists() && !file.delete()) return;
                tmp.renameTo(file);
            }
        } catch (IOException | RuntimeException e) {
            // disk full or similar: the event is lost, never crash
        }
    }

    /** Appends one event (newlines are flattened). Drops the oldest entries above the cap. */
    public synchronized void add(String line) {
        if (line == null) return;
        String one = line.replace('\r', ' ').replace('\n', ' ');
        if (one.trim().isEmpty()) return;
        List<String> all = read();
        all.add(one);
        while (all.size() > cap) all.remove(0);
        write(all);
    }

    public synchronized int size() {
        return read().size();
    }

    /** The oldest event, or null. */
    public synchronized String peek() {
        List<String> all = read();
        return all.isEmpty() ? null : all.get(0);
    }

    /** Removes the oldest event. */
    public synchronized void removeHead() {
        List<String> all = read();
        if (all.isEmpty()) return;
        all.remove(0);
        write(all);
    }
}
