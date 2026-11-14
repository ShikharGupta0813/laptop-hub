# Assistant safety: taint gate, unknown senders, Stop

## Taint gate (prompt-injection defence)
A turn starts clean: the owner's own typed or spoken message is never tainted. As soon as a tool returns
outside text, the turn is **tainted**. For the rest of that turn every tool that changes things (needs
approval, or flagged `mutating`) is refused with "Untrusted content was read this turn; ask the owner to
confirm in a new message." This holds **even in Full Control**. Read-only tools keep working. The next owner
message starts a fresh, clean turn. The tool card shows "Blocked: untrusted content was read"
(`tool_result` carries `blocked: "taint"`); the audit log gets `assistant_taint_block`.

Code: `ExtraTool(untrusted=..., mutating=...)`, `UNTRUSTED_BUILTIN` and the guard in
`AssistantService._call_tool` (hub/assistant.py). Tools are flagged with `mark_tools` in each `build()`.

Untrusted tools (reading their result taints the turn):
- built-in: `read_file`, `search_files`
- Work/mail/chat: `asana_tasks`, `asana_task`, `calendar`, `inbox_unread`, `watch_alerts`,
  `notifications_recent`, `search_documents` (Ask my files), `search_everything`
- laptop: `laptop_action` when the action reads the clipboard, screen, windows or OCR
- Friday/Autopilot/Today: `today_summary`, `friday_brief`, `friday_plan`, `friday_laptop_prep`,
  `autopilot_pending`, `autopilot_cards`
- memory: `recall`, `tasks_list`
- research and skills: `deep_research`, `skills_list`, `skill_search`, `skill_use`
- council: `council_status`
- screen vision: `screen_explain` (also needs approval; see docs/api/vision.md)

Changing tools without an approval card, also refused while tainted (`mutating`): `remember`, `task_intake`,
`task_update`, `autopilot_decide`. Owner-initiated fast commands ("run ...", "remember ...") are the
owner's own message, so they are never blocked.

## Unknown senders (Autopilot)
Mail and chat from a sender not in `known_senders` (optional config list of addresses or names) is only DATA.
The draft model has no tools, the sweep only reads, the card shows an "Unknown sender" badge
(`unknown_sender: true` in `GET /api/autopilot`), and nothing is sent until the owner taps Approve.
Replies go only to the original sender. Tests: `tests/test_taint.py`.

## Stop
`POST /api/assistant/stop` `{session_id}` -> `{ok, stopped}`. Cancels the running generation or tool wait,
denies pending approvals and ends the stream with `{"type":"stopped","message":"Stopped."}`. `stopped` is
false when nothing was running. Talk has the Stop button; voice mode has a Stop button, the orb, and the
spoken words "stop" / "cancel".

## Graded safety levels (N1 / N2 / N3)
Every tool call gets a level from `hub/safety.py` (`classify(tool_name, args)`, a pure function) and the level is
on the `tool_start` / `approval_needed` events and in the audit log (`assistant_tool` rows carry `level`).
An `ExtraTool` may declare `level="N3"` (or a function of the arguments); the default is N2 for a tool that needs
approval or is `mutating`, N1 otherwise. What the arguments show can only raise it.

| Level | Meaning | What happens | Examples |
|---|---|---|---|
| N1 | read-only | runs freely | `list_dir`, `read_file`, `system_status`, `laptop_action battery` |
| N2 | changes something | the normal approval card; automatic in Full Control; can be remembered | `write_file`, `run_command dir`, `git status`, `echo shutdown`, `git push`, `send_email` |
| N3 | CRITICAL: can lose data or take the laptop away | never automatic (not by auto-approve, Full Control or a remembered approval): typed `CONFIRM`, then a 10 s cancellable delay | `shutdown /s /t 0`, `Stop-Computer`, `Remove-Item -Recurse -Force C:\x`, `cmd /c del /s /q`, `rm -rf`, `format c:`, `reg delete`, `git push -f`, `taskkill /f /im x`, `winget install`, `netsh advfirewall ... off`, reading `~/.ssh` |

N3 covers: shutdown/restart/sleep/hibernate/lock-out; format/diskpart/mkfs/dd; any delete command run through
`run_command` (`rm`, `del`, `rd`, `ri`, `Remove-Item`, `find -delete`...) except inside the hub trash
(`~/.laptop-hub/trash/...`); killing processes by name or tree, or system processes; registry writes; anything
touching the hub's own token/settings (`~/.laptop-hub`, `hub.json`) or credential files (`.ssh`, `.aws`, `.env`...);
`git push --force/-f/+ref`, `git reset --hard`, `git clean -f`; disabling Tailscale, the firewall or Defender;
installing or removing software (winget, choco, apt, pip, `npm -g`, msiexec...); encoded PowerShell,
`Invoke-Expression` and pipes into a shell (cannot be read, so critical). The command line is lower-cased, quotes
and `^`/backtick evasions are stripped, program paths and `.exe` are ignored and wrappers (`cmd /c`,
`powershell -c`, `sudo`, `bash -c`, `xargs`, `wsl`...) are looked through. It is deliberately conservative: a
false alarm costs one typed word. Not N3 means N2 (still asks), never "safe". A refused call (credential read,
`~/.laptop-hub` write by the file tool) is refused before any card.

