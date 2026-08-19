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

### The portable half — `dayz-mcp.toml`

| Key | Meaning |
|---|---|
| `project.name` | what to call this project |
| `build.mods` | one entry per mod, by name; source, pbo and `@folder` all follow from it |
| `build.sources` | where a mod's source really lives, relative to the profile (`"."` = the repository root itself) |
| `build.exclude` | what must never be packed. Listing it **replaces** the default wholesale; the default is `.git`, `*.blend`, `*.blend1`, `.gitignore`, `.gitattributes`, `README.md`, `*.ps1` |
| `build.stage` | pack a filtered copy instead of refusing when something excluded is present — the layout a root-layout mod needs |
| `build.pre_script` | a PowerShell script run before packing, for projects that generate code first |
| `expect.ready_line` | the line the mod prints when it has finished loading — see below |
| `expect.counters` | `key = value` pairs carried by that line, compared numerically |
| `expect.max_warnings` | warning budget; omit the key to disable the check |
| `expect.forbid` | substrings that make a run bad regardless of anything else |
| `expect.error_regex` | regexes marking script errors that belong to you, used by the client compile check |
| `expect.noise` | extra engine noise to ignore, on top of the built-in list |

### The machine half — `dayz-mcp.local.toml`, never committed

| Key | Meaning |
|---|---|
| `machine.game` | the game installation; discovered automatically if absent |
| `machine.tools` | DayZ Tools; discovered automatically if absent |
| `machine.stand_root` | the prepared test stand. The server boots against it and its logs are read from `<stand_root>/profiles`. Defaults to `<root>/testenv` |
| `machine.config` | the server config filename inside the stand (default `serverDZ.cfg`). It is a setting because a stand can hold a config that hangs forever after world-compile and a working one under another name; it must resolve inside `stand_root` |
| `machine.port` | the port `server_start` passes to the server (default `2302`) |
| `mods.required` | mod folder names, resolved under the game's own `!Workshop` folder — `required = ["@CF"]` means `<game>/!Workshop/@CF` |
| `mods.extra` | full paths to anything else to load, for mods that do not live in `!Workshop` |
| `mods.server_only` | folder names to route to `-serverMod` instead of `-mod`. Matching is by folder name against every mod being loaded, wherever it came from — `mods.required`, `mods.extra` or the project's own `@Name` folders; everything not listed goes to `-mod`. The diagnostic client never loads server-only mods, so the client compile check drops them |

### The ready line

`server_start` finishes when `expect.ready_line` appears in a log written after
that boot began. It is the one thing the server cannot work out for itself, and
without it two things change: nothing can be waited for, so the boot job starts
the server, confirms it is still alive a moment later and finishes saying so;
and `log_verdict` has no line to read counters off, so `expect.counters` never
matches anything. Errors, crashes and the warning budget are still judged. A
profile without a ready line is supported, not broken — `project_open` says so
in its notes.

## Tools

| Tool | What it does |
|---|---|
| `project_open(path)` | read the profile, discover the game and tools, report what is missing |
| `project_status()` | current project, running server, recent jobs |
| `mod_build()` | pack and sign every declared mod; returns a job id. Refuses a second build of the same project while one is still running |
| `server_start(timeout)` | start the test server, finish when the ready line appears (or, with no ready line declared, as soon as the server is up — see above) |
| `server_status(pulse_seconds)` | pid, whether the process is alive, whether the log is growing (sampled `pulse_seconds` apart), and how long it has been stalled |
| `server_stop(pid)` | stop the server this session started (optional pid for orphaned servers) |
| `client_compile_check(extra_mods, wait_seconds)` | run the diagnostic client and read its logs |
| `log_verdict(source, since)` | pass/fail with reasons: counters, forbidden strings, warning budget. `source` is `"server"` (the newest log in the stand) or `"client"` (the log the latest `client_compile_check` produced). `since` (an epoch timestamp, e.g. the `since` `server_start` returns) refuses a log written before the run being judged, so a stale log from an earlier boot cannot be mistaken for this one's result |
| `log_tail(source, pattern, n)` | last lines, optionally filtered; same two sources |
| `job_status(job_id)` | status of a long-running job |
| `job_wait(job_id, timeout)` | wait for a job to finish |
| `job_artifacts(job_id)` | retrieve outputs from a completed job |
| `bridge_build()` | pack the bridge mod, whose sources ship with this server (`bridge/`), not with your project; returns a job id. Built **unsigned** — see below |
| `bridge_status(window)` | is the bridge inside the running game still ticking: reports the tick number and whether it advanced over `window` seconds. Succeeds only for a tick that actually moved, or for a stand that restarted mid-sample |
| `bridge_clear(force, probe_window)` | discard the command stuck in the mailbox, naming what it threw away. Refuses while the bridge looks alive unless `force=True` |

