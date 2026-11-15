# Local debugging handoff (read this first, no other context needed)

You are a Claude Code session running **on the owner's Windows laptop** inside `D:\laptop-hub`. You have the real machine, so you can finally do what the cloud sessions could not: run the hub, read real logs, install packages, and reproduce bugs. Your job: **find and fix real bugs, in rounds, with the owner testing on their phone.** Keep your messages short (the owner is token-sensitive, speaks informal English, prefers brief answers).

## 1. What this project is
REDLINE (repo `ShikharGupta0813/laptop-hub`, branch `main`): an installable Android PWA + native Capacitor shell (`mobile/`) + a FastAPI "hub" (`hub/`) that runs on the owner's Windows laptop (i7-1355U, 32 GB, Intel Iris Xe, **no NVIDIA GPU, CPU only**) and is reached from the phone over **Tailscale**. It is a personal Jarvis/Friday-style agent: assistant with tools, voice, wake word, mail/calendar/tasks, job applications queue, study tools, remote screen, playbooks, computer control, meeting assistant, etc.

Owner: Shikhar Gupta (GitHub ShikharGupta0813). Personal setup only. **Never touch**: the `AI-Task-Agent` repo, any work/OneStack folder or account, work Notion/mail. Do not write to Notion at all. Never paste secrets (login token from `python -m hub token`, ntfy topic, API keys) into chat or commits.

## 2. How to run (Windows PowerShell, repo root)
```powershell
.venv\Scripts\activate          # the hub MUST run from the venv python; earlier installs went to system Python by mistake
python -m hub doctor            # checks what is missing
python -m hub serve             # start hub (127.0.0.1:8765; Tailscale serves HTTPS in front)
python -m hub token             # prints a new login token (logs phones out; do not paste it anywhere)
```
- Config: `%USERPROFILE%\.laptop-hub\hub.json` (or `HUB_CONFIG`); data dir `%USERPROFILE%\.laptop-hub\data` (audit log, tokens, caches: contains secrets, never commit or paste).
- A one-shot installer is being written: `scripts/setup-all.ps1` (creates the venv, installs every requirements file, Playwright Chromium, Piper/SAPI voice, checks Ollama/Tailscale/agy). If it exists, run it first.
- Requirements files: `requirements.txt`, `-voice`, `-work`, `-laptop`, `-extra`, `-screen`. Pin `av==16.1.0` for faster-whisper.
- Tests: `python -m unittest discover -s tests -t .` takes ~9 minutes (about 3,850 tests, all fakes). Run focused modules while iterating, for example `python -m unittest tests.test_tts tests.test_fixit`. The full run must be green before you push. If it exceeds a tool timeout, run in 2 to 3 slices by module name.
- JS has no bundler: ES modules under `hub/static/js`. `node --check file.js` for syntax. Playwright is the way to see the UI at 390px.
- When static files change, bump `const VERSION` in `hub/static/sw.js` (currently `v34-chat-agy-2026.11`), and new static files must be listed in `SHELL` there.

