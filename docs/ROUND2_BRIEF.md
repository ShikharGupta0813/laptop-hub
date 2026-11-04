# Round 2 brief — conventions every backend agent follows

Read first: `hub/common.py` (Ctx, JsonFile, InProcessClient, parse_json, ask_json), `hub/laptop.py` (a finished feature module with an injectable System layer), `hub/jarvis.py`, `hub/study.py`, `hub/config.py`, `hub/server.py` (FEATURE_MODULES, middleware, lifespan), `tests/helpers.py`, `tests/test_jarvis.py` (how a module is tested), `docs/API.md`.

## Module contract
- One file `hub/<name>.py` (or package `hub/<name>/`) exporting `make_router(ctx) -> APIRouter` with `prefix="/api/<name>"` (extra prefixes allowed if the brief says so) and `dependencies=[Depends(ctx.need_auth)]`.
- Optional `async def start(ctx)` returning an optional async stop callable (or None) — use it for background loops. It runs on app startup; the server already calls it. Seed demo data there when `os.environ.get("HUB_DEMO")` is set.
- Optional `DEV_FAKES = {...}` (see `hub/english.py`) for the fake model in `hub.devserver`.
- Persist with `JsonFile(ctx.config.data_path() / "<x>.json")` for small data, or `sqlite3` in `ctx.config.data_path()` for larger. Atomic, no half files.
- Models: `model = ctx.model_for(choice)` with choice "local" | "claude"; `await model.complete(system, user, json_mode=True)`; `ask_json(...)` for JSON; `model.stream(...)`. Turn `LLMError` into `http_from(exc)` (503 with plain sentence). Every AI feature takes an optional `model: "local"|"claude"` field, default "local".
- Errors are plain one-sentence English in `HTTPException(detail=...)`, written for a non-technical phone user.
- Audit anything that changes the laptop, sends something out, or deletes: `ctx.audit("event_name", key=value)` (never log secrets/full file contents).
- Notify: call `ctx.notify(title, body="", level="info"|"warn"|"error", link=None, source="<name>")` at runtime (never capture it at import/make_router time — the notify module replaces it).
- Search: if your feature has searchable content, add `GET /api/<name>/search?q=` returning `{"results":[{"title","snippet","kind","open":{"view":"...","section":"...","id":"..."}}]}` (max 20).
- Call other features only through `InProcessClient(ctx.get_app, ctx.config).call(method, path, ...)`, never by importing their internals.
- Security: path inputs resolved only via the existing safe resolver in `hub/files.py` (look at it); never build shell strings (argv lists only); validate ids; cap sizes; no secrets in responses or audit; SSRF-guard any URL fetch (reuse the guarded fetch in `hub/study.py`). A logged-in phone is trusted, but every outward/destructive action needs the confirm/approval pattern used in `hub/laptop.py` / `hub/jarvis.py` (`confirm: true` body field, 409 otherwise) unless the brief says otherwise.
- Windows-only things degrade gracefully on Linux (tests run on Linux) and take an injectable fake for tests.

## Ownership & hygiene
- You own ONLY the files named in your task. Do not edit `server.py`, `common.py`, `config.py`, `assistant*.py`, `devserver.py`, existing modules, or other agents' files. If you need a change there, say so in your final report.
- Write your API docs to `docs/api/<name>.md` (routes, bodies, responses, errors), not to `docs/API.md`.
- Tests: `tests/test_<name>.py` with unittest, no network, no real models; use `tests/helpers.make_config`, `hub.server.create_app`, FastAPI `TestClient` with header `X-Hub: 1` and login first (copy the pattern from an existing test). Cover security refusals and failure paths, not just the happy path. Prove at least two of your safety checks by briefly breaking the rule and seeing a test fail (then restore).
- Run ONLY your own test file (`python -m unittest tests.test_<name>`), other agents are mid-edit. Do not `git commit` or push; the lead does that.
- Final report (under 250 words): routes added, files, anything you need from the lead, known gaps. Be honest about what is untested.
