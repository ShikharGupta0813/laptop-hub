# Offers and negotiation (hub/offers.py)

Record offers, compare up to four, draft a counter-offer, check clauses and check a posting's pay. All routes need the login cookie. Nothing is sent; there is no send route. Switch: `offers_enabled` (default true). Storage: `offers.json` in the data folder (`offers`, `assumptions`). Local data only: no scraping, no Glassdoor, no Levels.fyi.

**Every number is the owner's.** The hub never knows or guesses market pay. A part the owner did not type counts as zero and is listed under `missing`.

## Routes (`/api/offers`)
| Route | |
|---|---|
| `GET /` | the screen's ONE data call: `{offers[] (each with estimate, totals, clause_review, esop_per_year), assumptions, defaults, labels, clauses[], pay_target, rules, max_compare: 4, sends: false}` |
| `POST /`, `PUT /{id}`, `DELETE /{id}` | an offer: `company, role` (required), `currency` (3 letters, default INR), `fixed, variable, variable_payout_pct, bonus, joining_bonus, benefits_value, relocation_cost, relocation_support`, `notice_days, notice_buyout_days, buyout_covered, probation_months`, `location, work_mode, benefits, notes, recruiter`, `start_date, reply_by` (YYYY-MM-DD), `esop{grant_value, vesting_years, cliff_months, note}`, `clauses{id: yes\|no\|unknown}`, `status` (open, negotiating, accepted, declined). A field not sent on PUT is kept. At most 30 offers. Plain 400s for bad numbers and dates |
| `PUT /assumptions` | change the assumptions below (checked; regime must be new or old, percents 0 to 100) |
| `POST /compare` `{ids: [2 to 4]}` | `{offers[], rows[{key, label, values[], best[], note, numeric}], notes[], assumptions, rules}` |
| `POST /{id}/counter` `{ask{fixed, variable, joining_bonus, notice_days, start_date, other}, use_competing[ids], name_competitor, reasons[], recruiter, owner_name, channel: email\|call}` | `{subject, text, blocked, fills[], note, sends: false}` |
| `POST /pay-check` `{item_id \| application_id \| title, company, description, pay_text}` | the pay check (below) |

## In-hand estimate (INR offers only)
`estimate()` is plain arithmetic from the offer and the **assumptions**, which are printed next to every result and can be edited (`PUT /assumptions`):

| Assumption | Default | Meaning |
|---|---|---|
| `regime` | new | tax regime, new or old |
| `basic_pct` | 50 | basic pay as a percent of the fixed pay (PF is worked out on it) |
| `pf_rate_pct` | 12 | employee and employer PF rate |
| `pf_capped`, `pf_wage_cap_monthly` | false, 15000 | PF only on the wage cap |
| `ctc_includes_employer_pf` | true | the fixed pay already includes the employer's PF |
| `professional_tax_annual` | 2400 | state professional tax per year |
| `variable_payout_pct` | 80 | expected payout of variable pay (a per-offer `variable_payout_pct` overrides it) |
| `esop_counted_pct` | 0 | how much of the ESOP paper value counts as cash (0: shown apart) |
| `other_deductions_old` | 0 | extra deductions you can claim, old regime only |
| `current_monthly_gross` | none | your current monthly gross pay, to price a notice buy-out |

Tax rules built in (`TAX_RULES_AS_OF`, FY 2025-26, check them each year): new regime slabs 0 to 4 lakh nil, then 5, 10, 15, 20, 25 and 30 percent per 4 lakh, standard deduction 75,000, 87A rebate (nil tax up to 12 lakh taxable, with marginal relief just above); old regime 0 to 2.5 lakh nil, 5 percent to 5 lakh, 20 percent to 10 lakh, then 30 percent, standard deduction 50,000, 87A rebate to 5 lakh taxable, employee PF counted under 80C (cap 1.5 lakh) plus your other deductions; 4 percent cess on both. Not modelled: surcharge, HRA, 80D, gratuity, insurance and allowances. The result is always labelled an ESTIMATE.

Worked example (new regime, defaults, fixed 18,00,000): employer and employee PF 1,08,000 each, taxable 16,17,000, tax 1,28,336, in hand 1,21,105 a month.

- **Monthly in hand** is from the fixed pay only: (fixed - employer PF - employee PF - professional tax - the fixed share of the year's tax) / 12. **With variable** adds the expected variable and bonus (`avg_monthly_all_in`).
- **Variable** is an expected value: target times the payout percent.
- **ESOP** is shown as paper value per year (grant value over vesting years). It is not in the cash total unless `esop_counted_pct` is above 0.
- **Notice buy-out** = `current_monthly_gross` / 30 times the days to buy out, or 0 when the company covers it, or unknown when you have not typed your current pay.
- **Relocation** = your cost minus the company's support. **First year** = cash per year + joining bonus - buy-out - net relocation. The joining bonus after tax uses the extra tax it causes.
- Other currencies: no estimate; rows are compared on gross figures and not ranked when the offers differ in currency.

## Compare
Two to four offers side by side. `best` marks the highest number in a row (lowest for buy-out) when all offers share a currency. It is a hint, not advice.

## Counter-offer drafts
Email or call script. Every number in the text is from the offer, from the owner's `ask`, or from a competing offer the owner ticked (`use_competing`); the competitor is named only if `name_competitor` is true, and an offer with no figures is not mentioned. A missing ask or name is a `[FILL: ...]` blank; `blocked` is true until it is filled and the UI keeps Copy off. Reasons are the owner's own words. Never claim an offer you do not hold.

## Clause checklist
Non-compete, service bond, clawback, IP assignment, notice and buy-out, probation, variable pay terms, ESOP terms. Each is `yes`, `no` or `unknown` per offer, with the risk in a sentence and the question to ask the recruiter. A checklist, not legal advice.

## Pay check
`pay_check()` reads the advertised range from the posting (`oppsnorm.parse_pay`: Rs 18-24 LPA, $90-120k, monthly figures turned into a year; hourly and per-project are not compared), the owner's target from the Interview coach pay sheet (`interviews.json` negotiation), and ranges of similar postings already in the hub (at least two title words in common, same currency). Verdict: `below` (the top of the range is under the target), `in` (the target is inside the range), `above` (the bottom is at or above the target) or `unknown` (with the reason: no target, currency mismatch, no readable range). With no advertised range, the median range of similar postings is used and `verdict_basis` says so.

## Tool for Friday
`offer_compare {ids?}`: compares the owner's recorded offers (default the newest up to four) and returns the rows as text with the tax rules and the estimate warning. Read-only; no approval.
