# Formfill API (fill a web application form on the laptop, then wait for your tap)

`hub/formfill.py` (mapper, browser driver, flow, routes), assistant tools in `hub/applyq_tools.py`, UI in `js/applyq.js` (review sheet on a ready web item).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}` (plus extra keys noted below). Off until `formfill_enabled` is true in `hub.json`.

**Hard rule: nothing is submitted without your tap.** Starting a run opens the page and fills it; the only click that submits is `POST .../submit`, which needs
`confirm: true`, the `field_digest` of the screenshot you looked at, a screenshot that was actually served to you, an unchanged page and a run under 30 minutes old.

## Setup (once, on the laptop)
`pip install playwright`, then `python -m playwright install chromium`, then set `formfill_enabled` to `true` in `hub.json`. Playwright is an optional dependency
(`requirements-extra.txt`); without it `GET /api/formfill/status` says so with the install line and start answers `503`. If bundled Chromium is missing the driver tries
the laptop's own Chrome, then Edge (always present on Windows).

## Config (`hub.json`, appended)
| Key | Default | Meaning |
|---|---|---|
| `formfill_enabled` | `false` | routes and tools work (otherwise `404`) |
| `formfill_headed` | `true` | show the browser window on the laptop. A login window is always visible. |
| `formfill_never` | `["linkedin.com"]` | sites that are never opened. Editing it through `PUT /api/formfill/never` saves `formfill_never.json` in the data folder, which then wins over this key. |

## Safety rules (all enforced in code, all tested)
- **Pages**: `https://` only, public hosts only (the research SSRF rules: private, loopback, link-local, tailnet, reserved ranges and local names are refused after DNS). Every navigation inside the browser, including redirects and iframes, is checked again and refused if it leaves public space. `allow_local` (constructor switch used only by one test) lets `http://127.0.0.1` through; it is off in the product.
- **Never list**: a host on it (or a subdomain) is refused with a clear sentence.
- **Never filled**: ID numbers, date of birth, salary history, gender/ethnicity/disability and similar, work-authorization and citizenship questions, bank details, passwords/codes, criminal-record questions, checkboxes and anything about consent/terms/"I certify", date pickers, sliders, hidden or locked fields (hidden fields are honeypots), and anything unknown. These are listed under "needs you" (or "skipped" when unknown and optional). A `[FILL: x]` marker in the draft is never typed into a page; the field is listed as needs-you.
- **Never invented**: values come only from the profile (name, headline, summary, links, experience, standard answers) and the application draft. First/last name only when the name has exactly two words. "Current company/title" only from an entry marked present/current, or a standard answer. Years of experience only from a standard answer or dates in the experience.
- **Cookies and passwords** stay in the browser profile `<data>/formfill_profile` on this laptop. No code reads, exports or sends them (a test greps for it). Password fields are never read, even for the change check.
- **No evasion**: no stealth, user-agent games or captcha solving. A captcha, login wall or "verify you are human" is reported as a blocker. If the site blocks automation the answer is "apply yourself" and the Copy flow still works.
- **Limits**: one browser session at a time, at least 20 s between starts (`429`), 5 minutes without a call closes the browser (a login window gets 10 minutes), 30 minutes total, and the daily cap is `applyq_daily_cap` (sent + applied today; over it `409 {needs_confirm}` and `confirm_over_cap: true`).
- **Privacy**: page text is never sent to a model. Audit entries are `formfill_start`, `formfill_login`, `formfill_submit`, `formfill_result` with the host and item id only (the result adds `applied`/`needs_check`), never field values or paths.

## Routes
- `GET /api/formfill/status` -> `{enabled, playwright, headed, never[], install, busy, reason}`.
- `GET|PUT /api/formfill/never` (`{never: ["site.com", ...]}`, up to 50, bare domains).
- `POST /api/applyq/{id}/formfill/start {confirm_over_cap?}` -> run view. Only ready/failed `web` items with a draft and an `https` address.
- `POST /api/applyq/{id}/formfill/login` -> opens a visible window on the item's site for you to log in; returns a `kind: "login"` run (no fields). Close it with cancel (this saves the session in the profile). It replaces nothing: close a filled form first (`409` if one is open).
- `GET /api/applyq/{id}/formfill` -> the open run for that item, or `{run: null}`. `GET .../{run}` -> the run view. `410` after the run ended (idle, stale, cancelled), `404` for an unknown id.
- `GET .../{run}/screenshot` -> PNG (`Cache-Control: no-store`). **Serving it records that you looked** at the current digest. The file is deleted when the run ends or after 30 minutes.
- `POST .../{run}/refresh` -> re-reads the page (for example after you typed something in the window) and takes a new screenshot and digest. It does not fill.
- `POST .../{run}/next {confirm: true}` -> clicks the page's Next button and fills the new step. A multi-step form is handled one visible step at a time: "step 1 of 3 filled".
- `POST .../{run}/submit {confirm: true, field_digest}`:
  - `400` without `confirm: true` or a digest.
  - `409 {changed: true, digest}` if the page address or any field value differs from the screenshot (a fresh screenshot is taken; look at it and confirm again), or `409 {needs_look: true}` if the screenshot was never served, or the run is stale.
  - `422` if no single Submit control was found (more than one fits, or none), or a captcha waits and the window is hidden.
  - otherwise clicks, waits, takes a result screenshot and answers `result: "applied"` only if the page says so ("thank you for applying", a confirmation URL with no form left); the item becomes `applied`. Otherwise `result: "needs_check"`: the item stays ready with a `needs check` flag and a note. The result screenshot stays until you dismiss it (cancel) or 30 minutes pass.
- `POST .../{run}/cancel` -> closes the browser and deletes the screenshot.

### Run view
`{run, item_id, host, kind: fill|login, state: ready|login|finished, step{current,total,has_next,label}, fields[{n,label,type,status: filled|needs_you|skipped,value,reason,required,key}], counts{filled,needs_you,skipped}, blockers[{kind: captcha|login_required|blocked|no_form, message}], digest, seen, can_next, can_submit, submit_label, screenshot_url, headed, idle_seconds_left, result, result_note}`.
`value` is shown to you only for filled fields (it is your own data). `digest` is a hash of the page address, the field layout and every value now in the page.

## Mapping
Autocomplete tokens, then label text (`<label for>`, wrapping label, `aria-labelledby`, `aria-label`, nearby text, placeholder, name/id) decide the key: name, first/last name, email, phone, location, LinkedIn, GitHub, portfolio, headline, about you, current title/company, years of experience, resume upload, cover letter, "why do you want to work here" (draft cover note). Otherwise your **standard answers** are matched by meaning (concept groups such as rate/salary/compensation or notice/availability/start), never for "rate your skills" style questions. A label that fits two keys equally is ambiguous and not filled. Selects and radios are filled only when one option clearly matches. Text longer than the field's limit is never truncated.
The resume is the file `formfill_resume.pdf` (or .docx/.doc/.txt/.rtf) in the data folder if you put one there, else your profile resume text as a temporary `.txt` (many sites want a PDF, so prefer the file).

## Assistant
`fill_application_form(id)`: level N2, asks for approval, prepares only; the result holds counts and the labels of what needs you, never field values. `submit_form(id)`: **in `safety.ALWAYS_ASK`**, so Full Control and auto-approve never skip the tap; its preview refuses unless you have looked at the current screenshot. Friday offers: "want me to fill it on the laptop?" after showing a web application.

## Not verified
Real job sites (every ATS builds forms differently: shadow DOM, iframes and custom widgets are not read), Chromium on Windows, the visible-window login flow on the owner's laptop, the review sheet on a real phone at 390px.
