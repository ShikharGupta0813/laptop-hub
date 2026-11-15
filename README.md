# REDLINE: your own Jarvis, private and on a leash

**A personal AI agent that runs on your laptop and that you drive from your phone. Your data stays on your machine. It acts only when you say so.**

Most "Jarvis" projects are cloud wrappers that run wild. REDLINE is built the other way round: local-first, with a safety model you can read.

- **Local first.** Local models (Ollama) and Whisper work with no internet. Gemini, Antigravity (`agy`), Claude and a Claude + Gemini pair are optional, and a work-lane router sends routine jobs to local models first so paid tokens aren't wasted.
- **Never acts behind your back.** Sending mail, applying to jobs, publishing posts and submitting forms always wait for your tap. Voice can't approve a dangerous step. Full Control needs a typed `I UNDERSTAND`.
- **Graded safety.** Actions are ranked N1 to N3. N3 needs a typed CONFIRM and a 10-second cancel window. Untrusted text (web pages, mail, Asana comments) can't trigger tools on its own.
- **Panic button and audit trail.** One tap stops every background job. Every action is logged and searchable. A trust ladder only ever suggests loosening, never does it silently.
- **Two assistants with different jobs.** **Jarvis** runs the laptop and helps with technical work (files, terminal, apps, code). **Friday** runs your day (morning brief, mail, calendar, Asana tasks and comments, reminders, follow-ups, job applications and urgent alerts). Start a message with a name to pick one.
- **Voice that works with the screen off.** Say "Hey Friday" or "Hey Jarvis" and the phone answers out loud, even with the app closed. Urgent alerts are spoken, and the most urgent ones, and reminders you ask to be called about, ring like an alarm on the lock screen.
- **A full job-search pipeline.** Preferences, many job sources, daily batches, tailored resumes from your own facts, an application tracker with follow-ups, a learning loop, networking drafts and an interview coach. Nothing is sent or applied without your tap, and LinkedIn and Indeed stay manual.
- **Memory that lasts.** Chats survive a hub restart, Memory keeps notes, decisions and facts, and Today remembers slow sources for 15 minutes so it opens fast.
- **Everything is a list you edit.** Assistants, providers, sources, wake phrases, apps and destinations are all editable, with nothing hard-coded.
- **Phone-first.** An installable PWA plus a native Android app, reached over Tailscale so nothing is exposed to the internet.

> Status: a personal project with a large automated test suite (one process per module, run with `scripts/run-tests.py`). Issues and stars help.

## Quick start

```powershell
git clone https://github.com/ShikharGupta0813/laptop-hub
cd laptop-hub
python -m hub setup
```

Full steps are in [docs/SETUP-WINDOWS.md](docs/SETUP-WINDOWS.md) and the guide in `docs/guide/`.

## What's inside

| Area | Highlights |
|---|---|
| **Talk** | Voice or text assistant with tools, hands-free mode, Jarvis and Friday (and any assistants you add), "AI for this chat" switch, chat history that survives restarts. |
| **Today and Friday** | Morning and evening brief, calendar, Asana (tasks, details and comments), mail counts, follow-ups due. Slow sources are remembered for 15 minutes; Refresh gets live data. |
| **Work** | Asana, mail, calendar, chat, drafts you approve. |
| **Autopilot and alerts** | Triage across sources, drafts only, delivery timed to where you are. Urgent items are spoken on the phone; the most urgent ring like an alarm. |
| **Reminders** | Normal reminders, timers, and "call me" reminders that ring and read the full details on the phone. |
| **Job search** | Preferences and search profiles, about 30 ready-made source presets, daily batches, tailored resume and cover note, answers bank, application tracker, follow-ups, learning loop, networking drafts, interview coach. |
| **Learn** | Chat with docs, quizzes, flashcards (including from a PDF), roadmaps, English coach. |
| **Council** | Optional local multi-model team as a background job. |
| **Memory** | Notes, decisions and facts that the assistants can use; searchable. |
| **Laptop** | Files, terminal, remote screen, apps (such as Chrome and its profiles), routines, playbooks, computer control. |
| **Voice** | Engines: browser, Windows (SAPI), Piper, or your own command. Wake word on the phone, laptop and Android in the background. |
| **Safety** | Levels, panic, trust ladder, audit search, redaction, usage caps. |

