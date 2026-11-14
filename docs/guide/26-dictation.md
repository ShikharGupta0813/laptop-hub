# 26 · Dictation: talk and it types

**What this page is for:** type with your voice into any program on the laptop, using a key you hold.

## Switch it on
Settings, "Dictation (laptop)". Turn on **Dictation on**. On the laptop run once: `python -m pip install pynput sounddevice`. If a package is missing, the sheet says so in one sentence.

## Use it
Hold **Ctrl+Alt+Space** (change it in the sheet: click "Press the keys"), say a sentence, let go. A short beep means it is listening; a rising chime means the text was typed. The words land in **whichever window has focus**: an editor, a browser, a chat box. Choose "press once to start, again to stop" if you do not want to hold a key. The **Pause** switch ignores the key without losing your settings.

## What it does to your words
Say "comma", "full stop", "question mark", "colon" and the symbol is typed instead (you can edit that list). The first letter is capitalised and a space follows, so you can dictate the next phrase straight away. It **never presses Enter**: "new line" becomes a space unless you switch on "New line may press Enter". Say "scratch that" on its own to delete what was just typed (only while you are still in the same window). Optional: "send to Jarvis, ..." sends the rest to the assistant and speaks the answer instead of typing it.

## Safety
- REDLINE cannot tell if the window in front is a password box. The **blocklist** lists words in window titles or program names (password, keepass, bitwarden, 1password, lastpass) where it will not record or type at all. Add your bank, your vault, anything private. This works on Windows.
- If you switch windows while it types, it stops.
- Your voice is turned into text by Whisper on the laptop. No audio leaves it and none is saved. The words are not logged either.

Details for builders: `docs/api/dictation.md`.
