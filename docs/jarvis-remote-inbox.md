# Optional: let the phone send commands to Jarvis

Laptop Hub can already start and stop Jarvis from the phone. This optional patch adds one more thing:
the Say box on the phone's Jarvis screen (`POST /api/jarvis/say`), which sends Jarvis a typed command ("open the Q3 report",
"what is on my calendar"). Without the patch everything else works; Say answers `409` with a pointer to this page.

**Read this first.** This patch was written without being able to run Jarvis. Jarvis needs Windows, a
microphone, openWakeWord and a Claude Code login, and none of that exists where it was written. The new
file `remote_inbox.py` below was run on its own with a temporary folder (it picks up files in order, skips
half-written ones and empty ones, and deletes what it read), and the edited `jarvis.py` was checked to
compile. It has **not** been run inside Jarvis. Treat the first run as a test (see "Test it" below).

It was written against the Jarvis commit `a887807` ("Add Jarvis voice assistant for Windows"). If your
`jarvis.py` has changed since, the diff may not apply cleanly; the four edits are small enough to make by hand.

## How it works

1. The hub writes each command as `remote_inbox\<id>.txt` in the Jarvis folder (it writes a hidden `.tmp`
   file first and renames it, so Jarvis never sees half a command).
2. `remote_inbox.py` runs a background thread that looks in that folder every 0.5 s, reads each `.txt`, deletes
   it and puts the text in a queue. The thread runs nothing itself.
3. Jarvis's own listening loop (`listen()` in `jarvis.py`) checks that queue on every pass and, if there is a
   command, sends it down the same path as a spoken one: `clean_command()`, then the "stay with me" and "that's all"
   checks, then `handle(command)`.

Why the thread does not call `handle()` itself: Jarvis has no lock around `handle()`. It is safe today only because
`handle()` is called from one place, the single listening thread, and the confirmations (`confirm()`) read the same
microphone queue as the listening loop. A second thread calling `handle()` could run two commands at once and
steal the microphone from a "say yes to confirm". Handing the text to the listening loop keeps it to one command at
a time, with Jarvis's own confirmations intact.

What you will notice:

* Jarvis **speaks** its reply on the laptop, as it does for any command. The phone only shows that the command was queued.
* If the command needs a confirmation ("Say yes to confirm"), it asks **out loud** and listens on the laptop microphone,
  and cancels if nobody answers. Confirmations are never skipped.
* After a phone command Jarvis keeps listening for 60 seconds without the wake word, as it does after a spoken one.
* A phone command waits for the command in progress to finish; commands never overlap.
* Jarvis already refuses to start twice (the `Local\JarvisVoiceAssistant` mutex), so the hub starting it can never double it.

## Install

1. Save this as `remote_inbox.py` in the Jarvis folder, next to `jarvis.py`:

```python
"""Commands typed on the phone (Laptop Hub) for Jarvis.

Laptop Hub writes each command as a small text file in the remote_inbox folder next to
jarvis.py. This module watches that folder from a background thread and hands each command to
Jarvis's listening loop, which runs it exactly as if you had said it: same router, same
confirmations ("say yes to confirm"), same one-command-at-a-time rule.

It never runs anything itself. Running a command from a second thread could collide with a
command you are speaking at the same moment (they share the microphone and one Claude session),
so the thread only queues the text and listen() in jarvis.py picks it up between commands.
"""
import os
import queue
import threading
import time

POLL_SECONDS = 0.5
MAX_CHARS = 2000            # the hub refuses anything longer
STALE_SECONDS = 120         # a command older than this at startup is left over from a crash

_pending = queue.Queue()
_started = False


def take():
    """The next command from the phone, or None. Called by Jarvis's listening loop."""
    try:
        return _pending.get_nowait()
    except queue.Empty:
        return None


def start(jarvis_dir, log=print):
    """Starts the watcher thread (once). Never raises: if it can't start, Jarvis works as before."""
    global _started
    if _started:
        return
    _started = True
    try:
        inbox = os.path.join(jarvis_dir, "remote_inbox")
        os.makedirs(inbox, exist_ok=True)
        _drop_stale(inbox, log)
        threading.Thread(target=_watch, args=(inbox, log), daemon=True, name="remote-inbox").start()
        log(f"remote inbox ready: {inbox}")
    except Exception as e:                       # noqa: BLE001 - must not stop Jarvis from starting
        log(f"remote inbox could not start: {e!r}")


def _drop_stale(inbox, log):
    now = time.time()
    for name in os.listdir(inbox):
        path = os.path.join(inbox, name)
        try:
            if name.endswith((".txt", ".tmp")) and now - os.path.getmtime(path) > STALE_SECONDS:
                os.remove(path)
                log(f"remote inbox: dropped an old command ({name})")
        except OSError:
            pass


def _watch(inbox, log):
    handled = set()          # files that were read but could not be deleted yet: never run twice
    while True:
        try:
            names = sorted(n for n in os.listdir(inbox) if n.endswith(".txt"))
            handled &= set(names)
            for name in names:                   # sorted names = the order they were sent
                path = os.path.join(inbox, name)
                if name not in handled:
                    try:
                        with open(path, encoding="utf-8", errors="replace") as fh:
                            text = fh.read(MAX_CHARS + 1).strip()
                    except OSError:
                        continue                 # still being written or locked: try next time
                    handled.add(name)
                    if text:
                        _pending.put(text[:MAX_CHARS])
                        log(f"REMOTE: {text[:MAX_CHARS]}")
                try:
                    os.remove(path)
                    handled.discard(name)
                except OSError:
                    pass                         # retried on the next poll, not run again
        except Exception as e:                   # noqa: BLE001 - keep watching whatever happens
            log(f"remote inbox error: {e!r}")
        time.sleep(POLL_SECONDS)
```

