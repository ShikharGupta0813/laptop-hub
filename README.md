# Laptop Hub

Your laptop, from your phone, in one app you install on the home screen.

| Tab | What it does |
|---|---|
| **Talk** | Speak or type to an assistant that acts on your laptop: files, notes, Asana, calendar, email, laptop actions, Jarvis. Changes wait for your tap. |
| **Work** | Asana tasks (comment, complete), today's meetings with Join buttons, write and send email (AI drafts it, you review), post to Google Chat. |
| **Learn** | **Chat** with a note, document, web page or topic. **Quiz** yourself from a note, a PDF or document on your laptop, a web page, pasted text or any topic. **English coach**: speaking practice with pace, filler words and corrections, role-play, writing fixes, vocabulary and progress. |
| **Notes** | Tap the mic and talk. Saved as Markdown in time order with a title and tags. Typed notes work too. |
| **Apps** | A searchable directory of **every** feature, plus laptop actions you can run from your phone (lock, screenshot, processes, open a link or app, clipboard, volume and media keys, shutdown...) and the **Jarvis** screen: start or stop Jarvis on the laptop and send it commands. Also opens Files, Terminal and Hub. |

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

3. **Download a small local model** for Explain, Quiz and note tags:

   ```powershell
   ollama pull qwen3:4b
   ```

4. **Create your config and login token.** Choose the folders to share (repeat `--root`). A `notes` folder (`~\HubNotes`) and an `inbox` folder (`~\HubInbox`) are added for you:

   ```powershell
   python -m hub init --root study=C:\Users\YOU\Documents\study --root projects=C:\Users\YOU\projects --jarvis-dir C:\Users\YOU\jarvis-voice-assistant
   ```

   It prints a **login token once**. Copy it into your password manager. If you lose it: `python -m hub token`.

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
- The Files tab is **read-only** and limited to the folders you chose. The terminal and assistant are not limited to them.
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
| Notes have no tags | The local model adds them a few seconds after saving; check `python -m hub doctor` |

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
