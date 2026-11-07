# Windows setup, from zero

Copy and paste each block into **PowerShell**. Replace `YOU` with your Windows user name. Run the hub on Windows itself, not in WSL.
Nothing here has been run on a real Windows machine yet; if a step fails, run `python -m hub doctor` and read what it says.

## 1. Install the tools

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id Tailscale.Tailscale -e
winget install --id Ollama.Ollama -e
```

Close PowerShell and open a new window so the new programs are found. Then sign in to Tailscale (tray icon) and start the Ollama app once.

## 2. Get the code and install it

```powershell
cd $HOME
git clone https://github.com/ShikharGupta0813/laptop-hub
cd laptop-hub
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-voice.txt -r requirements-work.txt -r requirements-laptop.txt
pip install -r requirements-screen.txt
```

If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. The last line (screen) is optional.

## 3. Models

```powershell
ollama pull qwen3:4b
ollama pull nomic-embed-text
```

`nomic-embed-text` is only for Ask my files (meaning search).

## 4. Create the config and your token

Choose folders to share (repeat `--root`). A `notes` and an `inbox` folder are added for you. Leave out `--jarvis-dir` if you do not use Jarvis.

```powershell
python -m hub init --root study=C:\Users\YOU\Documents\study --root projects=C:\Users\YOU\projects --jarvis-dir C:\Users\YOU\jarvis-voice-assistant
```

Copy the printed **login token** into a password manager (shown once; `python -m hub token` makes a new one).
To let the Files screen change a folder, add its name to `writable_roots` in `C:\Users\YOU\.laptop-hub\hub.json` (default: `notes`, `inbox`).

## 5. Check, then start

```powershell
python -m hub doctor
python -m hub serve
```

Leave that window open for now. Optional Claude assistant: `$env:ANTHROPIC_API_KEY = "sk-ant-..."` before `serve`.

## 6. HTTPS with Tailscale (second PowerShell window)

```powershell
tailscale serve --bg 8765
tailscale serve status
```

First time only: in the Tailscale admin console turn on **MagicDNS** and **HTTPS Certificates**. `status` prints your address, like `https://yourlaptop.tail1234.ts.net`. On the phone install Tailscale (same account), open that address in Chrome, paste the token, or install the Android app (`mobile/README.md`).

## 7. Start automatically at logon

This scheduled task starts the hub when you log in. Run it once (it uses the venv's Python).

```powershell
$py  = "C:\Users\YOU\laptop-hub\.venv\Scripts\python.exe"
$dir = "C:\Users\YOU\laptop-hub"
$action  = New-ScheduledTaskAction -Execute $py -Argument "-m hub serve" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "LaptopHub" -Action $action -Trigger $trigger -Settings $set -Description "Laptop Hub"
Start-ScheduledTask -TaskName "LaptopHub"
```

Set `ANTHROPIC_API_KEY` for your user once if you want Claude (the task cannot see a per-window variable): `[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY","sk-ant-...","User")`, then log out and in.
Stop or remove it: `Stop-ScheduledTask LaptopHub` / `Unregister-ScheduledTask LaptopHub -Confirm:$false`. Tailscale's `serve --bg` setting persists by itself.

## 8. Keep the laptop reachable

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15
```

Keep it plugged in. Closing the lid still sleeps it unless you change Control Panel, Power Options, "What closing the lid does".

## 9. Update

```powershell
cd C:\Users\YOU\laptop-hub
Stop-ScheduledTask -TaskName "LaptopHub"
git pull
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-voice.txt -r requirements-work.txt -r requirements-laptop.txt -r requirements-screen.txt
python -m hub doctor
Start-ScheduledTask -TaskName "LaptopHub"
```

Your config, notes and data live in `C:\Users\YOU\.laptop-hub` and are not touched. Reload the app on the phone afterwards (the service worker updates itself). A new APK is only needed when `mobile/` changed.

## 9b. Remote workspace (company VPN + NiFi from the phone)

The Remote screen has a **Workspaces** row. One tap connects the Cloudflare WARP agent, opens `https://nifi.onestack.in` in the laptop's browser, starts the live view and then asks you to enable control. First make the "connect" button on the laptop (quick buttons can only be added here, never from the phone):

```powershell
python -m hub add-script warp_connect --label "Connect Cloudflare" -- "C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe" connect
```

Then restart the hub, open Remote on the phone, tap **+ New**, **Add NiFi template**.

- Check the path first: that is the usual install folder, but verify it in File Explorer. Run `& "C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe" status` in PowerShell: it should print the connection state.
- Your company policy may block command-line control of WARP (the command then fails or asks for a login). If so, open the Cloudflare app by hand once and remove the button from the workspace: the launcher then only opens the URL.
- If the button fails, the phone shows the error and offers **Open anyway**, which skips the buttons.
- Control stays off until you tap **Enable control** and confirm. A workspace never turns it on by itself.

## 10. Verify each feature in 10 minutes

Tick as you go. Anything that fails is worth writing down: none of this has been run on real hardware yet.

- [ ] `python -m hub doctor` shows no `[FIX]` lines you care about
- [ ] Phone opens the `https://...ts.net` address and logs in with the token
- [ ] **Home** loads with no error banner
- [ ] **Talk**: type "what time is it on my laptop"; a reply appears. Ask it to write a note; an approval card shows; Approve works
- [ ] **Talk, hands-free voice**: allow the mic, say a sentence, hear a reply (needs voice installed)
- [ ] **Notes**: record a voice note; it saves with a title and tags
- [ ] **Files**: browse a root, open a text file, edit it in a writable root, delete it, restore it from the trash
- [ ] **Ask my files**: index finishes, a question returns an answer with a source
- [ ] **Reminders**: add "in 2 minutes test"; it appears in Notifications when due
- [ ] **To-do / Habits / Journal / Focus**: add one of each; start a 1-minute focus timer
- [ ] **Learn**: make a quiz from a note; review one flashcard
- [ ] **Work**: Asana tasks and today's meetings load (needs Jarvis dir and Asana token)
- [ ] **Inbox**: recent mail lists (needs the Jarvis Gmail app password in the keyring)
- [ ] **Watch**: turn on mail watching, send yourself an "urgent" test mail, see an alert
- [ ] **Remote screen**: the picture appears; arm control for 1 minute, tap once, then it disarms itself
- [ ] **Laptop**: take a screenshot; lock action (only if you can unlock)
- [ ] **Terminal**: opens PowerShell and runs `dir`
- [ ] **Routines**: run one harmless routine
- [ ] **Search**: the top search finds a note you made
- [ ] **ntfy push** (optional): Settings, Notifications, set a long random topic, subscribe in the ntfy app, send a test
- [ ] **Android app**: Notification access on (and "Allow restricted settings" on Android 13+), press Test in Settings > Phone capture, a "Paid Rs 1" item appears in Spending under "Needs a category"
- [ ] Reboot the laptop; after you log in the app still connects
