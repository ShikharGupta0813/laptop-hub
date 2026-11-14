# Trust ladder (hub/trust.py)

After the owner approves the same kind of N2 action many times with no denial, REDLINE offers "Do this automatically from now on". Accepting creates a **trust rule**. State is in `<data>/trust.json` (only the shape: tool, program(s), folder; never the arguments).

## The ladder
- Every approved N2 call (a real tap, not a Stop or a timeout) is counted by `safety.arg_shape` (tool + program(s) + folder, or action id, or argument names).
- Suggested when the streak is at least `trust_threshold` (default 10, 3 to 100), there was no denial of that shape in the last 30 days, no active rule exists and it was not dismissed in the last 30 days. A push (source `trust`) at most once per day, batching all suggestions; none while Panic is active.
- A rule: tool + exact shape (so "the same program(s) in the same folder"), expiry (default 30 days, max 365, renewable), daily cap (default 20), use counts.

## Never eligible
N3 calls; any tool in `safety.ALWAYS_ASK` or whose name has the word send, publish, submit, apply, post, email, tweet or upload; tools marked `always_ask`; every MCP (dynamic) tool; any tainted turn; anything while Full Control or Panic is on; everything when `trust_enabled` is false.

## Revoked at once by
A denial of that shape; any Panic; any Full Control change on or off (`AssistantService._sync_full_control` and the Settings route); the owner (Revoke, which also stops suggesting it for 30 days); Revoke all. Each revoke is audited (`trust_rule_revoked` with `reason`). Creating: `trust_rule_created`; extending: `trust_rule_extended`.

## Auto-run
In `_call_tool` a matching rule means no approval card; the `assistant_tool` audit line carries `trust_rule: <id>`, `by: "trust_rule"`, `auto: true`. Over the daily cap the owner is asked as usual.

## Routes (login required, prefix `/api/trust`)
| Route | |
|---|---|
| `GET /` | `{enabled, threshold, suggestions:[{id, label, approvals, scope, tool}], rules:[{id, label, state active/expired/revoked, expires_in_days, daily_cap, uses, uses_today, last_used, extended, revoked}]}` |
| `POST /suggestions/{id}/accept` `{days?, daily_cap?}` | creates a rule (409 if not on the list) |
| `POST /suggestions/{id}/dismiss` | |
| `POST /rules/{id}/revoke`, `POST /rules/{id}/extend` `{days?}`, `POST /revoke-all` | |

Config (append-only, in the Settings allow-list): `trust_enabled` (true), `trust_threshold` (10). UI: Settings > Agent controls > Trust rules (`js/trust.js`). Tests: `tests/test_agent_controls.py`.
