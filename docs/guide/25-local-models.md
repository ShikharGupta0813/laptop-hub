# 25 · Local models, DeepSeek and "think harder"

**What this page is for:** choose, install and compare AI models that run **on your laptop through Ollama**. Nothing you type leaves the laptop. DeepSeek's own website or app (their hosted API) is **not** used: that would send your data to their servers. Only the open DeepSeek-R1 model files are downloaded and run here.

## Where
**Settings, Models, "Local models, deep thinking, benchmark".** It shows a catalogue (DeepSeek-R1, Qwen3, Qwen2.5 Coder, Gemma 3, Phi-4, Llama 3.2...) with size, memory estimate and strengths. Tap a model to **Install**, **Use for deep thinking**, **Use as default local**, **Remove**, **Edit** or delete it from the list. **Add a model by tag** takes any name from ollama.com/library. Sizes are approximate; "tools" and speed say *unknown* until you set or measure them.

## Your laptop is CPU only
With no NVIDIA graphics card, models of 7B and up typically give only a few words per second. That is fine for a slow background job and tiresome for chat. Keep the everyday model small (the default qwen3:4b) and use a reasoning model only for hard jobs.

## Deep thinking
Pick a reasoning model (for example `deepseek-r1:8b`) with **Use for deep thinking**. Then, in the same screen, paste an RCA, research notes, a plan request or an application brief and press **Think harder**, or ask Jarvis "think harder about...". It runs **in the background** (up to 20 minutes, one at a time) and you get a notification. The answer is shown without the model's inner monologue; **Show reasoning** unfolds it. It is never used for greetings, quick commands or spoken replies, so voice stays fast.

## Which model is best on MY laptop?
Press **Run benchmark** (or run `python -m hub models bench`). Each installed model gets three short prompts: a reasoning puzzle, a summary and a JSON extraction. You see seconds, tokens per second, and pass or fail for the puzzle and the JSON. A 7B+ model can take many minutes; close heavy apps first.

## Good to know
- Installing asks first, shows the download size and free disk, and can be cancelled. Removing asks first and refuses to remove the model you currently use.
- If a model cannot use tools, mark it `none` in its entry and the assistant just answers. `text-format` makes it use written tool lines like Gemini does.
- Details for builders: `docs/api/models.md`.
