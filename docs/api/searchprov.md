# Search providers API

`hub/searchprov.py` (module `searchprov`, appended to `FEATURE_MODULES` through a separate line), shared list plumbing `hub/provlist.py`, UI `js/provlists.js` + `css/provlists.css`
(Settings > Providers > Search providers). Needs the login cookie. Errors are `{"detail": "one plain sentence"}`.

Design rule (owner): things are LISTS. Research no longer hard-codes DuckDuckGo + Wikipedia: it searches with an owner-editable, ordered list, and the old choice is
just the first two entries. The list is in `<data>/search_providers.json` (no secrets); a key lives only in the OS keyring (service `redline-providers`, name `search:<id>`).
The old `research_search_backend` config key still seeds the list the first time (`ddg` = Wikipedia off, `wikipedia` = DuckDuckGo off).

## Kinds
| kind | settings | key | notes |
|---|---|---|---|
| `ddg-html` | none | no | DuckDuckGo HTML endpoint (ready-made entry 1) |
| `wikipedia` | `lang` (en) | no | Wikipedia search API (ready-made entry 2) |
| `searxng` | `base_url`, `allow_private` (true) | no | the owner's instance, `GET /search?q=&format=json` (the instance must allow the `json` format, else 403 is reported) |
| `brave` | none | Brave API key | official Search API, `X-Subscription-Token` header |
| `json-api` | `url_template` (`{query}` `{limit}`), `method` GET/POST, `body_template` (JSON with `{query}` `{limit}`), `key_in` none/header/bearer/query/body, `key_name`, `results_path`, `title_field`, `url_field`, `snippet_field`, `allow_private` | optional | ANY search API. Dotted paths (`web.results`, `items.0.title`). Presets: Bing Web Search, Google Custom Search |
| `site` | `mode` stackexchange (`site`, e.g. stackoverflow) or domains (`domains`) | no | Stack Exchange public API, or DuckDuckGo limited to the given domains (results are filtered to those domains) |

## Fallback and notes
The first ENABLED provider that returns results wins. One that errors or finds nothing falls through to the next. Each search leaves notes the job's progress lines show:
"Searched with X", "X failed: <reason>; trying the next one", "X found nothing; trying the next one", "Skipped X (failed lately, next try in 5 min)".
A failing provider is skipped with backoff (1 min, doubling, 1 h max; memory only; cleared by a success, an edit or the Test button working). If every provider is in backoff
they are tried anyway rather than giving up. Failures show in the list (`status.failing`, `last_error`, `skip_seconds`).

## Routes
- `GET /api/search-providers` -> `{entries[{id, kind, label, enabled, settings, has_secret, status}], kinds{kind: {label, secret, fields[]}}, presets[], note}`. A secret is never returned, only `has_secret`.
- `POST /api/search-providers {kind, label?, settings?, enabled?, secret?}` -> `{ok, entry}`. 400 with a plain sentence for bad settings (a `json-api` GET template without `{query}`, a URL with a login in it, a bad domain...). At most 30 entries.
- `PUT /api/search-providers/{id} {label?, enabled?, settings? (merged), secret? (blank keeps), clear_secret?}`; `PUT /api/search-providers/order {ids[]}` (listed ids first, the rest keep their order); `DELETE /api/search-providers/{id}` (also deletes its keyring entry).
- `POST /api/search-providers/{id}/test` -> `{ok, count, first, error}` (runs one real search for "python programming"; ignores backoff, updates it).
- `POST /api/search-providers/search {query}` -> `{results[], via, notes[], untrusted: true}` (the screen's "try it").
- `POST /api/search-providers/reset {confirm: true}` -> back to the two ready-made entries (saved keys are removed).

## Safety
- SSRF: public hosts go through the guard of `hub/research.py` (`check_url`: http/https only, private/loopback/link-local/tailnet/reserved addresses refused, DNS answers checked). The owner's own SearXNG (and a `json-api` entry that says `allow_private`) may be private; they still must be plain http(s) without a login. Redirects are never followed. Brave, Stack Exchange and Wikipedia use fixed public hosts.
- Secrets: keyring only; never in the JSON file, a response, the audit log or an error message (errors name the problem, not the address or key).
- Results are UNTRUSTED text: HTML stripped, control characters removed, titles 160 / snippets 300 characters, http(s) links without a login only, at most 20. Research then fetches pages through its own page guard and puts everything between delimiters for a model with no tools.
- Audit events: `search_provider_add`, `search_provider_delete` (id and kind only).

Tests: `tests/test_searchprov.py` (fallback order and notes, backoff, every adapter's parse, JSON-API template/header/query/body key placement, guards, routes need login). UNVERIFIED against the real services: SearXNG instance settings, Brave/Bing/Google/Stack Exchange live response shapes (taken from their docs), and the screen in a browser.
