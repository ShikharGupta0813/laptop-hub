# Privacy redaction and usage budget

Code: `hub/redact.py`, `hub/usage.py`, `hub/privacy.py` (routes), the cap check in `hub/router.py`.
UI: `js/privacy.js` + `css/privacy.css` (Settings "Usage today" card, caps sheet, redaction sheet; a gauge row on Home).

## Redaction
Before text leaves the laptop for a CLOUD model, sensitive tokens are swapped for stable placeholders
(`[EMAIL_1]`, `[PHONE_1]`, `[KEY_1]`, `[CARD_1]`, `[AADHAAR_1]`, `[PAN_1]`, `[IBAN_1]`, `[PASSWORD_1]`, `[NAME_1]`).
The same value always gets the same placeholder within a turn.

- **Where it hooks**: `redact.Guarded` wraps the Gemini, Claude-login, Claude-API and duo clients in `create_app`
  (`hub/server.py`), so every cloud request is covered: assistant, notes, cards, research, autopilot, vision's text,
  the duo's two legs and the local-model failover step to Gemini. The Claude API path in `assistant.py` redacts the
  message list in `_claude_params` (same map). The local Ollama model is never redacted.
- **Coming back**: placeholders in the model's text and in tool-call arguments are restored BEFORE a tool runs and
  before text is shown (streamed text holds back a half-written `[EMAIL_`; inside a `<tool_call>` the original is
  JSON-escaped so the call still parses). The assistant history keeps the real text; redaction happens on the way out.
- **Tool results** are redacted on the way to the model too (they are part of the next prompt). A value already hidden
  this turn is hidden wherever it shows up again, also JSON-escaped (a tool echoing a path or an error).
- **Per turn**: `redact.begin_turn(config)` / `end_turn(audit)` in `AssistantService.run`; the map is dropped in
  `finally`. One-shot calls outside the assistant use a map of their own for that call.
- **Collisions**: a literal `[EMAIL_1]` already in the input is reserved, new numbers skip it, and it is never
  "restored" into something else.
- **Audit**: event `redaction` with `counts` per kind (and provider/session). Never values. `redact_names` changes are
  audited as `{"changed": true}`.
- **Kinds and rules** (conservative): email; phone (+91 formats, bare Indian mobile numbers, other `+` international
  numbers); Aadhaar (4-4-4 with spaces or dashes, or 12 digits with a valid Verhoeff check); PAN `ABCPE1234F`;
  card (13-19 digits, Luhn valid); IBAN (mod-97 valid); keys (`sk-`, `AIza`, `ghp_`, `github_pat_`, `AKIA`, `xox*`, JWTs,
  private-key blocks, and a 20+ character value with letters and digits after `key|token|secret|api key`);
  passwords (the value after `password|passwd|pwd` and `:`, `=` or `is`); names (your `redact_names`, whole words).

### What redaction cannot do
It matches patterns, not meaning. It does NOT hide: names or addresses it was not told about, a secret with no label or
recognisable shape, anything the AI can infer from the rest of the text, the pixels of a screenshot (screen vision
sends the image as it is; only the words around it are redacted), and Council runs (an external program that talks to
the models itself). Treat it as a seat belt, not a vault: pick Local for truly private work.

## Config (`hub.json`, Settings allow-listed)
`redact_enabled` (true), `redact_kinds` (all of `email phone key card aadhaar pan iban password name`),
`redact_names` ([]; up to 50 words of 2-60 characters), `cloud_daily_cap` (0 = off), `provider_daily_caps`
(`{provider: n}`, 0/missing = no cap), `claude_login_5h_soft_cap` (0 = unknown).

## Routes (login required)
- `POST /api/redact/preview` `{text}` -> `{enabled, redacted, counts, total}`. Text up to 20 000 characters (413 over).
  Nothing is stored or audited.
- `GET /api/usage` -> `{day, today: {provider: n}, week: {provider: n}, providers: [{provider, label, today, week, cap,
  remaining}], caps, cloud: {used, cap, remaining}, claude_login_5h?: {calls, soft_cap, remaining, estimate: true}}`.
  Providers: gemini, claude-login, claude, duo, local, council, research, vision, autopilot.
- `PATCH /api/settings` takes the config keys above.

## Usage counting
`usage.json` in the hub data folder: `{"days": {"YYYY-MM-DD": {provider: n}}, "login_calls": [epoch...]}`, local days, 14 days
kept, a damaged file starts over, a failing write never breaks a request. Counted per request at: `Guarded` (gemini,
claude-login, claude; the duo's Gemini leg counts as `duo`; the duo's Claude supervisor counts as `claude-login`, which is
the quota it really uses), the Ollama client and local assistant turns (`local`), and the daily-budget points of
Autopilot, Research, Vision and Council. A text provider counts every model call in a turn (a tool turn is several).
The 5-hour Claude-login figure is an ESTIMATE from our own call timestamps, not a number Claude reports.

## Budget fallback
When the cap of the chosen cloud provider (or `cloud_daily_cap` over all cloud providers) is reached, `Router.route`
returns `local` with a decision `{routed_to: "local", capped: true, note: "Daily Gemini cap reached; using the local model."}`;
`/api/ask` and `/api/assistant/message` send it as the `meta` event (`reason` and `note`). It applies to explicit picks and
to the auto router, and never switches to another paid provider. One-turn override: say "use gemini anyway" (or
claude / claude login / duo) in the message, or send `override_cap: true`.
