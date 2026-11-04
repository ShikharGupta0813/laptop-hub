# Cards and roadmaps API

Module `hub/cards.py`. Flashcards with spaced repetition under `/api/cards`, learning roadmaps under
`/api/roadmaps`. Every route needs the login; every `POST`/`PUT`/`PATCH`/`DELETE` also needs the `X-Hub: 1` header
(server middleware). Errors are `{"detail": "<one plain sentence>"}`. Body validation errors (wrong type, missing
field) are FastAPI's standard 422 list.

Every AI route takes `"model": "local" | "claude"` (default `"local"`). If the model is unreachable the answer is 503
with the model's own sentence. If its reply is not JSON, the hub asks once more, then answers 503
"The model's answer could not be read (...). Try again or use Claude." If the JSON is readable but unusable (no valid
cards or weeks) the answer is 502.

Data is kept in the hub's data folder: `cards.json` and `roadmaps.json`.

## Scheduling (SM-2 style, no random fuzz)

| Card is... | again | hard | good | easy |
|---|---|---|---|---|
| new / learning | learning, due in 10 min | learning, due in 10 min | review, 1 day | review, 4 days |
| review | lapse: `lapses+1`, ease -0.20, interval x0.5 (min 1), relearning, due in 10 min | interval x1.2, ease -0.15 | interval x ease | interval x ease x1.3, ease +0.15 |
| relearning | relearning, due in 10 min | relearning, due in 10 min | review, keeps the shortened interval | review, interval x1.3 (at least +1), ease +0.15 |

A reviewed interval is always at least 1 day longer than the old one, never more than **365 days**. Ease stays
between 1.3 and 3.5 (new cards start at 2.5). Rounding is half-up. A review card is due from **midnight** of its
due day; a learning/relearning card is due at an exact minute. Intervals use the ease before the answer changes it.
The clock is the laptop's local time.

## Settings

- `GET /api/cards/settings` -> `{new_per_day}`; `PUT /api/cards/settings {new_per_day: 0..200}` (default 10).

## Decks

Deck: `{id, name, description, color, created, total, new, due, suspended}` (`due` = reviews due by end of today).

- `GET /api/cards/decks` -> `{decks: [...]}`
- `POST /api/cards/decks {name (<=80), description?, color? "#rrggbb"}` -> deck (422 "Pick a colour like #3366ff.")
- `GET|PATCH /api/cards/decks/{id}` (`PATCH` takes any of `name`, `description`, `color`)
- `DELETE /api/cards/decks/{id}` with `{"confirm": true}` in the body (or `?confirm=true`); otherwise 409. Deletes the
  deck's cards too. Audited as `cards_deck_delete`. -> `{ok, deleted_cards}`

## Cards

Card: `{id, deck_id, front, back, tags[], created, state, ease, interval_days, due, reps, lapses, last_reviewed,
suspended, front_render, back_render, is_cloze}`. `state` is `new | learning | review | relearning`.

**Cloze:** write `{{c1::answer}}` or `{{c1::answer::hint}}` in `front`. `front_render` hides each as `[...]` (or
`[hint]`); `back_render` shows the full sentence plainly followed by the card's `back` text. A cloze card may have an
empty `back`; any other card needs one. All clozes in a card are hidden together (one card, not one per number). For
a plain card `front_render == front` and `back_render == back`. Text is plain text: show it with `textContent`.

- `GET /api/cards/cards?deck=&q=&tag=&limit=50&offset=0` -> `{total, cards}` (limit max 200, oldest first)
- `POST /api/cards/cards {deck_id, front (<=2000), back (<=4000), tags[]}` -> card. 409 if the deck already has that
  front (ignoring case and spacing).
