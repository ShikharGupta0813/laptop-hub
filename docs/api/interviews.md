# Interview coach (hub/interviews.py)

All routes need the login cookie. Practice material only: nothing is sent anywhere. Switches: `interviews_enabled` (default true), `interviews_ai` (default false). Storage (data folder, mode 0600 where the OS supports it): `interviews.json` with `practice` (newest 300), `stories` (60) and `negotiation`. The owner's practice answers are their own words, stay on this laptop and can be deleted one by one or all at once.

## Routes (`/api/interviews`)
| Route | |
|---|---|
| `GET /` | the screen's ONE data call: `{applications, stories, suggestions, practice(20), negotiation, has_facts, settings{enabled,ai}, sends:false}` |
| `POST /questions` `{application_id \| job:{title,company,description} \| title,company,description, ai?}` | `{job, skills[], questions[{id,kind: technical\|behavioural\|motivation, text, why, fact{id,text}\|null, source: rules\|ai}], generator: rules\|rules+ai, ai_note?}` |
| `POST /feedback` `{question, kind, answer, ai?, role?, company?}` | feedback without storing |
| `POST /practice` (same body) | the same, and the answer is stored. Returns the entry `{id, question, kind, answer, feedback, critique, ai_note, created}` |
| `GET /practice`, `DELETE /practice/{id}`, `DELETE /practice` | list, delete one, delete all (`{deleted}`) |
| `GET /stories` | `{stories, suggestions}` |
| `POST /stories`, `PUT /stories/{id}`, `DELETE /stories/{id}` | `{title, situation, task, action, result, fact_ids}`; ids that are not real facts are dropped; `unverified_numbers` lists numbers in the story that no fact mentions (a reminder, not a block) |
| `POST /about-me` `{seconds: 60\|90, application_id?\|job?}` | `{text, words, est_seconds, sentences[], fact_ids[], blocked, blockers[], empty, note}` |
| `POST /brief` `{application_id \| job}` | `{title, company, location, summary, duties[], must_haves[], nice_to_have[], skills[], matched[], gaps[], ask_them[], checklist[], note}` |
| `GET /negotiation`, `PUT /negotiation` `{currency, low, target, high, notice, notes}` | the sheet; numbers must be 0 to 10,000,000,000 and ordered low <= target <= high |

## Plain code first
- **Questions** (`make_questions`): skills found in the posting from a built-in word list (the title counts extra; words such as "go" or "rest" are not skills), a technical question or two per skill, behavioural questions chosen by signals in the text ("mentor", "deadline", "stakeholder", "on-call", ...), always three core behavioural questions plus "Why this role?" and "Tell me about yourself". Same posting, same questions (ids are a hash of the text). When the owner has a fact that fits a question, it is shown as a hint.
- **Feedback** (`rule_feedback`): length (80 to 230 words for behavioural, 40 to 200 otherwise; about 145 spoken words a minute), STAR parts (situation, task, action with "I", result), ownership (too many "we"), a number, filler words. Technical answers are checked for a reason or an example instead of STAR. Numbers in the answer that no fact mentions are listed as a reminder.
- **Story starters** (`story_suggestions`): the owner's own bullets (those with a number first). The bullet is the Action, the role is the Situation; Task and Result are left empty for the owner.
- **Tell me about yourself** (`about_me`): present role, years counted from dates, summary (90 s), relevant skills, one or two achievements, education (90 s), and a "that is why this role interests me" line when a job is given. Capped at 150 words (60 s) or 225 words (90 s) by dropping optional sentences. Every claim sentence cites fact ids and is checked with `check_claims`.
- **Brief** (`make_brief`): the first paragraph, the duties and requirements lines, must-haves; matched and missing must-haves from `fitcheck.analyse` (a "gap" is a must-have with no confirmed fact); questions to ask; a checklist. Nothing is looked up on the internet.
- **Negotiation** (`negotiation_sheet`): generic tips and scripts. The only numbers are the ones the owner typed; an empty one shows as `[FILL: ...]`.

## Optional AI
`ai: true` (or `interviews_ai`) asks the owner's model through `ctx.model_for` (redaction applies).
- Questions: the model gets only the posting text (title, company, description up to 3000 characters) and the list of questions already made. Its extra questions are added after the rule questions, never instead of them.
- Critique: the model gets the question, the answer the owner typed (this is the one place the owner's own words leave the laptop, and only when they switch AI on) and the job title and company. It returns at most about 80 words. It is told not to invent facts.
- No AI, an error or an unreadable answer: the rule-based result is returned and `ai_note` says why.

## Tool for Friday
`interview_prep {part: questions|brief|about_me, application_id? | title, company, description, seconds?, ai?}`: returns a short text. Read-only practice material; marked untrusted (posting text is data); no approval; cannot send.

## Optional link to the application tracker
`hub/jobtrack.py` (another lane) is imported inside `try/except` and not used yet; the coach works from `hub/applyq.py` data only.
