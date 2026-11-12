# Council API

`hub/council.py` (routes, jobs) and `hub/council_tools.py` (assistant tools). Council runs the **multi-llm-lab** project
(`python -m lab council ...`: several models, local Ollama, Gemini and free hosted ones, working together on one task) as a
background side job, so the owner can use the laptop's models without spending Claude. Needs the login cookie; every
POST also needs `X-Hub: 1`. Errors are `{"detail": "one plain sentence"}`.

Off until `council_dir` in hub.json points at the lab checkout (`python -m hub setup --only council` does it). While it is
empty every route except `GET /status` answers `409` with "Council is not set up ... Run: python -m hub setup --only council".

## Config (hub.json, edit by hand; none of these can be changed from the phone)
| key | default | meaning |
|---|---|---|
| `council_dir` | `""` | the multi-llm-lab folder; it must contain a `lab` folder. Empty = feature off |
| `council_python` | `""` | interpreter; empty = `<council_dir>/.venv` (`Scripts/python.exe` on Windows) if present, else the hub's own |
| `council_timeout_min` | `90` | hard wall-clock limit for one run (minutes, clamped to 1 s .. 24 h) |
| `council_allow_claude` | `false` | only when true may a run use `--escalate=claude`, may Claude models be switched on or listed in `only`, and the hub's `ANTHROPIC_API_KEY` is passed to the child |

## How a run is started (the contract with the lab)
```
<python> -m lab council --json [--verify=CMD] [--workers N] [--rounds N] [--max-calls N]
         [--only=a,b] [--skip=c] [--escalate=claude] [--resume=RUN_DIR --notes -] -- TASK
```
cwd is `council_dir`. It is an argument list, never a shell. The task goes after `--`; the verify command is the single value
`--verify=CMD`; model names are checked against `^[A-Za-z0-9._:-]{1,64}$` and joined with commas (no name can contain one).
The owner's notes (rerun) travel on **stdin** (`--notes -`), never argv. stdout is ONE JSON object
`{status: accepted|failed|stopped, answer, files, calls: {model: n}, stopped, run_dir}`; stderr is progress. The hub parses the
last JSON object line, trims it (answer 20 000 chars, 50 files, 30 call counters) and treats all of it as data.

Rules: one run at a time (`409` if one is going), a hard timeout (the process tree is killed, status `timeout`), the job
record is kept in `<data dir>/council/jobs/<id>.json` (the latest 50), so a hub restart keeps finished results. A run that was
still going when the hub stopped becomes `interrupted` (the hub kills its children on a clean shutdown). When a run ends the
phone gets a notification (`ctx.notify("Council <status>", first 200 chars of the answer or error, link="council")`), except
for a cancel or a hub shutdown. Notes text is never stored; only its length. The audit log gets `council_run` /
`council_cancel` / `council_model` with lengths and flags, never the task or notes text.

Job `status`: `running | accepted | failed | stopped | cancelled | timeout | error | interrupted`.

## Routes (`/api/council`)
- `GET /status` -> `{enabled, ready, folder, python_found, allow_claude, timeout_min, running_job, setup_command, limits: {task, verify, notes}}`.
  `folder` is the folder's name only. `ready` = `lab` folder found.
- `POST /run` `{task, verify_command?, workers? (1-8), rounds? (1-10), max_calls? (1-500), only?: [name], skip?: [name], escalate_claude?: bool, resume_job_id?, notes?}`
  -> the new job (returns at once). `task` max 4000 chars, `verify_command` max 500 and one line, `notes` max 8000 and only with `resume_job_id`.
  A rerun (`resume_job_id`: a finished job that left a real `run_dir`) defaults task and verify to the earlier run's and passes
  `--resume=<run_dir>`; the run folder is looked up by the hub, the caller can never name a path.
  `400` bad input (also `escalate_claude`/Claude in `only` while `council_allow_claude` is false), `404` unknown resume job, `409` busy / not set up / no run folder.
- `GET /jobs` -> `{jobs: [summary, newest first]}`; a summary has `{id, status, running, task (160 chars), verify_command, options, created, finished, elapsed_s, resume_of, notes_chars, error, run_dir, has_result, answer_preview}`.
- `GET /jobs/{id}` -> the summary with the whole `task`, `log_tail` (last 4000 chars of stderr, ANSI codes removed) and `result: {status, answer, files, calls, stopped, run_dir}`. `404` for an unknown or malformed id.
- `POST /jobs/{id}/cancel` -> the job (`cancelled`). `409` if it is not running. The process tree is killed.
- `GET /jobs/{id}/review-packet` -> `{packet, truncated, chars}`. Runs `<python> -m lab review-packet -- <run_dir>` and returns its markdown, capped at 60 000 characters. The phone's **Copy review packet** button copies it so the owner can paste it into Claude themselves; the hub never calls Claude. `409` no run folder, `502` the lab could not make one, `504` too slow.
- `GET /models` -> `{models: [{name, provider, model, kind, cost, enabled, ready, reason, paid, claude, locked?, note?}]}` from `<python> -m lab models --json`. Claude entries are `locked` with a note while `council_allow_claude` is false.
- `POST /models/{name}` `{enabled: bool}` -> `{models: [...]}`. Runs `<python> -m lab models --enable=NAME` or `--disable=NAME`. `name` must match the pattern (`400`) and be one the lab listed (`404`); enabling a Claude model while not allowed is `409`.

## Assistant tools
| tool | asks the owner? | what |
|---|---|---|
| `council_run(task, verify_command?, workers?, rounds?, max_calls?, only?, skip?, notes?, resume_job_id?, escalate_claude?)` | yes (card shows the exact task, notes length, limits) | starts the background run, returns the job id |
| `council_status(job_id?)` | no | progress or result (newest run when no id) |
| `council_cancel(job_id)` | yes | kills a running run |

The approval preview refuses early (without asking) when the feature is off or a run is already going. The model never gives a folder; a
rerun is `resume_job_id` from `council_status`. Council output is returned as data, not instructions.

## Phone
Apps, Council (`js/council.js`, `css/council.css`): task box, optional verify command, optional "use only these models", Start; a
live log while it runs (polls every 3 s while the view is open) with Cancel; the result with the answer, files and per-model call
counts; **Copy review packet**, **Claude's notes** and **Rerun with notes**; earlier runs; a **Models** list with a switch per model,
a free/paid badge and why a model is not ready. Not set up: a card with `python -m hub setup --only council`.

## Unverified against the real lab
The lab was not merged when this was written. Assumed and covered only by fakes: `--json` output shape, `--resume`, `--notes -`
(stdin), `--only=`/`--skip=` (comma separated), `review-packet`, `models --json`, `models --enable=NAME` / `--disable=NAME`.