- `GET|PATCH /api/cards/cards/{id}` (`PATCH`: `front`, `back`, `tags`, `deck_id` to move, `suspended`)
- `DELETE /api/cards/cards/{id}` needs `confirm` like decks (409 otherwise). Audited `cards_delete`.
- `POST /api/cards/decks/{id}/cards/bulk {cards: [{front, back, tags}]}` (max 500) -> `{added, skipped_duplicates,
  skipped_invalid, cards}`. This is how accepted AI proposals are saved. Audited `cards_add`.
- `POST /api/cards/cards/{id}/explain {model}` -> `{explanation}` (<=150 words, plain text; the card is sent to the
  model as data).

## AI card generation

`POST /api/cards/decks/{id}/generate`

```json
{"source": {"type": "note|file|url|text|topic", "id": "", "root": "", "path": "", "url": "", "text": "", "topic": ""},
 "count": 10, "model": "local", "style": "qa|cloze|definition"}
```

`source` is exactly the shape Learn's quiz and chat accept (`hub/study.py` `SourceBody`): files only through the safe
file resolver, web pages only through the SSRF-guarded fetch (public http/https only), text capped. `count` is
clamped to 1-40. The material is sent inside a `<source>` DATA block (a closing tag inside it cannot break out) and
the model is told it is data, never instructions.

Response: `{title, cards: [{front, back, tags, front_render, back_render, is_cloze}], skipped_duplicates, saved: false}`.
Cards whose front matches one already in the deck (or repeats within the reply) are dropped. **Nothing is saved**:
post the cards you accept to `/decks/{id}/cards/bulk`. Style `cloze` keeps only cards that contain a cloze.
Errors: 404 deck/note/file, 400 empty/invalid source or a private address, 422 too little text, 502/503 as above.
Audited `cards_generate` (and `cards_url` with the host only).

## Review queue

- `GET /api/cards/due?deck=&limit=50` (limit max 200) ->
  `{cards: [card + intervals], new_per_day, counts: {due, new, queue}, next_due}`.
  `cards` are the due cards (due time <= now) with up to `new_per_day` minus the new cards already started today,
  spread evenly through them. `intervals` previews the next gap per grade, e.g. `{"again":"10m","hard":"10m",
  "good":"1d","easy":"4d"}`. `next_due` is the next learning card's time when the queue is empty, else null.
  Suspended cards never appear. The new-card allowance is global (also when filtering by deck).
- `POST /api/cards/review {card_id, grade: "again|hard|good|easy"}` -> `{card, next_label, undo_available}`.
  409 if the card is suspended. Early reviews are allowed.
- `POST /api/cards/review/undo` -> `{card, undo_available}`: restores the card and the day's counters (including the
  new-card allowance and streak). Goes back one review per call, up to 50. 409 when there is nothing to undo.

## Stats and Home

- `GET /api/cards/stats` ->
  `{due_today, due_now, new_today, new_introduced_today, reviewed_today, retention_30d, streak_days, forecast, by_deck}`
  - `due_today`: reviews due by the end of today (overdue included). `due_now`: due this minute.
  - `new_today`: new cards still available today (allowance left, capped by what exists).
  - `retention_30d`: percent (one decimal) of review-state answers in the last 30 days that were not "again", or `null`.
  - `streak_days`: consecutive days with a review, ending today or yesterday.
  - `forecast`: 14 entries `{date, due}` from today (overdue counted in today).
  - `by_deck`: deck objects plus `deck_id`.
- `GET /api/cards/summary` -> `{due_today, streak, roadmap_progress: [{id, title, percent}]}` for Home. Up to 5
  roadmaps, unfinished ones first (most recently touched on top); Home can show `roadmap_progress[0]`.

## Search

`GET /api/cards/search?q=` -> `{results: [{title, snippet, kind: "card", open: {view: "learn", section: "cards", id}}]}`
(max 20; matches front, back and tags; `title` is the front render, `snippet` the back render).

## Import and export

`POST /api/cards/import`

```json
{"text": "...", "deck_id": "existing id", "deck_name": "or a new deck's name", "format": "auto|tsv|csv",
 "has_header": null, "dry_run": false}
```

