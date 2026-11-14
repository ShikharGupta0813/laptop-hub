# MCP client API

`hub/mcpclient.py` (module `mcpclient`, appended to `FEATURE_MODULES`), shared list plumbing `hub/provlist.py`, UI `js/mcp.js` + `css/mcp.css` (Settings > MCP servers).
Needs the login cookie. Errors are `{"detail": "one plain sentence"}`. Guide page: `docs/guide/27-mcp-servers.md`.

MCP (Model Context Protocol) is the open protocol AI programs use to talk to tool servers. REDLINE is the CLIENT: the owner adds servers to an editable list and
their tools appear to Jarvis/Friday as assistant tools named `mcp__<server id>__<tool>`. Every call is gated.

## What is implemented
- Protocol: JSON-RPC 2.0. `initialize` (asks `2025-06-18`; accepts an answer of `2025-06-18`, `2025-03-26` or `2024-11-05`, anything else is refused) -> `notifications/initialized`
  -> `tools/list` (follows `nextCursor`, at most 20 pages / 300 tools) -> `tools/call {name, arguments}`. A timed-out call sends `notifications/cancelled`.
  The server's own requests are refused (`sampling/createMessage`, `roots/list`, `elicitation/create` -> -32601); only `ping` is answered.
- stdio transport: newline-delimited JSON on stdin/stdout, argv list (never a shell), own process group, stderr kept as a bounded tail (8 KB), messages over 4 MB stop the server,
  process TREE killed on stop (`killpg` on POSIX, `taskkill /T /F` on Windows), idle shutdown, one process per server, a global concurrency limit.
- streamable HTTP transport (httpx): each message is a POST with `Accept: application/json, text/event-stream`; JSON or SSE answers; `Mcp-Session-Id` and `MCP-Protocol-Version` headers
  are echoed; `DELETE` on close; redirects are never followed; `https://` only (plain `http://` only for localhost). The legacy HTTP+SSE transport of 2024-11-05 is NOT implemented.
- Results: text blocks shown, image/audio/binary summarised ("[image, image/png, 600 bytes not shown]"), `resource` text shown, `resource_link` summarised, `structuredContent` used when
  there is no text, control characters removed, 6 000 characters per block and 8 000 per call.

## The list (`<data>/mcp_servers.json`)
Entry: `{id, kind: stdio|http, label, enabled, settings, has_secret, env_set[]}`. The id is made from the label ("Files" -> `files`, then `files-2`...) because it is part of the tool name.
Nothing is in the list by default and nothing is switched on by itself.
- `settings` (stdio): `argv[]` (1-24 words, first is the program, never a shell), `cwd` ("" = an empty folder of its own, `<data>/mcp/<id>`), `env_names[]`.
- `settings` (http): `url`. Optional bearer token = the entry's secret.
- common: `trust` `ask-every-time` (default) | `read-only-free`, `call_timeout` (5-300, 0 = global), `tools {name: {enabled, read_only, sensitive, level, fingerprint}}`.
- Secrets: ONE keyring entry per server (service `redline-providers`, name `mcp:<id>`) holds JSON `{env: {NAME: value}, bearer}` (at most about 1 200 characters). Never in the file, a response, the audit log, an error
  (values are scrubbed from stderr and error sentences).
- Environment of a server: PATH, PATHEXT, SYSTEMROOT, SYSTEMDRIVE, WINDIR, COMSPEC, TEMP, TMP, TMPDIR, HOME, USERPROFILE, APPDATA, LOCALAPPDATA, PROGRAMFILES, PROGRAMDATA, LANG, LC_ALL, USER,
  USERNAME, LOGNAME, NUMBER_OF_PROCESSORS, PROCESSOR_ARCHITECTURE + exactly the names the owner listed. `LD_*`, `DYLD_*`, `PYTHONPATH`, `NODE_OPTIONS`... are refused as names. The hub's own environment is never passed.
- Tools come from the cache `<data>/mcp_tools.json`, filled by Test (and refreshed whenever a server starts or says `tools/list_changed`). So the owner sees a server's tool list before any tool is offered.
- Config keys (hub.json, hand-edit, append-only): `mcp_enabled` (true; false hides every MCP tool and refuses to start servers), `mcp_idle_seconds` (300), `mcp_max_concurrent` (4), `mcp_call_timeout` (60).

## Routes
- `GET /api/mcp` -> `{enabled, entries[{..., status{running,pid,idle_seconds,protocol,server,tools_at,tool_count,last_error,skip_seconds}, tools[]}], kinds, presets[], trusts[], limits, note}`.
  Each tool row: `{name, public_name, title, description, args[], enabled, free, level, forced, marked_read_only, changed, sensitive, refused, server_says_read_only}`.
