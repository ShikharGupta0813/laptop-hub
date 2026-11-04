# Screen API (`/api/screen`)

See the laptop screen from the phone, and (only after you switch it on) control it. Every route needs the
normal login. Errors are `{"detail": "<one plain sentence>"}`.

Optional libraries: `pip install -r requirements-screen.txt` (mss, Pillow, pyautogui, and pygetwindow on
Windows). The hub starts without them; the routes then answer **501** with a sentence saying what is missing.

Viewing is always on (once logged in). **Control is off until armed**, expires by itself (1-15 minutes) and every
control action is audited.

## Looking

### `GET /status`
```json
{"available": true, "reason": "", "monitors": [{"id": 1, "width": 1920, "height": 1080}],
 "control_available": true, "control_reason": "",
 "control_armed": false, "armed_until": null, "armed_seconds_left": 0}
```
`armed_until` is epoch seconds (null when off). Monitor ids start at 1 (1 = primary).

### `GET /frame?monitor=1&max_side=900&quality=55`
One JPEG (`image/jpeg`, `Cache-Control: no-store`, header `X-Screen-Size: WxH`). `max_side` is clamped to 100-3000,
`quality` to 10-95. Limit: 6 pictures per second per login session, then **429** "Too many screen pictures were
asked for at once. Wait a second and try again." (`Retry-After: 1`). Unknown monitor: 404. Not a number: 400.

### `GET /stream?monitor=1&fps=3&max_side=900&quality=55`
`multipart/x-mixed-replace; boundary=hubframe` (MJPEG; works in an `<img>`). `fps` is clamped to 1-6 (default 3).
- Only **one** stream runs at a time on the whole hub: opening a new one makes the previous one finish with a final
  text part ("This stream was closed because a newer one was opened.").
- Stops when the client disconnects.
- Hard cap of 10 minutes, ending with a text part "The stream stopped after 10 minutes. Open it again to keep
  watching." (the phone should offer a restart button).
- If a capture fails mid-stream a final text part says why. Problems known before it starts (not installed, unknown
  monitor) are normal 501/404 errors.
- Audit: `screen_stream` (monitor, fps). Frames are not audited.

## Control

### `POST /arm`  `{"confirm": true, "minutes": 5}`
`minutes` is a whole number 1-15 (default 5), else 400. Without `confirm: true`: **409**. If pyautogui is missing:
501. Audits `screen_arm`, and sends a `warn` notification "Remote control enabled for N minutes". Arming again
restarts the timer. Returns `{ok, control_armed: true, armed_until, armed_seconds_left, message}`.

### `POST /disarm`
Always works, returns `{ok: true, control_armed: false}`. Audits `screen_disarm`.

### `POST /input`
When not armed (or after it expires) **every** action, valid or not, answers **409**
"Remote control is off. Tap Enable control first." and does nothing (audited as `screen_input_refused`, action name only).

Body `{"action": ..., ...}`. Positions are **normalized 0..1** floats on the chosen monitor (`monitor`, default 1;
0,0 = top-left, 1,1 = bottom-right) so the phone never needs pixel sizes. Out-of-range numbers are clamped to the
edge of that monitor (never outside it). Non-numbers: 400.

| action | fields |
|---|---|
| `move` | `x`, `y` |
| `click`, `double_click`, `right_click` | optional `x`,`y` (both or neither; neither = where the pointer is), `button` `left`/`right`/`middle` (click only) |
| `drag` | `x`,`y` start, `x2`,`y2` end, optional `button` |
| `scroll` | `dy` (positive = down), `dx` (positive = right), whole steps clamped to +-30; optional `x`,`y` to move there first |
| `type` | `text` up to 500 characters; newline allowed; any other control character (tab, escape, CR, NUL...) is refused (400) |
| `key` | `key` from: enter, esc, tab, backspace, delete, up, down, left, right, home, end, pageup, pagedown, space, f1-f12 (also accepts escape, return, del, pgup, pgdn) |
| `hotkey` | `keys` list of 2-3 names, one of: ctrl+c, ctrl+v, ctrl+x, ctrl+a, ctrl+z, ctrl+y, ctrl+s, ctrl+f, ctrl+t, ctrl+w, ctrl+tab, alt+tab, win+d, win+tab, and alt+f4 which also needs `"confirm": true` (else 409) |

Response `{ok: true, action, x?, y?}` (pixel position used). Anything else on the key lists: 400 with the allowed list.
Audit `screen_input` has action and coordinates; for `type` only `chars` (the length), never the text.
Non-ASCII text is entered through the laptop clipboard (pyautogui cannot type it), which replaces the clipboard.
pyautogui's "corner aborts" failsafe is switched off on purpose (positions are clamped and Disable/expiry stops control).

### `POST /paste`  `{"text": "..."}`
Armed only. Up to 20,000 characters (no NUL). Puts the text on the laptop clipboard, then sends ctrl+v. Audit
`screen_paste` with `chars` only.

## Asking Claude

### `POST /ask`  `{"question": "What is open?", "model": "claude", "confirm": true, "monitor": 1}`
Takes a screenshot (max side 1568, JPEG) and asks Claude about it. Works while control is off.
- `model` must be exactly `"claude"` (the local model cannot see pictures): else 400.
- `confirm: true` is required (409), because the screenshot leaves the laptop.
- 501 "Needs Claude with image support..." if the connected Claude client cannot send images. It uses
  `model.ask_image(system, question, jpeg_bytes, mime)` if the client has one, otherwise the Anthropic SDK client held
  by `hub.llm.ClaudeClient` (`_get()` / `.model`). Missing API key etc.: 503 sentence.
- Response `{ok, answer, meta: {model: "claude", monitor, screenshot_size, screenshot_sent_to_claude: true, warning}}`.
- Audit `screen_ask` (model, monitor, `question_chars`; not the question text).

## Windows

### `GET /windows`
`{"windows": [{"id": "198430", "title": "Notes - Editor"}]}` (up to 100, titles trimmed). 501 where pygetwindow is not
available (it is Windows only).

### `POST /windows/focus`  `{"id": "198430"}`
Armed only (else 409). Brings that window to the front (restores it if minimised). Unknown/closed window: 404.
Bad id: 400. Audit `screen_focus_window` (id only).

## Not covered by automated tests
The real capture and input code (mss, pyautogui, pygetwindow, the clipboard tools) and a real Claude vision call;
tests use fakes (pyautogui is only checked through a stand-in module).
