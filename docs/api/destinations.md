# Posts and publishing destinations API

`hub/destinations.py` (the destination list and one isolated adapter per kind), `hub/posts.py` (ideas, drafts, variants, publishing, stats, reminders, routes, assistant tools),
`hub/devto.py` (the OLD `/api/devto/*` routes as thin shims), screen `js/posts.js` + `css/posts.css` (Apps > Posts; the route id stays `devto`, `js/devto.js` is a one-line re-export),
state `posts.json` and `destinations.json` in the data folder. Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. `404` while `devto_enabled` is false.

## The idea
A **destination** is a place a post can go: `{id, kind, label, enabled, mode, settings, format}` plus a secret kept in the OS keyring. The owner adds, edits, switches off or deletes them in
Posts > Destinations. dev.to is one ready-made entry. ONE draft goes to MANY destinations; each gets its own variant, and publishing is always a separate explicit action per destination.

| kind | mode | drafts | stats | comments | secret | settings |
|---|---|---|---|---|---|---|
| `devto` | api | yes | yes | yes | API key | none |
| `hashnode` | api | yes (create once) | yes | yes | personal access token | `publication_id` |
| `wordpress` | api | yes (`status=draft`) | yes | yes | application password | `url`, `username` |
| `ghost` | api | yes | yes | no | Admin API key `id:secret` | `url` |
| `markdown-file` | file | yes (`draft: true` file) | no | no | none | `folder` |
| `webhook` | api | no (publish sends) | no | no | optional bearer token | `url` |
| `manual` | manual | no | no | no | none | `url` (composer link) |

`manual` is how LinkedIn, Medium (no public write API), X, Reddit and anything else are added by name and URL with no code: the hub makes the text, you copy it, open the composer link, paste,
and tap **I posted**. Nothing is sent. `webhook` lets ANY site or automation (Zapier, n8n, a GitHub Action, your own server) receive `{title, body_markdown, tags, canonical_url}` as JSON when you publish.

### Starter list (Reset to starter list)
dev.to, Hashnode, WordPress, Ghost, Medium (manual), LinkedIn (manual), X (manual, thread of numbered parts <= 280 chars), Reddit (manual), My blog (webhook). Reset removes destinations you added
(and their saved keys); keys of starter entries stay.

### Per-destination formatting rules (`format`, all editable)
`style` (`article` | `short` | `thread`), `max_length` (0 = no limit), `hashtags` (bool), `tag_limit` (0 = no tags), `title_length`, `template` (short/thread text; placeholders
`{title} {hook} {points} {cta} {link} {hashtags}`, empty = built-in). Defaults per kind: dev.to tags 4, Hashnode 5, WordPress/Ghost/file/webhook 10; LinkedIn short, 3000 chars, 5 hashtags; X thread, 280,
2 hashtags; Reddit/Medium article. An article over its `max_length` is refused (`422`) before anything is sent. The `{cta}` link is a PUBLISHED url of the draft (never a private draft url) or a
`[add the link after you publish]` placeholder.

## Routes
Destinations
- `GET /api/destinations` -> `{destinations[{id,kind,kind_label,label,enabled,mode,settings,format,connected,has_secret,needs_secret,drafts,publishes,stats,comments}], kinds[...], warning}`
- `POST /api/destinations {kind, label, enabled?, settings?, format?, secret?}`; `GET|PATCH|DELETE /api/destinations/{id}` (PATCH: `label, enabled, settings, format, secret, clear_secret`)
- `POST /api/destinations/{id}/test` -> `{ok, name, note?}` (webhook and manual send nothing); `POST /api/destinations/reset` -> `{destinations}`

Posts
- `GET /api/posts/status`; `GET|PATCH /api/posts/settings` (`devto_*` config keys, names kept: `devto_enabled`, `devto_cadence_enabled/_days`, `devto_stats_enabled/_hours`, `devto_blocklist_words`)
- `GET /api/posts/ideas`, `POST /api/posts/ideas/refresh`
- `GET|POST /api/posts/drafts {topic | idea_id | title+body}`; `GET|PATCH|DELETE /api/posts/drafts/{id}` (PATCH: `title, body, tags, description, series, canonical_url`)
  Draft: `{id,title,body,tags,description,series,canonical_url,generated_by,status: local|saved|published,targets{dest_id:{status: saved|published|posted, remote_id, url, at, published_at}},blocked[],words}`
