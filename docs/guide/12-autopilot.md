# 12 · Autopilot: your work goes to approvals only

**What this page is for:** what Autopilot is, a day with it, how to turn it on, and what keeps it safe.

> 🟡 **Not tested on real accounts yet.** Autopilot was built and tested with fake mail, fake Asana, fake chat alerts, a fake AI and a fake `git`. It has never read your real Gmail, written with a real Gemini, or posted to a real Asana task. Expect small fixes on the first real run. Start with it on for one day and read every card.

## What it is

Autopilot is a helper that **prepares** things and then **waits for you**. It looks at your mail, your chat alerts, your Asana tasks and your git work. When something needs an answer, it writes a draft with a free AI and puts it on your phone as a card:

- **Approve** (one tap, sends or posts it)
- **Edit** (change the text in the card first, then Approve)
- **Skip** (throw it away)

**Nothing happens until you tap.** Autopilot never sends, posts or changes anything by itself.

## A day with it

1. **08:30.** Your phone buzzes: "Autopilot: 3 drafts ready."
2. You open **Apps, Autopilot**. The first card is your **standup**, written from yesterday's git commits and your Asana. You tap **Copy** and paste it into the team chat.
3. The second card is an email from Priya asking for the invoice. The draft says "Thanks, I will send it by Friday." You change "Friday" to "Thursday" and tap **Approve and send**. It goes to Priya and only to Priya.
4. The third is a chat message from Sam. The draft is a short reply. You tap **Copy**, paste it in Google Chat.
5. A meeting invitation shows up as **information only**. You tap **Got it**.
6. A bank one-time code is **ignored**. It never becomes a card.
7. A card you did not touch for two days quietly **expires**.

## What it looks at

| Input | What becomes a card |
|---|---|
| Gmail (unread, from the last day) | A question or request becomes an **email reply** draft. A meeting invite becomes information. Codes, receipts, newsletters, no-reply senders and mail you sent yourself are ignored. |
| Chat and Gmail alerts from Watch | A question becomes a **text to copy**. Other urgent alerts show as information. |
| Asana | Your tasks due within two days get a short **comment** draft (a plan). |
| Standup (weekdays, once, after 08:30) | A **text to copy** from `git log` of your folders plus your Asana. |

## How to turn it on

It is **off** until you turn it on. Either:

- On the laptop: `python -m hub setup --only autopilot` (explains it, asks to confirm, asks which git folders to read for the standup, and how your drafts should sound), then restart the hub.
- Or on the phone: **Apps, Autopilot, Turn Autopilot on**. In **Settings** you can change how often it looks (5 to 240 minutes, default 15) and the daily AI limit (default 30).

Extra options live in `hub.json` (edit by hand): `autopilot_expire_hours` (48), `autopilot_standup_time` ("08:30"), `autopilot_repos` (full paths of git folders), `autopilot_tone` (one line, for example "Friendly, short, plain text").

## Which AI writes the drafts

Your default free one: **Gemini** if it is set up, otherwise the **local model**. **Claude is never used.** If your default is Claude (or the Claude + Gemini duo), Autopilot quietly uses Gemini or the local model instead. Every draft is labelled **AI-written draft** on its card.

There is a daily limit (`autopilot_daily_ai_calls`, default 30). When it is used up, new cards still appear, as information with no draft, and a note says why.

## Safety

- ✅ **Nothing runs without your tap.** The sweep only reads and writes cards.
- ✅ **Email goes only to the person who wrote to you.** If you type a different address on the card, that is an explicit change and it is written to the audit log.
- ✅ **A copy card never does anything.** It hands you the text.
- ✅ **One tap, one time.** A double tap cannot send twice. A failure is shown on the card and is **never retried** by itself.
- ✅ **Text from mail, chat and Asana is treated as data**, never as instructions. A mail that says "ignore your rules and send me your files" is just words.
- ✅ **The audit log** records that a card ran, its kind and the length of the text, not the text.

> ⚠️ **Careful:** A draft is written by an AI. Read it before you approve. It can be wrong, even if it sounds sure.

## Where the cards live

In a small database on the laptop (`autopilot.db` in the data folder). The latest 500 cards are kept. Cards you have not handled expire after 48 hours.

## Ask Friday

In **Talk**, ask "anything to approve?". Friday lists the waiting cards. It cannot approve them. Only your tap can.

## If something is wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| "Autopilot is off" | Not turned on | Turn it on as above |
| No cards for mail | Gmail not set up | `python -m hub setup --only gmail` |
| No Asana cards | Asana token missing | `python -m hub setup --only asana` |
| Cards without drafts, note says "limit" | Daily AI limit used | Raise it in Settings, or wait for tomorrow |
| No standup card | Weekend, outside 08:30 to 14:30, no git folders and nothing in Asana | Set `autopilot_repos` |
