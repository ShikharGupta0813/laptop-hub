# Global search and Home (`hub/search.py`)

The router prefix is `/api` because Home is `/api/home`. All routes need the login cookie.

## Search

- `GET /api/search?q=&kinds=&limit=` and the identical `GET /api/search/all?q=&kinds=&limit=`
  → `{query, results:[{title, snippet, kind, open:{view?,section?,id?}, score}], groups:{kind:count}, total, timed_out:[source]}`
  - `q` 2..200 characters after trimming, else `400`. `limit` 1..1000, default 30, capped at 100. `kinds` = comma list of result kinds or source names (e.g. `note,cards`);
    sources not asked for are not called.
  - **Important**: `server.py` already owns `GET /api/search?root=&q=` (file-name search) and its route is registered first, so a request without `root` never reaches this module.
    Either delete that route from `server.py` (this module serves it: with `root` given, `GET /api/search` runs exactly the same file search) or let the UI call `/api/search/all`.
  - Fan-out: all sources run at once, 2.5 s each; a slow, broken or missing (404) source is left out (slow ones are named in `timed_out`).
    Each source is tried at its routes in order until one exists:
    `kb` /api/kb/search · `fileops` /api/fileops/search · `reminders` /api/reminders/search · `todos` /api/todos/search (then /api/productivity/todos/search) ·
    `journal` /api/journal/search (then /api/productivity/journal/search) · `bookmarks` /api/bookmarks/search (then /api/life/bookmarks/search) ·
    `snippets` /api/snippets/search · `expenses` /api/expenses/search (then /api/life/expenses/search) · `cards` · `automations` · `inbox`
    (each `GET <route>?q=` → `{results:[{title,snippet,kind,open}]}`; at most 20 are used per source).
  - Built in: `note` (title, tags, body; `open {view:"notes", id}`), `app` (names in `apps`; `open {view:"apps", section:"launch", id:name}`),
    `button` (scripts, by label or name; `open {view:"apps", section:"buttons", id:name}`), `setting` (`open {view:"settings", section}` with section in
    models, folders, notifications, backup, audit, diagnostics, about).
  - Results from features are cleaned: title 200, snippet 300 characters, `kind` must be `[a-z][a-z0-9_-]{0,29}` (else the source's default), `open` keeps only `view`, `section`, `id`.
  - Ranking (`score`): exact title 100, prefix 80, word prefix 65, substring 50, all words 40, fuzzy 20-35, snippet-only 12, anything a feature returned at least 10,
    plus up to 3 for the feature's own order. Ties keep source order, so the order is stable. Duplicates (same kind and `open`, or same kind and title) are merged. `groups` counts all matches, `results` is cut at `limit`.
- `GET /api/search/suggest?q=` → `{recent:[up to 10 earlier queries, filtered by prefix], suggestions:[up to 6 titles of settings pages, apps and buttons starting with q]}`
- `POST /api/search/recent` `{q}` → `{recent:[...]}` (newest first, no duplicates, max 10; `400` if q is not 2..200). Kept in `<data_dir>/search_recent.json`.
- `DELETE /api/search/recent` → `{recent:[]}`.

## Home

`GET /api/home[?model=]` (`model` is accepted and ignored: nothing here calls a model) → always `200`:

```
{greeting:{period:"morning|afternoon|evening|night", name?}, now:"YYYY-MM-DDTHH:MM:SS",
 laptop:{online:true, battery:{percent,plugged}|null, cpu, mem},
 agenda:[{title,start,end,all_day,link,platform}] (max 8),
 tasks:[{title,due|null,source:"asana"|"todo",overdue,open}] (5, soonest due first),
 reminders:[{id,title,when,open}] (next 5),
 notifications:{unread, latest:[{id,title,body,level,created,read}] (3)},
 streaks:{learn,english,habits,focus,journal}  (numbers or null),
 summaries:{productivity, life, cards}  (the feature's /summary reduced to plain values, or null),
 jarvis:{running}, recent_notes:[{id,title,created,tags,open}] (3),
 suggestions:[up to 4 strings], sections_failed:[names]}
```

- Each section is built on its own and cached for 20 s (a failed one is not cached, so it is retried next time). A section that cannot be built
  (feature missing, error, over 3 s) shows an empty value and is named in `sections_failed`: `laptop, agenda, tasks, reminders, notifications, streaks, summaries, jarvis, recent_notes`.
  `tasks` fails only if both Asana and the local to-dos fail; `streaks` and `summaries` only if every source fails.
- Routes read (all `GET`, in process): `/api/status`, `/api/work/status`, `/api/work/calendar?days=1`, `/api/work/asana/tasks?filter=all`, `/api/todos?done=false` (fallback `/api/productivity/todos`),
  `/api/reminders` (a list, or a dict whose first list is used; title from `title|text|message|name`, time from `when|due|due_at|at|fire_at|remind_at|next_run|time`),
  `/api/notifications` (fallback `/api/notify`; `{notifications,unread}`), `/api/learn/history` and `/api/english/overview` (`streak_days`), `/api/focus/stats` (`streak_days`),
  `/api/productivity/summary` (`habits_done_today`, `habits_total`, `overdue`, `best_streak`, `journal_streak`), `/api/journal/stats?days=1` (today's entry count),
  `/api/life/summary`, `/api/cards/summary` (`due_today`), `/api/jarvis/status`; notes come from the note store directly.
- `suggestions` are rule-based, in this order, at most 4: a meeting starting within an hour; overdue tasks; flashcards due; habits not checked today; no journal entry today; unread notifications.

- Results are tidied before ranking: raw ISO times in a title or snippet become friendly ones ("Today, 5:00 pm", "Wed 18 Nov, 5:00 pm"), and a note file's `--- key: value ---` header is removed from snippets. File-meaning results (`/api/kb/search`) must share a word (3+ letters) with the query or they are dropped, so a nonsense query returns no results; other sources are not filtered.
