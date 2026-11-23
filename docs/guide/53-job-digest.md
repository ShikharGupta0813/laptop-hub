# 53 · Job digest

> 🟡 Not tested on real devices yet. Built and tested with fake data only.

**In simple words:** every morning the app picks the best new jobs it found, the ones that did not get a "skip" label. It puts the best match first and shows a tag when you know someone at that company. Once a week it tells you how your search is going and gives you one thing to try. It also writes a follow-up or thank-you draft at the right time and sets a reminder. You copy the draft and send it yourself. The app never sends or applies anything.

Open **Apps > Job digest**. There are three tabs.

## 1. Top 10
- Built once a day, after 08:00 (change it with `jobdigest_hour`). Only jobs found since the last digest are considered, and a job is never shown twice.
- Each card shows the match score, the Go or Maybe label with its one-line reason, and "You know someone" when a contact works at the company.
- **Add to apply queue** is the same step as in Opportunities. It does not send anything. **Open posting** opens the job page in your browser; the app itself never opens LinkedIn, Naukri or other no-bot sites.
- You get one phone notification a day, and only when there is something new. Turn it off with `jobdigest_notify`.

## 2. Week
The last seven days: sent, replies, interviews, offers. It also shows which resume and which kind of opening did best, and one change to try. Small numbers are only a hint, so the screen says how many applications each result is based on. **Copy review** copies the text. Friday can read it aloud: ask "how was my week".

## 3. Drafts
- **No reply:** seven days after you applied, a follow-up draft and a reminder. If the Job tracker already reminds you near that day (it does at day 5 and 10), you do not get a second one.
- **After an interview:** a thank-you draft a couple of hours later, and a status check seven days after. Tap **Interview time** if the interview was at a different time than when you marked it.
- **Done** removes the reminder. Nothing is sent from this screen.

> ⚠️ **Careful:** The drafts say nothing about your skills. The follow-up uses the same draft as the Job tracker, built from your confirmed facts. Read every draft before you send it.

Friday can read what is new: ask "what's new in jobs". The tool `job_digest` is read-only and cannot queue anything.
