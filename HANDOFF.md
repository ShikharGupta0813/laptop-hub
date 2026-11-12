# Laptop Hub: handoff

**This project lives at https://github.com/ShikharGupta0813/laptop-hub (branch `main`).** (It was first built inside
`ShikharGupta0813/AI-Task-Agent`, folder `laptop-hub/`, branch `claude/clever-dirac-r85yh2`, and split out with its history.
Use the new repo from now on.)

Read this first when starting a new chat. It says what the project is, what is finished, what is
still to do, how it was built, and the exact words to paste to continue.

_Last updated: 2026-11-04. round 2 complete: backends, design system, shell and all phone screens built and tested against fake data; see 4a for what is unverified._

## 1. What this is

One app on the owner's Android phone that works with their Windows laptop:

- **Talk**: speak or type to an assistant that acts on the laptop (files, notes, commands).
- **Apps**: a searchable directory of every feature, incl. Jarvis control and laptop actions (lock, screenshot, processes, open apps, media keys, clipboard...)
- **Work**: Asana tasks, calendar, send email, post to Google Chat (reuses the owner's existing
  `jarvis-voice-assistant` setup and their Asana token).
- **Learn**: chat with notes/documents, quizzes (from notes, a document, a web page or any topic),
  and an **English fluency coach**.
- **Notes**: voice notes saved as Markdown with titles and tags.
- **More**: Files (read-only reader), Terminal (real shell), Hub (status, buttons, upload).

Every AI feature has a **Local (Ollama) or Claude** switch.

The owner (Shikhar) asked for full access to their own laptop from their own phone and accepts the risk.
Safety design: approvals on by default, token login, Tailscale only, audit log (section 7).

## 2. Architecture

```
Android app (Capacitor shell, or the installed web app)
        │  HTTPS (Tailscale serve)
        ▼
Laptop Hub  (FastAPI, 127.0.0.1:8765, on the Windows laptop)
   ├─ hub/server.py        routes, login, security headers, terminal websocket
   ├─ hub/assistant.py     tool-using assistant (Claude or Ollama), approvals
   ├─ hub/llm.py           Ollama + Claude clients, prompts
   ├─ hub/files.py notes.py speech.py terminal.py config.py auth.py
   ├─ hub/static/          the phone UI (vanilla JS modules, no build step)
   ├─ hub/work/ study.py english.py laptop.py jarvis.py   feature routers (make_router(ctx), see hub/common.py)
   └─ hub/devserver.py     fake models + sample data for UI work
Ollama (local models)   faster-whisper (offline speech)   Jarvis modules (keyring secrets)
```

Key decisions and why:

- **No build step, vanilla JS.** Each tab is `hub/static/js/<tab>.js` exporting `init(container, ctx)`.
  Easy for any agent or person to edit; libraries are vendored in `hub/static/vendor`.
- **Python standard-library style backend + FastAPI.** One process, runs on Windows directly (not WSL)
  so it uses all RAM, reaches Windows files, and can open PowerShell and WSL terminals.
- **Tailscale `serve` gives HTTPS.** Android needs HTTPS for the microphone and for installing the app.
  The hub itself only listens on `127.0.0.1`.
- **Cookie login, SameSite=Strict.** A bundled-assets native app on another origin would not send that
  cookie, so the native shell loads the hub URL directly (Capacitor `server.url`), same origin.
- **Models:** local model via Ollama (default `qwen3:4b`, CPU-only laptop so keep it small), Claude for
  hard work. The assistant uses `claude-sonnet-5-5`; "Ask Claude" uses `claude-opus-5-5`.
- **The model never decides safety.** Approval gating, path limits and protected files are code.

## 3. Status: finished (tested with fake models and a fake operating system, in a Linux container)

