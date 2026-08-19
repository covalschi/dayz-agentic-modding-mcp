# dayz-agentic-modding-mcp

An MCP server that lets an agent build a DayZ mod, check that the client compiles,
run a test server and get a structured verdict instead of a log.

The server does the work itself: it calls FileBank, the signing tool and the
diagnostic executable directly. A project does not need its own build script.

## Why a profile

The server knows nothing about any particular mod. Everything specific lives in a
two-part profile:

* `dayz-mcp.toml` — portable, committed to the mod repository: which mods to pack,
  what a healthy boot looks like.
* `dayz-mcp.local.toml` — machine-specific, never committed: where the game, the
  tools and the test stand live.

Mixing the halves is rejected on load: that is how a repository stops building on
anyone else's machine. Start from `dayz-mcp.example.toml`.

A mod is declared once, by name: sources in `<root>/Name`, output `@Name/addons/Name.pbo`.

## Tools

| Tool | What it does |
|---|---|
| `project_open(path)` | read the profile, discover the game and tools, report what is missing |
| `project_status()` | current project, running server, recent jobs |
| `mod_build()` | pack and sign every declared mod; returns a job id |
| `server_start(timeout)` | start the test server, finish when the ready line appears |
| `server_status()` | pid, whether the process is alive, whether the log is growing, and how long it has been stalled |
| `server_stop(pid)` | stop the server this session started (optional pid for orphaned servers) |
| `client_compile_check(extra_mods, wait_seconds)` | run the diagnostic client and read its logs |
| `log_verdict(source)` | pass/fail with reasons: counters, forbidden strings, warning budget |
| `log_tail(source, pattern, n)` | last lines, optionally filtered |
| `job_status(job_id)` | status of a long-running job |
| `job_wait(job_id, timeout)` | wait for a job to finish (only tool that blocks) |
| `job_artifacts(job_id)` | retrieve outputs from a completed job |

Nothing blocks except `job_wait`, and that always takes a timeout.

## Install

    python -m pip install -e ".[dev]"
    python -m pytest

Register in your MCP client:

    { "mcpServers": { "dayz": { "command": "dayz-mcp" } } }

## Licence

GPL-3.0-or-later. See `NOTICE.md`.
