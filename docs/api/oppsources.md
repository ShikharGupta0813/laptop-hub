# Job and gig source connectors API

`hub/oppsources.py` (adapters, presets, engine, routes, tools), `hub/oppsnorm.py` (pure parsing: remote, pay, employment, posted time, duplicate keys, CSS selectors),
screen `js/oppsources.js` (Settings > Job and gig sources). Data (hub data folder): `oppsources.json` (sources), `oppsources_state.json` (last fetch, ETags, webhook secret hashes, seen mail ids),
`oppsources_items.json` (normalised items). All routes need the login cookie except the webhook. Errors are `{"detail": "one plain sentence"}`.

New, non-duplicate, not-stale items go to `opps.Opps.ingest` (scam check `opps_vet`, scoring, apply queue). This module never applies, signs in, sends or contacts anyone.

## Config (Settings or `hub.json`)
| Key | Default | Meaning |
|---|---|---|
| `oppsources_enabled` | `false` | background fetching (Test and Run now always work) |
| `oppsources_interval_min` | `60` | normal sources, 5-1440 |
| `oppsources_watch_interval_min` | `10` | watch-list companies (early-bird), 2-120 |
| `oppsources_stale_days` | `45` | listings older than this expire, 7-365 |
| `oppsources_ghost_days` | `60` | still open after this long = possible ghost, 14-730 |

A source can override its own interval with `interval_min` (0 = default). Failures back off exponentially (interval x 2^n, cap 24 h); a block or login wall waits 24 h ("needs manual check").

## Adapters
`rss` (RSS 2.0/1.0/Atom; `split_company` for "Company: Role" titles), `json` (`list_path`, `fields` mapping, `url_template`), `html` (`item_selector` + `fields` as `css`, `css@attr`, `@attr`;
robots.txt respected; selectors are compiled when saved: tags, `.class`, `#id`, `[attr]`, `[attr=v]`, `^= $= *= ~=`, space, `>`, comma), `email` (alert mails from the owner's mail accounts via gmailx; `senders`,
`subject_contains`, `account`, `hours`; read with destination `local` and parsed by plain code, so per-account `cloud_ai_allowed` is never needed and bodies never reach a model),
`webhook`, ATS (`greenhouse lever ashby workable smartrecruiters recruitee teamtailor personio breezy`, setting `board`, Lever `region: eu`), `workday` (`tenant`, `site`, `host` wdN, optional `search`),
search APIs that need the owner's own free key (`adzuna`: `country`, `what`, optional `where`, `pages`; `jooble`: `keywords`, optional `location`; `findwork`: `search`, optional `location`), `hn` (Hacker News "Who is hiring" via the public Algolia API),
`link` (open-link mode: nothing fetched, the owner pastes listing links).
JSON field names for `fields`: `title company url description location posted employment remote pay_min pay_max pay_currency pay_unit pay_text id tags`; a value starting with `=` is a literal.

## Routes
- `GET /api/oppsources` status: config, counts, `early`, every source with `status` {state ok|unchanged|manual_check|error, at, found, new, items, error, fails, next_at}.
- `GET /api/oppsources/presets` the catalogue: `{id,label,group(ats|search|remote|freelance|task),adapter,kind,needs,optional,needs_key,key_set,disabled_reason,verified_on,verification,note,docs,modes,auto_submit:false,never_automated,added}` plus `keys`. Starts with nothing added.
- `GET /api/oppsources/keys` -> `{keys: {adzuna|jooble|findwork: {label, set, signup, fields[{name,label,set}]}}}`: whether each key exists, NEVER its value. `PUT /api/oppsources/keys/{provider} {values: {...}}` saves it in the OS keyring
  (service `redline-providers`, name `oppsources:<provider>:<field>`; the field shape is checked, 400 otherwise); `DELETE` removes it and switches that provider's sources off. A key is never in `hub.json`, a response, the audit log, an error or a stored item (errors are scrubbed).