`job_wait` is the tool meant to wait, and its `timeout` is capped at **600
seconds** however large a value is passed. Two other tools sleep: `server_status`
samples the log twice, `pulse_seconds` apart, capped at 10 seconds — that pause
is how it tells a slow boot from a hung one — and `bridge_status` samples the
bridge's tick twice, `window` apart, capped at the same 10 seconds, for the same
reason. Everything else returns immediately; work that takes minutes happens
behind a job id.

### The bridge mod

`bridge_build` packs `bridge/` **from this server's own repository** into
`@DZMCP_Bridge` beside it. It is the server's mod, not yours: one copy serves
every project, nothing is written into your repository except the job record,
and no project's signing key is used — a `-serverMod` pbo is never handed to a
client to verify, so it is built **unsigned** and its output folder is kept free
of signatures and keys.

Building it does not load it. That stays your profile's decision, because the
bridge is an extra pbo in the stand and a run without it has to remain possible.
To attach it, add two lines to `dayz-mcp.local.toml` (the same two
`bridge_build`'s job summary prints):

```toml
[mods]
extra       = ["<path printed by bridge_build>/@DZMCP_Bridge"]
server_only = ["@DZMCP_Bridge"]
```

`server_only` is what routes it to `-serverMod` instead of `-mod`. Without it
the stand boots perfectly well and `bridge_status` reports that the bridge never
wrote any state — which is true, and easy to mistake for a broken bridge.

`bridge_status` also reports the command mailbox, because only the mod ever
empties it: a command sent while the stand was down, or before the bridge was
attached, is not discarded and does not expire on its own — it keeps blocking
every later send, and a stand booted outside these tools would pick it up.
`server_start` clears both transport files before every boot, so a server
started through this tool never runs a command from a previous session; that is
hygiene, not a substitute for knowing the command is there. The state comes back
as `stale_command`, and
`bridge_clear()` is the way out of it. Clearing is a separate tool on purpose:
throwing away a queued command is a decision, not something a status check
should do behind your back. It refuses while the bridge looks alive unless you
pass `force=True`, and either way it reports the command id it discarded.

### What `bridge_status` can tell apart

The tick alone is not enough to judge a bridge, because it restarts at 0 every
boot while the state file survives in the profile directory. Every answer
carries the channel's own verdict in `heartbeat`, and the four are genuinely
different facts:

| `state` | `heartbeat` | meaning |
|---|---|---|
| `alive` | `growing` | the tick moved within one session — the only `ok: true` liveness answer |
| `restarted` | `restarted` | a new world came up between the two samples: alive, **not** frozen, and anything sent to the old session is gone |
| `frozen` | `stalled` | the same world seen twice, not moving — a script-side problem, so `log_verdict` is the next step |
| `unknown` | `unmeasurable` | a sample could not be read (or `window=0`): no comparison was made. **Not** a diagnosis — call again |

`no_server`, `stale_command`, `no_state_file`, `unreadable_state` and
`outdated_bridge` come before any of that: nothing is running, a command is
wedged, the mod is not loaded, its file never parses, or the file parses but
predates this server's protocol (rebuild it with `bridge_build`).

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
  whose source directory contains anything matching `build.exclude` (the
  seven-pattern default is listed above) rather than silently shipping it
  inside the published pbo. It refuses regardless of `build.exclude` when the
  source contains this server's own artifacts -- the signing keys, either half
  of the profile, the job store, the mod's previous build -- because packing
  those publishes the private signing key, and no project should have to
  configure that away. By default it does not stage a filtered copy first: a
  copy is always newer than the sources, which would permanently disable the
  stale-pbo check above *if that check measured the copy*. `build.stage = true`
  opts into copying anyway -- safe only because the stale-pbo comparison always
  measures the original source tree, never the copy. This is the layout a mod
  whose source is the repository root needs (it always contains at least
  `.git`).
* **A verdict judges the whole log, not just your mod's lines.** `log_verdict`
  reads the log of the stand it is pointed at, so a stand shared with other
  mods counts their warnings against your `expect.max_warnings` budget and
  their errors as reasons. Two projects sharing one `machine.stand_root` will
  see each other's baseline. Either give each project its own stand, or set the
  budget knowing what else is loaded. A project-scoped filter, symmetric with
  `expect.error_regex`, is the obvious refinement and is not implemented.
* **`expect.noise` cannot rescue a line that already counts as an error.**
  Classification is ordered `forbid` → crash → error → noise → warning, so a
  line containing `ERROR` or `FATAL` (or one of your `forbid` strings) is
  decided before noise is consulted. That order is deliberate -- noise matching
  first would let an innocuous substring swallow a fatal line -- but it means
  `noise` can only suppress warnings and ordinary lines, never demote an
  error-level one.

## Install

    python -m pip install -e ".[dev]"
    python -m pytest

Register in your MCP client:

    { "mcpServers": { "dayz": { "command": "dayz-mcp" } } }

## Licence

GPL-3.0-or-later. See `NOTICE.md`.