### The critical flow (server-enforced)
1. `approval_needed` carries `critical: true`, `confirm_word: "CONFIRM"`, `delay_seconds`, `command`. The card shows
   a red "Critical action" banner, the exact command, and a box.
2. `POST /api/assistant/approve {session_id, call_id, approve: true, confirm_text: "CONFIRM"}`. Without the exact
   word: 400 and the call keeps waiting. With `voice: true`: 403 and Jarvis says critical actions need the typed
   confirmation on screen (a spoken yes, or a spoken "CONFIRM", never counts; saying no is fine).
3. The stream sends `{"type":"countdown","id","seconds":10,"cancelable":true}` and the call runs only after the
   delay. Approving again meanwhile is 409 and cannot shorten it.
4. `POST /api/assistant/cancel {session_id, call_id}` (the card's Cancel button) stops it (404 when nothing is
   counting). Stop, and a spoken "stop"/"cancel" in voice mode, cancel it too. Shutdown and restart are covered
   the same way.
5. Fast commands ("run ...") in Full Control go through the same gate: an N3 one asks, it is never direct-run.
Audit: `assistant_n3_attempt` (with the command), `assistant_n3_confirmed`, `assistant_n3_cancelled`,
`assistant_n3_refused` (`reason: voice | no_confirm_text`), then the usual `assistant_tool` row with `level: "N3"`.

### Remembered approvals (N2 only)
An N2 card (`can_remember: true`) offers "Allow this for 1 hour" / "for this session"
(`approve` with `remember: "hour" | "session"`). A grant is memory-only, keyed by tool + a normalised argument shape
(`run_command`: the program(s) the line starts + the folder; `write_file`: the folder; `laptop_action`: the action;
others: the argument names), and expires by itself (1 h, or the chat session). N3 is never remembered. The taint
gate still wins. Full Control turning on or off clears every grant.
`GET /api/assistant/permissions` -> `{grants:[{id,tool,label,scope,expires_in}]}`;
`POST /api/assistant/permissions/revoke {id}` or `{all:true}`. Settings > Account and security > Active permissions
lists them with Revoke and Revoke all. Audit: `assistant_grant`, `assistant_grant_revoked`; a call that rode on one
has `remembered: "hour" | "session"`. Tests: `tests/test_safety.py`.

## My safety rules (owner-editable, only ever stricter)
Settings > My safety rules. Extra rules on top of the built-in checks: `{pattern, regex, applies_to: command|tool|path, level: N2|N3, note, enabled}`.
"Treat anything touching D:\\payroll as critical" is one rule: pattern `D:\\payroll`, applies to `path`, level N3.

- A rule can only RAISE a level (`worst()` of the built-in level and every rule that matches); it can never lower one. Levels are N2 (ask) or N3 (typed CONFIRM, then the countdown).
- `command` looks at the command line (and a script name); `path` looks at file arguments (`path`, `src`, `dest`, `cwd`, ...) and also at command lines, since a command names paths; `tool` looks at the tool name.
- Plain patterns are case-insensitive substrings (slash and backslash count the same); `regex: true` makes it a regular expression.
- Validation (`hub/safepattern.py`): at most 200 characters, must compile, no repeat inside a repeat, no repeat over alternatives, no back-references, counted repeats up to 100, and matching runs on at most 4000 characters, so a pattern cannot hang the hub. At most 50 rules. A bad rule is a 400 and nothing is saved.
- When a rule fires, the audit log gets `assistant_custom_rule` with the tool, the level and the rule ids (never the command or path). The N3 card shows the rule's note as the reason. Saving is audited as `safety_rules_update` (counts only).
- Stored in `safety_rules.json` in the hub data folder; `AssistantService._level` reloads it when the file changes. Code: `safety.classify_with_custom`, `safety.apply_custom`, `safety.custom_hits`, `safety.load_custom_rules`, `safety.save_custom_rules` (all new; `classify` and `ALWAYS_ASK` are unchanged).

Routes (login needed): `GET /api/safety/rules` -> `{rules[], applies_to[], levels[], max}`; `PUT /api/safety/rules {rules: [...]}` replaces the list; `POST /api/safety/rules/test {kind: command|path|tool, text, rules?}` -> `{level, built_in, rules[], reason}` (the Settings test box: "type a command to see its level"; `rules` tries an unsaved draft).
