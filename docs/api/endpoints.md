# AI endpoints API (OpenAI-compatible)

`hub/endpoints.py` (module `endpoints`, appended to `FEATURE_MODULES` on its own line), shared plumbing `hub/provlist.py`, UI `js/provlists.js` (Settings > Providers > AI endpoints).
Needs the login cookie. List file `<data>/ai_endpoints.json`; chosen endpoint `<data>/ai_endpoints_choice.json`; keys in the OS keyring (service `redline-providers`, name `endpoint:<id>`).

A list of the owner's own OpenAI-compatible APIs (OpenAI, OpenRouter, LM Studio, vLLM, Groq...). Each entry: label, on/off, `base_url` (".../v1"), `model`, optional key, `allow_private`.
Nothing is ready-made (an endpoint needs the owner's URL); presets for OpenAI, OpenRouter and LM Studio fill the form.

## What it changes (and what it does not)
One endpoint can be chosen for ROUTINE DRAFTS (`PUT /choice`). `autopilot.pick_provider` returns `endpoint:<id>` first, so every cheap path that already uses it (Posts, Autopilot, Research summaries,
application drafts) uses the endpoint, and `ctx.model_for("endpoint:<id>")` in `hub/server.py` builds the client behind the same `redact.Guarded` wrapper as the other cloud clients (private details hidden on the way out
and restored on the way back, usage counted as `endpoint` in `hub/usage.py`, "My AI endpoint" in the usage card). The main assistant routing, the router and `default_provider` are untouched: this is only one more option.
A deleted or switched-off chosen endpoint silently falls back to the old free path.

## Routes
- `GET /api/ai-endpoints` -> `{entries[], chosen, kinds, presets[], note}` (no secret ever; `has_secret` only).
- `POST /api/ai-endpoints {kind: "openai-compatible", label?, settings: {base_url, model, allow_private?}, secret?}`; `PUT /{id}` (label, enabled, settings merged, `secret`, `clear_secret`); `PUT /order {ids}`; `DELETE /{id}` (clears the choice if it was chosen).
- `PUT /api/ai-endpoints/choice {id}` (`""` clears; 404 for an unknown id).
- `POST /api/ai-endpoints/{id}/test` -> `{ok, reply}` or `{ok: false, error}` (one tiny chat request).

## Safety
Public endpoints pass the SSRF guard of `hub/research.py`; a private address (LM Studio on this laptop or tailnet) only when the entry says `allow_private`. Redirects are not followed. The key is sent only as `Authorization: Bearer`,
never logged or returned; errors never contain it. The client is also what the vision list's "OpenAI-compatible" kind uses (images as `data:` URLs in `image_url` parts).
Tests: `tests/test_provider_lists.py`. UNVERIFIED against real services: OpenAI/OpenRouter/LM Studio response quirks (`response_format` json mode is sent when JSON is asked; servers that reject it answer 400 and the caller falls back).
