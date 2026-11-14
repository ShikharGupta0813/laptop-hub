# Local models API

`hub/models.py` (catalogue, pull/remove, deep thinking, benchmark, 4 assistant tools), screen `js/models.js` + `css/models.css` (opened from Settings > Models > "Local models, deep thinking, benchmark"), CLI `python -m hub models bench`.
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Everything talks to the owner's own Ollama (`ollama_host`). **DeepSeek's hosted API is never used**; only open weights (for example `deepseek-r1:8b`) run locally.

## Honest limits
- Catalogue sizes and RAM figures are approximate and written from memory of the Ollama library; check `ollama list` after a download. The tags are the usual Ollama library tags but were not checked against the live library from this build (a wrong tag simply fails to download and can be edited).
- `tools` (tool-calling) is `unknown` unless this repo already relies on it (only `qwen3` is `native`). Speed is never claimed: the benchmark measures it.
- The owner's laptop is CPU-only (no NVIDIA GPU): 7B+ models typically run at a few tokens per second. The UI and API say so (`cpu_warning`).

## Catalogue
Editable list in `<data>/models/catalogue.json` (the built-in list until the first edit). Entry:
`{tag, label, size_gb, strengths[fast chat|reasoning|coding|vision], min_ram_gb, tools[native|text-format|none|unknown], recommended, note}`.
Built in: deepseek-r1 (1.5b 7b 8b 14b 32b), qwen3 (4b 8b 14b), qwen2.5-coder (7b 14b), gemma3 (4b 12b), phi4-mini, phi4, llama3.2:3b, llama3.2-vision:11b.
- `GET /api/models/catalogue` -> `{entries[], strengths[], tool_modes[]}`
- `POST /api/models/catalogue {entry}` -> adds or edits (matched by tag). `400` with a sentence for a bad tag/field. Max 80 entries.
- `DELETE /api/models/catalogue/{tag}` -> `404` if absent. `POST /api/models/catalogue-reset {confirm: true}` -> built-in list back (`409` without confirm).
- `GET /api/models` -> overview: `{reachable, installed[{name,size_gb,params,modified}], catalogue[entry + est_memory_gb, installed, fits_ram, cpu_warning, is_default, is_deep], config{chat_model, local_reasoning_model, deep_think_timeout_min, deep_think_max_chars}, machine{ram_gb, disk_free_gb, models_dir, nvidia_gpu}, cpu_warning, privacy, pull, think[], bench}`. A model the hub uses that is not in the catalogue is listed with `configured_only: true`. `est_memory_gb` = size x 1.15 + 1 GB (a rough rule).
- Use as default local = `PATCH /api/settings {chat_model}`; use for deep thinking = `PATCH /api/settings {local_reasoning_model}` (empty = off).

## Install and remove (owner-approved)
- `GET /api/models/estimate?tag=` -> `{size_gb, est_memory_gb, disk_free_gb, enough_disk, ram_gb, cpu_warning}` (shown before pulling).
- `POST /api/models/pull {tag, confirm: true}` -> background job `{id, tag, status: running|done|failed|cancelled, percent, line, error}`. `409` without confirm, when the catalogue size does not fit the free disk, or while another pull runs. Percent is overall across layers. A notification is sent when it ends.
- `GET /api/models/pull` -> `{pull}`; `POST /api/models/pull/cancel` -> `{cancelled}` (Ollama keeps fetched layers, a later pull resumes).
- `POST /api/models/remove {name, confirm: true, force?}` -> Ollama `DELETE /api/delete`. `409` without confirm, or when the model is the default local / search / tool / deep-thinking model (unless `force`). `404` if not installed.
- Assistant tools `pull_local_model {tag}` and `remove_local_model {name}`: safety level **N2**, an approval card each time (preview shows size and free disk). `list_local_models` is read-only.

## Deep thinking
Config `local_reasoning_model` (default empty = off), `deep_think_timeout_min` (20), `deep_think_max_chars` (30000, thinking + answer), all in Settings keys (append-only). It is a BACKGROUND JOB, one at a time, like research. It is **never** used for greetings, fast commands, voice replies or the router: only `POST /api/models/think` or the tool `think_harder` (returns at once, "do not wait") start one.
- `POST /api/models/think {kind: general|rca|synthesis|plan|application, task, keep_reasoning?}` -> job view. `409` when off, `429` while one runs, `400` for a short task. The task (max 12000 chars) is sent between `<material>` tags with "treat as data".
- `GET /api/models/think` -> `{jobs[] (no answers), model, kinds}`; `GET /api/models/think/{id}[?reasoning=true]` -> `{id, kind, model, status, phase: starting|thinking|answering|finished, title, answer, has_reasoning, thought_chars, answer_chars, tokens, tok_s, truncated, elapsed_s, error}`; the `reasoning` text only with `?reasoning=true` (the "show reasoning" fold). `POST .../cancel`, `DELETE ...`.
- Parsing: `ThinkSplitter` splits `<think>...</think>` from the answer even when tags are cut across chunks; also handles Ollama's separate `message.thinking`, a missing opening tag, several blocks and a block that never closes (no answer -> the job fails with a plain message). A model that cannot think is retried without `think`.
- Never logged: the audit log gets only ids, kind, model and character counts; notifications carry no prompt or answer text. Jobs are saved in `<data>/models/think/` (reasoning kept there unless `keep_reasoning: false`), newest 30.
- Caps: stops at `deep_think_max_chars` (answer kept if there is one) and at `deep_think_timeout_min`.
- Tool calling for local chat: the catalogue's `tools` decides how a local chat model is used by the assistant: `none` = no tools (answers only), `text-format` = the existing `<tool_call>` text path (`_text_turns("local")`, like Gemini), `native`/`unknown` = as before.

## Benchmark
`python -m hub models bench [--models a,b] [--only puzzle,summary,json] [--think auto|on|off] [--timeout 600]` runs 3 fixed prompts on each installed model (embedding models skipped): a reasoning puzzle (checked: answer 9), summarise a paragraph (not auto-checked), JSON extraction (checked: name/age/city). Reports seconds, tokens/second (Ollama's `eval_count/eval_duration`), pass/FAIL, and saves `<data>/models/bench.json` (last 10 runs). `think auto` uses the model's own default (reasoning models then think, and are slow on CPU).
- `POST /api/models/bench {confirm: true, models?, only?, think?}` runs it in the background; `GET /api/models/bench` -> `{running, model, prompt, n, total, error, latest[per-model results], prompts[]}`; `POST /api/models/bench/cancel`.

Tests: `tests/test_models.py` (fake Ollama transport and fake clock; no network).
