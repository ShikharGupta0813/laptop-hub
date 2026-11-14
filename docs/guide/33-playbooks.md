# 33 · Playbooks: "when this happens, do that"

**What this page is for:** write your own rules that connect something that happens (a message, an email, a meeting ending, a time of day) to a few steps REDLINE does for you.

## The idea
A playbook has three parts: **When** (the trigger), **Only if** (optional conditions), **Then do** (up to 20 steps). Examples you can build: "when my manager asks for status, draft a reply and tell me", "every Friday at 4 summarise my week", "when a meeting ends, list my action items", "when I go into a meeting, hold my notifications".

Apps > Playbooks starts empty. Tap **Start from a template** for five labelled examples (each is added **switched off**, so you can read and change it first), or **New playbook**. Nothing runs until you turn its switch on.

## Building one
1. **When**: pick the trigger (a schedule, an urgent alert, a message that mentions you, a calendar event, a meeting with finished notes, a task that is due, new mail, a ready application, where you are, Resume after Panic, the Run button, or a webhook) and fill in its filters (who, which words, which priority).
2. **Only if**: add conditions on the event (sender contains, priority at least, text matches), a time window (weekdays 9 to 6), the places you must be, a limit per day and a cooldown between runs.
3. **Then do**: add steps. *Tell me* sends you a notification (and can say it aloud); *Draft a reply* writes a draft (never sent); *Ask the assistant* runs a question through the free models first; *Save a task* and *Remember something* always ask you; *Run an assistant tool* lets a step use any tool the assistant has; *Hold notifications*, *Wait*, and *Continue only if* complete the set. Put `{event.sender}`, `{event.text}` or `{step1.output}` into text to use what happened. These are inserted as plain text, never as instructions.
4. Tap **Save and test**: a test with a sample event shows what each step *would* do. Nothing is sent, saved or run.

## What keeps it safe
- A playbook never has more power than you. Anything that changes something asks you first, exactly as in chat, with the same levels (a critical action needs you to type CONFIRM). Sending email, applications or posts asks **every time**. If you created a trust rule for that exact kind of action, it can run without asking, as it would in chat.
- When something needs you, the run waits and you get a notification; open Playbooks to Approve or Deny. Nobody answering for 10 minutes counts as no.
- Text that came from outside (a message, an email, a webhook, meeting notes) can never make a playbook change anything. A run started by such an event only lets steps that tell you things or draft things go ahead; steps that save or change something are refused and the run says why.
- Steps cannot run shell commands, change Settings, turn on Full Control, press Panic, or edit playbooks. A playbook cannot start itself or loop through others (at most two playbooks deep).
- **Panic** stops every playbook run and pauses them all; **Resume** brings back only the ones you had turned on. The master switch at the top turns all playbooks off at once.
- REDLINE's assistant can list your playbooks, run one, and turn one on or off (it asks first). It **cannot create or edit** them: you do that here.

## Looking back
Every run is logged: what started it, each step and its result, what it asked you, and why a step failed. Tap a run to see it, **Run again** to repeat it, or **Cancel run** to stop one that is waiting. The audit screen shows the same events (no message text beyond 200 characters, never a secret).

## Webhooks
Pick the *A webhook is called* trigger, save, then tap **Make webhook secret** on the playbook (shown once). Call `POST <your hub address>/hooks/playbook/<id>` with the header `X-Webhook-Secret` and JSON `{"title": "...", "text": "...", "sender": "..."}`. What it says is treated as outside text.

Details: `docs/api/playbooks.md`. Back to the [guide index](README.md).
