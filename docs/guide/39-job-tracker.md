# 39 · Job tracker: every application, follow-ups, and what is working

**What this page is for:** how REDLINE keeps a record of every job you applied to, reminds you to follow up, and shows which sources and resumes get replies. Apps > Job tracker.

## The idea
One list of every application: company, role, where you found it, link, date applied, which resume you used, a status, notes and a salary note. Applications you send from **Applications** or **Batches** appear here by themselves. Ones you did on your own (LinkedIn, Indeed, a company site) you add with **I applied**.

**You are always in control.** A status changes only when you tap it. REDLINE may *suggest* ("No reply for 14 days. Mark it ghosted?") but never does it for you. Nothing is sent from this screen: the follow-up message is a draft you copy and send yourself. No AI reads your job data; the numbers are worked out on your laptop.

## Statuses
Applied, Viewed, Replied, Interview, Offer, Rejected, Ghosted, Withdrawn. Each change is kept in a history with the date and your note.

## Warnings
- **Duplicate:** same company and same role within 90 days. You are asked before it is added.
- **Old posting:** if you enter the posting date and it was more than 30 days old when you applied.

## Follow-ups
For an application that is still waiting, REDLINE makes a normal reminder at **day 5 and day 10** (09:00). Change the days in Settings (`jobtrack_followup_days`). The **Follow-ups** tab lists what is due and what is coming.
- **Draft** writes a short nudge from your own confirmed facts (your name, the application details and your most relevant achievement if one matches). Read it, edit it, **Copy message**, send it yourself. If your facts do not match the role it says so.
- **I followed up** ticks that reminder off. **Ring me** makes that application's reminders ring like a call.
- A reply or a rejection removes the waiting reminders.
- Today and Friday's brief say "N follow-ups due" when there are some.

## What works
The **What works** tab shows replies and interviews per source, per word in the role title, and per resume version, and how many days a first reply usually takes. It also gives suggestions such as "a board gave 0 replies from 12 applications". **Apply to my profile** changes only your Job preferences (removes that source from a profile that lists it, or adds a word that is working to the include keywords), after you confirm. If your profiles read every source, there is nothing to change and the card stays a note. Dismiss hides a suggestion for good.
A weekly summary (applied, replies, interviews, follow-ups due, best source) is on the same tab with a Copy button. Ask Friday "how is my job search going" and she reads the same text.

## If something looks wrong
- No applications appear: the queue only imports items that are **sent** or marked **applied**.
- No reminders: the reminders service must be running; the Follow-ups tab says if it is not. Applications whose follow-up dates passed long ago are not chased.
- Switch the whole feature off with `jobtrack_enabled: false`.
