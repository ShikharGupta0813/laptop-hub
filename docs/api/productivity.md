# Productivity API

Personal todos, a focus (pomodoro) timer, habits and a journal. Module `hub/productivity.py`.
All routes need the login cookie (`401` otherwise). Errors are `{"detail": "one plain sentence"}`.
Request bodies must come from the app (`X-Hub: 1` header on POST/PATCH/DELETE).

Storage: one SQLite file, `<data>/productivity.sqlite` (WAL, created on first use, versioned by
`meta.schema_version`). A damaged file, or one written by a newer hub, gives `503` with a sentence
and is never overwritten.

Dates are the laptop's **local** dates. Ids are opaque strings with a prefix (`t_` task, `h_` habit,
`j_` journal entry, `f_` focus session, `s_` subtask); a malformed or unknown id is `404`.

AI features take `model: "local" | "claude"` (default `"local"`; anything else is `422`). A model
that is down or answers nonsense gives `503` with a sentence. Task and journal text reaches the model
only inside a `<data>` block (forged `<data>` tags in the text are defused) and the system prompt says
it is data, not instructions. AI calls are audited (`todo_breakdown`, `journal_reflect`) with the model
name and counts, never the text. Choosing `claude` sends that text to Anthropic; that is the user's
explicit choice in the request.

Audit events for deletions: `todo_delete`, `todo_list_delete`, `habit_delete`, `journal_delete` (ids only).

---

## Todos `/api/todos`

Task: `{id, title, notes, list, due, priority, tags[], done, done_at, repeat, subtasks[], created, order}`

- `due`: `null`, `"2026-11-04"` or `"2026-11-04T17:30"` (local time).
- `priority`: `0` none, `1` low, `2` medium, `3` urgent.
- `tags`: lower-case words (a leading `#` is dropped, spaces become `-`), at most 10.
- `repeat`: `none | daily | weekly | monthly`. A repeating task must have a due date.
- `subtasks`: `[{id, text, done}]`, at most 30. In a request each item may be a plain string.
- `order`: manual position (lower is first).
- Limits: title 200 chars, notes 5000, subtask 200.

### Lists
- `GET /lists` -> `{lists:[{name, open, total, default}]}`. `Inbox` and `Today` always exist.
- `POST /lists` `{name}` -> `{name}` · `409` duplicate (case-insensitive) or more than 50 lists.
- `PATCH /lists/{name}` `{name}` -> `{name}` (tasks follow) · `409` for the built-in lists.
- `DELETE /lists/{name}` -> `{deleted, moved_to_inbox}` (its tasks move to Inbox) · `409` for the built-in lists.

### Tasks
- `GET ?list=&due=&done=&tag=&q=` -> `{tasks:[Task]}` (max 500), open tasks first, then by `order`.
  `due` is `today | overdue | week | none` (`week` = today through the next 6 days, `overdue` = open tasks
  before today). `done=true|false`. `tag` has no `#`. `q` searches title and notes.
- `POST` `{title, notes?, list? ("Inbox"), due?, priority?, tags?, repeat?, subtasks?}` -> Task
  · `422` for a bad field or an unknown list.
- `GET /{id}` -> Task · `404`.
- `PATCH /{id}` any of the create fields (send `"due": null` to clear it) -> Task.
- `DELETE /{id}` -> `{deleted}`.
- `POST /{id}/toggle` optional body `{done: bool}` (omit to flip) -> `{task, spawned}`.
  Completing a repeating task creates the next copy and returns it as `spawned`: due date one step later
  (a daily/weekly/monthly step from the old due date, repeated until it is not in the past; time of day
  kept), steps reset to not done. Undoing and redoing never creates a second copy.
- `POST /{id}/subtasks/{sid}/toggle` -> Task.
- `POST /reorder` `{ids:[...]}` -> `{ok}`. The listed tasks take the listed order among the positions they
  already occupy, so other tasks stay put (works for a filtered view). `404` unknown id, `422` repeats.
- `GET /smart` -> `{today, overdue, upcoming, no_date, week, total_open}` (open tasks only; by date).
- `POST /quick` `{text, list?}` -> Task. Rule-based parsing of `buy milk tomorrow !2 #errands`:
  `#tag`; `!1`-`!3` (3 = urgent, must stand alone); `today`, `tonight`, `tomorrow`, `day after tomorrow`,
  weekday names (`friday` = the next one after today, `next friday` = that day in next week), `in 3 days`,
  `in 2 weeks`, `in a month`, `next week` (Monday), `next month`, `on 2026-12-01`, `nov 20`, `20th dec`
  (past dates roll to next year); times `at 5pm`, `5:30pm`, `17:30` (a time alone means today);
  `daily`, `weekly`, `monthly`, `every day/week/month`, `every monday`. Matched words are removed from the
  title; if nothing is left the original text is the title.
- `POST /{id}/breakdown` `{model?}` -> `{task, added:[subtask]}`. The model splits the task into steps;
  at most 8 are added, after the existing ones, duplicates skipped. `409` when the task is full.
- `GET /search?q=` -> `{results:[{title, snippet, kind:"todo", open:{view:"notes", section:"todos", id}}]}` (max 20).

---

## Focus `/api/focus`

Session: `{id, label, task_id, state, planned_minutes, break_minutes, started_ts, started_at, ends_ts, ends_at,
elapsed_sec, remaining_sec, ended_at, actual_minutes}`; `state` is `running | completed | stopped`.
`*_ts` are epoch seconds (use them for countdowns); `*_at` are local ISO times.

