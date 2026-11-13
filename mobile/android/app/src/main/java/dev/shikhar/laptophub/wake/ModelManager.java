package dev.shikhar.laptophub.wake;

import android.content.Context;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Downloads the small English Vosk model (about 40 MB, Apache-2.0) into the app's private storage the first
 * time the owner turns the background wake word on. Nothing else is ever downloaded.
 */
final class ModelManager {
    static final String MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip";

    private static ModelManager instance;

    static synchronized ModelManager get(Context c) {
        if (instance == null) instance = new ModelManager(c.getApplicationContext());
        return instance;
    }

    private final File root;
    private ModelStateMachine.State state;
    private long bytes;
    private long total;
    private String error = "";

    private ModelManager(Context c) {
        root = new File(c.getFilesDir(), "vosk");
        state = readyOnDisk() ? ModelStateMachine.State.READY : ModelStateMachine.State.ABSENT;
    }

    private File modelDir() {
        return new File(root, "model");
    }

    private boolean readyOnDisk() {
        return new File(modelDir(), "am/final.mdl").isFile();
    }

    /** The folder to hand to Vosk, or null when the model is not there. */
    synchronized File modelDirIfReady() {
        return readyOnDisk() ? modelDir() : null;
    }

    synchronized boolean isReady() {
        return state == ModelStateMachine.State.READY && readyOnDisk();
    }

    synchronized String stateName() {
        return state.name().toLowerCase(java.util.Locale.ROOT);
    }

    synchronized long bytes() {
        return bytes;
    }

    synchronized long total() {
        return total;
    }

    synchronized String error() {
        return error;
    }

    private synchronized void move(ModelStateMachine.Event e) {
        state = ModelStateMachine.next(state, e);
    }

    private synchronized void fail(String message) {
        error = message;
        try {
            state = ModelStateMachine.next(state, ModelStateMachine.Event.FAIL);
        } catch (IllegalStateException ignored) {
            state = ModelStateMachine.State.FAILED;
        }
    }

    /** Starts the download on a background thread. Does nothing when it is already running or done. */
    synchronized void start() {
        if (state == ModelStateMachine.State.DOWNLOADING || state == ModelStateMachine.State.UNPACKING) return;
        if (readyOnDisk()) {
            state = ModelStateMachine.State.READY;
            return;
        }
        state = ModelStateMachine.next(state, ModelStateMachine.Event.START);
        bytes = 0;
        total = 0;
        error = "";
        Thread t = new Thread(this::run, "wake-model-download");
        t.start();
    }

    private void run() {
        File part = new File(root, "model.zip.part");
        File tmp = new File(root, "model.tmp");
        try {
            if (!root.isDirectory() && !root.mkdirs()) throw new IOException("Cannot create storage folder");
            deleteTree(tmp);
            download(part);
            move(ModelStateMachine.Event.DOWNLOADED);
            try (InputStream in = new FileInputStream(part)) {
                SafeUnzip.unzip(in, tmp, 5000, 400L * 1024 * 1024);
            }
            File found = findModelRoot(tmp);
            if (found == null) throw new IOException("The downloaded file is not a Vosk model");
            deleteTree(modelDir());
            if (!found.renameTo(modelDir())) throw new IOException("Could not move the model into place");
            deleteTree(tmp);
            //noinspection ResultOfMethodCallIgnored
            part.delete();
            move(ModelStateMachine.Event.UNPACKED);
        } catch (Throwable t) {
            String m = t.getMessage();
            fail(m == null || m.isEmpty() ? t.getClass().getSimpleName() : m);
            deleteTree(tmp);
            //noinspection ResultOfMethodCallIgnored
            part.delete();
        }
    }

    private void download(File part) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(MODEL_URL).openConnection();
        c.setConnectTimeout(20_000);
        c.setReadTimeout(30_000);
        try {
            if (c.getResponseCode() != 200) throw new IOException("Download failed (HTTP " + c.getResponseCode() + ")");
            long len = c.getContentLengthLong();
            if (len > 0 && !ModelStateMachine.sizeLooksRight(len)) {
                throw new IOException("The model file has an unexpected size (" + len + " bytes)");
            }
            synchronized (this) {
                total = len > 0 ? len : 0;
            }
            long got = 0;
            byte[] buf = new byte[64 * 1024];
            try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part)) {
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    got += n;
                    if (got > ModelStateMachine.MAX_BYTES) throw new IOException("The download is larger than expected");
                    synchronized (this) {
                        bytes = got;
                    }
                }
            }
            if (!ModelStateMachine.sizeLooksRight(got) || (len > 0 && got != len)) {
                throw new IOException("The download is incomplete or the wrong size");
            }
        } finally {
            c.disconnect();
        }
    }

    /** Vosk zips hold one top-level folder; find the folder that contains am/final.mdl. */
    private static File findModelRoot(File dir) {
        if (new File(dir, "am/final.mdl").isFile()) return dir;
        File[] kids = dir.listFiles();
        if (kids == null) return null;
        for (File k : kids) {
            if (k.isDirectory() && new File(k, "am/final.mdl").isFile()) return k;
        }
        return null;
    }

    private static void deleteTree(File f) {
        if (f == null || !f.exists()) return;
        File[] kids = f.listFiles();
        if (kids != null) {
            for (File k : kids) deleteTree(k);
        }
        //noinspection ResultOfMethodCallIgnored
        f.delete();
    }
}