2. Apply this change to `jarvis.py` (four small edits; the last is optional). Either use `git apply`
   from the Jarvis folder, after saving the diff as `remote-inbox.diff`, or make the edits by hand:

```diff
--- a/jarvis.py
+++ b/jarvis.py
@@ -54,6 +54,7 @@
 import agenda
 import gchat
 import mailer
+import remote_inbox
 import screen
 from overlay import Overlay
 
@@ -1668,6 +1669,7 @@
 
     def worker():
         try:
+            remote_inbox.start(JARVIS_DIR, log)               # commands typed on the phone
             router.warm_up("[startup] Jarvis just started. Reply with an empty say and no actions.")
             asker.warm_up("Jarvis is starting up. Reply with just: ready")
             load_start_apps()
@@ -1776,12 +1778,19 @@
         print('\nListening. Say "Hey Jarvis", then your command.  (Ctrl+C to quit)\n')
         while True:
             try:
-                if stay or time.time() < follow_up_until:
+                remote = remote_inbox.take()                   # a command typed on the phone, or None
+                if remote:
+                    command = clean_command(remote)
+                    if not command:
+                        continue
+                    ui.set_state("thinking", "from your phone")
+                elif stay or time.time() < follow_up_until:
                     # ---- conversation: no wake word needed ----
                     manual_wake.clear()                        # already listening
                     ui.set_state("listening")
                     ui.set_window(None if stay else follow_up_until, FOLLOW_UP_SECONDS, stay)
                     wait = STAY_SILENCE_SECONDS if stay else max(0.5, follow_up_until - time.time())
+                    wait = min(wait, 2.0)                      # look at the phone inbox every 2 s
                     audio = record_utterance(wait_s=wait)
                     if audio is None:
                         # A short noise (an echo of Jarvis's own voice, a click) also comes back as
```

   * Edit 1 imports the module. Edit 2 starts the watcher thread. Edit 3 makes the loop look at the queue.
   * Edit 4, the line (`wait = min(wait, 2.0)`) is optional. Without it a phone command sent while Jarvis is in its
     60 second listening window waits until the window ends, because the loop is busy listening. With it Jarvis looks up every 2 s.
     It only shortens how long one listening call waits for you to start speaking; it does not cut you off.
3. Optional: add `remote_inbox/` to the Jarvis `.gitignore` (the folder is created on first start and is normally empty).
4. Restart Jarvis (from the phone's Jarvis screen: Stop, then Start, or close and reopen `jarvis.cmd`).
   The Jarvis log should show `remote inbox ready: ...`, and the hub's `remote_ready` turns true.

## Test it (in this order)

1. **Jarvis still works by voice.** Say "Hey Jarvis" and a simple command. If it does not start at all, undo the patch (below) and look at `jarvis.log`.
2. **Without the phone.** In the Jarvis folder run, in a second terminal:
   `echo what time is it> remote_inbox\test.txt`
   Within a second Jarvis should log `REMOTE: what time is it`, answer out loud, and the file should be gone.
3. **From the phone.** Open the Jarvis screen and use Say with something harmless ("what time is it").
4. **A confirmation.** Say something that makes Jarvis ask to confirm and check it asks out loud and cancels if you stay silent.
5. **Both at once.** Speak a command and send one from the phone together. They should run one after the other, never together.

## Remove it

* In `jarvis.py`, delete the `import remote_inbox` line, the `remote_inbox.start(...)` line and the `remote = ...` block
  (put back `if stay or time.time() < follow_up_until:`), and the `wait = min(wait, 2.0)` line.
  If you applied it with git, `git apply -R remote-inbox.diff` does all of that.
* Delete `remote_inbox.py` and the `remote_inbox` folder.
* The hub needs nothing changed: `remote_ready` goes back to false and Say answers `409` again.
