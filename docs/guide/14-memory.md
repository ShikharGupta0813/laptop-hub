# 14 · Memory: Jarvis and Friday remember for you

**What this page is for:** how to hand things to your assistants and get them back later, by voice, text or the Memory screen.

## What it is

A small private notebook on your laptop. Two voices share it:

| Voice | Owns | Examples |
|---|---|---|
| **Jarvis** | the technical side | RCAs, decisions, facts, "what was the fix", "can this be done?" |
| **Friday** | people and time | what someone asked you, follow-ups, "tell me tomorrow", what to say to your manager |

## Say or type

- "Remember the staging box is on port 8081." Saved at once, answer: "Saved."
- "Jarvis, this is the RCA of the login timeout: the proxy idle limit was too low. Remember it."
- "Jarvis, what was the RCA we did for the login timeout?" The answer includes the date.
- "Friday, remember I owe Ravi the report and tell me tomorrow at 9." A real reminder is made, so your phone gets the push.
- "Someone gave me a task: Ravi wants all invoices exported in one call." Jarvis looks at it with your memory and tells you "this can be done", "partly", or "this can't be done yet because ...".
- "What are my follow-ups?" or "list my tasks".

## The Memory screen

Apps, Memory. Search box, filters (All, RCA, Decisions, Tasks, Notes; Jarvis or Friday), a quick-add box, a "Someone gave me a task" box, and a box to ask Jarvis about what you see. Task cards show a green, amber or muted-red verdict badge with the blockers and questions.
Today shows the follow-ups due today, and Friday reads them in her brief.

## Good to know

- ✅ Task checks use the free AI (Gemini or your local model). The paid Claude API is never used.
- ⚠️ Saved text stays on this laptop in `brain.sqlite` (hub data folder). Turn the feature off with `brain_enabled` in Settings.
- 🟡 Not tested on real devices yet. Built and tested with fake data.
