# Laptop wake word API

`hub/wakeword.py`, switch in Settings > Appearance ("Listen on the laptop microphone"). Say "Hey Jarvis, ..." or "Hey Friday, ..." at the desk and the
laptop answers out loud; the phone stays untouched. Off by default. Routes need the login cookie.

## Config (`hub.json`)
| Key | Default | Meaning | Settings |
|---|---|---|---|
| `wake_laptop_enabled` | `false` | listen on the laptop microphone | yes (and the toggle route) |
| `wake_laptop_device` | `""` | `sounddevice` device name or number; empty = default microphone | yes |

## Routes
- `GET /api/wakeword/status` -> `{enabled, state, message, listening, device}`. `state` is one of `off, starting, listening, thinking, speaking,
  no_mic, missing_dependency, error`; `message` is a plain sentence for the last four.
- `POST /api/wakeword/toggle` body `{"enabled": true|false}` (omitted = flip) -> the new status. Saved to `hub.json` when the file is known. `400` for a non-boolean.

## Pipeline
16 kHz mono frames (optional `sounddevice`, imported lazily; missing package or no microphone is a status, never a crash) -> energy gate (adaptive to the
room, 0.3 s pre-roll, ends after 0.8 s of quiet, 12 s cap, clicks ignored) -> local Whisper (the hub's own transcriber, no audio leaves the laptop) ->
wake check (`match_wake`, a port of `js/wakephrase.js`: greeting word `hey|hi|ok|okay|yo` + Jarvis / your Jarvis name / Friday, mis-hearings jarvas / jarves;
"Jarvis is a nice name" does not wake) -> chime (Windows only) -> the rest of the sentence, or the next thing you say within 8 s after a bare wake phrase,
goes to `POST /api/assistant/message` in-process with `voice: true`, `auto_approve: false`, session `laptop-wake` -> the reply is spoken on the laptop.

## Safety and privacy
- Approvals still gate every change. An `approval_needed` event becomes a push (`ctx.notify`, source `wakeword`) and one spoken line; you approve on the phone.
- Audio is written to a temp WAV only to be transcribed and deleted at once. The transcript of anything that is not a wake phrase is dropped in memory:
  never stored, never logged, never audited. The audit log records only `wakeword` with the persona and a character count.
- Echo guard: the microphone is ignored while the laptop speaks and for 0.5 s after.
- Speaking: Windows SAPI through a fixed PowerShell argv (the text goes in on stdin, never into the command line); macOS `say`; Linux `espeak-ng`/`espeak` when installed; otherwise silent.
- Lifecycle: `start(ctx)` starts the thread when enabled; the hub shutting down stops it.

## Names come from the assistants list
The wake check uses the owner's assistants (Settings > Assistants, docs/api/assistants.md): every enabled assistant's name, wake names and mis-hearings, so a renamed Jarvis or a third assistant ("Hey Atlas") wakes the laptop too. `match_wake(text, jarvis_name, assistants)`; without a list the old Jarvis/Friday rules apply. The browser does the same through `js/wakephrase.js` and `GET /api/assistants` (cached, with the old constants as fallback). The always-on Android service keeps its built-in phrases.