- `POST /api/oppsources/presets/{id}/add {board?, region?, mode?, label?, enabled?}` one-tap add (mode `email-alert` or `open-link` for platforms).
- `POST /api/oppsources/watch {url, label?}` company watch list: detects the ATS from the address, else from the page's own markup (robots.txt respected), subscribes with early-bird polling.
- `GET/POST /api/oppsources/sources`, `PUT/DELETE /api/oppsources/sources/{id}`, `POST .../{id}/test` (fetch + parse, stores nothing, does not touch the ETag or mark mail as seen),
  `POST .../{id}/run`, `POST /api/oppsources/run` (due sources), `POST .../{id}/webhook` (new secret, shown once).
- `POST /hooks/source/{id}` the webhook: header `X-Webhook-Secret` (or Bearer), JSON item or `{items:[...]}` with title, company, url, description, location, pay, employment, posted, apply_email. 10 wrong tries a minute lock it.
- `GET /api/oppsources/items?kind=&mode=&status=&q=&early=&limit=`; `GET .../items/{id}`; `POST .../items/{id}/dismiss`.
- Freelance: `GET .../items/{id}/proposal-context`, `PUT .../items/{id}/proposal {state: none|draft|ready|sent, text?}`, `PUT .../items/{id}/bid {status: none|drafting|submitted|shortlisted|won|lost|withdrawn, amount?, currency?, note?}`,
  `POST /api/oppsources/links {url, title?, company?, budget?, note?, kind?}` (open-link mode; nothing is fetched).
- Python hook for the proposal writer: `oppsources.proposal_context(item_or_id, ctx=None)` returns `{ok, title, client, task_summary, budget{min,max,unit,currency,label,type fixed|hourly|periodic|unspecified}, url, platform, kind, employment, work_mode, apply_method, auto_submit:false, rules}`.

## Item schema
`id, title, company, url, apply_method (email|ats_form|external_manual|platform_manual), apply_email, work_mode (remote|hybrid|onsite|unknown), geo {worldwide, regions, text}, employment (full-time|part-time|contract|freelance|task-based|internship|unspecified),
kind (job|freelance|llm-task), pay {min,max,unit hour|day|week|month|year|project,currency}, pay_label, posted_ts/posted_at, source, source_id, also_on, description, status (new|blocked|expired|closed|dismissed),
flags/severity (opps_vet), signals [{code repost|reopened|long_open|vague|noreply|ghost|multi|early,label}] (see docs/api/triage.md), early, reposts, bid, proposal (freelance/task only), auto_submit (always false), apply_note`.

## Rules
- Dedupe: same company + noise-stripped title across boards (or same normalised URL) is ONE item; `also_on` lists the other boards. Same source with a new posting id for the same job = repost; two reposts, or a posting still open past `oppsources_ghost_days`, = possible ghost (signal only, never hidden).
- Stale: items older than `oppsources_stale_days` expire (posted time, else first seen). ATS lists are complete, so a role that disappears is marked `closed`.
- Early-bird: the first successful run of a watched source is a baseline (what was already open is not "early"). After that a new role from a watched company is flagged `early`, notified through proactive/notify as "Be early", and always reaches the apply queue (the owner still taps).
- Platforms (Upwork, Fiverr, Freelancer, Toptal, Contra, PeoplePerHour, data-annotation and evaluation marketplaces, LinkedIn, Indeed...) are never fetched and never automated: `apply_method platform_manual`, `auto_submit false`.
- Fetching: research SSRF guard, polite UA, 2 s gap per host, ETag / If-Modified-Since, unchanged body hash skipped, 2 MB cap.
- Assistant tools: `sources_list`, `sources_add` (needs approval; preset, or `watch` with a careers url, or an adapter), `sources_test` (read-only; listing text is data).

## Verification of presets
Checked against the live public endpoints on 2026-11-15: read-only, one request per source (a few extra single requests only to save small samples), a normal polite User-Agent, no logins, no cookies, robots.txt read for the
feed hosts that were added (no Disallow for the feed paths). Only presets that answered with the expected shape say `verified 2026-11-15`; the rest say `unverified` in `verification` and in their `note`. A 401/403/429, a login
redirect or a robots refusal is never worked around: the source goes to "needs manual check". Freelance and AI-task platforms are never fetched, so they stay `unverified` by design.

