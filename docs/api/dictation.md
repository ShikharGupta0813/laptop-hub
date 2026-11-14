# Laptop dictation API

`hub/dictation.py`, sheet in Settings > Dictation (laptop) (`js/dictation.js`). Hold a hotkey on the LAPTOP, speak, let go, and the words are typed into
whatever window has focus. Off by default. Routes need the login cookie.

**The target is whatever has focus** when you let go. REDLINE cannot see inside other programs, so it cannot know a field is a password box; that is what the blocklist is for.

## Config (`hub.json`; `null` for a list = the built-in default)
| Key | Default | Meaning |
|---|---|---|
| `dictation_enabled` | `false` | listen for the hotkey |
| `dictation_paused` | `false` | pause switch: hotkey ignored, settings kept |
| `dictation_hotkey` | `ctrl+alt+space` | 1 to 4 keys joined by `+`; a single key must be a function key |
| `dictation_mode` | `hold` | `hold` (push to talk) or `toggle` (press to start, press to stop) |
| `dictation_language` | `""` | Whisper language code, empty = detect |
| `dictation_punctuate` | `true` | spoken punctuation on |
| `dictation_spoken` | comma, full stop, question mark, exclamation mark, colon, semicolon, new line | `{phrase: symbol}`; symbol `"\n"` = line break |
| `dictation_add_space` | `true` | trailing space after each chunk |
| `dictation_max_seconds` | `120` | recording stops by itself (5 to 600) |
| `dictation_max_chars` | `2000` | most characters typed per dictation (50 to 10000) |
| `dictation_newline` | `false` | may "new line" press Enter; off = it becomes a space |
| `dictation_use_clipboard` | `false` | paste (Ctrl+V) instead of typing; the old clipboard is restored |
| `dictation_blocklist` | password, keepass, bitwarden, 1password, lastpass | window title / program fragments where nothing is recorded or typed |
| `dictation_scratch_phrases` | `scratch that` | say alone: backspace the last chunk |
| `dictation_assistant_commands` | `false` | "send to Jarvis ..." goes to the assistant |
| `dictation_assistant_prefixes` | `send to jarvis`, `ask jarvis` | how such a sentence starts |

## Routes
- `GET /api/dictation/status` -> `{enabled, paused, state, message, active, recording, hotkey, mode, settings{...effective values}, defaults, target_note}`.
  `state`: `off, starting, ready, recording, transcribing, typing, thinking, paused, missing_dependency, no_mic, error`. Missing packages give `missing_dependency` with
  `Dictation needs optional packages. Run: python -m pip install pynput sounddevice`.
- `POST /api/dictation/toggle` body `{"enabled": bool}` and/or `{"paused": bool}` (empty body flips `enabled`) -> status. Starts/stops the hotkey listener. `400` for non-booleans.
- `PUT /api/dictation/settings` body: any of `enabled, paused, hotkey, mode, language, punctuate, add_space, max_seconds, max_chars, newline, use_clipboard,
  assistant_commands, blocklist, spoken, scratch_phrases, assistant_prefixes` (no `dictation_` prefix) -> status. `400` with a plain sentence for anything invalid. Saved to `hub.json`.

## Pipeline
`pynput` (or the `keyboard` package) global hotkey -> short beep (Windows) -> microphone through `hub.wakeword.SoundDeviceSource` (`sounddevice`) -> release / second press /
time limit -> the hub's local Whisper on an in-memory WAV (no cloud, no file) -> post-process (spoken punctuation, capital letters, trailing space, control characters and tabs removed, length cap) ->
typed with key events in 24-character chunks (pynput = SendInput on Windows) -> done chime. Optional clipboard paste when `dictation_use_clipboard` is on.

## Safety and privacy
- Blocklist: the foreground window title plus program name (Windows, ctypes) is checked when you press the hotkey (nothing is recorded), again before typing, and between chunks (typing stops if the window changed). Skipped on other systems, where the title is unknown.
- Enter is never pressed unless `dictation_newline` is on and you said "new line".
- Audio exists only in memory; it is dropped after transcription. Dictated text is never logged, never audited (the audit lines `dictation` and `dictation_assistant` hold a character count), never stored; errors log only the exception type.
- Assistant commands use the same in-process route as the laptop wake listener (`voice: true`, `auto_approve: false`, approvals on the phone); the reply is spoken (or pushed when the laptop cannot speak).
- Lifecycle: `start(ctx)` starts the listener when enabled; shutdown stops it. `python -m hub doctor` shows a `dictation hotkey` line.
- Hotkey keys are not swallowed: the focused app also sees them, so choose a combination it ignores.