Everything else (notes, to-dos, habits, spending, focus, search) lives in the Apps directory. Settings has a search box to find any setting fast.

## How it is connected

```
phone app  ──HTTPS──►  Tailscale (private link)  ──►  Laptop Hub on 127.0.0.1:8765  ──►  your files, shell, Ollama
```

The hub only listens on the laptop itself. [Tailscale](https://tailscale.com) (free) is the only way in, so nothing is exposed to the internet. Tailscale also gives the hub the HTTPS address that Android requires for the **microphone** and for **installing the app**.

## Jarvis and Friday

| | Jarvis | Friday |
|---|---|---|
| **For** | The laptop and technical work | Your day and your work life |
| **Does** | Files, terminal, opening apps, screen, code, system checks | Brief, calendar, mail, Asana tasks and comments, reminders, follow-ups, applications, alerts |
| **Voice** | Own name, wake word and voice settings | Own name, wake word and voice settings |

Start a message with a name ("Friday, what is due today?") to choose. Every assistant is a row you can edit in Settings, Assistants, and you can add more.

**Choosing the AI.** Settings, Providers: Gemini (key in the Windows keyring), Antigravity (`agy -p`, uses your Google AI Pro login, no key), Claude (your login), Claude + Gemini together, or the local Ollama model. In auto mode, tool and code requests go to the strong provider and ordinary chat goes to your default provider. Short chit-chat goes to the local model only if you turn on `router_local_chat` in `hub.json` (leave it off on a laptop without a fast GPU).

## Voice, wake word and alerts

- **Wake word, app open or closed.** The Android app can keep listening in the background (offline, on the phone). Say "Hey Friday" or "Hey Jarvis" plus your sentence.
- **Answers with the screen off.** With the app closed, the phone sends your sentence to the hub and speaks the answer with the phone's own voice. "Open RedLine" opens the app. Anything that needs an approval (sending, changing, running) is not done in the background: it says so and posts a notification.
- **Urgent alerts spoken.** While the screen is off, the phone checks the hub about every 30 seconds for urgent items and speaks them.
- **Alarm-style ring.** The most urgent alerts and reminders marked "call" ring like an alarm on the lock screen and read the whole message. It is not a real phone call and uses no outside service. Android 14 and newer may ask you to allow full-screen notifications for the app.
- **Call reminders.** Say "remind me at 5 to check the deploy, with these details: ..., call me". The reminder keeps the full details and rings at the time.
- **What counts as urgent** is a list you edit (Settings, Alerts): urgent words (defaults include prod issue, payment failed, server down, deployment failed, hotfix, p1, data loss, security incident), VIP senders, a meeting starting in 5 minutes, Asana tasks assigned to you that are due, payment alerts, and hub problems.
- **Voice engines** (Settings, Voice): browser, Windows (SAPI), Piper, or a command you provide. The laptop can play audio on the phone. Panic stops everything.

These features need the hub reachable from the phone (Tailscale on) and a new APK from the GitHub "Build Android APK" workflow. Uninstall the old app first. Details: `mobile/README.md`.

## Job search

Everything is a draft or a suggestion. **Nothing is sent or applied without your tap, and LinkedIn and Indeed are never automated.**

1. **Preferences** (Apps, Job preferences): several search profiles, a remote-only filter, hard filters and soft points.
2. **Sources** (Apps, Opportunities): ATS boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Teamtailor), remote boards (RemoteOK, Remotive, Himalayas, We Work Remotely, Working Nomads, Jobicy, Arbeitnow, Jobspresso, NoDesk, Python.org Jobs), Hacker News "Who is hiring", a company watch list, email alerts and open-link mode for platforms that forbid bots. Each source was checked against its live public endpoint (table in `docs/api/oppsources.md`; Workable is marked unverified).
3. **Batches** (Apps, Batches): a daily set of ready applications, sent only with one confirming tap.
4. **Tailor** (Apps, Tailor): a resume and cover note built only from facts you confirmed, with an honesty check, plus a saved answers bank.
5. **Job tracker** (Apps, Job tracker): every application with a status history, duplicate warning, follow-ups at day 5 and 10 (they can ring), a draft nudge, and a "what works" view showing replies per source, role and resume. Suggestions change your profiles only when you accept them.
6. **Networking** (Apps, Networking): referral, recruiter, thank-you and follow-up drafts (up to 120 words, facts only, copy and send yourself) and a contacts list with nudges.
7. **Interview coach** (Apps, Interview coach): likely questions from a posting, practice with feedback, a STAR story bank, "tell me about yourself", a pre-interview brief and a pay cheat sheet built from your own numbers. AI critique is off by default and, if you switch it on for an answer, sends only that answer to your chosen AI.

