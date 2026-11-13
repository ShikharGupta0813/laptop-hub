# Research API

`hub/research.py` (jobs, search, fetch, report, tool `deep_research`), screen `js/research.js` + `css/research.css`, store `research.sqlite` in the data folder.
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. `404` while `research_enabled` is false.

## What it does
"Jarvis, research X" runs a multi-hop investigation on the public web and returns a short spoken answer plus a full report with numbered sources.
Pipeline: plan 2-4 queries -> search -> fetch top pages -> extract readable text -> write a report with inline `[n]` citations -> optional follow-up hops
from the gaps (at most `research_max_hops`).

## Styles (the two personas)
| style | persona | for | short answer |
|---|---|---|---|
| `technical` | Jarvis (cyan) | errors, libraries, how-tos, comparing tools; code and commands in plain lines | verdict, the one fix or command, one caveat |
| `brief` | Friday (amber) | prep for a meeting, person or company; trips and purchases; "what should I know before my 3pm" | what matters first, then 2-3 facts to remember |

The default comes from the persona that was addressed (`persona: "friday"` -> `brief`, otherwise `technical`). A `brief` also reads the owner's own
context through the hub's read-only routes (`GET /api/work/calendar`, `GET /api/work/asana/tasks`); a failure there is ignored. That context is not a numbered source.

## Config (`hub.json`, all editable in Settings)
| Key | Default | Meaning |
|---|---|---|
| `research_enabled` | `true` | routes, tool and screen |
| `research_max_hops` | `3` | search rounds (1-5) |
| `research_max_sources` | `8` | pages read in total (1-20) |
| `research_daily_ai_calls` | `20` | model calls per day (0-500); planning and each writing round count, a JSON retry counts |

## Routes
- `POST /api/research {question, style?, persona?}` -> `{id, status: "running", style}`. Question 3-600 characters. At most 2 jobs run at once (`429`).
- `GET /api/research/{id}` -> `{id, question, style, status: running|done|failed, steps[], report, sources[{n,title,url,domain,snippet,read}], answer_short, degraded, error, created, finished}`.
  `steps` are plain progress lines ("Searching: ...", "Reading: domain").
- `GET /api/research` -> `{jobs[{id,question,style,status,answer_short,sources(count),created,finished,degraded}], budget_left}` newest first, capped at 200.
- `POST /api/research/{id}/notify` -> push a notification (ntfy, via the notify module) when the job finishes (now, if it already has).
- `DELETE /api/research/{id}` -> cancels it if running and removes it.
Jobs still running when the hub restarts are marked failed ("Interrupted by a restart.").

## Assistant tool
`deep_research {question, style?, persona?}` (ONE `TOOL_GROUPS` line `research`: research, look up, find out, investigate, deep dive, "what should I know before").
It starts a job, waits about 25 s and returns the short answer (at most 120 words, no citations, plus the source sites). If the job is slower it says
"I'm on it, the report will be in Research", the job keeps going and sends a notification when done. `needs_approval` is false: it only reads public web pages
(SSRF-guarded), writes only its own history, and the model behind it has no tools.

## Safety
- Search needs no key: DuckDuckGo's HTML endpoint, Wikipedia's API as the fallback.
- Pages: http/https only; localhost, private, link-local, `100.64.0.0/10` (tailnet), reserved and `.local`/`.internal`-style names are refused, the host's DNS answers are
  all checked, and every redirect is checked again (max 4). Timeouts 8 s connect / 12 s read, 1.5 MB cap, only `text/html`, `application/xhtml+xml`, `text/plain` (PDF is skipped).
- Page text is UNTRUSTED: it is cut to 6000 characters per page, `<<<`/`>>>` are removed so it cannot close its delimiter, and it sits between
  `<<<SOURCE n ...>>>` markers with an instruction never to follow anything inside. The model gets no tools. Citation numbers that point at no source are removed.
- AI: the same free path as Autopilot (Gemini if ready, else the local model; never the paid Claude API, never the Claude+Gemini duo).
- Budget spent or model down: the report lists the sources with their snippets (`degraded: true`).
