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

## 3h. Watchdog + backups (2026-11-12)

`python -m hub watchdog [--once]` (hub/watchdog.py): checks `GET 127.0.0.1:<port>/api/health` (new unauthenticated route returning `{"ok":true}`), starts the `LaptopHub` task if the hub is down (wait 10 s, re-check), runs `tailscale up` (30 s) when Tailscale is Stopped/NoState, only pushes when it is Logged out, and `tailscale serve --bg <port>` when serve status lacks `127.0.0.1:<port>`. One ntfy push after any repair or unfixable problem (topic read directly from config / notify_settings.json, no hub needed; an unfixable problem is repeated at most every 6 h, state in `watchdog.state.json`). One line per run in `<config dir>/watchdog.log` (rotated at 1 MB to `.log.1`). Never kills anything. `python -m hub backup [--to DIR] [--restore ZIP]` (hub/backup.py): `redline-backup-YYYY-MM-DD.zip` with `config/` (hub.json with `token_hash` and `session_secret` blanked: hub.json does hold those two, unlike the brief's assumption; restore keeps this laptop's own), `data/` (SQLite via the online backup API), `notes/`, `inbox/`; excludes .venv/.git/trash/model files/kb.sqlite; keeps 8; restore needs typing RESTORE and makes a `before-restore-*.zip` first. Config field `backup_dir`. Wizard steps `backup` (task `LaptopHubBackup`, weekly Sun 11:00) and `watchdog` (task `LaptopHubWatchdog`, every 10 min, `--once`); `doctor` lists both. Tests: tests/test_reliability.py. Unverified: real `schtasks`, `tailscale status/up/serve status` output wording on the owner's Windows build, the PowerShell trigger syntax, and ntfy delivery.

## 3d. Council (multi-llm-lab side jobs)
Built against an interface the lab teammate is still adding, with fakes only (plus a stand-in `lab` package run by the real
process runner in `tests/test_council.py`): `council --json [--only= --skip= --resume= --notes -]`, `review-packet`, `models
--json|--enable=|--disable=`. **Unverified against the real lab**: flag spellings (comma-separated `--only=`, `--resume=`,
`--enable=NAME`), the `models --json` field names, and that `run_dir` is an absolute path. Not tried on Windows or with real models.
Claude is never used unless `council_allow_claude` is set by hand in hub.json. A crash (not a clean stop) can leave an orphan lab
process; the job is shown as `interrupted` and a new run is allowed.

## 3e. Router, tool failover, tool model (2026-11-12)

Goal: use Claude as little as possible; local + Gemini carry the load. Config (hub.json only, not in the phone settings allow-list): `router_enabled` (true), `tool_failover` (true), `local_tool_model` ("").
- `hub/router.py`: `classify()` (word heuristic, no AI call) and `Router.route()`. Only when the request AND `default_provider` are auto/blank: trivial -> local (if Ollama not known down), heavy (tool group match, action verb, code, multi-step, >60 words) -> duo (if Gemini ready), else `resolve_default`. Explicit picks (also an explicit `default_provider`) are untouched. `/api/assistant/message` first emits `{"type":"meta","routed_to","reason"}`; `/api/ask` first emits `{"meta":{...}}`; `/api/status.providers.router = {enabled, last_decision}`. The UI ignores `meta` (badge not built yet). Note: duo still uses the rationed Claude supervisor (`duo_max_claude_per_hour`, set 0 to never ask Claude).
- Failover: the `local` provider runs `_local_turns` (native Ollama tool calls), not `_text_turns`, so that is where it lives. After 2 consecutive turns with an unknown tool or invalid arguments (`_fumbled`), `_gemini_step` runs ONE turn on Gemini (text tool protocol), emits `{"type":"note"}` ("local model fumbled a tool call; asked Gemini for this step") and the local model carries on. Once per message, only to Gemini, needs `availability()["gemini"]["ready"]`. Counter `assistant.failovers` (process memory) is `/api/status.failovers`. `assistant.js` shows `note` events in the notice bar.
- `local_tool_model`: `LocalChat.stream(..., model=)`; turns that send tools use it, plain chat keeps `chat_model`; warmed on startup too; shown by `python -m hub doctor` and Settings diagnostics. Docs: `docs/SETUP-WINDOWS.md`, `docs/guide/04-ai-brains.md`.
- Ollama knobs were already fine (`KEEP_ALIVE` 30m, num_ctx 4096 plain / 8192 tools), unchanged. Tests: `tests/test_router.py`. Not verified on the real laptop.

## 3f. Autopilot (2026-11-12): "my work goes to approvals only"

`hub/autopilot.py` (feature module, `FEATURE_MODULES`), `js/autopilot.js` + `css/autopilot.css` (Apps > Autopilot, also a Setup checklist row), tool `autopilot_cards` in
`friday_tools.py`, wizard step `autopilot` (after `council`), docs `docs/api/autopilot.md`, `docs/guide/12-autopilot.md`. sw.js VERSION v15-autopilot-2026.11.
- Off by default (`autopilot_enabled`). A loop wakes every 30 s and sweeps when on and `autopilot_sweep_min` (15) has passed (config is read live, so Settings
  toggles it without a restart). The sweep only READS and writes cards to `<data>/autopilot.db`: Watch alerts (`/api/watch/alerts`), unread Gmail, Asana tasks due in <= 2 days,
  and once per weekday after `autopilot_standup_time` a standup (git log of `autopilot_repos` by absolute `git` path, no shell, 10 s timeout, filtered to the repo's `user.email`, plus
  my Asana comments of the previous workday and tasks due today). It reuses the other features through `InProcessClient` (inbox, work, watch routes), like Watch and Friday do.
- Classification is words first (`classify()`): one-time codes, no-reply senders, receipts/newsletters ignored; meeting invites = information; questions/requests get an AI draft.
  AI budget `autopilot_daily_ai_calls` (30/day, counted in the db); over it, or on a model error, the card is made as information with a note. (The standup falls back to a plain list.)
- Never Claude: `pick_provider()` = default provider, but `claude`, `claude-login` and `duo` (it asks Claude) become Gemini if ready, else local.
- Execution only on `POST /api/autopilot/{id}/approve {confirm: true, text?, to?}`: the card is claimed (`pending` -> approved/edited) first so a double tap runs once; email goes through
  `/api/work/email/draft` + `/send` (the existing SMTP path) to the original sender (stored `to` must equal `from_addr`, resolved recipient must equal it, `to` in the body is an explicit,
  audited change); Asana comment via `/api/work/asana/tasks/{gid}/comment`; copy cards only return the text. Failure = `failed` + short reason, never retried. Audit has kinds/lengths only.
- Settings may change `autopilot_enabled`, `autopilot_sweep_min` (5-240), `autopilot_daily_ai_calls` (0-500); tone/repos/expiry/standup time are wizard or hand-edit.
- Tests: `tests/test_autopilot.py` (fakes for hub routes, model, clock, git), wizard tests in `test_setup_wizard.py`. UNVERIFIED: real Gmail/Asana/Gemini/local-model draft quality, the real
  jarvis `mailer.resolve_recipient` accepting a bare address, real `git` on Windows, the phone UI (never opened in a browser here), Watch-captured Gmail alerts carry no address so they are information-only.
  Known limits: Asana "yesterday" = comments I wrote on open tasks (the Work API cannot list completed tasks); only the first 8 open tasks are checked.


## 3g. Today + voice approvals + Autopilot research (2026-11-12): "never search for anything"

`hub/today.py` (feature module `today`, last in `FEATURE_MODULES`), `js/today.js` (Apps > Today; the Home "Today" card action opens it), docs `docs/api/today.md`, `docs/guide/13-today.md`. sw.js VERSION v16-today-2026.11.
- `GET /api/today` aggregates calendar, Asana, expenses, inbox, Autopilot and Council through `InProcessClient` plus git via `autopilot.git_commits` (new module-level helper; `run_git` too). One failing source goes to
  `sources_failed`. `GET /api/today/text?style=morning|evening` is a template (no AI, deterministic, <= 180 words). Config `today_enabled` (true), `today_evening_time` ("19:30"), `autopilot_research` (true); all three editable in Settings.
- Friday tools (friday_tools.py): `today_summary`, `autopilot_pending`, `autopilot_decide`. Routing: ONE new `"today"` group line in `TOOL_GROUPS` (hub/assistant.py; summary words + approve/skip words), the same path for typed and voice.
  The `friday_brief` TOOL appends the day summary (the `/api/friday/brief` route is untouched: friday.py belongs to the persona work, so the day-start hook lives in the tool).
- decide: the number map is per app (closure), expires after 1 hour, needs a prior `autopilot_pending`; ids must be in that listing too; descriptions ("the mail one") must fit exactly one card; stale/handled cards are refused;
  it calls the normal approve/skip routes with `confirm: true`, so the recipient guard and audit apply. The tool is NOT behind the assistant's approval prompt (the owner's command is the approval).
- Evening: `Autopilot.maybe_evening()` in the sweep loop: weekdays, at/after `today_evening_time`, once per day (meta key set BEFORE sending), needs autopilot + today enabled; push (first 200 chars) + FYI card (`source: today`, kind none, full text in `draft`, shown as plain text by autopilot.js).
- Research: `Autopilot.research()`; up to 3 read-only lookups (inbox search by subject keywords, Asana subtasks/comments from the detail already fetched, `/api/search/all` notes/files), 2500 chars cap, only while budget remains; payload `research` label -> badge. 0 extra AI calls.
- Tests: `tests/test_today.py`. UNVERIFIED: real calendar/inbox/Council data shapes (written from the route code), the phone UI (never opened in a browser), `/api/search/all` quality for research, git commit-time parsing on Windows, "mails replied" counts only Autopilot-sent replies (the Sent folder is not read).

## 3h. Installable skills (2026-11-13)