## Memory and history

- **Chat history** is stored in `chat_history.sqlite` in the data folder (last 40 turns per conversation, 30 days by default: `chat_history_days`; switch off with `chat_history_enabled`). It keeps your text and the assistant's final answer, never raw tool output.
- **Memory** (`brain.sqlite`) keeps notes, decisions and facts the assistants can use, with search.
- **Today** keeps calendar, Asana and mail answers for `today_cache_min` minutes (default 15, 0 = always live) in `today_cache.sqlite`. The Refresh button or pull-to-refresh fetches live data.
- Everything stays in SQLite or JSON files in the hub's data folder on your laptop. Nothing needs a database server.

## Set up (Windows, PowerShell)

Run the hub on Windows itself, not inside WSL: it then uses all your RAM, reaches your Windows files, and its terminal can open both PowerShell and WSL.

1. **Install the tools** once: Python 3.11+, Tailscale, Ollama.

   ```powershell
   winget install --id Python.Python.3.12 -e
   winget install --id Tailscale.Tailscale -e
   winget install --id Ollama.Ollama -e
   ```

2. **Install the hub** (in this folder). Voice is a separate, larger download:

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt -r requirements-voice.txt -r requirements-work.txt -r requirements-laptop.txt
   ```

   Optional, for the **Remote screen** (see and control the laptop screen):

   ```powershell
   pip install -r requirements-screen.txt
   ```

   Step-by-step with every command for a fresh Windows machine: [docs/SETUP-WINDOWS.md](docs/SETUP-WINDOWS.md).

3. **Download a small local model** for Explain, Quiz and note tags:

   ```powershell
   ollama pull qwen3:4b
   ```

   Optional, for **Ask my files** (meaning-based search; without it the hub falls back to keyword search):

   ```powershell
   ollama pull nomic-embed-text
   ```

4. **Create your config and login token.** Choose the folders to share (repeat `--root`). A `notes` folder (`~\HubNotes`) and an `inbox` folder (`~\HubInbox`) are added for you:

   ```powershell
   python -m hub init --root study=C:\Users\YOU\Documents\study --root projects=C:\Users\YOU\projects --jarvis-dir C:\Users\YOU\jarvis-voice-assistant
   ```

   It prints a **login token once**. Copy it into your password manager. If you lose it: `python -m hub token`.

   To let the Files screen change files in a folder, add its name to `writable_roots` in `hub.json` (default: `notes` and `inbox` only). Every other folder stays read-only.

   `--jarvis-dir` points the hub at your Jarvis folder, which Work (calendar, email, chat) and the Jarvis screen use. Skip it if you don't use Jarvis. Asana uses your existing token (`ASANA_TOKEN`, or the file `~\.claude\.asana_token`).

5. **Check everything:**

   ```powershell
   python -m hub doctor
   ```

6. **Optional: a cloud AI, no API key needed.** Skip this to use only the local model. Otherwise: install Claude Code, run `claude` once and log in (the hub then offers **Claude (my login)**, which uses your Claude subscription quota, not API billing), run `agy` once and sign in (the hub then offers **Antigravity**, which uses your Google AI Pro login), and/or save a free Gemini key from aistudio.google.com/apikey:
   Best of both: choose **Claude + Gemini** (`default_provider: "duo"`, offered in the wizard when both are ready). Claude plans each non-trivial assistant request and checks the work after tools ran (a few short calls, capped by `duo_max_claude_per_hour`, default 20, and paused automatically on a usage limit); Gemini does all the work. Set `gemini_model` to your Gemini Pro model name if you have one.

   ```powershell
   python -m hub setup --only ai        # explains the options, checks the claude login, saves the default AI
   python -m hub setup --only gemini    # just the Gemini key (stored in the Windows keyring)
   ```

   Details in `docs/SETUP-WINDOWS.md` ("AI without an API key"). `ANTHROPIC_API_KEY` still works if you want it.

7. **Start it** (leave this window open):

   ```powershell
   python -m hub serve
   ```

8. **Put HTTPS in front** (a second window):

   ```powershell
   tailscale serve --bg 8765
   tailscale serve status
   ```

   First time only: in the [Tailscale admin console](https://login.tailscale.com/admin/dns) turn on **MagicDNS** and **HTTPS Certificates**. `status` prints your address, like `https://yourlaptop.tail1234.ts.net`.

