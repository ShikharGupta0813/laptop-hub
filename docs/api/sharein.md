# Share to REDLINE API

`hub/sharein.py` (route `POST /api/sharein`), screen `js/share.js` + `css/share.css` (Apps > Shared job, hash `#/share`), Android hand-over in `mobile/android/.../MainActivity.java` and `ShareIn.java`.
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Switched off with `sharein_enabled: false` (the route answers 404; the key is in the settings allow-list).

A job the owner sees in LinkedIn, Naukri or any app: Share, then REDLINE. The shared TEXT becomes an opportunity, gets the go / no-go label (`hub/triage.py`) and the ATS match (`hub/ats.py`), and the owner can tap **Tailor + packet** (`/api/packet/prepare`) or **Add to apply queue** (`/api/opps/finds/{id}/queue`). The same screen works in a desktop browser with a paste box.

## Rules
- **Nothing is fetched from LinkedIn, Naukri, Indeed, Glassdoor, Instahyre, Wellfound or any other host in `oppsources.NEVER_AUTOMATE`**, and nothing from a link shortener (`lnkd.in`, `bit.ly`, `t.co`... it may point at one of them). For those only the shared text is used. The link is kept so the owner can open it themselves.
- A **bare link** (fewer than 12 words once links are removed) to such a host is saved as a note (`meta.placeholder`), the answer has `needs_text: true` and says "paste the job text too". Pasting the text later replaces the note, so there is one find, not two.
- A bare link to any other host goes through the existing pasted-link path (`Opps.add_link`: public page only, SSRF-guarded, as Opportunities > Add a link does today). If the page cannot be read, the answer asks for the text.
- When text is shared, **no page is fetched at all**, even for an ordinary link: the text is enough.
- **Nothing is queued, sent or applied by the share.** The find is stored with `auto_queue=False` (a new keyword on `Opps.ingest` / `Opps.add_link`; the finder's own behaviour is unchanged). Queuing is the owner's tap.
- Plain code only: no model sees the text, nothing leaves the laptop. The text is data: it is cut at 20,000 characters (6,000 are stored with the find), never run, and the audit line holds the host and the size only.
- A skill the posting asks for that is not in the facts is shown as a gap, never added to anything (the ATS rule).

## `POST /api/sharein`
Body: `{text, url?, subject?, title?, company?, replace_id?}`. At least `text` or `url` is needed. `title` and `company` are the owner's corrections; with `replace_id` (a find this module made) the find is rebuilt with them.
- 400 nothing to read. 404 switched off. 429 more than 30 shares in an hour (in memory; `RATE_MAX`, `RATE_WINDOW`).

Returns `{find, job, triage, ats, existing, needs_text, message, problem, host, site, never_automate, actions, nothing_sent}`:
- `find`: `{id, title, company, url, status, score, reason, source ("Shared from phone"), flags[], blocked_reason, filtered_reason, note (true for a saved link without text), triage}`.
- `job`: `{title, company, url, description}` for the packet call (null for a note).
- `triage`: the compact label from `triage.compact` (`verdict apply|maybe|skip`, `reason`, `locks`, `ghost`, `gig`); null for a note.
- `ats`: `{score, label, resume, matched[], matched_total, listed_in_facts[], gap[], tips[], breakdown[]}`, or `{score: null, note}` when there are no facts yet, or null.
- `existing`: the same job was already saved (found by the link, which for LinkedIn is the job id, or by title + company). Nothing is added twice. A job the owner dismissed earlier comes back as new, because sharing it was on purpose.
- `actions`: `{packet, queue, queued}` which buttons make sense (a scam-looking find, status `blocked`, offers none).

How the text is read: LinkedIn style ("Check out this job at X: Title", "X is hiring a Title in City"), Naukri style ("Title - Company | Naukri.com", labelled "Role:" / "Company:" lines) and plain "Title at Company". A guess that is wrong can be fixed on the screen ("Fix title or company"). The job link is normalised so the same job shared twice matches (tracking parameters removed; LinkedIn `currentJobId` and `/jobs/view/<id>` become one link).

## Android
`AndroidManifest.xml` has an `ACTION_SEND` `text/plain` intent filter on the main activity (also added by `mobile/scripts/patch-manifest.js`). `MainActivity` reads `EXTRA_TEXT` and `EXTRA_SUBJECT` (cold start and `onNewIntent`), waits (at most about 12 seconds) until the hub page has built its screens, then runs one script: `window.__hubShare = {text, url, subject}` and `location.hash = '#/share'` (or the `hub-share` event when the screen is already open). The text goes in as an escaped string literal (`ShareIn.java`, tested on a JVM in `ShareInTest.java`). `share.js` takes it, calls `/api/sharein` once and draws the result. Files and images shared to the app are not accepted (text only).

Tests: `tests/test_sharein.py` (invented LinkedIn and Naukri style texts, bare links, dedupe, a normal host, rate limit, no fetch from blocked hosts), `ShareInTest.java`.
