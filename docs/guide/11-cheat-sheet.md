# 11 · Cheat sheet

**What this page is for:** one page to print. The commands, addresses and places you need most.

## Daily

| I want to | Do this |
|---|---|
| Open the app | Turn on Tailscale on the phone, open REDLINE |
| Morning brief | Home, **Brief me** |
| My whole day | Say or type "summary" or "my day", or Apps, **Today** |
| Approve a draft by voice | "What is pending?", then "approve 1" |
| Update REDLINE | Apps, Laptop, Quick buttons, **Update REDLINE** |
| Check the laptop is fine | Home, **Setup** checklist |
| Make a backup | Settings, Backups |
| Log out everywhere | Settings, **Log out of all devices** |

## Commands (PowerShell, in the project folder, venv active)

| What | Command |
|---|---|
| Activate the venv | `.venv\Scripts\activate` |
| Health check | `python -m hub doctor` |
| Start the hub | `python -m hub serve` |
| Setup wizard | `python -m hub setup` |
| Turn on Autopilot (drafts for one-tap approval) | `python -m hub setup --only autopilot` |
| One wizard step | `python -m hub setup --only gemini` |
| Test setup, change nothing | `python -m hub setup --check` |
| Update code | `python -m hub update --restart` |
| Add the update button | `python -m hub install-updater` |
| Back up now (zip, keeps last 8) | `python -m hub backup` |
| Put a backup back | `python -m hub backup --restore C:\path\redline-backup-DATE.zip` |
| Check and repair hub and Tailscale now | `python -m hub watchdog --once` |
| Set up weekly backup / 10-minute watchdog | `python -m hub setup --only backup,watchdog` |
| New login token | `python -m hub token` |
| Add a quick button | `python -m hub add-script NAME --label "Label" -- PROGRAM ARGS` |

Wizard step names: `jarvis`, `gmail`, `asana`, `calendar`, `chat`, `ai`, `gemini`, `council`, `autopilot`, `phone`, `name`, `warp`, `autostart`, `updater`, `backup`, `watchdog`.

Start and stop the background hub:

```powershell
Start-ScheduledTask -TaskName "LaptopHub"
```

```powershell
Stop-ScheduledTask -TaskName "LaptopHub"
```

Show your Tailscale address:

```powershell
tailscale serve status
```

Turn HTTPS on (first time):

```powershell
tailscale serve --bg 8765
```

Read the update log:

```powershell
Get-Content $HOME\.laptop-hub\update.log -Tail 20
```

## Addresses

| What | Address |
|---|---|
| Your hub | `https://shikharonestack.tail3ffc35.ts.net` |
| The code | `https://github.com/ShikharGupta0813/laptop-hub` |
| Build the Android app | GitHub repo, Actions, **Build Android APK** |
| Council project | `https://github.com/ShikharGupta0813/multi-llm-lab` |
| Tailscale admin (MagicDNS, HTTPS) | `https://login.tailscale.com/admin/dns` |
| Free Gemini key | `https://aistudio.google.com/apikey` |
| Gmail app password | `https://myaccount.google.com/apppasswords` |
| Asana token | `https://app.asana.com/0/my-apps` |

## Where files live (replace YOU)

| What | Where |
|---|---|
| Settings | `C:\Users\YOU\.laptop-hub\hub.json` |
| Activity log | `C:\Users\YOU\.laptop-hub\audit.log` |
| Update log | `C:\Users\YOU\.laptop-hub\update.log` |
| App data, trash, backups | `C:\Users\YOU\.laptop-hub\data\` |
| Notes | `C:\Users\YOU\HubNotes` |
| Uploads | `C:\Users\YOU\HubInbox` |
| Asana token file | `C:\Users\YOU\.claude\.asana_token` |
| The code | for example `D:\laptop-hub` |

## Fast fixes

| Problem | First thing to try |
|---|---|
| Cannot find the laptop | Tailscale on? Laptop awake? Hub running? |
| Token rejected after a new one | Restart the hub |
| Gemini no quota | Use Claude login or Local, or a new key |
| Notification switch greyed | Allow restricted settings |
| Anything odd | `python -m hub doctor` |

## Who to ask

| Question | Ask |
|---|---|
| "How do I...?" | These guide pages, then the Apps search box |
| "It is broken" | Run `python -m hub doctor`, then ask Claude with the output (never paste keys) |
| Continue building the project | Open a new Claude chat and paste the resume text at the end of `HANDOFF.md` |
| Network or VPN | Tailscale help at tailscale.com |
| Company mail, calendar or Chat blocked | Your Workspace admin |

> ⚠️ **Careful:** Never print this page with a token, key or ntfy topic written on it.
