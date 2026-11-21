# 45 · Replies, insights and personal outreach

**What this page is for:** three helpers around the Job tracker. Replies sorts the job mail you get. Insights shows what is working and gives you one experiment a week. Outreach lets you write to people you chose, in one reviewed batch. Apps > Job tracker, and Apps > Outreach.

## The rule that never changes
**Nothing is sent, and no status changes, without your tap.** REDLINE only reads, counts, drafts and suggests.

## 1 · Replies (top of the Job tracker)
Every 30 minutes (and when you tap **Check mail now**) REDLINE looks at your recent mail **on this laptop**, with plain rules, no AI. If a mail looks like it belongs to an application you track, you get a card:

> Acme replied: an interview invite. Mark as interview?

- **Mark interview** (or rejected, offer...) changes the status after you confirm. A rejection or an interview also stops the follow-up reminders for that application. **Dismiss** throws the card away and changes nothing.
- It knows rejections ("unfortunately...", "not shortlisted", "keep your profile in our database"), interview invites ("HR round", "schedule a call", "share your availability"), tests (HackerRank, take-home), offers, recruiters ("came across your profile", "current CTC"), and automatic "thank you for applying" mails.
- If it cannot tell which application a mail is about, the card says so and you pick one.
- Off when no mail account is connected. Switch it off with `replies_enabled: false`. `replies_ai` (default off) lets a model **on your laptop** look at mails the rules could not sort.
- Friday's brief and Today say, for example, "2 replies to review, 1 interview invite".

## 2 · Insights (a tab on the Job tracker)
Everything is worked out on your laptop from your own tracker.
- **Funnel:** how many applications got a reply, an interview, an offer, by source, kind of role, resume version, time of day and weekday.
- **Rejections:** at which stage they came, after how many days, and what rejected applications have in common.
- **Does the score predict replies?** Reply rate for low, middle and high scored applications. It says "not enough data" until it has 9 scored ones.
- **Skills postings ask for:** the skills that appear most in the postings you saved or applied to and that are **not in your facts**. This is a report. REDLINE never adds a skill for you; add one in Facts only if it is true.
- **This week's experiment:** one idea, for example "Try the Data resume for your next 10 applications". Tap **Start it** or **Skip**. Next week the result shows, with a warning that small numbers are only a hint.

## 3 · Outreach
For 5 to 20 personal emails a day to people you picked.
1. **People:** tap **Add a person**. You type the address yourself and tick that you got it from the person or their public page, and add the link to that page (not needed for someone from your Networking contacts). REDLINE never guesses or looks up addresses. Pick the people for today.
2. **Draft emails:** each email is written for that person from your confirmed facts (the company, the role, one fact that fits). If no fact fits, it says nothing about you. Open **Batch**, read them, edit any, untick any.
3. **Send N emails with one tap:** you confirm once; they go from your own Gmail one by one, about 3 minutes apart. An email cannot be taken back.
4. **Follow-ups:** 5 days later (change with `outreach_followup_days`) you can draft **one** follow-up per person who has not replied. It is its own batch and needs its own tap.
5. **Opt-out:** anyone you mark, or who replied "no thanks" or "unsubscribe" and you confirmed it on the Replies card, is never written to again, even if they were already in a batch. Every email ends with a line offering to stop.

Hard limits that you cannot raise: 20 people in a batch, 20 emails a day (the setting `batches_outreach_cap` can only lower it), no attachment unless you tick "attach my resume".

## If something looks wrong
- "Only N emails fit today": the day limit counts what was sent and what waits in a batch. Discard a batch you do not want.
- An email shows "missing info: your name": add your name in your profile, then edit the draft.
- No Replies cards: connect a mail account in Settings; mail is only read on this laptop.

## About this guide

It matches the project as of its `HANDOFF.md`. If the code changes, update the page that mentions it. Details for programmers: `docs/api/replies.md`, `jobinsights.md`, `outreach.md`.
