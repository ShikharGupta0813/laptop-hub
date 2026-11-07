# Laptop Hub

Your laptop, from your phone, in one app you install on the home screen.

First time? Run `python -m hub setup` (see docs/SETUP-WINDOWS.md, "One-command setup").

| Screen | What it does |
|---|---|
| **Home** | One dashboard: what needs you now (reminders, urgent mail, pending payments), today's meetings, quick actions. |
| **Talk** | Speak or type to an assistant that acts on your laptop: files, notes, Asana, calendar, reminders, to-dos, laptop actions, Jarvis. Changes wait for your tap. **Hands-free voice mode** lets you talk back and forth without touching the screen. |
| **Work** | Asana tasks (comment, complete), today's meetings with Join buttons, write and send email (AI drafts it, you review), post to Google Chat, and a daily **briefing**. |
| **Inbox** | Read-only Gmail with AI summaries, reply drafts (never sent from here) and a digest. |
| **Reminders** | Reminders, alarms and timers, with quick add in plain language ("in 20 minutes call mum"). |
| **Learn** | **Chat** with a note, document, web page or topic. **Quiz** from a note, PDF, web page, pasted text or topic. **Flashcards** (spaced repetition, decks, import/export) and AI **roadmaps**. **Ask my files**: cited answers over your documents. **English coach**: speaking practice, role-play, writing fixes, vocabulary, progress. |
| **Notes, To-do, Habits, Journal, Focus** | Voice or typed Markdown notes with titles and tags; to-dos; habit streaks; a journal; a focus (pomodoro) timer. |
| **Files** | Browse your shared folders, open and edit text files, upload, rename, move, copy, zip, and a **trash** you can restore from. Changes only work in folders you list as writable. |
| **Spending** | Expenses with categories. On Android the app can add UPI payments **automatically** from payment-app notifications (and optional bank SMS); each shows up under "Needs a category" and the hub remembers merchant to category. |
| **Watch** | Alerts for urgent Gmail and for Google Chat messages that match your rules. |
| **Remote screen** | See the laptop screen. Control is a separate switch you arm for a few minutes. |
| **Routines** | Named chains of steps ("Good morning", "Leaving desk"), run by hand or on a schedule. Dangerous steps are gated. |
| **Tools, Read-later** | Calculator, unit converter, world clock, countdowns, snippets and clipboard history; saved links to read later. |
| **Settings, Notifications, Search** | Models, folders, themes, backups, audit viewer, diagnostics; a notification center (plus optional phone push); one search box across notes, files, tasks, apps and more. |
| **Apps** | A searchable directory of every feature, plus laptop actions (lock, screenshot, processes, open a link or app, clipboard, volume and media keys, shutdown...). |
| **Terminal** | A real shell on the laptop (PowerShell, WSL). |
| **Laptop / Jarvis** | Laptop actions, and start or stop Jarvis on the laptop and send it commands. |

Every AI feature has a **Local (Ollama) or Claude** switch.

Everything runs on your laptop. The local AI (Ollama) and speech-to-text (Whisper) work with no internet. The assistant can use Claude (needs an API key) or your local model.

## How it is connected

```
phone app  ──HTTPS──►  Tailscale (private link)  ──►  Laptop Hub on 127.0.0.1:8765  ──►  your files, shell, Ollama
```

The hub only listens on the laptop itself. [Tailscale](https://tailscale.com) (free) is the only way in, so nothing is exposed to the internet. Tailscale also gives the hub the HTTPS address that Android requires for the **microphone** and for **installing the app**.

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

6. **Optional: the Claude assistant.** Skip this to use only the local model. Otherwise set your key in the same window before starting the hub:

   ```powershell
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   ```

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
| Explain, Quiz, Learn chat, note tags | Ollama and `qwen3:4b` (or a Claude key) |
| Voice notes, hands-free voice, English speaking | `requirements-voice.txt` (Whisper) |
| Assistant with Claude, "Ask Claude" | `ANTHROPIC_API_KEY` |
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

## Troubleshooting

| Problem | Fix |
|---|---|
| Microphone says it needs HTTPS | Open the `https://…ts.net` address, not the IP or `http://` |
| "Can't reach your laptop" | Is `python -m hub serve` running, and is the laptop awake and online? |
| Explain/Quiz says Ollama isn't running | Open the Ollama app; `ollama pull qwen3:4b` |
| Voice note: "needs faster-whisper" | `pip install -r requirements-voice.txt` |
| Terminal won't open on Windows | `pip install pywinpty`; the Windows terminal path has not been tested on a real machine yet |
| Terminal closes after the origin check | Add the address to `allowed_origins` in `hub.json` |
| Ask my files says "keywords only" | `ollama pull nomic-embed-text`, then rescan |
| Remote screen says something is missing | `pip install -r requirements-screen.txt`, restart the hub |
| Remote screen shows but taps do nothing | Control is off until armed: tap **Enable control** |
| Files says "That folder is read-only" | Add the folder name to `writable_roots` in `hub.json`, restart |
| Inbox: Gmail not set up | Jarvis' Gmail address and app password must be in the keyring; set `--jarvis-dir` |
| No Chat or payment alerts | Notification access for Laptop Hub is off, or "Allow restricted settings" was not done (Android 13+); then turn on monitoring in Watch / Spending |
| No push when the app is closed | Subscribe the ntfy app to the same topic; check quiet hours |
| Notes have no tags | The local model adds them a few seconds after saving; check `python -m hub doctor` |

## What is not tested

Everything was tested in a Linux container with fake models and a fake operating system. **Not yet run on real hardware:** the Windows laptop (terminal, lock, volume, screenshots, Jarvis launch), real Ollama, Whisper, Claude, Asana, Gmail and calendar, and any Android phone. The Android capture code (UPI, Gmail and Chat notifications) has never been run on a phone. Remote screen touch control and streaming, hands-free voice, and ntfy push are also untested for real. Expect small fixes on the first real run, and run `python -m hub doctor` first.

## Android app

`mobile/` builds a real Android app (APK) around the hub. See `mobile/README.md`. It needs Android Studio and Node 22 on the laptop that builds it.

## Hand-off notes

`HANDOFF.md` says what is built, what is still to do, and how to continue in a new chat.

## For developers

```bash
python -m unittest discover -s tests -t .      # no models or network needed
python -m hub.devserver --port 8799            # fake models, sample files, token: devtoken
```

API contract: [docs/API.md](docs/API.md). Front-end libraries are vendored in `hub/static/vendor` (marked, DOMPurify, xterm.js), with their licences.

## Built on ideas from

[CloudSH](https://github.com/JongoDB/cloudsh), [Katulong](https://awesome.ecosyste.ms/projects/github.com%2Fdorky-robot%2Fkatulong) and [webterm](https://github.com/unn-Known1/webterm) (terminal and files in the browser), [WhisPad](https://hub.docker.com/r/drakonis96/whispad) and [LocalWhisper](https://pypi.org/project/localwhisper/) (offline voice notes), [Khoj](https://dev.to/jovan_chan_9500711396d4e6/khoj-review-2026-your-self-hosted-ai-second-brain-28mi) (local AI over your notes).
