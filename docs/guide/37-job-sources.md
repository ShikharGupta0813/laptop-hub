# 37 · Job and gig sources: where REDLINE looks, and how to add more

**What this page is for:** tell REDLINE which company job boards, remote job sites and freelance alerts to watch. It finds, tidies and ranks them; you only tap.

## Where to find it
Settings > Assistants and rules > **Job and gig sources**. The list starts **empty**. Nothing is on until you add it.

## Three quick ways to add
1. **From the catalogue:** public company job systems (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Teamtailor: give the company's board name), remote boards (RemoteOK, We Work Remotely, Remotive, Himalayas, Jobicy, Arbeitnow, Hacker News "Who is hiring"), and freelance platforms by email alert. Each shows **verified <date>** or **unverified**.
2. **Watch a company:** paste its careers page address. REDLINE works out which job system it uses and checks it every few minutes. A brand-new role is marked **Be early** and you get a notification. What was already open when you started is not marked.
3. **My own source:** an RSS feed, a JSON address with a field mapping, a public page with CSS selectors, alert emails from your mail, or a webhook. Tap **Test now** to see what it would find before you rely on it.

## What you see on every item
Title, company, link, how to apply (email, the company's form, or by hand), remote / hybrid / on-site with any region limit, full-time / part-time / contract / freelance / task-based / internship, pay with its unit and currency, and when it was posted. The same job on three boards shows once ("Also on ...").
Old listings expire. A role that keeps being reposted or stays open for months is marked as a possible ghost. Scam-looking posts are held back with the reason.

## Freelance and AI-task platforms
> ⚠️ **Careful:** Upwork, Fiverr, Freelancer, Toptal, Contra and AI-training marketplaces do not allow bots. REDLINE never logs in or scrapes them and **auto-submit is off**. Use their email alerts (REDLINE reads those mails on your laptop only), or paste a listing link. REDLINE prepares a proposal slot and tracks your bid (drafting, submitted, won...). You open the link and submit by hand. LinkedIn and Indeed are the same: prepare and open link only.

## Timing and clean-up
Under **Timing**: switch background fetching on, how often normal sources and watched companies are checked, when old listings expire, and when to suspect a ghost. A source that fails is retried with growing pauses; one that blocks automated reading is marked **Needs a manual check** and left alone.

## Privacy
Alert emails are parsed on the laptop by plain code. Nothing from your profile or your mail is sent to a website or to an AI by this feature.

Details: `docs/api/oppsources.md`. Back to the [guide index](README.md).