| Source | Date | Result |
|---|---|---|
| Greenhouse (`boards-api.greenhouse.io`, public company board) | 2026-11-15 | verified, shape matches (`jobs[].absolute_url, title, content, first_published, location.name, id`) |
| Lever (`api.lever.co`) | 2026-11-15 | verified (answers 200 with a postings list; fields as in the Lever docs) |
| Ashby (`api.ashbyhq.com/posting-api`) | 2026-11-15 | verified, shape matches (`jobs[]` with `isListed, jobUrl, descriptionPlain, compensation`) |
| Workable (`apply.workable.com/api/v1/widget`) | 2026-11-15 | **unverified**: HTTP 200 with the expected top level (`name`, `jobs`) but an empty job list for every account tried (some accounts 404), so the job fields were not checked; the widget API may be retired |
| SmartRecruiters (`api.smartrecruiters.com/v1/companies/<c>/postings`) | 2026-11-15 | verified; the list has no description text (only the per-posting call has it), so descriptions stay empty |
| Recruitee (`<company>.recruitee.com/api/offers/`) | 2026-11-15 | verified, shape matches (`offers[]` with `careers_url, remote, salary, company_name`); unknown companies answer 404 |
| Teamtailor (`<company>.teamtailor.com/jobs.rss`) | 2026-11-15 | verified; **fixed**: locations are nested (`tt:locations/tt:location/tt:city,tt:country`) and were dropped before |
| RemoteOK (`remoteok.com/api`) | 2026-11-15 | verified; first array element is a legal notice (skipped); `epoch`, `position`, `apply_url` as mapped |
| Remotive (`remotive.com/api/remote-jobs`) | 2026-11-15 | verified with ONE call as they ask (shape matches). Their response says the main domain moved: keep the interval long |
| Himalayas (`himalayas.app/jobs/api`) | 2026-11-15 | verified; **fixed**: pay period now read from `salaryPeriod` (was hard-coded to year); `pubDate` is an epoch number (handled) |
| Hacker News "Who is hiring" (Algolia) | 2026-11-15 | verified; **fixed**: the comment search also returns replies (about half the hits); only top-level comments are postings now |
| Jobicy (`jobicy.com/api/v2/remote-jobs`) | 2026-11-15 | verified, shape matches (`jobs[]`: `jobTitle, companyName, jobGeo, salaryPeriod ...`) |
| Arbeitnow (`arbeitnow.com/api/job-board-api`) | 2026-11-15 | verified, shape matches (`data[]`, `created_at` is an epoch number) |
| We Work Remotely (RSS, all jobs; and programming category) | 2026-11-15 | verified; titles are "Company: Role" (split) |
| Working Nomads (`workingnomads.com/api/exposed_jobs/`) | 2026-11-15 | **new**, verified (top-level list, `tags` is a comma string) |
| Jobspresso (RSS) | 2026-11-15 | **new**, verified |
| NoDesk (RSS) | 2026-11-15 | **new**, verified; **fixed**: the feed puts an HTML entity (`&copy;`) in plain XML, which a strict parser refused. Named HTML entities are now turned into characters on a second try (entity declarations are still refused); titles are "Role at Company" (split) |
| Python.org Jobs (RSS) | 2026-11-15 | **new**, verified |
| Personio (`<company>.jobs.personio.de/xml`) | 2026-11-21 | **new**, verified with ONE request to a public board: `<workzag-jobs><position>` with `id, office, department, name, jobDescriptions, employmentType, seniority, schedule, createdAt`; the job address is `https://<company>.jobs.personio.de/job/<id>`; descriptions were empty on that board, nested `<jobDescription><name/><value/>` blocks are read when present |
| Breezy HR (`<company>.breezy.hr/json`) | 2026-11-21 | **new**, verified with ONE request to a public board: a JSON list with `id, name, url, published_date, type{name}, location{name,is_remote,remote_details}, department, salary (text), company{name}`; no description in the list |
| Workday (`<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs`, POST) | 2026-11-21 | **unverified**: one request to the robots.txt and one POST to one big public board both met a redirect to Workday's maintenance page ("Workday is currently unavailable"), so nothing was read. The code follows Workday's published answer shape (`total`, `jobPostings[]` with `title, externalPath, locationsText, postedOn, bulletFields`) and is tested on invented fixtures. It sends `{appliedFacets, limit 20, offset, searchText}`, at most 3 pages, robots.txt checked, 2 s apart. Not retried (one request only). Use Test on a real tenant before relying on it |
| Adzuna (`api.adzuna.com/v1/api/jobs/<country>/search/<page>`) | 2026-11-21 | **unverified**: needs the owner's free app id and key (none used here, nothing called). Fields per developer.adzuna.com: `results[]` with `id, title, description, created, redirect_url, salary_min, salary_max, salary_is_predicted, contract_type, contract_time, location.display_name, company.display_name`; salaries are yearly in the country's currency, and a predicted salary is ignored |
| Jooble (`POST jooble.org/api/<key>`) | 2026-11-21 | **unverified**: needs the owner's free key (nothing called). Fields per jooble.org/api/about: `jobs[]` with `title, location, snippet, salary, source, type, link, company, updated, id` |
| Findwork (`findwork.dev/api/jobs/`, header `Authorization: Token <key>`) | 2026-11-21 | **unverified**: needs the owner's free token (nothing called). Fields per findwork.dev/developers: `results[]` with `id, role, company_name, employment_type, location, remote, url, text, date_posted, keywords` |
| Upwork, Fiverr, Freelancer, Toptal, Contra, PeoplePerHour, DataAnnotation, Outlier, Alignerr, Mercor | n/a | never fetched (email alert or open link only), `unverified` by design |