Lines are `front<TAB>back[<TAB>tags]` (Anki's plain-text export works) or CSV (`front,back[,tags]`). Delimiter
`auto` picks tab when tab lines are at least as common as comma lines. A cell may be in double quotes to hold tabs,
commas, quotes (`""`) and line breaks. Leading `#key:value` lines (Anki header), empty lines and a UTF-8 BOM are
ignored. A first row like `front,back` is treated as a header (override with `has_header`). Rows with an empty
front, a missing back (unless the front is a cloze), or a cell over the length limits are skipped and counted, not
truncated. Duplicate fronts (in the file or already in the deck) are skipped. A leading `'` that exports add in
front of `= + - @` is removed again.

Max **5000** cards per import: more answers 413 and imports nothing. Over the 50,000 card total: 409.
Unusable text: 422. `dry_run` returns the counts and a 5-card sample without saving (and without creating the deck).
Response: `{rows, will_add, skipped_duplicates, skipped_invalid, delimiter, dry_run, deck_id, added}`. Audited
`cards_import`.

`GET /api/cards/decks/{id}/export?format=tsv|csv|json` (default tsv) downloads the deck.
- `tsv` / `csv`: columns `front, back, tags` (space separated), no header. **Formula-injection safe:** any cell that
  starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`. File name is the deck name reduced to
  `A-Za-z0-9_-`.
- `json`: `{deck, cards: [full card objects with scheduling state]}`, unmodified.

## Roadmaps (`/api/roadmaps`)

Roadmap: `{id, title, topic, level, hours_per_week, created, updated, weeks: [Week], progress}`.
Week: `{n, title, goals[], resources[], tasks: [{id, text, done}], progress, deck_id?}`. Progress:
`{done, total, percent}`. Task ids look like `t2-3` (week 2, task 3).

- `POST /api/roadmaps/generate {topic (<=200), weeks 1-26 (default 4), hours_per_week 1-80 (default 5),
  level: "beginner|intermediate|advanced", model}` -> the saved roadmap plus `weeks_requested`. `weeks` and
  `hours_per_week` are clamped, not refused. The model's plan is normalised: at most the weeks asked for, goals
  <=8, resources <=8, tasks <=12 (tasks default to the goals), text cut to 200 characters, empty weeks dropped,
  weeks renumbered. Max 100 roadmaps (409). Audited `roadmap_generate`.
- `GET /api/roadmaps` -> `{roadmaps: [{id, title, topic, level, weeks, created, updated, progress}]}` newest first.
- `GET /api/roadmaps/{id}` -> roadmap. `PATCH /api/roadmaps/{id} {title}` renames.
- `DELETE /api/roadmaps/{id}` needs `confirm` (409 otherwise). Audited `roadmap_delete`.
- `POST /api/roadmaps/{id}/tasks/{task_id}/toggle` -> `{task, week_progress, progress}`.
- `POST /api/roadmaps/{id}/weeks/{n}/make-cards {model, count 1-40 (default 10)}` -> `{deck, added, skipped_duplicates}`.
  Writes question-and-answer cards from the week's goals and **saves them** into a deck named
  "<roadmap title> - week n". The deck is created on the first call and reused (new fronts only) on later calls.
  Audited `roadmap_make_cards`.
- `POST /api/roadmaps/{id}/weeks/{n}/regenerate {model, note?}` -> `{week, progress}`. Replaces that week (its tasks
  start undone; the other weeks are untouched and sent to the model as context). `note` (<=300 characters) is an
  optional wish, passed as data. Audited `roadmap_regenerate_week`.

## Demo data

With `HUB_DEMO` set, `start(ctx)` seeds (only when empty): decks "Python basics" and "AI agents" (12 cards, 4 of
them already due, 3 cloze) and the roadmap "Learn AI agents" (4 weeks, some tasks done).
