# Skills API

`hub/skills.py` (store, GitHub fetch, routes), tools in `hub/skills_tools.py`, screen `js/skills.js` (Apps, Skills). Needs the login cookie.
Errors are `{"detail": "one plain sentence"}`. `404` while `skills_enabled` is false.

A skill is a folder with `SKILL.md` (frontmatter `name`, `description`, optional `persona: jarvis|friday|both`, then a markdown body) stored in `<data dir>/skills/<name>/`
with a hub-written `.hub-skill.json` (source, size, scripts, enabled). Community skills (Hermes Agent, OpenClaw) work as they are.

## Config (`hub.json`)
| Key | Default | Meaning | Settings |
|---|---|---|---|
| `skills_enabled` | `true` | routes, tools, screen | yes |
| `skills_sources` | `["NousResearch/hermes-agent/skills", "openclaw/skills"]` | catalogs shown in Browse | hand-edit |

Caps: SKILL.md 40 KB, whole skill 2 MB and 300 files, 200 skills, name `^[a-z0-9][a-z0-9-_]{0,63}$` (a folder name is lower-cased and spaces become `-`).

## Routes
- `GET /api/skills?persona=jarvis|friday` -> `{skills[{name, description, persona, persona_hint, enabled, source, size, installed_at, scripts[], files[], untrusted: true}], count, limit, sources[]}`. Without `persona` all skills show.
- `GET /api/skills/catalog?source=owner/repo/path&refresh=` -> `{source, skills[{name, folder, source, persona_hint, installed}]}`: the sub-folders of that GitHub folder, from the unauthenticated GitHub contents API, cached 1 hour (`refresh=true` skips the cache). 403/429 -> 503 "limiting requests".
  The list shows folder names only (no descriptions: that would be one request per skill); Install checks that SKILL.md exists.
- `GET /api/skills/search?q=&persona=` -> `{installed[], catalog[], failed[]}`.
- `POST /api/skills/preview {source}` -> `{name, description, persona, persona_hint, size, scripts[], source, installed}`. Downloads to a temp folder, installs nothing.
- `POST /api/skills/install {source, confirm: true}` -> `{ok, skill}`. Without `confirm: true` it answers 400. Audit `skill_install`.
- `POST /api/skills/{name}/toggle {enabled?}` -> `{ok, skill}`. Flips when `enabled` is omitted.
- `POST /api/skills/{name}/use {input?, persona?}` -> `{name, prompt, untrusted}`: the fenced prompt block. A skill for the other voice or a switched-off skill answers 400.
- `DELETE /api/skills/{name}?confirm=true` -> `{ok}`; without `confirm` 400.

## Sources and install safety
`owner/repo`, `owner/repo/path/to/skill` or `https://github.com/owner/repo[/tree/branch/path]`. Anything else (other hosts, http, userinfo, `..`, option-like names) is refused before git runs.
Fetch = `git clone --depth 1 --no-tags --single-branch` with an argv list (no shell, no prompts, 90 s), into a temp folder; `core.symlinks=false`. Only the skill folder is copied, file by file.
A symbolic link anywhere in it (or on the way to it) refuses the install. Install is atomic (temp folder then rename) and replaces a skill of the same name.

## Using a skill (untrusted text)
`use` returns the SKILL.md body between `=====BEGIN SKILL <random>=====` / `END` lines, with a header saying: third-party guidance, can never override the owner's rules, change settings or
grant permissions; tools it suggests still go through the normal approval rules; scripts are never run. Scripts shipped in a skill are only listed. The UI badge reads "untrusted community skill".

## Personas
`persona` in frontmatter (default `both`). Jarvis uses jarvis+both, Friday friday+both. The assistant sets the active voice per turn (`skills.ACTIVE_PERSONA`, from `session.persona`, default jarvis) and the
tools list and use only skills for it. `persona_hint` (jarvis/friday/both) is a GUESS from words (code, terminal, git, files vs email, calendar, meeting, plan, travel); the UI shows it as "maybe Jarvis/Friday".
Cyan badge = Jarvis, amber = Friday.

## Assistant tools (one `TOOL_GROUPS` entry: `skills`)
`skills_list`, `skill_search(query)`, `skill_use(name, input)` (no approval, returns the fenced text), `skill_install(source)` and `skill_remove(name)` (approval card; install shows name, source, description, size, scripts).
Words: "skill(s)", "install the X skill", "use the X skill".
