# Screen vision

"Jarvis, what's this error on my screen?" / "what am I looking at?" The hub captures the laptop screen (the Screen
feature's own capture), shrinks it (long edge <= 1600 px, JPEG q75, <= 1.2 MB) and asks Gemini about it.
Code: `hub/vision.py`, `GeminiClient.ask_image` in `hub/llm.py`.

## Which AI: an owner-editable LIST of vision providers
Settings > Providers > Vision providers (`js/provlists.js`). The first switched-on provider that can take a picture and answers wins; one that fails falls through to the next
(the answer says `provider` and lists `skipped` reasons) and is skipped for a while (backoff 1 min, doubling, 1 h max). List file `<data>/vision_providers.json`, keys in the OS keyring (`vision:<id>`).
Ready-made entries: `gemini` (free key from setup) and `claude-login`. The paid Claude API is never used.
| kind | settings | notes |
|---|---|---|
| `gemini` | none | today's behaviour; needs the keyring key |
| `claude-login` | none | usable only when that client has `ask_image`. Today it does NOT (the CLI client runs with no tools and no image input), so it is listed as "Unsupported" and skipped, honestly |
| `ollama` | `model` (llava, qwen2.5vl, llama3.2-vision...), `host` (blank = the laptop's Ollama) | `POST {host}/api/chat` with `images: [base64 JPEG]` in the user message, `stream: false` |
| `openai-compatible` | `base_url`, `model`, `allow_private`; key optional | `chat/completions` with an `image_url` data URL (any vision API: OpenAI, OpenRouter, LM Studio, vLLM...) |
Redaction and usage: the words around the picture go through `redact.Guarded` like every cloud call and count as `endpoint` (Ollama counts as `local`, no redaction: it never leaves the laptop); the picture itself cannot be redacted.
With no usable provider: 503 "Gemini is needed to look at your screen..." plus a pointer to the list.

### Provider routes (login required)
- `GET /api/vision/providers` -> `{entries[{id, kind, label, enabled, settings, has_secret, usable, unsupported, status}], kinds, presets, note}`
- `POST /api/vision/providers {kind, label?, settings, secret?}`, `PUT /api/vision/providers/{id}`, `PUT /api/vision/providers/order {ids}`, `DELETE /api/vision/providers/{id}`
- `POST /api/vision/providers/{id}/test` -> `{ok, answer}` / `{ok: false, error}`: sends a tiny generated picture (a red square), never the screen; does not use the daily budget.

## Routes (login required)
- `POST /api/vision/explain` `{question?, region?}` -> `{ok, answer, untrusted: true, left_today, provider, skipped[]}`.
  `region` is `{x, y, w, h}` as fractions 0..1 of the screen. Answer is <= 120 words, plain language: what it is,
  likely cause, fix, error text verbatim. Errors: 400 bad region, 404 `vision_enabled` false, 429 daily budget used,
  501 no screen capture, 503 no Gemini / Gemini error.
- `GET /api/vision/status` -> `{enabled, left_today, budget, ready}` (`ready`: some switched-on provider can take a picture).

## Assistant tool
`screen_explain(question?, region?)`: `needs_approval` (card preview "Look at your screen", skipped in Full Control),
`untrusted` (the answer taints the turn, so mutating tools are refused afterwards). Group `vision` in `TOOL_GROUPS`
(screen, error, what am I looking at, what's this).

## Config (Settings allow-listed)
`vision_enabled` (true), `vision_daily_calls` (30, 0..500; counted per local day in memory, resets on restart).

## Privacy
The screenshot is held in memory only: no file, never logged, audited or returned. The audit event `vision_explain`
carries the model name, question length, image KB and whether a region was used. The phone button asks for
confirmation before the picture leaves the laptop. Tests: `tests/test_vision.py`.
