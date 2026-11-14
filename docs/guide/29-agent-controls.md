# 29 · Agent controls: Panic, trust rules and "What did the agent do?"

**What this page is for:** stay in charge of an assistant that can act on your laptop.

## Panic
The red stop button at the top of every screen (also Settings, Agent controls, and the Home quick actions). **One tap** stops the assistant everywhere: running replies, waiting approvals, critical countdowns, remembered approvals, trust rules, Full Control, the wake word, dictation, MCP servers, form filling, Council and lane runs, deep thinking, research and the background helpers (Autopilot, opportunities finder, alerts, proactive pushes). A toast offers **Undo** for 10 seconds. Until you tap **Resume** in the "Agent paused" banner nothing that changes a file, runs a command or sends something will run. Resume restarts only what Panic stopped; Full Control and trust rules stay off, you switch them on again yourself.

You can also say or type **"Jarvis stop everything"** or **"panic"**. Want a safety net against accidental taps? Settings: `panic_confirm`. The phrases are editable (`panic_phrases`).

## Trust rules
When you have approved the same kind of change (same program in the same folder, for example) ten times without ever saying no, REDLINE offers **Do this automatically from now on**. A rule lasts 30 days (extend it any time), runs at most 20 times a day, and shows how often it fired. It never covers critical actions, anything that sends or publishes, MCP tools, or work after the assistant read web pages or mail. Saying no to it once, a Panic, or any Full Control change cancels the rule. Manage them in Settings, Agent controls, Trust rules.

## What did the agent do?
Settings, Agent controls, or the Home shortcut. A timeline of tool calls, approvals (and who approved: your tap, your voice, a trust rule, Full Control), denials, critical attempts, things sent out, MCP calls, Panic events, setting changes and logins. Filter by day, kind, level and outcome, search, tap a row for details, **Export CSV**. Secrets are never shown.

Details for builders: `docs/api/panic.md`, `docs/api/trust.md`, `docs/api/audit.md`.
