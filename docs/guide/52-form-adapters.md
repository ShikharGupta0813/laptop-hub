# 52 · Form filling on more job sites

> 🟡 Not tested on real job sites yet. Built and tested with invented pages only.

**In simple words:** the hub can already fill a web application for you and wait for your tap. Now it also understands five more job-site systems: SuccessFactors, Taleo, Oracle Recruiting, iCIMS and Ashby. These sites use their own pop-up lists, Yes/No buttons and (iCIMS) a form inside a frame. The hub can now use them, fill your name, email, phone, links and resume, and then stop. You press Next and Submit yourself.

You use it the same way as before: open an application that is ready on the Apply queue and tap **Fill the form on my laptop**. Nothing new to set up.

## What it fills
- Your name, email, phone, links, resume file and city.
- Country, state and similar, only if you saved an answer with exactly that name in your standard answers.
- "How did you hear about us": Job Board, else Company Website.
- Yes/No questions, only when your profile already has the answer (for example "Willing to relocate").

## What it never touches
- Sign-in or create-account pages. It tells you to use **Open a login window** and log in yourself.
- Voluntary or equal-opportunity pages and sections, gender, ethnicity, disability, veteran, visa, work authorization, salary history, date of birth, ID numbers.
- Every date picker, and every "I agree / I consent" box.
- Next and Submit. Next asks you first. Submit needs you to look at the screenshot, and if anything changes after that, it refuses.

## Good to know
- A choice from a pop-up list is only made when exactly one option clearly matches, and the page must show it afterwards. If not, you see "needs you" for that field.
- These sites do not get Work Experience or Education entries added (only Workday does). Upload your resume and let the site read it, or type them yourself.
- If the hub cannot read a page (some sites build widgets that are not real inputs), the field stays "needs you". Fill it by hand in the window.

Details for developers: [docs/api/formfill.md](../api/formfill.md), section "SuccessFactors, Taleo, Oracle Recruiting, iCIMS, Ashby".
