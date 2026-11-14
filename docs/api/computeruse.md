# Computer control

Jarvis looks at the laptop screen, decides ONE next action (a click, some typing, a key press, a scroll) and the hub performs it, over and over, until the task is done or a limit is reached. Code: `hub/computeruse.py`, UI `js/computeruse.js` (Apps > Computer control), tests `tests/test_computeruse.py` (fakes only). Guide: `docs/guide/33-computer-control.md`.

**OFF by default** (`computeruse_enabled`). Windows only for now: the hub must know which app is in front (title and process name) before every step.

## What it will never do
- Start anything the owner has not approved. A job starts only after `POST .../approve` carrying the exact digest of the scope the owner was shown. The assistant can create the pending record; it has no tool, route or token that approves it. Trust rules, remembered approvals and Full Control are never consulted.
- Act in an app you did not allow, or in any window on the never-control list (password managers, banking titles, UAC / `consent.exe`, Windows Security, remote-desktop clients, the Hub itself, terminals; editable). Foreground outside the scope: the job pauses and asks. It checks again right before every action and after every wait for your tap.
- Touch the secure desktop (UAC prompt, lock screen). No window information or an input desktop that cannot be opened means "secure": refused.
- Type into a password field, or into anything whose window title, control name or stated reason looks like a password / PIN / CVV / one-time code. The text never reaches the audit log either.
- Send a screenshot of a password field to a cloud model: UI Automation password rectangles are blacked out first; if they cannot be hidden the picture is not sent and the job pauses. Without `pywinauto` the hub cannot see password fields at all (the window-title and blocklist rules still apply): the UI says "Coordinates only".
- Press Windows-key chords, Alt+F4, Ctrl+Alt+Del, Ctrl+Shift+Esc (unless the owner allow-listed a chord in Settings; Ctrl+Alt+Del and Ctrl+Shift+Esc can never be allow-listed), or type text longer than 200 characters or containing line breaks.
- Delete, send, submit, pay, buy, install, uninstall, format, confirm, reset, change security settings or anything the words below suggest, without a separate tap for THAT step (shown with the screenshot). Those steps never use trust rules, remembered approvals or Full Control.
- Keep driving when the owner grabs the mouse, presses Pause / Stop / Panic, throws the mouse into the top-left corner (pyautogui fail-safe), presses Ctrl+Alt+Shift+Esc (pynput), taps STOP on the always-on-top banner, or when presence says they are away (unless `computeruse_allow_unattended`).
- Run more than one job at a time, more than 100 steps, more than 3600 seconds, or more than 20 actions per minute.
- Keep pictures: they live in memory plus a rolling folder of the last 20, deleted when the job ends (unless `computeruse_keep_shots`). Image bytes are never logged.
- Use a cloud vision model unless the owner picked that provider for the job; the default is a local Ollama vision model and the approval card says plainly when pictures leave the laptop.

## How a job runs
1. `POST /api/computer/jobs` records a PENDING job: `{goal, scope: {apps[], max_steps, allow_typing, provider?, max_seconds?}}`. Apps are process names chosen from the open windows (`GET /api/computer/windows`). The record carries `summary` ("Jarvis may click and type in notepad and explorer for up to 25 steps. ...") and `digest` (SHA-256 of goal + apps + limits + typing + provider). Pending records expire after 10 minutes.
2. `POST /api/computer/jobs/{id}/approve {digest}` starts it. A wrong digest is 400 and starts nothing. Also refused: switched off 403, Panic 423, unattended 423, nothing to drive with 501, another job live 409.
3. The loop (background thread): guard (Panic, enabled, presence, mouse moved, foreground window blocked / outside scope) -> screenshot (longest side 1280) + UI tree names if `pywinauto` is installed -> the vision model proposes ONE action -> validate -> classify -> perform -> wait 0.4 s -> repeat.
4. The model's reply is data: it must be one JSON object `{action, target: {x, y} | {name}, text, key, amount, reason}` with `action` in `click|double_click|right_click|type|key|scroll|move|wait|done|ask_owner`. Anything else is rejected (the model is told why and tries again; three bad replies in a row fail the job). Coordinates are pixels of the picture, rounded and clamped to it (more than half a screen outside = rejected), then scaled to the real screen. Extra fields are ignored. A screen that "tells" Jarvis to do something is data, never an instruction.

### Classification of each step
| step | level | needs |
|---|---|---|
| screenshot, `wait`, `done`, `ask_owner` | N1 | nothing |
| click / type / key / scroll / move in an allowed app | N2 | the job's scope approval counts once |
| the reason text, the target or control name (UI Automation), or the window title contains delete, remove, erase, wipe, format, send, submit, pay, payment, purchase, buy, checkout, confirm, uninstall, install, password, bank, admin, UAC, permanently, security, firewall, defender, registry, overwrite, publish, transfer, authorize, reset, sign out, recycle bin; or the key is Delete | confirm | `POST .../confirm-step {step, confirm: true, digest}`; the phone shows the picture and the exact step. `confirm: false` pauses the job |
| typing when `allow_typing` is false (anything but arrows / Home / End / PageUp / PageDown / Esc), typing into a password-like place, a cloud model facing a password-like control | refused | the model is told; three refusals in a row pause the job with a question |