- `GET /api/posts/drafts/{id}/variants?destinations=a,b` -> `{variants[{destination,label,kind,style,title,body,text,parts[],tags,chars,over_limit,max_length,open,target}]}` (no network)
- `POST /api/posts/drafts/{id}/prepare {destinations[]}` -> per destination `{ok, saved, variant, error}`: a variant for each, and an UNPUBLISHED draft where the kind has drafts. Never publishes; one failure does not stop the rest.
- `POST /api/posts/drafts/{id}/destinations/{dest}/save` (draft only), `GET .../check` (what publishing would do), `POST .../publish {confirm: true}`, `POST .../posted {url?}` (manual 'I posted')
- `GET /api/posts/polish`; `GET /api/posts/published`, `POST /api/posts/refresh`, `POST /api/posts/comments/ack`

Old routes: every `/api/devto/*` route still works (same shapes, dev.to destination only; the draft view keeps `status`, `devto_id`, `devto_url`).

## Publishing is separate (N2)
`Posts.publish` is the ONLY caller of an adapter's publish (`Destinations.publish_remote`). It needs `{"confirm": true}`, works on ONE destination, needs a saved draft first on kinds that have drafts, and is
never reached from drafting, saving, preparing, the scheduler or the reminder (tests assert it by patching and by reading the source). The assistant tool is `post_publish(id, destination)`: an approval card
every time, and `post_publish` is in `safety.ALWAYS_ASK` so Full Control, auto-approve and remembered approvals never skip it. A manual destination cannot be published for you (`400`).

## Secrets
Kept only in the OS keyring (service `redline-posts`, name `dest-<id>`), never in `hub.json`, `destinations.json`, `posts.json`, logs or audit, and no route returns them (`has_secret` yes/no). Error text from a
service has the secret (and derived tokens: the basic-auth string, the Ghost JWT) stripped. Audit lines carry names and ids only.

## Migration
The first time the destination list is read it is seeded with the starter list. If the old dev.to key exists (`redline-devto/api_key`) it is COPIED to the dev.to destination (the old entry is left alone).
`devto.json` is converted once into `posts.json`: each draft's `status/devto_id/devto_url` becomes `targets.devto`; stats, comments seen and the baseline flag get a `dest`. Config keys `devto_*` are unchanged.

## Stats, comments, cadence
For each ENABLED, connected destination that supports it (dev.to, Hashnode, WordPress, Ghost) `POST /api/posts/refresh` (and the optional scheduled check) reads own posts with counts, and comments where available
(dev.to, Hashnode, WordPress). One destination failing is reported in `errors` and does not stop the others. Comments are untrusted: `untrusted: true`, HTML stripped, 500 chars, shown with `textContent`, never put
in a notification (only the post title and a count), and `post_list` is an untrusted tool for the taint gate. Each destination's first check only records what is already there. Events are
`post_comment` and `post_cadence` (batch types); `proactive.py` maps the old `devto_comment`/`devto_cadence` names to them, so both keep working. A post marked posted on a manual destination counts for the reminder.

## Assistant tools (`TOOL_GROUPS["posts"]`)
`post_destinations`, `post_ideas`, `post_draft {topic}`, `post_list`, `post_save_draft {id, destination}` (id or name), `post_publish {id, destination}` (approval, N2, ALWAYS_ASK, preview shows the destination, title
and first words and refuses unsaved/manual/blocked), `post_pack {id, destination?}` (copy text; nothing is posted).

## Blocklist (always on)
A draft containing a `devto_blocklist_words` word (whole word, any case) cannot be saved to ANY destination, published, prepared or turned into a variant (`422`); a generated draft with one is not kept; source notes with
one (or marked private/confidential/company/internal/proprietary/nda) are never read. *Do not publish company confidential material.*

## UNVERIFIED (written from public docs, never run against the real services or a real browser)
- **Hashnode**: GraphQL shapes (`createDraft`/`CreateDraftInput` fields `title, contentMarkdown, publicationId, tags[{slug,name}], originalArticleURL`; `publishDraft(input:{draftId})`; the `publication(id:){posts}` stats query with
  `views/reactionCount/responseCount`; `post(id:){comments}`), the bare-token `Authorization` header, and that a created draft cannot be updated (re-saving is refused; edit it on Hashnode).
- **Ghost**: the Admin API JWT (`kid`, `aud: /admin/`, 5 minute expiry), `?source=html`, `updated_at` handling on PUT, `Accept-Version: v5.0`; no comment API is used.
- **WordPress**: Application Password basic auth, `status=draft|publish`, tags are not sent (WordPress needs tag ids), `/comments` for the whole site; needs the REST API reachable.
- **dev.to**: real response shapes, `PUT {published:true}` as a partial update, `/users/me`.
- The markdown-file front matter fits Jekyll/Hugo/GitHub Pages in general, not a specific theme; the screen on a phone (390px, source-checked only), the real keyring, AI draft quality.
