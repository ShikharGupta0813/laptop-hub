# Triage API: go / no-go label, ghost and lock flags, gig score

`hub/triage.py` (rules, routes), called by `hub/opps.py` for every find and by `hub/oppsources.py` for the ghost signals. Screens: `js/triagebadges.js` + `css/triage.css`
(shared label, badges and filter chips), used by `js/opps.js` (Finds) and `js/applyq.js` (the queue). Job preferences for it live in Apps > Job preferences ("Where you live").
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Nothing is stored in a new file: the label is stored on the find (`opps_finds.json`) and, small, on the queue item's `meta.triage`.

## What it does
Every found posting gets a **verdict** `apply | maybe | skip` with ONE short plain reason, worked out by plain code from the posting text, the owner's job preferences
(`hub/jobprefs.py`) and the owner's own facts (`hub/factbase.py`: skills and dated roles). No model reads the posting. Examples of reasons:
"9 of 11 must-have skills, remote India ok", "needs 8+ years, you have 3", "Must be authorised to work in United States", "gig score 78: budget meets your minimum".

**Nothing is hidden and nothing is sent.** A skip is a label. The find stays in the normal list and under the **Skip** filter chip; with any chip on, finds that your job
profiles filtered out of the normal list are listed too, with that reason. Applying, queueing and status changes stay your taps.

## Verdict rules (first match wins)
1. Looks like a scam (`severity high` from `opps_vet`) -> **skip**.
2. A hard-lock flag that conflicts with your preferences -> **skip** with the lock's own words.
3. Freelance / task items: a red flag (unpaid test, off-platform payment, "easy money") -> **skip**; gig score below 35 -> **skip**; 65 or more -> **apply**; between -> **maybe**.
4. The posting asks for 3 or more years more experience than your dated roles add up to -> **skip** ("needs 8+ years, you have 3"). 1 or 2 more years -> at most **maybe**.
5. Your enabled job profiles rejected it (`jobprefs.assess` not ok; a link you pasted is exempt) -> **skip** ("your job preferences: ...").
6. You have no skills yet -> **maybe** ("add skills to your profile so fit can be judged").
7. Skills: must-have skills are the technical terms in the posting (a built-in list of common terms plus any skill you listed), minus those only under "nice to have / preferred / bonus".
   You have a skill when your profile, your skill facts or your facts (confirmed facts, role bullets) back it. Four or more must-haves and under 30% matched -> **skip**
   ("only 1 of 7 must-have skills"). Three or more and 60% or more matched, and no experience gap -> **apply**; else **maybe**.
8. Too little detail to count skills: **apply** only for a find score of 70 or more; otherwise **maybe** ("not enough detail in the posting to judge"). Unknown never causes a skip.
9. A **likely ghost** turns an apply into a maybe ("... but it may be a ghost listing").

The result on a find is `find.triage = {verdict, reason, locks[], ghost{likely, signals[]}, gig | null, skills{matched,total,missing[]}, years{need,have} | null, at, ai?}`.
Missing skills are only listed; nothing is added to your profile or facts.

## Ghost and repost signals
Computed by `triage.ghost_signals` (the finder `signals` list on source items uses the same function). Each has a plain label:
`repost` (same job, new posting id), `reopened` (same company and title seen again after it closed), `long_open` (listed for 45 days or more, age from the posted date, else first seen),
`vague` (two or more of: no salary, no team named, no location), `noreply` (you applied to this company two or more times and no application ever got an answer: read from the job tracker,
`hub/jobtrack.py`, when it exists; the newest must be older than `jobtrack_ghost_days`), plus `multi` and `early` as before. Points: 2 reposts = 2, 1 repost = 1, reopened = 1, open 45+ days = 1 (2 past
`oppsources_ghost_days`), vague = 1, noreply = 2. **2 points or more adds `ghost` ("Likely ghost listing: ...")**, shown as a "Likely ghost" badge.

## Hard-lock flags
`[{code visa|location|notice|clearance|language, label, conflict}]`. `conflict` true means it clashes with your preferences (a skip); false means worth knowing.
- **visa**: "must be authorised to work in X", "right to work in X", "no sponsorship", "unable to sponsor". Conflict when X is not inside the countries you may work in
  (`owner.work_auth`, else `owner.regions`), or when "no sponsorship" meets a place outside them, or you set that you need sponsorship (fact base preference `needs_sponsorship`).
- **location**: "remote but only for the US", "must be based in X", "on-site only", "no remote", "in-office only", "5 days a week in the office". Conflict when its place is outside your regions and you are not willing to relocate.
- **notice**: "immediate joiners" (read as 15 days or less), "notice period of 30 days max", "join within 2 weeks". Conflict when your `owner.notice_days` is longer.
- **clearance**: security clearance, TS/SCI, public trust, "able to obtain a clearance" ("no clearance required" is ignored). Always a conflict (a clearance is not something this app can confirm for you).
- **language**: "fluent German required" and similar. English is always counted as spoken. Conflict only when you listed your languages and the one asked for is missing.

## Gig score (freelance and AI-task items)
0-100: budget against your minimum rate (35: meets it; 5: below; neutral when it cannot be compared), client signals in the alert text (25: payment verified, hires, money spent, rating; neutral 8 when none are shown),
proposals count if shown (10), skill fit (30), minus 25 for each red flag. `gig = {score, reasons[], red_flags[], client{payment_verified, hires, spent, rating, proposals}}`.
Your minimum rate is the first enabled job profile with a minimum pay per hour / day / project / task, else the fact base `min_pay_hourly`.

## Job preferences added for this (`PUT /api/jobprefs/owner`)
`work_auth` (list of countries or regions you may work in without sponsorship), `notice_days` (whole days 0-365, or empty), `relocate` (true/false), `languages` (list; English is always counted).

## Config
| Key | Default | Meaning |
|---|---|---|
| `triage_ai` | `false` | allow the optional **Ask AI** second opinion on one find. Off: no text ever leaves the laptop for triage |

## Routes
- `GET /api/triage` -> `{verdicts, lock_codes, ai, rules{...}}`.
- `POST /api/triage/check {title?, company?, description, location?, kind?}` -> the triage object for pasted text. Stores nothing.
- `POST /api/triage/refine/{find_id}` -> `{id, ai{verdict, reason}}`. 409 when `triage_ai` is off; 503 without a model; 502 when the answer is unusable. The answer is stored as `find.triage.ai`
  and shown beside the plain verdict; it never replaces it. The model call goes through `ctx.model_for` (redaction applies); the posting is sent as data and the prompt says never to follow it.
- `GET /api/opps/finds?verdict=apply|maybe|skip` filters by label (any status except dismissed). The answer also has `triage_counts {apply, maybe, skip, none}` and `triage_ai`.
- The apply queue item carries `meta.triage = {verdict, reason, locks[], ghost, gig}`; its screen filters by it client-side.

## Not verified
Real postings (the patterns are tested on written examples only), non-English postings, an AI answer from a real model, the screens on a real phone.
The apply queue items you add by hand (not from the finder) have no label.
