package dev.shikhar.laptophub.wake;

/** The life of the offline voice model on the phone. Pure logic, unit tested. */
public final class ModelStateMachine {
    private ModelStateMachine() {}

    public enum State { ABSENT, DOWNLOADING, UNPACKING, READY, FAILED }

    public enum Event { START, DOWNLOADED, UNPACKED, FAIL, FOUND, DELETE }

    /** A download of the small English model is about 40 MB; anything wildly off is rejected. */
    public static final long MIN_BYTES = 20L * 1024 * 1024;
    public static final long MAX_BYTES = 120L * 1024 * 1024;

    public static boolean sizeLooksRight(long bytes) {
        return bytes >= MIN_BYTES && bytes <= MAX_BYTES;
    }

    /** Returns the next state, or throws IllegalStateException for a move that makes no sense. */
    public static State next(State s, Event e) {
        switch (s) {
            case ABSENT:
            case FAILED:
                if (e == Event.START) return State.DOWNLOADING;
                if (e == Event.FOUND) return State.READY;
                if (e == Event.DELETE) return State.ABSENT;
                break;
            case DOWNLOADING:
                if (e == Event.DOWNLOADED) return State.UNPACKING;
                if (e == Event.FAIL) return State.FAILED;
                break;
            case UNPACKING:
                if (e == Event.UNPACKED) return State.READY;
                if (e == Event.FAIL) return State.FAILED;
                break;
            case READY:
                if (e == Event.DELETE) return State.ABSENT;
                if (e == Event.FOUND) return State.READY;
                break;
            default:
                break;
        }
        throw new IllegalStateException(s + " cannot handle " + e);
    }
}