The step digest binds job, step number, action, target, text, window title and process. If the front window changes while waiting for the tap, the step is cancelled.

## Routes (login required)
- `GET /api/computer/status` (also `GET /api/computer/jobs/status`) -> `{enabled, available{ok, reason, screen, input, windows, uia, uia_note}, panic, presence, unattended_blocked, settings{enabled, allow_unattended, keep_shots, max_steps, blocklist, blocklist_default, allow_chords, defaults}, providers[{id, label, kind, local, cloud}], active, jobs[], hotkey, limits}`.
- `GET /api/computer/windows` -> `{apps[{app, titles[], blocked}]}` (501 off Windows).
- `PUT /api/computer/settings {enabled?, allow_unattended?, keep_shots?, max_steps?, blocklist? (null = defaults), allow_chords?}`; turning it ON needs `confirm: true`; turning it off stops every job.
- `POST /api/computer/jobs`, `GET /api/computer/jobs`, `GET /api/computer/jobs/{id}`.
- `POST /api/computer/jobs/{id}/approve | pause | takeover | resume | stop | confirm-step`, `POST /api/computer/stop-all`.
- `GET /api/computer/shots/{id}/latest` -> the newest picture (PNG or JPEG, `Cache-Control: no-store`), 404 once the job has ended. The next planned action is in the job record (`planned: {action, x, y, img_w, img_h, reason}`) and the phone draws it over the picture.

Job `state`: `pending | running | paused | waiting_confirm | done | stopped | failed | expired`. `paused_kind`: `owner | takeover | owner_moved | refused | ask | guard`. A job left paused for 30 minutes stops; paused time does not count against `max_seconds`.

## Assistant tools (Jarvis, group `computeruse`: "on my screen", "click", "do this in the app for me", "control my pc")
- `computer_run(goal, apps, max_steps?, allow_typing?)`: in `safety.ALWAYS_ASK` (N2, `always_ask`, mutating): asks the owner every time, whatever Full Control, auto-approve, a remembered approval or a trust rule says, and is refused after untrusted content was read in the same turn. It only CREATES the pending record; the owner then approves the exact scope on the Computer screen. Two taps by design. The reply does not contain the digest.
- `computer_status` (untrusted: step text came from the screen) and `computer_stop` (free: stopping is always safe).
`computer_step_confirm` is deliberately NOT an assistant tool: the per-step tap exists only as the route above, so no tool call can ever answer it.

## Config (appended; changed by `PUT /api/computer/settings`, not the Settings allow-list)
`computeruse_enabled` false, `computeruse_allow_unattended` false, `computeruse_keep_shots` false, `computeruse_max_steps` 25 (1..100), `computeruse_blocklist` null (= built-in list; a list replaces it, an empty list blocks nothing), `computeruse_allow_chords` null.

## Drivers and dependencies (all optional, all lazy)
`mss` + `Pillow` (screen), `pyautogui` (mouse and keyboard, fail-safe ON; `pynput` is the fallback for clicks and typing), `pywinauto` (UI Automation: click by control name, see password fields; optional), `tkinter` (the red banner with a STOP button), `pynput` (Ctrl+Alt+Shift+Esc). See `requirements-extra.txt`. Every one is an injectable object in `ComputerUse(...)`, which is how the tests run without a screen.

## Audit
`computer_job_created | approved | paused | resumed | stop | end`, `computer_step` (job, step, action, target description, window title, app, outcome, level, `approved_by`: `scope`, `owner (this step)`, `declined`, `none`), `computer_step_confirm_asked`, `computer_job_digest_mismatch`, `computer_step_digest_mismatch`, `computer_settings`. Typed text is cut to 40 characters and replaced by `(hidden)` for password-like targets; the goal text is logged as a length only.

## Panic and presence
`panic.register("computeruse", ...)` stops every job and blocks creating, approving and resuming until Resume; Resume restarts nothing. Presence (`hub/presence.py`) `away` refuses to start and pauses a running job unless `computeruse_allow_unattended`; an unknown presence does not block.

## UNVERIFIED
Real clicking and typing on Windows, `pywinauto` behaviour (control lookup, `CurrentIsPassword`), the secure-desktop test (`OpenInputDesktop`), the vision models' accuracy at proposing coordinates (a small local model will often miss), the tkinter banner on a real desktop, the global hotkey, the UI in a real browser at 390 px.
