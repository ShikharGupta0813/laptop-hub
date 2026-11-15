# 36 · Tailor and answers: a resume and cover note for each job, from your own facts

**What this page is for:** how REDLINE prepares a resume and a cover note (or freelance proposal) for one posting, checks that every sentence is true, and fills form questions from answers you wrote. Apps > Tailor and answers.

## The idea
REDLINE keeps a **fact base**: every statement it may make about you, taken from your profile (roles with dates, achievements, skills, education, links, your standard answers) plus facts you confirmed by hand. Each fact has an id. A drafted sentence must name the facts behind it. A sentence the facts do not back is **blocked**: you remove it, rewrite it, or confirm it as true. Nothing is ever sent from this screen.

## Tailoring one posting
1. **Tailor** tab: pick an application from your queue, or paste a posting. Choose the format (email, application form, freelance proposal, cold outreach, short message) or let REDLINE choose.
2. **Fit and honesty check**: a score out of 100, each requirement marked **matched**, **partial** or **missing** with the facts behind it, and flags: more or less experience than they ask, a different place or time zone, pay under your minimum, visa or work-authorization needs, and scam signals. If you have not set a preference (your location, minimum pay, time zone, where you may work), that check is listed as **not checked**; REDLINE never guesses.
3. **Cover note or proposal**: short, opens with your most relevant concrete achievement, no flattery. A freelance proposal restates the task in the posting's words, lists how you would approach it, shows relevant work, and gives availability and your rate from your settings (or a blank to fill).
4. **Resume for this posting**: your own bullets reordered and trimmed for the posting, your skills listed first when the posting asks for them and written the way the posting spells them, a short targeted summary. A keyword the posting wants is only used if your facts back it. **Compare with my base resume** shows what changed. Export as Markdown, text, DOCX or PDF (plain, one column, readable by applicant systems).
5. **Use in application** writes the cover note into that application's draft. If any claim is blocked or a blank is open, a visible `[FILL: ...]` line goes in too, so the queue refuses to send until you resolve it. Sending still needs your tap in Applications.

Every variant is kept per application under **Earlier variants**.

## The honesty check
It looks at every sentence: it must cite facts that exist, and the words that matter (numbers, tool and employer names, anything that sounds like experience) must appear in the facts it cites. "I led a team of 12" with no fact behind it, "50M requests" when your bullet says 2M, a tool you never listed: all blocked. If AI wording is switched on, the AI's sentences go through the same check, and it gets one chance to rewrite what was rejected.
- **Remove** drops the sentence.
- **Rewrite** lets you change it; it is checked again.
- **This is true** saves your words as a confirmed fact (visible and editable under Facts) and backs the sentence with it. Only confirm what is true.

## The answer bank
Your saved answers to the questions every application asks: salary expectation, notice period, work authorization, relocation, availability, links, "why this company". It starts with labelled blank entries (links fill themselves from your profile). Write your answers once.
- **Blanks** such as `{company}`, `{role}`, `{rate}`, `{availability}`, `{link_github}` and `{proof}` (your best matching achievement) fill themselves; one that cannot be filled shows as `[FILL: ...]` and stops auto-fill.
- **Matching** is fuzzy and runs on your laptop (word stems and synonyms; with *Match questions with my local model* on, also embeddings). Each match shows a confidence. Only an **approved** answer at or above your limit (setting `answerbank_min_confidence`, default 75) may be pre-filled; lower is only a suggestion; two close answers that differ are never pre-filled.
- **Always yours**: gender, ethnicity, disability, veteran status, religion, sexual orientation, age or date of birth, ID numbers, criminal-record and bank questions. The bank suggests "Prefer not to say" and never fills it in.
- **A question it does not know**: *Draft one from my facts* asks your AI for a short answer using only your facts. The draft is checked, saved as **needs your OK**, and joins the bank only when you approve it (you can edit it first).
- The bank also feeds the form filler (answers to questions your profile does not already cover) and the drafts in Applications (answers for the topics a posting mentions).

## Settings
Apps > Tailor and answers > Preferences: tone and sign-off (you can add tones), rate and availability as you want them written, your location, time zone, minimum pay, where you may work, whether you need sponsorship, words that mean the same (for example `k8s = kubernetes`), and the switches `tailor_enabled`, `tailor_ai`, `answerbank_enabled`, `answerbank_embeddings`, `answerbank_min_confidence`.

## Voice and chat
"Tailor my resume for application <id>" (`tailor_preview`), "what is in my answer bank" (`answerbank_list`) and "save this as my answer" (`answerbank_add`, asks first).

## Privacy
Your profile stays on the laptop. With AI wording or an AI answer draft, only the posting text and the selected fact sentences (no contact details, no raw resume) go to the AI you chose, through the same redaction as everything else.

Details: `docs/api/tailor.md`. Back to the [guide index](README.md).