- `POST /start` `{minutes? (1-240, 25), break_minutes? (0-60, 5), label? (80), task_id?}` -> Session.
  With a `task_id` and no label the task title is the label. `409` if one is already running (only one at a
  time, even for simultaneous requests), `404` unknown task.
- `POST /stop` -> Session. Early stop records the elapsed time (`stopped`). If the time was already up it is
  `completed`. `409` when nothing is running.
- `GET /current` -> `{running, session|null, now_ts, just_finished|null}`.
- `GET /stats?days=14` (1-365) -> `{days, today_minutes, week_minutes (last 7 days), streak_days,
  by_day:[{date, minutes, sessions}] (oldest first, zeros included), by_label:[{label, minutes, sessions}]}`.
  Sessions count on the day they started. `streak_days` counts consecutive days with at least a minute of
  focus; today without focus yet does not break it.
- `GET /history?limit=20` -> `{sessions:[Session]}` newest first.

The timer is stored in the database, so it survives a hub restart. `start(ctx)` runs a loop (every 3 s)
that marks a session completed when its time is up and calls
`ctx.notify("Focus session finished", "<label>: 25 minutes done. Take a 5-minute break.", source="productivity")`,
exactly once, whether or not any phone is connected. A session that ended while the hub was off is announced
at startup.

---

## Habits `/api/habits`

Habit: `{id, name, icon, color, target, archived, created, order}`. `target` is `"daily"` or a number of days
per week `1`-`6` (`7` is stored as `"daily"`). `color` is a name (`green`) or `#rrggbb`.

- `GET ?include_archived=false` -> `{habits:[Habit]}`.
- `POST` `{name, icon?, color?, target? ("daily")}` -> Habit · `409` past 100 habits.
- `PATCH /{id}` any of `name, icon, color, target, archived` -> Habit.
- `DELETE /{id}?confirm=true` -> `{deleted}`. Without `confirm` it is `409` ("archive instead"): deleting erases
  the history.
- `POST /{id}/check` `{date?}` (default today) toggles that day -> `{habit_id, date, checked, done_today,
  current_streak, best_streak, streak_unit, week_count}`. `422` for a future day, one over 400 days old,
  or a bad date; `409` for an archived habit.
- `GET /overview?days=84&include_archived=false` (7-366) -> `{today, days, habits:[Habit + {current_streak,
  best_streak, streak_unit:"day"|"week", completion (0-100), done_today, week_count, grid:[{date, done}]}]}`.
- `GET /today` -> `{date, habits:[Habit + current_streak, done_today, week_count, week_target, week_met, ...],
  done, total}` (active habits only).

Streaks:
- **Daily** habits: consecutive checked days. If today is not checked yet, a run ending yesterday is still
  alive; if yesterday was also missed the current streak is 0 (the best streak is kept).
- **Weekly-target** habits (target N per week): consecutive calendar weeks (Monday-Sunday) with at least N
  checks; `streak_unit` is `"week"`. The week in progress does not break the streak while N is still
  reachable; once the days left cannot make N it counts as missed.
- `completion`: checks in the window against what the target expects, counted from the habit's first known
  day (creation or its earliest check) so a new habit is not judged on days before it existed.

---

## Journal `/api/journal`

Entry: `{id, date, mood (1-5 or null), text (max 10000), tags[], created}`. Many entries per day (max 50).

- `GET ?month=2026-11` (default this month) -> `{month, entries:[Entry]}` newest day first.
- `POST` `{text, date? (today, never in the future), mood?, tags?}` -> Entry.
- `GET /{id}` · `PATCH /{id}` `{date?, mood?, text?, tags?}` -> Entry.
- `DELETE /{id}?confirm=true` -> `{deleted}`; without `confirm` it is `409` (an entry cannot be recovered).
- `GET /on-this-day` -> `{date, entries:[Entry + {years_ago}]}` (same month and day, earlier years).
- `GET /stats?days=30` (1-365) -> `{days, entries, mood_entries, average_mood|null, distribution:{"1".."5"},
  by_day:[{date, mood (day average)|null, entries}], streak, best_streak}`. `streak` = consecutive days with an
  entry (today not yet written keeps it alive).
- `POST /reflect` `{model?, days? (7, 1-90)}` -> `{days, entries, empty, message, reflection}` where
  `reflection = {summary, themes[], mood_note, suggestion}`. With no entries in the period it answers
  `{empty: true, entries: 0, reflection: null, message: "You have not written any journal entries in the last 7
  days, so there is nothing to reflect on yet."}` without calling a model. At most 12000 characters of entries
  are sent (2000 per entry).
- `GET /search?q=` -> `{results:[{title:"Journal 2026-11-04", snippet, kind:"journal",
  open:{view:"notes", section:"journal", id}}]}` (max 20).

---

## Summary `GET /api/productivity/summary`

For the Home dashboard:
`{todos_due_today, overdue, focus_today_minutes, habits_done_today, habits_total, best_streak, journal_streak}`.
Open tasks only; archived habits are ignored; `best_streak` is the longest *current* habit streak in days (a
weekly habit's streak counts 7 days per week).

## Demo data

With `HUB_DEMO` set, `start(ctx)` seeds once per data file (a flag in `meta`) and never on top of existing
data: 8 tasks across Inbox/Today/Errands/Work (one overdue, one repeating, one done, one with steps),
3 habits with history, 5 journal entries (one a year ago for "on this day") and 6 focus sessions.
