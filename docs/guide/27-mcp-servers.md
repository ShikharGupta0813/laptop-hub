# 27 · MCP servers

MCP servers are small programs (or web services) that give Jarvis and Friday extra tools: read a folder, fetch a page, look up the time. REDLINE can use any server that speaks the open Model Context Protocol. You choose which ones, and every use is gated.

## What you see
Settings > **MCP servers**. The list starts empty. Nothing runs until you switch it on.

1. **Add an MCP server**. Give it a name, then either pick an example from the list ("review before enabling": the command lines come from each project's public page and have not been checked here) or type your own command, one word per line, program first (for example `npx`, `-y`, `@modelcontextprotocol/server-memory`). A new server is always added **switched off**.
2. **Edit** it if needed: folder to start in, environment variables (`NAME=value`, kept in the laptop's secure store and never shown again), and trust.
3. Open it (the pencil) and press **Test**. REDLINE asks first, starts the program, lists its tools, and stops it. Read the tool list and what each one says it does.
4. Switch the server on. Its tools now show up for the assistant. Each tool has its own switches: offer it or not, run without asking (read-only), always critical, keep arguments out of the log.

> ⚠️ **Careful:** a server is a program that runs with your rights on this laptop. Only add ones you trust, and read the command before you switch it on.

## What happens when the assistant uses a tool
- A card asks you **every time**, with the exact arguments. This is also true with auto-approve and in Full Control.
- The one exception is a tool you marked read-only, on a server whose trust you set to "let tools I mark read-only run without asking". If the tool's description changes afterwards, the mark is dropped and it asks again.
- Tools that look like running commands, deleting or sending (by name or description) are always **critical**: you type `CONFIRM` and get a few seconds to cancel. You cannot mark those read-only. A spoken yes never counts.
- Whatever a tool returns is treated as untrusted text, like a web page. After it, the assistant cannot run anything that changes things until you send your next message.
- Descriptions written by a server are shown to the assistant inside markers and cannot change any of these rules.
- A server stops by itself when idle (5 minutes) and is stopped with all its helper programs when you stop it, change it, or turn it off. The audit log records the server, tool, level and outcome; for a tool marked sensitive it records no arguments.

## Good to know
- Nothing is enabled by default and the assistant has no tool to add servers: only you can, in Settings.
- Web servers (streamable HTTP) need an `https://` address (plain `http://` only for this laptop) and may use one access token.
- If Test says the program could not be started, run its command in a terminal first (Node.js for `npx`, uv for `uvx`).
- The master switch is `mcp_enabled` in `hub.json`; the idle time, parallel calls and call timeout are `mcp_idle_seconds`, `mcp_max_concurrent`, `mcp_call_timeout`.
- Details for builders: `docs/api/mcp.md`.

🟡 Not tested on real devices yet. Built and tested only against a fake server; real servers, Windows `npx` and the screen on a phone are unverified.
