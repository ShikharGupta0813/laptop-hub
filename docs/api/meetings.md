# Meetings API

`hub/meetings.py` (+ `hub/meetingtext.py` pure text helpers, `hub/meetings_tools.py` assistant tools), screen `js/meetings.js` + `css/meetings.css`
(Apps > Meetings, plus a "Recording" pill shown at the top of every screen while a recording runs). Record a meeting on the LAPTOP, transcribe it
locally, get notes, decisions and action items that you can send to the Brain. Off by default. All routes need the login cookie.

> **Legal note.** Recording laws differ by country and state, and some workplaces forbid it. Tell everyone in the meeting. For work meetings check
> your company's policy (for example OneStack's) before you record. REDLINE helps you ask for consent; it does not make a recording lawful.

## Privacy and consent rules (all enforced in code, all tested)
- Nothing records unless the owner (or an approved `meeting_start` call) starts it: `confirm: true`, and `told_participants: true` the first time for a
  calendar event title (remembered per title in `<data>/meetings/consent.json`; an untitled meeting always asks).
- A calendar meeting starting only SENDS the prompt "Meeting starting: record it?" (proactive type `meeting_prompt`, link `meetings`); it never starts anything.
- The app shows a persistent "Recording mm:ss" pill. A recording stops by itself after `meetings_max_minutes`, on Panic (`panic.register("meetings", ...)`,
  no resume hook: Resume never restarts a recording) and when meetings are switched off.
- Audio is cut into 25 s chunks with a 2 s overlap, transcribed with the local faster-whisper model (`meetings_model`) in memory and discarded. Silence is
  never sent to the model. Only TRANSCRIPT TEXT is stored: `<data>/meetings/<id>/meeting.json`, file mode 0600, folder 0700. Speaker label = the audio track
  (`me` = microphone, `others` = Windows system audio); there is NO speaker identification.
- `meetings_keep_audio` (default false) also writes `audio-me.wav` / `audio-others.wav` (0600) beside the transcript and shows a warning.
- Notes are made through the work lanes (`hub/lanes.py`): class `routine` (free Council first) or `private` (local models only, no redaction needed). Any
  non-private text is redacted with `hub/redact.py` first (the owner's redaction settings, or the default kinds if redaction is off) and the placeholders are put
  back locally. If no AI answers, a pattern extractor finds lines with will / todo / action / deadline / by Friday ... and the notes say so.
- Nothing reaches the Brain without a tap: `GET /proposal` only shows; `POST /apply` writes only the items picked (edited first if you wish).
- Transcript text is untrusted data: the reading tools taint the assistant turn.

## Config (`hub.json`; append-only keys)
| Key | Default | Meaning |
|---|---|---|
| `meetings_enabled` | `false` | master switch (also off while Panic is on) |
| `meetings_keep_audio` | `false` | keep WAV files beside the transcript |
| `meetings_max_minutes` | `120` | auto-stop (1 to 480) |
| `meetings_model` | `base` | local Whisper model: tiny, base, small, medium |

They are changed through `PUT /api/meetings/settings`, not the Settings allow-list.

## Routes
- `GET /api/meetings/status` -> `{enabled, panic, recording, consent_text, legal, settings{enabled,keep_audio,max_minutes,model,models}, sources{me,others,whisper: {ok,why}}, prompt, processing[]}`
  and while recording `{id, title, elapsed_s, remaining_s, max_minutes, tracks, warnings, chunks_done, pending_chunks, dropped_chunks, words, keep_audio}`.
- `GET /api/meetings/consent?title=` -> `{needed, text, legal}`.
- `PUT /api/meetings/settings` `{enabled?, keep_audio?, max_minutes?, model?}` (400 on bad values; switching off stops a running recording).
- `POST /api/meetings/start` `{title?, confirm: true, told_participants?, tracks?: ["me","others"], private?}`. 400 without `confirm`, 403 when off, 409 when the tick is
  needed or a recording is already running, 423 under Panic, 503 when no audio source / faster-whisper is available (the message says what to install).
  `warnings` explains a missing track (e.g. "The other participants are NOT being recorded").
- `POST /api/meetings/stop` -> the meeting (status `processing`; notes follow). 409 when nothing is recording.
- `POST /api/meetings/prompt/dismiss`.
- `GET /api/meetings` -> `{meetings: [{id, title, started, ended, status, duration_s, method, private, actions, mine, added, words, audio, error}]}` newest first.
- `GET /api/meetings/{id}` -> the row plus `{notes, segments[{t,track,text}], warnings, stop_reason, tracks, added, legal}`.
  `notes` = `{summary[], decisions[], risks[], questions[], actions[{id,text,owner(me|others|unknown),who,due(YYYY-MM-DD),due_text,t,done,added,brain_id}], method(ai|fallback|manual), note, generated}`.
- `PATCH /api/meetings/{id}` `{title?, private?, notes?}`: the notes stay editable (cleaned to the same shape; `added` is the hub's record and cannot be set).
- `DELETE /api/meetings/{id}`: removes the folder (transcript, notes, audio). 409 while it is being recorded. Tasks already added to the Brain stay.
- `POST /api/meetings/{id}/process` `{private?}`: make the notes again (a re-run keeps "added" marks on unchanged actions).
- `GET /api/meetings/{id}/proposal` -> `{items: [{key, type(action|summary|decision), label, owner, default, kind, title, body, who, due, tags, persona, already}]}`:
  my actions -> task for you; others' actions -> follow-up "Ask Priya about: X on Thursday"; summary (and risks, open questions) -> `note`; each decision -> `decision`
  (Jarvis); all tagged `meeting` + the title slug; actions go to Friday. Unknown-owner items are not pre-ticked.
- `POST /api/meetings/{id}/apply` `{items: [{key, title?, due?, who?, body?}]}` or `{all: true}`. Writes through `POST /api/brain/items` (reminder at 09:00 on the due date; if the
  date is past the item is kept with "Due ..." in its text). -> `{added[], failed[], proposal}`. An item already added is never written twice.
- `GET /api/meetings/search?q=&limit=` -> `{hits: [{id, title, date, where(decision|action|summary|risk|transcript), t, text, score}]}`. A date phrase in the query ("last week", "yesterday") filters.
- `GET /api/meetings/owed` -> `{owed: [{text, due, meeting, id, date}]}`: your open actions from the last 21 days (the front page and morning brief show "You owe: ...").

## Assistant tools (`hub/meetings_tools.py`, TOOL_GROUPS `meetings`)
`meeting_list`, `meeting_summary(id|title)`, `meeting_search(query)` (untrusted: they taint the turn), `meeting_start(title, told_participants, private?)` (asks every time; the approval card is
the consent tick and shows "Recording meeting. Make sure everyone present knows."), `meeting_stop` (free). Jarvis owns technical decisions and RCAs from meetings; Friday owns commitments and follow-ups.

## Windows audio
The microphone uses `sounddevice`. The other participants use WASAPI loopback through the optional `soundcard` package (`pip install soundcard`), Windows only; without it only the
microphone is recorded and the app says so. The transcript needs `faster-whisper` (`requirements-voice.txt`).