`hub/skills.py` (feature module `skills`, last in `FEATURE_MODULES`), `hub/skills_tools.py` (5 ExtraTools, one `skills` line in `TOOL_GROUPS`), `js/skills.js` + `css/skills.css` (Apps > Skills), docs `docs/api/skills.md`, `docs/guide/14-skills.md`. sw.js VERSION NOT bumped (the owner's rule here); the new files are in SHELL.
- Skills = `<data dir>/skills/<name>/SKILL.md` (+ `.hub-skill.json`). Install is GitHub only: `git clone --depth 1` via argv list, temp dir, copy only the skill folder, symlinks refused, 2 MB / 300 files, SKILL.md <= 40 KB, <= 200 skills.
  Catalog = GitHub contents API (folder names only), cached 1 h, sources in `skills_sources`. Config: `skills_enabled` (Settings), `skills_sources` (hand-edit).
- Security: skill text is untrusted; `use` fences it with a random code and a never-overrides note; scripts are listed, never run; install and remove are approval-gated (tool `needs_approval` + preview, route needs `confirm: true`, UI confirm).
- Personas: frontmatter `persona: jarvis|friday|both`; `skills.ACTIVE_PERSONA` (a ContextVar) is set from `session.persona` at the start of each assistant turn and the tools filter by it; `persona_hint` is only a word-based guess.
- Tests: `tests/test_skills.py` (fake git runner and fake GitHub HTTP). UNVERIFIED: real GitHub clone and API (rate limits), the real layout of hermes-agent / openclaw (default source paths are guesses), the phone UI (never opened in a browser), whether the assistant context var reaches tool calls in every provider path.
## 3j. Screen vision (2026-11-13): "Jarvis, what's this error on my screen?"

- `hub/vision.py`: `POST /api/vision/explain`, `GET /api/vision/status`, tool `screen_explain` (approval card unless Full Control,
  `untrusted`, group `vision`). Reuses `screen.RealScreenSystem.capture_jpeg`, then `shrink()` (1600 px, q75, <= 1.2 MB, optional region crop).
  `GeminiClient.ask_image` (new, `hub/llm.py`) sends an inline image part. claude-login only if its client gains `ask_image`; never the paid API.
- Config `vision_enabled` (true), `vision_daily_calls` (30); both in Settings. Phone: "Explain this screen" card on the Screen tab (`js/screen.js`).
- Docs `docs/api/vision.md`; tests `tests/test_vision.py`. No sw.js change (screen.js/css already in SHELL).
- UNVERIFIED: real Gemini image call with a live key, real mss capture, the phone UI in a browser, voice phrasing routing on the local model.

## 3i. Deep research with citations (2026-11-13): "Jarvis, research X for me"

`hub/research.py` (feature module `research`, last in `FEATURE_MODULES`; also `research.build()` registered in `server.py` with the other assistant tools), `js/research.js` + `css/research.css` (Apps > Research; added to sw.js SHELL, VERSION not bumped),
docs `docs/api/research.md`, `docs/guide/15-research.md`. Config `research_enabled` (true), `research_max_hops` (3), `research_max_sources` (8), `research_daily_ai_calls` (20), all editable in Settings.
- Pipeline: plan queries -> search (DuckDuckGo HTML, Wikipedia fallback; injectable) -> fetch (httpx, SSRF guard, 1.5 MB cap, html/text only) -> extract -> report with `[n]` citations -> follow-up hops from gaps. Jobs in `research.sqlite` (cap 200); `POST/GET/DELETE /api/research`, `POST /api/research/{id}/notify`.
- Two styles: `technical` (Jarvis, cyan) and `brief` (Friday, amber; also reads calendar + Asana through read-only in-process routes). Default from the persona; UI has a Jarvis/Friday chooser.
- Tool `deep_research` (no approval, read-only; the model has no tools): waits ~25 s, else "I'm on it, the report will be in Research" + ntfy push when done. One `TOOL_GROUPS` line `research`.
- AI: Autopilot's `pick_provider` (Gemini/local, never Claude, never duo), daily call budget; degrades to sources + snippets.
- Tests: `tests/test_research.py` (fakes only). UNVERIFIED: real DuckDuckGo HTML shape (it changes; Wikipedia is the fallback), DNS-rebinding between the check and the connect (the host is resolved and checked, then httpx resolves again), real local-model JSON quality, the phone UI (never opened in a browser), the tool with a real model deciding when to pass `style`.
## 3h. Brain: long-term memory + task desk (2026-11-13), both personas

`hub/brain.py` (module `brain`, last in `FEATURE_MODULES`), `hub/brain_tools.py` (5 assistant tools, registered in server.py `extra_tools`), `js/brain.js` + `css/brain.css` (Apps > Memory), docs `docs/api/brain.md`, `docs/guide/16-memory.md`.
- SQLite `brain.sqlite` in the data dir, FTS5 with LIKE fallback; items carry a `persona` (jarvis = technical kinds, friday = people/time, both = notes). Time phrases go through the Reminders parser/service (real push) and the reminder id is stored.
- Hooks in `hub/assistant.py` (kept tiny): ONE `"brain"` TOOL_GROUPS entry; `_brain_save` ("remember ..."/"note ..." answered "Saved." with no model; not for questions or text naming a time); `_brain_memory` -> `brain.context_for` (question-like text, score >= 0.6, <= 600 chars) injected by `_framed` via `session.memory`.
- `persona.py`: one sentence each (Jarvis: recall for technical questions; Friday: recall + follow-ups, mention due follow-ups). Today: `brain` source, `followup` timeline rows, a Follow-ups section, a "Follow-ups due" sentence in the brief text.
- Intake uses `autopilot.pick_provider` (Gemini or local, never the paid Claude API). Config `brain_enabled`, `brain_context` (Settings).
- Tests: `tests/test_brain.py` (fakes only). sw.js: files added to SHELL, VERSION NOT bumped (orchestrator). UNVERIFIED: the Memory screen in a real browser, real AI verdict quality, real ntfy push from a brain reminder, FTS5 on the laptop's SQLite build, Windows.

## 3i. Claude Code sessions: watch and steer (2026-11-13)

`hub/ccsessions.py` (module `ccsessions`, last in `FEATURE_MODULES`; 3 tools registered in server.py `extra_tools`), `js/ccsessions.js` + `css/ccsessions.css` (Apps > Claude), docs `docs/api/ccsessions.md`, `docs/guide/18-claude-sessions.md`. Config `ccsessions_enabled` (true), `ccsessions_dir` ("" = ~/.claude/projects), hub.json only.
- Read-only core: tail (256 KB) of `<dir>/<project>/<id>.jsonl`, status working/waiting/idle inferred from the tail + mtime, <= 50 sessions. Tools `cc_sessions`, `cc_session_read` (untrusted) and `cc_session_send` (needs_approval + mutating, untrusted; the taint gate applies, no new bypass). One `ccsessions` TOOL_GROUPS entry.
- Send = `claude --resume <id> -p <msg> --output-format json` via argv list in the project folder, ANTHROPIC_API_KEY removed, 600 s, background thread + job id; refused while the session is working. Route needs `confirm: true`.
- Proactive: `Watcher` (60 s, only while enabled and the folder exists) -> one `cc_waiting` batch event per waiting episode; `proactive.py` got `cc_waiting` in `BATCH_TYPES` and a sentence.
- Tests: `tests/test_ccsessions.py` (fake transcripts, `subprocess.run` mocked). sw.js: files added to SHELL, VERSION NOT bumped. UNVERIFIED: the real transcript shapes (assumed, listed in the API doc), `claude --resume -p` on Windows (.cmd shim, quoting), whether an unanswered tool_use is really a permission prompt, the Claude screen in a real browser.
## 3i. Build flow: spec-first delegation (2026-11-13), Jarvis

`hub/buildflow.py` (module `buildflow`, last in `FEATURE_MODULES`), `hub/buildflow_tools.py` (7 tools in server.py `extra_tools`), `js/buildflow.js` + `css/buildflow.css` (Apps > Build), docs `docs/api/buildflow.md`, `docs/guide/17-build-flow.md`.
- Flow in `buildflow.sqlite`: brainstorm (free AI, one question at a time, max 6) -> spec file -> checkbox plan file -> Council per step. Files live INSIDE a shared root (`roots`), never overwritten (`-2`). The plan file is the truth (steps re-parsed on every look, hand edits fine).
- Delegation reuses `POST /api/council/run` / `GET /api/council/jobs/{id}` in-process; a run `accepted` ticks the box, anything else records the reason. 20 s poll in `start(ctx)` plus a settle on every look. Finish: Brain `decision` item + proactive `build_done` batch event (added to `BATCH_TYPES` and `batch_sentence`).
- Preview/approve split: `spec`/`plan`/`next` routes take `{preview: true}`; tools `build_spec|build_plan|build_next` are approval-gated + `mutating` (taint gate). One `"buildflow"` TOOL_GROUPS entry. Config `buildflow_enabled` (Settings).
- Tests: `tests/test_buildflow.py` (fake AI, fake council caller, temp workspace). sw.js: files added to SHELL, VERSION NOT bumped. UNVERIFIED: the Build screen in a real browser, real AI question/spec/plan quality, a real Council run ticking a box, Windows paths, the Council having no working-directory option (the task text names the folder, the lab is trusted to obey).

## 3k. Privacy redaction and usage budget (2026-11-13), Jarvis

`hub/redact.py` (patterns, per-turn placeholder map, `Guarded` client wrapper), `hub/usage.py` (counters, caps, report), `hub/privacy.py` (module `privacy`: `GET /api/usage`, `POST /api/redact/preview`), `js/privacy.js` + `css/privacy.css`, docs `docs/api/privacy-usage.md`, guide paragraph in `docs/guide/10-security-and-privacy.md`.
- Hook: `create_app` wraps the Gemini, claude-login, claude and duo-Gemini clients in `redact.Guarded`; the Claude-API path redacts in `assistant._claude_params` and restores text blocks and `tool_use.input` in `_claude_turns`; `AssistantService.run` opens/closes the per-turn map. Local Ollama is only counted. Audit = counts per kind.
- Router: `Router.route(choice, text, ollama_up, override_cap)` falls back to local at a cap with a `note` in the meta event; "use gemini anyway" lifts it for one message. Config: `redact_*`, `cloud_daily_cap`, `provider_daily_caps`, `claude_login_5h_soft_cap` (Settings allow-listed; `redact_names` audited without value).
- `tests/helpers.make_config` now sets a temp `data_dir` so usage.json never lands in the real home. sw.js: files added to SHELL, VERSION NOT bumped.
- Tests: `tests/test_redact.py`, `tests/test_usage.py` (fakes). UNVERIFIED: the real Anthropic SDK accepting dict text blocks mixed with thinking blocks after redaction, real Gemini/claude-login output with split placeholders, false-positive rate on real documents (regexes are conservative but untested on real data), Council and screen images are NOT redacted, the Settings card, caps sheet and Home gauge in a real browser.

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


## 10. Jarvis and Friday perfection pass: Full Control, personas, latency

- **Full Control** (`full_control`, default false; settings allow-list; ON needs `full_control_confirm: "I UNDERSTAND"`):
  `AssistantService._call_tool` skips the approval step for every gated tool, audits `assistant_tool` with
  `full_control: true`, marks `tool_start.full_control`. `_precheck` refusals (protected hub files, credential folders) stay.
  `/api/status` and `/api/assistant/info` show it; `js/fullcontrol.js` owns the header chip and the typed sheet; Settings row.
  `friday_prep_run` audit also carries the marker; voice and the Friday screen run laptop prep without the readout when on.
- **Personas**: `hub/persona.py` (`split_persona`, short prompts). The server routes ANY message (typed or spoken) with a
  leading/trailing name; `persona`/`voice` are optional fields of `POST /api/assistant/message`. `js/speechchunk.js` ports the splitter.
- **Fast path** (`hub/fastcmd.py`, `fast_commands`): "run/execute <cmd>", "open <configured app or link>", "type <text>" with
  Full Control on skip the model. New tool `type_text` (needs Apps > Screen armed).
- **Latency**: `hub/latency.py`, `GET /api/latency`, shown in Settings > Diagnostics; `POST /api/assistant/warm`
  (chat + tool model + Whisper + Gemini connection) called by voice mode every 8 min; Gemini keeps one HTTP connection;
  Whisper preloads and uses beam 1; voice turns skip the Claude planning/review calls; voice replies are spoken sentence by sentence.
- Tool exceptions and model crashes become error messages; voice retries once if the stream drops before any event.
- Tests: `tests/test_full_control.py`. Not in `sw.js` precache yet: `/js/fullcontrol.js`, `/js/speechchunk.js` (add on merge).
- **Wake word ("Hey Jarvis" / "Hey Friday")**: `js/wakephrase.js` (pure matcher, node-tested), `js/wake.js` + `css/wake.css`
  (continuous browser SpeechRecognition, auto-restart with backoff, paused while hidden / voice mode / dictation, chip row under the
  top bar, WebAudio chime), `voicemode.js` `setPreload(text, persona)`, Settings > Appearance switch (`localStorage hub.wake`, default off).
  Works only while the app is open and the screen on. Tests: `tests/test_wake.py`, `tests/wake_e2e.js` (fake SpeechRecognition).
  In `sw.js` SHELL; VERSION not bumped. **Unverified on a real phone** (mic, Android session restarts, en-IN accuracy).
  Dev server note: its fake assistant rejects the `persona` argument, so a voice turn there shows an error after the words arrive.

## 9. Related repos

- `ShikharGupta0813/jarvis-voice-assistant`: the Windows desktop assistant (wake word, Claude Code as its
  brain, about 30 fixed actions, email/calendar/chat helpers). The hub reuses its helper modules; it does
  not replace it.
- `ShikharGupta0813/AI-Task-Agent`: the Asana-to-PR agent. Its `task_agent/asana.py` shows the Asana API usage.

## 3j. Loose ends closed (2026-11-13)
- Proactive hooks: Friday's meeting check (10 min lead, once per meeting via `dedupe`) now goes through `proactive.submit_or_notify` (falls back to `ctx.notify`), so one push only. In-hub backup (`POST /api/settings/backup`) sends `backup_done` (batch) / `backup_failed` (now, new type). `python -m hub backup` and the watchdog are separate processes with no dispatcher: they KEEP their direct ntfy.
- Skills catalog: hermes-agent is `skills/<category>/<skill>/SKILL.md`, openclaw/openclaw is `skills/<skill>/SKILL.md`; `openclaw/skills` is not public. Defaults are now `NousResearch/hermes-agent/skills/*` (trailing `/*` = categories, `kind: category`) and `openclaw/openclaw/skills`. Verified by clone into temp dirs.
- Research: `research_search_backend` (auto|ddg|wikipedia). DDG parser now pairs title/snippet per block. UNVERIFIED: the real DDG page (html.duckduckgo.com is blocked from the agent sandbox; the test fixture is hand-written from the known shape, not a saved live response). No second backend added (nothing else reachable).
- Update button: Settings card + `POST /api/update {confirm:true}` -> `updater.start_update` (task LaptopHubUpdate if it exists, else detached `python -m hub [--config X] update --restart`). Audited `update_start`. UNVERIFIED on real Windows (a detached child may still die when `schtasks /End LaptopHub` kills the task's process tree).

## Laptop wake word + screen keep-alive
- `js/wake.js`: Screen Wake Lock (`navigator.wakeLock.request("screen")`) while wake is on and the page is visible; re-acquired on `visibilitychange`/`hub:wake`, released when off. Switch `localStorage hub.wake.keepawake` (default on, only "0" = off), Settings row "Keep screen on while listening". Unsupported browsers: silent no-op. `tests/wake_e2e.js` has a fake `navigator.wakeLock` (that e2e needs node + playwright; NOT run in the build session).
- `hub/wakeword.py` (module `wakeword`, in FEATURE_MODULES): optional `sounddevice` mic -> energy gate (`Segmenter`) -> hub Whisper -> `match_wake` (Python port of wakephrase.js) -> chime (Windows) -> in-process `/api/assistant/message` (voice, auto_approve False, session `laptop-wake`) -> speak (SAPI via PowerShell argv + stdin / `say` / `espeak`). Config `wake_laptop_enabled` (False), `wake_laptop_device`. Routes `GET /api/wakeword/status`, `POST /api/wakeword/toggle`. Doc `docs/api/wakeword.md`; tests `tests/test_wakeword.py` (all fakes).
- Not verified on real hardware: a real microphone, real Whisper accuracy for "Hey Jarvis", SAPI speech, the in-process SSE call against a live assistant, the Playwright wake-lock check. `requirements-voice.txt` lists `sounddevice` as a commented optional line.
## 3k. Background wake word, Android (2026-11-13)
- Native: `mobile/android/app/src/main/java/dev/shikhar/laptophub/wake/` (`WakeService` microphone foreground service + Vosk
  grammar spotting, `ModelManager` model download, `HubWakePlugin` = Capacitor plugin `HubWake`, pure `WakeMatcher` / `WakeLaunch` /
  `SafeUnzip` / `ModelStateMachine` with JVM tests in `app/src/test/.../wake/WakeLogicTest.java`). Dependency `com.alphacephei:vosk-android:0.3.75`.
  Manifest: FOREGROUND_SERVICE(+_MICROPHONE), POST_NOTIFICATIONS, WAKE_LOCK, VIBRATE, USE_FULL_SCREEN_INTENT, service type microphone. No boot receiver (Android 14 forbids it).
- Web: `hub/static/js/wakebridge.js` (loaded by one line in `main.js`, in `sw.js` SHELL, VERSION not bumped): Settings card, model download progress, switch, and
  opening voice mode from `HubWake.consumePending()`. No-op outside the Android app. Docs: `mobile/README.md` "Hey Jarvis in the background".
- **Unverified:** a real Gradle build (not possible in the sandbox; only javac against android-all API 36 + real Vosk classes, with hand-written Capacitor/androidx stubs), a real phone,
  the Android 10+ background activity launch rule (fallback is the full-screen-intent notification), Android 14/15 FGS behaviour, model SHA (only a size range check, the alphacephei list was not reachable), battery.
## 3k. Graded safety levels N1/N2/N3 (2026-11-13)
- `hub/safety.py` `classify()` + `ExtraTool(level=...)`; the gate lives in `AssistantService._call_tool` (`resolve(..., confirm_text, remember, voice)`, `cancel_critical`, grants API). Routes: `/api/assistant/approve` (new optional `confirm_text`, `remember`, `voice`), `/cancel`, `/permissions`, `/permissions/revoke`. UI: assistant.js approval card (red banner, CONFIRM box, countdown, remember buttons), voicemode.js (no spoken approve for N3), settings.js "Active permissions". Details: docs/api/assistant-safety.md. No sw.js bump, no new files in SHELL.
- Behaviour changes: every executed tool call now audits `assistant_tool` with `level` (N1 calls too); Full Control still asks for N3. Any delete via `run_command` is N3 (outside hub trash), even a single file: intentional.
- UNVERIFIED: the real 10 s countdown on a phone, voice-mode wording read aloud, ARC-Glass look of the red banner on a device (only source-checked), real Windows shutdown/lock paths (tests use fakes; `laptop_action` shutdown/restart/sleep/lock_screen are N3 by id).

## 3l. Opportunities finder (2026-11-13)
`hub/opps.py` (sources, finder loop, ranking, routes, tools), `hub/opps_vet.py` (scam flags), `hub/profile.py` (private profile, `get_profile(ctx)`), screen `js/opps.js`.
Docs: `docs/api/opps.md`, `docs/guide/19-opportunities.md`. Settings: `opps_enabled` (off), `opps_interval_min`, `opps_min_score`. Pushes to `applyq.add_item` via guarded import.
Unverified: every starter feed/API address (no network when built); PDF parsing without pypdf; the UI was not run in a browser.
## 3l. Application queue / APPLYQ (2026-11-13)
- `hub/applyq.py` (SQLite `applyq.sqlite`, drafting, `/api/applyq` routes, `add_item(ctx, item)` for the finder, `today_line`), `hub/applyq_tools.py` (6 tools; only `send_application` sends: approval, N2, one item by id), `js/applyq.js` + `css/applyq.css` (Apps > Applications; in `sw.js` SHELL, VERSION not bumped). Docs: `docs/api/applyq.md`, `docs/guide/20-applications.md`.
- Hard rule: NO automatic sending. The only sender is `Applyq.send` via `POST /{id}/send` or the approved tool; `tests/test_applyq.py::NeverAutomatic` asserts add/draft/summary/proactive/routes never call gmailx.
- Drafts are template-only by default (profile facts + `[FILL: x]` markers, sending blocked until filled); optional AI via `draft_fn` or `{"ai": true}` using `ctx.model_for("auto")`.
- Optional neighbours, imported defensively: `hub.profile.get_profile`, `hub.gmailx.send_mail/list_accounts/search`. Settings keys appended: `applyq_enabled`, `applyq_daily_cap`. Proactive: `applyq_ready` (batch), `applyq_daily` (now, once after `today_evening_time`). Today gains an `applyq` line (also on the front page).
- UNVERIFIED: real Gmail send/search (fake in tests), AI rewrite with a real model, the screen on a phone/390px (source-checked only), the `gmailx` search result shape (assumed list of dicts with `from`/`subject`).
## 3l. Multi-account Gmail (2026-11-13)
- `hub/gmailx.py` (module `gmailx`, appended to FEATURE_MODULES), `hub/gmailx_tools.py` (tools `gmail_summary`, `gmail_search`, untrusted/read-only), `js/gmailx.js` (Settings "Gmail accounts" card, in `sw.js` SHELL, VERSION not bumped). Doc: `docs/api/gmailx.md`; guide 05 has "Connect a second Gmail account (company)".
- IMPORTANT FINDING: the repo had NO Google OAuth. The "one Google connection" is the Gmail app password in the keyring (`jarvis-gmail`) read over IMAP by `hub/inbox.py`. It becomes account `default` / `personal` untouched. Extra accounts use a NEW OAuth loopback+PKCE flow (Google's device flow does not allow Gmail scopes). The owner must create a Desktop-app OAuth client (steps in the guide).
- Scopes: `gmail.readonly`; `gmail.send` only when "allow sending" is on (re-consent). Company default read-only. Tokens in keyring service `redline-gmail`. Config `gmail_company_ai_allowed` (False): company bodies never go to a cloud model (`get_message(destination=...)`, Autopilot, `/api/inbox` AI refuse), redact applied when allowed.
- Hooks: Autopilot cards carry `account` (chip in `autopilot.js`), new `src_gmailx` source (cards only, no AI), Today `mail.accounts`, assistant `send_email` takes `account` (required with 2+ accounts, refuses non-send-enabled). Other workers: import `hub.gmailx` guarded; functions are BLOCKING (use `asyncio.to_thread`).
- Tests: `tests/test_gmailx.py`, `tests/test_gmailx_autopilot.py` (fake Google, dict keyring). UNVERIFIED: real Google consent/refresh, Workspace admin blocks, loopback listener in a real browser, the Settings sheet on a phone.
## 3l. Posts: publishing destinations (2026-11-13, generalised from dev.to-only)
- DESIGN RULE (owner): build the GENERAL feature. Posts sends ONE draft to MANY destinations from an owner-editable list. `hub/destinations.py` (kinds `devto`, `hashnode`, `wordpress`, `ghost`, `markdown-file`, `webhook`, `manual`; one isolated adapter each; list store, secrets, formatting rules, `make_variant`), `hub/posts.py` (engine, `/api/posts` + `/api/destinations` routes, scheduler, `post_*` assistant tools; module `posts` in `FEATURE_MODULES`, `posts.build()` in `server.py`), `hub/devto.py` = thin shim for the OLD `/api/devto/*` routes. Screen `js/posts.js` + `css/posts.css` (Apps > Posts, tabs Ideas | Drafts | Published | Destinations; route id still `devto`; `js/devto.js` is a one-line re-export; sw SHELL updated, VERSION not bumped). Docs `docs/api/destinations.md` (devto.md is a pointer), `docs/guide/21-posts.md`. Tests `tests/test_posts.py` (fakes: one httpx MockTransport World for every service, dict keyring, scripted AI, fake clock) and `tests/test_devto.py` (the old routes as shims).
- Adding a site needs no code: `manual` (name + composer URL; copy text, open link, tap "I posted") or `webhook`. Per-destination `format` rules (style article|short|thread, max_length, hashtags, tag_limit, title_length, template) are editable. Starter list restorable with "Reset to starter list".
- HARD RULES (tested): saving a draft remotely always sends a draft status; PUBLISH is `Posts.publish` only (one destination, `confirm:true`, saved draft first on draft-capable kinds), never from drafting/saving/prepare/scheduler/reminder; assistant `post_publish` is in `safety.ALWAYS_ASK` (replaced `devto_publish`), so Full Control/auto-approve/grants still ask. Blocklist covers every destination. Markdown-file never runs git.
- Secrets: OS keyring only (service `redline-posts`, `dest-<id>`); never in files, routes, logs, audit; scrubbed from errors. Migration: old keyring `redline-devto/api_key` is COPIED to the dev.to destination, `devto.json` is converted once to `posts.json` (draft `targets`). Config keys `devto_*` unchanged (edited via `PATCH /api/posts/settings`).
- Friday batch: events are now `post_comment` / `post_cadence`; `proactive.py` has `TYPE_ALIASES` so `devto_comment`/`devto_cadence` still work. Comments (dev.to, Hashnode, WordPress) are untrusted; `post_list` is an untrusted tool.
- UNVERIFIED (public docs only, never run live): Hashnode GraphQL (createDraft/publishDraft/stats/comments shapes, bare-token header, no draft update), Ghost Admin JWT + `updated_at` flow, WordPress REST (tags not sent), dev.to shapes and partial `PUT {published:true}`, the screen in a browser/390px, real keyring, AI draft quality. See `docs/api/destinations.md`.
## 3l. Urgent-alert triage (2026-11-13), Friday owns, Jarvis supplies technical content

`hub/alerts.py` (triage, rules, plan/evidence/draft, delivery), `hub/asana.py` (optional poller, token in keyring), `hub/alerts_tools.py` (5 tools, TOOL_GROUPS `triage`), `js/alerts.js` + `css/alerts.css` (#/alerts, Settings tab inside it), docs `docs/api/alerts.md`, `docs/guide/22-urgent-alerts.md`, tests `tests/test_alerts.py`.
- Hooks kept small: `proactive.py` (event type `alert`, urgency 3 passes quiet hours, batch sentence), `watch.py` ingest calls `alerts.on_watch_ingest`, `main.js` registry + `pollAlerts` (speaks `/api/alerts/pending`), sw SHELL (VERSION NOT bumped), `server.py` FEATURE_MODULES + tools, config keys appended (`alerts_*`, `asana_*`).
- GENERAL design (owner correction): every mention/question/request raises an alert; VIPs, watched sources (`alerts_sources`) and word lists (`alerts_rules`) are editable in Alerts > Settings; one generic draft path (Brain memory + task plan; RCA is just a memory lookup).
- HARD RULE: drafts are never sent; Copy / wa.me / intent / mailto links the owner taps.
- UNVERIFIED: real Asana API, gmailx result shape, Android intent links, the screen in a real browser, laptop speaker on Windows. Android capture only forwards Google Chat and Gmail today (WhatsApp/Slack/Teams/Asana need a Java filter change, not done). Possible double push with Watch rules for the same Gmail/Chat message.


## Inbound sources generalised (task sources, mail accounts, calendar feeds, one Settings list)
Owner rule: build the GENERAL feature; everything is a list the owner can add to, edit, switch off or delete; existing items are ready-made entries.
- New: `hub/sources.py` (generic `/api/sources/{kind}` routes over app | task | webhook | mail | calendar, the app list, `GET /api/alerts/capture-config`), `hub/tasksources.py` (adapter registry: asana, jira, trello, linear, github, rss, webhook; public `POST /hooks/{id}` with a shared secret), `hub/calendars.py` (ICS parser + feed list), `hub/mailaccounts.py` (IMAP/SMTP helpers), `js/sources.js` (panel in Alerts > Settings, in sw SHELL, VERSION not bumped). Appended to FEATURE_MODULES: tasksources, calendars, sources. No new config keys (lists live in `sources.json`, `tasksources.json`, `calendars.json`, `gmailx.json` in the data folder; secrets in the keyring: services `redline-sources`, `redline-calendars`, `redline-gmail` key `imap:<id>`).
- Changed: `hub/gmailx.py` (kind `imap`, free-text `tags`, per-account `cloud_ai_allowed`, `alerts` flag, add/edit/remove; card renamed "Mail accounts" in `js/gmailx.js`), `hub/asana.py` (`fetch_events` split out of `poll_once`, optional own state/workspace; behaviour unchanged), `hub/alerts.py` (`trusted` ingest, more source labels, capture-config route, `poll_email` now passes account ids), `hub/work/__init__.py` (`/api/work/calendar` merges Google with the enabled feeds; the calendar was never in `hub/life.py`).
- Docs: `docs/api/sources.md`, `docs/api/gmailx.md`, `docs/api/alerts.md`, guide 22. Tests: `tests/test_sources.py`.
- UNVERIFIED against live services: Jira (`/search/jql` with fallback), Trello notification filters, Linear `assignedIssues`, GitHub notification reasons, real Outlook/Yahoo/Zoho IMAP/SMTP (Office 365 often blocks basic auth), real ICS feeds from Google/Outlook, the Settings screens on a phone. The Android capture app keeps its own package list until the Java worker reads `capture-config`.
## 3m. Owner lists: the general features behind the examples (2026-11-13)
Design rule from the owner: build the GENERAL feature; today's hard-coded choice is one ready-made entry. Settings > "Assistants and rules" (js/ownerlists.js, built on the shared list editor js/listeditor.js) holds five lists; defaults reproduce the old behaviour exactly.
- **Assistants** (`hub/assistants.py`, `assistants.json`, `/api/assistants`): id, name, wake names + mis-hearings, role prompt, voice, tool-group focus, accent, enabled. Jarvis and Friday are the two default entries (prompts word for word as before; empty `tool_groups` = no focus). `persona.py` functions take the list (`assistants=`), `wakeword.match_wake`, `wakephrase.js` `matchWake({assistants})`, `speechchunk.js splitName`, voice mode and the assistant service read it; `js/assistants.js` caches `GET /api/assistants` with the old constants as fallback. Docs: docs/api/assistants.md.
- **Notification preferences** (`proactive.py`): per-type `now|batch|quiet-only|off` (`proactive_prefs.json`, `/api/proactive/prefs`), `proactive.register_type(name, label, default)` (guarded; unknown types self-register on first submit), generic batch sentence for types without a template. The alerts module's "always notify me about X" is NOT here.
- **Custom safety rules** (`safety.py` NEW code only: `clean_custom_rules`, `classify_with_custom`, `load/save_custom_rules`; `ALWAYS_ASK` and `classify` untouched): only raise N2/N3; patterns validated by `hub/safepattern.py` (length, compile, no nested repeats, bounded subject; Python `re` cannot be interrupted, so safety is by construction, not a timer); `assistant._level` applies them, `assistant_custom_rule` audit (ids only); routes in `hub/rules.py` (`/api/safety/rules`, test box).
- **Custom redaction rules** (`redact.py`: `load/save/clean_rules`, `Redactor(custom=)`, placeholder regex now accepts any `[KIND_n]`): `/api/redact/rules`, preview covers them, counts-only audit.
- **Brain**: free-form tags (up to 20, `tag=` filter, `/api/brain/tags`, `add_tags/remove_tags`), memory kinds list with default persona (`brain_kinds.json`, `/api/brain/kinds`), tools `recall(tags)`, `remember(tags)`, `task_update(tags)`; persona may be any assistant id. No schema migration (tags column existed).
- No `ALLOWED_KEYS` changes (each list has its own file and routes). sw.js SHELL has the three new JS files; VERSION not bumped (the owner bumps on merge). Tests: `tests/test_owner_lists.py`.
- **Unverified**: the new Settings sheets and Memory tag UI were syntax-checked with node but not driven in a browser; Android native background wake keeps its built-in phrases; voice rate/pitch depend on the phone's speech engine; the ARC-Glass look of `.le-*` was not screenshot-checked.
## 3m. Provider-bound features generalised into owner lists (2026-11-13)
Owner rule: build the general feature, never the single example; things are LISTS the owner can add to, edit, switch off, reorder or delete in Settings, today's choice is one ready-made entry. Guide page `docs/guide/24-provider-lists.md`.
- `hub/provlist.py` shared plumbing (JSON list in the data folder, keyring secrets under service `redline-providers`, `Backoff` 1 min doubling to 1 h, validators). UI `js/provlists.js` + `css/provlists.css` (in sw.js SHELL, VERSION not bumped): one list editor used by Settings > Providers, Skills > Sources and the Claude "Agents watched" sheet. No `hub.json` key added or changed.
- Search providers: `hub/searchprov.py`, kinds ddg-html, wikipedia, searxng, brave, json-api (URL template + key placement + field mapping), site (Stack Exchange / domains); first enabled with results wins, fallback + "Searched with X" notes + backoff; `Researcher._configured_search` uses it (`research_search_backend` only seeds). `docs/api/searchprov.md`.
- Vision providers: list in `hub/vision.py` (gemini, claude-login = listed but unsupported unless the client has `ask_image`, ollama via `/api/chat` `images`, openai-compatible); failure falls through; daily cap unchanged; Guarded redaction/usage. `docs/api/vision.md`.
- AI endpoints: `hub/endpoints.py` (OpenAI-compatible list + `OpenAICompatClient`), chosen one is returned by `autopilot.pick_provider` as `endpoint:<id>` and built in `server.model_for`; usage key `endpoint` added to `hub/usage.py`; main assistant routing untouched. `docs/api/endpoints.md`.
- Skill sources: list in `hub/skills.py` (github, git on owner-approved hosts, local folder, direct SKILL.md link); same install approval, no execution, size caps, SSRF-guarded link fetch; the assistant `skill_install` tool stays GitHub-only. `docs/api/skills.md`.
- Coding-agent adapters: `hub/ccsessions.py` list (claude-code ready-made with a validated `resume_argv` template, experimental read-only codex-cli, read-only generic-log; Gemini CLI is only a raw-log preset because its format is not known). Steering only for claude-code with a template. `docs/api/ccsessions.md`.
- Tests: `tests/test_searchprov.py`, `test_provider_lists.py`, `test_skill_sources.py`, `test_ccadapters.py`.
- UNVERIFIED: every real service shape (SearXNG, Brave, Bing, Google CSE, Stack Exchange, Ollama vision, OpenAI-compatible servers, Codex rollout files), the keyring on the owner's Windows, and the new screens in a real browser (source-checked with `node --check` only).

## 3n. Duplicate-push dedupe and CLI login providers (2026-11-14)
- `hub/notifydedupe.py`: shared registry so Watch and Alerts push a Gmail/Google Chat message once (details in `docs/api/watch.md`). `watch.ingest` route now runs Alerts triage BEFORE Watch; `Engine.raise_alert` and `Alerts._deliver` consult the registry. `alerts_enabled` false leaves Watch unchanged. Tests `tests/test_notifydedupe.py`.
- `hub/cliprov.py` + `docs/api/cliprov.md`: owner-editable `cli-login` provider list (`/api/cli-providers`, file `cli_providers.json`), provider name `cli:<id>`, Guarded (redaction) + usage key `cli`, prompt on stdin only, env-strip list, tree kill on timeout, 256 KB cap. Seeds `gemini-cli` (on) and `agy` (off placeholder). Wired in `server.model_for`, `providers.resolve_default`, `settings` (default_provider may be `cli:<id>`), `assistant` (text turns for `cli:*`), `/api/status` (`cli`), `js/providers.js`, `js/provlists.js` (CLI login list, `lines` field type, Test shows stdout/stderr summary). No new settings keys, no new static files (sw.js and VERSION untouched). Tests `tests/test_cliprov.py`.
- UNVERIFIED: the real Gemini CLI flags with stdin in the owner's version (default argv `gemini -p "<instruction>"`, prompt on stdin, per Google's headless docs), the real "not logged in" stderr text, Antigravity's CLI (placeholder), Windows `.cmd` shims and `taskkill`, and the new UI in a browser (node syntax check only).
## 3n. Local models, DeepSeek, deep thinking, bench (2026-11-14)
Owner: i7-1355U, 32 GB, Iris Xe, no Nvidia, so CPU inference through Ollama; asked about DeepSeek. Guide `docs/guide/25-local-models.md`, API `docs/api/models.md`.
- `hub/models.py` (in `FEATURE_MODULES`, routes `/api/models/*`, 4 tools in `server.py` extra_tools): editable catalogue (`<data>/models/catalogue.json`, built-in list incl. deepseek-r1 1.5b-32b, qwen3, qwen2.5-coder, gemma3, phi4, llama3.2, tools/speed `unknown` unless known); pull job (overall percent, cancel, disk check) and remove (`confirm`, refuses the model in use); tools `pull_local_model`/`remove_local_model` are N2; `list_local_models`, `think_harder` free.
- Deep thinking: new config `local_reasoning_model` ("" = off), `deep_think_timeout_min` (20), `deep_think_max_chars` (30000), appended to `ALLOWED_KEYS`/`config.save`. Background job, one at a time, `<think>` split (`ThinkSplitter`), answer shown, reasoning only with `?reasoning=true`, audit/notifications hold counts only. Never reachable from greetings, fast path, voice or the router (test). NOT yet wired into the brain/research/applyq screens as a per-screen "think harder" switch: any module can call `POST /api/models/think` (`kind` rca|synthesis|plan|application).
- Assistant hook (`assistant.py`, small): catalogue `tools` = `none` drops tools for the local model; `text-format` routes local chat through `_text_turns("local")`; `native`/`unknown` unchanged.
- `python -m hub models bench`: 3 fixed prompts per installed model, seconds, tok/s, puzzle + JSON auto-check, saved to `<data>/models/bench.json`, shown in the Models screen. UI `js/models.js` + `css/models.css` (in sw.js SHELL, VERSION not bumped), opened from Settings > Models.
- UNVERIFIED: that every catalogue tag/size exists in the live Ollama library; real tool-calling support per model; speed on the real laptop; whether deepseek-r1 builds honour `think` / return `message.thinking` on the owner's Ollama version (both forms are parsed); the Android/browser look of the new sheet (`node --check` only); real DELETE `/api/delete` body spelling on older Ollama (sends both `model` and `name`).

## 3o. Dictation hotkey on the laptop (2026-11-14)
Hold a hotkey on the LAPTOP, speak, release; local Whisper text is typed into the focused window. Off by default. Guide `docs/guide/26-dictation.md`, API `docs/api/dictation.md`.
- `hub/dictation.py` (in `FEATURE_MODULES`, routes `/api/dictation/status|toggle|settings`, `start(ctx)` lifecycle): lazy `pynput` (or `keyboard`) for hotkey + typing, `sounddevice` through `wakeword.SoundDeviceSource`, transcriber = `ctx.transcriber` on an in-memory WAV (no file; `language` uses the Transcriber's model directly). New config (appended to `Config` and `config.save`, NOT to `settings.ALLOWED_KEYS`; the module has its own validated PUT): `dictation_enabled/paused/hotkey/mode/language/punctuate/add_space/max_seconds/max_chars/use_clipboard/newline/assistant_commands/blocklist/spoken/scratch_phrases/assistant_prefixes` (`null` list = built-in default).
- Safety: blocklist on the foreground title (Windows ctypes, skipped elsewhere) at press, before typing and between chunks; no Enter unless `dictation_newline`; length caps; pause switch; nothing dictated is logged or audited (counts only). "scratch that" backspaces the last chunk if the window title is unchanged. `dictation_assistant_commands` ("send to Jarvis ...") reuses `wakeword.default_assistant`.
- UI: `js/dictation.js` + `css/dictation.css` (in sw.js SHELL, VERSION not bumped), row in Settings (one line in `settings.js`), hotkey recorder only builds the string in the browser. `hub/doctor_checks.py` lists `hub.dictation` (its `doctor_line`). `requirements-extra.txt` has `pynput`. Tests `tests/test_dictation.py`.
- UNVERIFIED: real global hotkeys, pynput SendInput on Windows (and apps that drop typed keys, hence the clipboard option), the ctypes foreground-title/process lookup, the tkinter clipboard fallback, Whisper accuracy on short clips and with `language` set, ctrl+letter hotkeys under pynput (control characters are mapped back), the keyboard-package backend, the new sheet in a browser (`node --check` only).
## 3o. Form pre-fill for the application queue (2026-11-14)
- `hub/formfill.py` (in `FEATURE_MODULES`, routes `/api/formfill/*` and `/api/applyq/{id}/formfill/*`, docs `docs/api/formfill.md`, guide 20). Playwright is optional (`requirements-extra.txt`, owner runs `python -m playwright install chromium`; falls back to the laptop's Chrome/Edge). Persistent profile `<data>/formfill_profile`; no code reads cookies (tested by a source grep). One session at a time on a single worker thread; 5 min idle, 30 min stale, 20 s between starts, daily cap = `applyq_daily_cap`.
- Pure mapper (`parse_page`, `plan_form`, `find_controls`, `detect_blockers`, `judge_result`) is the tested core; the browser is behind the `Driver` interface (tests use a fake; `PlaywrightDriver` is tested with a fake Playwright object model plus one real-browser test that skips when none is installed).
- Submit safety: `field_digest` over URL + field layout + live values, the screenshot route records that it was served, mismatch -> 409 + fresh capture. `safety.ALWAYS_ASK` gains `submit_form`; tools `fill_application_form` (N2) and `submit_form` live in `applyq_tools.py`. `Applyq.formfill_result` records applied / needs check.
- Config appended: `formfill_enabled` (false), `formfill_headed` (true), `formfill_never` (["linkedin.com"], owner-editable list saved as `formfill_never.json`). Not added to the Settings allow-list: turn it on by editing `hub.json`.
- UI: `js/applyq.js` review sheet + `css/applyq.css` (no new files, so no SHELL/VERSION change).
- UNVERIFIED: real job sites and ATS quirks (iframes, shadow DOM, custom widgets are not read), Chromium on Windows, the visible-window login flow on the owner's laptop, the sheet on a real phone at 390px (UI is source-checked only), a hidden window with `formfill_headed` false against a real site.
## 3o. MCP client (2026-11-14)
Owner rule: general, an editable list. The owner adds MCP servers (stdio or streamable HTTP); their tools become assistant tools `mcp__<server>__<tool>`, every call gated. Docs `docs/api/mcp.md`, guide `docs/guide/27-mcp-servers.md`.
- `hub/mcpclient.py` (in `FEATURE_MODULES`; client, transports, list store `McpList` on `provlist.ProviderList`, policy, `McpManager`, routes `/api/mcp`, `start()` stops servers), `tests/test_mcp.py` + `tests/fake_mcp_server.py`, UI `js/mcp.js` + `css/mcp.css` (Settings card in `settings.js`; in sw.js SHELL, VERSION not bumped). Config keys appended: `mcp_enabled`, `mcp_idle_seconds`, `mcp_max_concurrent`, `mcp_call_timeout` (hand-edit; not in `ALLOWED_KEYS`).
- Small hooks in `assistant.py`: `ExtraTool(always_ask, sensitive, reason)`, `AssistantService(dynamic=)` + `_sync_dynamic()` (re-read at each owner message) + `set_mcp_group()` / `TOOL_GROUPS["mcp"]`, array-of-non-strings accepted by `validate()`. `server.py`: one `McpManager`, `dynamic=`, `ctx.mcp`.
- Security rules (tested): results untrusted (taint); every tool asks every time incl. Full Control/auto-approve (no remembered grants) unless server trust is read-only-free AND that tool marked read-only AND its fingerprint unchanged; name/description keywords (shell, exec, delete, remove, send, ...) force N3 whatever the marks; owner can raise to N3 only; descriptions sanitised, delimited, never affect policy; server requests (sampling, roots) refused; env allow-list, secrets in keyring (`mcp:<id>`), scrubbed from errors; process tree killed; no assistant tool adds servers; adding/enabling/changing/testing needs `confirm: true`.
- Starter examples (filesystem, fetch, git, memory, sequential-thinking, time) are only suggestions in `GET /api/mcp` `presets`, never in the list.
- UNVERIFIED: any real MCP server and the example command lines, Windows (`npx.cmd`/`uvx` resolution, `taskkill`), protocol revision 2025-11-25 (not offered), HTTP servers needing OAuth, legacy HTTP+SSE servers, real keyring size limits, the Settings sheet in a browser (`node --check` only).
## 3o. Work lanes: routine work to the free Council first (2026-11-14)
Owner: do not waste Claude/Gemini tokens on routine work. General, not specific: lanes and task classes are editable lists. Guide `docs/guide/28-work-lanes.md`, API `docs/api/lanes.md`.
- `hub/lanes.py` (appended to `FEATURE_MODULES`, routes `/api/lanes/*`): `LANES` (id, label, kind council|local|local-reasoning|gemini|claude-login|claude|cli:id|endpoint:id, enabled, cost free|limited|paid, daily_cap, notes, escalate_claude) and `CLASSES` (routine, research, coding, hard-reasoning, private with ordered lane ids) in `<data>/lanes.json`; one automatic lane per switched-on `cli:*` provider. Runs in `<data>/lanes_runs.json` (100 kept).
- Escalation: first lane that can start; next only on failure/timeout/unavailable, Council verdict not accepted (verify failed, stopped, no answer), or the owner's "try a stronger model" (`POST /runs/{id}/stronger`); each attempt records why; lanes over `daily_cap` or `usage.cap_reached` are skipped with a note. Council runs reuse `CouncilManager` (one at a time; busy = unavailable) and are watched by a background task; quick lanes use `ctx.model_for` (Guarded, so redaction + usage counting apply); `local-reasoning` uses the Models deep-thinking engine. `escalate_claude` on a council lane still needs `council_allow_claude`.
- Private classes (`private: true`) can only use council (started with `--only=` its local models, from `lab models --json`) and local kinds, enforced in code regardless of the list; they end `needs_owner` rather than going to a cloud lane. "Saved" = a free lane produced the accepted answer (`GET /saved`, 7 days).
- Tools `run_in_lane` (asks, preview names the first lane and cost) and `lanes_status` in `hub/lanes_tools.py` (wired in `server.py` extra_tools), TOOL_GROUPS `lanes`, one sentence added to the Jarvis and Friday prompts in `persona.py`. Brain link: `link {kind:"brain", id}` makes the finished run add a note to that item via PATCH `/api/brain/items/{id}`.
- UI: `js/lanepicker.js` + `css/lanes.css` (in sw.js SHELL, VERSION not bumped); chip row in the Talk composer (non-Auto lane replaces the chat send), "Send to Council" on Brain task cards and Today task rows, Build flow "Run this step with" (Council = old tick flow; other lanes show a one-off answer and tick nothing), Applications draft editor "Write draft with" (small guarded edit in `js/applyq.js`), "Saved calls this week" tile on Council, Settings > Work lanes (`openLanes` in `js/ownerlists.js`). No hub.json key added.
- Tests: `tests/test_lanes.py` (fake council, fake model clients, fake think port; one test drives the real CouncilManager with its fake lab process).
- UNVERIFIED: a real Council run through a lane (argument shape follows the tested `CouncilManager`), real `lab models --json` `kind: "local"` values for the private filter, real Gemini/Claude clients through `model_for`, `providers.availability` skip messages, and every new UI piece in a real browser (`node --check` only; 390px overflow not screenshot-checked).
## 3o. Presence-aware delivery (2026-11-14)
Where the owner is decides how each notification reaches them. Docs `docs/api/presence.md`, guide `docs/guide/30-presence.md`.
- `hub/presence.py` (appended to `FEATURE_MODULES`; routes `/api/presence/*`, `start()` samples every 15 s): states at_laptop|near|away|phone_active|in_meeting|focus|quiet_hours|unknown from injectable signals (Windows idle via `GetLastInputInfo`, lock via `OpenInputDesktop`, phone heartbeat `POST /api/presence/phone`, calendar feeds via `calendars.meetings_between`, manual focus / away / back overrides that expire); 60 s dwell between location states. Editable delivery rules (`presence_rules.json`), held items released as one digest, spoken lines queue for the open phone app, editable voice phrases (`presence_phrases.json`), Windows toast via PowerShell with the title on STDIN only.
- Hooks (additive): `proactive.submit` calls `presence.route()` for 'now'/'batch' (None = old behaviour: feature off, unknown/quiet_hours, no rule, paused, `hub.panic` active when it exists); `alerts._deliver` asks `presence.alert_actions()` before speaking; `assistant._presence_say` (fast layer, before `_brain_save`); `NotifyCenter.push(phone=True|"silent"|False)`.
- Config appended: `presence_enabled` (true), `presence_laptop_toasts` (false); not in `ALLOWED_KEYS`. UI: `js/presence.js` (heartbeat, Home presence row, Settings > Delivery rules); in sw.js SHELL, VERSION not bumped. Tests `tests/test_presence.py`.
- UNVERIFIED: Windows `GetLastInputInfo` / `OpenInputDesktop` lock heuristic (a secure prompt also reads as locked), the PowerShell toast on a real desktop, the `hub.panic` hook names (guessed: is_active/active/panic_active), the phone heartbeat from the Android app/PWA with the screen off, the new UI in a browser (`node --check` only).

## 3o. Agent controls: Panic, trust ladder, audit screen (2026-11-14)
Guide `docs/guide/29-agent-controls.md`; API `docs/api/panic.md`, `trust.md`, `audit.md`, `assistant-safety.md`.
- `hub/panic.py`, `hub/trust.py`, `hub/auditview.py` (appended to `FEATURE_MODULES`). `server.py` sets `ctx.assistant`. Config appended: `panic_active` (route-only), `panic_confirm`, `panic_phrases`, `trust_enabled`, `trust_threshold` (all but `panic_active` in `settings.ALLOWED_KEYS`).
- Panic: persistent `config.panic_active`; every engine's `enabled` ANDs `not panic.on(config)` (proactive, autopilot, opps, alerts, buildflow, wakeword, dictation, mcp, formfill), so Settings are never rewritten and Resume only restarts threads/servers. Hooks via `panic.register(name, stop, resume, ctx=ctx)` / `register_flag` in each module's `make_router`; start routes of applyq send, formfill start/submit, opps run, council run, lanes run, models think, research start answer 423 while paused. `assistant._call_tool` refuses gated/mutating/outbound tools; `notify._maybe_push_phone` holds ntfy back (source `panic` passes). Voice phrase is matched at the top of `assistant.run`. `GET /api/panic/arm` gives a 5 min token for `X-Panic-Token`. Resume never turns Full Control back on.
- Trust: `AssistantService.trust` (set by `trust.make_router`); `_call_tool` claims a rule before asking and records taps; `_sync_full_control` and the Settings route revoke on any Full Control change; `resolve()` stores `meta["via"]` (tap/voice). Audit `assistant_tool` gained `by` and `trust_rule`.
- UI: `js/panic.js`, `js/trust.js`, `js/audit.js`, `css/panic.css`, `css/audit.css` (sw.js SHELL, VERSION not bumped); one line each in `main.js`, `home.js` (quick actions), `settings.js` (Agent controls group).
- Tests: `tests/test_agent_controls.py` (fakes).
- UNVERIFIED: the UI in a real browser (no screenshots, 390px not checked), real wake word/dictation/MCP/formfill/council stop paths (only the registry and fake hooks are tested; each module hook is not exercised individually), Windows process behaviour, whether the wake word thread restarts cleanly after `stop()` on Resume, the 423 guards on start routes (source-checked only).

## Bug bash (2026-11-14)
Integration pass over the real app (fakes only). New tests: `tests/test_bootcheck.py` (modules load, no duplicate method+path routes, tool names/schemas, TOOL_GROUPS names,
sw.js SHELL and main.js VIEWS files exist, relative JS imports resolve, `node --check` on every static JS, ALLOWED_KEYS vs Config, every Config key saved + round-trips, create_app < 3 s),
`tests/test_api_smoke.py` (every GET and every write route with `{}`/junk bodies: no 500 on a first run, start/stop leaves no threads or never-awaited tasks), `tests/test_cross_feature.py`
(taint after every untrusted tool blocks every changing tool, outbound tools ask in Full Control, new-tool results redacted for Claude, a fake keyring's secrets never in any GET response, audit or data file,
alert -> notifications -> phone voice queue -> proactive log), `tests/ui_smoke_check.js` + `tests/test_ui_smoke.py` (all 40 screens at 390x844 and 360x740: console/page errors, failed /api calls, blank views,
elements past the right edge; screenshots go to the folder given as argument; skips when node/playwright/browser are missing). `tests/apiwalk.py` flattens FastAPI's nested included routers.
Found and fixed:
- `search_files` was defined twice (built-in file-name search and an `assistant_tools_more` Ask-my-files tool): duplicate tool names are rejected by cloud APIs and the extra silently replaced the built-in at run time. The extra is now `search_documents` (also in the `search` tool group).
- `screen_explain` schema had no `required` key: `validate()` raised KeyError and ended the whole assistant turn ("Assistant failed: 'required'"). Added the key and made `validate()` tolerant.
- `Config.save()` dropped `applyq_enabled`, `applyq_daily_cap` and the four `mcp_*` settings (changes made from Settings reverted at the next start).
- Autopilot "off" screen showed the text `null` (a null child passed to `replaceChildren`).
- Settings > Mail accounts overflowed the card on phones (it reused Autopilot-only CSS classes; long buttons did not wrap).
- Dev server `FakeAssistant` lacked `grants/revoke/revoke_all`, so Settings > Active permissions 500'd there.
Checked and fine: no GET/POST/PUT/DELETE route answers 500 (only deliberate 501/503 "not set up"), route table has no clashes, startup < 1 s, Full Control still asks for send_email/send_application/post_publish/submit_form.
Not fixed / notes: `chat_send` (Google Chat post) is approval-gated but not in `safety.ALWAYS_ASK`, so Full Control sends it without a tap (design call, left as is). Restarting the same app object in one process fails in modules that pop their engine in `stop()` (council, lanes, watch, autopilot, friday, automations); real runs build one app per process, so left alone.

## 3p. Jarvis/Friday-grade voice: local TTS, streaming, barge-in (2026-11-14)
Guide `docs/guide/31-voice.md`, API `docs/api/tts.md`.
- `hub/tts.py` (appended to `FEATURE_MODULES`, routes `/api/tts/*`, tests `tests/test_tts.py`): engines `browser` (default, unchanged behaviour), `sapi`, `piper`, `command` (owner argv, validated: no shell, no eval flags, text only on stdin, `{out}` the only placeholder). Per-sentence disk cache (200 phrases), Piper downloads only from a Settings button with confirm + size warning (`<data>/tts/piper`), settings in `<data>/tts/tts.json`. Config appended: `tts_enabled` True, `tts_engine` "browser" (not in `ALLOWED_KEYS`; Settings > Voices writes `tts.json`). `assistants.py` voice gains `engine` (additive, old lists read `""`).
- Browser: `js/speechchunk.js` `createSpeechPlanner` (skips code/tables/long lists/URLs), `js/speechqueue.js` (ordered queue, 3 s per-sentence fallback to the phone voice, `cancel()`), `js/hubtts.js`, `js/voicemode.js` speak path rewritten on them (speaking indicator, Stop talking button, barge-in by voice with echo floor + headphones mode, "okay okay" stop word; interrupting mid-reply listens on the side and carries the words over), Talk tab "Read answers aloud" uses the hub voice when chosen. `js/voices.js` = Settings > Voices (assistant voices, commands, Piper, Test voice). `css/voicemode.css` additions; `sw.js` SHELL entries (VERSION not bumped).
- Laptop: `WakeListener` default speaker and alerts go through `tts.laptop_speaker` (old one-liner as fallback and for the browser engine), persona picks the voice, `_maybe_barge` stops speech in headphones mode only, presence (in_meeting/focus) keeps it silent, `panic.register("voice", ...)` guarded.
- UNVERIFIED: Piper flags and models on Windows, real audio and playback gaps, the Hugging Face catalogue/sizes, barge-in thresholds on a real phone (speaker echo), `hub/presence.py` and `hub/panic.py` APIs (neither exists in this checkout; calls are guarded duck-typing), SAPI script on a real Windows box, the Voices sheet at 390px in a browser (`node --check` only).
## 3o. Meeting assistant (2026-11-14)
Record a meeting on the LAPTOP (consent first), transcribe locally, notes + action items into the Brain only on the owner's tap. Docs `docs/api/meetings.md`, guide `docs/guide/32-meetings.md` (has the legal/consent note: tell participants, check OneStack policy).
- `hub/meetings.py` (appended to `FEATURE_MODULES`; routes `/api/meetings/*`; `start()` ticks every 20 s: time limit + calendar prompt every minute), `hub/meetingtext.py` (stitching, due dates, no-AI extractor, `clean_notes`, search date hints), `hub/meetings_tools.py` (`meeting_list|summary|search` untrusted, `meeting_start` asks, `meeting_stop` free; TOOL_GROUPS `meetings`; ownership lives in the tool text and the saved personas: decisions -> jarvis, commitments -> friday, because the persona prompts are length-capped by tests).
- Audio: `MicSource` (sounddevice) and `LoopbackSource` (Windows WASAPI loopback via optional `soundcard`), both lazy with plain "install X" messages; 25 s chunks, 2 s overlap, `stitch()` removes the repeated words; silence is never transcribed; `speech.Transcriber.transcribe_pcm` (new, in memory, no file). Transcript segments `{t, track me|others, text}` in `<data>/meetings/<id>/meeting.json` (0600). `meetings_keep_audio` adds WAVs (warning).
- Consent: `confirm` + `told_participants` once per calendar title (`consent.json`), pill in the app (`js/meetings.js mountIndicator`, one line in `main.js`), `meetings_max_minutes` auto-stop, `panic.register("meetings")` (no resume), calendar start only sends the proactive prompt `meeting_prompt`.
- Notes job: lanes class `routine` / `private` (`POST /api/lanes/run`, polled), else `ctx.model_for("local")`, else the pattern extractor; non-private text is redacted first and restored after. Brain: `GET /proposal` then `POST /apply` (picked, editable; reminder 09:00 on the due date). "You owe" on the front page (`today._owe_list`, `frontpage.render`) and the spoken morning brief.
- Config appended: `meetings_enabled` (false), `meetings_keep_audio` (false), `meetings_max_minutes` (120), `meetings_model` ("base"); changed via `PUT /api/meetings/settings`, not the Settings allow-list. UI: Apps > Meetings (`js/meetings.js`, `css/meetings.css`, in sw.js SHELL, VERSION not bumped). Tests `tests/test_meetings.py` (fakes only).
- UNVERIFIED: Windows system-audio loopback with `soundcard` (never run on Windows), real faster-whisper accuracy on meeting audio and CPU speed vs the 25 s chunk (a slow CPU drops chunks and warns; try `tiny`), real sounddevice mic, the UI in a real browser at 390px (no screenshots), a real lane/Council answer for the JSON notes prompt, real calendar feed timing for the prompt.

## 3q. Computer control (2026-11-14)
Jarvis looks at the screen and clicks/types in owner-approved apps, step by step. OFF by default. Docs `docs/api/computeruse.md` (has "What it will never do"), guide `docs/guide/33-computer-control.md` (warning + Notepad first test).
- `hub/computeruse.py` (appended to `FEATURE_MODULES`; routes `/api/computer/*`; tools via `computeruse.build`): pure validators (`validate_action`, `parse_chord`, `blocked_reason`, `destructive_hits`, `password_like`, digests), `Job`, `ComputerUse` engine (one background thread per job, injectable screen / inputs / windows / uia / proposer / presence / clock / sleep / banner / hotkey), real drivers (mss+Pillow via `screen.RealScreenSystem`, pyautogui FAILSAFE on with pynput fallback, ctypes window list + `OpenInputDesktop` secure-desktop test, pywinauto UIA, tkinter banner with STOP, pynput Ctrl+Alt+Shift+Esc), `VisionProposer` (uses the `hub/vision.py` provider list through `Vision.client_for`, so cloud text still goes through `redact.Guarded`; the picture is masked for password fields; local Ollama is the default provider, a cloud one only when chosen in the scope).
- Permission model: pending record + scope digest -> `approve` (digest must match) -> N2 steps inside scope; destructive-looking steps (reason / control name / window title keywords, Delete key) wait for `confirm-step` with a per-step digest and read no trust, grant or Full Control state. `safety.ALWAYS_ASK` gained `computer_run` (`tests/test_cross_feature.py` set updated). `computer_step_confirm` is a route only, never a tool. `panic.register("computeruse")` (resume restarts nothing), presence `away` refuses/pauses unless `computeruse_allow_unattended`, cursor-jump auto-pause (checked before every action too), step / time / 20-per-minute limits, audit rows per step (typed text cut to 40, hidden for password-like).
- Config appended (not in Settings allow-list; `PUT /api/computer/settings`, enabling needs `confirm: true`): `computeruse_enabled` false, `_allow_unattended` false, `_keep_shots` false, `_max_steps` 25, `_blocklist` None (built-in list), `_allow_chords` None. `requirements-extra.txt` gained pyautogui, pywinauto (comments). UI: Apps > Computer control (`js/computeruse.js`, `css/computeruse.css`, in sw.js SHELL, VERSION not bumped, `main.js` VIEWS + `apps.js` entries). TOOL_GROUPS `computeruse`. Tests `tests/test_computeruse.py` (fakes only).
- UNVERIFIED: real clicking and typing on Windows, pywinauto lookups and `CurrentIsPassword`, the secure-desktop test, vision accuracy for coordinates, tkinter banner and pynput hotkey on a real desktop, the UI in a real browser at 390px (`node --check` only).
## 3q. Playbooks: "when X happens, do Y" (2026-11-14)
An editable list of owner-written rules (event -> steps). Docs `docs/api/playbooks.md`, guide `docs/guide/34-playbooks.md`. No playbooks exist by default; new ones start off.
- `hub/playbooks.py` (appended to `FEATURE_MODULES`; `start()` ticks every 20 s: schedules + pollers for presence, calendar, due tasks, mail). Trigger registry `playbooks.register_trigger(...)`, other modules announce with `playbooks.emit(ctx, trigger, event)` (hooks added in `alerts.ingest` (message, alert), `meetings.process` (meeting-finished), `applyq._announce_ready` (opportunity-ready); `panic.register("playbooks")` emits panic-resume). Schedules reuse `automations.validate_schedule/prev_slot/next_slot`.
- The gate is REUSED: every tool step runs through `AssistantService._call_tool` in session `playbook:<run>` (approval cards become pending approvals on the run, ALWAYS_ASK, N3 CONFIRM, trust rules, Panic, taint). Only change to `hub/assistant.py`: `session.force_gate` (names forced to ask, used by create_task/add_memory) and the `playbooks` TOOL_GROUPS entry. Runs from outside-text triggers start tainted. Steps: notify, draft_reply, ask_assistant (lanes routine/private, redacted, event text in `<data id=..>` markers), create_task, add_memory, run_tool, set_focus, wait, if. Denied tools: shell, type_text, settings, Full Control, Panic, computer control, playbook edit/enable.
- Loop protection (chain via a ContextVar, 2 hops, no self), max concurrent (`playbooks_max_concurrent`), daily cap, cooldown, 10 starts/min breaker, per-run timeout; dry run ("Test with a sample event") executes nothing; history in `<data>/playbook_runs.json`, audit events `playbook_*` (no secrets, text <= 200 chars). Webhook `POST /hooks/playbook/{id}` (secret hashed, shown once).
- Assistant tools `playbook_list`, `playbook_run`, `playbook_enable` (asks); it CANNOT create or edit playbooks (owner edits in the UI only). Config appended: `playbooks_enabled` (true), `playbooks_max_concurrent` (3). Small edit in `proactive.py`: type `playbook` joins NOW_TYPES.
- UI: Apps > Playbooks (`js/playbooks.js`, `css/playbooks.css`, SHELL entries, VERSION not bumped, `main.js` + `apps.js` entries). Starter templates (5, labelled examples, added disabled). Tests `tests/test_playbooks.py` (fakes only).
- UNVERIFIED: the UI in a real browser at 390px (node syntax check only), a push with Approve/Deny buttons (the notify layer has none: the push opens the Playbooks screen), real calendar/mail/presence polling shapes (`calendars.meetings_between`, `gmailx.recent`, `presence.state`), real lane answers for `ask_assistant`, the `alert` event for a real Android capture.

## 3m. Job preferences + scheduled batches (2026-11-14)
- `hub/jobprefs.py` (profiles in `jobprefs.json`, detection, hard filters + soft points, routes `/api/jobprefs`, docs `docs/api/jobprefs.md`), `hub/batches.py` (slots, builder, sender, routes `/api/batches`, tools `batch_list`, `batch_prepare`, `batch_send`; docs `docs/api/batches.md`), screens `js/jobprefs.js`, `js/batches.js`, `css/jobprefs.css` (Apps > Job preferences, Apps > Batches; in `sw.js` SHELL, VERSION not bumped), guide `docs/guide/35-job-preferences-and-batches.md`. Both appended to `FEATURE_MODULES` (`jobprefs`, `batches`); `batches.build` in `server.py`.
- Finder hook (small, additive): `opps.ingest` calls `jobprefs.assess` (hard filter -> status `filtered`, never queued, hidden from `/finds` unless asked; soft points re-rank; `find.match` stored); sources may carry `remote: true` (starter remote boards do); `push_to_queue` passes `meta{source_id, match}` and `applyq.add` stores `item.meta` (`clean_meta`). No profile = nothing filtered; pasted links exempt.
- Remote detection is pure code (location line + title + description, negations, hybrid/remote-friendly, "remote sensing" ignored, region restriction like "Remote (US)"/"US only"/"must be located in"/"authorized to work in", company-address phrases ignored). The owner's region (`owner.regions`, `honour_region`, owner aliases) turns "remote but US-only" into a hard reject; unset = warning only.
- Batches: hard rule building never sends; sending only via `POST /{id}/send {confirm:true}` or `batch_send` (added to `safety.ALWAYS_ASK`; exact-set assertion in `tests/test_cross_feature.py` updated). Email through `Applyq.send`; forms through `ctx.formfill` (set in `formfill.make_router`) one by one honouring the 20 s gap; ANY captcha/login/block/error or unclear submit stops the batch; manual hosts (LinkedIn, Indeed fixed + owner list) never automated. The batch tap stands for the form's "I looked" tap (screenshot captured, digest still checked): documented trade-off.
- Scheduling: `tick()` every 30 s via `automations.prev_slot`; catch-up once within `batches_catchup_min` (default 360), older misses audited and skipped; new slot never fires for a past time; a new batch replaces the slot's untouched older one. ONE `batch_ready` notification through proactive/presence (registered type, default now). Config appended: `batches_enabled`, `batches_catchup_min`, `batches_outreach_cap` (dataclass, `config.save`, Settings allow-list and validation, settings view).
- Tests `tests/test_jobprefs_batches.py` (detection edge cases, evaluator, finder hook, slots/build/schedule/send with fake Gmail + fake formfill + fake clock, routes).
- UNVERIFIED: real postings and non-English text, real Gmail/form sites through batches, waking from sleep, presence routing on the phone, the screens in a real browser at 390px (node syntax check only), outreach items (this lane makes kind message/dm/outreach items sendable but nothing else creates them yet).
## 3r. Source connectors for the opportunities finder (2026-11-14)
`hub/oppsources.py` (adapters rss/json/html/email/webhook/ats/hn/link, preset catalogue, company watch list, engine, routes, tools `sources_list/add/test`), `hub/oppsnorm.py` (pure: remote, pay, employment, posted time, dedupe keys, small CSS selector engine), `js/oppsources.js` (Settings > Job and gig sources). Docs: `docs/api/oppsources.md`, `docs/guide/37-job-sources.md`. Settings: `oppsources_enabled` (off), `oppsources_interval_min`, `oppsources_watch_interval_min`, `oppsources_stale_days`, `oppsources_ghost_days`.
- Hands new, non-duplicate, non-stale items to `opps.Opps.ingest` (additive edit there: `row["meta"]` is copied onto the find, and an `early` find reaches the queue regardless of `opps_min_score`). The source list starts empty; presets are added by tap.
- Platforms that forbid bots (Upwork, Fiverr, Freelancer, Toptal, Contra, AI-task marketplaces, LinkedIn, Indeed) are never fetched: email-alert or open-link only, `apply_method platform_manual`, `auto_submit false`. Mail bodies are read with destination `local`.
- Freelance hook `oppsources.proposal_context(item)` (task summary + budget) for the proposal writer; per-item `proposal` slot and `bid` status.
- First successful run of a watched source is a baseline (no Be early); after that new roles notify via `proactive.submit_or_notify`.
- UNVERIFIED: only Lever's endpoint/fields were checked against its public docs (github.com/lever/postings-api, 2026-11-14). The sandbox reached only github.com, so Greenhouse, Ashby, Workable, SmartRecruiters, Recruitee, Teamtailor and all remote boards are marked `unverified` (parsers are defensive). Teamtailor's RSS shape, Lever `createdAt`, and the email sender domains need a first real run. No real network, mail account or phone UI was exercised; tests use fakes.
## 3r. Tailoring, fit and honesty check, answer bank (2026-11-14)
- `hub/factbase.py` (facts with ids from the private profile + owner-confirmed facts + preferences, `factbase.json`), `hub/fitcheck.py` (requirements matched/partial/missing, score, flags, `check_claims` honesty rules), `hub/tailor.py` (per-job resume variant, cover note / proposal per channel, optional AI wording, variants per application, diff, md/txt/docx/pdf via stdlib writers, apply to an application draft; also the 3 assistant tools), `hub/answerbank.py` (owner-editable Q&A, fuzzy local matching with confidence, slots, AI drafts as pending). Routes `/api/tailor/*`, `/api/answerbank/*`; screen `js/tailor.js` + `css/tailor.css` (Apps > Tailor and answers; SHELL entries, VERSION not bumped). Docs `docs/api/tailor.md`, guide 36.
- Rule: every generated sentence cites fact ids; a sentence whose numbers/names/tools are not in the cited facts is `unbacked` and BLOCKS the variant (export of the cover, and `Applyq.send` through a visible `[FILL: ...]` line) until the owner removes, rewrites or confirms it. Nothing sends. Owner-only questions (demographic, ID, DOB, criminal, bank, consent) are never auto-filled; AI answers are saved pending and used only after approval; low confidence is only a suggestion.
- Additive touches: `applyq.draft` (bank answers for topics the posting mentions), `formfill` (bank answers merged into `_answers`), `settings.ALLOWED_KEYS` + config fields `tailor_enabled`, `tailor_ai`, `answerbank_enabled`, `answerbank_min_confidence`, `answerbank_embeddings`, `assistant.TOOL_GROUPS["tailor"]`, `FEATURE_MODULES`.
- Tests `tests/test_tailor.py` (fake models verify prompt rules, fact-grounding and the block on unbacked claims; fake embedder).
- UNVERIFIED: the screen in a real browser at 390px (node syntax check only), real local-model JSON quality (small models may fall back to the template), embeddings with a real Ollama model, PDF/DOCX opened in real Word/Acrobat/ATS (structure and the hub's own reader checked only), currency conversion (pay is compared as written), the fit heuristics on real postings in other languages.
## Bug-fix round: Learn study library (2026-11-15)
Owner: "Flashcards etc. should let me upload files from phone and laptop." Root cause: the Learn source picker only read one note, one shared-folder file, a URL, pasted text or a topic; there was no upload and no library, so nothing could be reused or multi-selected.
- `hub/studylib.py` (library, worker-thread reading, chunk + embed with keyword fallback, routes, tools `study_library_list`/`study_import_path`), `js/studylib.js` (Add material sheet: this device with per-file progress and retry, laptop shared-folder files multi-select, paste/link; library list with rename/tags/re-index/delete/cancel). New `library` source type in `study.py`/`cards.py`; Library tab in the shared source picker (Chat, Quiz, Flashcards, roadmaps); Ask files scope switch (`kb.py` `library` ids); English Fix writing "Add material". Flashcards gain `difficulty`. Laptop import is individual files only (no folders), Files roots/permission model; outside roots only via the assistant after approval. `sw.js` SHELL has `/js/studylib.js` (VERSION not bumped). Docs: `docs/api/studylib.md`, guide 01 (Learn section). Tests: `tests/test_studylib.py`.
- UNVERIFIED: real phone file picker / Android document provider, big real PDFs, OCR (Tesseract or a vision provider), real Ollama embeddings, the screens in a real browser.
## Bug-fix round: lag and Android Back (2026-11-15)
- Lag causes: 94 backdrop-filter layers on small controls, looping box-shadow/text-shadow glows (idle Home burned ~30% of a 4x-throttled CPU in style recalcs), 8 always-on timers, five separate shell polls (alerts, presence, meetings, panic, unread) plus a 30 s presence beat, duplicate GETs per screen open, Remote screen defaulting to 4 fps frame polling. Now: `css/perf.css` (blur only on bar and dock, no looping glows, paused animations while hidden, `prefers-reduced-motion` path), `js/poller.js` + `hub/ticker.py` (one `GET /api/ticker` with ETag/304, 15 s Home / 30 s elsewhere, paused while hidden; presence beat on change or once a minute), `api.js` dedupes identical in-flight GETs and reuses answers for 2 s (cleared by any write), Home caches "Google not connected" 10 min and delays the setup checks, Remote screen streams by default with a 1-2 fps fallback, SW serves `/vendor` and `/icons` cache-first. Idle Home task time 930 -> 360 ms per 3 s; scroll 850 -> 80; timers 8 -> 2.
- Back: `js/navstack.js` + `main.js`. History is `[exit base, Home, ...]`; tab switches replace; sheets/modals are layers (close first); Home Back toasts "Press back again to exit" (2 s window, next system Back leaves). Refresh/deep link restore from the hash. Native: `@capacitor/app` added to `mobile/package.json`; the web router listens to its `backButton` (and a `hub:back` window event) and calls `App.exitApp()` only on the second Home press. Run `npm install && npm run sync` in `mobile/` to wire the plugin.
- Tests: `tests/test_ticker.py`, `tests/test_ui_nav_perf.py` (`nav_back_check.js`, `perf_check.js`).
- UNVERIFIED: a real Android phone (system Back, Capacitor `backButton`, PWA exit), `npm install` of `@capacitor/app` (lockfile not regenerated), real frame rates.
## 3s. Bug-fix round: Friday/Jarvis on the phone (2026-11-15)
- Root causes: (1) `auto`/default only knew the Gemini API key, Claude key/login and local; a Gemini CLI login (`cli:gemini-cli`) was never considered and a stale saved `gemini` pick with no key returned the bare key error. (2) Jarvis screen text box was disabled unless Jarvis was running AND `remote_inbox.py` (docs/jarvis-remote-inbox.md) was installed, so with the patch missing the owner could only open Jarvis and talk at the laptop. (3) Phone speech: `ctx.speak` did `cancel()` then one long `speak()` with no voice wait, no gesture unlock, no chunking, no per-screen switch or message. (4) The hub console was blank: nothing was printed after one line.
- New: `hub/provchain.py` (fallback chain: asked/default, Claude key, Gemini key, CLI logins in list order, Claude login, endpoints, local; never leaves local for cloud; falls back only before the first word; `failure_message` names each provider, the exact fix/command), `providers.resolve_default` counts installed CLI logins, `server.model_for` wraps cloud/auto picks in the chain (injected test fakes are left alone), `hub/fixit.py` (`/api/fixit/providers|providers/{id}/test|checklist|log|doctor`), `hub/consolelog.py` (startup banner + `hub-console.log`, used by `python -m hub serve`), `js/voicekit.js` (late voices, first-tap unlock, per-sentence queue, Voice on/off, Test voice, plain no-voice messages; hub voice when configured), `js/fixpanel.js` + `css/fixkit.css` (Voice, Fix-it checklist, Provider check with Test buttons, Live log + Doctor) on both Friday and Jarvis screens. Jarvis box is always usable: Send says "not running" with Start, or offers "Ask Jarvis here" (Talk screen, text handed over) when the patch is missing; queued commands show Jarvis's new log lines. Docs `docs/api/fixit.md`. Tests `tests/test_fixit.py`, `tests/test_fixit_ui.py` (390px Playwright, fake phone voice).
- UNVERIFIED: real Android Chrome/Capacitor speech and autoplay, a real Gemini CLI login and its argv (`gemini -p`), the separate Jarvis project's own "Gemini unavailable" text (that string is not in this repo; if it comes from Jarvis itself, check its Gemini key/keyring `jarvis-gemini`), the remote-inbox patch inside a real Jarvis.

## Bug-fix round: Explain this screen and Remote typing (2026-11-15)
Owner: "Explain this screen did nothing" and "Remote control has no typing pad". Root causes: (1) /api/vision/explain answered 503 when no Gemini key was saved (a pulled Ollama vision model was ignored) and the Screen tab only wrote the error into a card far below the fold, so nothing visibly happened; (2) the keyboard existed only as an icon-only HUD button feeding an invisible 1px textarea (Android does not reliably open the keyboard for it), with no visible field, no send-text box and no PgUp/PgDn/Win/F5/Space.
- Vision: auto-detects Ollama vision models, `GET /api/vision/setup`, `POST /api/vision/test`, phone-screen text explain (`source: "phone"`); the UI uses sheets with progress, the exact reason, Set up vision (providers, Test, one-tap model download via /api/models/pull), and "Explain the app screen instead".
- Remote screen: labelled Keyboard button opening a visible live-typing field (sentinel so Backspace works) plus Send text (clipboard paste, unicode) and Send + Enter; more keys (`win`, PgUp/PgDn, F5, Space); Drag toggle for trackpad mode; stream is the default with automatic fallback to 1-2 fps polling. Hub input rules and audit (no typed text) unchanged; bare `win` key now allowed.
- Tests: `tests/test_vision.py`, `tests/test_screen.py`, `tests/test_screen_keyboard_ui.py` + `tests/screen_keyboard_check.js` (Playwright 390px, fake hub).
- UNVERIFIED: real Android soft keyboard (Gboard composition), a real Ollama vision model, real Gemini, pyautogui typing on Windows.
## 3t. Google Chat for a PERSONAL Google account (2026-11-15)
Owner has no Workspace: no Chat webhook, no Chat app. Guide `docs/guide/38-google-chat-personal.md`, API `docs/api/chatchan.md` (has the can/cannot table).
- `hub/chatchan.py` (appended to `FEATURE_MODULES`; routes `/api/chatch/*`; tools `chat_list_pending`, `chat_draft_reply` via `chatchan.build`, group `chatchan` in `assistant.TOOL_GROUPS`; screen `js/chatch.js`, Settings > Alerts > Google Chat channels, `ownerlists.OWNER_SECTIONS.chatchannels`; button on Chat alerts in `js/alerts.js`). The owner's list reuses `hub/provlist.py` (`chat_channels.json`; kinds `space`, `email-source`, `web-draft`; a space switched off = muted).
- Alerts hook (additive, `hub/alerts.py`): `clean_event` keeps `conversation`/`group`; `ingest` calls `chatchan.pre_ingest` (parse notification or Chat email -> source `gchat`, sender/space/mention, mute / mention-only, 6 h de-dupe across both ways) and `chatchan.register` (pending reply with its own draft; `alert.chat`, `alert.chat_pending`). `chatch_enabled=false` leaves alerts as before.
- Reply, no API: `prepare` returns text + open plan (Chat link or home, Android intent with web fallback, share intent); phone = `HubCapture.openChat` (new `ChatLink.java` + plugin method, clipboard + intent, no auto-paste/send) else clipboard + `intent:`/`window.open`; laptop = `open-laptop` (clipboard on stdin + `webbrowser.open`, Chat addresses only). Per-conversation drafts: nothing is copied to all.
- Experimental `web-draft` entry (off): visible Playwright window via `formfill.PlaywrightDriver`, only `fill` on the compose box, never presses/clicks (a test greps the driver source), needs a saved space link, confirm, Playwright; own profile `chatweb_profile`; no stealth code, so Google may refuse sign-in.
- NOT shipped: Chat REST API with the owner's OAuth (unknown whether personal accounts may use it; docs unreachable). `chat_send` unchanged and still in `safety.ALWAYS_ASK` (note: the older line in section 2 saying it is not there is stale).
- Config appended: `chatch_enabled` (true), `chatch_email_parse` (true); both in the Settings allow-list. Tests `tests/test_chatchan.py`, `mobile/scripts/test-chat-link.js` (Java compiled and cross-checked on a JVM).
- UNVERIFIED: real Chat email subjects/bodies (patterns are a guess; `Check a sample` in the UI), real Chat notification shapes (title/`conversation`/thread are what the listener gives, not checked on a device), whether the Chat app opens `chat.google.com/room/...` links, whether Chat's compose box matches the default selector, Google's Chat API docs (personal-account use), whether Google blocks the automated sign-in, `openChat` compiled in a real Android build, the screens in a real browser at 390px (node syntax check only).

## Bug-fix round: Talk "AI for this chat", stray "null", sheet polish (2026-11-15)
Owner (real 390px Android): model strip unreadable, the chip row was the lanes picker (answers in a "Work lane result" sheet), Voices sheet printed "null".
- Roots: (1) `ui.segmented` forced one equal column per provider (Local, Claude, Gemini, CLI logins...) so labels overlapped. (2) Talk had two pickers: provider segmented control + `lanePicker` whose non-Auto chip ran `runInLane` and opened `resultSheet`. (3) `append()/replaceChildren()` turn a `null` child into the text "null"; `listEditor`'s foot did `replaceChildren(onAdd ? btn : null, onReset ? btn : null)`, and ~15 other call sites do the same (Hub, Playbooks, Job prefs, Batches and the provider lists showed it too). Fixed once in `js/api.js` (guard skips null/undefined/false children) and in `listeditor.js`.
- New `js/aichat.js` + `css/aichat.css`: one compact "AI for this chat" button (current AI, ellipsis) opening a sheet: Auto, Local, Claude (key/login), Gemini, Claude+Gemini, CLI logins (Antigravity `agy` is the seeded default), plus Local deep thinking and Council (lanes). Not-ready ones are greyed with the reason; Council not set up shows "Council isn't set up yet" + "How to set up". Choice is stored per conversation (`hub.assistant.chatai` map) and seeds the next chat. Lane choices run via `/api/lanes/run` and the answer is posted into the chat (spoken; voice mode hears it); "Send to lane" is a separate chip that does the same for one message; the old sheet is only "Details". Server: `assistant.run` emits `{type:"answered_by", provider, label}` (the provider that really wrote the words, via `ChainModel.used` when a fallback answered), shown as "answered by X" under each reply.
- Shared helper `hub/common.py setup_all_hint` + `js/setuphint.js` ("Run scripts\setup-all.ps1 (installs everything)") used by dictation, screen, hub checks, voices; the script itself is another worker's. `css/sheets.css`: reading face and line height for long sheet text, safe bottom padding.
- Tests: `tests/test_chat_ai.py`, `tests/test_chat_ai_ui.py` (`chat_ai_check.js` at 390 and 320 with a scripted fake hub, `ui_nulltext_check.js` over every screen and Settings sheet).
- UNVERIFIED: real phone fonts/touch, a real Antigravity login (`agy -p`), a real Council run, other screens that still use `providerSegmented` with many CLI logins (same overlap possible), lane answers do not carry earlier chat turns (stateless task).
