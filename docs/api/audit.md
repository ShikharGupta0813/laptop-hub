# Audit search (hub/auditview.py)

`GET /api/audit/search?since=&until=&kind=&level=&outcome=&q=&limit=&before=&format=` (login required) over the existing audit log (JSON lines written by `server.audit`). Reads from the end, skips damaged lines (`skipped` counts them), never reads more than 16 MB back.

- `since`, `until`: `2026-11-13`, `2026-11-13T10:30`, `today` or `7d`. A bare `until` date means the whole day.
- `kind`: comma list of `tool, approval, denial, n3, outbound, mcp, panic, trust, setting, login, redaction, other`. `level`: `N1,N2,N3`. `outcome`: `ran, approved, denied, blocked, asked, cancelled, failed, info`. `q`: substring, matched on the already-redacted text.
- `limit` 1..500 (5000 with `format=csv`), `before` = the `next_before` of the previous page (a byte offset). Newest first.
- Rows: `{pos, time, event, kind, level, outcome, by, summary, detail}`. `by` says who approved: tap, voice, trust rule, full control, remembered approval, auto-approve, typed CONFIRM.
- Secrets never leave: keys like token, secret, password, api_key, cookie, authorization, topic are `[hidden]`; secret-shaped text (bearer tokens, `sk-` keys, long hex, JWTs) is scrubbed before truncation; a `redaction` event shows its counts only. A secret cannot be found by searching either.
- `format=csv`: header `time,kind,event,level,outcome,approved_by,summary,detail`; cells starting with `= + - @` get a leading quote. Audited as `audit_export`.

UI: `js/audit.js` + `css/audit.css`, "What did the agent do?" in Settings > Agent controls and a Home quick action: day, kind, level, outcome filters, search, tap for details, Export CSV. Tests: `tests/test_agent_controls.py`.
