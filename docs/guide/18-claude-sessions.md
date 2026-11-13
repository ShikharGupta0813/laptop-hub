# 18 · Claude sessions: watch Claude Code from your phone

**What this page is for:** see what Claude Code is doing on your laptop and nudge it, without walking to the desk.

## What you see
Open **Apps, Claude**. Each card is one Claude Code session on the laptop:
- a **cyan pulsing dot** means Claude is working, an **amber dot** means it is waiting on you, a grey dot means it is idle (nothing for 30 minutes);
- the project folder, your last message, Claude's last answer, and how long ago.

Tap a card to read the last few turns. If Claude has been waiting on you for 5 minutes you also get one notification ("Claude is waiting on you in ...").

## Tell it something
Open a card, type in **Tell it...** and tap **Send**. You are asked to confirm and see the exact text. The hub then runs `claude --resume` on the laptop in that project and shows Claude's reply here when it is done (it can take minutes). If Claude is working in that session right now, sending is refused: wait until it is waiting.

## Good to know
- It reads Claude Code's own session files in `~/.claude/projects`. Nothing is changed by looking.
- It uses your own Claude login, never a paid API key.
- Text from the sessions is shown as plain text and treated as untrusted: it can never make Friday do things by itself.
- Turn it off with `ccsessions_enabled: false` in hub.json.
