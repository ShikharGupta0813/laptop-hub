# Freelance proposals (hub/proposals.py)

A proposal draft for a freelance or task posting, three opening lines, and a record of which opening wins more. All routes need the login cookie. **Copy only:** there is no send, submit or apply route and no platform is ever automated; the owner copies the text and submits it on the platform by hand. Switch: `proposals_enabled` (default true). Storage: `proposals.json` in the data folder (newest 400).

## Routes (`/api/proposals`)
| Route | |
|---|---|
| `GET /` | the screen's ONE data call: `{tasks[] (freelance and task items already in the hub), proposals[] (newest 40), stats, openings[], settings, sends: false}` |
| `POST /draft` `{item_id \| title, company, description, url?, rate?, currency?, unit?, timeline?, name?}` | `{id, text, blocked, blockers[], openings[{key, label, line, text, fact_ids, blocked, blockers}], opening, sentences[], fact_ids[], question, no_facts, note}`. The draft is stored. 400 for no task or a bad rate; 404 for an unknown item |
| `POST /{id}/used` `{opening?, note?}` | the owner says "I pasted this on the platform, with this opening". Records it (and the date); sends nothing |
| `POST /{id}/outcome` `{outcome: won\|lost\|no_reply}` | the owner records the result (only after "used") |
| `GET /stats` | which openings win more |
| `DELETE /{id}` | remove a proposal (and its place in the stats) |

## The draft
Hook (the opening line), relevant proof, approach, timeline, rate, one question to the client, sign-off. Plain code only: the same posting and facts give the same draft.
- **Proof** is the owner's own bullets ranked by the posting (`tailor._rank_bullets`), written in the first person, each citing fact ids. All claim sentences go through `fitcheck.check_claims`: one the facts do not back blocks that version of the draft (`blocked`, `blockers`). A skill the posting asks for that the owner never confirmed is never claimed. With no facts there is no proof line (`no_facts`).
- **Approach** is three fixed steps that name the posting's own skills; they say nothing about the owner.
- **Timeline and rate** are the owner's typed values. Missing ones are `[FILL: ...]` and block Copy.
- **Question to the client** fills a gap in the posting: no budget, no deadline, no definition of done.
- **Openings** (three are offered, the first is the default): `proof` (your most relevant bullet first), `problem` (the job and the first thing you would confirm), `question` (ask before you quote), `direct` (short and plain). Each alternative is a complete proposal with its own `blocked` flag.

## A/B record
`used` stores which opening the owner pasted; `outcome` stores won, lost or no reply. `win_stats()` gives, per opening, bids used, decided, won, lost, no reply and the win rate. A rate is only compared when an opening has at least 5 decided bids (`min_bids`); a tie is not called a winner; the `verdict` sentence says plainly "Too few bids to tell" when that is the case. Unused drafts never count.

## Tool for Friday
`proposal_stats`: the verdict and the per-opening numbers as text. Read-only; no approval.
