# 45 · Company brief and "who do I know here"

> 🟡 Not tested on real devices yet. Built and tested with fake data only.

**In simple words:** before you apply or interview, tap one button and get a one-page note about the company, made from its own public website and public news. It also shows red flags and who in your own contacts works, or used to work, there. It never reads LinkedIn or Glassdoor, and it never sends anything.

Open **Apps > Company brief**.

## 1. Build a brief
Pick an application, or type the company name. Add the company's **own website** if you know it (for example `https://www.example.com`): the app then reads its home page and the about, careers and blog pages it links to. If you leave the website empty, only public news and the postings already in your hub are used. The app never guesses a website.

You get:
- a short **summary** written by plain code (the first paragraph of the about page, and what the pages say about founding year, place, size and funding),
- **red flags**: a news headline about layoffs, job cuts or shutdown; the same role posted again and again; postings that are 60 days old or more,
- **news** from the company's own press or blog feed, when its website announces one (Google News is not used: its rules ask robots to stay away),
- the postings and your own applications that the hub already holds for that company.

> ⚠️ **Careful:** This is built from public text. It can be wrong or old. A missing red flag does not mean the company is safe. Check what matters in the interview.

The brief is saved on this laptop for **7 days**, so tapping again costs nothing. **Read again** fetches fresh text. **Forget** deletes a saved brief.

## 2. Why it is polite and safe
- It reads at most a few pages per brief, with a gap between requests to the same site, and it follows `robots.txt`.
- It cannot be pointed at your own laptop or network: private addresses are blocked.
- LinkedIn, Indeed, Glassdoor, Naukri, Instahyre, Wellfound, Levels.fyi and similar sites are **never** read. If you type one, you get a plain message and nothing is requested.
- The optional **Summarise with AI** switch (setting `companyintel_ai`, off by default) sends only the public text that was read to your AI. Nothing about you is sent.

## 3. Who do I know here
After a brief, tap **Who do I know here?**. The app looks in your **Networking contacts** (company, "worked before at", how you know them) and your **Job tracker** and shows the best person to ask first, with the reasons. Tap **Draft a referral request** to get a short message built from your own facts. Copy it and send it yourself.

Tip: in **Networking > contacts**, fill **Worked before at** (for example "Initech, Hooli"). People who used to work at a company are often the best referral.

Friday can do the brief too: the tool `company_brief` reads it out. It is read-only.

Back to the [guide index](README.md).
