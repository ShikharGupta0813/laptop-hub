# 20 · Applications: drafts waiting for your tap

**What this page is for:** how the application queue works and why it never sends anything by itself.

## What it is

When the job and gig finder spots something that fits you, a draft application is prepared from your profile and placed in the **Ready** tab (Apps, Applications). You look, edit, and decide.

- **Honest drafts.** A draft uses only facts from your profile. Where it needs something it does not know (your rate, when you can start) it leaves a `[FILL: ...]` marker and lists it under "Fill in before sending".
- **You press Send.** Email applications have a Send button (you pick which Gmail account). Website applications give you Copy and Open, then you tap **I applied**. Nothing is sent without your tap or your approval.
- **Daily cap.** After 10 sends in a day (setting `applyq_daily_cap`) the app asks once more.
- **No undo for email.** Once Gmail has it, it is gone.

## Fill the form on my laptop (optional)

For a website application you can let the laptop do the typing. Tap **Fill the form on my laptop** on the item: a browser window opens the page, fills your name, links, notice period, expected rate and similar answers from your profile, and **stops**. You see a screenshot on your phone, a list of what it filled, and a list of what **needs you**. Only your tap on the separate **Submit now** button (after ticking "I have looked at it") clicks Submit, and only if nothing on the page changed since the screenshot you saw.

Set up once on the laptop: `pip install playwright`, then `python -m playwright install chromium`, then set `formfill_enabled` to `true` in `hub.json`. If the site wants a login, tap **Open login window**, sign in yourself in the window (your password stays in that browser on the laptop), close it, and fill again.

What it will not do: fill ID numbers, date of birth, salary history, gender or ethnicity questions, bank details, passwords or any "I agree / I certify" box; guess an answer your profile does not hold; solve a captcha or get around a login or two-step code; open LinkedIn (it is on a never list you can edit) or any private address. If a site blocks it, use Copy instead. It closes itself after 5 idle minutes. Friday asks "want me to fill it on the laptop?" and `submit_form` always asks for your tap, even in Full Control.
Details: [docs/api/formfill.md](../api/formfill.md).

## Say or type

- "What applications are ready?" (Friday lists them)
- "Show me the Acme one", then "Send application <id>": an approval card appears; one item at a time.
- "Skip that one", "I applied to Acme".

## The screen

Header: sent today, ready, replies, streak, this week. Tabs: Ready, Sent, Skipped. "Add one": paste a link or the text of a posting. Replies are noticed when Gmail search is available and move an item to Replied.
Friday adds one line to Today ("Applications today: ...") and a nightly "Today: N applied, M ready, K replies".

## Good to know

- ✅ The queue works without Gmail or a profile: drafts then say what is missing, and Send becomes Copy.
- ⚠️ Check the draft. It is a starting point written from your own profile text.
- Details: [docs/api/applyq.md](../api/applyq.md).
