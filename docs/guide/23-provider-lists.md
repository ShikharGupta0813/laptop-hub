# 23 · Your own lists: search, vision, AI endpoints, skill sources, coding agents

**What this page is for:** REDLINE does not hard-code one search engine, one vision model or one place to get skills. Each is a **list you control**: add to it, change it, switch an entry off, put the best first, or delete it. What REDLINE used to do by default is simply the first entry.

## Where
- **Settings, Providers** (a card near the top): *Search providers*, *Vision providers*, *AI endpoints*.
- **Apps, Skills, Sources**: where skills may come from.
- **Apps, Claude, Agents watched**: which coding tools show up.

Every list works the same way: the **first switched-on entry that works wins**; if it fails, the next one is tried. A failing entry is skipped for a few minutes and the list says why. **Test** checks one entry. **Keys** (API keys) are typed once, kept in the laptop's secure store, and never shown again.

## Search providers (used by Research)
DuckDuckGo and Wikipedia are there already. Add your own **SearXNG** address, a **Brave** key, **Stack Overflow** or a docs site (search only those places), or **any search API** (Bing, Google Custom Search, ...) by telling REDLINE the address pattern, where the key goes and which fields hold the title and link. Research shows "Searched with X" so you always know who answered.

## Vision providers ("what's on my screen?")
Gemini is the first entry. Add a **local Ollama vision model** (name it: llava, qwen2.5vl, llama3.2-vision) to keep your screen on the laptop, or any **OpenAI-compatible** vision API. "Claude (my login)" is listed but marked unsupported, because it cannot take pictures here. **Test** sends a small made-up picture, never your screen.

## AI endpoints
Your own OpenAI-compatible APIs (OpenAI, OpenRouter, LM Studio...). Pick one under "Use for routine drafts" and the cheap jobs (posts, autopilot, research summaries, application drafts) use it. The main assistant does not change.

## Skill sources
GitHub repos (as before), a **Git address on a host you approved** (the screen warns you when you approve one), a **folder on this laptop**, or a **direct link to one SKILL.md**. You still approve each install, skills are only text, and nothing inside is ever run.

## Coding agents
Claude Code is there already (and the only one you can send messages to). Add a **log file or folder** to watch any other tool, read-only, or the **experimental Codex CLI** reader. Gemini CLI is shown as a raw log tail, because REDLINE does not parse its files.

## Good to know
- Nothing here changes unless you change it. Deleting an entry never breaks the app: the rest of the list is used.
- Private addresses (your own server) must be allowed on the entry; public ones are always checked for safety.
- Details for builders: `docs/api/searchprov.md`, `vision.md`, `endpoints.md`, `skills.md`, `ccsessions.md`.
