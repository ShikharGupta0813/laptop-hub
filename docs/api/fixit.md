# Fix-it: provider chain, provider check, checklist, live logs

All routes need the login. Nothing is sent anywhere except the one tiny question of a provider Test.

- `GET /api/fixit/providers` -> `{providers:[{id,label,ready,reason,fix,command,action,chosen}], default, last, summary}`. `action.test_path` is the endpoint the Test button posts to (`/api/fixit/providers/{id}/test`, `/api/cli-providers/{id}/test`, `/api/ai-endpoints/{id}/test`).
- `POST /api/fixit/providers/{id}/test` -> `{ok, reply, latency_ms}` or `{ok:false, error, fix, command}`. Asks that one provider, never the fallback chain.
- `GET /api/fixit/checklist` -> `{items:[{id,label,ok,detail,step,action}], problems}`: AI ready, Jarvis folder / script / `.venv` / remote inbox, voice engine. The app adds browser-only checks (HTTPS, microphone permission, phone voice).
- `GET /api/fixit/log?source=jarvis|output|hub&lines=` -> tail of `jarvis.log`, `jarvis-output.log` or `hub-console.log` (secrets masked).
- `GET /api/fixit/doctor` -> `{lines:[{ok,text}]}`.

Provider chain (`hub/provchain.py`): asked or default provider, Claude API key, Gemini API key, enabled CLI logins (Gemini CLI) in list order, Claude login, enabled endpoints, local model. An explicit `local` pick never goes to the cloud. A failing provider is skipped only before its first word. When nothing answers the error lists every provider, what is missing, and the exact command or setting.

Console: `python -m hub serve` prints a banner (URL, login, voice engine, AI found/missing, features) and one line per AI choice or failure; the same lines are kept in `<data>/hub-console.log`.
