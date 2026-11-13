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
  `notifications_recent`, `search_files` (Ask my files), `search_everything`
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