Not added on purpose: LinkedIn, Indeed, Naukri, Instahyre, Wellfound, Glassdoor (they forbid bots). Remote.co was too slow to answer (timeout) and was skipped. Every preset `note` states the terms to respect (link back, call frequency).
Use Test on a source to see what the live service answers today.

## Freelance and task platforms in one tap

`POST /api/oppsources/presets/add-group` with `{"group": "freelance"}` or `{"group": "task"}` adds every ready-made platform of that group that is not on your list yet (it answers `{added, already_there}`). The platforms are email-alert sources: nothing is fetched from them, nothing is automated, and you submit by hand. The source catalogue screen has an "Add all in this group" button. Platforms include Upwork, Fiverr, Freelancer, Toptal, Contra, PeoplePerHour, Guru, Braintrust, Codeable, Gun.io, Lemon.io, Arc, Truelancer, Internshala and 99designs (freelance), and DataAnnotation, Outlier, Alignerr, Mercor, Prolific, Remotasks, Appen, TELUS Digital, Turing, Toloka, Clickworker, Mechanical Turk, Microworkers, Respondent and UserTesting (tasks). They are never fetched, so they are not "verified"; LinkedIn, Indeed, Naukri, Instahyre and Wellfound stay manual.

## Search sites with your own free key (Adzuna, Jooble, Findwork)
These three are search APIs. You register for a free key yourself (Adzuna: developer.adzuna.com, Jooble: jooble.org/api/about, Findwork: findwork.dev/developers). In the source catalogue,
pick the site, paste the key into the password box (it is saved in this laptop's OS keyring and never shown again) and fill in the search words. **Until a key exists the preset stays off** and says
"needs your free <name> key": adding it creates a switched-off source, switching it on is refused (409), and nothing is fetched. Removing the key switches its sources off again.
The key is placed in the request and nowhere else: Jooble puts it in the address, so errors and test results have every stored key value replaced by `***`; Findwork's token header is sent only to its own host
(never after a redirect to another host). Adzuna's own salary guesses are ignored.

## Workday, Personio and Breezy
Workday needs three things from the careers address `https://<tenant>.<wdN>.myworkdayjobs.com/<site>`: the tenant, the site and the data centre (`wd1`, `wd3`, `wd5`...). Personio and Breezy need only the company slug
(`<slug>.jobs.personio.de`, `<slug>.breezy.hr`). Watching a company by its careers address detects all three. All use the shared guard (public hosts only), the 2 s per-host gap and the polite user agent. Items have
`apply_method ats_form` and nothing is ever submitted for you.
