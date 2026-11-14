# Text to speech API

`hub/tts.py` (module `tts` in `FEATURE_MODULES`), browser side `js/hubtts.js`, `js/speechqueue.js`, `js/speechchunk.js` (planner), `js/voicemode.js`, screen `js/voices.js` (Settings > Voices). Guide: `docs/guide/31-voice.md`. All routes need the login cookie. Everything runs on the laptop; spoken text is never logged, audited or used in a file name.

## Behaviour until the owner chooses
Config `tts_enabled` (default `true`) and `tts_engine` (default `browser`). `browser` means the hub makes no audio: `POST /api/tts` answers `204` and the phone's own `speechSynthesis` speaks, as before. The laptop wake word and alerts keep using the old SAPI/`say`/espeak one-liner.

## Engines
| id | what it runs | voices |
|---|---|---|
| `browser` | the phone's speechSynthesis | the phone's own (named in the assistant's `voice.voice`) |
| `sapi` | Windows only: fixed PowerShell argv, text on **stdin**, voice/rate/output path in environment variables (`REDLINE_TTS_VOICE`, `REDLINE_TTS_SAPI_RATE`, `REDLINE_TTS_OUT`) | installed System.Speech voices |
| `piper` | `piper` on PATH, the `piper_exe` setting, or `python -m piper`; `--model <data>/tts/piper/<id>.onnx --output_file <tmp> --length_scale <1/rate>`, text on stdin (flag spelling UNVERIFIED across Piper versions) | the downloaded `.onnx` models |
| `command` | the owner's argv (below) | the owner's commands (voice = command id) |

Kokoro and other ONNX engines are not built in; run them through `command`. Pitch is applied by the browser engine and handed to `command` programs as `REDLINE_TTS_PITCH`; SAPI and Piper use speed only.

### The `command` engine
`{id, name, argv[], output: "stdout"|"file", lang}`. Validation (`clean_command`): argv is a list of at most 24 strings (300 characters, no newline or NUL); the program may not be a shell (`sh bash zsh cmd powershell pwsh wsl ...`); no eval flags (`-c /c -Command -EncodedCommand ...`); the only placeholder is `{out}` (the WAV path), required for `output: "file"` and forbidden for `stdout`; there is no text placeholder, the text only ever arrives on stdin. Run with `shell=False`, 60 s timeout, output must be a RIFF/WAVE file. At most 8 commands.

## Config in the data folder
`<data>/tts/tts.json`: `{enabled, engine, headphones, piper_exe, cache_phrases, commands[]}` (written by `PUT /api/tts/settings` and `/commands`; missing = the config defaults). Per-assistant voices are the assistant's `voice` object (`engine`, `voice`, `rate`, `pitch`; docs/api/assistants.md). Resolution: assistant `voice.engine` -> `tts.json engine` -> config `tts_engine` -> `browser`.

## Routes
- `GET /api/tts/status` -> `{enabled, engine, headphones, silenced, engines[{id,name,available,note}], profiles{assistant_id: {engine,voice,rate,pitch,hub}}, commands[], piper_exe, cache{phrases,bytes,max}, speaking, last_error, max_text, quiet_now}`. `hub` is true when the laptop makes that assistant's audio.
- `GET /api/tts/voices?engine=sapi|piper|command[&fresh=true]` -> `{engine, voices[{id,name,lang}]}` (cached 60 s).
- `PUT /api/tts/settings` `{enabled?, engine?, headphones?, piper_exe?}` -> status. `400` for a bad value.
- `GET /api/tts/commands`; `PUT /api/tts/commands {commands[], confirm: true}` (confirm required: it saves a program the hub will run).
- `POST /api/tts {text, assistant?, engine?, voice?, rate?, pitch?}` -> `200 audio/wav` (`X-TTS-Engine`, `X-TTS-Cached: 0|1`), `204` (browser engine or disabled: use the phone's voice), `423` (panic: say nothing), `502 {detail}` (the engine failed; a plain sentence without the text), `413` over 1500 characters, `400` for a bad body. The overrides are for the Test voice button. Each sentence is cached on disk (`<data>/tts/cache/<hash>.wav`, last `cache_phrases` = 200, 30 days, a hit refreshes it).
- `POST /api/tts/stream` body: lines of JSON `{"text","assistant"?}` -> `application/octet-stream` frames `[kind u8][length u32 big-endian][payload]`, kind `0` WAV, `1` use the phone's voice, `2` silent (panic), `3` error text. Each frame leaves as soon as its sentence is made. Browsers cannot send and read at once over HTTP/1.1, so voice mode posts one sentence per `POST /api/tts` instead (prefetching the next while one plays).
- `POST /api/tts/stop` stops the laptop speaker now.
- `GET /api/tts/piper` -> `{models[], catalog[], job{state,voice,bytes,total,message}, installed_exe, note, warning}`; `POST /api/tts/piper/download {voice, confirm: true, sha256?}` (one at a time; `400` without confirm); `DELETE /api/tts/piper/{voice}`.

### Piper downloads
Only the owner starts one (button + size confirm). URL pattern from the Piper voices repo: `https://huggingface.co/rhasspy/piper-voices/resolve/main/<family>/<lang_REGION>/<name>/<quality>/<id>.onnx` and `.onnx.json` (id like `en_US-lessac-medium`; the catalogue list and size hints are **UNVERIFIED**, any id of that shape works). Checks: https and huggingface.co only, size cap 250 MB, the model at least 1 MB, the JSON must parse and have `audio`, SHA-256 recorded in `<id>.onnx.sha256` and compared to `sha256` when the request gave one; temp `.part` files are removed; nothing is kept on any failure. Stored in `<data>/tts/piper/`. There is no automatic download.

## Streaming speech (browser)
`speechchunk.js createSpeechPlanner` turns streamed markdown into sentences (reusing `splitStream`): code blocks, tables, the tail of lists longer than three items and links are not read ("I put the details on screen." once, links become "a link"), total length capped at 700 characters ("The rest is in the chat."). `speechqueue.js createSpeechQueue` starts making each sentence at once, plays in order, falls back to the phone's voice for a sentence the hub does not deliver within 3 s or errors on, and `cancel()` stops playback and aborts pending requests. The text on screen is not touched.

## Barge-in
Voice mode measures the mic while speaking: after a 0.7 s deaf window it learns the level of the assistant's own sound that leaks back and treats a louder voice for about 0.4 s as the owner talking (0.25 s and a lower bar in headphones mode). Echo cancellation is requested on the stream. Also: the **Stop talking** button, the orb, and the words stop / quiet / cancel / okay okay. A barge-in only stops speech; while a reply is still being generated it also listens on the side: "stop" calls the stop endpoint, any other words become the next request and replace the old turn. Phone loudspeakers can still trigger it (use headphones mode). On the laptop (`wakeword.py`) the owner talking over the speaker stops it only in headphones mode.

## Laptop speaker
`laptop_speaker(ctx, persona)` is the speaker of `WakeListener` and the one alerts use: presence (`hub/presence.py` `current|state|status`, values `in_meeting`/`focus`, guarded: UNVERIFIED API shape) keeps it silent; otherwise `TTS.speak_aloud` splits into sentences, makes the next while the player plays one, and gives anything that failed to the old one-liner. Players: `winsound` on Windows, `afplay`, `paplay`, `aplay` or `ffplay` elsewhere.

## Panic
`panic.register("voice", svc.silence, svc.resume, ctx=ctx)` (guarded: hub/panic.py may not exist). Silence stops the player, refuses synthesis (`423`) and the laptop speaker; the phone sees `silenced` in the status.

## Tests
`tests/test_tts.py`: fake runner, player and clock; the browser queue and planner are run with node (skipped without it).
