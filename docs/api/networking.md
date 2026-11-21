# Networking drafts (hub/networking.py)

All routes need the login cookie. **Nothing here sends anything**: there is no send route. The owner copies a draft and sends it by hand (LinkedIn and Indeed are never opened or automated). Switches: `networking_enabled` (default true), `networking_nudge_days` (1 to 365, default 14), `networking_ai` (default false). Storage (data folder): `networking.json` (contacts and drafts, newest 500 and 200 kept).

## Honesty rule
Drafts use the same fact base and honesty check as tailoring (`hub/factbase.py`, `hub/fitcheck.py` `check_claims`). A sentence about the owner must cite fact ids that exist, and numbers, tool and employer names must be in those facts. The owner's own typed context (the contact's name, "how you know them", the topic, a date) is allowed in the draft because the owner wrote it. An unbacked sentence or an open `[FILL: ...]` blank sets `blocked: true`; the screen disables Copy until the owner removes it, rewrites it, or confirms it as a fact. A draft is at most 120 words: optional sentences (the proof, skills, availability, links) are dropped from the end until it fits.

## Routes (`/api/networking`)
| Route | |
|---|---|
| `GET /` | the screen's ONE data call: `{contacts, nudges, drafts, applications[{id,title,company,state}], kinds[{id,label}], settings{enabled,nudge_days,ai}, max_words, sends:false}` |
| `POST /contacts` `{name, company?, role?, how_known?, last_contact?, note?}` | add (name required; `last_contact` is `YYYY-MM-DD`, not in the future) |
| `PUT /contacts/{id}`, `DELETE /contacts/{id}` | edit (only the fields sent change), delete |
| `GET /nudges` | `{nudges, days}`: suggestions only |
| `POST /drafts` `{kind, application_id? \| company?, role?, description?, contact_id?, contact_name?, how_known?, interviewer?, topic?, sent_on?, ai?, provider?}` | build and store a draft. `kind`: `referral recruiter thank_you follow_up` |
| `GET /drafts`, `GET /drafts/{id}`, `DELETE /drafts/{id}` | |
| `POST /drafts/{id}/resolve` `{action, sentence?, ...}` | `remove {sentence}`, `edit {sentence, text, fact_ids?}` (re-checked), `confirm {sentence, fact_text?}` (adds a confirmed fact), `fill {label, value}` |
| `POST /drafts/{id}/sent` `{date?}` or `{undo:true}` | "marked as sent by me": records the date (default today, never future) and moves the linked contact's `last_contact`. It does not send. |

A draft: `{id, kind, label, subject, text, words, over_cap, blocked, blockers[], cover{blocks[{sentences[{id,text,fact_ids,kind,src,status,reason}]}], generator: template|ai, ai_note?}, application_id, contact_id, company, role, sent_on, created, note}`.

## Nudges
A contact with a `last_contact` of at least N days ago (N = `networking_nudge_days`) is listed, oldest first. A contact with no date is never nudged. An application in the queue with state `sent` or `applied` (not `replied`) for N days, with no follow-up draft marked sent since, is also listed (`kind: application`). A nudge only suggests; tapping "Draft follow-up" opens the form filled in. Marking a follow-up as sent resets it.

## Optional AI
`ai: true` (or `networking_ai`) asks the owner's model (through `ctx.model_for`, so redaction applies). The model gets only the posting text (title, company, description up to 2000 characters) and up to 30 confirmed fact sentences without email addresses: never contact names or the "how you know them" text. Its sentences go through the honesty check; if any claim is unbacked, the draft is over 120 words, the answer cannot be read, or no AI is ready, the plain template draft stays and `cover.ai_note` says why.

## Tool for Friday
`network_draft {kind, application_id? | company, role, description, contact_name, how_known, interviewer, topic, ai?}`: builds a draft and returns the text. No approval is needed because it only stores a local draft; it is marked untrusted (posting text is data). It cannot send.

## Warm path finder (added)
Contacts have one more optional field, `past_companies` (text, up to 200 characters, for example "Initech, Hooli").

`POST /warm-path` `{company | application_id, role?, draft?, ai?}` answers "who do I know at this company": `{company, role, candidates[{contact, score, reasons[]}] (best first, at most 6), best (contact id or ""), tracker[{role, status, applied_at}], tips[], draft, sends: false}`.
- Matching is by whole company name (legal endings such as Inc or Pvt Ltd are ignored): the contact's `company` (works there now: 10 points exact, 8 when one name holds the other as whole words), `past_companies` (5), or the company named in `how_known` or `note` (2). "Acme" does not match "Acmeology". A contact written to in the last 180 days adds 2; an engineering, manager, recruiter or HR role adds 1.
- `tracker`: your application records (`jobtrack.json`) for that company. With an interview already in progress the tip says to ask for an introduction to the hiring manager instead of a referral.
- `draft: true` also builds the referral request for the best person through the normal draft (`POST /drafts`, kind `referral`, same honesty check and 120-word cap, stored with the other drafts). A draft needs facts; without them the answer is a plain 400. Nothing is ever sent.

## Optional link to the application tracker
`hub/jobtrack.py` (another lane) is imported inside `try/except` and not used yet; networking works from `hub/applyq.py` data only.
