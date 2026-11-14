# Presence-aware delivery (`hub/presence.py`)

The hub works out where you are and delivers each notification the right way. All routes need login. Config (in `hub.json`, not in the Settings allow-list): `presence_enabled` (default true) and `presence_laptop_toasts` (default false).

## States
`at_laptop` (laptop input within 3 min) | `near` (idle 3-15 min) | `away` (idle over 15 min, or locked) | `phone_active` (phone app open, screen on, laptop not in use) | `in_meeting` (a timed event happening now in the hub's calendar feeds) | `focus` (manual) | `quiet_hours` (the proactive quiet window) | `unknown` (no usable signal).
Order of precedence: focus, manual away/back override, meeting, quiet hours, then the sensors. A change between `near`, `away`, `phone_active` and `unknown` must hold 60 s (no flapping); entering `at_laptop`, `focus`, `in_meeting` or `quiet_hours` is immediate. Sampled every 15 s.

## Routes
- `GET /api/presence` -> `{enabled, paused, state, since, signals{laptop_idle_s, locked, phone{visible,screen_on,age_s,active,app_open}, meeting, focus, override, quiet_hours}, focus{on,until,minutes_left}, held, override}`
- `POST /api/presence/phone` `{visible: bool, screen_on?: bool}` -> `{ok, state}`. The app sends it on visibility change and every 30 s while visible; 75 s of silence = app closed. 400 on any other shape.
- `POST /api/presence/focus` `{minutes: 1-720}` or `{off: true}` (or `{minutes: "off"}`). Ending focus releases held items.
- `POST /api/presence/override` `{state: "away"|"back"|"clear", minutes?}`. `away` lasts 8 h by default, `back` pins `at_laptop` for 30 min. Both expire.
- `GET|PUT /api/presence/rules`, `POST /api/presence/rules/reset`: the delivery rules (below). Bad rows are dropped.
- `GET|PUT /api/presence/phrases`: the voice phrases `{away, back, focus, focus_off}` (lists of phrases, editable).
- `GET /api/presence/pending`, `POST /api/presence/pending/ack {ids}`: short spoken lines for the open phone app (speak-phone).

## Delivery rules
A rule is `{id, urgency, state, actions, enabled}`. `urgency`: `critical` (alert priority 3, a VIP, or priority 3), `urgent` (any event the proactive rules push at once), `normal` (batched kinds). `state`: any state or `*`. A critical item with no critical rule follows the urgent rules. No matching rule = the old behaviour.
Actions: `silent-list` (in-app list only), `toast-on-laptop` (in-app list, plus a Windows toast when `presence_laptop_toasts`), `push-silent` (low-priority phone push), `push-phone`, `speak-phone` (only if the app is open), `speak-laptop`, `hold-until-free`, `batch` (the usual one-sentence batch).
Defaults: urgent at laptop = toast + silent push; away or on phone = push + speak; in meeting or focus = hold. Critical in a meeting = silent push; in focus = push. Normal at laptop = list/toast only; near/away = batch; meeting/focus = hold.
Held items are released as ONE digest ("You missed 3 things in the meeting") when the state ends.

## Unchanged behaviour
Presence is bypassed (the old direct delivery) when it is off, the state is `unknown` or `quiet_hours`, no rule matches, the owner paused the rules, or a panic is active (`hub.panic.is_active`/`active`/`panic_active`, when that module exists).

## Voice
"Jarvis, I am going out", "I'm back", "focus for an hour", "focus off" (phrases editable). Handled before any model call; the sentence must be short and clear.

## Toast safety
The toast is a PowerShell script (`-NoProfile`) that reads the TITLE from stdin; no text is ever in the command line, and the body is never shown.
