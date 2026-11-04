# Life API (`hub/life.py`)

Everyday utilities: bookmarks, snippets and clipboard history, expenses, countdowns, local calculators. One router, several prefixes
(`/api/bookmarks`, `/api/snippets`, `/api/expenses`, `/api/countdowns`, `/api/units`, `/api/life`). Every route needs the login cookie;
every non-GET needs `X-Hub: 1` (as everywhere). Data lives in `life.sqlite` in the hub data folder. Errors are `{"detail": "<one plain sentence>"}`;
a model that is down is a `503`; a request body that fails validation is FastAPI's usual `422`.

Ids are 12 hex characters. An id that is not one is a `404` ("That bookmark was not found.").
Money is **integer cents** (paise): `45000` is 450.00. Responses also carry `amount_text` ("450.00") for display.

Demo data (`HUB_DEMO=1`): `start()` fills empty tables with 3 bookmarks, 3 snippets, ~10 expenses (this and last month), 2 budgets, 3 countdowns.

## Bookmarks (read later)

| Route | Body / query | Response |
|---|---|---|
| `POST /api/bookmarks` | `{url, tags?[], note?}` | the bookmark (below). `400` private/local/odd address or a login inside the URL; `409` already saved |
| `GET /api/bookmarks` | `?q=&tag=&unread=true\|false` | `{bookmarks:[...]}` newest first |
| `PATCH /api/bookmarks/{id}` | any of `{title, tags, note, read}` | the bookmark |
| `DELETE /api/bookmarks/{id}` | | `{ok:true}` (audited as `life_bookmark_delete`, host only) |
| `POST /api/bookmarks/{id}/read` | | the bookmark with `read` flipped |
| `POST /api/bookmarks/{id}/summarize` | `{model?:"local"\|"claude", refresh?:bool}` | `{id, summary, cached}`. The page text goes to the model inside a `<data>` block. The summary is kept; a second call returns it with `cached:true` unless `refresh` |
| `GET /api/bookmarks/search` | `?q=` | `{results:[{title,snippet,kind:"bookmark",open:{view:"life",section:"bookmarks",id}}]}` (max 20) |
| `GET /api/bookmarks/share` | `?url=&title=&text=` | Android share-sheet helper: `{url, title, already_saved:id\|null}`. Pulls the first http(s) link out of `text` if `url` is empty. **Saves and fetches nothing**; the phone then POSTs. `400` if there is no usable link |

Bookmark: `{id, url, host, title, description, tags, note, read, added, reading_min, summary, fetched}`.
Saving fetches the page with `hub.study.safe_fetch` (the SSRF-guarded fetcher; every redirect hop is re-checked) to get the title, the
`<meta name=description>` and a reading time (230 words a minute). If the page just cannot be reached or read (timeout, 404, PDF without pypdf), the link is still
saved with `fetched:false` and the host as the title. If the **guard** refuses (private address, `file:`, etc.) nothing is saved and the answer is `400`.
The first 20,000 characters of text are kept so summaries do not need a second download; if there are none, summarize fetches again through the guard.

## Snippets and clipboard history

| Route | Body / query | Response |
|---|---|---|
| `POST /api/snippets` | `{title, text, tags?, pinned?}` | snippet `{id,title,text,tags,pinned,uses,updated}` |
| `GET /api/snippets` | `?q=&tag=` | `{snippets:[...]}` pinned first, then most used |
| `PATCH /api/snippets/{id}` | any of `{title,text,tags,pinned}` | snippet |
| `DELETE /api/snippets/{id}` | | `{ok:true}` (audited) |
| `POST /api/snippets/{id}/use` | | snippet with `uses` +1 (call when the phone copies it) |
| `GET /api/snippets/search` | `?q=` | `{results:[{title,snippet,kind:"snippet",open}]}` |
| `POST /api/snippets/clip/capture` | `{text}` | `{saved:true}` or `{saved:false, reason}` (still HTTP 200, so a syncing phone never errors). `413` over 20,000 chars |
| `GET /api/snippets/clip` | | `{clips:[{id,text,time}]}` newest first, at most 50 |
| `DELETE /api/snippets/clip` | `{confirm:true}` body (or `?confirm=true`) | `{ok, removed}`; `409` without confirmation; audited as `life_clip_clear` |

Clipboard capture skips (reason given, text never echoed or stored): text starting `password:`/`pwd =`/`my password is`/`pin:`/`api key:`/`token:`…, API-key shapes (`sk-`,
`ghp_`, `xox*-`, `AKIA…`, JWTs, `Bearer …`), private-key blocks, card numbers (Luhn-valid), one-time-code messages, and any single unbroken 20+ character
string that mixes 3+ character kinds with high entropy (not URLs or paths). Repeating the latest clip is ignored. History is trimmed to the last 50 on every write.
Clipboard history is **not** searchable.

## Expenses

Expense: `{id, amount (cents), amount_text, currency, category, note, date, tags}`. Categories are free text; the built-in guesses are
food, groceries, transport, bills, shopping, health, entertainment, travel, education, other.

