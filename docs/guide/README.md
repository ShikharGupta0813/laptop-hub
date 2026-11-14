# REDLINE beginner guide

**What this page is for:** the front door. It says what these pages are and the order to read them.

REDLINE is an Android app that controls your Windows laptop. This guide explains it in plain words, for someone who did not write the code. You can hand these pages to a new person and they will understand.

## Read in this order

1. [00 · The big picture](00-big-picture.md): what REDLINE is and how it fits your work.
2. [01 · Your first day](01-first-day.md): what to install, first login, a tour of the screens.
3. [02 · How it works](02-how-it-works.md): the five pieces, where data lives, what is private.
4. [03 · Technologies explained](03-technologies-explained.md): a glossary of every tool and library.
5. [04 · AI brains](04-ai-brains.md): local, Claude, Gemini and the duo.
6. [05 · Friday, your daily assistant](05-friday-assistant.md): briefs, plans and alerts.
7. [06 · Controlling your laptop](06-laptop-control.md): launcher, screen, terminal, files, approvals.
8. [07 · The Council in the app](07-council-in-the-app.md): running the multi-model team.
9. [08 · Setup and updates](08-setup-and-updates.md): the wizard, updating, auto-start, backups.
10. [09 · Troubleshooting](09-troubleshooting.md): symptom, cause, fix.
11. [10 · Security and privacy](10-security-and-privacy.md): what protects you, what to never share.
12. [11 · Cheat sheet](11-cheat-sheet.md): one printable page.
13. [12 · Autopilot](12-autopilot.md): drafts that wait for your one tap.
14. [13 · Today](13-today.md): your whole day on one page.
15. [14 · Skills](14-skills.md): add community skills for Jarvis and Friday.
16. [15 · Research](15-research.md): ask once, get a report with numbered sources.
17. [16 · Memory](16-memory.md): Jarvis and Friday remember RCAs, decisions and follow-ups.
18. [17 · Build flow](17-build-flow.md): tell Jarvis what to build; spec, plan, then step by step.
19. [18 · Claude sessions](18-claude-sessions.md): watch Claude Code on your laptop from your phone.
20. [19 · Opportunities](19-opportunities.md): finds, sources and your profile.
20. [20 · Applications](20-applications.md): drafted applications wait for your tap; nothing is sent by itself.
20. [21 · Posts](21-posts.md): one draft to many places (dev.to, Hashnode, your blog, LinkedIn, X...), your own destinations list.
20. [22 · Urgent alerts](22-urgent-alerts.md): Friday tells you first, with a draft you send yourself.
20. [23 · Your lists](23-your-lists.md): assistants, notification choices, your own safety and privacy rules, memory kinds and tags.
20. [24 · Provider lists](24-provider-lists.md): search, vision, AI endpoints, skill sources, coding agents.
20. [25 · Local models](25-local-models.md): DeepSeek and other Ollama models, install, deep thinking, speed test.
20. [26 · Dictation](26-dictation.md): hold a key on the laptop, speak, and it types into the window in front.
20. [26 · MCP servers](27-mcp-servers.md): add tool servers (MCP) to a list; every tool call asks first.
20. [28 · Work lanes](28-work-lanes.md): give routine work to the free Council first, escalate only when needed, private work stays local.
21. [99 · Troubleshooting new features](99-troubleshooting-new-features.md): plain fixes for first-run problems.

Also: [How to import this guide into Notion](IMPORT-INTO-NOTION.md).

## How the pages connect

```mermaid
flowchart TD
    A["00 Big picture"] --> B["01 First day"]
    B --> C["02 How it works"]
    C --> D["03 Technologies"]
    C --> E["04 AI brains"]
    E --> F["05 Friday"]
    C --> G["06 Laptop control"]
    E --> H["07 Council"]
    B --> I["08 Setup and updates"]
    I --> J["09 Troubleshooting"]
    G --> K["10 Security"]
    J --> L["11 Cheat sheet"]
    K --> L
    E --> M["12 Autopilot"]
```

## Quick start by goal

| I want to... | Read |
|---|---|
| Understand it in five minutes | 00 |
| Get it running | 01, then 08 |
| Fix a problem now | 09 |
| Know what is safe | 10, 12 |
| Have drafts ready for one-tap approval | 12 |
| Hand it to someone else | 00, 01, 11 |

## How to read the marks

| Mark | Meaning |
|---|---|
| ✅ Check | A good thing to confirm |
| ⚠️ Careful | A risk or a common mistake |
| 🟡 Not tested on real devices yet | Built and tested only with fake data. Expect small fixes. |
| 🟢 Worked on the first real run | Seen working on the owner's laptop and phone |

> ⚠️ **Careful:** No page in this guide contains a real token, key or ntfy topic. Never add one.

## About this guide

It matches the project as of its `HANDOFF.md` (last updated 2026-11-04, with later notes through 2026-11-12). If the code changes, update the page that mentions it.