## 3. Architecture in one page
- `hub/server.py`: builds the app. **Feature-module pattern**: each module exposes `make_router(ctx)` and `start(ctx)` and is named in `FEATURE_MODULES` (tuple extended by appended `FEATURE_MODULES = FEATURE_MODULES + (...)` lines). Assistant tools are `ExtraTool` objects added in the big `extra_tools=` expression.
- `hub/assistant.py`: the tool-using assistant. `TOOL_GROUPS` regexes pick which tool groups to offer. `hub/safety.py`: levels N1/N2/N3; `safety.ALWAYS_ASK` (send_email, send_application, post_publish, submit_form, chat_send, computer_run, batch_send ...) are never auto-approved. Taint gate for untrusted content. `hub/panic.py` (`panic.register(name, stop, resume, ctx=)`, `panic.on(config)`, persisted `config.panic_active`), `hub/trust.py`, `hub/auditview.py`, `hub/presence.py` (delivery rules by where the owner is).
- Providers: `hub/providers.py` (auto/local/claude/claude-login/gemini/cli:<id>), `hub/provchain.py` (fallback chain + fix-it messages), `hub/cliprov.py` (CLI-login list; **Antigravity `agy -p` is the default now because Google ended personal Gemini CLI sign-in**; prompt goes on stdin), `hub/endpoints.py`, `hub/lanes.py` (work lanes: routine work to local/Council first), `hub/models.py` (Ollama catalogue), `hub/fixit.py` routes under `/api/fixit/*` (provider check, checklist, live log).
- Voice: `hub/tts.py` (engines: browser, sapi, piper, command; per-sentence cache; `/api/tts`; **panic hook sets `svc.silenced`**), `hub/wakeword.py` (laptop mic), `hub/dictation.py`, JS `voices.js`, `hubtts.js`, `speechqueue.js`, `speechchunk.js`, `wake.js` (browser SpeechRecognition wake word), `voicemode.js`.
- Opportunities/applications: `hub/opps.py`, `oppsources.py`, `oppsnorm.py`, `opps_vet.py`, `profile.py`, `jobprefs.py`, `batches.py`, `applyq.py`, `formfill.py` (Playwright, stops before Submit), `tailor.py`, `factbase.py`, `fitcheck.py`, `answerbank.py`. Rules: nothing applies/sends without an owner tap; never automate LinkedIn/Indeed.
- Alerts/mail: `alerts.py`, `gmailx.py`, `mailaccounts.py`, `asana.py`, `tasksources.py`, `calendars.py`, `chatchan.py` (Google Chat for a personal account: notification capture, reply by clipboard + open Chat), `posts.py`/`destinations.py`.
- Other: `playbooks.py`, `computeruse.py` (off by default), `meetings.py`, `mcpclient.py`, `studylib.py` + `study.py` + `kb`, `screen.py` (remote screen; `/api/screen/*`), `ticker.py` (+ `js/poller.js`: one shared poll `/api/ticker`).
- Android: `mobile/` Capacitor project. Native pieces under `mobile/android/app/src/main/java/dev/shikhar/laptophub/`: `wake/` (Vosk foreground-service wake word, `WakeService`, `HubWakePlugin`), `capture/` (notification capture, `ChatLink`). The APK is built by GitHub Actions ("Build Android APK"); the owner must **uninstall the old APK first**. Locally you can only run the web side; do not try to build the APK on Windows unless asked.
- Docs: `HANDOFF.md` (feature-by-feature notes), `docs/guide/NN-*.md` (owner guide pages, numbered), `docs/api/*.md`.

## 4. Known real-device problems (from the owner's phone, 2026-11-15). Fix these first.
Evidence comes from the owner's screenshots and hub log. A cloud session had no device access, so everything below is **unverified in a real run**; reproduce first.

