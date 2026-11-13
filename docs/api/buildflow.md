# Build flow API (spec-first delegation)

`hub/buildflow.py` (engine, store, routes), `hub/buildflow_tools.py` (assistant tools), screen `js/buildflow.js` + `css/buildflow.css` (Apps > Build).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. "Jarvis, build me X" -> one question at a time -> a spec file -> a checkbox plan -> the Council builds it one step at a time while the owner only approves.

## Config (`hub.json`)
| Key | Default | Meaning | Settings |
|---|---|---|---|
| `buildflow_enabled` | `true` | routes (409 when false), tools, background poll | yes |

## A flow
`{id, title, goal, workspace, stage, answers[], question, ready, spec_path, plan_path, steps[], done, total, next, job_id, summary}` in `buildflow.sqlite` (data dir), at most 50 flows.
`stage`: `brainstorm` -> `spec` -> `plan` -> `build` -> `done`.

- **Workspace**: must be one of the owner's shared roots (`roots` in hub.json): a root name (`docs`), `root/sub/folder`, or an absolute path inside a root. `..` anywhere, a path outside every root, a file, a missing folder and symlinks that leave the root are refused (400). Stored resolved.
- **Brainstorm**: the configured free AI (Gemini or local, never the paid Claude API) asks ONE question at a time, 2-3 suggested answers each, at most 6; it may say "ready" after the first answer. At the cap the flow is ready without another AI call.
- **Spec**: `docs/specs/YYYY-MM-DD-<slug>.md` inside the workspace (Goal, Scope, Non-goals, Approach, Acceptance checks, Risks). Never overwrites: `-2`, `-3` ... (exclusive create).
- **Plan**: `docs/plans/<same stem>.md`, numbered checkbox steps `- [ ] 1. text` each followed by `  - Check: one line`. Never overwrites. **The file is the truth**: steps are parsed back on every look (any `- [ ]`/`- [x]` line, `*` bullets and `1)` numbers work, max 30), so the owner can edit it by hand; a ticked box is progress.
- **Build**: the next unchecked step goes to the Council (`POST /api/council/run`, text = step + check + spec/plan/workspace paths) as a background job. When the run is `accepted` the box is ticked in the file; on any other end the box stays and the reason is kept with the step (`failure`). A background poll (20 s) and every look settle finished runs. Only one step runs at a time (the Council itself allows one run).
- **Finish**: when every box is ticked (also if the owner ticks the last one by hand): once, a Brain item `kind=decision` ("Built: <title>", tags `buildflow`) if `brain_enabled`, and a proactive batch event `build_done` "Build finished: <title> (N steps)".

## Routes
- `GET /api/buildflow` -> `{enabled, max_questions, flows[]}`; `GET /api/buildflow/workspaces` -> `{workspaces:[{name, path}]}`.
- `POST /api/buildflow {goal, workspace, title?}` -> the flow with its first `question {question, options[]}`.
- `GET /api/buildflow/{id}` and `/status` -> the flow (settles a finished council run first).
- `POST /api/buildflow/{id}/answer {answer}` -> next question, or `ready: true`.
- `POST /api/buildflow/{id}/spec {preview?}` and `/plan {preview?}`: `preview: true` drafts and returns `{path, first_lines (20), total_lines, written: false}` and writes nothing; without it the drafted text is written (same draft, one AI call).
- `POST /api/buildflow/{id}/next {preview?}`: preview returns the step; otherwise starts the Council run -> the flow with `job_id`.
- `DELETE /api/buildflow/{id}`: forgets the flow; the files stay.

## Assistant tools
`build_start`, `build_answer`, `build_status`, `build_list` run freely (they only touch the flow's own database). `build_spec`, `build_plan`, `build_next` are `mutating` and need the owner's approval (the card shows the file path and its first 20 lines, or the exact step); they are refused on a turn that read outside text (taint gate); Full Control skips the card as for every approval tool. One `TOOL_GROUPS` entry: `build me | build a | new project | spec | plan the | next step`.

## Safety
Files only ever go inside a shared root, never overwrite, and are created by the hub (the Council is told not to tick the plan). The Council text contains paths, not file contents. Failure reasons are one line, 200 characters.
