# Tailoring, fit check, answer bank (hub/factbase.py, fitcheck.py, tailor.py, answerbank.py)

All routes need the login cookie. Nothing here sends anything. Switches: `tailor_enabled`, `tailor_ai`, `answerbank_enabled`, `answerbank_min_confidence` (30 to 100, default 75), `answerbank_embeddings`. Storage (data folder, mode 0600 for the first): `factbase.json`, `tailor_variants.json`, `answerbank.json`.

## Fact base (`/api/tailor/...`)
A fact is `{id, type, text, source, ...}`. Types: `name headline summary skill role bullet metric education link answer confirmed derived pref`. Ids are stable (a hash of type and text). Skills carry `evidence` (bullet ids) and `years` (union of the roles whose bullets mention it); `calc-years` is the years covered by dated roles; `pref-rate`, `pref-availability`, `pref-location` come from preferences.

| Route | |
|---|---|
| `GET /facts` | `{facts, years_experience, hidden, aliases}` |
| `POST /facts` `{text, tags[]}` | add an owner-confirmed fact |
| `PUT /facts/{id}` `{text?, tags?}` | edit a confirmed fact |
| `DELETE /facts/{id}` | confirmed: deleted; profile-derived: hidden (never used) |
| `POST /facts/{id}/unhide` | |
| `GET /prefs`, `PUT /prefs` | tone, tone_notes, sign_off, tones{name:{greeting,closing}}, rate, availability, location, remote_ok, relocate, timezone_offset, tz_tolerance_hours, min_pay_yearly, min_pay_hourly, work_auth[], needs_sponsorship, max_bullets, plus `aliases` {short: long} |

## Fit and honesty check
- `POST /fitcheck {job}` -> `{score, label, counts, requirements[{text, must, kind, status: matched|partial|missing|info, note, evidence[fact ids]}], flags[{code, label, severity, detail, source}], unchecked[], risk_blocked, summary}`. Flags: `over_qualified`, `under_qualified`, `location_mismatch`, `region_restriction`, `timezone_mismatch`, `pay_below_minimum`, `pay_unstated`, `work_authorization`, `sponsorship_unknown`, and `scam_*` from `hub/opps_vet.py` (source `scam`). A check without its preference is listed in `unchecked`.
- `POST /check {job?, sentences:[{text, fact_ids, kind: claim|glue|plan|quote}]}` -> `{ok, unbacked[{index, text, reason}], verdicts}`. Rules: a claim needs fact ids that exist; numbers, tool/employer names and all-caps words must appear in the cited facts (near spellings count, owner aliases apply); at least 60% of the other meaningful words must be in the cited facts; glue and plan sentences may use only the posting's own words and may not sound like an experience claim; a quote must be in the posting; `[FILL: x]` blanks are not claims.

## Tailoring
- `POST /preview {application_id | job:{title, company, description, url, contact_email, kind, channel}, channel?, ai?, provider?}` -> a saved **variant** `{id, application_id, job, channel, fit, requirements, resume, cover, risk_ack, blocked, blockers[]}`. Channels: `email form freelance outreach message` (inferred when omitted). `ai` defaults to `tailor_ai`; the model comes from `ctx.model_for(provider or "auto")` (redaction applies). The AI gets only the posting and up to 45 relevant fact sentences (never the raw resume or contact details); a rejected draft gets one rewrite attempt; an unusable reply falls back to the template (`cover.ai_note`).
- `cover = {channel, subject, greeting, closing, signature, blocks[{heading, sentences[{id, text, fact_ids, kind, src, status: ok|unbacked|resolved}]}], missing[], words, tone, generator}`.
- `GET /variants?application_id=`, `GET /variants/{id}`, `DELETE /variants/{id}` (200 kept, oldest dropped).
- `POST /variants/{id}/resolve {action}`: `remove {sentence}`, `edit {sentence, text, fact_ids?}` (re-checked), `confirm {sentence, fact_text?}` (adds a confirmed fact), `fill {label, value}` (a `[FILL: label]` blank), `ack_risk`. **Blocked** = any `unbacked` sentence, any open blank, or scam severity high without `ack_risk`.
- `GET /variants/{id}/diff` -> unified diff of the base resume against the variant.
- `GET /variants/{id}/export?format=md|txt|docx|pdf&part=resume|cover` (cover export is refused with 400 while blocked). DOCX and PDF come from small stdlib writers (plain text, ATS friendly).
- `POST /variants/{id}/apply {application_id}` writes subject, body and cover note into that application's draft via `Applyq.update`. A blocked variant adds a visible `[FILL: resolve the flagged claims in Tailor first]` line, so `Applyq.send` refuses. It never sends.

## Answer bank (`/api/answerbank`)
Entry: `{id, label, category, questions[], answer, owner_only, approved, source: starter|owner|ai|ai_approved, fact_ids, uses, state: ready|empty|pending|owner_only}`. Slots in answers: `{company} {role} {name} {first_name} {link_github} {link_linkedin} {link_portfolio} {link_website} {link_email} {rate} {availability} {location} {years} {proof}`; an unfillable slot becomes `[FILL: slot]`. Category `demographic` is always `owner_only`.
- `GET ?q=`, `POST`, `PUT /{id}`, `DELETE /{id}`, `POST /reset-starters`.
- `POST /match {question, job?, use?}` -> `{status: auto|suggest|owner_only|none, confidence 0-100, answer, entry_id, fills[], fact_ids[], alternatives[], reason}`. `auto` needs an approved answer, confidence at least `answerbank_min_confidence`, no blank, and not ambiguous. Owner-only questions (demographic, ID, date of birth, criminal record, bank, salary history, consent) return the suggestion "Prefer not to say" and are never auto.
- `POST /match-form {questions[], job?}` -> `{results[]}`.
- `POST /draft {question, job?, provider?}`: AI draft grounded in the facts, checked with the honesty rules, saved pending (`status: needs_ok`); invented content is dropped (`needs_you`).
- `GET /pending`, `POST /{id}/approve {answer?}`.

## Integrations (additive)
- `formfill.py`: `answerbank.formfill_answers(ctx)` is merged into the form filler's standard answers (approved, filled, no slots needing a posting, never owner-only). Formfill still refuses sensitive fields itself.
- `applyq.py`: `answerbank.draft_answers(ctx, item, profile)` adds answers for the topics the posting mentions (pay, notice, availability, work authorization, relocation, links) without overriding profile answers.

## Assistant tools
`tailor_preview` (untrusted: posting text; sends nothing), `answerbank_list`, `answerbank_add` (asks first). Group `tailor` in `assistant.TOOL_GROUPS`.

## Master resume and version
`POST /preview` also records `master {id, name, version, label, stale, auto}` and `resume_version` (the label, for example "Backend v2") on the variant, from the master resume named in `master_id` (or `"none"`; default: the family that best matches the posting, if any). Applying the variant copies `resume_version` onto the application item, where the job tracker reads it for its per-resume reply stats. See docs/api/resumes.md.
