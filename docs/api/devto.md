# Posts API (dev.to publishing, post packs, profile polish)

`hub/devto.py` (routes, engine, scheduler, tools), screen `js/devto.js` + `css/devto.css` (Apps > Posts), state `devto.json` in the data folder.
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. `404` while `devto_enabled` is false.

## What it does
- **Ideas and drafts** from the owner's own work: Brain memories (rca, decision, fact, research), Build-flow specs, and the hub repo's recent commit subjects
  (read-only, `git log` subjects only). The free AI path writes them (`pick_provider`, never the paid Claude API; the draft function is injectable in tests; at most
  20 AI calls a day); when it is unavailable a plain template is used. Everything sent to the AI and every draft goes through `hub/redact.py` when it is on.
- **dev.to**, official API only (`https://dev.to/api`, headers `api-key` and `Accept: application/vnd.forem.api-v1+json`):
  `GET /users/me` (Test), `GET /articles/me/all?per_page=100` (own posts incl. drafts with views/reactions/comments), `POST /articles`
  `{article:{title, body_markdown, published:false, tags(<=4), description, series?}}`, `PUT /articles/{id}`, `GET /comments?a_id=`.
- **Publishing is separate.** "Save as dev.to draft" always sends `published:false`. Publishing is its own route that needs `{"confirm": true}`, only works on a post that
  was saved to dev.to first, and is never called by drafting, saving, the scheduler or the weekly reminder (tests assert this).
- **LinkedIn / X / Hashnode / Medium:** text to copy plus a plain link that opens the site. Nothing is posted for you. See "LinkedIn" below.

## Secrets
The dev.to API key lives ONLY in the OS keyring (service `redline-devto`, name `api_key`; the Gemini key uses the same store). It is never written to `hub.json`, never
logged or audited, and no route returns it (`/status` says `connected` yes/no; error text from dev.to has the key stripped). Set it in Posts > Settings or `PUT /api/devto/key`.

## Config (`hub.json`; edited from Posts > Settings through `PATCH /api/devto/settings`)
| Key | Default | Meaning |
|---|---|---|
| `devto_enabled` | `true` | routes, tools and screen |
| `devto_cadence_enabled` | `false` | weekly "no post for N days, ideas ready" reminder (one Friday batch event, at most once per 7 days) |
| `devto_cadence_days` | `7` | days without a post before the reminder (1-90) |
| `devto_stats_enabled` | `false` | scheduled read of views/reactions/comments from dev.to |
| `devto_stats_hours` | `24` | hours between those checks (1-168) |
| `devto_blocklist_words` | `[]` | words that must never appear in a post (up to 50, 2-60 characters each) |

## Blocklist (always on)
A draft whose title, text or description contains a listed word (whole word, any case) is flagged (`blocked: [...]` in the draft), cannot be saved to dev.to, published or
turned into a pack (`422`), and a generated draft containing one is not kept. Source notes (memories, specs, commit subjects) that contain a listed word, or the markers
private / confidential / company / internal / proprietary / nda / "do not publish", are never read into ideas or drafts. The UI warns: *Do not publish company confidential material.*
Setting changes are audited by key name only (a blocklist can hold company names).

## Routes
- `GET /api/devto/status` -> `{connected, drafts, ideas, checked_at, days_since_post, new_comments, warning}`
- `PUT /api/devto/key {key}` / `DELETE /api/devto/key` -> `{connected}`; `POST /api/devto/test` -> `{ok, username}`
- `GET|PATCH /api/devto/settings` (keys above, plus `connected`)
- `GET /api/devto/ideas`; `POST /api/devto/ideas/refresh` -> `{ideas[{id,title,angle,source}], via: ai|template, sources}`
- `GET /api/devto/drafts`; `POST /api/devto/drafts {topic | idea_id | title+body}` -> draft (saved locally only); `GET|PATCH|DELETE /api/devto/drafts/{id}`
  Draft: `{id,title,body,tags,description,series,status: local|saved|published,devto_id,devto_url,generated_by: ai|template|manual,blocked[],words}`
- `POST /api/devto/drafts/{id}/save` -> create (or update) the dev.to article as an UNPUBLISHED draft
- `POST /api/devto/drafts/{id}/publish {confirm: true}` -> `PUT /articles/{id} {article:{published:true}}`; `400` without `confirm:true`, `409` if not saved on dev.to yet
- `GET /api/devto/drafts/{id}/pack` -> `{linkedin{text,chars,open}, x_thread{tweets[],open}, hashnode_medium{markdown,open_hashnode,open_medium}, note, warning}`
- `GET /api/devto/polish` -> `{from_profile, items[{id: headline|about|featured|skills, label, current, status, tip, suggestion}], note}`
  (reads `profile.get_profile(ctx)` when that module exists; works without it)
- `GET /api/devto/published` (cached) / `POST /api/devto/refresh` (live) -> `{articles[{id,title,url,published,published_at,views,reactions,comments}], comments[], checked_at}`
- `POST /api/devto/comments/ack` clears the "new" flags.

## Comments are untrusted
Comments are other people's text. Each stored comment has `untrusted: true`, HTML stripped, 500 characters max; the screen shows them with `textContent`. They never go into a
notification (only the post title and a count do), and `devto_list` is an untrusted tool for the taint gate: after it runs, changing tools (`devto_save_draft`, `devto_publish`) are refused for the rest of the turn.
The first check only records the comments that already exist (nothing is "new" yet); later checks report new ones, and a post whose comment count did not change costs no request.

## Scheduler and notifications
`start(ctx)` runs a 30-minute tick that does two things only, both off by default: the stats/comment check and the posting reminder. Both submit events through
`proactive.submit_or_notify` as batch types `devto_comment` and `devto_cadence`, so Friday delivers one sentence ("Chief, no post for 10 days, ideas ready." / "Chief, new comment on your post X.").
Neither can publish.

## Assistant tools (`TOOL_GROUPS` entry `devto`)
`devto_ideas`, `devto_draft {topic}`, `devto_list`, `devto_save_draft {id}`, `devto_publish {id}` (approval card, level N2, shows the title and the start of the text, refuses unsaved or blocked drafts;
Full Control auto-grants N2 like every other N2 tool), `post_pack {id}`. Jarvis writes the technical content; Friday owns reminders. Only `devto_publish` needs approval; `devto_save_draft` and `devto_publish` are mutating (refused after untrusted text was read).

## LinkedIn (honest note)
There is no LinkedIn automation here and none is planned: LinkedIn's terms forbid automating posting from a personal profile, and its official posting API is for approved partner apps.
So the pack is **draft-and-paste**: copy the text, tap "Open LinkedIn composer" (`https://www.linkedin.com/feed/`, no pre-filled parameters because none are documented), paste, post. Same for X, Hashnode and Medium.

## UNVERIFIED (never run against the real services)
- The exact dev.to response shapes (`page_views_count`, `public_reactions_count`, `comments_count`, `published_at`, the comment tree `id_code`/`body_html`/`user`/`children`); the code reads them with fallbacks.
- That `PUT /articles/{id}` with only `{article:{published:true}}` publishes without resending the body (a partial update); if dev.to wants the full body, Publish needs the body added.
- `GET /users/me` as the Test call, and `per_page=100` on `/articles/me/all`.
- The keyring on the real laptop, the phone UI (never opened in a browser), real AI draft quality.
