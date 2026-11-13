# 99 · Troubleshooting new features

**What this page is for:** plain fixes for the problems you are most likely to hit the first time you try the newer features.

Start with one command. It prints one line per thing and tells you the fix:

```powershell
python -m hub doctor
```

## Quick table

| What you see | Likely cause | Fix |
|---|---|---|
| The microphone is blocked | You opened an `http://` or IP address, or the browser permission was denied. | Open the `https://...ts.net` address. Tap the lock icon in the browser, allow the microphone, reload. |
| Voice mode answers "500" or never replies | The `av` package is too new (version 17 or later) and speech-to-text cannot load. | `python -m hub fix-deps`, then restart the hub. Doctor shows `av ... (needs >=14.0,<17)`. |
| Packages will not install, `pip.exe` is blocked | Windows Application Control blocks `pip.exe`. | Never run `pip` directly. Use `python -m hub fix-deps` or `python -m pip install -r requirements.txt -r requirements-voice.txt`. |
| Today shows no meetings | Google calendar is not connected (the app answers 503). | `python -m hub setup --only calendar`, paste your calendar's secret iCal link, restart the hub. |
| There is no Update REDLINE button | The updater task was never added, or the hub was not restarted after adding it. | `python -m hub install-updater` (or `python -m hub setup --all`), then restart the hub. |
| Update seems to do nothing | Local edits to code files, or no network. | Settings shows the last lines of `update.log`. Run `git status` in the REDLINE folder. |
| Skills, Browse is empty | No internet on the laptop, or GitHub could not be reached. | Check the laptop is online and reopen Browse, or paste a GitHub link like `owner/repo/path` into Install. |
| Research returns nothing | The laptop is offline, the free AI has hit its daily limit, or no Gemini key is saved. | `python -m hub setup --only gemini`. The limit resets daily; raise `research_daily_ai_calls` in Settings if needed. |
| The hub does not answer | It stopped. | `python -m hub serve`, or install the watchdog: `python -m hub setup --all`. |

> ✅ **Check:** After fixing packages, a key or `hub.json`, restart the hub.

> ⚠️ **Careful:** When you ask for help, copy the doctor output. It never prints your token, keys or ntfy topic.

Back to [Troubleshooting](09-troubleshooting.md) or the [guide index](README.md).
