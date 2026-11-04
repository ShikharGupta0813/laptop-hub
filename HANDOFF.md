# Laptop Hub: handoff

**This project lives at https://github.com/ShikharGupta0813/laptop-hub (branch `main`).** (It was first built inside
`ShikharGupta0813/AI-Task-Agent`, folder `laptop-hub/`, branch `claude/clever-dirac-r85yh2`, and split out with its history.
Use the new repo from now on.)

Read this first when starting a new chat. It says what the project is, what is finished, what is
still to do, how it was built, and the exact words to paste to continue.

_Last updated: 2026-11-04 (all planned features built; security review fixed)._

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
| Android shell (Capacitor) in `mobile/` | `npm run sync` verified; **no APK built** |

416 unit tests passed on the last full run (`python -m unittest discover -s tests -t .`).

## 4. Still to do (in this order)

1. **Run it on the real laptop** (nothing below was ever run on Windows or with real models): follow README.md,
   then `python -m hub doctor`. Fix whatever the first real run shows; expect small Windows fixes (terminal ConPTY,
   process kill, lock/volume/screenshot calls, Jarvis launch).
2. **Build the APK** on the laptop: `mobile/README.md`.
3. **Apply the optional Jarvis patch** (`docs/jarvis-remote-inbox.md`) so the phone can send Jarvis text commands. Untested.
4. Remaining items from the security review (all lower priority): server-side **session revocation** (a generation
   counter in hub.json; today `python -m hub token` rotates the secret and logs everyone out, sessions last 14 days);
   **pin dependencies with hashes**; close the DNS-rebinding gap in the quiz URL fetch (connect to the vetted IP);
   consider blocking "read sensitive file then send email" in one assistant turn; check whether `tailscale serve`
   forwards `X-Forwarded-Proto` (the cookie's `Secure` flag depends on it) and what `Host` it sends.
5. Native extras: background wake word ("Hey Jarvis") plugin, push notifications for approvals, app icon/splash,
   signed release build.
6. Optional: read-only inbox via IMAP, Learn streak should count from yesterday like English does, assistant-made
   notes are not auto-titled, "Log out everywhere" button.

## 5. Known gaps and honest limits

- Everything was tested **in a Linux container with fake models and a fake OS layer**. Not tested on: the real
  Windows laptop, real Ollama, real Whisper, real Claude API, real Asana/Gmail/iCal, real Android.
- Windows-only code (ConPTY terminal, process kill, lock/sleep/volume keys, screenshot, Jarvis launch, UNC path
  behaviour) has never run on Windows.
- **No APK has been built.** `mobile/` is configured and syncs; the Gradle build needs the Android SDK.
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
python -m unittest discover -s tests -t .           # 400+ tests, no models or network needed
python -m hub.devserver --port 8799                 # fake models and a harmless fake OS; token: devtoken
```

## 9. Related repos

- `ShikharGupta0813/jarvis-voice-assistant`: the Windows desktop assistant (wake word, Claude Code as its
  brain, about 30 fixed actions, email/calendar/chat helpers). The hub reuses its helper modules; it does
  not replace it.
- `ShikharGupta0813/AI-Task-Agent`: the Asana-to-PR agent. Its `task_agent/asana.py` shows the Asana API usage.
