# Opportunities API

`hub/opps.py` (sources, finder, ranking, routes, tools), `hub/opps_vet.py` (scam check), `hub/profile.py` (private profile),
screen `js/opps.js` + `css/opps.css`. Data (hub data folder): `opps_sources.json`, `opps_finds.json`, `profile.json` (0600, never in git).
All routes need the login cookie. Errors are `{"detail": "one plain sentence"}`.

## Config (Settings or `hub.json`)
| Key | Default | Meaning |
|---|---|---|
| `opps_enabled` | `false` | background finder (Run now always works) |
| `opps_interval_min` | `120` | minutes between runs (15-1440) |
| `opps_min_score` | `40` | finds at or above this go to the apply queue (0-100) |

## Sources
`GET /api/opps/sources`, `POST /api/opps/sources {name,url,kind,how,enabled?,note?}`, `PUT /api/opps/sources/{id}`, `DELETE /api/opps/sources/{id}`,
`POST /api/opps/sources/reset` (back to the starter list). `kind`: `llm-task|freelance|job|community`. `how`: `rss|api|html|email-alert|manual`.
Only `rss`/`api`/`html` sources with an address are fetched. The starter endpoints were not verified from the build sandbox; a source that fails shows its status.

## Finder
- Fetching: public http/https only (the research SSRF guard, every redirect re-checked), robots.txt honoured, polite user agent, 20 s timeout, 2 MB cap,
  exponential backoff after failures. 401/403/429, robots refusal or a login redirect mark the source "needs manual check" and it is left alone.
- XML: entity declarations are refused. Everything fetched is untrusted text, shown with textContent, never given to a model.
- `GET /api/opps/finds?status=&q=&limit=` ranked finds plus `counts`, `enabled`, `last_run`. `GET /api/opps/status`.
- `POST /api/opps/run {source_id?}`: run now. `POST /api/opps/add {url, note}`: a pasted link (page read if public, else made from the note and flagged).
- `POST /api/opps/finds/{id}/queue|dismiss`, `DELETE /api/opps/finds/{id}`. Blocked finds cannot be queued.
- `opps.ingest_email(ctx, subject, sender, body)`: for the mail feature; extracts links/titles from alert emails.
- Dedupe by normalised URL + title; kept 30 days.

## Scam check and ranking
`opps_vet.vet(...)` returns `{flags:[{code,label,severity,detail}], severity: none|low|medium|high}`. High is held back (status `blocked`, reason shown) and never
sent to the queue. Score 0-100: skill overlap (60), seniority (15), remote (15), pay (10), with a one-line `reason`. Good finds go to
`applyq.add_item(ctx, {title, company, url, source, description, score, reason, flags, kind})` (guarded import; works without it).

## Profile (`hub/profile.py`)
`get_profile(ctx) -> {name, headline, summary, skills, experience[{title,company,period,bullets}], education, links, standard_answers, resume_text, updated_at}`.
`GET/PUT /api/profile`; `POST /api/profile/resume {filename,data_b64}` or `{text}` (+ `overwrite`, `use_model`); `DELETE /api/profile/resume`.
PDF via optional `pypdf` (requirements-extra.txt), else a small reader that tells the owner to paste text. A new resume keeps fields edited by hand unless `overwrite`.

## Assistant tools
`find_opportunities(query, persona)`, `list_sources`, `show_profile` (read-only); `add_source`, `remove_source` (approval). Jarvis answers with fit and flags, Friday with a short summary.