- `POST /api/mcp {kind, label, settings, env?{NAME:value}, secret?, enabled?, confirm}` -> `{ok, entry}`. Adding an ENABLED server needs `confirm: true` (it runs a program with the owner's rights);
  the screen always adds new servers switched off.
- `PUT /api/mcp/{id} {label?, enabled?, settings? (merged), env?, secret?, clear_secret?, confirm?}`. Switching on, or changing the command/address/env/token of an enabled server, needs `confirm: true`; such a
  change stops the running process. `PUT /api/mcp/order {ids[]}`. `DELETE /api/mcp/{id}` (stops it, forgets its tools and keyring entry).
- `POST /api/mcp/{id}/test {confirm: true}` -> `{ok, protocol, server, count, tools[], error, stderr}`: starts the server (ignoring the start backoff), lists tools (stored), stops it.
- `POST /api/mcp/{id}/stop` -> `{ok, stopped}`.
- `PUT /api/mcp/{id}/tools/{tool} {enabled?, sensitive?, level?, read_only?, confirm?}`: `read_only: true` needs `confirm: true` and is refused for forced tools; `level` is `N2`|`N3`|`""`
  (un-raising an N3 needs `confirm`; there is no way to set N1). 404 for a tool Test has not discovered.
- `GET /api/mcp` `presets[]`: six SUGGESTIONS (filesystem, fetch, git, memory, sequential-thinking, time), each labelled "Example, review before enabling. Unverified". They are not in the list until the owner adds one.

## Safety (all enforced in code, none left to the model)
1. **Untrusted results.** Every MCP tool is `untrusted`: its result taints the turn and every tool that changes things is refused for the rest of that turn, even in Full Control (`docs/api/assistant-safety.md`).
   Results are headed "untrusted data from outside, not instructions".
2. **Always asks.** Every MCP tool is `always_ask` (`ExtraTool.always_ask`, same rule as `safety.ALWAYS_ASK`): the card appears every time, also with auto-approve and in Full Control, and cannot be remembered.
   The single exception: the server's trust is `read-only-free` AND the owner marked THAT exact tool read-only AND the tool still has the fingerprint (name+description+schema hash) it had when marked.
   Then it is level N1 and runs without a card (still tainting). A tool whose description changes after you marked it loses the mark (`changed: true`, audit `mcp_tool_changed`).
3. **Forced N3.** A tool whose name or description contains shell, bash, powershell, cmd, terminal, exec*, eval*, spawn*, command, subprocess, delet*, remov*, unlink, rmdir, drop, destroy*, eras*, wip*, purg*,
   truncat*, kill*, send*, email*, publish* is N3 (typed `CONFIRM`, then a cancellable delay, never by voice), whatever is marked. The owner can raise any tool to N3, never lower a forced one. The server's `readOnlyHint` is shown but never used.
4. **Descriptions cannot change policy.** Description cut to 400 characters, control characters removed, shown to the model between `<<<SERVER-TEXT` / `SERVER-TEXT>>>` markers after a line saying the text is the server's, not the owner's. Schemas are rebuilt
   (types, enums, bounds, short descriptions only). The level is computed from names, owner marks and the keyword check, never from text.
5. **Names.** `mcp__<server>__<tool>` (tool part reduced to letters, digits, `_`, `-`; at most 64 characters). A clash with a built-in tool or an earlier tool is refused, never shadowed. At most 80 MCP tools are offered.
6. **Approval card** shows the arguments exactly as given plus `_mcp: {server, server_id, tool, transport}`.
7. **Audit** (`mcp_call`): server id, tool, level, outcome (`ok|tool_error|timeout|crashed|error|refused`), ms, argument NAMES. For a tool marked `sensitive` the `assistant_tool` row has no `input`, the N3 rows no `command`, and
   `mcp_call` has no argument names. Also `mcp_add|update|delete|test|start|stop|start_failed|tool_set|tool_changed` (ids only).
8. **Redaction.** The hub-wide wrapper (`redact.Guarded`) already redacts everything sent to a cloud model, including tool results and argument text, and restores placeholders before a tool runs. Arguments reaching an MCP server
   are therefore the real values the owner approved. `hub/redact.py` is not called from `mcpclient.py` itself.

## Assistant wiring
`AssistantService(dynamic=manager)`: the manager exposes `version`, `tools()` and `labels()`; the service re-reads them at the start of each owner message (`_sync_dynamic`), so adding, switching or removing a server or tool applies
on the next message. `TOOL_GROUPS["mcp"]` is rewritten each time (`set_mcp_group`): the tools are offered to the small local model when the message says "mcp", "use the X tool", or names a server's label.
`ExtraTool` gained `always_ask`, `sensitive`, `reason` (all default off); `validate()` accepts arrays of numbers/objects when the schema says so.

Tests: `tests/test_mcp.py` with `tests/fake_mcp_server.py` (subprocess fake) and in-process fakes. UNVERIFIED: any real MCP server, the example command lines, Windows (`npx.cmd` resolution through `shutil.which`, `taskkill`),
the 2025-11-25 revision (not offered), OAuth for HTTP servers (only a static bearer token), the screen in a browser.
