# Company brief (hub/companyintel.py)

A one-page brief about a company from PUBLIC sources only. All routes need the login cookie. Nothing is sent anywhere; no fact about the owner is used. Switches: `companyintel_enabled` (default true), `companyintel_ai` (default false). Storage: `companyintel.json` in the data folder (public text only, newest 60 briefs).

## Sources, in order
1. The company's own website, if the owner gives it (or the posting's own address when it is not a job board or applicant system): the home page, then the about, careers and blog or press pages the home page links to (same site only), and an RSS or Atom feed the home page announces.
2. News: only the company's own press or blog RSS/Atom feed that its pages announce (`<link rel="alternate" type="application/rss+xml">` on the home or blog page), at most 2 feeds. Google News RSS search is NOT used: its robots.txt asks automated readers to stay away (checked live with one request on 2026-11-21).
3. Postings the hub already holds (`oppsources_items.json`) and the owner's application records (`jobtrack.json`), read locally.

Reading is polite: through `oppsources.Fetch` (the SSRF guard on every redirect hop, robots.txt, a 2 second gap between requests to one host, no logins, a 2 MB cap), at most 6 requests per brief (the cap is noted in `notes`). Private and local addresses are blocked by the guard.

**Never read:** the bot-forbidden list the job sources use (`NEVER_AUTOMATE`: LinkedIn, Indeed, Glassdoor, Naukri, Instahyre, Wellfound and the freelance platforms) plus Levels.fyi, AmbitionBox, Blind, Crunchbase, Comparably and the main social sites (`FORBIDDEN` in the module). Typing one as the website is a 400; a link or feed to one found on a page is skipped and listed under `refused`.

## Routes (`/api/companyintel`)
| Route | |
|---|---|
| `GET /` | the screen's ONE data call: `{briefs[{company, site, days_left}], applications[], settings{enabled, ai}, cache_days: 7, forbidden[], sends: false}` |
| `POST /brief` `{company \| application_id, site?, ai?, refresh?, provider?}` | the brief (below). A saved brief younger than 7 days is returned with `cached: true` and nothing is fetched; `refresh: true` reads again. 400 for no company or a forbidden site; 404 for an unknown application |
| `DELETE /cache?company=` | forget one company's saved brief (or all when empty). `{ok, deleted}` |

Brief: `{company, site, generated_at, cached, expires_in_days, summary, summary_by, ai_note, about, pages[{kind, url, title, snippet}], hints{size[], funding[], founded, place}, news[{title, url, date, source}], postings{count, rows[], oldest_days, old, repeated[]}, tracker[{role, status, applied_at}], red_flags[{id, severity, text, url?}], sources[{url, ok, note}], refused[{url, why}], notes[], disclaimer, sends: false}`.

## Plain code first
- **Summary:** the first real paragraph of the about page, then what the text says (founded, place, size, funding), the number of postings held, the latest headline.
- **Hints:** quoted with the words around them from patterns such as "founded in 2012", "team of 150", "Series B", "raised $20 million", "backed by". Nothing is guessed; a hint is only what the page says.
- **News:** the last 365 days, newest first, from the feeds read.
- **Red flags:** a headline with layoffs, job cuts, restructuring, shutdown, insolvency, unpaid salaries, fraud (news); the same title listed 3 or more times (reposts); postings 60 or more days old (info). When nothing is found, one info line says a missing flag does not mean the company is safe.

## Optional AI
`ai: true` (or `companyintel_ai`) asks the owner's model through `ctx.model_for` (redaction applies) for a summary of at most 90 words, from the public text that was read (up to 5000 characters) and nothing else. No model, an error or an empty answer: the plain summary stays and `ai_note` says why.

## Tool for Friday
`company_brief {company | application_id, site?, refresh?}`: returns the summary, red flags, news and counts as text. Read-only, marked untrusted (page text is data); no approval.

## Also used by
The interview day pack (`POST /api/interviews/day-pack`) includes a saved brief without any network call, or reads one now when asked (`fetch_brief: true`). Warm paths ("who do I know here") are in `docs/api/networking.md`.
