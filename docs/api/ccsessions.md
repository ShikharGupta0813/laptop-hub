# Claude Code sessions API

`hub/ccsessions.py` (module `ccsessions`), tools `cc_sessions`, `cc_session_read`, `cc_session_send`, screen `js/ccsessions.js` + `css/ccsessions.css` (Apps > Claude).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. `404` while `ccsessions_enabled` is false.

## Config (`hub.json`)
| Key | Default | Meaning |
|---|---|---|
| `ccsessions_enabled` | `true` | routes, tools and the waiting watcher |
| `ccsessions_dir` | `""` | transcript folder; empty = `Path.home()/.claude/projects` (Windows: `%USERPROFILE%\.claude\projects`) |

## Reading (read-only)
Transcripts are `<dir>/<project-dir>/<session-id>.jsonl`. Only the last 256 KB of a file is read; a cut first line, a partial last line, non-JSON lines and unknown shapes are skipped, never raised.
- `GET /api/ccsessions` -> `{enabled, dir_exists, sessions[]}`, newest first, at most 50. A session: `id, project, project_name, modified, age_seconds, status, permission_pending, last_user (<=200), last_assistant (<=400), messages, messages_approximate, tokens {input, output} | null`.
- `GET /api/ccsessions/{id}` -> the same plus `turns[]` (last 10, `{role, text <=600, tools[], at}`).
- Status: `working` (file changed < 60 s ago, or the newest turn is a user/tool-result turn less than 10 min old), `waiting` (newest turn is Claude's and nothing is newer; `permission_pending` when that turn ends in an unanswered tool call), `idle` (> 30 min). `messages` is counted in the tail, so `messages_approximate` is true for big files.
- Everything from a transcript is UNTRUSTED text: the three tools are `untrusted` (reading taints the turn, see assistant-safety.md) and the UI uses `textContent` only.

## Steering (the only write)
- `POST /api/ccsessions/{id}/send {message, confirm: true}` -> `{job, status}`. Runs `claude --resume <id> -p <message> --output-format json` headless in the session's project folder: argv list, no shell, binary via `shutil.which("claude")`, `ANTHROPIC_API_KEY` removed from the environment (the owner's own login is used), 600 s timeout, background thread.
- Refused with 409 and a plain reason when: the session is `working` (two writers on one transcript corrupt it), another send to it is still running, the message is empty or over 4000 characters, the project folder is gone, `claude` is not on PATH.
- `GET /api/ccsessions/jobs/{job}` -> `{id, session, project, status running|done|failed, started, finished, result (<=2000), error}`. Jobs live in memory (last 50).
- Tool `cc_session_send(id, message)`: `needs_approval` and `mutating`, standard ExtraTool rules (taint gate applies, also in Full Control). The approval card shows the project, the session's last message and the exact text.

## Proactive
Every 60 s, only while enabled and the folder exists, a session `waiting` for more than 5 minutes sends ONE `cc_waiting` batch event ("Claude is waiting on you in <project>") through `hub/proactive.py`, once per waiting episode (it re-arms when the session leaves `waiting`).

## Tests and limits
`tests/test_ccsessions.py` (fake transcripts only). UNVERIFIED: the real transcript line shapes (assumed: `type`, `message.role`, `message.content` string or blocks with `text`/`tool_use`/`tool_result`, `message.usage`, `cwd`, `isMeta`, `isSidechain`), that an unanswered `tool_use` really means a permission prompt, `claude --resume -p` on Windows (including `.cmd` shims and argument quoting), and the screen in a real browser.

## Adapters: which coding agents are watched (an owner-editable list)
`<data>/ccsessions_adapters.json`; UI: Claude screen > "Agents watched" (`js/provlists.js`). `claude-code` (the transcripts above, `ccsessions_dir` still applies when its folder is blank) is the ready-made entry; it can be switched off, edited or deleted.
| kind | status | notes |
|---|---|---|
| `claude-code` | ready-made | settings `dir` (blank = `ccsessions_dir` / `~/.claude/projects`) and `resume_argv` (the steering template, see below). Another folder (WSL, another machine's share) can be added as a second entry; its session ids are `<adapter-id>_<id>` |
| `codex-cli` | EXPERIMENTAL, read-only | reads `~/.codex/sessions/**/rollout-*.jsonl` (setting `dir`). Parses `{type: response_item, payload: {type: message, role, content: [{input_text/output_text}]}}` and the older flat `{type: message, role, content}`; skips `<environment_context>` rows; the working folder comes from the first `session_meta` line. If a file has no readable messages it falls back to raw lines. Written from the format as understood today; Codex may change it |
| `generic-log` | read-only | any text or JSONL log file or folder (`path`, `patterns` default `*.log, *.jsonl, *.txt`). Tails the last 256 KB, a JSON line shows its text/message/content field, `role`/`type` user|assistant are honoured. No symlinks followed, depth 4, 2000 files, status is only `working` (written < 60 s ago) or `idle` |
A Gemini CLI preset points `generic-log` at `~/.gemini/tmp` with a raw tail: its own file layout is NOT parsed (not sure of the format), so it is shown as plain log lines. Rows carry `adapter, adapter_label, kind, experimental, steerable, read_only, native_id` (and `last_line` for logs).
**Steering** (`POST /{id}/send`, the `cc_session_send` tool) works ONLY for a `claude-code` adapter whose `resume_argv` template is a list of 2-12 words with `{session}` (default `["claude","--resume","{session}","-p","--output-format","json"]`): the program name is resolved with `which`, shells (`bash`, `cmd`, `powershell`, `sudo`, `env`...) are refused as the program, only the `{session}` placeholder is allowed, the message goes in on standard input, `confirm: true` or the approval card is required, and `shell=False`. Everything else answers 409 "read-only". An empty `resume_argv` turns steering off for that adapter. The waiting notification stays Claude Code only.
- `GET /api/ccsessions/adapters` -> `{entries[{..., exists, experimental, steerable}], kinds, presets, note}`; `POST /adapters {kind, label?, settings}`; `PUT /adapters/{id}`; `PUT /adapters/order {ids}`; `DELETE /adapters/{id}`. (`GET /api/ccsessions` is unchanged.)
- Log text is UNTRUSTED like transcripts (the tools stay `untrusted`). The owner chooses what to point `generic-log` at; it is read only and never written.
Tests: `tests/test_ccadapters.py`. UNVERIFIED: the real Codex rollout shape on the owner's version, and Gemini CLI's files (deliberately not parsed).
