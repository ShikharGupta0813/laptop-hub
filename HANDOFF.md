# Laptop Hub: handoff

Read this first when starting a new chat. It says what the project is, what is finished, what is
still to do, how it was built, and the exact words to paste to continue.

_Last updated: 2026-11-04. Update the status tables whenever something changes._

## 1. What this is

One app on the owner's Android phone that works with their Windows laptop:

- **Talk**: speak or type to an assistant that acts on the laptop (files, notes, commands).
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
   └─ planned/next: hub/study.py, hub/english.py, hub/work/ (see section 4)
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

## 3. Status: finished

| Part | State | Evidence |
|---|---|---|
| Server: login, lockout, cookies, headers, audit log | done | `tests/test_server.py` |
| Files (read-only, folder-jailed), upload to inbox | done | `tests/test_files.py`, UI tested |
| Notes: voice + text, background title/tags, edit | done | `tests/test_notes.py`, UI tested |
| Ask (Explain/Summarise/Quiz) over SSE | done | `tests/test_llm.py`, UI tested |
| Terminal over WebSocket (POSIX pty) | done on Linux | `tests/test_server.py::TerminalTest` |
| Assistant backend (tools, approvals, limits) | done | `tests/test_assistant.py` (38 tests) |
| Talk, Files, Notes, Hub tabs | done | Playwright runs, screenshots viewed |
| App shell, PWA manifest, service worker, icons | done | |
| Terminal tab UI | see section 5 | |

104 unit tests passed on the last full run (`python -m unittest discover -s tests -t .`).

## 4. Status: still to do (in this order)

1. **Integrate + review the finished tabs** (Terminal tab report, full-app browser test, security review by Opus).
2. **Work tab + backend** (`hub/work/`): Asana tasks (direct REST, token from `ASANA_TOKEN`,
   `~/.claude/.asana_token` or `~/.config/task-agent/asana_token`; read first, comment/complete with
   approval), calendar (reuse Jarvis `agenda.py` secret iCal link), email draft/send (Jarvis `mailer.py`,
   Gmail app password in Windows Credential Manager), Google Chat post (Jarvis `gchat.py`).
   Also expose these as assistant tools. Config key `jarvis_dir` points at the Jarvis folder.
3. **Learn tab + backend** (`hub/study.py`): chat with a note/document/pasted text; quiz generator with
   sources: notes, a file from the laptop, a pasted text, a web page URL (with an SSRF guard), or a
   topic from the model's own knowledge. Structured JSON questions (multiple choice + short answer),
   scoring, explanations, history, wrong-answer review. Provider switch per feature.
4. **English coach** (`hub/english.py`): speaking practice (record, Whisper transcript with word
   timestamps, words per minute, filler words, grammar and wording feedback, a better rewrite),
   role-play scenarios (standup, interview, client call), writing correction, vocabulary with
   spaced repetition, and a progress page. Pronunciation: only a hint from Whisper word confidence.
5. **Navigation**: bottom bar becomes Talk, Work, Learn, Notes, More (Files, Terminal, Hub live under More).
6. **Native Android app** (`mobile/`, Capacitor): wraps the hub URL; build steps in `mobile/README.md`.
   Later: background wake word ("Hey Jarvis") as a native plugin, push notifications.
7. **Inbox reading** (IMAP read-only with the same Gmail app password), if wanted.
8. Windows terminal path (`pywinpty`) has never run on real Windows: test and fix.

## 5. Open items and known gaps

- Everything marked "done" was tested **in a Linux container with fake models**. Not yet tested on:
  the real Windows laptop, real Ollama, real Whisper, real Claude API, real Android Chrome/keyboard.
- Windows terminal (ConPTY via pywinpty) and process-tree kill on Windows are untested.
- Assistant `run_command` can write the hub's own config if the owner approves a command that does so
  (the `write_file` tool refuses it, a shell command does not).
- Notes the assistant adds are not auto-titled (the labeller lives in the server).

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

> I'm continuing the project in `laptop-hub/` of repo ShikharGupta0813/AI-Task-Agent (branch
> `claude/clever-dirac-r85yh2`). Read `laptop-hub/HANDOFF.md` and `laptop-hub/README.md`, run the tests
> (`python -m unittest discover -s tests -t .`), then continue with the first unfinished item in section 4.
> Keep updating HANDOFF.md as you go and push often. I have given you permission to use my GitHub
> account for pushes to that repo. Ask me before spending a lot of credit.

## 9. Related repos

- `ShikharGupta0813/jarvis-voice-assistant`: the Windows desktop assistant (wake word, Claude Code as its
  brain, about 30 fixed actions, email/calendar/chat helpers). The hub reuses its helper modules; it does
  not replace it.
- `ShikharGupta0813/AI-Task-Agent`: the Asana-to-PR agent. Its `task_agent/asana.py` shows the Asana API usage.
