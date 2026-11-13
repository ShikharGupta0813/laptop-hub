# 19 · Applications: drafts waiting for your tap

**What this page is for:** how the application queue works and why it never sends anything by itself.

## What it is

When the job and gig finder spots something that fits you, a draft application is prepared from your profile and placed in the **Ready** tab (Apps, Applications). You look, edit, and decide.

- **Honest drafts.** A draft uses only facts from your profile. Where it needs something it does not know (your rate, when you can start) it leaves a `[FILL: ...]` marker and lists it under "Fill in before sending".
- **You press Send.** Email applications have a Send button (you pick which Gmail account). Website applications give you Copy and Open, then you tap **I applied**. Nothing is sent without your tap or your approval.
- **Daily cap.** After 10 sends in a day (setting `applyq_daily_cap`) the app asks once more.
- **No undo for email.** Once Gmail has it, it is gone.

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
