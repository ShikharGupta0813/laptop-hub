# 31 · Meetings: notes and action items from a recorded meeting

**What this page is for:** record a meeting on your laptop, and afterwards get a summary, the decisions and a list of action items, which you can send to your tasks with one tap.

## Read this first: consent
Recording laws differ by country and state, and some workplaces forbid it. **Tell everyone in the meeting that you are recording.** For work meetings check your company's policy (for example OneStack's) before you press record. REDLINE asks you to tick "I have told participants" the first time for each meeting title, and a red "Recording" pill stays at the top of the app the whole time. It never starts by itself: when a calendar meeting begins it only asks "Meeting starting: record it?".

## Turn it on
Apps > Meetings > **Turn on meetings** (off until you do). You need `pip install sounddevice` for your microphone, `pip install -r requirements-voice.txt` for the transcription, and on Windows `pip install soundcard` to also capture the other people on a call (what the laptop plays). Without that last one only your own microphone is recorded, and the app says so.

## A meeting, step by step
1. Tap **Record a meeting**, type the title, read "Recording meeting. Make sure everyone present knows.", tick the box and start.
2. The laptop turns speech into text a few seconds at a time and throws the audio away. Only the text is kept on the laptop. It stops by itself after 120 minutes (change it in Settings) or when you tap **Stop recording** or the Panic button.
3. When it stops, the notes are made in the background. By default the free Council or a local model writes them, after your personal details (emails, phone numbers, names you listed) are hidden. Tick **Private notes** and only local models are used. If no AI is available you still get a simple list of lines that sound like actions ("will", "todo", "by Friday", "deadline") and the page says so.
4. Open the meeting: summary, decisions, risks, open questions and action items with an owner (you / others / unknown), a due date if one was said, and the time in the meeting. Tap **Edit notes** to fix anything.
5. Under **Add to my tasks and memory** you see exactly what would be saved. Tick what you want (items for you become tasks with a reminder on the due date, other people's commitments become follow-ups like "Ask Priya about the contract on Friday", the summary and decisions become memory items tagged `meeting`), edit a title or date, and tap **Add picked** or **Add all**. Nothing is saved before that tap.
6. Your open items show up as **You owe** on the morning front page and in the spoken brief.

## Ask about meetings
"What did we decide about the rollout last week?", "summarise my last meeting", "list my meetings". "Start recording" asks you first (your approval is the consent tick); "stop recording" works at once. Jarvis answers technical decisions; Friday handles commitments and follow-ups.

## Good to know
- Speakers are only labelled **You** (your microphone) and **Others** (the laptop's sound). There is no speaker recognition.
- Audio files are NOT kept unless you switch on "Keep audio files" in Settings (a warning is shown). **Delete this meeting** really removes the folder: transcript, notes and any audio.
- Transcription quality depends on the Whisper model (`base` by default; `small` is more accurate, slower) and on the microphone and room.
- Transcripts are treated as untrusted text: something said in a meeting can never make the assistant do anything.

Details: `docs/api/meetings.md`. Back to the [guide index](README.md).