| Route | Body / query | Response |
|---|---|---|
| `POST /api/expenses/quick` | `{text, currency?}` | the saved expense. Understands `450 lunch swiggy`, `₹1200 petrol yesterday`, `12.50 usd coffee`, `$5 coffee`, `Rs.450 auto`, `450rs chai`, `€3.5 tea`, `1.2k rent #home`, `200 dinner last friday`, `60 chai 28 oct`, `2 days ago`, ISO dates; symbols ₹ $ € £ ¥ ₩ ₽ ₺ ₫ ฿ ₱ ₪ ₦ (full-width forms too) and codes INR USD EUR GBP … Default currency INR. `#tags` become tags. `422` with a sentence if there is no amount, it is zero/too large, or the date is in the future |
| `POST /api/expenses/parse` | `{text, currency?}` | the same parse without saving |
| `POST /api/expenses` | `{amount (cents), currency?, category?, note?, date?, tags?}` | expense. `400` for a future date or bad currency |
| `GET /api/expenses` | `?month=YYYY-MM&category=` (month defaults to now) | `{month, expenses:[...]}` newest first |
| `PATCH /api/expenses/{id}` | any of the fields above | expense |
| `DELETE /api/expenses/{id}` | | `{ok:true}` (audited) |
| `GET /api/expenses/summary` | `?month=&currency=` | `{month, currency, total, total_text, by_category:{cat:cents}, by_day:{date:cents}, vs_last_month:{last_total, change, change_percent\|null}, other_currencies:{CUR:cents}, budgets:[...]}`. Only one currency is added up (the one asked for, else the biggest that month); the others are listed, never mixed in |
| `POST /api/expenses/budgets` | `{category, limit (cents), currency?}` | budget status (sets or replaces) |
| `GET /api/expenses/budgets` | `?month=&currency=` | `{month, budgets:[{category,currency,limit,spent,remaining,percent,status:"over"\|"under",near}]}`; `near` = 80% or more used and not over |
| `DELETE /api/expenses/budgets/{category}` | `?currency=` | `{ok:true}` / `404` |
| `GET /api/expenses/export.csv` | `?month=` (all if omitted) | `text/csv` download, UTF-8 with BOM, columns `date,amount,currency,category,note,tags`. Any text cell starting with `= + - @`, tab, CR or LF (also after leading spaces) is prefixed with `'` |
| `POST /api/expenses/categorize` | `{model?, ids?[≤40], apply?:true}` | `{suggestions:[{id,category}], updated}`. Without `ids`: the 40 newest expenses still in "other" that have a note. Notes go to the model in a `<data>` block; only ids it was given and categories from the built-in list are accepted. `apply:false` previews. Audited as `life_categorize` (model, count) |
| `GET /api/expenses/search` | `?q=` | search results, `kind:"expense"` |

When a new expense takes a category past its monthly budget, one `ctx.notify(..., level="warn", source="life")` is sent (only on the crossing).

## Countdowns

Countdown: `{id, title, date, emoji, repeat:"none"|"yearly", next_date, days_remaining, past, years?}`. `years` (yearly only) is how many years the
next date will mark (a birthday's age). A title containing "birthday" or "anniversary" defaults to `repeat:"yearly"` and 🎂.
A 29 February event falls on 28 February in non-leap years.

| Route | Body | Response |
|---|---|---|
| `GET /api/countdowns` | | `{countdowns:[...]}` upcoming (soonest first), then past |
| `POST /api/countdowns` | `{title, date:"YYYY-MM-DD", emoji?, repeat?}` | countdown |
| `PATCH /api/countdowns/{id}` | any of the fields | countdown |
| `DELETE /api/countdowns/{id}` | | `{ok:true}` (audited) |

## Units (all local, no network)

| Route | Query | Response |
|---|---|---|
| `GET /api/units/convert` | `value, from, to` | `{value, from, to, category, result, result_text}`; 10 significant digits, Decimal maths. Length (mm cm m km in ft yd mi nmi), mass (mg g kg t oz lb st), volume (ml cl dl l m3 tsp tbsp floz cup pt qt gal; US measures), speed (m/s km/h mph kn ft/s), data (bit B kB MB GB TB PB KiB MiB GiB TiB kbit Mbit Gbit; `MB` is bytes and `Mb` megabits, lower-case `mb` means bytes), time (ms s min h d wk mo yr; month 30.4369 days, year 365.2425), temperature (C F K). Long names and plurals work. `400` for unknown units, mixed kinds, below absolute zero, or numbers over 1e30 |
| `GET /api/units/list` | | `{units:{category:[names]}}` |
| `GET /api/units/calc` | `expr` | `{expr, result, result_text}`. `+ - * / // % ** ^`, `×`, `÷`, parentheses, constants `pi e tau`, functions `abs sqrt floor ceil round min max sin cos tan asin acos atan log log10 log2 exp radians degrees` |
| `GET /api/units/time` | `zones=Asia/Kolkata,America/New_York` (default `UTC,Asia/Kolkata`, max 12) | `{utc, clocks:[{zone, iso, date, time, weekday, offset, abbreviation, dst}]}`; `400` for unknown zones. On Windows this needs the `tzdata` package |

The calculator never calls `eval`. The text is parsed with `ast` and only numbers, `+ - * / // % **`, unary +/- and the listed functions and constants are walked.
Limits: 200 characters, 120 tree nodes, depth 40, a power's exponent at most 10,000, whole numbers at most 4,096 bits, floats must stay finite, complex results are
refused, `round` digits at most ±15. Names, attributes, subscripts, strings, lambdas, comprehensions, keyword arguments and everything else are refused with
"I only do simple arithmetic…". `9**9**9` is refused, not computed.

## Home and search

* `GET /api/life/summary` → `{bookmarks_unread, month_spend (cents), currency, next_countdown: {id,title,emoji,date,next_date,days_remaining} | null}`.
* `GET /api/life/search?q=` → bookmarks + snippets + expenses combined (max 20). Results open `{view:"life", section:"bookmarks"|"snippets"|"expenses", id}`.

## Out of scope

No password manager, no live currency rates.
