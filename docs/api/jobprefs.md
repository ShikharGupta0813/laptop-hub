# Jobprefs API (search profiles and the filters they apply)

`hub/jobprefs.py` (profiles, detection, filters, routes), hooked into `hub/opps.py` (the finder) and `hub/batches.py` (batches). Screen `js/jobprefs.js` + `css/jobprefs.css` (Apps > Job preferences).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Data: `jobprefs.json` in the hub data folder (no secrets).

## Rules
- With **no enabled profile nothing is filtered** (the finder and batches behave as before).
- A posting must pass **at least one** enabled profile. Within a profile, ANY failing HARD rule rejects it. Rejected finds get status `filtered` (reason in `filtered_reason`), are never queued, never batched, and are hidden from `GET /api/opps/finds` unless `?status=filtered`.
- Pasted links (`source_id` = `manual`) are never filtered (you chose them); the match is still shown as warnings.
- Pure code reads the posting. No model sees it.

## Profile
`{id, name, enabled, remote_only, work_modes[remote|hybrid|onsite], kinds[fulltime|parttime|contract|freelance|task|internship], locations[], timezones[], min_pay{amount, currency (3 letters or ""), unit hour|day|week|month|year|project|task}|null, include[], include_required, exclude[], avoid_companies[], seniority[intern|junior|mid|senior|lead|principal], languages[], sources[opps source ids; empty = all], daily_quota (0 = none), schedule{daily|weekdays|weekly, time}|null}`. Up to 12 profiles.

HARD rules: work mode (`remote_only` = must clearly say remote; an unclear posting is rejected for it), remote but limited to a region you are not in (when `owner.regions` is set and `honour_region`), on-site/hybrid place (when the posting states one), employment kind (only when the posting states one), minimum pay (only when stated in the same currency and a convertible unit: hour/day/week/month/year; project and task compare only to the same unit), excluded keywords (whole words), avoided companies, seniority (from the title), languages the posting requires that you do not list, the profile's source list.
SOFT scoring (-25..+25 added to the finder score): included keywords, preferred mode, kind match, pay meets minimum, seniority match; unknown pay/kind/region take a little off and appear as warnings on the card.

## Remote detection
Reads the `Location:` line, title and description. Remote: "remote", "fully remote", "work from home/anywhere", WFH, telecommute, home-based, distributed team, virtual role. Negations win ("not remote", "no remote work", "remote is not available", "on-site only", "must work from the office"). "Remote sensing/control/desktop/access..." is ignored. "Hybrid", "N days in office", "remote-friendly/optional" = hybrid. A source flagged `remote: true` (remote-only job boards in the starter list; editable per source) makes an unclear posting remote. A restriction is read from "Remote (US)", "US only", "EU residents", "must be located in the UK", "authorized to work in the EU", "open to candidates in ...", "work remotely from ..." and the location line; "worldwide", "anywhere", "work from anywhere" lift it. Ignored on purpose: a negation ("not limited to the US") and the company's own address ("we are based in Germany", "a US-based company"). Short codes (US, UK, EU, USA) match only in capitals, so "join us only" is not a region. Regions form groups: EU is inside Europe inside EMEA; the UK is Europe, not the EU. A restriction is satisfied when the posting's region equals your region or contains it (a "Europe" job is open to an owner in Germany, an "EU" job is not open to an owner in the UK). Add your own place names with `PUT /aliases` (`{"Kerala": "IN"}`).

## Routes
- `GET /api/jobprefs` -> `{profiles, owner{regions[], timezones[], honour_region}, aliases{}, options{modes, kinds, units, levels, regions}, templates[], has_profiles}`.
- `POST /api/jobprefs/profiles` (profile body) -> profile. `PUT /api/jobprefs/profiles/{id}` partial update. `DELETE`. `POST /api/jobprefs/profiles/from-template/{key}` (`remote-only`, `gigs`).
- `PUT /api/jobprefs/owner {regions?, timezones?, honour_region?}`; `PUT /api/jobprefs/aliases {aliases: {name: code}}`.
- `POST /api/jobprefs/check {title, company, description, location?, kind?, source_id?}` -> `{analysis{mode, geo{restricted, regions, worldwide, timezones, text}, kinds, pay, level, languages}, profiles[{ok, fails, warns, why, delta, profile, profile_name}]}`. Stores nothing.

## Finder hook
`opps.ingest` calls `jobprefs.assess` after scoring: adds the soft points, stores `find.match`, sets `filtered`. `applyq.add_item` receives `meta: {source_id, match}` (stored as `item.meta`) so batches can show mode, pay and warnings. Counts gain `filtered`. A source may carry `remote: true`.

## Not verified
Real postings (the detectors are tested on written examples only), non-English postings, the screen in a real browser at 390px.