| Part | Evidence |
|---|---|
| Server: login, lockout, cookies, headers, audit log, websocket refusal codes | `tests/test_server.py` |
| Files (read-only, folder-jailed), upload to inbox | `tests/test_files.py` + UI run |
| Notes: voice + text, background title/tags, edit | `tests/test_notes.py` + UI run |
| Ask (Explain/Summarise/Quiz) over SSE | `tests/test_llm.py` + UI run |
| Terminal (POSIX pty) + Terminal tab | server tests + UI run |
| Assistant backend (tools, approvals, limits) + Talk tab | `tests/test_assistant*.py` + UI run |
| Assistant tools for Work, laptop actions, Jarvis, English, quizzes (call the hub's own routes in-process) | `tests/test_assistant_tools.py` |
| Work: Asana, calendar, email (draft then send once), Google Chat; Work screens | `tests/test_work.py` + UI run |
| Learn: sources (note/file/PDF/URL with SSRF guard/text/topic), chat, quizzes, history; screens | `tests/test_study.py` + UI run |
| English coach: speaking analysis (fillers/WPM/pauses computed in code), role-play, correction, vocabulary SRS, progress; screens | `tests/test_english.py` + UI run |
| Laptop actions (26: lock, screenshot, processes, open url/app, clipboard, media keys, power...) + generic runner | `tests/test_laptop.py` + UI run |
| Jarvis control (start/stop one process, log, remote command queue) + screen | `tests/test_jarvis.py` + UI run |
| Apps directory (47 entries, search, favourites, recents), navigation Talk/Work/Learn/Notes/Apps | browser run |
| Security review (Opus) and fixes, with regression tests | `tests/test_security.py` |
| Android shell (Capacitor) in `mobile/`, launcher icon, cloud build workflow | cloud build verified (run #2, 3.7 MB APK, placeholder address); not installed on a phone |

1,337 unit tests passed on the last full run (416 before round 2) (`python -m unittest discover -s tests -t .`).

## 3b. Round 2 (finished backends, design system, assistant tools) - 1,337 tests pass

Conventions for every module: `docs/ROUND2_BRIEF.md`. Route docs: one file per module in `docs/api/*.md`.
Each module is `hub/<name>.py` with `make_router(ctx)` (+ optional `start(ctx)` loop, `DEV_FAKES`), listed in
`FEATURE_MODULES` in `hub/server.py`. Demo data is seeded when `HUB_DEMO=1` (the devserver sets it).

| Module | What it does | Tests |
|---|---|---|
| `kb` | Ask my files: SQLite FTS5 + optional Ollama embeddings, cited streaming answers | `test_kb` |
| `fileops` | Write side of Files (writable roots only, etag conflicts, trash, upload, zip) | `test_fileops` |
| `notify` | Notification center + SSE + optional ntfy phone push; replaces `ctx.notify` | `test_notify` |
| `reminders` | Reminders/alarms/timers, natural-language quick add, scheduler | `test_reminders` |
| `inbox` | Read-only Gmail (IMAP), summaries, reply drafts (never sends), digest, daily briefing | `test_inbox` |
| `watch` | Monitor Gmail + Google Chat with rules and AI "urgent" detection; chat arrives from the Android listener via `POST /api/watch/ingest` | `test_watch` |
| `council` | Background side jobs that run the separate `multi-llm-lab` CLI (`python -m lab council --json`): one at a time, timeout, jobs persisted in `<data>/council/jobs`, notify on finish, review packet + rerun with notes (stdin), per-model on/off (`lab models`), assistant tools `council_run/status/cancel` in `hub/council_tools.py`, Apps > Council (`js/council.js`), wizard step `council`. Feature is off while `council_dir` is empty. See `docs/api/council.md` | `test_council` |
| `screen` | Live remote screen + opt-in remote control (armed for N minutes, allow-listed keys) | `test_screen` |
| `productivity` | To-dos, focus timer, habits, journal | `test_productivity` |
| `life` | Bookmarks, snippets/clipboard, expenses (+ UPI auto-capture), countdowns, calculator/units/world clock | `test_life`, `test_life_capture` |
| `automations` | Routines of steps, manual or scheduled, dangerous steps gated | `test_automations` |
| `cards` | Flashcards (SM-2), decks, import/export, AI roadmaps | `test_cards` |
| `settings` | Settings (allow-listed keys only), models, diagnostics, audit viewer, backups | `test_settings` |
| `search` | Global search fan-out + `GET /api/home` dashboard data | `test_search` |
| `assistant_tools_more.py` | 18 assistant tools for the above (reads free, writes ask first, no deletes/sends, mail bodies never shown) | `test_assistant_tools_more` |

UPI spend capture (the owner's request): the Android app (`mobile/android/.../capture/`, Java) reads payment-app
notifications (GPay, PhonePe, Paytm, BHIM, CRED, bank apps) and optional bank SMS, filters debits locally, and POSTs
to `/api/expenses/capture`. The hub parses it (`hub/life.py`), de-duplicates, stores a "pending" item, notifies, and the
owner picks a category; it remembers merchant -> category. Same listener also forwards Gmail and Google Chat
notifications to `/api/watch/ingest` once the owner turns monitoring on (`HubCapture.setWatch`). JS API on
`window.Capacitor.Plugins.HubCapture`: `status, openListenerSettings, requestSms, setEnabled, setWatch, flush, test`
(see `docs/api/capture-android.md`). **The Android code has never been compiled** (no SDK in the build container):
the first GitHub Actions run is the real compile. Filter logic is checked by `node mobile/scripts/test-capture-filter.js`
and `test-watch-filter.js`.

Design system (done): `docs/DESIGN.md`, `hub/static/css/design.css` (tokens, 3 themes Midnight/Daylight/Aurora, 6
accents), `js/ui.js` (sheet, toast, confirm, segmented, ring, ...), `js/icons.js` (127 icons), shell in `js/main.js`
(bottom nav Home|Talk|Work|Learn|Apps, top bar with search/bell/settings, view registry `VIEWS`), new login,
Home dashboard (`js/home.js`), global search overlay (`js/omnibox.js`).

## 3c. Update button (2026-11-12)

`python -m hub update [--restart]` (hub/updater.py) pulls `git pull --ff-only` in the repo (refuses on a dirty tree, pip only if requirements.txt changed) and restarts the `LaptopHub` task, logging to `~/.laptop-hub/update.log`. `python -m hub install-updater` (also offered by `setup --only autostart`, step `updater`) registers the on-demand task `LaptopHubUpdate` and saves the `update` quick button (`schtasks /Run /TN LaptopHubUpdate`). `/api/status` has `version` (short commit); `/api/update-log` returns the last 20 log lines. sw.js VERSION v13-update-button-2026.11. Unverified: real Windows scheduled-task behaviour (tests use fakes only).

## 3d. Council (multi-llm-lab side jobs)
Built against an interface the lab teammate is still adding, with fakes only (plus a stand-in `lab` package run by the real
process runner in `tests/test_council.py`): `council --json [--only= --skip= --resume= --notes -]`, `review-packet`, `models
--json|--enable=|--disable=`. **Unverified against the real lab**: flag spellings (comma-separated `--only=`, `--resume=`,
`--enable=NAME`), the `models --json` field names, and that `run_dir` is an absolute path. Not tried on Windows or with real models.
Claude is never used unless `council_allow_claude` is set by hand in hub.json. A crash (not a clean stop) can leave an orphan lab
process; the job is shown as `interrupted` and a new run is allowed.

## 4. Still to do (in this order)

### 4a. Phone screens for round 2: BUILT (checked with Playwright against the devserver, Midnight + Daylight)

Done: Talk (+ hands-free voice mode), Work, Inbox, Reminders, Learn, Flashcards + roadmaps, Ask my files, English, Notes, To-do,
Habits, Journal, Focus, Files manager + editor, Settings, Notifications, Watch, Spending (incl. the "Needs a category" UPI list),
Remote screen, Automations, Apps directory (67 entries), Tools, Read-later, Terminal/Laptop/Jarvis restyle. `sw.js` caches every
screen (VERSION v6-screens-2026.11). Per-screen screenshots were reviewed by the building agents; none of it has run on a phone.

Known unverified bits (from the agents' reports): Focus timer reaching zero and break countdown; delete confirms for habits,
journal, reminders, flashcards, bookmarks; Tools/Read-later actions beyond rendering; opening a citation from Ask my files; TSV
import/export and roadmap generation; Inbox "not configured" state and older-mail paging; Remote screen touch gestures, MJPEG
stream and real vision; real Android plugin calls from Settings > Phone capture; Terminal typing; English with real mic/Claude;
spoken "stop"/"cancel" in voice mode; on-screen-keyboard layout. Tools: switching sections via the URL hash alone does not open
that section.

DONE since: second security review (8 findings fixed, tests in tests/test_security_round2.py; `POST /api/logout-all` revokes all sessions) and README/SETUP-WINDOWS.md/API index. Review leftovers: backups include notify_settings.json (ntfy topic); unbounded page memory for a logged-in phone; concurrent logout-all + settings saves could lose a write. (Old list:) (2) update README (feature list, new setup steps:
`pip install -r requirements-screen.txt` optional, `ollama pull nomic-embed-text`, ntfy topic) and index `docs/api/*` in
`docs/API.md`; (3) rebuild the APK in GitHub Actions (first real compile of the Android capture code; fix any Java errors);
(4) real-laptop run (item 1 below); (5) real-phone checks of the unverified list above.

### 4a-2. Added after first real run (2026-11-07)
First real run worked on the owner's laptop (Windows, D: drive) and phone: app loads over Tailscale, Terminal/Talk/Learn/Files screens render.
Fixes made from that run: Local assistant speed (tool routing, small prompt, keep_alive, warm-up at startup), terminal touch scroll +
copy output, Files open/download (Java DownloadListener in `mobile/.../HubWeb.java`, text preview for PDFs in the app).
New: **Friday** (`hub/friday.py`, `hub/friday_tools.py`, `js/friday.js`, voice persona in `js/voicemode.js`): spoken daily brief from
mail headers/Asana/calendar/reminders/alerts, plan the day, laptop-prep list approved by the owner before anything opens,
proactive "you were tagged" alerts (off until enabled). Limits: always-listening "Hey Friday" is NOT built (needs a native
foreground service + on-device wake word); "tagged" = comments by others on the owner's open tasks; Asana priority is not read.
Owner has no Jarvis folder; calendar shows 503 until Jarvis/iCal is configured. Android build run #3 succeeded (capture code compiles).

### 4a-3. Added 2026-11-07/08 (after the first real run)
- **REDLINE**: the app is renamed (title, manifest, Android app_name) and restyled to the owner's GitHub/portfolio identity: near-black
  `#08090d`, crimson `#ff5368`, amber, JetBrains Mono, window-chrome cards, `01 / LABEL` eyebrows. Themes: redline (default), slate, paper.
  Portfolio tokens were read from https://portfolio-nu-eight-vlznys99og.vercel.app/ (the session's network had to allow that host).
- **Display name**: `config.display_name` (default "Chief"), set by the wizard or Settings; used by the Home greeting and as Friday's
  default way to address the owner.
- **`python -m hub setup`** (hub/setup_wizard.py): interactive wizard for Jarvis folder clone, Gmail app password (keyring), Asana token,
  calendar iCal URL, Google Chat webhooks, AI step (Gemini key in keyring, Claude login check, default provider; replaced the Claude key step), ntfy topic, Cloudflare WARP script, auto-start task, power
  settings. Secrets only via getpass into the existing stores; never printed or logged. Run `--only gmail,asana` or `--check`.
- **AI providers without an API key (2026-11-07)**: `local | claude | claude-login | gemini`. `hub/llm.py` has `ClaudeCliClient` (runs `claude -p
  --output-format text --tools "" --no-session-persistence`, prompt on stdin, temp cwd, ANTHROPIC_API_KEY/AUTH_TOKEN stripped so the subscription
  login is used; retries once without optional flags on "unknown option") and `GeminiClient` (REST, `x-goog-api-key` header, SSE stream; key from
  `GEMINI_API_KEY` or keyring `jarvis-gemini`/`api_key`). `hub/providers.py`: availability + `default_provider` ("auto" = API key > Gemini >
  Claude login > local). `server.model_for` resolves "auto"; `/api/status` and `/api/settings` return `providers` and the default. Routine jobs
  (Friday brief_model now "auto", note labels, quick Ask) use the default; claude-login is meant to be picked by hand for heavy work (shared
  subscription quota). The assistant runs claude-login/gemini through `_text_turns` (tools described in the prompt, `<tool_call>{json}</tool_call>`
  parsed back; same approvals). Front end: `js/providers.js` (`providerSegmented`, options shown only when ready; `ui.segmented().setOptions`).
  Wizard: step `ai` (alias `claude`) and `gemini`; doctor and Setup checklist report "AI ready". `tests/helpers.make_config` sets
  `default_provider="local"` so tests never reach a real AI. UNVERIFIED on a real laptop: the live `claude -p` flags/errors on Windows (`claude.cmd`),
  real Gemini calls, tool-calling quality of the text protocol.
- **Duo provider "Claude + Gemini" (2026-11-12)**: `hub/duo.py` (`Supervisor`, `DuoClient`). Claude = `claude-login` CLI client is the planner/checker, Gemini the worker.
  `assistant._text_turns` for provider `duo`: if `needs_plan(text)` (>12 words, a TOOL_GROUPS match or a tool verb) ONE Claude call returns a <=120 word plan, injected
  into Gemini's system prompt ("Supervisor plan — follow it"); tools the plan names are added to the routed set. Gemini runs all turns. If `config.duo_verify` (default True)
  and Gemini ran tools, ONE Claude review (<=80 words, `OK` or `PROBLEM: ...`) and at most ONE Gemini correction turn. Plain `complete()/stream()` go to Gemini only.
  Quota safety in `Supervisor.ask`: `duo_max_claude_per_hour` (20; 0 = off), 25 s timeout, failures count as calls, any LLMError/timeout -> cooldown (usage limit 30 min,
  not logged in / missing CLI 10 min, other 5 min) and the request continues with Gemini alone. `/api/status` has `duo` {paused, reason, note, calls_last_hour...} and
  `providers.duo.note` ("Claude supervisor paused: usage limit"). Duo is ready when Gemini is. `auto` never picks duo. Settings accept `default_provider=duo`, `duo_verify`,
  `duo_max_claude_per_hour`. Text-tool protocol tightened (JSON example, one call at a time, malformed/cut-off call -> repair reminder, max 2). GeminiClient 404 fallback
  prefers the best Pro when the configured name contains "pro", else Flash. Wizard recommends duo when Gemini + Claude CLI are ready. Tests: `tests/test_duo.py`.
  UNVERIFIED: real `claude -p` plan/review latency and quality, real Gemini Pro tool-call compliance, Windows CLI behaviour.
- **Setup checklist** screen (`js/setup.js`) + Home card: live status of every integration with fix steps.
- **Workspaces + Launcher** (`hub/workspaces.py`, `js/launcher.js`, Remote screen): typed multi-step workspaces (script/url/app/folder/file/wait),
  Start Menu app discovery, a "Whole laptop" tile, window switcher; `python -m hub add-script NAME --label L -- ARGV...` registers a
  laptop command (scripts are not editable from the phone by design). NiFi-over-company-Cloudflare-VPN example in SETUP-WINDOWS.md 9b.
- **Friday**: @mention detection (name + gid), followed tasks, Asana Priority field, alert batching; voice persona and spoken alerts.
- **Fixes from real use**: Local-model speed (tool routing, short prompt, keep_alive, warm-up), terminal touch scroll + reconnect banner,
  Files open/download (HubWeb.java DownloadListener) and Back handling, double-tap guards (`ui.busy`, sheet actions), "Log out of all
  devices" (Settings > Account & security, `POST /api/logout-all`).
- Android build runs #3 and #4 succeeded; rebuild after the REDLINE rename/icons (Actions > Build Android APK, hub_url =
  https://shikharonestack.tail3ffc35.ts.net).
- Owner environment: Windows, repo at D:\laptop-hub, Tailscale address above, no Jarvis folder yet (wizard offers to clone it).
  Still unverified on real hardware: Start Menu app launch, WARP connect command, Gmail/Asana/calendar with real accounts, UPI capture,
  Android notification access, voice mode on the phone, remote-screen touch control.

### 4b. Older items

1. **Run it on the real laptop** (nothing below was ever run on Windows or with real models): follow README.md,
   then `python -m hub doctor`. Fix whatever the first real run shows; expect small Windows fixes (terminal ConPTY,
   process kill, lock/volume/screenshot calls, Jarvis launch).
2. **Get the APK**: GitHub builds it in the cloud. Repo > Actions > "Build Android APK" > Run workflow > paste the hub's
   https address (`tailscale serve status`) > download the `laptop-hub-debug-apk` artifact. Details: `mobile/README.md`.
   Install it on the phone (uninstall any older build first: each build has a fresh debug signing key).
3. **Apply the optional Jarvis patch** (`docs/jarvis-remote-inbox.md`) so the phone can send Jarvis text commands. Untested.
4. Remaining items from the security review (all lower priority): server-side **session revocation** (a generation
   counter in hub.json; today `python -m hub token` rotates the secret and logs everyone out, sessions last 14 days);
   **pin dependencies with hashes**; close the DNS-rebinding gap in the quiz URL fetch (connect to the vetted IP);
   consider blocking "read sensitive file then send email" in one assistant turn; check whether `tailscale serve`
   forwards `X-Forwarded-Proto` (the cookie's `Secure` flag depends on it) and what `Host` it sends.
5. Native extras: background wake word ("Hey Jarvis") plugin, push notifications for approvals, splash screen,
   a release-signed build (keystore in GitHub secrets) so updates install over the old app.
6. Optional: read-only inbox via IMAP, Learn streak should count from yesterday like English does, assistant-made
   notes are not auto-titled, "Log out everywhere" button.

## 5. Known gaps and honest limits

- Everything was tested **in a Linux container with fake models and a fake OS layer**. Not tested on: the real
  Windows laptop, real Ollama, real Whisper, real Claude API, real Asana/Gmail/iCal, real Android.
- Windows-only code (ConPTY terminal, process kill, lock/sleep/volume keys, screenshot, Jarvis launch, UNC path
  behaviour) has never run on Windows.
- The APK has **never been installed on a real phone**. The build runs in GitHub Actions (`.github/workflows/android-apk.yml`) and has succeeded once with a placeholder address.
- Pronunciation feedback is only Whisper word confidence (a hint, not a grade).
- Quiz "from the internet" = a web page you give (SSRF-guarded) or a topic from the model's own knowledge. No live web search.
- `run_command` (assistant) can still touch the hub's own config once the owner approves it.
- **Full access is deliberate**: the terminal, assistant and laptop actions can do anything the owner can. The safeguards
  are the login token, Tailscale-only access, the host allow-list, the `X-Hub` header on every changing request,
  approval cards that show the whole action, and the audit log. Turning on "run commands without asking"
  removes the approval step for that session.

## 6. How it was built (so you can continue the same way)

- **Orchestration:** one main session designed the contract, then parallel agents built separate files:
  Opus for the assistant backend and security review, Sonnet for each UI tab. Each agent owned only its
  files and tested with Playwright against `python -m hub.devserver` (fake models, token `devtoken`).
- **Contract first:** `docs/API.md` is the single source for routes and event shapes. Update it with any
  route change. UI modules only use `js/api.js` helpers and the `ctx` from `js/main.js`.
- **Tests are not decoration:** safety rules got mutation checks (break the rule, see a test fail).
- **Rules the agents followed:** no `innerHTML` with untrusted text (markdown only via
  `renderMarkdown`, which sanitises), 44 px touch targets, no horizontal scroll, dark+light CSS variables.
- Commands: `python -m unittest discover -s tests -t .` · `python -m hub.devserver --port 8799` ·
  Playwright (node) for UI checks: `chromium.launch()`, viewport 412x915.

## 7. Safety model (do not weaken without the owner's say-so)

- Login token (only its hash is stored), 5 failures lock login for a minute, signed HttpOnly cookie.
- Hub listens on 127.0.0.1 only. Tailscale is the only way in.
- Terminal and assistant are full access; **approval cards are on by default**, auto-approve is per
  session only and off after restart.
- Files tab is read-only and limited to chosen folders; HTML/SVG are served as text with a sandbox CSP.
- Assistant: file contents, command output and notes are data, never instructions; cannot write
  `~/.laptop-hub` or the config/audit paths with `write_file`.
- Audit log `~/.laptop-hub/audit.log`: logins, terminal open/close, quick-button runs, assistant requests
  and approvals. It does not record terminal keystrokes.

## 8. Resume in a new chat: paste this

> I'm continuing my project `ShikharGupta0813/laptop-hub` (branch `main`). Read `HANDOFF.md` and `README.md`,
> run the tests (`python -m unittest discover -s tests -t .`), then continue with the first unfinished item in
> section 4 of HANDOFF.md. Keep HANDOFF.md up to date and push often. I give you permission to use my GitHub
> account to push to that repo. Don't ask me about credits.

Working with the repo from scratch on a new machine:

```bash
git clone https://github.com/ShikharGupta0813/laptop-hub && cd laptop-hub
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-work.txt -r requirements-laptop.txt
python -m unittest discover -s tests -t .           # 1,300+ tests, no models or network needed
python -m hub.devserver --port 8799                 # fake models and a harmless fake OS; token: devtoken
```

## 9. Related repos

- `ShikharGupta0813/jarvis-voice-assistant`: the Windows desktop assistant (wake word, Claude Code as its
  brain, about 30 fixed actions, email/calendar/chat helpers). The hub reuses its helper modules; it does
  not replace it.
- `ShikharGupta0813/AI-Task-Agent`: the Asana-to-PR agent. Its `task_agent/asana.py` shows the Asana API usage.
