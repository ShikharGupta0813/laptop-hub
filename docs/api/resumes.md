# Master resumes and the batch tailoring queue API

`hub/resumes.py` (masters, queue, routes, `start(ctx)`). Screen `js/ats.js` (Apps > ATS and packets, tabs Masters and Queue). Needs the login cookie. Switched off with `tailor_enabled: false`.
Data (hub data folder, local): `resume_masters.json`, `resume_queue.json`.

## Master resumes
A master is one resume per role family ("Backend", "Data", "Product"...), built **only from your facts** (`hub/factbase.py`) with the family's focus words steering which of your skills and bullets come first. A focus word you have not confirmed is never written into the resume. Every rebuild that changes the content is a new numbered version; the label is `"<name> v<n>"` (80 characters at most).
Families: backend, frontend, fullstack, data, ml, devops, product, qa, mobile, ops, or a custom name with your own focus words. Up to 12 masters, 20 versions each.

**Which master a tailored resume came from.** `POST /api/tailor/preview` (and so the queue and the packet) records `variant.master = {id, name, version, label, stale, auto}` and `variant.resume_version = label`. The master is the one you name (`master_id`), `"none"` to skip, or, by default, the family whose focus words best match the posting (none when nothing matches). `stale` means your facts changed since that version: rebuild it. When the tailored version is applied to an application (Tailor's "use it", the queue's Accept, the packet's put-in-queue) the label is copied to the application item as `resume_version`, which is what `hub/jobtrack.py` reads (`item.resume_version`) for its **By resume version** table, so reply rates are shown per master version. Renaming a master does not rewrite labels already recorded.

Routes:
- `GET /api/resumes/overview` -> `{masters[{id, name, family, focus, current, label, stale, versions[{n, label, created, note, lines}]}], queue[], pending, max_pending, families[], facts}`. One call for the screen.
- `POST /masters {family?, name?, focus?}` -> master (400: duplicate name, no facts, limit). `PUT /masters/{id} {name?, focus?}`. `DELETE /masters/{id}`.
- `POST /masters/{id}/rebuild {note?}` -> master + `changed`. Unchanged content makes no new version.
- `POST /masters/{id}/use/{n}` makes version n current. `GET /masters/{id}/versions/{n}` -> `{label, text, created, note}`.

## Batch tailoring queue
Pick several postings (`application_ids` from the application queue, or `jobs` pasted); the hub prepares a tailored resume, a cover note and an ATS score for each **one at a time** in the background. Nothing is sent. You review them one by one: accept, edit or discard.
- `POST /queue {application_ids?[], jobs?[{title, description,...}], master_id?}` -> `{added[], skipped[{ref, reason}], queue[]}`. Returns at once and starts the single worker.
- `GET /queue` -> `{queue[], pending, max_pending}`; items `{id, application_id, title, company, state queued|working|ready|accepted|discarded|failed, ats{score, label}, master, variant_id, blocked, edited, error}`. `GET /queue/{id}` -> the item with `resume_text`, `cover_text`, `variant`, `ats_detail` (full score).
- `POST /queue/{id}/accept`: refused (400) while the cover note has a blank or an unbacked sentence; otherwise writes the cover note into the application's draft (as Tailor's own apply does) and records `resume_version`. A pasted posting is only marked accepted (use the packet to queue it). Never sends.
- `POST /queue/{id}/edit {action: fill|edit|remove|confirm, sentence?, text?, label?, value?, fact_text?}` goes through Tailor's resolver; the honesty check runs again and the ATS score is refreshed.
- `POST /queue/{id}/discard` deletes the tailored version. `POST /queue/clear` removes finished entries.

Bounds (smooth app): at most **12** waiting (queued or being prepared), **8** added per call, 100 stored. One worker; each item runs in a thread (`asyncio.to_thread`), never on the event loop, with a 0.3 s pause between items and a 120 s limit per item. `start(ctx)` only wakes every 3 minutes to start the worker for left-over work (after a restart an item stuck as working goes back to queued). The screen does not poll: tap Refresh. AI cover rewrites are off in the queue (the template draft is used); the honesty check still runs.

Tests: `tests/test_packet.py` (MasterTests, QueueTests, QueueRouteTests).
