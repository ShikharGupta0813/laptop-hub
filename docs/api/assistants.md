# Assistants (the list of voices)

**What this page is for:** the routes and rules behind Settings > Assistants. Jarvis and Friday are the two ready-made entries; the owner can rename them, add more (for example "Atlas" for study), switch one off or delete one. At least one stays on.

Code: `hub/assistants.py` (list, validation, routes), `hub/persona.py` (routing by name, prompts, greeting), `hub/wakeword.py` and `js/wakephrase.js` (wake matching), `js/assistants.js` (browser cache). Screen: `js/ownerlists.js` (built on the shared list editor `js/listeditor.js`).

## The entry

| Field | Meaning |
|---|---|
| `id` | 2 to 24 characters, `a-z 0-9 - _`, starts with a letter. Never changes. Brain items, sessions and the wake word use it. `jarvis` and `friday` are the ready-made ids. |
| `name` | Display name (up to 40 characters). Its first word also wakes the assistant. |
| `wake_names` | Words that wake it ("Hey atlas"). One word each (2 to 29 characters). Empty = the first word of `name`. |
| `heard_as` | How the microphone often mis-hears the name (`jarves`, `jarvas` for Jarvis). |
| `role_prompt` | Empty = the built-in prompt (Jarvis, Friday) or a one-line plain prompt (new assistants). Up to 1500 characters. `{name}` and `{call}` (the owner's call sign) are filled in. |
| `voice` | `{rate 0.5-2, pitch 0-2, voice ""}` for the phone speaker (voice mode applies it; the defaults change nothing). |
| `tool_groups` | Names from `TOOL_GROUPS` (hub/assistant.py) this assistant owns. Their tools are always offered to the local model on that assistant's turns and named in its prompt. **Empty = no focus**, which is how Jarvis and Friday ship, so their behaviour is unchanged. |
| `accent` | `cyan`, `amber`, `green`, `violet` or `white` (the app's accent tokens). |
| `enabled` | Off = it no longer wakes or routes. |

Rules: unique ids; no wake word used by two enabled assistants; words too common to wake on (`the`, `at`, `evening`...) are refused; at most 8 assistants; unknown tool groups are refused.

## Defaults and fallback
With no file, or a broken one, the two defaults apply and everything behaves as before the list existed: same routing ("Jarvis, ..." / "run it, Jarvis"), same prompts (word for word), same greetings. The older `jarvis_name` setting still renames an untouched Jarvis. The list is stored in `assistants.json` in the hub data folder; nothing is written until the owner edits.

## Routes (login needed)
- `GET /api/assistants` -> `{assistants[] (each also has builtin, default_prompt, words), tool_groups[], accents[], max, customised}`
- `POST /api/assistants {id, name, wake_names?, heard_as?, role_prompt?, voice?, tool_groups?, accent?, enabled?}` -> the same view
- `PATCH /api/assistants/{id}` any field except `id` (voice merges) -> the view
- `DELETE /api/assistants/{id}` -> the view; 400 when it is the last one
- `POST /api/assistants/reset` -> back to Jarvis and Friday

Errors are 400 with one plain sentence. Every change is audited (`assistant_add`, `assistant_update`, `assistant_delete`, `assistants_reset`; ids only).

## Who reads the list
- `POST /api/assistant/message` and the assistant service: name routing, `persona` validation, the prompt, the greeting, tool focus.
- Browser wake word (`js/wake.js` via `js/wakephrase.js` `matchWake(text, {assistants})`) and voice mode name routing (`splitName(text, jarvisName, assistants)`): they fetch `GET /api/assistants` (cached 5 minutes, refreshed after Settings edits) and fall back to the old Jarvis/Friday constants until it answers.
- Laptop wake word (`wakeword.match_wake(text, jarvis_name, assistants)`).
- Brain: a memory kind's default persona and the persona filter accept any assistant id (docs/api/brain.md).

## Limits (honest)
- The always-on Android background listener (docs/api/wakeword.md, `mobile/`) learns the enabled assistants' names when the app is opened (and every 15 minutes while it is open); open the app once after renaming or adding one. It needs the newer APK, and an uncommon name may be heard less reliably than Jarvis or Friday.
- Voice speed, pitch and voice name use the phone's speech engine, which may ignore them.
- Brain items keep the persona id they were saved with; deleting an assistant leaves its items readable.
