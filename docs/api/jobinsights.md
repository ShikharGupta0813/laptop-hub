# Job insights API (the learning loop, deeper)

`hub/jobinsights.py`. Screen: the **Insights** tab of the job tracker (`js/jobinsights.js`). Needs the login cookie. Data: the tracker's own `jobtrack.json` (read only) and this module's `jobinsights.json` (the weekly experiments). Switched off with `jobinsights_enabled: false` (404).

## Rules
- Plain code on the laptop. **No model reads anything**, nothing is sent, nothing is changed except starting or skipping an experiment.
- One call when the tab opens; no timer.
- Small numbers: every text says it is a hint. Reply rates are only shown with their counts.
- **Skill gaps are a report only.** Nothing is ever added to your facts or your resume; add a skill only if it is true, in Facts.

## What the records now carry
New optional fields on tracker records (all local): `applied_hour` (0 to 23, from the queue's sent time), `score` (the match score the posting had), `triaged` (the finder's triage said apply, when it exists), `posting` (first 2000 characters of the posting, for the skill report). Older records lack them; the report then simply has fewer rows.

## Routes
- `GET /api/jobinsights` -> `{generated, applications, funnel, rejections, score_check, skill_gaps, experiment, note}`.
  - `funnel`: `{total, by_source, by_role_family, by_resume, by_hour, by_weekday}`. A row is `{key, applied, replies, interviews, offers, reply_rate}`; source rows and the total also have `found` and `triaged_apply` (counts from the opportunity finder, `null` when the finder has no triage). Replied means the history ever had replied, interview, offer or rejected. Hour buckets: before 11:00, 11:00 to 14:00, 14:00 to 18:00, after 18:00 (laptop time). Role families come from words in the role (data, backend, frontend, devops and cloud, mobile, fullstack, qa, product and design, support, other).
  - `rejections`: `{n, text, stages[{stage, n}], days{median, n}, traits[{trait, rejected_share, all_share}]}`. Stage is what the application had reached (straight after applying, after a first reply, after an interview). A trait is listed when it is at least 15 points more common among rejections than among all applications.
  - `score_check`: `{verdict (yes | no | unclear | not_enough), n, bands[{key low|middle|high, score_from, score_to, applied, replies, reply_rate}], text}`. Needs 9 scored applications; bands are thirds of the scored ones. Scores of 0 (unscored) are ignored.
  - `skill_gaps`: `{postings, gaps[{skill, count}], have[{skill, count}], note}`. Skills are counted over your tracked applications, the application queue and saved finder items (deduplicated). "gaps" are skills not found in your fact base.
  - `experiment`: `{current, last}`. `current` is `{id, week (ISO), state (suggested | active | skipped | done), kind (resume | hour | score), target, text, why, started, result?}`; `last` is last week's started experiment with its `result`.
- `POST /api/jobinsights/experiment/{id}/start` -> experiment (state active, starts today). `POST .../skip`. 400 when it is no longer suggested, 404 for an unknown id.

## The weekly experiment
One suggestion per ISO week, chosen by plain rules from your data, first that fits: try the resume version with the best reply rate; apply before 11:00; only apply to the best-scored third. Recent ones are not repeated. After you start it, applications that match it are compared with those from before it started. `result.verdict` is `too_early` (under 5 matching applications), `better`, `worse` or `no_change` (a 10 point difference counts). Next week the previous experiment shows with its result.

## Settings
`jobinsights_enabled` (default true).
