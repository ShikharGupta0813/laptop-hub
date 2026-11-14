# CLI login providers API

`hub/cliprov.py` (module `cliprov`, appended to `FEATURE_MODULES`), shared plumbing `hub/provlist.py`, UI `js/provlists.js` (Settings > Providers > CLI login). Needs the login cookie.
List file `<data>/cli_providers.json`. No secret is stored: this kind uses a program that is already logged in on the laptop. Tests: `tests/test_cliprov.py`.

An owner-editable list; one kind, `cli-login`. Entry settings:
- `argv`: the command as a list (first the program). The user's prompt is NEVER in argv: it is written to the program's stdin (keeps text off the command line and makes Windows `.cmd` shims work). A shell program (sh, bash, cmd, powershell...), a placeholder like `{prompt}`, `& | < > ^ % "` or control characters, and `--api-key*` words are refused (400).
- `env_strip`: environment variable names removed for the child so it uses its own login, never a key (default `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI`).
- `timeout` (5-900 s, default 120; the whole process tree is killed on expiry or cancel), `output` (`text` or `json`), `json_path` (dotted, e.g. `response` or `result.text`, list indexes allowed), `cwd` ("" = a fresh empty temp folder), `login_hint`, `note`.

Ready-made: `gemini-cli` (on) = `gemini -p "<fixed instruction>"` with the prompt on stdin; `agy` (OFF) = placeholder, note "run `agy --help`, then edit the argv template here". The older `claude-login` provider is separate and unchanged.

## Provider name
`cli:<id>`. `server.model_for("cli:<id>")` returns the client inside `redact.Guarded` (private details hidden going out and restored coming back) counted as `cli` in `hub/usage.py` ("CLI login AI"; not in the CLOUD cap group). A switched-off or deleted entry falls back to the local model. The assistant answers `cli:*` with its text turns (never the Claude API path). `default_provider` may be `cli:<id>` (`providers.resolve_default` honours it when the entry is on; Settings accepts it). `/api/status` gains `cli: [{id, label, ready, reason}]` and the model pickers (`js/providers.js`) list the ready entries.

## Routes (prefix `/api/cli-providers`)
- `GET` -> `{entries[], kinds, presets[], note}`; each entry has `status: {state: not-found|not-logged-in|failing|ok|installed, message, installed, failing, ...}`. Detection is `shutil.which` of argv[0]: "CLI not found on PATH". "not logged in: ..." appears after a call whose stderr looks like a login problem (the entry's `login_hint`).
- `POST {kind: "cli-login", label?, settings}`; `PUT /{id}` (label, enabled, settings merged); `PUT /order {ids}`; `DELETE /{id}`; `POST /reset` (back to the two ready-made entries).
- `POST /{id}/test` (works on a switched-off entry too) sends "Reply with the single word: ok" -> `{ok, reply, state, stdout, stderr}` or `{ok: false, error, state, stdout, stderr}`; stdout/stderr are one short line each with key-like text masked.

## Safety
argv list with `shell=False` always; output capped at 256 KB (the program is stopped when it prints more), stderr kept to 16 KB and shown only as a masked summary; POSIX children run in their own process group (killed with `killpg`), Windows uses `taskkill /T /F`.

## UNVERIFIED (no network to a real CLI in the build sandbox)
Google's headless docs say `-p/--prompt` runs non-interactively, piped stdin is read as input, `--output-format json` returns `{response, stats}` and exit codes 0/1/42/53. The exact way stdin combines with `-p` in the owner's installed version, whether Login-with-Google needs `GOOGLE_CLOUD_PROJECT` for Workspace accounts, and the real stderr text for "not logged in" are not verified: edit the argv and press Test. Antigravity (`agy`): its command line is unknown; the entry is a disabled placeholder. Windows `.cmd` shims and `taskkill` were not run on Windows.
