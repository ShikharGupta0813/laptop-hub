# Work lanes API

`hub/lanes.py` (store, escalation, routes) and `hub/lanes_tools.py` (assistant tools). Routine work goes to the **free Council
or a local model first**; Gemini and Claude are used only when that fails or the owner asks. Needs the login cookie; every POST
also needs `X-Hub: 1`. Errors are `{"detail": "one plain sentence"}`. No `hub.json` key was added: the lists live in the data
folder (`lanes.json`, runs in `lanes_runs.json`, the latest 100).

## The lists (owner-editable, Settings > Work lanes)
**Lane** `{id, label, kind, enabled, cost, daily_cap, notes, escalate_claude}`
- `kind`: `council` | `local` | `local-reasoning` (deep thinking, hub/models.py) | `gemini` | `claude-login` | `claude` | `cli:<id>` | `endpoint:<id>`.
- `cost`: `free` | `limited` | `paid`. `daily_cap`: 0 = none; the lane is skipped for the day once `usage.py` counts that many calls
  (key: council, local, gemini, claude-login, claude, cli, endpoint). The caps of `docs/api/privacy-usage.md` still apply.
- `escalate_claude` (council kind only) lets that lane ask Claude through the Council; the Council itself still refuses unless `council_allow_claude` is true.
- Ready-made: `council` (free), `local` (free), `local-reasoning` (free), `gemini` (limited), `claude-login` (paid), plus one automatic entry
  (`auto: true`, id `cli-<id>`) for every switched-on CLI login provider. Editing an automatic entry pins it as the owner's own.

**Class** `{id, label, lanes: [lane ids, best first], private, notes}`. Ready-made: `routine` council, local, gemini; `research`
council, gemini, claude-login; `coding` council, claude-login; `hard-reasoning` local-reasoning, gemini, claude-login; `private`
council, local (`private: true`). Deleting a lane removes it from every class; the last class cannot be deleted.

## Escalation policy
A run takes the first lane that can start and moves to the next ONLY when (a) the lane failed, timed out or was unavailable (not set up,
switched off, no key, busy), (b) the Council's own verdict was not `accepted` with an answer (verify failed, `stopped`, no answer, timeout)
or (c) the owner asks for a stronger model (`POST /runs/{id}/stronger`). A lane that gave an answer is never second-guessed. Every
attempt records `status` (`accepted|failed|skipped|running`), `why` and, after the first, `escalated_because`. A lane over its
daily cap is skipped with a note. An explicit `lane` never escalates by itself. An owner cancel ends the run.

**Private** (class `private: true`): only `council` and `local`/`local-reasoning` kinds may serve it, whatever the list says. The Council is
started with `--only=<its local models>` (from `lab models --json`, `kind == "local"`); none switched on means the council lane is skipped.
Cloud lanes are skipped with "Private work stays on the laptop"; an explicit cloud lane is refused (`409`). When nothing on the laptop
produced an answer the run ends `needs_owner` and says so; `stronger` finds no cloud lane to go to.

**Saved calls**: a run counts as saved when a free lane produced the accepted answer. `GET /saved` counts the last 7 days.

## Routes (`/api/lanes`)
- `GET ""` -> `{lanes: [... + used_today, why_not, private_safe, usage_key], classes, kinds, costs, default_class, council: {ready, why}, saved}`
- `POST ""` add a lane, `PUT /{id}` change fields (`id` never changes), `DELETE /{id}` (`409` for an automatic CLI entry: switch the provider off instead). All return the whole overview.
- `POST /classes`, `PUT /classes/{id}`, `DELETE /classes/{id}`: same for classes (`lanes` must name existing lanes). `POST /reset` restores the ready-made lists.
- `POST /plan` `{class?, lane?}` -> `{class, private, first, lanes: [{lane, label, cost, why_not}], skipped}` (what would go first; used by the approval card).
- `POST /run` `{task (<=4000), class? (default routine), lane? ("auto" or an id), verify? (one line, Council lanes only), link? {kind:"brain", id}}`.
  Returns the run at once when a Council lane goes first (`running: true`, `council_job_id`, polled in the background and visible in the Council screen);
  a quick lane (local, Gemini, Claude, CLI, endpoint) answers before the response returns (`status: done`, `answer`, `lane`, `saved`). `400` bad input,
  `404` unknown class or lane, `409` explicit lane not allowed for a private class.
- `GET /runs` (summaries), `GET /runs/{id}` (`attempts`, `answer`, `files`, `council: {status, elapsed_s, log_tail}` while running, `linked`).
- `POST /runs/{id}/stronger` re-opens a finished run on the lanes of its class it has not tried yet (`409` still running / none left / private).
- `POST /runs/{id}/cancel` stops the Council run too.
- `GET /saved` -> `{week, today, by_lane, runs_week, paid_or_limited_week, meaning}`.
- `link`: when the run ends the hub adds a note to that Brain item ("Council result: ..." or "... could not finish"); `linked` is `yes` or the reason.
  The Brain's **Send to Council** button uses it.
Status: `running | done | failed | cancelled | interrupted | needs_owner`. A hub restart turns running runs into `interrupted`.
The audit log gets `lanes_run`, `lanes_done`, `lanes_stronger`, `lanes_cancel`, `lanes_edit` with ids and lengths, never the task text.

## Assistant tools
| tool | asks the owner? | what |
|---|---|---|
| `run_in_lane(task, class?, lane?, verify_command?)` | yes (card: class, the lane that goes first, its cost, "stays on the laptop", the lanes used only if it fails) | starts a run |
| `lanes_status(run_id?)` | no | lanes, caps, classes, saved calls, recent runs; or one run in full |
Both results are marked untrusted (a model's answer is data). Group `lanes` in `TOOL_GROUPS` ("give this to the council", "send this to the council", "routine work", "save my tokens", "stronger model", "private work"). Jarvis and Friday prompts say routine and private work goes to a free lane first.

## UI
`js/lanepicker.js`: `lanePicker()` chip row (Auto | Council | Local | Gemini ...), `runInLane`, `askLane`, `sendToCouncil`, `councilButton`, `savedTile`.
Used by the Talk composer (a non-Auto lane hands the message to that lane instead of the chat), Brain task cards and Today task rows
("Send to Council"), Build flow ("Run this step with"; Council keeps the old tick-the-box flow, another lane shows a one-off answer),
the Applications draft editor ("Write draft with: Template / Council / AI"), the Council screen ("Saved calls this week") and Settings > Work lanes.