1. **Friday/Jarvis never speak.** The Voices/Friday screens show "Voice is paused by the panic button" although the owner says panic was not pressed (they tapped it once to test, then Resume; the top-bar red square is just the panic button's normal look). `hub/static/js/voices.js` maps ANY bare HTTP 423 to that message; `hub/tts.py` returns 423 when `svc.silenced` is true (set by `panic.register("voice", svc.silence, svc.resume)`). Suspect: `silenced` not cleared on Resume or after restart with `panic_active` persisted, or set without panic. Make TTS read panic state live, return a JSON `detail` with the real reason (panic / voice off / quiet by presence), and offer a Resume button in the UI.
2. **"This phone has no voice to read with"** (Android WebView often has no speechSynthesis voice) and the Voices sheet says **Piper is not installed** (`pip install piper-tts`) although a voice (`en_US-lessac-medium`) was downloaded, so laptop voice fails and the fallback phone voice does not exist, giving silence. Desired chain on Windows: Piper, then SAPI (needs nothing extra), then browser voice, and the hub should be able to synthesise WAV and stream it so the phone needs no voice. Verify end to end for Friday, Jarvis and Talk, including Android autoplay unlock.
3. **Constant tapping/"dot" sound** on the phone while the app is open. Suspects: `hub/static/js/wake.js` (browser `webkitSpeechRecognition` restart loop; each restart beeps in Android WebView) running alongside the native wake service; `WakeService.java` notification channel created with `IMPORTANCE_HIGH` (around line 124); any other beep. Native shell should never use the in-page wake word by default, channels other than real alerts must be silent.
4. **Provider strip at the top of Talk overlaps** itself ("Local ude lo Geminijde+Gem..."): unreadable.
5. **The chips under the chat (Auto, Council, Local model, Local deep thinking, Gemini, Claude, Gemini CLI)** look like a model switch but are the **work lanes** picker: they hand one message to a lane and show the answer in a popup sheet "Work lane result". The owner wants a real "AI for this chat" switch whose answers appear **in the chat**, and the answering provider shown under each reply. Council currently fails with "Council is not set up on this laptop" (it is optional; show a how-to instead).
6. **Stray `null` text** in Settings > Voices ("nullnull", "null" after "Add command"). Scan other screens for printed `null`/`undefined`.
7. **Many "needs optional packages / not installed" messages** (dictation needs `pynput`, `sounddevice`; Piper; Playwright browser; Tesseract optional; Council). Root cause: installs went to system Python instead of `.venv`. Fix with the setup script and make messages say `run scripts\setup-all.ps1`.
8. **Gemini auth:** Google ended Gemini CLI personal login ("migrate to Antigravity"). `agy` works: `agy -p "<prompt>"` prints one answer non-interactively (`agy --help` confirmed flags: `--print/-p`, `--model`, `--output-format text|json|stream-json`, `--input-format`, `--effort`). **Unverified:** whether `agy -p` reads the prompt from stdin; if not, pass the prompt as the argument. The saved CLI list on the owner's hub may still hold the old `gemini-cli` entry (it answered "Hi!" once in the lane sheet as `cli-gemini-cli`), so edit entries via Settings > Providers > CLI login or fix migration.
9. **Calendar:** `GET /api/work/calendar` returns 503 (Google not connected); Home now shows a "connect" hint and caches that state. Verify it is calm and not spammy.
10. **Remote screen:** a Keyboard button, key bar and text box were added; **not tested on a real phone** (Gboard composition/backspace, pyautogui typing on Windows). **Explain this screen** needs a vision provider: Ollama vision model (for example `moondream`) or Gemini; "This app screen" mode works from text.
11. **Back button / smoothness** were reworked (history stack in `navstack.js`; one shared poller; removed blur layers). Re-verify on the phone: Back goes one page, Home needs a second Back within 2 s to exit; scrolling smooth.
12. Owner reported earlier (before fixes): Jarvis screen had no working text box (now always usable), laptop terminal blank (a startup banner and `hub-console.log` were added), "Gemini unavailable" (provider chain now tries CLI logins then local).

## 5. Unverified areas (everything was tested only with fakes)
Windows idle APIs, global hotkeys, SendInput/pyautogui typing, loopback audio, Piper, Android Vosk wake word and Gradle build, live job-site endpoints (only Lever was checked against docs; Greenhouse/Ashby/Workable/SmartRecruiters/Recruitee/Teamtailor/remote boards marked "unverified"), Jira/Trello/Linear/GitHub/Hashnode/WordPress/Ghost APIs, IMAP providers, Gmail OAuth, Google Chat email/notification shapes, Council runs through lanes, DeepSeek speed, OCR, real Ollama embeddings, real browser rendering beyond Playwright 390px smoke.
Known unfixed: `stop()`/restart of the same app object in one process fails in some modules (council, lanes, watch, autopilot, friday, automations); Build flow with a non-Council lane does not tick the plan step; deep thinking is not a per-screen "think harder" switch; formfill cannot read iframes/shadow DOM.

## 6. Hard rules (do not break)
- Nothing sends/applies/submits/publishes automatically; every outward action waits for the owner's tap (`safety.ALWAYS_ASK`). Full Control needs the typed phrase `I UNDERSTAND`. Voice cannot approve N3.
- No automation of LinkedIn or Indeed (prepare + copy + open link only). Stop at CAPTCHAs and forced logins.
- Never invent facts about the owner in resumes/answers (fact-locked: `factbase.py`, `fitcheck.py`).
- Company mail bodies are not sent to cloud AI by default.
- Voice and text parity for assistant tools. Make everything configurable lists, no hard-coded examples.
- Untrusted content (web pages, mail) cannot trigger tools on its own (taint gate).
- Do not print or log typed keystrokes or tokens.

## 7. Working method for you
1. `git pull`, activate venv, run `python -m hub doctor` and fix what is red (or run `scripts\setup-all.ps1`).
2. Start the hub with `python -m hub serve` in a visible terminal; read the console and `hub-console.log` (the startup banner lists URL, auth, voice engine, providers, features).
3. Reproduce bugs 1 to 6 on the laptop first (`/api/tts` with curl, Voices sheet in a desktop browser at `http://127.0.0.1:8765`), then ask the owner to retest on the phone.
4. Fix at root cause, add a regression test, run the focused tests, then the full suite before pushing.
5. Commit style: end messages with the two trailer lines
   `Co-Authored-By: Claude <noreply@anthropic.com>` and a `Claude-Session:` line if one is provided; use `--author="Shikhar Gupta <ShikharGupta0813@users.noreply.github.com>"` so the owner appears as author (committer email must stay `noreply@anthropic.com` or commits show as Unverified). Push to `main` only when the full suite is green.
6. Do round-by-round: list what you fixed, what still needs the owner's phone, and the exact thing for them to tap. Keep it under 150 words.

## 8. Not built yet (owner knows; wait for their testing first)
Application pipeline tracking and follow-up drafts, learning loop on outcomes, networking/outreach drafts, interview/offer coach, a setup checklist screen, Wake-on-LAN (needs another always-on device).
