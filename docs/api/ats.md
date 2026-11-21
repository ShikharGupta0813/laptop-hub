# ATS match score and resume read check API

`hub/ats.py` (score, read check, routes, assistant tool `ats_score`) and `hub/skillwords.py` (the skills dictionary). Screen `js/ats.js` + `css/ats.css` (Apps > ATS and packets, tabs Match and Check).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Switched off with `tailor_enabled: false` (routes answer 404). No new settings.

## Rules
- **Plain code, no AI, nothing leaves the laptop.** The module imports no mail, chat or HTTP client. A resume file is read in memory and is never stored; the audit line holds sizes only.
- **Only confirmed facts count.** A keyword the posting asks for is shown in one of two lists and is never added to a resume:
  - `listed_in_facts`: "you have this": it is in your fact base (`hub/factbase.py`) but not on this resume. A suggestion; you add it yourself.
  - `gap`: "you do not have this": shown only as a gap.
- Postings and resumes are data. Odd or huge input never raises; text is cut at 60,000 characters.

## Score (`POST /api/ats/score`)
Body: `{application_id}` (an item of the application queue) or `{job: {title, description, company?, location?}}`; and one resume: `variant_id` (a tailored variant from Tailor), `master_id` (+ `version`), `resume_text` (pasted), or nothing (your base resume from every fact).
Returns `{score 0-100 | null, label strong|good|fair|low, breakdown[{key, label, score|null, weight, note, matched?, missing?, required?, have?}], matched[keyword], missing{listed_in_facts[], gap[]}, tips[], keywords_total, unknown_phrases[], resume, job, note}`.
A keyword is `{term, kind hard|soft|cert, weight, bucket must|general|nice, in_title, count, known, fact_ids?}`.

Breakdown categories and weights: hard skills 45, soft skills 10, job title 15, years of experience 15, education and certificates 10, location 5. A category the posting says nothing about (`score: null`, weight 0) is left out and the others are scaled up, so a posting with no degree line is not punished for it.
- **Hard / soft skills**: share of the posting's keyword weight that the resume contains (synonyms count: JS = JavaScript, k8s = Kubernetes, Postgres = PostgreSQL).
- **Job title**: the posting title against your role titles and headline (developer = engineer, seniority words checked separately).
- **Years**: the largest "N years" the posting asks for, against the years your dated roles cover.
- **Education and certificates**: the degree level the posting names (and "or equivalent"), and named certificates (AWS Certified, PMP, CFA, CA...).
- **Location**: remote = full; a named city against your location preference and resume; "can relocate" counts partly.

Keywords: a curated dictionary (`hub/skillwords.py`: tech, data, product, ops, Indian-market terms such as UPI, NPCI, GST, KYC, NBFC; soft skills; certificates; each with synonyms) plus phrase rules ("experience with X, Y and Z", tech-looking words). A phrase the dictionary does not know counts 0.6 of a known skill and is matched by exact words. Weight by place: a line under Requirements or with "must/required" is 3, a general line 2, Nice to have 1, plus 0.5 for each repeat (up to 3) and +2 when it is in the job title.

## How an ATS reads your resume (`POST /api/ats/readcheck`)
Body (one of): `{filename, data_b64}` (PDF, DOCX or text, 6 MB at most), `{text}`, `{variant_id, format: pdf|docx|txt}` (an export of a tailored resume), or `{}` (your saved resume text; layout traps cannot be checked for text, and the answer says so).
Returns `{format, size, readable 0-100, label, fields{name, email, phone, links, titles, companies, dates, skills, education, sections, headline}, traps[{code, severity high|medium|low, title, detail, fix}], ats_view (the text an ATS gets, 4,000 characters), meta, pdf_supported, note}`.
Traps found: tables, columns, text boxes, text that is a picture (scanned PDF), pictures, contact details in a header, footer or page margin, missing or partial dates and year-only dates, creative section names ("My Journey", "Toolbox"), missing Experience / Skills / Education headings, no email or phone or clear name, special characters (icons, arrows, ligatures, hidden spaces), a file over 2 MB, more than two pages, unusual or not-embedded fonts, garbled letters. PDF uses `pypdf` (already in requirements.txt); if it is missing the answer says so and text and DOCX still work. DOCX is read with the standard library. Other types (.doc, .rtf, .odt, .pages) are refused with advice.
This is a guide to common ATS behaviour, not a test of one named system.

## `GET /api/ats/info`
`{enabled, pdf_supported, docx_supported, max_file_mb, weights, skills_known}`.

## Friday
`ats_score` (read-only; untrusted: posting text): `application_id` or `title` + `description`, optional `variant_id` / `master_id`. Group `ats` in `assistant.TOOL_GROUPS`.

Tests: `tests/test_ats.py`.