9. **On your phone:** install the Tailscale app and sign in to the same account. Open the address in Chrome, paste the token, then Chrome menu → **Install app**. It now opens from your home screen like any app.

10. **Keep the laptop reachable:** plugged in, and sleep set to never while charging:

    ```powershell
    powercfg /change standby-timeout-ac 0
    ```

## Optional: phone push with ntfy

The in-app notification list always works. To also get a push on the phone when the app is closed, install the [ntfy](https://ntfy.sh) app and subscribe to a topic, then give the same topic to the hub (Settings, Notifications, or `ntfy_topic` in `hub.json`).

**ntfy topics on ntfy.sh are public.** Anyone who knows or guesses the name can read what the hub sends and can send you fake alerts. Pick a long random name, never a word like `alerts`:

```powershell
python -c "import secrets; print('hub-' + secrets.token_urlsafe(24))"
```

Topic rules: 8 to 64 characters from `A-Z a-z 0-9 _ -`. Keep secrets out of reminders and notes titles. Quiet hours and a minimum level can be set so only important items push.

## On the phone (Android app)

Install the app from `mobile/` (see `mobile/README.md`), then:

- **Microphone:** allow when asked (Talk, Notes, English, hands-free voice).
- **Notification access** (needed for automatic spend capture and for Gmail / Chat watching): Settings, Notifications, Device and app notifications, switch **Laptop Hub** on. The Settings > Phone capture screen in the app opens this for you.
- **Android 13 and newer:** apps installed from an APK are blocked from this. If the switch is greyed out: Settings, Apps, Laptop Hub, three-dot menu, **Allow restricted settings**, then try again.
- **SMS permission (optional):** only for bank-SMS capture. It is receive-only; the app cannot read your inbox.
- **Battery:** set Laptop Hub to Unrestricted so capture keeps working in the background.

## What needs what

| Feature | Needs |
|---|---|
| Notes, Files, To-do, Habits, Journal, Focus, Reminders, Tools, Routines | Nothing extra |
| Explain, Quiz, Learn chat, note tags | Ollama and `qwen3:4b` (or Gemini, Claude login or a Claude key) |
| Voice notes, hands-free voice, English speaking | `requirements-voice.txt` (Whisper) |
| Assistant with a cloud AI, "Ask Claude" | Claude login (`claude` installed and logged in), a free Gemini key, or `ANTHROPIC_API_KEY`. Screen "Ask about my screen" needs the API key (screenshots) |
| Ask my files | Ollama; `nomic-embed-text` for meaning search (keywords work without it) |
| Work: Asana | `ASANA_TOKEN` or `~\.claude\.asana_token` |
| Work: calendar, email, chat posting; Jarvis screen | `--jarvis-dir` and `requirements-work.txt` |
| Inbox and Watch (mail) | The Jarvis mailer's Gmail address and **app password stored in the keyring** (the Jarvis app sets it up). Read-only IMAP |
| Watch (Google Chat alerts) | The **Android app** with Notification access; Chat notifications are forwarded from the phone |
| Spending: automatic UPI capture | The **Android app** with Notification access (SMS optional) |
| Remote screen | `requirements-screen.txt`; control also needs you to arm it each time |
| Phone push | ntfy topic (see above) |
| Terminal on Windows | `pywinpty` (installed by `requirements.txt` on Windows) |

## Using it

- **Talk:** "What changed in my projects folder today?", "Find my sharding notes", "Run the tests in my AI-Task-Agent folder". Commands and file writes show an approval card with exactly what will run; tap **Approve** or **Deny**. The **Run commands without asking** switch turns that off for the current session only.
- **Notes:** tap the mic, speak, tap again. Say "note it" if you like; the phrase is removed from the note. Notes are plain `.md` files in `~\HubNotes`, so they also show up in Files and in any editor.
- **Quick buttons:** add to `hub.json` (in `~\.laptop-hub\`):

  ```json
  "scripts": {
    "backup": {"label": "Back up notes", "argv": ["robocopy", "C:\\Users\\YOU\\HubNotes", "D:\\backup\\notes", "/MIR"]}
  }
  ```

## Safety, plainly

- **A logged-in phone can do anything you can do on the laptop** (terminal, assistant). Lock your phone. If it is lost: `python -m hub token` logs every phone out, and you can remove the phone from your Tailscale account.
- Only the **login token's hash** is stored. Five wrong tries lock login for a minute.
- The assistant treats text in files and command output as **data, never as instructions**. It cannot change the hub's own config or the audit log.
- `~\.laptop-hub\audit.log` records logins, terminal sessions opening and closing, quick-button runs, assistant requests, and every approval. It does **not** record what you type in the terminal.
- **Remote control is off until you arm it** (1 to 15 minutes, then it switches itself off). Viewing the screen needs only the login.
- **Files are read-only** except in folders listed in `writable_roots` (default `notes` and `inbox`). The terminal and assistant are not limited to your shared folders.
- **Mail is read-only.** The Inbox reads Gmail and drafts replies; it never sends. Sending happens only in Work, after you review and tap.
- **Nothing is deleted permanently except by emptying the trash.** Deleting in Files moves to a trash you can restore from.
- Routines need your explicit confirmation before dangerous steps can run.
- Voice is processed on the laptop. The first recording downloads the Whisper model once.

## Update from your phone

One time, on the laptop (needs a terminal once):

```powershell
cd D:\laptop-hub
git pull
python -m hub setup --only autostart     # or just: python -m hub install-updater
```

This registers a second on-demand scheduled task, `LaptopHubUpdate`, and adds an **Update REDLINE** quick button. Restart the hub once so the button appears. After that, open **Launcher > Update REDLINE** (also under Hub > Quick buttons) on the phone and tap it. The laptop runs `python -m hub update --restart`: it checks the tree has no local changes, runs `git fetch` and `git pull --ff-only`, runs `pip install -r requirements.txt` only if that file changed, then stops and starts the `LaptopHub` task. The app reconnects after about 20 seconds; the running build (`build abc1234`) shows in Hub and Setup.

- It is a separate task so that stopping the hub cannot kill the updater.
- Log: `~/.laptop-hub/update.log` (timestamped, old and new commit). The last lines are also at `/api/update-log` when logged in.
- If tracked files on the laptop were edited (dirty tree), nothing is pulled and the hub keeps running; the log says so. Commit or discard the changes (`git status`) and tap again. Any failure (no network, non-fast-forward, pip error) leaves the running hub untouched.
- Manual run without restart: `python -m hub update`.

## Troubleshooting

| Problem | Fix |
|---|---|
| Microphone says it needs HTTPS | Open the `https://…ts.net` address, not the IP or `http://` |
| "Can't reach your laptop" | Is `python -m hub serve` running, and is the laptop awake and online? |
| Explain/Quiz says Ollama isn't running | Open the Ollama app; `ollama pull qwen3:4b` |
| Voice note: "needs faster-whisper" | `pip install -r requirements-voice.txt` |
| Terminal won't open on Windows | `pip install pywinpty` |
| Terminal closes after the origin check | Add the address to `allowed_origins` in `hub.json` |
| Ask my files says "keywords only" | `ollama pull nomic-embed-text`, then rescan |
| Remote screen says something is missing | `pip install -r requirements-screen.txt`, restart the hub |
| Remote screen shows but taps do nothing | Control is off until armed: tap **Enable control** |
| Files says "That folder is read-only" | Add the folder name to `writable_roots` in `hub.json`, restart |
| Inbox: Gmail not set up | Jarvis' Gmail address and app password must be in the keyring; set `--jarvis-dir` |
| No Chat or payment alerts | Notification access for Laptop Hub is off, or "Allow restricted settings" was not done (Android 13+); then turn on monitoring in Watch / Spending |
| No push when the app is closed | Subscribe the ntfy app to the same topic; check quiet hours |
| "Open Chrome" says the app is not on your list | Add it under `apps` in `hub.json`, for example `"Chrome Personal": {"target": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "args": ["--profile-directory=Default"]}`, then restart |
| Replies to short messages are very slow | The local model is too slow on CPU: keep `router_local_chat` off (the default) |
| Friday says it can't see Asana tasks | Ask for overdue or all tasks; make sure `ASANA_TOKEN` is set |
| Phone does not answer with the screen off | Needs the always-on wake word switched on, microphone and notification access, the hub reachable over Tailscale and a current APK |
| Notes have no tags | The local model adds them a few seconds after saving; check `python -m hub doctor` |

## Android app

`mobile/` builds a real Android app (APK) around the hub. See `mobile/README.md`. The easiest way to get it is the GitHub Actions workflow **Build Android APK**: run it with your hub's https address and install the result (uninstall an older copy first). It adds the background wake word, spoken replies and alerts, the alarm-style ring, notification and SMS capture, and the Back button handling. Building locally needs Android Studio and Node 22.

## Hand-off notes

`HANDOFF.md` says what is built, what is still to do, and how to continue in a new chat.

## For developers

```bash
python -m unittest discover -s tests -t .      # no models or network needed
python scripts/run-tests.py                    # same tests, one process per module, 4 at a time, 420 s limit each
python scripts/run-tests.py test_notes test_auth --workers 1   # only some modules
```

`scripts/run-tests.py` prints one line per module and a summary, and exits non-zero if any module fails or times out. Prefer it on Windows: a single `discover` run can look hung.

The browser tests (`test_ui_*`, `test_chat_ai_ui`, `test_fixit_ui`, `test_screen_keyboard_ui`) skip unless Node.js, the Playwright package for Node and a browser are installed. To enable them (optional, installs outside the repo):

```bash
npm install -g playwright
npx playwright install chromium
```

```bash
python -m hub.devserver --port 8799            # fake models, sample files, token: devtoken
```

API contract: [docs/API.md](docs/API.md). Front-end libraries are vendored in `hub/static/vendor` (marked, DOMPurify, xterm.js), with their licences.
