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
`webhook`, ATS (`greenhouse lever ashby workable smartrecruiters recruitee teamtailor`, setting `board`, Lever `region: eu`), `hn` (Hacker News "Who is hiring" via the public Algolia API),
`link` (open-link mode: nothing fetched, the owner pastes listing links).
JSON field names for `fields`: `title company url description location posted employment remote pay_min pay_max pay_currency pay_unit pay_text id tags`; a value starting with `=` is a literal.

## Routes
- `GET /api/oppsources` status: config, counts, `early`, every source with `status` {state ok|unchanged|manual_check|error, at, found, new, items, error, fails, next_at}.
- `GET /api/oppsources/presets` the catalogue: `{id,label,group(ats|remote|freelance|task),adapter,kind,needs,verified_on,verification,note,docs,modes,auto_submit:false,never_automated,added}`. Starts with nothing added.
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
flags/severity (opps_vet), signals [{code repost|ghost|multi|early,label}], early, reposts, bid, proposal (freelance/task only), auto_submit (always false), apply_note`.

## Rules
- Dedupe: same company + noise-stripped title across boards (or same normalised URL) is ONE item; `also_on` lists the other boards. Same source with a new posting id for the same job = repost; two reposts, or a posting still open past `oppsources_ghost_days`, = possible ghost (signal only, never hidden).
- Stale: items older than `oppsources_stale_days` expire (posted time, else first seen). ATS lists are complete, so a role that disappears is marked `closed`.
- Early-bird: the first successful run of a watched source is a baseline (what was already open is not "early"). After that a new role from a watched company is flagged `early`, notified through proactive/notify as "Be early", and always reaches the apply queue (the owner still taps).
- Platforms (Upwork, Fiverr, Freelancer, Toptal, Contra, PeoplePerHour, data-annotation and evaluation marketplaces, LinkedIn, Indeed...) are never fetched and never automated: `apply_method platform_manual`, `auto_submit false`.
- Fetching: research SSRF guard, polite UA, 2 s gap per host, ETag / If-Modified-Since, unchanged body hash skipped, 2 MB cap.
- Assistant tools: `sources_list`, `sources_add` (needs approval; preset, or `watch` with a careers url, or an adapter), `sources_test` (read-only; listing text is data).

## Verification of presets
Only Lever is marked `verified 2026-11-14` (endpoint and field names read from github.com/lever/postings-api). The build sandbox could reach only github.com, so Greenhouse, Ashby, Workable, SmartRecruiters, Recruitee, Teamtailor and every remote board are
`unverified`: use Test on a source to see what the live service answers.
