# 10 · Security and privacy

**What this page is for:** what REDLINE can do on your laptop, what protects it, what you must never share, how to change your keys, and what a stranger could do.

## The honest summary

A logged-in phone can do almost anything you can do at the laptop. That is on purpose. So the phone and the token must be protected like a house key.

> ⚠️ **Careful:** Lock your phone. Anyone with your unlocked phone and the app logged in has the keys to your laptop.

## What the app can do

| Power | Limit |
|---|---|
| Run commands in a Terminal | None. Full access as you. |
| Let the assistant run commands and write files | You approve each action, unless you switch approvals off for a session. |
| Read your shared folders | Only the folders you chose |
| Change files | Only in `writable_roots` (default `notes` and `inbox`) |
| Watch the screen | Needs login |
| Click and type on the screen | Needs arming, 1 to 15 minutes |
| Lock, sleep, restart, shut down | Sleep, restart and shutdown ask first |
| Read Gmail | Read only. Drafts replies. Never sends from the Inbox. |
| Send email or post to Chat | Only from Work, after you review and tap |

## What protects it

```mermaid
flowchart LR
    S["Stranger"] --> T["Tailscale: only your devices"]
    T --> H["Hub checks address and header"]
    H --> L["Token login"]
    L --> A["Approval cards"]
    A --> Y["Your laptop"]
```

| Layer | What it does |
|---|---|
| **Tailscale only** | The hub listens on the laptop itself. Nothing is open to the internet. |
| **Login token** | One long random password. Only its **hash** is stored. |
| **Lockout** | Five wrong tries lock login for one minute. |
| **Signed cookie** | Keeps you logged in for 14 days. It cannot be read by page scripts and is not sent from other sites. |
| **Address check** | The hub answers only to localhost and `ts.net` names. |
| **Header check** | Every change request must come from the app. |
| **Approval cards** | Show the whole action. The AI cannot skip them. |
| **Safety is code, not AI** | Approvals, path limits and protected files are enforced by the program. |
| **Data, not orders** | Text in files, mail and command output is treated as data. It cannot tell the assistant what to do. |
| **Protected files** | The assistant cannot rewrite the hub's own settings or the activity log. |
| **Remote control timer** | Control switches itself off. |
| **Trash** | Deleted files can be restored. |
| **Activity log** | `audit.log` records logins, terminal open and close, quick buttons, assistant requests and approvals. |

> ⚠️ **Careful:** The activity log does **not** record what you type in the Terminal. And one known gap: after you approve a command, it can still touch the hub's own settings. Approve with care.

## What never to share

| Never share | Why |
|---|---|
| Your login token | It is the key to the hub. |
| Gemini, Anthropic, Asana, ntfy, Gmail app password | Each opens that service as you. |
| Your calendar's secret iCal link | Anyone with it can read your calendar. |
| Chat webhook URLs | Anyone can post into that Space. |
| The ntfy topic | Anyone who knows it can read your alerts and send fake ones. |
| `hub.json` | It holds your settings and session secret. |
| Screenshots of the setup wizard, Settings or Terminal | They may show the topic or other private data. |

> ✅ **Check:** Your Tailscale address (`shikharonestack.tail3ffc35.ts.net`) is not secret on its own. Without your Tailscale account and the token, it is useless.

## Rotate things

```mermaid
flowchart TD
    A["Something leaked or phone lost"] --> B["New login token"]
    B --> C["Log out all devices"]
    C --> D["Replace the leaked key"]
```

| Situation | What to do |
|---|---|
| **Phone lost or stolen** | Run `python -m hub token`, restart the hub, and remove the phone from your Tailscale account. |
| **Token leaked** | Same: `python -m hub token`, then restart the hub. |
| **Log everyone out** | In the app, **Settings**, **Log out of all devices**. |
| **Gemini key leaked** | Delete it at aistudio.google.com/apikey, make a new one, run the Gemini setup. |
| **Gmail app password leaked** | Revoke it at myaccount.google.com/apppasswords, make a new one, run the Gmail setup. |
| **Asana token leaked** | Delete it in Asana's developer console, make a new one, run the Asana setup. |
| **ntfy topic leaked** | Run the phone setup to get a new topic, subscribe to it, drop the old one. |
| **Calendar link leaked** | Reset the secret address in Google Calendar, run the calendar setup. |
| **Chat webhook leaked** | Delete the webhook in the Space, make a new one, run the chat setup. |

Commands:

```powershell
python -m hub token
```

```powershell
python -m hub setup --only gemini
```

```powershell
python -m hub setup --only phone
```

```powershell
python -m hub setup --only gmail
```

```powershell
python -m hub setup --only asana
```

> ⚠️ **Careful:** `python -m hub token` prints the new token **once**. Save it in a password manager. Then restart the hub, or the old token keeps working.

## What a stranger could do if they had...

| If they had | They could | They could not |
|---|---|---|
| Your Tailscale address only | See the login page, if they are on your tailnet | Log in, or even reach it from outside |
| The address and a wrong token | Try five times a minute | Get in by guessing |
| Your login token, but not your tailnet | Nothing, because they cannot reach the hub | |
| Your token **and** a device on your tailnet | Do almost anything you can do on the laptop | Skip approval cards in the assistant |
| Your unlocked, logged-in phone | The same, including the Terminal | |
| Your ntfy topic | Read your push alerts, send fake alerts | Reach the hub |
| Your Gemini key | Spend your free quota | Reach the laptop |
| Your `hub.json` | See your folder list and settings | Log in. It holds a token hash, not the token. |
| A backup zip | Read your notes and data | Log in. It has no token hash or secrets. |

## Privacy summary

- **Local model and Whisper** keep your words on the laptop.
- **Gemini and Claude** receive what you send them. Pick **Local** for private things.
- **Phone push** uses the public ntfy.sh server. Keep secrets out of reminder and note titles.
- **Payment, Gmail and Chat capture** reads only listed apps' notifications, and sends them only to your own hub. 🟡 Not tested on real devices yet.
- **SMS** permission, if you grant it, is receive-only. The app cannot read your inbox.
