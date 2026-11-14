# 31 · A voice that sounds like Jarvis and Friday

**What this page is for:** make REDLINE talk with a natural voice, give Jarvis and Friday different ones, and interrupt them by talking.

## The short version
Out of the box nothing changes: the phone reads answers with its own voice. In **Settings, Voices** you can switch to voices that the laptop makes, which usually sound better, and start speaking after the first sentence instead of after the whole answer.

## Pick a voice
1. Open **Settings, Voices**.
2. Choose the **Default engine**:
   - *This phone's voice*: what you have now.
   - *Windows voices (SAPI)*: needs nothing extra on a Windows laptop. Good enough to tell two assistants apart.
   - *Piper (local neural)*: more natural. On the laptop run `pip install piper-tts`, then use **Download voice** in the same screen. A voice is a download of tens of megabytes (some are over 100) from Hugging Face; it only starts when you press the button and confirm. Small ("low") voices usually run faster than real time on a CPU-only laptop. That is a general expectation, not measured on your machine.
   - *My own command*: any program that reads text on stdin and writes a WAV (for example a local ONNX voice). Advanced; the hub never runs it through a shell and the text is never put on the command line.
3. Under **Voice for each assistant** tap Jarvis, choose an engine and voice, and press **Test voice**. Do the same for Friday with a different voice.

## What you will notice
- The first sentence is spoken while the rest of the answer is still being written. The text on screen is the same as before.
- Code, tables, links and the end of a long list are not read out. You hear "I put the details on screen."
- If the laptop is slow or cannot make a sentence, the phone's voice says it, so you still hear the answer.
- Short phrases ("Done.") are remembered on the laptop, so they are instant.

## Interrupting
Talk over the assistant, tap **Stop talking**, tap the orb, or say "stop", "quiet" or "okay okay". The voice stops at once and REDLINE listens. Interrupting only silences it; if the answer is still being written it carries on on screen unless you say "stop" or ask something new (then the old answer is dropped).

> ⚠️ **Careful:** a phone's loudspeaker can hear the assistant and cut it off by mistake. Use headphones and switch on **Headphones mode** (also lets you interrupt the laptop's own voice by talking), or tap instead of talking.

## Quiet times
The panic button silences every voice. When presence says you are in a meeting or focusing (if that feature is installed), the laptop does not speak aloud.

> 🟡 **Not tested on real devices yet:** Piper on Windows, real audio, and interrupting on a phone were only checked with fakes.

Details for builders: `docs/api/tts.md`.
