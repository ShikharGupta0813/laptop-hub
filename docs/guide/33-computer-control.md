# 33 · Computer control: let Jarvis click and type for you

**What this page is for:** Jarvis looks at your screen and uses your mouse and keyboard to do a small task in an app, one step at a time, while you watch.

> **WARNING. This moves your real mouse and types on your real keyboard.** Treat it like handing your laptop to a very fast, very literal assistant who sometimes misreads the screen. It is **off** until you turn it on, it only works in the apps you pick, and you can stop it at any moment. Do not use it for anything you could not undo, and never leave it running unattended.

## What keeps it safe
- **You approve the scope first**: a card says, for example, "Jarvis may click and type in Notepad for up to 25 steps." Nothing moves until you tap **Approve and start**. Jarvis can ask for a task, but only you can approve it.
- **Only the apps you pick.** If another window comes to the front, Jarvis pauses and asks.
- **Some things are never touched**: password managers, banking pages, the Windows permission prompt, Windows Security, remote-desktop tools, this app itself and terminals. You can add words to that list in Settings.
- **Risky steps need your tap, every time**: anything that looks like deleting, sending, paying, installing or changing security settings pauses with a picture of the screen and asks "Allow this one step?". This cannot be turned off, and Full Control and "remember my approval" do not apply.
- **Password fields are never typed into** and are blacked out before any picture goes to a cloud AI.
- **Your mouse wins.** If you move it, Jarvis stops and waits. **Take over** does the same on purpose. If you walk away from the laptop, it pauses.
- **A big red STOP** is always on the screen. Emergency stops: push the mouse into the **top-left corner** of the screen, press **Ctrl+Alt+Shift+Esc**, tap STOP on the red bar at the top of the laptop screen, or press the Panic button.
- **A local AI first.** By default a model on your own laptop (through Ollama) looks at the screen. If you choose a cloud AI instead, the card says so: pictures of your screen then leave the laptop.

## Set it up
1. Windows only for now. Run `pip install pyautogui pywinauto` (see `requirements-extra.txt`; `requirements-screen.txt` gives the screen capture). `pywinauto` is optional but lets Jarvis click controls by name and lets the hub see password fields; without it the screen says "Coordinates only".
2. Add a vision model in Settings > Vision providers, for example `ollama pull llava` and a "llava (Ollama)" entry. Small models are not very good at finding things on a screen. Try `qwen2.5vl` or `llama3.2-vision` if you can.
3. Apps > **Computer control** > **Turn on computer control** and confirm.

## A first safe test: Notepad
1. Open **Notepad** (an empty, unsaved one) and keep it on the screen.
2. On the Computer screen type the task: `Type the words hello from Jarvis in the Notepad text area.`
3. Under "Which open apps", tick **notepad** only. Switch **Allow typing** on. Leave the steps at 10. Tap **Review the scope**.
4. Read the card. It should say Jarvis may click and type in notepad for up to 10 steps, and show which AI looks at the screen. Tap **Approve and start**.
5. Watch the picture: a red circle shows where Jarvis plans to click next. It should click the text area, type, and say it is done. Try **Pause**, **Take over** (move the mouse yourself), then **Resume**, and finally **STOP**.
6. Notepad is empty and has no files to lose, so nothing here can go wrong in a way that matters. Once that works, try something small in File Explorer (rename a test file) before anything else. A step that mentions "delete" will stop and ask you.

## Asking by voice or text
"Jarvis, on my screen, rename these three files in Explorer", "do this in the app for me". Jarvis asks you to confirm the request (the same card you know from sending mail), then creates the pending task. Then open the Computer screen and approve the scope. That is two taps on purpose.

## Good to know
- Every step is in the audit log (the Audit screen): what it did, in which window, and who approved. Typed text is cut short there and left out for anything password-like.
- Pictures are kept in memory plus a small folder of the last 20, which is deleted when the task ends (Settings > Keep the last 20 pictures changes that).
- It does one task at a time, at most 100 steps, at most 20 actions a minute, and stops by itself at the time limit.
- When it is unsure it asks you. Answer by finishing the step yourself and tapping **Resume**, or **Stop**.

Details and the exact list of "never": `docs/api/computeruse.md`. Back to the [guide index](README.md).
