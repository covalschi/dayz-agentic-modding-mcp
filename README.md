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

A mod is declared once, by name: sources in `<root>/Name` by default, output
`@Name/addons/Name.pbo`. `build.sources` can redirect a mod's source elsewhere
(e.g. `"."` for a mod whose `config.cpp` sits at the repository root itself).

## Tools

| Tool | What it does |
|---|---|
| `project_open(path)` | read the profile, discover the game and tools, report what is missing |
| `project_status()` | current project, running server, recent jobs |
| `mod_build()` | pack and sign every declared mod; returns a job id |
| `server_start(timeout)` | start the test server, finish when the ready line appears |
| `server_status(pulse_seconds)` | pid, whether the process is alive, whether the log is growing (sampled `pulse_seconds` apart), and how long it has been stalled |
| `server_stop(pid)` | stop the server this session started (optional pid for orphaned servers) |
| `client_compile_check(extra_mods, wait_seconds)` | run the diagnostic client and read its logs |
| `log_verdict(source, since)` | pass/fail with reasons: counters, forbidden strings, warning budget; `since` (an epoch timestamp, e.g. the `since` `server_start` returns) refuses a log written before the run being judged, so a stale log from an earlier boot cannot be mistaken for this one's result |
| `log_tail(source, pattern, n)` | last lines, optionally filtered |
| `job_status(job_id)` | status of a long-running job |
| `job_wait(job_id, timeout)` | wait for a job to finish (only tool that blocks) |
| `job_artifacts(job_id)` | retrieve outputs from a completed job |

Nothing blocks except `job_wait`, and that always takes a timeout.

## Known limitations

* **Stale-pbo detection is mtime-based, not content-based.** `mod_build`
  refuses a freshly built pbo that is older than its sources -- the usual
  cause is a running server still holding the old file open, so packing
  silently produced nothing. But `git checkout` changes a file's modification
  time without changing its content, so a perfectly good pbo built right
  after switching branches can trip this check too. If `mod_build` reports
  "stale pbo" immediately after a branch switch, this is the likely reason,
  not a real packing failure -- rebuild and it will pass. A mature tool in
  this space moved to a content hash for exactly this reason; that is future
  work here, not done in this phase (see `packer.py`, `pack_one`).
* **A mod source folder is packed whole.** `mod_build` refuses to pack a mod
  whose source directory contains anything matching `build.exclude`
  (default: `.git`, `*.blend`, `*.blend1`) rather than silently shipping it
  inside the published pbo. By default it does not stage a filtered copy
  first: a copy is always newer than the sources, which would permanently
  disable the stale-pbo check above *if that check measured the copy*.
  `build.stage = true` opts into copying anyway -- safe only because the
  stale-pbo comparison always measures the original source tree, never the
  copy. This is the layout a mod whose source is the repository root needs
  (it always contains at least `.git`).

## Install

    python -m pip install -e ".[dev]"
    python -m pytest

Register in your MCP client:

    { "mcpServers": { "dayz": { "command": "dayz-mcp" } } }

## Licence

GPL-3.0-or-later. See `NOTICE.md`.
