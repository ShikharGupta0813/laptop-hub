# Panic (hub/panic.py)

One tap stops the agent everywhere and keeps it stopped until the owner taps **Resume**. Routes need the login cookie.

## Routes
| Route | What |
|---|---|
| `GET /api/panic` | `{active, at, source, paused:[{name, detail, state}], errors:[{name, error}], full_control_was_on, confirm, phrases}` |
| `POST /api/panic` `{source?}` | Panic now. Login **or** header `X-Panic-Token` (below). Never throws; one failing hook is listed in `errors` and the rest still run. A second call while active returns the same report with `already: true` and runs nothing. |
| `POST /api/panic/resume` | Clears `panic_active` and restarts only what Panic actually stopped (login required, a token is not enough). Returns `{active:false, resumed:[names]}`. |
| `GET /api/panic/arm` | `{token, expires_in: 300}`: a 5 minute HMAC token (keyed on the session secret) that can trigger Panic, and only Panic, without the cookie. Does not need the assistant to be healthy. |

## What Panic does
1. Sets the persistent flag `panic_active` (config, saved to hub.json), first, so every gate is already shut.
2. Turns Full Control **off**. It stays off after Resume.
3. `AssistantService.panic_stop_all()`: stops every running turn, denies pending approvals, cancels N3 countdowns, revokes remembered approvals.
4. Runs every registered hook (below), each with a 10 s timeout: trust rules revoked; wake word, dictation, MCP servers, form-filling session, council runs, work-lane runs, deep-thinking jobs and model downloads, research jobs stopped; proactive pushes, autopilot, opportunities finder, alerts pollers and build flow paused.
5. Audit `panic_activated` (`source`, `stopped`, `errors`, `full_control_was_on`); a push with source `panic`.

While `panic_active` is true: the assistant refuses every tool that is gated, mutating or outbound (`tool_result.blocked == "panic"`, audit `assistant_panic_block`); read-only (N1) tools still work; every background engine's `enabled` reads False (`panic.on(config)`); the phone push (ntfy) is held back except the panic confirmation; the start/send routes of applyq, formfill, opps, council, lanes, models (think) and research answer 423. Settings are not changed, so Resume needs no bookkeeping.

## Hooks
`panic.register(name, stop_fn, resume_fn=None, ctx=ctx)`. `stop_fn()` (sync or async) returns a short description of what it stopped, or `None` when nothing was running (not listed, not resumed). `resume_fn(state)` gets that return value back. `panic.register_flag(ctx, name, config_key, default)` is the one-liner for engines whose `enabled` already reads `panic.on(config)`. Registering a name again replaces it. Hooks live on `ctx.panic_hooks`.

## Voice and typed phrase
Before any greeting, fast command or model, `assistant.run` checks `panic.matches_phrase(text, config)`. The WHOLE message must be a phrase (optionally with "hey", "Jarvis", "please", "now" around it): "Jarvis stop everything", "panic", "emergency stop", "pause everything", "kill everything". "Don't panic about the bug" does nothing. Edit the list with `panic_phrases` (Settings API; replaces the built-in list).

## Config (append-only)
`panic_active` (set only by the Panic and Resume routes, not by Settings), `panic_confirm` (default false: a single tap; true adds "are you sure"), `panic_phrases` (null = built-in).

## UI
Red Panic button in the header (`js/panic.js`, `css/panic.css`), a "Panic" row in Settings > Agent controls and a Home quick action. After a tap: a 10 second toast with **Undo** (resumes). While active: the "Agent paused" banner lists what was paused, with **Resume**. Android: nothing.

Tests: `tests/test_agent_controls.py`.
