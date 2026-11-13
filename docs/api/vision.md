# Screen vision

"Jarvis, what's this error on my screen?" / "what am I looking at?" The hub captures the laptop screen (the Screen
feature's own capture), shrinks it (long edge <= 1600 px, JPEG q75, <= 1.2 MB) and asks Gemini about it.
Code: `hub/vision.py`, `GeminiClient.ask_image` in `hub/llm.py`.

## Which AI
Gemini only (free key in the OS keyring). The claude-login client is used only if it already has `ask_image`
(today it does not). The paid Claude API is never used. With neither: 503 "Gemini is needed to look at your screen."

## Routes (login required)
- `POST /api/vision/explain` `{question?, region?}` -> `{ok, answer, untrusted: true, left_today}`.
  `region` is `{x, y, w, h}` as fractions 0..1 of the screen. Answer is <= 120 words, plain language: what it is,
  likely cause, fix, error text verbatim. Errors: 400 bad region, 404 `vision_enabled` false, 429 daily budget used,
  501 no screen capture, 503 no Gemini / Gemini error.
- `GET /api/vision/status` -> `{enabled, left_today, budget, ready}`.

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
